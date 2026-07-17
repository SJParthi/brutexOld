"""Tests — the /db window-ledger grid's single-parse row projection (D3 + fix 3).

DEC-SUSPICION-SWEEP-FIXES-001: ``/api/v1/db/window-ledger`` previously ran an
UNCACHED ``load_window_ledger(...).to_rows()`` — a full Parquet re-parse of the
whole ledger (~841k rows on a pre-planned full-universe options pull) on EVERY
paging click. :func:`brutex.dashboard.db_view.window_ledger_rows_cached` routes
the read through the SAME stamp-keyed ``_ledger_records_cached`` single parse the
other ledger readers share and builds the canonical row projection
(:func:`brutex.data.window_ledger.ledger_record_row`) ON DEMAND — since
DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3 the projection is NOT cached (the
retired ``ledger-rows`` family was a SECOND resident whole-ledger per-row
payload; the records family is THE one residency) and the route rides the
O(page) :func:`brutex.dashboard.db_view.window_ledger_rows_page`. Pins: ONE
parse per ledger generation (a read-count SPY, the regression the sweep
demanded), byte-equality with ``WindowLedger.to_rows`` (whole + page — parity
against the VERBATIM pre-fix projection shape), mtime invalidation, the
no-ledger empty case, and the fix-3 no-second-family residency law.
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from brutex.dashboard import db_view
from brutex.dashboard.db_view import window_ledger_rows_cached, window_ledger_rows_page
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger, ledger_record_row
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


class TestWindowLedgerRowsPage:
    """DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 3 — the route's O(page) read."""

    def test_page_parity_with_the_verbatim_pre_fix_projection(self, tmp_path: Path) -> None:
        """BYTE-PARITY pin: every ``(total, page)`` the paged helper serves
        equals the VERBATIM pre-fix path — the whole-ledger projection
        (``ledger_record_row`` over the cached records, the exact value the
        retired ``ledger-rows`` family held) sliced ``[offset : offset+limit]``
        — across page boundaries, a past-the-end offset and the full read.
        Slice-then-project == project-then-slice because the projection is
        per-record pure."""
        _seed(tmp_path, ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "FINNIFTY"])
        # The VERBATIM pre-fix cached value: the whole-records projection.
        old_rows = tuple(ledger_record_row(r) for r in load_window_ledger(tmp_path).records())
        assert window_ledger_rows_cached(tmp_path) == old_rows  # whole-surface parity
        for offset, limit in [(0, 2), (2, 2), (4, 2), (0, 500), (3, 1), (999, 10)]:
            total, page = window_ledger_rows_page(tmp_path, offset=offset, limit=limit)
            assert total == len(old_rows)
            assert page == old_rows[offset : offset + limit]

    def test_empty_lake_reads_zero_total_empty_page(self, tmp_path: Path) -> None:
        assert window_ledger_rows_page(tmp_path, offset=0, limit=100) == (0, ())

    def test_base_less_delta_only_lake_pages_live_rows(self, tmp_path: Path) -> None:
        """A first pull's DELTA-ONLY checkpoint pages its live rows exactly
        like the whole-rows reader (the T-b6 base-less law on the page path)."""
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
        total, page = window_ledger_rows_page(tmp_path, offset=0, limit=100)
        assert total == 1
        assert [row[1] for row in page] == ["NIFTY"]
        assert list(page) == load_window_ledger(tmp_path).to_rows()

    def test_negative_offset_clamps_to_zero(self, tmp_path: Path) -> None:
        """The COMMON ``clamp_offset`` guard: a (route-impossible, ``ge=0``)
        negative offset reads page 1, never a Python negative-slice surprise."""
        _seed(tmp_path, ["NIFTY", "BANKNIFTY"])
        total, page = window_ledger_rows_page(tmp_path, offset=-5, limit=1)
        assert total == 2
        assert page == window_ledger_rows_page(tmp_path, offset=0, limit=1)[1]

    def test_page_read_leaves_no_rows_family_resident(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE fix-3 residency law: after serving the page consumer AND the
        whole-rows consumer AND the records consumer, the shared cache holds
        the ``ledger-records`` family as the ONLY per-row payload — no
        ``ledger-rows`` entry exists (the collapsed second residency)."""
        monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
        _seed(tmp_path, ["NIFTY"])
        window_ledger_rows_page(tmp_path, offset=0, limit=100)
        window_ledger_rows_cached(tmp_path)
        db_view._ledger_records_cached(tmp_path)
        keys = list(db_view._LEDGER_READ_CACHE._entries)
        assert [k.split("|", 1)[0] for k in keys] == ["ledger-records"]

    def test_page_read_shares_the_records_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeat paging clicks within one ledger stamp cost ZERO parses after
        the first (the records-cache hit) — and only the page is projected."""
        monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
        _seed(tmp_path, ["NIFTY", "BANKNIFTY", "SENSEX"])
        parses = {"n": 0}

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return load_window_ledger(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _counting_load)
        projected = {"n": 0}
        real_row = ledger_record_row

        def _counting_row(record: object) -> tuple[object, ...]:
            projected["n"] += 1
            return real_row(record)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.window_ledger.ledger_record_row", _counting_row)
        first = window_ledger_rows_page(tmp_path, offset=0, limit=1)
        second = window_ledger_rows_page(tmp_path, offset=1, limit=1)
        third = window_ledger_rows_page(tmp_path, offset=0, limit=1)
        assert parses["n"] == 1  # three clicks, ONE parse
        assert projected["n"] == 3  # ONE row projected per 1-row page — O(page)
        assert first == third
        assert first[1] != second[1]
