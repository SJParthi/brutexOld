"""The `/results` rollup serve path + route wiring (DEC-RESULTS-TOPK-ROLLUP-001).

Covers the dashboard half: :func:`load_sweep_scan_via_rollup`'s three-outcome
mapping (fresh / stale-with-note / fallback) + the (revision, reason)-deduped
log gate + the recovery INFO, the ``SweepScan`` superset/filter structural
refusal + the ``total_rows_override`` law in :func:`build_sweep_leaderboard`,
and the REAL ``/api/v1/sweep/leaderboard`` route through ``build_app`` +
``TestClient``: rollup-served wire parity vs the legacy scan, the serve
predicate (filters / empty-string params stay legacy and never touch the
classifier), the emit-on-nonzero staleness note keys, the Hook-C fallback
rebuild (once per revision), and the unchanged 400-preset contract.
"""

# The wiring tests reach module-private helpers deliberately (house pattern).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

import brutex.engine.results_rollup as rr
import structlog.testing
from brutex.dashboard.sweep_results_view import (
    SWEEPS_DIRNAME,
    RollupServe,
    SweepScan,
    build_sweep_leaderboard,
    load_sweep_scan,
    load_sweep_scan_tree,
    load_sweep_scan_via_rollup,
    preset_rank_record_source,
)
from brutex.engine.results_store import ResultRow, ResultsStore


def _row(
    seed: int,
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    sharpe: float = 1.0,
) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=1,
        net_pnl_paisa=100_00 + seed,
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
    )


def _write_store(
    lake_root: Path,
    rows: list[ResultRow],
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    digest: str = "a" * 16,
) -> Path:
    dest = lake_root / SWEEPS_DIRNAME / instrument / tf / f"{direction}_k1_{digest}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _rev(lake_root: Path) -> str:
    from brutex.dashboard.lake_revision import sweep_results_revision

    return sweep_results_revision(lake_root) or ""

    # ────── load_sweep_scan_via_rollup (pure, no FastAPI) ──────


