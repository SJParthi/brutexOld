"""F&O window-tail wiring of the session-density census (DEC-SESSION-DENSITY-CENSUS-001).

The FUT-only scope gate (shared verbatim with `_observe_partial_window` —
options are structurally excluded) + the ``1minute`` interval belt, driven
through the REAL ``_observe_session_density`` helper over hand-built
``_OptionRawWindow`` shapes (the `_observe_partial_window` test template) —
PLUS the real-tail end-to-end proof (review round-1): the census event firing
from the ACTUAL ``_process_option_window_tail`` entry point with its REAL
call-site arguments (session-FILTERED bars, ``observed=`` threaded), so a
call-site argument regression can never hide behind a source-substring pin.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.adapters.groww import option_ingest as _oi
from brutex.adapters.protocol import DerivativeContract
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.session_density import SESSION_DENSITY_LOW_EVENT
from brutex.data.sink import SinkAppendResult
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")


def _minute_bar(d: date, hh: int, mm: int) -> CandleBar:
    ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.0"),
        high=Decimal("110.0"),
        low=Decimal("90.0"),
        close=Decimal("105.0"),
        volume=50,
        open_interest=7,
    )


def _holey_week_bars() -> list[CandleBar]:
    """Tue 2024-01-02 + Thu 2024-01-04 full sessions; Wed 2024-01-03 MISSING."""
    bars: list[CandleBar] = []
    for d in (date(2024, 1, 2), date(2024, 1, 4)):
        for i in range(375):
            hh, mm = divmod(9 * 60 + 15 + i, 60)
            bars.append(_minute_bar(d, hh, mm))
    return bars


def _raw_window(
    *,
    contract_type: str,
    interval: str = "1minute",
    raw_bars: list[CandleBar] | None = None,
) -> _oi._OptionRawWindow:
    is_option = contract_type in {"CE", "PE"}
    symbol = "NSE-NIFTY-28Mar24-22000-CE" if is_option else "NSE-NIFTY-28Mar24-FUT"
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 3, 28),
        symbol=symbol,
        contract_type=contract_type,
        strike=22000.0 if is_option else None,
    )
    key = WindowKey(
        market="groww-option" if is_option else "groww-future",
        security_id=contract.symbol,
        interval=interval,
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
    )
    return _oi._OptionRawWindow(
        contract=contract,
        interval=interval,
        exchange="NSE",
        key=key,
        raw_bars=_holey_week_bars() if raw_bars is None else raw_bars,
        fetch_count=1,
        settle_empty=False,
        old_window=False,
        attempt=0,
    )


class _Sink:
    """Minimal accepting sink for driving the real consumer tail."""

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


def _run_real_tail(
    raw: _oi._OptionRawWindow, *, observed_holidays: frozenset[date] = frozenset()
) -> None:
    """Drive the REAL consumer tail (`_process_option_window_tail`) end-to-end."""
    _oi._process_option_window_tail(
        _Sink(),
        raw,
        ledger=None,
        on_success=None,
        pull_id="pid-1",
        fields={"symbol": raw.contract.symbol, "interval": raw.interval},
        observed_holidays=observed_holidays,
    )


def _observe(raw: _oi._OptionRawWindow) -> None:
    _oi._observe_session_density(
        raw,
        list(raw.raw_bars),
        pull_id="pid-1",
        fields={"symbol": raw.contract.symbol, "interval": raw.interval},
    )


class TestFnoSessionDensityWiring:
    def test_futures_window_fires_census_once(self) -> None:
        with capture_logs() as logs:
            _observe(_raw_window(contract_type="FUT"))
        lows = [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT]
        assert len(lows) == 1
        assert lows[0]["session"] == "2024-01-03"
        assert lows[0]["symbol"] == repr("NSE-NIFTY-28Mar24-FUT")

    def test_call_option_is_structurally_excluded(self) -> None:
        with capture_logs() as logs:
            _observe(_raw_window(contract_type="CE"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_put_option_is_structurally_excluded(self) -> None:
        with capture_logs() as logs:
            _observe(_raw_window(contract_type="PE"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_non_minute_interval_is_belted_out(self) -> None:
        # The per-minute-bucket expectation math only holds on the 1-minute
        # base — a (hypothetical) non-1minute FUT window is belted out.
        with capture_logs() as logs:
            _observe(_raw_window(contract_type="FUT", interval="1day"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_census_runs_at_the_real_tail_beside_partial_window(self) -> None:
        # Wiring-order pin: `_process_option_window_tail` calls the census
        # helper beside `_observe_partial_window`. The BEHAVIOR (the census
        # event firing from the real tail with the real call-site arguments)
        # is proven by `TestFnoSessionDensityRealTail` below; this substring
        # check only pins that both observers live in the SAME tail body.
        import inspect

        src = inspect.getsource(_oi)
        tail_body = src.split("def _process_option_window_tail", 1)[1].split("\ndef ", 1)[0]
        assert "_observe_session_density(" in tail_body
        assert "_observe_partial_window(" in tail_body


class TestFnoSessionDensityRealTail:
    """End-to-end through the REAL consumer tail (review round-1 fix): the
    census event fires from `_process_option_window_tail` itself, judged over
    the session-FILTERED bars with `observed=` threaded — so a call-site
    argument regression (raw bars / dropped overlay) fails here, not just a
    substring pin."""

    def test_holey_fut_window_fires_census_from_the_real_tail(self) -> None:
        # The raw bars carry a stray SATURDAY (2024-01-06) bar the session
        # filter must drop before the census: with the FILTERED bars the
        # covered span ends Thu 2024-01-04 and EXACTLY ONE session (Wed
        # 2024-01-03) flags. A regression passing raw.raw_bars instead would
        # widen the span to Sat and ALSO flag Fri 2024-01-05 — two events.
        raw = _raw_window(
            contract_type="FUT",
            raw_bars=[*_holey_week_bars(), _minute_bar(date(2024, 1, 6), 10, 0)],
        )
        with capture_logs() as logs:
            _run_real_tail(raw)
        lows = [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT]
        assert len(lows) == 1
        assert lows[0]["session"] == "2024-01-03"
        assert lows[0]["symbol"] == repr("NSE-NIFTY-28Mar24-FUT")

    def test_ce_window_never_fires_from_the_real_tail(self) -> None:
        with capture_logs() as logs:
            _run_real_tail(_raw_window(contract_type="CE"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_pe_window_never_fires_from_the_real_tail(self) -> None:
        with capture_logs() as logs:
            _run_real_tail(_raw_window(contract_type="PE"))
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []

    def test_observed_overlay_threads_through_the_real_tail(self) -> None:
        # `observed_holidays` must reach the census: with Wed 2024-01-03
        # overlaid as an observed holiday the hole is a NON-session (abstain),
        # so no event fires — a regression dropping `observed=` at the call
        # site would flag it as a missing session and fail here.
        with capture_logs() as logs:
            _run_real_tail(
                _raw_window(contract_type="FUT"),
                observed_holidays=frozenset({date(2024, 1, 3)}),
            )
        assert [e for e in logs if e["event"] == SESSION_DENSITY_LOW_EVENT] == []
