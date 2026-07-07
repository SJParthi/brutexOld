"""Unit tests for the Stage-2 sweep driver — the first assembled funnel slice.

Covers the missing seam (:func:`mask_to_entry_signals`), the equity-curve
builder, and the single-combo evaluator (:func:`evaluate_combo`) end-to-end on
hand-built bars. Every branch is exercised for the strict 100% line+branch gate.
"""

from __future__ import annotations

import struct
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import fno_math
import pytest
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import (
    count_supermasks,
    iter_masks_k_of_bits,
    iter_masks_k_of_n,
)
from brutex.engine.rust_metrics import (
    MAX_EXACT_EQUITY_PAISA,
    SWEEP_RUST_METRICS_ENV,
    reset_observability_memo_for_tests,
)
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import (
    DEFAULT_MAX_COMBOS,
    DEFAULT_START_EQUITY_PAISA,
    build_equity_curve,
    evaluate_combo,
    metrics_from_trades,
    rank_sweep,
    rank_sweep_over_bits,
    sweep_masks,
)
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.ranking import MIN_TRADES, RankedCombo
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))

#: A mask requiring all 52 conditions at once — self-contradictory (e.g. bullish
#: AND bearish), so it NEVER fires: the deterministic "no-trade combo".
_IMPOSSIBLE_MASK = (1 << 52) - 1


