"""Generate the frozen Greeks golden fixture (`greeks_golden.json`).

DEC-GREEKS-ENGINE-PR2-001 — plan `docs/GREEKS_ENGINE_PLAN.md` §10/§12,
ADR-0015 D6. The fixture is the bit-identity contract BOTH engines satisfy
(the Greeks twin of `vocab_golden_vectors.json`):

- Rust: `rust/trading-core/tests/greeks_golden.rs`
- Python (via the wheel): `tests/golden/test_greeks_golden_parity.py`

HOW THE VALUES ARE COMPUTED (independence — Golden Rule #1):

1. ``reference_*`` blocks are computed here with **mpmath at 50 decimal
   digits** — an arithmetic path fully independent of the Rust kernel
   (mpmath's ``erfc``-based normal CDF + ``findroot`` IV inversion vs the
   crate's Cody erf + "Let's Be Rational"). Each reference value is the
   50-digit result correctly rounded to the nearest f64.
   - the solve-path reference IV is the root of
     ``black76(sigma) == f64(price)`` (the f64-ROUNDED premium — exactly
     what the kernel is asked to invert), solved by ``mp.findroot`` at
     50 digits;
   - Greeks are then evaluated at that root with the plan §2.2 conventions
     (theta per Act/365 calendar day; vega per 1% IV; rho per 1% rate,
     plan-form ``±K·T·e^(−rT)·N(±d2)/100``).
2. ``frozen_*_bits`` blocks are the EXACT IEEE-754 bit patterns produced by
   the compiled ``trading_core`` kernel on the reference platform
   (Linux x86_64 CI): run with ``--freeze`` against the installed wheel.
   They are asserted with ``assert_eq!`` on bits — any kernel change that
   moves one bit fails both goldens.

The two layers together give: independent CORRECTNESS (mpmath tolerance
gate) + Rust↔Python BIT-IDENTITY (shared frozen bits). Cross-PLATFORM
bit-identity (e.g. macOS arm64) is NOT claimed — plan §13 open decision #6
records the documented-tolerance fallback; the tolerance gate is the
cross-platform contract.

Regenerate (deterministic):

    .venv/bin/python tests/golden/generate_greeks_golden.py --freeze

(requires ``mpmath`` + the built ``trading_core`` wheel; without ``--freeze``
the existing frozen bits are preserved and only the references recompute).
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

from mpmath import erfc, exp, log, mp, mpf, sqrt  # type: ignore[import-untyped]

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greeks_golden.json"

#: Digits for every reference computation (comfortably beyond f64's ~17).
REFERENCE_DPS = 50

#: Combined tolerance the golden tests assert between an engine value ``got``
#: and the mpmath reference ``want``: ``|got − want| ≤ ABS + REL·|want|``.
#: Chosen from the measured worst drift of the kernel over this fixture
#: (printed by this script on every run; 1.212e-12 relative at freeze time,
#: so the gate carries ~8× margin — tightened deliberately over a loose
#: gate so a real numeric regression cannot hide inside it).
TOLERANCE_REL = 1e-11
TOLERANCE_ABS = 1e-12

# Case grid: (id, forward, strike, t_years, rate, is_call, sigma_true).
# Rupee-scale forwards mirror the three engine indices (CLAUDE.md §3) plus a
# stock-scale underlying; T spans the expiry minute to one year; sigma spans
# the liquid band plus a high-vol stress point. Every case must be SOLVABLE
# (in-clamp, above intrinsic) — the generator fails loudly otherwise.
CASES: list[tuple[str, float, float, float, float, bool, float]] = [
    ("nifty_atm_call_30d", 23500.0, 23500.0, 30.0 / 365.0, 0.065, True, 0.12),
    ("nifty_atm_put_30d", 23500.0, 23500.0, 30.0 / 365.0, 0.065, False, 0.12),
    ("nifty_itm_call_30d", 23500.0, 23000.0, 30.0 / 365.0, 0.065, True, 0.14),
    ("nifty_otm_call_30d", 23500.0, 24000.0, 30.0 / 365.0, 0.065, True, 0.13),
    ("nifty_itm_put_30d", 23500.0, 24000.0, 30.0 / 365.0, 0.065, False, 0.13),
    ("nifty_otm_put_30d", 23500.0, 23000.0, 30.0 / 365.0, 0.065, False, 0.14),
    ("nifty_deep_itm_call_30d", 23500.0, 20000.0, 30.0 / 365.0, 0.065, True, 0.18),
    ("nifty_deep_otm_put_30d", 23500.0, 20000.0, 30.0 / 365.0, 0.065, False, 0.22),
    ("nifty_weekly_atm_call_2d", 23500.0, 23500.0, 2.0 / 365.0, 0.065, True, 0.15),
    ("nifty_weekly_otm_put_2d", 23500.0, 23300.0, 2.0 / 365.0, 0.065, False, 0.17),
    (
        "nifty_expiry_minute_atm_call",
        23500.0,
        23500.0,
        650.0 / (365.0 * 86400.0),
        0.065,
        True,
        0.30,
    ),
    ("nifty_penny_otm_call_7d", 23500.0, 26500.0, 7.0 / 365.0, 0.065, True, 0.35),
    ("banknifty_atm_call_30d", 51000.0, 51000.0, 30.0 / 365.0, 0.065, True, 0.14),
    ("banknifty_otm_put_7d", 51000.0, 50000.0, 7.0 / 365.0, 0.065, False, 0.16),
    ("sensex_atm_put_30d", 77000.0, 77000.0, 30.0 / 365.0, 0.065, False, 0.11),
    ("sensex_itm_call_90d", 77000.0, 74000.0, 90.0 / 365.0, 0.065, True, 0.12),
    ("stock_atm_call_30d", 1500.0, 1520.0, 30.0 / 365.0, 0.065, True, 0.28),
    ("stock_itm_put_30d", 1500.0, 1560.0, 30.0 / 365.0, 0.065, False, 0.32),
    ("long_dated_atm_call_1y", 23500.0, 23500.0, 1.0, 0.065, True, 0.20),
    ("high_vol_atm_call_30d", 23500.0, 23500.0, 30.0 / 365.0, 0.065, True, 1.50),
    ("low_vol_atm_put_30d", 23500.0, 23500.0, 30.0 / 365.0, 0.065, False, 0.05),
    ("zero_rate_atm_call_30d", 23500.0, 23500.0, 30.0 / 365.0, 0.0, True, 0.12),
    ("zero_rate_otm_put_30d", 23500.0, 23200.0, 30.0 / 365.0, 0.0, False, 0.18),
    ("high_rate_itm_call_60d", 23500.0, 23100.0, 60.0 / 365.0, 0.09, True, 0.16),
]

THETA_DAY_COUNT = 365
PER_CENT = 100


def _norm_cdf(x: Any) -> Any:
    """Standard normal CDF via mpmath erfc (independent of the Rust path)."""
    return erfc(-x / sqrt(mpf(2))) / 2


def _norm_pdf(x: Any) -> Any:
    """Standard normal PDF at 50 digits."""
    return exp(-x * x / 2) / sqrt(2 * mp.pi)


def _black76(sigma: Any, forward: Any, strike: Any, t: Any, rate: Any, is_call: bool) -> Any:
    """Discounted Black-76 price at 50 digits (plan §2.2)."""
    sqrt_t = sqrt(t)
    d1 = (log(forward / strike) + sigma * sigma * t / 2) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    df = exp(-rate * t)
    if is_call:
        return df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def _reference_greeks(
    sigma: Any, forward: Any, strike: Any, t: Any, rate: Any, is_call: bool
) -> dict[str, float]:
    """All six Greeks + price at 50 digits, rounded once to f64 (plan §2.2)."""
    sqrt_t = sqrt(t)
    d1 = (log(forward / strike) + sigma * sigma * t / 2) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    df = exp(-rate * t)
    pdf1 = _norm_pdf(d1)
    if is_call:
        price = df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
        delta = df * _norm_cdf(d1)
        rho = strike * t * df * _norm_cdf(d2) / PER_CENT
    else:
        price = df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))
        delta = -df * _norm_cdf(-d1)
        rho = -strike * t * df * _norm_cdf(-d2) / PER_CENT
    gamma = df * pdf1 / (forward * sigma * sqrt_t)
    vega_raw = forward * df * pdf1 * sqrt_t
    theta = (rate * price - df * forward * pdf1 * sigma / (2 * sqrt_t)) / THETA_DAY_COUNT
    return {
        "iv": float(sigma),
        "delta": float(delta),
        "gamma": float(gamma),
        "theta": float(theta),
        "vega": float(vega_raw / PER_CENT),
        "rho": float(rho),
        "price": float(price),
        "admissible": bool(abs(float(delta)) >= 0.02 and float(vega_raw) >= 1e-6),
    }


def _solve_reference_iv(
    price_f64: float, forward: Any, strike: Any, t: Any, rate: Any, is_call: bool, sigma_seed: float
) -> Any:
    """Invert the f64-rounded premium at 50 digits (mp.findroot, monotone)."""
    from mpmath import findroot  # type: ignore[import-untyped]

    target = mpf(repr(price_f64))
    return findroot(
        lambda s: _black76(s, forward, strike, t, rate, is_call) - target,
        mpf(repr(sigma_seed)),
    )


def _f64_bits(value: float) -> str:
    """Exact IEEE-754 bit pattern of an f64, as 16 lowercase hex chars."""
    return format(struct.unpack("<Q", struct.pack("<d", value))[0], "016x")


def _freeze_block(out: tuple[Any, ...]) -> dict[str, Any]:
    """Frozen kernel output: exact bit patterns + the admissible flag."""
    iv, delta, gamma, theta, vega, rho, price, admissible = out
    return {
        "iv": _f64_bits(iv),
        "delta": _f64_bits(delta),
        "gamma": _f64_bits(gamma),
        "theta": _f64_bits(theta),
        "vega": _f64_bits(vega),
        "rho": _f64_bits(rho),
        "price": _f64_bits(price),
        "admissible": bool(admissible),
    }


def _check_drift(
    reference: dict[str, float], got: tuple[Any, ...], case_id: str, path: str
) -> float:
    """Assert the kernel is inside the tolerance gate; return the worst drift."""
    worst = 0.0
    keys = ("iv", "delta", "gamma", "theta", "vega", "rho", "price")
    for key, value in zip(keys, got[:7], strict=False):
        want = reference[key]
        allowed = TOLERANCE_ABS + TOLERANCE_REL * abs(want)
        drift = abs(float(value) - want)
        if drift > allowed:
            msg = (
                f"{case_id}/{path}/{key}: kernel {value!r} vs reference {want!r} (drift {drift:e})"
            )
            raise SystemExit(msg)
        worst = max(worst, drift / (abs(want) + TOLERANCE_ABS))
    if bool(got[7]) != reference["admissible"]:
        msg = f"{case_id}/{path}: admissible mismatch"
        raise SystemExit(msg)
    return worst


def main() -> None:
    """Build the fixture; ``--freeze`` re-stamps the kernel bit patterns."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true", help="re-freeze kernel bits via the wheel")
    args = parser.parse_args()

    mp.dps = REFERENCE_DPS
    import trading_core

    existing: dict[str, Any] = {}
    if FIXTURE_PATH.exists():
        for case in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]:
            existing[case["id"]] = case

    worst_drift = 0.0
    cases_out: list[dict[str, Any]] = []
    for case_id, forward, strike, t_years, rate, is_call, sigma_true in CASES:
        mp_f = mpf(repr(forward))
        mp_k = mpf(repr(strike))
        mp_t = mpf(repr(t_years))
        mp_r = mpf(repr(rate))
        price_f64 = float(_black76(mpf(repr(sigma_true)), mp_f, mp_k, mp_t, mp_r, is_call))
        sigma_star = _solve_reference_iv(price_f64, mp_f, mp_k, mp_t, mp_r, is_call, sigma_true)

        reference_solve = _reference_greeks(sigma_star, mp_f, mp_k, mp_t, mp_r, is_call)
        reference_solve["price"] = price_f64  # the root reproduces the premium by construction
        reference_sigma = _reference_greeks(mpf(repr(sigma_true)), mp_f, mp_k, mp_t, mp_r, is_call)

        got_solve = trading_core.greeks(price_f64, None, forward, strike, t_years, rate, is_call)
        got_sigma = trading_core.greeks(None, sigma_true, forward, strike, t_years, rate, is_call)
        worst_drift = max(worst_drift, _check_drift(reference_solve, got_solve, case_id, "solve"))
        worst_drift = max(worst_drift, _check_drift(reference_sigma, got_sigma, case_id, "sigma"))

        case: dict[str, Any] = {
            "id": case_id,
            "forward": forward,
            "strike": strike,
            "t_years": t_years,
            "rate": rate,
            "is_call": is_call,
            "sigma_true": sigma_true,
            "price": price_f64,
            "reference_solve": reference_solve,
            "reference_sigma": reference_sigma,
        }
        if args.freeze or case_id not in existing:
            case["frozen_solve_bits"] = _freeze_block(got_solve)
            case["frozen_sigma_bits"] = _freeze_block(got_sigma)
        else:
            case["frozen_solve_bits"] = existing[case_id]["frozen_solve_bits"]
            case["frozen_sigma_bits"] = existing[case_id]["frozen_sigma_bits"]
        cases_out.append(case)

    fixture = {
        "_meta": {
            "decision": "DEC-GREEKS-ENGINE-PR2-001",
            "generator": "tests/golden/generate_greeks_golden.py",
            "reference": f"mpmath dps={REFERENCE_DPS} (erfc-based normal CDF + findroot IV inversion)",
            "frozen_bits": "exact f64 bit patterns of the trading_core kernel on Linux x86_64 (CI reference platform)",
            "tolerance": {"rel": TOLERANCE_REL, "abs": TOLERANCE_ABS},
            "conventions": {
                "model": "Black-76 on the forward, discount e^(-rT) (plan docs/GREEKS_ENGINE_PLAN.md section 2.2)",
                "theta": "per calendar day, Act/365",
                "vega": "per 1 vol-point (per 1% IV) = raw/100",
                "rho": "per 1% rate = plan-form/100 (BSM-form K*T*df*N(d2))",
            },
        },
        "cases": cases_out,
    }
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"wrote {FIXTURE_PATH} — {len(cases_out)} cases, worst kernel-vs-mpmath relative drift {worst_drift:.3e}\n"
    )


if __name__ == "__main__":
    main()
