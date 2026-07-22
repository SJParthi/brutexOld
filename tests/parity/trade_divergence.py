"""Trade-list divergence comparison — the belt oracle spot-check utility.

Slice-3 scaffolding (DEC-SWEEP-BELT-RUST-001, design-of-record §2.2 / gate G4):
the belt PR's burn-in + permanent-sample oracle compares each kernel-served
finalist's trade list against the pure-Python :func:`simulate_trades` oracle
"trade-by-trade on ALL ``ClosedTrade`` fields incl. ``vix_at_entry/exit``
(Decimal equality exact); ANY mismatch ⇒ the ORACLE trades ship". This module
supplies the pure, tested comparison + report-formatting half of that
mechanism so the belt PR only wires it.

Exactness law
-------------
* Every field named by :func:`dataclasses.fields` on
  :class:`~brutex.metrics.types.ClosedTrade` is compared — a future field is
  automatically covered (never a hand-maintained list).
* ``Decimal`` fields (the VIX stamps) compare REPRESENTATION-exact
  (``str(a) == str(b)``): ``Decimal("19.5")`` vs ``Decimal("19.50")`` is a
  reported divergence even though the two compare numerically equal —
  "bit-identical" per the design's parity law, strictly stronger than ``==``.
* Everything else compares with plain ``==`` (int paisa, tz-aware datetimes,
  ``Side``). NOTE: a future ``float`` field would also fall to plain ``==`` —
  no ``-0.0`` vs ``0.0`` distinction and no NaN bit-exactness; add a bit-exact
  (``struct.pack``-style) branch here if one is ever introduced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields
from decimal import Decimal

from brutex.metrics.types import ClosedTrade

__all__ = ["render_divergence_report", "trade_divergences"]


def _field_exact_equal(a: object, b: object) -> bool:
    """True iff *a* and *b* are exactly equal under the module's exactness law."""
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return isinstance(a, Decimal) and isinstance(b, Decimal) and str(a) == str(b)
    return bool(a == b)


def trade_divergences(
    expected: Sequence[ClosedTrade],
    actual: Sequence[ClosedTrade],
    *,
    label: str = "trade",
) -> list[str]:
    """Compare two trade lists field-by-field; return one line per divergence.

    An empty return value means the two lists are exactly equal (same length,
    every field of every trade exact per the module's exactness law).

    Parameters
    ----------
    expected
        The oracle (Python ``simulate_trades``) trade list.
    actual
        The candidate (kernel-served belt) trade list.
    label
        Prefix used in each divergence line (e.g. the finalist's rank/mask).

    Returns
    -------
    list[str]
        Human-readable divergence lines; ``[]`` iff exactly equal. A length
        mismatch is reported AND the shared prefix is still compared, so the
        report names the first diverging field even on truncated lists.
    """
    out: list[str] = []
    if len(expected) != len(actual):
        out.append(f"{label}: length mismatch expected={len(expected)} actual={len(actual)}")
    for i, (exp, act) in enumerate(zip(expected, actual, strict=False)):
        for f in fields(ClosedTrade):
            ev = getattr(exp, f.name)
            av = getattr(act, f.name)
            if not _field_exact_equal(ev, av):
                out.append(f"{label}[{i}].{f.name}: expected={ev!r} actual={av!r}")
    return out


def render_divergence_report(divergences: Sequence[str], *, limit: int = 20) -> str:
    """Format divergence lines into one bounded report string.

    Parameters
    ----------
    divergences
        Lines from :func:`trade_divergences` (or any harness compare).
    limit
        Maximum lines rendered verbatim; the remainder is summarised as a
        count (never silently dropped — the truncation is NAMED).

    Returns
    -------
    str
        ``"no divergences"`` for an empty input, else up to *limit* lines
        joined by newlines plus an honest ``"... N more"`` suppression note.
    """
    if limit < 1:
        raise ValueError(f"render_divergence_report: limit must be >= 1, got {limit}")
    if not divergences:
        return "no divergences"
    shown = list(divergences[:limit])
    hidden = len(divergences) - len(shown)
    if hidden > 0:
        shown.append(f"... {hidden} more divergence line(s) suppressed (limit={limit})")
    return "\n".join(shown)
