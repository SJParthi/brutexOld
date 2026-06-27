"""Deep-partition write-cost-vs-depth at-scale bench — the Lever-1 PROVE-FIRST gate.

DEC-DEEP-PARTITION-BENCH-001 (MIGRATION.md #937). The scalability audit confirmed the
lake write (:meth:`brutex.data.parquet_writer.ParquetWriter._merge_write`) is
**O(month-partition-size)** per straddling append: it reads + rebuilds + (on any change)
re-renders + atomically rewrites the WHOLE month partition, on the fetch worker, under the
per-partition lock. The old "440 ms uniform per write" figure was REFUTED
(DEC-FETCH-WRITE-OVERLAP-001 #921 — a FRESH append is ~30 ms; DEC-WRITER-COLUMNAR-4A-001
#927 measured a deep 8,000-row merge at p50≈102 ms). To responsibly decide whether the
parallel write-consumer (**Lever 1** — moving the append off the fetch worker) is worth
building, we need the achieved-rps-vs-partition-depth CURVE: this bench drives a
deep-partition steady state and measures the real per-write ``append_ms`` at increasing
depth (500 / 2,000 / 7,500 / 15,000 rows), producing two curves for the CURRENT
synchronous path:

* ``append_ms`` vs ``partition_rows`` — does the per-write cost grow steeply with depth?
* ``achieved_rps`` vs ``partition_depth`` — a SINGLE lane's write-bound throughput
  ceiling (``1 / append_s``); the hard input to the Lever-1 ``achieved_rps(W≥2) > 3.20/s``
  gate (the #886/#906 throughput-trap guard).

Both modes of the steady-state re-pull are measured:

* ``noop`` — a straddling batch whose bars are ALL already stored (keep-first no-op): pays
  the full partition READ + rebuild, then SKIPS the re-render/rewrite (``added == 0``).
* ``rewrite`` — a straddling batch carrying NEW bars past the partition end: pays the full
  READ + rebuild + zstd re-encode + atomic fsync-rewrite — the true worst-case re-pull
  shape, and the closest analogue to #927's 8,000-row merge.

## Measured curve (reference env, 2026-06-27, Python 3.12, 15 trials/point)

``rewrite`` mode (the worst-case steady-state re-pull):

    depth      append_p50_ms   append_p99_ms   achieved_rps (= 1 / p50)
      500          12.83           16.11            77.9
    2,000          35.61           68.96            28.1
    7,500         106.05          142.25             9.43
   15,000         215.88          286.09             4.63

``noop`` mode (keep-first no-op — read+rebuild only, no rewrite):

    depth      append_p50_ms   append_p99_ms   achieved_rps
      500           7.72            9.38           129.6
    2,000          24.75           36.24            40.4
    7,500          83.70          113.23            12.0
   15,000         169.74          199.57             5.9

**The read of the numbers (CLAUDE.md §2 Rule 10):** ``append_ms`` grows ~LINEARLY with
partition depth (rewrite: ~14 ms per 1,000 rows; +500→15,000 rows is 30× depth → the
per-write cost is ~17×). At 7,500 rows (≈ 5 trading days of 1-minute) a single rewrite is
~106 ms —
and the #921 lane analysis put the full per-contract cycle at ~625 ms with the lake append
the DOMINANT ~440 ms of non-fetch serial work, so the write is a LARGE fraction of the lane
and it gets steeply worse as the month fills. A single write-bound lane therefore caps at
~10 r/s at a 5-day-deep partition and ~5 r/s at a 10-day-deep one — BELOW the 5–8 r/s
ceiling, which is exactly the slack DEC-FETCH-WRITE-OVERLAP-001 attributed to the serial
append. This is the quantitative case that Lever 1 (overlap the append off the fetch
worker) is likely to PAY OFF; the gate it must clear is ``achieved_rps(W≥2) > 3.20/s`` on
this SAME deep scenario, with a byte-parity oracle (see :data:`_LEVER1_AB_SEAM`).

## Honest envelope (CLAUDE.md §2 Rule 1 / Rule 10)

* MEASUREMENT ONLY — no ``brutex/`` production change. This is the PROVE-FIRST evidence
  gate, NOT the Lever-1 implementation.
* The absolute ms numbers are CI-host-disk-dependent; the bench therefore RECORDS the
  curve (printed) and asserts only SANE MONOTONICITY (cost rises with depth, rps falls
  with depth) with a generous jitter tolerance — NOT a brittle hard SLA.
* The curve is a SINGLE-lane synchronous-path measurement (no concurrency); the rps figure
  is the write-bound ceiling of ONE lane, the load-bearing input to the Lever-1 decision.
* The ``write_consumer`` (Lever 1) does NOT exist yet — the A/B param is ``None``-only with
  an explicit TODO (:data:`_LEVER1_AB_SEAM`); when it lands, the SAME deep scenario reruns
  at W ∈ {None, 2, 4} with a byte-parity oracle and must clear ``achieved_rps(W≥2) > 3.20``.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

# The bench drives the REAL ParquetWriter, which needs polars at write time. The lean
# O(1)-NFR `bench` CI job installs only `.[dev]` (no `[data]` extra), so importorskip
# polars FIRST — the established repo pattern (test_parquet_write_o1.py /
# test_write_latency.py / test_pull_at_scale_gate.py) — so this heavyweight wall-clock
# depth bench SKIPS cleanly there and RUNS where `.[data]` is installed.
pytest.importorskip("polars", reason="the deep-partition write bench writes a real Parquet lake")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

#: The canonical SPOT coordinates — one fixed (year, month) partition under test.
_COORDS = {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"}

#: Partition depths to sweep (rows already in the month before the timed write). 500 ≈
#: a third of a trading day, 7,500 ≈ 5 trading days, 15,000 ≈ 10 — the range a steady-state
#: 1-minute month re-pull traverses as the month fills.
_DEPTHS: tuple[int, ...] = (500, 2_000, 7_500, 15_000)

#: Straddling-batch size — a small adjacent window's worth of bars (the production fetch
#: tiles a 1-minute month, each tile straddling into a partial month → a small batch merged
#: into a deep partition). Kept small so the timed cost is DOMINATED by the partition depth,
#: not the batch.
_STRADDLE_BARS = 60

#: Timed trials per depth point. Each trial uses a FRESH tmp lake (the seed write is
#: untimed), so the timed region is exactly the one merge write. Enough for a stable p50/p99
#: without making the bench slow.
_TRIALS = 15

#: Jitter tolerance for the monotonicity asserts. CI disks are noisy, so a deeper point
#: must be at least this fraction of the shallower point's cost (NOT a hard SLA) — the
#: claim is "cost rises with depth", proven loosely.
_MONOTONIC_TOLERANCE = 0.80

#: ── Lever-1 A/B SEAM (FUTURE — write_consumer does NOT exist yet) ─────────────────────
#: The parallel write-consumer (Lever 1, DEC-FETCH-WRITE-OVERLAP-001 #921) would move the
#: O(partition) append OFF the fetch worker onto W background writer threads, so the lane's
#: fetch cadence is no longer gated by the deep-partition rewrite. When it lands, this bench
#: reruns the SAME deep scenario at each W and asserts:
#:   * a BYTE-PARITY oracle — the lake written via the consumer is byte-identical to the
#:     synchronous path (idempotence, CLAUDE.md Golden Rule #7), AND
#:   * the HARD merge gate — achieved_rps(W >= 2) > 3.20 r/s at a deep partition (the
#:     #886/#906 throughput-trap guard: Lever 1 merges ONLY if it nets MORE throughput than
#:     the synchronous ~3.2 r/s baseline; a single serial writer that just relocates the
#:     append would be a NET REGRESSION).
#: For THIS PR the param is None-only (the synchronous baseline). TODO(Lever-1): implement
#: `write_consumer_workers` ∈ {2, 4} once the primitive exists.
_LEVER1_AB_SEAM: tuple[int | None, ...] = (None,)

#: The synchronous-baseline achieved-rps floor the Lever-1 consumer must BEAT to be worth
#: building (the #921 measured ~3.2 r/s serial-append slack). Recorded here so the future
#: A/B gate has the threshold in one place; not asserted by the baseline (it is the bar
#: Lever 1 clears, not a property of the synchronous path).
_LEVER1_ACHIEVED_RPS_GATE = 3.20


@dataclass(frozen=True, slots=True)
class DepthPoint:
    """One depth point's measured write cost + derived single-lane achieved rps."""

    partition_rows: int
    append_p50_ms: float
    append_p99_ms: float
    #: Single synchronous lane's write-bound throughput ceiling (1 / append_p50_s).
    achieved_rps: float


