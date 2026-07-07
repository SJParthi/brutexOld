"""Bench: 2 concurrent polars resample workers, bounded vs unbounded pool.

DEC-THREAD-BUDGET-BOUNDS-001. Because polars sizes its ONE process-global
thread pool at FIRST import from ``POLARS_MAX_THREADS``, the A/B must run in
SUBPROCESSES. The A/B arms use an EXPLICIT divided budget
(``expected_workers=2``) to prove the bounding MECHANISM takes effect and to
measure its cost; the DEFAULT entrypoint budget is deliberately FULL physical
width (the hostile-review MAJOR: the #1401 prebuild's worker threads share
the one global pool, and halving it would waste half the box in the phase
the operator cares most about) — pinned here by the default-width arm
(``pool == physical_cores``). Each child runs the SAME deterministic
workload: two concurrent threads resampling a synthetic 1-minute frame to
5-minute buckets in a loop.

HONEST envelope (Golden Rule #10): the contention fix is the per-connection
DuckDB caps; the polars entrypoint bound caps polars at PHYSICAL cores (its
own default is all LOGICAL cpus) and gives the operator one lever. On this
small CI fixture the divided-budget arm is expected ~NEUTRAL-to-slower; no
speedup is claimed. The bench asserts (a) the explicit bound genuinely took
effect in the child (``pl.thread_pool_size() == budget``), (b) the arms
compute BYTE-IDENTICAL results (checksum), (c) the bounded arm is not
catastrophically slower (a loose 3× ceiling — a regression guard), and
(d) the DEFAULT budget keeps the pool at full physical width; measured times
are printed for the PR record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")

from brutex.config.hardware_envelope import polars_thread_budget

if TYPE_CHECKING:
    from pathlib import Path

_WORKER = textwrap.dedent(
    """
    import json, sys, threading, time
    from datetime import datetime

    import polars as pl

    rows = 120_000
    base = datetime(2024, 1, 1, 3, 45)
    df = pl.DataFrame(
        {
            "ts": pl.datetime_range(
                base, base + pl.duration(minutes=rows - 1), interval="1m", eager=True
            ),
            "o": pl.arange(0, rows, eager=True).cast(pl.Float64) * 1.5,
            "h": pl.arange(0, rows, eager=True).cast(pl.Float64) * 2.0,
            "l": pl.arange(0, rows, eager=True).cast(pl.Float64) * 0.5,
            "c": pl.arange(0, rows, eager=True).cast(pl.Float64),
            "v": pl.arange(0, rows, eager=True) % 997,
        }
    ).sort("ts")

    checksums = []
    lock = threading.Lock()

    def work() -> None:
        acc = 0.0
        for _ in range(6):
            out = (
                df.group_by_dynamic("ts", every="5m")
                .agg(
                    pl.col("o").first(),
                    pl.col("h").max(),
                    pl.col("l").min(),
                    pl.col("c").last(),
                    pl.col("v").sum(),
                )
                .sort("ts")
            )
            acc += float(out["c"].sum()) + float(out["v"].sum())
        with lock:
            checksums.append(acc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dur = time.perf_counter() - start
    print(
        json.dumps(
            {
                "seconds": dur,
                "pool": pl.thread_pool_size(),
                "checksum": sum(checksums),
            }
        )
    )
    """
)


def _run_child(extra_env: dict[str, str]) -> dict[str, object]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("POLARS_MAX_THREADS", "BRUTEX_POLARS_THREADS")
    }
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=300,
    )
    out: dict[str, object] = json.loads(proc.stdout.strip().splitlines()[-1])
    return out


class TestPolarsThreadBoundBench:
    def test_default_budget_keeps_full_physical_width(self) -> None:
        # The hostile-review MAJOR pin: the DEFAULT entrypoint budget must
        # keep the prebuild-path polars pool at FULL physical width.
        from brutex.config.hardware_envelope import cached_envelope

        default_budget = polars_thread_budget({})
        assert default_budget == cached_envelope().physical_cores
        child = _run_child({"POLARS_MAX_THREADS": str(default_budget)})
        assert child["pool"] == cached_envelope().physical_cores

    def test_bounded_vs_unbounded_two_concurrent_resample_workers(self, tmp_path: Path) -> None:
        # Mechanism A/B with an EXPLICIT divided budget (the default is full
        # width — see the pin above); proves the env-var bound takes effect
        # and measures its cost honestly.
        budget = polars_thread_budget({}, expected_workers=2)
        bounded = _run_child({"POLARS_MAX_THREADS": str(budget)})
        unbounded = _run_child({})

        # (a) the bound genuinely took effect in the bounded child.
        assert bounded["pool"] == budget
        # (b) byte-identical work — thread count changes parallelism only.
        assert bounded["checksum"] == unbounded["checksum"]
        # (c) contention guard, not a speedup claim: the bounded arm must not
        # be catastrophically slower on this small fixture.
        b_s = float(str(bounded["seconds"]))
        u_s = float(str(unbounded["seconds"]))
        assert b_s < u_s * 3.0
        # The PR-record line (run with -s to see it).
        sys.stdout.write(
            f"\npolars thread-bound bench: bounded({budget} thr)={b_s:.3f}s "
            f"unbounded({unbounded['pool']} thr)={u_s:.3f}s ratio={b_s / u_s:.2f}x\n"
        )
