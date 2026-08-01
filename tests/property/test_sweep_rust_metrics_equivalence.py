"""Cross-arm bit-equality property gate for the Rust sweep-metrics tail.

DEC-SWEEP-RUST-METRICS-001. The frozen golden fixture cannot exercise the
numerical hazards (near-2⁵²-boundary magnitudes, >100% / ruin drawdowns,
multi-exit bars), so this hypothesis suite drives ADVERSARIAL trade sets
through :func:`brutex.engine.sweep_driver.metrics_from_trades` with the flag
OFF and ON and asserts the full :class:`ResultMetrics` row is equal — with
BIT-level float comparison (``struct.pack``), never ``approx`` (statistical
metrics keep full precision, CLAUDE.md §4). A second property pins the seam
itself: :func:`drawdown_stats_via_kernel` == ``_drawdown_stats`` over the
materialised curve, bit-for-bit, across random exit scatters.
"""

from __future__ import annotations

import os
import struct
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.config.rust_strict import RUST_STRICT_ENV
from brutex.data.models import CandleBar
from brutex.engine.rust_metrics import (
    MAX_EXACT_EQUITY_PAISA,
    SWEEP_RUST_METRICS_ENV,
    drawdown_stats_via_kernel,
    exceeds_exact_equity_envelope,
)
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.compute import (
    ResultMetrics,
    _drawdown_stats,  # pyright: ignore[reportPrivateUsage] — oracle seam pin
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st


@pytest.fixture
def _strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable strict mode for the boundary case that PROVES the envelope
    guard degrades (DEC-RUST-STRICT-NO-FALLBACK-001).

    One paisa past 2⁵² is a Python fall-back, which strict mode refuses. The
    degrade contract is still a supported, kill-switched mode and this test
    pins it, so it opts in by name rather than weakening its assertion.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)  # 09:16 IST

_N_BARS = 40

#: A per-trade magnitude that keeps 32 trades + the base inside the 2⁵²
#: envelope while reaching >100%-of-peak (ruin) drawdowns against the base.
_BIG_PNL = MAX_EXACT_EQUITY_PAISA // 64


def _bars(n: int = _N_BARS) -> list[CandleBar]:
    price = Decimal("100.00")
    return [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=price,
            high=price + Decimal("1.00"),
            low=price - Decimal("1.00"),
            close=price,
            volume=10,
            open_interest=None,
        )
        for i in range(n)
    ]


_BARS = _bars()


def _trades_from(raw: list[tuple[int, int]]) -> list[ClosedTrade]:
    """Map ``(exit_idx, pnl)`` pairs to timestamps-only ClosedTrades.

    The single-position walker emits exit-ordered trades; sort by exit index
    so the fixture satisfies the production stream shape (int scatter order
    is value-irrelevant either way).
    """
    out: list[ClosedTrade] = []
    for exit_idx, pnl in sorted(raw, key=lambda pair: pair[0]):
        out.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=max(pnl, 0),
                mae_paisa=max(-pnl, 0),
                holding_bars=1,
                entry_ts=_BARS[exit_idx].timestamp,
                exit_ts=_BARS[exit_idx].timestamp,
                direction=Side.BUY,
            )
        )
    return out


def _assert_bit_identical(a: ResultMetrics, b: ResultMetrics) -> None:
    for f in fields(a):
        x = getattr(a, f.name)
        y = getattr(b, f.name)
        if isinstance(x, float) and isinstance(y, float):
            assert struct.pack("<d", x) == struct.pack("<d", y), (f.name, x, y)
        else:
            assert x == y, (f.name, x, y)


