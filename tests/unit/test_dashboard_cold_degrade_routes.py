"""Route-level cold-degrade tests (DEC-DASHBOARD-COLD-IO-DEADLINE-001, BUG-1).

The five summary-backed routes' NEW ``SummaryColdWaitTimeout`` arms, driven
through the REAL ``build_app`` + ``TestClient`` with an injected
``SummaryCache`` (tiny cold-wait budget) and an Event-blocked recompute:

* a COLD reader behind a WEDGED first scan gets the honest **503**
  ``error_type="summary_scan_pending"`` (+ ``kind``) — never a raw 500,
  never a fabricated empty payload (the ``_store_outage_response``
  Golden-Rule-#1 argument);
* the CSV route degrades to a text/plain 503 (a fake-EMPTY CSV would
  fabricate an empty lake for a one-shot curl/Excel reader);
* two consecutive degraded polls serve byte-IDENTICAL bodies (the
  skip-render poller discipline);
* after the wedge releases, every route serves its real 200 payload again
  (self-healing).

The pre-existing ``UniverseStoreOutageError`` arm on ``/universe/completeness``
stays beside the new one — its own suite
(``test_dashboard_probe_outage_serve_last_known.py``) re-runs UNMODIFIED as
that proof. Skipped when ``fastapi``/``duckdb`` are absent (the
``test_dashboard_instruments.py`` convention).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard import app as _app_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache, SummaryColdWaitTimeout
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    yield s
    s.close()


class _BlockedScan:
    """An Event-orchestrated recompute stand-in: signals entry, blocks until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, _conn: Any, _lake: Path, **_kwargs: Any) -> list[dict[str, Any]]:
        # ``**_kwargs`` mirrors the production seam's kw-only ``scope=``
        # (DEC-UNIVERSE-INDEX-ONLY-001 R2/R9 — the routes thread the read
        # union; this stand-in pins the cold-wait choreography, not the scan).
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=30), "test orchestration failure"
        return []


def _assert_warming_503(resp: Any, kind: str) -> None:
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["error_type"] == "summary_scan_pending"
    assert body["kind"] == kind
    assert "retry shortly" in body["error"]


class TestColdDegradeRoutes:
    def test_spot_family_degrades_503_then_recovers(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The four SPOT-backed routes 503 honestly behind a wedged first scan.

        One wedged SPOT leader (a real request thread blocked inside the
        recompute) → the first cold poller pays the tiny budget and 503s;
        every LATER cold poll — /coverage/spot, /completeness/scoreboard,
        the CSV, /universe/completeness — fails fast on the latch with the
        SAME byte-stable degrade body; release → real 200 payloads.
        """
        scan = _BlockedScan()
        monkeypatch.setattr(_app_module, "summarize_spot_verdict_datasets", scan)
        cache = SummaryCache(cold_wait_budget_s=0.05)
        app = build_app(DashboardState(), instrument_store=store, summary_cache=cache)
        client = TestClient(app)

        leader_status: list[int] = []

        def _leader() -> None:
            leader_status.append(client.get("/api/v1/coverage/spot").status_code)

        lead = threading.Thread(target=_leader, daemon=True)
        lead.start()
        try:
            assert scan.entered.wait(timeout=10)  # the leader owns the first scan
            first = client.get("/api/v1/coverage/spot")  # pays the 0.05 s budget
            _assert_warming_503(first, "spot")
            # LATCHED fail-fasts, one per SPOT-backed route — all instant.
            second = client.get("/api/v1/coverage/spot")
            _assert_warming_503(second, "spot")
            assert second.content == first.content  # byte-stable degrade body
            _assert_warming_503(client.get("/api/v1/completeness/scoreboard"), "spot")
            csv = client.get("/api/v1/completeness/scoreboard.csv")
            assert csv.status_code == 503
            assert csv.headers["content-type"].startswith("text/plain")
            assert "first lake summary scan still running" in csv.text
            _assert_warming_503(client.get("/api/v1/universe/completeness"), "spot")
            assert scan.calls == 1  # the single-scan law held throughout
        finally:
            scan.release.set()
            lead.join(timeout=10)
        assert not lead.is_alive()
        assert leader_status == [200]  # the wedged owner finally served
        # Self-healing: the stored entry now serves every route for real.
        assert client.get("/api/v1/coverage/spot").status_code == 200
        assert client.get("/api/v1/completeness/scoreboard").status_code == 200
        assert client.get("/api/v1/completeness/scoreboard.csv").status_code == 200
        assert client.get("/api/v1/universe/completeness").status_code == 200

    def test_options_coverage_degrades_503_then_recovers(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OPTIONS kind has its own owner/latch — /coverage/options 503s alike."""
        scan = _BlockedScan()
        monkeypatch.setattr(_app_module, "summarize_option_datasets", scan)
        cache = SummaryCache(cold_wait_budget_s=0.05)
        app = build_app(DashboardState(), instrument_store=store, summary_cache=cache)
        client = TestClient(app)

        leader_status: list[int] = []

        def _leader() -> None:
            leader_status.append(client.get("/api/v1/coverage/options").status_code)

        lead = threading.Thread(target=_leader, daemon=True)
        lead.start()
        try:
            assert scan.entered.wait(timeout=10)
            first = client.get("/api/v1/coverage/options")
            _assert_warming_503(first, "options")
            second = client.get("/api/v1/coverage/options")
            _assert_warming_503(second, "options")
            assert second.content == first.content
            assert scan.calls == 1
        finally:
            scan.release.set()
            lead.join(timeout=10)
        assert not lead.is_alive()
        assert leader_status == [200]
        assert client.get("/api/v1/coverage/options").status_code == 200

    def test_helper_body_is_the_store_outage_structural_twin(self) -> None:
        """The degrade body mirrors ``_store_outage_response``'s marker vocabulary.

        Same keys (``available``/``error``/``error_type`` + the route's
        envelope key), same 503, DISTINCT ``error_type`` — one marker
        vocabulary, never two that drift.
        """
        exc = SummaryColdWaitTimeout("spot", 60.0, latched=False)
        assert isinstance(exc, RuntimeError)
        assert not isinstance(exc, OSError)  # the except-OSError leak class
