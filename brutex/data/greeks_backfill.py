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
  real resume mechanism — a crash-restart re-scans, completed contracts no
  longer classify stale, the plan shrinks, the ``backfill_id`` changes and the
  stale checkpoint is ignored; in ``force`` mode the plan is stamp-INdependent
  and stable, so the checkpoint genuinely resumes at ``next_index``.
* BLIND SPOT, stated everywhere: the stamp prefilter cannot see a rate/spot
  VALUE drift under an unchanged ``(rate_source_id, RISK_FREE_SOURCE_VERSION)``
  — ``force=True`` routes every contract through the driver's VALUE-checked
  gate, which catches exactly that class.
* No wall-clock SLA is claimed — the first wet pass is O(stale bars) kernel
  compute + O(changed partitions) latest-wins rewrites.

ZERO engine impact: nothing under :mod:`brutex.engine` changes semantics (only
the cycle-free checkpoint store is imported); the F&O lake stays
REFERENCE_ONLY; spot INDEX partitions are never written.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
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
from brutex.options.risk_free import RISK_FREE_SOURCE_VERSION

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
        Month files the probe could not read (corrupt Parquet / I/O error) —
        skipped + logged; repair is :mod:`brutex.data.lake_heal`'s job.
    files_probed
        Month files whose stamp column was actually read (the O(files) cost,
        stated honestly).
    """

    targets: tuple[GreeksEnrichTarget, ...]
    stale_null: int = 0
    stale_v1: int = 0
    stale_inputs_drift: int = 0
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
        ``sha256`` over the formula version ‖ rate-source id ‖
        ``RISK_FREE_SOURCE_VERSION`` ‖ ``force`` ‖ ``expired_only`` ‖ the sorted
        target keys — ANY input or plan-set change yields a different id, so a
        stale checkpoint self-ignores (the ``sweep_id`` guard of
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
        On a non-float or negative override — fail-loud (the #1360 contract).
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
    if parsed < 0:
        msg = f"{BACKFILL_PAUSE_ENV}={parsed} must be >= 0"
        raise ValueError(msg)
    return parsed


def _today_ist() -> date:
    """Today's IST calendar date (fixed +05:30 — the lake_heal idiom)."""
    return datetime.now(UTC).astimezone(_IST).date()


def _month_files(minute_dir: Path) -> list[tuple[int, int, Path]]:
    """The contract's ``<yyyy>/<mm>.parquet`` month files, ascending.

    Non-numeric AND numeric-but-out-of-calendar-range directory / file names
    are skipped (a foreign tree never raises): a ``13.parquet`` / ``0.parquet``
    or a year outside ``[1, 9998]`` would make :func:`_contract_window`'s
    :class:`datetime.date` construction raise ``ValueError`` and kill the WHOLE
    scan (the December wrap computes ``year + 1``, hence the ``9998`` cap —
    ``date.max.year - 1``). No lake writer produces such names; the guard is
    the same foreign-file tolerance the probe already grants corrupt bytes.
    O(contract's month files).
    """
    out: list[tuple[int, int, Path]] = []
    max_window_year = date.max.year - 1  # _contract_window's December wrap needs year + 1
    for candidate in minute_dir.glob("*/*.parquet"):
        try:
            year = int(candidate.parent.name)
            month = int(candidate.stem)
        except ValueError:
            continue
        if not (1 <= month <= _DECEMBER) or not (date.min.year <= year <= max_window_year):
            continue
        out.append((year, month, candidate))
    out.sort()
    return out


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


