"""Committed independent Black-76 cross-check of the Greeks value chain.

Lifts the DEC-GREEKS-CORRECTNESS-AUDIT-001 independent-reference lens
(``scratchpad/black76-crosscheck/`` — black76_ref.py + grid_a.py + grid_b.py)
into the permanent suite: the production chain (``trading_core`` Rust kernel →
``brutex.options.greeks`` → ``brutex.data.greeks_enrich``) is numerically
cross-checked against a REFERENCE implementation embedded below.

INDEPENDENCE CONTRACT (the whole point of this file): the reference is built
from the literature —

* Black, F. (1976). "The pricing of commodity contracts",
  *Journal of Financial Economics* 3(1-2):167-179 — the futures-option
  pricing formulas: with discount factor ``e^{-rT}`` on the forward ``F``,
  ``d1 = [ln(F/K) + σ²T/2] / (σ√T)``, ``d2 = d1 − σ√T``,
  ``call = e^{-rT}(F·N(d1) − K·N(d2))``,
  ``put  = e^{-rT}(K·N(−d2) − F·N(−d1))``

— and MUST NEVER import, call, or copy the production math
(``trading_core``'s Rust kernel, ``brutex.options.greeks``, or any
``brutex.indicators``/``brutex.math`` kernel path). It is pure stdlib
(``math.erfc``). The QUOTE CONVENTIONS below were matched to the production
kernel's header documentation (``rust/trading-core/src/greeks/mod.rs`` — read
for CONVENTIONS only, all formulas re-derived by hand from the Black-76
price):

* ``delta = df·N(d1)`` CE, ``−df·N(−d1)`` PE (per unit of F);
* ``gamma = df·φ(d1) / (F·σ·√T)`` (per unit of F, both sides);
* ``vega  = F·df·φ(d1)·√T / 100`` (per 1 vol-point);
* ``theta = [r·price − df·F·φ(d1)·σ/(2√T)] / 365`` (per Act/365 calendar
  day; hand-derived as ``−dV/dT`` for BOTH sides — put-call parity
  ``put = call − df·(F−K)`` yields the identical form with the side's own
  price);
* ``rho = K·T·df·N(d2)/100`` CE, ``−K·T·df·N(−d2)/100`` PE (the BSM-form rho
  the kernel documents — NOT the pure-Black ``−T·price``).

TOLERANCES (each anchored to the audited full-grid MEASURED maxima — the
3,822-point sigma path / 3,321-point solve path run of the lens, re-measured
on this exact wheel; Grid A below IS that grid, so every ceiling has known
empirical headroom):

* sigma path, meaningful magnitudes (``max|·| ≥ 1e-8``): rel ``≤ 1e-11``
  (measured worst ~5e-13; ZERO audited deviations above 1e-11 — the ceiling
  leaves ≥20x headroom for cross-platform libm ulp drift);
* sigma path, underflow tail (``max|·| < 1e-8`` — where ``φ(d1)`` /
  the discounted price underflow toward subnormals and relative error
  inflates from quantization alone): rel ``≤ 5e-9`` (measured worst
  7.75e-10 on ``price`` at ~3e-254 magnitude; Greeks worst ~1.9e-12), with
  both-below-``1e-280`` treated as agreeing (both sides underflow to
  zero-adjacent noise);
* solve path round-trip on the REFERENCE pricer: rel ``≤ 1e-8`` (the kernel
  verifies its root at 1e-9 relative on ITS OWN pricer; the audited run had
  ZERO round-trip failures above 1e-8 — the "no silent wrong roots" oracle);
* generating-sigma recovery, well-conditioned (raw vega ``> 1e-3·F``):
  ``≤ 5e-9`` for ``σ√T ≤ 2.5`` (audited failures beyond 1e-9 all sit at
  ``σ√T ≥ 3``), ``≤ 1e-6`` beyond (measured worst 7.53e-8, confined to the
  documented Jäckel hard band ``σ√T > 6`` family — 13x headroom);
* Grid-B stored overlay vs the reference closed form: rel ``≤ 5e-12`` at the
  chains' meaningful magnitudes; stored-IV reprice of the stored close
  ``≤ 2e-9`` (kernel 1e-9 verification + ~1e-12 reference↔kernel pricer
  drift, doubled).
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

import trading_core
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    Q_BIAS_ZERO,
    RATE_SOURCE_ID_FIXED,
    STATUS_BELOW_INTRINSIC,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    TTE_ACT365_EXPIRY_1530_IST,
    enrich_option_bars,
    unpack_greeks_provenance,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.options.risk_free import FixedRateSource

# --------------------------------------------------------------------------
# The INDEPENDENT reference (pure stdlib — see the module-docstring contract).
# --------------------------------------------------------------------------

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)
_SECONDS_PER_YEAR = 365.0 * 86400.0  # Act/365 — hand-derived, not imported


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc: ``N(x) = erfc(−x/√2)/2``."""
    return 0.5 * math.erfc(-x / _SQRT2)


