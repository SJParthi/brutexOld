"""Unit tests for the read-only DuckDB viewer (DEC-DB-VIEWER-001).

Exercises :mod:`brutex.dashboard.db_view` against a REAL temp
:class:`~brutex.data.instrument_store.InstrumentStore` (no FastAPI
dependency, so it runs in every environment):

* an empty opened store → every known table counts 0, samples empty;
* after ``upsert_many`` of a couple of instruments → counts reflect and
  ``sample_rows`` returns columns + rows with JSON-safe cells;
* an unknown table name raises ``ValueError`` (no SQL executed);
* an injection-y table name is rejected, NOT executed;
* the row limit clamps to ``[1, 500]`` (0 / -1 / huge);
* :func:`clamp_limit` standalone behaviour.

Target: 100% line + branch coverage of ``brutex/dashboard/db_view.py``.
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.dashboard.db_view import (
    KNOWN_TABLES,
    WINDOW_PHASES,
    _build_window_payload_duckdb,
    _json_safe,
    _window_counts_total,
    _window_severity_order_sql,
    build_phase_progress,
    build_window_payload,
    clamp_limit,
    clamp_offset,
    fno_current_phase_from_counts,
    phase_progress_payload,
    phase_window_progress,
    sample_rows,
    table_summary,
    window_status_label,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.session import IST
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.data.window_status import WindowStatusLog
from brutex.data.window_status_store import save_window_status
from brutex.models import Exchange, InstrumentType, Segment


def _open_empty(tmp_path: Path) -> InstrumentStore:
    """Open a fresh InstrumentStore (schema auto-created, zero rows)."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _idx(symbol: str, name: str) -> Instrument:
    """Build a minimal NSE index (IDX) instrument-master row."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _seed(store: InstrumentStore) -> None:
    """Upsert two index rows so the instruments table is non-empty."""
    store.upsert_many(
        [_idx("NIFTY", "NIFTY 50"), _idx("BANKNIFTY", "NIFTY BANK")],
        snapshot_date=date.today(),
        run_id="test-seed",
        csv_source_sha256="0" * 64,
    )


# ── clamp_limit ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 1),
        (-1, 1),
        (-9999, 1),
        (1, 1),
        (50, 50),
        (500, 500),
        (501, 500),
        (10**9, 500),
    ],
)
def test_clamp_limit(raw: int, expected: int) -> None:
    """clamp_limit pins any requested limit into [1, 500]."""
    assert clamp_limit(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), (-1, 0), (-9999, 0), (1, 1), (250, 250)],
)
def test_clamp_offset(raw: int, expected: int) -> None:
    """clamp_offset pins any requested offset to a non-negative integer."""
    assert clamp_offset(raw) == expected


# ── _json_safe ───────────────────────────────────────────────────────


def test_json_safe_passthrough_and_coercion() -> None:
    """_json_safe keeps primitives, decodes bytes, str()-ifies the rest."""
    assert _json_safe(None) is None
    assert _json_safe(True) is True
    assert _json_safe(7) == 7
    assert _json_safe(3.5) == 3.5
    assert _json_safe("hi") == "hi"
    # bytes are not JSON-safe → decoded for display
    assert _json_safe(b"abc") == "abc"
    assert _json_safe(b"\xff") == "�"  # invalid utf-8 → replacement char
    # a Decimal / date is not a primitive → str()-rendered
    assert _json_safe(Decimal("1.50")) == "1.50"
    assert _json_safe(date(2026, 5, 29)) == "2026-05-29"


# ── table_summary ────────────────────────────────────────────────────


def test_table_summary_empty_store_all_zero(tmp_path: Path) -> None:
    """A fresh store reports every known table with row_count 0."""
    store = _open_empty(tmp_path)
    summary = table_summary(store.connection)
    assert [row["table"] for row in summary] == list(KNOWN_TABLES)
    # schema_meta has a single seeded schema_version row; the others are
    # empty. We assert the data tables are zero and the shape is right.
    # expiries/contracts were dropped in DEC-EXPIRED-OPTIONS-TABLE-001.
    by_name = {row["table"]: row["row_count"] for row in summary}
    assert by_name["instruments"] == 0
    assert by_name["instrument_runs"] == 0
    assert by_name["schema_meta"] >= 1
    for row in summary:
        assert isinstance(row["row_count"], int)


def test_table_summary_reflects_inserts(tmp_path: Path) -> None:
    """After upsert_many the instruments count goes non-zero."""
    store = _open_empty(tmp_path)
    _seed(store)
    by_name = {row["table"]: row["row_count"] for row in table_summary(store.connection)}
    assert by_name["instruments"] == 2


def test_table_summary_tolerates_missing_table(tmp_path: Path) -> None:
    """A dropped table is reported as count 0, not an exception."""
    store = _open_empty(tmp_path)
    store.connection.execute("DROP TABLE instrument_runs")
    by_name = {row["table"]: row["row_count"] for row in table_summary(store.connection)}
    assert by_name["instrument_runs"] == 0
    # the other tables still report normally
    assert by_name["schema_meta"] >= 1


# ── sample_rows ──────────────────────────────────────────────────────


def test_sample_rows_empty_table(tmp_path: Path) -> None:
    """An empty table returns its columns but zero rows."""
    store = _open_empty(tmp_path)
    out = sample_rows(store.connection, "instruments", limit=50)
    assert out["table"] == "instruments"
    assert len(out["columns"]) > 0
    assert out["rows"] == []


def test_sample_rows_returns_columns_and_rows(tmp_path: Path) -> None:
    """A seeded table returns columns + JSON-safe row cells."""
    store = _open_empty(tmp_path)
    _seed(store)
    out = sample_rows(store.connection, "instruments", limit=50)
    assert out["table"] == "instruments"
    assert "symbol_name" in out["columns"]
    assert len(out["rows"]) == 2
    # every cell is JSON-safe: None, bool, int, float, or str
    for row in out["rows"]:
        for cell in row:
            assert cell is None or isinstance(cell, bool | int | float | str)
    # the symbols we seeded are present somewhere in the rows
    flat = {str(cell) for row in out["rows"] for cell in row}
    assert "NIFTY" in flat
    assert "BANKNIFTY" in flat


def test_sample_rows_schema_meta_roundtrip(tmp_path: Path) -> None:
    """schema_meta (always populated) yields a key/value row."""
    store = _open_empty(tmp_path)
    out = sample_rows(store.connection, "schema_meta", limit=10)
    assert out["columns"][:2] == ["key", "value"]
    flat = {str(cell) for row in out["rows"] for cell in row}
    assert "schema_version" in flat


def test_sample_rows_limit_clamped_low(tmp_path: Path) -> None:
    """A non-positive limit is clamped up to 1 (not an empty/error query)."""
    store = _open_empty(tmp_path)
    _seed(store)
    out = sample_rows(store.connection, "instruments", limit=0)
    assert len(out["rows"]) == 1
    out_neg = sample_rows(store.connection, "instruments", limit=-5)
    assert len(out_neg["rows"]) == 1


def test_sample_rows_limit_clamped_high(tmp_path: Path) -> None:
    """An absurd limit clamps to 500 (no unbounded read)."""
    store = _open_empty(tmp_path)
    _seed(store)
    # only 2 rows exist, but the query must not error on a huge limit
    out = sample_rows(store.connection, "instruments", limit=10**9)
    assert len(out["rows"]) == 2


def test_sample_rows_reports_total_and_page_bounds(tmp_path: Path) -> None:
    """sample_rows returns the indexed full-table total + the resolved page bounds."""
    store = _open_empty(tmp_path)
    _seed(store)
    out = sample_rows(store.connection, "instruments", limit=50)
    # total is the full row count (for the pager), NOT the page size.
    assert out["total"] == 2
    assert out["limit"] == 50
    assert out["offset"] == 0


def test_sample_rows_pages_via_offset(tmp_path: Path) -> None:
    """Paging with LIMIT/OFFSET returns disjoint pages, never the whole table."""
    store = _open_empty(tmp_path)
    _seed(store)
    page1 = sample_rows(store.connection, "instruments", limit=1, offset=0)
    page2 = sample_rows(store.connection, "instruments", limit=1, offset=1)
    page3 = sample_rows(store.connection, "instruments", limit=1, offset=2)
    assert len(page1["rows"]) == 1
    assert len(page2["rows"]) == 1
    assert page1["rows"] != page2["rows"]  # disjoint pages
    assert page3["rows"] == []  # past the end → empty, not an error
    assert page1["total"] == page2["total"] == 2


def test_sample_rows_negative_offset_clamped(tmp_path: Path) -> None:
    """A hostile negative offset is clamped to 0 (DuckDB would error otherwise)."""
    store = _open_empty(tmp_path)
    _seed(store)
    out = sample_rows(store.connection, "instruments", limit=50, offset=-10)
    assert out["offset"] == 0
    assert len(out["rows"]) == 2


def test_sample_rows_unknown_table_raises(tmp_path: Path) -> None:
    """An unknown table name raises ValueError before any SQL runs."""
    store = _open_empty(tmp_path)
    with pytest.raises(ValueError, match="unknown table"):
        sample_rows(store.connection, "not_a_table", limit=10)


def test_sample_rows_injection_rejected_not_executed(tmp_path: Path) -> None:
    """An injection-y table name is rejected; the real table survives."""
    store = _open_empty(tmp_path)
    _seed(store)
    payload = "instruments; DROP TABLE instrument_runs"
    with pytest.raises(ValueError, match="unknown table"):
        sample_rows(store.connection, payload, limit=10)
    # instrument_runs table must NOT have been dropped — the payload never ran
    by_name = {row["table"]: row["row_count"] for row in table_summary(store.connection)}
    assert "instrument_runs" in by_name
    # and instruments is still intact
    assert by_name["instruments"] == 2


def test_sample_rows_json_safe_handles_none(tmp_path: Path) -> None:
    """A NULL cell (e.g. sm_expiry_date for an index) stays JSON null."""
    store = _open_empty(tmp_path)
    _seed(store)
    out = sample_rows(store.connection, "instruments", limit=50)
    expiry_idx = out["columns"].index("sm_expiry_date")
    # indices have no expiry → that column is None in every seeded row
    assert all(row[expiry_idx] is None for row in out["rows"])


def test_json_safe_non_finite_floats_are_stringified() -> None:
    """NaN/±inf are stringified — Starlette's JSONResponse rejects them
    (hostile-review LOW: guards a future DOUBLE column from a 500)."""
    assert _json_safe(1.5) == 1.5  # finite float passes through unchanged
    assert _json_safe(float("nan")) == "nan"
    assert _json_safe(float("inf")) == "inf"
    assert _json_safe(float("-inf")) == "-inf"


# ── build_window_payload — /coverage/windows projection (DEC-WINDOW-LEDGER-001) ──

_WINDOW_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _window_key(security_id: str = "NIFTY", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market=GROWW_COVERAGE_MARKET,
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def test_build_window_payload_reads_the_ledger_when_present(tmp_path: Path) -> None:
    ledger = WindowLedger()
    ledger.plan(_window_key("SENSEX"), now=_WINDOW_NOW)  # PLANNED
    ledger.record(
        _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
    )  # SUCCESS
    save_window_ledger(ledger, tmp_path)

    payload = build_window_payload(tmp_path)
    assert payload["source"] == "ledger"
    assert payload["count"] == 2
    # PLANNED rows are counted but NOT in the default row list (hostile C3 —
    # they are the bulk of a freshly-planned pull); explicit status fetches them.
    assert payload["counts"]["spot"] == {"PLANNED": 1, "SUCCESS": 1}
    by_symbol = {w["symbol"]: w for w in payload["windows"]}
    assert "SENSEX" not in by_symbol
    planned = build_window_payload(tmp_path, status="PLANNED")["windows"]
    assert [w["symbol"] for w in planned] == ["SENSEX"]
    assert by_symbol["NIFTY"]["status"] == "SUCCESS"
    # Displayed as clean IST (CLAUDE.md §6): _WINDOW_NOW = 12:00 UTC → 17:30 IST.
    assert by_symbol["NIFTY"]["first_seen_at"] == _WINDOW_NOW.astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    # The legacy field names are preserved so the existing UI keeps working
    # (+ the DEC-PIPELINE-AUTOPREBUILD-001 chunk-3 `status_label` honesty field).
    assert set(by_symbol["NIFTY"]) == {
        "market",
        "symbol",
        "interval",
        "window_start",
        "window_end",
        "status",
        "status_label",
        "bars",
        "attempts",
        "empty_streak",
        "in_pull_fetches",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
    }
    # R1 visibility (DEC-EMPTY-STREAK-VISIBLE-001): the consecutive-EMPTY
    # streak ships per row and the promotion bound ships once at the top.
    assert by_symbol["NIFTY"]["empty_streak"] == 0
    assert payload["empty_threshold"] == 3
    # In-pull fetch count ships per row (DEC-WINDOW-INPULL-FETCHES-001): the
    # NIFTY SUCCESS row was seeded from the lake (one fetch's data → 1).
    assert by_symbol["NIFTY"]["in_pull_fetches"] == 1


def test_build_window_payload_falls_back_to_status_sidecar(tmp_path: Path) -> None:
    log = WindowStatusLog()
    log.record(
        _window_key("NIFTY"),
        raw_bars=0,
        error_type="DhanRateLimitError",
        bars_appended=0,
        now=_WINDOW_NOW,
    )
    save_window_status(log, tmp_path)

    payload = build_window_payload(tmp_path)
    assert payload["source"] == "status_log"
    assert payload["count"] == 1
    window = payload["windows"][0]
    assert window["status"] == "FAILED"
    assert window["first_seen_at"] is None  # legacy log has no first_seen_at
    assert window["empty_streak"] == 0  # legacy log has no streak — defaulted


def test_build_window_payload_ships_the_live_empty_streak(tmp_path: Path) -> None:
    # DEC-EMPTY-STREAK-VISIBLE-001 + DEC-EMPTY-STREAK-RATELIMIT-001: an EMPTY row at
    # attempts 3 with an INTERLEAVED rate-limit FAILED shows streak 2/3 — the page
    # can SAY why it is not yet SKIPPED. The FAILED no longer RESETS the streak (a
    # 429 is not evidence of data), so two genuine empties separated by a transient
    # rate-limit still count as two toward the threshold-3 promotion.
    ledger = WindowLedger()
    key = _window_key("NIFTY")
    ledger.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_WINDOW_NOW)
    ledger.record(
        key, raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_WINDOW_NOW
    )
    ledger.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_WINDOW_NOW)
    save_window_ledger(ledger, tmp_path)

    (window,) = build_window_payload(tmp_path, status="EMPTY")["windows"]
    assert window["status"] == "EMPTY"
    assert window["attempts"] == 3
    assert window["empty_streak"] == 2  # the FAILED PRESERVED the streak (not yet 3 → not SKIPPED)


def test_build_window_payload_empty_when_neither_exists(tmp_path: Path) -> None:
    assert build_window_payload(tmp_path) == {
        "source": "status_log",
        "count": 0,
        "counts": {"spot": {}, "options": {}, "futures": {}},
        "empty_threshold": 3,
        "matched": 0,
        "returned": 0,
        "capped": False,
        "windows": [],
    }


# ── option-window decode (DEC-WINDOW-OPTION-DECODE-001) ──────────────────


def _option_window_key(
    security_id: str = "25|NSE_FNO", interval: str = "OPT|WEEK|1|ATM+3|CALL"
) -> WindowKey:
    """An option-series ledger key exactly as option_ingest._coverage_key folds it."""
    return WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 1, 31),
    )


class TestOptionWindowDecode:
    """Option windows display the operator's PRECISE coordinates, never vendor ids.

    The live /coverage/windows complaint (2026-06-10): option rows rendered as
    ``25|NSE_FNO`` / ``OPT|WEEK|1|ATM+3|CALL`` — undecipherable and sorted below
    20k+ spot rows. The projection now decodes them via the id→symbol map.
    """

    def test_option_window_decodes_symbol_and_series(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(
            _option_window_key(), raw_bars=375, error_type=None, bars_appended=375, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        payload = build_window_payload(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
        (window,) = payload["windows"]
        assert window["symbol"] == "BANKNIFTY · NSE_FNO"
        assert window["interval"] == "WEEK#1 ATM+3 CALL · 1m"
        assert window["market"] == GROWW_OPTION_COVERAGE_MARKET  # the filter key stays raw

    def test_unknown_id_keeps_numeric_id_not_a_crash(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(
            _option_window_key(security_id="9999|BSE_FNO"),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=_WINDOW_NOW,
        )
        save_window_ledger(ledger, tmp_path)

        payload = build_window_payload(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
        (window,) = payload["windows"]
        assert window["symbol"] == "9999 · BSE_FNO"  # still precise, still distinguishable

    def test_no_map_still_decodes_series_coordinates(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(
            _option_window_key(), raw_bars=1, error_type=None, bars_appended=1, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        (window,) = build_window_payload(tmp_path)["windows"]
        assert window["symbol"] == "25 · NSE_FNO"
        assert window["interval"] == "WEEK#1 ATM+3 CALL · 1m"

    def test_malformed_option_key_falls_back_to_raw(self, tmp_path: Path) -> None:
        # A dhan-option row whose coordinates don't parse (no segment separator /
        # wrong arity) is shown RAW — fallback, never an exception, never hidden.
        ledger = WindowLedger()
        ledger.record(
            _option_window_key(security_id="25", interval="OPT|WEEK|1"),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=_WINDOW_NOW,
        )
        save_window_ledger(ledger, tmp_path)

        (window,) = build_window_payload(tmp_path, symbol_for_id={"25": "BANKNIFTY"})["windows"]
        assert window["symbol"] == "25"
        assert window["interval"] == "OPT|WEEK|1"

    def test_window_market_and_status_filters(self, tmp_path: Path) -> None:
        # C3 (DEC-OPTION-WINDOW-PLAN-001): server-side filters — the page no
        # longer ships every row to the browser.
        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        ledger.record(
            _option_window_key(), raw_bars=375, error_type=None, bars_appended=375, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        spot = build_window_payload(tmp_path, market="spot")
        assert [w["symbol"] for w in spot["windows"]] == ["NIFTY"]
        options = build_window_payload(tmp_path, market="options")
        assert [w["market"] for w in options["windows"]] == [GROWW_OPTION_COVERAGE_MARKET]
        assert options["counts"]["spot"] == {"SUCCESS": 1}
        assert options["counts"]["options"] == {"SUCCESS": 1}

    def test_futures_window_buckets_separately(self, tmp_path: Path) -> None:
        # DEC-COVERAGE-GROWW-NAMESPACE-001: a ``groww-future`` window lands in its
        # OWN "futures" bucket, NOT lumped into "spot" (the pre-fix bug);
        # ``?market=futures`` filters to it; the spot completeness rollup (exact
        # spot-market match) excludes it.
        from brutex.dashboard.db_view import spot_window_rollup

        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        ledger.record(
            WindowKey(
                market=GROWW_FUTURE_COVERAGE_MARKET,
                security_id="NSE-NIFTY-25Jan24-FUT",
                interval="1day",
                start=date(2024, 1, 1),
                end=date(2024, 1, 26),
            ),
            raw_bars=375,
            error_type=None,
            bars_appended=375,
            now=_WINDOW_NOW,
        )
        save_window_ledger(ledger, tmp_path)

        payload = build_window_payload(tmp_path)
        assert payload["counts"]["futures"] == {"SUCCESS": 1}
        assert payload["counts"]["spot"] == {"SUCCESS": 1}  # the future is NOT lumped here
        futures = build_window_payload(tmp_path, market="futures")
        assert [w["symbol"] for w in futures["windows"]] == ["NSE-NIFTY-25Jan24-FUT"]
        assert [w["market"] for w in futures["windows"]] == [GROWW_FUTURE_COVERAGE_MARKET]
        # The spot completeness rollup excludes futures (exact spot-market match).
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}

    def test_row_cap_keeps_failed_and_empty_first(self, tmp_path: Path) -> None:
        # The cap can never hide actionable rows: severity-first ordering puts
        # FAILED, then EMPTY, ahead of the bulk statuses (hostile C3).
        ledger = WindowLedger()
        for i in range(3):
            ledger.record(
                _window_key(f"OK{i}"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
            )
        ledger.record(
            _window_key("BROKEN"),
            raw_bars=0,
            error_type="DhanRateLimitError",
            bars_appended=0,
            now=_WINDOW_NOW,
        )
        ledger.record(
            _window_key("HOLLOW"), raw_bars=0, error_type=None, bars_appended=0, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        payload = build_window_payload(tmp_path, limit=2)
        assert payload["capped"] is True
        assert payload["matched"] == 5
        assert payload["returned"] == 2
        assert [w["symbol"] for w in payload["windows"]] == ["BROKEN", "HOLLOW"]

    def test_ledger_read_is_cached_until_the_file_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # C3: the 4 s page poll must not re-pay the full Parquet load — the read
        # is cached on the file's mtime and ANY flush invalidates it.
        import os

        from brutex.data import window_ledger_store as store_module
        from brutex.data.window_ledger_store import ledger_path

        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        calls = {"n": 0}
        real_load = store_module.load_window_ledger

        def _counting_load(root: Path, **kwargs: object) -> object:
            calls["n"] += 1
            return real_load(root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "load_window_ledger", _counting_load)

        first = build_window_payload(tmp_path)
        second = build_window_payload(tmp_path)
        assert first["windows"] == second["windows"]
        assert calls["n"] <= 1  # the second poll was served from the cache

        # Any flush bumps the mtime → the cache MUST refresh.
        path = ledger_path(tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns + 1, path.stat().st_mtime_ns + 1))
        loaded_before = calls["n"]
        build_window_payload(tmp_path)
        assert calls["n"] == loaded_before + 1


class TestSpotWindowRollup:
    """DEC-SPOT-COVERAGE-COMPLETENESS-001 — the (symbol, interval) ledger rollup."""

    def test_no_ledger_file_is_empty(self, tmp_path: Path) -> None:
        # The is_file() guard — never a FileNotFoundError on a fresh lake.
        from brutex.dashboard.db_view import spot_window_rollup

        assert spot_window_rollup(tmp_path) == {}

    def test_groups_resolved_pending_failed_and_excludes_options(self, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import spot_window_rollup

        ledger = WindowLedger()
        # SUCCESS + SKIPPED → resolved; PLANNED + non-terminal EMPTY → pending; FAILED
        # → failed (DEC-COMPLETENESS-HONEST-EMPTY-001 — a below-threshold EMPTY is
        # OUTSTANDING work the next pull re-fetches, so the chip counts it pending,
        # never "full"; a TERMINAL empty settles to SKIPPED → resolved).
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        ledger.settle_known_gap(
            WindowKey(
                market=GROWW_COVERAGE_MARKET,
                security_id="NIFTY",
                interval="1minute",
                start=date(2021, 4, 1),
                end=date(2021, 7, 1),
            ),
            now=_WINDOW_NOW,
        )
        ledger.plan(
            WindowKey(
                market=GROWW_COVERAGE_MARKET,
                security_id="NIFTY",
                interval="1minute",
                start=date(2021, 7, 1),
                end=date(2021, 10, 1),
            ),
            now=_WINDOW_NOW,
        )
        ledger.record(
            WindowKey(
                market=GROWW_COVERAGE_MARKET,
                security_id="NIFTY",
                interval="1minute",
                start=date(2021, 10, 1),
                end=date(2022, 1, 1),
            ),
            raw_bars=0,
            error_type=None,  # clean EMPTY (no data) — RESOLVED, NOT pending
            bars_appended=0,
            now=_WINDOW_NOW,
        )
        ledger.record(
            WindowKey(
                market=GROWW_COVERAGE_MARKET,
                security_id="NIFTY",
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2022, 1, 1),
            ),
            raw_bars=0,
            error_type="DhanRateLimitError",
            bars_appended=0,
            now=_WINDOW_NOW,
        )
        # An option row MUST be excluded by the exact spot-market filter.
        ledger.plan(_option_window_key(), now=_WINDOW_NOW)
        save_window_ledger(ledger, tmp_path)

        rollup = spot_window_rollup(tmp_path)
        assert rollup == {
            # resolved=2 (SUCCESS+SKIPPED), pending=2 (PLANNED + non-terminal EMPTY), failed=0
            ("NIFTY", "1minute"): (2, 2, 0),
            ("NIFTY", "1day"): (0, 0, 1),
        }

    def test_rollup_is_cached_until_the_file_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The grouping is O(rows) at universe scale — it must run once per
        # ledger flush, not once per 4 s poll (hostile C7).
        import os

        from brutex.dashboard import db_view as db_view_module
        from brutex.dashboard.db_view import spot_window_rollup
        from brutex.data.window_ledger_store import ledger_path

        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        calls = {"n": 0}
        real_records = db_view_module._ledger_records_cached

        def _counting_records(root: Path) -> tuple[object, ...]:
            calls["n"] += 1
            return real_records(root)

        monkeypatch.setattr(db_view_module, "_ledger_records_cached", _counting_records)

        first = spot_window_rollup(tmp_path)
        second = spot_window_rollup(tmp_path)
        assert first == second
        assert calls["n"] <= 1  # second poll served from the rollup cache

        path = ledger_path(tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns + 1, path.stat().st_mtime_ns + 1))
        before = calls["n"]
        spot_window_rollup(tmp_path)
        assert calls["n"] == before + 1  # mtime bump re-groups exactly once

    def test_spot_rows_are_byte_unchanged_by_the_map(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        with_map = build_window_payload(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
        without_map = build_window_payload(tmp_path)
        assert with_map == without_map
        assert with_map["windows"][0]["symbol"] == "NIFTY"


# ── build_window_payload DuckDB fast path (DEC-COVERAGE-WINDOWS-O1-001) ──────────
def _save_mixed_ledger(tmp_path: Path) -> None:
    """Save a ledger spanning spot + option + future markets and several statuses."""
    ledger = WindowLedger()
    # spot rows: SUCCESS, FAILED (last_error set), PLANNED, EMPTY (streak ≥ 1)
    ledger.record(
        _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
    )
    ledger.record(
        _window_key("BANKNIFTY"),
        raw_bars=0,
        error_type="GrowwTimeoutError",
        bars_appended=0,
        now=_WINDOW_NOW,
    )
    ledger.plan(_window_key("SENSEX"), now=_WINDOW_NOW)
    ledger.record(
        _window_key("INFY"), raw_bars=0, error_type=None, bars_appended=0, now=_WINDOW_NOW
    )
    # option + future SUCCESS rows (interval kept simple — bucket keys on `market`).
    ledger.record(
        WindowKey(
            market=GROWW_OPTION_COVERAGE_MARKET,
            security_id="OPT1",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        ),
        raw_bars=3,
        error_type=None,
        bars_appended=3,
        now=_WINDOW_NOW,
    )
    ledger.record(
        WindowKey(
            market=GROWW_FUTURE_COVERAGE_MARKET,
            security_id="FUT1",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        ),
        raw_bars=4,
        error_type=None,
        bars_appended=4,
        now=_WINDOW_NOW,
    )
    save_window_ledger(ledger, tmp_path)


def _win_key(w: dict[str, object]) -> tuple[object, ...]:
    return (w["market"], w["symbol"], w["status"], w["window_start"], w["window_end"])


class TestBuildWindowPayloadDuckDB:
    """The O(page) DuckDB fast path equals the legacy pyarrow path + adds pagination."""

    def test_fast_path_matches_legacy_path(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        fast = build_window_payload(tmp_path, conn=conn)
        legacy = build_window_payload(tmp_path)
        assert fast["source"] == legacy["source"] == "ledger"
        assert fast["count"] == legacy["count"] == 6
        assert fast["counts"] == legacy["counts"]
        assert fast["matched"] == legacy["matched"] == 5  # 6 total minus the 1 PLANNED
        assert fast["empty_threshold"] == legacy["empty_threshold"]
        assert fast["capped"] is False
        # Same window SET (both severity-first; compare order-independently).
        assert sorted(fast["windows"], key=_win_key) == sorted(legacy["windows"], key=_win_key)

    def test_severity_first_order(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        statuses = [w["status"] for w in payload["windows"]]
        # FAILED then EMPTY must precede the bulk SUCCESS rows (cap can't hide them).
        assert statuses[0] == "FAILED"
        assert statuses[1] == "EMPTY"
        assert set(statuses[2:]) == {"SUCCESS"}

    def test_market_filter_options(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), market="options")
        assert payload["matched"] == 1
        assert {w["symbol"] for w in payload["windows"]} == {"OPT1"}

    def test_market_filter_futures(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), market="futures")
        assert {w["symbol"] for w in payload["windows"]} == {"FUT1"}

    def test_market_filter_spot_excludes_derivatives(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), market="spot")
        symbols = {w["symbol"] for w in payload["windows"]}
        assert "OPT1" not in symbols and "FUT1" not in symbols
        assert symbols == {"NIFTY", "BANKNIFTY", "INFY"}  # the spot non-PLANNED rows

    def test_status_filter_failed(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), status="FAILED")
        assert [w["symbol"] for w in payload["windows"]] == ["BANKNIFTY"]
        assert payload["matched"] == 1

    def test_planned_only_on_explicit_request(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        default = build_window_payload(tmp_path, conn=conn)
        assert "SENSEX" not in {w["symbol"] for w in default["windows"]}
        planned = build_window_payload(tmp_path, conn=conn, status="PLANNED")
        assert [w["symbol"] for w in planned["windows"]] == ["SENSEX"]

    def test_offset_paging_is_disjoint_and_capped(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        page0 = build_window_payload(tmp_path, conn=conn, limit=1, offset=0)
        page1 = build_window_payload(tmp_path, conn=conn, limit=1, offset=1)
        assert page0["returned"] == page1["returned"] == 1
        assert page0["capped"] is True  # matched (5) > limit (1)
        assert page0["windows"][0]["status"] == "FAILED"  # severity-first page 1
        assert page1["windows"][0]["status"] == "EMPTY"  # severity-first page 2
        assert page0["windows"][0] != page1["windows"][0]

    def test_count_cache_memoises_and_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()
        calls = {"n": 0}
        original = _window_counts_total  # the top-level import (no second `import` form)

        def _counting(c: object, glob: str) -> tuple[dict[str, dict[str, int]], int]:
            calls["n"] += 1
            return original(c, glob)

        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
        # Two calls at the SAME generation → counts computed ONCE (the cache HIT).
        a = build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=1)
        b = build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=1)
        assert a["counts"] == b["counts"]
        assert calls["n"] == 1
        # A new generation (a pull) → recompute (never a stale total).
        build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=2)
        assert calls["n"] == 2

    def test_empty_ledger_parquet(self, tmp_path: Path) -> None:
        save_window_ledger(WindowLedger(), tmp_path)  # writes a 0-row Parquet
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        assert payload["count"] == 0
        assert payload["matched"] == 0
        assert payload["returned"] == 0
        assert payload["capped"] is False
        assert payload["counts"] == {"spot": {}, "options": {}, "futures": {}}
        assert payload["windows"] == []

    def test_no_conn_uses_legacy_path(self, tmp_path: Path) -> None:
        # conn=None (an existing caller) must keep the pyarrow path, not the fast path.
        _save_mixed_ledger(tmp_path)
        assert build_window_payload(tmp_path, conn=None)["source"] == "ledger"

    def test_window_counts_total_helper(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        from brutex.data.window_ledger_store import ledger_path

        counts, total = _window_counts_total(duckdb.connect(), ledger_path(tmp_path).as_posix())
        assert total == 6
        assert counts["options"] == {"SUCCESS": 1}
        assert counts["futures"] == {"SUCCESS": 1}
        assert counts["spot"] == {"SUCCESS": 1, "FAILED": 1, "PLANNED": 1, "EMPTY": 1}

    def test_severity_order_sql_is_built_from_the_status_order(self) -> None:
        sql = _window_severity_order_sql()
        assert sql.startswith("CASE status ")
        assert "WHEN 'FAILED' THEN 0" in sql
        assert "WHEN 'EMPTY' THEN 1" in sql
        assert sql.endswith("ELSE 9 END")

    def test_direct_duckdb_builder_with_symbol_map(self, tmp_path: Path) -> None:
        # Exercise _build_window_payload_duckdb directly (the option symbol_for_id decode
        # path is shared with the legacy projection via _window_row).
        _save_mixed_ledger(tmp_path)
        from brutex.data.window_ledger_store import ledger_path

        payload = _build_window_payload_duckdb(
            duckdb.connect(),
            ledger_path(tmp_path),
            symbol_for_id={"OPT1": "BANKNIFTY"},
            market=None,
            status=None,
            limit=20_000,
            offset=0,
            count_cache=None,
            generation=0,
        )
        assert payload["count"] == 6
        assert payload["matched"] == 5

    def test_legacy_shape_parquet_derives_streaks(self, tmp_path: Path) -> None:
        # A LEGACY pre-R1 Parquet lacks empty_streak/in_pull_fetches; BOTH the fast path
        # and the legacy load must DERIVE them identically (hostile-review MAJOR: NOT
        # cosmetic — empty_streak tracks EMPTY→SKIPPED promotion).
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.window_ledger_store import ledger_dir, ledger_path

        core = (
            "market",
            "security_id",
            "interval",
            "window_start",
            "window_end",
            "status",
            "bars",
            "attempts",
            "last_error",
            "first_seen_at",
            "last_attempt_at",
        )
        schema = pa.schema(
            [
                ("market", pa.string()),
                ("security_id", pa.string()),
                ("interval", pa.string()),
                ("window_start", pa.string()),
                ("window_end", pa.string()),
                ("status", pa.string()),
                ("bars", pa.int64()),
                ("attempts", pa.int64()),
                ("last_error", pa.string()),
                ("first_seen_at", pa.string()),
                ("last_attempt_at", pa.string()),
            ]
        )
        row = {
            "market": "groww",
            "security_id": "EMPTYCO",
            "interval": "1minute",
            "window_start": "2021-01-01",
            "window_end": "2021-04-01",
            "status": "EMPTY",
            "bars": 0,
            "attempts": 5,
            "last_error": None,
            "first_seen_at": "2026-06-08T12:00:00Z",
            "last_attempt_at": "2026-06-08T12:00:00Z",
        }
        ledger_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({c: [row[c]] for c in core}, schema=schema), ledger_path(tmp_path))

        fast = build_window_payload(tmp_path, conn=duckdb.connect(), status="EMPTY")
        legacy = build_window_payload(tmp_path, status="EMPTY")
        (fw,) = fast["windows"]
        (lw,) = legacy["windows"]
        assert fw["empty_streak"] == lw["empty_streak"] == 5  # EMPTY → attempts (derived)
        assert fw["in_pull_fetches"] == lw["in_pull_fetches"] == 1  # non-PLANNED → 1 (derived)
        assert fast["windows"] == legacy["windows"]  # byte-for-byte on a legacy-shape ledger

    def test_invalid_market_returns_no_rows(self, tmp_path: Path) -> None:
        # An unrecognized non-null market matches NO bucket — matched 0 + no rows (legacy
        # parity), NOT every row; counts stay complete.
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), market="junk")
        assert payload["matched"] == 0
        assert payload["windows"] == []
        assert payload["count"] == 6  # the always-complete counts are unaffected

    def test_wrong_shape_parquet_falls_back_to_legacy_empty(self, tmp_path: Path) -> None:
        # A corrupt / wrong-shape ledger Parquet (missing the `status` core column) makes
        # the DuckDB GROUP BY raise — it must NOT 500, but fall through to the legacy path
        # which guards core columns and degrades to an empty ledger (route "never a 500").
        # The probe's ValueError is DETERMINISTIC, so the torn-read retry sleeper must
        # NEVER fire on this path (DEC-WINDOW-TORN-READ-RETRY-001: duckdb.Error ONLY).
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.window_ledger_store import ledger_dir, ledger_path

        ledger_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"market": ["groww"], "security_id": ["X"]}), ledger_path(tmp_path))
        sleeps: list[float] = []
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append)
        assert payload["source"] == "ledger"
        assert payload["count"] == 0
        assert payload["windows"] == []
        assert sleeps == []  # real ValueError shape error → immediate fallback, no retry


# ── Live per-phase pull progress (DEC-UNIVERSE-PER-PHASE-PROGRESS-001) ────────
#
# The Universe pull section shows, for EVERY window phase (spot / futures /
# options), a live windows count + progress + status breakdown — CONSISTENTLY.
# The per-segment counts are the SAME generation-cached `_window_counts_total`
# the /coverage/windows page reads (no new scan); `phase_window_progress` /
# `fno_current_phase_from_counts` / `build_phase_progress` are pure projections
# over those counts, and `phase_progress_payload` is the one route entry point.


class TestPhaseWindowProgress:
    """`phase_window_progress` folds ONE bucket's status counts into the display shape."""

    def test_full_status_breakdown(self) -> None:
        p = phase_window_progress(
            {"SUCCESS": 5, "SKIPPED": 2, "EMPTY": 3, "FAILED": 1, "PLANNED": 4}
        )
        assert p == {
            "done": 7,  # SUCCESS + SKIPPED (the settled authority)
            "total": 15,  # every status summed
            "success": 5,
            "empty": 3,
            "failed": 1,
            "pending": 4,  # PLANNED
        }

    def test_zero_window_phase_reads_all_zero(self) -> None:
        # A phase with NO windows honestly reads total==0 — never a fake value.
        assert phase_window_progress({}) == {
            "done": 0,
            "total": 0,
            "success": 0,
            "empty": 0,
            "failed": 0,
            "pending": 0,
        }

    def test_missing_status_keys_default_to_zero(self) -> None:
        # Only SUCCESS present: done == total == success, the rest 0.
        assert phase_window_progress({"SUCCESS": 9}) == {
            "done": 9,
            "total": 9,
            "success": 9,
            "empty": 0,
            "failed": 0,
            "pending": 0,
        }

    def test_pending_only_options_flush_lag(self) -> None:
        # The options flush-lag: PLANNED rows on disk, nothing fetched yet → pending,
        # never a fabricated 0-done "complete".
        p = phase_window_progress({"PLANNED": 120})
        assert p["pending"] == 120
        assert p["done"] == 0
        assert p["total"] == 120

    def test_failed_phase_surfaces_failed_count(self) -> None:
        # A FAILED phase surfaces its failed count honestly (never masked).
        p = phase_window_progress({"FAILED": 7, "SUCCESS": 3})
        assert p["failed"] == 7
        assert p["done"] == 3
        assert p["total"] == 10


