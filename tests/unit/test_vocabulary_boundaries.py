"""Boundary / permutation unit tests at the PYTHON ``evaluate_atoms`` shim level.

The per-bit suite (``test_vocabulary.py``) and the Rust crate's own tests cover
the happy-path firing of each condition. This file pins the EXACT boundary
behaviour of the Python production path (the shim + its ``trading_core``
delegation) for the corners most likely to regress silently — every assertion
verified against the live evaluator, not assumed:

* ``is_near`` is an inclusive ``<=`` band: a level exactly at ``close ± tol`` is
  near; one paisa beyond is not (``tol = close * bps // 10000``).
* ``close == 0`` is never near any level (the divide-by-the-close guard).
* A zero-body bar (``open == close``) is a DOJI; a full-range body is LARGE_BODY.
* A degenerate bar (``high == low``) fires NO shape bits despite ``open != close``.
* A flat day (``day_high == day_low``) fires the two AT bits but no upper/lower
  third (the ``day_range > 0`` guard).
* ``pdc == 0`` clears both gap bits (the ``pdc > 0`` guard) even on a huge open.
* Time-of-day bucket edges are half-open ``[start, end)``: 09:16:00 is the first
  EARLY_MORNING second, 09:15:59 is none (pre-open), 15:00:00 is none (the
  forced-exit boundary), 14:59:59 is the last AFTERNOON second.
* A wrong-length pivot/fib level list raises ``ValueError`` (the ABI guard).
* ``prior_n == 1`` with a single flag is BOTH directional AND "alternating" — a
  documented evaluator corner (the shared Rust core's n==1 convention) that
  co-sets a registered conflict pair; pinned so a future change is deliberate.

All money is int64 paisa; the snapshots are synthetic (no real data / no I/O).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import trading_core
from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)
from brutex.vocabulary import bits as B
from brutex.vocabulary.conflicts import has_conflict

_IST = ZoneInfo("Asia/Kolkata")
_PROX = ProximityParams(pivot_proximity_bps=10, pdh_pdl_proximity_bps=10, fib_proximity_bps=10)


def _bit(mask: int, pos: int) -> bool:
    return bool(mask & (1 << pos))


def _snapshot(
    *,
    timestamp_ist: datetime | None = None,
    open_paisa: int = 24_000_00,
    high_paisa: int = 24_010_00,
    low_paisa: int = 23_990_00,
    close_paisa: int = 24_005_00,
    pivot_p_paisa: int | None = None,
    pdh_paisa: int = 24_050_00,
    pdl_paisa: int = 23_950_00,
    pdc_paisa: int = 24_000_00,
    day_high_paisa: int = 24_010_00,
    day_low_paisa: int = 23_990_00,
    open_of_day_paisa: int = 24_000_00,
) -> IndicatorSnapshot:
    """Build a snapshot. ``pivot_p_paisa`` overrides the computed pivot ``P``."""
    ts = timestamp_ist or datetime(2026, 5, 13, 10, 30, tzinfo=_IST)
    day = ts.date()
    pv = compute_pivot_levels(day, pdh_paisa, pdl_paisa, pdc_paisa)
    if pivot_p_paisa is not None:
        from dataclasses import replace

        pv = replace(pv, p_paisa=pivot_p_paisa)
    return IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=open_paisa,
        high_paisa=high_paisa,
        low_paisa=low_paisa,
        close_paisa=close_paisa,
        ema20_paisa=None,
        ema200_paisa=None,
        pivots=pv,
        fib=compute_fib_levels(day, pdh_paisa, pdl_paisa),
        day_high_paisa=day_high_paisa,
        day_low_paisa=day_low_paisa,
        open_of_day_paisa=open_of_day_paisa,
    )


def _eval(snap: IndicatorSnapshot, prior: PriorBarHistory | None = None) -> int:
    return evaluate_atoms(snap, _PROX, prior or PriorBarHistory(prior_n=3, flags=[]))


class TestIsNearBoundary:
    """``near_pivot_p`` band is inclusive ``<=`` of ``close * bps // 10000``."""

    def test_level_exactly_at_lower_band_is_near(self) -> None:
        close = 24_000_00  # tol = 24_000_00 * 10 // 10000 = 24_00 paisa
        snap = _snapshot(close_paisa=close, pivot_p_paisa=close - 24_00)
        assert _bit(_eval(snap), B.BIT_NEAR_PIVOT_P)

    def test_one_paisa_below_lower_band_is_not_near(self) -> None:
        close = 24_000_00
        snap = _snapshot(close_paisa=close, pivot_p_paisa=close - 24_00 - 1)
        assert not _bit(_eval(snap), B.BIT_NEAR_PIVOT_P)

    def test_level_exactly_at_upper_band_is_near(self) -> None:
        close = 24_000_00
        snap = _snapshot(close_paisa=close, pivot_p_paisa=close + 24_00)
        assert _bit(_eval(snap), B.BIT_NEAR_PIVOT_P)

    def test_one_paisa_above_upper_band_is_not_near(self) -> None:
        close = 24_000_00
        snap = _snapshot(close_paisa=close, pivot_p_paisa=close + 24_00 + 1)
        assert not _bit(_eval(snap), B.BIT_NEAR_PIVOT_P)

    def test_zero_close_is_never_near(self) -> None:
        # close == 0 → tolerance 0 AND the guard short-circuits to "never near".
        snap = _snapshot(
            close_paisa=0,
            pivot_p_paisa=0,
            high_paisa=10_00,
            low_paisa=0,
            open_paisa=0,
            day_high_paisa=10_00,
            day_low_paisa=0,
        )
        assert not _bit(_eval(snap), B.BIT_NEAR_PIVOT_P)


