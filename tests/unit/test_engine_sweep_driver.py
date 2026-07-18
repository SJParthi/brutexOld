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
from brutex.engine.exec_grid import build_exec_grid_map
from brutex.engine.rust_metrics import (
    MAX_EXACT_EQUITY_PAISA,
    SELF_CHECK_SAMPLES,
    SWEEP_RUST_METRICS_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    self_check_pending,
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

    def test_interior_and_late_exits_fold_onto_the_owning_bar(self) -> None:
        # DEC-FILL-EXEC-GRID-1M-001 (design §3.8): the old exact-timestamp law
        # ("exit_ts must be a bar timestamp") is RETIRED — exits now resolve on
        # the 1-minute execution grid, so a coarse-TF trade's exit_ts is an
        # INTERIOR instant of its owning decision bucket. P&L lands on the
        # LAST bar whose label <= exit_ts (bisect_right - 1).
        bars = _session_bars()
        trades = [
            # Interior instant: bars[2].ts + 30s → bisect_right over the five
            # labels finds the first label > exit at index 3 → owner idx 2.
            ClosedTrade(
                pnl_paisa=500,
                mfe_paisa=600,
                mae_paisa=50,
                holding_bars=2,
                entry_ts=bars[0].timestamp,
                exit_ts=bars[2].timestamp + timedelta(seconds=30),
                direction=Side.BUY,
            ),
            # Past the last label (the retired law's raise case): every label
            # <= exit → owner is the LAST bar (idx 4), never an error.
            ClosedTrade(
                pnl_paisa=-200,
                mfe_paisa=0,
                mae_paisa=200,
                holding_bars=1,
                entry_ts=bars[3].timestamp,
                exit_ts=bars[-1].timestamp + timedelta(minutes=99),
                direction=Side.BUY,
            ),
        ]
        curve = build_equity_curve(bars, trades, start_equity_paisa=1_000, tf_minutes=1)
        # 1_000 flat → +500 on idx 2 → 1_500 → -200 on idx 4 → 1_300.
        assert curve.equity_paisa == (1_000, 1_000, 1_500, 1_500, 1_300)

    def test_trade_exit_before_first_label_raises(self) -> None:
        # The ONE remaining out-of-window refusal: an exit_ts strictly before
        # the first bar's label has no owning bar (bisect idx < 0) → LOUD.
        bars = _session_bars()
        stray = bars[0].timestamp - timedelta(minutes=1)
        trade = ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=1,
            mae_paisa=0,
            holding_bars=1,
            # entry_ts == exit_ts (ClosedTrade refuses exit < entry) — both
            # strictly before the first bar's label, the out-of-window shape.
            entry_ts=stray,
            exit_ts=stray,
            direction=Side.BUY,
        )
        with pytest.raises(ValueError, match="precedes the first bar's label"):
            build_equity_curve(bars, [trade], tf_minutes=1)

    def test_single_bar_span_floor(self) -> None:
        # first == last bar → span 0 → floored to one bar's worth of time (> 0).
        one = [_bar(9, 16, "100.00", "100.50", "99.50", "100.00")]
        curve = build_equity_curve(one, [], tf_minutes=5)
        assert curve.span_years == pytest.approx(5.0 / (365.25 * 24.0 * 60.0))
        assert curve.equity_paisa == (DEFAULT_START_EQUITY_PAISA,)


# ────── evaluate_combo (the single-combo backtest) ──────


