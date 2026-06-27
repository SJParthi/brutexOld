"""At-scale write-depth oracle helpers (DEC-ATSCALE-WRITEDEPTH-ORACLE-001, ledger #939).

The repeated O(1)-per-write claims in the decision ledger (#867, #893, #921 …) are
asserted for the lake-write path but the **at-scale write-depth bound** — does a
single ``ParquetWriter.write`` stay O(1) as the *target month partition* itself
grows DEEP (1K → 1M rows in ONE file)? — was never independently gated. The
existing ``tests/bench/test_parquet_write_o1.py`` proves write cost is independent
of how MANY partitions the lake holds (lake BREADTH); it says nothing about a
partition's DEPTH, which is a different axis: the merge-on-write step reads the
WHOLE stored month back, unions by timestamp, and re-encodes it
(``brutex/data/parquet_writer.py`` ``_merge_write`` → ``_read_existing_bars`` →
``_render_bytes``), so a re-pull into an N-row partition plausibly costs O(N), not
O(1).

This module is the **deterministic, timing-free** core of the Gate-2 oracle: the
data generation, the depth-tier ladder, and the maths that turn a measured
``{depth: latency}`` table into an HONEST verdict — a ratio AND a fitted scaling
exponent — so the bench test (which owns the wall-clock timing) can either gate a
true O(1) path at ``≤ 1.1×`` OR, if the path is genuinely O(depth), report the
measured complexity instead of manufacturing a false pass (CLAUDE.md Golden
Rule #10). Engine UNCHANGED (NIFTY/BANKNIFTY/SENSEX); this is a read-only test
harness over production code.

All public functions here are pure (no I/O, no timing, no global state), so they
are covered 100% line+branch by fast unit tests; the only non-deterministic part —
``time.perf_counter`` — lives in the bench test, never here (CLAUDE.md §11 COMMON:
put the logic in one covered place, the timing in the bench).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from brutex.data.models import CandleBar

__all__ = [
    "DEFAULT_DEPTH_TIERS",
    "O1_RATIO_CEILING",
    "DepthScalingResult",
    "classify_scaling",
    "fit_scaling_exponent",
    "make_depth_bars",
]

#: The decreed O(1) latency-ratio ceiling (CLAUDE.md §2 Rule 5 / §12). A write
#: path is "O(1) in depth" only if ``p99(deep) / p99(shallow) ≤`` this.
O1_RATIO_CEILING: Final[float] = 1.1

#: Fitted-exponent bucket edges for :meth:`DepthScalingResult.complexity_label`.
#: A slope below :data:`_EXP_O1` reads as O(1); below :data:`_EXP_SUBLINEAR` as
#: a named sub-linear power; below :data:`_EXP_SQRT` as ~O(√n); below
#: :data:`_EXP_LINEAR` as O(n); anything higher is super-linear. Named so the
#: thresholds live in ONE place (no magic numbers in the branch).
_EXP_O1: Final[float] = 0.15
_EXP_SUBLINEAR: Final[float] = 0.40
_EXP_SQRT: Final[float] = 0.75
_EXP_LINEAR: Final[float] = 1.25

#: A power-law fit needs at least this many distinct (depth, latency) points to
#: define a slope.
_MIN_FIT_POINTS: Final[int] = 2

#: The standard depth ladder the oracle measures across: each tier is the number
#: of bars already resident in ONE month partition before a write is timed. Spans
#: three decades (1K → 100K) so a power-law fit has enough leverage to separate
#: O(1) (exponent ≈ 0) from O(n) (exponent ≈ 1). The bench may pass its own
#: (smaller, on a shared runner; up to 1M on the M4-Pro tier).
DEFAULT_DEPTH_TIERS: Final[tuple[int, ...]] = (1_000, 10_000, 100_000)

#: A fixed epoch for generated bars. One bar per SECOND keeps every timestamp
#: distinct AND keeps a deep block inside a SINGLE ``(year, month)`` partition —
#: a calendar month holds ~2.6M seconds, so even a 1M-row depth block stays in one
#: month (``ParquetWriter.write`` rejects bars that span partitions). One-second
#: spacing is the right unit for a write-DEPTH bench: the merge cost tracks the
#: total rows merged in one partition, not the wall-clock span they represent.
_BARS_EPOCH: Final[datetime] = datetime(2021, 1, 1, 0, 0, tzinfo=UTC)

#: The hard depth ceiling that still fits one month at one-second spacing. January
#: has 31 days = 2,678,400 seconds; cap generation below that so a caller can never
#: silently spill across the month boundary into a ``WriterError``.
_MAX_SINGLE_MONTH_BARS: Final[int] = 2_678_400


def make_depth_bars(count: int, *, start_index: int = 0) -> list[CandleBar]:
    """Return *count* deterministic, strictly-increasing-timestamp :class:`CandleBar`.

    Each bar is one SECOND after the previous, beginning ``start_index`` seconds
    after a fixed epoch — so calling ``make_depth_bars(n)`` and
    ``make_depth_bars(m, start_index=n)`` yields two NON-overlapping, contiguous
    blocks (a seed block + a disjoint timed block), and re-calling with the same
    arguments returns byte-identical bars (the idempotence the property test
    relies on). Every bar stays inside ONE ``(year, month)`` partition (the
    one-second spacing keeps even a 1M-row block within a single month, which
    ``ParquetWriter.write`` requires). Prices are a smooth deterministic ramp
    snapped to 2dp so every bar is a valid ``low ≤ open,close ≤ high`` candle.

    Parameters
    ----------
    count
        Number of bars to generate. Must be ``>= 0``; ``0`` returns ``[]``. The
        total span (``start_index + count``) must stay within one month's worth
        of seconds (:data:`_MAX_SINGLE_MONTH_BARS`) so the block never spills the
        partition boundary.
    start_index
        Second offset from the fixed epoch for the first bar (lets the caller
        place a timed block immediately after a seed block with no overlap).

    Returns
    -------
    list[CandleBar]
        ``count`` bars, timestamps strictly increasing by one second, all in one
        month partition.

    Raises
    ------
    ValueError
        If *count* or *start_index* is negative, or the block would spill past a
        single month partition.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if start_index < 0:
        raise ValueError(f"start_index must be >= 0, got {start_index}")
    if start_index + count > _MAX_SINGLE_MONTH_BARS:
        raise ValueError(
            f"start_index + count ({start_index + count}) would spill past one "
            f"month partition (max {_MAX_SINGLE_MONTH_BARS} one-second bars)"
        )
    bars: list[CandleBar] = []
    for i in range(start_index, start_index + count):
        ts = _BARS_EPOCH + timedelta(seconds=i)
        # A deterministic ramp; modulo keeps the magnitude bounded but every
        # adjacent bar distinct. Snap to 2dp so it is already on the tick grid.
        base = Decimal(10_000) + (Decimal(i % 5_000) / Decimal(100))
        bars.append(
            CandleBar(
                timestamp=ts,
                open=base,
                high=base + Decimal("1.50"),
                low=base - Decimal("1.50"),
                close=base + Decimal("0.50"),
                volume=i % 1_000,
                open_interest=None if i % 3 == 0 else (i % 7_000) * 11,
            )
        )
    return bars


