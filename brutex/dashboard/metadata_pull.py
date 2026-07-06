"""Metadata-only F&O pull — "Pull expiries + contracts" WITHOUT any candles.

DEC-DERIV-METADATA-ORCHESTRATION-001 (PR-2 of the metadata/historical split,
builds on DEC-DERIV-METADATA-HISTORICAL-SPLIT-001 #732 + DEC-UNIVERSE-PULL-PHASED-001).
This is the orchestration that finally WIRES the dormant two-phase engine
(:mod:`brutex.orchestrator.universe_phases`, DEC-UNIVERSE-PULL-PHASED-001) to the
metadata sinks PR-1 shipped — the :class:`~brutex.data.registry_store.RegistryStore`
(expiry DATES + contract SYMBOLS) and the shared ``coverage_ledger`` rollup the
``/coverage`` page reads.

## Why a NEW sibling (the metadata HALF of the metadata/historical split)

The retired fused bulk chain pull enumerated the chain AND pulled historical
candles into the ``bars/`` lake (the heavy, weeks-scale minute fill) in one click
— the runaway behaviour PR-6 (DEC-DERIV-DECOMMISSION-FUSED-001) decommissioned.
This module is the OPPOSITE half: it enumerates + PERSISTS the metadata (expiries
+ contracts) and pulls **ZERO candles** — so the cheap, NSE-verifiable chain SHAPE
lands in minutes and the heavy historical fill stays behind its own separate
button (:func:`~brutex.dashboard.historical_pull.pull_contract_candles`). Keeping
them as two siblings (CLAUDE.md §11 COMMON — one job each) means this module NEVER
constructs a :class:`~brutex.data.sink.BarSink` and NEVER calls
``get_historical_candles`` / ``run_groww_option_ingest``; that separation is the
whole point and is asserted by the tests.

## The two phases (delegated to the dormant engine)

:func:`pull_expiries_contracts` builds the Phase-1 ``(exch, underlying, year)``
cells via :func:`~brutex.orchestrator.universe_phases.phase1_cells`, computes the
incremental skip-sets from the registry + coverage ledger
(:func:`~brutex.orchestrator.universe_phases.cells_to_pull` /
:func:`~brutex.orchestrator.universe_phases.contract_jobs_to_pull` via
``settled`` frozensets), and drives
:func:`~brutex.orchestrator.universe_phases.run_two_phase`:

* **Phase 1** — ``fetch_expiries(cell)`` calls
  :func:`~brutex.adapters.groww.contracts.resolve_expiries` (metadata only) and
  PERSISTS the discovered dates as ONE deadline-guarded batch via
  :meth:`~brutex.data.registry_store.RegistryStore.upsert_expiries`
  (DEC-STORE-WRITE-DEADLINE-001 — the fetcher persists, the Phase-2 shape, so a
  wedged registry write defers the CELL, never the pull); the ``on_expiry_cell``
  callback records the outcome and writes the per-``(underlying, year)`` coverage
  rollup (:data:`~brutex.data.coverage_ledger.STATUS_EXPIRIES_ONLY` on success —
  the expiries are captured but contracts are a separate sub-phase here).
* **Phase 2** — ``fetch_contracts(job)`` calls
  :func:`~brutex.adapters.groww.contracts.resolve_contracts` (metadata only) and
  PERSISTS the job's contracts as ONE deadline-guarded batch via
  :meth:`~brutex.data.registry_store.RegistryStore.upsert_contracts`
  (DEC-STORE-WRITE-DEADLINE-001), taking the ``strike`` / ``side`` from the
  already-parsed :class:`~brutex.adapters.protocol.DerivativeContract` (never
  re-invented); the ``on_contract`` callback records the outcome.

## Honest envelope (Golden Rule #1 + #10)

* **No candles** — metadata only; the historical OHLC stays behind the separate
  "Pull historical" button. This module imports neither the sink nor the candle
  ingest.
* **Honest dry-run** (Golden Rule #1) — without live Groww credentials the F&O
  chain (which strikes/expiries exist) is unknown REAL vendor data we refuse to
  fabricate. The default dry-run client is the SAME empty-chain SDK
  :mod:`brutex.dashboard.derivatives_pull` uses, so a no-creds pull persists 0
  expiries + 0 contracts and reports an honest "set credentials" note — never an
  invented strike.
* **Network-bound, not O(1)-total** — one ``get_expiries`` per cell + one
  ``get_contracts`` per discovered expiry. The incremental skip
  (DEC-UNIVERSE-PULL-PHASED-001) keeps a re-click O(N) membership checks issuing
  only genuinely-new calls; the upsert PKs are the second dedup line (rerun-safe,
  CLAUDE.md §7).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import resolve_contracts, resolve_expiries
from brutex.dashboard.fno_underlying import (
    primary_fno_underlying,
    resolve_fno_index_aliases,
    resolve_fno_underlying_forms,
)
from brutex.data.coverage_ledger import (
    STATUS_COMPLETE,
    STATUS_EXPIRIES_ONLY,
    CoverageRecord,
    classify_cell,
)
from brutex.data.errors import StoreWriteDeadlineExceeded
from brutex.data.registry_store import RegistryStore
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import (
    ContractJob,
    ExpiryCell,
    RunParallel,
    cells_to_pull,
    contract_jobs_from,
    contract_jobs_to_pull,
    phase1_cells,
    run_two_phase,
    settled_contract_jobs,
    settled_expiry_cells,
)

if TYPE_CHECKING:
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

#: Observe-only structlog event for an underlying whose F&O expiry chain came back
#: EMPTY across EVERY year tried (DEC-FNO-UNDERLYING-RESOLVE-001 — the metadata-pull
#: twin of the post-fetch ``pull.underlying_all_empty`` of DEC-SUSPICIOUS-EMPTY-FLAG
#: -001 #919). Surfaced as a WARNING naming the resolved F&O underlying so a whole
#: F&O-eligible stock can never silently vanish — DETECT-only, no return/ledger
#: change, no auto-recovery.
METADATA_UNDERLYING_NO_EXPIRIES_EVENT: Final[str] = "metadata.underlying_no_expiries"

#: WARN — a Phase-1 cell's registry expiry-batch write blew the store-write deadline
#: (DEC-STORE-WRITE-DEADLINE-001): the cell records ``failed`` (never settled) and is
#: re-attempted on the NEXT pull. Fields: ``run_id`` / ``exchange`` / ``underlying`` /
#: ``year`` / ``error_type`` / ``defer_seconds`` / ``latched``.
METADATA_EXPIRY_WRITE_DEFERRED_EVENT: Final[str] = "metadata.expiry_cell.write_deferred"

#: WARN — a Phase-1 cell's coverage rollup write blew the store-write deadline. Safe
#: swallow: every cell in the loop came through ``cells_to_pull``, so its persisted
#: status is already non-settled and the cell re-attempts next pull regardless.
METADATA_COVERAGE_WRITE_DEFERRED_EVENT: Final[str] = "metadata.coverage_cell.write_deferred"

#: WARN — a Phase-2 job's registry contract-batch write blew the store-write deadline:
#: the job counts into ``contract_failures``, persisted no contracts, and is rebuilt +
#: re-pulled on the NEXT pull (DEC-PHASE2-REGISTRY-REBUILD-001).
METADATA_CONTRACT_WRITE_DEFERRED_EVENT: Final[str] = "metadata.contract_job.write_deferred"

#: WARN — the post-Phase-2 contract-count rewrite hit the store-write deadline; the
#: rewrite loop BREAKS (the remaining cells would only fail fast on the wedge latch).
#: Fail-safe: an unsettled cell re-pulls + rewrites next run; a previously-``complete``
#: cell keeps a stale display count until recovery (honest envelope).
METADATA_COUNTS_WRITE_DEFERRED_EVENT: Final[str] = "metadata.contract_counts.write_deferred"

#: Max underlyings inlined in the zero-expiry WARNING's sample list — the full count
#: is always logged via ``count``; only the inlined ``sample`` is capped so a
#: pathological all-empty pull can never bloat one log line.
_NO_EXPIRIES_LOG_CAP: Final[int] = 50

#: FNO history floor — Groww FNO data is "available from 2020" (CLAUDE.md §6); the
#: metadata pull mirrors the derivatives pull's floor so the year grid agrees.
GROWW_METADATA_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)

#: The default ``run_id`` stamped on persisted rows when the caller injects none —
#: a stable, honest marker (the dashboard route injects a per-click id).
DEFAULT_METADATA_RUN_ID: Final[str] = "metadata-pull"

#: Factory: ``() -> GrowwClient`` — injected in tests; the default returns the
#: honest empty-chain dry-run client or (live) the credentialed client.
MetadataClientFactory = Callable[[], GrowwClient]

#: ABSOLUTE live-progress hook — ``(completed, total, current)``
#: (DEC-METADATA-LIVE-PROGRESS-001). Fired incrementally as the two phases settle
#: so the ``/universe`` progress bar — which used to freeze at ``0/N`` for the
#: whole metadata pull — advances per (underlying, year) expiry cell (Phase 1) and
#: per contract job (Phase 2). The metadata pull has NO bars (it discards the ~89k
#: option legs per DEC-INSTRUMENT-UNIVERSE-FAST-001 — it persists only the chain
#: SHAPE), so this carries no ``bars`` arg: the absolute ``(completed, total)`` is
#: the whole signal. ``total`` is the count of metadata UNITS — Phase-1 expiry cells
#: plus the Phase-2 contract jobs discovered after Phase 1 (so the route's initial
#: cell-only total is re-scaled the moment Phase 2's job count is known, the spot
#: pull's ledger-resync shape, DEC-SPOT-WINDOW-PROGRESS-001). The route adapts it
#: onto :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.sync_from_ledger` with
#: ``bars=0``.
MetadataProgressHook = Callable[[int, int, str], None]


class _CoverageSink(Protocol):
    """The coverage-ledger rollup surface this module needs (a structural subset).

    :class:`~brutex.data.instrument_store.InstrumentStore` satisfies it in
    production; tests pass a tiny fake. Kept to exactly the two methods used
    (CLAUDE.md §11 COMMON — depend on the minimum) so the metadata pull never
    couples to the full store: ``upsert_coverage`` writes the per-cell rollup the
    ``/coverage`` page reads, and ``list_coverage`` seeds the Phase-1 incremental
    skip.
    """

    def upsert_coverage(self, record: CoverageRecord) -> None:
        """Insert or replace one ``coverage_ledger`` cell (idempotent on the PK)."""
        ...

    def list_coverage(self) -> list[CoverageRecord]:
        """Return every persisted coverage cell."""
        ...


@dataclass(frozen=True, slots=True)
class MetadataPullReport:
    """The honest outcome of one metadata-only expiries+contracts pull.

    Mirrors :class:`~brutex.dashboard.historical_pull.HistoricalPullReport`'s style
    — display + audit — but counts METADATA, never bars (there are none).

    Attributes
    ----------
    dry_run
        ``True`` when the empty-chain dry-run client was used (no creds).
    years
        The calendar years the Phase-1 grid covered.
    underlyings
        The underlying symbols whose chains were enumerated.
    expiry_cells
        Phase-1 cells attempted after the incremental skip.
    cells_skipped
        Phase-1 cells dropped by the incremental skip (immutable past years
        already settled) — ``len(all_cells) - expiry_cells``.
    expiry_failures
        Phase-1 cells whose ``resolve_expiries`` raised (recorded, resumed).
    expiries_persisted
        Total expiry dates upserted into the registry.
    contract_jobs
        Phase-2 ``(exch, underlying, expiry)`` jobs attempted after Phase-2 skip.
    contract_failures
        Phase-2 jobs whose ``resolve_contracts`` raised.
    contracts_persisted
        Total contract legs upserted into the registry.
    note
        An honest one-line outcome note (dry-run / persisted counts).
    store_write_deferrals
        Registry/coverage write batches deferred by the store-write wall-clock
        deadline (DEC-STORE-WRITE-DEADLINE-001) — each deferred unit re-attempts
        on the next pull; a non-zero count fails the pipeline's
        :func:`~brutex.dashboard.pipeline_pull.metadata_phase_ok` gate.
    """

    dry_run: bool
    years: tuple[int, ...]
    underlyings: tuple[str, ...]
    expiry_cells: int
    cells_skipped: int
    expiry_failures: int
    expiries_persisted: int
    contract_jobs: int
    contract_failures: int
    contracts_persisted: int
    note: str
    store_write_deferrals: int = 0

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the metadata-pull route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "years": list(self.years),
            "underlyings": list(self.underlyings),
            "expiry_cells": self.expiry_cells,
            "cells_skipped": self.cells_skipped,
            "expiry_failures": self.expiry_failures,
            "expiries_persisted": self.expiries_persisted,
            "contract_jobs": self.contract_jobs,
            "contract_failures": self.contract_failures,
            "contracts_persisted": self.contracts_persisted,
            "note": self.note,
            "store_write_deferrals": self.store_write_deferrals,
        }


class _EmptyChainSDK:
    """An honest dry-run SDK: the F&O chain is unknown without creds, so it is EMPTY.

    Refuses to fabricate strikes/expiries (Golden Rule #1). It carries ONLY the two
    metadata methods this module ever calls (``get_expiries`` / ``get_contracts``) —
    deliberately NO ``get_historical_candles``, because the metadata pull must never
    reach for a candle even in the dry-run default.
    """

    def get_expiries(self, **_kwargs: object) -> dict[str, object]:
        """No expiries without live creds."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> dict[str, object]:
        """No contracts without live creds."""
        return {"contracts": []}


def _build_dry_run_client() -> GrowwClient:
    """Honest dry-run client: an empty-chain SDK (no fabricated chain, no candles)."""
    return GrowwClient(sdk=_EmptyChainSDK())  # type: ignore[arg-type]


def _build_live_client(  # pragma: no cover - live Groww TOTP token + SDK + network
    *,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
) -> GrowwClient:
    """Build the LIVE credentialed :class:`GrowwClient` from stored TOTP creds.

    LIVE ONLY (network + TOTP); tests inject a fake ``client_factory``. Mirrors the
    historical pull's live-client creds chain
    (:func:`brutex.dashboard.historical_pull._build_live_client`, same rate hooks at
    the one choke point), MINUS the candle sink — the metadata pull has no lake sink
    at all.
    """
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_aws_credentials

    aws_creds = resolve_groww_aws_credentials()
    creds = GrowwCredentials(api_key=aws_creds.api_key, totp_secret=aws_creds.totp_secret)
    return build_live_client(creds, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success)


def _rate_hooks(
    session_rate: SessionRateController | None, *, dry_run: bool
) -> tuple[Callable[[], None] | None, Callable[[], None] | None, Callable[[], None] | None]:
    """Resolve ``(pace, on_rate_limit, on_success)`` from the session rate controller.

    Dry-run or no controller → all ``None`` (no pacing offline). Live with a
    :class:`~brutex.dashboard.pull_controller.SessionRateController` → its single
    persistent controller's hooks, so the 500/min governor spans the whole pull
    (the metadata calls ride the SAME bucket as the spot/derivatives pulls).
    """
    if dry_run or session_rate is None:
        return None, None, None
    controller = session_rate.get()
    return controller.pace, controller.on_rate_limit, controller.on_success


def _resolve_client_factory(
    client_factory: MetadataClientFactory | None,
    *,
    dry_run: bool,
    session_rate: SessionRateController | None,
) -> MetadataClientFactory:
    """Pick the ``() -> GrowwClient`` factory, wiring the live limiter when live.

    An injected *client_factory* (tests) wins untouched. Otherwise dry-run → the
    honest empty-chain client; live → a factory that injects the shared rate
    controller's hooks so the metadata calls can never bypass the limiter.
    """
    if client_factory is not None:
        return client_factory
    if dry_run:
        return _build_dry_run_client
    pace, on_rate_limit, on_success = _rate_hooks(session_rate, dry_run=dry_run)

    def _live() -> GrowwClient:  # pragma: no cover - live network
        return _build_live_client(pace=pace, on_rate_limit=on_rate_limit, on_success=on_success)

    return _live


def _resolve_underlying_forms(
    instruments: Sequence[Instrument],
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], tuple[str, ...]]]:
    """Project instruments to the ``(exchange, F&O underlying)`` grid + a forms map.

    DEC-FNO-UNDERLYING-RESOLVE-001. The Phase-1 ``phase1_cells`` keys on
    ``(exchange, underlying)`` and that ``underlying`` is the symbol Groww's
    ``get_expiries`` / ``get_contracts`` query — so it MUST be the **F&O
    ``underlying_symbol``** the vendor stamps on the contract legs (``BAJAJAUTO`` /
    ``NIFTYNXT50``), NOT the SPOT ``trading_symbol`` (``symbol_name`` = ``BAJAJ-AUTO``
    / ``NIFTYJR``). Passing the spot form made ``get_expiries`` return a
    clean-but-EMPTY ``200`` → 0 expiries → the whole F&O-eligible underlying silently
    missed.

    Returns a 2-tuple ``(grid, forms_by_key)``:

    * **grid** — the de-duplicated ``(exchange, PRIMARY F&O underlying)`` pairs, the
      stable, reproducible coverage/registry KEY (the head of
      :func:`~brutex.dashboard.fno_underlying.fno_underlying_forms`, via the COMMON
      :func:`~brutex.dashboard.fno_underlying.primary_fno_underlying` resolver — the
      sourced index alias, else the case/space/hyphen normalization, else the spot
      spelling). A no-op for an already-canonical symbol (NIFTY / BANKNIFTY / SENSEX),
      so those are byte-unchanged.
    * **forms_by_key** — ``(exchange, primary) -> the FULL ordered forms tuple`` so
      the fetchers can try each fallback form when the primary yields ZERO data (the
      first non-empty wins; each form composes the ``&`` → ``%26`` ampersand fallback
      INSIDE ``resolve_expiries`` / ``resolve_contracts``). A canonical symbol maps to
      a single-element tuple → exactly one SDK call, byte-unchanged.

    Order-preserving, first-wins dedup on the RESOLVED key so the pull order is
    reproducible (CLAUDE.md §4) and two spot spellings that map to the same F&O
    primary underlying are enumerated once (the FIRST spot symbol's forms win the
    map — deterministic by grid order).
    """
    aliases = resolve_fno_index_aliases()
    grid: list[tuple[str, str]] = []
    forms_by_key: dict[tuple[str, str], tuple[str, ...]] = {}
    seen: set[tuple[str, str]] = set()
    for instrument in instruments:
        exchange = str(instrument.exch_id)
        forms = resolve_fno_underlying_forms(instrument.symbol_name)
        pair = (exchange, primary_fno_underlying(instrument.symbol_name, aliases=aliases))
        if pair not in seen:
            seen.add(pair)
            grid.append(pair)
            forms_by_key[pair] = forms
    return grid, forms_by_key


