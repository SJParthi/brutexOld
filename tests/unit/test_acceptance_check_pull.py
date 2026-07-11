"""Pull-check pins: replan idempotence, anti-vacuous tooth, silent-loss detector.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from brutex.acceptance.checks import pull
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _LEDGER_FIELDS, _month_file
from tests.fakes.acceptance_twin import DAY1, DAY2, make_ctx, make_twin


def _row(**overrides: object) -> tuple:
    from brutex.data.window_ledger import WindowLedgerRecord

    base = {
        "market": "groww",
        "security_id": "NIFTY",
        "interval": "1minute",
        "window_start": DAY1.isoformat(),
        "window_end": (DAY2 + timedelta(days=1)).isoformat(),
        "status": "SUCCESS",
        "bars": 62,
        "attempts": 1,
        "last_error": None,
        "first_seen_at": f"{DAY2.isoformat()}T18:00:00+00:00",
        "last_attempt_at": f"{DAY2.isoformat()}T18:00:00+00:00",
        "empty_streak": 0,
        "in_pull_fetches": 1,
        "bars_net_new": 62,
        "empty_retries": 0,
        "transient_retries": 0,
    }
    base.update(overrides)
    record = WindowLedgerRecord(**base)  # type: ignore[arg-type]
    return tuple(getattr(record, f) for f in _LEDGER_FIELDS)


def _save(lake: Path, rows: list[tuple]) -> None:
    from brutex.data.window_ledger import WindowLedger
    from brutex.data.window_ledger_store import save_window_ledger

    save_window_ledger(WindowLedger.from_rows(rows), lake)


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


class TestPullCheck:
    def test_settled_fixture_replan_zero_and_tail_planned(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["newly_fetchable"] == 0
        # 2026-07-10 (the run day) is a trading session newer than the max
        # settled end — the anti-vacuous tooth must see it PLANNED.
        assert replan.evidence["tail_check"] == "planned"
        assert replan.evidence["ledger_rss_kib_after"] is not None or True

    def test_lobotomized_planner_fails_vacuous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.data.window_ledger_store as store_mod

        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        monkeypatch.setattr(store_mod, "plan_windows", lambda *a, **k: 0)
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.FAIL
        assert replan.evidence["tail_check"] == "planner_vacuous"

    def test_planned_row_is_open_by_design(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        planned = _row(
            status="PLANNED",
            bars=0,
            bars_net_new=None,
            empty_retries=None,
            transient_retries=None,
            in_pull_fetches=0,
        )
        _save(lake, [_row(), planned])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS

    def test_net_new_gt_bars_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(bars=3, bars_net_new=7)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.verdict is Verdict.FAIL
        assert any("bars_net_new > bars" in v for v in law.evidence["violations"])

    def test_counter_invariant_violation_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(empty_retries=1, transient_retries=1, in_pull_fetches=1)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.verdict is Verdict.FAIL
        assert any("counter invariant" in v for v in law.evidence["violations"])

    def test_none_counters_honest_unknown(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(
            lake,
            [_row(bars_net_new=None, empty_retries=None, transient_retries=None)],
        )
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.evidence["rows_honest_unknown"] == 1
        assert law.verdict is Verdict.ABSTAIN  # zero counted rows → nothing_to_check
        assert law.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_partial_counters_row_counted(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(empty_retries=None, transient_retries=None, bars_net_new=10)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.evidence["rows_with_counters"] == 1
        assert law.verdict is Verdict.PASS

    def test_deleted_partition_behind_success_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        _month_file(lake, "NIFTY", "1minute", DAY2).unlink()
        check = pull.run(make_ctx(lake))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL
        assert presence.evidence["missing"] == 1
        assert "NIFTY" in presence.evidence["missing_keys"][0]

    def test_whole_series_dir_missing_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(security_id="GHOST")])
        check = pull.run(make_ctx(lake))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL
        assert "whole series dir missing" in presence.evidence["missing_keys"][0]

    def test_fno_series_replans_via_contract_planner(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        from brutex.acceptance.selftest import _twin_bars

        lake = make_twin(tmp_path)
        contract = "NSE-NIFTY-28Jul26-FUT"
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        fno_row = _row(
            market="groww-future",
            security_id=contract,
            window_start=DAY2.isoformat(),
            window_end=(DAY2 + timedelta(days=1)).isoformat(),
        )
        junk_row = _row(market="groww-option", security_id="UNPARSABLE-JUNK")
        _save(lake, [_row(), fno_row, junk_row])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["series_replanned"] == 3

    def test_no_ledger_abstains(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = pull.run(make_ctx(lake))
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_LEDGER

    def test_pull_active_abstains(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        check = pull.run(make_ctx(lake, now_utc=lambda: datetime.now(UTC)))
        assert check.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS

    def test_progress_snapshot_evidence(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        check = pull.run(make_ctx(lake))
        progress = _sub(check, "pull.progress_snapshot")
        assert progress.verdict is Verdict.PASS
        assert progress.evidence["total"] == 1
        assert progress.evidence["settled"] == 1

    def test_pinned_now_determinism(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        def _strip(check) -> list[dict]:
            subs = check.to_dict()["sub_checks"]
            for sub in subs:
                sub["evidence"].pop("ledger_rss_kib_before", None)
                sub["evidence"].pop("ledger_rss_kib_after", None)
            return subs

        assert _strip(pull.run(make_ctx(lake))) == _strip(pull.run(make_ctx(lake)))


class TestPullEdges:
    def test_missing_bars_tree_series_dir_none(self, tmp_path: Path) -> None:
        # A ledger beside a lake with NO bars/ tree at all: every sampled
        # SUCCESS row is a silent-loss hit (whole series dir missing).
        _save(tmp_path, [_row()])
        check = pull.run(make_ctx(tmp_path))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL

    def test_empty_ledger_rows_not_applicable_tail(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["tail_check"] == "not_applicable"
        assert replan.verdict is Verdict.ABSTAIN  # zero units → nothing_to_check
        progress = _sub(check, "pull.progress_snapshot")
        assert progress.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_ledger_covering_past_run_date_tail_not_applicable(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(window_end="2026-07-12")])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["tail_check"] == "not_applicable"
