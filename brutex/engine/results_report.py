"""Read-back leaderboard over a persisted sweep results tree (DEC-SWEEP-WINNERS-BY-NAME-001).

The orchestrator (:mod:`brutex.engine.sweep_orchestrator`) writes one Parquet
:class:`~brutex.engine.results_store.ResultsStore` per grid tuple under a results
root (``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet``). On their own
those files answer "what survived?" only as opaque BLAKE3 ``run_id`` keys. This
module is the **operator read-back**: it discovers every per-tuple Parquet under a
root, merges them into one in-memory store, and renders a single ranked leaderboard
that shows — for each finalist — the **winning conditions BY NAME**
(``ResultRow.condition_names``, decoded from ``mask_bits`` via the single-source
vocabulary table), alongside net P&L, trade count, win-rate, Sharpe, and the
row's Deflated-Sharpe.

A candidate can carry TWO deflated numbers (ADR-0008 condition 7 /
DEC-TRIAL-STATS-CONSUME-001), so every surface here labels which ``N`` was
deflated against: the per-row ``dsr@tupleN`` (the in-sweep overfit gate's value,
deflated against that ONE tuple's trial count at rank time) and the Stage-4
cascade's DSR (deflated against the full per-(instrument, TF) leaderboard N),
whose source stamp ``dsr_n_source`` renders in the cascade sidecar section. The
cascade's larger-N deflation is strictly stricter — it supersedes, never
double-spends, the per-tuple gate.

Honest envelope (Golden Rule #10)
---------------------------------
Discovery + merge is inherently **O(total finalists)** work and memory — there is no
O(1)-total read of an N-row leaderboard (claiming so would be a hallucination). Each
*per-row* projection (the mask→names decode is already materialised on the row; the
``format_inr`` render) is O(1). The leaderboard is sized for the funnel's final
10²–10⁴ finalists (``TRACK1_BACKTEST_SPEC.md``), so the whole merged set fits in
memory — the same sizing :class:`~brutex.engine.results_store.ResultsStore` assumes.

Every Stage-2 grid row is, by construction, a **gate survivor** (the sweep only
persists combos that cleared the hard rejection gates — and, when the overfit gate
was active, the Deflated-Sharpe gate too); a ``REJECT`` is never persisted. A tree
swept with ``--stage3-enable`` ADDITIONALLY holds Stage-3 refined-eval rows
(DEC-STAGE3-WIRE-001), which are persisted **UNGATED** as Bailey-LdP trials
(ADR-0007 condition 6) and are indistinguishable from grid rows at the row level
— so NO per-row gate verdict is rendered (the prior unconditional ``gate=PASS``
column would be FALSE for refined rows; stated plainly rather than implied,
Golden Rule #10). Pure read + render — no lake, no network; the only files read
are the results Parquet tree and (when present) the Stage-4 cascade sidecar
``cascade/cascade-report.json`` (DEC-PBO-SIDECAR-001), whose per-slice measured
pool PBO is appended as an optional trailing section.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import structlog

from brutex.data.guarded_read import read_regular_text
from brutex.engine.results_store import (
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    VixTrioConflictError,
)
from brutex.money import format_inr

_LOG = structlog.get_logger(__name__)

#: Friendly ``--sort-by`` aliases → the underlying rankable :class:`ResultRow` column.
#: Kept here as the ONE place the CLI sort vocabulary lives (CLAUDE.md §11 COMMON).
SORT_ALIASES: Final[dict[str, str]] = {
    "sharpe": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "calmar": "calmar_ratio",
    "pnl": "net_pnl_paisa",
    "win": "win_rate",
    "trades": "total_trades",
    "pf": "profit_factor",
}

#: Default leaderboard sort key (the headline risk-adjusted metric).
DEFAULT_SORT_BY: Final[str] = "sharpe"

#: The heal protocol's quarantine subtree (DEC-SWEEP-HEAL-001, hostile-review
#: condition C3) — quarantined stores hold the SAME run-ids as their redone
#: replacements with different metrics, so merging them would raise the
#: cross-file append conflict on a perfectly healed tree. MUST equal
#: ``brutex.engine.sweep_verify.QUARANTINE_DIRNAME`` — pinned by test, not
#: imported: this module stays a pure read+render surface free of the
#: orchestrator import graph (the ``_TUPLE_DIGEST_LEN`` precedent).
_QUARANTINE_DIRNAME: Final[str] = "quarantine"

#: Where the sweep's hash-chained audit log lives under a results root — the
#: SAME convention every ``brutex sweep-all`` entry point writes
#: (``cli.py``: ``out_root / "audit" / "sweep-all.jsonl"``). The recorded
#: reproducibility context (``data_snapshot_hash``) is read from its
#: ``SWEEP_START`` payloads for the snapshot-mismatch quarantine below.
_AUDIT_RELPATH: Final[tuple[str, str]] = ("audit", "sweep-all.jsonl")

#: Cap on the quarantined-path list woven into the aggregated WARNING (the
#: count is always exact; the sample is bounded so a huge tree cannot log-storm).
_QUARANTINE_LOG_PATH_CAP: Final[int] = 20

#: The one aggregated snapshot-mismatch quarantine WARNING
#: (DEC-LAKE-READ-UNIQUE-001) — poisoned-era result files excluded from a
#: read-back because their recorded ``data_snapshot_hash`` no longer matches
#: the caller's CURRENT lake snapshot context.
SNAPSHOT_QUARANTINE_EVENT: Final[str] = "results.snapshot_mismatch_quarantined"


def _recorded_snapshot_hash(root: Path) -> str | None:
    """Best-effort read of the tree's recorded ``data_snapshot_hash``, or ``None``.

    Reads the audit chain's ``SWEEP_START`` payloads via the doctor's
    :func:`~brutex.engine.sweep_verify.recorded_context_from_audit` (CLAUDE.md
    §11 COMMON — the ONE recorded-context reader; function-local import so this
    module's read+render surface stays lean and is only paid when a caller
    actually supplies a current snapshot context). ``None`` when the audit log
    is absent, carries no ``SWEEP_START``, or is unreadable/inconsistent — the
    caller then keeps legacy behaviour (conservative: never quarantine on a
    guess, Golden Rule #1).
    """
    from brutex.engine.sweep_verify import SweepVerifyError, recorded_context_from_audit

    try:
        context = recorded_context_from_audit(root.joinpath(*_AUDIT_RELPATH))
    except SweepVerifyError:
        return None
    return None if context is None else context.data_snapshot_hash


def _quarantine_snapshot_mismatch(
    root: Path, files: list[Path], *, current_snapshot_hash: str | None
) -> list[Path]:
    """Filter poisoned-era result files whose recorded snapshot mismatches — the seam.

    DEC-LAKE-READ-UNIQUE-001: after a lake heal/re-pull changes the data, result
    rows swept against the OLD (possibly duplicate-poisoned) lake must not
    co-rank with clean ones in ``brutex report`` / the leaderboard / Stage-3
    survivor selection. With a *current_snapshot_hash* supplied AND the tree's
    recorded ``data_snapshot_hash`` derivable (its audit chain), a mismatch
    quarantines the discovered files with ONE aggregated
    :data:`SNAPSHOT_QUARANTINE_EVENT` WARNING naming the count + a bounded path
    sample.

    Honest envelope (Golden Rule #10): the recorded context is TREE-level (one
    ``data_snapshot_hash`` per audit chain — per-FILE recorded hashes do not
    exist today; the per-tuple filename digest folds the hash but is a one-way
    BLAKE3), so a mismatch quarantines the WHOLE tree's files, and a tree with
    NO derivable recorded context is kept byte-identical to legacy behaviour.
    Per-file-granular quarantine over a mixed-era tree needs the at-rest census
    (dup-train PR-3) and is NOT claimed here. ``current_snapshot_hash=None``
    (every existing call site's default) is byte-identical to legacy.
    """
    if current_snapshot_hash is None or not files:
        return files
    recorded = _recorded_snapshot_hash(root)
    if recorded is None or recorded == current_snapshot_hash:
        return files
    rel_paths = [p.relative_to(root).as_posix() for p in files]
    _LOG.warning(
        SNAPSHOT_QUARANTINE_EVENT,
        root=str(root),
        recorded=recorded,
        current=current_snapshot_hash,
        count=len(files),
        paths=rel_paths[:_QUARANTINE_LOG_PATH_CAP],
    )
    return []


def discover_result_files(root: Path, *, current_snapshot_hash: str | None = None) -> list[Path]:
    """Return every per-tuple ``*.parquet`` under *root*, in deterministic sorted order.

    Recursively globs ``**/*.parquet`` so the orchestrator's
    ``<root>/<instrument>/<tf>/<file>.parquet`` layout is fully covered, then
    EXCLUDES the heal protocol's ``quarantine/`` subtree (condition C3 — see
    :data:`_QUARANTINE_DIRNAME`). The sorted order makes the merge + the
    leaderboard reproducible regardless of filesystem enumeration order
    (Golden Rule #4).

    Parameters
    ----------
    root
        The grid results root directory.
    current_snapshot_hash
        Optional CURRENT lake snapshot context
        (:meth:`~brutex.data.catalog.Catalog.data_snapshot_hash`). When
        supplied and the tree's recorded context mismatches, the poisoned-era
        files are quarantined with one aggregated WARNING
        (:func:`_quarantine_snapshot_mismatch`, DEC-LAKE-READ-UNIQUE-001).
        ``None`` (the default) is byte-identical to legacy behaviour.

    Returns
    -------
    list[pathlib.Path]
        Sorted Parquet paths (empty when none exist under *root*).

    Raises
    ------
    ResultsStoreError
        If *root* does not exist or is not a directory.
    """
    if not root.is_dir():
        raise ResultsStoreError(f"results root not found or not a directory: {root}")
    files = [
        path
        for path in sorted(root.glob("**/*.parquet"))
        if _QUARANTINE_DIRNAME not in path.relative_to(root).parts
    ]
    return _quarantine_snapshot_mismatch(root, files, current_snapshot_hash=current_snapshot_hash)


def load_results_tree(root: Path, *, current_snapshot_hash: str | None = None) -> ResultsStore:
    """Merge every per-tuple Parquet under *root* into one in-memory store.

    Each tuple's finalists carry globally-unique BLAKE3 ``run_id`` keys (the 9-field
    run-identity folds in instrument / tf / direction / mode / params), so merging
    across files never aliases — :meth:`ResultsStore.append` dedups idempotently and
    a genuine cross-file key clash (data corruption) raises rather than silently
    overwriting (Golden Rule #4).

    Parameters
    ----------
    root
        The grid results root directory.
    current_snapshot_hash
        Optional CURRENT lake snapshot context — threaded to
        :func:`discover_result_files`'s snapshot-mismatch quarantine
        (DEC-LAKE-READ-UNIQUE-001). ``None`` (the default) is byte-identical
        to legacy behaviour.

    Returns
    -------
    ResultsStore
        A store holding every discovered finalist (empty when no files exist). Its
        ``path`` is set to *root* for reference only — this store is read-only here
        and is never flushed.

    Raises
    ------
    ResultsStoreError
        If *root* does not exist, ``polars`` is missing, or a cross-file ``run_id``
        conflict is detected.
    """
    return _merge_files(
        root, discover_result_files(root, current_snapshot_hash=current_snapshot_hash)
    )


def _merge_files(root: Path, files: Sequence[Path]) -> ResultsStore:
    """Merge already-discovered *files* into one read-only store rooted at *root*."""
    merged, _ = _merge_files_with_sources(root, files)
    return merged


def _merge_files_with_sources(
    root: Path, files: Sequence[Path]
) -> tuple[ResultsStore, dict[str, str]]:
    """Merge *files* AND keep each row's source file (root-relative POSIX path).

    The provenance map (DEC-ROW-PROVENANCE-001) is what lets the read-back
    label refined rows: the row itself carries no provenance, but its SOURCE
    FILE does — refined cells are distinct files the cascade manifest names.

    A cross-file duplicate whose ONLY difference is two different KNOWN
    values on the REFERENCE-ONLY VIX trio (DEC-VIX-STAMP-WIRE-001 — the
    India-VIX lake moved between the two stores' writes, e.g. an
    overlapping-grid relaunch after a VIX re-pull; ``tuple_store_path``
    digests fold ``condition_bits``, so overlapping grids legitimately write
    DISTINCT files carrying the SAME run_ids) is NOT a run-identity breach:
    this READ surface keeps the deterministic FIRST row (*files* arrive
    sorted from discovery) and logs one
    ``results_report.vix_cross_store_divergence`` WARNING per dropped row —
    the leaderboard must render regardless (the dashboard
    ``_load_rows_resilient`` precedent; the engine's WRITE surfaces keep the
    typed tuple/cell-skip). The directional ``None`` → value backfill inside
    :meth:`ResultsStore.append` still applies silently, and a NON-VIX
    difference still raises the plain run-identity :class:`ResultsStoreError`
    (Golden Rule #4 — never masked).
    """
    merged = ResultsStore(path=root)
    sources: dict[str, str] = {}
    for file in files:
        rel = file.relative_to(root).as_posix()
        for row in ResultsStore.load(file).rows():
            try:
                merged.append(row)
            except VixTrioConflictError:
                _LOG.warning(
                    "results_report.vix_cross_store_divergence",
                    run_id=row.run_id,
                    kept_from=sources.get(row.run_id),
                    dropped_from=rel,
                    policy="first-wins",
                )
                continue
            sources[row.run_id] = rel
    return merged, sources


def _stage3_rel_paths(root: Path) -> frozenset[str] | None:
    """Return the manifest-attributed Stage-3 cell paths, or ``None`` = unknown.

    The three honest states (DEC-ROW-PROVENANCE-001): a readable manifest
    whose every refined cell carries ``rel_path`` → the attribution set
    (possibly empty when Stage-3 was disabled — then every row is a grid
    row); a manifest that is absent, unreadable, or PRE-provenance (any cell
    missing ``rel_path``) → ``None`` — the caller keeps the blanket
    disclaimer rather than guessing (Golden Rule #1).
    """
    from brutex.engine.cascade_wire import CascadeWireError, load_manifest

    try:
        stage3 = load_manifest(root).stage3
    except CascadeWireError:
        return None
    if stage3 is None:
        return None
    if not stage3.enabled:
        return frozenset()
    rel_paths = [cell.rel_path for cell in stage3.refined_cells]
    if any(rel is None for rel in rel_paths):
        return None  # pre-DEC-ROW-PROVENANCE-001 manifest — no attribution
    return frozenset(rel for rel in rel_paths if rel is not None)


def _resolve_sort_field(sort_by: str) -> str:
    """Map a friendly ``--sort-by`` alias to its :class:`ResultRow` column.

    Raises
    ------
    ResultsStoreError
        If *sort_by* is not a known alias.
    """
    field = SORT_ALIASES.get(sort_by)
    if field is None:
        raise ResultsStoreError(
            f"unknown sort key {sort_by!r}; choose one of {sorted(SORT_ALIASES)}"
        )
    return field


def _format_deflated(value: float | None) -> str:
    """Render the Deflated-Sharpe column — full precision, NEVER rounded; ``None`` → ``—``."""
    if value is None:
        return "—"
    return f"{value:+.4f}"


def _format_conditions(condition_names: str) -> str:
    """Render the winning conditions — the stored NAMES, or a clear no-condition note."""
    if not condition_names:
        return "(no conditions — enters every bar)"
    return condition_names


def render_leaderboard(rows: Sequence[ResultRow], *, origins: dict[str, str] | None = None) -> str:
    """Render *rows* (already in leaderboard order) as the readable winners table.

    Each finalist renders as two lines: a headline row (rank · instrument · tf ·
    direction · net P&L · #trades · win-rate · Sharpe · Deflated-Sharpe) and an
    indented condition-NAMES line. The row's deflated number is labelled
    ``dsr@tupleN`` — it was deflated against that ONE tuple's rank-time trial
    count, NOT the leaderboard N the Stage-4 cascade uses (ADR-0008 condition 7;
    see the module docstring). With *origins* supplied (DEC-ROW-PROVENANCE-001)
    each headline row additionally carries ``src=grid`` (a gate survivor) or
    ``src=stage3`` (an UNGATED refined trial) — the per-row honesty the old
    blanket disclaimer could not give; with ``origins=None`` no marker renders
    and a row cannot reveal which it is (the DEC-STAGE3-WIRE-001 fallback).
    Money goes through :func:`brutex.money.format_inr` (2dp,
    DEC-MONEY-2DP-001); statistical metrics are only *displayed* to a fixed
    width — the stored values are never rounded.

    Parameters
    ----------
    rows
        The finalists to render, in the desired order (see :func:`results_show_report`).
    origins
        Optional ``run_id → "grid" | "stage3"`` attribution (built by
        :func:`results_show_report` from the cascade manifest's cell paths).

    Returns
    -------
    str
        The rendered table (a single placeholder line when *rows* is empty).
    """
    if not rows:
        return "(no persisted results)"
    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        # Per-row provenance marker (DEC-ROW-PROVENANCE-001): rendered ONLY
        # when the cascade manifest attributes the tree's files — `grid` rows
        # are gate survivors, `stage3` rows are UNGATED refined trials. With
        # no attribution the output is byte-identical to the unmarked render.
        marker = "" if origins is None else f"  src={origins.get(row.run_id, '?')}"
        lines.append(
            f"#{rank}  {row.instrument} {row.tf} {row.direction}  "
            f"pnl={format_inr(row.net_pnl_paisa)}  "
            f"trades={row.total_trades}  "
            f"win={row.win_rate * 100:.1f}%  "
            f"sharpe={row.sharpe_ratio:+.3f}  "
            f"dsr@tupleN={_format_deflated(row.deflated_sharpe)}{marker}\n"
            f"    {_format_conditions(row.condition_names)}"
        )
    return "\n".join(lines)


def _provisional_note(report: dict[str, object]) -> str:
    """The counted provisional-pass label for one group, or ``""``.

    Reads the group's verdict list for the additive per-verdict
    ``provisional`` key (R2-SR-F4). Returns ``""`` when the verdicts are not
    a list, no verdict carries the key (a pre-change report — the flag was
    never recorded), or no PASS is flagged (a leaderboard-sourced group).
    Lenient by design: an additive key on a best-effort render surface never
    hard-fails a pre-change sidecar.
    """
    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list):
        return ""
    stamped = [v for v in verdicts if isinstance(v, dict) and "provisional" in v]
    if not stamped:
        return ""
    n_provisional = sum(
        1 for v in stamped if v.get("provisional") is True and v.get("passed") is True
    )
    if n_provisional == 0:
        return ""
    return f"  provisional={n_provisional} (pool-N DSR pass — not proven at leaderboard N)"


