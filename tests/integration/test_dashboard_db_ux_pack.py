"""Integration tests for the ``/db`` UX pack (DEC-DB-UX-PACK-001) — routes + frontend.

BACKEND (Feature 6): the ``/api/v1/db/fno/expiries`` route pairs each expiry's
vendor-LISTED count (the authoritative ``registry.duckdb`` calendar) with its
WITH-DATA count (contracts that truly have lake candles), so a young live chain is
self-explanatory. Exercised end-to-end against a real lake written by the production
:class:`~brutex.data.parquet_writer.ParquetWriter` + a real sibling
:class:`~brutex.data.registry_store.RegistryStore`, covering every graceful-degrade
path (no registry, registry that does not know the underlying, corrupt registry).

FRONTEND (Features 1/2/4/5/6): route tests over the SERVED pages/assets assert the
rendered HTML — the COLUMNS strips are gone on every ``/db`` tab (all columns always
visible, proven live via ``/api/v1/db/candles`` with no projection), the date filters
AUTO-APPLY on ``change`` (the bx-datepicker Go-to event finally has a consumer), an
empty date input renders NO date in the chip (the honest ``applied_range`` chip,
"no stored data" for an empty partition — never today), the ``/universe`` hero names
the REAL all-vendor-listed-strikes rule, and the F&O cascade renders the per-expiry
"listed N · with data M" chip. The dashboard JS has no unit harness, so substring
guards over the served HTML lock the wiring in CI (the repo's template-presence
convention — see ``test_db_page_wires_the_cascade_frontend``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.registry_store import RegistryStore
from fastapi.testclient import TestClient

_NOW = datetime(2025, 1, 2, 9, 15, tzinfo=UTC)


def _write_fno(lake: Path, symbol: str) -> None:
    """Write one F&O 1-minute bar to ``bars/NSE/FNO/<symbol>/1minute/…``."""
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        bars=[
            CandleBar(
                timestamp=_NOW,
                open=Decimal("100.10"),
                high=Decimal("101.20"),
                low=Decimal("99.50"),
                close=Decimal("100.80"),
                volume=375,
                open_interest=1000,
            )
        ],
    )


def _seed_registry(lake: Path, *, listed: int, underlying: str = "NIFTY") -> None:
    """Seed ``registry.duckdb`` with one 2025-01-02 expiry + *listed* NIFTY legs."""
    with RegistryStore.open(lake / "registry.duckdb") as reg:
        exp = date(2025, 1, 2)
        reg.upsert_expiry("NSE", underlying, exp, run_id="r1", fetched_at=_NOW)
        for i in range(listed):
            reg.upsert_contract(
                "NSE",
                underlying,
                exp,
                f"NSE-{underlying}-02Jan25-{24000 + i * 100}-CE",
                side="CE",
                strike=float(24000 + i * 100),
                run_id="r1",
                fetched_at=_NOW,
            )


def _client(lake: Path) -> TestClient:
    """A TestClient whose store path makes ``registry.duckdb`` a sibling of the lake."""
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=fake_store))


def test_expiry_counts_registry_present_young_chain(tmp_path: Path) -> None:
    # The vendor listed THREE strikes but only ONE has candle data yet →
    # "listed 3 with data 1" (the self-explanatory young-chain case).
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _seed_registry(lake, listed=3)
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiries"] == ["2025-01-02"]
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": 3, "with_data": 1}]


def test_expiry_counts_registry_absent_listed_is_null(tmp_path: Path) -> None:
    # No sibling registry.duckdb → an honest "with data M", listed is null.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-PE")
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": None, "with_data": 2}]


def test_expiry_counts_registry_does_not_know_underlying_listed_is_null(tmp_path: Path) -> None:
    # A registry that knows BANKNIFTY but NOT the queried NIFTY → listed null (never a
    # misleading "listed 0", Golden Rule #1) even though the registry file exists.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _seed_registry(lake, listed=2, underlying="BANKNIFTY")
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": None, "with_data": 1}]


def test_expiry_counts_corrupt_registry_degrades_to_null(tmp_path: Path) -> None:
    # A corrupt registry.duckdb (RegistryError on open) → listed null, never a 500.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    (lake).mkdir(parents=True, exist_ok=True)
    (lake / "registry.duckdb").write_bytes(b"not-a-duckdb-file \x00\x01 corrupt")
    resp = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    assert resp.json()["expiry_counts"] == [
        {"expiry": "2025-01-02", "listed": None, "with_data": 1}
    ]


# ════════════════════════════════════════════════════════════════════════
# Frontend wiring (DEC-DB-UX-PACK-001 Features 1/2/4/5/6) — the served pages
# ════════════════════════════════════════════════════════════════════════


def _bare_client() -> TestClient:
    """An app with no lake — the template/asset routes need no data."""
    return TestClient(build_app(DashboardState(), instrument_store=SimpleNamespace(path=None)))


def test_db_page_columns_strip_removed_on_all_tabs() -> None:
    """Feature 1: NO COLUMNS checkbox strip anywhere on /db — every column is
    always visible; the column-visibility JS/state is deleted, and no `column=`
    projection is ever emitted (omitting it returns all columns server-side)."""
    html = _bare_client().get("/db").text
    for gone in (
        'class="colbox"',  # the strip container (candle + table bars)
        ".colbox {",  # its stylesheet block
        'class="qcol"',  # the table-mode column checkboxes
        'class="ccol"',  # the candle-mode column checkboxes
        "curCols",  # the dead per-lake column-list helper
        "cstate.cols",  # the dead candle projection state
        "st.cols",  # the dead table projection state
        '"&column=" + encodeURIComponent',  # the projection param emission
        'id="capply"',  # the candle-bar Apply button (auto-apply now)
    ):
        assert gone not in html, f"the removed column picker leaked back: {gone!r}"
    # The always-all-columns grid path is intact.
    assert "function gridAndPager" in html


def test_db_date_filters_auto_apply_on_change() -> None:
    """Feature 2: the candle date inputs (and the sort select) AUTO-APPLY on
    `change` — the two-stage "pick a date, then ALSO click Apply query" trap is
    dead, and the bx-datepicker Go-to `change` dispatch finally has a consumer."""
    client = _bare_client()
    html = client.get("/db").text
    assert 'id="cstart"' in html and 'id="cend"' in html
    assert (
        "const autoApply = () => "
        "{ readCandleControls(); cstate.offset = 0; runCandleQuery(true); };"
    ) in html, "the auto-apply handler is missing"
    assert '["cstart", "cend", "cdir"].forEach' in html, (
        "the date inputs + sort select must all wire the auto-apply handler"
    )
    assert 'el.addEventListener("change", autoApply)' in html
    # The producer half of the pair: the shared picker's Go-to dispatches the
    # exact `change` event the /db wiring above consumes.
    js = client.get("/static/bx-datepicker.js").text
    assert 'dispatchEvent(new Event("change"))' in js


def test_db_applied_range_chip_rendered_from_payload_via_textcontent() -> None:
    """Feature 2: the active-filter chip is rendered from the payload's
    server-resolved `applied_range` (never from the inputs), reads "no stored
    data" for an empty partition, and is filled via textContent (CodeQL XSS
    guard) — an empty input can never render today's date as a fake filter."""
    html = _bare_client().get("/db").text
    assert 'id="crange"' in html, "the applied-range chip element is missing"
    assert "function appliedRangeText" in html
    assert "body.applied_range" in html, "the chip must read the payload's applied_range"
    assert '"no stored data"' in html, "an empty partition must read honestly"
    assert "rc.textContent = txt" in html, "the chip must be filled via textContent"


