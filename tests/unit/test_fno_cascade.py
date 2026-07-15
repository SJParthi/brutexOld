"""Unit tests for the ``/db`` F&O symbol→expiry→contract cascade (DEC-DB-FNO-CASCADE-001).

Pure + deterministic, no I/O. The contract: grouping the decoded F&O datasets
(``list_fno_candle_datasets`` shape) into underlying → expiry → contract makes a
precise strike one O(1) cascade away instead of a flat thousand-row dropdown. An
undecodable partition is surfaced (``unparsed``), never hidden (Golden Rule #1).
"""

from __future__ import annotations

from typing import Any

from brutex.dashboard.fno_cascade import (
    FnoContract,
    build_expiry_counts,
    build_fno_cascade,
    contract_to_json,
)


def _ds(
    symbol: str,
    *,
    underlying: str | None,
    expiry: str | None,
    strike: str | None,
    option_type: str | None,
    instrument_type: str | None,
    interval: str = "1minute",
    exchange: str = "NSE",
) -> dict[str, Any]:
    """One ``list_fno_candle_datasets``-shaped row."""
    return {
        "kind": "fno",
        "exchange": exchange,
        "segment": "FNO",
        "symbol": symbol,
        "interval": interval,
        "underlying": underlying,
        "expiry": expiry,
        "strike": strike,
        "instrument_type": instrument_type,
        "option_type": option_type,
    }


def _opt(
    symbol: str, underlying: str, expiry: str, strike: str, opt: str, **kw: Any
) -> dict[str, Any]:
    return _ds(
        symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=opt,
        instrument_type=opt,
        **kw,
    )


def _fut(symbol: str, underlying: str, expiry: str, **kw: Any) -> dict[str, Any]:
    return _ds(
        symbol,
        underlying=underlying,
        expiry=expiry,
        strike=None,
        option_type=None,
        instrument_type="FUT",
        **kw,
    )


def test_empty_datasets_is_empty_index() -> None:
    idx = build_fno_cascade([])
    assert idx.underlyings() == ()
    assert idx.expiries("NIFTY") == ()  # never raises on unknown
    assert idx.contracts("NIFTY", "2020-02-06") == ()
    assert idx.unparsed == ()


def test_underlyings_are_distinct_and_sorted() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-06Feb20-12250-PE", "NIFTY", "2020-02-06", "12250", "PE"),
            _opt("NSE-BANKNIFTY-06Feb20-30000-CE", "BANKNIFTY", "2020-02-06", "30000", "CE"),
            _opt("NSE-NIFTY-06Feb20-12300-CE", "NIFTY", "2020-02-06", "12300", "CE"),
        ]
    )
    assert idx.underlyings() == ("BANKNIFTY", "NIFTY")  # sorted, deduped


def test_expiries_scoped_to_underlying_and_chronological() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-27Feb20-12250-PE", "NIFTY", "2020-02-27", "12250", "PE"),
            _opt("NSE-NIFTY-06Feb20-12250-PE", "NIFTY", "2020-02-06", "12250", "PE"),
            _opt("NSE-BANKNIFTY-13Feb20-30000-CE", "BANKNIFTY", "2020-02-13", "30000", "CE"),
        ]
    )
    # ISO YYYY-MM-DD sorts lexically == chronologically; scoped per underlying.
    assert idx.expiries("NIFTY") == ("2020-02-06", "2020-02-27")
    assert idx.expiries("BANKNIFTY") == ("2020-02-13",)


def test_contracts_sorted_futures_first_then_strike_then_type() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-06Feb20-12300-PE", "NIFTY", "2020-02-06", "12300", "PE"),
            _opt("NSE-NIFTY-06Feb20-12300-CE", "NIFTY", "2020-02-06", "12300", "CE"),
            _opt("NSE-NIFTY-06Feb20-9000-CE", "NIFTY", "2020-02-06", "9000", "CE"),
            _fut("NSE-NIFTY-06Feb20-FUT", "NIFTY", "2020-02-06"),
        ]
    )
    contracts = idx.contracts("NIFTY", "2020-02-06")
    labels = [c.label for c in contracts]
    # Future first, then ascending NUMERIC strike (9000 before 12300, not lexical),
    # then CE before PE at the tied strike.
    assert labels == ["FUT", "9000 CE", "12300 CE", "12300 PE"]


