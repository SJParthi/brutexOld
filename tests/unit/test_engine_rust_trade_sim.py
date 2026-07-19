"""Unit + parity tests for the resident Rust trade-sim seam.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Exercises
:class:`brutex.engine.rust_trade_sim.ResidentTradeSim` to 100% line + branch:
construction validation, the ``n_bars`` view, the ``HOLD``/empty guards, both the
Rust path (the default when the ``fno_math`` wheel is built — CI) AND the
pure-Python fallback (forced by clearing ``_rust`` / shadowing the import), and
BUY/SELL — all under the ONE hardcoded worst-case fill law
(DEC-FILL-WORST-CASE-ONLY-001; the retired ``FillPolicy`` → kernel-flag mapping
is deleted). The parity assertions pin the seam's contract: its output equals
the :func:`brutex.engine.trade_sim.simulate_trades` oracle field-for-field.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from types import ModuleType
from typing import cast

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import U64_MASK, mask_to_entry_signals
from brutex.engine.reference_series import TradeReferenceStamper

# Whitebox friend access (DEC-P0-017 per-site convention): the guard seam is
# module-private; each COLUMN of its envelope check is pinned directly here
# because a single-column corrupt bar is unrepresentable through CandleBar's
# OHLC invariant (see TestEnvelopeGuardSeamPerColumn).
from brutex.engine.rust_trade_sim import (
    _MAX_KERNEL_PRICE_PAISA,  # pyright: ignore[reportPrivateUsage]
    _TAIL_HOLE_WARN_CAP,  # pyright: ignore[reportPrivateUsage]
    _U128_MASK,  # pyright: ignore[reportPrivateUsage]
    ResidentTradeSim,
    _construction_fallback_reason,  # pyright: ignore[reportPrivateUsage]
    _warn_session_tail_holes,  # pyright: ignore[reportPrivateUsage]
)
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import simulate_trades
from brutex.strategies.types import Side
from structlog.testing import capture_logs
from structlog.typing import EventDict

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
) -> list:
    """The pure-Python oracle trades for one mask (what the seam must match)."""
    return simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, mask),
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
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

    def test_out_of_coverage_weekday_bar_refuses_loud_at_construction(self) -> None:
        # Calendar-correctness pack: the OLD silent behaviour is GONE — a bar
        # dated on a weekday OUTSIDE the published holiday span used to get a
        # silently-FABRICATED regular 15:20 forced-exit flag; construction now
        # raises HolidayDataUnavailableError via the strict
        # ``get_session_checked``, in lock-step with the streaming walker
        # (byte-parity preserved: both paths refuse the same date).
        from brutex.calendars.nse import HolidayDataUnavailableError

        out_day = date(2027, 1, 4)  # Monday, above the 2026 coverage ceiling
        ts = datetime(out_day.year, out_day.month, out_day.day, 9, 15, tzinfo=IST).astimezone(UTC)
        bars = [
            CandleBar(
                timestamp=ts,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=0,
                open_interest=None,
            )
        ]
        with pytest.raises(HolidayDataUnavailableError):
            ResidentTradeSim(bars, [0], tf_minutes=1)

    def test_multi_pathology_window_raises_the_calendar_error_first(self) -> None:
        # Refuter RF2 C-1 regression: the forced/session loop must run BEFORE
        # the price-column derivation (main's pre-W2-rider statement order),
        # so a window holding BOTH an out-of-coverage weekday bar AND an
        # over-precision price — each ACCEPTED by CandleBar validation
        # (Decimal("1E+40") is positive + ordered) — raises the class
        # contract's HolidayDataUnavailableError, never the price snap's
        # decimal.InvalidOperation.
        from brutex.calendars.nse import HolidayDataUnavailableError

        def _dated(day: date, price: str) -> CandleBar:
            ts = datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST).astimezone(UTC)
            return CandleBar(
                timestamp=ts,
                open=Decimal(price),
                high=Decimal(price),
                low=Decimal(price),
                close=Decimal(price),
                volume=100,
                open_interest=None,
            )

        out_of_coverage = _dated(date(2030, 1, 2), "100")  # Wednesday, > 2026 ceiling
        over_precision = _dated(date(2026, 1, 5), "1E+40")  # validates; snap raises
        with pytest.raises(HolidayDataUnavailableError):
            ResidentTradeSim([out_of_coverage, over_precision], [0, 0], tf_minutes=1)

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


class TestParity:
    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize(
        ("sl", "tp"),
        [(None, None), (300, None), (None, 400), (300, 400)],
    )
    def test_seam_matches_oracle_rust_path(
        self,
        direction: Side,
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
        assert fire_trades[0].pnl_paisa == 0  # forced at the bar LOW 100.00, not 200

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
            )
            for m in masks
        ]
        assert got == expected

    def test_rust_kernel_extended_outage_day_matches_oracle_at_1650(self) -> None:
        # DEC-EXTENDED-SESSION-OUTAGE-001 (fails-before pin, Rust ≡ Python):
        # on the 2021-02-24 extended day (close 17:00 IST at NSE + BSE + MSEI,
        # SEBI press releases verified 2026-07-11) the kernel's forced flags
        # resolve the 16:50 boundary — a 15:25 bar is NOT forced (the pre-fix
        # resolver forced every bar >= 15:20), and both paths agree
        # field-for-field.
        outage = date(2021, 2, 24)

        def _outage_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
            ts = datetime(outage.year, outage.month, outage.day, hh, mm, tzinfo=IST).astimezone(UTC)
            return CandleBar(
                timestamp=ts,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(lo),
                close=Decimal(c),
                volume=0,
                open_interest=None,
            )

        bars = [
            _outage_bar(15, 18, "100", "100", "100", "100"),
            _outage_bar(15, 25, "100", "100", "100", "100"),  # NOT forced (pre-fix: forced)
            _outage_bar(16, 49, "100", "100", "100", "100"),  # NOT forced
            _outage_bar(16, 50, "100.60", "101", "100", "100.80"),  # forced @ 16:50
            _outage_bar(16, 51, "200", "200", "200", "200"),  # never reached
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
            )
            for m in masks
        ]
        assert got == expected
        # Anti-vacuity: the fire-every-bar mask opens AND force-exits at the
        # 16:50 extended boundary (NOT 15:20, NOT the 200-print next bar).
        # Worst-case fill hand-derivation (DEC-FILL-WORST-CASE-ONLY-001 — the
        # member authored this pin pre-#1825's open-anchored law): BUY entry
        # fills at bar[1]'s adverse HIGH 100.00; the forced exit fills at the
        # decided bar[3]'s adverse LOW 100.00 → pnl 0. The 16:50 bar's prices
        # still provably fold: mfe 60 paisa = its 100.60 open over the 100.00
        # entry. A 15:20-forced exit or a bar[4] leak (200-print → pnl
        # 10_000 paisa) would fail BOTH the exit_ts and the pnl pins.
        fire_trades = got[0]
        assert len(fire_trades) == 1
        assert fire_trades[0].exit_ts == bars[3].timestamp
        assert fire_trades[0].pnl_paisa == 0  # adverse LOW 100.00, never 200
        assert fire_trades[0].mfe_paisa == 60  # the 100.60 open registered

    def test_rust_and_fallback_agree_on_extended_outage_day(self) -> None:
        # The Rust path and the forced pure-Python fallback agree on the
        # extended day too (the whole contract on the new session tier).
        outage = date(2021, 2, 24)
        bars = [
            CandleBar(
                timestamp=datetime(
                    outage.year, outage.month, outage.day, hh, mm, tzinfo=IST
                ).astimezone(UTC),
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(lo),
                close=Decimal(c),
                volume=0,
                open_interest=None,
            )
            for hh, mm, o, h, lo, c in [
                (16, 40, "100", "100", "100", "100"),
                (16, 49, "100", "100", "100", "100"),
                (16, 50, "100.50", "101", "100", "100.90"),
                (16, 51, "100", "100", "100", "100"),
            ]
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
            )
        ]
        assert got == expected
        assert len(got[0]) == 1
        assert got[0][0].entry_ts == bars[2].timestamp  # 09:16 day-2, NOT 09:15


# ── kernel-envelope guards (DEC-SWEEP-RUST-TRADESIM-WIRE-001 + U128-KERNEL-001) ─
#
# TWO compiled kernels (u64 + u128, one shared MaskWord core) #1090-validate
# every paisa input against MAX_PRICE_PAISA = i64::MAX / 8. Wide (>64-bit,
# ≤ u128) bar_bits/masks are Rust-SERVED (DEC-TRADE-SIM-U128-KERNEL-001); an
# input beyond even the u128 envelope must route to the bit-exact Python
# oracle — LOUDLY (one structlog WARNING) — never surface as a PyO3
# OverflowError / kernel ValueError the pure-Python engine would not have
# raised (wheel state must never change behaviour).


def _fallback_events(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "rust_trade_sim.fallback"]


def _call_fallback_events(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "rust_trade_sim.call_fallback"]


class TestRustActiveProperty:
    def test_true_when_the_wheel_serves_the_window(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is True  # the wheel is built in CI

    def test_false_on_the_forced_fallback(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        sim._rust = None
        assert sim.rust_active is False


class TestConstructionEnvelopeGuards:
    def test_wheel_missing_warns_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = ModuleType("fno_math")
        monkeypatch.setitem(sys.modules, "fno_math", fake)
        bars = _feed()
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"
        assert events[0]["n_bars"] == len(bars)

    def test_bar_bits_wide_window_served_by_u128_kernel(self) -> None:
        # DEC-TRADE-SIM-U128-KERNEL-001: a bar_bits value in (U64_MASK,
        # _U128_MASK] is NO LONGER a fallback — the constructor builds the
        # u128 kernel and the window is Rust-served (rust_active True), with
        # trades bit-identical to the oracle for u64-fit AND wide masks.
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] |= 1 << 100  # inside the u128 headroom (MAX_VOCAB_BITS = 128)
        masks = [0, 1, 1 << 100]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        assert _fallback_events(logs) == []
        with capture_logs() as call_logs:
            got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(call_logs) == []  # the wide kernel served it
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        assert got == expected
        # Anti-vacuity: the wide mask actually produced a trade through Rust.
        assert len(got[2]) >= 1

    def test_bar_bits_beyond_u128_falls_back_and_matches_oracle(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] |= 1 << 130  # beyond even the u128 headroom (MAX_VOCAB_BITS = 128)
        masks = [0, 1]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "bar_bits_beyond_u128"
        got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        assert got == expected

    def test_bar_bits_wide_window_on_stale_wheel_falls_back_wheel_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Old-shim+old-.so posture (monkeypatched attribute): a wheel exposing
        # ResidentSweepSim but NOT ResidentSweepSimU128 — the wide-window
        # construction lands in the SAME except (ImportError, AttributeError)
        # arm -> graceful whole-object oracle with reason "wheel_missing".
        import fno_math as real_fno_math

        fake = ModuleType("fno_math")
        fake.ResidentSweepSim = real_fno_math.ResidentSweepSim  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fno_math", fake)
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] |= 1 << 100
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"
        got = sim.simulate_masks([1 << 100], direction=Side.BUY)
        assert got == [
            _oracle(
                bars,
                bits,
                1 << 100,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]

    @pytest.mark.parametrize("rupees", ["200000000000000000", "-200000000000000000"])
    def test_price_out_of_envelope_falls_back_and_matches_oracle(self, rupees: str) -> None:
        """A price beyond ±(i64::MAX/8) paisa never crosses the FFI boundary.

        2·10¹⁷ rupees = 2·10¹⁹ paisa > the #1090 ceiling (≈1.15·10¹⁸) — the
        kernel ctor would raise ValueError; the wrapper routes to the oracle
        (which simulates arbitrary-precision ints) instead, both signs.
        Hand-crafted bar_bits: Stage-1A's own kernels cannot digest such a
        price either (its VWAP paisa crosses an i64 FFI edge upstream), so in
        production the corruption fails EARLIER — this pins the COMMON seam's
        contract for direct callers of the wrapper.
        """
        big = Decimal(rupees)
        bars = [*_feed()]
        bars[1] = CandleBar(
            timestamp=bars[1].timestamp,
            open=big,
            high=big + 1,
            low=big - 1,
            close=big,
            volume=0,
            open_interest=None,
        )
        bits = [1 if i % 2 == 0 else 0 for i in range(len(bars))]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "price_out_of_envelope"
        got = sim.simulate_masks([0], direction=Side.BUY)
        assert got == [
            _oracle(
                bars,
                bits,
                0,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]

    @pytest.mark.parametrize("column", ["high", "low"])
    def test_single_column_corruption_falls_back_and_matches_oracle(self, column: str) -> None:
        """A bar corrupt in ONLY one price column still trips the guard.

        The two SINGLE-column corruptions representable under CandleBar's OHLC
        invariant (``low <= open, close <= high``): a huge positive ``high``
        (everything else in envelope) and a huge negative ``low``. A corrupt
        ``open``/``close`` cannot exist alone through the public ctor — it
        drags ``high``/``low`` out with it — so those columns' mutation teeth
        live in :class:`TestEnvelopeGuardSeamPerColumn` at the guard seam.
        """
        big = Decimal("200000000000000000")  # 2·10¹⁹ paisa > the #1090 ceiling
        bars = [*_feed()]
        base = bars[1]
        bars[1] = CandleBar(
            timestamp=base.timestamp,
            open=base.open,
            high=big if column == "high" else base.high,
            low=-big if column == "low" else base.low,
            close=base.close,
            volume=0,
            open_interest=None,
        )
        bits = [1 if i % 2 == 0 else 0 for i in range(len(bars))]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "price_out_of_envelope"
        got = sim.simulate_masks([1], direction=Side.BUY)
        assert got == [
            _oracle(
                bars,
                bits,
                1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]

    def test_negative_bar_bits_falls_back_and_matches_oracle(self) -> None:
        """A NEGATIVE bar_bits value (API misuse only) routes to the oracle too.

        `build_bar_bits` emits only non-negative ints, but a hand-rolled
        negative int would raise PyO3 ``OverflowError`` at EITHER ``Vec``
        edge (u64 or u128) where the wheel-absent Python arm happily simulates
        (Python's infinite-precision ``&``) — the exact
        wheel-present-vs-absent asymmetry class the guard closes, in BOTH
        directions.
        """
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] = -1
        masks = [0, 1]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "bar_bits_beyond_u128"
        got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        assert got == [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]


class TestEnvelopeGuardSeamPerColumn:
    """Per-COLUMN teeth for `_construction_fallback_reason` (mutation-kill).

    The integration fixtures cannot corrupt ``open``/``close`` alone (CandleBar
    enforces ``low <= open, close <= high``, so a huge open/close drags a
    sibling column out of envelope with it) — a mutant dropping ONE column from
    the guard's ``(open_p, high_p, low_p, close_p)`` tuple would survive any
    CandleBar-built fixture for those columns. The guard takes RAW int columns,
    so each column is pinned here one at a time, both signs, plus the exact
    ceiling boundary (in-envelope) and the bar_bits range.
    """

    _N = 4

    def _clean_columns(self) -> dict[str, list[int]]:
        return {
            "bar_bits": [1, 0, 1, 0],
            "open_p": [10_000] * self._N,
            "high_p": [10_150] * self._N,
            "low_p": [9_850] * self._N,
            "close_p": [10_020] * self._N,
        }

    def _reason(self, columns: dict[str, list[int]]) -> str | None:
        return _construction_fallback_reason(
            columns["bar_bits"],
            columns["open_p"],
            columns["high_p"],
            columns["low_p"],
            columns["close_p"],
        )

    def test_clean_window_has_no_reason(self) -> None:
        assert self._reason(self._clean_columns()) is None

    def test_exact_ceiling_boundary_is_in_envelope_every_column(self) -> None:
        """±MAX_PRICE_PAISA exactly is kernel-valid on EVERY column (strict >)."""
        columns = self._clean_columns()
        for name in ("open_p", "high_p", "low_p", "close_p"):
            columns[name][2] = _MAX_KERNEL_PRICE_PAISA
            columns[name][3] = -_MAX_KERNEL_PRICE_PAISA
        assert self._reason(columns) is None

    @pytest.mark.parametrize("column", ["open_p", "high_p", "low_p", "close_p"])
    @pytest.mark.parametrize("sign", [1, -1])
    def test_single_out_of_envelope_column_is_detected(self, column: str, sign: int) -> None:
        columns = self._clean_columns()
        columns[column][1] = sign * (_MAX_KERNEL_PRICE_PAISA + 1)
        assert self._reason(columns) == "price_out_of_envelope"

    @pytest.mark.parametrize("bad", [_U128_MASK + 1, 1 << 200, -1])
    def test_bar_bits_outside_u128_range_is_detected(self, bad: int) -> None:
        columns = self._clean_columns()
        columns["bar_bits"][3] = bad
        assert self._reason(columns) == "bar_bits_beyond_u128"

    def test_bar_bits_width_boundaries_are_in_envelope(self) -> None:
        """0, U64_MASK and _U128_MASK exactly are kernel-valid (inclusive range).

        U64_MASK + 1 is ALSO in envelope now — it selects the u128 kernel
        instead of a fallback (DEC-TRADE-SIM-U128-KERNEL-001 boundary
        inclusivity: the guard trips strictly ABOVE _U128_MASK).
        """
        columns = self._clean_columns()
        columns["bar_bits"][0] = 0
        columns["bar_bits"][1] = U64_MASK
        columns["bar_bits"][2] = U64_MASK + 1
        columns["bar_bits"][3] = _U128_MASK
        assert self._reason(columns) is None

    def test_bar_bits_checked_before_prices(self) -> None:
        """Both out of range → the bar_bits reason wins (documented scan order)."""
        columns = self._clean_columns()
        columns["bar_bits"][0] = -1
        columns["open_p"][0] = _MAX_KERNEL_PRICE_PAISA + 1
        assert self._reason(columns) == "bar_bits_beyond_u128"


class TestPerCallEnvelopeGuards:
    def test_mask_beyond_u128_falls_back_for_that_call_only(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)  # u64-clean (bits 0-59)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        masks = [(1 << 128) | 1, 1]  # a beyond-u128 mask + a real firing mask
        with capture_logs() as logs:
            got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "mask_beyond_u128"
        assert events[0]["n_masks"] == 2
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        assert got == expected
        # Per-CALL, not sticky: the kernel stays active and a clean follow-up
        # call crosses the FFI boundary again (no new fallback warning).
        assert sim.rust_active is True
        with capture_logs() as logs2:
            again = sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(logs2) == []
        assert again == [expected[1]]

    def test_negative_mask_falls_back_for_that_call_only(self) -> None:
        """A NEGATIVE mask (API misuse only) routes to the oracle, not PyO3.

        `iter_masks_k_of_bits` emits only non-negative ints; a hand-rolled
        negative mask would raise PyO3 ``OverflowError`` at EITHER ``Vec``
        edge (u64 or u128) where the wheel-absent arm simulates (Python's
        infinite-precision ``&``) — guarded in BOTH directions so wheel state
        never changes behaviour.
        """
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        masks = [-1, 1]
        with capture_logs() as logs:
            got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "mask_beyond_u128"
        assert got == [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        # The kernel stays active for clean follow-up calls.
        assert sim.rust_active is True

    def test_call_fallback_warns_once_per_reason_per_instance(self) -> None:
        """A repeated same-reason fallback does NOT re-warn (log-flood guard).

        The production sweep chunks kept masks into up to thousands of
        `simulate_masks` calls per tuple; a sticky beyond-envelope reason on
        EVERY chunk would flood the sweep with millions of WARNING lines. The
        fallback itself stays per-call and bit-exact — only the WARNING is
        memoised.
        """
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        beyond_masks = [(1 << 128) | 1]
        with capture_logs() as logs1:
            first = sim.simulate_masks(beyond_masks, direction=Side.BUY, stop_loss_paisa=300)
        assert len(_call_fallback_events(logs1)) == 1
        with capture_logs() as logs2:
            second = sim.simulate_masks(beyond_masks, direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(logs2) == []  # memoised — no re-warn
        expected = [
            _oracle(
                bars,
                bits,
                (1 << 128) | 1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
        ]
        assert first == expected  # the fallback STILL served both calls,
        assert second == expected  # bit-exactly — only the warning is deduped

    def test_call_fallback_new_reason_warns_again(self) -> None:
        """The warn-once memo is per REASON — a different cause is still loud."""
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        with capture_logs() as logs1:
            sim.simulate_masks([(1 << 130) | 1], direction=Side.BUY)
        events1 = _call_fallback_events(logs1)
        assert [e["reason"] for e in events1] == ["mask_beyond_u128"]
        with capture_logs() as logs2:
            sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=_MAX_KERNEL_PRICE_PAISA + 1)
        events2 = _call_fallback_events(logs2)
        assert [e["reason"] for e in events2] == ["stop_loss_out_of_envelope"]

    @pytest.mark.parametrize(
        ("leg", "reason"),
        [
            ("stop_loss_paisa", "stop_loss_out_of_envelope"),
            ("take_profit_paisa", "take_profit_out_of_envelope"),
        ],
    )
    def test_oversized_distance_falls_back_and_matches_oracle(self, leg: str, reason: str) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        distance = _MAX_KERNEL_PRICE_PAISA + 1  # kernel would ValueError; oracle simulates
        sl = distance if leg == "stop_loss_paisa" else None
        tp = distance if leg == "take_profit_paisa" else None
        with capture_logs() as logs:
            got = sim.simulate_masks(
                [1], direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp
            )
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == reason
        assert got == [
            _oracle(
                bars,
                bits,
                1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=distance if leg == "stop_loss_paisa" else None,
                take_profit_paisa=distance if leg == "take_profit_paisa" else None,
            )
        ]

    def test_boundary_distance_stays_on_the_rust_path(self) -> None:
        """EXACTLY the ceiling is kernel-valid (the guard is strict-greater)."""
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        with capture_logs() as logs:
            got = sim.simulate_masks(
                [1],
                direction=Side.BUY,
                stop_loss_paisa=_MAX_KERNEL_PRICE_PAISA,
                take_profit_paisa=_MAX_KERNEL_PRICE_PAISA,
            )
        assert _call_fallback_events(logs) == []  # the kernel served it
        assert got == [
            _oracle(
                bars,
                bits,
                1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=_MAX_KERNEL_PRICE_PAISA,
                take_profit_paisa=_MAX_KERNEL_PRICE_PAISA,
            )
        ]

    @pytest.mark.parametrize("forced_fallback", [False, True])
    @pytest.mark.parametrize("leg", ["stop_loss_paisa", "take_profit_paisa"])
    @pytest.mark.parametrize("value", [0, -5])
    def test_nonpositive_distance_rejected_identically_on_both_arms(
        self, forced_fallback: bool, leg: str, value: int
    ) -> None:
        """d <= 0 raises the SAME ValueError whether the kernel serves or not.

        The kernel accepts a 0 distance where the Python walker raises — the
        wrapper normalises the domain BEFORE arm dispatch, so wheel state can
        never change the error behaviour (DEC-SWEEP-RUST-TRADESIM-WIRE-001).
        """
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        if forced_fallback:
            sim._rust = None
        sl = value if leg == "stop_loss_paisa" else None
        tp = value if leg == "take_profit_paisa" else None
        with pytest.raises(ValueError, match=f"{leg} must be > 0 or None"):
            sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp)


# ── wide-mask promotion to the u128 sibling (DEC-TRADE-SIM-U128-KERNEL-001) ──


def _promoted_events(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "rust_trade_sim.wide_kernel_promoted"]


class TestWideMaskPromotion:
    """A >64-bit (≤ u128) mask over a u64-resident window is Rust-served.

    DEC-TRADE-SIM-U128-KERNEL-001: the u128 sibling kernel is built LAZILY (at
    most once per instance, one ``wide_kernel_promoted`` INFO) and the WHOLE
    batch routes wide — the old mixed-batch whole-chunk oracle degrade is
    dissolved. u64-fit batches keep the resident u64 kernel byte-identically;
    only a stale wheel lacking the class falls back (warn-once
    ``wheel_missing_u128``).
    """

    def test_wide_mask_promotes_and_matches_oracle(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)  # u64-fit → the u64 kernel is resident
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        masks = [(1 << 64) | 1, 1]  # a wide mask + a u64-fit firing mask
        with capture_logs() as logs:
            got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(logs) == []  # NOT a fallback any more
        assert len(_promoted_events(logs)) == 1
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        assert got == expected
        assert sim.rust_active is True  # the u64 kernel stays resident

    def test_wide_kernel_built_once_and_memoised(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        wide = [(1 << 64) | 1]
        with capture_logs() as logs1:
            first = sim.simulate_masks(wide, direction=Side.BUY)
        with capture_logs() as logs2:
            second = sim.simulate_masks(wide, direction=Side.BUY)
        assert len(_promoted_events(logs1)) == 1  # announced exactly once …
        assert _promoted_events(logs2) == []  # … the memoised reuse is silent
        assert first == second
        assert sim._rust_wide is not None  # built exactly once, memoised

    def test_whole_batch_routes_wide_and_u64_batches_keep_the_u64_kernel(self) -> None:
        """Routing pin via a recording proxy on the promoted sibling.

        A batch containing ONE wide mask goes WHOLLY to the u128 kernel (the
        mixed-batch whole-chunk degrade is dissolved); a u64-fit batch after
        promotion still takes the resident u64 kernel — never the sibling.
        """
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        sim.simulate_masks([(1 << 64) | 1], direction=Side.BUY)  # trigger promotion
        real_wide = sim._rust_wide
        assert real_wide is not None
        calls: list[list[int]] = []

        class _RecordingWide:
            def simulate_masks(
                self, masks: list[int], *args: object
            ) -> list[list[tuple[int, int, int, int, int, int]]]:
                calls.append(list(masks))
                result = real_wide.simulate_masks(masks, *args)  # type: ignore[attr-defined]
                return cast("list[list[tuple[int, int, int, int, int, int]]]", result)

        sim._rust_wide = _RecordingWide()
        mixed = [1, (1 << 64) | 1]
        got = sim.simulate_masks(mixed, direction=Side.BUY)
        assert calls == [mixed]  # the WHOLE batch went wide, positionally intact
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
            for m in mixed
        ]
        assert got == expected
        calls.clear()
        u64_only = sim.simulate_masks([1], direction=Side.BUY)
        assert calls == []  # the u64 batch never touched the wide sibling
        assert u64_only == [expected[0]]

    def test_u64_mask_boundary_does_not_promote(self) -> None:
        """mask == U64_MASK stays on the u64 kernel (promotion is strictly greater)."""
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        with capture_logs() as logs:
            got = sim.simulate_masks([U64_MASK], direction=Side.BUY)
        assert _promoted_events(logs) == []
        assert _call_fallback_events(logs) == []
        assert sim._rust_wide is None
        assert got == [
            _oracle(
                bars,
                bits,
                U64_MASK,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]

    def test_u64_mask_plus_one_promotes_and_u128_mask_is_served(self) -> None:
        """Boundary inclusivity: U64_MASK+1 promotes; _U128_MASK crosses the FFI."""
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        with capture_logs() as logs:
            got = sim.simulate_masks([U64_MASK + 1, _U128_MASK], direction=Side.BUY)
        assert len(_promoted_events(logs)) == 1
        assert _call_fallback_events(logs) == []  # _U128_MASK is IN envelope
        # Neither mask can fire on ≤60-bit bar_bits — the pin is that the wide
        # kernel SERVED them (no fallback, no OverflowError) with the oracle's
        # exact (empty) verdicts.
        assert got == [[], []]
        with capture_logs() as logs2:
            beyond = sim.simulate_masks([_U128_MASK + 1], direction=Side.BUY)
        events = _call_fallback_events(logs2)
        assert [e["reason"] for e in events] == ["mask_beyond_u128"]
        assert beyond == [[]]

    def test_wide_resident_window_serves_all_widths_without_promotion(self) -> None:
        """A window built WIDE (bar_bits > u64) never promotes — it already is."""
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] |= 1 << 100
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        masks = [1, (1 << 100) | 1]
        with capture_logs() as logs:
            got = sim.simulate_masks(masks, direction=Side.BUY)
        assert _promoted_events(logs) == []
        assert _call_fallback_events(logs) == []
        assert sim._rust_wide is None
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
            for m in masks
        ]
        assert got == expected

    def test_wide_mask_on_stale_wheel_warns_once_and_uses_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old-shim+old-.so posture (monkeypatched attribute) → graceful oracle.

        The u64 kernel was built from the REAL wheel; the promotion then sees
        a module lacking ResidentSweepSimU128 → warn-once ``wheel_missing_u128``,
        oracle-served bit-exactly, verdict memoised (no per-chunk re-import).
        The LOUD new-shim+old-.so mode — ``import fno_math`` ImportError naming
        the symbol — is pinned in test_doctor_wheel_freshness_fno_math.py.
        """
        import fno_math as real_fno_math

        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        fake = ModuleType("fno_math")
        fake.ResidentSweepSim = real_fno_math.ResidentSweepSim  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fno_math", fake)
        wide = [(1 << 64) | 1, 1]
        with capture_logs() as logs1:
            got = sim.simulate_masks(wide, direction=Side.BUY, stop_loss_paisa=300)
        events = _call_fallback_events(logs1)
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing_u128"
        assert "offending_value" not in events[0]  # not a width reason
        expected = [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
            )
            for m in wide
        ]
        assert got == expected
        with capture_logs() as logs2:
            again = sim.simulate_masks(wide, direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(logs2) == []  # warn-once memo
        assert again == expected
        assert sim._wide_unavailable is True  # stale verdict memoised
        assert sim._rust_wide is None
        assert sim.rust_active is True  # u64 batches stay Rust-served

    def test_wide_mask_import_error_posture_also_wheel_missing_u128(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ImportError arm of the promotion (module vanished) → same reason."""
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        monkeypatch.setitem(sys.modules, "fno_math", None)  # import → ImportError
        with capture_logs() as logs:
            got = sim.simulate_masks([(1 << 64) | 1], direction=Side.BUY)
        events = _call_fallback_events(logs)
        assert [e["reason"] for e in events] == ["wheel_missing_u128"]
        assert got == [
            _oracle(
                bars,
                bits,
                (1 << 64) | 1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]


# ── width-fallback WARNINGs NAME the offending word (DEC-SWEEP-RUST-WIRE-001 p1) ─
#
# The engine-scale spec's PR-1 gate: the width-guard WARNING must name the
# offending value + bit, not just a reason string, so the operator sees exactly
# which word could not cross even the `Vec<u128>` FFI edge. These tests pin the
# `offending_value` (hex) / `offending_bit` fields on BOTH width reasons
# (`bar_bits_beyond_u128` at construction, `mask_beyond_u128` at call time),
# the None-bit for the negative API-misuse direction, first-offender
# determinism, and that NON-width reasons carry NO offender fields.


class TestWidthOffenderNamedInWarning:
    def test_construction_oversize_bar_word_names_value_and_bit(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[1] |= 1 << 130  # first offender positionally (beyond u128)
        bits[3] |= 1 << 128  # a second offender — must NOT be the one named
        with capture_logs() as logs:
            ResidentTradeSim(bars, bits, tf_minutes=1)
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "bar_bits_beyond_u128"
        assert events[0]["offending_value"] == hex(bits[1])  # the FIRST offender
        assert events[0]["offending_bit"] == 130

    def test_construction_negative_bar_word_names_value_with_none_bit(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] = -1
        with capture_logs() as logs:
            ResidentTradeSim(bars, bits, tf_minutes=1)
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["offending_value"] == "-0x1"
        assert events[0]["offending_bit"] is None  # a "bit" has no meaning here

    def test_construction_non_width_reason_carries_no_offender_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = ModuleType("fno_math")
        monkeypatch.setitem(sys.modules, "fno_math", fake)
        bars = _feed()
        with capture_logs() as logs:
            ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"
        assert "offending_value" not in events[0]
        assert "offending_bit" not in events[0]

    def test_call_oversize_mask_names_value_and_bit(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is True
        with capture_logs() as logs:
            sim.simulate_masks([1, (1 << 128) | 1], direction=Side.BUY)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "mask_beyond_u128"
        assert events[0]["offending_value"] == hex((1 << 128) | 1)
        assert events[0]["offending_bit"] == 128  # the FIRST width u128 cannot hold

    def test_call_negative_mask_names_value_with_none_bit(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is True
        with capture_logs() as logs:
            sim.simulate_masks([-7, 1], direction=Side.BUY)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["offending_value"] == "-0x7"
        assert events[0]["offending_bit"] is None

    def test_call_non_width_reason_carries_no_offender_fields(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is True
        with capture_logs() as logs:
            sim.simulate_masks([1], direction=Side.BUY, stop_loss_paisa=(2**63 - 1) // 8 + 1)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "stop_loss_out_of_envelope"
        assert "offending_value" not in events[0]
        assert "offending_bit" not in events[0]


# ── DEC-SWEEP-EXIT-INDEX-FASTPATH-001: exit-index-carrying batch ─────────────


class TestSimulateMasksWithExitIndices:
    """The pair-shaped batch spine + the double certification."""

    def _sim(self) -> ResidentTradeSim:
        bars = _feed()
        return ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)

    def test_rust_arm_indices_match_the_trade_exit_timestamps(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.rust_active is True
        pairs = sim.simulate_masks_with_exit_indices(
            [0], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
        )
        assert len(pairs) == 1
        trades, exit_indices = pairs[0]
        assert trades  # the mask-0 walk trades on this feed
        assert exit_indices is not None
        assert len(exit_indices) == len(trades)
        for trade, idx in zip(trades, exit_indices, strict=True):
            assert bars[idx].timestamp == trade.exit_ts
        assert exit_indices == sorted(set(exit_indices))  # strictly increasing

    def test_python_fallback_arm_emits_identical_pairs(self) -> None:
        bars = _feed()
        wired = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        fallback = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        fallback._rust = None  # pyright: ignore[reportPrivateUsage] — the forcing seam
        assert wired.simulate_masks_with_exit_indices(
            [0, 1], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
        ) == fallback.simulate_masks_with_exit_indices(
            [0, 1], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
        )

    def test_simulate_masks_is_the_thin_delegate(self) -> None:
        sim = self._sim()
        pairs = sim.simulate_masks_with_exit_indices(
            [0, 1], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
        )
        assert sim.simulate_masks(
            [0, 1], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
        ) == [trades for trades, _ in pairs]

    def test_uncertified_window_withholds_indices_on_both_arms(self) -> None:
        """A duplicate timestamp fails the construction certificate — trades
        still simulate, indices are None (the dict path serves downstream)."""
        bars = [*_feed(), _feed()[-1]]  # duplicate final timestamp
        bar_bits = [*build_bar_bits(_feed()), 0]
        for force_python in (False, True):
            sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
            if force_python:
                sim._rust = None  # pyright: ignore[reportPrivateUsage]
            pairs = sim.simulate_masks_with_exit_indices(
                [0], direction=Side.BUY, stop_loss_paisa=200, take_profit_paisa=300
            )
            (trades, exit_indices) = pairs[0]
            assert trades
            assert exit_indices is None

    def test_non_monotonic_kernel_stream_is_refused_with_one_warning(self) -> None:
        """A kernel returning out-of-order records (an ordering regression)
        gets its indices withheld LOUDLY — once per instance."""
        sim = self._sim()

        class _ShuffledKernel:
            """Returns two well-formed records with REVERSED exit order."""

            def simulate_masks(self, *args: object) -> list[list[tuple[int, ...]]]:
                record_late = (100, 100, 0, 2, 2, 3)
                record_early = (50, 50, 0, 1, 1, 1)
                return [[record_late, record_early], [record_early, record_late]]

        sim._rust = _ShuffledKernel()  # pyright: ignore[reportPrivateUsage]
        with capture_logs() as logs:
            pairs = sim.simulate_masks_with_exit_indices([0, 1], direction=Side.BUY)
        assert pairs[0][1] is None  # the non-monotonic mask: indices refused
        assert pairs[1][1] == [1, 3]  # the well-ordered mask keeps its indices
        events = [e for e in logs if e["event"] == "rust_trade_sim.exit_indices_uncertified"]
        assert len(events) == 1
        assert events[0]["reason"] == "exit_indices_not_monotonic"
        # Repeat call: already warned — silent refusal.
        with capture_logs() as logs2:
            sim.simulate_masks_with_exit_indices([0], direction=Side.BUY)
        assert not [e for e in logs2 if e["event"] == "rust_trade_sim.exit_indices_uncertified"]

    def test_duplicate_exit_index_kernel_stream_is_refused_with_one_warning(self) -> None:
        """EQUAL consecutive exit indices fail the STRICTNESS half of the
        certificate (``idx <= prev``, not just ``idx < prev`` decreasing) —
        the boundary the shuffled-kernel fixture never touches: a kernel
        duplicate-record bug emitting the same exit index twice must be
        refused exactly like a reversed stream (the ``<=`` → ``<`` hand-mutant
        killer)."""
        sim = self._sim()

        class _DuplicateKernel:
            """Returns two well-formed records sharing ONE exit index."""

            def simulate_masks(self, *args: object) -> list[list[tuple[int, ...]]]:
                record_a = (50, 50, 0, 1, 0, 1)
                record_b = (60, 60, 0, 1, 1, 1)  # duplicate exit_idx 1
                return [[record_a, record_b]]

        sim._rust = _DuplicateKernel()  # pyright: ignore[reportPrivateUsage]
        with capture_logs() as logs:
            pairs = sim.simulate_masks_with_exit_indices([0], direction=Side.BUY)
        assert pairs[0][1] is None  # equal-index stream: indices refused
        assert len(pairs[0][0]) == 2  # the trades themselves still ship
        events = [e for e in logs if e["event"] == "rust_trade_sim.exit_indices_uncertified"]
        assert len(events) == 1
        assert events[0]["reason"] == "exit_indices_not_monotonic"


# ── reference-stamper seam (per-module 100% gate; DEC-VIX-STAMP-WIRE-001) ────


def _vix_stamper_for(bars: list[CandleBar]) -> TradeReferenceStamper:
    """A stamper whose VIX series covers every bar timestamp of *bars*."""
    vix_bars = [
        CandleBar(
            timestamp=bar.timestamp,
            open=Decimal("14.00"),
            high=Decimal("15.00"),
            low=Decimal("13.00"),
            close=Decimal("14.00") + Decimal(i % 7) / Decimal(100),
            volume=0,
            open_interest=None,
        )
        for i, bar in enumerate(bars)
    ]
    return TradeReferenceStamper.from_vix_bars(vix_bars)


class TestReferenceStamperSeam:
    """The stamper seam's own lines, covered by THIS module's discovered suite.

    The full wiring behaviour (oracle ≡ batch stamp parity, spawn-pool
    threading, byte-identity) lives in
    ``tests/unit/test_engine_vix_stamper_wiring.py``; these mirrors keep the
    per-file 100% gate honest on the module's OWN covering suite
    (``scripts/ci/coverage_gate_discovery.py`` maps ``rust_trade_sim.py`` to
    ``test_engine_rust_trade_sim.py`` only).
    """

    def test_reference_stamper_property_default_is_none(self) -> None:
        bars = _feed()
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert sim.reference_stamper is None

    def test_reference_stamper_property_returns_bound_stamper(self) -> None:
        bars = _feed()
        stamper = _vix_stamper_for(bars)
        sim = ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1, reference_stamper=stamper)
        assert sim.reference_stamper is stamper

    def test_stamped_trades_resolve_vix_at_the_conversion_seam(self) -> None:
        bars = _feed()
        stamper = _vix_stamper_for(bars)
        bar_bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1, reference_stamper=stamper)
        (trades,) = sim.simulate_masks([0], direction=Side.BUY)
        assert trades
        for trade in trades:
            assert trade.vix_at_entry == stamper.vix.level_at(trade.entry_ts)
            assert trade.vix_at_exit == stamper.vix.level_at(trade.exit_ts)
            assert trade.vix_at_entry is not None
            assert trade.vix_at_exit is not None


# ────────────────────────────────────────────────────────────────────────
# PriceColumns + derive_price_columns + the injection/memo seams
# (DEC-STAGE1A-RUST-BATCH-001 — the W2 rider)
# ────────────────────────────────────────────────────────────────────────

from brutex.engine import rust_trade_sim as _rts_module  # noqa: E402
from brutex.engine.rust_trade_sim import (  # noqa: E402
    PriceColumns,
    derive_price_columns,
)


class TestPriceColumns:
    def test_derive_matches_the_historical_inline_derivation(self) -> None:
        # The extraction golden: the module-level derive equals the old
        # inline comprehensions (via the canonical _price_to_paisa seam) +
        # the per-bar IST-date ordinals.
        from brutex.engine.stage1a import (
            _price_to_paisa,  # pyright: ignore[reportPrivateUsage]
        )

        bars = _feed()
        columns = derive_price_columns(bars)
        # Immutable tuples (review LOW-2 — the columns are SHARED across sims).
        assert columns.open_paisa == tuple(_price_to_paisa(b.open) for b in bars)
        assert columns.high_paisa == tuple(_price_to_paisa(b.high) for b in bars)
        assert columns.low_paisa == tuple(_price_to_paisa(b.low) for b in bars)
        assert columns.close_paisa == tuple(_price_to_paisa(b.close) for b in bars)
        assert columns.session_days == tuple(
            b.timestamp.astimezone(IST).date().toordinal() for b in bars
        )
        assert columns.n_bars == len(bars)

    @pytest.mark.parametrize("field", ["high_paisa", "low_paisa", "close_paisa", "session_days"])
    def test_inconsistent_columns_are_refused_naming_the_field(self, field: str) -> None:
        kwargs: dict[str, tuple[int, ...]] = {
            "open_paisa": (1, 2),
            "high_paisa": (1, 2),
            "low_paisa": (1, 2),
            "close_paisa": (1, 2),
            "session_days": (1, 2),
        }
        kwargs[field] = (1,)
        with pytest.raises(ValueError, match=field):
            PriceColumns(**kwargs)

    def test_precomputed_arm_equals_the_derive_arm_bit_for_bit(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        derived = ResidentTradeSim(bars, bits, tf_minutes=1)
        injected = ResidentTradeSim(
            bars, bits, tf_minutes=1, precomputed_columns=derive_price_columns(bars)
        )
        # The assembled kernel-input 6-tuples are bit-equal…
        assert (
            derived._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
            == injected._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
        )
        # …and the simulated trades identical (both kernel-served).
        masks = [0, 1, 3]
        assert derived.rust_active is True
        assert injected.rust_active is True
        assert derived.simulate_masks(
            masks, direction=Side.BUY, stop_loss_paisa=300
        ) == injected.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)

    def test_length_mismatched_precomputed_columns_are_refused_naming_both(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        short = derive_price_columns(bars[:-1])
        with pytest.raises(ValueError, match=r"cover 4 bars != bars length 5"):
            ResidentTradeSim(bars, bits, tf_minutes=1, precomputed_columns=short)

    def test_default_none_is_the_flag_independent_byte_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default-None arm derives locally regardless of the Stage-1A
        # flag (the W2 rider is flag-INDEPENDENT — pinned). The OFF arm is
        # the explicit "0" kill switch — since DEC-STAGE1A-RUST-DEFAULT-ON-001
        # an unset env falls to the flipped default-ON constant.
        monkeypatch.setenv("BRUTEX_SWEEP_RUST_STAGE1A", "0")
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        monkeypatch.setenv("BRUTEX_SWEEP_RUST_STAGE1A", "1")
        sim_on = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert (
            sim._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
            == sim_on._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
        )


class TestKernelInputsMemo:
    def test_derive_runs_once_per_instance_including_wide_promotion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The G-4 memo: construction derives ONCE; the u128 promotion reuses
        # the memoised 6-tuple instead of re-paying the Decimal pass.
        calls = [0]
        real = derive_price_columns

        def _counting(bars: object) -> PriceColumns:
            calls[0] += 1
            return real(bars)  # type: ignore[arg-type]

        monkeypatch.setattr(_rts_module, "derive_price_columns", _counting)
        bars = _feed()
        bits = build_bar_bits(bars)  # u64-fit bits
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert calls[0] == 1
        # A >64-bit mask promotes the window to the u128 sibling kernel …
        wide_mask = 1 << 100
        with capture_logs() as logs:
            got = sim.simulate_masks([wide_mask], direction=Side.BUY)
        assert [e for e in logs if e.get("event") == "rust_trade_sim.wide_kernel_promoted"]
        # … WITHOUT a second derivation (the memo handoff).
        assert calls[0] == 1
        assert got == [
            _oracle(
                bars,
                bits,
                wide_mask,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )
        ]

    def test_injected_columns_skip_the_local_derivation_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        columns = derive_price_columns(bars)
        monkeypatch.setattr(
            _rts_module,
            "derive_price_columns",
            lambda *_a, **_k: pytest.fail("injected columns re-derived"),
        )
        sim = ResidentTradeSim(bars, bits, tf_minutes=1, precomputed_columns=columns)
        assert sim.rust_active is True
        first = sim._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
        second = sim._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
        assert first is second  # the memo hit


class TestSessionTailHoleWarn:
    """The mask-independent session-tail-hole WARN (DEC-TRADESIM-OPEN-SESSION-GUARD-001).

    ``_derive_kernel_inputs`` ends with ONE capped sweep naming every
    NON-final session whose stored bars never reach a forced-exit bar — the
    exact shape the walker's (A2) ``session_gap`` guard fires on. Once per
    derivation (never per mask, never inside ``_close`` — the
    DEC-SWEEP-LOG-VOLUME-GUARD-001 posture), capped at
    ``_TAIL_HOLE_WARN_CAP`` with ONE suppressed summary past it (the house
    preopen capped-warn pattern).
    """

    @staticmethod
    def _tail_hole_events(logs: list[EventDict]) -> list[EventDict]:
        return [e for e in logs if e["event"] == "trade_sim.session_tail_hole"]

    def test_construction_warns_once_for_a_non_final_forced_less_session(self) -> None:
        # Day-1 (2024-01-02) ends at 11:12 — a session-tail hole; day-2 is the
        # final session (exempt however it ends).
        bars = [
            _bar(11, 10, "100", "101", "99", "100"),
            _bar(11, 11, "100", "101", "99", "100"),
            _bar(11, 12, "100", "101", "99", "100"),
            _next_day_bar(9, 15, "600", "601", "599", "600"),
        ]
        with capture_logs() as logs:
            ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        holes = self._tail_hole_events(logs)
        assert len(holes) == 1
        assert holes[0]["session"] == "2024-01-02"
        assert holes[0]["bars"] == 3
        assert [e for e in logs if e["event"] == "trade_sim.session_tail_hole_suppressed"] == []

    def test_silent_for_a_healthy_window_and_for_the_final_session(self) -> None:
        # (a) day-1 REACHES its forced-exit bar (15:20) → not a hole; (b) the
        # final session (day-2, ends 09:16) is exempt however truncated.
        bars = [
            _bar(15, 19, "100", "101", "99", "100"),
            _bar(15, 20, "100", "101", "99", "100"),  # forced bar — session complete
            _next_day_bar(9, 15, "600", "601", "599", "600"),
            _next_day_bar(9, 16, "600", "601", "599", "600"),
        ]
        with capture_logs() as logs:
            ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert self._tail_hole_events(logs) == []

    def test_single_session_window_never_warns(self) -> None:
        # A one-session window IS the final session — the ordinary
        # end-of-window shape end_of_data already names; zero WARNs.
        bars = _feed()
        with capture_logs() as logs:
            ResidentTradeSim(bars, build_bar_bits(bars), tf_minutes=1)
        assert self._tail_hole_events(logs) == []

    def test_cap_20_plus_one_suppressed_summary(self) -> None:
        # Direct-call teeth on the pure helper (the house private-import
        # pattern): 22 hole sessions + a final session → exactly 20 WARNs +
        # ONE suppressed summary carrying the TRUE total.
        session_days: list[int] = []
        forced: list[bool] = []
        for ordinal in range(738522, 738522 + 23):  # 23 sessions, 2 bars each
            session_days.extend([ordinal, ordinal])
            forced.extend([False, False])
        with capture_logs() as logs:
            _warn_session_tail_holes(forced, session_days)
        holes = self._tail_hole_events(logs)
        assert len(holes) == _TAIL_HOLE_WARN_CAP == 20
        suppressed = [e for e in logs if e["event"] == "trade_sim.session_tail_hole_suppressed"]
        assert len(suppressed) == 1
        assert suppressed[0]["total"] == 22  # the final (23rd) session is exempt
        assert suppressed[0]["shown"] == _TAIL_HOLE_WARN_CAP

    def test_exactly_at_cap_20_warns_all_with_no_suppressed_summary(self) -> None:
        # Boundary teeth on the strict `>` cap compare (refuter round 3 CM9):
        # EXACTLY _TAIL_HOLE_WARN_CAP hole sessions (20 holes + the exempt
        # final session) emit ALL 20 per-session WARNs and ZERO suppressed
        # summaries — nothing was suppressed, so a summary event here would
        # fabricate suppression. A `>` → `>=` mutant at the cap compare fires
        # the summary at exactly-the-cap and fails this test.
        session_days: list[int] = []
        forced: list[bool] = []
        for ordinal in range(738522, 738522 + 21):  # 20 holes + 1 exempt final
            session_days.extend([ordinal, ordinal])
            forced.extend([False, False])
        with capture_logs() as logs:
            _warn_session_tail_holes(forced, session_days)
        holes = self._tail_hole_events(logs)
        assert len(holes) == _TAIL_HOLE_WARN_CAP == 20
        assert [e["session"] for e in holes] == [
            date.fromordinal(o).isoformat() for o in range(738522, 738522 + 20)
        ]
        assert [e for e in logs if e["event"] == "trade_sim.session_tail_hole_suppressed"] == []

    def test_forced_bearing_interior_session_is_not_a_hole(self) -> None:
        # Direct call: an interior session carrying ANY forced bar is healthy;
        # its forced-less interior sibling still warns (the run split logic).
        session_days = [1, 1, 2, 2, 3]
        forced = [False, True, False, False, False]
        with capture_logs() as logs:
            _warn_session_tail_holes(forced, session_days)
        holes = self._tail_hole_events(logs)
        assert [(e["session"], e["bars"]) for e in holes] == [(date.fromordinal(2).isoformat(), 2)]

    def test_single_forced_bar_interior_session_is_not_a_hole(self) -> None:
        # Direct call (refuter round 5 MR3b, slice-START mutant killer): an
        # interior session whose ONLY stored bar IS a forced-exit bar — e.g. a
        # lake hole leaving just the 15:20 bar — is healthy, NOT a hole.
        # Because forced flags are a monotone suffix within a session, the
        # single-bar forced-only run is the ONE shape that discriminates the
        # slice start: a `forced[run_start:i]` → `forced[run_start+1:i]`
        # mutant scans the EMPTY slice here and fabricates a false
        # session_tail_hole WARN (bars=1). The forced-bearing sibling test
        # above puts its forced bar SECOND, so only this shape kills it.
        session_days = [738522, 738523, 738523]
        forced = [True, False, False]
        with capture_logs() as logs:
            _warn_session_tail_holes(forced, session_days)
        assert self._tail_hole_events(logs) == []
        assert [e for e in logs if e["event"] == "trade_sim.session_tail_hole_suppressed"] == []

    def test_session_days_derive_from_the_ist_session_date_not_the_utc_date(self) -> None:
        """``session_days`` ordinals derive from the bar's IST session date,
        never its UTC calendar date (refuter round 5 MD2, directed mutant pin
        — the Rust-path derivation mirror of the walker's
        ``test_entry_day_is_the_ist_session_date_not_the_utc_date``).

        The ``bar.timestamp.date()`` (UTC-date) mutant at the
        ``session_days.append`` in ``_derive_kernel_inputs`` survived the
        suite because the mismatch is domain-bounded — no NSE session bar
        exists before 05:30 IST post-ingest-filter, so IST and UTC dates
        always agree on real lake bars. The derivation's DOCUMENTED semantics
        are the IST session date ("mirrors the walker's gate bit-for-bit"),
        so this pins that law beyond the NSE trading window
        (defense-in-depth; the domain never produces such bars): two bars in
        ONE IST session 2024-01-02 straddle UTC midnight (IST 01:00 = UTC
        2024-01-01 19:30; IST 06:00 = UTC 2024-01-02 00:30) — under the
        mutant the IST session SPLITS into two kernel sessions (a phantom
        boundary the kernel's (A2) guard would session_gap-close across) and
        a false ``trade_sim.session_tail_hole`` WARN fires naming the UTC
        date 2024-01-01.
        """
        bars = [
            _bar(1, 0, "100", "101", "99", "100"),  # IST 01:00 = UTC 2024-01-01T19:30
            _bar(6, 0, "100", "101", "99", "100"),  # IST 06:00 = UTC 2024-01-02T00:30
        ]
        # The fixture genuinely straddles UTC midnight within one IST session.
        assert [b.timestamp.astimezone(UTC).date().isoformat() for b in bars] == [
            "2024-01-01",
            "2024-01-02",
        ]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, [0, 0], tf_minutes=1)
            derived = sim._derive_kernel_inputs()  # pyright: ignore[reportPrivateUsage]
        session_days = derived[5]
        # Container glue only (the W2 memo stores immutable tuples — review
        # LOW-2): the behavioral assertion — the VALUES — is main's, unchanged.
        assert list(session_days) == [_DAY.toordinal()] * 2  # ONE IST session (2024-01-02)
        assert self._tail_hole_events(logs) == []
