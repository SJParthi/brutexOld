"""Seam-wiring pins: the daily Rust route inside ``resample_1m_to_daily`` (T11).

Stem-matched to ``brutex/data/spot_daily_resample.py`` (the discovery-gate
law) so the module's gates cover the DEC-LAKE-RUST-RESAMPLE-DAILY-001 daily
branch: the OFF (default) byte-identity bomb-spy over the verbatim
extracted ``_resample_daily_frame_python``, the routed spy
(``oi_aware=False``, the frame oracle identity), the absent-wheel degrade
whose ON arm equals the OFF arm at ``CandleBar`` strength, and
validation-precedes-the-seam. The facade's own suite is
``tests/unit/test_lake_rust_resample_daily.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import sys
from datetime import date
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_resample_daily as lrd
from brutex.data import spot_daily_resample

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrd.reset_lake_rust_resample_daily_for_tests()
    yield
    lrd.reset_lake_rust_resample_daily_for_tests()


def _df_1m() -> pl.DataFrame:
    """Two IST sessions of spot 1-minute bars (2024-01-02 + 2024-01-03)."""
    open_utc = dt.datetime(2024, 1, 2, 3, 45, tzinfo=dt.UTC)
    instants = [open_utc + dt.timedelta(minutes=i) for i in range(3)]
    instants += [open_utc + dt.timedelta(days=1, minutes=i) for i in range(3)]
    n = len(instants)
    return pl.DataFrame(
        {
            "ts_utc": instants,
            "open": [100.25 + i for i in range(n)],
            "high": [110.5 + i for i in range(n)],
            "low": [90.75 + i for i in range(n)],
            "close": [105.05 + i for i in range(n)],
            "volume": [10 + i for i in range(n)],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))


_BEFORE = date(2024, 1, 4)  # both sessions complete


class TestSpotDailySeamWiring:
    def test_flag_off_never_touches_the_daily_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached with the flag OFF")

        monkeypatch.setattr(spot_daily_resample, "resample_daily_frame_via_rust", _bomb)
        bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=_BEFORE)
        assert len(bars) == 2
        assert all(bar.open_interest is None for bar in bars), "spot daily OI is None"
        # Midnight-IST stamp: 2024-01-02 IST session → 2024-01-01 18:30 UTC.
        assert bars[0].timestamp == dt.datetime(2024, 1, 1, 18, 30, tzinfo=dt.UTC)

    def test_flag_on_routes_with_oi_aware_false_and_the_frame_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        seen: list[tuple[date, bool]] = []

        def _spy(
            df: pl.DataFrame, before: date, *, oi_aware: bool, oracle: Any
        ) -> pl.DataFrame:
            seen.append((before, oi_aware))
            assert oracle is spot_daily_resample._resample_daily_frame_python
            return oracle(df, before)

        monkeypatch.setattr(spot_daily_resample, "resample_daily_frame_via_rust", _spy)
        bars = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=_BEFORE)
        assert seen == [(_BEFORE, False)]
        assert len(bars) == 2

    def test_flag_on_with_absent_wheel_equals_the_off_arm_at_candlebar_strength(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        expected = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=_BEFORE)
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        got = spot_daily_resample.resample_1m_to_daily(_df_1m(), before=_BEFORE)
        assert got == expected
        for g, e in zip(got, expected, strict=True):
            assert str(g.open) == str(e.open)
            assert str(g.close) == str(e.close)
            assert g.timestamp == e.timestamp

    def test_flag_on_empty_frame_is_the_early_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached for an empty frame")

        monkeypatch.setattr(spot_daily_resample, "resample_daily_frame_via_rust", _bomb)
        assert spot_daily_resample.resample_1m_to_daily(_df_1m().clear(), before=_BEFORE) == []

    def test_validation_raises_before_the_daily_seam_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("daily seam must not be reached for invalid input")

        monkeypatch.setattr(spot_daily_resample, "resample_daily_frame_via_rust", _bomb)
        bad = _df_1m().rename({"ts_utc": "wrong"})
        with pytest.raises(ValueError, match="missing required columns"):
            spot_daily_resample.resample_1m_to_daily(bad, before=_BEFORE)

    def test_extracted_frame_body_drops_oi_and_stamps_midnight_ist(self) -> None:
        # The verbatim-extraction law: _resample_daily_frame_python selects
        # exactly the OHLCV-6 (an OI column on the input is dropped — the
        # spot daily contract), stamps Datetime(us, UTC) midnight IST, and
        # applies the >= before drop.
        with_oi = _df_1m().with_columns(pl.lit(7, dtype=pl.Int64).alias("open_interest"))
        frame = spot_daily_resample._resample_daily_frame_python(with_oi, date(2024, 1, 3))
        assert frame.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        assert frame.schema["ts_utc"] == pl.Datetime("us", "UTC")
        assert frame.height == 1
        assert frame.get_column("ts_utc")[0] == dt.datetime(2024, 1, 1, 18, 30, tzinfo=dt.UTC)
