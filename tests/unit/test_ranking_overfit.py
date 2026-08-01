"""Unit matrix for :mod:`brutex.ranking.overfit` — the refusal + undeflatable arms.

The module's HAPPY paths already have homes: the ordering/threshold laws live in
``tests/golden/test_ranking_overfit_golden.py``, the invariants in
``tests/property/test_ranking_overfit_properties.py``, the flatness pin in
``tests/bench/test_ranking_overfit_o1.py``. What none of them drive are the
LOUD arms — the four ``__post_init__`` validators and the two
``deflated_sharpe_for`` ``None`` returns — because a golden corpus and a
property strategy both build *valid* configs by construction.

This file is the stem-matched home the touched-module coverage gate resolves
(``scripts/ci/coverage_gate_discovery.py`` maps ``brutex/ranking/overfit.py``
to ``tests/unit/test_ranking_overfit*.py``), so those arms are covered by the
file the gate actually measures rather than only by a sibling it cannot see.
"""

from __future__ import annotations

import math

import pytest
from brutex.ranking.overfit import (
    OverfitGateConfig,
    PoolDispersion,
    deflated_sharpe_for,
    passes_overfit_gate,
    pool_sharpe_dispersion,
)


class TestConfigValidation:
    """The four ``__post_init__`` refusals — each message quotes the INPUT value.

    Validation runs BEFORE normalisation precisely so the message reads back
    what the caller passed, not a coerced echo of it.
    """

    @pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf")])
    def test_dsr_min_outside_the_unit_interval_refuses(self, bad: float) -> None:
        with pytest.raises(ValueError, match=r"dsr_min must be a probability in \[0, 1\]"):
            OverfitGateConfig(dsr_min=bad)

    @pytest.mark.parametrize("edge", [0.0, 1.0])
    def test_dsr_min_boundaries_are_accepted(self, edge: float) -> None:
        # ``<=`` on both ends — the boundary is IN, not a hair outside it.
        assert OverfitGateConfig(dsr_min=edge).dsr_min == edge

    def test_negative_weight_refuses(self) -> None:
        with pytest.raises(ValueError, match=r"weight must be >= 0, got -1"):
            OverfitGateConfig(weight=-1.0)

    def test_zero_weight_is_accepted(self) -> None:
        assert OverfitGateConfig(weight=0.0).weight == 0.0

    @pytest.mark.parametrize("name", ["default_skew", "default_excess_kurtosis"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_gaussian_fallback_refuses(self, name: str, bad: float) -> None:
        with pytest.raises(ValueError, match=rf"{name} must be finite"):
            OverfitGateConfig(**{name: bad})


class TestConfigNormalisation:
    """DEC-RUST-STRICT-NO-FALLBACK-001 — every knob lands as an EXACT ``float``.

    The run-identity gate fragment routes through ``sweep_ops_rust``, whose FFI
    gate is ``type(value) is float`` — exactly, not ``isinstance``. Normalising
    here removes an ``int``/``bool`` knob from the crate's out-of-envelope
    class instead of holding its degrade out of the strict refusal.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"dsr_min": 1},
            {"weight": 0},
            {"default_skew": 0},
            {"default_excess_kurtosis": 0},
            {"dsr_min": True},  # bool IS an int subclass — normalised too.
        ],
    )
    def test_int_valued_knob_becomes_an_exact_float(self, kwargs: dict[str, object]) -> None:
        cfg = OverfitGateConfig(**kwargs)  # type: ignore[arg-type]
        for name in ("dsr_min", "weight", "default_skew", "default_excess_kurtosis"):
            assert type(getattr(cfg, name)) is float

    def test_already_float_values_are_unchanged(self) -> None:
        # The common path rewrites each value with itself — nothing observable
        # moves, so the digest this feeds is untouched.
        cfg = OverfitGateConfig(
            dsr_min=0.95, weight=0.05, default_skew=-0.25, default_excess_kurtosis=1.5
        )
        assert (cfg.dsr_min, cfg.weight, cfg.default_skew, cfg.default_excess_kurtosis) == (
            0.95,
            0.05,
            -0.25,
            1.5,
        )


class TestUndeflatable:
    """``deflated_sharpe_for`` returns ``None`` — never raises into the leaderboard."""

    _CFG = OverfitGateConfig()

    def _call(self, observed: float | None, sample_length: int = 100) -> float | None:
        return deflated_sharpe_for(
            observed,
            sample_length,
            None,
            None,
            n_trials=64,
            trial_sharpe_variance=0.25,
            cfg=self._CFG,
        )

    @pytest.mark.parametrize("observed", [None, float("nan"), float("inf"), float("-inf")])
    def test_absent_or_non_finite_sharpe_is_undeflatable(self, observed: float | None) -> None:
        # No measurable Sharpe to deflate — the gate treats None as a FAIL,
        # so this is the honest answer, not a fabricated probability.
        assert self._call(observed) is None

    @pytest.mark.parametrize("sample_length", [0, 1])
    def test_engine_value_error_is_caught_not_propagated(self, sample_length: int) -> None:
        # ``sample_length`` below 2 puts the PSR estimator variance out of
        # domain; the engine raises ValueError and this catches it — one
        # undeflatable combo must never abort the whole leaderboard.
        assert self._call(1.0, sample_length=sample_length) is None

    def test_a_well_posed_combo_still_deflates(self) -> None:
        # Anti-vacuous: the None arms above are reached by the REFUSAL, not
        # because this call shape always returns None.
        dsr = self._call(1.0)
        assert dsr is not None
        assert math.isfinite(dsr)


class TestPoolDispersion:
    """Abstain, never fabricate — the pool-side half of the same rule."""

    def test_none_and_non_finite_sharpes_are_excluded(self) -> None:
        # A combo with no measurable risk-adjusted edge is not a deflation
        # trial, so it must not inflate ``n_finite`` NOR the variance.
        pool = pool_sharpe_dispersion([1.0, None, float("nan"), 3.0, float("inf")])
        assert pool.n_finite == 2
        assert pool.variance == pytest.approx(1.0)  # pvariance([1.0, 3.0])

    @pytest.mark.parametrize("sharpes", [[], [1.0], [None, float("nan")]])
    def test_fewer_than_two_finite_sharpes_is_zero_variance(
        self, sharpes: list[float | None]
    ) -> None:
        pool = pool_sharpe_dispersion(sharpes)
        assert pool.variance == 0.0
        assert pool.deflatable is False

    def test_deflatable_needs_both_the_count_and_a_positive_variance(self) -> None:
        # Two finite Sharpes are necessary but NOT sufficient: an all-identical
        # pool has variance 0.0, so SR_0 would be 0 and the deflation would
        # correct nothing — the gate abstains rather than pretend.
        assert PoolDispersion(variance=0.25, n_finite=2).deflatable is True
        assert PoolDispersion(variance=0.0, n_finite=2).deflatable is False
        assert PoolDispersion(variance=0.25, n_finite=1).deflatable is False
        assert pool_sharpe_dispersion([2.0, 2.0]).deflatable is False


class TestGateThreshold:
    """``>=`` keeps the boundary; an undeflatable ``None`` DSR FAILS."""

    _CFG = OverfitGateConfig(dsr_min=0.95)

    def test_none_dsr_fails(self) -> None:
        assert passes_overfit_gate(None, self._CFG) is False

    @pytest.mark.parametrize(("dsr", "expected"), [(0.95, True), (0.96, True), (0.9499, False)])
    def test_threshold_boundary(self, dsr: float, expected: bool) -> None:
        assert passes_overfit_gate(dsr, self._CFG) is expected
