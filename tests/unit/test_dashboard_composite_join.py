"""Unit suite for :mod:`brutex.dashboard.composite_join` (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

Covers: newest-run selection (the discover_runs (mtime_ns, name) law),
column-projection reads over REAL parquet bytes, per-file fault skip+count,
non-finite/null/junk-cell drops, deterministic first-wins across sidecars,
the honest absent / joined / error states, the aggs path-shape pin
against the REAL ``trade_store.sidecar_paths_for`` law (the glob literal is
mirrored, never imported), and — fix round 1 — the dedicated
``composite_index_revision`` freshness probe (an aggs generation landing
under ``sweep_runs/`` must move the token even though NO sweeps-tree write
follows it).
"""

from __future__ import annotations

import os
import pathlib
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.dashboard.composite_join import (
    _AGGS_GLOB,
    _RESULTS_DEPTH_DIRNAME,
    COMPOSITE_STATE_ABSENT,
    COMPOSITE_STATE_ERROR,
    COMPOSITE_STATE_JOINED,
    composite_index_revision,
    load_composite_index,
)
from brutex.dashboard.lake_revision import ABSENT_TOKEN
from brutex.engine.trade_store import RESULTS_DEPTH_DIRNAME, sidecar_paths_for
from structlog.testing import capture_logs

_RUN = "grid" + "0" * 60


def _seed_aggs(
    results_root: Path,
    rows: list[dict[str, Any]],
    *,
    run_id: str = _RUN,
    instrument: str = "NIFTY",
    tf: str = "1minute",
    stem: str = "bull_k1_deadbeefdeadbeef",
) -> Path:
    """Write one aggs sidecar carrying (run_id, composite_score) rows."""
    depth = results_root.parent / "sweep_runs" / run_id / _RESULTS_DEPTH_DIRNAME
    target = depth / instrument / tf / f"{stem}.aggs.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "run_id": [r["run_id"] for r in rows],
            "composite_score": pl.Series([r["composite_score"] for r in rows], dtype=pl.Float64),
        }
    )
    frame.write_parquet(target)
    return target


class TestPathLawPins:
    def test_aggs_glob_matches_the_real_sidecar_layout(self, tmp_path: Path) -> None:
        """The mirrored glob/dirname literals track the trade_store law."""
        assert _RESULTS_DEPTH_DIRNAME == RESULTS_DEPTH_DIRNAME
        store = tmp_path / "sweeps" / "NIFTY" / "1minute" / "bull_k1_aaaaaaaaaaaaaaaa.parquet"
        paths = sidecar_paths_for(store, tmp_path / "sweeps", run_id=_RUN)
        depth_dir = tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME
        rel = paths.aggs.relative_to(depth_dir)
        # The REAL aggs path is exactly <instrument>/<tf>/<stem>.aggs.parquet —
        # the module's */*/*.aggs.parquet glob shape.
        assert len(rel.parts) == 3
        assert rel.parts[-1].endswith(".aggs.parquet")
        assert _AGGS_GLOB == "*/*/*.aggs.parquet"


class TestStates:
    def test_absent_when_no_run_dirs(self, tmp_path: Path) -> None:
        index = load_composite_index(tmp_path / "sweeps")
        assert index.state == COMPOSITE_STATE_ABSENT
        assert index.run_id is None
        assert index.by_run_id == {}
        assert index.scored_rows == 0

    def test_joined_with_zero_sidecars(self, tmp_path: Path) -> None:
        (tmp_path / "sweep_runs" / _RUN).mkdir(parents=True)
        index = load_composite_index(tmp_path / "sweeps")
        assert index.state == COMPOSITE_STATE_JOINED
        assert index.run_id == _RUN
        assert index.scored_rows == 0

    def test_error_when_listing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sweep_runs" / _RUN).mkdir(parents=True)

        def _boom(self: Path, pattern: str) -> Any:
            raise OSError("wedged volume")

        monkeypatch.setattr(pathlib.Path, "glob", _boom)
        with capture_logs() as logs:
            index = load_composite_index(tmp_path / "sweeps")
        assert index.state == COMPOSITE_STATE_ERROR
        assert index.run_id == _RUN
        assert index.by_run_id == {}
        assert [e for e in logs if e["event"] == "composite_join.listing_failed"]


