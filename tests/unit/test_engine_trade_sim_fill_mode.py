"""The worst-case adverse-max fill LAW on the 1m grid — walker + identity units.

DEC-FILL-WORST-CASE-ONLY-001 + DEC-FILL-EXEC-GRID-1M-001: worst-case is the
ONLY fill law, resolved per 1-MINUTE bar (the configurable ``FillPolicy`` /
``LevelFill`` / ``SameBarTie`` machinery of the absorbed #1522 design stays
DELETED). The named regressions (gap-through-level clamp both directions —
the B5 gap-open law; entry at the adverse extreme), every fill arm (entry /
at-level / Mode-B signal exit / forced / end_of_data / session_gap ×
BUY/SELL), the killed-token pins on the retired knobs, the dual-feed
``exec_bars_1m``-REQUIRED pin (design §3.3), and the run-identity
UNCONDITIONAL-emit law. The resident-kernel seam's stale-wheel demotion
lives in the stem-matched sibling ``test_engine_rust_trade_sim_fill_mode.py``
(the discovery gate's ``rust_trade_sim`` pairing — DEC-CICD-GATE-HONESTY-001),
which imports this module's ``_bar`` helper (Golden Rule #11 COMMON).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from brutex.data.models import CandleBar
from brutex.engine.sweep_run import canonical_trade_params
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.strategies.types import Side


def _bar(
    minute: int,
    o: float,
    h: float,
    lo: float,
    c: float,
    *,
    day: int = 0,
) -> CandleBar:
    # 04:00 UTC == 09:30 IST — inside the regular session, far from the
    # forced-exit boundary; ``day`` steps whole IST session dates.
    ts = datetime(2024, 1, 2, 4, 0, tzinfo=UTC) + timedelta(days=day, minutes=minute)
    dec = lambda v: Decimal(str(v))  # noqa: E731 — local literal shorthand
    return CandleBar(
        timestamp=ts,
        open=dec(o),
        high=dec(h),
        low=dec(lo),
        close=dec(c),
        volume=0,
        open_interest=None,
    )


def _sim(
    bars: Sequence[CandleBar],
    *,
    direction: Side,
    sl: int | None = None,
    tp: int | None = None,
    signals: Sequence[bool] | None = None,
    exit_signals: Sequence[bool] | None = None,
) -> tuple[list[Any], list[TradeDetail]]:
    """tf==1 degenerate dual feed: the same bars are decision AND exec stream."""
    sink: list[TradeDetail] = []
    trades = simulate_trades(
        bars,
        [True] + [False] * (len(bars) - 1) if signals is None else list(signals),
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_signals,
        detail_sink=sink,
    )
    return trades, sink


class TestKnobsDeleted:
    """Killed-token pins: the retired fill machinery stays deleted."""

    def test_policy_vocabulary_gone(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001: no FillPolicy, no LevelFill (and its
        # NEXT_OPEN optimism member), no SameBarTie (and its TP_FIRST
        # optimism member), no DEFAULT_FILL_POLICY — the law is hardcoded.
        mod = importlib.import_module("brutex.engine.trade_sim")

        for token in ("FillPolicy", "LevelFill", "SameBarTie", "DEFAULT_FILL_POLICY"):
            assert not hasattr(mod, token), token

    def test_engine_package_no_longer_reexports_the_knobs(self) -> None:
        import brutex.engine as pkg

        for token in ("FillPolicy", "LevelFill", "SameBarTie", "DEFAULT_FILL_POLICY"):
            assert token not in pkg.__all__, token
            assert not hasattr(pkg, token), token

    def test_simulate_trades_takes_no_policy_kwarg(self) -> None:
        import inspect

        params = inspect.signature(simulate_trades).parameters
        assert "policy" not in params
        assert "fill_mode" not in params

    def test_exec_bars_1m_is_required_no_default(self) -> None:
        # Design §3.3 [FILL-DUALSTREAM-ABSENT]: no silent single-feed call
        # shape survives — ``exec_bars_1m`` is keyword-only with NO default
        # on BOTH batch wrappers.
        import inspect

        from brutex.engine.trade_sim import simulate_trades_with_exit_indices

        for fn in (simulate_trades, simulate_trades_with_exit_indices):
            param = inspect.signature(fn).parameters["exec_bars_1m"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is inspect.Parameter.empty


class TestNamedRegressionGapThroughLevel:
    """(a) gap-through-level: the adverse-max clamp, both directions (B5)."""

    def test_long_tp_gap_up_fills_at_level_never_bar_low(self) -> None:
        # Long TP with a gap-up bar ENTIRELY ABOVE the level (low > tp): the
        # worst-case fill is min(tp, low) == tp — a naive sell-at-low (126 >
        # level 125) would OVERSTATE vs the at-level fill.
        bars = [
            _bar(0, 100, 100, 100, 100),  # signal bar
            _bar(1, 100, 105, 98, 100),  # entry bar — worst fill at HIGH 105
            _bar(2, 130, 140, 126, 135),  # gap-up: low 126 > level 125
        ]
        trades, sink = _sim(bars, direction=Side.BUY, tp=2000)
        assert sink[0].exit_reason == "take_profit"
        assert sink[0].entry_fill_paisa == 10500  # entry bar HIGH
        assert sink[0].exit_fill_paisa == 12500  # the LEVEL (105 + 20), NOT low 12600
        assert trades[0].pnl_paisa == 2000  # exactly the tp distance

    def test_long_sl_gap_through_fills_at_bar_low(self) -> None:
        # The other clamp arm: a bar gapping THROUGH the stop fills at its
        # extreme (min(level, low) == low), never the flattering level.
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 105, 98, 100),  # entry at HIGH 105; sl 10 → level 95
            _bar(2, 85, 88, 80, 82),  # gap-through: low 80 < 95
        ]
        trades, sink = _sim(bars, direction=Side.BUY, sl=1000)
        assert sink[0].exit_reason == "stop_loss"
        assert sink[0].exit_fill_paisa == 8000  # bar LOW, not the level 9500
        assert trades[0].pnl_paisa == 8000 - 10500

    def test_short_tp_gap_down_fills_at_level_never_bar_high(self) -> None:
        # SHORT mirror of the gap clamp: a bar entirely BELOW the target
        # covers at max(tp, high) == tp, never the (better) bar high.
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 102, 95, 100),  # SELL entry at LOW 95; tp 15 → level 80
            _bar(2, 74, 75, 70, 72),  # high 75 < level 80
        ]
        trades, sink = _sim(bars, direction=Side.SELL, tp=1500)
        assert sink[0].exit_reason == "take_profit"
        assert sink[0].entry_fill_paisa == 9500  # entry bar LOW (worst for a sell)
        assert sink[0].exit_fill_paisa == 8000  # the LEVEL
        assert trades[0].pnl_paisa == 1500

    def test_short_tp_touch_fills_at_bar_high(self) -> None:
        # A short TP TOUCH bar (low reaches the level, high far above) covers
        # at the bar HIGH — the maximally adverse printed cover.
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 102, 95, 100),  # entry LOW 95; tp level 80
            _bar(2, 90, 96, 80, 82),  # touches 80, high 96
        ]
        trades, sink = _sim(bars, direction=Side.SELL, tp=1500)
        assert sink[0].exit_fill_paisa == 9600  # max(level 8000, high 9600)
        assert trades[0].pnl_paisa == 9500 - 9600  # a "take-profit" priced at worst

    def test_sl_first_tie_on_a_bar_spanning_both_levels(self) -> None:
        # Same-bar tie law (hardcoded): a bar spanning BOTH the stop and the
        # target ALWAYS fills the stop, at the adverse-max fill
        # min(sl level, bar low).
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 105, 98, 100),  # entry HIGH 105; sl→95, tp→125
            _bar(2, 100, 130, 90, 110),  # spans BOTH 95 and 125
        ]
        trades, sink = _sim(bars, direction=Side.BUY, sl=1000, tp=2000)
        assert sink[0].exit_reason == "stop_loss"  # never take_profit
        assert sink[0].exit_fill_paisa == 9000  # min(level 9500, low 9000)
        assert trades[0].pnl_paisa == 9000 - 10500


class TestNamedRegressionEntryExtreme:
    """(b) entry extreme — the entry fills at the mapped 1m bar's adverse extreme."""

    def test_long_entry_fills_at_bar_high(self) -> None:
        bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 107, 96, 103)]
        _, sink = _sim(bars, direction=Side.BUY)
        assert sink[0].entry_fill_paisa == 10700  # bar HIGH, never open 10000

    def test_short_entry_fills_at_bar_low(self) -> None:
        bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 107, 96, 103)]
        _, sink = _sim(bars, direction=Side.SELL)
        assert sink[0].entry_fill_paisa == 9600  # bar LOW, never open 10000


