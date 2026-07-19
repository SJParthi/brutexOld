# pyright: reportPrivateUsage=none
"""Coverage backfill for the thin :mod:`brutex.dashboard.app` route glue.

Closes the residual line + branch gaps the route-family suites leave open
(100% line+branch gate, CLAUDE.md §2 Rule 12): the FastAPI-missing guard in
``build_app``, the no-lake-root degrade branches, the pull-all /
derive-timeframes selection forms (background job bodies replaced by offline
fakes — NEVER a real network call), the error→HTTP mapping arms of the db
viewer routes, and the WebSocket disconnect return.

Follows the established route-test pattern (``test_dashboard_credentials_route``
/ ``test_dashboard_e2e``): a real DuckDB-backed :class:`InstrumentStore` wired
into ``build_app`` + a ``TestClient``; skipped cleanly when the ``[ui]`` extra
(FastAPI) or duckdb is not installed.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.app import (
    DashboardState,
    _collect_store_stats,
    _make_ws_handler,
    _resolve_pull_lake_root,
    build_app,
)
from brutex.dashboard.db_view import ALLOWED_OPERATORS, KNOWN_TABLES
from brutex.data.instrument_store import InstrumentStore
from brutex.data.partition import VALID_INTERVALS
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

# The monkeypatch target for the route handlers' module-level
# collaborators (pull_candles / pull_all_spot / ...). Resolved via the
# module machinery so brutex.dashboard.app has ONE import style in this
# file (CodeQL import/import-from mixing — the test_dashboard_lazy_exports
# precedent).
app_module = importlib.import_module("brutex.dashboard.app")

# ────────────────────────────────────────────────────────────────────────
# Fixtures + fakes
# ────────────────────────────────────────────────────────────────────────


def _nifty() -> Instrument:
    """Build the admitted NIFTY index instrument fixture."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
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
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real DuckDB-backed store with one admitted index (NIFTY)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """A TestClient over the wired store; credentials redirected to an empty tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


class _NoLakeStore:
    """Wired-but-pathless store double for the no-lake-root degrade branches."""

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        """No F&O-eligible underlyings."""
        return frozenset()

    def list_index_equity_instruments(self) -> list[Instrument]:
        """No IDX/EQ candidates."""
        return []

    def reference_only_security_ids(self) -> frozenset[int]:
        """No reference-only ids."""
        return frozenset()


@pytest.fixture
def nolake_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient whose store is wired but carries NO lake ``path``."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=_NoLakeStore())) as c:
        yield c


class _FakeVerdict:
    """Stub verdict carrying the single method the background ``_work`` reads."""

    def to_dict(self) -> dict[str, Any]:
        """Minimal verdict payload for the banner slot."""
        return {"status": "clean"}


class _FakeReport:
    """Stub pull report — only ``.verdict`` is read on the job thread."""

    @property
    def verdict(self) -> _FakeVerdict:
        """The stub verdict."""
        return _FakeVerdict()


def _fake_pull_all_spot(instruments: list[Instrument], **kwargs: Any) -> _FakeReport:
    """Offline stand-in for the bulk spot pull (no network, returns at once)."""
    return _FakeReport()


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    """Poll /pull-progress until the background job settles (done/error)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background pull job did not settle within the timeout")


def _dry(params: dict[str, str] | None = None) -> dict[str, str]:
    """Return *params* + the EXPLICIT ``dry_run=true`` opt-in.

    DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 killed the silent creds-absent
    downgrade (the old ``_force_dry_run`` monkeypatch): synthetic pulls now
    require the explicit ``?dry_run=true`` param, which also short-circuits the
    credential gate — deterministic regardless of the ambient environment.
    """
    return {**(params or {}), "dry_run": "true"}


# ────────────────────────────────────────────────────────────────────────
# build_app guard + module-scope helpers
# ────────────────────────────────────────────────────────────────────────


