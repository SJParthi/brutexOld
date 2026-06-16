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

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.dashboard.db_view import (
    KNOWN_TABLES,
    _json_safe,
    build_window_payload,
    clamp_limit,
    clamp_offset,
    format_clean_ist_millis,
    sample_rows,
    table_summary,
)
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.session import IST
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.data.window_status import WindowStatusLog
from brutex.data.window_status_store import save_window_status
from brutex.models import Exchange, InstrumentType, Segment


def test_format_clean_ist_millis_drops_offset_and_trims_to_ms() -> None:
    """A UTC instant renders clean IST without +05:30, trimmed to milliseconds.

    DEC-LIVE-DASH-PROBE-001 — the live-table timestamp formatter: aware UTC →
    IST wall-clock with the µs sub-second trimmed to ms; a naive datetime is read as
    already-wall-clock and rendered as-is (no conversion).
    """
    aware = datetime(2026, 6, 15, 10, 4, 41, 335741, tzinfo=UTC)
    rendered = format_clean_ist_millis(aware)
    assert rendered == "2026-06-15 15:34:41.335"
    assert "+05:30" not in rendered
    # A naive datetime (no tzinfo) is rendered verbatim (no astimezone conversion).
    naive = datetime(2026, 6, 15, 15, 34, 41, 335741)
    assert format_clean_ist_millis(naive) == "2026-06-15 15:34:41.335"


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
    # The legacy field names are preserved so the existing UI keeps working.
    assert set(by_symbol["NIFTY"]) == {
        "market",
        "symbol",
        "interval",
        "window_start",
        "window_end",
        "status",
        "bars",
        "attempts",
        "empty_streak",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
    }
    # R1 visibility (DEC-EMPTY-STREAK-VISIBLE-001): the consecutive-EMPTY
    # streak ships per row and the promotion bound ships once at the top.
    assert by_symbol["NIFTY"]["empty_streak"] == 0
    assert payload["empty_threshold"] == 3


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
        # SUCCESS + SKIPPED + EMPTY → resolved; PLANNED → pending; FAILED → failed
        # (DEC-EMPTY-RESOLVED-REPORTING-001 — EMPTY is no-data-RESOLVED, not pending).
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
            # resolved=3 (SUCCESS+SKIPPED+EMPTY), pending=1 (PLANNED only), failed=0
            ("NIFTY", "1minute"): (3, 1, 0),
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
