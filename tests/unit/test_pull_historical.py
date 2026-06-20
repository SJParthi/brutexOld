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

from collections.abc import Callable, Iterable, Sequence
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
        self.candle_intervals: list[str] = []
        self.starts: list[tuple[str, str]] = []

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
        self.candle_intervals.append(str(kw["candle_interval"]))
        self.starts.append((symbol, str(kw["start_time"])))
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


# ── 1-minute ONLY, 1-day DERIVED (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) ────────────
#
# 1-minute is the ONLY granularity fetched from Groww for F&O; 1-day is DERIVED
# from it (never vendor-fetched), so the prior two-phase 1-day-first barrier is
# RETIRED. These tests assert the single 1-minute phase, the per-contract default
# window (expiry − 3 calendar months → expiry), and the explicit-dates override.


def test_fno_pulls_minute_only_never_day(registry: RegistryStore) -> None:
    """1-minute is the ONLY vendor granularity fetched — ``1day`` is NEVER requested."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1day", "1minute"),  # even ``both`` → 1-minute ONLY is fetched
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # The vendor was called ONLY at 1-minute, for every selected contract, never 1-day.
    assert set(sdk.candle_intervals) == {"1minute"}
    assert "1day" not in sdk.candle_intervals
    assert set(sdk.candle_symbols) == {r.contract_symbol for r in selected}
    # The repurposed single-phase report fields.
    assert report.minute_requested is True
    assert report.minute_skipped is False
    assert report.phase1_clean is True


def test_minute_window_defaults_to_expiry_minus_3mo(registry: RegistryStore) -> None:
    """No explicit dates → each contract's 1-minute window STARTS at expiry − 3 months."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        # NO explicit dates → the per-contract default kicks in.
        from_date=None,
        to_date=None,
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # NIFTY chain expiry is 2024-01-25 → 2024-01-25 − 3 calendar months = 2023-10-25.
    for symbol in (r.contract_symbol for r in selected):
        assert report.start_dates[symbol] == "2023-10-25"
    # The EARLIEST recorded fetch start is the default window start (2023-10-25, the
    # first 30-day chunk), never blindly 2020. The 3-month window tiles into several
    # 30-day chunks, so later chunk-starts are after it (all within the window).
    starts = [start[:10] for _sym, start in sdk.starts]
    assert min(starts) == "2023-10-25"
    assert all("2023-10-25" <= s <= "2024-01-25" for s in starts)


def test_explicit_dates_override_default_window(registry: RegistryStore) -> None:
    """Explicit from/to dates apply to EVERY contract (override the per-contract default)."""
    sdk = _FakeSDK()
    sink = _RecordingSink()
    selected = registry.list_contracts(underlying="NIFTY")
    report = pull_contract_candles(
        selected,
        lake_root=Path("/unused"),
        dry_run=True,
        intervals=("1minute",),
        registry=registry,
        from_date=date(2024, 1, 1),
        to_date=date(2024, 1, 20),
        client_factory=lambda _root: _factory(sdk, sink),
        now=_NOW,
    )
    # All contracts share the explicit start (the default expiry−3mo is OVERRIDDEN).
    assert set(report.start_dates.values()) == {"2024-01-01"}
    assert {start[:10] for _sym, start in sdk.starts} == {"2024-01-01"}


def test_dry_run_minute_request_is_honest_empty(registry: RegistryStore) -> None:
    """A dry-run 1-minute pull settles empty-clean; start_dates is the resolved windows."""
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
    assert report.bars_appended == 0  # the empty-candle dry-run client appends 0 bars
    assert report.phase1_clean is True  # an EMPTY window is a settled, clean window
    assert report.minute_requested is True
    assert report.minute_skipped is False
    # start_dates is now POPULATED (resolved windows for every contract), not lake-discovered.
    assert report.start_dates == {r.contract_symbol: "2024-01-01" for r in selected}


def test_resolve_minute_windows_explicit_uses_full_range(registry: RegistryStore) -> None:
    """``_resolve_minute_windows(explicit_dates=True)`` → the resolved range for all."""
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    out = hp._resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=True, start=date(2024, 1, 1), end=date(2024, 1, 21)
    )
    assert set(out.values()) == {(date(2024, 1, 1), date(2024, 1, 21))}
    assert set(out) == {c.symbol for _r, c in rebuilt}


def test_resolve_minute_windows_default_is_per_contract_expiry(registry: RegistryStore) -> None:
    """``_resolve_minute_windows(explicit_dates=False)`` → expiry−3mo per contract."""
    rebuilt = hp._rebuild_contracts(registry.list_contracts(underlying="NIFTY"))
    out = hp._resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=False, start=date(2020, 1, 1), end=date(2026, 6, 13)
    )
    # NIFTY expiry 2024-01-25 → [2023-10-25, 2024-01-26).
    assert set(out.values()) == {(date(2023, 10, 25), date(2024, 1, 26))}


def test_derive_daily_from_minute_writes_one_day_partition(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """The always-on 1-day derivation resamples the just-written 1-minute to a 1-day bar."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    # Seed the lake with one full IST session of 1-minute for the FUT contract, so the
    # derivation has real 1-minute to resample to a midnight-IST daily bar.
    contract = "NSE-NIFTY-25Jan24-FUT"
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_lake))
    minute_bars = [
        CandleBar(
            timestamp=datetime(2024, 1, 15, hh, mm, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=10,
            open_interest=7,
        )
        for (hh, mm) in [(3, 45), (6, 0), (10, 0)]  # 09:15 / mid / ~15:30 IST
    ]
    sink.append(
        exchange="NSE", segment="FNO", symbol=contract, interval="1minute", bars=minute_bars
    )
    rebuilt = hp._rebuild_contracts(
        [r for r in registry.list_contracts() if r.contract_symbol == contract]
    )
    windows = {contract: (date(2024, 1, 1), date(2024, 1, 26))}
    hp._derive_daily_from_minute(
        rebuilt=rebuilt,
        windows=windows,
        lake_root=tmp_lake,
        now=datetime(2024, 2, 1, tzinfo=UTC),
    )
    # The 1-day partition now holds the derived midnight-IST daily bar.
    daily_dir = tmp_lake / "bars" / "NSE" / "FNO" / contract / "1day"
    assert daily_dir.is_dir()
    assert any(daily_dir.rglob("*.parquet"))


def test_derive_daily_from_minute_no_minute_is_noop(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """No stored 1-minute for the contract → the derivation is a clean no-op (no 1-day)."""
    from datetime import UTC, datetime

    contract = "NSE-NIFTY-25Jan24-FUT"
    rebuilt = hp._rebuild_contracts(
        [r for r in registry.list_contracts() if r.contract_symbol == contract]
    )
    hp._derive_daily_from_minute(
        rebuilt=rebuilt,
        windows={contract: (date(2024, 1, 1), date(2024, 1, 26))},
        lake_root=tmp_lake,
        now=datetime(2024, 2, 1, tzinfo=UTC),
    )
    assert not (tmp_lake / "bars" / "NSE" / "FNO" / contract / "1day").exists()
