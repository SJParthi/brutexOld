"""Frozen golden oracle for :class:`brutex.execution.driver.BacktestDriver`.

The driver wires the **execution geometry** — symmetric bps slippage (BUY pays
up / SELL gets cut), the fill vs no-fill branch (``BUY``/``SELL`` fill, ``HOLD``
and out-of-scope events do not), the average-cost position lifecycle, the cash /
freeze boundary checks, and the per-bar mark-to-market equity curve. That
geometry is unit-tested (``tests/unit/test_execution_driver_edges.py`` +
``tests/integration/test_execution_driver.py``) but, before this oracle, was NOT
pinned to a frozen golden answer-key the way the 52-condition vocabulary and the
Stage-2 trade-sim walker are. A `<` that should have been `<=` (e.g. the funds
check ``required_cash > cash`` firing one paisa early) would have slipped past
the unit suite without tripping a byte-equality regression fixture (audit Gap
#5).

This module closes the **execution-driver** half of that gap. It runs the REAL
:class:`BacktestDriver` + :class:`~brutex.execution.simulator.ExecutionSimulator`
(no mock arithmetic — the answer-key is generated *from the current driver*) over
a hand-built set of scenarios, serialises every emitted :class:`Fill`,
:class:`~brutex.execution.types.Position`, and
:class:`~brutex.execution.types.EquityPoint` to canonical JSON, and asserts the
bytes match :data:`_FIXTURE_PATH` exactly. Any change to slippage, the cost
seam, position accounting, the cash/freeze boundary, or the equity-curve shape
breaks this test (DEC-EXEC-GOLDEN-PIN-001 / NFR-DET-001 bit-exactness).

Scope honesty (Golden Rule #10 / #1)
------------------------------------
The audit gap names "1-tick SL where ``bar.low == SL`` exactly". The literal
**SL/TP intrabar geometry** lives in :mod:`brutex.engine.trade_sim` (the
signal-only spot walker, DEC-TRADESIM-001) — NOT in the execution driver, which
fills at ``bar.close`` ± slippage and has no SL/TP logic — and is already pinned
by ``tests/golden/test_engine_trade_sim_golden.py``. ``engine/`` is out of scope
here. What this oracle pins is the **driver's own** ``>``-vs-``>=`` boundary
equalities — the cash-exact fill (``required_cash == cash`` *fills*, not
rejects) and the freeze-exact pass (``qty == freeze_quantity`` *passes*) — which
are the direct structural analogues of the "``==`` must not be ``<``" edge, plus
the cash "gap-through" rejection one paisa past the boundary.

Regenerate the fixture (only on an intentional, reviewed behaviour change)::

    python tests/golden/test_execution_driver_golden.py --update
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from brutex.data.models import CandleBar
from brutex.execution.driver import BacktestDriver
from brutex.execution.errors import ExecutionError
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import BacktestResult, InstrumentMeta
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.models import InstrumentType, Segment
from brutex.strategies.protocol import Strategy, StrategyMeta
from brutex.strategies.types import Side, Signal

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "execution_driver_golden_vectors.json"

#: Symbol every scenario trades. A synthetic CASH/EQ row so the real
#: :class:`~brutex.execution.costs.IndianCostModel` exercises its non-trivial
#: branches (sell-side STT, exchange + SEBI turnover, GST) — the index path is
#: all-zero costs and would hide the cost seam in the answer-key.
_SYMBOL = "NSE-GOLDEN-EQ"

#: Fixed slippage every scenario uses, so the golden pins one exact factor.
_SLIPPAGE_BPS = Decimal("5")


def _ts(hh: int, mm: int) -> datetime:
    """Timezone-aware UTC bar timestamp on a fixed reference day."""
    return datetime(2024, 1, 2, hh, mm, tzinfo=UTC)


def _bar(ts: datetime, *, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _meta(*, symbols: tuple[str, ...] = (_SYMBOL,)) -> StrategyMeta:
    return StrategyMeta(
        strategy_id="exec-golden",
        version="1.0",
        symbols=symbols,
        interval="1minute",
        lookback_bars=0,
    )


def _instrument(*, freeze_quantity: int | None = None) -> InstrumentMeta:
    return InstrumentMeta(
        symbol=_SYMBOL,
        segment=Segment.CASH,
        instrument_type=InstrumentType.EQ,
        lot_size=1,
        freeze_quantity=freeze_quantity,
    )


class _ScriptedStrategy:
    """Strategy double: emits a signal keyed on each in-scope event's timestamp.

    Decoupled from call ordering — ``on_event`` looks the event's bar timestamp
    up in *script* and emits a single :class:`Signal` (``signal.timestamp ==
    event.timestamp`` so the look-ahead contract holds) for that side, or
    nothing when the timestamp is absent. ``Side.HOLD`` is a legitimate emission
    that the simulator turns into a no-fill.
    """

    META: StrategyMeta

    def __init__(self, meta: StrategyMeta, script: Mapping[datetime, Side]) -> None:
        self.META = meta
        self._script = dict(script)

    def on_event(self, event: FeedEvent, state: object) -> list[Signal]:
        del state
        side = self._script.get(event.timestamp)
        if side is None:
            return []
        return [Signal(symbol=event.symbol, side=side, timestamp=event.timestamp)]


class _ScriptedFeed:
    """Feed double: a fixed event list, the declared interval, a no-op guard."""

    def __init__(self, events: Sequence[FeedEvent]) -> None:
        self.interval = "1minute"
        self._events = list(events)

    def __iter__(self) -> Any:
        return iter(self._events)

    def ensure_no_lookahead(self, ts: datetime) -> None:
        del ts


def _run(
    *,
    events: Sequence[FeedEvent],
    script: Mapping[datetime, Side],
    initial_cash: Decimal,
    default_qty: int,
    freeze_quantity: int | None = None,
) -> BacktestResult:
    """Drive the REAL :class:`BacktestDriver` over *events* and return its result."""
    simulator = ExecutionSimulator(
        initial_cash=initial_cash,
        instruments={_SYMBOL: _instrument(freeze_quantity=freeze_quantity)},
        slippage_bps=_SLIPPAGE_BPS,
        default_qty=default_qty,
    )
    driver = BacktestDriver(
        strategy=cast(Strategy, _ScriptedStrategy(_meta(), script)),
        feed=cast(ReplayFeed, _ScriptedFeed(events)),
        simulator=simulator,
    )
    return driver.run()


# ── Canonical serialisation ─────────────────────────────────────────────────


def _d(value: Decimal) -> str:
    """Serialise a Decimal as its exact string form (byte-stable)."""
    return str(value)


def _serialise_result(result: BacktestResult) -> OrderedDict[str, Any]:
    """Flatten a :class:`BacktestResult` into a deterministic, JSON-able dict."""
    return OrderedDict(
        (
            (
                "fills",
                [
                    OrderedDict(
                        (
                            ("symbol", f.symbol),
                            ("side", f.side.value),
                            ("qty", f.qty),
                            ("requested_price", _d(f.requested_price)),
                            ("fill_price", _d(f.fill_price)),
                            ("timestamp", f.timestamp.isoformat()),
                            (
                                "costs",
                                OrderedDict(
                                    (
                                        ("brokerage", _d(f.costs.brokerage)),
                                        ("stt", _d(f.costs.stt)),
                                        ("exchange_fee", _d(f.costs.exchange_fee)),
                                        ("sebi_fee", _d(f.costs.sebi_fee)),
                                        ("stamp_duty", _d(f.costs.stamp_duty)),
                                        ("gst", _d(f.costs.gst)),
                                        ("total", _d(f.costs.total)),
                                    )
                                ),
                            ),
                            ("notional", _d(f.notional)),
                            ("cash_delta", _d(f.cash_delta)),
                        )
                    )
                    for f in result.fills
                ],
            ),
            (
                "equity_curve",
                [
                    OrderedDict(
                        (
                            ("timestamp", p.timestamp.isoformat()),
                            ("cash", _d(p.cash)),
                            ("positions_value", _d(p.positions_value)),
                            ("total_equity", _d(p.total_equity)),
                        )
                    )
                    for p in result.equity_curve
                ],
            ),
            (
                "final_positions",
                OrderedDict(
                    (
                        symbol,
                        OrderedDict(
                            (
                                ("qty", pos.qty),
                                ("avg_price", _d(pos.avg_price)),
                                ("realized_pnl", _d(pos.realized_pnl)),
                                ("unrealized_pnl", _d(pos.unrealized_pnl)),
                                ("last_mark_price", _d(pos.last_mark_price)),
                            )
                        ),
                    )
                    for symbol, pos in sorted(result.final_positions.items())
                ),
            ),
            ("final_cash", _d(result.final_cash)),
            ("final_equity", _d(result.final_equity)),
            ("total_realized_pnl", _d(result.total_realized_pnl)),
            ("total_costs", _d(result.total_costs)),
            ("total_fills", result.total_fills),
        )
    )


# ── Scenario generators ─────────────────────────────────────────────────────


def _scenario_buy_fill() -> OrderedDict[str, Any]:
    """FILL branch — a BUY fills at close * (1 + bps): slippage geometry up."""
    t0 = _ts(9, 16)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100"))]
    result = _run(
        events=events,
        script={t0: Side.BUY},
        initial_cash=Decimal("1000000"),
        default_qty=10,
    )
    return OrderedDict(
        (
            ("name", "buy_fill_slippage_up"),
            ("description", "BUY @ close 100 fills at 100.05 (100 * (1 + 5/10000))."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _scenario_sell_fill() -> OrderedDict[str, Any]:
    """FILL branch — a SELL fills at close * (1 - bps) and pays sell-side STT."""
    t0 = _ts(9, 16)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="200", h="200", lo="200", c="200"))]
    result = _run(
        events=events,
        script={t0: Side.SELL},
        initial_cash=Decimal("1000000"),
        default_qty=10,
    )
    return OrderedDict(
        (
            ("name", "sell_fill_slippage_down"),
            ("description", "SELL @ close 200 fills at 199.90 (200 * (1 - 5/10000)); STT on sell."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _scenario_no_fill() -> OrderedDict[str, Any]:
    """NO-FILL branch — a HOLD signal + an out-of-scope event both fill nothing.

    The in-scope HOLD bar still marks-to-market (an equity point with no fill);
    the out-of-scope bar is skipped before ``on_event`` (no point, no fill).
    """
    t0 = _ts(9, 16)
    t1 = _ts(9, 17)
    events = [
        FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100")),
        FeedEvent(symbol="NSE-OUT-OF-SCOPE", bar=_bar(t1, o="100", h="100", lo="100", c="100")),
    ]
    result = _run(
        events=events,
        script={t0: Side.HOLD},
        initial_cash=Decimal("1000000"),
        default_qty=10,
    )
    return OrderedDict(
        (
            ("name", "hold_and_out_of_scope_no_fill"),
            ("description", "HOLD emits no fill (one MTM point); out-of-scope event is skipped."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _scenario_position_lifecycle() -> OrderedDict[str, Any]:
    """Average-cost lifecycle — open, extend, partial-close, full-close, re-open.

    Pins realised P&L accrual, the weighted-average entry, and one
    mark-to-market equity point per bar (the equity-curve geometry).
    """
    t = [_ts(9, 16 + i) for i in range(5)]
    events = [
        FeedEvent(symbol=_SYMBOL, bar=_bar(t[0], o="100", h="100", lo="100", c="100")),
        FeedEvent(symbol=_SYMBOL, bar=_bar(t[1], o="110", h="110", lo="110", c="110")),
        FeedEvent(symbol=_SYMBOL, bar=_bar(t[2], o="120", h="120", lo="120", c="120")),
        FeedEvent(symbol=_SYMBOL, bar=_bar(t[3], o="90", h="90", lo="90", c="90")),
        FeedEvent(symbol=_SYMBOL, bar=_bar(t[4], o="95", h="95", lo="95", c="95")),
    ]
    script = {
        t[0]: Side.BUY,  # open long 10
        t[1]: Side.BUY,  # extend long → 20 (weighted-avg entry)
        t[2]: Side.SELL,  # partial close → 10 (realised P&L)
        t[3]: Side.SELL,  # full close → flat (realised P&L)
        t[4]: Side.SELL,  # re-open short 10
    }
    result = _run(
        events=events,
        script=script,
        initial_cash=Decimal("10000000"),
        default_qty=10,
    )
    return OrderedDict(
        (
            ("name", "position_lifecycle_open_extend_close_reopen"),
            ("description", "Long open→extend→partial-close→full-close then short re-open."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _required_cash_for_buy(*, close: str, qty: int) -> Decimal:
    """Compute the exact ``notional + costs`` a BUY of *qty* @ *close* needs."""
    probe = ExecutionSimulator(
        initial_cash=Decimal("1000000000"),
        instruments={_SYMBOL: _instrument()},
        slippage_bps=_SLIPPAGE_BPS,
        default_qty=qty,
    )
    t0 = _ts(9, 16)
    fill = probe.execute(
        Signal(symbol=_SYMBOL, side=Side.BUY, timestamp=t0),
        _bar(t0, o=close, h=close, lo=close, c=close),
    )
    assert fill is not None
    return fill.notional + fill.costs.total


def _scenario_funds_boundary_exact() -> OrderedDict[str, Any]:
    """Boundary equality — ``required_cash == cash`` FILLS (the `>` not `>=` edge).

    The structural analogue of "``bar.low == SL`` exactly must still trigger":
    funding the account to the exact paisa must let the BUY through (cash → 0),
    because the simulator rejects only on a strict ``required_cash > cash``.
    """
    t0 = _ts(9, 16)
    required = _required_cash_for_buy(close="100", qty=10)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100"))]
    result = _run(
        events=events,
        script={t0: Side.BUY},
        initial_cash=required,  # EXACTLY the required cash
        default_qty=10,
    )
    return OrderedDict(
        (
            ("name", "funds_boundary_exact_fills"),
            ("description", "initial_cash == required_cash: the BUY fills (boundary is `>`)."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _scenario_funds_boundary_gap_through() -> OrderedDict[str, Any]:
    """Boundary — one paisa short of required REJECTS (cash "gap-through")."""
    t0 = _ts(9, 16)
    required = _required_cash_for_buy(close="100", qty=10)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100"))]
    outcome = "ok"
    try:
        _run(
            events=events,
            script={t0: Side.BUY},
            initial_cash=required - Decimal("0.01"),  # one paisa short
            default_qty=10,
        )
    except ExecutionError as exc:
        outcome = type(exc).__name__
    return OrderedDict(
        (
            ("name", "funds_boundary_gap_through_rejects"),
            (
                "description",
                "initial_cash == required − 0.01: BUY rejected (InsufficientFundsError).",
            ),
            ("outcome", outcome),
            ("result", None),
        )
    )


def _scenario_freeze_boundary_exact() -> OrderedDict[str, Any]:
    """Boundary equality — ``qty == freeze_quantity`` PASSES (the `>` not `>=` edge)."""
    t0 = _ts(9, 16)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100"))]
    result = _run(
        events=events,
        script={t0: Side.BUY},
        initial_cash=Decimal("1000000"),
        default_qty=10,
        freeze_quantity=10,  # qty == freeze → passes
    )
    return OrderedDict(
        (
            ("name", "freeze_boundary_exact_passes"),
            ("description", "qty == freeze_quantity (10): the order passes (boundary is `>`)."),
            ("outcome", "ok"),
            ("result", _serialise_result(result)),
        )
    )


def _scenario_freeze_boundary_exceed() -> OrderedDict[str, Any]:
    """Boundary — ``qty == freeze_quantity + 1`` REJECTS (freeze "gap-through")."""
    t0 = _ts(9, 16)
    events = [FeedEvent(symbol=_SYMBOL, bar=_bar(t0, o="100", h="100", lo="100", c="100"))]
    outcome = "ok"
    try:
        _run(
            events=events,
            script={t0: Side.BUY},
            initial_cash=Decimal("1000000"),
            default_qty=10,
            freeze_quantity=9,  # qty (10) > freeze (9) → rejected
        )
    except ExecutionError as exc:
        outcome = type(exc).__name__
    return OrderedDict(
        (
            ("name", "freeze_boundary_exceed_rejects"),
            (
                "description",
                "qty (10) > freeze_quantity (9): rejected (FreezeQuantityExceededError).",
            ),
            ("outcome", outcome),
            ("result", None),
        )
    )


def build_golden_vectors() -> OrderedDict[str, Any]:
    """Generate the full golden answer-key from the CURRENT driver behaviour."""
    return OrderedDict(
        (
            (
                "_meta",
                OrderedDict(
                    (
                        ("decision", "DEC-EXEC-GOLDEN-PIN-001"),
                        ("subject", "brutex.execution.driver.BacktestDriver"),
                        ("slippage_bps", _d(_SLIPPAGE_BPS)),
                        ("symbol", _SYMBOL),
                        (
                            "note",
                            "Generated FROM the real driver/simulator. SL/TP intrabar "
                            "geometry is pinned separately in test_engine_trade_sim_golden.py "
                            "(engine/, out of scope here).",
                        ),
                    )
                ),
            ),
            (
                "scenarios",
                [
                    _scenario_buy_fill(),
                    _scenario_sell_fill(),
                    _scenario_no_fill(),
                    _scenario_position_lifecycle(),
                    _scenario_funds_boundary_exact(),
                    _scenario_funds_boundary_gap_through(),
                    _scenario_freeze_boundary_exact(),
                    _scenario_freeze_boundary_exceed(),
                ],
            ),
        )
    )


def _canonical_json(vectors: Mapping[str, Any]) -> str:
    """Serialise *vectors* to the one canonical byte-form the fixture stores."""
    return json.dumps(vectors, indent=2, ensure_ascii=True) + "\n"


# ── Tests ────────────────────────────────────────────────────────────────────


def test_execution_driver_golden_byte_stable() -> None:
    """The current driver reproduces the frozen answer-key byte-for-byte."""
    generated = _canonical_json(build_golden_vectors())
    expected = _FIXTURE_PATH.read_text(encoding="utf-8")
    assert generated == expected, (
        "execution-driver golden vectors drifted. If this is an INTENTIONAL, "
        "reviewed behaviour change, regenerate with:\n"
        "    python tests/golden/test_execution_driver_golden.py --update"
    )


def test_fixture_is_valid_json_with_all_scenarios() -> None:
    """The committed fixture parses and carries every named scenario branch."""
    loaded = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    names = {s["name"] for s in loaded["scenarios"]}
    assert names == {
        "buy_fill_slippage_up",
        "sell_fill_slippage_down",
        "hold_and_out_of_scope_no_fill",
        "position_lifecycle_open_extend_close_reopen",
        "funds_boundary_exact_fills",
        "funds_boundary_gap_through_rejects",
        "freeze_boundary_exact_passes",
        "freeze_boundary_exceed_rejects",
    }
    # Both boundary-equality scenarios must FILL (the `>` not `>=` edge), and
    # both gap-through scenarios must REJECT with the typed execution error.
    by_name = {s["name"]: s for s in loaded["scenarios"]}
    assert by_name["funds_boundary_exact_fills"]["outcome"] == "ok"
    assert by_name["funds_boundary_exact_fills"]["result"]["total_fills"] == 1
    assert by_name["freeze_boundary_exact_passes"]["outcome"] == "ok"
    assert by_name["freeze_boundary_exact_passes"]["result"]["total_fills"] == 1
    assert by_name["funds_boundary_gap_through_rejects"]["outcome"] == "InsufficientFundsError"
    assert by_name["freeze_boundary_exceed_rejects"]["outcome"] == "FreezeQuantityExceededError"


if __name__ == "__main__":  # pragma: no cover — regeneration entry point
    import sys

    if "--update" in sys.argv[1:]:
        _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE_PATH.write_text(_canonical_json(build_golden_vectors()), encoding="utf-8")
        sys.stdout.write(f"wrote {_FIXTURE_PATH}\n")
    else:
        sys.stdout.write(
            "no-op: pass --update to regenerate the frozen fixture "
            "(only on an intentional, reviewed behaviour change)\n"
        )
