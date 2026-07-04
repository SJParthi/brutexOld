"""Tests for `brutex.dashboard.pull_census` (DEC-WRITTEN-SETTLED-CENSUS-001).

100% line + branch on the observe-only written-vs-settled census: both
mismatch classes, the scope filters (symbols + intervals — the derived-TF
false-positive guard), the 20-sample cap, the clean-census silence, and the
guarded emit that can never break a pull.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from brutex.dashboard.pull_census import (
    CENSUS_SAMPLE_CAP,
    WrittenSettledCensus,
    build_written_vs_settled_census,
    emit_written_settled_census,
)
from structlog.testing import capture_logs

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _row(symbol: str, interval: str = "1minute", status: str = "SUCCESS") -> tuple[Any, ...]:
    """One serialised 13-tuple ledger row (only idx 1/2/5 are census-read)."""
    return (
        "groww",
        symbol,
        interval,
        "2026-06-01",
        "2026-07-01",
        status,
        5,
        1,
        None,
        _NOW.isoformat(),
        _NOW.isoformat(),
        0,
        1,
    )


def _span(symbol: str, interval: str = "1minute", bars: int = 100) -> Mapping[str, Any]:
    return {
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": symbol,
        "interval": interval,
        "bars": bars,
        "first_date": "2026-06-01",
        "last_date": "2026-06-30",
    }


class TestBuildCensus:
    def test_agreeing_series_is_clean(self) -> None:
        census = build_written_vs_settled_census(
            [_row("NIFTY")],
            [_span("NIFTY")],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census == WrittenSettledCensus(
            settled_unwritten=0, written_unsettled=0, samples=(), scanned=1
        )
        assert census.mismatched is False

    def test_settled_but_unwritten_is_flagged(self) -> None:
        # The loud class: the ledger says SUCCESS, the lake holds NOTHING.
        census = build_written_vs_settled_census(
            [_row("NIFTY")],
            [],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census.settled_unwritten == 1
        assert census.written_unsettled == 0
        assert census.samples == ("settled-unwritten: 'NIFTY' '1minute'",)
        assert census.mismatched is True

    def test_zero_bar_span_counts_as_unwritten(self) -> None:
        census = build_written_vs_settled_census(
            [_row("NIFTY")],
            [_span("NIFTY", bars=0)],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census.settled_unwritten == 1

    def test_written_but_unsettled_is_flagged(self) -> None:
        census = build_written_vs_settled_census(
            [_row("NIFTY", status="FAILED")],  # no SUCCESS row for the series
            [_span("NIFTY")],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census.written_unsettled == 1
        assert census.settled_unwritten == 0
        assert census.samples == ("written-unsettled: 'NIFTY' '1minute'",)

    def test_interval_scope_guards_derived_partitions(self) -> None:
        # The derived 1day/5m partitions carry NO ledger rows by design
        # (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — the interval scope must keep
        # them OUT of the census or every derived partition would false-flag.
        census = build_written_vs_settled_census(
            [_row("NIFTY")],
            [_span("NIFTY"), _span("NIFTY", interval="1day"), _span("NIFTY", interval="5minute")],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census.mismatched is False

    def test_symbol_scope_ignores_out_of_scope_series(self) -> None:
        # A prior pull's other universe must not flood this pull's census.
        census = build_written_vs_settled_census(
            [_row("NIFTY"), _row("BANKNIFTY")],
            [_span("NIFTY"), _span("SENSEX")],
            intervals=("1minute",),
            symbols=("NIFTY",),
        )
        assert census.mismatched is False
        assert census.scanned == 1  # only the in-scope row was examined

    def test_samples_capped_at_twenty(self) -> None:
        symbols = [f"SYM{i:03d}" for i in range(30)]
        census = build_written_vs_settled_census(
            [_row(s) for s in symbols],
            [],
            intervals=("1minute",),
            symbols=symbols,
        )
        assert census.settled_unwritten == 30  # the TRUE count is never capped
        assert len(census.samples) == CENSUS_SAMPLE_CAP

    def test_sample_cap_spans_both_classes(self) -> None:
        unwritten = [f"AAA{i:02d}" for i in range(15)]
        unsettled = [f"ZZZ{i:02d}" for i in range(15)]
        census = build_written_vs_settled_census(
            [_row(s) for s in unwritten],
            [_span(s) for s in unsettled],
            intervals=("1minute",),
            symbols=unwritten + unsettled,
        )
        assert census.settled_unwritten == 15
        assert census.written_unsettled == 15
        assert len(census.samples) == CENSUS_SAMPLE_CAP
        assert sum(s.startswith("settled-unwritten") for s in census.samples) == 15
        assert sum(s.startswith("written-unsettled") for s in census.samples) == 5

    def test_empty_inputs_are_a_clean_census(self) -> None:
        census = build_written_vs_settled_census([], [], intervals=("1minute",), symbols=())
        assert census == WrittenSettledCensus(
            settled_unwritten=0, written_unsettled=0, samples=(), scanned=0
        )


class TestEmitGuarded:
    def test_mismatch_emits_exactly_one_warning(self) -> None:
        with capture_logs() as logs:
            emit_written_settled_census(
                [_row("NIFTY")],
                [],
                intervals=("1minute",),
                symbols=("NIFTY",),
                source="spot",
            )
        warnings = [log for log in logs if log["event"] == "pull.written_settled_mismatch"]
        assert len(warnings) == 1
        assert warnings[0]["source"] == "spot"
        assert warnings[0]["settled_unwritten"] == 1
        assert warnings[0]["samples"] == ["settled-unwritten: 'NIFTY' '1minute'"]

    def test_clean_census_is_debug_only(self) -> None:
        with capture_logs() as logs:
            emit_written_settled_census(
                [_row("NIFTY")],
                [_span("NIFTY")],
                intervals=("1minute",),
                symbols=("NIFTY",),
                source="fno",
            )
        assert [log["event"] for log in logs] == ["pull.written_settled_census"]

    def test_internal_error_is_swallowed_loudly(self) -> None:
        # A malformed span (missing keys) must NEVER break the pull — the
        # guard logs and returns (the #1014 observe-only wiring contract).
        with capture_logs() as logs:
            emit_written_settled_census(
                [_row("NIFTY")],
                [{"symbol": "NIFTY"}],  # missing interval/bars → KeyError inside
                intervals=("1minute",),
                symbols=("NIFTY",),
                source="fno",
            )
        assert [log["event"] for log in logs] == ["pull.written_settled_census_error"]
