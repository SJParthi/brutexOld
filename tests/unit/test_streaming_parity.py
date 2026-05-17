"""Streaming/batch parity tests — same math, two interfaces.

Verifies that for EVERY indicator with both a streaming state class and
a batch helper, the two paths produce BIT-IDENTICAL output.

This is the load-bearing test for the operator's requirement (2026-05-16):
    "Indicators must be a common structure used by both backtesting
     and live TickVault."

If this test ever fails, it means a code path drifted and backtest will
not match live. CATASTROPHIC for strategy validity.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from brutex.indicators.day_extremes import (
    DayExtremesState,
    compute_day_extremes,
)
from brutex.indicators.ema import EMAState, compute_ema

IST = ZoneInfo("Asia/Kolkata")


class TestEmaStreamingMatchesBatch:
    """For any sequence of closes, running EMAState.update() must produce
    the same series as compute_ema(values, period)."""

    @pytest.mark.parametrize("period", [1, 3, 5, 14, 20, 50, 200])
    def test_streaming_equals_batch_for_known_sequence(self, period: int) -> None:
        # Generate a deterministic sequence
        closes = [100.0 + 0.5 * (i % 7) + 0.1 * i for i in range(500)]

        # Batch
        batch = compute_ema(closes, period=period)

        # Streaming
        state = EMAState.new(period)
        streaming = [state.update(c) for c in closes]

        assert len(batch) == len(streaming)
        for i, (b, s) in enumerate(zip(batch, streaming)):
            if b is None and s is None:
                continue
            assert b is not None and s is not None
            assert b == s, f"Drift at bar {i}: batch={b} streaming={s}"

    def test_streaming_serialize_restore_continues_correctly(self) -> None:
        """Checkpoint mid-stream → restore → continue. Must match a fresh
        single-pass run."""
        closes = [100.0 + i * 0.1 for i in range(50)]

        # Path 1: single uninterrupted pass
        state_full = EMAState.new(20)
        full_results = [state_full.update(c) for c in closes]

        # Path 2: process first 25 → serialize → restore → process remaining 25
        state_a = EMAState.new(20)
        for c in closes[:25]:
            state_a.update(c)
        snapshot = state_a.to_dict()

        state_b = EMAState.from_dict(snapshot)
        restored_results = []
        for c in closes[25:]:
            restored_results.append(state_b.update(c))

        # The last 25 entries of full_results must match restored_results
        assert full_results[25:] == restored_results

    def test_streaming_reset_clears_history(self) -> None:
        state = EMAState.new(5)
        for c in [100.0, 101.0, 102.0]:
            state.update(c)
        state.reset()
        assert state.value is None
        assert state.bars_seen == 0
        # After reset, processing 3 bars again should still be in warm-up
        for c in [100.0, 101.0, 102.0]:
            assert state.update(c) is None


def _ts(day: int, h: int, m: int) -> datetime:
    return datetime(2026, 5, day, h, m, tzinfo=IST)


class TestDayExtremesStreamingMatchesBatch:
    """compute_day_extremes() must match a streaming DayExtremesState
    loop bit-for-bit."""

    def test_streaming_equals_batch_single_day(self) -> None:
        bars = [
            (_ts(13, 9, 15), 5_400_000, 5_395_000),
            (_ts(13, 9, 16), 5_410_000, 5_398_000),
            (_ts(13, 9, 17), 5_405_000, 5_390_000),
            (_ts(13, 9, 18), 5_408_000, 5_392_000),
        ]

        # Batch
        batch = compute_day_extremes(bars)

        # Streaming
        state = DayExtremesState()
        streaming = []
        for ts, h, l in bars:
            dh, dl = state.update(ts, h, l)
            streaming.append((dh, dl))

        for i, (br, (sh, sl)) in enumerate(zip(batch, streaming)):
            assert br.day_high_paisa == sh, f"Drift at bar {i}"
            assert br.day_low_paisa == sl, f"Drift at bar {i}"

    def test_streaming_equals_batch_across_3_days(self) -> None:
        bars = [
            (_ts(13, 9, 15), 1000, 500),
            (_ts(13, 9, 16), 1100, 450),
            (_ts(14, 9, 15), 2000, 1500),  # new day reset
            (_ts(14, 9, 16), 2100, 1450),
            (_ts(15, 9, 15), 3000, 2500),  # new day reset
        ]
        batch = compute_day_extremes(bars)

        state = DayExtremesState()
        for i, (ts, h, l) in enumerate(bars):
            dh, dl = state.update(ts, h, l)
            assert batch[i].day_high_paisa == dh
            assert batch[i].day_low_paisa == dl

    def test_streaming_serialize_restore_across_day_boundary(self) -> None:
        """Checkpoint at end of day 1 → restore → start day 2. Must
        correctly RESET on the new day rather than carry over day 1 extremes."""
        # Day 1
        state_a = DayExtremesState()
        state_a.update(_ts(13, 9, 15), 1000, 500)
        state_a.update(_ts(13, 9, 16), 1100, 450)
        snapshot = state_a.to_dict()

        # Restore + day 2
        state_b = DayExtremesState.from_dict(snapshot)
        dh, dl = state_b.update(_ts(14, 9, 15), 2000, 1500)
        # Day 2 starts fresh: extremes should be (2000, 1500), NOT carryover (1100, 450)
        assert dh == 2000
        assert dl == 1500

    def test_streaming_reset_clears_history(self) -> None:
        state = DayExtremesState()
        state.update(_ts(13, 9, 15), 1000, 500)
        state.reset()
        assert state.current_day is None
        # Next bar starts fresh
        dh, dl = state.update(_ts(14, 9, 15), 2000, 1500)
        assert dh == 2000
        assert dl == 1500


class TestSerializeRoundTrip:
    """Verify state serialization round-trips losslessly."""

    def test_ema_to_dict_from_dict(self) -> None:
        state = EMAState.new(20)
        for c in [100.0 + i for i in range(30)]:
            state.update(c)
        d = state.to_dict()
        restored = EMAState.from_dict(d)
        assert restored.period == state.period
        assert restored.alpha == state.alpha
        assert restored.value == state.value
        assert restored.bars_seen == state.bars_seen
        assert restored.seed_sum == state.seed_sum

    def test_day_extremes_to_dict_from_dict(self) -> None:
        state = DayExtremesState()
        state.update(_ts(13, 9, 15), 1000, 500)
        state.update(_ts(13, 9, 16), 1100, 450)
        d = state.to_dict()
        restored = DayExtremesState.from_dict(d)
        assert restored.current_day == state.current_day
        assert restored.day_high_paisa == state.day_high_paisa
        assert restored.day_low_paisa == state.day_low_paisa

    def test_day_extremes_to_dict_handles_fresh_state(self) -> None:
        """Fresh state has current_day=None — must serialize OK."""
        state = DayExtremesState()
        d = state.to_dict()
        assert d["current_day"] is None
        restored = DayExtremesState.from_dict(d)
        assert restored.current_day is None
