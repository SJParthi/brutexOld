"""Unit tests for :mod:`brutex.data.registry_store` (DEC-DERIV-METADATA-HISTORICAL-SPLIT-001).

The expiries+contracts REGISTRY cache: a SEPARATE ``registry.duckdb`` that stores the
actual expiry dates + contract symbols the lean instrument master discards. Every
path is exercised against a REAL temp DuckDB (the store is a thin, deterministic SQL
layer) — open/drop-recreate, idempotent upserts, validation, the static filtered
SELECTs, and the ``list_contract_jobs`` the dormant phase engine consumes. 100%
line+branch on ``brutex.data.registry_store``.

Also home to the store-write deadline + wedge fail-fast suite
(DEC-STORE-WRITE-DEADLINE-001) — white-box (``store._conn`` proxy swap /
``store._write_guard`` probes), so private usage is silenced file-wide per the
DEC-P0-017 whitebox-test convention.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from brutex.data.errors import StoreWriteDeadlineExceeded
from brutex.data.registry_store import (
    CANDLE_STATUS_COMPLETE,
    CANDLE_STATUS_NONE,
    REGISTRY_SCHEMA_VERSION,
    ContractRow,
    ExpiryRow,
    RegistryError,
    RegistryStore,
    open_registry,
)
from brutex.data.store_write_deadline import STORE_CLOSE_SKIPPED_EVENT
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_EXP = date(2020, 1, 2)


def _store(tmp_path: Path) -> RegistryStore:
    return RegistryStore.open(tmp_path / "registry.duckdb")


def _seed_one(store: RegistryStore) -> None:
    store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r1", fetched_at=_NOW)
    store.upsert_contract(
        "NSE",
        "NIFTY",
        _EXP,
        "NSE-NIFTY-02Jan20-12100-CE",
        side="CE",
        strike=12100.0,
        run_id="r1",
        fetched_at=_NOW,
    )


# ── open / schema / lifecycle ────────────────────────────────────────────────


def test_open_creates_schema_and_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.path == tmp_path / "registry.duckdb"
    assert store.count_expiries() == 0
    assert store.count_contracts() == 0
    store.close()


def test_reopen_current_version_keeps_data(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_one(store)
    store.close()
    # Re-open: existing version == current → NO drop, data survives.
    reopened = _store(tmp_path)
    assert reopened.count_expiries() == 1
    assert reopened.count_contracts() == 1
    reopened.close()


def test_open_older_version_drops_and_recreates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_one(store)
    store.close()
    # Stamp an OLDER schema_version directly → next open must drop + recreate.
    conn = duckdb.connect(str(tmp_path / "registry.duckdb"))
    conn.execute("UPDATE schema_meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()
    reopened = _store(tmp_path)
    assert reopened.count_expiries() == 0  # dropped + recreated empty
    assert reopened.count_contracts() == 0
    # And the version is re-stamped to current.
    rows = reopened.list_expiries()
    assert rows == []
    reopened.close()
    assert REGISTRY_SCHEMA_VERSION == 1


def test_close_is_idempotent_and_blocks_ops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.close()
    store.close()  # second close is a no-op
    with pytest.raises(RegistryError, match="closed"):
        store.count_expiries()


def test_context_manager_closes(tmp_path: Path) -> None:
    with open_registry(tmp_path / "registry.duckdb") as store:
        _seed_one(store)
        assert store.count_expiries() == 1
    # After the with-block the store is closed.
    with pytest.raises(RegistryError, match="closed"):
        store.count_contracts()


def test_store_enter_returns_self(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store as same:
        assert same is store
    with pytest.raises(RegistryError, match="closed"):
        store.count_expiries()


# ── upsert + idempotence + validation ────────────────────────────────────────


def test_upsert_expiry_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r1", fetched_at=_NOW)
    store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r2", fetched_at=_NOW)  # same PK → replace
    assert store.count_expiries() == 1
    assert store.list_expiries()[0].last_run_id == "r2"  # replaced, not duplicated
    store.close()


def test_upsert_contract_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for run in ("r1", "r2"):
        store.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "NSE-NIFTY-02Jan20-12100-CE",
            side="CE",
            strike=12100.0,
            run_id=run,
            fetched_at=_NOW,
        )
    assert store.count_contracts() == 1
    store.close()


def test_upsert_contract_futures_null_strike(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_contract(
        "NSE",
        "NIFTY",
        _EXP,
        "NSE-NIFTY-02Jan20-FUT",
        side="FUT",
        strike=None,
        run_id="r",
        fetched_at=_NOW,
    )
    row = store.list_contracts()[0]
    assert row.side == "FUT"
    assert row.strike is None
    store.close()


def test_upsert_contract_rejects_bad_side(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RegistryError, match="side must be one of"):
        store.upsert_contract(
            "NSE", "NIFTY", _EXP, "x", side="ZZ", strike=1.0, run_id="r", fetched_at=_NOW
        )
    store.close()


def test_upsert_contract_rejects_bad_candle_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RegistryError, match="candle_status must be one of"):
        store.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "x",
            side="CE",
            strike=1.0,
            run_id="r",
            fetched_at=_NOW,
            candle_status="bogus",
        )
    store.close()


def test_upsert_rejects_naive_datetime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    naive = datetime(2020, 1, 1)  # intentionally naive for the timezone-aware guard
    with pytest.raises(RegistryError, match="timezone-aware"):
        store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=naive)
    with pytest.raises(RegistryError, match="timezone-aware"):
        store.upsert_contract(
            "NSE", "NIFTY", _EXP, "x", side="CE", strike=1.0, run_id="r", fetched_at=naive
        )
    store.close()


# ── set_candle_status ────────────────────────────────────────────────────────


def test_set_candle_status_updates_known_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_one(store)
    updated = store.set_candle_status(
        "NSE", "NIFTY", "NSE-NIFTY-02Jan20-12100-CE", CANDLE_STATUS_COMPLETE
    )
    assert updated == 1
    assert store.list_contracts()[0].candle_status == CANDLE_STATUS_COMPLETE
    store.close()


def test_set_candle_status_unknown_contract_is_zero(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_one(store)
    assert store.set_candle_status("NSE", "NIFTY", "NOPE", CANDLE_STATUS_COMPLETE) == 0
    store.close()


def test_set_candle_status_rejects_bad_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RegistryError, match="candle_status must be one of"):
        store.set_candle_status("NSE", "NIFTY", "x", "bogus")
    store.close()


# ── filtered reads (the static (? IS NULL OR col = ?) predicates) ─────────────


def _seed_grid(store: RegistryStore) -> None:
    store.upsert_expiry("NSE", "NIFTY", date(2020, 1, 2), run_id="r", fetched_at=_NOW)
    store.upsert_expiry("NSE", "BANKNIFTY", date(2020, 1, 30), run_id="r", fetched_at=_NOW)
    store.upsert_expiry("BSE", "SENSEX", date(2020, 1, 9), run_id="r", fetched_at=_NOW)
    store.upsert_contract(
        "NSE",
        "NIFTY",
        date(2020, 1, 2),
        "NSE-NIFTY-02Jan20-12100-CE",
        side="CE",
        strike=12100.0,
        run_id="r",
        fetched_at=_NOW,
    )
    store.upsert_contract(
        "NSE",
        "NIFTY",
        date(2020, 1, 2),
        "NSE-NIFTY-02Jan20-12100-PE",
        side="PE",
        strike=12100.0,
        run_id="r",
        fetched_at=_NOW,
    )
    store.upsert_contract(
        "NSE",
        "BANKNIFTY",
        date(2020, 1, 30),
        "NSE-BANKNIFTY-30Jan20-32000-CE",
        side="CE",
        strike=32000.0,
        run_id="r",
        fetched_at=_NOW,
    )


def test_list_expiries_all_and_filtered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_grid(store)
    assert len(store.list_expiries()) == 3  # all (both filters None)
    assert [e.underlying for e in store.list_expiries(exch="NSE")] == ["BANKNIFTY", "NIFTY"]
    assert [e.underlying for e in store.list_expiries(underlying="SENSEX")] == ["SENSEX"]
    nifty = store.list_expiries(exch="NSE", underlying="NIFTY")
    assert len(nifty) == 1 and isinstance(nifty[0], ExpiryRow)
    assert nifty[0].year == 2020  # the ExpiryRow.year property
    store.close()


def test_list_contracts_all_and_filtered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_grid(store)
    assert len(store.list_contracts()) == 3  # all three filters None
    assert len(store.list_contracts(exch="NSE")) == 3
    assert len(store.list_contracts(underlying="NIFTY")) == 2
    by_expiry = store.list_contracts(exch="NSE", underlying="NIFTY", expiry_date=date(2020, 1, 2))
    assert {c.side for c in by_expiry} == {"CE", "PE"}
    assert isinstance(by_expiry[0], ContractRow)
    assert store.list_contracts(expiry_date=date(2099, 1, 1)) == []  # no match
    store.close()


def test_list_contract_jobs_is_distinct_sorted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_grid(store)  # NIFTY has 2 contracts on the same expiry → one job
    jobs = store.list_contract_jobs()
    assert jobs == [
        ("NSE", "BANKNIFTY", date(2020, 1, 30)),
        ("NSE", "NIFTY", date(2020, 1, 2)),
    ]
    store.close()


def test_default_candle_status_is_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_one(store)
    assert store.list_contracts()[0].candle_status == CANDLE_STATUS_NONE
    store.close()


# ── pagination + filtered counts (DEC-DB-VIEWER-PAGINATE-ALL-001) ─────────────


def _seed_many(store: RegistryStore, n_exp: int = 6, legs_per: int = 5) -> None:
    """Seed *n_exp* NIFTY expiries, each with *legs_per* contract legs, deterministically."""
    for i in range(n_exp):
        exp = date(2020, 1, 1 + i)
        store.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=_NOW)
        for k in range(legs_per):
            store.upsert_contract(
                "NSE",
                "NIFTY",
                exp,
                f"NSE-NIFTY-{i:02d}-{k:02d}-CE",
                side="CE",
                strike=12000.0 + k,
                run_id="r1",
                fetched_at=_NOW,
            )


def test_count_expiries_filtered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_expiry("NSE", "NIFTY", date(2020, 1, 2), run_id="r1", fetched_at=_NOW)
    store.upsert_expiry("NSE", "BANKNIFTY", date(2020, 1, 9), run_id="r1", fetched_at=_NOW)
    assert store.count_expiries() == 2  # unfiltered = whole-table total (nav badge)
    assert store.count_expiries(underlying="NIFTY") == 1  # FILTERED
    assert store.count_expiries(exch="NSE", underlying="BANKNIFTY") == 1
    assert store.count_expiries(underlying="ABSENT") == 0
    store.close()


def test_count_contracts_filtered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_many(store, n_exp=3, legs_per=4)  # 12 legs across 3 expiries
    assert store.count_contracts() == 12
    assert store.count_contracts(expiry_date=date(2020, 1, 1)) == 4
    assert store.count_contracts(underlying="NIFTY", expiry_date=date(2020, 1, 2)) == 4
    assert store.count_contracts(underlying="ABSENT") == 0
    store.close()


def test_list_expiries_limit_offset_pages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_many(store, n_exp=6, legs_per=1)
    # limit=None keeps the legacy "all rows" behaviour.
    assert len(store.list_expiries()) == 6
    page1 = store.list_expiries(limit=2, offset=0)
    page2 = store.list_expiries(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    # ORDER BY exch, underlying, expiry_date → contiguous, non-overlapping pages.
    assert [r.expiry_date for r in page1] == [date(2020, 1, 1), date(2020, 1, 2)]
    assert [r.expiry_date for r in page2] == [date(2020, 1, 3), date(2020, 1, 4)]
    # Offset past the end → empty page (never raises).
    assert store.list_expiries(limit=2, offset=999) == []
    store.close()


def test_list_contracts_limit_offset_pages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_many(store, n_exp=2, legs_per=5)  # 10 legs
    assert len(store.list_contracts()) == 10  # limit=None → all
    page = store.list_contracts(limit=3, offset=3)
    assert len(page) == 3
    # A last partial page returns only the remainder.
    last = store.list_contracts(limit=4, offset=8)
    assert len(last) == 2
    store.close()


# ── store-write deadline + wedge fail-fast (DEC-STORE-WRITE-DEADLINE-001) ────
#
# The metadata twin of the #999 lake-sink freeze guard: every registry DuckDB
# write batch is bounded by a wall-clock deadline; a wedged volume raises the
# TRANSIENT StoreWriteDeadlineExceeded (the caller-held RLock is released on the
# OWNING thread), later writes fail fast on the wedge latch, reads/close/open
# fail loudly instead of freezing, and the store self-heals when the zombie
# completes. The hang is DETERMINISTIC (a proxy conn blocking on a gate with a
# 30 s safety, released in ``finally``) — never a real wedge risk.


class _GateConn:
    """Proxy over the real DuckDB conn that HANGS on statements matching a marker.

    The white-box ``store._conn`` swap follows the sibling
    ``test_registry_store_threads.py`` probe convention; ``entered`` proves the
    hang genuinely started, ``gate`` is released by the test's ``finally`` (30 s
    safety so a crashed test never leaves a forever-daemon), and ``close_calls``
    pins the wedged-close skip.
    """

    def __init__(self, real: object, *, hang_on: str) -> None:
        self._real = real
        self._hang_on = hang_on
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.close_calls = 0

    def execute(self, sql: str, params: list[object] | None = None) -> object:
        if self._hang_on in sql:
            self.entered.set()
            self.gate.wait(timeout=30.0)
        real: Any = self._real
        if params is None:
            return real.execute(sql)
        return real.execute(sql, params)

    def close(self) -> None:
        self.close_calls += 1
        real: Any = self._real
        real.close()


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll *predicate* until true or fail loudly (never hangs the suite)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:  # pragma: no cover - only on a real test hang
            pytest.fail("condition not reached within the watchdog bound")
        time.sleep(0.005)


def _wedge(store: RegistryStore, monkeypatch: pytest.MonkeyPatch, *, hang_on: str) -> _GateConn:
    """Swap the store's conn for a gate proxy + shrink the deadline to 0.2 s."""
    monkeypatch.setenv("BRUTEX_REGISTRY_WRITE_DEADLINE_S", "0.2")
    proxy = _GateConn(store._conn, hang_on=hang_on)
    store._conn = proxy
    return proxy