@dataclass(frozen=True, slots=True)
class DepthScalingResult:
    """The honest verdict of a write-depth measurement (DEC-ATSCALE-WRITEDEPTH-ORACLE-001).

    Attributes
    ----------
    tiers
        The depth tiers measured (ascending), e.g. ``(1000, 10000, 100000)``.
    latencies_ns
        The measured per-write latency at each tier, nanoseconds, same order as
        *tiers*.
    ratio
        ``latency(deepest) / latency(shallowest)`` — the headline O(1) number the
        bench gate compares against :data:`O1_RATIO_CEILING`.
    exponent
        The fitted power-law exponent ``b`` in ``latency ≈ a · depth**b`` (least
        squares on log/log). ``≈ 0`` ⇒ O(1); ``≈ 1`` ⇒ O(depth); ``≈ 0.5`` ⇒
        O(√depth). The HONEST complexity descriptor when the ratio fails the O(1)
        gate.
    is_o1
        ``True`` iff *ratio* ``≤`` :data:`O1_RATIO_CEILING` — i.e. the path is
        gate-2-clean O(1) in partition depth.
    """

    tiers: tuple[int, ...]
    latencies_ns: tuple[float, ...]
    ratio: float
    exponent: float
    is_o1: bool

    @property
    def complexity_label(self) -> str:
        """A human-readable big-O label derived from the fitted exponent.

        Buckets the exponent into the nearest familiar class so the bench output
        and the PR body can state the MEASURED complexity in plain words rather
        than a bare float (Golden Rule #10 — report what is actually true).
        """
        b = self.exponent
        if b < _EXP_O1:
            return "O(1)"
        if b < _EXP_SUBLINEAR:
            return f"sub-linear (~O(n^{b:.2f}))"
        if b < _EXP_SQRT:
            return "~O(sqrt(n))"
        if b < _EXP_LINEAR:
            return "O(n) (linear in depth)"
        return f"super-linear (~O(n^{b:.2f}))"


