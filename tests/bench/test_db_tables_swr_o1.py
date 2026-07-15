"""O(1)-on-repeat gate for the ``/api/v1/db/tables`` SWR memo (H4b).

DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G4t) memoised the tables-route
``table_summary`` COUNT(*) sweep on ONE stable key whose generation digests
the write generation + the instrument-master content revision — but shipped
with unit/route pins only, NO bench gate (the reader-C missing-gates census).
This gate pins the cost law DETERMINISTICALLY (compute-count spies — no wall
clock, no flake):

* N repeat polls within one (generation, master-revision) token run EXACTLY
  ONE ``table_summary`` sweep — the O(1)-on-repeat law;
* a master-revision move (a keyless ``load-instruments`` — the exact signal
  the write generation is blind to) re-sweeps exactly once;
* a write-generation bump (``mark_dirty``) re-sweeps exactly once.

HONEST envelope (Golden Rule #10): O(1)-on-repeat — the FIRST poll per token
pays the real 3-table COUNT(*) sweep (tiny, but real store I/O), and the SWR
arm may serve last-known past the adaptive floor mid-pull (pinned in the
#1751 unit suite, not re-pinned here).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard import app as app_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _instrument(security_id: int, name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=name,
        display_name=name,
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
        [_instrument(13, "NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def sweep_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every real ``table_summary`` sweep the route triggers."""
    calls = {"sweeps": 0}
    real = app_module.table_summary

    def _spy(conn: Any, **kwargs: Any) -> Any:
        calls["sweeps"] += 1
        return real(conn, **kwargs)

    monkeypatch.setattr(app_module, "table_summary", _spy)
    return calls


class TestTablesSummarySweepOncePerToken:
    def test_repeat_polls_pay_one_sweep(
        self, store: InstrumentStore, sweep_spy: dict[str, int]
    ) -> None:
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            payloads = [client.get("/api/v1/db/tables").json() for _ in range(5)]
        assert sweep_spy["sweeps"] == 1, (
            f"expected ONE table_summary sweep for five polls within one token, "
            f"measured {sweep_spy['sweeps']} — the /db tables memo regressed to per-poll"
        )
        assert all(p == payloads[0] for p in payloads)
        names = {t["table"] for t in payloads[0]["tables"]}
        assert "instruments" in names

    def test_master_revision_move_resweeps_once(
        self, store: InstrumentStore, sweep_spy: dict[str, int]
    ) -> None:
        """A keyless load-instruments (master content moves, generation does NOT)
        re-sweeps exactly once — the exact staleness class the digest closes."""
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            first = client.get("/api/v1/db/tables").json()
            assert sweep_spy["sweeps"] == 1
            store.upsert_many(
                [_instrument(25, "BANKNIFTY")],
                snapshot_date=date(2024, 1, 3),
                run_id="r2",
                csv_source_sha256="1" * 64,
            )
            fresh = [client.get("/api/v1/db/tables").json() for _ in range(3)]
        assert sweep_spy["sweeps"] == 2  # one re-sweep, then memoised again
        rows = {t["table"]: t["row_count"] for t in fresh[0]["tables"]}
        assert rows["instruments"] == 2  # the fresh sweep SAW the new row
        assert first != fresh[0]

    def test_write_generation_bump_resweeps_once(
        self, store: InstrumentStore, sweep_spy: dict[str, int]
    ) -> None:
        summary_cache = SummaryCache()
        app = build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
        with TestClient(app) as client:
            for _ in range(3):
                client.get("/api/v1/db/tables")
            assert sweep_spy["sweeps"] == 1
            summary_cache.mark_dirty()  # a lake write landed
            for _ in range(3):
                client.get("/api/v1/db/tables")
        assert sweep_spy["sweeps"] == 2
