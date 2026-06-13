"""The ``/api/v1/universe/verify-nse`` route (DEC-NSE-VERIFY-ROUTE-001).

The dashboard surface of the spot-vs-NSE one-day trust anchor: a thin wrapper over
the degrade-safe :func:`brutex.data.validation.nse_oneday.validate_index_oneday`.
The harness itself is exhaustively covered by ``test_validation_nse_oneday.py``;
here we cover only the ROUTE's own logic — ISO-date parsing (good + the lone 400),
the threadpool call, and that the verdict is serialised verbatim through
``OneDayVerdict.to_dict`` — by monkeypatching ``validate_index_oneday`` to a fake
that returns a crafted verdict (so the route test is 100% offline, no network).
The ``/universe`` page is also asserted to carry the new button + route wiring.
Skipped cleanly when the ``[ui]`` extra (FastAPI) is absent.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import date

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.validation.nse_oneday import (
    DEFAULT_TOLERANCE_RUPEES_UNVERIFIED,
    OneDayVerdict,
    VerdictStatus,
)
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient over a credential-free dashboard (no store needed for verify)."""
    with TestClient(build_app(DashboardState(), instrument_store=None)) as c:
        yield c


def _patch_validate(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    """Monkeypatch the lazily-imported ``validate_index_oneday`` on its source module."""
    mod = importlib.import_module("brutex.data.validation.nse_oneday")
    monkeypatch.setattr(mod, "validate_index_oneday", fake)


def _unavailable_verdict(symbol: str, trade_date: date) -> OneDayVerdict:
    return OneDayVerdict(
        symbol=symbol,
        trade_date=trade_date,
        status=VerdictStatus.UNAVAILABLE,
        tolerance=DEFAULT_TOLERANCE_RUPEES_UNVERIFIED,
        fields=(),
        groww_source=None,
        official_source="nse",
        note="no Groww 1-day bar for 2025-09-24 IST",
    )


def test_verify_returns_serialised_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake(symbol: str, trade_date: date) -> OneDayVerdict:
        captured["symbol"] = symbol
        captured["trade_date"] = trade_date
        return _unavailable_verdict(symbol, trade_date)

    _patch_validate(monkeypatch, fake)
    resp = client.post("/api/v1/universe/verify-nse?symbol=NIFTY&date=2025-09-24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NIFTY"
    assert body["status"] == "unavailable"
    assert body["trade_date"] == "2025-09-24"
    assert body["fields"] == []
    assert body["official_source"] == "nse"
    # The route parsed the ISO date into a real date and passed the symbol through.
    assert captured["symbol"] == "NIFTY"
    assert captured["trade_date"] == date(2025, 9, 24)


def test_verify_bad_date_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(symbol: str, trade_date: date) -> OneDayVerdict:  # pragma: no cover - never reached
        raise AssertionError("validate must not run on a malformed date")

    _patch_validate(monkeypatch, fake)
    resp = client.post("/api/v1/universe/verify-nse?symbol=NIFTY&date=2025-13-99")
    assert resp.status_code == 400
    assert "invalid date" in resp.json()["detail"]
    assert "YYYY-MM-DD" in resp.json()["detail"]


def test_verify_missing_date_param_is_422(client: TestClient) -> None:
    # ``date`` is a required query param → FastAPI validation 422 (no handler run).
    resp = client.post("/api/v1/universe/verify-nse?symbol=NIFTY")
    assert resp.status_code == 422


def test_universe_page_carries_verify_button_and_route(client: TestClient) -> None:
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert "/api/v1/universe/verify-nse" in resp.text
    assert 'id="verifybtn"' in resp.text
    assert "Verify vs NSE" in resp.text
