"""Sweep results-tree HEAL — quarantine, never delete (DEC-SWEEP-HEAL-001).

The recovery half of the doctor (#656/#657 shipped detection, report-only):
move a Tier-3-condemned tuple's artifact trio into a quarantine subtree, wipe
the two resume-skip stores so the next ``sweep-all`` actually redoes the
condemned tuples, and leave everything else untouched. Hostile-reviewed
(11 binding conditions, MIGRATION.md DEC-SWEEP-HEAL-001); the load-bearing
ones this module enforces:

* **C1 — evidence is a STRUCTURED plan from the recount run itself**, never
  re-typed flags or Tier-1/2 findings: ``rows_exceed_top_k`` compares against
  the ``--top`` flag, which is in neither the path digest nor the recorded
  context, so a wrong flag reads tree-wide false CORRUPT — only
  ``recount_divergence`` (byte divergence under a fully matched recorded
  context) is heal-grade.
* **C2 — byte-hash freshness, whole-heal refusal.** The plan records each
  artifact's SHA-256 at condemnation; ANY mismatch at heal time means the
  tree moved since the report → refuse everything, demand re-verification.
  The verdict attaches to frozen bytes, so no recount re-run is needed — and
  pointed at the wrong root, nothing matches, so the heal refuses.
* **C4 — move order store → sidecar → sentinel.** Every crash prefix is a
  state the engine already self-heals: a missing store unpairs the sentinel
  (``_sentinel_resume`` → full redo from a fresh store); the reverse order
  would leave a sentinel-less store that ``load_or_create_store`` PRELOADS —
  re-appending the corrupt rows (the #650 wedge class).
* **C5 — wipe BOTH ``checkpoint/`` and ``dedup-cache/`` wholesale.** Either
  skip tier ALONE suffices to skip the condemned tuple forever (the resume
  skip is by index; the cache skip never consults the store), so a partial
  wipe is NOT self-healing — the idempotent heal re-run is THE crash
  recovery, never "the next sweep fixes it".
* **C7 — mass-quarantine refusal.** The recorded-context triple does not pin
  the venv (a polars drift could byte-diverge every Parquet under a "matched"
  context); condemning ≥ half the expected stores refuses without the
  explicit override.

Honest envelope (Golden Rule #10)
---------------------------------
* Reversible by construction: artifacts are MOVED (same-filesystem
  ``os.replace``), never deleted; the wiped checkpoint/dedup-cache are
  regenerable; intact tuples re-enter via their completion sentinels at one
  JSON read + one SHA-256 each.
* The concurrency guard is best-effort (the caller checks the progress
  file); multi-process sweeps over one root remain out of contract (#654).
* Heal+redo requires an UNCHANGED lake: the store-path digest folds in
  ``data_snapshot_hash``, so a drifted lake re-sweeps to new paths while the
  healed paths stay missing (the caller prints this, C11).
* Stage-3 cell stores are NOT healable: they now carry their OWN recount
  evidence codes (``cell_recount_divergence*``, DEC-STAGE3-RECOUNT-001) which
  this module deliberately does not consume — cells have no sentinels, need no
  checkpoint/dedup wipe to redo (``run_stage3_pass`` re-derives every cell on
  any ``--stage3-enable`` sweep), and healing stays grid-only (C10); plan
  construction refuses non-grid-tuple paths.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from os import replace as _os_replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.guarded_read import read_regular_bytes, read_regular_text
from brutex.engine.sweep_ops_rust import (
    routed_heal_mismatch,
    sweep_ops_rust_enabled,
)
from brutex.engine.sweep_verify import (
    QUARANTINE_DIRNAME,
    RecordedContext,
    TreeVerifyReport,
)
from brutex.engine.trial_stats import _validate_hex
from brutex.models import BruteXError

if TYPE_CHECKING:
    from collections.abc import Callable

    from brutex.engine.sweep_orchestrator import SweepGridConfig, SweepTuple
    from brutex.orchestrator.audit import AuditWriter

_LOG = structlog.get_logger("brutex.engine.sweep_heal")

#: Heal-plan schema version — bump on any incompatible field change.
HEAL_PLAN_SCHEMA_VERSION: Final[int] = 1

#: Where the doctor emits heal plans under a results root.
HEAL_PLAN_DIRNAME: Final[str] = "heal"

#: The artifact suffixes of one tuple's trio, IN MOVE ORDER (C4): the bare
#: store FIRST (a missing store unpairs the sentinel → engine self-heal),
#: the completion sentinel LAST (its presence implies the trio is whole).
ARTIFACT_SUFFIXES: Final[tuple[str, ...]] = ("", ".trials.json", ".complete.json")

#: C7 — refuse a heal condemning at least this fraction of expected stores.
MASS_QUARANTINE_FRACTION: Final[float] = 0.5

#: The two resume-skip stores the wipe removes wholesale (C5).
WIPE_DIRNAMES: Final[tuple[str, ...]] = ("dedup-cache", "checkpoint")

#: The only heal-grade finding code (C1).
_HEAL_GRADE_CODE: Final[str] = "recount_divergence"

_TUPLE_DIGEST_LEN: Final[int] = 16
_SHA256_LEN: Final[int] = 64


class SweepHealError(BruteXError):
    """Raised for any heal failure — bad plan, refused assessment, unsafe target."""


def _sha256_or_none(path: Path) -> str | None:
    """SHA-256 hex of *path*'s bytes, or ``None`` when the file is absent."""
    if not path.exists():
        return None
    return hashlib.sha256(read_regular_bytes(path)).hexdigest()


