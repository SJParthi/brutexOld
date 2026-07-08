"""Bulk Greeks v1→v2 re-enrichment backfill (DEC-GREEKS-BULK-REENRICH-001).

Closes the two DEC-GREEKS-CORRECTNESS-AUDIT-001 (MIGRATION.md #1188) named
follow-ups: (a) EXPIRED option contracts frozen at ``GREEKS_FORMULA_VERSION=1``
(the pull-side enrich pass reaches only contracts inside a LIVE pull window),
and (b) a CLI-``brutex fetch``-only lake that is never Greeks-enriched at all.
Per ledger #1071's pre-answer the surface is **stamp-driven / incremental**: a
target-DISCOVERY + batching layer feeding the EXISTING
:func:`brutex.data.greeks_enrich.enrich_greeks_for_targets` driver UNMODIFIED —
its value-checked reuse gate + the honest NO_RATE / NO_SPOT / BELOW_INTRINSIC /
BAD_INPUT / AT_EXPIRY demotions do ALL the correctness work here; a Greek is
NEVER fabricated and the driver logic is never duplicated (CLAUDE.md §11 COMMON).

## The three stages

1. :func:`scan_stale_greeks_targets` — enumerate the lake's F&O 1-minute option
   datasets (manifest-first via
   :func:`brutex.dashboard.db_view.list_fno_candle_datasets`), probe each
   contract's stored ``greeks_provenance_id`` column, and classify it STALE
   (unstamped / v1 / inputs-drift via the ONE exported predicate
   :func:`brutex.data.greeks_enrich.stamp_is_current_inputs`) or current.
2. :func:`plan_greeks_backfill` — freeze the sorted target list under a
   content-addressed ``backfill_id`` so a stale checkpoint self-ignores.
3. :func:`run_greeks_backfill` — bounded batches through the existing driver,
   with a DURABLE advisory checkpoint
   (:class:`brutex.engine.sweep_checkpoint.SweepCheckpointStore` reused
   verbatim — atomic ``tmp→fsync→os.replace`` writes via the COMMON
   :mod:`brutex.data.atomic_io`) so a crash resumes from the last completed
   batch instead of restarting.

## Honest complexity + resume envelope (Golden Rule #10)

* The SCAN is **O(files)** — one bounded column read per month partition —
  NEVER O(1) (the lake_heal #1175 precedent wording); enumeration is
  O(manifest-rows) with a graceful O(files) walk fallback.
* O(1) claims that ARE true: per-contract dedup membership (a set), the
  staleness predicate per stamp value (two int ops after one constant pack),
  the per-batch checkpoint ``record`` (a small-JSON atomic write behind a
  monotonic compare), and the per-bar reuse decision inside the driver.
* TRUE idempotence is owned by the driver's stamp+value reuse gate + the
  ``latest-wins`` byte-identical restate (``stats.changed == 0`` ⇒ no write);
  the checkpoint is a resume ACCELERATOR only (the ``tuple_complete``
  advisory-cache discipline). In DEFAULT incremental mode the STAMPS are the
  usual resume mechanism — a crash-restart re-scans, completed contracts no
  longer classify stale, the plan shrinks, the ``backfill_id`` changes and the
  stale checkpoint is ignored. THE ERRORED-PREFIX EXCEPTION (fix round 3,
  refuter-confirmed): an ERRORED contract keeps its stale stamps (the driver's
  per-contract quarantine), so a recorded prefix containing only errored
  contracts reproduces the IDENTICAL plan — same ``backfill_id`` — and a
  checkpoint recorded past it would make the resume SKIP those contracts
  silently and report a false-clean ``errored=0`` run. The runner therefore
  FREEZES the durable high-water mark at the last fully-clean prefix boundary
  (``record`` fires only while the run's CUMULATIVE ``errored == 0``): a
  crash-resume always re-feeds every errored contract, cheaply restating any
  clean ones redone alongside. In ``force`` mode the plan is stamp-INdependent
  and stable, so the checkpoint genuinely resumes at ``next_index`` — under
  the same clean-prefix-only guarantee AND only for the SAME rate VALUES: the
  ``backfill_id`` binds a rate-source VALUE fingerprint (fix round 4,
  refuter-confirmed — a force run that crashed under rate A, resumed after the
  operator revised the fixed rate / the ``BRUTEX_RISK_FREE_CSV`` values, would
  otherwise ADOPT the old checkpoint and exempt the recorded prefix from the
  driver's value-checked gate — the exact drift class ``force`` exists to
  catch — ending in a false-clean green run), so ANY rate-value change re-keys
  the plan and the stale checkpoint self-ignores.
* BLIND SPOT, stated everywhere: the stamp prefilter cannot see a rate/spot
  VALUE drift under an unchanged ``(rate_source_id, RISK_FREE_SOURCE_VERSION)``
  — ``force=True`` routes every contract through the driver's VALUE-checked
  gate, which catches exactly that class. Honest residue (Golden Rule #10): a
  SPOT-lake mutation (e.g. a DEC-LAKE-HEAL-001 heal) landing in the narrow
  window between a force run's crash and its resume is NOT representable in
  the ``backfill_id`` (no cheap spot-content token exists), so the resumed
  prefix would not be re-gated against the healed spot — delete
  ``<lake>/.greeks_backfill/checkpoint.json`` (or let the force run complete
  afresh) after a heal in that window to guarantee full coverage.
* A STALE-stamped bar the driver CANNOT act on — a stored row with a
  non-finite OHLC value, which the driver's per-bar IVX-2 guard SKIPS without
  a write — is classified ``stale_unfixable`` whether the stale stamp is NULL
  (fix round 4, refuter-confirmed) or a NON-null v1/inputs-drift value sitting
  on the skippable row itself (fix round 6, refuter-confirmed — the null-only
  gate's sibling escape: the driver skipped the row BEFORE its reuse gate,
  ``changed == 0`` read ``already_current``, the stale stamp persisted, and
  the contract was re-targeted on every run under a ✅/EXIT_OK false green):
  counted + logged + surfaced on the report/Telegram honesty heads, NEVER
  targeted, so the census converges instead of re-feeding the contract forever
  while the driver honestly reports it ``already_current``. Repairing the
  poisoned row is a lake-repair task (``lake_heal`` is strictly row-removing,
  never value-editing — a manual partition fix is required), stated, not
  hidden.
* The scanner probes ONLY month files the driver's reader can also open —
  canonical zero-padded ``<yyyy>/<mm>.parquet`` names (fix round 5,
  refuter-confirmed): an in-range NON-canonical name (``2024/1.parquet``)
  passed the round-3 range guard yet is invisible to
  ``ist_span_partition_files``'s zero-padded path construction, so the
  contract was re-targeted on EVERY run while the wet run wrote nothing and
  read green. Such a file now folds into ``probe_failed`` + ONE loud WARN
  (repair = rename to the canonical form), never an eternal re-queue.
* No wall-clock SLA is claimed — the first wet pass is O(stale bars) kernel
  compute + O(changed partitions) latest-wins rewrites.

Engine scope impact: the brute-force ENGINE scope (NIFTY/BANKNIFTY/SENSEX) is
untouched and this module only IMPORTS the cycle-free checkpoint store — but
honesty (Golden Rule #10) requires naming the one engine-visible change of
this change set: :mod:`brutex.engine.sweep_checkpoint` gained fail-loud
corruption refusals (1 MiB size cap, ``RecursionError`` nesting-bomb mapping,
same-id ``total_combos`` mismatch refused, parent-mkdir inside the error
mapping — ledger #1558 fix round 2; the ``int()``-coercion ``OverflowError``
of a JSON ``1e999``/``Infinity`` int field mapped alongside
``ValueError``/``TypeError`` — fix round 6) that ALSO harden the sweep's own resume
path (``sweep_supervisor``/``sweep_parallel`` call ``load()`` uncaught, so a
corrupt/tampered checkpoint now aborts loudly where it previously resumed
silently). The F&O lake stays REFERENCE_ONLY; spot INDEX partitions are never
written.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    GreeksEnrichReport,
    GreeksEnrichTarget,
    enrich_greeks_for_targets,
    rate_source_id_for,
    stamp_is_current_inputs,
    unpack_greeks_provenance,
)
from brutex.engine.sweep_checkpoint import SweepCheckpointError, SweepCheckpointStore
from brutex.observability.progress_beat import emit_progress_beat
from brutex.options.risk_free import (
    RISK_FREE_SOURCE_VERSION,
    CsvRateSource,
    FixedRateSource,
)

if TYPE_CHECKING:
    from brutex.data.fno_symbol import FnoSymbolParts
    from brutex.options.risk_free import RiskFreeSource

_LOG = structlog.get_logger("brutex.data.greeks_backfill")

__all__ = [
    "BACKFILL_BATCH_ENV",
    "BACKFILL_PAUSE_ENV",
    "CHECKPOINT_DIR_NAME",
    "CHECKPOINT_FILE_NAME",
    "DEFAULT_BACKFILL_BATCH_SIZE",
    "REVISION_MARKER_FILE_NAME",
    "BackfillPlan",
    "BackfillProgress",
    "BackfillScan",
    "GreeksBackfillReport",
    "checkpoint_path",
    "dry_run_backfill_report",
    "plan_greeks_backfill",
    "resolve_backfill_batch_size",
    "resolve_backfill_pause_s",
    "revision_marker_path",
    "run_greeks_backfill",
    "scan_stale_greeks_targets",
]

#: Env var overriding the per-batch contract count. FAIL-LOUD contract
#: (the DEC-RAM-BUDGETS-001 #1360 env convention): a non-integer or ``< 1``
#: value raises :class:`ValueError` — never a silent fallback.
BACKFILL_BATCH_ENV: Final[str] = "BRUTEX_GREEKS_BACKFILL_BATCH"

#: Env var naming an inter-batch pause in seconds (the lake_heal L3 gentleness
#: idiom). FAIL-LOUD: a non-float or negative value raises :class:`ValueError`.
BACKFILL_PAUSE_ENV: Final[str] = "BRUTEX_GREEKS_BACKFILL_PAUSE_S"

#: Default contracts per batch — one checkpoint write + one progress tick per
#: batch; memory stays O(batch) live target windows.
DEFAULT_BACKFILL_BATCH_SIZE: Final[int] = 25

#: The checkpoint sidecar's dot-directory under the lake root — OUTSIDE
#: ``bars/`` + ``options/`` like ``.locks`` / ``.lake_heal``, so no lake
#: scanner ever sees it.
CHECKPOINT_DIR_NAME: Final[str] = ".greeks_backfill"

#: The checkpoint JSON file name inside :data:`CHECKPOINT_DIR_NAME`.
CHECKPOINT_FILE_NAME: Final[str] = "checkpoint.json"

#: The durable cross-process FRESHNESS marker inside :data:`CHECKPOINT_DIR_NAME`
#: (DEC-GREEKS-BULK-REENRICH-001 fix round 2). The backfill rewrites option
#: 1-minute partitions but never touches the window ledger, so the
#: :func:`brutex.dashboard.lake_revision.lake_revision` token — which a running
#: dashboard's SummaryCache probes for cross-process invalidation
#: (DEC-HARDENING-LEFTOVERS-001) — never moved on a CLI ``brutex
#: greeks-backfill``: ``/movers`` kept serving the PRE-backfill iv/delta the
#: backfill was run to fix, until an unrelated pull or a restart. Every WET
#: batch that wrote bars now touches this marker (atomic tmp→``os.replace``),
#: and ``lake_revision`` folds one ``os.stat`` of it into the composite token —
#: the dashboard sees the new Greeks on its next poll. The literals are
#: duplicated in ``lake_revision`` (that module is deliberately lean
#: stdlib-only — the ``_SWEEPS_DIRNAME`` precedent) and pinned equal by test.
REVISION_MARKER_FILE_NAME: Final[str] = "revision.json"

#: The one Greeks-bearing interval (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — derived
#: TFs never carry the overlay and are deliberately never scanned.
_MINUTE_INTERVAL: Final[str] = "1minute"

#: Fixed +05:30 IST (no DST) — the expiry-comparison calendar.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

#: December — the year-wrap month for the contract-window derivation and the
#: upper bound of the valid month-file range (one COMMON constant, ruff PLR2004).
_DECEMBER: Final[int] = 12

#: The per-contract staleness verdicts the probe can return.
_STALE_NULL: Final[str] = "null"
_STALE_V1: Final[str] = "v1"
_STALE_DRIFT: Final[str] = "drift"

#: The four physical price columns the driver's per-bar IVX-2 guard inspects —
#: the probe's fixability check mirrors EXACTLY that predicate (fix round 4).
_PRICE_COLUMNS: Final[tuple[str, str, str, str]] = ("open", "high", "low", "close")

#: The runner's per-batch progress hook — ABSOLUTE counts, O(1) per batch:
#: ``(completed_targets, total_targets, bars_written_cum, errored_cum, label)``.
BackfillProgress = Callable[[int, int, int, int, str], None]


@dataclass(frozen=True, slots=True)
class BackfillScan:
    """One scan pass's verdict — the stale target list + the honest census.

    Attributes
    ----------
    targets
        The STALE option contracts, one full-life
        :class:`~brutex.data.greeks_enrich.GreeksEnrichTarget` each, sorted
        deterministically by ``(underlying, exchange, symbol)`` (the checkpoint
        precondition; adjacent CE/PE chains keep the driver's spot memo warm).
    stale_null
        Contracts with at least one UNSTAMPED bar (a ``NULL``
        ``greeks_provenance_id`` or a legacy 7-column partition).
    stale_v1
        Contracts whose first stale stamp carries a formula version older than
        :data:`~brutex.data.greeks_enrich.GREEKS_FORMULA_VERSION`.
    stale_inputs_drift
        Contracts stale on a NON-formula input bit (rate source / rate version
        / q bias / TTE convention drift).
    stale_unfixable
        Contracts whose ONLY staleness is null-stamped rows the driver CANNOT
        act on (a non-finite OHLC value — the per-bar IVX-2 skip class): NEVER
        targeted (the driver would honestly report them ``already_current``
        forever while the null stamp persisted — the fix-round-4 refuter-
        confirmed non-convergence), counted + logged + surfaced on the
        report/Telegram honesty heads instead. Repair is a lake-repair task
        (``lake_heal`` is strictly row-removing, never value-editing).
    current
        Contracts whose every probed stamp already matches the current inputs
        — skipped, no target built.
    futures_skipped
        FUT datasets skipped by design (never a Greek for a future).
    undecodable
        Datasets whose directory name failed
        :func:`~brutex.data.fno_symbol.parse_fno_symbol` (counted, never a crash).
    live_skipped
        Option contracts skipped by ``expired_only=True`` because their expiry
        is on/after *today* (an honest tally, never a silent drop).
    no_files
        Decoded option contracts whose ``1minute`` tree held no month files —
        nothing to probe or enrich.
    probe_failed
        Month files the backfill could not act on: corrupt Parquet / I/O
        error, PLUS (fix round 5) in-range NON-canonical month filenames the
        driver's zero-padded reader can never open (``2024/1.parquet``) —
        each skipped + logged, never targeted; repair is
        :mod:`brutex.data.lake_heal`'s job (or a rename to the canonical
        zero-padded form for the non-canonical class).
    files_probed
        Month files whose stamp column was actually read (the O(files) cost,
        stated honestly).
    """

    targets: tuple[GreeksEnrichTarget, ...]
    stale_null: int = 0
    stale_v1: int = 0
    stale_inputs_drift: int = 0
    stale_unfixable: int = 0
    current: int = 0
    futures_skipped: int = 0
    undecodable: int = 0
    live_skipped: int = 0
    no_files: int = 0
    probe_failed: int = 0
    files_probed: int = 0


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """A frozen, content-addressed execution plan for one backfill run.

    Attributes
    ----------
    backfill_id
        ``sha256`` over the formula version ‖ rate-source KIND id ‖ the
        rate-source VALUE fingerprint (:func:`_rate_source_value_fingerprint`,
        fix round 4) ‖ ``RISK_FREE_SOURCE_VERSION`` ‖ ``force`` ‖
        ``expired_only`` ‖ the sorted target keys — ANY input or plan-set
        change (including a rate VALUE revision under an unchanged source
        kind) yields a different id, so a stale checkpoint self-ignores (the
        ``sweep_id`` guard of
        :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore`).
    targets
        The scan's sorted stale targets, frozen.
    scan
        The full :class:`BackfillScan` census (carried into the report).
    force, expired_only
        The scan modes baked into the id.
    """

    backfill_id: str
    targets: tuple[GreeksEnrichTarget, ...]
    scan: BackfillScan
    force: bool
    expired_only: bool


@dataclass(frozen=True, slots=True)
class GreeksBackfillReport:
    """One backfill run's honest outcome (dry-run or wet).

    Contract-level tallies (``enriched`` / ``already_current`` / ``errored`` /
    ``no_bars`` / ``futures_skipped`` / ``undecodable``) fold the driver's
    :class:`~brutex.data.greeks_enrich.GreeksEnrichReport` counters across the
    run's batches; bar-level tallies (``bars_*``) likewise. The scan census
    (``stale_*`` / ``probe_failed``) rides along so one report answers both
    "what was found" and "what was done". ``resumed_from`` is the target index
    the run started at (``0`` = fresh); ``note`` is the completion headline the
    dashboard job surfaces verbatim.
    """

    backfill_id: str
    planned: int
    batches: int
    enriched: int
    already_current: int
    errored: int
    no_bars: int
    futures_skipped: int
    undecodable: int
    bars_written: int
    bars_solved: int
    bars_null: int
    bars_reused: int
    bars_skipped: int
    stale_null: int
    stale_v1: int
    stale_inputs_drift: int
    stale_unfixable: int
    probe_failed: int
    resumed_from: int
    dry_run: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        """The JSON-friendly projection (the dashboard job's return shape)."""
        return {
            "backfill_id": self.backfill_id,
            "planned": self.planned,
            "batches": self.batches,
            "enriched": self.enriched,
            "already_current": self.already_current,
            "errored": self.errored,
            "no_bars": self.no_bars,
            "futures_skipped": self.futures_skipped,
            "undecodable": self.undecodable,
            "bars_written": self.bars_written,
            "bars_solved": self.bars_solved,
            "bars_null": self.bars_null,
            "bars_reused": self.bars_reused,
            "bars_skipped": self.bars_skipped,
            "stale_null": self.stale_null,
            "stale_v1": self.stale_v1,
            "stale_inputs_drift": self.stale_inputs_drift,
            "stale_unfixable": self.stale_unfixable,
            "probe_failed": self.probe_failed,
            "resumed_from": self.resumed_from,
            "dry_run": self.dry_run,
            "note": self.note,
        }


def checkpoint_path(lake_root: Path) -> Path:
    """The durable checkpoint sidecar path under *lake_root* — pure, O(1)."""
    return lake_root / CHECKPOINT_DIR_NAME / CHECKPOINT_FILE_NAME


def revision_marker_path(lake_root: Path) -> Path:
    """The cross-process freshness marker path under *lake_root* — pure, O(1).

    See :data:`REVISION_MARKER_FILE_NAME` for why the marker exists (the
    lake-revision component a running dashboard probes so a CLI backfill's
    freshly-written Greeks overlay is served without a restart).
    """
    return lake_root / CHECKPOINT_DIR_NAME / REVISION_MARKER_FILE_NAME


def _touch_revision_marker(lake_root: Path, *, backfill_id: str, bars_written: int) -> None:
    """Move the cross-process lake-revision token after a bar-writing batch.

    Writes a tiny JSON marker (atomic tmp→``fsync``→``os.replace`` via the
    COMMON :func:`~brutex.data.atomic_io.atomic_write_simple`) whose
    ``mtime``/size :func:`brutex.dashboard.lake_revision.lake_revision` stats
    into its composite token, so a running dashboard's generation-keyed caches
    (``/movers`` iv/delta overlays, ``/db`` totals, …) recompute on the next
    poll instead of serving the pre-backfill Greeks until an unrelated pull or
    a restart (Golden Rule #1). BEST-EFFORT — an :class:`OSError` (including a
    stray non-directory file at the sidecar dir) logs ONE
    ``greeks_backfill.revision_marker_failed`` warning and never affects the
    run: the marker is a freshness signal, never data (the dashboard-run
    backfill also bumps in-process via ``mark_dirty``; a lost marker there
    costs nothing, and on the CLI it degrades to the pre-fix staleness window,
    never a wrong value). O(1) per batch.
    """
    dest = revision_marker_path(lake_root)
    payload = json.dumps(
        {"backfill_id": backfill_id, "bars_written": bars_written, "updated_at": time.time()},
        sort_keys=True,
    ).encode("utf-8")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(payload))
    except OSError as exc:
        _LOG.warning(
            "greeks_backfill.revision_marker_failed",
            path=str(dest),
            error=str(exc),
        )


def resolve_backfill_batch_size(env: Mapping[str, str] | None = None) -> int:
    """The per-batch contract count — default 25, env override FAIL-LOUD.

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable.

    Returns
    -------
    int
        :data:`DEFAULT_BACKFILL_BATCH_SIZE` when :data:`BACKFILL_BATCH_ENV` is
        unset/blank, else the parsed override.

    Raises
    ------
    ValueError
        On a non-integer or ``< 1`` override — fail-loud, never a silent
        fallback (the #1360 env contract).
    """
    source = os.environ if env is None else env
    raw = source.get(BACKFILL_BATCH_ENV, "").strip()
    if not raw:
        return DEFAULT_BACKFILL_BATCH_SIZE
    try:
        parsed = int(raw)
    except ValueError as exc:
        msg = f"{BACKFILL_BATCH_ENV}={raw!r} is not an integer"
        raise ValueError(msg) from exc
    if parsed < 1:
        msg = f"{BACKFILL_BATCH_ENV}={parsed} must be >= 1"
        raise ValueError(msg)
    return parsed


def resolve_backfill_pause_s(env: Mapping[str, str] | None = None) -> float:
    """The inter-batch pause in seconds — default 0.0, env override FAIL-LOUD.

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable.

    Returns
    -------
    float
        ``0.0`` when :data:`BACKFILL_PAUSE_ENV` is unset/blank, else the
        parsed override.

    Raises
    ------
    ValueError
        On a non-float, non-FINITE, or negative override — fail-loud (the
        #1360 contract). Non-finite is rejected explicitly (fix round 4,
        refuter-confirmed): ``float("inf")``/``float("nan")`` parse fine and
        pass a bare ``< 0`` check (NaN compares ``False`` to everything), yet
        ``inf`` crashes the wet run mid-batch with a raw ``OverflowError``
        from ``time.sleep`` AFTER the full O(files) scan, and ``nan``
        SILENTLY disables the configured gentleness pause — the exact silent
        fallback this contract forbids.
    """
    source = os.environ if env is None else env
    raw = source.get(BACKFILL_PAUSE_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        parsed = float(raw)
    except ValueError as exc:
        msg = f"{BACKFILL_PAUSE_ENV}={raw!r} is not a number"
        raise ValueError(msg) from exc
    if not math.isfinite(parsed):
        msg = f"{BACKFILL_PAUSE_ENV}={raw!r} must be finite"
        raise ValueError(msg)
    if parsed < 0:
        msg = f"{BACKFILL_PAUSE_ENV}={parsed} must be >= 0"
        raise ValueError(msg)
    return parsed


def _today_ist() -> date:
    """Today's IST calendar date (fixed +05:30 — the lake_heal idiom)."""
    return datetime.now(UTC).astimezone(_IST).date()


def _month_files(minute_dir: Path) -> tuple[list[tuple[int, int, Path]], list[Path]]:
    """The contract's canonical ``<yyyy>/<mm>.parquet`` month files, ascending.

    Returns ``(files, driver_unreachable)``. Non-numeric AND
    numeric-but-out-of-calendar-range directory / file names are skipped
    silently (a foreign tree never raises): a ``13.parquet`` / ``0.parquet``
    or a year outside ``[1, 9998]`` would make :func:`_contract_window`'s
    :class:`datetime.date` construction raise ``ValueError`` and kill the WHOLE
    scan (the December wrap computes ``year + 1``, hence the ``9998`` cap —
    ``date.max.year - 1``); such a name can also never hold in-window data.

    An IN-range name that does NOT round-trip to the canonical zero-padded
    form (``2024/1.parquet``, a ``023`` year dir) is returned in
    ``driver_unreachable`` instead of ``files`` (fix round 5,
    refuter-confirmed): the enrich driver reads a target through
    ``ist_span_partition_files``, which constructs ONLY canonical
    ``f"{year:04d}/{month:02d}.parquet"`` candidates — probing/targeting such
    a file would re-select the contract on EVERY run while the driver read
    ZERO bars, wrote nothing, and the run exited a false green with the stale
    stamp persisting forever. The caller surfaces the rejects loudly
    (``probe_failed`` + WARN); repair is a rename-to-canonical lake task.

    No lake writer produces non-canonical names; the guard is the same
    foreign-file tolerance the probe already grants corrupt bytes.
    O(contract's month files).
    """
    out: list[tuple[int, int, Path]] = []
    driver_unreachable: list[Path] = []
    max_window_year = date.max.year - 1  # _contract_window's December wrap needs year + 1
    for candidate in minute_dir.glob("*/*.parquet"):
        try:
            year = int(candidate.parent.name)
            month = int(candidate.stem)
        except ValueError:
            continue
        if not (1 <= month <= _DECEMBER) or not (date.min.year <= year <= max_window_year):
            continue
        if candidate.parent.name != f"{year:04d}" or candidate.stem != f"{month:02d}":
            driver_unreachable.append(candidate)
            continue
        out.append((year, month, candidate))
    out.sort()
    driver_unreachable.sort()
    return out, driver_unreachable


def _contract_window(files: list[tuple[int, int, Path]]) -> tuple[date, date]:
    """The half-open IST window covering the contract's full stored life.

    ``[first-month day 1, day 1 of the month AFTER the last month, + 1 day)`` —
    the trailing ``+ 1 day`` absorbs the UTC-file-month vs IST-date skew (a
    23:59 UTC month-end bar's IST date lands on the NEXT day). Over-inclusion
    is harmless: the driver's reuse fast path restates an already-current bar,
    never loses one.
    """
    first_year, first_month, _ = files[0]
    last_year, last_month, _ = files[-1]
    start = date(first_year, first_month, 1)
    after = (
        date(last_year + 1, 1, 1) if last_month == _DECEMBER else date(last_year, last_month + 1, 1)
    )
    return start, after + timedelta(days=1)


def _stamped_rows_fixable(path: Path, stale_pids: Sequence[int] | None = None) -> bool:
    """Whether ANY stale-stamped row in *path* is one the enrich driver can stamp.

    The value-level twin of the driver's per-bar IVX-2 guard: the driver SKIPS
    a row whose OHLC carries a non-finite value — no bar, no write, the stale
    stamp persists — so a probe that targeted a contract on such a row's
    account alone re-fed it forever while the driver honestly reported it
    ``already_current``. ``stale_pids=None`` selects the NULL-stamped rows
    (fix round 4, refuter-confirmed); a non-``None`` *stale_pids* selects the
    rows carrying exactly those stale NON-null stamp values (fix round 6,
    refuter-confirmed sibling class: a v1 / inputs-drift stamp sitting ON the
    driver-skippable row itself bypassed the null-only gate — the same eternal
    re-target + false-green ✅/EXIT_OK the round-4 fix closed for nulls).
    Mirrors the driver's predicate EXACTLY: a row with all four prices
    present + finite is FIXABLE (the driver stamps it); a row with a NULL or
    junk-typed price is ALSO treated fixable because the driver ERRORS the
    target LOUDLY on it (``errored`` → ⚠️ head + non-zero CLI exit — never a
    silent green); ONLY the all-prices-present-but-non-finite row is
    driver-SKIPPABLE. FAIL-OPEN on any read trouble (missing OHLC columns /
    corrupt bytes / a stamp column the ``is_in`` filter cannot compare →
    ``True``): feeding the driver is the safe over-approximation this module
    documents. One extra bounded 5-column read, paid ONLY on the
    stale/null-stamp branch — the clean fast path stays a single stamp-column
    read per file.
    """
    import polars as pl

    try:
        frame = pl.read_parquet(path, columns=[*_PRICE_COLUMNS, "greeks_provenance_id"])
        stamp = pl.col("greeks_provenance_id")
        selector = stamp.is_null() if stale_pids is None else stamp.is_in(list(stale_pids))
        stale_rows = frame.filter(selector).select(_PRICE_COLUMNS)
    except (pl.exceptions.PolarsError, OSError):
        return True  # fail-open: re-feed the driver (its own guards are total)
    saw_row = False
    for row in stale_rows.iter_rows():
        saw_row = True
        if any(value is None for value in row):
            return True  # a NULL price ERRORS the target loudly in the driver
        try:
            prices = [float(value) for value in row]
        except (TypeError, ValueError):
            return True  # a junk-typed price ERRORS the target loudly too
        if all(math.isfinite(price) for price in prices):
            return True  # the driver CAN stamp this row — genuinely stale
    # Every visible stale-stamped row is driver-skippable (or none were visible
    # at all — a probe/read skew, fail-open): unfixable only when rows were seen.
    return not saw_row


def _probe_contract_staleness(
    files: list[tuple[int, int, Path]],
    *,
    rate_source: RiskFreeSource,
    symbol: str,
    per_file_pause: float,
    sleep: Callable[[float], None],
) -> tuple[str | None, int, int, bool]:
    """Probe one contract's month files for a stale stamp — bounded column reads.

    Returns ``(stale_reason, files_probed, probe_failed, unfixable_seen)``
    where ``stale_reason`` is one of :data:`_STALE_NULL` / :data:`_STALE_V1` /
    :data:`_STALE_DRIFT` or ``None`` (every probed stamp current or
    driver-unfixable) and ``unfixable_seen`` flags a file whose null OR stale
    NON-null stamps sit ONLY on driver-skippable rows (non-finite OHLC — see
    :func:`_stamped_rows_fixable`; fix rounds 4 + 6, both refuter-confirmed:
    targeting on such a row's account re-fed the contract forever while the
    driver read ``already_current``. An unfixable-null file's remaining
    NON-null stamps are still checked, so a v1/drift sibling stamp on a
    driver-FIXABLE row still targets the contract — but a stale stamp whose
    every carrying row is itself skippable no longer does).
    Short-circuits on the FIRST stale file. A legacy 7-column partition (no
    ``greeks_provenance_id`` column at all) classifies :data:`_STALE_NULL`. A
    corrupt/unreadable file is counted + logged and the probe continues — and
    so is a SCHEMA-POISONED stamp column (a wrong-dtype ``greeks_provenance_id``
    like utf8, a non-null NaN float, or an inf/1e999 float — values the
    ``int()`` coercion rejects with ``ValueError``/``TypeError``/
    ``OverflowError``; the last is the fix-round-6 refuter repro: ``int(inf)``
    raises ``OverflowError``, which subclasses ``ArithmeticError`` — NOT
    ``ValueError`` — so one inf-stamped file previously killed the WHOLE
    scan): all fold into ``probe_failed`` so ONE junk file never kills the
    scan (the DEC-MOVERS-CORRUPT-SKIP-001 class). A genuine code bug elsewhere
    still propagates — the read catch is the narrow movers_resilient
    ``(PolarsError, OSError)`` set, the value catch only the coercion trio;
    healing is :mod:`brutex.data.lake_heal`'s job, never re-implemented here.
    Each probed file fires one liveness
    :func:`~brutex.observability.progress_beat.emit_progress_beat` (a no-op
    outside a dashboard job) so the O(files) probe never trips the pull stall
    watchdog (DEC-PIPELINE-STALL-HEARTBEAT-001).
    """
    import polars as pl

    files_probed = 0
    probe_failed = 0
    unfixable_seen = False
    for _year, _month, path in files:
        if per_file_pause > 0:
            sleep(per_file_pause)
        # Liveness beat per probed file: the stamp probe is the scan's O(files)
        # bulk — silent for >600 s on a deep lake it would false-trip the
        # dashboard watchdog (false CRITICAL pull-halt alert). No-op on the CLI.
        emit_progress_beat()
        files_probed += 1
        try:
            series = pl.read_parquet(path, columns=["greeks_provenance_id"])["greeks_provenance_id"]
        except pl.exceptions.ColumnNotFoundError:
            # A legacy 7-column partition — every bar is unstamped.
            return _STALE_NULL, files_probed, probe_failed, unfixable_seen
        except (pl.exceptions.PolarsError, OSError) as exc:
            probe_failed += 1
            _LOG.warning(
                "greeks_backfill.probe_skip",
                symbol=symbol,
                file=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        stamp_values = series
        if series.null_count() > 0:
            if _stamped_rows_fixable(path):
                return _STALE_NULL, files_probed, probe_failed, unfixable_seen
            # Every null stamp in this file sits on a driver-skippable row
            # (non-finite OHLC): targeting on their account would loop forever
            # (fix round 4). Flag it, then still check the NON-null stamps.
            unfixable_seen = True
            stamp_values = series.drop_nulls()
        stale_reason: str | None = None
        stale_pids: list[int] = []
        try:
            for value in stamp_values.unique(maintain_order=True).to_list():
                pid = int(value)
                if not stamp_is_current_inputs(pid, rate_source=rate_source):
                    stale_pids.append(pid)
                    if stale_reason is None:
                        stored = unpack_greeks_provenance(pid)
                        stale_reason = (
                            _STALE_V1
                            if stored.formula_version != GREEKS_FORMULA_VERSION
                            else _STALE_DRIFT
                        )
        except (ValueError, TypeError, OverflowError) as exc:
            # A schema-poisoned stamp column (utf8 "poison", a NaN float, an
            # inf/1e999 float — int(inf) raises OverflowError, fix round 6):
            # data corruption, not a code bug — count + log + continue, exactly
            # like the unreadable-file arm above.
            probe_failed += 1
            _LOG.warning(
                "greeks_backfill.probe_skip",
                symbol=symbol,
                file=str(path),
                dtype=str(series.dtype),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if stale_reason is not None:
            if _stamped_rows_fixable(path, stale_pids):
                return stale_reason, files_probed, probe_failed, unfixable_seen
            # Every row carrying a stale stamp in this file is itself
            # driver-skippable (non-finite OHLC) — the fix-round-4 null
            # class's NON-null sibling (fix round 6, refuter-confirmed):
            # targeting on its account re-fed the contract forever while the
            # wet run read a false green. Flag it and keep probing.
            unfixable_seen = True
    return None, files_probed, probe_failed, unfixable_seen


def scan_stale_greeks_targets(
    lake_root: Path,
    *,
    rate_source: RiskFreeSource,
    expired_only: bool = False,
    force: bool = False,
    limit: int | None = None,
    today: date | None = None,
    per_file_pause: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillScan:
    """Discover the option contracts whose stored Greeks stamps are STALE.

    HONEST COST (Golden Rule #10): the scan is **O(files)** — one bounded
    ``greeks_provenance_id`` column read per month partition of every decoded
    option contract (short-circuiting per contract on the first stale file) —
    NEVER O(1). Enumeration rides the manifest-first
    :func:`brutex.dashboard.db_view.list_fno_candle_datasets`
    (O(manifest-rows), graceful O(files) walk fallback).

    LIVENESS (DEC-PIPELINE-STALL-HEARTBEAT-001): one
    :func:`~brutex.observability.progress_beat.emit_progress_beat` fires per
    considered contract AND per probed month file, AND — fix round 2 — per
    walked symbol directory inside the ENUMERATION phase's manifest-miss
    O(files) tree walk (``db_view._walk_candle_datasets``, which fully
    materialises BEFORE the first per-contract beat and measured 29.7 minutes
    on the production 2.36M-file lake — ~3× the watchdog timeout), so a long
    scan on the dashboard job thread keeps the 600 s pull stall watchdog fed
    through EVERY phase instead of false-tripping a CRITICAL "pull halted"
    alert on a healthy backfill (the beat is a documented no-op outside a
    dashboard job — the CLI path is unaffected).

    READ-only in lake-DATA terms — the scan never touches ``bars/`` /
    ``options/`` / the checkpoint. HONEST side-effect note: the manifest-first
    enumeration may SELF-HEAL the shared ``.manifest/`` dataset cache (the
    ``list_candle_datasets`` read-through rebuild — a derived cache every
    dashboard read also rebuilds), pinned by the dry-run inertness test as the
    ONLY byte that can move.

    Parameters
    ----------
    lake_root
        The lake root (the parent of ``bars/``).
    rate_source
        The resolved risk-free source — its packed id keys the staleness
        predicate (:func:`~brutex.data.greeks_enrich.stamp_is_current_inputs`).
    expired_only
        When ``True``, only contracts whose expiry is strictly before *today*
        (IST) are considered; the default ``False`` also reaches LIVE contracts
        so a CLI-pulled lake (gap b of #1188) is fully coverable.
    force
        Skip the stamp probe entirely — EVERY decoded option contract becomes
        a target and the driver's VALUE-checked reuse gate decides per bar (the
        documented escape for the stamp-invisible rate/spot value-drift class).
        REFUSED in combination with *limit* (see below).
    limit
        Truncate the SORTED target list to the first *limit* contracts — an
        incremental chip for the DEFAULT stamp-driven mode ONLY: there the
        next invocation self-resumes the remainder because completed contracts
        classify current. Combined with *force* the scan raises
        :class:`ValueError` — force-mode selection is stamp-INdependent, so a
        limited force run would deterministically re-select the SAME first
        *limit* sorted contracts on EVERY run and the remainder would be
        unreachable (the chipping promise would be an illusion, Golden
        Rule #1).
    today
        Injectable IST "today" for the ``expired_only`` comparison (defaults
        to the real IST date).
    per_file_pause
        Optional seconds slept before each file probe (the lake_heal L3
        gentleness throttle; default ``0.0`` = no pause).
    sleep
        The sleeper (injected for deterministic tests).

    Returns
    -------
    BackfillScan
        The sorted stale targets + the honest census counters.

    Raises
    ------
    ValueError
        On ``force=True`` combined with a non-``None`` *limit* (the
        never-progressing chip — refused loudly, never a silent footgun).
    """
    from brutex.dashboard.db_view import list_fno_candle_datasets

    if force and limit is not None:
        msg = (
            "force combined with limit never progresses: force-mode selection is "
            "stamp-independent, so every run re-selects the SAME first "
            f"{limit} sorted contract(s) and the remainder is unreachable — "
            "incremental chipping (limit) works only in the default stamp-driven "
            "mode. Drop force or drop limit."
        )
        raise ValueError(msg)
    today_resolved = _today_ist() if today is None else today
    seen: set[tuple[str, str]] = set()
    decoded: list[tuple[FnoSymbolParts, str, str]] = []
    futures_skipped = undecodable = live_skipped = 0
    for dataset in list_fno_candle_datasets(lake_root):
        if dataset["interval"] != _MINUTE_INTERVAL:
            continue
        exchange = str(dataset["exchange"])
        symbol = str(dataset["symbol"])
        key = (exchange, symbol)
        if key in seen:
            continue
        seen.add(key)
        parts = parse_fno_symbol(symbol)
        if parts is None:
            undecodable += 1
            continue
        if not parts.is_option:
            futures_skipped += 1
            continue
        if expired_only and parts.expiry >= today_resolved:
            live_skipped += 1
            continue
        decoded.append((parts, exchange, symbol))
    # Deterministic total order (the checkpoint precondition) that also groups
    # one underlying's CE/PE chain adjacently for the driver's spot memo.
    decoded.sort(key=lambda item: (item[0].underlying, item[1], item[2]))

    targets: list[GreeksEnrichTarget] = []
    stale_null = stale_v1 = stale_drift = stale_unfixable = current = no_files = 0
    probe_failed = files_probed = 0
    for _parts, exchange, symbol in decoded:
        if limit is not None and len(targets) >= limit:
            break
        # Liveness beat per considered contract (DEC-PIPELINE-STALL-HEARTBEAT-001):
        # covers the force / no-files arms the per-file probe beat never reaches.
        # No-op outside a dashboard job; best-effort; O(1).
        emit_progress_beat()
        minute_dir = lake_root / "bars" / exchange / FNO_CANDLE_LAKE_SEGMENT / symbol
        files, driver_unreachable = _month_files(minute_dir / _MINUTE_INTERVAL)
        if driver_unreachable:
            # Fix round 5 (refuter-confirmed): an in-range NON-canonical month
            # name ("2024/1.parquet") is a file the driver's zero-padded reader
            # (ist_span_partition_files) can NEVER open — probing/targeting it
            # would re-select the contract on EVERY run while the wet run wrote
            # zero bars and exited a false green (the stale stamp persisting
            # forever, the census never converging). Fold into probe_failed +
            # WARN loudly: repair is a rename-to-canonical lake task, never a
            # re-queue.
            probe_failed += len(driver_unreachable)
            _LOG.warning(
                "greeks_backfill.month_file_driver_unreachable",
                symbol=symbol,
                exchange=exchange,
                count=len(driver_unreachable),
                files=[str(path) for path in driver_unreachable[:5]],
                reason=(
                    "non-canonical month filename (not zero-padded "
                    "<yyyy>/<mm>.parquet) — the enrich driver reads ONLY "
                    "canonical names; rename the file to its canonical form"
                ),
            )
        if not files:
            no_files += 1
            continue
        start, end = _contract_window(files)
        if force:
            targets.append(
                GreeksEnrichTarget(exchange=exchange, symbol=symbol, start=start, end=end)
            )
            continue
        reason, probed, failed, unfixable = _probe_contract_staleness(
            files,
            rate_source=rate_source,
            symbol=symbol,
            per_file_pause=per_file_pause,
            sleep=sleep,
        )
        files_probed += probed
        probe_failed += failed
        if reason is None:
            if unfixable:
                # The contract's ONLY staleness is driver-skippable rows
                # (non-finite OHLC) — null-stamped (fix round 4) or stale
                # NON-null-stamped (fix round 6), both refuter-confirmed
                # non-convergence: targeting it would re-feed forever while
                # the driver honestly reported it already_current. Count +
                # log LOUDLY — repair is a lake-repair task, never silently
                # re-queued.
                stale_unfixable += 1
                _LOG.warning(
                    "greeks_backfill.contract_unfixable",
                    symbol=symbol,
                    exchange=exchange,
                    reason=(
                        "null- or stale-stamped row(s) with non-finite OHLC — "
                        "the enrich driver skips them by design (IVX-2); lake "
                        "repair needed"
                    ),
                )
            else:
                current += 1
            continue
        if reason == _STALE_NULL:
            stale_null += 1
        elif reason == _STALE_V1:
            stale_v1 += 1
        else:
            stale_drift += 1
        targets.append(GreeksEnrichTarget(exchange=exchange, symbol=symbol, start=start, end=end))
    scan = BackfillScan(
        targets=tuple(targets),
        stale_null=stale_null,
        stale_v1=stale_v1,
        stale_inputs_drift=stale_drift,
        stale_unfixable=stale_unfixable,
        current=current,
        futures_skipped=futures_skipped,
        undecodable=undecodable,
        live_skipped=live_skipped,
        no_files=no_files,
        probe_failed=probe_failed,
        files_probed=files_probed,
    )
    _LOG.info(
        "greeks_backfill.scan_done",
        targets=len(scan.targets),
        stale_null=scan.stale_null,
        stale_v1=scan.stale_v1,
        stale_inputs_drift=scan.stale_inputs_drift,
        stale_unfixable=scan.stale_unfixable,
        current=scan.current,
        futures_skipped=scan.futures_skipped,
        undecodable=scan.undecodable,
        live_skipped=scan.live_skipped,
        no_files=scan.no_files,
        probe_failed=scan.probe_failed,
        files_probed=scan.files_probed,
        force=force,
        expired_only=expired_only,
    )
    return scan


def _rate_source_value_fingerprint(rate_source: RiskFreeSource) -> str:
    """The rate source's VALUE fingerprint — a ``backfill_id`` head component.

    Fix round 4 (refuter-confirmed MAJOR): ``rate_source_id_for`` labels only
    the source KIND (fixed=1 / csv=2), so a force-mode plan — whose target set
    is stamp-independent — produced the IDENTICAL ``backfill_id`` across a
    rate VALUE change, and a surviving checkpoint from a crashed force run was
    ADOPTED after the operator revised the fixed rate / the
    ``BRUTEX_RISK_FREE_CSV`` values: the recorded prefix was never routed
    through the driver's value-checked gate — the exact stamp-invisible drift
    class ``--force`` is documented to catch — ending in a false-clean green
    run with the wrong-rate Greeks stamped CURRENT forever. Fingerprinting the
    VALUES the source actually serves re-keys the plan on any change, so the
    stale checkpoint self-ignores (the ``sweep_id`` guard).

    * :class:`~brutex.options.risk_free.FixedRateSource` → the exact
      configured rate (``repr`` — full float precision, deterministic).
    * :class:`~brutex.options.risk_free.CsvRateSource` → a sha256 over the
      LOADED curve (source label + every ``date=rate`` pair) — the values the
      driver consumes, so two byte-different CSVs parsing to the same curve
      are honestly value-identical. O(rows), once per plan.
    * Any other (unrecognised) source kind → the stable ``"unlabelled"``
      sentinel — an honest kind-only fallback mirroring
      ``RATE_SOURCE_ID_NONE`` (the stamp cannot represent such a source
      either; production resolves only the two shapes above).
    """
    if isinstance(rate_source, FixedRateSource):
        return f"fixed={rate_source.rate!r}"
    if isinstance(rate_source, CsvRateSource):
        digest = hashlib.sha256()
        digest.update(rate_source.source.encode("utf-8"))
        for on_date, rate in zip(rate_source.dates, rate_source.rates, strict=True):
            digest.update(f"\x1f{on_date.isoformat()}={rate!r}".encode())
        return f"csv={digest.hexdigest()}"
    return "unlabelled"


def plan_greeks_backfill(
    scan: BackfillScan,
    *,
    rate_source: RiskFreeSource,
    force: bool,
    expired_only: bool,
) -> BackfillPlan:
    """Freeze *scan* into a content-addressed :class:`BackfillPlan`.

    The ``backfill_id`` hashes every input that shapes the run (formula
    version, rate-source KIND id, the rate-source VALUE fingerprint
    (:func:`_rate_source_value_fingerprint` — fix round 4: a rate VALUE change
    under an unchanged kind must re-key the plan so a crashed force run's
    checkpoint can never exempt its recorded prefix from the driver's
    value-checked gate), rate-source version, the two scan modes, the sorted
    target keys), so ANY change — a formula bump, a different rate CSV kind,
    a revised rate value/curve, a shrunk/grown plan — yields a NEW id and a
    prior checkpoint self-ignores. Deterministic: the same scan + inputs
    always produce the same id (pinned by test). O(targets).
    """
    hasher = hashlib.sha256()
    head = (
        f"{GREEKS_FORMULA_VERSION}|{rate_source_id_for(rate_source)}|"
        f"{_rate_source_value_fingerprint(rate_source)}|"
        f"{RISK_FREE_SOURCE_VERSION}|{int(force)}|{int(expired_only)}"
    )
    hasher.update(head.encode("utf-8"))
    for target in scan.targets:
        key = f"\x1f{target.exchange}|{target.symbol}|{target.start}|{target.end}"
        hasher.update(key.encode("utf-8"))
    return BackfillPlan(
        backfill_id=hasher.hexdigest(),
        targets=scan.targets,
        scan=scan,
        force=force,
        expired_only=expired_only,
    )


def _unfixable_note_clause(stale_unfixable: int) -> str:
    """The honest completion-note clause for driver-unfixable contracts — O(1).

    Empty when none were found (note stability); otherwise a LOUD clause the
    dashboard headline + CLI print surface verbatim (fix round 4 — the census
    must never read green while a null stamp persists on a row the driver
    cannot act on).
    """
    if stale_unfixable <= 0:
        return ""
    return (
        f"; {stale_unfixable} contract(s) UNFIXABLE (null-stamped non-finite-OHLC "
        "row(s) the enrich driver skips by design — lake repair needed, excluded "
        "from targeting)"
    )


def dry_run_backfill_report(plan: BackfillPlan) -> GreeksBackfillReport:
    """The INERT dry-run report — scan census only, zero writes/checkpoint/alerts."""
    scan = plan.scan
    note = (
        f"DRY RUN — would re-enrich {len(plan.targets)} contract(s) "
        f"({scan.stale_null} unstamped, {scan.stale_v1} v1, "
        f"{scan.stale_inputs_drift} inputs-drift"
        f"{'; force — stamp probe skipped' if plan.force else ''}); "
        "zero writes, zero checkpoint, zero alerts" + _unfixable_note_clause(scan.stale_unfixable)
    )
    return GreeksBackfillReport(
        backfill_id=plan.backfill_id,
        planned=len(plan.targets),
        batches=0,
        enriched=0,
        already_current=0,
        errored=0,
        no_bars=0,
        futures_skipped=scan.futures_skipped,
        undecodable=scan.undecodable,
        bars_written=0,
        bars_solved=0,
        bars_null=0,
        bars_reused=0,
        bars_skipped=0,
        stale_null=scan.stale_null,
        stale_v1=scan.stale_v1,
        stale_inputs_drift=scan.stale_inputs_drift,
        stale_unfixable=scan.stale_unfixable,
        probe_failed=scan.probe_failed,
        resumed_from=0,
        dry_run=True,
        note=note,
    )


@dataclass(slots=True)
class _RunTallies:
    """Mutable fold accumulator over the batches' driver reports."""

    enriched: int = 0
    already_current: int = 0
    errored: int = 0
    no_bars: int = 0
    futures_skipped: int = 0
    undecodable: int = 0
    bars_written: int = 0
    bars_solved: int = 0
    bars_null: int = 0
    bars_reused: int = 0
    bars_skipped: int = 0

    def fold(self, report: GreeksEnrichReport) -> None:
        """Fold one batch's :class:`GreeksEnrichReport` — O(1)."""
        self.enriched += report.enriched
        self.already_current += report.already_current
        self.errored += report.errored
        self.no_bars += report.no_bars
        self.futures_skipped += report.futures_skipped
        self.undecodable += report.undecodable
        self.bars_written += report.bars_written
        self.bars_solved += report.bars_solved
        self.bars_null += report.bars_null
        self.bars_reused += report.bars_reused
        self.bars_skipped += report.bars_skipped


def run_greeks_backfill(
    lake_root: Path,
    *,
    plan: BackfillPlan,
    rate_source: RiskFreeSource,
    batch_size: int | None = None,
    pause_s: float | None = None,
    workers: int | None = None,
    on_progress: BackfillProgress | None = None,
    checkpoint_store: SweepCheckpointStore | None = None,
    enrich: Callable[..., GreeksEnrichReport] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GreeksBackfillReport:
    """Execute *plan* in bounded batches through the EXISTING enrich driver.

    Each batch is ONE :func:`~brutex.data.greeks_enrich.enrich_greeks_for_targets`
    call — inheriting for free the value-checked reuse gate, every honest
    demotion, the per-contract error quarantine (only
    :class:`~brutex.data.errors.LakeWriteHalt` and
    :class:`~brutex.data.errors.GreeksEnrichPoolBroken` escape — BOTH
    deliberately NOT caught here, so a disk-full/OOM halts gracefully with the
    checkpoint intact and the next run resumes), the spot-join memo, the
    DEC-GREEKS-PARALLEL-001 process pool (``BRUTEX_GREEKS_WORKERS`` /
    *workers*), and the ``latest-wins`` flock-guarded write spine.

    Checkpoint discipline (ADVISORY — the ``tuple_complete`` rule): the
    monotonic per-batch ``record`` is a resume ACCELERATOR; true idempotence is
    the driver's stamp+value gate (a redone batch restates byte-identically
    with ``changed == 0`` ⇒ no write). The high-water mark advances ONLY over a
    fully-CLEAN prefix — ``record`` fires only while the run's cumulative
    ``errored == 0`` (fix round 3): an errored contract keeps its stale stamps,
    so an unchanged re-scan reproduces the IDENTICAL ``backfill_id`` and a
    checkpoint recorded past it would make a crash-resume SKIP the errored
    contract and report a false-clean ``errored=0`` run (bypassing the CLI's
    ``EXIT_USER_ERROR`` gate + flipping the Telegram head to a dishonest ✅);
    freezing at the last clean boundary re-feeds every errored contract on
    resume, at the cost of cheaply restating any clean batch redone alongside.
    A corrupt / foreign-``backfill_id``
    checkpoint is ignored LOUDLY (fresh start — correct via the stamps); a
    FAILING ``record`` logs + continues (checkpoint loss ≠ halt — a deliberate,
    documented deviation from the lake writers' ``atomic_write_via`` rule: the
    sidecar is advisory, while a REAL lake ENOSPC still halts through the
    driver's ``LakeWriteHalt``). On completion the checkpoint file is DELETED —
    the stamps are the durable done-record. Every batch that WROTE bars also
    touches the durable :func:`revision_marker_path` freshness marker
    (best-effort) so a running dashboard's cross-process
    ``lake_revision`` probe invalidates its generation-keyed caches — a CLI
    backfill's new Greeks reach ``/movers`` on the next poll, no restart
    (fix round 2; the backfill never touches the window ledger, so the marker
    is its ONLY revision signal).

    Parameters
    ----------
    lake_root
        The lake root the driver reads + writes under.
    plan
        The frozen :func:`plan_greeks_backfill` output.
    rate_source
        The resolved risk-free source (MUST be the one the plan was built
        with — the ``backfill_id`` binds its KIND and its VALUES via
        :func:`_rate_source_value_fingerprint`, fix round 4, so a surviving
        checkpoint from a different rate configuration self-ignores).
    batch_size
        Contracts per batch (default :func:`resolve_backfill_batch_size`).
    pause_s
        Seconds slept between batches (default :func:`resolve_backfill_pause_s`).
    workers
        Optional explicit driver worker count (``None`` = the driver's own
        resolution; ``1`` = the sequential kill-switch).
    on_progress
        Optional per-batch hook ``(completed, total, bars_written, errored,
        label)`` — absolute counts, O(1) per batch.
    checkpoint_store
        Injectable checkpoint store (tests); the default binds
        :func:`checkpoint_path` under *plan*'s ``backfill_id``.
    enrich
        Injectable driver seam (tests); the default is the REAL
        :func:`~brutex.data.greeks_enrich.enrich_greeks_for_targets`.
    sleep
        The inter-batch sleeper (injected for deterministic tests).

    Returns
    -------
    GreeksBackfillReport
        The folded outcome (``dry_run=False``).

    Raises
    ------
    ValueError
        On an explicit *batch_size* ``< 1`` or a *pause_s* that is negative or
        non-finite (fix round 4: ``inf`` would crash mid-batch with a raw
        ``OverflowError`` from ``time.sleep`` and ``nan`` would silently
        disable the pause — both refused eagerly, the resolver contract).
    """
    if batch_size is None:
        batch_size = resolve_backfill_batch_size()
    elif batch_size < 1:
        msg = f"batch_size must be >= 1, got {batch_size}"
        raise ValueError(msg)
    if pause_s is None:
        pause_s = resolve_backfill_pause_s()
    elif not math.isfinite(pause_s):
        msg = f"pause_s must be finite, got {pause_s}"
        raise ValueError(msg)
    elif pause_s < 0:
        msg = f"pause_s must be >= 0, got {pause_s}"
        raise ValueError(msg)
    enrich_fn = enrich_greeks_for_targets if enrich is None else enrich
    store = (
        SweepCheckpointStore(
            path=checkpoint_path(lake_root),
            sweep_id=plan.backfill_id,
            total_combos=len(plan.targets),
        )
        if checkpoint_store is None
        else checkpoint_store
    )
    try:
        store.load()
    except SweepCheckpointError as exc:
        _LOG.warning(
            "greeks_backfill.checkpoint_ignored",
            path=str(store.path),
            error=str(exc),
            action="fresh start (correct via the stamps)",
        )
    resumed_from = store.next_index
    total = len(plan.targets)
    tallies = _RunTallies()
    batches = 0
    for batch_start in range(resumed_from, total, batch_size):
        batch = plan.targets[batch_start : batch_start + batch_size]
        report = enrich_fn(
            targets=list(batch),
            lake_root=lake_root,
            rate_source=rate_source,
            workers=workers,
        )
        tallies.fold(report)
        batches += 1
        completed = batch_start + len(batch)
        if tallies.errored == 0:
            try:
                store.record(completed - 1)
            except SweepCheckpointError as exc:
                # ADVISORY sidecar: a lost checkpoint costs a re-scan, never data —
                # log LOUDLY and keep enriching (a REAL lake ENOSPC already halts
                # via the driver's LakeWriteHalt above).
                _LOG.warning(
                    "greeks_backfill.checkpoint_failed",
                    path=str(store.path),
                    error=str(exc),
                    completed=completed,
                )
        else:
            # ERRORED-prefix freeze (fix round 3, refuter-confirmed): the durable
            # resume index NEVER advances past an errored (per-contract-
            # quarantined) contract. An errored contract keeps its STALE stamps
            # by design, so an unchanged re-scan reproduces the IDENTICAL plan
            # (same content-addressed ``backfill_id``) — a checkpoint recorded
            # past it would make a crash-resume adopt it, SKIP the errored
            # contract silently, and report a false-clean ``errored=0`` run
            # (EXIT_OK + a green ✅ Telegram head for a plan with known-errored
            # contracts). Holding the high-water mark at the last fully-clean
            # prefix boundary is correct-by-stamps: the resume re-feeds every
            # errored contract, and any clean contract redone alongside restates
            # byte-identically through the driver's stamp+value reuse gate
            # (cheap, idempotent). The freeze is CUMULATIVE (``tallies.errored``,
            # not the batch's own count) so a later clean batch's monotonic
            # ``record`` can never leapfrog an earlier errored one.
            _LOG.info(
                "greeks_backfill.checkpoint_held",
                completed=completed,
                errored=tallies.errored,
                reason="errored contract(s) in the prefix — a resume must re-feed them",
            )
        if report.bars_written > 0:
            # This batch WROTE overlay bars: move the cross-process freshness
            # marker so a concurrently-running dashboard's revision probe sees
            # the write on its next poll (best-effort — see the helper).
            _touch_revision_marker(
                lake_root, backfill_id=plan.backfill_id, bars_written=tallies.bars_written
            )
        _LOG.info(
            "greeks_backfill.batch_done",
            batch=batches,
            completed=completed,
            total=total,
            enriched=tallies.enriched,
            already_current=tallies.already_current,
            errored=tallies.errored,
            bars_written=tallies.bars_written,
        )
        if on_progress is not None:
            label = f"contracts {completed}/{total} (batch {batches})"
            on_progress(completed, total, tallies.bars_written, tallies.errored, label)
        if pause_s > 0 and completed < total:
            sleep(pause_s)
    # Clean completion: the stamps are the durable done-record — delete the
    # advisory checkpoint so a later (different-plan) run never even reads it.
    try:
        store.path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning(
            "greeks_backfill.checkpoint_cleanup_failed",
            path=str(store.path),
            error=str(exc),
        )
    note = (
        f"Greeks backfill {plan.backfill_id[:12]}: {tallies.enriched} contract(s) "
        f"re-stamped v2, {tallies.already_current} already current, "
        f"{tallies.errored} errored (re-runnable); {tallies.bars_written} bars written"
        + (f"; resumed from target {resumed_from}" if resumed_from > 0 else "")
        + _unfixable_note_clause(plan.scan.stale_unfixable)
    )
    result = GreeksBackfillReport(
        backfill_id=plan.backfill_id,
        planned=total,
        batches=batches,
        enriched=tallies.enriched,
        already_current=tallies.already_current,
        errored=tallies.errored,
        no_bars=tallies.no_bars,
        futures_skipped=tallies.futures_skipped,
        undecodable=tallies.undecodable,
        bars_written=tallies.bars_written,
        bars_solved=tallies.bars_solved,
        bars_null=tallies.bars_null,
        bars_reused=tallies.bars_reused,
        bars_skipped=tallies.bars_skipped,
        stale_null=plan.scan.stale_null,
        stale_v1=plan.scan.stale_v1,
        stale_inputs_drift=plan.scan.stale_inputs_drift,
        stale_unfixable=plan.scan.stale_unfixable,
        probe_failed=plan.scan.probe_failed,
        resumed_from=resumed_from,
        dry_run=False,
        note=note,
    )
    _LOG.info("greeks_backfill.done", **result.to_dict())
    return result
