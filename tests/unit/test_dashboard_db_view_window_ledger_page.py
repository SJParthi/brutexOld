"""Tests — the /db window-ledger DuckDB page fast path (DEC-DB-LATENCY-HARDENING-001).

MIGRATION.md #1719: ``/api/v1/db/window-ledger`` previously served a 100-row
page by paying ONE full ``load_window_ledger`` Python parse per ledger stamp
move — measured 7,177.5 ms of a 7,628 ms request at 400k rows (the profiling
evidence, 2026-07-11), recurring on every mid-pull flush.
:func:`brutex.dashboard.db_view.query_window_ledger_page` now serves the page
via a DuckDB ``LIMIT``/``OFFSET`` projection over the SAME delta-folded
:func:`_ledger_scan_source` the ``/coverage/windows`` fast paths scan, with the
total from one stamp-memoised ``COUNT(*)``.

Pins:

* **fast-equals-legacy** (the #893 pattern): every page of the fast path is
  cell-for-cell equal to the legacy ``window_ledger_rows_cached`` slice over a
  REAL writer ledger including pending deltas + a demotion;
* the degrade law — legacy-shape base / NULL-in-derivable-cell / persistent
  ``duckdb.Error`` / ``conn=None`` each serve the byte-identical legacy parse;
* the torn-read retry-once arm; the memo families (repeat click = O(1) hit,
  ZERO further SQL); the column-constant drift pin vs the store schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _LEDGER_ROW_COLUMNS,
    _ledger_page_row,
    query_window_ledger_page,
    window_ledger_rows_cached,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from structlog.testing import capture_logs

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_ISO = "2026-07-01T12:00:00+00:00"


def _key(
    symbol: str,
    *,
    market: str = "groww",
    start_day: int = 1,
    interval: str = "1minute",
    end_day: int = 31,
) -> WindowKey:
    return WindowKey(
        market=market,
        security_id=symbol,
        interval=interval,
        start=date(2025, 1, start_day),
        end=date(2025, 1, end_day),
    )


def _seed_mixed_ledger(lake: Path) -> None:
    """A REAL writer ledger: mixed statuses, then deltas incl. a DEMOTION.

    Base: SUCCESS / FAILED / EMPTY rows across two markets (spot + options).
    Deltas (``append_ledger_delta``): one NEW key + one FAILED demotion of a
    base SUCCESS key — the newer-file-wins fold both paths must agree on.
    """
    ledger = WindowLedger()
    for i in range(8):
        market = "groww" if i % 2 == 0 else "groww-option"
        ledger.record(
            # Vary interval / window_start / window_end across rows (hostile-
            # review fold): the DuckDB-vs-Python order over the 3 TEMPORAL key
            # columns is genuinely exercised, not vacuously equal on constants.
            # i=0 keeps the defaults so the SYM00 demotion delta's key matches.
            _key(
                f"SYM{i:02d}",
                market=market,
                start_day=1 + (i % 3) * 9,
                interval="1day" if i % 2 else "1minute",
                end_day=31 - (i % 4),
            ),
            raw_bars=5 if i % 3 else 0,
            error_type="timeout" if i % 4 == 3 else None,
            bars_appended=5 if i % 3 else 0,
            bars_net_new=3 if i % 3 else None,
            now=_NOW,
        )
    # A SECOND window under an EXISTING (market, security_id, interval) triple —
    # rows differing ONLY in window_start — so the within-triple temporal
    # ordering is pinned directly (never just cross-symbol).
    ledger.record(
        _key("SYM01", market="groww-option", start_day=2, interval="1day", end_day=29),
        raw_bars=5,
        error_type=None,
        bars_appended=5,
        now=_NOW,
    )
    wls.save_window_ledger(ledger, lake)
    # Delta 1: a brand-new key (mid-pull flush).
    live = wls.load_window_ledger(lake)
    live.record(_key("SYMNEW"), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
    assert wls.append_ledger_delta(live, lake) == 1
    # Delta 2: DEMOTE a base SUCCESS key to FAILED (newer-file-wins must keep it).
    live.record(_key("SYM00"), raw_bars=0, error_type="timeout", bars_appended=0, now=_NOW)
    assert wls.append_ledger_delta(live, lake) >= 1


def _fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the shared legacy read cache so parity runs are hermetic."""
    monkeypatch.setattr(
        db_view,
        "_LEDGER_READ_CACHE",
        db_view._make_ledger_read_cache(),  # pyright: ignore[reportPrivateUsage]
    )


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


class _SpyConn:
    """A pass-through DuckDB connection recording every (sql, params) pair."""

    def __init__(self) -> None:
        self._real = duckdb.connect()
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        return self._real.execute(sql, params)


