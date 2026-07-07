"""Unit tests for :mod:`brutex.engine.trade_capture` (DEC-SWEEP-RESULTS-DEPTH-001 WP1).

Covers: the parity-tooth pass/fail branches (mismatch ⇒ loud WARN + empty
capture, never a wrong trade list), VIX stamped / missing / no-stamper honesty
counting, zero-trade combos as first-class, the exit-fill belt + alignment
belt, the 1m-path attachment (incl. the tf==1m walker-equality law), the
NULL-BY-DESIGN ``lots`` / ``costs_paisa`` (honest ``None`` until P5's futures
translation — gap-register Ruling 18, never a fabricated 1/0), and the O-e
baked decision — the walker's AS-FOUND re-entry semantics pinned by test (next
fresh signal after an exit re-enters, no cooldown, until the 15:20 wall).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import structlog.testing
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.minute_path import MinuteIndex, PathStatus
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.results_store import ResultRow
from brutex.engine.trade_capture import (
    CapturedTrade,
    TradeCaptureError,
    _check_detail_alignment,
    _check_exit_fill_belt,
    capture_combo,
    derived_exit_fill,
)
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)


def _ts(hh: int, mm: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


# TP → SL → forced-15:20 (the trade-sim golden Fixture A, verbatim bars).
_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
    _bar(9, 18, "100", "100", "100", "100"),
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),
    _bar(15, 17, "100", "100", "100", "100"),
    _bar(15, 18, "100.00", "100.20", "99.80", "100.10"),
    _bar(15, 19, "100.00", "100.30", "99.90", "100.20"),
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),
]
#: bar_bits with bit 0 set exactly where Fixture A's signals fired.
_SIGNAL_IDX = (0, 3, 6)
_BAR_BITS = [1 if i in _SIGNAL_IDX else 0 for i in range(len(_BARS))]
_MASK = 0b1

_RUN_ID = "ab" * 32
_SL = 30
_TP = 50


def _oracle_trades(direction: Side = Side.BUY) -> list[ClosedTrade]:
    signals = [bool(b & _MASK == _MASK) for b in _BAR_BITS]
    return simulate_trades(
        _BARS,
        signals,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=_SL,
        take_profit_paisa=_TP,
    )


def _row(
    *,
    run_id: str = _RUN_ID,
    direction: str = "bull",
    total: int | None = None,
    winning: int | None = None,
    losing: int | None = None,
    net: int | None = None,
) -> ResultRow:
    trades = _oracle_trades(Side.BUY if direction == "bull" else Side.SELL)
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction=direction,
        mode="A",
        mask_bits=_MASK,
        net_pnl_paisa=net if net is not None else sum(t.pnl_paisa for t in trades),
        gross_profit_paisa=sum(t.pnl_paisa for t in trades if t.pnl_paisa > 0),
        gross_loss_paisa=-sum(t.pnl_paisa for t in trades if t.pnl_paisa < 0),
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=total if total is not None else len(trades),
        winning_trades=(
            winning if winning is not None else sum(1 for t in trades if t.pnl_paisa > 0)
        ),
        losing_trades=losing if losing is not None else sum(1 for t in trades if t.pnl_paisa < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )


class TestCaptureParityPass:
    def test_capture_reproduces_the_stored_row(self) -> None:
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is True
        assert capture.parity_detail == ""
        assert capture.run_id == _RUN_ID
        assert [t.exit_reason for t in capture.trades] == [
            "take_profit",
            "stop_loss",
            "forced_1520",
        ]
        # Contract fields: real fill prices as 2dp Decimals; lots/costs are
        # honest None — NULL-by-design until P5 (Ruling 18), never a
        # fabricated 1/0.
        assert [t.entry_px for t in capture.trades] == [Decimal("100.00")] * 3
        assert [t.exit_px for t in capture.trades] == [
            Decimal("100.50"),
            Decimal("99.70"),
            Decimal("100.15"),
        ]
        assert all(t.lots is None for t in capture.trades)
        assert all(t.costs_paisa is None for t in capture.trades)
        assert [t.pnl_paisa for t in capture.trades] == [50, -30, 15]
        # No minute index → honest NO_1M_DATA, None excursions.
        assert all(t.path_1m_status is PathStatus.NO_1M_DATA for t in capture.trades)
        assert all(t.mae_1m_paisa is None and t.mfe_1m_paisa is None for t in capture.trades)
        # No stamper → every trade counts as VIX-missing (honest NULL).
        assert capture.vix_missing_trades == 3
        assert all(t.vix_entry is None and t.vix_exit is None for t in capture.trades)
        # Equity: one session, close carries the +35 net.
        assert capture.equity_status is PathStatus.NO_1M_DATA
        assert len(capture.equity) == 1
        assert capture.equity[0].equity_close_paisa == 10_000_000 + 35
        assert capture.equity[0].trades_closed == 3

    def test_sell_direction_maps_bear(self) -> None:
        capture = capture_combo(
            _row(direction="bear"),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is True
        assert all(t.direction is Side.SELL for t in capture.trades)

    def test_minute_index_at_tf1m_matches_walker_excursions(self) -> None:
        """At tf==1m the 1m fold equals the walker's own MFE/MAE bit-for-bit."""
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=MinuteIndex.from_bars(_BARS),
            stamper=None,
        )
        assert capture.parity_ok is True
        for trade in capture.trades:
            assert trade.path_1m_status is PathStatus.OK
            assert trade.mae_1m_paisa == trade.mae_paisa
            assert trade.mfe_1m_paisa == trade.mfe_paisa
        assert capture.equity_status is PathStatus.OK
        assert capture.equity[0].equity_intraday_low_paisa is not None

    def test_vix_stamped_and_partial_miss_counted(self) -> None:
        # VIX bars exist at ALL trade instants except the 15:20 forced exit.
        vix_bars = [
            _bar(9, 16, "15.25", "15.25", "15.25", "15.25"),
            _bar(9, 17, "15.30", "15.30", "15.30", "15.30"),
            _bar(9, 19, "15.40", "15.40", "15.40", "15.40"),
            _bar(9, 20, "15.45", "15.45", "15.45", "15.45"),
            _bar(15, 18, "15.50", "15.50", "15.50", "15.50"),
        ]
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=TradeReferenceStamper.from_vix_bars(vix_bars),
        )
        assert capture.parity_ok is True
        first, second, third = capture.trades
        assert (first.vix_entry, first.vix_exit) == (Decimal("15.25"), Decimal("15.30"))
        assert (second.vix_entry, second.vix_exit) == (Decimal("15.40"), Decimal("15.45"))
        assert third.vix_entry == Decimal("15.50")
        assert third.vix_exit is None  # no VIX bar at 15:20 — honest NULL
        assert capture.vix_missing_trades == 1

    def test_zero_trade_combo_first_class(self) -> None:
        # A mask bit that never fires → zero trades everywhere; parity holds.
        row = _row(total=0, winning=0, losing=0, net=0)
        object.__setattr__(row, "mask_bits", 0b10)
        capture = capture_combo(
            row,
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=MinuteIndex.from_bars(_BARS),
            stamper=None,
        )
        assert capture.parity_ok is True
        assert capture.trades == ()
        assert capture.vix_missing_trades == 0
        assert len(capture.equity) == 1
        assert capture.equity[0].equity_close_paisa == 10_000_000  # flat base