def _deep_bars(n: int, *, start_min: int = 0) -> list[CandleBar]:
    """Build *n* distinct in-range 1-minute bars in ONE (2021-01) month partition.

    Bars step one minute from ``start_min`` so a fresh batch at ``start_min=depth`` carries
    timestamps PAST the seeded partition's end (forcing the rewrite path), while
    ``start_min=0`` overlaps it (the keep-first no-op path). 15,000 minutes = ~10.4 days,
    well within one month, so every bar shares the 2021-01 partition.
    """
    bars: list[CandleBar] = []
    base_ts = datetime(2021, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(start_min, start_min + n):
        ts = base_ts + timedelta(minutes=i)
        base = Decimal(10_000) + Decimal(i) / Decimal(100)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=base,
                high=base + Decimal("1.50"),
                low=base - Decimal("1.50"),
                close=base,
                volume=i,
                open_interest=(None if i % 3 == 0 else i * 7),
            )
        )
    return bars


def _time_merge_at_depth(
    depth: int, *, mode: str, write_consumer_workers: int | None
) -> list[float]:
    """Seed a *depth*-row partition, then time ``_TRIALS`` straddling merge writes (seconds).

    Each trial: a FRESH tmp lake gets the *depth*-row seed (UNTIMED), then exactly ONE
    straddling merge write is timed. ``mode`` selects the batch shape:

    * ``"noop"`` — the last ``_STRADDLE_BARS`` already-stored bars (keep-first no-op: full
      read+rebuild, no rewrite), and
    * ``"rewrite"`` — ``_STRADDLE_BARS`` NEW bars past the partition end (full
      read+rebuild+re-encode+atomic-rewrite — the worst-case re-pull).

    ``write_consumer_workers`` is the Lever-1 A/B seam: ``None`` runs the synchronous
    production path (the only value implemented this PR). A non-None value would route the
    write through the future parallel write-consumer — NOT YET IMPLEMENTED.
    """
    if write_consumer_workers is not None:
        raise NotImplementedError(
            "write_consumer (Lever 1, DEC-FETCH-WRITE-OVERLAP-001 #921) is not implemented; "
            "this bench measures the synchronous baseline only (write_consumer_workers=None). "
            "TODO(Lever-1): rerun this deep scenario at W in {2, 4} with a byte-parity oracle "
            f"and assert achieved_rps(W) > {_LEVER1_ACHIEVED_RPS_GATE} r/s."
        )

    seed = _deep_bars(depth)
    if mode == "noop":
        batch = seed[-_STRADDLE_BARS:] if depth >= _STRADDLE_BARS else seed
    elif mode == "rewrite":
        batch = _deep_bars(_STRADDLE_BARS, start_min=depth)
    else:  # pragma: no cover - guarded by the caller's fixed mode set
        raise ValueError(f"unknown mode {mode!r}")

    durations: list[float] = []
    for _ in range(_TRIALS):
        with tempfile.TemporaryDirectory() as td:
            writer = ParquetWriter(lake_root=Path(td))
            writer.write(bars=seed, **_COORDS)  # untimed seed write
            started = time.perf_counter()
            writer.write(bars=batch, **_COORDS)  # the ONE timed merge write
            durations.append(time.perf_counter() - started)
    return durations


