"""The worst-case-only fill law — the RESIDENT-KERNEL seam
(DEC-FILL-WORST-CASE-ONLY-001, on the DEC-FILL-EXEC-GRID-1M-001 dual feed).

The ``ResidentTradeSim`` half of the fill-law suite, split out of
``test_engine_trade_sim_fill_mode.py`` so the DEC-CICD-GATE-HONESTY-001
discovery gate's stem mapping credits these tests to
``brutex/engine/rust_trade_sim.py`` (the module they gate — the sibling file
stems to ``trade_sim`` and is invisible to the rust_trade_sim pairing).
Covers the ``_kernel_fill_law_current`` signature probe matrix, the
STALE-WHEEL construction demotion (``fill_law_kernel_stale`` — a wheel that
still exposes the retired mode flags would silently price the retired
PRECISE defaults through its flagless call, so the WHOLE object serves the
worst-case Python oracle instead; the stale fake carries the CURRENT
exec-grid 8-arg constructor so the fill-law probe — not the exec-grid one —
is the arm under test), the flagless four-argument kernel call, and the
real-kernel bit-parity vs the dual-feed oracle. The shared bar fixture is
imported from the sibling module (Golden Rule #11 COMMON — never a copy);
at ``tf_minutes == 1`` the same sequence serves both streams (design §6).
"""

from __future__ import annotations

from typing import Any

import pytest
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import (
    _STALE_KERNEL_FLAGS,
    ResidentTradeSim,
    _kernel_fill_law_current,
)
from brutex.engine.trade_sim import simulate_trades
from brutex.strategies.types import Side
from structlog.testing import capture_logs

from tests.unit.test_engine_trade_sim_fill_mode import _bar


def _window() -> tuple[list[Any], list[int]]:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 105, 98, 100),
        _bar(2, 85, 88, 80, 82),
        _bar(3, 90, 95, 85, 92),
    ]
    return bars, [1, 0, 1, 0]


def _resident(bars: list[Any], bits: list[int]) -> ResidentTradeSim:
    """The tf=1 degenerate dual-feed build (both streams = the same sequence)."""
    return ResidentTradeSim(bars, bits, tf_minutes=1, exec_bars_1m=bars)


class TestKernelServesTheOneLaw:
    """A current wheel serves worst-case bit-identical to the dual-feed oracle."""

    def test_real_kernel_bit_identical_to_oracle(self) -> None:
        bars, bits = _window()
        sim = _resident(bars, bits)
        if not sim.rust_active:  # pragma: no cover — wheel-less environment
            pytest.skip("fno_math wheel not installed")
        kernel = sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=1000)[0]
        oracle = simulate_trades(
            bars,
            mask_to_entry_signals(bits, 1),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            stop_loss_paisa=1000,
        )
        assert [(t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars) for t in kernel] == [
            (t.pnl_paisa, t.mfe_paisa, t.mae_paisa, t.holding_bars) for t in oracle
        ]
        # The worst-case bytes: entry at bar-1 HIGH 105, the bar-2 gap-through
        # stop (level 95, low 80) fills at the LOW — never the retired
        # precise open-entry / at-level fills.
        assert kernel[0].pnl_paisa == 8000 - 10500

    def test_simulate_masks_takes_no_policy_kwarg(self) -> None:
        # Killed-token pin: the batch entry carries NO mode/policy axis.
        import inspect

        params = inspect.signature(ResidentTradeSim.simulate_masks).parameters
        assert "policy" not in params
        assert "fill_mode" not in params


class TestStaleWheelDemotion:
    """A wheel predating the one-law kernel demotes to the oracle at construction."""

    def test_stale_flags_tuple_pinned(self) -> None:
        # The four retired mode flags a PRE-DEC wheel still exposes — their
        # DEFAULTS priced the retired PRECISE law, so their presence marks
        # the kernel stale (append-only detection vocabulary).
        assert _STALE_KERNEL_FLAGS == ("worst_case", "sl_next_open", "tp_next_open", "tp_first")

    def test_stale_kernel_demotes_whole_object_to_oracle(self, monkeypatch: Any) -> None:
        # Construction-time demotion: a stale wheel's kernel is DISCARDED
        # (self._rust = None, reason fill_law_kernel_stale) so every batch
        # rides the bit-exact worst-case oracle — never a silent precise
        # answer through the retired flags' defaults. The fake carries the
        # CURRENT exec-grid constructor (named ``arm_source`` parameter) so
        # the pre-construction ``_kernel_exec_grid_current`` probe passes and
        # the FILL-LAW probe is provably the demoting arm.
        import fno_math

        class _StaleKernel:
            def __init__(
                self,
                exec_open: list[int],
                exec_high: list[int],
                exec_low: list[int],
                exec_close: list[int],
                exec_is_forced: list[bool],
                exec_session_idx: list[int],
                arm_source: list[int],
                bar_bits: list[int],
            ) -> None:
                pass

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
                raise AssertionError("a stale kernel must never serve a batch")

        monkeypatch.setattr(fno_math, "ResidentSweepSim", _StaleKernel)
        bars, bits = _window()
        with capture_logs() as logs:
            sim = _resident(bars, bits)
        assert sim.rust_active is False
        stale_warns = [
            log
            for log in logs
            if log.get("event") == "rust_trade_sim.fallback"
            and log.get("reason") == "fill_law_kernel_stale"
        ]
        assert len(stale_warns) == 1
        served = sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=1000)[0]
        oracle = simulate_trades(
            bars,
            mask_to_entry_signals(bits, 1),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            stop_loss_paisa=1000,
        )
        assert [(t.pnl_paisa, t.mae_paisa) for t in served] == [
            (t.pnl_paisa, t.mae_paisa) for t in oracle
        ]

    def test_kernel_fill_law_probe_matrix(self) -> None:
        class _Current:
            def simulate_masks(self, masks: object, is_buy: bool, sl: object, tp: object) -> None:
                raise NotImplementedError  # signature probe only — never invoked

        class _StaleWorstCaseFlag:
            def simulate_masks(self, masks: object, *, worst_case: bool = False) -> None:
                raise NotImplementedError

        class _StaleTpFirst:
            def simulate_masks(self, masks: object, tp_first: bool = False) -> None:
                raise NotImplementedError

        class _NoAttr:
            pass

        class _NotCallable:
            simulate_masks = property()

        class _NoSignature:
            # ``inspect.signature(type)`` raises ValueError (no signature for
            # the builtin) — the conservative STALE arm: the oracle serves
            # the batch correctly either way.
            simulate_masks = type

        assert _kernel_fill_law_current(_Current()) is True
        assert _kernel_fill_law_current(_StaleWorstCaseFlag()) is False
        assert _kernel_fill_law_current(_StaleTpFirst()) is False
        assert _kernel_fill_law_current(_NoAttr()) is False
        assert _kernel_fill_law_current(_NotCallable()) is False
        assert _kernel_fill_law_current(_NoSignature()) is False

    def test_real_wheel_probes_current(self) -> None:
        # The wheel built from THIS tree's flagless kernel reads CURRENT.
        pytest.importorskip("fno_math")
        bars, bits = _window()
        sim = _resident(bars, bits)
        if sim._rust is None:  # pragma: no cover — wheel-less environment
            pytest.skip("fno_math wheel not installed")
        assert _kernel_fill_law_current(sim._rust) is True
