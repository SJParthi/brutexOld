"""The Fib-anchor sweep axis is REAL, not cosmetic (DEC-NIFTY-FIB-AXIS-001).

PR-11b wires the three Fibonacci-anchor strategy families — prev-day /
rolling-weekly / weekly-static — as a swept axis of the brute-force grid, so a
sweep actually tests all three strategies (before this PR only prev-day was ever
built). These tests PROVE the wiring is not cosmetic:

* the three anchors produce DISTINCT Group-4 Fib proximity bits on a synthetic
  history where the prev-day / trailing-5-session / last-ISO-week ranges all
  differ (the core anti-cosmetic test — before the axis all three would be
  byte-identical prev-day);
* two tuples differing ONLY by anchor get DISTINCT run-identities (``combo_hash``)
  AND DISTINCT store addresses (``_tuple_digest``) — Golden Rule #4, no collision;
* the grid enumerates the axis (×3 tuples), the ``max_tuples`` cap still trips
  loudly at ×3 (never a silent blowup), and the resident kernel carries the
  anchor (R-1) so a Near/… anchor never reuses a stale prev-day kernel;
* a cold Fib anchor clears ONLY the Group-4 Fib bits while the prev-day pivots
  stay warm (R-2), and no Fib level is ever fabricated on a cold week
  (Golden Rule #1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import (
    _DAILY_HISTORY_MAX,  # pyright: ignore[reportPrivateUsage]
    FIB_DEPENDENT_MASK,
    PIVOT_DEPENDENT_MASK,
    FibAnchorKind,
    Stage1AMatrixBuilder,
    build_bar_bits,
)
from brutex.engine.sweep_kernel import ResidentKernelCache, ResidentSweepKernel
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    _tuple_digest,  # pyright: ignore[reportPrivateUsage]
    count_sweep_tuples,
    iter_sweep_tuples,
)
from brutex.engine.sweep_run import canonical_trade_params, mask_run_id
from brutex.strategies.types import Side

# ─────────────────────────────────────────────────────────────────────────────
# Synthetic multi-day history whose prev-day / trailing-5 / last-ISO-week ranges
# are ALL DIFFERENT. Trading days (early-Jan 2021, all in-coverage NSE sessions):
#   week 1: Mon 01-04 .. Fri 01-08
#   week 2: Mon 01-11 .. Fri 01-15
#   week 3: Mon 01-18, Tue 01-19  (the TEST day is 01-19)
# For a bar on 2021-01-19:
#   prev-day        = 01-18 range
#   rolling-weekly  = max/min over {01-18, 01-15, 01-14, 01-13, 01-12}
#   weekly-static   = max/min over completed ISO week 2 {01-11 .. 01-15}
# We choose per-day OHLC so those three (high, low) pairs are pairwise distinct.
# ─────────────────────────────────────────────────────────────────────────────

_TRADING_DAYS: list[date] = [
    date(2021, 1, 4),
    date(2021, 1, 5),
    date(2021, 1, 6),
    date(2021, 1, 7),
    date(2021, 1, 8),
    date(2021, 1, 11),
    date(2021, 1, 12),
    date(2021, 1, 13),
    date(2021, 1, 14),
    date(2021, 1, 15),
    date(2021, 1, 18),
    date(2021, 1, 19),
]

#: Per-day (open, high, low, close) in rupees, hand-picked so the three anchors'
#: (high, low) windows for 2021-01-19 differ:
#:   * 01-18 (prev-day) → H=10600 L=10400
#:   * trailing-5 {01-12..01-15, 01-18} → H=11000 (from 01-13) L=9800 (from 01-14)
#:   * ISO week 2 {01-11..01-15} → H=11000 (01-13) L=9800 (01-14) — SAME hi/lo
#:     window pair as rolling here, so we bump 01-18 to widen rolling's high past
#:     week-2's, making all three distinct.
_DAY_OHLC: dict[date, tuple[str, str, str, str]] = {
    date(2021, 1, 4): ("10000", "10100", "9950", "10050"),
    date(2021, 1, 5): ("10050", "10200", "10000", "10150"),
    date(2021, 1, 6): ("10150", "10300", "10100", "10250"),
    date(2021, 1, 7): ("10250", "10400", "10200", "10350"),
    date(2021, 1, 8): ("10350", "10500", "10300", "10450"),
    # Week 2 — its own H/L window is {H=11200 (01-13), L=9800 (01-14)}.
    date(2021, 1, 11): ("10450", "10600", "10400", "10550"),
    date(2021, 1, 12): ("10550", "10700", "10500", "10650"),
    date(2021, 1, 13): ("10650", "11200", "10600", "10800"),  # week-2 HIGH
    date(2021, 1, 14): ("10800", "10900", "9800", "10100"),  # week-2 LOW
    date(2021, 1, 15): ("10100", "10500", "10050", "10300"),
    # 01-18 (prev-day) — a NARROW range distinct from both windows above; its high
    # (11500) also widens the trailing-5 window past week-2's 11200, so
    # rolling-weekly {H=11500, L=9800} ≠ weekly-static {H=11200, L=9800} ≠
    # prev-day {H=10600, L=10400}.
    date(2021, 1, 18): ("10500", "11500", "10400", "10600"),
    # 01-19 — the TEST day (intraday bars swept below).
    date(2021, 1, 19): ("10600", "10700", "10500", "10650"),
}


def _bar(ts_ist: datetime, o: str, h: str, low: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=ts_ist.astimezone(UTC),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=1000,
        open_interest=None,
    )


def _history_bars(*, test_day_bars: int = 30, prices_around: str | None = None) -> list[CandleBar]:
    """Build the multi-day 1-minute stream ending on 2021-01-19.

    Each prior day gets a handful of bars whose OHLC folds to the intended daily
    OHLC (the first bar carries the day's open + high + low so the accumulator's
    extremes are exact regardless of the later bars). The final day (01-19) gets
    ``test_day_bars`` bars; when ``prices_around`` is set every 01-19 bar trades
    flat at that price (to probe a specific Fib level), else it uses the day's
    natural OHLC.
    """
    bars: list[CandleBar] = []
    for day in _TRADING_DAYS:
        o, h, low, c = _DAY_OHLC[day]
        base = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        if day == _TRADING_DAYS[-1] and prices_around is not None:
            for i in range(test_day_bars):
                bars.append(
                    _bar(
                        base + timedelta(minutes=i),
                        prices_around,
                        prices_around,
                        prices_around,
                        prices_around,
                    )
                )
            continue
        n = test_day_bars if day == _TRADING_DAYS[-1] else 5
        # First bar establishes open + the day's extreme high/low + close.
        bars.append(_bar(base, o, h, low, c))
        for i in range(1, n):
            # Interior bars stay within [low, high] so they never move extremes.
            bars.append(_bar(base + timedelta(minutes=i), c, c, c, c))
    return bars


def _fib_bits_on_test_day(anchor: FibAnchorKind, *, prices_around: str | None = None) -> list[int]:
    """Return the Group-4 Fib bits of each 2021-01-19 bar under *anchor*."""
    bars = _history_bars(prices_around=prices_around)
    matrix = build_bar_bits(bars, fib_anchor=anchor)
    # The last 30 masks are the 01-19 test-day bars.
    return [mask & FIB_DEPENDENT_MASK for mask in matrix[-30:]]


# ────── The core anti-cosmetic test ──────


class TestAnchorsProduceDistinctFibBits:
    #: Each anchor's ``retrace_50`` price (rupees) for the 2021-01-19 windows the
    #: builder derives —
    #:   prev-day  {H=11500, L=10400} → 10950
    #:   rolling   {H=11500, L=9800}  → 10650
    #:   weekly    {H=11200, L=9800}  → 10500
    #: pairwise distinct (the three ranges are pairwise distinct), so a bar trading
    #: AT a given anchor's retrace_50 fires that anchor's Fib bit.
    _RETRACE_50: ClassVar[dict[FibAnchorKind, str]] = {
        FibAnchorKind.PREV_DAY: "10950",
        FibAnchorKind.ROLLING_WEEKLY: "10650",
        FibAnchorKind.WEEKLY_STATIC: "10500",
    }

    def test_each_anchor_fires_its_own_retrace_50_level(self) -> None:
        # Each anchor is WARM on 01-19 and fires when the price sits at ITS OWN
        # retrace_50 — proving all three resolve real, distinct levels (not a stub).
        for anchor, price in self._RETRACE_50.items():
            bits = _fib_bits_on_test_day(anchor, prices_around=price)
            assert any(bits), f"{anchor.value} Fib bits never fired at its retrace_50 {price}"

    def test_three_anchors_yield_distinct_fib_bits_on_the_test_day(self) -> None:
        # The core anti-cosmetic proof. Before this PR the builder hardcoded
        # prev-day, so ALL THREE anchors produced BYTE-IDENTICAL Fib bits — these
        # assertions would fail. Now:
        #   (a) at each anchor's OWN retrace_50 that anchor fires but the other two
        #       (whose levels sit elsewhere) do NOT, so the three vectors are NOT
        #       all identical at that probe (≥ 2 distinct);
        #   (b) each anchor's per-probe vector is pairwise distinct from the other
        #       two anchors' vectors ACROSS the probes (aggregate distinctness).
        per_anchor: dict[FibAnchorKind, list[list[int]]] = {a: [] for a in FibAnchorKind}
        for price in self._RETRACE_50.values():
            probe = {a: _fib_bits_on_test_day(a, prices_around=price) for a in FibAnchorKind}
            for a, bits in probe.items():
                per_anchor[a].append(bits)
            distinct = {tuple(v) for v in probe.values()}
            assert len(distinct) >= 2, f"all three anchors gave identical Fib bits at {price}"

        prev, roll, wkly = (per_anchor[a] for a in FibAnchorKind)
        # Aggregate: no two anchors produced the SAME sequence of probe responses —
        # each strategy family is genuinely its own computation (Golden Rule #4).
        assert prev != roll, "prev-day and rolling-weekly are indistinguishable"
        assert prev != wkly, "prev-day and weekly-static are indistinguishable"
        assert roll != wkly, "rolling-weekly and weekly-static are indistinguishable"

    def test_non_fib_groups_are_identical_across_anchors(self) -> None:
        # The Fib axis moves ONLY the Group-4 bits (R-2): every OTHER bit (EMA,
        # pivots, PDH, candle, prior-bar, time-of-day, VWAP, market-structure,
        # day-type) is byte-identical across the three anchors.
        bars = _history_bars()
        prev = build_bar_bits(bars, fib_anchor=FibAnchorKind.PREV_DAY)
        roll = build_bar_bits(bars, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        wkly = build_bar_bits(bars, fib_anchor=FibAnchorKind.WEEKLY_STATIC)
        non_fib = ~FIB_DEPENDENT_MASK
        assert [m & non_fib for m in prev] == [m & non_fib for m in roll]
        assert [m & non_fib for m in prev] == [m & non_fib for m in wkly]

    def test_prev_day_default_is_byte_identical_to_explicit_prev_day(self) -> None:
        # DEFAULT-PRESERVATION: an unset fib_anchor == explicit PREV_DAY.
        bars = _history_bars()
        assert build_bar_bits(bars) == build_bar_bits(bars, fib_anchor=FibAnchorKind.PREV_DAY)


# ────── Distinct run-identity + store address (Golden Rule #4) ──────


class TestAnchorRunIdentity:
    def test_anchor_yields_distinct_combo_hash(self) -> None:
        ctx = {
            "direction": "bull",
            "instrument": "NIFTY",
            "tf": "1minute",
            "mode": "A",
            "data_snapshot_hash": "snap",
            "indicator_version": "iv",
            "code_commit_sha": "sha",
        }

        def rid(anchor: FibAnchorKind) -> str:
            return mask_run_id(
                mask=0b101,
                params=canonical_trade_params(
                    sl_paisa=80,
                    tp_paisa=50,
                    prior_n=3,
                    proximity=None,
                    start_equity_paisa=10_000_000,
                    fib_anchor=anchor,
                ),
                **ctx,
            )

        ids = {rid(a) for a in FibAnchorKind}
        assert len(ids) == 3, "the three anchors must have three distinct run-identities"

    def test_anchor_yields_distinct_store_digest(self) -> None:
        grid = _grid(fib_anchors=list(FibAnchorKind))
        base: dict[str, object] = {
            "instrument": "NIFTY",
            "tf": Timeframe("1minute", 1),
            "direction": Side.BUY,
            "k": 1,
            "sl_paisa": 80,
            "tp_paisa": 50,
            "proximity": None,
        }
        digests = {
            _tuple_digest(SweepTuple(fib_anchor=a, **base), grid)  # type: ignore[arg-type]
            for a in FibAnchorKind
        }
        assert len(digests) == 3, "each anchor must map to its own Parquet store"


# ────── Grid enumeration + the ×3 cap ──────


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap",
        "indicator_version": "iv",
        "code_commit_sha": "sha",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


class TestGridAxis:
    def test_default_grid_is_prev_day_only(self) -> None:
        grid = _grid()
        assert tuple(grid.fib_anchors) == (FibAnchorKind.PREV_DAY,)
        assert count_sweep_tuples(grid) == 1

    def test_three_anchors_triple_the_tuples(self) -> None:
        grid = _grid(fib_anchors=list(FibAnchorKind))
        assert count_sweep_tuples(grid) == 3
        anchors = [t.fib_anchor for t in iter_sweep_tuples(grid)]
        assert set(anchors) == set(FibAnchorKind)
        assert len(anchors) == 3

    def test_empty_fib_axis_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="fib_anchors must be a non-empty axis"):
            _grid(fib_anchors=[])

    def test_duplicate_anchors_are_deduped(self) -> None:
        grid = _grid(fib_anchors=[FibAnchorKind.PREV_DAY, FibAnchorKind.PREV_DAY])
        assert tuple(grid.fib_anchors) == (FibAnchorKind.PREV_DAY,)

    def test_max_tuples_cap_trips_loudly_at_triple(self) -> None:
        # A single-anchor grid at the cap becomes ×3 with three anchors → the
        # loud opt-in guard must refuse (never a silent blowup).
        _grid(max_tuples=1)  # single-anchor grid is exactly at the cap → OK
        with pytest.raises(ValueError, match="tuples exceed max_tuples"):
            _grid(fib_anchors=list(FibAnchorKind), max_tuples=1)


# ────── R-1: resident kernel carries the anchor ──────


class TestKernelCarriesAnchor:
    def _bars(self) -> list[CandleBar]:
        return _history_bars()

    def test_kernel_records_its_anchor(self) -> None:
        bars = self._bars()
        kernel = ResidentSweepKernel(bars, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        assert kernel.fib_anchor is FibAnchorKind.ROLLING_WEEKLY
        # Its bar_bits equal a direct rolling-weekly build (bit-exact reuse).
        assert kernel.bar_bits == build_bar_bits(bars, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)

    def test_cache_keys_kernels_per_anchor(self) -> None:
        bars = self._bars()
        cache = ResidentKernelCache(capacity=8)
        prev = cache.get(
            bars,
            instrument="NIFTY",
            tf_label="1minute",
            proximity=None,
            prior_n=3,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )
        roll = cache.get(
            bars,
            instrument="NIFTY",
            tf_label="1minute",
            proximity=None,
            prior_n=3,
            fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
        )
        assert prev is not roll  # a different anchor never reuses a stale kernel
        assert cache.builds == 2
        # Re-fetch prev → a hit (proves the anchor is a stable part of the key).
        prev_again = cache.get(
            bars,
            instrument="NIFTY",
            tf_label="1minute",
            proximity=None,
            prior_n=3,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )
        assert prev_again is prev
        assert cache.hits == 1


# ────── R-2: cold Fib anchor clears ONLY the Fib bits, pivots stay warm ──────


class TestColdFibAnchorClearsOnlyFib:
    def _short_history_bars(self, n_prior_days: int) -> list[CandleBar]:
        """A stream with only *n_prior_days* completed days before a final day.

        With ``n_prior_days == 2`` the rolling-weekly anchor (needs 5 prior
        sessions) is COLD on the final day, while the prev-day pivots ARE warm
        (one prior day suffices).
        """
        days = _TRADING_DAYS[: n_prior_days + 1]
        bars: list[CandleBar] = []
        for day in days:
            o, h, low, c = _DAY_OHLC[day]
            base = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
            n = 20 if day == days[-1] else 5
            bars.append(_bar(base, o, h, low, c))
            for i in range(1, n):
                bars.append(_bar(base + timedelta(minutes=i), c, c, c, c))
        return bars

    def test_cold_rolling_weekly_clears_fib_but_keeps_pivots(self) -> None:
        bars = self._short_history_bars(2)
        rolling = build_bar_bits(bars, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        # Final-day bars (the last 20): rolling-weekly is COLD (< 5 prior
        # sessions), so EVERY Group-4 Fib bit is cleared (no fabricated level).
        final = rolling[-20:]
        assert all((m & FIB_DEPENDENT_MASK) == 0 for m in final), "cold Fib bits leaked"
        # But the prev-day pivot/PDH/day-type bits ARE warm (one prior day is
        # enough) — at least one final bar carries a pivot-group bit.
        assert any((m & PIVOT_DEPENDENT_MASK) != 0 for m in final), "pivots wrongly cleared"

    def test_warm_prev_day_still_fires_fib_when_rolling_is_cold(self) -> None:
        # The SAME 2-prior-day stream under the PREV_DAY anchor: prev-day is warm
        # from the 2nd day, so the Fib bits are NOT all-cleared (contrast with the
        # cold rolling-weekly above — proving the clear is anchor-specific).
        bars = self._short_history_bars(2)
        prev = build_bar_bits(bars, fib_anchor=FibAnchorKind.PREV_DAY)
        final = prev[-20:]
        assert any((m & FIB_DEPENDENT_MASK) != 0 for m in final), "prev-day Fib never fired"

    def test_fib_and_pivot_masks_are_disjoint(self) -> None:
        # The two clear-masks partition the anchor-dependent bits (no overlap), so
        # a Fib-only clear can never accidentally blank a pivot bit.
        assert FIB_DEPENDENT_MASK & PIVOT_DEPENDENT_MASK == 0


class TestDailyHistoryIsBounded:
    """The rolling daily-history is bounded to ``_DAILY_HISTORY_MAX`` (R-4, O(1)/bar).

    DEC-NIFTY-FIB-AXIS-001: a non-prev-day anchor records each completed day into a
    bounded ``OrderedDict`` and EVICTS the oldest once the bound is exceeded, so the
    per-bar memory stays O(1) over an arbitrarily long stream (never O(days)).
    """

    @staticmethod
    def _many_day_bars(n_completed_days: int) -> tuple[list[CandleBar], date]:
        """A stream of ``n_completed_days`` completed NSE sessions + a final partial day.

        A non-prev-day anchor resolves against REAL NSE trading days (the rolling
        window raises on a non-session date), so this walks actual in-coverage 2021
        sessions. Each session is one 09:15 bar (its whole OHLC); a trailing bar on
        one further session keeps the last completed day OUT of the accumulating slot.
        Returns the bars and the OLDEST completed session (which must be evicted).
        """
        from brutex.calendars.nse import is_trading_day

        sessions: list[date] = []
        cursor = date(2021, 1, 4)  # a Monday, in-coverage
        while len(sessions) < n_completed_days + 1:  # +1 → a final accumulating day
            if is_trading_day(cursor):
                sessions.append(cursor)
            cursor += timedelta(days=1)
        bars: list[CandleBar] = []
        price = 10000
        for day in sessions:
            base = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
            hi = str(price + 100)
            lo = str(price - 50)
            bars.append(_bar(base, str(price), hi, lo, str(price + 50)))
            price += 25
        return bars, sessions[0]

    def test_history_never_exceeds_the_bound_and_evicts_oldest(self) -> None:
        # Feed MORE than the bound of completed days so the eviction loop fires.
        n = _DAILY_HISTORY_MAX + 5
        bars, oldest_day = self._many_day_bars(n)
        builder = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        builder.build(bars)
        hist = builder._daily_history  # pyright: ignore[reportPrivateUsage]
        # The bound is enforced: at most `_DAILY_HISTORY_MAX` completed days retained.
        assert len(hist) == _DAILY_HISTORY_MAX
        # The retained days are the MOST-RECENT completed ones (oldest evicted).
        assert oldest_day not in hist, "oldest completed day was not evicted"

    def test_prev_day_anchor_keeps_history_empty(self) -> None:
        # The prev-day path never populates the rolling history (byte-identical +
        # allocation-free), so the eviction loop is never even reached for it.
        bars, _oldest = self._many_day_bars(_DAILY_HISTORY_MAX + 5)
        builder = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.PREV_DAY)
        builder.build(bars)
        assert len(builder._daily_history) == 0  # pyright: ignore[reportPrivateUsage]
