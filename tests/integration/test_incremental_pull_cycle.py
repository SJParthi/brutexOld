"""Day-over-day INCREMENTAL pull cycle, end-to-end offline (verification harness).

Preserved from an independent verification session: the repo had end-to-end
single-pull coverage (``test_synthetic_end_to_end.py``) and direct-write
prebuild-stamp coverage (``test_prebuild_incremental.py``), but NO scenario
driving the PRODUCTION day-over-day incremental cycle — pull day N, then pull
day N+1 into the SAME lake with the persisted ledger — through the real spine.

The mechanism under test (and what makes it incremental): incrementality is
the :class:`~brutex.data.window_ledger.WindowLedger` SUCCESS/SKIPPED skip
(the "SOLE skip authority"), NOT a narrowed fetch range. Windows are 30-day
chunks ANCHORED at ``start`` (``plan_windows`` + the ingest tiler), so with a
FIXED floor start the COMPLETED 30-day chunks keep identical ``(start, end)``
keys → skipped, and only the trailing partial chunk (whose end grows daily)
is re-fetched; the keep-first ``ParquetWriter`` merge absorbs the trailing
overlap → no duplicate bars, no lost bars.

Everything is OFFLINE: the :class:`SyntheticMarketAdapter` (deterministic,
seeded, real-NSE-calendar sessions) drives the REAL vendor-neutral ingest
spine (``run_groww_spot_ingest``) with no creds / no network, writing a REAL
Parquet lake through the REAL ``ParquetBarSink`` + the REAL durable
``WindowLedger`` persistence (``plan_windows`` → throttled live flush →
``save_window_ledger``), exactly like a production pull.

Scenarios (each genuinely missing from the existing suites):

* completed-chunk skip + trailing-chunk-only re-fetch on the +1-day increment,
  with zero duplicate timestamps, zero lost bars, exactly the new day added,
  and ledger convergence (superseded trailing ghost row pruned);
* re-pull of an identical range is FULLY skipped (zero fetches) and
  byte-identically idempotent (lake + ledger);
* THE SHARP EDGE: a 1-minute partition deleted out-of-band while its ledger
  row stays SUCCESS is NOT restored by a later increment (the skip runs on
  LEDGER authority, not lake presence) — and the sanctioned
  ``record(degraded=True)`` reopen + re-pull IS the recovery (documented in
  ``docs/runbooks/lake-backup-restore.md`` §4);
* derived-TF freshness: after the +1-day increment the prebuild re-derives
  (stamp invalidated by the REAL spine's trailing-chunk rewrite) and the
  incremental derive chain is byte-equal to a full from-scratch re-derive —
  the through-the-real-pull twin of ``test_prebuild_incremental.py``'s
  direct-write source-change test;
* read consistency: the increment moves ``lake_revision`` and bumps the
  ``SummaryCache`` generation, so dashboard caches recompute.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.run_ingest import GrowwIngestReport, run_groww_spot_ingest
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.adapters.synthetic import SyntheticMarketAdapter, build_synthetic_index
from brutex.dashboard.lake_revision import lake_revision
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    load_window_ledger,
    make_live_ledger_flush,
    make_locked_flush,
    plan_windows,
    save_window_ledger,
)
from brutex.models import Exchange, Instrument

#: A fixed production-shaped floor so the FIRST 30-day chunk is a stable,
#: completed key across pulls (the real pull anchors at GROWW_HISTORY_FLOOR).
_FLOOR = date(2020, 12, 1)
#: The floor-anchored 30-day chunk — identical (start, end) on every pull.
_COMPLETED_WIN = (date(2020, 12, 1), date(2020, 12, 31))
#: Day-N pull end (exclusive) → last session pulled is 2021-01-04.
_DAY_N_END = date(2021, 1, 5)
#: Day-N+1 pull end (exclusive) → adds exactly the 2021-01-05 session.
_DAY_N1_END = date(2021, 1, 6)
_MARKET = GROWW_COVERAGE_MARKET
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


@dataclass
class _FetchSpy:
    """A full :class:`MarketAdapter` over the synthetic adapter, recording fetches.

    Delegates every protocol method to the wrapped deterministic adapter and
    records each ``fetch_ohlcv`` call's ``(symbol, interval, start, end)`` so
    the tests can assert WHICH windows the spine actually re-fetched.
    """

    inner: SyntheticMarketAdapter = field(default_factory=SyntheticMarketAdapter)
    calls: list[tuple[str, str, date, date]] = field(default_factory=list)

    @property
    def capabilities(self) -> MarketCapabilities:
        return self.inner.capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        return self.inner.list_instruments(cursor=cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        return self.inner.list_derivatives(underlying_symbol=underlying_symbol, exchange=exchange)

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        self.calls.append((symbol, interval, start, end))
        return self.inner.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )

    def reset(self) -> None:
        self.calls.clear()

    def windows(self) -> set[tuple[date, date]]:
        """The distinct 1-minute fetch windows issued since the last reset."""
        return {(s, e) for (_sym, iv, s, e) in self.calls if iv == _INTERVAL}


def _index_instrument(symbol: str) -> Instrument:
    return next(i for i in build_synthetic_index().universe if i.symbol_name == symbol)


def _pull(
    lake_root: Path, adapter: _FetchSpy, *, end: date, ledger: WindowLedger
) -> GrowwIngestReport:
    """One production-shaped spot pull over ``[_FLOOR, end)``, persisting the ledger.

    Mirrors the production daily cycle: ``plan_windows`` seeds PLANNED rows AND
    prunes superseded trailing ghosts (with Groww's 30-day tiling, so planned
    keys == fetched keys), the spine fires the throttled locked live flush per
    window, and the merged ledger is persisted at pull end.
    """
    instrument = _index_instrument(_SYMBOL)
    plan_windows(
        ledger,
        instruments=[instrument],
        intervals=[_INTERVAL],
        start=_FLOOR,
        end=end,
        market=_MARKET,
        window_days_for=window_days_for_interval,
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    on_window = make_locked_flush(make_live_ledger_flush(ledger, lake_root))
    report = run_groww_spot_ingest(
        adapter,
        sink,
        instruments=[instrument],
        intervals=[_INTERVAL],
        start=_FLOOR,
        end=end,
        ledger=ledger,
        on_window=on_window,
    )
    save_window_ledger(ledger, lake_root)
    return report


def _read_frame(lake_root: Path, interval: str = _INTERVAL) -> pl.DataFrame:
    """Every stored partition of NIFTY's *interval* as one frame (empty if none)."""
    parts = sorted((lake_root / "bars" / "NSE" / "INDEX" / _SYMBOL / interval).rglob("*.parquet"))
    if not parts:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(p) for p in parts])


