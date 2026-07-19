"""Unit tests for the rank-memory budget planner (DEC-SWEEP-STREAMING-RANK-001).

Every branch of :mod:`brutex.engine.sweep_mem_budget` — the env knob
resolution, the full :func:`plan_rank_memory` decision table (batch-preserving
below budget · bounded engage · graceful refusals · mode ``on``'s structural
``ValueError`` refusals), the plan accessors, and the message contracts (every
refusal NAMES both env knobs + concrete remedies).
"""

from __future__ import annotations

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


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1b routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the `plan_rank_memory` decision table.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    value-identical to the flag-OFF Python law (which every OTHER test in
    this file drives) — plans compare as frozen dataclasses, refusal
    message bytes included.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        # The shared §11-COMMON builder carries EVERY `_CRATE_API` symbol —
        # a partial fake would latch `crate_stale` and turn this class vacuous.
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

    def test_flag_on_matches_python_law_every_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_mem_budget import BoundedRankMode, plan_rank_memory

        cases: list[dict[str, object]] = [
            # batch (within budget)
            {
                "n_masks": 1000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 8 * 1024**2,
            },
            # bounded (auto, past budget, eligible)
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            # refuse_no_top_k
            {
                "n_masks": 10_000_000,
                "top_k": None,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            # refuse_no_digest
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": False,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            # refuse_off
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.OFF,
                "budget_bytes": 1024,
            },
            # mode on → bounded regardless of budget
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.ON,
                "budget_bytes": 1024,
            },
        ]
        off = [plan_rank_memory(**case) for case in cases]  # type: ignore[arg-type]
        self._arm_fake_crate(monkeypatch)
        try:
            on = [plan_rank_memory(**case) for case in cases]  # type: ignore[arg-type]
            assert on == off
        finally:
            self._reset_facade()

    def test_flag_on_mode_on_config_raises_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine.sweep_mem_budget import BoundedRankMode, plan_rank_memory

        self._arm_fake_crate(monkeypatch)
        try:
            with pytest.raises(ValueError, match="LEGACY no-digest"):
                plan_rank_memory(
                    n_masks=10,
                    top_k=5,
                    digest_path=False,
                    mode=BoundedRankMode.ON,
                    budget_bytes=1024,
                )
            with pytest.raises(ValueError, match="requires a finite top_k"):
                plan_rank_memory(
                    n_masks=10,
                    top_k=None,
                    digest_path=True,
                    mode=BoundedRankMode.ON,
                    budget_bytes=1024,
                )
        finally:
            self._reset_facade()
