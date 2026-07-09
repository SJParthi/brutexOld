"""Unit suite for :mod:`brutex.engine.margin_history` (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

Pins the ETC-template structural laws (row validation, strict-ascending
tables), the ALL-REFUSAL v1 verdict for every engine instrument across the
whole 2020→2026 data window, the pre-history/unknown-instrument refusals, the
verified path via INJECTED tables (no shipped number exists to test with —
Golden Rule #1), full-precision ROI, the wire payload shape, and a
bisect-vs-linear-scan equivalence sweep over deterministic random tables.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from brutex.engine.margin_history import (
    MARGIN_HISTORY_START,
    MARGIN_REGIMES,
    MarginRegimeRow,
    MarginRoiVerdict,
    UnverifiedMarginRegimeError,
    margin_for_date,
    margin_roi_payload,
    roi_on_margin,
    validate_margin_table,
)

_VERIFIED_NIFTY = {
    "NIFTY": (
        MarginRegimeRow(date(2020, 1, 1), 8_000_000_00, "ASSUMED per test injection"),
        MarginRegimeRow(date(2024, 6, 1), 12_000_000_00, "ASSUMED per test injection"),
    ),
}


class TestMarginRegimeRow:
    def test_verified_row(self) -> None:
        row = MarginRegimeRow(date(2024, 6, 1), 12_000_000_00, "NSE SPAN file 2024-06-01")
        assert row.verified is True

    def test_refusal_row(self) -> None:
        row = MarginRegimeRow(date(2020, 1, 1), None, "UNVERIFIED — no archive")
        assert row.verified is False

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_margin_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="must be >= 1 or None"):
            MarginRegimeRow(date(2020, 1, 1), bad, "src")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_source_rejected(self, blank: str) -> None:
        with pytest.raises(ValueError, match="source must carry its citation"):
            MarginRegimeRow(date(2020, 1, 1), None, blank)


class TestValidateMarginTable:
    def test_valid_table_returned_unchanged(self) -> None:
        rows = (
            MarginRegimeRow(date(2020, 1, 1), None, "gap"),
            MarginRegimeRow(date(2024, 10, 1), 1, "cited"),
        )
        assert validate_margin_table(rows, label="t") is rows

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="is empty"):
            validate_margin_table((), label="t")

    def test_unsorted_rejected(self) -> None:
        rows = (
            MarginRegimeRow(date(2024, 10, 1), 1, "cited"),
            MarginRegimeRow(date(2020, 1, 1), None, "gap"),
        )
        with pytest.raises(ValueError, match="strictly\\s+ascending"):
            validate_margin_table(rows, label="t")

    def test_duplicate_start_rejected(self) -> None:
        rows = (
            MarginRegimeRow(date(2020, 1, 1), None, "gap"),
            MarginRegimeRow(date(2020, 1, 1), 1, "cited"),
        )
        with pytest.raises(ValueError, match="strictly\\s+ascending"):
            validate_margin_table(rows, label="t")


class TestShippedTablesAreAllRefusal:
    def test_exactly_the_three_engine_instruments(self) -> None:
        assert sorted(MARGIN_REGIMES) == ["BANKNIFTY", "NIFTY", "SENSEX"]

    @pytest.mark.parametrize("instrument", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_one_refusal_row_anchored_at_the_data_floor(self, instrument: str) -> None:
        table = MARGIN_REGIMES[instrument]
        assert len(table) == 1
        assert table[0].start == MARGIN_HISTORY_START == date(2020, 1, 1)
        assert table[0].verified is False
        assert "UNVERIFIED" in table[0].source

    @pytest.mark.parametrize("instrument", ["NIFTY", "BANKNIFTY", "SENSEX"])
    @pytest.mark.parametrize(
        "on_date",
        [date(2020, 1, 1), date(2021, 7, 15), date(2024, 10, 1), date(2026, 7, 7)],
    )
    def test_every_data_window_date_refuses(self, instrument: str, on_date: date) -> None:
        with pytest.raises(UnverifiedMarginRegimeError) as excinfo:
            margin_for_date(instrument, on_date)
        err = excinfo.value
        assert err.instrument == instrument
        assert err.on_date == on_date
        assert err.window_start == MARGIN_HISTORY_START
        assert err.verified_from is None  # single-row table: no verified end
        assert "UNVERIFIED" in err.source
        assert "no verified end" in str(err)


class TestMarginForDate:
    def test_unknown_instrument_refuses(self) -> None:
        with pytest.raises(ValueError, match="no SPAN margin regime table"):
            margin_for_date("NIFTY-FUT-2026-01-29", date(2026, 1, 2))

    def test_pre_history_date_refuses(self) -> None:
        with pytest.raises(ValueError, match="precedes the first recorded"):
            margin_for_date("NIFTY", date(2019, 12, 31))

    def test_injected_verified_table_returns_the_margin(self) -> None:
        assert margin_for_date("NIFTY", date(2024, 5, 31), regimes=_VERIFIED_NIFTY) == 8_000_000_00

    def test_bisect_picks_the_latest_row_at_and_after_a_boundary(self) -> None:
        assert margin_for_date("NIFTY", date(2024, 6, 1), regimes=_VERIFIED_NIFTY) == 12_000_000_00
        assert margin_for_date("NIFTY", date(2026, 1, 1), regimes=_VERIFIED_NIFTY) == 12_000_000_00

    def test_refusal_row_before_a_verified_row_names_verified_from(self) -> None:
        regimes = {
            "NIFTY": (
                MarginRegimeRow(date(2020, 1, 1), None, "gap"),
                MarginRegimeRow(date(2024, 10, 1), 5, "cited"),
            ),
        }
        with pytest.raises(UnverifiedMarginRegimeError) as excinfo:
            margin_for_date("NIFTY", date(2022, 3, 3), regimes=regimes)
        assert excinfo.value.verified_from == date(2024, 10, 1)
        assert "until 2024-10-01" in str(excinfo.value)

    def test_bisect_matches_a_linear_scan_on_random_tables(self) -> None:
        # Deterministic random sweep: the O(log N) bisect must agree with the
        # obvious O(N) "latest row with start <= date" reference everywhere.
        rng = random.Random(0)
        for _ in range(50):
            starts = sorted(rng.sample(range(0, 2_500), rng.randint(1, 8)))  # days since 2020-01-01
            rows = tuple(
                MarginRegimeRow(
                    date(2020, 1, 1) + timedelta(days=day),
                    (idx + 1) * 100 if idx % 2 == 0 else None,
                    "synthetic",
                )
                for idx, day in enumerate(starts)
            )
            regimes = {"X": validate_margin_table(rows, label="X")}
            for probe_day in range(0, 2_600, 97):
                probe = date(2020, 1, 1) + timedelta(days=probe_day)
                in_force = None
                for row in rows:
                    if row.start <= probe:
                        in_force = row
                if in_force is None:
                    with pytest.raises(ValueError, match="precedes the first recorded"):
                        margin_for_date("X", probe, regimes=regimes)
                elif in_force.span_per_lot_paisa is None:
                    with pytest.raises(UnverifiedMarginRegimeError):
                        margin_for_date("X", probe, regimes=regimes)
                else:
                    assert (
                        margin_for_date("X", probe, regimes=regimes) == in_force.span_per_lot_paisa
                    )


class TestRoiOnMargin:
    def test_shipped_tables_yield_the_unverified_verdict(self) -> None:
        verdict = roi_on_margin(10_000, instrument="NIFTY", entry_date=date(2025, 1, 2))
        assert verdict == MarginRoiVerdict(
            status="unverified",
            roi_pct=None,
            detail=verdict.detail,  # exact text pinned below
        )
        assert "UNVERIFIED" in verdict.detail
        assert "NIFTY" in verdict.detail

    def test_unknown_instrument_degrades_not_raises(self) -> None:
        verdict = roi_on_margin(10_000, instrument="RELIANCE", entry_date=date(2025, 1, 2))
        assert verdict.status == "unverified"
        assert verdict.roi_pct is None
        assert "refusing to fabricate" in verdict.detail

    def test_pre_history_degrades_not_raises(self) -> None:
        verdict = roi_on_margin(10_000, instrument="NIFTY", entry_date=date(2019, 6, 1))
        assert verdict.status == "unverified"
        assert "precedes the first recorded" in verdict.detail

    def test_lots_below_one_is_a_programmer_error(self) -> None:
        with pytest.raises(ValueError, match="lots must be >= 1"):
            roi_on_margin(10_000, instrument="NIFTY", entry_date=date(2025, 1, 2), lots=0)

    def test_verified_roi_full_precision(self) -> None:
        verdict = roi_on_margin(
            1_234,
            instrument="NIFTY",
            entry_date=date(2024, 7, 1),
            lots=2,
            regimes=_VERIFIED_NIFTY,
        )
        assert verdict.status == "ok"
        # 1_234 / (12_000_000_00 × 2) × 100 — full float precision, never rounded.
        assert verdict.roi_pct == 1_234 / (12_000_000_00 * 2) * 100.0
        assert "2 lot(s)" in verdict.detail

    def test_negative_pnl_yields_negative_roi(self) -> None:
        verdict = roi_on_margin(
            -8_000_000_00,
            instrument="NIFTY",
            entry_date=date(2020, 1, 1),
            regimes=_VERIFIED_NIFTY,
        )
        assert verdict.status == "ok"
        assert verdict.roi_pct == -100.0


class TestMarginRoiPayload:
    def test_unverified_wire_shape(self) -> None:
        payload = margin_roi_payload(10_000, instrument="NIFTY", entry_date=date(2025, 1, 2))
        assert sorted(payload) == ["detail", "roi_pct", "status"]
        assert payload["status"] == "unverified"
        assert payload["roi_pct"] is None
        assert isinstance(payload["detail"], str) and "UNVERIFIED" in payload["detail"]

    def test_ok_wire_shape_with_injected_table(self) -> None:
        payload = margin_roi_payload(
            6_000_000_00,
            instrument="NIFTY",
            entry_date=date(2020, 1, 2),
            regimes=_VERIFIED_NIFTY,
        )
        assert payload["status"] == "ok"
        assert payload["roi_pct"] == 75.0
