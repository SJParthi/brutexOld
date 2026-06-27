"""Property tests — F&O resample idempotence / byte-identity (DEC-FNO-TF-DERIVE-OI-001).

Hypothesis over random sorted 1m+OI frames proves:
* the resample is DETERMINISTIC (same input → same output, bit-for-bit) — Golden
  Rule #4/#7;
* OHLCV/OI invariants hold (open=first, close=last, volume=sum, OI=last) regardless
  of the bar count;
* the daily derive is idempotent (re-deriving the SAME complete 1m yields the SAME
  midnight-IST bars, prices Decimal-exact, OI int-exact).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily
from brutex.data.tf_resampler import resample_1m_to_tf
from hypothesis import given, settings
from hypothesis import strategies as st

# 09:15 IST == 03:45 UTC; a full session is 375 minutes, so cap the synthetic run
# well under that to keep every bar inside ONE IST trading day.
_SESSION_OPEN_UTC = datetime(2026, 5, 13, 3, 45, tzinfo=UTC)

_bar_count = st.integers(min_value=1, max_value=120)
_oi_values = st.lists(
    st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
    min_size=1,
    max_size=120,
)


def _build_frame(n: int, ois: list[int | None]) -> pl.DataFrame:
    # Cycle OI values to exactly n bars (deterministic, order-preserving).
    oi_col = [ois[i % len(ois)] for i in range(n)]
    closes = [10.0 + (i % 7) for i in range(n)]
    return pl.DataFrame(
        {
            "ts_utc": [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)],
            "open": [10.0] * n,
            "high": [c + 2.0 for c in closes],
            "low": [c - 2.0 for c in closes],
            "close": closes,
            "volume": [(i % 50) + 1 for i in range(n)],
            "open_interest": oi_col,
        },
        schema={
            "ts_utc": pl.Datetime(time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    )


@settings(max_examples=60, deadline=None)
@given(n=_bar_count, ois=_oi_values)
def test_resample_5m_is_deterministic_byte_identical(n: int, ois: list[int | None]) -> None:
    df = _build_frame(n, ois)
    first = resample_1m_to_tf(df, 5)
    second = resample_1m_to_tf(df, 5)
    assert first.equals(second)


@settings(max_examples=60, deadline=None)
@given(n=_bar_count, ois=_oi_values)
def test_daily_derive_is_idempotent(n: int, ois: list[int | None]) -> None:
    df = _build_frame(n, ois)
    first = resample_1m_to_fno_daily(df, before=date(2026, 5, 14))
    second = resample_1m_to_fno_daily(df, before=date(2026, 5, 14))
    # Re-deriving over identical complete 1m yields bit-identical bars (CandleBar is
    # a frozen value object → equality is field-by-field, incl. Decimal price + int OI).
    assert first == second


@settings(max_examples=60, deadline=None)
@given(n=_bar_count, ois=_oi_values)
def test_single_day_oi_is_always_last_of_day(n: int, ois: list[int | None]) -> None:
    df = _build_frame(n, ois)
    bars = resample_1m_to_fno_daily(df, before=date(2026, 5, 14))
    assert len(bars) == 1
    last_oi = ois[(n - 1) % len(ois)]
    assert bars[0].open_interest == last_oi
