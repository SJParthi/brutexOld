"""Unit tests for the pure write-depth-oracle helpers (DEC-ATSCALE-WRITEDEPTH-ORACLE-001).

Covers :mod:`brutex.data.write_depth_oracle` — the deterministic, timing-free core
of the Gate-2 at-scale write-depth oracle — at 100% line+branch. The wall-clock
timing of the real Parquet write lives in ``tests/bench/test_write_depth_oracle_bench.py``
(it owns the non-deterministic measurement); this file pins the data generation,
the power-law fit, and the honest O(1) / O(n) classification maths.
"""

from __future__ import annotations

import itertools
import math

import pytest
from brutex.data.write_depth_oracle import (
    DEFAULT_DEPTH_TIERS,
    O1_RATIO_CEILING,
    DepthScalingResult,
    classify_scaling,
    fit_scaling_exponent,
    make_depth_bars,
)


class TestMakeDepthBars:
    """``make_depth_bars`` is deterministic, contiguous, and gap-free."""

    def test_count_and_strictly_increasing(self) -> None:
        bars = make_depth_bars(100)
        assert len(bars) == 100
        for prev, nxt in itertools.pairwise(bars):
            assert nxt.timestamp > prev.timestamp

    def test_zero_count_is_empty(self) -> None:
        assert make_depth_bars(0) == []

    def test_byte_identical_on_repeat(self) -> None:
        """Same args → byte-identical bars (the idempotence the property test needs)."""
        assert make_depth_bars(50, start_index=7) == make_depth_bars(50, start_index=7)

    def test_start_index_makes_disjoint_contiguous_block(self) -> None:
        """A seed block + a start_index block do not overlap and are gap-free."""
        seed = make_depth_bars(40)
        timed = make_depth_bars(10, start_index=40)
        seed_ts = {b.timestamp for b in seed}
        timed_ts = {b.timestamp for b in timed}
        assert seed_ts.isdisjoint(timed_ts)
        # contiguous: the timed block starts exactly one bar-step after the seed end.
        assert timed[0].timestamp - seed[-1].timestamp == (seed[1].timestamp - seed[0].timestamp)

    def test_bars_are_valid_candles(self) -> None:
        """Every generated bar satisfies low <= open,close <= high (no CandleBar raise)."""
        for b in make_depth_bars(64):
            assert b.low <= b.open <= b.high
            assert b.low <= b.close <= b.high

    def test_open_interest_alternates_none(self) -> None:
        """Both the None and the int open_interest branches are exercised."""
        bars = make_depth_bars(6)
        ois = [b.open_interest for b in bars]
        assert any(oi is None for oi in ois)
        assert any(oi is not None for oi in ois)

    def test_negative_count_raises(self) -> None:
        with pytest.raises(ValueError, match="count must be >= 0"):
            make_depth_bars(-1)

    def test_negative_start_index_raises(self) -> None:
        with pytest.raises(ValueError, match="start_index must be >= 0"):
            make_depth_bars(1, start_index=-1)

    def test_spill_past_single_month_raises(self) -> None:
        """A block bigger than one month of seconds is refused (it would span partitions)."""
        with pytest.raises(ValueError, match="spill past one month"):
            make_depth_bars(2_678_401)

    def test_spill_via_start_index_raises(self) -> None:
        """The spill guard accounts for start_index, not just count."""
        with pytest.raises(ValueError, match="spill past one month"):
            make_depth_bars(2, start_index=2_678_399)


