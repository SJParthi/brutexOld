"""Seam-wiring pins: the daily Rust route inside ``resample_1m_to_fno_daily`` (T11).

Stem-matched to ``brutex/data/fno_daily_resample.py`` so the module's 100%
line+branch gate covers the DEC-LAKE-RUST-RESAMPLE-DAILY-001 daily branch:
the OFF (default) byte-identity bomb-spy, the routed-with-the-frame-oracle
spy (``oi_aware=True``), the absent-wheel degrade whose ON-arm
materialization must equal the OFF arm at ``CandleBar`` strength INCLUDING
``Decimal`` reprs, and validation-precedes-the-seam. The facade's own
suite is ``tests/unit/test_lake_rust_resample_daily.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import sys
from datetime import date
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import fno_daily_resample
from brutex.data import lake_rust_resample_daily as lrd

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrd.reset_lake_rust_resample_daily_for_tests()
    yield
    lrd.reset_lake_rust_resample_daily_for_tests()


def _df_1m(*, null_last_oi: bool = False) -> pl.DataFrame:
    """Two IST sessions of F&O 1-minute bars (2024-01-02 + 2024-01-03)."""
    open_utc = dt.datetime(2024, 1, 2, 3, 45, tzinfo=dt.UTC)
    instants = [open_utc + dt.timedelta(minutes=i) for i in range(3)]
    instants += [open_utc + dt.timedelta(days=1, minutes=i) for i in range(3)]
    n = len(instants)
    oi: list[int | None] = [1_000 + i for i in range(n)]
    if null_last_oi:
        oi[2] = None  # the first session's LAST bar carries a null OI
    return pl.DataFrame(
        {
            "ts_utc": instants,
            "open": [100.25 + i for i in range(n)],
            "high": [110.5 + i for i in range(n)],
            "low": [90.75 + i for i in range(n)],
            "close": [105.05 + i for i in range(n)],
            "volume": [10 + i for i in range(n)],
            "open_interest": oi,
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))


_BEFORE = date(2024, 1, 4)  # both sessions complete


class TestFnoDailySeamWiring:
    def test_flag_off_never_touches_the_daily_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached with the flag OFF")

        monkeypatch.setattr(fno_daily_resample, "resample_daily_frame_via_rust", _bomb)
        bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=_BEFORE)
        assert len(bars) == 2
        assert bars[0].open_interest == 1_002, "carry-LAST session OI"
        assert bars[1].open_interest == 1_005

    def test_flag_on_routes_with_oi_aware_true_and_the_frame_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        seen: list[tuple[date, bool]] = []

        def _spy(
            df: pl.DataFrame, before: date, *, oi_aware: bool, oracle: Any
        ) -> pl.DataFrame:
            seen.append((before, oi_aware))
            assert oracle is fno_daily_resample._fno_daily_frame_python
            return oracle(df, before)

        monkeypatch.setattr(fno_daily_resample, "resample_daily_frame_via_rust", _spy)
        bars = fno_daily_resample.resample_1m_to_fno_daily(_df_1m(), before=_BEFORE)
        assert seen == [(_BEFORE, True)]
        assert len(bars) == 2

    def test_flag_on_with_absent_wheel_equals_the_off_arm_at_candlebar_strength(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing ON≡OFF wiring proof (fake-crate tier — the real
        # wheel's twin lives in the parity corpus): flag ON + no wheel
        # serves the FRAME oracle, and the ON arm's materialization loop
        # must reproduce the OFF arm's CandleBars EXACTLY — timestamps,
        # Decimal REPRS (str(bar.open) etc.), volumes, OI incl. the
        # null-last honest edge.
        for null_last_oi in (False, True):
            monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
            expected = fno_daily_resample.resample_1m_to_fno_daily(
                _df_1m(null_last_oi=null_last_oi), before=_BEFORE
            )
            monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
            monkeypatch.setitem(sys.modules, "brutex_lake", None)
            lrd.reset_lake_rust_resample_daily_for_tests()
            got = fno_daily_resample.resample_1m_to_fno_daily(
                _df_1m(null_last_oi=null_last_oi), before=_BEFORE
            )
            assert got == expected, f"null_last_oi={null_last_oi}"
            for g, e in zip(got, expected, strict=True):
                assert str(g.open) == str(e.open)
                assert str(g.high) == str(e.high)
                assert str(g.low) == str(e.low)
                assert str(g.close) == str(e.close)
                assert g.timestamp == e.timestamp
                assert g.open_interest == e.open_interest
            if null_last_oi:
                assert got[0].open_interest is None, "null last-of-session OI stays None"

    def test_flag_on_empty_frame_is_the_early_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached for an empty frame")

        monkeypatch.setattr(fno_daily_resample, "resample_daily_frame_via_rust", _bomb)
        assert fno_daily_resample.resample_1m_to_fno_daily(_df_1m().clear(), before=_BEFORE) == []

    def test_validation_raises_before_the_daily_seam_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached for invalid input")

        monkeypatch.setattr(fno_daily_resample, "resample_daily_frame_via_rust", _bomb)
        bad = _df_1m().rename({"ts_utc": "wrong"})
        with pytest.raises(ValueError, match="missing required columns"):
            fno_daily_resample.resample_1m_to_fno_daily(bad, before=_BEFORE)

    def test_frame_oracle_without_oi_column_yields_none_oi_bars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The defensive stamped_has_oi read: an OI-less frame (hand-built /
        # the 1-minute no-op branch) materializes honest None OI on the ON
        # arm exactly like the OFF arm's defensive read.
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        no_oi = _df_1m().drop("open_interest")
        expected = fno_daily_resample.resample_1m_to_fno_daily(no_oi, before=_BEFORE)
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        got = fno_daily_resample.resample_1m_to_fno_daily(no_oi, before=_BEFORE)
        assert got == expected
        assert all(bar.open_interest is None for bar in got)

    def test_frame_oracle_matches_the_off_arm_shape_law(self) -> None:
        # _fno_daily_frame_python: midnight-IST stamped, Datetime(us, UTC),
        # OHLCV + OI columns, forming session dropped at >= before.
        frame = fno_daily_resample._fno_daily_frame_python(_df_1m(), date(2024, 1, 3))
        assert frame.columns == [
            "ts_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]
        assert frame.schema["ts_utc"] == pl.Datetime("us", "UTC")
        assert frame.height == 1, "the second (>= before) session is dropped"
        stamp = frame.get_column("ts_utc")[0]
        assert stamp == dt.datetime(2024, 1, 1, 18, 30, tzinfo=dt.UTC), (
            "midnight IST == 18:30 UTC of the prior calendar day"
        )
