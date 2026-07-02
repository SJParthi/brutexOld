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
    """One 1-day bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
    return {"candles": [["2024-01-25T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


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


# ── the module exposes exactly its public surface ──────────────────────────────


def test_public_surface() -> None:
    assert set(cp.__all__) == {"CompositePullReport", "pull_historical_for_underlyings"}
