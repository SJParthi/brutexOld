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
  DEC-BENCH-SLOPE-BOUND-001 (MIGRATION.md #2250) and the test's own
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

# --- flatness fixture (DEC-BENCH-SLOPE-BOUND-001) -------------------------
# The two arms differ ONLY in store count: the row product is held equal
# (``_SMALL_STORES * _LARGE_STORES`` either way), so the K-union — and with
# it the Parquet-load + rank + project term — is byte-for-byte the same size
# on both sides. Anything left in the delta is the O(stores) stat walk.
_SMALL_STORES = 10
_LARGE_STORES = 100
_FIXTURE_ROWS = _SMALL_STORES * _LARGE_STORES
# The absolute marginal bound. MEASURED, never proposed — see the
# ``test_served_poll_flat_vs_store_count`` docstring for the two-shape table
# (healthy max 86.7 µs/store vs regressed legacy-scan min 1439.9 µs/store,
# ~18× apart). 600 µs sits 6.9× above healthy-max and 2.4× below
# regressed-min. Widening it to make a run green is forbidden
# (Golden Rule #10) — a breach means the O(stores) term genuinely grew.
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

        FIXTURE (DEC-BENCH-SLOPE-BOUND-001): the two arms are ROW-EQUAL by
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

        =========================  ==================  =======
        shape                      slope (µs/store)    trials
        =========================  ==================  =======
        healthy (rollup-served)    median 66.8, max 86.7    16
        regressed (legacy scan)    median 1592.8, min 1439.9  5
        =========================  ==================  =======

        ~18× separation, so the bound below is genuine teeth, not decoration:
        it sits 6.9× ABOVE the healthy max (headroom for a slower / contended
        shared runner) and 2.4× BELOW the regressed min (the exact O(stores)
        file-load shape ``test_legacy_scan_is_the_o_stores_shape`` pins still
        fails it). HONEST LIMIT (Golden Rule #10): this is still a wall-clock
        gate on a shared runner — it detects an O(stores) term that GROWS,
        never a constant-factor regression in F. The deterministic op-count
        pins in :class:`TestRollupServeOpCounts` (one Parquet load, zero
        audit bytes, independent of store count) remain the PRIMARY teeth and
        are untouched.
        """
        # Drift pin: the fixture is sized to sit exactly AT the union cap, so
        # both arms measure the heaviest legal load. A ``ROLLUP_K`` bump must
        # move the fixture with it or this arm silently becomes under-cap.
        assert _FIXTURE_ROWS == ROLLUP_K, (
            f"fixture rows ({_FIXTURE_ROWS}) no longer sit at the union cap "
            f"ROLLUP_K={ROLLUP_K} — resize _SMALL_STORES/_LARGE_STORES so their "
            f"product tracks it (their product is also the row count PER ARM)"
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
            f"measured healthy max 86.7, regressed legacy scan min 1439.9)"
        )
