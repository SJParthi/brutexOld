"""Unique-check pins: clean PASS, planted-dup FAIL, skip≠clean, abstain arms.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brutex.acceptance.checks import unique
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _month_file, poison_duplicate_row

from tests.fakes.acceptance_twin import DAY2, make_ctx, make_twin


class TestRecentMonths:
    def test_year_boundary(self) -> None:
        got = unique.recent_months(date(2026, 2, 15), 4)
        assert got == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


class TestUniqueCheck:
    def test_clean_twin_passes_with_counts(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = unique.run(make_ctx(lake))
        assert check.verdict is Verdict.PASS
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.evidence["findings_total"] == 0
        assert census.evidence["units_checked"] > 0
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.evidence["engine_detail"]["NIFTY"] == "CLEAN"
        assert check.coverage.mode == "bounded"

    def test_planted_dup_fails_naming_offender(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        poison_duplicate_row(lake, symbol="NIFTY", day=DAY2)
        check = unique.run(make_ctx(lake))
        assert check.verdict is Verdict.FAIL
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.FAIL
        assert any("NIFTY" in path for path in census.evidence["offenders"])
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.verdict is Verdict.FAIL  # engine headline flips too

    def test_unreadable_file_fails_never_certifies_clean(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _month_file(lake, "NIFTY", "1minute", DAY2).write_bytes(b"not parquet")
        check = unique.run(make_ctx(lake))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.FAIL
        assert census.evidence["files_skipped"] >= 1
        assert census.evidence["first_skipped_path"] is not None

    def test_missing_lake_abstains(self, tmp_path: Path) -> None:
        check = unique.run(make_ctx(tmp_path / "nowhere"))
        assert check.verdict is Verdict.ABSTAIN
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_LAKE
        assert check.coverage.mode == "none"

    def test_pull_active_abstains_and_force_overrides(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        from datetime import UTC, datetime

        live_now = datetime.now(UTC)
        gated = unique.run(make_ctx(lake, now_utc=lambda: live_now))
        assert gated.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS
        forced = unique.run(make_ctx(lake, now_utc=lambda: live_now, force_during_pull=True))
        assert forced.verdict is Verdict.PASS

    def test_full_mode_single_scan_engine_from_census(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = unique.run(make_ctx(lake, full=True))
        assert check.verdict is Verdict.PASS
        assert check.coverage.mode == "full"
        assert check.limits == ()
        engine = next(s for s in check.sub_checks if s.id == "unique.engine")
        assert engine.verdict is Verdict.PASS

    def test_empty_bars_tree_nothing_to_check(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        check = unique.run(make_ctx(tmp_path))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.verdict is Verdict.ABSTAIN
        assert census.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_offender_cap_disclosed(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        poison_duplicate_row(lake, symbol="NIFTY", day=DAY2)
        check = unique.run(make_ctx(lake))
        census = next(s for s in check.sub_checks if s.id == "unique.census")
        assert census.evidence["offenders_total"] >= 1
        assert census.evidence["offenders_truncated"] is False