def test_datepicker_empty_input_renders_no_date_not_today() -> None:
    """Feature 2: an EMPTY backing date input renders NO date in the picker chip
    ("—"), never today's date — the inert-filter illusion is closed at the
    shared component, for both the single and the range picker."""
    js = _bare_client().get("/static/bx-datepicker.js").text
    assert 'fromInput.value ? fmt(start) : "—"' in js
    assert 'toInput.value ? fmt(end) : "—"' in js


def test_universe_hero_states_all_vendor_listed_strikes() -> None:
    """Feature 5: the /universe hero no longer claims a fixed ATM-fan pull; it
    states the REAL rule — ALL vendor-listed strikes (verified: CLAUDE.md §6,
    get_contracts returns every listed strike, not relative-to-spot)."""
    html = _bare_client().get("/universe").text
    assert "ATM±10" not in html, "the false ATM±10 fan claim must be gone"
    assert "ATM±3" not in html, "the false ATM±3 fan claim must be gone"
    assert "ALL vendor-listed strikes" in html
    assert "not relative-to-spot" in html
    assert "CLAUDE.md §6" in html  # the citation marker


def test_db_fno_expiry_chip_wired() -> None:
    """Feature 6: the F&O cascade consumes `expiry_counts` and renders the
    per-expiry "listed N · with data M" chip (+ per-option dropdown labels);
    a null `listed` renders the honest "with data M" alone."""
    html = _bare_client().get("/db").text
    assert "expiry_counts" in html, "the cascade must consume the counts payload"
    assert "function fnoExpiryCountsText" in html
    assert "function fnoExpiryChipHtml" in html
    assert 'id="cexpchip"' in html and 'class="expchip"' in html
    assert '"listed " + ' in html
    assert '" · with data " + m' in html
    assert '"with data " + m' in html  # the honest null-listed rendering


