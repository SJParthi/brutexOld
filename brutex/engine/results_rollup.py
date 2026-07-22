"""Persisted cross-store top-K ``/results`` rollup (DEC-RESULTS-TOPK-ROLLUP-001).

The ``/results`` legacy path (``load_sweep_scan``) is ``O(stores × rows)`` cold
(measured 23.7 s at 1000 stores / 1M finalist rows) and its warm rank is
O(total rows) (measured 904 ms at 1M rows). This module owns the durable
**rollup sidecar** that bounds BOTH: a preset-agnostic top-K UNION superset of
every serveable ordering axis, persisted as a :class:`ResultsStore`-format
Parquet (``topk.parquet``) plus a stamp-LAST JSON sidecar (``topk.stamp.json``)
at the SIBLING root ``<lake>/sweeps_rollup/`` — the exact ``sweep_runs/``
Ruling-4 precedent — so the unfiltered ``/results`` poll is served from
O(K-union) rows regardless of store count.

Why the SIBLING root (ratified-with-evidence — flagged for the coordinator):
``lake_revision.sweeps_revision`` digests the sweeps root's own mtime + every
first/second-level directory mtime, so an IN-TREE rollup write (tmp create +
``os.replace``) would MOVE the very token the rollup is stamped with — a
permanent self-invalidation loop. The sibling root is invisible to the
revision digest, to the ``*/*/*.parquet`` store-discovery glob, AND to the
sweep-verify walks (``root.glob("**/*.parquet")`` with root = the sweeps dir)
— all three pinned by test.

Row content (the preset-agnostic superset)
------------------------------------------
Kept rows = the union, over the full deduped row set, of the exact total-order
top-K (K = :data:`ROLLUP_K` = 1000) prefixes of every serveable ordering axis
— :func:`union_axes` yields 19 today: the 14 ``RANKABLE_COLUMNS`` DESC
(run_id-ASC tie — the ``ResultsStore.top_k`` two-pass law),
``max_drawdown_pct`` ASC (the ``min_dd_first`` preset's primary key over
/results rows — its other key parts are constant-filled inert axes), and the
four NULLABLE DEC-METRICS-* preset axes ``union_axes`` self-extended to
include since #1699 (``log_growth_at_half_kelly`` · ``sl_hit_rate`` ·
``trailing_sharpe_ratio`` · ``decay_weighted_net_paisa`` — probed per-preset
directions; they do NOT dedupe into the 14) — plus the ``run_id``-ASC prefix
(the ``composite`` preset degenerates to the stable run_id pre-sort because
its axes are inert on /results; also generic tie coverage). The axis set is
DERIVED (:func:`union_axes`), never
hand-listed: every non-inert :class:`~brutex.ranking.presets.RankRecord` axis
must be a persisted :class:`ResultRow` field (else a LOUD
:class:`RollupRefusalError` — a future de-inerting forces a new union axis or
a refusal, never a silently-wrong superset), and each preset's direction is
PROBED from its own key function. **The prefix property:** every serveable
ordering is a strict total order (value key + run_id tie); the top-N
(N ≤ MAX_TOP_N = 500 ≤ K) of a total order over the FULL set is contained in
that order's top-K prefix, which the union contains verbatim — so ranking the
union under the SAME order yields the identical top-N sequence
(golden/property-pinned).

Maintenance hooks + the BLOCKER-1 fence
---------------------------------------
* **Hook A** — :class:`RollupMergeHook`, called by the grid drivers at the
  tuple-COMPLETION seams (sequential / supervised / the parallel parent's
  contiguous apply point) and by the Stage-3 pass end. The ENTIRE merge body
  runs inside ONE broad fence (``KeyboardInterrupt``/``SystemExit``/
  ``MemoryError`` re-raise carve-out — the ``results_depth_pass`` idiom): ANY
  failure logs ONE ``results_rollup.merge_failed``, latches rollup-merge OFF
  for the remainder of the launch, best-effort stamps ``conflict_suspect``,
  and NEVER re-raises into the grid — a display optimization must never abort
  a 2-4 day grid (regression-pinned: an injected merge failure mid-grid still
  completes the grid with identical sweep results).
* **Hook B** — :func:`rebuild_rollup` at ``cmd_sweep_all`` run-end (fenced in
  the CLI): ONE full legacy-loader fold — the exactness anchor (clears
  ``conflict_suspect``, restores ``total_rows_exact`` + the legacy
  first-wins/most-informed row content).
* **Hook C** — :func:`rebuild_rollup_from_scan_best_effort` on the serve-time
  fallback path: the route already built the full legacy ``SweepScan``; the
  rollup is written FROM it as a byproduct, so the system converges with
  Hooks A/B as pure latency optimizations.

Honest envelope (Golden Rule #10)
---------------------------------
A rollup-served poll is NOT literal O(1): the serve-time classification pays
an O(stores) stat-level glob per level-1 miss (the same walk legacy discovery
pays) plus — on the stale-serve arm — an O(|stamped stores|) re-stat, and a
miss pays ONE bounded O(K-union) Parquet load; "O(1) repeat poll" is scoped
to AFTER grid settle (the level-2 payload cache). The incremental merge's
``total_rows`` / ``run_id_conflicts`` / stored-VIX-content are documented
approximations restored exactly by Hooks B/C (an IDENTICAL duplicate run_id
whose row was already trimmed out is invisible — ``total_rows`` can overcount
until the next full fold; ``total_rows_exact`` flips ``False`` conservatively
whenever any cross-store collision among KEPT rows was observed). A
same-``(mtime_ns, size)`` in-place store overwrite is the standing stat-token
residual (never WORSE than the incumbent revision cache). The audit-era
resume (:class:`AuditCursor`) reads ONLY the chain tail per merge — O(new
audit lines), killing the O(tuples²) cumulative rescan — and deliberately
does NOT re-verify the prefix hash chain (full-stream integrity remains
``verify_chain``'s job, untouched).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.partition_lock import partition_write_lock

# The three ``_`` names are a FRIEND import (DEC-P0-017 reportPrivateUsage
# convention, per-line ignore): ``trim_to_union`` REUSES ResultsStore.top_k's
# EXACT nulls-last sort-key law (§11 COMMON — the ONE null-placement law of
# DEC-METRICS-KELLY-GROWTH-001, never re-derived), so a NULLABLE union axis (the
# DEC-METRICS-* preset fields #1699 grew ``union_axes()`` to include) ranks
# NULLS-LAST identically to top_k and the per-axis sort never raises the
# ``None``-vs-``None`` TypeError.
from brutex.engine.results_store import (
    _NULLABLE_RANKABLE_FIELDS,  # pyright: ignore[reportPrivateUsage]
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    _field_getter,  # pyright: ignore[reportPrivateUsage]
    _nullable_field_getter,  # pyright: ignore[reportPrivateUsage]
)
from brutex.models import BruteXError

# LogGate lives in a COMMON stdlib-only LEAF (re-exported here for the rollup
# API): the dashboard serve path consumes the SAME class from the leaf, so
# neither side imports the other for it — the CodeQL bidirectional-cycle fix
# (PR #1436; the #1533 leaf-homing pattern).
from brutex.observability.log_gate import LogGate

# The audit module is consumed via the MODULE attribute (late binding), never
# a from-import of `iter_entries`: the scan-split suite's zero-audit-reads spy
# patches `audit.iter_entries`, and an import-time binding here would make the
# spy's verdict depend on module import ORDER (observed: green standalone, red
# after any suite that had already imported this module).
from brutex.orchestrator import audit as _audit
from brutex.orchestrator.audit import GENESIS_PREV_HASH, AuditEntry, AuditError

# ONE import style for the presets module (CodeQL py/import-and-import-from):
# the submodule is bound as a NAME so `code_token` can hash its full source
# via the same binding every constant read uses.
from brutex.ranking import presets as ranking_presets

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Iterable, Iterator, Sequence

    # Engine→engine only. This module deliberately has NO module-level import
    # of brutex.dashboard.* (not even TYPE_CHECKING — the CodeQL cycle fix):
    # the legacy-scan input is typed by the structural `ScanLike` Protocol
    # below, which the dashboard's `SweepScan` satisfies by construction.
    from brutex.engine.sweep_orchestrator import SweepGridConfig, SweepTuple

_LOG = structlog.get_logger("brutex.engine.results_rollup")

__all__ = [
    "AUDIT_RELPATH",
    "CURSOR_START",
    "ROLLUP_DIRNAME",
    "ROLLUP_K",
    "ROLLUP_PARQUET_FILENAME",
    "ROLLUP_SCHEMA_VERSION",
    "ROLLUP_STAGE",
    "ROLLUP_STAMP_FILENAME",
    "SWEEP_START_ACTION",
    "AuditCursor",
    "AuditCursorMismatch",
    "LogGate",
    "RollupMergeHook",
    "RollupRefusalError",
    "RollupStamp",
    "RollupVerdict",
    "ScanLike",
    "classify_rollup",
    "code_token",
    "rebuild_rollup",
    "rebuild_rollup_from_scan_best_effort",
    "rollup_parquet_path",
    "rollup_root",
    "rollup_stamp_path",
    "snapshot_eras",
    "snapshot_eras_with_cursor",
    "trim_to_union",
    "union_axes",
]

#: The rollup's SIBLING-root directory name beside the sweeps tree — the
#: ``sweep_runs/`` Ruling-4 precedent. Deliberately NOT inside ``sweeps/``
#: (the in-tree self-invalidation loop, module docstring).
ROLLUP_DIRNAME: Final[str] = "sweeps_rollup"
#: The persisted union rows (a normal :class:`ResultsStore` Parquet file).
ROLLUP_PARQUET_FILENAME: Final[str] = "topk.parquet"
#: The stamp sidecar, written LAST (the ``top100_artifact`` stamp-last law).
ROLLUP_STAMP_FILENAME: Final[str] = "topk.stamp.json"
#: Per-axis prefix depth. 2× headroom over the /results ``MAX_TOP_N=500``
#: route cap (never a too-shallow union — enforced at import below).
ROLLUP_K: Final[int] = 1000
#: The stamp's schema version — a future shape change moves it (and the
#: :func:`code_token`, which folds it).
ROLLUP_SCHEMA_VERSION: Final[int] = 1
#: The stamp's ``stage`` marker (the stage-stamp law).
ROLLUP_STAGE: Final[str] = "results_topk_rollup"
#: One serialized era record = the 5 coordinate fields + the hash list.
_ERA_RECORD_LEN: Final[int] = 6

#: Where the sweeps tree's audit chain lives under ``<sweeps>`` — the SAME
#: convention `sweep_results_view._AUDIT_RELPATH` pins (drift-pinned by test,
#: deliberately not imported: the dashboard module delegates HERE).
AUDIT_RELPATH: Final[tuple[str, str]] = ("audit", "sweep-all.jsonl")
#: The per-tuple audit action carrying the sweep's reproducibility context.
SWEEP_START_ACTION: Final[str] = "SWEEP_START"

#: Tuple-coordinate key for the era map (mirrors ``sweep_results_view._EraKey``).
EraKey = tuple[str, str, str, str, int]

#: The absent-file stat token (mirrors ``lake_revision.ABSENT_TOKEN`` semantics
#: for the rollup's OWN store/audit tokens — a stable sentinel, never ``None``).
_ABSENT_TOKEN: Final[str] = "absent"  # noqa: S105 — a stat-token sentinel, not a secret (the lake_revision ABSENT_TOKEN twin)

#: The store-discovery glob — MUST mirror ``sweep_results_view._iter_store_paths``
#: (``<root>/<instrument>/<tf>/*.parquet``); drift-pinned by test.
_STORE_GLOB: Final[str] = "*/*/*.parquet"


class RollupRefusalError(BruteXError):
    """A LOUD rollup build refusal — the union superset would be WRONG.

    Raised by :func:`union_axes` when a :class:`~brutex.ranking.presets.RankRecord`
    axis is neither inert (``PRESET_INERT_AXES``) nor a persisted
    :class:`ResultRow` field: a future de-inerting (e.g. P2 populating
    ``composite_score``) must force a new union axis or this refusal — never a
    silently-wrong superset (MAJOR-3, DEC-RESULTS-TOPK-ROLLUP-001).
    """


class AuditCursorMismatch(BruteXError):
    """The audit-chain tail no longer links from the stamped cursor.

    The resume-validity failure of the incremental era fold: the entry at
    ``audit_offset`` does not carry ``prev_hash == audit_last_hash`` (or the
    chain shrank below the offset). The caller degrades to the FULL rescan
    from zero — the original all-or-nothing era-scan contract, preserved.
    """


# ── Import-time K ⊇ MAX_TOP_N assertion (MAJOR-3) ────────────────────────────


def _require_k_covers_max_top_n() -> None:
    """Refuse to import with a union shallower than the route's ``top`` cap.

    The import-time ``assert K >= MAX_TOP_N`` of DEC-RESULTS-TOPK-ROLLUP-001
    (an explicit raise — ``assert`` is stripped under ``-O`` and banned by the
    S101 lint in source): a ``MAX_TOP_N`` bump past :data:`ROLLUP_K` can never
    silently serve a too-shallow union. Function-local dashboard import — the
    dashboard module imports THIS module lazily, so there is no module cycle.
    """
    from brutex.dashboard.sweep_results_view import MAX_TOP_N

    if ROLLUP_K < MAX_TOP_N:
        raise RollupRefusalError(
            f"ROLLUP_K={ROLLUP_K} is shallower than the /results MAX_TOP_N={MAX_TOP_N} — "
            f"the persisted union could not serve the deepest legal leaderboard; raise "
            f"ROLLUP_K (DEC-RESULTS-TOPK-ROLLUP-001 MAJOR-3)"
        )


_require_k_covers_max_top_n()


# ── Paths ────────────────────────────────────────────────────────────────────


def rollup_root(sweeps_root: Path) -> Path:
    """The rollup's sibling root beside *sweeps_root* (``<parent>/sweeps_rollup``)."""
    return sweeps_root.parent / ROLLUP_DIRNAME


