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
    TABLE_SUMMARY_FAULT_TYPES,
    WINDOW_PHASES,
    _build_window_payload_duckdb,
    _json_safe,
    _ledger_scan_source,
    _window_counts_since,
    _window_counts_total,
    _window_severity_order_sql,
    build_phase_progress,
    build_window_payload,
    clamp_limit,
    clamp_offset,
    fno_current_phase_from_counts,
    last_pull_start_utc,
    phase_progress_payload,
    phase_window_progress,
    sample_rows,
    table_summary,
    window_status_label,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.errors import CatalogError
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


def test_table_summary_default_flag_parity_and_strict_reraises(tmp_path: Path) -> None:
    """Default vs explicit ``raise_on_fault=False`` are identical (the kw-only
    default keeps every existing caller byte-identical), and strict mode
    RE-RAISES the store fault (``duckdb.Error`` — a dropped table) the default
    folds to 0 — the round-8 probe→compute TOCTOU root fix."""
    store = _open_empty(tmp_path)
    store.connection.execute("DROP TABLE instrument_runs")
    default = table_summary(store.connection)
    explicit = table_summary(store.connection, raise_on_fault=False)
    assert explicit == default
    assert {r["table"]: r["row_count"] for r in default}["instrument_runs"] == 0
    with pytest.raises(duckdb.Error):
        table_summary(store.connection, raise_on_fault=True)


def test_table_summary_strict_reraises_wedge_catalog_error() -> None:
    """The DEC-STORE-WRITE-DEADLINE-001 wedge latch's ``CatalogError`` (a
    ``DataLakeError``, NOT a ``duckdb.Error``) re-raises under strict mode
    instead of folding into a cacheable all-zero summary — the exact round-8
    wedge-after-probe poison class. ``CatalogError`` is also pinned as a
    :data:`TABLE_SUMMARY_FAULT_TYPES` member here."""

    class _Wedged:
        def execute(self, _sql: str) -> object:
            raise CatalogError("store wedged: abandoned coverage write")

    assert CatalogError in TABLE_SUMMARY_FAULT_TYPES
    assert all(r["row_count"] == 0 for r in table_summary(_Wedged()))  # default folds
    with pytest.raises(CatalogError, match="wedged"):
        table_summary(_Wedged(), raise_on_fault=True)