def _first_nonempty_over_forms[T](
    forms: tuple[str, ...],
    fetch: Callable[[str], T],
    *,
    is_empty: Callable[[T], bool],
) -> T:
    """Try each underlying *form* in order; return the FIRST non-empty *fetch* result.

    DEC-FNO-UNDERLYING-RESOLVE-001 — the F&O-underlying spelling axis of the
    empty-retry, the metadata-pull twin of
    :func:`~brutex.adapters.groww.contracts._fetch_first_nonempty_form` (the ``&`` →
    ``%26`` ampersand axis, which composes INSIDE each per-form ``resolve_expiries`` /
    ``resolve_contracts`` call). The PRIMARY form (``forms[0]``) is ALWAYS called; a
    later form is tried ONLY when every prior form was empty, and the LAST form's
    (empty) result is returned when all are empty — so a genuinely-absent chain records
    the SAME empty as before (never a fabricated chain, Golden Rule #1). O(forms):
    exactly ONE call for a canonical single-form symbol (NIFTY / BANKNIFTY / SENSEX —
    byte-unchanged), at most three for a stock needing the alias + normalized + spot
    bridge.
    """
    result = fetch(forms[0])
    for form in forms[1:]:
        if not is_empty(result):
            return result
        result = fetch(form)
    return result


