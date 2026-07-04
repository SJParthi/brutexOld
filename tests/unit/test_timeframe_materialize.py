"""Unit tests for ``brutex.data.timeframe_materialize`` (DEC-TF-MATERIALIZE-001).

100% line+branch over the pure materialise core: a fixture 1m polars DataFrame
flows through the REAL :func:`~brutex.data.spot_resample.derive_intraday_tfs`,
with a fake ``write_tf`` capturing every write. Asserts the requested TFs are
written with the right bar counts, an unknown / non-storable TF is skipped, a
duplicate TF is de-duped (written once), the empty request is a clean no-op, and
the all-storable default round-trips. No lake, no DuckDB, no FastAPI — the
injected I/O makes the whole control flow offline-testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframe_materialize import (
    INTRADAY_TF_INTERVALS,
    MATERIALIZABLE_TFS,
    MaterializeResult,
    materialize_timeframes,
)


def _df_1m(n: int = 90) -> pl.DataFrame:
    """One session's worth of 1-minute bars anchored at 09:15 IST (03:45 UTC)."""
    base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
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


class _Recorder:
    """A fake ``write_tf`` that records each (tf, frame) and returns its height."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, pl.DataFrame]] = []

    def __call__(self, tf: str, frame: pl.DataFrame) -> int:
        self.writes.append((tf, frame))
        return frame.height


def test_writes_requested_tfs_with_correct_bar_counts() -> None:
    df = _df_1m()
    derived = derive_intraday_tfs(df)
    rec = _Recorder()

    result = materialize_timeframes(
        tfs=("5m", "15m", "60m"),
        read_1m=lambda: df,
        write_tf=rec,
    )

    assert isinstance(result, MaterializeResult)
    assert list(result.written) == ["5m", "15m", "60m"]
    # Each written count equals the REAL resampled frame height (the oracle).
    for tf in ("5m", "15m", "60m"):
        assert result.written[tf] == derived[tf].height
    assert result.skipped == ()
    assert result.total_bars == sum(derived[tf].height for tf in ("5m", "15m", "60m"))
    # The fake saw exactly those three frames, in order.
    assert [tf for tf, _ in rec.writes] == ["5m", "15m", "60m"]


def test_unknown_tf_is_skipped_not_written() -> None:
    df = _df_1m()
    rec = _Recorder()

    # Since DEC-TF-VOCAB-21-001 every derived TF is storable, so the skip
    # branch is exercised with "99m" — a label the derivation never produces.
    result = materialize_timeframes(
        tfs=("5m", "99m"),
        read_1m=lambda: df,
        write_tf=rec,
    )

    assert list(result.written) == ["5m"]
    assert result.skipped == ("99m",)
    assert [tf for tf, _ in rec.writes] == ["5m"]


def test_duplicate_tf_is_deduped_written_once() -> None:
    df = _df_1m()
    rec = _Recorder()

    result = materialize_timeframes(
        tfs=("5m", "5m", "15m", "5m"),
        read_1m=lambda: df,
        write_tf=rec,
    )

    assert list(result.written) == ["5m", "15m"]
    assert [tf for tf, _ in rec.writes] == ["5m", "15m"]
    assert result.skipped == ()


def test_empty_request_is_a_clean_noop() -> None:
    df = _df_1m()
    rec = _Recorder()
    called = {"n": 0}

    def _read() -> pl.DataFrame:
        called["n"] += 1
        return df

    result = materialize_timeframes(tfs=(), read_1m=_read, write_tf=rec)

    assert result.written == {}
    assert result.skipped == ()
    assert result.total_bars == 0
    assert rec.writes == []
    # read_1m is still called exactly once (derivation is unconditional, cheap),
    # but nothing is written.
    assert called["n"] == 1


def test_all_storable_default_round_trips() -> None:
    df = _df_1m()
    derived = derive_intraday_tfs(df)
    rec = _Recorder()

    result = materialize_timeframes(
        tfs=MATERIALIZABLE_TFS,
        read_1m=lambda: df,
        write_tf=rec,
    )

    # Every materialisable TF is a real derived TF, so all are written, none skipped.
    assert list(result.written) == list(MATERIALIZABLE_TFS)
    assert result.skipped == ()
    for tf in MATERIALIZABLE_TFS:
        assert result.written[tf] == derived[tf].height
    assert result.total_bars == sum(derived[tf].height for tf in MATERIALIZABLE_TFS)


def test_materializable_tfs_all_map_to_valid_partition_intervals() -> None:
    # Guards the canonical map against drift from the partition allowlist — every
    # storable TF must address a real VALID_INTERVALS partition.
    from brutex.data.partition import VALID_INTERVALS

    assert tuple(INTRADAY_TF_INTERVALS) == MATERIALIZABLE_TFS
    for interval in INTRADAY_TF_INTERVALS.values():
        assert interval in VALID_INTERVALS


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ({}, 0),
        ({"5m": 18}, 18),
        ({"5m": 18, "15m": 6}, 24),
    ],
)
def test_total_bars_property_sums_written(written: dict[str, int], expected: int) -> None:
    assert MaterializeResult(written=written, skipped=()).total_bars == expected


class TestStorableLadderComplete:
    """DEC-TF-VOCAB-21-001: all 19 derived intraday TFs are storable."""

    def test_19_storable_timeframes(self) -> None:
        assert len(INTRADAY_TF_INTERVALS) == 19
        assert set(MATERIALIZABLE_TFS) == set(INTRADAY_TF_INTERVALS)

    def test_excludes_base_and_daily(self) -> None:
        # 1m is the fetched base; 1d is fetched natively — neither is derived.
        assert "1m" not in INTRADAY_TF_INTERVALS
        assert "1d" not in INTRADAY_TF_INTERVALS

    def test_every_value_is_a_valid_lake_interval(self) -> None:
        from brutex.data.partition import VALID_INTERVALS

        for interval in INTRADAY_TF_INTERVALS.values():
            assert interval in VALID_INTERVALS

    def test_matches_canonical_timeframes_map(self) -> None:
        from brutex.data.timeframes import (
            ALL_TF_MINUTES,
            DAILY_TF_MINUTES,
            LAKE_INTERVAL_LABELS,
            TF_LABELS,
        )

        expected = {
            TF_LABELS[m]: LAKE_INTERVAL_LABELS[m]
            for m in ALL_TF_MINUTES
            if m not in (1, DAILY_TF_MINUTES)
        }
        assert expected == INTRADAY_TF_INTERVALS


class TestMonthPartitionFiles:
    """DEC-TF-REBUILD-MONTH-BOUND-001 — the pure explicit-month file-list core."""

    def test_returns_only_existing_months_sorted(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import month_partition_files

        for year, month in [(2021, 2), (2020, 12)]:
            f = tmp_path / f"{year:04d}" / f"{month:02d}.parquet"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"stub")
        files = month_partition_files(
            tmp_path,
            [(2021, 2), (2020, 12), (2023, 5)],  # 2023-05 absent on disk
        )
        assert files == [
            (tmp_path / "2020" / "12.parquet").as_posix(),
            (tmp_path / "2021" / "02.parquet").as_posix(),
        ]

    def test_duplicates_are_read_once_and_empty_is_empty(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import month_partition_files

        f = tmp_path / "2021" / "01.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"stub")
        assert month_partition_files(tmp_path, [(2021, 1), (2021, 1)]) == [f.as_posix()]
        assert month_partition_files(tmp_path, []) == []


def test_months_with_include_daily_raises(tmp_path: Path) -> None:
    """``months=`` + ``include_daily=True`` is REFUSED: the midnight-IST derived
    1day straddles UTC months (an IST date-D bar lives in the month of D−1), so
    a month-bounded daily derive would silently mis-derive — the daily recall
    path's ±1-day-padded window read is the correct tool (named follow-up)."""
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    with pytest.raises(ValueError, match="include_daily"):
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            include_daily=True,
            months=[(2021, 1)],
        )