class TestFnoCurrentPhaseFromCounts:
    """`fno_current_phase_from_counts` refines historical → futures/options from counts."""

    def test_futures_current_while_pending(self) -> None:
        counts = {"futures": {"SUCCESS": 2, "PLANNED": 3}, "options": {"PLANNED": 10}}
        assert fno_current_phase_from_counts(counts) == "futures"

    def test_options_current_once_futures_drained(self) -> None:
        # Futures fully settled (no PLANNED) → options has taken over.
        counts = {"futures": {"SUCCESS": 5, "SKIPPED": 1}, "options": {"SUCCESS": 2, "PLANNED": 8}}
        assert fno_current_phase_from_counts(counts) == "options"

    def test_no_futures_windows_yet_stays_futures(self) -> None:
        # Nothing planned yet (total==0) → futures is still the current sub-phase.
        assert fno_current_phase_from_counts({}) == "futures"
        assert fno_current_phase_from_counts({"options": {"PLANNED": 4}}) == "futures"


_PHASE_COUNTS: dict[str, dict[str, int]] = {
    "spot": {"SUCCESS": 10, "PLANNED": 2},
    "futures": {"SUCCESS": 3, "PLANNED": 1},
    "options": {"PLANNED": 5},
}


class TestBuildPhaseProgress:
    """`build_phase_progress` assembles the three phase rows + the current-phase flag."""

    def test_always_three_rows_in_pull_order(self) -> None:
        rows = build_phase_progress(_PHASE_COUNTS, current_phase="spot")
        assert [r["phase"] for r in rows] == list(WINDOW_PHASES) == ["spot", "futures", "options"]

    def test_spot_phase_highlights_spot(self) -> None:
        rows = build_phase_progress(_PHASE_COUNTS, current_phase="spot")
        current = {r["phase"] for r in rows if r["current"]}
        assert current == {"spot"}
        (spot,) = [r for r in rows if r["phase"] == "spot"]
        assert spot["done"] == 10 and spot["total"] == 12 and spot["pending"] == 2

    def test_metadata_phase_highlights_no_window_row(self) -> None:
        # The metadata phase writes no windows → no row is current; all read pending.
        rows = build_phase_progress(_PHASE_COUNTS, current_phase="metadata")
        assert not any(r["current"] for r in rows)

    def test_historical_phase_refined_to_futures_when_pending(self) -> None:
        rows = build_phase_progress(_PHASE_COUNTS, current_phase="historical")
        assert {r["phase"] for r in rows if r["current"]} == {"futures"}

    def test_historical_phase_refined_to_options_when_futures_drained(self) -> None:
        counts = {
            "spot": {"SUCCESS": 10},
            "futures": {"SUCCESS": 4},  # no PLANNED — drained
            "options": {"SUCCESS": 1, "PLANNED": 9},
        }
        rows = build_phase_progress(counts, current_phase="historical")
        assert {r["phase"] for r in rows if r["current"]} == {"options"}

    def test_explicit_futures_and_options_labels_also_refine(self) -> None:
        # A caller passing the refined label directly is still counts-driven (idempotent).
        assert {
            r["phase"]
            for r in build_phase_progress(_PHASE_COUNTS, current_phase="futures")
            if r["current"]
        } == {"futures"}

    def test_idle_label_highlights_nothing(self) -> None:
        rows = build_phase_progress(_PHASE_COUNTS, current_phase="")
        assert not any(r["current"] for r in rows)

    def test_empty_counts_all_rows_zero_and_neutral(self) -> None:
        rows = build_phase_progress({}, current_phase="spot")
        assert all(r["total"] == 0 and r["done"] == 0 for r in rows)
        # spot is current (the label) but its total is honestly 0 (never a fake).
        assert {r["phase"] for r in rows if r["current"]} == {"spot"}


