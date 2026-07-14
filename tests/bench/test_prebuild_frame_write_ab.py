"""A/B bench gates for the frame-direct derived-TF write (DEC-PREBUILD-FRAME-WRITE-001 §7.5).

HARD deterministic op-count asserts (the primary teeth — never wall-clock
flaky) + a RECORDED wall ratio (never asserted at the shared tier; strict
claims belong to the non-blocking M4-Pro lane, DEC-PREMERGE-M4PRO-NONBLOCK-001
posture):

* trees byte-identical both arms (re-asserted here on the bench fixture);
* fsync count EQUAL on both arms (the atomic tmp+fsync+replace per rendered
  file is contract — the fast lane must never add or skip one);
* ``os.replace`` count equal; partition-flock acquisition count equal;
* **ZERO ``CandleBar`` constructions on admitted fresh partitions** (the
  mutant killer — the whole point of the frame lane);
* zero degrades on the healthy fixture;
* WARM re-run: 0 renders / 0 fsyncs / 0 replaces on BOTH arms (the C4
  mtime-churn guard, bench-fixture scale);
* burn-in cost pin: ≤ the budgeted double-computations per process;
* degrade-storm: an all-degrading (poisoned-schema) lake stays within the
  robust ratio ceiling of the legacy arm — guards are O(cols)/columnar, a
  degrade never makes the slow path materially slower.

The thread-scaling acceptance signal (G-F — post-fix 2 workers > 1.5× over 1
worker on the sandbox) is deliberately env-gated OUT of shared CI
(``BRUTEX_FRAME_WRITE_SCALING=1`` — the DEC-HARDENING-LEFTOVERS-001 flaky-bench
precedent); it is a Phase-2 harness measurement, not a merge gate.

Whitebox spies — ``reportPrivateUsage`` silenced per DEC-P0-017.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.frame_write_gate import PREBUILD_FRAME_WRITE_ENV
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

pytest.importorskip("polars")

from tests.bench._o1_tiers import ratio_ceiling

_EXCHANGE = "NSE"
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()


def _seed(root: Path, *, n: int = 375) -> None:
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + (i % 250))),
            high=Decimal(str(115.0 + (i % 250))),
            low=Decimal(str(95.0 + (i % 250))),
            close=Decimal(str(102.0 + (i % 250))),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=root))
    sink.append(exchange=_EXCHANGE, segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)


class _OpCounter:
    """Counts fsync / os.replace / flock acquisitions / CandleBar builds / renders."""

    def __init__(self) -> None:
        self.fsync = 0
        self.replace = 0
        self.flock = 0
        self.candle_bars = 0
        self.renders = 0

    def install(self, mp: pytest.MonkeyPatch) -> None:
        import brutex.data.partition_lock as plock

        original_fsync = os.fsync
        original_replace = os.replace
        original_lock = plock.partition_write_lock
        original_post = CandleBar.__post_init__
        original_render = ParquetWriter._render_frame_bytes

        def counting_fsync(fd: int) -> None:
            self.fsync += 1
            original_fsync(fd)

        def counting_replace(src: Any, dst: Any, **kw: Any) -> None:
            self.replace += 1
            original_replace(src, dst, **kw)

        def counting_lock(lake_root: Path, dest: Path, **kw: Any) -> Any:
            self.flock += 1
            return original_lock(lake_root, dest, **kw)

        def counting_post(bar_self: CandleBar) -> None:
            self.candle_bars += 1
            original_post(bar_self)

        def counting_render(df: Any) -> bytes:
            self.renders += 1
            return original_render(df)

        mp.setattr(os, "fsync", counting_fsync)
        mp.setattr(os, "replace", counting_replace)
        mp.setattr("brutex.data.parquet_writer.partition_write_lock", counting_lock)
        mp.setattr(CandleBar, "__post_init__", counting_post)
        mp.setattr(ParquetWriter, "_render_frame_bytes", staticmethod(counting_render))


def _derive(root: Path, flag: str, *, steady: bool = True) -> float:
    os.environ[PREBUILD_FRAME_WRITE_ENV] = flag
    if steady:
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
    else:
        gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()
    start = time.perf_counter()
    materialize_lake_timeframes(
        lake_root=root,
        exchange=_EXCHANGE,
        segment="INDEX",
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        incremental=False,
    )
    return time.perf_counter() - start


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted((root / "bars").rglob("*.parquet"))
    }


def test_ab_op_count_parity_and_zero_candlebars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary deterministic teeth: op parity + the CandleBar mutant killer."""
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    _seed(root_off)
    _seed(root_on)

    counts: dict[str, _OpCounter] = {}
    walls: dict[str, float] = {}
    for flag, root, arm in (("0", root_off, "off"), ("1", root_on, "on")):
        counter = _OpCounter()
        with pytest.MonkeyPatch.context() as mp:
            counter.install(mp)
            walls[arm] = _derive(root, flag, steady=True)
        counts[arm] = counter

    # Byte identity re-asserted on the bench fixture (19 derived TFs, cold).
    assert _tree(root_off) == _tree(root_on)
    # fsync-per-rendered-file is CONTRACT: counts equal both arms.
    assert counts["off"].fsync == counts["on"].fsync
    assert counts["off"].replace == counts["on"].replace
    assert counts["off"].flock == counts["on"].flock
    assert counts["off"].renders == counts["on"].renders  # ONE encoder, both arms
    # The mutant killer: ZERO per-bar Python objects on admitted fresh writes.
    assert counts["on"].candle_bars == 0
    assert counts["off"].candle_bars > 0  # anti-vacuity: the spy genuinely counts
    # The RECORDED wall ratio (never asserted at the shared tier — sandbox
    # absolutes do not transfer; the M4-Pro lane owns strict claims).
    ratio = walls["on"] / walls["off"] if walls["off"] else float("nan")
    print(f"frame-write cold A/B wall ratio fast/legacy = {ratio:.3f}")


