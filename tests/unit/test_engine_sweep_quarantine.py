"""Unit suite for :mod:`brutex.engine.sweep_quarantine` (G1/G12 — DEC-SWEEP-UNKILLABLE-CORE-001).

Pins the trio MOVE ORDER (sentinel first — the crash-safe direction), the
monotonic-``n`` (never wall-clock) quarantine dirnames, the checkpoint
garbage/truncated/foreign-id matrix (foreign id ⇒ ``None`` WITHOUT quarantine
— the existing store law), the QUARANTINE flight record, and the G12
second-consecutive-corruption CRITICAL escalation edge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

import pytest
from brutex.engine.sweep_checkpoint import (
    SweepCheckpoint,
    SweepCheckpointStore,
)
from brutex.engine.sweep_quarantine import (
    CHECKPOINT_QUARANTINE_SUBDIR,
    RUNTIME_QUARANTINE_SUBDIR,
    CheckpointCorruptionTracker,
    QuarantineOutcome,
    load_checkpoint_resilient,
    quarantine_tuple_trio,
)
from brutex.observability.flight_recorder import FlightRecorder, iter_run_records

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g1_trio_move_order_sentinel_first": ("G1",),
    "test_g1_monotonic_n_dirname_never_wall_clock": ("G1",),
    "test_g1_run_id_prefix_used_when_given": ("G1",),
    "test_g1_partial_trio_tolerated": ("G1",),
    "test_g1_empty_trio_yields_empty_moved": ("G1",),
    "test_g1_foreign_dirnames_never_break_the_ordinal_scan": ("G1",),
    "test_g1_member_vanishing_mid_move_is_skipped": ("G1",),
    "test_g12_garbage_checkpoint_quarantined_fresh_start": ("G12",),
    "test_g12_truncated_checkpoint_quarantined": ("G12",),
    "test_g12_foreign_sweep_id_none_without_quarantine": ("G12",),
    "test_g12_missing_checkpoint_none_without_quarantine": ("G12",),
    "test_g12_valid_checkpoint_passes_through": ("G12",),
    "test_g12_quarantine_flight_record_appended": ("G12",),
    "test_g12_second_corruption_critical_edge": ("G12",),
    "test_g12_clean_load_resets_the_streak": ("G12",),
    "test_g12_tracker_edge_semantics": ("G12",),
    "test_g12_tracker_threshold_validated": ("G12",),
    "test_g12_checkpoint_move_failure_is_fail_soft": ("G12",),
    "test_g12_checkpoint_quarantine_ordinal_monotonic": ("G12",),
    "test_g12_unknown_schema_version_quarantines": ("G12",),
    "test_g12_crash_loop_edge_without_callback_is_log_only": ("G12",),
}

_RUN_ID: Final[str] = "a" * 64


def _make_trio(results_root: Path, name: str = "bull_k3_deadbeef.parquet") -> Path:
    """Create a store + trial-stats + sentinel trio; return the store path."""
    store = results_root / name
    store.write_bytes(b"PARQ-GARBAGE")
    store.with_name(store.name + ".trials.json").write_text("{}", encoding="utf-8")
    store.with_name(store.name + ".complete.json").write_text("{}", encoding="utf-8")
    return store


def _checkpoint_store(path: Path, *, sweep_id: str = "sweep-1") -> SweepCheckpointStore:
    return SweepCheckpointStore(path=path, sweep_id=sweep_id, total_combos=10)


def test_g1_trio_move_order_sentinel_first(tmp_path: Path) -> None:
    """The sentinel moves FIRST, then trial stats, then the store (crash-safe order)."""
    store = _make_trio(tmp_path)
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="corrupt parquet")
    assert outcome == QuarantineOutcome(
        moved=(
            "bull_k3_deadbeef.parquet.complete.json",
            "bull_k3_deadbeef.parquet.trials.json",
            "bull_k3_deadbeef.parquet",
        ),
        dest=tmp_path / RUNTIME_QUARANTINE_SUBDIR / "bull_k3_deadbeef-1",
        reason="corrupt parquet",
    )
    # Source files gone; forensic copies present (moved, never deleted).
    assert not store.exists()
    assert not store.with_name(store.name + ".trials.json").exists()
    assert not store.with_name(store.name + ".complete.json").exists()
    for name in outcome.moved:
        assert (outcome.dest / name).exists()
    assert (outcome.dest / "bull_k3_deadbeef.parquet").read_bytes() == b"PARQ-GARBAGE"


def test_g1_monotonic_n_dirname_never_wall_clock(tmp_path: Path) -> None:
    """Successive quarantines of the same prefix land in -1, -2, -3 (monotonic n)."""
    dests = []
    for _ in range(3):
        store = _make_trio(tmp_path)
        dests.append(quarantine_tuple_trio(store, results_root=tmp_path, reason="r").dest)
    assert [d.name for d in dests] == [
        "bull_k3_deadbeef-1",
        "bull_k3_deadbeef-2",
        "bull_k3_deadbeef-3",
    ]


def test_g1_run_id_prefix_used_when_given(tmp_path: Path) -> None:
    """An explicit run_id becomes the quarantine dirname prefix (D-1 threading)."""
    store = _make_trio(tmp_path)
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="r", run_id=_RUN_ID)
    assert outcome.dest == tmp_path / RUNTIME_QUARANTINE_SUBDIR / f"{_RUN_ID}-1"


def test_g1_partial_trio_tolerated(tmp_path: Path) -> None:
    """A tuple that tore before writing its sidecars still quarantines the store."""
    store = tmp_path / "solo.parquet"
    store.write_bytes(b"X")
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="torn")
    assert outcome.moved == ("solo.parquet",)
    assert (outcome.dest / "solo.parquet").exists()


def test_g1_empty_trio_yields_empty_moved(tmp_path: Path) -> None:
    """Nothing on disk ⇒ moved=() (tolerant; the dest dir records the attempt)."""
    store = tmp_path / "ghost.parquet"
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="gone")
    assert outcome.moved == ()
    assert outcome.dest.is_dir()


def test_g1_foreign_dirnames_never_break_the_ordinal_scan(tmp_path: Path) -> None:
    """Foreign/non-digit sibling names are ignored by the monotonic scan."""
    parent = tmp_path / RUNTIME_QUARANTINE_SUBDIR
    parent.mkdir(parents=True)
    (parent / "bull_k3_deadbeef-abc").mkdir()  # non-digit tail — ignored
    (parent / "other-99").mkdir()  # different prefix — ignored
    (parent / "bull_k3_deadbeef-7").mkdir()  # genuine prior ordinal
    store = _make_trio(tmp_path)
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="r")
    assert outcome.dest.name == "bull_k3_deadbeef-8"


def test_g1_member_vanishing_mid_move_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member vanishing between the exists() probe and the move is skipped."""
    store = _make_trio(tmp_path)
    real_replace = os.replace

    def _racy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".trials.json"):
            raise FileNotFoundError(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _racy_replace)
    outcome = quarantine_tuple_trio(store, results_root=tmp_path, reason="race")
    assert outcome.moved == (
        "bull_k3_deadbeef.parquet.complete.json",
        "bull_k3_deadbeef.parquet",
    )


