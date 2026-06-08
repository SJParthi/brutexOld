"""O(1) benchmarks for the unified per-window ledger (DEC-WINDOW-LEDGER-001).

The skip probe (`is_skippable`) and the outcome write (`record`) are on the
ingest hot path — one per window across a multi-thousand-window pull — so they
MUST stay O(1) in the number of recorded windows (CLAUDE.md §2 Rule 5). A
regression to a scan would blow the 10k-vs-1k latency ratio and fail this gate.

Mirrors `tests/bench/test_lmdb_membership_o1.py`: empirical ratio with CI-jitter
headroom (`< 5×` on shared runners, 1.1× on the M4-Pro strict tier).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 1_000
_LARGE = 10_000
_NOW = datetime(2026, 6, 8, tzinfo=UTC)


def _key(i: int) -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=f"SYM{i}",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _filled(n: int) -> WindowLedger:
    ledger = WindowLedger()
    for i in range(n):
        ledger.record(_key(i), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
    return ledger


class TestIsSkippableIsO1:
    def test_probe_at_small_scale(self, benchmark) -> None:
        ledger = _filled(_SMALL)
        probe = _key(_SMALL // 2)
        assert benchmark(lambda: ledger.is_skippable(probe)) is True

    def test_probe_at_large_scale(self, benchmark) -> None:
        ledger = _filled(_LARGE)
        probe = _key(_LARGE // 2)
        assert benchmark(lambda: ledger.is_skippable(probe)) is True

    def test_probe_ratio_is_o1(self) -> None:
        """`is_skippable` latency at 10k / 1k windows must stay O(1) (< 5×)."""

        def _time(n: int, *, iterations: int = 20_000) -> float:
            ledger = _filled(n)
            probe = _key(n // 2)
            for _ in range(100):  # warm
                _ = ledger.is_skippable(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                _ = ledger.is_skippable(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"WindowLedger.is_skippable ratio {ratio:.2f}× (10k vs 1k) exceeds 5× — "
            f"the skip probe may have regressed to a scan. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )


class TestRecordIsO1:
    def test_record_ratio_is_o1(self) -> None:
        """`record` (re-stamping one existing window) must stay O(1) (< 5×)."""

        def _time(n: int, *, iterations: int = 20_000) -> float:
            ledger = _filled(n)
            probe = _key(n // 2)
            for _ in range(100):  # warm
                ledger.record(probe, raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                ledger.record(probe, raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"WindowLedger.record ratio {ratio:.2f}× (10k vs 1k) exceeds 5× — "
            f"the outcome write may have regressed. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )
