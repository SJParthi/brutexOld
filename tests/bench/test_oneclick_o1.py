"""Bench pins for the ``/api/v1/oneclick/*`` artifact reads (DEC-OPERATOR-PAGE-REAL-001).

Honest, narrow claims (Golden Rule #10) — NONE is a literal O(1)-total claim:

* the events tail is **O(page)** — ``seek(offset)`` + ≤ ``limit`` lines inside
  a 1 MiB window; the pin gates page-read flatness vs LOG SIZE, not vs the
  page's own ``limit`` (a bigger page costs proportionally more, by design);
* every cached-poll pin gates the PRIMED hit — the FIRST read per stat-token
  revision parses the **O(artifact)** file (top100/manifest/trades), and the
  manifest route's fresh compute ADDITIONALLY pays one run-dir probe plus one
  O(page) events tail (``offset=-1, limit=200``) for the chain-map links
  (F-M3/F-M6 honesty clauses); a repeat poll also pays one ``os.stat`` per
  folded artifact token (O(1) syscalls, included in the timed loops);
* the trades page is an O(limit) tuple slice AFTER the once-per-revision
  O(file) parse (the ``FixtureTradesReader`` single-entry memo, C-M3a);
* the per-run loaders read ONLY their own artifact file — flat vs the SIBLING
  results tree (C-M3b) — but stay O(artifact) in their own file's size.

Ceiling mechanisms, PER PIN (T-MINOR-3 — measured 2026-07-07 FIRST, sandbox
shared-CI class, then routed):

* B-1 events-tail flatness vs log size → :func:`ratio_ceiling` (8.0 shared —
  file open/seek I/O noise, the ``test_registry_pagination_o1.py`` precedent);
* B-2 primed runs/top100 cache-hit flatness vs run count →
  :func:`ratio_ceiling` (5.0 shared — pure dict hit, the
  ``test_db_option_count_cache_o1.py`` precedent) + deterministic
  ``_must_not_run`` no-recompute teeth (no timing);
* B-3 events EOF repeat-poll flatness (stat + cache hit per poll) →
  :func:`ratio_ceiling` (8.0 — one ``os.stat`` per poll is in the loop) +
  ``_must_not_run`` teeth;
* B-4 trades page-N flatness vs total rows → :func:`ratio_ceiling` (5.0 —
  tuple slice) + the deterministic ``parse_count`` no-reparse teeth;
* B-5 per-run loader flatness vs sibling-tree size → :func:`ratio_ceiling`
  (8.0 — same-size artifact parsed both sides, file I/O noise);
* B-6 manifest repeat-poll flatness vs run-history depth → :func:`ratio_ceiling`
  (8.0 — three ``os.stat`` tokens + one cache hit per poll) +
  ``_must_not_run`` teeth;
* B-7 verify repeat-poll flatness vs manifest size (F1,
  DEC-ONECLICK-PERF-CLUB-001 — the verify route was the ONE oneclick route
  with NO revision cache) → :func:`ratio_ceiling` (8.0 — three ``os.stat``
  tokens + a raising chain cache-peek + one cache hit per poll) +
  ``_must_not_run`` no-recompute teeth + the C-r2-5 outage-serve teeth.

Strict-tier verdict (recorded per DEC-O1-STRICT-TIER-001): under
``BRUTEX_M4PRO_STRICT=1`` every pin above tightens to the decreed 1.1× via
``ratio_ceiling``; the measured sandbox ratios (docstrings below, 0.98×–1.00×)
suggest the strict tier is satisfiable on quiet hardware for every pin, but no
strict-tier promise is made until a real M4-Pro run records its own numbers
(Golden Rule #10 — NOT claimed here).
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

pytest.importorskip("structlog", reason="the oneclick adapter logs via structlog")

from brutex.dashboard import oneclick_artifacts as oca
from brutex.dashboard import oneclick_view
from brutex.dashboard.movers_cache import MoversSnapshotCache

from tests.bench._o1_tiers import ratio_ceiling

_RID = "r-bench01"

#: Best-of-N-MIN idiom (DEC-CANARY-MINTIME-001): the minimum over repeats is
#: the least-jitter-contaminated estimate of the true per-call cost.
_TIMING_REPEATS = 15
_INNER_CALLS = 5

_SMALL_EVENT_LINES = 1_000
_LARGE_EVENT_LINES = 100_000

_SMALL_TRADE_ROWS = 1_000
_LARGE_TRADE_ROWS = 200_000


def _best_of(op: Callable[[], object]) -> float:
    """Best-of-N minimum wall time (ns) for ONE ``op()`` call."""
    best = float("inf")
    for _ in range(_TIMING_REPEATS):
        t0 = time.perf_counter_ns()
        for _ in range(_INNER_CALLS):
            op()
        t1 = time.perf_counter_ns()
        best = min(best, (t1 - t0) / _INNER_CALLS)
    return best


def _must_not_run() -> NoReturn:
    """A ``compute`` that fails if called — a primed hit must never recompute."""
    raise AssertionError("a primed oneclick cache hit must not re-run the O(artifact) compute")


def _manifest_obj(rid: str) -> dict[str, Any]:
    """A minimal contract-complete manifest object (all 9 required keys)."""
    return {
        "run_id": rid,
        "selection": {"symbols": ["NSE-NIFTY"], "tfs": ["5m"]},
        "estimate": {"trials": 0},
        "actual": {"enumerated": 0, "tested": 0, "skipped": 0, "funnel": []},
        "per_stage_timings": {},
        "per_door_kills": [],
        "dedup_skips": 0,
        "tripwires": [],
        "suspect": False,
    }


def _top100_rows(count: int) -> list[dict[str, Any]]:
    """*count* contract-complete leaderboard rows (all 10 required row keys)."""
    return [
        {
            "rank": i + 1,
            "mask_hex": f"0x{i:x}",
            "conditions": ["cond_a", "cond_b"],
            "tf": "5m",
            "direction": "long",
            "arm": "fixed",
            "metrics": {"sharpe": 1.0 + i / 1000.0},
            "regime": {},
            "vix_band": None,
            "family_id": f"fam-{i % 7}",
        }
        for i in range(count)
    ]


def _write_run(
    root: Path,
    rid: str,
    *,
    events_lines: int = 0,
    top100_rows: int = 100,
) -> Path:
    """Materialise ONE contract-shaped run dir under the Ruling-4 ``sweep_runs/<rid>``."""
    run_dir = oca.runs_root(root) / rid
    run_dir.mkdir(parents=True)
    (run_dir / oca.MANIFEST_FILENAME).write_text(json.dumps(_manifest_obj(rid)), encoding="utf-8")
    # The writer's OBJECT root (DEC-ONECLICK-TOP100-OBJECT-ROOT-001).
    top100_doc = {
        "schema_version": oca.TOP100_SCHEMA_VERSION,
        "run_id": rid,
        "preset": "min_dd_first",
        "source": {"pool": top100_rows},
        "baselines": {},
        "entries": _top100_rows(top100_rows),
    }
    (run_dir / oca.TOP100_FILENAME).write_text(json.dumps(top100_doc), encoding="utf-8")
    if events_lines:
        lines = "".join(
            json.dumps(
                {
                    "ts_utc": f"2026-07-07T10:{i % 60:02d}:00Z",
                    "run_id": rid,
                    "kind": "tuple_done",
                    "payload": {"i": i},
                }
            )
            + "\n"
            for i in range(events_lines)
        )
        (run_dir / oca.EVENTS_FILENAME).write_text(lines, encoding="utf-8")
    return run_dir


def _write_runs_tree(root: Path, *, runs: int, events_lines: int = 0) -> Path:
    """A results root holding *runs* sibling run dirs; returns the TARGET run dir."""
    target = _write_run(root, _RID, events_lines=events_lines)
    for i in range(runs - 1):
        _write_run(root, f"r-sib{i:04d}")
    return target


def _write_trades_root(root: Path, rows: int) -> Path:
    """A fixture root carrying ``sweep_runs/<rid>/trades/rank-0001.json`` with *rows* rows."""
    path = oca.trades_path(oca.fixture_results_root(root), _RID, 1)
    path.parent.mkdir(parents=True)
    payload = {"rows": [{"i": i, "pnl_paisa": i * 3} for i in range(rows)], "total": rows}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return root


class TestEventsTailOPageFlat:
    """B-1 — the events tail is O(page): flat vs log size (1k vs 100k lines)."""

    def test_tail_page_flat_vs_log_size(self, tmp_path: Path) -> None:
        """A 100× larger events.jsonl does NOT slow the tail-anchored page read.

        Measured 2026-07-07 (sandbox, shared-CI class): 662 µs/page @ 1k lines
        vs 656 µs/page @ 100k lines → ratio 0.99× (the tail seek skips the
        bulk; both pages parse the same ≤200 lines). Ceiling:
        ``ratio_ceiling(8.0)`` — file open/seek I/O noise (the
        ``test_registry_pagination_o1.py`` precedent).
        """
        small = _write_run(tmp_path / "small" / "sweeps", _RID, events_lines=_SMALL_EVENT_LINES)
        large = _write_run(tmp_path / "large" / "sweeps", _RID, events_lines=_LARGE_EVENT_LINES)

        def _page(run_dir: Path) -> oca.EventsPage:
            page = oca.tail_events(run_dir, offset=-1, limit=200)
            assert page.status == "ok"
            assert len(page.records) == 200
            return page

        _page(small), _page(large)  # warm the page cache / verify shape
        t_small = _best_of(lambda: _page(small))
        t_large = _best_of(lambda: _page(large))
        ratio = t_large / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(8.0), (
            f"events tail page scaled with log size: {ratio:.2f}x "
            f"({t_small / 1e3:.1f} µs @ {_SMALL_EVENT_LINES} vs "
            f"{t_large / 1e3:.1f} µs @ {_LARGE_EVENT_LINES} lines)"
        )


class TestPrimedCacheHitFlatVsRunCount:
    """B-2 — primed runs/top100 cache hits are O(1) vs run count (+ no-recompute teeth)."""

    @staticmethod
    def _primed_runs(root: Path) -> tuple[MoversSnapshotCache, str]:
        """Prime one runs-listing payload the way the route does (real compute once)."""
        cache = MoversSnapshotCache()
        rev = oca.runs_revision(root)
        assert rev is not None
        key = f"oc_runs|rev={rev}|fx=False"
        cache.get_or_compute(
            key,
            generation=0,
            compute=lambda: oneclick_view.build_runs_payload(
                oca.discover_runs(root), fixture_root_active=False
            ),
        )
        return cache, key

    def test_primed_runs_hit_flat_vs_run_count(self, tmp_path: Path) -> None:
        """A 50-run root's PRIMED listing poll costs the same as a 3-run root's.

        Measured 2026-07-07 (sandbox): 0.55 µs/hit @ 3 runs vs 0.54 µs/hit
        @ 50 runs → ratio 0.98×. The FIRST poll per ``runs/`` revision pays
        the real O(run dirs) ``discover_runs`` scan — stated, not gated here.
        SCALING NOTE (hostile-round F5): EVERY poll additionally pays the
        ``runs_revision`` key computation — O(run dirs) ``stat`` syscalls
        (dir listing + one manifest stat per run dir, never a parse). That
        probe is DELIBERATELY uncapped: ranking dirs by mtime to reuse the
        listing's newest-50 cap would cost N dir stats + 50 manifest stats,
        MORE than the N manifest stats it pays today (see the
        ``runs_revision`` docstring ruling). Only the primed payload HIT
        below is the O(1) dict lookup this bench pins.
        Ceiling: ``ratio_ceiling(5.0)`` — pure dict hit (the
        ``test_db_option_count_cache_o1.py`` precedent).
        """
        root_small = tmp_path / "small" / "sweeps"
        root_big = tmp_path / "big" / "sweeps"
        _write_runs_tree(root_small, runs=3)
        _write_runs_tree(root_big, runs=50)
        cache_small, key_small = self._primed_runs(root_small)
        cache_big, key_big = self._primed_runs(root_big)

        def _hit(cache: MoversSnapshotCache, key: str) -> dict[str, Any]:
            return cache.get_or_compute(key, generation=0, compute=_must_not_run)

        _hit(cache_small, key_small), _hit(cache_big, key_big)  # warm
        t_small = _best_of(lambda: _hit(cache_small, key_small))
        t_big = _best_of(lambda: _hit(cache_big, key_big))
        ratio = t_big / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(5.0), (
            f"primed runs-listing hit scaled with run count: {ratio:.2f}x"
        )

    def test_primed_hits_never_recompute(self, tmp_path: Path) -> None:
        """Deterministic teeth: primed runs AND top100 hits never re-run the compute."""
        root = tmp_path / "root" / "sweeps"
        run_dir = _write_runs_tree(root, runs=5)
        cache, runs_key = self._primed_runs(root)
        # The runs hit serves the cached payload with a raising compute.
        payload: dict[str, Any] = cache.get_or_compute(
            runs_key, generation=0, compute=_must_not_run
        )
        assert payload["status"] == "ok"
        assert _RID in {run["run_id"] for run in payload["runs"]}
        # Prime + hit the top100 payload the same way (the route's key shape).
        top_rev = oca.artifact_revision(run_dir / oca.TOP100_FILENAME)
        assert top_rev is not None
        top_key = f"oc_top100|{_RID}|rev={top_rev}|fx=False"
        primed = cache.get_or_compute(
            top_key,
            generation=0,
            compute=lambda: oneclick_view.build_top100_payload(
                _RID, oca.load_top100(run_dir), fixture_root_active=False
            ),
        )
        hit = cache.get_or_compute(top_key, generation=0, compute=_must_not_run)
        assert hit is primed
        assert hit["row_count"] == 100


class TestEventsEofRepeatPollFlat:
    """B-3 — a repeat events poll at EOF short-circuits via the stat-token cache."""

    @staticmethod
    def _primed_eof(run_dir: Path) -> tuple[MoversSnapshotCache, str, Path]:
        """Prime the EOF page under the route's json-encoded cache key shape."""
        events_path = run_dir / oca.EVENTS_FILENAME
        rev = oca.artifact_revision(events_path)
        assert rev is not None
        size = events_path.stat().st_size
        cache = MoversSnapshotCache()
        key = "oc_events|" + json.dumps([_RID, size, 200, None, None, rev, False])
        page = cache.get_or_compute(
            key,
            generation=0,
            compute=lambda: oca.tail_events(run_dir, offset=size, limit=200),
        )
        assert page.records == ()  # caught up — the EOF short-circuit precondition
        assert page.next_offset == size
        return cache, key, events_path

    def test_eof_repeat_poll_flat_vs_log_size(self, tmp_path: Path) -> None:
        """The caught-up poll (one stat + one cache hit) is flat vs log size.

        Measured 2026-07-07 (sandbox): 2.6 µs/poll @ 1k lines vs 2.6 µs/poll
        @ 100k lines → ratio 1.00× (the poll re-stats the file — O(1)
        syscall — then serves the cached empty page; ``tail_events`` never
        runs). Ceiling: ``ratio_ceiling(8.0)`` — an ``os.stat`` syscall sits
        inside the timed loop.
        """
        small = _write_run(tmp_path / "small" / "sweeps", _RID, events_lines=_SMALL_EVENT_LINES)
        large = _write_run(tmp_path / "large" / "sweeps", _RID, events_lines=_LARGE_EVENT_LINES)
        cache_s, key_s, path_s = self._primed_eof(small)
        cache_l, key_l, path_l = self._primed_eof(large)

        def _poll(cache: MoversSnapshotCache, key: str, path: Path) -> None:
            # The route's repeat-poll work: re-derive the stat token, then hit.
            assert oca.artifact_revision(path) is not None
            cache.get_or_compute(key, generation=0, compute=_must_not_run)

        _poll(cache_s, key_s, path_s), _poll(cache_l, key_l, path_l)  # warm
        t_small = _best_of(lambda: _poll(cache_s, key_s, path_s))
        t_large = _best_of(lambda: _poll(cache_l, key_l, path_l))
        ratio = t_large / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(8.0), f"EOF repeat poll scaled with log size: {ratio:.2f}x"


