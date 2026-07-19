"""Unit tests for the Stage-2 trade-sim walker on the 1-MINUTE execution grid.

100% line + branch coverage (with the three sibling files) of
:mod:`brutex.engine.trade_sim` under DEC-FILL-EXEC-GRID-1M-001 — the dual-feed
walker that projects decision-TF signals onto their next-1-minute fill bars
(via :class:`brutex.engine.exec_grid.ExecGridMap`) and walks the 1m stream
into int64-paisa :class:`~brutex.metrics.types.ClosedTrade` round-trips.
Covers:

* construction validation (HOLD, bad ``tf_minutes`` / SL / TP);
* the dual-feed batch API (`run` / `run_with_exit_indices` /
  `simulate_trades`) — signal-length + exec-map dimension validation, the
  injected-map path, the B4 collision-loser signal drop;
* entry at the mapped next-1m bar's ADVERSE EXTREME; SL/TP detection + the
  adverse-max-vs-level fill per 1m bar (DEC-FILL-WORST-CASE-ONLY-001);
* the §2.4 same-bar deferral (the entry 1m bar folds its range but NEVER
  exits; ``exits_deferred`` counts an entry-bar level touch) + the B3
  terminal CLOSE carve-out (``end_of_data`` / ``session_gap`` on the trade's
  own entry bar price at that bar's CLOSE, never its adverse extreme);
* the session-aware forced exit on the 1m grid (regular 15:20, Muhurat /
  half-day boundaries, the missing-boundary-minute SLID forced fill) + the
  out-of-coverage loud refusal;
* Mode-B signal exits (mapped next-1m adverse-extreme fill; the entry-bar
  deferral into ``_pending_exit``; signal-beats-forced precedence);
* the open-position session-boundary guard (``session_gap``, the finish()
  idiom, pending subsumption, the boundary bar never arms);
* the widened SLID-fill counter family (``fill_slid_minutes`` /
  ``exit_fill_slid_minutes`` / ``forced_fill_slid_minutes``) +
  ``signals_dropped_collision``;
* the streaming ``push_1m`` / ``finish`` API, coarse-TF (decision 15m/5m over
  a real 1m stream) scenarios, VIX stamping, and the exit-index fast path.

At ``tf_minutes == 1`` with contiguous 1-minute bars the SAME sequence is
passed as ``decision_bars`` AND ``exec_bars_1m`` — the §6 degenerate fixed
point where the mapping fills a signal on decision bar N at 1m bar N+1
(exactly the historical next-bar law), so hand-derived fixtures carry over.
Every expected value is HAND-DERIVED in its test's comment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST, SessionType, TradingSession
from brutex.data.models import CandleBar
from brutex.engine import trade_sim as ts_mod
from brutex.engine.exec_grid import build_exec_grid_map
from brutex.engine.trade_sim import (
    TradeSimWalker,
    simulate_trades,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)  # a regular NSE trading Tuesday


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    """UTC-aware timestamp for an IST wall-clock ``hh:mm`` (bars store UTC)."""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    """Build a valid 1-minute :class:`CandleBar` at IST ``hh:mm`` from rupee strings."""
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _flat(hh: int, mm: int, *, day: date = _DAY) -> CandleBar:
    """A flat doji bar (O=H=L=C=100.00 → 10000 paisa) at IST ``hh:mm``."""
    return _bar(hh, mm, "100", "100", "100", "100", day=day)


def _sim(
    bars: list[CandleBar],
    entry: list[bool],
    *,
    direction: Side = Side.BUY,
    sl: int | None = None,
    tp: int | None = None,
    exits: list[bool] | None = None,
    sink: list[ts_mod.TradeDetail] | None = None,
) -> list[ClosedTrade]:
    """tf==1 degenerate batch: the SAME bars are decision AND execution stream."""
    return simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exits,
        detail_sink=sink,
    )


# ────────────────────────────────────────────────────────────────────────
# Construction validation
# ────────────────────────────────────────────────────────────────────────


class TestInit:
    def test_hold_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="BUY or SELL"):
            TradeSimWalker(direction=Side.HOLD, tf_minutes=1)

    def test_bad_tf_minutes_rejected(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=0)

    def test_non_positive_stop_rejected(self) -> None:
        with pytest.raises(ValueError, match="stop_loss_paisa must be > 0"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=0)

    def test_non_positive_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="take_profit_paisa must be > 0"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=-5)

    def test_valid_construction_with_no_legs_and_empty_feed(self) -> None:
        walker = TradeSimWalker(direction=Side.SELL, tf_minutes=5)
        assert walker.run([], [], []) == []

    def test_counters_start_at_zero(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        assert walker.exits_deferred == 0
        assert walker.signals_dropped_collision == 0
        assert walker.fill_slid_minutes == 0
        assert walker.exit_fill_slid_minutes == 0
        assert walker.forced_fill_slid_minutes == 0


# ────────────────────────────────────────────────────────────────────────
# run() input validation + the exec_map injection seam
# ────────────────────────────────────────────────────────────────────────


class TestRunValidation:
    def test_entry_signal_length_mismatch(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="entry_signals length"):
            walker.run([_flat(9, 15)], [_flat(9, 15)], [True, False])

    def test_exit_signal_length_mismatch(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="exit_signals length"):
            walker.run([_flat(9, 15)], [_flat(9, 15)], [True], [True, False])

    def test_exec_map_n_decision_mismatch_rejected(self) -> None:
        # A map built for a 2-decision stream refuses a 1-decision run.
        bars = [_flat(9, 15), _flat(9, 16)]
        exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="exec_map covers"):
            walker.run([_flat(9, 15)], bars, [True], exec_map=exec_map)

    def test_exec_map_n_exec_mismatch_rejected(self) -> None:
        bars = [_flat(9, 15), _flat(9, 16)]
        exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="exec_map covers"):
            walker.run(bars, [_flat(9, 15)], [True, False], exec_map=exec_map)

    def test_supplied_exec_map_is_used_never_rebuilt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The preload-scoped memo path: a matching injected map short-circuits
        # the build (the module-level builder must NOT be called) and yields
        # the identical trades.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        signals = [True, False, False]
        exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
        derived = _sim(bars, signals)

        def _bomb(*args: object, **kwargs: object) -> object:
            raise AssertionError("build_exec_grid_map must not be called with an injected map")

        monkeypatch.setattr(ts_mod, "build_exec_grid_map", _bomb)
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        injected = walker.run(bars, bars, signals, exec_map=exec_map)
        assert injected == derived

    def test_empty_feed_returns_no_trades(self) -> None:
        assert _sim([], []) == []

    def test_no_signal_never_opens(self) -> None:
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 17)]
        assert _sim(bars, [False, False, False]) == []


# ────────────────────────────────────────────────────────────────────────
# Entry @ the mapped next-1m adverse extreme + SL / TP adverse-max fills
# ────────────────────────────────────────────────────────────────────────


class TestEntryAndLevelExits:
    def test_long_take_profit_at_level(self) -> None:
        # signal@09:15 → fill @09:16 (flat — HIGH 100.00, the adverse
        # extreme); TP +50 → level 100.50; @09:17 high 101 triggers it but the
        # adverse-max fill is min(level 100.50, bar low 99.50) = 99.50
        # (DEC-FILL-WORST-CASE-ONLY-001) → pnl −50, holding 2 bars.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "99.50", "100.40"),
            _flat(9, 18),
        ]
        trades = _sim(bars, [True, False, False, False], tp=50)
        assert trades == [
            ClosedTrade(
                pnl_paisa=-50,  # worst-case: exit at the bar LOW 99.50
                mfe_paisa=100,  # high 101 → 100 paisa above entry
                mae_paisa=50,  # low 99.50 → 50 paisa below entry
                holding_bars=2,
                entry_ts=_ts(9, 16),
                exit_ts=_ts(9, 17),
                direction=Side.BUY,
            )
        ]

    def test_long_stop_loss_at_level(self) -> None:
        # SL -30 paisa → level 99.70; @09:17 low 99.50 breaches → the
        # adverse-max fill is min(level 99.70, low 99.50) = 99.50 → −50.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.20", "99.50", "99.80"),
            _flat(9, 18),
        ]
        trades = _sim(bars, [True, False, False, False], sl=30)
        assert len(trades) == 1
        assert trades[0].pnl_paisa == -50  # exit at the bar LOW, not the level
        assert trades[0].mae_paisa == 50  # low 99.50
        assert trades[0].mfe_paisa == 20  # high 100.20

    def test_short_take_profit_at_level(self) -> None:
        # SELL: TP +40 paisa → level 99.60; @09:17 low 99.50 triggers it but
        # the cover is max(level 99.60, bar high 100.30) = 100.30 → −30.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.30", "99.50", "99.70"),
            _flat(9, 18),
        ]
        trades = _sim(bars, [True, False, False, False], direction=Side.SELL, tp=40)
        assert trades[0].pnl_paisa == -30  # covered at the bar HIGH, not the level
        assert trades[0].mfe_paisa == 50  # short profits as price falls (low 99.50)
        assert trades[0].mae_paisa == 30  # high 100.30 is adverse for a short

    def test_short_stop_loss_at_level(self) -> None:
        # SELL: SL +25 paisa → level 100.25; @09:17 high 100.50 breaches →
        # cover = max(level 100.25, high 100.50) = 100.50 → −50.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.20"),
            _flat(9, 18),
        ]
        trades = _sim(bars, [True, False, False, False], direction=Side.SELL, sl=25)
        assert trades[0].pnl_paisa == -50

    def test_entry_bar_level_touch_defers_no_same_bar_exit(self) -> None:
        # §2.4 same-bar deferral (mirrors the Rust
        # ``entry_bar_level_touch_defers_no_same_bar_exit``): the entry bar's
        # range breaches the SL level but the entry bar NEVER exits — the
        # touch is COUNTED (exits_deferred) and detection begins on the next
        # 1m bar. Entry @09:16 HIGH 101.00; SL 100 → level 100.00; the entry
        # bar's low 99.00 ≤ 100.00 → deferred. @09:17 (flat 100): low
        # 100.00 ≤ 100.00 → SL fires, fill min(100.00, 100.00) = 100.00 →
        # pnl 10000 − 10100 = −100, holding 2 (NEVER 1).
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "101", "99", "100.50"),  # entry HIGH 101; low 99 touches
            _flat(9, 17),
        ]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=100)
        trades = walker.run(bars, bars, [True, False, False])
        assert walker.exits_deferred == 1
        assert trades[0].holding_bars == 2  # entry bar never exits
        assert trades[0].pnl_paisa == 10_000 - 10_100
        assert trades[0].mae_paisa == 200  # entry-bar low 99.00 vs entry 101.00

    def test_entry_bar_no_touch_counts_no_deferral(self) -> None:
        # The deferral counter stays 0 when the entry bar's range never
        # touches a level (the untriggered arm of the exits_deferred count).
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.10", "99.90", "100"),
            _flat(9, 17),
        ]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=1000)
        trades = walker.run(bars, bars, [True, False, False])
        assert walker.exits_deferred == 0
        assert len(trades) == 1


# ────────────────────────────────────────────────────────────────────────
# Same-bar SL+TP tie-break
# ────────────────────────────────────────────────────────────────────────


class TestSameBarTieBreak:
    def _bars(self) -> list[CandleBar]:
        # @09:17 the range 98..102 spans both SL(99) and TP(101).
        return [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "102", "98", "100"),
            _flat(9, 18),
        ]

    def test_sl_first_tie_hardcoded(self) -> None:
        # The tie is SL-first ALWAYS (DEC-FILL-WORST-CASE-ONLY-001). Entry =
        # the flat @09:16 bar's HIGH 100.00 (10000). SL level = 100.00 - 1.00
        # = 99.00 (9900); TP level = 100.00 + 1.00 = 101.00 (10100). On the
        # @09:17 bar (low 98.00 / high 102.00) BOTH levels fire — low 9800 <=
        # SL 9900 AND high 10200 >= TP 10100 — a GENUINE both-levels-touched
        # tie. The worst-case level fill is min(level, low): SL -> min(9900,
        # 9800) = 9800, TP -> min(10100, 9800) = 9800, so pnl == -200 is
        # INVARIANT under either leg — the DISTINGUISHING observable is
        # exit_reason, which MUST read stop_loss (SL-first). A test that pins
        # only pnl is vacuous w.r.t. its stated purpose (hostile-review
        # coverage-honesty; the same SL-first tie is hardcoded in the Rust
        # kernel — pinned in tests/golden/test_trade_sim_rust_golden.py).
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(self._bars(), [True, False, False, False], sl=100, tp=100, sink=sink)
        assert trades[0].pnl_paisa == 9800 - 10000  # SL first, at the bar LOW
        assert sink[0].exit_reason == "stop_loss"  # pins SL over TP on the genuine tie

    def test_short_same_bar_sl_first(self) -> None:
        # SELL mirror: entry at the flat @09:16 bar's LOW 100.00 (10000). SL
        # level = 100.00 + 1.00 = 101.00 (10100, above); TP level = 100.00 -
        # 1.00 = 99.00 (9900, below). On the @09:17 bar (low 98.00 / high
        # 102.00) BOTH fire — high 10200 >= SL 10100 AND low 9800 <= TP 9900 —
        # a GENUINE tie. Cover = max(level, high): SL -> max(10100, 10200) =
        # 10200, TP -> max(9900, 10200) = 10200, so pnl == -200 is INVARIANT;
        # exit_reason MUST read stop_loss (SL-first).
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(
            self._bars(),
            [True, False, False, False],
            direction=Side.SELL,
            sl=100,
            tp=100,
            sink=sink,
        )
        assert trades[0].pnl_paisa == 10000 - 10200
        assert sink[0].exit_reason == "stop_loss"  # pins SL over TP on the genuine tie


# ────────────────────────────────────────────────────────────────────────
# Forced session-close exit on the 1m grid
# ────────────────────────────────────────────────────────────────────────


class TestForcedExit:
    def test_forced_exit_overrides_open_position(self) -> None:
        # Enter @15:18, ride to the 15:20 bar → forced exit at its LOW
        # (100.00 — the adverse extreme). The boundary minute is STORED →
        # forced_fill_slid_minutes stays 0.
        bars = [
            _flat(15, 17),
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        walker = TradeSimWalker(
            direction=Side.BUY, tf_minutes=1, stop_loss_paisa=1000, take_profit_paisa=1000
        )
        trades = walker.run(bars, bars, [True, False, False, False])
        assert trades[0].pnl_paisa == 0  # exit at the forced bar's LOW 100.00
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].holding_bars == 3  # 15:18, 15:19, 15:20
        assert walker.forced_fill_slid_minutes == 0

    def test_entry_skipped_when_fill_bar_is_forced(self) -> None:
        # signal @15:19 → the mapped fill bar is @15:20 (forced) → the
        # walker's forced entry gate drops it; no position ever opens.
        bars = [
            _flat(15, 19),
            _bar(15, 20, "100", "105", "95", "100"),
            _bar(15, 21, "100", "105", "95", "100"),
        ]
        trades = _sim(bars, [True, False, False], tp=10)
        assert trades == []

    def test_missing_boundary_minute_slides_the_forced_fill(self) -> None:
        # §2.5/§5.2: the exact 15:20 minute is NOT stored — the first stored
        # at-or-after bar (15:21) takes the forced close, counted
        # forced_fill_slid_minutes. Fill at that bar's LOW 100.00 → pnl 0.
        bars = [
            _flat(15, 17),
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 21, "100.30", "101", "100", "100.50"),  # 15:20 missing
        ]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        trades = walker.run(bars, bars, [True, False, False, False])
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 21)
        assert trades[0].pnl_paisa == 0
        assert walker.forced_fill_slid_minutes == 1

    def test_coarse_tf_forced_exit_on_the_1m_grid(self) -> None:
        # tf=15 decision bars over a REAL 1m stream: the forced classification
        # is per 1-MINUTE bar (tf=1 in the walker), so the position closes on
        # the 15:20 1m bar exactly — never a straddling 15m-bar rule. Signal
        # on the 15:00 decision bar → close instant 15:15 → entry at the
        # 15:15 1m bar's HIGH 100.00; forced at the 15:20 1m bar's LOW 99.90.
        decision = [
            _bar(14, 45, "100", "100", "100", "100"),
            _flat(15, 0),  # signal here (close instant 15:15)
        ]
        exec_1m = [
            _flat(15, 14),
            _flat(15, 15),  # entry fill bar
            _flat(15, 16),
            _flat(15, 17),
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 20, "100.20", "100.30", "99.90", "100.10"),  # forced 1m bar
        ]
        trades = simulate_trades(
            decision,
            [False, True],
            direction=Side.BUY,
            tf_minutes=15,
            exec_bars_1m=exec_1m,
            take_profit_paisa=9999,
        )
        assert len(trades) == 1
        assert trades[0].entry_ts == _ts(15, 15)
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].pnl_paisa == -10  # forced at the 15:20 1m bar's LOW 99.90
        assert trades[0].holding_bars == 6  # 1m bars 15:15..15:20 inclusive

    def test_coarse_tf_entry_mapped_onto_forced_bar_is_dropped(self) -> None:
        # tf=15 signal on the 15:05 decision bar → close instant 15:20 → the
        # mapped fill bar IS the forced 15:20 1m bar → the entry never fills.
        decision = [_flat(15, 5)]
        exec_1m = [_flat(15, 19), _bar(15, 20, "100", "105", "95", "100"), _flat(15, 21)]
        trades = simulate_trades(
            decision, [True], direction=Side.BUY, tf_minutes=15, exec_bars_1m=exec_1m
        )
        assert trades == []


# ────────────────────────────────────────────────────────────────────────
# Mode-B signal exit (mapped next-1m adverse-extreme fill + §2.4 deferral)
# ────────────────────────────────────────────────────────────────────────


class TestModeBSignalExit:
    def test_signal_exit_fills_at_mapped_bar_adverse_extreme(self) -> None:
        # Enter @09:16; exit signal on decision @09:17 → fill @09:18 at that
        # bar's LOW (the adverse extreme for a BUY exit) 100.60 → +60.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.90", "99.90", "100.80"),
            _bar(9, 18, "100.70", "100.90", "100.60", "100.70"),
            _flat(9, 19),
        ]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(
            bars,
            [True, False, False, False, False],
            exits=[False, False, True, False, False],
            sink=sink,
        )
        assert [d.exit_reason for d in sink] == ["signal"]
        assert trades[0].pnl_paisa == 60  # @09:18 LOW 100.60 − entry 100.00
        assert trades[0].exit_ts == _ts(9, 18)
        assert trades[0].holding_bars == 3

    def test_exit_due_on_own_entry_bar_defers_to_the_successor(self) -> None:
        # §2.4 (mirrors the Rust ``mode_b_deferred_pending_fills_on_the_
        # successor``): entry AND exit signals on the same decision bar map
        # both dues onto the SAME 1m bar — the entry fills there (HIGH
        # 105.00) and the exit DEFERS into the pending slot, filling on the
        # successor at its LOW 96.00 with the (open, open) fold. pnl = 9600 −
        # 10500 = −900; mae = entry-bar fold 10500 − 9800 = 700 (the
        # successor folds (9900, 9900) → adverse 600 < 700); holding 2.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "105", "98", "100"),  # entry bar; exit_due defers
            _bar(9, 17, "99", "101", "96", "100"),  # pending fills at LOW 96
        ]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, [True, False, False], exits=[True, False, False], sink=sink)
        assert [d.exit_reason for d in sink] == ["signal"]
        assert trades == [
            ClosedTrade(
                pnl_paisa=-900,
                mfe_paisa=0,
                mae_paisa=700,
                holding_bars=2,
                entry_ts=_ts(9, 16),
                exit_ts=_ts(9, 17),
                direction=Side.BUY,
            )
        ]

    def test_deferred_pending_at_end_of_data_closes_at_close_basis(self) -> None:
        # Mirrors the Rust ``mode_b_deferred_pending_at_end_of_data_closes_
        # at_close_basis``: an exit_due on the entry bar defers; with NO
        # successor the pending is IGNORED by finish(), which closes
        # end_of_data at the entry bar's CLOSE (the B3 basis). Entry HIGH
        # 105.00, close 100.00 → pnl −500, holding 1; mae = 10500 − 9800.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "105", "98", "100"),  # entry AND last stored bar
        ]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, [True, False], exits=[True, False], sink=sink)
        assert [d.exit_reason for d in sink] == ["end_of_data"]
        assert trades[0].pnl_paisa == -500  # CLOSE 100.00 − entry HIGH 105.00
        assert trades[0].mae_paisa == 700
        assert trades[0].holding_bars == 1
        assert sink[0].exit_fill_paisa == 10_000  # the CLOSE, never the LOW 9800

    def test_exit_due_beats_forced_on_the_same_bar(self) -> None:
        # Precedence pin (mirrors the Rust ``mode_b_exit_due_beats_forced_on_
        # the_same_bar``): a bar that is BOTH the mapped exit-fill bar AND a
        # forced 1m bar closes via the SIGNAL arm — same adverse fill, but
        # the reason reads "signal", never "forced_1520". Exit signal on the
        # 15:19 decision bar → exit_due at the forced 15:20 bar. Entry
        # @15:18 HIGH 105.00; fill at the 15:20 bar's LOW 97.00 → −800; the
        # fold on the fill bar is (open, open) = (99, 99) → adverse 600 <
        # the entry-bar 700 → mae 700.
        bars = [
            _flat(15, 17),
            _bar(15, 18, "100", "105", "98", "100"),  # entry HIGH 105
            _bar(15, 19, "100", "104", "99", "100"),
            _bar(15, 20, "99", "101", "97", "100"),  # forced AND exit_due
        ]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(
            bars, [True, False, False, False], exits=[False, False, True, False], sink=sink
        )
        assert [d.exit_reason for d in sink] == ["signal"]
        assert trades[0].pnl_paisa == 9700 - 10_500
        assert trades[0].mae_paisa == 700
        assert trades[0].holding_bars == 3

    def test_exit_due_while_flat_does_nothing(self) -> None:
        # An exit signal with no open position maps its due bar but the
        # walker is flat there — nothing happens (the cheap path skips it).
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 17)]
        trades = _sim(bars, [False, False, False], exits=[True, False, False])
        assert trades == []


# ────────────────────────────────────────────────────────────────────────
# End-of-data close, the B3 terminal carve-out + the position rules
# ────────────────────────────────────────────────────────────────────────


class TestEndOfDataAndPositionRules:
    def test_open_position_closes_at_end_of_data(self) -> None:
        # No SL/TP/forced → rides to the end → closes at the last bar's LOW
        # (the adverse extreme for a BUY): 99.80 − entry 100.00 = −20.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        trades = _sim(bars, [True, False, False])
        assert trades[0].pnl_paisa == -20  # last bar LOW 99.80
        assert trades[0].holding_bars == 2
        assert trades[0].exit_ts == _ts(9, 17)

    def test_end_of_data_sell_closes_at_last_bar_high(self) -> None:
        # SELL mirror: entry at the flat bar's LOW 100.00; EOD covers at the
        # last bar's HIGH 100.50 → −50.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        trades = _sim(bars, [True, False, False], direction=Side.SELL)
        assert trades[0].pnl_paisa == -50

    def test_b3_terminal_entry_bar_is_last_bar_closes_at_close(self) -> None:
        # The B3 terminal carve-out (mirrors the Rust ``terminal_close_on_
        # entry_bar_prices_at_close_not_extreme``): the entry bar IS the
        # stream's last stored bar → the end_of_data fill is that bar's
        # CLOSE 100.45, never the adverse LOW 100.10 (the full-range phantom
        # loss). Entry HIGH 100.60 → pnl = 10045 − 10060 = −15, holding 1.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100.20", "100.60", "100.10", "100.45"),
        ]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, [True, False], sink=sink)
        assert [d.exit_reason for d in sink] == ["end_of_data"]
        assert trades == [
            ClosedTrade(
                pnl_paisa=-15,
                mfe_paisa=0,  # entry IS the bar high — nothing above it
                mae_paisa=50,  # low 100.10 vs entry 100.60
                holding_bars=1,
                entry_ts=_ts(9, 16),
                exit_ts=_ts(9, 16),
                direction=Side.BUY,
            )
        ]
        assert sink[0].exit_fill_paisa == 10_045

    def test_b3_terminal_sell_closes_at_close(self) -> None:
        # SELL twin (mirrors the Rust ``terminal_close_on_entry_bar_sell_
        # prices_at_close``): entry at the bar's LOW 100.10; close basis
        # 100.45 → pnl = 10010 − 10045 = −35; mae = high 100.60 − 100.10.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100.20", "100.60", "100.10", "100.45"),
        ]
        trades = _sim(bars, [True, False], direction=Side.SELL)
        assert trades[0].pnl_paisa == -35
        assert trades[0].mae_paisa == 50
        assert trades[0].holding_bars == 1

    def test_signals_ignored_while_in_position(self) -> None:
        # A signal every bar but only ONE position is open at a time.
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 17), _flat(9, 18)]
        trades = _sim(bars, [True, True, True, True])
        assert len(trades) == 1
        assert trades[0].holding_bars == 3  # 09:16, 09:17, 09:18

    def test_entry_due_on_exit_bar_is_ignored(self) -> None:
        # TP closes @09:17 AND an entry is due @09:17 (signal on the 09:16
        # decision bar) → no immediate re-entry on the consumed exit bar; the
        # next signal (@09:18) re-enters @09:19.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "100", "100.60"),  # TP +50 closes here
            _flat(9, 18),
            _flat(9, 19),
            _flat(9, 20),
        ]
        trades = _sim(bars, [True, True, False, True, False, False], tp=50)
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(9, 17)  # first: TP (fill min(10050, 10000))
        # second: signal@09:18 → enter@09:19 → EOD close@09:20.
        assert trades[1].entry_ts == _ts(9, 19)


# ────────────────────────────────────────────────────────────────────────
# _levels SL/TP-optionality matrix (both directions)
# ────────────────────────────────────────────────────────────────────────


class TestLevelsMatrix:
    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize(
        ("sl", "tp"),
        [(30, 50), (30, None), (None, 50), (None, None)],
    )
    def test_levels_optionality(self, direction: Side, sl: int | None, tp: int | None) -> None:
        # A calm window that never breaches a ±30-paisa band → every variant
        # rides to EOD (exercising every _levels branch without an early
        # exit). ±0.10 moves stay inside both bands.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.10", "99.90", "100"),
            _bar(9, 17, "100", "100.10", "99.90", "100"),
        ]
        trades = _sim(bars, [True, False, False], direction=direction, sl=sl, tp=tp)
        assert len(trades) == 1
        assert trades[0].holding_bars == 2


# ────────────────────────────────────────────────────────────────────────
# Streaming push_1m / finish API
# ────────────────────────────────────────────────────────────────────────


class TestStreamingAPI:
    def test_push_finish_matches_run(self) -> None:
        # The batch path is the streaming path + the map projection: driving
        # push_1m manually from the map's arm_source reproduces run() exactly.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        signals = [True, False, False]
        batch = _sim(bars, signals)

        exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        streamed: list[ClosedTrade] = []
        for j, bar in enumerate(bars):
            src = exec_map.arm_source[j]
            closed = walker.push_1m(bar, entry_due=src >= 0 and signals[src])
            if closed is not None:
                streamed.append(closed)
        tail = walker.finish()
        if tail is not None:
            streamed.append(tail)
        assert streamed == batch

    def test_finish_on_flat_walker_is_none(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        assert walker.push_1m(_flat(9, 15), entry_due=False) is None
        assert walker.finish() is None


# ────────────────────────────────────────────────────────────────────────
# MFE / MAE excursion tracking
# ────────────────────────────────────────────────────────────────────────


class TestExcursions:
    def test_mfe_mae_long(self) -> None:
        # Entry = the @09:16 bar's HIGH 100.30 (adverse extreme). Across the
        # hold the best high is 100.90 (+60) and the worst low 99.60 (−70).
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.30", "99.80", "100"),
            _bar(9, 17, "100", "100.90", "99.60", "100.20"),
        ]
        trades = _sim(bars, [True, False, False])
        assert trades[0].mfe_paisa == 60  # 100.90 − entry 100.30
        assert trades[0].mae_paisa == 70  # entry 100.30 − 99.60

    def test_mfe_mae_short(self) -> None:
        # Short entry = the @09:16 bar's LOW 99.80 (adverse extreme). Profit
        # is price falling: best low 99.60 → MFE 20; worst high 100.90 → MAE
        # 110.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.30", "99.80", "100"),
            _bar(9, 17, "100", "100.90", "99.60", "100.20"),
        ]
        trades = _sim(bars, [True, False, False], direction=Side.SELL)
        assert trades[0].mfe_paisa == 20  # entry 99.80 − 99.60
        assert trades[0].mae_paisa == 110  # 100.90 − entry 99.80


# ────────────────────────────────────────────────────────────────────────
# Session-aware forced exit (DEC-SESSION-FORCED-EXIT-001 on the 1m grid)
# ────────────────────────────────────────────────────────────────────────

# Real in-scope short sessions (verified from ``MUHURAT_SESSIONS``):
_EVENING_MUHURAT = date(2021, 11, 4)  # 18:15-19:15 IST → forced 19:05
_AFTERNOON_MUHURAT = date(2025, 10, 21)  # 13:45-14:45 IST → forced 14:35


def _trade_with_signal_then_ride(bars: list[CandleBar]) -> list[ClosedTrade]:
    """Signal on bar[0] → position opens at bar[1] and is only ever closed by
    the session forced exit / end of data (huge SL/TP disable the legs)."""
    signals = [i == 0 for i in range(len(bars))]
    return simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=100_000,
        take_profit_paisa=100_000,
    )


class TestSessionAwareForcedExit:
    def test_regular_day_still_exits_at_1520(self) -> None:
        bars = [
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
            _flat(15, 21),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)  # forced @ the 15:20 bar
        assert trades[0].pnl_paisa == 0  # filled at the bar LOW 100.00

    def test_regular_day_no_premature_exit_before_1520(self) -> None:
        # A bar at 15:19 (one minute BEFORE the boundary) does NOT force out;
        # the 15:20 bar does.
        bars = [
            _flat(15, 17),
            _flat(15, 18),
            _flat(15, 19),  # NOT forced (close 15:20 not strictly after 15:20)
            _bar(15, 20, "100.70", "101", "100", "100.90"),  # forced here
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].holding_bars == 3  # 15:18, 15:19, 15:20

    def test_evening_muhurat_exits_at_close_minus_10_not_1520(self) -> None:
        # The 2021 evening Diwali Muhurat runs 18:15-19:15 IST → forced 19:05
        # (session-resolved; a hardcoded 15:20 would force out immediately).
        bars = [
            _flat(18, 30, day=_EVENING_MUHURAT),
            _flat(18, 31, day=_EVENING_MUHURAT),
            _flat(19, 4, day=_EVENING_MUHURAT),  # NOT forced
            _bar(19, 5, "100.40", "101", "100", "100.60", day=_EVENING_MUHURAT),  # forced
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(19, 5, day=_EVENING_MUHURAT)  # NOT 15:20
        assert trades[0].pnl_paisa == 0  # forced at the 19:05 bar LOW 100.00

    def test_afternoon_muhurat_2025_exits_at_close_minus_10(self) -> None:
        # The 2025 afternoon Muhurat runs 13:45-14:45 IST → forced 14:35.
        bars = [
            _flat(14, 0, day=_AFTERNOON_MUHURAT),
            _flat(14, 34, day=_AFTERNOON_MUHURAT),  # NOT forced
            _bar(14, 35, "100.20", "101", "100", "100.30", day=_AFTERNOON_MUHURAT),  # forced
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)
        assert trades[0].pnl_paisa == 0  # forced-bar LOW 100.00 (worst-case)

    def test_position_open_exactly_at_forced_minute_exits_that_bar(self) -> None:
        bars = [
            _flat(14, 33, day=_AFTERNOON_MUHURAT),
            _flat(14, 34, day=_AFTERNOON_MUHURAT),
            _bar(14, 35, "100.50", "101", "100", "100.90", day=_AFTERNOON_MUHURAT),  # boundary
            _bar(14, 36, "200", "200", "200", "200", day=_AFTERNOON_MUHURAT),  # never reached
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)  # THIS bar
        assert trades[0].pnl_paisa == 0  # the bar LOW 100.00, never the next bar's 200

    def test_half_day_session_exits_at_early_close_minus_10(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No real HALF_DAY date exists in 2020-2026 (the enum is
        # forward-compat). Drive a genuine HALF_DAY session (12:30 early
        # close → forced 12:20) through the production ``get_session_checked``
        # seam so the real ``_forced_exit_seconds`` resolver runs. Only the
        # WALKER's seam is patched — the map build keeps the real calendar.
        half_day = date(2024, 1, 2)
        session = TradingSession(
            date=half_day,
            session_type=SessionType.HALF_DAY,
            open_time_ist=time(9, 15),
            close_time_ist=time(12, 30),
        )
        monkeypatch.setattr(
            ts_mod,
            "get_session_checked",
            lambda d: session if d == half_day else None,
        )
        bars = [
            _flat(12, 0, day=half_day),
            _flat(12, 19, day=half_day),  # NOT forced
            _bar(12, 20, "100.10", "101", "100", "100.30", day=half_day),  # forced @ 12:20
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(12, 20, day=half_day)
        assert trades[0].pnl_paisa == 0  # forced-bar LOW 100.00 (worst-case)

    def test_session_with_no_reachable_forced_exit_closes_at_end_of_data(self) -> None:
        # A short Muhurat feed that ENDS before the 19:05 boundary never
        # force-exits — it closes at the last bar's LOW (end of data).
        bars = [
            _flat(18, 30, day=_EVENING_MUHURAT),
            _flat(18, 31, day=_EVENING_MUHURAT),
            _bar(18, 45, "100", "100.50", "99.80", "100.40", day=_EVENING_MUHURAT),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(18, 45, day=_EVENING_MUHURAT)  # last bar
        assert trades[0].pnl_paisa == -20  # last bar LOW 99.80, not a forced fill

    def test_non_session_date_falls_back_to_1520_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An IN-COVERAGE date with NO trading session (get_session_checked →
        # None) must not crash: the resolver caches None and
        # is_forced_exit_bar falls back to the standard 15:20 boundary.
        monkeypatch.setattr(ts_mod, "get_session_checked", lambda d: None)
        bars = [
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)  # default 15:20 boundary
        assert trades[0].pnl_paisa == 0  # forced-bar LOW 100.00 (worst-case)

    def test_boundary_cache_resolves_once_per_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # O(1)-per-bar / O(1)-space: the boundary is resolved ONCE per IST
        # date and memoised (1-slot). Bar 0 (flat, no entry due) never
        # resolves; bar 1 (entry due) resolves; bar 2 (position) hits cache.
        calls: list[date] = []
        real_get_session = ts_mod.get_session_checked

        def _counting_get_session(d: date) -> TradingSession | None:
            calls.append(d)
            return real_get_session(d)

        monkeypatch.setattr(ts_mod, "get_session_checked", _counting_get_session)
        bars = [
            _flat(15, 17),
            _flat(15, 18),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        _trade_with_signal_then_ride(bars)
        assert calls == [_DAY]

    def test_multi_day_feed_re_resolves_boundary_per_date(self) -> None:
        # ONE batch call over TWO IST dates pins the 1-slot cache's
        # re-resolve-on-date-change branch: day-1 regular (forced 15:20),
        # day-2 the 2025 afternoon Muhurat (forced 14:35). Two independent
        # trades, each forced at its OWN session's boundary. (Day-1's
        # 15:20-decision signal is structurally inert: its fill candidate
        # lands on day 2 → the map's cross-session gate drops it.)
        bars = [
            _flat(15, 17),
            _flat(15, 18),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
            _flat(14, 33, day=_AFTERNOON_MUHURAT),
            _flat(14, 34, day=_AFTERNOON_MUHURAT),
            _bar(14, 35, "100.10", "101", "100", "100.30", day=_AFTERNOON_MUHURAT),
        ]
        signals = [True, False, False, True, False, False]
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            stop_loss_paisa=100_000,
            take_profit_paisa=100_000,
        )
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(15, 20)  # day-1 regular boundary
        assert trades[1].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)  # day-2 Muhurat
        assert trades[0].pnl_paisa == 0  # each forced fill = its bar's LOW 100.00
        assert trades[1].pnl_paisa == 0


class TestOutOfCoverageDateRefusesLoudly:
    """Calendar-correctness: a weekday OUTSIDE the published holiday span
    raises ``HolidayDataUnavailableError`` at the walker's session resolve
    (via the strict ``get_session_checked``) — loud, never a silently
    fabricated 15:20 boundary. Driven through the STREAMING seam: the batch
    path's map build would raise the same class first, so ``push_1m`` is the
    walker-owned raise site.
    """

    _OUT_DAY = date(2030, 1, 2)  # Wednesday, above the 2026 coverage ceiling

    def test_out_of_coverage_weekday_bar_raises_loud(self) -> None:
        from brutex.calendars.nse import HolidayDataUnavailableError

        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(HolidayDataUnavailableError):
            walker.push_1m(_flat(9, 15, day=self._OUT_DAY), entry_due=True)

    def test_in_coverage_behaviour_unchanged(self) -> None:
        bars = [
            _flat(15, 18),
            _flat(15, 19),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].pnl_paisa == 0  # forced-bar LOW 100.00 (worst-case)


# ────────────────────────────────────────────────────────────────────────
# Open-position session-boundary close (session_gap on the 1m stream)
# ────────────────────────────────────────────────────────────────────────

_NEXT_DAY = date(2024, 1, 3)  # the next NSE session (Wednesday)
_FRIDAY = date(2024, 1, 5)  # a regular NSE trading Friday


def _gap_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """A day-D+1 (2024-01-03) bar — the far side of the overnight gap."""
    return _bar(hh, mm, o, h, lo, c, day=_NEXT_DAY)


class TestOpenPositionSessionGapClose:
    """An open position never survives an IST session-date change (1m stream)."""

    def _day_d_hold(self) -> list[CandleBar]:
        """Signal @11:10 → entry @11:11 (flat, 100.00); flat hold through 11:14."""
        return [
            _flat(11, 10),  # signal
            _flat(11, 11),  # entry @ HIGH 100.00
            _flat(11, 12),
            _flat(11, 13),
            _flat(11, 14),  # day-D's LAST stored bar
        ]

    def test_v1_gap_bar_tp_hit_becomes_session_gap_close(self) -> None:
        # V1: the gap bar's range "hits" the ₹300 TP level — the boundary
        # guard closes FIRST at day-D's 11:14 bar (flat → LOW 100.00), pnl 0;
        # the gap bar's range NEVER enters MFE/MAE.
        bars = [*self._day_d_hold(), _gap_bar(9, 15, "600", "601", "599", "600")]
        signals = [True, False, False, False, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, tp=30_000, sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=0,
                mfe_paisa=0,
                mae_paisa=0,
                holding_bars=4,
                entry_ts=_ts(11, 11),
                exit_ts=_ts(11, 14),  # day-D's last stored bar — never day D+1
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert sink[0].exit_fill_paisa == 10_000  # day-D 11:14 adverse LOW

    def test_v1b_next_day_forced_bar_becomes_session_gap_close(self) -> None:
        # The guard fires at day D+1's FIRST bar — the position never rides
        # to D+1's 15:20 forced bar.
        bars = [
            *self._day_d_hold(),
            _gap_bar(9, 15, "600", "601", "599", "600"),
            _gap_bar(15, 20, "600", "601", "599", "600"),  # D+1's forced bar
        ]
        signals = [True] + [False] * 6
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, sink=sink)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].pnl_paisa == 0
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_v1b_boundary_bar_itself_forced_still_session_gap(self) -> None:
        # Day D+1's FIRST stored bar is itself ≥ 15:20 — the (A2) guard fires
        # BEFORE the forced branch can; the close is still session_gap at
        # day-D's last bar, never a forced fill at 600.
        bars = [*self._day_d_hold(), _gap_bar(15, 20, "600", "601", "599", "600")]
        signals = [True] + [False] * 5
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, sink=sink)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].pnl_paisa == 0
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_pending_exit_armed_on_entry_bar_is_subsumed_at_the_boundary(self) -> None:
        # The ONLY way a pending Mode-B exit survives to a session boundary:
        # entry AND exit due on the SAME 1m bar (the §2.4 deferral) where
        # that bar is the session's LAST stored bar. The boundary bar then
        # SUBSUMES the pending and closes session_gap at the entry bar's
        # CLOSE (the B3 carve-out: last_idx == entry_idx). Entry HIGH
        # 100.60; close 100.45 → pnl −15, holding 1.
        bars = [
            _flat(11, 13),  # entry + exit signals here
            _bar(11, 14, "100.20", "100.60", "100.10", "100.45"),  # entry AND last bar
            _gap_bar(9, 15, "600", "601", "599", "600"),  # boundary
        ]
        entry = [True, False, False]
        exits = [True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, entry, exits=exits, sink=sink)
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert trades[0].pnl_paisa == 10_045 - 10_060  # CLOSE − entry HIGH
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].holding_bars == 1
        assert sink[0].exit_fill_paisa == 10_045  # B3 close basis, never LOW 10010

    def test_subsumed_pending_never_reaches_a_new_position(self) -> None:
        """The (A2) pending-exit CLEAR has teeth.

        Under a mutant that drops the ``self._pending_exit = None`` clear,
        the stale day-D pending exit would close the NEW day-D+1 position on
        its first managed bar with the stale ``signal`` reason. Correct: the
        new position rides to end_of_data.
        """
        bars = [
            _flat(11, 13),  # entry + exit signals (deferral arms the pending)
            _bar(11, 14, "100.20", "100.60", "100.10", "100.45"),  # entry AND last bar
            _gap_bar(9, 15, "600", "601", "599", "600"),  # boundary → session_gap
            _gap_bar(9, 16, "600", "601", "599", "600"),  # signal → earliest re-arm
            _gap_bar(9, 17, "605", "606", "604", "605"),  # NEW entry @ HIGH 606.00
            _gap_bar(9, 18, "607", "608", "606", "607.50"),  # rides → EOD close
        ]
        entry = [True, False, False, True, False, False]
        exits = [True, False, False, False, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, entry, exits=exits, sink=sink)
        assert len(trades) == 2
        assert [d.exit_reason for d in sink] == ["session_gap", "end_of_data"]
        assert trades[1].entry_ts == _ts(9, 17, day=_NEXT_DAY)
        assert trades[1].exit_ts == _ts(9, 18, day=_NEXT_DAY)
        assert trades[1].pnl_paisa == 60_600 - 60_600  # EOD LOW 606.00 − entry HIGH 606.00
        assert trades[1].holding_bars == 2

    def test_sell_direction_symmetric_session_gap_close(self) -> None:
        # SELL variant: the boundary close is direction-aware through _close
        # (a short would otherwise have "profited" the inverse gap).
        bars = [*self._day_d_hold(), _gap_bar(9, 15, "600", "601", "599", "600")]
        signals = [True] + [False] * 5
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, direction=Side.SELL, sink=sink)
        assert len(trades) == 1
        assert trades[0].direction is Side.SELL
        assert trades[0].pnl_paisa == 0  # entry 100.00 − day-D HIGH 100.00 (flat)
        assert trades[0].exit_ts == _ts(11, 14)
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_boundary_bar_never_fills_even_when_entry_due(self) -> None:
        # The boundary bar EMITS the session_gap close, so the (A) gate's
        # ``closed is None`` clause suppresses an entry due ON it. (Via the
        # batch path such a due is structurally impossible — the map's
        # cross-session gate drops it — so the STREAMING seam pins the
        # walker-level defense.)
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        for j, bar in enumerate(self._day_d_hold()):
            assert walker.push_1m(bar, entry_due=j == 1) is None
        closed = walker.push_1m(_gap_bar(9, 15, "600", "601", "599", "600"), entry_due=True)
        assert closed is not None  # the session_gap close, priced at day-D's 11:14
        assert closed.exit_ts == _ts(11, 14)
        assert closed.pnl_paisa == 0
        assert walker.finish() is None  # NO new position opened on the boundary bar

    def test_multi_session_hole_fires_once_same_fill(self) -> None:
        # The compare is date INEQUALITY, not adjacency: a Tue → Fri step
        # fires the guard exactly once with the identical fill.
        bars = [
            _flat(11, 10),
            _flat(11, 11),
            _bar(11, 12, "100", "100.30", "99.90", "100.10"),
            _bar(9, 15, "600", "601", "599", "600", day=_FRIDAY),  # 3 calendar days later
        ]
        signals = [True, False, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=-10,  # 11:12 LOW 99.90 − entry 100.00 (adverse extreme)
                mfe_paisa=30,
                mae_paisa=10,
                holding_bars=2,
                entry_ts=_ts(11, 11),
                exit_ts=_ts(11, 12),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_entry_on_sessions_last_stored_bar_closes_at_close_basis(self) -> None:
        # The B3 terminal carve-out on the session_gap arm (mirrors the Rust
        # ``session_gap_on_entry_bar_prices_at_close_basis``): the entry
        # fills ON day-D's last stored bar → holding 1 and the fill is that
        # bar's CLOSE 100.45 (never the adverse LOW 100.10 — the full-range
        # phantom loss). pnl = 10045 − entry HIGH 10060 = −15.
        bars = [
            _flat(11, 13),  # signal
            _bar(11, 14, "100.20", "100.60", "100.10", "100.45"),  # entry AND last stored
            _gap_bar(9, 15, "600", "601", "599", "600"),
        ]
        signals = [True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = _sim(bars, signals, sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=-15,  # CLOSE 100.45 − entry HIGH 100.60
                mfe_paisa=0,  # entry IS the bar high — nothing above it
                mae_paisa=50,  # low 100.10 vs entry 100.60
                holding_bars=1,
                entry_ts=_ts(11, 14),
                exit_ts=_ts(11, 14),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert sink[0].exit_fill_paisa == 10_045

    def test_entry_day_is_the_ist_session_date_not_the_utc_date(self) -> None:
        """The (A) fill stamps ``entry_day_ist`` from the IST session date,
        never the UTC calendar date (defense-in-depth mutant pin).

        An entry fill at IST 2024-01-02 01:00 is UTC 2024-01-01 19:30 — under
        a ``bar.timestamp.date()`` (UTC-date) mutant ``entry_day_ist`` reads
        2024-01-01 and the (A2) guard phantom-closes the just-opened position
        on a later same-IST-session bar. The correct IST stamp HOLDS through
        IST 05:00 (UTC 2024-01-01 23:30) to the honest end_of_data finish.
        (The 00:55 signal's fill slides 00:56 → 01:00 — a counted slid fill.)
        """
        bars = [
            _flat(0, 55),  # signal — IST date 2024-01-02, UTC date 2024-01-01
            _flat(1, 0),  # entry fill (slid from 00:56) — UTC date still 2024-01-01
            _flat(5, 0),  # same IST session date — the position must HOLD
        ]
        sink: list[ts_mod.TradeDetail] = []
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        trades, _ = walker.run_with_exit_indices(bars, bars, [True, False, False])
        assert trades == [
            ClosedTrade(
                pnl_paisa=0,
                mfe_paisa=0,
                mae_paisa=0,
                holding_bars=2,
                entry_ts=_ts(1, 0),
                exit_ts=_ts(5, 0),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["end_of_data"]
        assert walker.fill_slid_minutes == 1  # the 00:56 → 01:00 slide

    def test_backward_session_date_change_still_closes_session_gap(self) -> None:
        """The (A2) compare is an EQUALITY law: ANY IST session-date CHANGE
        closes the open position — either direction, not only forward.

        The batch path now REFUSES a decreasing-timestamp stream at the map
        build, so the backward-dated bar is driven through the STREAMING
        seam (``push_1m`` derives each bar's session date from its own
        timestamp and enforces no monotonicity). The entry bar is the last
        managed bar, so the close takes the B3 CLOSE basis: entry HIGH
        102.00, close 101.00 → pnl −100.
        """
        sink: list[ts_mod.TradeDetail] = []
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert (
            walker.push_1m(_bar(11, 10, "100", "100", "100", "100", day=_NEXT_DAY), entry_due=False)
            is None
        )
        assert (
            walker.push_1m(_bar(11, 11, "100", "102", "99", "101", day=_NEXT_DAY), entry_due=True)
            is None
        )
        closed = walker.push_1m(_bar(11, 12, "100", "101", "99", "100"), entry_due=False)
        assert closed == ClosedTrade(
            pnl_paisa=-100,  # B3 close basis 101.00 − entry HIGH 102.00
            mfe_paisa=0,
            mae_paisa=300,  # entry-bar low 99.00 vs entry 102.00
            holding_bars=1,
            entry_ts=_ts(11, 11, day=_NEXT_DAY),
            exit_ts=_ts(11, 11, day=_NEXT_DAY),  # never the backward bar
            direction=Side.BUY,
        )
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert sink[0].exit_fill_paisa == 10_100  # the entry bar's CLOSE


class TestSessionGapFinishEquivalence:
    """The finish()-equivalence pin (suffix independence by construction).

    The SAME day-D prefix closed by (a) the (A2) boundary guard when the
    feed continues into day D+1 vs (b) ``finish()`` when the feed truncates
    at day-D's last stored bar emits a bit-equal ``ClosedTrade`` AND exit
    index; ONLY ``TradeDetail.exit_reason`` differs (``session_gap`` vs
    ``end_of_data``). VIX stamps ride the same (entry_ts, exit_ts) pair in
    both worlds.
    """

    def _prefix(self) -> list[CandleBar]:
        return [
            _bar(11, 10, "100", "101", "99", "100"),  # signal
            _bar(11, 11, "100.50", "102", "100", "101"),  # entry @ HIGH 102.00
            _bar(11, 12, "101", "103", "100.25", "102"),  # day-D's last stored bar
        ]

    def test_boundary_close_bit_equals_finish_close(self) -> None:
        from brutex.engine.reference_series import TradeReferenceStamper

        prefix = self._prefix()
        vix = [_vix_bar(11, 10, "15.10"), _vix_bar(11, 11, "15.20"), _vix_bar(11, 12, "15.30")]
        stamper = TradeReferenceStamper.from_vix_bars(vix)
        continued = [*prefix, _gap_bar(9, 15, "600", "601", "599", "600")]

        sink_full: list[ts_mod.TradeDetail] = []
        full_trades, full_idx = ts_mod.simulate_trades_with_exit_indices(
            continued,
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=continued,
            reference_stamper=stamper,
            detail_sink=sink_full,
        )
        sink_trunc: list[ts_mod.TradeDetail] = []
        trunc_trades, trunc_idx = ts_mod.simulate_trades_with_exit_indices(
            prefix,
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=prefix,
            reference_stamper=stamper,
            detail_sink=sink_trunc,
        )
        assert full_trades == trunc_trades  # bit-equal ClosedTrade (VIX stamps included)
        assert full_idx == trunc_idx == [2]  # same TRUE exit index
        assert [d.exit_reason for d in sink_full] == ["session_gap"]
        assert [d.exit_reason for d in sink_trunc] == ["end_of_data"]
        # Exit fill = the 11:12 bar's adverse LOW 100.25 in both worlds.
        assert sink_full[0].exit_fill_paisa == sink_trunc[0].exit_fill_paisa == 10_025
        assert full_trades[0].vix_at_exit == Decimal("15.30")


# ────────────────────────────────────────────────────────────────────────
# India VIX reference stamp (DEC-VIX-TRADE-STAMP-001)
# ────────────────────────────────────────────────────────────────────────


def _vix_bar(hh: int, mm: int, close: str, *, day: date = _DAY) -> CandleBar:
    """A flat VIX reference bar at IST ``hh:mm`` (aligned to the index feed)."""
    return _bar(hh, mm, close, close, close, close, day=day)


class TestVixStamp:
    """The reference-series stamper wires the India VIX level onto each trade.

    India VIX is REFERENCE ONLY (CLAUDE.md §1/§3): stamped for downstream
    filter analysis, never a signal input. ``None`` on a missing VIX bar is
    the no-hallucination contract (Golden Rule #1).
    """

    def _index_bars(self) -> list[CandleBar]:
        # signal @09:15 → enter @09:16 → end-of-data exit @09:17.
        return [
            _bar(9, 15, "100", "100", "100", "100"),
            _bar(9, 16, "101", "101", "101", "101"),
            _bar(9, 17, "102", "102", "102", "102"),
        ]

    def _stamped(self, vix: list[CandleBar]) -> list[ClosedTrade]:
        from brutex.engine.reference_series import TradeReferenceStamper

        return simulate_trades(
            self._index_bars(),
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=self._index_bars(),
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )

    def test_no_stamper_leaves_vix_none(self) -> None:
        trades = _sim(self._index_bars(), [True, False, False])
        assert len(trades) == 1
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit is None

    def test_stamp_present_at_entry_and_exit(self) -> None:
        # Entry fills at 09:16, exit at 09:17 — the stamp tracks those 1m
        # instants, NOT the 09:15 signal bar.
        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20"), _vix_bar(9, 17, "15.30")]
        trades = self._stamped(vix)
        assert trades[0].vix_at_entry == Decimal("15.20")
        assert trades[0].vix_at_exit == Decimal("15.30")

    def test_stamp_missing_at_entry_is_none(self) -> None:
        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 17, "15.30")]  # no 09:16
        trades = self._stamped(vix)
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit == Decimal("15.30")

    def test_stamp_missing_at_exit_is_none(self) -> None:
        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20")]  # no 09:17
        trades = self._stamped(vix)
        assert trades[0].vix_at_entry == Decimal("15.20")
        assert trades[0].vix_at_exit is None

    def test_stamp_missing_at_both_is_none(self) -> None:
        trades = self._stamped([])
        assert len(trades) == 1
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit is None

    def test_stamp_via_streaming_finish_path(self) -> None:
        """The end-of-data ``finish`` close is also stamped (routes via _close)."""
        from brutex.engine.reference_series import TradeReferenceStamper

        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20"), _vix_bar(9, 17, "15.30")]
        walker = TradeSimWalker(
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )
        bars = self._index_bars()
        closed = [walker.push_1m(b, entry_due=j == 1) for j, b in enumerate(bars)]
        assert all(c is None for c in closed)  # ends open → closes in finish()
        final = walker.finish()
        assert final is not None
        assert final.vix_at_entry == Decimal("15.20")
        assert final.vix_at_exit == Decimal("15.30")


# ────────────────────────────────────────────────────────────────────────
# DEC-SWEEP-EXIT-INDEX-FASTPATH-001 — exit-index-carrying walk (1m indices)
# ────────────────────────────────────────────────────────────────────────


class TestExitIndices:
    """run_with_exit_indices / simulate_trades_with_exit_indices."""

    def _bars(self) -> list[CandleBar]:
        """Two closed trades: a TP exit on bar 2 and a B3 terminal close on bar 4."""
        return [
            _flat(9, 16),  # signal arms here
            _flat(9, 17),  # entry @ HIGH 100.00
            _bar(9, 18, "100", "101", "100", "101"),  # TP +50 fires (exit idx 2)
            _flat(9, 19),  # signal arms again
            _flat(9, 20),  # entry AND last bar → B3 close (idx 4)
        ]

    def _signals(self) -> list[bool]:
        return [True, False, False, True, False]

    def _run(
        self, bars: list[CandleBar], signals: list[bool]
    ) -> tuple[list[ClosedTrade], list[int] | None]:
        return ts_mod.simulate_trades_with_exit_indices(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            take_profit_paisa=50,
        )

    def test_indices_align_with_the_trades(self) -> None:
        # Trade 1: entry 09:17 @10000, TP level 10050, bar 09:18 high 10100
        # fires → fill min(10050, low 10000) = 10000 → pnl 0, exit idx 2.
        # Trade 2: entry 09:20 (the last bar) → B3 close at its CLOSE 10000
        # → pnl 0, exit idx 4.
        trades, exit_indices = self._run(self._bars(), self._signals())
        assert exit_indices is not None
        assert len(trades) == 2
        assert exit_indices == [2, 4]
        assert trades[0].exit_ts == self._bars()[2].timestamp
        assert trades[1].exit_ts == self._bars()[4].timestamp

    def test_trades_are_byte_identical_to_simulate_trades(self) -> None:
        plain = _sim(self._bars(), self._signals(), tp=50)
        with_idx, _ = self._run(self._bars(), self._signals())
        assert plain == with_idx

    def test_uncertified_window_returns_none_indices(self) -> None:
        """Duplicate timestamps fail the bars_strictly_increasing certificate:
        the trades still simulate, the indices are withheld."""
        bars = self._bars()
        bars.append(bars[-1])  # duplicate final timestamp
        signals = [*self._signals(), False]
        trades, exit_indices = self._run(bars, signals)
        assert exit_indices is None
        assert len(trades) == 2

    def test_no_trades_yields_empty_indices(self) -> None:
        trades, exit_indices = ts_mod.simulate_trades_with_exit_indices(
            self._bars(),
            [False] * 5,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=self._bars(),
        )
        assert trades == []
        assert exit_indices == []

    def test_run_with_exit_indices_validates_signal_lengths(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="entry_signals length"):
            walker.run_with_exit_indices(self._bars(), self._bars(), [True])
        with pytest.raises(ValueError, match="exit_signals length"):
            walker.run_with_exit_indices(self._bars(), self._bars(), [False] * 5, [True])

    def test_run_delegates_to_the_index_walk(self) -> None:
        """run() is the thin delegate — same trades, indices dropped."""
        walker_a = TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=50)
        walker_b = TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=50)
        via_run = walker_a.run(self._bars(), self._bars(), self._signals())
        via_pair, _ = walker_b.run_with_exit_indices(self._bars(), self._bars(), self._signals())
        assert via_run == via_pair


# ────────────────────────────────────────────────────────────────────────
# The widened SLID-fill counter family + B4 collision signal drops (§5.2)
# ────────────────────────────────────────────────────────────────────────


class TestSlidFillCounters:
    def test_entry_fill_on_slid_bar_counts_fill_slid_minutes(self) -> None:
        # Signal on the 09:16 decision bar → close instant 09:17 — that
        # minute is NOT stored, so the fill slides to 09:18 (the next stored
        # same-session minute) and is COUNTED. Entry @ the 09:18 bar's HIGH
        # 102.00; EOD at the 09:19 flat bar's LOW 100.00 → −200.
        bars = [_flat(9, 15), _flat(9, 16), _bar(9, 18, "100", "102", "99", "101"), _flat(9, 19)]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        trades, _ = walker.run_with_exit_indices(bars, bars, [False, True, False, False])
        assert walker.fill_slid_minutes == 1
        assert walker.exit_fill_slid_minutes == 0
        assert len(trades) == 1
        assert trades[0].entry_ts == _ts(9, 18)
        assert trades[0].pnl_paisa == -200

    def test_direct_mode_b_exit_on_slid_bar_counts_exit_fill_slid(self) -> None:
        # Entry @09:16 (not slid). Exit signal on the 09:17 decision bar →
        # close instant 09:18 is missing → the exit fill slides to 09:19 and
        # is counted exit_fill_slid_minutes (the direct consumption arm).
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 17), _bar(9, 19, "100", "101", "99.50", "100")]
        sink: list[ts_mod.TradeDetail] = []
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        trades, _ = walker.run_with_exit_indices(
            bars, bars, [True, False, False, False], [False, False, True, False]
        )
        assert walker.fill_slid_minutes == 0
        assert walker.exit_fill_slid_minutes == 1
        assert [d.exit_reason for d in sink] == ["signal"]
        assert trades[0].pnl_paisa == 9950 - 10_000  # fill at the slid bar's LOW

    def test_deferred_exit_on_slid_entry_bar_counts_both(self) -> None:
        # Entry AND exit due on the SAME slid bar: the entry fill counts
        # fill_slid_minutes and the exit — deferred onto the entry bar's
        # successor — counts exit_fill_slid_minutes at the slid arm bar.
        bars = [_flat(9, 15), _flat(9, 16), _bar(9, 18, "100", "102", "99", "101"), _flat(9, 19)]
        sink: list[ts_mod.TradeDetail] = []
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        trades, _ = walker.run_with_exit_indices(
            bars, bars, [False, True, False, False], [False, True, False, False]
        )
        assert walker.fill_slid_minutes == 1
        assert walker.exit_fill_slid_minutes == 1
        assert [d.exit_reason for d in sink] == ["signal"]  # pending fills @09:19
        assert trades[0].exit_ts == _ts(9, 19)

    def test_slid_arm_bar_with_no_signal_counts_nothing(self) -> None:
        # The arm is mask-independent — a slid arm bar whose decision bar
        # carries NO signal consumes nothing and counts nothing.
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 18), _flat(9, 19)]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        trades, _ = walker.run_with_exit_indices(bars, bars, [False] * 4, [False] * 4)
        assert trades == []
        assert walker.fill_slid_minutes == 0
        assert walker.exit_fill_slid_minutes == 0

    def test_collision_loser_drops_entry_and_exit_signals_counted(self) -> None:
        # B4: two decision bars map onto ONE 1m fill bar (decision closes
        # 09:16 and 09:17 both slide to the 09:20 exec bar) — the
        # earliest-armed (idx 0) wins; the loser (idx 1) drops its entry AND
        # exit signal, each counted.
        decision = [_flat(9, 15), _flat(9, 16)]
        exec_1m = [_flat(9, 15), _flat(9, 20)]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        trades, _ = walker.run_with_exit_indices(decision, exec_1m, [False, True], [False, True])
        assert walker.signals_dropped_collision == 2
        assert trades == []  # the winner (idx 0) carried no signal

    def test_collision_loser_without_signals_drops_nothing(self) -> None:
        # A loser with no live signal drops nothing; the WINNER's entry
        # fills normally (on the slid shared bar).
        decision = [_flat(9, 15), _flat(9, 16)]
        exec_1m = [_flat(9, 15), _bar(9, 20, "100", "100.50", "99.80", "100.20")]
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        trades, _ = walker.run_with_exit_indices(decision, exec_1m, [True, False])
        assert walker.signals_dropped_collision == 0
        assert walker.fill_slid_minutes == 1  # the winner's fill is a slid fill
        assert len(trades) == 1  # B3 terminal close on the shared bar
        assert trades[0].entry_ts == _ts(9, 20)

    # ── Two-consecutive-slid-bars dedupe (fix-round-6 fill-law-1) ──────────
    #
    # A DEFERRED Mode-B exit (entry + exit on ONE decision bar) is counted at
    # its MAPPED arm bar (design law 6: exec_idx(N) = the arm bar, NOT the
    # §2.4 landing successor). When it physically lands on a slid successor
    # whose OWN decision bar ALSO carries an exit signal, the direct arm must
    # NOT re-count the same physical fill — a single exit fill contributes AT
    # MOST ONE to exit_fill_slid_minutes (law 9 / §5.2: EVERY slid FILL is
    # counted — a fill, once). tf=2 grid: decision [09:15, 09:17] (close
    # instants 09:17 / 09:19); exec [09:15, 09:18, 09:20] → arm_source
    # (-1, 0, 1), arm_slid (False, True, True) — the entry arm bar 09:18 AND
    # the exit's landing 09:20 both slid.

    def _two_slid_streams(self) -> tuple[list[CandleBar], list[CandleBar]]:
        decision = [_flat(9, 15), _flat(9, 17)]
        exec_1m = [_flat(9, 15), _flat(9, 18), _bar(9, 20, "100", "101", "99.50", "100")]
        emap = build_exec_grid_map(decision, exec_1m, tf_minutes=2)
        assert emap.arm_source == (-1, 0, 1)
        assert emap.arm_slid == (False, True, True)
        return decision, exec_1m

    def test_deferred_exit_on_two_consecutive_slid_bars_counts_once(self) -> None:
        # Entry+exit on decision 0 (slid arm 09:18) → deferred exit lands on
        # the slid 09:20 successor, whose own decision (idx 1) ALSO fires exit.
        # The deferred exit is counted ONCE (at its mapped arm bar), NOT twice.
        decision, exec_1m = self._two_slid_streams()
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=2)
        trades, idxs = walker.run_with_exit_indices(decision, exec_1m, [True, False], [True, True])
        assert len(trades) == 1
        assert walker.fill_slid_minutes == 1
        assert walker.exit_fill_slid_minutes == 1  # was 2 pre-dedupe
        assert trades[0].entry_ts == _ts(9, 18)
        assert trades[0].exit_ts == _ts(9, 20)
        assert idxs == [2]

    def test_deferred_exit_second_signal_is_pure_noop_on_fill(self) -> None:
        # The second (successor) exit signal only ever moved the COUNTER — the
        # physical trade is byte-identical to the single-exit run, proving the
        # dedupe touches nothing but the observability count.
        decision, exec_1m = self._two_slid_streams()
        w2 = TradeSimWalker(direction=Side.BUY, tf_minutes=2)
        t2, _ = w2.run_with_exit_indices(decision, exec_1m, [True, False], [True, True])
        w1 = TradeSimWalker(direction=Side.BUY, tf_minutes=2)
        t1, _ = w1.run_with_exit_indices(decision, exec_1m, [True, False], [True, False])
        # Same single deferred exit fill on the slid 09:20 successor either way.
        assert w1.exit_fill_slid_minutes == 1
        assert w2.exit_fill_slid_minutes == 1
        assert len(t1) == len(t2) == 1
        assert t1[0].pnl_paisa == t2[0].pnl_paisa
        assert t1[0].entry_ts == t2[0].entry_ts == _ts(9, 18)
        assert t1[0].exit_ts == t2[0].exit_ts == _ts(9, 20)


# ────────────────────────────────────────────────────────────────────────
# Coarse decision TF over a REAL 1m execution stream
# ────────────────────────────────────────────────────────────────────────


class TestCoarseTf:
    def _decision(self) -> list[CandleBar]:
        # Two 5-minute decision bars (bucket-START labels 09:15 / 09:20).
        return [
            _bar(9, 15, "100", "101", "99", "100"),
            _bar(9, 20, "100", "101", "99", "100"),
        ]

    def _exec_1m(self) -> list[CandleBar]:
        return [
            _flat(9, 15),
            _flat(9, 16),
            _flat(9, 17),
            _flat(9, 18),
            _flat(9, 19),
            _bar(9, 20, "100.20", "100.50", "100.10", "100.30"),  # the mapped fill bar
            _bar(9, 21, "100.30", "100.40", "100.00", "100.05"),  # SL fires here
            _flat(9, 22),
        ]

    def test_tf5_signal_fills_at_close_instant_1m_bar(self) -> None:
        # Signal on the 09:15 5m decision bar → close instant 09:20 → entry
        # at the 09:20 1m bar's HIGH 100.50. SL 40 → level 100.10; the entry
        # bar's own low 100.10 touches it → DEFERRED (counted). The 09:21
        # bar's low 100.00 ≤ 100.10 → SL fills min(100.10, 100.00) = 100.00.
        # pnl = 10000 − 10050 = −50, holding 2 (1m bars 09:20, 09:21);
        # mae = entry 10050 − low 10000 = 50.
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=5, stop_loss_paisa=40)
        trades, exit_idx = walker.run_with_exit_indices(
            self._decision(), self._exec_1m(), [True, False]
        )
        assert walker.exits_deferred == 1
        assert trades == [
            ClosedTrade(
                pnl_paisa=-50,
                mfe_paisa=0,
                mae_paisa=50,
                holding_bars=2,
                entry_ts=_ts(9, 20),
                exit_ts=_ts(9, 21),
                direction=Side.BUY,
            )
        ]
        assert exit_idx == [6]  # 1-MINUTE index within exec_bars_1m
        # No look-ahead: the fill instant is at/after the decision close.
        assert trades[0].entry_ts >= self._decision()[0].timestamp

    def test_decision_bar_prices_are_signal_domain_only(self) -> None:
        # Mutating the DECISION bars' OHLC (same timestamps) changes NO fill:
        # prices come exclusively from the 1m execution stream.
        mutated = [
            _bar(9, 15, "500", "999", "1", "42"),
            _bar(9, 20, "500", "999", "1", "42"),
        ]
        base = simulate_trades(
            self._decision(),
            [True, False],
            direction=Side.BUY,
            tf_minutes=5,
            exec_bars_1m=self._exec_1m(),
            stop_loss_paisa=40,
        )
        moved = simulate_trades(
            mutated,
            [True, False],
            direction=Side.BUY,
            tf_minutes=5,
            exec_bars_1m=self._exec_1m(),
            stop_loss_paisa=40,
        )
        assert base == moved
