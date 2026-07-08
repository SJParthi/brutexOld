"""``GET /api/v1/sweep/verify-me`` + ``GET /verify-me`` (DEC-INSTITUTIONAL-ONECLICK-001, P4 stage 5 of 6).

Mirrors the ``test_dashboard_oneclick_routes.py`` harness for the JSON route
(query-param wiring / honest ``awaiting_artifact`` envelope with no fixture
root / the ``ok`` envelope against the checked-in fixture tree / HTTP 400 on
a path-traversal ``run_id``) and the ``test_dashboard_strategy_route.py``
pattern for the static HTML page (``/verify-me`` carries NO server-side
token substitution — the client resolves/parses ``run_id`` entirely
client-side, so a hostile query value must never be reflected into the
response body).

``read_verify_me`` itself is already exhaustively unit-tested standalone in
``test_dashboard_oneclick_artifacts.py`` — this file covers the ROUTE GLUE
+ the two HTML pages only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard import oneclick_artifacts as oa
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

FIXTURE_ONECLICK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
FIXTURE_SWEEP_RUNS = FIXTURE_ONECLICK_ROOT / "sweep_runs"

RUN_FULL = "run_2026-07-08T090000Z_nifty_full"
RUN_SMOKE = "run_2026-07-07T210000Z_nifty_smoke_complete"
RUN_NO_VERIFYME = "run_no_verifyme_file"
RUN_CORRUPT_MANIFEST = "run_corrupt_manifest"
RUN_DEMO = "demo-allpass-2026-07-08"

_TRAVERSAL_RUN_IDS = ("../../etc/passwd", "..", "a/b", "", "run id with spaces")


def _nifty() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let BRUTEX_ONECLICK_FIXTURE_ROOT leak between tests."""
    monkeypatch.delenv(oa.FIXTURE_ROOT_ENV, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


@pytest.fixture
def fixture_client(store: InstrumentStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client with BRUTEX_ONECLICK_FIXTURE_ROOT pointed at the checked-in tree."""
    monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(FIXTURE_SWEEP_RUNS))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


# ── GET /api/v1/sweep/verify-me ─────────────────────────────────────────────


def test_verify_me_route_ok_when_no_run_has_launched(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/verify-me")
    assert resp.status_code == 200
    body = resp.json()
    # No run has ever launched — a valid state, not an error: still "ok" with
    # every one of the 7 checks honestly "awaiting" (never a 500, never a
    # fabricated pass).
    assert body["state"] == "ok"
    assert body["run_id"] is None
    assert body["is_demo"] is False
    assert set(body["checks"]) == {
        "determinism",
        "audit_replay",
        "trade_chart_match",
        "plain_english_arithmetic",
        "kill_resume",
        "dedup_zero_recomputed",
        "funnel_reconcile",
    }
    for check in body["checks"].values():
        assert check["status"] == "awaiting"
        assert check["detail"]


def test_verify_me_route_honest_run_all_awaiting(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/verify-me", params={"run_id": RUN_SMOKE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["run_id"] == RUN_SMOKE
    assert body["is_demo"] is False
    for check in body["checks"].values():
        assert check["status"] == "awaiting"


def test_verify_me_route_missing_file_synthesizes_honest_default(fixture_client: TestClient) -> None:
    """RUN_NO_VERIFYME carries no verify-me.json at all — must render
    IDENTICALLY to a run whose verify-me.json exists but says "awaiting"."""
    resp = fixture_client.get("/api/v1/sweep/verify-me", params={"run_id": RUN_NO_VERIFYME})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    for check in body["checks"].values():
        assert check["status"] == "awaiting"


def test_verify_me_route_demo_fixture_all_pass(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/verify-me", params={"run_id": RUN_DEMO})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["is_demo"] is True
    for check in body["checks"].values():
        assert check["status"] == "pass"
        assert check["detail"]


def test_verify_me_route_defaults_to_latest_run(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/verify-me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["run_id"] is not None


@pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
def test_verify_me_route_rejects_bad_run_id(client: TestClient, run_id: str) -> None:
    resp = client.get("/api/v1/sweep/verify-me", params={"run_id": run_id})
    assert resp.status_code == 400


def test_verify_me_route_degrades_honestly_with_no_lake_wired() -> None:
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.get("/api/v1/sweep/verify-me")
        assert resp.status_code == 200
        assert resp.json()["state"] == "ok"


# ── GET /verify-me (static HTML page) ───────────────────────────────────────


def test_verify_me_page_serves_200_html(client: TestClient) -> None:
    resp = client.get("/verify-me")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>BruteX — VERIFY ME</title>" in resp.text


def test_verify_me_page_is_byte_identical_with_and_without_query_string(client: TestClient) -> None:
    plain = client.get("/verify-me").text
    with_qs = client.get("/verify-me", params={"run_id": RUN_FULL}).text
    assert plain == with_qs


@pytest.mark.parametrize(
    "run_id",
    [
        "<script>alert(1)</script>",
        "\"><img src=x onerror=alert(1)>",
        "../../etc/passwd",
        "run_id_with_'quotes'_and_\"doublequotes\"",
    ],
)
def test_verify_me_page_never_reflects_hostile_query_values(client: TestClient, run_id: str) -> None:
    resp = client.get("/verify-me", params={"run_id": run_id})
    assert resp.status_code == 200
    assert run_id not in resp.text
    assert "<script>alert(1)</script>" not in resp.text
    assert "onerror=alert(1)" not in resp.text


def test_verify_me_page_parses_its_own_query_string_client_side(client: TestClient) -> None:
    html = client.get("/verify-me").text
    assert "new URLSearchParams(window.location.search)" in html
    assert 'qs("run_id")' in html


def test_verify_me_page_reads_the_documented_data_endpoints(client: TestClient) -> None:
    html = client.get("/verify-me").text
    assert "/api/v1/sweep/verify-me" in html
    assert "/api/v1/sweep/run-status" in html
    assert "/api/v1/sweep/top100" in html
    assert "/api/v1/sweep/trades?" in html
    assert "/api/v1/db/candles?" in html
    assert "/api/v1/sweep/timeline?" in html
    assert "/api/v1/sweep/runs?" in html


def test_verify_me_page_never_defaults_to_a_demo_run_id_in_source(client: TestClient) -> None:
    """The auto-resolve path must filter is_demo before picking a default —
    pinned structurally (the demo run_id constant only appears next to the
    explicit toggle wiring, never as a bare fallback)."""
    html = client.get("/verify-me").text
    assert "row.is_demo" in html
    assert "!row.is_demo" in html
    assert "demo-allpass-2026-07-08" in html  # only via the explicit toggle


def test_verify_me_page_loads_shared_helper_scripts(client: TestClient) -> None:
    html = client.get("/verify-me").text
    assert '<script defer src="/static/bx-oneclick.js"></script>' in html
    assert '<script defer src="/static/bx-oneclick-chart.js"></script>' in html


def test_verify_me_page_carries_no_topnav(client: TestClient) -> None:
    html = client.get("/verify-me").text
    assert '<nav class="topnav"' not in html
    assert 'href="/"' in html
    assert 'href="/results"' in html


def test_verify_me_route_not_registered_in_nav_links() -> None:
    """/verify-me is intentionally absent from the canonical nav.py NAV_LINKS."""
    from brutex.dashboard.nav import NAV_LINKS

    assert all(link.href != "/verify-me" for link in NAV_LINKS)


def test_verify_me_page_renders_all_7_checks_in_fixed_order(client: TestClient) -> None:
    html = client.get("/verify-me").text
    order = [
        "determinism",
        "audit_replay",
        "trade_chart_match",
        "plain_english_arithmetic",
        "kill_resume",
        "dedup_zero_recomputed",
        "funnel_reconcile",
    ]
    positions = [html.index(f'key: "{key}"') for key in order]
    assert positions == sorted(positions), "the 7 checks must render in the backend's fixed order"


def test_verify_me_page_never_wires_a_live_post_for_disabled_actions(client: TestClient) -> None:
    """R44's honesty pin: the 'Re-run verification' / 'Replay chain' buttons
    are rendered disabled, never wired to a fake POST — a clickable button
    that silently does nothing would itself violate the no-hallucination
    rule."""
    html = client.get("/verify-me").text
    assert "btn.disabled = true;" in html
    assert html.count("btn.disabled = true;") >= 2
    assert 'fetch("/api/v1/sweep/run"' not in html
    assert "POST" not in html


# ── operator-page reachability (v29.html link, not a NAV_LINKS entry) ───────


def test_operator_page_carries_a_verify_me_link(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="rsVerifyMeLink"' in html
    assert 'href="/verify-me"' in html
    assert "verifyLink.href = runId" in html
