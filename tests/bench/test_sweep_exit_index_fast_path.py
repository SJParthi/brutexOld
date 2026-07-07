"""Cost gate for the exit-index fast path of the Rust sweep-metrics tail.

DEC-SWEEP-EXIT-INDEX-FASTPATH-001. On the kernel arm, a certified
``exit_indices`` stream replaces the per-mask ``O(N_bars)`` ``exit_ts``→index
dict build with an ``O(trades)`` consistency check — the honest-envelope
residual DEC-SWEEP-RUST-METRICS-001 (#1186) recorded as a named follow-up.
This bench pins that the fast path (a) is result-EQUAL to the dict path (a
diverging fast arm must never pass as "fast") and (b) actually removes the
dict cost: the dict arm's median is at least ``_FAST_PATH_FLOOR``× the fast
arm's on a window where the dict build is a measurable share of the tail.

The ANTI-VACUOUS TWIN then proves the floor's detection property is real,
not prose: it forces the exact regression the gate exists to catch — a
"fast" arm that silently DEMOTES every call back to the dict build (an
inconsistent index stream, refused by ``exit_indices_consistent``) — runs
the SAME harness, and asserts the measured ratio sits BELOW the floor, so a
silent fall-back-to-dict regression genuinely trips the main gate.

HONEST ENVELOPE: sandbox ratio ONLY (not the §8 M4 Pro); a constant-factor
win — the per-tuple tail stays O(kept · N_bars); the floor is set far below
the local measurement so shared-CI noise cannot flake the gate while a
silent fall-back-to-dict regression (the fast path quietly demoting every
mask) still fails loudly — the property the twin pins empirically.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.rust_metrics import (
    SWEEP_RUST_METRICS_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)

_N_BARS = 20_000

#: Conservative shared-CI floor, far below the local measurement (see the
#: module docstring) — the dict arm must cost measurably MORE than the
#: certified fast arm, or the fast path has silently stopped engaging.
_FAST_PATH_FLOOR = 1.15


def _bars() -> list[CandleBar]:
    price = Decimal("100.00")
    return [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=price,
            high=price + Decimal("1.00"),
            low=price - Decimal("1.00"),
            close=price,
            volume=0,
            open_interest=None,
        )
        for i in range(_N_BARS)
    ]


def _trades_with_indices(
    bars: list[CandleBar], n: int = 200
) -> tuple[list[ClosedTrade], list[int]]:
    exit_indices = sorted({k * 97 % _N_BARS for k in range(n)})
    trades = [
        ClosedTrade(
            pnl_paisa=50 if i % 3 else -80,
            mfe_paisa=50,
            mae_paisa=80,
            holding_bars=1,
            entry_ts=bars[idx].timestamp,
            exit_ts=bars[idx].timestamp,
            direction=Side.BUY,
        )
        for i, idx in enumerate(exit_indices)
    ]
    return trades, exit_indices


def _median_seconds(fn: Callable[[], ResultMetrics], rounds: int) -> float:
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    samples.sort()
    return samples[len(samples) // 2]


def test_exit_index_fast_path_beats_the_dict_build(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = _bars()
    trades, exit_indices = _trades_with_indices(bars)
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
    reset_observability_memo_for_tests()
    # Steady state (the bounded burn-in self-check excluded — its oracle scan
    # would dominate both arms identically and mask the dict-build delta).
    reset_self_check_for_tests(remaining=0)

    def _dict_arm() -> ResultMetrics:
        return metrics_from_trades(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)

    def _fast_arm() -> ResultMetrics:
        return metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=10_000_000,
            tf_minutes=1,
            exit_indices=exit_indices,
        )

    dict_result = _dict_arm()  # warm-up + reference
    fast_result = _fast_arm()  # warm-up + equality teeth
    assert fast_result == dict_result  # result-EQUAL first, always
    dict_median = _median_seconds(_dict_arm, rounds=11)
    fast_median = _median_seconds(_fast_arm, rounds=11)
    reset_self_check_for_tests()  # never leak the exhausted budget to siblings
    ratio = dict_median / fast_median
    assert ratio >= _FAST_PATH_FLOOR, (
        f"exit-index fast path ratio {ratio:.2f}× below the {_FAST_PATH_FLOOR}× floor "
        f"(dict arm {dict_median * 1e3:.2f} ms vs fast arm {fast_median * 1e3:.2f} ms) — "
        f"has the fast path silently stopped engaging?"
    )


def test_anti_vacuous_twin_a_demoted_fast_path_trips_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETECTION teeth: a silently-demoting fast path measures BELOW the floor.

    The anti-vacuous twin of the gate above. It manufactures the exact
    regression the ``_FAST_PATH_FLOOR`` exists to catch — a "fast" arm whose
    every call silently rebuilds the ``O(N_bars)`` dict — by supplying an
    OUT-OF-RANGE index stream: ``exit_indices_consistent`` refuses it, so
    ``_exit_pairs`` DEMOTES each call to the ``build_ts_index`` dict path
    (warn-once ``exit_indices_demoted``; results unchanged by construction,
    only cost). The SAME harness then measures the demoted arm against the
    dict arm and asserts the ratio sits BELOW the floor — empirical proof the
    main gate would fail loudly on a silent fall-back-to-dict regression
    (sandbox measured ~0.96× vs the 1.15× floor: two dict builds can never be
    ``_FAST_PATH_FLOOR``× apart; the twin fails only if the demoting stream
    were wrongly ACCEPTED, i.e. the demotion belt itself regressed).
    """
    bars = _bars()
    trades, exit_indices = _trades_with_indices(bars)
    demoting_indices = [idx + _N_BARS for idx in exit_indices]  # all out of range
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
    reset_observability_memo_for_tests()
    reset_self_check_for_tests(remaining=0)  # steady state, as in the gate above

    def _dict_arm() -> ResultMetrics:
        return metrics_from_trades(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)

    def _demoted_arm() -> ResultMetrics:
        return metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=10_000_000,
            tf_minutes=1,
            exit_indices=demoting_indices,
        )

    dict_result = _dict_arm()  # warm-up + reference
    with capture_logs() as logs:
        demoted_result = _demoted_arm()  # warm-up + demotion-engaged teeth
    assert demoted_result == dict_result  # a demotion never changes results
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert [e["reason"] for e in events] == ["exit_indices_demoted"]
    dict_median = _median_seconds(_dict_arm, rounds=11)
    demoted_median = _median_seconds(_demoted_arm, rounds=11)
    reset_self_check_for_tests()  # never leak the exhausted budget to siblings
    ratio = dict_median / demoted_median
    assert ratio < _FAST_PATH_FLOOR, (
        f"a fully-DEMOTED fast path measured {ratio:.2f}×, at/above the "
        f"{_FAST_PATH_FLOOR}× floor (dict arm {dict_median * 1e3:.2f} ms vs demoted arm "
        f"{demoted_median * 1e3:.2f} ms) — the floor gate would NOT detect a silent "
        f"fall-back-to-dict regression, or the inconsistent stream was wrongly accepted"
    )
