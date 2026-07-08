"""Sweep results-tree verifier — the `sweep-verify` doctor's pure core (DEC-SWEEP-VERIFY-001).

The tracked follow-up from MIGRATION.md #654: the completion-sentinel fix
prevents NEW partial-``mark_many`` corruption (#650) but cannot detect or heal
the PRE-FIX kind — a tuple store inflated with a subset re-rank's extra rows
that then "completed". This module is the detection core, hostile-reviewed
into three tiers; this slice ships the two bars-free tiers:

* **Tier 1 — STRUCTURAL** (:func:`verify_structural`): needs ONLY the tree.
  Every artifact is self-describing — the store filename carries the 16-hex
  tuple digest, the trial-stats sidecar and the completion sentinel each carry
  the (digest, store-SHA-256) pairing identity — so loadability, pairing,
  orphanhood, and the post-#654 tally invariant are all checkable offline.
* **Tier 2 — IDENTITY** (:func:`verify_identity`): needs the grid config
  re-built with the tree's RECORDED context strings (read them via
  :func:`recorded_context_from_audit`, never from memory — review condition
  2a). Re-derives every expected store path and every row's 9-field
  ``run_id``, checks mask membership/labels, and applies the killer cheap
  check: ``rows > top_k`` under one digest is the #650 inflation signature —
  post-#654 the digest path always rewrites the FULL population, so an
  oversized store cannot occur legitimately. This detects the pre-#646
  inflation #654's envelope called "undetectable by the sweep" — without bars.

The Tier-3 full recount (a shadow re-sweep into a scratch root, byte-diffed —
valid as an oracle ONLY under an exact recorded-context match) and the CLI
shim are the next slice; healing is REJECTED for the doctor (report-only):
deleting artifacts without also wiping the checkpoint + dedup-cache is a
verified no-op trap, and a recount under wrong inputs condemning a good store
is the catastrophic failure mode (the hostile review's P0 — see MIGRATION.md
DEC-SWEEP-VERIFY-001).

Honest envelope (Golden Rule #10)
---------------------------------
* Pre-#646 trees (no sidecars/sentinels) carry no recorded expectations:
  Tier 1 reports the absence (a ``warn``, never a verdict); Tier 2 still
  applies fully (filenames + grid suffice), so the ``top_k`` inflation class
  IS detectable there — but metric-level corruption in such trees stays
  undetectable until the Tier-3 recount, and only under matched context.
* A row's ``run_id`` commits to the mask + context + params, NOT to its
  metric values — internally-consistent metric corruption passes Tiers 1-2
  by design (stated, not hidden; the recount tier is the metric oracle).
* Cost: O(artifacts) JSON reads + one SHA-256 per store (Tier 1) and
  O(total rows) BLAKE3 re-derivations (Tier 2). Offline batch work, NOT a
  Rule-#5 hot path; no O(1)-total claim.
* Stage-3 refined-cell stores have NO sentinel (only ``run_persisted_sweep``
  writes one) and legitimately accrete rows across survivors — sentinel
  absence is an ``info`` on every store, and the ``top_k`` bound applies only
  to grid tuples (review conditions; never CORRUPT-by-misclassification).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.engine.results_store import VIX_FIELDS, ResultsStore, ResultsStoreError
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_run import canonical_trade_params, mask_run_id
from brutex.engine.trial_stats import (
    TrialStatsError,
    read_tuple_trial_stats,
    trial_stats_sidecar_path,
)
from brutex.engine.tuple_complete import (
    TupleCompletionError,
    completion_sentinel_path,
    read_completion_sentinel,
)
from brutex.models import BruteXError
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams, has_conflict

if TYPE_CHECKING:
    # Annotation-only: cascade_wire pulls the numpy statistics stack — kept out
    # of this module's runtime import graph (the sweep_run TYPE_CHECKING
    # discipline) so the engine coverage gates stay stable.
    from brutex.engine.cascade_wire import Stage3ManifestRecord
    from brutex.engine.trial_stats import TupleTrialStats
    from brutex.engine.tuple_complete import TupleCompletionSentinel
    from brutex.orchestrator.audit import AuditEntry

_LOG = structlog.get_logger("brutex.engine.sweep_verify")

#: Finding severities — ``corrupt`` is verdict-grade; ``warn`` needs operator
#: attention but is NOT evidence of corruption; ``info`` is honest narration.
SEVERITIES: Final[tuple[str, ...]] = ("corrupt", "warn", "info")

#: The three recorded reproducibility strings every per-tuple ``SWEEP_START``
#: audit payload carries (the doctor's Tier-2 context source).
_CONTEXT_KEYS: Final[tuple[str, ...]] = (
    "data_snapshot_hash",
    "indicator_version",
    "code_commit_sha",
)


#: The heal protocol's quarantine subtree under a results root
#: (DEC-SWEEP-HEAL-001) — EXCLUDED from every results-tree glob here and in
#: ``results_report`` (hostile-review condition C3): quarantined artifacts keep
#: intact pairing and the same run-ids as their redone replacements, so
#: re-scanning them would re-condemn a healed tree / crash the merged read-back.
QUARANTINE_DIRNAME: Final[str] = "quarantine"


def _outside_quarantine(root: Path, paths: list[Path]) -> list[Path]:
    """Filter *paths* (under *root*) to those NOT inside the quarantine subtree."""
    return [p for p in paths if QUARANTINE_DIRNAME not in p.relative_to(root).parts]


class SweepVerifyError(BruteXError):
    """Raised for verifier USAGE failures (bad inputs) — never for tree findings.

    Tree problems are :class:`VerifyFinding`\\s in the report; this error means
    the doctor itself was invoked unusably (an unreadable audit log, an
    inconsistent recorded context, a malformed grid input).
    """


@dataclass(frozen=True, slots=True)
class VerifyFinding:
    """One verifier finding against one artifact (or the whole tree).

    Parameters
    ----------
    path
        The artifact path RELATIVE to the verified root (``"."`` for
        tree-level findings such as a grid mismatch).
    code
        The machine-readable finding code (stable; tests pin these).
    severity
        One of :data:`SEVERITIES`.
    detail
        The human explanation (paths/hashes quoted as needed).

    Raises
    ------
    SweepVerifyError
        On an empty field or an unknown severity.
    """

    path: str
    code: str
    severity: str
    detail: str

    def __post_init__(self) -> None:
        for name in ("path", "code", "detail"):
            if not getattr(self, name):
                raise SweepVerifyError(f"VerifyFinding.{name} must be non-empty")
        if self.severity not in SEVERITIES:
            raise SweepVerifyError(
                f"VerifyFinding.severity must be one of {SEVERITIES}, got {self.severity!r}"
            )


@dataclass(frozen=True, slots=True)
class TreeVerifyReport:
    """One tier's verdict over a results tree.

    Parameters
    ----------
    root
        The verified root (string, for rendering).
    tier
        ``"structural"`` or ``"identity"`` (the recount tier is slice 2).
    checked_stores
        How many ``*.parquet`` stores the tier examined.
    findings
        Every finding, in deterministic (path, code) order.
    """

    root: str
    tier: str
    checked_stores: int
    findings: tuple[VerifyFinding, ...]

    @property
    def corrupt(self) -> tuple[VerifyFinding, ...]:
        """The verdict-grade findings only."""
        return tuple(f for f in self.findings if f.severity == "corrupt")

    @property
    def clean(self) -> bool:
        """``True`` iff no ``corrupt`` finding exists (warns/infos allowed)."""
        return not self.corrupt

    def render(self) -> str:
        """Render the deterministic human report (one line per finding)."""
        verdict = "CLEAN" if self.clean else f"CORRUPT ({len(self.corrupt)} finding(s))"
        lines = [
            f"sweep-verify [{self.tier}] — {self.root}",
            f"  stores checked: {self.checked_stores} · verdict: {verdict}",
        ]
        lines.extend(
            f"  [{f.severity.upper()}] {f.path} {f.code}: {f.detail}" for f in self.findings
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RecordedContext:
    """The tree's recorded reproducibility strings (review condition 2a).

    Read from the audit chain's ``SWEEP_START`` payloads — the tree describes
    itself; the operator's memory is never the source.
    """

    data_snapshot_hash: str
    indicator_version: str
    code_commit_sha: str


def recorded_context_from_audit(audit_path: Path) -> RecordedContext | None:
    """Read the tree's recorded context from its audit log, or ``None`` if absent.

    Scans every ``SWEEP_START`` entry's payload for the three reproducibility
    strings. Returns ``None`` when the audit file does not exist or carries no
    ``SWEEP_START`` line (the caller falls back to explicit operator input).

    Raises
    ------
    SweepVerifyError
        If the audit log is unreadable/corrupt, a ``SWEEP_START`` payload is
        missing a context key, or two entries disagree (an inconsistent tree
        is not a verifiable tree — refuse rather than guess, Golden Rule #1).
    """
    if not audit_path.exists():
        return None
    # Function-local import: the audit module pulls the orchestrator/bloom
    # graph nowhere, but keeping the verifier's module surface minimal mirrors
    # the trial_stats discipline.
    from brutex.orchestrator.audit import AuditError, read_entries

    try:
        entries: list[AuditEntry] = list(read_entries(audit_path))
    except AuditError as exc:
        raise SweepVerifyError(f"unreadable audit log at {audit_path}: {exc}") from exc
    context: RecordedContext | None = None
    for entry in entries:
        if entry.action != "SWEEP_START":
            continue
        payload = entry.payload
        missing = [key for key in _CONTEXT_KEYS if key not in payload]
        if missing:
            raise SweepVerifyError(
                f"SWEEP_START payload at {audit_path} (seq {entry.seq}) missing {missing}"
            )
        found = RecordedContext(
            data_snapshot_hash=str(payload["data_snapshot_hash"]),
            indicator_version=str(payload["indicator_version"]),
            code_commit_sha=str(payload["code_commit_sha"]),
        )
        if context is None:
            context = found
        elif context != found:
            raise SweepVerifyError(
                f"inconsistent recorded context in {audit_path}: {context} vs {found}"
            )
    return context


# ── Tier 1 — STRUCTURAL ──────────────────────────────────────────────────────

#: ``<direction>_k<K>_<16-hex>.parquet`` — the orchestrator's store filename.
_STORE_NAME_PARTS: Final[int] = 3

#: The tuple path digest length (the orchestrator's ``_PATH_DIGEST_LEN``;
#: pinned by test against the trial-stats sidecar's constant, not imported).
_TUPLE_DIGEST_LEN: Final[int] = 16


def _filename_digest(store_path: Path) -> str | None:
    """Extract the 16-hex tuple digest from a store filename, or ``None``."""
    parts = store_path.stem.split("_")
    if len(parts) < _STORE_NAME_PARTS:
        return None
    digest = parts[-1]
    if len(digest) != _TUPLE_DIGEST_LEN or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def _rel(root: Path, path: Path) -> str:
    """Render *path* relative to *root* (the report's path convention)."""
    return str(path.relative_to(root))


def verify_structural(root: Path) -> TreeVerifyReport:
    """Tier 1: verify every artifact's self-described pairing — tree only, no inputs.

    Per store: Parquet loadability (every row re-validated at construction);
    sidecar readability + (digest, store-hash) pairing — the SAME check the
    cascade-build performs loudly, runnable without a cascade; sentinel
    readability + pairing + the recorded-sidecar cross-check; and the
    post-#654 tally invariant (a sentinel'd tuple bypassed dedup, so
    ``n_enumerated - n_conflict_pruned - n_minhits_dropped - n_backtested``
    must be exactly 0). Plus orphan sidecars/sentinels whose store is gone.

    Raises
    ------
    SweepVerifyError
        If *root* does not exist or is not a directory.
    """
    if not root.is_dir():
        raise SweepVerifyError(f"results root not found or not a directory: {root}")
    findings: list[VerifyFinding] = []

    def add(path: Path, code: str, severity: str, detail: str) -> None:
        findings.append(
            VerifyFinding(path=_rel(root, path), code=code, severity=severity, detail=detail)
        )

    stores = _outside_quarantine(root, sorted(root.glob("**/*.parquet")))
    for store_path in stores:
        try:
            ResultsStore.load(store_path)
        except ResultsStoreError as exc:
            add(store_path, "store_unreadable", "corrupt", str(exc))
            continue
        digest = _filename_digest(store_path)
        if digest is None:
            add(
                store_path,
                "unrecognized_store_name",
                "warn",
                "filename carries no 16-hex tuple digest — pairing unverifiable",
            )
            continue
        actual_hash = hashlib.sha256(store_path.read_bytes()).hexdigest()

        sidecar_path = trial_stats_sidecar_path(store_path)
        sidecar_ok = False
        if not sidecar_path.exists():
            add(
                store_path,
                "legacy_no_sidecar",
                "warn",
                "no trial-stats sidecar — pre-DEC-TRIAL-STATS-WRITE-001 tree, "
                "no recorded expectations at this tier",
            )
        else:
            try:
                stats = read_tuple_trial_stats(sidecar_path)
            except TrialStatsError as exc:
                add(sidecar_path, "sidecar_unreadable", "corrupt", str(exc))
            else:
                sidecar_ok = True
                if stats.tuple_digest != digest:
                    add(
                        sidecar_path,
                        "sidecar_digest_mismatch",
                        "corrupt",
                        f"sidecar pairs tuple {stats.tuple_digest}, filename says {digest}",
                    )
                if stats.store_content_hash != actual_hash:
                    add(
                        sidecar_path,
                        "sidecar_store_hash_mismatch",
                        "corrupt",
                        "sidecar pairs DIFFERENT store bytes — the #650 inflation "
                        "signature on a post-#646 tree (or store tampering)",
                    )

        sentinel_path = completion_sentinel_path(store_path)
        if not sentinel_path.exists():
            add(
                store_path,
                "no_completion_proof",
                "info",
                "no completion sentinel (pre-#654 tuple or Stage-3 cell store)",
            )
            continue
        try:
            sentinel = read_completion_sentinel(sentinel_path)
        except TupleCompletionError as exc:
            add(sentinel_path, "sentinel_unreadable", "corrupt", str(exc))
            continue
        if sentinel.tuple_digest != digest:
            add(
                sentinel_path,
                "sentinel_digest_mismatch",
                "corrupt",
                f"sentinel pairs tuple {sentinel.tuple_digest}, filename says {digest}",
            )
        if sentinel.store_content_hash != actual_hash:
            add(
                sentinel_path,
                "sentinel_store_hash_mismatch",
                "corrupt",
                "sentinel pairs DIFFERENT store bytes",
            )
        if sentinel.sidecar_written and not sidecar_ok:
            add(
                sentinel_path,
                "sentinel_sidecar_missing",
                "corrupt",
                "sentinel records a written sidecar but none reads validly",
            )
        if sentinel.sidecar_written and sidecar_ok:
            stats = read_tuple_trial_stats(sidecar_path)
            remainder = (
                stats.n_enumerated
                - stats.n_conflict_pruned
                - stats.n_minhits_dropped
                - stats.n_backtested
            )
            if remainder != 0:
                add(
                    sidecar_path,
                    "sidecar_tally_remainder",
                    "corrupt",
                    f"sentinel'd tuple bypassed dedup yet {remainder} mask(s) are "
                    f"unaccounted — impossible by construction post-#654",
                )

    store_names = {p.name for p in stores}
    for suffix, code in ((".trials.json", "orphan_sidecar"), (".complete.json", "orphan_sentinel")):
        for orphan in _outside_quarantine(root, sorted(root.glob(f"**/*{suffix}"))):
            if orphan.name.removesuffix(suffix) not in store_names:
                add(orphan, code, "warn", "paired store file is missing")

    report = TreeVerifyReport(
        root=str(root),
        tier="structural",
        checked_stores=len(stores),
        findings=tuple(findings),
    )
    _LOG.info(
        "sweep_verify.structural",
        root=str(root),
        stores=report.checked_stores,
        corrupt=len(report.corrupt),
        findings=len(report.findings),
    )
    return report


# ── Tier 2 — IDENTITY ────────────────────────────────────────────────────────


def _direction_label(direction: Side) -> str:
    """The sweep's direction label (mirrors the runner's mapping)."""
    return "bull" if direction is Side.BUY else "bear"


def plan_expected_stores(
    root: Path, grid: SweepGridConfig, *, stage3: Stage3ManifestRecord | None = None
) -> dict[Path, tuple[SweepTuple, SweepGridConfig, bool]]:
    """Map every expected store path to ``(tuple, owning grid, is_grid_tuple)``.

    Grid tuples enumerate from *grid*; Stage-3 refined cells (review condition
    2c) derive from the cascade manifest's recorded coordinates + train
    snapshot hash — NEVER guessed: with no manifest record, cell stores stay
    unattributed (Tier 1 still covers them).

    Raises
    ------
    SweepVerifyError
        If a Stage-3 cell references a timeframe label the grid lacks, or the
        record is enabled without its train hash (a malformed manifest).
    """
    plan: dict[Path, tuple[SweepTuple, SweepGridConfig, bool]] = {
        tuple_store_path(root, t, grid): (t, grid, True) for t in iter_sweep_tuples(grid)
    }
    if stage3 is None or not stage3.enabled:
        return plan
    train_hash = stage3.train_snapshot_hash
    if train_hash is None:  # pragma: no cover — Stage3ManifestRecord forbids this state
        raise SweepVerifyError("stage3 record enabled without a train_snapshot_hash")
    import dataclasses

    train_grid = dataclasses.replace(grid, data_snapshot_hash=train_hash)
    minutes_by_label = {tf.label: tf.minutes for tf in grid.timeframes}
    for cell in stage3.refined_cells:
        minutes = minutes_by_label.get(cell.tf_label)
        if minutes is None:
            raise SweepVerifyError(
                f"stage3 cell references timeframe {cell.tf_label!r} not in the grid"
            )
        cell_tuple = SweepTuple(
            instrument=cell.instrument,
            tf=Timeframe(cell.tf_label, minutes),
            direction=Side.BUY if cell.direction_label == "bull" else Side.SELL,
            k=cell.k,
            sl_paisa=cell.sl_paisa,
            tp_paisa=cell.tp_paisa,
            proximity=(
                None
                if cell.proximity_bps is None
                else ProximityParams(
                    pivot_proximity_bps=cell.proximity_bps[0],
                    pdh_pdl_proximity_bps=cell.proximity_bps[1],
                    fib_proximity_bps=cell.proximity_bps[2],
                )
            ),
            # The refined cell's Fib anchor (DEC-NIFTY-FIB-AXIS-001) — round-tripped
            # from the manifest so the reconstructed tuple's run_id re-derives at
            # its ORIGINATING anchor (a non-prev-day cell would otherwise mismatch).
            fib_anchor=cell.fib_anchor,
        )
        plan[tuple_store_path(root, cell_tuple, train_grid)] = (cell_tuple, train_grid, False)
    return plan


def _verify_store_rows(
    root: Path,
    store_path: Path,
    tuple_: SweepTuple,
    grid: SweepGridConfig,
    *,
    is_grid_tuple: bool,
    vocabulary_trusted: bool,
    findings: list[VerifyFinding],
) -> None:
    """Re-derive every row's identity in one attributed store (Tier 2 per-store)."""

    def add(code: str, severity: str, detail: str) -> None:
        findings.append(
            VerifyFinding(path=_rel(root, store_path), code=code, severity=severity, detail=detail)
        )

    try:
        rows = ResultsStore.load(store_path).rows()
    except ResultsStoreError as exc:
        add("store_unreadable", "corrupt", str(exc))
        return
    if is_grid_tuple and grid.top_k is not None and len(rows) > grid.top_k:
        add(
            "rows_exceed_top_k",
            "corrupt",
            f"{len(rows)} rows > top_k={grid.top_k} — the #650 subset-re-rank "
            f"inflation signature (impossible legitimately, pre- AND post-#654)",
        )
    cfg = tuple_.to_run_config(grid)
    params = canonical_trade_params(
        sl_paisa=cfg.sl_paisa,
        tp_paisa=cfg.tp_paisa,
        prior_n=cfg.prior_n,
        proximity=cfg.proximity,
        start_equity_paisa=cfg.start_equity_paisa,
        fib_anchor=cfg.fib_anchor,
    )
    expected_label = _direction_label(tuple_.direction)
    bits_mask = 0
    for bit in grid.condition_bits:
        bits_mask |= 1 << bit
    for row in rows:
        if (
            row.instrument != tuple_.instrument
            or row.tf != tuple_.tf.label
            or row.direction != expected_label
            or row.mode != grid.mode
        ):
            add(
                "row_label_mismatch",
                "corrupt",
                f"row {row.run_id[:16]}… labelled "
                f"{row.instrument}/{row.tf}/{row.direction}/{row.mode}, tuple expects "
                f"{tuple_.instrument}/{tuple_.tf.label}/{expected_label}/{grid.mode}",
            )
            continue
        if row.mask_bits.bit_count() != tuple_.k or row.mask_bits & ~bits_mask:
            add(
                "row_mask_not_in_grid",
                "corrupt",
                f"row {row.run_id[:16]}… mask {row.mask_bits:#x} is not a "
                f"k={tuple_.k} subset of the grid's condition bits",
            )
            continue
        if vocabulary_trusted and has_conflict(row.mask_bits):
            add(
                "row_conflict_mask",
                "corrupt",
                f"row {row.run_id[:16]}… mask {row.mask_bits:#x} carries a "
                f"conflict pair the sweep prunes before evaluation",
            )
        derived = mask_run_id(
            mask=row.mask_bits,
            direction=expected_label,
            instrument=tuple_.instrument,
            tf=tuple_.tf.label,
            mode=grid.mode,
            params=params,
            data_snapshot_hash=grid.data_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        if derived != row.run_id:
            add(
                "row_run_id_mismatch",
                "corrupt",
                f"row claims {row.run_id[:16]}… but its mask+context re-derives "
                f"{derived[:16]}… — foreign or tampered row",
            )


def verify_identity(
    root: Path,
    grid: SweepGridConfig,
    *,
    stage3: Stage3ManifestRecord | None = None,
    vocabulary_trusted: bool = False,
) -> TreeVerifyReport:
    """Tier 2: re-derive every expected path + every row identity — grid, no bars.

    *grid* MUST be constructed with the tree's RECORDED context strings
    (:func:`recorded_context_from_audit`; review condition 2a) or every digest
    re-derivation is meaningless. The grid-mismatch guard (condition 2b)
    refuses to judge per-file when the expected and found digest sets are
    fully disjoint — wrong flags must read as GRID-MISMATCH, never as
    tree-wide corruption.

    Parameters
    ----------
    root
        The results root.
    grid
        The sweep's grid axes + the RECORDED context.
    stage3
        The cascade manifest's Stage-3 record (attributes the refined-cell
        stores); ``None`` leaves any cell stores unattributed.
    vocabulary_trusted
        Run the ``has_conflict`` row check — valid ONLY when the current
        vocabulary matches the tree's recorded ``code_commit_sha`` /
        ``indicator_version`` (ADR-0008's drift channel); default off.

    Raises
    ------
    SweepVerifyError
        If *root* is not a directory or the stage3 record is malformed.
    """
    if not root.is_dir():
        raise SweepVerifyError(f"results root not found or not a directory: {root}")
    findings: list[VerifyFinding] = []
    plan = plan_expected_stores(root, grid, stage3=stage3)
    found = _outside_quarantine(root, sorted(root.glob("**/*.parquet")))

    if plan and found and not any(path in plan for path in found):
        findings.append(
            VerifyFinding(
                path=".",
                code="grid_mismatch",
                severity="corrupt",
                detail=(
                    f"none of the {len(found)} store(s) matches any of the "
                    f"{len(plan)} expected path(s) — wrong grid flags, wrong "
                    f"recorded context, or wrong root; per-file verdicts withheld"
                ),
            )
        )
        return TreeVerifyReport(
            root=str(root), tier="identity", checked_stores=0, findings=tuple(findings)
        )

    checked = 0
    for store_path in found:
        attribution = plan.get(store_path)
        if attribution is None:
            findings.append(
                VerifyFinding(
                    path=_rel(root, store_path),
                    code="unattributed_store",
                    severity="warn",
                    detail=(
                        "no grid tuple or manifest-recorded Stage-3 cell derives "
                        "this path (another grid's leftovers, or a cell with no "
                        "manifest) — Tier 1 still applies"
                    ),
                )
            )
            continue
        tuple_, owning_grid, is_grid_tuple = attribution
        checked += 1
        _verify_store_rows(
            root,
            store_path,
            tuple_,
            owning_grid,
            is_grid_tuple=is_grid_tuple,
            vocabulary_trusted=vocabulary_trusted,
            findings=findings,
        )
    for expected in sorted(set(plan) - set(found)):
        findings.append(
            VerifyFinding(
                path=_rel(root, expected),
                code="missing_expected_store",
                severity="info",
                detail="expected store absent — mid-sweep tuple, or no bars for it",
            )
        )

    report = TreeVerifyReport(
        root=str(root), tier="identity", checked_stores=checked, findings=tuple(findings)
    )
    _LOG.info(
        "sweep_verify.identity",
        root=str(root),
        stores=report.checked_stores,
        corrupt=len(report.corrupt),
        findings=len(report.findings),
    )
    return report


# ── Tier 3 — RECOUNT compare (the shadow re-sweep's byte-diff) ───────────────


@dataclass(frozen=True, slots=True)
class RecordedStage3Knobs:
    """The Stage-3 refinement knobs a sweep RECORDED (DEC-STAGE3-RECOUNT-001).

    Read from the tree's own ``STAGE_3_PASS_START`` audit payload — emitted by
    every stage3-enabled sweep since the pass first shipped — never from the
    operator's memory (the condition-2a discipline). Knob-shifted recounts can
    byte-diverge SAME-coordinate cell stores, so a knob match is a binding
    part of cell evidence (hostile-review condition B1).
    """

    stage3_top_k: int
    max_fevals: int
    max_restarts: int
    train_snapshot_hash: str


@dataclass(frozen=True, slots=True)
class RecordedStage3Audit:
    """Everything the audit chain says about the tree's Stage-3 history.

    Parameters
    ----------
    knobs
        The recorded knobs, or ``None`` when no ``STAGE_3_PASS_START`` line
        exists (a grid-only tree, or a pre-stage3/audit-less one).
    consistent
        ``False`` when MULTIPLE pass lines disagree (a mixed-knob history —
        its cell stores hold preloaded foreign-knob rows and are honestly
        non-reproducible; condition B4: cells inconclusive, never compared).
    optimizer_versions
        The sorted unique ``(pycma, numpy)`` pairs from ``STAGE_3_REFINE_START``
        lines — the refined-row identity folds them, so a drift changes
        trajectories without changing the context triple (condition B3).
    """

    knobs: RecordedStage3Knobs | None
    consistent: bool
    optimizer_versions: tuple[tuple[str, str], ...]


def recorded_stage3_from_audit(audit_path: Path) -> RecordedStage3Audit:
    """Read the tree's recorded Stage-3 knob history from its audit log.

    Returns the no-history record when the file or the pass lines are absent.

    Raises
    ------
    SweepVerifyError
        If the audit log is unreadable or a pass payload is missing a knob key
        (a malformed tree is not a verifiable tree).
    """
    if not audit_path.exists():
        return RecordedStage3Audit(knobs=None, consistent=True, optimizer_versions=())
    from brutex.orchestrator.audit import AuditError, read_entries

    try:
        entries: list[AuditEntry] = list(read_entries(audit_path))
    except AuditError as exc:
        raise SweepVerifyError(f"unreadable audit log at {audit_path}: {exc}") from exc
    knobs: RecordedStage3Knobs | None = None
    consistent = True
    versions: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.action == "STAGE_3_REFINE_START":
            payload = entry.payload
            versions.add((str(payload.get("pycma_version")), str(payload.get("numpy_version"))))
            continue
        if entry.action != "STAGE_3_PASS_START":
            continue
        payload = entry.payload
        missing = [
            key
            for key in ("stage3_top_k", "max_fevals", "max_restarts", "train_snapshot_hash")
            if key not in payload
        ]
        if missing:
            raise SweepVerifyError(
                f"STAGE_3_PASS_START payload at {audit_path} (seq {entry.seq}) missing {missing}"
            )
        found = RecordedStage3Knobs(
            stage3_top_k=int(payload["stage3_top_k"]),
            max_fevals=int(payload["max_fevals"]),
            max_restarts=int(payload["max_restarts"]),
            train_snapshot_hash=str(payload["train_snapshot_hash"]),
        )
        if knobs is None:
            knobs = found
        elif knobs != found:
            consistent = False
    return RecordedStage3Audit(
        knobs=knobs, consistent=consistent, optimizer_versions=tuple(sorted(versions))
    )


def _stores_differ_only_in_vix(original: Path, scratch: Path) -> str | None:
    """Attribute a store-pair diff to the VIX trio — and name the SHAPE, or abstain.

    The attribution test behind the ``recount_vix_divergence`` finding family
    (DEC-VIX-STAMP-WIRE-001): both stores must LOAD cleanly, hold the SAME
    ``run_id`` set, agree on EVERY non-VIX field of every row, and either
    disagree on at least one ``vix_mean``/``vix_min``/``vix_max`` value OR
    differ ONLY in the trio columns' very EXISTENCE (the schema-generation
    shape below). Anything less provable returns ``None`` — an unreadable
    Parquet, a run_id-set drift, a non-VIX field difference, or fully-EQUAL
    rows over IDENTICAL schemas (the byte diff then has an unknown non-row
    cause) all fall back to the caller's normal corrupt/inconclusive ladder.
    Honest attribution only, never a guess (Golden Rule #1).

    A confirmed VIX-only diff is one of THREE cheaply-distinguishable shapes
    (two row-level + one schema-level) — the round-4 panel caught the old
    single "the lake moved" wording claiming a cause it had not established
    for the first shape, and the round-5 panel caught the schema shape
    silently abstaining onto the generic ladder:

    * ``"backfill"`` (row shape) — every differing trio field is ``None`` on
      the ORIGINAL side (a VIX lake that has since GAINED coverage, or a
      part-null store): nothing recorded was contradicted, and a plain redo
      appends the trio as the sanctioned directional None→value backfill —
      no quarantine needed.
    * ``"backfill"`` (schema shape) — every row fully EQUAL (both sides an
      all-``None`` trio) but the ORIGINAL file predates the trio COLUMNS
      themselves: a pre-VIX-generation store loads ``None`` per
      ``_row_from_record``'s optional read, while the recount's reflush
      writes the current schema WITH the three all-null columns appended, so
      such a tree can NEVER be byte-identical. Attributed iff the ONLY
      schema difference is the missing trio
      (:func:`_is_pre_vix_schema_migration`) — same family, same remedy.
    * ``"moved"`` — some recorded trio VALUE disagrees with the recount's
      (value-vs-value, or a recorded value the recount can no longer
      reproduce): the India-VIX lake genuinely moved between the run and the
      recount.
    """
    try:
        original_rows = {row.run_id: row for row in ResultsStore.load(original).rows()}
        scratch_rows = {row.run_id: row for row in ResultsStore.load(scratch).rows()}
    except ResultsStoreError:
        return None  # unreadable/corrupt bytes are never VIX-attributable
    if set(original_rows) != set(scratch_rows):
        return None
    vix_differs = False
    recorded_value_differs = False
    # Null the trio from the ONE public column source (results_store.VIX_FIELDS,
    # CLAUDE.md §2 Rule 11 COMMON) — a fourth trio field added there is nulled
    # here automatically, never a silently-desynced mirrored literal.
    # dict[str, Any]: the trio fields are typed non-None on ResultRow's
    # replace() signature, so a dict[str, None] splat fails strict mypy even
    # though None is the valid null for every nullable trio column.
    vix_nulled: dict[str, Any] = dict.fromkeys(VIX_FIELDS)
    for run_id, ours in original_rows.items():
        theirs = scratch_rows[run_id]
        if ours == theirs:
            continue
        if replace(ours, **vix_nulled) != replace(theirs, **vix_nulled):
            return None  # a non-VIX field differs — not VIX-attributable
        vix_differs = True
        for name in VIX_FIELDS:
            mine = getattr(ours, name)
            if mine != getattr(theirs, name) and mine is not None:
                recorded_value_differs = True
    if not vix_differs:
        # Rows fully EQUAL yet the caller saw a byte diff: the ONE provable
        # cause is the schema-generation migration (a pre-VIX-columns store
        # vs the current writer's appended all-null trio — the round-5 panel
        # shape); anything else honestly abstains to the normal ladder.
        return "backfill" if _is_pre_vix_schema_migration(original, scratch) else None
    return "moved" if recorded_value_differs else "backfill"


def _is_pre_vix_schema_migration(original: Path, scratch: Path) -> bool:
    """``True`` iff *original*'s Parquet schema is exactly *scratch*'s minus the VIX trio.

    The schema test behind :func:`_stores_differ_only_in_vix`'s third shape
    (round-5 panel): a store written BEFORE the trio columns landed loads an
    all-``None`` trio, so its rows compare fully EQUAL to an unstamped
    recount's — yet the recount's reflush writes the CURRENT schema with the
    three all-null trio columns and the bytes differ. That diff is
    attributable iff the ONLY schema difference is the missing trio: the
    scratch must carry all three columns, the original none, and stripping
    the trio from the scratch schema must reproduce the original schema
    EXACTLY (names AND dtypes, order-sensitively). Anything less provable —
    partial trio presence, any other column/dtype drift, an unreadable
    footer — returns ``False`` and the caller abstains to the normal ladder.
    Honest attribution only, never a guess (Golden Rule #1). O(columns) —
    two Parquet footer reads, no row data.
    """
    import polars as pl  # deferred exactly like ResultsStore.load (the data extra)

    try:
        original_schema = list(pl.read_parquet_schema(original).items())
        scratch_schema = list(pl.read_parquet_schema(scratch).items())
    except (pl.exceptions.PolarsError, OSError):
        return False  # an unreadable footer is never VIX-attributable
    original_names = {name for name, _ in original_schema}
    if any(name in original_names for name in VIX_FIELDS):
        return False  # partial/total trio presence — not the pre-VIX generation
    scratch_names = {name for name, _ in scratch_schema}
    if any(name not in scratch_names for name in VIX_FIELDS):
        return False  # the recount side lacks the trio — not this migration
    stripped = [entry for entry in scratch_schema if entry[0] not in VIX_FIELDS]
    return stripped == original_schema


def _hashes_pair_with_stores(
    ours_hash: str, theirs_hash: str, original_store: Path, scratch_store: Path
) -> bool:
    """``True`` iff each recorded content hash pairs with its OWN store's bytes."""
    return (
        ours_hash == hashlib.sha256(original_store.read_bytes()).hexdigest()
        and theirs_hash == hashlib.sha256(scratch_store.read_bytes()).hexdigest()
    )


#: The ONE store-pairing hash field the two re-pair checks below exempt from
#: their non-hash equality gates (it NECESSARILY diverges with a VIX-moved
#: store; each side is instead verified against its OWN store's actual bytes).
_STORE_HASH_FIELD: Final[str] = "store_content_hash"


def _non_hash_fields_equal(
    ours: TupleCompletionSentinel | TupleTrialStats,
    theirs: TupleCompletionSentinel | TupleTrialStats,
) -> bool:
    """``True`` iff every field EXCEPT :data:`_STORE_HASH_FIELD` compares equal.

    Enumerated via :func:`dataclasses.fields` so the gate is drift-proof by
    construction: a field added to either sidecar model LATER is automatically
    part of the equality gate — a hand-enumerated field list would silently
    exclude it, letting a divergence in the new field be attributed into the
    non-corrupt "honest VIX re-pair" family (the unsafe drift direction).
    """
    return all(
        getattr(ours, field.name) == getattr(theirs, field.name)
        for field in fields(ours)
        if field.name != _STORE_HASH_FIELD
    )


def _sentinel_diff_is_the_vix_repair(
    original_sentinel: Path,
    scratch_sentinel: Path,
    original_store: Path,
    scratch_store: Path,
) -> bool:
    """``True`` iff the sentinels differ ONLY as the honest re-pair of VIX-moved stores.

    A completion sentinel embeds its store's ``store_content_hash``
    (DEC-MARK-MANY-SENTINEL-001), so a VIX-only store divergence NECESSARILY
    diverges the sentinel bytes too. The attribution is accepted only when
    BOTH sentinels read valid, agree on every non-hash field (enumerated via
    :func:`_non_hash_fields_equal` — drift-proof, never a hand list), and each hash
    pairs with its OWN store's actual bytes — anything else (a tampered or
    stale sentinel) returns ``False`` and falls back to the caller's normal
    ladder. The caller consults this ONLY after
    :func:`_stores_differ_only_in_vix` confirmed the store pair itself.
    """
    try:
        ours = read_completion_sentinel(original_sentinel)
        theirs = read_completion_sentinel(scratch_sentinel)
    except TupleCompletionError:
        return False  # an unreadable/invalid sentinel is never VIX-attributable
    if not _non_hash_fields_equal(ours, theirs):
        return False  # a non-hash field differs — not the VIX re-pair
    return _hashes_pair_with_stores(
        ours.store_content_hash, theirs.store_content_hash, original_store, scratch_store
    )


def _sidecar_diff_is_the_vix_repair(
    original_sidecar: Path,
    scratch_sidecar: Path,
    original_store: Path,
    scratch_store: Path,
) -> bool:
    """``True`` iff the trial-stats sidecars differ ONLY as the VIX-moved re-pair.

    The sidecar's statistics are VIX-blind (Sharpe moments + prune tallies —
    a VIX-only store movement cannot change them), but it ALSO embeds its
    store's ``store_content_hash`` (ADR-0008 condition 5 pairing identity),
    so it necessarily diverges with a VIX-moved store. Same acceptance law as
    :func:`_sentinel_diff_is_the_vix_repair`: both sidecars valid, every
    non-hash field equal, each hash pairing with its OWN store's bytes.
    """
    try:
        ours = read_tuple_trial_stats(original_sidecar)
        theirs = read_tuple_trial_stats(scratch_sidecar)
    except TrialStatsError:
        return False  # an unreadable/invalid sidecar is never VIX-attributable
    if not _non_hash_fields_equal(ours, theirs):
        return False  # a statistic differs — not the VIX re-pair
    return _hashes_pair_with_stores(
        ours.store_content_hash, theirs.store_content_hash, original_store, scratch_store
    )


def compare_recount(
    original_root: Path,
    scratch_root: Path,
    plan: dict[Path, tuple[SweepTuple, SweepGridConfig, bool]],
    *,
    context_match: bool,
    cell_evidence: bool = False,
) -> TreeVerifyReport:
    """Tier 3: byte-diff the original tree against a freshly re-swept scratch tree.

    Stage-3 CELL stores (``is_grid_tuple=False`` plan entries) route to a
    DISTINCT finding-code family gated by *cell_evidence* (DEC-STAGE3-RECOUNT-001
    conditions B1/B2/B5): cell divergence is verdict-grade only under the full
    cell gate (context AND recorded knobs AND optimizer versions — the caller
    computes it), degrades to a dependent warn when the same leaderboard's GRID
    recount diverged (scratch cells derive from scratch grid stores), and never
    aliases ``recount_divergence`` (the heal's evidence code).

    The recount itself is NOT performed here (§11 COMMON — the caller composes
    the existing 100%-tested ``run_sweep_grid`` into *scratch_root*, exactly
    the shipped A/B tree-oracle pattern, never a hand-rolled re-implementation
    that could drift from the engine). This function is the pure comparator:
    for every *plan*-expected tuple it diffs the store, the trial-stats
    sidecar, and the completion sentinel byte-for-byte.

    The VALIDITY GATE (the hostile review's condition 3b): *context_match*
    states whether ALL THREE recorded reproducibility strings match the
    current environment (code commit, indicator version, a fresh
    data-snapshot derivation over the loaded bars). Divergence is
    verdict-grade ``recount_divergence`` (corrupt) ONLY under a full match;
    under any mismatch it degrades to ``recount_divergence_inconclusive``
    (warn) — a recount under changed inputs is NOT evidence of corruption
    (ADR-0008's two drift channels), and condemning a good store on wrong
    inputs is the catastrophic failure mode this gate exists to prevent.
    Byte-identity under a MISMATCH is still reported clean (silence): it is
    strong reassurance, never a downgrade.

    THE VIX RESIDUAL (DEC-VIX-STAMP-WIRE-001 round-2 BLOCKER fold): the
    REFERENCE-ONLY ``vix_mean``/``vix_min``/``vix_max`` trio derives from the
    India-VIX *lake*, which sits deliberately OUTSIDE the recorded context
    AND the run identity — so a VIX re-pull / ``1day`` re-derivation between
    the original run and the recount byte-diverges the trio even under a
    FULLY MATCHED context. A diverging results-store Parquet whose loaded
    rows prove the divergence is VIX-ONLY (same run_id set, every non-VIX
    field equal, at least one trio field differing) is therefore classified
    ``recount_vix_divergence`` (grid) / ``cell_recount_vix_divergence``
    (cell) at severity ``warn`` — NEVER ``corrupt`` and never heal-grade (the
    heal consumes only ``recount_divergence``): every reproducibility-bearing
    column provably reproduced; only the reference annotation moved with its
    lake. The pairing-artifact divergences that necessarily ride a VIX-only
    store movement — BOTH the trial-stats sidecar AND the completion sentinel
    embed the store's content hash — are attributed into the SAME family, but
    only when the artifact reads valid, agrees on every non-hash field, and
    each recorded hash pairs with its OWN store's actual bytes
    (:func:`_sidecar_diff_is_the_vix_repair` /
    :func:`_sentinel_diff_is_the_vix_repair`). An UNREADABLE store, a
    run_id-set mismatch, any non-VIX field difference, or a pairing artifact
    that differs in anything but the honest re-pair falls through to the
    normal corrupt/inconclusive ladder.

    Parameters
    ----------
    original_root, scratch_root
        The verified tree and the scratch tree the shadow re-sweep produced.
        Must be distinct directories (the recount must never write into the
        original tree — the preload at ``load_or_create_store`` would mask
        inflation otherwise).
    plan
        :func:`plan_expected_stores` output for *original_root* (the
        attribution map; scratch paths re-derive by relative path).
    context_match
        Whether the recorded context fully matches the current environment.

    Raises
    ------
    SweepVerifyError
        If either root is not a directory, or the roots are not distinct.
    """
    for name, root in (("original_root", original_root), ("scratch_root", scratch_root)):
        if not root.is_dir():
            raise SweepVerifyError(f"{name} not found or not a directory: {root}")
    if original_root.resolve() == scratch_root.resolve():
        raise SweepVerifyError(
            "scratch_root must be DISTINCT from original_root — the recount "
            "never writes into the verified tree"
        )
    findings: list[VerifyFinding] = []
    checked = 0
    grid_corrupt_leaderboards: set[tuple[str, str]] = set()

    def _diff_store(store_path: Path, *, is_grid_tuple: bool, leaderboard: tuple[str, str]) -> None:
        nonlocal checked
        sentinel_path = completion_sentinel_path(store_path)
        artifacts = (
            store_path,
            trial_stats_sidecar_path(store_path),
            sentinel_path,
        )
        scratch_store = scratch_root / store_path.relative_to(original_root)
        # Memoised store-pair VIX verdict (one load per tuple; all three
        # artifacts of a VIX-diverged tuple consult it). The memo holds the
        # shape string — moved or backfill — or the not-VIX-attributable
        # None; the separate computed flag distinguishes a memo that has not
        # run yet from one that ran and honestly abstained.
        vix_only_memo: str | None = None
        vix_memo_computed = False

        def _vix_only_store_pair() -> str | None:
            nonlocal vix_only_memo, vix_memo_computed
            if not vix_memo_computed:
                vix_only_memo = _stores_differ_only_in_vix(store_path, scratch_store)
                vix_memo_computed = True
            return vix_only_memo

        def _artifact_vix_shape(original: Path, scratch: Path) -> str | None:
            """The VIX-divergence SHAPE for this artifact, or None (not attributable)."""
            if original == store_path:
                return _vix_only_store_pair()
            # A pairing artifact (sidecar/sentinel) is attributable only when
            # BOTH stores exist AND the store pair itself proved VIX-only.
            if not (store_path.exists() and scratch_store.exists()):
                return None
            shape = _vix_only_store_pair()
            if shape is None:
                return None
            if original == sentinel_path:
                return (
                    shape
                    if _sentinel_diff_is_the_vix_repair(
                        original, scratch, store_path, scratch_store
                    )
                    else None
                )
            return (
                shape
                if _sidecar_diff_is_the_vix_repair(original, scratch, store_path, scratch_store)
                else None
            )

        for original in artifacts:
            scratch = scratch_root / original.relative_to(original_root)
            rel = _rel(original_root, original)
            original_exists = original.exists()
            scratch_exists = scratch.exists()
            if not original_exists and not scratch_exists:
                continue  # absent in BOTH worlds — nothing to compare
            checked += 1
            if not scratch_exists:
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code="recount_scratch_absent",
                        severity="warn",
                        detail=(
                            "the shadow re-sweep produced no counterpart — "
                            "bars unavailable in the current lake; "
                            "unverifiable at this tier, never condemned"
                        ),
                    )
                )
                continue
            if not original_exists:
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code="recount_original_absent",
                        severity="warn",
                        detail=(
                            "the recount produced this artifact but the tree "
                            "lacks it — a mid-sweep PENDING tuple or a lost "
                            "artifact; not classified as corruption"
                        ),
                    )
                )
                continue
            if original.read_bytes() == scratch.read_bytes():
                continue  # byte-identical — verified (silence is the verdict)
            # VIX-only divergence (see the docstring's VIX RESIDUAL). All
            # THREE artifacts necessarily ride a VIX-only lake movement: the
            # results-store Parquet (the trio columns) and BOTH pairing
            # artifacts — the trial-stats sidecar + the completion sentinel
            # each embed the store's content hash. A pairing artifact is
            # attributed ONLY when it reads valid, agrees on every non-hash
            # field, and re-pairs its OWN store's actual bytes; anything less
            # provable stays on the normal ladder.
            vix_shape = _artifact_vix_shape(original, scratch)
            if vix_shape is not None:
                # Two honestly-distinguished variants (round-4 panel): the
                # old single detail definitively claimed "the lake moved",
                # which is FALSE for the reachable backfill shape (a
                # pre-VIX-column store recounted under any VIX-carrying
                # lake) — and its quarantine remedy is over-heavy there.
                if vix_shape == "moved":
                    cause_and_remedy = (
                        "The India-VIX lake moved between the recorded run and "
                        "this recount (a VIX re-pull or a derived-1day "
                        "re-derivation); VIX is deliberately OUTSIDE the "
                        "recorded context and the run identity (CLAUDE.md §2 "
                        "Rule 3), so this is NOT evidence of corruption and "
                        "never heal-grade. Remedy: quarantine + redo the tuple "
                        "to re-stamp against the current VIX series, or keep "
                        "the older stamps"
                    )
                else:
                    cause_and_remedy = (
                        "The original rows carry NO trio values — a store "
                        "written before the VIX columns landed, or a VIX lake "
                        "that has since GAINED coverage; nothing recorded was "
                        "contradicted. VIX is deliberately OUTSIDE the recorded "
                        "context and the run identity (CLAUDE.md §2 Rule 3), so "
                        "this is NOT evidence of corruption and never "
                        "heal-grade. Remedy: a plain redo appends the trio as "
                        "the sanctioned directional None→value backfill — no "
                        "quarantine needed"
                    )
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code=(
                            "recount_vix_divergence"
                            if is_grid_tuple
                            else "cell_recount_vix_divergence"
                        ),
                        severity="warn",
                        detail=(
                            "ONLY the REFERENCE-ONLY vix_mean/vix_min/vix_max trio "
                            "diverges (the pairing sidecar/sentinel re-pair that "
                            "store's hash) — every run_id and every non-VIX column "
                            f"reproduced exactly. {cause_and_remedy}"
                        ),
                    )
                )
                continue
            if is_grid_tuple:
                if context_match:
                    grid_corrupt_leaderboards.add(leaderboard)
                    findings.append(
                        VerifyFinding(
                            path=rel,
                            code="recount_divergence",
                            severity="corrupt",
                            detail=(
                                "bytes diverge from the shadow re-sweep under a "
                                "FULLY MATCHED recorded context — verdict-grade "
                                "corruption (the only state heal-grade evidence "
                                "may ever come from)"
                            ),
                        )
                    )
                else:
                    findings.append(
                        VerifyFinding(
                            path=rel,
                            code="recount_divergence_inconclusive",
                            severity="warn",
                            detail=(
                                "bytes diverge BUT the recorded context does not "
                                "match the current environment — NOT evidence of "
                                "corruption (changed code/indicators/lake change "
                                "results legitimately; ADR-0008 drift channels)"
                            ),
                        )
                    )
            # Stage-3 cell stores (DEC-STAGE3-RECOUNT-001): a DISTINCT code
            # family — the heal consumes ONLY `recount_divergence` (its C1
            # gate raises on non-grid paths), so cell findings must never
            # alias the grid code (condition B2).
            elif leaderboard in grid_corrupt_leaderboards:
                # Condition B5: scratch cells derive from scratch GRID stores,
                # so cell divergence under grid corruption in the same
                # leaderboard is collateral, not independent evidence.
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code="cell_recount_divergence_dependent",
                        severity="warn",
                        detail=(
                            "cell bytes diverge, but this leaderboard's GRID "
                            "recount already diverged — the scratch cells derive "
                            "from the scratch grid stores, so this is collateral, "
                            "not independent evidence"
                        ),
                    )
                )
            elif context_match and cell_evidence:
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code="cell_recount_divergence",
                        severity="corrupt",
                        detail=(
                            "Stage-3 cell bytes diverge under a fully matched "
                            "context AND matched recorded knobs/optimizer "
                            "versions — verdict-grade (NOT heal-grade: cells "
                            "carry no sentinels and healing stays grid-only)"
                        ),
                    )
                )
            else:
                findings.append(
                    VerifyFinding(
                        path=rel,
                        code="cell_recount_divergence_inconclusive",
                        severity="warn",
                        detail=(
                            "Stage-3 cell bytes diverge without full cell "
                            "evidence (context/knobs/optimizer-version gate not "
                            "fully matched) — NOT evidence of corruption "
                            "(condition B1)"
                        ),
                    )
                )

    # GRID stores first — their verdicts feed the cell collateral rule (B5).
    ordered = sorted(plan)
    for store_path in ordered:
        tuple_, _, is_grid = plan[store_path]
        if is_grid:
            _diff_store(
                store_path,
                is_grid_tuple=True,
                leaderboard=(tuple_.instrument, tuple_.tf.label),
            )
    for store_path in ordered:
        tuple_, _, is_grid = plan[store_path]
        if not is_grid:
            _diff_store(
                store_path,
                is_grid_tuple=False,
                leaderboard=(tuple_.instrument, tuple_.tf.label),
            )

    report = TreeVerifyReport(
        root=str(original_root),
        tier="recount",
        checked_stores=checked,
        findings=tuple(findings),
    )
    _LOG.info(
        "sweep_verify.recount",
        root=str(original_root),
        scratch=str(scratch_root),
        context_match=context_match,
        cell_evidence=cell_evidence,
        artifacts=checked,
        corrupt=len(report.corrupt),
        findings=len(report.findings),
    )
    return report


__all__ = [
    "QUARANTINE_DIRNAME",
    "SEVERITIES",
    "RecordedContext",
    "RecordedStage3Audit",
    "RecordedStage3Knobs",
    "SweepVerifyError",
    "TreeVerifyReport",
    "VerifyFinding",
    "compare_recount",
    "plan_expected_stores",
    "recorded_context_from_audit",
    "recorded_stage3_from_audit",
    "verify_identity",
    "verify_structural",
]
