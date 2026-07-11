"""Context pins: pull-activity probe arms, TOCTOU retry-once law, shared loads.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from brutex.acceptance.context import (
    PULL_ACTIVE_STALE_S,
    AcceptanceContext,
    fingerprint_paths,
    stat_token,
)

NOW = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)  # 11:30 IST


def _ctx(tmp_path: Path, **kwargs: object) -> AcceptanceContext:
    return AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: NOW, **kwargs)


class TestClocks:
    def test_now_ist_and_run_date(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.now_ist() == datetime(2026, 7, 10, 11, 30)
        assert ctx.run_date().isoformat() == "2026-07-10"

    def test_default_clock_is_utc_now(self, tmp_path: Path) -> None:
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s")
        assert abs((ctx.now_utc() - datetime.now(UTC)).total_seconds()) < 5


class TestStatTokens:
    def test_token_and_absent(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_text("hi", encoding="utf-8")
        assert ":" in stat_token(f)
        assert stat_token(tmp_path / "nope") == "absent"

    def test_fingerprint_paths(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_text("hi", encoding="utf-8")
        fp = fingerprint_paths([f])
        assert fp[str(f)] == stat_token(f)


class TestSharedLedger:
    def test_absent_ledger_returns_none(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.ledger() is None
        assert ctx.ledger_loads == 0

    def test_ledger_loaded_once(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger import WindowLedger, WindowLedgerRecord
        from brutex.data.window_ledger_store import save_window_ledger

        record = WindowLedgerRecord(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            window_start="2026-07-08",
            window_end="2026-07-09",
            status="SUCCESS",
            bars=5,
            attempts=1,
            last_error=None,
            first_seen_at="2026-07-08T18:00:00+00:00",
            last_attempt_at="2026-07-08T18:00:00+00:00",
        )
        ledger = WindowLedger()
        ledger._by_window[("groww", "NIFTY", "1minute", "2026-07-08", "2026-07-09")] = record
        save_window_ledger(ledger, tmp_path)
        ctx = _ctx(tmp_path)
        first = ctx.ledger()
        second = ctx.ledger()
        assert first is second  # the ONE shared load — spy-pinned
        assert ctx.ledger_loads == 1


class TestClientFactory:
    def test_injected_factory_wins_and_memoises(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def factory() -> str:
            calls.append(1)
            return "fake-client"

        ctx = _ctx(tmp_path, client_factory=factory)
        assert ctx.client() == "fake-client"
        assert ctx.client() == "fake-client"
        assert len(calls) == 1

    def test_default_factory_builds_real_app(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        ctx = _ctx(tmp_path)
        client = ctx.client()
        # No instruments.duckdb beside this lake: env-based lake resolution arm.
        assert os.environ.get("BRUTEX_LAKE_ROOT") == str(tmp_path)
        response = client.get("/api/v1/state")
        assert response.status_code == 200

    def test_default_factory_opens_store_when_present(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        from brutex.data.instrument_store import InstrumentStore

        InstrumentStore.open(tmp_path / "instruments.duckdb").close()
        ctx = _ctx(tmp_path)
        assert ctx.client().get("/api/v1/state").status_code == 200


class TestPullActiveProbe:
    def test_idle_lake(self, tmp_path: Path) -> None:
        assert _ctx(tmp_path).pull_active() is False

    def test_probe_memoised(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.pull_active() is False
        # flip the underlying state; the memo must hold for the run
        from brutex.observability.pull_journal import record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        assert ctx.pull_active() is False

    def test_live_journal_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.observability.pull_journal import record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is True

    def test_finished_journal_run_idle(self, tmp_path: Path) -> None:
        from brutex.observability.pull_journal import record_pull_finish, record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        record_pull_finish(tmp_path, pull_id="p1", verdict="ok")
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is False

    def test_stale_unfinished_run_idle(self, tmp_path: Path) -> None:
        from brutex.observability.pull_journal import record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        later = datetime.now(UTC) + timedelta(seconds=PULL_ACTIVE_STALE_S + 60)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: later)
        assert ctx.pull_active() is False

    def test_heartbeat_extends_liveness(self, tmp_path: Path) -> None:
        from brutex.observability.pull_journal import record_pull_heartbeat, record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        record_pull_heartbeat(tmp_path, pull_id="p1")
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is True

    def test_bad_journal_stamp_is_idle(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal" / "pull_journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            '{"event": "started", "pull_id": "p", "wall_utc": "not-a-date", '
            '"monotonic_s": 0.0, "dry_run": false}\n',
            encoding="utf-8",
        )
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is False

    def test_fresh_lease_active(self, tmp_path: Path) -> None:
        lease = tmp_path / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True)
        lease.write_text("{}", encoding="utf-8")
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is True

    def test_stale_lease_idle(self, tmp_path: Path) -> None:
        lease = tmp_path / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True)
        lease.write_text("{}", encoding="utf-8")
        later = datetime.now(UTC) + timedelta(seconds=600)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: later)
        assert ctx.pull_active() is False


class TestFingerprintedCompute:
    def test_clean_run(self, tmp_path: Path) -> None:
        f = tmp_path / "input.txt"
        f.write_text("v1", encoding="utf-8")
        ctx = _ctx(tmp_path)
        result, info, ok = ctx.run_fingerprinted(lambda: [f], lambda: 42)
        assert (result, ok) == (42, True)
        assert info == {"moved": False, "retried": False}

    def test_moved_once_retries_then_succeeds(self, tmp_path: Path) -> None:
        f = tmp_path / "input.txt"
        f.write_text("v1", encoding="utf-8")
        ctx = _ctx(tmp_path)
        state = {"calls": 0}

        def compute() -> int:
            state["calls"] += 1
            if state["calls"] == 1:  # mutate INSIDE the first compute (the TOCTOU pin)
                f.write_text("v2-moved-mid-read", encoding="utf-8")
            return state["calls"]

        result, info, ok = ctx.run_fingerprinted(lambda: [f], compute)
        assert ok is True
        assert result == 2  # the retried compute's value
        assert info == {"moved": True, "retried": True}

    def test_moved_twice_abstains(self, tmp_path: Path) -> None:
        f = tmp_path / "input.txt"
        f.write_text("v1", encoding="utf-8")
        ctx = _ctx(tmp_path)
        state = {"calls": 0}

        def compute() -> int:
            state["calls"] += 1
            f.write_text(f"v{state['calls'] + 1}", encoding="utf-8")
            return state["calls"]

        result, info, ok = ctx.run_fingerprinted(lambda: [f], compute)
        assert ok is False
        assert result is None
        assert info["moved"] is True
        assert str(f) in info["movers"]

    def test_new_path_appearing_counts_as_moved(self, tmp_path: Path) -> None:
        # A path present only in the AFTER fingerprint (a file landing
        # mid-compute) counts as moved on both attempts → honest abstain.
        f = tmp_path / "input.txt"
        f.write_text("v1", encoding="utf-8")
        extra = tmp_path / "new.txt"
        extra.write_text("x", encoding="utf-8")
        ctx = _ctx(tmp_path)
        sequences = iter([[f], [f, extra], [f], [f, extra]])
        _result, info, ok = ctx.run_fingerprinted(lambda: next(sequences), lambda: 1)
        assert ok is False
        assert str(extra) in info["movers"]


class TestJournalProbeEdges:
    def test_journal_read_oserror_is_idle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.observability.pull_journal as pj

        def _boom(lake_root: Path) -> list[object]:
            raise OSError("wedged volume")

        monkeypatch.setattr(pj, "read_journal", _boom)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is False

    def test_naive_journal_stamp_treated_utc(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal" / "pull_journal.jsonl"
        journal.parent.mkdir(parents=True)
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        journal.write_text(
            '{"event": "started", "pull_id": "p", "wall_utc": "' + naive + '", '
            '"monotonic_s": 0.0, "dry_run": false}\n',
            encoding="utf-8",
        )
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is True
