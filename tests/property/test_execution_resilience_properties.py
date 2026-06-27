"""Property tests for the execution-driver resilience guards.

Covers two of the FIVE hardening fixes in
DEC-EXECUTION-REPRO-RESILIENCE-001 as hypothesis invariants:

* **Monotonic-bar guard (FIX 2)** — for ANY strictly-increasing
  per-symbol synthetic feed the
  :class:`~brutex.execution.driver.BacktestDriver` never raises
  :class:`~brutex.execution.errors.NonMonotonicBarError`; injecting a
  backwards (or duplicate) step ALWAYS raises.
* **Cost rounding (FIX 5)** — the
  :func:`brutex.execution.costs._quantise` paise rounding is invariant
  to the ambient process-global ``decimal.getcontext().rounding`` mode.

These guards are pure (no Rust wheel, no lake), so this file runs on a
bare checkout.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import decimal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import cast

import pytest
from brutex.data.models import CandleBar
from brutex.execution.costs import IndianCostModel, _quantise
from brutex.execution.driver import BacktestDriver
from brutex.execution.errors import NonMonotonicBarError
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import Fill
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.models import InstrumentType, Segment
from brutex.strategies.protocol import Strategy, StrategyMeta
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_SYMBOL = "NSE-NIFTY"
_EPOCH = datetime(2024, 1, 1, 9, 15, 0, tzinfo=UTC)


# ── Lightweight fakes (no lake) ─────────────────────────────────────────────


def _bar(close: float, *, ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=100,
        open_interest=0,
    )


def _meta() -> StrategyMeta:
    return StrategyMeta(
        strategy_id="prop",
        version="1.0",
        symbols=(_SYMBOL,),
        interval="1minute",
        lookback_bars=0,
    )


class _FakeStrategy:
    def __init__(self, meta: StrategyMeta) -> None:
        self.META = meta

    def on_event(self, event: object, state: object) -> list[object]:
        """No-op strategy double; emits no signals for the property suite."""
        return []


class _FakeFeed:
    def __init__(self, events: Sequence[FeedEvent]) -> None:
        self.interval = "1minute"
        self._events = list(events)

    def __iter__(self) -> object:
        return iter(self._events)

    def ensure_no_lookahead(self, ts: datetime) -> None:
        """No-op look-ahead guard; the property suite never trips it."""


class _FakeSimulator:
    def __init__(self) -> None:
        self.fills: list[Fill] = []
        self.equity_curve: list[object] = []
        self.positions: dict[str, object] = {}
        self.cash = Decimal("0")

    def reset(self) -> None:
        pass

    def execute(self, signal: object, bar: object) -> Fill | None:
        """No-op execution double; never produces a fill for the property suite."""
        return None

    def mark_to_market(self, *, symbol: str, price: object, timestamp: object) -> None:
        """No-op mark-to-market double; records nothing for the property suite."""


def _driver(events: Sequence[FeedEvent]) -> BacktestDriver:
    return BacktestDriver(
        strategy=cast(Strategy, _FakeStrategy(_meta())),
        feed=cast(ReplayFeed, _FakeFeed(events)),
        simulator=cast(ExecutionSimulator, _FakeSimulator()),
    )


# ── FIX 2 — monotonic-bar guard invariants ──────────────────────────────────


@given(gaps=st.lists(st.integers(min_value=1, max_value=10_000), min_size=0, max_size=30))
def test_strictly_increasing_feed_never_raises(gaps: list[int]) -> None:
    """Any strictly-increasing per-symbol feed passes the guard."""
    ts = _EPOCH
    events: list[FeedEvent] = [FeedEvent(symbol=_SYMBOL, bar=_bar(100, ts=ts))]
    for gap in gaps:
        ts = ts + timedelta(seconds=gap)  # gap >= 1 → strictly increasing
        events.append(FeedEvent(symbol=_SYMBOL, bar=_bar(100, ts=ts)))
    # Must not raise.
    _driver(events).run()


@given(
    prefix=st.lists(st.integers(min_value=1, max_value=1_000), min_size=0, max_size=10),
    back=st.integers(min_value=0, max_value=1_000),
)
def test_backwards_or_duplicate_step_always_raises(prefix: list[int], back: int) -> None:
    """A backwards (back > 0) or duplicate (back == 0) step always raises."""
    ts = _EPOCH
    events: list[FeedEvent] = [FeedEvent(symbol=_SYMBOL, bar=_bar(100, ts=ts))]
    for gap in prefix:
        ts = ts + timedelta(seconds=gap)
        events.append(FeedEvent(symbol=_SYMBOL, bar=_bar(100, ts=ts)))
    # Inject a non-increasing step: ts - back is <= the last ts.
    bad_ts = ts - timedelta(seconds=back)
    events.append(FeedEvent(symbol=_SYMBOL, bar=_bar(100, ts=bad_ts)))
    with pytest.raises(NonMonotonicBarError):
        _driver(events).run()


# ── FIX 5 — cost rounding is ambient-context invariant ──────────────────────

_MODES = [ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_DOWN]


@given(
    paise=st.integers(min_value=0, max_value=2_000_000),
    half_step=st.booleans(),
    mode=st.sampled_from(_MODES),
)
def test_quantise_invariant_to_ambient_rounding(paise: int, half_step: bool, mode: str) -> None:
    """_quantise is byte-identical regardless of the ambient context mode."""
    # Build a value that may sit exactly on a half-paise tie (the only place
    # the rounding mode could ever differ).
    amount = Decimal(paise) / Decimal("100")
    if half_step:
        amount = amount + Decimal("0.005")

    baseline = _quantise(amount)

    original = decimal.getcontext().rounding
    try:
        decimal.getcontext().rounding = mode
        under_mode = _quantise(amount)
    finally:
        decimal.getcontext().rounding = original

    assert under_mode == baseline


@given(
    qty=st.integers(min_value=1, max_value=10_000),
    price_paise=st.integers(min_value=1, max_value=10_000_000),
    mode=st.sampled_from(_MODES),
)
def test_compute_costs_invariant_to_ambient_rounding(qty: int, price_paise: int, mode: str) -> None:
    """A full FillCosts is byte-identical across ambient rounding modes."""
    model = IndianCostModel()
    fill_price = Decimal(price_paise) / Decimal("100")
    baseline = model.compute(
        side=Side.SELL,
        qty=qty,
        fill_price=fill_price,
        segment=Segment.CASH,
        instrument_type=InstrumentType.EQ,
    )

    original = decimal.getcontext().rounding
    try:
        decimal.getcontext().rounding = mode
        under_mode = model.compute(
            side=Side.SELL,
            qty=qty,
            fill_price=fill_price,
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
    finally:
        decimal.getcontext().rounding = original

    assert under_mode == baseline
