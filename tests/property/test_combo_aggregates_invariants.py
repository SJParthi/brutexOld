"""Property invariants for brutex.engine.combo_aggregates (WP2).

Over hypothesis-generated chronological trade lists (the capture contract —
the walker's own output order, pinned):

1. **Conservation** — the per-year AND per-trend P&L buckets each sum exactly
   to the trades' net P&L; exit tallies and the entry-hour histogram each sum
   to ``n_trades``; ``vix_stamped_trades`` equals the stamped count.
2. **Fold ordering** — ``avg ≤ p95 ≤ worst`` for MAE (mirrored for MFE),
   ``min ≤ avg ≤ max`` for the VIX classes, ``band_lo ≤ band_hi`` and both
   inside the winning-VIX [min, max] envelope.
3. **Worst-days shape** — at most 5 entries, ordered most-negative-first
   (ties by date), every entry a real session P&L sum.
4. **Determinism** — aggregating the SAME capture twice yields byte-identical
   records (the pure-function pin).
"""

# pyright: reportCallIssue=false
# Rationale (the DEC-PYRIGHT-UNMASK-001 precedent, test_combo_hash_uniqueness):
# the Hypothesis `@st.composite` strategy `_trade_specs` is called WITHOUT the
# `draw` argument — Hypothesis injects it at runtime; pyright does not model
# the `@composite` signature transform and reports a false "Argument missing
# for parameter draw" at every `@given(specs=_trade_specs())` site. Scoped to
# this file only; `reportCallIssue` stays a hard error everywhere else.
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from brutex.calendars.nse import IST
from brutex.engine.combo_aggregates import ComboAggregate, aggregate_combo
from brutex.engine.minute_path import PathStatus
from brutex.engine.results_store import ResultRow
from brutex.engine.session_lens import SessionLens, TrendRegime
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.strategies.types import Side
from hypothesis import example, given, settings
from hypothesis import strategies as st

_RUN = "ab" * 32
_FP = "fe" * 32
_START_EQUITY = 10_000_000
_BASE_DAY = date(2020, 12, 24)  # day offsets cross the 2020→2021 year boundary


@st.composite
def _trade_specs(draw: st.DrawFn) -> list[dict[str, Any]]:
    n = draw(st.integers(min_value=0, max_value=25))
    specs: list[dict[str, Any]] = []
    for _ in range(n):
        specs.append(
            {
                "pnl": draw(st.integers(min_value=-500, max_value=500)),
                "mae": draw(st.integers(min_value=0, max_value=1_000)),
                "mfe": draw(st.integers(min_value=0, max_value=1_000)),
                "vix": draw(
                    st.one_of(
                        st.none(),
                        st.decimals(
                            min_value=Decimal("5"),
                            max_value=Decimal("60"),
                            allow_nan=False,
                            allow_infinity=False,
                            places=2,
                        ),
                    )
                ),
                "reason": draw(
                    st.sampled_from(
                        [
                            "stop_loss",
                            "take_profit",
                            "signal",
                            "forced_1520",
                            "end_of_data",
                            "session_gap",
                        ]
                    )
                ),
                "day_step": draw(st.integers(min_value=0, max_value=3)),
            }
        )
    return specs