class TestReads:
    def test_clean_join(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        _seed_aggs(
            results_root,
            [
                {"run_id": f"{1:064x}", "composite_score": 1.25},
                {"run_id": f"{2:064x}", "composite_score": -0.5},
            ],
        )
        with capture_logs() as logs:
            index = load_composite_index(results_root)
        assert index.state == COMPOSITE_STATE_JOINED
        assert index.by_run_id == {f"{1:064x}": 1.25, f"{2:064x}": -0.5}
        assert index.scored_rows == 2
        assert index.skipped_files == 0
        assert index.dropped_values == 0
        # Clean join fires NO partial WARN.
        assert not [e for e in logs if e["event"] == "composite_join.partial"]

    def test_null_nan_and_junk_cells_dropped_counted(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        target = _seed_aggs(
            results_root,
            [
                {"run_id": f"{1:064x}", "composite_score": 1.0},
                {"run_id": f"{2:064x}", "composite_score": None},
                {"run_id": f"{3:064x}", "composite_score": float("nan")},
            ],
        )
        # A junk (empty-string) run_id cell in a second file.
        frame = pl.DataFrame(
            {"run_id": ["", None], "composite_score": pl.Series([2.0, 3.0], dtype=pl.Float64)}
        )
        sibling = target.with_name("bull_k2_deadbeefdeadbeef.aggs.parquet")
        frame.write_parquet(sibling)
        with capture_logs() as logs:
            index = load_composite_index(results_root)
        assert index.by_run_id == {f"{1:064x}": 1.0}
        assert index.dropped_values == 4
        assert index.skipped_files == 0
        (partial,) = [e for e in logs if e["event"] == "composite_join.partial"]
        assert partial["dropped_values"] == 4

    def test_corrupt_sidecar_skipped_counted(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        target = _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        garbage = target.with_name("aaa_corrupt.aggs.parquet")
        garbage.write_bytes(b"not a parquet at all")
        with capture_logs() as logs:
            index = load_composite_index(results_root)
        # The corrupt sibling never blanks the index.
        assert index.by_run_id == {f"{1:064x}": 1.0}
        assert index.skipped_files == 1
        assert [e for e in logs if e["event"] == "composite_join.sidecar_skip"]

    def test_duplicate_run_id_first_file_wins(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        first = _seed_aggs(
            results_root,
            [{"run_id": f"{1:064x}", "composite_score": 1.0}],
            stem="a_first",
        )
        _seed_aggs(
            results_root,
            [{"run_id": f"{1:064x}", "composite_score": 9.0}],
            stem="b_second",
        )
        assert first.name < "b_second.aggs.parquet"
        index = load_composite_index(results_root)
        # Deterministic first-wins over the SORTED file list.
        assert index.by_run_id == {f"{1:064x}": 1.0}
        assert index.scored_rows == 1


class TestNewestRunSelection:
    def test_newest_run_wins(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        old_run = "grid" + "1" * 60
        new_run = "grid" + "2" * 60
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}], run_id=old_run)
        _seed_aggs(results_root, [{"run_id": f"{2:064x}", "composite_score": 2.0}], run_id=new_run)
        runs = tmp_path / "sweep_runs"
        os.utime(runs / old_run, ns=(1_000, 1_000))
        os.utime(runs / new_run, ns=(2_000, 2_000))
        index = load_composite_index(results_root)
        assert index.run_id == new_run
        # ONLY the newest run's sidecars are read (newest-run-wins).
        assert index.by_run_id == {f"{2:064x}": 2.0}


class TestCompositeIndexRevision:
    """Fix round 1 (refuter-confirmed MAJOR): the DEDICATED freshness probe.

    The aggs sidecars land under ``sweep_runs/`` AFTER the sweep's last
    generation-moving write, so the /results composite index needs its OWN
    token — these pins prove the token covers exactly what the loader reads.
    """

    def test_no_runs_tree_is_the_stable_absent_sentinel(self, tmp_path: Path) -> None:
        assert composite_index_revision(tmp_path / "sweeps") == ABSENT_TOKEN
        assert composite_index_revision(tmp_path / "sweeps") == ABSENT_TOKEN

    def test_depth_tree_absent_is_a_stable_per_run_component(self, tmp_path: Path) -> None:
        (tmp_path / "sweep_runs" / _RUN).mkdir(parents=True)
        token = composite_index_revision(tmp_path / "sweeps")
        # DEC-RANKING-SPLUS-TIER-001: the token now ALSO carries the top100
        # stamp component (absent here — the stable per-run absent pair).
        assert token == f"{_RUN}|{ABSENT_TOKEN}|top100:{ABSENT_TOKEN}"
        # Stable across repeat polls (no depth tree yet)…
        assert composite_index_revision(tmp_path / "sweeps") == token
        # …and its FIRST creation moves the token exactly once (the structural
        # absent → digest flip — deterministic regardless of mtime granularity).
        (tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME).mkdir()
        moved = composite_index_revision(tmp_path / "sweeps")
        assert moved is not None
        assert moved != token

    def test_aggs_generation_moves_the_token(self, tmp_path: Path) -> None:
        """A NEW tf dir (an aggs sidecar's parent) is a structural token move,
        and the tf-dir mtime itself is IN the digest — the mechanism by which
        every atomic aggs write (tempfile + os.replace into ``<tf>/``, which
        moves that dir's mtime) invalidates the cached index.
        """
        results_root = tmp_path / "sweeps"
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        # Stray FILES at both levels are ignored (dir-only digest law).
        depth = tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME
        (depth / "stray.txt").write_text("x")
        (depth / "NIFTY" / "stray.txt").write_text("x")
        before = composite_index_revision(results_root)
        assert before is not None
        assert before != ABSENT_TOKEN
        # (a) a second sidecar landing in a NEW tf dir — structural move.
        _seed_aggs(results_root, [{"run_id": f"{2:064x}", "composite_score": 2.0}], tf="5m")
        after_new_tf = composite_index_revision(results_root)
        assert after_new_tf != before
        # (b) the EXISTING tf dir's mtime is load-bearing: simulate the dir
        # mtime move an in-dir aggs write performs (utime keeps the pin
        # deterministic on coarse-mtime filesystems).
        os.utime(depth / "NIFTY" / "1minute", ns=(3_000_000_000, 3_000_000_000))
        after_touch = composite_index_revision(results_root)
        assert after_touch != after_new_tf

    def test_newest_run_switch_moves_the_token(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        old_run = "grid" + "1" * 60
        new_run = "grid" + "2" * 60
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}], run_id=old_run)
        runs = tmp_path / "sweep_runs"
        os.utime(runs / old_run, ns=(1_000, 1_000))
        only_old = composite_index_revision(results_root)
        _seed_aggs(results_root, [{"run_id": f"{2:064x}", "composite_score": 2.0}], run_id=new_run)
        os.utime(runs / new_run, ns=(2_000, 2_000))
        assert composite_index_revision(results_root) != only_old

    def test_depth_stat_fault_returns_none_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME).mkdir(parents=True)
        depth = tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME
        real_stat = os.stat

        def _stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(str(path)) == depth:
                raise PermissionError("wedged volume")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _stat)
        with capture_logs() as logs:
            assert composite_index_revision(tmp_path / "sweeps") is None
        assert [e for e in logs if e["event"] == "composite_join.revision_stat_failed"]

    def test_depth_scan_fault_returns_none_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        depth = tmp_path / "sweep_runs" / _RUN / _RESULTS_DEPTH_DIRNAME
        depth.mkdir(parents=True)
        real_scandir = os.scandir

        def _scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            if Path(str(path)) == depth:
                raise OSError("scan fault")
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", _scandir)
        with capture_logs() as logs:
            assert composite_index_revision(tmp_path / "sweeps") is None
        assert [e for e in logs if e["event"] == "composite_join.revision_scan_failed"]


