"""Bench pins for the mid-pull ledger delta fold + the /results scan split (DEC-LEDGER-DELTA-O-PAGE-001).

Honest, narrow claims (Golden Rule #10) — NONE is a literal O(1)-total claim:

* mid-pull, the folded window-ledger source is ONE **O(total physical rows,
  incl. every delta file's Parquet footer)** columnar ``QUALIFY``
  hash-partition per FRESH stamp — NOT literal O(page); repeat polls WITHIN
  one stamp are stamp-generation cache hits for the counts AND (since
  DEC-WINDOWS-MIDPULL-FLOOR-001) the row PAGE (the flatness these pins gate),
  leaving the honest O(pending-delta) stamp-probe directory listing per poll;
  the O(delta-file-count) footer-open term is MEASURED and documented (B-4
  fresh-fold growth, B-8 accumulation regime), with delta compaction NAMED
  future work;
* the FIRST Python ledger parse per stamp stays **O(rows)** — sub-part (b)
  bounds only the duty cycle (``min(max(2 s, the parse's own duration),
  120 s)``, the #920 law) and the concurrency (single-flight: one parse per
  cold stamp, however many pollers);
* the /results probe is **O(sweep dirs) + O(1 audit stat)** per poll; the
  cold scan is O(stores × rows) + O(audit lines), paid once per
  ``sweep_results_revision`` — the absolute bound here carries generous
  measured slack, and the caching proof rides deterministic spies/sentinels
  so the timing assertions can stay loose on shared CI.

Ceiling mechanisms, PER PIN (not one shared tier): the phase-progress flatness
ratio (B-3) routes through :func:`tests.bench._o1_tiers.ratio_ceiling` (the
DuckDB-I/O-noisy 8.0 tier — the ``test_registry_pagination_o1.py`` precedent;
strict 1.1 only under ``BRUTEX_M4PRO_STRICT=1``); the window-payload depth
ratio (B-1) routes through :func:`tests.bench._o1_tiers.cache_tier_ceiling`
(8.0 shared / strict **3.0**, NOT 1.1 — its 10× workload legitimately scans
10× the columnar rows, so the strict 1.1 tier is physically unsatisfiable
there, the DEC-O1-CACHE-TIER-001 discipline); the fresh-fold delta-file growth
pin (B-4, ``< 20.0×``), the cold-scan bound (B-6, ``< 2 s`` mean), the
accumulation-regime bounds (B-8: ``< 100 ms`` absolute AND a marginal
``< 250 µs`` per pending delta — a SLOPE, not a ratio, because the two
fixtures' 8× spread caps any ratio from above; DEC-BENCH-SLOPE-BOUND-001) and
the production-scale fresh-fold bound (B-9, ``< 8 s`` @ ≥500k base rows,
DEC-DB-OPAGE-HONESTY-001) are ABSOLUTE measured-slack ceilings, deliberately
not tier-routed; B-2/B-5/B-7 are deterministic spy/sentinel pins with no timing
ratio at all.

B-9 (DEC-DB-OPAGE-HONESTY-001) is the honest production-scale companion to B-1: the fresh window-ledger
fold GROUP BY is an **O(total physical rows)** columnar QUALIFY hash-partition,
so at ≥500k base rows (approaching the disclosed ~841k full-universe worst case,
DEC-SUSPICION-SWEEP-FIXES-001 #1492) it is NOT flat vs a 10k base — measured
2026-07-08 (sandbox) 11.8 ms @ 10k vs 230.1 ms @ 500k → 19.52× for 50× the
rows — bounded but genuinely O(rows). Golden Rule #10: the fold is never O(page).
"""

from __future__ import annotations

# Whitebox bench pins read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import pytest

pytest.importorskip("duckdb", reason="the ledger fold fast path reads Parquet via DuckDB")
pytest.importorskip("pyarrow", reason="the ledger store writes Parquet via pyarrow")

import duckdb
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _ledger_scan_source,
    _window_counts_total,
    build_window_payload,
    phase_progress_payload,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling

_SMALL = 1_000
_LARGE = 10_000
_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

#: B-8's two accumulation fixtures. Their SPREAD (400/50 = 8.0) is the exact
#: upper bound of the warm-poll wall-clock RATIO whenever the honest
#: O(pending deltas) stamp-probe listing dominates the per-poll constant term —
#: so a ratio ceiling AT 8.0 had ZERO margin by construction (it could only
#: pass while some box's constant term happened to be large). Measured
#: 2026-07-25 on the SAME code, no regression between them:
#:   this container   t50=1.5 ms  t400=4.5 ms  → ratio 2.40-3.44× (8 trials)
#:   shared CI runner t50=1.31 ms t400=10.46 ms → ratio 8.00×  (FAILED at 8.0)
#: and the ratio is additionally NON-DISCRIMINATING: the pre-fix per-poll
#: re-fold regime measures 76.9 ms @ 50 → 443.1 ms @ 400 = ratio 5.76×, which
#: sits INSIDE the post-fix range — i.e. the old gate would have PASSED the very
#: regression its own message names (the docstring already admitted "a RATIO
#: cannot discriminate here"). It is replaced below by the per-delta SLOPE.
_ACC_SMALL_DELTAS = 50
_ACC_LARGE_DELTAS = 400

