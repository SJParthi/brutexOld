"""Deterministic input corpus for the cascade/results-depth Rust parity goldens.

DEC-RUST-CASCADE-RESULTS-001: the T14 lane converts the cascade / results-depth
compute laws to the ``rust/brutex-cascade`` crate under the compressed
greenfield cycle (convert → verify vs FROZEN captured goldens → default-ON →
delete the converted Python). This module is the ONE builder of the parity
corpus INPUTS — shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixtures frozen), and
* the replay golden (``test_cascade_rust_parity_golden.py`` — drives the
  post-conversion Rust-backed API against the frozen fixtures),

so the two sides can never drift on what was fed in. Inputs are built in CODE
(never parsed from the fixture) because several hostile cases carry values JSON
cannot round-trip faithfully (``float('nan')``, bool-vs-int puns, tuples).

Every case is fully deterministic: fixed dates, fixed prices, no RNG, no
wall-clock, no I/O.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.strategies.types import Side

__all__ = [
    "aggregate_cases",
    "cascade_join_payloads",
    "drawdown_cases",
    "equity_cases",
    "excursion_cases",
    "float_stats_cases",
    "make_bar",
    "nearest_rank_cases",
    "session_dates_cases",
]

_DAY = date(2024, 1, 2)
_DAY2 = date(2024, 1, 3)
_DAY3 = date(2024, 1, 4)


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    """IST wall-clock instant expressed in UTC (the lake's storage law)."""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def make_bar(
    hh: int,
    mm: int,
    *,
    day: date = _DAY,
    o: str = "100.00",
    h: str = "101.00",
    lo: str = "99.00",
    c: str = "100.50",
    volume: int = 10,
) -> CandleBar:
    """One deterministic 1-minute bar at IST ``hh:mm`` of *day*."""
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=volume,
        open_interest=None,
    )


# ──────────────────────────── cascade_join ────────────────────────────


def _verdict(
    cid: str,
    *,
    passed: bool,
    rejected_at: object = None,
    detail: object = "d",
    composite: object = 1.5,
    provisional: object = False,
    rate: object = 0.75,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "candidate_id": cid,
        "passed": passed,
        "detail": detail,
        "composite_score": composite,
        "provisional": provisional,
        "cpcv_pass_rate": rate,
    }
    if rejected_at is not None or not passed:
        entry["rejected_at"] = rejected_at
    return entry


def _group(
    instrument: str,
    tf: str,
    verdicts: list[Any],
    survivor_ids: list[Any],
    *,
    n_input: object = None,
    n_survived: object = None,
    pool_pbo: object = 0.25,
    dsr_n_source: object = "pool",
    dsr_n_trials: object = 12,
    dsr_trial_variance: object = 0.5,
) -> dict[str, Any]:
    return {
        "instrument": instrument,
        "tf": tf,
        "report": {
            "verdicts": verdicts,
            "n_input": len(verdicts) if n_input is None else n_input,
            "n_survived": len(survivor_ids) if n_survived is None else n_survived,
            "pool_pbo": pool_pbo,
            "dsr_n_source": dsr_n_source,
            "dsr_n_trials": dsr_n_trials,
            "dsr_trial_variance": dsr_trial_variance,
            "survivor_ids": survivor_ids,
        },
    }