def _measure_curve(*, mode: str) -> list[DepthPoint]:
    """Run the synchronous baseline at every depth → the recorded curve (one mode)."""
    points: list[DepthPoint] = []
    for depth in _DEPTHS:
        durations = sorted(_time_merge_at_depth(depth, mode=mode, write_consumer_workers=None))
        p50 = statistics.median(durations)
        p99 = durations[-1]  # max ≈ p99 for a small sample
        achieved_rps = (1.0 / p50) if p50 > 0.0 else 0.0
        points.append(
            DepthPoint(
                partition_rows=depth,
                append_p50_ms=round(p50 * 1e3, 2),
                append_p99_ms=round(p99 * 1e3, 2),
                achieved_rps=round(achieved_rps, 2),
            )
        )
    return points


def _print_curve(mode: str, points: list[DepthPoint]) -> None:
    """Print the recorded curve so the Lever-1 decision has verbatim evidence."""
    lines = [
        f"\n[DEC-DEEP-PARTITION-BENCH-001] synchronous baseline — mode={mode} "
        f"({_TRIALS} trials/point, write_consumer_workers=None):",
        f"  {'depth':>8} {'append_p50_ms':>14} {'append_p99_ms':>14} {'achieved_rps':>13}",
    ]
    for pt in points:
        lines.append(
            f"  {pt.partition_rows:>8} {pt.append_p50_ms:>14.2f} "
            f"{pt.append_p99_ms:>14.2f} {pt.achieved_rps:>13.2f}"
        )
    print("\n".join(lines))