#: Ceiling on the MARGINAL warm-poll cost per pending delta — the SLOPE of the
#: O(pending deltas) stamp-probe directory listing, which is what the residual
#: per-poll term actually is. Unlike the ratio this is NOT capped by the fixture
#: spread, and it genuinely separates the regimes (measured 2026-07-25):
#:   post-fix, this container   6.4-10.8 µs/delta (8 trials)
#:   post-fix, shared CI runner 26.1 µs/delta
#:   PRE-FIX per-poll re-fold  1046   µs/delta   ← 40× above the worst post-fix
#: 250 µs/delta leaves ~9.6× headroom over the worst post-fix reading while the
#: pre-fix regime still fails it by 4.2×. A regression to a per-poll re-fold —
#: or any term that grows faster than the listing — blows it.
_MAX_MARGINAL_US_PER_DELTA = 250.0


def _key(i: int) -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=f"SYM{i}",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _seeded_lake(root: Path, *, depth: int, deltas: int, tag: str) -> Path:
    """A REAL lake: *depth* base rows via ``save_window_ledger``, then *deltas*
    pending delta files via ``append_ledger_delta`` with OVERLAPPING keys — so
    the widened fast-path gate condition (pending deltas) is genuinely TRUE and
    the SQL fold performs real latest-wins work per delta."""
    lake = root / tag
    ledger = WindowLedger()
    for i in range(depth):
        ledger.record(_key(i), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    wls.save_window_ledger(ledger, lake)
    for bump in range(deltas):
        for i in range(10):  # the same 10 keys re-dirtied per delta — overlap by design
            ledger.record(
                _key(i), raw_bars=5 + bump, error_type=None, bars_appended=5 + bump, now=_NOW
            )
        appended = wls.append_ledger_delta(ledger, lake)
        assert appended == 10
    assert len(wls.list_ledger_deltas(lake)) == deltas
    return lake


_SentinelT = TypeVar("_SentinelT")


class _SentinelCountCache:
    """A :class:`WindowCountsCache` that COUNTS real recomputes PER KEY FAMILY
    (``window-counts`` / ``window-counts-since`` / ``window-page`` — the
    page-row family joined the cache in DEC-WINDOWS-MIDPULL-FLOOR-001) and,
    once ``frozen``, RAISES if any poll tries to recompute ANY family — the
    deterministic no-recompute teeth (the ``test_summary_cache_o1.py``
    sentinel pattern). *inner* injects the concrete cache (default: a plain
    un-floored :class:`MoversSnapshotCache`; B-8's floor teeth inject the
    production-shaped floored instance)."""

    def __init__(self, inner: MoversSnapshotCache | None = None) -> None:
        self._inner = inner if inner is not None else MoversSnapshotCache()
        self.computes_by_family: dict[str, int] = {}
        self.frozen = False

    @property
    def count_computes(self) -> int:
        return self.computes_by_family.get("window-counts", 0)

    @property
    def page_computes(self) -> int:
        return self.computes_by_family.get("window-page", 0)

    def get_or_compute(
        self,
        key: str,
        *,
        generation: int,
        compute: Callable[[], _SentinelT],
    ) -> _SentinelT:
        family = key.split("|", 1)[0]

        def _wrapped() -> _SentinelT:
            if self.frozen:
                raise AssertionError(f"stamp unchanged — the {family} query must not re-run")
            self.computes_by_family[family] = self.computes_by_family.get(family, 0) + 1
            return compute()

        return self._inner.get_or_compute(key, generation=generation, compute=_wrapped)


def test_midpull_window_payload_flat_vs_ledger_depth(tmp_path: Path) -> None:
    """B-1: repeat mid-pull ``/coverage/windows`` PAGE polls stay flat vs ledger depth.

    With 5 pending deltas (the (a) SQL fold path genuinely exercised — gate TRUE
    via ``list_ledger_deltas``), repeat one-page (``limit=500``)
    ``build_window_payload`` polls WITHIN one stamp at 1k vs 10k base rows: the
    counts AND (since DEC-WINDOWS-MIDPULL-FLOOR-001) the page rows are both
    composite-stamp cache hits, so the within-stamp poll pays only the O(pending
    deltas) stamp-probe listings + cache handoffs. HONEST envelope: each FRESH
    stamp still pays the **O(total-rows) columnar** scan of the QUALIFY fold
    (plus one footer open per delta file) per cached family — NOT literal
    O(page) (the same envelope ``query_candles`` already accepts).
    Ceiling: :func:`cache_tier_ceiling` (8.0 shared / 3.0 strict) — the
    pre-page-cache measurement legitimately scanned 10× the columnar data per
    poll (the decreed 1.1× is physically unsatisfiable for an admitted
    O(total-rows) scan — the DEC-O1-CACHE-TIER-001 discipline), and the ceiling
    is deliberately KEPT after the page cache made the ratio flatter (a
    regression back to per-poll scanning must still fail it). Measured
    2026-07-06 pre-page-cache (sandbox, shared-CI class): 14.18 ms/poll @ 1k vs
    17.58 ms/poll @ 10k → ratio 1.24×.
    """

    def _time(depth: int, tag: str) -> float:
        lake = _seeded_lake(tmp_path, depth=depth, deltas=5, tag=tag)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()

        def _poll() -> None:
            payload = build_window_payload(
                lake, conn=conn, count_cache=cache, page_cache=cache, generation=0, limit=500
            )
            assert payload["source"] == "ledger"
            assert payload["count"] == depth  # the fold served every distinct key
            assert payload["returned"] == 500  # one real page projected

        for _ in range(5):  # warm: page cache + the one real GROUP BY per stamp
            _poll()
        iterations = 20
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            _poll()
        t1 = time.perf_counter_ns()
        conn.close()
        return (t1 - t0) / iterations

    t_small = _time(_SMALL, "small")
    t_large = _time(_LARGE, "large")
    ratio = t_large / max(t_small, 1.0)
    assert ratio < cache_tier_ceiling(8.0), (
        f"mid-pull window-payload repeat-poll ratio {ratio:.2f}× (10k vs 1k rows, 5 deltas, "
        f"one 500-row page) exceeds the ceiling — the within-stamp poll may have regressed "
        f"to a per-poll Python re-fold. t_1k={t_small / 1e6:.2f}ms, t_10k={t_large / 1e6:.2f}ms."
    )


def test_midpull_counts_no_recompute_within_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-2: ONE counts ``GROUP BY`` + ONE page query per ledger stamp — ZERO Python parses.

    The composite stamp token's teeth (stamp-DIGEST generation on stable keys
    since DEC-WINDOWS-MIDPULL-FLOOR-001; the page-row family joined the cache
    there too): 20 repeat polls at one stamp are pure cache hits for BOTH
    families (the frozen sentinel RAISES on any recompute); ONE real
    ``append_ledger_delta`` moves the token → exactly ONE recount + ONE page
    re-query. A spy on ``window_ledger_store.load_window_ledger`` proves the
    fast path performs ZERO Python ledger parses mid-pull (the O(records) fold
    it replaced).
    """
    lake = _seeded_lake(tmp_path, depth=_SMALL, deltas=3, tag="lake")
    ledger = WindowLedger()  # a writer-side handle for the mid-test delta append
    parses = {"n": 0}
    real_load = wls.load_window_ledger

    def _counting_load(lake_root: Path, **kwargs: Any) -> WindowLedger:
        parses["n"] += 1
        return real_load(lake_root, **kwargs)

    monkeypatch.setattr(wls, "load_window_ledger", _counting_load)
    conn = duckdb.connect()
    cache = _SentinelCountCache()
    first = build_window_payload(lake, conn=conn, count_cache=cache, page_cache=cache, generation=0)
    assert first["source"] == "ledger"
    assert cache.count_computes == 1
    assert cache.page_computes == 1
    cache.frozen = True  # any further recompute now raises
    for _ in range(20):
        repeat = build_window_payload(
            lake, conn=conn, count_cache=cache, page_cache=cache, generation=0
        )
        assert repeat["counts"] == first["counts"]
        assert repeat["windows"] == first["windows"]
    assert cache.count_computes == 1  # 20 polls, zero recounts
    assert cache.page_computes == 1  # …and zero page re-queries
    # ONE real delta append → the stamp token moves → exactly ONE recount + page.
    cache.frozen = False
    ledger.record(_key(0), raw_bars=99, error_type=None, bars_appended=99, now=_NOW)
    assert wls.append_ledger_delta(ledger, lake) == 1
    build_window_payload(lake, conn=conn, count_cache=cache, page_cache=cache, generation=0)
    assert cache.count_computes == 2
    assert cache.page_computes == 2
    cache.frozen = True
    build_window_payload(
        lake, conn=conn, count_cache=cache, page_cache=cache, generation=0
    )  # hit again
    conn.close()
    assert parses["n"] == 0, "the SQL fast path must never fall back to a Python ledger parse"


def test_midpull_phase_progress_flat_vs_depth(tmp_path: Path) -> None:
    """B-3: the pull-progress poll (the widgets' 600 ms/3 s tick) is FLAT vs depth.

    ``phase_progress_payload`` polls with 5 pending deltas at 1k vs 10k base
    rows within one stamp must stay under ``ratio_ceiling(8.0)`` — the counts
    ride the SAME composite-stamp-token cache entry the window payload primes,
    so a repeat poll is an O(1) hit (no page query on this route at all).
    HONEST: the first poll per stamp pays the O(total-rows + delta footers)
    fold GROUP BY. Measured 2026-07-06 (sandbox): 62.3 µs/poll @ 1k vs
    62.5 µs/poll @ 10k → ratio 1.00× (pure cache-hit handoff).
    """

    def _time(depth: int, tag: str) -> float:
        lake = _seeded_lake(tmp_path, depth=depth, deltas=5, tag=tag)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()
        path = wls.ledger_path(lake)

        def _poll() -> None:
            rows = phase_progress_payload(
                conn, path, current_phase="spot", count_cache=cache, generation=0
            )
            assert rows  # a seeded ledger always projects phase rows

        for _ in range(5):
            _poll()
        iterations = 200
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            _poll()
        t1 = time.perf_counter_ns()
        conn.close()
        return (t1 - t0) / iterations

    t_small = _time(_SMALL, "small")
    t_large = _time(_LARGE, "large")
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(8.0), (
        f"mid-pull phase-progress repeat-poll ratio {ratio:.2f}× (10k vs 1k rows, 5 deltas) "
        f"exceeds the ceiling — the poll may have regressed to a per-tick re-fold. "
        f"t_1k={t_small / 1e3:.1f}µs, t_10k={t_large / 1e3:.1f}µs."
    )


def test_delta_fold_cost_scales_with_files_not_polls(tmp_path: Path) -> None:
    """B-4: the fold's O(delta-file-count) term hits the FRESH fold, never the poll.

    Fixed 1k-row base, 5 vs 50 pending delta files. (i) Repeat polls stay pure
    cache hits at BOTH delta counts (the frozen sentinel raises on any
    recompute) — the file-count term never re-hits within a stamp. (ii) The
    FRESH fold's cost genuinely grows with the delta file count (one Parquet
    footer open per file, Golden Rule #10 stated not hidden): measured
    2026-07-06 (sandbox) 5.77 ms @ 5 deltas vs 14.09 ms @ 50 → growth 2.44× for
    10× the files (the footer term rides on the fixed query cost). The pin
    allows generous shared-CI slack (< 20×) and exists so a regression to
    super-linear per-file work fails loudly; a multi-hour pull accumulates
    deltas until the pull-end save subsumes them — delta COMPACTION is the
    named future bound, NOT claimed here.
    """
    lake5 = _seeded_lake(tmp_path, depth=_SMALL, deltas=5, tag="d5")
    lake50 = _seeded_lake(tmp_path, depth=_SMALL, deltas=50, tag="d50")
    conn = duckdb.connect()
    # (i) repeat-poll flatness: pure hits (counts AND page) at both delta counts.
    for lake in (lake5, lake50):
        cache = _SentinelCountCache()
        build_window_payload(lake, conn=conn, count_cache=cache, page_cache=cache, generation=0)
        cache.frozen = True
        for _ in range(10):
            build_window_payload(lake, conn=conn, count_cache=cache, page_cache=cache, generation=0)
        assert cache.count_computes == 1
        assert cache.page_computes == 1

    # (ii) the fresh-fold (uncached GROUP BY over the QUALIFY fold) cost per
    # delta count — measured + bounded, never hidden.
    def _fresh(lake: Path) -> float:
        path = wls.ledger_path(lake)
        source = _ledger_scan_source(path)
        for _ in range(3):
            _window_counts_total(conn, source)
        iterations = 10
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            _window_counts_total(conn, source)
        t1 = time.perf_counter_ns()
        return (t1 - t0) / iterations

    t5 = _fresh(lake5)
    t50 = _fresh(lake50)
    growth = t50 / max(t5, 1.0)
    conn.close()
    assert growth < 20.0, (
        f"fresh-fold cost grew {growth:.2f}× for 10× the delta files (measured 2.44× on "
        f"2026-07-06) — the per-file footer term may have regressed to super-linear work. "
        f"t_5={t5 / 1e6:.2f}ms, t_50={t50 / 1e6:.2f}ms."
    )


def test_ledger_record_cache_single_flight_no_duplicate_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-5: 8 concurrent pollers on a COLD stamp pay exactly ONE ledger parse.

    The sub-part (b) teeth: ``_ledger_records_cached`` rides the shared
    ``MoversSnapshotCache`` single-flight, so a concurrent stamp-miss storm
    (the measured ~13.6 s duplicate-parse bug) collapses to one
    ``load_window_ledger`` call; every waiter gets the identical tuple. The
    spy sleeps inside the parse to hold the race window open — without
    single-flight all 8 threads would parse.
    """
    lake = _seeded_lake(tmp_path, depth=_SMALL, deltas=2, tag="lake")
    monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
    parses = {"n": 0}
    real_load = wls.load_window_ledger

    def _slow_counting_load(lake_root: Path, **kwargs: Any) -> WindowLedger:
        parses["n"] += 1
        time.sleep(0.05)  # hold the cold-stamp window open for the whole pack
        return real_load(lake_root, **kwargs)

    monkeypatch.setattr(wls, "load_window_ledger", _slow_counting_load)
    barrier = threading.Barrier(8)
    results: list[tuple[Any, ...]] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=10.0)
            results.append(db_view._ledger_records_cached(lake))
        except Exception as exc:  # surfaced to the main thread's assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert not errors
    assert parses["n"] == 1, f"single-flight broken — {parses['n']} parses for one cold stamp"
    assert len(results) == 8
    assert all(r == results[0] for r in results)


