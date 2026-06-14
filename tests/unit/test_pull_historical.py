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

from collections.abc import Sequence
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