class _FlakyConn(_SpyConn):
    """Raises ``duckdb.Error`` for the first *n_failures* executes, then delegates."""

    def __init__(self, n_failures: int) -> None:
        super().__init__()
        self._remaining = n_failures

    def execute(self, sql: str, params: Any = None) -> Any:
        if self._remaining > 0:
            self._remaining -= 1
            raise duckdb.Error("simulated torn read")
        return super().execute(sql, params)


class _NoneCountConn(_SpyConn):
    """Serves a ``fetchone() -> None`` cursor for the COUNT statement only.

    Covers the defensive ``if row else 0`` arm of the total query — a real
    DuckDB aggregate always yields one row, but the connection is an injected
    seam (the route wires a store proxy), so the arm is kept + pinned.
    """

    class _NoneCursor:
        def fetchone(self) -> None:
            return None

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql.startswith("SELECT COUNT(*)"):
            return self._NoneCursor()
        return super().execute(sql, params)


# ── drift pin ─────────────────────────────────────────────────────────────────


def test_ledger_row_columns_pinned_to_the_store_schema() -> None:
    """The 17-column projection MUST equal the store's ``_COLUMNS`` exactly
    (the ``_WINDOW_KEY_COLUMNS`` precedent — pinned, never imported)."""
    assert wls._COLUMNS == _LEDGER_ROW_COLUMNS


def test_ledger_table_path_constants_pinned_to_the_store() -> None:
    """The fast path derives ``<lake>/window_ledger/ledger.parquet`` from
    pinned module-local constants (NO ``window_ledger_store`` import — even a
    function-local one participates in the CodeQL ``py/cyclic-import`` cycle);
    this pin keeps them from ever drifting off the store's own names."""
    assert db_view._LEDGER_TABLE_DIRNAME == wls.LEDGER_DIRNAME
    assert db_view._LEDGER_TABLE_FILENAME == wls.LEDGER_FILENAME


# ── fast-equals-legacy (the #893 pattern) ────────────────────────────────────