class TestCandleShapeBoundaries:
    """Body/range corners: doji, large body, and the degenerate zero-range bar."""

    def test_zero_body_bar_is_doji_not_directional(self) -> None:
        # open == close, real range → body*10 (0) < range → DOJI; not bull/bear.
        snap = _snapshot(open_paisa=24_000_00, close_paisa=24_000_00)
        m = _eval(snap)
        assert _bit(m, B.BIT_BAR_DOJI)
        assert not _bit(m, B.BIT_BAR_BULLISH)
        assert not _bit(m, B.BIT_BAR_BEARISH)

    def test_full_range_body_is_large_body(self) -> None:
        # body == range (close at high, open at low) → body*10 > range*7.
        snap = _snapshot(
            open_paisa=23_990_00, close_paisa=24_010_00, high_paisa=24_010_00, low_paisa=23_990_00
        )
        m = _eval(snap)
        assert _bit(m, B.BIT_BAR_LARGE_BODY)
        assert not _bit(m, B.BIT_BAR_DOJI)
        assert not _bit(m, B.BIT_BAR_SMALL_BODY)

    def test_degenerate_zero_range_bar_fires_no_conditional_shape_bits(self) -> None:
        # high == low but open != close (corrupt-ish bar): the total_range > 0
        # guard must keep large/small/wick bits clear (no spurious fire).
        snap = _snapshot(
            open_paisa=24_000_00,
            close_paisa=24_005_00,
            high_paisa=24_000_00,
            low_paisa=24_000_00,
            day_high_paisa=24_000_00,
            day_low_paisa=24_000_00,
        )
        m = _eval(snap)
        for pos in (
            B.BIT_BAR_LARGE_BODY,
            B.BIT_BAR_SMALL_BODY,
            B.BIT_LONG_UPPER_WICK,
            B.BIT_LONG_LOWER_WICK,
        ):
            assert not _bit(m, pos)


class TestDayStructureBoundaries:
    """Flat-day guard: the AT bits fire but the third bits stay clear."""

    def test_flat_day_fires_at_bits_but_no_thirds(self) -> None:
        # day_high == day_low == close → close_at_day_high AND close_at_day_low,
        # but the day_range > 0 guard keeps the third bits clear.
        snap = _snapshot(
            close_paisa=24_000_00,
            high_paisa=24_000_00,
            low_paisa=24_000_00,
            open_paisa=24_000_00,
            day_high_paisa=24_000_00,
            day_low_paisa=24_000_00,
        )
        m = _eval(snap)
        assert _bit(m, B.BIT_CLOSE_AT_DAY_HIGH)
        assert _bit(m, B.BIT_CLOSE_AT_DAY_LOW)
        assert not _bit(m, B.BIT_CLOSE_IN_UPPER_THIRD)
        assert not _bit(m, B.BIT_CLOSE_IN_LOWER_THIRD)


class TestGapDayBoundaries:
    """``pdc == 0`` clears both gap bits even on a large positive open."""

    def test_zero_pdc_clears_gap_bits(self) -> None:
        # The shim passes hand-built levels verbatim; the pdc > 0 guard handles 0.
        snap = _snapshot(pdc_paisa=24_000_00, open_of_day_paisa=99_999_00)
        from dataclasses import replace

        snap = replace(snap, pivots=replace(snap.pivots, pdc_paisa=0))
        m = _eval(snap)
        assert not _bit(m, B.BIT_GAP_UP_DAY)
        assert not _bit(m, B.BIT_GAP_DOWN_DAY)


