"""100% line+branch tests for the `/universe` backend snapshot cache (DEC-UNIVERSE-BACKEND-CACHE-001).

The cache memoises the three lean store reads + the derived ``summary`` /
per-``kind`` instrument payloads, keyed by a content-revision token over the
``instruments`` table (``count(*)`` + ``max(fetched_at)``). These tests pin:

* an empty store keys at the stable ``(0, "")`` token and is cached;
* a hit returns the SAME memoised object (no recompute);
* each ``kind`` (``None`` / ``index`` / ``stock``) is cached independently;
* a bad ``kind`` raises ``ValueError`` and is NOT cached;
* a master change (a second ``upsert_many`` → a fresh ``fetched_at``) moves the
  token and forces a rebuild (no stale illusion — Golden Rule #1);
* the defensive ``_revision`` branches (a ``None`` row / a ``None`` count); and
* the two concurrent-install re-check branches (``_current`` adopting a peer's
  snapshot, ``summary`` adopting a peer's memo) via deterministic side-effect
  fakes — the real (rare) interleavings, simulated without threads.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these tests reach into the cache's own _revision / _current / _snapshot
# / _Snapshot to pin the resident-snapshot + concurrent-install invariants.)

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the universe cache reads a real DuckDB store")

from brutex.dashboard import universe_cache as uc
from brutex.dashboard.universe_cache import UniverseSnapshotCache, _Snapshot
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _nifty(security_id: int = 13) -> Instrument:
    """An admitted NIFTY index row (classified ``index``)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _equity(security_id: int) -> Instrument:
    """An F&O-eligible NSE equity row (classified ``stock``)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=f"S{security_id}",
        display_name=f"Stock {security_id}",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=f"S{security_id}",
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real, empty DuckDB instrument store (caller seeds it as needed)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    yield instrument_store
    instrument_store.close()


def _seed(store: InstrumentStore, instruments: list[Instrument], *, run_id: str, day: date) -> None:
    """Upsert *instruments* as one snapshot (stamps a fresh ``fetched_at``)."""
    store.upsert_many(
        instruments,
        snapshot_date=day,
        run_id=run_id,
        csv_source_sha256=run_id.ljust(64, "0"),
    )


# ── empty store ────────────────────────────────────────────────────────────────


def test_fresh_cache_size_is_zero() -> None:
    """A cache that has answered nothing holds no resident snapshot."""
    assert UniverseSnapshotCache().size() == 0


def test_empty_store_is_cached_at_stable_token(store: InstrumentStore) -> None:
    """An empty universe keys at ``(0, "")`` and is cached + served."""
    cache = UniverseSnapshotCache()
    assert cache._revision(store) == (0, "")
    assert cache.summary(store) == {"index_count": 0, "stock_count": 0, "total": 0}
    assert cache.instruments(store, kind=None) == []
    assert cache.instruments(store, kind="index") == []
    assert cache.instruments(store, kind="stock") == []
    assert cache.size() == 1


# ── hits + per-kind memoisation ─────────────────────────────────────────────────


def test_summary_hit_returns_memoised_object(store: InstrumentStore) -> None:
    """A repeat ``summary`` poll returns the IDENTICAL cached dict (no recompute)."""
    _seed(store, [_nifty()], run_id="r1", day=date(2024, 1, 2))
    cache = UniverseSnapshotCache()
    first = cache.summary(store)
    assert first == {"index_count": 1, "stock_count": 0, "total": 1}
    assert cache.summary(store) is first  # same object → served from the memo.


def test_instruments_per_kind_cached_independently(store: InstrumentStore) -> None:
    """Each ``kind`` caches its own row list; repeats return the same object."""
    _seed(store, [_nifty(), _equity(100)], run_id="r1", day=date(2024, 1, 2))
    cache = UniverseSnapshotCache()
    both = cache.instruments(store, kind=None)
    index_rows = cache.instruments(store, kind="index")
    stock_rows = cache.instruments(store, kind="stock")
    assert {r["symbol"] for r in index_rows} == {"NIFTY"}
    assert {r["symbol"] for r in stock_rows} == {"S100"}
    assert len(both) == 2
    # Each kind is memoised on its own key → identity-stable on repeat.
    assert cache.instruments(store, kind=None) is both
    assert cache.instruments(store, kind="index") is index_rows
    assert cache.instruments(store, kind="stock") is stock_rows


def test_bad_kind_raises_value_error_and_is_not_cached(store: InstrumentStore) -> None:
    """A bad ``kind`` raises ``ValueError`` BEFORE any caching (no poison)."""
    _seed(store, [_nifty()], run_id="r1", day=date(2024, 1, 2))
    cache = UniverseSnapshotCache()
    cache.summary(store)  # establish the resident snapshot
    with pytest.raises(ValueError, match="kind must be one of"):
        cache.instruments(store, kind="bogus")
    # The bad key was never stored — only legitimate kinds may ever appear.
    assert cache._snapshot is not None
    assert "bogus" not in cache._snapshot.instruments


# ── invalidation ────────────────────────────────────────────────────────────────


def test_master_change_invalidates_and_rebuilds(store: InstrumentStore) -> None:
    """A second load (fresh ``fetched_at``) moves the token → recompute, new object."""
    _seed(store, [_nifty()], run_id="r1", day=date(2024, 1, 2))
    cache = UniverseSnapshotCache()
    first = cache.summary(store)
    first_index = cache.instruments(store, kind="index")
    assert first == {"index_count": 1, "stock_count": 0, "total": 1}

    # A new snapshot day adds a stock → the (count, max fetched_at) token moves.
    _seed(store, [_nifty(), _equity(200)], run_id="r2", day=date(2024, 1, 3))
    second = cache.summary(store)
    assert second == {"index_count": 1, "stock_count": 1, "total": 2}
    assert second is not first  # rebuilt, not the stale memo.
    second_index = cache.instruments(store, kind="index")
    assert second_index is not first_index  # the per-kind memo was dropped too.


# ── defensive `_revision` branches ──────────────────────────────────────────────


def test_revision_none_row_is_zero_token() -> None:
    """A ``fetchone()`` of ``None`` (paranoid guard) yields the ``(0, "")`` token."""
    fake = SimpleNamespace(
        connection=SimpleNamespace(execute=lambda _sql: SimpleNamespace(fetchone=lambda: None))
    )
    assert UniverseSnapshotCache._revision(fake) == (0, "")


def test_revision_null_count_is_zero_token() -> None:
    """A ``(None, None)`` aggregate row (no rows) normalises to ``(0, "")``."""
    fake = SimpleNamespace(
        connection=SimpleNamespace(
            execute=lambda _sql: SimpleNamespace(fetchone=lambda: (None, None))
        )
    )
    assert UniverseSnapshotCache._revision(fake) == (0, "")


def test_public_master_revision_is_the_shared_token_source(store: InstrumentStore) -> None:
    """The public ``master_revision`` COMMON helper (DEC-DASHBOARD-NAV-LATENCY-001).

    An empty store keys at the stable ``(0, "")``; a load advances the count, and a
    re-load (fresh ``fetched_at``) advances the timestamp — the exact freshness signal
    the ``/coverage`` + ``/universe`` nav-latency caches fold into their composite key.
    ``_revision`` delegates to it, so both surfaces read ONE source of truth.
    """
    assert uc.master_revision(store) == (0, "")
    store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    first = uc.master_revision(store)
    assert first[0] >= 1 and first[1] != ""
    # A re-load stamps a fresh ``fetched_at``, so the token strictly MOVES.
    store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 3), run_id="r2", csv_source_sha256="1" * 64
    )
    second = uc.master_revision(store)
    assert second != first
    # The private wrapper returns the identical token (no second source).
    assert UniverseSnapshotCache._revision(store) == second


# ── coverage_revision — the master_revision twin over coverage_ledger ───────────
# (DEC-STALENESS-CACHE-TRUTH-001, MIGRATION.md #1641)


def _cov_record(underlying: str, year: int, *, fetched_at_iso: str) -> Any:
    """One coverage_ledger cell with an explicit ``last_fetched_at``."""
    from datetime import datetime

    from brutex.data.coverage_ledger import CoverageRecord, classify_cell

    return CoverageRecord(
        exch_id="NSE",
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=2,
        contracts_pulled=20,
        status=classify_cell(2, 20, None),
        last_run_id="run-1",
        last_fetched_at=datetime.fromisoformat(fetched_at_iso),
    )


def test_coverage_revision_empty_store_is_stable_zero_token(store: InstrumentStore) -> None:
    """An empty coverage_ledger keys at the stable ``(0, "")`` — and stays there."""
    assert uc.coverage_revision(store) == (0, "")
    # Unchanged table → byte-identical token (no thrash — the key is stable).
    assert uc.coverage_revision(store) == (0, "")


def test_coverage_revision_moves_on_upsert_and_on_newer_refetch(store: InstrumentStore) -> None:
    """Every ``upsert_coverage`` write moves the token; an unchanged table does not.

    The freshness signal the ``_nav_latency_key`` 4th component folds in
    (DEC-STALENESS-CACHE-TRUTH-001): a NEW cell moves the count, an idempotent
    re-upsert of the SAME cell with a newer ``last_fetched_at`` moves the MAX —
    either way the composite key changes and the nav-latency caches recompute.
    """
    store.upsert_coverage(_cov_record("RELIANCE", 2024, fetched_at_iso="2026-05-30T09:15:00+00:00"))
    first = uc.coverage_revision(store)
    assert first[0] == 1 and first[1] != ""
    # No write → the token is byte-identical (stable key, no cache thrash).
    assert uc.coverage_revision(store) == first
    # An idempotent re-upsert of the SAME cell with a newer stamp moves the MAX.
    store.upsert_coverage(_cov_record("RELIANCE", 2024, fetched_at_iso="2026-05-30T10:15:00+00:00"))
    second = uc.coverage_revision(store)
    assert second[0] == 1
    assert second != first
    # A new cell moves the count.
    store.upsert_coverage(_cov_record("TCS", 2024, fetched_at_iso="2026-05-30T10:15:00+00:00"))
    third = uc.coverage_revision(store)
    assert third[0] == 2
    assert third != second


def test_coverage_revision_defensive_none_row_is_zero_token() -> None:
    """The shared ``_revision_token`` paranoid ``None``-row guard (both probes)."""
    fake = SimpleNamespace(
        connection=SimpleNamespace(execute=lambda _sql: SimpleNamespace(fetchone=lambda: None))
    )
    assert uc.coverage_revision(fake) == (0, "")


# ── concurrent-install re-check branches (deterministic, no threads) ────────────


class _PeerInstallStore:
    """A fake store whose LAST base read installs a peer snapshot of the same token.

    Simulates a concurrent miss winning the race: while THIS call rebuilds outside
    the lock, a peer thread installs a snapshot of the identical revision. The
    re-check in :meth:`UniverseSnapshotCache._current` must then ADOPT the peer's
    snapshot (keeping its memoised payloads), not overwrite it with the rebuild.
    """

    def __init__(self, cache: UniverseSnapshotCache, token: tuple[int, str]) -> None:
        self._cache = cache
        self._token = token
        self.peer = _Snapshot(
            revision=token,
            eligible=frozenset(),
            candidates=[],
            reference=frozenset(),
        )

    @property
    def connection(self) -> Any:
        return SimpleNamespace(
            execute=lambda _sql: SimpleNamespace(fetchone=lambda: (self._token[0], None))
        )

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        return frozenset()

    def list_index_equity_instruments(self) -> list[Instrument]:
        return []

    def reference_only_security_ids(self) -> frozenset[int]:
        # The last read before `_current` re-acquires the lock: install the peer.
        self._cache._snapshot = self.peer
        return frozenset()


def test_current_adopts_concurrent_peer_snapshot() -> None:
    """``_current`` returns the peer's snapshot when one of the same revision raced in."""
    cache = UniverseSnapshotCache()
    fake = _PeerInstallStore(cache, token=(0, ""))
    snap = cache._current(fake)
    assert snap is fake.peer  # adopted the peer, did NOT overwrite with the rebuild.


def test_summary_adopts_concurrent_peer_memo(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``summary`` returns a peer-set memo when one lands during its compute."""
    _seed(store, [_nifty()], run_id="r1", day=date(2024, 1, 2))
    cache = UniverseSnapshotCache()
    peer_value = {"index_count": 99, "stock_count": 0, "total": 99}

    def _peer_sets_summary(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        # Mimic a concurrent compute that memoised first; this call's own value
        # must then lose (last-meaningful-write-wins is the resident snapshot's).
        assert cache._snapshot is not None
        cache._snapshot.summary = peer_value
        return {"index_count": 1, "stock_count": 0, "total": 1}

    monkeypatch.setattr(uc, "universe_summary", _peer_sets_summary)
    result = cache.summary(store)
    assert result is peer_value  # adopted the peer's memo, not this call's value.