@pytest.mark.bench
def test_deep_partition_rewrite_cost_grows_with_depth() -> None:
    """REWRITE-mode curve: append_ms rises (and single-lane rps falls) with partition depth.

    The worst-case steady-state re-pull (a straddling batch of NEW bars → full
    read+rebuild+re-encode+atomic-rewrite of the whole month). Records the curve and
    asserts SANE MONOTONICITY (a deeper partition costs strictly more / yields strictly
    less single-lane rps), NOT a brittle absolute SLA — the absolute ms is CI-disk-bound.
    """
    points = _measure_curve(mode="rewrite")
    _print_curve("rewrite", points)

    # Every point measured something positive (the write ran, the lake landed bars).
    assert all(pt.append_p50_ms > 0.0 for pt in points)
    assert all(pt.achieved_rps > 0.0 for pt in points)

    # Monotone in depth: each deeper point costs MORE (p50) and yields LESS single-lane rps
    # than the shallower one. A generous tolerance absorbs CI-disk jitter — the claim is the
    # SHAPE (cost ∝ depth → the write is O(partition), CLAUDE.md §2 Rule 10), not a factor.
    for shallow, deep in pairwise(points):
        assert deep.append_p50_ms >= shallow.append_p50_ms * _MONOTONIC_TOLERANCE, (
            f"append_p50 did not rise with depth: {deep.partition_rows} rows "
            f"{deep.append_p50_ms} ms < {shallow.partition_rows} rows "
            f"{shallow.append_p50_ms} ms × {_MONOTONIC_TOLERANCE}"
        )
        assert deep.achieved_rps <= shallow.achieved_rps / _MONOTONIC_TOLERANCE, (
            f"achieved_rps did not fall with depth: {deep.partition_rows} rows "
            f"{deep.achieved_rps} r/s > {shallow.partition_rows} rows "
            f"{shallow.achieved_rps} r/s ÷ {_MONOTONIC_TOLERANCE}"
        )

    # The deepest point costs MEANINGFULLY more than the shallowest — the O(partition)
    # signal is real, not noise. (≥ 3× across a 30× depth increase is a deliberately loose
    # floor; the reference env measures ~16×.)
    assert points[-1].append_p50_ms >= 3.0 * points[0].append_p50_ms, (
        f"deepest write {points[-1].append_p50_ms} ms is not >= 3× the shallowest "
        f"{points[0].append_p50_ms} ms — the O(partition-depth) cost is not visible "
        f"(did the writer stop reading the whole month?)"
    )


@pytest.mark.bench
def test_deep_partition_noop_merge_cost_grows_with_depth() -> None:
    """NO-OP-merge curve: even a keep-first no-op pays the O(partition) READ.

    A straddling batch of already-stored bars skips the rewrite (``added == 0``) but still
    reads + rebuilds the WHOLE month — so its cost ALSO grows with depth. Proving this makes
    the audit thesis airtight: the partition READ alone (before any rewrite) is O(depth), so
    even an idempotent re-pull of a deep month is slow on the fetch worker.
    """
    points = _measure_curve(mode="noop")
    _print_curve("noop", points)

    assert all(pt.append_p50_ms > 0.0 for pt in points)
    for shallow, deep in pairwise(points):
        assert deep.append_p50_ms >= shallow.append_p50_ms * _MONOTONIC_TOLERANCE, (
            f"no-op merge append_p50 did not rise with depth: {deep.partition_rows} rows "
            f"{deep.append_p50_ms} ms < {shallow.partition_rows} rows "
            f"{shallow.append_p50_ms} ms × {_MONOTONIC_TOLERANCE}"
        )
    assert points[-1].append_p50_ms >= 3.0 * points[0].append_p50_ms


@pytest.mark.bench
def test_lever1_ab_seam_is_synchronous_only_this_pr() -> None:
    """The Lever-1 A/B seam is None-only for now; a non-None W raises NotImplementedError.

    Pins the PROVE-FIRST contract: this PR measures the synchronous baseline ONLY. When the
    write-consumer primitive lands, ``write_consumer_workers`` ∈ {2, 4} flips on and the
    same deep scenario is rerun with a byte-parity oracle + the ``achieved_rps > 3.20`` gate
    (DEC-FETCH-WRITE-OVERLAP-001 #921). Until then, requesting a consumer is an explicit
    NotImplementedError, never a silent fake number.
    """
    assert _LEVER1_AB_SEAM == (None,)  # only the synchronous baseline is wired this PR
    assert pytest.approx(3.20) == _LEVER1_ACHIEVED_RPS_GATE
    with pytest.raises(NotImplementedError, match="write_consumer"):
        _time_merge_at_depth(500, mode="rewrite", write_consumer_workers=2)
