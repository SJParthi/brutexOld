"""Unit + property coverage for :mod:`brutex.data.bar_screen` (DEC-DASHBOARD-READ-SCREEN-001).

100% line + branch on ``brutex/data/bar_screen.py`` — the ONE COMMON screened
bar reader that routes the ``/movers`` + ``/db`` dashboard readers through the
SAME ``bad_tick`` + ``price_sanity`` screen the engine
(:func:`~brutex.feed.replay.arrow_table_to_bars`) applies, so a corrupt bar can
never render one number on ``/movers`` and a different one on ``/db`` (the
"split-brain"), nor diverge from what the engine refuses.

The ANTI-DIVERGENCE ORACLE (:class:`TestAntiDivergenceOracle`) is the load-bearing
guarantee: over random bar streams :class:`SessionBarScreen`'s KEEP-set equals
:func:`arrow_table_to_bars`'s kept set, so the two call sites can never diverge
and both inherit any future internal change to first-bar handling
(DEC-BADTICK-HEAD-SEED-001) automatically.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
import pyarrow as pa
from brutex.data.bad_tick import BAD_TICK_SKIP_EVENT
from brutex.data.bar_screen import (
    BarVerdict,
    SessionBarScreen,
    screen_frame,
    screen_rows,
    session_date_from_ts,
)
from brutex.feed.replay import arrow_table_to_bars
from hypothesis import given
from hypothesis import strategies as st

_DAY = date(2024, 1, 2)
# 09:15 IST == 03:45 UTC.
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
#: The split-brain reproducer: a ×10 fat-finger spike (positive + ordered — the
#: bad-tick class the positivity/ordering checks miss). Engine drops the 10000.
_REPRO_CLOSES = [1000.0, 1001.0, 10000.0, 1002.0, 1003.0]


def _movers_frame(closes: list[float], *, ts_col: str = "ts_utc"):
    """A tz-aware ascending bar frame (o/h/l/c = close ± 2, ordered + positive)."""
    ts = [_OPEN_UTC + timedelta(minutes=i) for i in range(len(closes))]
    return pl.DataFrame(
        {
            ts_col: pl.Series(ts).dt.replace_time_zone("UTC"),
            "open": [c - 1.0 for c in closes],
            "high": [c + 2.0 for c in closes],
            "low": [c - 3.0 for c in closes],
            "close": closes,
            "volume": [10] * len(closes),
        }
    )


def _arrow(closes: list[float], *, days: list[date] | None = None) -> pa.Table:
    """Canonical 7-column arrow table for :func:`arrow_table_to_bars`."""
    n = len(closes)
    ts = [_OPEN_UTC + timedelta(minutes=i) for i in range(n)]
    if days is not None:  # spread bars over the given day list (session boundaries)
        ts = [
            datetime.combine(d, _OPEN_UTC.timetz()) + timedelta(minutes=i)
            for i, d in enumerate(days)
        ]
    return pa.table(
        {
            "timestamp": ts,
            "open": [c - 1.0 for c in closes],
            "high": [c + 2.0 for c in closes],
            "low": [c - 3.0 for c in closes],
            "close": closes,
            "volume": [10] * n,
            "open_interest": [None] * n,
        }
    )


class TestSessionDateFromTs:
    def test_tz_aware_uses_ist_date(self) -> None:
        # 23:00 UTC on the 1st is 04:30 IST on the 2nd.
        ts = datetime(2024, 1, 1, 23, 0, tzinfo=UTC)
        assert session_date_from_ts(ts) == date(2024, 1, 2)

    def test_naive_uses_own_date(self) -> None:
        assert session_date_from_ts(datetime(2024, 1, 2, 5, 0)) == date(2024, 1, 2)


class TestSessionBarScreenClassify:
    def test_reproducer_spike_is_bad_tick(self) -> None:
        s = SessionBarScreen(bad_tick_enabled=True)
        verdicts = [s.classify(ist_date=_DAY, o=c, h=c, l=c, c=c).value for c in _REPRO_CLOSES]
        assert verdicts == ["keep", "keep", "drop_bad_tick", "keep", "keep"]
        assert s.insane_dropped == 0

    def test_insane_row_dropped_and_does_not_advance_baseline(self) -> None:
        s = SessionBarScreen(bad_tick_enabled=True)
        # 100 kept; an insane high<low bar dropped; 101 judged vs 100 (kept, not
        # vs the insane close) — proving the insane row never advanced the baseline.
        assert s.classify(ist_date=_DAY, o=100.0, h=101.0, l=99.0, c=100.0) is BarVerdict.KEEP
        assert s.classify(ist_date=_DAY, o=100.0, h=1.0, l=50.0, c=100.5) is BarVerdict.DROP_INSANE
        assert s.classify(ist_date=_DAY, o=101.0, h=102.0, l=100.0, c=101.0) is BarVerdict.KEEP
        assert s.insane_dropped == 1

    def test_null_price_is_insane(self) -> None:
        s = SessionBarScreen(bad_tick_enabled=True)
        assert s.classify(ist_date=_DAY, o=None, h=1.0, l=1.0, c=1.0) is BarVerdict.DROP_INSANE

    def test_kill_switch_off_keeps_spike_but_still_drops_insane(self) -> None:
        s = SessionBarScreen(bad_tick_enabled=False)
        verdicts = [s.classify(ist_date=_DAY, o=c, h=c, l=c, c=c) for c in _REPRO_CLOSES]
        assert all(v is BarVerdict.KEEP for v in verdicts)  # bad-tick arm OFF
        assert s.classify(ist_date=_DAY, o=100.0, h=1.0, l=50.0, c=100.5) is BarVerdict.DROP_INSANE

    def test_env_kill_switch_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        s = SessionBarScreen()
        assert s.classify(ist_date=_DAY, o=1001.0, h=1001.0, l=1001.0, c=1001.0) is BarVerdict.KEEP
        assert (
            s.classify(ist_date=_DAY, o=10000.0, h=10000.0, l=10000.0, c=10000.0)
            is BarVerdict.DROP_BAD_TICK
        )

    def test_finish_emits_one_warn_per_session(self) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            s = SessionBarScreen(bad_tick_enabled=True, source="x")
            for c in _REPRO_CLOSES:
                s.classify(ist_date=_DAY, o=c, h=c, l=c, c=c)
            s.finish()
        assert [entry["event"] for entry in logs].count(BAD_TICK_SKIP_EVENT) == 1

    def test_finish_no_bad_tick_arm_is_noop(self) -> None:
        SessionBarScreen(bad_tick_enabled=False).finish()  # must not raise


class TestScreenFrame:
    def test_reproducer_drops_spike(self) -> None:
        out = screen_frame(_movers_frame(_REPRO_CLOSES), ts_col="ts_utc", source="x")
        assert out.get_column("close").to_list() == [1000.0, 1001.0, 1002.0, 1003.0]

    def test_clean_frame_is_same_object(self) -> None:
        clean = _movers_frame([1000.0, 1001.0, 1002.0, 1003.0])
        assert screen_frame(clean, ts_col="ts_utc") is clean

    def test_empty_frame_is_same_object(self) -> None:
        empty = _movers_frame([1000.0]).head(0)
        assert screen_frame(empty, ts_col="ts_utc") is empty

    def test_futures_ts_col(self) -> None:
        out = screen_frame(_movers_frame(_REPRO_CLOSES, ts_col="timestamp"), ts_col="timestamp")
        assert out.get_column("close").to_list() == [1000.0, 1001.0, 1002.0, 1003.0]

    def test_insane_row_dropped(self) -> None:
        df = _movers_frame([1000.0, 1001.0, 1002.0])
        # Poison the middle bar's ordering (high<low) without touching close order.
        df = df.with_columns(
            pl.when(pl.arange(0, df.height) == 1).then(1.0).otherwise(pl.col("high")).alias("high")
        )
        out = screen_frame(df, ts_col="ts_utc")
        assert out.get_column("close").to_list() == [1000.0, 1002.0]

    def test_kill_switch_off_keeps_spike(self) -> None:
        out = screen_frame(_movers_frame(_REPRO_CLOSES), ts_col="ts_utc", bad_tick_enabled=False)
        assert out.get_column("close").to_list() == _REPRO_CLOSES


def _page(closes: list[float], *, descending: bool = False) -> tuple[list[tuple], list[str]]:
    cols = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
    rows = [
        (_OPEN_UTC + timedelta(minutes=i), c - 1.0, c + 2.0, c - 3.0, c, 10, None)
        for i, c in enumerate(closes)
    ]
    if descending:
        rows = list(reversed(rows))
    return rows, cols


class TestScreenRows:
    def test_reproducer_ascending(self) -> None:
        rows, cols = _page(_REPRO_CLOSES)
        out = screen_rows(rows, cols)
        assert [r[4] for r in out] == [1000.0, 1001.0, 1002.0, 1003.0]

    def test_reproducer_descending_preserves_render_order(self) -> None:
        rows, cols = _page(_REPRO_CLOSES, descending=True)
        out = screen_rows(rows, cols)
        # DESC render order, spike gone.
        assert [r[4] for r in out] == [1003.0, 1002.0, 1001.0, 1000.0]

    def test_clean_page_is_same_object(self) -> None:
        rows, cols = _page([1000.0, 1001.0, 1002.0])
        assert screen_rows(rows, cols) is rows

    def test_empty_page_is_same_object(self) -> None:
        empty: list[tuple] = []
        assert screen_rows(empty, ["timestamp", "open", "high", "low", "close"]) is empty

    def test_missing_ohlc_columns_serves_raw(self) -> None:
        rows = [(1,), (2,)]
        assert screen_rows(rows, ["timestamp"]) is rows

    def test_insane_row_dropped_per_row(self) -> None:
        cols = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
        rows = [
            (_OPEN_UTC, 100.0, 101.0, 99.0, 100.0, 10, None),
            (_OPEN_UTC + timedelta(minutes=1), 100.0, 1.0, 50.0, 100.5, 10, None),  # high<low
        ]
        out = screen_rows(rows, cols)
        assert [r[4] for r in out] == [100.0]

    def test_seed_catches_left_edge_spike(self) -> None:
        # A page whose FIRST row is itself a spike vs a same-session predecessor:
        # without a seed the first bar is exempt (kept); WITH the seed it drops.
        rows, cols = _page([10000.0, 1002.0, 1003.0])
        # shift the page's timestamps forward so a seed can precede them
        rows = [(t + timedelta(minutes=5), *rest) for (t, *rest) in rows]
        seed = (_OPEN_UTC, 1001.0, 1001.0, 1001.0, 1001.0)  # ts, o, h, l, close=1001
        no_seed = screen_rows(rows, cols)
        seeded = screen_rows(rows, cols, seed_bar=seed)
        # Without a seed the spike is the exempt first-of-session bar (kept) and
        # cascades — it drops the healthy successors (the split-brain left-edge
        # gap); WITH the seed the spike is judged vs 1001 and dropped correctly.
        assert [r[4] for r in no_seed] == [10000.0]
        assert [r[4] for r in seeded] == [1002.0, 1003.0]

    def test_kill_switch_off_keeps_spike(self) -> None:
        rows, cols = _page(_REPRO_CLOSES)
        out = screen_rows(rows, cols, bad_tick_enabled=False)
        assert [r[4] for r in out] == _REPRO_CLOSES


class TestAntiDivergenceOracle:
    """SessionBarScreen KEEP-set == arrow_table_to_bars kept set (the oracle)."""

    def _engine_and_screen(
        self, closes: list[float], days: list[date] | None = None
    ) -> tuple[list[float], list[float]]:
        table = _arrow(closes, days=days)
        engine = [float(b.close) for b in arrow_table_to_bars(table, source="oracle")]
        screen = SessionBarScreen(bad_tick_enabled=True, source="oracle")
        kept: list[float] = []
        ts_list = table.column("timestamp").to_pylist()
        o = table.column("open").to_pylist()
        h = table.column("high").to_pylist()
        low = table.column("low").to_pylist()
        c = table.column("close").to_pylist()
        for ts, oo, hh, ll, cc in zip(ts_list, o, h, low, c, strict=True):
            if (
                screen.classify(ist_date=session_date_from_ts(ts), o=oo, h=hh, l=ll, c=cc)
                is BarVerdict.KEEP
            ):
                kept.append(float(cc))
        screen.finish()
        return engine, kept

    def test_reproducer_matches_engine(self) -> None:
        engine, screen = self._engine_and_screen(_REPRO_CLOSES)
        assert screen == engine == [1000.0, 1001.0, 1002.0, 1003.0]

    def test_multi_session_reset(self) -> None:
        # Two sessions; a spike opening day 2 is the first bar (exempt) on BOTH.
        closes = [1000.0, 1001.0, 10000.0]
        days = [date(2024, 1, 2), date(2024, 1, 2), date(2024, 1, 3)]
        engine, screen = self._engine_and_screen(closes, days=days)
        assert screen == engine

    @given(
        closes=st.lists(
            st.floats(min_value=50.0, max_value=50000.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=40,
        )
    )
    def test_random_streams_keep_set_matches_engine(self, closes: list[float]) -> None:
        engine, screen = self._engine_and_screen(closes)
        assert screen == engine