def _validate_hex_field(name: str, value: str, length: int) -> None:
    """Hex validation via the ONE validator (§11 COMMON), re-typed."""
    from brutex.engine.trial_stats import TrialStatsError

    try:
        _validate_hex(name, value, length)
    except TrialStatsError as exc:
        raise SweepHealError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class HealEntry:
    """One condemned tuple: its store path + the frozen evidence hashes (C2).

    Parameters
    ----------
    rel_store
        The store's path RELATIVE to the results root.
    tuple_digest
        The tuple's 16-hex path digest (the filename's collision guard).
    artifact_hashes
        SHA-256 per :data:`ARTIFACT_SUFFIXES` key at CONDEMNATION time
        (``None`` = the artifact was absent then). The store hash is
        mandatory — ``recount_divergence`` requires existing bytes.

    Raises
    ------
    SweepHealError
        On a malformed path/digest/hash set.
    """

    rel_store: str
    tuple_digest: str
    artifact_hashes: dict[str, str | None]

    def __post_init__(self) -> None:
        if not self.rel_store or Path(self.rel_store).is_absolute():
            raise SweepHealError(f"rel_store must be a relative path, got {self.rel_store!r}")
        if QUARANTINE_DIRNAME in Path(self.rel_store).parts:
            raise SweepHealError(f"rel_store may not point into quarantine: {self.rel_store!r}")
        _validate_hex_field("tuple_digest", self.tuple_digest, _TUPLE_DIGEST_LEN)
        if set(self.artifact_hashes) != set(ARTIFACT_SUFFIXES):
            raise SweepHealError(
                f"artifact_hashes keys must be exactly {ARTIFACT_SUFFIXES}, "
                f"got {sorted(self.artifact_hashes)}"
            )
        if self.artifact_hashes[""] is None:
            raise SweepHealError("the store hash is mandatory (divergence requires bytes)")
        for suffix, value in self.artifact_hashes.items():
            if value is not None:
                _validate_hex_field(f"artifact_hashes[{suffix!r}]", value, _SHA256_LEN)


@dataclass(frozen=True, slots=True)
class HealPlan:
    """The doctor-emitted, structured heal evidence (C1).

    Parameters
    ----------
    created_utc
        Filesystem-safe creation stamp (e.g. ``20260611T103000Z``) — keys the
        quarantine directory, so heal re-runs of ONE plan are idempotent into
        one directory and a later re-condemnation lands in a different one.
    grid_identity
        The sweep grid's 64-hex identity — wrong flags cannot reach the heal.
    context
        The recorded reproducibility triple the recount matched against.
    n_expected_stores
        Plan-expected grid stores at recount time (the C7 denominator).
    entries
        The condemned tuples, sorted by ``rel_store``, unique.

    Raises
    ------
    SweepHealError
        On any malformed field, unsorted/duplicate entries, or an unsafe stamp.
    """

    created_utc: str
    grid_identity: str
    context: RecordedContext
    n_expected_stores: int
    entries: tuple[HealEntry, ...]

    def __post_init__(self) -> None:
        if not self.created_utc or not all(c.isalnum() or c in "_-" for c in self.created_utc):
            raise SweepHealError(
                f"created_utc must be a filesystem-safe stamp, got {self.created_utc!r}"
            )
        _validate_hex_field("grid_identity", self.grid_identity, _SHA256_LEN)
        if self.n_expected_stores < 1:
            raise SweepHealError(f"n_expected_stores must be >= 1, got {self.n_expected_stores}")
        rels = [entry.rel_store for entry in self.entries]
        if rels != sorted(rels) or len(set(rels)) != len(rels):
            raise SweepHealError("entries must be sorted by rel_store and unique")

    @property
    def quarantine_subdir(self) -> str:
        """The plan-keyed quarantine directory name (idempotent re-runs)."""
        return f"{self.created_utc}-{self.grid_identity[:8]}"


