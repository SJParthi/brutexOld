"""Tests for `brutex.options.greeks` (DEC-GREEKS-ENGINE-PR2-001).

Phase-2 gate of the Greeks engine plan (`docs/GREEKS_ENGINE_PLAN.md` §11):
100% line + branch on the thin wrapper, the extreme-case matrix mirrored at
the Python boundary, the risk-free rate consumed ONLY through PR-1's
``RiskFreeRate`` seam, and the plan §12 property family (delta bounds /
vega ≥ 0 / put-call parity / strike monotonicity / IV round-trip) driven
through the wrapper with hypothesis.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
import struct
import sys
from datetime import date

import pytest
import trading_core
from brutex.options.greeks import (
    DELTA_ADMISSION_FLOOR,
    IV_CLAMP_MAX,
    IV_CLAMP_MIN,
    T_FLOOR_YEARS,
    VEGA_ADMISSION_FLOOR,
    GreeksBatchOutcome,
    GreeksInputError,
    GreeksResult,
    GreeksSolveRequest,
    compute_greeks,
    compute_greeks_batch,
)
from brutex.options.risk_free import RISK_FREE_SOURCE_VERSION, FixedRateSource, RiskFreeRate
from hypothesis import given
from hypothesis import strategies as st

F = 23_500.0
K = 23_500.0
T = 30.0 / 365.0
RATE = FixedRateSource().rate_on(date(2026, 7, 2))
ZERO_RATE = RiskFreeRate(rate=0.0, source="fixed", version=RISK_FREE_SOURCE_VERSION)


class TestSeamBasics:
    """The one shared seam: sigma path, solve path, precedence, stamping."""

    def test_sigma_path_prices_atm_call(self) -> None:
        out = compute_greeks(sigma=0.18, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert out is not None
        assert out.iv == 0.18
        assert 0.5 < out.delta < 0.6
        assert out.gamma > 0.0
        assert out.vega > 0.0
        assert out.theta < 0.0
        assert out.rho > 0.0
        assert out.price > 0.0
        assert out.admissible is True

    def test_solve_path_round_trips_the_sigma_path(self) -> None:
        priced = compute_greeks(
            sigma=0.22, forward=F, strike=23_600.0, t_years=T, rate=RATE, is_call=False
        )
        assert priced is not None
        solved = compute_greeks(
            price=priced.price, forward=F, strike=23_600.0, t_years=T, rate=RATE, is_call=False
        )
        assert solved is not None
        assert solved.iv == pytest.approx(0.22, rel=1e-12)

    def test_sigma_takes_precedence_over_price(self) -> None:
        with_both = compute_greeks(
            price=1.0, sigma=0.18, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
        )
        sigma_only = compute_greeks(
            sigma=0.18, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
        )
        assert with_both == sigma_only

    def test_rate_provenance_is_stamped(self) -> None:
        out = compute_greeks(sigma=0.2, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert out is not None
        assert out.rate_source == "fixed"
        assert out.rate_version == RISK_FREE_SOURCE_VERSION

    def test_result_is_frozen(self) -> None:
        out = compute_greeks(sigma=0.2, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert out is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            out.iv = 0.5  # type: ignore[misc]

    def test_result_type_and_constants_mirror_the_kernel(self) -> None:
        out = compute_greeks(sigma=0.2, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert isinstance(out, GreeksResult)
        assert pytest.approx(600.0 / (365.0 * 86_400.0)) == T_FLOOR_YEARS
        assert (IV_CLAMP_MIN, IV_CLAMP_MAX) == (0.001, 5.0)
        assert DELTA_ADMISSION_FLOOR == 0.02
        assert VEGA_ADMISSION_FLOOR == 1e-6

    def test_facade_surfaces_the_raw_kernel(self) -> None:
        """The auto-facade exposes the kernel as ``brutex.math.greeks`` (plan §10).

        Fresh-imported (the ``_fresh_real_facade`` pattern of
        ``tests/golden/test_math_facade_golden.py``) so a sibling suite's
        fake-kernel facade left in ``sys.modules`` can never leak in.
        """
        sys.modules.pop("brutex.math", None)
        facade = importlib.import_module("brutex.math")

        assert facade.greeks is trading_core.greeks


class TestHonestNoneFamily:
    """Well-formed but unsolvable inputs → honest ``None`` (plan §9), never a value."""

    def test_t_at_or_below_zero_is_none(self) -> None:
        for t_years in (0.0, -T):
            assert (
                compute_greeks(
                    sigma=0.2, forward=F, strike=K, t_years=t_years, rate=RATE, is_call=True
                )
                is None
            )

    def test_t_just_below_600s_floor_is_none_and_at_floor_prices(self) -> None:
        just_below = 599.0 / (365.0 * 86_400.0)
        assert (
            compute_greeks(
                sigma=0.2, forward=F, strike=K, t_years=just_below, rate=RATE, is_call=True
            )
            is None
        )
        at_floor = compute_greeks(
            sigma=0.2, forward=F, strike=K, t_years=T_FLOOR_YEARS, rate=RATE, is_call=True
        )
        assert at_floor is not None
        assert math.isfinite(at_floor.gamma)

    def test_price_below_intrinsic_is_none(self) -> None:
        # Deep ITM call: undiscounted intrinsic 1500; a 1000 premium violates no-arb.
        assert (
            compute_greeks(
                price=1_000.0, forward=25_000.0, strike=K, t_years=T, rate=RATE, is_call=True
            )
            is None
        )

    def test_price_above_upper_bound_is_none(self) -> None:
        assert (
            compute_greeks(price=F, forward=F, strike=K, t_years=T, rate=ZERO_RATE, is_call=True)
            is None
        )

    def test_price_outside_iv_clamp_is_none(self) -> None:
        low = compute_greeks(
            sigma=IV_CLAMP_MIN, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
        )
        assert low is not None
        # A premium below the sigma=0.001 price implies an out-of-clamp IV.
        assert (
            compute_greeks(
                price=low.price * 0.5, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
            )
            is None
        )


class TestLoudInputFamily:
    """Caller-contract violations raise ``GreeksInputError`` (never silent)."""

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf, 5e-324])
    def test_bad_forward_raises(self, bad: float) -> None:
        with pytest.raises(GreeksInputError, match="forward"):
            compute_greeks(sigma=0.2, forward=bad, strike=K, t_years=T, rate=RATE, is_call=True)

    @pytest.mark.parametrize("bad", [0.0, -2.0, math.nan, -math.inf, 5e-324])
    def test_bad_strike_raises(self, bad: float) -> None:
        with pytest.raises(GreeksInputError, match="strike"):
            compute_greeks(sigma=0.2, forward=F, strike=bad, t_years=T, rate=RATE, is_call=True)

    @pytest.mark.parametrize("bad", [0.0, -5.0, math.nan, math.inf, 5e-324])
    def test_bad_price_raises(self, bad: float) -> None:
        with pytest.raises(GreeksInputError, match="price"):
            compute_greeks(price=bad, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)

    @pytest.mark.parametrize("bad", [0.0009, 5.1, 0.0, -0.2, math.nan, math.inf])
    def test_sigma_outside_clamp_raises(self, bad: float) -> None:
        with pytest.raises(GreeksInputError, match="sigma"):
            compute_greeks(sigma=bad, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)

    def test_sigma_clamp_boundaries_are_valid_inputs(self) -> None:
        for boundary in (IV_CLAMP_MIN, IV_CLAMP_MAX):
            out = compute_greeks(
                sigma=boundary, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
            )
            assert out is not None
            assert out.iv == boundary

    def test_neither_price_nor_sigma_raises(self) -> None:
        with pytest.raises(GreeksInputError, match="need price or sigma"):
            compute_greeks(forward=F, strike=K, t_years=T, rate=RATE, is_call=True)

    def test_nonfinite_t_raises(self) -> None:
        with pytest.raises(GreeksInputError, match="t_years"):
            compute_greeks(
                sigma=0.2, forward=F, strike=K, t_years=math.nan, rate=RATE, is_call=True
            )

    def test_out_of_band_rate_raises(self) -> None:
        absurd = RiskFreeRate(rate=1.5, source="fixed", version=RISK_FREE_SOURCE_VERSION)
        with pytest.raises(GreeksInputError, match="rate"):
            compute_greeks(sigma=0.2, forward=F, strike=K, t_years=T, rate=absurd, is_call=True)

    def test_bare_float_rate_is_refused(self) -> None:
        """`r` is consumed ONLY through the PR-1 RiskFreeRate seam."""
        with pytest.raises(GreeksInputError, match="RiskFreeRate"):
            compute_greeks(
                sigma=0.2,
                forward=F,
                strike=K,
                t_years=T,
                rate=0.065,  # type: ignore[arg-type]
                is_call=True,
            )


class TestExtremeMatrix:
    """The extreme-case matrix (plan §9) mirrored at the wrapper boundary."""

    def test_expiry_minute_prices_cleanly(self) -> None:
        t_years = 650.0 / (365.0 * 86_400.0)
        out = compute_greeks(
            sigma=0.30, forward=F, strike=K, t_years=t_years, rate=RATE, is_call=True
        )
        assert out is not None
        assert math.isfinite(out.gamma)
        assert math.isfinite(out.theta)

    def test_deep_itm_call_delta_saturates(self) -> None:
        out = compute_greeks(
            sigma=0.15, forward=30_000.0, strike=K, t_years=T, rate=RATE, is_call=True
        )
        assert out is not None
        discount = math.exp(-RATE.rate * T)
        assert out.delta == pytest.approx(discount, abs=1e-6)

    def test_deep_otm_put_is_flagged_inadmissible(self) -> None:
        out = compute_greeks(
            sigma=0.12, forward=F, strike=15_000.0, t_years=T, rate=RATE, is_call=False
        )
        assert out is not None
        assert out.admissible is False
        assert -DELTA_ADMISSION_FLOOR < out.delta <= 0.0

    def test_penny_option_solves_honestly(self) -> None:
        """A one-tick deep-OTM premium either solves in-clamp or reads None."""
        out = compute_greeks(
            price=0.05, forward=F, strike=26_500.0, t_years=T, rate=RATE, is_call=True
        )
        if out is not None:
            assert IV_CLAMP_MIN <= out.iv <= IV_CLAMP_MAX
            assert out.admissible is False

    def test_exact_atm_matches_normalized_form(self) -> None:
        out = compute_greeks(sigma=0.2, forward=F, strike=F, t_years=T, rate=RATE, is_call=True)
        assert out is not None
        discount = math.exp(-RATE.rate * T)
        expected = discount * F * (2.0 * _norm_cdf(0.5 * 0.2 * math.sqrt(T)) - 1.0)
        assert out.price == pytest.approx(expected, rel=1e-9)


def _norm_cdf(x: float) -> float:
    """Reference normal CDF via ``math.erfc`` (independent of the kernel)."""
    return math.erfc(-x / math.sqrt(2.0)) / 2.0


_LIQUID_SIGMA = st.floats(min_value=0.01, max_value=2.0, allow_nan=False)
_LIQUID_FORWARD = st.floats(min_value=100.0, max_value=100_000.0, allow_nan=False)
_MONEYNESS = st.floats(min_value=0.5, max_value=1.5, allow_nan=False)
_TENOR = st.floats(min_value=T_FLOOR_YEARS, max_value=1.0, allow_nan=False)


class TestProperties:
    """Plan §12 property family, driven through the wrapper (hypothesis)."""

    @given(forward=_LIQUID_FORWARD, moneyness=_MONEYNESS, t_years=_TENOR, sigma=_LIQUID_SIGMA)
    def test_delta_bounds_and_vega_gamma_nonnegative(
        self, forward: float, moneyness: float, t_years: float, sigma: float
    ) -> None:
        strike = forward * moneyness
        call = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=True
        )
        put = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=False
        )
        assert call is not None
        assert put is not None
        assert 0.0 <= call.delta <= 1.0
        assert -1.0 <= put.delta <= 0.0
        assert call.vega >= 0.0
        assert call.gamma >= 0.0

    @given(forward=_LIQUID_FORWARD, moneyness=_MONEYNESS, t_years=_TENOR, sigma=_LIQUID_SIGMA)
    def test_put_call_parity_residual_is_noise(
        self, forward: float, moneyness: float, t_years: float, sigma: float
    ) -> None:
        strike = forward * moneyness
        call = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=True
        )
        put = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=False
        )
        assert call is not None
        assert put is not None
        discount = math.exp(-RATE.rate * t_years)
        residual = call.price - put.price - discount * (forward - strike)
        assert abs(residual) <= 1e-9 * forward

    @given(
        forward=_LIQUID_FORWARD,
        moneyness=_MONEYNESS,
        t_years=_TENOR,
        sigma=_LIQUID_SIGMA,
        bump=st.floats(min_value=1.001, max_value=1.25, allow_nan=False),
    )
    def test_call_price_monotone_decreasing_in_strike(
        self, forward: float, moneyness: float, t_years: float, sigma: float, bump: float
    ) -> None:
        strike = forward * moneyness
        low = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=True
        )
        high = compute_greeks(
            sigma=sigma,
            forward=forward,
            strike=strike * bump,
            t_years=t_years,
            rate=RATE,
            is_call=True,
        )
        assert low is not None
        assert high is not None
        assert high.price <= low.price + 1e-12 * forward

    @given(forward=_LIQUID_FORWARD, moneyness=_MONEYNESS, t_years=_TENOR, sigma=_LIQUID_SIGMA)
    def test_iv_round_trip_reprices_within_tolerance(
        self, forward: float, moneyness: float, t_years: float, sigma: float
    ) -> None:
        strike = forward * moneyness
        priced = compute_greeks(
            sigma=sigma, forward=forward, strike=strike, t_years=t_years, rate=RATE, is_call=True
        )
        assert priced is not None
        if priced.price < sys.float_info.min:
            return  # premium underflowed below f64-normal — nothing to invert
        solved = compute_greeks(
            price=priced.price,
            forward=forward,
            strike=strike,
            t_years=t_years,
            rate=RATE,
            is_call=True,
        )
        if solved is None:
            # An honest NA is allowed only for ill-conditioned tuples.
            assert priced.admissible is False
        else:
            assert solved.price == pytest.approx(priced.price, rel=1e-9)


def _result_bits(result: GreeksResult) -> tuple[bytes, bool, str, int]:
    """Bit-exact compare key for a solved result (signed zeros included)."""
    return (
        struct.pack(
            "<7d",
            result.iv,
            result.delta,
            result.gamma,
            result.theta,
            result.vega,
            result.rho,
            result.price,
        ),
        result.admissible,
        result.rate_source,
        result.rate_version,
    )


class TestBatchFace:
    """`compute_greeks_batch` ≡ per-element `compute_greeks` (DEC-GREEKS-RUST-BATCH-001)."""

    def test_outcome_refuses_both_set(self) -> None:
        solved = compute_greeks(sigma=0.18, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert solved is not None
        with pytest.raises(ValueError, match="BOTH a result and an invalid_input"):
            GreeksBatchOutcome(result=solved, invalid_input="invalid_input: nope")

    def test_outcome_allows_each_legal_shape(self) -> None:
        solved = compute_greeks(sigma=0.18, forward=F, strike=K, t_years=T, rate=RATE, is_call=True)
        assert solved is not None
        assert GreeksBatchOutcome(result=solved, invalid_input=None).result is solved
        both_none = GreeksBatchOutcome(result=None, invalid_input=None)
        assert both_none.result is None and both_none.invalid_input is None
        reject = GreeksBatchOutcome(result=None, invalid_input="invalid_input: x")
        assert reject.invalid_input == "invalid_input: x"

    @staticmethod
    def _matrix() -> list[GreeksSolveRequest]:
        """ok CE + ok PE + sigma-direct + no_solution + invalid_input rows."""
        return [
            GreeksSolveRequest(
                price=180.0, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
            ),
            GreeksSolveRequest(
                price=165.0, forward=F, strike=K, t_years=T, rate=RATE, is_call=False
            ),
            GreeksSolveRequest(
                price=None,
                forward=F,
                strike=23_000.0,
                t_years=T,
                rate=RATE,
                is_call=True,
                sigma=0.18,
            ),
            # no_solution: t below the 600 s floor.
            GreeksSolveRequest(
                price=50.0,
                forward=F,
                strike=K,
                t_years=1.0 / (365.0 * 86_400.0),
                rate=RATE,
                is_call=True,
            ),
            # no_solution: premium above the no-arb upper bound.
            GreeksSolveRequest(
                price=F * 2.0, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
            ),
            # invalid_input: non-positive forward.
            GreeksSolveRequest(
                price=100.0, forward=-1.0, strike=K, t_years=T, rate=RATE, is_call=True
            ),
            # invalid_input: neither price nor sigma.
            GreeksSolveRequest(price=None, forward=F, strike=K, t_years=T, rate=RATE, is_call=True),
        ]

    def test_per_element_parity_with_scalar_entry(self) -> None:
        """Each batch element bit-equals the scalar call on the same args."""
        requests = self._matrix()
        outcomes = compute_greeks_batch(requests)
        assert len(outcomes) == len(requests)
        for req, outcome in zip(requests, outcomes, strict=True):
            scalar_error: str | None = None
            scalar: GreeksResult | None = None
            try:
                scalar = compute_greeks(
                    price=req.price,
                    sigma=req.sigma,
                    forward=req.forward,
                    strike=req.strike,
                    t_years=req.t_years,
                    rate=req.rate,
                    is_call=req.is_call,
                )
            except GreeksInputError as exc:
                scalar_error = str(exc)
            if scalar_error is not None:
                assert outcome.result is None
                assert outcome.invalid_input == scalar_error
                continue
            if scalar is None:
                assert outcome.result is None
                assert outcome.invalid_input is None
            else:
                assert outcome.invalid_input is None
                assert outcome.result is not None
                assert _result_bits(outcome.result) == _result_bits(scalar)

    def test_matrix_genuinely_covers_all_three_families(self) -> None:
        outcomes = compute_greeks_batch(self._matrix())
        assert sum(1 for o in outcomes if o.result is not None) == 3
        assert sum(1 for o in outcomes if o.result is None and o.invalid_input is None) == 2
        rejects = [o for o in outcomes if o.invalid_input is not None]
        assert len(rejects) == 2
        assert all(
            o.invalid_input is not None and o.invalid_input.startswith("invalid_input")
            for o in rejects
        )

    def test_bare_float_rate_is_a_whole_call_loud_reject(self) -> None:
        good = GreeksSolveRequest(
            price=180.0, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
        )
        bad = GreeksSolveRequest(
            price=180.0,
            forward=F,
            strike=K,
            t_years=T,
            rate=0.065,  # type: ignore[arg-type]  # the exact dynamic-caller bug the seam guards
            is_call=True,
        )
        with pytest.raises(GreeksInputError, match="stamped RiskFreeRate"):
            compute_greeks_batch([good, bad])

    def test_empty_request_list_is_empty_outcomes(self) -> None:
        assert compute_greeks_batch([]) == []

    def test_short_kernel_return_fails_the_strict_zip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A length-lying kernel is refused by ``strict=True``, never truncated."""
        import brutex.options.greeks as greeks_mod

        def _short(*_args: object) -> list[object]:
            return []

        monkeypatch.setattr(greeks_mod.trading_core, "greeks_batch", _short)
        with pytest.raises(ValueError, match="zip"):
            compute_greeks_batch(self._matrix()[:1])

    def test_rate_provenance_stamped_per_request(self) -> None:
        """Each solved element carries ITS OWN request's rate provenance."""
        other = RiskFreeRate(rate=0.05, source="csv", version=RISK_FREE_SOURCE_VERSION)
        requests = [
            GreeksSolveRequest(
                price=180.0, forward=F, strike=K, t_years=T, rate=RATE, is_call=True
            ),
            GreeksSolveRequest(
                price=None,
                forward=F,
                strike=K,
                t_years=T,
                rate=other,
                is_call=True,
                sigma=0.2,
            ),
        ]
        outcomes = compute_greeks_batch(requests)
        assert outcomes[0].result is not None
        assert outcomes[0].result.rate_source == RATE.source
        assert outcomes[1].result is not None
        assert outcomes[1].result.rate_source == "csv"
        assert outcomes[1].result.rate_version == RISK_FREE_SOURCE_VERSION
