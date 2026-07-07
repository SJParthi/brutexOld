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
        # DEC-MOVERS-WARM-UNBLOCK-001: the note states the REAL release condition
        # — the gate lifts at prebuild completion; the warm continues un-gated.
        assert "movers stay gated until the final prebuild finishes" in body["note"]
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

    Post-merge semantics: main's DEC-PREBUILD-GATE-HONEST-NOTE-001 supersedes
    the branch's ``_PHASE_NOTES`` map — the gate's OWN ``set_phase`` annotation
    feeds the gated payload's ``phase`` echo and the "(current stage: …)" note
    clause, so the gated note still names what is ACTUALLY running instead of a
    blanket "prebuild in progress" during the fetch phases.
    """

    def test_gated_note_names_the_injected_phase(
        self, store: InstrumentStore, gate: PrebuildGate
    ) -> None:
        app = build_app(
            DashboardState(),
            instrument_store=store,
            prebuild_gate=gate,
        )
        with TestClient(app) as client:
            gate.begin()
            gate.set_phase("spot")
            body = client.get(
                "/api/v1/movers", params={"asset": "spot", "date": "2024-06-13"}
            ).json()
            assert body["gated"] is True
            assert body["phase"] == "spot"
            assert "(current stage: spot)" in body["note"]
            assert "prebuild in progress" not in body["note"]
            # The phase moves → the very next poll's note moves with it.
            gate.set_phase("historical")
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert body["phase"] == "historical"
            assert "(current stage: historical)" in body["note"]

    def test_default_probe_reads_idle_job_as_generic_note(
        self, client: TestClient, gate: PrebuildGate
    ) -> None:
        # No finer phase annotated → begin() opens at the coarse "pull" phase
        # and the note carries the generic release condition (never a
        # fabricated finer-stage claim).
        gate.begin()
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert body["gated"] is True
        assert body["phase"] == "pull"
        assert "(current stage: pull)" in body["note"]
        assert "movers stay gated until the final prebuild finishes" in body["note"]


class TestCrossProcessDeriveGate:
    """Derive-lock VISIBILITY + honest ATTRIBUTION end-to-end
    (DEC-DERIVE-LOCK-VISIBILITY-001 + the round-1 review fold).

    The DEFAULT (un-injected) gate built by ``build_app`` wires BOTH probes
    over the store's lake root: the same-process reentrancy-registry read
    (``derive_lock_held_by_this_process`` — consulted FIRST, zero I/O) and
    the cross-process flock probe (``derive_in_progress``). A derive holding
    the fixed ``<lake_root>/.locks/derive.lock`` yields the honest
    ``gated: true`` movers payload with ZERO candidate computation either
    way, ATTRIBUTED per holder: this process's own ``derive_write_lock`` (the
    lake-heal recall shape) reads ``source: "local_derive"``; a raw flock
    taken WITHOUT the registry (exactly what a foreign process's holder looks
    like — ``fcntl.flock`` is per open file description, and a foreign
    process never touches OUR registry) reads ``source: "cross_process"``.
    """

    def test_same_process_derive_lock_gates_as_local_derive_then_releases(
        self, store: InstrumentStore
    ) -> None:
        # The round-1 review regression: pre-fix this read source="cross_process"
        # with a note claiming "another process" — a false provenance claim for
        # the dashboard's OWN lake-heal recall (Golden Rule #1).
        from brutex.data.derive_lock import derive_write_lock

        # NO injected gate — the production default (probe-wired) gate.
        app = build_app(DashboardState(), instrument_store=store)
        lake_root = store.path.parent
        with TestClient(app) as client:
            # The compute spies live ONLY inside the held-lock window (the
            # post-release recovery request below computes legitimately).
            with pytest.MonkeyPatch.context() as mp, derive_write_lock(lake_root):
                calls = _spy_compute(mp)
                body = client.get(
                    "/api/v1/movers", params={"asset": "spot", "date": "2024-06-13"}
                ).json()
                assert body["gated"] is True
                assert body["source"] == "local_derive"
                assert body["phase"] == "derive"
                assert "this dashboard process" in body["note"]
                assert "another process" not in body["note"]
                # Honest unknowns — a lock probe cannot know the holder's start.
                assert body["started_at"] is None
                assert body["gated_for_s"] is None
                assert calls == []  # zero candidate computation while gated
            # Lock released → the very next poll serves the normal payload.
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert "gated" not in body
            assert "price_gainers" in body

    def test_foreign_style_derive_lock_gates_as_cross_process(self, store: InstrumentStore) -> None:
        # A raw exclusive flock taken WITHOUT the process-local registry is
        # indistinguishable from a SEPARATE process's holder (the registry is
        # per-process; a foreign process never touches ours) — the honest
        # cross_process attribution survives the fold.
        import fcntl
        import os

        app = build_app(DashboardState(), instrument_store=store)
        lake_root = store.path.parent
        lock_path = lake_root / ".locks" / "derive.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with TestClient(app) as client, pytest.MonkeyPatch.context() as mp:
                calls = _spy_compute(mp)
                body = client.get(
                    "/api/v1/movers", params={"asset": "spot", "date": "2024-06-13"}
                ).json()
                assert body["gated"] is True
                assert body["source"] == "cross_process"
                assert body["phase"] == "derive"
                assert "another process" in body["note"]
                assert "cross-process derive lock" in body["note"]
                assert calls == []
        finally:
            os.close(fd)  # releases the flock

    def test_no_lake_root_store_never_probes_never_gates(self) -> None:
        # The probe's no-real-Path arm: a store without a lake root has nothing
        # to probe — the route serves the honest empty payload, never a gate.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        with TestClient(build_app(DashboardState(), instrument_store=fake)) as client:
            body = client.get("/api/v1/movers").json()
            assert "gated" not in body
            assert body["instrument_count"] == 0

    def test_injected_gate_stays_purely_process_local(
        self, store: InstrumentStore, gate: PrebuildGate
    ) -> None:
        # Back-compat pin: an INJECTED probe-less gate (every pre-existing
        # test / caller) ignores a held derive lock — byte-identical behaviour.
        from brutex.data.derive_lock import derive_write_lock

        app = build_app(DashboardState(), instrument_store=store, prebuild_gate=gate)
        with TestClient(app) as client, derive_write_lock(store.path.parent):
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert "gated" not in body
            assert "price_gainers" in body
