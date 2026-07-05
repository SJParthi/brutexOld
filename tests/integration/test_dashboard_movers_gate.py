"""``/api/v1/movers`` under the PREBUILD GATE (DEC-PIPELINE-AUTOPREBUILD-001, chunk 2).

End-to-end through FastAPI's :class:`TestClient` with an INJECTED
:class:`~brutex.dashboard.prebuild_gate.PrebuildGate` (the ``build_app`` seam):
a gated route answers 200 ``gated: true`` with ZERO candidate computation (spied
— no lake read while the pipeline is writing), every asset tab is gated alike,
a release restores the normal payload, and the malformed-date 400 contract
survives the gated path. The pipeline-side begin/release binding is covered in
``tests/unit/test_dashboard_pull_pipeline_route.py``.

Skipped when ``fastapi`` / ``duckdb`` are absent.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.prebuild_gate import PrebuildGate
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


@pytest.fixture
def gate() -> PrebuildGate:
    return PrebuildGate()


@pytest.fixture
def client(store: InstrumentStore, gate: PrebuildGate) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store, prebuild_gate=gate)) as c:
        yield c


def _spy_compute(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace all three candidate computes with recorders that must NOT run gated."""
    calls: list[str] = []

    def _make(name: str) -> Any:
        def _spy(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not be computed while the gate blocks")

        return _spy

    monkeypatch.setattr(app_module, "compute_spot_candidates", _make("spot"))
    monkeypatch.setattr(app_module, "compute_futures_candidates", _make("futures"))
    monkeypatch.setattr(app_module, "compute_options_candidates", _make("options"))
    return calls


class TestMoversGate:
    def test_gated_returns_honest_payload_without_computing(
        self, client: TestClient, gate: PrebuildGate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_compute(monkeypatch)
        gate.begin()
        body = client.get("/api/v1/movers", params={"asset": "spot", "date": "2024-06-13"}).json()
        assert body["gated"] is True
        assert "movers stay gated until the final prebuild AND the movers warm" in body["note"]
        assert body["instrument_count"] == 0
        assert "price_gainers" not in body  # zero ranked lists, zero lake reads
        assert calls == []  # the spies prove NO candidate computation happened

    def test_gated_covers_every_asset_tab(
        self, client: TestClient, gate: PrebuildGate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_compute(monkeypatch)
        gate.begin()
        for asset in ("spot", "futures", "options"):
            body = client.get("/api/v1/movers", params={"asset": asset}).json()
            assert body["gated"] is True, f"{asset} must be gated"
        assert calls == []

    def test_release_restores_normal_payload(self, client: TestClient, gate: PrebuildGate) -> None:
        gate.begin()
        assert client.get("/api/v1/movers").json().get("gated") is True
        gate.release(True)
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        # The normal (empty-lake) spot payload — not the gated shell.
        assert "gated" not in body
        assert "price_gainers" in body
        assert body["stored_reads"] == 0
        assert body["resampled_reads"] == 0

    def test_gated_malformed_date_is_still_400(
        self, client: TestClient, gate: PrebuildGate
    ) -> None:
        gate.begin()
        resp = client.get("/api/v1/movers", params={"date": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_idle_gate_never_intercepts(self, client: TestClient) -> None:
        # Regression pin: with no pipeline running the gate is transparent.
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert "gated" not in body
        assert "price_gainers" in body


class TestMoversGatePhaseNote:
    """Phase-aware gated notes end-to-end (DEC-PIPELINE-PHASE-UI-HONESTY-001).

    The route reads the pipeline's CURRENT coarse phase via the injectable
    ``pull_phase_probe`` seam (production default = this app's own
    ``BackgroundPullJob.snapshot().phase``) and threads it into
    ``gated_movers_payload`` — so the gated note names what is ACTUALLY running
    instead of a blanket "prebuild in progress" during the fetch phases.
    """

    def test_gated_note_names_the_injected_phase(
        self, store: InstrumentStore, gate: PrebuildGate
    ) -> None:
        phase_holder = {"phase": "spot"}
        app = build_app(
            DashboardState(),
            instrument_store=store,
            prebuild_gate=gate,
            pull_phase_probe=lambda: phase_holder["phase"],
        )
        with TestClient(app) as client:
            gate.begin()
            body = client.get(
                "/api/v1/movers", params={"asset": "spot", "date": "2024-06-13"}
            ).json()
            assert body["gated"] is True
            assert body["phase"] == "spot"
            assert "phase: spot 1-minute fetch" in body["note"]
            assert "prebuild in progress" not in body["note"]
            # The phase moves → the very next poll's note moves with it.
            phase_holder["phase"] = "historical"
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert body["phase"] == "historical"
            assert "phase: F&O historical 1-minute fetch" in body["note"]

    def test_default_probe_reads_idle_job_as_generic_note(
        self, client: TestClient, gate: PrebuildGate
    ) -> None:
        # No probe injected → the production default reads this app's own
        # BackgroundPullJob phase ("" — nothing in flight) → the byte-compatible
        # generic note + an empty phase echo (never a fabricated phase claim).
        gate.begin()
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert body["gated"] is True
        assert body["phase"] == ""
        assert "prebuild in progress — movers available when finished" in body["note"]
