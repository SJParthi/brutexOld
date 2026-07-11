"""Per-worker resident-kernel cache tests — `brutex.engine.sweep_parallel`.

DEC-SWEEP-WORKER-KERNEL-CACHE-001 (MIGRATION.md #1729, slice-5 PR-A): the
parallel sweep threads ONE per-worker-process
:class:`~brutex.engine.sweep_kernel.ResidentKernelCache` through the EXISTING
``run_one_tuple(kernel_cache=)`` seam, behind the DEFAULT-OFF
``BRUTEX_SWEEP_WORKER_KERNEL_CACHE`` flag. The design's proof obligations
(T1-T6 + the flag hygiene + the real-spawn reuse pin), each held to the
100% line+branch gate alongside ``test_engine_sweep_parallel.py``:

* **T1 — mixed-key reuse byte-parity**: a real 2-instrument parallel run,
  flag ON, produces the byte-identical result tree / ingest chain /
  per-tuple ``store_content_hash`` sequence as the SAME grid flag OFF, with
  the parent's ``kernel_reuse_summary`` proving ``builds ≤ keys × workers``
  and ``hits > 0``.
* **T2 — LRU-thrash correctness**: a ``capacity=1`` override under a
  2-proximity grid rebuilds MORE, bytes IDENTICAL (correctness never
  depends on hit rate).
* **T3 — roll-schedule / #1062 pin**: a mixed spot + ``NSE-NIFTY-FUT`` grid
  keys distinct kernels per instrument (the schedule is IN the key), bytes ≡
  sequential; a hand-built WRONG-schedule kernel still trips the
  ``run_persisted_sweep`` fail-fast THROUGH the worker path.
* **T4 — bars-intern memo + the IDENTITY serve guard** (fix-round-2,
  refuter-1 A-1/A-2): same-``(instrument, tf)`` dispatches share ONE
  resident bars copy, registered ONCE per worker (write-once) — a resident
  kernel is only ever served against the EXACT object it was built from.
  Any arrival that cannot be identity-reconciled disables the WHOLE
  per-worker mechanism (cache AND memo) fail-safe, WARN-once with a machine
  ``reason``: a resident copy failing the len + first/mid/last value probe
  (``resident_value_mismatch``), or a key re-arriving AFTER its memo entry
  was LRU-evicted (``revisit_after_eviction`` — stale kernels may still be
  resident and a fresh copy can never BE the evicted object; the A-1
  executed repro is ported as a permanent regression). Residuals pinned
  honestly: the disable is PER-WORKER (siblings keep their own caches;
  cross-worker safety rides the deterministic ``bars_for`` contract); a
  disabled worker's telemetry freezes, so the drain-end summary
  UNDER-COUNTS; the value probe is a corruption DETECTOR, never the safety
  rail — a probe-blind interior mutation substitutes the registered copy
  SELF-CONSISTENTLY (never a mixed kernel-vs-sim pairing; pinned).
* **T5 — flag hygiene**: junk env → OFF + warn-once; invalid capacity →
  grid default + loud warn; capacity above ``WORKER_KERNEL_CAPACITY_MAX``
  → clamped + loud warn (fix-round-1 R3 fold; the two above-ceiling params
  carry the revert-teeth — boundary-64 pins the silent-verbatim contract
  and structurally passes with the clamp reverted); the grid-DERIVED
  default clamps at the same ceiling (fix-round-2, refuter-2 NIT); the OFF
  arm constructs NO cache (spy).
* **T6 — memory bound**: the cache length never exceeds its capacity.
* **SPAWN** — a REAL spawn pool (``workers=2`` → ONE EVAL worker, so the
  arithmetic is deterministic) reuses the kernel across tuples:
  ``builds_total == distinct keys``, ``hits_total == tuples − builds``.

The existing byte-identity/golden suites pass UNMODIFIED (default-OFF pins
the legacy path); the RAYON_NUM_THREADS=1 determinism pin is re-asserted
with the cache ON (the pin is LOAD-BEARING for the published perf law).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from collections.abc import Generator, Mapping, Sequence
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import brutex.engine.sweep_parallel as mod
import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_audit import read_entries as read_sup_entries
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_kernel import ResidentKernelCache, ResidentSweepKernel
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
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
from brutex.observability.logging import reset_configuration
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.audit import read_entries as read_ingest_entries
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _reset_worker_state() -> Generator[None, None, None]:
    """Reset the per-worker module globals + warn-once memos + structlog.

    In-process ``_worker_init`` calls mutate module-global worker state (the
    kernel cache / bars memo / warn latch) and configure GLOBAL structlog —
    reset after each test so the suite is leak-free (the sibling suite's
    ``_reset_logging_state`` discipline, extended to the cache globals).
    """
    yield
    mod._WORKER_KERNEL_CACHE = None
    mod._WORKER_BARS_MEMO = None
    mod._WORKER_BARS_KEYS_SEEN = set()
    mod._WORKER_BARS_MEMO_WARNED = False
    mod.reset_worker_kernel_flag_warnings_for_tests()
    reset_configuration()


# ────────────────────────── shared fixtures ──────────────────────────
# Mirrors of the sibling suite's deterministic synthetic-session fixtures
# (tz-aware Decimal OHLC, Golden Rule #1) — duplicated MINIMALLY here so this
# file stays importable standalone (the coverage-gate discovery collects both
# files together; neither imports the other).


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
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


def _trending_bars(n_days: int = 4, per_day: int = 60) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _trending_bars()


def _bars_for(_instrument: str, _tf: str) -> list[CandleBar]:
    return _BARS


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY, Side.SELL],
        "k_values": [1, 2],
        "condition_bits": [0, 1, 2, 3, 4],
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


#: An ADR-0012 roll schedule spanning the ``_trending_bars`` fixture (which
#: starts 2026-01-05 IST): one mid-stream roll boundary + a covering tail
#: entry, expressed as owning-expiry day-ordinals (the orchestrator-suite twin).
_FUT_SCHEDULE: tuple[int, ...] = (
    datetime(2026, 1, 8, tzinfo=_IST).date().toordinal(),
    datetime(2026, 3, 31, tzinfo=_IST).date().toordinal(),
)

#: A DIFFERENT but bars-covering schedule — used ONLY to hand-build the
#: wrong-schedule kernel for the T3 fail-fast re-pin.
_WRONG_SCHEDULE: tuple[int, ...] = (
    datetime(2026, 1, 9, tzinfo=_IST).date().toordinal(),
    datetime(2026, 3, 31, tzinfo=_IST).date().toordinal(),
)


def _run_grid(
    tmp: Path,
    grid: SweepGridConfig,
    *,
    workers: int,
    roll: dict[str, tuple[int, ...]] | None = None,
) -> tuple[Path, Path, int]:
    """Run the grid (sequential when ``workers == 1``, else parallel)."""
    out = tmp / "out"
    with (
        AuditWriter.open(tmp / "ingest.jsonl") as ingest,
        SweepAuditWriter.open(tmp / "sup.jsonl") as sup,
        SweepCache.open(tmp / "cache", bloom_capacity=10_000) as cache,
    ):
        guards = SweepGuards(
            audit=sup,
            progress=SweepProgress(),
            checkpoint=SweepCheckpointStore(
                path=tmp / "ckpt.json",
                sweep_id=grid_identity(grid),
                total_combos=count_sweep_tuples(grid),
            ),
            cache=cache,
            watchdog=SweepWatchdog(),
        )
        if workers == 1:
            run_supervised_grid(
                grid,
                bars_for=_bars_for,
                store_for=make_store_for(out, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                ingest_audit=ingest,
                guards=guards,
                roll_schedule_by_instrument=roll,
            )
        else:
            mod.run_parallel_grid(
                grid,
                bars_for=_bars_for,
                root=out,
                ingest_audit=ingest,
                guards=guards,
                workers=workers,
                roll_schedule_by_instrument=roll,
            )
        ckpt = guards.checkpoint.next_index
    return out, tmp / "ingest.jsonl", ckpt


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _ingest_lines(path: Path) -> list[tuple[int, str, str, dict[str, Any]]]:
    return [(e.seq, e.action, e.subject, e.payload) for e in read_ingest_entries(path)]


def _tuple_done_hashes(sup_path: Path) -> list[tuple[object, object]]:
    """The per-tuple ``(tuple, store_content_hash)`` sequence from the sup chain."""
    return [
        (e.payload.get("tuple"), e.payload.get("store_content_hash"))
        for e in read_sup_entries(sup_path)
        if e.action == "TUPLE_DONE"
    ]


def _summary_events(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [e for e in logs if e.get("event") == "sweep_parallel.kernel_reuse_summary"]


# ────────────────────────── flag resolution (T5) ──────────────────────────


class TestResolveWorkerKernelCache:
    def test_unset_is_the_default_off(self) -> None:
        assert mod.resolve_worker_kernel_cache({}) is mod.WORKER_KERNEL_CACHE_DEFAULT
        assert mod.WORKER_KERNEL_CACHE_DEFAULT is False

    def test_blank_is_the_default_off(self) -> None:
        assert mod.resolve_worker_kernel_cache({mod.WORKER_KERNEL_CACHE_ENV: "  "}) is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_enables(self, raw: str) -> None:
        assert mod.resolve_worker_kernel_cache({mod.WORKER_KERNEL_CACHE_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_disables(self, raw: str) -> None:
        assert mod.resolve_worker_kernel_cache({mod.WORKER_KERNEL_CACHE_ENV: raw}) is False

    def test_unrecognised_fails_safe_off_and_warns_once(self) -> None:
        env = {mod.WORKER_KERNEL_CACHE_ENV: "maybe"}
        with capture_logs() as logs:
            assert mod.resolve_worker_kernel_cache(env) is False
            assert mod.resolve_worker_kernel_cache(env) is False
        warned = [
            e
            for e in logs
            if e.get("event") == "sweep_parallel.worker_kernel_cache.flag_unrecognised"
        ]
        assert len(warned) == 1  # warn-ONCE per process
        assert warned[0]["requested_env"] == repr("maybe")

    def test_warn_once_reset_seam(self) -> None:
        env = {mod.WORKER_KERNEL_CACHE_ENV: "junk"}
        with capture_logs() as logs:
            mod.resolve_worker_kernel_cache(env)
            mod.reset_worker_kernel_flag_warnings_for_tests()
            mod.resolve_worker_kernel_cache(env)
        assert len(_summary_events(logs)) == 0
        assert (
            len(
                [
                    e
                    for e in logs
                    if e.get("event") == "sweep_parallel.worker_kernel_cache.flag_unrecognised"
                ]
            )
            == 2
        )

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        assert mod.resolve_worker_kernel_cache() is True


class TestResolveWorkerKernelCapacity:
    def test_default_is_the_orchestrator_law(self) -> None:
        grid = _grid(
            proximities=[None, ProximityParams(pivot_proximity_bps=25)],
        )
        assert mod.worker_kernel_capacity_default(grid) == 2 * len(grid.fib_anchors)
        assert mod.resolve_worker_kernel_capacity(grid, {}) == mod.worker_kernel_capacity_default(
            grid
        )

    def test_single_axis_grid_floors_at_one(self) -> None:
        grid = _grid()
        assert mod.worker_kernel_capacity_default(grid) == 1

    def test_blank_env_is_the_default(self) -> None:
        grid = _grid()
        assert mod.resolve_worker_kernel_capacity(grid, {mod.WORKER_KERNEL_CAPACITY_ENV: " "}) == 1

    def test_valid_override_is_verbatim(self) -> None:
        grid = _grid()
        env = {mod.WORKER_KERNEL_CAPACITY_ENV: "7"}
        assert mod.resolve_worker_kernel_capacity(grid, env) == 7

    @pytest.mark.parametrize("raw", ["zero", "0", "-3", "2.5"])
    def test_invalid_falls_back_to_default_and_warns(self, raw: str) -> None:
        grid = _grid()
        with capture_logs() as logs:
            got = mod.resolve_worker_kernel_capacity(grid, {mod.WORKER_KERNEL_CAPACITY_ENV: raw})
        assert got == mod.worker_kernel_capacity_default(grid)
        warned = [
            e for e in logs if e.get("event") == "sweep_parallel.worker_kernel_capacity.invalid"
        ]
        assert len(warned) == 1
        assert warned[0]["requested_env"] == repr(raw)

    @pytest.mark.parametrize("raw", ["65", "100000000"])
    def test_above_ceiling_clamps_and_warns(self, raw: str) -> None:
        """Fix-round-1 R3 fold: a pathological-large override cannot disable
        the LRU eviction — it clamps to WORKER_KERNEL_CAPACITY_MAX, loudly."""
        grid = _grid()
        with capture_logs() as logs:
            got = mod.resolve_worker_kernel_capacity(grid, {mod.WORKER_KERNEL_CAPACITY_ENV: raw})
        assert got == mod.WORKER_KERNEL_CAPACITY_MAX == 64
        clamped = [
            e for e in logs if e.get("event") == "sweep_parallel.worker_kernel_capacity.clamped"
        ]
        assert len(clamped) == 1
        assert clamped[0]["requested_env"] == repr(raw)
        assert clamped[0]["clamped_to"] == mod.WORKER_KERNEL_CAPACITY_MAX

    def test_ceiling_boundary_is_verbatim_and_silent(self) -> None:
        grid = _grid()
        env = {mod.WORKER_KERNEL_CAPACITY_ENV: str(mod.WORKER_KERNEL_CAPACITY_MAX)}
        with capture_logs() as logs:
            got = mod.resolve_worker_kernel_capacity(grid, env)
        assert got == mod.WORKER_KERNEL_CAPACITY_MAX
        hygiene = [
            e
            for e in logs
            if str(e.get("event", "")).startswith("sweep_parallel.worker_kernel_capacity")
        ]
        assert hygiene == []

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mod.WORKER_KERNEL_CAPACITY_ENV, "5")
        assert mod.resolve_worker_kernel_capacity(_grid()) == 5

    def test_grid_derived_default_clamps_at_the_ceiling(self) -> None:
        """Fix-round-2 (refuter-2 NIT): the DERIVED default rides the same
        64 ceiling as the env override — a hypothetical 70-proximity grid can
        no longer bypass the per-worker memory bound silently (deliberate
        deviation from the sequential path's unclamped sizing: the RSS
        multiplies by pool size here)."""
        grid = _grid(proximities=[ProximityParams(i + 1, i + 1, i + 1) for i in range(70)])
        assert mod.worker_kernel_capacity_default(grid) == mod.WORKER_KERNEL_CAPACITY_MAX == 64
        assert mod.resolve_worker_kernel_capacity(grid, {}) == 64


# ────────────────────────── worker init (T5 / T6) ──────────────────────────


class TestWorkerInitKernelCache:
    def test_on_constructs_cache_and_memo(self, tmp_path: Path) -> None:
        ctx = mod._WorkerContext(
            grid=_grid(),
            root=tmp_path,
            worker_kernel_cache=True,
            worker_kernel_capacity=3,
        )
        mod._worker_init(ctx)
        cache = mod._WORKER_KERNEL_CACHE
        assert isinstance(cache, ResidentKernelCache)
        assert isinstance(mod._WORKER_BARS_MEMO, OrderedDict)
        assert mod._WORKER_BARS_MEMO_WARNED is False
        # Capacity travelled from the ctx (T6 bound is enforced by the cache).
        assert len(cache) == 0

    def test_off_constructs_no_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The OFF arm never builds a cache object (spy-proven zero constructions)."""
        calls: list[object] = []

        class _Spy:
            def __init__(self, **kwargs: object) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(mod, "ResidentKernelCache", _Spy)
        mod._worker_init(mod._WorkerContext(grid=_grid(), root=tmp_path))
        assert calls == []
        assert mod._WORKER_KERNEL_CACHE is None
        assert mod._WORKER_BARS_MEMO is None

    def test_reinit_resets_the_seen_key_ledger_and_warn_latch(self, tmp_path: Path) -> None:
        """Fix-round-7 (final-refuter B1 MINOR-1): the two ``_worker_init``
        reset lines ``_WORKER_BARS_KEYS_SEEN = set()`` and
        ``_WORKER_BARS_MEMO_WARNED = False`` have TEETH — severing either
        (a self-assign no-op) fails HERE. Production-inert under the
        spawn-only contract (a spawn worker's globals start fresh); the
        resets defend exactly the in-process ``_worker_init`` reuse this
        suite performs, so the pin pollutes the module globals in-process
        and asserts the re-init clears them alongside the existing
        cache/memo constructions."""
        stale_cache = ResidentKernelCache(capacity=1)
        mod._WORKER_KERNEL_CACHE = stale_cache
        mod._WORKER_BARS_MEMO = OrderedDict({("JUNK", "1minute"): _BARS})
        mod._WORKER_BARS_KEYS_SEEN = {("JUNK", "1minute")}
        mod._WORKER_BARS_MEMO_WARNED = True
        mod._worker_init(
            mod._WorkerContext(
                grid=_grid(), root=tmp_path, worker_kernel_cache=True, worker_kernel_capacity=2
            )
        )
        assert not mod._WORKER_BARS_KEYS_SEEN
        assert mod._WORKER_BARS_MEMO_WARNED is False
        fresh = mod._WORKER_KERNEL_CACHE
        assert isinstance(fresh, ResidentKernelCache)
        assert fresh is not stale_cache
        memo = mod._WORKER_BARS_MEMO
        assert isinstance(memo, OrderedDict)
        assert not memo

    def test_rayon_determinism_pin_survives_cache_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The RAYON_NUM_THREADS=1 pin is LOAD-BEARING (the published perf
        law's walk-cost coefficient was measured under it) — the kernel-cache
        block is APPENDED after it and must never disturb it."""
        for var in ("OMP_NUM_THREADS", "RAYON_NUM_THREADS", "POLARS_MAX_THREADS"):
            monkeypatch.delenv(var, raising=False)
        mod._worker_init(
            mod._WorkerContext(
                grid=_grid(), root=tmp_path, worker_kernel_cache=True, worker_kernel_capacity=1
            )
        )
        assert os.environ["RAYON_NUM_THREADS"] == "1"
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["POLARS_MAX_THREADS"] == "1"


# ─────────────────── driver ctor OFF-path capacity gate ───────────────────


class TestDriverOffPathCapacitySilence:
    @pytest.mark.parametrize("raw", ["zero", "100000000"])
    def test_off_run_never_resolves_capacity_or_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Fix-round-7 (final-refuter B1 MINOR-2): the ctor's flag-gated
        capacity resolution has TEETH — "an OFF run never emits the
        capacity-hygiene warning for an env var that would not be
        consumed". Flag OFF + a junk/over-ceiling
        ``BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY`` must yield ZERO
        ``worker_kernel_capacity.*`` events across a driver construction
        AND zero resolver calls (a delegating spy — the
        ``test_off_constructs_no_cache`` discipline); the carried capacity
        pins at the inert ``1``. An unconditional resolution (the FM7
        mutant) fails BOTH asserts."""
        monkeypatch.delenv(mod.WORKER_KERNEL_CACHE_ENV, raising=False)
        monkeypatch.setenv(mod.WORKER_KERNEL_CAPACITY_ENV, raw)
        calls: list[object] = []
        real = mod.resolve_worker_kernel_capacity

        def _spy(grid: SweepGridConfig, env: dict[str, str] | None = None) -> int:
            calls.append(grid)
            return real(grid, env)

        monkeypatch.setattr(mod, "resolve_worker_kernel_capacity", _spy)
        grid = _grid()
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ingest,
            SweepAuditWriter.open(tmp_path / "s.jsonl") as sup,
            SweepCache.open(tmp_path / "cache", bloom_capacity=10_000) as cache,
        ):
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=SweepCheckpointStore(
                    path=tmp_path / "ckpt.json",
                    sweep_id=grid_identity(grid),
                    total_combos=count_sweep_tuples(grid),
                ),
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            with capture_logs() as logs:
                driver = mod.ParallelGridDriver(
                    grid,
                    bars_for=_bars_for,
                    root=tmp_path / "out",
                    ingest_audit=ingest,
                    guards=guards,
                    workers=2,
                    stall_seconds=300.0,
                )
        assert calls == []
        hygiene = [
            e
            for e in logs
            if str(e.get("event", "")).startswith("sweep_parallel.worker_kernel_capacity")
        ]
        assert hygiene == []
        assert driver._worker_kernel_cache is False
        assert driver._worker_kernel_capacity == 1


