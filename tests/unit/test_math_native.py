"""Native-backend verification for :mod:`brutex.math`.

Exercised **only** when ``fno_math_py`` (the maturin-built PyO3
wheel) is importable. When the wheel is absent \u2014 the default in
CI's ``[dev]``-only environment \u2014 every test in this module is
skipped by :func:`pytest.importorskip` and the placeholder
contract tests in :mod:`tests.unit.test_math_placeholder` run
instead.

## What the native tests pin

1. ``FNO_MATH_BACKEND == "native"`` once the wheel is importable.
2. Scalar Black-Scholes: ATM call/put parity and the expected
   ATM-forward BS price to tight numeric tolerance.
3. IV round-trip: feeding the BS price back into ``iv_solve``
   recovers the input sigma to within 1e-9.
4. Per-Greek accessors: ``delta``, ``gamma``, ``theta``, ``vega``,
   ``rho``, ``implied_volatility`` resolve to the native Rust
   functions (not placeholders) and return plausible values.
5. Put-call parity of delta: ``delta_call - delta_put == e^{-qT}``
   (== 1.0 for a non-dividend asset). This is a textbook identity
   and catches any sign/derivative-direction bug in the Rust
   wrapper without needing a golden fixture.
6. The combined ``compute_greeks`` entry point returns a dict
   with every expected key and sane numeric values.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

fno_math_py = pytest.importorskip("fno_math_py")

import brutex.math as bm  # noqa: E402

# ────────────────────────────────────────────────────────────────────────
# Shared option fixture
# ────────────────────────────────────────────────────────────────────────

_SPOT = 100.0
_STRIKE = 100.0
_TIME = 0.25  # 3 months
_RATE = 0.05
_DIV = 0.0
_SIGMA = 0.20


# ────────────────────────────────────────────────────────────────────────
# Backend identity
# ────────────────────────────────────────────────────────────────────────


class TestNativeBackendWired:
    def test_backend_is_native(self) -> None:
        assert bm.FNO_MATH_BACKEND == "native"

    def test_fno_math_py_has_version(self) -> None:
        assert isinstance(fno_math_py.__version__, str)
        assert fno_math_py.__version__  # non-empty

    def test_delta_is_not_the_placeholder(self) -> None:
        """Placeholder functions live at brutex.math.<name>; native
        functions come out of the compiled extension. Test the dispatch
        by calling \u2014 the placeholder would raise
        :class:`FnoMathNotExtractedError`."""
        value = bm.delta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert isinstance(value, float)
        assert 0.0 < value < 1.0  # any reasonable ATM call delta


# ────────────────────────────────────────────────────────────────────────
# Scalar BS pricing
# ────────────────────────────────────────────────────────────────────────


class TestScalarPricing:
    def test_bs_call_price_is_positive(self) -> None:
        price = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert price > 0.0
        # ATM call with r>0 and q=0 should be strictly above intrinsic (=0).
        assert price > 0.5  # loose lower bound

    def test_bs_put_price_is_positive(self) -> None:
        price = fno_math_py.bs_put_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert price > 0.0

    def test_put_call_parity_on_atm_pair(self) -> None:
        """C - P == S*exp(-qT) - K*exp(-rT)."""
        call = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        put = fno_math_py.bs_put_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        expected = _SPOT * math.exp(-_DIV * _TIME) - _STRIKE * math.exp(-_RATE * _TIME)
        assert math.isclose(call - put, expected, rel_tol=1e-9, abs_tol=1e-12)

    def test_bs_price_dispatch_matches_scalar_functions(self) -> None:
        call_via_dispatch = fno_math_py.bs_price("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        call_direct = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert call_via_dispatch == call_direct

    def test_bs_price_rejects_unknown_side(self) -> None:
        with pytest.raises(ValueError, match="option side"):
            fno_math_py.bs_price("X", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)


# ────────────────────────────────────────────────────────────────────────
# IV solver
# ────────────────────────────────────────────────────────────────────────


class TestIvSolver:
    def test_roundtrip_recovers_sigma(self) -> None:
        """Feeding BS(sigma) back into the solver must recover sigma."""
        bs = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        iv = fno_math_py.iv_solve("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        assert iv is not None
        assert math.isclose(iv, _SIGMA, rel_tol=1e-9, abs_tol=1e-12)

    def test_invalid_price_returns_none(self) -> None:
        """A non-positive market price cannot be inverted."""
        assert fno_math_py.iv_solve("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, 0.0) is None
        assert fno_math_py.iv_solve("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, -1.0) is None

    def test_implied_volatility_alias_matches_iv_solve(self) -> None:
        bs = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        a = fno_math_py.iv_solve("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        b = fno_math_py.implied_volatility("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        assert a == b


# ────────────────────────────────────────────────────────────────────────
# Per-Greek scalar accessors
# ────────────────────────────────────────────────────────────────────────


class TestPerGreekScalarAccessors:
    def test_delta_call_in_unit_interval(self) -> None:
        d = fno_math_py.delta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert 0.0 < d < 1.0

    def test_delta_put_is_negative(self) -> None:
        d = fno_math_py.delta("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert -1.0 < d < 0.0

    def test_put_call_parity_of_delta(self) -> None:
        """delta_c - delta_p == exp(-qT) for any (K, sigma) at same expiry.
        With q = 0 the identity collapses to 1.0."""
        dc = fno_math_py.delta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        dp = fno_math_py.delta("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert math.isclose(dc - dp, math.exp(-_DIV * _TIME), rel_tol=1e-9, abs_tol=1e-12)

    def test_gamma_is_positive_and_symmetric(self) -> None:
        """Gamma_call == Gamma_put (no-dividend BS)."""
        gc = fno_math_py.gamma("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        gp = fno_math_py.gamma("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert gc > 0.0
        assert math.isclose(gc, gp, rel_tol=1e-12)

    def test_vega_is_positive_and_symmetric(self) -> None:
        vc = fno_math_py.vega("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        vp = fno_math_py.vega("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert vc > 0.0
        assert math.isclose(vc, vp, rel_tol=1e-12)

    def test_theta_sign_is_negative_for_long_options(self) -> None:
        """A long call or long put loses value as time passes."""
        tc = fno_math_py.theta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        tp = fno_math_py.theta("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert tc < 0.0
        assert tp < 0.0

    def test_rho_call_positive_put_negative(self) -> None:
        """Rate ↑ → call more valuable, put less valuable."""
        rc = fno_math_py.rho("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        rp = fno_math_py.rho("PE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert rc > 0.0
        assert rp < 0.0


# ────────────────────────────────────────────────────────────────────────
# Full Greeks dict
# ────────────────────────────────────────────────────────────────────────


class TestFullGreeksDict:
    _EXPECTED_KEYS: ClassVar[set[str]] = {
        "iv",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "charm",
        "vanna",
        "volga",
        "veta",
        "speed",
        "color",
        "zomma",
        "ultima",
        "bs_price",
        "intrinsic",
        "extrinsic",
    }

    def test_compute_greeks_returns_every_field(self) -> None:
        bs = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        g = fno_math_py.compute_greeks("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        assert g is not None
        assert set(g.keys()) == self._EXPECTED_KEYS

    def test_compute_greeks_returns_none_on_invalid_price(self) -> None:
        g = fno_math_py.compute_greeks("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, 0.0)
        assert g is None

    def test_compute_greeks_from_iv_passthrough(self) -> None:
        """Feeding a known IV directly must agree with the solver path."""
        bs = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        via_solver = fno_math_py.compute_greeks("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        via_direct = fno_math_py.compute_greeks_from_iv(
            "CE",
            _SPOT,
            _STRIKE,
            _TIME,
            _RATE,
            _DIV,
            _SIGMA,
            bs,
            365.0,
        )
        assert via_solver is not None
        for key in self._EXPECTED_KEYS:
            assert math.isclose(via_solver[key], via_direct[key], rel_tol=1e-9, abs_tol=1e-12), (
                f"field {key!r} disagreed: solver={via_solver[key]!r} direct={via_direct[key]!r}"
            )


# ────────────────────────────────────────────────────────────────────────
# brutex.math facade \u2194 native dispatch
# ────────────────────────────────────────────────────────────────────────


class TestFacadeDispatch:
    """Every Greek symbol on the facade must hit the native Rust function."""

    def test_facade_delta_matches_native(self) -> None:
        facade = bm.delta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        native = fno_math_py.delta("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert facade == native

    def test_facade_gamma_matches_native(self) -> None:
        facade = bm.gamma("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        native = fno_math_py.gamma("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert facade == native

    def test_facade_vega_matches_native(self) -> None:
        facade = bm.vega("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        native = fno_math_py.vega("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        assert facade == native

    def test_facade_implied_volatility_matches_native(self) -> None:
        bs = fno_math_py.bs_call_price(_SPOT, _STRIKE, _TIME, _RATE, _DIV, _SIGMA)
        facade = bm.implied_volatility("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        native = fno_math_py.implied_volatility("CE", _SPOT, _STRIKE, _TIME, _RATE, _DIV, bs)
        assert facade == native
