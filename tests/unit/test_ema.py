"""Tests for `brutex.indicators.ema` — exponential moving average.

Verified against:
- Known closed-form: EMA(N=1) = the input sequence
- TradingView ta.ema() warm-up convention: bar (N-1) is the seed (SMA of first N)
- Hand-computed values for short sequences
"""

from __future__ import annotations

import pytest
from brutex.indicators.ema import EMAState, alpha, compute_ema, latest_ema


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


class TestComputeEmaInvalidPeriod:
    def test_period_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            compute_ema([1.0, 2.0, 3.0], period=0)

    def test_period_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            compute_ema([1.0, 2.0, 3.0], period=-5)


class TestStateLifecycle:
    def test_reset_clears_value_and_counters(self) -> None:
        state = EMAState.new(period=3)
        state.update(10.0)
        state.update(20.0)
        state.update(30.0)
        # Snapshot via to_dict so we don't narrow state.value to float
        # — that would make the post-reset `is None` assertion below
        # unreachable per mypy's strict inference.
        populated = state.to_dict()
        assert populated["value"] == 20.0
        assert populated["bars_seen"] == 3
        assert populated["seed_sum"] == 60.0

        state.reset()
        fresh = state.to_dict()
        assert fresh["value"] is None
        assert fresh["bars_seen"] == 0
        assert fresh["seed_sum"] == 0.0

    def test_reset_allows_clean_replay(self) -> None:
        state = EMAState.new(period=3)
        for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
            state.update(v)
        assert state.to_dict()["value"] == 40.0
        state.reset()
        replay = [state.update(v) for v in [10.0, 20.0, 30.0, 40.0, 50.0]]
        assert replay == [None, None, 20.0, 30.0, 40.0]


class TestStateSerialization:
    def test_to_dict_during_warmup(self) -> None:
        state = EMAState.new(period=5)
        state.update(10.0)
        state.update(20.0)
        d = state.to_dict()
        assert d == {
            "period": 5,
            "alpha": 2.0 / 6.0,
            "value": None,
            "bars_seen": 2,
            "seed_sum": 30.0,
        }

    def test_to_dict_post_seed(self) -> None:
        state = EMAState.new(period=3)
        for v in [10.0, 20.0, 30.0, 40.0]:
            state.update(v)
        d = state.to_dict()
        assert d["period"] == 3
        assert d["value"] == 30.0
        assert d["bars_seen"] == 4

    def test_from_dict_round_trip(self) -> None:
        original = EMAState.new(period=3)
        for v in [10.0, 20.0, 30.0, 40.0]:
            original.update(v)

        snapshot = original.to_dict()
        restored = EMAState.from_dict(snapshot)
        assert restored.value == original.value
        assert restored.bars_seen == original.bars_seen
        assert restored.seed_sum == original.seed_sum
        assert restored.period == original.period
        assert restored.alpha == original.alpha

        next_value_original = original.update(50.0)
        next_value_restored = restored.update(50.0)
        assert next_value_original == next_value_restored


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
                f"Look-ahead detected at bar {n}: prefix={prefix_ema[n]} != full={full_ema[n]}"
            )
