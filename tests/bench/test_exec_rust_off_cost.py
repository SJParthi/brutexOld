"""OFF-mode cost gate for the flag-gated Rust exec-cores seam.

DEC-EXEC-RUST-CORE-001's hard constraint (mirrors
``test_sweep_rust_metrics_off_cost.py``): with ``BRUTEX_EXEC_RUST`` OFF —
BOTH the default (unset) arm and the explicit ``"0"`` kill switch —
``compute_trade_cost`` / ``compute_futures_trade_cost`` must be
byte-identical AND cost-unchanged: the ONLY addition on the OFF path is one
env-dict probe + one returning-``None`` seam call per dispatch.

NO speedup gate and NO speedup claim exist anywhere for this slice: the
executors are a COLD path (per-trade loops over FINALIST trades only), so
the Rust arm's value is one-language consistency, not throughput (Golden
Rule #10 — the DEC carries the payoff tag UNKNOWN-expected-small). This
file pins only that the OFF arm costs nothing.

Two complementary proofs (the structural third — OFF never touches the
kernel machinery — is the bomb-kernel test in ``test_exec_rust_seam.py``):

1. **Flag-probe cost is nil:** the per-call ``exec_rust_enabled()`` read
   must cost less than ONE bare no-op walk over a large list — it can
   therefore never hide per-trade work.
2. **A/B wall-clock:** the OFF-mode ``compute_trade_cost`` median stays
   within a generous noise ceiling of the DIRECT Python oracle chain it
   wraps (the same lot/rate resolution + ``_charge_stack_py`` + breakdown
   construction, ± one env read + one ``None``-returning seam call).
"""

from __future__ import annotations

import time
from datetime import date, datetime

import pytest
from brutex import exec_rust
from brutex.costs import constants as const
from brutex.costs.calculator import (
    _charge_stack_py,  # pyright: ignore[reportPrivateUsage] — the direct oracle arm
    compute_trade_cost,
)
from brutex.costs.types import TradeCostBreakdown, TradeInput, require_bar_extremes
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size

_LARGE = 100_000


def _trade() -> TradeInput:
    return TradeInput(
        entry_dt_ist=datetime(2026, 5, 15, 10, 0),
        exit_dt_ist=datetime(2026, 5, 15, 11, 0),
        instrument="NIFTY",
        option_type="CE",
        strike=2_500_000,
        expiry_date=date(2026, 5, 21),
        entry_bar_open_paisa=9_995,
        exit_bar_open_paisa=12_005,
        entry_bar_high_paisa=10_005,
        entry_bar_low_paisa=9_990,
        exit_bar_high_paisa=12_010,
        exit_bar_low_paisa=11_995,
        side="long_call",
        n_lots=1,
        broker="groww",
        gst_split="intra_state",
        trade_outcome="NORMAL_CLOSE",
    )


