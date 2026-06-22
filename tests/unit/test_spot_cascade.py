"""Unit tests for the ``/db`` SPOT symbol→interval cascade (DEC-DB-SPOT-CASCADE-001).

Pure + deterministic, no I/O. The contract: grouping the discovered SPOT datasets
(``list_candle_datasets`` non-F&O shape) into symbol → interval makes picking a
symbol + a STORED timeframe one O(1) cascade away instead of a flat ~750-row
dropdown. Only intervals with a real on-disk partition are offered (no phantom TFs,
Golden Rule #1); the brute-force indices (``INDEX`` segment) float above the CASH
reference equities.
"""

from __future__ import annotations

from typing import Any

from brutex.dashboard.spot_cascade import (
    SpotSymbol,
    build_spot_cascade,
    symbol_to_json,
)


def _ds(
    symbol: str,
    *,
    segment: str = "CASH",
    interval: str = "1minute",
    exchange: str = "NSE",
) -> dict[str, Any]:
    """One ``list_candle_datasets``-shaped (non-F&O) row."""
    return {
        "kind": "spot",
        "exchange": exchange,
        "segment": segment,
        "symbol": symbol,
        "interval": interval,
    }


def test_empty_datasets_is_empty_index() -> None:
    idx = build_spot_cascade([])
    assert idx.symbols() == ()


def test_indices_sort_before_cash_then_alpha() -> None:
    # An INDEX segment floats above CASH; within a rank, symbol-alphabetical.
    idx = build_spot_cascade(
        [
            _ds("NSE-RELIANCE", segment="CASH"),
            _ds("NSE-NIFTY", segment="INDEX"),
            _ds("NSE-ASIANPAINT", segment="CASH"),
            _ds("BSE-SENSEX", segment="INDEX"),
        ]
    )
    ordered = [(s.segment, s.symbol) for s in idx.symbols()]
    # Both INDEX rows first (alpha by symbol: BSE-SENSEX < NSE-NIFTY), then CASH alpha.
    assert ordered == [
        ("INDEX", "BSE-SENSEX"),
        ("INDEX", "NSE-NIFTY"),
        ("CASH", "NSE-ASIANPAINT"),
        ("CASH", "NSE-RELIANCE"),
    ]


def test_intervals_aggregated_per_symbol_sorted() -> None:
    # A symbol present at 1minute AND the derived 1day collapses to ONE leaf with both.
    idx = build_spot_cascade(
        [
            _ds("NSE-NIFTY", segment="INDEX", interval="1minute"),
            _ds("NSE-NIFTY", segment="INDEX", interval="1day"),
        ]
    )
    (leaf,) = idx.symbols()
    assert leaf.symbol == "NSE-NIFTY"
    assert leaf.intervals == ("1day", "1minute")  # ascending, deduped


def test_same_symbol_distinct_exchange_are_distinct_leaves() -> None:
    # The (exchange, segment, symbol) triple keys the leaf — the exchange tie-break in
    # the sort key is exercised when symbol + segment match but exchange differs.
    idx = build_spot_cascade(
        [
            _ds("DUP", segment="CASH", exchange="NSE"),
            _ds("DUP", segment="CASH", exchange="BSE"),
        ]
    )
    leaves = [(s.exchange, s.symbol) for s in idx.symbols()]
    assert leaves == [("BSE", "DUP"), ("NSE", "DUP")]  # exchange breaks the tie


def test_symbol_label_is_the_bare_symbol() -> None:
    sym = SpotSymbol(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", intervals=("1minute",))
    assert sym.label == "NSE-NIFTY"


def test_symbol_to_json_projection() -> None:
    sym = SpotSymbol(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-RELIANCE",
        intervals=("1day", "1minute"),
    )
    assert symbol_to_json(sym) == {
        "exchange": "NSE",
        "segment": "CASH",
        "symbol": "NSE-RELIANCE",
        "label": "NSE-RELIANCE",
        "intervals": ["1day", "1minute"],
    }
