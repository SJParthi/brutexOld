"""Unit tests for walk-forward mode edges (DEC-WALK-FORWARD-OOS-001).

Covers the guard rails that the property/golden/integration suites do not reach by
construction: the empty-bars + invalid-window refusals of
:func:`~brutex.engine.stage1a.build_walk_forward_schedule`, the
:class:`~brutex.feed.lookahead.LookAheadBarrier` actually *tripping* on a
deliberately leaky hand-built schedule (proving the guard bites, not just decorates),
and the defensive ``LookAheadError`` re-raise in the two sweep-driver entry points.
These close the strict 100% line+branch ``brutex.engine`` gate for the new code.

Named ``test_engine_sweep_driver_walk_forward.py`` (was
``test_engine_walk_forward.py``) so the auto-discovery coverage gate
(``scripts/ci/coverage_gate_discovery.py``) stem-maps it to
``brutex/engine/sweep_driver.py`` — the module hosting the walk-forward entry
points this file covers (``evaluate_combo_walk_forward`` / ``walk_forward_sweep``
/ ``verify_walk_forward_no_lookahead`` / ``_assert_trades_confined`` /
``_aggregate_folds``). Without the stem match the discovery gate saw only
``test_engine_sweep_driver*.py``'s other files and read sweep_driver.py at ~83%
standalone, failing every sweep_driver-touching PR (DEC-CORP-ACTION-GATE-001
CI fix). The stage1a schedule helpers it also exercises stay covered via the
static engine gate (all ``tests/unit/test_engine_*.py`` at 100%) + the
whole-package floor — stage1a.py remains on ``COVERAGE_DISCOVERY_EXCLUDE``
(DEC-COVERAGE-DISCOVERY-STAGE1A-001).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import pytest
from brutex.data.models import CandleBar
from brutex.engine import sweep_driver
from brutex.engine.stage1a import (
    WalkForwardFold,
    WalkForwardSchedule,
    build_walk_forward_schedule,
)
from brutex.engine.sweep_driver import (
    FoldResult,
    WalkForwardResult,
    evaluate_combo_walk_forward,
    verify_walk_forward_no_lookahead,
    walk_forward_sweep,
)
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadReport
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.strategies.combo import canonicalise_params
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

#: A self-contradictory mask (all 52 bits) never fires → deterministic no-trade.
_IMPOSSIBLE_MASK = (1 << 52) - 1

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int) -> CandleBar:
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _stream(n_days: int, bars_per_day: int = 2) -> list[CandleBar]:
    return [_bar(d, m) for d in range(n_days) for m in range(bars_per_day)]


def _schedule_of(*folds: WalkForwardFold, n_bars: int = 64) -> WalkForwardSchedule:
    """Wrap hand-built folds in a minimal schedule (window params are immaterial)."""
    return WalkForwardSchedule(
        folds=folds,
        train_days=5,
        test_days=5,
        step_days=5,
        n_trading_days=max(1, len(folds)),
        n_bars=n_bars,
        schedule_hash="0" * 64,
    )


def _fold(*, train_end_bar: int, test_start_bar: int, idx: int = 0) -> WalkForwardFold:
    """A fold pinning only the two bar bounds the look-ahead guard reads."""
    return WalkForwardFold(
        fold_index=idx,
        train_start_day=0,
        train_end_day=5,
        test_start_day=4,
        test_end_day=9,
        train_start_bar=0,
        train_end_bar=train_end_bar,
        test_start_bar=test_start_bar,
        test_end_bar=test_start_bar + 10,
    )


def _leaky_schedule() -> WalkForwardSchedule:
    """A hand-built schedule that leaks by EXACTLY ONE bar.

    ``train_end_bar = 9 > test_start_bar = 8`` — a 1-bar leak, the tightest case a
    well-formed schedule can never produce. The 1-bar gap (not 2) is deliberate:
    it pins the barrier cursor boundary so a ``test_start_bar - 1`` → ``test_start_bar``
    off-by-one mutant in the guard is killed (a 2-bar leak would let it survive).
    """
    return _schedule_of(_fold(train_end_bar=9, test_start_bar=8), n_bars=18)


# ── build_walk_forward_schedule guards ──


def test_empty_bars_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        build_walk_forward_schedule([])


def test_invalid_window_raises() -> None:
    # train_days < 1 is rejected by the shared window validator.
    with pytest.raises(ValueError, match="train_size"):
        build_walk_forward_schedule(_stream(20), train_days=0, test_days=5, step_days=5)


def test_too_few_days_raises() -> None:
    with pytest.raises(ValueError, match="too short"):
        build_walk_forward_schedule(_stream(6), train_days=10, test_days=5, step_days=5)


# ── the barrier actually trips on a leak ──


def test_verify_detects_leaky_schedule() -> None:
    report = verify_walk_forward_no_lookahead(_leaky_schedule())
    assert report.passed is False
    assert report.violation is not None
    assert "look-ahead" in report.violation


# ── defensive re-raise in the two entry points ──


def test_evaluate_combo_reraises_on_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = LookAheadReport(
        passed=False,
        indices_checked=1,
        max_index_touched=9,
        final_cursor=7,
        violation="boom",
    )
    monkeypatch.setattr(sweep_driver, "verify_walk_forward_no_lookahead", lambda schedule: failing)
    with pytest.raises(LookAheadError, match="P-06 re-check"):
        evaluate_combo_walk_forward(
            _stream(20),
            0,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=_stream(20),
            train_days=10,
            test_days=5,
            step_days=5,
        )


def test_walk_forward_sweep_reraises_on_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = LookAheadReport(
        passed=False,
        indices_checked=1,
        max_index_touched=9,
        final_cursor=7,
        violation="boom",
    )
    monkeypatch.setattr(sweep_driver, "verify_walk_forward_no_lookahead", lambda schedule: failing)
    with pytest.raises(LookAheadError, match="P-06 re-check"):
        walk_forward_sweep(
            _stream(20),
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=_stream(20),
            train_days=10,
            test_days=5,
            step_days=5,
        )


# ── malformed schedules → passed=False, NOT an uncaught ValueError ──


def test_verify_reports_negative_test_start_as_violation() -> None:
    # test_start_bar == 0 → cursor would be -1; the guard maps it to passed=False
    # rather than letting LookAheadBarrier.advance_to raise a bare ValueError.
    bad = _schedule_of(_fold(train_end_bar=0, test_start_bar=0))
    report = verify_walk_forward_no_lookahead(bad)
    assert report.passed is False
    assert report.violation is not None
    assert "malformed" in report.violation


def test_verify_reports_backward_folds_as_violation() -> None:
    # Two folds whose test_start_bar decreases (a backward cursor move) is
    # malformed → passed=False, not a ValueError traceback.
    bad = _schedule_of(
        _fold(train_end_bar=20, test_start_bar=20, idx=0),
        _fold(train_end_bar=10, test_start_bar=10, idx=1),
    )
    report = verify_walk_forward_no_lookahead(bad)
    assert report.passed is False
    assert report.violation is not None
    assert "malformed" in report.violation


def test_verify_passes_well_formed_contiguous_schedule() -> None:
    good = _schedule_of(
        _fold(train_end_bar=10, test_start_bar=10, idx=0),
        _fold(train_end_bar=20, test_start_bar=20, idx=1),
    )
    report = verify_walk_forward_no_lookahead(good)
    assert report.passed is True
    assert report.violation is None
    assert report.indices_checked == 2


# ── the P-06 trade-confinement guard (pillar 2b) ──


def _trade(entry: datetime, exit_: datetime) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=50,
        mfe_paisa=50,
        mae_paisa=0,
        holding_bars=1,
        entry_ts=entry,
        exit_ts=exit_,
        direction=Side.BUY,
    )


def test_trades_confined_happy_path() -> None:
    bars = _stream(1, bars_per_day=4)  # 4 bars on one IST day
    # one trade entering bar 0, exiting bar 2 — fully inside the window
    trades = [_trade(bars[0].timestamp, bars[2].timestamp)]
    sweep_driver._assert_trades_confined(bars, trades)  # no raise


def test_trades_confined_rejects_out_of_window_trade() -> None:
    bars = _stream(1, bars_per_day=4)
    outside = _bar(99, 0)  # a bar whose timestamp is not in `bars`
    trades = [_trade(bars[0].timestamp, outside.timestamp)]
    with pytest.raises(LookAheadError, match="escapes its test window"):
        sweep_driver._assert_trades_confined(bars, trades)


def test_trades_confined_rejects_unordered_trades() -> None:
    bars = _stream(1, bars_per_day=6)
    # second trade exits BEFORE the first → not chronologically ordered by exit
    trades = [
        _trade(bars[0].timestamp, bars[4].timestamp),
        _trade(bars[1].timestamp, bars[2].timestamp),
    ]
    with pytest.raises(LookAheadError, match="not chronologically ordered"):
        sweep_driver._assert_trades_confined(bars, trades)


# ── _aggregate_folds: the aggregation layer (pass-rate + Sharpe summary) ──


def _metrics(sharpe: float | None) -> ResultMetrics:
    """A neutral ResultMetrics double varying only ``sharpe_ratio``."""
    return ResultMetrics(
        net_pnl_paisa=0,
        total_return_pct=0.0,
        cagr_pct=0.0,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        sharpe_ratio=sharpe,
        sortino_ratio=None,
        calmar_ratio=None,
        mar_ratio=None,
        information_ratio=None,
        omega_ratio=None,
        max_drawdown_pct=0.0,
        max_drawdown_paisa=0,
        max_drawdown_duration_bars=0,
        recovery_factor=None,
        ulcer_index=0.0,
        largest_losing_trade_paisa=0,
        max_consecutive_losses=0,
        max_consecutive_wins=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        loss_rate=0.0,
        profit_factor=None,
        avg_winning_trade_paisa=0,
        avg_losing_trade_paisa=0,
        win_loss_ratio=None,
        expectancy_paisa=0,
        avg_mfe_paisa=0,
        avg_mae_paisa=0,
        avg_holding_time_bars=0.0,
        max_holding_time_bars=0,
        time_in_market_pct=0.0,
        tail_ratio=None,
        skewness=None,
        kurtosis=None,
        returns_std_dev=None,
        kelly_fraction=None,
        log_growth_at_half_kelly=None,
        deflated_sharpe_ratio=None,
        pbo_probability=None,
    )


def _fold_result(idx: int, *, sharpe: float | None, passed: bool) -> FoldResult:
    return FoldResult(
        fold_index=idx,
        train_metrics=_metrics(0.0),
        test_metrics=_metrics(sharpe),
        test_gates_held=passed,
    )


def _n_fold_schedule(n: int) -> WalkForwardSchedule:
    return _schedule_of(*[_fold(train_end_bar=10, test_start_bar=10, idx=i) for i in range(n)])


def test_aggregate_pass_rate_and_distinct_sharpe_summary() -> None:
    # 5 folds, 3 pass → pass_rate 0.6 exactly; distinct Sharpes pin min != mean.
    folds = [
        _fold_result(0, sharpe=2.0, passed=True),
        _fold_result(1, sharpe=1.0, passed=True),
        _fold_result(2, sharpe=-1.0, passed=False),
        _fold_result(3, sharpe=4.0, passed=True),
        _fold_result(4, sharpe=3.0, passed=False),
    ]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(5), folds, "h" * 64)
    assert result.folds_passed == 3
    assert result.pass_rate == 0.6
    # mean = (2+1-1+4+3)/5 = 1.8 (NOT max 4.0 → kills sum→max); min = -1.0 (NOT max)
    assert result.mean_test_sharpe == 1.8
    assert result.min_test_sharpe == -1.0
    assert result.held_up is True  # 0.6 >= 0.6 threshold (boundary)


def test_aggregate_held_up_false_just_below_threshold() -> None:
    # 2 of 5 pass → pass_rate 0.4 < 0.6 → held_up False (kills >= → > only if
    # paired with the boundary test above; together they pin the comparator).
    folds = [
        _fold_result(0, sharpe=1.0, passed=True),
        _fold_result(1, sharpe=1.0, passed=True),
        _fold_result(2, sharpe=1.0, passed=False),
        _fold_result(3, sharpe=1.0, passed=False),
        _fold_result(4, sharpe=1.0, passed=False),
    ]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(5), folds, "h" * 64)
    assert result.pass_rate == 0.4
    assert result.held_up is False
    assert result.pass_rate < sweep_driver.WALK_FORWARD_PASS_RATE_THRESHOLD


def test_aggregate_all_none_sharpe_yields_none_summary() -> None:
    folds = [_fold_result(i, sharpe=None, passed=False) for i in range(3)]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(3), folds, "h" * 64)
    assert result.mean_test_sharpe is None
    assert result.min_test_sharpe is None
    assert result.pass_rate == 0.0
    assert result.held_up is False


# ── end-to-end happy paths (the unit suite is the brutex.engine 100% gate, which
#    runs ONLY tests/unit/test_engine_*.py — so the scoring bodies must be unit-covered) ──


def test_evaluate_combo_walk_forward_end_to_end() -> None:
    # 25 IST days × 2 bars; train=10/test=5/step=5 → 3 folds.
    result = evaluate_combo_walk_forward(
        _stream(25),
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_stream(25),
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert result.n_folds == 3
    assert len(result.fold_results) == 3
    assert 0.0 <= result.pass_rate <= 1.0
    assert isinstance(result.held_up, bool)
    assert len(result.run_hash) == 64
    for i, fr in enumerate(result.fold_results):
        assert fr.fold_index == i
        assert fr.train_metrics.total_trades >= 0
        assert fr.test_metrics.total_trades >= 0


def test_evaluate_combo_walk_forward_with_explicit_proximity_and_sltp() -> None:
    # An explicit (non-None) proximity exercises the run-hash proximity branch;
    # SL/TP cycles trades inside the windows so the trade-confinement guard runs.
    result = evaluate_combo_walk_forward(
        _stream(25),
        required_mask=0,
        direction=Side.SELL,
        tf_minutes=1,
        exec_bars_1m=_stream(25),
        stop_loss_paisa=20,
        take_profit_paisa=20,
        proximity=ProximityParams(),
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert result.n_folds == 3
    assert len(result.run_hash) == 64


def test_walk_forward_sweep_end_to_end_matches_single() -> None:
    bars = _stream(25)
    masks = [0, _IMPOSSIBLE_MASK]
    swept = walk_forward_sweep(
        bars,
        masks,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert [m for m, _ in swept] == masks
    for mask, swept_result in swept:
        single = evaluate_combo_walk_forward(
            bars,
            required_mask=mask,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            train_days=10,
            test_days=5,
            step_days=5,
        )
        assert swept_result.run_hash == single.run_hash
        assert swept_result.fold_results == single.fold_results


def test_walk_forward_roll_schedule_threads_and_rekeys_the_run_hash() -> None:
    # ADR-0012 pass-through (review round-2 finding 2): the WF evaluators now
    # accept roll_schedule/apply_1day_mask; a schedule-aware evaluation is a
    # DIFFERENT computation so its run-hash re-keys, while the defaults stay
    # byte-identical to the pre-launch spot derivation.
    bars = _stream(25)
    covering = (bars[-1].timestamp.astimezone(_IST).date().toordinal(),)
    base = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    explicit_defaults = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
        roll_schedule=None,
        apply_1day_mask=False,
    )
    scheduled = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
        roll_schedule=covering,
    )
    assert explicit_defaults.run_hash == base.run_hash  # spot byte-identity
    assert scheduled.run_hash != base.run_hash  # conditional identity entry
    # walk_forward_sweep threads the SAME params → identical to the single.
    [(_, swept)] = walk_forward_sweep(
        bars,
        [0],
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
        roll_schedule=covering,
    )
    assert swept.run_hash == scheduled.run_hash
    assert swept.fold_results == scheduled.fold_results


@pytest.mark.parametrize("entry", ["single", "sweep"])
def test_walk_forward_schedule_reaches_the_stage1a_build(entry: str) -> None:
    # A schedule ending BEFORE the stream provably reaches Stage-1A: its
    # loud desync raise fires (pre-fix the kwarg did not exist — TypeError).
    bars = _stream(25)
    stale = (bars[0].timestamp.astimezone(_IST).date().toordinal() - 1,)

    def _run() -> object:
        if entry == "single":
            return evaluate_combo_walk_forward(
                bars,
                required_mask=0,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=bars,
                train_days=10,
                test_days=5,
                step_days=5,
                roll_schedule=stale,
            )
        return walk_forward_sweep(
            bars,
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            train_days=10,
            test_days=5,
            step_days=5,
            roll_schedule=stale,
        )

    with pytest.raises(ValueError, match="beyond the last roll_schedule expiry"):
        _run()


def test_no_trade_mask_aggregates_to_zero() -> None:
    result = evaluate_combo_walk_forward(
        _stream(25),
        required_mask=_IMPOSSIBLE_MASK,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_stream(25),
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert result.folds_passed == 0
    assert result.pass_rate == 0.0
    assert result.held_up is False
    assert result.mean_test_sharpe is None


def test_evaluate_and_sweep_empty_bars_raise() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_combo_walk_forward(
            [], required_mask=0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=[]
        )
    with pytest.raises(ValueError, match="non-empty"):
        walk_forward_sweep([], [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=[])


def test_walk_forward_dual_arm_rust_metrics_flag_is_bit_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC-SWEEP-RUST-METRICS-001: the COMMON metrics seam arms walk-forward too.

    ``BRUTEX_SWEEP_RUST_METRICS`` flips the drawdown engine under
    ``metrics_from_trades`` for EVERY consumer — this pins the non-sweep
    consumer: run_hash AND every per-fold train/test metric are equal across
    the two arms (raw ``==`` on full-precision floats, never approx —
    CLAUDE.md §4)."""
    bars = _stream(25)

    def _run() -> WalkForwardResult:
        return evaluate_combo_walk_forward(
            bars,
            0,
            direction=Side.SELL,
            tf_minutes=1,
            exec_bars_1m=bars,
            stop_loss_paisa=20,
            take_profit_paisa=20,
            train_days=10,
            test_days=5,
            step_days=5,
        )

    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "0")
    off = _run()
    monkeypatch.setenv("BRUTEX_SWEEP_RUST_METRICS", "1")
    on = _run()
    assert off.run_hash == on.run_hash
    assert off.pass_rate == on.pass_rate
    assert off.mean_test_sharpe == on.mean_test_sharpe
    assert off.min_test_sharpe == on.min_test_sharpe
    assert len(off.fold_results) == len(on.fold_results)
    for fr_off, fr_on in zip(off.fold_results, on.fold_results, strict=True):
        assert fr_off.train_metrics == fr_on.train_metrics
        assert fr_off.test_metrics == fr_on.test_metrics
        assert fr_off.test_gates_held == fr_on.test_gates_held


