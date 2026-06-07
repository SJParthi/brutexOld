"""Golden corpus #5 — cross-instrument resample 1m → 5m (associativity, P-08).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #5 —
"Cross-instrument resample (1m → 5m) / resample associativity, P-08").

Drives the REAL canonical resampler
(:func:`brutex.data.tf_resampler.resample_1m_to_tf`) over the SAME 10-bar
1-minute shape at three instrument price scales (NIFTY ≈ 14000, BANKNIFTY ≈
50000, SENSEX ≈ 73000) — the cross-instrument guarantee that the bucket math is
price-scale-agnostic — plus the **associativity** property P-08.

Hand-derived 1m → 5m buckets (left-closed, anchored at the 09:15 IST open).
For base ``B``, the ``i``-th 1m bar (``i = 0 .. 9``) is
``open = B+i, high = B+i+2, low = B+i-1, close = B+i+1, volume = 100+i``:

* **bucket 1** (bars 0-4) — open ``B``, high ``B+6``, low ``B-1``, close
  ``B+5``, volume ``sum(100..104) = 510``.
* **bucket 2** (bars 5-9) — open ``B+5``, high ``B+11``, low ``B+4``, close
  ``B+10``, volume ``sum(105..109) = 535``.

**Associativity (P-08)** — resampling 1m → 15m directly equals first resampling
1m → 5m and then 5m → 15m (15 is an integer multiple of 5, both anchored to the
same session open), proven byte-for-byte via :meth:`polars.DataFrame.equals`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
from brutex.data.tf_resampler import resample_1m_to_tf

#: 09:15 IST == 03:45 UTC — the session anchor the resampler buckets against.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45)

#: The three brute-force index price scales (cross-instrument coverage).
_SCALES = {"NIFTY": 14000, "BANKNIFTY": 50000, "SENSEX": 73000}

_SCHEMA = {
    "ts_utc": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _one_minute(base: int, n_bars: int) -> pl.DataFrame:
    """``n_bars`` synthetic 1m bars at price scale ``base`` (the shape above)."""
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    for i in range(n_bars):
        ts = _SESSION_OPEN_UTC + timedelta(minutes=i)
        rows.append(
            (
                ts,
                float(base + i),
                float(base + i + 2),
                float(base + i - 1),
                float(base + i + 1),
                100 + i,
            )
        )
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row")


def _expected_5m(base: int) -> pl.DataFrame:
    """The two hand-derived 5m buckets for the 10-bar feed at scale ``base``."""
    return pl.DataFrame(
        {
            "ts_utc": [_SESSION_OPEN_UTC, _SESSION_OPEN_UTC + timedelta(minutes=5)],
            "open": [float(base), float(base + 5)],
            "high": [float(base + 6), float(base + 11)],
            "low": [float(base - 1), float(base + 4)],
            "close": [float(base + 5), float(base + 10)],
            "volume": [510, 535],
        },
        schema=_SCHEMA,
    )


def test_one_to_five_minute_buckets_match_across_instruments() -> None:
    """The 1m → 5m aggregation is the hand-derived buckets at every price scale."""
    for base in _SCALES.values():
        resampled = resample_1m_to_tf(_one_minute(base, 10), 5)
        assert resampled.equals(_expected_5m(base))


def test_five_minute_aggregation_is_price_scale_agnostic() -> None:
    """Cross-instrument: the buckets differ only by the constant price offset."""
    nifty = resample_1m_to_tf(_one_minute(_SCALES["NIFTY"], 10), 5)
    banknifty = resample_1m_to_tf(_one_minute(_SCALES["BANKNIFTY"], 10), 5)
    delta = _SCALES["BANKNIFTY"] - _SCALES["NIFTY"]
    shifted = nifty.with_columns(
        (pl.col("open") + delta).alias("open"),
        (pl.col("high") + delta).alias("high"),
        (pl.col("low") + delta).alias("low"),
        (pl.col("close") + delta).alias("close"),
    )
    assert shifted.equals(banknifty)


def test_resample_is_associative_1m_5m_15m() -> None:
    """P-08: 1m → 15m == (1m → 5m) → 15m (15 is a multiple of 5, same anchor)."""
    feed = _one_minute(_SCALES["NIFTY"], 15)
    direct = resample_1m_to_tf(feed, 15)
    composed = resample_1m_to_tf(resample_1m_to_tf(feed, 5), 15)
    assert direct.equals(composed)
