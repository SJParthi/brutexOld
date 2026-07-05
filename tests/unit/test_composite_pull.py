# pyright: reportPrivateUsage=none
"""Tests for ``brutex.dashboard.composite_pull`` (DEC-FNO-ONECLICK-HISTORICAL-001).

The one-click multi-select metadata→candle COMPOSITE: tick many F&O symbols, click
"Pull historical", and for EACH F&O-eligible selected underlying run (a) the guarded
metadata refresh THEN (b) the contract candle pull over the freshly-cached registry
rows. The orchestrator COMPOSES the two existing spines (it does not fuse them).

Every path runs FULLY OFFLINE: ONE fake ``GrowwSDK`` carrying ``get_expiries`` +
``get_contracts`` (phase a) AND ``get_historical_candles`` (phase b) so the SAME
injected client serves both phases, a synchronous ``run_parallel`` (zero threads), a
REAL temp :class:`RegistryStore`, a tiny in-memory coverage sink, a recording
:class:`BarSink`, and injected clocks. The make-or-break invariants are asserted:

* phase (a) (metadata) runs BEFORE phase (b) (candles) — both spines invoked, in order;
* a reference-only underlying (index / India VIX / non-F&O) is a TRUE zero-call no-op
  (no ``get_expiries``, no contracts, no candles) — the operator's safety ask;
* FUTURES (FUT) are pulled alongside CE/PE — not options-only;
* a second composite run over the same selection SKIPS already-settled windows (only
  non-settled re-fetched) — the WindowLedger incremental skip.

100% line+branch on ``brutex.dashboard.composite_pull``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import OPTION_SEGMENT
from brutex.dashboard import composite_pull as cp
from brutex.dashboard.composite_pull import (
    CompositePullReport,
    pull_historical_for_underlyings,
)
from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.registry_store import RegistryStore
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.universe_phases import ItemResult

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_TODAY = date(2026, 6, 14)
#: A NARROW candle window so the 1-day tier tiles each contract into exactly ONE window.
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)


# ── offline fakes ──────────────────────────────────────────────────────────────


def _candle_payload() -> dict[str, object]:
    """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi).

    2024-01-19 (Friday, a regular NSE trading day) sits INSIDE every window this
    file fetches ([_FROM, _TO] spot + the expiry-clamped F&O windows), so the
    recorded outcome is a genuine stored-bar SUCCESS. The previous 2024-01-25
    stamp fell PAST the clamped window ends — every bar was future-dropped at the
    boundary, which DEC-DATA-HONESTY-ALLFILTERED-SEED-001 now honestly demotes to
    a re-pullable FAILED (the exact silent-green shape it exists to catch), so an
    out-of-window canned bar can no longer fake a settled SUCCESS fixture.
    """
    return {"candles": [["2024-01-19T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake serving BOTH phases off one client.

    Phase (a) calls ``get_expiries`` / ``get_contracts`` (canned maps); phase (b)
    calls ``get_historical_candles`` (a canned bar). ``calls`` records every
    invocation so a test can prove what ran (and that a reference-only underlying
    triggered ZERO calls). ``candle_symbols`` records each contract candle-fetched
    so the incremental-skip test can assert the second run re-fetches nothing.
    """

    def __init__(
        self,
        *,
        expiries: Mapping[tuple[str, str, int], Sequence[str]] | None = None,
        contracts: Mapping[tuple[str, str, str], Sequence[str]] | None = None,
    ) -> None:
        self._expiries = dict(expiries or {})
        self._contracts = dict(contracts or {})
        self.calls: list[str] = []
        self.candle_symbols: list[str] = []

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.calls.append("get_expiries")
        exchange = str(kw["exchange"])
        underlying = str(kw["underlying_symbol"])
        year = kw["year"]
        assert isinstance(year, int)
        return {"expiries": list(self._expiries.get((exchange, underlying, year), ()))}

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        self.calls.append("get_contracts")
        key = (str(kw["exchange"]), str(kw["underlying_symbol"]), str(kw["expiry_date"]))
        return {"contracts": list(self._contracts.get(key, ()))}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        self.calls.append("get_historical_candles")
        self.candle_symbols.append(str(kw["groww_symbol"]))
        return _candle_payload()


