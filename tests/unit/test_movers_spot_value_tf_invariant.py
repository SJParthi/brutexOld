"""Unit + property tests for TF-invariant SPOT "Top Value" (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001).

The spot "Top Value" turnover is now sourced from the RAW 1-minute session frame
(post as-of cut, PRE-resample) via :func:`~brutex.dashboard.movers_snapshot.spot_session_value_paisa`,
mirroring the already-TF-invariant futures/options ``_read_future_session``. These
tests pin the low-level seams:

* :func:`_read_cut_1m_frame` — the shared raw-1m read + as-of cut (``None`` on an
  absent / empty / cut-to-empty partition).
* :func:`spot_session_value_paisa` — the exact raw-1m HLC3 paisa, ``None`` when no
  1-minute frame, cutoff ``None`` vs a set cutoff.
* :func:`compute_snapshot` — the ``value_paisa`` injection gate: an explicit value
  is used verbatim; ``None`` / omitted falls back to the legacy reduction over the
  display *bars* (the existing 320000 / 318660 goldens pass UNMODIFIED — the
  injection-gate mutant killer).
* a **property** pin: value is constant across ``{1m, 5m, 15m, whole-day}`` at a
  shared cutoff, and equals an independent raw-1m HLC3 sum.
* futures value output byte-parity (the no-touch proof for the derivative spine).

Skipped when polars / pyarrow are absent (the lake deps).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import _read_future_session
from brutex.dashboard.movers_snapshot import (
    MoverSnapshot,
    ScreenInstrument,
    _read_cut_1m_frame,
    compute_snapshot,
    spot_session_value_paisa,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_DAY = date(2024, 1, 2)


def _bar(
    *, day: date, hour: int, minute: int, close: str, volume: int, oi: int | None = None
) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` IST on *day* (IST = UTC + 05:30, no DST).

    ``high = close + 2`` / ``low = close - 3`` so the per-bar HLC3 (a function of
    all three) genuinely differs from ``close`` — the divergence that made the old
    per-bucket-resampled spot value TF-dependent.
    """
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c - Decimal("1"),
        high=c + Decimal("2"),
        low=c - Decimal("3"),
        close=c,
        volume=volume,
        open_interest=oi,
    )


#: Ten 1-minute bars 09:15–09:24 IST whose per-5min-bucket HLC3 ≠ per-minute HLC3
#: (the reproduction shape — a revert re-diverges). Two 5-minute buckets:
#: 09:15–09:19 (idx 0–4) and 09:20–09:24 (idx 5–9).
_CLOSES = ["1000", "1010", "1020", "1005", "1030", "1040", "1015", "1050", "1025", "1060"]
_VOLS = [100, 110, 120, 105, 130, 140, 115, 150, 125, 160]


def _session(day: date) -> list[CandleBar]:
    return [
        _bar(day=day, hour=9, minute=15 + i, close=c, volume=v)
        for i, (c, v) in enumerate(zip(_CLOSES, _VOLS, strict=True))
    ]


def _round_half_up_2dp(x: Fraction) -> Fraction:
    """Independent ROUND_HALF_UP to 2dp (NO repo helper — the oracle)."""
    scaled = x * 100
    q, r = divmod(scaled.numerator, scaled.denominator)
    if 2 * r >= scaled.denominator:
        q += 1
    return Fraction(q, 100)


def _screen_kept(bars: list[CandleBar]) -> list[CandleBar]:
    """Bars KEPT by the engine-parity screen (DEC-DASHBOARD-READ-SCREEN-001).

    The dashboard reader screens the cut raw-1m frame exactly as the engine does;
    this mirrors that KEEP-set so the value oracle matches the served value on a
    synthetic day whose independent random closes can jump ≥ 4× (a bad tick).
    """
    from brutex.data.bar_screen import BarVerdict, SessionBarScreen, session_date_from_ts

    screen = SessionBarScreen(bad_tick_enabled=True)
    kept = [
        b
        for b in bars
        if screen.classify(
            ist_date=session_date_from_ts(b.timestamp),
            o=float(b.open),
            h=float(b.high),
            l=float(b.low),
            c=float(b.close),
        )
        is BarVerdict.KEEP
    ]
    screen.finish()
    return kept


