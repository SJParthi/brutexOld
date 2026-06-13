"""F&O Universe Explorer — bulk-pull target selection (pure, offline).

The pure core behind the **bulk** pull buttons (① bulk-indices, ② bulk-stocks;
DEC-UNIVERSE-DASHBOARD-001): given the instrument snapshot it returns the
concrete :class:`~brutex.models.Instrument` objects to fan a spot pull over for
one explorer *kind* (``"index"`` / ``"stock"``).

Where :func:`brutex.dashboard.universe_query.list_underlyings` returns the
JSON **rows** the grid renders, this returns the **instruments themselves** —
exactly what :func:`brutex.dashboard.pull_controller.pull_all_spot` consumes. It
reuses the single classification rule
(:func:`brutex.dashboard.universe_query.classify_instrument`, CLAUDE.md §11
COMMON — no re-derivation of the index-vs-stock / F&O-eligibility split), so
the bulk pull selects exactly the same universe the grid shows.

The dashboard FastAPI routes in :mod:`brutex.dashboard.app` (coverage-excluded,
FastAPI-dependent) stay thin wrappers that delegate the selection here so it is
100% unit-testable offline.
"""

from __future__ import annotations

from collections.abc import Iterable

from brutex.dashboard.universe_query import EXPLORER_KINDS, classify_instrument
from brutex.models import Exchange, Instrument
from brutex.universe import build_fno_eligible_underlyings


def select_pull_instruments(
    snapshot: Iterable[Instrument],
    *,
    kind: str,
    fno_eligible: frozenset[tuple[Exchange, str]] | None = None,
) -> list[Instrument]:
    """Return the admitted :class:`Instrument` objects for one explorer *kind*.

    Filters *snapshot* to exactly the instruments the explorer classifies as
    *kind* (``"index"`` → every admitted ``IDX``; ``"stock"`` → every
    F&O-eligible ``EQ``), returning the instruments themselves (sorted by
    ``trading_symbol`` for a stable, deterministic pull order) — the bulk-pull
    target list for :func:`~brutex.dashboard.pull_controller.pull_all_spot`.

    Parameters
    ----------
    snapshot
        Any iterable of :class:`~brutex.models.Instrument` — typically the
        IDX + EQ candidate rows from
        :meth:`~brutex.data.instrument_store.InstrumentStore.list_index_equity_instruments`.
    kind
        ``"index"`` or ``"stock"`` (required — a bulk pull is always one kind).
    fno_eligible
        Pre-computed NSE F&O-eligible ``(exchange, symbol)`` set. When ``None``
        it is derived from *snapshot* (which must then carry the derivative
        legs); supplying it lets the caller pass a candidates-only snapshot
        plus the cheaply-queried eligible set
        (DEC-INSTRUMENT-UNIVERSE-FAST-001) — the rule is unchanged either way.

    Returns
    -------
    list[Instrument]
        The admitted instruments of *kind*, sorted by ``trading_symbol``.

    Raises
    ------
    ValueError
        If *kind* is not ``"index"`` or ``"stock"``.
    """
    if kind not in EXPLORER_KINDS:
        raise ValueError(f"kind must be one of {EXPLORER_KINDS}, got {kind!r}")

    instruments = list(snapshot)
    if fno_eligible is None:
        fno_eligible = build_fno_eligible_underlyings(instruments)
    selected = [
        instrument
        for instrument in instruments
        if classify_instrument(instrument, fno_eligible) == kind
    ]
    selected.sort(key=lambda instrument: instrument.symbol_name)
    return selected


__all__ = ["select_pull_instruments"]