def test_table_summary_non_fault_exception_folds_in_both_modes() -> None:
    """An exception OUTSIDE :data:`TABLE_SUMMARY_FAULT_TYPES` still folds to 0
    even under strict mode — the never-a-500 posture for unexpected defect
    classes is unchanged by the round-8 flag."""

    class _Broken:
        def execute(self, _sql: str) -> object:
            raise RuntimeError("unexpected defect class")

    for flag in (False, True):
        rows = table_summary(_Broken(), raise_on_fault=flag)
        assert [r["table"] for r in rows] == list(KNOWN_TABLES)
        assert all(r["row_count"] == 0 for r in rows)


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
        "streak_provenance",
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
        # is cached on the ledger stamp and ANY flush invalidates it (past the
        # DEC-LEDGER-DELTA-O-PAGE-001 adaptive floor — the injected clock steps
        # over it so the fresh-parse-per-stamp law stays pinned).
        import os

        from brutex.dashboard import db_view as db_view_module
        from brutex.data import window_ledger_store as store_module
        from brutex.data.window_ledger_store import ledger_path

        clock = {"t": 0.0}
        monkeypatch.setattr(
            db_view_module,
            "_LEDGER_READ_CACHE",
            db_view_module._make_ledger_read_cache(
                clock=lambda: clock["t"], swr_runner=lambda job: job()
            ),
        )

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

        # Any flush bumps the mtime → the cache MUST refresh (past the floor).
        path = ledger_path(tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns + 1, path.stat().st_mtime_ns + 1))
        clock["t"] += 121.0  # step past the adaptive-floor cap
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
        # ledger flush, not once per 4 s poll (hostile C7). The injected clock
        # steps past the DEC-LEDGER-DELTA-O-PAGE-001 adaptive floor so the
        # flush → exactly-one-regroup law stays pinned.
        import os

        from brutex.dashboard import db_view as db_view_module
        from brutex.dashboard.db_view import spot_window_rollup
        from brutex.data.window_ledger_store import ledger_path

        clock = {"t": 0.0}
        monkeypatch.setattr(
            db_view_module,
            "_LEDGER_READ_CACHE",
            db_view_module._make_ledger_read_cache(
                clock=lambda: clock["t"], swr_runner=lambda job: job()
            ),
        )
        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)

        calls = {"n": 0}
        from brutex.data import window_ledger_store as store_module

        real_load = store_module.load_window_ledger

        def _counting_load(root: Path, **kwargs: object) -> object:
            calls["n"] += 1
            return real_load(root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "load_window_ledger", _counting_load)

        first = spot_window_rollup(tmp_path)
        second = spot_window_rollup(tmp_path)
        assert first == second
        # Object IDENTITY: the second poll is served from the rollup cache —
        # the O(rows) regroup never re-ran (a recompute builds a new dict).
        assert second is first
        assert calls["n"] <= 1  # and the ledger was parsed at most once

        path = ledger_path(tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns + 1, path.stat().st_mtime_ns + 1))
        clock["t"] += 121.0  # step past the adaptive-floor cap
        before = calls["n"]
        # Past the cap the records key rides stale-while-revalidate
        # (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001): the read serves the
        # last-known rollup while the (inline) revalidator re-parses ONCE.
        third = spot_window_rollup(tmp_path)
        assert calls["n"] == before + 1  # mtime bump re-parses exactly once…
        assert third is first  # …served last-known — never a blocking regroup
        spot_window_rollup(tmp_path)  # the rollup key's own one-serve SWR cycle
        fresh = spot_window_rollup(tmp_path)
        assert calls["n"] == before + 1  # STILL exactly one re-parse (the law)
        assert fresh is not first  # the landed revalidates re-grouped anew
        assert fresh == first
        assert spot_window_rollup(tmp_path) is fresh  # then cached again

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

        def _counting(c: object, source: object) -> tuple[dict[str, dict[str, int]], int]:
            calls["n"] += 1
            return original(c, source)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
        # Two calls at the SAME ledger stamp → counts computed ONCE (the cache HIT) —
        # even across GENERATION bumps (DEC-LEDGER-DELTA-O-PAGE-001: the keys ride the
        # composite stamp token at a fixed generation=0, so a mid-pull mark_dirty storm
        # can never thrash the entry).
        a = build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=1)
        b = build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=2)
        assert a["counts"] == b["counts"]
        assert calls["n"] == 1
        # A STAMP move (a fresh ledger save — new base mtime) → recompute (never stale).
        _save_mixed_ledger(tmp_path)
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

        counts, total = _window_counts_total(
            duckdb.connect(), _ledger_scan_source(ledger_path(tmp_path))
        )
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

    # ── counts-only mode (DEC-WINDOWS-COUNTS-ONLY-001, ``limit=0``) ──────────

    def test_counts_only_meta_equals_the_default_call(self, tmp_path: Path) -> None:
        """``limit=0`` ships the IDENTICAL counts/meta with ``windows=[]``.

        The byte-parity pin for the counts-only consumers: every meta field the
        default call carries is unchanged — only the row list is empty (and
        ``returned``/``capped`` reflect that honestly: matching rows exist,
        none shipped).
        """
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        default = build_window_payload(tmp_path, conn=conn)
        counts_only = build_window_payload(tmp_path, conn=conn, limit=0)
        assert counts_only == {
            **default,
            "windows": [],
            "returned": 0,
            "capped": True,  # matched (5) > limit (0) — rows exist, none shipped
        }
        assert counts_only["counts"] == default["counts"]
        assert counts_only["count"] == 6
        assert counts_only["matched"] == 5

    def test_counts_only_skips_the_row_page_query(self, tmp_path: Path) -> None:
        """``limit=0`` never issues the row-page ``LIMIT ? OFFSET ?`` query.

        The point of the mode: the counts GROUP BY is stamp-cached and computed
        independently, so counts-only costs ZERO row-page work — spied on the
        real connection's SQL text (never a ``LIMIT 0`` plan either).
        """
        _save_mixed_ledger(tmp_path)
        real = duckdb.connect()
        executed: list[str] = []

        class _SpyConn:
            def execute(self, sql: str, params: list[object] | None = None) -> object:
                executed.append(sql)
                return real.execute(sql, params) if params is not None else real.execute(sql)

        payload = build_window_payload(tmp_path, conn=_SpyConn(), limit=0)
        assert payload["windows"] == []
        assert payload["counts"]["spot"]["FAILED"] == 1  # counts still complete
        assert not any("LIMIT ? OFFSET ?" in sql for sql in executed), (
            "counts-only must skip the row-page query entirely"
        )
        # A rowful call on the same spy DOES issue the page query (spy sanity).
        build_window_payload(tmp_path, conn=_SpyConn(), limit=1)
        assert any("LIMIT ? OFFSET ?" in sql for sql in executed)

    def test_counts_only_market_and_status_filters_shape_matched(self, tmp_path: Path) -> None:
        """counts-only ``matched`` still honours the market/status filters (from counts)."""
        _save_mixed_ledger(tmp_path)
        conn = duckdb.connect()
        assert build_window_payload(tmp_path, conn=conn, limit=0, market="options") == {
            **build_window_payload(tmp_path, conn=conn, market="options"),
            "windows": [],
            "returned": 0,
            "capped": True,
        }
        failed = build_window_payload(tmp_path, conn=conn, limit=0, status="FAILED")
        assert failed["matched"] == 1
        assert failed["windows"] == []

    def test_counts_only_on_the_legacy_paths(self, tmp_path: Path) -> None:
        """``limit=0`` on the ``conn=None`` pyarrow path and the empty-lake default."""
        _save_mixed_ledger(tmp_path)
        legacy = build_window_payload(tmp_path, conn=None, limit=0)
        assert legacy["source"] == "ledger"
        assert legacy["windows"] == []
        assert legacy["matched"] == 5
        assert legacy["counts"] == build_window_payload(tmp_path, conn=None)["counts"]
        # An empty lake stays the honest all-zero payload (capped False — no rows hidden).
        empty = build_window_payload(tmp_path / "nowhere", limit=0)
        assert empty["windows"] == []
        assert empty["matched"] == 0
        assert empty["capped"] is False

    def test_negative_limit_still_clamps_up_to_one(self, tmp_path: Path) -> None:
        """Only EXACTLY 0 means counts-only — a negative direct-caller limit keeps
        the historical clamp-to-1 (the route already refuses negatives via ge=0)."""
        _save_mixed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), limit=-3)
        assert payload["returned"] == 1  # clamped UP to 1, not counts-only

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
        # ledger stamp across polls (the cache HIT) — even across GENERATION bumps
        # (DEC-LEDGER-DELTA-O-PAGE-001: fixed generation=0, stamp-token keys) — then
        # again on a STAMP move (a fresh save), never stale.
        from brutex.data.window_ledger_store import ledger_path

        _save_phase_ledger(tmp_path)
        ledger_file = ledger_path(tmp_path)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()
        calls = {"n": 0}
        original = _window_counts_total

        def _counting(c: object, source: object) -> tuple[dict[str, dict[str, int]], int]:
            calls["n"] += 1
            return original(c, source)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
            phase_progress_payload(
                conn, ledger_file, current_phase="spot", count_cache=cache, generation=1
            )
            phase_progress_payload(
                conn, ledger_file, current_phase="spot", count_cache=cache, generation=2
            )
            assert calls["n"] == 1  # second poll is an O(1) cache HIT (stamp unchanged)
            # A STAMP move (a fresh ledger save) → recompute, never stale.
            _save_phase_ledger(tmp_path)
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

    def test_window_status_label_today_incomplete_failed_wording(self) -> None:
        """DEC-PULL-TODAY-COMPLETENESS-GATE-001: a covers-today window demoted
        because the vendor has not settled TODAY yet reads the honest deferral
        wording — a time-transient re-pull, never an error / never a data hole."""
        from brutex.data.today_completeness import TODAY_INCOMPLETE

        assert window_status_label("FAILED", 0, TODAY_INCOMPLETE) == (
            "today's session not yet settled by vendor (will re-pull)"
        )
        # The (structurally unreachable) SKIPPED arm is covered defensively by
        # DEMOTED_SETTLE_REASONS membership — never "vendor confirmed no trades".
        assert window_status_label("SKIPPED", 1, TODAY_INCOMPLETE) == (
            f"completeness gap accepted ({TODAY_INCOMPLETE}, 1 flagged attempt(s)) — settled"
        )

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


# ── per-run vs lifetime window counts (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001) ──
# Session-B item 6: the /coverage/windows counts were LIFETIME-only ("88 empty" shown
# when only 24 happened tonight). A memoised per-run GROUP BY filtered on
# last_attempt_at >= the current pull's journal `started` instant adds the honest
# "N this run" figure — OMITTED entirely when no journal/pull-start exists.

_RUN_OLD = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
_RUN_NEW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_RUN_SINCE = "2026-06-08T11:00:00+00:00"


