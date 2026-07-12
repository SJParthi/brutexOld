"""Spot window-tail wiring of the session-density census (DEC-SESSION-DENSITY-CENSUS-001).

The census fires exactly once per qualifying window (real-vendor opt-in +
dense INDEX + the fetched ``1minute`` + not session-exempt) and NEVER fires on
a CASH stock / the derived ``1day`` / an opted-out (dry-run-shaped) pull / an
exempt symbol — and it NEVER changes the ``WindowLedger.record`` call shape
(the no-demotion spy: record kwargs are identical census-on vs
kill-switched-off).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.adapters.groww.ingest import ingest_spot
from brutex.data.models import CandleBar
from brutex.data.session_density import (
    SESSION_DENSITY_KILL_ENV,
    SESSION_DENSITY_LOW_EVENT,
)
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")


def _minute_bar(d: date, hh: int, mm: int) -> CandleBar:
    ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str = "NIFTY", *, instrument: InstrumentType = InstrumentType.IDX) -> Instrument:
    is_index = instrument is InstrumentType.IDX
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=instrument,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if is_index else 1,
        tick_size=Decimal("0") if is_index else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _Adapter:
    """Fake MarketAdapter returning a fixed bar list for every window fetch."""

    def __init__(self, bars: Sequence[CandleBar]) -> None:
        self._bars = list(bars)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        return list(self._bars)


class _Sink:
    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, merge_mode
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _SpyLedger(WindowLedger):
    """Records every ``record`` call's kwargs (minus the wall clock)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def record(self, key: Any, **kwargs: Any) -> Any:
        self.calls.append((key, {k: v for k, v in kwargs.items() if k != "now"}))
        return super().record(key, **kwargs)


def _holey_week_bars() -> list[CandleBar]:
    """Tue 2024-01-02 + Thu 2024-01-04 full sessions; Wed 2024-01-03 MISSING.

    One interior fully-missing session — the census flags it while the
    interior-gap detector (tolerance 5) stays silent.
    """
    bars: list[CandleBar] = []
    for d in (date(2024, 1, 2), date(2024, 1, 4)):
        for i in range(375):
            hh, mm = divmod(9 * 60 + 15 + i, 60)
            bars.append(_minute_bar(d, hh, mm))
    return bars


def _run(
    *,
    instrument: Instrument,
    interval: str = "1minute",
    detect_completeness_gaps: bool = True,
    ledger: WindowLedger | None = None,
    observed_holidays: frozenset[date] = frozenset(),
) -> WindowLedger:
    led = ledger if ledger is not None else WindowLedger()
    ingest_spot(
        _Adapter(_holey_week_bars()),  # type: ignore[arg-type]
        _Sink(),
        instrument=instrument,
        interval=interval,
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=led,
        detect_completeness_gaps=detect_completeness_gaps,
        observed_holidays=observed_holidays,
    )
    return led


class TestSpotSessionDensityWiring:
    def test_index_1minute_window_fires_census_once(self) -> None:
        with capture_logs() as logs:
            _run(instrument=_inst())
        lows = [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT]
        assert len(lows) == 1
        assert lows[0]["session"] == "2024-01-03"
        assert lows[0]["shape"] == "missing_session"
        assert lows[0]["symbol"] == repr("NIFTY")

    def test_cash_stock_never_censused(self) -> None:
        with capture_logs() as logs:
            _run(instrument=_inst("RELIANCE", instrument=InstrumentType.EQ))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_daily_interval_never_censused(self) -> None:
        with capture_logs() as logs:
            _run(instrument=_inst(), interval="1day")
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_opted_out_pull_never_censused(self) -> None:
        # detect_completeness_gaps=False is the dry-run / synthetic posture.
        with capture_logs() as logs:
            _run(instrument=_inst(), detect_completeness_gaps=False)
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_session_exempt_symbol_never_censused(self) -> None:
        # The belt: GIFTNIFTY is session-exempt (a ~21h instrument) — its
        # density is not session-shaped even if a master ever flags it IDX.
        with capture_logs() as logs:
            _run(instrument=_inst("GIFTNIFTY"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_observed_overlay_threads_through_the_spot_tail(self) -> None:
        # `observed_holidays` must reach the census (the spot twin of the F&O
        # pin `test_observed_overlay_threads_through_the_real_tail`): with Wed
        # 2024-01-03 overlaid as an observed holiday the hole is a NON-session
        # (abstain), so no event fires — a regression dropping `observed=` at
        # the spot window-tail call site would flag it as a missing session
        # and fail here.
        with capture_logs() as logs:
            _run(
                instrument=_inst(),
                observed_holidays=frozenset({date(2024, 1, 3)}),
            )
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_ledger_record_shape_identical_census_on_and_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The no-demotion spy: the census NEVER touches WindowLedger.record —
        # the full record kwargs are byte-identical with the census enabled vs
        # kill-switched off, and the window stays whatever the other detectors
        # decided (visibility only, never a re-pull action).
        monkeypatch.delenv(SESSION_DENSITY_KILL_ENV, raising=False)
        on = _SpyLedger()
        _run(instrument=_inst(), ledger=on)
        monkeypatch.setenv(SESSION_DENSITY_KILL_ENV, "0")
        off = _SpyLedger()
        _run(instrument=_inst(), ledger=off)
        assert on.calls == off.calls
        assert len(on.calls) >= 1
