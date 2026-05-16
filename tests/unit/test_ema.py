"""Tests for `brutex.indicators.ema` — exponential moving average.

Verified against:
- Known closed-form: EMA(N=1) = the input sequence
- TradingView ta.ema() warm-up convention: bar (N-1) is the seed (SMA of first N)
- Hand-computed values for short sequences
"""

from __future__ import annotations

import math

import pytest

from brutex.indicators.ema import alpha, compute_ema, latest_ema


class TestAlpha:
    def test_alpha_period_20(self) -> None:
        # alpha = 2 / (20 + 1) = 2/21 ≈ 0.09524
        assert abs(alpha(20) - (2.0 / 21.0)) < 1e-12

    def test_alpha_period_200(self) -> None:
        assert abs(alpha(200) - (2.0 / 201.0)) < 1e-12

    def test_alpha_period_1(self) -> None:
        # alpha = 2 / 2 = 1.0 (degenerate but valid)
        assert alpha(1) == 1.0

    def test_alpha_period_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            alpha(0)


class TestComputeEma:
    def test_empty_input(self) -> None:
        assert compute_ema([], period=20) == []

    def test_period_1_returns_values(self) -> None:
        # Degenerate case: EMA(1) = identity
        values = [100.0, 101.0, 102.0]
        assert compute_ema(values, period=1) == values

    def test_warm_up_returns_none(self) -> None:
        # N = 5; bars 0..3 are warm-up (None); bar 4 is first valid
        values = [100.0] * 10
        result = compute_ema(values, period=5)
        assert result[:4] == [None, None, None, None]
        assert result[4] == 100.0  # SMA seed of constant 100s

    def test_constant_input_returns_constant_ema(self) -> None:
        # If all input values are 100, EMA must also be 100 from bar (N-1) onward
        values = [100.0] * 30
        result = compute_ema(values, period=10)
        # Bars 0..8 are None (warm-up); bar 9 = seed = SMA(first 10) = 100
        assert result[8] is None
        assert result[9] == 100.0
        for i in range(10, 30):
            assert result[i] is not None
            assert abs(result[i] - 100.0) < 1e-12  # type: ignore[operator]

    def test_known_short_sequence_period_3(self) -> None:
        """Hand-computed EMA for period=3.

        alpha = 2 / (3+1) = 0.5
        Values: [10, 20, 30, 40, 50]
        Bar 0: None (warm-up)
        Bar 1: None (warm-up)
        Bar 2: SMA(10, 20, 30) = 20.0 (seed)
        Bar 3: 0.5 * 40 + 0.5 * 20 = 30.0
        Bar 4: 0.5 * 50 + 0.5 * 30 = 40.0
        """
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = compute_ema(values, period=3)
        assert result[0] is None
        assert result[1] is None
        assert result[2] == 20.0
        assert result[3] == 30.0
        assert result[4] == 40.0

    def test_period_20_warm_up_length(self) -> None:
        # For period=20, first 19 entries must be None, entry 19 is the SMA seed
        values = [float(i) for i in range(1, 31)]  # [1, 2, ..., 30]
        result = compute_ema(values, period=20)
        assert all(r is None for r in result[:19])
        assert result[19] is not None
        # Seed = SMA(1..20) = (1+2+...+20)/20 = 210/20 = 10.5
        assert abs(result[19] - 10.5) < 1e-12  # type: ignore[operator]

    def test_increasing_sequence_ema_lags_below(self) -> None:
        # EMA should lag below a strictly increasing sequence
        values = [float(i) for i in range(1, 51)]
        result = compute_ema(values, period=10)
        # At bar 49 (the last bar), the current value is 50.0
        # The EMA must be < 50.0 (lagging)
        assert result[49] is not None
        assert result[49] < 50.0  # type: ignore[operator]
        # And > 1.0 (above the start)
        assert result[49] > 1.0  # type: ignore[operator]


class TestLatestEma:
    def test_latest_only(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        # Same as last entry of compute_ema(period=3)
        assert latest_ema(values, period=3) == 40.0

    def test_warm_up_returns_none(self) -> None:
        # Only 4 values, period=10 -> not enough data
        values = [1.0, 2.0, 3.0, 4.0]
        assert latest_ema(values, period=10) is None

    def test_empty_returns_none(self) -> None:
        assert latest_ema([], period=20) is None


class TestNoLookAhead:
    def test_ema_at_bar_n_only_uses_bars_0_to_n(self) -> None:
        """Property P-06 (instrumented bar accessor): EMA at bar N must
        be computable from only bars 0..N.

        This test verifies by comparison: EMA computed on prefix [0..N]
        must match EMA at index N when computed on the full series.
        """
        values = [float(i) * 1.5 + 100.0 for i in range(50)]
        full_ema = compute_ema(values, period=10)
        for n in range(10, 50):
            prefix_ema = compute_ema(values[: n + 1], period=10)
            # The EMA at bar N in the prefix series == EMA at bar N in the full series
            assert prefix_ema[n] == full_ema[n], (
                f"Look-ahead detected at bar {n}: "
                f"prefix={prefix_ema[n]} != full={full_ema[n]}"
            )
