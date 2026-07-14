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

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.data.models import CandleBar
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframe_materialize import (
    INTRADAY_TF_INTERVALS,
    MATERIALIZABLE_TFS,
    MaterializeResult,
    frame_to_candle_bars,
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


# ────────────────────────────────────────────────────────────────────────
# The zero-stored-bars probe + the typed whole-partition refusal
# (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001)
# ────────────────────────────────────────────────────────────────────────


class TestHasStored1mBars:
    """T1 — the ONE lake-truth existence predicate + the base-path law."""

    def test_spot_1m_partition_dir_path_law(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import spot_1m_partition_dir

        assert spot_1m_partition_dir(tmp_path, "NSE", "CASH", "RELIANCE") == (
            tmp_path / "bars" / "NSE" / "CASH" / "RELIANCE" / "1minute"
        )

    def test_absent_dir_is_false(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import has_stored_1m_bars

        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is False

    def test_dir_with_zero_parquet_is_false(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "RELIANCE")
        (base / "2024").mkdir(parents=True)
        (base / "2024" / "notes.txt").write_bytes(b"not a parquet")
        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is False

    def test_nested_year_month_parquet_is_true(self, tmp_path: Path) -> None:
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "RELIANCE")
        f = base / "2024" / "01.parquet"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"stub")
        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is True

    def test_directory_named_parquet_is_true(self, tmp_path: Path) -> None:
        """``rglob("*.parquet")``-parity pin (refuter round 4): the original
        spelling matched a dataset-style DIRECTORY plant (``…/01.parquet/``)
        too, so the walk-based probe must keep that verdict — a semantics
        drift here would silently reclassify the #1558-round-8 shape."""
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "RELIANCE")
        (base / "2024" / "01.parquet").mkdir(parents=True)  # a DIR named *.parquet
        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is True

    def test_file_at_partition_path_is_false(self, tmp_path: Path) -> None:
        """A FILE planted at the ``1minute`` path (out-of-band: nothing can be
        stored under a file) reads ``False`` — the pre-fix branch verdict for
        the non-directory shape, preserved (refuter round 4)."""
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "RELIANCE")
        base.parent.mkdir(parents=True)
        base.write_bytes(b"not a directory")
        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is False

    def test_file_component_mid_path_is_false(self, tmp_path: Path) -> None:
        """A FILE at the symbol dir makes ``os.stat(base)`` raise
        ``NotADirectoryError`` — folded into the benign ``False`` arm
        (refuter round 4)."""
        from brutex.data.timeframe_materialize import has_stored_1m_bars

        symbol_dir = tmp_path / "bars" / "NSE" / "CASH" / "RELIANCE"
        symbol_dir.parent.mkdir(parents=True)
        symbol_dir.write_bytes(b"a file where the symbol dir should be")
        assert has_stored_1m_bars(tmp_path, "NSE", "CASH", "RELIANCE") is False

    def test_unenumerable_partition_raises_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[fails-before, refuter round 4] a permission-broken POPULATED
        partition must raise LOUDLY, never read as *empty*: CPython pathlib
        SUPPRESSES ``PermissionError`` during glob traversal, so the original
        ``is_dir() and any(rglob(...))`` spelling returned ``False`` here —
        reclassifying a real fault as the benign ``no_source``
        ("vendor-empty or never pulled": both causes false) with ``ok=True``
        and the movers warm firing, where the merge-base classified the
        identical lake state a loud warm-withholding FAILURE. The propagated
        ``PermissionError`` is an ``OSError`` — a ``parquet_read_skip_errors``
        member — restoring the per-symbol-FAILURE classification."""
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "PERM")
        (base / "2024").mkdir(parents=True)
        (base / "2024" / "01.parquet").write_bytes(b"stub")  # data provably EXISTS

        monkeypatch.setattr(os, "scandir", _deny_scandir_under(base))
        with pytest.raises(PermissionError):
            has_stored_1m_bars(tmp_path, "NSE", "CASH", "PERM")

    def test_unenumerable_subtree_raises_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[refuter round 4] a fault DEEPER than the base dir (an unreadable
        year subdir hiding the only parquet) is loud too — the walk descends
        with the same OSError-propagating ``onerror`` at every level."""
        from brutex.data.timeframe_materialize import (
            has_stored_1m_bars,
            spot_1m_partition_dir,
        )

        base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "PERM")
        sub = base / "2024"
        sub.mkdir(parents=True)
        (sub / "01.parquet").write_bytes(b"stub")

        monkeypatch.setattr(os, "scandir", _deny_scandir_under(sub))
        with pytest.raises(PermissionError):
            has_stored_1m_bars(tmp_path, "NSE", "CASH", "PERM")


