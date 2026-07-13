"""Tests — the /db window-ledger grid's mtime-cached row projection (D3).

DEC-SUSPICION-SWEEP-FIXES-001: ``/api/v1/db/window-ledger`` previously ran an
UNCACHED ``load_window_ledger(...).to_rows()`` — a full Parquet re-parse of the
whole ledger (~841k rows on a pre-planned full-universe options pull) on EVERY
paging click. :func:`brutex.dashboard.db_view.window_ledger_rows_cached` routes
the read through the SAME mtime-keyed ``_ledger_records_cached`` single parse the
other ledger readers share and memoises the canonical row projection
(:func:`brutex.data.window_ledger.ledger_record_row`). Pins: ONE parse per ledger
generation (a read-count SPY, the regression the sweep demanded), byte-equality
with ``WindowLedger.to_rows``, mtime invalidation, and the no-ledger empty case.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from brutex.dashboard import db_view
from brutex.dashboard.db_view import window_ledger_rows_cached
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger


def _key(symbol: str) -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=symbol,
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 1, 31),
    )


def _seed(lake_root: Path, symbols: list[str]) -> None:
    ledger = WindowLedger()
    for symbol in symbols:
        ledger.record(
            _key(symbol),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            bars_net_new=3,
            now=datetime(2026, 7, 1, tzinfo=UTC),
        )
    save_window_ledger(ledger, lake_root)


class TestWindowLedgerRowsCached:
    def test_rows_byte_equal_canonical_to_rows(self, tmp_path: Path) -> None:
        """The cached projection equals ``WindowLedger.to_rows`` exactly — ONE
        row-shape source (``ledger_record_row``), so the grid can never drift."""
        _seed(tmp_path, ["NIFTY", "BANKNIFTY"])
        expected = load_window_ledger(tmp_path).to_rows()
        assert list(window_ledger_rows_cached(tmp_path)) == expected

    def test_one_parse_per_generation_spy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE D3 read-count pin: repeated paging reads within one ledger
        generation cost EXACTLY ONE Parquet parse; a ledger flush (mtime bump)
        costs exactly one more — never one per click.

        DEC-LEDGER-DELTA-O-PAGE-001: the shared ledger read cache now carries
        the #920 adaptive floor, so a post-flush read WITHIN the floor serves
        the prior rows stale (bounded + logged); the injected clock steps past
        the floor so this pin keeps asserting the fresh-parse-per-stamp law.
        """
        clock = {"t": 0.0}
        monkeypatch.setattr(
            db_view,
            "_LEDGER_READ_CACHE",
            db_view._make_ledger_read_cache(  # pyright: ignore[reportPrivateUsage]
                clock=lambda: clock["t"], swr_runner=lambda job: job()
            ),
        )
        _seed(tmp_path, ["NIFTY"])
        parses = {"n": 0}

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return load_window_ledger(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _counting_load)
        first = window_ledger_rows_cached(tmp_path)
        second = window_ledger_rows_cached(tmp_path)
        third = window_ledger_rows_cached(tmp_path)
        assert parses["n"] == 1  # three "clicks", one parse
        assert first == second == third
        # A new ledger generation (flush → new mtime) → exactly one fresh parse
        # once past the adaptive floor. (The SAVE itself may parse for its
        # merge-on-save — that is the writer's cost, not the grid's — so pin
        # the DELTA of the grid reads only.)
        _seed(tmp_path, ["NIFTY", "SENSEX"])
        clock["t"] += 121.0  # step past the floor cap → the SWR arm: one
        # last-known serve while the (inline) revalidator parses ONCE.
        base = parses["n"]
        stale = window_ledger_rows_cached(tmp_path)
        assert parses["n"] == base + 1  # exactly one fresh parse (the revalidate)
        assert len(stale) == 1  # the last-known projection — never a blocked read
        # The rows FAMILY holds its own cached entry: one more one-serve SWR
        # cycle on the ledger-rows key (no extra parse — records are fresh).
        window_ledger_rows_cached(tmp_path)
        refreshed = window_ledger_rows_cached(tmp_path)
        assert len(refreshed) == 2  # the landed store serves the flush
        assert window_ledger_rows_cached(tmp_path) == refreshed  # still cached
        assert parses["n"] == base + 1

    def test_no_ledger_file_returns_empty(self, tmp_path: Path) -> None:
        """A lake with no ledger Parquet AND no deltas reads ``()`` — the same
        empty grid the uncached read produced (never a raise, never a 500)."""
        assert window_ledger_rows_cached(tmp_path) == ()

    def test_base_less_delta_only_lake_renders_rows(self, tmp_path: Path) -> None:
        """DEC-LEDGER-DELTA-O-PAGE-001 (the fixed base-less inconsistency): a
        first pull's DELTA-ONLY checkpoint (no ``ledger.parquet`` yet) now
        renders its live rows on the /db grid — ``_ledger_records_cached``
        always read it while this projection returned ``()``."""
        from brutex.data.window_ledger_store import append_ledger_delta

        live = WindowLedger()
        live.record(
            _key("NIFTY"),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            bars_net_new=3,
            now=datetime(2026, 7, 1, tzinfo=UTC),
        )
        assert append_ledger_delta(live, tmp_path) == 1
        rows = window_ledger_rows_cached(tmp_path)
        assert [row[1] for row in rows] == ["NIFTY"]
        assert list(rows) == load_window_ledger(tmp_path).to_rows()

    def test_rows_cache_shares_the_records_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rows projection rides ``_ledger_records_cached`` — a grid read
        after another ledger reader already parsed this generation costs ZERO
        additional parses (ONE COMMON parse, CLAUDE.md §11)."""
        _seed(tmp_path, ["NIFTY"])
        # Warm the shared records cache the way the other readers do.
        db_view._ledger_records_cached(tmp_path)  # pyright: ignore[reportPrivateUsage]
        parses = {"n": 0}

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return load_window_ledger(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _counting_load)
        assert len(window_ledger_rows_cached(tmp_path)) == 1
        assert parses["n"] == 0  # the records parse was reused, rows only projected
