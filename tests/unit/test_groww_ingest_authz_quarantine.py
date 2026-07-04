"""Spot-spine authorisation split — quarantine vs fleet storm (DEC-GROWW-AUTHZ-QUARANTINE-001).

End-to-end through the REAL spot spine (``run_groww_spot_ingest`` — no
monkeypatched internals) against fake adapters:

* ONE authorisation-rejected symbol in a 3-symbol pull is QUARANTINED (the pull
  completes for the other two) — never a pull-wide halt.
* ≥ 5 DISTINCT authorisation-rejected symbols with ZERO transport successes
  escalate through the armed breaker to the fleet-wide
  :class:`GrowwAuthorisationStormError` HALT; 4 do NOT.
* A transport SUCCESS between authorisation rejections RESETS the storm's
  distinct-symbol set (a mixed pull can never trip).

Fail-before/pass-after: on origin/main ``GrowwAPIAuthorisationException`` mapped
to ``GrowwAuthError`` (terminal → ``is_terminal_auth_failure`` True → the drain
check re-raised it as a pull-wide halt), so
``test_one_authz_symbol_no_longer_stops_a_three_symbol_pull`` FAILED with an
escaped ``GrowwAuthError`` (the whole pull died on the ONE restricted symbol) —
verified by running this file against the pre-fix ``_SDK_NAME_TO_ERROR`` row.
The storm tests fail-before trivially: neither the breaker arm nor
``GrowwAuthorisationStormError`` existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.error_storm_breaker import ErrorStormBreaker
from brutex.adapters.groww import ingest as ingest_mod
from brutex.adapters.groww.errors import (
    GrowwAuthorisationError,
    GrowwAuthorisationStormError,
    from_groww_exception,
)
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.parallel import SerialRunParallel

# 04:05 UTC = 09:35 IST → inside the [09:15, 15:30) session filter.
_IN_SESSION_TS = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)


class GrowwAPIAuthorisationException(Exception):
    """A fake named EXACTLY like the SDK's authorisation class (name-matched)."""