def _ist_dates(lake_root: Path, interval: str = _INTERVAL) -> set[str]:
    """The distinct IST calendar dates present in the stored *interval* bars."""
    frame = _read_frame(lake_root, interval)
    if frame.is_empty():
        return set()
    ist = frame.get_column("timestamp").dt.convert_time_zone("Asia/Kolkata")
    return {d.isoformat() for d in ist.dt.date().to_list()}


def _count_1m(lake_root: Path) -> int:
    return _read_frame(lake_root).height


def _unique_ts_1m(lake_root: Path) -> int:
    frame = _read_frame(lake_root)
    return 0 if frame.is_empty() else frame.get_column("timestamp").n_unique()


def _ledger_rows(lake_root: Path) -> set[tuple[str, str, str]]:
    """``(window_start, window_end, status)`` for NIFTY's 1-minute ledger rows."""
    ledger = load_window_ledger(lake_root)
    return {
        (r.window_start, r.window_end, r.status)
        for r in ledger.records()
        if r.security_id == _SYMBOL and r.interval == _INTERVAL
    }


def _skippable(lake_root: Path, win: tuple[date, date]) -> bool:
    key = WindowKey(
        market=_MARKET, security_id=_SYMBOL, interval=_INTERVAL, start=win[0], end=win[1]
    )
    return load_window_ledger(lake_root).is_skippable(key)