def _save_two_pull_ledger(tmp_path: Path) -> None:
    """A ledger whose rows straddle the pull-start instant ``_RUN_SINCE``.

    OLD pull (10:00 UTC): a NIFTY EMPTY. NEW pull (12:00 UTC): a BANKNIFTY EMPTY +
    an INFY SUCCESS. Plus a PLANNED SENSEX row (NULL ``last_attempt_at`` — never
    attempted, must NEVER enter the per-run figure).
    """
    ledger = WindowLedger()
    ledger.record(_window_key("NIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_RUN_OLD)
    ledger.record(
        _window_key("BANKNIFTY"), raw_bars=0, error_type=None, bars_appended=0, now=_RUN_NEW
    )
    ledger.record(_window_key("INFY"), raw_bars=5, error_type=None, bars_appended=5, now=_RUN_NEW)
    ledger.plan(_window_key("SENSEX"), now=_RUN_NEW)
    save_window_ledger(ledger, tmp_path)


class TestWindowCountsSincePullStart:
    """The per-run counts helper + its payload threading and memoisation."""

    def test_counts_since_filters_to_rows_attempted_on_or_after_since(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path

        _save_two_pull_ledger(tmp_path)
        counts, total = _window_counts_since(
            duckdb.connect(), _ledger_scan_source(ledger_path(tmp_path)), _RUN_SINCE
        )
        # Only the NEW pull's two attempted rows: the OLD EMPTY is before the start,
        # the PLANNED row has a NULL last_attempt_at (attempted statuses only).
        assert counts["spot"] == {"EMPTY": 1, "SUCCESS": 1}
        assert total == 2

    def test_counts_since_boundary_is_inclusive(self, tmp_path: Path) -> None:
        """A row stamped EXACTLY at the pull start counts (>= — the pull's own first
        window, recorded the same instant, is this run's work)."""
        from brutex.data.window_ledger_store import ledger_path

        _save_two_pull_ledger(tmp_path)
        counts, total = _window_counts_since(
            duckdb.connect(), _ledger_scan_source(ledger_path(tmp_path)), _RUN_NEW.isoformat()
        )
        assert total == 2  # both 12:00 rows; the 10:00 row is out
        assert counts["spot"] == {"EMPTY": 1, "SUCCESS": 1}

    def test_counts_since_empty_when_start_after_every_attempt(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path

        _save_two_pull_ledger(tmp_path)
        counts, total = _window_counts_since(
            duckdb.connect(),
            _ledger_scan_source(ledger_path(tmp_path)),
            "2026-06-08T13:00:00+00:00",
        )
        assert total == 0
        assert counts == {"spot": {}, "options": {}, "futures": {}}

    def test_payload_with_since_adds_per_run_keys_uncached(self, tmp_path: Path) -> None:
        _save_two_pull_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), since=_RUN_SINCE)
        assert payload["counts_since_pull_start"]["spot"] == {"EMPTY": 1, "SUCCESS": 1}
        assert payload["pull_start_utc"] == _RUN_SINCE
        # The lifetime counts are UNCHANGED alongside (both figures shown honestly).
        assert payload["counts"]["spot"] == {"EMPTY": 2, "SUCCESS": 1, "PLANNED": 1}

    def test_payload_without_since_is_byte_identical_no_new_keys(self, tmp_path: Path) -> None:
        """PARITY PIN (Session-B contract): ``since=None`` (every pre-change caller +
        the no-journal lake) adds ZERO keys — the payload equals the pre-change
        shape exactly; the per-run figure is OMITTED, never a fabricated 0."""
        _save_two_pull_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        assert "counts_since_pull_start" not in payload
        assert "pull_start_utc" not in payload
        assert set(payload) == {
            "source",
            "count",
            "counts",
            "empty_threshold",
            "matched",
            "returned",
            "capped",
            "windows",
        }

    def test_payload_since_memoised_and_keyed_by_pull_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REUSES the generation-stamped memo (no new cache class): a repeat poll at
        the same (generation, mtime, pull-start) computes ONCE; a NEW pull start
        re-keys (never served the prior run's per-run figures)."""
        _save_two_pull_ledger(tmp_path)
        conn = duckdb.connect()
        cache = MoversSnapshotCache()
        calls = {"n": 0}
        original = _window_counts_since

        def _counting(
            c: object, source: object, since: str
        ) -> tuple[dict[str, dict[str, int]], int]:
            calls["n"] += 1
            return original(c, source, since)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_since", _counting)
        a = build_window_payload(
            tmp_path, conn=conn, count_cache=cache, generation=1, since=_RUN_SINCE
        )
        b = build_window_payload(
            tmp_path, conn=conn, count_cache=cache, generation=1, since=_RUN_SINCE
        )
        assert a["counts_since_pull_start"] == b["counts_since_pull_start"]
        assert calls["n"] == 1  # memoised — the repeat poll is an O(1) hit
        later = build_window_payload(
            tmp_path,
            conn=conn,
            count_cache=cache,
            generation=1,
            since="2026-06-08T13:00:00+00:00",
        )
        assert calls["n"] == 2  # a new pull start re-keys → recompute
        assert later["counts_since_pull_start"] == {"spot": {}, "options": {}, "futures": {}}

    def test_payload_cached_without_since_never_queries_per_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_two_pull_ledger(tmp_path)

        def _boom(*_a: object, **_k: object) -> tuple[dict[str, dict[str, int]], int]:
            raise AssertionError("per-run counts must not be queried without a pull start")

        monkeypatch.setattr("brutex.dashboard.db_view._window_counts_since", _boom)
        payload = build_window_payload(
            tmp_path, conn=duckdb.connect(), count_cache=MoversSnapshotCache(), generation=1
        )
        assert "counts_since_pull_start" not in payload

    def test_legacy_no_conn_path_omits_per_run_even_with_since(self, tmp_path: Path) -> None:
        """The pyarrow fallback cannot answer the per-run figure — honest omission."""
        _save_two_pull_ledger(tmp_path)
        payload = build_window_payload(tmp_path, conn=None, since=_RUN_SINCE)
        assert payload["source"] == "ledger"
        assert "counts_since_pull_start" not in payload
        assert "pull_start_utc" not in payload


class TestLastPullStartUtc:
    """The journal-derived pull-start anchor — honest ``None`` when unknowable."""

    def test_none_when_no_journal_exists(self, tmp_path: Path) -> None:
        assert last_pull_start_utc(tmp_path) is None

    def test_returns_latest_real_started_wall_utc(self, tmp_path: Path) -> None:
        from brutex.observability.pull_journal import (
            record_pull_finish,
            record_pull_heartbeat,
            record_pull_start,
        )

        record_pull_start(tmp_path, pull_id="p1", dry_run=False, now_utc=_RUN_OLD, monotonic_s=1.0)
        record_pull_finish(tmp_path, pull_id="p1", verdict="clean", now_utc=_RUN_OLD)
        record_pull_start(tmp_path, pull_id="p2", dry_run=False, now_utc=_RUN_NEW, monotonic_s=2.0)
        record_pull_heartbeat(tmp_path, pull_id="p2", now_utc=_RUN_NEW)
        assert last_pull_start_utc(tmp_path) == _RUN_NEW.isoformat()

    def test_dry_run_starts_are_skipped(self, tmp_path: Path) -> None:
        """A dry-run writes no ledger rows — anchoring on it would zero the per-run
        figure right after a real pull, so the LAST REAL start wins."""
        from brutex.observability.pull_journal import record_pull_start

        record_pull_start(tmp_path, pull_id="real", dry_run=False, now_utc=_RUN_OLD)
        record_pull_start(tmp_path, pull_id="dry", dry_run=True, now_utc=_RUN_NEW)
        assert last_pull_start_utc(tmp_path) == _RUN_OLD.isoformat()

    def test_none_when_only_dry_run_starts_exist(self, tmp_path: Path) -> None:
        from brutex.observability.pull_journal import record_pull_start

        record_pull_start(tmp_path, pull_id="dry", dry_run=True, now_utc=_RUN_NEW)
        assert last_pull_start_utc(tmp_path) is None

    def test_garbled_trailing_line_is_tolerated(self, tmp_path: Path) -> None:
        """The tolerant journal reader skips a crash-truncated line (the exact crash
        the journal exists to survive) — the prior real start still anchors."""
        from brutex.observability.pull_journal import journal_path, record_pull_start

        record_pull_start(tmp_path, pull_id="p1", dry_run=False, now_utc=_RUN_OLD)
        with journal_path(tmp_path).open("a", encoding="utf-8") as handle:
            handle.write('{"event": "started", "pull_id"')  # power-loss mid-append
        assert last_pull_start_utc(tmp_path) == _RUN_OLD.isoformat()


class TestLedgerDeltaAwareness:
    """DEC-LEDGER-DELTA-FLUSH-001 — every /coverage ledger reader sees pending deltas.

    The mid-pull flush now APPENDS delta Parquets beside ``ledger.parquet``
    instead of rewriting it; a base-only reader would freeze for the whole
    pull. The fast DuckDB paths SERVE the latest-wins fold IN SQL while deltas
    are pending (DEC-LEDGER-DELTA-O-PAGE-001 — supersedes the defer-to-legacy
    stance; the dedicated fold suite lives in
    ``test_dashboard_db_view_ledger_delta_fold.py``).
    """

    @staticmethod
    def _append_delta(tmp_path: Path, security_id: str) -> None:
        from brutex.data.window_ledger_store import append_ledger_delta, load_window_ledger

        live = load_window_ledger(tmp_path)
        live.record(
            _window_key(security_id), raw_bars=7, error_type=None, bars_appended=7, now=_WINDOW_NOW
        )
        assert append_ledger_delta(live, tmp_path) == 1

    def test_deltas_dirname_pinned_to_the_store_constant(self) -> None:
        """The not-imported dirname copy cannot drift from the ONE definition."""
        from brutex.dashboard import db_view as db_view_module
        from brutex.data.window_ledger_store import DELTAS_DIRNAME

        assert db_view_module._LEDGER_DELTAS_DIRNAME == DELTAS_DIRNAME

    def test_window_payload_fast_path_serves_the_delta_fold(self, tmp_path: Path) -> None:
        """With a conn AND pending deltas, the payload reflects the delta rows.

        Regression: the DuckDB fast path once read ONLY the base Parquet, which
        the mid-pull flush no longer rewrites — the page froze at pull-start
        state for the whole pull. It now folds the deltas latest-wins IN SQL
        (DEC-LEDGER-DELTA-O-PAGE-001) and stays field-for-field equal to the
        Python fold on this fixture.
        """
        import duckdb

        base = WindowLedger()
        base.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(base, tmp_path)
        self._append_delta(tmp_path, "BANKNIFTY")
        conn = duckdb.connect()
        try:
            payload = build_window_payload(tmp_path, conn=conn)
        finally:
            conn.close()
        assert payload["source"] == "ledger"
        assert payload["count"] == 2  # base row + the delta-only row
        assert {w["symbol"] for w in payload["windows"]} == {"NIFTY", "BANKNIFTY"}
        # Byte-parity with the conn-less legacy path (ONE fold, one answer).
        assert payload == build_window_payload(tmp_path)

    def test_window_payload_base_less_deltas_read_as_ledger(self, tmp_path: Path) -> None:
        """A first pull's delta-only checkpoint is a LIVE ledger, never status_log."""
        self._append_delta(tmp_path, "NIFTY")
        payload = build_window_payload(tmp_path)
        assert payload["source"] == "ledger"
        assert [w["symbol"] for w in payload["windows"]] == ["NIFTY"]

    def test_records_cache_refreshes_on_a_delta_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The injected clock steps past the DEC-LEDGER-DELTA-O-PAGE-001 adaptive
        # floor so the delta-append → fresh-parse law stays pinned (a read
        # WITHIN the floor serves the prior fold stale — bounded + logged).
        from brutex.dashboard import db_view as db_view_module

        clock = {"t": 0.0}
        monkeypatch.setattr(
            db_view_module,
            "_LEDGER_READ_CACHE",
            db_view_module._make_ledger_read_cache(
                clock=lambda: clock["t"], swr_runner=lambda job: job()
            ),
        )
        base = WindowLedger()
        base.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(base, tmp_path)
        assert {w["symbol"] for w in build_window_payload(tmp_path)["windows"]} == {"NIFTY"}
        self._append_delta(tmp_path, "SENSEX")  # base mtime UNTOUCHED
        clock["t"] += 121.0  # step past the adaptive-floor cap
        # One last-known SWR serve (the prior fold), then the landed (inline)
        # revalidate serves the delta row — bounded, logged, never blocking.
        assert {w["symbol"] for w in build_window_payload(tmp_path)["windows"]} == {"NIFTY"}
        assert {w["symbol"] for w in build_window_payload(tmp_path)["windows"]} == {
            "NIFTY",
            "SENSEX",
        }

    def test_spot_window_rollup_sees_mid_pull_deltas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import db_view as db_view_module
        from brutex.dashboard.db_view import spot_window_rollup

        clock = {"t": 0.0}
        monkeypatch.setattr(
            db_view_module,
            "_LEDGER_READ_CACHE",
            db_view_module._make_ledger_read_cache(
                clock=lambda: clock["t"], swr_runner=lambda job: job()
            ),
        )
        base = WindowLedger()
        base.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(base, tmp_path)
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}
        self._append_delta(tmp_path, "BANKNIFTY")  # base mtime UNTOUCHED
        clock["t"] += 121.0  # step past the adaptive-floor cap
        # Layer-by-layer SWR convergence (records, then the derived rollup):
        # two last-known serves, then the delta row lands — never blocking.
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}
        spot_window_rollup(tmp_path)  # the rollup key's own one-serve cycle
        assert spot_window_rollup(tmp_path) == {
            ("NIFTY", "1minute"): (1, 0, 0),
            ("BANKNIFTY", "1minute"): (1, 0, 0),
        }

    def test_spot_window_rollup_base_less_deltas(self, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import spot_window_rollup

        self._append_delta(tmp_path, "NIFTY")
        assert spot_window_rollup(tmp_path) == {("NIFTY", "1minute"): (1, 0, 0)}

    def test_phase_progress_folds_pending_deltas(self, tmp_path: Path) -> None:
        """Mid-pull the live phase rows count over the fold, not the frozen base."""
        import duckdb
        from brutex.data.window_ledger_store import ledger_path as _ledger_path

        base = WindowLedger()
        base.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(base, tmp_path)
        self._append_delta(tmp_path, "BANKNIFTY")
        conn = duckdb.connect()
        try:
            rows = phase_progress_payload(conn, _ledger_path(tmp_path), current_phase="spot")
        finally:
            conn.close()
        spot_row = next(r for r in rows if r["phase"] == "spot")
        assert spot_row["done"] == 2  # the delta row is COUNTED mid-pull
        assert spot_row["total"] == 2


# ── the legacy window-payload fallback honours offset (DEC-DB-FNO-DISPLAY-TRUTH-001 item 2) ──


class TestBuildWindowPayloadLegacyOffset:
    """The legacy pyarrow / status_log fallback pages with the SAME
    ``clamp_offset`` law the DuckDB fast path binds — pre-fix its slice was
    ``selected[:limit]``, so every "Next ›" click on a legacy backing silently
    re-served page 1."""

    @staticmethod
    def _seed_ledger(tmp_path: Path, n: int = 5) -> list[str]:
        # Symbols seeded in sorted order with identical interval/dates so the
        # legacy stable sort and the DuckDB secondary ORDER BY agree row-for-row.
        symbols = [f"SYM{i}" for i in range(n)]
        ledger = WindowLedger()
        for s in symbols:
            ledger.record(
                _window_key(s), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
            )
        save_window_ledger(ledger, tmp_path)
        return symbols

    def test_legacy_offset_returns_the_offset_slice(self, tmp_path: Path) -> None:
        # FAILS BEFORE THE FIX: offset=2 returned rows 1-2 (page 1 again).
        symbols = self._seed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, limit=2, offset=2)  # conn=None → legacy
        assert [w["symbol"] for w in payload["windows"]] == symbols[2:4]
        assert payload["matched"] == 5
        assert payload["returned"] == 2

    def test_legacy_offset_past_the_end_is_an_empty_page(self, tmp_path: Path) -> None:
        self._seed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, limit=2, offset=10_000)
        assert payload["windows"] == []
        assert payload["returned"] == 0
        assert payload["matched"] == 5  # matched is the filter total, not the page

    def test_legacy_negative_offset_clamps_to_zero(self, tmp_path: Path) -> None:
        symbols = self._seed_ledger(tmp_path)
        payload = build_window_payload(tmp_path, limit=2, offset=-5)
        assert [w["symbol"] for w in payload["windows"]] == symbols[:2]

    def test_legacy_page_matches_the_duckdb_fast_path_page(self, tmp_path: Path) -> None:
        # Parity pin: the SAME filter/sort inputs at the SAME offset yield the SAME
        # rows on both backings (the seeded tier order is deterministic on both).
        self._seed_ledger(tmp_path)
        legacy = build_window_payload(tmp_path, limit=2, offset=2)
        fast = build_window_payload(tmp_path, limit=2, offset=2, conn=duckdb.connect())
        assert legacy["windows"] == fast["windows"]
        assert legacy["matched"] == fast["matched"]

    def test_status_log_fallback_honours_offset_too(self, tmp_path: Path) -> None:
        # The frozen window_status.json backing pages identically.
        log = WindowStatusLog()
        for s in ("AAA", "BBB", "CCC"):
            log.record(
                _window_key(s),
                raw_bars=0,
                error_type="DhanRateLimitError",
                bars_appended=0,
                now=_WINDOW_NOW,
            )
        save_window_status(log, tmp_path)
        payload = build_window_payload(tmp_path, limit=2, offset=2)
        assert payload["source"] == "status_log"
        assert [w["symbol"] for w in payload["windows"]] == ["CCC"]
        assert payload["returned"] == 1


