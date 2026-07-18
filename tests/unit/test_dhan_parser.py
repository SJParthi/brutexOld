"""Unit tests for :mod:`brutex.adapters.dhan.parser` — 100% line+branch."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan import parser as p
from brutex.data.candle_fields import CandleParseError

# ── parse_timestamp_epoch ──────────────────────────────────────────────────


def test_parse_timestamp_epoch_valid() -> None:
    got = p.parse_timestamp_epoch(1_700_000_000)
    assert got.tzinfo is UTC
    assert got == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_parse_timestamp_epoch_float_ok() -> None:
    assert p.parse_timestamp_epoch(1_700_000_000.0).tzinfo is UTC


def test_parse_timestamp_epoch_bool_rejected() -> None:
    with pytest.raises(CandleParseError, match="must not be bool"):
        p.parse_timestamp_epoch(True)


def test_parse_timestamp_epoch_non_numeric_rejected() -> None:
    with pytest.raises(CandleParseError, match="must be epoch seconds"):
        p.parse_timestamp_epoch("2024-01-01")


def test_parse_timestamp_epoch_non_finite_rejected() -> None:
    with pytest.raises(CandleParseError, match="non-finite"):
        p.parse_timestamp_epoch(float("inf"))


def test_parse_timestamp_epoch_out_of_range_rejected() -> None:
    with pytest.raises(CandleParseError, match="out-of-range"):
        p.parse_timestamp_epoch(10**20)


# ── parse_candle_columns ───────────────────────────────────────────────────


def test_parse_candle_columns_happy_with_oi() -> None:
    payload = {
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [10, 20],
        "timestamp": [1_700_000_000, 1_700_000_060],
        "open_interest": [500, 600],
    }
    bars = p.parse_candle_columns(payload)
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.0")
    assert bars[0].open_interest == 500


def test_parse_candle_columns_without_oi() -> None:
    payload = {
        "open": [100.0],
        "high": [102.0],
        "low": [99.0],
        "close": [101.0],
        "volume": [10],
        "timestamp": [1_700_000_000],
    }
    bars = p.parse_candle_columns(payload)
    assert bars[0].open_interest is None


def test_parse_candle_columns_drop_volume() -> None:
    payload = {
        "open": [100.0],
        "high": [102.0],
        "low": [99.0],
        "close": [101.0],
        "volume": [999],
        "timestamp": [1_700_000_000],
    }
    bars = p.parse_candle_columns(payload, drop_volume=True)
    assert bars[0].volume == 0


def test_parse_candle_columns_skips_no_trade_row() -> None:
    payload = {
        "open": [None, 100.0],
        "high": [None, 102.0],
        "low": [None, 99.0],
        "close": [None, 101.0],
        "volume": [0, 10],
        "timestamp": [1_700_000_000, 1_700_000_060],
    }
    bars = p.parse_candle_columns(payload)
    assert len(bars) == 1  # the all-empty row is skipped


def test_parse_candle_columns_missing_column() -> None:
    with pytest.raises(CandleParseError, match="missing required column"):
        p.parse_candle_columns({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})


def test_parse_candle_columns_non_array_column() -> None:
    payload = {
        "open": 100.0,  # not an array
        "high": [1],
        "low": [1],
        "close": [1],
        "volume": [1],
        "timestamp": [1],
    }
    with pytest.raises(CandleParseError, match="must be an array"):
        p.parse_candle_columns(payload)


def test_parse_candle_columns_ragged() -> None:
    payload = {
        "open": [1, 2],
        "high": [1],
        "low": [1],
        "close": [1],
        "volume": [1],
        "timestamp": [1],
    }
    with pytest.raises(CandleParseError, match="ragged"):
        p.parse_candle_columns(payload)


def test_parse_candle_columns_ragged_oi() -> None:
    payload = {
        "open": [1.0],
        "high": [2.0],
        "low": [0.5],
        "close": [1.5],
        "volume": [1],
        "timestamp": [1_700_000_000],
        "open_interest": [1, 2],
    }
    with pytest.raises(CandleParseError, match="'open_interest' has 2"):
        p.parse_candle_columns(payload)


# ── parse_rolling_option ───────────────────────────────────────────────────


def _rolling_side(n: int = 1) -> dict[str, object]:
    return {
        "open": [10.0] * n,
        "high": [11.0] * n,
        "low": [9.0] * n,
        "close": [10.5] * n,
        "volume": [100] * n,
        "timestamp": [1_700_000_000] * n,
        "oi": [2000] * n,
        "strike": [20000.0] * n,
        "spot": [20050.0] * n,
        "iv": [0.15] * n,
    }


def test_parse_rolling_option_both_sides() -> None:
    bars = p.parse_rolling_option({"ce": _rolling_side(), "pe": _rolling_side()})
    assert [b.side for b in bars] == ["CE", "PE"]
    assert bars[0].strike == Decimal("20000.0")
    assert bars[0].spot == Decimal("20050.0")
    assert bars[0].iv == Decimal("0.15")
    assert bars[0].open_interest == 2000


def test_parse_rolling_option_negative_iv_nulled_never_valueerror() -> None:
    """A present NEGATIVE iv cell nulls to None (round-1 correctness MINOR).

    Pre-fix a negative iv reached ``OptionBar.__post_init__`` and raised a bare
    ``ValueError`` — NOT a BruteXError, so it escaped the per-symbol quarantine
    and crashed a multi-symbol pull. The invalid-value law mirrors the
    strike/spot siblings: honest None (unknown), never a fabricated value,
    never an un-quarantinable crash.
    """
    side = _rolling_side()
    side["iv"] = [-0.5]
    bars = p.parse_rolling_option({"ce": side})
    assert len(bars) == 1
    assert bars[0].iv is None


def test_parse_rolling_option_zero_iv_kept_exact() -> None:
    """iv == 0 is a legitimate value — kept as exact Decimal, never nulled."""
    side = _rolling_side()
    side["iv"] = [0]
    bars = p.parse_rolling_option({"ce": side})
    assert bars[0].iv == Decimal("0")


def test_parse_rolling_option_one_side_absent() -> None:
    bars = p.parse_rolling_option({"ce": _rolling_side()})  # pe absent
    assert [b.side for b in bars] == ["CE"]


def test_parse_rolling_option_neither_side() -> None:
    with pytest.raises(CandleParseError, match="neither 'ce' nor 'pe'"):
        p.parse_rolling_option({"data": {}})


def test_parse_rolling_option_side_not_mapping() -> None:
    with pytest.raises(CandleParseError, match="must be a mapping"):
        p.parse_rolling_option({"ce": [1, 2, 3]})


def test_parse_rolling_option_missing_optional_columns() -> None:
    side = {
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [10.5],
        "volume": [100],
        "timestamp": [1_700_000_000],
    }  # no oi/strike/spot/iv
    bars = p.parse_rolling_option({"pe": side})
    assert bars[0].strike is None
    assert bars[0].spot is None
    assert bars[0].iv is None
    assert bars[0].open_interest is None


def test_parse_rolling_option_ragged_side() -> None:
    side = _rolling_side()
    side["strike"] = [1.0, 2.0]  # ragged optional column
    with pytest.raises(CandleParseError, match="ragged Dhan rolling-option"):
        p.parse_rolling_option({"ce": side})


def test_parse_rolling_option_skips_no_trade_row() -> None:
    side = _rolling_side(2)
    side["open"] = [None, 10.0]
    side["high"] = [None, 11.0]
    side["low"] = [None, 9.0]
    side["close"] = [None, 10.5]
    bars = p.parse_rolling_option({"ce": side})
    assert len(bars) == 1


# ── option chain ───────────────────────────────────────────────────────────


def test_parse_option_chain_happy() -> None:
    payload = {
        "data": {
            "last_price": 20050.0,
            "oc": {
                "20100.0": {"ce": {"oi": 1}, "pe": {"oi": 2}},
                "20000.0": {"ce": {"oi": 3}, "pe": {"oi": 4}},
            },
        }
    }
    snap = p.parse_option_chain(payload)
    assert snap.last_price == Decimal("20050.0")
    assert [s.strike for s in snap.strikes] == [Decimal("20000.0"), Decimal("20100.0")]  # sorted
    assert snap.strikes[0].ce == {"oi": 3}


def test_parse_option_chain_top_level_envelope() -> None:
    payload = {"last_price": 100.0, "oc": {"100.0": {"ce": {}, "pe": {}}}}
    snap = p.parse_option_chain(payload)
    assert snap.last_price == Decimal("100.0")
    assert len(snap.strikes) == 1


def test_parse_option_chain_no_oc() -> None:
    snap = p.parse_option_chain({"data": {"last_price": 1.0}})
    assert snap.strikes == ()


def test_parse_option_chain_oc_not_mapping() -> None:
    with pytest.raises(CandleParseError, match="'oc' must be a mapping"):
        p.parse_option_chain({"data": {"oc": [1, 2]}})


def test_parse_option_chain_skips_malformed_entries() -> None:
    payload = {
        "oc": {
            "notanumber": {"ce": {}, "pe": {}},  # unparseable strike → skip
            "0": {"ce": {}, "pe": {}},  # non-positive strike → skip
            "100.0": "notamapping",  # non-mapping value → skip
            "200.0": {"ce": "x", "pe": {"oi": 5}},  # ce non-mapping → {}
        }
    }
    snap = p.parse_option_chain(payload)
    assert [s.strike for s in snap.strikes] == [Decimal("200.0")]
    assert snap.strikes[0].ce == {}
    assert snap.strikes[0].pe == {"oi": 5}
    assert snap.last_price is None


def test_nameable_option_chain_fields() -> None:
    side = {"last_price": 10.0, "oi": 5, "unknown_greek": 0.1}
    got = p.nameable_option_chain_fields(side)
    assert got == {"last_price": 10.0, "oi": 5}  # only nameable keys, unknown omitted


# ── expiry list ────────────────────────────────────────────────────────────


def test_parse_expiry_list_happy_dedup_sorted() -> None:
    got = p.parse_expiry_list(
        {"status": "success", "data": ["2026-09-24", "2026-08-27", "2026-08-27"]}
    )
    assert got == [date(2026, 8, 27), date(2026, 9, 24)]


def test_parse_expiry_list_skips_blank() -> None:
    got = p.parse_expiry_list({"data": ["2026-08-27", "", None, "  "]})
    assert got == [date(2026, 8, 27)]


def test_parse_expiry_list_missing_data() -> None:
    with pytest.raises(CandleParseError, match="missing required 'data'"):
        p.parse_expiry_list({"status": "success"})


def test_parse_expiry_list_non_array() -> None:
    with pytest.raises(CandleParseError, match="must be an array"):
        p.parse_expiry_list({"data": "2026-08-27"})


def test_parse_expiry_list_non_string_entry() -> None:
    with pytest.raises(CandleParseError, match="must be a date string"):
        p.parse_expiry_list({"data": [12345]})


def test_parse_expiry_list_unparseable_date() -> None:
    with pytest.raises(CandleParseError, match="unparseable Dhan expiry"):
        p.parse_expiry_list({"data": ["27-08-2026"]})
