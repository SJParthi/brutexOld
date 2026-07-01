"""Honest empty-state for a corrupt / missing registry + the credentials first-run hint.

Guards **DEC-DASHBOARD-EMPTY-STATE-001**. A corrupt / unreadable ``registry.duckdb``
makes :meth:`~brutex.data.registry_store.RegistryStore.open` raise ``RegistryError``
(a ``BruteXError``, NOT a ``ValueError``), which previously propagated uncaught out of
the ``/api/v1/db/registry/*`` routes as an HTTP 500 — and the ``/universe`` panel, which
read the body WITHOUT checking ``r.ok``, then showed a misleading "No expiries pulled
yet" empty message. The ``db_view`` registry readers now catch ``RegistryError`` in ONE
COMMON guard (``_open_registry_or_none``) and degrade to the SAME honest empty grid the
missing-file path returns, but with a DISTINCT note ("registry unreadable — …") so
empty ≠ corrupt is distinguishable. The frontend gains an ``r.ok`` guard; the
credentials page gains a first-run empty-state banner.

Backend (``db_view`` + the routes) must stay 100% line+branch; frontend behaviour is
asserted on the RENDERED template string (the established pattern — JS is not executed
in this repo). Skipped cleanly when the ``[ui]`` extra is absent.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import (
    DashboardState,
    _render_universe_html,
    build_app,
)
from brutex.dashboard.db_view import REGISTRY_DB_NAME
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_EXP = date(2020, 1, 30)
_CORRUPT_BYTES = b"not a duckdb file\x00\x01"
_UNREADABLE_NOTE = "registry unreadable — re-pull expiries + contracts"
_MISSING_NOTE = "no registry yet — pull expiries + contracts first"


def _nifty() -> Instrument:
    """One admitted NIFTY index row so the instrument store opens cleanly."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
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
    """A real DuckDB instrument store; the registry is its on-disk sibling."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()],
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
    """A TestClient over the wired store; credentials redirected to an empty tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _registry_path(tmp_path: Path) -> Path:
    """The sibling ``registry.duckdb`` path the routes resolve."""
    return tmp_path / REGISTRY_DB_NAME


def _corrupt_registry(tmp_path: Path) -> Path:
    """Write GARBAGE bytes at the registry path so ``RegistryStore.open`` raises."""
    path = _registry_path(tmp_path)
    path.write_bytes(_CORRUPT_BYTES)
    return path


def _seed_registry(tmp_path: Path) -> None:
    """Seed a VALID sibling registry with one NIFTY expiry + a CE+PE pair."""
    with RegistryStore.open(_registry_path(tmp_path)) as reg:
        reg.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r1", fetched_at=_NOW)
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "NSE-NIFTY-30Jan20-12100-CE",
            side="CE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "NSE-NIFTY-30Jan20-12100-PE",
            side="PE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )


# ── CHANGE 1: route — corrupt registry → 200 honest note, NOT 500 ────────────


def test_corrupt_registry_expiries_route_is_200_unreadable_note(
    client: TestClient, tmp_path: Path
) -> None:
    _corrupt_registry(tmp_path)
    r = client.get("/api/v1/db/registry/expiries?underlying=NIFTY")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert "unreadable" in body["note"]
    assert body["note"] == _UNREADABLE_NOTE
    # counts also degrade honestly (the route calls registry_counts on the same path)
    assert body["counts"]["note"] == _UNREADABLE_NOTE
    assert body["counts"]["exists"] is True


def test_corrupt_registry_contracts_route_is_200_unreadable_note(
    client: TestClient, tmp_path: Path
) -> None:
    _corrupt_registry(tmp_path)
    r = client.get("/api/v1/db/registry/contracts?underlying=NIFTY")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == _UNREADABLE_NOTE
    assert body["counts"]["note"] == _UNREADABLE_NOTE


# ── regression: missing + empty + valid registry are byte-unchanged ──────────


def test_missing_registry_expiries_route_keeps_missing_note(
    client: TestClient, tmp_path: Path
) -> None:
    assert not _registry_path(tmp_path).exists()
    body = client.get("/api/v1/db/registry/expiries").json()
    assert body["rows"] == []
    assert body["note"] == _MISSING_NOTE
    assert body["counts"]["exists"] is False


def test_empty_seeded_registry_expiries_route_has_no_note(
    client: TestClient, tmp_path: Path
) -> None:
    # An EMPTY but VALID registry file (opened then closed with no rows seeded).
    RegistryStore.open(_registry_path(tmp_path)).close()
    body = client.get("/api/v1/db/registry/expiries").json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] is None
    assert body["counts"] == {"expiries": 0, "contracts": 0, "exists": True, "note": None}


def test_valid_seeded_registry_expiries_route_has_rows(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/expiries").json()
    assert body["total"] == 1
    assert body["note"] is None
    assert body["rows"][0][:3] == ["NSE", "NIFTY", "2020-01-30"]


def test_valid_seeded_registry_contracts_route_has_rows(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/contracts?underlying=NIFTY").json()
    assert body["total"] == 2
    assert {row[5] for row in body["rows"]} == {"CE", "PE"}


# ── property: ANY bytes → never a 500 ────────────────────────────────────────


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(payload=st.binary(max_size=64))
def test_arbitrary_registry_bytes_never_500(
    client: TestClient, tmp_path: Path, payload: bytes
) -> None:
    """For ARBITRARY bytes written as ``registry.duckdb`` the expiries route is
    ALWAYS a 200 with a list ``rows`` — never an HTTP 500. A few random byte blobs
    open as an empty DuckDB (note ``None``), most raise ``RegistryError`` (unreadable
    note); either way the route degrades honestly."""
    _registry_path(tmp_path).write_bytes(payload)
    r = client.get("/api/v1/db/registry/expiries?underlying=NIFTY")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["rows"], list)
    assert body["rows"] == []


# ── bench / O(1) honesty: the guard returns immediately, no row scan ──────────


def test_corrupt_registry_guard_is_o1_independent_of_size(
    client: TestClient, tmp_path: Path
) -> None:
    """The route-level corrupt-registry guard fails at ``open`` (before any row scan),
    so the response cost does NOT grow with the file size — a 64-byte garbage file and
    a 256 KiB garbage file both return in constant work. Honest envelope: this asserts
    the guard does not SCALE, not a hard wall-clock budget."""
    path = _registry_path(tmp_path)

    def _elapsed(blob: bytes) -> float:
        path.write_bytes(blob)
        start = time.perf_counter()
        r = client.get("/api/v1/db/registry/expiries?underlying=NIFTY")
        assert r.json()["note"] == _UNREADABLE_NOTE
        return time.perf_counter() - start

    t_small = max(_elapsed(_CORRUPT_BYTES), 1e-6)
    t_big = _elapsed(b"\x00\x01\x02\x03" * 65536)  # 256 KiB
    # 4096× more bytes must NOT make the guard 4096× slower — it never reads the body.
    assert t_big < t_small * 50


# ── CHANGE 2 + 3: rendered-template string assertions ────────────────────────


def test_universe_template_has_r_ok_guard_for_expiries() -> None:
    html = _render_universe_html()
    assert "if (!r.ok)" in html
    assert "registry may be unreadable" in html
    # the empty-state hint stays clear + still prefers the route's own note
    assert "No expiries pulled yet" in html
    assert "body.note ||" in html


def test_universe_template_has_r_ok_guard_for_contracts() -> None:
    html = _render_universe_html()
    # both the expiries and contracts fetch now guard r.ok (two distinct messages)
    assert html.count("if (!r.ok)") >= 2
    assert "couldn't load contracts" in html
