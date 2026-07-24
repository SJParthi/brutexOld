"""Python-FFI-level pins for the u128 trade-sim kernel (DEC-TRADE-SIM-U128-KERNEL-001).

``fno_math.ResidentSweepSimU128`` is the >64-bit twin of ``ResidentSweepSim``
— both ride the ONE width-generic ``MaskWord`` core in
``rust/fno-math/src/trade_sim.rs``. The Rust ``#[cfg(test)]`` suite covers the
pure cores (bit 63/64/127 boundaries, the anti-drift bridge, rayon alignment,
validation parity); this file is the complementary REAL-wheel oracle pinning
what only the PyO3 boundary can prove:

* the probe-verified ``Vec<u128>`` extraction envelope — ``2**128`` and a
  negative int raise ``OverflowError`` at the FFI edge (bar_bits AND masks),
  while ``2**128 - 1`` round-trips exactly;
* the FFI-level bridge — on u64-fit inputs the installed u128 kernel emits
  tuples identical to the installed u64 kernel's;
* validation ``ValueError``s naming ``ResidentSweepSimU128`` (never the u64
  sibling's label) through the wheel.

HONEST envelope (Golden Rule #10): today's production vocabulary is 60 bits
(``IMPLEMENTED_MASK = (1 << 63) - 1``), so this kernel is future headroom —
production behaviour is unchanged and only tests (plus a future >64-bit
vocabulary) drive it.
"""

from __future__ import annotations

import fno_math
import pytest


def _arm(n: int) -> list[int]:
    """The tf==1 fixed-point ``arm_source`` for an ``n``-bar exec stream.

    At ``tf_minutes == 1`` the OHLC columns ARE their own execution grid, so
    1m bar ``j`` is the next-1m fill of decision bar ``j - 1``
    (``exec_idx(N) = N + 1`` — DEC-FILL-EXEC-GRID-1M-001) and bar 0 arms
    nothing. Mirrors ``_tf1_arm_source`` in
    ``tests/bench/test_exec_grid_dual_feed_bench.py``.
    """
    return [-1, *range(n - 1)]


def _flat_window(
    n: int, bar_bits: list[int]
) -> tuple[list[int], list[int], list[int], list[int], list[bool], list[int]]:
    """One-session window: open/close 10_000, high +150, low −150, no forced bars."""
    return (
        [10_000] * n,
        [10_150] * n,
        [9_850] * n,
        [10_000] * n,
        [False] * n,
        [0] * n,
    )


def _u128_sim(bar_bits: list[int]) -> fno_math.ResidentSweepSimU128:
    o, h, lo, c, forced, sess = _flat_window(len(bar_bits), bar_bits)
    return fno_math.ResidentSweepSimU128(o, h, lo, c, forced, sess, _arm(len(bar_bits)), bar_bits)


class TestU128FfiExtractionEnvelope:
    """The probe-pinned ``Vec<u128>`` edge: in-range exact, out-of-range OverflowError."""

    def test_max_u128_bar_bits_and_mask_round_trip(self) -> None:
        # 2**128 − 1 is IN envelope on both bar_bits and masks: the all-ones
        # mask fires only on the all-ones bar (arms bar0, worst-case fill at
        # high[1] = 10_150, end-of-data at the last bar's adverse low[2] =
        # 9_850 → pnl −300, mfe 0, mae 300, holding 2 — the one law,
        # DEC-FILL-WORST-CASE-ONLY-001).
        sim = _u128_sim([(1 << 128) - 1, 0, 0])
        out = sim.simulate_masks([(1 << 128) - 1], True, None, None)
        assert out == [[(-300, 0, 300, 2, 1, 2)]]

    @pytest.mark.parametrize("bad", [1 << 128, -1])
    def test_out_of_range_bar_bits_raise_overflow_error(self, bad: int) -> None:
        o, h, lo, c, forced, sess = _flat_window(2, [0, 0])
        with pytest.raises(OverflowError):
            fno_math.ResidentSweepSimU128(o, h, lo, c, forced, sess, _arm(2), [0, bad])

    @pytest.mark.parametrize("bad", [1 << 128, -1])
    def test_out_of_range_mask_raises_overflow_error(self, bad: int) -> None:
        sim = _u128_sim([1, 0, 0])
        with pytest.raises(OverflowError):
            sim.simulate_masks([bad], True, None, None)
        with pytest.raises(OverflowError):
            sim.count_trades([bad], True, None, None)