class TestLoadSweepScanViaRollup:
    def test_fresh_serve_builds_the_superset_scan(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(1, sharpe=2.0), _row(2, sharpe=1.0)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        assert rr.rebuild_rollup(sweeps_root) is True
        serve = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=rr.LogGate())
        assert serve.fallback_reason is None
        assert serve.behind_stores == 0
        assert serve.conflict_suspect is False
        scan = serve.scan
        assert scan is not None
        assert scan.superset_only is True
        assert scan.total_rows_override == 2
        assert scan.store_count == 1
        assert scan.instruments == ("NIFTY",)
        assert len(scan.rows) == 2
        # #1467 fold seam: the all-ungated union serves the ROW-derived
        # coordinate so the rollup payload matches the legacy wire.
        assert scan.gate_coordinates == ("ungated",)

    def test_gated_rows_force_the_legacy_fallback(self, tmp_path: Path) -> None:
        """#1467 fold seam (DEC-SWEEP-SERVING-GATE-PROVENANCE-001 ×
        DEC-RESULTS-TOPK-ROLLUP-001): a rollup union carrying ANY gated row
        falls back to the legacy scan (reason ``gate_coordinates_present``) —
        the case-2/case-3 gate semantics need the store-level census only the
        legacy scan performs, and a superset scan must never be gate-scoped."""
        token = '{"gate_config":{"overfit":[0.95]}}'
        _write_store(tmp_path, [replace(_row(1), gate_provenance=token), _row(2)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        assert rr.rebuild_rollup(sweeps_root) is True
        gate = rr.LogGate()
        with structlog.testing.capture_logs() as logs:
            serve = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=gate)
        assert serve.scan is None
        assert serve.fallback_reason == "gate_coordinates_present"
        warns = [e for e in logs if e["event"] == "sweep_results.rollup_fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "gate_coordinates_present"

    def test_fallback_names_the_reason_and_dedupes_the_warn(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(1)])
        gate = rr.LogGate()
        with structlog.testing.capture_logs() as logs:
            first = load_sweep_scan_via_rollup(tmp_path, rev="r1", log_gate=gate)
            second = load_sweep_scan_via_rollup(tmp_path, rev="r1", log_gate=gate)
        assert first.scan is None and first.fallback_reason == "absent"
        assert second.fallback_reason == "absent"
        warns = [e for e in logs if e["event"] == "sweep_results.rollup_fallback"]
        assert len(warns) == 1  # per (revision, reason), never per poll
        with structlog.testing.capture_logs() as logs2:
            load_sweep_scan_via_rollup(tmp_path, rev="r2", log_gate=gate)
        assert any(e["event"] == "sweep_results.rollup_fallback" for e in logs2)

    def test_behind_reconciles_via_the_fallback_arm(self, tmp_path: Path) -> None:
        """The folded land-pass ruling: additions-only drift routes to the
        legacy fallback (ONE scan per revision + Hook C) — never a stale
        serve missing the newest winners, and never a permanent "N behind"
        for an out-of-band writer."""
        _write_store(tmp_path, [_row(1)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        _write_store(tmp_path, [_row(2, tf="15m")], tf="15m", digest="d" * 16)
        gate = rr.LogGate()
        stale_rev = _rev(tmp_path)
        with structlog.testing.capture_logs() as logs:
            first = load_sweep_scan_via_rollup(tmp_path, rev=stale_rev, log_gate=gate)
            second = load_sweep_scan_via_rollup(tmp_path, rev=stale_rev, log_gate=gate)
        assert first.scan is None and first.fallback_reason == "behind"
        assert second.fallback_reason == "behind"
        warns = [e for e in logs if e["event"] == "sweep_results.rollup_fallback"]
        assert len(warns) == 1 and warns[0]["reason"] == "behind"  # deduped per rev
        # Reconcile (Hook B) → the next serve is FRESH and logs ONE recovery INFO.
        rr.rebuild_rollup(sweeps_root)
        with structlog.testing.capture_logs() as logs2:
            fresh = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=gate)
            again = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=gate)
        assert fresh.scan is not None and fresh.behind_stores == 0
        assert again.behind_stores == 0
        infos = [e for e in logs2 if e["event"] == "sweep_results.rollup_recovered"]
        assert len(infos) == 1  # the degraded latch reports exactly once

    def test_revision_drift_reconciles_via_the_fallback_arm(self, tmp_path: Path) -> None:
        """Set-equal + rev-moved (the audit-append shape) also reconciles via
        the fallback, so era honesty fields refresh on the next poll."""
        _write_store(tmp_path, [_row(1)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        from brutex.orchestrator.audit import AuditWriter

        audit_path = sweeps_root / "audit" / "sweep-all.jsonl"
        with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
            writer.append(action="SWEEP_START", subject="s", payload={"x": 1})
        serve = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=rr.LogGate())
        assert serve.scan is None
        assert serve.fallback_reason == "revision_drift"

    def test_conflict_suspect_stale_serves_with_the_note(self, tmp_path: Path) -> None:
        """MAJOR-1: a latched fence stale-serves — never a per-poll legacy scan."""
        import json

        _write_store(tmp_path, [_row(1)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        payload = json.loads(rr.rollup_stamp_path(sweeps_root).read_bytes())
        payload["flags"]["conflict_suspect"] = True
        rr.rollup_stamp_path(sweeps_root).write_text(rr._dumps(payload) + "\n", encoding="utf-8")
        gate = rr.LogGate()
        rev = _rev(tmp_path)
        with structlog.testing.capture_logs() as logs:
            serve = load_sweep_scan_via_rollup(tmp_path, rev=rev, log_gate=gate)
            again = load_sweep_scan_via_rollup(tmp_path, rev=rev, log_gate=gate)
        assert serve.scan is not None
        assert serve.conflict_suspect is True
        assert again.conflict_suspect is True
        stale_warns = [e for e in logs if e["event"] == "sweep_results.rollup_stale_served"]
        assert len(stale_warns) == 1  # deduped per (revision, reason)

    def test_conflict_suspect_with_additions_carries_the_behind_count(self, tmp_path: Path) -> None:
        import json

        _write_store(tmp_path, [_row(1)])
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        payload = json.loads(rr.rollup_stamp_path(sweeps_root).read_bytes())
        payload["flags"]["conflict_suspect"] = True
        rr.rollup_stamp_path(sweeps_root).write_text(rr._dumps(payload) + "\n", encoding="utf-8")
        _write_store(tmp_path, [_row(2, tf="15m")], tf="15m", digest="d" * 16)
        serve = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=rr.LogGate())
        assert serve.scan is not None
        assert serve.conflict_suspect is True
        assert serve.behind_stores == 1

    def test_rollup_serve_is_frozen(self) -> None:
        serve = RollupServe(scan=None, behind_stores=0, conflict_suspect=False, fallback_reason="x")
        with pytest.raises(AttributeError):
            serve.behind_stores = 1  # type: ignore[misc]

            # ────── SweepScan superset law + build_sweep_leaderboard ──────


class TestSupersetScanLaw:
    def _rollup_scan(self, tmp_path: Path) -> SweepScan:
        _write_store(tmp_path, [_row(1, sharpe=2.0), _row(2, sharpe=1.0)])
        rr.rebuild_rollup(tmp_path / SWEEPS_DIRNAME)
        serve = load_sweep_scan_via_rollup(tmp_path, rev=_rev(tmp_path), log_gate=rr.LogGate())
        assert serve.scan is not None
        return serve.scan

    def test_filtering_a_superset_scan_is_structurally_refused(self, tmp_path: Path) -> None:
        scan = self._rollup_scan(tmp_path)
        with pytest.raises(ValueError, match="superset"):
            build_sweep_leaderboard(tmp_path, instrument="NIFTY", scan=scan)

    def test_unfiltered_superset_ranks_and_reports_the_override_total(self, tmp_path: Path) -> None:
        scan = self._rollup_scan(tmp_path)
        board = build_sweep_leaderboard(tmp_path, scan=scan)
        assert board.total_rows == 2  # the STAMPED global count, not len(rows)
        assert [r["sharpe_ratio"] for r in board.rows] == [2.0, 1.0]

    def test_default_scan_keeps_the_len_filtered_law(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(1), _row(2)])
        scan = load_sweep_scan(tmp_path)
        assert scan.total_rows_override is None and scan.superset_only is False
        board = build_sweep_leaderboard(tmp_path, scan=scan)
        assert board.total_rows == 2

    def test_load_sweep_scan_delegates_to_the_tree_loader(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(1)])
        assert load_sweep_scan(tmp_path) == load_sweep_scan_tree(tmp_path / SWEEPS_DIRNAME)

    def test_preset_rank_record_source_is_the_projection_source(self) -> None:
        source = preset_rank_record_source()
        assert source.startswith("def _preset_rank_record(")
        assert "composite_score=None" in source

        # ────── the route (build_app + TestClient) ──────


fastapi = pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app  # noqa: E402
from brutex.data.instrument_store import InstrumentStore  # noqa: E402
from brutex.models import Exchange, Instrument, InstrumentType, Segment  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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


class TestLeaderboardRouteRollup:
    def test_unfiltered_poll_serves_the_rollup_with_wire_parity(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The rollup-served payload is byte-identical to the legacy compute."""
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1, sharpe=0.5), _row(2, sharpe=2.5)])
        legacy = build_sweep_leaderboard(lake_root, top_n=50).to_dict()
        assert rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME) is True
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload == legacy
        assert "rollup_behind_stores" not in payload  # fresh ⇒ no note keys
        assert "rollup_conflict_suspect" not in payload

    def test_fresh_serve_never_reloads_the_per_tuple_stores(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fails-before op-count pin: a rollup poll loads exactly ONE
        Parquet (the K-union) — the pre-fix path loaded O(stores) files."""
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        _write_store(lake_root, [_row(2, tf="15m")], tf="15m", digest="d" * 16)
        rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME)
        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["available"] is True
        assert loads == [rr.rollup_parquet_path(lake_root / SWEEPS_DIRNAME)]

    def test_filtered_requests_never_touch_the_classifier(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME)

        def _forbidden(root: Path, *, current_rev: str) -> object:
            raise AssertionError("a FILTERED request must never consult the rollup")

        monkeypatch.setattr(rr, "classify_rollup", _forbidden)
        for params in (
            {"instrument": "NIFTY"},
            {"tf": "5m"},
            {"direction": "bull"},
            {"instrument": ""},  # a DEGENERATE empty-string filter stays legacy
        ):
            payload = client.get("/api/v1/sweep/leaderboard", params=params).json()
            assert payload["available"] is True

    def test_gate_scoped_requests_never_touch_the_rollup(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1467 fold seam (DEC-SWEEP-SERVING-GATE-PROVENANCE-001 ×
        DEC-RESULTS-TOPK-ROLLUP-001): ANY ``?gate=`` request bypasses the
        rollup — a gate-coexistence lake (one run_id under two gate
        coordinates) structurally cannot be one run_id-keyed rollup store, so
        the legacy scan owns gate serving (the composite-preset fallback
        precedent). Gate-None rollup serving is pinned by the sibling wire-
        parity tests above — deliberately not duplicated here."""
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME)

        def _forbidden(root: Path, *, current_rev: str) -> object:
            raise AssertionError("a GATE-scoped request must never consult the rollup")

        monkeypatch.setattr(rr, "classify_rollup", _forbidden)
        # A resolvable coordinate + the blank degenerate both serve via legacy.
        for params in (
            {"gate": "ungated"},
            {"gate": ""},  # blank ?gate= = the degenerate-param class, stays legacy
        ):
            payload = client.get("/api/v1/sweep/leaderboard", params=params).json()
            assert payload["available"] is True
            assert payload["gate_coordinate"] == "ungated"
        # An UNKNOWN token 400s via the legacy resolution — still zero rollup
        # consults (the _forbidden spy above is the proof either way).
        resp = client.get("/api/v1/sweep/leaderboard", params={"gate": "gate:" + "f" * 16})
        assert resp.status_code == 400

    def test_rank_by_and_top_ride_the_rollup(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1, sharpe=0.5), _row(2, sharpe=2.5), _row(3, sharpe=1.5)])
        rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME)
        payload = client.get(
            "/api/v1/sweep/leaderboard", params={"rank_by": "net_pnl_paisa", "top": 2}
        ).json()
        legacy = build_sweep_leaderboard(lake_root, rank_by="net_pnl_paisa", top_n=2).to_dict()
        assert payload == legacy
        assert len(payload["rows"]) == 2

    def test_preset_rides_the_rollup_and_unknown_preset_stays_400(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1), _row(2)])
        rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME)
        payload = client.get("/api/v1/sweep/leaderboard", params={"preset": "min_dd_first"}).json()
        legacy = build_sweep_leaderboard(lake_root, preset="min_dd_first", top_n=50).to_dict()
        assert payload == legacy
        assert payload["preset"] == "min_dd_first"
        resp = client.get("/api/v1/sweep/leaderboard", params={"preset": "bogus"})
        assert resp.status_code == 400

    def test_preset_with_active_composite_stays_legacy(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merge-reconcile COMPOSITE-ACTIVATION ruling (MIGRATION.md #1732):
        a JOINED non-empty composite index feeds preset ORDERING (the
        ``composite`` preset's primary axis AND ``min_dd_first``'s composite
        tiebreak) with a value that is NOT a persisted ``ResultRow`` field —
        the persisted union structurally cannot cover it, so a PRESET-ordered
        poll must stay LEGACY while the index is active; a plain ``rank_by``
        poll (persisted fields only) keeps the rollup serve."""
        import polars as pl

        lake_root = store.path.parent
        _write_store(lake_root, [_row(1), _row(2)])
        assert rr.rebuild_rollup(lake_root / SWEEPS_DIRNAME) is True
        # Activate the read-time composite index (the #1749 join): the newest
        # run's aggs sidecar under the sweep_runs/ SIBLING tree.
        depth = lake_root / "sweep_runs" / ("grid" + "0" * 60) / "results-depth" / "NIFTY" / "5m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {
                "run_id": [f"{1:064x}"],
                "composite_score": pl.Series([1.75], dtype=pl.Float64),
            }
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")

        calls: list[str] = []
        real_classify = rr.classify_rollup

        def _spy(root: Path, *, current_rev: str) -> object:
            calls.append(current_rev)
            return real_classify(root, current_rev=current_rev)

        monkeypatch.setattr(rr, "classify_rollup", _spy)
        for preset in ("composite", "min_dd_first"):
            payload = client.get("/api/v1/sweep/leaderboard", params={"preset": preset}).json()
            assert payload["composite_join"]["state"] == "joined"
            assert payload["preset"] == preset
            # ACTIVE composite + preset ⇒ the rollup arm never engaged: the
            # served ordering is the legacy compute's (composite-aware — the
            # control threads the SAME index the route's level-1b slot loads).
            from brutex.dashboard.composite_join import load_composite_index

            legacy = build_sweep_leaderboard(
                lake_root,
                preset=preset,
                top_n=50,
                composite=load_composite_index(lake_root / SWEEPS_DIRNAME),
            ).to_dict()
            assert payload == legacy
        assert calls == []
        # A plain rank_by poll keeps the rollup serve (persisted-field
        # ordering — the union covers it; the composite only DECORATES).
        rank_payload = client.get("/api/v1/sweep/leaderboard").json()
        assert calls, "the unfiltered rank_by poll must still ride the rollup"
        assert rank_payload["rows"][0]["composite_score"] == 1.75

    def test_additions_serve_fresh_legacy_and_reconverge(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The incumbent freshness contract: a store flush is visible on the
        very next poll (via the behind→fallback reconcile + Hook C)."""
        import json

        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        sweeps_root = lake_root / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        _write_store(lake_root, [_row(2, tf="15m")], tf="15m", digest="d" * 16)
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["total_rows"] == 2  # FRESH — never a stale total
        assert "rollup_behind_stores" not in payload
        # Hook C reconverged the rollup to the full 2-store tree.
        stamp = json.loads(rr.rollup_stamp_path(sweeps_root).read_bytes())
        assert stamp["aggregates"]["store_count"] == 2

    def test_conflict_suspect_stale_serves_with_the_note_keys(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """MAJOR-1 on the wire: a latched fence stale-serves the LAST-VALID
        rollup with the honest conflict + behind notes."""
        import json

        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        sweeps_root = lake_root / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        payload = json.loads(rr.rollup_stamp_path(sweeps_root).read_bytes())
        payload["flags"]["conflict_suspect"] = True
        rr.rollup_stamp_path(sweeps_root).write_text(rr._dumps(payload) + "\n", encoding="utf-8")
        _write_store(lake_root, [_row(2, tf="15m")], tf="15m", digest="d" * 16)
        wire = client.get("/api/v1/sweep/leaderboard").json()
        assert wire["rollup_conflict_suspect"] is True
        assert wire["rollup_behind_stores"] == 1
        # The served rows are the LAST-VALID rollup (the new store's row is
        # honestly NOT in them yet — the wire says so instead of a per-poll
        # legacy scan on a standing-conflict tree).
        assert wire["total_rows"] == 1

    def test_fallback_serves_legacy_and_hook_c_rebuilds_once(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1, sharpe=2.0), _row(2, sharpe=1.0)])
        sweeps_root = lake_root / SWEEPS_DIRNAME
        assert not rr.rollup_parquet_path(sweeps_root).exists()
        legacy = build_sweep_leaderboard(lake_root, top_n=50).to_dict()
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload == legacy  # the byte-identical legacy fallback
        # Hook C: the fallback poll wrote the rollup from the scan it built.
        assert rr.rollup_parquet_path(sweeps_root).exists()
        stamp_bytes = rr.rollup_stamp_path(sweeps_root).read_bytes()
        # A repeat poll within the SAME revision serves cached legacy and the
        # once-per-revision gate never rewrites the artifact.
        again = client.get("/api/v1/sweep/leaderboard").json()
        assert again == legacy
        assert rr.rollup_stamp_path(sweeps_root).read_bytes() == stamp_bytes

    def test_floored_stale_scan_never_feeds_hook_c(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Hook-C ``scan_computed`` freshness witness (the refold
        composition fix over DEC-NAV-CACHES-FLOOR-REKEY-001): a level-1 slot
        serve that did NOT compute the legacy scan in THIS request (the
        floored/STALE cache-serve shape a mid-job generation move produces)
        must NEVER feed ``rebuild_rollup_from_scan_best_effort`` — rebuilding
        from a stale scan would stamp a FRESH glob + rev sweep over rows that
        never folded the newer stores, so a later poll would serve it FRESH.

        Fails-before: without the witness the route's condition
        (``serve.scan is None`` + the once-per-revision gate) fires Hook C on
        this exact request and writes the rollup artifact.
        """
        from brutex.dashboard.movers_cache import MoversSnapshotCache

        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        sweeps_root = lake_root / SWEEPS_DIRNAME
        assert not rr.rollup_parquet_path(sweeps_root).exists()
        stale_scan = load_sweep_scan(lake_root)
        real_get = MoversSnapshotCache.get_or_compute

        def _floored(self: MoversSnapshotCache, key: str, **kwargs: object) -> object:
            if key == "sweep_scan":
                return stale_scan  # served WITHOUT computing — the floor shape
            return real_get(self, key, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(MoversSnapshotCache, "get_or_compute", _floored)
        rebuilds = {"n": 0}
        real_rebuild = rr.rebuild_rollup_from_scan_best_effort

        def _spy(root: Path, scan: SweepScan) -> None:
            rebuilds["n"] += 1
            return real_rebuild(root, scan)

        # The route imports the symbol at CALL time from the module, so the
        # module-attr patch intercepts the real wiring.
        monkeypatch.setattr(rr, "rebuild_rollup_from_scan_best_effort", _spy)
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["available"] is True  # the stale scan still serves
        assert rebuilds["n"] == 0  # no computed scan ⇒ Hook C never fires
        assert not rr.rollup_parquet_path(sweeps_root).exists()

    def test_settled_tree_repeat_revision_serves_the_rollup(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Convergence: after Hook C lands and the tree SETTLES, the next
        revision-moving-but-store-preserving poll cycle serves rollup-first."""
        lake_root = store.path.parent
        _write_store(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["available"] is True
        sweeps_root = lake_root / SWEEPS_DIRNAME
        assert rr.rollup_parquet_path(sweeps_root).exists()  # Hook C landed
        # Reconcile to the current tree (Hook B): the rollup now matches the
        # settled tree, so a fresh poll of the SAME revision is rollup-served.
        rr.rebuild_rollup(sweeps_root)
        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))
        # A fresh client (fresh caches) polls the settled tree.
        app2 = build_app(DashboardState(), instrument_store=store)
        with TestClient(app2) as client2:
            payload = client2.get("/api/v1/sweep/leaderboard").json()
        assert payload["available"] is True
        assert "rollup_behind_stores" not in payload
        assert loads == [rr.rollup_parquet_path(sweeps_root)]  # rollup-served