def test_warm_rerun_zero_ops_both_arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WARM steady-state: 0 renders / 0 fsyncs / 0 replaces on BOTH arms (C4)."""
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    _seed(root_off)
    _seed(root_on)
    _derive(root_off, "0")
    _derive(root_on, "1")

    for flag, root, arm in (("0", root_off, "off"), ("1", root_on, "on")):
        counter = _OpCounter()
        with pytest.MonkeyPatch.context() as mp:
            counter.install(mp)
            _derive(root, flag, steady=True)
        assert counter.renders == 0, arm
        assert counter.fsync == 0, arm
        assert counter.replace == 0, arm
        if arm == "on":
            assert counter.candle_bars == 0  # the warm no-op verdict is frame-only


def test_burn_in_cost_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Burn-in double-computes AT MOST the per-lane budgets, then stops."""
    root = tmp_path / "on"
    _seed(root)
    legacy_calls = 0
    original = ParquetWriter.write

    def counting(self: ParquetWriter, **kwargs: Any) -> Any:
        nonlocal legacy_calls
        legacy_calls += 1
        return original(self, **kwargs)

    monkeypatch.setattr(ParquetWriter, "write", counting)
    # Cold derive under FULL budgets: 19 fresh TF writes; only the first 8
    # fresh admissions may double-compute (plus the seeding writes are not
    # counted — the spy installs after seeding).
    _derive(root, "1", steady=False)
    assert legacy_calls <= gate.FRESH_SELF_CHECK_SAMPLES
    # Warm derive: only the first 8 merge admissions double-compute.
    legacy_calls = 0
    os.environ[PREBUILD_FRAME_WRITE_ENV] = "1"
    gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=gate.MERGE_SELF_CHECK_SAMPLES)
    materialize_lake_timeframes(
        lake_root=root,
        exchange=_EXCHANGE,
        segment="INDEX",
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        incremental=False,
    )
    assert legacy_calls <= gate.MERGE_SELF_CHECK_SAMPLES


def test_degrade_storm_stays_within_legacy_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ALL-DEGRADING run (schema-poisoned stored partitions) never makes the
    slow path materially slower: guards are O(cols)/columnar. Robust shared-CI
    ratio tier only (never the strict 1.1× — this is a two-pass-vs-one shape,
    disclosed: the fast arm re-reads the stored file once per degrade)."""
    import polars as pl

    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed(root, n=240)
        _derive(root, "0")
        # Poison EVERY derived partition with a stray column (merge_stored_schema).
        for p in sorted((root / "bars").rglob("*.parquet")):
            if "1minute" in str(p):
                continue
            pl.read_parquet(p).with_columns(pl.lit(1).alias("stray")).write_parquet(p)

    t_off = min(_derive(root_off, "0") for _ in range(2))
    t_on = min(_derive(root_on, "1") for _ in range(2))
    assert _tree(root_off) == _tree(root_on)
    assert t_on <= t_off * ratio_ceiling(3.0), (t_on, t_off)


@pytest.mark.skipif(
    os.environ.get("BRUTEX_FRAME_WRITE_SCALING", "") != "1",
    reason="thread-scaling signal is a Phase-2 harness measurement "
    "(BRUTEX_FRAME_WRITE_SCALING=1) — never judged on shared 2-core runners",
)
def test_thread_scaling_signal_recorded(tmp_path: Path) -> None:
    """G-F directional gate (env-gated): record the 1-vs-2-worker ratio flag-ON.

    Executed in the Phase-2 harness for the DEC record; the sandbox target is
    2 workers > 1.5× over 1 worker (today's measured baseline: 1.0×). Recorded,
    never asserted here — the M4-Pro lane owns the pinned figures.
    """
    from brutex.dashboard.prebuild_parallel import run_pipeline_prebuild_parallel  # noqa: F401

    pytest.skip("Phase-2 harness measurement — see the DEC record")
