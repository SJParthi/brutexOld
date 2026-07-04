"""Integration tests for the parse-skip route + /universe panel.

DEC-PARSE-SKIP-REPORT-001 — ``GET /api/v1/universe/parse-skips`` over a real wired
:class:`InstrumentStore`, plus the ``_persist_parse_skips`` hook that persists the
parser's bounded skip accounting (DEC-INSTRUMENT-SKIP-NO-GSYM-001) to ``schema_meta``,
and the green/red ``/universe`` panel markup (textContent-only DOM).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.adapters.groww.instruments import PARSE_SKIPS_META_KEY, ParseSkipReport
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.instrument_loader import _persist_parse_skips
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    yield s
    s.close()


def test_route_503_when_store_unwired() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/parse-skips").status_code == 503


def test_present_false_before_any_load(store: InstrumentStore) -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    assert client.get("/api/v1/universe/parse-skips").json() == {"present": False}


def test_persist_hook_and_route_render_green(store: InstrumentStore) -> None:
    report = ParseSkipReport(skipped_count=1, in_scope_rows=162_000, samples=("bad row",))
    payload = _persist_parse_skips(report, store, run_id="run-x", csv_sha256="sha-x")
    assert payload["is_green"] is True
    assert payload["threshold"] == 162  # ceil(0.1% of 162k)
    assert store.get_meta(PARSE_SKIPS_META_KEY) is not None

    client = TestClient(build_app(DashboardState(), instrument_store=store))
    body = client.get("/api/v1/universe/parse-skips").json()
    assert body["present"] is True and body["is_green"] is True
    assert body["skipped_count"] == 1
    assert body["in_scope_rows"] == 162_000
    assert body["samples"] == ["bad row"]
    assert body["run_id"] == "run-x"


def test_persist_hook_and_route_render_red(store: InstrumentStore) -> None:
    # 11 skips on a 100-row master breaches max(10, ceil(0.1)) = 10 → RED.
    report = ParseSkipReport(
        skipped_count=11,
        in_scope_rows=100,
        samples=tuple(f"reason {i}" for i in range(10)),
    )
    payload = _persist_parse_skips(report, store, run_id="run-y", csv_sha256="sha-y")
    assert payload["is_green"] is False

    client = TestClient(build_app(DashboardState(), instrument_store=store))
    body = client.get("/api/v1/universe/parse-skips").json()
    assert body["present"] is True and body["is_green"] is False
    assert body["skipped_count"] == 11 and body["threshold"] == 10
    assert len(body["samples"]) == 10  # bounded — the parser's sample cap


def test_universe_page_carries_parse_skip_panel() -> None:
    """The /universe page ships the green/red panel + its textContent-only loader."""
    client = TestClient(build_app(DashboardState()))
    page = client.get("/universe")
    assert page.status_code == 200
    assert 'id="skippanel"' in page.text
    assert "Master parse skips" in page.text
    assert "loadParseSkips" in page.text
    assert "/api/v1/universe/parse-skips" in page.text
    # XSS guard: the panel loader writes via textContent only, never innerHTML.
    loader = page.text.split("async function loadParseSkips()")[1].split("async function")[0]
    assert "innerHTML" not in loader
    assert "textContent" in loader
