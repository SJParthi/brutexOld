"""Unit tests for the sweep RUN-CORE gap-#3 + corrupt-checkpoint additions.

DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1605). Covers:

* :func:`brutex.engine.sweep_orchestrator.gate_identity_fields` (all four
  gate-config states) + its fold into ``_tuple_digest`` / ``tuple_store_path``.
* The gap-#3 REGRESSION PIN: a GATE-CONFIG change must NEVER cache-skip — the
  grid identity, the tuple cache key, and the tuple store path all DIFFER for a
  gated grid, while an ungated grid stays byte-identical (default-preservation).
* :func:`brutex.engine.sweep_supervisor.load_checkpoint_resilient` — a
  corrupt/truncated checkpoint is QUARANTINED, never fatal at resume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from brutex.engine.sweep_checkpoint import SweepCheckpointError, SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    gate_identity_fields,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_supervisor import (
    _grid_descriptor,
    _tuple_cache_params,
    grid_identity,
    load_checkpoint_resilient,
    tuple_cache_key,
)
from brutex.ranking import OverfitGateConfig
from brutex.strategies.types import Side


def _grid(**overrides: Any) -> SweepGridConfig:
    base: dict[str, Any] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)


def _overfit() -> OverfitGateConfig:
    return OverfitGateConfig(dsr_min=0.95, weight=0.05)


# ─────────────────────────── gate_identity_fields ───────────────────────────


def test_gate_identity_empty_when_both_gates_off() -> None:
    assert gate_identity_fields(_grid()) == {}


def test_gate_identity_overfit_only() -> None:
    fields = gate_identity_fields(_grid(overfit=_overfit()))
    assert set(fields) == {"overfit"}
    assert fields["overfit"] == {
        "dsr_min": 0.95,
        "weight": 0.05,
        "default_skew": 0.0,
        "default_excess_kurtosis": 0.0,
    }


def test_gate_identity_stage3_only() -> None:
    fields = gate_identity_fields(_grid(stage3_enable=True, stage3_top_k=5))
    assert set(fields) == {"stage3"}
    assert fields["stage3"] == {
        "enable": True,
        "top_k": 5,
        "max_fevals": 200,
        "max_restarts": 3,
    }


def test_gate_identity_both_gates() -> None:
    fields = gate_identity_fields(_grid(overfit=_overfit(), stage3_enable=True))
    assert set(fields) == {"overfit", "stage3"}


# ─────────────────── gap #3 regression pin: a gate change never cache-skips ───────────────────


def test_ungated_identity_is_byte_identical_across_equal_grids() -> None:
    # Default-preservation: two identical ungated grids hash identically, and the
    # descriptor carries NO gate_config key (byte-identical to the pre-fix value).
    g1, g2 = _grid(), _grid()
    assert grid_identity(g1) == grid_identity(g2)
    assert "gate_config" not in _grid_descriptor(g1)
    t1 = next(iter_sweep_tuples(g1))
    assert "gate_config" not in _tuple_cache_params(t1, g1)


def test_gated_grid_gets_distinct_identity_everywhere() -> None:
    ungated = _grid()
    gated = _grid(overfit=_overfit())
    tu = next(iter_sweep_tuples(ungated))
    tg = next(iter_sweep_tuples(gated))

    # 1. checkpoint identity (grid_identity → SweepCheckpointStore.sweep_id) differs
    #    → a gated re-run never RESUMES an ungated light-run's high-water mark.
    assert grid_identity(ungated) != grid_identity(gated)
    # 2. tuple cache key differs → the cache-skip path never FALSELY skips.
    assert tuple_cache_key(tu, ungated) != tuple_cache_key(tg, gated)
    # 3. store path differs → the gated Parquet never overwrites the ungated one.
    root = Path("/lake/sweeps")
    assert tuple_store_path(root, tu, ungated) != tuple_store_path(root, tg, gated)
    # And the descriptors carry the gate_config only when active.
    assert "gate_config" in _grid_descriptor(gated)
    assert "gate_config" in _tuple_cache_params(tg, gated)


def test_changing_gate_knob_re_keys() -> None:
    a = _grid(overfit=OverfitGateConfig(dsr_min=0.95))
    b = _grid(overfit=OverfitGateConfig(dsr_min=0.90))
    ta = next(iter_sweep_tuples(a))
    tb = next(iter_sweep_tuples(b))
    assert grid_identity(a) != grid_identity(b)
    assert tuple_cache_key(ta, a) != tuple_cache_key(tb, b)


def test_stage3_enable_re_keys() -> None:
    off = _grid()
    on = _grid(stage3_enable=True)
    assert grid_identity(off) != grid_identity(on)


# ─────────────────────────── load_checkpoint_resilient ───────────────────────────


def _store(tmp_path: Path, name: str = "ckpt.json", total: int = 10) -> SweepCheckpointStore:
    return SweepCheckpointStore(path=tmp_path / name, sweep_id="a" * 64, total_combos=total)


def test_resilient_load_clean_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(4)  # writes a valid checkpoint at index 4
    fresh = _store(tmp_path)  # a new store over the SAME file
    load_checkpoint_resilient(fresh)
    assert fresh.next_index == 5  # resumed the high-water mark


def test_resilient_load_absent_checkpoint_is_fresh(tmp_path: Path) -> None:
    store = _store(tmp_path)
    load_checkpoint_resilient(store)  # no file → clean fresh start
    assert store.next_index == 0


def test_resilient_load_corrupt_json_quarantines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text("{not valid json", encoding="utf-8")
    load_checkpoint_resilient(store)
    assert store.next_index == 0  # FRESH start, not a crash
    # The corrupt file is moved aside to a .corrupt-<ns> sibling (never deleted).
    assert not store.path.exists()
    quarantined = list(tmp_path.glob("ckpt.json.corrupt-*"))
    assert len(quarantined) == 1


def test_resilient_load_corrupt_when_file_absent_branch(tmp_path: Path) -> None:
    # A duck-typed store whose load() RAISES while its path does NOT exist —
    # exercises the ``if path.exists()`` False arc (no move, moved stays None).
    class _RaisingStore:
        def __init__(self, path: Path) -> None:
            self.path = path
            self._last_index = -1

        @property
        def next_index(self) -> int:
            return self._last_index + 1

        def load(self) -> None:
            raise SweepCheckpointError("boom")

    store = _RaisingStore(tmp_path / "ghost.json")
    load_checkpoint_resilient(store)  # type: ignore[arg-type]
    assert store.next_index == 0
    assert not (tmp_path / "ghost.json").exists()


def test_resilient_load_move_failure_still_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.path.write_text("garbage-not-json", encoding="utf-8")

    def _boom(self: Path, target: Any) -> None:
        raise OSError("cannot move")

    monkeypatch.setattr(Path, "rename", _boom)
    load_checkpoint_resilient(store)  # the move fails but the run still starts fresh
    assert store.next_index == 0