# ────────────────────────── bars-intern memo (T4) ──────────────────────────


class TestInternBars:
    def _memo_on(self) -> None:
        mod._WORKER_BARS_MEMO = OrderedDict()
        mod._WORKER_BARS_KEYS_SEEN = set()
        mod._WORKER_BARS_MEMO_WARNED = False

    def test_memo_off_returns_bars_untouched(self) -> None:
        mod._WORKER_BARS_MEMO = None
        bars = list(_BARS)
        assert mod._intern_bars("NIFTY", "1minute", bars) is bars

    def test_empty_bars_never_memoized(self) -> None:
        self._memo_on()
        empty: list[CandleBar] = []
        assert mod._intern_bars("NIFTY", "1minute", empty) is empty
        assert not mod._WORKER_BARS_MEMO

    def test_hit_returns_the_resident_copy(self) -> None:
        self._memo_on()
        first = list(_BARS)
        fresh = list(_BARS)  # a value-identical "fresh pickled copy"
        assert mod._intern_bars("NIFTY", "1minute", first) is first
        assert mod._intern_bars("NIFTY", "1minute", fresh) is first  # interned!

    @pytest.mark.parametrize(
        "mutant",
        ["shorter", "first_bar", "mid_bar", "last_bar"],
    )
    def test_mismatch_degrades_to_fresh_and_warns_once(self, mutant: str) -> None:
        self._memo_on()
        first = list(_BARS)
        mod._intern_bars("NIFTY", "1minute", first)
        if mutant == "shorter":
            fresh = first[:-1]
        elif mutant == "first_bar":
            fresh = [_bar(0, 0, 999.0, 999.5), *first[1:]]
        elif mutant == "mid_bar":
            # Fix-round-2 (refuter-1 A-2): the probe now sees the midpoint too.
            mid = len(first) // 2
            fresh = [*first[:mid], _bar(*divmod(mid, 60), 999.0, 999.5), *first[mid + 1 :]]
        else:
            fresh = [*first[:-1], _bar(3, 59, 999.0, 999.5)]
        with capture_logs() as logs:
            got = mod._intern_bars("NIFTY", "1minute", fresh)
            # The disable is permanent for the worker: later calls run the
            # legacy path — bars returned untouched, never a second warn.
            got_disabled = mod._intern_bars("NIFTY", "1minute", first)
            got2 = mod._intern_bars("NIFTY", "1minute", fresh)
        assert got is fresh  # the FRESH bars are used, never the stale resident
        assert got_disabled is first
        assert got2 is fresh
        # Fail-safe OFF (fix-round-1 R1 fold): the memo is disabled outright.
        assert mod._WORKER_BARS_MEMO is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1  # warn-once per worker process
        assert warned[0]["reason"] == "resident_value_mismatch"

    def test_revisit_after_eviction_disables_even_value_equal(self) -> None:
        """Fix-round-2 (refuter-1 A-1, unit arm): a key re-arriving AFTER its
        memo entry was LRU-evicted can never be identity-reconciled with the
        object its kernels were built from — even a value-EQUAL fresh copy
        disables (conservative; the reference is gone, so identity is
        unverifiable). Pre-fix this arm silently RE-SEEDED with no comparison.
        """
        self._memo_on()
        mod._WORKER_KERNEL_CACHE = ResidentKernelCache(capacity=8)
        a = list(_BARS)
        mod._intern_bars("A", "1minute", a)
        mod._intern_bars("B", "1minute", list(_BARS))
        mod._intern_bars("C", "1minute", list(_BARS))  # evicts A (LRU)
        memo = mod._WORKER_BARS_MEMO
        assert memo is not None
        assert ("A", "1minute") not in memo
        with capture_logs() as logs:
            got = mod._intern_bars("A", "1minute", list(_BARS))  # value-EQUAL revisit
        assert got is not a  # the shipped copy is used, never a resurrected resident
        assert mod._WORKER_BARS_MEMO is None
        assert mod._WORKER_KERNEL_CACHE is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1
        assert warned[0]["reason"] == "revisit_after_eviction"

    def test_mismatch_with_latched_warn_still_disables_silently(self) -> None:
        """Belt-and-braces arm: with the warn-once latch ALREADY set (only a
        future memo re-enable path could reach this — the disable makes a
        second natural mismatch per worker unreachable), a mismatch still
        fail-safe-disables both globals, without a second WARN."""
        self._memo_on()
        mod._WORKER_BARS_MEMO_WARNED = True
        first = list(_BARS)
        mod._intern_bars("NIFTY", "1minute", first)
        with capture_logs() as logs:
            got = mod._intern_bars("NIFTY", "1minute", first[:-1])
        assert got is not first
        assert mod._WORKER_BARS_MEMO is None
        assert mod._WORKER_KERNEL_CACHE is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert warned == []

    def test_disable_writes_the_warn_once_latch(self) -> None:
        """Fix-round-8 (FC-N3 kill): the disable WRITES the module warn-once
        latch — the belt-and-braces arm the latch exists for. A second DIRECT
        disable (reachable naturally only via a future memo re-enable path —
        any disable Nones the memo, so ``_intern_bars`` short-circuits) must
        find the latch True and stay silent; with the latch write severed the
        forced second disable would warn AGAIN, breaking the warn-once law.
        """
        self._memo_on()  # latch explicitly False
        with capture_logs() as logs:
            mod._disable_worker_kernel_cache("NIFTY", "1minute", "resident_value_mismatch")
            assert mod._WORKER_BARS_MEMO_WARNED is True  # the WRITE arm, pinned
            mod._disable_worker_kernel_cache("NIFTY", "1minute", "resident_value_mismatch")
        assert mod._WORKER_BARS_MEMO is None
        assert mod._WORKER_KERNEL_CACHE is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1  # the second disable is silent

    def test_mismatch_disables_cache_and_memo(self) -> None:
        """Fix-round-1 R1 fold: a mismatch fail-safe-disables BOTH globals.

        ResidentKernelCache exposes no per-key eviction (call-only seam), so
        the degrade sets the kernel cache AND the memo to ``None`` — every
        previously-built kernel becomes unreachable for the rest of the
        worker process.
        """
        self._memo_on()
        mod._WORKER_KERNEL_CACHE = ResidentKernelCache(capacity=1)
        first = list(_BARS)
        mod._intern_bars("NIFTY", "1minute", first)
        mod._intern_bars("NIFTY", "1minute", first[:-1])  # mismatch → fail-safe OFF
        assert mod._WORKER_BARS_MEMO is None
        assert mod._WORKER_KERNEL_CACHE is None

    def test_lru_bound_capacity_two(self) -> None:
        self._memo_on()
        a, b, c = list(_BARS), list(_BARS), list(_BARS)
        mod._intern_bars("A", "1minute", a)
        mod._intern_bars("B", "1minute", b)
        mod._intern_bars("C", "1minute", c)  # evicts A (LRU)
        memo = mod._WORKER_BARS_MEMO
        assert memo is not None
        assert len(memo) == mod._BARS_MEMO_CAPACITY == 2
        assert set(memo) == {("B", "1minute"), ("C", "1minute")}

    def test_lru_recency_hit_protects_the_hit_key(self) -> None:
        """Fix-round-5 (refuter R4-1 M20 kill): a value-probe HIT refreshes the
        key's recency (``memo.move_to_end``) — the memo is a genuine LRU, not
        a FIFO. Seed A, B; HIT A; seed C: the eviction must take B (the
        genuinely least-recently-USED key), never the just-hit A — and a
        follow-up A arrival is served the RESIDENT object with the mechanism
        still ENABLED. Under the M20 mutant (``move_to_end`` deleted) C evicts
        A instead, and the follow-up A arrival spuriously fires the
        ``revisit_after_eviction`` fail-safe disable on a key in ACTIVE reuse.
        """
        self._memo_on()
        a = list(_BARS)
        mod._intern_bars("A", "1minute", a)
        mod._intern_bars("B", "1minute", list(_BARS))
        assert mod._intern_bars("A", "1minute", list(_BARS)) is a  # HIT refreshes recency
        mod._intern_bars("C", "1minute", list(_BARS))  # must evict B, not the just-hit A
        memo = mod._WORKER_BARS_MEMO
        assert memo is not None
        assert set(memo) == {("A", "1minute"), ("C", "1minute")}
        with capture_logs() as logs:
            got = mod._intern_bars("A", "1minute", list(_BARS))
        assert got is a  # the resident A copy is served — still a live memo entry
        assert mod._WORKER_BARS_MEMO is not None  # mechanism still ENABLED
        assert mod._WORKER_KERNEL_CACHE is None  # untouched (never set in this test)
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert warned == []  # no spurious revisit_after_eviction disable

    def test_len_mismatch_alone_disables_never_serves(self) -> None:
        """Fix-round-9 (FD-M4 kill): the value probe's LEN arm has its OWN
        teeth. A FEWER-bar arrival that AGREES with the resident at first /
        its-own-mid / last — so ONLY the length comparison can catch it —
        must take the mismatch path (fail-safe disable + one WARN), never a
        resident serve (the mutant would sim the 5-bar resident where 3 bars
        were shipped, mechanism silently still enabled). A strict PREFIX
        cannot build this fixture (distinct timestamps make ``arrival[-1] ==
        resident[-1]`` impossible), so the probe-agreeing shape is the
        first/mid/last SUBSAMPLE — the FD-1 executed-probe fixture.
        """
        self._memo_on()
        resident = list(_BARS[:5])
        mod._intern_bars("NIFTY", "1minute", resident)
        arrival = [resident[0], resident[2], resident[4]]  # 3 bars, probe-agreeing
        assert arrival[0] == resident[0]
        assert arrival[len(arrival) // 2] == resident[len(resident) // 2]
        assert arrival[-1] == resident[-1]
        with capture_logs() as logs:
            got = mod._intern_bars("NIFTY", "1minute", arrival)
        assert got is arrival  # the shipped 3-bar copy — never the 5-bar resident
        assert mod._WORKER_BARS_MEMO is None  # fail-safe disable, not a serve
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1
        assert warned[0]["reason"] == "resident_value_mismatch"


# ─────────────── in-process worker reuse + counters (T1/T3/T6) ───────────────


def _worker_ctx_on(
    tmp_path: Path,
    grid: SweepGridConfig,
    *,
    capacity: int = 4,
    roll: dict[str, tuple[int, ...]] | None = None,
) -> mod._WorkerContext:
    return mod._WorkerContext(
        grid=grid,
        root=tmp_path / "out",
        roll_schedule_by_instrument=roll,
        worker_kernel_cache=True,
        worker_kernel_capacity=capacity,
    )


class TestRunTupleInWorkerKernelCache:
    def test_reuse_within_a_worker_and_counters(self, tmp_path: Path) -> None:
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        tuples = list(iter_sweep_tuples(grid))
        with capture_logs() as logs:
            first = mod._run_tuple_in_worker(0, tuples[0], _BARS)
            second = mod._run_tuple_in_worker(1, tuples[1], _BARS)
        cache = mod._WORKER_KERNEL_CACHE
        assert cache is not None
        # One key (one instrument × tf × proximity × anchor) → ONE build, one hit.
        assert cache.builds == 1
        assert cache.hits == 1
        # The additive telemetry fields are stamped (cumulative snapshots).
        assert first.worker_pid == os.getpid() == second.worker_pid
        assert (first.kernel_builds, first.kernel_hits) == (1, 0)
        assert (second.kernel_builds, second.kernel_hits) == (1, 1)
        # ONE bounded worker_kernel_built INFO — the MISS logged, the hit silent.
        built = [e for e in logs if e.get("event") == "sweep_parallel.worker_kernel_built"]
        assert len(built) == 1
        assert built[0]["instrument"] == "NIFTY"
        assert built[0]["roll"] is False
        # Fix-round-8 (FC-N1 kill): the FULL payload is pinned — every field is
        # the truth at EMISSION time (after the first tuple's miss: one build,
        # zero hits, one resident kernel), never a stale or fabricated stamp.
        assert built[0]["tf"] == tuples[0].tf.label == "1minute"
        assert built[0]["proximity"] == repr(tuples[0].proximity) == "None"
        assert built[0]["fib_anchor"] == tuples[0].fib_anchor.value
        assert built[0]["builds"] == 1
        assert built[0]["hits"] == 0
        assert built[0]["resident"] == 1 == len(cache)
        # Both tuples produced real summaries (the reuse is byte-safe).
        assert first.summary is not None
        assert second.summary is not None

    def test_off_results_carry_default_none_telemetry(self, tmp_path: Path) -> None:
        grid = _grid()
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
        result = mod._run_tuple_in_worker(0, next(iter_sweep_tuples(grid)), _BARS)
        assert result.worker_pid is None
        assert result.kernel_builds is None
        assert result.kernel_hits is None

    def test_failed_tuple_still_stamps_counters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine import sweep_run as sr

        def _boom(*_a: object, **_k: object) -> None:
            raise ValueError("combo blew up")

        monkeypatch.setattr(sr, "metrics_from_trades", _boom)
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        with capture_logs() as logs:
            result = mod._run_tuple_in_worker(0, next(iter_sweep_tuples(grid)), _BARS)
        assert result.failed_error is not None
        assert result.worker_pid == os.getpid()
        assert result.kernel_builds == 1  # the kernel HAD been built before the failure
        assert result.kernel_hits == 0
        built = [e for e in logs if e.get("event") == "sweep_parallel.worker_kernel_built"]
        assert len(built) == 1  # the miss is logged on the failed path too

    def test_distinct_instruments_key_distinct_kernels(self, tmp_path: Path) -> None:
        """T3 (#1062 class): spot + NSE-NIFTY-FUT coexisting in ONE worker cache."""
        grid = _grid(instruments=["NIFTY", "NSE-NIFTY-FUT"], k_values=[1])
        roll = {"NSE-NIFTY-FUT": _FUT_SCHEDULE}
        mod._worker_init(_worker_ctx_on(tmp_path, grid, roll=roll))
        tuples = list(iter_sweep_tuples(grid))
        spot = [t for t in tuples if t.instrument == "NIFTY"]
        fut = [t for t in tuples if t.instrument == "NSE-NIFTY-FUT"]
        with capture_logs() as logs:
            assert mod._run_tuple_in_worker(0, spot[0], _BARS).summary is not None
            assert mod._run_tuple_in_worker(1, fut[0], _BARS).summary is not None
            assert mod._run_tuple_in_worker(2, spot[1], _BARS).summary is not None
            assert mod._run_tuple_in_worker(3, fut[1], _BARS).summary is not None
        cache = mod._WORKER_KERNEL_CACHE
        assert cache is not None
        assert cache.builds == 2  # one kernel per INSTRUMENT (schedule in the key)
        assert cache.hits == 2
        assert len(cache) == 2
        built = [e for e in logs if e.get("event") == "sweep_parallel.worker_kernel_built"]
        assert [e["roll"] for e in built] == [False, True]

    def test_wrong_schedule_kernel_trips_the_fail_fast_through_the_worker(
        self, tmp_path: Path
    ) -> None:
        """T3 belt: the sweep_run schedule fail-fast still guards the worker path.

        Unreachable through the REAL cache (the schedule is in its key) — so a
        stub cache serves a hand-built WRONG-schedule kernel to prove the
        run_persisted_sweep guard refuses it loudly (and propagates: it is not
        a SweepComboError, so the worker never converts it to a failed marker).
        """
        grid = _grid(instruments=["NSE-NIFTY-FUT"], k_values=[1], directions=[Side.BUY])
        roll = {"NSE-NIFTY-FUT": _FUT_SCHEDULE}
        mod._worker_init(_worker_ctx_on(tmp_path, grid, roll=roll))
        wrong = ResidentSweepKernel(
            _BARS,
            proximity=None,
            prior_n=grid.prior_n,
            roll_schedule=_WRONG_SCHEDULE,
        )

        class _StubCache:
            builds = 1
            hits = 0

            def get(self, *_a: object, **_k: object) -> ResidentSweepKernel:
                return wrong

            def __len__(self) -> int:
                return 1

        mod._WORKER_KERNEL_CACHE = _StubCache()  # type: ignore[assignment]
        with pytest.raises(ValueError, match="must match this tuple's schedule"):
            mod._run_tuple_in_worker(0, next(iter_sweep_tuples(grid)), _BARS)

    def test_capacity_one_thrash_is_correct_and_bounded(self, tmp_path: Path) -> None:
        """T2/T6 in-process arm: capacity 1 under 2 keys thrashes (more builds),
        results stay real, and the cache never exceeds its capacity."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"], k_values=[1], directions=[Side.BUY])
        mod._worker_init(_worker_ctx_on(tmp_path, grid, capacity=1))
        tuples = list(iter_sweep_tuples(grid))
        # Interleave the two instruments so every access misses under LRU-1.
        order = [tuples[0], tuples[1], tuples[0], tuples[1]]
        for i, t in enumerate(order):
            assert mod._run_tuple_in_worker(i, t, _BARS).summary is not None
        cache = mod._WORKER_KERNEL_CACHE
        assert cache is not None
        assert cache.builds == 4  # 100% miss on the cycle — the LRU pathology
        assert cache.hits == 0
        assert len(cache) == 1  # T6: never exceeds capacity

    def test_mismatch_never_serves_a_stale_kernel(self, tmp_path: Path) -> None:
        """Fix-round-1 R1 fold: value-different bars for an already-built key
        can never be paired with the stale resident kernel.

        The R1 hypothetical: worker builds K1 on copy1 for key κ; a buggy
        parent later ships value-DIFFERENT bars for the same (instrument, tf);
        pre-fix a later same-κ hit served K1's bar_bits (built on copy1) while
        the trade sim ran on the fresh bars — a silently mixed result. The
        fix disables the whole per-worker mechanism on mismatch, so the stale
        kernel is UNREACHABLE and the tuple runs the legacy byte-correct
        per-tuple build on the bars it actually simulates.
        """
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        tuples = list(iter_sweep_tuples(grid))
        copy1 = list(_BARS)
        first = mod._run_tuple_in_worker(0, tuples[0], copy1)
        assert first.summary is not None
        stale_cache = mod._WORKER_KERNEL_CACHE
        assert stale_cache is not None
        assert (stale_cache.builds, stale_cache.hits) == (1, 0)
        # A value-DIFFERENT copy for the SAME (instrument, tf) key.
        mutated = [_bar(0, 0, 999.0, 999.5), *copy1[1:]]
        with capture_logs() as logs:
            second = mod._run_tuple_in_worker(1, tuples[1], list(mutated))
        assert second.summary is not None
        # The degrade disabled the cache + memo for the rest of the worker...
        assert mod._WORKER_KERNEL_CACHE is None
        assert mod._WORKER_BARS_MEMO is None
        # ...so the STALE kernel was never consulted (counters frozen — a
        # pre-fix hit on K1 would read hits == 1 here)...
        assert (stale_cache.builds, stale_cache.hits) == (1, 0)
        # ...and the mismatching tuple ran the legacy path (no telemetry).
        assert second.worker_pid is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1
        # Byte-proof: the mismatching tuple's result equals a fresh legacy
        # (no-cache) reference computed on the SAME mutated bars.
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "ref"))
        reference = mod._run_tuple_in_worker(1, tuples[1], list(mutated))
        assert reference.summary == second.summary

    def test_evicted_key_revisit_never_serves_a_stale_kernel(self, tmp_path: Path) -> None:
        """Fix-round-2 regression (refuter-1 A-1, the EXECUTED repro ported).

        The A-1 attack: a worker sees three ``(instrument, tf)`` keys, so the
        capacity-2 memo EVICTS the first while its kernel stays resident in
        the (larger) kernel cache; a later dispatch for that key carrying
        value-DIFFERENT bars then re-seeded with NO comparison — the stale
        kernel HIT silently (zero warns, cache ON, mixed
        bar_bits-vs-trade-sim result). Post-fix the seen-key ledger disables
        the whole mechanism on the revisit: the stale kernel is unreachable,
        the tuple runs the legacy build on the bars actually shipped.
        """
        grid = _grid(instruments=["NIFTY", "BANKNIFTY", "SENSEX"])
        mod._worker_init(_worker_ctx_on(tmp_path, grid, capacity=8))
        tuples = list(iter_sweep_tuples(grid))
        by_inst: dict[str, list[Any]] = {}
        for t in tuples:
            by_inst.setdefault(t.instrument, []).append(t)
        copy1 = list(_BARS)
        assert mod._run_tuple_in_worker(0, by_inst["NIFTY"][0], copy1).summary is not None
        assert mod._run_tuple_in_worker(1, by_inst["BANKNIFTY"][0], list(_BARS)).summary is not None
        assert mod._run_tuple_in_worker(2, by_inst["SENSEX"][0], list(_BARS)).summary is not None
        memo = mod._WORKER_BARS_MEMO
        assert memo is not None
        assert ("NIFTY", "1minute") not in memo  # evicted — the A-1 precondition
        stale_cache = mod._WORKER_KERNEL_CACHE
        assert stale_cache is not None
        assert (stale_cache.builds, stale_cache.hits) == (3, 0)
        # The revisit ships value-DIFFERENT bars for the evicted NIFTY key.
        mutated = [_bar(0, 0, 999.0, 999.5), *copy1[1:]]
        with capture_logs() as logs:
            revisit = mod._run_tuple_in_worker(3, by_inst["NIFTY"][1], list(mutated))
        assert revisit.summary is not None
        # The whole mechanism disabled; the stale kernel's counters FROZEN
        # (pre-fix a HIT registered here — hits read 1) and no telemetry.
        assert mod._WORKER_KERNEL_CACHE is None
        assert mod._WORKER_BARS_MEMO is None
        assert (stale_cache.builds, stale_cache.hits) == (3, 0)
        assert revisit.worker_pid is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1
        assert warned[0]["reason"] == "revisit_after_eviction"
        # Byte-proof: equals a fresh legacy (no-cache) reference on the SAME
        # mutated bars — the tuple computed on what the parent shipped.
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "ref"))
        reference = mod._run_tuple_in_worker(3, by_inst["NIFTY"][1], list(mutated))
        assert reference.summary == revisit.summary

    def test_middle_bar_divergence_disables_never_substitutes(self, tmp_path: Path) -> None:
        """Fix-round-2 regression (refuter-1 A-2, ported): a same-key dispatch
        whose bars differ ONLY at the midpoint (len/first/last identical) is
        caught by the widened len + first/mid/last probe → fail-safe disable,
        and the tuple runs on the bars ACTUALLY shipped (pre-fix the resident
        copy was silently substituted — ON ≠ OFF with zero warns)."""
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        tuples = list(iter_sweep_tuples(grid))
        copy1 = list(_BARS)
        assert mod._run_tuple_in_worker(0, tuples[0], copy1).summary is not None
        mid = len(copy1) // 2
        shipped = [*copy1[:mid], _bar(*divmod(mid, 60), 250.0, 249.0), *copy1[mid + 1 :]]
        assert shipped[0] == copy1[0] and shipped[-1] == copy1[-1] and len(shipped) == len(copy1)
        with capture_logs() as logs:
            second = mod._run_tuple_in_worker(1, tuples[1], list(shipped))
        assert second.summary is not None
        assert mod._WORKER_KERNEL_CACHE is None
        warned = [e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"]
        assert len(warned) == 1
        assert warned[0]["reason"] == "resident_value_mismatch"
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "ref"))
        reference = mod._run_tuple_in_worker(1, tuples[1], list(shipped))
        assert reference.summary == second.summary

    def test_probe_blind_interior_mutation_substitutes_self_consistently(
        self, tmp_path: Path
    ) -> None:
        """HONEST-residual pin (disclosed, deliberate): a value-different copy
        agreeing at len/first/mid/last passes the probe, so the REGISTERED
        resident copy is served — the serve stays identity-EXACT (kernel and
        sim share ONE object; never a mixed bar_bits-vs-trade-sim pairing),
        byte-equal to a legacy run on the RESIDENT bars. The substitution
        itself is unreachable under the deterministic ``bars_for`` contract
        and cannot be closed by any O(1) probe (full value equality would be
        O(n_bars) per tuple)."""
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        tuples = list(iter_sweep_tuples(grid))
        copy1 = list(_BARS)
        assert mod._run_tuple_in_worker(0, tuples[0], copy1).summary is not None
        # Index 1 is neither first, mid nor last — probe-blind by design.
        shipped = [copy1[0], _bar(0, 1, 999.0, 999.5), *copy1[2:]]
        with capture_logs() as logs:
            second = mod._run_tuple_in_worker(1, tuples[1], list(shipped))
        assert second.summary is not None
        cache = mod._WORKER_KERNEL_CACHE
        assert cache is not None  # still enabled — the probe cannot see index 1
        assert cache.hits == 1  # the resident kernel served (self-consistent)
        assert [
            e for e in logs if e.get("event") == "sweep_parallel.worker_bars_memo_mismatch"
        ] == []
        # Self-consistency proof: equals a legacy reference on the RESIDENT bars.
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "ref"))
        reference = mod._run_tuple_in_worker(1, tuples[1], list(copy1))
        assert reference.summary == second.summary

    def test_bars_interned_across_same_key_dispatches(self, tmp_path: Path) -> None:
        """T4 integration: a fresh value-equal pickled copy is swapped for the
        resident copy, so the cached kernel and every later dispatch share ONE
        underlying bars object."""
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        tuples = list(iter_sweep_tuples(grid))
        first_bars = list(_BARS)
        fresh_copy = list(_BARS)
        mod._run_tuple_in_worker(0, tuples[0], first_bars)
        mod._run_tuple_in_worker(1, tuples[1], fresh_copy)
        memo = mod._WORKER_BARS_MEMO
        assert memo is not None
        assert memo[("NIFTY", "1minute")] is first_bars  # the FIRST copy stays resident

    def test_second_dispatch_consumes_the_interned_resident(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-round-9 (FD-M1 kill): the intern-CONSUMPTION law end-to-end —
        the bars object that actually FLOWS INTO ``run_one_tuple`` on a
        same-key second dispatch IS the interned resident, on BOTH
        consumption paths: the ``bars_for`` closure feeding the trade sim AND
        the ``kernel_cache.get`` bars argument feeding the kernel. The memo
        side-effect pin above cannot see this (under the severed
        ``bars = _intern_bars(...)`` reassignment the memo still holds the
        first copy while the sim runs the FRESH copy against the RESIDENT
        kernel's ``bar_bits`` — the exact mixed pairing the identity-serve
        law forbids, and the A.4 memory bound severed at its delivery point).
        """
        grid = _grid()
        mod._worker_init(_worker_ctx_on(tmp_path, grid))
        real_cache = mod._WORKER_KERNEL_CACHE
        assert real_cache is not None
        kernel_bars: list[Sequence[CandleBar]] = []

        class _GetSpyCache:
            """Delegating wrapper recording every ``get``'s bars argument."""

            def get(self, bars: Sequence[CandleBar], **kwargs: Any) -> ResidentSweepKernel:
                kernel_bars.append(bars)
                assert real_cache is not None
                return real_cache.get(bars, **kwargs)

            @property
            def builds(self) -> int:
                assert real_cache is not None
                return real_cache.builds

            @property
            def hits(self) -> int:
                assert real_cache is not None
                return real_cache.hits

            def __len__(self) -> int:
                assert real_cache is not None
                return len(real_cache)

        mod._WORKER_KERNEL_CACHE = _GetSpyCache()  # type: ignore[assignment]
        sim_bars: list[Sequence[CandleBar]] = []
        real_run_one_tuple = mod.run_one_tuple

        def _spy_run_one_tuple(*args: Any, **kwargs: Any) -> Any:
            # The bars_for closure yields the SAME local `bars` the worker
            # passes to the sim — capture exactly what run_one_tuple consumes.
            sim_bars.append(kwargs["bars_for"]("probe", "probe"))
            return real_run_one_tuple(*args, **kwargs)

        monkeypatch.setattr(mod, "run_one_tuple", _spy_run_one_tuple)
        tuples = list(iter_sweep_tuples(grid))
        first_bars = list(_BARS)
        fresh_copy = list(_BARS)  # a value-identical "fresh pickled copy"
        assert mod._run_tuple_in_worker(0, tuples[0], first_bars).summary is not None
        assert mod._run_tuple_in_worker(1, tuples[1], fresh_copy).summary is not None
        assert (real_cache.builds, real_cache.hits) == (1, 1)  # the kernel WAS reused
        # The first dispatch registered + consumed its own copy...
        assert sim_bars[0] is first_bars
        assert kernel_bars[0] is first_bars
        # ...and the second dispatch CONSUMED the interned resident — never
        # the fresh copy — on both the sim path and the kernel path.
        assert sim_bars[1] is first_bars
        assert kernel_bars[1] is first_bars
        assert sim_bars[1] is not fresh_copy


# ──────────── parent telemetry fold (fix-round-2, refuter-3 F3) ────────────


class TestKernelStatsFold:
    """Deterministic teeth for the per-pid element-wise-MAX telemetry fold.

    The pop-order race fix (`_fold_kernel_snapshot`): `done.pop()` on a
    completed-future set is arbitrary, so a worker's EARLIER (lower)
    cumulative snapshot can be folded AFTER its later (higher) one — a plain
    last-write would lower the frozen total. Previously pinned only
    probabilistically (refuter-3 F3: the last-write revert survived the whole
    suite); these unit pins fail deterministically on that revert.
    """

    def test_late_lower_snapshot_never_overwrites_an_earlier_higher_one(self) -> None:
        stats: dict[int, tuple[int, int]] = {}
        mod._fold_kernel_snapshot(stats, 4242, 5, 3)  # the HIGHER snapshot popped first
        mod._fold_kernel_snapshot(stats, 4242, 2, 1)  # the earlier LOWER one pops later
        assert stats[4242] == (5, 3)  # last-write would read (2, 1)

    def test_fold_is_element_wise_not_snapshot_wise(self) -> None:
        stats: dict[int, tuple[int, int]] = {}
        mod._fold_kernel_snapshot(stats, 7, 5, 1)
        mod._fold_kernel_snapshot(stats, 7, 2, 3)
        assert stats[7] == (5, 3)  # each counter keeps its OWN max

    def test_none_counters_fold_as_zero_and_never_lower(self) -> None:
        stats: dict[int, tuple[int, int]] = {7: (4, 9)}
        mod._fold_kernel_snapshot(stats, 7, None, None)
        assert stats[7] == (4, 9)
        mod._fold_kernel_snapshot(stats, 9, None, None)
        assert stats[9] == (0, 0)


# ─────────────── T1/T2/T3 — real-pool byte-identity goldens ───────────────


class TestByteIdentityOnVsOff:
    def test_mixed_key_grid_on_equals_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T1 HEADLINE: flag-ON parallel ≡ flag-OFF parallel, byte-for-byte,
        with the parent summary proving builds ≤ keys × workers and hits > 0."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        monkeypatch.delenv(mod.WORKER_KERNEL_CACHE_ENV, raising=False)
        off_root, off_audit, off_ckpt = _run_grid(tmp_path / "off", grid, workers=3)
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        with capture_logs() as logs:
            on_root, on_audit, on_ckpt = _run_grid(tmp_path / "on", grid, workers=3)
        assert _tree(off_root) == _tree(on_root)
        assert len(_tree(on_root)) > 0
        assert _ingest_lines(off_audit) == _ingest_lines(on_audit)
        assert off_ckpt == on_ckpt
        # Per-tuple store_content_hash equality (the T1 letter).
        assert _tuple_done_hashes(tmp_path / "off" / "sup.jsonl") == _tuple_done_hashes(
            tmp_path / "on" / "sup.jsonl"
        )
        summaries = _summary_events(logs)
        assert len(summaries) == 1  # ONE parent INFO at drain end
        builds_total = summaries[0]["builds_total"]
        hits_total = summaries[0]["hits_total"]
        assert isinstance(builds_total, int)
        assert isinstance(hits_total, int)
        # 2 (instrument, tf) keys × 2 EVAL workers — the A.3 arithmetic.
        assert 0 < builds_total <= 2 * 2
        # 8 tuples over ≤2 workers in 2 contiguous key blocks of 4: pigeonhole
        # guarantees some worker sees a key at least twice → at least one hit.
        assert hits_total > 0
        assert builds_total + hits_total == count_sweep_tuples(grid)

    def test_capacity_one_thrash_bytes_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T2: an operator-shrunk capacity trades builds for RSS, NEVER bytes."""
        grid = _grid(
            proximities=[None, ProximityParams(pivot_proximity_bps=25)],
            k_values=[1],
        )
        monkeypatch.delenv(mod.WORKER_KERNEL_CACHE_ENV, raising=False)
        off_root, _off_audit, _ = _run_grid(tmp_path / "off", grid, workers=3)
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        monkeypatch.setenv(mod.WORKER_KERNEL_CAPACITY_ENV, "1")
        with capture_logs() as logs:
            on_root, _on_audit, _ = _run_grid(tmp_path / "thrash", grid, workers=3)
        assert _tree(off_root) == _tree(on_root)
        summaries = _summary_events(logs)
        assert len(summaries) == 1
        builds_total = summaries[0]["builds_total"]
        assert isinstance(builds_total, int)
        assert builds_total > 0  # correctness never depended on the hit rate

    def test_futures_grid_on_equals_sequential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T3: mixed spot + NSE-NIFTY-FUT, flag ON ≡ the sequential reference
        (the #1062 pin through REAL spawn workers — two instruments' kernels
        genuinely coexist in one long-lived per-worker cache)."""
        grid = _grid(instruments=["NIFTY", "NSE-NIFTY-FUT"], k_values=[1])
        roll = {"NSE-NIFTY-FUT": _FUT_SCHEDULE}
        monkeypatch.delenv(mod.WORKER_KERNEL_CACHE_ENV, raising=False)
        seq_root, seq_audit, seq_ckpt = _run_grid(tmp_path / "seq", grid, workers=1, roll=roll)
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        par_root, par_audit, par_ckpt = _run_grid(tmp_path / "par", grid, workers=3, roll=roll)
        assert _tree(seq_root) == _tree(par_root)
        assert len(_tree(par_root)) > 0
        assert _ingest_lines(seq_audit) == _ingest_lines(par_audit)
        assert seq_ckpt == par_ckpt


class _StampedResultPool:
    """A ``pool.submit``-compatible fake serving pre-minted results in-process.

    The minimal local twin of the sibling suite's ``_FakePool`` (neither test
    file imports the other — the standalone-import discipline): every submitted
    index resolves immediately to an injected :class:`mod.TupleWorkResult`, so a
    test controls EXACTLY which worker pids the parent observes.
    """

    def __init__(self, results: Mapping[int, mod.TupleWorkResult]) -> None:
        self._results = dict(results)

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        fut: Future[mod.TupleWorkResult] = Future()
        fut.set_result(self._results[index])
        return fut


class TestRealSpawnWorkerReuse:
    def test_summary_workers_counts_distinct_worker_pids(self, tmp_path: Path) -> None:
        """Fix-round-8 (FC-N2 kill): the drain-end summary's ``workers`` field
        is ``len(kernel_stats)`` — the count of DISTINCT worker pids observed
        on stamped results — never a constant. Pinned at a value > 1: three
        fake eval-worker pids are round-robined across the grid's tuples
        through a deterministic in-process pool (the sibling ``_FakePool``
        discipline — a REAL spawn pool's worker split is scheduler-dependent,
        so the ≥2-participants guarantee is stamped, not raced: each pid's
        first stamped result is its ≥1 build, the builds-bound pigeonhole).
        """
        grid = _grid()  # 4 tuples → pids A, B, C, A
        pids = (11111, 22222, 33333)
        results = {
            i: mod.TupleWorkResult(
                index=i,
                summary=None,  # a no-bars-shaped result legitimately snapshots 1/0
                failed_error=None,
                audit_records=(),
                worker_pid=pids[i % len(pids)],
                kernel_builds=1,
                kernel_hits=0,
            )
            for i in range(count_sweep_tuples(grid))
        }
        with (
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
            SweepAuditWriter.open(tmp_path / "sup.jsonl") as sup,
            SweepCache.open(tmp_path / "cache", bloom_capacity=10_000) as cache,
        ):
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=SweepCheckpointStore(
                    path=tmp_path / "ckpt.json",
                    sweep_id=grid_identity(grid),
                    total_combos=count_sweep_tuples(grid),
                ),
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=4,  # 3 eval workers — matches the three stamped pids
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            with capture_logs() as logs:
                driver._drive(_StampedResultPool(results), 0, 2, 4, acc)  # type: ignore[arg-type]
        summaries = _summary_events(logs)
        assert len(summaries) == 1
        observed_pids = {r.worker_pid for r in results.values()}
        assert summaries[0]["workers"] == len(observed_pids) == 3  # > 1, kills the constant
        assert summaries[0]["builds_total"] == 3  # one ≥1-build fold per distinct pid
        assert summaries[0]["hits_total"] == 0

    def test_single_eval_worker_reuses_across_tuples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REAL-spawn reuse pin: ``workers=2`` → ONE EVAL worker process, so
        every tuple lands on the SAME long-lived worker and the arithmetic is
        deterministic — builds_total == distinct keys (1), hits_total ==
        tuples − builds (3), workers == 1. Proves the cache genuinely persists
        across tasks in a spawned worker (not just in-process)."""
        grid = _grid()  # 4 tuples, ONE (instrument, tf, prox, anchor) key
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        with capture_logs() as logs:
            _run_grid(tmp_path, grid, workers=2)
        summaries = _summary_events(logs)
        assert len(summaries) == 1
        assert summaries[0]["builds_total"] == 1
        assert summaries[0]["hits_total"] == count_sweep_tuples(grid) - 1
        assert summaries[0]["workers"] == 1

    def test_off_run_emits_no_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(mod.WORKER_KERNEL_CACHE_ENV, raising=False)
        with capture_logs() as logs:
            _run_grid(tmp_path, _grid(), workers=2)
        assert _summary_events(logs) == []

    def test_junk_flag_resolves_off_for_the_whole_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T5 run-level: a junk env value fails SAFE to OFF (no summary, one
        parent warn) — the memory-shape change never switches on via a typo."""
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "definitely")
        with capture_logs() as logs:
            _run_grid(tmp_path, _grid(), workers=2)
        assert _summary_events(logs) == []
        warned = [
            e
            for e in logs
            if e.get("event") == "sweep_parallel.worker_kernel_cache.flag_unrecognised"
        ]
        assert len(warned) == 1


