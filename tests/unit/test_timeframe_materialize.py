"""Unit tests for ``brutex.data.timeframe_materialize`` (DEC-TF-MATERIALIZE-001).

100% line+branch over the pure materialise core: a fixture 1m polars DataFrame
flows through the REAL :func:`~brutex.data.spot_resample.derive_intraday_tfs`,
with a fake ``write_tf`` capturing every write. Asserts the requested TFs are
written with the right bar counts, an unknown / non-storable TF is skipped, a
duplicate TF is de-duped (written once), the empty request is a clean no-op, and
the all-storable default round-trips. No lake, no DuckDB, no FastAPI — the
injected I/O makes the whole control flow offline-testable.
"""

# The _last_ist_date whitebox tests read a module-private helper (the repo's
# whitebox convention — mirrors test_lake_heal.py).
# pyright: reportPrivateUsage=none
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


# ── _last_ist_date (DEC-PREBUILD-INCREMENTAL-001 stamp input) ────────────────


def test_last_ist_date_empty_frame_is_none() -> None:
    pl = pytest.importorskip("polars")
    from brutex.data.timeframe_materialize import _last_ist_date

    empty = pl.DataFrame({"ts_utc": pl.Series([], dtype=pl.Datetime(time_zone="UTC"))})
    assert _last_ist_date(empty) is None


def test_last_ist_date_non_datetime_column_is_none() -> None:
    pl = pytest.importorskip("polars")
    from brutex.data.timeframe_materialize import _last_ist_date

    weird = pl.DataFrame({"ts_utc": [1, 2, 3]})
    assert _last_ist_date(weird) is None


def test_last_ist_date_converts_utc_to_ist_calendar_date() -> None:
    pl = pytest.importorskip("polars")
    from datetime import UTC, datetime

    from brutex.data.timeframe_materialize import _last_ist_date

    # 2021-01-04 19:30 UTC == 2021-01-05 01:00 IST → the IST date rolls over.
    frame = pl.DataFrame(
        {
            "ts_utc": [
                datetime(2021, 1, 4, 3, 45, tzinfo=UTC),
                datetime(2021, 1, 4, 19, 30, tzinfo=UTC),
            ]
        }
    )
    assert _last_ist_date(frame) == "2021-01-05"


def test_last_ist_date_naive_timestamp_treated_as_utc() -> None:
    pl = pytest.importorskip("polars")
    from datetime import datetime

    from brutex.data.timeframe_materialize import _last_ist_date

    frame = pl.DataFrame({"ts_utc": [datetime(2021, 1, 4, 3, 45)]})
    assert _last_ist_date(frame) == "2021-01-04"


# ── _derived_dirs_present (the incremental skip's existence guard) ───────────


def test_derived_dirs_present_ignores_non_storable_labels(tmp_path: Path) -> None:
    from brutex.data.timeframe_materialize import _derived_dirs_present

    (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute").mkdir(parents=True)
    # "bogus" has no storable interval — a full run reports it `skipped`, so it
    # needs no partition dir to exist; "5m" maps to the present 5minute dir.
    assert _derived_dirs_present(tmp_path, "NSE", "INDEX", "NIFTY", ("5m", "bogus"), False) is True


def test_derived_dirs_present_false_on_missing_tf_dir(tmp_path: Path) -> None:
    from brutex.data.timeframe_materialize import _derived_dirs_present

    (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute").mkdir(parents=True)
    assert _derived_dirs_present(tmp_path, "NSE", "INDEX", "NIFTY", ("5m", "15m"), False) is False


def test_derived_dirs_present_requires_1day_dir_when_daily(tmp_path: Path) -> None:
    from brutex.data.timeframe_materialize import _derived_dirs_present

    base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
    (base / "5minute").mkdir(parents=True)
    assert _derived_dirs_present(tmp_path, "NSE", "INDEX", "NIFTY", ("5m",), True) is False
    (base / "1day").mkdir()
    assert _derived_dirs_present(tmp_path, "NSE", "INDEX", "NIFTY", ("5m",), True) is True
