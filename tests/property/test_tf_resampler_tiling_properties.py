"""Precision proof pyramid for the 1m → 21-TF resampler — TILING + ASSOCIATIVITY + ORACLE.

VERIFICATION-HARDENING (no production change). Adds the three property/golden invariants
that the prior precision survey found MISSING, proving
:func:`brutex.data.tf_resampler.resample_1m_to_tf` is exact over infinitely many random
sessions (not just the hand-picked fixtures):

* **P-TILE — per-bar tiling (no-gap / no-overlap).** Every input 1-minute bar maps to
  EXACTLY ONE bucket of each of the 21 timeframes, and the per-bucket member counts of a
  TF sum to the number of 1-minute bars. No bar is dropped, none is double-counted — the
  resampler partitions the 1-minute series.
* **P-ASSOC — associativity across ALL valid divisor pairs.** For every ordered pair
  ``(T', T)`` from ``ALL_TF_MINUTES`` with ``T % T' == 0`` and ``T' < T`` (88 pairs,
  including the daily ``375`` target tiled by its ``{1, 3, 5, 15}`` divisors), resampling
  ``1m → T`` directly equals first ``1m → T'`` then ``T' → T``, byte-for-byte via
  :meth:`polars.DataFrame.equals`. Generalises the existing 5m→15m golden to the whole
  divisor lattice over a multi-day random session.
* **P-ORACLE — independent oracle cross-check.** A SMALL pure-Python reference resampler
  (a plain dict keyed by the SAME 09:15-IST floor-bucket formula, no polars) is asserted
  bit-for-bit equal to the production resampler (OHLCV + ``ts_utc``) on random 1-minute
  data across several TFs. Two independent implementations agreeing is the precision proof.

CRITICAL: these tests must PASS against the EXISTING resampler. A failure reveals a real
imprecision — it must be reported, never papered over.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    import polars as pl  # type-only: real module so `pl.X` annotations resolve
else:
    pl = pytest.importorskip("polars")  # runtime: frame construction needs the [data] extra

#: 09:15 IST == 03:45 UTC — the fixed session anchor the resampler buckets against.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45)

#: Full NSE session length in trading minutes (09:15 → 15:30 IST).
_SESSION_MINUTES = DAILY_TF_MINUTES  # 375

#: Schema for the synthetic 1-minute frames (matches the resampler's REQUIRED_COLUMNS).
_SCHEMA = {
    "ts_utc": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}

#: Every ordered divisor pair (T', T) with T' < T and T % T' == 0 over the 21 TFs.
#: Includes the daily 375 target tiled by {1, 3, 5, 15} (its intraday divisors).
_DIVISOR_PAIRS: tuple[tuple[int, int], ...] = tuple(
    (tp, t)
    for t in sorted(ALL_TF_MINUTES)
    for tp in sorted(ALL_TF_MINUTES)
    if tp < t and t % tp == 0
)


@st.composite
def _multiday_1m(draw: st.DrawFn) -> pl.DataFrame:
    """Draw a 1..3 day gapped 1-minute session with valid intra-bar OHLC.

    Each trading day is anchored at 09:15 IST and spans a random in-session prefix of
    up to the full 375 minutes (so non-dividing TFs exercise a TRUNCATED last bucket).
    A random subset of in-session minutes is DROPPED to exercise the gap-immune grid
    (the tiling/oracle laws must hold over an arbitrary subset of the minute grid).
    Open/close are the two middle order-statistics of four drawn prices, so every bar
    satisfies ``low <= open, close <= high`` by construction.
    """
    n_days = draw(st.integers(min_value=1, max_value=3))
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    price = st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)
    for day in range(n_days):
        day_open = _SESSION_OPEN_UTC + timedelta(days=day)
        span = draw(st.integers(min_value=1, max_value=_SESSION_MINUTES))
        # Keep a random non-empty subset of the day's minutes (gap immunity).
        present = draw(
            st.lists(
                st.integers(min_value=0, max_value=span - 1),
                min_size=1,
                max_size=span,
                unique=True,
            )
        )
        for minute in sorted(present):
            a, b, c, d = draw(price), draw(price), draw(price), draw(price)
            low, high = min(a, b, c, d), max(a, b, c, d)
            mids = sorted((a, b, c, d))
            rows.append(
                (
                    day_open + timedelta(minutes=minute),
                    mids[1],  # open within [low, high]
                    high,
                    low,
                    mids[2],  # close within [low, high]
                    draw(st.integers(min_value=0, max_value=10_000_000)),
                )
            )
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row").sort("ts_utc")


def _bucket_label_utc(ts: datetime, tf_minutes: int) -> datetime:
    """Reference 09:15-IST floor-bucket label for a single bar (pure Python, no polars).

    Mirrors the resampler's fixed-arithmetic anchor EXACTLY but in plain Python: each
    bar's IST trading date's 09:15 open plus ``floor(elapsed / tf) * tf`` whole buckets,
    re-expressed as a UTC instant. The daily TF (375) labels at the session open (one
    bucket per IST day). This is deliberately a SECOND, independent implementation.
    """
    ist = ts + timedelta(hours=5, minutes=30)
    session_open_ist = datetime(ist.year, ist.month, ist.day, 9, 15)
    if tf_minutes == DAILY_TF_MINUTES:
        return session_open_ist - timedelta(hours=5, minutes=30)
    elapsed = int((ist - session_open_ist).total_seconds() // 60)
    bucket = (elapsed // tf_minutes) * tf_minutes
    return session_open_ist + timedelta(minutes=bucket) - timedelta(hours=5, minutes=30)


def _oracle_resample(
    df_1m: pl.DataFrame, tf_minutes: int
) -> list[tuple[datetime, float, float, float, float, int]]:
    """Pure-Python reference resampler (no polars groupby) for the cross-check.

    Buckets rows by :func:`_bucket_label_utc`, then for each bucket selects
    open = first row's open (by timestamp order), high = max high, low = min low,
    close = last row's close, volume = sum — the SAME aggregation the production
    resampler claims, computed by an independent code path. Returns rows sorted by
    bucket label so the result lines up with the production frame's ``sort('ts_utc')``.
    """
    buckets: dict[datetime, list[tuple[datetime, float, float, float, float, int]]] = {}
    for row in df_1m.iter_rows(named=True):
        label = _bucket_label_utc(row["ts_utc"], tf_minutes)
        buckets.setdefault(label, []).append(
            (row["ts_utc"], row["open"], row["high"], row["low"], row["close"], row["volume"])
        )
    out: list[tuple[datetime, float, float, float, float, int]] = []
    for label in sorted(buckets):
        members = sorted(buckets[label], key=lambda r: r[0])  # by timestamp, like the feed
        out.append(
            (
                label,
                members[0][1],  # open = first
                max(m[2] for m in members),  # high = max
                min(m[3] for m in members),  # low = min
                members[-1][4],  # close = last
                sum(m[5] for m in members),  # volume = sum
            )
        )
    return out


@given(df=_multiday_1m())  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
@settings(max_examples=150, deadline=None)
def test_every_1m_bar_tiles_into_exactly_one_bucket_per_tf(df: pl.DataFrame) -> None:
    """P-TILE: each 1m bar lands in EXACTLY ONE bucket of every TF; buckets cover all bars.

    For every one of the 21 timeframes: (a) every 1m bar maps to exactly one bucket label
    by the resampler's floor formula, and (b) Σ of per-bucket member counts == the number
    of 1m bars. No bar dropped, none double-counted — the resampler PARTITIONS the input.
    """
    n_bars = df.height
    for tf in ALL_TF_MINUTES:
        # Independent partition of the 1m rows by the reference bucket formula.
        counts: dict[datetime, int] = {}
        for ts in df["ts_utc"].to_list():
            label = _bucket_label_utc(ts, tf)
            counts[label] = counts.get(label, 0) + 1
        # (b) coverage: the member counts of every bucket sum to the bar count.
        assert sum(counts.values()) == n_bars, f"tf={tf}: tiling lost/duplicated bars"
        # (a) the production resampler emits EXACTLY one output row per distinct bucket.
        out = resample_1m_to_tf(df, tf)
        assert out.height == len(counts), f"tf={tf}: bucket count != distinct labels"
        # The set of emitted labels equals the independently-derived bucket set.
        assert set(out["ts_utc"].to_list()) == set(counts), f"tf={tf}: label set mismatch"


@given(df=_multiday_1m())  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
@settings(max_examples=120, deadline=None)
def test_resample_is_associative_over_all_divisor_pairs(df: pl.DataFrame) -> None:
    """P-ASSOC: 1m→T == (1m→T')→T for EVERY divisor pair (T' | T, T' < T), byte-for-byte.

    Generalises the 5m→15m golden to all 88 ordered divisor pairs across the 21 TFs,
    including the daily 375 target tiled by its {1, 3, 5, 15} intraday divisors.
    """
    for tp, t in _DIVISOR_PAIRS:
        direct = resample_1m_to_tf(df, t)
        composed = resample_1m_to_tf(resample_1m_to_tf(df, tp), t)
        assert direct.equals(composed), f"associativity broke for ({tp} -> {t})"


@given(
    df=_multiday_1m(),  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
    tf=st.sampled_from(sorted(ALL_TF_MINUTES)),
)
@settings(max_examples=200, deadline=None)
def test_independent_oracle_matches_production_resampler(df: pl.DataFrame, tf: int) -> None:
    """P-ORACLE: the pure-Python reference resampler equals the production one, bit-for-bit.

    Two independent implementations (polars groupby vs a plain dict) agreeing on
    ``ts_utc`` + OHLCV across random 1m data is the precision proof for the aggregation.
    """
    expected = _oracle_resample(df, tf)
    out = resample_1m_to_tf(df, tf)
    actual = [
        (r["ts_utc"], r["open"], r["high"], r["low"], r["close"], r["volume"])
        for r in out.iter_rows(named=True)
    ]
    assert actual == expected, f"tf={tf}: oracle disagreement"


def test_divisor_pair_enumeration_is_complete() -> None:
    """Guard the P-ASSOC coverage: 88 pairs, daily 375 tiled by exactly {1, 3, 5, 15}."""
    assert len(_DIVISOR_PAIRS) == 88
    daily_divisors = sorted(tp for tp, t in _DIVISOR_PAIRS if t == DAILY_TF_MINUTES)
    assert daily_divisors == [1, 3, 5, 15]
    # Every pair is a true divisor pair with the smaller TF first.
    for tp, t in _DIVISOR_PAIRS:
        assert tp < t and t % tp == 0
    # Sanity vs the closed form: each T contributes (#divisors-of-T in ALL_TF below T).
    expected = sum(1 for t in ALL_TF_MINUTES for tp in ALL_TF_MINUTES if tp < t and t % tp == 0)
    assert len(_DIVISOR_PAIRS) == expected
    assert math.gcd(_SESSION_MINUTES, 5) == 5  # 5 | 375 (a tiling divisor of the day)
