"""Derived-TF Greeks CLOSE-SNAPSHOT propagation (DEC-GREEKS-ENGINE-PR3-001).

Plan §15.10 (semantics LOCKED by DEC-GREEKS-ENGINE-PR1-001): a derived
N-minute bar's stored Greeks overlay (DEC-GREEKS-ENGINE-PR2B-001) equals
EXACTLY its LAST constituent 1-minute bar's values — never summed, never
averaged, never OHLC-aggregated; an NA last constituent propagates the SAME
honest NULL, never backfilled from an earlier minute; a spot/futures frame
with no overlay columns stays byte-identical. Includes the §15.10 test
obligation: property tests over random 1-minute windows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from brutex.data.fno_resample import resample_session_to_tf
from brutex.data.models import GREEKS_OVERLAY_FIELDS
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import DAILY_TF_MINUTES
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    import polars

pl = pytest.importorskip("polars", reason="the resampler needs polars")

#: 09:15 IST session open = 03:45 UTC.
_OPEN = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)


def _frame(n: int, *, iv: list[float | None] | None = None) -> polars.DataFrame:
    """An enriched option 1-minute frame: n bars from the session open.

    Every overlay column is populated with a per-minute-distinct value (so a
    wrong aggregation — mean/sum/first — is detectable), except ``iv`` which
    the caller may override with explicit NULLs.
    """
    ts = [_OPEN + timedelta(minutes=i) for i in range(n)]
    data: dict[str, object] = {
        "ts_utc": ts,
        "open": [100.0 + i for i in range(n)],
        "high": [105.0 + i for i in range(n)],
        "low": [95.0 + i for i in range(n)],
        "close": [100.0 + i for i in range(n)],
        "volume": [10] * n,
        "open_interest": [100 + i for i in range(n)],
    }
    schema: dict[str, object] = {
        "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "open_interest": pl.Int64,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        if name == "greeks_provenance_id":
            data[name] = [1_000_000 + i for i in range(n)]
            schema[name] = pl.Int32
        elif name == "iv" and iv is not None:
            data[name] = iv
            schema[name] = pl.Float64
        else:
            data[name] = [round(0.1 + 0.001 * i, 6) for i in range(n)]
            schema[name] = pl.Float64
    return pl.DataFrame(data, schema=schema)  # type: ignore[arg-type]


class TestCloseSnapshotRule:
    def test_derived_bar_greeks_equal_last_constituent(self) -> None:
        df = _frame(10)
        out = resample_1m_to_tf(df, 5).sort("ts_utc")
        assert out.height == 2
        for name in GREEKS_OVERLAY_FIELDS:
            assert name in out.columns
            # Bucket [09:15,09:20) last minute = index 4; [09:20,09:25) = index 9.
            assert out[name].to_list() == [df[name][4], df[name][9]]

    def test_never_aggregated(self) -> None:
        """The snapshot is the last VALUE — provably not sum/mean/first/max."""
        df = _frame(5)
        out = resample_1m_to_tf(df, 5)
        iv = out["iv"].to_list()[0]
        column = df["iv"].to_list()
        assert iv == column[-1]
        assert iv != sum(column)
        assert iv != sum(column) / len(column)
        assert iv != column[0]

    def test_daily_bucket_snapshots_last_minute(self) -> None:
        df = _frame(20)
        out = resample_1m_to_tf(df, DAILY_TF_MINUTES)
        assert out.height == 1
        assert out["iv"].to_list() == [df["iv"][19]]
        assert out["greeks_provenance_id"].to_list() == [df["greeks_provenance_id"][19]]

    def test_null_last_constituent_propagates_never_backfilled(self) -> None:
        # Minutes 0-3 solved, minute 4 (the bucket's LAST) honestly NULL.
        df = _frame(5, iv=[0.1, 0.11, 0.12, 0.13, None])
        out = resample_1m_to_tf(df, 5)
        assert out["iv"].to_list() == [None]  # NEVER backfilled from minute 3

    def test_null_mid_bucket_does_not_poison_snapshot(self) -> None:
        df = _frame(5, iv=[None, None, None, None, 0.14])
        out = resample_1m_to_tf(df, 5)
        assert out["iv"].to_list() == [0.14]

    def test_frame_without_overlay_is_unchanged(self) -> None:
        """A spot/futures/legacy frame gains NO overlay columns (additive-only)."""
        df = _frame(5).drop(list(GREEKS_OVERLAY_FIELDS))
        out = resample_1m_to_tf(df, 5)
        assert out.columns == [
            "ts_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_identity_1m_passthrough_keeps_overlay(self) -> None:
        df = _frame(3)
        out = resample_1m_to_tf(df, 1)
        assert out.columns == df.columns
        assert out["iv"].to_list() == df["iv"].to_list()

    def test_on_demand_session_path_inherits_snapshot(self) -> None:
        """``resample_session_to_tf`` (the movers/TB-scale option read) carries it."""
        df = _frame(10).rename({"ts_utc": "timestamp"})
        out = resample_session_to_tf(df, 5).sort("ts_utc")
        assert out["iv"].to_list() == [df["iv"][4], df["iv"][9]]
        # The forming-bucket cutoff guard still applies to overlay-carrying frames:
        # cutoff at 09:22 IST (562) keeps only the fully-closed [09:15, 09:20).
        guarded = resample_session_to_tf(df, 5, cutoff_min=9 * 60 + 22)
        assert guarded["iv"].to_list() == [df["iv"][4]]


class TestCloseSnapshotProperties:
    """The §15.10 test obligation — random 1-minute windows, every TF."""

    @settings(max_examples=40, deadline=None)
    @given(
        n=st.integers(min_value=1, max_value=90),
        tf=st.sampled_from([2, 3, 5, 10, 15, 30, 60]),
        null_at=st.lists(st.integers(min_value=0, max_value=89), max_size=10),
    )
    def test_every_derived_bar_equals_its_last_constituent(
        self, n: int, tf: int, null_at: list[int]
    ) -> None:
        iv: list[float | None] = [0.1 + 0.001 * i for i in range(n)]
        for idx in null_at:
            if idx < n:
                iv[idx] = None  # NA minutes propagate honestly
        df = _frame(n, iv=iv)
        out = resample_1m_to_tf(df, tf).sort("ts_utc")
        # Recompute each bucket's LAST constituent by pure arithmetic.
        for row in out.iter_rows(named=True):
            bucket_start = row["ts_utc"]
            offset = int((bucket_start - _OPEN).total_seconds() // 60)
            members = [i for i in range(n) if offset <= i < offset + tf]
            last = members[-1]
            for name in GREEKS_OVERLAY_FIELDS:
                assert row[name] == df[name][last]