# ── _walk_forward_run_hash: guard parity (DEC-RUN-IDENTITY-GUARD-PARITY-001) ──


def _run_hash(*, schedule_hash: str = "cd" * 32, start_equity_paisa: int = 10_000_000) -> str:
    """Call the private run-hash with fixed base fields (whitebox seam)."""
    return sweep_driver._walk_forward_run_hash(
        schedule_hash,
        required_mask=11,
        direction=Side.BUY,
        tf_minutes=5,
        stop_loss_paisa=None,
        take_profit_paisa=None,
        start_equity_paisa=start_equity_paisa,
        proximity=None,
        prior_n=3,
    )


def test_walk_forward_run_hash_rejects_unit_separator_in_schedule_hash() -> None:
    """A ``\\x1f``-carrying schedule_hash could shift the payload's field
    boundary and forge another run identity — rejected via the COMMON
    ``reject_unit_separator`` guard BEFORE any digest byte is produced."""
    with pytest.raises(ValueError, match="schedule_hash"):
        _run_hash(schedule_hash="cd" * 15 + "\x1f" + "cd" * 16)


def test_walk_forward_run_hash_rejects_nan_param() -> None:
    """``allow_nan=False``: a smuggled non-finite float raises instead of
    silently emitting non-canonical ``NaN`` JSON (unreachable for the typed
    int/str/None fields — a defence-in-depth rider)."""
    with pytest.raises(ValueError, match="[Oo]ut of range float"):
        _run_hash(start_equity_paisa=cast(int, float("nan")))


