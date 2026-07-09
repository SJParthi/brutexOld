"""The rank-budget worker divisor wiring (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001).

The C4/W7 root: :func:`brutex.engine.sweep_mem_budget.resolve_mem_budget_bytes`
never passed ``workers=`` to the COMMON
:func:`brutex.config.hardware_envelope.memory_budget_bytes`, so each of the
pool's EVAL workers independently admitted ``total × 1/6`` — ~1.167× total RAM
of aggregate admissible rank retention at the AUTO cap's 7 eval workers
(Breaker B measured 3 processes × 2.62 GiB on a 15.70 GiB box ≡ 7 × 8 GiB =
56 GiB on the CLAUDE.md §8 48 GB reference). These tests pin the fix:

* :func:`resolve_budget_workers` — the internal ``BRUTEX_SWEEP_BUDGET_WORKERS``
  divisor seam (unset → 1; garbage fails LOUD, Golden Rule #1);
* :func:`resolve_mem_budget_bytes` divides the RAM-DERIVED default by the live
  eval-worker count (env seam AND explicit ``workers=`` kwarg), while an
  explicit ``BRUTEX_SWEEP_MEM_BUDGET_BYTES`` override stays per-worker
  UNDIVIDED (the #1352 contract — the dashboard launch_button's 1 GiB hardcode
  keeps working unchanged) and the no-env single-process path stays
  byte-identical (``workers == 1`` parity);
* the POOL SEAM: ``_worker_init`` exports the divisor from
  ``_WorkerContext.budget_workers`` into the worker's environment, and
  ``ParallelGridDriver.run`` wires the live ``pool_size`` (= ``workers − 1``
  eval workers) into the context it hands the pool initializer — so the bare
  CLI ``brutex sweep-all`` and the nightly chain get the divided budget
  automatically, with no call-site change.

ADMISSION-only, never results: the bounded rank is pinned bit-identical to the
batch rank, so a divided budget changes only WHEN the bounded path engages /
a refusal fires — the frozen sweep goldens pass unmodified.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from collections.abc import Generator, Iterator, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, ClassVar

import brutex.engine.sweep_parallel as mod
import pytest
from brutex.config.hardware_envelope import UNDETECTED_RAM, reset_hardware_envelope_cache
from brutex.data.models import CandleBar
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_mem_budget import (
    BUDGET_WORKERS_ENV,
    DEFAULT_MEM_BUDGET_BYTES,
    MEM_BUDGET_ENV,
    MEM_BUDGET_FRACTION,
    resolve_budget_workers,
    resolve_mem_budget_bytes,
)
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_supervisor import SweepGuards, grid_identity
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.observability.logging import reset_configuration
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

_GIB = 1024**3


@pytest.fixture(autouse=True)
def _fresh_ram_cache() -> Iterator[None]:
    """Isolate from the process-global RAM detection cache (DEC-RAM-BUDGETS-001)."""
    reset_hardware_envelope_cache()
    yield
    reset_hardware_envelope_cache()


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None, None, None]:
    """In-process ``_worker_init`` binds GLOBAL structlog state — reset per test."""
    yield
    reset_configuration()


def _simulate_ram(monkeypatch: pytest.MonkeyPatch, total_bytes: int) -> None:
    """Force RAM detection to *total_bytes* (0 = the undetected sentinel)."""
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysconf_total_ram_bytes", lambda: total_bytes
    )
    monkeypatch.setattr(
        "brutex.config.hardware_envelope._sysctl_total_ram_bytes", lambda: UNDETECTED_RAM
    )
    reset_hardware_envelope_cache()


# ────── resolve_budget_workers (the internal divisor seam) ──────


class TestResolveBudgetWorkers:
    def test_unset_is_one(self) -> None:
        assert resolve_budget_workers({}) == 1

    def test_blank_is_one(self) -> None:
        assert resolve_budget_workers({BUDGET_WORKERS_ENV: "   "}) == 1

    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("7", 7), ("  4  ", 4)])
    def test_valid_values(self, raw: str, expected: int) -> None:
        assert resolve_budget_workers({BUDGET_WORKERS_ENV: raw}) == expected

    def test_junk_raises_naming_the_env(self) -> None:
        with pytest.raises(ValueError, match=BUDGET_WORKERS_ENV):
            resolve_budget_workers({BUDGET_WORKERS_ENV: "many"})

    @pytest.mark.parametrize("raw", ["0", "-3"])
    def test_non_positive_raises(self, raw: str) -> None:
        with pytest.raises(ValueError, match=BUDGET_WORKERS_ENV):
            resolve_budget_workers({BUDGET_WORKERS_ENV: raw})

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BUDGET_WORKERS_ENV, "5")
        assert resolve_budget_workers() == 5


# ────── resolve_mem_budget_bytes divides by the live worker count ──────


class TestResolveMemBudgetBytesDividesByWorkers:
    def test_env_divisor_divides_derived_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILS-BEFORE: pre-wire each worker resolved the FULL total × 1/6."""
        _simulate_ram(monkeypatch, 48 * _GIB)
        divided = resolve_mem_budget_bytes({BUDGET_WORKERS_ENV: "7"})
        assert divided == int(48 * _GIB * MEM_BUDGET_FRACTION / 7)
        assert divided < DEFAULT_MEM_BUDGET_BYTES  # strictly below the undivided 8 GiB

    def test_workers_kwarg_divides_and_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILS-BEFORE: the kwarg did not exist (TypeError pre-wire)."""
        _simulate_ram(monkeypatch, 48 * _GIB)
        got = resolve_mem_budget_bytes({BUDGET_WORKERS_ENV: "7"}, workers=3)
        assert got == int(48 * _GIB * MEM_BUDGET_FRACTION / 3)

    def test_explicit_override_stays_undivided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The #1352 contract: BRUTEX_SWEEP_MEM_BUDGET_BYTES is a PER-WORKER figure.

        The dashboard launch_button's ``1073741824`` (1 GiB) hardcode keeps
        working unchanged — the override wins verbatim BEFORE any division.
        """
        _simulate_ram(monkeypatch, 48 * _GIB)
        got = resolve_mem_budget_bytes({MEM_BUDGET_ENV: "1073741824", BUDGET_WORKERS_ENV: "7"})
        assert got == 1_073_741_824

    def test_no_divisor_parity_with_single_process_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """workers=1 / env-absent parity: byte-identical to the pre-wire default."""
        _simulate_ram(monkeypatch, 48 * _GIB)
        assert resolve_mem_budget_bytes({}) == 8 * _GIB
        assert resolve_mem_budget_bytes({}, workers=1) == 8 * _GIB

    def test_undetected_ram_returns_floor_exactly_even_with_divisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undetected RAM → the 8 GiB floor EXACTLY (the memory_budget_bytes law).

        The divisor applies to the DETECTED derivation only — the undetected
        sentinel keeps the pre-derivation constant bit-for-bit (documented
        honestly in the ledger entry, not a silent behavior).
        """
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert resolve_mem_budget_bytes({BUDGET_WORKERS_ENV: "7"}) == DEFAULT_MEM_BUDGET_BYTES

    def test_invalid_env_divisor_raises_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * _GIB)
        with pytest.raises(ValueError, match=BUDGET_WORKERS_ENV):
            resolve_mem_budget_bytes({BUDGET_WORKERS_ENV: "junk"})


# ────── the pool seam: _worker_init exports the divisor ──────


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


class TestWorkerInitExportsBudgetWorkers:
    def test_worker_init_exports_the_divisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILS-BEFORE: _WorkerContext had no ``budget_workers`` field."""
        monkeypatch.delenv(BUDGET_WORKERS_ENV, raising=False)
        ctx = mod._WorkerContext(grid=_grid(), root=Path("/tmp/x"), budget_workers=7)
        mod._worker_init(ctx)
        assert os.environ[BUDGET_WORKERS_ENV] == "7"

    def test_worker_context_default_is_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BUDGET_WORKERS_ENV, raising=False)
        ctx = mod._WorkerContext(grid=_grid(), root=Path("/tmp/y"))
        assert ctx.budget_workers == 1
        mod._worker_init(ctx)
        assert os.environ[BUDGET_WORKERS_ENV] == "1"

    def test_worker_init_overwrites_a_stale_parent_export(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live pool size is the truth — a leftover/parent value never wins."""
        monkeypatch.setenv(BUDGET_WORKERS_ENV, "99")
        mod._worker_init(mod._WorkerContext(grid=_grid(), root=Path("/tmp/z"), budget_workers=3))
        assert os.environ[BUDGET_WORKERS_ENV] == "3"

    def test_each_simulated_worker_divides_by_the_live_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N worker-context resolves each divide by the live eval-worker count.

        FAILS-BEFORE: each simulated worker resolved the FULL undivided
        ``total × 1/6`` (the Breaker-B FINDING-2 aggregate-over-admission repro,
        here run through the REAL ``_worker_init`` → ``resolve_mem_budget_bytes``
        chain instead of raw subprocesses).
        """
        monkeypatch.delenv(BUDGET_WORKERS_ENV, raising=False)
        monkeypatch.delenv(MEM_BUDGET_ENV, raising=False)
        _simulate_ram(monkeypatch, 48 * _GIB)
        pool_size = 3
        budgets = []
        for _ in range(pool_size):
            mod._worker_init(
                mod._WorkerContext(grid=_grid(), root=Path("/tmp/w"), budget_workers=pool_size)
            )
            budgets.append(resolve_mem_budget_bytes())
        expected_each = int(48 * _GIB * MEM_BUDGET_FRACTION / pool_size)
        assert budgets == [expected_each] * pool_size
        # The aggregate admissible retention is now ~ONE budget share — the
        # pre-fix aggregate was pool_size × 8 GiB (1.167× total RAM at 7 workers).
        assert sum(budgets) <= int(48 * _GIB * MEM_BUDGET_FRACTION) + pool_size


# ────── the driver wires the live pool size into the context ──────


class _CaptureExecutor:
    """A ``ProcessPoolExecutor`` stand-in that CAPTURES the initializer context.

    Runs the real ``_worker_init`` in-process (so the export is observable) and
    resolves every submitted tuple via the real in-process worker body — the
    ``_FakePool`` precedent of ``test_engine_sweep_parallel.py``, plus the
    constructor capture this pin needs.
    """

    captured: ClassVar[list[tuple[int, Any]]] = []

    def __init__(
        self,
        *,
        max_workers: int,
        mp_context: object,
        initializer: Any,
        initargs: tuple[Any, ...],
    ) -> None:
        type(self).captured.append((max_workers, initargs[0]))
        initializer(*initargs)

    def __enter__(self) -> _CaptureExecutor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        fut: Future[mod.TupleWorkResult] = Future()
        fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))
        return fut