@dataclass(frozen=True, slots=True)
class _CellOutcome:
    """Rolled-up Phase-1 outcome for one ``(exchange, underlying)`` across its cells.

    DEC-FNO-UNDERLYING-RESOLVE-001. Tracks two booleans the zero-expiry audit needs:
    whether ANY of the underlying's pulled expiry cells discovered ≥1 expiry, and
    whether ANY cell ERRORED. An underlying that errored on a cell is NOT flagged
    (the failure is already recorded as ``STATUS_FAILED``); the audit fires only for a
    clean-but-empty whole-underlying miss.
    """

    any_expiry: bool = False
    errored: bool = False

    def merge(self, *, any_expiry: bool, errored: bool) -> _CellOutcome:
        """Return a new outcome OR-ing this cell's result into the accumulator."""
        return _CellOutcome(
            any_expiry=self.any_expiry or any_expiry,
            errored=self.errored or errored,
        )


def underlyings_with_no_expiries(
    cell_outcomes: dict[tuple[str, str], _CellOutcome],
) -> list[tuple[str, str]]:
    """Return the ``(exchange, underlying)`` pairs pulled clean yet with ZERO expiries.

    DEC-FNO-UNDERLYING-RESOLVE-001. The pure, deterministic predicate behind the
    observe-only zero-expiry audit: an underlying is flagged iff it was pulled (it has
    an outcome), it errored on NO cell, AND it discovered NO expiry on any cell. An
    underlying that errored is EXCLUDED (already recorded as a failure, never
    double-counted as a silent miss). Order-preserving over *cell_outcomes*'
    insertion order (the grid order), so the report is reproducible.
    """
    return [
        pair
        for pair, outcome in cell_outcomes.items()
        if not outcome.errored and not outcome.any_expiry
    ]


