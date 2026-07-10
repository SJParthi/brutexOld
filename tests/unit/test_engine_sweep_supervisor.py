"""Unit tests for the live G&A sweep supervisor (`brutex.engine.sweep_supervisor`).

Covers every public symbol + every branch for the strict 100% line + branch gate
(CLAUDE.md §4 / Golden Rule #12), across the 7-test-types discipline:

* **unit** — :func:`grid_identity`, :func:`tuple_cache_key`, :func:`publish_progress`
  / :func:`read_progress` / :func:`idle_progress`, the ``_drive_watchdog`` loop
  (both branches), and every :func:`run_supervised_grid` path: run, no-bars skip,
  cache skip, resume skip (incl. the BATCHED O(1) resume emission of
  DEC-SWEEP-SCALE-OPERABILITY-001 — one summarising audit line + one progress publish
  per resumed prefix), abort, and the watchdog daemon thread.
* **integration** — a real end-to-end supervised grid (real bars, real
  ``ComboDedup``/``AuditWriter``/``ResultsStore``/``SweepCache``) persisting +
  audit-chaining + checkpointing + dedup-caching a tuple.
* **property** (hypothesis) — the P0-1 regression guard: the cache key is
  sensitive to the honest guards (``top_k`` / ``min_hits``), so a re-run with a
  changed knob is NEVER falsely skipped.
* **golden** — frozen BLAKE3 byte-equality for :func:`grid_identity` +
  :func:`tuple_cache_key` (serialization-drift tripwire).

Bars are small, deterministic, tz-aware synthetic sessions with ``Decimal`` OHLC
(Golden Rule #1). Real wheel-backed objects throughout (no monkeypatched runner),
mirroring ``tests/unit/test_engine_sweep_orchestrator.py``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter, read_entries, verify_chain
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    make_store_for,
    tuple_store_path,
)
from brutex.engine.sweep_progress import (
    STATE_DONE,
    STATE_ERROR,
    SweepProgress,
    SweepProgressSnapshot,
)
from brutex.engine.sweep_run import ComboDedup, SweepComboError
from brutex.engine.sweep_supervisor import (
    DEFAULT_WORKER_NAME,
    SupervisedGridSummary,
    SweepGuards,
    _drive_watchdog,
    grid_identity,
    idle_progress,
    publish_progress,
    read_progress,
    run_supervised_grid,
    tuple_cache_key,
    tuple_cache_params,
    tuple_label,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import PriceSource, PriceSourceCell, Side
from brutex.vocabulary import ProximityParams
from hypothesis import assume, given
from hypothesis import strategies as st

_IST = timezone(timedelta(hours=5, minutes=30))


# ────── shared fixtures ──────


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


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win often via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
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


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


@contextmanager
def _open_guards(
    tmp_path: Path, grid: SweepGridConfig, *, worker_name: str = DEFAULT_WORKER_NAME
) -> Generator[SweepGuards, None, None]:
    """Open the five real G&A objects, bundle them, and clean up on exit."""
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
        worker_name=worker_name,
    )
    try:
        yield guards
    finally:
        audit.close()
        cache.close()


# ────── grid_identity / tuple_cache_key ──────


class TestIdentity:
    def test_grid_identity_is_stable(self) -> None:
        grid = _grid()
        assert grid_identity(grid) == grid_identity(grid)
        assert len(grid_identity(grid)) == 64

    def test_grid_identity_differs_with_proximity(self) -> None:
        """The proximity not-None descriptor branch hashes distinctly from None."""
        g_none = _grid(proximities=[None])
        g_prox = _grid(proximities=[ProximityParams(pivot_proximity_bps=12)])
        assert grid_identity(g_none) != grid_identity(g_prox)

    def test_grid_identity_differs_with_top_k(self) -> None:
        assert grid_identity(_grid(top_k=5)) != grid_identity(_grid(top_k=9))

    def test_grid_identity_differs_with_non_default_price_source(self) -> None:
        """The non-default price-source axis emits its descriptor key + re-keys the grid.

        A sole-baseline ``(HLC3, CLOSE)`` axis omits the ``price_source_cells`` key
        (byte-identical to the pre-axis grid); a non-default axis emits it, so an
        expanded grid can never silently resume a baseline checkpoint's work.
        """
        g_default = _grid()
        g_non_default = _grid(
            price_source_cells=[PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE)]
        )
        assert grid_identity(g_default) != grid_identity(g_non_default)

    def test_tuple_cache_key_is_32_bytes(self) -> None:
        grid = _grid()
        key = tuple_cache_key(next(iter_sweep_tuples(grid)), grid)
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_tuple_cache_key_proximity_branches(self) -> None:
        """Both the None and not-None proximity legs of the tuple key are exercised."""
        g_none = _grid(proximities=[None])
        g_prox = _grid(proximities=[ProximityParams(pivot_proximity_bps=12)])
        k_none = tuple_cache_key(next(iter_sweep_tuples(g_none)), g_none)
        k_prox = tuple_cache_key(next(iter_sweep_tuples(g_prox)), g_prox)
        assert k_none != k_prox

    def test_golden_identity_and_cache_key(self) -> None:
        """Frozen BLAKE3 byte-equality — a serialization change must break this."""
        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe("1minute", 1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0, 1, 2, 3, 4],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            prior_n=3,
            start_equity_paisa=10_000_000,
            max_combos=200_000,
            top_k=10,
            min_hits=30,
        )
        # Re-locked for DEC-NIFTY-FIB-AXIS-001: the grid descriptor gains the
        # ``fib_anchors`` axis and the tuple-cache params gain the ``fib_anchor``
        # key, so both digests moved by design (the anchor is part of the identity).
        assert (
            grid_identity(grid)
            == "e55de617a85d93ce83416ba68624c24b2eaaf2d63c482457a84837c09d11bf1a"
        )
        key = tuple_cache_key(next(iter_sweep_tuples(grid)), grid)
        assert key.hex() == "f9f118e0cfdc414f95e3a89a04f8d0b648e2a53960ba9c6608a6be3a55da9222"

    @given(
        top_k_a=st.integers(min_value=1, max_value=5),
        top_k_b=st.integers(min_value=1, max_value=5),
        min_hits_a=st.integers(min_value=0, max_value=5),
        min_hits_b=st.integers(min_value=0, max_value=5),
    )
    def test_cache_key_sensitive_to_guards(
        self, top_k_a: int, top_k_b: int, min_hits_a: int, min_hits_b: int
    ) -> None:
        """P0-1 regression: a changed top_k / min_hits ⇒ a different key (no false skip)."""
        assume((top_k_a, min_hits_a) != (top_k_b, min_hits_b))
        grid_a = _grid(top_k=top_k_a, min_hits=min_hits_a)
        grid_b = _grid(top_k=top_k_b, min_hits=min_hits_b)
        tuple_a = next(iter_sweep_tuples(grid_a))
        assert tuple_cache_key(tuple_a, grid_a) != tuple_cache_key(tuple_a, grid_b)


class TestParallelReexports:
    """The PUBLIC re-exports the parallel driver uses (DEC-MULTICORE-SWEEP-PHASE1-001).

    ``tuple_label`` / ``tuple_cache_params`` are the ONE-source-of-truth seams so
    :mod:`brutex.engine.sweep_parallel` emits byte-identical progress/audit labels
    and gates the ``SweepCache`` with the byte-identical key WITHOUT importing the
    private ``_tuple_label`` / ``_tuple_cache_params``. These focused unit tests
    scope those re-exports' coverage to sweep_supervisor's OWN per-module gate (the
    parallel suite is not scoped for this module by the touched-module gate).
    """

    def test_tuple_label_format(self) -> None:
        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        assert tuple_label(tuple_) == "NIFTY/1minute/bull/k=1/sl=80/tp=50"

    def test_tuple_cache_params_carries_full_identity(self) -> None:
        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        params = tuple_cache_params(tuple_, grid)
        assert isinstance(params, dict)
        assert params["k"] == tuple_.k
        assert params["sl_paisa"] == tuple_.sl_paisa
        assert params["tp_paisa"] == tuple_.tp_paisa
        assert params["condition_bits"] == sorted(set(grid.condition_bits))
        assert params["top_k"] == grid.top_k
        assert params["min_hits"] == grid.min_hits
        # The three reproducibility hashes are NO LONGER in the params dict —
        # they are structural keyword-only parameters of ``combo_digest``
        # (DEC-COMBO-DIGEST-REPRO-HASHES-001); ``combo_digest`` would REFUSE a
        # params dict carrying them (reserved-key guard).
        assert "data_snapshot_hash" not in params
        assert "indicator_version" not in params
        assert "code_commit_sha" not in params

    def test_tuple_cache_params_reproduces_the_cache_key(self) -> None:
        """The public params dict rebuilds the byte-identical SweepCache key.

        Proves the re-export is the SAME identity the private path uses — feeding
        it into ``combo_digest`` with the public ``tuple_label`` direction leg
        AND the grid's three structural reproducibility hashes reproduces
        ``tuple_cache_key`` exactly (no silent divergence).
        """
        from brutex.engine.sweep_cache import combo_digest

        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        rebuilt = combo_digest(
            tuple_.k,
            tuple_.instrument,
            tuple_.tf.label,
            "bull",
            tuple_cache_params(tuple_, grid),
            data_snapshot_hash=grid.data_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        assert rebuilt == tuple_cache_key(tuple_, grid)

    def test_tuple_cache_key_byte_identical_to_pre_structural_convention(self) -> None:
        """PRODUCTION-path byte-identity golden (DEC-COMBO-DIGEST-REPRO-HASHES-001).

        The hex below was captured by running the PRE-change (main)
        ``tuple_cache_key`` — where the three reproducibility hashes travelled
        INSIDE ``_tuple_cache_params`` by convention — over exactly this
        ``_grid()`` fixture. The structural-kwargs code must reproduce it
        EXACTLY: existing Bloom/LMDB dedup stores keyed under the legacy
        convention remain valid (no stale-skip, no store invalidation).
        """
        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        assert tuple_cache_key(tuple_, grid).hex() == (
            "a840ae6838edd7ab87b457ed898c2a51f48ca5435959f52f9916bec1c5baac92"
        )


# ────── _drive_watchdog ──────


class TestDriveWatchdog:
    def test_exits_immediately_when_stopped(self) -> None:
        wd = SweepWatchdog()
        stop = threading.Event()
        stop.set()  # wait() returns True at once → loop body never runs.
        _drive_watchdog(wd, stop, 0.0)

    def test_polls_once_then_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wd = SweepWatchdog()
        stop = threading.Event()
        calls: list[int] = []

        def _fake_check() -> tuple[object, ...]:
            calls.append(1)
            stop.set()  # so the next wait() returns True and the loop exits.
            return ()

        monkeypatch.setattr(wd, "check_once", _fake_check)
        _drive_watchdog(wd, stop, 0.0)
        assert calls == [1]


# ────── run_supervised_grid ──────


class TestRunSupervisedGrid:
    def test_run_with_hooks_persists_audits_caches_checkpoints(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        grid = _grid()
        bars = _trending_bars()
        seen: list[str] = []
        snaps: list[object] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: seen.append(t.instrument),
                on_progress=snaps.append,
            )
            assert summary.tuples_run == 1
            assert summary.tuples_total == 1
            assert summary.skipped_cache == 0
            assert summary.skipped_resume == 0
            assert guards.progress.snapshot().state == STATE_DONE
            # The tuple is now in the cache + checkpoint high-water mark advanced.
            tuple0 = next(iter_sweep_tuples(grid))
            assert guards.cache.seen_digest(tuple_cache_key(tuple0, grid))
            assert guards.checkpoint.last_completed_index == 0
        assert seen == ["NIFTY"]
        assert snaps  # on_progress fired at least once
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert verify_chain(tmp_path / "sup.jsonl") == 3  # START + TUPLE_DONE + COMPLETE
        assert actions[0] == "SUPERVISOR_START"
        assert actions[-1] == "SUPERVISOR_COMPLETE"
        assert "TUPLE_DONE" in actions

    def test_run_without_hooks(self, tmp_path: Path) -> None:
        """The run path with on_tuple=None + on_progress=None (the False branches)."""
        pytest.importorskip("polars")
        grid = _grid()
        bars = _trending_bars()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert isinstance(summary, SupervisedGridSummary)
        assert summary.tuples_run == 1

    def test_no_bars_tuple_is_skipped_and_checkpointed(self, tmp_path: Path) -> None:
        grid = _grid()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.tuples_skipped_no_bars == 1
            assert summary.tuples_run == 0
            assert guards.checkpoint.last_completed_index == 0
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert "TUPLE_NO_BARS" in actions

    def test_no_bars_raises_when_skip_empty_false(self, tmp_path: Path) -> None:
        grid = _grid()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
            pytest.raises(ValueError, match="no bars for NIFTY/1minute"),
        ):
            run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                skip_empty=False,
            )

    def test_cache_hit_skips_the_tuple(self, tmp_path: Path) -> None:
        """A pre-cached tuple is skipped (cache branch); a fresh sibling runs the no-bars path."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            tuple0 = next(iter_sweep_tuples(grid))  # NIFTY (index 0)
            guards.cache.record_digest(tuple_cache_key(tuple0, grid))
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.skipped_cache == 1
            assert summary.tuples_skipped_no_bars == 1
            assert summary.skipped_resume == 0
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert "TUPLE_SKIPPED_CACHE" in actions

    def test_resume_skips_completed_tuples(self, tmp_path: Path) -> None:
        """A loaded checkpoint resume-skips index 0; index 1 runs the no-bars path."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            guards.checkpoint.record(0)  # pretend index 0 completed on a prior run
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.skipped_resume == 1
            assert summary.tuples_skipped_no_bars == 1
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert "TUPLE_SKIPPED_RESUME" in actions

    def test_resume_batches_prefix_into_one_audit_line(self, tmp_path: Path) -> None:
        """A resume over N>1 completed tuples emits ONE summarising audit line.

        O(1) resume emission (DEC-SWEEP-SCALE-OPERABILITY-001): on a 4-tuple grid
        with indices 0-2 already checkpointed, the restart writes EXACTLY ONE
        ``TUPLE_SKIPPED_RESUME`` line (the prior per-tuple emission wrote 3)
        carrying ``resumed_count`` + the resume boundary, fires ONE progress
        publish for the whole prefix, and the hash chain — with the summary as
        an ordinary entry — still verifies end-to-end.
        """
        # Synthetic instrument names by test intent (resume prefix needs 4
        # distinct tuples) — opt out of the Layer-1 engine-scope invariant
        # (DEC-ENGINE-SCOPE-GUARD-001).
        grid = _grid(
            instruments=["I0", "I1", "I2", "I3"],
            unsafe_allow_out_of_scope_instruments=True,
        )
        snaps: list[SweepProgressSnapshot] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            for idx in range(3):  # indices 0-2 completed on a prior life
                guards.checkpoint.record(idx)
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_progress=snaps.append,
            )
            assert summary.skipped_resume == 3
            assert summary.tuples_skipped_no_bars == 1  # index 3 (no bars)
            # Progress advanced by the FULL prefix + the one no-bars tuple.
            snap = guards.progress.snapshot()
            assert snap.evaluated == 4
            assert snap.skipped == 4
        entries = read_entries(tmp_path / "sup.jsonl")
        actions = [e.action for e in entries]
        # EXACTLY ONE summarising resume line — the O(1) emission proof.
        assert actions == [
            "SUPERVISOR_START",
            "TUPLE_SKIPPED_RESUME",
            "TUPLE_NO_BARS",
            "SUPERVISOR_COMPLETE",
        ]
        resume = next(e for e in entries if e.action == "TUPLE_SKIPPED_RESUME")
        # The payload reconstructs the skipped set exactly: enumeration indices
        # [0, resumed_count) below the resume boundary (Golden Rule #7).
        assert resume.payload == {"resumed_count": 3, "resume_from": 3}
        # The resumed run's chain verifies end-to-end (Golden Rule #8).
        assert verify_chain(tmp_path / "sup.jsonl") == 4
        # ONE batched progress publish for the whole prefix (the first snapshot
        # already carries all 3 resumed tuples), then one per real tuple + the
        # finish publish — O(1) fires for the prefix, not O(resumed).
        assert len(snaps) == 3
        assert snaps[0].evaluated == 3
        assert snaps[0].skipped == 3

    def test_fully_resumed_rerun_emits_single_summary_line(self, tmp_path: Path) -> None:
        """An idempotent re-run over an N-tuple complete grid is O(1) in audit lines.

        The resume_count == total edge: the second run's ENTIRE grid is the
        resumed prefix — one summary line, zero per-tuple emission, and the
        shared audit file still verifies across both runs.
        """
        # Synthetic instrument names by test intent (full-resume edge needs 3
        # distinct tuples) — opt out of the Layer-1 engine-scope invariant
        # (DEC-ENGINE-SCOPE-GUARD-001).
        grid = _grid(
            instruments=["I0", "I1", "I2"],
            unsafe_allow_out_of_scope_instruments=True,
        )
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            first = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            second = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert first.tuples_skipped_no_bars == 3
        assert first.skipped_resume == 0
        assert second.skipped_resume == 3
        assert second.tuples_skipped_no_bars == 0
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        # Run 1: START + 3×NO_BARS + COMPLETE; run 2: START + ONE summarising
        # RESUME + COMPLETE — never one line per resumed tuple.
        assert actions.count("TUPLE_SKIPPED_RESUME") == 1
        assert verify_chain(tmp_path / "sup.jsonl") == len(actions) == 8

    def test_idempotent_rerun_does_no_work(self, tmp_path: Path) -> None:
        """A second run over the same guards resumes past the only tuple (no rework)."""
        pytest.importorskip("polars")
        grid = _grid()
        bars = _trending_bars()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            first = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "r", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            second = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "r", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
        assert first.tuples_run == 1
        assert second.tuples_run == 0
        assert second.skipped_resume == 1

    def test_abort_records_failure_and_reraises(self, tmp_path: Path) -> None:
        grid = _grid()

        def _boom(_i: str, _t: str) -> Sequence[CandleBar]:
            raise RuntimeError("wedged loader")

        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            with pytest.raises(RuntimeError, match="wedged loader"):
                run_supervised_grid(
                    grid,
                    bars_for=_boom,
                    store_for=make_store_for(tmp_path, grid),
                    dedup=_dedup(),
                    ingest_audit=ingest,
                    guards=guards,
                )
            assert guards.progress.snapshot().state == STATE_ERROR
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert actions[-1] == "SUPERVISOR_ABORT"

    def test_keyboard_interrupt_aborts_grid_not_a_tuple_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyboardInterrupt from a tuple ESCAPES the per-tuple catch + ABORTS the grid.

        The supervisor's tuple loop catches only ``SweepComboError`` (the
        Resilient skip path); the outer guard catches ``Exception``
        (record-then-reraise ``SUPERVISOR_ABORT``). A ``KeyboardInterrupt`` is a
        ``BaseException`` (NOT an ``Exception``) so it is caught by NEITHER — an
        operator Ctrl-C propagates BARE through the ``finally`` (which only stops
        the watchdog) and aborts the WHOLE (33B-trial) grid instead of being
        silently downgraded to a single-tuple skip + continue
        (DEC-ENGINE-DEDUP-NAN-CANON-001 / the audit H1 guard). The interrupted
        tuple must NOT bump ``tuples_failed``, NOT write a ``TUPLE_FAILED`` audit
        line, NOT record its cache key, and NOT advance the checkpoint, and the
        sibling tuple must never run. It is NOT downgraded to a SUPERVISOR_ABORT
        either (the outer except is ``Exception``, not ``BaseException``).
        """
        pytest.importorskip("polars")

        # 2 tuples: index 0 NIFTY raises Ctrl-C, index 1 BANKNIFTY would run.
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))
        ran: list[str] = []

        def _interrupt(grid_: object, tuple_: object, **_kwargs: object) -> object:
            ran.append(tuple_.instrument)  # type: ignore[attr-defined]
            raise KeyboardInterrupt("operator ctrl-c")

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _interrupt)

        seen: list[str] = []
        # Capture the escaping exception with a plain try/except (NOT
        # pytest.raises) so the still-open guards can be probed inside the
        # handler — the KeyboardInterrupt unwinds the `with`, which closes the
        # cache, so the probe must happen before the block exits.
        raised: BaseException | None = None
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            try:
                run_supervised_grid(
                    grid,
                    bars_for=lambda _i, _t: bars,
                    store_for=make_store_for(tmp_path / "results", grid),
                    dedup=_dedup(),
                    ingest_audit=ingest,
                    guards=guards,
                    on_tuple=lambda t, _s: seen.append(t.instrument),
                )
            except KeyboardInterrupt as exc:
                raised = exc
                # Probe the still-open guards: neither tuple was cached and the
                # checkpoint never advanced (stays at the fresh -1 sentinel, so a
                # resume re-runs from index 0).
                assert not guards.cache.seen_digest(tuple_cache_key(tuples[0], grid))
                assert not guards.cache.seen_digest(tuple_cache_key(tuples[1], grid))
                assert guards.checkpoint.last_completed_index == -1
        # The Ctrl-C escaped the grid (the handler captured it, did not re-raise
        # for the assertions below).
        assert isinstance(raised, KeyboardInterrupt)
        assert str(raised) == "operator ctrl-c"
        # It is NOT downgraded to a SweepComboError (the per-tuple skip path).
        assert not isinstance(raised, SweepComboError)
        assert not isinstance(raised, Exception)
        # The grid aborted on the FIRST tuple — the second never ran, on_tuple
        # never fired.
        assert ran == ["NIFTY"]
        assert seen == []
        # The Ctrl-C escaped BARE: no TUPLE_FAILED (the per-tuple catch is
        # narrow), no SUPERVISOR_COMPLETE (the loop aborted), and — because the
        # outer except is ``Exception`` not ``BaseException`` — NOT even a
        # SUPERVISOR_ABORT (it is never recorded as an in-band failure).
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert "TUPLE_FAILED" not in actions
        assert "SUPERVISOR_COMPLETE" not in actions
        assert "SUPERVISOR_ABORT" not in actions

    def test_combo_error_skips_tuple_without_cache_or_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SweepComboError SKIPS the tuple ("Resilient: skip + continue").

        The failed tuple is counted in ``tuples_failed`` + audited ``TUPLE_FAILED``,
        the sibling still runs, and — CRITICALLY — the failed tuple's cache entry
        + checkpoint index are NOT recorded so a resume re-runs it (sweep_heal C5).
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        # 2 tuples: index 0 NIFTY runs, index 1 BANKNIFTY fails.
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "BANKNIFTY":  # type: ignore[attr-defined]
                raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        # Patch the name as resolved inside run_supervised_grid (string target
        # avoids the re-export attr check).
        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _maybe_raise)

        seen: list[str] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: seen.append(t.instrument),
            )
            assert summary.tuples_run == 1
            assert summary.tuples_failed == 1
            assert summary.tuples_skipped_no_bars == 0
            assert summary.skipped_cache == 0
            assert summary.skipped_resume == 0
            # The successful tuple (index 0) was cached + checkpointed; the FAILED
            # tuple (index 1) was NEITHER — so a resume re-runs it (C5).
            assert guards.cache.seen_digest(tuple_cache_key(tuples[0], grid))
            assert not guards.cache.seen_digest(tuple_cache_key(tuples[1], grid))
            assert guards.checkpoint.last_completed_index == 0  # never advanced to 1
            # The run is not a clean done illusion: it finished but with a failure.
            snap = guards.progress.snapshot()
            assert snap.state == STATE_DONE
            # The finish detail surfaces the failed count (never reads green-only).
            assert snap.detail == "1 run · 0 cached · 0 resumed · 1 failed"
        assert seen == ["NIFTY"]  # on_tuple never fired for the failed tuple
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert "TUPLE_FAILED" in actions
        failed = next(e for e in read_entries(tmp_path / "sup.jsonl") if e.action == "TUPLE_FAILED")
        assert failed.payload["index"] == 1
        assert "BANKNIFTY" in failed.payload["tuple"]
        assert "SweepComboError" in failed.payload["error"]
        complete = next(
            e for e in read_entries(tmp_path / "sup.jsonl") if e.action == "SUPERVISOR_COMPLETE"
        )
        assert complete.payload["tuples_failed"] == 1
        assert verify_chain(tmp_path / "sup.jsonl") > 0

    def test_failed_tuple_reruns_on_resume_when_later_tuple_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKER regression: a FAILED tuple is RE-RUN on resume, never skipped.

        The checkpoint is a CONTIGUOUS-CLEAN-PREFIX mark. When tuple index 0 FAILS
        but a LATER tuple (index 1) SUCCEEDS, the checkpoint must NOT advance past
        the failure (it stays at the last clean index before it) — so on a resume
        run ``resume_from`` lands on the failed index 0 and it RE-RUNS, instead of
        being silently ``skipped_resume`` forever (the sweep_heal C5 lesson +
        Golden Rules #4/#7). Without the fix this test FAILS: index 1's success
        would record(1) → next_index=2 → index 0 resume-skipped, never re-run.
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        # 2 tuples: index 0 NIFTY, index 1 BANKNIFTY.
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))

        # On the FIRST run NIFTY (index 0) raises; on the resume it succeeds.
        fail_nifty = {"on": True}

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "NIFTY" and fail_nifty["on"]:  # type: ignore[attr-defined]
                raise SweepComboError("combo 0x0000000000000001 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _maybe_raise)

        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            # ── First run: index 0 fails, index 1 succeeds. ──
            first_ran: list[str] = []
            first = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: first_ran.append(t.instrument),
            )
            assert first.tuples_failed == 1
            assert first.tuples_run == 1
            assert first_ran == ["BANKNIFTY"]  # the LATER tuple ran
            # The checkpoint must be FROZEN before the failure — NOT advanced to 1
            # by index 1's success (the BLOCKER: without the fix it would be 1).
            assert guards.checkpoint.last_completed_index == -1
            # Index 1 (succeeded) IS cached so the resume short-circuits it via the
            # cache tier; index 0 (failed) is NOT cached so the resume re-evaluates.
            assert guards.cache.seen_digest(tuple_cache_key(tuples[1], grid))
            assert not guards.cache.seen_digest(tuple_cache_key(tuples[0], grid))

            # ── Resume run (SAME guards): NIFTY now succeeds. ──
            fail_nifty["on"] = False
            second_ran: list[str] = []
            second = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: second_ran.append(t.instrument),
            )
            # The previously-FAILED tuple 0 was RE-RUN (not skipped_resume); the
            # already-succeeded tuple 1 is short-circuited by the cache tier.
            assert second.tuples_failed == 0
            assert second.tuples_run == 1
            assert second.skipped_resume == 0  # NOT resume-skipped — the BLOCKER fix
            assert second.skipped_cache == 1  # tuple 1 short-circuited via cache
            assert second_ran == ["NIFTY"]  # the failed tuple genuinely re-ran
            # Both tuples now clean → the checkpoint advanced to the full prefix.
            assert guards.checkpoint.last_completed_index == 1

    def test_checkpoint_frozen_for_cache_and_nobars_tuples_after_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a failure the checkpoint stays frozen even past cache-hit / no-bars tuples.

        Covers the ``if not had_failure`` FALSE branch on BOTH the cache-skip and
        the no-bars paths: a later cache-HIT or no-bars tuple must NOT advance the
        checkpoint past the earlier failed tuple (else that failed tuple would be
        resume-skipped — the BLOCKER). 3 tuples: index 0 FAILS, index 1 is a
        pre-cached cache-hit, index 2 has no bars.
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        grid = _grid(instruments=["NIFTY", "BANKNIFTY", "SENSEX"])
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))

        def _bars_for(instrument: str, _t: str) -> Sequence[CandleBar]:
            return [] if instrument == "SENSEX" else bars  # index 2 has no bars

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "NIFTY":  # type: ignore[attr-defined]  # index 0 fails
                raise SweepComboError("combo 0x0000000000000001 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _maybe_raise)

        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            guards.cache.record_digest(tuple_cache_key(tuples[1], grid))  # pre-cache index 1
            summary = run_supervised_grid(
                grid,
                bars_for=_bars_for,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.tuples_failed == 1  # index 0
            assert summary.skipped_cache == 1  # index 1
            assert summary.tuples_skipped_no_bars == 1  # index 2
            # The checkpoint is FROZEN before the failure — neither the cache-hit
            # (index 1) nor the no-bars (index 2) tuple advanced it past index 0.
            assert guards.checkpoint.last_completed_index == -1

    def test_all_tuples_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """EVERY tuple raises SweepComboError: tuples_failed == total, run == 0.

        The supervisor finishes the whole grid (no global abort), audits one
        ``TUPLE_FAILED`` per tuple, the chain still verifies, the checkpoint NEVER
        advances (every tuple un-recorded so a resume re-runs all), and NO tuple
        is cache-recorded.
        """
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"], directions=[Side.BUY, Side.SELL])
        total = count_sweep_tuples(grid)
        assert total == 4
        tuples = list(iter_sweep_tuples(grid))

        def _always_raise(*_args: object, **_kwargs: object) -> object:
            raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _always_raise)

        seen: list[str] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: seen.append(t.instrument),
            )
            assert summary.tuples_failed == total
            assert summary.tuples_run == 0
            assert summary.tuples_skipped_no_bars == 0
            assert summary.skipped_cache == 0
            assert summary.skipped_resume == 0
            # Checkpoint frozen at the start: every tuple re-runs on resume.
            assert guards.checkpoint.last_completed_index == -1
            # No tuple was cache-recorded (a failed tuple must never be cached).
            for t in tuples:
                assert not guards.cache.seen_digest(tuple_cache_key(t, grid))
        assert seen == []  # on_tuple never fires for a failed tuple
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert actions.count("TUPLE_FAILED") == total
        assert actions[0] == "SUPERVISOR_START"
        assert actions[-1] == "SUPERVISOR_COMPLETE"
        assert verify_chain(tmp_path / "sup.jsonl") == total + 2
        complete = next(
            e for e in read_entries(tmp_path / "sup.jsonl") if e.action == "SUPERVISOR_COMPLETE"
        )
        assert complete.payload["tuples_failed"] == total

    def test_buckets_reconcile_to_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-tuple-silently-lost: run + no_bars + cache + resume + failed == total.

        A mixed 5-tuple grid touching EVERY disposition: index 0 resume-skipped
        (pre-checkpointed), index 1 cache-skipped (pre-cached), index 2 RUNS,
        index 3 NO BARS, index 4 FAILS.
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        # 5 instruments → 5 tuples in enumeration order.
        instruments = ["I0", "I1", "I2", "I3", "I4"]
        # Synthetic instrument names by test intent (bucket accounting needs 5
        # distinct tuples) — opt out of the Layer-1 engine-scope invariant
        # (DEC-ENGINE-SCOPE-GUARD-001).
        grid = _grid(instruments=instruments, unsafe_allow_out_of_scope_instruments=True)
        total = count_sweep_tuples(grid)
        assert total == 5
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))

        def _bars_for(instrument: str, _t: str) -> Sequence[CandleBar]:
            return [] if instrument == "I3" else bars  # index 3 has no bars

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "I4":  # type: ignore[attr-defined]  # index 4 fails
                raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _maybe_raise)

        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            guards.checkpoint.record(0)  # index 0 completed on a prior life → resume-skip
            guards.cache.record_digest(tuple_cache_key(tuples[1], grid))  # index 1 pre-cached
            summary = run_supervised_grid(
                grid,
                bars_for=_bars_for,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.skipped_resume == 1  # index 0
            assert summary.skipped_cache == 1  # index 1
            assert summary.tuples_run == 1  # index 2
            assert summary.tuples_skipped_no_bars == 1  # index 3
            assert summary.tuples_failed == 1  # index 4
            # Every tuple lands in exactly one bucket — the reconciliation invariant.
            assert (
                summary.tuples_run
                + summary.tuples_skipped_no_bars
                + summary.skipped_cache
                + summary.skipped_resume
                + summary.tuples_failed
                == summary.tuples_total
                == total
            )

    def test_two_consecutive_failures_freeze_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ADJACENT failing tuples (i, i+1) both re-run on resume.

        Index 0 + 1 run clean; index 2 + 3 BOTH raise. The checkpoint must freeze
        at the last clean index BEFORE the first failure (index 1), so a resume
        re-runs BOTH failed tuples, and NEITHER failed tuple was cache-recorded.
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        # 4 instruments → 4 tuples; indices 2 and 3 are the consecutive failures.
        # Synthetic instrument names by test intent (DEC-ENGINE-SCOPE-GUARD-001 opt-out).
        grid = _grid(
            instruments=["I0", "I1", "I2", "I3"], unsafe_allow_out_of_scope_instruments=True
        )
        total = count_sweep_tuples(grid)
        assert total == 4
        bars = _trending_bars()
        tuples = list(iter_sweep_tuples(grid))

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument in {"I2", "I3"}:  # type: ignore[attr-defined]
                raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _maybe_raise)

        seen: list[str] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=lambda t, _s: seen.append(t.instrument),
            )
            assert summary.tuples_failed == 2
            assert summary.tuples_run == 2  # indices 0, 1
            # The checkpoint froze at the last CLEAN index (1) before the first
            # failure (2) — so on resume `next_index == 2` re-runs BOTH failures.
            assert guards.checkpoint.last_completed_index == 1
            # The two clean tuples are cached; NEITHER failed tuple is.
            assert guards.cache.seen_digest(tuple_cache_key(tuples[0], grid))
            assert guards.cache.seen_digest(tuple_cache_key(tuples[1], grid))
            assert not guards.cache.seen_digest(tuple_cache_key(tuples[2], grid))
            assert not guards.cache.seen_digest(tuple_cache_key(tuples[3], grid))
        assert seen == ["I0", "I1"]  # on_tuple only for the clean tuples
        # Two TUPLE_FAILED audit lines, at indices 2 and 3.
        failed = [e for e in read_entries(tmp_path / "sup.jsonl") if e.action == "TUPLE_FAILED"]
        assert [e.payload["index"] for e in failed] == [2, 3]
        assert verify_chain(tmp_path / "sup.jsonl") > 0

    def test_combo_error_real_raise_leaves_store_unsentineled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REAL combo raise skips the tuple WITHOUT writing its store/sentinel."""
        pytest.importorskip("polars")
        from brutex.engine.tuple_complete import completion_sentinel_path

        grid = _grid()  # single NIFTY tuple
        bars = _trending_bars()
        results_root = tmp_path / "results"

        def _always_raise(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("corrupt window")

        # The wired per-mask METRICS TAIL (DEC-SWEEP-RUST-TRADESIM-WIRE-001) —
        # raising here produces the real fingerprinted SweepComboError exactly
        # as the pre-wiring per-mask metrics_for_mask seam did.
        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _always_raise)

        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results_root, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
            )
            assert summary.tuples_failed == 1
            assert summary.tuples_run == 0
            assert guards.checkpoint.last_completed_index == -1  # nothing recorded
        store_path = tuple_store_path(results_root, next(iter_sweep_tuples(grid)), grid)
        assert not store_path.exists()
        assert not completion_sentinel_path(store_path).exists()

    def test_non_combo_error_in_run_one_tuple_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-SweepComboError from run_one_tuple is NOT caught — it aborts."""
        grid = _grid()

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("genuinely fatal")

        monkeypatch.setattr("brutex.engine.sweep_supervisor.run_one_tuple", _boom)
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            with pytest.raises(RuntimeError, match="genuinely fatal"):
                run_supervised_grid(
                    grid,
                    bars_for=lambda _i, _t: [],
                    store_for=make_store_for(tmp_path, grid),
                    dedup=_dedup(),
                    ingest_audit=ingest,
                    guards=guards,
                )
            assert guards.progress.snapshot().state == STATE_ERROR
        actions = [e.action for e in read_entries(tmp_path / "sup.jsonl")]
        assert actions[-1] == "SUPERVISOR_ABORT"

    def test_reuse_bar_bits_disabled(self, tmp_path: Path) -> None:
        """reuse_bar_bits=False takes the legacy no-kernel-cache branch."""
        grid = _grid()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                reuse_bar_bits=False,
            )
        assert summary.tuples_skipped_no_bars == 1

    def test_watchdog_daemon_thread_starts_and_stops(self, tmp_path: Path) -> None:
        """watchdog_poll_seconds spawns + joins the detector daemon (no-bars grid)."""
        grid = _grid()
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                watchdog_poll_seconds=0.01,
            )
        assert summary.tuples_skipped_no_bars == 1