class TestTimeOfDayBoundaries:
    """Half-open ``[start, end)`` bucket edges to the second."""

    @pytest.mark.parametrize(
        ("hh", "mm", "ss", "expected_bit"),
        [
            (9, 15, 59, None),  # pre-open / before EARLY_MORNING_START — no bucket
            (9, 16, 0, B.BIT_EARLY_MORNING),  # first EARLY_MORNING second
            (9, 59, 59, B.BIT_EARLY_MORNING),  # last EARLY_MORNING second
            (10, 0, 0, B.BIT_MID_MORNING),  # first MID_MORNING second
            (11, 30, 0, B.BIT_MIDDAY),  # first MIDDAY second
            (13, 0, 0, B.BIT_AFTERNOON),  # first AFTERNOON second
            (14, 59, 59, B.BIT_AFTERNOON),  # last AFTERNOON second
            (15, 0, 0, None),  # forced-exit boundary — no bucket
        ],
    )
    def test_tod_bucket_edges(self, hh: int, mm: int, ss: int, expected_bit: int | None) -> None:
        snap = _snapshot(timestamp_ist=datetime(2026, 5, 13, hh, mm, ss, tzinfo=_IST))
        m = _eval(snap)
        tod_bits = (
            B.BIT_EARLY_MORNING,
            B.BIT_MID_MORNING,
            B.BIT_MIDDAY,
            B.BIT_AFTERNOON,
        )
        for pos in tod_bits:
            assert _bit(m, pos) is (pos == expected_bit)

    def test_sub_second_is_floored_for_bucketing(self) -> None:
        # 09:15:59.999999 floors to 09:15:59 → still pre-open (no bucket); the
        # microsecond never lifts it into EARLY_MORNING.
        snap = _snapshot(timestamp_ist=datetime(2026, 5, 13, 9, 15, 59, 999_999, tzinfo=_IST))
        m = _eval(snap)
        assert not _bit(m, B.BIT_EARLY_MORNING)


class TestLevelListLengthGuard:
    """The ABI rejects a wrong-length pivot/fib level list with ``ValueError``."""

    def test_short_pivot_list_raises(self) -> None:
        # PR-2: pivot_levels is 12 entries ([…, r5, s5]); the retired
        # vwap_paisa arg is GONE from the narrowed 21-arg ABI (crate 0.4.0).
        with pytest.raises(ValueError, match="pivot_levels must have 12 entries"):
            trading_core.evaluate_with_levels(
                0,
                1,
                1,
                1,
                1,
                None,
                None,
                [0] * 11,
                [0] * 15,
                1,
                1,
                1,
                10,
                10,
                10,
                3,
                [],
                False,
                False,
                False,
                False,
            )

    def test_short_fib_list_raises(self) -> None:
        with pytest.raises(ValueError, match="fib_levels must have 15 entries"):
            trading_core.evaluate_with_levels(
                0,
                1,
                1,
                1,
                1,
                None,
                None,
                [0] * 12,
                [0] * 14,
                1,
                1,
                1,
                10,
                10,
                10,
                3,
                [],
                False,
                False,
                False,
                False,
            )


class TestPriorBarHistoryBoundaries:
    """``PriorBarHistory.update`` ring + the documented ``prior_n == 1`` corner."""

    def test_update_keeps_only_last_n_flags(self) -> None:
        h = PriorBarHistory(prior_n=3, flags=[])
        for flag in (True, False, True, False, True):  # 5 updates, keep last 3
            h.update(flag)
        assert h.flags == [True, False, True]

    def test_update_below_capacity_keeps_all(self) -> None:
        h = PriorBarHistory(prior_n=4, flags=[])
        h.update(True)
        h.update(False)
        assert h.flags == [True, False]

    def test_prior_n_one_single_flag_is_directional_and_alternating(self) -> None:
        # DOCUMENTED corner (the alternating-window convention in the shared Rust
        # core, rust/trading-core/src/evaluator.rs): a 1-bar window
        # is BOTH all-bearish AND trivially "alternating", so it co-sets the
        # (prior_n_bearish, prior_alternating) registered conflict pair. Pinned so
        # any future change to the n==1 convention or the conflict set is deliberate.
        snap = _snapshot()
        m = _eval(snap, PriorBarHistory(prior_n=1, flags=[False]))
        assert _bit(m, B.BIT_PRIOR_N_BEARISH)
        assert _bit(m, B.BIT_PRIOR_ALTERNATING)
        assert has_conflict(m), "the n==1 directional+alternating overlap is a conflict pair"

    def test_prior_n_one_single_bullish_flag(self) -> None:
        snap = _snapshot()
        m = _eval(snap, PriorBarHistory(prior_n=1, flags=[True]))
        assert _bit(m, B.BIT_PRIOR_N_BULLISH)
        assert _bit(m, B.BIT_PRIOR_ALTERNATING)
