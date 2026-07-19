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
  catch — ending in a false-clean green run), so a rate-value change re-keys
  the plan and the stale checkpoint self-ignores. THE INTERVENING-WRITER
  EXCEPTION (fix round 10, refuter-confirmed — the round-4 fingerprint is
  plan-vs-checkpoint only, structurally blind to the LAKE's state): a rate
  A→B→A ROUND TRIP — a force run crashed under rate A, a second force run
  under revised rate B whose errored first batch FREEZES the id-A checkpoint
  (round 3) while genuinely REWRITING the recorded prefix under rate B
  through the driver's value gate, then the natural revert to A — reproduces
  the id-A plan exactly, and the same vector needs NO second crash when the
  intervening writer is a ``brutex enrich-greeks`` / pull-tail enrich pass
  under the changed rate env (it never touches the backfill checkpoint).
  Blind adoption would SKIP the rate-B-rewritten prefix into a false-clean ✅
  ``errored=0`` exit with the residue stamped CURRENT forever (the stamp
  carries no rate value). ADOPTION is therefore freshness-gated
  (:func:`_checkpoint_superseded_reason`): a loaded checkpoint self-ignores
  LOUDLY — and the stale file is deleted — when the SHARED overlay freshness
  marker was last touched by a FOREIGN writer at/after the checkpoint's
  ``updated_at`` (this plan's OWN batch touches are identity-exempt, so a
  legitimate same-plan resume still adopts), or when the window ledger moved
  at/after it (a pull's tail re-enriches under the CURRENT rate env). A
  fresh start is always correct-by-stamps; in force mode it restates cheaply
  through the driver's value-checked gate.
* BLIND SPOT, stated everywhere: the stamp prefilter cannot see a rate/spot
  VALUE drift under an unchanged ``(rate_source_id, RISK_FREE_SOURCE_VERSION)``
  — ``force=True`` routes every contract through the driver's VALUE-checked
  gate, which catches exactly that class. Honest residue (Golden Rule #10): a
  SPOT-lake mutation (e.g. a DEC-LAKE-HEAL-001 heal) landing in the narrow
  window between a force run's crash and its resume is NOT representable in
  the ``backfill_id`` (no cheap spot-content token exists), so the resumed
  prefix would not be re-gated against the healed spot — delete
  ``<lake>/.greeks_backfill/checkpoint.json`` (or let the force run complete
  afresh) after a heal in that window to guarantee full coverage. A SECOND
  residue with the SAME remediation (fix round 10): an intervening overlay
  writer that crashed MID-pass — after partition writes but before its
  batch/pass-end marker touch, with no ledger movement (a mid-crash CLI
  enrich pass) — leaves no freshness signal for the adoption gate to see.
* A STALE-stamped bar the driver CANNOT act on is classified
  ``stale_unfixable``: counted + logged + surfaced on the report/Telegram
  honesty heads, NEVER targeted, so the census converges instead of re-feeding
  the contract forever while the driver honestly reports it
  ``already_current``. Three refuter-confirmed classes fold here: (1) a stored
  row with a non-finite OHLC value, which the driver's per-bar IVX-2 guard
  SKIPS without a write — whether the stale stamp is NULL (fix round 4) or a
  NON-null v1/inputs-drift value sitting on the skippable row itself (fix
  round 6 — the null-only gate's sibling escape: the driver skipped the row
  BEFORE its reuse gate, ``changed == 0`` read ``already_current``, the stale
  stamp persisted, and the contract was re-targeted on every run under a
  ✅/EXIT_OK false green); (2) staleness carried ONLY by rows the driver's
  reader NEVER SEES (fix round 7): every suspicious-file judgment now runs
  over the DRIVER's row view — the month file bounded to its OWN UTC month
  (the G-RACE-1 guard) then keep-FIRST deduped on ``timestamp``, EXACTLY
  ``read_option_rows``'s per-file discipline — because judging over the RAW
  row set re-targeted a contract forever on a stamp only a dropped
  duplicate-timestamp / foreign-month row carried (both directions escaped:
  a kept non-finite row shadowed by a finite dup, and a dropped dup carrying
  the ONLY stale stamp); (3) a LEGACY 7-column partition whose every
  driver-visible row is skippable (fix round 7 — the legacy arm's
  unconditional ``stale-null`` verdict bypassed the rounds-4/6 gate, the same
  eternal false-green loop on the CLI-``brutex fetch``-only lake that is
  entirely 7-column). Repairing a value-poisoned row is a lake-repair task
  (``lake_heal`` is strictly row-removing, never value-editing — a manual
  partition fix is required); a driver-invisible dup/foreign row IS
  ``lake_heal``'s job (its intra-file dup + foreign-row heals) — both stated
  loudly, never hidden.
* The scanner probes ONLY month entries the driver's reader can also open —
  canonical zero-padded ``<yyyy>/<mm>.parquet`` names (fix round 5,
  refuter-confirmed) that are REGULAR FILES (fix round 8,
  refuter-confirmed): an in-range NON-canonical name (``2024/1.parquet``)
  passed the round-3 range guard yet is invisible to
  ``ist_span_partition_files``'s zero-padded path construction, and a
  canonical-NAMED dataset-style DIRECTORY (``2024/01.parquet/``) passed the
  round-5 name check AND probed successfully (polars reads the parquet
  inside a directory) yet is invisible to the same reader's ``is_file()``
  gate — either way the contract was re-targeted on EVERY run while the wet
  run wrote nothing and read green. Such an entry now folds into
  ``probe_failed`` + ONE loud WARN (repair = rename to the canonical form /
  move the directory's data into a regular month file), never an eternal
  re-queue.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.calendars.nse import IST_FIXED_OFFSET
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
from brutex.data.greeks_revision import (
    GREEKS_SIDECAR_DIR_NAME,
    REVISION_MARKER_FILE_NAME,
    revision_marker_path,
    touch_greeks_revision_marker,
)
from brutex.data.guarded_read import (
    read_parquet_regular,
    read_parquet_schema_regular,
    read_regular_text,
)
from brutex.data.lake_read import dedup_bars_frame
from brutex.data.scope_filter import scope_admits_fno_contract
from brutex.data.window_ledger_store import deltas_dir, ledger_path
from brutex.engine.sweep_checkpoint import (
    MAX_CHECKPOINT_BYTES,
    SweepCheckpointError,
    SweepCheckpointStore,
)
from brutex.observability.progress_beat import emit_progress_beat
from brutex.options.risk_free import (
    RISK_FREE_SOURCE_VERSION,
    CsvRateSource,
    FixedRateSource,
)

if TYPE_CHECKING:
    import polars as pl

    from brutex.config.feed_scope import FeedScope
    from brutex.data.fno_symbol import FnoSymbolParts
    from brutex.engine.sweep_checkpoint import SweepCheckpoint
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
#: scanner ever sees it. The canonical definition lives in the WHEEL-FREE
#: :mod:`brutex.data.greeks_revision` (fix round 8 — the marker touch is
#: shared with the DEC-CLI-GREEKS-ENRICH-001 enrich pass, a second sanctioned
#: ledger-less overlay writer); re-exported here so every existing consumer +
#: the ``lake_revision`` literal pin stay byte-identical.
CHECKPOINT_DIR_NAME: Final[str] = GREEKS_SIDECAR_DIR_NAME

#: The checkpoint JSON file name inside :data:`CHECKPOINT_DIR_NAME`.
CHECKPOINT_FILE_NAME: Final[str] = "checkpoint.json"

# REVISION_MARKER_FILE_NAME (re-exported above from greeks_revision,
# DEC-GREEKS-BULK-REENRICH-001 fix round 2 → COMMON in fix round 8): the
# durable cross-process FRESHNESS marker inside CHECKPOINT_DIR_NAME. The
# backfill rewrites option 1-minute partitions but never touches the window
# ledger, so the `brutex.dashboard.lake_revision.lake_revision` token — which
# a running dashboard's SummaryCache probes for cross-process invalidation
# (DEC-HARDENING-LEFTOVERS-001) — never moved on a CLI `brutex
# greeks-backfill`: `/movers` kept serving the PRE-backfill iv/delta the
# backfill was run to fix, until an unrelated pull or a restart. Every WET
# batch that wrote bars touches this marker (atomic tmp→`os.replace`), and
# `lake_revision` folds one `os.stat` of it into the composite token — the
# dashboard sees the new Greeks on its next poll. The literals are duplicated
# in `lake_revision` (that module is deliberately lean stdlib-only — the
# `_SWEEPS_DIRNAME` precedent) and pinned equal by test.

#: The one Greeks-bearing interval (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — derived
#: TFs never carry the overlay and are deliberately never scanned.
_MINUTE_INTERVAL: Final[str] = "1minute"

#: Fixed +05:30 IST (no DST) — the expiry-comparison calendar; the ONE
#: canonical :data:`brutex.calendars.nse.IST_FIXED_OFFSET` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST: Final[timezone] = IST_FIXED_OFFSET

#: December — the year-wrap month for the contract-window derivation and the
#: upper bound of the valid month-file range (one COMMON constant, ruff PLR2004).
_DECEMBER: Final[int] = 12

#: The per-contract staleness verdicts the probe can return.
_STALE_NULL: Final[str] = "null"
_STALE_V1: Final[str] = "v1"
_STALE_DRIFT: Final[str] = "drift"

#: The four physical price columns the driver's per-bar IVX-2 guard inspects —
#: the probe's fixability check mirrors EXACTLY that predicate (fix round 4),
#: evaluated over the DRIVER's per-file row view (fix round 7).
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
        Contracts whose ONLY staleness is rows the driver CANNOT act on:
        driver-SKIPPABLE rows (a non-finite OHLC value — the per-bar IVX-2
        skip class, null-stamped [fix round 4], stale non-null-stamped [fix
        round 6], or a whole legacy 7-column file's rows [fix round 7]) and
        driver-INVISIBLE rows (dropped by the driver's keep-first timestamp
        dedup / own-UTC-month bound — fix round 7). NEVER targeted (the
        driver would honestly report them ``already_current`` forever while
        the stale stamp persisted — all refuter-confirmed non-convergence
        classes), counted + logged + surfaced on the report/Telegram honesty
        heads instead. Repair: a value-poisoned row needs a manual partition
        fix (``lake_heal`` is strictly row-removing, never value-editing); a
        driver-invisible dup/foreign row is ``lake_heal``'s job.
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
        driver's zero-padded reader can never open (``2024/1.parquet``),
        PLUS (fix round 8) canonical-NAMED entries that are not regular
        files (a dataset-style directory ``2024/01.parquet/`` — probe-readable
        by polars yet invisible to the driver's ``is_file()``-gated reader) —
        each skipped + logged, never targeted; repair is
        :mod:`brutex.data.lake_heal`'s job (or a rename to the canonical
        zero-padded form / a move into a regular month file for the
        driver-unreachable classes).
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
    #: Decoded option contracts excluded by the read-scope gate at ENUMERATION
    #: (DEC-UNIVERSE-INDEX-ONLY-001, design §3 R4) — never probed, never
    #: targeted; their stored Greeks stamps stay INERT. Defaulted LAST.
    scope_excluded: int = 0


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
    dashboard job surfaces verbatim. ``cancelled`` (DEC-JOB-CANCEL-CONTROL-001,
    additive + defaulted — every pre-existing constructor call site is
    byte-identical) is ``True`` iff the run stopped at a between-batches
    boundary on the operator's cooperative Stop: an honest PARTIAL, never an
    error — the checkpoint is kept and the next same-plan run resumes.
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
    #: The scan's DEC-UNIVERSE-INDEX-ONLY-001 exclusion census (design §3 R4)
    #: — surfaced on the report / Telegram summary / CLI stderr. Defaulted
    #: LAST so every existing constructor call site stays valid.
    scope_excluded: int = 0
    cancelled: bool = False

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
            "scope_excluded": self.scope_excluded,
            "cancelled": self.cancelled,
        }


def checkpoint_path(lake_root: Path) -> Path:
    """The durable checkpoint sidecar path under *lake_root* — pure, O(1)."""
    return lake_root / CHECKPOINT_DIR_NAME / CHECKPOINT_FILE_NAME


def _touch_revision_marker(lake_root: Path, *, backfill_id: str, bars_written: int) -> None:
    """Move the cross-process lake-revision token after a bar-writing batch.

    A thin delegator into the COMMON
    :func:`~brutex.data.greeks_revision.touch_greeks_revision_marker` (fix
    round 8 — the touch is shared with the DEC-CLI-GREEKS-ENRICH-001 enrich
    pass, the second sanctioned ledger-less Greeks-overlay writer), keeping
    the backfill's payload keys + its
    ``greeks_backfill.revision_marker_failed`` best-effort WARN byte-identical.
    O(1) per batch.
    """
    touch_greeks_revision_marker(
        lake_root,
        fields={"backfill_id": backfill_id, "bars_written": bars_written},
        failure_event="greeks_backfill.revision_marker_failed",
    )


def _marker_last_writer(marker: Path, *, size: int) -> str:
    """Best-effort LAST-writer identity of the overlay freshness marker.

    The marker payload carries its writer's identity (the backfill stamps
    ``backfill_id``; the DEC-CLI-GREEKS-ENRICH-001 enrich pass stamps
    ``writer`` — see :func:`_touch_revision_marker` /
    :func:`brutex.data.greeks_revision.touch_greeks_revision_marker`), and the
    file is replaced whole (atomic ``os.replace``), so the payload always
    names the LAST writer. Anything unreadable — oversized (the checkpoint's
    own :data:`~brutex.engine.sweep_checkpoint.MAX_CHECKPOINT_BYTES` small-JSON
    sanity cap, reused), unparseable (a nesting bomb's ``RecursionError``
    included — the round-2 checkpoint precedent), non-object, or missing/
    non-string identity — reads the CONSERVATIVE ``"unknown"``: an unknown
    writer is treated as FOREIGN by :func:`_checkpoint_superseded_reason`,
    costing at worst a fresh (correct-by-stamps) start, never a wrongly
    adopted checkpoint. O(1) — one bounded read of a ~200-byte sidecar.
    """
    if size > MAX_CHECKPOINT_BYTES:
        return "unknown"
    try:
        payload = json.loads(read_regular_text(marker))
    except (OSError, ValueError, RecursionError):
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    writer = payload.get("backfill_id") or payload.get("writer")
    return writer if isinstance(writer, str) and writer else "unknown"


def _checkpoint_superseded_reason(
    lake_root: Path, *, backfill_id: str, recorded_at: float
) -> str | None:
    """Why a surviving checkpoint must self-ignore — or ``None`` to adopt it.

    Fix round 10 (refuter-confirmed MAJOR): the round-4 rate-VALUE fingerprint
    in the ``backfill_id`` attests only that THIS plan's rate equals the
    CHECKPOINT's rate — it structurally cannot see an INTERVENING overlay
    writer between the checkpoint's ``record`` and this resume (the rate
    A→B→A round trip: a second force run under a revised rate whose errored
    first batch FREEZES the old checkpoint while genuinely rewriting the
    recorded prefix, or a ``brutex enrich-greeks`` / pull-tail enrich pass
    under the changed rate env — either leaves rate-B Greeks on prefix
    contracts whose stamps read CURRENT, and the reverted-rate plan reproduces
    the old id exactly, adopting the stale checkpoint into a false-clean ✅
    ``errored=0`` run). Adoption is therefore gated on the lake's own
    overlay-write freshness signals, both cheap O(1) stats:

    * the SHARED Greeks-overlay freshness marker
      (:func:`revision_marker_path` — every sanctioned ledger-less overlay
      writer touches it after a bar-writing batch/pass, fix rounds 2+8): a
      marker whose mtime is at/after *recorded_at* AND whose last-writer
      identity is NOT this plan's ``backfill_id`` is a foreign overlay write
      (this run's OWN batch touches — which always land after their batch's
      ``record`` — are identity-exempt, so a legitimate same-plan resume
      still adopts);
    * the window ledger (base Parquet + deltas dir): the backfill never
      touches it and wet runs hold the lake-root single-flight, so ANY
      movement at/after *recorded_at* means a pull ran in between — and a
      pull's post-pull tail re-enriches the overlay under the CURRENT rate
      env (its ledger writes land BEFORE the tail, so even a tail that
      crashed mid-pass is signalled).

    ``>=`` (not ``>``) on both comparisons and ``unknown``-writer-as-foreign
    bias every ambiguity toward the FRESH start — correct-by-stamps and, in
    force mode, a cheap value-gated restatement — never toward adopting a
    possibly-poisoned resume point (Golden Rule #1). A signal path that
    cannot be statted at all (a non-ENOENT ``OSError``) is likewise a refusal
    reason, never a silent adopt. HONEST residue (stated in the module
    docstring): an intervening writer that crashed mid-pass — after partition
    writes but BEFORE its batch/pass-end marker touch, with no ledger
    movement — leaves no signal; the remediation is deleting the checkpoint,
    exactly like the spot-heal residue.
    """
    marker = revision_marker_path(lake_root)
    try:
        marker_stat = os.stat(marker)
    except FileNotFoundError:
        marker_stat = None
    except OSError as exc:
        return f"cannot stat the overlay freshness marker {marker}: {exc}"
    if marker_stat is not None and marker_stat.st_mtime >= recorded_at:
        writer = _marker_last_writer(marker, size=marker_stat.st_size)
        if writer != backfill_id:
            return (
                f"the Greeks-overlay freshness marker was touched by writer {writer!r} "
                f"at/after this checkpoint was recorded (marker mtime "
                f"{marker_stat.st_mtime:.3f} >= checkpoint updated_at {recorded_at:.3f}) "
                "— an intervening overlay writer may have rewritten the recorded "
                "prefix under different inputs"
            )
    newest_ledger: float | None = None
    for target in (ledger_path(lake_root), deltas_dir(lake_root)):
        try:
            mtime = os.stat(target).st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"cannot stat the window ledger at {target}: {exc}"
        newest_ledger = mtime if newest_ledger is None else max(newest_ledger, mtime)
    if newest_ledger is not None and newest_ledger >= recorded_at:
        return (
            f"the window ledger moved at/after this checkpoint was recorded (ledger "
            f"mtime {newest_ledger:.3f} >= checkpoint updated_at {recorded_at:.3f}) — a "
            "pull (whose post-pull tail re-enriches the overlay under the CURRENT rate "
            "env) ran between the crash and this resume"
        )
    return None


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
    stamp persisting forever. A canonical-NAMED entry that is NOT a regular
    file rides the SAME reject list (fix round 8, refuter-confirmed): a
    pyarrow/Spark dataset-style DIRECTORY named ``2024/01.parquet/``
    round-trips the name check AND probes successfully (polars reads the
    parquet file(s) INSIDE a directory), yet the driver's reader gates every
    candidate on ``is_file()`` and skips the month entirely — the identical
    eternal-re-feed false green escaping through filesystem TYPE instead of
    filename shape. The reachability predicate here mirrors
    ``ist_span_partition_files`` EXACTLY: canonical name AND ``is_file()``.
    The caller surfaces the rejects loudly (``probe_failed`` + WARN); repair
    is a rename-to-canonical / move-the-data-into-a-regular-file lake task.

    No lake writer produces non-canonical names or dataset directories; the
    guard is the same foreign-tree tolerance the probe already grants corrupt
    bytes. O(contract's month files) — the ``is_file()`` stat is O(1) per
    candidate.
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
        if not candidate.is_file():
            # Fix round 8 (refuter-confirmed): a canonical-named NON-file (a
            # dataset-style directory `2024/01.parquet/`) is probe-readable
            # but driver-INVISIBLE (`ist_span_partition_files` requires
            # `is_file()`) — targeting it re-fed the contract on EVERY run
            # under an errored=0 ✅/EXIT_OK false green.
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


def _driver_row_view(frame: pl.DataFrame, path: Path) -> pl.DataFrame:
    """The subset of *path*'s rows the enrich driver ACTS on — the read mirror.

    :func:`brutex.data.greeks_enrich.read_option_rows` bounds each month file
    to its OWN UTC ``(year, month)`` (the G-RACE-1 foreign-row guard) and then
    keep-FIRST dedups on ``timestamp`` (the COMMON
    :func:`~brutex.data.lake_read.dedup_bars_frame`) BEFORE the driver ever
    sees a bar. Judging staleness/fixability over any WIDER row set re-targets
    a contract the driver can never change (fix round 7, refuter-confirmed):
    a stale stamp or non-finite price carried ONLY by a dropped
    duplicate-timestamp row drove targeting while the driver saw only the kept
    row — ``changed == 0`` read ``already_current``, zero writes, and the
    contract was re-fed on EVERY run under a ✅/EXIT_OK false green. *path* is
    a canonical ``<yyyy>/<mm>.parquet`` name (:func:`_month_files` rejects the
    rest), so the file's own month is recoverable exactly as the driver
    recovers it. A junk ``timestamp`` dtype raises the polars error to the
    caller (its fail-open arm — the driver ERRORS such a target loudly).
    """
    import polars as pl

    year = int(path.parent.name)
    month = int(path.stem)
    bounded = frame.filter(
        (pl.col("timestamp").dt.year() == year) & (pl.col("timestamp").dt.month() == month)
    )
    view, _dropped = dedup_bars_frame(bounded, source=f"greeks_backfill_probe:{path}")
    return view


def _read_driver_frames(
    path: Path, *, with_stamp: bool
) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """Read *path*'s price(+stamp) columns; return ``(raw, driver_view)``.

    ONE bounded column read per SUSPICIOUS/legacy file — the clean fast path
    never pays it (the bench-pinned single stamp-column read stands). ``None``
    is the FAIL-OPEN verdict on any read trouble (a missing price/stamp
    column, corrupt bytes, a junk ``timestamp`` dtype the month-bound cannot
    evaluate): feed the driver, whose own guards are total — a missing or
    junk-typed column ERRORS the target loudly, never a silent green. A file
    WITHOUT a ``timestamp`` column cannot come from the lake writers and the
    driver errors loudly on it if ever targeted; it is judged over its raw
    rows unchanged (``view == raw`` — no driver view is derivable), which can
    only ever fold it into the LOUD ``stale_unfixable``/target arms, never a
    silent green.
    """
    import polars as pl

    needed: list[str] = list(_PRICE_COLUMNS)
    if with_stamp:
        needed.append("greeks_provenance_id")
    try:
        # The footer probe rides the GUARDED seam (DEC-LAKE-READ-INODE-GUARD-001
        # fix round — the diagnostic-arm class sweep): it opens the SAME inode
        # the guarded read_parquet_regular below opens, and it runs FIRST, so a
        # bare pl.read_parquet_schema(path) here would wedge in C-level
        # fifo_open on a post-enumeration inode swap BEFORE the guard is ever
        # reached. NotRegularFileError is an OSError → the existing fail-open
        # None arm below absorbs it.
        schema = read_parquet_schema_regular(path)
        if any(name not in schema for name in needed):
            return None
        has_timestamp = "timestamp" in schema
        columns = ["timestamp", *needed] if has_timestamp else needed
        # Guarded literal read of ONE concrete month file
        # (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, the #1594 round-15 metachar
        # law — held by construction inside read_parquet_regular — a
        # `[`-carrying contract dir must never glob-read a sibling's rows into
        # this probe; + DEC-LAKE-READ-INODE-GUARD-001 fd validation).
        frame = read_parquet_regular(path, columns=columns)
        view = _driver_row_view(frame, path) if has_timestamp else frame
    except (pl.exceptions.PolarsError, OSError):
        return None
    return frame, view


def _any_row_fixable(rows: pl.DataFrame) -> bool:
    """Whether ANY of *rows* is one the enrich driver can stamp — the IVX-2 twin.

    Mirrors the driver's per-bar predicate EXACTLY: a row with all four prices
    present + finite is FIXABLE (the driver stamps it); a row with a NULL or
    junk-typed price is ALSO treated fixable because the driver ERRORS the
    target LOUDLY on it (``errored`` → ⚠️ head + non-zero CLI exit — never a
    silent green); ONLY the all-prices-present-but-non-finite row is
    driver-SKIPPABLE. ``False`` for an empty *rows* — the callers own the
    no-visible-row posture (fail-open target on a probe/read skew, the
    driver-invisible fold when the view dropped every carrying row).
    """
    for row in rows.select(_PRICE_COLUMNS).iter_rows():
        if any(value is None for value in row):
            return True  # a NULL price ERRORS the target loudly in the driver
        try:
            prices = [float(value) for value in row]
        except (TypeError, ValueError):
            return True  # a junk-typed price ERRORS the target loudly too
        if all(math.isfinite(price) for price in prices):
            return True  # the driver CAN stamp this row — genuinely stale
    return False


def _stale_stamp_pairs(series: pl.Series, *, rate_source: RiskFreeSource) -> list[tuple[int, str]]:
    """The stale ``(pid, reason)`` pairs among *series*' distinct non-null stamps.

    First-seen row order (the census label is the FIRST stale stamp's reason —
    the pre-round-7 tie-break preserved). Both callers gate *series* through
    :func:`_stamp_dtype_poisoned` FIRST (fix round 9), so every value here is
    a Python ``int`` from an integer-dtype column — the ``int()`` coercion is
    pure normalisation and can never raise, and
    :func:`~brutex.data.greeks_enrich.unpack_greeks_provenance` /
    :func:`~brutex.data.greeks_enrich.stamp_is_current_inputs` are total over
    ``int`` (pure bit masking).
    """
    pairs: list[tuple[int, str]] = []
    for value in series.drop_nulls().unique(maintain_order=True).to_list():
        pid = int(value)
        if not stamp_is_current_inputs(pid, rate_source=rate_source):
            stored = unpack_greeks_provenance(pid)
            reason = _STALE_V1 if stored.formula_version != GREEKS_FORMULA_VERSION else _STALE_DRIFT
            pairs.append((pid, reason))
    return pairs


def _stamp_dtype_poisoned(series: pl.Series, *, symbol: str, path: Path) -> bool:
    """``True`` + ONE ``probe_skip`` WARN iff *series*' dtype is non-INTEGER.

    The lake writers only ever produce INTEGER ``greeks_provenance_id``
    columns (:func:`~brutex.data.greeks_enrich.pack_greeks_provenance` packs
    Python ints; the driver's overlay null-fill is ``Int32``), so ANY other
    dtype is a SCHEMA-POISONED column — folded into ``probe_failed`` as ONE
    repair unit (the round-7 precedence), never fed further.

    Fix round 9 (refuter-confirmed): the rounds-1/6 poisoned-column folds
    keyed on the ``int()`` value coercion RAISING
    (``ValueError``/``TypeError``/``OverflowError``), which a NUMERIC-STRING
    utf8 (``"7"``), a Boolean (``int(True) == 1``) or a fractional float
    (``int(258.5)`` truncates) stamp does NOT — the coercible-but-non-integer
    dtype then detonated the un-fenced ``is_in`` driver-view filter in
    :func:`_suspicious_file_verdict` (polars ``InvalidOperationError``:
    ``'is_in' cannot check for List(Int64) values in String data``) and ONE
    junk file killed the WHOLE scan — the DEC-MOVERS-CORRUPT-SKIP-001 class
    this module pledges away; the fractional float instead truncated,
    silently ``is_in``-matched nothing and mis-classified the contract
    ``stale_unfixable`` ("lake repair needed") although the driver's
    ``isinstance(stored_pid, int)`` gate would recompute it outright. Gating
    the DTYPE at the fold makes the downstream expression space total: every
    polars integer dtype (Int8…UInt64, huge UInt64 values included) is
    ``is_in``-safe against the Int64 pid list (probed on the pinned polars)
    and ``int()`` over its values can never raise — so the old value-level
    coercion folds are SUBSUMED, not narrowed.
    """
    if series.dtype.is_integer():
        return False
    _LOG.warning(
        "greeks_backfill.probe_skip",
        symbol=symbol,
        file=str(path),
        dtype=str(series.dtype),
        error="schema-poisoned greeks_provenance_id dtype (non-integer)",
    )
    return True


def _warn_driver_invisible(
    path: Path, *, symbol: str, dropped_stale_stamps: int, dropped_null_stamps: int
) -> None:
    """ONE loud WARN for staleness carried ONLY by driver-invisible rows.

    Fix round 7 (refuter-confirmed): such a stamp can never drive targeting —
    the driver's keep-first timestamp dedup / own-UTC-month bound drops the
    carrying row before its reuse gate ever sees it, so a target on its
    account looped forever under a false-green ✅/EXIT_OK. Repair is
    :mod:`brutex.data.lake_heal`'s intra-file duplicate + foreign-row heal
    (row-REMOVING is in its power — unlike the value-poison class), stated
    here, never a silent re-queue.
    """
    _LOG.warning(
        "greeks_backfill.stale_on_driver_invisible_rows",
        symbol=symbol,
        file=str(path),
        dropped_stale_stamps=dropped_stale_stamps,
        dropped_null_stamps=dropped_null_stamps,
        reason=(
            "stale/null stamp(s) sit ONLY on rows the enrich driver never "
            "sees — its keep-first timestamp dedup / own-UTC-month bound "
            "(read_option_rows) drops them; lake_heal's intra-file duplicate "
            "+ foreign-row heal is the repair, never a re-queue"
        ),
    )


def _suspicious_file_verdict(
    path: Path, *, rate_source: RiskFreeSource, symbol: str, fallback_reason: str
) -> tuple[str | None, bool, bool]:
    """Judge one raw-SUSPICIOUS month file over the DRIVER's row view.

    Called ONLY after the bounded stamp-column probe saw a null or stale stamp
    among the file's PHYSICAL rows. The verdict re-reads the file ONCE
    (timestamp + OHLC + stamp) and answers over :func:`_driver_row_view` — the
    rows the driver actually acts on — so a stamp visible only on a dropped
    duplicate-timestamp / foreign-month row can never drive targeting (fix
    round 7, refuter-confirmed: both escapes looped forever under a
    false-green ✅/EXIT_OK — a kept non-finite row shadowed by a finite dup,
    and a dropped dup carrying the ONLY stale stamp).

    Returns ``(reason, unfixable, probe_failed)``:

    * ``reason`` non-``None`` — TARGET the contract: driver-actionable
      staleness in the view, or the fail-open posture (unreadable price/stamp
      projection, or a probe/read skew where THIS read sees no staleness at
      all — *fallback_reason* names the raw probe's verdict; feeding the
      driver is the safe over-approximation: it errors loudly or restates).
    * ``unfixable`` — staleness exists but the driver cannot act on ANY
      carrying row: non-finite OHLC in the view (fix rounds 4 + 6), or
      staleness ONLY on driver-invisible rows (fix round 7 — WARNed here
      naming ``lake_heal`` as the repair).
    * ``probe_failed`` — the stamp column turned NON-INTEGER between the two
      bounded reads (a concurrent rewrite): the schema-poisoned fold, counted
      by the caller's census discipline (fix round 9 — the dtype gate, BEFORE
      the null branch: a poisoned column is ONE repair unit even when null
      stamps sit alongside, the round-7 precedence; it also fences the
      ``is_in`` driver-view filter below, which detonated on a
      coercible-but-non-integer dtype the old value-level ``int()`` fold
      could not see).
    """
    import polars as pl

    frames = _read_driver_frames(path, with_stamp=True)
    if frames is None:
        return fallback_reason, False, False
    raw, view = frames
    stamp = pl.col("greeks_provenance_id")
    raw_series = raw["greeks_provenance_id"]
    view_series = view["greeks_provenance_id"]
    if _stamp_dtype_poisoned(raw_series, symbol=symbol, path=path):
        return None, False, True
    unfixable = False
    if view_series.null_count() > 0:
        if _any_row_fixable(view.filter(stamp.is_null())):
            return _STALE_NULL, False, False
        # Every view null stamp sits on a driver-skippable row (fix round 4).
        unfixable = True
    view_pairs = _stale_stamp_pairs(view_series, rate_source=rate_source)
    raw_pairs = _stale_stamp_pairs(raw_series, rate_source=rate_source)
    if view_pairs:
        if _any_row_fixable(view.filter(stamp.is_in([pid for pid, _ in view_pairs]))):
            return view_pairs[0][1], False, False
        # Every view row carrying a stale stamp is driver-skippable (round 6).
        unfixable = True
    view_ids = {pid for pid, _ in view_pairs}
    dropped_stale = sum(1 for pid, _ in raw_pairs if pid not in view_ids)
    dropped_nulls = raw_series.null_count() - view_series.null_count()
    if dropped_stale > 0 or dropped_nulls > 0:
        _warn_driver_invisible(
            path,
            symbol=symbol,
            dropped_stale_stamps=dropped_stale,
            dropped_null_stamps=max(dropped_nulls, 0),
        )
        unfixable = True
    if not unfixable:
        # The stamp probe flagged the file but THIS read sees no staleness
        # anywhere (view OR dropped rows) — a skew between the two bounded
        # reads: fail OPEN, feed the driver (restating is a cheap no-op).
        return fallback_reason, False, False
    return None, True, False


def _legacy_file_verdict(path: Path, *, symbol: str) -> tuple[str | None, bool]:
    """Judge one LEGACY (no stamp column) month file — every bar is unstamped.

    Fix round 7 (refuter-confirmed): the unconditional stale-null verdict this
    arm used to return bypassed the rounds-4/6 fixability gate — a legacy
    7-column partition whose EVERY driver-visible row carries a non-finite
    OHLC value was targeted on EVERY run while the driver's per-bar IVX-2
    guard skipped every row: ``changed == 0`` read ``already_current``, zero
    writes, the file stayed 7-column, and the run exited a ✅/EXIT_OK false
    green forever. The legacy arm is moreover the COMMON arm for the
    CLI-``brutex fetch``-only lake (gap b of #1188), which is entirely
    7-column. The same driver-actionability discipline now applies, judged
    over the DRIVER's row view (:func:`_driver_row_view`).

    Returns ``(reason, unfixable)``: ``(_STALE_NULL, False)`` targets (at
    least one driver-actionable row, or the fail-open posture on read
    trouble); ``(None, True)`` folds into ``stale_unfixable`` (every visible
    row skippable, or every row driver-INVISIBLE — the latter WARNed naming
    ``lake_heal``); ``(None, False)`` is the EMPTY partition — nothing the
    driver could ever stamp, honestly nothing to do (a target would loop as a
    ``no_bars`` re-feed forever).
    """
    frames = _read_driver_frames(path, with_stamp=False)
    if frames is None:
        return _STALE_NULL, False  # fail-open: feed the driver — its guards are total
    raw, view = frames
    if raw.height == 0:
        return None, False
    if _any_row_fixable(view):
        return _STALE_NULL, False
    if view.height == 0:
        # Every physical row is driver-INVISIBLE (duplicate-timestamp /
        # foreign-month drops) — lake_heal's repair, never a re-queue.
        _warn_driver_invisible(
            path, symbol=symbol, dropped_stale_stamps=0, dropped_null_stamps=raw.height
        )
    return None, True


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
    driver-unfixable) and ``unfixable_seen`` flags a file whose staleness the
    driver cannot act on. The CLEAN fast path stays ONE bounded
    ``greeks_provenance_id`` column read per month file (bench-pinned); a file
    whose PHYSICAL rows show a null or stale stamp takes ONE further bounded
    read (timestamp + OHLC + stamp) and is judged over the DRIVER's row view
    (:func:`_suspicious_file_verdict` → :func:`_driver_row_view` — the file
    bounded to its own UTC month then keep-first deduped on ``timestamp``,
    EXACTLY ``read_option_rows``'s per-file discipline; fix round 7,
    refuter-confirmed: judging over the RAW row set re-targeted a contract
    forever on a stamp only a dropped duplicate-timestamp row carried while
    the driver read ``already_current`` — the same eternal-re-feed +
    false-green class as rounds 4-6). Staleness carried ONLY by
    driver-invisible rows folds into ``unfixable_seen`` + ONE loud WARN naming
    ``lake_heal`` as the repair; a v1/drift stamp on a driver-FIXABLE view row
    still targets the contract. Short-circuits on the FIRST targeting file. A
    legacy 7-column partition (no ``greeks_provenance_id`` column at all)
    rides the SAME driver-actionability gate (:func:`_legacy_file_verdict` —
    fix round 7 closed its unconditional stale-null bypass of the rounds-4/6
    fixability discipline). A corrupt/unreadable file is counted + logged and
    the probe continues — and so is a SCHEMA-POISONED stamp column: ANY
    non-INTEGER ``greeks_provenance_id`` dtype (utf8 — numeric-string or not,
    Boolean, every float incl. NaN/inf/fractional, …) folds into
    ``probe_failed`` at the :func:`_stamp_dtype_poisoned` DTYPE gate, so ONE
    junk file never kills the scan (the DEC-MOVERS-CORRUPT-SKIP-001 class).
    Fix round 9 (refuter-confirmed) moved this fold from the raising-``int()``
    VALUE gate of rounds 1/6 to the dtype: a coercible-but-non-integer stamp
    (utf8 ``"7"``, Boolean ``True``, a fractional float) sailed past the
    ``ValueError``/``TypeError``/``OverflowError`` trio and detonated the
    un-fenced ``is_in`` in :func:`_suspicious_file_verdict` — see
    :func:`_stamp_dtype_poisoned` for the full class. A poisoned stamp column
    folds into ``probe_failed`` even when null stamps sit alongside — the
    column is ONE repair unit (a deliberate round-7 precedence over the old
    null-branch-first order: feeding the driver a schema-poisoned column
    could never converge either). A genuine code bug elsewhere still
    propagates — the read catch is the narrow movers_resilient
    ``(PolarsError, OSError)`` set, the stamp-column gate the dtype check
    only; healing is :mod:`brutex.data.lake_heal`'s job, never
    re-implemented here. Each probed file fires one liveness
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
            # Guarded literal read (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 —
            # the #1594 round-15 metachar law, held by construction; see
            # _read_driver_frames' twin comment; +
            # DEC-LAKE-READ-INODE-GUARD-001 fd validation).
            series = read_parquet_regular(path, columns=["greeks_provenance_id"])[
                "greeks_provenance_id"
            ]
        except pl.exceptions.ColumnNotFoundError:
            # A legacy 7-column partition — every bar is unstamped; the
            # driver-actionability gate decides (fix round 7).
            legacy_reason, legacy_unfixable = _legacy_file_verdict(path, symbol=symbol)
            if legacy_reason is not None:
                return legacy_reason, files_probed, probe_failed, unfixable_seen
            unfixable_seen = unfixable_seen or legacy_unfixable
            continue
        except (pl.exceptions.PolarsError, OSError) as exc:
            probe_failed += 1
            _LOG.warning(
                "greeks_backfill.probe_skip",
                symbol=symbol,
                file=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if _stamp_dtype_poisoned(series, symbol=symbol, path=path):
            # A schema-poisoned stamp column (utf8 — numeric-string or not,
            # Boolean, any float incl. NaN/inf/fractional): data corruption,
            # not a code bug — count + log + continue, exactly like the
            # unreadable-file arm above (ONE repair unit even when null
            # stamps sit alongside — round-7 precedence; fix round 9 moved
            # the fold from the raising-int() value gate to the DTYPE,
            # closing the coercible-dtype is_in crash class).
            probe_failed += 1
            continue
        raw_pairs = _stale_stamp_pairs(series, rate_source=rate_source)
        if series.null_count() == 0 and not raw_pairs:
            continue  # fully current — the clean fast path stays ONE stamp read
        fallback_reason = _STALE_NULL if series.null_count() > 0 else raw_pairs[0][1]
        reason, unfixable, failed = _suspicious_file_verdict(
            path, rate_source=rate_source, symbol=symbol, fallback_reason=fallback_reason
        )
        if failed:
            # The stamp column turned uncoercible between the two bounded
            # reads (a concurrent rewrite) — already WARNed by the verdict.
            probe_failed += 1
            continue
        if reason is not None:
            return reason, files_probed, probe_failed, unfixable_seen
        unfixable_seen = unfixable_seen or unfixable
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
    scope: FeedScope | None = None,
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
    futures_skipped = undecodable = live_skipped = scope_excluded = 0
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
        if scope is not None and not scope_admits_fno_contract(exchange, parts.underlying, scope):
            # DEC-UNIVERSE-INDEX-ONLY-001 (design §3 R4): a scoped-out stored
            # contract is never probed / never targeted — a bulk backfill must
            # not spend hours re-probing ~10⁵ stored stock contracts the
            # operator has scoped out. Counted honestly; stamps stay INERT.
            # `scope=None` (the §2.4 read-default law) is byte-identical.
            scope_excluded += 1
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
            # Fix rounds 5 + 8 (both refuter-confirmed): an in-range
            # NON-canonical month name ("2024/1.parquet") or a canonical-named
            # NON-regular-file (a dataset-style DIRECTORY "2024/01.parquet/")
            # is an entry the driver's reader (ist_span_partition_files —
            # zero-padded names, is_file()-gated) can NEVER open —
            # probing/targeting it would re-select the contract on EVERY run
            # while the wet run wrote zero bars and exited a false green (the
            # stale stamp persisting forever, the census never converging).
            # Fold into probe_failed + WARN loudly: repair is a
            # rename-to-canonical / move-into-a-regular-file lake task, never
            # a re-queue.
            probe_failed += len(driver_unreachable)
            _LOG.warning(
                "greeks_backfill.month_file_driver_unreachable",
                symbol=symbol,
                exchange=exchange,
                count=len(driver_unreachable),
                files=[str(path) for path in driver_unreachable[:5]],
                reason=(
                    "non-canonical month name or non-regular-file entry — "
                    "the enrich driver reads ONLY canonical zero-padded "
                    "<yyyy>/<mm>.parquet REGULAR files; rename the file to "
                    "its canonical form (a dataset-style directory needs its "
                    "parquet data moved into a canonical month file)"
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
                # The contract's ONLY staleness is rows the driver cannot act
                # on — driver-SKIPPABLE (non-finite OHLC: null-stamped, fix
                # round 4; stale NON-null-stamped, fix round 6; a legacy
                # 7-column file's rows, fix round 7) or driver-INVISIBLE
                # (dropped duplicate-timestamp / foreign-month rows, fix
                # round 7) — all refuter-confirmed non-convergence classes:
                # targeting would re-feed forever while the driver honestly
                # reported already_current. Count + log LOUDLY — repair is a
                # lake-repair task, never silently re-queued.
                stale_unfixable += 1
                _LOG.warning(
                    "greeks_backfill.contract_unfixable",
                    symbol=symbol,
                    exchange=exchange,
                    reason=(
                        "stale/null-stamped row(s) the enrich driver cannot "
                        "act on — non-finite OHLC it skips by design (IVX-2), "
                        "or rows its keep-first timestamp dedup / "
                        "own-UTC-month bound never sees; lake repair needed"
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
        scope_excluded=scope_excluded,
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
        scope_excluded=scan.scope_excluded,
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
    dashboard headline + CLI print surface verbatim (fix rounds 4-7 — the
    census must never read green while a stale/null stamp persists on a row
    the driver cannot act on or never sees).
    """
    if stale_unfixable <= 0:
        return ""
    return (
        f"; {stale_unfixable} contract(s) UNFIXABLE (stale/null-stamped row(s) "
        "the enrich driver cannot act on: non-finite OHLC it skips by design, "
        "or rows its dedup/month-bound read never sees — lake repair needed, "
        "excluded from targeting)"
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
        scope_excluded=scan.scope_excluded,
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
    should_stop: Callable[[], bool] | None = None,
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
    checkpoint is ignored LOUDLY (fresh start — correct via the stamps), and
    so is a SAME-id checkpoint older than the lake's overlay-write freshness
    signals — a foreign-writer marker touch or any window-ledger movement
    at/after its ``updated_at`` (fix round 10, the rate A→B→A round-trip /
    intervening-enrich-pass class; :func:`_checkpoint_superseded_reason` —
    the stale FILE is deleted at the trip so a later frozen-then-crashed run
    can never resurrect it); a
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
    should_stop
        Optional zero-arg cooperative-cancel probe (DEC-JOB-CANCEL-CONTROL-001
        — the Greeks-backfill arm of the DEC-JOB-COOPERATIVE-CANCEL-001
        mechanism; the dashboard threads the job's
        :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.cancel_requested`).
        Consulted at the BETWEEN-BATCHES boundary ONLY — before each batch's
        driver call, never mid-batch — so the in-flight batch always completes
        its driver call, its clean-prefix checkpoint ``record`` and its
        freshness-marker touch first: the durable state at a stop is
        bit-identical to a graceful stop at that boundary. ``True`` stops the
        run COOPERATIVELY: the loop breaks, the clean-completion checkpoint
        unlink below is SKIPPED (the advisory checkpoint stays frozen at the
        last fully-clean prefix, so the next same-plan run ADOPTS it — the
        run's own marker touches are identity-exempt in the round-10 adoption
        gate — and fast-resumes; correctness never depends on it, the stamps
        make resume correct regardless), and the report returns with
        ``cancelled=True`` + an honest CANCELLED note — never a raise, never
        an error state, never a CRITICAL alert. HONEST latency (Golden Rule
        #10): cooperative — cancel lands only once the in-flight batch
        (default 25 contracts) finishes; a wedged driver call ignores it (the
        stall watchdog stays that class's honest terminal). ``None`` (the
        default — every pre-existing caller) is byte-identical to today.

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
    loaded: SweepCheckpoint | None = None
    try:
        loaded = store.load()
    except SweepCheckpointError as exc:
        _LOG.warning(
            "greeks_backfill.checkpoint_ignored",
            path=str(store.path),
            error=str(exc),
            action="fresh start (correct via the stamps)",
        )
    if loaded is not None:
        # Adoption freshness gate (fix round 10, refuter-confirmed MAJOR): the
        # rate-value fingerprint in the id cannot see an INTERVENING overlay
        # writer (the rate A→B→A round trip / an enrich pass under a changed
        # rate env), so a checkpoint older than the lake's own overlay-write
        # signals self-ignores LOUDLY — see _checkpoint_superseded_reason.
        reason = _checkpoint_superseded_reason(
            lake_root, backfill_id=plan.backfill_id, recorded_at=loaded.updated_at
        )
        if reason is not None:
            store.discard_adoption()
            # Delete the stale FILE too (best-effort): leaving it on disk
            # re-opens a side door — if THIS fresh run's first batches freeze
            # on an errored contract (round 3) while writing bars (an OWN-id
            # marker touch), the NEXT resume would read the marker as
            # identity-exempt and adopt the STALE checkpoint after all,
            # skipping an errored/rewritten prefix. A failing unlink logs and
            # continues (the sidecar is advisory); the first advancing
            # ``record`` overwrites the file atomically anyway.
            try:
                store.path.unlink(missing_ok=True)
            except OSError as exc:
                _LOG.warning(
                    "greeks_backfill.checkpoint_cleanup_failed",
                    path=str(store.path),
                    error=str(exc),
                )
            _LOG.warning(
                "greeks_backfill.checkpoint_ignored",
                path=str(store.path),
                error=reason,
                action="fresh start (correct via the stamps)",
            )
    resumed_from = store.next_index
    total = len(plan.targets)
    tallies = _RunTallies()
    batches = 0
    completed = resumed_from
    cancelled = False
    checkpoint_survives = False
    for batch_start in range(resumed_from, total, batch_size):
        if should_stop is not None and should_stop():
            # Cooperative operator cancel (DEC-JOB-CANCEL-CONTROL-001): probed
            # at the BETWEEN-BATCHES boundary ONLY, so the in-flight batch has
            # already landed its driver writes + checkpoint record + marker
            # touch — the durable state is bit-identical to a graceful stop at
            # this boundary. Deliberately NO checkpoint write here (nothing new
            # to record) and NO unlink below (the frozen last-clean-prefix
            # checkpoint is the resume accelerator). One loud WARN, then the
            # honest cancelled report — never a raise (contrast the pull
            # spines' PullCancelled, which must propagate past nested layers;
            # this flat loop returns gracefully like the pipeline's
            # stopped_after verdict).
            cancelled = True
            # Honest checkpoint state (hostile-review fix, #1748): "kept" is
            # claimed only when a frozen file ACTUALLY survives on disk — a
            # cancel before the first clean record (or after an errored-prefix
            # freeze / a failed record) has no file, and says so. Probed ONCE
            # here and reused by the report note below (nothing touches the
            # file between this break and the note — the clean-completion
            # unlink is skipped on the cancelled path).
            checkpoint_survives = store.path.exists()
            _LOG.warning(
                "greeks_backfill.cancelled",
                completed=completed,
                total=total,
                batches=batches,
                checkpoint=(
                    "kept (frozen at the last clean prefix) — the next run resumes"
                    if checkpoint_survives
                    else "none recorded — the next run re-scans (correct via the stamps)"
                ),
            )
            break
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
    if not cancelled:
        # Clean completion: the stamps are the durable done-record — delete the
        # advisory checkpoint so a later (different-plan) run never even reads
        # it. A CANCELLED run skips this on purpose (DEC-JOB-CANCEL-CONTROL-001):
        # the checkpoint stays frozen at the last fully-clean prefix so the next
        # same-plan run adopts it and fast-resumes (own-plan marker touches are
        # identity-exempt in the round-10 adoption gate).
        try:
            store.path.unlink(missing_ok=True)
        except OSError as exc:
            _LOG.warning(
                "greeks_backfill.checkpoint_cleanup_failed",
                path=str(store.path),
                error=str(exc),
            )
    tallies_clause = (
        f"{tallies.enriched} contract(s) "
        f"re-stamped v2, {tallies.already_current} already current, "
        f"{tallies.errored} errored (re-runnable); {tallies.bars_written} bars written"
        + (f"; resumed from target {resumed_from}" if resumed_from > 0 else "")
        + _unfixable_note_clause(plan.scan.stale_unfixable)
    )
    if cancelled:
        # The honest cancelled headline (surfaced verbatim by the dashboard
        # job's _finish_detail): the stop point + the resume promise FIRST,
        # then the same tallies clause a completed run reports. The resume
        # clause is BRANCHED on the on-disk truth (hostile-review fix, #1748):
        # "checkpoint kept" describes the unlink-skip above and is claimed
        # ONLY when a frozen file actually survives — on a cancel before the
        # first clean record there is simply no file, and that arm says so
        # honestly. Resume stays correct via the stamps either way (the
        # checkpoint is only the accelerator).
        resume_clause = (
            "resumable (checkpoint kept; re-run to resume)"
            if checkpoint_survives
            else "resumable (no checkpoint recorded — the re-run re-scans; resume "
            "correct via stored stamps)"
        )
        note = (
            f"Greeks backfill {plan.backfill_id[:12]} CANCELLED by operator at "
            f"contract {completed}/{total} — {resume_clause}: {tallies_clause}"
        )
    else:
        note = f"Greeks backfill {plan.backfill_id[:12]}: {tallies_clause}"
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
        scope_excluded=plan.scan.scope_excluded,
        cancelled=cancelled,
    )
    _LOG.info("greeks_backfill.done", **result.to_dict())
    return result
