"""Single-flight + adaptive floor on the ledger record caches (DEC-LEDGER-DELTA-O-PAGE-001, sub-part b).

The three db_view module dict caches (``_WINDOW_CACHE`` / ``_WINDOW_ROWS_CACHE``
/ ``_SPOT_ROLLUP_CACHE``) collapsed onto ONE reused
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`
(``db_view._LEDGER_READ_CACHE``), keyed per family at a generation digested from
the composite ledger stamp token
(:func:`~brutex.dashboard.lake_revision.revision_generation`). This suite pins:

* T-b1  8 concurrent cold-stamp pollers pay EXACTLY ONE ledger parse
        (per-key single-flight — the concurrent ~13.6 s duplicate-parse bug);
* T-b2  the two-key rows→records read (two key families, one instance)
        completes threaded without deadlock, still one parse;
* T-b3  a stamp move within the adaptive floor serves the prior entry STALE with
        ONE ``movers_cache.stale_served`` log (the honesty surface — never a
        silent illusion), and a read past the floor parses fresh;
* T-b4  the floor is the #920 formula — a cheap parse keeps the 2 s base, a
        500 s parse is capped at 120 s;
* T-b5  a raising compute caches nothing and a single-flight waiter recovers;
* T-b6  a base-less DELTA-ONLY lake now renders /db grid rows (the fixed
        ``window_ledger_rows_cached`` ↔ ``_ledger_records_cached`` inconsistency);
* T-b7  the ``_reset_ledger_read_cache`` test seam replaces the dict-clear idiom;
* T-b8  ``effective_adaptive_floor`` boundary cases + the one-source-of-truth
        delegation pins (summary_cache is THE home; movers_cache + the db_view
        cap delegate — CLAUDE.md §11 COMMON);
* T-b9  the fix-round LATCH regression: a derived projection (rows / rollup)
        built from floor-STALE records is stamped at the records' SERVED
        generation — never the fresh one — so it reconverges to on-disk truth
        the moment the records floor passes instead of serving pre-flush
        content indefinitely under a clean generation hit.

Tests MAY import ``window_ledger_store`` (the module itself must not — the
pinned no-import edge, verified by ``test_db_view_no_window_ledger_store_import``
in the fold suite).
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")

from brutex.dashboard import db_view, movers_cache, summary_cache
from brutex.dashboard.db_view import (
    _ledger_records_cached,
    spot_window_rollup,
    window_ledger_rows_cached,
)
from brutex.dashboard.lake_revision import revision_generation
from brutex.dashboard.summary_cache import effective_adaptive_floor
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    append_ledger_delta,
    load_window_ledger,
    save_window_ledger,
)
from structlog.testing import capture_logs

_NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _key(symbol: str) -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=symbol,
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 1, 31),
    )


def _seed(lake_root: Path, symbols: list[str]) -> None:
    ledger = WindowLedger()
    for symbol in symbols:
        ledger.record(_key(symbol), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    save_window_ledger(ledger, lake_root)


def _append_delta(lake_root: Path, symbol: str) -> None:
    live = load_window_ledger(lake_root)
    live.record(_key(symbol), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
    assert append_ledger_delta(live, lake_root) == 1


def _install_clocked_cache(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Swap in a fresh ledger read cache driven by a mutable test clock."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache(clock=lambda: clock["t"])
    )
    return clock


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


class TestSingleFlight:
    """T-b1/T-b2/T-b5 — concurrent stamp-miss pollers share ONE parse."""

    def test_eight_cold_pollers_pay_one_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b1: 8 threads on a cold stamp → exactly 1 ``load_window_ledger``."""
        _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY", "BANKNIFTY"])
        compute_started = threading.Event()
        release = threading.Event()
        parses = {"n": 0}
        real_load = load_window_ledger

        def _gated_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            compute_started.set()
            assert release.wait(timeout=10)
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _gated_load)
        results: list[tuple[Any, ...]] = []

        def _worker() -> None:
            results.append(_ledger_records_cached(tmp_path))

        lead = threading.Thread(target=_worker)
        lead.start()
        assert compute_started.wait(timeout=10)  # the leader is registered + parsing
        waiters = [threading.Thread(target=_worker) for _ in range(7)]
        for t in waiters:
            t.start()
        release.set()
        lead.join(timeout=10)
        for t in waiters:
            t.join(timeout=10)
        assert not lead.is_alive() and not any(t.is_alive() for t in waiters)
        assert parses["n"] == 1  # ONE parse — never eight
        assert len(results) == 8
        assert all(r == results[0] for r in results)
        assert len(results[0]) == 2

    def test_nested_rows_records_compute_completes_without_deadlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b2: the rows read crosses TWO key families of the SAME cache
        instance (``ledger-records`` served first, then the ``ledger-rows``
        projection stamped at the served records generation) — per-key
        single-flight, so the two-key read never self-deadlocks and 8
        concurrent grid pollers still pay exactly ONE parse."""
        _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        parses = {"n": 0}
        real_load = load_window_ledger

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _counting_load)
        results: list[tuple[tuple[Any, ...], ...]] = []
        threads = [
            threading.Thread(target=lambda: results.append(window_ledger_rows_cached(tmp_path)))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)  # no deadlock
        assert parses["n"] == 1  # the nested records compute ran ONCE
        assert len(results) == 8
        assert all(r == results[0] for r in results)
        assert list(results[0]) == load_window_ledger(tmp_path).to_rows()

    def test_raising_compute_caches_nothing_and_waiter_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b5: a raised parse propagates, stores nothing, wakes the waiter —
        which takes over and parses fresh (the reused cache contract)."""
        _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        compute_started = threading.Event()
        release = threading.Event()
        fail_first = {"armed": True}
        real_load = load_window_ledger

        class _TornRead(RuntimeError):
            pass

        def _flaky_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            if fail_first["armed"]:
                fail_first["armed"] = False
                compute_started.set()
                assert release.wait(timeout=10)
                raise _TornRead("simulated torn parse")
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _flaky_load)
        errors: list[BaseException] = []
        results: list[tuple[Any, ...]] = []

        def _leader() -> None:
            try:
                _ledger_records_cached(tmp_path)
            except _TornRead as exc:
                errors.append(exc)

        def _waiter() -> None:
            results.append(_ledger_records_cached(tmp_path))

        lead = threading.Thread(target=_leader)
        lead.start()
        assert compute_started.wait(timeout=10)
        wait_thread = threading.Thread(target=_waiter)
        wait_thread.start()
        release.set()
        lead.join(timeout=10)
        wait_thread.join(timeout=10)
        assert not lead.is_alive() and not wait_thread.is_alive()
        assert len(errors) == 1  # the exception propagated untouched (nothing cached)
        assert len(results) == 1 and len(results[0]) == 1  # the waiter took over