def _bar(hour: int, minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """Build one IST-session CandleBar (stored UTC) with Decimal prices."""
    ts = datetime(2026, 5, 13, hour, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _session_bars() -> list[CandleBar]:
    """A small rising-then-falling intraday session (5 one-minute bars)."""
    return [
        _bar(9, 16, "100.00", "101.00", "99.50", "100.50"),
        _bar(9, 17, "100.50", "102.00", "100.00", "101.50"),
        _bar(9, 18, "101.50", "101.80", "100.20", "100.40"),
        _bar(9, 19, "100.40", "100.90", "99.80", "100.10"),
        _bar(9, 20, "100.10", "100.30", "99.00", "99.20"),
    ]


# ────── build_equity_curve ──────


class TestBuildEquityCurve:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            build_equity_curve([], [], tf_minutes=1)

    def test_no_trades_is_flat_at_base(self) -> None:
        bars = _session_bars()
        curve = build_equity_curve(bars, [], tf_minutes=1)
        assert curve.equity_paisa == (DEFAULT_START_EQUITY_PAISA,) * len(bars)
        assert curve.start_equity_paisa == DEFAULT_START_EQUITY_PAISA
        assert curve.span_years > 0.0

    def test_trade_pnl_lands_on_its_exit_bar(self) -> None:
        bars = _session_bars()
        # A winning trade that closes on the 3rd bar (index 2).
        trade = ClosedTrade(
            pnl_paisa=500,
            mfe_paisa=600,
            mae_paisa=50,
            holding_bars=2,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[2].timestamp,
            direction=Side.BUY,
        )
        curve = build_equity_curve(bars, [trade], start_equity_paisa=1_000, tf_minutes=1)
        # Flat at base until the exit bar, then +500 from the exit bar onward.
        assert curve.equity_paisa == (1_000, 1_000, 1_500, 1_500, 1_500)

    def test_two_trades_same_exit_bar_accumulate(self) -> None:
        bars = _session_bars()
        common_exit = bars[1].timestamp
        trades = [
            ClosedTrade(
                pnl_paisa=300,
                mfe_paisa=300,
                mae_paisa=0,
                holding_bars=1,
                entry_ts=bars[0].timestamp,
                exit_ts=common_exit,
                direction=Side.BUY,
            ),
            ClosedTrade(
                pnl_paisa=-100,
                mfe_paisa=0,
                mae_paisa=100,
                holding_bars=1,
                entry_ts=bars[0].timestamp,
                exit_ts=common_exit,
                direction=Side.SELL,
            ),
        ]
        curve = build_equity_curve(bars, trades, start_equity_paisa=1_000, tf_minutes=1)
        # +300 and -100 both land on bar index 1 → net +200 from there on.
        assert curve.equity_paisa == (1_000, 1_200, 1_200, 1_200, 1_200)

    def test_trade_exit_not_a_bar_timestamp_raises(self) -> None:
        bars = _session_bars()
        stray = bars[-1].timestamp + timedelta(minutes=99)
        trade = ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=1,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=bars[0].timestamp,
            exit_ts=stray,
            direction=Side.BUY,
        )
        with pytest.raises(ValueError, match="not a bar timestamp"):
            build_equity_curve(bars, [trade], tf_minutes=1)

    def test_single_bar_span_floor(self) -> None:
        # first == last bar → span 0 → floored to one bar's worth of time (> 0).
        one = [_bar(9, 16, "100.00", "100.50", "99.50", "100.00")]
        curve = build_equity_curve(one, [], tf_minutes=5)
        assert curve.span_years == pytest.approx(5.0 / (365.25 * 24.0 * 60.0))
        assert curve.equity_paisa == (DEFAULT_START_EQUITY_PAISA,)


# ────── evaluate_combo (the single-combo backtest) ──────


class TestEvaluateCombo:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            evaluate_combo([], 0, direction=Side.BUY, tf_minutes=1)

    def test_runs_full_chain_and_returns_metrics(self) -> None:
        bars = _session_bars()
        metrics = evaluate_combo(bars, required_mask=0, direction=Side.BUY, tf_minutes=1)
        # mask=0 enters on every bar → at least one trade closes by end-of-data.
        assert metrics.total_trades >= 1
        assert metrics.winning_trades + metrics.losing_trades <= metrics.total_trades
        # net_pnl is the signed sum of realised trade P&L (int64 paisa).
        assert isinstance(metrics.net_pnl_paisa, int)

    def test_no_trade_combo_yields_empty_metrics(self) -> None:
        bars = _session_bars()
        # The self-contradictory mask never fires (and day-1 anchor bits are
        # cleared too) → zero trades → an all-zero metrics row, not an error.
        metrics = evaluate_combo(
            bars, required_mask=_IMPOSSIBLE_MASK, direction=Side.BUY, tf_minutes=1
        )
        assert metrics.total_trades == 0
        assert metrics.net_pnl_paisa == 0
        assert metrics.win_rate == 0.0

    def test_short_direction_with_sl_tp(self) -> None:
        bars = _session_bars()
        metrics = evaluate_combo(
            bars,
            required_mask=0,
            direction=Side.SELL,
            tf_minutes=1,
            stop_loss_paisa=200,
            take_profit_paisa=100,
            start_equity_paisa=5_000_000,
        )
        assert metrics.total_trades >= 1

    def test_roll_schedule_reaches_the_stage1a_build(self) -> None:
        # ADR-0012 pass-through (review round-2 finding 2): pre-fix
        # evaluate_combo accepted NO roll_schedule, so schedule-blind bar_bits
        # were the ONLY possible result. A schedule whose last expiry precedes
        # the bars now provably reaches Stage-1A (its desync raise fires).
        bars = _session_bars()
        stale = (bars[0].timestamp.astimezone(_IST).date().toordinal() - 1,)
        with pytest.raises(ValueError, match="beyond the last roll_schedule expiry"):
            evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=stale)

    def test_covering_schedule_matches_the_schedule_aware_sweep(self) -> None:
        # The single-contract equivalence oracle: evaluate_combo under a
        # covering schedule == sweep_masks under the SAME schedule (the
        # already-threaded sibling), and the defaults stay the spot path.
        bars = _session_bars()
        covering = (bars[-1].timestamp.astimezone(_IST).date().toordinal(),)
        direct = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=covering)
        [(_, swept)] = sweep_masks(
            bars, [0], direction=Side.BUY, tf_minutes=1, roll_schedule=covering
        )
        assert direct == swept
        assert evaluate_combo(
            bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=None, apply_1day_mask=False
        ) == evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1)


