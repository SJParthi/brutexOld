"""OFF-mode cost gate for the flag-gated Rust exec-cores seam.

DEC-EXEC-RUST-CORE-001's hard constraint (mirrors
``test_sweep_rust_metrics_off_cost.py``): with ``BRUTEX_EXEC_RUST`` OFF —
since DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215) the seam default is
ON, so OFF means an EXPLICIT kill-switch value (``"0"``/``"off"``); the
unset arm now engages the crate and is no longer an OFF spelling —
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
    @pytest.mark.parametrize("flag", ["0", "off"])
    def test_flag_probe_cost_is_below_one_bare_walk(
        self, monkeypatch: pytest.MonkeyPatch, flag: str
    ) -> None:
        """The whole OFF-mode seam addition (one env probe) is nil next to
        even ONE no-op walk over a large list — so it cannot hide a
        per-trade cost. Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the seam
        default is ON, so OFF is an explicit kill-switch value ('0'/'off') —
        the unset arm now engages the crate and is not an OFF spelling."""
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
        call per trade).

        FIDELITY (DEC-BENCH-SLOPE-BOUND-001): ``_direct`` mirrors the seam's
        per-trade work EXACTLY — the three in-loop ``entry_dt_ist.date()``
        calls, the ``OptionsUnderlying(trade.instrument)`` enum-from-value
        construction inside ``_lookup_lot_size``, and the
        ``side.startswith("long")`` read. The prior shape hoisted the date out
        of the loop and passed the enum MEMBER + a literal ``is_long=True``, so
        the control arm did strictly LESS work than the subject and the
        docstring's "identical big work" claim was false: the unloaded true
        ratio measured 1.163 rather than ~1.0, leaving only 1.72× headroom
        under the 2.0 ceiling. Restoring parity measures what the docstring
        says it measures — the env probe + the None-returning seam call — and
        the teeth are UNCHANGED: an accidental extra pass inside
        ``compute_trade_cost`` is still work ``_direct`` does not do, so it
        still inflates the ratio.

        ESTIMATOR (DEC-BENCH-SLOPE-BOUND-001): the statistic is the median of
        the PER-PAIR ratios over interleaved samples, never two independently
        estimated medians. Independent medians can draw from different
        iterations, so a scheduler stall / co-tenant burst landing in ONE arm's
        list shifts that median with nothing to cancel it. Reproduced twice
        2026-07-25 on an emulated contended 2-core runner (6 busy loops,
        ``taskset -c 0,1``): run A trial 6 read ``direct=8.80 ms`` against a
        normal ``seam=23.84 ms`` → unpaired 2.71× FAIL where the paired median
        over the SAME samples was 1.38×; run B trial 4 read unpaired 2.56× vs
        paired 1.29×. In both the DENOMINATOR was the outlier, not the
        numerator. Pairing alone was not sufficient (run A had a separate trial
        at paired 2.05×) — which is why the fidelity fix above ships WITH it,
        restoring the headroom that makes the pair robust. The ceiling is
        UNCHANGED at 2.0: a genuine regression inflates EVERY pair, so the
        median moves with it.
        """
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        trade = _trade()
        n_trades = 200

        def _direct() -> None:
            for _ in range(n_trades):
                # Mirror `_lookup_lot_size`: enum-from-value + the .date() call.
                lot = get_lot_size(OptionsUnderlying(trade.instrument), trade.entry_dt_ist.date())
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
                    is_long=trade.side.startswith("long"),
                    lot_size=lot,
                    n_lots=trade.n_lots,
                    brokerage_round_trip_paisa=const.brokerage_paisa(trade.broker) * 2,
                    stt_rate=const.stt_options_rate_bps_x100(trade.entry_dt_ist.date()),
                    etc_rate=const.exchange_charge_bps_x100(
                        trade.instrument, trade.entry_dt_ist.date()
                    ),
                    sebi_rate=const.SEBI_FEE_BPS_X100,
                    ipft_rate=const.ipft_bps_x100(trade.instrument),
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
        pair_ratios: list[float] = []
        direct_samples: list[float] = []
        seam_samples: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _direct()
            direct_i = time.perf_counter() - t0
            t0 = time.perf_counter()
            _seam()
            seam_i = time.perf_counter() - t0
            direct_samples.append(direct_i)
            seam_samples.append(seam_i)
            pair_ratios.append(seam_i / direct_i)
        # Identical big work ± one env probe + a None-returning call; 2.0×
        # absorbs shared-CI timer noise while still catching an accidental
        # second charge-stack pass. The statistic is the PER-PAIR median (see
        # the ESTIMATOR paragraph) — a stall inside one arm's window is
        # cancelled by its own paired neighbour, never by an unrelated
        # iteration's sample.
        ratio = _median(pair_ratios)
        assert ratio < 2.0, (
            f"OFF-mode compute_trade_cost is not within the noise ceiling of the direct "
            f"oracle chain: paired median {ratio:.2f}× over {len(pair_ratios)} interleaved "
            f"rounds of {n_trades} trades (arm medians seam "
            f"{_median(seam_samples) * 1e3:.2f} ms vs direct "
            f"{_median(direct_samples) * 1e3:.2f} ms)"
        )