class TestAdaptiveFloor:
    """T-b3/T-b4 — the #920 floor bounds re-parse duty; staleness is surfaced."""

    def test_stamp_move_within_floor_serves_stale_logged_then_fresh_past_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b3: within ``min(max(2 s, duration), 120 s)`` of the last parse a
        stamp move serves the PRIOR entry with the ``movers_cache.stale_served``
        honesty log; past the floor the same read parses fresh. The log fires on
        the ``ledger-records`` BASE key — the entry that actually serves stale;
        the derived rollup is stamped at the SERVED records generation, so it is
        a clean generation hit on that stale epoch (the latch fix), never a
        silently-fresh-stamped copy."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}  # warm @ t=0
        _append_delta(tmp_path, "BANKNIFTY")  # the stamp moves
        clock["t"] = 1.0  # age 1 s < the 2 s base floor (cheap parse)
        with capture_logs() as logs:
            stale = spot_window_rollup(tmp_path)
        assert stale == {("NIFTY", "1minute"): (1, 0, 0)}  # the PRIOR rollup, delta unseen
        (served,) = _events(logs, "movers_cache.stale_served")  # ONE honesty log
        assert served["stale"] is True
        assert served["age_s"] == 1.0
        assert served["floor_s"] == 2.0
        assert str(served["key"]).startswith("ledger-records|")
        assert served["entry_generation"] != served["generation"]
        clock["t"] = 2.5  # past the floor → fresh parse
        assert spot_window_rollup(tmp_path) == {
            ("NIFTY", "1minute"): (1, 0, 0),
            ("BANKNIFTY", "1minute"): (1, 0, 0),
        }

    def test_production_cache_wiring_binds_the_stamp_probe(self) -> None:
        """The PRODUCTION module cache is built by the SAME factory the clocked
        tests exercise: the constant-``True`` stamp probe (a cross-process CLI
        writer trips the floor too — the judges' probe fix) with the floor
        opted in at the 120 s #920 cap — so the deterministic clocked pins
        above ARE the production semantics, not a test-only construction."""
        assert db_view._ledger_stamp_moved() is True
        cache = db_view._LEDGER_READ_CACHE
        assert cache._pull_active_probe is db_view._ledger_stamp_moved
        assert cache._adaptive_floor is True
        assert cache._adaptive_floor_cap_s == 120.0
        assert cache.capacity == 32

    def test_slow_parse_throttles_refire_and_caps_at_120s(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b4 (slow arm): a 500 s parse earns the CAPPED 120 s floor — a
        stamp move at age 119 s still serves stale; 121 s parses fresh."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        real_load = load_window_ledger

        def _slow_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            clock["t"] += 500.0  # the parse itself "takes" 500 s
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _slow_load)
        warmed = _ledger_records_cached(tmp_path)  # computed_at = 500.0, duration = 500 s
        _append_delta(tmp_path, "BANKNIFTY")
        clock["t"] = 500.0 + 119.0  # within the 120 s CAP (not the raw 500 s)
        with capture_logs() as logs:
            assert _ledger_records_cached(tmp_path) == warmed  # stale-served
        (served,) = _events(logs, "movers_cache.stale_served")
        assert served["floor_s"] == 120.0  # min(max(2, 500), 120) — the cap's teeth
        clock["t"] = 500.0 + 121.0  # past the cap → fresh parse (≤50%-duty law upheld)
        refreshed = _ledger_records_cached(tmp_path)
        assert {r.security_id for r in refreshed} == {"NIFTY", "BANKNIFTY"}

    def test_cheap_parse_keeps_the_2s_base_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-b4 (cheap arm): a fast parse's floor is the snappy 2 s base — a
        stamp move at age 2.1 s parses fresh immediately (no cap inflation)."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        _ledger_records_cached(tmp_path)  # duration 0 under the frozen clock
        _append_delta(tmp_path, "BANKNIFTY")
        clock["t"] = 2.1  # just past min(max(2, 0), 120) = 2 s
        refreshed = _ledger_records_cached(tmp_path)
        assert {r.security_id for r in refreshed} == {"NIFTY", "BANKNIFTY"}


