"""Route-level serve-last-known under a revision-probe outage (DEC-CACHE-SERVE-LAST-KNOWN-001).

Drives the ``/api/v1/oneclick/*`` routes through the REAL app + TestClient and
proves the keystone at the wire: a probe OUTAGE (a stalled lake volume — the
stat probe raises a non-missing-file :class:`OSError`) serves the LAST-KNOWN
payload labelled with the additive ``revision_probe`` degrade marker and ONE
latched ``movers_cache.revision_probe_outage`` WARN — never the pre-change
per-poll compute storm (fails-before: on the pre-fix tree the compute spies
here count one full ``discover_runs``/``load_top100`` per poll and the marker
key does not exist). An ABSENT artifact (the probe answered "no file") keeps
the old cheap fresh-uncached behaviour — never a stale serve of a deleted
artifact. The ``/results`` twin lives in
``test_dashboard_results_revision_key.py`` (T-c6, rewritten).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard import oneclick_artifacts as oc
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs
from structlog.typing import EventDict


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


def _top100_doc(run_id: str) -> dict[str, Any]:
    """The WRITER's object root (DEC-ONECLICK-TOP100-OBJECT-ROOT-001 shape law)."""
    return {
        "schema_version": oc.TOP100_SCHEMA_VERSION,
        "run_id": run_id,
        "preset": "min_dd_first",
        "source": {"pool": 0},
        "baselines": {},
        "entries": [],
    }


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


@pytest.fixture(autouse=True)
def _rearm_probe_latch() -> Iterator[None]:
    """Deterministic module-latch state per test."""
    oc._PROBE_OUTAGE_LATCH.reset()
    yield
    oc._PROBE_OUTAGE_LATCH.reset()


def _write_run(lake_root: Path, run_id: str = "r-live1") -> Path:
    """One sweep_runs/<run_id> with a valid (empty-rows) top100 artifact."""
    run_dir = lake_root / oc.SWEEP_RUNS_DIRNAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / oc.TOP100_FILENAME).write_text(json.dumps(_top100_doc(run_id)), encoding="utf-8")
    return run_dir


def _outage_warns(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "movers_cache.revision_probe_outage"]


class TestOneclickRunsOutage:
    def test_outage_serves_last_known_with_marker_and_one_warn(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Primed listing + probe outage → zero extra discovers, marker, ONE WARN.

        Fails-before: the pre-fix route computed a full ``discover_runs`` on
        EVERY poll under a ``None`` revision, with no marker and no WARN.
        """
        lake_root = store.path.parent
        _write_run(lake_root)
        calls = {"discover": 0}
        real_discover = oc.discover_runs

        def _spy(results_root: Path) -> Any:
            calls["discover"] += 1
            return real_discover(results_root)

        monkeypatch.setattr(oc, "discover_runs", _spy)
        primed = client.get("/api/v1/oneclick/runs").json()
        assert primed["status"] == "ok"
        assert "revision_probe" not in primed
        assert calls["discover"] == 1
        monkeypatch.setattr(oc, "runs_probe", lambda _root: oc.ArtifactProbe(None, True))
        with capture_logs() as logs:
            for _ in range(3):
                payload = client.get("/api/v1/oneclick/runs").json()
                assert payload["status"] == "ok"  # the last-known listing
                assert payload["revision_probe"] == "outage — serving last-known"
        assert calls["discover"] == 1  # ZERO extra walks across the outage
        assert len(_outage_warns(logs)) == 1  # latched — never per-poll
        # Recovery: the real probe again → clean payload, no marker.
        monkeypatch.undo()
        monkeypatch.setattr(oc, "discover_runs", _spy)
        recovered = client.get("/api/v1/oneclick/runs").json()
        assert recovered["status"] == "ok"
        assert "revision_probe" not in recovered

    def test_cold_outage_seeds_once(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage with NO primed listing computes once, then serves the seed."""
        lake_root = store.path.parent
        _write_run(lake_root)
        calls = {"discover": 0}
        real_discover = oc.discover_runs

        def _spy(results_root: Path) -> Any:
            calls["discover"] += 1
            return real_discover(results_root)

        monkeypatch.setattr(oc, "discover_runs", _spy)
        monkeypatch.setattr(oc, "runs_probe", lambda _root: oc.ArtifactProbe(None, True))
        first = client.get("/api/v1/oneclick/runs").json()
        assert first["revision_probe"] == "outage — computed fresh (seeded as last-known)"
        second = client.get("/api/v1/oneclick/runs").json()
        assert second["revision_probe"] == "outage — serving last-known"
        assert calls["discover"] == 1  # the seed served — no per-poll storm

    def test_absent_tree_stays_fresh_uncached(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sweep_runs/ tree at all: awaiting payload, computed per poll, no marker."""
        calls = {"discover": 0}
        real_discover = oc.discover_runs

        def _spy(results_root: Path) -> Any:
            calls["discover"] += 1
            return real_discover(results_root)

        monkeypatch.setattr(oc, "discover_runs", _spy)
        for _ in range(2):
            payload = client.get("/api/v1/oneclick/runs").json()
            assert payload["status"] == "awaiting"
            assert "revision_probe" not in payload
        assert calls["discover"] == 2  # cheap fresh computes — nothing cached


class TestOneclickTop100Outage:
    def test_outage_serves_last_known_page(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails-before: every outage poll re-parsed the whole top100 artifact."""
        lake_root = store.path.parent
        _write_run(lake_root, run_id="r-live1")
        calls = {"parse": 0}
        real_load = oc.load_top100

        def _spy(run_dir: Path) -> Any:
            calls["parse"] += 1
            return real_load(run_dir)

        monkeypatch.setattr(oc, "load_top100", _spy)
        primed = client.get("/api/v1/oneclick/top100", params={"run_id": "r-live1"}).json()
        assert primed["status"] == "ok"
        assert calls["parse"] == 1
        real_probe = oc.artifact_probe

        def _outage_on_top100(path: Path) -> oc.ArtifactProbe:
            if path.name == oc.TOP100_FILENAME:
                return oc.ArtifactProbe(None, True)
            return real_probe(path)

        monkeypatch.setattr(oc, "artifact_probe", _outage_on_top100)
        with capture_logs() as logs:
            for _ in range(3):
                payload = client.get("/api/v1/oneclick/top100", params={"run_id": "r-live1"}).json()
                assert payload["status"] == "ok"
                assert payload["revision_probe"] == "outage — serving last-known"
        assert calls["parse"] == 1  # zero re-parses across the outage
        assert len(_outage_warns(logs)) == 1

    def test_absent_artifact_stays_fresh_uncached(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run dir with NO top100.json keeps the cheap awaiting compute per poll."""
        lake_root = store.path.parent
        run_dir = _write_run(lake_root, run_id="r-live1")
        (run_dir / oc.TOP100_FILENAME).unlink()
        calls = {"parse": 0}
        real_load = oc.load_top100

        def _spy(run_dir: Path) -> Any:
            calls["parse"] += 1
            return real_load(run_dir)

        monkeypatch.setattr(oc, "load_top100", _spy)
        for _ in range(2):
            payload = client.get("/api/v1/oneclick/top100", params={"run_id": "r-live1"}).json()
            assert payload["status"] == "awaiting"
            assert "revision_probe" not in payload
        assert calls["parse"] == 2  # fresh (cheap) awaiting parse per poll — uncached