class _CoverageSink:
    """A tiny in-memory ``coverage_ledger`` rollup sink (PK upsert, list)."""

    def __init__(self, *, seed: Sequence[CoverageRecord] = ()) -> None:
        self._by_cell: dict[tuple[str, str, int], CoverageRecord] = {
            (r.exch_id, r.underlying_symbol, r.year): r for r in seed
        }

    def upsert_coverage(self, record: CoverageRecord) -> None:
        self._by_cell[(record.exch_id, record.underlying_symbol, record.year)] = record

    def list_coverage(self) -> list[CoverageRecord]:
        return list(self._by_cell.values())


class _RecordingSink:
    """A minimal recording :class:`BarSink` capturing every append's segment + symbol."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str, int]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[object],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _SyncRunParallel:
    """A synchronous, deterministic ``RunParallel`` fake — zero threads."""

    def __call__[ItemT, ResultT](
        self, fn: Callable[[ItemT], ResultT], items: Sequence[ItemT]
    ) -> list[ItemResult[ItemT, ResultT]]:
        out: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            try:
                result = fn(item)
            except Exception as exc:  # mirror the pool's capture-not-propagate
                out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
            else:
                out.append(ItemResult(item=item, ok=True, result=result, exc=None))
        return out


_sync = _SyncRunParallel()


def _instrument(symbol: str, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
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
        asm_gsm_flag=False,
    )


def _registry(tmp_path: Path) -> RegistryStore:
    return RegistryStore.open(tmp_path / "registry.duckdb")


def _metadata_factory(sdk: _FakeSDK) -> Callable[[], GrowwClient]:
    return lambda: GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _historical_factory(
    sdk: _FakeSDK, sink: BarSink
) -> Callable[[Path], tuple[GrowwClient, BarSink]]:
    return lambda _root: (
        GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
        sink,
    )


def _nifty_sdk() -> _FakeSDK:
    """A NIFTY chain SDK with CE + PE + FUT legs — so the futures-included invariant holds."""
    return _FakeSDK(
        expiries={("NSE", "NIFTY", 2024): ["2024-01-25"]},
        contracts={
            ("NSE", "NIFTY", "2024-01-25"): [
                "NSE-NIFTY-25Jan24-21000-CE",
                "NSE-NIFTY-25Jan24-21000-PE",
                "NSE-NIFTY-25Jan24-FUT",
            ]
        },
    )


def _run(
    instruments: Sequence[Instrument],
    *,
    sdk: _FakeSDK,
    sink: BarSink,
    registry: RegistryStore,
    coverage: _CoverageSink,
    reference_only_ids: frozenset[int] = frozenset(),
    intervals: Sequence[str] = ("1day",),
    lake_root: Path | None = None,
    scope_start: date | None = None,
    scope_end: date | None = None,
    on_bars: Callable[[int], None] | None = None,
    on_ledger_sync: Callable[[int, int, int, int, str], None] | None = None,
) -> CompositePullReport:
    if lake_root is None:
        # Each test gets its OWN lake (so the persisted WindowLedger never bleeds across
        # tests); a sibling of the registry's tmp dir keeps it inside the pytest tmp_path.
        path = registry.path
        assert isinstance(path, Path)
        lake_root = path.parent / "lake"
        lake_root.mkdir(exist_ok=True)
    return pull_historical_for_underlyings(
        instruments,
        reference_only_ids=reference_only_ids,
        registry=registry,
        coverage_store=coverage,
        lake_root=lake_root,
        dry_run=False,
        intervals=intervals,
        years=[2024],
        from_date=_FROM,
        to_date=_TO,
        now=_NOW,
        today=_TODAY,
        metadata_client_factory=_metadata_factory(sdk),
        historical_client_factory=_historical_factory(sdk, sink),
        metadata_run_parallel=_sync,
        historical_run_parallel=_sync,
        until_clean=False,
        scope_start=scope_start,
        scope_end=scope_end,
        on_bars=on_bars,
        on_ledger_sync=on_ledger_sync,
    )


# ── sequencing: phase (a) metadata THEN phase (b) candles, both spines invoked ──


def test_metadata_then_candles_both_spines(tmp_path: Path) -> None:
    """The composite enumerates the chain (phase a) THEN pulls its candles (phase b)."""
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=coverage
    )

    # Phase (a) ran FIRST: get_expiries + get_contracts appear before the first candle.
    first_candle = sdk.calls.index("get_historical_candles")
    assert "get_expiries" in sdk.calls[:first_candle]
    assert "get_contracts" in sdk.calls[:first_candle]

    # Phase (a) persisted the chain into the registry.
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 3
    assert report.metadata is not None
    assert report.metadata.contracts_persisted == 3

    # Phase (b) pulled the candles for the freshly-cached contracts (PATH A, FNO).
    assert report.historical is not None
    assert report.historical.contracts_selected == 3
    assert report.historical.contracts_completed == 3
    assert {seg for seg, *_ in sink.appends} == {OPTION_SEGMENT}
    assert set(sdk.candle_symbols) == {
        "NSE-NIFTY-25Jan24-21000-CE",
        "NSE-NIFTY-25Jan24-21000-PE",
        "NSE-NIFTY-25Jan24-FUT",
    }
    assert report.note.startswith("refreshed metadata for 1 underlying(s)")
    assert report.skipped_reference_only == ()
    assert report.tradable == ("NIFTY",)


# ── safety: reference-only underlyings are a TRUE zero-call no-op (operator ask) ─


def test_reference_only_in_mixed_selection_is_skipped_zero_calls(tmp_path: Path) -> None:
    """A mixed selection pulls the F&O name only; the REF underlying triggers ZERO calls."""
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    nifty = _instrument("NIFTY", 13)
    indiavix = _instrument("INDIAVIX", 26000)
    report = _run(
        [nifty, indiavix],
        sdk=sdk,
        sink=sink,
        registry=registry,
        coverage=coverage,
        reference_only_ids=frozenset({26000}),
    )

    # The REF name was skipped + reported; only NIFTY reached the pull.
    assert report.skipped_reference_only == ("INDIAVIX",)
    assert report.tradable == ("NIFTY",)
    # No contract / candle anywhere mentions INDIAVIX (a true zero-call no-op for it).
    assert not any("INDIAVIX" in s for s in sdk.candle_symbols)
    assert all("NIFTY" in s for s in sdk.candle_symbols)
    assert {c.underlying for c in registry.list_contracts()} == {"NIFTY"}


def test_all_reference_only_is_an_honest_zero_call_no_op(tmp_path: Path) -> None:
    """An ALL-REF selection makes ZERO vendor calls and returns an honest note."""
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("INDIAVIX", 26000), _instrument("NIFTYIT", 999)],
        sdk=sdk,
        sink=sink,
        registry=registry,
        coverage=coverage,
        reference_only_ids=frozenset({26000, 999}),
    )

    # Zero-call no-op: neither phase ran, the registry is untouched.
    assert sdk.calls == []
    assert sink.appends == []
    assert registry.count_contracts() == 0
    assert report.metadata is None
    assert report.historical is None
    assert report.tradable == ()
    assert set(report.skipped_reference_only) == {"INDIAVIX", "NIFTYIT"}
    assert report.note == "all 2 selected are reference-only (no F&O chain) — nothing to pull"


# ── futures included: a selection yields FUT + CE + PE (not options-only) ───────


def test_futures_pulled_alongside_options(tmp_path: Path) -> None:
    """The composite pulls the FUT leg too — superseding the never-futures decree #602."""
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    _run([_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=coverage)

    sides = {c.side for c in registry.list_contracts()}
    assert sides == {"CE", "PE", "FUT"}
    # The FUT contract's candles were fetched (not filtered out).
    assert "NSE-NIFTY-25Jan24-FUT" in sdk.candle_symbols


# ── incremental skip: a second run re-fetches ONLY non-settled windows ──────────


def test_second_run_skips_settled_windows(tmp_path: Path) -> None:
    """A re-pull over the same selection re-fetches NO candles (all windows settled)."""
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    # First run: every window is fetched + recorded SUCCESS in the shared WindowLedger.
    sdk1 = _nifty_sdk()
    sink1 = _RecordingSink()
    _run(
        [_instrument("NIFTY", 13)],
        sdk=sdk1,
        sink=sink1,
        registry=registry,
        coverage=coverage,
        lake_root=lake_root,
    )
    assert sdk1.candle_symbols  # the first run DID fetch candles

    # Second run over the SAME selection + SAME lake_root: the ledger marks every window
    # settled (is_skippable), so the candle phase re-fetches NOTHING.
    sdk2 = _nifty_sdk()
    sink2 = _RecordingSink()
    report2 = _run(
        [_instrument("NIFTY", 13)],
        sdk=sdk2,
        sink=sink2,
        registry=registry,
        coverage=coverage,
        lake_root=lake_root,
    )
    assert sdk2.candle_symbols == []  # NO candle re-fetched — every window was settled
    assert sink2.appends == []  # nothing re-appended
    assert report2.historical is not None
    assert report2.historical.bars_appended == 0


# ── honest degrade: tradable but no contract cached (empty chain) ───────────────


def test_tradable_with_empty_chain_skips_candle_phase(tmp_path: Path) -> None:
    """A tradable name whose chain has NO contracts → metadata ran, candles skipped honestly."""
    sdk = _FakeSDK(expiries={}, contracts={})  # the vendor returns an empty chain
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=coverage
    )

    # Phase (a) ran (get_expiries was called), but no contract was cached → no candles.
    assert "get_expiries" in sdk.calls
    assert "get_historical_candles" not in sdk.calls
    assert sink.appends == []
    assert report.metadata is not None
    assert report.historical is None
    assert "no contracts cached yet" in report.note