def rollup_parquet_path(sweeps_root: Path) -> Path:
    """The union-rows Parquet path for *sweeps_root*'s rollup."""
    return rollup_root(sweeps_root) / ROLLUP_PARQUET_FILENAME


def rollup_stamp_path(sweeps_root: Path) -> Path:
    """The stamp-sidecar path for *sweeps_root*'s rollup."""
    return rollup_root(sweeps_root) / ROLLUP_STAMP_FILENAME


def _audit_chain_path(sweeps_root: Path) -> Path:
    """The sweeps tree's audit-chain path (``<sweeps>/audit/sweep-all.jsonl``)."""
    return sweeps_root.joinpath(*AUDIT_RELPATH)


# ── The derived union-axis enumeration + code_token (MAJOR-3) ────────────────


@dataclass(frozen=True, slots=True)
class UnionAxis:
    """One serveable ordering axis of the union superset.

    ``descending=True`` takes the top-K by *field* DESC (run_id-ASC ties);
    ``False`` by *field* ASC — both via the exact ``ResultsStore.top_k``
    two-pass law (:func:`trim_to_union`).
    """

    field: str
    descending: bool


#: Process-memo for the derived axes + code token (reset seam for tests —
#: `inspect.getsource` is file I/O; the derivation is a pure function of
#: code-level constants, so one compute per process is exact).
_DERIVATION_LOCK: Final[threading.Lock] = threading.Lock()
_UNION_AXES_MEMO: list[tuple[UnionAxis, ...]] = []
_CODE_TOKEN_MEMO: list[str] = []