class TestCaptureParityFail:
    def test_mismatch_returns_loud_empty_capture(self) -> None:
        row = _row(total=99, winning=98)  # deliberately wrong stored counts
        with structlog.testing.capture_logs() as logs:
            capture = capture_combo(
                row,
                bars=_BARS,
                bar_bits=_BAR_BITS,
                tf_minutes=1,
                sl_paisa=_SL,
                tp_paisa=_TP,
                minute_index=MinuteIndex.from_bars(_BARS),
                stamper=None,
            )
        assert capture.parity_ok is False
        assert capture.trades == ()
        assert capture.equity == ()
        assert "total_trades=3!=99" in capture.parity_detail
        assert "winning_trades=2!=98" in capture.parity_detail
        events = [log for log in logs if log["event"] == "results_depth.capture_mismatch"]
        assert len(events) == 1
        assert events[0]["run_id"] == _RUN_ID

    def test_net_pnl_mismatch_alone_fails(self) -> None:
        capture = capture_combo(
            _row(net=999_999),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is False
        assert capture.parity_detail == "net_pnl_paisa=35!=999999"


class TestReentryPinAsFound:
    """O-e baked decision: pin the walker's AS-FOUND intraday re-entry semantics."""

    def test_multi_entry_day_reenters_on_next_fresh_signal(self) -> None:
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        # Three entries the SAME session — after each exit the next fresh
        # signal re-enters at the following bar's open with NO cooldown and
        # NO same-bar re-entry (each entry is a later bar than the prior exit),
        # up to the 15:20 forced wall.
        assert len(capture.trades) == 3
        for earlier, later in zip(capture.trades, capture.trades[1:], strict=False):
            assert later.entry_ts > earlier.exit_ts
        assert capture.trades[-1].exit_reason == "forced_1520"
        assert all(
            t.entry_ts.astimezone(IST).date() == t.exit_ts.astimezone(IST).date()
            for t in capture.trades
        )


class TestBelts:
    def test_derived_exit_fill_buy_sell(self) -> None:
        assert derived_exit_fill(Side.BUY, 10_000, 50) == 10_050
        assert derived_exit_fill(Side.SELL, 10_000, 50) == 9_950

    def test_derived_exit_fill_rejects_hold(self) -> None:
        with pytest.raises(TradeCaptureError, match="BUY or SELL"):
            derived_exit_fill(Side.HOLD, 10_000, 50)

    def test_alignment_belt(self) -> None:
        _check_detail_alignment(2, 2)  # no raise
        with pytest.raises(TradeCaptureError, match="per-close append invariant"):
            _check_detail_alignment(2, 1)

    def test_exit_fill_belt_pass_and_fail(self) -> None:
        trade = ClosedTrade(
            pnl_paisa=50,
            mfe_paisa=60,
            mae_paisa=10,
            holding_bars=2,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 17),
            direction=Side.BUY,
        )
        good = TradeDetail(
            trade=trade, exit_reason="take_profit", entry_fill_paisa=10_000, exit_fill_paisa=10_050
        )
        _check_exit_fill_belt(good, Side.BUY)  # no raise
        bad = TradeDetail(
            trade=trade, exit_reason="take_profit", entry_fill_paisa=10_000, exit_fill_paisa=10_049
        )
        with pytest.raises(TradeCaptureError, match="exit-fill belt failed"):
            _check_exit_fill_belt(bad, Side.BUY)