# ════════════════════════════════════════════════════════════════════════
# Feature 1 route guarantee — every column always arrives with NO projection
# (the server-side contract the strip removal rides on), plus the spec
# permutations at the ROUTE layer: empty lake, single bar, dup-poisoned
# partition must-not-500, page boundaries ascending AND descending.
# ════════════════════════════════════════════════════════════════════════

_SPOT_OHLCV = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]


@pytest.fixture
def store_client(tmp_path: Path):
    """An app over a REAL InstrumentStore (the candle routes need ``.connection``);
    the lake root is ``tmp_path`` (the store file's parent)."""
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        yield TestClient(build_app(DashboardState(), instrument_store=store))


def _write_spot(lake: Path, instants: list[datetime], symbol: str = "NSE-NIFTY") -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1minute",
        bars=[
            CandleBar(
                timestamp=t,
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=None,
            )
            for t in instants
        ],
    )


def _today_ist() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


def test_candles_route_single_bar_returns_every_column_without_projection(
    store_client: TestClient, tmp_path: Path
) -> None:
    # Single-bar permutation: no `column=` sent → EVERY stored column + the two
    # derived %-columns arrive, and the spot applied_range defaults honestly.
    _write_spot(tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)])
    body = store_client.get(
        "/api/v1/db/candles",
        params={
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        },
    ).json()
    assert body["total"] == 1 and len(body["rows"]) == 1
    assert set(body["columns"]) >= set(_SPOT_OHLCV)
    # DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001: the prev-close contiguity honesty
    # flag is appended after the two derived %-columns.
    assert body["columns"][-3:] == ["change_pct", "open_gap_pct", "prev_close_contiguous"]
    assert len(body["rows"][0]) == len(body["columns"])  # every column has a cell
    assert body["applied_range"] == {"start": "2020-01-01", "end": _today_ist()}


def test_fno_candles_route_returns_derived_columns_without_projection(
    store_client: TestClient, tmp_path: Path
) -> None:
    # The F&O twin: no projection → OHLCV+OI plus the five derived F&O columns,
    # and the applied_range is the contract's FULL STORED LIFE.
    _write_fno(tmp_path, "NSE-NIFTY-02Jan25-24000-CE")
    body = store_client.get(
        "/api/v1/db/candles",
        params={
            "exchange": "NSE",
            "segment": "FNO",
            "symbol": "NSE-NIFTY-02Jan25-24000-CE",
            "interval": "1minute",
        },
    ).json()
    assert set(body["columns"]) >= set(_SPOT_OHLCV)
    # DEC-DB-PREVCLOSE-CONTIGUITY-MARKER-001: prev_close_contiguous is appended
    # after the two spot derived %-columns, ahead of the six F&O derived columns
    # (incl. the decoded greeks_status — DEC-DB-FNO-DISPLAY-TRUTH-001).
    assert body["columns"][-9:] == [
        "change_pct",
        "open_gap_pct",
        "prev_close_contiguous",
        "moneyness",
        "spot",
        "intrinsic",
        "time_value",
        "days_to_expiry",
        "greeks_status",
    ]
    assert body["applied_range"] == {"start": "2025-01-02", "end": "2025-01-02"}


