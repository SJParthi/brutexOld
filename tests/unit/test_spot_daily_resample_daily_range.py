"""Tests for the >9% daily-range detector (DEC-HALT-DAY-TREATMENT-001).

`brutex.data.spot_daily_resample.detect_extreme_daily_ranges` + its emit /
guarded-observe wiring: the >= 0.09 boundary (both sides + the exact
boundary), the known-halt INFO arm vs the unknown manual-review WARN arm, the
forming-session ``before`` cutoff, the cap-20 + suppressed summary, the
fail-soft env knob, the defensive non-positive-low skip, and the error guard
(a detector crash never propagates).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest
from brutex.data import spot_daily_resample as sdr
from brutex.data.spot_daily_resample import (
    DAILY_RANGE_ERROR_EVENT,
    DAILY_RANGE_EVENT,
    DAILY_RANGE_KNOWN_EVENT,
    DAILY_RANGE_SUPPRESSED_EVENT,
    DAILY_RANGE_WARN_RATIO,
    MAX_RANGE_WARNS,
    ExtremeRangeDay,
    detect_extreme_daily_ranges,
    emit_extreme_daily_ranges,
    observe_extreme_daily_ranges,
)
from structlog.testing import capture_logs

_FAR_FUTURE = date(2026, 12, 31)


def _frame(rows: list[tuple[datetime, float, float]]) -> pl.DataFrame:
    """A minimal 1-minute frame: (ts_utc, high, low) per row."""
    return pl.DataFrame(
        {
            "ts_utc": [r[0] for r in rows],
            "open": [r[2] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [1] * len(rows),
        }
    )


def _day_row(d: date, high: float, low: float) -> tuple[datetime, float, float]:
    # 04:00 UTC = 09:30 IST → the row's IST date is d.
    return (datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC), high, low)


class TestDetectExtremeDailyRanges:
    def test_9_5_percent_day_flags(self) -> None:
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 109.5, 100.0)]), before=_FAR_FUTURE
        )
        assert len(out) == 1
        day = out[0]
        assert day.session_date == date(2024, 1, 3)
        assert day.range_ratio == pytest.approx(0.095)
        assert day.known_halt is False
        assert day.reason is None

    def test_8_5_percent_day_passes(self) -> None:
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 108.5, 100.0)]), before=_FAR_FUTURE
        )
        assert out == ()

    def test_exact_009_boundary_flags(self) -> None:
        # >= threshold, not > — the boundary day flags.
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 109.0, 100.0)]), before=_FAR_FUTURE
        )
        assert len(out) == 1
        assert out[0].range_ratio == pytest.approx(DAILY_RANGE_WARN_RATIO)

    def test_2020_03_23_shaped_day_reads_known_halt(self) -> None:
        # The worst-ever single-day fall (verified 2026-07-11) — annotated.
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2020, 3, 23), 100.0, 87.0)]), before=_FAR_FUTURE
        )
        assert len(out) == 1
        day = out[0]
        assert day.known_halt is True
        assert day.reason == "market_wide_circuit_halt"

    def test_extended_outage_day_reads_known_reason(self) -> None:
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2021, 2, 24), 110.0, 100.0)]), before=_FAR_FUTURE
        )
        assert out[0].known_halt is True
        assert out[0].reason == "exchange_outage_extended_close"

    def test_before_cutoff_excludes_forming_day(self) -> None:
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 120.0, 100.0)]), before=date(2024, 1, 3)
        )
        assert out == ()

    def test_empty_frame_is_a_no_op(self) -> None:
        assert detect_extreme_daily_ranges(_frame([]), before=_FAR_FUTURE) == ()

    def test_non_positive_low_is_skipped_defensively(self) -> None:
        # A non-positive low cannot be ratioed (and cannot enter a price-sane
        # lake) — skipped, never a ZeroDivisionError, siblings still judged.
        out = detect_extreme_daily_ranges(
            _frame(
                [
                    _day_row(date(2024, 1, 3), 100.0, 0.0),
                    _day_row(date(2024, 1, 4), 110.0, 100.0),
                ]
            ),
            before=_FAR_FUTURE,
        )
        assert [d.session_date for d in out] == [date(2024, 1, 4)]

    def test_explicit_threshold_overrides_default(self) -> None:
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 105.0, 100.0)]),
            before=_FAR_FUTURE,
            threshold=0.04,
        )
        assert len(out) == 1

    def test_env_knob_valid_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DAILY_RANGE_WARN_RATIO", "0.20")
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 110.0, 100.0)]), before=_FAR_FUTURE
        )
        assert out == ()  # 10% < the env-raised 20%

    @pytest.mark.parametrize("raw", ["junk", "", "nan", "inf", "-1", "0"])
    def test_env_knob_junk_falls_back(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DAILY_RANGE_WARN_RATIO", raw)
        out = detect_extreme_daily_ranges(
            _frame([_day_row(date(2024, 1, 3), 109.5, 100.0)]), before=_FAR_FUTURE
        )
        assert len(out) == 1  # the 0.09 default still governs


class TestEmitExtremeDailyRanges:
    def test_known_day_emits_one_info_no_warn(self) -> None:
        days = (
            ExtremeRangeDay(
                session_date=date(2020, 3, 13),
                range_ratio=0.12,
                known_halt=True,
                reason="market_wide_circuit_halt",
            ),
        )
        with capture_logs() as logs:
            emit_extreme_daily_ranges(days, symbol="NIFTY")
        infos = [e for e in logs if e["event"] == DAILY_RANGE_KNOWN_EVENT]
        assert len(infos) == 1
        assert infos[0]["reason"] == "market_wide_circuit_halt"
        assert "no action needed" in infos[0]["remediation"]
        assert [e for e in logs if e["event"] == DAILY_RANGE_EVENT] == []

    def test_unknown_day_emits_manual_review_warn(self) -> None:
        days = (
            ExtremeRangeDay(
                session_date=date(2024, 1, 3), range_ratio=0.095, known_halt=False, reason=None
            ),
        )
        with capture_logs() as logs:
            emit_extreme_daily_ranges(days, symbol="NIFTY")
        (warn,) = [e for e in logs if e["event"] == DAILY_RANGE_EVENT]
        assert "CIRCUIT_HALT_SESSIONS" in warn["remediation"]
        assert "citation" in warn["remediation"]
        assert [e for e in logs if e["event"] == DAILY_RANGE_SUPPRESSED_EVENT] == []

    def test_warn_cap_plus_suppressed_summary(self) -> None:
        days = tuple(
            ExtremeRangeDay(
                session_date=date(2024, 1, 1) + timedelta(days=i),
                range_ratio=0.10,
                known_halt=False,
                reason=None,
            )
            for i in range(MAX_RANGE_WARNS + 3)
        )
        with capture_logs() as logs:
            emit_extreme_daily_ranges(days, symbol="NIFTY")
        warns = [e for e in logs if e["event"] == DAILY_RANGE_EVENT]
        assert len(warns) == MAX_RANGE_WARNS
        (summary,) = [e for e in logs if e["event"] == DAILY_RANGE_SUPPRESSED_EVENT]
        assert summary["total"] == MAX_RANGE_WARNS + 3

    def test_zero_hit_scan_emits_nothing(self) -> None:
        with capture_logs() as logs:
            emit_extreme_daily_ranges((), symbol="NIFTY")
        assert logs == []


class TestObserveExtremeDailyRanges:
    def test_detect_and_emit_in_one_guarded_call(self) -> None:
        with capture_logs() as logs:
            count = observe_extreme_daily_ranges(
                _frame([_day_row(date(2024, 1, 3), 109.5, 100.0)]),
                before=_FAR_FUTURE,
                symbol="NIFTY",
            )
        assert count == 1
        assert [e["event"] for e in logs] == [DAILY_RANGE_EVENT]

    def test_error_guard_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected detector crash")

        monkeypatch.setattr(sdr, "detect_extreme_daily_ranges", _boom)
        with capture_logs() as logs:
            count = observe_extreme_daily_ranges(
                _frame([_day_row(date(2024, 1, 3), 109.5, 100.0)]),
                before=_FAR_FUTURE,
                symbol="NIFTY",
            )
        assert count == 0  # honest "not observed", never a fabricated count
        (err,) = [e for e in logs if e["event"] == DAILY_RANGE_ERROR_EVENT]
        assert "injected detector crash" in err["error"]

    def test_frame_is_never_mutated(self) -> None:
        frame = _frame([_day_row(date(2024, 1, 3), 109.5, 100.0)])
        before_bytes = frame.write_csv()
        observe_extreme_daily_ranges(frame, before=_FAR_FUTURE, symbol="NIFTY")
        assert frame.write_csv() == before_bytes
