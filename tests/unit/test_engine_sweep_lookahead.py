"""Unit tests for the opt-in sweep look-ahead barrier (`brutex.engine.sweep_lookahead`).

DEC-SWEEP-LOOKAHEAD-GUARD-001. Covers, at 100% line + branch:

* the env-flag parser (truthy literals only; everything else fail-safe OFF),
* the zero-cost OFF contract of :func:`maybe_guard_bars` — the IDENTICAL
  object is returned (``result is bars``), which is the structural proof
  that the OFF hot path is byte-identical and cost-unchanged,
* :class:`GuardedBarWindow` — sequential consumption advances the front;
  indexed / sliced / negative / reversed reads ahead of the front raise
  :class:`~brutex.feed.errors.LookAheadError`; reads at/behind the front
  pass; re-iteration after a full pass is permitted (a past re-read).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_lookahead import (
    SWEEP_LOOKAHEAD_GUARD_ENV,
    GuardedBarWindow,
    env_flag_tristate,
    maybe_guard_bars,
    sweep_lookahead_guard_enabled,
)
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadBarrier

# 09:15:00 IST == 03:45 UTC (session-open anchor).
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)


def _bar(minute: int, price: int) -> CandleBar:
    """A minimal valid 1-minute bar at *minute* offsets from session open."""
    p = Decimal(price)
    return CandleBar(
        timestamp=_OPEN_UTC + timedelta(minutes=minute),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=0,
        open_interest=None,
    )


def _bars(n: int) -> list[CandleBar]:
    return [_bar(i, 100 + i) for i in range(n)]


# ────── env-flag parsing ──────


class TestGuardEnabledFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, value)
        assert sweep_lookahead_guard_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "2", "garbage"])
    def test_everything_else_is_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        # Fail-safe default: an unrecognised value must never silently enable
        # the instrumented mode.
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, value)
        assert sweep_lookahead_guard_enabled() is False

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        assert sweep_lookahead_guard_enabled() is False


class TestEnvFlagTristate:
    """The COMMON vocabulary classifier every sweep env flag rides on."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
    def test_truthy_literals_are_true(self, value: str) -> None:
        assert env_flag_tristate(value) is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "Off"])
    def test_falsy_literals_are_false(self, value: str) -> None:
        # The explicit kill-switch vocabulary — the default-ON sibling flag
        # (brutex.engine.rust_metrics) relies on this arm to honour an
        # operator OFF regardless of its default.
        assert env_flag_tristate(value) is False

    @pytest.mark.parametrize("value", ["", "   ", "2", "garbage", "flase", "0ff"])
    def test_everything_else_is_none(self, value: str) -> None:
        # Unset/empty AND unrecognised both classify None — the CALLER owns
        # the default + warn policy for those (fail-safe by construction).
        assert env_flag_tristate(value) is None


# ────── maybe_guard_bars — the ONE wiring seam ──────


class TestMaybeGuardBars:
    def test_off_returns_the_identical_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # THE zero-cost OFF contract: the caller's object itself comes back,
        # so no wrapper — and no per-bar check — can exist downstream.
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        bars = _bars(3)
        assert maybe_guard_bars(bars) is bars

    def test_env_on_wraps_in_guarded_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, "1")
        bars = _bars(3)
        guarded = maybe_guard_bars(bars)
        assert isinstance(guarded, GuardedBarWindow)
        assert len(guarded) == 3

    def test_explicit_enabled_override_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, "1")
        bars = _bars(2)
        assert maybe_guard_bars(bars, enabled=False) is bars
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        assert isinstance(maybe_guard_bars(bars, enabled=True), GuardedBarWindow)


# ────── GuardedBarWindow semantics ──────


class TestGuardedBarWindow:
    def test_sequential_iteration_yields_all_bars_and_advances_front(self) -> None:
        bars = _bars(5)
        window = GuardedBarWindow(bars)
        assert list(window) == bars
        assert window.barrier.current_index == 4

    def test_indexed_read_before_any_consumption_raises(self) -> None:
        window = GuardedBarWindow(_bars(3))
        with pytest.raises(LookAheadError):
            _ = window[0]

    def test_indexed_read_ahead_of_front_raises(self) -> None:
        bars = _bars(5)
        window = GuardedBarWindow(bars)
        it = iter(window)
        next(it)  # front at 0
        next(it)  # front at 1
        with pytest.raises(LookAheadError, match="look-ahead"):
            _ = window[2]

    def test_indexed_read_at_or_behind_front_passes(self) -> None:
        bars = _bars(5)
        window = GuardedBarWindow(bars)
        it = iter(window)
        next(it)
        next(it)
        next(it)  # front at 2
        assert window[2] is bars[2]
        assert window[0] is bars[0]

    def test_negative_index_is_a_read_of_the_last_bar(self) -> None:
        bars = _bars(4)
        window = GuardedBarWindow(bars)
        it = iter(window)
        next(it)  # front at 0
        with pytest.raises(LookAheadError):
            _ = window[-1]
        for _ in it:  # consume the rest — front reaches the end
            pass
        assert window[-1] is bars[-1]

    def test_out_of_range_index_raises_index_error(self) -> None:
        window = GuardedBarWindow(_bars(3))
        list(window)  # consume fully first — the bounds check is separate
        with pytest.raises(IndexError):
            _ = window[3]
        with pytest.raises(IndexError):
            _ = window[-4]

    def test_slice_ahead_of_front_raises(self) -> None:
        window = GuardedBarWindow(_bars(6))
        it = iter(window)
        next(it)
        next(it)  # front at 1
        with pytest.raises(LookAheadError):
            _ = window[0:4]

    def test_slice_behind_front_passes_and_matches_underlying(self) -> None:
        bars = _bars(6)
        window = GuardedBarWindow(bars)
        it = iter(window)
        for _ in range(4):
            next(it)  # front at 3
        assert list(window[0:4]) == bars[0:4]
        assert list(window[1:3]) == bars[1:3]

    def test_empty_slice_touches_nothing(self) -> None:
        # An empty slice reads no index, so it is legal even pre-consumption.
        bars = _bars(3)
        window = GuardedBarWindow(bars)
        assert list(window[2:2]) == []

    def test_reiteration_after_full_pass_is_a_permitted_past_read(self) -> None:
        bars = _bars(4)
        window = GuardedBarWindow(bars)
        assert list(window) == bars
        # Second full walk: every index is at/behind the front — the `access`
        # arm of __iter__ (a past re-read), never a backwards cursor move.
        assert list(window) == bars
        assert window.barrier.current_index == 3

    def test_reversed_iteration_before_consumption_raises(self) -> None:
        # Sequence gives reversed() for free via __getitem__ from the end —
        # a future-first walk, rejected until the front reaches the end.
        window = GuardedBarWindow(_bars(3))
        with pytest.raises(LookAheadError):
            _ = list(reversed(window))

    def test_injected_recording_barrier_traces_touched_indices(self) -> None:
        bars = _bars(3)
        barrier = LookAheadBarrier(record=True)
        window = GuardedBarWindow(bars, barrier=barrier)
        list(window)  # first pass: pure advance, no access recorded
        _ = window[1]
        assert window.barrier is barrier
        assert barrier.touched_indices == (1,)

    def test_len_matches_underlying(self) -> None:
        assert len(GuardedBarWindow(_bars(7))) == 7
        assert len(GuardedBarWindow([])) == 0

    def test_empty_window_iterates_to_nothing(self) -> None:
        window = GuardedBarWindow([])
        assert list(window) == []
        assert window.barrier.current_index == -1
