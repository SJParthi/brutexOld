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

Pure (stdlib + :class:`~decimal.Decimal`). It **never raises** — a future (no strike), an
absent spot, or any junk input classifies as :attr:`Moneyness.NA` / :attr:`Moneyness.UNKNOWN`
so a per-bar browse or a movers render never crashes on one bad value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_DOWN, Decimal
from enum import StrEnum
from typing import Final, TypeGuard


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
    diff = spot - strike  # > 0 → spot above strike
    if abs(diff) <= band:
        return Moneyness.ATM
    if option_type == OPTION_CALL:
        return Moneyness.ITM if diff > 0 else Moneyness.OTM
    return Moneyness.ITM if diff < 0 else Moneyness.OTM


def derive_strike_step(strikes: Iterable[Decimal | None]) -> Decimal | None:
    """Derive the strike-grid step from a ladder of strikes — the smallest positive gap.

    DYNAMIC per ``(underlying, expiry)`` (Golden Rule #1 — never a hardcoded NSE rule):
    sort the DISTINCT positive strikes and take the MINIMUM positive consecutive gap.
    The min (not the mode) is the true base grid — a wider gap is just a missing strike
    in the ladder (a multiple of the base step), never a finer grid. Returns ``None`` when
    fewer than two distinct strikes exist (a single-strike contract set has no inferable
    grid — the caller then reports ``atm_offset = None`` but still classifies ITM/OTM).

    O(K log K) once per ``(underlying, expiry)`` (the sort); the per-bar / per-row
    ``atm_offset`` that consumes the step is O(1).
    """
    unique = sorted({s for s in strikes if _is_positive_finite(s)})
    if len(unique) < _MIN_DISTINCT_STRIKES:
        return None
    # A distinct, ascending ladder ⇒ every consecutive gap is strictly positive, so the
    # minimum gap IS the base grid step (a wider gap is just a missing strike).
    return min(unique[i + 1] - unique[i] for i in range(len(unique) - 1))


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
    "atm_offset",
    "classify",
    "derive_strike_step",
    "moneyness_view",
]