def _render_sidecar_group(entry: object) -> str:
    """Render one cascade-sidecar group line, or raise ``ValueError`` on a bad shape.

    The three honest states (DEC-PBO-SIDECAR-001, hostile C7a — never flattened
    into one dash): ``pool_pbo`` key ABSENT → a pre-sidecar report (the value was
    never recorded); ``null`` → the filter measurably SKIPPED (pool < 2
    candidates, series shorter than one split block, or the pool was already
    empty at filter #5); a float → the measured COMBINED-DIRECTION pool PBO,
    displayed fixed-width — the stored JSON value is the raw float, never
    rounded (the ``_format_deflated`` precedent).

    Each line also labels which trial population the cascade's DSR filter #2
    deflated against (ADR-0008 condition 7 / DEC-TRIAL-STATS-CONSUME-001), the
    same additive three-state convention: ``dsr_n_source`` key ABSENT → a
    pre-ADR-0008 report (the stamp was never recorded); ``"leaderboard"`` →
    the frozen per-(instrument, TF) trial stats (the spec-mandated N);
    ``"pool"`` → the legacy entering-pool fallback (an anti-conservative
    under-count, shown so it can never masquerade as the corrected figure).
    The N is echoed verbatim — distinct from the leaderboard rows' per-tuple
    ``dsr@tupleN`` label above it.

    PROVISIONAL passes (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F4): when the
    group's verdicts carry the additive per-verdict ``provisional`` key and
    any pass is flagged (a pool-N-deflated survivor), the line appends a
    counted ``provisional=N`` label so a pool-sourced pass can never silently
    read as proven. A pre-change report LACKING the key renders unchanged
    (the same absent-key convention as ``pool_pbo``), as does a
    leaderboard-sourced group (zero flagged passes).
    """
    if not isinstance(entry, dict):
        raise ValueError("group entry is not an object")
    report = entry.get("report")
    if not isinstance(report, dict):
        raise ValueError("group report is not an object")
    label = f"{entry.get('instrument', '?')} {entry.get('tf', '?')}"
    counts = f"in={report.get('n_input', '?')} survived={report.get('n_survived', '?')}"
    if "dsr_n_source" not in report:
        dsr = "dsr N not stamped (pre-ADR-0008 report)"
    else:
        dsr = f"dsr N={report.get('dsr_n_trials', '?')} source={report['dsr_n_source']}"
    dsr += _provisional_note(report)
    if "pool_pbo" not in report:
        return f"  {label}  pool PBO not recorded (pre-sidecar report)  {counts}  {dsr}"
    pbo = report["pool_pbo"]
    if pbo is None:
        return (
            f"  {label}  pool PBO skipped (pool < 2 candidates / short series / "
            f"pool empty at #5)  {counts}  {dsr}"
        )
    return f"  {label}  pool PBO={float(pbo):.4f}  {counts}  {dsr}"


