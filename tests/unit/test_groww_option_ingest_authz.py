"""F&O-spine authorisation split — per-contract quarantine vs fleet storm
(DEC-GROWW-AUTHZ-QUARANTINE-001).

The option-spine twin of ``test_groww_ingest_authz_quarantine.py``: drives the
REAL ``run_groww_option_ingest`` against a fake client whose restricted
underlyings raise the terminal :class:`GrowwAuthorisationError` — one restricted
underlying's chain is QUARANTINED per contract (never a pull-wide halt), while
≥ 5 distinct authz-rejected UNDERLYINGS with zero successes escalate to the
fleet-wide :class:`GrowwAuthorisationStormError` halt (the storm's
distinct-symbol unit is the UNDERLYING, mirroring the vendor-down arm).

Fail-before/pass-after: with the pre-fix mapping the classified error was a
terminal ``GrowwAuthError``, so the single-underlying case RAISED out of the
drain check (a pull-wide halt) instead of quarantining — verified against the
original ``_SDK_NAME_TO_ERROR`` row. The storm case fail-before trivially (the
arm did not exist).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest
from brutex.adapters.error_storm_breaker import ErrorStormBreaker
from brutex.adapters.groww.errors import GrowwAuthorisationError, GrowwAuthorisationStormError
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult

_OPTION_PAYLOAD: Mapping[str, object] = {
    "candles": [["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]
}


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


class _AuthzClient:
    """Fake Groww client — contracts of a *restricted* underlying raise the authz error."""

    def __init__(self, restricted: frozenset[str]) -> None:
        self._restricted = restricted
        self.calls: list[str] = []

    def historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        self.calls.append(symbol)
        if any(f"-{u}-" in symbol for u in self._restricted):
            raise GrowwAuthorisationError("account not entitled to this contract")
        return _OPTION_PAYLOAD


def _ce(strike: int, *, underlying: str = "NIFTY") -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol=underlying,
        expiry=date(2025, 1, 2),
        symbol=f"NSE-{underlying}-02Jan25-{strike}-CE",
        contract_type="CE",
        strike=float(strike),
    )


def _now() -> datetime:
    """Frozen IST clock — every test range is safely in the past."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class TestOptionAuthzQuarantine:
    def test_one_restricted_underlying_is_quarantined_not_a_halt(self) -> None:
        """A restricted underlying's contracts quarantine; the healthy chain still lands."""
        sink = _Sink()
        contracts = [_ce(21000, underlying="BLOCKED"), _ce(21100, underlying="NIFTY")]
        breaker = ErrorStormBreaker(alert_authorisation_storm=lambda _h: None)
        report = run_groww_option_ingest(
            _AuthzClient(restricted=frozenset({"BLOCKED"})),  # type: ignore[arg-type]
            sink,  # type: ignore[arg-type]
            contracts=contracts,
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            max_attempts=0,
            now_ist=_now,
            storm_breaker=breaker,
        )
        assert len(report.failures) == 1
        assert report.failures[0].symbol == "NSE-BLOCKED-02Jan25-21000-CE"
        assert report.failures[0].auth_failure is False  # never the auth-halt family
        assert ("NSE-NIFTY-02Jan25-21100-CE", "1day") in report.pairs
        assert breaker.tripped is False
        assert sum(sink.appends) == 1  # the healthy contract's bars landed

    def test_five_distinct_restricted_underlyings_halt_fleet_wide(self) -> None:
        """5 distinct authz-rejected UNDERLYINGS, zero successes → the storm halt."""
        fired: list[GrowwAuthorisationStormError] = []
        breaker = ErrorStormBreaker(alert_authorisation_storm=fired.append)
        underlyings = [f"BLK{i}" for i in range(5)]
        contracts = [_ce(21000 + i, underlying=u) for i, u in enumerate(underlyings)]
        with pytest.raises(GrowwAuthorisationStormError) as exc_info:
            run_groww_option_ingest(
                _AuthzClient(restricted=frozenset(underlyings)),  # type: ignore[arg-type]
                _Sink(),  # type: ignore[arg-type]
                contracts=contracts,
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 10),
                max_attempts=0,
                now_ist=_now,
                storm_breaker=breaker,
            )
        assert breaker.trip_rule == "authorisation_storm"
        assert exc_info.value.distinct_symbols == 5
        assert len(fired) == 1

    def test_one_underlyings_whole_chain_counts_as_one_symbol(self) -> None:
        """30 restricted contracts of ONE underlying = ONE distinct symbol — never a storm."""
        breaker = ErrorStormBreaker(alert_authorisation_storm=lambda _h: None)
        contracts = [_ce(21000 + 100 * i, underlying="BLOCKED") for i in range(30)]
        report = run_groww_option_ingest(
            _AuthzClient(restricted=frozenset({"BLOCKED"})),  # type: ignore[arg-type]
            _Sink(),  # type: ignore[arg-type]
            contracts=contracts,
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            max_attempts=0,
            now_ist=_now,
            storm_breaker=breaker,
        )
        assert breaker.tripped is False
        assert breaker.authz_distinct_symbols == 1  # the UNDERLYING, not 30 contracts
        assert len(report.failures) == 30  # every contract quarantined individually
