"""The ``?gate=`` leg of ``/api/v1/sweep/leaderboard`` (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).

Route-level coverage of the gate-scoped serving fix over a REAL DuckDB-backed
``InstrumentStore`` + ``TestClient``: the additive payload keys on the wire,
explicit ``?gate=`` scoping, the unknown-token 400 (the preset precedent), the
L2 cache two-call teeth (different gate params occupy DISTINCT payload slots
while the L1 SweepScan is computed ONCE — the poisoning-safe key law), and the
``results.html`` template pins for the gate dropdown + column.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.dashboard.sweep_results_view import GATE_COORDINATE_UNGATED, gate_coordinate_of
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.sweep_identity import gate_provenance_token
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_TOKEN = gate_provenance_token(gate_config={"overfit": [0.95, 0.5, 0.0, 0.0]}, selection=None)
assert _TOKEN is not None
_COORD = gate_coordinate_of(_TOKEN)


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


def _row(seed: int, *, gate_provenance: str | None = None, sharpe: float = 1.0) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=0,
        net_pnl_paisa=100_00,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=sharpe,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        gate_provenance=gate_provenance,
    )


def _write_sweeps(
    lake_root: Path,
    name: str,
    rows: list[ResultRow],
    *,
    gate_provenance: str | None = None,
) -> None:
    dest = lake_root / "sweeps" / "NIFTY" / "5m" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest, gate_provenance=gate_provenance)
    for row in rows:
        store.append(row)
    store.flush()


class _NoPathStore:
    """A store stand-in whose ``path`` is not a Path (the no-lake-root arm)."""

    path = None

    def list_index_equity_instruments(self) -> list[object]:
        """Return no instruments (the arm needs only the route to boot)."""
        return []


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
    app = build_app(DashboardState(), instrument_store=store, summary_cache=SummaryCache())
    with TestClient(app) as c:
        yield c


class TestGateRoute:
    def test_additive_keys_on_the_wire(self, client: TestClient, store: InstrumentStore) -> None:
        """The four gate keys ride every payload; a legacy lake reads case 1."""
        _write_sweeps(store.path.parent, "u.parquet", [_row(1)])
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["gate_coordinate"] == GATE_COORDINATE_UNGATED
        assert payload["gate_coordinates"] == [GATE_COORDINATE_UNGATED]
        assert payload["mixed_gate_coordinates"] is False
        assert payload["gate_reasons"] == []
        assert payload["rows"][0]["gate_coordinate"] == GATE_COORDINATE_UNGATED

    def test_case2_default_serves_gated(self, client: TestClient, store: InstrumentStore) -> None:
        """One gated + ungated coordinate: the default view is the GATED one."""
        lake_root = store.path.parent
        _write_sweeps(lake_root, "a_u.parquet", [_row(1, sharpe=0.1)])
        _write_sweeps(lake_root, "b_g.parquet", [_row(1, gate_provenance=_TOKEN, sharpe=0.9)])
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["gate_coordinate"] == _COORD
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [0.9]
        assert payload["run_id_conflicts"] == 0  # cross-coordinate twin ≠ conflict

    def test_explicit_gate_param_scopes(self, client: TestClient, store: InstrumentStore) -> None:
        """?gate=ungated flips the served coordinate."""
        lake_root = store.path.parent
        _write_sweeps(lake_root, "a_u.parquet", [_row(1, sharpe=0.1)])
        _write_sweeps(lake_root, "b_g.parquet", [_row(1, gate_provenance=_TOKEN, sharpe=0.9)])
        payload = client.get("/api/v1/sweep/leaderboard", params={"gate": "ungated"}).json()
        assert payload["gate_coordinate"] == GATE_COORDINATE_UNGATED
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [0.1]

    def test_unknown_gate_is_400(self, client: TestClient, store: InstrumentStore) -> None:
        """An unknown explicit token refuses loudly (the preset precedent)."""
        _write_sweeps(store.path.parent, "u.parquet", [_row(1)])
        resp = client.get("/api/v1/sweep/leaderboard", params={"gate": "gate:nope"})
        assert resp.status_code == 400
        assert "unknown gate coordinate" in resp.json()["detail"]

    def test_unknown_gate_is_400_without_lake_root(self) -> None:
        """The no-lake-root degrade arm refuses an explicit token too."""
        with TestClient(build_app(DashboardState(), instrument_store=_NoPathStore())) as client:  # type: ignore[arg-type]
            resp = client.get("/api/v1/sweep/leaderboard", params={"gate": "ungated"})
            assert resp.status_code == 400

    def test_no_lake_root_blank_gate_serves_empty(self) -> None:
        """No lake root + no gate: the honest empty shape with the additive keys."""
        with TestClient(build_app(DashboardState(), instrument_store=_NoPathStore())) as client:  # type: ignore[arg-type]
            payload = client.get("/api/v1/sweep/leaderboard").json()
            assert payload["available"] is False
            assert payload["gate_coordinate"] == GATE_COORDINATE_UNGATED
            assert payload["gate_coordinates"] == []

    def test_l2_cache_distinct_slots_shared_scan(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two-call teeth: different gate params → distinct payloads, ONE L1 scan.

        The poisoning-safe key law: the |g= leg keys the RANKED payload, while
        the coordinate-agnostic SweepScan is computed once per revision and
        shared — spied via load_sweep_scan.
        """
        from brutex.dashboard import app as app_module

        lake_root = store.path.parent
        _write_sweeps(lake_root, "a_u.parquet", [_row(1, sharpe=0.1)])
        _write_sweeps(lake_root, "b_g.parquet", [_row(1, gate_provenance=_TOKEN, sharpe=0.9)])
        calls = {"scan": 0}
        real_scan = app_module.load_sweep_scan

        def _spy(lake: Path) -> object:
            calls["scan"] += 1
            return real_scan(lake)

        monkeypatch.setattr(app_module, "load_sweep_scan", _spy)
        first = client.get("/api/v1/sweep/leaderboard", params={"gate": _COORD}).json()
        second = client.get("/api/v1/sweep/leaderboard", params={"gate": "ungated"}).json()
        assert [r["sharpe_ratio"] for r in first["rows"]] == [0.9]
        assert [r["sharpe_ratio"] for r in second["rows"]] == [0.1]  # NOT the cached first
        assert calls["scan"] == 1  # the L1 scan is SHARED across the two gate slots
        # A repeat of each call is a pure L2 hit — still one scan.
        again = client.get("/api/v1/sweep/leaderboard", params={"gate": _COORD}).json()
        assert again == first
        assert calls["scan"] == 1


class TestResultsTemplateGateControls:
    """Presence pins for the gate dropdown + column (the house substring envelope)."""

    def test_template_carries_gate_controls(self) -> None:
        template = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "templates"
            / "results.html"
        ).read_text(encoding="utf-8")
        assert 'select id="fGate"' in template
        assert 'params.set("gate", fGate.value)' in template
        assert '"gate_coordinate", "Gate"' in template
        assert "fillGateSelect(board.gate_coordinates" in template
        assert "mixed_gate_coordinates" in template
        # textContent-only DOM for the new dropdown (CodeQL XSS): the builder
        # never assembles option HTML strings.
        assert "opt.textContent = String(c);" in template
