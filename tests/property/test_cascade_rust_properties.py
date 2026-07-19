"""Property differentials for the Rust-hosted cascade/results-depth laws.

DEC-RUST-CASCADE-RESULTS-001. The converted Python implementations are
DELETED, so each property here drives the Rust-backed public API against a
VERBATIM in-test copy of the pre-conversion Python law (the retained
independent-oracle-in-test convention — the trade-sim naive-reference
precedent, R45): hypothesis generates hostile inputs, the oracle re-derives
the answer with the exact deleted-law tokens, and equality must be EXACT
(ints) / bit-exact (floats — the same IEEE operation sequence).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.combo_aggregates import _float_stats, nearest_rank
from brutex.engine.minute_path import (
    MinuteIndex,
    PathStatus,
    SessionEquityPoint,
    ist_session_dates,
    minute_path_max_drawdown,
    trade_excursions_1m,
)
from brutex.money import rupees_to_paisa
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_DAY0 = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)  # 09:16 IST


def _oracle_nearest_rank(sorted_values: list[int], pct: int) -> int:
    """VERBATIM pre-conversion law: rank = -(-pct·n // 100), value[rank-1]."""
    n = len(sorted_values)
    rank = -(-pct * n // 100)
    return sorted_values[rank - 1]


class TestNearestRankDifferential:
    @given(
        values=st.lists(st.integers(-(10**12), 10**12), min_size=1, max_size=200),
        pct=st.integers(min_value=1, max_value=100),
    )
    def test_matches_the_deleted_law(self, values: list[int], pct: int) -> None:
        ordered = sorted(values)
        assert nearest_rank(ordered, pct) == _oracle_nearest_rank(ordered, pct)

    @given(
        values=st.lists(st.integers(-(10**12), 10**12), min_size=1, max_size=200),
        pct=st.integers(min_value=1, max_value=100),
    )
    def test_returns_an_element(self, values: list[int], pct: int) -> None:
        ordered = sorted(values)
        assert nearest_rank(ordered, pct) in ordered


def _oracle_float_stats(values: list[float]) -> tuple[float, float, float]:
    """VERBATIM pre-conversion law: (min, clamped left-fold mean, max)."""
    lo = min(values)
    hi = max(values)
    return lo, min(max(sum(values) / len(values), lo), hi), hi


class TestFloatStatsDifferential:
    @given(
        values=st.lists(
            st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=100,
        )
    )
    def test_bit_exact_vs_the_deleted_law(self, values: list[float]) -> None:
        got = _float_stats(values)
        want = _oracle_float_stats(values)
        # Bit-exact: identical IEEE summation order + the same clamp.
        assert got == want
        lo, mean, hi = got
        assert lo <= mean <= hi


def _bar(minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_DAY0 + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=1,
        open_interest=None,
    )


@st.composite
def _bar_lists(draw: st.DrawFn) -> list[CandleBar]:
    """Strictly-increasing minute bars with valid 2-dp OHLC brackets."""
    n = draw(st.integers(min_value=1, max_value=30))
    minutes = sorted(draw(st.sets(st.integers(0, 120), min_size=n, max_size=n)))
    bars: list[CandleBar] = []
    for m in minutes:
        lo_c = draw(st.integers(9_000, 10_500))
        hi_c = draw(st.integers(lo_c, 11_000))
        o_c = draw(st.integers(lo_c, hi_c))
        c_c = draw(st.integers(lo_c, hi_c))
        bars.append(
            _bar(
                m,
                f"{o_c / 100:.2f}",
                f"{hi_c / 100:.2f}",
                f"{lo_c / 100:.2f}",
                f"{c_c / 100:.2f}",
            )
        )
    return bars


def _oracle_excursions(
    bars: list[CandleBar],
    entry_ts: datetime,
    exit_ts: datetime,
    entry_fill_paisa: int,
    direction: Side,
    exit_bar_full_range: bool,
) -> tuple[int | None, int | None, str]:
    """VERBATIM pre-conversion excursion fold (bisect + per-bar max)."""
    from bisect import bisect_left, bisect_right

    timestamps = [bar.timestamp for bar in bars]
    lo_i, hi_i = bisect_left(timestamps, entry_ts), bisect_right(timestamps, exit_ts)
    if lo_i == hi_i:
        return None, None, "no_1m_data"
    status = "ok" if timestamps[lo_i] == entry_ts and timestamps[hi_i - 1] == exit_ts else "partial"
    mfe = 0
    mae = 0
    for i in range(lo_i, hi_i):
        bar = bars[i]
        if timestamps[i] == exit_ts and not exit_bar_full_range:
            open_paisa = rupees_to_paisa(bar.open)
            high_paisa = open_paisa
            low_paisa = open_paisa
        else:
            high_paisa = rupees_to_paisa(bar.high)
            low_paisa = rupees_to_paisa(bar.low)
        if direction is Side.BUY:
            favourable = high_paisa - entry_fill_paisa
            adverse = entry_fill_paisa - low_paisa
        else:
            favourable = entry_fill_paisa - low_paisa
            adverse = high_paisa - entry_fill_paisa
        mfe = max(mfe, favourable)
        mae = max(mae, adverse)
    return mae, mfe, status


class TestExcursionsDifferential:
    @given(
        bars=_bar_lists(),
        entry_minute=st.integers(0, 120),
        span=st.integers(0, 120),
        fill=st.integers(9_500, 10_500),
        is_buy=st.booleans(),
        full_range=st.booleans(),
    )
    def test_matches_the_deleted_law(
        self,
        bars: list[CandleBar],
        entry_minute: int,
        span: int,
        fill: int,
        is_buy: bool,
        full_range: bool,
    ) -> None:
        entry_ts = _DAY0 + timedelta(minutes=entry_minute)
        exit_ts = entry_ts + timedelta(minutes=span)
        direction = Side.BUY if is_buy else Side.SELL
        index = MinuteIndex.from_bars(bars)
        got = trade_excursions_1m(
            index,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_fill_paisa=fill,
            direction=direction,
            exit_bar_full_range=full_range,
        )
        mae, mfe, status = _oracle_excursions(bars, entry_ts, exit_ts, fill, direction, full_range)
        assert (got.mae_paisa, got.mfe_paisa, got.status.value) == (mae, mfe, status)


def _oracle_drawdown(
    points: list[SessionEquityPoint], start_equity_paisa: int | None
) -> tuple[int, float]:
    """VERBATIM pre-conversion drawdown fold (independent paisa/pct maxima)."""
    peak: int | None = start_equity_paisa
    max_dd = 0
    max_dd_pct = 0.0
    for point in points:
        trough = point.equity_close_paisa
        if point.equity_intraday_low_paisa is not None:
            trough = min(trough, point.equity_intraday_low_paisa)
        if peak is not None:
            drop = peak - trough
            max_dd = max(max_dd, drop)
            if peak > 0 and drop > 0:
                max_dd_pct = max(max_dd_pct, drop / peak)
        peak = point.equity_close_paisa if peak is None else max(peak, point.equity_close_paisa)
    return max_dd, max_dd_pct


class TestDrawdownDifferential:
    @given(
        rows=st.lists(
            st.tuples(
                st.integers(-(10**9), 10**9),
                st.one_of(st.none(), st.integers(-(10**9), 10**9)),
            ),
            min_size=0,
            max_size=50,
        ),
        start=st.one_of(st.none(), st.integers(1, 10**9)),
    )
    def test_matches_the_deleted_law(
        self, rows: list[tuple[int, int | None]], start: int | None
    ) -> None:
        base = date(2024, 1, 1)
        points = [
            SessionEquityPoint(
                session_date=base + timedelta(days=i),
                equity_close_paisa=close,
                equity_intraday_low_paisa=low,
                trades_closed=0,
            )
            for i, (close, low) in enumerate(rows)
        ]
        got = minute_path_max_drawdown(points, start_equity_paisa=start)
        want = _oracle_drawdown(points, start)
        assert got == want  # int exact + float bit-exact (same IEEE ops)


class TestSessionDatesDifferential:
    @given(minutes=st.lists(st.integers(0, 5_000), min_size=0, max_size=60, unique=True))
    def test_matches_the_astimezone_law(self, minutes: list[int]) -> None:
        from zoneinfo import ZoneInfo

        bars = []
        for m in sorted(minutes):
            bars.append(
                CandleBar(
                    timestamp=_DAY0 + timedelta(minutes=m),
                    open=Decimal("1.00"),
                    high=Decimal("1.00"),
                    low=Decimal("1.00"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                )
            )
        got = ist_session_dates(bars)
        want = tuple(
            sorted({bar.timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date() for bar in bars})
        )
        assert got == want


class TestStatusVocabulary:
    def test_status_tokens_round_trip_the_enum(self) -> None:
        assert {status.value for status in PathStatus} == {"ok", "no_1m_data", "partial"}
