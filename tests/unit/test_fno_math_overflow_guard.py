"""Python-FFI-level regression tests for the fno-math overflow guards (R2-RC-F1).

The ``rust/fno-math`` crate builds with ``overflow-checks = true`` +
``panic = "abort"`` (both LOCKED — never change them). Before the FFI-edge
guards, an integer overflow inside the extension did NOT raise a Python
exception — it SIGABRT'd the WHOLE host interpreter (exit code 134,
uncatchable from Python). These tests pin, at the REAL PyO3 boundary of the
installed wheel, that the guarded paths now raise a clean ``ValueError``
instead:

* ``fno_math.add_u64`` — the ``checked_add`` guard for a sum exceeding
  ``u64::MAX`` (the exact old-abort reproduction is ``2**63 + 2**63``).
* ``fno_math.ResidentSweepSim`` — the ``MAX_PRICE_PAISA = i64::MAX / 8``
  boundary on every price column at construction, and the
  ``[0, MAX_PRICE_PAISA]`` SL/TP distance boundary on ``simulate_masks`` /
  ``count_trades``.

The Rust ``#[cfg(test)]`` suite already covers the pure cores
(``add_u64_checked`` / ``check_price_column`` / ``check_level_distance``);
this file is the complementary Python-level oracle proving the ``PyResult``
wrappers actually surface ``ValueError`` through the wheel — the very fact a
``pytest.raises`` block below completes proves the process was NOT aborted.
"""

from __future__ import annotations

import fno_math
import pytest

# The crate's overflow-safe price ceiling: `MAX_PRICE_PAISA = i64::MAX / 8`
# (see `rust/fno-math/src/trade_sim.rs`). Re-derived here independently so a
# silent ceiling change breaks this pin.
MAX_PRICE_PAISA = (2**63 - 1) // 8
assert MAX_PRICE_PAISA == 1_152_921_504_606_846_975

U64_MAX = 2**64 - 1


def _arm(n: int) -> list[int]:
    """The tf==1 fixed-point ``arm_source`` for an ``n``-bar exec stream.

    At ``tf_minutes == 1`` the OHLC columns ARE their own execution grid, so
    1m bar ``j`` is the next-1m fill of decision bar ``j - 1``
    (``exec_idx(N) = N + 1`` — DEC-FILL-EXEC-GRID-1M-001) and bar 0 arms
    nothing. Mirrors ``_tf1_arm_source`` in
    ``tests/bench/test_exec_grid_dual_feed_bench.py``.
    """
    return [-1, *range(n - 1)]


class TestAddU64OverflowGuard:
    """``add_u64``: overflowing sums raise ``ValueError``, never SIGABRT."""

    def test_two_pow_63_operands_raise_value_error(self) -> None:
        """The EXACT old-abort reproduction: ``2**63 + 2**63`` (= 2**64)."""
        with pytest.raises(ValueError, match=r"add_u64: .*overflows u64"):
            fno_math.add_u64(2**63, 2**63)

    def test_minimal_off_by_one_overflow_raises(self) -> None:
        """``u64::MAX + 1`` — the smallest possible overflow, both orders."""
        with pytest.raises(ValueError, match="overflows u64"):
            fno_math.add_u64(U64_MAX, 1)
        with pytest.raises(ValueError, match="overflows u64"):
            fno_math.add_u64(1, U64_MAX)

    def test_max_plus_max_raises(self) -> None:
        with pytest.raises(ValueError, match="overflows u64"):
            fno_math.add_u64(U64_MAX, U64_MAX)

    def test_exact_fit_boundary_is_not_rejected(self) -> None:
        """The largest representable sums must still compute (guard teeth)."""
        assert fno_math.add_u64(U64_MAX, 0) == U64_MAX
        assert fno_math.add_u64(U64_MAX - 1, 1) == U64_MAX
        assert fno_math.add_u64(2**63, 2**63 - 1) == U64_MAX

    def test_in_range_values_compute_identically(self) -> None:
        """Regression: the pre-guard results are byte-identical in range."""
        assert fno_math.add_u64(2, 3) == 5
        assert fno_math.add_u64(2**32, 2**32) == 2**33


def _sim_columns(price: int) -> tuple[list[int], list[int], list[int], list[int]]:
    """One-bar OHLC columns all pinned at *price* paisa."""
    return ([price], [price], [price], [price])