def render_cascade_sidecar(root: Path) -> str:
    """Render the optional Stage-4 cascade sidecar section — ``""`` when absent.

    Reads ``<root>/cascade/cascade-report.json`` (the path + version constants
    are imported from :mod:`brutex.engine.cascade_wire`, the single source —
    function-local so ``import brutex.engine`` stays lean, the ``cli`` pattern).
    A present-but-unreadable sidecar (corrupt JSON, wrong shape, unsupported
    version) renders a LOUD ``UNREADABLE`` section naming the path and reason —
    never a silent skip (Golden Rule #10); the leaderboard above it still
    renders (the sidecar is supplementary, not load-bearing).
    """
    from brutex.engine.cascade_wire import REPORT_VERSION, report_path

    path = report_path(root)
    if not path.is_file():
        return ""
    header = f"Stage-4 cascade sidecar — {path}"
    try:
        payload: object = json.loads(read_regular_text(path))
    except (OSError, ValueError) as exc:
        return f"{header}\n  UNREADABLE: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return f"{header}\n  UNREADABLE: not a cascade report object"
    version = payload.get("version")
    if version != REPORT_VERSION:
        return f"{header}\n  UNREADABLE: unsupported report version {version!r}"
    try:
        groups = [_render_sidecar_group(entry) for entry in payload["groups"]]
    except ValueError as exc:
        return f"{header}\n  UNREADABLE: {exc}"
    if not groups:
        return f"{header}\n  (no pooled slices)"
    return "\n".join([header, *groups])


