"""Chaos tests for the resumable brute-force sweep engine (DEC-CHAOS-RACE-001).

Companion to ``tests/chaos/test_audit_chain_chaos.py`` (which chaos-tests only the
hash-chained audit log). These inject **randomized crash/restart boundaries**,
**crash-after-flush torn boundaries**, and **concurrent checkpoint readers** to
prove the sweep engine's two hard recoverability invariants survive chaotic
schedules (CLAUDE.md Golden Rule #7 — idempotence; same inputs → same outputs,
bit-for-bit):

1. **Crash-mid-sweep → resume is BIT-IDENTICAL.** No matter where the "crash"
   falls — before a tuple flushes (loader crash) or AFTER it flushed but before
   its checkpoint is recorded (the redo-not-skip boundary, ``sweep_run`` flushes
   then marks dedup) — a fresh-process resume reproduces the EXACT same set of
   per-tuple Parquet result files, byte-for-byte, as one uninterrupted run. Each
   "life" rebuilds the in-memory dedup from scratch (the realistic restart) and
   reloads only the on-disk checkpoint + LMDB cache, exactly as a real
   ``brutex sweep-all`` relaunch does.
2. **A concurrent reader of the resume checkpoint NEVER sees a torn/corrupt
   JSON.** While one thread advances :meth:`SweepCheckpointStore.record`, N reader
   threads spin on :meth:`SweepCheckpointStore.load`; the atomic tmp+``os.replace``
   write means a reader always sees either the previous or the next checkpoint —
   never a half-written file (no :class:`SweepCheckpointError`), and the
   high-water mark a reader observes is monotonic non-decreasing.

``CHAOS_ROUNDS`` / ``CHAOS_WRITERS`` scale the run (small default for the PR gate;
a soak sets them large). Marked ``chaos`` so the default unit suite stays fast.

Bars are small, deterministic, tz-aware synthetic sessions with ``Decimal`` OHLC
(Golden Rule #1) — the SAME builders the supervisor unit tests use, so the chaos
harness rides the real wheel-backed engine (ComboDedup/AuditWriter/ResultsStore/
SweepCache), never a mock.
"""

from __future__ import annotations

import hashlib
import os
import random
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_audit import SweepAuditWriter, verify_chain
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointError, SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
    make_store_for,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import ComboDedup
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

pytestmark = pytest.mark.chaos

#: Rounds of crash/restart (overridable for a soak via the env).
_ROUNDS = int(os.environ.get("CHAOS_ROUNDS", "25"))
#: Concurrent reader threads in the checkpoint-read-during-record test.
_READERS = int(os.environ.get("CHAOS_WRITERS", "8"))

_IST = timezone(timedelta(hours=5, minutes=30))


class _SimulatedCrash(RuntimeError):
    """A deliberate mid-sweep failure standing in for a SIGKILL / power loss."""


# ────── synthetic bars + grid (mirrors tests/unit/test_engine_sweep_supervisor.py) ──────


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
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


def _trending_bars(n_days: int = 12, per_day: int = 90) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win often via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _chaos_grid() -> SweepGridConfig:
    """A multi-tuple grid (2 instruments × 2 directions × 2 sl × 2 tp = 16 tuples).

    Wide enough that a randomized crash boundary lands mid-grid most rounds, small
    enough that the whole sweep runs in well under a second per life.
    """
    return SweepGridConfig(
        instruments=["NIFTY", "BANKNIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80, 120],
        take_profits=[50, 90],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap-chaos",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _fresh_dedup() -> ComboDedup:
    """A brand-new (empty) mask-level dedup — what a real restart rebuilds."""
    return ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-6))


