"""Tests for `brutex.data.spot_resample` — Track-1 derived-TF policy.

100% line+branch over a REAL polars 1m DataFrame: the result holds exactly
the 19 derived intraday timeframes (2m..15m + 30m/60m/120m/180m/240m), keyed
by canonical label, with the 1m base and 1d daily excluded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframes import ALL_TF_MINUTES, TF_LABELS

# The 19 derived intraday TFs = all 21 minus the 1m base and the 1d (375) daily.
_EXPECTED_LABELS = {TF_LABELS[m] for m in ALL_TF_MINUTES if m not in (1, 375)}


def _df_1m(n: int = 90) -> pl.DataFrame:
    base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)  # 09:15 IST
    return pl.DataFrame(
        {
            "ts_utc": [base + timedelta(minutes=i) for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        }
    )


def test_returns_exactly_the_19_derived_tfs() -> None:
    out = derive_intraday_tfs(_df_1m())
    assert set(out) == _EXPECTED_LABELS
    assert len(out) == 19


def test_excludes_1m_base_and_1d_daily() -> None:
    out = derive_intraday_tfs(_df_1m())
    assert "1m" not in out  # the source, not a derived output
    assert "1d" not in out  # fetched from /charts/historical, not resampled


def test_each_tf_is_a_nonempty_dataframe() -> None:
    out = derive_intraday_tfs(_df_1m())
    for label, frame in out.items():
        assert isinstance(frame, pl.DataFrame), label
        assert frame.height >= 1, label


def test_labels_are_canonical() -> None:
    # Spot-checks of the canonical labels across minute + multi-minute TFs.
    out = derive_intraday_tfs(_df_1m())
    for label in ("2m", "5m", "15m", "30m", "60m", "120m", "180m", "240m"):
        assert label in out
