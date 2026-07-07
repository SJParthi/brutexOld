"""Python-FFI-level regression tests for the trading-core boundary guards.

The ``rust/trading-core`` crate builds with ``overflow-checks = true`` +
``panic = "abort"`` (both LOCKED — never change them). Before these FFI-edge
guards, garbage input to three metadata entry points did NOT raise a Python
exception — it SIGABRT'd the WHOLE host interpreter (rc=-6, uncatchable from
Python; each repro was CONFIRMED live on the pre-guard wheel):

* ``trading_core.minute_of_session(2**63 - 1, -1)`` — raw ``tod - open``
  i64-overflow in ``metadata.rs``;
* ``trading_core.weekday_from_ordinal(-2**63)`` — raw ``ordinal - 1``
  i64-overflow;
* ``trading_core.derive_bar_metadata(1, 2_000_000, 0, 0, 0, 1)`` — bypassed
  the ``seconds_of_day_impl`` guard that only fronted ``py_seconds_of_day``,
  so ``hour * 3600`` overflowed u32.

These tests pin, at the REAL PyO3 boundary of the installed wheel, that each
guarded path now raises a clean ``ValueError`` (the very fact a
``pytest.raises`` block completes proves the process was NOT aborted), and
that the valid domain is byte-unchanged. The Rust ``#[cfg(test)]`` suite
covers the pure ``*_impl`` cores; this file is the complementary Python-level
oracle — mirroring ``tests/unit/test_fno_math_overflow_guard.py``.

Also pinned here: the VWAP NEGATIVE-TYPICAL rejection. Historical note (the
trunc-vs-floor seam this guard closed): while the hand-synced Python twin
still existed, feeding bars ``(typical=-7, vol=1)`` then ``(0, 1)`` gave Rust
``-3`` (i128 ``/`` truncates toward zero) vs Python ``-4`` (``//`` floors)
pre-guard — a silent one-paisa twin divergence on garbage input. Both twins
then rejected ``typical_paisa < 0`` with ``ValueError`` (a negative typical is
not a real price — HLC3 of positive prices). The Python twin was deleted by
DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (``trading_core.VwapState`` is the
single source), and the Rust-boundary rejection stays pinned here so the
closed domain hole cannot silently reopen.
"""

from __future__ import annotations

from datetime import date

import pytest
import trading_core

I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
# Python date.max.toordinal() (9999-12-31) — the FFI ordinal-domain ceiling.
MAX_DAY_ORDINAL = date.max.toordinal()
assert MAX_DAY_ORDINAL == 3_652_059
# 23:59:59 as seconds-since-midnight — the FFI civil-seconds ceiling.
MAX_CIVIL_SECONDS = 86_399
# The PyO3 overflow guard ceiling (python.rs MAX_BAR_PAISA = i64::MAX / 65536).
MAX_BAR_PAISA = I64_MAX // 65536
OPEN_0915 = 9 * 3600 + 15 * 60


class TestMinuteOfSessionGuard:
    """``minute_of_session``: out-of-domain seconds raise, never SIGABRT."""

    def test_exact_abort_repro_raises_value_error(self) -> None:
        """The CONFIRMED pre-guard SIGABRT: ``(i64::MAX, -1)``."""
        with pytest.raises(ValueError, match="tod_seconds"):
            trading_core.minute_of_session(I64_MAX, -1)

    def test_each_operand_guarded_and_named(self) -> None:
        with pytest.raises(ValueError, match="session_open_seconds"):
            trading_core.minute_of_session(0, -1)
        with pytest.raises(ValueError, match="tod_seconds"):
            trading_core.minute_of_session(MAX_CIVIL_SECONDS + 1, 0)
        with pytest.raises(ValueError, match="session_open_seconds"):
            trading_core.minute_of_session(0, MAX_CIVIL_SECONDS + 1)
        with pytest.raises(ValueError, match="tod_seconds"):
            trading_core.minute_of_session(I64_MIN, 0)

    def test_valid_domain_byte_unchanged(self) -> None:
        """Boundary + in-domain values equal the independent floor reference."""
        for tod, open_s in [(0, 0), (MAX_CIVIL_SECONDS, 0), (0, MAX_CIVIL_SECONDS)]:
            assert trading_core.minute_of_session(tod, open_s) == (tod - open_s) // 60
        # The honest pre-open NEGATIVE minute survives the guard.
        assert trading_core.minute_of_session(9 * 3600, OPEN_0915) == -15


class TestWeekdayFromOrdinalGuard:
    """``weekday_from_ordinal``: garbage ordinals raise, never SIGABRT."""

    def test_exact_abort_repro_raises_value_error(self) -> None:
        """The CONFIRMED pre-guard SIGABRT: ``-2**63``."""
        with pytest.raises(ValueError, match="day_ordinal"):
            trading_core.weekday_from_ordinal(I64_MIN)

    def test_domain_is_python_date_toordinal(self) -> None:
        with pytest.raises(ValueError, match="day_ordinal"):
            trading_core.weekday_from_ordinal(0)
        with pytest.raises(ValueError, match="day_ordinal"):
            trading_core.weekday_from_ordinal(MAX_DAY_ORDINAL + 1)

    def test_valid_domain_byte_unchanged(self) -> None:
        for d in (date.min, date(2026, 6, 30), date.max):
            assert trading_core.weekday_from_ordinal(d.toordinal()) == d.weekday()


