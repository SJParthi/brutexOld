"""Unit tests for the rank-memory budget planner (DEC-SWEEP-STREAMING-RANK-001).

Every branch of :mod:`brutex.engine.sweep_mem_budget` — the env knob
resolution, the full :func:`plan_rank_memory` decision table (batch-preserving
below budget · bounded engage · graceful refusals · mode ``on``'s structural
``ValueError`` refusals), the plan accessors, and the message contracts (every
refusal NAMES both env knobs + concrete remedies).
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from brutex.config.hardware_envelope import UNDETECTED_RAM, reset_hardware_envelope_cache
from brutex.engine.sweep_mem_budget import (
    BATCH_RANK_BYTES_PER_KEPT,
    BOUNDED_RANK_MODE_ENV,
    DEFAULT_MEM_BUDGET_BYTES,
    MEM_BUDGET_ENV,
    MEM_BUDGET_FRACTION,
    BoundedRankMode,
    RankMemoryPlan,
    per_worker_mem_budget_bytes,
    plan_rank_memory,
    resolve_bounded_rank_mode,
    resolve_mem_budget_bytes,
    watermark_message,
)

_GIB = 1024**3


@pytest.fixture(autouse=True)
def _fresh_ram_cache() -> Iterator[None]:
    """Isolate from the process-global RAM detection cache (DEC-RAM-BUDGETS-001)."""
    reset_hardware_envelope_cache()
    yield
    reset_hardware_envelope_cache()


def _simulate_ram(monkeypatch: pytest.MonkeyPatch, total_bytes: int) -> None:
    """Force RAM detection to *total_bytes* (0 = the undetected sentinel)."""
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysconf_total_ram_bytes", lambda: total_bytes
    )
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysctl_total_ram_bytes", lambda: UNDETECTED_RAM
    )
    reset_hardware_envelope_cache()


# ────── resolve_bounded_rank_mode ──────


class TestResolveBoundedRankMode:
    def test_unset_defaults_to_auto(self) -> None:
        assert resolve_bounded_rank_mode({}) is BoundedRankMode.AUTO

    def test_blank_defaults_to_auto(self) -> None:
        assert resolve_bounded_rank_mode({BOUNDED_RANK_MODE_ENV: "   "}) is BoundedRankMode.AUTO

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("off", BoundedRankMode.OFF),
            ("auto", BoundedRankMode.AUTO),
            ("on", BoundedRankMode.ON),
            ("  ON  ", BoundedRankMode.ON),
            ("Off", BoundedRankMode.OFF),
        ],
    )
    def test_valid_values_case_and_whitespace_insensitive(
        self, raw: str, expected: BoundedRankMode
    ) -> None:
        assert resolve_bounded_rank_mode({BOUNDED_RANK_MODE_ENV: raw}) is expected

    def test_junk_raises_naming_the_knob(self) -> None:
        with pytest.raises(ValueError, match=BOUNDED_RANK_MODE_ENV) as excinfo:
            resolve_bounded_rank_mode({BOUNDED_RANK_MODE_ENV: "bounded"})
        assert "'off', 'auto', 'on'" in str(excinfo.value)

    def test_none_environ_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BOUNDED_RANK_MODE_ENV, "off")
        assert resolve_bounded_rank_mode() is BoundedRankMode.OFF


# ────── resolve_mem_budget_bytes ──────


class TestResolveMemBudgetBytes:
    def test_unset_with_undetected_ram_floors_at_8_gib(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The DEC-RAM-BUDGETS-001 sentinel law: RAM undetected → the old
        # hand-picked 8 GiB constant EXACTLY (pre-derivation behavior).
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert resolve_mem_budget_bytes({}) == DEFAULT_MEM_BUDGET_BYTES == 8 * _GIB

    def test_unset_with_48_gib_ram_derives_exact_old_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The 48 GiB CLAUDE.md §8 regression pin: 48 GiB × 1/6 == 8 GiB.
        _simulate_ram(monkeypatch, 48 * _GIB)
        assert resolve_mem_budget_bytes({}) == DEFAULT_MEM_BUDGET_BYTES

    def test_unset_with_detected_ram_scales_by_fraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _simulate_ram(monkeypatch, 96 * _GIB)
        assert resolve_mem_budget_bytes({}) == int(96 * _GIB * MEM_BUDGET_FRACTION)

    def test_blank_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert resolve_mem_budget_bytes({MEM_BUDGET_ENV: "  "}) == DEFAULT_MEM_BUDGET_BYTES

    def test_explicit_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The #1352 explicit-override contract is byte-unchanged — the env
        # value wins over any detection outcome.
        _simulate_ram(monkeypatch, 48 * _GIB)
        assert resolve_mem_budget_bytes({MEM_BUDGET_ENV: " 123456 "}) == 123456

    def test_non_integer_raises_naming_the_knob(self) -> None:
        with pytest.raises(ValueError, match=MEM_BUDGET_ENV):
            resolve_mem_budget_bytes({MEM_BUDGET_ENV: "8GiB"})

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_raises(self, raw: str) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            resolve_mem_budget_bytes({MEM_BUDGET_ENV: raw})

    def test_none_environ_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MEM_BUDGET_ENV, "4096")
        assert resolve_mem_budget_bytes() == 4096


# ────── plan_rank_memory decision table ──────


def _plan(
    *,
    n_masks: int = 100,
    top_k: int | None = 10,
    digest_path: bool = True,
    mode: BoundedRankMode = BoundedRankMode.AUTO,
    budget_bytes: int = 10**9,
) -> RankMemoryPlan:
    return plan_rank_memory(
        n_masks=n_masks,
        top_k=top_k,
        digest_path=digest_path,
        mode=mode,
        budget_bytes=budget_bytes,
    )


class TestPlanRankMemory:
    def test_projection_is_the_documented_model(self) -> None:
        plan = _plan(n_masks=7)
        assert plan.projected_bytes == 7 * BATCH_RANK_BYTES_PER_KEPT
        assert plan.n_masks == 7

    @pytest.mark.parametrize("mode", [BoundedRankMode.OFF, BoundedRankMode.AUTO])
    def test_below_budget_keeps_the_batch_path(self, mode: BoundedRankMode) -> None:
        plan = _plan(mode=mode, n_masks=10, budget_bytes=10 * BATCH_RANK_BYTES_PER_KEPT)
        assert plan.bounded is False
        assert plan.bounded_top_k is None
        assert plan.refusal is None

    def test_budget_boundary_is_inclusive(self) -> None:
        # projected == budget stays batch (`<=` — engage only strictly past it).
        plan = _plan(
            mode=BoundedRankMode.AUTO, n_masks=10, budget_bytes=10 * BATCH_RANK_BYTES_PER_KEPT
        )
        assert plan.bounded is False
        over = _plan(
            mode=BoundedRankMode.AUTO,
            n_masks=10,
            budget_bytes=10 * BATCH_RANK_BYTES_PER_KEPT - 1,
        )
        assert over.bounded is True

    def test_auto_over_budget_engages_bounded(self) -> None:
        plan = _plan(mode=BoundedRankMode.AUTO, n_masks=100, budget_bytes=1, top_k=5)
        assert plan.bounded is True
        assert plan.bounded_top_k == 5
        assert plan.refusal is None

    def test_off_over_budget_refuses_naming_knobs_and_remedies(self) -> None:
        plan = _plan(mode=BoundedRankMode.OFF, n_masks=100, budget_bytes=1)
        assert plan.bounded is False
        assert plan.refusal is not None
        assert MEM_BUDGET_ENV in plan.refusal
        assert BOUNDED_RANK_MODE_ENV in plan.refusal
        assert "remedies" in plan.refusal
        assert "never a raw MemoryError" in plan.refusal
        assert str(plan.projected_bytes) in plan.refusal

    def test_auto_over_budget_top_k_none_refuses_naming_top_k(self) -> None:
        plan = _plan(mode=BoundedRankMode.AUTO, n_masks=100, budget_bytes=1, top_k=None)
        assert plan.bounded is False
        assert plan.refusal is not None
        assert "top_k=None" in plan.refusal
        assert "set a finite top_k" in plan.refusal

    def test_auto_over_budget_legacy_path_refuses_naming_digest(self) -> None:
        plan = _plan(mode=BoundedRankMode.AUTO, n_masks=100, budget_bytes=1, digest_path=False)
        assert plan.bounded is False
        assert plan.refusal is not None
        assert "LEGACY" in plan.refusal
        assert "digest path" in plan.refusal

    def test_on_engages_bounded_regardless_of_budget(self) -> None:
        plan = _plan(mode=BoundedRankMode.ON, n_masks=1, budget_bytes=10**12, top_k=3)
        assert plan.bounded is True
        assert plan.bounded_top_k == 3
        assert plan.refusal is None

    def test_on_legacy_path_is_a_config_valueerror(self) -> None:
        with pytest.raises(ValueError, match="LEGACY no-digest"):
            _plan(mode=BoundedRankMode.ON, digest_path=False)

    def test_on_top_k_none_is_a_config_valueerror(self) -> None:
        with pytest.raises(ValueError, match="requires a finite top_k"):
            _plan(mode=BoundedRankMode.ON, top_k=None)


# ────── RankMemoryPlan accessors ──────


class TestRankMemoryPlanAccessors:
    def test_require_bounded_top_k_returns_the_engaged_k(self) -> None:
        plan = _plan(mode=BoundedRankMode.ON, top_k=42)
        assert plan.require_bounded_top_k() == 42

    def test_require_bounded_top_k_raises_on_batch_plan(self) -> None:
        plan = _plan(mode=BoundedRankMode.OFF)
        with pytest.raises(ValueError, match="did not engage"):
            plan.require_bounded_top_k()


# ────── watermark_message ──────


class TestWatermarkMessage:
    def test_names_exact_bytes_budget_and_knobs(self) -> None:
        message = watermark_message(owned_bytes=2048, budget_bytes=1024, n_kept=17)
        assert "2048 bytes" in message
        assert "17 kept masks" in message
        assert f"{MEM_BUDGET_ENV}=1024" in message
        assert "remedies" in message
        assert "deterministic redo" in message

    def test_omits_the_engage_bounded_remedy(self) -> None:
        # HONESTY: the watermark fires only when the bounded rank is ALREADY
        # engaged, so "set BRUTEX_SWEEP_BOUNDED_RANK=auto/on" would be an
        # inapplicable remedy — the mode clause must be absent here (it stays
        # present in every PRE-FLIGHT refusal, pinned above).
        message = watermark_message(owned_bytes=2048, budget_bytes=1024, n_kept=17)
        assert BOUNDED_RANK_MODE_ENV not in message


# ────── per_worker_mem_budget_bytes (DEC-SWEEP-SCALE-GUARDS-001, the OOM fix) ──────


class TestPerWorkerMemBudget:
    """The worker-aware budget divide — the OOM fix's arithmetic core."""

    def test_single_worker_is_byte_unchanged(self) -> None:
        # Pin (b): the sequential path (one resident tuple) divides by 1, so the
        # per-tuple budget is byte-for-byte the pre-change whole-machine figure.
        assert per_worker_mem_budget_bytes(8 * _GIB, 1) == 8 * _GIB

    def test_eight_workers_divide_the_total(self) -> None:
        # The 48 GiB §8 box: default total 8 GiB / 8 workers == 1 GiB per tuple.
        assert per_worker_mem_budget_bytes(8 * _GIB, 8) == 1 * _GIB

    def test_override_is_divided_the_same_way(self) -> None:
        # The BRUTEX_SWEEP_MEM_BUDGET_BYTES override stays a TOTAL, divided the
        # same way — an operator's 4 GiB total under 4 workers is 1 GiB/tuple.
        assert per_worker_mem_budget_bytes(4 * _GIB, 4) == 1 * _GIB

    def test_floor_is_one_byte_never_zero(self) -> None:
        # A pathological total//workers == 0 must never yield a zero budget (that
        # would refuse every non-empty tuple); the floor is 1 byte.
        assert per_worker_mem_budget_bytes(3, 8) == 1

    def test_integer_division_floors(self) -> None:
        assert per_worker_mem_budget_bytes(10, 3) == 3

    @pytest.mark.parametrize("bad", [0, -1, -8])
    def test_rejects_non_positive_worker_count(self, bad: int) -> None:
        with pytest.raises(ValueError, match="sweep_concurrency must be >= 1"):
            per_worker_mem_budget_bytes(8 * _GIB, bad)

    def test_eight_concurrent_k5_retention_stays_under_half_ram(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin (a): the OOM guarantee. On the 48 GiB §8 box the default total is
        # 8 GiB; under the bounded rank each of 8 concurrent tuples is capped at
        # the per-worker budget, so the concurrent aggregate is well under half
        # of RAM (8 × 1 GiB = 8 GiB ≤ 24 GiB) — never the ~42 GB the undivided
        # budget allowed (8 × 5.21 GB).
        _simulate_ram(monkeypatch, 48 * _GIB)
        total_ram = 48 * _GIB
        workers = 8
        per_tuple = per_worker_mem_budget_bytes(resolve_mem_budget_bytes({}), workers)
        assert workers * per_tuple <= total_ram // 2

    def test_k5_bounded_rank_engages_under_default_workers_but_not_single(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin (c): a k=5 tuple's ~5.21 GB batch projection sat JUST UNDER the
        # undivided 8 GiB gate, so the bounded rank NEVER engaged (silent OOM
        # risk). Divided across 8 workers the gate is 1 GiB, so it now ENGAGES.
        _simulate_ram(monkeypatch, 48 * _GIB)
        n_masks_k5 = math.comb(60, 5)  # 5,461,512 masks
        projected = n_masks_k5 * BATCH_RANK_BYTES_PER_KEPT  # ~5.21 GB
        total = resolve_mem_budget_bytes({})
        assert total == 8 * _GIB
        # The register's exact numbers: 5.21 GB projection under the 8 GiB gate.
        assert 5 * _GIB < projected < 6 * _GIB

        # Single worker (undivided): the projection is UNDER the gate → batch (never bounded).
        single_plan = plan_rank_memory(
            n_masks=n_masks_k5,
            top_k=1000,
            digest_path=True,
            mode=BoundedRankMode.AUTO,
            budget_bytes=per_worker_mem_budget_bytes(total, 1),
        )
        assert single_plan.bounded is False

        # Default 8 workers (divided): the 1 GiB gate is exceeded → bounded ENGAGES.
        parallel_plan = plan_rank_memory(
            n_masks=n_masks_k5,
            top_k=1000,
            digest_path=True,
            mode=BoundedRankMode.AUTO,
            budget_bytes=per_worker_mem_budget_bytes(total, 8),
        )
        assert parallel_plan.bounded is True
        assert parallel_plan.bounded_top_k == 1000