def _deny_scandir_under(denied: Path) -> Any:
    """An ``os.scandir`` stand-in raising EACCES for paths at/under *denied*.

    The uid-independent, CI-safe twin of a real ``chmod 000`` on the dir
    (refuter round 4 — the executed real-permissions A/B ran under a dropped
    uid 65534; CI runs unprivileged, so the probe is simulated here).
    """
    real_scandir = os.scandir

    def _scandir(path: Any = ".") -> Any:
        p = os.fspath(path)
        if isinstance(p, str) and p.startswith(str(denied)):
            raise PermissionError(13, "Permission denied", p)
        return real_scandir(path)

    return _scandir


def test_unreadable_populated_partition_propagates_oserror_never_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[fails-before, refuter round 4] the READER classification boundary: an
    unreadable-but-POPULATED partition propagates ``PermissionError`` out of
    :func:`materialize_lake_timeframes` — NEVER the benign
    :class:`NoStored1mBarsError` — so the prebuild's spot phase lands it in
    ``failed`` (WARNING, ``ok=False``, movers warm withheld: the merge-base
    fail-safe classification; the honest counterpart of the pre-fix
    "vendor-empty or never pulled" wording, both of whose causes were false
    for this shape)."""
    from brutex.data.timeframe_materialize import (
        PrebuildKind,
        materialize_lake_timeframes,
        spot_1m_partition_dir,
    )

    base = spot_1m_partition_dir(tmp_path, "NSE", "CASH", "PERM")
    (base / "2024").mkdir(parents=True)
    (base / "2024" / "01.parquet").write_bytes(b"stub")

    monkeypatch.setattr(os, "scandir", _deny_scandir_under(base))
    with pytest.raises(PermissionError):
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="PERM",
            kind=PrebuildKind.SPOT,
            include_daily=True,
        )


def test_empty_partition_raises_typed_no_stored_1m_bars_error(tmp_path: Path) -> None:
    """T2 [fails-before] — the whole-partition zero-stored-bars refusal is the
    typed :class:`NoStored1mBarsError` (a ``ValueError`` subclass, so every
    existing catcher is behavior-identical) with the two-cause message bytes
    UNCHANGED (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001)."""
    from brutex.data.timeframe_materialize import (
        NoStored1mBarsError,
        PrebuildKind,
        materialize_lake_timeframes,
    )

    with pytest.raises(NoStored1mBarsError) as exc:
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="RELIANCE",
            kind=PrebuildKind.SPOT,
            include_daily=True,
        )
    assert isinstance(exc.value, ValueError)  # back-compat pin — subclass, never a sibling
    assert str(exc.value) == (
        "no stored 1minute bars for NSE/CASH/RELIANCE — "
        "nothing to derive: if the 1-minute spot was never pulled, "
        "pull it first, then derive timeframes; if the vendor cleanly "
        "returned no data for this symbol (windows settled EMPTY), "
        "there is nothing to derive — no action needed"
    )


def test_months_bounded_absence_stays_plain_value_error(tmp_path: Path) -> None:
    """T3 — the months-bounded arm (the heal-recall path) keeps plain
    ``ValueError`` and is NEVER the typed no-source class: a months-scoped
    absence says nothing about the whole partition (kills the
    retype-both-arms mutant; DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001)."""
    from brutex.data.timeframe_materialize import (
        NoStored1mBarsError,
        PrebuildKind,
        materialize_lake_timeframes,
    )

    with pytest.raises(ValueError, match="in the requested months") as exc:
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="RELIANCE",
            kind=PrebuildKind.SPOT,
            months=[(2030, 1)],
        )
    assert not isinstance(exc.value, NoStored1mBarsError)


# ────────────────────────────────────────────────────────────────────────
# frame_to_candle_bars — the batched frame → CandleBar conversion
# (DEC-PREBUILD-GIL-VECTORIZE-001). BYTE-IDENTITY oracle: the verbatim
# legacy per-row loop this replaced.
# ────────────────────────────────────────────────────────────────────────


def _legacy_frame_to_bars(frame: pl.DataFrame) -> list[CandleBar]:
    """The VERBATIM pre-DEC-PREBUILD-GIL-VECTORIZE-001 per-row conversion.

    Copied byte-for-byte (modulo the closure boilerplate) from the former
    ``make_lake_write_tf._write`` body — the equivalence oracle the batched
    :func:`frame_to_candle_bars` must match bar-for-bar.
    """
    has_oi = "open_interest" in frame.columns

    def _oi(row: dict[str, object]) -> int:
        if not has_oi:
            return 0
        value = row["open_interest"]
        return 0 if value is None else int(value)  # type: ignore[call-overload]

    return [
        CandleBar(
            timestamp=row["ts_utc"],
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["volume"]),
            open_interest=_oi(row),
        )
        for row in frame.iter_rows(named=True)
    ]


class TestFrameToCandleBars:
    """Batched conversion == the legacy per-row loop, bar-for-bar."""

    def test_spot_frame_matches_legacy_per_row_loop(self) -> None:
        frame = _df_1m(90)
        assert frame_to_candle_bars(frame) == _legacy_frame_to_bars(frame)

    def test_oi_frame_with_nulls_matches_legacy_per_row_loop(self) -> None:
        base = datetime(2021, 6, 28, 3, 45, tzinfo=UTC)
        frame = pl.DataFrame(
            {
                "ts_utc": [base + timedelta(minutes=i) for i in range(12)],
                "open": [15720.4 + i * 0.05 for i in range(12)],
                "high": [15721.75 + i * 0.05 for i in range(12)],
                "low": [15718.25 + i * 0.05 for i in range(12)],
                "close": [15721.0 + i * 0.05 for i in range(12)],
                "volume": [100 + i for i in range(12)],
                "open_interest": [None if i % 3 == 0 else i * 7 for i in range(12)],
            }
        )
        bars = frame_to_candle_bars(frame)
        assert bars == _legacy_frame_to_bars(frame)
        # Present-but-null OI maps to the absent value 0 (the legacy `_oi` contract).
        assert [b.open_interest for b in bars[:4]] == [0, 7, 14, 0]

    def test_empty_frame_returns_empty_list(self) -> None:
        assert frame_to_candle_bars(_df_1m(0)) == []

    def test_absent_oi_column_defaults_to_zero(self) -> None:
        bars = frame_to_candle_bars(_df_1m(3))
        assert [b.open_interest for b in bars] == [0, 0, 0]

    def test_absent_open_interest_none_for_daily_bars(self) -> None:
        bars = frame_to_candle_bars(_df_1m(3), absent_open_interest=None)
        assert [b.open_interest for b in bars] == [None, None, None]

    def test_repeated_prices_share_memoised_decimals(self) -> None:
        base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
        frame = pl.DataFrame(
            {
                "ts_utc": [base, base + timedelta(minutes=1)],
                "open": [100.05, 100.05],
                "high": [100.05, 100.05],
                "low": [100.05, 100.05],
                "close": [100.05, 100.05],
                "volume": [1, 2],
            }
        )
        bars = frame_to_candle_bars(frame)
        assert bars == _legacy_frame_to_bars(frame)
        # The memo hands the SAME immutable Decimal back for a repeated string
        # (value-safe: Decimal is immutable; equality is what the lake keys on).
        assert bars[0].open is bars[1].open

    def test_hostile_float_reprs_match_legacy_str_parse(self) -> None:
        """polars ``cast(String)`` → ``Decimal`` == ``Decimal(str(x))`` numerically.

        Seeded-random + adversarial long-tail floats (the DEC-LAKE-WRITE-VECTORIZE-001
        boundary class: 11-17-significant-digit shortest reprs near the 2dp half
        grid, vendor 4dp noise, tiny + large magnitudes) — the whole bar list must
        equal the legacy loop's, which pins ``Decimal`` NUMERIC equality per value.
        """
        import random

        rng = random.Random(20260704)
        hostile = [
            0.00499999999,
            14020.7999,
            0.005,
            100.005,
            26000.115,
            0.01,
            123456.789012345,
            4e14 - 0.005,
        ]
        values = hostile + [rng.uniform(0.01, 1e6) for _ in range(2000)]
        base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
        frame = pl.DataFrame(
            {
                "ts_utc": [base + timedelta(minutes=i) for i in range(len(values))],
                "open": values,
                "high": [v + 1.0 for v in values],
                "low": values,
                "close": values,
                "volume": [1] * len(values),
            }
        )
        assert frame_to_candle_bars(frame) == _legacy_frame_to_bars(frame)


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


# ── _last_ist_datetime (DEC-DAILY-CUTOFF-GROUNDED-001: one extraction, two
#    consumers — the grounded cutoff takes the full instant, the stamp its date)


def test_last_ist_datetime_returns_full_ist_instant() -> None:
    pl = pytest.importorskip("polars")
    from datetime import UTC, datetime

    from brutex.calendars.nse import IST
    from brutex.data.timeframe_materialize import _last_ist_date, _last_ist_datetime

    # 09:59 UTC == 15:29 IST — the full instant survives (a date-only
    # extraction could never ground the close edge).
    frame = pl.DataFrame(
        {
            "ts_utc": [
                datetime(2021, 1, 4, 3, 45, tzinfo=UTC),
                datetime(2021, 1, 4, 9, 59, tzinfo=UTC),
            ]
        }
    )
    last = _last_ist_datetime(frame)
    assert last == datetime(2021, 1, 4, 15, 29, tzinfo=IST)
    # The date wrapper delegates — one extraction, one convention.
    assert _last_ist_date(frame) == "2021-01-04"


def test_last_ist_datetime_empty_and_non_datetime_are_none() -> None:
    pl = pytest.importorskip("polars")
    from brutex.data.timeframe_materialize import _last_ist_datetime

    empty = pl.DataFrame({"ts_utc": pl.Series([], dtype=pl.Datetime(time_zone="UTC"))})
    assert _last_ist_datetime(empty) is None
    assert _last_ist_datetime(pl.DataFrame({"ts_utc": [1, 2]})) is None


def test_last_ist_datetime_naive_timestamp_treated_as_utc() -> None:
    pl = pytest.importorskip("polars")
    from datetime import datetime

    from brutex.calendars.nse import IST
    from brutex.data.timeframe_materialize import _last_ist_datetime

    frame = pl.DataFrame({"ts_utc": [datetime(2021, 1, 4, 3, 45)]})
    assert _last_ist_datetime(frame) == datetime(2021, 1, 4, 9, 15, tzinfo=IST)


# ── daily_cutoff_grounding_enabled (the BRUTEX_DAILY_CUTOFF_GROUNDED gate) ───


class TestDailyCutoffGroundingEnabled:
    """Default-ON env gate — the exact BRUTEX_PREBUILD_INCREMENTAL idiom over
    the SHARED derive_stamp.DISABLED_ENV_VALUES off-value set."""

    def test_default_on(self) -> None:
        from brutex.data.timeframe_materialize import daily_cutoff_grounding_enabled

        assert daily_cutoff_grounding_enabled(env={}) is True

    @pytest.mark.parametrize("value", ["0", "off", "OFF", "False", "no", " 0 "])
    def test_off_values_disable(self, value: str) -> None:
        from brutex.data.timeframe_materialize import (
            DAILY_CUTOFF_GROUNDED_ENV,
            daily_cutoff_grounding_enabled,
        )

        assert daily_cutoff_grounding_enabled(env={DAILY_CUTOFF_GROUNDED_ENV: value}) is False

    @pytest.mark.parametrize("value", ["1", "on", "yes", "banana", ""])
    def test_other_values_stay_on(self, value: str) -> None:
        from brutex.data.timeframe_materialize import (
            DAILY_CUTOFF_GROUNDED_ENV,
            daily_cutoff_grounding_enabled,
        )

        assert daily_cutoff_grounding_enabled(env={DAILY_CUTOFF_GROUNDED_ENV: value}) is True

    def test_reads_process_env_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data.timeframe_materialize import (
            DAILY_CUTOFF_GROUNDED_ENV,
            daily_cutoff_grounding_enabled,
        )

        monkeypatch.setenv(DAILY_CUTOFF_GROUNDED_ENV, "off")
        assert daily_cutoff_grounding_enabled() is False
        monkeypatch.delenv(DAILY_CUTOFF_GROUNDED_ENV)
        assert daily_cutoff_grounding_enabled() is True

    def test_shares_the_common_off_value_set(self) -> None:
        # CLAUDE.md §11 COMMON: one off-value spelling for both escape hatches.
        from brutex.data.derive_stamp import DISABLED_ENV_VALUES

        assert frozenset({"0", "off", "false", "no"}) == DISABLED_ENV_VALUES


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