class TestU128WideMasksThroughTheWheel:
    """Wide (>64-bit) masks produce real trades through the installed wheel."""

    def test_bit_64_and_127_masks_fire_on_their_bars(self) -> None:
        # bar0 carries bits 63+64 (straddles the limb boundary), bar2 bit 127.
        sim = _u128_sim([(1 << 63) | (1 << 64), 0, 1 << 127, 0])
        out = sim.simulate_masks([(1 << 63) | (1 << 64), 1 << 64, 1 << 127], True, None, None)
        # Straddle + lone-bit-64 masks: bar0 is a superset of both → worst-case
        # entry at high[1] = 10_150, end-of-data at the adverse low[3] = 9_850
        # → pnl −300, mfe 0, mae 300, holding 3 (the one law).
        assert out[0] == [(-300, 0, 300, 3, 1, 3)]
        assert out[1] == out[0]
        # Bit-127 mask fires bar2 (the LAST bar) → the position opens and
        # immediately hits end_of_data with no next bar, so exit_idx==entry_idx==3
        # (the degenerate same-bar terminal). bar[3] = open 10_000, high 10_150,
        # low 9_850, close 10_000; worst-case BUY entry @high[3]=10_150.
        # DEC-FILL-EXEC-GRID-1M-001 (#1989) §1 — the legalized exit_idx==entry_idx
        # degenerate terminal prices the entry bar's CLOSE (the #1743 finish()-idiom
        # forced mark-to-close on end_of_data/session_gap), NOT the adverse LOW:
        # exit @close[3]=10_000 ⇒ pnl = 10_000 − 10_150 = −150. The prior −300
        # (adverse-LOW) was #1825's correct PRE-B3 value; it hid behind a TypeError
        # until the Class-1 signature fix a81c5a29 made this test collectable — a
        # stale golden, not a kernel bug. The other two −300 literals (non-degenerate)
        # remain adverse-LOW and unchanged.
        assert out[2] == [(-150, 0, 300, 1, 3, 3)]
        assert sim.count_trades([(1 << 63) | (1 << 64), 1 << 64, 1 << 127], True, None, None) == [
            1,
            1,
            1,
        ]

    def test_n_bars_getter(self) -> None:
        assert _u128_sim([0, 0, 0, 0]).n_bars == 4


class TestU128BridgeMatchesU64KernelAtFfi:
    """On u64-fit inputs the two installed kernels emit identical tuples."""

    def test_simulate_masks_and_count_trades_field_identical(self) -> None:
        bar_bits = [1, 0, 1, 1 << 63, 0]
        o, h, lo, c, forced, sess = _flat_window(5, bar_bits)
        s64 = fno_math.ResidentSweepSim(o, h, lo, c, forced, sess, _arm(5), bar_bits)
        s128 = fno_math.ResidentSweepSimU128(o, h, lo, c, forced, sess, _arm(5), bar_bits)
        masks = [0, 1, 1 << 63]
        assert s64.simulate_masks(masks, True, 80, 120) == s128.simulate_masks(masks, True, 80, 120)
        assert s64.count_trades(masks, True, 80, 120) == s128.count_trades(masks, True, 80, 120)


class TestU128ValidationNamesTheWideClass:
    """ValueErrors from the wide kernel name ResidentSweepSimU128 (never the u64 twin)."""

    def test_length_mismatch_valueerror(self) -> None:
        # Under DEC-FILL-EXEC-GRID-1M-001 ``bar_bits`` is DECISION-length,
        # decoupled from the 1m EXECUTION columns, so a short ``bar_bits`` is
        # legal; the length-locked set (OHLC/forced/session/arm_source) is what
        # ``build_columns`` rejects. Trigger the mismatch on a length-locked
        # column (short ``high``) — mirrors the Rust
        # ``u128_build_rejects_length_mismatch_naming_the_class`` — to keep
        # pinning the class-named error.
        o, h, lo, c, forced, sess = _flat_window(2, [0, 0])
        with pytest.raises(ValueError, match="ResidentSweepSimU128: column length mismatch"):
            fno_math.ResidentSweepSimU128(o, h[:1], lo, c, forced, sess, _arm(2), [0, 0])

    def test_price_out_of_range_valueerror(self) -> None:
        ceiling = (2**63 - 1) // 8  # MAX_PRICE_PAISA — re-derived independently
        _o, h, lo, c, forced, sess = _flat_window(2, [0, 0])
        with pytest.raises(ValueError, match="ResidentSweepSimU128: open value"):
            fno_math.ResidentSweepSimU128(
                [ceiling + 1, 10_000], h, lo, c, forced, sess, _arm(2), [0, 0]
            )

    def test_sl_tp_out_of_range_valueerror(self) -> None:
        ceiling = (2**63 - 1) // 8
        sim = _u128_sim([1, 0, 0])
        with pytest.raises(ValueError, match="ResidentSweepSimU128: sl_paisa"):
            sim.simulate_masks([1], True, ceiling + 1, None)
        with pytest.raises(ValueError, match="ResidentSweepSimU128: tp_paisa"):
            sim.count_trades([1], True, None, -1)
