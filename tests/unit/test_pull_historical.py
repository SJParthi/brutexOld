# pyright: reportPrivateUsage=none
"""Tests for ``brutex.dashboard.historical_pull`` (DEC-DERIV-PULL-HISTORICAL-001).

PR-5 of the metadata/historical split (the capstone): the SEPARATE "Pull historical"
engine — download OHLC candles for the operator-SELECTED registry contracts WITHOUT
re-enumerating the chain (the metadata is already cached by PR-3's metadata button).

Every path runs FULLY OFFLINE: a fake ``GrowwSDK`` (a canned candle payload, NO
expiry/contract enumeration even reachable for the selection — the contracts come
straight from the registry), a recording :class:`BarSink`, a REAL temp
:class:`RegistryStore` seeded with contracts, and an injected ``now``. The make-or-break
invariants are asserted directly:

* the resolved contracts list fed to ``run_groww_option_ingest`` is EXACTLY the registry
  selection (NOT a re-enumeration of the chain);
* candles land on PATH A (the candle ``BarSink``) — the recording sink captures the
  ``segment="FNO"`` appends; the module imports neither the OptionBar lake nor a
  fabricated ``iv``/``spot``;
* ``set_candle_status(..., "complete")`` is called per LANDED contract;
* the selection grammar (single / multi / chain / underlying / all / empty→honest), the
  ``until_clean`` default, and the dry-run empty no-op.

100% line+branch on ``brutex.dashboard.historical_pull``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import OPTION_SEGMENT
from brutex.adapters.protocol import DerivativeContract
from brutex.dashboard import historical_pull as hp
from brutex.dashboard.historical_pull import (
    GROWW_HISTORICAL_FLOOR,
    HistoricalPullReport,
    pull_contract_candles,
)
from brutex.data.registry_store import (
    CANDLE_STATUS_COMPLETE,
    CANDLE_STATUS_NONE,
    ContractRow,
    RegistryStore,
)
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.universe_phases import ItemResult

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_RUN_ID = "hist-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)

#: A NARROW pull window — short enough that the 1-day tier (180-day chunks) tiles each
#: contract's range into exactly ONE window, so the per-contract bar/window counts are
#: deterministic (one canned bar per contract per interval). The full default range tiles
#: into ~14 windows, which is correct ingest behaviour but not a clean unit count.
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)


# ── offline fakes ────────────────────────────────────────────────────────────


def _candle_payload() -> dict[str, object]:
    """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
    return {"candles": [["2024-01-25T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake: a canned candle payload + a raise-for map.

    Carries ``get_expiries`` / ``get_contracts`` (the Protocol surface) but they MUST
    NOT be called — the contracts come from the registry, not a re-enumeration; the test
    asserts ``calls`` never records them. ``raise_for`` maps a contract ``groww_symbol``
    to an exception so a terminal/transient fetch can be exercised offline.
    """

    def __init__(self, *, raise_for: dict[str, Exception] | None = None) -> None:
        self._raise_for = dict(raise_for or {})
        self.calls: list[str] = []
        self.candle_symbols: list[str] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Record + return empty — never expected to be called (no re-enumeration)."""
        self.calls.append("get_expiries")
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Record + return empty — never expected to be called (no re-enumeration)."""
        self.calls.append("get_contracts")
        return {"contracts": []}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Return the canned candle bar, or raise the symbol's mapped error."""
        symbol = str(kw["groww_symbol"])
        self.candle_symbols.append(symbol)
        exc = self._raise_for.get(symbol)
        if exc is not None:
            raise exc
        return _candle_payload()


class _RecordingSink:
    """A minimal recording :class:`BarSink` capturing every append's segment + symbol."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str, int]] = []

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[object]
    ) -> SinkAppendResult:
        """Record ``(segment, symbol, interval, n_bars)`` and report all bars appended."""
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _client(sdk: _FakeSDK) -> GrowwClient:
    """Wrap *sdk* in a no-sleep, single-attempt :class:`GrowwClient` (offline)."""
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _factory(sdk: _FakeSDK, sink: BarSink) -> tuple[GrowwClient, BarSink]:
    """Return a fixed ``(client, sink)`` pair the ``client_factory`` hands back."""
    return _client(sdk), sink


def _factory_with(sdk: object, sink: BarSink) -> tuple[GrowwClient, BarSink]:
    """Wrap ANY GrowwSDK-shaped fake (e.g. ``_PhasedSDK``) in a no-sleep client + *sink*."""
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1), sink  # type: ignore[arg-type]


# ── Groww-shaped exception NAMES the error-mapper recognises ──────────────────


class GrowwAPIBadRequestException(Exception):
    """A terminal (non-auth) Groww error — quarantines the pair (leaves status none)."""


class GrowwAPIAuthenticationException(Exception):
    """A terminal auth error — sets ``auth_aborted`` and halts."""


# ── registry seeding ─────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path: Path) -> RegistryStore:
    """A real temp :class:`RegistryStore` seeded with a NIFTY chain + a future."""
    store = RegistryStore.open(tmp_path / "registry.duckdb")
    expiry = date(2024, 1, 25)
    rows = [
        ("NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0, expiry),
        ("NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0, expiry),
        ("NSE-NIFTY-25Jan24-FUT", "FUT", None, expiry),
    ]
    for symbol, side, strike, exp in rows:
        store.upsert_contract(
            "NSE",
            "NIFTY",
            exp,
            symbol,
            side=side,
            strike=strike,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
    # A second underlying + expiry, to prove the underlying / expiry filters.
    store.upsert_contract(
        "NSE",
        "RELIANCE",
        date(2024, 2, 29),
        "NSE-RELIANCE-29Feb24-2800-CE",
        side="CE",
        strike=2800.0,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    return store


@pytest.fixture
def tmp_lake(tmp_path: Path) -> Path:
    """A real temp lake root for the LIVE-shaped (``dry_run=False``) ledger paths."""
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _statuses(registry: RegistryStore) -> dict[str, str]:
    """Map each persisted contract symbol → its current ``candle_status``."""
    return {row.contract_symbol: row.candle_status for row in registry.list_contracts()}


# ── happy path: the selection IS what is pulled (no re-enumeration) ───────────


def test_pull_marks_landed_contracts_complete_path_a(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # We pass a REAL sdk via the injected factory, so this exercises the LIVE-shaped
    # ingest fully offline (the dry_run flag only steers the DEFAULT factory).
    assert isinstance(report, HistoricalPullReport)
    # 3 NIFTY contracts × 1 interval, all landed → all marked complete.
    assert report.contracts_selected == 3
    assert report.contracts_completed == 3
    # The candle fetch was hit EXACTLY for the 3 selected contracts — never the chain
    # enumeration (no get_expiries / get_contracts).
    assert sdk.calls == []
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    # PATH A: every append is on the candle lake under segment FNO (NOT options/).
    assert {seg for seg, *_ in sink.appends} == {OPTION_SEGMENT}
    # candle_status flipped to complete for exactly the NIFTY legs (RELIANCE untouched).
    statuses = _statuses(registry)
    assert statuses["NSE-NIFTY-25Jan24-21000-CE"] == CANDLE_STATUS_COMPLETE
    assert statuses["NSE-NIFTY-25Jan24-FUT"] == CANDLE_STATUS_COMPLETE
    assert statuses["NSE-RELIANCE-29Feb24-2800-CE"] == CANDLE_STATUS_NONE


def test_option_and_future_split_distinct_markets(registry: RegistryStore) -> None:
    """CE/PE ride groww-option; FUT rides groww-future — distinct ledger namespaces."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.bars_appended == 3  # one canned bar per contract (one window each)
    assert report.windows == 3


# ── empty selection: honest 0-bar no-op ──────────────────────────────────────


def test_empty_selection_is_honest_no_op(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    report = pull_contract_candles(
        [],
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.contracts_selected == 0
    assert report.contracts_completed == 0
    assert report.bars_appended == 0
    assert sink.appends == []
    assert sdk.candle_symbols == []
    assert "no contracts selected" in report.note


# ── dry-run via the default factory: empty candles, but still settles complete ──


def test_dry_run_default_factory_empty_candles(registry: RegistryStore) -> None:
    """No injected factory + dry_run=True → the empty-candle client (0 bars), honest note."""
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        now=_NOW,
    )
    assert report.dry_run is True
    assert report.bars_appended == 0
    # An EMPTY window is still a SETTLED clean window, so the selected contracts ARE
    # marked complete (coverage of the selected range — honestly empty offline).
    assert report.contracts_completed == 3
    assert "dry-run" in report.note


# ── terminal quarantine: a bad-request leaves the contract status none ────────


def test_terminal_quarantine_leaves_status_none(registry: RegistryStore, tmp_lake: Path) -> None:
    sdk = _FakeSDK(raise_for={"NSE-NIFTY-25Jan24-21000-CE": GrowwAPIBadRequestException("bad")})
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    # dry_run=False (the LIVE-shaped path) so a real failing SDK exercises the quarantine
    # note branch; the injected factory bypasses live client/network construction.
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.failed_pairs == 1
    statuses = _statuses(registry)
    # The quarantined CE stays none; the clean PE + FUT are marked complete.
    assert statuses["NSE-NIFTY-25Jan24-21000-CE"] == CANDLE_STATUS_NONE
    assert statuses["NSE-NIFTY-25Jan24-21000-PE"] == CANDLE_STATUS_COMPLETE
    assert report.contracts_completed == 2
    assert "quarantined" in report.note


def test_auth_abort_halts_and_reports(registry: RegistryStore, tmp_lake: Path) -> None:
    sdk = _FakeSDK(
        raise_for={"NSE-NIFTY-25Jan24-21000-CE": GrowwAPIAuthenticationException("dead")}
    )
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.auth_aborted is True
    assert "auth token expired" in report.note


# ── until-clean: bounded re-pull rounds (default-off vs on) ───────────────────


def test_until_clean_default_is_single_pass(registry: RegistryStore) -> None:
    """``until_clean`` defaults to False — exactly one pass per market bucket."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # One candle call per contract (single pass, one window each — no re-pull rounds).
    assert len(sdk.candle_symbols) == 3
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}


def test_until_clean_runs_to_clean(registry: RegistryStore) -> None:
    """``until_clean=True`` drains a clean selection in one productive round."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        until_clean=True,
        now=_NOW,
    )
    assert report.still_failed_windows == 0
    assert report.contracts_completed == 3


def test_until_clean_zero_rounds_is_safe_no_op(registry: RegistryStore) -> None:
    """``until_clean_rounds=0`` runs the loop zero times → an honest all-zero report."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        until_clean=True,
        until_clean_rounds=0,
        now=_NOW,
    )
    assert report.bars_appended == 0
    assert report.windows == 0
    assert report.contracts_completed == 0
    assert sdk.candle_symbols == []


# ── progress hook + range resolution ─────────────────────────────────────────


def test_progress_hook_is_forwarded(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    events: list[tuple[str, int, bool]] = []
    selected = registry.list_contracts(underlying="NIFTY")
    pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        now=_NOW,
    )
    assert len(events) == 3
    assert all(not failed for _label, _bars, failed in events)


def test_explicit_range_is_honoured(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=date(2023, 1, 1),
        to_date=date(2023, 12, 31),
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    assert report.contracts_completed == 3


def test_inverted_range_raises(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    with pytest.raises(ValueError, match="empty"):
        pull_contract_candles(
            selected,
            lake_root=Path("/unused"),
            dry_run=True,
            intervals=("1day",),
            registry=registry,
            from_date=date(2024, 6, 1),
            to_date=date(2024, 1, 1),
            client_factory=lambda _root: _factory(sdk, sink),
            now=_NOW,
        )


def test_from_date_below_floor_is_clamped(registry: RegistryStore) -> None:
    """A ``from_date`` before the 2020 floor is clamped (never widens scope)."""
    start, end = hp._resolve_range(from_date=date(2018, 1, 1), to_date=date(2021, 1, 1), now=_NOW)
    assert start == GROWW_HISTORICAL_FLOOR
    assert end == date(2021, 1, 2)


def test_default_range_is_floor_to_prev_day() -> None:
    """No bounds → ``[2020-01-01, previous day)`` (never the in-progress session)."""
    start, end = hp._resolve_range(from_date=None, to_date=None, now=_NOW)
    assert start == GROWW_HISTORICAL_FLOOR
    assert end == date(2026, 6, 14)  # prev day (2026-06-13) + 1 → exclusive


# ── to_dict surface ──────────────────────────────────────────────────────────


def test_report_to_dict_is_json_safe(registry: RegistryStore) -> None:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    payload = report.to_dict()
    assert payload["contracts_selected"] == 3
    assert payload["contracts_completed"] == 3
    assert payload["intervals"] == ["1day"]
    assert isinstance(payload["note"], str)


# ── DerivativeContract reconstruction fidelity ───────────────────────────────


def test_rebuild_is_bit_faithful_to_registry(registry: RegistryStore) -> None:
    """The rebuilt DerivativeContract carries the registry's authoritative fields."""
    rows = registry.list_contracts(underlying="NIFTY")
    rebuilt = hp._rebuild_contracts(rows)
    by_symbol = {row.contract_symbol: contract for row, contract in rebuilt}
    ce = by_symbol["NSE-NIFTY-25Jan24-21000-CE"]
    assert isinstance(ce, DerivativeContract)
    assert ce.contract_type == "CE"
    assert ce.strike == 21000.0
    assert ce.underlying_symbol == "NIFTY"
    assert ce.expiry == date(2024, 1, 25)
    fut = by_symbol["NSE-NIFTY-25Jan24-FUT"]
    assert fut.contract_type == "FUT"
    assert fut.strike is None


def test_strike_is_not_money_full_precision(registry: RegistryStore) -> None:
    """Strike stays a plain float (never money-rounded) — Decimal-equal to the registry."""
    rows = registry.list_contracts(underlying="RELIANCE")
    rebuilt = hp._rebuild_contracts(rows)
    _row, contract = rebuilt[0]
    assert Decimal(str(contract.strike)) == Decimal("2800.0")


# ── direct helper coverage (the branches not reached end-to-end) ──────────────


def test_empty_candle_sdk_is_inert() -> None:
    """The dry-run SDK returns empty for every surface (no fabricated chain/candle)."""
    sdk = hp._EmptyCandleSDK()
    assert sdk.get_expiries() == {"expiries": []}
    assert sdk.get_contracts() == {"contracts": []}
    assert sdk.get_historical_candles() == {"candles": []}


def test_dry_run_factory_builds_null_sink() -> None:
    """``_build_dry_run_client`` returns a real GrowwClient + a NullBarSink (0 bars)."""
    from brutex.data.sink import NullBarSink

    client, sink = hp._build_dry_run_client(Path("/unused"))
    assert isinstance(client, GrowwClient)
    assert isinstance(sink, NullBarSink)


def test_resolve_client_factory_dry_run_vs_live() -> None:
    """dry_run → the empty-candle factory; live → the (pragma'd) live factory closure."""
    dry = hp._resolve_client_factory(dry_run=True, pace=None, on_rate_limit=None, on_success=None)
    assert dry is hp._build_dry_run_client
    live = hp._resolve_client_factory(dry_run=False, pace=None, on_rate_limit=None, on_success=None)
    # The live branch returns a distinct closure (not the dry-run factory).
    assert live is not hp._build_dry_run_client


def test_rate_hooks_offline_are_all_none() -> None:
    """Dry-run / no controller → all-None hooks (no pacing offline)."""
    assert hp._rate_hooks(None, dry_run=True) == (None, None, None, None)
    assert hp._rate_hooks(None, dry_run=False) == (None, None, None, None)


def test_rate_hooks_live_returns_controller_hooks() -> None:
    """Live + a SessionRateController → its single controller's pace/limit/timeout/success."""
    from brutex.dashboard.pull_controller import SessionRateController

    session = SessionRateController()
    pace, on_rl, on_to, on_ok = hp._rate_hooks(session, dry_run=False)
    controller = session.get()
    assert pace == controller.pace
    assert on_rl == controller.on_rate_limit
    assert on_to == controller.on_timeout
    assert on_ok == controller.on_success


class _CountingRunner:
    """A ``RunParallel`` that COUNTS its uses, delegating to a deterministic serial runner.

    Proves an explicit ``run_parallel`` is forwarded all the way to
    ``run_groww_option_ingest`` (used once per market bucket) without changing behaviour.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._inner = SerialRunParallel()

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterable[ItemResult[ItemT, ResultT]]:
        self.calls += 1
        yield from self._inner(fn, items)


def test_option_run_parallel_offline_is_none() -> None:
    """Dry-run / no controller → None (the option ingest keeps its serial default)."""
    from brutex.dashboard.pull_controller import SessionRateController

    assert hp._option_run_parallel(dry_run=True, session_rate=None) is None
    # A dry-run WITH a controller is still None (no network → deterministic serial).
    assert hp._option_run_parallel(dry_run=True, session_rate=SessionRateController()) is None
    # Live but NO session controller → None too.
    assert hp._option_run_parallel(dry_run=False, session_rate=None) is None


def test_option_run_parallel_live_is_pool() -> None:
    """Live (a session controller present, not a dry-run) → a bounded PoolRunParallel."""
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.orchestrator.parallel import PoolRunParallel

    runner = hp._option_run_parallel(dry_run=False, session_rate=SessionRateController())
    assert isinstance(runner, PoolRunParallel)


def test_pull_contract_candles_forwards_explicit_run_parallel(registry: RegistryStore) -> None:
    """An explicit *run_parallel* WINS and is forwarded to the option ingest per bucket."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    runner = _CountingRunner()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
        run_parallel=runner,
    )
    # NIFTY spans 2 market buckets (option CE/PE + future FUT); the INJECTED runner drained
    # each bucket's phase-1 pass — never the auto-derived default.
    assert runner.calls >= 1
    assert report.contracts_completed == 3


def test_pull_contract_candles_live_derives_pool(registry: RegistryStore, tmp_lake: Path) -> None:
    """Live (session controller, not dry-run, no explicit runner) → derives + uses the pool."""
    from brutex.dashboard.pull_controller import SessionRateController

    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
        session_rate=SessionRateController(),
    )
    # The run_parallel-is-None live branch built a PoolRunParallel; the pull still completes
    # correctly through the real thread pool (offline fakes), all 3 NIFTY legs landed.
    assert report.contracts_completed == 3


def test_contract_market_split() -> None:
    """A FUT row maps to the future market; CE/PE map to the option market."""
    from brutex.adapters.groww.constants import (
        GROWW_FUTURE_COVERAGE_MARKET,
        GROWW_OPTION_COVERAGE_MARKET,
    )

    fetched = datetime(2026, 1, 1, tzinfo=UTC)
    fut = ContractRow(
        "NSE",
        "NIFTY",
        date(2024, 1, 25),
        "NSE-NIFTY-25Jan24-FUT",
        None,
        "FUT",
        "none",
        "r",
        fetched,
    )
    ce = ContractRow("NSE", "NIFTY", date(2024, 1, 25), "x-CE", 1.0, "CE", "none", "r", fetched)
    assert hp._contract_market(fut) == GROWW_FUTURE_COVERAGE_MARKET
    assert hp._contract_market(ce) == GROWW_OPTION_COVERAGE_MARKET


def test_build_note_branches() -> None:
    """Every ``_build_note`` arm renders an honest, distinct one-liner."""
    empty = hp._build_note(
        dry_run=False,
        contracts_selected=0,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "no contracts selected" in empty

    dry = hp._build_note(
        dry_run=True,
        contracts_selected=3,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "dry-run" in dry

    auth = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=True,
    )
    assert "auth token expired" in auth

    quarantine = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=2,
        windows=2,
        failed_pairs=1,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "quarantined" in quarantine

    transient = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=2,
        windows=2,
        failed_pairs=0,
        still_failed_windows=1,
        auth_aborted=False,
    )
    assert "transient window(s) still" in transient

    clean = hp._build_note(
        dry_run=False,
        contracts_selected=3,
        contracts_completed=3,
        windows=3,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
    )
    assert "into the candle lake" in clean


def test_clean_symbols_excludes_quarantine_and_still_failing() -> None:
    """``_clean_symbols`` returns only the pairs that succeeded with no residual."""
    from brutex.adapters.groww.ingest import IngestReport
    from brutex.adapters.groww.option_ingest import GrowwOptionIngestReport, OptionPairFailure

    report = GrowwOptionIngestReport(
        pairs={
            ("CLEAN", "1day"): IngestReport(windows=1, bars_appended=1, transient_failures=0),
            ("STUCK", "1day"): IngestReport(
                windows=1, bars_appended=0, transient_failures=1, still_failed_windows=1
            ),
        },
        failures=(OptionPairFailure(symbol="QUAR", interval="1day", error="bad"),),
    )
    assert hp._clean_symbols(report) == frozenset({"CLEAN"})


# ── 1-day-first GLOBAL barrier (DEC-HISTORICAL-1DAY-FIRST-001) ─────────────────
#
# These tests use a REAL ``ParquetBarSink`` so Phase 1 writes real 1-day Parquet and
# Phase 2's start-date discovery reads the contract's actual earliest 1-day bar date
# back off the lake (the discovery is a lake read — a NullBarSink cannot exercise it).


class _PhasedSDK:
    """A two-phase fake: per-symbol 1-day bars at a KNOWN start date + recorded 1m starts.

    ``listing`` maps each contract ``groww_symbol`` to the earliest 1-day bar date the
    Phase-1 pull should reveal (so Phase 2 can be asserted to START there). A symbol mapped
    to ``None`` returns ZERO 1-day bars (it never traded → excluded from Phase 2).
    ``raise_for_day`` raises on a symbol's 1-day fetch (to fail the global barrier).
    ``minute_starts`` records the ``(symbol, start_time)`` of every 1-minute call so the
    test can assert each minute pull began at the contract's discovered date.
    """

    def __init__(
        self,
        *,
        listing: Mapping[str, date | None],
        raise_for_day: Mapping[str, Exception] | None = None,
    ) -> None:
        self._listing = dict(listing)
        self._raise_for_day = dict(raise_for_day or {})
        self.minute_starts: list[tuple[str, str]] = []
        self.day_symbols: list[str] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Never called (contracts are pre-resolved) — present for the Protocol surface."""
        return {"expiries": []}  # pragma: no cover - never reached (no re-enumeration)

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Never called (contracts are pre-resolved) — present for the Protocol surface."""
        return {"contracts": []}  # pragma: no cover - never reached (no re-enumeration)

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Phase 1 → a 1-day bar at the symbol's listing; Phase 2 → a recorded 1-minute bar."""
        symbol = str(kw["groww_symbol"])
        interval = str(kw["candle_interval"])
        if interval == "1day":
            self.day_symbols.append(symbol)
            exc = self._raise_for_day.get(symbol)
            if exc is not None:
                raise exc
            listing = self._listing.get(symbol)
            if listing is None:
                return {"candles": []}  # no 1-day data → excluded from Phase 2
            ts = f"{listing.isoformat()}T09:15:00"
            return {"candles": [[ts, 100.0, 110.0, 90.0, 105.0, 50, 7]]}
        # 1-minute: record where this contract's minute pull STARTED.
        self.minute_starts.append((symbol, str(kw["start_time"])))
        return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


def _real_sink(lake_root: Path) -> BarSink:
    """A real catalog-free ``ParquetBarSink`` (the dashboard/derivative-pull reality)."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def test_one_day_pulled_first_even_when_only_minute_requested(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """Phase 1 (1-day) ALWAYS runs first — even for a 1-minute-ONLY request."""
    sdk = _PhasedSDK(
        listing={
            "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-21000-PE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 8),
        }
    )
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),  # operator asked for 1-minute ONLY
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    # 1-day was pulled FIRST for every contract despite the 1-minute-only request.
    assert set(sdk.day_symbols) == {r.contract_symbol for r in selected}
    assert report.phase1_clean is True
    assert report.minute_requested is True
    assert report.minute_skipped is False
    # Then 1-minute ran for every contract (the barrier passed).
    assert {sym for sym, _start in sdk.minute_starts} == {r.contract_symbol for r in selected}


def test_phase2_minute_starts_at_discovered_one_day_date(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """Each contract's 1-minute pull STARTS from its own discovered earliest 1-day date."""
    listing = {
        "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 5),
        "NSE-NIFTY-25Jan24-21000-PE": date(2024, 1, 12),
        "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 3),
    }
    sdk = _PhasedSDK(listing=listing)
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    # The minute pull for each contract began at the DISCOVERED 1-day listing date.
    started = {sym: start[:10] for sym, start in sdk.minute_starts}
    for symbol, day in listing.items():
        assert started[symbol] == day.isoformat()
    # The report carries each contract's discovered start date verbatim.
    assert report.start_dates == {sym: day.isoformat() for sym, day in listing.items()}


def test_global_barrier_skips_minute_when_any_one_day_fails(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """A contract whose 1-day FAILS → 1-minute is NOT attempted for ANY contract."""
    sdk = _PhasedSDK(
        listing={
            "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-21000-PE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 8),
        },
        raise_for_day={"NSE-NIFTY-25Jan24-21000-CE": GrowwAPIBadRequestException("bad day")},
    )
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    # The 1-day phase did NOT clean → 1-minute SKIPPED entirely (no minute calls at all).
    assert report.phase1_clean is False
    assert report.minute_skipped is True
    assert sdk.minute_starts == []
    assert "1-day phase INCOMPLETE" in report.note
    assert "SKIPPED" in report.note
    # The quarantined CE is NOT in the 1-day completion set; the clean PE + FUT are.
    assert "NSE-NIFTY-25Jan24-21000-CE" not in report.phase1_complete
    assert "NSE-NIFTY-25Jan24-21000-PE" in report.phase1_complete


def test_zero_one_day_bar_contract_excluded_from_phase2(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """A contract with ZERO 1-day bars is EXCLUDED from Phase 2 (its 1-minute is empty too)."""
    sdk = _PhasedSDK(
        listing={
            "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-21000-PE": None,  # never traded → no 1-day data
            "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 8),
        }
    )
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    # The barrier still passes (an EMPTY 1-day window is a SETTLED clean window), but the
    # zero-data PE is absent from start_dates → it is NOT in the Phase-2 minute pulls.
    assert report.phase1_clean is True
    minute_symbols = {sym for sym, _start in sdk.minute_starts}
    assert "NSE-NIFTY-25Jan24-21000-PE" not in minute_symbols
    assert "NSE-NIFTY-25Jan24-21000-PE" not in report.start_dates
    assert minute_symbols == {"NSE-NIFTY-25Jan24-21000-CE", "NSE-NIFTY-25Jan24-FUT"}


def test_both_intervals_pull_one_day_then_minute(registry: RegistryStore, tmp_lake: Path) -> None:
    """``both`` → Phase 1 (1-day) then Phase 2 (1-minute); 1-day is never pulled twice."""
    sdk = _PhasedSDK(
        listing={
            "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-21000-PE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 8),
        }
    )
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day", "1minute"),  # the ``both`` ordering
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    # Each contract's 1-day was pulled EXACTLY once (Phase 1, not duplicated by Phase 2).
    assert sdk.day_symbols.count("NSE-NIFTY-25Jan24-FUT") == 1
    assert report.minute_requested is True
    assert report.minute_skipped is False
    assert {sym for sym, _ in sdk.minute_starts} == {r.contract_symbol for r in selected}


def test_one_day_only_request_runs_only_phase1(registry: RegistryStore, tmp_lake: Path) -> None:
    """``intervals=["1day"]`` runs only Phase 1 — no minute pull, never skipped."""
    sdk = _PhasedSDK(
        listing={
            "NSE-NIFTY-25Jan24-21000-CE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-21000-PE": date(2024, 1, 8),
            "NSE-NIFTY-25Jan24-FUT": date(2024, 1, 8),
        }
    )
    sink = _real_sink(tmp_lake)
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=tmp_lake,
        dry_run=False,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory_with(sdk, sink),
        now=_NOW,
    )
    assert report.minute_requested is False
    assert report.minute_skipped is False
    assert report.start_dates == {}
    assert sdk.minute_starts == []  # Phase 2 never ran
    assert report.contracts_completed == 3


def test_dry_run_minute_request_is_honest_empty(registry: RegistryStore) -> None:
    """A dry-run 1-minute request settles Phase 1 empty-clean, then runs Phase 2 empty."""
    # The default dry-run factory uses the NullBarSink (no lake write), so start-date
    # discovery finds nothing → every contract excluded from Phase 2 → honest 0-bar no-op.
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        now=_NOW,
    )
    assert report.dry_run is True
    assert report.bars_appended == 0
    # Phase 1 settled (empty windows are clean) → barrier passes; no lake → no discovered
    # starts → Phase 2 has nothing to pull (honest empty, never fabricated).
    assert report.phase1_clean is True
    assert report.minute_requested is True
    assert report.minute_skipped is False
    assert report.start_dates == {}


def test_discover_start_dates_catalog_unavailable_degrades_empty(tmp_path: Path) -> None:
    """A missing-deps / unopenable catalog degrades start discovery to an EMPTY map (loud)."""
    rows = [
        ContractRow(
            "NSE",
            "NIFTY",
            date(2024, 1, 25),
            "NSE-NIFTY-25Jan24-21000-CE",
            21000.0,
            "CE",
            "none",
            "r",
            _FETCHED,
        )
    ]
    rebuilt = hp._rebuild_contracts(rows)
    # Point at a path whose catalog cannot be reconciled (no bars/ lake on disk → reconcile
    # finds nothing, returns {} cleanly). The contract has no coverage → absent from the map.
    out = hp._discover_start_dates(rebuilt=rebuilt, lake_root=tmp_path, start=_FROM, end=_TO)
    assert out == {}


def test_discover_start_dates_catalog_open_raises_degrades_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Catalog.open`` raising :class:`CatalogError` → an EMPTY map (loud degrade)."""
    from brutex.data.errors import CatalogError

    def _boom(_path: Path) -> object:
        raise CatalogError("no duckdb")

    monkeypatch.setattr("brutex.dashboard.historical_pull.Catalog.open", staticmethod(_boom))
    out = hp._discover_start_dates(rebuilt=[], lake_root=tmp_path, start=_FROM, end=_TO)
    assert out == {}


def test_discover_start_dates_reconcile_raises_degrades_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile that raises :class:`CatalogError` → an EMPTY map; the catalog is closed."""
    import brutex.data.catalog_reconcile as cr
    from brutex.data.errors import CatalogError

    def _boom(**_kw: object) -> None:
        raise CatalogError("busy lake")

    monkeypatch.setattr(cr, "reconcile_catalog_from_lake", _boom)
    out = hp._discover_start_dates(rebuilt=[], lake_root=tmp_path, start=_FROM, end=_TO)
    assert out == {}


def test_discover_start_dates_per_contract_error_skips_that_contract(
    tmp_path: Path,
) -> None:
    """A malformed contract symbol (no exchange token) is skipped — the pass continues."""
    bad = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 1, 25),
        symbol="NODASH",  # no '-' → contract_exchange raises ValueError downstream
        contract_type="FUT",
        strike=None,
    )
    row = ContractRow(
        "NSE", "NIFTY", date(2024, 1, 25), "NODASH", None, "FUT", "none", "r", _FETCHED
    )
    out = hp._discover_start_dates(rebuilt=[(row, bad)], lake_root=tmp_path, start=_FROM, end=_TO)
    # The malformed contract is excluded from the discovered-start map (skipped, not raised).
    assert out == {}
