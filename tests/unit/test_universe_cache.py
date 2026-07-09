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
* the defensive ``_revision`` branches (a ``None`` row / a ``None`` count);
* the two concurrent-install re-check branches (``_current`` adopting a peer's
  snapshot, ``summary`` adopting a peer's memo) via deterministic side-effect
  fakes — the real (rare) interleavings, simulated without threads; and
* the per-revision single-flight (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002): N
  concurrent cold pollers pay ONE base-read scan, and a base-read error releases
  the single-flight event (never a wedged waiter) + stores nothing.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these tests reach into the cache's own _revision / _current / _snapshot
# / _Snapshot to pin the resident-snapshot + concurrent-install invariants.)

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the universe cache reads a real DuckDB store")

import duckdb
from brutex.dashboard import universe_cache as uc
from brutex.dashboard.universe_cache import UniverseSnapshotCache, _Snapshot
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs
from structlog.typing import EventDict


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
    snap, served_last_known = cache._current(fake)
    assert snap is fake.peer  # adopted the peer, did NOT overwrite with the rebuild.
    assert served_last_known is False  # healthy path — never flagged as an outage serve.


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


# ── single-flight cold miss (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002) ────────────────


def test_cold_concurrent_pollers_single_flight() -> None:
    """N simultaneous cold misses pay ONE base-read scan (per-revision single-flight).

    The leader is held inside its FIRST base read while the followers pile up behind
    the single-flight event; releasing the leader wakes every follower onto the
    published snapshot. If the single-flight were absent, each follower would run its
    own base reads and ``compute_calls`` would climb past 1.
    """
    cache = UniverseSnapshotCache()
    leader_in = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    compute_calls = 0

    class _CountingStore:
        """A store whose first base read counts + blocks until released."""

        @property
        def connection(self) -> Any:
            return SimpleNamespace(execute=lambda _sql: SimpleNamespace(fetchone=lambda: (0, None)))

        def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
            nonlocal compute_calls
            with count_lock:
                compute_calls += 1
            leader_in.set()
            assert release.wait(timeout=5.0), "leader was never released"
            return frozenset()

        def list_index_equity_instruments(self) -> list[Instrument]:
            return []

        def reference_only_security_ids(self) -> frozenset[int]:
            return frozenset()

    store = _CountingStore()
    results: list[dict[str, int]] = []
    results_lock = threading.Lock()

    def _poll() -> None:
        value = cache.summary(store)
        with results_lock:
            results.append(value)

    # The leader enters the (blocked) base read first; the followers then register
    # behind its single-flight event (still held, so they all take the wait path).
    leader = threading.Thread(target=_poll)
    leader.start()
    assert leader_in.wait(timeout=5.0), "the leader never entered the base-read compute"
    followers = [threading.Thread(target=_poll) for _ in range(4)]
    for thread in followers:
        thread.start()
    # Give the followers a beat to reach ``event.wait()`` before releasing (harmless
    # if already there — a set event returns immediately, no lost wakeup).
    time.sleep(0.05)
    release.set()
    leader.join(timeout=10.0)
    for thread in followers:
        thread.join(timeout=10.0)

    assert not leader.is_alive() and all(not t.is_alive() for t in followers)
    assert compute_calls == 1  # single-flight: exactly ONE base-read scan
    assert len(results) == 5
    assert all(r == {"index_count": 0, "stock_count": 0, "total": 0} for r in results)
    assert cache.size() == 1
    assert cache._refreshing == {}  # the single-flight registration was cleaned up


class _RaisingStore:
    """A store whose FIRST base read raises — exercises the leader failure path."""

    def __init__(self, token: tuple[int, str]) -> None:
        self._token = token

    @property
    def connection(self) -> Any:
        return SimpleNamespace(
            execute=lambda _sql: SimpleNamespace(fetchone=lambda: (self._token[0], None))
        )

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        raise RuntimeError("store read failed")

    def list_index_equity_instruments(self) -> list[Instrument]:
        raise AssertionError("unreachable — the first base read raised")

    def reference_only_security_ids(self) -> frozenset[int]:
        raise AssertionError("unreachable — the first base read raised")


def test_compute_error_releases_single_flight() -> None:
    """A base-read error propagates, releases the event, and stores nothing.

    The leader's ``finally`` must drop the single-flight registration on failure so a
    waiter (or the next caller) can retry instead of hanging, and no snapshot is
    installed from the failed compute.
    """
    cache = UniverseSnapshotCache()
    raising = _RaisingStore(token=(0, ""))
    with pytest.raises(RuntimeError, match="store read failed"):
        cache.summary(raising)
    assert cache._refreshing == {}  # registration dropped — no waiter can hang
    assert cache.size() == 0  # nothing resident from the failed compute
    # A second call must also RAISE (not hang), proving the event was released.
    with pytest.raises(RuntimeError, match="store read failed"):
        cache.summary(raising)


# ── store-outage serve-last-known (DEC-CACHE-SERVE-LAST-KNOWN-001, 8th keystone) ──


def _outage_warns(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "universe_cache.store_probe_outage"]


def _recovered_infos(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "universe_cache.store_probe_recovered"]


class _TogglableOutageStore:
    """The scout's repro stub: independently wedgeable revision probe + base reads.

    ``fault`` picks the store-fault class (``duckdb.Error`` / ``OSError`` — both
    members of :data:`~brutex.dashboard.universe_cache.STORE_FAULT_TYPES`).
    """

    def __init__(self, fault: type[Exception] = duckdb.Error) -> None:
        self.token: tuple[int, str | None] = (1, "2026-07-09 00:00:00")
        self.wedge_probe = False
        self.wedge_base = False
        self.fault = fault
        self.probe_calls = 0
        self.base_calls = 0

    @property
    def connection(self) -> Any:
        outer = self

        class _Conn:
            def execute(self, _sql: str) -> Any:
                outer.probe_calls += 1
                if outer.wedge_probe:
                    raise outer.fault("IO Error: simulated wedged volume (probe)")
                return SimpleNamespace(fetchone=lambda: outer.token)

        return _Conn()

    def _wedgeable(self) -> None:
        self.base_calls += 1
        if self.wedge_base:
            raise self.fault("IO Error: simulated wedged volume (base read)")

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        self._wedgeable()
        return frozenset()

    def list_index_equity_instruments(self) -> list[Instrument]:
        return []

    def reference_only_security_ids(self) -> frozenset[int]:
        return frozenset()


@pytest.mark.parametrize("fault", [duckdb.Error, OSError])
def test_probe_outage_serves_resident_snapshot_labelled(fault: type[Exception]) -> None:
    """S2 (scout): warm snapshot + probe outage → serve-last-known flagged, ONE WARN.

    Fails-before: `_current` probed the revision BEFORE the resident-snapshot
    check, so the fault propagated on every poll (serve-last-known impossible by
    construction — the 3×500 storm despite a good snapshot).
    """
    fake = _TogglableOutageStore(fault=fault)
    cache = UniverseSnapshotCache()
    warm, warm_flag = cache.summary_view(fake)
    assert warm == {"index_count": 0, "stock_count": 0, "total": 0}
    assert warm_flag is False
    fake.wedge_probe = True
    with capture_logs() as logs:
        for _ in range(3):
            value, served_last_known = cache.summary_view(fake)
            assert value is warm  # the RESIDENT memo — never a recompute storm
            assert served_last_known is True
        rows, rows_flag = cache.instruments_view(fake, kind="index")
        assert rows == [] and rows_flag is True  # derived from the resident snapshot
    warns = _outage_warns(logs)
    assert len(warns) == 1  # latched — one WARN per outage, never per poll
    assert warns[0]["served"] == "last_known"
    assert _recovered_infos(logs) == []


def test_probe_outage_cold_raises_typed_fault() -> None:
    """S1 (scout): cold cache + probe outage → typed fault, never a raw store error.

    Fails-before: the raw ``duckdb.Error`` escaped to the route (a 500 per poll).
    """
    fake = _TogglableOutageStore()
    fake.wedge_probe = True
    cache = UniverseSnapshotCache()
    with capture_logs() as logs:
        with pytest.raises(uc.UniverseStoreOutageError, match="revision probe"):
            cache.summary_view(fake)
        with pytest.raises(uc.UniverseStoreOutageError):
            cache.instruments_view(fake, kind=None)
    assert cache.size() == 0  # nothing fabricated
    warns = _outage_warns(logs)
    assert len(warns) == 1 and warns[0]["served"] == "cold_fault"  # latched across repeats


def test_probe_outage_chains_the_original_store_fault() -> None:
    """The typed fault carries the store fault as ``__cause__`` (server-side diagnosis)."""
    fake = _TogglableOutageStore()
    fake.wedge_probe = True
    with pytest.raises(uc.UniverseStoreOutageError) as excinfo:
        UniverseSnapshotCache().summary_view(fake)
    assert isinstance(excinfo.value.__cause__, duckdb.Error)


def test_base_read_outage_serves_resident_snapshot_labelled() -> None:
    """S3 warm twin (scout): probe OK + base reads raise → serve-last-known flagged."""
    fake = _TogglableOutageStore()
    cache = UniverseSnapshotCache()
    warm = cache.summary(fake)
    fake.token = (2, "2026-07-09 06:00:00")  # a NEW revision → the rebuild path runs
    fake.wedge_base = True
    base_before = fake.base_calls
    with capture_logs() as logs:
        for _ in range(3):
            value, served_last_known = cache.summary_view(fake)
            assert value is warm
            assert served_last_known is True
    assert fake.base_calls == base_before + 3  # one failed rebuild attempt per poll…
    warns = _outage_warns(logs)
    assert len(warns) == 1 and warns[0]["served"] == "last_known"  # …but ONE WARN
    assert cache._refreshing == {}  # the single-flight registration never leaks


def test_base_read_outage_cold_raises_typed_fault() -> None:
    """S3 cold twin (scout): probe OK + base reads raise + nothing resident → typed fault."""
    fake = _TogglableOutageStore()
    fake.wedge_base = True
    cache = UniverseSnapshotCache()
    with capture_logs() as logs, pytest.raises(uc.UniverseStoreOutageError, match="base read"):
        cache.summary_view(fake)
    assert cache.size() == 0
    assert cache._refreshing == {}  # the finally released the registration
    warns = _outage_warns(logs)
    assert len(warns) == 1 and warns[0]["served"] == "cold_fault"


def test_recovery_logs_one_info_and_recomputes_fresh() -> None:
    """The next good probe recomputes fresh + logs EXACTLY one recovery INFO."""
    fake = _TogglableOutageStore()
    cache = UniverseSnapshotCache()
    cache.summary(fake)  # warm
    fake.wedge_probe = True
    with capture_logs() as logs:
        cache.summary_view(fake)  # outage serve → latch armed
    assert len(_outage_warns(logs)) == 1
    fake.wedge_probe = False
    fake.token = (2, "2026-07-09 06:00:00")  # the master moved during the outage
    with capture_logs() as logs:
        value, served_last_known = cache.summary_view(fake)
        assert served_last_known is False  # a real fresh compute, never a stale label
        again, again_flag = cache.summary_view(fake)
        assert again is value and again_flag is False
    assert len(_recovered_infos(logs)) == 1  # one flip INFO, then silence
    assert _outage_warns(logs) == []
    # A SECOND outage after recovery WARNs again (the latch re-armed).
    fake.wedge_probe = True
    with capture_logs() as logs:
        cache.summary_view(fake)
    assert len(_outage_warns(logs)) == 1


def test_outage_summary_derivation_is_pure_over_the_resident_snapshot() -> None:
    """An outage serve still derives + memoises payloads (zero store I/O needed)."""
    fake = _TogglableOutageStore()
    cache = UniverseSnapshotCache()
    cache.instruments(fake, kind=None)  # warm via instruments — summary NOT yet memoised
    fake.wedge_probe = True
    probe_before = fake.probe_calls
    base_before = fake.base_calls
    value, served_last_known = cache.summary_view(fake)
    assert value == {"index_count": 0, "stock_count": 0, "total": 0}
    assert served_last_known is True
    rows, rows_flag = cache.instruments_view(fake, kind="stock")
    assert rows == [] and rows_flag is True
    assert fake.probe_calls == probe_before + 2  # one failed probe per call…
    assert fake.base_calls == base_before  # …and ZERO base reads (pure derivation)


def test_empty_store_path_is_byte_identical_after_the_fix(store: InstrumentStore) -> None:
    """REGRESSION: the healthy empty-store path (stable ``(0, "")`` token) is unchanged."""
    cache = UniverseSnapshotCache()
    assert cache._revision(store) == (0, "")
    payload, served_last_known = cache.summary_view(store)
    assert payload == {"index_count": 0, "stock_count": 0, "total": 0}
    assert served_last_known is False
    rows, rows_flag = cache.instruments_view(store, kind=None)
    assert rows == [] and rows_flag is False
    assert cache.summary(store) is payload  # the delegator serves the same memo
    assert cache.instruments(store, kind=None) is rows
