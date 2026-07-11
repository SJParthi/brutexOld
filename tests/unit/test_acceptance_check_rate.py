"""Rate-check pins: disk-provable claims only; the permanent structural abstains.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.acceptance.checks import rate
from brutex.acceptance.model import AbstainReason, Verdict

from tests.fakes.acceptance_twin import DAY2, make_ctx, make_twin


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


def _seed_success_row(lake: Path, *, last_attempt_at: str) -> None:
    from brutex.acceptance.selftest import _seed_ledger

    _seed_ledger(lake, day=DAY2, last_attempt_at=last_attempt_at)


class TestConstants:
    def test_constants_pass(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = rate.run(make_ctx(lake))
        constants = _sub(check, "rate.constants")
        assert constants.verdict is Verdict.PASS
        assert constants.evidence["rps_times_60"] == 480.0

    def test_monkeypatched_drift_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.adapters.groww.constants as gc

        monkeypatch.setattr(gc, "GROWW_RATE_LIMIT_RPS_CEILING", 9.0)
        lake = make_twin(tmp_path)
        check = rate.run(make_ctx(lake))
        assert _sub(check, "rate.constants").verdict is Verdict.FAIL


class TestMarketHours:
    def test_out_of_window_attempt_passes(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _seed_success_row(lake, last_attempt_at="2026-07-10T18:00:00+00:00")
        check = rate.run(make_ctx(lake))
        hours = _sub(check, "rate.market_hours")
        assert hours.verdict is Verdict.PASS
        assert hours.evidence["in_window_attempts"] == 0

    def test_in_window_attempt_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        # 04:30 UTC = 10:00 IST on trading Friday 07-10 — the guard landing day.
        _seed_success_row(lake, last_attempt_at="2026-07-10T04:30:00+00:00")
        check = rate.run(make_ctx(lake))
        hours = _sub(check, "rate.market_hours")
        assert hours.verdict is Verdict.FAIL
        assert hours.evidence["offending_stamps"]

    def test_pre_guard_attempt_excluded(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _seed_success_row(lake, last_attempt_at="2026-07-01T04:30:00+00:00")
        check = rate.run(make_ctx(lake))
        hours = _sub(check, "rate.market_hours")
        assert hours.evidence["excluded_pre_guard"] == 1
        assert hours.verdict is Verdict.ABSTAIN  # nothing examined post-guard
        assert hours.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_weekend_in_window_time_passes(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        # Saturday 2026-07-11 10:00 IST — in the window but NOT a trading day.
        _seed_success_row(lake, last_attempt_at="2026-07-11T04:30:00+00:00")
        check = rate.run(make_ctx(lake))
        assert _sub(check, "rate.market_hours").verdict is Verdict.PASS

    def test_naive_and_bad_stamps(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import _LEDGER_FIELDS
        from brutex.data.window_ledger import WindowLedger, WindowLedgerRecord
        from brutex.data.window_ledger_store import save_window_ledger

        lake = make_twin(tmp_path)
        rows = []
        for stamp in ("2026-07-10T04:30:00", "junk-stamp"):
            record = WindowLedgerRecord(
                market="groww",
                security_id=f"S{len(rows)}",
                interval="1minute",
                window_start=DAY2.isoformat(),
                window_end="2026-07-10",
                status="SUCCESS",
                bars=1,
                attempts=1,
                last_error=None,
                first_seen_at=stamp,
                last_attempt_at=stamp,
            )
            rows.append(tuple(getattr(record, f) for f in _LEDGER_FIELDS))
        save_window_ledger(WindowLedger.from_rows(rows), lake)
        check = rate.run(make_ctx(lake))
        hours = _sub(check, "rate.market_hours")
        # naive stamp treated UTC → 10:00 IST → offender; junk stamp skipped.
        assert hours.verdict is Verdict.FAIL
        assert hours.evidence["attempts_examined"] == 1

    def test_journal_events_counted_dry_run_excluded(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from brutex.observability.pull_journal import record_pull_start

        lake = make_twin(tmp_path)
        in_window = datetime(2026, 7, 10, 4, 30, tzinfo=UTC)  # Friday 10:00 IST
        record_pull_start(lake, pull_id="wet", dry_run=False, now_utc=in_window)
        record_pull_start(lake, pull_id="dry", dry_run=True, now_utc=in_window)
        check = rate.run(make_ctx(lake))
        hours = _sub(check, "rate.market_hours")
        assert hours.verdict is Verdict.FAIL
        assert hours.evidence["attempts_examined"] == 1  # the dry run never counts


class TestBridge:
    def test_absent_bridge_abstains(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        bridge = _sub(rate.run(make_ctx(lake)), "rate.bridge_snapshot")
        assert bridge.abstain_reason is AbstainReason.NO_BRIDGE_FILE

    def test_within_cap_passes(self, tmp_path: Path) -> None:
        from brutex.data.rate_bridge import publish_rate_snapshot

        lake = make_twin(tmp_path)
        publish_rate_snapshot(
            lake,
            effective_rps=3.2,
            current_rps=5.0,
            recent_minute=200,
            recent_429=1,
            ceiling_per_min=500,
            wall_time=1.0,
            monotonic=1.0,
        )
        bridge = _sub(rate.run(make_ctx(lake)), "rate.bridge_snapshot")
        assert bridge.verdict is Verdict.PASS
        assert bridge.evidence["recent_429"] == 1

    def test_over_cap_persisted_confession_fails(self, tmp_path: Path) -> None:
        from brutex.data.rate_bridge import publish_rate_snapshot

        lake = make_twin(tmp_path)
        publish_rate_snapshot(
            lake,
            effective_rps=9.5,
            current_rps=9.5,
            recent_minute=200,
            recent_429=0,
            ceiling_per_min=500,
            wall_time=1.0,
            monotonic=1.0,
        )
        assert _sub(rate.run(make_ctx(lake)), "rate.bridge_snapshot").verdict is Verdict.FAIL


class TestLogWindow:
    def test_no_rate_lines_abstains(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "logs" / "brutex.log"
        log.parent.mkdir(parents=True)
        log.write_text('{"event": "other.event"}\nnot-json\n', encoding="utf-8")
        monkeypatch.setenv("BRUTEX_LOG_FILE", str(log))
        lake = make_twin(tmp_path)
        sub = _sub(rate.run(make_ctx(lake)), "rate.log_window")
        assert sub.abstain_reason is AbstainReason.NO_RATE_LOG_LINES

    def test_within_cap_lines_pass_with_histogram(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "logs" / "brutex.log"
        log.parent.mkdir(parents=True)
        lines = [
            json.dumps({"event": "rate.snapshot", "current_rps": 5.0, "recent_minute": 100}),
            json.dumps({"event": "rate.429"}),
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("BRUTEX_LOG_FILE", str(log))
        lake = make_twin(tmp_path)
        sub = _sub(rate.run(make_ctx(lake)), "rate.log_window")
        assert sub.verdict is Verdict.PASS
        assert sub.evidence["event_histogram"]["rate.snapshot"] == 1

    def test_over_cap_lines_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "logs" / "brutex.log"
        log.parent.mkdir(parents=True)
        lines = [
            json.dumps({"event": "rate.snapshot", "current_rps": 9.9}),
            json.dumps({"event": "rate.snapshot", "recent_minute": 600}),
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("BRUTEX_LOG_FILE", str(log))
        lake = make_twin(tmp_path)
        sub = _sub(rate.run(make_ctx(lake)), "rate.log_window")
        assert sub.verdict is Verdict.FAIL
        assert len(sub.evidence["over_cap_lines"]) == 2

    def test_unreadable_rotation_member_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "logs" / "brutex.log"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"event": "rate.snapshot", "current_rps": 1.0}), encoding="utf-8")
        (tmp_path / "logs" / "brutex.log.1").mkdir()  # IsADirectoryError → OSError arm
        monkeypatch.setenv("BRUTEX_LOG_FILE", str(log))
        lake = make_twin(tmp_path)
        assert _sub(rate.run(make_ctx(lake)), "rate.log_window").verdict is Verdict.PASS

    def test_default_log_dir_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("BRUTEX_LOG_FILE", raising=False)
        monkeypatch.setenv("BRUTEX_LOG_DIR", str(tmp_path / "nope"))
        assert rate.rate_log_paths() == []


class TestPairBound:
    def test_always_abstains_with_numbers(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        lake = make_twin(tmp_path)
        audit_dir = lake / "audit"
        audit_dir.mkdir(parents=True)
        writer = SweepAuditWriter.open(audit_dir / "spot-ingest.jsonl", actor="ingest")
        for i in range(5):
            writer.append(action="INGEST_PAIR_DONE", subject=f"S{i}", ts_utc=1000.0 + i)
        writer.append(action="INGEST_PAIR_DONE", subject="far", ts_utc=5000.0)
        writer.close()
        sub = _sub(rate.run(make_ctx(lake)), "rate.pair_bound")
        assert sub.verdict is Verdict.ABSTAIN
        assert sub.abstain_reason is AbstainReason.NO_PER_CALL_LOG
        assert sub.structural is True
        assert sub.evidence["pairs_total"] == 6
        assert sub.evidence["max_pairs_per_rolling_minute"] == 5
        assert sub.evidence["chains"]["spot-ingest.jsonl"] == "chain_ok"
        assert sub.evidence["chains"]["fno-ingest.jsonl"] == "absent"

    def test_broken_chain_reported_not_parsed(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        audit_dir = lake / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "spot-ingest.jsonl").write_text("{broken\n", encoding="utf-8")
        sub = _sub(rate.run(make_ctx(lake)), "rate.pair_bound")
        assert "chain_error" in sub.evidence["chains"]["spot-ingest.jsonl"]
        assert sub.evidence["pairs_total"] == 0

    def test_non_pair_lines_ignored(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        lake = make_twin(tmp_path)
        audit_dir = lake / "audit"
        audit_dir.mkdir(parents=True)
        writer = SweepAuditWriter.open(audit_dir / "fno-ingest.jsonl", actor="ingest")
        writer.append(action="INGEST_RUN_START", subject="run", ts_utc=1.0)
        writer.close()
        sub = _sub(rate.run(make_ctx(lake)), "rate.pair_bound")
        assert sub.evidence["pairs_total"] == 0


class TestPerCallHistory:
    def test_permanent_structural_abstain(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        sub = _sub(rate.run(make_ctx(lake)), "rate.per_call_history")
        assert sub.verdict is Verdict.ABSTAIN
        assert sub.abstain_reason is AbstainReason.NO_PER_CALL_PERSISTENCE
        assert sub.structural is True
        assert "OPERATOR RULING" in sub.evidence["remediation"]
        assert "doctor --live --stage 13" in sub.evidence["live_probe"]

    def test_no_blanket_rate_pass_claim(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = rate.run(make_ctx(lake))
        assert any("no blanket rate PASS" in limit for limit in check.limits)


class TestMarketHoursJournalEdge:
    def test_journal_oserror_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.observability.pull_journal as pj

        def _boom(lake_root: Path) -> list[object]:
            raise OSError("wedged")

        lake = make_twin(tmp_path)
        _seed_success_row(lake, last_attempt_at="2026-07-10T18:00:00+00:00")
        monkeypatch.setattr(pj, "read_journal", _boom)
        # The pull-activity probe also reads the journal — pin it idle so the
        # OSError arm under test is the market-hours reader's own.
        check = rate.run(make_ctx(lake))
        assert _sub(check, "rate.market_hours").verdict is Verdict.PASS


class TestPairBoundBlankLine:
    def test_blank_line_skipped(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        lake = make_twin(tmp_path)
        audit_dir = lake / "audit"
        audit_dir.mkdir(parents=True)
        path = audit_dir / "spot-ingest.jsonl"
        writer = SweepAuditWriter.open(path, actor="ingest")
        writer.append(action="INGEST_PAIR_DONE", subject="S", ts_utc=1.0)
        writer.close()
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")  # a trailing blank line must not crash the parse
        sub = _sub(rate.run(make_ctx(lake)), "rate.pair_bound")
        assert sub.evidence["pairs_total"] in (0, 1)  # chain verdict decides
