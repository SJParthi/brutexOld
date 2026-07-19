"""Self-test pins: smoke detection, battery table, the comparator-inversion tooth.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958). The inversion tooth
proves the self-test is not decorative: lobotomize the comparator and the
battery itself must flip to TEETH_FAILED.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.acceptance import selftest
from brutex.acceptance.model import TeethVerdict

from tests.fakes.acceptance_twin import NOW


class TestTwinSessions:
    def test_pinned_clock_resolves_the_two_prior_sessions(self) -> None:
        assert selftest.twin_sessions(NOW) == (date(2026, 7, 8), date(2026, 7, 9))

    def test_coverage_exhausted_falls_back_to_pinned_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.calendars import nse

        monkeypatch.setattr(nse, "previous_trading_session", lambda d, **k: None)
        assert selftest.twin_sessions(NOW) == (date(2026, 7, 8), date(2026, 7, 9))

    def test_single_session_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.calendars import nse

        calls = {"n": 0}

        def _one_shot(d: date, **kwargs: object) -> date | None:
            calls["n"] += 1
            return date(2026, 7, 9) if calls["n"] == 1 else None

        monkeypatch.setattr(nse, "previous_trading_session", _one_shot)
        assert selftest.twin_sessions(NOW) == (date(2026, 7, 8), date(2026, 7, 9))


class TestSmoke:
    def test_smoke_teeth_proven(self) -> None:
        result = selftest.run_smoke(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_PROVEN
        assert result.poisons_planted == 3
        assert result.poisons_detected == 3
        assert result.false_fails_on_clean == 0
        assert result.mode == "smoke"
        assert result.to_dict()["verdict"] == "TEETH_PROVEN"

    def test_smoke_default_clock(self) -> None:
        # The no-arg arm binds the real clock — still deterministic teeth.
        result = selftest.run_smoke()
        assert result.verdict is TeethVerdict.TEETH_PROVEN

    def test_inverted_comparator_fails_smoke(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.acceptance.checks import movers as movers_check

        monkeypatch.setattr(movers_check, "compare_spot_cells", lambda *a, **k: [])
        result = selftest.run_smoke(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        missed = [o for o in result.outcomes if not o.detected]
        assert [o.name for o in missed] == ["movers_cell"]

    def test_blinded_unique_check_fails_smoke(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.acceptance.checks import unique as unique_check
        from brutex.acceptance.model import (
            Verdict,
            make_check,
            make_coverage,
            make_subcheck,
        )

        def _always_pass(ctx):
            sub = make_subcheck("unique.census", Verdict.PASS, units_checked=1, evidence={"k": 1})
            return make_check(
                "unique",
                [sub],
                coverage=make_coverage(mode="full", population=1, examined=1, seed="s"),
            )

        monkeypatch.setattr(unique_check, "run", _always_pass)
        result = selftest.run_smoke(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED


class TestSkipped:
    def test_skipped_record(self) -> None:
        result = selftest.skipped_selftest()
        assert result.verdict is TeethVerdict.TEETH_SKIPPED
        assert result.mode == "skipped"


class TestRenderTable:
    def test_table_lists_every_poison(self) -> None:
        result = selftest.run_smoke(now_utc=NOW)
        text = selftest.render_battery_table(result)
        assert "TEETH_PROVEN" in text
        for outcome in result.outcomes:
            assert outcome.name in text
        assert "false_fails_on_clean=0" in text

    def test_table_marks_missed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.acceptance.checks import movers as movers_check

        monkeypatch.setattr(movers_check, "compare_spot_cells", lambda *a, **k: [])
        text = selftest.render_battery_table(selftest.run_smoke(now_utc=NOW))
        assert "MISSED" in text


class TestPoison1dayValueBranches:
    """Round-5 F2: the tf.daily VALUE poison helper's multi-file + no-bar arms."""

    def _twin(self, tmp_path: Path) -> Path:
        root = tmp_path / "twin"
        selftest.build_twin_lake(root, days=(date(2026, 7, 8), date(2026, 7, 9)))
        return root

    def test_poison_walks_past_a_non_matching_month_file(self, tmp_path: Path) -> None:
        # Plant an EXTRA 1day bar in an earlier month so the target day's bar
        # lives in a LATER-sorted file — forcing the per-file `continue` arm.
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter

        root = self._twin(tmp_path)
        early = datetime.combine(date(2026, 3, 1), time(18, 30), tzinfo=selftest.UTC)
        ParquetWriter(lake_root=root).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=early,
                    open=Decimal("1.00"),
                    high=Decimal("2.00"),
                    low=Decimal("0.50"),
                    close=Decimal("1.50"),
                    volume=1,
                    open_interest=None,
                )
            ],
        )
        day_dir = root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day"
        assert len(list(day_dir.rglob("*.parquet"))) >= 2  # multi-file
        selftest.poison_1day_value(root, symbol="NIFTY", day=date(2026, 7, 9))
        # the target day-9 bar was corrupted (its file is not the first-sorted).
        import polars as pl

        found = False
        for path in day_dir.rglob("*.parquet"):
            frame = pl.read_parquet(path, glob=False)
            for ts, close in zip(
                frame.get_column("timestamp").to_list(),
                frame.get_column("close").to_list(),
                strict=True,
            ):
                if (ts + timedelta(hours=5, minutes=30)).date() == date(2026, 7, 9):
                    assert float(close) > 110.0  # bumped
                    found = True
        assert found

    def test_poison_raises_when_no_bar_for_day(self, tmp_path: Path) -> None:
        root = self._twin(tmp_path)
        with pytest.raises(RuntimeError, match="no stored 1day bar"):
            selftest.poison_1day_value(root, symbol="NIFTY", day=date(2020, 1, 2))


class TestBatteryGuardDateWalk:
    def test_in_window_poison_walks_past_weekend_guard_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import date as _date

        from brutex.acceptance.checks import rate as rate_check

        # A Saturday guard-landing date forces the poison's walk-forward arm.
        monkeypatch.setattr(rate_check, "MARKET_HOURS_GUARD_LANDED", _date(2026, 7, 11))
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_PROVEN