class TestEvaluateCombo:
    # tf == 1minute FIXED POINT (DEC-FILL-EXEC-GRID-1M-001 design §6): the
    # decision stream IS the execution stream, so every call passes
    # exec_bars_1m=bars — the dual-feed call shape with PR-A-identical timing.
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            evaluate_combo([], 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=[])

    def test_runs_full_chain_and_returns_metrics(self) -> None:
        bars = _session_bars()
        metrics = evaluate_combo(
            bars, required_mask=0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
        )
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
            bars,
            required_mask=_IMPOSSIBLE_MASK,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
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
            exec_bars_1m=bars,
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
            evaluate_combo(
                bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, roll_schedule=stale
            )

    def test_covering_schedule_matches_the_schedule_aware_sweep(self) -> None:
        # The single-contract equivalence oracle: evaluate_combo under a
        # covering schedule == sweep_masks under the SAME schedule (the
        # already-threaded sibling), and the defaults stay the spot path.
        bars = _session_bars()
        covering = (bars[-1].timestamp.astimezone(_IST).date().toordinal(),)
        direct = evaluate_combo(
            bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, roll_schedule=covering
        )
        [(_, swept)] = sweep_masks(
            bars, [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, roll_schedule=covering
        )
        assert direct == swept
        assert evaluate_combo(
            bars,
            0,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            roll_schedule=None,
            apply_1day_mask=False,
        ) == evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)


# ────── sweep_masks + rank_sweep (the leaderboard machine) ──────