_EXITS = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=_N_BARS - 1),
        st.integers(min_value=-_BIG_PNL, max_value=_BIG_PNL),
    ),
    min_size=0,
    max_size=32,
)


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=60)
@given(exits=_EXITS, start_equity=st.integers(min_value=1, max_value=10_000_000))
# Exit on the first and last bar; two exits on ONE bar; ruin (equity < 0).
@example(exits=[(0, -10)], start_equity=1)
@example(exits=[(0, -5), (0, -5), (_N_BARS - 1, 3)], start_equity=7)
@example(exits=[(3, -_BIG_PNL)], start_equity=1_000)  # deep negative equity
@example(exits=[(2, _BIG_PNL), (5, -_BIG_PNL), (5, -_BIG_PNL)], start_equity=1_000)
def test_metrics_from_trades_flag_arms_are_bit_identical(
    exits: list[tuple[int, int]], start_equity: int
) -> None:
    """OFF vs ON: the FULL metrics row is bit-identical over adversarial trades.

    The OFF arm is the EXPLICIT "0" kill switch (not unset): under the
    ADR-0017 default-ON constant an unset flag IS the kernel arm, so only
    "0" pins the pure-Python oracle side of the equivalence.
    """
    trades = _trades_from(exits)
    prior = os.environ.pop(SWEEP_RUST_METRICS_ENV, None)
    try:
        os.environ[SWEEP_RUST_METRICS_ENV] = "0"
        off = metrics_from_trades(
            _BARS, trades, start_equity_paisa=start_equity, tf_minutes=1, exec_bars_1m=_BARS
        )
        os.environ[SWEEP_RUST_METRICS_ENV] = "1"
        on = metrics_from_trades(
            _BARS, trades, start_equity_paisa=start_equity, tf_minutes=1, exec_bars_1m=_BARS
        )
    finally:
        if prior is None:
            os.environ.pop(SWEEP_RUST_METRICS_ENV, None)
        else:
            os.environ[SWEEP_RUST_METRICS_ENV] = prior
    _assert_bit_identical(off, on)


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=60)
@given(
    exits=_EXITS,
    start_equity=st.integers(min_value=1, max_value=10_000_000),
    n_bars=st.integers(min_value=1, max_value=_N_BARS),
)
@example(exits=[(0, MAX_EXACT_EQUITY_PAISA // 64)], start_equity=1, n_bars=8)
def test_kernel_seam_matches_drawdown_stats_bitwise(
    exits: list[tuple[int, int]], start_equity: int, n_bars: int
) -> None:
    """drawdown_stats_via_kernel == _drawdown_stats on the materialised curve."""
    in_range = [(idx % n_bars, pnl) for idx, pnl in exits]
    got = drawdown_stats_via_kernel(n_bars, start_equity, in_range)
    assert got is not None
    realized = [0] * n_bars
    for idx, pnl in in_range:
        realized[idx] += pnl
    equity: list[int] = []
    running = start_equity
    for delta in realized:
        running += delta
        equity.append(running)
    expected = _drawdown_stats(
        EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=start_equity, span_years=1.0)
    )
    assert got == expected
    assert struct.pack("<d", got[1]) == struct.pack("<d", expected[1])
    assert struct.pack("<d", got[3]) == struct.pack("<d", expected[3])


@pytest.mark.usefixtures("_strict_off")
def test_near_envelope_boundary_stays_bit_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LAST admissible magnitude (start + Σ|pnl| == 2⁵²) still runs the
    kernel and matches Python bit-for-bit; ONE extra paisa (2⁵² + 1) exceeds
    the envelope so the ON arm falls back to the oracle (also bit-equal).
    Each side of the boundary is asserted against the envelope predicate so
    the fixture can never silently drift onto one arm only."""
    start = 1_000
    at_pnl = MAX_EXACT_EQUITY_PAISA - start  # even ⇒ Σ|pnl| == at_pnl exactly
    at_bound = [(1, -at_pnl // 2), (2, at_pnl // 2)]  # start + Σ|pnl| == 2⁵²
    over_bound = [*at_bound, (3, 1)]  # start + Σ|pnl| == 2⁵² + 1 → fallback
    for raw, exceeds in ((at_bound, False), (over_bound, True)):
        trades = _trades_from(raw)
        assert exceeds_exact_equity_envelope(start, trades) is exceeds
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            _BARS, trades, start_equity_paisa=start, tf_minutes=1, exec_bars_1m=_BARS
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            _BARS, trades, start_equity_paisa=start, tf_minutes=1, exec_bars_1m=_BARS
        )
        _assert_bit_identical(off, on)
