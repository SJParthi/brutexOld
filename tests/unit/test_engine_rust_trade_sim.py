"""Unit + parity tests for the resident Rust trade-sim seam.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Exercises
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


# ── parity on a NON-regular session (DEC-SESSION-FORCED-EXIT-001) ────────────
#
# The Rust kernel's precomputed `forced` flags must be SESSION-AWARE — byte-
# identical to the simulate_trades oracle — on a half-day / Muhurat session, not
# just the regular 15:20 day. These tests would FAIL if ResidentTradeSim kept
# deriving `forced` against the hardcoded 15:20 (its flags would force-exit
# every evening/afternoon bar while the oracle exits at the real close − 10min).

_AFTERNOON_MUHURAT = date(2025, 10, 21)  # 13:45-14:45 IST → forced exit 14:35


def _muhurat_ts(hh: int, mm: int) -> datetime:
    """UTC-aware timestamp for an IST ``hh:mm`` on the 2025 afternoon Muhurat."""
    return datetime(
        _AFTERNOON_MUHURAT.year,
        _AFTERNOON_MUHURAT.month,
        _AFTERNOON_MUHURAT.day,
        hh,
        mm,
        tzinfo=IST,
    ).astimezone(UTC)


def _muhurat_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_muhurat_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


class TestSessionAwareForcedExitParity:
    def test_rust_kernel_forced_flags_session_aware_match_oracle(self) -> None:
        # An afternoon-Muhurat feed straddling the 14:35 forced exit. The Rust
        # kernel + the simulate_trades oracle must agree field-for-field (the
        # kernel's `forced` array is now resolved against the 14:35 boundary).
        bars = [
            _muhurat_bar(14, 0, "100", "100", "100", "100"),
            _muhurat_bar(14, 33, "100", "100", "100", "100"),
            _muhurat_bar(14, 34, "100", "100", "100", "100"),  # NOT forced
            _muhurat_bar(14, 35, "100.20", "101", "100", "100.30"),  # forced @ 14:35
            _muhurat_bar(14, 36, "200", "200", "200", "200"),  # never reached
        ]
        bar_bits = build_bar_bits(bars)
        masks = [0, 1, 1 << 63]
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        got = sim.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=100_000, take_profit_paisa=100_000
        )
        expected = [
            _oracle(
                bars,
                bar_bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=100_000,
                take_profit_paisa=100_000,
                policy=FillPolicy(),
            )
            for m in masks
        ]
        assert got == expected
        # Anti-vacuity: the fire-every-bar mask actually opens AND force-exits at
        # the 14:35 Muhurat boundary (NOT 15:20, NOT skipped) — proves the flags
        # are session-aware, not just "two paths agree on nothing".
        fire_trades = got[0]
        assert len(fire_trades) == 1
        assert fire_trades[0].exit_ts == _muhurat_ts(14, 35)
        assert fire_trades[0].pnl_paisa == 20  # forced at OPEN 100.20, not 200

    def test_rust_and_fallback_agree_on_muhurat(self) -> None:
        # The Rust path and the forced pure-Python fallback agree on a Muhurat
        # session too (the whole contract, on a non-regular day).
        bars = [
            _muhurat_bar(14, 30, "100", "100", "100", "100"),
            _muhurat_bar(14, 34, "100", "100", "100", "100"),
            _muhurat_bar(14, 35, "100.50", "101", "100", "100.90"),
            _muhurat_bar(14, 36, "100", "100", "100", "100"),
        ]
        bar_bits = build_bar_bits(bars)
        masks = [0, 1, 2, 3]
        rust = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        rust_out = rust.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=100_000, take_profit_paisa=100_000
        )
        fallback = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        fallback._rust = None
        fb_out = fallback.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=100_000, take_profit_paisa=100_000
        )
        assert rust_out == fb_out

    def test_rust_kernel_multi_day_forced_flags_match_oracle(self) -> None:
        # A SINGLE window spanning day-1 regular (exits 15:20) + day-2 Muhurat
        # (exits 14:35) pins the 1-slot date-cache RE-RESOLVE branch in the
        # kernel's `forced` precompute against the oracle.
        bars = [
            _bar(15, 18, "100", "100", "100", "100"),  # day-1 regular, near 15:20
            _bar(15, 20, "100.30", "101", "100", "100.50"),  # day-1 forced @ 15:20
            _muhurat_bar(14, 34, "100", "100", "100", "100"),  # day-2 NOT forced
            _muhurat_bar(14, 35, "100.10", "101", "100", "100.30"),  # day-2 forced @ 14:35
        ]
        bar_bits = build_bar_bits(bars)
        masks = [0, 1]
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        got = sim.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=100_000, take_profit_paisa=100_000
        )
        expected = [
            _oracle(
                bars,
                bar_bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=100_000,
                take_profit_paisa=100_000,
                policy=FillPolicy(),
            )
            for m in masks
        ]
        assert got == expected


# ── cross-session pending-entry drop parity (R2-OC-2) ─────────────────────────

_NEXT_DAY = date(2024, 1, 3)  # the next NSE session (Wednesday)


def _next_day_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """A day-2 (next-session) 1-minute bar at IST ``hh:mm``."""
    ts = datetime(_NEXT_DAY.year, _NEXT_DAY.month, _NEXT_DAY.day, hh, mm, tzinfo=IST).astimezone(
        UTC
    )
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


class TestCrossSessionPendingEntryParity:
    """R2-OC-2: the kernel drops a cross-session pending entry like the oracle."""

    def _two_session_feed(self) -> tuple[list[CandleBar], list[int]]:
        """Two-session feed + bar_bits whose bit0 fires ONLY on day-1's last bar."""
        bars = [
            _bar(15, 28, "100", "101", "99", "100"),
            _bar(15, 29, "100", "101", "99", "100"),  # signal (bit0) — session's last bar
            _next_day_bar(9, 15, "119", "120", "118", "119"),  # +19 overnight gap
            _next_day_bar(9, 16, "119.50", "120.50", "119", "119.50"),
        ]
        signals = [False, True, False, False]
        raw = build_bar_bits(bars)
        bits = [(b | 1) if sig else (b & ~1) for b, sig in zip(raw, signals, strict=True)]
        return bars, bits

    def test_rust_kernel_drops_cross_session_entry_and_matches_oracle(self) -> None:
        # Parity AND the absolute verdict: BOTH sides produce ZERO trades for a
        # last-bar signal followed by the next session's open (pre-fix both
        # produced one trade entering at 119.00 = the overnight gap at zero
        # slippage — the absolute pin catches a lock-step regression).
        bars, bits = self._two_session_feed()
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        got = sim.simulate_masks([1], direction=Side.BUY)
        expected = [
            _oracle(
                bars,
                bits,
                1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
                policy=FillPolicy(),
            )
        ]
        assert got == expected
        assert got == [[]]  # absolute: no overnight trade on EITHER side

    def test_rust_and_fallback_agree_on_cross_session_drop(self) -> None:
        # The forced pure-Python fallback path takes the same drop decision.
        bars, bits = self._two_session_feed()
        rust = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert rust._rust is not None  # the wheel is built in CI — a real Rust run
        fallback = ResidentTradeSim(bars, bits, tf_minutes=1)
        fallback._rust = None
        assert rust.simulate_masks([1], direction=Side.BUY) == fallback.simulate_masks(
            [1], direction=Side.BUY
        )

    def test_rust_kernel_next_session_signal_still_fills(self) -> None:
        # A genuine day-2 signal still fills within day-2 (the drop must not eat
        # it): signal on day-1 15:29 AND day-2 09:15 → ONE trade entering 09:16.
        bars = [
            _bar(15, 29, "100", "101", "99", "100"),
            _next_day_bar(9, 15, "119", "120", "118", "119"),
            _next_day_bar(9, 16, "119.50", "120.50", "119", "119.60"),
            _next_day_bar(9, 17, "119.80", "120.80", "119.30", "120"),
        ]
        signals = [True, True, False, False]
        raw = build_bar_bits(bars)
        bits = [(b | 1) if sig else (b & ~1) for b, sig in zip(raw, signals, strict=True)]
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        got = sim.simulate_masks([1], direction=Side.BUY)
        expected = [
            _oracle(
                bars,
                bits,
                1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
                policy=FillPolicy(),
            )
        ]
        assert got == expected
        assert len(got[0]) == 1
        assert got[0][0].entry_ts == bars[2].timestamp  # 09:16 day-2, NOT 09:15
