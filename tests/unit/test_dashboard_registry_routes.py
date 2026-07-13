"""Route tests for the F&O registry browser endpoints (DEC-DERIV-REGISTRY-DB-VIEW-001).

A real DuckDB-backed :class:`~brutex.data.instrument_store.InstrumentStore` wired into
``build_app`` + a :class:`~fastapi.testclient.TestClient` (mirrors
``test_dashboard_app_backfill``), so the thin ``/api/v1/db/registry/*`` wrappers are
checked end-to-end: a 200 + honest empty-grid ``note`` when no ``registry.duckdb``
exists yet, the structured chain shape once a sibling registry is seeded, and the
HTTP-400 on a malformed ``expiry_date``. Skipped cleanly when the ``[ui]`` extra is
absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.db_view import REGISTRY_CONTRACT_COLUMNS, REGISTRY_EXPIRY_COLUMNS
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_EXP = date(2020, 1, 30)


def _nifty() -> Instrument:
    """The admitted NIFTY index fixture (one row so the store opens cleanly)."""
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
    """A real DuckDB store at ``<tmp>/instruments.duckdb`` (registry sibling lives here)."""
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


def _seed_registry(tmp_path: Path) -> None:
    """Seed the sibling ``registry.duckdb`` with one NIFTY expiry + a CE+PE pair."""
    reg = RegistryStore.open(tmp_path / "registry.duckdb")
    try:
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
    finally:
        reg.close()


def test_registry_expiries_empty_returns_note(client: TestClient) -> None:
    r = client.get("/api/v1/db/registry/expiries")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_EXPIRY_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == "no registry yet — pull expiries + contracts first"
    assert body["counts"]["exists"] is False


def test_registry_contracts_empty_returns_note(client: TestClient) -> None:
    r = client.get("/api/v1/db/registry/contracts")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_CONTRACT_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == "no registry yet — pull expiries + contracts first"


def test_registry_expiries_seeded_chain(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/expiries").json()
    assert body["total"] == 1
    assert body["note"] is None
    assert body["rows"][0][:3] == ["NSE", "NIFTY", "2020-01-30"]
    assert body["counts"] == {"expiries": 1, "contracts": 2, "exists": True, "note": None}


def test_registry_contracts_seeded_chain(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/contracts?underlying=NIFTY").json()
    assert body["total"] == 2
    assert {row[5] for row in body["rows"]} == {"CE", "PE"}
    assert body["counts"]["contracts"] == 2


def test_registry_contracts_filter_by_expiry(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/contracts?expiry_date=2020-01-30").json()
    assert body["total"] == 2


def test_registry_contracts_malformed_expiry_is_400(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    r = client.get("/api/v1/db/registry/contracts?expiry_date=2020/01/30")
    assert r.status_code == 400
    assert "expiry_date" in r.json()["detail"]


def test_registry_routes_above_int64_offset_is_200_empty_not_500(
    client: TestClient, tmp_path: Path
) -> None:
    """A hostile ``?offset=10000000000000000000`` (above int64) is a clean empty
    page, NOT an HTTP 500 (DEC-DB-VIEWER-PAGINATE-ALL-001 hostile-review MINOR-1):
    ``clamp_offset`` caps it before the DuckDB ``OFFSET`` bind, honouring the
    routes' "never a 500" promise."""
    _seed_registry(tmp_path)
    huge = 10_000_000_000_000_000_000  # > 2**63 - 1
    for route in ("/api/v1/db/registry/expiries", "/api/v1/db/registry/contracts"):
        r = client.get(f"{route}?offset={huge}")
        assert r.status_code == 200, route
        body = r.json()
        assert body["rows"] == []  # past the end → empty page
        assert body["total"] >= 1  # but the real (filtered) total is intact


