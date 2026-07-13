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

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

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
        "limit": 500,
        "offset": 0,
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
        "limit": 500,
        "offset": 0,
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


# ── pagination (DEC-DB-VIEWER-PAGINATE-ALL-001) ──────────────────────────────


def test_list_registry_expiries_paged_total_is_filtered_not_page_len(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)  # 3 expiries total
    page = list_registry_expiries(path, limit=2, offset=0)
    assert len(page["rows"]) == 2  # one PAGE
    assert page["total"] == 3  # but the FILTERED total is the full count
    assert page["limit"] == 2 and page["offset"] == 0
    # Page 2 is the contiguous remainder.
    page2 = list_registry_expiries(path, limit=2, offset=2)
    assert len(page2["rows"]) == 1
    assert page2["total"] == 3 and page2["offset"] == 2


def test_list_registry_expiries_paged_total_respects_filter(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    page = list_registry_expiries(path, underlying="NIFTY", limit=1, offset=0)
    assert page["total"] == 2  # NIFTY has 2 expiries (filtered count, not 3)
    assert len(page["rows"]) == 1


def test_list_registry_contracts_paged_total_is_filtered(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)  # 4 contract legs total
    page = list_registry_contracts(path, limit=2, offset=0)
    assert len(page["rows"]) == 2
    assert page["total"] == 4
    assert page["limit"] == 2 and page["offset"] == 0
    # Filtered by underlying → filtered total.
    nifty = list_registry_contracts(path, underlying="NIFTY", limit=10, offset=0)
    assert nifty["total"] == 3  # NIFTY has 3 legs (CE+PE+FUT), BANKNIFTY excluded


# ── H1 count cache (DEC-O1-GUARANTEE-HARDEN-001) ────────────────────────────


_T = TypeVar("_T")


class _CountingCache:
    """A minimal :class:`CandleCountCache` recording computes per (key, generation)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, int], Any] = {}
        self.computes: list[str] = []

    def get_or_compute(self, key: str, *, generation: int, compute: Callable[[], _T]) -> _T:
        slot = (key, generation)
        if slot not in self.store:
            self.computes.append(key)
            self.store[slot] = compute()
        return cast("_T", self.store[slot])


def test_registry_count_key_kind_and_filters_are_unambiguous() -> None:
    """`repr`-folded filters — a literal ``|`` inside a filter never collides keys."""
    from brutex.dashboard.db_view import registry_count_key

    hostile = registry_count_key("exp", exch="A|B")
    split = registry_count_key("exp", exch="A", underlying="B")
    assert hostile != split
    assert registry_count_key("exp") != registry_count_key("con")
    assert registry_count_key("con", expiry_date="2020-01-30") != registry_count_key(
        "con", expiry_date="2020-02-27"
    )


def test_list_registry_expiries_count_cached_once_per_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeat/paging clicks within one generation pay ONE filtered COUNT."""
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    counts = [0]
    real = RegistryStore.count_expiries

    def _spy(self: RegistryStore, **kwargs: object) -> int:
        counts[0] += 1
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RegistryStore, "count_expiries", _spy)
    cache = _CountingCache()
    pages = [
        list_registry_expiries(path, limit=1, offset=n, count_cache=cache, generation=7)
        for n in range(3)
    ]
    assert counts[0] == 1  # one COUNT for three paging clicks
    assert [p["total"] for p in pages] == [3, 3, 3]
    # A generation bump (the registry content token moved) recounts exactly once.
    list_registry_expiries(path, limit=1, offset=0, count_cache=cache, generation=8)
    assert counts[0] == 2


def test_list_registry_contracts_count_cached_once_per_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    counts = [0]
    real = RegistryStore.count_contracts

    def _spy(self: RegistryStore, **kwargs: object) -> int:
        counts[0] += 1
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RegistryStore, "count_contracts", _spy)
    cache = _CountingCache()
    for offset in range(3):
        page = list_registry_contracts(
            path,
            underlying="NIFTY",
            expiry_date=_EXP_A.isoformat(),
            limit=1,
            offset=offset,
            count_cache=cache,
            generation=3,
        )
        assert page["total"] == 2  # the CE+PE pair on _EXP_A
    assert counts[0] == 1
    # A DIFFERENT filter is a different key — counted independently, once.
    other = list_registry_contracts(
        path, underlying="BANKNIFTY", limit=5, offset=0, count_cache=cache, generation=3
    )
    assert other["total"] == 1
    assert counts[0] == 2


def test_registry_pagers_cached_payload_equals_legacy(tmp_path: Path) -> None:
    """`count_cache=None` (legacy) and the cached path serve IDENTICAL payloads."""
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    cache = _CountingCache()
    legacy_exp = list_registry_expiries(path, underlying="NIFTY", limit=2, offset=1)
    cached_exp = list_registry_expiries(
        path, underlying="NIFTY", limit=2, offset=1, count_cache=cache, generation=1
    )
    assert cached_exp == legacy_exp
    legacy_con = list_registry_contracts(path, exch="NSE", limit=3, offset=0)
    cached_con = list_registry_contracts(
        path, exch="NSE", limit=3, offset=0, count_cache=cache, generation=1
    )
    assert cached_con == legacy_con


def test_registry_counts_cached_once_and_copy_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nav-badge counts dict is read once per generation and served as a COPY."""
    path = tmp_path / REGISTRY_DB_NAME
    _seed_registry(path)
    opens = [0]
    real_open = RegistryStore.open.__func__  # type: ignore[attr-defined]

    def _spy(cls: type[RegistryStore], p: Path, **kwargs: object) -> RegistryStore:
        opens[0] += 1
        return real_open(cls, p, **kwargs)  # type: ignore[arg-type,no-any-return]

    monkeypatch.setattr(RegistryStore, "open", classmethod(_spy))
    cache = _CountingCache()
    first = registry_counts(path, count_cache=cache, generation=5)
    second = registry_counts(path, count_cache=cache, generation=5)
    assert opens[0] == 1  # ONE store open + count pair per generation
    assert first == second == {"expiries": 3, "contracts": 4, "exists": True, "note": None}
    # The served dict is a COPY — a caller mutation never poisons the memo.
    first["expiries"] = 999
    assert registry_counts(path, count_cache=cache, generation=5)["expiries"] == 3


def test_registry_counts_missing_file_never_touches_the_cache(tmp_path: Path) -> None:
    cache = _CountingCache()
    payload = registry_counts(tmp_path / REGISTRY_DB_NAME, count_cache=cache, generation=1)
    assert payload["exists"] is False
    assert cache.computes == []


def test_registry_pagers_missing_file_never_touch_the_cache(tmp_path: Path) -> None:
    cache = _CountingCache()
    exp = list_registry_expiries(tmp_path / REGISTRY_DB_NAME, count_cache=cache, generation=1)
    con = list_registry_contracts(tmp_path / REGISTRY_DB_NAME, count_cache=cache, generation=1)
    assert exp["total"] == 0 and con["total"] == 0
    assert cache.computes == []