# ── report serialisation (the route + live panel read to_dict) ─────────────────


def test_to_dict_roundtrips_both_sub_reports(tmp_path: Path) -> None:
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=coverage
    )
    payload = report.to_dict()
    assert payload["requested"] == ["NIFTY"]
    assert payload["tradable"] == ["NIFTY"]
    assert payload["skipped_reference_only"] == []
    assert payload["skipped_count"] == 0
    assert isinstance(payload["metadata"], dict)
    assert isinstance(payload["historical"], dict)
    assert payload["dry_run"] is False


def test_to_dict_all_reference_only_has_null_sub_reports(tmp_path: Path) -> None:
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("INDIAVIX", 26000)],
        sdk=sdk,
        sink=sink,
        registry=registry,
        coverage=coverage,
        reference_only_ids=frozenset({26000}),
    )
    payload = report.to_dict()
    assert payload["metadata"] is None
    assert payload["historical"] is None
    assert payload["skipped_reference_only"] == ["INDIAVIX"]
    assert payload["skipped_count"] == 1


# ── aliased/hyphenated underlying: write-key/read-key SYMMETRY ──────────────────
# DEC-PULL-FORENSIC-WAVE2-001: phase (a) persists the chain under the PRIMARY F&O
# underlying form (NIFTYJR → NIFTYNXT50, BAJAJ-AUTO → BAJAJAUTO); phase (b) must
# re-read with the SAME resolved key — the raw spot name read back ZERO rows and the
# freshly-enumerated chain was silently invisible (no candles pulled).