class TestDeriveBarMetadataGuard:
    """``derive_bar_metadata``: every component guarded, never SIGABRT."""

    def test_exact_abort_repro_raises_value_error(self) -> None:
        """The CONFIRMED pre-guard SIGABRT: ``hour = 2_000_000`` bypassing
        the guard that only fronted ``py_seconds_of_day``."""
        with pytest.raises(ValueError, match="hour"):
            trading_core.derive_bar_metadata(1, 2_000_000, 0, 0, 0, 1)

    def test_every_component_guarded_and_named(self) -> None:
        ok = date(2026, 6, 30).toordinal()
        with pytest.raises(ValueError, match="day_ordinal"):
            trading_core.derive_bar_metadata(0, 10, 0, 0, OPEN_0915, ok)
        with pytest.raises(ValueError, match="expiry_ordinal"):
            trading_core.derive_bar_metadata(ok, 10, 0, 0, OPEN_0915, I64_MIN)
        with pytest.raises(ValueError, match="session_open_seconds"):
            trading_core.derive_bar_metadata(ok, 10, 0, 0, -1, ok)
        with pytest.raises(ValueError, match="minute"):
            trading_core.derive_bar_metadata(ok, 10, 60, 0, OPEN_0915, ok)
        with pytest.raises(ValueError, match="second"):
            trading_core.derive_bar_metadata(ok, 10, 0, 60, OPEN_0915, ok)

    def test_valid_domain_byte_unchanged(self) -> None:
        """The production-shaped call (real ordinals, 09:20:30 IST) still
        returns the exact pre-guard tuple."""
        d = date(2026, 6, 30)  # Tuesday
        expiry = date(2026, 7, 3)
        tod, weekday, minute, is_expiry = trading_core.derive_bar_metadata(
            d.toordinal(), 9, 20, 30, OPEN_0915, expiry.toordinal()
        )
        assert tod == 9 * 3600 + 20 * 60 + 30
        assert weekday == 1
        assert minute == 5
        assert bool(is_expiry) is False
        # Expiry-day flag at the exact domain ceiling.
        _, _, _, on_expiry = trading_core.derive_bar_metadata(
            MAX_DAY_ORDINAL, 23, 59, 59, MAX_CIVIL_SECONDS, MAX_DAY_ORDINAL
        )
        assert bool(on_expiry) is True


class TestVwapNegativeTypicalRejected:
    """The floor-vs-trunc domain hole stays CLOSED at the PyO3 boundary:
    negative typical raises instead of silently entering the accumulator.

    Historical teeth (kept so the closed hole cannot silently reopen): the
    (-7,1),(0,1) stream is the exact empirical input on which the Rust i128
    truncating ``/`` (-3) and the deleted Python twin's flooring ``//`` (-4)
    diverged pre-guard."""

    def test_rust_state_rejects_negative_typical(self) -> None:
        state = trading_core.VwapState()
        with pytest.raises(ValueError, match="non-negative"):
            state.update(-7, 1, False, 1, 739_621, OPEN_0915)
        # Guard-first ordering: the rejected bar left NO state behind.
        assert state.current() is None

    def test_divergence_stream_rejected_at_first_bar(self) -> None:
        """The exact empirical divergence input is rejected up front — the
        state can never reach the bar where the trunc-vs-floor answers
        differed."""
        rs = trading_core.VwapState()
        with pytest.raises(ValueError, match="non-negative"):
            rs.update(-7, 1, False, 1, 739_621, OPEN_0915)

    def test_zero_typical_still_valid(self) -> None:
        """The guard is strictly ``< 0`` — the golden fixture's zero-typical
        bar (a valid drag-to-zero print) stays accepted and bit-identical to
        the independently-derived volume-weighted answer."""
        rs = trading_core.VwapState()
        for typical, volume in [(24_000_00, 100), (0, 50)]:
            rs_out = rs.update(typical, volume, False, 1, 739_621, OPEN_0915)
            assert rs_out == rs.current()
        assert rs.current() == (24_000_00 * 100) // 150

    def test_rust_state_magnitude_ceiling_still_enforced(self) -> None:
        """The pre-existing i128-overflow ceiling guard is unchanged."""
        state = trading_core.VwapState()
        with pytest.raises(ValueError, match="typical_paisa"):
            state.update(MAX_BAR_PAISA + 1, 1, False, 1, 739_621, OPEN_0915)
        with pytest.raises(ValueError, match="volume"):
            state.update(0, MAX_BAR_PAISA + 1, False, 1, 739_621, OPEN_0915)