def test_contract_aggregates_multiple_intervals_into_one_leaf() -> None:
    idx = build_fno_cascade(
        [
            _opt(
                "NSE-NIFTY-06Feb20-12250-PE",
                "NIFTY",
                "2020-02-06",
                "12250",
                "PE",
                interval="1minute",
            ),
            _opt(
                "NSE-NIFTY-06Feb20-12250-PE", "NIFTY", "2020-02-06", "12250", "PE", interval="1day"
            ),
        ]
    )
    contracts = idx.contracts("NIFTY", "2020-02-06")
    assert len(contracts) == 1  # ONE leaf, not two flat rows
    assert contracts[0].intervals == ("1day", "1minute")  # ascending, deduped
    assert contracts[0].symbol == "NSE-NIFTY-06Feb20-12250-PE"


def test_unparsed_partition_is_surfaced_not_hidden() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-06Feb20-12250-PE", "NIFTY", "2020-02-06", "12250", "PE"),
            # No underlying/expiry decoded (junk partition) → unparsed bucket.
            _ds(
                "NSE-GARBAGE-XYZ",
                underlying=None,
                expiry=None,
                strike=None,
                option_type=None,
                instrument_type=None,
            ),
        ]
    )
    assert idx.underlyings() == ("NIFTY",)  # the junk is NOT in the cascade
    assert [c.symbol for c in idx.unparsed] == ["NSE-GARBAGE-XYZ"]  # but IS surfaced


def test_partial_decode_missing_expiry_goes_unparsed() -> None:
    # A row that decoded an underlying but NOT an expiry cannot be placed → unparsed.
    idx = build_fno_cascade(
        [
            _ds(
                "NSE-NIFTY-BADEXPIRY-CE",
                underlying="NIFTY",
                expiry=None,
                strike="12250",
                option_type="CE",
                instrument_type="CE",
            ),
        ]
    )
    assert idx.underlyings() == ()
    assert [c.symbol for c in idx.unparsed] == ["NSE-NIFTY-BADEXPIRY-CE"]


def test_future_label_and_fields() -> None:
    idx = build_fno_cascade([_fut("NSE-NIFTY-06Feb20-FUT", "NIFTY", "2020-02-06")])
    fut = idx.contracts("NIFTY", "2020-02-06")[0]
    assert fut.label == "FUT"
    assert fut.strike is None and fut.option_type is None and fut.instrument_type == "FUT"


def test_contract_label_for_undecoded_instrument_type_falls_back_to_symbol() -> None:
    # A leaf with neither strike/type nor instrument_type labels by its symbol.
    c = FnoContract(
        symbol="NSE-NIFTY-06Feb20-FUT",
        exchange="NSE",
        strike=None,
        option_type=None,
        instrument_type=None,
        intervals=("1minute",),
    )
    assert c.label == "NSE-NIFTY-06Feb20-FUT"


def test_fractional_strike_sorts_numerically() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-X-06Feb20-100.5-CE", "X", "2020-02-06", "100.5", "CE"),
            _opt("NSE-X-06Feb20-99-CE", "X", "2020-02-06", "99", "CE"),
            _opt("NSE-X-06Feb20-100-CE", "X", "2020-02-06", "100", "CE"),
        ]
    )
    labels = [c.label for c in idx.contracts("X", "2020-02-06")]
    assert labels == ["99 CE", "100 CE", "100.5 CE"]


