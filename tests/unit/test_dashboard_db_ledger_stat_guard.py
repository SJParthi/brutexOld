"""Ledger stat guard + the readable-subset latch (DEC-WINDOWS-STAT-GUARD-LATCH-001).

Two C-r2 defect classes on the ``/db`` window-ledger + degraded-pager paths:

* **C-r2-2 stat arm** — ``window_ledger_rows_cached`` gated on a bare
  ``path.is_file()``: pathlib silently swallows EBADF/ELOOP into ``False``
  (FAILS-BEFORE: a lake WITH a ledger rendered a SILENT empty grid — zero
  logs) and RAISES on EIO (FAILS-BEFORE: a route 500); the sibling
  ``_ledger_cache_stamp`` / ``_ledger_scan_source`` stats caught ONLY
  ``FileNotFoundError``. Now every arm rides the OSError-classed
  :func:`_ledger_base_stat` with the one-per-outage-flip
  ``db.ledger_stat_degraded`` WARN latch — a NAMED degrade, never silent,
  never a 500.
* **C-r2-4 latch** — while a corrupt file persists, EVERY paging click took
  the degraded path and re-probed EVERY file (FAILS-BEFORE: O(files) probe
  queries per click); the verdict is now memoised per
  ``(directory, sql)`` + file-set stat token — repeat clicks pay ZERO probe
  queries until a file actually changes.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path, PosixPath
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

# Module import (not ``from … import``): the tests monkeypatch attributes ON
# this module object (``ledger_path``), which db_view resolves at call time.
import brutex.data.window_ledger_store as wls
from brutex.dashboard import db_view
from brutex.dashboard.db_view import query_candles, window_ledger_rows_cached
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger import WindowLedger

_CORRUPT = b"not a parquet file at all \r garbage"


class _StatRaisesPath(PosixPath):
    """A PosixPath whose ``stat`` raises a chosen errno — for the LEDGER FILE only.

    The deltas-dir listing keeps working (the fault under test is the BASE
    stat, the exact ``is_file()`` swallow/raise site).
    """

    _errno = errno.EIO

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        if self.name == "ledger.parquet":
            raise OSError(type(self)._errno, "simulated stat fault", str(self))
        return super().stat(follow_symlinks=follow_symlinks)


class _EbadfPath(_StatRaisesPath):
    _errno = errno.EBADF


class _EloopPath(_StatRaisesPath):
    _errno = errno.ELOOP


class _EioPath(_StatRaisesPath):
    _errno = errno.EIO


class _GlobRaisesPath(PosixPath):
    """A PosixPath whose ``glob`` raises EIO (a wedged deltas listing)."""

    def glob(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "simulated glob fault", str(self))


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    db_view._reset_ledger_read_cache()
    db_view._reset_readable_subset_cache()
    # Reset the per-source outage latches so per-test flip assertions are
    # deterministic (review fold: dict keyed by stat source, not one bool).
    db_view._LEDGER_STAT_DEGRADED.clear()
    yield
    db_view._LEDGER_STAT_DEGRADED.clear()


def _seed_ledger(lake_root: Path) -> None:
    ledger = WindowLedger()
    ledger.record(
        WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
        ),
        raw_bars=5,
        error_type=None,
        bars_appended=5,
        bars_net_new=3,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    wls.save_window_ledger(ledger, lake_root)


class TestLedgerBaseStat:
    def test_healthy_stat(self, tmp_path: Path) -> None:
        f = tmp_path / "ledger.parquet"
        f.write_bytes(b"x")
        mtime, present = db_view._ledger_base_stat(f)
        assert present is True
        assert mtime == f.stat().st_mtime_ns

    def test_missing_is_definitively_absent(self, tmp_path: Path) -> None:
        assert db_view._ledger_base_stat(tmp_path / "nope.parquet") == (0, False)

    def test_notadirectory_is_definitively_absent(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file.txt"
        blocker.write_text("x")
        assert db_view._ledger_base_stat(blocker / "ledger.parquet") == (0, False)

    def test_outage_warns_once_per_flip_then_recovers(self, tmp_path: Path) -> None:
        healthy = tmp_path / "ledger.parquet"
        healthy.write_bytes(b"x")
        with capture_logs() as logs:
            assert db_view._ledger_base_stat(_EbadfPath(healthy)) == (0, None)
            assert db_view._ledger_base_stat(_EloopPath(healthy)) == (0, None)  # latched
            _mtime, present = db_view._ledger_base_stat(healthy)  # recovery
        assert present is True
        warns = [e for e in logs if e["event"] == "db.ledger_stat_degraded"]
        assert len(warns) == 1  # one WARN per outage FLIP, never per poll
        assert [e for e in logs if e["event"] == "db.ledger_stat_recovered"]

    def test_delta_listing_outage_degrades_to_empty(self, tmp_path: Path) -> None:
        ledger = _GlobRaisesPath(tmp_path / "window_ledger" / "ledger.parquet")
        with capture_logs() as logs:
            assert db_view._ledger_delta_files(ledger) == ()
        assert [e for e in logs if e["event"] == "db.ledger_stat_degraded"]

    def test_split_fault_deltas_bad_base_healthy_warns_once_not_per_poll(
        self, tmp_path: Path
    ) -> None:
        # Review fold (per-source latch): a persistently-faulty deltas listing
        # beside a HEALTHY base previously flipped the SHARED latch on every
        # poll — the healthy base stat reset it (INFO), the deltas fault
        # re-armed it (WARN) — a WARN+INFO storm across the whole outage.
        # FAILS-BEFORE: 3 polls → 3 WARNs + recovery INFOs. Now the two
        # sources latch independently: exactly ONE deltas WARN, ZERO recovery
        # INFOs (the base was never degraded), across N polls.
        wdir = tmp_path / "window_ledger"
        wdir.mkdir(parents=True)
        base = wdir / "ledger.parquet"
        base.write_bytes(b"x")  # a real, healthy base — its stat succeeds
        ledger = _GlobRaisesPath(base)  # the deltas-dir glob raises EIO per poll
        with capture_logs() as logs:
            for _ in range(3):
                mtime, names = db_view._ledger_cache_stamp(ledger)
                assert mtime == base.stat().st_mtime_ns  # base stays healthy
                assert names == ()  # deltas degrade to empty per poll
        warns = [e for e in logs if e["event"] == "db.ledger_stat_degraded"]
        infos = [e for e in logs if e["event"] == "db.ledger_stat_recovered"]
        assert len(warns) == 1  # ONE flip for the deltas source — never a storm
        assert warns[0]["source"] == "deltas"
        assert infos == []  # the healthy base never "recovers" the deltas fault

    def test_deltas_recovery_logs_once_and_rearms(self, tmp_path: Path) -> None:
        # The per-source split gives the deltas source its OWN recovery edge
        # (previously it recovered only via the base's shared reset).
        wdir = tmp_path / "window_ledger"
        wdir.mkdir(parents=True)
        base = wdir / "ledger.parquet"
        base.write_bytes(b"x")
        with capture_logs() as logs:
            assert db_view._ledger_cache_stamp(_GlobRaisesPath(base))[1] == ()
            db_view._ledger_cache_stamp(base)  # healthy listing → recovery
            assert db_view._ledger_cache_stamp(_GlobRaisesPath(base))[1] == ()  # re-arms
        warns = [e for e in logs if e["event"] == "db.ledger_stat_degraded"]
        infos = [e for e in logs if e["event"] == "db.ledger_stat_recovered"]
        assert [w["source"] for w in warns] == ["deltas", "deltas"]
        assert [i["source"] for i in infos] == ["deltas"]


class TestWindowLedgerRowsGuard:
    def _patch_ledger_path(self, monkeypatch: pytest.MonkeyPatch, cls: type[PosixPath]) -> None:
        real = wls.ledger_path
        monkeypatch.setattr(wls, "ledger_path", lambda root: cls(real(root)))

    def test_ebadf_is_named_degrade_never_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FAILS-BEFORE: is_file() swallowed EBADF → () with ZERO logs — a lake
        # WITH a ledger rendered a silent empty grid. Now the degrade is NAMED.
        _seed_ledger(tmp_path)
        self._patch_ledger_path(monkeypatch, _EbadfPath)
        with capture_logs() as logs:
            rows = window_ledger_rows_cached(tmp_path)
        assert rows == ()  # the fault also blinds the reader's own stat — empty this poll
        assert [e for e in logs if e["event"] == "db.ledger_stat_degraded"]

    def test_eio_degrades_not_500(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # FAILS-BEFORE: is_file() RAISED EIO → the route 500'd. Now: one named
        # WARN + an empty grid this poll; nothing cached, the next poll retries.
        _seed_ledger(tmp_path)
        self._patch_ledger_path(monkeypatch, _EioPath)
        with capture_logs() as logs:
            rows = window_ledger_rows_cached(tmp_path)  # must not raise
        assert rows == ()
        assert [e for e in logs if e["event"] == "db.ledger_stat_degraded"]
        assert [e for e in logs if e["event"] == "db.window_ledger_read_degraded"]

    def test_recovery_serves_rows_after_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        self._patch_ledger_path(monkeypatch, _EioPath)
        assert window_ledger_rows_cached(tmp_path) == ()
        monkeypatch.undo()
        rows = window_ledger_rows_cached(tmp_path)
        assert len(rows) == 1  # nothing was latched under the outage token

    def test_page_eio_degrades_not_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The window_ledger_rows_page twin (the #1721 x #1386 merge-compose:
        # the /db grid route rides the PAGE function, so the stat-guard +
        # named read degrade must cover it too): EIO → one named WARN + an
        # empty page with total 0 this poll — never a raise/500.
        _seed_ledger(tmp_path)
        self._patch_ledger_path(monkeypatch, _EioPath)
        with capture_logs() as logs:
            total, page = db_view.window_ledger_rows_page(tmp_path, offset=0, limit=10)
        assert (total, page) == (0, ())
        assert [e for e in logs if e["event"] == "db.window_ledger_read_degraded"]
        monkeypatch.undo()
        total, page = db_view.window_ledger_rows_page(tmp_path, offset=0, limit=10)
        assert total == 1 and len(page) == 1  # nothing latched — recovery serves

    def test_absent_ledger_keeps_honest_empty_grid(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert window_ledger_rows_cached(tmp_path) == ()
        assert not [e for e in logs if e["event"].startswith("db.ledger_stat")]

    def test_scan_source_outage_keeps_base_in_fold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stat OUTAGE must NOT exclude an existing base from the delta fold —
        # a delta-only view would be a fabricated partial ledger; the READ
        # decides instead.
        _seed_ledger(tmp_path)
        ledger_file = wls.ledger_path(tmp_path)
        deltas = ledger_file.parent / "deltas"
        deltas.mkdir(parents=True, exist_ok=True)
        (deltas / "0001-delta.parquet").write_bytes(ledger_file.read_bytes())
        source = db_view._ledger_scan_source(_EioPath(ledger_file))
        files = source.params[0]
        assert isinstance(files, list)
        assert ledger_file.as_posix() in files  # base KEPT under the outage
        assert source.stamp_token.startswith("0.")  # outage stamps the 0 token

    def test_scan_source_definite_absence_excludes_base(self, tmp_path: Path) -> None:
        ledger_file = wls.ledger_path(tmp_path)
        deltas = ledger_file.parent / "deltas"
        deltas.mkdir(parents=True, exist_ok=True)
        (deltas / "0001-delta.parquet").write_bytes(b"")
        source = db_view._ledger_scan_source(ledger_file)
        files = source.params[0]
        assert isinstance(files, list)
        assert ledger_file.as_posix() not in files


class _ProbeCountingConn:
    """Delegating DuckDB-connection wrapper counting the per-file READABILITY probe.

    Counts the exact stage-1 probe statement (``SELECT 1 FROM read_parquet(?)
    LIMIT 1``) — one per file per un-memoised degraded pass, both pre- and
    post-fix (the fails-before capture needs a spy that exists pre-seam).
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.probe_count = 0

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql == "SELECT 1 FROM read_parquet(?) LIMIT 1":
            self.probe_count += 1
        return self._conn.execute(sql, params)