def _norm_pdf(x: float) -> float:
    """Standard normal density ``φ(x)``."""
    return math.exp(-0.5 * x * x) * _INV_SQRT_2PI


def _d1_d2(sigma: float, forward: float, strike: float, t_years: float) -> tuple[float, float]:
    """Black-76 ``(d1, d2)`` — plain ``log(F/K)``, no production tricks."""
    sig_sqrt_t = sigma * math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * t_years) / sig_sqrt_t
    return d1, d1 - sig_sqrt_t


def _ref_price(
    sigma: float, forward: float, strike: float, t_years: float, rate: float, is_call: bool
) -> float:
    """Discounted Black-76 price (Black 1976)."""
    df = math.exp(-rate * t_years)
    d1, d2 = _d1_d2(sigma, forward, strike, t_years)
    if is_call:
        return df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def _ref_greeks(
    sigma: float, forward: float, strike: float, t_years: float, rate: float, is_call: bool
) -> dict[str, float]:
    """Price + the six production-convention Greeks (module-docstring forms)."""
    df = math.exp(-rate * t_years)
    sqrt_t = math.sqrt(t_years)
    d1, d2 = _d1_d2(sigma, forward, strike, t_years)
    pdf1 = _norm_pdf(d1)
    if is_call:
        price = df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
        delta = df * _norm_cdf(d1)
        rho = strike * t_years * df * _norm_cdf(d2) / 100.0
    else:
        price = df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))
        delta = -df * _norm_cdf(-d1)
        rho = -strike * t_years * df * _norm_cdf(-d2) / 100.0
    gamma = df * pdf1 / (forward * sigma * sqrt_t)
    vega = forward * df * pdf1 * sqrt_t / 100.0
    theta = (rate * price - df * forward * pdf1 * sigma / (2.0 * sqrt_t)) / 365.0
    return {
        "iv": sigma,
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def _ref_iv_bisect(
    target_price: float,
    forward: float,
    strike: float,
    t_years: float,
    rate: float,
    is_call: bool,
    lo: float = 0.001,
    hi: float = 5.0,
    iters: int = 200,
) -> float | None:
    """Independent plain bisection on the discounted price over ``[lo, hi]``.

    Returns ``None`` when the target sits outside ``[price(lo), price(hi)]``
    (no in-clamp root). 200 halvings shrink the bracket far below one ulp of
    any representable sigma, so the result is machine-precise.
    """
    p_lo = _ref_price(lo, forward, strike, t_years, rate, is_call)
    p_hi = _ref_price(hi, forward, strike, t_years, rate, is_call)
    if target_price < p_lo or target_price > p_hi:
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if mid in (lo, hi):  # bracket collapsed to adjacent floats
            break
        if _ref_price(mid, forward, strike, t_years, rate, is_call) < target_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _rel_err(a: float, b: float, abs_floor: float = 0.0) -> float:
    """``|a−b| / max(|a|, |b|, abs_floor)``; 0 when exactly equal."""
    if a == b:
        return 0.0
    denom = max(abs(a), abs(b), abs_floor)
    if denom == 0.0:
        return 0.0
    return abs(a - b) / denom


def _expiry_utc_independent(expiry: date) -> datetime:
    """15:30 IST = 10:00 UTC same calendar day (IST = UTC+5:30, no DST).

    Hand-derived — deliberately NOT ``greeks_enrich.expiry_instant_utc``.
    """
    return datetime(expiry.year, expiry.month, expiry.day, 10, 0, tzinfo=UTC)


def _t_years_independent(ts: datetime, expiry: date) -> float:
    """Act/365 years from the bar-label instant to 15:30 IST expiry."""
    return (_expiry_utc_independent(expiry) - ts).total_seconds() / _SECONDS_PER_YEAR


class TestReferenceSelfConsistency:
    """The reference must invert itself before it may judge the kernel."""

    def test_bisection_inverts_the_reference_pricer(self) -> None:
        for sigma in (0.05, 0.18, 0.9, 3.2):
            for is_call in (True, False):
                premium = _ref_price(sigma, 23500.0, 24000.0, 30.0 / 365.0, 0.065, is_call)
                recovered = _ref_iv_bisect(premium, 23500.0, 24000.0, 30.0 / 365.0, 0.065, is_call)
                assert recovered is not None
                assert _rel_err(recovered, sigma) <= 1e-12


# --------------------------------------------------------------------------
# Grid A — the exact audited adversarial grid vs trading_core.greeks.
# 13 moneyness x 7 tenors (601 s .. 2 y) x 7 sigmas (0.005 .. 4.9) x
# 3 rates {0, 0.065, 0.1} x both sides = 3,822 points; measured ~0.1 s total.
# --------------------------------------------------------------------------

_GRID_FORWARD = 23_500.0
_MONEYNESS = (0.5, 0.7, 0.85, 0.95, 0.99, 1.0, 1.01, 1.05, 1.15, 1.5, 2.0, 0.2, 5.0)  # F/K
_T_GRID = (
    601.0 / _SECONDS_PER_YEAR,  # one second above the kernel's 600 s floor
    3600.0 / _SECONDS_PER_YEAR,
    86400.0 / _SECONDS_PER_YEAR,
    7.0 * 86400.0 / _SECONDS_PER_YEAR,
    30.0 / 365.0,
    1.0,
    2.0,
)
_SIGMA_GRID = (0.005, 0.05, 0.18, 0.5, 1.0, 3.0, 4.9)  # inside the [0.001, 5.0] clamp
_R_GRID = (0.0, 0.065, 0.1)
_OUTPUT_NAMES = ("iv", "delta", "gamma", "theta", "vega", "rho", "price")

#: Sigma-path tolerance bands (see the module docstring for the measured
#: anchors): tight where magnitudes are meaningful, looser ONLY where φ(d1)
#: underflow / subnormal quantization inflates relative error.
_MEANINGFUL_ABS = 1e-8
_TOL_MEANINGFUL = 1e-11
_TOL_UNDERFLOW_TAIL = 5e-9
_BOTH_ZERO_BAND = 1e-280
_TOL_ROUNDTRIP = 1e-8
_TOL_RECOVERY_SHARP = 5e-9  # sigma*sqrt(T) <= 2.5
_TOL_RECOVERY_HARD_BAND = 1e-6  # beyond — the documented Jäckel hard band family


def _grid_points() -> list[tuple[float, float, float, float, bool]]:
    """The deterministic (strike, t_years, sigma, rate, is_call) grid."""
    return [
        (_GRID_FORWARD / m, t_years, sigma, rate, is_call)
        for m in _MONEYNESS
        for t_years in _T_GRID
        for sigma in _SIGMA_GRID
        for rate in _R_GRID
        for is_call in (True, False)
    ]


def _kernel_outputs(
    price: float | None,
    sigma: float | None,
    forward: float,
    strike: float,
    t_years: float,
    rate: float,
    is_call: bool,
) -> dict[str, float]:
    """One raw kernel call unpacked by name (raises ValueError like the kernel)."""
    iv, delta, gamma, theta, vega, rho, model_price, _admissible = trading_core.greeks(
        price, sigma, forward, strike, t_years, rate, is_call
    )
    return {
        "iv": iv,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
        "price": model_price,
    }


def _sigma_path_deviation(got: float, expected: float) -> float | None:
    """The banded sigma-path comparison — ``None`` = within tolerance."""
    if abs(got) < _BOTH_ZERO_BAND and abs(expected) < _BOTH_ZERO_BAND:
        return None
    err = _rel_err(got, expected, abs_floor=1e-300)
    tol = (
        _TOL_MEANINGFUL if max(abs(got), abs(expected)) >= _MEANINGFUL_ABS else _TOL_UNDERFLOW_TAIL
    )
    return err if err > tol else None


class TestGridAKernelVsReference:
    """The audited 3,822-point kernel↔reference grid, committed as a gate."""

    def test_sigma_path_matches_reference_everywhere(self) -> None:
        points = _grid_points()
        assert len(points) == 3822  # the FULL audited grid, not a shrunken one
        failures: list[str] = []
        for strike, t_years, sigma, rate, is_call in points:
            got = _kernel_outputs(None, sigma, _GRID_FORWARD, strike, t_years, rate, is_call)
            expected = _ref_greeks(sigma, _GRID_FORWARD, strike, t_years, rate, is_call)
            for name in _OUTPUT_NAMES:
                deviation = _sigma_path_deviation(got[name], expected[name])
                if deviation is not None:
                    failures.append(
                        f"{name} rel={deviation:.3e} at K={strike:.1f} T={t_years:.6g} "
                        f"sigma={sigma} r={rate} call={is_call} "
                        f"got={got[name]!r} exp={expected[name]!r}"
                    )
        assert not failures, f"{len(failures)} sigma-path deviations; first: {failures[:5]}"

    def test_solve_path_no_silent_wrong_roots(self) -> None:
        """Every kernel-solved IV must reprice on the REFERENCE pricer.

        A ``no_solution`` refusal is only accepted as honest when the point is
        genuinely degenerate: representable time value below the kernel's
        1e-9-relative verification band, price at its upper asymptote, or a
        physically-unreachable sub-1e-250-rupee premium (the audited solver
        hard-fail family sat at ~1e-281 rupees).
        """
        solved = refused = 0
        failures: list[str] = []
        for strike, t_years, sigma, rate, is_call in _grid_points():
            premium = _ref_price(sigma, _GRID_FORWARD, strike, t_years, rate, is_call)
            if not (premium > 0.0 and math.isfinite(premium)) or premium < sys.float_info.min:
                continue  # the kernel documents a subnormal/zero-price reject
            try:
                out = _kernel_outputs(premium, None, _GRID_FORWARD, strike, t_years, rate, is_call)
            except ValueError as exc:
                refused += 1
                if not str(exc).startswith("no_solution"):
                    failures.append(f"invalid_input at K={strike:.1f} T={t_years:.6g}: {exc}")
                    continue
                if premium < 1e-250:
                    continue  # physically unreachable magnitude — refusal honest
                undiscounted = premium * math.exp(rate * t_years)
                intrinsic = max(
                    0.0,
                    (_GRID_FORWARD - strike) if is_call else (strike - _GRID_FORWARD),
                )
                upper = _GRID_FORWARD if is_call else strike
                time_value = undiscounted - intrinsic
                if time_value > 1e-9 * undiscounted and undiscounted < upper * (1.0 - 1e-12):
                    failures.append(
                        f"dishonest no_solution at K={strike:.1f} T={t_years:.6g} "
                        f"sigma={sigma} r={rate} call={is_call} premium={premium!r}"
                    )
                continue
            solved += 1
            reprice = _ref_price(out["iv"], _GRID_FORWARD, strike, t_years, rate, is_call)
            round_trip = _rel_err(reprice, premium, abs_floor=1e-300)
            if round_trip > _TOL_ROUNDTRIP:
                failures.append(
                    f"round-trip rel={round_trip:.3e} at K={strike:.1f} T={t_years:.6g} "
                    f"sigma={sigma} r={rate} call={is_call} iv={out['iv']!r}"
                )
        assert solved >= 3000  # the solve path was genuinely exercised
        assert solved + refused > 0
        assert not failures, f"{len(failures)} solve-path failures; first: {failures[:5]}"

    def test_solve_path_recovers_generating_sigma_when_conditioned(self) -> None:
        """Where vega identifies sigma, the kernel's root IS the generator."""
        checked = 0
        failures: list[str] = []
        for strike, t_years, sigma, rate, is_call in _grid_points():
            premium = _ref_price(sigma, _GRID_FORWARD, strike, t_years, rate, is_call)
            if not (premium > 0.0 and math.isfinite(premium)) or premium < sys.float_info.min:
                continue
            d1 = _d1_d2(sigma, _GRID_FORWARD, strike, t_years)[0]
            vega_raw = (
                _GRID_FORWARD * math.exp(-rate * t_years) * _norm_pdf(d1) * math.sqrt(t_years)
            )
            if vega_raw <= 1e-3 * _GRID_FORWARD:
                continue  # sigma not identifiable from the premium — skip
            try:
                out = _kernel_outputs(premium, None, _GRID_FORWARD, strike, t_years, rate, is_call)
            except ValueError:  # honesty asserted by the round-trip test above
                continue
            checked += 1
            tol = (
                _TOL_RECOVERY_SHARP
                if sigma * math.sqrt(t_years) <= 2.5
                else _TOL_RECOVERY_HARD_BAND
            )
            err = _rel_err(out["iv"], sigma)
            if err > tol:
                failures.append(
                    f"iv recovery rel={err:.3e} at K={strike:.1f} T={t_years:.6g} "
                    f"sigma={sigma} r={rate} call={is_call} iv={out['iv']!r}"
                )
        assert checked >= 1000  # the well-conditioned band was genuinely exercised
        assert not failures, f"{len(failures)} recovery failures; first: {failures[:5]}"


# --------------------------------------------------------------------------
# Grid B — enrich_option_bars end-to-end vs the reference: synthetic
# 2dp-snapped chains through the REAL pure core, every STORED value
# independently recomputed (T from the bar ts to 15:30 IST expiry, forward
# F = S·e^{rT}, IV by reference bisection, Greeks by the reference closed
# form).
# --------------------------------------------------------------------------

_CHAIN_EXPIRY = date(2025, 8, 28)
_CHAIN_STRIKE = 24_000.0
_CHAIN_BASE_TS = datetime(2025, 8, 1, 3, 45, tzinfo=UTC)  # 09:15 IST
_CHAIN_BARS = 48
_FIXED_RATE = 0.065  # the documented FixedRateSource default (ADR-0015 §3)
_TOL_T_YEARS = 1e-15
_TOL_STORED_REPRICE = 2e-9
_TOL_BISECT_VS_STORED = 1e-6
_TOL_STORED_GREEKS = 5e-12


def _two_dp(value: float) -> Decimal:
    """Snap a synthetic premium/spot to the production 2dp tick grid."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _lake_row(ts: datetime, close: Decimal) -> dict[str, object]:
    """One never-enriched lake row (overlay columns present but NULL)."""
    row: dict[str, object] = {
        "timestamp": ts,
        "open": float(close),
        "high": float(close),
        "low": float(close),
        "close": float(close),
        "volume": 100,
        "open_interest": 5_000,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        row[name] = None
    return row


@dataclass(frozen=True)
class _ChainCase:
    """One enriched synthetic chain plus its independent generators."""

    bars: tuple[FnoGreeksBar, ...]
    generated: tuple[tuple[datetime, float, float], ...]  # (ts, spot, close)
    is_call: bool


@cache
def _enriched_chain(is_call: bool) -> _ChainCase:
    """Build + enrich one 48-minute 2dp chain through the REAL pure core."""
    side = "CE" if is_call else "PE"
    parts = parse_fno_symbol(f"NSE-NIFTY-28Aug25-24000-{side}")
    assert parts is not None
    spot_by_ts: dict[datetime, Decimal] = {}
    rows: list[dict[str, object]] = []
    generated: list[tuple[datetime, float, float]] = []
    for i in range(_CHAIN_BARS):
        ts = _CHAIN_BASE_TS + timedelta(minutes=i)
        spot_d = _two_dp(23_500.0 + 40.0 * math.sin(i / 9.0) + 0.6 * i)
        spot_f = float(spot_d)
        spot_by_ts[ts] = spot_d
        t_years = _t_years_independent(ts, _CHAIN_EXPIRY)
        forward = spot_f * math.exp(_FIXED_RATE * t_years)
        sigma = (0.14 + 0.04 * math.sin(i / 9.0)) if is_call else (0.17 + 0.03 * math.cos(i / 7.0))
        close_d = _two_dp(_ref_price(sigma, forward, _CHAIN_STRIKE, t_years, _FIXED_RATE, is_call))
        rows.append(_lake_row(ts, close_d))
        generated.append((ts, spot_f, float(close_d)))
    bars, stats = enrich_option_bars(
        rows,
        parts=parts,
        spot_by_ts=spot_by_ts,
        rate_source=FixedRateSource(),
        style_flag=STYLE_EUROPEAN_EXACT,
    )
    assert stats.solved == _CHAIN_BARS
    assert stats.nulls == 0
    assert stats.skipped == 0
    return _ChainCase(bars=tuple(bars), generated=tuple(generated), is_call=is_call)


def _verify_stored_bar(
    bar: FnoGreeksBar,
    *,
    ts: datetime,
    close_f: float,
    spot_f: float,
    strike_f: float,
    is_call: bool,
    rate: float,
    expiry: date = _CHAIN_EXPIRY,
) -> list[str]:
    """Independently recompute everything a STORED OK bar claims."""
    problems: list[str] = []
    tag = f"{'CE' if is_call else 'PE'}@{ts:%Y-%m-%d %H:%M}"
    t_years = _t_years_independent(ts, expiry)
    if bar.t_years_used is None or abs(bar.t_years_used - t_years) > _TOL_T_YEARS * max(
        1.0, t_years
    ):
        problems.append(f"{tag}: t_years_used {bar.t_years_used!r} != independent {t_years!r}")
    if bar.rate_used != rate:
        problems.append(f"{tag}: rate_used {bar.rate_used!r} != {rate!r}")
    if bar.spot_at_bar is None or float(bar.spot_at_bar) != spot_f:
        problems.append(f"{tag}: spot_at_bar {bar.spot_at_bar!r} != {spot_f!r}")
    forward = spot_f * math.exp(rate * t_years)
    if bar.iv is None:
        problems.append(f"{tag}: iv is None on an OK bar")
        return problems
    # (1) The STORED IV must reprice the STORED close on the REFERENCE pricer.
    reprice = _ref_price(bar.iv, forward, strike_f, t_years, rate, is_call)
    if _rel_err(reprice, close_f) > _TOL_STORED_REPRICE:
        problems.append(
            f"{tag}: stored iv {bar.iv!r} reprices to {reprice!r} vs close {close_f!r} "
            f"(rel {_rel_err(reprice, close_f):.2e})"
        )
    # (2) The reference's OWN bisection root must agree with the stored IV.
    bisect_iv = _ref_iv_bisect(close_f, forward, strike_f, t_years, rate, is_call)
    if bisect_iv is None or _rel_err(bisect_iv, bar.iv) > _TOL_BISECT_VS_STORED:
        problems.append(f"{tag}: reference bisect iv {bisect_iv!r} vs stored {bar.iv!r}")
    # (3) All five stored Greeks at the STORED IV vs the reference closed form.
    expected = _ref_greeks(bar.iv, forward, strike_f, t_years, rate, is_call)
    for name in ("delta", "gamma", "theta", "vega", "rho"):
        got = getattr(bar, name)
        if got is None or _rel_err(got, expected[name], abs_floor=1e-300) > _TOL_STORED_GREEKS:
            problems.append(
                f"{tag}: {name} stored {got!r} vs reference {expected[name]!r} "
                f"(rel {_rel_err(got or 0.0, expected[name], abs_floor=1e-300):.2e})"
            )
    # (4) The provenance stamp must describe exactly these inputs.
    assert bar.greeks_provenance_id is not None
    stamp = unpack_greeks_provenance(bar.greeks_provenance_id)
    if (
        stamp.status,
        stamp.formula_version,
        stamp.q_bias_id,
        stamp.tte_convention_id,
        stamp.rate_source_id,
        stamp.style_flag,
    ) != (
        STATUS_OK,
        GREEKS_FORMULA_VERSION,
        Q_BIAS_ZERO,
        TTE_ACT365_EXPIRY_1530_IST,
        RATE_SOURCE_ID_FIXED,
        STYLE_EUROPEAN_EXACT,
    ):
        problems.append(f"{tag}: provenance fields {stamp!r}")
    return problems


class TestGridBEnrichedChainVsReference:
    """enrich_option_bars end-to-end: every stored byte independently redone."""

    def test_ce_chain_stored_overlay_matches_reference(self) -> None:
        case = _enriched_chain(True)
        problems: list[str] = []
        for (ts, spot_f, close_f), bar in zip(case.generated, case.bars, strict=True):
            problems.extend(
                _verify_stored_bar(
                    bar,
                    ts=ts,
                    close_f=close_f,
                    spot_f=spot_f,
                    strike_f=_CHAIN_STRIKE,
                    is_call=True,
                    rate=_FIXED_RATE,
                )
            )
        assert not problems, f"{len(problems)} CE deviations; first: {problems[:5]}"

    def test_pe_chain_stored_overlay_matches_reference(self) -> None:
        case = _enriched_chain(False)
        problems: list[str] = []
        for (ts, spot_f, close_f), bar in zip(case.generated, case.bars, strict=True):
            problems.extend(
                _verify_stored_bar(
                    bar,
                    ts=ts,
                    close_f=close_f,
                    spot_f=spot_f,
                    strike_f=_CHAIN_STRIKE,
                    is_call=False,
                    rate=_FIXED_RATE,
                )
            )
        assert not problems, f"{len(problems)} PE deviations; first: {problems[:5]}"

    def test_deep_itm_put_below_naive_intrinsic_still_solves(self) -> None:
        """A put between the DISCOUNTED floor and naive K−S intrinsic solves.

        The discounted-parity subtlety: at r > 0 a deep-ITM put legitimately
        trades below ``K − S`` (its true floor is ``K·e^{−rT} − S``) — the
        enrichment must solve it, and the solved IV must reprice on the
        reference. Independently verified band: floor < close < K − S.
        """
        ts = datetime(2025, 8, 1, 5, 0, tzinfo=UTC)
        spot = Decimal("23500.00")
        t_years = _t_years_independent(ts, _CHAIN_EXPIRY)
        disc_floor = _CHAIN_STRIKE * math.exp(-_FIXED_RATE * t_years) - 23_500.0
        close = 450.0
        assert disc_floor < close < _CHAIN_STRIKE - 23_500.0  # the deliberate band
        parts = parse_fno_symbol("NSE-NIFTY-28Aug25-24000-PE")
        assert parts is not None
        bars, stats = enrich_option_bars(
            [_lake_row(ts, Decimal("450.00"))],
            parts=parts,
            spot_by_ts={ts: spot},
            rate_source=FixedRateSource(),
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.solved == 1
        problems = _verify_stored_bar(
            bars[0],
            ts=ts,
            close_f=close,
            spot_f=23_500.0,
            strike_f=_CHAIN_STRIKE,
            is_call=False,
            rate=_FIXED_RATE,
        )
        assert not problems, problems

    def test_degenerate_band_artifact_refused_below_intrinsic(self) -> None:
        """The audited IVX-1 production artifact is REFUSED, not solved.

        NSE-NIFTY-27Aug26-14000-CE, bar 2026-06-30 10:14 IST, spot 23500.00,
        close 9644.40 — independently verified DEGENERATE below: the premium
        sits within ``1e-9·close`` of the reference-computed discounted no-arb
        floor (time value ~4.2e-6 rupees), where ANY sigma across a wide band
        reprices within the kernel's verification tolerance. Pre-fix this
        stored ``iv=0.2384`` as STATUS_OK; the pre-screen must now stamp the
        honest BELOW_INTRINSIC NULL.
        """
        ts = datetime(2026, 6, 30, 4, 44, tzinfo=UTC)  # 10:14 IST
        expiry = date(2026, 8, 27)
        close = 9644.40
        t_years = _t_years_independent(ts, expiry)
        floor = 23_500.0 - 14_000.0 * math.exp(-_FIXED_RATE * t_years)
        assert 0.0 <= close - floor <= 1e-9 * close  # independently in-band
        parts = parse_fno_symbol("NSE-NIFTY-27Aug26-14000-CE")
        assert parts is not None
        bars, stats = enrich_option_bars(
            [_lake_row(ts, Decimal("9644.40"))],
            parts=parts,
            spot_by_ts={ts: Decimal("23500.00")},
            rate_source=FixedRateSource(),
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.nulls == 1
        bar = bars[0]
        assert bar.greeks_provenance_id is not None
        assert unpack_greeks_provenance(bar.greeks_provenance_id).status == STATUS_BELOW_INTRINSIC
        assert bar.iv is None
        assert bar.delta is None
        assert bar.spot_at_bar is not None  # the join spot is still recorded

    def test_same_geometry_with_real_time_value_solves(self) -> None:
        """Anti-over-screen: the artifact geometry + REAL time value solves.

        Instructive aside pinning WHY the artifact family exists in real
        vendor data: at this depth even the σ = 0.2 MODEL price carries only
        ~4e-6 rupees of time value — it 2dp-snaps to exactly the audited
        artifact close (9644.40). A genuine HALF-RUPEE of time value on the
        same geometry (the unit regression's anti-over-screen shape) must
        sit above the band and solve to a reference-verified root.
        """
        ts = datetime(2026, 6, 30, 4, 44, tzinfo=UTC)
        expiry = date(2026, 8, 27)
        t_years = _t_years_independent(ts, expiry)
        floor = 23_500.0 - 14_000.0 * math.exp(-_FIXED_RATE * t_years)
        assert _two_dp(
            _ref_price(
                0.2,
                23_500.0 * math.exp(_FIXED_RATE * t_years),
                14_000.0,
                t_years,
                _FIXED_RATE,
                True,
            )
        ) == Decimal("9644.40")
        close_d = _two_dp(floor + 0.51)
        assert float(close_d) > floor + 1e-9 * float(close_d)  # independently ABOVE the band
        parts = parse_fno_symbol("NSE-NIFTY-27Aug26-14000-CE")
        assert parts is not None
        bars, stats = enrich_option_bars(
            [_lake_row(ts, close_d)],
            parts=parts,
            spot_by_ts={ts: Decimal("23500.00")},
            rate_source=FixedRateSource(),
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.solved == 1
        problems = _verify_stored_bar(
            bars[0],
            ts=ts,
            close_f=float(close_d),
            spot_f=23_500.0,
            strike_f=14_000.0,
            is_call=True,
            rate=_FIXED_RATE,
            expiry=expiry,
        )
        assert not problems, problems


# --------------------------------------------------------------------------
# Hypothesis property — random in-band tuples, reference ↔ kernel agreement.
# --------------------------------------------------------------------------


class TestReferenceKernelProperty:
    """Random well-conditioned tuples: the kernel IS the reference closed form."""

    @settings(max_examples=80, deadline=None)
    @given(
        z=st.floats(min_value=-6.0, max_value=6.0),
        t_years=st.floats(min_value=1.0 / 365.0, max_value=1.0),
        sigma=st.floats(min_value=0.05, max_value=2.0),
        rate=st.floats(min_value=0.0, max_value=0.1),
        is_call=st.booleans(),
    )
    def test_kernel_and_reference_agree_on_in_band_tuples(
        self, z: float, t_years: float, sigma: float, rate: float, is_call: bool
    ) -> None:
        """Strike constructed from a bounded standardized moneyness ``z``.

        ``K = F·e^{−z·σ√T}`` keeps ``|d1| ≤ 7`` by construction (no φ(d1)
        underflow, no assume-rejection), so the tight tolerance applies
        everywhere: rel ``≤ 1e-10`` per output (the audited meaningful-band
        worst was ~5e-13 — two decades of headroom).
        """
        forward = _GRID_FORWARD
        sig_sqrt_t = sigma * math.sqrt(t_years)
        strike = forward * math.exp(-z * sig_sqrt_t)
        got = _kernel_outputs(None, sigma, forward, strike, t_years, rate, is_call)
        expected = _ref_greeks(sigma, forward, strike, t_years, rate, is_call)
        for name in _OUTPUT_NAMES:
            if abs(got[name]) < _BOTH_ZERO_BAND and abs(expected[name]) < _BOTH_ZERO_BAND:
                continue
            assert _rel_err(got[name], expected[name], abs_floor=1e-300) <= 1e-10, (
                f"{name}: kernel {got[name]!r} vs reference {expected[name]!r} "
                f"(z={z}, T={t_years}, sigma={sigma}, r={rate}, call={is_call})"
            )
        # Opportunistic solve round-trip where the premium is comfortably
        # non-degenerate: the kernel must find A root and it must reprice on
        # the REFERENCE pricer (no silent wrong roots on random tuples).
        premium = expected["price"]
        disc_floor = max(
            0.0,
            math.exp(-rate * t_years) * ((forward - strike) if is_call else (strike - forward)),
        )
        if premium >= 0.05 and premium - disc_floor >= 1e-6 * premium:
            solved = _kernel_outputs(premium, None, forward, strike, t_years, rate, is_call)
            reprice = _ref_price(solved["iv"], forward, strike, t_years, rate, is_call)
            assert _rel_err(reprice, premium) <= _TOL_ROUNDTRIP