def test_registry_routes_pathless_store_return_no_store_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wired-but-path-less store (no `.path`) → both routes degrade to "no store wired".

    Mirrors the existing `/db` candle/option routes' path-less guard
    (`getattr(store, "path", None)` → `not isinstance(..., Path)`); the
    `SimpleNamespace(path=None)` store reproduces it without a real DuckDB file.
    """
    from types import SimpleNamespace

    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    pathless = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=pathless)) as c:
        for route in ("/api/v1/db/registry/expiries", "/api/v1/db/registry/contracts"):
            body = c.get(route).json()
            assert body["note"] == "no store wired"
            assert body["rows"] == []
            assert body["total"] == 0


# ── DEC-DASHBOARD-EMPTY-STATE-001: corrupt registry → honest 200 empty-state ──
# A corrupt / truncated / version-incompatible ``registry.duckdb`` makes
# ``RegistryStore.open`` raise ``RegistryError`` (a ``BruteXError``, NOT a
# ``ValueError``). The ROUTE handlers (``app.py``) catch it and return an honest 200
# empty grid + the "registry unreadable" note instead of a misleading HTTP 500. These
# tests exercise the NEW ``except RegistryError`` branches in both registry routes, so
# they register in ``app.py``'s whole-package 100% coverage floor.

_UNREADABLE_NOTE = "registry unreadable — re-pull expiries + contracts"
_CORRUPT_BYTES = b"not a duckdb file\x00\x01"


def _corrupt_registry(tmp_path: Path) -> None:
    """Write GARBAGE bytes at the sibling registry path so ``RegistryStore.open`` raises."""
    (tmp_path / "registry.duckdb").write_bytes(_CORRUPT_BYTES)


def test_corrupt_registry_expiries_route_is_200_unreadable_note(
    client: TestClient, tmp_path: Path
) -> None:
    _corrupt_registry(tmp_path)
    r = client.get("/api/v1/db/registry/expiries?underlying=NIFTY")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_EXPIRY_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == _UNREADABLE_NOTE
    # counts also degrade honestly; the file IS present, just damaged.
    assert body["counts"]["note"] == _UNREADABLE_NOTE
    assert body["counts"]["exists"] is True


def test_corrupt_registry_contracts_route_is_200_unreadable_note(
    client: TestClient, tmp_path: Path
) -> None:
    _corrupt_registry(tmp_path)
    r = client.get("/api/v1/db/registry/contracts?underlying=NIFTY")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_CONTRACT_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == _UNREADABLE_NOTE
    assert body["counts"]["note"] == _UNREADABLE_NOTE


def test_corrupt_registry_contracts_malformed_date_still_400_not_unreadable(
    client: TestClient, tmp_path: Path
) -> None:
    """A malformed ``expiry_date`` is parsed BEFORE the registry is opened, so even with
    a corrupt registry on disk the contracts route still returns the HTTP-400 contract —
    the unreadable guard never masks the date-validation error."""
    _corrupt_registry(tmp_path)
    r = client.get("/api/v1/db/registry/contracts?expiry_date=2020/01/30")
    assert r.status_code == 400
    assert "expiry_date" in r.json()["detail"]


# ── H1 route-level count cache (DEC-O1-GUARANTEE-HARDEN-001) ────────────────


def _count_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every real ``COUNT(*)`` the routes trigger, per store method."""
    calls = {"expiries": 0, "contracts": 0}
    real_exp = RegistryStore.count_expiries
    real_con = RegistryStore.count_contracts

    def _spy_exp(self: RegistryStore, **kwargs: object) -> int:
        calls["expiries"] += 1
        return real_exp(self, **kwargs)  # type: ignore[arg-type]

    def _spy_con(self: RegistryStore, **kwargs: object) -> int:
        calls["contracts"] += 1
        return real_con(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RegistryStore, "count_expiries", _spy_exp)
    monkeypatch.setattr(RegistryStore, "count_contracts", _spy_con)
    return calls


def test_registry_expiries_repeat_click_counts_once(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeat/paging clicks within one registry revision run ONE COUNT pair.

    DEC-O1-GUARANTEE-HARDEN-001 H1: the filtered total + the nav-badge counts
    are memoised on the registry-content stat token — the pre-fix route ran
    a store open + three COUNTs on EVERY click.
    """
    _seed_registry(tmp_path)
    calls = _count_spies(monkeypatch)
    first = client.get("/api/v1/db/registry/expiries").json()
    assert first["total"] == 1
    baseline = dict(calls)
    for offset in (0, 0, 1):
        page = client.get(f"/api/v1/db/registry/expiries?offset={offset}").json()
        assert page["total"] == 1
        assert page["counts"]["expiries"] == 1
    assert calls == baseline  # zero further COUNTs — O(1)-on-repeat


def test_registry_contracts_repeat_click_counts_once(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_registry(tmp_path)
    calls = _count_spies(monkeypatch)
    first = client.get("/api/v1/db/registry/contracts?underlying=NIFTY").json()
    assert first["total"] == 2
    baseline = dict(calls)
    for _ in range(3):
        page = client.get("/api/v1/db/registry/contracts?underlying=NIFTY").json()
        assert page["total"] == 2
    assert calls == baseline


def test_registry_write_moves_the_token_and_recounts(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry WRITE (new expiry) is seen on the next click — never stale."""
    _seed_registry(tmp_path)
    assert client.get("/api/v1/db/registry/expiries").json()["total"] == 1
    with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
        reg.upsert_expiry("NSE", "NIFTY", date(2020, 2, 27), run_id="r2", fetched_at=_NOW)
    fresh = client.get("/api/v1/db/registry/expiries").json()
    assert fresh["total"] == 2
    assert fresh["counts"]["expiries"] == 2


def test_registry_probe_outage_serves_uncached_legacy(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``registry_revision`` outage (None) computes fresh UNCACHED per click."""
    _seed_registry(tmp_path)
    # String-target setattr — avoids re-importing brutex.dashboard.app with
    # BOTH `import` and `from … import` styles (CodeQL py/import-and-import-from).
    monkeypatch.setattr("brutex.dashboard.app.registry_revision", lambda path: None)
    calls = _count_spies(monkeypatch)
    for _ in range(2):
        page = client.get("/api/v1/db/registry/expiries").json()
        assert page["total"] == 1
    # Legacy per-click behaviour: each click pays its own COUNT pair
    # (1 filtered expiries + the nav-badge expiries+contracts pair).
    assert calls == {"expiries": 4, "contracts": 2}
