"""Thin Python wrapper over the shared Rust Black-76 Greeks kernel (Phase 2).

`DEC-GREEKS-ENGINE-PR2-001` — plan `docs/GREEKS_ENGINE_PLAN.md` §11 Phase 2 /
ADR-0015 D2-D4. The arithmetic lives in ONE place: the compiled
``trading_core`` wheel (``rust/trading-core/src/greeks/``), the same crate
TickVault links natively — this module only validates, marshals, and stamps
(the ``brutex/indicators/pivots.py`` delegation pattern; a pure-Python math
fallback is REJECTED per plan §10, Golden Rule #11 COMMON). The auto-facade
surfaces the raw kernel as ``brutex.math.greeks``.

The risk-free rate is consumed ONLY through PR-1's ``RiskFreeSource`` seam:
:func:`compute_greeks` takes a stamped :class:`~brutex.options.risk_free.RiskFreeRate`
(never a bare float), and echoes its ``(source, version)`` provenance on the
result (`DEC-GREEKS-ENGINE-PR1-001`).

Outcome contract (mirrors the kernel's two error prefixes):

- a WELL-FORMED but unsolvable tuple (below the 600 s T-floor, price below
  intrinsic / above the no-arb bound, IV outside the [0.001, 5.0] clamp,
  solver non-convergence) → an honest ``None`` — NEVER a fabricated value
  (plan §9);
- a CALLER-CONTRACT violation (zero/negative/non-finite/subnormal inputs,
  out-of-band sigma, neither price nor sigma, a bare-float rate) → a loud
  :class:`GreeksInputError`.

Complexity (honest): O(1) per call — a fixed count of transcendentals plus
the verified Jäckel "Let's Be Rational" inversion (bounded by construction)
with a deterministic, iteration-capped bracketed fallback. NOT an
O(1)-total claim for batch callers.

Batch face (`DEC-GREEKS-RUST-BATCH-001`): :func:`compute_greeks_batch` routes
N frozen :class:`GreeksSolveRequest` tuples through ONE
``trading_core.greeks_batch`` FFI call — the compiled side calls the SAME
frozen scalar kernel per element, so every outcome is bit-identical to the
scalar entry's; per-element rejects travel as :class:`GreeksBatchOutcome`
DATA instead of exceptions. Consumed by the default-OFF
``BRUTEX_GREEKS_RUST_BATCH`` seam in :mod:`brutex.data.greeks_enrich`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import trading_core

from brutex.options.risk_free import RiskFreeRate

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DELTA_ADMISSION_FLOOR",
    "IV_CLAMP_MAX",
    "IV_CLAMP_MIN",
    "T_FLOOR_YEARS",
    "VEGA_ADMISSION_FLOOR",
    "GreeksBatchOutcome",
    "GreeksInputError",
    "GreeksResult",
    "GreeksSolveRequest",
    "compute_greeks",
    "compute_greeks_batch",
]

#: Hard time-to-expiry floor (600 s in Act/365 years) — below it the kernel
#: refuses (honest ``None``), mirroring ``trading_core.GREEKS_T_FLOOR_YEARS``.
T_FLOOR_YEARS: Final[float] = float(trading_core.GREEKS_T_FLOOR_YEARS)

#: IV clamp bounds — a solve outside [0.001, 5.0] is ``None``; the boundary is
#: never reported as "the IV" (plan §9 row 10).
IV_CLAMP_MIN: Final[float] = float(trading_core.GREEKS_IV_CLAMP_MIN)
IV_CLAMP_MAX: Final[float] = float(trading_core.GREEKS_IV_CLAMP_MAX)

#: Admission floors (plan §11 Phase 2): a tuple with |delta| below 0.02 or raw
#: vega below 1e-6 is returned with ``admissible=False`` — a delta-target
#: selection FILTER, not an error.
DELTA_ADMISSION_FLOOR: Final[float] = float(trading_core.GREEKS_DELTA_ADMISSION_FLOOR)
VEGA_ADMISSION_FLOOR: Final[float] = float(trading_core.GREEKS_VEGA_ADMISSION_FLOOR)

#: Kernel error prefix that maps to the honest-``None`` family; everything
#: else re-raises loudly (the kernel's documented two-prefix taxonomy).
_NO_SOLUTION_PREFIX: Final[str] = "no_solution"


class GreeksInputError(ValueError):
    """A Greeks input violates the kernel's caller contract.

    Raised for the LOUD family only — zero/negative/non-finite/subnormal
    forward, strike, or price; sigma outside the IV clamp; neither price nor
    sigma; a rate not carried by the PR-1 ``RiskFreeRate`` seam. An
    UNSOLVABLE-but-well-formed tuple is an honest ``None`` instead, never an
    exception (plan §9).
    """


@dataclass(frozen=True, slots=True)
class GreeksResult:
    """One priced Greeks tuple with its risk-free provenance stamp.

    Attributes
    ----------
    iv:
        Implied (or supplied) volatility, annualized decimal fraction. FULL
        precision — never rounded (DEC-MONEY-2DP-001 forbids rounding ``iv``).
    delta:
        Signed delta — CE in ``[0, 1]``, PE in ``[-1, 0]``.
    gamma:
        Gamma (per unit of forward), >= 0, identical CE/PE.
    theta:
        Theta per CALENDAR DAY (Act/365). Under Black-76 a deep-ITM option's
        theta can be legitimately positive at ``r > 0`` (discount unwind).
    vega:
        Vega per 1 vol-point (per 1% IV), >= 0.
    rho:
        Rho per 1% rate (plan §2.2 form; signed CE + / PE −).
    price:
        Black-76 model price at :attr:`iv` (rupees; on the solve path it
        reproduces the input premium to verification tolerance).
    admissible:
        The plan's admission floors (``|delta| >= 0.02`` and raw vega
        ``>= 1e-6``) — ``False`` flags a numerically well-defined but
        untrustworthy-for-selection tuple.
    rate_source:
        The consumed :class:`~brutex.options.risk_free.RiskFreeRate` source
        label (e.g. ``"fixed"``) — the provenance stamp.
    rate_version:
        The consumed rate's ``RISK_FREE_SOURCE_VERSION`` — the staleness key
        the Phase-2b ``greeks_provenance_id`` folds in.
    """

    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    price: float
    admissible: bool
    rate_source: str
    rate_version: int


def compute_greeks(
    *,
    price: float | None = None,
    sigma: float | None = None,
    forward: float,
    strike: float,
    t_years: float,
    rate: RiskFreeRate,
    is_call: bool,
) -> GreeksResult | None:
    """Price the six Greeks off the ONE shared Rust kernel (ADR-0015 D3).

    ``sigma`` given → prices directly (live / vendor-IV path; ``price`` is
    ignored, sigma takes precedence). ``sigma`` omitted → the kernel solves
    IV from the traded ``price`` first (historical path), then prices the
    Greeks off the SAME closed form.

    Parameters
    ----------
    price:
        Traded (discounted) option premium in rupees — required when
        ``sigma`` is ``None``.
    sigma:
        Known annualized volatility in ``[IV_CLAMP_MIN, IV_CLAMP_MAX]``.
    forward:
        Underlying forward ``F = S * exp((r - q) * T)`` — built by the
        caller, whose ``q`` assumption is stamped upstream (plan §7 #3).
    strike:
        Option strike ``K``.
    t_years:
        Time to expiry in years under the caller's stamped convention; below
        :data:`T_FLOOR_YEARS` (600 s) the result is an honest ``None``.
    rate:
        The stamped risk-free rate from PR-1's ``RiskFreeSource`` seam — the
        ONLY way ``r`` enters a Greeks solve (DEC-GREEKS-ENGINE-PR1-001).
    is_call:
        ``True`` for CE, ``False`` for PE.

    Returns
    -------
    GreeksResult | None
        The priced tuple stamped with the rate provenance, or ``None`` when
        no trustworthy IV exists (the kernel's ``no_solution`` family —
        never a fabricated value, plan §9).

    Raises
    ------
    GreeksInputError
        On a caller-contract violation (the kernel's ``invalid_input``
        family, or a ``rate`` that is not a :class:`RiskFreeRate`).
    """
    # Runtime seam guard: the annotation already says RiskFreeRate, but an
    # untyped/dynamic caller (FFI-ish dashboard code) could still hand a bare
    # float — refuse it LOUDLY here instead of failing later on `.rate`. The
    # `object` rebind keeps the branch visible to mypy --strict (which would
    # otherwise flag the check unreachable under the annotation).
    rate_obj: object = rate
    if not isinstance(rate_obj, RiskFreeRate):
        msg = (
            "rate must be a stamped RiskFreeRate from the PR-1 RiskFreeSource "
            f"seam (DEC-GREEKS-ENGINE-PR1-001), got {type(rate_obj).__name__}"
        )
        raise GreeksInputError(msg)
    try:
        iv, delta, gamma, theta, vega, rho, model_price, admissible = trading_core.greeks(
            price, sigma, forward, strike, t_years, rate.rate, is_call
        )
    except ValueError as exc:
        if str(exc).startswith(_NO_SOLUTION_PREFIX):
            return None
        raise GreeksInputError(str(exc)) from exc
    return GreeksResult(
        iv=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        price=model_price,
        admissible=admissible,
        rate_source=rate.source,
        rate_version=rate.version,
    )


@dataclass(frozen=True, slots=True)
class GreeksSolveRequest:
    """One already-screened solve tuple for :func:`compute_greeks_batch`.

    The exact argument set of :func:`compute_greeks`, frozen as data so N
    requests can cross the FFI in ONE columnar call
    (`DEC-GREEKS-RUST-BATCH-001`). ``sigma`` given → direct pricing (the
    live / vendor-IV path; ``price`` ignored); ``sigma`` omitted → IV is
    solved from ``price`` first (the historical enrich path).
    """

    price: float | None
    forward: float
    strike: float
    t_years: float
    rate: RiskFreeRate
    is_call: bool
    sigma: float | None = None


@dataclass(frozen=True, slots=True)
class GreeksBatchOutcome:
    """One request's outcome — the data mirror of :func:`compute_greeks`.

    Exactly three legal shapes, mirroring the scalar entry's contract:

    * ``result`` set — solved (the scalar return value);
    * both ``None`` — the honest ``no_solution`` NA (the scalar ``None``);
    * ``invalid_input`` set — the caller-contract reject message the scalar
      entry would raise as :class:`GreeksInputError` (per-element DATA here,
      so one bad row never aborts its batch siblings).

    ``__post_init__`` refuses the impossible both-set shape loudly.
    """

    result: GreeksResult | None
    invalid_input: str | None

    def __post_init__(self) -> None:
        if self.result is not None and self.invalid_input is not None:
            msg = (
                "GreeksBatchOutcome cannot carry BOTH a result and an "
                "invalid_input — the kernel contract is exactly-one-of"
            )
            raise ValueError(msg)


def compute_greeks_batch(
    requests: Sequence[GreeksSolveRequest],
) -> list[GreeksBatchOutcome]:
    """Price N requests through ONE ``trading_core.greeks_batch`` FFI call.

    The columnar batch face of :func:`compute_greeks`
    (`DEC-GREEKS-RUST-BATCH-001`): the compiled kernel routes each element
    through the SAME frozen scalar ``greeks`` entry (zero math duplication),
    so element ``i``'s outcome is bit-identical to
    ``compute_greeks(**requests[i])`` — solved values, the honest-``None``
    ``no_solution`` family, and the ``invalid_input`` reject message alike
    (the latter carried as per-element DATA instead of an exception, so one
    unsolvable strike never aborts its siblings). Classification uses the
    SAME :data:`_NO_SOLUTION_PREFIX` ``startswith`` the scalar wrapper uses;
    ``rate_source`` / ``rate_version`` are stamped Python-side from each
    request's :class:`~brutex.options.risk_free.RiskFreeRate`, exactly as the
    scalar wrapper stamps them.

    Complexity (honest): O(len(requests)) kernel solves — a constant-factor
    FFI-overhead win over N scalar calls, NEVER an O(1) claim.

    Raises
    ------
    GreeksInputError
        Whole-call, BEFORE any FFI crossing, when any request's ``rate`` is
        not a :class:`RiskFreeRate` (the scalar entry's seam guard, applied
        per element up front — a bare float must never reach ``.rate``).
    """
    for req in requests:
        rate_obj: object = req.rate
        if not isinstance(rate_obj, RiskFreeRate):
            msg = (
                "rate must be a stamped RiskFreeRate from the PR-1 "
                "RiskFreeSource seam (DEC-GREEKS-ENGINE-PR1-001), got "
                f"{type(rate_obj).__name__}"
            )
            raise GreeksInputError(msg)
    raw = trading_core.greeks_batch(
        [req.price for req in requests],
        [req.sigma for req in requests],
        [req.forward for req in requests],
        [req.strike for req in requests],
        [req.t_years for req in requests],
        [req.rate.rate for req in requests],
        [req.is_call for req in requests],
    )
    outcomes: list[GreeksBatchOutcome] = []
    for req, (values, error) in zip(requests, raw, strict=True):
        if error is not None:
            if error.startswith(_NO_SOLUTION_PREFIX):
                outcomes.append(GreeksBatchOutcome(result=None, invalid_input=None))
            else:
                outcomes.append(GreeksBatchOutcome(result=None, invalid_input=error))
            continue
        iv, delta, gamma, theta, vega, rho, model_price, admissible = values
        outcomes.append(
            GreeksBatchOutcome(
                result=GreeksResult(
                    iv=iv,
                    delta=delta,
                    gamma=gamma,
                    theta=theta,
                    vega=vega,
                    rho=rho,
                    price=model_price,
                    admissible=admissible,
                    rate_source=req.rate.source,
                    rate_version=req.rate.version,
                ),
                invalid_input=None,
            )
        )
    return outcomes