def cascade_join_payloads() -> list[tuple[str, Any]]:
    """The ``build_cascade_join`` corpus: (case_name, payload) pairs."""
    a1 = "a1" * 32
    a2 = "a2" * 32
    b1 = "b1" * 32
    b2 = "b2" * 32
    nan = float("nan")
    cases: list[tuple[str, Any]] = [
        (
            "clean_two_groups",
            {
                "version": 1,
                "sweep_id": "sweep-1",
                "groups": [
                    _group(
                        "NIFTY",
                        "5minute",
                        [
                            _verdict(a1, passed=True),
                            _verdict(a2, passed=False, rejected_at="dsr", rate=0.25),
                        ],
                        [a1],
                    ),
                    _group(
                        "SENSEX",
                        "1minute",
                        [_verdict(b1, passed=True, provisional=True, composite=-0.5)],
                        [b1],
                    ),
                ],
            },
        ),
        (
            "root_counts_present",
            {
                "version": 1,
                "sweep_id": "sweep-2",
                "n_input": 40,
                "n_survived": 3,
                "groups": [_group("NIFTY", "1minute", [_verdict(a1, passed=True)], [a1])],
            },
        ),
        ("root_not_mapping", ["not", "a", "dict"]),
        ("version_bool_pun", {"version": True, "sweep_id": "s", "groups": []}),
        ("version_float", {"version": 1.0, "sweep_id": "s", "groups": []}),
        ("version_future", {"version": 99, "sweep_id": "s", "groups": []}),
        ("sweep_id_missing", {"version": 1, "groups": []}),
        ("sweep_id_empty", {"version": 1, "sweep_id": "", "groups": []}),
        ("groups_not_list", {"version": 1, "sweep_id": "s", "groups": {"a": 1}}),
        (
            "group_junk_mix",
            {
                "version": 1,
                "sweep_id": "sweep-3",
                "groups": [
                    "not-a-dict",
                    {"instrument": "", "tf": "1minute", "report": {}},
                    {"instrument": "NIFTY", "tf": 7, "report": {}},
                    {"instrument": "NIFTY", "tf": "1minute", "report": "junk"},
                    {
                        "instrument": "NIFTY",
                        "tf": "1minute",
                        "report": {"verdicts": "junk", "n_input": 1, "n_survived": 0},
                    },
                    {
                        "instrument": "NIFTY",
                        "tf": "2minute",
                        "report": {"verdicts": [], "n_input": True, "n_survived": 0},
                    },
                    {
                        "instrument": "NIFTY",
                        "tf": "3minute",
                        "report": {"verdicts": [], "n_input": -1, "n_survived": 0},
                    },
                    _group("BANKNIFTY", "3minute", [_verdict(a1, passed=True)], [a1]),
                ],
            },
        ),
        (
            "verdict_junk_mix",
            {
                "version": 1,
                "sweep_id": "sweep-4",
                "groups": [
                    _group(
                        "NIFTY",
                        "5minute",
                        [
                            17,
                            {"candidate_id": "", "passed": True},
                            {"candidate_id": 42, "passed": True},
                            {"candidate_id": a1, "passed": "yes"},
                            _verdict(a2, passed=True, rejected_at="dsr"),
                            _verdict(b1, passed=False, rejected_at=13, detail=9),
                            _verdict(
                                b2,
                                passed=False,
                                rejected_at="cpcv",
                                composite=nan,
                                rate=1.5,
                                provisional="yes",
                            ),
                        ],
                        [],
                    ),
                ],
            },
        ),
        (
            "rate_nan_and_negative",
            {
                "version": 1,
                "sweep_id": "sweep-5",
                "groups": [
                    _group(
                        "NIFTY",
                        "1minute",
                        [
                            _verdict(a1, passed=True, rate=nan),
                            _verdict(a2, passed=True, rate=-0.1, composite=True),
                        ],
                        [a1, a2],
                    ),
                ],
            },
        ),
        (
            "dup_within_group",
            {
                "version": 1,
                "sweep_id": "sweep-6",
                "groups": [
                    _group(
                        "NIFTY",
                        "1minute",
                        [_verdict(a1, passed=True), _verdict(a1, passed=False, rejected_at="x")],
                        [a1],
                    ),
                ],
            },
        ),
        (
            "dup_across_groups",
            {
                "version": 1,
                "sweep_id": "sweep-7",
                "groups": [
                    _group("NIFTY", "1minute", [_verdict(a1, passed=True)], [a1]),
                    _group("SENSEX", "1minute", [_verdict(a1, passed=False, rejected_at="x")], []),
                ],
            },
        ),
        (
            "later_group_dup_sweeps_earlier",
            {
                "version": 1,
                "sweep_id": "sweep-8",
                "groups": [
                    _group("NIFTY", "1minute", [_verdict(a1, passed=True)], [a1]),
                    _group(
                        "SENSEX",
                        "1minute",
                        [_verdict(a1, passed=True), _verdict(a1, passed=True)],
                        [],
                    ),
                ],
            },
        ),
        (
            "survivor_inconsistencies",
            {
                "version": 1,
                "sweep_id": "sweep-9",
                "groups": [
                    _group(
                        "NIFTY",
                        "1minute",
                        [
                            _verdict(a1, passed=True),
                            _verdict(a2, passed=False, rejected_at="dsr"),
                            _verdict(b1, passed=True),
                        ],
                        [a1, a2, "ghost", 42, None],
                    ),
                ],
            },
        ),
        (
            "group_stats_junk_numerics",
            {
                "version": 1,
                "sweep_id": "sweep-10",
                "groups": [
                    _group(
                        "NIFTY",
                        "1minute",
                        [_verdict(a1, passed=True)],
                        [a1],
                        pool_pbo=nan,
                        dsr_n_source=17,
                        dsr_n_trials=True,
                        dsr_trial_variance="junk",
                    ),
                ],
            },
        ),
        (
            "warn_detail_cap_overflow",
            {
                "version": 1,
                "sweep_id": "sweep-11",
                "groups": [
                    _group(
                        "NIFTY",
                        "1minute",
                        [{"candidate_id": f"junk-{i}", "passed": "no"} for i in range(9)],
                        [],
                    ),
                ],
            },
        ),
        ("empty_groups", {"version": 1, "sweep_id": "sweep-12", "groups": []}),
        (
            "version_zero_legacy",
            {
                "version": 0,
                "sweep_id": "sweep-13",
                "groups": [_group("NIFTY", "1minute", [_verdict(a1, passed=True)], [a1])],
            },
        ),
    ]
    return cases


