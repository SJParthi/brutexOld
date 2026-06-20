"""Unit tests for :mod:`brutex.data.registry_store` (DEC-DERIV-METADATA-HISTORICAL-SPLIT-001).

The expiries+contracts REGISTRY cache: a SEPARATE ``registry.duckdb`` that stores the
actual expiry dates + contract symbols the lean instrument master discards. Every
path is exercised against a REAL temp DuckDB (the store is a thin, deterministic SQL
layer) — open/drop-recreate, idempotent upserts, validation, the static filtered
SELECTs, and the ``list_contract_jobs`` the dormant phase engine consumes. 100%
line+branch on ``brutex.data.registry_store``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
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
