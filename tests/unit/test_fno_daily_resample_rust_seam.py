"""Seam-wiring pins: the Rust daily-restamp gate inside ``fno_daily_resample``.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-14 fold — the frame
stage routes to the crate; the ``Decimal(str(x))`` repr edges + the
stdlib-UTC stamp stay Python-side, output objects stay Python-side).
Stem-matched to ``brutex/data/fno_daily_resample.py`` (the discovery-gate
law) so the module's 100% line+branch gates cover the seam branch: the OFF
(default) byte-identity bomb-spy, the ON-with-absent-wheel degrade producing
ATTRIBUTE-identical bars, the routed-through-the-seam spy (``keep_oi=True``),
and the retained loop's restamp/filter IDEMPOTENCE law. The facade's own
suite is ``tests/unit/test_lake_rust_resample_siblings.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import sys
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import fno_daily_resample
from brutex.data import lake_rust_resample as lrs

if TYPE_CHECKING:
    from collections.abc import Iterator

#: 2024-01-01 (a Monday) — the fixture session's IST date.
_SESSION = dt.date(2024, 1, 1)


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the seam latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m(*, with_oi: bool = True) -> pl.DataFrame:
    """Two F&O 1-minute bars at the 2024-01-01 09:15 IST open (03:45 UTC)."""
    frame = pl.DataFrame(
        {
            "ts_utc": [dt.datetime(2024, 1, 1, 3, 45), dt.datetime(2024, 1, 1, 3, 46)],
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "volume": [10, 11],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us")))
    if with_oi:
        frame = frame.with_columns(pl.Series("open_interest", [1000, 1001]))
    return frame


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: object | None) -> None:
    """Install a fake crate (or a poisoned ``None`` -> ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class TestSeamWiring:
    def test_flag_off_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached with the flag OFF")

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _bomb)
        before = _SESSION + dt.timedelta(days=1)
        bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=before)
        assert len(bars) == 1
        assert bars[0].timestamp == dt.datetime(2023, 12, 31, 18, 30, tzinfo=dt.UTC)
        assert bars[0].open_interest == 1001  # carry-LAST snapshot

    def test_flag_on_with_absent_wheel_builds_attribute_identical_bars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ON arm (here degraded to the frame ORACLE by the absent wheel)
        # must build bars ATTRIBUTE-identical to the OFF arm — incl. the
        # stdlib timezone.utc stamp the retained loop re-mints (the row-14
        # "output objects stay Python-side" law).
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        before = _SESSION + dt.timedelta(days=1)
        on_bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=before)
        lrs.reset_lake_rust_resample_for_tests()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off_bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=before)
        assert on_bars == off_bars
        assert len(on_bars) == 1
        assert on_bars[0].timestamp.tzinfo is dt.UTC
        assert str(on_bars[0].open) == str(off_bars[0].open)

    def test_flag_on_routes_through_the_seam_keep_oi_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[dt.date, bool]] = []

        def _spy(
            daily: pl.DataFrame, before: dt.date, *, keep_oi: bool, oracle: Any
        ) -> pl.DataFrame:
            seen.append((before, keep_oi))
            assert oracle is fno_daily_resample._fno_daily_restamp_oracle
            return oracle(daily, before)

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _spy)
        before = _SESSION + dt.timedelta(days=1)
        bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=before)
        assert seen == [(before, True)]
        assert len(bars) == 1
        assert bars[0].open_interest == 1001

    def test_empty_input_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached for empty input")

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _bomb)
        assert fno_daily_resample.resample_1m_to_fno_daily(_df_1m().head(0), before=_SESSION) == []


class TestRetainedLoopIdempotence:
    def test_loop_over_restamped_frame_equals_loop_over_raw_daily(self) -> None:
        # The ON-arm law: the verbatim loop's own astimezone(_IST) re-stamp
        # and >= before filter are IDEMPOTENT over the already-restamped
        # frame, so ONE retained loop serves both arms identically.
        from brutex.data.tf_resampler import resample_1m_to_tf

        before = _SESSION + dt.timedelta(days=1)
        daily = resample_1m_to_tf(_df_1m(), 375)
        restamped = fno_daily_resample._fno_daily_restamp_oracle(daily, before)
        raw_bars = fno_daily_resample._daily_frame_to_fno_bars(daily, before)
        restamped_bars = fno_daily_resample._daily_frame_to_fno_bars(restamped, before)
        assert raw_bars == restamped_bars
        assert len(raw_bars) == 1

    def test_oracle_keeps_oi_and_null_oi_stays_null(self) -> None:
        # A null LAST-bar OI stays an honest None through frame stage + loop
        # (the module's documented carry-last edge) — on BOTH shapes.
        from brutex.data.tf_resampler import resample_1m_to_tf

        df = _df_1m().with_columns(pl.Series("open_interest", [1000, None], dtype=pl.Int64))
        before = _SESSION + dt.timedelta(days=1)
        daily = resample_1m_to_tf(df, 375)
        restamped = fno_daily_resample._fno_daily_restamp_oracle(daily, before)
        assert restamped.get_column("open_interest").to_list() == [None]
        bars = fno_daily_resample._daily_frame_to_fno_bars(restamped, before)
        assert bars[0].open_interest is None

    def test_oracle_without_oi_column_stays_ohlcv6(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        before = _SESSION + dt.timedelta(days=1)
        daily = resample_1m_to_tf(_df_1m(with_oi=False), 375)
        restamped = fno_daily_resample._fno_daily_restamp_oracle(daily, before)
        assert restamped.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        bars = fno_daily_resample._daily_frame_to_fno_bars(restamped, before)
        assert bars[0].open_interest is None
