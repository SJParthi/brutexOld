"""Cost-scope predicate — the ONE source of truth for "is this instrument cost-free?".

DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001 (MIGRATION.md #1060). The operator-locked
rule:

* **COST-FREE (signal-only — zero brokerage, zero STT/taxes, zero tick
  slippage; the ONE worst-case fill law still prices the trade off the fill
  bar's adverse extreme, DEC-FILL-WORST-CASE-ONLY-001 — cost-free means no
  CHARGE stack, never a different fill law):** ONLY **INDEX SPOT**
  (NIFTY / BANKNIFTY / SENSEX) and **INDEX FUTURES** (NIFTY futures). An
  index has no order book position of its own — the spot brute-force
  research engine trades a pure directional price move — and index futures
  are admitted to the dynamic-strategy track as signal-only too
  (DEC-NIFTY-DYNAMIC-STRATEGY-001 / ADR-0012).
* **COST-BEARING (full brokerage + STT + tick slippage + liquidity apply, on
  top of the SAME worst-case fill anchors):** EVERYTHING ELSE — stock spot,
  stock futures, stock options, AND **index options**. An index OPTION is a
  tradable premium instrument with a real bid/ask, so it is priced through
  the Track-2 cost calculator (:mod:`brutex.costs.calculator`); a STOCK in
  any segment is likewise a tradable order-book instrument.

The single-source-of-truth predicate::

    is_cost_free(instrument) == (
        instrument is an INDEX          # underlying is a brute-force index
        AND its segment is SPOT/CASH or FUTURES  # NOT an option
    )

Concretely, on the canonical :class:`~brutex.models.Instrument`:

* an **index spot** is ``instrument_type == IDX`` (``is_index`` True);
* an **index future** is ``instrument_type == FUT`` whose ``underlying_symbol``
  is one of the brute-force indexes (:data:`~brutex.universe.BRUTE_FORCE_INDEX_UNDERLYINGS`);
* an **index option** (``CE``/``PE`` on an index underlying) is **cost-bearing** —
  the ``is_option`` guard rejects it;
* a **stock** in any segment is **cost-bearing** — its underlying is not an index.

Pure, O(1) (a fixed set of enum + frozenset membership checks), no I/O, no global
state. This module deliberately imports only :mod:`brutex.models` +
:mod:`brutex.universe` (both dependency-light), so it can gate a cost/fill
application at any call site without dragging in the heavy Track-2 calculator
(the deprecated execution simulator this note once also named was deleted per
DEC-EXECUTION-TRIO-DELETE-001).
"""

from __future__ import annotations

from brutex.models import Instrument, InstrumentType
from brutex.universe import BRUTE_FORCE_INDEX_UNDERLYINGS

__all__ = ["is_cost_bearing", "is_cost_free", "is_index_future"]


def is_index_future(instrument: Instrument) -> bool:
    """Return True iff *instrument* is a FUTURE on a brute-force INDEX underlying.

    An index future carries ``instrument_type == FUT`` (so the model's
    :attr:`~brutex.models.Instrument.is_index` — which is IDX-only — is False),
    but its ``underlying_symbol`` is one of NIFTY / BANKNIFTY / SENSEX. This is
    the discriminator the cost-scope rule needs: a NIFTY future is cost-free, a
    single-stock future (e.g. RELIANCE FUT) is cost-bearing.

    O(1): one enum compare + one frozenset membership test.
    """
    return (
        instrument.instrument == InstrumentType.FUT
        and (instrument.exch_id, instrument.underlying_symbol) in BRUTE_FORCE_INDEX_UNDERLYINGS
    )


def is_cost_free(instrument: Instrument) -> bool:
    """Return True iff *instrument* is priced signal-only (ZERO transaction costs).

    The operator-locked cost-scope rule (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001):
    cost-free is **INDEX SPOT** or **INDEX FUTURES** ONLY. Everything else —
    every stock (spot / future / option) and every INDEX OPTION — is
    cost-bearing.

    An index OPTION is explicitly EXCLUDED even though its underlying is an
    index: :attr:`~brutex.models.Instrument.is_option` short-circuits to False,
    because an option is a tradable premium instrument with a real order book and
    must carry the full brokerage + STT + slippage + fill-scenario stack.

    Pure, O(1). No I/O, no global state.

    Parameters
    ----------
    instrument
        The canonical :class:`~brutex.models.Instrument`.

    Returns
    -------
    bool
        True for index spot (IDX) and index futures (FUT on an index
        underlying); False for index options, stock spot, stock futures, and
        stock options.
    """
    if instrument.is_option:
        # Any option (incl. an INDEX option) is cost-bearing — real premium,
        # real order book. This guard makes the "index options are NOT cost-free"
        # rule explicit and unmissable.
        return False
    # Index SPOT (IDX) OR an index FUTURE — the only two cost-free classes.
    return instrument.is_index or is_index_future(instrument)


def is_cost_bearing(instrument: Instrument) -> bool:
    """Return True iff *instrument* carries the full transaction-cost stack.

    The exact complement of :func:`is_cost_free`: every stock (spot / future /
    option) and every index OPTION bears brokerage + STT + slippage + liquidity +
    best/avg/worst-case fills. O(1).
    """
    return not is_cost_free(instrument)
