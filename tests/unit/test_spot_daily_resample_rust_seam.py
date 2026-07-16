"""Seam-wiring pins: the Rust daily-restamp gate inside ``spot_daily_resample``.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-15 fold — orchestration
keeps its injected seams; the resample callable routes to the crate).
Stem-matched to ``brutex/data/spot_daily_resample.py`` (the discovery-gate
law) so the module's 100% line+branch gates cover the seam branch: the OFF
(default) byte-identity bomb-spy, the ON-with-absent-wheel oracle degrade,
the routed-through-the-seam spy, and the extracted frame pipeline's
``keep_oi`` law. The facade's own suite is
``tests/unit/test_lake_rust_resample_siblings.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import sys
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_resample as lrs
from brutex.data import spot_daily_resample

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


def _df_1m() -> pl.DataFrame:
    """Two 1-minute bars at the 2024-01-01 09:15 IST open (03:45 UTC)."""
    return pl.DataFrame(
        {
            "ts_utc": [dt.datetime(2024, 1, 1, 3, 45), dt.datetime(2024, 1, 1, 3, 46)],
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "volume": [10, 11],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us")))


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: object | None) -> None:
    """Install a fake crate (or a poisoned ``None`` -> ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class TestSeamWiring:
    def test_flag_off_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The daily resampler imports the seam LAZILY from the facade module,
        # so the bomb patches the facade's own attribute.
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached with the flag OFF")

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _bomb)
        before = _SESSION + dt.timedelta(days=1)
        bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=before)
        assert len(bars) == 1
        # Midnight IST 2024-01-01 == 18:30 UTC 2023-12-31.
        assert bars[0].timestamp == dt.datetime(2023, 12, 31, 18, 30, tzinfo=dt.UTC)
        assert bars[0].open_interest is None

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        before = _SESSION + dt.timedelta(days=1)
        on_bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=before)
        lrs.reset_lake_rust_resample_for_tests()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off_bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=before)
        assert on_bars == off_bars
        assert len(on_bars) == 1

    def test_flag_on_routes_through_the_seam_keep_oi_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[dt.date, bool]] = []

        def _spy(
            daily: pl.DataFrame, before: dt.date, *, keep_oi: bool, oracle: Any
        ) -> pl.DataFrame:
            seen.append((before, keep_oi))
            assert oracle is spot_daily_resample._daily_restamp_frame_python
            return oracle(daily, before)

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _spy)
        before = _SESSION + dt.timedelta(days=1)
        bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=before)
        assert seen == [(before, False)]
        assert len(bars) == 1

    def test_empty_input_never_touches_the_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached for empty input")

        monkeypatch.setattr(lrs, "daily_restamp_via_rust", _bomb)
        assert spot_daily_resample.resample_1m_to_daily(_df_1m().head(0), before=_SESSION) == []


class TestDailyRestampFramePython:
    def test_keep_oi_true_appends_oi_when_present(self) -> None:
        # The F&O arm of the ONE shared pipeline: OI (carry-last, from the
        # resampler) survives the select; the spot default drops it.
        from brutex.data.tf_resampler import resample_1m_to_tf

        df = _df_1m().with_columns(pl.Series("open_interest", [1000, 1001]))
        daily = resample_1m_to_tf(df, 375)
        before = _SESSION + dt.timedelta(days=1)
        fno = spot_daily_resample._daily_restamp_frame_python(daily, before, keep_oi=True)
        assert fno.columns == [
            "ts_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]
        assert fno.get_column("open_interest").to_list() == [1001]
        spot = spot_daily_resample._daily_restamp_frame_python(daily, before)
        assert spot.columns == ["ts_utc", "open", "high", "low", "close", "volume"]

    def test_keep_oi_true_without_oi_column_stays_ohlcv6(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        daily = resample_1m_to_tf(_df_1m(), 375)
        before = _SESSION + dt.timedelta(days=1)
        out = spot_daily_resample._daily_restamp_frame_python(daily, before, keep_oi=True)
        assert out.columns == ["ts_utc", "open", "high", "low", "close", "volume"]

    def test_restamp_is_idempotent(self) -> None:
        # Restamping an already-restamped frame is a fixed point — the law
        # the F&O ON arm's retained materialisation loop relies on.
        from brutex.data.tf_resampler import resample_1m_to_tf

        daily = resample_1m_to_tf(_df_1m(), 375)
        before = _SESSION + dt.timedelta(days=1)
        once = spot_daily_resample._daily_restamp_frame_python(daily, before)
        twice = spot_daily_resample._daily_restamp_frame_python(once, before)
        assert once.schema == twice.schema
        assert once.equals(twice)