def _log_underlyings_with_no_expiries(
    cell_outcomes: dict[tuple[str, str], _CellOutcome], *, run_id: str
) -> None:
    """Emit ONE observe-only WARNING when ≥1 underlying came back clean-but-empty.

    DEC-FNO-UNDERLYING-RESOLVE-001 — the metadata-pull twin of the post-fetch
    ``pull.underlying_all_empty`` flag (DEC-SUSPICIOUS-EMPTY-FLAG-001 #919). Names the
    resolved F&O underlyings (capped sample) so a whole F&O-eligible stock can never
    silently vanish. DETECT-only: no return value, no ledger/registry change, no
    auto-recovery. A no-op when nothing is flagged (the common healthy pull).
    """
    flagged = underlyings_with_no_expiries(cell_outcomes)
    if not flagged:
        return
    _LOG.warning(
        METADATA_UNDERLYING_NO_EXPIRIES_EVENT,
        run_id=run_id,
        count=len(flagged),
        sample=[
            f"{exchange}-{underlying}" for exchange, underlying in flagged[:_NO_EXPIRIES_LOG_CAP]
        ],
    )


def _build_note(*, dry_run: bool, expiries_persisted: int, contracts_persisted: int) -> str:
    """Honest one-line outcome note for the report / live panel."""
    if dry_run:
        return (
            "dry-run: set Groww credentials to enumerate + persist the live F&O metadata "
            "(the chain's strikes/expiries are real vendor data, never fabricated offline)"
        )
    return (
        f"persisted {expiries_persisted} expiry date(s) + {contracts_persisted} contract(s) "
        "to the registry (metadata only — no candles; rerun-safe, PK-idempotent)"
    )