# ────── progress publish / read bridge ──────


class TestProgressBridge:
    def test_publish_then_read_round_trip(self, tmp_path: Path) -> None:
        progress = SweepProgress()
        progress.start(enumerated=10)
        progress.advance(current="NIFTY/1minute")
        path = tmp_path / "nested" / "progress.json"
        publish_progress(path, progress.snapshot())
        out = read_progress(path)
        assert out["enumerated"] == 10
        assert out["evaluated"] == 1
        assert out["state"] == "running"

    def test_read_missing_file_is_idle(self, tmp_path: Path) -> None:
        out = read_progress(tmp_path / "nope.json")
        assert out == idle_progress()
        assert out["state"] == "idle"

    def test_read_malformed_file_is_idle(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert read_progress(bad)["state"] == "idle"

    def test_idle_progress_returns_a_fresh_copy(self) -> None:
        first = idle_progress()
        first["state"] = "mutated"
        assert idle_progress()["state"] == "idle"


# ────── dataclasses ──────


def test_summary_and_guards_are_frozen() -> None:
    summary = SupervisedGridSummary(
        sweep_id="x",
        tuples_total=1,
        tuples_run=1,
        tuples_skipped_no_bars=0,
        skipped_cache=0,
        skipped_resume=0,
        enumerated=5,
        conflict_pruned=0,
        skipped_dedup=0,
        evaluated=5,
        ranked=2,
        persisted=2,
        tuples_failed=0,
    )
    assert summary.sweep_id == "x"
    with pytest.raises((AttributeError, TypeError)):
        summary.persisted = 9  # type: ignore[misc]