def _save_phase_ledger(tmp_path: Path) -> None:
    """Write a ledger with a DISTINCT status mix per bucket (spot/futures/options).

    spot: SUCCESS + FAILED + PLANNED; futures: SUCCESS + PLANNED; options: PLANNED only
    (the flush-lag shape — planned but not yet fetched).
    """
    ledger = WindowLedger()
    # spot SUCCESS
    ledger.record(
        _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
    )
    # spot FAILED
    ledger.record(
        _window_key("BANKNIFTY"),
        raw_bars=0,
        error_type="GrowwTimeoutError",
        bars_appended=0,
        now=_WINDOW_NOW,
    )
    # spot PLANNED
    ledger.plan(_window_key("SENSEX"), now=_WINDOW_NOW)
    # futures SUCCESS + PLANNED
    fut_ok = WindowKey(
        market=GROWW_FUTURE_COVERAGE_MARKET,
        security_id="FUT1",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )
    ledger.record(fut_ok, raw_bars=4, error_type=None, bars_appended=4, now=_WINDOW_NOW)
    ledger.plan(
        WindowKey(
            market=GROWW_FUTURE_COVERAGE_MARKET,
            security_id="FUT2",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        ),
        now=_WINDOW_NOW,
    )
    # options PLANNED only (flush-lag: planned but not yet fetched)
    ledger.plan(
        WindowKey(
            market=GROWW_OPTION_COVERAGE_MARKET,
            security_id="OPT1",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        ),
        now=_WINDOW_NOW,
    )
    save_window_ledger(ledger, tmp_path)


