"""Unit tests for the Stage-3 refinement runner (DEC-STAGE3-REFINE-001, ADR-0007 slice 4).

100% line+branch with a SYNTHETIC evaluator (no engine, no bars): the A/B
determinism oracle (same seed + config ⇒ byte-identical run, the ADR-0007
condition-5 envelope), the ≤ fevals×restarts hard budget (condition 8), the
quantize-before-eval guarantee (condition 3 — the evaluator only ever sees
on-grid ints), the run-scoped value cache (re-proposed grid points never
re-simulate), every-eval surfacing (condition 6 — the DSR-N contribution),
the seed/identity derivations (conditions 4/5), and the three-line
STAGE_3_REFINE audit pattern (§4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest
from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS
from brutex.engine.stage3_refine import (
    STAGE3_ACTION_COMPLETE,
    STAGE3_ACTION_FLUSH,
    STAGE3_ACTION_START,
    Evaluator,
    RefineBudget,
    Stage3RefineError,
    optimizer_versions,
    refine_mask,
    refinement_run_id,
    seed_from_mask_run_id,
)

pytest.importorskip("cma")  # the optimize extra — installed in the CI unit job

#: A valid 64-char BLAKE3-shaped mask run identity (deterministic test fixture).
_MASK_RUN_ID = "ab" * 32


@dataclass(frozen=True, slots=True)
class _FakeMetrics:
    """The single field the runner projects (the pool-independent objective)."""

    sharpe_ratio: float | None


@dataclass
class _BowlEvaluator:
    """A smooth synthetic objective peaked at a known grid point.

    Sharpe = -((sl-100)² + (tp-250)²)/10⁴ — maximum 0.0 exactly at the on-grid
    point (100, 250). Records every call so the tests can assert quantization
    (ints only), uniqueness (the cache prevents re-evaluation), and ordering.
    """

    calls: list[tuple[int, int]] = field(default_factory=list)

    def __call__(self, sl: int, tp: int) -> _FakeMetrics:
        assert type(sl) is int and type(tp) is int  # condition 3: never floats
        self.calls.append((sl, tp))
        return _FakeMetrics(sharpe_ratio=-((sl - 100) ** 2 + (tp - 250) ** 2) / 1e4)


def _run(
    evaluator: _BowlEvaluator | None = None,
    *,
    budget: RefineBudget | None = None,
    audit: object = None,
) -> tuple[_BowlEvaluator, object]:
    ev = evaluator if evaluator is not None else _BowlEvaluator()
    report = refine_mask(
        mask_run_id=_MASK_RUN_ID,
        start_sl_paisa=50,
        start_tp_paisa=100,
        evaluate=cast(Evaluator, ev),
        budget=budget,
        audit=audit,  # type: ignore[arg-type]
    )
    return ev, report


class TestDeterminism:
    """The A/B oracle — ADR-0007 condition 5's same-process envelope."""

    def test_same_seed_and_budget_reproduce_the_run_exactly(self) -> None:
        ev_a, report_a = _run()
        ev_b, report_b = _run()
        assert report_a == report_b  # identity, evals (order included), best, counts
        assert ev_a.calls == ev_b.calls  # the literal evaluation sequence

    def test_seed_derives_from_the_mask_identity_low_32_bits(self) -> None:
        # numpy MT19937 caps seeds at 2**32-1 — the ADR's 64-bit derivation is
        # narrowed to 32 (recorded in DEC-STAGE3-REFINE-001); still fully derived.
        assert seed_from_mask_run_id(_MASK_RUN_ID) == int(_MASK_RUN_ID[-8:], 16)
        assert seed_from_mask_run_id(_MASK_RUN_ID) < 2**32
        other = "cd" * 32
        assert seed_from_mask_run_id(other) != seed_from_mask_run_id(_MASK_RUN_ID)

    def test_identity_changes_with_seed_budget_and_versions(self) -> None:
        pycma_v, numpy_v = optimizer_versions()
        base = refinement_run_id(
            mask_run_id=_MASK_RUN_ID,
            budget=RefineBudget(),
            seed=7,
            pycma_version=pycma_v,
            numpy_version=numpy_v,
        )
        assert base != refinement_run_id(
            mask_run_id=_MASK_RUN_ID,
            budget=RefineBudget(),
            seed=8,
            pycma_version=pycma_v,
            numpy_version=numpy_v,
        )
        assert base != refinement_run_id(
            mask_run_id=_MASK_RUN_ID,
            budget=RefineBudget(max_fevals=100),
            seed=7,
            pycma_version=pycma_v,
            numpy_version=numpy_v,
        )
        assert base != refinement_run_id(
            mask_run_id=_MASK_RUN_ID,
            budget=RefineBudget(),
            seed=7,
            pycma_version="9.9.9",  # a bumped optimizer is a NEW identity
            numpy_version=numpy_v,
        )