class TestCapturedTradeValidation:
    @staticmethod
    def _kwargs() -> dict[str, object]:
        return {
            "entry_ts": _ts(9, 16),
            "entry_px": Decimal("100.00"),
            "exit_ts": _ts(9, 17),
            "exit_px": Decimal("100.50"),
            "direction": Side.BUY,
            "lots": None,
            "exit_reason": "take_profit",
            "pnl_paisa": 50,
            "costs_paisa": None,
            "mae_paisa": 10,
            "mfe_paisa": 60,
            "mae_1m_paisa": 10,
            "mfe_1m_paisa": 60,
            "path_1m_status": PathStatus.OK,
            "vix_entry": None,
            "vix_exit": None,
        }

    def test_valid_construction(self) -> None:
        trade = CapturedTrade(**self._kwargs())  # type: ignore[arg-type]
        assert trade.exit_reason == "take_profit"
        assert trade.lots is None  # NULL-by-design until P5 (Ruling 18)
        assert trade.costs_paisa is None

    def test_future_real_execution_leg_values_stay_constructible(self) -> None:
        # P5's translation sidecar will carry REAL lots/costs — the container
        # accepts genuine values while refusing fabricated sub-1 lots.
        kwargs = self._kwargs()
        kwargs["lots"] = 2
        kwargs["costs_paisa"] = 0
        trade = CapturedTrade(**kwargs)  # type: ignore[arg-type]
        assert (trade.lots, trade.costs_paisa) == (2, 0)

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("direction", Side.HOLD, "BUY or SELL"),
            ("exit_reason", "vibes", "is not one of"),
            ("lots", 0, "lots must be None or >= 1"),
            ("costs_paisa", -1, "costs_paisa must be None or >= 0"),
            ("mae_paisa", -1, "must be >= 0"),
            ("mfe_paisa", -5, "must be >= 0"),
            ("mae_1m_paisa", None, "None exactly when"),
            ("mfe_1m_paisa", None, "None exactly when"),
            ("mae_1m_paisa", -1, "1m excursions are magnitudes"),
            ("mfe_1m_paisa", -1, "1m excursions are magnitudes"),
        ],
    )
    def test_invalid_field_rejected(self, field: str, value: object, match: str) -> None:
        kwargs = self._kwargs()
        kwargs[field] = value
        with pytest.raises(TradeCaptureError, match=match):
            CapturedTrade(**kwargs)  # type: ignore[arg-type]

    def test_no_1m_data_requires_none_pair(self) -> None:
        kwargs = self._kwargs()
        kwargs["path_1m_status"] = PathStatus.NO_1M_DATA  # values still set → reject
        with pytest.raises(TradeCaptureError, match="None exactly when"):
            CapturedTrade(**kwargs)  # type: ignore[arg-type]

    def test_no_1m_data_with_none_pair_ok(self) -> None:
        kwargs = self._kwargs()
        kwargs.update(
            {"path_1m_status": PathStatus.NO_1M_DATA, "mae_1m_paisa": None, "mfe_1m_paisa": None}
        )
        trade = CapturedTrade(**kwargs)  # type: ignore[arg-type]
        assert trade.path_1m_status is PathStatus.NO_1M_DATA