class TestPhaseProgressPayload:
    """`phase_progress_payload` reads the CACHED window counts (no new scan) and projects."""

    def test_reads_real_ledger_all_three_phases(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path

        _save_phase_ledger(tmp_path)
        rows = phase_progress_payload(duckdb.connect(), ledger_path(tmp_path), current_phase="spot")
        by_phase = {r["phase"]: r for r in rows}
        assert set(by_phase) == set(WINDOW_PHASES)
        assert by_phase["spot"] == {
            "phase": "spot",
            "current": True,
            "done": 1,  # NIFTY SUCCESS
            "total": 3,  # + BANKNIFTY FAILED + SENSEX PLANNED
            "success": 1,
            "empty": 0,
            "failed": 1,
            "pending": 1,
        }
        assert by_phase["futures"]["done"] == 1 and by_phase["futures"]["pending"] == 1
        assert by_phase["options"]["pending"] == 1  # flush-lag → pending, never a fake 0
        assert by_phase["options"]["done"] == 0

    def test_dry_run_no_ledger_yields_empty_list(self, tmp_path: Path) -> None:
        # No ledger Parquet at all (a dry-run / a pull that planned nothing) → [].
        from brutex.data.window_ledger_store import ledger_path

        assert (
            phase_progress_payload(duckdb.connect(), ledger_path(tmp_path), current_phase="spot")
            == []
        )

    def test_historical_label_highlights_futures_over_a_real_ledger(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path

        _save_phase_ledger(tmp_path)
        rows = phase_progress_payload(
            duckdb.connect(), ledger_path(tmp_path), current_phase="historical"
        )
        # futures still has a PLANNED window → it is the current sub-phase.
        assert {r["phase"] for r in rows if r["current"]} == {"futures"}

    def test_cache_reuses_counts_across_two_polls(self, tmp_path: Path) -> None:
        # Prove NO new O(files) scan per poll: `_window_counts_total` runs ONCE per
        # (generation, mtime) across two polls (the cache HIT), then again on a new
        # generation (a pull) — the O(1)-on-repeat guarantee.
        from brutex.data.window_ledger_store import ledger_path

        _save_phase_ledger(tmp_path)
        ledger_file = ledger_path(tmp_path)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()
        calls = {"n": 0}
        original = _window_counts_total

        def _counting(c: object, glob: str) -> tuple[dict[str, dict[str, int]], int]:
            calls["n"] += 1
            return original(c, glob)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
            phase_progress_payload(
                conn, ledger_file, current_phase="spot", count_cache=cache, generation=1
            )
            phase_progress_payload(
                conn, ledger_file, current_phase="spot", count_cache=cache, generation=1
            )
            assert calls["n"] == 1  # second poll is an O(1) cache HIT
            # A new generation (a pull bumped it) → recompute, never stale.
            phase_progress_payload(
                conn, ledger_file, current_phase="spot", count_cache=cache, generation=2
            )
            assert calls["n"] == 2

    def test_empty_ledger_parquet_all_zero_rows(self, tmp_path: Path) -> None:
        # A 0-row ledger Parquet (planned nothing yet) → three neutral zero rows.
        from brutex.data.window_ledger_store import ledger_path

        save_window_ledger(WindowLedger(), tmp_path)
        rows = phase_progress_payload(duckdb.connect(), ledger_path(tmp_path), current_phase="spot")
        assert [r["phase"] for r in rows] == list(WINDOW_PHASES)
        assert all(r["total"] == 0 and r["done"] == 0 for r in rows)


# ── window_status_label — empty-vs-failed honesty (DEC-PIPELINE-AUTOPREBUILD-001 c3) ──


class TestWindowStatusLabel:
    """The ONE common label function both payload paths share via ``_window_row``."""

    def test_empty_streak_one(self) -> None:
        assert window_status_label("EMPTY", 1) == "vendor confirmed no trades (1 clean attempt(s))"

    def test_empty_streak_two(self) -> None:
        assert window_status_label("EMPTY", 2) == "vendor confirmed no trades (2 clean attempt(s))"

    def test_empty_streak_three(self) -> None:
        assert window_status_label("EMPTY", 3) == "vendor confirmed no trades (3 clean attempt(s))"

    def test_empty_streak_zero_floors_to_one(self) -> None:
        # A legacy record with no streak column still had the ONE clean attempt
        # that classified it EMPTY — the floor keeps the label honest, never "0".
        assert window_status_label("EMPTY", 0) == "vendor confirmed no trades (1 clean attempt(s))"

    def test_skipped_reads_settled(self) -> None:
        assert window_status_label("SKIPPED", 3) == (
            "vendor confirmed no trades (3 clean attempt(s) — settled)"
        )

    def test_skipped_streak_floors_to_one(self) -> None:
        # A fast-settled old-gap SKIPPED may carry a sub-threshold streak.
        assert window_status_label("SKIPPED", 0) == (
            "vendor confirmed no trades (1 clean attempt(s) — settled)"
        )

    def test_skipped_with_expired_window_stamp_reads_the_honest_reason(self) -> None:
        """DEC-OLD-EMPTY-SINGLE-CONFIRM-001: the single-confirm settle stamps its
        reason as last_error; the label uses IT verbatim (+ "— settled") instead
        of the streak-derived count — the forced-threshold streak would lie
        ("3 clean attempt(s)") about a 1-attempt expired-window settle."""
        from brutex.data.window_ledger import expired_window_settle_reason

        stamp = expired_window_settle_reason(1)
        assert window_status_label("SKIPPED", 3, stamp) == (
            "vendor confirmed no trades (1 clean attempt, expired window) — settled"
        )
        # Plural genuine count kept honest too.
        assert window_status_label("SKIPPED", 3, expired_window_settle_reason(2)) == (
            "vendor confirmed no trades (2 clean attempts, expired window) — settled"
        )

    def test_skipped_with_unrelated_last_error_keeps_streak_label(self) -> None:
        """A non-stamp last_error (e.g. REOPENED) keeps the streak-derived label."""
        assert window_status_label("SKIPPED", 2, "REOPENED") == (
            "vendor confirmed no trades (2 clean attempt(s) — settled)"
        )
        assert window_status_label("SKIPPED", 2, None) == (
            "vendor confirmed no trades (2 clean attempt(s) — settled)"
        )

    @pytest.mark.parametrize(
        "reason", ["ALL_FILTERED", "INTERIOR_GAP", "SUSPECTED_PARTIAL", "DEGRADED"]
    )
    def test_skipped_with_demoted_reason_reads_completeness_gap(self, reason: str) -> None:
        """DEC-DATA-HONESTY-ALLFILTERED-SEED-001 (refuter F-3 fold): a SKIPPED row
        settled through the #1031 degraded channel must NOT read "vendor confirmed
        no trades" — the vendor RETURNED bars every time (boundary-filtered /
        hole-flagged / truncation-flagged); the label names the reason honestly.
        ``DEGRADED`` is record()'s reason-less defensive fallback (latent — both
        spines always pass a concrete tag): same channel, same honest wording
        (wiring-review fold, PR #1158)."""
        assert window_status_label("SKIPPED", 3, reason) == (
            f"completeness gap accepted ({reason}, 3 flagged attempt(s)) — settled"
        )

    def test_skipped_demoted_reason_streak_floors_to_one(self) -> None:
        assert window_status_label("SKIPPED", 0, "ALL_FILTERED") == (
            "completeness gap accepted (ALL_FILTERED, 1 flagged attempt(s)) — settled"
        )

    def test_failed_with_demoted_reason_keeps_repull_label(self) -> None:
        # A demoted FAILED is honestly re-pullable — the generic label already
        # says so; only the terminal SKIPPED needed the reason-aware wording.
        assert window_status_label("FAILED", 1, "ALL_FILTERED") == "FAILED (will re-pull)"

    def test_failed_reads_will_repull(self) -> None:
        assert window_status_label("FAILED", 0) == "FAILED (will re-pull)"

    def test_success_is_the_bare_status(self) -> None:
        assert window_status_label("SUCCESS", 0) == "SUCCESS"

    def test_planned_is_the_bare_status(self) -> None:
        assert window_status_label("PLANNED", 0) == "PLANNED"

    def test_unknown_status_degrades_to_itself(self) -> None:
        assert window_status_label("WEIRD", 5) == "WEIRD"


class TestWindowPayloadStatusLabels:
    """Both payload paths carry the label — via the SHARED ``_window_row`` projection."""

    def test_fast_and_legacy_paths_emit_identical_labels(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        fast = build_window_payload(tmp_path, conn=duckdb.connect())
        legacy = build_window_payload(tmp_path)
        fast_labels = {w["symbol"]: w["status_label"] for w in fast["windows"]}
        legacy_labels = {w["symbol"]: w["status_label"] for w in legacy["windows"]}
        assert fast_labels == legacy_labels
        # The honest spellings, per status (the mixed ledger's known rows).
        assert fast_labels["BANKNIFTY"] == "FAILED (will re-pull)"  # errored fetch
        assert fast_labels["INFY"] == "vendor confirmed no trades (1 clean attempt(s))"
        assert fast_labels["NIFTY"] == "SUCCESS"

    def test_planned_row_label_via_explicit_status_filter(self, tmp_path: Path) -> None:
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), status="PLANNED")
        (row,) = payload["windows"]
        assert row["symbol"] == "SENSEX"
        assert row["status_label"] == "PLANNED"

    def test_terminally_settled_empty_reads_settled(self, tmp_path: Path) -> None:
        # Three genuine clean EMPTYs promote the window to SKIPPED (R1 threshold);
        # its label then reads "… — settled" with the true streak count.
        from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD

        ledger = WindowLedger()
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            ledger.record(
                _window_key("TATASTEEL"),
                raw_bars=0,
                error_type=None,
                bars_appended=0,
                now=_WINDOW_NOW,
            )
        save_window_ledger(ledger, tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        (row,) = payload["windows"]
        assert row["status"] == "SKIPPED"
        assert row["status_label"] == ("vendor confirmed no trades (3 clean attempt(s) — settled)")

    def test_legacy_status_log_rows_carry_labels(self, tmp_path: Path) -> None:
        # The pre-ledger window_status.json fallback has NO empty_streak field —
        # the label floors at 1 clean attempt (getattr keeps ONE row shape).
        log = WindowStatusLog()
        log.record(
            _window_key("WIPRO"), raw_bars=0, error_type=None, bars_appended=0, now=_WINDOW_NOW
        )
        save_window_status(log, tmp_path)
        payload = build_window_payload(tmp_path)
        assert payload["source"] == "status_log"
        (row,) = payload["windows"]
        assert row["status_label"] == "vendor confirmed no trades (1 clean attempt(s))"


def test_fetch_windows_template_renders_status_label() -> None:
    """The /coverage/windows page renders the server's honesty label.

    String-presence pins: the row builder reads ``r.status_label``, renders it
    through the page's ``esc()`` escaping convention, and — when the label is
    present — REPLACES the legacy x/3 streak span (the label carries the same
    clean-attempt count; never printed twice). The legacy streak widget survives
    as the fallback for an older payload.
    """
    template = (
        Path(__file__).resolve().parents[2]
        / "brutex"
        / "dashboard"
        / "templates"
        / "fetch_windows.html"
    ).read_text(encoding="utf-8")
    assert "r.status_label" in template
    assert "esc(label)" in template  # escaped, never raw interpolation
    assert "label && label !== v" in template  # replace-not-double-print guard
    assert "r.empty_streak" in template  # the legacy fallback widget is kept
