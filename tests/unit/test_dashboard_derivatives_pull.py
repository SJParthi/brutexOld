"""Tests for ``brutex.dashboard.derivatives_pull`` (DEC-PULL-DERIVATIVES-001).

The bulk DERIVATIVES pull controller: interval/contract-type/range resolution, the
honest dry-run (empty chain — never a fabricated strike), and the orchestration
(``pull_all_derivatives`` loops underlyings × {option, future} calling the verified
ingest spine, accumulates the report, stops early on a dead auth token, opens/closes
the ledger). The per-contract ingest itself is covered by
``test_groww_option_ingest.py``; here we drive it end-to-end through a fake
``GrowwSDK`` (canned expiries / contracts / candles) + a recording sink, so the whole
controller is 100% offline. 100% line+branch on ``brutex.dashboard.derivatives_pull``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import GROWW_FUTURE_COVERAGE_MARKET
from brutex.dashboard import derivatives_pull as dp
from brutex.dashboard.derivatives_pull import (
    GROWW_DERIVATIVE_HISTORY_FLOOR,
    GROWW_DERIVATIVE_INTERVALS_ORDER,
    DerivativesPullReport,
    pull_all_derivatives,
    resolve_contract_types,
    resolve_derivative_range,
    resolve_pull_intervals,
)
from brutex.dashboard.pull_controller import SessionRateController
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NOW = datetime(2026, 6, 13, tzinfo=UTC)


# ── Groww-shaped fakes (exception class NAMES the error-mapper recognises) ──


class GrowwAPIBadRequestException(Exception):
    """A terminal (non-auth) Groww error — quarantines the pair."""


class GrowwAPIAuthenticationException(Exception):
    """A terminal auth error — sets ``auth_aborted``."""


def _candles() -> Mapping[str, object]:
    """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
    return {"candles": [["2024-01-25T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake: canned expiries / contracts + a candle payload."""

    def __init__(
        self,
        *,
        expiries: Callable[..., Mapping[str, object]] | None = None,
        contracts: Callable[..., Mapping[str, object]] | None = None,
        raise_for: Mapping[str, Exception] | None = None,
    ) -> None:
        self._expiries = expiries or (lambda **_kw: {"expiries": []})
        self._contracts = contracts or (lambda **_kw: {"contracts": []})
        self._raise_for = dict(raise_for or {})

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        return self._expiries(**kw)

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        return self._contracts(**kw)

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        exc = self._raise_for.get(str(kw["groww_symbol"]))
        if exc is not None:
            raise exc
        return _candles()


class _RecordingSink:
    """A minimal recording :class:`BarSink`."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, int]] = []

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[object]
    ) -> SinkAppendResult:
        del exchange, segment
        self.appends.append((symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _client(sdk: _FakeSDK) -> GrowwClient:
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _instrument(symbol: str = "NIFTY", *, exch: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exch_id=exch,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _option_chain(**_kw: object) -> Mapping[str, object]:
    """Canned chain: one CE strike + the FUT leg (the FUT is filtered for options)."""
    return {"contracts": ["NSE-NIFTY-25Jan24-100-CE", "NSE-NIFTY-25Jan24-FUT"]}


def _expiries_2024(**kw: object) -> Mapping[str, object]:
    """One expiry in 2024 (empty for any other year)."""
    return {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {"expiries": []}


def _factory_for(sdk: _FakeSDK, sink: BarSink) -> dp.DerivativeClientFactory:
    def _factory(_lake_root: Path) -> tuple[GrowwClient, BarSink]:
        return _client(sdk), sink

    return _factory


# ── interval / contract-type resolution ─────────────────────────────────────


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("1day", ("1day",)), ("1minute", ("1minute",)), ("both", GROWW_DERIVATIVE_INTERVALS_ORDER)],
)
def test_resolve_pull_intervals(choice: str, expected: tuple[str, ...]) -> None:
    assert resolve_pull_intervals(choice) == expected


def test_resolve_pull_intervals_both_is_one_day_first() -> None:
    # The whole point: 1day BEFORE 1minute.
    assert resolve_pull_intervals("both") == ("1day", "1minute")


def test_resolve_pull_intervals_bad() -> None:
    with pytest.raises(ValueError, match="interval must be one of"):
        resolve_pull_intervals("5minute")


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("option", ("option",)), ("future", ("future",)), ("both", ("option", "future"))],
)
def test_resolve_contract_types(choice: str, expected: tuple[str, ...]) -> None:
    assert resolve_contract_types(choice) == expected


def test_resolve_contract_types_bad() -> None:
    with pytest.raises(ValueError, match="contract_type must be one of"):
        resolve_contract_types("swap")


# ── range resolution ─────────────────────────────────────────────────────────


def test_range_default_is_floor_to_prev_day() -> None:
    start, end = resolve_derivative_range(from_date=None, to_date=None, now=_NOW)
    assert start == GROWW_DERIVATIVE_HISTORY_FLOOR  # 2020-01-01
    assert end == date(2026, 6, 13)  # exclusive end = (prev day 06-12) + 1


def test_range_explicit_inclusive_end() -> None:
    start, end = resolve_derivative_range(from_date="2021-03-01", to_date="2021-03-31", now=_NOW)
    assert start == date(2021, 3, 1)
    assert end == date(2021, 4, 1)  # 03-31 inclusive → exclusive 04-01


def test_range_clamps_below_floor_and_above_today() -> None:
    start, end = resolve_derivative_range(from_date="2018-01-01", to_date="2099-01-01", now=_NOW)
    assert start == GROWW_DERIVATIVE_HISTORY_FLOOR  # clamped up to 2020
    assert end == date(2026, 6, 14)  # today (06-13) inclusive → exclusive 06-14


def test_range_empty_raises() -> None:
    with pytest.raises(ValueError, match="nothing to pull"):
        resolve_derivative_range(from_date="2024-05-10", to_date="2024-05-01", now=_NOW)


def test_range_bad_date_raises() -> None:
    with pytest.raises(ValueError, match="invalid from_date"):
        resolve_derivative_range(from_date="2024-13-40", to_date=None, now=_NOW)


# ── report serialisation ─────────────────────────────────────────────────────


def test_report_to_dict_round_trips() -> None:
    report = DerivativesPullReport(
        dry_run=False,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 1),
        intervals=("1day",),
        contract_types=("option",),
        underlyings=("NIFTY",),
        bars_appended=3,
        windows=2,
        failed_pairs=1,
        auth_aborted=False,
        note="ok",
    )
    payload = report.to_dict()
    assert payload["start_date"] == "2024-01-01"
    assert payload["intervals"] == ["1day"]
    assert payload["underlyings"] == ["NIFTY"]
    assert payload["bars_appended"] == 3
    assert payload["note"] == "ok"


# ── empty-chain dry-run SDK + factory ────────────────────────────────────────


def test_empty_chain_sdk_is_empty() -> None:
    sdk = dp._EmptyChainSDK()
    assert sdk.get_expiries() == {"expiries": []}
    assert sdk.get_contracts() == {"contracts": []}
    assert sdk.get_historical_candles() == {"candles": []}


def test_build_dry_run_client_pairs_empty_chain_with_null_sink() -> None:
    client, sink = dp._build_dry_run_derivative_client(Path("/tmp/lake"))
    assert isinstance(client, GrowwClient)
    # NullBarSink swallows appends without raising.
    result = sink.append(exchange="NSE", segment="FNO", symbol="X", interval="1day", bars=())
    assert result.bars_appended == 0


# ── rate-hook resolution ─────────────────────────────────────────────────────


def test_rate_hooks_none_when_dry_run() -> None:
    rate = SessionRateController()  # default factory; .get() is NOT called when dry-run
    assert dp._rate_hooks(rate, dry_run=True) == (None, None, None, None)


def test_rate_hooks_none_when_no_session() -> None:
    assert dp._rate_hooks(None, dry_run=False) == (None, None, None, None)


def test_rate_hooks_uses_controller_when_live() -> None:
    rate = SessionRateController()  # default factory builds a real (pure) RateController
    controller = rate.get()
    pace, on_rl, on_to, on_ok = dp._rate_hooks(rate, dry_run=False)
    assert pace == controller.pace
    assert on_rl == controller.on_rate_limit
    assert on_to == controller.on_timeout
    assert on_ok == controller.on_success


# ── progress callback (all four optional-side combinations) ──────────────────


def test_make_progress_all_combinations() -> None:
    calls: list[str] = []
    flushes: list[int] = []
    # both present
    both = dp._make_progress(lambda label, _b, _f: calls.append(label), lambda: flushes.append(1))
    both("a", 5, False)
    # on_progress None, flush present
    flush_only = dp._make_progress(None, lambda: flushes.append(2))
    flush_only("b", 0, True)
    # on_progress present, flush None
    prog_only = dp._make_progress(lambda label, _b, _f: calls.append(label), None)
    prog_only("c", 1, False)
    # both None — a no-op that must not raise
    dp._make_progress(None, None)("d", 0, False)
    assert calls == ["a", "c"]
    assert flushes == [1, 2]


# ── pull_all_derivatives — orchestration ─────────────────────────────────────


def test_pull_dry_run_empty_chain_is_honest_zero() -> None:
    # Real dry-run path: the empty-chain client enumerates nothing → 0 windows,
    # an honest "set credentials" note — never an invented strike.
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=Path("/tmp/lake-x"),
        dry_run=True,
        contract_types=("option",),
        intervals=("1day",),
        now=_NOW,
    )
    assert report.dry_run is True
    assert report.windows == 0
    assert report.bars_appended == 0
    assert "set Groww credentials" in report.note


def test_pull_options_happy_path_appends(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries=_expiries_2024, contracts=_option_chain)
    sink = _RecordingSink()
    progressed: list[tuple[str, int, bool]] = []
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=tmp_path,
        dry_run=False,
        contract_types=("option",),
        intervals=("1day",),
        from_date="2024-01-01",
        to_date="2024-01-25",
        now=_NOW,
        client_factory=_factory_for(sdk, sink),
        on_progress=lambda label, bars, failed: progressed.append((label, bars, failed)),
    )
    # Only the CE leg is ingested (FUT filtered out by option_contracts).
    assert report.bars_appended == 1
    assert sink.appends == [("NSE-NIFTY-25Jan24-100-CE", "1day", 1)]
    assert report.auth_aborted is False
    assert "rerun-safe" in report.note
    assert progressed  # the progress hook fired


def test_pull_both_contract_types_pulls_option_and_future(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries=_expiries_2024, contracts=_option_chain)
    sink = _RecordingSink()
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=tmp_path,
        dry_run=False,
        contract_types=("option", "future"),
        intervals=("1day",),
        from_date="2024-01-01",
        to_date="2024-01-25",
        now=_NOW,
        client_factory=_factory_for(sdk, sink),
    )
    pulled = {symbol for symbol, _interval, _n in sink.appends}
    assert pulled == {"NSE-NIFTY-25Jan24-100-CE", "NSE-NIFTY-25Jan24-FUT"}
    assert report.bars_appended == 2


def test_pull_auth_failure_aborts_early(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries=_expiries_2024,
        contracts=_option_chain,
        raise_for={"NSE-NIFTY-25Jan24-100-CE": GrowwAPIAuthenticationException("dead token")},
    )
    sink = _RecordingSink()
    report = pull_all_derivatives(
        [_instrument("NIFTY"), _instrument("BANKNIFTY")],
        lake_root=tmp_path,
        dry_run=False,
        contract_types=("option", "future"),
        intervals=("1day",),
        from_date="2024-01-01",
        to_date="2024-01-25",
        now=_NOW,
        client_factory=_factory_for(sdk, sink),
    )
    assert report.auth_aborted is True
    assert "auth token expired" in report.note


def test_pull_quarantines_failed_pair(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries=_expiries_2024,
        contracts=_option_chain,
        raise_for={"NSE-NIFTY-25Jan24-100-CE": GrowwAPIBadRequestException("nope")},
    )
    sink = _RecordingSink()
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=tmp_path,
        dry_run=False,
        contract_types=("option",),
        intervals=("1day",),
        from_date="2024-01-01",
        to_date="2024-01-25",
        now=_NOW,
        client_factory=_factory_for(sdk, sink),
    )
    assert report.auth_aborted is False
    assert report.failed_pairs == 1
    assert "quarantined" in report.note


def test_pull_futures_uses_future_namespace(tmp_path: Path) -> None:
    # Sanity that the future kind routes through ingest_underlying_futures
    # (the FUT leg is the one ingested).
    sdk = _FakeSDK(expiries=_expiries_2024, contracts=_option_chain)
    sink = _RecordingSink()
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=tmp_path,
        dry_run=False,
        contract_types=("future",),
        intervals=("1day",),
        from_date="2024-01-01",
        to_date="2024-01-25",
        now=_NOW,
        client_factory=_factory_for(sdk, sink),
    )
    assert sink.appends == [("NSE-NIFTY-25Jan24-FUT", "1day", 1)]
    assert report.contract_types == ("future",)
    assert GROWW_FUTURE_COVERAGE_MARKET == "groww-future"


def test_pull_default_now_branch(tmp_path: Path) -> None:
    # now=None → uses datetime.now(UTC); empty chain so no network, just covers
    # the default-clock branch.
    report = pull_all_derivatives(
        [_instrument("NIFTY")],
        lake_root=tmp_path,
        dry_run=True,
        contract_types=("option",),
        intervals=("1day",),
    )
    assert report.dry_run is True