def test_g12_garbage_checkpoint_quarantined_fresh_start(tmp_path: Path) -> None:
    """Garbage JSON ⇒ quarantined forensic copy + None (fresh start), no raise."""
    path = tmp_path / "checkpoint.json"
    path.write_text("{{{ not json", encoding="utf-8")
    store = _checkpoint_store(path)
    result = load_checkpoint_resilient(store, results_root=tmp_path, recorder=None)
    assert result is None
    assert not path.exists()
    dest = tmp_path / CHECKPOINT_QUARANTINE_SUBDIR / "1.json"
    assert dest.read_text(encoding="utf-8") == "{{{ not json"


def test_g12_truncated_checkpoint_quarantined(tmp_path: Path) -> None:
    """A half-written (truncated) checkpoint quarantines the same way."""
    path = tmp_path / "checkpoint.json"
    good = _checkpoint_store(path)
    good.record(3)  # writes a valid checkpoint
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[: len(raw) // 2], encoding="utf-8")
    result = load_checkpoint_resilient(
        _checkpoint_store(path), results_root=tmp_path, recorder=None
    )
    assert result is None
    assert (tmp_path / CHECKPOINT_QUARANTINE_SUBDIR / "1.json").exists()


def test_g12_foreign_sweep_id_none_without_quarantine(tmp_path: Path) -> None:
    """A VALID checkpoint for another sweep id ⇒ None WITHOUT quarantine (existing law)."""
    path = tmp_path / "checkpoint.json"
    _checkpoint_store(path, sweep_id="other-sweep").record(4)
    result = load_checkpoint_resilient(
        _checkpoint_store(path, sweep_id="sweep-1"), results_root=tmp_path, recorder=None
    )
    assert result is None
    assert path.exists()  # NOT moved — it is another sweep's valid state
    assert not (tmp_path / CHECKPOINT_QUARANTINE_SUBDIR).exists()


def test_g12_missing_checkpoint_none_without_quarantine(tmp_path: Path) -> None:
    """No checkpoint file ⇒ None (a first run), nothing quarantined."""
    store = _checkpoint_store(tmp_path / "checkpoint.json")
    tracker = CheckpointCorruptionTracker()
    tracker.consecutive = 1
    result = load_checkpoint_resilient(store, results_root=tmp_path, recorder=None, tracker=tracker)
    assert result is None
    assert not (tmp_path / CHECKPOINT_QUARANTINE_SUBDIR).exists()
    assert tracker.consecutive == 0  # a clean (non-raising) load resets the streak


def test_g12_valid_checkpoint_passes_through(tmp_path: Path) -> None:
    """A valid same-sweep checkpoint is returned unchanged (and resets the tracker)."""
    path = tmp_path / "checkpoint.json"
    _checkpoint_store(path).record(5)
    tracker = CheckpointCorruptionTracker()
    tracker.consecutive = 1
    result = load_checkpoint_resilient(
        _checkpoint_store(path), results_root=tmp_path, recorder=None, tracker=tracker
    )
    assert isinstance(result, SweepCheckpoint)
    assert result.last_completed_index == 5
    assert result.sweep_id == "sweep-1"
    assert tracker.consecutive == 0


def test_g12_quarantine_flight_record_appended(tmp_path: Path) -> None:
    """The quarantine lands ONE QUARANTINE record with the D-15 payload shape."""
    path = tmp_path / "checkpoint.json"
    path.write_text("garbage", encoding="utf-8")
    recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1)
    result = load_checkpoint_resilient(
        _checkpoint_store(path), results_root=tmp_path, recorder=recorder
    )
    recorder.close()
    assert result is None
    records = [r for r in iter_run_records(tmp_path, _RUN_ID) if r["kind"] == "QUARANTINE"]
    assert len(records) == 1
    payload = records[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["what"] == "checkpoint"
    assert payload["path_rel"] == "checkpoint.json"
    assert "Malformed checkpoint JSON" in str(payload["reason"])
    assert payload["attempt"] == 1


def test_g12_second_corruption_critical_edge(tmp_path: Path) -> None:
    """The 2nd CONSECUTIVE corruption fires the crash-loop callback EXACTLY once."""
    calls: list[str] = []
    tracker = CheckpointCorruptionTracker()

    def _corrupt_once(n: int) -> None:
        path = tmp_path / "checkpoint.json"
        path.write_text(f"garbage-{n}", encoding="utf-8")
        result = load_checkpoint_resilient(
            _checkpoint_store(path),
            results_root=tmp_path,
            recorder=None,
            tracker=tracker,
            on_crash_loop=calls.append,
        )
        assert result is None

    _corrupt_once(1)
    assert calls == []  # first corruption: quarantine only, no escalation
    _corrupt_once(2)
    assert len(calls) == 1  # the edge — exactly once
    _corrupt_once(3)
    assert len(calls) == 1  # beyond the edge: no repeat alert spam


def test_g12_clean_load_resets_the_streak(tmp_path: Path) -> None:
    """corrupt → clean → corrupt never escalates (the streak reset breaks the loop)."""
    calls: list[str] = []
    tracker = CheckpointCorruptionTracker()
    path = tmp_path / "checkpoint.json"

    path.write_text("garbage-a", encoding="utf-8")
    load_checkpoint_resilient(
        _checkpoint_store(path),
        results_root=tmp_path,
        recorder=None,
        tracker=tracker,
        on_crash_loop=calls.append,
    )
    _checkpoint_store(path).record(2)  # a clean checkpoint lands
    assert (
        load_checkpoint_resilient(
            _checkpoint_store(path),
            results_root=tmp_path,
            recorder=None,
            tracker=tracker,
            on_crash_loop=calls.append,
        )
        is not None
    )
    path.write_text("garbage-b", encoding="utf-8")
    load_checkpoint_resilient(
        _checkpoint_store(path),
        results_root=tmp_path,
        recorder=None,
        tracker=tracker,
        on_crash_loop=calls.append,
    )
    assert calls == []  # never two CONSECUTIVE corruptions ⇒ no escalation


def test_g12_tracker_edge_semantics() -> None:
    """record_corruption fires True EXACTLY at the threshold; clean resets."""
    tracker = CheckpointCorruptionTracker()
    assert tracker.threshold == 2
    assert tracker.record_corruption() is False
    assert tracker.record_corruption() is True
    assert tracker.record_corruption() is False  # beyond the edge — no re-fire
    tracker.record_clean()
    assert tracker.consecutive == 0
    assert tracker.record_corruption() is False


def test_g12_tracker_threshold_validated() -> None:
    """A threshold < 1 is a construction defect and fails loudly."""
    with pytest.raises(ValueError, match="threshold"):
        CheckpointCorruptionTracker(threshold=0)
    one = CheckpointCorruptionTracker(threshold=1)
    assert one.record_corruption() is True  # edge at the very first corruption


def test_g12_tracker_persists_across_separate_instances(tmp_path: Path) -> None:
    """MAJOR (round 2): the consecutive count SURVIVES a fresh tracker per enter.

    A checkpoint crash-loop is cross-PROCESS: each relaunch loads the checkpoint
    ONCE with a FRESH tracker. A purely-in-memory tracker could reach at most
    ``consecutive == 1`` so the ``== threshold`` edge would be unreachable in
    production. ``load`` seeds the count from the durable per-run file so the 2nd
    corruption ACROSS separate trackers fires the edge EXACTLY once.
    """
    first = CheckpointCorruptionTracker.load(tmp_path)
    assert first.consecutive == 0
    assert first.record_corruption() is False  # 1st launch: no edge

    second = CheckpointCorruptionTracker.load(tmp_path)  # a fresh "process"
    assert second.consecutive == 1  # persisted from the first
    assert second.record_corruption() is True  # 2nd launch: the crash-loop edge

    third = CheckpointCorruptionTracker.load(tmp_path)
    assert third.consecutive == 2
    assert third.record_corruption() is False  # beyond the edge: no re-fire


def test_g12_tracker_clean_load_clears_persisted_count(tmp_path: Path) -> None:
    """A clean load persists a reset, so the next relaunch starts the streak fresh."""
    tracker = CheckpointCorruptionTracker.load(tmp_path)
    tracker.record_corruption()
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 1
    tracker.record_clean()
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0


def test_g12_reset_clears_durable_counter_after_success(tmp_path: Path) -> None:
    """Round-4: ``reset_checkpoint_corruption`` clears the sticky count on success.

    A single crash-recovery (corrupt load → quarantine → fresh start → COMPLETE)
    otherwise leaves ``consecutive == 1`` DURABLY, and a LATER independent
    corruption escalates ``1 -> 2 == threshold`` into a false CRITICAL. Resetting
    at the successful run boundary makes the next relaunch start the streak fresh.
    """
    from brutex.engine.sweep_quarantine import reset_checkpoint_corruption

    CheckpointCorruptionTracker.load(tmp_path).record_corruption()
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 1
    reset_checkpoint_corruption(tmp_path)
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0


def test_g12_reset_is_noop_and_fail_soft(tmp_path: Path) -> None:
    """Reset with no counter file is a no-op; an OSError is swallowed (I-3)."""
    from brutex.engine.sweep_quarantine import reset_checkpoint_corruption

    reset_checkpoint_corruption(tmp_path)  # no file yet — missing_ok, no raise
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0
    # A path whose parent is a FILE makes unlink raise OSError → suppressed.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    reset_checkpoint_corruption(blocker)  # unlink(blocker/<file>) raises → swallowed


def test_g12_tracker_load_tolerates_missing_and_malformed_state(tmp_path: Path) -> None:
    """A missing / malformed / out-of-range state file loads a clean 0 (fail-soft)."""
    from brutex.engine.sweep_quarantine import CHECKPOINT_CORRUPTION_STATE_FILENAME

    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0  # missing
    state = tmp_path / CHECKPOINT_CORRUPTION_STATE_FILENAME
    state.write_text("{not json", encoding="utf-8")
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0  # malformed
    state.write_text('{"consecutive": -3}', encoding="utf-8")
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0  # out of range
    state.write_text('["not-a-dict"]', encoding="utf-8")
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0  # wrong shape
    state.write_text('{"consecutive": "x"}', encoding="utf-8")
    assert CheckpointCorruptionTracker.load(tmp_path).consecutive == 0  # wrong type


def test_g12_tracker_in_memory_when_state_path_none() -> None:
    """A ``state_path=None`` tracker is purely in-memory (no persistence side effect)."""
    tracker = CheckpointCorruptionTracker()
    assert tracker.state_path is None
    tracker.record_corruption()  # must not raise despite no path
    tracker.record_clean()


def test_g12_tracker_persist_is_fail_soft_on_oserror(tmp_path: Path) -> None:
    """A persist write failure is swallowed (I-3) — the in-memory count still advances."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    # state_path's parent is a FILE → mkdir/write raise OSError, suppressed.
    tracker = CheckpointCorruptionTracker(state_path=blocker / "state.json")
    assert tracker.record_corruption() is False  # no raise; count still moves
    assert tracker.consecutive == 1


def test_g12_checkpoint_move_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed quarantine move still returns None (fresh start) — never a crash."""
    path = tmp_path / "checkpoint.json"
    path.write_text("garbage", encoding="utf-8")

    def _boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("dead disk")

    monkeypatch.setattr(os, "replace", _boom)
    result = load_checkpoint_resilient(
        _checkpoint_store(path), results_root=tmp_path, recorder=None
    )
    assert result is None
    assert path.exists()  # the corrupt file remains — the NEXT launch re-quarantines


def test_g12_checkpoint_quarantine_ordinal_monotonic(tmp_path: Path) -> None:
    """Successive checkpoint quarantines land at 1.json, 2.json (never overwrite)."""
    for n in (1, 2):
        path = tmp_path / "checkpoint.json"
        path.write_text(f"garbage-{n}", encoding="utf-8")
        load_checkpoint_resilient(_checkpoint_store(path), results_root=tmp_path, recorder=None)
    parent = tmp_path / CHECKPOINT_QUARANTINE_SUBDIR
    assert (parent / "1.json").read_text(encoding="utf-8") == "garbage-1"
    assert (parent / "2.json").read_text(encoding="utf-8") == "garbage-2"
    # A foreign non-digit name never confuses the scan.
    (parent / "notes.txt").write_text("operator note", encoding="utf-8")
    path = tmp_path / "checkpoint.json"
    path.write_text("garbage-3", encoding="utf-8")
    load_checkpoint_resilient(_checkpoint_store(path), results_root=tmp_path, recorder=None)
    assert (parent / "3.json").read_text(encoding="utf-8") == "garbage-3"


def test_g12_unknown_schema_version_quarantines(tmp_path: Path) -> None:
    """A future/unknown schema version is corruption-for-us: quarantine + fresh start."""
    path = tmp_path / "checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "version": 999,
                "sweep_id": "sweep-1",
                "total_combos": 10,
                "last_completed_index": 3,
                "updated_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    result = load_checkpoint_resilient(
        _checkpoint_store(path), results_root=tmp_path, recorder=None
    )
    assert result is None
    assert (tmp_path / CHECKPOINT_QUARANTINE_SUBDIR / "1.json").exists()


