"""Option moneyness (ITM / ATM / OTM) + ATM±N strike offset — the ONE source of truth.

Moneyness is a **DERIVED, time-varying** classification of an option contract against the
underlying **spot** at a given instant — it is NOT a stored column. The strike is fixed,
but the spot moves every bar, so the same ``24000 CE`` is OTM in the morning and ITM in the
afternoon. Computing it at read time from ``(option_type, strike, spot[, strike_step])``
costs **O(1) per classification** and **O(1) extra space** (nothing is stored / can go
stale). Shared by the ``/db`` F&O candle browser (per-bar) and the options-movers screen
(as-of snapshot) — CLAUDE.md §11 COMMON, one parser, no copy-paste.

The strike grid (the ATM±N step) is **DYNAMIC** — derived from the real strike ladder of an
``(underlying, expiry)`` via :func:`derive_strike_step` — NOT hardcoded. This is what makes
it work identically for **EVERY F&O underlying** (all ~212 single stocks AND the indices),
where :mod:`brutex.options.strike_grid` only knows the 3 index grids (NIFTY/BANKNIFTY/SENSEX)
and would ``KeyError`` on a stock. Golden Rule #1: never assume a step for a symbol we did
not measure.

Pure decision math (stdlib + :class:`~decimal.Decimal`, plus the flag-gated
:mod:`brutex.optcost_rust` seam leaf — DEC-OPTCOST-TAILS-RUST-001: with
``BRUTEX_OPTCOST_TAILS_RUST`` truthy the graded classify / ATM-offset /
strike-step decisions run on the ``fno_math`` ``optcost_*`` kernels; flag OFF —
the default — is byte-identical and never imports the wheel). It **never
raises** — a future (no strike), an absent spot, or any junk input classifies as
:attr:`Moneyness.NA` / :attr:`Moneyness.UNKNOWN` so a per-bar browse or a movers
render never crashes on one bad value; the seam preserves that contract on every
path (kernel faults degrade to the Python oracle inside the facade).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_DOWN, Decimal
from enum import StrEnum
from typing import Final, TypeGuard

from brutex import optcost_rust


class Moneyness(StrEnum):
    """The moneyness bucket of an option against the underlying spot.

    A :class:`~enum.StrEnum` so it JSON-serialises to its value (``"ITM"``) with no adapter
    and compares equal to the bare string (``Moneyness.ITM == "ITM"``).
    """

    ITM = "ITM"
    ATM = "ATM"
    OTM = "OTM"
    #: A future (or any non-option) — moneyness does not apply.
    NA = "NA"
    #: Spot / strike absent or invalid — surfaced honestly, never a fabricated bucket.
    UNKNOWN = "UNKNOWN"


#: The two option-type tokens (mirrors :data:`brutex.data.fno_symbol.OPTION_TYPES`).
OPTION_CALL: Final[str] = "CE"
OPTION_PUT: Final[str] = "PE"

#: A strike grid needs at least this many DISTINCT strikes before a step is inferable.
_MIN_DISTINCT_STRIKES: Final[int] = 2


def _is_positive_finite(value: object) -> TypeGuard[Decimal]:
    """True iff *value* is a finite, strictly-positive :class:`~decimal.Decimal`.

    A traded option strike / spot is always a finite positive rupee value, so anything
    else (``None``, a non-``Decimal``, ``NaN`` / ``inf``, ``<= 0``) is rejected — the
    caller then surfaces :attr:`Moneyness.UNKNOWN` rather than a fabricated bucket. Typed
    as a :class:`~typing.TypeGuard` so a positive check narrows the value to ``Decimal``
    for the arithmetic that follows — no ``assert`` / ``cast`` needed.
    """
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def classify(
    option_type: str | None,
    strike: Decimal | None,
    spot: Decimal | None,
    *,
    atm_band: Decimal | None = None,
) -> Moneyness:
    """Classify an option's moneyness at a given *spot*. O(1), never raises.

    Parameters
    ----------
    option_type
        ``"CE"`` / ``"PE"``. Anything else (``None`` / ``"FUT"``) → :attr:`Moneyness.NA`.
    strike, spot
        Positive :class:`~decimal.Decimal` rupees. A missing / non-positive / non-finite
        value → :attr:`Moneyness.UNKNOWN`.
    atm_band
        ``|spot - strike| <= atm_band`` is treated as :attr:`Moneyness.ATM`. Defaults to
        ``0`` (exact-equality ATM); the per-grid view passes ``step / 2`` so the strike
        nearest spot reads ATM.

    Notes
    -----
    CALL: ``spot > strike`` → ITM, ``spot < strike`` → OTM. PUT: the mirror.
    """
    if option_type not in (OPTION_CALL, OPTION_PUT):
        return Moneyness.NA
    if not _is_positive_finite(strike) or not _is_positive_finite(spot):
        return Moneyness.UNKNOWN
    band = atm_band if (atm_band is not None and atm_band >= 0) else Decimal(0)
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.classify_graded(
            is_put=option_type == OPTION_PUT,
            strike=strike,
            spot=spot,
            band=band,
            python=lambda: _classify_graded_python(option_type, strike, spot, band),
        )
    return _classify_graded_python(option_type, strike, spot, band)


def _classify_graded_python(
    option_type: str, strike: Decimal, spot: Decimal, band: Decimal
) -> Moneyness:
    """The verbatim graded arm of :func:`classify` — the live oracle."""
    diff = spot - strike  # > 0 → spot above strike
    if abs(diff) <= band:
        return Moneyness.ATM
    if option_type == OPTION_CALL:
        return Moneyness.ITM if diff > 0 else Moneyness.OTM
    return Moneyness.ITM if diff < 0 else Moneyness.OTM


def derive_strike_step(
    strikes: Iterable[Decimal | None],
    *,
    authoritative_strikes: Iterable[Decimal | None] | None = None,
) -> Decimal | None:
    """Derive the strike-grid step from a ladder of strikes — the smallest positive gap.

    DYNAMIC per ``(underlying, expiry)`` (Golden Rule #1 — never a hardcoded NSE rule):
    sort the DISTINCT positive strikes and take the MINIMUM positive consecutive gap.
    The min (not the mode) is the true base grid — a wider gap is just a missing strike
    in the ladder (a multiple of the base step), never a finer grid. Returns ``None`` when
    fewer than two distinct strikes exist (a single-strike contract set has no inferable
    grid — the caller then reports ``atm_offset = None`` but still classifies ITM/OTM).

    HONESTY CAVEAT (DEC-MONEYNESS-REGISTRY-STEP-001): the "a wider gap is just a
    missing strike" reading holds only when *strikes* is the FULL chain. Fed a SPARSE
    lake-observed subset (e.g. only every 2nd strike's candles were pulled), the min
    OBSERVED gap itself is an integer multiple of the true base step — an
    OVERESTIMATE. When the AUTHORITATIVE vendor chain is available (the metadata-pull
    registry), pass it as *authoritative_strikes*: a chain that yields a step wins,
    refined over the UNION with the observed ladder (see :func:`resolve_strike_step`);
    otherwise the observed ladder is the graceful fallback. Callers that must LABEL
    which source produced the step use :func:`resolve_strike_step` (this function is
    the back-compat ``step``-only facade over it).

    O(K log K) once per ``(underlying, expiry)`` (the sort); the per-bar / per-row
    ``atm_offset`` that consumes the step is O(1).
    """
    if authoritative_strikes is not None:
        return resolve_strike_step(strikes, authoritative_strikes=authoritative_strikes).step
    qualifying = tuple(s for s in strikes if _is_positive_finite(s))
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.strike_step(
            qualifying, python=lambda: _derive_strike_step_python(qualifying)
        )
    return _derive_strike_step_python(qualifying)


def _derive_strike_step_python(qualifying: tuple[Decimal, ...]) -> Decimal | None:
    """The verbatim min-gap arm of :func:`derive_strike_step` — the live oracle."""
    unique = sorted(set(qualifying))
    if len(unique) < _MIN_DISTINCT_STRIKES:
        return None
    # A distinct, ascending ladder ⇒ every consecutive gap is strictly positive, so the
    # minimum gap IS the base grid step (a wider gap is just a missing strike).
    return min(unique[i + 1] - unique[i] for i in range(len(unique) - 1))


class StepConfidence(StrEnum):
    """How trustworthy the derived strike-grid step is — which ladder produced it.

    A :class:`~enum.StrEnum` so it JSON-serialises to its value (``"registry"``) with
    no adapter, mirroring :class:`Moneyness`. DEC-MONEYNESS-REGISTRY-STEP-001.
    """

    #: The step came from the AUTHORITATIVE vendor strike chain (the metadata-pull
    #: registry) — the full ladder, immune to lake sparsity. NOTE (Golden Rule #1):
    #: "authoritative" means *vendor-listed as of the last metadata pull*; whether the
    #: live vendor chain itself is complete is vendor behaviour — UNVERIFIED here.
    REGISTRY = "registry"
    #: The step came from the SPARSE lake-observed ladder only (no registry chain
    #: available) — on a sparsely-pulled expiry it may OVERESTIMATE the true base
    #: step by an integer factor, so surfaces carry an honest low-confidence marker.
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class StrikeStepResult:
    """A derived strike-grid step + the honesty label of the ladder that produced it.

    Attributes
    ----------
    step
        The derived grid step (rupees), or ``None`` when neither ladder had two
        distinct positive strikes (no grid → no ATM±N offset, bucket still resolves).
    confidence
        :attr:`StepConfidence.REGISTRY` when the authoritative chain produced *step*,
        :attr:`StepConfidence.OBSERVED` when the lake-observed ladder did, and
        ``None`` when *step* is ``None`` (no step presented → nothing to qualify).
    """

    step: Decimal | None
    confidence: StepConfidence | None


def resolve_strike_step(
    observed_strikes: Iterable[Decimal | None],
    *,
    authoritative_strikes: Iterable[Decimal | None] | None = None,
) -> StrikeStepResult:
    """Resolve the strike-grid step, preferring the AUTHORITATIVE chain, with honesty.

    The ONE confidence-aware step resolver (DEC-MONEYNESS-REGISTRY-STEP-001, CLAUDE.md
    §11 COMMON — the gap math itself lives only in :func:`derive_strike_step`): when
    *authoritative_strikes* (the FULL registry chain of the ``(underlying, expiry)``)
    yields a step, the step derives over the UNION of the chain and the observed
    ladder (DEC-FNO-HONESTY-PACK-001 — a stale/sparser registry must not overestimate
    against a vendor-grounded lake strike; see the inline note) and wins with
    :attr:`StepConfidence.REGISTRY`; otherwise the *observed_strikes* (the sparse
    lake-observed ladder — today's behaviour, byte-identical step) is the graceful
    fallback, labelled :attr:`StepConfidence.OBSERVED` so ``/db`` + ``/movers`` never
    silently present a possibly-overestimated step. No step from either ladder →
    ``(None, None)``.

    Parameters
    ----------
    observed_strikes
        The lake-observed strike ladder (may be sparse).
    authoritative_strikes
        The full vendor-registry strike chain, or ``None`` when no registry is
        available (a CLI-only-pulled lake / unreadable cache — the caller degrades
        gracefully, never raising).

    Returns
    -------
    StrikeStepResult
        The step + its :class:`StepConfidence` (``None`` confidence iff no step).
    """
    observed = tuple(observed_strikes)
    if authoritative_strikes is not None:
        authoritative = tuple(authoritative_strikes)
        if derive_strike_step(authoritative) is not None:
            # UNION refinement (DEC-FNO-HONESTY-PACK-001): a lake partition directory
            # is itself vendor-grounded proof a strike exists (only a real contract
            # gets candle partitions), so a STALE/SPARSER registry chain (a metadata
            # pull predating newly-listed strikes) must not OVERESTIMATE the step the
            # way the sparse-observed direction did. The step derives over the UNION
            # of both ladders — NOT min(reg_step, obs_step): a cross-ladder gap like
            # registry {24000, 24100} + observed {24050} only surfaces via the union.
            # The label stays REGISTRY (both ladders are vendor evidence), and it is
            # claimed only when the registry chain ITSELF yields a step — a 0/1-strike
            # chain never launders a purely-observed step into high confidence. The
            # union is a superset of the chain, so its step here is never None.
            return StrikeStepResult(
                derive_strike_step((*authoritative, *observed)), StepConfidence.REGISTRY
            )
    observed_step = derive_strike_step(observed)
    if observed_step is not None:
        return StrikeStepResult(observed_step, StepConfidence.OBSERVED)
    return StrikeStepResult(None, None)


def atm_offset(strike: Decimal | None, spot: Decimal | None, step: Decimal | None) -> int | None:
    """Signed ATM±N: how many strike-steps *strike* sits from the at-the-money rung. O(1).

    ``ATM`` is the rung nearest *spot*; the offset is ``round((strike - spot) / step)`` —
    anchor-independent (no assumption about where the grid starts), so ``0`` is the
    nearest strike, ``+N`` is N steps above spot, ``-N`` is N steps below. Returns
    ``None`` when *step* is missing / non-positive or *strike* / *spot* is invalid (no
    grid → no offset, but moneyness is still classifiable).

    Rounds **half toward zero** (``ROUND_HALF_DOWN``) on purpose: a strike exactly
    ``step / 2`` from spot — the INCLUSIVE edge of :func:`classify`'s default ATM band
    (``|spot - strike| <= step / 2``) — then rounds to ``0``, so ``atm_offset == 0`` is
    EXACTLY equivalent to :attr:`Moneyness.ATM`. Rounding half away from zero would put
    that boundary strike at ``±1`` while :func:`classify` still called it ATM — the two
    derived columns would contradict at the midpoint (every on-grid spot at
    ``strike ± step/2`` hits it). Half-toward-zero makes them agree for every step.
    """
    if (
        not _is_positive_finite(step)
        or not _is_positive_finite(strike)
        or not _is_positive_finite(spot)
    ):
        return None
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.atm_offset_paisa(
            strike, spot, step, python=lambda: _atm_offset_python(strike, spot, step)
        )
    return _atm_offset_python(strike, spot, step)


def _atm_offset_python(strike: Decimal, spot: Decimal, step: Decimal) -> int:
    """The verbatim graded arm of :func:`atm_offset` — the live oracle."""
    return int(((strike - spot) / step).to_integral_value(rounding=ROUND_HALF_DOWN))


@dataclass(frozen=True, slots=True)
class MoneynessView:
    """The derived moneyness of one option contract at one spot — a JSON-friendly bundle.

    Attributes
    ----------
    moneyness
        The :class:`Moneyness` bucket.
    atm_offset
        The signed ATM±N (``None`` when no grid could be derived, or N/A / UNKNOWN).
    label
        The trader-facing ``"<bucket> <signed depth>"`` string: ``"ITM -1"`` /
        ``"OTM +10"`` / ``"ATM"`` · ``"ITM"`` / ``"OTM"`` (no grid) · ``"—"`` (a
        future) · ``"?"`` (spot unavailable). ITM is shown negative, OTM positive,
        depth = ``|atm_offset|`` strikes — see :func:`_format_label`.
    """

    moneyness: Moneyness
    atm_offset: int | None
    label: str


def _format_label(moneyness: Moneyness, offset: int | None) -> str:
    """Compose the trader-facing ``"<bucket> <signed depth>"`` label (display-only).

    Bucket first, then HOW FAR in/out of the money the contract is, in strikes:
    ``"ITM -1"`` / ``"ITM -10"`` (in the money by N strikes) · ``"OTM +1"`` /
    ``"OTM +10"`` (out of the money by N strikes) · ``"ATM"`` (at the money) ·
    ``"ITM"`` / ``"OTM"`` (no strike grid derivable → bucket only) · ``"—"`` (a
    future) · ``"?"`` (spot unavailable). The sign tracks the BUCKET — ITM negative,
    OTM positive — so it reads the same for calls AND puts (a deep-ITM put is still
    ``"ITM -N"``, not ``"ITM +N"``); the depth is ``|offset|`` strikes from the
    at-the-money rung.
    """
    if moneyness is Moneyness.NA:
        return "—"
    if moneyness is Moneyness.UNKNOWN:
        return "?"
    if moneyness is Moneyness.ATM:
        return "ATM"
    if offset is None:
        return moneyness.value  # no grid — just ITM / OTM
    sign = "-" if moneyness is Moneyness.ITM else "+"
    return f"{moneyness.value} {sign}{abs(offset)}"


#: The buckets for which a signed ATM±N offset is meaningful (an option with a real spot).
_GRADED: Final[frozenset[Moneyness]] = frozenset({Moneyness.ITM, Moneyness.ATM, Moneyness.OTM})


def moneyness_view(
    option_type: str | None,
    strike: Decimal | None,
    spot: Decimal | None,
    step: Decimal | None,
) -> MoneynessView:
    """Bundle moneyness + ATM±N + label for one option at one spot. O(1), never raises.

    The ATM band is ``step / 2`` when a grid is known, so the rung nearest spot reads
    :attr:`Moneyness.ATM`; without a grid it falls back to exact-equality ATM.
    """
    band = (step / 2) if _is_positive_finite(step) else None
    bucket = classify(option_type, strike, spot, atm_band=band)
    offset = atm_offset(strike, spot, step) if bucket in _GRADED else None
    return MoneynessView(bucket, offset, _format_label(bucket, offset))


__all__ = [
    "OPTION_CALL",
    "OPTION_PUT",
    "Moneyness",
    "MoneynessView",
    "StepConfidence",
    "StrikeStepResult",
    "atm_offset",
    "classify",
    "derive_strike_step",
    "moneyness_view",
    "resolve_strike_step",
]