def test_candles_route_empty_partition_stable_columns_and_no_fake_today(
    store_client: TestClient,
) -> None:
    # Empty-lake permutation: a never-pulled F&O partition returns 200 with the
    # STABLE column set, zero rows, and an honest (null, null) applied_range —
    # the frontend chip renders "no stored data", never today.
    resp = store_client.get(
        "/api/v1/db/candles",
        params={
            "exchange": "NSE",
            "segment": "FNO",
            "symbol": "NSE-NIFTY-02Jan25-24000-CE",
            "interval": "1minute",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0 and body["rows"] == []
    assert set(body["columns"]) >= set(_SPOT_OHLCV)
    assert body["applied_range"] == {"start": None, "end": None}


def test_candles_route_page_boundaries_ascending_and_descending(
    store_client: TestClient, tmp_path: Path
) -> None:
    # Page-boundary permutation at the ROUTE layer, both sort directions.
    _write_spot(
        tmp_path,
        [
            datetime(2025, 1, 2, 3, 45, tzinfo=UTC),
            datetime(2025, 1, 2, 3, 46, tzinfo=UTC),
            datetime(2025, 1, 2, 3, 47, tzinfo=UTC),
        ],
    )
    client = store_client

    def _ts_col(desc: bool, offset: int) -> list[str]:
        body = client.get(
            "/api/v1/db/candles",
            params={
                "exchange": "NSE",
                "segment": "CASH",
                "symbol": "NSE-NIFTY",
                "interval": "1minute",
                "desc": str(desc).lower(),
                "limit": 2,
                "offset": offset,
            },
        ).json()
        idx = body["columns"].index("timestamp")
        return [row[idx] for row in body["rows"]]

    asc_p0, asc_p1 = _ts_col(False, 0), _ts_col(False, 2)
    assert len(asc_p0) == 2 and len(asc_p1) == 1  # 3 bars split 2+1
    assert asc_p0 == sorted(asc_p0) and asc_p0[-1] < asc_p1[0]  # ascending across pages
    desc_p0, desc_p1 = _ts_col(True, 0), _ts_col(True, 2)
    assert len(desc_p0) == 2 and len(desc_p1) == 1
    assert desc_p0 == sorted(desc_p0, reverse=True) and desc_p0[-1] > desc_p1[0]
    assert desc_p0 + desc_p1 == list(reversed(asc_p0 + asc_p1))  # same bars, mirrored


def test_candles_route_dup_poisoned_partition_never_500(
    store_client: TestClient, tmp_path: Path
) -> None:
    # Dup-poisoned permutation at the ROUTE layer: a legacy duplicate-timestamp
    # parquet (the pre-DEC-LAKE-WRITE-UNIQUE-001 poison shape) is HTTP 200, never
    # a 500 — and since main's DEC-LAKE-READ-UNIQUE-001 read-side dedup chokepoint
    # the page collapses the poison keep-FIRST (ONE row, DISTINCT-timestamp total),
    # so the viewer serves exactly what the deduped readers serve.
    import polars as pl

    part_dir = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute" / "2024"
    part_dir.mkdir(parents=True)
    ts = datetime(2024, 3, 1, 3, 45, tzinfo=UTC)
    pl.DataFrame(
        {
            "timestamp": [ts, ts],  # the dup poison
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 99.0],
            "close": [100.5, 100.5],
            "volume": [10, 10],
            "open_interest": [None, None],
        },
        schema={
            "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    ).write_parquet(part_dir / "03.parquet")
    resp = store_client.get(
        "/api/v1/db/candles",
        params={
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        },
    )
    assert resp.status_code == 200
    # DEC-LAKE-READ-UNIQUE-001: the dup collapses keep-first at read time.
    assert resp.json()["total"] == 1
