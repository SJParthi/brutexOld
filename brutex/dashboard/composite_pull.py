"""One-click multi-select F&O historical pull — the metadata→candle COMPOSITE.

DEC-FNO-ONECLICK-HISTORICAL-001 (the one-click "enumerate + pull" capstone of the
metadata/historical split). The two power-user buttons stay exactly as they are:

* "Pull expiries + contracts" (PR-3, DEC-DERIV-METADATA-ROUTE-001) — the METADATA
  half: refresh every expiry DATE + contract SYMBOL into ``registry.duckdb``,
  ZERO candles.
* "Pull historical" single-underlying (PR-5, DEC-DERIV-PULL-HISTORICAL-001) — the
  CANDLE half: download OHLC for the ALREADY-cached registry rows of ONE
  underlying / chain / contract / all.

The operator's request (2026-06-19) is the one-click fusion of those two over a
MULTI-select: tick many F&O symbols, click "Pull historical", and for EACH
F&O-eligible selected symbol run (a) the guarded metadata refresh THEN (b) the
contract candle pull over the freshly-cached registry rows — in that order, for
both FUT and CE/PE legs.

## Why a thin orchestrator (NOT a new fused engine)

The retired fused chain pull (decommissioned by DEC-DERIV-DECOMMISSION-FUSED-001;
the dead ``pull_all_derivatives`` anti-pattern guarded against in
``historical_pull.py``) RE-ENUMERATED the chain INSIDE the candle engine on every
click — the runaway "pull everything, every time" shape. This module does the
OPPOSITE: it COMPOSES the two existing, separately-tested spines WITHOUT fusing
them. Phase (a) calls :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`
(the metadata enumerator); phase (b) re-reads the freshly-persisted
:class:`~brutex.data.registry_store.ContractRow` rows and calls
:func:`~brutex.dashboard.historical_pull.pull_contract_candles` (the candle engine,
which NEVER re-enumerates). The metadata/candle split is preserved — the candle
engine still receives a CONCRETE registry selection, never a chain enumeration.

## The safety guard (the operator's explicit ask)

Phase (a) reuses the SAME reference-only partition the metadata route uses
(DEC-SKIP-REF-METADATA-001): an index / India VIX / a non-F&O NTM constituent is
``reference_only`` and is SKIPPED before any network call — zero ``get_expiries``,
zero contracts enumerated, zero candles. The skipped set is REPORTED (never
silently dropped). So ticking ALL symbols (including reference-only ones) and
clicking "Pull historical" pulls candles for the F&O-eligible names only and is a
true zero-call no-op for every reference-only underlying.

## Both FUT and CE/PE (the futures supersession)

The metadata enumerator persists FUT + CE + PE legs (no side filter), and the
candle engine pulls each persisted leg under its own ledger namespace
(``groww-option`` / ``groww-future``). So a composite selection pulls FUTURES as
well as options — superseding the stale "never pull futures" decree
(DEC-OPTIONS-ELIGIBLE-GUARD-001, #602) for the historical-lake pull, consistent
with DEC-GROWW-FUTURE-INGEST-001 (#707) + DEC-MOVERS-FUTURES-001 (#720). The
BRUTE-FORCE ENGINE is UNCHANGED (NIFTY/BANKNIFTY/SENSEX only) — this is the LAKE.

## Honest envelope (Golden Rule #1 + #10)

* **No re-enumeration in the candle engine** — phase (b) feeds CONCRETE registry
  rows; the metadata/candle split is intact.
* **Reference-only is a true zero-call no-op** — skipped before any network call,
  reported never dropped.
* **Incremental + idempotent** — phase (b) shares the WindowLedger, so a second
  composite run over the same selection skips already-settled windows
  (``is_skippable``) and re-fetches ONLY non-settled ones.
* **Network-bound, NOT O(1)-total** — both phases are I/O-bound (one
  ``get_expiries`` per cell + one ``get_historical_candles`` per contract-window);
  the phasing optimises the work set, it does NOT make the pull O(1)-total.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brutex.dashboard.fno_underlying import (
    primary_fno_underlying,
    resolve_fno_index_aliases,
)
from brutex.dashboard.historical_pull import (
    HistoricalClientFactory,
    HistoricalPullReport,
    pull_contract_candles,
)
from brutex.dashboard.metadata_pull import (
    MetadataClientFactory,
    MetadataPullReport,
    # Reuse the metadata module's shared coverage-sink Protocol (CLAUDE.md §11 COMMON —
    # one source of truth; don't redeclare the same structural type here). The friend
    # import is the established convention (pyproject reportPrivateUsage="warning" +
    # per-site ignore at deliberate cross-module reuse sites — DEC-P0-017).
    _CoverageSink,  # pyright: ignore[reportPrivateUsage]
    pull_expiries_contracts,
)
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.adapters.groww.option_ingest import ProgressHook
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.models import Instrument

#: Structured logger — the reference-only skip + the empty-registry degrade are logged
#: LOUDLY (Golden Rule #10 observability), never silent.
_LOG = structlog.get_logger("brutex.dashboard.composite_pull")


@dataclass(frozen=True, slots=True)
class CompositePullReport:
    """The honest outcome of one one-click multi-select metadata→candle composite.

    Two-sub-report shape: the composite delegates to the two existing spines and
    carries BOTH their reports plus the reference-only skip set, so every phase is
    auditable from one object.

    Attributes
    ----------
    dry_run
        ``True`` when the empty-chain / empty-candle dry-run clients were used.
    requested
        The symbol names the operator selected (before the reference-only partition).
    tradable
        The F&O-eligible symbol names that survived the partition (phase-a input).
    skipped_reference_only
        The selected symbol names skipped as reference-only — a TRUE zero-call no-op
        (no ``get_expiries``, no contracts, no candles). Reported, never dropped.
    metadata
        The phase-(a) metadata-refresh report (``None`` when every selection was
        reference-only — phase (a) never ran).
    historical
        The phase-(b) candle-pull report (``None`` when phase (a) found no tradable
        underlying OR the freshly-cached registry held no matching contract).
    note
        An honest one-line outcome summary.
    """

    dry_run: bool
    requested: tuple[str, ...]
    tradable: tuple[str, ...]
    skipped_reference_only: tuple[str, ...]
    metadata: MetadataPullReport | None
    historical: HistoricalPullReport | None
    note: str

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the composite route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "requested": list(self.requested),
            "tradable": list(self.tradable),
            "skipped_reference_only": list(self.skipped_reference_only),
            "skipped_count": len(self.skipped_reference_only),
            "metadata": None if self.metadata is None else self.metadata.to_dict(),
            "historical": None if self.historical is None else self.historical.to_dict(),
            "note": self.note,
        }


def _partition_reference_only(
    instruments: Sequence[Instrument], reference_only_ids: frozenset[int]
) -> tuple[list[Instrument], list[str]]:
    """Split *instruments* into ``(tradable, skipped_reference_only_names)``.

    Reuses the SAME ``security_id`` membership test the metadata route uses
    (DEC-SKIP-REF-METADATA-001) so a mistaken reference-only selection is a TRUE
    zero-call no-op (it never reaches phase (a)'s network calls).
    """
    tradable = [inst for inst in instruments if inst.security_id not in reference_only_ids]
    skipped = [inst.symbol_name for inst in instruments if inst.security_id in reference_only_ids]
    return tradable, skipped


def _registry_read_keys(instruments: Sequence[Instrument]) -> tuple[str, ...]:
    """Map the tradable spot instruments → the REGISTRY ``underlying`` keys to re-read.

    DEC-PULL-FORENSIC-WAVE2-001 — the read-key half of the write/read symmetry.
    Phase (a) (:func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`)
    persists every expiry/contract under the PRIMARY F&O underlying form
    (:func:`~brutex.dashboard.fno_underlying.primary_fno_underlying` — the alias /
    normalization bridge: ``NIFTYJR`` → ``NIFTYNXT50``, ``BAJAJ-AUTO`` →
    ``BAJAJAUTO``), but phase (b) previously re-read with the RAW spot
    ``symbol_name`` — so an aliased index / hyphenated stock read back ZERO rows and
    its freshly-enumerated chain was silently invisible (no candles pulled). The
    read key now goes through the SAME resolver the write key uses, byte-symmetric.
    Order-preserving first-wins de-dup mirrors the write-side grid de-dup, so two
    spot spellings that share one F&O primary are read once.
    """
    aliases = resolve_fno_index_aliases()
    keys: list[str] = []
    seen: set[str] = set()
    for inst in instruments:
        key = primary_fno_underlying(inst.symbol_name, aliases=aliases)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def _select_fresh_contracts(
    registry: RegistryStore, underlyings: Sequence[str]
) -> list[ContractRow]:
    """Re-read the FRESHLY-cached registry rows for the tradable *underlyings*.

    Phase (b) reads the registry AFTER phase (a) refreshed it, so the candle pull
    sees the just-enumerated expiries + contracts (FUT + CE + PE) — without the
    candle engine ever re-enumerating the chain. One ``list_contracts`` per
    underlying keeps the SQL static (CLAUDE.md §11 COMMON). *underlyings* must be
    the RESOLVED registry keys (:func:`_registry_read_keys`), never raw spot names.
    """
    rows: list[ContractRow] = []
    for underlying in underlyings:
        rows.extend(registry.list_contracts(underlying=underlying))
    return rows


def pull_historical_for_underlyings(
    instruments: Sequence[Instrument],
    *,
    reference_only_ids: frozenset[int],
    registry: RegistryStore,
    coverage_store: _CoverageSink,
    lake_root: Path,
    dry_run: bool,
    intervals: Sequence[str],
    years: Sequence[int],
    from_date: date | None = None,
    to_date: date | None = None,
    now: datetime | None = None,
    today: date | None = None,
    metadata_client_factory: MetadataClientFactory | None = None,
    historical_client_factory: HistoricalClientFactory | None = None,
    metadata_run_parallel: RunParallel | None = None,
    historical_run_parallel: RunParallel | None = None,
    on_progress: ProgressHook | None = None,
    session_rate: SessionRateController | None = None,
    until_clean: bool = True,
    scope_start: date | None = None,
    scope_end: date | None = None,
) -> CompositePullReport:
    """One-click composite: refresh metadata THEN pull candles for the selected underlyings.

    For the F&O-eligible (``tradable``) subset of *instruments* — partitioned by the
    SAME reference-only guard the metadata route uses (DEC-SKIP-REF-METADATA-001) —
    run BOTH existing spines in order:

    1. **Phase (a) — metadata refresh.** Call
       :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts` to enumerate +
       persist every expiry DATE + contract SYMBOL (FUT + CE + PE) into *registry*
       (ZERO candles). Reference-only underlyings are SKIPPED before this — a true
       zero-call no-op.
    2. **Phase (b) — candle pull.** Re-read the freshly-persisted
       :class:`~brutex.data.registry_store.ContractRow` rows for the tradable
       underlyings and call
       :func:`~brutex.dashboard.historical_pull.pull_contract_candles` over THAT
       concrete selection (the candle engine NEVER re-enumerates). 1-day-first →
       1-minute, end clamped to each contract's expiry, both FUT + CE/PE legs.

    The two phases are COMPOSED, not fused — the metadata/candle split is preserved
    (the candle engine receives concrete registry rows, never a chain enumeration).

    Parameters
    ----------
    instruments
        The operator's selected underlyings (indices and/or F&O stocks) — BEFORE the
        reference-only partition.
    reference_only_ids
        The ``security_id`` set flagged reference-only (the store's
        ``reference_only_security_ids``); members are skipped as a zero-call no-op.
    registry
        The shared metadata sink — phase (a) writes it, phase (b) re-reads it.
    coverage_store
        The per-``(underlying, year)`` rollup sink phase (a) writes (``/coverage``).
    lake_root
        The lake root phase (b)'s Path-A candle sink writes under.
    dry_run
        When ``True``, both phases use the honest empty dry-run clients (no creds).
    intervals
        The operator's requested candle intervals (``1day`` / ``1minute`` / both);
        ``1day`` is ALWAYS pulled first by phase (b) (DEC-HISTORICAL-1DAY-FIRST-001).
    years
        The calendar years phase (a)'s metadata grid covers (e.g. ``2020 → today``).
    from_date, to_date
        Optional inclusive candle date bounds forwarded to phase (b); default
        ``[2020-01-01, previous day]``.
    now, today
        Injected clocks (tests pin them; default ``datetime.now(UTC)`` / its date).
    metadata_client_factory, historical_client_factory
        Injected client factories for the two phases (tests pass fakes); ``None``
        selects each phase's dry-run / live default.
    metadata_run_parallel, historical_run_parallel
        Injected concurrency primitives for the two phases (tests inject synchronous
        fakes); ``None`` selects each phase's production default.
    on_progress
        The 3-arg candle progress hook forwarded to phase (b).
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    until_clean
        Forwarded to phase (b) (default ``True`` — auto-retry every failed window
        until the selection drains clean; DEC-GROWW-DERIV-UNTIL-CLEAN-001).
    scope_start, scope_end
        OPTIONAL inclusive EXPIRY-scope bounds forwarded to phase (b)
        (DEC-FNO-EXPIRY-SCOPE-001): only registry contracts whose expiry lies
        within ``[scope_start, scope_end]`` are candle-pulled; an out-of-scope
        expiry is a true zero-call, zero-ledger-row skip (one honest
        ``fno.expiry_out_of_scope`` log per expiry group; stored bars
        untouched). The scope selects WHICH expiries — the in-scope contracts
        keep their normal per-type default window law. Phase (a)'s metadata
        refresh is NOT scoped (the registry deliberately caches the FULL
        calendar; scoping is a candle-pull concern). ``None``/``None`` (the
        default) is byte-identical to the unscoped composite.

    Returns
    -------
    CompositePullReport
        Both sub-reports + the reference-only skip set + an honest summary note.
    """
    requested = tuple(inst.symbol_name for inst in instruments)
    tradable, skipped = _partition_reference_only(instruments, reference_only_ids)
    tradable_names = tuple(inst.symbol_name for inst in tradable)

    if not tradable:
        # Every selection was reference-only — a TRUE zero-call no-op (no get_expiries,
        # no contracts, no candles). Reported, never silently dropped.
        _LOG.info(
            "composite_pull.all_reference_only",
            requested=list(requested),
            skipped_reference_only=skipped,
            skipped_count=len(skipped),
        )
        note = f"all {len(skipped)} selected are reference-only (no F&O chain) — nothing to pull"
        return CompositePullReport(
            dry_run=dry_run,
            requested=requested,
            tradable=(),
            skipped_reference_only=tuple(skipped),
            metadata=None,
            historical=None,
            note=note,
        )

    _LOG.info(
        "composite_pull.started",
        requested=list(requested),
        tradable=list(tradable_names),
        skipped_reference_only=skipped,
        skipped_count=len(skipped),
        intervals=list(intervals),
        dry_run=dry_run,
    )

    # ── Phase (a) — METADATA refresh (the guarded enumerator; ZERO candles) ────────
    metadata = pull_expiries_contracts(
        tradable,
        registry=registry,
        coverage_store=coverage_store,
        years=years,
        dry_run=dry_run,
        run_parallel=metadata_run_parallel,
        client_factory=metadata_client_factory,
        session_rate=session_rate,
        now=now,
        today=today,
    )

    # ── Phase (b) — CANDLE pull over the FRESHLY-cached registry rows (no re-enum) ──
    # Read with the RESOLVED registry keys (the SAME primary form phase (a) persisted
    # under), never the raw spot names — DEC-PULL-FORENSIC-WAVE2-001 (an aliased index
    # like NIFTYJR or a hyphenated stock like BAJAJ-AUTO otherwise reads back ZERO rows).
    selected = _select_fresh_contracts(registry, _registry_read_keys(tradable))
    if not selected:
        # The metadata refresh persisted no contract (e.g. an honest dry-run / a name
        # with no live chain). An empty candle selection would be a 0-bar no-op, so
        # skip phase (b) and report honestly — never a fabricated contract (Golden #1).
        _LOG.info(
            "composite_pull.no_cached_contracts",
            tradable=list(tradable_names),
            dry_run=dry_run,
        )
        note = (
            f"refreshed metadata for {len(tradable)} underlying(s); no contracts cached "
            "yet (dry-run or empty chain) — no candles pulled"
        )
        return CompositePullReport(
            dry_run=dry_run,
            requested=requested,
            tradable=tradable_names,
            skipped_reference_only=tuple(skipped),
            metadata=metadata,
            historical=None,
            note=note,
        )

    historical = pull_contract_candles(
        selected,
        lake_root=lake_root,
        dry_run=dry_run,
        intervals=intervals,
        registry=registry,
        from_date=from_date,
        to_date=to_date,
        now=now,
        client_factory=historical_client_factory,
        on_progress=on_progress,
        session_rate=session_rate,
        until_clean=until_clean,
        run_parallel=historical_run_parallel,
        scope_start=scope_start,
        scope_end=scope_end,
    )

    # Display honesty (DEC-FNO-FAR-EXPIRY-WINDOW-001): a clean pull with strict-law
    # skips would read like partial failure ("8/12") — carry the honest breakdown so
    # a not-yet-open skip is never mistaken for a failed contract. Likewise a
    # scoped-out expiry (DEC-FNO-EXPIRY-SCOPE-001) is NAMED in the note, never a
    # silent shrink of the pulled set.
    skipped_suffix = (
        f" ({historical.contracts_not_yet_in_window} not yet in window)"
        if historical.contracts_not_yet_in_window
        else ""
    )
    scope_suffix = (
        f", {historical.expiries_out_of_scope} expiries out of scope skipped"
        if historical.expiries_out_of_scope
        else ""
    )
    # DEC-GREEKS-ENRICH-OBSERVE-001: a WHOLE-pass Greeks-enrichment failure is NAMED
    # in the composite note (the overlay is re-derivable — the next pull re-attempts
    # it), never a swallowed log line. Per-target quarantines never flip the verdict.
    # The clause carries the same <ErrorType> class as the historical note
    # (hostile-review fold — one uniform operator-facing string).
    greeks_suffix = (
        f", Greeks enrichment FAILED "
        f"({(historical.greeks_enrich_error or 'error').split(':', 1)[0]}) "
        "— overlay re-attempted next pull"
        if historical.greeks_enrich_ok is False
        else ""
    )
    note = (
        f"refreshed metadata for {len(tradable)} underlying(s) "
        f"({metadata.contracts_persisted} contract(s)), then pulled "
        f"{historical.contracts_completed}/{historical.contracts_selected} contract(s)"
        f"{skipped_suffix} ({historical.bars_appended} bar(s)){scope_suffix}{greeks_suffix}"
    )
    return CompositePullReport(
        dry_run=dry_run,
        requested=requested,
        tradable=tradable_names,
        skipped_reference_only=tuple(skipped),
        metadata=metadata,
        historical=historical,
        note=note,
    )


__all__ = [
    "CompositePullReport",
    "pull_historical_for_underlyings",
]