def test_registry_read_keys_resolve_alias_normalize_and_dedup() -> None:
    """The read keys are the PRIMARY forms, order-preserving first-wins de-duped."""
    instruments = [
        _instrument("NIFTYJR", 1),  # sourced alias → NIFTYNXT50
        _instrument("BAJAJ-AUTO", 2),  # hyphen normalization → BAJAJAUTO
        _instrument("BAJAJAUTO", 3),  # same primary as above → de-duped
        _instrument("NIFTY", 4),  # canonical → byte-unchanged
    ]
    assert cp._registry_read_keys(instruments) == ("NIFTYNXT50", "BAJAJAUTO", "NIFTY")


def test_aliased_underlying_chain_visible_to_candle_phase(tmp_path: Path) -> None:
    """FAILS-BEFORE regression: an aliased index's fresh chain must reach phase (b).

    The operator selects ``NIFTYJR``; phase (a) resolves + persists the chain under
    the primary form ``NIFTYNXT50``. Before the fix, phase (b) re-read the registry
    with the raw ``NIFTYJR`` → zero rows → ``historical is None`` ("no contracts
    cached yet") despite a just-persisted chain — the whole-underlying silent no-pull.
    """
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTYNXT50", 2024): ["2024-01-25"]},
        contracts={
            ("NSE", "NIFTYNXT50", "2024-01-25"): [
                "NSE-NIFTYNXT50-25Jan24-60000-CE",
                "NSE-NIFTYNXT50-25Jan24-FUT",
            ]
        },
    )
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("NIFTYJR", 14)], sdk=sdk, sink=sink, registry=registry, coverage=coverage
    )

    # The chain WAS persisted under the primary key AND phase (b) found + pulled it.
    assert report.metadata is not None
    assert report.historical is not None, "aliased chain must be visible to phase (b)"
    assert "no contracts cached yet" not in report.note
    assert "get_historical_candles" in sdk.calls
    assert sorted(sdk.candle_symbols) == [
        "NSE-NIFTYNXT50-25Jan24-60000-CE",
        "NSE-NIFTYNXT50-25Jan24-FUT",
    ]
    assert sink.appends, "the aliased underlying's bars must actually be stored"


