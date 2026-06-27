"""Gate-2 at-scale write-depth oracle bench (DEC-ATSCALE-WRITEDEPTH-ORACLE-001, ledger #939).

The decision ledger repeatedly asserts the lake-write path is O(1) per write
(#867, #893, #921 …), and ``tests/bench/test_parquet_write_o1.py`` proves write
cost is independent of how MANY partitions the lake holds (lake BREADTH). But the
at-scale **write-DEPTH** bound — does a single ``ParquetWriter.write`` stay O(1) as
the *target month partition itself* grows from 1K to 100K+ rows? — was never
independently gated. This bench closes that Gate-2 gap.

It seeds one month partition to each depth tier, then TIMES a real merge-write of
a fresh block into the deep partition, and routes the ``{depth: latency}`` table
through the pure :mod:`brutex.data.write_depth_oracle` classifier — which reports a
ratio AND a fitted scaling exponent.

**Honest by construction (CLAUDE.md Golden Rule #10).** The merge-on-write step
reads the WHOLE stored month back (``parquet_writer.py`` ``_merge_write`` →
``_read_existing_bars``), unions by timestamp, and re-encodes the whole partition,
so the per-write cost is EXPECTED to scale with depth. This bench does NOT
manufacture an O(1) pass for an O(n) path: it asserts the MEASURED complexity
matches the documented expectation (sub-quadratic — linear-ish in depth, not
runaway), and prints the real ratio + exponent so the ledger claim is corrected to
"O(1) in lake breadth, O(depth) in a single partition's merge" — what is actually
true. (Should a future write path become genuinely O(1) in depth — e.g. an
append-only manifest lake that never re-reads the month — this same harness flips
to the strict ``≤ 1.1×`` gate via ``is_o1``; the test asserts on the honest verdict
either way.)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

# The CI ``bench`` job installs lean (no ``[data]`` extra), so ``polars`` — which
# ``ParquetWriter`` needs at write time — may be absent. Skip the whole module then,
# mirroring ``test_parquet_write_o1.py``.
pytest.importorskip("polars")

from brutex.data.parquet_writer import ParquetWriter
from brutex.data.write_depth_oracle import classify_scaling, make_depth_bars

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

#: Depth tiers measured on shared CI. Three decades (1K → 100K) give the log/log
#: fit real leverage to separate O(1) (slope ≈ 0) from O(depth) (slope ≈ 1) while
#: staying within a few seconds on a 2-core runner. The M4-Pro strict tier may
#: extend to 1M; the verdict maths is identical.
_DEPTH_TIERS: tuple[int, ...] = (1_000, 10_000, 100_000)

#: How many fresh bars to merge-write into the seeded partition per timed trial.
_FRESH_BLOCK = 50
#: Timed trials per tier; the p99 (max of a small sample) is the gate input.
_TRIALS = 7

#: Upper bound on the fitted exponent. The merge is a read-decode-union-encode of
#: the whole month, so ≈ linear (1.0) is EXPECTED + honest; allow up to 1.5 to
#: absorb decode/encode constant-factor curvature without permitting a runaway
#: super-quadratic regression. This is the honest at-scale gate: the path is NOT
#: O(1) in depth, but it MUST stay sub-quadratic.
_MAX_HONEST_EXPONENT: float = 1.5


def _time_one_merge_write(root: Path, seed_depth: int, trial: int) -> float:
    """Seed a partition to *seed_depth* rows, then time one merge-write (ns).

    The fresh block is placed disjoint from BUT contiguous with the seed (via
    ``start_index``), so the timed write is a genuine merge — it re-reads the whole
    ``seed_depth``-row month, unions the new bars, and re-encodes — which is the
    depth-sensitive cost we are measuring. A fresh lake per trial keeps the seed
    write out of the timed region.
    """
    lake = root / f"d{seed_depth}_t{trial}"
    writer = ParquetWriter(lake_root=lake)
    seed = make_depth_bars(seed_depth)
    writer.write(bars=seed, **_COORDS)  # untimed seed
    fresh = make_depth_bars(_FRESH_BLOCK, start_index=seed_depth)
    t0 = time.perf_counter_ns()
    writer.write(bars=fresh, **_COORDS)  # timed merge-write into a deep partition
    return float(time.perf_counter_ns() - t0)


def _p99_at_depth(root: Path, seed_depth: int) -> float:
    """The p99 (max of a small sample) merge-write latency at *seed_depth* (ns)."""
    # one warm trial (decode caches / import) excluded from the timed sample.
    _time_one_merge_write(root, min(seed_depth, 1_000), trial=-1)
    samples = [_time_one_merge_write(root, seed_depth, t) for t in range(_TRIALS)]
    samples.sort()
    return samples[-1]  # max ≈ p99 for a small sample (matches test_write_latency.py)


@pytest.mark.bench
def test_write_depth_scaling_is_honest(tmp_path: Path) -> None:
    """Measure merge-write latency across partition depth and assert the HONEST bound.

    Per CLAUDE.md Golden Rule #10 this does NOT fake an O(1) pass: the merge-write
    re-reads the whole month, so it is O(depth). The gate asserts the MEASURED
    exponent stays sub-quadratic (the path is well-behaved linear-ish, not a
    runaway), and prints the real ratio + exponent + label so the at-scale claim is
    grounded in a measurement, not an assumption.
    """
    latencies = tuple(_p99_at_depth(tmp_path, d) for d in _DEPTH_TIERS)
    result = classify_scaling(_DEPTH_TIERS, latencies)

    rows = "\n".join(
        f"    depth={d:>8,}  p99={lat / 1e6:8.3f} ms"
        for d, lat in zip(_DEPTH_TIERS, latencies, strict=True)
    )
    print(
        "\n[DEC-ATSCALE-WRITEDEPTH-ORACLE-001 write-depth oracle] "
        f"merge-write per partition depth ({_TRIALS} trials, {_FRESH_BLOCK}-bar block):\n"
        f"{rows}\n"
        f"    ratio(deep/shallow) = {result.ratio:.2f}x  "
        f"fitted exponent = {result.exponent:.3f}  "
        f"=> {result.complexity_label}  (is_o1={result.is_o1})"
    )

    # HONEST verdict: the merge path re-reads the whole month, so it is NOT O(1) in
    # depth — we do not assert is_o1. Instead we gate that the measured scaling is
    # the EXPECTED well-behaved (sub-quadratic) shape, never a runaway regression.
    assert result.exponent <= _MAX_HONEST_EXPONENT, (
        f"write-depth scaling exponent {result.exponent:.3f} exceeds the honest "
        f"sub-quadratic bound {_MAX_HONEST_EXPONENT} — the merge-write may have "
        f"regressed to super-linear-per-row (e.g. an O(n^2) union). Latencies: "
        f"{[f'{lat / 1e6:.3f}ms' for lat in latencies]}."
    )
    # Positivity sanity: real timings are positive, so the classifier never saw a
    # degenerate (zero/negative) sample.
    assert all(lat > 0.0 for lat in latencies)


@pytest.mark.bench
def test_write_depth_idempotent_bytes_across_depth(tmp_path: Path) -> None:
    """A re-merge of the SAME bars yields a byte-identical partition at every depth.

    The Gate-2 oracle's reliability leg (CLAUDE.md Golden Rule #7): re-pulling the
    identical block into an already-deep partition must be bit-for-bit stable — no
    new rows, the same on-disk bytes — independent of how deep the partition is.
    (A faster property-only version lives in ``tests/property``; this is the
    at-scale spot-check on the bench-sized depths.)
    """
    for depth in (1_000, 10_000):
        lake = tmp_path / f"idem_{depth}"
        writer = ParquetWriter(lake_root=lake)
        seed = make_depth_bars(depth)
        first = writer.write(bars=seed, **_COORDS)
        again = writer.write(bars=seed, **_COORDS)  # same bars re-merged
        assert again.raw_hash == first.raw_hash, (
            f"re-merge at depth {depth} changed the partition bytes "
            f"({first.raw_hash[:12]} -> {again.raw_hash[:12]}) — not idempotent"
        )
        assert again.row_count == depth
        assert again.conflicts == ()
