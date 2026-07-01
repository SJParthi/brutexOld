"""Tests for `brutex.indicators.ema` — exponential moving average.

Verified against:
- Known closed-form: EMA(N=1) = the input sequence
- TradingView ta.ema() warm-up convention: bar (N-1) is the seed (SMA of first N)
- Hand-computed values for short sequences
"""

from __future__ import annotations

import math

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
        assert abs(result[19] - 10.5) < 1e-12

    def test_increasing_sequence_ema_lags_below(self) -> None:
        # EMA should lag below a strictly increasing sequence
        values = [float(i) for i in range(1, 51)]
        result = compute_ema(values, period=10)
        # At bar 49 (the last bar), the current value is 50.0
        # The EMA must be < 50.0 (lagging)
        assert result[49] is not None
        assert result[49] < 50.0
        # And > 1.0 (above the start)
        assert result[49] > 1.0


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


class TestStatePeriodOne:
    """`EMAState.update` with period == 1 collapses to the identity (no
    warm-up, no seed): every bar's EMA equals that bar's close. This exercises
    the streaming period-1 fast path that `compute_ema` (the batch Rust call)
    never routes through.
    """

    def test_update_period_1_returns_each_close(self) -> None:
        state = EMAState.new(period=1)
        out = [state.update(v) for v in [10.0, 25.5, 7.0, 7.0, 99.0]]
        # Identity: EMA == close on every bar, never None (no warm-up).
        assert out == [10.0, 25.5, 7.0, 7.0, 99.0]

    def test_update_period_1_tracks_bars_and_value(self) -> None:
        state = EMAState.new(period=1)
        assert state.update(42.0) == 42.0
        d = state.to_dict()
        assert d["value"] == 42.0
        assert d["bars_seen"] == 1
        # period-1 never accumulates a warm-up seed
        assert d["seed_sum"] == 0.0

    def test_update_period_1_matches_compute_ema(self) -> None:
        values = [10.0, 25.5, 7.0, 7.0, 99.0]
        state = EMAState.new(period=1)
        streamed = [state.update(v) for v in values]
        assert streamed == compute_ema(values, period=1)


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


class TestNonFiniteGuard:
    """`EMAState.update` must fail-fast on a non-finite close so a NaN/inf
    can never silently poison every subsequent EMA value."""

    def test_update_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            EMAState.new(20).update(float("nan"))

    def test_update_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            EMAState.new(20).update(float("inf"))

    def test_update_neg_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            EMAState.new(20).update(float("-inf"))

    def test_guard_runs_before_state_mutation(self) -> None:
        # A rejected close must NOT advance bars_seen or accumulate seed_sum.
        state = EMAState.new(5)
        state.update(100.0)
        before = state.to_dict()
        with pytest.raises(ValueError, match="must be finite"):
            state.update(math.nan)
        assert state.to_dict() == before

    def test_compute_ema_with_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            compute_ema([100.0, float("nan"), 102.0], period=2)

    def test_latest_ema_with_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            latest_ema([100.0, float("inf")], period=2)

    def test_finite_series_unchanged(self) -> None:
        # Regression: a normal finite series computes exactly as before the
        # guard was added (the hand-computed period=3 oracle).
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert compute_ema(values, period=3) == [None, None, 20.0, 30.0, 40.0]


class TestStreamingSeedOverflowGuard:
    """`EMAState.update` (the LIVE streaming path in `engine.stage1a`) must RAISE
    on a seed that overflows to a non-finite value — never return `inf` and
    poison every subsequent bar.

    DEC-EMA-PARITY-001: before this fix the streaming `update` returned `+inf`
    from an overflowed SMA seed (`trading_core.ema_seed` is a bare division that
    only guards `period < 1`), diverging from BOTH the Rust core's
    `EmaState::update` seed guard AND the Python batch path (`compute_ema`, which
    delegates to that same Rust `update`). The streaming path now re-checks
    `math.isfinite(seed)` — the streaming twin of the Rust seed guard — so
    streaming == batch == Rust.
    """

    def test_update_overflowing_finite_seed_raises_not_inf(self) -> None:
        # Two finite-but-huge closes overflow `seed_sum` to +inf; the SEED bar
        # (bars_seen == period) must RAISE, not return inf.
        state = EMAState.new(period=2)
        assert state.update(1e308) is None  # warm-up bar (bars_seen 1 < 2)
        with pytest.raises(ValueError, match="seed overflowed to a non-finite value"):
            state.update(1e308)  # seed bar (bars_seen == 2) — overflows

    def test_update_overflowing_seed_never_returns_non_finite(self) -> None:
        # Belt-and-suspenders: prove the streaming path can NEVER emit a
        # non-finite EMA on the seed bar (the poisoning the guard prevents) —
        # collect every emitted value and assert the seed bar raised instead of
        # slipping an ``inf`` through.
        state = EMAState.new(period=3)
        emitted: list[float | None] = [state.update(1e308), state.update(1e308)]
        with pytest.raises(ValueError, match="the warm-up close sum is too large"):
            emitted.append(state.update(1e308))  # seed bar — must RAISE, never inf
        # No non-finite value ever reached the caller (all warm-up ``None``s).
        assert all(v is None or math.isfinite(v) for v in emitted)

    def test_streaming_seed_overflow_matches_batch_and_rust(self) -> None:
        # Parity: for the SAME overflowing inputs the streaming update, the
        # Python batch path, and the Rust FFI all raise the byte-identical
        # ValueError message (streaming == batch == Rust).
        import trading_core

        values = [1e308, 1e308]

        def _streaming_error() -> str:
            state = EMAState.new(period=2)
            for value in values:
                state.update(value)
            raise AssertionError("streaming did not raise")  # pragma: no cover

        streaming_msg = _capture_value_error(_streaming_error)
        batch_msg = _capture_value_error(lambda: (compute_ema(values, period=2), None)[1])
        rust_msg = _capture_value_error(lambda: (trading_core.compute_ema(values, 2), None)[1])

        assert streaming_msg == batch_msg == rust_msg
        assert "seed overflowed to a non-finite value" in streaming_msg

    def test_update_guard_runs_before_seed_state_mutation(self) -> None:
        # The overflow guard fires on the SEED bar; `value` must stay `None`
        # (never a stored inf) so a caught error leaves no poisoned state.
        state = EMAState.new(period=2)
        state.update(1e308)  # warm-up
        with pytest.raises(ValueError, match="seed overflowed"):
            state.update(1e308)  # seed bar overflows
        assert state.to_dict()["value"] is None

    def test_finite_seed_streaming_unchanged(self) -> None:
        # Regression: a normal finite series streams exactly as before the seed
        # guard was added (the hand-computed period=3 oracle), and streaming ==
        # batch bit-for-bit.
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        state = EMAState.new(period=3)
        streamed = [state.update(v) for v in values]
        assert streamed == [None, None, 20.0, 30.0, 40.0]
        assert streamed == compute_ema(values, period=3)


def _capture_value_error(fn: object) -> str:
    """Run a zero-arg callable expected to raise ``ValueError`` and return its
    message. Fails the test if it does not raise."""
    try:
        fn()  # type: ignore[operator]
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a ValueError but none was raised")  # pragma: no cover


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