def test_g12_crash_loop_edge_without_callback_is_log_only(tmp_path: Path) -> None:
    """The escalation edge with NO callback bound is log-only — never a crash."""
    tracker = CheckpointCorruptionTracker()
    for n in (1, 2):
        path = tmp_path / "checkpoint.json"
        path.write_text(f"garbage-{n}", encoding="utf-8")
        result = load_checkpoint_resilient(
            _checkpoint_store(path),
            results_root=tmp_path,
            recorder=None,
            tracker=tracker,
            on_crash_loop=None,
        )
        assert result is None
    assert tracker.consecutive == 2  # the edge fired (logged) with no callback bound


# ────────────────────────────────────────────────────────────────────────
# DEC-RUST-SWEEP-OPS-001 — flag-ON routing parity (wheel-free fake crate)
# ────────────────────────────────────────────────────────────────────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the monotonic quarantine-ordinal fold.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must mint the SAME
    quarantine dirnames as the flag-OFF Python fold (which every OTHER test
    in this file drives) — the listing I/O and the MOVE stay Python on both
    arms, so the crash-ordering law is untouched.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_ordinals_match_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def quarantine_once(root: Path) -> str:
            (root / "sweeps").mkdir(parents=True, exist_ok=True)
            store = root / "sweeps" / "tuple.parquet"
            store.write_text("corrupt")
            return quarantine_tuple_trio(
                store, results_root=root, reason="routing", run_id="runx"
            ).dest.name

        off_root = tmp_path / "off"
        off_names = [quarantine_once(off_root), quarantine_once(off_root)]
        assert off_names == ["runx-1", "runx-2"]
        self._arm_fake_crate(monkeypatch)
        try:
            on_root = tmp_path / "on"
            on_names = [quarantine_once(on_root), quarantine_once(on_root)]
        finally:
            self._reset_facade()
        assert on_names == off_names

    def test_flag_on_unicode_dirname_degrades_to_oracle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A planted non-ASCII dirname refuses to the oracle — the answer is
        the ORACLE's Unicode-digit read on BOTH arms (never a latch)."""
        parent = tmp_path / ".quarantine" / "runtime"
        parent.mkdir(parents=True)
        (parent / "runx-٢").mkdir()  # int("٢") == 2 in Python
        (tmp_path / "sweeps").mkdir()
        store = tmp_path / "sweeps" / "tuple.parquet"
        store.write_text("corrupt")
        self._arm_fake_crate(monkeypatch)
        try:
            outcome = quarantine_tuple_trio(
                store, results_root=tmp_path, reason="routing", run_id="runx"
            )
            from brutex.engine.sweep_ops_rust import sweep_ops_rust_disabled

            assert outcome.dest.name == "runx-3"
            assert sweep_ops_rust_disabled() is False
        finally:
            self._reset_facade()