def _rewrite_contract_counts(
    coverage_store: _CoverageSink,
    *,
    cells: dict[ExpiryCell, Sequence[date]],
    contract_counts: dict[ContractJob, int],
    run_id: str,
    now: datetime,
) -> int:
    """Re-upsert each Phase-1 coverage cell with its REAL enumerated contract count.

    DEC-METADATA-CONTRACTS-COUNT-001 (year-key correctness fix, DEC-METADATA-CONTRACTS
    -EXPIRY-FOLD-001). Phase 1 wrote every successful cell with ``contracts_pulled=0`` +
    status :data:`STATUS_EXPIRIES_ONLY` (contracts are the separate Phase 2). After Phase
    2 enumerates them, this folds the per-job counts accumulated in *contract_counts*
    (keyed by the FULL ``(exchange, underlying, expiry)`` job — NOT ``expiry.year``) back
    to the cell that DISCOVERED each expiry and rewrites the rollup so the
    ``/completeness`` page shows the REAL integer count instead of the misleading "0 by
    design".

    The fold matches Phase-1's discovery grouping EXACTLY: for each cell
    ``(exchange, underlying, year)`` it sums the counts of every job whose ``expiry`` is
    in THAT cell's own discovered-expiries list (*cells* = ``expiries_seen``). This is the
    correct attribution because ``get_expiries(year=Y)`` is NOT filtered to Y by the
    vendor (``adapters/groww/contracts.resolve_expiries`` passes ``year`` to the SDK but
    never drops returned dates), so a 2025 cell can legitimately surface a 2026-Jan
    expiry; keying the fold by ``expiry.year`` would mis-credit that count to a
    (possibly never-pulled / already-settled-and-skipped) 2026 cell, leaving the 2025
    cell permanently stuck at ``expiries_only contracts_pulled=0`` (``expiries_only`` is
    in :data:`~brutex.orchestrator.universe_phases._SETTLED_STATUSES`, so a past-year cell
    is SKIPPED on re-run and the under-count never self-heals). Folding by the discovered
    expiry is also robust to ``contract_jobs_from``'s cross-cell dedup (a shared expiry's
    single count lands on every cell that discovered it).

    A cell that enumerated ≥1 contract graduates to :data:`STATUS_COMPLETE`; a cell that
    genuinely has no contracts (e.g. a sectoral index) stays ``expiries_only`` (0
    contracts — the honest metadata-only state, the by-design note still reads truthfully).
    Idempotent on the PK (rerun-safe, CLAUDE.md §7); lean-universe compliant — only the
    COUNT is persisted, the legs themselves are never stored
    (DEC-INSTRUMENT-UNIVERSE-FAST-001). O(total discovered expiries).

    Returns the number of DEFERRED rewrite batches (``0`` healthy, ``1`` on a
    store-write-deadline breach — DEC-STORE-WRITE-DEADLINE-001): on the FIRST
    :class:`~brutex.data.errors.StoreWriteDeadlineExceeded` the loop WARNs
    (:data:`METADATA_COUNTS_WRITE_DEFERRED_EVENT`, naming the remaining cells) and
    BREAKS — the remaining cells would only fail fast on the wedge latch. Fail-safe:
    an unsettled cell re-pulls + rewrites next run; a previously-``complete`` cell
    keeps a stale display count until the next successful pull (honest envelope,
    Golden Rule #10).
    """
    items = list(cells.items())
    for index, ((exchange, underlying, year), expiries) in enumerate(items):
        if not expiries:
            # A 0-expiry cell ran but discovered nothing — it has NO contract jobs and
            # Phase 1 already recorded it as ``pending`` (classify_cell). Leave that intact;
            # rewriting it here would wrongly claim ``expiries_only`` for an empty chain.
            continue
        # Sum every job count for an expiry THIS cell discovered — attributing each
        # expiry's contracts to the cell that surfaced it, regardless of the expiry's
        # own calendar year (the vendor does not year-filter; a year=2025 cell can
        # legitimately surface a 2026-Jan expiry).
        count = sum(contract_counts.get((exchange, underlying, expiry), 0) for expiry in expiries)
        status = STATUS_COMPLETE if count > 0 else STATUS_EXPIRIES_ONLY
        try:
            coverage_store.upsert_coverage(
                CoverageRecord(
                    exch_id=exchange,
                    underlying_symbol=underlying,
                    year=year,
                    expiries_pulled=len(expiries),
                    contracts_pulled=count,
                    status=status,
                    last_run_id=run_id,
                    last_fetched_at=now,
                    error_message=None,
                )
            )
        except StoreWriteDeadlineExceeded as exc:
            _LOG.warning(
                METADATA_COUNTS_WRITE_DEFERRED_EVENT,
                run_id=run_id,
                exchange=exchange,
                underlying=underlying,
                year=year,
                cells_remaining=len(items) - index,
                error_type=type(exc).__name__,
                defer_seconds=exc.deadline_seconds,
                latched=exc.latched,
            )
            return 1
    return 0