# ──────────────────────────── minute_path ────────────────────────────


def _bars_regular() -> list[CandleBar]:
    """Five contiguous minutes 09:16..09:20 with a spike + a dip."""
    return [
        make_bar(9, 16),
        make_bar(9, 17, h="103.00", lo="98.50", o="100.10", c="102.00"),
        make_bar(9, 18, h="102.50", lo="97.25", o="101.90", c="98.00"),
        make_bar(9, 19, h="100.75", lo="98.00", o="98.10", c="100.00"),
        make_bar(9, 20, h="102.40", lo="96.80", o="100.00", c="101.30"),
    ]


def excursion_cases() -> list[dict[str, Any]]:
    """``trade_excursions_1m`` corpus: bars + trade spans + directions."""
    bars = _bars_regular()
    holey = [bars[0], bars[2], bars[4]]  # endpoint minutes present, interior holes
    return [
        {
            "name": "buy_full_range",
            "bars": bars,
            "entry_ts": _ts(9, 16),
            "exit_ts": _ts(9, 20),
            "entry_fill_paisa": 10_000,
            "direction": Side.BUY,
            "exit_bar_full_range": True,
        },
        {
            "name": "buy_open_only_exit",
            "bars": bars,
            "entry_ts": _ts(9, 16),
            "exit_ts": _ts(9, 20),
            "entry_fill_paisa": 10_000,
            "direction": Side.BUY,
            "exit_bar_full_range": False,
        },
        {
            "name": "sell_full_range",
            "bars": bars,
            "entry_ts": _ts(9, 17),
            "exit_ts": _ts(9, 19),
            "entry_fill_paisa": 10_050,
            "direction": Side.SELL,
            "exit_bar_full_range": True,
        },
        {
            "name": "sell_open_only_exit",
            "bars": bars,
            "entry_ts": _ts(9, 17),
            "exit_ts": _ts(9, 19),
            "entry_fill_paisa": 10_050,
            "direction": Side.SELL,
            "exit_bar_full_range": False,
        },
        {
            "name": "no_1m_data",
            "bars": bars,
            "entry_ts": _ts(11, 0),
            "exit_ts": _ts(11, 5),
            "entry_fill_paisa": 10_000,
            "direction": Side.BUY,
            "exit_bar_full_range": True,
        },
        {
            "name": "partial_missing_entry_minute",
            "bars": bars,
            "entry_ts": _ts(9, 15),
            "exit_ts": _ts(9, 18),
            "entry_fill_paisa": 10_000,
            "direction": Side.BUY,
            "exit_bar_full_range": True,
        },
        {
            "name": "partial_missing_exit_minute",
            "bars": bars,
            "entry_ts": _ts(9, 16),
            "exit_ts": _ts(9, 21),
            "entry_fill_paisa": 10_000,
            "direction": Side.SELL,
            "exit_bar_full_range": True,
        },
        {
            "name": "holey_interior_still_ok",
            "bars": holey,
            "entry_ts": _ts(9, 16),
            "exit_ts": _ts(9, 20),
            "entry_fill_paisa": 9_900,
            "direction": Side.BUY,
            "exit_bar_full_range": True,
        },
        {
            "name": "single_minute_trade_open_only",
            "bars": bars,
            "entry_ts": _ts(9, 18),
            "exit_ts": _ts(9, 18),
            "entry_fill_paisa": 10_190,
            "direction": Side.BUY,
            "exit_bar_full_range": False,
        },
        {
            "name": "zero_excursions_never_negative",
            "bars": [make_bar(9, 16, h="100.00", lo="100.00", o="100.00", c="100.00")],
            "entry_ts": _ts(9, 16),
            "exit_ts": _ts(9, 16),
            "entry_fill_paisa": 10_000,
            "direction": Side.BUY,
            "exit_bar_full_range": True,
        },
    ]


