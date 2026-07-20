"""Tests for :mod:`brutex.adapters.dhan.daily_consistency` — the observe-only observer.

DEC-DHAN-NATIVE-DAILY-001 item 7 (implementing `DEC-SCOPE-MINIMAL-CORE-001`):
after both Dhan spot stages complete, the native ``1day`` closes are compared
against the ``1minute`` last-by-timestamp aggregate for the pull's overlapping
days — observe-only (a divergence is a WARN, never a rewrite), capped WARNs,
per-target crash-guarded, whole-observer fenced, fail-loud kill switch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from brutex.adapters.dhan.daily_consistency import (
    DHAN_DAILY_CONSISTENCY_ENV,
    DIVERGENCE_WARN_CAP,
    ConsistencyReport,
    consistency_enabled,
    observe_daily_minute_consistency,
    observe_daily_minute_consistency_guarded,
)
from structlog.testing import capture_logs

# ─────────────────────────────────────────────────────────────────────────────
# consistency_enabled — the fail-loud kill switch
# ─────────────────────────────────────────────────────────────────────────────


class TestConsistencyEnabled:
    @pytest.mark.parametrize("token", ["", "1", "on", "true", "yes", " ON ", "True"])
    def test_on_tokens_and_unset_enable(self, token: str) -> None:
        assert consistency_enabled({DHAN_DAILY_CONSISTENCY_ENV: token}) is True

    def test_absent_key_defaults_enabled(self) -> None:
        assert consistency_enabled({}) is True

    @pytest.mark.parametrize("token", ["0", "off", "false", "no", " OFF "])
    def test_off_tokens_disable(self, token: str) -> None:
        assert consistency_enabled({DHAN_DAILY_CONSISTENCY_ENV: token}) is False

    def test_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match=DHAN_DAILY_CONSISTENCY_ENV):
            consistency_enabled({DHAN_DAILY_CONSISTENCY_ENV: "maybe"})

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DHAN_DAILY_CONSISTENCY_ENV, "off")
        assert consistency_enabled() is False
        monkeypatch.delenv(DHAN_DAILY_CONSISTENCY_ENV)
        assert consistency_enabled() is True


# ─────────────────────────────────────────────────────────────────────────────
# Lake fixture helpers — REAL month-partition parquet files
# ─────────────────────────────────────────────────────────────────────────────

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _midnight_ist_utc(day: date) -> datetime:
    """Midnight IST of *day* stored UTC (18:30 UTC of the prior calendar day)."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC) - _IST_OFFSET


def _write_month(base: Path, rows: list[tuple[datetime, float]]) -> None:
    """Write ``<base>/<yyyy>/<mm>.parquet`` month partitions for (timestamp, close) rows."""
    by_month: dict[tuple[int, int], list[tuple[datetime, float]]] = {}
    for ts, close in rows:
        by_month.setdefault((ts.year, ts.month), []).append((ts, close))
    for (year, month), month_rows in by_month.items():
        dest = base / f"{year:04d}" / f"{month:02d}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(
            {
                "timestamp": [ts for ts, _ in month_rows],
                "close": [close for _, close in month_rows],
            },
            schema={"timestamp": pl.Datetime("us", "UTC"), "close": pl.Float64},
        )
        frame.write_parquet(dest)


def _seed_symbol(
    dhan_root: Path,
    symbol: str,
    *,
    daily: list[tuple[date, float]],
    minute: list[tuple[datetime, float]],
    exchange: str = "NSE",
) -> None:
    symbol_base = dhan_root / "bars" / exchange / "INDEX" / symbol
    _write_month(symbol_base / "1day", [(_midnight_ist_utc(d), c) for d, c in daily])
    _write_month(symbol_base / "1minute", minute)


def _minute_bar(day: date, hour_utc: int, minute_utc: int, close: float) -> tuple[datetime, float]:
    return (datetime(day.year, day.month, day.day, hour_utc, minute_utc, tzinfo=UTC), close)


_START = date(2022, 6, 1)
_END = date(2022, 6, 30)


