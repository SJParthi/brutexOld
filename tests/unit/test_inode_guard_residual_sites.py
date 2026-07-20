# pyright: reportPrivateUsage=none
"""The verifier-identified four-site residual (DEC-LAKE-READ-INODE-GUARD-002).

The #1832 assurance verification named FOUR sites still FIFO-hangable OUTSIDE
the #1832 wired set: the THREE lazy ``pl.scan_parquet`` reads (the spot
daily-resample window reader, the spot-join window reader, the
timeframe-materialize 1m reader) — where the file OPEN is deferred to
``.collect()``, deep in C-level ``fifo_open`` where no Python guard can
fire — and the rate_bridge byte-read (a bare ``path.read_bytes()`` whose
CALLER runs on the deadline daemon: the route stayed bounded, but the wedged
daemon never returned and the cache's single-flight never cleared). Each site
now rides the guarded seam (``scan_parquet_regular`` — the EAGER per-file fd
probe at scan-BUILD time — / ``read_regular_bytes``), raising the typed
``NotRegularFileError`` (an ``OSError``) into the site's EXISTING degrade arm
in bounded time.

FIFO reachability per site (honest disclosure, Golden Rule #10):

- GENUINE pre-fix HANGS (fails-before proven via bounded ``timeout``
  subprocess runs at the pre-fix tree — the evidence in MIGRATION.md
  #1833): the three LIST-shaped scan sites (spot daily-resample /
  spot-join / the months-bounded timeframe arm) plus the rate_bridge
  byte-read. The LIST sites enumerate through an ``is_file()`` gate that
  excludes a statically planted FIFO, so their reachable hang class is the
  enumeration→collect inode SWAP window; those tests force that class
  DETERMINISTICALLY by monkeypatching the enumeration seam to return the
  FIFO path (the exact ``list[str]`` polars opened pre-fix — each hung in
  C-level fifo_open, subprocess timeout-killed exit 143). The rate_bridge
  byte-read is STATICALLY plantable and hung the pre-fix read directly
  (in-test watchdog fired at the pre-fix tree).
- the timeframe-materialize whole-tree GLOB arm is STATICALLY plantable
  but NOT statically hangable: probed on polars 1.42.1, ``pl.scan_parquet``
  GLOB expansion SILENTLY IGNORES a non-regular inode — the pre-fix
  collect served the healthy sibling's rows with the planted FIFO
  silently skipped (no hang, no error). The guard there is a CORRECTNESS
  improvement (a planted FIFO now raises the typed ``NotRegularFileError``
  instead of being silently dropped from the read set — the
  correct-or-absent posture) plus a hang belt should polars' glob
  semantics ever change; the fails-before shape for that test is
  "DID NOT RAISE" at the pre-fix tree, never a hang claim.

Every hang-class case runs under the house SIGALRM watchdog.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.data.guarded_read import NotRegularFileError
from brutex.data.rate_bridge import rate_bridge_path, read_rate_snapshot
from brutex.data.spot_daily_resample import make_lake_read_1m_window
from brutex.data.spot_join import spot_close_by_ts_window, utc_instant
from brutex.data.timeframe_materialize import make_lake_read_1m


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """Fail LOUDLY if the guarded site blocks — the fails-before shape is a hang."""

    def _boom(_signum: int, _frame: object) -> None:
        raise AssertionError(f"site read blocked > {seconds}s — the inode-guard hang is back")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


_TS_A = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)  # 09:30 IST on 2024-01-02
_TS_B = datetime(2024, 1, 2, 4, 1, tzinfo=UTC)  # 09:31 IST on 2024-01-02


def _write_1m_partition(lake_root: Path, *, segment: str = "INDEX", symbol: str = "NIFTY") -> Path:
    """One stored-shape 1minute month partition (timestamp Datetime-UTC + OHLCV)."""
    part_dir = lake_root / "bars" / "NSE" / segment / symbol / "1minute" / "2024"
    part_dir.mkdir(parents=True)
    frame = pl.DataFrame(
        {
            "timestamp": [_TS_A, _TS_B],
            "open": [100.0, 100.5],
            "high": [101.0, 101.5],
            "low": [99.0, 100.0],
            "close": [100.5, 101.0],
            "volume": [10, 20],
        },
        schema_overrides={"timestamp": pl.Datetime(time_unit="us", time_zone="UTC")},
    )
    dest = part_dir / "01.parquet"
    frame.write_parquet(dest)
    return dest


class TestSpotDailyResampleWindowReader:
    """Residual site 1 — ``make_lake_read_1m_window``'s lazy scan."""

    def test_regular_files_frame_parity(self, tmp_path: Path) -> None:
        """Regular-file byte-identity: the guarded lazy scan yields the SAME
        frame the plain schema-tolerant ``pl.scan_parquet`` pipeline did."""
        _write_1m_partition(tmp_path)
        reader = make_lake_read_1m_window(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
        )
        got = reader()
        assert got.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        assert got.get_column("ts_utc").to_list() == [_TS_A, _TS_B]
        assert got.get_column("close").to_list() == [100.5, 101.0]

    def test_fifo_month_file_raises_eagerly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The swap-class shape forced deterministically (the ``is_file()``
        enumeration gate excludes a statically planted FIFO — disclosed):
        the enumeration seam returns the FIFO path, exactly the frame the
        pre-fix ``pl.scan_parquet(files).collect()`` hung on in C-level
        fifo_open; the eager probe now raises typed at scan-build."""
        fifo = tmp_path / "01.parquet"
        os.mkfifo(fifo)
        monkeypatch.setattr(
            "brutex.data.spot_daily_resample.ist_span_partition_files",
            lambda _base, _start, _end: [fifo.as_posix()],
        )
        reader = make_lake_read_1m_window(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
        )
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            reader()


class TestSpotJoinWindowReader:
    """Residual site 2 — ``spot_close_by_ts_window``'s lazy scan."""

    def test_regular_files_join_parity(self, tmp_path: Path) -> None:
        _write_1m_partition(tmp_path)
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
        )
        assert join.segment == "INDEX"
        assert join.close_by_ts == {
            utc_instant(_TS_A): Decimal("100.5"),
            utc_instant(_TS_B): Decimal("101.0"),
        }

    def test_fifo_month_file_raises_eagerly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The swap-class shape forced deterministically via the enumeration
        seam (spot_join's module-level ``ist_span_partition_files`` import)."""
        fifo = tmp_path / "01.parquet"
        os.mkfifo(fifo)
        monkeypatch.setattr(
            "brutex.data.spot_join.ist_span_partition_files",
            lambda _base, _start, _end: [fifo.as_posix()],
        )
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            spot_close_by_ts_window(
                tmp_path,
                exchange="NSE",
                underlying="NIFTY",
                interval="1minute",
                start=date(2024, 1, 1),
                end=date(2024, 2, 1),
            )


class TestTimeframeMaterializeReader:
    """Residual site 3 — ``make_lake_read_1m``'s lazy scan (BOTH source shapes)."""

    def test_regular_tree_frame_parity(self, tmp_path: Path) -> None:
        _write_1m_partition(tmp_path)
        reader = make_lake_read_1m(
            lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY"
        )
        got = reader()
        assert got.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        assert got.get_column("ts_utc").to_list() == [_TS_A, _TS_B]

    def test_glob_arm_planted_fifo_raises_eagerly(self, tmp_path: Path) -> None:
        """The statically plantable GLOB-arm CORRECTNESS pin (honest scope —
        see the module docstring): the whole-tree ``**/*.parquet`` arm has
        NO ``is_file()`` enumeration gate, but polars' own glob expansion
        SILENTLY IGNORED the planted FIFO pre-fix (probed: the collect
        served the sibling's rows, no hang, no error — a silent read-set
        drop). The eager probe now raises typed at scan-build, so a planted
        non-regular inode is LOUD, never silently skipped; fails-before =
        "DID NOT RAISE" at the pre-fix tree."""
        dest = _write_1m_partition(tmp_path)
        os.mkfifo(dest.parent / "02.parquet")
        reader = make_lake_read_1m(
            lake_root=tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY"
        )
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            reader()

    def test_months_arm_fifo_raises_eagerly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The months-bounded LIST arm's swap-class shape, forced via the
        ``month_partition_files`` enumeration seam (its ``is_file()`` gate
        excludes a statically planted FIFO — disclosed)."""
        dest = _write_1m_partition(tmp_path)
        fifo = dest.parent / "02.parquet"
        os.mkfifo(fifo)
        monkeypatch.setattr(
            "brutex.data.timeframe_materialize.month_partition_files",
            lambda _base, _months: [fifo.as_posix()],
        )
        reader = make_lake_read_1m(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            months={(2024, 1)},
        )
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            reader()


class TestRateBridgeSite:
    """Residual site 4 — the rate-bridge byte-read (statically plantable).

    Pre-fix the bare ``path.read_bytes()`` blocked FOREVER on a planted FIFO:
    the CALLER (``RateBridgeCache._refresh_body``) runs on the deadline
    daemon so the ROUTE stayed bounded, but the wedged daemon never returned
    and the cache's single-flight ``_inflight`` never cleared — the bridge
    was permanently stuck on last-known until restart. The fd gate makes the
    FIFO raise ``NotRegularFileError`` into the existing ``except OSError``
    arm instantly: honest ``None``, no zombie, the single-flight self-heals.
    """

    def test_fifo_bridge_reads_none_bounded(self, tmp_path: Path) -> None:
        path = rate_bridge_path(tmp_path)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        with _hang_watchdog():
            assert read_rate_snapshot(tmp_path) is None

    def test_absent_bridge_still_reads_none(self, tmp_path: Path) -> None:
        assert read_rate_snapshot(tmp_path) is None