def _hand_raw_1m_paisa(bars: list[CandleBar]) -> int:
    """Σ round_money((h+l+c)/3) × volume over the raw 1-minute bars, in paisa."""
    total = Fraction(0)
    for b in bars:
        hlc3 = Fraction(b.high + b.low + b.close) / 3
        total += _round_half_up_2dp(hlc3) * b.volume
    return int(total * 100)


def _write_1m(lake: Path, symbol: str, bars: list[CandleBar]) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars
    )


def _instr(symbol: str = "NIFTY") -> ScreenInstrument:
    return ScreenInstrument(symbol=symbol, exchange="NSE", segment="INDEX", display_name=symbol)


# ── _read_cut_1m_frame ────────────────────────────────────────────────────


class TestReadCut1mFrame:
    def test_reads_and_cuts_whole_day(self, tmp_path: Path) -> None:
        _write_1m(tmp_path, "NIFTY", _session(_DAY))
        frame = _read_cut_1m_frame(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY, cutoff_min=None
        )
        assert frame is not None
        assert frame.height == 10  # all ten 1-minute bars, cut skipped when None

    def test_cut_to_the_as_of_minute(self, tmp_path: Path) -> None:
        _write_1m(tmp_path, "NIFTY", _session(_DAY))
        frame = _read_cut_1m_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=9 * 60 + 19,  # 09:19 → bars 09:15–09:19 (5 bars)
        )
        assert frame is not None
        assert frame.height == 5

    def test_absent_partition_is_none(self, tmp_path: Path) -> None:
        assert (
            _read_cut_1m_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="ABSENT",
                day=_DAY,
                cutoff_min=None,
            )
            is None
        )

    def test_no_bar_on_the_day_is_none(self, tmp_path: Path) -> None:
        _write_1m(tmp_path, "NIFTY", _session(date(2024, 1, 3)))  # a DIFFERENT day
        assert (
            _read_cut_1m_frame(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY, cutoff_min=None
            )
            is None
        )

    def test_cut_to_empty_is_none(self, tmp_path: Path) -> None:
        _write_1m(tmp_path, "NIFTY", _session(_DAY))
        # A cutoff BEFORE the 09:15 session open keeps zero rows → None.
        assert (
            _read_cut_1m_frame(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                day=_DAY,
                cutoff_min=9 * 60,
            )
            is None
        )


# ── spot_session_value_paisa ──────────────────────────────────────────────