def build_heal_plan(
    report: TreeVerifyReport,
    plan_map: dict[Path, tuple[SweepTuple, SweepGridConfig, bool]],
    *,
    root: Path,
    grid_identity: str,
    context: RecordedContext,
    created_utc: str,
) -> HealPlan:
    """Build the structured heal plan from a JUST-RUN recount report.

    Called by the doctor immediately after :func:`compare_recount` so the
    recorded hashes ARE the condemnation-time bytes (C2). Collects ONLY
    ``recount_divergence`` findings (C1), maps each artifact path back to its
    store, refuses any non-grid-tuple attribution (C10 — Stage-3 cells carry
    no recount evidence), and freezes the trio's current hashes.

    Raises
    ------
    SweepHealError
        If *report* is not a recount report, a condemned path is not an
        expected grid-tuple store, or a condemned store is unreadable.
    """
    if report.tier != "recount":
        raise SweepHealError(f"heal evidence must come from a recount report, got {report.tier!r}")
    condemned_rels: set[str] = set()
    for finding in report.findings:
        if finding.code != _HEAL_GRADE_CODE:
            continue
        rel = finding.path
        for suffix in ARTIFACT_SUFFIXES[1:]:
            rel = rel.removesuffix(suffix)
        condemned_rels.add(rel)
    grid_stores = {
        store.relative_to(root).as_posix(): store
        for store, (_, _, is_grid) in plan_map.items()
        if is_grid
    }
    entries: list[HealEntry] = []
    for rel in sorted(condemned_rels):
        store = grid_stores.get(rel)
        if store is None:
            raise SweepHealError(
                f"condemned path {rel!r} is not an expected grid-tuple store — "
                f"Stage-3 cells / foreign files are not healable (C10)"
            )
        hashes = {
            suffix: _sha256_or_none(store.with_name(store.name + suffix))
            for suffix in ARTIFACT_SUFFIXES
        }
        if hashes[""] is None:
            raise SweepHealError(f"condemned store vanished before plan build: {rel!r}")
        entries.append(
            HealEntry(
                rel_store=rel,
                tuple_digest=store.stem.split("_")[-1],
                artifact_hashes=hashes,
            )
        )
    return HealPlan(
        created_utc=created_utc,
        grid_identity=grid_identity,
        context=context,
        n_expected_stores=sum(1 for _, _, is_grid in plan_map.values() if is_grid),
        entries=tuple(entries),
    )


