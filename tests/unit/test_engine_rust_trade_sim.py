"""Unit + parity tests for the resident Rust trade-sim seam.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #561). Exercises
:class:`brutex.engine.rust_trade_sim.ResidentTradeSim` to 100% line + branch:
construction validation, the ``n_bars`` view, the ``HOLD``/empty guards, both the
Rust path (the default when the ``fno_math`` wheel is built — CI) AND the
pure-Python fallback (forced by clearing ``_rust`` / shadowing the import), every
``FillPolicy`` → kernel-flag mapping, and BUY/SELL. The parity assertions pin the
seam's contract: its output equals the
:func:`brutex.engine.trade_sim.simulate_trades` oracle field-for-field.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from types import ModuleType

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import (
    FillPolicy,
    LevelFill,
    SameBarTie,
    simulate_trades,
)
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)  # a regular NSE trading Tuesday


def _ts(hh: int, mm: int) -> datetime:
    """UTC-aware timestamp for an IST wall-clock ``hh:mm`` (bars store UTC)."""
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """Build a valid 1-minute :class:`CandleBar` at IST ``hh:mm`` from rupee strings."""
    return CandleBar(
        timestamp=_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _feed() -> list[CandleBar]:
    """A small intraday feed with enough movement to open + close trades."""
    return [
        _bar(9, 15, "100", "101", "99", "100"),
        _bar(9, 16, "100", "110", "92", "100"),
        _bar(9, 17, "100", "108", "95", "104"),
        _bar(9, 18, "104", "112", "100", "108"),
        _bar(9, 19, "108", "115", "85", "100"),
    ]


def _oracle(
    bars: list[CandleBar],
    bar_bits: list[int],
    mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    policy: FillPolicy,
) -> list:
    """The pure-Python oracle trades for one mask (what the seam must match)."""
    return simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, mask),
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        policy=policy,
    )


# ── construction validation ──────────────────────────────────────────────────


class TestConstruction:
    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            ResidentTradeSim([], [], tf_minutes=1)

    def test_bar_bits_length_mismatch_rejected(self) -> None:
        bars = _feed()
        with pytest.raises(ValueError, match="bar_bits length"):
            ResidentTradeSim(bars, [0, 0], tf_minutes=1)

    def test_tf_minutes_below_one_rejected(self) -> None:
        bars = _feed()
        bar_bits = build_bar_bits(bars)
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            ResidentTradeSim(bars, bar_bits, tf_minutes=0)

    def test_n_bars_reports_window_length(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.n_bars == len(bars)

    def test_missing_kernel_falls_back_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Shadow `import fno_math` with a module lacking ResidentSweepSim so the
        # `except (ImportError, AttributeError)` branch is covered even when the
        # real wheel is installed (CI). The seam must still construct + simulate.
        fake = ModuleType("fno_math")
        monkeypatch.setitem(sys.modules, "fno_math", fake)
        bars = _feed()
        bar_bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        out = sim.simulate_masks([0], direction=Side.BUY)
        assert out == [
            _oracle(
                bars,
                bar_bits,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
                policy=FillPolicy(),
            )
        ]


# ── simulate_masks guards ────────────────────────────────────────────────────


class TestGuards:
    def test_hold_direction_rejected(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        with pytest.raises(ValueError, match="must be BUY or SELL"):
            sim.simulate_masks([0], direction=Side.HOLD)

    def test_empty_masks_returns_empty(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.simulate_masks([], direction=Side.BUY) == []


# ── parity: seam == oracle (Rust path AND forced fallback) ───────────────────


_POLICIES = [
    FillPolicy(),  # AT_LEVEL / AT_LEVEL / SL_FIRST (default)
    FillPolicy(stop_loss=LevelFill.NEXT_OPEN),
    FillPolicy(take_profit=LevelFill.NEXT_OPEN),
    FillPolicy(same_bar=SameBarTie.TP_FIRST),
    FillPolicy(
        stop_loss=LevelFill.NEXT_OPEN,
        take_profit=LevelFill.NEXT_OPEN,
        same_bar=SameBarTie.TP_FIRST,
    ),
]


class TestParity:
    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize("policy", _POLICIES)
    @pytest.mark.parametrize(
        ("sl", "tp"),
        [(None, None), (300, None), (None, 400), (300, 400)],
    )
    def test_seam_matches_oracle_rust_path(
        self,
        direction: Side,
        policy: FillPolicy,
        sl: int | None,
        tp: int | None,
    ) -> None:
        bars = _feed()
        bar_bits = build_bar_bits(bars)
        masks = [0, 1, 1 << 63]  # fire-every / sometimes / never
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        got = sim.simulate_masks(
            masks,
            direction=direction,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            policy=policy,
        )
        expected = [
            _oracle(
                bars,
                bar_bits,
                m,
                direction=direction,
                tf_minutes=1,
                stop_loss_paisa=sl,
                take_profit_paisa=tp,
                policy=policy,
            )
            for m in masks
        ]
        assert got == expected

    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    def test_seam_matches_oracle_fallback_path(self, direction: Side) -> None:
        # Force the pure-Python fallback by clearing the resident kernel handle.
        bars = _feed()
        bar_bits = build_bar_bits(bars)
        masks = [0, 1, 1 << 63]
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        # Exercise the pure-Python fallback branch on purpose.
        sim._rust = None
        got = sim.simulate_masks(
            masks,
            direction=direction,
            stop_loss_paisa=300,
            take_profit_paisa=400,
        )
        expected = [
            _oracle(
                bars,
                bar_bits,
                m,
                direction=direction,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=400,
                policy=FillPolicy(),
            )
            for m in masks
        ]
        assert got == expected

    def test_rust_and_fallback_agree(self) -> None:
        # The two code paths must produce identical trades (the whole contract).
        bars = _feed()
        bar_bits = build_bar_bits(bars)
        masks = [0, 1, 2, 3]
        rust = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        rust_out = rust.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=300, take_profit_paisa=400
        )
        fallback = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        # Force the fallback path for the cross-path comparison.
        fallback._rust = None
        fb_out = fallback.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=300, take_profit_paisa=400
        )
        assert rust_out == fb_out
