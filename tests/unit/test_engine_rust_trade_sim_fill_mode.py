"""WORST_CASE fill mode — the RESIDENT-KERNEL seam of DEC-FILL-WORST-CASE-001.

The ``ResidentTradeSim`` half of the fill-mode suite, split out of
``test_engine_trade_sim_fill_mode.py`` so the DEC-CICD-GATE-HONESTY-001
discovery gate's stem mapping credits these tests to
``brutex/engine/rust_trade_sim.py`` (the module they gate — the sibling
file stems to ``trade_sim`` and is invisible to the rust_trade_sim
pairing; the coverage gap that redded PR #1522's discovery gate on lines
380-384 / 964-967 / 1018). Covers the ``_kernel_accepts_worst_case``
feature-detection probe matrix, the legacy-wheel
``fill_mode_kernel_missing`` demotion (memo + warn-once), the PRECISE
seven-argument back-compat call, and the real-kernel WORST_CASE
bit-parity vs the Python oracle (the 8-argument kernel call). The
shared bar fixture + WORST policy are imported from the sibling module
(Golden Rule #11 COMMON — never a copy).
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim, _kernel_accepts_worst_case
from brutex.engine.trade_sim import simulate_trades
from brutex.strategies.types import Side
from structlog.testing import capture_logs

from tests.unit.test_engine_trade_sim_fill_mode import _WORST, _bar


class TestKernelSeamFeatureDetection:
    """The resident-kernel seam: real-wheel parity + the legacy-wheel demotion."""

    @staticmethod
    def _window() -> tuple[list[Any], list[int]]:
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
            def simulate_masks(self, masks: object, *, worst_case: bool = False) -> None:
                raise NotImplementedError  # signature probe only — never invoked

        class _Old:
            def simulate_masks(self, masks: object, tp_first: bool = False) -> None:
                raise NotImplementedError  # signature probe only — never invoked

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