_EXPIRY_WRITE = "INSERT OR REPLACE INTO expiries"
_CONTRACT_WRITE = "INSERT OR REPLACE INTO contracts"
_STATUS_WRITE = "UPDATE contracts SET candle_status"


class TestStoreWriteDeadline:
    def test_hung_upsert_expiry_defers_within_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        start = time.monotonic()
        try:
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=_NOW)
            assert proxy.entered.is_set()
            assert time.monotonic() - start < 5.0
            assert excinfo.value.store == "registry"
            assert excinfo.value.op == "upsert_expiries"
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_hung_upsert_contracts_batch_defers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_CONTRACT_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                store.upsert_contracts(
                    "NSE",
                    "NIFTY",
                    _EXP,
                    [("NSE-NIFTY-02Jan20-12100-CE", "CE", 12100.0), ("X-FUT", "FUT", None)],
                    run_id="r",
                    fetched_at=_NOW,
                )
            assert excinfo.value.op == "upsert_contracts"
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_hung_set_candle_status_defers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _seed_one(store)
        proxy = _wedge(store, monkeypatch, hang_on=_STATUS_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                store.set_candle_status(
                    "NSE", "NIFTY", "NSE-NIFTY-02Jan20-12100-CE", CANDLE_STATUS_COMPLETE
                )
            assert excinfo.value.op == "set_candle_status"
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_hung_mark_candle_statuses_defers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        _seed_one(store)
        proxy = _wedge(store, monkeypatch, hang_on=_STATUS_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                store.mark_candle_statuses(
                    [("NSE", "NIFTY", "NSE-NIFTY-02Jan20-12100-CE")], CANDLE_STATUS_COMPLETE
                )
            assert excinfo.value.op == "mark_candle_statuses"
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_breach_releases_the_store_lock_on_the_owning_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE lock-safety regression: after a breach ``store._lock`` is FREE.

        An implementation that acquired the RLock on the guard's daemon thread
        would orphan it (RLock ownership is per-thread) and this acquire would
        time out — freezing every sibling worker forever, the exact #999-class
        failure this decision closes.
        """
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded):
                store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=_NOW)
            acquired: list[bool] = []

            def _try_acquire() -> None:
                got = store._lock.acquire(timeout=2.0)
                acquired.append(got)
                if got:
                    store._lock.release()

            t = threading.Thread(target=_try_acquire)
            t.start()
            t.join(timeout=5.0)
            assert acquired == [True]
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_second_write_fails_fast_on_the_latch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded):
                store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=_NOW)
            start = time.monotonic()
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                store.upsert_expiry("NSE", "BANKNIFTY", _EXP, run_id="r", fetched_at=_NOW)
            assert time.monotonic() - start < 0.1  # instant — no deadline stacking
            assert excinfo.value.latched is True
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_wedged_reads_fail_fast_and_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded):
                store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=_NOW)
            with pytest.raises(RegistryError, match="wedged"):
                store.count_expiries()
            with pytest.raises(RegistryError, match="wedged"):
                store.count_contracts()
            with pytest.raises(RegistryError, match="wedged"):
                store.list_expiries()
            with pytest.raises(RegistryError, match="wedged"):
                store.list_contracts()
            with pytest.raises(RegistryError, match="wedged"):
                store.list_contract_jobs()
        finally:
            proxy.gate.set()
        _wait_until(lambda: not store._write_guard.wedged)
        store.close()

    def test_wedged_close_skips_the_raw_close_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        try:
            with pytest.raises(StoreWriteDeadlineExceeded):
                store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r", fetched_at=_NOW)
            with capture_logs() as logs:
                store.close()  # the ``with`` exit path — must NEVER block
            assert proxy.close_calls == 0  # raw close skipped under the zombie
            skipped = [r for r in logs if r["event"] == STORE_CLOSE_SKIPPED_EVENT]
            assert len(skipped) == 1
            assert skipped[0]["store"] == "registry"
            store.close()  # idempotent second close
            with pytest.raises(RegistryError, match="closed"):
                store.count_expiries()
        finally:
            proxy.gate.set()

    def test_recovery_after_the_volume_heals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Release the gate → the zombie's idempotent write lands → store resumes."""
        store = _store(tmp_path)
        proxy = _wedge(store, monkeypatch, hang_on=_EXPIRY_WRITE)
        with pytest.raises(StoreWriteDeadlineExceeded):
            store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r1", fetched_at=_NOW)
        proxy.gate.set()  # the volume recovers
        _wait_until(lambda: not store._write_guard.wedged)
        # The zombie's late keyed INSERT OR REPLACE landed harmlessly; the retry
        # is idempotent on the PK — exactly one row, reads work again.
        store.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r2", fetched_at=_NOW)
        assert store.count_expiries() == 1
        assert store.list_expiries()[0].last_run_id == "r2"
        store.close()

    def test_hung_open_fails_loudly_within_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh open on a still-wedged volume → terminal RegistryError, no freeze."""
        monkeypatch.setenv("BRUTEX_REGISTRY_WRITE_DEADLINE_S", "0.2")
        gate = threading.Event()
        entered = threading.Event()

        def _hanging_connect(_path: str) -> object:
            entered.set()
            gate.wait(timeout=30.0)
            raise RuntimeError("released after the deadline already fired")

        monkeypatch.setattr(duckdb, "connect", _hanging_connect)
        start = time.monotonic()
        try:
            with pytest.raises(RegistryError, match="failed to open RegistryStore"):
                RegistryStore.open(tmp_path / "registry.duckdb")
            assert entered.is_set()
            assert time.monotonic() - start < 5.0
        finally:
            gate.set()


class TestBatchWriteMethods:
    def test_batch_vs_per_row_byte_equality(self, tmp_path: Path) -> None:
        """The A/B oracle: batch and per-row writes land IDENTICAL rows."""
        per_row = RegistryStore.open(tmp_path / "per_row.duckdb")
        batch = RegistryStore.open(tmp_path / "batch.duckdb")
        expiries = [date(2020, 1, 2), date(2020, 1, 9)]
        legs: list[tuple[str, str, float | None]] = [
            ("NSE-NIFTY-02Jan20-12100-CE", "CE", 12100.0),
            ("NSE-NIFTY-02Jan20-FUT", "FUT", None),
        ]
        for expiry in expiries:
            per_row.upsert_expiry("NSE", "NIFTY", expiry, run_id="r", fetched_at=_NOW)
        for symbol, side, strike in legs:
            per_row.upsert_contract(
                "NSE", "NIFTY", _EXP, symbol, side=side, strike=strike, run_id="r", fetched_at=_NOW
            )
        assert batch.upsert_expiries("NSE", "NIFTY", expiries, run_id="r", fetched_at=_NOW) == 2
        assert batch.upsert_contracts("NSE", "NIFTY", _EXP, legs, run_id="r", fetched_at=_NOW) == 2
        assert per_row.list_expiries() == batch.list_expiries()
        assert per_row.list_contracts() == batch.list_contracts()
        per_row.close()
        batch.close()

    def test_upsert_expiries_empty_batch_is_a_no_op(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.upsert_expiries("NSE", "NIFTY", [], run_id="r", fetched_at=_NOW) == 0
        assert store.count_expiries() == 0
        store.close()

    def test_upsert_contracts_invalid_mid_list_leg_persists_nothing(self, tmp_path: Path) -> None:
        """Upfront validation: a bad mid-list leg writes NO prefix (all-or-nothing)."""
        store = _store(tmp_path)
        with pytest.raises(RegistryError, match="side must be one of"):
            store.upsert_contracts(
                "NSE",
                "NIFTY",
                _EXP,
                [("ok-CE", "CE", 1.0), ("bad", "ZZ", 1.0)],
                run_id="r",
                fetched_at=_NOW,
            )
        assert store.count_contracts() == 0
        store.close()

    def test_upsert_contracts_rejects_bad_status_and_naive_datetime(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(RegistryError, match="candle_status must be one of"):
            store.upsert_contracts(
                "NSE",
                "NIFTY",
                _EXP,
                [("x", "CE", 1.0)],
                run_id="r",
                fetched_at=_NOW,
                candle_status="bogus",
            )
        naive = datetime(2020, 1, 1)  # intentionally naive for the tz-aware guard
        with pytest.raises(RegistryError, match="timezone-aware"):
            store.upsert_contracts(
                "NSE", "NIFTY", _EXP, [("x", "CE", 1.0)], run_id="r", fetched_at=naive
            )
        with pytest.raises(RegistryError, match="timezone-aware"):
            store.upsert_expiries("NSE", "NIFTY", [_EXP], run_id="r", fetched_at=naive)
        assert store.count_contracts() == 0
        store.close()

    def test_mark_candle_statuses_updates_the_batch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_grid(store)
        rows = [
            ("NSE", "NIFTY", "NSE-NIFTY-02Jan20-12100-CE"),
            ("NSE", "NIFTY", "NSE-NIFTY-02Jan20-12100-PE"),
            ("NSE", "NIFTY", "UNKNOWN"),  # honest 0-row no-op, never invented
        ]
        assert store.mark_candle_statuses(rows, CANDLE_STATUS_COMPLETE) == 3
        statuses = {c.contract_symbol: c.candle_status for c in store.list_contracts()}
        assert statuses["NSE-NIFTY-02Jan20-12100-CE"] == CANDLE_STATUS_COMPLETE
        assert statuses["NSE-NIFTY-02Jan20-12100-PE"] == CANDLE_STATUS_COMPLETE
        assert statuses["NSE-BANKNIFTY-30Jan20-32000-CE"] == CANDLE_STATUS_NONE
        assert "UNKNOWN" not in statuses
        store.close()

    def test_mark_candle_statuses_rejects_bad_status(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(RegistryError, match="candle_status must be one of"):
            store.mark_candle_statuses([("NSE", "NIFTY", "x")], "bogus")
        store.close()