class TestBaseLessLakeReconciliation:
    """T-b6 — the base-less delta-only inconsistency is FIXED."""

    def test_delta_only_lake_renders_grid_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first pull's delta-only checkpoint renders on the /db grid like
        every other ledger reader (``window_ledger_rows_cached`` returned
        ``()`` while ``_ledger_records_cached`` read the live deltas)."""
        _install_clocked_cache(monkeypatch)
        _append_delta(tmp_path, "NIFTY")  # NO base ledger.parquet exists
        rows = window_ledger_rows_cached(tmp_path)
        assert [row[1] for row in rows] == ["NIFTY"]
        # Reconciled: the grid projection equals the records read + the
        # canonical writer-side row shape — ONE answer across every reader.
        assert list(rows) == load_window_ledger(tmp_path).to_rows()
        assert len(_ledger_records_cached(tmp_path)) == 1

    def test_no_base_no_deltas_still_reads_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely-empty lake keeps the honest ``()`` (never a raise)."""
        _install_clocked_cache(monkeypatch)
        assert window_ledger_rows_cached(tmp_path) == ()


class TestResetSeam:
    """T-b7 — the reset seam replaces the retired dict-clear idiom."""

    def test_reset_forces_a_fresh_parse_at_the_same_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(tmp_path, ["NIFTY"])
        db_view._reset_ledger_read_cache()
        before = db_view._LEDGER_READ_CACHE
        parses = {"n": 0}
        real_load = load_window_ledger

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _counting_load)
        _ledger_records_cached(tmp_path)
        _ledger_records_cached(tmp_path)
        assert parses["n"] == 1  # warm
        db_view._reset_ledger_read_cache()
        assert db_view._LEDGER_READ_CACHE is not before  # a genuinely new instance
        _ledger_records_cached(tmp_path)
        assert parses["n"] == 2  # cold again — the seam's whole purpose


