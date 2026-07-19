"""Bench teeth for the DEC-FEED-RUST-CORE-001 feed decision kernel.

Three layers, honestly tiered (Golden Rule #10 — the scan is O(n_bars) per
LAUNCH; no O(1) claim, no speedup SLA):

1. **Deterministic teeth** (no wall clock): the ON arm genuinely engages the
   kernel, and ``BadTickScreen`` is NEVER constructed on a served ON arm —
   the Python state machine is provably out of the loop, pinned by a bomb
   spy, not a timer.
2. **Linearity** (both arms): per-bar cost at 10x the rows stays within a
   1.3x/decade envelope — the scan is honestly LINEAR in rows; a
   quadratic-shaped regression (e.g. a per-row re-scan) blows the ratio.
3. **Wall-clock**: shared-CI asserts NON-REGRESSION only (ON <= 1.15x OFF,
   best-of-N mins); the strictly-faster floor is gated behind
   ``BRUTEX_M4PRO_ABS=1`` (the quiet self-hosted tier) so shared-runner
   noise can never flake the gate. Measured numbers are REPORTED for the PR
   body, never claimed as an SLA.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from brutex import feed_rust
from brutex.feed import replay
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import FEED_RUST_ENV, reset_for_tests

pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — build via maturin (bench needs the ON arm)",
)

_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: Row counts for the linearity decade.
N_SMALL = 1_000
N_BIG = 10_000

#: Best-of-N repetitions per timed arm (min absorbs scheduler noise).
_REPS = 5


def _table(n: int) -> pa.Table:
    """A clean n-row single-session-per-375-bars table (RFC session shape)."""
    minutes = [_BASE + timedelta(days=i // 375, minutes=i % 375) for i in range(n)]
    opens = [27_940.0 + (i % 50) * 0.05 for i in range(n)]
    highs = [o + 10.0 for o in opens]
    lows = [o - 10.0 for o in opens]
    closes = [o + 2.5 for o in opens]
    return pa.table(
        {
            "timestamp": pa.array(minutes, type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(opens, type=pa.float64()),
            "high": pa.array(highs, type=pa.float64()),
            "low": pa.array(lows, type=pa.float64()),
            "close": pa.array(closes, type=pa.float64()),
            "volume": pa.array([10] * n, type=pa.int64()),
            "open_interest": pa.array([None] * n, type=pa.int64()),
        }
    )


def _time_arm(table: pa.Table, flag: str | None, monkeypatch: pytest.MonkeyPatch) -> float:
    """Best-of-``_REPS`` wall seconds for one ``arrow_table_to_bars`` launch."""
    if flag is None:
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
    else:
        monkeypatch.setenv(FEED_RUST_ENV, flag)
    reset_for_tests()
    arrow_table_to_bars(table, source=None)  # warm-up (imports, caches)
    best = float("inf")
    for _ in range(_REPS):
        t0 = time.perf_counter()
        arrow_table_to_bars(table, source=None)
        best = min(best, time.perf_counter() - t0)
    return best


class TestDeterministicTeeth:
    """No-timer proofs that the ON arm is real."""

    def test_kernel_engaged_and_screen_never_constructed_on_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = feed_rust.resolve_feed_kernel()
        assert real is not None
        calls: list[int] = []

        def _spy(*args: Any) -> Any:
            calls.append(1)
            return real(*args)

        class _BombScreen:
            def __init__(self, **_kw: Any) -> None:
                raise AssertionError("BadTickScreen constructed on a SERVED kernel arm")

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _spy)
        monkeypatch.setattr(replay, "BadTickScreen", _BombScreen)
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        reset_for_tests()
        bars = arrow_table_to_bars(_table(256), source=None)
        assert len(bars) == 256
        assert calls == [1]


class TestLinearity:
    """Per-bar cost stays within 1.3x across a 10x row decade, both arms."""

    @pytest.mark.parametrize("flag", [None, "1"], ids=["oracle", "kernel"])
    def test_per_bar_cost_linear_in_rows(
        self, flag: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        small = _table(N_SMALL)
        big = _table(N_BIG)
        t_small = _time_arm(small, flag, monkeypatch) / N_SMALL
        t_big = _time_arm(big, flag, monkeypatch) / N_BIG
        ratio = t_big / max(t_small, 1e-12)
        assert ratio < 1.3, (
            f"per-bar cost ratio {ratio:.2f}x across the {N_SMALL}->{N_BIG} decade "
            f"(flag={flag!r}) — the scan may have regressed past linear. "
            f"t_small={t_small * 1e9:.0f}ns/bar, t_big={t_big * 1e9:.0f}ns/bar."
        )


class TestWallClock:
    """Non-regression on shared CI; the speedup floor only on the quiet tier."""

    def test_on_arm_never_regresses_whole_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = _table(N_BIG)
        t_off = _time_arm(table, None, monkeypatch)
        t_on = _time_arm(table, "1", monkeypatch)
        ratio = t_on / max(t_off, 1e-12)
        # REPORT the measurement (per-LAUNCH framing — the PR body cites it).
        print(
            f"\nfeed_rust bench (n={N_BIG}): off={t_off * 1e3:.2f}ms "
            f"on={t_on * 1e3:.2f}ms whole-call speedup={t_off / max(t_on, 1e-12):.2f}x"
        )
        assert ratio <= 1.15, (
            f"the ON arm is {ratio:.2f}x the OFF arm — the kernel path must "
            f"never make a launch slower (off={t_off * 1e3:.2f}ms, on={t_on * 1e3:.2f}ms)"
        )

    @pytest.mark.skipif(
        os.environ.get("BRUTEX_M4PRO_ABS", "") != "1",
        reason="absolute speedup floor gated to the quiet M4-Pro tier (BRUTEX_M4PRO_ABS=1)",
    )
    def test_on_arm_speedup_floor_m4pro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # HONEST floor (Golden Rule #10): the 2026-07-11 sandbox measured a
        # 1.06x whole-call speedup (materialization — Decimal/CandleBar —
        # dominates BY DESIGN; only the decision plane moved to Rust), so the
        # quiet-tier floor asserts NOT-SLOWER (>= 1.0), never a fabricated
        # speedup SLA. Raising the floor to a real measured margin is the
        # flip DEC's (`DEC-FEED-RUST-FLIP-001`) business once genuine M4-Pro
        # numbers exist.
        table = _table(N_BIG)
        t_off = _time_arm(table, None, monkeypatch)
        t_on = _time_arm(table, "1", monkeypatch)
        speedup = t_off / max(t_on, 1e-12)
        assert speedup >= 1.0, (
            f"whole-call speedup {speedup:.2f}x < 1.0x on the quiet tier "
            f"(off={t_off * 1e3:.2f}ms, on={t_on * 1e3:.2f}ms)"
        )
