"""Twin-run sweep parity harness — arm-parameterized byte/content identity.

Slice-3 scaffolding (DEC-SWEEP-BELT-RUST-001, design-of-record §2.5 gate G2):
run the SAME sweep configuration once per **arm** (an env/config delta) into a
fresh temp root each, through the REAL production entries — the persisted-sweep
runner (:func:`brutex.engine.sweep_run.run_persisted_sweep`, the path the
frozen golden ``tests/golden/test_engine_sweep_run_golden.py`` drives) and the
supervised grid (:func:`brutex.engine.sweep_supervisor.run_supervised_grid`,
the sequential entry the seq≡par golden drives) — then compare every durable
artifact:

* results-store Parquet bytes (SHA-256 per file — the whole store tree, which
  includes the ``.trials.json`` trial-stats sidecar + the ``.complete.json``
  completion sentinel, both byte-deterministic by design),
* the audit chain(s), content-equal MODULO ``ts_utc`` (reduced to
  ``(seq, action, subject, payload)``) AND independently verified with the
  existing chain verifiers (:func:`brutex.orchestrator.audit.verify_chain` /
  :func:`brutex.engine.sweep_audit.verify_chain` — the P-20 oracle's verifier),
* the checkpoint file (parsed, modulo its ``updated_at`` wall-clock stamp),
* the final progress file (modulo its wall-clock-derived keys — a DENYLIST,
  like the checkpoint, so future deterministic keys are compared by default),
* the #1608 quarantine sidecar's ABSENCE,
* the :class:`~brutex.engine.sweep_run.SweepRunSummary` tallies.

Arms are first-class (:class:`TwinArm`): today's shipped gate runs IDENTICAL
baseline arms (OFF vs OFF — run-to-run determinism of the full artifact set);
the future belt-ON arm is a one-line addition::

    TwinArm(name="belt_on", env={"BRUTEX_SWEEP_RUST_BELT": "1"})

Deliberate scope notes (per the design-of-record):

* **No checkpoint/resume interrupt leg here** — design §2.5 G3 keeps the
  checkpoint/resume + chaos suites UNMODIFIED as the regression assertion
  (those modules have zero diff in the belt slice), so the twin harness does
  not duplicate them.
* **Sequential ≡ parallel is NOT re-pinned here** — it is already the headline
  golden ``tests/unit/test_engine_sweep_parallel.py::
  TestSequentialEqualsParallelGolden`` (design §2.5 G7 re-runs it with the
  flag exported into spawn workers); this harness references it instead of
  duplicating it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_audit import read_entries as read_supervisor_entries
from brutex.engine.sweep_audit import verify_chain as verify_supervisor_chain
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    count_sweep_tuples,
    make_store_for,
)
from brutex.engine.sweep_parallel import quarantine_sidecar_path
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    run_persisted_sweep,
)
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    publish_progress,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.audit import read_entries as read_ingest_entries
from brutex.orchestrator.audit import verify_chain as verify_ingest_chain

if TYPE_CHECKING:
    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.engine.sweep_progress import SweepProgressSnapshot

__all__ = [
    "CHECKPOINT_VOLATILE_KEYS",
    "DEFAULT_TWIN_DIGEST",
    "PROGRESS_VOLATILE_KEYS",
    "AuditLine",
    "GridArmArtifacts",
    "PersistedArmArtifacts",
    "TwinArm",
    "arm_env",
    "audit_line_divergences",
    "checkpoint_stable_view",
    "grid_twin_divergences",
    "identical_baseline_arms",
    "mapping_divergences",
    "persisted_twin_divergences",
    "progress_stable_view",
    "run_grid_arm",
    "run_persisted_arm",
    "tree_sha256",
]

#: One audit-chain line reduced to its timestamp-independent content
#: (``seq, action, subject, payload``) — the sweep_parallel golden's compare law.
AuditLine = tuple[int, str, str, dict[str, Any]]

#: A valid 16-hex tuple digest for persisted-level twin runs (enables the
#: trial-stats sidecar + completion-sentinel legs, DEC-MARK-MANY-SENTINEL-001).
DEFAULT_TWIN_DIGEST: Final[str] = "abcdef0123456789"

#: Progress-file keys EXCLUDED from cross-arm comparison — each is
#: wall-clock-derived by construction. A DENYLIST (mirroring
#: :data:`CHECKPOINT_VOLATILE_KEYS`, review M2) so any FUTURE deterministic
#: progress key is compared by default instead of silently dropped.
PROGRESS_VOLATILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "combos_per_sec",
        "elapsed_seconds",
        "eta_seconds",
        "started_at",
        "updated_at",
        # wall-clock stamp added by main's #1712 G4 liveness keys
        # (DEC-SWEEP-UNKILLABLE-CORE-001 — publish_progress always stamps
        # ``wall_time_utc = time.time()``) — wall-clock-derived by
        # construction, per this denylist's admission criterion. Its G4
        # siblings ``pid`` + ``running`` stay COMPARED: both are
        # deterministic in today's in-process twin (compare-by-default).
        "wall_time_utc",
        # the dashboard file-bridge decay stamp added by main post-#1733
        # (``sweep_supervisor.PROGRESS_WALL_TIME_KEY`` — publish_progress
        # always folds ``wall_time = time.time()`` so a reader can decay a
        # dead writer's frozen ``running=True`` claim) — wall-clock-derived
        # by construction, the same #1712 ``wall_time_utc`` class, per this
        # denylist's admission criterion.
        "wall_time",
    }
)

#: Checkpoint-file keys excluded from cross-arm comparison (wall-clock stamp).
CHECKPOINT_VOLATILE_KEYS: Final[frozenset[str]] = frozenset({"updated_at"})


@dataclass(frozen=True, slots=True)
class TwinArm:
    """One arm of a twin run: a name + an env delta + an optional pre-run hook.

    Parameters
    ----------
    name
        Human-readable arm label (used in divergence lines).
    env
        Env-var deltas applied for the arm's run only (``None`` value =
        ensure-unset); every touched var is restored afterwards.
    before_run
        Optional per-process reset hook run INSIDE the arm's env, before the
        sweep (the ``reset_self_check_for_tests`` pattern — a flagged kernel's
        burn-in/latch state must not leak between arms).
    """

    name: str
    env: Mapping[str, str | None] = field(default_factory=dict)
    before_run: Callable[[], None] | None = None


def identical_baseline_arms() -> tuple[TwinArm, TwinArm]:
    """Two baseline arms with NO env delta — today's shipped OFF-vs-OFF twin."""
    return (TwinArm(name="baseline_a"), TwinArm(name="baseline_b"))