# ─────────────────────────────────────────────────────────────────────────────
# observe_daily_minute_consistency — the comparison law
# ─────────────────────────────────────────────────────────────────────────────


class TestObserveDailyMinuteConsistency:
    def test_matching_closes_compare_clean(self, tmp_path: Path) -> None:
        day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(day, 100.5)],
            # Two minute bars — LAST by timestamp (100.5) is the aggregate close.
            minute=[_minute_bar(day, 3, 45, 99.0), _minute_bar(day, 10, 0, 100.5)],
        )
        with capture_logs() as logs:
            report = observe_daily_minute_consistency(
                dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
            )
        assert report.compared_days == 1
        assert report.divergences == ()
        assert report.warned == 0
        assert report.suppressed == 0
        assert report.targets_failed == {}
        assert [e for e in logs if e["event"] == "dhan.daily_minute_divergence"] == []

    def test_divergence_past_tolerance_warns_with_rel_diff(self, tmp_path: Path) -> None:
        day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(day, 100.0)],
            minute=[_minute_bar(day, 10, 0, 102.0)],  # 2% > the 0.5% tolerance
        )
        with capture_logs() as logs:
            report = observe_daily_minute_consistency(
                dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
            )
        assert report.compared_days == 1
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert (d.symbol, d.day, d.daily_close, d.minute_close) == ("NIFTY", day, 100.0, 102.0)
        assert d.rel_diff == pytest.approx(0.02)
        warns = [e for e in logs if e["event"] == "dhan.daily_minute_divergence"]
        assert len(warns) == 1
        assert warns[0]["symbol"] == "NIFTY"
        assert report.warned == 1
        assert report.suppressed == 0

    def test_zero_daily_close_divergence_reads_inf(self, tmp_path: Path) -> None:
        # Division-free predicate: any non-zero minute close diverges from a
        # zero daily close; the RECORDED rel_diff is the honest inf.
        day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(day, 0.0)],
            minute=[_minute_bar(day, 10, 0, 1.0)],
        )
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert len(report.divergences) == 1
        assert report.divergences[0].rel_diff == float("inf")

    def test_zero_equals_zero_does_not_diverge(self, tmp_path: Path) -> None:
        day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(day, 0.0)],
            minute=[_minute_bar(day, 10, 0, 0.0)],
        )
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert report.compared_days == 1
        assert report.divergences == ()

    def test_day_on_one_side_only_skipped_honestly(self, tmp_path: Path) -> None:
        # Daily-but-no-minute is a completeness matter, never a divergence.
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(date(2022, 6, 1), 100.0), (date(2022, 6, 2), 200.0)],
            minute=[_minute_bar(date(2022, 6, 1), 10, 0, 100.0)],
        )
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert report.compared_days == 1
        assert report.divergences == ()

    def test_daily_day_outside_range_skipped(self, tmp_path: Path) -> None:
        # A same-month daily day OUTSIDE the half-open [start, end) is skipped.
        inside = date(2022, 6, 1)
        outside = date(2022, 6, 10)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(inside, 100.0), (outside, 100.0)],
            minute=[
                _minute_bar(inside, 10, 0, 100.0),
                _minute_bar(outside, 10, 0, 500.0),  # would diverge if compared
            ],
        )
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path,
            targets=[("NSE", "NIFTY")],
            start=inside,
            end=date(2022, 6, 3),
        )
        assert report.compared_days == 1
        assert report.divergences == ()

    def test_warn_cap_suppresses_with_one_summary(self, tmp_path: Path) -> None:
        days = [date(2022, 6, 1), date(2022, 6, 2), date(2022, 6, 3)]
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(d, 100.0) for d in days],
            minute=[_minute_bar(d, 10, 0, 150.0) for d in days],  # all diverge
        )
        with capture_logs() as logs:
            report = observe_daily_minute_consistency(
                dhan_root=tmp_path,
                targets=[("NSE", "NIFTY")],
                start=_START,
                end=_END,
                warn_cap=1,
            )
        assert len(report.divergences) == 3  # the RECORD is never capped
        assert report.warned == 1
        assert report.suppressed == 2
        warns = [e for e in logs if e["event"] == "dhan.daily_minute_divergence"]
        assert len(warns) == 1
        summaries = [e for e in logs if e["event"] == "dhan.daily_minute_divergence_suppressed"]
        assert len(summaries) == 1
        assert summaries[0]["suppressed"] == 2
        assert summaries[0]["total"] == 3

    def test_default_warn_cap_is_module_constant(self, tmp_path: Path) -> None:
        # A tiny sanity pin on the default cap wiring (no divergences needed).
        _seed_symbol(tmp_path, "NIFTY", daily=[], minute=[])
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert DIVERGENCE_WARN_CAP == 20
        assert report.compared_days == 0

    def test_absent_partitions_compare_nothing(self, tmp_path: Path) -> None:
        # No files at all (the empty-files arm of _last_close_by_day).
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert report.compared_days == 0
        assert report.targets_failed == {}

    def test_empty_parquet_frame_compares_nothing(self, tmp_path: Path) -> None:
        # A schema-complete ZERO-row month file (the empty-frame arm).
        base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day"
        dest = base / "2022" / "06.parquet"
        dest.parent.mkdir(parents=True)
        pl.DataFrame(
            {"timestamp": [], "close": []},
            schema={"timestamp": pl.Datetime("us", "UTC"), "close": pl.Float64},
        ).write_parquet(dest)
        report = observe_daily_minute_consistency(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert report.compared_days == 0
        assert report.targets_failed == {}

    def test_per_target_fault_isolated_and_counted(self, tmp_path: Path) -> None:
        # A garbage parquet on ONE target's read faults that target only — the
        # sibling still runs (per-target crash guard, observe-only).
        bad_base = tmp_path / "bars" / "NSE" / "INDEX" / "BADSYM" / "1day" / "2022"
        bad_base.mkdir(parents=True)
        (bad_base / "06.parquet").write_bytes(b"not a parquet file")
        good_day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(good_day, 100.0)],
            minute=[_minute_bar(good_day, 10, 0, 100.0)],
        )
        with capture_logs() as logs:
            report = observe_daily_minute_consistency(
                dhan_root=tmp_path,
                targets=[("NSE", "BADSYM"), ("NSE", "NIFTY")],
                start=_START,
                end=_END,
            )
        assert "BADSYM" in report.targets_failed
        assert report.compared_days == 1  # the sibling still compared
        faults = [e for e in logs if e["event"] == "dhan.daily_minute_consistency_target_error"]
        assert len(faults) == 1
        assert faults[0]["symbol"] == "BADSYM"