class TestZeroManifestIo:
    """Fix round 2 (refuter-confirmed MINOR): probe + loader are NAME-only.

    Pre-fix both reached the newest run through ``discover_runs``, whose
    per-selected-dir :class:`RunInfo` probe read + json-parsed that run's
    ``manifest.json`` on EVERY /results poll — an undisclosed O(manifest)
    file read + parse whose parsed value (the ``suspect`` flag) neither
    caller ever used. The killing spy: seed a run CARRYING a manifest.json,
    poll the probe 10x + build the index once — ZERO ``load_manifest``
    calls (pre-fix this counted 11).
    """

    def test_probe_and_loader_never_read_or_parse_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.dashboard.oneclick_artifacts as oa

        results_root = tmp_path / "sweeps"
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        manifest = tmp_path / "sweep_runs" / _RUN / "manifest.json"
        manifest.write_text('{"suspect": false}', encoding="utf-8")
        calls: list[str] = []
        real_load = oa.load_manifest

        def _spy(run_dir: Path) -> Any:
            calls.append(str(run_dir))
            return real_load(run_dir)

        monkeypatch.setattr(oa, "load_manifest", _spy)
        tokens = {composite_index_revision(results_root) for _ in range(10)}
        assert len(tokens) == 1
        assert None not in tokens
        index = load_composite_index(results_root)
        assert index.run_id == _RUN
        assert index.by_run_id == {f"{1:064x}": 1.0}
        # The whole point: the newest-run selection is name-only.
        assert calls == []