def test_composite_note_counts_not_yet_in_window(tmp_path: Path) -> None:
    """DEC-FNO-FAR-EXPIRY-WINDOW-001 display honesty on the composite note.

    A DEFAULT-dates pull (no from/to → envelope ends 2026-06-13) over a chain with
    three near legs (expiry 2024-01-25) plus ONE far Sep-2026 CE (opens 2026-08-30,
    not yet in window → strict-law skip): the note's completed/selected ratio carries
    the honest "(1 not yet in window)" breakdown so the skip never reads as a
    failure — and the far leg is never candle-fetched.
    """
    sdk = _FakeSDK(
        expiries={
            ("NSE", "NIFTY", 2024): ["2024-01-25"],
            ("NSE", "NIFTY", 2026): ["2026-09-29"],
        },
        contracts={
            ("NSE", "NIFTY", "2024-01-25"): [
                "NSE-NIFTY-25Jan24-21000-CE",
                "NSE-NIFTY-25Jan24-21000-PE",
                "NSE-NIFTY-25Jan24-FUT",
            ],
            ("NSE", "NIFTY", "2026-09-29"): ["NSE-NIFTY-29Sep26-21000-CE"],
        },
    )
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    report = pull_historical_for_underlyings(
        [_instrument("NIFTY", 13)],
        reference_only_ids=frozenset(),
        registry=registry,
        coverage_store=coverage,
        lake_root=lake_root,
        dry_run=False,
        intervals=("1day",),
        years=[2024, 2026],
        from_date=None,
        to_date=None,
        now=_NOW,
        today=_TODAY,
        metadata_client_factory=_metadata_factory(sdk),
        historical_client_factory=_historical_factory(sdk, sink),
        metadata_run_parallel=_sync,
        historical_run_parallel=_sync,
        until_clean=False,
    )
    assert report.historical is not None
    assert report.historical.contracts_not_yet_in_window == 1
    assert "3/4 contract(s) (1 not yet in window)" in report.note
    assert "NSE-NIFTY-29Sep26-21000-CE" not in sdk.candle_symbols  # never fetched


def test_composite_note_without_skips_has_no_breakdown(tmp_path: Path) -> None:
    """No strict-law skips → the composite note stays byte-identical (no breakdown)."""
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=coverage
    )
    assert report.historical is not None
    assert report.historical.contracts_not_yet_in_window == 0
    assert "not yet in window" not in report.note
    assert "3/3 contract(s)" in report.note


# ── the module exposes exactly its public surface ──────────────────────────────


def test_public_surface() -> None:
    assert set(cp.__all__) == {"CompositePullReport", "pull_historical_for_underlyings"}


# ── expiry-level date scoping (DEC-FNO-EXPIRY-SCOPE-001) ────────────────────────