class TestCaptureOverRealSweepStore:
    """Integration tooth: capture EVERY finalist of a real ``run_persisted_sweep``.

    The stored rows come off the production sweep spine (the Rust kernel arm
    where wheels are installed); the captures come off the pure-Python oracle
    — so a green parity tooth here IS the kernel⇄oracle correctness bridge the
    design leans on (F.3), exercised end-to-end against a real store.
    """

    def test_every_persisted_finalist_captures_with_parity(self, tmp_path: Path) -> None:
        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.results_store import ResultsStore
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
        from brutex.orchestrator.audit import AuditWriter

        bars = _drift_bars()
        cfg = SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=[0, 1, 2, 3, 4],
            k=1,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )
        store = ResultsStore(path=tmp_path / "store.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter(path=tmp_path / "audit.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
        assert summary.persisted > 0
        rows = ResultsStore.load(tmp_path / "store.parquet").rows()
        assert len(rows) == summary.persisted
        bar_bits = build_bar_bits(
            bars, cfg.proximity, prior_n=cfg.prior_n, fib_anchor=cfg.fib_anchor
        )
        index = MinuteIndex.from_bars(bars)
        for row in rows:
            capture = capture_combo(
                row,
                bars=bars,
                bar_bits=bar_bits,
                tf_minutes=cfg.tf_minutes,
                sl_paisa=cfg.sl_paisa,
                tp_paisa=cfg.tp_paisa,
                minute_index=index,
                stamper=None,
            )
            assert capture.parity_ok is True, capture.parity_detail
            assert len(capture.trades) == row.total_trades
            assert sum(t.pnl_paisa for t in capture.trades) == row.net_pnl_paisa
            assert all(t.path_1m_status is PathStatus.OK for t in capture.trades)


def _drift_bars() -> list[CandleBar]:
    """The sweep golden's 12-day upward-drift fixture shape (deterministic)."""
    from datetime import timedelta

    out: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out