class TestModeBAndTailExits:
    """Mode-B signal / forced / end_of_data / session_gap — adverse extreme."""

    def test_mode_b_exit_fills_at_adverse_extreme_fold_unchanged(self) -> None:
        # Mode-B signal exit on the 1m grid (mirrors the Rust
        # ``mode_b_exit_due_fills_at_adverse_extreme_fold_unchanged`` shape):
        # the exit signal on decision bar 2 maps its fill onto bar 3, which
        # closes at its LOW for a BUY (not open 80) — while the fill bar's
        # excursion still folds the legacy (open, open), so mae reads
        # 10500 − 8000 = 2500, never the 10500 − 7800 the fill implies (fold
        # logic deliberately untouched — only the fill PRICE is worst-case).
        bars = [
            _bar(0, 50, 60, 40, 55),
            _bar(1, 100, 105, 98, 100),  # entry HIGH 105
            _bar(2, 95, 99, 85, 90),  # exit signal fires here
            _bar(3, 80, 82, 78, 81),  # mapped fill — LOW 78 (not open 80)
        ]
        trades, sink = _sim(
            bars,
            direction=Side.BUY,
            signals=[True, False, False, False],
            exit_signals=[False, False, True, False],
        )
        assert sink[0].exit_reason == "signal"
        assert sink[0].exit_fill_paisa == 7800
        assert trades[0].pnl_paisa == 7800 - 10500
        assert trades[0].mae_paisa == 10500 - 8000

    def test_short_mode_b_exit_fills_at_bar_high(self) -> None:
        bars = [
            _bar(0, 50, 60, 40, 55),
            _bar(1, 100, 102, 95, 100),  # SELL entry LOW 95
            _bar(2, 104, 106, 103, 105),  # exit signal fires here
            _bar(3, 108, 112, 107, 110),  # covers at HIGH 112 (not open 108)
        ]
        _, sink = _sim(
            bars,
            direction=Side.SELL,
            signals=[True, False, False, False],
            exit_signals=[False, False, True, False],
        )
        assert sink[0].exit_reason == "signal"
        assert sink[0].exit_fill_paisa == 11200

    def test_forced_exit_fills_at_adverse_extreme(self) -> None:
        # A 1m bar in the forced window (15:21 IST = 09:51 UTC) closes the
        # position at its LOW for a BUY, not its open. (The 15:20 minute is
        # not stored → a slid forced fill, silently displaced.)
        base = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)
        dec = lambda v: Decimal(str(v))  # noqa: E731

        def at(ts: datetime, o: float, h: float, lo: float, c: float) -> CandleBar:
            return CandleBar(
                timestamp=ts,
                open=dec(o),
                high=dec(h),
                low=dec(lo),
                close=dec(c),
                volume=0,
                open_interest=None,
            )

        forced_ts = datetime(2024, 1, 2, 9, 51, tzinfo=UTC)  # 15:21 IST
        bars = [
            at(base, 100, 100, 100, 100),
            at(base + timedelta(minutes=1), 100, 106, 97, 103),
            at(forced_ts, 108, 110, 104, 109),
        ]
        _, sink = _sim(bars, direction=Side.BUY)
        assert sink[0].exit_reason == "forced_1520"
        assert sink[0].exit_fill_paisa == 10400  # forced bar LOW, not open 10800

    def test_forced_exit_ts_stays_in_session_never_session_gap(self) -> None:
        # [FILL-SESSION-END-NO-NEXT-1M]: a position held into the forced
        # window closes IN the current session — exit_ts is the forced bar's
        # own instant and the reason is forced_1520, NEVER session_gap and
        # never a next-session fill, even with the next session in the feed.
        base = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)
        dec = lambda v: Decimal(str(v))  # noqa: E731

        def at(ts: datetime, o: float, h: float, lo: float, c: float) -> CandleBar:
            return CandleBar(
                timestamp=ts,
                open=dec(o),
                high=dec(h),
                low=dec(lo),
                close=dec(c),
                volume=0,
                open_interest=None,
            )

        forced_ts = datetime(2024, 1, 2, 9, 51, tzinfo=UTC)  # 15:21 IST
        bars = [
            at(base, 100, 100, 100, 100),
            at(base + timedelta(minutes=1), 100, 106, 97, 103),
            at(forced_ts, 108, 110, 104, 109),
            at(base + timedelta(days=1), 130, 135, 128, 132),  # next session
        ]
        trades, sink = _sim(bars, direction=Side.BUY)
        assert sink[0].exit_reason == "forced_1520"
        assert trades[0].exit_ts == forced_ts  # in-session, the forced bar itself
        assert all(d.exit_reason != "session_gap" for d in sink)

    def test_end_of_data_fills_at_adverse_extreme_both_directions(self) -> None:
        # Three bars so the LAST bar is NOT the entry bar (the B3 close-basis
        # carve-out is pinned separately) — a genuine adverse-extreme EOD.
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 110, 95, 105),
            _bar(2, 104, 111, 96, 106),
        ]
        _, buy = _sim(bars, direction=Side.BUY)
        _, sell = _sim(bars, direction=Side.SELL)
        assert buy[0].exit_reason == "end_of_data"
        assert buy[0].exit_fill_paisa == 9600  # last bar LOW
        assert sell[0].exit_fill_paisa == 11100  # last bar HIGH

    def test_session_gap_fills_at_prior_bar_adverse_extreme(self) -> None:
        # An open position crossing an IST session date closes retroactively
        # at the PRIOR session's last stored bar — its LOW for a BUY (the
        # retired precise law read its CLOSE; the entry bar is NOT the last
        # bar here, so the B3 close basis does not apply).
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 106, 97, 103),  # entry bar (worst: HIGH 106)
            _bar(2, 103, 108, 99, 101),  # last stored bar of the session
            _bar(0, 130, 135, 128, 132, day=1),  # next session — the boundary bar
        ]
        _, sink = _sim(bars, direction=Side.BUY)
        assert sink[0].exit_reason == "session_gap"
        assert sink[0].exit_fill_paisa == 9900  # prior bar LOW (never close 10100)

    def test_session_gap_short_fills_at_prior_bar_high(self) -> None:
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 106, 97, 103),
            _bar(2, 103, 108, 99, 101),
            _bar(0, 130, 135, 128, 132, day=1),
        ]
        _, sink = _sim(bars, direction=Side.SELL)
        assert sink[0].exit_reason == "session_gap"
        assert sink[0].exit_fill_paisa == 10800  # prior bar HIGH