def _bars_tree(lake_root: Path) -> dict[str, bytes]:
    """Every ``bars/`` Parquet file's bytes, keyed by relative posix path."""
    root = lake_root / "bars"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


def _reopen_completed_chunk(lake_root: Path) -> None:
    """The SANCTIONED SUCCESS→re-pullable reopen lever: ``record(degraded=True)``.

    Registers the key in ``degraded_keys`` so the save-time merge does NOT
    resurrect the on-disk SUCCESS (a naive ``record(error_type=...)`` FAILED is
    silently reverted by ``merge_ledger_rows``' more-settled-wins rule — the
    SUCCESS row is deliberately sticky).
    """
    ledger = load_window_ledger(lake_root)
    ledger.record(
        WindowKey(
            market=_MARKET,
            security_id=_SYMBOL,
            interval=_INTERVAL,
            start=_COMPLETED_WIN[0],
            end=_COMPLETED_WIN[1],
        ),
        raw_bars=1,  # degraded demotes only a would-be SUCCESS classification
        error_type=None,
        bars_appended=0,
        now=datetime.now(UTC),
        degraded=True,
    )
    save_window_ledger(ledger, lake_root)


def test_day_over_day_completed_chunk_skipped_and_only_trailing_refetched(
    tmp_path: Path,
) -> None:
    """The +1-day increment: completed chunk skipped, trailing chunk re-fetched,
    no duplicate bars, no lost bars, exactly the new day added, ledger converged."""
    lake = tmp_path / "lake"
    spy = _FetchSpy()

    # Day N: floor → 2021-01-05. Two windows fetched (completed + trailing).
    report_n = _pull(lake, spy, end=_DAY_N_END, ledger=WindowLedger())
    assert report_n.failed_pairs == 0
    assert spy.windows() == {_COMPLETED_WIN, (date(2020, 12, 31), _DAY_N_END)}
    assert _skippable(lake, _COMPLETED_WIN) is True
    day_n_count = _count_1m(lake)
    day_n_dates = _ist_dates(lake)

    # Day N+1 increment: SAME lake, ledger reloaded like a fresh process would.
    spy.reset()
    report_n1 = _pull(lake, spy, end=_DAY_N1_END, ledger=load_window_ledger(lake))
    assert report_n1.failed_pairs == 0

    # (a) the COMPLETED 30-day chunk was NOT re-fetched — only the grown
    # trailing chunk was (the real incrementality shape).
    fetched = spy.windows()
    assert _COMPLETED_WIN not in fetched, f"completed chunk was re-fetched: {fetched}"
    assert fetched == {(date(2020, 12, 31), _DAY_N1_END)}

    # (b) zero duplicate 1-minute timestamps despite the trailing overlap
    # re-fetch (keep-first merge), (c) no day-N bars lost, exactly the new day.
    total = _count_1m(lake)
    assert _unique_ts_1m(lake) == total, "duplicate timestamps in the lake"
    assert total > day_n_count, "the increment added no bars"
    day_n1_dates = _ist_dates(lake)
    assert day_n_dates <= day_n1_dates, "a day-N date vanished"
    assert day_n1_dates - day_n_dates == {"2021-01-05"}

    # (d) ledger convergence: both live windows SUCCESS, the superseded
    # trailing ghost (…→01-05) pruned by plan_windows' trailing GC.
    rows = _ledger_rows(lake)
    assert {status for (_s, _e, status) in rows} == {"SUCCESS"}
    assert ("2020-12-01", "2020-12-31", "SUCCESS") in rows
    assert ("2020-12-31", "2021-01-06", "SUCCESS") in rows
    assert ("2020-12-31", "2021-01-05", "SUCCESS") not in rows, "stale trailing ghost row"