@contextmanager
def _life(
    tmp_path: Path, grid: SweepGridConfig, *, resume: bool
) -> Generator[SweepGuards, None, None]:
    """Open the five G&A guards for ONE process "life", reusing the on-disk state.

    A fresh in-memory :class:`SweepProgress` / :class:`SweepWatchdog` and a fresh
    :class:`SweepCheckpointStore` (which ``.load()``s the persisted JSON) and a
    reopened :class:`SweepCache` (which reloads the persisted LMDB) — exactly what
    a relaunched ``brutex sweep-all`` would reconstruct. The audit writer resumes
    its hash chain (``resume=resume``).
    """
    audit = SweepAuditWriter.open(tmp_path / "sup.jsonl", resume=resume)
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=100_000)
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
    try:
        yield guards
    finally:
        audit.close()
        cache.close()


def _snapshot_results(results_root: Path) -> dict[str, str]:
    """Return ``{relative_parquet_path: sha256(bytes)}`` for every result file."""
    out: dict[str, str] = {}
    for parquet in sorted(results_root.rglob("*.parquet")):
        rel = parquet.relative_to(results_root).as_posix()
        out[rel] = hashlib.sha256(parquet.read_bytes()).hexdigest()
    return out


def _run_clean(tmp_path: Path, grid: SweepGridConfig, bars: list[CandleBar]) -> dict[str, str]:
    """Run the grid once, uninterrupted, and return the golden result fingerprint."""
    results = tmp_path / "results"
    with (
        _life(tmp_path, grid, resume=False) as guards,
        AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
    ):
        summary = run_supervised_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(results, grid),
            dedup=_fresh_dedup(),
            ingest_audit=ingest,
            guards=guards,
        )
        assert summary.tuples_run == count_sweep_tuples(grid)
    return _snapshot_results(results)


# ────── invariant 1: crash-mid-sweep → resume is bit-identical ──────


