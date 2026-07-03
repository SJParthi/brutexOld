"""Segment-aware pre-build routing tests (DEC-PREBUILD-SELECT-001 + DEC-PIPELINE-AUTOPREBUILD-001).

Covers the segment classification + resampler routing in
``brutex.data.timeframe_materialize``:

* :func:`prebuild_kind_for` maps INDEX/CASH → SPOT, FNO+FUT → FUTURES,
  FNO+CE/PE → OPTIONS;
* :func:`intraday_resampler_for` returns the SPOT resampler for SPOT and the
  OI-aware F&O resampler for BOTH FUTURES and OPTIONS
  (DEC-PIPELINE-AUTOPREBUILD-001 enabled option materialisation — supersedes
  the earlier ``None``-for-OPTIONS on-demand design);
* :func:`daily_resample_fn_for` returns the spot midnight-IST daily for SPOT
  and the carry-last-OI F&O daily for FUTURES/OPTIONS (the extracted COMMON
  core of the removed mid-pull ``_derive_daily_from_minute`` selection);
* :func:`materialize_timeframes` with an injected ``resampler`` routes through
  it (FUTURES/OPTIONS preserve OI), and the default (``resampler=None``) is the
  SPOT path — byte-identical to the pre-change behaviour (golden bit-identity);
* the regression: an F&O contract is NO longer routed through the
  OI-dropping SPOT resampler.

Pure — fake ``write_tf``, REAL resamplers, polars fixtures. No lake / FastAPI.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional
from brutex.data.fno_resample import derive_fno_intraday_tfs
from brutex.data.spot_daily_resample import resample_1m_to_daily_default
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframe_materialize import (
    DAILY_TF_LABEL,
    PrebuildKind,
    daily_resample_fn_for,
    intraday_resampler_for,
    materialize_timeframes,
    prebuild_kind_for,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _df_1m(n: int = 90, *, with_oi: bool = False) -> pl.DataFrame:
    """One session of 1-minute bars anchored at 09:15 IST (03:45 UTC)."""
    base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
    data: dict[str, object] = {
        "ts_utc": [base + timedelta(minutes=i) for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    }
    if with_oi:
        data["open_interest"] = [5000 + 10 * i for i in range(n)]
    return pl.DataFrame(data)


def _spot(symbol: str = "NSE-NIFTY", segment: Segment = Segment.INDEX) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX if segment is Segment.INDEX else InstrumentType.EQ,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if segment is Segment.INDEX else 1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _future(symbol: str = "NSE-NIFTY-25JAN-FUT") -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=2,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=None,
    )


def _option(itype: InstrumentType = InstrumentType.CE) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=3,
        symbol_name=f"NSE-NIFTY-25JAN-20000-{itype.value}",
        display_name="opt",
        instrument=itype,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("20000"),
    )


class _Recorder:
    def __init__(self) -> None:
        self.writes: list[tuple[str, pl.DataFrame]] = []

    def __call__(self, tf: str, frame: pl.DataFrame) -> int:
        self.writes.append((tf, frame))
        return frame.height


# ── prebuild_kind_for ─────────────────────────────────────────────────────


class TestPrebuildKindFor:
    def test_index_is_spot(self) -> None:
        assert prebuild_kind_for(_spot(segment=Segment.INDEX)) is PrebuildKind.SPOT

    def test_cash_equity_is_spot(self) -> None:
        assert prebuild_kind_for(_spot(symbol="NSE-RELIANCE", segment=Segment.CASH)) is (
            PrebuildKind.SPOT
        )

    def test_future_is_futures(self) -> None:
        assert prebuild_kind_for(_future()) is PrebuildKind.FUTURES

    @pytest.mark.parametrize("itype", [InstrumentType.CE, InstrumentType.PE])
    def test_option_is_options(self, itype: InstrumentType) -> None:
        assert prebuild_kind_for(_option(itype)) is PrebuildKind.OPTIONS


# ── intraday_resampler_for ────────────────────────────────────────────────


class TestIntradayResamplerFor:
    def test_spot_returns_spot_resampler(self) -> None:
        assert intraday_resampler_for(PrebuildKind.SPOT) is derive_intraday_tfs

    def test_futures_returns_fno_resampler(self) -> None:
        assert intraday_resampler_for(PrebuildKind.FUTURES) is derive_fno_intraday_tfs

    def test_options_returns_fno_resampler(self) -> None:
        """REGRESSION FLIP (DEC-PIPELINE-AUTOPREBUILD-001): OPTIONS materialise
        OI-aware like FUTURES — the old pin was ``is None`` (on-demand only)."""
        assert intraday_resampler_for(PrebuildKind.OPTIONS) is derive_fno_intraday_tfs


# ── daily_resample_fn_for (the extracted daily-derive selection) ───────────


class TestDailyResampleFnFor:
    def test_spot_returns_spot_daily(self) -> None:
        assert daily_resample_fn_for(PrebuildKind.SPOT) is resample_1m_to_daily_default

    @pytest.mark.parametrize("kind", [PrebuildKind.FUTURES, PrebuildKind.OPTIONS])
    def test_fno_kinds_return_carry_last_oi_daily(self, kind: PrebuildKind) -> None:
        assert daily_resample_fn_for(kind) is resample_1m_to_fno_daily_positional

    def test_daily_tf_label_is_the_lake_interval(self) -> None:
        assert DAILY_TF_LABEL == "1day"


# ── materialize_timeframes resampler routing ──────────────────────────────


class TestMaterializeRouting:
    def test_default_resampler_is_spot_byte_identical(self) -> None:
        """resampler=None ⇒ SPOT path; output byte-identical to the spot resampler."""
        df = _df_1m()
        derived = derive_intraday_tfs(df)
        rec = _Recorder()

        result = materialize_timeframes(
            tfs=("5m", "15m", "60m"),
            read_1m=lambda: df,
            write_tf=rec,
        )

        for tf in ("5m", "15m", "60m"):
            assert result.written[tf] == derived[tf].height
            written_frame = next(f for t, f in rec.writes if t == tf)
            assert written_frame.equals(derived[tf])  # byte-identical (golden)

    def test_explicit_spot_resampler_matches_default(self) -> None:
        df = _df_1m()
        rec_default = _Recorder()
        rec_explicit = _Recorder()

        materialize_timeframes(tfs=("5m",), read_1m=lambda: df, write_tf=rec_default)
        materialize_timeframes(
            tfs=("5m",),
            read_1m=lambda: df,
            write_tf=rec_explicit,
            resampler=intraday_resampler_for(PrebuildKind.SPOT),
        )

        assert rec_default.writes[0][1].equals(rec_explicit.writes[0][1])

    def test_futures_resampler_preserves_open_interest(self) -> None:
        """The OI-aware FUTURES resampler carries open_interest into the frames."""
        df = _df_1m(with_oi=True)
        rec = _Recorder()

        result = materialize_timeframes(
            tfs=("5m",),
            read_1m=lambda: df,
            write_tf=rec,
            resampler=intraday_resampler_for(PrebuildKind.FUTURES),
        )

        assert result.written["5m"] > 0
        frame = rec.writes[0][1]
        assert "open_interest" in frame.columns  # the OI column survives (regression)

    def test_options_resampler_preserves_open_interest(self) -> None:
        """OPTIONS route through the SAME OI-aware F&O resampler as FUTURES
        (DEC-PIPELINE-AUTOPREBUILD-001 — option bars are OI-carrying F&O bars)."""
        df = _df_1m(with_oi=True)
        rec = _Recorder()

        result = materialize_timeframes(
            tfs=("5m",),
            read_1m=lambda: df,
            write_tf=rec,
            resampler=intraday_resampler_for(PrebuildKind.OPTIONS),
        )

        assert result.written["5m"] > 0
        assert "open_interest" in rec.writes[0][1].columns

    def test_regression_futures_not_routed_through_oi_dropping_spot(self) -> None:
        """REGRESSION: an F&O futures frame keeps OI through the prebuild path.

        Before DEC-PREBUILD-SELECT-001 the route ALWAYS used the SPOT read builder
        (which selects only OHLCV — no ``open_interest`` column), so an F&O
        contract's OI was silently dropped. The fix routes FUTURES through an
        OI-carrying read + the OI-aware resampler. This pins both halves:

        * the SPOT path's input has NO ``open_interest`` (the spot read selects
          only OHLCV) → the derived frame has no OI;
        * the FUTURES path's input HAS ``open_interest`` (the OI-aware read keeps
          it) → the derived frame carries it.
        """
        df_spot = _df_1m(with_oi=False)  # what the spot read builder yields
        df_fut = _df_1m(with_oi=True)  # what the OI-aware futures read yields
        rec_spot = _Recorder()
        rec_fut = _Recorder()

        materialize_timeframes(
            tfs=("5m",),
            read_1m=lambda: df_spot,
            write_tf=rec_spot,
            resampler=intraday_resampler_for(PrebuildKind.SPOT),
        )
        materialize_timeframes(
            tfs=("5m",),
            read_1m=lambda: df_fut,
            write_tf=rec_fut,
            resampler=intraday_resampler_for(PrebuildKind.FUTURES),
        )

        assert "open_interest" not in rec_spot.writes[0][1].columns
        assert "open_interest" in rec_fut.writes[0][1].columns