def _result_row(seed: int, *, instrument: str, tf: str, net_pnl_paisa: int) -> Any:
    from brutex.engine.results_store import ResultRow

    return ResultRow(
        run_id=f"{seed:064x}",
        instrument=instrument,
        tf=tf,
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=float(seed % 7) + 0.5,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _seeded_sweeps(lake_root: Path) -> list[Any]:
    """12 REAL per-tuple stores (3 instruments × 4 TFs, 25 rows each) + an audit
    chain carrying ~200 real ``SWEEP_START`` entries via ``AuditWriter``."""
    from brutex.engine.results_store import ResultsStore
    from brutex.orchestrator.audit import AuditWriter

    all_rows: list[Any] = []
    seed = 0
    for instrument in ("NIFTY", "BANKNIFTY", "SENSEX"):
        for tf in ("1m", "5m", "15m", "1d"):
            rows = []
            for _ in range(25):
                seed += 1
                rows.append(
                    _result_row(seed, instrument=instrument, tf=tf, net_pnl_paisa=seed * 100 + 7)
                )
            dest = lake_root / "sweeps" / instrument / tf / f"bull_k1_{'a' * 16}.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            store = ResultsStore(path=dest)
            store.extend(rows)
            store.flush()
            all_rows.extend(rows)
    audit_path = lake_root / "sweeps" / "audit" / "sweep-all.jsonl"
    with AuditWriter.open(audit_path, resume=False) as writer:
        for i in range(200):
            writer.append(
                action="SWEEP_START",
                subject="sweep:bench",
                payload={
                    "instrument": ("NIFTY", "BANKNIFTY", "SENSEX")[i % 3],
                    "tf": ("1m", "5m", "15m", "1d")[i % 4],
                    "direction": "bull",
                    "mode": "A",
                    "k": 1,
                    "data_snapshot_hash": "e" * 64,
                },
            )
    return all_rows


def test_cold_leaderboard_rebuild_bounded_and_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-6: the cold /results scan is absolutely bounded; rank clicks re-scan NOTHING.

    Over a REAL sweeps tree (12 ``ResultsStore`` stores, 300 rows, a 200-entry
    hash-chained audit log): (i) one cold ``load_sweep_scan`` completes inside
    an absolute ceiling with generous measured slack — measured 2026-07-06
    (sandbox) 21.8 ms/scan mean, ceiling 2 s (~×92, the ~×60-slack discipline of
    ``test_lake_invariants_scan_cost.py`` — CI boxes are slower, never looser
    in shape); (ii) three different filter/rank ``build_sweep_leaderboard(scan=…)``
    calls leave the ``ResultsStore.load`` spy at the scan's own 12 and the
    ``iter_entries`` spy at 1 — the (d) split's zero-rescan teeth; (iii) the
    ranked run_id order equals ``ResultsStore.top_k``'s total order over the
    same 300 rows (Golden Rule #4 — the two-pass sort moved verbatim).
    HONEST: the cold scan stays O(stores × rows) + O(audit lines) — paid once
    per ``sweep_results_revision``, never per poll.
    """
    pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")
    from brutex.dashboard.sweep_results_view import build_sweep_leaderboard, load_sweep_scan
    from brutex.engine.results_store import ResultsStore
    from brutex.orchestrator import audit as audit_mod

    all_rows = _seeded_sweeps(tmp_path)
    assert len(all_rows) == 300

    # (i) absolute bound on the cold scan (timed BEFORE the spies add overhead).
    scan = load_sweep_scan(tmp_path)  # warm: imports + page cache
    iterations = 5
    t0 = time.perf_counter()
    for _ in range(iterations):
        scan = load_sweep_scan(tmp_path)
    mean_s = (time.perf_counter() - t0) / iterations
    assert mean_s < 2.0, (
        f"one cold load_sweep_scan took {mean_s * 1e3:.1f} ms mean over the 12-store/"
        f"200-audit-entry tree (ceiling 2000 ms; measured 21.8 ms on 2026-07-06) — "
        f"the scan cost regressed."
    )
    assert scan.store_count == 12
    assert len(scan.rows) == 300

    # (ii) the split's teeth: rank clicks perform ZERO store loads + audit reads.
    load_calls: list[Path] = []
    real_load = ResultsStore.load

    def _load_spy(path: Path) -> ResultsStore:
        load_calls.append(path)
        return real_load(path)

    monkeypatch.setattr(ResultsStore, "load", _load_spy)
    iter_calls: list[Path] = []
    real_iter = audit_mod.iter_entries

    def _iter_spy(path: Path) -> Any:
        iter_calls.append(path)
        return real_iter(path)

    monkeypatch.setattr(audit_mod, "iter_entries", _iter_spy)
    spied_scan = load_sweep_scan(tmp_path)
    assert len(load_calls) == 12  # one per store, paid by the ONE scan
    assert len(iter_calls) == 1  # ONE streaming era pass
    boards = [
        build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa", top_n=50, scan=spied_scan),
        build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="5m", scan=spied_scan),
        build_sweep_leaderboard(tmp_path, rank_by="sharpe_ratio", top_n=10, scan=spied_scan),
    ]
    assert [b.total_rows for b in boards] == [300, 25, 300]
    assert len(load_calls) == 12  # the three clicks re-loaded NOTHING
    assert len(iter_calls) == 1  # and re-read NO audit lines

    # (iii) rank order == ResultsStore.top_k over the SAME rows (Golden Rule #4).
    combined = ResultsStore(path=tmp_path / "combined.parquet")  # outside the sweeps glob
    combined.extend(all_rows)
    expected = [r.run_id for r in combined.top_k(50, by="net_pnl_paisa")]
    assert [row["run_id"] for row in boards[0].rows] == expected


def test_results_poll_flat_during_pull(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B-7: /results polls are cache hits under mid-pull churn — the (c) API-seam teeth.

    Prime ``/api/v1/sweep/leaderboard`` through the real route (the two-level
    scan + rank compute path), then simulate pull churn — REAL
    ``append_ledger_delta`` writes into the same lake plus a ×100
    ``mark_dirty`` storm (the per-window-tick funnel that used to bump the old
    ``_summary_cache.generation`` key). With the sweeps tree unchanged, the
    ``sweep_results_revision`` token is immobile, so repeat polls MUST be pure
    cache hits: the compute-must-raise sentinels on both ``load_sweep_scan``
    and ``build_sweep_leaderboard`` prove neither ever re-runs. Each poll
    still pays the honest O(dirs)+O(1-stat) revision probe — bounded, stated.
    """
    pytest.importorskip("fastapi", reason="the route seam needs the [ui] extra")
    pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")
    from brutex.dashboard import app as app_module
    from brutex.dashboard import sweep_results_view as view_mod
    from brutex.dashboard.app import DashboardState, build_app
    from brutex.dashboard.summary_cache import SummaryCache
    from brutex.data.instrument_store import InstrumentStore
    from brutex.models import Exchange, Instrument, InstrumentType, Segment
    from fastapi.testclient import TestClient

    nifty = Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as instrument_store:
        instrument_store.upsert_many(
            [nifty], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
        )
        lake_root = tmp_path
        _seeded_sweeps(lake_root)
        frozen = {"on": False}
        real_scan = view_mod.load_sweep_scan
        real_build = view_mod.build_sweep_leaderboard

        def _scan_sentinel(path: Path) -> Any:
            if frozen["on"]:
                raise AssertionError("revision unchanged — the sweeps scan must not re-run")
            return real_scan(path)

        def _build_sentinel(path: Path, **kwargs: Any) -> Any:
            if frozen["on"]:
                raise AssertionError("revision unchanged — the rank must not re-run")
            return real_build(path, **kwargs)

        monkeypatch.setattr(app_module, "load_sweep_scan", _scan_sentinel)
        monkeypatch.setattr(app_module, "build_sweep_leaderboard", _build_sentinel)
        summary_cache = SummaryCache()
        app = build_app(
            DashboardState(), instrument_store=instrument_store, summary_cache=summary_cache
        )
        with TestClient(app) as client:
            first = client.get("/api/v1/sweep/leaderboard")
            assert first.status_code == 200
            primed = first.json()
            assert primed["total_rows"] == 300
            # Mid-pull churn: REAL ledger delta appends + a mark_dirty storm.
            ledger = WindowLedger()
            for i in range(5):
                ledger.record(_key(i), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
                assert wls.append_ledger_delta(ledger, lake_root) == 1
            for _ in range(100):
                summary_cache.mark_dirty()
            frozen["on"] = True  # any scan/rank recompute now raises
            for _ in range(10):
                poll = client.get("/api/v1/sweep/leaderboard")
                assert poll.status_code == 200
                assert poll.json() == primed


def test_accumulation_regime_warm_poll_bounded(tmp_path: Path) -> None:
    """B-8 (DEC-WINDOWS-MIDPULL-FLOOR-001): the mid-pull delta-ACCUMULATION regime.

    Deltas accrue ~1 file/2 s for the whole pull (pruned only by the pull-end
    save), so a multi-hour pull reaches hundreds→thousands of pending files.
    Pre-fix the row-PAGE query re-ran the whole O(total rows + delta footers)
    fold on EVERY poll — measured 2026-07-09 (sandbox, shared-CI class):
    33 ms/warm-poll @ 50 deltas → 158 ms @ 400 (the 4 s+ poll class at
    thousands). Three teeth:

    (i)  DETERMINISTIC at accumulation scale: at 400 pending deltas, warm polls
         within one stamp recompute NOTHING — counts AND page families (frozen
         family-aware sentinel);
    (ii) ABSOLUTE (the discriminating bound — the B-4/B-6 "deliberately not
         tier-routed" measured-slack discipline; a RATIO cannot discriminate
         here because the pre-fix cost was also roughly linear in deltas):
         mean warm poll @ 400 pending deltas < 100 ms — measured 4.2 ms
         post-fix (~×24 slack) vs 158 ms pre-fix (fails the bound);
    (iii) MARGINAL per-delta SLOPE (``_MAX_MARGINAL_US_PER_DELTA``): the
         residual per-poll term is the honest O(pending deltas) stamp-PROBE
         directory listing (how a flush is detected at all), never a re-fold —
         so the statistic that bounds it is its SLOPE, ``(t400 − t50) / 350``.
         Measured 6.4-26.1 µs/delta post-fix across two boxes vs 1046 µs/delta
         for the pre-fix re-fold — a 40× separation with no overlap.

         This REPLACES a ``cache_tier_ceiling(8.0)`` ratio gate that was both
         structurally impossible (8.0 IS the 400/50 fixture spread, i.e. the
         ratio's own asymptote as the listing term dominates — zero margin) and
         non-discriminating (the pre-fix regime measures 5.76×, INSIDE the
         post-fix range). Full measured evidence on the module constants above.
    """

    def _warm_lake(deltas: int, tag: str) -> tuple[Path, _SentinelCountCache]:
        lake = _seeded_lake(tmp_path, depth=_SMALL, deltas=deltas, tag=tag)
        return lake, _SentinelCountCache()

    conn = duckdb.connect()

    def _time(lake: Path, cache: _SentinelCountCache) -> float:
        def _poll() -> None:
            payload = build_window_payload(
                lake, conn=conn, count_cache=cache, page_cache=cache, generation=0, limit=500
            )
            assert payload["source"] == "ledger"

        for _ in range(3):  # warm: one real GROUP BY + one real page per stamp
            _poll()
        iterations = 20
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            _poll()
        t1 = time.perf_counter_ns()
        return (t1 - t0) / iterations

    lake50, cache50 = _warm_lake(_ACC_SMALL_DELTAS, "acc50")
    lake400, cache400 = _warm_lake(_ACC_LARGE_DELTAS, "acc400")
    t50 = _time(lake50, cache50)
    t400 = _time(lake400, cache400)
    # (i) the accumulation-scale no-recompute teeth: everything above was ONE
    # compute per family; freeze and poll again — any recompute raises.
    for cache, lake in ((cache50, lake50), (cache400, lake400)):
        assert cache.count_computes == 1
        assert cache.page_computes == 1
        cache.frozen = True
        for _ in range(10):
            build_window_payload(
                lake, conn=conn, count_cache=cache, page_cache=cache, generation=0, limit=500
            )
    conn.close()
    # (ii) the absolute accumulation bound — the pre-fix page-refold regime
    # (158 ms measured) FAILS this; ~×24 measured slack for slower CI boxes.
    assert t400 / 1e6 < 100.0, (
        f"warm /coverage/windows poll at 400 pending deltas took {t400 / 1e6:.1f} ms mean "
        f"(ceiling 100 ms; measured 4.2 ms on 2026-07-09, pre-fix 158 ms) — the page query "
        f"may have regressed to a per-poll re-fold of the delta accumulation."
    )
    # (iii) the probe-term SLOPE — the marginal wall-clock the honest O(deltas)
    # stamp-probe listing adds per pending delta. NOT a ratio: the ratio of the
    # two fixtures is capped from above by their 8× spread, so a ratio ceiling
    # can never carry margin once the listing dominates the constant term (and
    # measured 5.76× for the pre-fix re-fold — inside the post-fix range, i.e.
    # no teeth either). The slope is uncapped and separates the regimes 40×.
    marginal_us_per_delta = (t400 - t50) / (_ACC_LARGE_DELTAS - _ACC_SMALL_DELTAS) / 1_000.0
    assert marginal_us_per_delta < _MAX_MARGINAL_US_PER_DELTA, (
        f"warm-poll marginal cost {marginal_us_per_delta:.1f} µs per pending delta exceeds the "
        f"{_MAX_MARGINAL_US_PER_DELTA:.0f} µs ceiling — the within-stamp poll may have regressed "
        f"past the O(deltas) stamp probe into a per-poll re-fold (measured 6.4-26.1 µs post-fix, "
        f"1046 µs pre-fix). t_50={t50 / 1e6:.2f}ms, t_400={t400 / 1e6:.2f}ms."
    )


def test_fresh_fold_at_production_scale_is_bounded_and_not_flat(tmp_path: Path) -> None:
    """B-9 (relabelled from B-8 at the club fold — main's DEC-WINDOWS-MIDPULL-FLOOR-001
    accumulation-regime pin already holds the B-8 label): the fresh window-ledger fold
    at ≥500k base rows is O(total rows), not O(page).

    The prior B-1 flatness pin tops out at 10k base rows; the disclosed
    full-universe worst case is ~841k ledger rows (DEC-SUSPICION-SWEEP-FIXES-001
    #1492), so this arm exercises the fresh (uncached) fold GROUP BY at ≥500k
    rows — two orders above B-1 — and asserts the HONEST envelope
    (DEC-DB-OPAGE-HONESTY-001): the fold is ONE O(total physical rows) columnar
    QUALIFY hash-partition per FRESH stamp, so (i) it stays under a generous
    ABSOLUTE ceiling (a super-linear regression fails loudly) and (ii) it is NOT
    flat vs a 10k base — the ``_window_counts_total`` cost genuinely grows with
    the stored row count (a false O(page)/O(1) claim would collapse the ratio
    toward 1×). Measured 2026-07-08 (sandbox): 11.8 ms @ 10k vs 230.1 ms @ 500k
    → 19.52× for 50× the rows; the full first payload (fold + one 500-row page)
    at 500k ran 562 ms. Both ≪ the 8 s ABSOLUTE ceiling.
    """
    small = _seeded_lake(tmp_path, depth=_LARGE, deltas=5, tag="b9_small")  # 10k rows
    big = _seeded_lake(tmp_path, depth=500_000, deltas=5, tag="b9_big")
    conn = duckdb.connect()

    def _fresh(lake: Path) -> float:
        source = _ledger_scan_source(wls.ledger_path(lake))
        for _ in range(3):  # warm DuckDB + the OS page cache
            _window_counts_total(conn, source)
        iterations = 8
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            _window_counts_total(conn, source)
        return (time.perf_counter_ns() - t0) / iterations

    # The fold really served every distinct key at production scale (not a footer
    # illusion) — one full payload build over the 500k lake.
    payload = build_window_payload(big, conn=conn, count_cache=MoversSnapshotCache(), generation=0)
    assert payload["source"] == "ledger"
    assert payload["count"] == 500_000

    t_small = _fresh(small)
    t_big = _fresh(big)
    conn.close()
    ratio = t_big / max(t_small, 1.0)

    assert t_big < 8e9, (
        f"fresh fold @ 500k base rows took {t_big / 1e6:.1f} ms (ceiling 8000 ms; measured "
        f"230.1 ms on 2026-07-08) — the O(total-rows) fold may have regressed to super-linear "
        f"work as the ledger approaches the ~841k worst case."
    )
    assert ratio > 1.5, (
        f"fresh-fold latency ratio {ratio:.2f}× (500k vs 10k base rows) is unexpectedly FLAT "
        f"— the fold is an O(total physical rows) columnar QUALIFY hash-partition, so it MUST "
        f"scale with the stored row count (measured 19.52× on 2026-07-08). A near-1× ratio "
        f"means the honest O(total-rows) claim silently became O(page)/O(1) (then update the "
        f"docstrings + this pin) or the fixture failed to scale. "
        f"t_10k={t_small / 1e6:.1f}ms, t_500k={t_big / 1e6:.1f}ms."
    )


def test_module_header_enumerates_the_real_ceiling_mechanisms() -> None:
    """Fix-round-2 doc pin (DEC-LEDGER-DELTA-O-PAGE-001): the module header must
    enumerate the REAL per-pin ceiling mechanisms — never the refuted blanket
    claim that "Every ratio routes through ``ratio_ceiling`` … strict 1.1"
    (B-1 routes through ``cache_tier_ceiling``, whose strict tier is 3.0, and
    B-4/B-6 assert raw ABSOLUTE bounds not routed through any tier helper).
    Golden Rule #10: a maintainer must not be told the strict M4-Pro tier
    enforces 1.1× on every pin in this file."""
    assert __doc__ is not None
    assert "Every ratio routes through" not in __doc__  # the refuted blanket claim
    assert "cache_tier_ceiling" in __doc__  # B-1's tier named…
    assert "3.0" in __doc__  # …with its ACTUAL strict ceiling
    assert "ABSOLUTE" in __doc__  # B-4/B-6 honestly not tier-routed