# ─────────────────────────────────────────────────────────────────────────
# build_expiry_counts — per-expiry "listed N with data M" rows
# (DEC-DB-UX-PACK-001 Feature 6; pure, aligned with FnoCascadeIndex.expiries)
# ─────────────────────────────────────────────────────────────────────────
def test_build_expiry_counts_listed_none_when_registry_absent() -> None:
    # No registry calendar (None) → an honest "with data M" chip, never "listed 0".
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-02Jan25-24000-CE", "NIFTY", "2025-01-02", "24000", "CE"),
            _opt("NSE-NIFTY-02Jan25-24000-PE", "NIFTY", "2025-01-02", "24000", "PE"),
        ]
    )
    rows = build_expiry_counts(idx, "NIFTY", None)
    assert rows == [{"expiry": "2025-01-02", "listed": None, "with_data": 2}]


def test_build_expiry_counts_young_chain_listed_exceeds_with_data() -> None:
    # Only ONE strike has candle data, but the vendor listed THREE → "listed 3 with data 1".
    idx = build_fno_cascade(
        [_opt("NSE-NIFTY-02Jan25-24000-CE", "NIFTY", "2025-01-02", "24000", "CE")]
    )
    rows = build_expiry_counts(idx, "NIFTY", {"2025-01-02": 3})
    assert rows == [{"expiry": "2025-01-02", "listed": 3, "with_data": 1}]


def test_build_expiry_counts_listed_equals_with_data() -> None:
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-02Jan25-24000-CE", "NIFTY", "2025-01-02", "24000", "CE"),
            _opt("NSE-NIFTY-02Jan25-24000-PE", "NIFTY", "2025-01-02", "24000", "PE"),
        ]
    )
    rows = build_expiry_counts(idx, "NIFTY", {"2025-01-02": 2})
    assert rows == [{"expiry": "2025-01-02", "listed": 2, "with_data": 2}]


def test_build_expiry_counts_unknown_underlying_is_empty() -> None:
    idx = build_fno_cascade(
        [_opt("NSE-NIFTY-02Jan25-24000-CE", "NIFTY", "2025-01-02", "24000", "CE")]
    )
    assert build_expiry_counts(idx, "NOPE", {"2025-01-02": 3}) == []


def test_build_expiry_counts_rows_align_with_expiries_order() -> None:
    # Rows follow FnoCascadeIndex.expiries ascending-ISO order; an expiry the
    # registry does not know reads listed=None while its sibling reads the count.
    idx = build_fno_cascade(
        [
            _opt("NSE-NIFTY-30Jan25-24000-CE", "NIFTY", "2025-01-30", "24000", "CE"),
            _opt("NSE-NIFTY-02Jan25-24000-CE", "NIFTY", "2025-01-02", "24000", "CE"),
        ]
    )
    rows = build_expiry_counts(idx, "NIFTY", {"2025-01-30": 5})
    assert rows == [
        {"expiry": "2025-01-02", "listed": None, "with_data": 1},
        {"expiry": "2025-01-30", "listed": 5, "with_data": 1},
    ]


# ─────────────────────────────────────────────────────────────────────────
# contract_to_json — JSON-native projection
# ─────────────────────────────────────────────────────────────────────────
def test_contract_to_json_option_shape() -> None:
    c = FnoContract(
        symbol="NSE-NIFTY-06Feb20-12250-PE",
        exchange="NSE",
        strike="12250",
        option_type="PE",
        instrument_type="PE",
        intervals=("1day", "1minute"),
    )
    assert contract_to_json(c) == {
        "symbol": "NSE-NIFTY-06Feb20-12250-PE",
        "label": "12250 PE",
        "exchange": "NSE",
        "strike": "12250",
        "option_type": "PE",
        "instrument_type": "PE",
        "intervals": ["1day", "1minute"],
    }


def test_contract_to_json_future_nulls() -> None:
    c = FnoContract(
        symbol="NSE-NIFTY-06Feb20-FUT",
        exchange="NSE",
        strike=None,
        option_type=None,
        instrument_type="FUT",
        intervals=("1minute",),
    )
    row = contract_to_json(c)
    assert row["strike"] is None and row["option_type"] is None
    assert row["label"] == "FUT" and row["intervals"] == ["1minute"]
