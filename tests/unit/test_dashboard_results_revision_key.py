"""/results keyed on the sweep-results CONTENT revision (DEC-LEDGER-DELTA-O-PAGE-001).

Sub-part (c): ``api_sweep_leaderboard`` no longer keys its cache on
``_summary_cache.generation`` (which the pull funnel's ``mark_dirty`` bumps per
window tick, and which a CLI ``brutex sweep-all`` in a separate process never
bumps at all) — it folds the NEW ``lake_revision.sweep_results_revision`` token
(sweeps directory-mtime digest + the audit chain's file stat) into the cache KEY
at a fixed generation.

Covers T-c1..T-c8: the mark_dirty-storm immunity, every invalidation path
(dashboard flush / CLI flush / store unlink / audit-only ``SWEEP_START`` append —
the dir-mtime-blind gap), the probe-outage fresh-uncached arm, the relpath drift
pin, and route-vs-direct payload parity; plus branch-level unit fixtures for
``sweep_results_revision`` itself.
"""

# The drift-pin tests read module-private constants deliberately.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard import app as app_module
from brutex.dashboard import lake_revision as lake_revision_mod
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_revision import ABSENT_TOKEN, sweep_results_revision, sweeps_revision
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


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


def _row(seed: int, *, sharpe: float = 1.0) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
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
    )


def _write_sweeps(lake_root: Path, rows: list[ResultRow], *, tf: str = "5m") -> Path:
    dest = lake_root / "sweeps" / "NIFTY" / tf / f"bull_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _append_sweep_start(lake_root: Path, *, snapshot: str) -> Path:
    """Append one ``SWEEP_START`` to the tree's REAL hash-chained audit log."""
    from brutex.orchestrator.audit import AuditWriter

    audit_path = lake_root / "sweeps" / "audit" / "sweep-all.jsonl"
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        writer.append(
            action="SWEEP_START",
            subject="sweep:test",
            payload={
                "instrument": "NIFTY",
                "tf": "5m",
                "direction": "bull",
                "mode": "A",
                "k": 1,
                "data_snapshot_hash": snapshot,
            },
        )
    return audit_path


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def summary_cache() -> SummaryCache:
    return SummaryCache()


