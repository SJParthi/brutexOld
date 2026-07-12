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
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.dashboard.sweep_results_view import (
    GATE_COORDINATE_UNGATED,
    UnknownGateCoordinateError,
    _fold_gate_coordinates,
    _resolve_gate_scope,
    gate_coordinate_of,
)
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


def _row(
    seed: int,
    *,
    gate_provenance: str | None = None,
    sharpe: float = 1.0,
    instrument: str = "NIFTY",
    tf: str = "5m",
    max_dd_pct: float = 0.05,
) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument=instrument,
        tf=tf,
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
        max_drawdown_pct=max_dd_pct,
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


class TestL2CacheKeyInjection:
    """The d1 cache-key-injection regression (MIGRATION.md #1758 fix round).

    The refuter's SURVIVED d1 shape: the L2 key was built by raw f-string
    interpolation, so request A ``?rank_by=sharpe_ratio|p=|g=ungated`` (no
    gate — legal 200) and request B ``?rank_by=sharpe_ratio&gate=ungated|p=|g=``
    (must-400: unknown gate coordinate) forged IDENTICAL keys and B was served
    A's cached 200. Fixed structurally: every user-controlled leg is
    percent-encoded (``quote``, ``safe=''``) AND ``rank_by`` enters the key as
    its RESOLVED effective column (``resolve_rank_by_param``), never the raw
    string.
    """

    def _seed(self, lake_root: Path) -> None:
        _write_sweeps(lake_root, "u.parquet", [_row(1, sharpe=0.1)])
        _write_sweeps(lake_root, "g1.parquet", [_row(2, gate_provenance=_TOKEN, sharpe=0.9)])

    def test_d1_a_then_b_b_is_400_not_cached_200(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The exact refuter repro: A caches, then must-400 B is still 400."""
        self._seed(store.path.parent)
        resp_a = client.get(
            "/api/v1/sweep/leaderboard", params={"rank_by": "sharpe_ratio|p=|g=ungated"}
        )
        assert resp_a.status_code == 200
        # The hostile rank_by is unknown → resolved to the default column.
        assert resp_a.json()["rank_by"] == "sharpe_ratio"
        resp_b = client.get(
            "/api/v1/sweep/leaderboard",
            params={"rank_by": "sharpe_ratio", "gate": "ungated|p=|g="},
        )
        assert resp_b.status_code == 400  # NOT A's cached 200 (the d1 cross-serve)
        assert "unknown gate coordinate" in resp_b.json()["detail"]

    def test_d1_b_cold_is_400(self, client: TestClient, store: InstrumentStore) -> None:
        """The must-400 request refuses on a cold cache too (the d2 control)."""
        self._seed(store.path.parent)
        resp_b = client.get(
            "/api/v1/sweep/leaderboard",
            params={"rank_by": "sharpe_ratio", "gate": "ungated|p=|g="},
        )
        assert resp_b.status_code == 400
        # ...and a follow-up legal request is untouched by the refusal.
        resp_a = client.get(
            "/api/v1/sweep/leaderboard", params={"rank_by": "sharpe_ratio|p=|g=ungated"}
        )
        assert resp_a.status_code == 200

    def test_same_resolved_rank_by_shares_one_cache_slot(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normalization teeth (beyond escaping): every rank_by spelling that
        RESOLVES to one column maps to ONE L2 slot — absent, the exact column
        name, and an unknown token (which falls back to the default) all serve
        the SAME cached payload with exactly ONE ranked build."""
        from brutex.dashboard import app as app_module

        self._seed(store.path.parent)
        calls = {"build": 0}
        real_build = app_module.build_sweep_leaderboard

        def _spy(*args: object, **kwargs: object) -> object:
            calls["build"] += 1
            return real_build(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "build_sweep_leaderboard", _spy)
        first = client.get("/api/v1/sweep/leaderboard").json()
        second = client.get("/api/v1/sweep/leaderboard", params={"rank_by": "sharpe_ratio"}).json()
        third = client.get("/api/v1/sweep/leaderboard", params={"rank_by": "not_a_column"}).json()
        assert first == second == third  # byte-exact shared slot (resolved echo)
        assert first["rank_by"] == "sharpe_ratio"
        assert calls["build"] == 1  # ONE compute — the aliases were cache hits

    @pytest.mark.parametrize("leg", ["instrument", "tf", "direction"])
    @pytest.mark.parametrize("empty_first", [False, True])
    def test_empty_filter_leg_is_absent_parity(
        self,
        client: TestClient,
        store: InstrumentStore,
        monkeypatch: pytest.MonkeyPatch,
        leg: str,
        empty_first: bool,
    ) -> None:
        """Empty-string filter leg ('' ) ≡ absent (round-2 LOW fix).

        The L2 key already collapsed '' and None to one instrument/tf/direction
        slot, but ``build_sweep_leaderboard``'s ``_matches`` treated '' as an
        ACTIVE exclude-all filter while None = no filter — so two decode-identical
        keys computed DIFFERENT payloads (an order-dependent stale/empty
        cross-serve). After the normalization ``GET(absent)`` and ``GET(?<leg>=)``
        serve the IDENTICAL full board regardless of call order AND share ONE L2
        cache slot ('' is 'no filter' everywhere). Discriminating: with the fix
        reverted the ``empty_first`` order caches (then cross-serves) a 0-row
        board, so the ``[0.9]`` assertion fails.
        """
        from brutex.dashboard import app as app_module

        # gated 0.9 + ungated 0.1 → the default resolution serves the GATED
        # coordinate's full board ([0.9]); an active-'' filter would exclude it.
        self._seed(store.path.parent)
        calls = {"build": 0}
        real_build = app_module.build_sweep_leaderboard

        def _spy(*args: object, **kwargs: object) -> object:
            calls["build"] += 1
            return real_build(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "build_sweep_leaderboard", _spy)

        def _absent() -> dict[str, Any]:
            return client.get("/api/v1/sweep/leaderboard").json()

        def _empty() -> dict[str, Any]:
            return client.get("/api/v1/sweep/leaderboard", params={leg: ""}).json()

        first, second = (_empty(), _absent()) if empty_first else (_absent(), _empty())
        # '' behaves as 'no filter' → the full default board, both orders.
        assert [r["sharpe_ratio"] for r in first["rows"]] == [0.9]
        assert first == second  # identical payload regardless of call order
        assert calls["build"] == 1  # ONE compute → absent and '' share a slot


class TestGateComposesWithPresetAndFilters:
    """?gate= ∘ ?preset= ∘ instrument/tf filters (the tests-adequacy MINOR)."""

    def test_gate_preset_and_filters_compose(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The served set is exactly (gate coordinate ∩ filters), preset-ranked."""
        lake_root = store.path.parent
        # Gated config: two NIFTY/5m rows (dd 0.09 vs 0.01 — preset order is the
        # REVERSE of the sharpe order), plus one wrong-instrument and one
        # wrong-tf row that the filters must drop.
        _write_sweeps(
            lake_root,
            "g.parquet",
            [
                _row(1, gate_provenance=_TOKEN, sharpe=0.9, max_dd_pct=0.09),
                _row(2, gate_provenance=_TOKEN, sharpe=0.5, max_dd_pct=0.01),
                _row(3, gate_provenance=_TOKEN, sharpe=0.8, instrument="BANKNIFTY"),
                _row(4, gate_provenance=_TOKEN, sharpe=0.7, tf="15m"),
            ],
        )
        # Ungated twin rows the gate scope must exclude.
        _write_sweeps(lake_root, "u.parquet", [_row(5, sharpe=2.0, max_dd_pct=0.001)])
        payload = client.get(
            "/api/v1/sweep/leaderboard",
            params={
                "gate": _COORD,
                "preset": "min_dd_first",
                "instrument": "NIFTY",
                "tf": "5m",
            },
        ).json()
        assert payload["gate_coordinate"] == _COORD
        assert payload["preset"] == "min_dd_first"
        # Exactly the (coordinate ∩ filters) survivors — the ungated 2.0-sharpe
        # row, the BANKNIFTY row, and the 15m row are all absent.
        assert [r["run_id"] for r in payload["rows"]] == [f"{2:064x}", f"{1:064x}"]
        # ...ranked by the PRESET (smallest max-DD first), NOT by sharpe.
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [0.5, 0.9]
        assert all(r["gate_coordinate"] == _COORD for r in payload["rows"])


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


class TestUnknownGateTypedAndSanitised:
    """The typed-unknown-gate 400 (round-3 MINOR: broad ``except ValueError``).

    The route wrapped the whole threadpool build in ``except ValueError -> 400``,
    which (a) mislabelled ANY unexpected server-side ``ValueError`` as a client
    400 and (b) echoed the raw ``?gate=`` token unsanitised/unbounded. The fix
    raises the typed :class:`UnknownGateCoordinateError` (carrying the token) and
    the route reflects only a control-char-stripped, length-bounded slice; a
    genuine ValueError now propagates to the framework 500.
    """

    def test_resolve_gate_scope_raises_typed_error(self) -> None:
        """The resolver raises the typed subclass, carrying the raw token."""
        folded, mixed = _fold_gate_coordinates(("ungated",))
        with pytest.raises(UnknownGateCoordinateError) as excinfo:
            _resolve_gate_scope("gate:nope", folded, mixed)
        assert excinfo.value.coordinate == "gate:nope"
        # Still a ValueError so existing ``except ValueError`` callers stay OK.
        assert isinstance(excinfo.value, ValueError)

    def test_unknown_gate_detail_is_bounded_and_stripped(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """A 500-char / control-char hostile token → BOUNDED, control-free detail."""
        _write_sweeps(store.path.parent, "u.parquet", [_row(1)])
        hostile = "x" * 500 + "\n\r\t\x00\x01<script>"
        resp = client.get("/api/v1/sweep/leaderboard", params={"gate": hostile})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "unknown gate coordinate" in detail
        # The reflected token is truncated to the 64-char cap (+ a "…" marker).
        assert len(detail) <= len("unknown gate coordinate: ") + 65
        # Control characters are stripped from the reflected token.
        reflected = detail.split("unknown gate coordinate: ", 1)[1]
        assert "\n" not in reflected
        assert "\r" not in reflected
        assert "\t" not in reflected
        assert "\x00" not in reflected
        assert "\x01" not in reflected
        assert all(ch.isprintable() for ch in reflected.rstrip("…"))

    def test_genuine_valueerror_is_500_not_400(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ValueError deep in the build now yields a 500, NOT a mislabel 400."""
        from brutex.dashboard import app as app_module

        _write_sweeps(store.path.parent, "u.parquet", [_row(1)])

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise ValueError("unexpected server-side bug")

        monkeypatch.setattr(app_module, "build_sweep_leaderboard", _boom)
        app = build_app(DashboardState(), instrument_store=store, summary_cache=SummaryCache())
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/sweep/leaderboard")
        assert resp.status_code == 500
