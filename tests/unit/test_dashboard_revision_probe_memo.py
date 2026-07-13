"""Route wiring for the revision-probe TTL memo (DEC-O1-GUARANTEE-HARDEN-001 H2).

The two per-poll O(dirs) freshness probes — the oneclick runs-listing
manifest-stat walk (``runs_revision_probe``) and the ``/results``
``sweep_results_revision`` scandir digest — ride ONE
:class:`~brutex.dashboard.lake_revision.RevisionProbeMemo` injected through
``build_app(revision_probe_memo=)``. These pins prove, with counting probe
spies + an injected clock:

* N polls within one TTL window pay exactly ONE probe sweep (per route);
* a poll AFTER the TTL always re-probes — the H2-guard: whatever default
  ships, there is no permanent-staleness state (a post-TTL write is SEEN);
* the suite-default ``BRUTEX_REVISION_PROBE_TTL_S=0`` (tests/conftest.py) is
  the byte-identical legacy pass-through — every poll probes.

The memo PRIMITIVE's own laws (None-not-cached, fail-loud env, boundary
strictness) are unit-pinned in ``tests/unit/test_lake_revision.py``; this file
pins only the app WIRING.
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
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard import app as app_module
from brutex.dashboard import oneclick_artifacts
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_revision import RevisionProbeMemo
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _nifty() -> Instrument:
    """One admitted index row so the store opens with a real ``path``."""
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
    """A real DuckDB instrument store rooted in the tmp lake."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


class _FakeClock:
    """An injectable monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(store: InstrumentStore, memo: RevisionProbeMemo) -> TestClient:
    return TestClient(build_app(DashboardState(), instrument_store=store, revision_probe_memo=memo))


class TestSweepResultsProbeMemo:
    """/results — the ``sweep_results_revision`` sweep runs once per TTL window."""

    @pytest.fixture
    def probe_spy(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        calls = {"probe": 0}
        real = app_module.sweep_results_revision

        def _spy(lake_root: Path) -> Any:
            calls["probe"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "sweep_results_revision", _spy)
        return calls

    def test_polls_within_ttl_pay_one_probe_and_post_ttl_reprobes(
        self, store: InstrumentStore, probe_spy: dict[str, int]
    ) -> None:
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            for _ in range(3):
                assert client.get("/api/v1/sweep/leaderboard").status_code == 200
            assert probe_spy["probe"] == 1  # three polls, ONE O(dirs) sweep
            clock.now = 60.0  # the TTL elapsed — the next poll MUST re-probe
            assert client.get("/api/v1/sweep/leaderboard").status_code == 200
            assert probe_spy["probe"] == 2

    def test_ttl_zero_default_is_the_legacy_per_poll_probe(
        self, store: InstrumentStore, probe_spy: dict[str, int]
    ) -> None:
        """The suite default (conftest TTL=0) probes on EVERY poll — legacy."""
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            for _ in range(3):
                assert client.get("/api/v1/sweep/leaderboard").status_code == 200
        assert probe_spy["probe"] == 3

    def test_post_ttl_write_is_seen_never_permanently_stale(self, store: InstrumentStore) -> None:
        """The H2-guard end-to-end: a write after the TTL reaches the payload."""
        pl = pytest.importorskip("polars")
        lake_root = store.path.parent
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 0
            # A sweeps-tree artifact lands AFTER the first (memoised) probe.
            dest = lake_root / "sweeps" / "NIFTY" / "5m"
            dest.mkdir(parents=True)
            pl.DataFrame({"x": [1]}).write_parquet(dest / "not-a-store.parquet")
            # Within the TTL the memoised token holds (staleness <= TTL)...
            clock.now = 59.0
            client.get("/api/v1/sweep/leaderboard")
            # ...and one tick past the TTL the probe re-runs: the moved token
            # re-keys the cache and the fresh scan SEES the new tree (the file
            # is a non-store parquet, so the honest signal is the scan's
            # ``skipped`` undecodable-store counter — total_rows stays 0 but
            # the payload now counts the new file, proving a recompute).
            clock.now = 61.0
            fresh = client.get("/api/v1/sweep/leaderboard").json()
            assert fresh["skipped"] == 1

    def test_probe_outage_none_is_never_memoised(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage verdict (``None``) re-derives per poll — never held for the TTL."""
        calls = {"probe": 0}

        def _outage(lake_root: Path) -> Any:
            calls["probe"] += 1
            return None

        monkeypatch.setattr(app_module, "sweep_results_revision", _outage)
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            for _ in range(2):
                assert client.get("/api/v1/sweep/leaderboard").status_code == 200
        assert calls["probe"] == 2


class TestOneclickRunsProbeMemo:
    """/api/v1/oneclick/runs — the O(run dirs) manifest-stat walk once per TTL window."""

    @pytest.fixture
    def probe_spy(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        calls = {"probe": 0}
        real = oneclick_artifacts.runs_revision_probe

        def _spy(results_root: Path) -> Any:
            calls["probe"] += 1
            return real(results_root)

        monkeypatch.setattr(oneclick_artifacts, "runs_revision_probe", _spy)
        return calls

    def test_polls_within_ttl_pay_one_walk_and_post_ttl_rewalks(
        self, store: InstrumentStore, probe_spy: dict[str, int]
    ) -> None:
        # A REAL sweep_runs tree so the probe token is non-None (memoisable).
        (store.path.parent / "sweep_runs").mkdir()
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            for _ in range(4):
                assert client.get("/api/v1/oneclick/runs").status_code == 200
            assert probe_spy["probe"] == 1
            clock.now = 60.0
            assert client.get("/api/v1/oneclick/runs").status_code == 200
            assert probe_spy["probe"] == 2

    def test_absent_tree_verdict_is_not_memoised(
        self, store: InstrumentStore, probe_spy: dict[str, int]
    ) -> None:
        """An absent ``sweep_runs/`` (token None) is re-probed per poll — its
        CREATION is therefore seen immediately, never TTL-delayed."""
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            assert client.get("/api/v1/oneclick/runs").json()["status"] == "awaiting"
            assert client.get("/api/v1/oneclick/runs").json()["status"] == "awaiting"
            assert probe_spy["probe"] == 2  # never cached — absent != memoisable


class TestBuildAppEagerTtlValidation:
    """Review round 1 MINOR-1 — junk env refuses at build_app, not first request.

    The house env-kill-switch idiom (BRUTEX_LOOP_WATCHDOG / market-hours /
    today-gate) fails loud at STARTUP; the memo previously deferred junk to a
    first-request 500 on the two wired routes. ``build_app`` now resolves the
    TTL eagerly when it constructs the default memo (an INJECTED memo still
    validates itself lazily — its own contract, unchanged).
    """

    def test_junk_env_refuses_at_build_app(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
        with pytest.raises(ValueError, match="BRUTEX_REVISION_PROBE_TTL_S"):
            build_app(DashboardState(), instrument_store=store)

    def test_valid_env_builds_and_serves(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "2.5")
        with TestClient(build_app(DashboardState(), instrument_store=store)) as client:
            assert client.get("/api/v1/oneclick/runs").status_code == 200

    def test_injected_memo_skips_the_eager_resolve(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An injected memo owns its own TTL — build_app must not read the env."""
        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
        memo = RevisionProbeMemo(ttl_s=60.0, clock=_FakeClock())
        with _client(store, memo) as client:  # junk env untouched — no raise
            assert client.get("/api/v1/oneclick/runs").status_code == 200