def _median(samples: list[float]) -> float:
    samples.sort()
    return samples[len(samples) // 2]


class TestOffModeIsFree:
    @pytest.mark.parametrize("flag", [None, "0"])
    def test_flag_probe_cost_is_below_one_bare_walk(
        self, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        """The whole OFF-mode seam addition (one env probe) is nil next to
        even ONE no-op walk over a large list — so it cannot hide a
        per-trade cost (both OFF spellings: unset default + kill switch)."""
        if flag is None:
            monkeypatch.delenv(exec_rust.ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(exec_rust.ENV_FLAG, flag)
        items = list(range(_LARGE))

        probe_samples: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            enabled = exec_rust.exec_rust_enabled()
            probe_samples.append(time.perf_counter() - t0)
            assert enabled is False
        probe = _median(probe_samples)

        walk_samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _item in items:
                pass
            walk_samples.append(time.perf_counter() - t0)
        walk = _median(walk_samples)

        assert probe < walk * 2.0, (
            f"OFF-mode flag probe ({probe * 1e6:.1f} µs) is not nil next to a bare "
            f"{_LARGE}-item walk ({walk * 1e6:.1f} µs)"
        )

    def test_off_compute_trade_cost_matches_the_direct_oracle_cost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OFF-arm seam cost ≈ the direct lot/rate/stack/breakdown chain it
        wraps (identical big work ± one env probe + one None-returning seam
        call per trade)."""
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        trade = _trade()
        entry_date = trade.entry_dt_ist.date()
        n_trades = 200

        def _direct() -> None:
            for _ in range(n_trades):
                lot = get_lot_size(OptionsUnderlying.NIFTY, entry_date)
                # Worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001): the stack
                # takes the four bar extremes, narrowed by the COMMON
                # ``require_bar_extremes`` seam exactly as compute_trade_cost does.
                entry_high, entry_low, exit_high, exit_low = require_bar_extremes(
                    entry_bar_high_paisa=trade.entry_bar_high_paisa,
                    entry_bar_low_paisa=trade.entry_bar_low_paisa,
                    exit_bar_high_paisa=trade.exit_bar_high_paisa,
                    exit_bar_low_paisa=trade.exit_bar_low_paisa,
                )
                stack = _charge_stack_py(
                    entry_high,
                    entry_low,
                    exit_high,
                    exit_low,
                    is_long=True,
                    lot_size=lot,
                    n_lots=trade.n_lots,
                    brokerage_round_trip_paisa=const.brokerage_paisa(trade.broker) * 2,
                    stt_rate=const.stt_options_rate_bps_x100(entry_date),
                    etc_rate=const.exchange_charge_bps_x100("NIFTY", entry_date),
                    sebi_rate=const.SEBI_FEE_BPS_X100,
                    ipft_rate=const.ipft_bps_x100("NIFTY"),
                    stamp_rate=const.STAMP_DUTY_OPTIONS_BPS_X100,
                    gst_rate=const.GST_TOTAL_BPS_X100,
                )
                # The validated frozen-breakdown construction is PART of the
                # work compute_trade_cost performs — mirror it so the A/B
                # compares like with like (± the seam probe only).
                TradeCostBreakdown(
                    n_shares_per_lot=lot,
                    n_shares=stack.n_shares,
                    buy_fill_paisa=stack.buy_fill_paisa,
                    sell_fill_paisa=stack.sell_fill_paisa,
                    buy_notional_paisa=stack.buy_notional_paisa,
                    sell_notional_paisa=stack.sell_notional_paisa,
                    brokerage_paisa=stack.brokerage_paisa,
                    stt_paisa=stack.stt_paisa,
                    exch_paisa=stack.exch_paisa,
                    sebi_paisa=stack.sebi_paisa,
                    ipft_paisa=stack.ipft_paisa,
                    stamp_paisa=stack.stamp_paisa,
                    gst_paisa=stack.gst_paisa,
                    slippage_paisa=stack.slippage_paisa,
                    total_charges_paisa=stack.total_charges_paisa,
                    gross_pnl_paisa=stack.gross_pnl_paisa,
                    net_pnl_paisa=stack.net_pnl_paisa,
                    used_exercise_branch=False,
                    n_orders_per_leg=1,
                    lot_size_basis="trade_date",
                )

        def _seam() -> None:
            for _ in range(n_trades):
                compute_trade_cost(trade)

        _direct()
        _seam()  # warm-up both
        direct_samples: list[float] = []
        seam_samples: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _direct()
            direct_samples.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            _seam()
            seam_samples.append(time.perf_counter() - t0)
        direct = _median(direct_samples)
        seam = _median(seam_samples)
        # Identical big work ± one env probe + a None-returning call + the
        # breakdown construction; 2.0× absorbs shared-CI timer noise while
        # still catching an accidental second charge-stack pass.
        assert seam < direct * 2.0, (
            f"OFF-mode compute_trade_cost ({seam * 1e3:.2f} ms / {n_trades} trades) is not "
            f"within the noise ceiling of the direct oracle chain ({direct * 1e3:.2f} ms)"
        )
