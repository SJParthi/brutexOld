"""Cost gate for the revision-probe TTL memo (DEC-O1-GUARANTEE-HARDEN-001 H2).

The two per-poll O(dirs) freshness probes — the ``/results``
``sweep_results_revision`` scandir digest and the oneclick runs-listing
manifest-stat walk (``runs_revision_probe``) — ride ONE
:class:`~brutex.dashboard.lake_revision.RevisionProbeMemo` injected through
``build_app(revision_probe_memo=)``. This bench-dir gate pins the COST law
DETERMINISTICALLY through the REAL routes (probe-count spies + an injected
clock — no wall clock, no flake):

* N polls within one TTL window pay EXACTLY ONE probe sweep per route (the
  O(1)-on-repeat handoff — the pre-H2 shape re-walked the dirs per poll);
* the H2-guard: a poll AFTER the TTL always re-probes — whatever default
  ships, there is no permanent-staleness state.

Harness REUSED from ``tests/unit/test_dashboard_revision_probe_memo.py``
(§11 COMMON — the cross-suite import precedent); the memo PRIMITIVE's own
laws (None-not-cached, fail-loud env, boundary strictness, TTL=0 legacy
pass-through) are unit-pinned in ``tests/unit/test_lake_revision.py``.

HONEST envelope (Golden Rule #10): O(1)-on-repeat within the TTL only — the
first poll per TTL window pays the real O(sweep dirs) / O(run dirs) stat
sweep, staleness is bounded by the TTL (default 1.0 s; the suite runs under
the conftest ``BRUTEX_REVISION_PROBE_TTL_S=0`` legacy belt), and the payload
compute behind the probe keys is the SEPARATE revision-cache family — this
gate bounds the PROBE cost, not the compute.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard import app as app_module
from brutex.dashboard import oneclick_artifacts
from brutex.dashboard.lake_revision import RevisionProbeMemo
from brutex.data.instrument_store import InstrumentStore

from tests.unit.test_dashboard_revision_probe_memo import _client, _FakeClock, _nifty

#: Polls per window — enough that a per-poll regression is unambiguous.
_POLLS = 6


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real DuckDB instrument store rooted in the tmp lake."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


class TestSweepResultsProbeOncePerTtlWindow:
    def test_polls_pay_one_sweep_and_post_ttl_resweeps(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"probe": 0}
        real = app_module.sweep_results_revision

        def _spy(lake_root: Path) -> Any:
            calls["probe"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "sweep_results_revision", _spy)
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            for _ in range(_POLLS):
                assert client.get("/api/v1/sweep/leaderboard").status_code == 200
            assert calls["probe"] == 1, (
                f"expected ONE sweep_results_revision sweep for {_POLLS} polls "
                f"within one TTL window, measured {calls['probe']} — the H2 memo "
                f"has regressed to a per-poll O(sweep dirs) walk"
            )
            clock.now = 60.0  # the TTL elapsed — staleness must be TTL-bounded
            assert client.get("/api/v1/sweep/leaderboard").status_code == 200
            assert calls["probe"] == 2  # the H2-guard: post-TTL always re-probes


class TestOneclickRunsProbeOncePerTtlWindow:
    def test_polls_pay_one_walk_and_post_ttl_rewalks(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"probe": 0}
        real = oneclick_artifacts.runs_revision_probe

        def _spy(results_root: Path) -> Any:
            calls["probe"] += 1
            return real(results_root)

        monkeypatch.setattr(oneclick_artifacts, "runs_revision_probe", _spy)
        # A REAL sweep_runs tree so the probe token is non-None (memoisable).
        (store.path.parent / "sweep_runs").mkdir()
        clock = _FakeClock()
        memo = RevisionProbeMemo(ttl_s=60.0, clock=clock)
        with _client(store, memo) as client:
            for _ in range(_POLLS):
                assert client.get("/api/v1/oneclick/runs").status_code == 200
            assert calls["probe"] == 1, (
                f"expected ONE runs_revision_probe walk for {_POLLS} polls within "
                f"one TTL window, measured {calls['probe']} — the H2 memo has "
                f"regressed to a per-poll O(run dirs) manifest-stat walk"
            )
            clock.now = 60.0
            assert client.get("/api/v1/oneclick/runs").status_code == 200
            assert calls["probe"] == 2