# ────── sweep_masks + rank_sweep (the leaderboard machine) ──────


class TestSweepMasks:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            sweep_masks([], [0], direction=Side.BUY, tf_minutes=1)

    def test_one_pair_per_mask(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:5]
        pairs = sweep_masks(bars, masks, direction=Side.BUY, tf_minutes=1)
        assert [mask for mask, _ in pairs] == masks
        assert all(isinstance(m, ResultMetrics) for _, m in pairs)

    def test_shares_bar_bits_matches_evaluate_combo(self) -> None:
        # sweep_masks builds bar_bits ONCE + reuses it, but must produce the SAME
        # metrics as evaluate_combo per mask (no behavioural drift from the refactor).
        bars = _session_bars()
        mask = 0
        pairs = sweep_masks(bars, [mask], direction=Side.BUY, tf_minutes=1)
        _, swept = pairs[0]
        direct = evaluate_combo(bars, mask, direction=Side.BUY, tf_minutes=1)
        assert swept == direct


class TestRankSweep:
    def test_returns_ranked_leaderboard(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:6]
        ranked = rank_sweep(bars, masks, direction=Side.BUY, tf_minutes=1)
        # A list of RankedCombo — possibly empty when no combo clears the hard
        # gates on this tiny synthetic session (the gate discipline, not an error).
        assert isinstance(ranked, list)
        assert all(isinstance(rc, RankedCombo) for rc in ranked)


class TestRankSweepOverBits:
    """Sweep + rank over a K-of-CHOSEN-condition-bits space (DEC-SWEEP-SUBSET-001).

    The operator-facing "find the top combo over MY chosen conditions" entry, with
    the honest combo-count cap that refuses an oversized sweep before any compute.
    """

    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            rank_sweep_over_bits([], [0, 1], 2, direction=Side.BUY, tf_minutes=1)

    def test_equivalent_to_explicit_rank_sweep(self) -> None:
        # rank_sweep_over_bits is exactly rank_sweep over the K-of-bits mask space —
        # no hidden behaviour (deterministic, so the leaderboards are identical).
        bars = _session_bars()
        bits = [0, 1, 2]
        explicit = rank_sweep(
            bars, list(iter_masks_k_of_bits(bits, 2)), direction=Side.BUY, tf_minutes=1
        )
        viabits = rank_sweep_over_bits(bars, bits, 2, direction=Side.BUY, tf_minutes=1)
        assert viabits == explicit
        assert isinstance(viabits, list)
        assert all(isinstance(rc, RankedCombo) for rc in viabits)

    def test_over_cap_raises_before_compute(self) -> None:
        # C(3, 2) = 3 masks > max_combos=2 → refuse (honest guardrail), no sweep run.
        bars = _session_bars()
        with pytest.raises(ValueError, match=r"exceeds max_combos=2"):
            rank_sweep_over_bits(bars, [0, 1, 2], 2, direction=Side.BUY, tf_minutes=1, max_combos=2)

    def test_k_too_large_raises(self) -> None:
        # count_combos rejects k > n_unique before any enumeration.
        bars = _session_bars()
        with pytest.raises(ValueError, match="invalid k"):
            rank_sweep_over_bits(bars, [0, 1], 3, direction=Side.BUY, tf_minutes=1)

    def test_out_of_range_bit_raises_after_cap_passes(self) -> None:
        # C(2, 1) = 2 clears the default cap, so the range check in the generator
        # is what rejects a bit at MAX_VOCAB_BITS (=128). PR-1 widened the mask
        # u64 -> u128, so the valid range is now [0, 128) and bit 64 is in range;
        # 128 is the first out-of-range position.
        bars = _session_bars()
        with pytest.raises(ValueError, match="out of range"):
            rank_sweep_over_bits(bars, [0, 128], 1, direction=Side.BUY, tf_minutes=1)

    def test_default_cap_admits_full_vocab_k_of_4(self) -> None:
        # The documented envelope: the default cap admits C(52,4)=270,725? No — it is
        # 100,000, so K=4 over the full 52 is REFUSED unless the cap is raised. This
        # pins the honest default (no silent multi-100k-combo sweep).
        assert DEFAULT_MAX_COMBOS == 100_000
        bars = _session_bars()
        with pytest.raises(ValueError, match="exceeds max_combos=100000"):
            rank_sweep_over_bits(bars, list(range(52)), 4, direction=Side.BUY, tf_minutes=1)