class TestBudgetAndCache:
    """Condition 8's hard cap + condition 4's run-scoped value cache."""

    def test_unique_evals_bounded_by_fevals_times_restarts(self) -> None:
        budget = RefineBudget(max_fevals=40, max_restarts=2)
        ev, report = _run(budget=budget)
        assert report.restarts_run == 2  # type: ignore[attr-defined]
        assert report.evals_total <= 40 * 2  # type: ignore[attr-defined]
        assert report.evals_total == len(ev.calls)  # type: ignore[attr-defined]

    def test_cache_answers_reproposed_points_without_reevaluating(self) -> None:
        ev, report = _run()  # 600-ask default budget over a 40×50 grid → hits
        assert len(set(ev.calls)) == len(ev.calls)  # NEVER re-evaluated
        assert report.cache_hits > 0  # type: ignore[attr-defined]
        assert report.evals_total == len(ev.calls)  # type: ignore[attr-defined]

    def test_every_unique_eval_is_surfaced_for_the_pool(self) -> None:
        # Condition 6: the caller gets EVERY trial, in evaluation order.
        ev, report = _run(budget=RefineBudget(max_fevals=30, max_restarts=1))
        assert [(e.sl_paisa, e.tp_paisa) for e in report.evals] == ev.calls  # type: ignore[attr-defined]

    def test_finds_the_known_optimum_region(self) -> None:
        _, report = _run()
        # The bowl peaks exactly at the on-grid (100, 250); the deterministic
        # 600-eval run must land within a few ticks (no exact-coordinate pin —
        # that would couple the test to pycma internals, not our contract).
        assert abs(report.best_sl_paisa - 100) <= 3 * MODE_A_SL_AXIS.tick_paisa  # type: ignore[attr-defined]
        assert abs(report.best_tp_paisa - 250) <= 3 * MODE_A_TP_AXIS.tick_paisa  # type: ignore[attr-defined]
        assert report.best_sharpe <= 0.0  # type: ignore[attr-defined]

    def test_none_sharpe_projects_to_zero(self) -> None:
        class _NoneEvaluator:
            def __call__(self, sl: int, tp: int) -> _FakeMetrics:
                return _FakeMetrics(sharpe_ratio=None)

        report = refine_mask(
            mask_run_id=_MASK_RUN_ID,
            start_sl_paisa=50,
            start_tp_paisa=100,
            evaluate=cast(Evaluator, _NoneEvaluator()),
            budget=RefineBudget(max_fevals=10, max_restarts=1),
        )
        assert report.best_sharpe == 0.0


class TestAuditPattern:
    """The three-line START/FLUSH/COMPLETE chain (ADR-0007 §4)."""

    def test_emits_start_flush_complete_in_order(self) -> None:
        lines: list[tuple[str, str, dict[str, object]]] = []

        class _Audit:
            def append(self, *, action: str, subject: str, payload: dict[str, object]) -> None:
                lines.append((action, subject, payload))

        _, report = _run(budget=RefineBudget(max_fevals=10, max_restarts=1), audit=_Audit())
        assert [line[0] for line in lines] == [
            STAGE3_ACTION_START,
            STAGE3_ACTION_FLUSH,
            STAGE3_ACTION_COMPLETE,
        ]
        start, flush, complete = lines
        assert start[2]["mask_run_id"] == _MASK_RUN_ID
        assert start[2]["refinement_run_id"] == report.refinement_run_id  # type: ignore[attr-defined]
        assert flush[2]["evals_total"] == report.evals_total  # type: ignore[attr-defined]
        assert complete[2]["best_sl_paisa"] == report.best_sl_paisa  # type: ignore[attr-defined]
        assert start[1] == flush[1] == complete[1]  # one subject per refinement run

    def test_no_audit_sink_is_a_clean_noop(self) -> None:
        _, report = _run(budget=RefineBudget(max_fevals=10, max_restarts=1))
        assert report.evals_total > 0  # type: ignore[attr-defined]


class TestGuards:
    """Fail-fast configuration + identity guards."""

    def test_bad_mask_run_id_length_rejected(self) -> None:
        with pytest.raises(Stage3RefineError, match="64-char"):
            seed_from_mask_run_id("abc")

    def test_refinement_run_id_rejects_unit_separator_in_mask_run_id(self) -> None:
        """A ``\\x1f``-carrying mask_run_id could forge another identity's payload
        (DEC-RUN-IDENTITY-GUARD-PARITY-001) — rejected before any digest byte."""
        with pytest.raises(Stage3RefineError, match="mask_run_id"):
            refinement_run_id(
                mask_run_id="ab" * 15 + "\x1f" + "ab" * 16,
                budget=RefineBudget(),
                seed=7,
                pycma_version="4.0.0",
                numpy_version="2.1.0",
            )

    def test_refinement_run_id_rejects_unit_separator_in_pycma_version(self) -> None:
        """The injectable pycma_version token rides the same reject-only guard."""
        with pytest.raises(Stage3RefineError, match="pycma_version"):
            refinement_run_id(
                mask_run_id=_MASK_RUN_ID,
                budget=RefineBudget(),
                seed=7,
                pycma_version="4.0.0\x1fX",
                numpy_version="2.1.0",
            )

    def test_refinement_run_id_rejects_unit_separator_in_numpy_version(self) -> None:
        """The injectable numpy_version token rides the same reject-only guard."""
        with pytest.raises(Stage3RefineError, match="numpy_version"):
            refinement_run_id(
                mask_run_id=_MASK_RUN_ID,
                budget=RefineBudget(),
                seed=7,
                pycma_version="4.0.0",
                numpy_version="2.1.0\x1fX",
            )

    def test_budget_guards(self) -> None:
        with pytest.raises(Stage3RefineError, match="max_fevals"):
            RefineBudget(max_fevals=0)
        with pytest.raises(Stage3RefineError, match="max_restarts"):
            RefineBudget(max_restarts=0)
        with pytest.raises(Stage3RefineError, match="sigma0"):
            RefineBudget(sigma0=0.0)

    def test_off_grid_start_rejected_fail_fast(self) -> None:
        from brutex.engine.stage3_grid import Stage3GridError

        with pytest.raises(Stage3GridError):
            refine_mask(
                mask_run_id=_MASK_RUN_ID,
                start_sl_paisa=51,  # off the 5-paisa tick
                start_tp_paisa=100,
                evaluate=cast(Evaluator, _BowlEvaluator()),
            )

    def test_budget_canonical_is_deterministic(self) -> None:
        assert RefineBudget().canonical() == RefineBudget().canonical()
        assert RefineBudget(max_fevals=10).canonical() != RefineBudget().canonical()