@contextmanager
def arm_env(env: Mapping[str, str | None]) -> Generator[None, None, None]:
    """Apply an arm's env deltas for the duration of the block, then restore.

    A ``None`` value means "ensure the variable is UNSET". Restoration is
    exact: variables that pre-existed get their prior value back; variables
    the arm introduced are removed.
    """
    saved = {key: os.environ.get(key) for key in env}
    try:
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def tree_sha256(root: Path) -> dict[str, str]:
    """SHA-256 of every file under *root*, keyed by its relative POSIX path."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def progress_stable_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A progress-file payload with its wall-clock-derived keys removed.

    Denylist semantics (review M2): every key NOT named volatile — including
    any future deterministic key — is kept and therefore compared.
    """
    return {key: value for key, value in raw.items() if key not in PROGRESS_VOLATILE_KEYS}


def checkpoint_stable_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A checkpoint payload with its wall-clock keys removed."""
    return {key: value for key, value in raw.items() if key not in CHECKPOINT_VOLATILE_KEYS}


# ────────────────────────── divergence folds ──────────────────────────


def mapping_divergences(label: str, a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """One line per key whose presence or value differs between two mappings."""
    out: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key not in a:
            out.append(f"{label}[{key!r}]: only in second arm ({b[key]!r})")
        elif key not in b:
            out.append(f"{label}[{key!r}]: only in first arm ({a[key]!r})")
        elif a[key] != b[key]:
            out.append(f"{label}[{key!r}]: {a[key]!r} != {b[key]!r}")
    return out


def audit_line_divergences(label: str, a: Sequence[AuditLine], b: Sequence[AuditLine]) -> list[str]:
    """One line per position where two ts-masked audit chains differ."""
    out: list[str] = []
    if len(a) != len(b):
        out.append(f"{label}: length mismatch {len(a)} != {len(b)}")
    out.extend(
        f"{label}[{i}]: {line_a!r} != {line_b!r}"
        for i, (line_a, line_b) in enumerate(zip(a, b, strict=False))
        if line_a != line_b
    )
    return out


# ────────────────────────── persisted-sweep level ──────────────────────────


@dataclass(frozen=True, slots=True)
class PersistedArmArtifacts:
    """Everything one persisted-sweep arm produced, reduced for comparison.

    Attributes
    ----------
    arm
        The arm's name.
    summary
        ``dataclasses.asdict`` of the returned
        :class:`~brutex.engine.sweep_run.SweepRunSummary`.
    tree
        SHA-256 per relative path of the store directory — the Parquet store
        PLUS the ``.trials.json`` sidecar PLUS the ``.complete.json`` sentinel.
    audit_lines
        The per-combo (ingest-format) audit chain, ts-masked.
    audit_verified_entries
        The chain verifier's entry count (Golden Rule #8 — the chain links).
    """

    arm: str
    summary: dict[str, Any]
    tree: dict[str, str]
    audit_lines: tuple[AuditLine, ...]
    audit_verified_entries: int


def run_persisted_arm(
    root: Path,
    arm: TwinArm,
    *,
    bars: Sequence[CandleBar],
    cfg: SweepRunConfig,
    trial_stats_digest: str = DEFAULT_TWIN_DIGEST,
    reference_stamper: TradeReferenceStamper | None = None,
) -> PersistedArmArtifacts:
    """Run ONE persisted-sweep arm into a fresh *root*; capture its artifacts.

    Drives the REAL :func:`~brutex.engine.sweep_run.run_persisted_sweep` with
    a fresh store / dedup / audit writer per arm. The digest path is ON by
    default so the trial-stats sidecar + completion sentinel are genuinely
    written and compared (the frozen sweep-run golden does not exercise them).
    """
    store_dir = root / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    audit_path = root / "sweep.jsonl"
    with arm_env(arm.env):
        if arm.before_run is not None:
            arm.before_run()
        store = ResultsStore(path=store_dir / "tuple.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(audit_path) as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest=trial_stats_digest,
                reference_stamper=reference_stamper,
            )
    return PersistedArmArtifacts(
        arm=arm.name,
        summary=asdict(summary),
        tree=tree_sha256(store_dir),
        audit_lines=tuple(
            (e.seq, e.action, e.subject, e.payload) for e in read_ingest_entries(audit_path)
        ),
        audit_verified_entries=verify_ingest_chain(audit_path),
    )


def persisted_twin_divergences(a: PersistedArmArtifacts, b: PersistedArmArtifacts) -> list[str]:
    """Every content divergence between two persisted-sweep arms (``[]`` = twin)."""
    out: list[str] = []
    out.extend(mapping_divergences("summary", a.summary, b.summary))
    out.extend(mapping_divergences("store_tree", a.tree, b.tree))
    out.extend(audit_line_divergences("audit", a.audit_lines, b.audit_lines))
    if a.audit_verified_entries != b.audit_verified_entries:
        out.append(
            f"audit_verified_entries: {a.audit_verified_entries} != {b.audit_verified_entries}"
        )
    return out


# ────────────────────────── supervised-grid level ──────────────────────────


@dataclass(frozen=True, slots=True)
class GridArmArtifacts:
    """Everything one supervised-grid arm produced, reduced for comparison.

    Attributes
    ----------
    arm
        The arm's name.
    tree
        SHA-256 per relative path of the results root — every per-tuple
        Parquet store + ``.trials.json`` sidecar + ``.complete.json`` sentinel.
    checkpoint
        The parsed checkpoint file modulo :data:`CHECKPOINT_VOLATILE_KEYS`.
    checkpoint_next_index
        The in-memory high-water mark after the run.
    ingest_lines / ingest_verified_entries
        The per-combo SHA-256 chain, ts-masked + verifier count.
    supervisor_lines / supervisor_verified_entries
        The BLAKE3 supervisor chain, ts-masked + verifier count.
    progress_stable
        The final progress file modulo :data:`PROGRESS_VOLATILE_KEYS`.
    quarantine_sidecar_exists
        Whether the #1608 quarantine sidecar exists under the results root
        (a clean run must never produce one).
    """

    arm: str
    tree: dict[str, str]
    checkpoint: dict[str, Any]
    checkpoint_next_index: int
    ingest_lines: tuple[AuditLine, ...]
    ingest_verified_entries: int
    supervisor_lines: tuple[AuditLine, ...]
    supervisor_verified_entries: int
    progress_stable: dict[str, Any]
    quarantine_sidecar_exists: bool


def run_grid_arm(
    root: Path,
    arm: TwinArm,
    *,
    grid: SweepGridConfig,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    reference_stamper: TradeReferenceStamper | None = None,
) -> GridArmArtifacts:
    """Run ONE supervised sequential grid arm into a fresh *root*.

    Wires the five REAL guarantee-&-assurance objects (supervisor chain, cache,
    checkpoint, progress, watchdog) exactly as the seq≡par golden does, plus
    the real :func:`~brutex.engine.sweep_supervisor.publish_progress` file
    bridge, so every durable artifact class exists to be compared.
    """
    root.mkdir(parents=True, exist_ok=True)
    out = root / "out"
    progress_path = root / "progress.json"
    ckpt_path = root / "ckpt.json"
    with arm_env(arm.env):
        if arm.before_run is not None:
            arm.before_run()
        # ExitStack (review M3): a failed SweepCache.open cannot leak the
        # already-opened audit handle, and a close() raise cannot skip the
        # sibling close (callbacks run LIFO and ALL run even if one raises).
        with ExitStack() as stack:
            sup_audit = SweepAuditWriter.open(root / "sup.jsonl")
            stack.callback(sup_audit.close)
            cache = SweepCache.open(root / "cache", bloom_capacity=10_000)
            stack.callback(cache.close)
            guards = SweepGuards(
                audit=sup_audit,
                progress=SweepProgress(),
                checkpoint=SweepCheckpointStore(
                    path=ckpt_path,
                    sweep_id=grid_identity(grid),
                    total_combos=count_sweep_tuples(grid),
                ),
                cache=cache,
                watchdog=SweepWatchdog(),
            )

            def _publish(snapshot: SweepProgressSnapshot) -> None:
                publish_progress(progress_path, snapshot)

            with AuditWriter.open(root / "ingest.jsonl") as ingest:
                run_supervised_grid(
                    grid,
                    bars_for=bars_for,
                    store_for=make_store_for(out, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    ingest_audit=ingest,
                    guards=guards,
                    on_progress=_publish,
                    reference_stamper=reference_stamper,
                )
            next_index = guards.checkpoint.next_index
    raw_checkpoint: dict[str, Any] = json.loads(ckpt_path.read_text(encoding="utf-8"))
    raw_progress: dict[str, Any] = json.loads(progress_path.read_text(encoding="utf-8"))
    return GridArmArtifacts(
        arm=arm.name,
        tree=tree_sha256(out),
        checkpoint=checkpoint_stable_view(raw_checkpoint),
        checkpoint_next_index=next_index,
        ingest_lines=tuple(
            (e.seq, e.action, e.subject, e.payload)
            for e in read_ingest_entries(root / "ingest.jsonl")
        ),
        ingest_verified_entries=verify_ingest_chain(root / "ingest.jsonl"),
        supervisor_lines=tuple(
            (e.seq, e.action, e.subject, e.payload)
            for e in read_supervisor_entries(root / "sup.jsonl")
        ),
        supervisor_verified_entries=verify_supervisor_chain(root / "sup.jsonl"),
        progress_stable=progress_stable_view(raw_progress),
        quarantine_sidecar_exists=quarantine_sidecar_path(out).exists(),
    )


def grid_twin_divergences(a: GridArmArtifacts, b: GridArmArtifacts) -> list[str]:
    """Every content divergence between two supervised-grid arms (``[]`` = twin)."""
    out: list[str] = []
    out.extend(mapping_divergences("results_tree", a.tree, b.tree))
    out.extend(mapping_divergences("checkpoint", a.checkpoint, b.checkpoint))
    if a.checkpoint_next_index != b.checkpoint_next_index:
        out.append(f"checkpoint_next_index: {a.checkpoint_next_index} != {b.checkpoint_next_index}")
    out.extend(audit_line_divergences("ingest_audit", a.ingest_lines, b.ingest_lines))
    if a.ingest_verified_entries != b.ingest_verified_entries:
        out.append(
            f"ingest_verified_entries: {a.ingest_verified_entries} != {b.ingest_verified_entries}"
        )
    out.extend(audit_line_divergences("supervisor_audit", a.supervisor_lines, b.supervisor_lines))
    if a.supervisor_verified_entries != b.supervisor_verified_entries:
        out.append(
            "supervisor_verified_entries: "
            f"{a.supervisor_verified_entries} != {b.supervisor_verified_entries}"
        )
    out.extend(mapping_divergences("progress", a.progress_stable, b.progress_stable))
    if a.quarantine_sidecar_exists != b.quarantine_sidecar_exists:
        out.append(
            "quarantine_sidecar_exists: "
            f"{a.quarantine_sidecar_exists} != {b.quarantine_sidecar_exists}"
        )
    return out
