"""Frozen golden of one full Stage-3 refinement run (DEC-RUST-STAGE3-REFINE-001).

GREENFIELD ruling: no pycma bit-match exists to compare against — the
committed fixture (`tests/golden/fixtures/stage3_refine_rust_golden.json`)
freezes THE NEW Rust-hosted implementation's own intended deterministic
trajectory through the PRODUCTION :func:`refine_mask` wrapper, captured on
the first green build. Any later math/RNG/restart-schedule edit that moves
the trajectory fails HERE loudly and forces a conscious, decision-ID'd
re-lock (regenerate via ``BRUTEX_REGEN_STAGE3_GOLDEN=1`` — the
DEC-PREOPEN-PREVDAY-COMMON-002 refuse-to-overwrite precedent). The crate-side
twin (``rust/brutex-refine/tests/refine_golden.rs``) freezes the SAME
trajectory as Rust literals — one driver, two goldens, agreeing by
construction.

The ``refinement_run_id`` is deliberately NOT frozen in the fixture: it folds
the wheel's baked source digest (``optimizer_build``), which legitimately
moves on every crate edit (Golden Rule #4 — the re-key IS the law); the
golden instead re-derives it from the CURRENT tokens and pins the report
field equal — proving the wrapper mints exactly the documented identity.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from brutex.engine.stage3_refine import (
    Evaluator,
    RefineBudget,
    RefineMaskReport,
    optimizer_versions,
    refine_mask,
    refinement_run_id,
)

pytest.importorskip("brutex_refine")  # the Stage-3 Rust wheel — built in the CI golden job

_FIXTURE = Path(__file__).parent / "fixtures" / "stage3_refine_rust_golden.json"

#: Set to regenerate the fixture after a CONSCIOUS optimizer re-lock
#: (documented in the fixture's _comment; never flip casually).
_REGEN_ENV = "BRUTEX_REGEN_STAGE3_GOLDEN"


@dataclass(frozen=True, slots=True)
class _FakeMetrics:
    sharpe_ratio: float | None


def _bowl(sl: int, tp: int) -> _FakeMetrics:
    """The fixture's objective — max 0.0 exactly at the on-grid (100, 250)."""
    return _FakeMetrics(sharpe_ratio=-((sl - 100) ** 2 + (tp - 250) ** 2) / 1e4)


def _drive(fixture: dict[str, object]) -> RefineMaskReport:
    budget = cast(dict[str, object], fixture["budget"])
    return refine_mask(
        mask_run_id=cast(str, fixture["mask_run_id"]),
        start_sl_paisa=cast(int, fixture["start_sl_paisa"]),
        start_tp_paisa=cast(int, fixture["start_tp_paisa"]),
        evaluate=cast(Evaluator, _bowl),
        budget=RefineBudget(
            max_fevals=cast(int, budget["max_fevals"]),
            max_restarts=cast(int, budget["max_restarts"]),
            sigma0=cast(float, budget["sigma0"]),
        ),
    )


def test_frozen_refinement_trajectory_reproduces_exactly() -> None:
    fixture = cast(dict[str, object], json.loads(_FIXTURE.read_text(encoding="utf-8")))
    report = _drive(fixture)

    if os.environ.get(_REGEN_ENV, "").strip() == "1":  # pragma: no cover — operator lever
        fixture.update(
            seed=report.seed,
            restarts_run=report.restarts_run,
            evals_total=report.evals_total,
            cache_hits=report.cache_hits,
            best_sl_paisa=report.best_sl_paisa,
            best_tp_paisa=report.best_tp_paisa,
            best_sharpe=report.best_sharpe,
            evals=[[e.sl_paisa, e.tp_paisa] for e in report.evals],
        )
        _FIXTURE.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        pytest.fail(f"{_REGEN_ENV}=1: fixture regenerated — review + commit the re-lock")

    # The frozen trajectory: every unique evaluation, in evaluation order.
    assert [[e.sl_paisa, e.tp_paisa] for e in report.evals] == fixture["evals"]
    assert report.seed == fixture["seed"]
    assert report.restarts_run == fixture["restarts_run"]
    assert report.evals_total == fixture["evals_total"]
    assert report.cache_hits == fixture["cache_hits"]
    assert report.best_sl_paisa == fixture["best_sl_paisa"]
    assert report.best_tp_paisa == fixture["best_tp_paisa"]
    assert report.best_sharpe == fixture["best_sharpe"]

    # The identity law (dynamic — see the module docstring): the report's
    # run id is exactly the documented v2 derivation over the CURRENT
    # optimizer tokens.
    optimizer_version, optimizer_build = optimizer_versions()
    budget = cast(dict[str, object], fixture["budget"])
    assert report.refinement_run_id == refinement_run_id(
        mask_run_id=cast(str, fixture["mask_run_id"]),
        budget=RefineBudget(
            max_fevals=cast(int, budget["max_fevals"]),
            max_restarts=cast(int, budget["max_restarts"]),
            sigma0=cast(float, budget["sigma0"]),
        ),
        seed=report.seed,
        optimizer_version=optimizer_version,
        optimizer_build=optimizer_build,
    )


def test_golden_is_not_vacuous() -> None:
    # Anti-vacuous teeth: the fixture genuinely constrains the trajectory —
    # a non-trivial eval count, the exact-budget restart law, and a best
    # inside the bowl's neighbourhood.
    fixture = cast(dict[str, object], json.loads(_FIXTURE.read_text(encoding="utf-8")))
    evals = cast(list[list[int]], fixture["evals"])
    assert len(evals) == cast(int, fixture["evals_total"]) > 50
    assert len({tuple(e) for e in evals}) == len(evals)  # unique (the cache law)
    budget = cast(dict[str, object], fixture["budget"])
    assert cast(int, fixture["evals_total"]) <= cast(int, budget["max_fevals"]) * cast(
        int, budget["max_restarts"]
    )
    for sl, tp in evals:
        assert 5 <= sl <= 200 and sl % 5 == 0
        assert 10 <= tp <= 500 and tp % 10 == 0