def test_repull_same_range_is_fully_skipped_and_idempotent(tmp_path: Path) -> None:
    """Re-pulling an identical settled range fetches NOTHING and changes NOTHING."""
    lake = tmp_path / "lake"
    spy = _FetchSpy()
    _pull(lake, spy, end=_DAY_N_END, ledger=WindowLedger())
    tree_before = _bars_tree(lake)
    rows_before = _ledger_rows(lake)

    spy.reset()
    report = _pull(lake, spy, end=_DAY_N_END, ledger=load_window_ledger(lake))

    assert spy.windows() == set(), f"settled windows were re-fetched: {spy.calls}"
    assert report.bars_appended == 0
    assert _bars_tree(lake) == tree_before, "lake bytes changed on an idempotent re-pull"
    assert _ledger_rows(lake) == rows_before


def test_deleted_partition_with_success_ledger_is_not_repulled(tmp_path: Path) -> None:
    """THE SHARP EDGE: the ledger is the SOLE skip authority, not lake presence.

    A 1-minute partition deleted out-of-band while its ledger row stays SUCCESS
    is NOT restored by a later increment — the window is skipped on LEDGER
    authority. Recovery is the sanctioned ``record(degraded=True)`` reopen +
    re-pull (proven below); see ``docs/runbooks/lake-backup-restore.md`` §4.
    """
    lake = tmp_path / "lake"
    spy = _FetchSpy()
    _pull(lake, spy, end=_DAY_N_END, ledger=WindowLedger())

    december = lake / "bars" / "NSE" / "INDEX" / _SYMBOL / _INTERVAL / "2020" / "12.parquet"
    assert december.is_file()
    dates_before = _ist_dates(lake)
    december.unlink()
    lost = dates_before - _ist_dates(lake)
    assert lost, "expected the deleted partition's dates to vanish"
    # Dates strictly inside the COMPLETED (skipped) chunk — Dec 1..30. The
    # 12-31 session belongs to the still-growing trailing window and IS
    # restored by the increment's trailing re-fetch.
    completed_only = {d for d in lost if d < "2020-12-31"}
    assert completed_only

    # Increment: the completed window is STILL SUCCESS → skipped → NOT restored.
    spy.reset()
    assert _skippable(lake, _COMPLETED_WIN) is True
    _pull(lake, spy, end=_DAY_N1_END, ledger=load_window_ledger(lake))
    assert _COMPLETED_WIN not in spy.windows()
    dates_now = _ist_dates(lake)
    assert completed_only.isdisjoint(dates_now), "completed-chunk data unexpectedly restored"
    assert "2020-12-31" in dates_now  # the trailing re-fetch restored ITS day

    # RECOVERY: the sanctioned degraded reopen → the next pull re-fetches the
    # chunk and restores every lost date (idempotent keep-first sink).
    _reopen_completed_chunk(lake)
    assert _skippable(lake, _COMPLETED_WIN) is False
    spy.reset()
    _pull(lake, spy, end=_DAY_N1_END, ledger=load_window_ledger(lake))
    assert _COMPLETED_WIN in spy.windows(), "reopened window was not re-fetched"
    assert lost <= _ist_dates(lake), "reopen-then-repull did not restore the lost dates"


def _derive(lake_root: Path, *, incremental: bool | None = None) -> MaterializeResult:
    return materialize_lake_timeframes(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m", "15m"),
        include_daily=True,
        daily_before=date(2021, 2, 1),  # every pulled session is complete
        incremental=incremental,
    )


def _derived_tree(lake_root: Path) -> dict[str, bytes]:
    """Every DERIVED (non-1minute) partition's bytes, keyed by relative path."""
    return {
        rel: data for rel, data in _bars_tree(lake_root).items() if rel.split("/")[3] != _INTERVAL
    }