class TestFastEqualsLegacy:
    def test_every_page_byte_equal_to_legacy_slice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EXHAUSTIVE parity: every (limit, offset) page of the DuckDB fast path
        equals the legacy cached-parse slice cell-for-cell, over a real writer
        ledger WITH pending deltas + a FAILED demotion (newer-file-wins)."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        legacy = window_ledger_rows_cached(tmp_path)
        assert len(legacy) == 10  # 8 base keys + 1 second-window key + 1 delta-new
        # The within-triple temporal pin really exists (two SYM01 rows differing
        # only in window_start) and the 5-column sort keeps them adjacent + ordered.
        sym01 = [r for r in legacy if r[1] == "SYM01"]
        assert len(sym01) == 2 and sym01[0][3] < sym01[1][3]
        # The demotion survived the fold on the legacy side (sanity).
        demoted = [r for r in legacy if r[1] == "SYM00"]
        assert demoted and demoted[0][5] == "FAILED"
        with duckdb.connect() as conn:
            for limit in (1, 2, 4, 500):
                for offset in (0, 1, 3, 9, 10, 50):
                    page, total = query_window_ledger_page(
                        conn, tmp_path, limit=limit, offset=offset
                    )
                    assert total == len(legacy)
                    assert page == legacy[offset : offset + limit]

    def test_no_ledger_at_all_is_empty(self, tmp_path: Path) -> None:
        """No base + no deltas → ``((), 0)`` with ZERO queries (never a raise)."""
        with duckdb.connect() as conn:
            assert query_window_ledger_page(conn, tmp_path, limit=100, offset=0) == ((), 0)

    def test_base_less_delta_only_lake_serves_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first pull's DELTA-ONLY checkpoint (no ``ledger.parquet``) serves
        its live rows — the schema probe is skipped (every delta carries the
        full writer schema) and the fold orders the deltas among themselves."""
        _fresh_caches(monkeypatch)
        live = WindowLedger()
        live.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert wls.append_ledger_delta(live, tmp_path) == 1
        legacy = window_ledger_rows_cached(tmp_path)
        with duckdb.connect() as conn:
            page, total = query_window_ledger_page(conn, tmp_path, limit=10, offset=0)
        assert total == 1 and page == legacy


# ── degrade law ──────────────────────────────────────────────────────────────


class TestDegradeLaw:
    def test_conn_none_serves_the_legacy_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No DuckDB connection wired → the byte-identical legacy path outright."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        legacy = window_ledger_rows_cached(tmp_path)
        page, total = query_window_ledger_page(None, tmp_path, limit=3, offset=2)
        assert total == len(legacy) and page == legacy[2:5]

    def test_legacy_shape_base_defers_wholesale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-R1 base missing optional columns (here: the retry-split pair)
        defers the WHOLE request to the legacy parse, whose per-row derivation
        rules the SQL path deliberately does not duplicate — output identical."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        _fresh_caches(monkeypatch)
        path = wls.ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # An 11-column CORE-only file (legacy shape: every optional column absent).
        table = pa.table(
            {
                "market": ["groww"],
                "security_id": ["NIFTY"],
                "interval": ["1minute"],
                "window_start": ["2025-01-01"],
                "window_end": ["2025-01-31"],
                "status": ["SKIPPED"],
                "bars": pa.array([0], type=pa.int64()),
                "attempts": pa.array([4], type=pa.int64()),
                "last_error": pa.array([None], type=pa.string()),
                "first_seen_at": [_ISO],
                "last_attempt_at": [_ISO],
            }
        )
        pq.write_table(table, path)  # type: ignore[no-untyped-call, unused-ignore]
        legacy = window_ledger_rows_cached(tmp_path)
        # The legacy parse DERIVES empty_streak (SKIPPED → threshold) — the exact
        # rule the fast path must not half-reproduce.
        assert legacy[0][11] > 0
        with duckdb.connect() as conn, capture_logs() as logs:
            page, total = query_window_ledger_page(conn, tmp_path, limit=10, offset=0)
        assert total == 1 and page == legacy
        (event,) = _events(logs, "window_ledger_page.fallback")
        assert event["fallback"] == "legacy_shape"

    def test_null_derivable_cell_defers_wholesale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NULL ``empty_streak`` cell (all 17 columns PRESENT) trips the row
        projection's ValueError → the whole request defers to the legacy parse,
        which derives the cell from ``(status, attempts)`` — output identical."""
        import pyarrow.parquet as pq

        _fresh_caches(monkeypatch)
        # Widened (Any) — this deliberately-junk row carries a NULL in an
        # int-typed LedgerRowTuple slot, which is the very shape under test.
        row: tuple[Any, ...] = (
            "groww",
            "NIFTY",
            "1minute",
            "2025-01-01",
            "2025-01-31",
            "EMPTY",
            0,
            2,
            None,
            _ISO,
            _ISO,
            None,  # empty_streak NULL — the legacy parse derives it
            1,
            None,
            None,
            None,
            None,  # streak_provenance
            0,  # failed_streak — the 18th column (DEC-AUTO-RESUME-ZERO-SURPRISES-001)
        )
        path = wls.ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(wls._rows_to_table([row]), path)  # type: ignore[no-untyped-call, unused-ignore]
        legacy = window_ledger_rows_cached(tmp_path)
        assert legacy[0][11] == 2  # derived: EMPTY → attempts
        with duckdb.connect() as conn, capture_logs() as logs:
            page, total = query_window_ledger_page(conn, tmp_path, limit=10, offset=0)
        assert total == 1 and page == legacy
        (event,) = _events(logs, "window_ledger_page.fallback")
        assert event["fallback"] == "legacy_shape"

    def test_torn_read_retries_once_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ONE transient ``duckdb.Error`` (the atomic-replace race) retries once
        after the bounded delay and serves the fast path normally."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        legacy = window_ledger_rows_cached(tmp_path)
        conn = _FlakyConn(1)
        sleeps: list[float] = []
        with capture_logs() as logs:
            page, total = query_window_ledger_page(
                conn, tmp_path, limit=4, offset=0, retry_sleep=sleeps.append
            )
        assert total == len(legacy) and page == legacy[0:4]
        assert sleeps == [db_view._DUCKDB_RETRY_DELAY_S]  # pyright: ignore[reportPrivateUsage]
        assert len(_events(logs, "window_ledger_page.duckdb_retry")) == 1
        assert not _events(logs, "window_ledger_page.duckdb_fallback")

    def test_persistent_duckdb_error_degrades_to_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistently malformed DELTA (the multi-file SQL read cannot skip
        it file-granularly) fails both attempts → the legacy parse serves the
        page, SKIPPING the bad delta exactly as ``load_window_ledger`` does.
        Uses the REAL default sleeper (``retry_sleep=None`` — one bounded
        100 ms pause), covering that arm too."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        (wls.deltas_dir(tmp_path) / "99999999999999999999-1-1.parquet").write_bytes(
            b"not a parquet file"
        )
        legacy = window_ledger_rows_cached(tmp_path)
        assert len(legacy) == 10  # the bad delta is skipped, rows intact
        with duckdb.connect() as conn, capture_logs() as logs:
            page, total = query_window_ledger_page(conn, tmp_path, limit=500, offset=0)
        assert total == len(legacy) and page == legacy
        assert len(_events(logs, "window_ledger_page.duckdb_retry")) == 1
        (fallback,) = _events(logs, "window_ledger_page.duckdb_fallback")
        assert fallback["fallback"] == "rows"