def _bar() -> CandleBar:
    return CandleBar(
        timestamp=_IN_SESSION_TS,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _Sink:
    """Recording sink — appends the per-call bar count, returns a clean result."""

    def __init__(self) -> None:
        self.appends: list[int] = []

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
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _AuthzAdapter:
    """Fake spot adapter — *restricted* symbols raise the CLASSIFIED authz error.

    The raised error is built through the REAL ``from_groww_exception`` over a
    fake SDK exception named exactly ``GrowwAPIAuthorisationException`` — so
    these tests pin the FULL production chain (SDK-name classification →
    terminal error → spine handling), not a hand-built error class.
    """

    def __init__(self, restricted: frozenset[str]) -> None:
        self._restricted = restricted
        self.calls: list[str] = []

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del exchange, interval, start, end
        self.calls.append(symbol)
        if symbol in self._restricted:
            raise from_groww_exception(
                GrowwAPIAuthorisationException("not entitled to this instrument")
            )
        return [_bar()]


# ONE 1-day window per symbol over this range (the sticky-window e2e's envelope).
_START = date(2020, 11, 1)
_END = date(2021, 4, 1)


def _pull(
    adapter: _AuthzAdapter,
    instruments: list[Instrument],
    *,
    sink: _Sink | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
) -> object:
    return run_groww_spot_ingest(
        adapter,  # type: ignore[arg-type]
        sink if sink is not None else _Sink(),  # type: ignore[arg-type]
        instruments=instruments,
        intervals=["1day"],
        start=_START,
        end=_END,
        run_parallel=SerialRunParallel(),
        storm_breaker=storm_breaker,
    )


class TestAuthzQuarantine:
    def test_one_authz_symbol_no_longer_stops_a_three_symbol_pull(self) -> None:
        """THE headline regression: one restricted instrument = one quarantine, pull survives.

        Fail-before: with the pre-fix ``GrowwAPIAuthorisationException`` →
        ``GrowwAuthError`` mapping this call RAISED (the drain check re-raised the
        terminal auth error as a pull-wide halt) and the other two symbols' bars
        never landed.
        """
        sink = _Sink()
        adapter = _AuthzAdapter(restricted=frozenset({"BANKNIFTY"}))
        report = _pull(adapter, [_inst("NIFTY"), _inst("BANKNIFTY"), _inst("SENSEX")], sink=sink)
        assert report.failed_pairs == 1  # type: ignore[attr-defined]
        failure = report.failures[0]  # type: ignore[attr-defined]
        assert failure.symbol == "BANKNIFTY"
        # NOT the auth-halt family: the auto-retry wrapper must not read a token death.
        assert failure.auth_failure is False
        assert report.auth_aborted is False  # type: ignore[attr-defined]
        assert ("NIFTY", "1day") in report.pairs  # type: ignore[attr-defined]
        assert ("SENSEX", "1day") in report.pairs  # type: ignore[attr-defined]
        assert sum(sink.appends) == 2  # both healthy symbols' bars landed

    def test_authz_quarantine_with_armed_breaker_below_threshold(self) -> None:
        """The SAME single-symbol case with the breaker ARMED still quarantines (no halt)."""
        breaker = ErrorStormBreaker(alert_authorisation_storm=lambda _h: None)
        report = _pull(
            _AuthzAdapter(restricted=frozenset({"BANKNIFTY"})),
            [_inst("NIFTY"), _inst("BANKNIFTY"), _inst("SENSEX")],
            storm_breaker=breaker,
        )
        assert report.failed_pairs == 1  # type: ignore[attr-defined]
        assert breaker.tripped is False


class TestAuthzStorm:
    def test_five_distinct_authz_symbols_halt_fleet_wide(self) -> None:
        """5 distinct authz-rejected symbols, zero successes → the storm HALT (default threshold)."""
        fired: list[GrowwAuthorisationStormError] = []
        breaker = ErrorStormBreaker(alert_authorisation_storm=fired.append)
        symbols = [f"AUTHZ{i}" for i in range(5)]
        with pytest.raises(GrowwAuthorisationStormError) as exc_info:
            _pull(
                _AuthzAdapter(restricted=frozenset(symbols)),
                [_inst(s) for s in symbols],
                storm_breaker=breaker,
            )
        assert breaker.trip_rule == "authorisation_storm"
        assert exc_info.value.distinct_symbols == 5
        assert len(fired) == 1  # ONE CRITICAL alert at the trip transition

    def test_four_distinct_authz_symbols_do_not_halt(self) -> None:
        """4 distinct authz symbols stay below the default threshold — quarantines only."""
        breaker = ErrorStormBreaker(alert_authorisation_storm=lambda _h: None)
        symbols = [f"AUTHZ{i}" for i in range(4)]
        report = _pull(
            _AuthzAdapter(restricted=frozenset(symbols)),
            [_inst(s) for s in symbols],
            storm_breaker=breaker,
        )
        assert breaker.tripped is False
        assert report.failed_pairs == 4  # type: ignore[attr-defined]
        assert breaker.authz_distinct_symbols == 4

    def test_interleaved_success_resets_the_storm_set(self) -> None:
        """authz ×4 → a healthy success → authz again: the set restarts, NO halt.

        A serial pull ordered [4 restricted, 1 healthy, 1 restricted]: the healthy
        symbol's clean fetch clears the 4-symbol set, so the trailing restricted
        symbol counts 1 — a mixed pull can never trip the account-suspension arm.
        """
        breaker = ErrorStormBreaker(alert_authorisation_storm=lambda _h: None)
        restricted = [f"AUTHZ{i}" for i in range(4)]
        order = [_inst(s) for s in restricted] + [_inst("HEALTHY"), _inst("AUTHZ4")]
        report = _pull(
            _AuthzAdapter(restricted=frozenset([*restricted, "AUTHZ4"])),
            order,
            storm_breaker=breaker,
        )
        assert breaker.tripped is False
        assert report.failed_pairs == 5  # type: ignore[attr-defined]
        assert breaker.authz_distinct_symbols == 1  # only the post-success rejection

    def test_authz_error_still_terminal_for_the_ingest_module(self) -> None:
        """The spine handles the split class exactly like a terminal GrowwError.

        (Also anchors this file to :mod:`brutex.adapters.groww.ingest` for the
        per-module coverage gate — the authorisation observe call lives there.)
        """
        assert issubclass(GrowwAuthorisationError, ingest_mod.GrowwError)
        assert not ingest_mod.is_retryable(GrowwAuthorisationError("x"))