def test_derived_tfs_fresh_after_increment_and_equal_full_rederive(tmp_path: Path) -> None:
    """After a REAL +1-day increment the prebuild re-derives, picks up the new
    day, and lands byte-identical to a full from-scratch re-derive.

    The through-the-real-pull twin of ``test_prebuild_incremental.py``'s
    direct-write source-change test: here the 1-minute change arrives via the
    spine's trailing-chunk re-fetch + keep-first merge (a partition REWRITE,
    not a fresh append), and the byte-equality pin is INCREMENTAL-after-
    increment vs FULL re-derive over the same two-pull lake.
    """
    # lake_incr: pull day N → derive → increment → derive (the incremental path).
    lake_incr = tmp_path / "incr"
    spy_incr = _FetchSpy()
    _pull(lake_incr, spy_incr, end=_DAY_N_END, ledger=WindowLedger())
    first = _derive(lake_incr)
    assert first.unchanged is False
    assert first.written["5m"] > 0
    assert "2021-01-05" not in _ist_dates(lake_incr, "5minute")

    # No 1-minute change → the stamped re-run SKIPS.
    assert _derive(lake_incr).unchanged is True

    # Increment through the REAL spine, then re-derive.
    _pull(lake_incr, spy_incr, end=_DAY_N1_END, ledger=load_window_ledger(lake_incr))
    rerun = _derive(lake_incr)
    assert rerun.unchanged is False, "the increment did not invalidate the derive stamp"
    dates_5m = _ist_dates(lake_incr, "5minute")
    assert "2021-01-05" in dates_5m, "derived 5m did not pick up the new day"
    assert "2021-01-05" in _ist_dates(lake_incr, "1day"), "derived 1day missed the new session"

    # lake_full: the same two pulls, then ONE forced from-scratch derive.
    lake_full = tmp_path / "full"
    spy_full = _FetchSpy()
    _pull(lake_full, spy_full, end=_DAY_N_END, ledger=WindowLedger())
    _pull(lake_full, spy_full, end=_DAY_N1_END, ledger=load_window_ledger(lake_full))
    assert _derive(lake_full, incremental=False).unchanged is False

    # THE PIN: the incremental derive chain equals a full re-derive, byte-for-byte.
    assert _derived_tree(lake_incr) == _derived_tree(lake_full), (
        "incremental derive diverged from a full re-derive"
    )


def test_increment_moves_lake_revision_and_bumps_summary_cache_generation(
    tmp_path: Path,
) -> None:
    """Read consistency: the +1-day increment moves the O(1) revision token and
    bumps the real ``SummaryCache`` generation (dashboard caches recompute)."""
    lake = tmp_path / "lake"
    spy = _FetchSpy()
    _pull(lake, spy, end=_DAY_N_END, ledger=WindowLedger())

    token_n = lake_revision(lake)
    assert token_n is not None
    cache = SummaryCache(revision_probe=partial(lake_revision, lake))
    generation_n = cache.generation  # records the baseline token (no bump)
    assert cache.generation == generation_n  # stable while the lake is unchanged

    _pull(lake, spy, end=_DAY_N1_END, ledger=load_window_ledger(lake))

    token_n1 = lake_revision(lake)
    assert token_n1 is not None
    assert token_n1 != token_n, "lake_revision token did not move after the increment"
    assert cache.generation > generation_n, "SummaryCache generation did not bump"


def test_lake_read_helpers_report_an_empty_lake_honestly(tmp_path: Path) -> None:
    """The module's lake-read helpers degrade honestly on a bar-less lake
    (exercises their empty branches — no fabricated counts, ever)."""
    assert _read_frame(tmp_path).is_empty()
    assert _ist_dates(tmp_path) == set()
    assert _count_1m(tmp_path) == 0
    assert _unique_ts_1m(tmp_path) == 0
