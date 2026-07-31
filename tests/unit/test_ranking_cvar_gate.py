"""Unit matrix for the CVaR tail-risk gate (``brutex.ranking.cvar_gate``).

DEC-RANK-CVAR-TAIL-GATE-001. Covers the whole module at 100% line+branch
inside the ci.yml ``--cov=brutex.ranking`` package gate: the integer-exact
law (hand-derived golden vectors, k-rounding boundaries, the strict-``<``
decision), the config validation, the ``BRUTEX_RANK_CVAR_RUST`` flag
matrix, the kernel resolve/degrade ladder (wheel missing/stale, faults,
refused results), the burn-in self-check + autorevert latch, and the
real-wheel kernel parity (the fno_math wheel is a transitive requirement of
importing ``brutex.ranking`` at all — see ``brutex.metrics.rust_stats`` —
so the real-kernel tests run unconditionally).
"""

from __future__ import annotations

import sys
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import ClassVar, NoReturn

import pytest
from brutex import ranking
from brutex.ranking.cvar_gate import (
    ALPHA_BPS_MAX,
    CVAR_RUST_DEFAULT,
    CVAR_RUST_ENV,
    CVAR_SELF_CHECK_SAMPLES,
    GATE_CVAR_TAIL,
    CvarGateConfig,
    CvarTailFold,
    _cvar_tail_fold_python,  # pyright: ignore[reportPrivateUsage]
    _valid_fold_result,  # pyright: ignore[reportPrivateUsage]
    cvar_kernel_disabled,
    cvar_rejects,
    cvar_rust_enabled,
    cvar_tail_fold,
    reset_cvar_observability_for_tests,
    reset_cvar_rust_for_tests,
)
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice() -> Iterator[None]:
    """Pin ``BRUTEX_RUST_STRICT=0`` — this FILE drives the degrade lattice.

    Under DEC-RUST-STRICT-NO-FALLBACK-001 the shipped default is strict-ON, so
    every CLASS-A/CLASS-B degrade this suite exercises (``wheel_missing``,
    ``wheel_stale``, ``kernel_result_refused``, the burn-in autorevert +
    ``kernel_disabled``) would RAISE instead of serving the oracle triple. The
    mode is DECLARED here rather than by weakening a single assertion; the
    strict-ON refusals of the SAME reasons are pinned in
    :class:`TestStrictRefusal` below.

    Owns its OWN :class:`pytest.MonkeyPatch` context (never the shared
    ``monkeypatch``) so its teardown cannot interleave with the per-test
    ``delenv`` in :meth:`TestStrictRefusal._strict_on`.
    """
    from brutex.config import rust_strict

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rust_strict.RUST_STRICT_ENV, "0")
        yield


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag unset + fresh burn-in/latch/memo per test (isolation)."""
    monkeypatch.delenv(CVAR_RUST_ENV, raising=False)
    reset_cvar_rust_for_tests()
    reset_cvar_observability_for_tests()
    yield
    reset_cvar_rust_for_tests()
    reset_cvar_observability_for_tests()


def _fake_wheel(kernel: object) -> SimpleNamespace:
    """A fake ``fno_math`` module carrying *kernel* as the fold symbol."""
    return SimpleNamespace(stats_cvar_tail_fold=kernel)


# ────── config validation ──────


class TestCvarGateConfig:
    def test_valid_defaults(self) -> None:
        cfg = CvarGateConfig(threshold_paisa=50_000)
        assert (cfg.threshold_paisa, cfg.alpha_bps) == (50_000, 500)

    @pytest.mark.parametrize("alpha", [1, ALPHA_BPS_MAX])
    def test_alpha_domain_bounds_are_valid(self, alpha: int) -> None:
        assert CvarGateConfig(threshold_paisa=1, alpha_bps=alpha).alpha_bps == alpha

    @pytest.mark.parametrize("bad", [1.0, True, "5"])
    def test_threshold_exact_int_type_required(self, bad: object) -> None:
        with pytest.raises(ValueError, match="threshold_paisa must be an exact int"):
            CvarGateConfig(threshold_paisa=bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [500.0, True])
    def test_alpha_exact_int_type_required(self, bad: object) -> None:
        with pytest.raises(ValueError, match="alpha_bps must be an exact int"):
            CvarGateConfig(threshold_paisa=1, alpha_bps=bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, -1])
    def test_nonpositive_threshold_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="threshold_paisa must be > 0"):
            CvarGateConfig(threshold_paisa=bad)

    @pytest.mark.parametrize("bad", [0, ALPHA_BPS_MAX + 1])
    def test_alpha_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="alpha_bps must be in"):
            CvarGateConfig(threshold_paisa=1, alpha_bps=bad)


# ────── the integer-exact fold law (flag unset ⇒ the Python oracle) ──────


class TestFoldLaw:
    def test_hand_derived_worst_five_percent_of_100(self) -> None:
        # n=100, alpha=500 ⇒ k = ceil(100·500/10000) = 5. pnls = -1000,
        # -900, …, +8900 (step +100): worst 5 = [-1000, -900, -800, -700,
        # -600] ⇒ var (5th smallest) = -600, tail_sum = -4000.
        pnls = list(range(-1000, 8901, 100))
        assert cvar_tail_fold(pnls, 500) == CvarTailFold(
            n=100, k=5, var_paisa=-600, tail_sum_paisa=-4000
        )

    def test_alpha_rounding_boundary_rounds_up(self) -> None:
        # n=41, alpha=500 ⇒ 41·500 = 20_500 ⇒ ceil(2.05) = 3; the worst 3
        # of [0, 10, …, 400] are [0, 10, 20] ⇒ var=20, sum=30.
        fold = cvar_tail_fold([i * 10 for i in range(41)], 500)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (3, 20, 30)

    def test_alpha_exact_boundary_does_not_round_up(self) -> None:
        # n=40 ⇒ 40·500 = 20_000 ⇒ exactly 2 (no spurious +1 on the exact
        # multiple — kills the `+9_999` → `+10_000` mutant class).
        fold = cvar_tail_fold([i * 10 for i in range(40)], 500)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (2, 10, 10)

    def test_single_trade_k_floors_at_one(self) -> None:
        # n=1, alpha=1 ⇒ ceil(1/10000) = 1 — the max(1, …) floor.
        assert cvar_tail_fold([-77], 1) == CvarTailFold(n=1, k=1, var_paisa=-77, tail_sum_paisa=-77)

    def test_whole_distribution_alpha_10000(self) -> None:
        fold = cvar_tail_fold([5, -3, 9, -8], ALPHA_BPS_MAX)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (4, 9, 3)

    def test_all_equal_values_tie_law(self) -> None:
        fold = cvar_tail_fold([-50] * 10, 5_000)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (5, -50, -250)

    def test_all_positive_pnls_tail_is_positive(self) -> None:
        fold = cvar_tail_fold([10, 20, 30, 40], 2_500)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (1, 10, 10)

    def test_permutation_invariance(self) -> None:
        pnls = [7, -9, 3, -9, 0, 12, -1]
        assert cvar_tail_fold(pnls, 3_000) == cvar_tail_fold(list(reversed(pnls)), 3_000)

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="pnls must be non-empty"):
            cvar_tail_fold([], 500)

    @pytest.mark.parametrize("bad", [0, ALPHA_BPS_MAX + 1])
    def test_alpha_out_of_range_raises(self, bad: int) -> None:
        with pytest.raises(ValueError, match="alpha_bps must be in"):
            cvar_tail_fold([1], bad)

    def test_oracle_alpha_zero_floor_is_defensive(self) -> None:
        # The `max(1, …)` floor's teeth: alpha_bps=0 is OUT of the public
        # contract (cvar_tail_fold validates [1, 10_000] first), but the
        # private oracle's floor must still yield k=1 — never k=0, whose
        # empty tail would IndexError. Kills the floor-drop mutant the
        # in-contract domain provably cannot (ceil ≥ 1 whenever alpha ≥ 1).
        from brutex.ranking.cvar_gate import (
            _cvar_tail_fold_python,  # pyright: ignore[reportPrivateUsage]
        )

        fold = _cvar_tail_fold_python([-5, 3, 9], 0)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == (1, -5, -5)

    def test_tail_mean_x100_is_exact_floor_division(self) -> None:
        # tail_sum=-4000, k=5 ⇒ (-4000·100)//5 = -80_000 centi-paisa.
        fold = CvarTailFold(n=100, k=5, var_paisa=-600, tail_sum_paisa=-4000)
        assert fold.tail_mean_x100_paisa == -80_000
        # Floor toward -inf on a non-divisible negative: (-7·100)//2 = -350.
        assert CvarTailFold(n=4, k=2, var_paisa=-3, tail_sum_paisa=-7).tail_mean_x100_paisa == -350


# ────── the gate verdict (strict-< law + abstain) ──────


class TestCvarRejects:
    #: 100 trades whose worst-5% tail (k=5) sums to -4000 ⇒ mean tail loss
    #: EXACTLY 800 paisa/trade — the boundary fixture for the strict-< law.
    _PNLS: ClassVar[list[int]] = list(range(-1000, 8901, 100))

    def test_empty_pnls_abstains(self) -> None:
        assert cvar_rejects([], CvarGateConfig(threshold_paisa=1)) is False

    def test_threshold_equal_to_tail_mean_does_not_reject(self) -> None:
        # tail_sum = -4000 vs -threshold·k = -800·5 = -4000: NOT strictly
        # worse ⇒ pass (kills the `<` → `<=` mutant).
        assert cvar_rejects(self._PNLS, CvarGateConfig(threshold_paisa=800)) is False

    def test_one_paisa_worse_than_threshold_rejects(self) -> None:
        # threshold 799 ⇒ -threshold·k = -3995 > -4000 = tail_sum ⇒ REJECT.
        assert cvar_rejects(self._PNLS, CvarGateConfig(threshold_paisa=799)) is True

    def test_all_positive_pnls_never_reject(self) -> None:
        assert cvar_rejects([10, 20, 30], CvarGateConfig(threshold_paisa=1)) is False

    def test_single_catastrophic_trade_rejects_at_alpha_1(self) -> None:
        # alpha=1 bps over 100 trades still floors k at 1 — the single
        # worst trade (-50_000) exceeds the 10_000 threshold.
        pnls = [-50_000] + [100] * 99
        cfg = CvarGateConfig(threshold_paisa=10_000, alpha_bps=1)
        assert cvar_rejects(pnls, cfg) is True


# ────── flag resolution matrix ──────


class TestFlagResolution:
    def test_unset_is_default_on(self) -> None:
        # DEC-RUST-DEFAULT-ON-WAVE-001: unset ⇒ the kernel is ON.
        assert cvar_rust_enabled() is CVAR_RUST_DEFAULT is True

    def test_whitespace_empty_is_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "   ")
        assert cvar_rust_enabled() is CVAR_RUST_DEFAULT

    def test_truthy_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        assert cvar_rust_enabled() is True

    def test_falsy_disarms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "off")
        assert cvar_rust_enabled() is False

    def test_junk_fails_safe_off_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert cvar_rust_enabled() is False
            assert cvar_rust_enabled() is False  # memoised: warn once
        warns = [e for e in logs if e["event"] == "cvar_gate.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_engine_unavailable_degrades_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        # None in sys.modules makes `import brutex.engine.sweep_lookahead`
        # raise ImportError — the wheel-absent engine shape.
        monkeypatch.setitem(sys.modules, "brutex.engine.sweep_lookahead", None)
        with capture_logs() as logs:
            assert cvar_rust_enabled() is CVAR_RUST_DEFAULT
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == [
            "flag_engine_unavailable"
        ]


# ────── kernel resolve / degrade ladder ──────


class TestKernelDegrades:
    _PNLS: ClassVar[list[int]] = [-40, -30, -20, -10, 0, 10, 20, 30, 40, 50]

    def _oracle(self) -> CvarTailFold:
        return _cvar_tail_fold_python(self._PNLS, 500)

    def test_flag_off_never_touches_the_kernel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bomb-kernel pin RETARGETED (DEC-RUST-DEFAULT-ON-WAVE-001): with the
        # explicit "0" kill switch the wheel is NEVER consulted (unset is
        # now default-ON).
        def _bomb(*_a: object, **_k: object) -> NoReturn:
            raise AssertionError("kernel touched on the OFF arm")

        monkeypatch.setenv(CVAR_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(_bomb))
        assert cvar_tail_fold(self._PNLS, 500) == self._oracle()

    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)  # import raises
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == [
            "wheel_missing"
        ]

    @pytest.mark.parametrize("stale_attr", [None, "not-callable"])
    def test_wheel_stale_degrades(
        self, monkeypatch: pytest.MonkeyPatch, stale_attr: object
    ) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        wheel = SimpleNamespace() if stale_attr is None else _fake_wheel(stale_attr)
        monkeypatch.setitem(sys.modules, "fno_math", wheel)
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == ["wheel_stale"]

    @pytest.mark.parametrize("exc", [ValueError("boom"), TypeError("boom"), OverflowError("boom")])
    def test_kernel_fault_degrades(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")

        def _raises(*_a: object, **_k: object) -> NoReturn:
            raise exc

        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(_raises))
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == ["kernel_error"]

    def test_foreign_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")

        def _raises(*_a: object, **_k: object) -> NoReturn:
            raise RuntimeError("byzantine")

        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(_raises))
        with pytest.raises(RuntimeError, match="byzantine"):
            cvar_tail_fold(self._PNLS, 500)

    def test_memory_error_re_raises_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")

        def _oom(*_a: object, **_k: object) -> NoReturn:
            raise MemoryError

        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(_oom))
        with pytest.raises(MemoryError):
            cvar_tail_fold(self._PNLS, 500)

    @pytest.mark.parametrize(
        "poison",
        [
            "not-a-tuple",
            (1, 2),  # wrong arity
            (True, -40, -40),  # bool k — exact-int refused
            (1, -40, True),  # bool tail_sum
            (0, -40, -40),  # k below 1
            (11, -40, -40),  # k above n
            (1, -40, -39),  # tail_sum > var·k — violates CVaR ≤ VaR
        ],
    )
    def test_shape_invalid_result_refused_loudly(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: poison))
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
            # Second call: the refusal ERROR is memoised (log-volume law).
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
        assert len([e for e in logs if e["event"] == "cvar_gate.kernel_result_refused"]) == 1

    def test_valid_fold_result_accepts_the_real_shape(self) -> None:
        assert _valid_fold_result((1, -40, -40), 10) is True

    def test_latched_kernel_short_circuits_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        # Latch via a burn-in mismatch: a valid-shape but WRONG triple.
        wrong = (5, -10, -100)  # k=5 valid for n=10; sum ≤ var·k holds
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: wrong))
        with capture_logs() as logs:
            served = cvar_tail_fold(self._PNLS, 500)
        assert served == self._oracle()  # the ORACLE triple ships
        assert cvar_kernel_disabled() is True
        assert [e["event"] for e in logs if e["log_level"] == "error"] == [
            "cvar_gate.kernel_autoreverted"
        ]
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
            assert cvar_tail_fold(self._PNLS, 500) == self._oracle()
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == [
            "kernel_disabled"
        ]


# ────── burn-in + steady state (fake kernel — deterministic) ──────


class TestBurnIn:
    _PNLS: ClassVar[list[int]] = [-40, -30, -20, -10, 0, 10, 20, 30, 40, 50]

    def test_burn_in_consumes_and_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        oracle = _cvar_tail_fold_python(self._PNLS, 500)
        good = (oracle.k, oracle.var_paisa, oracle.tail_sum_paisa)
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: good))
        reset_cvar_rust_for_tests(remaining=2)
        with capture_logs() as logs:
            assert cvar_tail_fold(self._PNLS, 500) == oracle
            assert cvar_tail_fold(self._PNLS, 500) == oracle
        assert len([e for e in logs if e["event"] == "cvar_gate.engaged"]) == 1

    def test_post_burn_in_runs_unchecked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # DOCUMENTED envelope: after burn-in a valid-shape kernel triple is
        # served UNCOMPARED — proven by a wrong-but-valid fake triple.
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        wrong = (5, -10, -100)
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: wrong))
        reset_cvar_rust_for_tests(remaining=0)
        fold = cvar_tail_fold(self._PNLS, 500)
        assert (fold.k, fold.var_paisa, fold.tail_sum_paisa) == wrong
        assert cvar_kernel_disabled() is False

    def test_disable_kernel_is_idempotent(self) -> None:
        # The latch keeps its FIRST reason; a repeat is a silent no-op (the
        # rust_stats law — one autorevert ERROR per process, never two).
        from brutex.ranking.cvar_gate import (
            _KERNEL_DISABLED_REASON,  # pyright: ignore[reportPrivateUsage]
            _disable_kernel,  # pyright: ignore[reportPrivateUsage]
        )

        with capture_logs() as logs:
            _disable_kernel("first")
            _disable_kernel("second")
        assert _KERNEL_DISABLED_REASON["cvar"] == "first"
        assert len([e for e in logs if e["event"] == "cvar_gate.kernel_autoreverted"]) == 1

    def test_reset_restores_the_default_budget(self) -> None:
        reset_cvar_rust_for_tests(remaining=0)
        reset_cvar_rust_for_tests()
        from brutex.ranking.cvar_gate import (
            _SELF_CHECK_REMAINING,  # pyright: ignore[reportPrivateUsage]
        )

        assert _SELF_CHECK_REMAINING["cvar"] == CVAR_SELF_CHECK_SAMPLES


# ────── the REAL wheel (parity + element-degrade semantics) ──────


class TestRealKernel:
    def test_real_kernel_parity_and_engagement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        pnls = [7, -9, 3, -9, 0, 12, -1, -1000, 55, 2]
        with capture_logs() as logs:
            fold = cvar_tail_fold(pnls, 2_500)
        assert fold == _cvar_tail_fold_python(pnls, 2_500)
        # Anti-vacuous: the kernel genuinely engaged (never a silent oracle).
        assert [e["event"] for e in logs if e["event"] == "cvar_gate.engaged"] == [
            "cvar_gate.engaged"
        ]

    def test_real_kernel_i128_tail_sum_is_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        big = -(2**63)  # i64::MIN — two of them sum past i64 range
        fold = cvar_tail_fold([big, big, 2**63 - 1], ALPHA_BPS_MAX)
        assert fold.tail_sum_paisa == 2 * big + (2**63 - 1)

    def test_non_int_element_degrades_to_python_semantics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The kernel's Vec<i64> extraction raises TypeError → oracle serves.
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        pnls = [-1.5, 2, 3]  # float element — outside the int contract
        with capture_logs() as logs:
            fold = cvar_tail_fold(pnls, 500)  # type: ignore[arg-type]
        assert fold == _cvar_tail_fold_python(pnls, 500)  # type: ignore[arg-type]
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == ["kernel_error"]

    def test_int_past_i64_degrades_to_the_unbounded_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        pnls = [-(2**80), 5, 9]
        with capture_logs() as logs:
            fold = cvar_tail_fold(pnls, 500)
        assert fold == _cvar_tail_fold_python(pnls, 500)
        assert [e["reason"] for e in logs if e["event"] == "cvar_gate.fallback"] == ["kernel_error"]


# ────── mutation-workflow wiring pin ──────


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s ranking/DSR math mutmut pass.

    The DEC-LAKE-STATS-MANIFEST-001 round-8 lesson: an unwired mutation gate
    presented as a deferred one is a Golden-Rule-#10 defect. ``cvar_gate.py``
    rides the ranking-math pass (DEC-MUTATION-GATE-RANKING-001 lineage) with
    THIS file in the runner list — the deterministic pin fails if either
    half is ever dropped. HONEST: it pins the WIRING only — the weekly run
    executes on its own schedule/push triggers.
    """

    def test_module_and_runner_wired_into_the_ranking_pass(self) -> None:
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert "brutex/ranking/cvar_gate.py" in text, (
            "cvar_gate.py is not in mutation.yml's ranking-math --paths-to-mutate"
        )
        assert "tests/unit/test_ranking_cvar_gate.py" in text, (
            "test_ranking_cvar_gate.py is not in mutation.yml's ranking-math runner"
        )


