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
    assert cache.summary(store) == {
        "index_count": 0,
        "stock_count": 0,
        "total": 0,
        "reference_stock_count": 0,
        "stock_count_total": 0,
    }
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
    assert first == {
        "index_count": 1,
        "stock_count": 0,
        "total": 1,
        "reference_stock_count": 0,
        "stock_count_total": 0,
    }
    assert cache.summary(store) is first  # same object → served from the memo.


def test_summary_threads_reference_ids_for_stock_split(store: InstrumentStore) -> None:
    """DEC-LABEL-TRUTH-001: the snapshot's reference ids flow into the summary.

    A REAL lean store with one F&O equity + one REFERENCE_ONLY constituent must
    serve the additive ``reference_stock_count`` / ``stock_count_total`` split
    the Stocks badge renders — with every legacy key byte-identical (on the
    lean store the reference row is eligible-by-storage, so ``stock_count``
    already counts it; the id-membership split is what names the subset).

    FAILS BEFORE: the cache computed ``universe_summary`` WITHOUT the
    snapshot's ``reference`` set → ``reference_stock_count`` was absent (and a
    naive classify-diff would read 0 here even after adding the keys).
    """
    store.upsert_many(
        [_nifty(), _equity(100), _equity(200)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="r1".ljust(64, "0"),
        reference_only_of=lambda inst: inst.security_id == 200,
    )
    cache = UniverseSnapshotCache()
    assert cache.summary(store) == {
        "index_count": 1,
        "stock_count": 2,  # lean-store legacy value — reference row eligible-by-storage
        "total": 3,
        "reference_stock_count": 1,
        "stock_count_total": 2,
    }
    # The badge's population equals the Stocks tab's population (the label truth).
    assert cache.summary(store)["stock_count_total"] == len(
        cache.instruments(store, kind="stock")
    )


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
    assert first == {
        "index_count": 1,
        "stock_count": 0,
        "total": 1,
        "reference_stock_count": 0,
        "stock_count_total": 0,
    }

    # A new snapshot day adds a stock → the (count, max fetched_at) token moves.
    _seed(store, [_nifty(), _equity(200)], run_id="r2", day=date(2024, 1, 3))
    second = cache.summary(store)
    assert second == {
        "index_count": 1,
        "stock_count": 1,
        "total": 2,
        "reference_stock_count": 0,
        "stock_count_total": 1,
    }
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