class TestReadableSubsetLatch:
    def _mixed_partition(self, lake: Path) -> Path:
        writer = ParquetWriter(lake_root=lake)
        c = Decimal("100.00")
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
                    open=c,
                    high=c,
                    low=c,
                    close=c,
                    volume=10,
                    open_interest=None,
                )
            ],
        )
        base = lake / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
        corrupt = base / "2024" / "07.parquet"
        corrupt.write_bytes(_CORRUPT)
        return base

    def test_repeat_click_zero_reprobes_within_one_fileset(self, tmp_path: Path) -> None:
        # FAILS-BEFORE: every paging click on a corrupt-degraded partition
        # re-probed EVERY file — O(files) probe queries per click.
        self._mixed_partition(tmp_path)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        conn = _ProbeCountingConn(store.connection)

        def _click() -> None:
            out = query_candles(
                conn,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                limit=50,
            )
            assert out["total"] == 1  # the good June bar, corrupt July skipped

        _click()
        first_click = conn.probe_count
        assert first_click > 0  # the first degraded click genuinely probes
        _click()
        assert conn.probe_count == first_click  # ZERO re-probes on the repeat click

    def test_fileset_change_reprobes(self, tmp_path: Path) -> None:
        base = self._mixed_partition(tmp_path)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        conn = _ProbeCountingConn(store.connection)

        def _click() -> int:
            query_candles(
                conn,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                limit=50,
            )
            return conn.probe_count

        first = _click()
        # A NEW corrupt file moves the file-set stat token → full re-probe.
        (base / "2024" / "08.parquet").write_bytes(_CORRUPT)
        second = _click()
        assert second > first

    def test_vanished_file_reads_gone_token(self, tmp_path: Path) -> None:
        # The token helper's per-file stat guard: a file vanishing between the
        # listing and the stat folds into the digest as ":gone", never a raise.
        f = tmp_path / "x.parquet"
        token = db_view._fileset_stat_token([f])
        assert token == db_view._fileset_stat_token([f])  # deterministic
        f.write_bytes(b"data")
        assert db_view._fileset_stat_token([f]) != token

    def test_cache_cap_evicts_fifo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(db_view, "_READABLE_SUBSET_CACHE_CAP", 1)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            (d / "junk.parquet").write_bytes(_CORRUPT)
            rows = db_view._execute_parquet_resilient(
                store.connection,
                d,
                "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)",
                [],
                source="unit",
            )
            assert rows == []
        assert len(db_view._READABLE_SUBSET_CACHE) == 1  # FIFO-evicted to the cap

    def test_all_unreadable_memo_hit_stays_loud(self, tmp_path: Path) -> None:
        # The empty-verdict memo hit re-logs the unreadable ERROR per degraded
        # request — an empty serve is never quiet.
        d = tmp_path / "dead"
        d.mkdir()
        (d / "junk.parquet").write_bytes(_CORRUPT)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        sql = "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)"
        db_view._execute_parquet_resilient(store.connection, d, sql, [], source="unit")
        with capture_logs() as logs:
            rows = db_view._execute_parquet_resilient(store.connection, d, sql, [], source="unit")
        assert rows == []
        assert [e for e in logs if e["event"] == "db.parquet_unreadable"]
        assert not [e for e in logs if e["event"] == "db.parquet_skip"]  # memo hit: no re-probe
