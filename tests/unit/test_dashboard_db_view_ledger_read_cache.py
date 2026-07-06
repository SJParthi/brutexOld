"""Single-flight + adaptive floor on the ledger record caches (DEC-LEDGER-DELTA-O-PAGE-001, sub-part b).

The three db_view module dict caches (``_WINDOW_CACHE`` / ``_WINDOW_ROWS_CACHE``
/ ``_SPOT_ROLLUP_CACHE``) collapsed onto ONE reused
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`
(``db_view._LEDGER_READ_CACHE``), keyed per family at a generation digested from
the composite ledger stamp token
(:func:`~brutex.dashboard.lake_revision.revision_generation`). This suite pins:

* T-b1  8 concurrent cold-stamp pollers pay EXACTLY ONE ledger parse
        (per-key single-flight — the concurrent ~13.6 s duplicate-parse bug);
* T-b2  the nested rows→records compute (two key families, one instance)
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
        cap delegate — CLAUDE.md §11 COMMON).

Tests MAY import ``window_ledger_store`` (the module itself must not — the
pinned no-import edge, verified by ``test_db_view_no_window_ledger_store_import``
in the fold suite).
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
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
        """T-b2: the rows compute re-enters the SAME cache instance under a
        DIFFERENT key (``ledger-rows`` → ``ledger-records``) — per-key
        single-flight, so the nested compute never self-deadlocks."""
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
        honesty log; past the floor the same read parses fresh."""
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
        assert str(served["key"]).startswith("spot-rollup|")
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