# ---------------------------------------------------------------------------
# DEC-RANKING-SPLUS-TIER-001 — the additive tier join over the SAME index
# ---------------------------------------------------------------------------

import json  # noqa: E402


def _write_top100_doc(results_root: Path, entries: list[dict[str, Any]], *, run_id: str = _RUN) -> None:
    """One display-reader-valid top100.json object root under sweep_runs/<run_id>."""
    run_dir = results_root.parent / "sweep_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "run_id": run_id,
        "preset": "min_dd_first",
        "source": {"pool": len(entries)},
        "baselines": {},
        "entries": entries,
    }
    (run_dir / "top100.json").write_text(json.dumps(doc), encoding="utf-8")


def _tier_entry(rank: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "rank": rank,
        "mask_hex": "0x05",
        "conditions": ["PDH_CROSS_UP"],
        "tf": "5m",
        "direction": "long",
        "arm": "SL1.0_TP2.0",
        "metrics": {"sharpe": 1.0},
        "regime": {},
        "vix_band": None,
        "family_id": "fam-0001",
    }
    row.update(overrides)
    return row


class TestTierIndex:
    def test_tier_map_joins_from_the_newest_runs_top100(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        _write_top100_doc(
            results_root,
            [
                _tier_entry(
                    1,
                    run_id=f"{1:064x}",
                    tier={"label": "A", "blocked_next": ["min_trades"]},
                ),
                # A row WITHOUT the additive keys contributes nothing —
                # honest absence, never a fabricated tier.
                _tier_entry(2),
            ],
        )
        index = load_composite_index(results_root)
        assert index.state == COMPOSITE_STATE_JOINED
        tier = index.tier_by_run_id[f"{1:064x}"]
        assert tier["label"] == "A"
        assert tier["blocked_next"] == ["min_trades"]
        # The ONE display reader stamped the server-built text — the /results
        # join can never disagree with the /screener wire.
        assert tier["status_text"] == "A — blocked from S: min_trades"
        assert len(index.tier_by_run_id) == 1

    def test_absent_or_corrupt_artifact_reads_an_empty_tier_map(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        # No top100.json at all → empty map (never a raise).
        assert load_composite_index(results_root).tier_by_run_id == {}
        run_dir = tmp_path / "sweep_runs" / _RUN
        (run_dir / "top100.json").write_text("{not json", encoding="utf-8")
        assert load_composite_index(results_root).tier_by_run_id == {}


class TestTop100StampRevisionComponent:
    def test_top100_stamp_write_moves_the_token(self, tmp_path: Path) -> None:
        """The tier join reads top100.json, which lands AFTER the depth-tree
        aggs writes — the probe folds the STAMP's stat so a poll landing in
        that window can never freeze a tier-less index (the fix-round-1
        composite doctrine applied to this DEC's reader)."""
        results_root = tmp_path / "sweeps"
        _seed_aggs(results_root, [{"run_id": f"{1:064x}", "composite_score": 1.0}])
        before = composite_index_revision(results_root)
        assert before is not None
        stamp = tmp_path / "sweep_runs" / _RUN / "top100.stamp.json"
        stamp.write_text('{"stage": "top100"}', encoding="utf-8")
        os.utime(stamp, ns=(5_000_000_000, 5_000_000_000))
        after = composite_index_revision(results_root)
        assert after is not None
        assert after != before
        # …and a rewrite (new mtime) moves it again.
        os.utime(stamp, ns=(6_000_000_000, 6_000_000_000))
        assert composite_index_revision(results_root) != after

    def test_stamp_stat_fault_is_a_probe_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sweep_runs" / _RUN).mkdir(parents=True)
        stamp = tmp_path / "sweep_runs" / _RUN / "top100.stamp.json"
        real_stat = os.stat

        def _stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(str(path)) == stamp:
                raise PermissionError("wedged volume")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _stat)
        with capture_logs() as logs:
            assert composite_index_revision(tmp_path / "sweeps") is None
        assert [e for e in logs if e["event"] == "composite_join.revision_stat_failed"]
