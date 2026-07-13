"""WORST_CASE adverse-max fill mode — walker + kernel-seam + identity units.

DEC-FILL-WORST-CASE-001. The named regressions (gap-through-level clamp both
directions; entry at the adverse extreme), every new walker branch (entry /
AT_LEVEL / pending / forced / end_of_data / session_gap × BUY/SELL), the
resident-kernel seam's feature detection + ``fill_mode_kernel_missing``
demotion, and the run-identity emit-on-non-PRECISE law.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim, _kernel_accepts_worst_case
from brutex.engine.sweep_run import canonical_trade_params
from brutex.engine.trade_sim import (
    DEFAULT_FILL_POLICY,
    FillPolicy,
    LevelFill,
    TradeDetail,
    simulate_trades,
)
from brutex.execution.fill_mode import FillMode
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_WORST = FillPolicy(fill_mode=FillMode.WORST_CASE)


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
    mode: FillMode,
    sl: int | None = None,
    tp: int | None = None,
    stop_loss: LevelFill = LevelFill.AT_LEVEL,
    signals: Sequence[bool] | None = None,
) -> tuple[list[Any], list[TradeDetail]]:
    sink: list[TradeDetail] = []
    trades = simulate_trades(
        bars,
        [True] + [False] * (len(bars) - 1) if signals is None else signals,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        policy=FillPolicy(fill_mode=mode, stop_loss=stop_loss),
        detail_sink=sink,
    )
    return trades, sink


class TestDefaultPolicyIsPrecise:
    """The library-level default stays byte-identical legacy."""

    def test_default_policy_fill_mode(self) -> None:
        assert DEFAULT_FILL_POLICY.fill_mode is FillMode.PRECISE
        assert FillPolicy().fill_mode is FillMode.PRECISE

    def test_junk_fill_mode_string_fails_loud(self) -> None:
        # NIT-4 (fix round 2): a junk string would compare
        # `is FillMode.WORST_CASE` False at every fill site and SILENTLY
        # walk PRECISE — the config-boundary guard refuses it up front.
        with pytest.raises(ValueError, match="not a FillMode member"):
            FillPolicy(fill_mode="worst_case")  # type: ignore[arg-type]

    def test_both_real_members_construct(self) -> None:
        assert FillPolicy(fill_mode=FillMode.WORST_CASE).fill_mode is FillMode.WORST_CASE
        assert FillPolicy(fill_mode=FillMode.PRECISE).fill_mode is FillMode.PRECISE


class TestNamedRegressionGapThroughLevel:
    """(a) gap-through-level: the adverse-max clamp, both directions."""

    def test_long_tp_gap_up_fills_at_level_never_bar_low(self) -> None:
        # Long TP with a gap-up bar ENTIRELY ABOVE the level (low > tp): the
        # worst-case fill is min(tp, low) == tp — a naive sell-at-low (126 >
        # level 125) would OVERSTATE vs today's at-level fill.
        bars = [
            _bar(0, 100, 100, 100, 100),  # signal bar
            _bar(1, 100, 105, 98, 100),  # entry bar — worst fill at HIGH 105
            _bar(2, 130, 140, 126, 135),  # gap-up: low 126 > level 125
        ]
        trades, sink = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE, tp=2000)
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
        trades, sink = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE, sl=1000)
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
        trades, sink = _sim(bars, direction=Side.SELL, mode=FillMode.WORST_CASE, tp=1500)
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
        trades, sink = _sim(bars, direction=Side.SELL, mode=FillMode.WORST_CASE, tp=1500)
        assert sink[0].exit_fill_paisa == 9600  # max(level 8000, high 9600)
        assert trades[0].pnl_paisa == 9500 - 9600  # a "take-profit" priced at worst

    def test_precise_same_fixture_fills_at_level(self) -> None:
        # The PRECISE arm of the same fixture stays the legacy at-level fill.
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 105, 98, 100),
            _bar(2, 85, 88, 80, 82),
        ]
        trades, sink = _sim(bars, direction=Side.BUY, mode=FillMode.PRECISE, sl=1000)
        assert sink[0].entry_fill_paisa == 10000  # entry bar OPEN
        assert sink[0].exit_fill_paisa == 9000  # the level (100 − 10)
        assert trades[0].pnl_paisa == -1000


class TestNamedRegressionEntryExtreme:
    """(b) entry extreme — the entry fills at the adverse extreme, ≥ open."""

    def test_long_entry_fills_at_bar_high(self) -> None:
        bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 107, 96, 103)]
        _, worst = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE)
        _, precise = _sim(bars, direction=Side.BUY, mode=FillMode.PRECISE)
        assert worst[0].entry_fill_paisa == 10700  # bar HIGH
        assert precise[0].entry_fill_paisa == 10000  # bar OPEN
        assert worst[0].entry_fill_paisa >= precise[0].entry_fill_paisa

    def test_short_entry_fills_at_bar_low(self) -> None:
        bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 107, 96, 103)]
        _, worst = _sim(bars, direction=Side.SELL, mode=FillMode.WORST_CASE)
        _, precise = _sim(bars, direction=Side.SELL, mode=FillMode.PRECISE)
        assert worst[0].entry_fill_paisa == 9600  # bar LOW
        assert precise[0].entry_fill_paisa == 10000
        assert worst[0].entry_fill_paisa <= precise[0].entry_fill_paisa


class TestOpenAnchoredAndTailExits:
    """Pending / forced / end_of_data / session_gap under the adverse-max law."""

    def test_pending_next_open_exit_fills_at_adverse_extreme_fold_unchanged(self) -> None:
        # NEXT_OPEN stop: the fill bar's LOW prices the exit while the
        # excursion still folds the legacy (open, open) — mae reads 25 from
        # the fold, NOT the 27 the fill implies (fold logic untouched).
        bars = [
            _bar(0, 50, 60, 40, 55),
            _bar(1, 100, 105, 98, 100),  # entry HIGH 105; sl 10 → level 95
            _bar(2, 95, 99, 85, 90),  # low 85 ≤ 95 → pending
            _bar(3, 80, 82, 78, 81),  # fills at LOW 78 (not open 80)
        ]
        trades, sink = _sim(
            bars,
            direction=Side.BUY,
            mode=FillMode.WORST_CASE,
            sl=1000,
            stop_loss=LevelFill.NEXT_OPEN,
            signals=[True, False, False, False],
        )
        assert sink[0].exit_reason == "stop_loss"
        assert sink[0].exit_fill_paisa == 7800
        assert trades[0].pnl_paisa == 7800 - 10500
        assert trades[0].mae_paisa == 10500 - 8000  # the (open, open) fold — 25.00

    def test_short_pending_exit_fills_at_bar_high(self) -> None:
        bars = [
            _bar(0, 50, 60, 40, 55),
            _bar(1, 100, 102, 95, 100),  # SELL entry LOW 95; sl 10 → level 105
            _bar(2, 104, 106, 103, 105),  # high 106 ≥ 105 → pending
            _bar(3, 108, 112, 107, 110),  # covers at HIGH 112 (not open 108)
        ]
        _, sink = _sim(
            bars,
            direction=Side.SELL,
            mode=FillMode.WORST_CASE,
            sl=1000,
            stop_loss=LevelFill.NEXT_OPEN,
            signals=[True, False, False, False],
        )
        assert sink[0].exit_fill_paisa == 11200

    def test_forced_exit_fills_at_adverse_extreme(self) -> None:
        # A bar in the forced window (15:20+ IST — 09:50 UTC) closes the
        # position at its LOW under WORST_CASE, not its open.
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
        _, sink = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE)
        assert sink[0].exit_reason == "forced_1520"
        assert sink[0].exit_fill_paisa == 10400  # forced bar LOW, not open 10800

    def test_end_of_data_fills_at_adverse_extreme_both_directions(self) -> None:
        bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 110, 95, 105)]
        _, buy = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE)
        _, sell = _sim(bars, direction=Side.SELL, mode=FillMode.WORST_CASE)
        assert buy[0].exit_reason == "end_of_data"
        assert buy[0].exit_fill_paisa == 9500  # last bar LOW
        assert sell[0].exit_fill_paisa == 11000  # last bar HIGH

    def test_session_gap_fills_at_prior_bar_adverse_extreme(self) -> None:
        # An open position crossing an IST session date closes retroactively
        # at the PRIOR session's last stored bar — its LOW under WORST_CASE
        # (its CLOSE under PRECISE, re-pinned here as the contrast).
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 106, 97, 103),  # entry bar (worst: HIGH 106)
            _bar(2, 103, 108, 99, 101),  # last stored bar of the session
            _bar(0, 130, 135, 128, 132, day=1),  # next session — the boundary bar
        ]
        _, worst = _sim(bars, direction=Side.BUY, mode=FillMode.WORST_CASE)
        _, precise = _sim(bars, direction=Side.BUY, mode=FillMode.PRECISE)
        assert worst[0].exit_reason == "session_gap"
        assert worst[0].exit_fill_paisa == 9900  # prior bar LOW
        assert precise[0].exit_fill_paisa == 10100  # prior bar CLOSE (legacy)

    def test_session_gap_short_fills_at_prior_bar_high(self) -> None:
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 106, 97, 103),
            _bar(2, 103, 108, 99, 101),
            _bar(0, 130, 135, 128, 132, day=1),
        ]
        _, sink = _sim(bars, direction=Side.SELL, mode=FillMode.WORST_CASE)
        assert sink[0].exit_reason == "session_gap"
        assert sink[0].exit_fill_paisa == 10800  # prior bar HIGH


class TestKernelSeamFeatureDetection:
    """The resident-kernel seam: real-wheel parity + the legacy-wheel demotion."""

    @staticmethod
    def _window() -> tuple[list[CandleBar], list[int]]:
        bars = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 105, 98, 100),
            _bar(2, 85, 88, 80, 82),
            _bar(3, 90, 95, 85, 92),
        ]
        return bars, [1, 0, 1, 0]

    def test_real_kernel_serves_worst_case_bit_identical_to_oracle(self) -> None:
        bars, bits = self._window()
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        if not sim.rust_active:  # pragma: no cover — wheel-less environment
            pytest.skip("fno_math wheel not installed")
        kernel = sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=1000, policy=_WORST)[0]
        oracle = simulate_trades(
            bars,
            mask_to_entry_signals(bits, 1),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=1000,
            policy=_WORST,
        )
        assert [(t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars) for t in kernel] == [
            (t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars) for t in oracle
        ]
        assert kernel[0].pnl_paisa == 8000 - 10500  # the worst-case bytes, not precise

    def test_legacy_kernel_demotes_worst_case_to_oracle_with_one_warn(self) -> None:
        # A wheel PREDATING the worst_case kwarg: the batch demotes to the
        # bit-exact Python oracle with the warn-once fill_mode_kernel_missing
        # reason — never a TypeError, never a wrong kernel answer.
        class _LegacyKernel:
            calls: ClassVar[list[tuple[Any, ...]]] = []

            def simulate_masks(
                self,
                masks: list[int],
                is_buy: bool,
                sl_paisa: int | None,
                tp_paisa: int | None,
                sl_next_open: bool = False,
                tp_next_open: bool = False,
                tp_first: bool = False,
            ) -> list[list[tuple[int, int, int, int, int, int]]]:
                raise AssertionError("a legacy kernel must never serve a WORST_CASE batch")

        bars, bits = self._window()
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        sim._rust = _LegacyKernel()
        with capture_logs() as logs:
            first = sim.simulate_masks(
                [1], direction=Side.BUY, stop_loss_paisa=1000, policy=_WORST
            )[0]
            second = sim.simulate_masks(  # repeat: memo + warn-once
                [1], direction=Side.BUY, stop_loss_paisa=1000, policy=_WORST
            )[0]
        oracle = simulate_trades(
            bars,
            mask_to_entry_signals(bits, 1),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=1000,
            policy=_WORST,
        )
        for served in (first, second):
            assert [(t.pnl_paisa, t.mae_paisa) for t in served] == [
                (t.pnl_paisa, t.mae_paisa) for t in oracle
            ]
        warns = [
            log
            for log in logs
            if log.get("event") == "rust_trade_sim.call_fallback"
            and log.get("reason") == "fill_mode_kernel_missing"
        ]
        assert len(warns) == 1  # warn-once per (instance, reason)

    def test_legacy_kernel_still_serves_precise_via_the_seven_arg_call(self) -> None:
        # PRECISE batches never probe the flag and keep the historical
        # 7-argument kernel call — an old wheel keeps serving them.
        recorded: list[int] = []

        class _LegacyKernel:
            def simulate_masks(
                self,
                masks: list[int],
                is_buy: bool,
                sl_paisa: int | None,
                tp_paisa: int | None,
                sl_next_open: bool = False,
                tp_next_open: bool = False,
                tp_first: bool = False,
            ) -> list[list[tuple[int, int, int, int, int, int]]]:
                recorded.append(len(masks))
                return [[] for _ in masks]

        bars, bits = self._window()
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        sim._rust = _LegacyKernel()
        out = sim.simulate_masks([1, 2], direction=Side.BUY, stop_loss_paisa=1000)
        assert recorded == [2]
        assert out == [[], []]

    def test_kernel_accepts_worst_case_probe_matrix(self) -> None:
        class _New:
            def simulate_masks(self, masks: object, *, worst_case: bool = False) -> None: ...

        class _Old:
            def simulate_masks(self, masks: object, tp_first: bool = False) -> None: ...

        class _NoAttr:
            pass

        class _NotCallable:
            simulate_masks = property()

        class _NoSignature:
            # ``inspect.signature(type)`` raises ValueError (no signature for
            # the builtin) — the conservative UNSUPPORTED arm.
            simulate_masks = type

        assert _kernel_accepts_worst_case(_New()) is True
        assert _kernel_accepts_worst_case(_Old()) is False
        assert _kernel_accepts_worst_case(_NoAttr()) is False
        assert _kernel_accepts_worst_case(_NotCallable()) is False
        assert _kernel_accepts_worst_case(_NoSignature()) is False


class TestRunIdentityEmitLaw:
    """canonical_trade_params: PRECISE emits NO key; WORST_CASE exactly one."""

    _KW: ClassVar[dict[str, Any]] = {
        "sl_paisa": 80,
        "tp_paisa": 50,
        "prior_n": 3,
        "proximity": None,
        "start_equity_paisa": 10_000_000,
    }

    def test_precise_params_are_byte_identical_to_the_legacy_dict(self) -> None:
        legacy = canonical_trade_params(**self._KW)
        explicit = canonical_trade_params(**self._KW, fill_mode=FillMode.PRECISE)
        assert explicit == legacy
        assert "fill_mode" not in legacy
        assert list(legacy) == [
            "sl_paisa",
            "tp_paisa",
            "prior_n",
            "proximity_bps",
            "fib_anchor",
            "start_equity_paisa",
        ]

    def test_worst_case_adds_exactly_the_one_key(self) -> None:
        worst = canonical_trade_params(**self._KW, fill_mode=FillMode.WORST_CASE)
        precise = canonical_trade_params(**self._KW)
        assert worst.pop("fill_mode") == "worst_case"
        assert worst == precise