def _reset_derivation_caches() -> None:
    """TEST SEAM: forget the memoised axis/token derivations (invalidation teeth)."""
    with _DERIVATION_LOCK:
        _UNION_AXES_MEMO.clear()
        _CODE_TOKEN_MEMO.clear()


def _probe_preset_axis_direction(preset_name: str, field_name: str) -> bool | None:
    """Probe whether *preset_name*'s key reads *field_name*, and which way.

    Returns ``None`` when the preset's key is INSENSITIVE to the field (no
    union axis needed), ``True`` for a DESCENDING axis (a larger value ranks
    first under the preset), ``False`` for ASCENDING. The probe compares the
    preset's OWN key outputs for two records differing ONLY in *field_name*
    — derived from the live preset table, never hand-listed (MAJOR-3); the
    preset keys are strict monotone total orders (property-pinned in
    ``brutex.ranking``), so a two-point probe is exact.
    """
    preset = ranking_presets.PRESETS[preset_name]
    base = ranking_presets.RankRecord(
        run_id="a" * 64,
        max_drawdown_pct=1.0,
        composite_score=None,
        max_consecutive_losses=0,
        profit_factor=1.0,
        win_rate=1.0,
        calmar_ratio=1.0,
    )
    kwargs: dict[str, Any] = {field_name: 2.0}
    perturbed = replace(base, **kwargs)
    key_lo = preset.key(base)
    key_hi = preset.key(perturbed)
    if key_lo == key_hi:
        return None
    # sorted(reverse=preset.reverse) puts the LARGER key first when reverse is
    # True and the smaller first otherwise; the union axis takes the prefix
    # from the "best" end, so descending == "the larger field value is best".
    return (key_hi > key_lo) if preset.reverse else (key_hi < key_lo)


def union_axes() -> tuple[UnionAxis, ...]:
    """The DERIVED serveable ordering axes of the union superset (memoised).

    14 ``RANKABLE_COLUMNS`` DESC (the ``rank_by`` axes) plus every probed
    preset axis — 19 axes today: ``max_drawdown_pct`` ASC (the
    ``min_dd_first`` primary) and the four NULLABLE DEC-METRICS-* preset
    fields self-extended since #1699 (``log_growth_at_half_kelly`` ·
    ``sl_hit_rate`` · ``trailing_sharpe_ratio`` · ``decay_weighted_net_paisa``
    — probed per-preset directions; they do NOT dedupe into the 14). The
    run_id-ASC prefix is applied by :func:`trim_to_union` unconditionally (it
    is not a metric axis). Refuses LOUDLY (:class:`RollupRefusalError`) when a
    non-inert ``RankRecord`` axis is not a persisted ``ResultRow`` field.
    """
    with _DERIVATION_LOCK:
        if _UNION_AXES_MEMO:
            return _UNION_AXES_MEMO[0]
    from brutex.dashboard.sweep_results_view import PRESET_INERT_AXES, RANKABLE_COLUMNS

    axes: list[UnionAxis] = [UnionAxis(field=col, descending=True) for col in RANKABLE_COLUMNS]
    seen: set[tuple[str, bool]] = {(a.field, a.descending) for a in axes}
    row_fields = {f.name for f in dataclass_fields(ResultRow)}
    rank_fields = tuple(
        f.name for f in dataclass_fields(ranking_presets.RankRecord) if f.name != "run_id"
    )
    for field_name in rank_fields:
        if field_name in PRESET_INERT_AXES:
            # Constant-filled by the /results projection — ordering-inert by
            # construction; a de-inerting removes it from PRESET_INERT_AXES
            # and lands in the checks below.
            continue
        if field_name not in row_fields:
            raise RollupRefusalError(
                f"RankRecord axis {field_name!r} is neither inert (PRESET_INERT_AXES) nor a "
                f"persisted ResultRow field — the rollup union cannot cover it; add a union "
                f"axis (persist the field) or re-inert it (DEC-RESULTS-TOPK-ROLLUP-001 MAJOR-3)"
            )
        for preset_name in ranking_presets.PRESET_NAMES:
            direction = _probe_preset_axis_direction(preset_name, field_name)
            if direction is None:
                continue
            key = (field_name, direction)
            if key not in seen:
                seen.add(key)
                axes.append(UnionAxis(field=field_name, descending=direction))
    derived = tuple(axes)
    with _DERIVATION_LOCK:
        if not _UNION_AXES_MEMO:
            _UNION_AXES_MEMO.append(derived)
        return _UNION_AXES_MEMO[0]


def code_token() -> str:
    """The V1 stamp token — the sha256 of every serving-law input (memoised).

    Folds (MAJOR-3): the rollup schema version + K, the /results ``MAX_TOP_N``,
    the preset registry (names + reverse flags + the FULL ``presets.py``
    source), ``PRESET_INERT_AXES``, the ``_preset_rank_record`` AND ``_rank_value``
    projection SOURCES (both live OUTSIDE the hashed ``presets.py`` — the
    projections make the inert-axes / raw-rank-value assumptions true, so an
    edit to either must move the token; LOW-1, PR #1436), the
    DERIVED union-axis enumeration, the ``ResultRow`` field schema, and
    ``RANKABLE_COLUMNS``. Source-hashing over-invalidates on comment edits —
    the safe direction.
    """
    with _DERIVATION_LOCK:
        if _CODE_TOKEN_MEMO:
            return _CODE_TOKEN_MEMO[0]
    import inspect

    from brutex.dashboard import sweep_results_view as view

    axis_text = ",".join(
        f"{axis.field}:{'desc' if axis.descending else 'asc'}" for axis in union_axes()
    )
    parts = (
        str(ROLLUP_SCHEMA_VERSION),
        str(ROLLUP_K),
        str(view.MAX_TOP_N),
        ",".join(ranking_presets.PRESET_NAMES),
        ";".join(f"{p.name}:{int(p.reverse)}" for p in ranking_presets.PRESETS.values()),
        hashlib.sha256(inspect.getsource(ranking_presets).encode("utf-8")).hexdigest(),
        ",".join(view.PRESET_INERT_AXES),
        hashlib.sha256(view.preset_rank_record_source().encode("utf-8")).hexdigest(),
        hashlib.sha256(view.rank_value_source().encode("utf-8")).hexdigest(),
        axis_text + ",run_id:asc",
        ",".join(f.name for f in dataclass_fields(ResultRow)),
        ",".join(view.RANKABLE_COLUMNS),
    )
    token = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    with _DERIVATION_LOCK:
        if not _CODE_TOKEN_MEMO:
            _CODE_TOKEN_MEMO.append(token)
        return _CODE_TOKEN_MEMO[0]


# ── The trim (union of exact top-K prefixes) ─────────────────────────────────