class TestTradesPageFlatVsTotalRows:
    """B-4 — a primed trades page is an O(limit) slice, flat vs total rows."""

    def test_trades_page_flat_vs_total_rows(self, tmp_path: Path) -> None:
        """Page N of a 200k-row artifact costs the same as page N of a 1k-row one.

        Measured 2026-07-07 (sandbox): 17.2 µs/page @ 1k rows vs 17.3 µs/page
        @ 200k rows → ratio 1.00×. HONEST: the FIRST page per stat-token
        revision parses the WHOLE file — O(file), the single-entry memo law
        (C-M3a) — and that parse is deliberately OUTSIDE the timed loop
        (primed below); the timed page is the memo hit + tuple slice +
        ``os.stat`` token check. Ceiling: ``ratio_ceiling(5.0)``.
        """
        reader_small = oca.FixtureTradesReader(
            _write_trades_root(tmp_path / "small", _SMALL_TRADE_ROWS)
        )
        reader_big = oca.FixtureTradesReader(
            _write_trades_root(tmp_path / "big", _LARGE_TRADE_ROWS)
        )

        def _page(reader: oca.FixtureTradesReader, total: int) -> oca.TradesResult:
            page = reader.page(_RID, 1, offset=total - 100, limit=100)
            assert page.status == "ok"
            assert len(page.rows) == 100
            assert page.total == total
            return page

        _page(reader_small, _SMALL_TRADE_ROWS)  # prime the per-revision memo
        _page(reader_big, _LARGE_TRADE_ROWS)
        t_small = _best_of(lambda: _page(reader_small, _SMALL_TRADE_ROWS))
        t_big = _best_of(lambda: _page(reader_big, _LARGE_TRADE_ROWS))
        ratio = t_big / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(5.0), (
            f"trades page scaled with total rows: {ratio:.2f}x "
            f"({t_small / 1e3:.1f} µs @ {_SMALL_TRADE_ROWS} vs "
            f"{t_big / 1e3:.1f} µs @ {_LARGE_TRADE_ROWS} rows)"
        )

    def test_repeat_pages_never_reparse(self, tmp_path: Path) -> None:
        """Deterministic teeth: 25 page clicks after priming = exactly ONE parse."""
        reader = oca.FixtureTradesReader(_write_trades_root(tmp_path / "root", _SMALL_TRADE_ROWS))
        reader.page(_RID, 1, offset=0, limit=100)
        assert reader.parse_count == 1
        for offset in range(0, 25 * 40, 40):
            page = reader.page(_RID, 1, offset=offset, limit=40)
            assert page.status == "ok"
        assert reader.parse_count == 1  # the memo held for every later page


