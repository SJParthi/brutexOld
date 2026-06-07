"""Unit tests for the live G&A sweep supervisor (`brutex.engine.sweep_supervisor`).

Covers every public symbol + every branch for the strict 100% line + branch gate
(CLAUDE.md §4 / Golden Rule #12), across the 7-test-types discipline:

* **unit** — :func:`grid_identity`, :func:`tuple_cache_key`, :func:`publish_progress`
  / :func:`read_progress` / :func:`idle_progress`, the ``_drive_watchdog`` loop
  (both branches), and every :func:`run_supervised_grid` path: run, no-bars skip,
  cache skip, resume skip, abort, and the watchdog daemon thread.
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
from collections.abc import Iterator, Sequence
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
)
from brutex.engine.sweep_progress import STATE_DONE, STATE_ERROR, SweepProgress
from brutex.engine.sweep_run import ComboDedup
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
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
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
) -> Iterator[SweepGuards]:
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
        assert (
            grid_identity(grid)
            == "456d0d52896a984f7b1737fea8bc9429b68d738f9db062bb9d386b8f66ef2e2e"
        )
        key = tuple_cache_key(next(iter_sweep_tuples(grid)), grid)
        assert key.hex() == "f862ca7c495df7be6126e76aea04e6718e8f2f8bcfb278e1e682737e895b1494"

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
    )
    assert summary.sweep_id == "x"
    with pytest.raises((AttributeError, TypeError)):
        summary.persisted = 9  # type: ignore[misc]