class _EqTrade:
    """Structural EquityTradeLike stand-in (plain attrs, deterministic)."""

    def __init__(
        self,
        entry_ts: datetime,
        exit_ts: datetime,
        direction: Side,
        pnl_paisa: int,
        entry_px: Decimal,
    ) -> None:
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.direction = direction
        self.pnl_paisa = pnl_paisa
        self.entry_px = entry_px


def equity_cases() -> list[dict[str, Any]]:
    """``session_equity_series`` corpus."""
    day1_bars = _bars_regular()
    day2_bars = [
        make_bar(9, 16, day=_DAY2, h="105.00", lo="94.00"),
        make_bar(9, 17, day=_DAY2, h="99.00", lo="90.00", o="98.00", c="91.00"),
    ]
    all_bars = day1_bars + day2_bars
    t_buy = _EqTrade(_ts(9, 16), _ts(9, 18), Side.BUY, -5_000, Decimal("100.00"))
    t_sell = _EqTrade(_ts(9, 19), _ts(9, 20), Side.SELL, 2_500, Decimal("98.10"))
    t_day2 = _EqTrade(
        _ts(9, 16, day=_DAY2), _ts(9, 17, day=_DAY2), Side.BUY, -80_000, Decimal("100.00")
    )
    t_gap_exit = _EqTrade(_ts(10, 0), _ts(10, 5), Side.BUY, -1_000, Decimal("100.00"))
    return [
        {
            "name": "two_sessions_three_trades",
            "bars": all_bars,
            "trades": [t_buy, t_sell, t_day2],
            "sessions": [_DAY, _DAY2],
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "no_index_close_only",
            "bars": None,
            "trades": [t_buy, t_sell],
            "sessions": [_DAY, _DAY2],
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "empty_index_close_only",
            "bars": [],
            "trades": [t_buy],
            "sessions": [_DAY],
            "start_equity_paisa": 500_000,
        },
        {
            "name": "partial_session_without_bars",
            "bars": day1_bars,
            "trades": [t_buy, t_day2],
            "sessions": [_DAY, _DAY2],
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "zero_trades_flat_series",
            "bars": all_bars,
            "trades": [],
            "sessions": [_DAY, _DAY2],
            "start_equity_paisa": 750_000,
        },
        {
            "name": "exit_minute_absent_end_of_session_flush",
            "bars": day1_bars,
            "trades": [t_gap_exit],
            "sessions": [_DAY],
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "trailing_empty_session",
            "bars": day1_bars,
            "trades": [t_buy],
            "sessions": [_DAY, _DAY2, _DAY3],
            "start_equity_paisa": 1_000_000,
        },
    ]


