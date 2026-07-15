"""The ONE composite run-core seam ``cmd_sweep_all`` wraps its grid run in.

DEC-SWEEP-UNKILLABLE-CORE-001 (WP-5, design §2/§4): a single context manager
(:func:`sweep_run_core`) composes every Phase-2 run-lifecycle + observability
primitive around the UNTOUCHED grid body:

* **G6 single-flight** — :func:`~brutex.engine.sweep_singleflight.sweep_run_lock`
  is entered BEFORE any chain writer opens (two processes can never interleave
  appends and fork a chain).
* **G9 keep-awake** — :func:`~brutex.observability.power_keepawake.keep_awake`
  wraps the run (CLI, daemon cycles, and the detached child all enter here).
* **G12 checkpoint resilience** — the checkpoint is pre-loaded through
  :func:`~brutex.engine.sweep_quarantine.load_checkpoint_resilient` at enter, so
  a corrupt file is quarantined (fresh start) before the supervisor's own
  ``load()`` ever sees it.
* **R41 flight recorder / R39 tripwires / R28 manifest / D-18 chain map /
  R40 bundle / G5 exit status** — all fail-soft (I-3): an observability failure
  is WARNed and NEVER changes results or kills the run.
* **R45 diffcheck** — a budgeted per-tuple closure over
  :class:`~brutex.engine.sweep_diffcheck.DiffCheckHook` with the production
  legs injected; a mismatch latches SUSPECT and NEVER alters a result byte
  (never-adopt law, I-4).
* **G1 quarantine-retry** — :attr:`RunCoreHandles.store_quarantine` composes
  :func:`~brutex.engine.sweep_quarantine.quarantine_tuple_trio` for
  :func:`~brutex.engine.sweep_orchestrator.run_one_tuple`.
* **G15 / D-14 beat** — the composed ``on_progress`` emits
  :func:`~brutex.observability.progress_beat.emit_progress_beat` (the channel
  ``pull_jobs`` actually consumes; the CLI path has no registered beat = no-op).

Byte-identity law (I-1): every artifact this module writes lives under
``<results_root>/runs/<run_id>/`` (plus the progress file's ADDITIVE keys and
the ONE additive ``RUN_MANIFEST`` action on the CLI-level SHA-256 chain, D-3).
No enumeration order, checkpoint index, dedup decision, store byte, or rank
order is touched — proven by the run-core ON-vs-OFF byte-identity integration
test.

Honest envelope (Golden Rule #10): the stage-2 aggregate stamp is one
O(tuples) sentinel read at the sweep→stage3 boundary; the diagnostic bundle is
O(artifacts); diffcheck costs O(K sampled masks) per run — none of these is
claimed O(1).

Parallel-mode parity (MAJOR round-2 fixes, DEC-SWEEP-UNKILLABLE-CORE-001):
the DEFAULT multi-core sweep (``run_parallel_grid``, ``workers > 1``) now runs
the ``on_tuple`` fan-out (TUPLE_DONE flight records + the R39 NaN/absurd
per-tuple SUSPECT tripwire) from the single-threaded writer-actor apply path —
the applied summary is parent-side, so no closure crosses the spawn boundary —
and G1 store-quarantine runs IN THE WORKER (a corrupt on-disk store is
quarantined + retried in-process so it can NEVER abort the whole grid; its
QUARANTINE events replay into the flight recorder via
:meth:`RunCoreHandles.record_worker_quarantine`). The ONE parallel-mode gap
that remains is R45 diffcheck: its stateful K budget + its ``on_mismatch``
fan-out into the run-core SUSPECT latch are not carried across the spawn
boundary, so diffcheck stays a sequential-spine sampling ASSURANCE (K=200 ≈
0.004% of a large grid) — a named, documented gap (follow-up
DEC-SWEEP-DIFFCHECK-PARALLEL-001), NOT a silent one; the audit-replay
determinism lane (``audit_verify.auto_verify``, exercised by the rehearsal
gate) still covers parallel runs, and the two genuine default-mode gaps (the
NaN tripwire + the grid-abort-on-corrupt-store) are now CLOSED.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.errors import LakeWriteHalt
from brutex.engine.run_layout import mirror_run_dir, run_dir
from brutex.engine.run_verdicts import VERDICTS_FILENAME, build_run_verdicts
from brutex.engine.sweep_diffcheck import (
    DiffCheckHook,
    DiffCheckMismatch,
    resolve_diffcheck_k,
)
from brutex.engine.sweep_exit_status import (
    EXIT_STATUS_FILENAME,
    RunState,
    read_exit_status,
    write_exit_status_soft,
)
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    _tuple_digest,
    count_sweep_tuples,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_quarantine import (
    CheckpointCorruptionTracker,
    load_checkpoint_resilient,
    quarantine_tuple_trio,
    reset_checkpoint_corruption,
)
from brutex.engine.sweep_singleflight import sweep_run_lock
from brutex.engine.tuple_complete import (
    TupleCompletionError,
    completion_sentinel_path,
    read_completion_sentinel,
)
from brutex.models import BruteXError
from brutex.observability.chain_map import CHAIN_MAP_FILENAME, build_chain_map, write_chain_map
from brutex.observability.correlation import run_id_scope
from brutex.observability.diag_bundle import BUNDLE_FILENAME, build_diagnostic_bundle
from brutex.observability.flight_recorder import (
    FLIGHT_RECORDER_FILENAME,
    FlightRecorder,
    recorder_has_suspect,
)
from brutex.observability.power_keepawake import keep_awake
from brutex.observability.progress_beat import emit_progress_beat
from brutex.observability.run_manifest import MANIFEST_FILENAME, ManifestAccumulator
from brutex.observability.run_tripwires import (
    TripwireConfig,
    TripwireEvent,
    TripwireMonitor,
)
from brutex.observability.stage_stamp import (
    StampVerdict,
    content_hash_of,
    discard_and_flag,
    stamp_stage3_cells,
    validate_stage3_cells,
    validate_stage_stamp,
    write_stage_stamp,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from pathlib import Path

    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.engine.sweep_checkpoint import SweepCheckpoint, SweepCheckpointStore
    from brutex.engine.sweep_orchestrator import StoreQuarantine, SweepTuple
    from brutex.engine.sweep_progress import SweepProgressSnapshot
    from brutex.engine.sweep_run import SweepRunSummary
    from brutex.notifications.sweep_alerts import SweepAlerter

_LOG = structlog.get_logger("brutex.engine.sweep_run_core")

#: The R40 forensic artifacts mirrored to the INTERNAL disk (Row 15). Names are
#: imported from their owning modules (§11 COMMON — never re-minted here); a
#: name absent on disk for a given run is simply skipped.
_MIRRORED_FORENSIC_NAMES: Final[tuple[str, ...]] = (
    BUNDLE_FILENAME,
    FLIGHT_RECORDER_FILENAME,
    MANIFEST_FILENAME,
    EXIT_STATUS_FILENAME,
    CHAIN_MAP_FILENAME,
    VERDICTS_FILENAME,
)

#: TUPLE_DONE decimation denominator (design D-9): the recorder stride is
#: ``ceil(total / DECIMATION_TARGET_RECORDS)`` so even a 100k-tuple grid
#: records at most ~10k TUPLE_DONE lines (cumulative counters keep it lossless).
DECIMATION_TARGET_RECORDS: Final[int] = 10_000

#: Telegram / recorder milestone edges (R41).
MILESTONE_PCTS: Final[tuple[int, ...]] = (25, 50, 75)

#: The run-core stage vocabulary — chain-map LINK names (D-18); the manifest
#: ``per_stage_timings`` keys are these same names (``launch``/``preload``
#: happen BEFORE the core is entered and are honestly absent, never fabricated).
CORE_STAGES: Final[tuple[str, ...]] = ("preflight", "sweep", "stage3", "cascade", "artifacts")

#: ``FilterStage.value`` → manifest door name (D-8: only honestly sourceable
#: doors are mapped; every other cascade stage stays ABSENT, never fabricated).
CASCADE_DOOR_BY_STAGE: Final[Mapping[str, str]] = {
    "deflated_sharpe": "dsr",
    "reality_check_spa": "spa",
    "cpcv": "cpcv",
    "walk_forward": "walk_forward",
}


class Stage3CellStampError(BruteXError):
    """A stage-3 cell failed stamp validation even after ONE discard + re-run (D-4)."""


def tuple_record_stride(total_tuples: int) -> int:
    """Return the D-9 TUPLE_DONE decimation stride for a *total_tuples* grid.

    ``ceil(total / 10_000)`` with a floor of 1 — a small grid records every
    tuple; a 100k-tuple grid records every 10th.

    Parameters
    ----------
    total_tuples
        The grid's enumerated tuple count (negative clamps to the floor).

    Returns
    -------
    int
        The stride (``>= 1``).
    """
    return max(1, math.ceil(max(0, total_tuples) / DECIMATION_TARGET_RECORDS))


def per_tuple_diffcheck_budget(k_per_run: int, total_tuples: int) -> int:
    """Distribute the run-level R45 K budget across tuples (design D-11).

    ``max(1, k // total)`` when the budget is positive — every tuple gets at
    least one sampled mask until the run budget is exhausted; ``0`` when the
    diffcheck is disabled.

    Parameters
    ----------
    k_per_run
        The resolved run-level budget (``0`` = disabled).
    total_tuples
        The grid's tuple count (a non-positive total degrades to 1).

    Returns
    -------
    int
        The per-tuple sample allowance.
    """
    if k_per_run <= 0:
        return 0
    return max(1, k_per_run // max(1, total_tuples))


def _conditions_hex(condition_bits: Sequence[int]) -> str:
    """Fold the grid's condition-bit list into one lowercase hex mask string."""
    mask = 0
    for bit in condition_bits:
        mask |= 1 << bit
    return f"{mask:x}"


def _selection_fragment(
    grid: SweepGridConfig, extra: Mapping[str, object] | None
) -> dict[str, object]:
    """Build the manifest ``selection`` fragment (design §3) from the grid."""
    selection: dict[str, object] = {
        "symbols": list(grid.instruments),
        "intervals": [tf.label for tf in grid.timeframes],
        "directions": [d.value for d in grid.directions],
        "k": list(grid.k_values),
        "sl": list(grid.stop_losses),
        "tp": list(grid.take_profits),
        "conditions_hex": _conditions_hex(grid.condition_bits),
        "mode": grid.mode,
    }
    if extra is not None:
        selection.update(extra)
    return selection


@dataclass(slots=True)
class RunCoreHandles:
    """The live handles :func:`sweep_run_core` yields to the CLI shim.

    Mutable BY DESIGN (the design §2 sketch's frozen dataclass could not carry
    the exit code / stage cursor the wiring needs — a flagged, documented
    deviation): the CLI calls :meth:`stage` / :meth:`grid_done` /
    :meth:`run_stage3` / :meth:`cascade_done` / :meth:`set_exit_code` as flat
    statements so the existing block indentation never moves.

    Attributes
    ----------
    run_id
        ``grid_identity(grid)`` (D-1 — no timestamp, resume lands in the same
        ``runs/<run_id>/``).
    attempt
        Observability-only monotone attempt ordinal (never in any identity).
    recorder, tripwires, manifest
        The composed observability primitives (each individually fail-soft).
    on_progress
        The composed progress callback (publish + tripwires + milestones +
        the G15 beat) — pass as BOTH drivers' ``on_progress``.
    on_tuple
        The composed per-tuple callback (caller's inner hook + recorder
        TUPLE_DONE + tripwire NaN screen) — pass as the sequential driver's
        ``on_tuple``.
    diffcheck
        The budgeted R45 closure for ``run_one_tuple(diffcheck=…)``, or
        ``None`` when disabled (``BRUTEX_SWEEP_DIFFCHECK`` off / budget 0).
    store_quarantine
        The G1 composition for ``run_one_tuple(store_quarantine=…)``.
    """

    run_id: str
    attempt: int
    recorder: FlightRecorder
    tripwires: TripwireMonitor
    manifest: ManifestAccumulator
    on_progress: Callable[[SweepProgressSnapshot], None]
    on_tuple: Callable[[SweepTuple, SweepRunSummary], None]
    diffcheck: Callable[..., None] | None
    store_quarantine: StoreQuarantine
    _results_root: Path
    _grid: SweepGridConfig
    _alerter: SweepAlerter
    _progress_path: Path | None
    _wall: Callable[[], float]
    _monotonic: Callable[[], float]
    _stage: str = "preflight"
    _exit_code: int | None = None
    _suspect: bool = False
    _suspect_fanned: bool = False
    _tuples_done: int = 0
    _sentinel_skips: int = 0
    _minhits_dropped: int = 0
    _last_snapshot: dict[str, Any] = field(default_factory=dict)
    _milestones_fired: set[int] = field(default_factory=set)
    _stamps_present: set[str] = field(default_factory=set)
    _link_stamps: dict[str, StampVerdict] = field(default_factory=dict)
    _observability_warned: bool = False
    #: The run's India-VIX stamper for the R45 production leg — late-bound by
    #: the CLI via :meth:`bind_reference_stamper` (the stamper is built AFTER
    #: this CM enters); ``None`` = the honest unstamped default.
    _reference_stamper: TradeReferenceStamper | None = None

    # ── fail-soft plumbing (I-3) ──────────────────────────────────────────

    def _guarded(self, step: str, fn: Callable[[], object]) -> object | None:
        """Run one observability step; a failure is ONE WARN, never a raise."""
        try:
            return fn()
        except Exception as exc:  # fail-soft law (I-3)
            with contextlib.suppress(Exception):
                _LOG.warning(
                    "sweep_run_core.observability_failed",
                    step=step,
                    run_id=self.run_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            return None

    @property
    def suspect(self) -> bool:
        """The run-level SUSPECT latch (tripwires OR diffcheck; set-once, D-12)."""
        return self._suspect or self.tripwires.suspect

    @property
    def exit_code(self) -> int | None:
        """The exit code the CLI reported via :meth:`set_exit_code` (or ``None``)."""
        return self._exit_code

    def set_exit_code(self, code: int) -> None:
        """Record the CLI's computed exit code for the finalize pass."""
        self._exit_code = code

    # ── SUSPECT fan-out (D-12) ────────────────────────────────────────────

    def mark_suspect(self, reason: str, detail: str) -> None:
        """Latch the run SUSPECT and fan out ONCE (recorder + alert + manifest)."""
        self._suspect = True
        self._guarded("manifest.mark_suspect", lambda: self.manifest.mark_suspect(reason))
        if self._suspect_fanned:
            return
        self._suspect_fanned = True
        # D-12 never-launder (MAJOR round-2 fix): stamp the durable exit-status
        # ``suspect=True`` FIRST — before the recorder append, the structlog
        # ERROR, and the potentially-blocking Telegram publish — so a SIGKILL
        # (dashboard restart, the exact "unkillable" premise) between the
        # in-memory latch and this write cannot leave a resume reading
        # ``suspect=false``. The kill-window before the durable signal now
        # spans only the in-memory latch + one atomic write, no network hop.
        # Resume ALSO folds the durable flight-recorder SUSPECT record (see
        # ``recorder_has_suspect`` in the CM), so even a torn exit-status write
        # cannot launder a durably-recorded SUSPECT back to a clean COMPLETE.
        self._guarded(
            "exit_status.suspect",
            lambda: write_exit_status_soft(
                self._results_root,
                run_id=self.run_id,
                state=RunState.RUNNING,
                attempt=self.attempt,
                pid=os.getpid(),
                suspect=True,
            ),
        )
        self._guarded(
            "recorder.suspect",
            lambda: self.recorder.record("SUSPECT", {"reason": reason, "detail": detail}),
        )
        with contextlib.suppress(Exception):
            _LOG.error(
                "sweep_run_core.run_suspect", run_id=self.run_id, reason=reason, detail=detail
            )
        self._guarded(
            "alerter.run_suspect",
            lambda: self._alerter.run_suspect(run_id=self.run_id, tripwire=reason, detail=detail),
        )

    def _on_trip(self, event: TripwireEvent) -> None:
        """The R39 tripwire fan-out (recorder record + manifest row + SUSPECT)."""
        self._guarded(
            "recorder.tripwire",
            lambda: self.recorder.record(
                "TRIPWIRE",
                {
                    "name": event.name,
                    "severity": event.severity,
                    "detail": event.detail,
                    "values": dict(event.values),
                },
            ),
        )
        self._guarded(
            "manifest.add_tripwire",
            lambda: self.manifest.add_tripwire(
                {
                    "ts_utc": event.ts_utc,
                    "name": event.name,
                    "severity": event.severity,
                    "detail": event.detail,
                }
            ),
        )
        self.mark_suspect(event.name, event.detail)

    def _on_mismatch(self, mismatch: DiffCheckMismatch) -> None:
        """The R45 mismatch fan-out — record + SUSPECT; NEVER touches the result (I-4)."""
        self._guarded(
            "recorder.diffcheck_mismatch",
            lambda: self.recorder.record("DIFFCHECK_MISMATCH", mismatch.payload()),
        )
        self.mark_suspect("diffcheck_mismatch", f"mask {mismatch.mask_hex}: {mismatch.field}")

    # ── stage boundaries + chain map (D-18) ───────────────────────────────

    def stage(self, name: str) -> None:
        """Close the previous stage and enter *name* (flat — no re-indent needed).

        Fires the manifest timing boundary, the recorder STAGE_START/END pair,
        the R39 stage-invariant check, and a chain-map snapshot — all fail-soft.

        Parameters
        ----------
        name
            One of :data:`CORE_STAGES` (an unknown name still times honestly;
            the tripwire tolerates unknown stages by design).
        """
        previous = self._stage
        self._guarded("manifest.stage_ended", lambda: self.manifest.stage_ended(previous))
        self._guarded(
            "recorder.stage_end",
            lambda: self.recorder.record("STAGE_END", {"stage": previous}),
        )
        self._stage = name
        self._guarded("manifest.stage_started", lambda: self.manifest.stage_started(name))
        self._guarded(
            "recorder.stage_start",
            lambda: self.recorder.record("STAGE_START", {"stage": name}),
        )
        self._guarded(
            "tripwires.on_stage",
            lambda: self.tripwires.on_stage(name, stamps_present=frozenset(self._stamps_present)),
        )
        self._write_chain_map()

    def _write_chain_map(self) -> None:
        """Snapshot the D-18 chain map (fail-soft, tiny atomic JSON)."""
        self._guarded(
            "chain_map.write",
            lambda: write_chain_map(
                self._results_root,
                self.run_id,
                build_chain_map(
                    progress=self._progress_payload_view(),
                    stamps=dict(self._link_stamps),
                    suspect=self.suspect,
                    exit_status=read_exit_status(self._results_root, self.run_id),
                ),
            ),
        )

    def _progress_payload_view(self) -> dict[str, Any] | None:
        """The freshest progress payload (additive keys included) for the chain map."""
        if not self._last_snapshot:
            return None
        view = dict(self._last_snapshot)
        view["run_id"] = self.run_id
        view["stage"] = self._stage
        return view

    # ── grid / stage3 / cascade folds ─────────────────────────────────────

    def grid_done(self, result: Any) -> None:
        """Fold the grid summary into the manifest + write the stage-2 aggregate stamp.

        Parameters
        ----------
        result
            The :class:`~brutex.engine.sweep_supervisor.SupervisedGridSummary`
            either driver returned.
        """
        self._guarded(
            "manifest.set_actual",
            lambda: self.manifest.set_actual(
                {
                    "trials": int(result.evaluated),
                    "enumerated": int(result.enumerated),
                    "tuples_done": int(result.tuples_run),
                    "tuples_failed": int(result.tuples_failed),
                    "tuples_skipped_cache": int(result.skipped_cache),
                    "tuples_skipped_resume": int(result.skipped_resume),
                }
            ),
        )

        def _fold_dedup() -> None:
            self.manifest.add_dedup_skip("cache", int(result.skipped_cache))
            self.manifest.add_dedup_skip("resume", int(result.skipped_resume))
            self.manifest.add_dedup_skip("sentinel", self._sentinel_skips)

        self._guarded("manifest.dedup", _fold_dedup)

        def _fold_doors() -> None:
            self.manifest.add_door_kills("no_bars", int(result.tuples_skipped_no_bars))
            self.manifest.add_door_kills("cache_skip", int(result.skipped_cache))
            self.manifest.add_door_kills("resume_skip", int(result.skipped_resume))
            self.manifest.add_door_kills("min_hits", self._minhits_dropped)

        self._guarded("manifest.doors", _fold_doors)
        # I-3 fail-soft: the R39 stage-invariant prerequisite is the CORRECTNESS
        # milestone (the grid ran + its per-tuple completion sentinels exist),
        # NOT the durability of the forensic ``stage2`` aggregate sidecar. A
        # transient ENOSPC/EIO on that observability write must never flip the
        # next ``stage("stage3")`` to a false STAGE_INVARIANT → SUSPECT, so the
        # prerequisite is recorded HERE, decoupled from the guarded write below.
        self._stamps_present.add("stage2")
        self._guarded("stage2.aggregate_stamp", self._write_stage2_aggregate)

    def _write_stage2_aggregate(self) -> None:
        """Write ``runs/<run_id>/stage2`` (sorted sentinel pairs) + its stamp (§3).

        Fail-soft (I-3): a failure here is WARNed by the ``_guarded`` caller and
        never changes the run outcome — the ``stage2`` correctness prerequisite
        was already recorded in :meth:`grid_done`; only the ``sweep`` chain-map
        LINK evidence (a durable-stamp signal) is withheld when the write fails.
        """
        pairs: list[list[str]] = []
        for tuple_ in iter_sweep_tuples(self._grid):
            sentinel = completion_sentinel_path(
                tuple_store_path(self._results_root, tuple_, self._grid)
            )
            try:
                record = read_completion_sentinel(sentinel)
            except TupleCompletionError:
                continue  # no-bars / failed / unfinished tuple — honestly absent
            pairs.append([record.tuple_digest, record.store_content_hash])
        pairs.sort()
        dest = run_dir(self._results_root, self.run_id) / "stage2"
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"pairs": pairs}, sort_keys=True).encode("utf-8")
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))
        write_stage_stamp(dest, stage="stage2", run_id=self.run_id)
        # Durable stamp landed → the chain-map SWEEP link is OK. The tripwire
        # prerequisite ``stage2`` was already recorded in grid_done (correctness
        # path), so it stands even when this forensic write fails.
        self._link_stamps["sweep"] = StampVerdict.OK

    def run_stage3(self, factory: Callable[[], Any]) -> Any:
        """Run the stage-3 pass with the D-4 cell stamp/validate/discard loop.

        Runs *factory* once, stamps every refined cell (run-id + train-snapshot
        bound), validates them, and on ANY invalid cell discards the offenders
        and re-runs the (idempotent) pass EXACTLY once — a second validation
        failure is a hard :class:`Stage3CellStampError`. A disabled pass (no
        cells / no train hash) passes through untouched; the ``stage3``
        prerequisite is then vacuously satisfied for the R39 stage invariant.

        Parameters
        ----------
        factory
            Zero-arg callable running the REAL ``run_stage3_pass`` (the CLI
            binds the grid/audit/bars arguments in a lambda).

        Returns
        -------
        Any
            The (possibly re-run) ``Stage3PassReport``.

        Raises
        ------
        Stage3CellStampError
            When a cell is still invalid after the single discard + re-run.
        """
        report = factory()
        cells = [cell.path for cell in report.cells]
        snapshot = report.train_snapshot_hash
        if not cells or snapshot is None:
            self._stamps_present.add("stage3")  # vacuous prerequisite (disabled pass)
            self._link_stamps.pop("stage3", None)
            return report
        # The stage-3 CORRECTNESS pass ran → the R39 prerequisite is met
        # regardless of whether the forensic cell stamps durably write. A stamp
        # WRITE failure (I-3: a transient ENOSPC/permission hiccup on the
        # ``.stamp.json`` sidecar) must NEVER kill an otherwise-successful
        # sweep — it is WARNed and the cells are left UNSTAMPED so the cascade
        # replays them on the documented validate→MISSING_STAMP legacy
        # back-compat rebuild path. The hard :class:`Stage3CellStampError` is
        # reserved STRICTLY for a genuine post-rerun content-hash MISMATCH.
        self._stamps_present.add("stage3")
        if self._guarded("stage3.stamp_cells", lambda: self._stamp_cells(cells, snapshot)) is None:
            self._link_stamps.pop("stage3", None)  # write failed → fail-soft, unstamped
            return report
        # I-3 fail-soft symmetry (MINOR round-2 fix): the cell RE-HASH inside
        # ``validate_stage3_cells`` re-reads each ``.parquet`` cell + its stamp
        # sidecar off the results SSD, so a transient EIO on the documented §8
        # external SSD raises ``StageStampError`` — which must NOT convert an
        # otherwise-successful sweep into FAILED any more than the guarded stamp
        # WRITE above. Guard the re-hash: a raise ⇒ ``None`` ⇒ fail-soft
        # UNSTAMPED (the cascade replays via the legacy MISSING_STAMP rebuild),
        # the hard :class:`Stage3CellStampError` reserved STRICTLY for a
        # genuine returned HASH_MISMATCH after the discard + re-run.
        first = self._guarded(
            "stage3.validate",
            lambda: validate_stage3_cells(cells, run_id=self.run_id, snapshot_hash=snapshot),
        )
        if first is None:  # re-hash I/O glitch → fail-soft, unstamped
            self._link_stamps.pop("stage3", None)
            return report
        invalid = cast("tuple[Path, ...]", first)
        if invalid:
            if self._guarded("stage3.discard", lambda: self._discard_cells(invalid)) is None:
                self._link_stamps.pop("stage3", None)  # discard write failed → fail-soft
                return report
            report = factory()
            cells = [cell.path for cell in report.cells]
            snapshot = report.train_snapshot_hash
            if cells and snapshot is not None:
                if (
                    self._guarded(
                        "stage3.restamp_cells", lambda: self._stamp_cells(cells, snapshot)
                    )
                    is None
                ):
                    self._link_stamps.pop("stage3", None)  # re-stamp write failed → fail-soft
                    return report
                second = self._guarded(
                    "stage3.validate",
                    lambda: validate_stage3_cells(
                        cells, run_id=self.run_id, snapshot_hash=snapshot
                    ),
                )
                if second is None:  # re-hash I/O glitch on the re-run → fail-soft, unstamped
                    self._link_stamps.pop("stage3", None)
                    return report
                if cast("tuple[Path, ...]", second):  # genuine HASH_MISMATCH after re-run
                    raise Stage3CellStampError(
                        f"stage-3 cell stamps still invalid after one discard + re-run: "
                        f"{[str(p) for p in cast('tuple[Path, ...]', second)]}"
                    )
        self._link_stamps["stage3"] = StampVerdict.OK
        return report

    def _stamp_cells(self, cells: Sequence[Path], snapshot: str) -> int:
        """Stamp the stage-3 cells (raises ``StageStampError`` on any write failure)."""
        return stamp_stage3_cells(cells, run_id=self.run_id, snapshot_hash=snapshot)

    def _discard_cells(self, invalid: Sequence[Path]) -> int:
        """Discard+flag each invalid stage-3 cell (raises on a rename failure)."""
        for cell in invalid:
            discard_and_flag(cell, reason="stage3_cell_stamp_invalid", recorder=self.recorder)
        return len(invalid)

    def cascade_done(self, report: Any) -> None:
        """Fold the cascade report's sourceable door kills + stamp verdicts (D-8/G7).

        Parameters
        ----------
        report
            The ``CascadeWireReport`` ``run_and_write_from_specs`` returned.
        """

        def _fold() -> None:
            kills: dict[str, int] = {}
            for _instrument, _tf, group in report.groups:
                for verdict in group.verdicts:
                    stage_enum = verdict.rejected_at
                    if stage_enum is None:
                        continue
                    door = CASCADE_DOOR_BY_STAGE.get(stage_enum.value)
                    if door is None:
                        continue  # not honestly sourceable — ABSENT, never fabricated
                    kills[door] = kills.get(door, 0) + 1
            for door, count in sorted(kills.items()):
                self.manifest.add_door_kills(door, count)

        # I-3 fail-soft: the CASCADE correctness pass ran, so the ``artifacts``
        # stage's prerequisites are satisfied HERE (decoupled from whether the
        # forensic cascade stamps durably wrote/validated — that only feeds the
        # chain-map LINK evidence in ``_fold_cascade_stamps``). Without this a
        # transient stamp-write hiccup would false-trip STAGE_INVARIANT →
        # SUSPECT on ``stage("artifacts")``.
        self._stamps_present.add("cascade_manifest")
        self._stamps_present.add("cascade_report")
        self._guarded("manifest.cascade_doors", _fold)
        self._guarded("cascade.stamp_verdicts", self._fold_cascade_stamps)
        self._write_chain_map()

    def mark_cascade_absent(self) -> None:
        """Vacuously satisfy the ``artifacts`` prerequisite when NO cascade ran.

        A legitimate degenerate-but-clean run (an all-no-bars grid, or every
        combo below ``min_hits``) yields ZERO cascade specs, so the CLI never
        calls :meth:`cascade_done` and no cascade stamp is ever written. The
        R39 stage invariant would then false-trip on ``stage("artifacts")`` and
        flag the whole clean run SUSPECT. Mirroring :meth:`run_stage3`'s
        disabled-pass treatment, mark the two cascade prerequisites vacuously
        present (no LINK stamp is asserted — there is genuinely no cascade
        artifact to evidence).
        """
        self._stamps_present.add("cascade_manifest")
        self._stamps_present.add("cascade_report")
        self._link_stamps.pop("cascade", None)

    def _fold_cascade_stamps(self) -> None:
        """Validate the just-written cascade stamps into the chain-map LINK evidence.

        Durable-write evidence ONLY (I-3): the ``cascade_manifest`` /
        ``cascade_report`` tripwire prerequisites were already recorded on the
        correctness path in :meth:`cascade_done`; this method records solely the
        chain-map ``cascade`` LINK verdict, so a failed/absent stamp withholds
        the LINK evidence without changing the run outcome.
        """
        from brutex.engine.cascade_wire import manifest_path as cascade_manifest_path
        from brutex.engine.cascade_wire import report_path as cascade_report_path

        manifest_verdict, _ = validate_stage_stamp(
            cascade_manifest_path(self._results_root),
            expected_stage="cascade_manifest",
            expected_run_id=self.run_id,
        )
        report_verdict, _ = validate_stage_stamp(
            cascade_report_path(self._results_root),
            expected_stage="cascade_report",
            expected_run_id=self.run_id,
        )
        worst = manifest_verdict if manifest_verdict is not StampVerdict.OK else report_verdict
        self._link_stamps["cascade"] = worst

    # ── composed callbacks ────────────────────────────────────────────────

    def _compose_on_progress(
        self, inner: Callable[[SweepProgressSnapshot], None] | None
    ) -> Callable[[SweepProgressSnapshot], None]:
        """Build the composed ``on_progress`` (publish + tripwires + milestones + beat)."""

        def _on_progress(snap: SweepProgressSnapshot) -> None:
            try:
                self._last_snapshot = snap.to_dict()
                if self._progress_path is not None:
                    from brutex.engine.sweep_supervisor import publish_progress

                    publish_progress(
                        self._progress_path,
                        snap,
                        run_id=self.run_id,
                        suspect=self.suspect,
                        stage=self._stage,
                    )
                if inner is not None:
                    inner(snap)
                self.tripwires.on_progress(snap)
                self._fire_milestones(snap)
                emit_progress_beat()
            except Exception as exc:  # fail-soft law (I-3)
                if not self._observability_warned:
                    self._observability_warned = True
                    with contextlib.suppress(Exception):
                        _LOG.warning(
                            "sweep_run_core.on_progress_failed",
                            run_id=self.run_id,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )

        return _on_progress

    def _fire_milestones(self, snap: SweepProgressSnapshot) -> None:
        """Edge-fire the 25/50/75 milestone record + Telegram push (R41)."""
        pct = snap.percent
        for edge in MILESTONE_PCTS:
            if pct >= edge and edge not in self._milestones_fired:
                self._milestones_fired.add(edge)
                self.recorder.milestone_pct(
                    edge,
                    {
                        "tuples_done": snap.evaluated,
                        "total": snap.enumerated,
                        "rate_per_s": snap.combos_per_sec,
                    },
                )
                self._alerter.sweep_milestone(
                    run_id=self.run_id,
                    pct=edge,
                    tuples_done=snap.evaluated,
                    total=snap.enumerated,
                )

    def _compose_on_tuple(
        self, inner: Callable[[SweepTuple, SweepRunSummary], None] | None
    ) -> Callable[[SweepTuple, SweepRunSummary], None]:
        """Build the composed ``on_tuple`` (inner hook first, then observability)."""

        def _on_tuple(tuple_: SweepTuple, summary: SweepRunSummary) -> None:
            if inner is not None:
                inner(tuple_, summary)  # the caller's own hook keeps its raw contract
            self._guarded("on_tuple", lambda: self._record_tuple(tuple_, summary))

        return _on_tuple

    def _record_tuple(self, tuple_: SweepTuple, summary: SweepRunSummary) -> None:
        """Record ONE run tuple: TUPLE_DONE (decimated, cumulative) + NaN screen."""
        self._tuples_done += 1
        if summary.resumed_complete:
            self._sentinel_skips += 1
        self._minhits_dropped += int(summary.minhits_dropped)
        skipped_cum = int(self._last_snapshot.get("skipped", 0) or 0)
        evaluated_cum = int(self._last_snapshot.get("evaluated", 0) or 0)
        failed_cum = max(0, evaluated_cum - skipped_cum - self._tuples_done)
        self.recorder.record(
            "TUPLE_DONE",
            {
                "digest": _tuple_digest(tuple_, self._grid),
                "evaluated": summary.evaluated,
                "kept": summary.persisted,
                "done_cum": self._tuples_done,
                "skipped_cum": skipped_cum,
                "failed_cum": failed_cum,
            },
        )
        self.tripwires.on_tuple_summary(
            {
                "trial_sum_sharpe": float(summary.trial_sum_sharpe),
                "trial_sum_sharpe_sq": float(summary.trial_sum_sharpe_sq),
            }
        )

    def bind_reference_stamper(self, stamper: TradeReferenceStamper | None) -> None:
        """Late-bind the run's India-VIX stamper for the R45 production leg.

        The CLI builds the launch stamper INSIDE its abort-alert wrap — after
        this CM has already entered and composed :attr:`diffcheck` — so the
        binding arrives late by design; the closure reads the field at CALL
        time, so every sampled production re-simulation after this bind
        stamps with the run's own stamper (DEC-VIX-STAMP-WIRE-001 / Golden
        Rule #3). ``None`` keeps the honest unstamped default (an absent or
        empty VIX lake).
        """
        self._reference_stamper = stamper

    def _make_diffcheck(self, k_per_run: int, total_tuples: int) -> Callable[..., None] | None:
        """Build the budgeted R45 per-tuple closure (``None`` when disabled)."""
        if k_per_run <= 0:
            return None
        from brutex.engine.sweep_driver import build_equity_curve
        from brutex.engine.trade_sim import simulate_trades
        from brutex.metrics.compute import _drawdown_stats
        from brutex.metrics.types import ClosedTrade
        from brutex.strategies.types import Side

        # DEC-FILL-WORST-CASE-ONLY-001: the ONE hardcoded worst-case fill law
        # prices BOTH legs — the production trade sim here and the naive
        # reference's independently re-stated worst-case arm — so the R45
        # differential is always a same-law compare with no mode plumbing.

        def _production_simulate(
            bars: Sequence[Any],
            entry_signals: Sequence[bool],
            *,
            direction: str,
            tf_minutes: int,
            stop_loss_paisa: int | None,
            take_profit_paisa: int | None,
        ) -> Sequence[Any]:
            return simulate_trades(
                bars,
                entry_signals,
                direction=Side(direction),
                tf_minutes=tf_minutes,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                # DEC-VIX-STAMP-WIRE-001 (Golden Rule #3): the production leg
                # mirrors the run's own VIX binding — the CLI late-binds the
                # launch stamper via bind_reference_stamper, read here at CALL
                # time. The reference compare surface (COMPARED_TRADE_FIELDS)
                # carries no VIX field, so the stamp is compare-neutral.
                reference_stamper=self._reference_stamper,
            )

        def _production_drawdown(
            bars: Sequence[Any],
            trades: Sequence[Any],
            *,
            start_equity_paisa: int,
            tf_minutes: int,
        ) -> tuple[int, float, int, float]:
            return _drawdown_stats(
                build_equity_curve(
                    bars,
                    cast("Sequence[ClosedTrade]", trades),
                    start_equity_paisa=start_equity_paisa,
                    tf_minutes=tf_minutes,
                )
            )

        hook = DiffCheckHook(
            sweep_id=self.run_id,
            tuple_digest_of=lambda t, g: _tuple_digest(
                cast("SweepTuple", t), cast("SweepGridConfig", g)
            ),
            production_simulate=_production_simulate,
            production_drawdown=_production_drawdown,
            on_mismatch=self._on_mismatch,
        )
        per_tuple = per_tuple_diffcheck_budget(k_per_run, total_tuples)
        budget = {"left": k_per_run}

        def _diffcheck(
            *,
            bars: Sequence[Any],
            bar_bits: Sequence[int],
            tuple_: Any,
            grid: Any,
        ) -> None:
            give = min(per_tuple, budget["left"])
            if give <= 0:
                return
            report = hook.run_for_tuple(
                bars=bars, bar_bits=bar_bits, tuple_=tuple_, grid=grid, budget_left=give
            )
            budget["left"] -= report.checked
            if report.checked:
                self._guarded(
                    "recorder.diffcheck_ok",
                    lambda: self.recorder.record(
                        "DIFFCHECK_OK",
                        {"checked": report.checked, "mismatches": report.mismatches},
                    ),
                )

        return _diffcheck

    def _quarantine_store(self, tuple_: SweepTuple, exc: BaseException) -> None:
        """The G1 composition: quarantine the tuple's trio (forensics, never delete)."""
        outcome = quarantine_tuple_trio(
            tuple_store_path(self._results_root, tuple_, self._grid),
            results_root=self._results_root,
            reason=f"{type(exc).__name__}: {exc}",
            run_id=self.run_id,
        )
        self._guarded(
            "recorder.quarantine",
            lambda: self.recorder.record(
                "QUARANTINE",
                {
                    "what": "tuple_store",
                    "path_rel": str(outcome.dest.relative_to(self._results_root)),
                    "reason": outcome.reason,
                },
            ),
        )

    def record_worker_quarantine(self, dest_rel: str, reason: str) -> None:
        """Record a PARALLEL-worker store-quarantine event in the flight recorder (G1).

        The parallel driver's ``on_quarantine`` adapter (MAJOR round-2 fix): the
        worker already MOVED the corrupt tuple trio in-process (so the grid never
        aborts), then returns the ``(dest_rel, reason)`` as data; this records the
        SAME ``QUARANTINE`` flight record the sequential :meth:`_quarantine_store`
        emits, so the forensic trail matches across modes. Fail-soft (I-3).
        """
        self._guarded(
            "recorder.quarantine_worker",
            lambda: self.recorder.record(
                "QUARANTINE",
                {"what": "tuple_store", "path_rel": dest_rel, "reason": reason},
            ),
        )

    # ── launch / terminal progress ────────────────────────────────────────

    def _reset_progress_at_launch(self) -> None:
        """Write the launch baseline progress file (G4 — reset-at-launch)."""
        if self._progress_path is None:
            return
        from brutex.engine.sweep_supervisor import idle_progress

        payload = idle_progress()
        payload.update(
            {
                "state": "running",
                "running": True,
                "run_id": self.run_id,
                "stage": self._stage,
                "suspect": False,
                "wall_time_utc": self._wall(),
                "pid": os.getpid(),
            }
        )
        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, sort_keys=True).encode("utf-8")
        atomic_write_simple(self._progress_path, lambda tmp: tmp.write_bytes(content))

    def _terminal_progress_patch(self) -> None:
        """Patch the progress file terminal (``running=False`` + fresh wall/pid)."""
        if self._progress_path is None:
            return
        try:
            payload = json.loads(self._progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        payload.update(
            {
                "running": False,
                "run_id": self.run_id,
                "stage": self._stage,
                "suspect": self.suspect,
                "wall_time_utc": self._wall(),
                "pid": os.getpid(),
            }
        )
        content = json.dumps(payload, sort_keys=True).encode("utf-8")
        atomic_write_simple(self._progress_path, lambda tmp: tmp.write_bytes(content))


@contextmanager
def sweep_run_core(
    *,
    results_root: Path,
    grid: SweepGridConfig,
    alerter: SweepAlerter,
    entry_label: str,
    total_tuples: int | None = None,
    workers: int = 1,
    detach_child: bool = False,
    progress_path: Path | None = None,
    checkpoint_store: SweepCheckpointStore | None = None,
    cli_chain_path: Path | None = None,
    selection_extra: Mapping[str, object] | None = None,
    inner_on_tuple: Callable[[SweepTuple, SweepRunSummary], None] | None = None,
    inner_on_progress: Callable[[SweepProgressSnapshot], None] | None = None,
    tripwire_config: TripwireConfig | None = None,
    diffcheck_k: int | None = None,
    log_file: Path | None = None,
    wall: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> Generator[RunCoreHandles, None, None]:
    """Compose the unkillable run core around ONE grid run (design §2, the CLI seam).

    ENTER order (G13 alerter-first — *alerter* exists BEFORE this CM):
    (1) results-root flock (G6 — a peer sweep raises
    :class:`~brutex.engine.sweep_singleflight.SweepAlreadyRunningError` before
    any chain writer opens); (2) exit-status ``state=running``; (3)
    ``keep_awake(entry_label)`` (G9); (4) recorder ``RUN_START`` (+ ``RESUME``
    when the checkpoint carries prior progress); (5) tripwires armed; (6)
    progress reset-at-launch. The G12 checkpoint pre-load runs at enter so a
    corrupt file is quarantined before the supervisor's own ``load()``.

    EXIT (all fail-soft; the with-body's exception ALWAYS re-raises unchanged):
    exit-status finalized (``COMPLETE`` / ``SUSPECT_COMPLETE`` / ``FAILED`` /
    ``HALTED``), verdicts built, manifest flushed + stamped + the ONE additive
    ``RUN_MANIFEST`` action appended to the CLI-level SHA-256 chain (D-3),
    ``RUN_END``/``HALT`` recorded, chain-map snapshotted, diagnostic bundle
    built, and the abort Telegram (``sweep_halted``) fired for an
    ``Exception``-class abort (never for an operator ``KeyboardInterrupt`` /
    ``SystemExit``).

    Parameters
    ----------
    results_root
        The sweep results root (hosts ``runs/<run_id>/`` + the G6 lock).
    grid
        The validated grid config — ``run_id = grid_identity(grid)`` (D-1).
    alerter
        The already-constructed :class:`~brutex.notifications.sweep_alerts.SweepAlerter`.
    entry_label
        The keep-awake reason label (e.g. ``"brutex sweep-all"``).
    total_tuples
        The grid's tuple count; ``None`` computes ``count_sweep_tuples(grid)``.
    workers
        The resolved worker budget (RUN_START observability only).
    detach_child
        ``True`` when this process IS a detached child (RUN_START
        observability only — R7).
    progress_path
        The live progress JSON path (``None`` = no publishing).
    checkpoint_store
        The launch's checkpoint store for the G12 resilient pre-load
        (``None`` = skip).
    cli_chain_path
        The CLI-level SHA-256 sweep chain path for the D-3 ``RUN_MANIFEST``
        append (``None`` = skip).
    selection_extra
        Extra manifest ``selection`` keys the grid cannot know
        (exchange/segment/start/end).
    inner_on_tuple, inner_on_progress
        The caller's own hooks, composed FIRST (their raw contracts kept).
    tripwire_config
        Pre-resolved config (``None`` = ``TripwireConfig.from_env()`` — junk
        env raises HERE, before any lock, the launch-time fail-loud tier).
    diffcheck_k
        Pre-resolved R45 budget (``None`` = ``resolve_diffcheck_k()``, same
        fail-loud law).
    log_file
        Optional log file bundled into the diagnostic zip tail.
    wall, monotonic
        Injected clocks (tests).

    Yields
    ------
    RunCoreHandles
        The composed handles (see :class:`RunCoreHandles`).
    """
    from brutex.engine.sweep_supervisor import grid_identity

    run_id = grid_identity(grid)
    total = count_sweep_tuples(grid) if total_tuples is None else total_tuples
    config = TripwireConfig.from_env() if tripwire_config is None else tripwire_config
    k_per_run = resolve_diffcheck_k() if diffcheck_k is None else diffcheck_k

    prior = read_exit_status(results_root, run_id)
    prior_attempt = prior.get("attempt") if prior else None
    attempt = (prior_attempt + 1) if isinstance(prior_attempt, int) and prior_attempt >= 1 else 1
    # D-12 SUSPECT stickiness (never-adopt guarantee, I-4): a prior attempt that
    # latched SUSPECT (a diffcheck mismatch / tripwire on already-persisted
    # stores) is the ONLY durable "these result bytes may be wrong" signal.
    # ``run_id = grid_identity`` is timestamp-free, so a re-run resumes into the
    # SAME ``runs/<run_id>/`` and resume-SKIPS the offending (already-complete)
    # tuple — the SUSPECT would never re-fire. Carry it forward so the run can
    # never launder SUSPECT_COMPLETE back to a clean COMPLETE on resume.
    # D-12 never-launder (MAJOR round-2 fix): the exit-status flag is the
    # primary sticky-SUSPECT signal, but it is a single fail-soft artifact
    # written after the in-memory latch — a torn write / SIGKILL in that window
    # could leave it ``false``. Fold the durable, hash-chained flight-recorder
    # SUSPECT record as an independent belt: a run whose result bytes were ever
    # flagged possibly-wrong can NEVER resume-complete as a clean COMPLETE.
    prior_suspect = (bool(prior.get("suspect")) if prior else False) or recorder_has_suspect(
        results_root, run_id
    )

    with ExitStack() as stack:
        stack.enter_context(sweep_run_lock(results_root))  # G6 — BEFORE any writer
        stack.enter_context(run_id_scope(run_id))
        write_exit_status_soft(
            results_root,
            run_id=run_id,
            state=RunState.RUNNING,
            attempt=attempt,
            pid=os.getpid(),
            suspect=prior_suspect,  # sticky across resume (D-12 never-adopt)
        )
        stack.enter_context(keep_awake(entry_label))  # G9 — best-effort, never raises
        recorder = FlightRecorder.open(
            results_root,
            run_id=run_id,
            attempt=attempt,
            tuple_record_every=tuple_record_stride(total),
        )
        # Closed on stack unwind so even an ENTER failure (e.g. a fail-loud
        # env knob resolved late) never leaks the chain writer's fd.
        stack.callback(recorder.close)
        core = RunCoreHandles(
            run_id=run_id,
            attempt=attempt,
            recorder=recorder,
            tripwires=TripwireMonitor(
                config=config,
                enumerated_total=total,
                on_trip=lambda event: None,  # rebound below (needs the handles)
            ),
            manifest=ManifestAccumulator(
                results_root=results_root,
                run_id=run_id,
                selection=_selection_fragment(grid, selection_extra),
                attempt=attempt,
                monotonic=monotonic,
                wall=wall,
            ),
            on_progress=lambda snap: None,  # rebound below
            on_tuple=lambda tuple_, summary: None,  # rebound below
            diffcheck=None,
            store_quarantine=lambda tuple_, exc: None,  # rebound below
            _results_root=results_root,
            _grid=grid,
            _alerter=alerter,
            _progress_path=progress_path,
            _wall=wall,
            _monotonic=monotonic,
            _suspect=prior_suspect,  # D-12: SUSPECT carries forward across attempts
        )
        # Rebind the callbacks that need the finished handles object (the
        # dataclass cannot reference itself mid-construction).
        core.tripwires = TripwireMonitor(
            config=config,
            enumerated_total=total,
            on_trip=core._on_trip,
        )
        core.on_progress = core._compose_on_progress(inner_on_progress)
        core.on_tuple = core._compose_on_tuple(inner_on_tuple)
        core.diffcheck = core._make_diffcheck(k_per_run, total)
        core.store_quarantine = core._quarantine_store

        core._guarded(
            "recorder.run_start",
            lambda: recorder.record(
                "RUN_START",
                {
                    "entry_label": entry_label,
                    "workers": workers,
                    "detach_child": detach_child,
                    "pid": os.getpid(),
                    "tuples_total": total,
                    "diffcheck_k": k_per_run,
                    "data_snapshot_hash": grid.data_snapshot_hash,
                },
            ),
        )
        if prior_suspect:
            # Record the carried-forward SUSPECT in the recorder + manifest so
            # every G5/D-18 surface (exit-status already stamped above) agrees the
            # run stays SUSPECT. Not the full mark_suspect fan-out — no fresh
            # CRITICAL page for a state the prior attempt already alerted; a NEW
            # tripwire this attempt still fans out normally.
            core._guarded(
                "recorder.resume_suspect",
                lambda: recorder.record(
                    "SUSPECT",
                    {"reason": "resumed_suspect", "detail": "prior attempt latched SUSPECT"},
                ),
            )
            core._guarded(
                "manifest.resume_suspect", lambda: core.manifest.mark_suspect("resumed_suspect")
            )
        if checkpoint_store is not None:  # G12 — quarantine-start-fresh pre-load
            checkpoint: SweepCheckpoint | None = cast(
                "SweepCheckpoint | None",
                core._guarded(
                    "checkpoint.preload",
                    lambda: load_checkpoint_resilient(
                        checkpoint_store,
                        results_root=results_root,
                        recorder=recorder,
                        # G12 crash-loop breaker (MAJOR round-2 fix): the tracker
                        # is loaded from a DURABLE per-run counter file so the
                        # consecutive-corruption count survives the per-enter /
                        # per-process boundary a crash-loop is made of — the
                        # ``== threshold`` escalation edge is now reachable in
                        # the wired path (a fresh in-memory tracker could never
                        # reach 2 with one load per enter).
                        tracker=CheckpointCorruptionTracker.load(run_dir(results_root, run_id)),
                        on_crash_loop=lambda detail: alerter.run_suspect(
                            run_id=run_id, tripwire="checkpoint_crash_loop", detail=detail
                        ),
                    ),
                ),
            )
            if checkpoint is not None and checkpoint.next_index > 0:
                core._guarded(
                    "recorder.resume",
                    lambda: recorder.record("RESUME", {"resume_from": checkpoint.next_index}),
                )
        core._guarded("progress.reset", core._reset_progress_at_launch)
        core._guarded("manifest.stage_started", lambda: core.manifest.stage_started(core._stage))
        core._write_chain_map()

        try:
            yield core
        except BaseException as exc:
            state = RunState.HALTED if _is_halt(exc) else RunState.FAILED
            _finalize(
                core,
                state=state,
                exit_code=core.exit_code if core.exit_code is not None else 1,
                error_type=type(exc).__name__,
                cli_chain_path=cli_chain_path,
                log_file=log_file,
                checkpoint_store=checkpoint_store,
            )
            if isinstance(exc, Exception):
                reason = type(exc).__name__
                core._guarded(
                    "alerter.sweep_halted",
                    lambda: alerter.sweep_halted(run_id=run_id, reason=reason),
                )
            raise
        else:
            state = RunState.SUSPECT_COMPLETE if core.suspect else RunState.COMPLETE
            _finalize(
                core,
                state=state,
                exit_code=core.exit_code if core.exit_code is not None else 0,
                error_type=None,
                cli_chain_path=cli_chain_path,
                log_file=log_file,
                checkpoint_store=checkpoint_store,
            )


def _is_halt(exc: BaseException) -> bool:
    """A global-stop class (operator interrupt / lake halt) maps to ``HALTED``."""
    if not isinstance(exc, Exception):
        return True  # KeyboardInterrupt / SystemExit — operator-driven halt
    return isinstance(exc, LakeWriteHalt)


def _finalize(
    core: RunCoreHandles,
    *,
    state: RunState,
    exit_code: int,
    error_type: str | None,
    cli_chain_path: Path | None,
    log_file: Path | None,
    checkpoint_store: SweepCheckpointStore | None,
) -> None:
    """The exit pass — every step individually fail-soft (I-3)."""
    core._guarded("manifest.stage_ended", lambda: core.manifest.stage_ended(core._stage))
    core._guarded("progress.terminal", core._terminal_progress_patch)
    core._guarded(
        "exit_status.final",
        lambda: write_exit_status_soft(
            core._results_root,
            run_id=core.run_id,
            state=state,
            exit_code=exit_code,
            error_type=error_type,
            attempt=core.attempt,
            pid=os.getpid(),
            suspect=core.suspect,
        ),
    )
    if state in (RunState.COMPLETE, RunState.SUSPECT_COMPLETE):
        verdicts = core._guarded(
            "verdicts.build",
            lambda: build_run_verdicts(core._results_root, run_id=core.run_id),
        )
        if verdicts is not None:
            core._link_stamps["artifacts"] = StampVerdict.OK
        if checkpoint_store is not None:
            # G12 (round-4 hostile-review fix): a SUCCESSFUL run boundary breaks
            # the crash-loop chain. Reset the DURABLE consecutive-corruption
            # counter so a single crash-recovery (this run's own load was the
            # corrupt one → quarantine → fresh start → COMPLETE, which never
            # calls record_clean) cannot leave a sticky count that a LATER,
            # INDEPENDENT corruption escalates to a false CRITICAL 'crash loop'.
            core._guarded(
                "checkpoint_corruption.reset",
                lambda: reset_checkpoint_corruption(run_dir(core._results_root, core.run_id)),
            )
    core._guarded("manifest.exit_code", lambda: core.manifest.set_exit_code(exit_code))
    core._guarded(
        "manifest.recorder_degraded",
        lambda: core.manifest.set_recorder_degraded(core.recorder.degraded),
    )
    manifest_path = core._guarded("manifest.flush", core.manifest.flush)
    if manifest_path is not None:
        core._guarded(
            "manifest.stamp",
            lambda: write_stage_stamp(
                cast("Path", manifest_path), stage="manifest", run_id=core.run_id
            ),
        )
        if cli_chain_path is not None:
            core._guarded(
                "cli_chain.run_manifest",
                lambda: _append_run_manifest_line(
                    cast("Path", manifest_path), cli_chain_path, run_id=core.run_id
                ),
            )
    if error_type is None:
        core._guarded(
            "recorder.run_end",
            lambda: core.recorder.record(
                "RUN_END", {"exit_code": exit_code, "suspect": core.suspect}
            ),
        )
    else:
        core._guarded(
            "recorder.halt",
            lambda: core.recorder.record("HALT", {"reason": state.value, "error_type": error_type}),
        )
    core._write_chain_map()
    extra_files: list[Path] = []
    if core._progress_path is not None:
        extra_files.append(core._progress_path)
    if checkpoint_store is not None:
        extra_files.append(checkpoint_store.path)
    core._guarded(
        "diag_bundle.build",
        lambda: build_diagnostic_bundle(
            core._results_root,
            run_id=core.run_id,
            log_file=log_file,
            extra_files=tuple(extra_files),
        ),
    )
    # Row 15 (Ruling 15): mirror the R40 forensics to the INTERNAL disk so an
    # SSD-death — the exact failure the bundle exists to diagnose — cannot take
    # the only copy of the evidence with it. Best-effort DUPLICATE, run LAST +
    # fail-soft, so it can never change results or kill the run (I-3).
    core._guarded("forensics.mirror", lambda: mirror_run_forensics(core._results_root, core.run_id))


def mirror_run_forensics(
    results_root: Path, run_id: str, *, env: dict[str, str] | None = None
) -> Path | None:
    """Copy the run's R40 forensic artifacts to the INTERNAL-disk mirror (Row 15).

    Copies the diagnostic bundle + flight recorder + manifest + exit-status +
    chain-map + verdicts (whichever exist) from the results-root ``run_dir`` to
    :func:`~brutex.engine.run_layout.mirror_run_dir` so the forensics survive an
    SSD unplug/death. A best-effort DUPLICATE: mirroring disabled/unavailable
    returns ``None``, and each per-file copy is individually guarded so one
    unreadable member never aborts the mirror (I-3 — the caller wraps this in
    ``_guarded`` too, so any residual error is a WARN, never a raise).

    Parameters
    ----------
    results_root
        The sweep results root (the primary copy's home).
    run_id
        The run identity (D-1).
    env
        Environment mapping (injected in tests; defaults to ``os.environ``).

    Returns
    -------
    Path | None
        The mirror directory written to, or ``None`` when mirroring is
        disabled/unavailable.
    """
    import shutil

    dest_dir = mirror_run_dir(run_id, env=env)
    if dest_dir is None:
        return None
    source_dir = run_dir(results_root, run_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in _MIRRORED_FORENSIC_NAMES:
        src = source_dir / name
        if not src.is_file():
            continue
        with contextlib.suppress(OSError):
            shutil.copy2(src, dest_dir / name)
    return dest_dir


def _append_run_manifest_line(manifest_path: Path, chain_path: Path, *, run_id: str) -> None:
    """Append the ONE additive D-3 ``RUN_MANIFEST`` action to the CLI SHA-256 chain."""
    from brutex.orchestrator.audit import AuditWriter

    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = {"manifest": doc, "manifest_content_hash": content_hash_of(manifest_path)}
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(chain_path, resume=chain_path.exists()) as audit:
        audit.append(action="RUN_MANIFEST", subject=run_id, payload=payload)


__all__ = [
    "CASCADE_DOOR_BY_STAGE",
    "CORE_STAGES",
    "DECIMATION_TARGET_RECORDS",
    "MILESTONE_PCTS",
    "RunCoreHandles",
    "Stage3CellStampError",
    "per_tuple_diffcheck_budget",
    "sweep_run_core",
    "tuple_record_stride",
]
