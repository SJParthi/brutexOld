"""Money formatting — the single source of truth for rupee display.

Money amounts at the **cost + display boundary** are int64 **paisa** (an exact
integer; 1 rupee = 100 paisa) — see ``brutex.costs``. This module is the only
place that converts paisa back to a human-facing rupee value, and it enforces
the operator rule (2026-05-29, ``DEC-MONEY-2DP-001``): **money is displayed with
at most two decimal places.**

Price storage is separate (``DEC-PAISA-DOC-RECONCILE-001``): OHLC / spot /
option **prices** in the lake are exact :class:`~decimal.Decimal` rupees, NOT
paisa (``data/candle_fields.to_price`` -> ``CandleBar``; Parquet ``Float64``;
see ``brutex/data/models.py``). int64 paisa is the unit of the costs calculator
plus this display module, never of price storage.

The 2dp rule applies to MONEY ONLY. Statistical metrics (Sharpe, Deflated
Sharpe, p-values, CPCV scores) keep full precision — rounding them would corrupt
strategy ranking (Golden Rule #10).

Rust seam (``DEC-CONFIG-RUST-LEAVES-001``, MIGRATION.md #1943 — slice T35):
when — and ONLY when — ``BRUTEX_CORE_RUST_MONEY`` is set truthy, the four
public laws route through the default-ON, burn-in-checked, fail-closed
:mod:`brutex.money_rust` facade to the ``rust/brutex-core`` crate (exact
decimal-STRING arithmetic; strings only cross the FFI). The flag-OFF
default is the verbatim ``_*_python`` oracle below — byte-identical AND
crate-untouched (bomb-crate spy-pinned). The oracles are the LIVE parity
reference every degrade path serves; honesty banner (ADR-0020, mandatory
verbatim): expected payoff ≈ 0 — conversion is for uniformity, not speed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from brutex.money_rust import (
    money_rust_enabled,
    route_format_inr,
    route_paisa_to_rupees,
    route_round_money,
    route_rupees_to_paisa,
)

#: Quantum for money rounding — two decimal places, always (DEC-MONEY-2DP-001).
#: ``# pragma: no mutate`` — mutmut's string mutation yields ``Decimal("XX0.01XX")``
#: which raises ``InvalidOperation`` at import, so it cannot represent any real
#: behavioural change (it just breaks the module load); the 2-decimal behaviour it
#: governs is fully pinned by ``test_money.py`` (``1.005→1.01`` / ``1.004→1.00``).
#: Excluding it lets ``money.py`` reach a clean 100% mutation kill (DEC-MUTATION-MONEY-001).
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")  # pragma: no mutate

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
    if not money_rust_enabled():
        return _round_money_python(rupees)
    return route_round_money(rupees, _round_money_python)


def _round_money_python(rupees: Decimal) -> Decimal:
    """The verbatim :func:`round_money` body — the LIVE Decimal oracle.

    Token-for-token the pre-seam public body (DEC-CONFIG-RUST-LEAVES-001);
    every flag-OFF / ineligible / degraded / burn-in call serves this.
    """
    return rupees.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def rupees_to_paisa(rupees: Decimal) -> int:
    """Convert an exact rupee :class:`~decimal.Decimal` to int64 paisa.

    The forward complement of :func:`paisa_to_rupees` and the single source of
    truth for the lake-boundary rupees→paisa snap (CLAUDE.md §11 COMMON): the
    rupee price is first snapped to the 2-decimal tick grid via
    :func:`round_money` (``ROUND_HALF_UP``, never truncation —
    ``DEC-SPOT-PRICE-2DP-001``), then the ``× 100`` is exact in
    :class:`~decimal.Decimal`, so the resulting ``int`` is loss-free.

    Parameters
    ----------
    rupees
        A price / money amount in rupees at arbitrary precision.

    Returns
    -------
    int
        The amount in int64 paisa.
    """
    if not money_rust_enabled():
        return _rupees_to_paisa_python(rupees)
    return route_rupees_to_paisa(rupees, _rupees_to_paisa_python)


def _rupees_to_paisa_python(rupees: Decimal) -> int:
    """The verbatim :func:`rupees_to_paisa` body over the extracted oracle.

    Composes :func:`_round_money_python` (not the routed public) so the
    oracle chain is PURE Python end-to-end — a burn-in comparison must
    never recurse into the seam.
    """
    return int(_round_money_python(rupees) * _PAISA_PER_RUPEE)


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
    if not money_rust_enabled():
        return _paisa_to_rupees_python(paisa)
    return route_paisa_to_rupees(paisa, _paisa_to_rupees_python)


def _paisa_to_rupees_python(paisa: int) -> Decimal:
    """The verbatim :func:`paisa_to_rupees` body over the extracted oracle."""
    return _round_money_python(Decimal(paisa) / _PAISA_PER_RUPEE)


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
    if not money_rust_enabled():
        return _format_inr_python(paisa)
    return route_format_inr(paisa, _format_inr_python)


def _format_inr_python(paisa: int) -> str:
    """The verbatim :func:`format_inr` body over the extracted oracle."""
    return f"₹{_paisa_to_rupees_python(paisa):,.2f}"


__all__ = ["format_inr", "paisa_to_rupees", "round_money", "rupees_to_paisa"]