# ── memo families + row projection ───────────────────────────────────────────


class TestMemoFamilies:
    def test_repeat_click_is_an_o1_cache_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the caches injected, a repeat click within one ledger stamp runs
        ZERO further SQL (no COUNT, no page SELECT, no schema probe) — the
        pre-fix route re-paid a full O(rows) Python parse per stamp move."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        conn = _SpyConn()
        count_cache = MoversSnapshotCache()
        page_cache = MoversSnapshotCache(capacity=8)
        first = query_window_ledger_page(
            conn, tmp_path, limit=4, offset=0, count_cache=count_cache, page_cache=page_cache
        )
        cold_calls = len(conn.calls)
        assert cold_calls >= 2  # probe + COUNT + page
        second = query_window_ledger_page(
            conn, tmp_path, limit=4, offset=0, count_cache=count_cache, page_cache=page_cache
        )
        assert second == first
        assert len(conn.calls) == cold_calls  # ZERO further SQL — pure cache handoff
        # A DIFFERENT page coordinate re-uses the memoised total (no second COUNT).
        query_window_ledger_page(
            conn, tmp_path, limit=4, offset=4, count_cache=count_cache, page_cache=page_cache
        )
        new_sql = [sql for sql, _ in conn.calls[cold_calls:]]
        assert not any(sql.startswith("SELECT COUNT(*)") for sql in new_sql)

    def test_ledger_flush_moves_the_stamp_and_recomputes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mid-pull delta append moves the composite stamp → the next click
        recomputes (fresh COUNT + page) and serves the NEW row (never stale)."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        conn = _SpyConn()
        count_cache = MoversSnapshotCache()
        page_cache = MoversSnapshotCache(capacity=8)
        _page, total = query_window_ledger_page(
            conn, tmp_path, limit=500, offset=0, count_cache=count_cache, page_cache=page_cache
        )
        assert total == 10
        live = wls.load_window_ledger(tmp_path)
        live.record(_key("ZZLATE"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        assert wls.append_ledger_delta(live, tmp_path) == 1
        page2, total2 = query_window_ledger_page(
            conn, tmp_path, limit=500, offset=0, count_cache=count_cache, page_cache=page_cache
        )
        assert total2 == 11
        assert any(r[1] == "ZZLATE" for r in page2)

    def test_defensive_none_count_row_reads_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defensive ``fetchone() -> None`` arm of the COUNT (an injected
        conn seam could yield it) reads an honest 0, never a TypeError."""
        _fresh_caches(monkeypatch)
        _seed_mixed_ledger(tmp_path)
        page, total = query_window_ledger_page(_NoneCountConn(), tmp_path, limit=2, offset=0)
        assert total == 0 and len(page) == 2  # page still served; total defensive


class TestLedgerPageRow:
    # Widened annotation (Any elements): the row cells legitimately take None
    # in the mutation tests below — an unannotated literal tuple makes pyright
    # infer per-element Literal types, refusing `row[i] = None` (the CI-caught
    # reportCallIssue class).
    _BASE: tuple[Any, ...] = (
        "groww",
        "NIFTY",
        "1minute",
        "2025-01-01",
        "2025-01-31",
        "SUCCESS",
        5,
        1,
        "boom",
        _ISO,
        _ISO,
        0,
        1,
        3,
        2,
        1,
        "EMPTY@2026-07-05",  # streak_provenance (DEC-STREAK-PROVENANCE-001)
        # failed_streak — the 18th column (DEC-AUTO-RESUME-ZERO-SURPRISES-001).
        2,
    )

    def test_full_row_coerces_all_cells(self) -> None:
        assert _ledger_page_row(self._BASE) == self._BASE

    def test_optional_none_cells_pass_through(self) -> None:
        row = list(self._BASE)
        row[8] = None  # last_error
        row[10] = None  # last_attempt_at
        row[13] = None  # bars_net_new
        row[14] = None  # empty_retries
        row[15] = None  # transient_retries
        row[16] = None  # streak_provenance
        projected = _ledger_page_row(tuple(row))
        assert projected[8] is None and projected[10] is None
        assert projected[13] is None and projected[14] is None and projected[15] is None
        assert projected[16] is None

    def test_null_required_cell_raises(self) -> None:
        row = list(self._BASE)
        row[12] = None  # in_pull_fetches — legacy-derivable, must defer
        with pytest.raises(ValueError, match="defer to the legacy Python parse"):
            _ledger_page_row(tuple(row))