# ────────────────────────────────────────────────────────────────────────
# R11 window-census read scope (DEC-UNIVERSE-INDEX-ONLY-001, #1934)
# ────────────────────────────────────────────────────────────────────────

from typing import Any  # noqa: E402

from brutex.config.feed_scope import SCOPE_FULL, FeedScope, resolve_feed_scope  # noqa: E402

_R11_FULL_SCOPE = FeedScope(
    feed="test", token=SCOPE_FULL, admitted_underlyings=None, fno_underlyings=None
)
#: The route-shaped sid map — post-prune the 4-row store maps every INDEX sid.
_R11_SID_MAP = {"13": "NIFTY", "25": "BANKNIFTY", "1": "SENSEX", "21": "INDIAVIX"}


class _CountingWindowCache:
    """A dict-backed WindowCountsCache spy — counts distinct computes."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, int], Any] = {}
        self.computes = 0
        self.keys: list[str] = []

    def get_or_compute(self, key: str, *, generation: int, compute: Any) -> Any:
        slot = (key, generation)
        if slot not in self.data:
            self.computes += 1
            self.keys.append(key)
            self.data[slot] = compute()
        return self.data[slot]


def _seed_mixed_scope_ledger(tmp_path: Path) -> None:
    """Spot + modern-F&O + legacy windows for BOTH in- and out-of-scope names."""
    ledger = WindowLedger()
    # Spot: an admitted index + a stored-only stock.
    ledger.record(
        _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
    )
    ledger.record(
        _window_key("RELIANCE"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
    )
    # Modern F&O contract windows (security_id = the vendor contract symbol).
    for contract in ("NSE-NIFTY-25Jan24-FUT", "NSE-RELIANCE-25Jan24-FUT"):
        ledger.record(
            WindowKey(
                market=GROWW_FUTURE_COVERAGE_MARKET,
                security_id=contract,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            ),
            raw_bars=3,
            error_type=None,
            bars_appended=3,
            now=_WINDOW_NOW,
        )
    # Legacy sid|segment option windows: an admitted-index sid + a stock sid
    # the pruned map no longer knows (→ legacy_unresolved).
    for sid in ("25|NSE_FNO", "500325|NSE_FNO"):
        ledger.record(
            _option_window_key(security_id=sid),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=_WINDOW_NOW,
        )
    save_window_ledger(ledger, tmp_path)


class TestWindowPayloadReadScope:
    """R11 — the census narrows; the /db raw grid + defaulted callers never do."""

    def test_python_fold_filters_and_counts(self, tmp_path: Path) -> None:
        """[FB] stock spot + stock-F&O + unmapped-legacy windows excluded + counted."""
        _seed_mixed_scope_ledger(tmp_path)
        payload = build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, scope=resolve_feed_scope("groww")
        )
        symbols = {w["symbol"] for w in payload["windows"]}
        assert "RELIANCE" not in symbols
        assert not any("RELIANCE" in s for s in symbols)
        assert "NIFTY" in symbols
        assert any("BANKNIFTY" in s for s in symbols)  # legacy index sid KEPT via the map
        assert payload["count"] == 3  # NIFTY spot + NIFTY FUT + the legacy 25| window
        assert payload["out_of_scope"] == {
            "windows": 3,
            "legacy_unresolved": 1,
            "dec": "DEC-UNIVERSE-INDEX-ONLY-001",
        }

    def test_duckdb_path_equals_python_fold(self, tmp_path: Path) -> None:
        """The SQL≡Python equivalence pin over the fixture corpus (§6#8)."""
        _seed_mixed_scope_ledger(tmp_path)
        scope = resolve_feed_scope("groww")
        python_payload = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope)
        duck_payload = build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope, conn=duckdb.connect()
        )
        assert duck_payload["counts"] == python_payload["counts"]
        assert duck_payload["count"] == python_payload["count"]
        assert duck_payload["matched"] == python_payload["matched"]
        assert duck_payload["out_of_scope"] == python_payload["out_of_scope"]
        assert {w["symbol"] for w in duck_payload["windows"]} == {
            w["symbol"] for w in python_payload["windows"]
        }

    def test_default_none_serves_everything_no_census_key(self, tmp_path: Path) -> None:
        """Law 0: a defaulted caller (scope=None) is the unfiltered legacy payload."""
        _seed_mixed_scope_ledger(tmp_path)
        for conn in (None, duckdb.connect()):
            payload = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP, conn=conn)
            assert "out_of_scope" not in payload
            assert payload["count"] == 6
            assert any(w["symbol"] == "RELIANCE" for w in payload["windows"])

    def test_unbounded_scope_is_pass_through(self, tmp_path: Path) -> None:
        _seed_mixed_scope_ledger(tmp_path)
        wide = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP, scope=_R11_FULL_SCOPE)
        default = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP)
        assert wide == default

    def test_whitespace_padded_admitted_key_sql_python_parity(self, tmp_path: Path) -> None:
        """[FB] an ASCII-whitespace-padded admitted spot key (``"NIFTY\\t"`` —
        the #907/#911 drift family generalized past trailing spaces) is
        ADMITTED by BOTH the Python fold and the DuckDB fast path: DuckDB's
        one-arg ``TRIM`` strips spaces ONLY, so pre-fix the SQL path
        excluded+counted the tab-padded key the Python ``str.strip()`` twin
        admitted — violating the bind's one-directional (admit-side-wider)
        envelope. The two-arg ASCII-whitespace ``TRIM`` restores parity."""
        ledger = WindowLedger()
        for key in ("NIFTY\t", " NIFTY \r\n", "RELIANCE"):
            ledger.record(
                _window_key(key), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
            )
        save_window_ledger(ledger, tmp_path)
        scope = resolve_feed_scope("groww")
        python_payload = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope)
        duck_payload = build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope, conn=duckdb.connect()
        )
        for payload in (python_payload, duck_payload):
            assert payload["count"] == 2  # both padded NIFTY forms admitted
            assert payload["out_of_scope"] == {
                "windows": 1,  # RELIANCE only — never a padded admitted key
                "legacy_unresolved": 0,
                "dec": "DEC-UNIVERSE-INDEX-ONLY-001",
            }
        assert duck_payload["counts"] == python_payload["counts"]

    def test_index_windows_byte_identical_under_scope(self, tmp_path: Path) -> None:
        """An index-only ledger under the bounded scope: payload byte-identical."""
        ledger = WindowLedger()
        ledger.record(
            _window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_WINDOW_NOW
        )
        save_window_ledger(ledger, tmp_path)
        scoped = build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, scope=resolve_feed_scope("groww")
        )
        default = build_window_payload(tmp_path, symbol_for_id=_R11_SID_MAP)
        assert scoped == default

    def test_multipage_stock_dominated_pages_with_no_holes(self, tmp_path: Path) -> None:
        """Σ page rows == matched — the offset pager never blank-walks (§6#8)."""
        ledger = WindowLedger()
        for i in range(37):
            ledger.record(
                _window_key(f"STOCK{i:03d}"),
                raw_bars=2,
                error_type=None,
                bars_appended=2,
                now=_WINDOW_NOW,
            )
        for symbol in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "NSE-NIFTY"):
            ledger.record(
                _window_key(symbol), raw_bars=2, error_type=None, bars_appended=2, now=_WINDOW_NOW
            )
        save_window_ledger(ledger, tmp_path)
        scope = resolve_feed_scope("groww")
        conn = duckdb.connect()
        first = build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope, conn=conn, limit=2
        )
        assert first["matched"] == 5  # the 4 admitted + the recovered NSE-NIFTY drift form
        assert first["out_of_scope"]["windows"] == 37
        collected: list[str] = []
        offset = 0
        while True:
            page = build_window_payload(
                tmp_path, symbol_for_id=_R11_SID_MAP, scope=scope, conn=conn, limit=2, offset=offset
            )
            if not page["windows"]:
                break
            collected.extend(w["symbol"] for w in page["windows"])
            offset += 2
        assert len(collected) == first["matched"]
        assert set(collected) == {"NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "NSE-NIFTY"}

    def test_phase_progress_shares_one_scoped_counts_entry(self, tmp_path: Path) -> None:
        """The two-route consistency pin: ONE cached entry, EQUAL counts (§6#8)."""
        from brutex.data.window_ledger_store import ledger_path

        _seed_mixed_scope_ledger(tmp_path)
        scope = resolve_feed_scope("groww")
        cache = _CountingWindowCache()
        conn = duckdb.connect()
        payload = build_window_payload(
            tmp_path,
            symbol_for_id=_R11_SID_MAP,
            scope=scope,
            conn=conn,
            count_cache=cache,
            limit=0,
        )
        phases = phase_progress_payload(
            conn,
            ledger_path(tmp_path),
            current_phase="",
            count_cache=cache,
            scope=scope,
            symbol_for_id=_R11_SID_MAP,
        )
        # ONE shared counts entry — the second route was a cache HIT.
        assert cache.computes == 1
        assert len(set(cache.keys)) == 1
        by_phase = {row["phase"]: row for row in phases}
        counts = payload["counts"]
        for bucket in ("spot", "futures", "options"):
            expected_total = sum(counts[bucket].values())
            assert by_phase[bucket]["total"] == expected_total

    def test_defaulted_caller_keys_separately_from_threaded(self, tmp_path: Path) -> None:
        """The Rev-5 key law: unfiltered vs scoped computes can never share an entry."""
        _seed_mixed_scope_ledger(tmp_path)
        cache = _CountingWindowCache()
        conn = duckdb.connect()
        build_window_payload(
            tmp_path, symbol_for_id=_R11_SID_MAP, conn=conn, count_cache=cache, limit=0
        )
        build_window_payload(
            tmp_path,
            symbol_for_id=_R11_SID_MAP,
            scope=resolve_feed_scope("groww"),
            conn=conn,
            count_cache=cache,
            limit=0,
        )
        assert cache.computes == 2
        assert len(set(cache.keys)) == 2
        unfiltered_keys = [k for k in cache.keys if k.endswith("|unfiltered")]
        assert len(unfiltered_keys) == 1

    def test_stock_planned_population_no_longer_pins_futures_phase(self, tmp_path: Path) -> None:
        """The fno_current_phase_from_counts fixture arm (§6#8): a residual
        stock futures PLANNED population reads phantom pending at merge-base
        (scope=None parity arm) and no pending at head under the scope."""
        from brutex.data.window_ledger_store import ledger_path

        ledger = WindowLedger()
        ledger.plan(
            WindowKey(
                market=GROWW_FUTURE_COVERAGE_MARKET,
                security_id="NSE-RELIANCE-25Jan24-FUT",
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            ),
            now=_WINDOW_NOW,
        )
        save_window_ledger(ledger, tmp_path)
        conn = duckdb.connect()
        unfiltered = phase_progress_payload(
            conn, ledger_path(tmp_path), current_phase="", count_cache=None
        )
        scoped = phase_progress_payload(
            conn,
            ledger_path(tmp_path),
            current_phase="",
            count_cache=None,
            scope=resolve_feed_scope("groww"),
            symbol_for_id=_R11_SID_MAP,
        )
        unfiltered_fut = next(r for r in unfiltered if r["phase"] == "futures")
        scoped_fut = next(r for r in scoped if r["phase"] == "futures")
        assert unfiltered_fut["pending"] > 0  # merge-base parity: phantom pending
        assert scoped_fut["pending"] == 0

    def test_since_counts_ride_the_same_admit_filter(self, tmp_path: Path) -> None:
        _seed_mixed_scope_ledger(tmp_path)
        conn = duckdb.connect()
        payload = build_window_payload(
            tmp_path,
            symbol_for_id=_R11_SID_MAP,
            scope=resolve_feed_scope("groww"),
            conn=conn,
            since="2020-01-01T00:00:00+00:00",
            limit=0,
        )
        since_counts = payload["counts_since_pull_start"]
        assert since_counts == payload["counts"]  # every seeded row is post-since + in scope


class TestWindowScopeBindEdges:
    """R11 bind-edge coverage: empty admitted sets + the records fallback."""

    def test_empty_bounded_scope_admits_nothing_sql_equals_python(self, tmp_path: Path) -> None:
        """A bounded scope with EMPTY admitted sets + no sid map: every window
        excluded; legacy rows (no known sids at all) read legacy_unresolved."""
        _seed_mixed_scope_ledger(tmp_path)
        empty_scope = FeedScope(
            feed="test",
            token="index4",
            admitted_underlyings=frozenset(),
            fno_underlyings=frozenset(),
        )
        duck = build_window_payload(tmp_path, scope=empty_scope, conn=duckdb.connect())
        python_fold = build_window_payload(tmp_path, scope=empty_scope)
        assert duck["count"] == 0
        assert duck["windows"] == []
        assert duck["out_of_scope"] == {
            "windows": 6,
            "legacy_unresolved": 2,
            "dec": "DEC-UNIVERSE-INDEX-ONLY-001",
        }
        assert python_fold["out_of_scope"] == duck["out_of_scope"]
        assert python_fold["counts"] == duck["counts"]

    def test_scoped_since_counts_ride_the_cache(self, tmp_path: Path) -> None:
        """The scoped since arm shares the counts cache (isolated compute twin)."""
        _seed_mixed_scope_ledger(tmp_path)
        cache = _CountingWindowCache()
        payload = build_window_payload(
            tmp_path,
            symbol_for_id=_R11_SID_MAP,
            scope=resolve_feed_scope("groww"),
            conn=duckdb.connect(),
            count_cache=cache,
            since="2020-01-01T00:00:00+00:00",
            limit=0,
        )
        assert payload["counts_since_pull_start"] == payload["counts"]
        assert cache.computes == 2  # ONE lifetime + ONE since entry
        assert all("|s=" in key or key.endswith("|unfiltered") for key in cache.keys)

    def test_phase_records_fallback_applies_the_scope_filter(self, tmp_path: Path) -> None:
        """The degraded phases never widen past the census (R11 fallback arm)."""
        from brutex.data.window_ledger_store import ledger_path

        _seed_mixed_scope_ledger(tmp_path)
        ledger_file = ledger_path(tmp_path)
        deltas = ledger_file.parent / "deltas"
        deltas.mkdir(exist_ok=True)
        # A junk pending delta: keeps the deltas-pending branch alive (the
        # records fold skips the bad delta file-granularly).
        (deltas / "00000001-1.parquet").write_bytes(b"not-a-parquet")

        class _Boom:
            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise duckdb.Error("boom")

        phases = phase_progress_payload(
            _Boom(),
            ledger_file,
            current_phase="",
            count_cache=None,
            retry_sleep=lambda _s: None,
            scope=resolve_feed_scope("groww"),
            symbol_for_id=_R11_SID_MAP,
        )
        by_phase = {row["phase"]: row for row in phases}
        # RELIANCE spot + the stock FUT + the unmapped legacy sid are filtered
        # out of the degraded fold too: 1 spot / 1 futures / 1 options remain.
        assert by_phase["spot"]["total"] == 1
        assert by_phase["futures"]["total"] == 1
        assert by_phase["options"]["total"] == 1


class TestWindowStatusLabelProvenance:
    """DEC-STREAK-PROVENANCE-001 — the 4th (streak_provenance) param: mixed-evidence
    settles render per channel; legacy None + pure-channel lineages BYTE-IDENTICAL."""

    # -- legacy None: byte-identical on EVERY branch (the back-compat pin) -----

    @pytest.mark.parametrize(
        ("status", "streak", "last_error"),
        [
            ("EMPTY", 2, None),
            ("SKIPPED", 3, None),
            ("SKIPPED", 3, "ALL_FILTERED"),
            ("FAILED", 1, "ALL_FILTERED"),
            ("SUCCESS", 0, None),
            ("PLANNED", 0, None),
        ],
    )
    def test_legacy_none_provenance_is_byte_identical(
        self, status: str, streak: int, last_error: str | None
    ) -> None:
        assert window_status_label(status, streak, last_error, None) == window_status_label(
            status, streak, last_error
        )

    def test_legacy_none_expired_stamp_verbatim(self) -> None:
        from brutex.data.window_ledger import expired_window_settle_reason

        stamp = expired_window_settle_reason(2)
        assert window_status_label("SKIPPED", 3, stamp, None) == f"{stamp} — settled"

    # -- pure-channel lineages: byte-identical strings (edge 23) ---------------

    def test_pure_clean_lineage_renders_byte_identical(self) -> None:
        prov = "EMPTY@a|EMPTY@b|EMPTY@c"
        assert window_status_label("SKIPPED", 3, None, prov) == (
            "vendor confirmed no trades (3 clean attempt(s) — settled)"
        )
        assert window_status_label("EMPTY", 2, None, "EMPTY@a|EMPTY@b") == (
            "vendor confirmed no trades (2 clean attempt(s))"
        )

    def test_pure_flagged_lineage_renders_byte_identical(self) -> None:
        prov = "ALL_FILTERED@a|ALL_FILTERED@b|ALL_FILTERED@c"
        assert window_status_label("SKIPPED", 3, "ALL_FILTERED", prov) == (
            "completeness gap accepted (ALL_FILTERED, 3 flagged attempt(s)) — settled"
        )

    # -- mixed-evidence settles: the honest per-channel split (edge 22) --------

    def test_mixed_clean_final_names_the_flagged_attempts(self) -> None:
        # ALL_FILTERED,ALL_FILTERED,EMPTY → SKIPPED with last_error=None: was the
        # lie "3 clean attempt(s)"; now names the 2 boundary-filter demotions.
        prov = "ALL_FILTERED@a|ALL_FILTERED@b|EMPTY@c"
        assert window_status_label("SKIPPED", 3, None, prov) == (
            "vendor confirmed no trades (1 clean attempt(s); 2 completeness-flagged) — settled"
        )

    def test_mixed_flagged_final_names_the_clean_attempts(self) -> None:
        # EMPTY,EMPTY,ALL_FILTERED → SKIPPED with last_error=ALL_FILTERED: was
        # "3 flagged attempt(s)"; now names the 2 clean empties.
        prov = "EMPTY@a|EMPTY@b|ALL_FILTERED@c"
        assert window_status_label("SKIPPED", 3, "ALL_FILTERED", prov) == (
            "completeness gap accepted (ALL_FILTERED, 1 flagged + 2 clean attempt(s)) — settled"
        )

    def test_mixed_empty_status_names_the_flagged_attempts(self) -> None:
        prov = "ALL_FILTERED@a|EMPTY@b"
        assert window_status_label("EMPTY", 2, None, prov) == (
            "vendor confirmed no trades (1 clean attempt(s); 1 completeness-flagged)"
        )

    def test_expired_stamp_wins_over_provenance(self) -> None:
        # The stamp is provenance-honest at WRITE time — the label renders it
        # verbatim even when a mixed lineage rides the row.
        from brutex.data.window_ledger import expired_window_settle_reason

        stamp = expired_window_settle_reason(1)
        prov = "ALL_FILTERED@a|EMPTY@b"
        assert window_status_label("SKIPPED", 3, stamp, prov) == f"{stamp} — settled"

    def test_known_zero_lineage_floors_like_today(self) -> None:
        # "" = KNOWN zero bumps (a fresh reset) — the defensive floor keeps the
        # wording at 1, exactly as the streak floor does today.
        assert window_status_label("EMPTY", 0, None, "") == (
            "vendor confirmed no trades (1 clean attempt(s))"
        )
        assert window_status_label("SKIPPED", 0, None, "") == (
            "vendor confirmed no trades (1 clean attempt(s) — settled)"
        )
        assert window_status_label("SKIPPED", 0, "ALL_FILTERED", "") == (
            "completeness gap accepted (ALL_FILTERED, 1 flagged attempt(s)) — settled"
        )


class TestWindowRowStreakProvenance:
    """``_window_row`` threads the provenance into the label + the JSON row —
    the ONE chokepoint both /coverage/windows payload paths share."""

    def test_window_row_carries_and_threads_the_field(self, tmp_path: Path) -> None:
        from datetime import UTC, date, datetime

        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        ledger = WindowLedger()
        key = WindowKey(
            market="dhan",
            security_id="MIXED",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
        )
        now = datetime(2026, 6, 9, tzinfo=UTC)
        for pull, (degraded, reason) in enumerate(
            [(True, "ALL_FILTERED"), (True, "ALL_FILTERED"), (False, None)]
        ):
            ledger.record(
                key,
                raw_bars=3 if degraded else 0,
                error_type=None,
                bars_appended=0,
                now=now,
                degraded=degraded,
                degraded_reason=reason,
                pull_id=f"pl{pull}",
            )
        rec = ledger.records()[0]
        assert rec.status == "SKIPPED"  # the mixed-evidence threshold settle
        save_window_ledger(ledger, tmp_path)
        # BOTH payload paths render the SAME honest mixed label + carry the field.
        fast = build_window_payload(tmp_path, conn=duckdb.connect())
        legacy = build_window_payload(tmp_path)
        for payload in (fast, legacy):
            (row,) = payload["windows"]
            assert row["streak_provenance"] == "ALL_FILTERED@pl0|ALL_FILTERED@pl1|EMPTY@pl2"
            assert row["status_label"] == (
                "vendor confirmed no trades (1 clean attempt(s); 2 completeness-flagged) — settled"
            )

    def test_duckdb_fast_path_reads_a_legacy_file_without_error(self, tmp_path: Path) -> None:
        # Edge 29: a post-R1 file that PREDATES streak_provenance (has
        # empty_streak + in_pull_fetches, so the fast path does NOT defer) —
        # the missing column reads None (legacy label), never a SELECT error.
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.window_ledger_store import ledger_path

        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "market": ["dhan"],
                    "security_id": ["LEGACY"],
                    "interval": ["1minute"],
                    "window_start": ["2021-01-01"],
                    "window_end": ["2021-02-01"],
                    "status": ["SKIPPED"],
                    "bars": [0],
                    "attempts": [3],
                    "last_error": ["ALL_FILTERED"],
                    "first_seen_at": ["2026-06-08T12:00:00+00:00"],
                    "last_attempt_at": ["2026-06-08T12:00:00+00:00"],
                    "empty_streak": [3],
                    "in_pull_fetches": [1],
                    "bars_net_new": [None],
                }
            ),
            path,
        )
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        assert payload["source"] == "ledger"
        (row,) = payload["windows"]
        assert row["streak_provenance"] is None  # honest unknown, never ""
        # The legacy wording, byte-identical (counts is None → streak fallback).
        assert row["status_label"] == (
            "completeness gap accepted (ALL_FILTERED, 3 flagged attempt(s)) — settled"
        )

    def test_duckdb_fast_path_reads_a_new_file_with_the_column(self, tmp_path: Path) -> None:
        from datetime import UTC, date, datetime

        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        ledger = WindowLedger()
        key = WindowKey(
            market="dhan",
            security_id="NEWROW",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
        )
        ledger.record(
            key,
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=datetime(2026, 6, 9, tzinfo=UTC),
            pull_id="px",
        )
        save_window_ledger(ledger, tmp_path)
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        (row,) = payload["windows"]
        assert row["streak_provenance"] == "EMPTY@px"