def drawdown_cases() -> list[dict[str, Any]]:
    """``minute_path_max_drawdown`` corpus: (points, start_equity)."""
    return [
        {
            "name": "independent_pct_vs_paisa_maxima",
            "points": [
                (_DAY, 100, 40, 1),
                (_DAY2, 1_000, 900, 1),
                (_DAY3, 1_000, 850, 0),
            ],
            "start_equity_paisa": 100,
        },
        {
            "name": "none_lows_close_only",
            "points": [(_DAY, 900, None, 1), (_DAY2, 1_200, None, 0), (_DAY3, 700, None, 2)],
            "start_equity_paisa": 1_000,
        },
        {
            "name": "no_start_equity_peak_seeds_from_first_close",
            "points": [(_DAY, 500, 450, 0), (_DAY2, 800, 300, 1)],
            "start_equity_paisa": None,
        },
        {
            "name": "empty_points",
            "points": [],
            "start_equity_paisa": 1_000,
        },
        {
            "name": "nonpositive_peak_pct_zero",
            "points": [(_DAY, -50, -80, 1), (_DAY2, -10, -200, 1)],
            "start_equity_paisa": None,
        },
        {
            "name": "monotonic_up_no_drawdown",
            "points": [(_DAY, 1_100, 1_050, 0), (_DAY2, 1_200, 1_150, 0)],
            "start_equity_paisa": 1_000,
        },
    ]