class TestDriverCtxCapacityHandoff:
    def test_env_capacity_reaches_the_pool_initializer_ctx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-round-9 (FD-M12 kill): the driver-RESOLVED capacity provably
        reaches the workers THROUGH THE DRIVER PATH — the ``_WorkerContext``
        handed to the pool's ``initargs`` inside the real ``run()`` carries
        the env-resolved value, never a constant. FM4 pins that
        ``_worker_init`` honors ctx capacity, but every test exercising that
        builds the ctx ITSELF — under the severed handoff (ctx capacity → 1)
        the operator capacity lever (and the grid-derived sizing law) is
        silently dead pool-wide while ``driver._worker_kernel_capacity``
        still reads the resolved value. The pool is a deterministic
        stamped-result fake (the ``_StampedResultPool`` discipline) so the
        HANDOFF is asserted, not raced.
        """
        grid = _grid()
        monkeypatch.setenv(mod.WORKER_KERNEL_CACHE_ENV, "1")
        monkeypatch.setenv(mod.WORKER_KERNEL_CAPACITY_ENV, "3")
        results = {
            i: mod.TupleWorkResult(
                index=i,
                summary=None,  # a no-bars-shaped result — the stamped-pool shape
                failed_error=None,
                audit_records=(),
                worker_pid=11111,
                kernel_builds=1,
                kernel_hits=0,
            )
            for i in range(count_sweep_tuples(grid))
        }
        captured: list[mod._WorkerContext] = []

        class _CtxCaptureExecutor:
            """A ``ProcessPoolExecutor`` stand-in capturing the real ``initargs``."""

            def __init__(self, **kwargs: Any) -> None:
                captured.extend(kwargs["initargs"])

            def __enter__(self) -> _StampedResultPool:
                return _StampedResultPool(results)

            def __exit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(mod, "ProcessPoolExecutor", _CtxCaptureExecutor)
        with (
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
            SweepAuditWriter.open(tmp_path / "sup.jsonl") as sup,
            SweepCache.open(tmp_path / "cache", bloom_capacity=10_000) as cache,
        ):
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=SweepCheckpointStore(
                    path=tmp_path / "ckpt.json",
                    sweep_id=grid_identity(grid),
                    total_combos=count_sweep_tuples(grid),
                ),
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=4,
            )
            assert driver._worker_kernel_capacity == 3  # env-resolved in the parent
            driver.run()
        [ctx] = captured  # exactly ONE ctx built, inside run()
        assert ctx.worker_kernel_cache is True
        assert ctx.worker_kernel_capacity == 3  # the handoff — never a constant