class TestDerivedKeysNeverLatchStaleRecords:
    """T-b9 — the fix-round MAJOR: derived keys must never latch stale records.

    Pre-fix, ``window_ledger_rows_cached`` / ``spot_window_rollup`` computed
    their projections at the CURRENT stamp generation while the nested
    ``_ledger_records_cached`` read could be adaptive-floor STALE-served —
    caching pre-flush content AT THE FRESH generation, which every later poll
    then hit cleanly (no recompute, no ``stale_served`` log) until the NEXT
    writer flush, hours or days later. The fix stamps the derived entry with
    the generation the records were ACTUALLY served at
    (``_ledger_records_stamped`` embeds it in the cached value), so the derived
    entry stays generation-stale and reconverges once the records floor passes.
    """

    def test_rows_and_rollup_reconverge_once_the_records_floor_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE latch repro (fails-before/passes-after): cold records parse with
        a long duration (10 s floor) → cheap derived computes on a records HIT
        (2 s floor) → stamp move → a poll in the (2 s, 10 s) age window builds
        the derived values from floor-stale records → long past every floor the
        derived keys MUST serve the on-disk truth (pre-fix: latched forever)."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        real_load = load_window_ledger

        def _slow_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            clock["t"] += 10.0  # the records parse "takes" 10 s → records floor 10 s
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _slow_load)
        # One consumer parses records COLD (duration 10 s → floor 10 s)…
        _ledger_records_cached(tmp_path)  # computed_at = 10.0
        # …the derived consumers compute cheaply on a records HIT (floor 2 s).
        assert [r[1] for r in window_ledger_rows_cached(tmp_path)] == ["NIFTY"]
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}
        _append_delta(tmp_path, "BANKNIFTY")  # the pull-end flush — the stamp moves
        # A poll past the derived 2 s floor but WITHIN the records 10 s floor:
        # the derived values rebuild/serve from the floor-stale records epoch.
        clock["t"] = 13.0  # derived age 3 s > 2 s; records age 3 s < 10 s
        with capture_logs() as logs:
            stale_rows = window_ledger_rows_cached(tmp_path)
            stale_rollup = spot_window_rollup(tmp_path)
        assert [r[1] for r in stale_rows] == ["NIFTY"]  # bounded stale — records floor
        assert stale_rollup == {("NIFTY", "1minute"): (1, 0, 0)}
        served = _events(logs, "movers_cache.stale_served")
        assert len(served) == 2  # one honest log PER serve — never silent
        assert {str(e["key"]).split("|")[0] for e in served} == {"ledger-records"}
        # THE LATCH PIN: long past every floor the derived keys reconverge to
        # the on-disk truth. Pre-fix both were cached at the FRESH generation
        # (a clean hit forever) and this still read ["NIFTY"] / settled-only.
        clock["t"] = 990.0
        with capture_logs() as logs:
            rows = window_ledger_rows_cached(tmp_path)
            rollup = spot_window_rollup(tmp_path)
        assert sorted(r[1] for r in rows) == ["BANKNIFTY", "NIFTY"]
        assert list(rows) == load_window_ledger(tmp_path).to_rows()
        assert rollup == {
            ("NIFTY", "1minute"): (1, 0, 0),
            ("BANKNIFTY", "1minute"): (1, 0, 0),
        }
        assert _events(logs, "movers_cache.stale_served") == []  # a genuinely fresh read

    def test_derived_value_is_never_stamped_newer_than_its_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The structural invariant behind T-b9, asserted on the cache entries
        themselves: a derived projection built COLD from a floor-stale records
        serve is stamped with the records entry's generation (the stale epoch)
        — never the fresh stamp the records serve did not reach (which is
        exactly what the pre-fix code stored, creating the latch)."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        real_load = load_window_ledger

        def _slow_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            clock["t"] += 10.0
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _slow_load)
        # Warm ONLY the records key (the derived keys stay cold), then move the
        # stamp: the derived computes below build from the STALE-served records.
        _ledger_records_cached(tmp_path)
        _append_delta(tmp_path, "BANKNIFTY")
        clock["t"] = 13.0  # inside the records 10 s floor
        window_ledger_rows_cached(tmp_path)
        spot_window_rollup(tmp_path)
        from brutex.data.window_ledger_store import ledger_path

        path = ledger_path(tmp_path).as_posix()
        entries = db_view._LEDGER_READ_CACHE._entries
        records_gen = entries[f"ledger-records|{path}"].generation
        fresh_gen = revision_generation(db_view._ledger_stamp_token(ledger_path(tmp_path)))
        assert records_gen != fresh_gen  # the records really are floor-stale here
        assert entries[f"ledger-rows|{path}"].generation == records_gen
        assert entries[f"spot-rollup|{path}"].generation == records_gen