def results_show_report(root: Path, *, top: int, sort_by: str = DEFAULT_SORT_BY) -> str:
    """Load the results tree under *root* and render the top-*top* winners leaderboard.

    Parameters
    ----------
    root
        The grid results root directory (the orchestrator's ``--results-root``).
    top
        How many leaders to show (``>= 1``).
    sort_by
        A :data:`SORT_ALIASES` key (default :data:`DEFAULT_SORT_BY`).

    Returns
    -------
    str
        A self-describing header (root · file count · finalist count · sort key)
        followed by the ranked winners table, then — ONLY when
        ``<root>/cascade/cascade-report.json`` exists — the Stage-4 cascade
        sidecar section (per-slice measured pool PBO, DEC-PBO-SIDECAR-001).
        With no sidecar file the output is byte-identical to the pre-sidecar
        render.

    Raises
    ------
    ResultsStoreError
        If *root* does not exist, ``top < 1``, *sort_by* is unknown, ``polars`` is
        missing, or a cross-file ``run_id`` conflict is detected.
    """
    if top < 1:
        raise ResultsStoreError(f"top must be >= 1, got {top}")
    field = _resolve_sort_field(sort_by)
    files = discover_result_files(root)
    store, sources = _merge_files_with_sources(root, files)
    leaders = store.top_k(min(top, len(store)), by=field) if len(store) else ()
    # Per-row provenance (DEC-ROW-PROVENANCE-001): attribution exists ONLY
    # when the cascade manifest names every refined cell's path — then each
    # row labels src=grid|stage3; otherwise the blanket disclaimer stands.
    stage3_files = _stage3_rel_paths(root)
    origins: dict[str, str] | None = None
    if stage3_files is not None:
        origins = {
            run_id: ("stage3" if rel in stage3_files else "grid") for run_id, rel in sources.items()
        }
    if origins is None:
        provenance_note = (
            "  (Stage-2 grid rows are gate survivors; a --stage3-enable sweep also "
            "persists UNGATED Stage-3 refined-eval trials — no per-row gate verdict)"
        )
    else:
        provenance_note = (
            "  (per-row src= markers from the cascade manifest: src=grid rows are "
            "gate survivors; src=stage3 rows are UNGATED refined trials — "
            "DEC-ROW-PROVENANCE-001)"
        )
    header = (
        f"BruteX results — {root}\n"
        f"  files={len(files)}  finalists={len(store)}  "
        f"sort={sort_by}  showing top {len(leaders)}\n"
        f"{provenance_note}"
    )
    sidecar = render_cascade_sidecar(root)
    body = f"{header}\n\n{render_leaderboard(leaders, origins=origins)}"
    return f"{body}\n\n{sidecar}" if sidecar else body


__all__ = [
    "DEFAULT_SORT_BY",
    "SNAPSHOT_QUARANTINE_EVENT",
    "SORT_ALIASES",
    "discover_result_files",
    "load_results_tree",
    "render_cascade_sidecar",
    "render_leaderboard",
    "results_show_report",
]