def test_build_app_raises_helpful_error_without_fastapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_app`` maps a missing FastAPI dep to a RuntimeError naming the fix."""
    real_import = builtins.__import__

    def _no_fastapi(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fastapi":
            raise ImportError("No module named 'fastapi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastapi)
    with pytest.raises(RuntimeError) as excinfo:
        build_app(DashboardState())
    assert str(excinfo.value) == (
        "FastAPI is not installed. Install with `pip install "
        "fastapi uvicorn` to run the dashboard. Unit tests for the "
        "state model do not need FastAPI."
    )


def test_resolve_pull_lake_root_dry_run_placeholder_without_store() -> None:
    """The dry-run path tolerates a missing store, returning the placeholder root."""
    assert _resolve_pull_lake_root(None, dry_run=True) == Path.home() / ".brutex" / "lake"


def test_ws_handler_returns_on_disconnect() -> None:
    """The /ws push loop returns cleanly when the client disconnects mid-send."""
    sent: list[dict[str, Any]] = []

    class _DisconnectingWS:
        """Fake WebSocket whose first send raises WebSocketDisconnect."""

        async def accept(self) -> None:
            """Accept the handshake."""

        async def send_json(self, payload: dict[str, Any]) -> None:
            """Record the frame, then drop the connection."""
            sent.append(payload)
            raise WebSocketDisconnect(code=1000)

    handler = _make_ws_handler(DashboardState())
    assert asyncio.run(handler(_DisconnectingWS())) is None
    assert len(sent) == 1
    assert "phases" in sent[0]


def test_collect_store_stats_skips_option_series_without_lake_path(
    store: InstrumentStore,
) -> None:
    """Without a ``path`` on the store, the option-series tallies stay zero."""

    class _ConnOnlyStore:
        """Store double exposing only the DuckDB connection (no ``path``)."""

        @property
        def connection(self) -> Any:
            """The wrapped store's read connection."""
            return store.connection

    stats = _collect_store_stats(_ConnOnlyStore())
    assert stats["totals"]["instruments"] == 1
    assert stats["totals"]["expiries"] == 0
    assert stats["totals"]["contracts"] == 0


# ────────────────────────────────────────────────────────────────────────
# No-lake-root degrade branches (never a 500)
# ────────────────────────────────────────────────────────────────────────


def test_expiries_empty_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/expiries degrades to an empty list when no lake root is wired."""
    resp = nolake_client.get("/api/v1/expiries")
    assert resp.status_code == 200
    assert resp.json() == {"expiries": []}


def test_contracts_empty_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/contracts degrades to an empty list when no lake root is wired."""
    resp = nolake_client.get("/api/v1/contracts")
    assert resp.status_code == 200
    assert resp.json() == {"contracts": [], "limit": 500}


def test_index_constituents_scope_excluded_under_shipped_default(
    nolake_client: TestClient,
) -> None:
    """A5b (DEC-UNIVERSE-INDEX-ONLY-001): the shipped index4 default reads the
    dormancy payload — honest 200, zero network, BEFORE any cache/lake probe
    (the live picker never offers picks the P7 pull screens would refuse)."""
    resp = nolake_client.get("/api/v1/universe/index-constituents", params={"symbol": "NIFTY"})
    assert resp.status_code == 200
    assert resp.json() == {
        "members": [],
        "source": "scope_excluded",
        "as_of": None,
        "note": (
            "universe scope excludes stocks — index-constituent "
            "picking is dormant (DEC-UNIVERSE-INDEX-ONLY-001)"
        ),
    }


def test_index_constituents_unavailable_when_no_lake_root(
    nolake_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/index-constituents degrades honestly (source=unavailable) without a cache root.

    EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration Law 2):
    constituent picking is STOCK machinery, dormant under the shipped index4
    default (the guard arm above); the no-lake-root degrade is reachable only
    under a stock-admitting scope.
    """
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")
    resp = nolake_client.get("/api/v1/universe/index-constituents", params={"symbol": "NIFTY"})
    assert resp.status_code == 200
    assert resp.json() == {
        "members": [],
        "source": "unavailable",
        "as_of": None,
        "note": (
            "No lake wired — constituents cache unavailable; "
            "use 'Pull ALL F&O stocks' (the superset)."
        ),
    }


def test_coverage_options_empty_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/coverage/options serves the empty projection when no lake root is wired."""
    resp = nolake_client.get("/api/v1/coverage/options")
    assert resp.status_code == 200
    assert resp.json() == {
        "headline": {"underlyings": 0, "series": 0, "total_bars": 0},
        "rows": [],
        # Freshness honesty fields (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001) —
        # the no-lake degrade keeps the same shape as the real path.
        "duty_degraded": False,
        "snapshot_age_s": 0.0,
    }


