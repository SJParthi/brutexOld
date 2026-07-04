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
from brutex.engine.combo_mask import U64_MASK, mask_to_entry_signals

# Whitebox friend access (DEC-P0-017 per-site convention): the guard seam is
# module-private; each COLUMN of its envelope check is pinned directly here
# because a single-column corrupt bar is unrepresentable through CandleBar's
# OHLC invariant (see TestEnvelopeGuardSeamPerColumn).
from brutex.engine.rust_trade_sim import (
    _MAX_KERNEL_PRICE_PAISA,  # pyright: ignore[reportPrivateUsage]
    ResidentTradeSim,
    _construction_fallback_reason,  # pyright: ignore[reportPrivateUsage]
)
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import (
    FillPolicy,
    LevelFill,
    SameBarTie,
    simulate_trades,
)
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


# ── kernel-envelope guards (DEC-SWEEP-RUST-TRADESIM-WIRE-001) ─────────────────
#
# The compiled kernel is u64-native (bar_bits / masks) and #1090-validates every
# paisa input against MAX_PRICE_PAISA = i64::MAX / 8. An out-of-envelope input
# must route to the bit-exact Python oracle — LOUDLY (one structlog WARNING) —
# never surface as a PyO3 OverflowError / kernel ValueError the pure-Python
# engine would not have raised (wheel state must never change behaviour).


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

    def test_bar_bits_u128_falls_back_and_matches_oracle(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[0] |= 1 << 100  # the documented u128 headroom (MAX_VOCAB_BITS = 128)
        masks = [0, 1]
        with capture_logs() as logs:
            sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is False
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "bar_bits_u128"
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
                policy=FillPolicy(),
            )
            for m in masks
        ]
        assert got == expected

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
                policy=FillPolicy(),
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
                policy=FillPolicy(),
            )
        ]

    def test_negative_bar_bits_falls_back_and_matches_oracle(self) -> None:
        """A NEGATIVE bar_bits value (API misuse only) routes to the oracle too.

        `build_bar_bits` emits only non-negative ints, but a hand-rolled
        negative int would raise PyO3 ``OverflowError`` at the ``Vec<u64>``
        edge where the wheel-absent Python arm happily simulates (Python's
        infinite-precision ``&``) — the exact wheel-present-vs-absent
        asymmetry class the guard closes, in BOTH directions.
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
        assert events[0]["reason"] == "bar_bits_u128"
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
                policy=FillPolicy(),
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

    @pytest.mark.parametrize("bad", [U64_MASK + 1, 1 << 100, -1])
    def test_bar_bits_outside_u64_range_is_detected(self, bad: int) -> None:
        columns = self._clean_columns()
        columns["bar_bits"][3] = bad
        assert self._reason(columns) == "bar_bits_u128"

    def test_bar_bits_u64_boundary_is_in_envelope(self) -> None:
        """0 and U64_MASK exactly are kernel-valid (inclusive range)."""
        columns = self._clean_columns()
        columns["bar_bits"][0] = 0
        columns["bar_bits"][1] = U64_MASK
        assert self._reason(columns) is None

    def test_bar_bits_checked_before_prices(self) -> None:
        """Both out of range → the bar_bits reason wins (documented scan order)."""
        columns = self._clean_columns()
        columns["bar_bits"][0] = -1
        columns["open_p"][0] = _MAX_KERNEL_PRICE_PAISA + 1
        assert self._reason(columns) == "bar_bits_u128"


class TestPerCallEnvelopeGuards:
    def test_mask_u128_falls_back_for_that_call_only(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)  # u64-clean (bits 0-59)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        masks = [(1 << 64) | 1, 1]  # a u128 mask + a real firing mask
        with capture_logs() as logs:
            got = sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=300)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "mask_u128"
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
                policy=FillPolicy(),
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
        negative mask would raise PyO3 ``OverflowError`` at the ``Vec<u64>``
        edge where the wheel-absent arm simulates (Python's infinite-precision
        ``&``) — guarded in BOTH directions so wheel state never changes
        behaviour.
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
        assert events[0]["reason"] == "mask_u128"
        assert got == [
            _oracle(
                bars,
                bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
                policy=FillPolicy(),
            )
            for m in masks
        ]
        # The kernel stays active for clean follow-up calls.
        assert sim.rust_active is True

    def test_call_fallback_warns_once_per_reason_per_instance(self) -> None:
        """A repeated same-reason fallback does NOT re-warn (log-flood guard).

        The production sweep chunks kept masks into up to thousands of
        `simulate_masks` calls per tuple; once the vocabulary crosses 64 bits
        EVERY chunk would trip the guard, so a per-CALL warning would flood a
        u128-era sweep with millions of WARNING lines. The fallback itself
        stays per-call and bit-exact — only the WARNING is memoised.
        """
        bars = _feed()
        bits = build_bar_bits(bars)
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        u128_masks = [(1 << 64) | 1]
        with capture_logs() as logs1:
            first = sim.simulate_masks(u128_masks, direction=Side.BUY, stop_loss_paisa=300)
        assert len(_call_fallback_events(logs1)) == 1
        with capture_logs() as logs2:
            second = sim.simulate_masks(u128_masks, direction=Side.BUY, stop_loss_paisa=300)
        assert _call_fallback_events(logs2) == []  # memoised — no re-warn
        expected = [
            _oracle(
                bars,
                bits,
                (1 << 64) | 1,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=300,
                take_profit_paisa=None,
                policy=FillPolicy(),
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
            sim.simulate_masks([(1 << 64) | 1], direction=Side.BUY)
        events1 = _call_fallback_events(logs1)
        assert [e["reason"] for e in events1] == ["mask_u128"]
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
                policy=FillPolicy(),
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
                policy=FillPolicy(),
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


# ── width-fallback WARNINGs NAME the offending word (DEC-SWEEP-RUST-WIRE-001 p1) ─
#
# The engine-scale spec's PR-1 gate: the u64 width-guard WARNING must name the
# offending value + bit, not just a reason string, so the operator sees exactly
# which word could not cross the `Vec<u64>` FFI edge. These tests pin the
# `offending_value` (hex) / `offending_bit` fields on BOTH width reasons
# (`bar_bits_u128` at construction, `mask_u128` at call time), the None-bit for
# the negative API-misuse direction, first-offender determinism, and that
# NON-width reasons carry NO offender fields.


class TestWidthOffenderNamedInWarning:
    def test_construction_oversize_bar_word_names_value_and_bit(self) -> None:
        bars = _feed()
        bits = build_bar_bits(bars)
        bits[1] |= 1 << 100  # first offender positionally
        bits[3] |= 1 << 64  # a second offender — must NOT be the one named
        with capture_logs() as logs:
            ResidentTradeSim(bars, bits, tf_minutes=1)
        events = _fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "bar_bits_u128"
        assert events[0]["offending_value"] == hex(bits[1])  # the FIRST offender
        assert events[0]["offending_bit"] == 100

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
            sim.simulate_masks([1, (1 << 64) | 1], direction=Side.BUY)
        events = _call_fallback_events(logs)
        assert len(events) == 1
        assert events[0]["reason"] == "mask_u128"
        assert events[0]["offending_value"] == hex((1 << 64) | 1)
        assert events[0]["offending_bit"] == 64  # the FIRST width u64 cannot hold

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
