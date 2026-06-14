"""Unit tests for the F&O registry browsing helpers in :mod:`brutex.dashboard.db_view`.

DEC-DERIV-REGISTRY-DB-VIEW-001 (PR-4 of the metadata/historical split): surface
the SEPARATE ``registry.duckdb`` cache (PR-1, DEC-DERIV-METADATA-HISTORICAL-SPLIT-001)
on the ``/db`` page, structured by expiry / strike / CE-PE. Every path is exercised
against a REAL temp ``registry.duckdb`` seeded via
:class:`~brutex.data.registry_store.RegistryStore` (the store is a thin,
deterministic SQL layer), plus the honest missing-registry degrade path (no file →
empty grid + note, never an invented row — Golden Rule #1) and the malformed-date
reject. 100% line+branch on the new ``db_view`` registry functions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.dashboard.db_view import (
    REGISTRY_CONTRACT_COLUMNS,
    REGISTRY_DB_NAME,
    REGISTRY_EXPIRY_COLUMNS,
    list_registry_contracts,
    list_registry_expiries,
    registry_counts,
    registry_path_for,
)
from brutex.data.registry_store import RegistryStore

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_EXP_A = date(2020, 1, 30)
_EXP_B = date(2020, 2, 27)


def _seed_registry(path: Path) -> None:
    """Seed a real ``registry.duckdb`` with two expiries + three contract legs.

    NIFTY has two expiries (a CE+PE pair on the first, one FUT on the second);
    BANKNIFTY has a single expiry + CE, so the filters have something to exclude.
    """
    store = RegistryStore.open(path)
    try:
        store.upsert_expiry("NSE", "NIFTY", _EXP_A, run_id="r1", fetched_at=_NOW)
        store.upsert_expiry("NSE", "NIFTY", _EXP_B, run_id="r1", fetched_at=_NOW)
        store.upsert_expiry("NSE", "BANKNIFTY", _EXP_A, run_id="r1", fetched_at=_NOW)
        store.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP_A,
            "NSE-NIFTY-30Jan20-12100-CE",
            side="CE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        store.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP_A,
            "NSE-NIFTY-30Jan20-12100-PE",
            side="PE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        store.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP_B,
            "NSE-NIFTY-27Feb20-FUT",
            side="FUT",
            strike=None,
            run_id="r1",
            fetched_at=_NOW,
        )
        store.upsert_contract(
            "NSE",
            "BANKNIFTY",
            _EXP_A,
            "NSE-BANKNIFTY-30Jan20-32000-CE",
            side="CE",
            strike=32000.0,
            run_id="r1",
            fetched_at=_NOW,
        )
    finally:
        store.close()


# ── path resolution ──────────────────────────────────────────────────────────


def test_registry_path_for_is_master_sibling(tmp_path: Path) -> None:
    master = tmp_path / "lake" / "instruments.duckdb"
    assert registry_path_for(master) == tmp_path / "lake" / REGISTRY_DB_NAME


# ── counts ───────────────────────────────────────────────────────────────────


def test_registry_counts_missing_file_degrades_honestly(tmp_path: Path) -> None:
    counts = registry_counts(tmp_path / REGISTRY_DB_NAME)
    assert counts == {
        "expiries": 0,
        "contracts": 0,
        "exists": False,
        "note": "no registry yet — pull expiries + contracts first",
    }


def test_registry_counts_missing_file_does_not_create_it(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    registry_counts(path)
    # A pure count of an absent cache must NEVER side-effect a new empty file.
    assert not path.exists()


def test_registry_counts_reads_seeded_totals(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    counts = registry_counts(path)
    assert counts == {"expiries": 3, "contracts": 4, "exists": True, "note": None}


# ── expiries grid ────────────────────────────────────────────────────────────


def test_list_registry_expiries_missing_file(tmp_path: Path) -> None:
    grid = list_registry_expiries(tmp_path / REGISTRY_DB_NAME)
    assert grid == {
        "columns": list(REGISTRY_EXPIRY_COLUMNS),
        "rows": [],
        "total": 0,
        "note": "no registry yet — pull expiries + contracts first",
    }


def test_list_registry_expiries_all(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    grid = list_registry_expiries(path)
    assert grid["columns"] == list(REGISTRY_EXPIRY_COLUMNS)
    assert grid["total"] == 3
    assert grid["note"] is None
    # Ordered exch, underlying, expiry_date → BANKNIFTY first, then NIFTY×2.
    assert grid["rows"][0] == ["NSE", "BANKNIFTY", "2020-01-30", 2020, "2026-06-14 14:45:00"]
    assert [r[1] for r in grid["rows"]] == ["BANKNIFTY", "NIFTY", "NIFTY"]
    # expiry_date rendered ISO; year derived.
    assert grid["rows"][2] == ["NSE", "NIFTY", "2020-02-27", 2020, "2026-06-14 14:45:00"]


def test_list_registry_expiries_filtered_by_underlying(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    grid = list_registry_expiries(path, exch="NSE", underlying="BANKNIFTY")
    assert grid["total"] == 1
    assert grid["rows"][0][1] == "BANKNIFTY"


# ── contracts grid ───────────────────────────────────────────────────────────


def test_list_registry_contracts_missing_file(tmp_path: Path) -> None:
    grid = list_registry_contracts(tmp_path / REGISTRY_DB_NAME)
    assert grid == {
        "columns": list(REGISTRY_CONTRACT_COLUMNS),
        "rows": [],
        "total": 0,
        "note": "no registry yet — pull expiries + contracts first",
    }


def test_list_registry_contracts_all(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    grid = list_registry_contracts(path)
    assert grid["columns"] == list(REGISTRY_CONTRACT_COLUMNS)
    assert grid["total"] == 4
    assert grid["note"] is None
    # The FUT leg keeps a None strike (not money, never invented).
    fut = next(r for r in grid["rows"] if r[3] == "NSE-NIFTY-27Feb20-FUT")
    assert fut == [
        "NSE",
        "NIFTY",
        "2020-02-27",
        "NSE-NIFTY-27Feb20-FUT",
        None,
        "FUT",
        "none",
        "2026-06-14 14:45:00",
    ]
    # A CE leg carries its strike + side + candle_status.
    ce = next(r for r in grid["rows"] if r[3] == "NSE-NIFTY-30Jan20-12100-CE")
    assert ce[4] == 12100.0
    assert ce[5] == "CE"
    assert ce[6] == "none"


def test_list_registry_contracts_filtered_by_expiry(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    grid = list_registry_contracts(path, underlying="NIFTY", expiry_date="2020-01-30")
    assert grid["total"] == 2
    assert {r[5] for r in grid["rows"]} == {"CE", "PE"}


def test_list_registry_contracts_expiry_filter_no_match(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    grid = list_registry_contracts(path, expiry_date="2099-12-31")
    assert grid["total"] == 0
    assert grid["rows"] == []
    assert grid["note"] is None


def test_list_registry_contracts_rejects_malformed_expiry_on_missing_file(tmp_path: Path) -> None:
    # The date is parsed BEFORE the file check, so a bad bound is rejected even
    # when the cache does not exist yet (no silent empty page).
    with pytest.raises(ValueError, match="expiry_date must be ISO"):
        list_registry_contracts(tmp_path / REGISTRY_DB_NAME, expiry_date="2020/01/30")


def test_list_registry_contracts_rejects_malformed_expiry_on_seeded_file(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    with pytest.raises(ValueError, match="expiry_date must be ISO"):
        list_registry_contracts(path, expiry_date="30-01-2020")