def test_walk_forward_payload_canonicalise_params_equivalence() -> None:
    """Tripwire: the run-hash's raw ``json.dumps`` ≡ ``canonicalise_params``
    over the production value domain (int / str / None).

    Documents WHY the DEC-RUN-IDENTITY-GUARD-PARITY-001 review REJECTED
    routing ``_walk_forward_run_hash`` through the combo canonicaliser: today
    the two encodings are byte-identical for every value this payload can
    carry, but the canonicaliser serialises floats via ``repr()`` into JSON
    *strings* (``{"x":"1.5"}`` vs raw ``{"x":1.5}``), so a swap would bind
    this hash to that future-float convention. If this test ever fails, the
    two conventions have drifted for the CURRENT domain — re-open the
    consolidation decision before touching either encoder."""
    variants: list[dict[str, object]] = [
        {
            "mask": 11,
            "direction": "BUY",
            "tf_minutes": 5,
            "stop_loss_paisa": None,
            "take_profit_paisa": None,
            "start_equity_paisa": 10_000_000,
            "pivot_proximity_bps": 10,
            "pdh_pdl_proximity_bps": 10,
            "fib_proximity_bps": 10,
            "prior_n": 3,
        },
        {
            "mask": (1 << 52) - 1,
            "direction": "SELL",
            "tf_minutes": 15,
            "stop_loss_paisa": 1500,
            "take_profit_paisa": 4500,
            "start_equity_paisa": 1,
            "pivot_proximity_bps": 25,
            "pdh_pdl_proximity_bps": 5,
            "fib_proximity_bps": 50,
            "prior_n": 1,
        },
    ]
    for payload in variants:
        assert canonicalise_params(payload) == json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )


def test_walk_forward_fold_metrics_keep_recency_none() -> None:
    # DEC-METRICS-TRAILING-WINDOW-001: the walk-forward scorer's bars are
    # train/test SLICES, so it deliberately passes NO trailing_anchor (a
    # slice-end anchor would violate the GLOBAL-anchor law) — every fold
    # metric's recency fields stay the honest None.
    result = evaluate_combo_walk_forward(
        _stream(25),
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_stream(25),
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert result.n_folds >= 1  # anti-vacuity
    for fr in result.fold_results:
        for metrics in (fr.train_metrics, fr.test_metrics):
            assert metrics.trailing_sharpe_ratio is None
            assert metrics.trailing_net_pnl_paisa is None
            assert metrics.decay_weighted_net_paisa is None