class TestLoaderFlatVsSiblingTree:
    """B-5 — per-run loaders read ONLY their own file: flat vs a LARGE sibling tree."""

    def test_top100_and_manifest_flat_vs_sibling_tree(self, tmp_path: Path) -> None:
        """A 200-sibling results tree does NOT slow one run's top100/manifest read.

        The pre-P3 "flat vs stored-winner count" reading of C-M3b: the loaders
        open exactly ``runs/<rid>/{top100,manifest}.json`` — never a tree scan.
        Measured 2026-07-07 (sandbox): 254 µs vs 254 µs (top100 + manifest
        parsed back-to-back per call) → ratio 1.00×. HONEST: each call is a FRESH
        O(artifact) parse of the SAME-SIZE file on both sides (this pin gates
        tree-independence, not parse cost — the cached-poll pins gate that).
        Ceiling: ``ratio_ceiling(8.0)`` — real file I/O both sides.
        """
        target_small = _write_runs_tree(tmp_path / "small" / "sweeps", runs=3)
        target_big = _write_runs_tree(tmp_path / "big" / "sweeps", runs=200)

        def _reads(run_dir: Path) -> None:
            assert oca.load_top100(run_dir).status == "ok"
            assert oca.load_manifest(run_dir).status == "ok"

        _reads(target_small), _reads(target_big)  # warm
        t_small = _best_of(lambda: _reads(target_small))
        t_big = _best_of(lambda: _reads(target_big))
        ratio = t_big / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(8.0), (
            f"per-run loader scaled with sibling-tree size: {ratio:.2f}x"
        )