def _valid_sim() -> fno_math.ResidentSweepSim:
    """A minimal in-range one-bar resident window (open=high=low=close)."""
    o, h, lo, c = _sim_columns(10_000)
    return fno_math.ResidentSweepSim(o, h, lo, c, [False], [0], _arm(1), [0])


class TestResidentSweepSimPriceBoundary:
    """Constructor rejects any price column outside ``±MAX_PRICE_PAISA``."""

    def test_out_of_range_open_raises_value_error(self) -> None:
        """The verify-phase probe value ``2**62 + 5`` (> i64::MAX/8, < i64::MAX)."""
        bad = 2**62 + 5
        o, h, lo, c = _sim_columns(bad)
        with pytest.raises(
            ValueError,
            match=(
                r"out of the overflow-safe range "
                r"\[-1152921504606846975, 1152921504606846975\] paisa"
            ),
        ):
            fno_math.ResidentSweepSim(o, h, lo, c, [False], [0], _arm(1), [0])

    @pytest.mark.parametrize("column", ["open", "high", "low", "close"])
    def test_each_column_is_guarded_and_named(self, column: str) -> None:
        """Every one of the four price columns rejects ``MAX + 1`` by name."""
        cols = {name: [10_000] for name in ("open", "high", "low", "close")}
        cols[column] = [MAX_PRICE_PAISA + 1]
        with pytest.raises(ValueError, match=rf"{column}.*out of the overflow-safe range"):
            fno_math.ResidentSweepSim(
                cols["open"], cols["high"], cols["low"], cols["close"], [False], [0], _arm(1), [0]
            )

    def test_negative_out_of_range_raises(self) -> None:
        o, h, lo, c = _sim_columns(-(MAX_PRICE_PAISA + 1))
        with pytest.raises(ValueError, match="out of the overflow-safe range"):
            fno_math.ResidentSweepSim(o, h, lo, c, [False], [0], _arm(1), [0])

    def test_exact_boundary_prices_are_accepted(self) -> None:
        """``±MAX_PRICE_PAISA`` exactly is VALID (inclusive range — guard teeth)."""
        for price in (MAX_PRICE_PAISA, -MAX_PRICE_PAISA):
            o, h, lo, c = _sim_columns(price)
            sim = fno_math.ResidentSweepSim(o, h, lo, c, [False], [0], _arm(1), [0])
            assert sim.n_bars == 1


class TestResidentSweepSimLevelDistanceBoundary:
    """``simulate_masks`` / ``count_trades`` reject SL/TP outside ``[0, MAX]``."""

    def test_simulate_masks_oversized_sl_raises(self) -> None:
        sim = _valid_sim()
        with pytest.raises(
            ValueError, match=r"sl_paisa.*out of the overflow-safe range \[0, 1152921504606846975\]"
        ):
            sim.simulate_masks([0], True, MAX_PRICE_PAISA + 1, None)

    def test_simulate_masks_negative_tp_raises(self) -> None:
        sim = _valid_sim()
        with pytest.raises(ValueError, match="tp_paisa.*out of the overflow-safe range"):
            sim.simulate_masks([0], True, None, -1)

    def test_count_trades_oversized_tp_raises(self) -> None:
        sim = _valid_sim()
        with pytest.raises(ValueError, match="tp_paisa.*out of the overflow-safe range"):
            sim.count_trades([0], False, None, MAX_PRICE_PAISA + 1)

    def test_boundary_distances_are_accepted(self) -> None:
        """``0`` and exactly ``MAX_PRICE_PAISA`` are VALID distances.

        Two bars so a trade can actually round-trip: the mask-0 entry signal on
        bar 0 fills at bar 1's open (next-bar-open entry), and the SL distance
        ``0`` puts the stop AT the entry price, which bar 1's own low touches —
        exactly one closed trade proves the sweep COMPUTED (not just validated).
        """
        price = 10_000
        two = [price, price]
        sim = fno_math.ResidentSweepSim(two, two, two, two, [False, False], [0, 0], _arm(2), [0, 0])
        counts = sim.count_trades([0], True, 0, MAX_PRICE_PAISA)
        assert counts == [1]

    def test_interpreter_survives_every_guard(self) -> None:
        """Meta-pin: after all the rejections above, the wheel still computes.

        A SIGABRT (exit 134) would have killed the whole pytest process before
        this line; a working ``add_u64`` proves the extension state is intact.
        """
        assert fno_math.add_u64(40, 2) == 42
