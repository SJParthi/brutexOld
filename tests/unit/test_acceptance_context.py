"""Context pins: pull-activity probe arms, TOCTOU retry-once law, shared loads.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from brutex.acceptance.context import (
    PULL_ACTIVE_STALE_S,
    AcceptanceContext,
    fingerprint_paths,
    stat_token,
)

NOW = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)  # 11:30 IST


def _ctx(tmp_path: Path, **kwargs: Any) -> AcceptanceContext:
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
        ctx.close()

    def test_default_factory_opens_store_when_present(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        from brutex.data.instrument_store import InstrumentStore

        InstrumentStore.open(tmp_path / "instruments.duckdb").close()
        ctx = _ctx(tmp_path)
        assert ctx.client().get("/api/v1/state").status_code == 200
        ctx.close()

    def test_env_override_scoped_and_restored(self, tmp_path: Path) -> None:
        # MN1 regression: the BRUTEX_LAKE_ROOT override is run-scoped — an
        # in-process caller (the F1 nightly chain) never inherits it.
        pytest.importorskip("fastapi")
        os.environ["BRUTEX_LAKE_ROOT"] = "/pre/existing"
        try:
            ctx = _ctx(tmp_path)
            ctx.client()
            assert os.environ["BRUTEX_LAKE_ROOT"] == str(tmp_path)
            ctx.close()
            assert os.environ["BRUTEX_LAKE_ROOT"] == "/pre/existing"
        finally:
            os.environ.pop("BRUTEX_LAKE_ROOT", None)

    def test_env_override_removed_when_previously_unset(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        os.environ.pop("BRUTEX_LAKE_ROOT", None)
        ctx = _ctx(tmp_path)
        ctx.client()
        assert os.environ.get("BRUTEX_LAKE_ROOT") == str(tmp_path)
        ctx.close()
        assert "BRUTEX_LAKE_ROOT" not in os.environ
        ctx.close()  # idempotent

    def test_close_swallows_teardown_errors(self, tmp_path: Path) -> None:
        class _Boom:
            def close(self) -> None:
                raise RuntimeError("teardown boom")

        ctx = _ctx(tmp_path, client_factory=_Boom)
        ctx.client()
        ctx._store = _Boom()
        ctx.close()  # never raises — teardown is best-effort


class TestReadOnlyStore:
    def test_older_schema_store_survives_byte_identical(self, tmp_path: Path) -> None:
        # Round 1 M1 regression: the acceptance path must NEVER run the
        # migration DDL — an older-schema store's bytes are untouched and the
        # verdict path abstains instead of DROPping tables.
        import hashlib

        from brutex.acceptance.context import open_instrument_store_readonly
        from brutex.data.instrument_store import InstrumentStore

        path = tmp_path / "instruments.duckdb"
        store = InstrumentStore.open(path)
        store._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        store.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        ro = open_instrument_store_readonly(path)
        version = ro._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        ro.close()
        assert version is not None and version[0] == "1"  # never re-stamped
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    def test_rw_holder_raises_store_unavailable(self, tmp_path: Path) -> None:
        import duckdb
        from brutex.acceptance.context import StoreUnavailable, open_instrument_store_readonly
        from brutex.data.instrument_store import InstrumentStore

        path = tmp_path / "instruments.duckdb"
        InstrumentStore.open(path).close()
        holder = duckdb.connect(str(path))  # a live RW holder (pull/dashboard)
        try:
            with pytest.raises(StoreUnavailable):
                open_instrument_store_readonly(path)
        finally:
            holder.close()

    def test_snapshot_store_leaves_drift_ladder_live(self, tmp_path: Path) -> None:
        # Round-2 fix 6 (refuter 2A MINOR): with the acceptance client BUILT
        # (store snapshot open), the production spot-drift ladder
        # (#1482/#1646) opens its OWN connection to instruments.duckdb
        # in-process — the previously-HELD read-only file handle hit DuckDB's
        # same-file "different configuration" refusal (master_error → ladder
        # silently degraded → leg A was not the production wire). The
        # snapshot holds NO file handle, so the ladder must work.
        pytest.importorskip("fastapi")
        from brutex.data.instrument_store import InstrumentStore
        from brutex.data.spot_drift import load_stock_drift_map

        InstrumentStore.open(tmp_path / "instruments.duckdb").close()
        ctx = _ctx(tmp_path)
        try:
            ctx.client()  # builds the app over the SNAPSHOT store
            assert load_stock_drift_map(tmp_path) is not None
        finally:
            ctx.close()

    def test_per_call_read_store_isolates_lock_and_delegates(self, tmp_path: Path) -> None:
        # Round-5 F1: the app-facing proxy hands each `connection` a PRIVATE
        # cursor + PRIVATE lock, so an unbalanced describe (a served route's
        # `.execute(...).description` with no fetch) leaks only that request's
        # lock and can never deadlock a later request on another worker thread.
        from brutex.acceptance.context import _PerCallReadStore, open_instrument_store_readonly
        from brutex.data.instrument_store import InstrumentStore

        path = tmp_path / "instruments.duckdb"
        InstrumentStore.open(path).close()
        snapshot = open_instrument_store_readonly(path)
        proxy = _PerCallReadStore(snapshot)
        try:
            v1 = proxy.connection
            v2 = proxy.connection
            assert v1 is not v2  # distinct views
            assert v1._lock is not v2._lock  # PRIVATE per-call locks (isolation)
            # An UNBALANCED describe on v1 (acquires, no fetch) must NOT block v2.
            v1.execute("SELECT 1")  # holds v1's private lock (no fetch → leaked)
            assert v2.execute("SELECT 1").fetchone() == (1,)  # v2 unaffected
            # Non-connection attributes delegate to the real snapshot.
            assert proxy.list_index_equity_instruments() == []
        finally:
            proxy.close()

    def test_client_factory_degrades_to_no_store_on_contention(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        import duckdb
        from brutex.data.instrument_store import InstrumentStore

        path = tmp_path / "instruments.duckdb"
        InstrumentStore.open(path).close()
        holder = duckdb.connect(str(path))
        ctx = _ctx(tmp_path)
        try:
            client = ctx.client()  # no crash — store=None + scoped env
            assert client.get("/api/v1/state").status_code == 200
            assert ctx._store is None
        finally:
            holder.close()
            ctx.close()


class TestEnumerationMemos:
    def test_spot_datasets_and_contracts_memoised(self, tmp_path: Path) -> None:
        bars = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        bars.mkdir(parents=True)
        fno = tmp_path / "bars" / "NSE" / "FNO" / "NSE-NIFTY-30Jul26-FUT"
        fno.mkdir(parents=True)
        ctx = _ctx(tmp_path)
        first = ctx.spot_datasets()
        contracts = ctx.fno_contracts()
        assert first == [("NSE", "INDEX", "NIFTY")]
        assert contracts == ["NSE-NIFTY-30Jul26-FUT"]
        # A dir added after the memo is NOT re-walked (one enumeration per run).
        (tmp_path / "bars" / "NSE" / "INDEX" / "LATER").mkdir()
        assert ctx.spot_datasets() is first
        assert ctx.fno_contracts() is contracts


class TestPullActiveProbe:
    def test_idle_lake(self, tmp_path: Path) -> None:
        assert _ctx(tmp_path).pull_active() is False

    def test_probe_memoised(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.pull_active() is False
        # flip the underlying state; the memo must hold for the run
        import brutex.observability.pull_journal as pj

        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        assert ctx.pull_active() is False

    def test_live_journal_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.observability.pull_journal as pj

        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is True

    def test_finished_journal_run_idle(self, tmp_path: Path) -> None:
        import brutex.observability.pull_journal as pj

        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        pj.record_pull_finish(tmp_path, pull_id="p1", verdict="ok")
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: datetime.now(UTC))
        assert ctx.pull_active() is False

    def test_stale_unfinished_run_idle(self, tmp_path: Path) -> None:
        import brutex.observability.pull_journal as pj

        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        later = datetime.now(UTC) + timedelta(seconds=PULL_ACTIVE_STALE_S + 60)
        ctx = AcceptanceContext(lake_root=tmp_path, seed="s", now_utc=lambda: later)
        assert ctx.pull_active() is False

    def test_heartbeat_extends_liveness(self, tmp_path: Path) -> None:
        import brutex.observability.pull_journal as pj

        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False)
        pj.record_pull_heartbeat(tmp_path, pull_id="p1")
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

    def test_on_retry_fires_once_before_second_attempt_only(self, tmp_path: Path) -> None:
        # Round-2 fix 4: the hook drops memoised state BEFORE the retry —
        # never on a clean run, exactly once on a moved run.
        f = tmp_path / "input.txt"
        f.write_text("v1", encoding="utf-8")
        ctx = _ctx(tmp_path)
        fired: list[int] = []
        _result, _info, ok = ctx.run_fingerprinted(
            lambda: [f], lambda: 1, on_retry=lambda: fired.append(-1)
        )
        assert ok is True and fired == []  # clean run — never fired
        state = {"calls": 0}

        def compute() -> int:
            state["calls"] += 1
            if state["calls"] == 1:
                f.write_text("v2", encoding="utf-8")
            return state["calls"]

        fired.clear()
        result, _info, ok = ctx.run_fingerprinted(
            lambda: [f], compute, on_retry=lambda: fired.append(state["calls"])
        )
        assert ok is True and result == 2
        assert fired == [1]  # once, after attempt 0, before attempt 1

    def test_drop_ledger_memo_forces_reread(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        save_window_ledger(WindowLedger.from_rows([]), tmp_path)
        ctx = _ctx(tmp_path)
        assert ctx.ledger() is not None
        assert ctx.ledger_loads == 1
        ctx.ledger()
        assert ctx.ledger_loads == 1  # memoised
        ctx.drop_ledger_memo()
        assert ctx.ledger() is not None
        assert ctx.ledger_loads == 2  # a genuine disk re-read


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
