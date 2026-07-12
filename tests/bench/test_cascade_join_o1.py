"""O-discipline pins for the cascade display join (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

Deterministic teeth first (the house law): the /results cascade loader parses
the report ONCE per stat token (a parse-count spy across N polls), a repeat
poll's freshness probe is exactly ONE extra ``os.stat``
(``cascade_report_revision`` — a syscall-count spy), and the flattener's
per-candidate lookup stays flat as the report grows 100 → 10,000 verdicts
(the wall-clock ratio rides ``ratio_ceiling``). The composite index reads
once per generation (route-level cache — pinned in the route suite; here the
loader itself is pinned single-read via a read spy).

Honest envelope (Golden Rule #10): ``build_cascade_join`` is O(report) per
invocation BY DESIGN — the flat pin below measures the O(1) LOOKUP over the
built join, never claims the parse is O(1).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("structlog")

from brutex.dashboard import lake_revision as lr
from brutex.dashboard import sweep_results_view as srv
from brutex.engine.cascade_join import build_cascade_join
from brutex.engine.cascade_wire import CascadeWireReport, write_report
from brutex.metrics.cascade import CandidateVerdict, CascadeReport

from tests.bench._o1_tiers import ratio_ceiling

# pyright: reportPrivateUsage=none

_BEST_OF = 5


def _best_of(fn: Any, *, repeat: int = _BEST_OF) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _wire(n_verdicts: int) -> CascadeWireReport:
    verdicts = tuple(
        CandidateVerdict(
            candidate_id=f"{i:064x}",
            passed=i % 2 == 0,
            rejected_at=None,
            detail="survived all Stage-4 filters",
            composite_score=1.0,
            cpcv_pass_rate=0.5,
        )
        if i % 2 == 0
        else CandidateVerdict(
            candidate_id=f"{i:064x}",
            passed=False,
            rejected_at=None,
            detail="d",
        )
        for i in range(n_verdicts)
    )
    survivors = tuple(v.candidate_id for v in verdicts if v.passed)
    report = CascadeReport(
        verdicts=verdicts,
        survivor_ids=survivors,
        n_input=n_verdicts,
        n_survived=len(survivors),
        stage_survivors={},
    )
    return CascadeWireReport(sweep_id="s" * 64, groups=(("NIFTY", "1m", report),))


class TestOneParsePerToken:
    def test_n_polls_one_parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "_JOIN_MEMO", None)
        write_report(tmp_path / "sweeps", _wire(50))
        token = lr.cascade_report_revision(tmp_path)
        assert token is not None
        parses: list[int] = []
        real_loads = json.loads

        def _spy(*args: Any, **kwargs: Any) -> Any:
            parses.append(1)
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(srv.json, "loads", _spy)
        for _ in range(50):
            load = srv.load_cascade_join_file(tmp_path / "sweeps", token=token)
            assert load.state == "joined"
        # 50 polls → exactly ONE report parse (the single-entry memo law).
        assert len(parses) == 1


class TestOneStatPerPoll:
    def test_revision_probe_is_one_os_stat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_report(tmp_path / "sweeps", _wire(5))
        calls: list[str] = []
        real_stat = lr.os.stat

        def _spy(path: Any, **kwargs: Any) -> Any:
            calls.append(str(path))
            return real_stat(path, **kwargs)

        monkeypatch.setattr(lr.os, "stat", _spy)
        lr.cascade_report_revision(tmp_path)
        # The repeat-poll freshness probe adds EXACTLY one os.stat.
        assert len(calls) == 1
        assert calls[0].endswith("cascade-report.json")


class TestFlatLookup:
    def test_lookup_flat_100_vs_10000_verdicts(self) -> None:
        small = build_cascade_join(json.loads(json.dumps(_wire_dict(100))))
        large = build_cascade_join(json.loads(json.dumps(_wire_dict(10_000))))
        probe_small = [f"{i:064x}" for i in range(0, 100, 7)]
        probe_large = [f"{i:064x}" for i in range(0, 10_000, 7)][: len(probe_small)]

        def _sweep(join: Any, ids: list[str]) -> None:
            sink = False
            for _ in range(2_000):
                for cid in ids:
                    join.by_candidate.get(cid)
                    sink = cid in join.ambiguous_ids
            assert sink is False

        t_small = _best_of(lambda: _sweep(small, probe_small))
        t_large = _best_of(lambda: _sweep(large, probe_large))
        assert t_large <= t_small * ratio_ceiling(8.0), (
            f"per-run_id cascade lookup must stay flat: {t_small=} {t_large=}"
        )


def _wire_dict(n: int) -> dict[str, Any]:
    from brutex.engine.cascade_wire import wire_report_to_dict

    return wire_report_to_dict(_wire(n))


class TestCompositeSingleRead:
    def test_loader_reads_each_sidecar_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars as pl
        from brutex.dashboard import composite_join as cj

        depth = tmp_path / "sweep_runs" / ("g" * 64) / "results-depth" / "NIFTY" / "1m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {"run_id": ["a" * 64], "composite_score": pl.Series([1.0], dtype=pl.Float64)}
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        reads: list[int] = []
        real_read = pl.read_parquet

        def _spy(*args: Any, **kwargs: Any) -> Any:
            reads.append(1)
            return real_read(*args, **kwargs)

        # composite_join does `import polars as pl` function-locally — the
        # SAME module object, so patching the polars attribute reaches it.
        monkeypatch.setattr(pl, "read_parquet", _spy)
        assert cj is not None
        index = cj.load_composite_index(tmp_path / "sweeps")
        assert index.scored_rows == 1
        assert len(reads) <= 1  # one sidecar → at most one read per build


class TestCompositeRevisionZeroManifestIo:
    """Fix round 2 (refuter-confirmed MINOR): the ``composite_index_revision``
    per-poll cost is dir stats ONLY — the ``TestOneStatPerPoll`` twin for the
    composite probe. Pre-fix each poll rode ``discover_runs`` →
    ``_probe_run_dir_known`` → ``load_manifest``: one manifest.json file read
    + ``json.loads`` per poll whose parsed value the probe never used. This
    pin regression-guards the path against a future re-heavying of the
    newest-run selection (e.g. re-adding per-poll manifest parses).
    """

    def test_n_polls_zero_manifest_reads_or_parses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars as pl
        from brutex.dashboard import composite_join as cj
        from brutex.dashboard import oneclick_artifacts as oa

        run = "g" * 64
        depth = tmp_path / "sweep_runs" / run / "results-depth" / "NIFTY" / "1m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {"run_id": ["a" * 64], "composite_score": pl.Series([1.0], dtype=pl.Float64)}
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        (tmp_path / "sweep_runs" / run / "manifest.json").write_text(
            '{"suspect": false}', encoding="utf-8"
        )
        manifest_loads: list[int] = []
        real_load = oa.load_manifest

        def _load_spy(run_dir: Any) -> Any:
            manifest_loads.append(1)
            return real_load(run_dir)

        monkeypatch.setattr(oa, "load_manifest", _load_spy)
        parses: list[int] = []
        real_json_loads = json.loads

        def _json_spy(*args: Any, **kwargs: Any) -> Any:
            parses.append(1)
            return real_json_loads(*args, **kwargs)

        # String-target form: patches the SAME stdlib json module object the
        # oneclick module holds, without a static module-attr access.
        monkeypatch.setattr("brutex.dashboard.oneclick_artifacts.json.loads", _json_spy)
        for _ in range(10):
            token = cj.composite_index_revision(tmp_path / "sweeps")
            assert token is not None
        # 10 polls → ZERO manifest reads AND zero JSON parses (dir stats only).
        assert manifest_loads == []
        assert parses == []
