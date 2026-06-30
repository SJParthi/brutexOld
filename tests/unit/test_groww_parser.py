"""Unit tests for ``brutex.adapters.groww.parser`` (DEC-GROWW-DEPS-REINSTATE-001)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww import parser
from brutex.data.candle_fields import CandleParseError


# ── parse_timestamp_ist ─────────────────────────────────────────────────────
def test_iso_t_separator_localizes_ist_to_utc() -> None:
    # 10:30 IST == 05:00 UTC (fixed +05:30).
    assert parser.parse_timestamp_ist("2025-09-24T10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_iso_space_separator() -> None:
    assert parser.parse_timestamp_ist("2025-09-24 10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_epoch_int_is_utc() -> None:
    assert parser.parse_timestamp_ist(0) == datetime(1970, 1, 1, 0, 0, tzinfo=UTC)


def test_epoch_float_is_utc() -> None:
    ts = parser.parse_timestamp_ist(1_700_000_000.0)
    assert ts.tzinfo == UTC


def test_bool_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must not be bool"):
        parser.parse_timestamp_ist(True)


def test_unparseable_string_rejected() -> None:
    with pytest.raises(CandleParseError, match="unparseable Groww timestamp"):
        parser.parse_timestamp_ist("not-a-date")


def test_non_finite_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="non-finite"):
        parser.parse_timestamp_ist(float("inf"))


def test_out_of_range_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="out-of-range"):
        parser.parse_timestamp_ist(1e30)


def test_wrong_type_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must be a string or epoch"):
        parser.parse_timestamp_ist(["2025-09-24"])


# ── parse_candle_rows ───────────────────────────────────────────────────────
def _row(ts: str = "2025-09-24T10:30:00") -> list[object]:
    return [ts, 100.0, 110.0, 90.0, 105.0, 1000, None]


def test_parses_normal_rows() -> None:
    bars = parser.parse_candle_rows({"candles": [_row(), _row("2025-09-24T10:31:00")]})
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.0")
    assert bars[0].high == Decimal("110.0")
    assert bars[0].volume == 1000


def test_no_trade_row_is_skipped() -> None:
    bars = parser.parse_candle_rows(
        {"candles": [_row(), ["2025-09-24T10:31:00", 0, 0, 0, 0, 0, None]]}
    )
    assert len(bars) == 1  # the empty bar is gracefully dropped


def test_drop_volume_zeros_volume() -> None:
    bars = parser.parse_candle_rows({"candles": [_row()]}, drop_volume=True)
    assert bars[0].volume == 0


def test_fno_open_interest_preserved() -> None:
    bars = parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", 1, 2, 1, 2, 5, 42]]})
    assert bars[0].open_interest == 42


def test_current_session_six_element_row_parsed_not_quarantined() -> None:
    """DEC-GROWW-FORMING-BAR-PARSE-001: the current not-yet-finalised day's intraday
    bars arrive as a 6-element row (no trailing open_interest slot) — parse them with
    ``open_interest=None`` instead of quarantining the whole series."""
    # The EXACT row from the operator's brutex.log that quarantined all 4 indices'
    # 1-minute series — 6 elements, trailing volume is literally ``None`` (no OI slot).
    bars = parser.parse_candle_rows(
        {"candles": [["2026-06-19T09:15:00", 26466.25, 26480.9, 26422.2, 26457.85, None]]}
    )
    assert len(bars) == 1
    assert bars[0].open == Decimal("26466.25")
    assert bars[0].close == Decimal("26457.85")
    assert bars[0].open_interest is None  # padded — OI slot absent on the live edge


def test_mixed_seven_then_six_element_rows() -> None:
    """A settled 7-element bar followed by the live-edge 6-element bar both parse."""
    bars = parser.parse_candle_rows(
        {
            "candles": [
                ["2026-06-18T15:29:00", 100.0, 110.0, 90.0, 105.0, 1000, None],  # settled
                ["2026-06-19T09:15:00", 105.0, 106.0, 104.0, 105.5, 0],  # current session (6)
            ]
        }
    )
    assert len(bars) == 2
    assert bars[1].open_interest is None


def test_five_element_row_still_raises() -> None:
    """A row shorter than 6 (a genuinely malformed row) is STILL rejected — the
    tolerance is exactly the 6-element current-session shape, nothing shorter.

    The single error message names BOTH accepted shapes, so we anchor on the
    rejected ROW repr (length-discriminating) rather than the shape names — the
    test would otherwise pass even if the 5/8 cases were swapped (hostile-review NIT).
    """
    with pytest.raises(CandleParseError) as exc:
        parser.parse_candle_rows({"candles": [["2026-06-19T09:15:00", 1, 2, 3, 4]]})
    assert "1, 2, 3, 4]" in str(exc.value)  # the rejected 5-element row, discriminating
    assert "1, 2, 3, 4, 5" not in str(exc.value)


def test_eight_element_row_still_raises() -> None:
    """A row longer than 7 is STILL rejected (an unexpected vendor shape, fail-loud).

    Anchored on the rejected ROW repr (length-discriminating), not the shape names.
    """
    with pytest.raises(CandleParseError) as exc:
        parser.parse_candle_rows({"candles": [["2026-06-19T09:15:00", 1, 2, 3, 4, 5, 6, 7]]})
    assert "1, 2, 3, 4, 5, 6, 7]" in str(exc.value)  # the rejected 8-element row, discriminating


def test_missing_candles_key_raises() -> None:
    with pytest.raises(CandleParseError, match="missing required 'candles'"):
        parser.parse_candle_rows({})


@pytest.mark.parametrize("payload", [None, [], "candles", 42, ("a", "b")])
def test_non_mapping_payload_raises_clean_parse_error(payload: object) -> None:
    # DEC-CANDLE-PAYLOAD-MAPPING-GUARD-001 (defensive insurance, NOT a current bug — the real
    # SDK never returns a non-mapping): a non-`Mapping` payload degrades to a clean
    # CandleParseError (→ per-symbol quarantine) instead of a raw TypeError on `"candles" not in`.
    with pytest.raises(CandleParseError, match="must be a mapping"):
        parser.parse_candle_rows(payload)  # type: ignore[arg-type]


def test_non_array_candles_raises() -> None:
    with pytest.raises(CandleParseError, match="must be an array"):
        parser.parse_candle_rows({"candles": "nope"})


def test_wrong_arity_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [[1, 2, 3]]})


def test_non_sequence_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [42]})


def test_malformed_nonempty_row_raises() -> None:
    # Non-empty (has prices) but an unparseable price → CandleParseError (not skipped).
    with pytest.raises(CandleParseError):
        parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", "x", 2, 1, 2, 5, None]]})


def test_partial_absent_open_row_is_skipped_not_fatal() -> None:
    # DEC-CANDLE-PARTIAL-ABSENT-SKIP-001: Groww's observed `open: None` with a
    # real high/low/close is an un-storable PARTIAL bar. It must be GRACEFULLY skipped (the
    # crash `unparseable open price: None` previously aborted the whole 779-symbol pull) —
    # the surrounding good rows survive, never lost to one bad neighbour.
    bars = parser.parse_candle_rows(
        {
            "candles": [
                _row(),
                ["2025-09-24T10:31:00", None, 110.0, 90.0, 105.0, 1000, None],
                _row("2025-09-24T10:32:00"),
            ]
        }
    )
    assert len(bars) == 2  # the partial bar is dropped; both good rows are kept