def _build_capture(specs: list[dict[str, Any]]) -> tuple[ComboCapture, ResultRow]:
    trades: list[CapturedTrade] = []
    day = _BASE_DAY
    slot = 0
    for spec in specs:
        if spec["day_step"] > 0:
            day = day + timedelta(days=spec["day_step"])
            slot = 0
        entry = datetime(day.year, day.month, day.day, 9, 16, tzinfo=IST).astimezone(
            UTC
        ) + timedelta(minutes=slot * 3)
        slot += 1
        trades.append(
            CapturedTrade(
                entry_ts=entry,
                entry_px=Decimal("100.00"),
                exit_ts=entry + timedelta(minutes=1),
                exit_px=Decimal("100.00"),
                direction=Side.BUY,
                lots=1,
                exit_reason=spec["reason"],
                pnl_paisa=spec["pnl"],
                costs_paisa=0,
                mae_paisa=spec["mae"],
                mfe_paisa=spec["mfe"],
                mae_1m_paisa=None,
                mfe_1m_paisa=None,
                path_1m_status=PathStatus.NO_1M_DATA,
                vix_entry=spec["vix"],
                vix_exit=spec["vix"],
            )
        )
    pnls = [t.pnl_paisa for t in trades]
    capture = ComboCapture(
        run_id=_RUN,
        trades=tuple(trades),
        equity=(),
        equity_status=PathStatus.NO_1M_DATA,
        vix_missing_trades=sum(1 for t in trades if t.vix_entry is None),
        parity_ok=True,
        parity_detail="",
    )
    row = ResultRow(
        run_id=_RUN,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=0b1,
        net_pnl_paisa=sum(pnls),
        gross_profit_paisa=sum(p for p in pnls if p > 0),
        gross_loss_paisa=-sum(p for p in pnls if p < 0),
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=len(pnls),
        winning_trades=sum(1 for p in pnls if p > 0),
        losing_trades=sum(1 for p in pnls if p < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )
    return capture, row


def _lens_labelling_every_other_day() -> SessionLens:
    trend = {
        _BASE_DAY + timedelta(days=i): (TrendRegime.UP if i % 2 == 0 else TrendRegime.DOWN)
        for i in range(0, 80, 2)  # odd offsets stay unlabelled → "unknown"
    }
    return SessionLens(trend_by_date=trend, event_flags_by_date={})


def _aggregate(capture: ComboCapture, row: ResultRow) -> ComboAggregate:
    return aggregate_combo(
        capture,
        row,
        lens=_lens_labelling_every_other_day(),
        composite_score=None,
        fingerprint=_FP,
        start_equity_paisa=_START_EQUITY,
    )


@given(specs=_trade_specs())
@settings(max_examples=75, deadline=None)
def test_bucket_sums_conserve_net_pnl(specs: list[dict[str, Any]]) -> None:
    capture, row = _build_capture(specs)
    agg = _aggregate(capture, row)
    net = sum(t.pnl_paisa for t in capture.trades)
    if capture.trades:
        assert sum(json.loads(agg.pnl_by_year_json).values()) == net
        assert sum(json.loads(agg.pnl_by_trend_json).values()) == net
    else:
        assert agg.pnl_by_year_json == "{}"
        assert agg.pnl_by_trend_json == "{}"


@given(specs=_trade_specs())
@settings(max_examples=75, deadline=None)
def test_tallies_and_histograms_sum_to_n_trades(specs: list[dict[str, Any]]) -> None:
    capture, row = _build_capture(specs)
    agg = _aggregate(capture, row)
    n = len(capture.trades)
    assert agg.n_trades == n
    exits = (
        agg.exits_stop_loss
        + agg.exits_take_profit
        + agg.exits_signal
        + agg.exits_forced_1520
        + agg.exits_end_of_data
        + agg.exits_session_gap
    )
    assert exits == n
    assert sum(json.loads(agg.entry_hour_hist_json).values()) == (n if n else 0)
    assert agg.vix_stamped_trades == sum(1 for t in capture.trades if t.vix_entry is not None)


def _ulp_vix_specs(pnl: int, n: int) -> list[dict[str, Any]]:
    """N same-class trades sharing one 2-dp VIX print — the float-summation
    mean-outside-bracket regression (``sum([6.15]*3)/3 > 6.15``;
    ``sum([6.15]*6)/6 < 6.15``), pinned as explicit Hypothesis examples so
    the previously-flaky discovery is exercised deterministically every run.
    """
    return [
        {"pnl": pnl, "mae": 1, "mfe": 1, "vix": Decimal("6.15"), "reason": "signal", "day_step": 0}
        for _ in range(n)
    ]


@given(specs=_trade_specs())
@example(specs=_ulp_vix_specs(50, 3))  # pre-fix: vix_win_avg > vix_win_max
@example(specs=_ulp_vix_specs(-50, 6))  # pre-fix: vix_loss_avg < vix_loss_min
@settings(max_examples=75, deadline=None)
def test_fold_ordering_invariants(specs: list[dict[str, Any]]) -> None:
    capture, row = _build_capture(specs)
    agg = _aggregate(capture, row)
    if not capture.trades:
        assert agg.mae_avg_paisa is None
        return
    assert agg.mae_avg_paisa is not None
    assert agg.mae_p95_paisa is not None
    assert agg.mae_worst_paisa is not None
    assert agg.mae_avg_paisa <= agg.mae_worst_paisa
    assert agg.mae_p95_paisa <= agg.mae_worst_paisa
    assert agg.mfe_avg_paisa is not None
    assert agg.mfe_p95_paisa is not None
    assert agg.mfe_best_paisa is not None
    assert agg.mfe_avg_paisa <= agg.mfe_best_paisa
    assert agg.mfe_p95_paisa <= agg.mfe_best_paisa
    if agg.vix_win_min is not None:
        assert agg.vix_win_avg is not None
        assert agg.vix_win_max is not None
        assert agg.vix_win_min <= agg.vix_win_avg <= agg.vix_win_max
        assert agg.vix_band_lo is not None
        assert agg.vix_band_hi is not None
        assert agg.vix_band_lo <= agg.vix_band_hi
        assert agg.vix_win_min <= agg.vix_band_lo
        assert agg.vix_band_hi <= agg.vix_win_max
    if agg.vix_loss_min is not None:
        assert agg.vix_loss_avg is not None
        assert agg.vix_loss_max is not None
        assert agg.vix_loss_min <= agg.vix_loss_avg <= agg.vix_loss_max


@given(specs=_trade_specs())
@settings(max_examples=75, deadline=None)
def test_worst_days_shape(specs: list[dict[str, Any]]) -> None:
    capture, row = _build_capture(specs)
    agg = _aggregate(capture, row)
    worst = json.loads(agg.worst5_days_json)
    assert len(worst) <= 5
    assert worst == sorted(worst, key=lambda entry: (entry[1], entry[0]))
    # Every entry is a REAL session sum recomputed independently.
    by_day: dict[str, int] = {}
    for trade in capture.trades:
        key = trade.exit_ts.astimezone(IST).date().isoformat()
        by_day[key] = by_day.get(key, 0) + trade.pnl_paisa
    for day, pnl in worst:
        assert by_day[day] == pnl


@given(specs=_trade_specs())
@settings(max_examples=50, deadline=None)
def test_aggregate_is_deterministic(specs: list[dict[str, Any]]) -> None:
    capture, row = _build_capture(specs)
    assert _aggregate(capture, row).to_record() == _aggregate(capture, row).to_record()
