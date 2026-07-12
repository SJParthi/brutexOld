"""Unit gate for :mod:`brutex.ranking.rust_ranking` (slice-4 PR-1).

100% line + branch over the flag-gated Rust ranking binding: the
``BRUTEX_RANKING_RUST`` resolution (default OFF), the ONE ``rank_combos``
dispatch seam (ON → Rust pipeline; wheel missing/stale → verbatim Python;
OFF → byte-identical legacy), the per-family parity wiring (burn-in for the
(a)/(b) families, ALWAYS-ON differential for the (b*) families), the
refuse-poison validators, and the ship-the-oracle / autorevert laws — plus
the source-level AST pins on ``rust/fno-math/src/ranking.rs`` (no rayon /
``mul_add`` / ``total_cmp`` — the order-sensitivity + signed-zero laws).

The pools used for the strong "kernel genuinely engaged, zero divergence"
assertions are constructed so every ``_zscores`` intermediate is EXACT
(symmetric two-survivor columns → mean/variance/sqrt all exact), making
those assertions host-libm-independent; the pow-identity-sensitive arms are
exercised separately (the always-on differential ships the oracle either
way — pinned by the output-equality assertions).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import fno_math
import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, RankedCombo, rank_combos
from brutex.ranking.rust_ranking import (
    RANKING_RUST_DEFAULT,
    RANKING_RUST_ENV,
    rank_combos_via_rust,
    ranking_rust_enabled,
    reset_ranking_rust_for_tests,
)
from structlog.testing import capture_logs

from tests.fakes.metrics_fixtures import make_result_metrics
from tests.fakes.pow_identity import pow_identity_holds


@pytest.fixture(autouse=True)
def _fresh_families() -> Any:
    """Every test observes fresh burn-in budgets, latches and memos."""
    reset_ranking_rust_for_tests()
    yield
    reset_ranking_rust_for_tests()


def _m(**overrides: Any) -> ResultMetrics:
    """A gate-passing ResultMetrics; override per scenario (COMMON fixture)."""
    return make_result_metrics(**overrides)


def _exact_pool() -> list[tuple[int, ResultMetrics]]:
    """Two survivors whose z-score arithmetic is EXACT on every host.

    Only the Sharpe column varies (1.0 vs 3.0 → mean 2.0, deviations ±1.0,
    squares 1.0, variance 1.0, sqrt(1.0) = 1.0 — every intermediate exact,
    so ``x ** 2`` / ``x ** 0.5`` vs ``x * x`` / ``sqrt`` cannot differ);
    every other column is zero-variance (all-equal → the exact 0.0
    short-circuit). The strong engaged-no-divergence assertions ride this.
    """
    return [
        (0b01, _m(sharpe_ratio=1.0)),
        (0b10, _m(sharpe_ratio=3.0)),
    ]


def _rank_off(candidates: list[tuple[int, ResultMetrics]], **kw: Any) -> list[RankedCombo]:
    """The verbatim Python reference arm (explicit OFF)."""
    import os

    prior = os.environ.get(RANKING_RUST_ENV)
    os.environ[RANKING_RUST_ENV] = "0"
    try:
        return rank_combos(candidates, **kw)
    finally:
        if prior is None:
            del os.environ[RANKING_RUST_ENV]
        else:
            os.environ[RANKING_RUST_ENV] = prior


#: The (b*) always-on-differential families — the ONLY families whose
#: bit-agreement is HOST-dependent (they embed the ``_zscores`` pow-identity
#: sites; every other family's arithmetic is deterministic IEEE ops).
_B_STAR_FAMILIES = frozenset({"ranking.composite", "ranking.dsr_z"})


def _assert_reverts_are_expected_or_loud_b_star(
    logs: list[Any], expected: tuple[str, ...] = ()
) -> None:
    """The golden legs' pow-probe autorevert law (the R12.1 host-fragility fix).

    *expected* names the deterministic burn-in autoreverts the test provokes
    (exactly those, in pipeline order). ANY other autorevert must be a LOUD
    (b*) always-on divergence — legal ONLY on a non-pow-identity host (a pool
    with genuine column variance rides the ``_zscores`` pow sites, so its
    (b*) bit-agreement is host-dependent by design; ship-the-oracle keeps the
    wire byte-identical either way, pinned by each caller's OFF-arm equality).
    On an identity host (none measured so far — the production M4-Pro itself
    measured non-identity, verify-m4pro run 29194011032, coordinator-ruled
    2026-07-12) any unexpected autorevert FAILS — the pre-fix strength is
    preserved where it was ever honestly claimable.
    """
    reverted = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
    deterministic = [e["family"] for e in reverted if e["family"] not in _B_STAR_FAMILIES]
    assert deterministic == list(expected)
    for event in reverted:
        if event["family"] in _B_STAR_FAMILIES:
            assert event["reason"] == "divergence"
            assert not pow_identity_holds()


def _keys(ranked: list[RankedCombo]) -> list[tuple[int, bytes, object]]:
    import struct

    return [
        (
            rc.mask,
            struct.pack("<d", rc.composite_score),
            None if rc.deflated_sharpe is None else struct.pack("<d", rc.deflated_sharpe),
        )
        for rc in ranked
    ]


# ── the flag ────────────────────────────────────────────────────────────


class TestRankingRustFlag:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RANKING_RUST_ENV, raising=False)
        assert RANKING_RUST_DEFAULT is False
        assert ranking_rust_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "ON"])
    def test_truthy_enables(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, value)
        assert ranking_rust_enabled() is True

    @pytest.mark.parametrize("value", ["0", "off"])
    def test_falsy_disables(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, value)
        assert ranking_rust_enabled() is False

    def test_unrecognised_fails_safe_off_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, "flase")
        with capture_logs() as logs:
            assert ranking_rust_enabled() is False
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["flag_unrecognised"]

    def test_reset_seam_supports_post_burn_in_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # remaining=0 puts every burn-in family in the steady state: the
        # gates oracle is NOT consulted, so a (valid-shape) poisoned verdict
        # flows through — the inverted engagement teeth (Golden Rule #10).
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        reset_ranking_rust_for_tests(remaining=0)
        monkeypatch.setattr(fno_math, "ranking_passes_gates", lambda *a: [False, True])
        ranked = rank_combos(_exact_pool())
        assert [rc.mask for rc in ranked] == [0b10]  # candidate 0b01 poisoned out


# ── the ONE rank_combos dispatch seam ───────────────────────────────────


class TestRankCombosDispatch:
    def test_off_arm_is_byte_identical_and_touches_no_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, "0")

        def _bomb(*args: object) -> object:
            raise AssertionError("the OFF arm must never touch the kernel")

        monkeypatch.setattr(fno_math, "ranking_passes_gates", _bomb)
        with capture_logs() as logs:
            ranked = rank_combos(_exact_pool())
        assert [rc.mask for rc in ranked] == [0b10, 0b01]
        assert not [e for e in logs if str(e["event"]).startswith("rust_parity.")]

    def test_on_arm_matches_off_arm_bitwise_and_engages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = _rank_off(_exact_pool())
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        with capture_logs() as logs:
            on = rank_combos(_exact_pool())
        assert _keys(on) == _keys(off)
        engaged = {e["family"] for e in logs if e["event"] == "rust_parity.engaged"}
        # The exact pool cannot diverge on any host — every family engages.
        assert {"ranking.gates", "ranking.term_inputs", "ranking.composite", "ranking.order"} <= (
            engaged
        )
        assert not [e for e in logs if e["event"] == "rust_parity.autoreverted"]

    def test_on_arm_with_wheel_missing_falls_back_to_verbatim_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)
        with capture_logs() as logs:
            ranked = rank_combos(_exact_pool())
        assert [rc.mask for rc in ranked] == [0b10, 0b01]
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["wheel_missing"]

    def test_on_arm_with_stale_wheel_falls_back_to_verbatim_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        monkeypatch.delattr(fno_math, "ranking_rank_order")
        with capture_logs() as logs:
            ranked = rank_combos(_exact_pool())
        assert [rc.mask for rc in ranked] == [0b10, 0b01]
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert [e["reason"] for e in events] == ["wheel_stale"]


# ── the ON-arm pipeline (rank_combos_via_rust) ──────────────────────────


class TestRankCombosViaRust:
    def test_empty_candidates_rank_empty(self) -> None:
        assert rank_combos_via_rust([]) == []

    def test_all_rejected_rank_empty(self) -> None:
        assert rank_combos_via_rust([(1, _m(total_trades=1))]) == []

    def test_matches_python_on_a_mixed_pool(self) -> None:
        pool = [
            (0b001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
            (0b010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
            (0b100, _m(sharpe_ratio=None, profit_factor=None)),  # rejected
        ]
        on = rank_combos_via_rust(pool)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool))

    def test_overfit_active_arm_matches_python(self) -> None:
        pool = [
            (0b0001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
            (0b0010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
            (0b0100, _m(sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8)),
            (0b1000, _m(sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5)),
        ]
        cfg = OverfitGateConfig(dsr_min=0.95)
        on = rank_combos_via_rust(pool, overfit=cfg)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool, overfit=cfg))
        assert all(rc.deflated_sharpe is not None for rc in on)
        assert all(rc.dsr_n_scope == "tuple" for rc in on)

    def test_overfit_abstains_on_an_undeflatable_pool(self) -> None:
        # All-identical Sharpes → zero cross-trial variance → abstain: the
        # survivors pass through with None DSRs, exactly like Python.
        pool = [(0b01, _m()), (0b10, _m())]
        cfg = OverfitGateConfig(dsr_min=0.95)
        on = rank_combos_via_rust(pool, overfit=cfg)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool, overfit=cfg))
        assert all(rc.deflated_sharpe is None for rc in on)

    def test_overfit_with_no_gate_survivors_skips_the_gate(self) -> None:
        pool = [(0b01, _m(total_trades=1)), (0b10, _m(total_trades=2))]
        assert rank_combos_via_rust(pool, overfit=OverfitGateConfig()) == []

    def test_overfit_rejecting_every_survivor_ranks_empty(self) -> None:
        pool = [
            (0b01, _m(sharpe_ratio=0.001)),
            (0b10, _m(sharpe_ratio=0.002)),
        ]
        cfg = OverfitGateConfig(dsr_min=1.0)
        on = rank_combos_via_rust(pool, overfit=cfg)
        assert on == _rank_off(pool, overfit=cfg) == []

    def test_duplicate_survivor_mask_raises_the_shared_valueerror(self) -> None:
        pool = [(7, _m(sharpe_ratio=1.0)), (7, _m(sharpe_ratio=2.0))]
        with pytest.raises(ValueError, match=r"duplicate survivor mask\(s\): \[7\]"):
            rank_combos_via_rust(pool)
        # Byte-identical to the Python arm's message.
        with pytest.raises(ValueError, match=r"duplicate survivor mask\(s\): \[7\]"):
            _rank_off(pool)

    def test_wheel_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert rank_combos_via_rust(_exact_pool()) is None

    def test_beyond_2_53_paisa_serves_the_oracle_via_kernel_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # avg MFE beyond the cr_div fast-path envelope: the term kernel
        # raises ValueError → the family serves the Python oracle (which
        # computes CPython's correctly-rounded int/int) — never a wrong bit.
        pool = [
            (0b01, _m(avg_mfe_paisa=(1 << 53) + 1, avg_mae_paisa=3)),
            (0b10, _m(avg_mfe_paisa=(1 << 53) + 3, avg_mae_paisa=3)),
        ]
        with capture_logs() as logs:
            on = rank_combos_via_rust(pool)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool))
        events = [e for e in logs if e["event"] == "rust_parity.fallback"]
        assert ("kernel_error", "ranking.term_inputs") in {
            (e["reason"], e["family"]) for e in events
        }


# ── refuse-poison validators + divergence laws ──────────────────────────


class TestPoisonAndDivergence:
    def test_gates_shape_poison_is_refused_and_the_oracle_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fno_math, "ranking_passes_gates", lambda *a: [1, 0])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.gates"]

    def test_gates_wrong_length_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fno_math, "ranking_passes_gates", lambda *a: [True])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        assert [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]

    def test_gates_value_poison_in_burn_in_autoreverts_and_ships_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Valid SHAPE, wrong VALUES: the burn-in bit-compare catches it,
        # ships the oracle verdicts and latches the family.
        monkeypatch.setattr(fno_math, "ranking_passes_gates", lambda *a: [False, False])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))  # oracle shipped
        reverted = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
        assert [e["family"] for e in reverted] == ["ranking.gates"]

    def test_term_rows_shape_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fno_math, "ranking_composite_term_inputs", lambda *a: [[1.0] * 6, [1.0] * 6]
        )
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.term_inputs"]

    def test_term_rows_non_list_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole-value shape arm (not a list / wrong row count).
        monkeypatch.setattr(fno_math, "ranking_composite_term_inputs", lambda *a: "junk")
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.term_inputs"]

    def test_composite_shape_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fno_math, "ranking_composite_scores", lambda *a: [0.1, None])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.composite"]

    def test_composite_value_poison_always_on_ships_the_oracle_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The (b*) wire invariant: EVERY call compares, so a valid-shape
        # wrong-value composite can never reach the leaderboard — even
        # long past any burn-in budget.
        reset_ranking_rust_for_tests(remaining=0)
        monkeypatch.setattr(fno_math, "ranking_composite_scores", lambda *a: [9.0, -9.0])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))  # oracle shipped
        reverted = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
        assert [e["family"] for e in reverted] == ["ranking.composite"]

    def test_dsr_z_shape_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = [
            (0b0001, _m(sharpe_ratio=2.0)),
            (0b0010, _m(sharpe_ratio=2.5)),
            (0b0100, _m(sharpe_ratio=3.0)),
        ]
        cfg = OverfitGateConfig(dsr_min=0.0)
        monkeypatch.setattr(fno_math, "ranking_dsr_z_contributions", lambda *a: "junk")
        with capture_logs() as logs:
            on = rank_combos_via_rust(pool, overfit=cfg)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool, overfit=cfg))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.dsr_z"]

    def test_order_non_permutation_poison_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fno_math, "ranking_rank_order", lambda *a: [0, 0])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        refused = [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]
        assert [e["family"] for e in refused] == ["ranking.order"]

    def test_order_bool_elements_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # bool IS an int subtype — the permutation validator must reject it.
        monkeypatch.setattr(fno_math, "ranking_rank_order", lambda *a: [False, True])
        with capture_logs() as logs:
            on = rank_combos_via_rust(_exact_pool())
        assert on is not None
        assert _keys(on) == _keys(_rank_off(_exact_pool()))
        assert [e for e in logs if e["event"] == "rust_parity.kernel_result_refused"]

    def test_overfit_verdict_poison_in_burn_in_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = [
            (0b0001, _m(sharpe_ratio=2.0)),
            (0b0010, _m(sharpe_ratio=2.5)),
            (0b0100, _m(sharpe_ratio=3.0)),
        ]
        cfg = OverfitGateConfig(dsr_min=0.0)
        monkeypatch.setattr(fno_math, "ranking_overfit_verdicts", lambda *a: [False] * 3)
        with capture_logs() as logs:
            on = rank_combos_via_rust(pool, overfit=cfg)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool, overfit=cfg))
        # After the shipped oracle verdicts keep all 3 survivors, the REAL
        # (b*) kernels run over this genuine-variance pool — host-dependent
        # by design, so the deterministic revert list is asserted through
        # the pow-probe law (the R12.1 class, same fold).
        _assert_reverts_are_expected_or_loud_b_star(logs, ("ranking.overfit_verdict",))


# ── source-level AST pins on the new kernel file ────────────────────────


def test_ranking_kernel_source_has_no_rayon_mul_add_or_total_cmp() -> None:
    """The ranking kernels are ORDER- and SIGN-sensitive transliterations:
    a rayon reduction would change the naive left folds, an explicit FMA
    (`mul_add`) would fuse where CPython rounds twice, and `total_cmp`
    would split `-0.0` from `+0.0` where Python tuple compare does not.
    Pin all three structurally (mirrors
    ``test_metrics_kernel_source_has_no_rayon``)."""
    src = (
        Path(__file__).resolve().parents[2] / "rust" / "fno-math" / "src" / "ranking.rs"
    ).read_text(encoding="utf-8")
    assert "use rayon" not in src
    assert "rayon::" not in src
    assert ".mul_add(" not in src
    assert ".total_cmp(" not in src
    # The clamp must be the literal py_min/py_max idiom, never f64::clamp
    # (NaN-propagating) — match the call shape, not the helper definitions.
    assert ".clamp(" not in src


# ── mutation-workflow wiring pin (the TestMutationWorkflowWiring precedent) ──


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    Slice-4 PR-1 review round 7 (R7.2/R7.3): the refuter caught BOTH new
    parity wrapper modules shipping with NO ``--paths-to-mutate`` naming
    them — the exact unwired-gate class MIGRATION #1614's round 8 recorded
    (Golden Rule #10) — while the same PR added ``ranking.rs`` to
    cargo-mutants while the slice-4 design's CI-wiring clause
    (DEC-RANKING-RUST-001) requires "the weekly mutmut loop" for the
    wrapper Python modules. The
    module:testfile:timeout pair now sits in the workflow's per-module
    ``mutmut`` loop (empty third field = the historical wrap-less runner —
    no spin-capable loop constructs here, bounded ``for`` only); this
    deterministic pin (the ``test_lake_stats_manifest.py`` precedent) fails
    if the pair is ever dropped. HONEST: it pins the WIRING only — the
    weekly run executes on its own schedule/push triggers.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert (
            '"brutex/ranking/rust_ranking.py:tests/unit/test_ranking_rust_ranking.py:"' in text
        ), "rust_ranking.py is not wired into mutation.yml's per-module mutmut loop"


# ── mutation hardening (slice-4 PR-1 review round 7 — the R7.2/R7.3 fix) ──


class TestMutationHardening:
    """Deterministic pins killing the round-7 in-session mutmut survivors.

    The wired mutation gate (``TestMutationWorkflowWiring`` above) demands
    ZERO surviving mutants, so the module's behavioural literals (the env
    flag name, the family-name/policy table) and its defensive laws (the
    refuse-poison validators' shape refusals, the ``strict=True`` zip
    guards, the ``key``/``validate`` closures on every parity site) are
    pinned EXACTLY here (the ``test_lake_stats_manifest.py`` precedent).
    Structural residue was RESTRUCTURED killable instead of pragma'd: the
    quoted (runtime-inert) ``cast`` type strings became evaluated forms
    with no mutable literal, the tandem-redundant second strict zip merged
    into ONE loop, and the runtime-unevaluated ``list[float] | None`` local
    annotation became a two-branch assignment.
    """

    def test_flag_and_family_literals_pinned(self) -> None:
        from brutex.ranking.rust_ranking import _ALL_FAMILIES  # pyright: ignore[reportPrivateUsage]

        assert RANKING_RUST_ENV == "BRUTEX_RANKING_RUST"
        assert RANKING_RUST_DEFAULT is False
        assert tuple(f.name for f in _ALL_FAMILIES) == (
            "ranking.seam",
            "ranking.gates",
            "ranking.term_inputs",
            "ranking.composite",
            "ranking.dsr_z",
            "ranking.order",
            "ranking.overfit_verdict",
        )
        # The float-parity policy table (module docstring): (b*) rows —
        # composite + dsr_z — always-on; every other family burn-in.
        assert tuple(f.policy for f in _ALL_FAMILIES) == (
            "burn_in",
            "burn_in",
            "burn_in",
            "always",
            "always",
            "burn_in",
            "burn_in",
        )

    def test_validators_refuse_adversarial_shapes(self) -> None:
        from brutex.ranking.rust_ranking import (
            _is_bool_tuple,  # pyright: ignore[reportPrivateUsage]
            _is_float_list,  # pyright: ignore[reportPrivateUsage]
            _is_permutation,  # pyright: ignore[reportPrivateUsage]
            _is_term_rows,  # pyright: ignore[reportPrivateUsage]
        )

        # _is_bool_tuple: tuple-of-REAL-bools of exactly n.
        assert _is_bool_tuple((True, False), 2)
        assert not _is_bool_tuple([True, False], 2)  # list, not tuple
        assert not _is_bool_tuple((True, False), 3)  # wrong length
        assert not _is_bool_tuple((1, 0), 2)  # ints are not bools
        # _is_float_list: list-of-REAL-floats of exactly n.
        assert _is_float_list([1.0, 2.0], 2)
        assert not _is_float_list((1.0, 2.0), 2)  # tuple, not list
        assert not _is_float_list([1.0], 2)  # wrong length
        assert not _is_float_list([1, 2], 2)  # ints are not floats
        # _is_term_rows: n LIST rows of exactly width floats — a TUPLE of
        # valid rows must refuse (kills the round-7 `or → and` survivor,
        # which fell through the early return for non-list values).
        assert _is_term_rows([[1.0, 2.0]], 1, 2)
        assert not _is_term_rows(([1.0, 2.0],), 1, 2)  # tuple of rows
        assert not _is_term_rows([[1.0, 2.0]], 2, 2)  # wrong row count
        assert not _is_term_rows([[1.0]], 1, 2)  # wrong width
        assert not _is_term_rows([(1.0, 2.0)], 1, 2)  # tuple row
        # _is_permutation: an int tuple permuting range(n), bools refused.
        assert _is_permutation((1, 0), 2)
        assert not _is_permutation([1, 0], 2)  # list, not tuple
        assert not _is_permutation((0, 0), 2)  # not a permutation
        assert not _is_permutation((True, False), 2)  # bools are not ints
        assert not _is_permutation((1, 2), 2)  # not range(n)

    def test_gate_zip_refuses_a_short_verdict_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # strict=True is the defensive law on the survivors projection — a
        # parity layer returning FEWER verdicts than candidates must raise,
        # never silently truncate the pool (kills the strict=False mutant).
        from brutex.ranking import rust_ranking

        monkeypatch.setattr(rust_ranking, "run_with_parity", lambda *a, **k: (True,))
        with pytest.raises(ValueError, match="zip"):
            rank_combos_via_rust(_exact_pool())

    def test_overfit_zip_refuses_a_short_verdict_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.ranking import rust_ranking

        kernels = rust_ranking._resolve_kernels()  # pyright: ignore[reportPrivateUsage]
        assert kernels is not None
        candidates = _exact_pool()  # distinct Sharpes → deflatable pool
        monkeypatch.setattr(rust_ranking, "run_with_parity", lambda *a, **k: (True,))
        with pytest.raises(ValueError, match="zip"):
            rust_ranking._overfit_gate_via_rust(  # pyright: ignore[reportPrivateUsage]
                candidates, list(candidates), OverfitGateConfig(dsr_min=0.0), kernels
            )

    def test_clean_on_arm_run_emits_no_refusal_fallback_or_autorevert(self) -> None:
        # A clean full-pipeline ON-arm run (gates → overfit DSR → terms →
        # dsr_z → composite → order) must not refuse a single kernel result
        # (kills the validate→None mutants: a refusal ships the oracle, so
        # only THIS log assertion can see it) nor fall back — on ANY host.
        # The autorevert clause rides the pow-probe law (R12.1): this pool's
        # z-score columns have genuine variance, so its (b*) bit-agreement
        # is HOST-dependent — a burn-in family may never autorevert, and a
        # (b*) autorevert is legal only as a loud divergence on a
        # non-identity host, with the wire pinned byte-identical to the OFF
        # arm below (ship-the-oracle). The dsr-column z-arithmetic cannot be
        # made exact-by-construction (DSR values are arbitrary CDF outputs),
        # so a `_exact_pool()`-style unconditional claim is unavailable here.
        pool = [
            (0b0001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
            (0b0010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
            (0b0100, _m(sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8)),
            (0b1000, _m(sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5)),
        ]
        cfg = OverfitGateConfig(dsr_min=0.95)
        with capture_logs() as logs:
            on = rank_combos_via_rust(pool, overfit=cfg)
        assert on is not None
        assert len(on) > 0  # the dsr_z arm genuinely ran (kept non-empty)
        assert _keys(on) == _keys(_rank_off(pool, overfit=cfg))  # the wire law
        bad_events = {
            "rust_parity.kernel_result_refused",
            "rust_parity.fallback",
        }
        assert [e["event"] for e in logs if e["event"] in bad_events] == []
        _assert_reverts_are_expected_or_loud_b_star(logs)

    def test_rank_order_divergence_autoreverts_and_ships_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A WRONG-but-valid permutation from the kernel must be caught by
        # the _ORDER burn-in bit-compare (kills the key→None mutant, under
        # which the wrong order would ship) — the oracle order serves and
        # the family latches.
        pool = _exact_pool()  # distinct composites → a unique true order
        monkeypatch.setattr(
            fno_math,
            "ranking_rank_order",
            lambda comps, masks: sorted(range(len(comps)), key=lambda i: (comps[i], masks[i])),
        )
        with capture_logs() as logs:
            on = rank_combos_via_rust(pool)
        assert on is not None
        assert _keys(on) == _keys(_rank_off(pool))
        reverted = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
        assert [e["family"] for e in reverted] == ["ranking.order"]
