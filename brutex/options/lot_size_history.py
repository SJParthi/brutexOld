"""Per-date options lot size lookup for NIFTY / BANKNIFTY / SENSEX.

Per `docs/track2/TRACK2_OPTIONS_SPEC.md` §8.3 — lot sizes changed 7+
times during the 2021-2026 backfill window. Using a single lot would
mis-size positions by 3-7× in places. This module is the per-date
lookup the engine consults at every backtest trade.

## Verified history (citation-grounded 2026-05-16)

| Effective from | NIFTY 50 | BANKNIFTY | SENSEX | Source |
|---|---|---|---|---|
| ≤ Jun 2021 | 75 | 25 | 10 | NSE historic |
| Jul 2021 expiry | 50 | 25 | 10 | NSE bulletin |
| 30-Jun-2023 | 50 | 15 | 10 | Zerodha Marketintel #353422 |
| 26-Apr-2024 | 25 | 15 | 10 | NSE FAOP circular |
| 20-Nov-2024 | 75 | 30 | 20 | SEBI/HO/MRD-PoD2/CIR/P/2024/00181 |
| Jul 2025 (transitional) | 75 | 35 | 20 | NSE bulletins |
| 30-Dec-2025 | 75 | 30 | 20 | NSE FAOP70616 |
| Jan 2026 onwards | **65** | **30** | **20** | NSE Jan-2026 circular |

## Encoding

A list of `(effective_date, lot_size)` tuples per instrument, sorted
ascending. Lookup uses binary search (O(log N)) where N=8 transitions,
so effectively O(1). For each (instrument, date), return the lot size
in effect on that date.

## Lookup semantics

`get_lot_size(instrument, date)`:
- Finds the LATEST transition where `effective_date <= date`.
- Returns that transition's lot size.
- For dates before the first transition, returns the first
  recorded value (pre-Jul-2021 defaults).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final

from brutex import optcost_rust


class OptionsUnderlying(Enum):
    """The 3 underlyings supported in Track 2."""

    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"
    SENSEX = "SENSEX"


# Per-instrument lot-size history, sorted ascending by effective_date.
# Each tuple = (effective_date, lot_size).
# Source-cites in module docstring above.

_LOT_HISTORY_NIFTY: Final[tuple[tuple[date, int], ...]] = (
    (date(2021, 1, 1), 75),  # pre-Jul 2021 default (extended back to start of backfill)
    (date(2021, 7, 29), 50),  # Jul 2021 expiry rebasing
    (date(2024, 4, 26), 25),  # 26-Apr-2024
    (date(2024, 11, 20), 75),  # 20-Nov-2024 SEBI Rs 15L notional rule
    (date(2026, 1, 1), 65),  # Jan 2026 onwards (current)
)

_LOT_HISTORY_BANKNIFTY: Final[tuple[tuple[date, int], ...]] = (
    (date(2021, 1, 1), 25),  # pre-Jun 2023
    (date(2023, 6, 30), 15),  # 30-Jun-2023
    (date(2024, 11, 20), 30),  # 20-Nov-2024
    (date(2025, 7, 1), 35),  # Jul 2025 transitional
    (date(2025, 12, 30), 30),  # 30-Dec-2025 EOD revert
)

_LOT_HISTORY_SENSEX: Final[tuple[tuple[date, int], ...]] = (
    (date(2021, 1, 1), 10),  # pre-Nov 2024
    (date(2024, 11, 20), 20),  # 20-Nov-2024
)


_HISTORY_BY_UNDERLYING: Final[dict[OptionsUnderlying, tuple[tuple[date, int], ...]]] = {
    OptionsUnderlying.NIFTY: _LOT_HISTORY_NIFTY,
    OptionsUnderlying.BANKNIFTY: _LOT_HISTORY_BANKNIFTY,
    OptionsUnderlying.SENSEX: _LOT_HISTORY_SENSEX,
}


@dataclass(frozen=True)
class LotSizeChange:
    """One lot size change event."""

    instrument: OptionsUnderlying
    effective_date: date
    lot_size: int


def get_lot_size(instrument: OptionsUnderlying, on_date: date) -> int:
    """Look up the lot size for `instrument` on `on_date`.

    Args:
        instrument: NIFTY, BANKNIFTY, or SENSEX.
        on_date: Trading date to look up.

    Returns:
        Lot size in effect on `on_date`.

    Lookup is O(log N) binary search where N = number of historical
    transitions (currently ≤ 5 per instrument). Effectively O(1).

    Raises:
        ValueError: if `on_date` is before the start of our recorded
            history (Jan 1, 2021).

    DEC-OPTCOST-TAILS-RUST-001: with ``BRUTEX_OPTCOST_TAILS_RUST`` truthy
    the bisect runs on the ``fno_math`` ``optcost_history_lookup`` kernel
    via :mod:`brutex.optcost_rust` (the pre-history ``ValueError`` keeps its
    exact message bytes, composed Python-side from this same table); flag
    OFF — the default — is byte-identical.
    """
    history = _HISTORY_BY_UNDERLYING[instrument]
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.history_value(
            "lot_size",
            history,
            on_date,
            prehistory_message=lambda: (
                f"on_date {on_date} is before recorded history start "
                f"({history[0][0]} for {instrument.value})"
            ),
            python=lambda: _get_lot_size_python(history, on_date, instrument),
        )
    return _get_lot_size_python(history, on_date, instrument)


def _get_lot_size_python(
    history: tuple[tuple[date, int], ...],
    on_date: date,
    instrument: OptionsUnderlying,
) -> int:
    """The verbatim Python arm of :func:`get_lot_size` — the live oracle."""
    if on_date < history[0][0]:
        raise ValueError(
            f"on_date {on_date} is before recorded history start "
            f"({history[0][0]} for {instrument.value})"
        )
    dates_only = [t[0] for t in history]
    # bisect_right finds the index of the first transition AFTER on_date;
    # we want the one BEFORE or AT on_date, which is index - 1.
    idx = bisect_right(dates_only, on_date) - 1
    return history[idx][1]


def all_transitions(instrument: OptionsUnderlying) -> list[LotSizeChange]:
    """Return all recorded lot-size transitions for `instrument` in order.

    Useful for backfill validation and for displaying a timeline.
    """
    return [
        LotSizeChange(instrument=instrument, effective_date=d, lot_size=lot)
        for (d, lot) in _HISTORY_BY_UNDERLYING[instrument]
    ]


def all_current_lot_sizes(on_date: date) -> dict[OptionsUnderlying, int]:
    """Return current lot sizes for all 3 underlyings on `on_date`."""
    return {inst: get_lot_size(inst, on_date) for inst in OptionsUnderlying}