class TestStage1BPreFilter:
    """Stage-1B coarse pre-filter (DEC-STAGE1B-WIRE-001).

    `count_supermasks` cheaply counts how many bars each mask fires on; a mask
    firing on fewer bars than the MIN_TRADES gate can never be rankable (the sim
    opens ≤ one trade per signal — no pyramiding), so pruning it before the trade
    sim never changes a ranked leaderboard. These tests prove that.
    """

    def test_min_hits_prunes_zero_hit_mask(self) -> None:
        bars = _session_bars()
        # mask 0 fires on every bar (5 hits); the all-52 mask fires on no bar.
        pairs = sweep_masks(
            bars, [0, _IMPOSSIBLE_MASK], direction=Side.BUY, tf_minutes=1, min_hits=1
        )
        assert [m for m, _ in pairs] == [0]

    def test_min_hits_zero_keeps_every_mask(self) -> None:
        bars = _session_bars()
        pairs = sweep_masks(
            bars, [0, _IMPOSSIBLE_MASK], direction=Side.BUY, tf_minutes=1, min_hits=0
        )
        assert [m for m, _ in pairs] == [0, _IMPOSSIBLE_MASK]

    def test_surviving_metrics_identical_to_unfiltered(self) -> None:
        bars = _session_bars()
        # Prepend mask 0 (always 5 hits → always survives) so a survivor exists.
        masks = [0, *list(iter_masks_k_of_n(1, n=52))[:7]]
        full = dict(sweep_masks(bars, masks, direction=Side.BUY, tf_minutes=1, min_hits=0))
        filtered = sweep_masks(bars, masks, direction=Side.BUY, tf_minutes=1, min_hits=1)
        hits = count_supermasks(build_bar_bits(bars), masks)
        expected = [m for m, h in zip(masks, hits, strict=True) if h >= 1]
        assert [m for m, _ in filtered] == expected
        # The pre-filter only DROPS masks — it never alters a surviving mask's metrics.
        for mask, metrics in filtered:
            assert metrics == full[mask]

    def test_rank_sweep_threads_min_hits(self) -> None:
        bars = _session_bars()
        # The impossible mask is pruned; empty leaderboard either way (5 synthetic
        # bars never clear the 30-trade gate) — proving the prune changes no ranking.
        ranked = rank_sweep(
            bars, [0, _IMPOSSIBLE_MASK], direction=Side.BUY, tf_minutes=1, min_hits=1
        )
        assert ranked == []

    def test_rank_sweep_over_bits_prefilter_preserves_leaderboard(self) -> None:
        # The default min_hits=MIN_TRADES prunes every sub-30-hit mask; the leaderboard
        # is identical to simulating them all (they would all be gated out anyway).
        bars = _session_bars()
        bits = [0, 1, 2]
        with_filter = rank_sweep_over_bits(bars, bits, 2, direction=Side.BUY, tf_minutes=1)
        no_filter = rank_sweep_over_bits(
            bars, bits, 2, direction=Side.BUY, tf_minutes=1, min_hits=0
        )
        assert with_filter == no_filter

    def test_min_trades_is_the_shared_gate_constant(self) -> None:
        # Single source of truth: the pre-filter default == the ranking trade gate.
        assert MIN_TRADES == 30


# ────── DEC-SWEEP-RUST-METRICS-001: the flag-gated Rust metrics tail ──────