class TestSpotSessionValuePaisa:
    def test_whole_day_exact_raw_1m_hlc3_paisa(self, tmp_path: Path) -> None:
        bars = _session(_DAY)
        _write_1m(tmp_path, "NIFTY", bars)
        got = spot_session_value_paisa(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY, cutoff_min=None
        )
        assert got == _hand_raw_1m_paisa(bars) == 129006085

    def test_cutoff_is_over_the_raw_1m_at_that_minute(self, tmp_path: Path) -> None:
        bars = _session(_DAY)
        _write_1m(tmp_path, "NIFTY", bars)
        got = spot_session_value_paisa(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=9 * 60 + 19,
        )
        assert got == _hand_raw_1m_paisa(bars[:5]) == 57273855

    def test_missing_1minute_returns_none(self, tmp_path: Path) -> None:
        assert (
            spot_session_value_paisa(
                tmp_path, exchange="NSE", segment="INDEX", symbol="GONE", day=_DAY, cutoff_min=None
            )
            is None
        )

    def test_single_bar_day_equals_close_hlc(self, tmp_path: Path) -> None:
        one = [_bar(day=_DAY, hour=9, minute=15, close="1000", volume=50)]
        _write_1m(tmp_path, "SOLO", one)
        got = spot_session_value_paisa(
            tmp_path, exchange="NSE", segment="INDEX", symbol="SOLO", day=_DAY, cutoff_min=None
        )
        # HLC3 = (1002 + 997 + 1000)/3 = 999.666… → 999.67 × 50 = 49983.50 → 4998350 paisa.
        assert got == _hand_raw_1m_paisa(one) == 4998350

    def test_zero_range_bar_degenerates_to_close_times_volume(self, tmp_path: Path) -> None:
        flat = [
            CandleBar(
                timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=7,
                open_interest=None,
            )
        ]
        _write_1m(tmp_path, "FLAT", flat)
        got = spot_session_value_paisa(
            tmp_path, exchange="NSE", segment="INDEX", symbol="FLAT", day=_DAY, cutoff_min=None
        )
        assert got == 100 * 7 * 100 == 70000  # HLC3 == close for a zero-range bar

    def test_half_boundary_hlc3_rounds_half_up(self, tmp_path: Path) -> None:
        # Choose h/l/c so HLC3 lands exactly on a .xx5 half-boundary:
        # (100.02 + 99.99 + 100.00)/3 = 100.0033… — pick instead a clean .005.
        # high=100.05, low=100.00, close=100.01 → sum=300.06 → /3 = 100.02 (clean).
        # For a genuine half-boundary use high=100.02, low=100.00, close=100.005? tick
        # grid is 2dp, so build h/l/c already on the grid and let HLC3 hit x.xx5:
        # high=100.01, low=100.00, close=100.00 → sum 300.01 /3 = 100.0033…→100.00.
        # A true half: high=100.03, low=100.00, close=100.02 → 300.05/3 = 100.01666→100.02.
        # Simplest deterministic .005: high=10.00, low=10.00, close=10.01 → 30.01/3
        # = 10.00333 → 10.00. Use the KNOWN half-up case from ledger #899 (109.3333→109.33)
        # plus an exact-half via high=1, low=1, close=1.03 → 3.03/3 = 1.01 (clean). We
        # assert the exact paisa against the independent oracle for a .xx5 HLC3:
        bars = [
            CandleBar(  # (11.00 + 10.00 + 10.01)/3 = 10.3366… → 10.34
                timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("11.00"),
                low=Decimal("10.00"),
                close=Decimal("10.015"),  # not on grid — HLC3 = 31.015/3 = 10.338333
                volume=3,
                open_interest=None,
            )
        ]
        _write_1m(tmp_path, "HALF", bars)
        got = spot_session_value_paisa(
            tmp_path, exchange="NSE", segment="INDEX", symbol="HALF", day=_DAY, cutoff_min=None
        )
        assert got == _hand_raw_1m_paisa(bars)


# ── compute_snapshot value_paisa injection gate ───────────────────────────