class TestSweepMasks:
    # tf == 1minute fixed point throughout: exec_bars_1m=bars (design §6).
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            sweep_masks([], [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=[])

    def test_one_pair_per_mask(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:5]
        pairs = sweep_masks(bars, masks, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        assert [mask for mask, _ in pairs] == masks
        assert all(isinstance(m, ResultMetrics) for _, m in pairs)

    def test_shares_bar_bits_matches_evaluate_combo(self) -> None:
        # sweep_masks builds bar_bits ONCE + reuses it, but must produce the SAME
        # metrics as evaluate_combo per mask (no behavioural drift from the refactor).
        bars = _session_bars()
        mask = 0
        pairs = sweep_masks(bars, [mask], direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        _, swept = pairs[0]
        direct = evaluate_combo(bars, mask, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        assert swept == direct

    def test_prebuilt_exec_map_is_adopted_not_rebuilt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hot sweep spine threads the tuple's memoised ExecGridMap
        # (DEC-FILL-EXEC-GRID-1M-001 design §1.5): a caller-supplied map is
        # ADOPTED — the build seam is never re-entered — and the results are
        # byte-identical to the derive-here arm.
        bars = _session_bars()
        baseline = sweep_masks(bars, [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        emap = build_exec_grid_map(bars, bars, tf_minutes=1)

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("a supplied exec_map must never be rebuilt")

        monkeypatch.setattr("brutex.engine.sweep_driver.build_exec_grid_map", _bomb)
        with_map = sweep_masks(
            bars, [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, exec_map=emap
        )
        assert with_map == baseline


class TestRankSweep:
    def test_returns_ranked_leaderboard(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:6]
        ranked = rank_sweep(bars, masks, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
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
            rank_sweep_over_bits([], [0, 1], 2, direction=Side.BUY, tf_minutes=1, exec_bars_1m=[])

    def test_equivalent_to_explicit_rank_sweep(self) -> None:
        # rank_sweep_over_bits is exactly rank_sweep over the K-of-bits mask space —
        # no hidden behaviour (deterministic, so the leaderboards are identical).
        bars = _session_bars()
        bits = [0, 1, 2]
        explicit = rank_sweep(
            bars,
            list(iter_masks_k_of_bits(bits, 2)),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
        )
        viabits = rank_sweep_over_bits(
            bars, bits, 2, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
        )
        assert viabits == explicit
        assert isinstance(viabits, list)
        assert all(isinstance(rc, RankedCombo) for rc in viabits)

    def test_over_cap_raises_before_compute(self) -> None:
        # C(3, 2) = 3 masks > max_combos=2 → refuse (honest guardrail), no sweep run.
        bars = _session_bars()
        with pytest.raises(ValueError, match=r"exceeds max_combos=2"):
            rank_sweep_over_bits(
                bars,
                [0, 1, 2],
                2,
                direction=Side.BUY,
                tf_minutes=1,
                exec_bars_1m=bars,
                max_combos=2,
            )

    def test_k_too_large_raises(self) -> None:
        # count_combos rejects k > n_unique before any enumeration.
        bars = _session_bars()
        with pytest.raises(ValueError, match="invalid k"):
            rank_sweep_over_bits(
                bars, [0, 1], 3, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
            )

    def test_out_of_range_bit_raises_after_cap_passes(self) -> None:
        # C(2, 1) = 2 clears the default cap, so the range check in the generator
        # is what rejects a bit at MAX_VOCAB_BITS (=128). PR-1 widened the mask
        # u64 -> u128, so the valid range is now [0, 128) and bit 64 is in range;
        # 128 is the first out-of-range position.
        bars = _session_bars()
        with pytest.raises(ValueError, match="out of range"):
            rank_sweep_over_bits(
                bars, [0, 128], 1, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
            )

    def test_default_cap_admits_full_vocab_k_of_4(self) -> None:
        # The documented envelope: the default cap admits C(52,4)=270,725? No — it is
        # 100,000, so K=4 over the full 52 is REFUSED unless the cap is raised. This
        # pins the honest default (no silent multi-100k-combo sweep).
        assert DEFAULT_MAX_COMBOS == 100_000
        bars = _session_bars()
        with pytest.raises(ValueError, match="exceeds max_combos=100000"):
            rank_sweep_over_bits(
                bars, list(range(52)), 4, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
            )


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
            bars,
            [0, _IMPOSSIBLE_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            min_hits=1,
        )
        assert [m for m, _ in pairs] == [0]

    def test_min_hits_zero_keeps_every_mask(self) -> None:
        bars = _session_bars()
        pairs = sweep_masks(
            bars,
            [0, _IMPOSSIBLE_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            min_hits=0,
        )
        assert [m for m, _ in pairs] == [0, _IMPOSSIBLE_MASK]

    def test_surviving_metrics_identical_to_unfiltered(self) -> None:
        bars = _session_bars()
        # Prepend mask 0 (always 5 hits → always survives) so a survivor exists.
        masks = [0, *list(iter_masks_k_of_n(1, n=52))[:7]]
        full = dict(
            sweep_masks(
                bars, masks, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, min_hits=0
            )
        )
        filtered = sweep_masks(
            bars, masks, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, min_hits=1
        )
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
            bars,
            [0, _IMPOSSIBLE_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            min_hits=1,
        )
        assert ranked == []

    def test_rank_sweep_over_bits_prefilter_preserves_leaderboard(self) -> None:
        # The default min_hits=MIN_TRADES prunes every sub-30-hit mask; the leaderboard
        # is identical to simulating them all (they would all be gated out anyway).
        bars = _session_bars()
        bits = [0, 1, 2]
        with_filter = rank_sweep_over_bits(
            bars, bits, 2, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
        )
        no_filter = rank_sweep_over_bits(
            bars, bits, 2, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars, min_hits=0
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
        reset_self_check_for_tests()

    def _both_arms(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bars: list[CandleBar],
        trades: list[ClosedTrade],
        *,
        start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
        tf_minutes: int = 1,
    ) -> tuple[ResultMetrics, ResultMetrics]:
        # Explicit "0" — the operator kill switch — so the OFF arm stays the
        # Python tail under the ADR-0017 default-ON constant. exec_bars_1m=bars
        # is the tf=1minute fixed point (DEC-FILL-EXEC-GRID-1M-001 design §6) —
        # total_bars = len(exec_bars_1m) = len(bars), identical on both arms.
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=start_equity_paisa,
            tf_minutes=tf_minutes,
            exec_bars_1m=bars,
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=start_equity_paisa,
            tf_minutes=tf_minutes,
            exec_bars_1m=bars,
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
        """Explicit OFF ("0", the kill switch) is the verbatim pre-#1186 path
        — no envelope check, no kernel."""

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("flag OFF must not reach the Rust metrics seam")

        monkeypatch.setattr("brutex.engine.sweep_driver.drawdown_stats_via_kernel", _bomb)
        monkeypatch.setattr("brutex.engine.sweep_driver.exceeds_exact_equity_envelope", _bomb)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        bars = _session_bars()
        metrics = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        assert metrics.total_trades == 3

    def test_empty_bars_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        with pytest.raises(ValueError, match="build_equity_curve: bars must be non-empty"):
            metrics_from_trades([], [], start_equity_paisa=1_000, tf_minutes=1, exec_bars_1m=[])
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with pytest.raises(ValueError, match="build_equity_curve: bars must be non-empty"):
            metrics_from_trades([], [], start_equity_paisa=1_000, tf_minutes=1, exec_bars_1m=[])

    def test_nonpositive_start_equity_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The EquityCurve invariant error text is IDENTICAL on both arms."""
        bars = _session_bars()
        errors: list[str] = []
        # Explicit "0" (not delenv) — under the ADR-0017 default-ON constant an
        # unset env resolves ON, so only the kill switch spans both arms.
        for env in ("0", "1"):
            monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(bars, [], start_equity_paisa=0, tf_minutes=1, exec_bars_1m=bars)
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "start_equity_paisa must be > 0" in errors[0]

    def test_exit_ts_before_first_label_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact build_equity_curve ValueError (the _combo_error
        fingerprint feed) fires with the SAME text on both arms.

        DEC-FILL-EXEC-GRID-1M-001: the retired "is not a bar timestamp" law
        is gone — a post-window exit now folds onto the LAST bar (bisect),
        so the out-of-window refusal both arms must share is the
        PRE-window exit (no owning bar: idx < 0 → the ON arm's _exit_pairs
        returns None and the Python tail raises the one exact error).
        """
        bars = _session_bars()
        # entry_ts == exit_ts (ClosedTrade refuses exit < entry) — both
        # strictly before the first bar's label, the out-of-window shape.
        pre_window = bars[0].timestamp - timedelta(minutes=7)
        stray = ClosedTrade(
            pnl_paisa=100,
            mfe_paisa=100,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=pre_window,
            exit_ts=pre_window,
            direction=Side.BUY,
        )
        errors: list[str] = []
        # Explicit "0" (not delenv) — under the ADR-0017 default-ON constant an
        # unset env resolves ON, so only the kill switch spans both arms.
        for env in ("0", "1"):
            monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(
                    bars, [stray], start_equity_paisa=1_000, tf_minutes=1, exec_bars_1m=bars
                )
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "precedes the first bar's label" in errors[0]

    def test_zero_tf_minutes_single_bar_raises_identically_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-positive span (tf_minutes=0, single bar) raises the same
        EquityCurve span error on both arms (the ON arm falls through)."""
        bars = _session_bars()[:1]
        errors: list[str] = []
        # Explicit "0" (not delenv) — under the ADR-0017 default-ON constant an
        # unset env resolves ON, so only the kill switch spans both arms.
        for env in ("0", "1"):
            monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(
                    bars, [], start_equity_paisa=1_000, tf_minutes=0, exec_bars_1m=bars
                )
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
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        monkeypatch.setattr(
            "brutex.engine.sweep_driver.drawdown_stats_via_kernel",
            lambda *a, **k: None,
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        _assert_metrics_bit_identical(off, on)

    def test_domain_valid_poison_flows_into_the_metrics_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain-valid poisoned kernel result reaches the ResultMetrics —
        proof the ON arm consumes the kernel (the vacuous-gate trap): were
        the seam silently recomputing in Python, the poison would vanish and
        this test would fail. The burn-in self-check is explicitly EXHAUSTED
        first (steady state) — with samples pending the same poison would be
        caught + autoreverted (pinned by TestKernelSelfCheck)."""
        bars = _session_bars()
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (12_345, 6.25, 2, 3.5),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        reset_self_check_for_tests(remaining=0)
        on = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
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
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            bars, _hand_trades(bars), start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (0, float("nan"), 0, 0.0),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with capture_logs() as logs:
            on = metrics_from_trades(
                bars,
                _hand_trades(bars),
                start_equity_paisa=1_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
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
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        on = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        _assert_metrics_bit_identical(off, on)


# ────── DEC-SWEEP-EXIT-INDEX-FASTPATH-001: exit-index fast path ──────


class TestExitIndexFastPath:
    """metrics_from_trades(exit_indices=…): fast path, demotion, OFF-arm inertness.

    On the 1m grid (DEC-FILL-EXEC-GRID-1M-001 §3.8) the certified fast path
    needs BOTH the execution stream and its ExecGridMap (`decision_of`
    translates each 1-minute exit index to the owning decision bar); a
    missing/None exec_map DEMOTES to the owning-bucket bisect fold. At the
    tf=1minute fixed point (exec == decision stream) `decision_of[i] == i`,
    so the translated pairs equal the bisect fold's — bit-identical results.
    """

    @pytest.fixture(autouse=True)
    def _fresh_memo(self) -> None:
        reset_observability_memo_for_tests()
        reset_self_check_for_tests()

    def test_consistent_indices_skip_the_dict_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The certified fast path never builds a ts→index dict — the whole
        point (O(trades) instead of O(N_bars) per mask) — and never demotes
        (no `exit_indices_demoted` fallback: the map+indices are consumed)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        emap = build_exec_grid_map(bars, bars, tf_minutes=1)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        baseline = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )

        def _bomb(window: object) -> None:
            raise AssertionError("fast path must not build the ts_to_index dict")

        monkeypatch.setattr("brutex.engine.sweep_driver.build_ts_index", _bomb)
        with capture_logs() as logs:
            fast = metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=1_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
                exec_map=emap,
                exit_indices=[1, 2, 4],
            )
        _assert_metrics_bit_identical(baseline, fast)
        # The certified path was genuinely taken — no demotion fallback fired.
        assert not [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]

    def test_fast_path_equals_off_arm_bitwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bars = _session_bars()
        trades = _hand_trades(bars)
        emap = build_exec_grid_map(bars, bars, tf_minutes=1)
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        fast = metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=1_000_000,
            tf_minutes=1,
            exec_bars_1m=bars,
            exec_map=emap,
            exit_indices=[1, 2, 4],
        )
        _assert_metrics_bit_identical(off, fast)

    @pytest.mark.parametrize(
        "bad_indices",
        [
            [1, 2],  # wrong length (short)
            [1, 2, 4, 4],  # wrong length (long)
            [1, 2, 5],  # out of range
            [1, 2, -1],  # negative (a Python-wraparound hazard if trusted)
            [1, 3, 4],  # in range but the WRONG bar for trade 2's exit_ts
        ],
    )
    def test_inconsistent_indices_demote_to_the_fold_not_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, bad_indices: list[int]
    ) -> None:
        """A bad stream demotes to the owning-bucket bisect fold (same kernel
        result) with one warn-once — NEVER adopted, never a Python-oracle
        downgrade. The oracle tail's entry (``build_equity_curve``) is BOMBED
        for the demoted call: the demotion must land on the kernel fold path,
        so the bomb proves the "NEVER to the Python oracle" half of the
        demotion law (bit-identity alone cannot — the oracle reproduces the
        kernel bytes by design)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        emap = build_exec_grid_map(bars, bars, tf_minutes=1)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        baseline = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("demotion must land on the kernel fold path, not the oracle")

        monkeypatch.setattr("brutex.engine.sweep_driver.build_equity_curve", _bomb)
        with capture_logs() as logs:
            demoted = metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=1_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
                exec_map=emap,
                exit_indices=bad_indices,
            )
        _assert_metrics_bit_identical(baseline, demoted)
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert [e["reason"] for e in events] == ["exit_indices_demoted"]

    def test_certified_index_owned_by_no_decision_bar_raises_out_of_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CONSISTENT exit index landing on an exec bar BEFORE the first
        decision label (decision_of == -1) is out-of-window: the fast path
        withholds its pairs (never a fabricated fold at index -1) and the
        Python tail raises build_equity_curve's exact pre-window error —
        identically on both arms (DEC-FILL-EXEC-GRID-1M-001 §3.8)."""
        exec_bars = _session_bars()
        decision = exec_bars[2:]  # the first two exec bars precede every label
        emap = build_exec_grid_map(decision, exec_bars, tf_minutes=1)
        early = exec_bars[1].timestamp  # a real exec bar ts, pre-window
        trade = ClosedTrade(
            pnl_paisa=100,
            mfe_paisa=100,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=early,
            exit_ts=early,
            direction=Side.BUY,
        )
        errors: list[str] = []
        for env in ("0", "1"):
            monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, env)
            with pytest.raises(ValueError) as exc_info:
                metrics_from_trades(
                    decision,
                    [trade],
                    start_equity_paisa=1_000,
                    tf_minutes=1,
                    exec_bars_1m=exec_bars,
                    exec_map=emap,
                    # Consistent vs the EXEC stream (exec_bars[1].ts == exit_ts)
                    # — only the decision translation is out of window.
                    exit_indices=[1],
                )
            errors.append(str(exc_info.value))
        assert errors[0] == errors[1]
        assert "precedes the first bar's label" in errors[0]

    def test_missing_exec_map_demotes_the_supplied_indices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """exec_map=None cannot translate 1m exit indices to decision bars —
        even a CONSISTENT stream demotes to the bisect fold (one warn-once),
        bit-identically (DEC-FILL-EXEC-GRID-1M-001 §3.8)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        baseline = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        with capture_logs() as logs:
            demoted = metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=1_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
                exit_indices=[1, 2, 4],  # consistent — but no map to translate
            )
        _assert_metrics_bit_identical(baseline, demoted)
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert [e["reason"] for e in events] == ["exit_indices_demoted"]

    def test_demoted_mask_is_still_served_by_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steady state + a domain-valid poisoned kernel: the poison flows
        through on the DEMOTED path — value-level proof the demotion is served
        by the kernel via the dict, not the Python oracle (which would erase
        the poison and compute the real drawdown). The self-check is exhausted
        first, exactly like test_domain_valid_poison_flows_into_the_metrics_row
        (with samples pending the poison would autorevert instead)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        emap = build_exec_grid_map(bars, bars, tf_minutes=1)
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (12_345, 6.25, 2, 3.5),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        reset_self_check_for_tests(remaining=0)
        with capture_logs() as logs:
            demoted = metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=1_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
                exec_map=emap,
                exit_indices=[1, 2, 5],  # out of range → inconsistent → demote
            )
        assert demoted.max_drawdown_paisa == 12_345
        assert demoted.max_drawdown_pct == 6.25
        assert demoted.max_drawdown_duration_bars == 2
        assert demoted.ulcer_index == 3.5
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert [e["reason"] for e in events] == ["exit_indices_demoted"]

    def test_off_arm_ignores_exit_indices_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OFF never reads the hint — even a poisoned stream is inert."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off_plain = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        off_poison = metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=1_000_000,
            tf_minutes=1,
            exec_bars_1m=bars,
            exit_indices=[0, 0, 0],  # wrong on purpose — must be ignored
        )
        _assert_metrics_bit_identical(off_plain, off_poison)

    def test_metrics_for_mask_threads_indices_without_dict_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The evaluate_combo→metrics_for_mask chain hands the walker's
        certified indices to the kernel arm — no per-mask dict build."""
        bars = _session_bars()
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        baseline = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)

        def _bomb(window: object) -> None:
            raise AssertionError("metrics_for_mask fast path must not build the dict")

        monkeypatch.setattr("brutex.engine.sweep_driver.build_ts_index", _bomb)
        fast = evaluate_combo(bars, 0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars)
        _assert_metrics_bit_identical(baseline, fast)


# ────── DEC-SWEEP-EXIT-INDEX-FASTPATH-001: burn-in self-check + autorevert ──────


class TestKernelSelfCheck:
    """The burn-in bit-compare: pass consumes budget; mismatch ships the oracle
    value and latches the kernel OFF for the process."""

    @pytest.fixture(autouse=True)
    def _fresh_memo(self) -> None:
        reset_observability_memo_for_tests()
        reset_self_check_for_tests()

    def test_passing_compares_consume_the_burn_in_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _session_bars()
        trades = _hand_trades(bars)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        reset_self_check_for_tests(remaining=2)
        assert self_check_pending() is True
        metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        assert self_check_pending() is True  # one sample left
        metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        assert self_check_pending() is False  # budget exhausted — steady state

    def test_mismatch_ships_the_oracle_value_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain-VALID wrong kernel answer during burn-in: the ORACLE value
        ships (bit-identical to the OFF arm — the poison never persists), one
        kernel_autoreverted ERROR fires, and the NEXT call short-circuits to
        the Python tail with a warn-once kernel_disabled fallback."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        # Explicit "0" — the operator kill switch — so this arm stays the
        # Python tail under the ADR-0017 default-ON constant (delenv would
        # resolve ON and silently compare kernel-vs-kernel).
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        off = metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        monkeypatch.setattr(
            fno_math,
            "sweep_drawdown_stats",
            lambda n_bars, start, exits: (12_345, 6.25, 2, 3.5),
        )
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with capture_logs() as logs:
            first = metrics_from_trades(
                bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
            )
            second = metrics_from_trades(
                bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
            )
        _assert_metrics_bit_identical(off, first)  # the oracle value shipped
        _assert_metrics_bit_identical(off, second)  # latched → Python tail
        reverted = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_autoreverted"]
        assert len(reverted) == 1
        assert reverted[0]["reason"] == "self_check_mismatch"
        fallbacks = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert [e["reason"] for e in fallbacks] == ["kernel_disabled"]
        # The burn-in budget was NOT consumed by the failed compare.
        assert self_check_pending() is True

    def test_signed_zero_divergence_is_caught_by_the_bit_compare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """-0.0 == +0.0 under float equality — the struct.pack bit-compare
        must still catch it (the reason the compare is bit-level at all)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        real_kernel = fno_math.sweep_drawdown_stats

        def _negzero(n_bars: int, start: int, exits: list[tuple[int, int]]) -> object:
            stats = real_kernel(n_bars, start, exits)
            return (stats[0], stats[1], stats[2], -0.0) if stats[3] == 0.0 else stats

        # A profitable-only trade set → true ulcer is +0.0; poison flips its sign.
        winners = [t for t in trades if t.pnl_paisa > 0]
        monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _negzero)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        with capture_logs() as logs:
            metrics_from_trades(
                bars, winners, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
            )
        reverted = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_autoreverted"]
        assert len(reverted) == 1

    def test_steady_state_runs_no_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Past the burn-in, the oracle scan never runs (the steady-state cost
        contract the speedup bench measures)."""
        bars = _session_bars()
        trades = _hand_trades(bars)
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
        reset_self_check_for_tests(remaining=0)

        def _bomb(*args: object, **kwargs: object) -> None:
            raise AssertionError("steady state must not run the oracle drawdown")

        monkeypatch.setattr("brutex.engine.sweep_driver._oracle_drawdown", _bomb)
        metrics_from_trades(
            bars, trades, start_equity_paisa=1_000_000, tf_minutes=1, exec_bars_1m=bars
        )

    def test_default_burn_in_budget_is_pinned(self) -> None:
        assert SELF_CHECK_SAMPLES == 16
