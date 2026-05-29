"""Money formatting — the single source of truth for rupee display.

Track 1 stores every price as int64 **paisa** (CLAUDE.md §6: Groww emits
prices as rupee floats; the adapter multiplies by 100 at the parse boundary to
keep an exact integer). This module is the only place that converts paisa back
to a human-facing rupee value, and it enforces the operator rule (2026-05-29,
``DEC-MONEY-2DP-001``): **money is displayed with at most two decimal places.**

The rule applies to MONEY ONLY. Statistical metrics (Sharpe, Deflated Sharpe,
p-values, CPCV scores) keep full precision — rounding them would corrupt
strategy ranking (Golden Rule #10). Internal storage stays int64 paisa; only
the display boundary rounds.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

#: Quantum for money rounding — two decimal places, always (DEC-MONEY-2DP-001).
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")

#: Paisa per rupee.
_PAISA_PER_RUPEE: Final[Decimal] = Decimal(100)


def round_money(rupees: Decimal) -> Decimal:
    """Round a rupee :class:`~decimal.Decimal` to exactly two places (half-up).

    Parameters
    ----------
    rupees
        A rupee amount at arbitrary precision.

    Returns
    -------
    Decimal
        The amount quantised to two decimal places, rounding half away from zero.
    """
    return rupees.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def paisa_to_rupees(paisa: int) -> Decimal:
    """Convert int64 paisa to an exact two-place rupee :class:`~decimal.Decimal`.

    Parameters
    ----------
    paisa
        The amount in paisa (the canonical int64 storage unit).

    Returns
    -------
    Decimal
        The amount in rupees, quantised to two decimal places.
    """
    return round_money(Decimal(paisa) / _PAISA_PER_RUPEE)


def format_inr(paisa: int) -> str:
    """Format int64 paisa as a ``₹`` string with grouping and two decimals.

    Parameters
    ----------
    paisa
        The amount in paisa.

    Returns
    -------
    str
        A display string such as ``"₹1,234.56"`` — always exactly two decimals.
    """
    return f"₹{paisa_to_rupees(paisa):,.2f}"


__all__ = ["format_inr", "paisa_to_rupees", "round_money"]