def trim_to_union(rows: Iterable[ResultRow], k: int = ROLLUP_K) -> tuple[ResultRow, ...]:
    """Return the union of every axis's exact total-order top-*k* prefix.

    Per axis the ordering is the exact ``ResultsStore.top_k`` two-pass law
    (stable run_id-ASC pre-sort, then ONE stable sort on the axis field), so
    each prefix is the axis's genuine total-order head; the run_id-ASC prefix
    rides unconditionally (the ``composite``-preset / generic-tie cover).
    A **nullable** union axis (the DEC-METRICS-* preset fields
    :func:`union_axes` self-extended to include since #1699) ranks under
    ``ResultsStore.top_k``'s SAME nulls-last law — REUSED via the store's
    ``_nullable_field_getter`` (never re-derived, §11 COMMON): a ``None`` value
    (a pre-extension store's honest unknown or an anchor-less run) ranks AFTER
    every real value in BOTH directions, so it can never occupy a top-k prefix
    slot it could not hold under the preset — and the raw ``getattr`` sort's
    ``None``-vs-``None`` :class:`TypeError` is gone. Every always-concrete axis
    keeps the byte-identical ``_field_getter`` == raw ``getattr`` key, so an
    all-non-``None`` rollup's order is unchanged.
    Kept rows return in run_id-sorted order. O(axes · R log R) time; additions
    only ever DEMOTE rows (trim monotonicity — a row outside every axis's
    top-k can never re-enter under further additions; property-pinned), so
    incremental union-and-trim equals rebuild-from-scratch on the same store
    set. Removals invalidate via the serve ladder → full rebuild.
    """
    ordered_base = sorted(rows, key=lambda r: r.run_id)
    if len(ordered_base) <= k:
        return tuple(ordered_base)
    kept: set[str] = {r.run_id for r in ordered_base[:k]}
    for axis in union_axes():
        # Dispatch the per-axis sort key EXACTLY as ``ResultsStore.top_k`` does
        # (the ONE nulls-last law reused, never copied): a nullable rankable
        # axis rides ``_nullable_field_getter`` (presence-flag NULLS-LAST in
        # both directions); every other axis rides ``_field_getter`` == raw
        # ``getattr`` (byte-identical order).
        key: Callable[[ResultRow], float] | Callable[[ResultRow], tuple[int, float]]
        if axis.field in _NULLABLE_RANKABLE_FIELDS:
            key = _nullable_field_getter(axis.field, descending=axis.descending)
        else:
            key = _field_getter(axis.field)
        ordered = list(ordered_base)
        ordered.sort(key=key, reverse=axis.descending)
        kept.update(r.run_id for r in ordered[:k])
    return tuple(r for r in ordered_base if r.run_id in kept)


# ── Era scan (moved from sweep_results_view) + the AuditCursor tail resume ──


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """Byte-offset resume point after the last consumed committed audit entry.

    ``offset`` is the byte position AFTER that entry's newline; ``last_hash``
    is its ``entry_hash`` (the genesis all-zero hash before any entry). The
    reader-side resume of DEC-RESULTS-TOPK-ROLLUP-001 (the #1533 named
    follow-up): ``verify_chain`` / ``iter_entries`` in
    :mod:`brutex.orchestrator.audit` are byte-untouched — the cursor lives
    HERE so the audit module's frozen mutation profile is preserved.
    """

    offset: int
    last_hash: str


#: The fresh-chain cursor (nothing consumed; genesis linkage).
CURSOR_START: Final[AuditCursor] = AuditCursor(offset=0, last_hash=GENESIS_PREV_HASH)