class TestEffectiveAdaptiveFloorExtraction:
    """T-b8 — ONE #920 formula, hand-computed boundaries, delegation pins."""

    def test_boundary_cases_hand_computed(self) -> None:
        # Below the base → the base wins.
        assert effective_adaptive_floor(0.5, base_s=2.0, cap_s=120.0) == 2.0
        # Between base and cap → the duration wins (the ≤50%-duty law).
        assert effective_adaptive_floor(30.0, base_s=2.0, cap_s=120.0) == 30.0
        # Above the cap → the cap wins (bounded staleness).
        assert effective_adaptive_floor(500.0, base_s=2.0, cap_s=120.0) == 120.0
        # Exactly at the boundaries.
        assert effective_adaptive_floor(2.0, base_s=2.0, cap_s=120.0) == 2.0
        assert effective_adaptive_floor(120.0, base_s=2.0, cap_s=120.0) == 120.0
        # A non-monotonic clock's negative duration is neutralized by the base.
        assert effective_adaptive_floor(-1.0, base_s=2.0, cap_s=120.0) == 2.0
        # A base above the cap is still capped (min applies last).
        assert effective_adaptive_floor(1.0, base_s=5.0, cap_s=3.0) == 3.0

    def test_one_source_of_truth_delegation(self) -> None:
        """summary_cache is THE home; movers_cache imports the SAME object
        (never a second copy of the arithmetic — CLAUDE.md §11 COMMON)."""
        # __dict__ access (not attribute) — movers_cache deliberately does NOT
        # re-export the name (mypy strict no-implicit-reexport); the pin is
        # that its module namespace binds THE summary_cache object.
        assert (
            movers_cache.__dict__["effective_adaptive_floor"]
            is summary_cache.effective_adaptive_floor
        )

    def test_summary_and_movers_arithmetic_byte_equal(self) -> None:
        """The delegated call sites' arguments reproduce the pre-extraction
        arithmetic byte-for-byte against hand-computed values."""
        # SummaryCache._plan: base = ttl_floor_s (2.0), cap = MAX_ADAPTIVE_FLOOR_S.
        assert effective_adaptive_floor(
            30.0, base_s=summary_cache.DEFAULT_TTL_FLOOR_S, cap_s=summary_cache.MAX_ADAPTIVE_FLOOR_S
        ) == min(max(2.0, 30.0), 120.0)
        # MoversSnapshotCache defaults: base 2.0, cap 600.0.
        assert effective_adaptive_floor(
            700.0,
            base_s=movers_cache.DEFAULT_ADAPTIVE_BASE_FLOOR_S,
            cap_s=movers_cache.MAX_ADAPTIVE_FLOOR_CAP_S,
        ) == min(max(2.0, 700.0), 600.0)

    def test_ledger_cache_cap_is_the_summary_cache_cap(self) -> None:
        """The ledger read cache deliberately takes the 120 s #920 cap, NOT the
        600 s movers default — its floor is not pull-probe-scoped, so the
        tighter cap bounds the worst-case served staleness (judge-2 graft)."""
        assert db_view._LEDGER_READ_FLOOR_CAP_S == summary_cache.MAX_ADAPTIVE_FLOOR_S == 120.0

    def test_revision_generation_moves_with_the_token(self) -> None:
        """The digest bridge: distinct tokens → distinct generations (with the
        documented ~2⁻⁶⁴ collision envelope); the same token is stable."""
        a = revision_generation("100.0.abc")
        assert a == revision_generation("100.0.abc")
        assert a != revision_generation("101.0.abc")
        assert 0 <= a < 2**64


