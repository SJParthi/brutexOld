"""Route-level store-OUTAGE degrades for the `/universe` + nav-latency-trio reads.

DEC-CACHE-SERVE-LAST-KNOWN-001, the 8th keystone site — adapted from the scout's
`repro_instruments_500storm.py` (verified pre-fix on merged main):

* **S2** warm-then-outage: a resident snapshot + a wedged revision probe served
  a 500 PER POLL despite the good snapshot (probe-before-snapshot ordering) —
  now a labelled 200 (`revision_probe` marker) + ONE latched WARN.
* **S1** cold outage: raw ``duckdb.Error`` traceback → 500 per poll — now the
  honest 503 markers (`error_type: store_outage`), never a raw 500, never a
  fabricated empty universe.
* **S3** partial (base-read) outage warm/cold twins.
* **S3b** the nav-latency trio (`/api/v1/coverage` + `/api/v1/universe/completeness`
  + `/api/v1/universe/mapping-integrity[.csv]`) under the SAME probe outage —
  `_nav_latency_coords` called ``master_revision`` unguarded, so the trio 500'd
  together — now serve-last-known labelled / honest 503 via
  ``MoversSnapshotCache.serve_last_known``.
* Recovery: the next good probe recomputes fresh (no marker) + one INFO.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="these routes read a real DuckDB instrument store")
pytest.importorskip("fastapi", reason="dashboard routes need FastAPI")

import duckdb
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient
from structlog.testing import capture_logs
from structlog.typing import EventDict


class _WedgeableStore:
    """A delegating wrapper over a REAL :class:`InstrumentStore` with wedge toggles.

    ``wedge_probe`` wedges the DuckDB ``connection`` (every ``execute`` raises —
    the whole-volume outage shape the scout simulated); ``wedge_base`` wedges the
    three lean base reads + ``list_coverage`` (the partial-outage shape). The
    delegation keeps every OTHER store surface real, so the warm (healthy) route
    payloads are the production ones.
    """

    def __init__(self, real: InstrumentStore) -> None:
        self._real = real
        self.wedge_probe = False
        self.wedge_base = False

    @property
    def path(self) -> Path:
        return self._real.path

    @property
    def connection(self) -> Any:
        if not self.wedge_probe:
            return self._real.connection

        class _WedgedConn:
            def execute(self, *_args: Any, **_kwargs: Any) -> Any:
                raise duckdb.Error("IO Error: simulated wedged volume (connection)")

        return _WedgedConn()

    def _guard(self) -> None:
        if self.wedge_base:
            raise duckdb.Error("IO Error: simulated wedged volume (base read)")

    def fno_eligible_underlyings(self, *args: Any, **kwargs: Any) -> Any:
        self._guard()
        return self._real.fno_eligible_underlyings(*args, **kwargs)

    def list_index_equity_instruments(self, *args: Any, **kwargs: Any) -> Any:
        self._guard()
        return self._real.list_index_equity_instruments(*args, **kwargs)

    def reference_only_security_ids(self, *args: Any, **kwargs: Any) -> Any:
        self._guard()
        return self._real.reference_only_security_ids(*args, **kwargs)

    def list_coverage(self, *args: Any, **kwargs: Any) -> Any:
        self._guard()
        return self._real.list_coverage(*args, **kwargs)

    def get_meta(self, key: str) -> Any:
        return self._real.get_meta(key)


@pytest.fixture
def wedgeable(tmp_path: Path) -> Any:
    """A dashboard client over a wedgeable real store (empty universe, real lake dir)."""
    real = InstrumentStore.open(tmp_path / "lake" / "instruments.duckdb")
    store = _WedgeableStore(real)
    app = build_app(DashboardState(), instrument_store=store)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield store, client
    finally:
        client.close()
        real.close()


def _universe_outage_warns(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "universe_cache.store_probe_outage"]


def _movers_outage_warns(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == "movers_cache.revision_probe_outage"]


# ── /universe summary + instruments (the UniverseSnapshotCache pair) ─────────────


def test_warm_then_probe_outage_serves_labelled_200_with_one_warn(wedgeable: Any) -> None:
    """S2: a resident snapshot + probe outage → labelled 200s, ONE WARN — not 3×500."""
    store, client = wedgeable
    warm_rows = client.get("/api/v1/universe/instruments")
    warm_sum = client.get("/api/v1/universe/summary")
    assert warm_rows.status_code == 200 and warm_sum.status_code == 200
    assert "revision_probe" not in warm_rows.json()  # clean payloads carry NO marker
    assert "revision_probe" not in warm_sum.json()
    store.wedge_probe = True
    with capture_logs() as logs:
        for _ in range(3):
            r = client.get("/api/v1/universe/instruments")
            assert r.status_code == 200
            body = r.json()
            assert body["instruments"] == warm_rows.json()["instruments"]
            assert body["revision_probe"] == "outage — serving last-known"
        s = client.get("/api/v1/universe/summary")
        assert s.status_code == 200
        assert s.json()["revision_probe"] == "outage — serving last-known"
    assert len(_universe_outage_warns(logs)) == 1  # latched — never a per-poll storm


def test_cold_probe_outage_degrades_503_not_raw_500(wedgeable: Any) -> None:
    """S1: cold cache + outage → the honest 503 markers on BOTH routes, never a 500."""
    store, client = wedgeable
    store.wedge_probe = True
    for _ in range(3):
        r = client.get("/api/v1/universe/instruments")
        assert r.status_code == 503
        body = r.json()
        assert body["error_type"] == "store_outage"
        assert body["available"] is False
        assert body["instruments"] == [] and body["kind"] is None
    s = client.get("/api/v1/universe/summary")
    assert s.status_code == 503
    assert s.json()["error_type"] == "store_outage"


def _seed_nifty(real: InstrumentStore, *, run_id: str) -> None:
    """Upsert one NIFTY index row (stamps a fresh ``fetched_at`` → the token moves)."""
    from datetime import date
    from decimal import Decimal

    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    real.upsert_many(
        [
            Instrument(
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
        ],
        snapshot_date=date(2024, 1, 2),
        run_id=run_id,
        csv_source_sha256=run_id.ljust(64, "0"),
    )


def test_base_read_outage_warm_and_cold_twins(wedgeable: Any) -> None:
    """S3: probe OK + base reads wedged → 503 when cold, labelled last-known when warm."""
    store, client = wedgeable
    # Cold twin FIRST (nothing resident): the typed fault maps to the honest 503.
    store.wedge_base = True
    r = client.get("/api/v1/universe/summary")
    assert r.status_code == 503
    assert r.json()["error_type"] == "store_outage"
    # Warm twin: heal, warm the snapshot, MOVE the revision token (a real seed),
    # then wedge the base reads — the rebuild fails but the resident snapshot serves.
    store.wedge_base = False
    warm = client.get("/api/v1/universe/summary")
    assert warm.status_code == 200
    _seed_nifty(store._real, run_id="r1")
    store.wedge_base = True
    served = client.get("/api/v1/universe/summary")
    assert served.status_code == 200
    body = served.json()
    assert body["revision_probe"] == "outage — serving last-known"
    # The LAST-KNOWN (pre-seed) counts serve — labelled, never fabricated fresh ones.
    assert body["total"] == warm.json()["total"]


def test_recovery_serves_fresh_unlabelled_with_one_info(wedgeable: Any) -> None:
    """After the outage clears, the next poll recomputes fresh — no marker, one INFO."""
    store, client = wedgeable
    assert client.get("/api/v1/universe/summary").status_code == 200
    store.wedge_probe = True
    assert client.get("/api/v1/universe/summary").json()["revision_probe"]
    store.wedge_probe = False
    with capture_logs() as logs:
        r = client.get("/api/v1/universe/summary")
    assert r.status_code == 200
    assert "revision_probe" not in r.json()
    infos = [e for e in logs if e["event"] == "universe_cache.store_probe_recovered"]
    assert len(infos) == 1


def test_bad_kind_still_400_under_healthy_store(wedgeable: Any) -> None:
    """REGRESSION: the ValueError→400 arm is untouched by the outage plumbing."""
    _store, client = wedgeable
    r = client.get("/api/v1/universe/instruments?kind=bogus")
    assert r.status_code == 400


# ── S3b: the nav-latency trio under the SAME probe outage ────────────────────────


def test_nav_trio_warm_then_outage_serves_labelled_200(wedgeable: Any) -> None:
    """Warm trio + full outage → every route serves last-known labelled, never a 500."""
    store, client = wedgeable
    warm_cov = client.get("/api/v1/coverage")
    warm_comp = client.get("/api/v1/universe/completeness")
    warm_map = client.get("/api/v1/universe/mapping-integrity")
    warm_csv = client.get("/api/v1/universe/mapping-integrity.csv")
    assert [r.status_code for r in (warm_cov, warm_comp, warm_map, warm_csv)] == [200] * 4
    assert "revision_probe" not in warm_map.json()
    store.wedge_probe = True
    store.wedge_base = True
    with capture_logs() as logs:
        cov = client.get("/api/v1/coverage")
        comp = client.get("/api/v1/universe/completeness")
        mapping = client.get("/api/v1/universe/mapping-integrity")
        csv = client.get("/api/v1/universe/mapping-integrity.csv")
    assert cov.status_code == 200
    assert cov.json()["revision_probe"] == "outage — serving last-known"
    assert comp.status_code == 200
    assert comp.json()["revision_probe"] == "outage — serving last-known"
    assert mapping.status_code == 200
    assert mapping.json()["revision_probe"] == "outage — serving last-known"
    assert mapping.json()["is_green"] == warm_map.json()["is_green"]
    # The CSV serves the last-known report UNLABELLED (no marker surface) — never a 500.
    assert csv.status_code == 200
    assert csv.text == warm_csv.text
    assert len(_movers_outage_warns(logs)) >= 1  # the serve-last-known latch fired


def test_nav_trio_cold_full_outage_degrades_503_not_500(tmp_path: Path) -> None:
    """A COLD trio under a full outage → honest 503 markers on all four surfaces."""
    real = InstrumentStore.open(tmp_path / "lake" / "instruments.duckdb")
    store = _WedgeableStore(real)
    store.wedge_probe = True
    store.wedge_base = True
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app, raise_server_exceptions=False) as client:
        cov = client.get("/api/v1/coverage")
        mapping = client.get("/api/v1/universe/mapping-integrity")
        csv = client.get("/api/v1/universe/mapping-integrity.csv")
        comp = client.get("/api/v1/universe/completeness")
    real.close()
    assert cov.status_code == 503 and cov.json()["error_type"] == "store_outage"
    assert mapping.status_code == 503 and mapping.json()["error_type"] == "store_outage"
    assert csv.status_code == 503 and "unavailable" in csv.text
    assert comp.status_code == 503 and comp.json()["error_type"] == "store_outage"


def test_nav_trio_cold_probe_only_outage_seeds_fresh_labelled(tmp_path: Path) -> None:
    """A probe-only outage on a COLD trio computes fresh + labels the seeded payload."""
    real = InstrumentStore.open(tmp_path / "lake" / "instruments.duckdb")
    store = _WedgeableStore(real)
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Wedge ONLY the connection probe AFTER app startup — the base reads
        # (`fno_eligible_underlyings` / `list_coverage`) stay healthy, so the
        # serve-last-known seed compute succeeds.
        store.wedge_probe = True
        mapping = client.get("/api/v1/universe/mapping-integrity")
        again = client.get("/api/v1/universe/mapping-integrity")
    real.close()
    assert mapping.status_code == 200
    assert mapping.json()["revision_probe"] == "outage — computed fresh (seeded as last-known)"
    # The seed is CACHED at the sentinel generation — the next outage poll serves it.
    assert again.status_code == 200
    assert again.json()["revision_probe"] == "outage — serving last-known"