def fit_scaling_exponent(tiers: tuple[int, ...], latencies_ns: tuple[float, ...]) -> float:
    """Least-squares power-law exponent ``b`` for ``latency ≈ a · depth**b``.

    Fits a straight line to ``(log depth, log latency)`` and returns its slope —
    the scaling exponent. An O(1) path yields a slope ≈ 0 (flat); an O(n) path
    yields a slope ≈ 1. This is the HONEST complexity descriptor the oracle
    reports when the simple ratio fails the O(1) gate, so an O(depth) merge is
    NEVER dressed up as an O(1) pass (CLAUDE.md Golden Rule #10).

    Parameters
    ----------
    tiers
        The depth tiers (all strictly positive — they are row counts).
    latencies_ns
        The measured latency at each tier (all strictly positive — real timings).
        Must be the same length as *tiers*, length ``>= 2`` (a slope needs two
        points).

    Returns
    -------
    float
        The fitted slope ``b``. ``0.0`` for a perfectly flat measurement.

    Raises
    ------
    ValueError
        If the inputs are mismatched in length, shorter than 2, or contain a
        non-positive value (a log is undefined there).
    """
    if len(tiers) != len(latencies_ns):
        raise ValueError(
            f"tiers ({len(tiers)}) and latencies ({len(latencies_ns)}) must be the same length"
        )
    if len(tiers) < _MIN_FIT_POINTS:
        raise ValueError("need at least 2 tiers to fit a scaling exponent")
    if any(t <= 0 for t in tiers):
        raise ValueError("every depth tier must be positive (it is a row count)")
    if any(v <= 0.0 for v in latencies_ns):
        raise ValueError("every latency must be positive (it is a real timing)")

    xs = [math.log(t) for t in tiers]
    ys = [math.log(v) for v in latencies_ns]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0.0:
        # All tiers identical → no leverage to fit a slope. A degenerate input the
        # caller should not pass; report a flat (O(1)) slope rather than divide by
        # zero. (Unreachable via DEFAULT_DEPTH_TIERS — distinct tiers — but kept
        # total for the pure-function contract.)
        return 0.0
    return cov / var_x


def classify_scaling(
    tiers: tuple[int, ...],
    latencies_ns: tuple[float, ...],
    *,
    ratio_ceiling: float = O1_RATIO_CEILING,
) -> DepthScalingResult:
    """Turn a measured ``{depth: latency}`` table into an honest verdict.

    Computes the headline ``latency(deepest)/latency(shallowest)`` ratio AND the
    fitted power-law exponent, and marks the path O(1) ONLY if the ratio clears
    *ratio_ceiling*. The exponent is always reported so a non-O(1) path is
    described by its REAL complexity instead of a manufactured pass (CLAUDE.md
    Golden Rule #10).

    Parameters
    ----------
    tiers
        Depth tiers, ascending, length ``>= 2``, all positive.
    latencies_ns
        Measured per-write latency at each tier (same order, all positive).
    ratio_ceiling
        The O(1) gate. Defaults to the decreed :data:`O1_RATIO_CEILING` (1.1×);
        the bench passes a looser shared-CI ceiling and the strict tier passes
        1.1× (the ``tests/bench/_o1_tiers.py`` pattern).

    Returns
    -------
    DepthScalingResult
        The ratio, exponent, label, and O(1) flag.

    Raises
    ------
    ValueError
        Propagated from :func:`fit_scaling_exponent` for malformed input, or if
        *ratio_ceiling* is not positive.
    """
    if ratio_ceiling <= 0.0:
        raise ValueError(f"ratio_ceiling must be positive, got {ratio_ceiling}")
    exponent = fit_scaling_exponent(tiers, latencies_ns)
    shallow = latencies_ns[0]
    deep = latencies_ns[-1]
    ratio = deep / shallow
    return DepthScalingResult(
        tiers=tuple(tiers),
        latencies_ns=tuple(latencies_ns),
        ratio=ratio,
        exponent=exponent,
        is_o1=ratio <= ratio_ceiling,
    )