def test_preopen_symbols_empty_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/reports/preopen/symbols returns no symbols when no lake root is wired.

    The payload keeps the SAME additive freshness-key shape as the real path
    (DEC-STALENESS-CACHE-TRUTH-001 — a store without a filesystem lake has
    trivially never observed a prebuild).
    """
    resp = nolake_client.get("/api/v1/reports/preopen/symbols")
    assert resp.status_code == 200
    assert resp.json() == {
        "symbols": [],
        "basis": "day",
        "basis_derived": True,
        "derived_last_built": None,
    }


def test_db_candles_400_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/db/candles rejects the read when no lake root is wired."""
    resp = nolake_client.get(
        "/api/v1/db/candles",
        params={"exchange": "NSE", "segment": "CASH", "symbol": "NIFTY", "interval": "1minute"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no lake root wired for candle reads"


def test_db_options_400_when_no_lake_root(nolake_client: TestClient) -> None:
    """/api/v1/db/options rejects the read when no lake root is wired."""
    resp = nolake_client.get("/api/v1/db/options", params={"series": "anything"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no lake root wired for option reads"


# ────────────────────────────────────────────────────────────────────────
# Expiries dedup + universe explorer wired-store routes
# ────────────────────────────────────────────────────────────────────────


def test_expiries_dedup_collapses_duplicate_series(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two strikes of one (underlying, flag, code) series collapse to ONE expiry row."""

    def _two_strikes(
        conn: Any, lake: Path, *, underlying_symbol: str | None = None
    ) -> list[dict[str, Any]]:
        base = {
            "exchange": "NSE",
            "underlying_symbol": "NIFTY",
            "expiry_flag": "MONTH",
            "expiry_code": 1,
        }
        return [
            {**base, "strike_offset": "ATM", "side": "CALL", "strike": 100.0},
            {**base, "strike_offset": "ATM+1", "side": "CALL", "strike": 150.0},
        ]

    monkeypatch.setattr(app_module, "list_option_series", _two_strikes)
    resp = client.get("/api/v1/expiries")
    assert resp.status_code == 200
    assert resp.json() == {
        "expiries": [
            {
                "exchange": "NSE",
                "underlying_symbol": "NIFTY",
                "expiry_flag": "MONTH",
                "expiry_code": 1,
            }
        ]
    }


def test_universe_summary_counts_wired_store(client: TestClient) -> None:
    """/api/v1/universe/summary tallies the wired store's single admitted index."""
    resp = client.get("/api/v1/universe/summary")
    assert resp.status_code == 200
    assert resp.json() == {
        "index_count": 1,
        "stock_count": 0,
        "total": 1,
        "reference_stock_count": 0,
        "stock_count_total": 0,
    }


def test_universe_instruments_invalid_kind_400(client: TestClient) -> None:
    """An unknown ?kind= filter maps the helper's ValueError to HTTP 400."""
    resp = client.get("/api/v1/universe/instruments", params={"kind": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "kind must be one of ('index', 'stock') or None, got 'bogus'"


def test_universe_instruments_index_happy_path(client: TestClient) -> None:
    """A valid ?kind=index filter returns HTTP 200 with the explorer payload."""
    resp = client.get("/api/v1/universe/instruments", params={"kind": "index"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == "index"
    assert "instruments" in payload
    assert isinstance(payload["instruments"], list)


def test_universe_instruments_stock_happy_path(client: TestClient) -> None:
    """A valid ?kind=stock filter returns HTTP 200 with the explorer payload."""
    resp = client.get("/api/v1/universe/instruments", params={"kind": "stock"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == "stock"
    assert "instruments" in payload
    assert isinstance(payload["instruments"], list)


def test_load_instruments_returns_counts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful keyless instrument-master load echoes the loader's counts."""
    monkeypatch.setattr(
        "brutex.dashboard.instrument_loader.load_instrument_master",
        lambda store: {"admitted": 3, "parsed": 10},
    )
    resp = client.post("/api/v1/universe/load-instruments")
    assert resp.status_code == 200
    assert resp.json() == {"admitted": 3, "parsed": 10}


def test_load_instruments_maps_oserror_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network failure on the instrument-master fetch surfaces as HTTP 502."""

    def _down(store: Any) -> dict[str, int]:
        raise OSError("asset host unreachable")

    monkeypatch.setattr("brutex.dashboard.instrument_loader.load_instrument_master", _down)
    resp = client.post("/api/v1/universe/load-instruments")
    assert resp.status_code == 502
    assert resp.json()["detail"] == (
        "couldn't reach vendor asset host / parse instrument master: asset host unreachable"
    )


# ────────────────────────────────────────────────────────────────────────
# Single-symbol pull error paths (DEC-PULL-ERROR-VISIBILITY-001)
# ────────────────────────────────────────────────────────────────────────


def test_universe_pull_unknown_symbol_400(client: TestClient) -> None:
    """An unknown ?symbol= on /universe/pull fails closed with the real reason."""
    resp = client.post("/api/v1/universe/pull", params=_dry({"symbol": "NOPE"}))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown security_id 'NOPE' — not in the instrument store"


def test_universe_pull_surfaces_real_failure_as_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull failure surfaces its REAL type + message, never a blank 400."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vendor exploded")

    monkeypatch.setattr(app_module, "pull_candles", _boom)
    resp = client.post("/api/v1/universe/pull", params=_dry({"symbol": "NIFTY"}))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "RuntimeError: vendor exploded"


# ────────────────────────────────────────────────────────────────────────
# Bulk spot pull — every selection form (DEC-DHAN-SPOT-MULTI-SATURATE-001)
# ────────────────────────────────────────────────────────────────────────


def test_pull_all_symbols_form_starts_background_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?symbols= resolves the ticked ids and starts the single-flight job."""
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"symbols": "NIFTY"}))
    assert resp.status_code == 200
    assert (
        resp.json()
        == {
            "started": True,
            "kind": None,
            "symbol": None,
            "symbols": "NIFTY",
            "total": 1,  # default = 1-minute only (1 interval × 1 instrument; DEC-1MINUTE-ONLY-DERIVE-1DAY-001)
            "dry_run": True,
        }
    )
    assert _wait_for_job(client)["state"] == "done"


def test_pull_all_symbols_unknown_id_fails_closed(client: TestClient) -> None:
    """An unknown id in ?symbols= rejects the WHOLE request, naming the offender."""
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"symbols": "NIFTY,NOPE"}))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown security_id(s) not in the instrument store: NOPE"


def test_pull_all_symbol_form_starts_background_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?symbol= resolves one instrument and starts the single-flight job."""
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"symbol": "NIFTY"}))
    assert resp.status_code == 200
    assert (
        resp.json()
        == {
            "started": True,
            "kind": None,
            "symbol": "NIFTY",
            "symbols": None,
            "total": 1,  # default = 1-minute only (1 interval × 1 instrument; DEC-1MINUTE-ONLY-DERIVE-1DAY-001)
            "dry_run": True,
        }
    )
    assert _wait_for_job(client)["state"] == "done"


def test_pull_all_unknown_symbol_400(client: TestClient) -> None:
    """An unknown ?symbol= on pull-all fails closed with the real reason."""
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"symbol": "NOPE"}))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown security_id 'NOPE' — not in the instrument store"


def test_pull_all_kind_form_starts_background_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?kind=index selects every admitted index and starts the single-flight job."""
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"kind": "index"}))
    assert resp.status_code == 200
    assert (
        resp.json()
        == {
            "started": True,
            "kind": "index",
            "symbol": None,
            "symbols": None,
            "total": 1,  # default = 1-minute only (1 interval × 1 instrument; DEC-1MINUTE-ONLY-DERIVE-1DAY-001)
            "dry_run": True,
        }
    )
    assert _wait_for_job(client)["state"] == "done"


def test_pull_all_invalid_kind_400(client: TestClient) -> None:
    """An unknown ?kind= maps the bulk-target ValueError to HTTP 400."""
    resp = client.post("/api/v1/universe/pull-all", params=_dry({"kind": "bogus"}))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "kind must be one of ('index', 'stock'), got 'bogus'"


def test_pull_all_requires_a_selection_form(client: TestClient) -> None:
    """No symbols / symbol / kind → 400 naming all three accepted forms."""
    resp = client.post("/api/v1/universe/pull-all", params=_dry())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "pull-all requires ?symbols=..., ?symbol=... or ?kind=..."


# ────────────────────────────────────────────────────────────────────────
# Manual derived-TF materialise route — REMOVED (DEC-PIPELINE-AUTOPREBUILD-001)
# ────────────────────────────────────────────────────────────────────────


def test_derive_timeframes_route_is_gone(client: TestClient) -> None:
    """The manual ``POST /api/v1/universe/derive-timeframes`` route stays DELETED.

    DEC-PIPELINE-AUTOPREBUILD-001: derived timeframes are written ONLY by the
    pipeline's automatic prebuild stage — the manual route (and its button) are
    removed, so the path must 404 for every request shape it used to serve.
    """
    for params in ({"symbol": "NIFTY"}, {"symbols": "NIFTY"}, {}):
        resp = client.post("/api/v1/universe/derive-timeframes", params=params)
        assert resp.status_code == 404, "the manual derive-timeframes route must stay deleted"


# ────────────────────────────────────────────────────────────────────────
# DB viewer routes — error→HTTP mapping arms (DEC-DB-QUERY-001)
# ────────────────────────────────────────────────────────────────────────


def test_db_query_valid_filter_token_is_applied(client: TestClient) -> None:
    """A well-formed f=col:op:value filter parses and narrows the page."""
    resp = client.get("/api/v1/db/query/instruments", params={"f": "symbol_name:eq:NIFTY"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    miss = client.get("/api/v1/db/query/instruments", params={"f": "symbol_name:eq:NOPE"})
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


def test_db_query_unknown_operator_maps_to_400(client: TestClient) -> None:
    """An operator outside the allowlist maps the ValueError to HTTP 400."""
    resp = client.get("/api/v1/db/query/instruments", params={"f": "symbol_name:zzz:NIFTY"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == f"unknown operator 'zzz'; allowed: {sorted(ALLOWED_OPERATORS)}"


def test_db_columns_unknown_table_maps_to_400(client: TestClient) -> None:
    """An unknown table name on /db/columns maps the ValueError to HTTP 400."""
    resp = client.get("/api/v1/db/columns/not_a_table")
    assert resp.status_code == 400
    assert (
        resp.json()["detail"] == f"unknown table 'not_a_table'; allowed tables are {KNOWN_TABLES}"
    )


def test_db_candles_empty_partition_returns_empty_page(client: TestClient) -> None:
    """Valid coordinates over a not-yet-pulled partition return an empty page, never a 500."""
    resp = client.get(
        "/api/v1/db/candles",
        params={"exchange": "NSE", "segment": "CASH", "symbol": "NIFTY", "interval": "1minute"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["symbol"] == "NIFTY"


def test_db_candles_invalid_interval_maps_to_400(client: TestClient) -> None:
    """An interval outside the partition allowlist maps the ValueError to HTTP 400."""
    resp = client.get(
        "/api/v1/db/candles",
        params={"exchange": "NSE", "segment": "CASH", "symbol": "NIFTY", "interval": "bogus"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == f"unknown interval 'bogus'; allowed: {sorted(VALID_INTERVALS)}"


def test_db_options_illegal_series_maps_to_400(client: TestClient) -> None:
    """A traversal-shaped series name maps the ValueError to HTTP 400."""
    resp = client.get("/api/v1/db/options", params={"series": "../evil"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "illegal option series '../evil'"


# ────────────────────────────────────────────────────────────────────────
# HTML pages + live-progress poll
# ────────────────────────────────────────────────────────────────────────


def test_pull_progress_idle_snapshot(client: TestClient) -> None:
    """/pull-progress serves the idle O(1) snapshot before any pull starts."""
    resp = client.get("/api/v1/universe/pull-progress")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_db_page_served_with_no_store_header(client: TestClient) -> None:
    """GET /db serves the browser page with the no-store cache header."""
    resp = client.get("/db")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"


def test_universe_page_served(client: TestClient) -> None:
    """GET /universe serves the F&O Universe Explorer page."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "/api/v1/universe" in resp.text


def test_universe_tick_constituents_ticks_in_place_no_navigation(client: TestClient) -> None:
    """⊕ tick its constituents selects the mapped stocks IN PLACE — no tab navigation.

    DEC-UNIVERSE-CONSTITUENTS-TICK-IN-PLACE-001: selecting an index and ticking its
    constituents must add the mapped stocks to the multi-select WITHOUT switching the
    active tab to the Stocks screen. The handler fetches the stock instruments in the
    BACKGROUND (``?kind=stock``) into a local list and re-renders the CURRENT view; it
    must NOT re-assign ``filter`` to the stock tab (the prior navigating behavior).
    """
    html = client.get("/universe").text
    # The in-place implementation marker + the background stock fetch are present.
    assert "DEC-UNIVERSE-CONSTITUENTS-TICK-IN-PLACE-001" in html
    assert "/api/v1/universe/instruments?kind=stock" in html
    # The navigating behavior is GONE: the constituents handler no longer forces the
    # Stocks tab (the removed ``filter = "stock"`` re-assignment).
    assert 'filter = "stock"' not in html