class TestCrashRestartResume:
    @pytest.mark.parametrize("seed", [1, 7, 42, 1234])
    def test_loader_crash_resume_is_bit_identical(self, tmp_path: Path, seed: int) -> None:
        """Randomized loader-crash boundaries → a resumed sweep reproduces golden.

        The chaos is the boundary: each life crashes (raises in ``bars_for``) after
        a random number of tuples actually ran, dropping the supervisor mid-grid.
        Because the crash precedes that tuple's flush, the tuple is un-checkpointed
        and re-runs on the next life. We keep relaunching with a FRESH dedup +
        reloaded checkpoint/cache until a life completes, then assert the result
        files are byte-for-byte identical to one uninterrupted run.
        """
        pytest.importorskip("polars")
        rng = random.Random(seed)
        grid = _chaos_grid()
        bars = _trending_bars()

        golden_dir = tmp_path / "golden"
        golden = _run_clean(golden_dir, grid, bars)
        assert golden, "golden run produced no result files — grid too small to test"

        chaos_dir = tmp_path / "chaos"
        results = chaos_dir / "results"
        total = count_sweep_tuples(grid)
        completed = False
        for life_no in range(_ROUNDS):
            # Crash after a random number of this life's tuple-runs (>=1 so progress
            # is made every life); a budget >= remaining runs lets a life finish.
            budget = rng.randrange(1, total + 1)
            runs = [0]

            def _crashing_bars(
                _i: str, _t: str, _b: int = budget, _r: list[int] = runs
            ) -> Sequence[CandleBar]:
                _r[0] += 1
                if _r[0] > _b:
                    raise _SimulatedCrash(f"loader crash after {_b} runs")
                return bars

            with (
                _life(chaos_dir, grid, resume=life_no > 0) as guards,
                AuditWriter.open(chaos_dir / "ingest.jsonl", resume=life_no > 0) as ingest,
            ):
                try:
                    run_supervised_grid(
                        grid,
                        bars_for=_crashing_bars,
                        store_for=make_store_for(results, grid),
                        dedup=_fresh_dedup(),
                        ingest_audit=ingest,
                        guards=guards,
                    )
                    completed = True
                except _SimulatedCrash:
                    # A crash leaves the chain ending in SUPERVISOR_ABORT; the
                    # checkpoint is the resume guarantee, not the abort line.
                    pass
            if completed:
                break

        assert completed, f"sweep never completed within {_ROUNDS} lives (seed={seed})"
        # The supervisor audit chain survives every restart boundary intact.
        assert verify_chain(chaos_dir / "sup.jsonl") > 0
        # The crash/resume result lake equals the uninterrupted one, byte-for-byte.
        assert _snapshot_results(results) == golden

    def test_crash_after_flush_before_checkpoint_is_bit_identical(self, tmp_path: Path) -> None:
        """The redo-not-skip boundary: a tuple flushes its Parquet, THEN the process
        dies before the checkpoint records it.

        ``run_persisted_sweep`` flushes the store, then marks dedup; the supervisor
        then records the checkpoint. We crash exactly between the flush and the
        checkpoint record (a wrapped store whose ``flush`` writes the real file then
        raises). On resume that tuple is NOT checkpointed → it re-runs; the existing
        Parquet is pre-loaded (``load_or_create_store``) and the re-evaluated
        finalists re-flush byte-identically — never a hole, never a double row.
        """
        pytest.importorskip("polars")
        grid = _chaos_grid()
        bars = _trending_bars()

        golden_dir = tmp_path / "golden"
        golden = _run_clean(golden_dir, grid, bars)

        chaos_dir = tmp_path / "chaos"
        results = chaos_dir / "results"

        # Life 1: crash after the FIRST tuple's store is flushed (post-write, pre-
        # checkpoint). The real Parquet is on disk; the checkpoint never advanced.
        real_store_for = make_store_for(results, grid)
        fired = [False]

        def _crashing_store_for(tuple_: SweepTuple) -> ResultsStore:
            store = real_store_for(tuple_)
            if not fired[0]:
                fired[0] = True
                return _FlushThenCrash(store)  # type: ignore[return-value]
            return store

        with (
            _life(chaos_dir, grid, resume=False) as guards,
            AuditWriter.open(chaos_dir / "ingest.jsonl") as ingest,
            pytest.raises(_SimulatedCrash),
        ):
            run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=_crashing_store_for,
                dedup=_fresh_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        # The crashing tuple wrote its Parquet but was never checkpointed: the
        # resume cursor sits BELOW that tuple's index (it re-runs next life).
        post_crash = SweepCheckpointStore(
            path=chaos_dir / "ckpt.json",
            sweep_id=grid_identity(grid),
            total_combos=count_sweep_tuples(grid),
        )
        post_crash.load()
        assert post_crash.next_index == 0, "the un-checkpointed crash tuple must re-run"
        assert fired[0], "the flush-then-crash store was never exercised"

        # Life 2: resume cleanly with the normal store factory + a fresh dedup.
        with (
            _life(chaos_dir, grid, resume=True) as guards,
            AuditWriter.open(chaos_dir / "ingest.jsonl", resume=True) as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results, grid),
                dedup=_fresh_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.skipped_resume + summary.tuples_run >= 1

        assert verify_chain(chaos_dir / "sup.jsonl") > 0
        assert _snapshot_results(results) == golden


class _FlushThenCrash:
    """A :class:`ResultsStore` proxy whose ``flush`` writes the real file then dies.

    Stands in for a SIGKILL that lands after ``sweep_run`` persisted the Parquet
    but before the supervisor recorded the resume checkpoint. ``append`` / ``path``
    delegate to the wrapped store; everything else the runner touches is forwarded.
    """

    def __init__(self, inner: ResultsStore) -> None:
        self._inner = inner

    def append(self, row: object) -> bool:
        return self._inner.append(row)  # type: ignore[arg-type]

    @property
    def path(self) -> Path:
        return self._inner.path

    def flush(self) -> object:
        self._inner.flush()  # the real, atomic write actually happens
        raise _SimulatedCrash("crash after flush, before checkpoint record")


# ────── invariant 2: concurrent checkpoint readers never see a torn file ──────