_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The REFUTED freshness claim (DEC-LEDGER-DELTA-O-PAGE-001 fix round): the
#: adaptive floor ages ``entry.computed_at`` only — it is a TIME bound, never a
#: stamp-COUNT bound, so "at most one stamp stale" overstated freshness.
_REFUTED_STALENESS_CLAIM = "at most one stamp"

#: The accurate replacement wording every reworded surface must carry.
_ACCURATE_STALENESS_CLAIM = "possibly spanning several ≥2 s delta flushes"


class TestStaleServeManyStampsBehind:
    """Fix-round behavioral pin: the adaptive floor is TIME-bounded, never
    stamp-COUNTED (DEC-LEDGER-DELTA-O-PAGE-001 fix round).

    The refuted docstring wording claimed a floor-stale serve is "at most one
    stamp stale"; in reality the floor branch compares only the entry's age
    against ``min(max(2 s, its own parse duration), 120 s)``, so a slow parse's
    floor window spans MANY ≥2 s delta flushes and every read inside it serves
    the SAME prior entry — each serve logged. This pins the ACCURATE envelope
    the reworded docs now state (Golden Rule #10).
    """

    def test_slow_parse_serves_six_stamps_behind_within_the_time_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Six ≥2 s delta flushes inside one 13.6 s floor window (the measured
        841k-row parse shape) all serve the SAME warmed entry — six stamps
        behind by the last serve, six honesty logs — and the first read past
        the floor folds ALL six deltas fresh (self-healing, time-bounded)."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        real_load = load_window_ledger

        def _slow_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            clock["t"] += 13.6  # the parse itself "takes" the measured ~13.6 s
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _slow_load)
        warmed = _ledger_records_cached(tmp_path)  # computed_at = 13.6 → floor 13.6 s
        flushed = [f"SYM{i}" for i in range(1, 7)]
        with capture_logs() as logs:
            for i, symbol in enumerate(flushed, start=1):
                _append_delta(tmp_path, symbol)  # a FRESH stamp per flush
                clock["t"] = 13.6 + 2.0 * i  # the ≥2 s flush cadence, all < the floor
                # The SAME prior entry, now i stamps behind — never re-parsed.
                assert _ledger_records_cached(tmp_path) == warmed
        served = _events(logs, "movers_cache.stale_served")
        assert len(served) == 6  # EVERY multi-stamp-behind serve is logged
        clock["t"] = 13.6 + 13.7  # past min(max(2, 13.6), 120) → fresh parse
        refreshed = _ledger_records_cached(tmp_path)
        assert {r.security_id for r in refreshed} == {"NIFTY", *flushed}


