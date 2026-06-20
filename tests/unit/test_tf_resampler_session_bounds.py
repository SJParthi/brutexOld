"""Precision proof pyramid for the 1m → 21-TF resampler — SESSION BOUNDS + SHORT SESSIONS.

VERIFICATION-HARDENING (no production change). Adds the two explicit-unit invariants the
prior precision survey found MISSING, locking
:func:`brutex.data.tf_resampler.resample_1m_to_tf` against the session window
``[09:15:00 IST inclusive, 15:30:00 IST exclusive)``:

* **Session-bound + truncated last bar.** Over a FULL 375-minute session, every output
  ``ts_utc`` (in IST) has ``bar_start ∈ [09:15:00, 15:30:00)`` for ALL 21 TFs (never
  ``>= 15:30``); the bar count is ``ceil(375 / tf)``; and the TFs that do NOT divide 375
  end on a TRUNCATED final bucket (60m → 7 bars, last label 15:15 IST covering only the
  15:15–15:29 minutes; 30m → 13; 120m → 4; 180m → 3; 240m → 2). The truncated last bar's
  OHLCV equals the aggregation of ONLY the bars inside its clipped window.
* **Half-day / short session.** A short session (Muhurat 09:15–11:15 IST = 120 1m bars,
  and an early 13:00-IST close) yields ``ceil(session_minutes / tf)`` bars per TF, the
  last bar clamps to the actual last 1m bar present (NO synthetic bars past the short
  close), and every bar_start sits inside the short session.

CRITICAL: these must PASS against the EXISTING resampler. A failure reveals a real
imprecision — report it, never paper over it.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES

if TYPE_CHECKING:
    import polars as pl  # type-only: real module so `pl.X` annotations resolve
else:
    pl = pytest.importorskip("polars")  # runtime: frame construction needs the [data] extra

#: 09:15 IST == 03:45 UTC — the fixed session anchor.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45)
_IST_OFFSET = timedelta(hours=5, minutes=30)
_SESSION_MINUTES = DAILY_TF_MINUTES  # 375 (09:15 → 15:30 IST)

_SCHEMA = {
    "ts_utc": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _make_session(n_bars: int, *, open_utc: datetime = _SESSION_OPEN_UTC) -> pl.DataFrame:
    """``n_bars`` consecutive 1m bars from ``open_utc``; bar i has close = i, vol = i.

    OHLC is deterministic and distinct per bar (open=i, high=i+2, low=i-1, close=i+1)
    so the aggregation of any clipped window is exactly checkable.
    """
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    for i in range(n_bars):
        ts = open_utc + timedelta(minutes=i)
        rows.append((ts, float(i), float(i + 2), float(i - 1), float(i + 1), i))
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row")


def _ist_hhmm(ts: datetime) -> str:
    """Bar-start label as ``HH:MM`` IST."""
    return (ts + _IST_OFFSET).strftime("%H:%M")


class TestSessionBoundAndTruncatedLastBar:
    """Full 375-minute session: bounds, ceil count, and the truncated last bucket."""

    def test_every_bar_start_within_session_window_all_21_tfs(self) -> None:
        """No bar_start is < 09:15 or >= 15:30 IST, for ALL 21 TFs."""
        df = _make_session(_SESSION_MINUTES)
        open_ist = _SESSION_OPEN_UTC + _IST_OFFSET
        close_ist = open_ist + timedelta(minutes=_SESSION_MINUTES)  # 15:30 IST (exclusive)
        for tf in ALL_TF_MINUTES:
            out = resample_1m_to_tf(df, tf)
            for ts in out["ts_utc"].to_list():
                start_ist = ts + _IST_OFFSET
                assert open_ist <= start_ist < close_ist, (
                    f"tf={tf}: bar_start {start_ist} outside [09:15, 15:30) IST"
                )

    def test_bar_count_is_ceil_375_over_tf_all_21_tfs(self) -> None:
        """A gap-free 375-minute session yields exactly ceil(375 / tf) bars per TF."""
        df = _make_session(_SESSION_MINUTES)
        for tf in ALL_TF_MINUTES:
            out = resample_1m_to_tf(df, tf)
            expected = 1 if tf == DAILY_TF_MINUTES else math.ceil(_SESSION_MINUTES / tf)
            assert out.height == expected, f"tf={tf}: {out.height} != ceil({_SESSION_MINUTES}/{tf})"

    def test_non_dividing_tfs_have_expected_counts_and_truncated_last_label(self) -> None:
        """The non-divisors of 375 end on a truncated final bucket with the known label.

        375 = 3·5³. Of the multi-minute TFs, 30/60/120/180/240 do NOT divide 375, so
        each ends on a clipped last bucket: 30m→13 (last 15:15), 60m→7 (last 15:15),
        120m→4 (last 15:15), 180m→3 (last 15:15), 240m→2 (last 13:15).
        """
        df = _make_session(_SESSION_MINUTES)
        # (count, last-label-IST) per non-dividing TF.
        expected = {
            30: (13, "15:15"),
            60: (7, "15:15"),
            120: (4, "15:15"),
            180: (3, "15:15"),
            240: (2, "13:15"),
        }
        for tf, (n_bars, last_label) in expected.items():
            assert _SESSION_MINUTES % tf != 0, f"tf={tf} unexpectedly divides 375"
            out = resample_1m_to_tf(df, tf)
            assert out.height == n_bars, f"tf={tf}: {out.height} != {n_bars}"
            assert _ist_hhmm(out["ts_utc"].to_list()[-1]) == last_label, (
                f"tf={tf}: last label {_ist_hhmm(out['ts_utc'].to_list()[-1])} != {last_label}"
            )

    def test_truncated_last_bar_aggregates_only_its_clipped_window(self) -> None:
        """Each non-dividing TF's last bar = aggregation of ONLY the bars in its clip.

        The last bucket of a non-divisor starts at the largest multiple of tf below 375
        and is clipped at 375. Its OHLCV must equal the aggregation of exactly the 1m
        bars in ``[bucket_start_minute, 375)`` — never any phantom bar past 15:30.
        """
        df = _make_session(_SESSION_MINUTES)
        for tf in (30, 60, 120, 180, 240):
            out = resample_1m_to_tf(df, tf)
            last = out.row(out.height - 1, named=True)
            start_minute = (_SESSION_MINUTES // tf) * tf  # e.g. 60m -> 360
            clipped_len = _SESSION_MINUTES - start_minute  # e.g. 60m -> 15
            assert 0 < clipped_len < tf  # genuinely truncated
            window = df.slice(start_minute, clipped_len)
            assert last["open"] == window["open"][0], f"tf={tf} open"
            assert last["high"] == window["high"].max(), f"tf={tf} high"
            assert last["low"] == window["low"].min(), f"tf={tf} low"
            assert last["close"] == window["close"][-1], f"tf={tf} close"
            assert last["volume"] == window["volume"].sum(), f"tf={tf} volume"
            # And the truncated bucket's start is exactly its session-grid label.
            assert (last["ts_utc"] + _IST_OFFSET) == (
                _SESSION_OPEN_UTC + _IST_OFFSET + timedelta(minutes=start_minute)
            )


class TestShortSession:
    """Half-day / short sessions: ceil count, clamped last bar, in-session bounds."""

    def _assert_short_session(self, n_bars: int) -> None:
        df = _make_session(n_bars)
        open_ist = _SESSION_OPEN_UTC + _IST_OFFSET
        close_ist = open_ist + timedelta(minutes=n_bars)  # actual short close (exclusive)
        last_src = df.row(n_bars - 1, named=True)  # the actual last 1m bar present
        for tf in ALL_TF_MINUTES:
            out = resample_1m_to_tf(df, tf)
            expected = 1 if tf == DAILY_TF_MINUTES else math.ceil(n_bars / tf)
            assert out.height == expected, f"n={n_bars} tf={tf}: {out.height} != {expected}"
            # Every bar_start within the SHORT session (no bar at/after the early close).
            for ts in out["ts_utc"].to_list():
                start_ist = ts + _IST_OFFSET
                assert open_ist <= start_ist < close_ist, (
                    f"n={n_bars} tf={tf}: bar_start {start_ist} past the short close"
                )
            # Last bar clamps to the actual last 1m bar — no synthetic bar past the close.
            last = out.row(out.height - 1, named=True)
            assert last["close"] == last_src["close"], f"n={n_bars} tf={tf}: last close not clamped"
            assert last["high"] >= last_src["high"], f"n={n_bars} tf={tf}: last high lost data"

    def test_muhurat_two_hour_session_120_bars(self) -> None:
        """Muhurat-style 09:15–11:15 IST session = 120 1m bars."""
        self._assert_short_session(120)

    def test_early_close_13_00_ist_session_225_bars(self) -> None:
        """Early 13:00-IST close: 09:15–13:00 IST = 225 1m bars."""
        # 09:15 -> 13:00 IST is 3h45m = 225 minutes.
        assert (datetime(2024, 1, 2, 13, 0) - datetime(2024, 1, 2, 9, 15)) == timedelta(minutes=225)
        self._assert_short_session(225)

    def test_short_session_emits_no_bar_past_actual_last_minute(self) -> None:
        """The 240m TF on a 120-minute session is ONE clipped bucket, not a forced 240m bar."""
        df = _make_session(120)
        out = resample_1m_to_tf(df, 240)
        assert out.height == 1  # ceil(120/240) == 1, clipped at the 120th minute
        only = out.row(0, named=True)
        # The single bucket aggregates exactly the 120 present bars.
        assert only["open"] == df["open"][0]
        assert only["close"] == df["close"][-1]
        assert only["high"] == df["high"].max()
        assert only["low"] == df["low"].min()
        assert only["volume"] == df["volume"].sum()
        # Its label is the 09:15 grid start — not a synthetic later bucket.
        assert _ist_hhmm(only["ts_utc"]) == "09:15"
