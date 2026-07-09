"""Integration: the F&O candle lake is first-class in ``/db`` (DEC-FNO-CANDLE-LAKE-001).

Proven end-to-end against a real lake written by the production
:class:`~brutex.data.sink.ParquetBarSink` + a real
:class:`~brutex.data.instrument_store.InstrumentStore`:

* ``GET /api/v1/db/candles/datasets`` returns the F&O sets under ``fno`` (decoded per
  contract) + the documented ``fno_schema``, and the spot ``datasets`` list EXCLUDES
  the F&O contracts (the honesty fix — an F&O bar is not spot).
* an F&O contract is queryable through the IDENTICAL ``segment="FNO"`` candle path, with
  ``open_interest`` populated (first-class for F&O).
* the F&O candle lake is registered in the master ``schema_meta`` at store-open and is
  browsable via the ``/db`` table reader.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_CE = "NSE-NIFTY-02Jan25-24000-CE"
_FUT = "NSE-NIFTY-30Jan25-FUT"


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
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


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2026, 6, 1), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


def _write(lake: Path, *, segment: str, symbol: str, oi: int | None) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=oi,
    )
    sink.append(exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=[bar])


class TestFnoCandleDatasetsEndpoint:
    def test_fno_listed_decoded_and_excluded_from_spot(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12000)
        _write(tmp_path, segment="FNO", symbol=_FUT, oi=9000)
        _write(tmp_path, segment="CASH", symbol="NSE-NIFTY", oi=None)
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        # spot list is honestly spot-only — the FNO contracts are NOT here
        spot_symbols = {d["symbol"] for d in body["datasets"]}
        assert spot_symbols == {"NSE-NIFTY"}
        # the F&O lake is its own first-class, decoded surface
        fno = {d["symbol"]: d for d in body["fno"]}
        assert set(fno) == {_CE, _FUT}
        assert fno[_CE]["underlying"] == "NIFTY"
        assert fno[_CE]["strike"] == "24000"
        assert fno[_CE]["option_type"] == "CE"
        assert fno[_FUT]["instrument_type"] == "FUT"
        assert fno[_FUT]["strike"] is None
        assert [c["name"] for c in body["fno_schema"]][:7] == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_fno_contract_queryable_with_open_interest(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12345)
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        page = client.get(
            "/api/v1/db/candles",
            params={"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"},
        ).json()
        assert page["total"] == 1
        assert "open_interest" in page["columns"]
        oi_idx = page["columns"].index("open_interest")
        assert page["rows"][0][oi_idx] == 12345


class TestSchemaMetaRegistration:
    def test_fno_candle_lake_registered_and_browsable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        page = client.get("/api/v1/db/table/schema_meta", params={"limit": 500}).json()
        key_idx = page["columns"].index("key")
        val_idx = page["columns"].index("value")
        registered = {r[key_idx]: r[val_idx] for r in page["rows"]}
        assert registered["fno_candle_lake.table"] == "fno_candles"
        assert registered["fno_candle_lake.segment"] == "FNO"
        # v3: the stored nullable Greeks overlay landed (DEC-GREEKS-ENGINE-PR2B-001).
        assert registered["fno_candle_lake.schema_version"] == "3"


class TestFnoCandleRegistryStep:
    """DEC-MONEYNESS-REGISTRY-STEP-001 — the /db candle route threads the registry.

    The route passes ``registry_path_for(store.path)`` into ``query_candles`` so an
    F&O page's ATM±N strike-grid step prefers the AUTHORITATIVE vendor chain, and the
    payload carries the ``strike_step_confidence`` honesty label either way.
    """

    _CE_24100 = "NSE-NIFTY-02Jan25-24100-CE"

    def _seed_sparse(self, tmp_path: Path) -> None:
        # Only alternate strikes pulled (observed gap 100 — 2× the true 50 grid).
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12000)
        _write(tmp_path, segment="FNO", symbol=self._CE_24100, oi=9000)

    def _seed_registry(self, tmp_path: Path) -> None:
        from brutex.data.registry_store import RegistryStore

        now = datetime(2025, 1, 1, tzinfo=UTC)
        with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
            for strike in (24000.0, 24050.0, 24100.0):
                reg.upsert_contract(
                    "NSE",
                    "NIFTY",
                    date(2025, 1, 2),
                    f"NSE-NIFTY-02Jan25-{int(strike)}-CE",
                    side="CE",
                    strike=strike,
                    run_id="r1",
                    fetched_at=now,
                )

    def _page(self, store: InstrumentStore) -> dict[str, object]:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        return client.get(
            "/api/v1/db/candles",
            params={"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"},
        ).json()

    def test_without_registry_the_page_is_honestly_low_confidence(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        self._seed_sparse(tmp_path)
        page = self._page(store)
        assert page["strike_step_confidence"] == "observed"

    def test_registry_chain_reaches_the_route(self, store: InstrumentStore, tmp_path: Path) -> None:
        # The registry sits beside the instrument store (registry_path_for), so the
        # route-threaded chain flips the page to high confidence end-to-end.
        self._seed_sparse(tmp_path)
        self._seed_registry(tmp_path)
        page = self._page(store)
        assert page["strike_step_confidence"] == "registry"

    def test_db_template_renders_the_low_confidence_marker(self) -> None:
        # The frontend honesty marker (a small muted note keyed off the payload
        # field) is present in the served template — display-only, string-pinned.
        import brutex.dashboard as dashboard_pkg

        template = (Path(dashboard_pkg.__file__).parent / "templates" / "db.html").read_text(
            encoding="utf-8"
        )
        assert 'strike_step_confidence === "observed"' in template
        assert "stepnote" in template


class TestFnoDisplayTruthPack:
    """DEC-DB-FNO-DISPLAY-TRUTH-001 — the /db F&O display-truth pack, route +
    template surfaces (items 3 / 9 / 10)."""

    def test_greeks_status_words_reach_the_route(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Item 3 end-to-end: an enriched below-intrinsic bar's stored NULL reason
        # renders as WORDS through the real route — never a bare "null" beside an
        # opaque packed int.
        from brutex.data.greeks_provenance import (
            RATE_SOURCE_ID_FIXED,
            STATUS_BELOW_INTRINSIC,
            STYLE_EUROPEAN_EXACT,
            pack_greeks_provenance,
        )
        from brutex.data.models import FnoGreeksBar

        bar = FnoGreeksBar(
            timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
            open=Decimal("100.10"),
            high=Decimal("101.20"),
            low=Decimal("99.50"),
            close=Decimal("100.80"),
            volume=375,
            open_interest=12000,
            greeks_provenance_id=pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                rate_version=1,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_BELOW_INTRINSIC,
            ),
        )
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        sink.append(exchange="NSE", segment="FNO", symbol=_CE, interval="1minute", bars=[bar])
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        page = client.get(
            "/api/v1/db/candles",
            params={"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"},
        ).json()
        idx = page["columns"].index("greeks_status")
        assert page["rows"][0][idx] == "below intrinsic — no unique IV"
        # Item 9 provenance key rides every F&O page (a 1minute page joined — or
        # not — at 1minute; here no spot exists, so the honest None).
        assert "spot_join_interval" in page

    def test_greeks_status_advertised_in_the_fno_schema(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Item 3: the datasets endpoint's advertised schema carries the ADDITIVE
        # derived column (no version bump — the bump law is non-additive-only).
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12000)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/db/candles/datasets").json()
        schema = {c["name"]: c for c in body["fno_schema"]}
        assert schema["greeks_status"]["source"] == "derived"

    def test_db_template_renders_the_spot_join_fallback_note(self) -> None:
        # Item 9 frontend (substring-presence envelope — the #1529/#1530 accepted
        # precedent; no headless JS runner).
        import brutex.dashboard as dashboard_pkg

        template = (Path(dashboard_pkg.__file__).parent / "templates" / "db.html").read_text(
            encoding="utf-8"
        )
        assert "underlying spot joined from the 1minute series" in template
        assert "spot_join_interval" in template
        assert "tfnote" in template
        # The tbody-patch gate requires the note's presence to be unchanged —
        # never a stale note across paging (the wantNote/wantContig idiom).
        assert 'grid.querySelector(".tfnote")' in template

    def test_db_template_marks_expired_dte_and_titles_the_spot_headers(self) -> None:
        # Item 10 frontend (substring-presence envelope): the negative
        # days_to_expiry "(expired)" marker + the per-tab <th title> provenance map.
        import brutex.dashboard as dashboard_pkg

        template = (Path(dashboard_pkg.__file__).parent / "templates" / "db.html").read_text(
            encoding="utf-8"
        )
        assert '(cell + " (expired)")' in template
        assert "const SPOT_TITLES = {" in template
        assert "derived at read time (not stored)" in template
        assert "stored spot used by the Greeks enrichment (per-bar stamp)" in template
        assert "stored per-bar spot stamp written at ingest" in template
        assert "function gridTheadHtml(cols, titles)" in template


def _db_template_text() -> str:
    import brutex.dashboard as dashboard_pkg

    return (Path(dashboard_pkg.__file__).parent / "templates" / "db.html").read_text(
        encoding="utf-8"
    )


def _js_function(template: str, header: str) -> str:
    """Extract one top-level ``function name(...) { ... }`` block from the template.

    Every extracted builder closes with the first column-0 ``}`` after its header
    (the db.html inline script's top-level layout) — a moved/renamed function fails
    LOUDLY here (ValueError), never a silently-empty extraction.
    """
    start = template.index(header)
    end = template.index("\n}\n", start)
    return template[start : end + 3]


class TestSpotJoinPerCellMarker:
    """DEC-DB-FNO-DISPLAY-TRUTH-001 item 9, review fold (M1) — the per-cell " ~".

    Full #1539 parity for the 1minute spot-join fallback: the page-level
    disclosure (spot_join_interval key / .tfnote / th title) gains a PER-CELL
    " ~" on the spot + moneyness cells whose provenance is the fallback join
    (bucket-OPEN 1-minute instant vs a prebuilt bucket's resampled close) —
    values MARKED, never changed. Two layers, mirroring
    ``test_dashboard_halfup_format.py``: string pins (the honest no-headless-JS
    envelope) + a node-executed golden over the REAL extracted builders
    (fails-before: pre-fold ``gridRowsHtml`` had no flag and emitted no marker).
    """

    def test_template_ships_the_marker_and_threads_the_flag(self) -> None:
        template = _db_template_text()
        # The marking line: spot + moneyness only; "?"/"—" carry no fallback-joined
        # value so they are honestly excluded; appended INSIDE the esc()'d string.
        assert (
            'if (spotJoinMark && (col === "spot" || col === "moneyness") '
            '&& cell !== "?" && cell !== "—") txt += " ~";'
        ) in template
        # The flag threads through BOTH render paths (full rebuild + tbody patch).
        assert "function gridRowsHtml(cols, rows, spotJoinMark)" in template
        assert "function gridAndPager(cols, rows, total, off, lim, note, titles, spotJoinMark)" in (
            template
        )
        assert "function patchGridRows(grid, cols, rows, total, off, spotJoinMark)" in template
        assert "tbody.innerHTML = gridRowsHtml(cols, rows, spotJoinMark);" in template
        assert "Number(body.total || 0), Number(body.offset || 0), wantTfNote);" in template
        # The legend explains the per-cell marker beside the existing page note.
        assert 'spot/moneyness cells marked " ~" carry the bar-instant 1-minute values' in template

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_node_executed_marker_present_on_fallback_absent_on_native(self) -> None:
        # The REAL template builders (esc/fmtCell/moneynessClass/gridRowsHtml +
        # their consts), executed in node over an FNO-shaped page. Fails-before:
        # the pre-fold gridRowsHtml ignored a third argument → no " ~" anywhere.
        template = _db_template_text()
        pct_cols = re.search(r"^const PCT_COLS = .*$", template, re.M)
        greek_dp = re.search(r"^const GREEK_DP = .*$", template, re.M)
        assert pct_cols is not None and greek_dp is not None
        source = "\n".join(
            [
                # bxFixed2 lives in the shared /static/bx-format.js (loaded before the
                # inline script in the browser); the marker path never reaches it — a
                # loud throwing stub keeps this golden honest about that claim.
                'const bxFixed2 = () => { throw new Error("PCT path not under test"); };',
                pct_cols.group(0),
                greek_dp.group(0),
                _js_function(template, "function esc(s)"),
                _js_function(template, "function fmtCell(col, cell)"),
                _js_function(template, "function moneynessClass(col, cell)"),
                _js_function(template, "function gridRowsHtml(cols, rows, spotJoinMark)"),
            ]
        )
        cols = [
            "timestamp",
            "close",
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
            "greeks_status",
        ]
        rows = [
            # a fallback-joined bar (spot present) — spot + moneyness get " ~"
            ["2025-01-02T09:15:00", 100.8, "OTM +1", "23950.00", "0.00", "100.80", 0, "ok"],
            # no spot at that instant — "?" carries no fallback value → NOT marked
            ["2025-01-02T09:16:00", 101.0, "?", None, None, None, 0, "not enriched"],
        ]
        script = (
            source
            + f"\nconst cols = {json.dumps(cols)};\nconst rows = {json.dumps(rows)};\n"
            + "console.log(JSON.stringify({"
            + "  marked: gridRowsHtml(cols, rows, true),"
            + "  native: gridRowsHtml(cols, rows),"
            + "  nativeFalse: gridRowsHtml(cols, rows, false),"
            + "}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        marked, native = out["marked"], out["native"]
        # PRESENT on the fallback page: the derived spot + moneyness cells only.
        assert '<td class="mny-otm">OTM +1 ~</td>' in marked
        assert '<td class="">23950.00 ~</td>' in marked
        # NOT on the value columns sharing the row (td-bounded — the spot cell's
        # own "…0.00 ~" tail must not satisfy a bare substring), never on "?".
        assert '<td class="">0.00</td>' in marked  # intrinsic, unmarked
        assert '<td class="">0.00 ~</td>' not in marked
        assert '<td class="">100.80</td>' in marked  # time_value, unmarked
        assert '<td class="">100.80 ~</td>' not in marked
        assert "? ~" not in marked
        # the null spot cell stays the plain nullcell (no marker path reached)
        assert '<td class="nullcell">null</td>' in marked
        # ABSENT on a native-interval page — flag omitted ≡ false ≡ pre-fold bytes.
        assert " ~" not in native
        assert native == out["nativeFalse"]
        assert '<td class="mny-otm">OTM +1</td>' in native
