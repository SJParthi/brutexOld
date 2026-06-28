"""Segment-aware pre-build routing tests (DEC-PREBUILD-SELECT-001).

Covers the NEW segment classification + resampler routing added to
``brutex.data.timeframe_materialize``:

* :func:`prebuild_kind_for` maps INDEX/CASH → SPOT, FNO+FUT → FUTURES,
  FNO+CE/PE → OPTIONS;
* :func:`intraday_resampler_for` returns the SPOT resampler, the OI-aware
  FUTURES resampler, and ``None`` for OPTIONS;
* :func:`materialize_timeframes` with an injected ``resampler`` routes through
  it (FUTURES preserves OI), and the default (``resampler=None``) is the SPOT
  path — byte-identical to the pre-change behaviour (golden bit-identity);
* the regression: an F&O FUTURES contract is NO longer routed through the
  OI-dropping SPOT resampler.

Pure — fake ``write_tf``, REAL resamplers, polars fixtures. No lake / FastAPI.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
from brutex.data.fno_resample import derive_fno_intraday_tfs
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframe_materialize import (
    PrebuildKind,
    intraday_resampler_for,
    materialize_lake_timeframes,
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

    def test_options_returns_none(self) -> None:
        assert intraday_resampler_for(PrebuildKind.OPTIONS) is None


# ── materialize_lake_timeframes OPTIONS guard ─────────────────────────────


def test_materialize_lake_timeframes_rejects_options(tmp_path: object) -> None:
    """The shared helper refuses OPTIONS — option intraday is on-demand, never built."""
    with pytest.raises(ValueError, match="on-demand"):
        materialize_lake_timeframes(
            lake_root=tmp_path,  # type: ignore[arg-type]  — never read; raises before any I/O
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-25JAN-20000-CE",
            kind=PrebuildKind.OPTIONS,
        )


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