def session_dates_cases() -> list[dict[str, Any]]:
    """``ist_session_dates`` corpus (incl. the IST-midnight-crossing edge)."""
    return [
        {"name": "two_days_shuffle_dedup", "bars": [*_bars_regular(), make_bar(9, 16, day=_DAY2)]},
        {"name": "empty", "bars": []},
        {
            "name": "utc_date_differs_from_ist_date",
            # 2024-01-02 22:30 UTC == 2024-01-03 04:00 IST — the IST date is day2.
            "bars": [
                CandleBar(
                    timestamp=datetime(2024, 1, 2, 22, 30, tzinfo=UTC),
                    open=Decimal("1.00"),
                    high=Decimal("1.00"),
                    low=Decimal("1.00"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                )
            ],
        },
    ]


# ──────────────────────────── combo_aggregates ────────────────────────────


def nearest_rank_cases() -> list[dict[str, Any]]:
    return [
        {"name": "n1_p95", "values": [7], "pct": 95},
        {"name": "n20_p95", "values": list(range(1, 21)), "pct": 95},
        {"name": "n20_p10", "values": list(range(1, 21)), "pct": 10},
        {"name": "n20_p90", "values": list(range(1, 21)), "pct": 90},
        {"name": "p100_max", "values": [3, 9, 27], "pct": 100},
        {"name": "p1_min", "values": [3, 9, 27], "pct": 1},
        {"name": "floats", "values": [1.25, 2.5, 3.75, 5.0], "pct": 50},
        {"name": "n3_p33_boundary", "values": [10, 20, 30], "pct": 33},
        {"name": "n3_p34_boundary", "values": [10, 20, 30], "pct": 34},
    ]


def float_stats_cases() -> list[dict[str, Any]]:
    return [
        {"name": "ulp_clamp_three_615", "values": [6.15, 6.15, 6.15]},
        {"name": "ulp_clamp_six_615", "values": [6.15] * 6},
        {"name": "plain_spread", "values": [11.0, 12.5, 19.75]},
        {"name": "single", "values": [14.2]},
    ]


def _captured_trade(
    day: date,
    slot: int,
    pnl: int,
    *,
    mae: int = 10,
    mfe: int = 20,
    reason: str = "signal",
    vix_entry: str | None = None,
    status: str = "no_1m_data",
    mae_1m: int | None = None,
    mfe_1m: int | None = None,
    hour: int = 9,
    direction: Side = Side.BUY,
) -> Any:
    """One deterministic CapturedTrade (lazy import — trade_capture is heavy)."""
    from brutex.engine.minute_path import PathStatus
    from brutex.engine.trade_capture import CapturedTrade

    entry = _ts(hour, 16 + slot * 3, day=day)
    return CapturedTrade(
        entry_ts=entry,
        entry_px=Decimal("100.00"),
        exit_ts=entry + timedelta(minutes=1),
        exit_px=Decimal("100.00"),
        direction=direction,
        lots=None,
        exit_reason=reason,
        pnl_paisa=pnl,
        costs_paisa=None,
        mae_paisa=mae,
        mfe_paisa=mfe,
        mae_1m_paisa=mae_1m,
        mfe_1m_paisa=mfe_1m,
        path_1m_status=PathStatus(status),
        vix_entry=None if vix_entry is None else Decimal(vix_entry),
        vix_exit=None if vix_entry is None else Decimal(vix_entry),
    )


def aggregate_cases() -> list[dict[str, Any]]:
    """``aggregate_combo`` corpus: full capture + row + lens shapes."""
    from brutex.engine.minute_path import PathStatus, SessionEquityPoint
    from brutex.engine.results_store import ResultRow
    from brutex.engine.session_lens import SessionLens, TrendRegime
    from brutex.engine.trade_capture import ComboCapture

    run = "ab" * 32
    fp = "fe" * 32

    def row(
        pnls: list[int],
        *,
        max_dd: int = 0,
        run_id: str = run,
    ) -> ResultRow:
        return ResultRow(
            run_id=run_id,
            instrument="NIFTY",
            tf="1minute",
            direction="bull",
            mode="A",
            mask_bits=0b1,
            net_pnl_paisa=sum(pnls),
            gross_profit_paisa=sum(p for p in pnls if p > 0),
            gross_loss_paisa=-sum(p for p in pnls if p < 0),
            max_drawdown_paisa=max_dd,
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

    def capture(
        trades: tuple[Any, ...],
        *,
        equity: tuple[Any, ...] = (),
        equity_status: str = "no_1m_data",
        vix_missing: int = 0,
        parity_ok: bool = True,
    ) -> ComboCapture:
        return ComboCapture(
            run_id=run,
            trades=trades,
            equity=equity,
            equity_status=PathStatus(equity_status),
            vix_missing_trades=vix_missing,
            parity_ok=parity_ok,
            parity_detail="" if parity_ok else "total_trades=0!=3",
        )

    lens_trend = SessionLens(
        trend_by_date={
            _DAY: TrendRegime.UP,
            _DAY2: TrendRegime.DOWN,
            _DAY3: TrendRegime.SIDEWAYS,
        },
        event_flags_by_date={
            _DAY: ("expiry", "muhurat"),
            _DAY3: ("nifty_rebalance",),
        },
    )
    empty_lens = SessionLens(trend_by_date={}, event_flags_by_date={})

    rich_trades = (
        _captured_trade(_DAY, 0, 5_000, mae=30, mfe=90, vix_entry="12.5", reason="take_profit"),
        _captured_trade(
            _DAY,
            1,
            -2_000,
            mae=55,
            mfe=10,
            vix_entry="18.25",
            reason="stop_loss",
            hour=10,
            direction=Side.SELL,
        ),
        _captured_trade(_DAY2, 0, 0, mae=5, mfe=5, reason="signal", hour=11),
        _captured_trade(_DAY2, 1, 7_500, mae=12, mfe=200, vix_entry="12.5", reason="forced_1520"),
        _captured_trade(
            _DAY3, 0, -4_000, mae=80, mfe=2, vix_entry="31.0", reason="end_of_data", hour=13
        ),
        _captured_trade(_DAY3, 1, 1_500, mae=8, mfe=44, vix_entry="6.15", reason="session_gap"),
    )
    full_1m_trades = tuple(
        _captured_trade(
            _DAY,
            slot,
            pnl,
            status="ok",
            mae_1m=mae1,
            mfe_1m=mfe1,
            vix_entry=vix,
        )
        for slot, (pnl, mae1, mfe1, vix) in enumerate(
            [(1_000, 40, 60, "12.5"), (-500, 90, 15, "12.5"), (2_000, 10, 130, "6.15")]
        )
    )
    partial_1m_trades = (
        _captured_trade(_DAY, 0, 1_000, status="ok", mae_1m=40, mfe_1m=60),
        _captured_trade(_DAY, 1, -500, status="partial", mae_1m=90, mfe_1m=15),
        _captured_trade(_DAY, 2, 250, status="no_1m_data"),
    )
    equity_points = (
        SessionEquityPoint(
            session_date=_DAY,
            equity_close_paisa=1_003_000,
            equity_intraday_low_paisa=996_500,
            trades_closed=2,
        ),
        SessionEquityPoint(
            session_date=_DAY2,
            equity_close_paisa=1_010_500,
            equity_intraday_low_paisa=None,
            trades_closed=2,
        ),
    )
    year_wonder_trades = (
        _captured_trade(date(2023, 1, 3), 0, 90_000, vix_entry="14.0"),
        _captured_trade(date(2024, 1, 2), 0, 5_000),
        _captured_trade(date(2024, 1, 3), 0, -1_000),
    )
    trend_wonder_trades = (
        _captured_trade(_DAY, 0, 90_000),
        _captured_trade(_DAY2, 0, -1_000),
        _captured_trade(_DAY3, 0, 11_000),
        _captured_trade(date(2024, 1, 5), 0, 500),  # unlabelled → unknown bucket
    )
    return [
        {
            "name": "rich_mixed",
            "capture": capture(rich_trades, equity=equity_points, equity_status="partial"),
            "row": row([5_000, -2_000, 0, 7_500, -4_000, 1_500], max_dd=9_000),
            "lens": lens_trend,
            "composite_score": 1.25,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "full_1m_coverage",
            "capture": capture(full_1m_trades, equity=equity_points, equity_status="ok"),
            "row": row([1_000, -500, 2_000], max_dd=700),
            "lens": lens_trend,
            "composite_score": None,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "partial_1m_nulls_1m_stats",
            "capture": capture(partial_1m_trades),
            "row": row([1_000, -500, 250]),
            "lens": empty_lens,
            "composite_score": 0.0,
            "fingerprint": fp,
            "start_equity_paisa": 500_000,
        },
        {
            "name": "zero_trade_null_record",
            "capture": capture(()),
            "row": row([]),
            "lens": lens_trend,
            "composite_score": None,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "parity_failed_null_record",
            "capture": capture((), parity_ok=False),
            "row": row([1_000, 2_000, 3_000]),
            "lens": lens_trend,
            "composite_score": 2.5,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "year_wonder",
            "capture": capture(year_wonder_trades, vix_missing=2),
            "row": row([90_000, 5_000, -1_000], max_dd=1_000),
            "lens": empty_lens,
            "composite_score": None,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "trend_wonder_unknown_excluded",
            "capture": capture(trend_wonder_trades),
            "row": row([90_000, -1_000, 11_000, 500], max_dd=2_500),
            "lens": lens_trend,
            "composite_score": None,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
        {
            "name": "all_losses_no_ratio",
            "capture": capture(
                (
                    _captured_trade(_DAY, 0, -1_000, vix_entry="22.0"),
                    _captured_trade(_DAY, 1, -2_000, vix_entry="26.5"),
                )
            ),
            "row": row([-1_000, -2_000], max_dd=3_000),
            "lens": lens_trend,
            "composite_score": None,
            "fingerprint": fp,
            "start_equity_paisa": 1_000_000,
        },
    ]
