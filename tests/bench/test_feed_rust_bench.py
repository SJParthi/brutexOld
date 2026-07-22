"""Bench teeth for the wheel-REQUIRED feed decision kernel
(DEC-FEED-RUST-MATERIALIZE-002 over DEC-FEED-RUST-CORE-001).

Two layers, honestly tiered (Golden Rule #10 — the scan is O(n_bars) per
LAUNCH; no O(1) claim, no speedup SLA):

1. **Deterministic teeth** (no wall clock): the strict kernel is genuinely
   engaged by ``arrow_table_to_bars`` — pinned by a counting spy around
   ``require_feed_kernel``, not a timer. (The old ``BadTickScreen`` bomb spy
   died with the Python oracle: ``replay`` no longer imports the class at
   all — the screen decision lives inside the Rust kernel.)
2. **Linearity**: per-bar cost at 10x the rows stays within a 1.3x/decade
   envelope — the scan is honestly LINEAR in rows; a quadratic-shaped
   regression (e.g. a per-row re-scan) blows the ratio.

The DEC-FEED-RUST-CORE-001 two-arm wall-clock compares (ON <= 1.15x OFF +
the ``BRUTEX_M4PRO_ABS``-gated not-slower floor) died with the oracle per
DEC-FEED-RUST-MATERIALIZE-002 — there is no OFF arm to compare against, and
no new SLA is invented in their place.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from brutex.feed import replay
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import FEED_RUST_ENV, MATERIALIZE_RUST_ENV

brutex_feed = pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — build via maturin (the wheel is REQUIRED)",
)

_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: Row counts for the linearity decade.
N_SMALL = 1_000
N_BIG = 10_000

#: Best-of-N repetitions per timed arm (min absorbs scheduler noise).
_REPS = 5


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both retired-axis flag env vars so ambient env can't refuse."""
    monkeypatch.delenv(MATERIALIZE_RUST_ENV, raising=False)
    monkeypatch.delenv(FEED_RUST_ENV, raising=False)


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


def _time_launch(table: pa.Table) -> float:
    """Best-of-``_REPS`` wall seconds for one ``arrow_table_to_bars`` launch."""
    arrow_table_to_bars(table, source=None)  # warm-up (imports, caches)
    best = float("inf")
    for _ in range(_REPS):
        t0 = time.perf_counter()
        arrow_table_to_bars(table, source=None)
        best = min(best, time.perf_counter() - t0)
    return best


class TestDeterministicTeeth:
    """No-timer proof that the strict kernel path is real."""

    def test_strict_kernel_engaged_on_a_real_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        class _Spy:
            def __getattr__(self, name: str) -> Any:
                return getattr(brutex_feed, name)

            def scan_bars_strict(self, *args: Any) -> Any:
                calls.append(1)
                return brutex_feed.scan_bars_strict(*args)

        spy = _Spy()
        monkeypatch.setattr(replay, "require_feed_kernel", lambda: spy)
        bars = arrow_table_to_bars(_table(256), source=None)
        assert len(bars) == 256
        assert calls == [1]


class TestLinearity:
    """Per-bar cost stays within 1.3x across a 10x row decade."""

    def test_per_bar_cost_linear_in_rows(self) -> None:
        small = _table(N_SMALL)
        big = _table(N_BIG)
        t_small = _time_launch(small) / N_SMALL
        t_big = _time_launch(big) / N_BIG
        ratio = t_big / max(t_small, 1e-12)
        assert ratio < 1.3, (
            f"per-bar cost ratio {ratio:.2f}x across the {N_SMALL}->{N_BIG} decade "
            f"— the scan may have regressed past linear. "
            f"t_small={t_small * 1e9:.0f}ns/bar, t_big={t_big * 1e9:.0f}ns/bar."
        )