def _iter_entries_with_cursor(
    path: Path, cursor: AuditCursor
) -> Iterator[tuple[AuditEntry, AuditCursor]]:
    """Yield ``(entry, cursor-after-it)`` for every committed line past *cursor*.

    The tail-only reader: seeks to ``cursor.offset`` and parses forward,
    checking LINKAGE only — the first parsed entry must carry
    ``prev_hash == cursor.last_hash`` and each subsequent entry must link from
    its predecessor (:class:`AuditCursorMismatch` otherwise → the caller's
    full-rescan degrade). Deliberately does NOT recompute ``entry_hash``
    (integrity stays ``verify_chain``'s job — the stated honest limit). An
    unterminated final line is the uncommitted torn tail and is stopped at
    (never consumed — the cursor stays before it, exactly the
    ``iter_entries`` torn-tail law); blank committed lines are skipped but do
    not advance the yielded cursor (re-skipping them on resume is a no-op).
    """
    expect_prev = cursor.last_hash
    with open(path, "rb") as f:
        f.seek(cursor.offset)
        while True:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):
                return
            after = f.tell()
            try:
                line = raw[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AuditError(f"{path}: undecodable audit line at offset {after}") from exc
            if not line:
                continue
            entry = AuditEntry.from_json_line(line)
            if entry.prev_hash != expect_prev:
                raise AuditCursorMismatch(
                    f"audit tail at {path} offset {after} does not link from the stamped "
                    f"cursor (expected prev_hash {expect_prev[:12]}…, got "
                    f"{entry.prev_hash[:12]}…) — full era rescan required"
                )
            expect_prev = entry.entry_hash
            yield entry, AuditCursor(offset=after, last_hash=entry.entry_hash)


def _fold_era_entry(collected: dict[EraKey, set[str]], entry: AuditEntry) -> None:
    """Fold ONE audit entry's ``SWEEP_START`` era signal into *collected*.

    Non-``SWEEP_START`` actions and malformed payloads contribute no signal —
    the exact skip law of the legacy ``_snapshot_eras`` body (moved verbatim).
    """
    if entry.action != SWEEP_START_ACTION:
        return
    payload = entry.payload
    try:
        key: EraKey = (
            str(payload["instrument"]),
            str(payload["tf"]),
            str(payload["direction"]),
            str(payload["mode"]),
            int(payload["k"]),
        )
        snapshot = str(payload["data_snapshot_hash"])
    except (KeyError, TypeError, ValueError):
        return
    collected.setdefault(key, set()).add(snapshot)


def snapshot_eras(sweeps_root: Path) -> dict[EraKey, frozenset[str]]:
    """Best-effort map of tuple coordinates → recorded ``data_snapshot_hash`` eras.

    The COMMON home of the ``/results`` era scan (moved VERBATIM from
    ``sweep_results_view._snapshot_eras``, which now delegates here — behavior
    byte-identical, its test battery green unmodified): one O(audit-lines)
    STREAMING pass over :func:`brutex.orchestrator.audit.iter_entries` with
    the all-or-nothing :class:`AuditError` degrade to ``{}`` (logged as the
    legacy ``sweep_results.era_scan_failed`` event — same name, same fields).
    """
    audit_path = _audit_chain_path(sweeps_root)
    if not audit_path.exists():
        return {}
    collected: dict[EraKey, set[str]] = {}
    try:
        for entry in _audit.iter_entries(audit_path):
            _fold_era_entry(collected, entry)
    except AuditError as exc:
        _LOG.warning("sweep_results.era_scan_failed", audit_path=str(audit_path), error=str(exc))
        return {}
    return {key: frozenset(hashes) for key, hashes in collected.items()}


def snapshot_eras_with_cursor(
    sweeps_root: Path,
) -> tuple[dict[EraKey, frozenset[str]], AuditCursor]:
    """The full era scan PLUS the end-of-chain :class:`AuditCursor`.

    The rollup writers' variant of :func:`snapshot_eras`: the returned cursor
    lets the NEXT incremental merge read only the chain tail. Same
    all-or-nothing degrade (``{}`` + the fresh :data:`CURSOR_START`) on any
    :class:`AuditError` — a degraded cursor forces the next fold to rescan.
    """
    audit_path = _audit_chain_path(sweeps_root)
    if not audit_path.exists():
        return {}, CURSOR_START
    collected: dict[EraKey, set[str]] = {}
    cursor = CURSOR_START
    try:
        for entry, after in _iter_entries_with_cursor(audit_path, CURSOR_START):
            _fold_era_entry(collected, entry)
            cursor = after
    except (AuditError, AuditCursorMismatch, OSError) as exc:
        _LOG.warning("sweep_results.era_scan_failed", audit_path=str(audit_path), error=str(exc))
        return {}, CURSOR_START
    return {key: frozenset(hashes) for key, hashes in collected.items()}, cursor


# ── Stat tokens + store discovery ────────────────────────────────────────────


def _stat_token(path: Path) -> str:
    """``"mtime_ns:size"`` for *path*, or the stable :data:`_ABSENT_TOKEN`.

    Any non-missing-file :class:`OSError` propagates to the caller's fence /
    degrade arm — never a fabricated token.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _ABSENT_TOKEN
    return f"{st.st_mtime_ns}:{st.st_size}"


def current_store_relpaths(sweeps_root: Path) -> tuple[str, ...]:
    """The sorted per-tuple store relpaths under *sweeps_root* (glob only).

    The SAME ``*/*/*.parquet`` discovery law as the legacy scan — an
    O(stores) stat-level directory walk per call (stated honestly wherever
    the serve envelope is described; it is not free).
    """
    if not sweeps_root.is_dir():
        return ()
    return tuple(
        sorted(p.relative_to(sweeps_root).as_posix() for p in sweeps_root.glob(_STORE_GLOB))
    )


# ── The stamp ────────────────────────────────────────────────────────────────


def _dumps(payload: object) -> str:
    """The ONE deterministic stamp JSON law (the ``top100_artifact`` law)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class RollupStamp:
    """The validated ``topk.stamp.json`` content (see the module docstring).

    ``produced_at_utc`` lives ONLY here — the Parquet bytes stay a pure
    function of the kept row set. ``rev`` may be ``""`` when the revision
    probe had no signal at write time (the V2 check then never matches and
    the V3 classification decides).
    """

    schema_version: int
    k: int
    content_hash: str
    produced_at_utc: str
    code_token: str
    rev: str
    audit_chain_token: str
    audit_offset: int
    audit_last_hash: str
    stores: dict[str, str]
    total_rows: int
    total_rows_exact: bool
    store_count: int
    skipped: int
    run_id_conflicts: int
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    directions: tuple[str, ...]
    snapshot_hashes: tuple[str, ...]
    eras: dict[EraKey, frozenset[str]]
    conflict_suspect: bool

    def to_payload(self) -> dict[str, Any]:
        """Render the deterministic stamp payload (the §b shape)."""
        return {
            "stage": ROLLUP_STAGE,
            "schema_version": self.schema_version,
            "k": self.k,
            "content_hash": self.content_hash,
            "produced_at_utc": self.produced_at_utc,
            "code_token": self.code_token,
            "rev": self.rev,
            "audit_chain_token": self.audit_chain_token,
            "audit_offset": self.audit_offset,
            "audit_last_hash": self.audit_last_hash,
            "stores": dict(sorted(self.stores.items())),
            "aggregates": {
                "total_rows": self.total_rows,
                "store_count": self.store_count,
                "skipped": self.skipped,
                "run_id_conflicts": self.run_id_conflicts,
                "instruments": list(self.instruments),
                "timeframes": list(self.timeframes),
                "directions": list(self.directions),
                "snapshot_hashes": list(self.snapshot_hashes),
            },
            "eras": [
                [key[0], key[1], key[2], key[3], key[4], sorted(hashes)]
                for key, hashes in sorted(self.eras.items())
            ],
            "flags": {
                "conflict_suspect": self.conflict_suspect,
                "total_rows_exact": self.total_rows_exact,
            },
        }


def _parse_stamp(raw: bytes) -> RollupStamp | None:
    """Parse + shape-validate stamp bytes; ``None`` on ANY damage (fail closed)."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("stage") != ROLLUP_STAGE:
        return None
    try:
        aggregates = payload["aggregates"]
        flags = payload["flags"]
        eras_raw = payload["eras"]
        stores_raw = payload["stores"]
        if (
            not isinstance(aggregates, dict)
            or not isinstance(flags, dict)
            or not isinstance(eras_raw, list)
            or not isinstance(stores_raw, dict)
        ):
            return None
        eras: dict[EraKey, frozenset[str]] = {}
        for item in eras_raw:
            if (
                not isinstance(item, list)
                or len(item) != _ERA_RECORD_LEN
                or not isinstance(item[5], list)
            ):
                return None
            key: EraKey = (str(item[0]), str(item[1]), str(item[2]), str(item[3]), int(item[4]))
            eras[key] = frozenset(str(h) for h in item[5])
        return RollupStamp(
            schema_version=int(payload["schema_version"]),
            k=int(payload["k"]),
            content_hash=str(payload["content_hash"]),
            produced_at_utc=str(payload["produced_at_utc"]),
            code_token=str(payload["code_token"]),
            rev=str(payload["rev"]),
            audit_chain_token=str(payload["audit_chain_token"]),
            audit_offset=int(payload["audit_offset"]),
            audit_last_hash=str(payload["audit_last_hash"]),
            stores={str(k): str(v) for k, v in stores_raw.items()},
            total_rows=int(aggregates["total_rows"]),
            total_rows_exact=bool(flags["total_rows_exact"]),
            store_count=int(aggregates["store_count"]),
            skipped=int(aggregates["skipped"]),
            run_id_conflicts=int(aggregates["run_id_conflicts"]),
            instruments=tuple(str(x) for x in aggregates["instruments"]),
            timeframes=tuple(str(x) for x in aggregates["timeframes"]),
            directions=tuple(str(x) for x in aggregates["directions"]),
            snapshot_hashes=tuple(str(x) for x in aggregates["snapshot_hashes"]),
            eras=eras,
            conflict_suspect=bool(flags["conflict_suspect"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ── Load + serve-time classification (the V1-V5 ladder) ──────────────────────


@dataclass(frozen=True, slots=True)
class _LoadedRollup:
    """A validated on-disk rollup (rows + stamp) or the named invalid reason."""

    rows: tuple[ResultRow, ...] | None
    stamp: RollupStamp | None
    invalid_reason: str | None


def _load_rollup(sweeps_root: Path) -> _LoadedRollup:
    """Load + validate the on-disk rollup (stamp shape, V1 code token, V4 hash).

    ANY damage — absent/torn stamp, wrong stage/schema, a moved code token, a
    content-hash mismatch (the stamp-LAST crash law: bytes newer than the
    stamp read as absent), or an undecodable Parquet — returns the NAMED
    invalid reason; never a raise, never a partial trust.
    """
    stamp_file = rollup_stamp_path(sweeps_root)
    try:
        stamp_raw = stamp_file.read_bytes()
    except FileNotFoundError:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="absent")
    except OSError:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="stamp_unreadable")
    stamp = _parse_stamp(stamp_raw)
    if stamp is None:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="stamp_invalid")
    if stamp.schema_version != ROLLUP_SCHEMA_VERSION:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="schema_version")
    if stamp.code_token != code_token():
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="code_token")
    parquet = rollup_parquet_path(sweeps_root)
    try:
        parquet_raw = parquet.read_bytes()
    except OSError:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="artifact_unreadable")
    if hashlib.sha256(parquet_raw).hexdigest() != stamp.content_hash:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="content_hash")
    try:
        rows = ResultsStore.load(parquet).rows()
    except ResultsStoreError:
        return _LoadedRollup(rows=None, stamp=None, invalid_reason="artifact_undecodable")
    return _LoadedRollup(rows=rows, stamp=stamp, invalid_reason=None)


@dataclass(frozen=True, slots=True)
class RollupVerdict:
    """The three-outcome serve classification (MAJOR-1 staleness doctrine).

    ``fresh`` — serve verbatim; ``stale`` — the last-valid rollup is still
    SOUND to serve (the caller decides: ``conflict_suspect`` stale-serves
    with the honest note — never a per-poll legacy scan on a latched fence —
    while ``behind``/``revision_drift`` reconcile through the legacy
    fallback + Hook C, the folded land-pass ruling in
    ``sweep_results_view.load_sweep_scan_via_rollup``); ``rebuild`` — fall
    back to the byte-identical legacy scan + Hook C (``reason`` names why).
    ``rows``/``stamp`` are ``None`` only on ``rebuild``.
    """

    outcome: Literal["fresh", "stale", "rebuild"]
    reason: str
    rows: tuple[ResultRow, ...] | None
    stamp: RollupStamp | None
    behind_stores: int


def classify_rollup(sweeps_root: Path, *, current_rev: str) -> RollupVerdict:
    """Run the serve-time validation ladder (V1-V5) for *sweeps_root*'s rollup.

    * V1 code-token equality + stamp/artifact validity (:func:`_load_rollup`)
      — HARD: any failure ⇒ ``rebuild`` naming the reason.
    * V3 store-SET classification over the current ``*/*/*.parquet`` glob
      (an O(stores) stat-level walk — stated, not free): a REMOVAL (stamped
      store gone) ⇒ ``rebuild`` ("store_removed" — the ONE event class that
      can promote a previously-trimmed row).
    * V5 ``conflict_suspect`` ⇒ ``stale`` with the conflict note (never a
      per-poll legacy blackout).
    * V2 rev equality + V3 set-equality ⇒ ``fresh`` (the incumbent
      revision-cache trust envelope — stamped tokens NOT re-stat'ed here).
    * Otherwise re-stat every STAMPED store (O(|stamp|)): a moved token ⇒
      ``rebuild`` ("store_rewritten" — a rewrite-plus-additions tree must
      never stale-serve superseded rows); additions-only ⇒ ``stale`` with
      the honest "N behind" count; set-equal-but-rev-moved with intact
      tokens ⇒ ``stale`` at 0 behind (the audit-append / dir-touch shape).

    The ``behind``/``revision_drift`` STALE verdicts are a soundness
    statement (trim monotonicity — every served row is still a valid axis
    prefix member), NOT a serve mandate: the dashboard maps them onto the
    legacy-fallback + Hook-C reconcile (see
    ``sweep_results_view.load_sweep_scan_via_rollup`` — the folded land-pass
    ruling preserving the incumbent next-poll-freshness contract AND the
    out-of-band convergence guarantee).
    """
    loaded = _load_rollup(sweeps_root)
    if loaded.rows is None or loaded.stamp is None:
        return RollupVerdict(
            outcome="rebuild",
            reason=loaded.invalid_reason or "invalid",
            rows=None,
            stamp=None,
            behind_stores=0,
        )
    stamp = loaded.stamp
    current = set(current_store_relpaths(sweeps_root))
    stamped = set(stamp.stores)
    if stamped - current:
        return RollupVerdict(
            outcome="rebuild", reason="store_removed", rows=None, stamp=None, behind_stores=0
        )
    behind = len(current - stamped)
    if stamp.conflict_suspect:
        return RollupVerdict(
            outcome="stale",
            reason="conflict_suspect",
            rows=loaded.rows,
            stamp=stamp,
            behind_stores=behind,
        )
    if behind == 0 and current_rev == stamp.rev:
        return RollupVerdict(
            outcome="fresh", reason="fresh", rows=loaded.rows, stamp=stamp, behind_stores=0
        )
    # The non-fresh, non-removal arms re-stat the STAMPED set (the matrix
    # nuance): a rewrite-plus-additions tree presents as a superset at
    # set-trust level — the per-store tokens close it.
    for rel, token in stamp.stores.items():
        try:
            if _stat_token(sweeps_root / rel) != token:
                return RollupVerdict(
                    outcome="rebuild",
                    reason="store_rewritten",
                    rows=None,
                    stamp=None,
                    behind_stores=0,
                )
        except OSError:
            return RollupVerdict(
                outcome="rebuild",
                reason="store_stat_failed",
                rows=None,
                stamp=None,
                behind_stores=0,
            )
    return RollupVerdict(
        outcome="stale",
        reason="behind" if behind else "revision_drift",
        rows=loaded.rows,
        stamp=stamp,
        behind_stores=behind,
    )


# ── The write path (flock + stamp-last) ──────────────────────────────────────


class ScanLike(Protocol):
    """The legacy-scan shape the rollup writers consume (structural typing).

    The engine deliberately does NOT import the dashboard's ``SweepScan`` —
    not even under ``TYPE_CHECKING`` (the CodeQL bidirectional-cycle fix,
    PR #1436): this Protocol names exactly the fields the Hook-B/C full folds
    read, and ``sweep_results_view.SweepScan`` satisfies it by construction
    (its additive ``total_rows_override``/``superset_only`` fields are
    serve-side only and deliberately absent here).
    """

    @property
    def rows(self) -> tuple[ResultRow, ...]:
        """Every distinct (run_id-deduped) persisted finalist row."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def store_count(self) -> int:
        """How many per-tuple stores the scan read."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def skipped(self) -> int:
        """Unreadable stores skipped by the resilient loader."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def run_id_conflicts(self) -> int:
        """Cross-store same-run_id content conflicts observed."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def instruments(self) -> tuple[str, ...]:
        """Sorted distinct instrument labels across the rows."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def timeframes(self) -> tuple[str, ...]:
        """Sorted distinct timeframe labels across the rows."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def directions(self) -> tuple[str, ...]:
        """Sorted distinct direction labels across the rows."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def snapshot_hashes(self) -> tuple[str, ...]:
        """Sorted distinct recorded data-snapshot eras."""
        raise NotImplementedError  # structural Protocol stub — never executed


@dataclass
class _RollupState:
    """The mutable accumulator state behind the hooks + the full folds."""

    rows: dict[str, ResultRow]
    stores: dict[str, str]
    eras: dict[EraKey, frozenset[str]]
    audit_cursor: AuditCursor
    audit_token: str
    total_rows: int
    total_rows_exact: bool
    skipped: int
    run_id_conflicts: int
    instruments: set[str]
    timeframes: set[str]
    directions: set[str]
    snapshot_hashes: set[str]
    conflict_suspect: bool


def _current_rev(sweeps_root: Path) -> str:
    """The ``sweep_results_revision`` token at write time (``""`` = no signal).

    Function-local dashboard import (the ONE revision law — never re-minted);
    a probe outage stamps ``""`` so V2 never false-matches and the V3
    classification decides.
    """
    from brutex.dashboard.lake_revision import sweep_results_revision

    rev = sweep_results_revision(sweeps_root.parent)
    return rev if rev is not None else ""


def _write_state(sweeps_root: Path, state: _RollupState) -> None:
    """Persist *state* — trim → Parquet (atomic) → stamp LAST, under the flock.

    The cross-process flock rides the COMMON ``partition_write_lock`` sidecar
    (``<parent>/.locks/…``, DEC-LAKE-PARTITION-FLOCK-001) keyed on the rollup
    Parquet path, so a CLI merge and a dashboard Hook-C rebuild serialise;
    each writer stamps the tree state it saw (the loser is overwritten
    atomically — either file is valid for its recorded state). Raw ``OSError``
    propagates (engine sidecar contract) into the caller's fence.
    """
    kept = trim_to_union(state.rows.values(), ROLLUP_K)
    dest = rollup_parquet_path(sweeps_root)
    lake_root = sweeps_root.parent
    with partition_write_lock(lake_root, dest):
        store = ResultsStore(path=dest)
        store.extend(kept)
        flushed = store.flush()
        stamp = RollupStamp(
            schema_version=ROLLUP_SCHEMA_VERSION,
            k=ROLLUP_K,
            content_hash=flushed.content_hash,
            produced_at_utc=datetime.now(tz=UTC).isoformat(),
            code_token=code_token(),
            rev=_current_rev(sweeps_root),
            audit_chain_token=state.audit_token,
            audit_offset=state.audit_cursor.offset,
            audit_last_hash=state.audit_cursor.last_hash,
            stores=dict(state.stores),
            total_rows=state.total_rows,
            total_rows_exact=state.total_rows_exact,
            store_count=len(state.stores),
            skipped=state.skipped,
            run_id_conflicts=state.run_id_conflicts,
            instruments=tuple(sorted(state.instruments)),
            timeframes=tuple(sorted(state.timeframes)),
            directions=tuple(sorted(state.directions)),
            snapshot_hashes=tuple(sorted(state.snapshot_hashes)),
            eras=dict(state.eras),
            conflict_suspect=state.conflict_suspect,
        )
        rendered = (_dumps(stamp.to_payload()) + "\n").encode("utf-8")
        atomic_write_simple(rollup_stamp_path(sweeps_root), lambda tmp: tmp.write_bytes(rendered))
    _LOG.info(
        "results_rollup.written",
        sweeps_root=str(sweeps_root),
        kept_rows=len(kept),
        total_rows=state.total_rows,
        store_count=len(state.stores),
        conflict_suspect=state.conflict_suspect,
    )


def _state_from_scan(sweeps_root: Path, scan: ScanLike) -> _RollupState:
    """Build the EXACT full-fold state from a legacy :class:`SweepScan`.

    The Hook B/C exactness anchor: aggregates come from the legacy loader's
    own counters (first-wins / most-informed row content included), the store
    map from a fresh glob + stat sweep, and the era map + cursor from ONE
    chain pass (:func:`snapshot_eras_with_cursor` — the same reader the scan
    used, so parity holds by construction). Clears ``conflict_suspect`` and
    restores ``total_rows_exact``.
    """
    eras, cursor = snapshot_eras_with_cursor(sweeps_root)
    stores: dict[str, str] = {}
    for rel in current_store_relpaths(sweeps_root):
        token = _stat_token(sweeps_root / rel)
        if token != _ABSENT_TOKEN:
            stores[rel] = token
    return _RollupState(
        rows={row.run_id: row for row in scan.rows},
        stores=stores,
        eras=eras,
        audit_cursor=cursor,
        audit_token=_stat_token(_audit_chain_path(sweeps_root)),
        total_rows=len(scan.rows),
        total_rows_exact=True,
        skipped=scan.skipped,
        run_id_conflicts=scan.run_id_conflicts,
        instruments=set(scan.instruments),
        timeframes=set(scan.timeframes),
        directions=set(scan.directions),
        snapshot_hashes=set(scan.snapshot_hashes),
        conflict_suspect=False,
    )


def rebuild_rollup_from_scan(sweeps_root: Path, scan: ScanLike) -> bool:
    """Write the rollup EXACTLY from an already-built legacy *scan* (Hooks B/C).

    Returns ``True`` iff a rollup was written. A ZERO-store scan writes
    nothing (a virgin lake never grows a ``sweeps_rollup/`` dir; the legacy
    empty payload is already cheap). Raises on I/O failure — the callers
    fence it (`rebuild_rollup_from_scan_best_effort` / the CLI adapter).
    """
    if scan.store_count == 0:
        _LOG.info("results_rollup.rebuild_skipped_empty", sweeps_root=str(sweeps_root))
        return False
    _write_state(sweeps_root, _state_from_scan(sweeps_root, scan))
    return True


def rebuild_rollup(sweeps_root: Path) -> bool:
    """Hook B — ONE full legacy fold + rollup write (the exactness anchor).

    O(stores × rows) ONCE at run-end (logged, never hidden). Returns ``True``
    iff a rollup was written; raises on failure (the CLI adapter fences it).
    """
    from brutex.dashboard.sweep_results_view import load_sweep_scan_tree

    return rebuild_rollup_from_scan(sweeps_root, load_sweep_scan_tree(sweeps_root))


def rebuild_rollup_from_scan_best_effort(sweeps_root: Path, scan: ScanLike) -> None:
    """Hook C — the fenced serve-side rebuild byproduct (never raises).

    Called on the route's fallback arm with the legacy scan it just built;
    a failure logs ONE ``results_rollup.rebuild_failed`` and serves on (the
    legacy payload was already computed — nothing is lost).
    """
    try:
        rebuild_rollup_from_scan(sweeps_root, scan)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as exc:
        _LOG.warning(
            "results_rollup.rebuild_failed",
            sweeps_root=str(sweeps_root),
            error_type=type(exc).__name__,
            error=str(exc),
        )


# ── Hook A: the launch-scoped incremental merge (FENCED) ─────────────────────


class RollupMergeHook:
    """The per-tuple incremental rollup merge, fenced at BLOCKER-1 strength.

    Constructed once per launch by ``cmd_sweep_all`` and threaded to the grid
    drivers (``rollup_hook=hook.merge_tuple``) + the Stage-3 pass
    (``rollup_paths_hook=hook.merge_paths``). The accumulator is seeded ONCE
    on first merge — from the on-disk rollup when it is valid AND every
    stamped store is intact (plus a catch-up merge of any unstamped
    pre-existing stores), else from ONE full legacy fold — then each
    completed tuple's store is union-merged via the ``ResultsStore.append``
    three-branch law, re-trimmed to the K-union, era-folded via the
    :class:`AuditCursor` tail read (O(new audit lines) — never a full
    rescan), and atomically re-written (Parquet then stamp-LAST) under the
    cross-process flock.

    THE FENCE: the entire merge body runs inside ONE broad ``except
    Exception`` (``KeyboardInterrupt``/``SystemExit``/``MemoryError``
    re-raise carve-out); ANY failure — flock timeout, ENOSPC, a Parquet
    render error, a :class:`ResultsStoreError` / ``VixTrioConflictError``
    conflict out of ``append`` — logs ONE ``results_rollup.merge_failed``,
    latches merging OFF for the remainder of the launch, best-effort stamps
    ``conflict_suspect`` (so /results stale-serves with the honest conflict
    note), and NEVER re-raises into the grid. The typed conflict is a
    CLASSIFICATION inside the fence (a distinct ``kind`` on the log), not the
    fence itself.
    """

    def __init__(self, *, sweeps_root: Path, grid: SweepGridConfig) -> None:
        self._sweeps_root = sweeps_root
        self._grid = grid
        self._state: _RollupState | None = None
        self._enabled = True

    @property
    def enabled(self) -> bool:
        """``False`` once the fence latched merging off for this launch."""
        return self._enabled

    def merge_tuple(self, tuple_: SweepTuple) -> None:
        """Merge ONE completed grid tuple's store (the driver seams' callable)."""
        from brutex.engine.sweep_orchestrator import tuple_store_path

        self._merge(
            (tuple_store_path(self._sweeps_root, tuple_, self._grid),),
            ref=f"{tuple_.instrument}/{tuple_.tf.label}/{tuple_.direction.value}/k={tuple_.k}",
        )

    def merge_paths(self, paths: Sequence[Path]) -> None:
        """Merge a batch of store paths (the Stage-3 one-merge-per-pass seam)."""
        self._merge(tuple(paths), ref=f"paths[{len(paths)}]")

    # ── Fence boundary ───────────────────────────────────────────────────

    def _merge(self, paths: Sequence[Path], *, ref: str) -> None:
        """Run ONE fenced merge of *paths* (see the class docstring's fence law).

        *ref* names the merge in the single ``results_rollup.merge_failed``
        log line. NEVER raises into the caller (the BLOCKER-1 fence) except
        for the KeyboardInterrupt/SystemExit/MemoryError carve-out.
        """
        if not self._enabled:
            return
        try:
            state = self._ensure_state()
            for path in paths:
                self._merge_store_file(state, path)
            self._fold_eras(state)
            _write_state(self._sweeps_root, state)
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as exc:
            self._enabled = False
            kind = "conflict" if isinstance(exc, ResultsStoreError) else "error"
            _LOG.error(
                "results_rollup.merge_failed",
                ref=ref,
                kind=kind,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self._mark_conflict_suspect()

    # ── Inside the fence ─────────────────────────────────────────────────

    def _ensure_state(self) -> _RollupState:
        """Seed the launch accumulator (once) — see the class docstring."""
        if self._state is not None:
            return self._state
        state = self._seed_from_disk()
        if state is None:
            from brutex.dashboard.sweep_results_view import load_sweep_scan_tree

            _LOG.info("results_rollup.seed_full_fold", sweeps_root=str(self._sweeps_root))
            state = _state_from_scan(self._sweeps_root, load_sweep_scan_tree(self._sweeps_root))
        self._state = state
        return state

    def _seed_from_disk(self) -> _RollupState | None:
        """Seed from a VALID + intact on-disk rollup; ``None`` ⇒ full fold.

        Intact = every stamped store still present with its recorded token (a
        removal/rewrite means the incremental base is unsound — trimmed rows
        could need promotion). Unstamped pre-existing stores are caught up by
        merging them here (bounded by their count).
        """
        loaded = _load_rollup(self._sweeps_root)
        if loaded.rows is None or loaded.stamp is None:
            return None
        stamp = loaded.stamp
        current = current_store_relpaths(self._sweeps_root)
        current_set = set(current)
        if set(stamp.stores) - current_set:
            return None
        for rel, token in stamp.stores.items():
            if _stat_token(self._sweeps_root / rel) != token:
                return None
        state = _RollupState(
            rows={row.run_id: row for row in loaded.rows},
            stores=dict(stamp.stores),
            eras=dict(stamp.eras),
            audit_cursor=AuditCursor(offset=stamp.audit_offset, last_hash=stamp.audit_last_hash),
            audit_token=stamp.audit_chain_token,
            total_rows=stamp.total_rows,
            total_rows_exact=stamp.total_rows_exact,
            skipped=stamp.skipped,
            run_id_conflicts=stamp.run_id_conflicts,
            instruments=set(stamp.instruments),
            timeframes=set(stamp.timeframes),
            directions=set(stamp.directions),
            snapshot_hashes=set(stamp.snapshot_hashes),
            conflict_suspect=stamp.conflict_suspect,
        )
        for rel in current:
            if rel not in stamp.stores:
                self._merge_store_file(state, self._sweeps_root / rel)
        return state

    def _merge_store_file(self, state: _RollupState, path: Path) -> None:
        """Union-merge ONE store's rows into *state* (the append three-branch law).

        Raises into the fence on an unreadable store or a cross-store
        conflict (``ResultsStoreError`` / ``VixTrioConflictError`` — arrival
        order must never fake the legacy sorted-path first-wins law; the next
        Hook B/C full fold restores exact accounting). A collision with a
        KEPT row conservatively flips ``total_rows_exact`` off — a collision
        with an already-TRIMMED row is invisible (the documented §h
        residual).
        """
        rel = path.relative_to(self._sweeps_root).as_posix()
        token = _stat_token(path)
        if token == _ABSENT_TOKEN:
            raise ResultsStoreError(f"rollup merge: store missing at {path}")
        known_store = rel in state.stores
        if known_store and state.stores[rel] == token:
            # Already folded, byte-unchanged (a resume re-fire / a repeat
            # stage-3 pass over an untouched cell) — the idempotent no-op.
            return
        loaded = ResultsStore.load(path)
        # The append-law scratch: seeded with the CURRENT union so the
        # three-branch conflict/backfill semantics run verbatim
        # (identical → no-op; VIX None→value backfill → per-field union;
        # non-VIX conflict / both-known VIX conflict → typed raise → fence).
        scratch = ResultsStore(path=rollup_parquet_path(self._sweeps_root))
        scratch.extend(state.rows.values())
        for row in loaded.rows():
            if row.run_id not in state.rows:
                state.total_rows += 1
            elif not known_store:
                # A cross-store collision among KEPT rows: the incremental
                # count can no longer prove the global distinct total (the
                # conservative §h signal — a KNOWN store's re-flush
                # re-encountering its own prior rows is not that class).
                state.total_rows_exact = False
            scratch.append(row)
            state.instruments.add(row.instrument)
            state.timeframes.add(row.tf)
            state.directions.add(row.direction)
        state.rows = {row.run_id: row for row in scratch.rows()}
        state.stores[rel] = token

    def _fold_eras(self, state: _RollupState) -> None:
        """Incremental era refresh — the AuditCursor tail read (MINOR-1).

        Chain stat unchanged ⇒ zero era work. Grown-and-linking ⇒ parse ONLY
        the tail. ANY other state (shrunk file / link mismatch / a torn or
        malformed tail / a probe fault) ⇒ the full rescan from zero — the
        original all-or-nothing degrade, preserved. Covered snapshot hashes
        accrete monotonically from the rows' own era keys.
        """
        chain = _audit_chain_path(self._sweeps_root)
        token = _stat_token(chain)
        if token != state.audit_token:
            if token == _ABSENT_TOKEN:
                state.eras = {}
                state.audit_cursor = CURSOR_START
            else:
                try:
                    size = os.stat(chain).st_size
                    if size < state.audit_cursor.offset:
                        raise AuditCursorMismatch(
                            f"audit chain at {chain} shrank below the stamped offset"
                        )
                    collected = {key: set(v) for key, v in state.eras.items()}
                    cursor = state.audit_cursor
                    for entry, after in _iter_entries_with_cursor(chain, cursor):
                        _fold_era_entry(collected, entry)
                        cursor = after
                    state.eras = {key: frozenset(v) for key, v in collected.items()}
                    state.audit_cursor = cursor
                except (AuditError, AuditCursorMismatch, OSError) as exc:
                    _LOG.warning(
                        "results_rollup.era_tail_rescan",
                        audit_path=str(chain),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    state.eras, state.audit_cursor = snapshot_eras_with_cursor(self._sweeps_root)
            state.audit_token = token
        for row in state.rows.values():
            key: EraKey = (
                row.instrument,
                row.tf,
                row.direction,
                row.mode,
                row.mask_bits.bit_count(),
            )
            state.snapshot_hashes |= state.eras.get(key, frozenset())

    def _mark_conflict_suspect(self) -> None:
        """Best-effort stamp ``conflict_suspect`` on the on-disk rollup (fenced).

        Rewrites ONLY the stamp (the Parquet bytes are untouched, so the
        content hash stays valid); an absent/invalid rollup or a write fault
        logs ``results_rollup.conflict_stamp_failed`` and moves on — /results
        then degrades via the ordinary V2/V3 staleness arms instead.
        """
        try:
            loaded = _load_rollup(self._sweeps_root)
            if loaded.stamp is None:
                return
            stamp = replace(loaded.stamp, conflict_suspect=True)
            rendered = (_dumps(stamp.to_payload()) + "\n").encode("utf-8")
            with partition_write_lock(
                self._sweeps_root.parent, rollup_parquet_path(self._sweeps_root)
            ):
                atomic_write_simple(
                    rollup_stamp_path(self._sweeps_root), lambda tmp: tmp.write_bytes(rendered)
                )
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as exc:
            _LOG.warning(
                "results_rollup.conflict_stamp_failed",
                sweeps_root=str(self._sweeps_root),
                error_type=type(exc).__name__,
                error=str(exc),
            )