def _probe_contract_staleness(
    files: list[tuple[int, int, Path]],
    *,
    rate_source: RiskFreeSource,
    symbol: str,
    per_file_pause: float,
    sleep: Callable[[float], None],
) -> tuple[str | None, int, int]:
    """Probe one contract's month files for a stale stamp — bounded column reads.

    Returns ``(stale_reason, files_probed, probe_failed)`` where
    ``stale_reason`` is one of :data:`_STALE_NULL` / :data:`_STALE_V1` /
    :data:`_STALE_DRIFT` or ``None`` (every probed stamp current).
    Short-circuits on the FIRST stale file. A legacy 7-column partition (no
    ``greeks_provenance_id`` column at all) classifies :data:`_STALE_NULL`. A
    corrupt/unreadable file is counted + logged and the probe continues — and
    so is a SCHEMA-POISONED stamp column (a wrong-dtype ``greeks_provenance_id``
    like utf8, or a non-null NaN float — values the ``int()`` coercion rejects
    with ``ValueError``/``TypeError``): both fold into ``probe_failed`` so ONE
    junk file never kills the whole scan (the DEC-MOVERS-CORRUPT-SKIP-001
    class). A genuine code bug elsewhere still propagates — the read catch is
    the narrow movers_resilient ``(PolarsError, OSError)`` set, the value
    catch only the coercion pair; healing is :mod:`brutex.data.lake_heal`'s
    job, never re-implemented here. Each probed file fires one liveness
    :func:`~brutex.observability.progress_beat.emit_progress_beat` (a no-op
    outside a dashboard job) so the O(files) probe never trips the pull stall
    watchdog (DEC-PIPELINE-STALL-HEARTBEAT-001).
    """
    import polars as pl

    files_probed = 0
    probe_failed = 0
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
            return _STALE_NULL, files_probed, probe_failed
        except (pl.exceptions.PolarsError, OSError) as exc:
            probe_failed += 1
            _LOG.warning(
                "greeks_backfill.probe_skip",
                symbol=symbol,
                file=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        try:
            if series.null_count() > 0:
                return _STALE_NULL, files_probed, probe_failed
            for value in series.unique().to_list():
                pid = int(value)
                if not stamp_is_current_inputs(pid, rate_source=rate_source):
                    stored = unpack_greeks_provenance(pid)
                    if stored.formula_version != GREEKS_FORMULA_VERSION:
                        return _STALE_V1, files_probed, probe_failed
                    return _STALE_DRIFT, files_probed, probe_failed
        except (ValueError, TypeError) as exc:
            # A schema-poisoned stamp column (utf8 "poison", a NaN float, …):
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
    return None, files_probed, probe_failed


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
    stale_null = stale_v1 = stale_drift = current = no_files = 0
    probe_failed = files_probed = 0
    for _parts, exchange, symbol in decoded:
        if limit is not None and len(targets) >= limit:
            break
        # Liveness beat per considered contract (DEC-PIPELINE-STALL-HEARTBEAT-001):
        # covers the force / no-files arms the per-file probe beat never reaches.
        # No-op outside a dashboard job; best-effort; O(1).
        emit_progress_beat()
        minute_dir = lake_root / "bars" / exchange / FNO_CANDLE_LAKE_SEGMENT / symbol
        files = _month_files(minute_dir / _MINUTE_INTERVAL)
        if not files:
            no_files += 1
            continue
        start, end = _contract_window(files)
        if force:
            targets.append(
                GreeksEnrichTarget(exchange=exchange, symbol=symbol, start=start, end=end)
            )
            continue
        reason, probed, failed = _probe_contract_staleness(
            files,
            rate_source=rate_source,
            symbol=symbol,
            per_file_pause=per_file_pause,
            sleep=sleep,
        )
        files_probed += probed
        probe_failed += failed
        if reason is None:
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


def plan_greeks_backfill(
    scan: BackfillScan,
    *,
    rate_source: RiskFreeSource,
    force: bool,
    expired_only: bool,
) -> BackfillPlan:
    """Freeze *scan* into a content-addressed :class:`BackfillPlan`.

    The ``backfill_id`` hashes every input that shapes the run (formula
    version, rate-source id, rate-source version, the two scan modes, the
    sorted target keys), so ANY change — a formula bump, a different rate CSV
    kind, a shrunk/grown plan — yields a NEW id and a prior checkpoint
    self-ignores. Deterministic: the same scan + inputs always produce the
    same id (pinned by test). O(targets).
    """
    hasher = hashlib.sha256()
    head = (
        f"{GREEKS_FORMULA_VERSION}|{rate_source_id_for(rate_source)}|"
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


def dry_run_backfill_report(plan: BackfillPlan) -> GreeksBackfillReport:
    """The INERT dry-run report — scan census only, zero writes/checkpoint/alerts."""
    scan = plan.scan
    note = (
        f"DRY RUN — would re-enrich {len(plan.targets)} contract(s) "
        f"({scan.stale_null} unstamped, {scan.stale_v1} v1, "
        f"{scan.stale_inputs_drift} inputs-drift"
        f"{'; force — stamp probe skipped' if plan.force else ''}); "
        "zero writes, zero checkpoint, zero alerts"
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
    with ``changed == 0`` ⇒ no write). A corrupt / foreign-``backfill_id``
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
        with — the ``backfill_id`` binds it).
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
        On an explicit *batch_size* ``< 1`` or *pause_s* ``< 0``.
    """
    if batch_size is None:
        batch_size = resolve_backfill_batch_size()
    elif batch_size < 1:
        msg = f"batch_size must be >= 1, got {batch_size}"
        raise ValueError(msg)
    if pause_s is None:
        pause_s = resolve_backfill_pause_s()
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
        probe_failed=plan.scan.probe_failed,
        resumed_from=resumed_from,
        dry_run=False,
        note=note,
    )
    _LOG.info("greeks_backfill.done", **result.to_dict())
    return result