def render_heal_plan(plan: HealPlan) -> bytes:
    """Serialize *plan* to deterministic JSON bytes (sorted keys, compact)."""
    payload = {
        "schema_version": HEAL_PLAN_SCHEMA_VERSION,
        "created_utc": plan.created_utc,
        "grid_identity": plan.grid_identity,
        "context": {
            "data_snapshot_hash": plan.context.data_snapshot_hash,
            "indicator_version": plan.context.indicator_version,
            "code_commit_sha": plan.context.code_commit_sha,
        },
        "n_expected_stores": plan.n_expected_stores,
        "entries": [
            {
                "rel_store": entry.rel_store,
                "tuple_digest": entry.tuple_digest,
                "artifact_hashes": entry.artifact_hashes,
            }
            for entry in plan.entries
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def write_heal_plan(root: Path, plan: HealPlan) -> Path:
    """Atomically write *plan* under ``<root>/heal/`` and return its path."""
    from brutex.engine.results_store import _atomic_write

    path = root / HEAL_PLAN_DIRNAME / f"plan-{plan.quarantine_subdir}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, render_heal_plan(plan))
    return path


def read_heal_plan(path: Path) -> HealPlan:
    """Read + validate a plan written by :func:`write_heal_plan`.

    Raises
    ------
    SweepHealError
        On a missing/corrupt/unsupported/malformed plan file.
    """
    if not path.exists():
        raise SweepHealError(f"heal plan not found at {path}")
    try:
        payload = json.loads(read_regular_text(path))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SweepHealError(f"heal plan at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SweepHealError(f"heal plan at {path} must be a JSON object")
    if payload.get("schema_version") != HEAL_PLAN_SCHEMA_VERSION:
        raise SweepHealError(
            f"unsupported heal-plan schema_version {payload.get('schema_version')!r} at {path}"
        )
    try:
        context = payload["context"]
        return HealPlan(
            created_utc=str(payload["created_utc"]),
            grid_identity=str(payload["grid_identity"]),
            context=RecordedContext(
                data_snapshot_hash=str(context["data_snapshot_hash"]),
                indicator_version=str(context["indicator_version"]),
                code_commit_sha=str(context["code_commit_sha"]),
            ),
            n_expected_stores=int(payload["n_expected_stores"]),
            entries=tuple(
                HealEntry(
                    rel_store=str(entry["rel_store"]),
                    tuple_digest=str(entry["tuple_digest"]),
                    artifact_hashes={
                        str(k): (None if v is None else str(v))
                        for k, v in dict(entry["artifact_hashes"]).items()
                    },
                )
                for entry in payload["entries"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SweepHealError(f"heal plan at {path} has a malformed field: {exc}") from exc


# ── Assessment (pure decision; dry-run IS this output) ──────────────────────


@dataclass(frozen=True, slots=True)
class HealAction:
    """One tuple's decided action: ``move_trio`` (all artifacts at source,
    hashes fresh), ``complete_partial`` (store already moved by a crashed
    heal; matching leftovers remain), or ``already_quarantined`` (no-op)."""

    rel_store: str
    decision: str


@dataclass(frozen=True, slots=True)
class HealAssessment:
    """The whole-heal verdict: per-tuple actions, or tree-level refusals.

    ``refusals`` non-empty ⇒ the heal must not execute (C2 whole-heal
    refusal / C6 / C7); the dry-run output and the execute input are THIS
    object, so preview ≡ action.
    """

    refusals: tuple[str, ...]
    actions: tuple[HealAction, ...]

    @property
    def ok(self) -> bool:
        """``True`` iff nothing refused."""
        return not self.refusals

    def render(self, root: Path, plan: HealPlan) -> str:
        """Deterministic human preview of exactly what execute would do."""
        lines = [f"sweep-heal — {root} · plan {plan.quarantine_subdir}"]
        if self.refusals:
            lines.append(f"  REFUSED ({len(self.refusals)} reason(s)):")
            lines.extend(f"    - {reason}" for reason in self.refusals)
            return "\n".join(lines)
        dest = Path(QUARANTINE_DIRNAME) / plan.quarantine_subdir
        for action in self.actions:
            lines.append(f"  [{action.decision.upper()}] {action.rel_store} → {dest}/…")
        lines.append(f"  then wipe: {', '.join(WIPE_DIRNAMES)} (wholesale, C5)")
        return "\n".join(lines)


def assess_heal(
    root: Path,
    plan: HealPlan,
    *,
    sweep_running: bool,
    allow_mass_quarantine: bool = False,
) -> HealAssessment:
    """Decide, per condemned tuple, what the heal would do — refusing on doubt.

    Freshness (C2): every artifact still at its source must hash to the
    plan-recorded value, and a plan-absent artifact must still be absent —
    ANY deviation refuses the WHOLE heal (the tree changed since
    condemnation; a since-rewritten good tuple must never be quarantined).
    A crashed prior heal is recognized by exact-match leftovers
    (``complete_partial``) or a fully-moved, hash-matching quarantined trio
    (``already_quarantined``) — anything else refuses.

    Raises
    ------
    SweepHealError
        If *root* is not a directory.
    """
    if not root.is_dir():
        raise SweepHealError(f"results root not found or not a directory: {root}")
    refusals: list[str] = []
    if sweep_running:
        refusals.append(
            "a sweep appears to be RUNNING on this root (fresh progress file) — "
            "stop it (and any sweep-daemon) first (C6)"
        )
    if (
        not allow_mass_quarantine
        and len(plan.entries) * 2 >= plan.n_expected_stores
        and plan.entries
    ):
        refusals.append(
            f"plan condemns {len(plan.entries)} of {plan.n_expected_stores} expected "
            f"stores (>= {MASS_QUARANTINE_FRACTION:.0%}) — a library/venv drift can "
            f"byte-diverge EVERYTHING under a 'matched' context (the recorded triple "
            f"does not pin the venv); pass --allow-mass-quarantine to override (C7)"
        )
    actions: list[HealAction] = []
    qdir = root / QUARANTINE_DIRNAME / plan.quarantine_subdir
    for entry in plan.entries:
        store = root / entry.rel_store
        src_hashes = {
            suffix: _sha256_or_none(store.with_name(store.name + suffix))
            for suffix in ARTIFACT_SUFFIXES
        }
        if src_hashes == entry.artifact_hashes:
            actions.append(HealAction(rel_store=entry.rel_store, decision="move_trio"))
            continue
        # MISMATCH TAIL — dest hashes are computed LAZILY (only past the
        # happy arm); the pure decision may route through the sweep-ops
        # crate (DEC-RUST-SWEEP-OPS-001 slice 4), the refusal MESSAGE and
        # the destructive move stay Python.
        dest_store = qdir / entry.rel_store
        dest_hashes = {
            suffix: _sha256_or_none(dest_store.with_name(dest_store.name + suffix))
            for suffix in ARTIFACT_SUFFIXES
        }
        src_values = [src_hashes[suffix] for suffix in ARTIFACT_SUFFIXES]
        plan_values = [entry.artifact_hashes[suffix] for suffix in ARTIFACT_SUFFIXES]
        dest_values = [dest_hashes[suffix] for suffix in ARTIFACT_SUFFIXES]
        if sweep_ops_rust_enabled():

            def _oracle(
                src: list[str | None] = src_values,
                plan: list[str | None] = plan_values,
                dest: list[str | None] = dest_values,
            ) -> str:
                return _mismatch_decision_python(src, plan, dest)

            decision = routed_heal_mismatch(
                src_values, plan_values, dest_values, python_law=_oracle
            )
        else:
            decision = _mismatch_decision_python(src_values, plan_values, dest_values)
        if decision == "already_quarantined":
            actions.append(HealAction(rel_store=entry.rel_store, decision="already_quarantined"))
            continue
        if decision == "complete_partial":
            actions.append(HealAction(rel_store=entry.rel_store, decision="complete_partial"))
            continue
        refusals.append(
            f"{entry.rel_store}: artifact bytes do not match the plan's "
            f"condemnation-time hashes — the tree changed since the report; "
            f"re-run sweep-verify --recount --emit-heal-plan (C2)"
        )
    return HealAssessment(refusals=tuple(refusals), actions=tuple(actions))


def _mismatch_decision_python(
    src_values: list[str | None],
    plan_values: list[str | None],
    dest_values: list[str | None],
) -> str:
    """The pure mismatch-tail decision — the retained Python oracle.

    The verbatim :func:`assess_heal` C2 tail over the ordered
    :data:`ARTIFACT_SUFFIXES` hash triples (the routed twin is the crate's
    ``heal_mismatch_decision`` — DEC-RUST-SWEEP-OPS-001 slice 4): a fully
    moved, plan-matching quarantined trio → ``already_quarantined``; a
    moved store with only plan-matching sidecar leftovers →
    ``complete_partial``; anything else → ``refuse`` (the caller owns the
    refusal message).
    """
    if all(value is None for value in src_values) and dest_values == plan_values:
        return "already_quarantined"
    sidecar_range = range(1, len(ARTIFACT_SUFFIXES))
    if (
        src_values[0] is None
        and all(src_values[i] in (None, plan_values[i]) for i in sidecar_range)
        and any(src_values[i] is not None for i in sidecar_range)
    ):
        return "complete_partial"
    return "refuse"


# ── Execution (injected FS primitives — the execute_wipe pattern) ───────────


@dataclass(frozen=True, slots=True)
class HealReport:
    """What execute actually did (counts + the wiped directories)."""

    moved: int
    completed_partial: int
    already_quarantined: int
    wiped: tuple[str, ...]


def _assert_contained(root: Path, target: Path) -> None:
    """Refuse a symlinked or root-escaping wipe target (the wipe discipline)."""
    if target.is_symlink():
        raise SweepHealError(f"refusing to remove a symlink: {target}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise SweepHealError(f"refusing to remove {target}: outside the results root {root}")


def execute_heal(
    root: Path,
    plan: HealPlan,
    assessment: HealAssessment,
    *,
    audit: AuditWriter,
    replace: Callable[[str, str], None] = _os_replace,
    rmtree: Callable[[Path], None] = shutil.rmtree,
) -> HealReport:
    """Execute an OK assessment: quarantine the trios, then wipe the skip stores.

    Ordering (every crash window converges via an idempotent heal re-run —
    the ONLY recovery, C5): ``HEAL_START`` audit line → pre-create every
    destination dir (disk-full fails BEFORE any tree mutation) → per tuple,
    store → sidecar → sentinel moves (C4) with one ``HEAL_QUARANTINE`` line
    per tuple → wipe ``dedup-cache/`` then ``checkpoint/`` (guarded) →
    ``HEAL_WIPE`` → ``HEAL_COMPLETE``.

    Raises
    ------
    SweepHealError
        If the assessment carries refusals, or a wipe target is unsafe.
    """
    if not assessment.ok:
        raise SweepHealError("refusing to execute a refused assessment (C2/C6/C7)")
    qdir = root / QUARANTINE_DIRNAME / plan.quarantine_subdir
    audit.append(
        action="HEAL_START",
        subject=f"heal:{plan.quarantine_subdir}",
        payload={
            "grid_identity": plan.grid_identity,
            "condemned": len(plan.entries),
            "actions": [a.decision for a in assessment.actions],
        },
    )
    for action in assessment.actions:
        (qdir / action.rel_store).parent.mkdir(parents=True, exist_ok=True)
    moved = completed = skipped = 0
    for action in assessment.actions:
        if action.decision == "already_quarantined":
            skipped += 1
            continue
        store = root / action.rel_store
        dest_store = qdir / action.rel_store
        for suffix in ARTIFACT_SUFFIXES:
            src = store.with_name(store.name + suffix)
            if src.exists():
                replace(str(src), str(dest_store.with_name(dest_store.name + suffix)))
        audit.append(
            action="HEAL_QUARANTINE",
            subject=f"heal:{plan.quarantine_subdir}",
            payload={"rel_store": action.rel_store, "decision": action.decision},
        )
        if action.decision == "move_trio":
            moved += 1
        else:
            completed += 1
    wiped: list[str] = []
    for dirname in WIPE_DIRNAMES:
        target = root / dirname
        if target.exists():
            _assert_contained(root, target)
            rmtree(target)
            wiped.append(dirname)
    audit.append(
        action="HEAL_WIPE",
        subject=f"heal:{plan.quarantine_subdir}",
        payload={"wiped": wiped},
    )
    audit.append(
        action="HEAL_COMPLETE",
        subject=f"heal:{plan.quarantine_subdir}",
        payload={"moved": moved, "completed_partial": completed, "already_quarantined": skipped},
    )
    report = HealReport(
        moved=moved,
        completed_partial=completed,
        already_quarantined=skipped,
        wiped=tuple(wiped),
    )
    _LOG.info(
        "sweep_heal.complete",
        root=str(root),
        plan=plan.quarantine_subdir,
        moved=moved,
        completed_partial=completed,
        already_quarantined=skipped,
        wiped=wiped,
    )
    return report


__all__ = [
    "ARTIFACT_SUFFIXES",
    "HEAL_PLAN_DIRNAME",
    "HEAL_PLAN_SCHEMA_VERSION",
    "MASS_QUARANTINE_FRACTION",
    "WIPE_DIRNAMES",
    "HealAction",
    "HealAssessment",
    "HealEntry",
    "HealPlan",
    "HealReport",
    "SweepHealError",
    "assess_heal",
    "build_heal_plan",
    "execute_heal",
    "read_heal_plan",
    "render_heal_plan",
    "write_heal_plan",
]
