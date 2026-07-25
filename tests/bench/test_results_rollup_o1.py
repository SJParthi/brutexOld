"""O(K)-serve bench pins for the /results rollup (DEC-RESULTS-TOPK-ROLLUP-001).

Two kinds of teeth (the house #1533/#1614 shape):

* **Deterministic op-count pins** (PRIMARY — no wall-clock flake): a
  rollup-served poll loads exactly ONE Parquet file (the K-union) and reads
  ZERO audit bytes, INDEPENDENT of store count; the legacy scan loads one file
  PER store — the fails-before shape this PR exists to remove.
* **Measured marginal slope**: the full rollup serve (classify → load →
  rank → project) at 10× the store count — over a ROW-EQUAL fixture, so the
  Parquet-load term is flat by construction rather than by assertion —
  stays under an ABSOLUTE per-store bound (:data:`_MAX_SLOPE_S_PER_STORE`).
  The V3 classification honestly pays an O(stores) stat-level glob; the
  bound is what keeps that term honest. A ratio was the previous shape and
  was near-vacuous once the rows were equalised — see
  DEC-BENCH-SLOPE-BOUND-002 (MIGRATION.md #2251, closing a residual
  DEC-BENCH-SLOPE-BOUND-001 #2250 registered) and the test's own
  docstring for the measured two-shape separation.

Honest envelope (Golden Rule #10): the serve is O(K-union) + an O(stores)
stat walk — NOT literal O(1); "O(1) repeat poll" is the route's level-2
payload cache, pinned in the route suite, not re-measured here.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

from brutex.dashboard.sweep_results_view import (
    SWEEPS_DIRNAME,
    build_sweep_leaderboard,
    load_sweep_scan_via_rollup,
)
from brutex.engine.results_rollup import (
    ROLLUP_K,
    LogGate,
    rebuild_rollup,
    rollup_parquet_path,
)
from brutex.engine.results_store import ResultRow, ResultsStore

# --- flatness fixture (DEC-BENCH-SLOPE-BOUND-002 #2251) -------------------------
# The two arms differ ONLY in store count: the row product is held equal
# (``_SMALL_STORES * _LARGE_STORES`` either way), so the K-union — and with
# it the Parquet-load + rank + project term — is byte-for-byte the same size
# on both sides. Anything left in the delta is the O(stores) stat walk.
_SMALL_STORES = 10
_LARGE_STORES = 100
_FIXTURE_ROWS = _SMALL_STORES * _LARGE_STORES
# The absolute marginal bound. MEASURED, never proposed — see the
# ``test_served_poll_flat_vs_store_count`` docstring for the two-shape table.
# Three sandboxes were measured and the PESSIMISTIC one is the figure to
# carry (Golden Rule #10 — min-over-N is not a distribution bound, so the
# headroom is quoted from the WORST observed arm, not the best):
# healthy-max 86.7 / 105.5 µs-per-store and regressed-min 1439.9 / 1304.3 /
# 1197.5 µs-per-store across the three. 600 µs therefore sits at worst
# 5.69× ABOVE healthy-max and 2.00× BELOW regressed-min, separation ~11×
# at worst / ~21× at best, with NO overlap in any sandbox. Widening it to
# make a run green is forbidden — a breach means the O(stores) term grew.
_MAX_SLOPE_S_PER_STORE = 600e-6


def _row(seed: int, *, tf: str) -> ResultRow:
    x = (seed * 2654435761) % (1 << 32)
    wins = x % 50
    losses = (x >> 6) % 50
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf=tf,
        direction="bull",
        mode="A",
        mask_bits=1 + (x % 7),
        net_pnl_paisa=(x % 100_000) - 50_000,
        gross_profit_paisa=x % 90_000,
        gross_loss_paisa=(x >> 3) % 70_000,
        max_drawdown_paisa=(x >> 5) % 40_000,
        expectancy_paisa=((x >> 2) % 9_000) - 4_500,
        total_trades=wins + losses + 1,
        winning_trades=wins,
        losing_trades=losses,
        sharpe_ratio=((x % 600) - 300) / 100,
        sortino_ratio=(((x >> 4) % 700) - 350) / 100,
        calmar_ratio=(((x >> 7) % 500) - 250) / 100,
        profit_factor=((x >> 2) % 400) / 100,
        win_rate=(x % 101) / 100,
        max_drawdown_pct=((x >> 9) % 100) / 100,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _build_lake(lake_root: Path, *, stores: int, rows_per_store: int) -> None:
    """The arch-eval store-builder recipe (scratchpad ``db_bench_results.py``)."""
    seed = 1
    for i in range(stores):
        tf = f"tf{i}"
        dest = lake_root / SWEEPS_DIRNAME / "NIFTY" / tf / f"bull_k3_{i:016x}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        store = ResultsStore(path=dest)
        store.extend([_row(seed + j, tf=tf) for j in range(rows_per_store)])
        store.flush()
        seed += rows_per_store


def _rev(lake_root: Path) -> str:
    from brutex.dashboard.lake_revision import sweep_results_revision

    return sweep_results_revision(lake_root) or ""


def _served_poll(lake_root: Path) -> None:
    """One full rollup-served /results poll: classify → load → rank → project."""
    serve = load_sweep_scan_via_rollup(lake_root, rev=_rev(lake_root), log_gate=LogGate())
    assert serve.scan is not None
    board = build_sweep_leaderboard(lake_root, top_n=50, scan=serve.scan)
    assert board.available is True


def _best_of(n: int, fn: object) -> float:
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        fn()  # type: ignore[operator]
        best = min(best, time.perf_counter() - start)
    return best


class TestRollupServeOpCounts:
    def test_served_poll_loads_one_parquet_and_zero_audit_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fails-before pin: pre-rollup, a cold poll loaded O(stores)
        Parquet files; the rollup-served poll loads exactly ONE."""
        _build_lake(tmp_path, stores=20, rows_per_store=5)
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        assert rebuild_rollup(sweeps_root) is True

        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))

        import brutex.orchestrator.audit as audit_module

        def _forbidden(path: Path) -> object:
            raise AssertionError("a rollup-served poll must not stream the audit chain")

        monkeypatch.setattr(audit_module, "iter_entries", _forbidden)
        _served_poll(tmp_path)
        assert loads == [rollup_parquet_path(sweeps_root)]

    def test_legacy_scan_is_the_o_stores_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The teeth twin: the legacy loader really is O(stores) file loads —
        the quantity the rollup removes from the poll path."""
        from brutex.dashboard.sweep_results_view import load_sweep_scan

        stores = 20
        _build_lake(tmp_path, stores=stores, rows_per_store=5)
        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))
        scan = load_sweep_scan(tmp_path)
        assert scan.store_count == stores
        assert len(loads) == stores


class TestRollupServeFlatness:
    def test_served_poll_flat_vs_store_count(self, tmp_path: Path) -> None:
        """10× the stores (10 → 100) must not cost more than the MEASURED
        marginal per-store bound — the O(stores) stat-glob is the only
        store-count term left on the serve path.

        FIXTURE (DEC-BENCH-SLOPE-BOUND-002 #2251): the two arms are ROW-EQUAL by
        construction — ``_SMALL_STORES * _LARGE_STORES`` rows either way, so
        both build exactly ``_FIXTURE_ROWS`` rows and both K-unions are the
        same size. The prior fixture was 10×10 vs 100×10, i.e. 100 rows vs
        1,000: the large arm paid 10× the stat-glob AND 10× the Parquet-load
        + rank + project work, so the docstring's "the Parquet-load term is
        K-bounded" claim was false FOR THIS FIXTURE (K-bounded is not
        row-count-equal). Equalising the rows is a CORRECTNESS fix — it
        removes work the control arm was not doing — not a teeth weakening.

        STATISTIC: a ratio is the wrong shape for a marginal cost. With the
        rows equalised the timing is ``t(s) = F + s*c`` (F = the fixed
        ``_FIXTURE_ROWS``-row load + rank + project; c = the per-store stat
        cost), so the ratio ``(F + 100c)/(F + 10c)`` is bounded above by 10
        and tends to 1 as ``c -> 0`` — an 8.0× ceiling on it could only fail
        if c dominated F by an order of magnitude. The honest statistic is
        the SLOPE, bounded absolutely.

        MEASURED 2026-07-25 (sandbox; ``scratchpad/measure_rollup_slope.py``
        + ``measure_legacy_slope.py``, best-of-5 per arm):

        =========================  =========================  =======
        shape                      slope (µs/store)           trials
        =========================  =========================  =======
        healthy (rollup-served)    median 66.8, max 86.7          16
        healthy (re-measure A)     median 63.2, max 105.5          5
        healthy (re-measure B)     median 61.2, max 75.9           8
        healthy (re-verify C)      median 65.6, max 93.5          10
        regressed (legacy scan)    median 1592.8, min 1439.9       5
        regressed (re-measure A)   median 1330.3, min 1197.5       5
        regressed (re-measure B)   median 1498.0, min 1304.3       6
        regressed (re-verify C)    median 1455.9, min 1335.5       5
        =========================  =========================  =======

        FOUR sandboxes, all reported because they DISAGREE and the honest
        bound is the weakest one (Golden Rule #10): separation runs ~11×
        (worst) to ~21× (best) with NO overlap anywhere, and 600 µs/store
        sits at worst **5.69× ABOVE healthy-max** (re-measure A) and **2.00×
        BELOW regressed-min** (re-measure A). Quote those, not the authoring
        container's 6.9× / 2.4× — a min-over-N is a sample, not a
        distribution bound. Sandbox C is the one REPRODUCIBLE on demand from
        the harnesses named above and reads 6.42× / 2.23×, i.e. it lands
        BETWEEN A and the authoring run and contradicts neither. The
        regressed shape is the exact O(stores) file-load path
        ``test_legacy_scan_is_the_o_stores_shape`` pins, and it breaches the
        bound on every trial of every sandbox.

        WHY THIS REPLACED A RATIO, measured rather than argued: applying the
        OLD ratio gate to the OLD fixture reads, POOLED ACROSS EVERY RECORDED
        RUN, healthy 6.36-6.97× against regressed 6.37-11.20× — the ranges
        **OVERLAP**, so the ratio could not reliably separate the two shapes
        ACROSS runs, while healthy sat at ~84% of its own 8.0 ceiling
        (flake-tight in the other direction). Stated precisely, because it
        matters: within any ONE run the two shapes may well separate (the
        sandbox-C run reads healthy 6.36-6.76× vs regressed 9.80-10.10×,
        which does not overlap) — but a CI gate has to hold ACROSS runs on
        unlike runners, and pooled across runs it does not. The slope is
        strictly STRONGER, not merely tidier.

        ESTIMATOR CAVEAT (stated because this file is otherwise scrupulous
        about estimator choice): ``_best_of`` takes the MINIMUM per arm, so
        ``min(large) - min(small)`` systematically UNDER-states the marginal
        cost versus a median-based slope — i.e. it biases toward PASSING.
        That is deliberate here (the min is the least contended sample, which
        is what a per-store cost should be read from on a shared runner) and
        harmless at the measured 5.69× worst-case headroom, but it means this
        gate is tuned to catch a term that GROWS, never one that merely
        wobbles. HONEST LIMIT (Golden Rule #10): still a wall-clock gate — it
        detects an O(stores) term that grows, never a constant-factor
        regression in F. The deterministic op-count pins in
        :class:`TestRollupServeOpCounts` (one Parquet load, zero audit bytes,
        independent of store count) remain the PRIMARY teeth and are
        untouched.
        """
        # Drift pin: the fixture is sized to sit exactly AT the union cap, so
        # both arms measure the heaviest legal load. A ``ROLLUP_K`` bump must
        # move the fixture with it or this arm silently becomes under-cap.
        assert _FIXTURE_ROWS >= ROLLUP_K, (
            f"fixture rows ({_FIXTURE_ROWS}) fell BELOW the union cap "
            f"ROLLUP_K={ROLLUP_K} — resize _SMALL_STORES/_LARGE_STORES so their "
            f"product stays at or above it (their product is also the row count "
            f"PER ARM). Below the cap the K-union stops being the heaviest legal "
            f"load and the fixed term F the slope is measured against shrinks."
        )
        # The slope divides by this span, so a fixture resize that collapsed it
        # would be a ZeroDivisionError rather than a readable refusal.
        assert _LARGE_STORES > _SMALL_STORES, (
            f"store-count arms collapsed ({_SMALL_STORES} vs {_LARGE_STORES}) — "
            f"the marginal slope needs a positive span to divide by"
        )
        small = tmp_path / "small"
        large = tmp_path / "large"
        _build_lake(small, stores=_SMALL_STORES, rows_per_store=_LARGE_STORES)
        _build_lake(large, stores=_LARGE_STORES, rows_per_store=_SMALL_STORES)
        assert rebuild_rollup(small / SWEEPS_DIRNAME) is True
        assert rebuild_rollup(large / SWEEPS_DIRNAME) is True
        # Warm both paths (filesystem cache + import warmup); best-of-5 keeps
        # each arm robust when a combined-suite run trashes the page cache
        # (observed flake class: the SMALL side catching a slow first stat).
        for _ in range(2):
            _served_poll(small)
            _served_poll(large)
        t_small = _best_of(5, lambda: _served_poll(small))
        t_large = _best_of(5, lambda: _served_poll(large))
        slope = (t_large - t_small) / (_LARGE_STORES - _SMALL_STORES)
        assert slope <= _MAX_SLOPE_S_PER_STORE, (
            f"rollup-served poll's marginal per-store cost regressed: "
            f"{t_small * 1e3:.2f} ms @{_SMALL_STORES} stores → {t_large * 1e3:.2f} ms "
            f"@{_LARGE_STORES} stores over an identical {_FIXTURE_ROWS}-row union "
            f"= {slope * 1e6:.1f} µs/store (bound {_MAX_SLOPE_S_PER_STORE * 1e6:.0f} µs/store; "
            f"measured healthy max 86.7-105.5 across sandboxes, regressed legacy "
            f"scan min 1197.5-1439.9 — see the docstring's three-sandbox table)"
        )
