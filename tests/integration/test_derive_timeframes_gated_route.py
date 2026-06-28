"""Gated multi-symbol derive-timeframes route integration (DEC-PREBUILD-SELECT-001).

FastAPI ``TestClient`` over ``POST /api/v1/universe/derive-timeframes`` proving:

* ``?symbols=`` runs the GATED SPOT → FUTURES → OPTIONS report;
* a forced failure in the SPOT phase HALTS the run — FUTURES + OPTIONS never
  start (the gate);
* OPTIONS is always reported as an on-demand no-op (no intraday written);
* the single-symbol path stays back-compat (``{written, skipped, total_bars}``),
  and a single OPTIONS symbol is the explicit no-op.

``materialize_timeframes`` is monkeypatched to a deterministic fake keyed by the
read coordinates so the gate is exercised without lake I/O (the real
materialise core is unit-tested separately).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.timeframe_materialize import MaterializeResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _index(name: str, sid: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _future(name: str, sid: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=None,
    )


def _option(name: str, sid: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("20000"),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [
            _index("NIFTY", 1),
            _future("NIFTY-25JAN-FUT", 2),
            _option("NIFTY-25JAN-20000-CE", 3),
        ],
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
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _patch_materialize(
    monkeypatch: pytest.MonkeyPatch, *, fail_symbol: str | None = None
) -> list[str]:
    """Patch the lake read builder + ``materialize_timeframes`` to a recording fake.

    ``_make_read_1m`` is patched to return a tagged sentinel carrying its
    ``symbol`` (robust — no closure-cell sniffing), and ``_make_write_tf`` to a
    no-op; ``materialize_timeframes`` then reads the tag, records the symbol, and
    raises for *fail_symbol* — exercising the gate without any lake I/O.
    """
    materialized: list[str] = []

    def _fake_read(*, symbol: str, **_kw: Any) -> Any:
        sentinel = lambda: None  # noqa: E731 — a tiny tagged callable
        sentinel.symbol = symbol  # type: ignore[attr-defined]
        return sentinel

    def _fake_write(**_kw: Any) -> Any:
        return lambda _tf, _frame: 0

    def _fake(
        *, tfs: tuple[str, ...], read_1m: Any, write_tf: Any, resampler: Any = None
    ) -> MaterializeResult:
        symbol = read_1m.symbol
        materialized.append(symbol)
        if fail_symbol is not None and symbol == fail_symbol:
            raise ValueError(f"no stored 1minute bars for {symbol}")
        return MaterializeResult(written={"5m": 9}, skipped=())

    monkeypatch.setattr(app_module, "_make_read_1m", _fake_read)
    monkeypatch.setattr(app_module, "_make_write_tf", _fake_write)
    monkeypatch.setattr(app_module, "materialize_timeframes", _fake)
    return materialized


def test_gated_multi_symbol_runs_all_three_phases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch_materialize(monkeypatch)
    resp = client.post(
        "/api/v1/universe/derive-timeframes",
        params={"symbols": "NIFTY,NIFTY-25JAN-FUT,NIFTY-25JAN-20000-CE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["gated"] is True
    assert body["ok"] is True
    assert body["halted_after"] is None
    assert body["source"] == "selection"
    phases = {p["phase"]: p for p in body["phases"]}
    assert list(phases) == ["spot", "futures", "options"]
    assert phases["spot"]["succeeded"] == ["NIFTY"]
    assert phases["futures"]["succeeded"] == ["NIFTY-25JAN-FUT"]
    # OPTIONS reported, never materialised (on-demand).
    assert phases["options"]["attempted"] == ["NIFTY-25JAN-20000-CE"]
    assert phases["options"]["total_bars"] == 0
    assert "on-demand" in phases["options"]["note"]
    assert "NIFTY-25JAN-20000-CE" not in materialized
    assert materialized == ["NIFTY", "NIFTY-25JAN-FUT"]
    assert body["total_bars"] == 18


def test_spot_phase_failure_halts_futures_and_options(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch_materialize(monkeypatch, fail_symbol="NIFTY")
    resp = client.post(
        "/api/v1/universe/derive-timeframes",
        params={"symbols": "NIFTY,NIFTY-25JAN-FUT,NIFTY-25JAN-20000-CE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["gated"] is True
    assert body["ok"] is False
    assert body["halted_after"] == "spot"
    # Only the SPOT phase ran — futures + options never started.
    assert [p["phase"] for p in body["phases"]] == ["spot"]
    # The futures contract was NEVER materialised (the gate held).
    assert materialized == ["NIFTY"]
    assert body["phases"][0]["failed"][0]["symbol"] == "NIFTY"


def test_options_only_selection_is_reported_noop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch_materialize(monkeypatch)
    resp = client.post(
        "/api/v1/universe/derive-timeframes",
        params={"symbols": "NIFTY-25JAN-20000-CE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["total_bars"] == 0
    assert materialized == []  # nothing materialised
    options = next(p for p in body["phases"] if p["phase"] == "options")
    assert options["attempted"] == ["NIFTY-25JAN-20000-CE"]


def test_empty_symbols_selection_fails_closed(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": ""})
    assert resp.status_code == 400


def test_unknown_symbols_member_fails_closed(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": "NIFTY,NOPE"})
    assert resp.status_code == 400


def test_single_symbol_backcompat_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_materialize(monkeypatch)
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbol": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"written": {"5m": 9}, "skipped": [], "total_bars": 9}


def test_single_option_symbol_is_noop(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/universe/derive-timeframes", params={"symbol": "NIFTY-25JAN-20000-CE"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["written"] == {}
    assert body["total_bars"] == 0
    assert "on-demand" in body["note"]


def test_no_symbol_or_symbols_is_400(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes")
    assert resp.status_code == 400
    assert "?symbol=" in resp.json()["detail"]