# ─────────────────────────────────────────────────────────────────────────────
# observe_daily_minute_consistency_guarded — the whole-observer fence
# ─────────────────────────────────────────────────────────────────────────────


class TestGuardedObserver:
    def test_delegates_and_returns_the_report(self, tmp_path: Path) -> None:
        day = date(2022, 6, 1)
        _seed_symbol(
            tmp_path,
            "NIFTY",
            daily=[(day, 100.0)],
            minute=[_minute_bar(day, 10, 0, 100.0)],
        )
        report = observe_daily_minute_consistency_guarded(
            dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
        )
        assert isinstance(report, ConsistencyReport)
        assert report.compared_days == 1

    def test_any_raise_is_fenced_to_none_with_one_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.adapters.dhan.daily_consistency as mod

        def _boom(**_kwargs: object) -> ConsistencyReport:
            raise RuntimeError("observer defect")

        monkeypatch.setattr(mod, "observe_daily_minute_consistency", _boom)
        with capture_logs() as logs:
            result = observe_daily_minute_consistency_guarded(
                dhan_root=tmp_path, targets=[("NSE", "NIFTY")], start=_START, end=_END
            )
        assert result is None
        fences = [e for e in logs if e["event"] == "dhan.daily_minute_consistency_error"]
        assert len(fences) == 1
        assert fences[0]["error_type"] == "RuntimeError"
