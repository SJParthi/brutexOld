"""Chaos tests for the brute-force sweep engine's crash/resume guarantee
(DEC-CHAOS-SWEEP-PARQUET-001).

Sibling of ``tests/chaos/test_audit_chain_chaos.py`` (which hammers the audit
log) — this file hammers the **sweep supervisor's resume contract**
(``brutex.engine.sweep_supervisor.run_supervised_grid`` driving the resident
:class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore` +
:class:`~brutex.engine.sweep_cache.SweepCache`). It injects **randomized crash
boundaries** mid-sweep and proves the engine's two hard invariants survive any
schedule —

1. **bit-identical resume** — a sweep killed at an arbitrary tuple boundary,
   restarted from its on-disk checkpoint + cache (a *fresh* process simulated by
   freshly-constructed guards over the SAME lake paths), produces a results lake
   **byte-for-byte identical** to an uninterrupted run (Golden Rule #4 / #7); and
2. **redo-not-skip, never-lose** — a crash *inside* a tuple (before its
   checkpoint/cache record) leaves the checkpoint at the last *fully-completed*
   tuple, so the crashed tuple is re-run on resume (never silently skipped, never
   double-persisted) — the store re-flushes the same rows byte-identically.

The chaos is the **boundary**: each round runs a random ``survive_k >= 1`` tuples
to completion (each checkpointed) then "crashes" the very next tuple's data load,
so the resume path is exercised at every reachable offset across the grid — the
sweep analogue of the audit chaos test's per-offset restart cycles.

``CHAOS_SWEEP_SEEDS`` / ``CHAOS_SWEEP_ROUNDS`` scale the run (small default for
the PR gate; a soak sets them large). Marked ``chaos`` so the default unit suite
stays fast. Real wheel-backed engine objects throughout (no monkeypatched runner)
— mirrors ``tests/unit/test_engine_sweep_supervisor.py``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
    make_store_for,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import ComboDedup, SweepRunSummary
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter

pytestmark = pytest.mark.chaos

#: Seeds for the randomized crash-boundary schedules (override for a soak).
_SEEDS = [int(s) for s in os.environ.get("CHAOS_SWEEP_SEEDS", "1,7,42").split(",")]
#: Hard cap on crash/restart rounds per schedule — a backstop against a logic
#: bug that fails to make forward progress (the grid is small, so a correct
#: engine always finishes in <= tuples rounds).
_MAX_ROUNDS = int(os.environ.get("CHAOS_SWEEP_ROUNDS", "64"))

_IST = timezone(timedelta(hours=5, minutes=30))


class _SweepCrash(RuntimeError):
    """The injected "process died here" signal raised mid-sweep by the loader."""


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """One IST-session minute bar (stored UTC) with 2dp Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 6, per_day: int = 60) -> list[CandleBar]:
    """A small multi-day upward-drifting session — BUY combos win via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _grid() -> SweepGridConfig:
    """A small multi-tuple grid (3 instruments x 2 take-profits = 6 tuples)."""
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY", "BANKNIFTY", "SENSEX"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50, 70],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap-chaos",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _dedup() -> ComboDedup:
    """A fresh mask-level dedup — one per simulated process, as production does."""
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


def _make_guards(
    tmp_path: Path, grid: SweepGridConfig
) -> tuple[SweepGuards, SweepAuditWriter, SweepCache]:
    """Open the five G&A objects over fixed on-disk paths (so a *restart* re-adopts them).

    The audit writer opens in ``resume`` mode iff its log already exists, so the
    BLAKE3 chain continues across simulated restarts rather than refusing.
    Returns the writer + cache too so the caller can close them.
    """
    audit_path = tmp_path / "sup.jsonl"
    audit = SweepAuditWriter.open(audit_path, resume=audit_path.exists())
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=tmp_path / "ckpt.json",
        sweep_id=grid_identity(grid),
        total_combos=count_sweep_tuples(grid),
    )
    guards = SweepGuards(
        audit=audit,
        progress=SweepProgress(),
        checkpoint=checkpoint,
        cache=cache,
        watchdog=SweepWatchdog(),
    )
    return guards, audit, cache


def _open_ingest(path: Path) -> AuditWriter:
    """Open the per-tuple engine audit, resuming an existing chain across restarts."""
    return AuditWriter.open(path, resume=path.exists())


def _results_fingerprint(root: Path) -> dict[str, str]:
    """Map every results Parquet under *root* to ``relpath -> sha256(bytes)``.

    The bit-identical oracle: two runs that produce the same mapping wrote the
    SAME files with the SAME bytes (Golden Rule #4). An empty mapping (no file)
    is itself a comparable state.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*.parquet")):
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class _CrashInjector:
    """A ``bars_for`` that "crashes" the sweep after *survive_k* completed tuples.

    ``on_tuple`` (fired only after a tuple's checkpoint+cache record) counts
    fully-completed tuples this round; once ``survive_k`` have completed the very
    next ``bars_for`` raises :class:`_SweepCrash` — a crash *inside* a tuple,
    before its own checkpoint record. With ``survive_k >= 1`` at least one tuple
    is durably checkpointed before the crash, so every restart makes forward
    progress (the resume cursor strictly advances) and the round loop terminates.
    """

    def __init__(self, bars: list[CandleBar], survive_k: int) -> None:
        self._bars = bars
        self._survive_k = survive_k
        self.completed = 0
        self.fired = False

    def bars_for(self, _instrument: str, _tf: str) -> Sequence[CandleBar]:
        if not self.fired and self.completed >= self._survive_k:
            self.fired = True
            raise _SweepCrash(f"chaos crash after {self.completed} completed tuples")
        return self._bars

    def on_tuple(self, _tuple: SweepTuple, _summary: SweepRunSummary) -> None:
        self.completed += 1


def _run_clean(tmp_path: Path, grid: SweepGridConfig, bars: list[CandleBar], results: Path) -> None:
    """Run the whole grid to completion once (the uninterrupted reference)."""
    guards, audit, cache = _make_guards(tmp_path, grid)
    try:
        with AuditWriter.open(tmp_path / "ingest.jsonl") as ingest:
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert summary.tuples_run == count_sweep_tuples(grid)
    finally:
        audit.close()
        cache.close()


@pytest.mark.parametrize("seed", _SEEDS)
def test_randomized_crash_boundaries_resume_bit_identical(tmp_path: Path, seed: int) -> None:
    """A sweep crashed at random tuple boundaries resumes to a BYTE-IDENTICAL lake.

    Reference: one clean uninterrupted run → its results fingerprint. Then a chaos
    run into a SEPARATE lake: each round freshly re-opens the guards over the same
    checkpoint/cache/audit paths (simulating a process restart), runs a random
    ``survive_k`` tuples then crashes the next tuple's loader, until a round
    completes with no crash. The final chaos lake must fingerprint-match the
    reference exactly — proving crash-safe, idempotent, lossless resume.
    """
    pytest.importorskip("polars")
    import random

    grid = _grid()
    bars = _trending_bars()
    total = count_sweep_tuples(grid)

    ref_results = tmp_path / "ref"
    _run_clean(tmp_path / "ref-ga", grid, bars, ref_results)
    expected = _results_fingerprint(ref_results)
    assert expected, "reference run produced no result files — fixture too weak to test resume"

    rng = random.Random(seed)
    chaos_ga = tmp_path / "chaos-ga"
    chaos_results = tmp_path / "chaos"
    completed_clean = False
    for _round in range(_MAX_ROUNDS):
        guards, audit, cache = _make_guards(chaos_ga, grid)
        # Resume cursor must never rewind across restarts (monotonic high-water).
        resume_before = guards.checkpoint.load()
        before_index = -1 if resume_before is None else resume_before.last_completed_index
        injector = _CrashInjector(bars, survive_k=rng.randint(1, total))
        try:
            with _open_ingest(chaos_ga / "ingest.jsonl") as ingest:
                run_supervised_grid(
                    grid,
                    bars_for=injector.bars_for,
                    store_for=make_store_for(chaos_results, grid),
                    dedup=_dedup(),
                    ingest_audit=ingest,
                    guards=guards,
                    on_tuple=injector.on_tuple,
                )
            completed_clean = True
        except _SweepCrash:
            # A crash mid-tuple: the checkpoint advanced by exactly the tuples that
            # fully completed this round (>= 1) and never rewound.
            after = SweepCheckpointStore(
                path=chaos_ga / "ckpt.json",
                sweep_id=grid_identity(grid),
                total_combos=total,
            ).load()
            assert after is not None
            assert after.last_completed_index == before_index + injector.completed
            assert after.last_completed_index >= before_index  # never rewinds
        finally:
            audit.close()
            cache.close()
        if completed_clean:
            break

    assert completed_clean, f"sweep never resumed to completion within {_MAX_ROUNDS} rounds"
    assert _results_fingerprint(chaos_results) == expected


def test_crash_after_checkpoint_resume_skips_and_is_bit_identical(tmp_path: Path) -> None:
    """A crash *after* a tuple is checkpointed resume-SKIPS it and stays bit-identical.

    Distinct from the mid-tuple crash above: here the "crash" fires from the
    ``on_tuple`` hook (which runs AFTER ``checkpoint.record`` + ``cache.record``),
    so the just-finished tuple is durably recorded. On restart it is skipped via
    the resume cursor (index < resume_from) AND would also hit the cache — proving
    a post-commit crash never re-runs or corrupts a completed tuple.
    """
    pytest.importorskip("polars")
    grid = _grid()
    bars = _trending_bars()
    total = count_sweep_tuples(grid)

    ref_results = tmp_path / "ref"
    _run_clean(tmp_path / "ref-ga", grid, bars, ref_results)
    expected = _results_fingerprint(ref_results)

    chaos_ga = tmp_path / "chaos-ga"
    chaos_results = tmp_path / "chaos"

    class _CrashOnFirstCommit:
        def __init__(self) -> None:
            self.armed = True

        def on_tuple(self, _t: SweepTuple, _s: SweepRunSummary) -> None:
            if self.armed:
                self.armed = False
                raise _SweepCrash("crash right after the first tuple committed")

    # Round 1: complete + checkpoint tuple 0, then crash in its on_tuple hook.
    guards, audit, cache = _make_guards(chaos_ga, grid)
    try:
        with _open_ingest(chaos_ga / "ingest.jsonl") as ingest, pytest.raises(_SweepCrash):
            run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(chaos_results, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=_CrashOnFirstCommit().on_tuple,
            )
        committed = guards.checkpoint.load()
        assert committed is not None
        assert committed.last_completed_index == 0  # tuple 0 durably recorded
    finally:
        audit.close()
        cache.close()

    # Round 2: clean resume — tuple 0 is resume-skipped, the rest run.
    guards, audit, cache = _make_guards(chaos_ga, grid)
    try:
        with _open_ingest(chaos_ga / "ingest.jsonl") as ingest:
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(chaos_results, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert summary.skipped_resume == 1  # exactly tuple 0 skipped
        assert summary.tuples_run == total - 1
    finally:
        audit.close()
        cache.close()

    assert _results_fingerprint(chaos_results) == expected