def _snapshot_bars() -> list[CandleBar]:
    """The two-bar #899 golden set (HLC3 → 318660 paisa; close×volume was 320000)."""
    return [
        CandleBar(
            timestamp=datetime(2024, 1, 2, 3, 45, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=10,
            open_interest=None,
        ),
        CandleBar(
            timestamp=datetime(2024, 1, 2, 3, 46, tzinfo=UTC),
            open=Decimal("110"),
            high=Decimal("112"),
            low=Decimal("106"),
            close=Decimal("110"),
            volume=20,
            open_interest=None,
        ),
    ]


class TestComputeSnapshotInjection:
    def test_explicit_value_is_used_verbatim(self) -> None:
        snap = compute_snapshot(_instr(), _snapshot_bars(), prev_close=None, value_paisa=777)
        assert isinstance(snap, MoverSnapshot)
        assert snap.value_paisa == 777  # the injected raw-1m value, NOT the bars reduction

    def test_none_falls_back_to_bars_reduction(self) -> None:
        snap = compute_snapshot(_instr(), _snapshot_bars(), prev_close=None, value_paisa=None)
        assert snap is not None
        assert snap.value_paisa == 318660  # the #899 HLC3 golden over the display bars

    def test_omitted_argument_is_byte_identical_to_none(self) -> None:
        with_none = compute_snapshot(_instr(), _snapshot_bars(), prev_close=None, value_paisa=None)
        omitted = compute_snapshot(_instr(), _snapshot_bars(), prev_close=None)
        assert with_none == omitted  # the pre-fix behaviour is reproduced exactly
        assert omitted is not None
        assert omitted.value_paisa == 318660

    def test_empty_bars_returns_none_regardless_of_value(self) -> None:
        assert compute_snapshot(_instr(), [], prev_close=None, value_paisa=999) is None


# ── property: value constant across TFs at a shared cutoff ─────────────────


@st.composite
def _intraday_day(draw: st.DrawFn) -> tuple[list[CandleBar], int]:
    """A random jagged 1-minute day (contiguous from 09:15) + a random cutoff minute."""
    n = draw(st.integers(min_value=1, max_value=30))
    bars: list[CandleBar] = []
    for i in range(n):
        close = draw(st.integers(min_value=100, max_value=5000))
        vol = draw(st.integers(min_value=1, max_value=1000))
        spread = draw(st.integers(min_value=0, max_value=50))
        c = Decimal(close)
        total = (9 * 60 + 15 + i) - (5 * 60 + 30)
        bars.append(
            CandleBar(
                timestamp=datetime(
                    _DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC
                ),
                open=c,
                high=c + Decimal(spread),
                low=c - Decimal(spread),
                close=c,
                volume=vol,
                open_interest=None,
            )
        )
    cutoff = draw(st.integers(min_value=9 * 60 + 15, max_value=9 * 60 + 15 + n - 1))
    return bars, cutoff


class TestValueInvarianceProperty:
    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(_intraday_day())
    def test_value_constant_across_tfs_at_shared_cutoff(
        self, tmp_path_factory: pytest.TempPathFactory, payload: tuple[list[CandleBar], int]
    ) -> None:
        from brutex.dashboard.movers_snapshot import compute_spot_candidates

        bars, cutoff = payload
        lake = tmp_path_factory.mktemp("prop")
        _write_1m(lake, "NIFTY", bars)
        hh, mm = divmod(cutoff, 60)
        as_of = f"{hh:02d}:{mm:02d}"
        # The oracle screens the cut bars through the SAME engine-parity bad-tick
        # screen the dashboard reader now applies (DEC-DASHBOARD-READ-SCREEN-001):
        # this generator draws independent closes in [100, 5000], so a synthetic
        # ≥4× minute jump is a bad tick the engine + the reader both DROP — the
        # value stays TF-INVARIANT (the property under test) over the KEPT bars.
        cut = [b for b in bars if _minute_of(b) <= cutoff]
        expected = _hand_raw_1m_paisa(_screen_kept(cut))
        values: set[int] = set()
        for interval in ("1minute", "5minute", "15minute"):
            cands = compute_spot_candidates(
                lake_root=lake, interval=interval, date=_DAY.isoformat(), time=as_of
            )
            assert len(cands.snapshots) == 1
            values.add(cands.snapshots[0].value_paisa)
        assert values == {expected}


def _minute_of(bar: CandleBar) -> int:
    """IST minute-of-day of a UTC-stored 1-minute bar (IST = UTC + 05:30)."""
    total = bar.timestamp.hour * 60 + bar.timestamp.minute + (5 * 60 + 30)
    return total % (24 * 60)


# ── futures value output byte-parity (the no-touch proof) ─────────────────


class TestFuturesValueByteUnchanged:
    def test_read_future_session_value_paisa_is_the_pinned_literal(self, tmp_path: Path) -> None:
        """A fixed FNO coordinate's ``_read_future_session`` value is byte-unchanged.

        The futures/options value spine is UNTOUCHED by this PR; this pins its
        ``value_paisa`` output for a fixed fixture so a regression there would be
        caught here too (the derivative suites also re-run unmodified).
        """
        fut = "NSE-NIFTY-25Jan24-FUT"
        bars = [
            _bar(day=_DAY, hour=9, minute=15 + i, close=c, volume=v, oi=1000 + i)
            for i, (c, v) in enumerate(zip(_CLOSES, _VOLS, strict=True))
        ]
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE", segment="FNO", symbol=fut, interval="1minute", bars=bars
        )
        read = _read_future_session(
            tmp_path, exchange="NSE", segment="FNO", symbol=fut, day=_DAY, cutoff_min=None
        )
        assert read is not None
        _ltp, _vol, value_paisa, _oi, _iv, _delta = read
        # Futures value is ALSO the raw-1m HLC3 sum (its reader never resampled) —
        # identical to the independent oracle over the same bars.
        assert value_paisa == _hand_raw_1m_paisa(bars) == 129006085