class TestRunIdentityEmitLaw:
    """canonical_trade_params: the fill_mode key is UNCONDITIONAL."""

    _KW: ClassVar[dict[str, Any]] = {
        "sl_paisa": 80,
        "tp_paisa": 50,
        "prior_n": 3,
        "proximity": None,
        "start_equity_paisa": 10_000_000,
    }

    def test_worst_case_params_add_exactly_the_one_key(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001: the key is emitted UNCONDITIONALLY —
        # removing it would alias every worst-case run_id with the frozen
        # precise-era identities. There is no parameter to vary: the builder
        # emits the one law on EVERY call.
        params = canonical_trade_params(**self._KW)
        assert params["fill_mode"] == "worst_case"
        assert list(params) == [
            "sl_paisa",
            "tp_paisa",
            "prior_n",
            "proximity_bps",
            "fib_anchor",
            "start_equity_paisa",
            "fill_mode",
        ]

    def test_no_fill_mode_parameter_exists(self) -> None:
        # Killed-token pin: the builder takes NO fill_mode kwarg — the law
        # cannot be varied per call (the config axis is deleted).
        import inspect

        assert "fill_mode" not in inspect.signature(canonical_trade_params).parameters

    def test_identity_differs_from_a_precise_era_dict(self) -> None:
        # The aliasing rationale, pinned: a worst-case params dict can never
        # equal the precise-era shape (which had NO fill_mode key), so no
        # run identity can collide across the law change.
        params = canonical_trade_params(**self._KW)
        precise_era_shape = {k: v for k, v in params.items() if k != "fill_mode"}
        assert params != precise_era_shape