class TestManifestPollFlatVsHistory:
    """B-6 — the primed manifest poll is flat vs run-history depth (F-M6)."""

    @staticmethod
    def _primed_manifest(root: Path) -> tuple[MoversSnapshotCache, str, Path]:
        """Prime one manifest payload under the route's composite-token key shape."""
        run_dir = oca.runs_root(root) / _RID
        m_rev = oca.artifact_revision(run_dir / oca.MANIFEST_FILENAME)
        top_rev = oca.artifact_revision(run_dir / oca.TOP100_FILENAME)
        ev_rev = oca.artifact_revision(run_dir / oca.EVENTS_FILENAME)
        assert m_rev is not None
        cache = MoversSnapshotCache()
        key = f"oc_manifest|{_RID}|rev={m_rev}|files={top_rev}{ev_rev}|ev={ev_rev}|fx=False"

        def _fresh() -> dict[str, Any]:
            # The route's fresh compute: manifest parse + run-dir probe + one
            # O(page) events tail feeding chain_links — O(artifact), paid ONCE
            # per revision (the honesty clause this pin's docstring states).
            manifest = oca.load_manifest(run_dir)
            payload = oneclick_view.build_manifest_payload(
                _RID,
                manifest,
                fixture_root_active=False,
                info=oca.probe_run_dir(run_dir),
            )
            recent = oca.tail_events(run_dir, offset=-1, limit=200)
            kinds = [str(r.get("kind", "")) for r in recent.records]
            payload["chain_links"] = oneclick_view.chain_links(manifest, kinds)
            return payload

        cache.get_or_compute(key, generation=0, compute=_fresh)
        return cache, key, run_dir

    def test_manifest_repeat_poll_flat_vs_history_depth(self, tmp_path: Path) -> None:
        """A 100-run history does NOT slow the primed manifest poll of one run.

        Measured 2026-07-07 (sandbox): 26.3 µs/poll @ 5 runs vs 26.4 µs/poll
        @ 100 runs → ratio 1.00× (per poll: three ``os.stat`` artifact tokens
        + one cache hit — the route's repeat-poll work). HONEST: the FIRST
        poll per changed manifest pays the O(artifact) parse + probe + the
        O(page, limit=200) events tail for the chain map — stated per
        F-M6/F-M3, deliberately outside the timed loop. Ceiling:
        ``ratio_ceiling(8.0)``.
        """
        root_small = tmp_path / "small" / "sweeps"
        root_big = tmp_path / "big" / "sweeps"
        _write_runs_tree(root_small, runs=5, events_lines=_SMALL_EVENT_LINES)
        _write_runs_tree(root_big, runs=100, events_lines=_SMALL_EVENT_LINES)
        cache_s, key_s, dir_s = self._primed_manifest(root_small)
        cache_l, key_l, dir_l = self._primed_manifest(root_big)

        def _poll(cache: MoversSnapshotCache, key: str, run_dir: Path) -> None:
            # Re-derive the three stat tokens exactly like the route, then hit.
            assert oca.artifact_revision(run_dir / oca.MANIFEST_FILENAME) is not None
            oca.artifact_revision(run_dir / oca.TOP100_FILENAME)
            oca.artifact_revision(run_dir / oca.EVENTS_FILENAME)
            cache.get_or_compute(key, generation=0, compute=_must_not_run)

        _poll(cache_s, key_s, dir_s), _poll(cache_l, key_l, dir_l)  # warm
        t_small = _best_of(lambda: _poll(cache_s, key_s, dir_s))
        t_large = _best_of(lambda: _poll(cache_l, key_l, dir_l))
        ratio = t_large / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(8.0), (
            f"manifest repeat poll scaled with run-history depth: {ratio:.2f}x"
        )

    def test_primed_manifest_poll_never_recomputes(self, tmp_path: Path) -> None:
        """Deterministic teeth: the primed manifest key never re-runs the compute."""
        root = tmp_path / "root" / "sweeps"
        _write_runs_tree(root, runs=5, events_lines=100)
        cache, key, _run_dir = self._primed_manifest(root)
        payload: dict[str, Any] = cache.get_or_compute(key, generation=0, compute=_must_not_run)
        assert payload["run_id"] == _RID
        assert len(payload["chain_links"]) == 8