# ────── package surface pins ──────


class TestPackageSurface:
    def test_ranking_re_exports(self) -> None:
        assert ranking.CvarGateConfig is CvarGateConfig
        assert ranking.cvar_rejects is cvar_rejects
        assert ranking.cvar_tail_fold is cvar_tail_fold
        assert ranking.CvarTailFold is CvarTailFold
        assert ranking.GATE_CVAR_TAIL == GATE_CVAR_TAIL == "cvar_tail"

    def test_cvar_token_is_deliberately_out_of_gate_reason_order(self) -> None:
        # The overfit-gate precedent (module docstring): gate_reasons judges
        # a ResultMetrics row; the CVaR verdict needs the per-trade P&L list
        # that never reaches it — the token must NOT join that vocabulary.
        assert GATE_CVAR_TAIL not in ranking.GATE_REASON_ORDER


# ────── strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ──────


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Every OTHER test in this module runs with ``BRUTEX_RUST_STRICT=0``. These
    DELETE the kill switch so strict mode takes its shipped default (ON) and
    pin that the choke points (``_warn_fallback_once`` / ``_disable_kernel``
    / the inline ``kernel_result_refused`` gate) REFUSE instead of quietly
    serving the Python oracle triple.
    """

    _PNLS: ClassVar[list[int]] = [-40, -30, -20, -10, 0, 10, 20, 30, 40, 50]

    @staticmethod
    @contextmanager
    def _strict_on() -> Generator[None]:
        """Delete the kill switch for the DURATION OF THE TEST BODY ONLY.

        Owns its OWN :class:`pytest.MonkeyPatch` context rather than the
        shared ``monkeypatch`` fixture: ``_strict_off_degrade_lattice`` sets
        ``BRUTEX_RUST_STRICT=0`` in a *separate* context and pytest tears
        fixtures down LIFO across fixture objects — a shared-``monkeypatch``
        ``delenv`` here would record ``"0"`` as the value to restore and leak
        the kill switch into every later test file in the process.
        """
        from brutex.config import rust_strict

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
            assert rust_strict.strict_mode() is True
            yield

    # ── CLASS A — wheel absent / stale / flag junk ──────────────────────────

    def test_class_a_wheel_missing_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)  # import raises
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="wheel_missing"):
            cvar_tail_fold(self._PNLS, 500)

    def test_class_a_wheel_stale_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace())
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="wheel_stale"):
            cvar_tail_fold(self._PNLS, 500)

    def test_class_a_flag_unrecognised_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd kill switch must be fatal, never a quiet OFF."""
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(CVAR_RUST_ENV, "banana")
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="flag_unrecognised"):
            cvar_rust_enabled()

    # ── CLASS B — Rust ran, returned a structurally-refused RESULT ──────────

    def test_class_b_result_refused_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed kernel triple is a crate BUG — never papered over."""
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: (1, 2)))
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="kernel_result_refused"):
            cvar_tail_fold(self._PNLS, 500)

    def test_class_b_burn_in_mismatch_refuses_before_the_latch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The autorevert refuses BEFORE ``_KERNEL_DISABLED_REASON`` is set.

        That is what makes the latched ``cvar_kernel_disabled()``
        early-return structurally unreachable under strict mode — the wiring
        is complete by construction, not by sampling.
        """
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        wrong = (5, -10, -100)  # valid shape for n=10, WRONG answer
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(lambda *_a, **_k: wrong))
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="self_check_mismatch"):
            cvar_tail_fold(self._PNLS, 500)
        assert cvar_kernel_disabled() is False

    # ── CLASS C — the Rust arm still serves ────────────────────────────────

    def test_class_c_flag_engine_unavailable_does_not_refuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engine package is unimportable, so the flag CLASSIFIER is —
        but the resolver then returns :data:`CVAR_RUST_DEFAULT` and the RUST
        arm still serves. An advisory, not a fall-back: refusing here would
        crash a call the crate is about to handle."""
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex.engine.sweep_lookahead", None)
        with self._strict_on():
            assert cvar_rust_enabled() is CVAR_RUST_DEFAULT

    def test_class_c_exempt_set_is_exactly_the_advisory_token(self) -> None:
        from brutex.ranking import cvar_gate

        assert (
            frozenset(  # pyright: ignore[reportPrivateUsage]
                {"flag_engine_unavailable"}
            )
            == cvar_gate._STRICT_EXEMPT_REASONS
        )

    # ── The HELD (overloaded) token ────────────────────────────────────────

    def test_unresolved_kernel_error_is_held_not_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``kernel_error`` is genuinely TWO classes at once (the crate's own
        ``Err`` arms vs a PyO3 ``Vec<i64>`` extraction failure), so it is HELD
        in ``_STRICT_UNRESOLVED_REASONS`` and NOT refused — guessing would
        either crash a legal call or hide a genuine kernel refusal. Splitting
        the token is the real fix (see the module constant's evidence)."""
        from brutex.ranking import cvar_gate

        assert frozenset({"kernel_error"}) == cvar_gate._STRICT_UNRESOLVED_REASONS  # pyright: ignore[reportPrivateUsage]

        def _raises(*_a: object, **_k: object) -> NoReturn:
            raise TypeError("Vec<i64> extraction failed")

        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", _fake_wheel(_raises))
        with self._strict_on():
            assert cvar_tail_fold(self._PNLS, 500) == _cvar_tail_fold_python(self._PNLS, 500)

    # ── The kill switch still works ────────────────────────────────────────

    def test_strict_off_still_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config import rust_strict

        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
        monkeypatch.setenv(CVAR_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert cvar_tail_fold(self._PNLS, 500) == _cvar_tail_fold_python(self._PNLS, 500)