def test_scope_threaded_to_candle_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``scope_start`` / ``scope_end`` reach ``pull_contract_candles`` verbatim.

    A capture spy on the candle spine proves the composite threads the expiry
    scope through unchanged (and that the default is ``None`` / ``None`` — the
    byte-identical unscoped pull).
    """
    captured: list[dict[str, object]] = []

    def _spy(*_args: object, **kwargs: object) -> object:
        captured.append(dict(kwargs))
        return _clean_historical_report()

    def _clean_historical_report() -> object:
        from brutex.dashboard.historical_pull import HistoricalPullReport

        return HistoricalPullReport(
            dry_run=False,
            intervals=("1day",),
            contracts_selected=0,
            contracts_completed=0,
            bars_appended=0,
            windows=0,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="spy",
            phase1_clean=True,
            phase1_complete=(),
            start_dates={},
            minute_requested=True,
            minute_skipped=False,
        )

    monkeypatch.setattr(cp, "pull_contract_candles", _spy)
    sdk = _nifty_sdk()
    registry = _registry(tmp_path)
    _run(
        [_instrument("NIFTY", 13)],
        sdk=sdk,
        sink=_RecordingSink(),
        registry=registry,
        coverage=_CoverageSink(),
        scope_start=date(2024, 1, 1),
        scope_end=date(2024, 1, 25),
    )
    assert len(captured) == 1
    assert captured[0]["scope_start"] == date(2024, 1, 1)
    assert captured[0]["scope_end"] == date(2024, 1, 25)
    # And the DEFAULT threads None/None (regression pin — unscoped by default).
    _run(
        [_instrument("NIFTY", 13)],
        sdk=_nifty_sdk(),
        sink=_RecordingSink(),
        registry=registry,
        coverage=_CoverageSink(),
    )
    assert captured[1]["scope_start"] is None
    assert captured[1]["scope_end"] is None


def test_composite_note_names_out_of_scope_expiries(tmp_path: Path) -> None:
    """An all-out-of-scope chain is NAMED in the composite note, never silent.

    The enumerated NIFTY chain (expiry 2024-01-25) with ``scope_end=2024-01-20``
    is entirely out of scope: zero candles pulled, and the note carries
    ", 1 expiries out of scope skipped".
    """
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    report = _run(
        [_instrument("NIFTY", 13)],
        sdk=sdk,
        sink=sink,
        registry=registry,
        coverage=_CoverageSink(),
        scope_start=date(2024, 1, 1),
        scope_end=date(2024, 1, 20),  # the 2024-01-25 chain is OUT
    )
    assert sdk.candle_symbols == []  # zero vendor candle calls
    assert report.historical is not None
    assert report.historical.expiries_out_of_scope == 1
    assert report.historical.contracts_out_of_scope == 3
    assert ", 1 expiries out of scope skipped" in report.note
    assert "then pulled 0/0 contract(s) (0 bar(s))" in report.note


# ── live-progress hooks threaded verbatim (DEC-PULL-PROGRESS-HONESTY-001) ────────


def test_progress_hooks_threaded_to_candle_phase(tmp_path: Path) -> None:
    """``on_bars`` / ``on_ledger_sync`` reach phase (b) end-to-end through the composite.

    Fails-before: the composite had no way to carry the dashboard's live-bars /
    honest-denominator hooks, so the F&O phase's headline froze (bars) and pinned
    (percent). This drives the REAL spines (no spy) and asserts BOTH hooks fire:
    the bars deltas sum to the report's appended bars (the no-double-count
    invariant across the composite boundary) and the ledger sync pushes the
    F&O-scoped totals.
    """
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    bars_deltas: list[int] = []
    syncs: list[tuple[int, int, int, int, str]] = []

    def _on_sync(resolved: int, total: int, bars: int, failed: int, current: str) -> None:
        syncs.append((resolved, total, bars, failed, current))

    report = _run(
        [_instrument("NIFTY", 13)],
        sdk=sdk,
        sink=sink,
        registry=registry,
        coverage=_CoverageSink(),
        on_bars=bars_deltas.append,
        on_ledger_sync=_on_sync,
    )
    assert report.historical is not None
    # This fixture's canned bar (2024-01-25) sits OUTSIDE the pull window, so 0 bars
    # land — the hook still fired per pair and its sum EQUALS the report (the
    # exactly-once contract; the >0-bars twin lives in
    # ``test_pull_historical_progress_honesty.py``).
    assert bars_deltas, "on_bars must fire through the composite boundary"
    assert sum(bars_deltas) == report.historical.bars_appended
    # The FINAL unthrottled push carries the settled F&O totals (3 contracts × 1 window).
    assert syncs, "on_ledger_sync must fire at least the final post-drain push"
    resolved, total, _bars, failed, _current = syncs[-1]
    assert total == 3 and resolved == 3 and failed == 0


# ── Greeks-enrichment failure suffix (DEC-GREEKS-ENRICH-OBSERVE-001) ────────────


def test_composite_note_names_greeks_enrich_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WHOLE-pass Greeks-enrichment failure is NAMED in the composite note.

    The driver's risk-free seam is broken (the same failure mode the historical
    suite pins) so the candle phase completes but its Greeks pass fails whole —
    the composite note must carry the FAILED suffix, never a swallowed log line.
    """
    pytest.importorskip("trading_core")
    import brutex.data.greeks_enrich as ge

    def _boom() -> object:
        raise RuntimeError("misconfigured risk-free source")

    monkeypatch.setattr(ge, "resolve_risk_free_source", _boom)
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=_CoverageSink()
    )
    assert report.historical is not None
    assert report.historical.greeks_enrich_ok is False
    # The clause carries the ErrorType class, uniform with the historical note.
    assert (
        ", Greeks enrichment FAILED (RuntimeError) — overlay re-attempted next pull" in report.note
    )


def test_composite_note_has_no_greeks_clause_on_a_clean_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed Greeks pass leaves the composite note byte-identical (no clause)."""
    pytest.importorskip("trading_core")
    monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)  # the fixed-6.5% default
    sdk = _nifty_sdk()
    sink = _RecordingSink()
    registry = _registry(tmp_path)
    report = _run(
        [_instrument("NIFTY", 13)], sdk=sdk, sink=sink, registry=registry, coverage=_CoverageSink()
    )
    assert report.historical is not None
    assert report.historical.greeks_enrich_ok is True
    assert "Greeks enrichment FAILED" not in report.note