class TestFitScalingExponent:
    """The power-law least-squares fit separates O(1) from O(n)."""

    def test_flat_latency_is_o1_exponent_zero(self) -> None:
        exp = fit_scaling_exponent((1_000, 10_000, 100_000), (100.0, 100.0, 100.0))
        assert exp == pytest.approx(0.0, abs=1e-9)

    def test_perfectly_linear_is_exponent_one(self) -> None:
        # latency proportional to depth → slope 1 in log/log.
        tiers = (1_000, 10_000, 100_000)
        lats = tuple(float(t) for t in tiers)
        assert fit_scaling_exponent(tiers, lats) == pytest.approx(1.0, abs=1e-9)

    def test_sqrt_is_exponent_half(self) -> None:
        tiers = (1_000, 10_000, 100_000)
        lats = tuple(math.sqrt(t) for t in tiers)
        assert fit_scaling_exponent(tiers, lats) == pytest.approx(0.5, abs=1e-9)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            fit_scaling_exponent((1, 2), (1.0,))

    def test_too_few_points_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 2 tiers"):
            fit_scaling_exponent((1,), (1.0,))

    def test_nonpositive_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="depth tier must be positive"):
            fit_scaling_exponent((0, 10), (1.0, 2.0))

    def test_nonpositive_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency must be positive"):
            fit_scaling_exponent((1, 10), (1.0, 0.0))

    def test_identical_tiers_returns_flat_slope(self) -> None:
        """All-equal tiers (zero variance) → 0.0 rather than a divide-by-zero."""
        assert fit_scaling_exponent((5, 5), (1.0, 2.0)) == 0.0


class TestClassifyScaling:
    """The honest verdict: ratio gates O(1), exponent always reported."""

    def test_o1_when_ratio_within_ceiling(self) -> None:
        res = classify_scaling((1_000, 10_000, 100_000), (100.0, 102.0, 105.0))
        assert isinstance(res, DepthScalingResult)
        assert res.is_o1 is True
        assert res.ratio == pytest.approx(1.05)
        assert res.complexity_label == "O(1)"

    def test_not_o1_when_ratio_exceeds_ceiling_reports_real_exponent(self) -> None:
        """An O(depth) merge is NOT faked as a pass — the exponent is reported."""
        tiers = (1_000, 10_000, 100_000)
        lats = tuple(float(t) for t in tiers)  # linear
        res = classify_scaling(tiers, lats)
        assert res.is_o1 is False
        assert res.ratio == pytest.approx(100.0)
        assert res.exponent == pytest.approx(1.0, abs=1e-9)
        assert res.complexity_label == "O(n) (linear in depth)"

    def test_ratio_exactly_at_ceiling_is_o1(self) -> None:
        res = classify_scaling((1, 10), (100.0, 100.0 * O1_RATIO_CEILING))
        assert res.is_o1 is True

    def test_custom_ratio_ceiling(self) -> None:
        res = classify_scaling((1, 10), (100.0, 150.0), ratio_ceiling=2.0)
        assert res.is_o1 is True

    def test_nonpositive_ceiling_raises(self) -> None:
        with pytest.raises(ValueError, match="ratio_ceiling must be positive"):
            classify_scaling((1, 10), (1.0, 2.0), ratio_ceiling=0.0)

    def test_result_preserves_tiers_and_latencies(self) -> None:
        res = classify_scaling((1_000, 10_000), (10.0, 20.0))
        assert res.tiers == (1_000, 10_000)
        assert res.latencies_ns == (10.0, 20.0)


class TestComplexityLabel:
    """Every exponent bucket of ``complexity_label`` is reachable."""

    @staticmethod
    def _label(exponent: float) -> str:
        return DepthScalingResult(
            tiers=(1, 10),
            latencies_ns=(1.0, 1.0),
            ratio=1.0,
            exponent=exponent,
            is_o1=True,
        ).complexity_label

    def test_o1_bucket(self) -> None:
        assert self._label(0.05) == "O(1)"

    def test_sublinear_bucket(self) -> None:
        assert self._label(0.30) == "sub-linear (~O(n^0.30))"

    def test_sqrt_bucket(self) -> None:
        assert self._label(0.55) == "~O(sqrt(n))"

    def test_linear_bucket(self) -> None:
        assert self._label(1.0) == "O(n) (linear in depth)"

    def test_superlinear_bucket(self) -> None:
        assert self._label(1.8) == "super-linear (~O(n^1.80))"


def test_default_depth_tiers_span_three_decades() -> None:
    """The default ladder gives a power-law fit real leverage (1K → 100K)."""
    assert DEFAULT_DEPTH_TIERS == (1_000, 10_000, 100_000)
    assert DEFAULT_DEPTH_TIERS[-1] // DEFAULT_DEPTH_TIERS[0] == 100