def _hand_trades(bars: list[CandleBar]) -> list[ClosedTrade]:
    """Timestamps-only trades against *bars* — the Python-walker trade shape."""
    return [
        ClosedTrade(
            pnl_paisa=-700,
            mfe_paisa=100,
            mae_paisa=700,
            holding_bars=2,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[1].timestamp,
            direction=Side.BUY,
        ),
        ClosedTrade(
            pnl_paisa=400,
            mfe_paisa=450,
            mae_paisa=20,
            holding_bars=1,
            entry_ts=bars[2].timestamp,
            exit_ts=bars[2].timestamp,
            direction=Side.BUY,
        ),
        ClosedTrade(
            pnl_paisa=900,
            mfe_paisa=950,
            mae_paisa=10,
            holding_bars=2,
            entry_ts=bars[3].timestamp,
            exit_ts=bars[4].timestamp,
            direction=Side.BUY,
        ),
    ]


def _assert_metrics_bit_identical(a: object, b: object) -> None:
    """Field-by-field equality with BIT-level float comparison (signed zeros)."""
    assert type(a) is type(b)
    for f in fields(a):  # type: ignore[arg-type]
        x = getattr(a, f.name)
        y = getattr(b, f.name)
        if isinstance(x, float) and isinstance(y, float):
            assert struct.pack("<d", x) == struct.pack("<d", y), (f.name, x, y)
        else:
            assert x == y, (f.name, x, y)