def _scaled_verify_manifest(rid: str, doors: int) -> dict[str, Any]:
    """A contract-complete manifest whose funnel/per_door_kills scale with *doors*.

    Reconciles to the digit (``stage_n == stage_{n-1} − kills_n`` and
    ``enumerated == tested + skipped``) so the verify FRESH compute pays the
    real O(manifest) reconcile work.
    """
    total = doors * 10
    funnel: list[dict[str, Any]] = [{"stage": "enumerated", "count": total}]
    kills: list[dict[str, Any]] = []
    remaining = total
    for i in range(doors):
        kills.append({"door": f"door-{i:05d}", "killed": 1})
        remaining -= 1
        funnel.append({"stage": f"stage-{i:05d}", "count": remaining})
    return {
        "run_id": rid,
        "selection": {"symbols": ["NSE-NIFTY"], "tfs": ["5m"]},
        "estimate": {"trials": total},
        "actual": {
            "enumerated": total,
            "tested": remaining,
            "skipped": total - remaining,
            "funnel": funnel,
        },
        "per_stage_timings": {},
        "per_door_kills": kills,
        "dedup_skips": 0,
        "tripwires": [],
        "suspect": False,
    }


class TestVerifyPollFlatVsManifestSize:
    """B-7 — the primed verify poll is flat vs manifest size (F1, DEC-ONECLICK-PERF-CLUB-001)."""

    @staticmethod
    def _primed_verify(root: Path, doors: int) -> tuple[MoversSnapshotCache, oca.OutageGate, Path]:
        """Prime one verify payload under the route's composite-token key shape."""
        run_dir = _write_run(root, _RID, events_lines=200)
        (run_dir / oca.MANIFEST_FILENAME).write_text(
            json.dumps(_scaled_verify_manifest(_RID, doors)), encoding="utf-8"
        )
        cache = MoversSnapshotCache()
        gate = oca.OutageGate()

        def _fresh() -> dict[str, Any]:
            manifest = oca.load_manifest(run_dir)
            return oneclick_view.build_verify_payload(
                _RID, manifest, None, False, fixture_root_active=False
            )

        TestVerifyPollFlatVsManifestSize._poll(cache, gate, run_dir, _fresh)
        return cache, gate, run_dir

    @staticmethod
    def _poll(
        cache: MoversSnapshotCache,
        gate: oca.OutageGate,
        run_dir: Path,
        compute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """One verify poll exactly like the route: 3 stats (siblings through
        the r1 MINOR-1 outage-doctrine gate) + chain peek + serve."""
        probe = oca.artifact_probe(run_dir / oca.MANIFEST_FILENAME)
        ev_rev = oca.sibling_key_token(
            gate,
            coordinate=f"oc_verify|{_RID}|sib=events|fx=False",
            probe=oca.artifact_probe(run_dir / oca.EVENTS_FILENAME),
        )
        top_rev = oca.sibling_key_token(
            gate,
            coordinate=f"oc_verify|{_RID}|sib=top100|fx=False",
            probe=oca.artifact_probe(run_dir / oca.TOP100_FILENAME),
        )
        with contextlib.suppress(oca.ChainNotVerifiedError):
            cache.get_or_compute(
                f"oc_chain|{_RID}|rev={ev_rev}",
                generation=0,
                compute=oca.raise_chain_not_verified,
            )
        key = f"oc_verify|{_RID}|rev={probe.token}|ev={ev_rev}|t100={top_rev}|chain=0|fx=False"
        return oca.serve_revision_cached(
            cache,
            gate,
            coordinate=f"oc_verify|{_RID}|fx=False",
            probe=probe,
            key=key,
            compute=compute,
        )

    def test_verify_repeat_poll_flat_vs_manifest_size(self, tmp_path: Path) -> None:
        """A 1000x-doors manifest does NOT slow the primed verify poll.

        Measured 2026-07-10 (sandbox): 26.5 µs/poll @ 4 doors vs 25.2 µs/poll
        @ 4000 doors → ratio 0.95× flat, while the big manifest's FRESH
        verify compute measured 6.89 ms — the pre-F1 route paid THAT per
        poll (no revision cache: load_manifest + reconcile_funnel per
        request). HONEST: the FIRST poll per changed (manifest,
        events, top100, chain-state) revision pays the O(manifest) parse +
        funnel reconcile + the one-row trades probe — stated in the route
        docstring, deliberately outside the timed loop; each timed poll pays
        three ``os.stat`` tokens (+ O(1) outage-gate bookkeeping on the two
        sibling probes — r1 MINOR-1), one raising chain cache-peek, and one
        cache hit. Ceiling: ``ratio_ceiling(8.0)`` (file-stat I/O noise
        class — the B-6 precedent).
        """
        cache_s, gate_s, dir_s = self._primed_verify(tmp_path / "small" / "sweeps", doors=4)
        cache_l, gate_l, dir_l = self._primed_verify(tmp_path / "big" / "sweeps", doors=4000)
        self._poll(cache_s, gate_s, dir_s, _must_not_run)  # warm
        self._poll(cache_l, gate_l, dir_l, _must_not_run)
        t_small = _best_of(lambda: self._poll(cache_s, gate_s, dir_s, _must_not_run))
        t_large = _best_of(lambda: self._poll(cache_l, gate_l, dir_l, _must_not_run))
        ratio = t_large / t_small if t_small > 0 else 1.0
        assert ratio < ratio_ceiling(8.0), (
            f"verify repeat poll scaled with manifest size: {ratio:.2f}x"
        )

    def test_primed_verify_poll_never_recomputes(self, tmp_path: Path) -> None:
        """Deterministic teeth: the primed verify key never re-runs the compute."""
        cache, gate, run_dir = self._primed_verify(tmp_path / "root" / "sweeps", doors=4)
        payload = self._poll(cache, gate, run_dir, _must_not_run)
        assert payload["run_id"] == _RID
        assert payload["funnel_reconcile"]["ok"] is True
        assert len(payload["checks"]) == 7

    def test_outage_poll_serves_last_known_without_compute(self, tmp_path: Path) -> None:
        """Deterministic teeth (C-r2-5): an outage poll never re-parses either."""
        cache, gate, run_dir = self._primed_verify(tmp_path / "root" / "sweeps", doors=4)
        primed = self._poll(cache, gate, run_dir, _must_not_run)
        served = oca.serve_revision_cached(
            cache,
            gate,
            coordinate=f"oc_verify|{_RID}|fx=False",
            probe=oca.ArtifactProbe(None, True),
            key=None,
            compute=_must_not_run,
        )
        assert served == primed