def test_concurrent_checkpoint_reads_never_see_torn_json(tmp_path: Path) -> None:
    """One thread records an advancing high-water mark while N readers spin on
    ``load`` — a reader never sees a corrupt/torn checkpoint, only a clean old/new.

    The atomic tmp+``os.replace`` write means every reader observes either the
    previous fully-written checkpoint or the next one, never a half-written JSON
    (no :class:`SweepCheckpointError`). We also assert each reader's observed
    high-water mark is monotonic non-decreasing and at least one read succeeded.
    """
    path = tmp_path / "ckpt.json"
    total = 400
    writer = SweepCheckpointStore(path=path, sweep_id="chaos-sweep", total_combos=total)
    writer.record(0)  # ensure the file exists before readers start

    done = threading.Event()
    corruption: list[Exception] = []
    successes = [0]
    lock = threading.Lock()

    def reader() -> None:
        local_ok = 0
        last = -1
        while not done.is_set():
            store = SweepCheckpointStore(path=path, sweep_id="chaos-sweep", total_combos=total)
            try:
                checkpoint = store.load()
            except SweepCheckpointError as exc:  # the invariant violation
                corruption.append(exc)
                return
            if checkpoint is not None:
                if checkpoint.last_completed_index < last:
                    corruption.append(
                        AssertionError(
                            f"checkpoint rewound {last} -> {checkpoint.last_completed_index}"
                        )
                    )
                    return
                last = checkpoint.last_completed_index
                local_ok += 1
        with lock:
            successes[0] += local_ok

    threads = [threading.Thread(target=reader) for _ in range(_READERS)]
    for t in threads:
        t.start()
    try:
        for index in range(1, total):
            writer.record(index)
    finally:
        done.set()
        for t in threads:
            t.join()

    assert not corruption, f"checkpoint reader saw a torn/rewound state: {corruption[:1]}"
    assert successes[0] > 0, "no reader ever completed a load — the race was not exercised"
    final = SweepCheckpointStore(path=path, sweep_id="chaos-sweep", total_combos=total).load()
    assert final is not None
    assert final.last_completed_index == total - 1


# ────── invariant: a CORRUPT resume checkpoint → quarantine + start fresh ──────


class TestCorruptCheckpointResume:
    """G12: a corrupt on-disk resume checkpoint never aborts a relaunched sweep.

    The chaos boundary: a life completes and advances ``ckpt.json``; the file is
    then mangled to invalid JSON (a torn/tampered/half-written resume point). The
    relaunched sweep must NOT raise :class:`SweepCheckpointError` — it quarantines
    the corrupt file, starts fresh, and reproduces the byte-identical golden
    results (the durable Parquet tree + LMDB cache are the safety net).
    """

    def test_corrupt_checkpoint_relaunch_reproduces_golden(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        pytest.importorskip("polars")
        grid = _chaos_grid()
        bars = _trending_bars()

        golden = _run_clean(tmp_path / "golden", grid, bars)
        assert golden, "golden run produced no result files — grid too small to test"

        run_dir = tmp_path / "run"
        results = run_dir / "results"
        # Life 1 — a clean uninterrupted run that advances the checkpoint on disk.
        with (
            _life(run_dir, grid, resume=False) as guards,
            AuditWriter.open(run_dir / "ingest.jsonl") as ingest,
        ):
            run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results, grid),
                dedup=_fresh_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert (run_dir / "ckpt.json").exists()

        # Corrupt the resume checkpoint between lives (torn / tampered JSON).
        (run_dir / "ckpt.json").write_text("{torn-checkpoint-bytes", encoding="utf-8")

        # Life 2 — the relaunch must NOT raise; it quarantines + starts fresh and
        # the result tree stays byte-identical to golden (idempotent redo/skip).
        with (
            _life(run_dir, grid, resume=True) as guards,
            AuditWriter.open(run_dir / "ingest.jsonl", resume=True) as ingest,
            capture_logs() as logs,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results, grid),
                dedup=_fresh_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.skipped_resume == 0  # the corrupt cursor did NOT resume

        assert any(e["event"] == "sweep.checkpoint_ignored" for e in logs)
        assert list(run_dir.glob("ckpt.json.corrupt-*"))  # forensic quarantine kept
        assert _snapshot_results(results) == golden
