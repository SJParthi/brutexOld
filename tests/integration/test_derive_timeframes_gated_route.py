"""Gated multi-symbol derive-timeframes route integration (DEC-PREBUILD-SELECT-001).

FastAPI ``TestClient`` over ``POST /api/v1/universe/derive-timeframes`` proving:

* ``?symbols=`` runs the GATED SPOT → FUTURES → OPTIONS report;
* the FUTURES phase discovers the selected underlyings' FUT contracts from the
  F&O candle lake cascade and materialises each;
* a forced failure in the SPOT phase HALTS the run — FUTURES + OPTIONS never
  start (the gate);
* OPTIONS is always reported as an on-demand no-op (no intraday written);
* the single-symbol path stays back-compat (``{written, skipped, total_bars}``),
  and a single OPTIONS symbol is the explicit no-op.

``materialize_lake_timeframes`` is monkeypatched to a deterministic fake keyed by
the partition symbol so the gate is exercised without lake I/O; the F&O cascade is
fed a fake dataset list so futures discovery is deterministic (the real
materialise core + cascade are unit/integration-tested separately).
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


_NIFTY_FUTURES = ["NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"]


def _fno_datasets() -> list[dict[str, object]]:
    """NIFTY futures (two expiries) the cascade discovery resolves for NSE-NIFTY."""
    rows: list[dict[str, object]] = []
    for sym, exp in zip(_NIFTY_FUTURES, ("2025-01-30", "2025-02-27"), strict=True):
        rows.append(
            {
                "symbol": sym,
                "exchange": "NSE",
                "underlying": "NIFTY",
                "expiry": exp,
                "strike": None,
                "option_type": None,
                "instrument_type": "FUT",
                "interval": "1minute",
            }
        )
    return rows


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [
            _index("NSE-NIFTY", 1),
            _option("NSE-NIFTY-25JAN-20000-CE", 3),
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


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_symbol: str | None = None,
    with_futures: bool = True,
) -> list[str]:
    """Patch materialize + F&O dataset listing; record each materialised symbol.

    ``materialize_lake_timeframes`` is patched to record its ``symbol`` and raise
    for *fail_symbol*; ``list_fno_candle_datasets`` is patched to feed the cascade
    deterministic NIFTY futures (or none, when *with_futures* is False) so the
    FUTURES phase's discovery is exercised without a real lake.
    """
    materialized: list[str] = []

    def _fake_materialize(
        *,
        lake_root: Any,
        exchange: str,
        segment: str,
        symbol: str,
        kind: Any,
        tfs: Any = (),
    ) -> MaterializeResult:
        materialized.append(symbol)
        if fail_symbol is not None and symbol == fail_symbol:
            raise ValueError(f"no stored 1minute bars for {symbol}")
        return MaterializeResult(written={"5m": 9}, skipped=())

    def _fake_datasets(_lake_root: Any) -> list[dict[str, object]]:
        return _fno_datasets() if with_futures else []

    monkeypatch.setattr(app_module, "materialize_lake_timeframes", _fake_materialize)
    monkeypatch.setattr(app_module, "list_fno_candle_datasets", _fake_datasets)
    return materialized


def test_gated_multi_symbol_runs_all_three_phases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch(monkeypatch)
    resp = client.post(
        "/api/v1/universe/derive-timeframes",
        params={"symbols": "NSE-NIFTY"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["gated"] is True
    assert body["ok"] is True
    assert body["halted_after"] is None
    assert body["source"] == "selection"
    phases = {p["phase"]: p for p in body["phases"]}
    assert list(phases) == ["spot", "futures", "options"]
    assert phases["spot"]["succeeded"] == ["NSE-NIFTY"]
    # FUTURES discovered + materialised the two NIFTY futures from the lake cascade.
    assert phases["futures"]["attempted"] == _NIFTY_FUTURES
    assert phases["futures"]["succeeded"] == _NIFTY_FUTURES
    # OPTIONS reported, never materialised (on-demand).
    assert phases["options"]["attempted"] == ["NSE-NIFTY"]
    assert phases["options"]["total_bars"] == 0
    assert "on-demand" in phases["options"]["note"]
    # spot underlying + the two futures contracts materialised; never an option.
    assert materialized == ["NSE-NIFTY", *_NIFTY_FUTURES]
    assert body["total_bars"] == 27  # 3 × 9


def test_no_futures_in_lake_is_clean_skip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch(monkeypatch, with_futures=False)
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": "NSE-NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    phases = {p["phase"]: p for p in body["phases"]}
    assert phases["futures"]["attempted"] == []
    assert "no futures contracts found" in phases["futures"]["note"]
    assert materialized == ["NSE-NIFTY"]  # only the spot underlying


def test_spot_phase_failure_halts_futures_and_options(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialized = _patch(monkeypatch, fail_symbol="NSE-NIFTY")
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": "NSE-NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["halted_after"] == "spot"
    assert [p["phase"] for p in body["phases"]] == ["spot"]
    # Futures contracts NEVER materialised (the gate held).
    assert materialized == ["NSE-NIFTY"]
    assert body["phases"][0]["failed"][0]["symbol"] == "NSE-NIFTY"


def test_empty_symbols_selection_fails_closed(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": ""})
    assert resp.status_code == 400


def test_unknown_symbols_member_fails_closed(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbols": "NSE-NIFTY,NOPE"})
    assert resp.status_code == 400


def test_single_symbol_backcompat_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbol": "NSE-NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"written": {"5m": 9}, "skipped": [], "total_bars": 9}


def test_single_symbol_zero_bars_skips_cache_dirty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-symbol materialise that writes 0 bars still 200s (no rescan needed)."""

    def _zero(
        *, lake_root: Any, exchange: str, segment: str, symbol: str, kind: Any, tfs: Any = ()
    ) -> MaterializeResult:
        return MaterializeResult(written={}, skipped=())

    monkeypatch.setattr(app_module, "materialize_lake_timeframes", _zero)
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbol": "NSE-NIFTY"})
    assert resp.status_code == 200
    assert resp.json() == {"written": {}, "skipped": [], "total_bars": 0}


