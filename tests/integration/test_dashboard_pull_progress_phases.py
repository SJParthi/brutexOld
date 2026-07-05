"""Integration tests for the ``/api/v1/universe/pull-progress`` per-phase block.

DEC-UNIVERSE-PER-PHASE-PROGRESS-001. The Universe pull section shows, for EVERY
window phase (spot / futures / options), a LIVE windows count + progress + status
breakdown — CONSISTENTLY. This exercises the SHIPPED route through a real wired
:class:`InstrumentStore` + a real window-ledger Parquet, proving:

* the ``phases`` array is present ALONGSIDE the existing flat snapshot keys (never
  removing any), read THROUGH the same generation-stamped ``_window_counts_cache``
  the ``/coverage/windows`` page uses (no new scan — an O(1) hit on a repeat poll);
* None-safe honesty — no store / no lake path / no ledger yields ``phases: []`` (an
  honest empty list, never a fabricated window count);
* the honest per-segment breakdown (spot SUCCESS/FAILED/PLANNED, a future SUCCESS,
  an options flush-lag PLANNED reading "pending", never a fake 0).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import InstrumentStore
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

#: Every flat snapshot key the frontend already reads — the additive `phases` block
#: must NEVER drop any of these (terminalPullHeader / renderLiveStats depend on them).
_FLAT_KEYS = {
    "state",
    "total",
    "completed",
    "failed",
    "gaps",
    "bars",
    "current",
    "detail",
    "percent",
    "running",
    "updated_at",
    "phase",
}


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
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


def _key(symbol: str, *, market: str = "groww") -> WindowKey:
    return WindowKey(
        market=market,
        security_id=symbol,
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _save_phase_ledger(tmp_path: Path) -> None:
    """A ledger with a distinct status mix per bucket: spot SUCCESS+FAILED+PLANNED,
    a futures SUCCESS, and an options PLANNED-only (the flush-lag shape)."""
    ledger = WindowLedger()
    ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    ledger.record(
        _key("BANKNIFTY"), raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW
    )
    ledger.plan(_key("SENSEX"), now=_NOW)
    ledger.record(
        _key("25", market=GROWW_FUTURE_COVERAGE_MARKET),
        raw_bars=7,
        error_type=None,
        bars_appended=7,
        now=_NOW,
    )
    ledger.plan(_key("42", market=GROWW_OPTION_COVERAGE_MARKET), now=_NOW)
    save_window_ledger(ledger, tmp_path)  # store.path parent (tmp_path) is the lake root


class TestPullProgressPhases:
    """The /pull-progress route carries a live per-phase block alongside the flat keys."""

    def test_phases_present_with_real_ledger(self, store: InstrumentStore, tmp_path: Path) -> None:
        _save_phase_ledger(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/pull-progress").json()

        # Every existing flat key is still present (additive, never removed).
        assert set(body) >= _FLAT_KEYS
        # ONE row per phase, in pull order.
        assert [p["phase"] for p in body["phases"]] == ["spot", "futures", "options"]
        by_phase = {p["phase"]: p for p in body["phases"]}
        # Spot: 1 SUCCESS (done), 1 FAILED, 1 PLANNED (pending) → total 3.
        assert by_phase["spot"]["done"] == 1
        assert by_phase["spot"]["total"] == 3
        assert by_phase["spot"]["failed"] == 1
        assert by_phase["spot"]["pending"] == 1
        # Futures: 1 SUCCESS.
        assert by_phase["futures"]["done"] == 1
        assert by_phase["futures"]["total"] == 1
        # Options flush-lag: PLANNED only → pending, never a fake 0-done "complete".
        assert by_phase["options"]["pending"] == 1
        assert by_phase["options"]["done"] == 0
        assert by_phase["options"]["total"] == 1
        # Idle job → no phase label → no window row is highlighted (honest).
        assert body["phase"] == ""
        assert not any(p["current"] for p in body["phases"])

    def test_phases_empty_when_no_ledger_yet(self, store: InstrumentStore) -> None:
        # A wired store but NO window ledger Parquet (nothing planned) → honest empty list.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/pull-progress").json()
        assert body["phases"] == []
        assert set(body) >= _FLAT_KEYS  # the flat snapshot still ships

    def test_phases_empty_when_no_store(self) -> None:
        # A dry-run / unwired dashboard (no store) → phases: [], never a 500.
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        body = client.get("/api/v1/universe/pull-progress").json()
        assert body["phases"] == []
        assert body["state"] == "idle"

    def test_phases_empty_when_store_has_no_lake_path(self) -> None:
        # A store whose ``path`` is None (no lake root) → phases: [].
        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/universe/pull-progress").json()
        assert body["phases"] == []

    def test_empty_ledger_parquet_three_neutral_rows(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A 0-row ledger Parquet (planned nothing yet) → three neutral zero rows, present.
        save_window_ledger(WindowLedger(), tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/pull-progress").json()
        assert [p["phase"] for p in body["phases"]] == ["spot", "futures", "options"]
        assert all(p["total"] == 0 and p["done"] == 0 for p in body["phases"])

    def test_garbage_ledger_parquet_degrades_never_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-WINDOW-TORN-READ-RETRY-001 regression: a torn/corrupt ledger Parquet drove
        # a REAL duckdb.Error through the previously-UNGUARDED counts read and 500'd the
        # 4 s poll. End-to-end (no monkeypatch — the real duckdb exception class): the
        # route now retries once, then serves the honest empty-phases degrade at 200
        # with every flat snapshot key intact.
        from brutex.data.window_ledger_store import ledger_dir, ledger_path

        ledger_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        ledger_path(tmp_path).write_bytes(b"definitely not a parquet file")
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/universe/pull-progress")
        assert resp.status_code == 200  # was a 500 before the guard
        body = resp.json()
        assert body["phases"] == []  # the same honest shape the no-ledger contract serves
        assert set(body) >= _FLAT_KEYS