class TestDriverWiresPoolSize:
    def test_run_parallel_grid_hands_the_eval_worker_count_to_the_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """workers=3 → pool_size=2 eval workers → ``budget_workers == 2``.

        FAILS-BEFORE: the context carried no divisor, so every worker's
        resolve divided by 1.
        """
        monkeypatch.delenv(BUDGET_WORKERS_ENV, raising=False)
        monkeypatch.setattr(mod, "ProcessPoolExecutor", _CaptureExecutor)
        _CaptureExecutor.captured = []
        grid = _grid()
        audit = SweepAuditWriter.open(tmp_path / "sup.jsonl")
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
        try:
            with AuditWriter.open(tmp_path / "ingest.jsonl") as ingest:
                mod.run_parallel_grid(
                    grid,
                    bars_for=lambda _i, _tf: [],  # no-bars skips — construction is the pin
                    root=tmp_path / "out",
                    ingest_audit=ingest,
                    guards=guards,
                    workers=3,
                )
        finally:
            audit.close()
            cache.close()
        assert len(_CaptureExecutor.captured) == 1
        max_workers, ctx = _CaptureExecutor.captured[0]
        assert max_workers == 2  # pool_size = workers − 1 (the writer-actor core)
        assert ctx.budget_workers == 2
        assert os.environ[BUDGET_WORKERS_ENV] == "2"  # the in-process initializer ran