#: The fix-round-2 REFUTED log-attribution claim: the derived docstrings said
#: every stale serve lands "on the ``ledger-records`` key" — the second-phase
#: serve (past the records floor, within the derived entry's OWN floor) logs
#: on the SERVING derived key (``ledger-rows`` / ``spot-rollup``).
_MISATTRIBUTED_LOG_CLAIM = "on the ``ledger-records`` key"


class TestComposedDerivedFloorHonesty:
    """Fix-round-2 pin: derived staleness COMPOSES two floors, logged per key.

    The round-3 docstrings confined derived staleness to the RECORDS adaptive
    floor — ONE ``min(max(2 s, the parse's own duration), 120 s)`` bound,
    "logged … on the ``ledger-records`` key". Reality (reproduced with the
    injectable clock): once the records floor passes and the records refresh,
    a derived projection (rows / rollup) ADDITIONALLY serves stale under its
    OWN #920 floor — driven by the projection's own compute duration — and
    THAT serve logs ``movers_cache.stale_served`` on the SERVING key
    (``ledger-rows`` / ``spot-rollup``), so worst-case derived staleness is
    records floor + derived floor (each capped at 120 s). The behaviour is
    sound (time-bounded, self-healing, honestly logged per serving key); this
    pins the ACCURATE envelope the reworded docs now state (Golden Rule #10).
    """

    def test_rows_serve_stale_under_their_own_floor_after_the_records_floor_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE composed-floor sequence: records parse 2 s (records floor 2 s),
        rows projection 10 s (rows floor 10 s), stamp move, poll PAST the
        records floor → the records read FRESH (post-flush truth) while the
        rows entry STILL serves the pre-flush projection under its own floor —
        logged on the ``ledger-rows`` key; past the composed bound the rows
        reconverge to on-disk truth."""
        clock = _install_clocked_cache(monkeypatch)
        _seed(tmp_path, ["NIFTY"])
        real_load = load_window_ledger

        def _timed_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            clock["t"] += 2.0  # the records parse "takes" 2 s → records floor 2 s
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _timed_load)
        from brutex.data import window_ledger as wl_mod

        real_row = wl_mod.ledger_record_row
        slow_once = {"armed": True}

        def _slow_row(record: Any) -> tuple[Any, ...]:
            if slow_once["armed"]:
                slow_once["armed"] = False
                clock["t"] += 10.0  # the rows projection "takes" 10 s → rows floor 10 s
            return real_row(record)

        monkeypatch.setattr("brutex.data.window_ledger.ledger_record_row", _slow_row)
        # Cold: records parse (t 0→2, floor 2 s), rows projection (t 2→12, floor 10 s).
        assert [r[1] for r in window_ledger_rows_cached(tmp_path)] == ["NIFTY"]
        _append_delta(tmp_path, "BANKNIFTY")  # the flush — the stamp moves
        # Poll PAST the records floor (records age 13 s > 2 s): the records
        # parse FRESH (t 15→17), then the rows entry — generation-mismatched
        # but only 5 s old against its OWN 10 s floor — serves STALE.
        clock["t"] = 15.0
        with capture_logs() as logs:
            stale_rows = window_ledger_rows_cached(tmp_path)
        assert {r.security_id for r in _ledger_records_cached(tmp_path)} == {
            "NIFTY",
            "BANKNIFTY",
        }  # the records ARE fresh — past their floor, delta folded in
        assert [r[1] for r in stale_rows] == ["NIFTY"]  # rows: the SECOND stale phase
        (served,) = _events(logs, "movers_cache.stale_served")
        assert str(served["key"]).startswith("ledger-rows|")  # the SERVING key, never records
        assert served["floor_s"] == 10.0  # the rows entry's OWN floor drives this phase
        assert served["age_s"] == 5.0  # within the rows floor, past the records floor
        assert served["entry_generation"] != served["generation"]
        # Self-healing: past the composed bound the rows reconverge fresh.
        clock["t"] = 40.0  # rows age 28 s > their 10 s floor
        with capture_logs() as logs:
            rows = window_ledger_rows_cached(tmp_path)
        assert sorted(r[1] for r in rows) == ["BANKNIFTY", "NIFTY"]
        assert list(rows) == load_window_ledger(tmp_path).to_rows()
        assert _events(logs, "movers_cache.stale_served") == []  # genuinely fresh

    @pytest.mark.parametrize(
        ("func", "family"),
        [(window_ledger_rows_cached, "ledger-rows"), (spot_window_rollup, "spot-rollup")],
        ids=["rows", "rollup"],
    )
    def test_derived_docstrings_state_the_composed_floor_and_serving_key(
        self, func: Any, family: str
    ) -> None:
        """Doc pin (fails-before/passes-after): the derived consumers' honesty
        paragraphs no longer misattribute every stale-serve log to the
        ``ledger-records`` key, and state the COMPOSED two-floor bound with
        their own serving key named."""
        doc = " ".join((func.__doc__ or "").split())  # whitespace-normalized
        assert _MISATTRIBUTED_LOG_CLAIM not in doc
        assert "COMPOSE" in doc  # the two-floor composition stated
        assert f"``{family}`` in the second" in doc  # the second-phase SERVING key named


class TestDocWordingHonesty:
    """Fix-round doc pins: the refuted claim is scrubbed everywhere it was
    stated (Golden Rule #10) and the ledger entry's Files citations resolve to
    real repo paths (Golden Rule #2 traceability)."""

    @pytest.mark.parametrize(
        "doc",
        [
            Path(db_view.__file__),
            _REPO_ROOT / "CLAUDE.md",
            _REPO_ROOT / "docs" / "MIGRATION.md",
        ],
        ids=["db_view", "claude_md", "migration_md"],
    )
    def test_refuted_one_stamp_claim_is_scrubbed_for_the_time_bound_wording(
        self, doc: Path
    ) -> None:
        """Each surface that carried the refuted stamp-count claim now states
        the accurate time-bound wording instead."""
        text = doc.read_text(encoding="utf-8")
        assert _REFUTED_STALENESS_CLAIM not in text
        assert _ACCURATE_STALENESS_CLAIM in text

    def test_migration_1528_files_list_paths_all_exist(self) -> None:
        """MIGRATION.md #1528's Files list cites only paths that exist — the
        fix-round citation defect was `tests/unit/test_lake_read.py` (never
        created; the real file is `tests/unit/test_data_lake_read.py`)."""
        text = (_REPO_ROOT / "docs" / "MIGRATION.md").read_text(encoding="utf-8")
        entry = next(
            line
            for line in text.splitlines()
            if line.startswith("1528. **DEC-LEDGER-DELTA-O-PAGE-001")
        )
        files_blob = entry.split("**Files:**", 1)[1].split("**Decision ID:**", 1)[0]
        cited = re.findall(r"`([^`]+)`", files_blob)
        assert "tests/unit/test_data_lake_read.py" in cited  # the corrected citation
        missing = [path for path in cited if not (_REPO_ROOT / path).exists()]
        assert missing == [], f"MIGRATION.md #1528 cites missing paths: {missing}"
