"""Route-level serve-last-known under a revision-probe outage (DEC-CACHE-SERVE-LAST-KNOWN-001).

RECONCILE NOTE (the #1406 club merge): the four oneclick route classes this
file originally carried (runs / manifest / top100 / events outage → the
labelled ``revision_probe`` marker over ``MoversSnapshotCache.serve_last_known``)
were SUPERSEDED by main's landed DEC-ONECLICK-PERF-CLUB-001 doctrine — those
routes now ride ``oneclick_artifacts.serve_revision_cached`` + the shared
``OutageGate`` and their outage/recovery behaviour is pinned by
``tests/integration/test_oneclick_artifacts_routes.py`` (byte-equal last-known
serve, one ``oneclick.probe_outage`` WARN per coordinate per outage). The
SURVIVING batch-A oneclick arm is the :class:`TradeStoreReader` top100
probe-outage split (main's reader was outage-blind: a stalled volume meant a
silent uncached full re-parse per trades request) — this file pins that seam AT
THE WIRE: the ``/api/v1/oneclick/trades`` detail NAMES the top100 probe outage
while serving the last-known memo, with the reader-owned gate's once-per-outage
WARN. The labelled ``revision_probe`` marker sites (non-oneclick keystones)
keep their own suites (``test_dashboard_results_revision_key.py``,
``test_dashboard_universe_outage.py``, ``test_dashboard_nav_floor_rekey.py``,
``test_movers_cache_serve_last_known.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard import oneclick_artifacts as oc
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs


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
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c


def _write_corrupt_top100(lake_root: Path, run_id: str = "r-live1") -> Path:
    """One sweep_runs/<run_id> whose top100 parses to a memoised CORRUPT verdict."""
    run_dir = lake_root / oc.SWEEP_RUNS_DIRNAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / oc.TOP100_FILENAME).write_text("{not json", encoding="utf-8")
    return run_dir


class TestOneclickTradesTop100Outage:
    """The trades route NAMES a top100 probe outage in ``detail`` (wire pin)."""

    def test_outage_note_rides_the_trades_detail_with_one_warn(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Primed memo + probe outage → last-known verdict served, note + ONE WARN.

        Fails-before (pre-batch-A reader): the outage was invisible — the
        reader re-parsed the artifact per request with no note and no WARN.
        """
        lake_root = store.path.parent
        run_dir = _write_corrupt_top100(lake_root)
        primed = client.get("/api/v1/oneclick/trades", params={"run_id": "r-live1", "rank": 1})
        assert primed.status_code == 200
        body = primed.json()
        assert body["status"] == "corrupt"
        assert "outage" not in body["detail"]
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))
        with capture_logs() as logs:
            for _ in range(3):
                served = client.get(
                    "/api/v1/oneclick/trades", params={"run_id": "r-live1", "rank": 1}
                ).json()
                assert served["status"] == "corrupt"  # the last-known memoised verdict
                assert "top100 revision probe outage — serving last-known" in served["detail"]
        warns = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(warns) == 1  # the reader-owned gate latches — never per poll
        assert warns[0]["coordinate"] == str(run_dir / oc.TOP100_FILENAME)

    def test_recovery_drops_the_note_and_logs_recovered(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_root = store.path.parent
        _write_corrupt_top100(lake_root)
        first = client.get("/api/v1/oneclick/trades", params={"run_id": "r-live1", "rank": 1})
        assert first.json()["status"] == "corrupt"
        real_probe = oc.artifact_probe
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))
        outage = client.get("/api/v1/oneclick/trades", params={"run_id": "r-live1", "rank": 1})
        assert "serving last-known" in outage.json()["detail"]
        monkeypatch.setattr(oc, "artifact_probe", real_probe)
        with capture_logs() as logs:
            recovered = client.get(
                "/api/v1/oneclick/trades", params={"run_id": "r-live1", "rank": 1}
            ).json()
        assert "outage" not in recovered["detail"]
        assert any(e["event"] == "oneclick.probe_recovered" for e in logs)