@pytest.fixture
def client(store: InstrumentStore, summary_cache: SummaryCache) -> Iterator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scan_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every ``load_sweep_scan`` a leaderboard build performs.

    Patched at BOTH resolution sites: the route's level-1 compute closure reads
    the ``app`` module's imported name, while the rev-``None`` fresh arm goes
    through ``build_sweep_leaderboard(scan=None)``, whose inline scan resolves
    the ``sweep_results_view`` module global — one counter sees both.
    """
    from brutex.dashboard import sweep_results_view as view_mod

    calls = {"scan": 0}
    real = view_mod.load_sweep_scan

    def _spy(lake_root: Path) -> Any:
        calls["scan"] += 1
        return real(lake_root)

    monkeypatch.setattr(app_module, "load_sweep_scan", _spy)
    monkeypatch.setattr(view_mod, "load_sweep_scan", _spy)
    return calls


class TestRevisionKeyedRoute:
    """T-c1..T-c6 + T-c8 — the route-level invalidation matrix."""

    def test_mark_dirty_storm_does_not_invalidate(
        self,
        client: TestClient,
        store: InstrumentStore,
        summary_cache: SummaryCache,
        scan_spy: dict[str, int],
    ) -> None:
        """T-c1: a ×100 mid-pull ``mark_dirty`` storm at an unchanged sweeps tree
        never re-scans — the compute spy runs ONCE across every poll (this FAILED
        under the pre-change ``_summary_cache.generation`` keying, where every
        bump forced a full O(stores × rows) + audit re-read).
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        first = client.get("/api/v1/sweep/leaderboard").json()
        assert first["total_rows"] == 1
        assert scan_spy["scan"] == 1
        for _ in range(100):
            summary_cache.mark_dirty()
        for _ in range(5):
            assert client.get("/api/v1/sweep/leaderboard").json() == first
        assert scan_spy["scan"] == 1  # zero re-scans across the storm

    def test_scan_reused_across_filter_and_rank_keys(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """Different filter/rank/top keys within ONE revision share the ONE cached
        SweepScan — the level-1 entry is loaded once, every click re-ranks it.
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1, sharpe=0.5), _row(2, sharpe=2.5)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 2
        assert (
            client.get("/api/v1/sweep/leaderboard?rank_by=net_pnl_paisa").json()["rank_by"]
            == "net_pnl_paisa"
        )
        assert (
            client.get("/api/v1/sweep/leaderboard?instrument=NIFTY&top=1").json()["total_rows"] == 2
        )
        assert scan_spy["scan"] == 1  # three distinct rank keys, one scan

    def test_dashboard_style_flush_recomputes(
        self,
        client: TestClient,
        store: InstrumentStore,
        summary_cache: SummaryCache,
        scan_spy: dict[str, int],
    ) -> None:
        """T-c2: a dashboard-sweep-style store flush (+ its on_success mark_dirty)
        moves the revision → the next poll re-reads the new winners.
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 1
        _write_sweeps(lake_root, [_row(2)], tf="15m")
        summary_cache.mark_dirty()  # the kept (now-redundant-for-/results) hook
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert fresh["total_rows"] == 2
        assert scan_spy["scan"] == 2

    def test_cli_style_flush_recomputes_without_mark_dirty(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """T-c3: a CLI-style flush (NO ``mark_dirty`` — a separate process) still
        invalidates — the case the old generation keying NEVER covered.
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 1
        _write_sweeps(lake_root, [_row(2)], tf="15m")  # no generation bump at all
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert fresh["total_rows"] == 2
        assert scan_spy["scan"] == 2

    def test_store_unlink_recomputes(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """T-c4: deleting a store moves its parent dir's mtime → recompute."""
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        second = _write_sweeps(lake_root, [_row(2)], tf="15m")
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 2
        # A coarse-mtime filesystem can stamp an unlink into the SAME tick as the
        # create; the tiny sleep keeps the dir-mtime signal deterministic.
        time.sleep(0.05)
        second.unlink()
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert fresh["total_rows"] == 1
        assert scan_spy["scan"] == 2

    def test_audit_only_append_recomputes(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """T-c5: an append to the EXISTING audit chain moves NO directory mtime —
        the chain's file-stat component (the judges' confirmed ``sweeps_revision``
        blindness) is what invalidates, and the era honesty fields refresh.
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        era_a, era_b = "a" * 64, "b" * 64
        _append_sweep_start(lake_root, snapshot=era_a)  # chain EXISTS before the poll
        first = client.get("/api/v1/sweep/leaderboard").json()
        assert first["snapshot_hashes"] == [era_a]
        # The gap being pinned: the append moves NO directory mtime…
        dirs_before = sweeps_revision(lake_root)
        _append_sweep_start(lake_root, snapshot=era_b)
        assert sweeps_revision(lake_root) == dirs_before  # dir digest is blind
        # …yet the composite token catches it and the next poll re-reads.
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert fresh["snapshot_hashes"] == [era_a, era_b]
        assert fresh["mixed_snapshot_eras"] is True
        assert scan_spy["scan"] == 2

    def test_probe_outage_serves_last_known_labelled(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """T-c6 (REWRITTEN by DEC-CACHE-SERVE-LAST-KNOWN-001): a ``None``
        revision (probe outage — `sweep_results_revision` returns None ONLY on
        a genuine stat fault, never for an absent tree) serves the LAST-KNOWN
        payload labelled with the additive ``revision_probe`` degrade marker —
        ONE build per outage, never a per-poll compute storm; recovery resumes
        normal one-scan-per-revision caching and drops the marker.
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        # Prime the cache pre-outage (the last-known entry).
        clean = client.get("/api/v1/sweep/leaderboard").json()
        assert clean["total_rows"] == 1
        assert "revision_probe" not in clean  # additive — absent on clean polls
        assert scan_spy["scan"] == 1
        outage = pytest.MonkeyPatch()
        try:
            outage.setattr(app_module, "sweep_results_revision", lambda _root: None)
            for _ in range(3):
                payload = client.get("/api/v1/sweep/leaderboard").json()
                assert payload["total_rows"] == 1  # the last-known rows
                assert payload["revision_probe"] == "outage — serving last-known"
            # ZERO extra scans across the outage — the last-known entry served
            # (pre-fix: every outage poll paid the full O(stores×rows) build).
            assert scan_spy["scan"] == 1
        finally:
            outage.undo()
        # Clean recovery: same revision as the primed entry → pure hit, no marker.
        recovered = client.get("/api/v1/sweep/leaderboard").json()
        assert recovered["total_rows"] == 1
        assert "revision_probe" not in recovered
        assert scan_spy["scan"] == 1

    def test_probe_outage_cold_start_seeds_once(
        self, client: TestClient, store: InstrumentStore, scan_spy: dict[str, int]
    ) -> None:
        """An outage with NO primed entry computes ONCE (seeded, labelled) —
        a persistent outage no longer pays the build per poll — and the first
        post-recovery poll recomputes fresh (the seed's sentinel generation
        can never read as clean).
        """
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        outage = pytest.MonkeyPatch()
        try:
            outage.setattr(app_module, "sweep_results_revision", lambda _root: None)
            first = client.get("/api/v1/sweep/leaderboard").json()
            assert first["total_rows"] == 1
            assert first["revision_probe"] == "outage — computed fresh (seeded as last-known)"
            assert scan_spy["scan"] == 1
            second = client.get("/api/v1/sweep/leaderboard").json()
            assert second["revision_probe"] == "outage — serving last-known"
            assert scan_spy["scan"] == 1  # the seed served — no per-poll storm
        finally:
            outage.undo()
        # Recovery: the seed is generation-sentinel-stamped → a real revision
        # always recomputes (never a lingering outage seed served as clean).
        recovered = client.get("/api/v1/sweep/leaderboard").json()
        assert recovered["total_rows"] == 1
        assert "revision_probe" not in recovered
        assert scan_spy["scan"] == 2

    def test_route_payload_matches_direct_build(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """T-c8: the cached route payload is byte-identical to a direct
        ``build_sweep_leaderboard(...).to_dict()`` over the same tree + params.
        """
        from brutex.dashboard.sweep_results_view import build_sweep_leaderboard

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1, sharpe=0.5), _row(2, sharpe=2.5)])
        _append_sweep_start(lake_root, snapshot="a" * 64)
        via_route = client.get(
            "/api/v1/sweep/leaderboard?instrument=NIFTY&rank_by=sharpe_ratio&top=1"
        ).json()
        direct = build_sweep_leaderboard(
            lake_root, instrument="NIFTY", rank_by="sharpe_ratio", top_n=1
        ).to_dict()
        assert via_route == direct


class TestSweepResultsRevisionToken:
    """Branch-level unit fixtures for ``lake_revision.sweep_results_revision``."""

    def test_absent_tree_is_the_stable_sentinel_composite(self, tmp_path: Path) -> None:
        """No sweeps tree at all → ``absent|absent`` (stable — never oscillates)."""
        token = sweep_results_revision(tmp_path)
        assert token == f"{ABSENT_TOKEN}|{ABSENT_TOKEN}"
        assert token == sweep_results_revision(tmp_path)

    def test_absent_chain_composes_with_the_dir_digest(self, tmp_path: Path) -> None:
        """A sweeps tree with no audit chain → ``<digest>|absent``; the chain's
        first-ever creation moves the composite exactly once.
        """
        _write_sweeps(tmp_path, [_row(1)])
        token = sweep_results_revision(tmp_path)
        assert token is not None
        assert token.endswith(f"|{ABSENT_TOKEN}")
        _append_sweep_start(tmp_path, snapshot="a" * 64)
        created = sweep_results_revision(tmp_path)
        assert created is not None
        assert created != token
        assert not created.endswith(f"|{ABSENT_TOKEN}")

    def test_audit_append_moves_the_composite_but_not_the_dir_digest(self, tmp_path: Path) -> None:
        """The invalidation gap all three judges confirmed: an append to the
        EXISTING chain moves no directory mtime (``sweeps_revision`` is blind) —
        the ``(mtime_ns:size)`` file stat closes it.
        """
        _write_sweeps(tmp_path, [_row(1)])
        _append_sweep_start(tmp_path, snapshot="a" * 64)
        dirs_before = sweeps_revision(tmp_path)
        composite_before = sweep_results_revision(tmp_path)
        _append_sweep_start(tmp_path, snapshot="b" * 64)
        assert sweeps_revision(tmp_path) == dirs_before
        assert sweep_results_revision(tmp_path) != composite_before

    def test_dirs_probe_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing OSError on the sweeps root → ``None`` (no signal)."""
        (tmp_path / "sweeps").mkdir()
        real_stat = os.stat

        def _deny(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if str(target).endswith("sweeps"):
                raise PermissionError("synthetic sweeps-root stat outage")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(lake_revision_mod.os, "stat", _deny)
        assert sweep_results_revision(tmp_path) is None

    def test_chain_probe_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing OSError on the audit chain stat → ``None`` (no signal)."""
        _write_sweeps(tmp_path, [_row(1)])
        real_stat = os.stat

        def _deny(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if str(target).endswith("sweep-all.jsonl"):
                raise PermissionError("synthetic audit-chain stat outage")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(lake_revision_mod.os, "stat", _deny)
        assert sweep_results_revision(tmp_path) is None

    def test_sweep_audit_relpath_pinned_to_the_view_copy(self) -> None:
        """T-c7: the not-imported relpath copy cannot drift from the view's
        (lake_revision stays stdlib-lean — the ``_SWEEPS_DIRNAME`` precedent).
        """
        from brutex.dashboard import sweep_results_view as view_mod

        assert lake_revision_mod._SWEEP_AUDIT_RELPATH == view_mod._AUDIT_RELPATH