def test_single_symbol_failure_maps_to_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, fail_symbol="NSE-NIFTY")
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbol": "NSE-NIFTY"})
    assert resp.status_code == 400
    assert "no stored 1minute bars" in resp.json()["detail"]


def test_single_unknown_symbol_400(client: TestClient) -> None:
    resp = client.post("/api/v1/universe/derive-timeframes", params={"symbol": "NOPE"})
    assert resp.status_code == 400
    assert "unknown security_id" in resp.json()["detail"]


def test_single_option_symbol_is_noop(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/universe/derive-timeframes", params={"symbol": "NSE-NIFTY-25JAN-20000-CE"}
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


# ── poisoned-partition quarantine verdict (DEC-LAKE-WRITE-UNIQUE-001) ──────


def test_poisoned_symbol_returns_gated_verdict_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FAILS on unfixed main: the resampler tripwire's ``ResampleInputError``
    escaped the per-symbol quarantine and the ``?symbols=`` route 500'd — now
    the route returns 200 with the honest per-symbol gated verdict (the
    poisoned symbol named in ``failed``, the healthy one still built).
    """
    from brutex.data.errors import ResampleInputError

    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))

    def _poisoned_materialize(
        *,
        lake_root: Any,
        exchange: str,
        segment: str,
        symbol: str,
        kind: Any,
        tfs: Any = (),
    ) -> MaterializeResult:
        if symbol == "NSE-BANKNIFTY":
            raise ResampleInputError("ts_utc must be STRICTLY increasing (duplicate row)")
        return MaterializeResult(written={"5m": 9}, skipped=())

    monkeypatch.setattr(app_module, "materialize_lake_timeframes", _poisoned_materialize)
    monkeypatch.setattr(app_module, "list_fno_candle_datasets", lambda _root: [])
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        store.upsert_many(
            [_index("NSE-NIFTY", 1), _index("NSE-BANKNIFTY", 2)],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        with TestClient(
            build_app(DashboardState(), instrument_store=store),
            raise_server_exceptions=False,
        ) as client:
            resp = client.post(
                "/api/v1/universe/derive-timeframes",
                params={"symbols": "NSE-NIFTY,NSE-BANKNIFTY"},
            )

    assert resp.status_code == 200  # was a 500 before the quarantine widening
    body = resp.json()
    assert body["gated"] is True
    assert body["ok"] is False
    assert body["halted_after"] == "spot"
    spot = body["phases"][0]
    assert spot["attempted"] == ["NSE-NIFTY", "NSE-BANKNIFTY"]
    assert spot["succeeded"] == ["NSE-NIFTY"]  # the healthy symbol still built
    assert spot["failed"] == [
        {
            "symbol": "NSE-BANKNIFTY",
            "error": ("ResampleInputError: ts_utc must be STRICTLY increasing (duplicate row)"),
        }
    ]