def pull_expiries_contracts(
    instruments: Sequence[Instrument],
    *,
    registry: RegistryStore,
    coverage_store: _CoverageSink,
    years: Sequence[int],
    dry_run: bool,
    run_parallel: RunParallel | None = None,
    client_factory: MetadataClientFactory | None = None,
    session_rate: SessionRateController | None = None,
    on_progress: MetadataProgressHook | None = None,
    now: datetime | None = None,
    today: date | None = None,
    run_id: str = DEFAULT_METADATA_RUN_ID,
) -> MetadataPullReport:
    """Enumerate + PERSIST F&O expiries + contracts for *instruments* — NO candles.

    Wires the dormant two-phase engine
    (:func:`~brutex.orchestrator.universe_phases.run_two_phase`) to the metadata
    sinks: Phase 1 discovers + persists expiry DATES (registry + an
    ``expiries_only`` coverage rollup), Phase 2 discovers + persists contract
    SYMBOLS (registry). It pulls ZERO historical candles — that stays behind the
    separate "Pull historical" button — so this module constructs no
    :class:`~brutex.data.sink.BarSink` and calls no ``get_historical_candles``.

    The incremental skip (DEC-UNIVERSE-PULL-PHASED-001) means a re-click does only
    NEW work: an immutable PAST year already settled in the coverage ledger is
    skipped (Phase 1); an EXPIRED expiry whose contracts are already persisted is
    skipped (Phase 2). The current year + not-yet-expired expiries always re-pull
    (new expiries/strikes appear as time passes). A skipped cell's PERSISTED
    expiries still seed the Phase-2 worklist (DEC-PHASE2-REGISTRY-REBUILD-001), so
    contracts that FAILED in an earlier run are rebuilt + re-pulled even though
    their settled ``expiries_only`` cell itself is never re-fetched.

    Parameters
    ----------
    instruments
        The underlyings whose chains to enumerate (indices and/or F&O stocks); the
        ``(exch_id, symbol_name)`` pairs form the Phase-1 grid.
    registry
        The metadata sink — expiry dates + contract symbols (idempotent on its PKs).
    coverage_store
        The per-``(underlying, year)`` rollup sink (``upsert_coverage`` +
        ``list_coverage``) so the ``/coverage`` page keeps working; its
        ``list_coverage`` also seeds the Phase-1 incremental skip.
    years
        Calendar years to cover (e.g. ``2020 → today``).
    dry_run
        When ``True`` (and no ``client_factory``), the honest empty-chain client is
        used (0 expiries, 0 contracts) — never a fabricated strike.
    run_parallel
        The concurrency mapper for BOTH phases; ``None`` selects the production
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`. Tests inject a
        synchronous fake so the wiring is provable with zero threads.
    client_factory
        Injected ``() -> GrowwClient`` (tests pass a fake SDK); ``None`` selects the
        dry-run / live default (live wires the session rate hooks).
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    on_progress
        ABSOLUTE live-progress hook ``(completed, total, current)``
        (DEC-METADATA-LIVE-PROGRESS-001) — fired once per Phase-1 expiry cell as it
        settles, once at the phase boundary to re-scale ``total`` from cells-only to
        ``cells + contract_jobs`` (so the bar tracks the REAL Phase-2 work the moment
        it is known), and once per Phase-2 contract job. ``None`` (the default, and
        every non-dashboard caller) is a silent no-op — progress is opt-in, so the
        engine's behaviour is byte-unchanged when no hook is wired. Carries no bars
        arg (this pull persists only the chain SHAPE, never candles).
    now
        Injected clock for the persisted ``fetched_at`` timestamp (defaults to
        ``datetime.now(UTC)``).
    today
        Injected date for the Phase-2 expired-expiry skip + the Phase-1 current-year
        cutoff (defaults to ``now``'s UTC date).
    run_id
        The reproducibility id stamped on every persisted row (defaults to
        :data:`DEFAULT_METADATA_RUN_ID`; the route injects a per-click id).

    Returns
    -------
    MetadataPullReport
        Honest metadata counts (expiries/contracts persisted, cells skipped,
        failures) — never a bar count (there are none).
    """
    now = now or datetime.now(UTC)
    today = today or now.astimezone(UTC).date()
    if run_parallel is None:
        run_parallel = PoolRunParallel()
    client = _resolve_client_factory(client_factory, dry_run=dry_run, session_rate=session_rate)()
    # Register the metadata client's shared MEASURED-rate meter into the session controller
    # (DEC-RATE-WIDGET-FNO-MEASURED-001) so the observe-only RatePanelSampler surfaces the REAL
    # completed-rate during the metadata (get_expiries / get_contracts) phase too, not the
    # admission average. Best-effort + observe-only — a dry-run / no-``session_rate`` pull, or
    # an empty-chain client with no ``.meter``, is a silent no-op. Lazy import avoids any
    # module-load ordering coupling with pull_controller.
    from brutex.dashboard.pull_controller import register_pull_meter

    register_pull_meter(session_rate, client)

    grid, forms_by_key = _resolve_underlying_forms(instruments)
    all_cells = phase1_cells(grid, years)
    expiry_skip = settled_expiry_cells(
        (rec.exch_id, rec.underlying_symbol, rec.year, rec.status)
        for rec in coverage_store.list_coverage()
    )
    cells = cells_to_pull(all_cells, settled=expiry_skip, current_year=today.year)
    contract_skip = settled_contract_jobs(registry.list_contract_jobs())
    # Phase-2 re-pull safety net (DEC-PHASE2-REGISTRY-REBUILD-001): rebuild the contract
    # worklist from the REGISTRY's persisted expiries as well as this run's in-memory
    # discoveries. A cell settled ``expiries_only`` in an EARLIER run is skipped by the
    # Phase-1 incremental skip, so its expiries never enter ``expiries_seen`` — and when
    # that run's Phase-2 contract jobs FAILED, the failed contracts had NO re-pull path
    # (the year was lost forever). The persisted expiries give those jobs back;
    # ``contract_jobs_to_pull`` still drops every already-captured expired job
    # (``contract_skip`` is built from persisted CONTRACTS, which a failed job never
    # wrote), so a healthy re-run issues zero extra calls. Grid-restricted so a
    # selected-symbols pull never resurrects another underlying's jobs; one O(persisted
    # expiries) read per pull (the pull itself is network-bound).
    grid_pairs = frozenset(grid)
    persisted_jobs: tuple[ContractJob, ...] = tuple(
        (row.exch, row.underlying, row.expiry_date)
        for row in registry.list_expiries()
        if (row.exch, row.underlying) in grid_pairs
    )

    # Per-cell expiry tally so the coverage rollup carries the real discovered count
    # (the classify_cell rule keys completeness off expiries_pulled).
    expiries_persisted = 0
    contracts_persisted = 0
    # Registry/coverage write batches deferred by the store-write wall-clock deadline
    # (DEC-STORE-WRITE-DEADLINE-001) — surfaced on the report so the pipeline's
    # metadata gate goes honestly red on a wedged store volume.
    store_write_deferrals = 0
    # Per-EXPIRY enumerated CONTRACT count (DEC-METADATA-CONTRACTS-COUNT-001, keyed by the
    # FULL ``(exchange, underlying, expiry)`` job — NOT ``expiry.year``; the year-key
    # correctness fix). Phase 2 enumerates the contracts per expiry; ``_rewrite_contract_
    # counts`` then folds each expiry's count into the cell that DISCOVERED it (matching
    # Phase-1's grouping), so the cell records the REAL count even when a year=Y cell
    # surfaced an expiry dated in Y+1 (the vendor does not year-filter expiries).
    # Lean-universe compliant (DEC-INSTRUMENT-UNIVERSE-FAST-001): only the integer COUNT is
    # persisted, never the ~89k legs (still discarded).
    contract_counts: dict[ContractJob, int] = {}

    # ── Live progress (DEC-METADATA-LIVE-PROGRESS-001) ───────────────────────
    # Drive the /universe bar — which used to freeze at 0/N for the whole metadata
    # pull — off the SAME two callbacks run_two_phase already fires. ``_completed``
    # ticks once per settled Phase-1 expiry cell, then once per settled Phase-2
    # contract job; ``_total`` starts cells-only and is re-scaled to
    # ``cells + contract_jobs`` at the phase boundary (computed by REUSING the
    # engine's own contract_jobs_from / contract_jobs_to_pull — CLAUDE.md §11 COMMON,
    # so the count matches the jobs run_two_phase then runs bit-for-bit). The hook is
    # ABSOLUTE (completed, total, current) — the route maps it onto sync_from_ledger,
    # the spot pull's exact resync shape (DEC-SPOT-WINDOW-PROGRESS-001).
    progress_total = len(cells)
    progress_completed = 0
    expiries_seen: dict[ExpiryCell, Sequence[date]] = {}
    cells_observed = 0
    # Per-cell Phase-1 outcome for the zero-expiry audit (DEC-FNO-UNDERLYING-RESOLVE-001):
    # ``(exchange, underlying) -> CellOutcome`` rolled up across that underlying's pulled
    # cells (any expiry discovered? any cell errored?). The audit fires for an underlying
    # that was pulled, errored on NO cell, yet discovered 0 expiries on every cell — the
    # silent-vanish signal a wrong F&O underlying form used to produce.
    cell_outcomes: dict[tuple[str, str], _CellOutcome] = {}

    def _emit_progress(current: str) -> None:
        """Fire *on_progress* with the absolute ``(completed, total, current)`` — no-op when unset."""
        if on_progress is not None:
            on_progress(progress_completed, progress_total, current)

    def _rescale_to_phase2() -> None:
        """At the Phase-1→2 boundary, grow ``total`` to ``cells + the real job count``.

        Reuses the engine's ``contract_jobs_from`` + ``contract_jobs_to_pull`` (the
        SAME derivation+skip ``run_two_phase`` applies — including the persisted-expiry
        union of DEC-PHASE2-REGISTRY-REBUILD-001) so the Phase-2 denominator is
        exactly the jobs it will run — never an estimate (CLAUDE.md §11 COMMON).
        """
        nonlocal progress_total
        jobs = contract_jobs_to_pull(
            contract_jobs_from(dict(expiries_seen), persisted=persisted_jobs),
            settled=contract_skip,
            today=today,
        )
        progress_total = len(cells) + len(jobs)

    def _fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
        """Phase-1 fetcher: resolve one cell's expiry dates (metadata only).

        Tries the cell's PRIMARY F&O underlying form first and, only if it returns
        ZERO expiries, the resolver's fallback forms in order — first non-empty wins
        (DEC-FNO-UNDERLYING-RESOLVE-001). Each form composes the ``&`` → ``%26``
        ampersand fallback inside :func:`resolve_expiries`. A canonical symbol carries
        one form → exactly one call, byte-unchanged.
        """
        exchange, underlying, year = cell
        forms = forms_by_key.get((exchange, underlying), (underlying,))
        expiries = _first_nonempty_over_forms(
            forms,
            lambda form: resolve_expiries(
                client, exchange=exchange, underlying_symbol=form, year=year
            ),
            is_empty=lambda r: not r,
        )
        # Persist HERE, in the fetcher — the exact Phase-2 shape (its fetcher
        # persists too) — as ONE deadline-guarded registry batch
        # (DEC-STORE-WRITE-DEADLINE-001). A wedged registry raises the TRANSIENT
        # :class:`~brutex.data.errors.StoreWriteDeadlineExceeded` out of the
        # FETCHER, which the engine captures per cell (record-and-resume): the
        # cell is recorded FAILED (never settled), derives NO Phase-2 jobs (no
        # vendor calls against unpersisted expiries), and re-attempts next pull.
        registry.upsert_expiries(
            exchange, underlying, list(expiries), run_id=run_id, fetched_at=now
        )
        return expiries

    def _on_expiry_cell(
        cell: ExpiryCell, expiries: Sequence[date], exc: BaseException | None
    ) -> None:
        """Record each cell's outcome + write the per-cell coverage rollup.

        The registry expiry persistence happens in the FETCHER
        (:func:`_fetch_expiries` — the Phase-2 shape), so a store-write-deadline
        breach arrives HERE as the cell's ``exc`` (DEC-STORE-WRITE-DEADLINE-001):
        the cell records ``failed`` (never settled — the settle-correctness rule:
        a registry-deferred cell settling would skip it next pull while the
        registry lacks its expiries, a permanent loss), contributes NO Phase-2
        jobs, and re-attempts next pull. This callback runs on the DRAINING
        thread, so its OWN coverage write is also caught below — a raise here
        would fail the WHOLE pull.
        """
        nonlocal expiries_persisted, progress_completed, cells_observed, store_write_deferrals
        exchange, underlying, year = cell
        if isinstance(exc, StoreWriteDeadlineExceeded):
            # The cell's expiry BATCH write blew the store-write deadline (or hit
            # the wedge latch): a per-cell DEFERRAL, loudly counted — never a
            # pull-wide crash/freeze.
            store_write_deferrals += 1
            _LOG.warning(
                METADATA_EXPIRY_WRITE_DEFERRED_EVENT,
                run_id=run_id,
                exchange=exchange,
                underlying=underlying,
                year=year,
                error_type=type(exc).__name__,
                defer_seconds=exc.deadline_seconds,
                latched=exc.latched,
            )
        if exc is None:
            expiries_persisted += len(expiries)
        # Roll this cell's outcome into the per-underlying zero-expiry audit accumulator
        # (DEC-FNO-UNDERLYING-RESOLVE-001).
        prior = cell_outcomes.get((exchange, underlying), _CellOutcome())
        cell_outcomes[(exchange, underlying)] = prior.merge(
            any_expiry=bool(expiries), errored=exc is not None
        )
        # Live progress: a successful cell contributes its expiries to the Phase-2
        # worklist; tick the bar per cell and, when the LAST cell settles, re-scale
        # ``total`` to include the now-known contract jobs (DEC-METADATA-LIVE-PROGRESS-001).
        if exc is None:
            expiries_seen[cell] = expiries
        progress_completed += 1
        cells_observed += 1
        if cells_observed >= len(cells):
            _rescale_to_phase2()
        _emit_progress(f"{exchange}-{underlying} {year} expiries")
        error_message = None if exc is None else (str(exc) or repr(exc))
        # A failed cell ⇒ classify_cell → "failed"; a success with ≥1 expiry ⇒
        # "expiries_only" (captured but contracts are the separate Phase 2 here);
        # a success with 0 expiries ⇒ classify_cell → "pending" (ran, found none).
        # A write-DEFERRED cell arrives with ``exc`` set + ``expiries == []`` ⇒
        # classify_cell(0, 0, error) → "failed" (NEVER settled; an honest 0 —
        # nothing durably persisted).
        status = (
            STATUS_EXPIRIES_ONLY
            if (exc is None and expiries)
            else classify_cell(len(expiries), 0, error_message)
        )
        try:
            coverage_store.upsert_coverage(
                CoverageRecord(
                    exch_id=exchange,
                    underlying_symbol=underlying,
                    year=year,
                    expiries_pulled=len(expiries),
                    contracts_pulled=0,
                    status=status,
                    last_run_id=run_id,
                    last_fetched_at=now,
                    error_message=error_message,
                )
            )
        except StoreWriteDeadlineExceeded as deferred:
            store_write_deferrals += 1
            _LOG.warning(
                METADATA_COVERAGE_WRITE_DEFERRED_EVENT,
                run_id=run_id,
                exchange=exchange,
                underlying=underlying,
                year=year,
                error_type=type(deferred).__name__,
                defer_seconds=deferred.deadline_seconds,
                latched=deferred.latched,
            )
            # Safe swallow: every cell in this loop came through ``cells_to_pull``,
            # so its PERSISTED status is already non-settled — the cell re-attempts
            # next pull regardless of this missing rollup row.

    def _fetch_contracts(job: ContractJob) -> int:
        """Phase-2 fetcher: resolve + persist one expiry's contracts (metadata only).

        Like :func:`_fetch_expiries`, tries the PRIMARY F&O underlying form first and
        the resolver's fallback forms only on a ZERO-contract reply — first non-empty
        wins (DEC-FNO-UNDERLYING-RESOLVE-001). The persisted registry ``underlying`` is
        always the PRIMARY (Phase-1) key, so the registry stays keyed consistently
        regardless of which form answered the vendor.
        """
        nonlocal contracts_persisted
        exchange, underlying, expiry = job
        forms = forms_by_key.get((exchange, underlying), (underlying,))
        contracts = _first_nonempty_over_forms(
            forms,
            lambda form: resolve_contracts(
                client, exchange=exchange, underlying_symbol=form, expiry=expiry
            ),
            is_empty=lambda r: not r,
        )
        # ONE deadline-guarded write BATCH for the whole leg list (O(1) guard cost
        # per job, not per contract — DEC-STORE-WRITE-DEADLINE-001). NO catch here:
        # the raise IS the defer — PoolRunParallel captures it per job, the job
        # counts into ``contract_failures``, persisted no contracts, and is rebuilt
        # + re-pulled on the NEXT pull (DEC-PHASE2-REGISTRY-REBUILD-001).
        contracts_stored = registry.upsert_contracts(
            exchange,
            underlying,
            expiry,
            [(contract.symbol, contract.contract_type, contract.strike) for contract in contracts],
            run_id=run_id,
            fetched_at=now,
        )
        contracts_persisted += contracts_stored
        return contracts_stored

    def _on_contract(job: ContractJob, count: int, exc: BaseException | None) -> None:
        """Per-job hook — the persistence already happened in the fetcher.

        ``run_two_phase`` separates the fetch (which persists) from this result
        callback; the counts are accumulated in the fetcher, so this is the audit
        seam. Here it ALSO ticks the live bar once per settled Phase-2 contract job
        (DEC-METADATA-LIVE-PROGRESS-001) AND records the per-job enumerated contract
        *count* keyed by the FULL ``(exchange, underlying, expiry)`` job
        (DEC-METADATA-CONTRACTS-COUNT-001) — ``_rewrite_contract_counts`` later folds each
        expiry's count into the cell that DISCOVERED it. A failed job (``exc`` set)
        contributes nothing. A contract job exists ONLY because Phase 1 already discovered
        + settled its expiry cell, so ``total`` is always already re-scaled to
        ``cells + jobs`` by the time the first job lands here.
        """
        nonlocal progress_completed, store_write_deferrals
        progress_completed += 1
        exchange, underlying, expiry = job
        _emit_progress(f"{exchange}-{underlying} {expiry} contracts")
        if isinstance(exc, StoreWriteDeadlineExceeded):
            # The job's contract-batch write blew the store-write deadline
            # (DEC-STORE-WRITE-DEADLINE-001): already counted into
            # ``contract_failures`` by the engine; surface the DEFERRAL loudly +
            # on the report (the pipeline gate goes red; the job re-pulls next run).
            store_write_deferrals += 1
            _LOG.warning(
                METADATA_CONTRACT_WRITE_DEFERRED_EVENT,
                run_id=run_id,
                exchange=exchange,
                underlying=underlying,
                expiry=expiry.isoformat(),
                error_type=type(exc).__name__,
                defer_seconds=exc.deadline_seconds,
                latched=exc.latched,
            )
        if exc is None:
            # Key by the FULL expiry, NOT ``expiry.year``: ``get_expiries(year=Y)`` is not
            # year-filtered by the vendor, so a year=Y cell can surface an expiry dated in
            # Y+1. ``_rewrite_contract_counts`` attributes this count to whichever cell's
            # discovered-expiries list contains this expiry (Phase-1's grouping exactly),
            # which keys-by-year would mis-credit to a never-pulled / skipped Y+1 cell.
            # Accumulate the integer count only — the legs themselves are persisted by the
            # fetcher then conceptually discarded from the lean universe.
            contract_counts[job] = contract_counts.get(job, 0) + count

    # A run whose EVERY Phase-1 cell was skipped can still carry Phase-2 work rebuilt
    # from the registry's persisted expiries (DEC-PHASE2-REGISTRY-REBUILD-001); with
    # zero cells Phase 1 never reaches the boundary rescale, so seed the progress total
    # up-front (an empty rebuilt worklist keeps it 0 — the bar stays honestly at 0/0).
    if not cells:
        _rescale_to_phase2()

    report = run_two_phase(
        cells,
        fetch_expiries=_fetch_expiries,
        fetch_contracts=_fetch_contracts,
        run_parallel=run_parallel,
        on_expiry_cell=_on_expiry_cell,
        on_contract=_on_contract,
        contract_skip=contract_skip,
        persisted_jobs=persisted_jobs,
        today=today,
    )

    # Phase 2 done — rewrite each cell's coverage rollup with the REAL enumerated contract
    # count (DEC-METADATA-CONTRACTS-COUNT-001). The Phase-1 record wrote ``contracts_pulled=0``
    # (the by-design metadata-only placeholder); now that contracts have been enumerated, the
    # cell carries the real integer count and — once ≥1 contract is in — graduates from
    # ``expiries_only`` to ``complete`` (a chain with no enumerated contracts stays
    # ``expiries_only``, so the "metadata-only by design" note still reads honestly).
    store_write_deferrals += _rewrite_contract_counts(
        coverage_store,
        cells=expiries_seen,
        contract_counts=contract_counts,
        run_id=run_id,
        now=now,
    )

    # Observe-only: WARN about any underlying that was pulled, errored on no cell, yet
    # discovered ZERO expiries on every cell — the silent-vanish signal a wrong F&O
    # underlying form used to produce (DEC-FNO-UNDERLYING-RESOLVE-001).
    _log_underlyings_with_no_expiries(cell_outcomes, run_id=run_id)

    note = _build_note(
        dry_run=dry_run,
        expiries_persisted=expiries_persisted,
        contracts_persisted=contracts_persisted,
    )
    return MetadataPullReport(
        dry_run=dry_run,
        years=tuple(years),
        underlyings=tuple(underlying for _exch, underlying in grid),
        expiry_cells=report.expiry_cells,
        cells_skipped=len(all_cells) - len(cells),
        expiry_failures=report.expiry_failures,
        expiries_persisted=expiries_persisted,
        contract_jobs=report.contract_jobs,
        contract_failures=report.contract_failures,
        contracts_persisted=contracts_persisted,
        note=note,
        store_write_deferrals=store_write_deferrals,
    )