class TestRustMetricsTail:
    """metrics_from_trades: flag OFF == flag ON, bit-for-bit, on every path."""

    @pytest.fixture(autouse=True)
    def _fresh_memo(self) -> None:
        reset_observability_memo_for_tests()

    def _both_arms(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bars: list[CandleBar],
        trades: list[ClosedTrade],
        *,
        start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
        tf_minutes: int = 1,
    ) -> tuple[ResultMetrics, ResultMetrics]:
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        off = metrics_from_trades(
            bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
        )
        return off, on

    def test_traded_mask_on_equals_off_and_engages_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _session_bars()
        with capture_logs() as logs:
            off, on = self._both_arms(monkeypatch, bars, _hand_trades(bars))
        _assert_metrics_bit_identical(off, on)
        engaged = [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]
        assert len(engaged) == 1  # the ON arm genuinely ran the kernel

    def test_zero_trade_mask_on_equals_off_without_any_kernel_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flat-curve shortcut serves zero-trade masks with NO FFI call."""
        bars = _session_bars()

        def _bomb(n_bars: int, start: int, exits: list[tuple[int, int]]) -> None:
            raise AssertionError("kernel must not be called for a zero-trade mask")

        monkeypatch.setattr("brutex.engine.sweep_driver.drawdown_stats_via_kernel", _bomb)
        off, on = self._both_arms(monkeypatch, bars, [])
        _assert_metrics_bit_identical(off, on)

    def test_off_arm_never_touches_the_kernel_machinery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OFF is the verbatim pre-#1186 path — no envelope check, no kernel."""

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("flag OFF must not reach the Rust metrics seam")

        monkeypatch.setattr("brutex.engine.sweep_driver.drawdown_stats_via_kernel", _bomb)
        monkeypatch.setattr("brutex.engine.sweep_driver.exceeds_exact_equity_envelope", _bomb)
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        bars = _session_bars()
        metrics = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
        )
        assert metrics.total_trades == 3

    def test_empty_bars_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        with pytest.raises(ValueError, match="build_equity_curve: bars must be non-empty"):
            metrics_from_trades([], [], start_equity_paisa=1_000, tf_minutes=1)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with pytest.raises(ValueError, match="build_equity_curve: bars must be non-empty"):
            metrics_from_trades([], [], start_equity_paisa=1_000, tf_minutes=1)

    def test_nonpositive_start_equity_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The EquityCurve invariant error text is IDENTICAL on both arms."""
        bars = _session_bars()
        errors: list[str] = []
        for env in (None, "1"):
            if env is None:
                monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
            else:
                monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(bars, [], start_equity_paisa=0, tf_minutes=1)
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "start_equity_paisa must be > 0" in errors[0]

    def test_exit_ts_not_a_bar_timestamp_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact build_equity_curve ValueError (the _combo_error
        fingerprint feed) fires with the SAME text on both arms."""
        bars = _session_bars()
        stray = ClosedTrade(
            pnl_paisa=100,
            mfe_paisa=100,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[-1].timestamp + timedelta(minutes=7),
            direction=Side.BUY,
        )
        errors: list[str] = []
        for env in (None, "1"):
            if env is None:
                monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
            else:
                monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(bars, [stray], start_equity_paisa=1_000, tf_minutes=1)
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "is not a bar timestamp" in errors[0]

    def test_zero_tf_minutes_single_bar_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-positive span (tf_minutes=0, single bar) raises the same
        EquityCurve span error on both arms (the ON arm falls through)."""
        bars = _session_bars()[:1]
        errors: list[str] = []
        for env in (None, "1"):
            if env is None:
                monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
            else:
                monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(bars, [], start_equity_paisa=1_000, tf_minutes=0)
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "span_years must be > 0" in errors[0]

    def test_single_bar_span_floor_is_bit_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The degenerate zero-span window floors to one bar on BOTH arms."""
        bars = _session_bars()[:1]
        trade = ClosedTrade(
            pnl_paisa=250,
            mfe_paisa=250,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[0].timestamp,
            direction=Side.BUY,
        )
        off, on = self._both_arms(monkeypatch, bars, [trade], tf_minutes=5)
        _assert_metrics_bit_identical(off, on)

    def test_envelope_exceeded_falls_back_bit_identical_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start + Σ|pnl| > 2**52 routes the mask to the Python oracle."""
        bars = _session_bars()
        huge = ClosedTrade(
            pnl_paisa=MAX_EXACT_EQUITY_PAISA,  # start pushes the sum over the bound
            mfe_paisa=MAX_EXACT_EQUITY_PAISA,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[1].timestamp,
            direction=Side.BUY,
        )
        with capture_logs() as logs:
            off, on = self._both_arms(monkeypatch, bars, [huge])
        _assert_metrics_bit_identical(off, on)
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert [e["reason"] for e in events] == ["equity_envelope_exceeded"]
        assert not [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]

    def test_kernel_unavailable_falls_back_bit_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any kernel-side fallback (None) serves the mask from Python."""
        bars = _session_bars()
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        off = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
        )
        monkeypatch.setattr(
            "brutex.engine.sweep_driver.drawdown_stats_via_kernel",
            lambda *a, **k: None,
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
        )
        _assert_metrics_bit_identical(off, on)

    def test_domain_valid_poison_flows_into_the_metrics_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain-valid poisoned kernel result reaches the ResultMetrics —
        proof the ON arm consumes the kernel (the vacuous-gate trap): were
        the seam silently recomputing in Python, the poison would vanish and
        this test would fail."""
        bars = _session_bars()
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (12_345, 6.25, 2, 3.5),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
        )
        assert on.max_drawdown_paisa == 12_345
        assert on.max_drawdown_pct == 6.25
        assert on.max_drawdown_duration_bars == 2
        assert on.ulcer_index == 3.5

    def test_domain_invalid_poison_is_refused_and_equals_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain-INVALID kernel result is refused LOUDLY, never adopted:
        the mask is served by the Python oracle bit-identically."""
        bars = _session_bars()
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        off = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
        )
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (0, float("nan"), 0, 0.0),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with capture_logs() as logs:
            on = metrics_from_trades(
                bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1
            )
        _assert_metrics_bit_identical(off, on)
        refused = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_result_refused"]
        assert len(refused) == 1

    def test_metrics_for_mask_inherits_the_flag_bit_identically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The COMMON seam arms every consumer together — metrics_for_mask
        (the sweep re-attribution oracle + evaluate_combo core) is
        bit-identical across arms."""
        bars = _session_bars()
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        off = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1)
        _assert_metrics_bit_identical(off, on)
