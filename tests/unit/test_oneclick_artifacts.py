"""Unit suite for ``brutex/dashboard/oneclick_artifacts.py`` (DEC-OPERATOR-PAGE-REAL-001).

Drives the ONE one-click integration adapter to 100% line+branch STANDALONE
(the ci.yml per-module gate). Fails-before framing: the adapter is NEW in this
change; every degrade law is pinned here so a future edit can never silently
fabricate data (a missing artifact MUST read ``awaiting``, a broken one MUST
read ``corrupt``/``partial`` with the defect NAMED, and no reader may raise
out of a poll path). The chain-law tests pin the ASSUMED algorithm behind the
``chain_hash`` seam (generator↔verifier self-consistency ONLY — "algorithm
assumed until P2 merges" must appear on EVERY verdict, T-M1).
"""

# Whitebox pins on the module's private seams (_CHAIN_GENESIS,
# _probe_run_dir_known, the log-once reset) by design.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from brutex.dashboard import oneclick_artifacts as oc

# ── helpers ─────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _valid_manifest(run_id: str = "r-unit01", **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "selection": {"symbols": ["NSE-NIFTY"]},
        "estimate": {"trials": 10},
        "actual": {
            "enumerated": 10,
            "tested": 8,
            "skipped": 2,
            "funnel": [{"stage": "tested", "count": 8}, {"stage": "gates", "count": 5}],
        },
        "per_stage_timings": [{"stage": "sweep", "seconds": 1.5}],
        "per_door_kills": [{"door": "gates", "killed": 3}],
        "dedup_skips": 2,
        "tripwires": [],
        "suspect": False,
    }
    manifest.update(overrides)
    return manifest


def _chained_lines(bodies: list[dict[str, Any]]) -> list[str]:
    """Chain *bodies* through the adapter's OWN seam (self-consistency law)."""
    prev = oc._CHAIN_GENESIS
    lines: list[str] = []
    for body in bodies:
        digest = oc.chain_hash(prev, oc.canonical_record_bytes(body))
        record = dict(body)
        record["hash"] = digest
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
        prev = digest
    return lines


def _event_bodies(n: int, run_id: str = "r-unit01") -> list[dict[str, Any]]:
    return [
        {
            "ts_utc": f"2026-07-07T04:30:{i:02d}Z",
            "run_id": run_id,
            "kind": f"sweep.tick_{i}",
            "payload": {"i": i},
        }
        for i in range(n)
    ]


def _write_events(run_dir: Path, bodies: list[dict[str, Any]], *, tail: str = "") -> Path:
    path = run_dir / oc.EVENTS_FILENAME
    text = "\n".join(_chained_lines(bodies)) + "\n" if bodies else ""
    _write(path, text + tail)
    return path


@pytest.fixture(autouse=True)
def _fixture_log_reset() -> Any:
    """Keep the once-per-process fixture-root log deterministic per test."""
    oc._reset_fixture_root_logged()
    yield
    oc._reset_fixture_root_logged()


# ── path helpers ────────────────────────────────────────────────────────


class TestPathHelpers:
    def test_runs_root_is_the_ruling4_sweep_runs_sibling(self, tmp_path: Path) -> None:
        """DEC-ONECLICK-SWEEP-RUNS-REBIND-001: <results_root parent>/sweep_runs."""
        assert oc.runs_root(tmp_path / "sweeps") == tmp_path / "sweep_runs"

    def test_runs_root_mirror_pins_the_engine_sweep_run_dir_seam(self, tmp_path: Path) -> None:
        """The discovery seam is byte-equal to trade_store.sweep_run_dir (Ruling 4)."""
        from brutex.engine.trade_store import sweep_run_dir

        results_root = tmp_path / "lake" / "sweeps"
        assert oc.runs_root(results_root) / "r-x" == sweep_run_dir(results_root, "r-x")

    def test_fixture_results_root_keeps_runs_inside_the_fixture_tree(self, tmp_path: Path) -> None:
        pseudo = oc.fixture_results_root(tmp_path)
        assert pseudo == tmp_path / oc.FIXTURE_SWEEPS_DIRNAME
        assert oc.runs_root(pseudo) == tmp_path / oc.SWEEP_RUNS_DIRNAME  # never escapes

    def test_trades_path_zero_pads_rank(self, tmp_path: Path) -> None:
        path = oc.trades_path(tmp_path / "sweeps", "r-x", 7)
        assert path == tmp_path / "sweep_runs" / "r-x" / "trades" / "rank-0007.json"

    @pytest.mark.parametrize("run_id", ["r-20260707-fixt01", "RUN_1", "a", "r.1-b_c", "A" * 128])
    def test_safe_run_ids_accepted(self, run_id: str) -> None:
        assert oc.is_safe_run_id(run_id) is True

    @pytest.mark.parametrize(
        "run_id",
        ["", ".", "..", "../evil", "a/b", "a\\b", ".hidden", "-lead", "x" * 129, "a b"],
    )
    def test_unsafe_run_ids_rejected(self, run_id: str) -> None:
        assert oc.is_safe_run_id(run_id) is False


class TestArtifactRevision:
    def test_token_is_mtime_ns_colon_size(self, tmp_path: Path) -> None:
        target = tmp_path / "a.json"
        target.write_text("xy", encoding="utf-8")
        st = os.stat(target)
        assert oc.artifact_revision(target) == f"{st.st_mtime_ns}:{st.st_size}"

    def test_missing_path_returns_none(self, tmp_path: Path) -> None:
        assert oc.artifact_revision(tmp_path / "absent.json") is None

    def test_stat_error_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.json"
        target.write_text("x", encoding="utf-8")
        real_stat = os.stat

        def _fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(target):
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(oc.os, "stat", _fake_stat)
        assert oc.artifact_revision(target) is None

    def test_runs_revision_stats_the_sweep_runs_dir(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        assert oc.runs_revision(results_root) is None
        (tmp_path / "sweep_runs").mkdir()
        assert oc.runs_revision(results_root) is not None

    def test_runs_revision_moves_on_an_in_place_manifest_rewrite(self, tmp_path: Path) -> None:
        """Fold [12] (DEC-ONECLICK-TOP100-OBJECT-ROOT-001): rewriting an
        EXISTING manifest in place (suspect flip) moves the token even though
        the sweep_runs directory mtime is byte-identical — the cached runs
        listing's suspect badge can no longer go stale."""
        results_root = tmp_path / "sweeps"
        runs = tmp_path / "sweep_runs"
        manifest = runs / "r-x" / oc.MANIFEST_FILENAME
        _write(manifest, json.dumps(_valid_manifest("r-x", suspect=False)))
        # Pin the directory mtimes so ONLY the manifest file token can move.
        os.utime(runs, ns=(1_000_000_000, 1_000_000_000))
        os.utime(runs / "r-x", ns=(1_000_000_000, 1_000_000_000))
        before = oc.runs_revision(results_root)
        manifest.write_text(json.dumps(_valid_manifest("r-x", suspect=True)), encoding="utf-8")
        os.utime(runs, ns=(1_000_000_000, 1_000_000_000))
        os.utime(runs / "r-x", ns=(1_000_000_000, 1_000_000_000))
        after = oc.runs_revision(results_root)
        assert before is not None and after is not None
        assert before.split(":m")[0] == after.split(":m")[0]  # dir token unchanged
        assert before != after  # the manifest fold moved the key

    def test_runs_revision_unlistable_tree_degrades_to_the_dir_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sweep_runs").mkdir()

        def _boom(_path: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr(oc.os, "scandir", _boom)
        token = oc.runs_revision(tmp_path / "sweeps")
        assert token is not None
        assert ":m" not in token  # the bare directory stat token

    def test_runs_revision_skips_files_and_unstattable_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = tmp_path / "sweep_runs"
        (runs / "r-x").mkdir(parents=True)
        (runs / "stray.txt").write_text("not a run dir", encoding="utf-8")
        real_scandir = os.scandir

        class _EvilEntry:
            name = "evil"

            @staticmethod
            def is_dir() -> bool:
                raise OSError("gone mid-scan")

        def _with_evil(path: Any) -> Any:
            entries: list[Any] = list(real_scandir(path))
            entries.append(_EvilEntry())
            return entries

        monkeypatch.setattr(oc.os, "scandir", _with_evil)
        token = oc.runs_revision(tmp_path / "sweeps")
        assert token is not None
        assert ":m" in token  # folded, with the file + evil entries skipped


# ── discovery ───────────────────────────────────────────────────────────


class TestProbeRunDir:
    def test_missing_dir_is_none(self, tmp_path: Path) -> None:
        assert oc.probe_run_dir(tmp_path / "runs" / "r-x") is None

    def test_file_not_dir_is_none(self, tmp_path: Path) -> None:
        target = tmp_path / "r-x"
        target.write_text("not a dir", encoding="utf-8")
        assert oc.probe_run_dir(target) is None

    def test_empty_dir_probes_all_false_suspect_none(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r-x"
        run_dir.mkdir(parents=True)
        info = oc.probe_run_dir(run_dir)
        assert info is not None
        assert info.run_id == "r-x"
        assert (info.has_manifest, info.has_top100, info.has_events) == (False, False, False)
        assert info.suspect is None  # never a fabricated False

    @pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), ("yes", None)])
    def test_suspect_only_from_boolean(
        self, tmp_path: Path, raw: Any, expected: bool | None
    ) -> None:
        run_dir = tmp_path / "runs" / "r-x"
        _write(run_dir / oc.MANIFEST_FILENAME, json.dumps(_valid_manifest(suspect=raw)))
        info = oc.probe_run_dir(run_dir)
        assert info is not None
        assert info.suspect is expected

    def test_corrupt_manifest_leaves_suspect_none(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r-x"
        _write(run_dir / oc.MANIFEST_FILENAME, "{not json")
        _write(run_dir / oc.TOP100_FILENAME, "[]")
        _write(run_dir / oc.EVENTS_FILENAME, "")
        info = oc.probe_run_dir(run_dir)
        assert info is not None
        assert info.has_manifest and info.has_top100 and info.has_events
        assert info.suspect is None


class TestDiscoverRuns:
    def test_absent_runs_tree_is_empty(self, tmp_path: Path) -> None:
        assert oc.discover_runs(tmp_path / "sweeps") == ()

    def test_newest_first_and_limit(self, tmp_path: Path) -> None:
        runs = tmp_path / "sweep_runs"
        for name, age in (("r-old", 100), ("r-mid", 50), ("r-new", 0)):
            (runs / name).mkdir(parents=True)
            stamp = 1_700_000_000 - age
            os.utime(runs / name, (stamp, stamp))
        found = oc.discover_runs(tmp_path / "sweeps")
        assert [info.run_id for info in found] == ["r-new", "r-mid", "r-old"]
        assert [info.run_id for info in oc.discover_runs(tmp_path / "sweeps", limit=2)] == [
            "r-new",
            "r-mid",
        ]
        assert oc.discover_runs(tmp_path / "sweeps", limit=0) == ()

    def test_stray_file_under_runs_is_ignored(self, tmp_path: Path) -> None:
        runs = tmp_path / "sweep_runs"
        (runs / "r-good").mkdir(parents=True)
        (runs / "stray.txt").write_text("x", encoding="utf-8")
        assert [info.run_id for info in oc.discover_runs(tmp_path / "sweeps")] == ["r-good"]

    def test_unreadable_entries_skipped_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-MINOR-1: unreadability via monkeypatched scandir entries, never chmod."""
        runs = tmp_path / "sweep_runs"
        (runs / "r-good").mkdir(parents=True)
        real_scandir = os.scandir

        class _BadStat:
            name = "r-badstat"

            def is_dir(self) -> bool:
                return True

            def stat(self) -> Any:
                raise PermissionError("denied")

        class _BadIsDir:
            name = "r-badisdir"

            def is_dir(self) -> bool:
                raise OSError("io error")

        def _fake_scandir(path: Any) -> Any:
            return iter([_BadStat(), _BadIsDir(), *list(real_scandir(path))])

        monkeypatch.setattr(oc.os, "scandir", _fake_scandir)
        with structlog.testing.capture_logs() as logs:
            found = oc.discover_runs(tmp_path / "sweeps")
        assert [info.run_id for info in found] == ["r-good"]
        skipped = [entry for entry in logs if entry["event"] == "oneclick.run_skipped"]
        assert {entry["run"] for entry in skipped} == {"r-badstat", "r-badisdir"}


class TestNewestRunName:
    """Fix round 2 (DEC-RESULTS-CASCADE-VERDICT-JOIN-001, refuter-confirmed
    MINOR): the name-only newest-run listing — the same ``(mtime_ns, name)``
    selection core ``discover_runs`` rides, with ZERO manifest reads/parses.
    """

    def test_absent_runs_tree_is_none(self, tmp_path: Path) -> None:
        assert oc.newest_run_name(tmp_path / "sweeps") is None

    def test_newest_name_matches_discover_runs_selection(self, tmp_path: Path) -> None:
        runs = tmp_path / "sweep_runs"
        for name, age in (("r-old", 100), ("r-new", 0)):
            (runs / name).mkdir(parents=True)
            stamp = 1_700_000_000 - age
            os.utime(runs / name, (stamp, stamp))
        assert oc.newest_run_name(tmp_path / "sweeps") == "r-new"
        # ONE selection law: name-only listing == the RunInfo listing's head.
        assert oc.newest_run_name(tmp_path / "sweeps") == (
            oc.discover_runs(tmp_path / "sweeps", limit=1)[0].run_id
        )

    def test_zero_manifest_reads_even_when_manifest_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "sweep_runs" / "r-with-manifest"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text('{"suspect": false}', encoding="utf-8")
        calls: list[str] = []
        real_load = oc.load_manifest

        def _spy(target: Path) -> Any:
            calls.append(str(target))
            return real_load(target)

        monkeypatch.setattr(oc, "load_manifest", _spy)
        assert oc.newest_run_name(tmp_path / "sweeps") == "r-with-manifest"
        assert calls == []


# ── manifest loader ─────────────────────────────────────────────────────


class TestLoadManifest:
    def test_missing_is_awaiting(self, tmp_path: Path) -> None:
        result = oc.load_manifest(tmp_path)
        assert result.status == "awaiting"
        assert result.manifest is None
        assert oc.AWAITING_DETAIL in result.detail
        assert result.revision is None

    def test_unreadable_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.MANIFEST_FILENAME).mkdir()  # a directory → IsADirectoryError
        result = oc.load_manifest(tmp_path)
        assert result.status == "corrupt"
        assert "IsADirectoryError" in result.detail

    def test_invalid_utf8_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.MANIFEST_FILENAME).write_bytes(b"\xff\xfe\x00garbage")
        result = oc.load_manifest(tmp_path)
        assert result.status == "corrupt"
        assert "UnicodeDecodeError" in result.detail

    def test_bad_json_is_corrupt_with_revision(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.MANIFEST_FILENAME, "{truncated")
        with structlog.testing.capture_logs() as logs:
            result = oc.load_manifest(tmp_path)
        assert result.status == "corrupt"
        assert result.revision is not None
        assert any(entry["event"] == "oneclick.artifact_corrupt" for entry in logs)

    def test_non_object_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.MANIFEST_FILENAME, "[1, 2]")
        result = oc.load_manifest(tmp_path)
        assert result.status == "corrupt"
        assert "not a JSON object" in result.detail

    def test_missing_contract_keys_is_partial_naming_them(self, tmp_path: Path) -> None:
        manifest = _valid_manifest()
        del manifest["per_door_kills"], manifest["suspect"]
        _write(tmp_path / oc.MANIFEST_FILENAME, json.dumps(manifest))
        result = oc.load_manifest(tmp_path)
        assert result.status == "partial"
        assert result.manifest is not None  # kept as-read, no invented defaults
        assert "per_door_kills" in result.detail
        assert "suspect" in result.detail

    def test_full_contract_shape_is_ok(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.MANIFEST_FILENAME, json.dumps(_valid_manifest()))
        result = oc.load_manifest(tmp_path)
        assert result.status == "ok"
        assert result.detail == ""
        assert result.manifest is not None
        assert result.manifest["run_id"] == "r-unit01"


# ── top100 loader ───────────────────────────────────────────────────────


def _top100_row(rank: int = 1, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "rank": rank,
        "mask_hex": "0x05",
        "conditions": ["PDH_CROSS_UP"],
        "tf": "5m",
        "direction": "long",
        "arm": "SL1.0_TP2.0",
        "metrics": {"sharpe": 1.2345678901},
        "regime": {},
        "vix_band": None,
        "family_id": "fam-0001",
    }
    row.update(overrides)
    return row


def _top100_doc(rows: list[Any], **root_overrides: Any) -> dict[str, Any]:
    """The WRITER's object root (DEC-ONECLICK-TOP100-OBJECT-ROOT-001 shape law)."""
    doc: dict[str, Any] = {
        "schema_version": oc.TOP100_SCHEMA_VERSION,
        "run_id": "r-unit01",
        "preset": "min_dd_first",
        "source": {"pool": len(rows)},
        "baselines": {},
        "entries": rows,
    }
    doc.update(root_overrides)
    return doc


def _write_top100(run_dir: Path, rows: list[Any], **root_overrides: Any) -> None:
    _write(run_dir / oc.TOP100_FILENAME, json.dumps(_top100_doc(rows, **root_overrides)))


class TestLoadTop100:
    def test_missing_is_awaiting(self, tmp_path: Path) -> None:
        result = oc.load_top100(tmp_path)
        assert result.status == "awaiting"
        assert result.rows == ()
        assert result.revision is None
        assert result.preset is None

    def test_unreadable_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.TOP100_FILENAME).mkdir()
        assert oc.load_top100(tmp_path).status == "corrupt"

    def test_bad_json_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, '{"entries": [{"rank": 1')
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert "not valid JSON" in result.detail

    def test_bare_array_is_corrupt_naming_the_retired_form(self, tmp_path: Path) -> None:
        """DEC-ONECLICK-TOP100-OBJECT-ROOT-001: the pre-#1621 fixture-only bare
        array is RETIRED — a NAMED corrupt, never a silent second dialect."""
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps([_top100_row(1)]))
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert result.rows == ()
        assert "bare JSON array" in result.detail
        assert "pre-#1621" in result.detail

    def test_non_object_scalar_root_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, '"junk"')
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert "not the writer's JSON object root" in result.detail

    def test_oversized_entries_capped_at_the_contract_100(self, tmp_path: Path) -> None:
        """Hostile-round F2a (defense-in-depth): the writer's ≤100 contract
        is enforced reader-side too — a 150-row out-of-band artifact parses
        exactly TOP100_ROW_CAP rows and reads a NAMED partial counting the
        dropped overflow, never an O(entries) parse or a silent overflow."""
        _write_top100(tmp_path, [_top100_row(i + 1) for i in range(150)])
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == oc.TOP100_ROW_CAP == 100
        assert [row["rank"] for row in result.rows] == list(range(1, 101))
        assert "150 entries" in result.detail
        assert "first 100 kept, 50 dropped" in result.detail
        assert result.preset == "min_dd_first"

    def test_unknown_future_schema_version_is_named_corrupt(self, tmp_path: Path) -> None:
        """An unknown FUTURE schema_version degrades LOUDLY — never a crash."""
        _write_top100(tmp_path, [_top100_row(1)], schema_version=oc.TOP100_SCHEMA_VERSION + 1)
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert f"schema_version {oc.TOP100_SCHEMA_VERSION + 1!r}" in result.detail
        assert "upgrade the display reader" in result.detail

    def test_missing_schema_version_key_is_tolerated(self, tmp_path: Path) -> None:
        """schema_version is validated only when PRESENT (lenient absent-key law)."""
        doc = _top100_doc([_top100_row(1)])
        del doc["schema_version"]
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps(doc))
        assert oc.load_top100(tmp_path).status == "ok"

    @pytest.mark.parametrize("pun", [True, 1.0, "1"])
    def test_schema_version_type_puns_are_named_corrupt(self, tmp_path: Path, pun: Any) -> None:
        """Hostile-round finder (b): a JSON ``true`` (``True == 1``), float
        ``1.0`` or string ``"1"`` must NOT pass the version equality — the
        version is a GENUINE int or the artifact names itself corrupt."""
        _write_top100(tmp_path, [_top100_row(1)], schema_version=pun)
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert result.rows == ()
        assert f"schema_version {pun!r}" in result.detail

    def test_object_root_without_entries_array_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps({"schema_version": 1, "rows": []}))
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert "no entries array" in result.detail

    def test_valid_empty_entries_is_ok(self, tmp_path: Path) -> None:
        """T-M2 status law: a VALID EMPTY artifact EXISTS — status ok, 0 rows."""
        _write_top100(tmp_path, [])
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert result.rows == ()
        assert result.preset == "min_dd_first"

    def test_short_entries_is_ok_with_true_row_count(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(rank) for rank in range(1, 18)])
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert len(result.rows) == 17  # never assumed 100

    def test_non_string_or_blank_preset_reads_none(self, tmp_path: Path) -> None:
        for bad_preset in (7, ""):
            _write_top100(tmp_path, [_top100_row(1)], preset=bad_preset)
            result = oc.load_top100(tmp_path)
            assert result.status == "ok"
            assert result.preset is None

    def test_non_dict_row_is_partial_keeping_prefix(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1), "junk"])
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == 1
        assert "row 1" in result.detail
        assert result.preset == "min_dd_first"  # the root parsed — preset kept

    def test_row_missing_keys_is_partial_naming_them(self, tmp_path: Path) -> None:
        bad = _top100_row(2)
        del bad["metrics"], bad["vix_band"]
        _write_top100(tmp_path, [_top100_row(1), bad])
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == 1
        assert "metrics" in result.detail
        assert "vix_band" in result.detail

    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"rank": True}, "non-integer rank"),
            ({"rank": "1"}, "non-integer rank"),
            ({"conditions": "PDH_CROSS_UP"}, "conditions is not an array"),
            ({"metrics": [1, 2]}, "metrics is not an object"),
            ({"regime": "trending"}, "regime is not an object"),
            ({"vix_band": 12.5}, "vix_band is neither an object nor null"),
        ],
    )
    def test_inner_shape_defects_are_partial_named(
        self, tmp_path: Path, overrides: dict[str, Any], named: str
    ) -> None:
        """Sub-key validation: a required key with an unusable SHAPE degrades
        to partial with the defect NAMED, rows kept up to it."""
        bad = _top100_row(2)
        bad.update(overrides)
        _write_top100(tmp_path, [_top100_row(1), bad])
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == 1
        assert "row 1" in result.detail
        assert named in result.detail

    def test_vix_band_object_passes_the_shape_gate(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1, vix_band={"lo": 11.0, "hi": 17.5})])
        assert oc.load_top100(tmp_path).status == "ok"

    def test_full_precision_metrics_pass_through_untouched(self, tmp_path: Path) -> None:
        row = _top100_row(1)
        row["metrics"]["sharpe"] = 1.0000000000000002
        _write_top100(tmp_path, [row])
        result = oc.load_top100(tmp_path)
        assert result.rows[0]["metrics"]["sharpe"] == 1.0000000000000002


class TestTop100MetricKeyMap:
    """The ONE reader-boundary metric mapping (DEC-ONECLICK-TOP100-OBJECT-ROOT-001)."""

    def test_engine_names_gain_the_display_aliases_additively(self, tmp_path: Path) -> None:
        """Every writer metric name maps to its display twin — engine keys kept."""
        engine_metrics = {
            "max_drawdown_paisa": -158_117,
            "sharpe_ratio": 3.2000000000000002,
            "deflated_sharpe": 0.93,
            "total_trades": 187,
            "net_pnl_paisa": 8_962_469,  # unmapped — passes through verbatim
        }
        _write_top100(tmp_path, [_top100_row(1, metrics=dict(engine_metrics))])
        metrics = oc.load_top100(tmp_path).rows[0]["metrics"]
        assert metrics["max_dd_paisa"] == -158_117
        assert metrics["sharpe"] == 3.2000000000000002  # full precision
        assert metrics["dsr"] == 0.93
        assert metrics["trades"] == 187
        for engine_key, value in engine_metrics.items():
            assert metrics[engine_key] == value  # additive — nothing removed

    def test_absent_cpcv_stays_absent_never_a_fabricated_zero(self, tmp_path: Path) -> None:
        """The writer emits no cpcv_pass_pct — the reader must NOT invent one."""
        _write_top100(tmp_path, [_top100_row(1, metrics={"sharpe_ratio": 1.0})])
        metrics = oc.load_top100(tmp_path).rows[0]["metrics"]
        assert "cpcv_pass_pct" not in metrics

    def test_existing_display_key_is_never_overwritten(self, tmp_path: Path) -> None:
        """An artifact already carrying the display name wins over the alias."""
        _write_top100(
            tmp_path,
            [_top100_row(1, metrics={"sharpe_ratio": 9.9, "sharpe": 1.1})],
        )
        metrics = oc.load_top100(tmp_path).rows[0]["metrics"]
        assert metrics["sharpe"] == 1.1
        assert metrics["sharpe_ratio"] == 9.9

    def test_row_without_mapped_keys_is_returned_unchanged(self, tmp_path: Path) -> None:
        """The no-alias fast path: no engine key present ⇒ the SAME row object."""
        _write_top100(tmp_path, [_top100_row(1)])
        metrics = oc.load_top100(tmp_path).rows[0]["metrics"]
        assert metrics == {"sharpe": 1.2345678901}


class TestTop100ArmBridge:
    """The reader-boundary arm dict → display string bridge (hostile-round F1).

    The SHIPPED writer emits ``arm`` as ``{"sl_paisa", "tp_paisa"}`` while
    every display site ``String()``s the field — an unbridged dict rendered
    ``"[object Object]"`` on every real sweep card.
    """

    def test_writer_arm_dict_bridges_to_the_house_money_string(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1, arm={"sl_paisa": 8_000, "tp_paisa": 5_000})])
        row = oc.load_top100(tmp_path).rows[0]
        assert row["arm"] == "SL ₹80.00 · TP ₹50.00"  # format_inr — paisa→₹ 2dp
        assert row["arm_raw"] == {"sl_paisa": 8_000, "tp_paisa": 5_000}  # additive

    def test_grouped_amounts_ride_the_house_formatter(self, tmp_path: Path) -> None:
        """format_inr grouping — the display never re-invents money format."""
        _write_top100(tmp_path, [_top100_row(1, arm={"sl_paisa": 123_456, "tp_paisa": 5})])
        assert oc.load_top100(tmp_path).rows[0]["arm"] == "SL ₹1,234.56 · TP ₹0.05"

    def test_none_legs_read_the_honest_dash_never_a_fabrication(self, tmp_path: Path) -> None:
        """The writer's honest ``None`` legs (a store row without a tuple arm)."""
        _write_top100(tmp_path, [_top100_row(1, arm={"sl_paisa": None, "tp_paisa": None})])
        assert oc.load_top100(tmp_path).rows[0]["arm"] == "SL — · TP —"

    def test_junk_and_missing_legs_read_the_dash_segment(self, tmp_path: Path) -> None:
        """A hand-edited non-int leg (bool/str) or an absent key reads "—"."""
        _write_top100(tmp_path, [_top100_row(1, arm={"sl_paisa": True})])
        assert oc.load_top100(tmp_path).rows[0]["arm"] == "SL — · TP —"

    def test_string_arm_passes_through_untouched(self, tmp_path: Path) -> None:
        """A non-dict arm (legacy display string) is NOT bridged, no arm_raw."""
        _write_top100(tmp_path, [_top100_row(1)])  # arm is the string default
        loaded = oc.load_top100(tmp_path).rows[0]
        assert loaded["arm"] == "SL1.0_TP2.0"
        assert "arm_raw" not in loaded

    def test_preexisting_arm_raw_is_never_overwritten(self, tmp_path: Path) -> None:
        """Additive law: an artifact already carrying arm_raw keeps its value."""
        row = _top100_row(1, arm={"sl_paisa": 100, "tp_paisa": 200}, arm_raw="keep-me")
        _write_top100(tmp_path, [row])
        loaded = oc.load_top100(tmp_path).rows[0]
        assert loaded["arm"] == "SL ₹1.00 · TP ₹2.00"
        assert loaded["arm_raw"] == "keep-me"

    # ── TOMB-01: retired-condition marking at the reader boundary ──

    def test_pre_reshape_conditions_read_retired(self, tmp_path: Path) -> None:
        """An OLD artifact's plain tombstoned name gains the "(retired)" suffix;
        live names + unknown strings pass through verbatim (never a crash)."""
        row = _top100_row(1, conditions=["close_above_vwap", "bar_bullish", "PDH_CROSS_UP"])
        _write_top100(tmp_path, [row])
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert result.rows[0]["conditions"] == [
            "close_above_vwap (retired)",
            "bar_bullish",
            "PDH_CROSS_UP",
        ]

    def test_new_artifact_suffixed_conditions_are_idempotent(self, tmp_path: Path) -> None:
        """A NEW artifact (writer already suffixes) is not double-marked."""
        row = _top100_row(1, conditions=["close_below_vwap (retired)", "narrow_cpr_day"])
        _write_top100(tmp_path, [row])
        assert oc.load_top100(tmp_path).rows[0]["conditions"] == [
            "close_below_vwap (retired)",
            "narrow_cpr_day",
        ]

    def test_non_string_condition_entries_pass_through(self, tmp_path: Path) -> None:
        """A hand-edited row with non-string entries is left untouched — the
        bridge never crashes an old payload (the list shape itself already
        passed the required-keys validator)."""
        row = _top100_row(1, conditions=["close_above_vwap", 7])
        _write_top100(tmp_path, [row])
        assert oc.load_top100(tmp_path).rows[0]["conditions"] == ["close_above_vwap", 7]


# ── preflight fixture loader ────────────────────────────────────────────


class TestLoadPreflightFixture:
    def test_missing_is_awaiting_naming_p1(self, tmp_path: Path) -> None:
        result = oc.load_preflight_fixture(tmp_path)
        assert result.status == "awaiting"
        assert "estimator not wired yet — lands at merge-integration (P1)" in result.detail

    def test_unreadable_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.PREFLIGHT_FILENAME).mkdir()
        assert oc.load_preflight_fixture(tmp_path).status == "corrupt"

    def test_bad_json_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.PREFLIGHT_FILENAME, "not json")
        assert oc.load_preflight_fixture(tmp_path).status == "corrupt"

    def test_non_object_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.PREFLIGHT_FILENAME, "[]")
        assert oc.load_preflight_fixture(tmp_path).status == "corrupt"

    def test_missing_keys_is_partial(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.PREFLIGHT_FILENAME, json.dumps({"ok": True}))
        result = oc.load_preflight_fixture(tmp_path)
        assert result.status == "partial"
        assert "refusals" in result.detail

    def test_contract_shape_is_ok(self, tmp_path: Path) -> None:
        verdict = {
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {"trials": 100, "est_seconds_provisional": 5, "est_bytes": 1024},
        }
        _write(tmp_path / oc.PREFLIGHT_FILENAME, json.dumps(verdict))
        result = oc.load_preflight_fixture(tmp_path)
        assert result.status == "ok"
        assert result.manifest == verdict


# ── events tail ─────────────────────────────────────────────────────────


class TestTailEvents:
    def test_missing_is_awaiting(self, tmp_path: Path) -> None:
        page = oc.tail_events(tmp_path)
        assert page.status == "awaiting"
        assert page.records == ()
        assert page.total_bytes == 0

    def test_unreadable_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.EVENTS_FILENAME).mkdir()
        page = oc.tail_events(tmp_path)
        assert page.status == "corrupt"
        assert "IsADirectoryError" in page.detail

    def test_paging_resumes_at_next_offset(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(10))
        first = oc.tail_events(tmp_path, offset=0, limit=3)
        assert first.status == "ok"
        assert [record["payload"]["i"] for record in first.records] == [0, 1, 2]
        assert 0 < first.next_offset < first.total_bytes
        second = oc.tail_events(tmp_path, offset=first.next_offset, limit=3)
        assert [record["payload"]["i"] for record in second.records] == [3, 4, 5]

    def test_poll_at_eof_is_empty_ok(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(2))
        size = (tmp_path / oc.EVENTS_FILENAME).stat().st_size
        page = oc.tail_events(tmp_path, offset=size)
        assert page.status == "ok"
        assert page.records == ()
        assert page.next_offset == size

    def test_offset_past_eof_resets_to_end(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(2))
        size = (tmp_path / oc.EVENTS_FILENAME).stat().st_size
        page = oc.tail_events(tmp_path, offset=size + 999)
        assert page.records == ()
        assert page.next_offset == size
        assert "past EOF" in page.detail

    def test_negative_offset_tail_anchors_small_file(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(4))
        page = oc.tail_events(tmp_path, offset=-1, limit=10)
        assert [record["payload"]["i"] for record in page.records] == [0, 1, 2, 3]

    def test_negative_offset_big_file_aligns_to_record_boundary(self, tmp_path: Path) -> None:
        """>1 MiB file: the tail anchor lands mid-line and skips to the next record."""
        pad = "x" * 600_000
        bodies = [
            {"ts_utc": "t", "run_id": "r", "kind": f"k{i}", "payload": {"pad": pad, "i": i}}
            for i in range(3)
        ]
        _write_events(tmp_path, bodies)
        page = oc.tail_events(tmp_path, offset=-1, limit=10)
        assert page.status == "ok"
        assert [record["payload"]["i"] for record in page.records] == [2]

    def test_bytes_cap_bounds_the_window(self, tmp_path: Path) -> None:
        """O(page) law: the read loop exits at the 1 MiB cap, not at EOF."""
        pad = "x" * 600_000
        bodies = [
            {"ts_utc": "t", "run_id": "r", "kind": f"k{i}", "payload": {"pad": pad, "i": i}}
            for i in range(3)
        ]
        _write_events(tmp_path, bodies)
        page = oc.tail_events(tmp_path, offset=0, limit=1000)
        assert [record["payload"]["i"] for record in page.records] == [0, 1]
        assert page.next_offset < page.total_bytes  # honest cursor mid-file

    def test_half_written_tail_is_truncated_not_error(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(2), tail='{"ts_utc":"2026-07-07T0')
        page = oc.tail_events(tmp_path, offset=0)
        assert page.status == "ok"
        assert page.truncated_tail is True
        assert len(page.records) == 2
        # next_offset stays at the partial line's start — re-read once complete.
        assert page.next_offset == page.total_bytes - len('{"ts_utc":"2026-07-07T0')

    def test_single_line_larger_than_window_is_bounded_not_oom(self, tmp_path: Path) -> None:
        """Hostile-round F3: a no-newline line larger than the 1 MiB window
        is NEVER materialised whole (the prior unbounded readline() peaked
        at the full line size BEFORE any cap check) — the page degrades
        honestly: records before the giant line served, truncated flagged,
        the over-window line NAMED, cursor left at its start."""
        lines = _chained_lines(_event_bodies(1))
        giant = '{"pad":"' + "x" * (oc._EVENTS_BYTES_CAP + 4096) + '"'  # no newline
        _write(tmp_path / oc.EVENTS_FILENAME, lines[0] + "\n" + giant)
        page = oc.tail_events(tmp_path, offset=0, limit=10)
        assert page.status == "ok"
        assert len(page.records) == 1  # the good record before the giant line
        assert page.truncated_tail is True
        assert f"exceeds the {oc._EVENTS_BYTES_CAP}-byte read window" in page.detail
        assert page.next_offset == len(lines[0]) + 1  # at the giant line's start

    def test_tail_anchor_window_without_record_boundary_is_named_corrupt(
        self, tmp_path: Path
    ) -> None:
        """Hostile-round F3 (tail-anchor arm): a tail window living entirely
        inside one giant line holds NO record boundary — a NAMED corrupt
        (the bounded skip read), never an unbounded in-memory line."""
        giant = '{"pad":"' + "x" * (oc._EVENTS_BYTES_CAP + 4096) + '"'  # no newline anywhere
        _write(tmp_path / oc.EVENTS_FILENAME, giant)
        page = oc.tail_events(tmp_path, offset=-1, limit=10)
        assert page.status == "corrupt"
        assert page.records == ()
        assert page.truncated_tail is True
        assert "no record boundary" in page.detail

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(2))
        _write(tmp_path / oc.EVENTS_FILENAME, lines[0] + "\n\n" + lines[1] + "\n")
        page = oc.tail_events(tmp_path)
        assert page.status == "ok"
        assert len(page.records) == 2

    def test_unparseable_complete_line_is_partial_skipped_counted(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(2))
        text = lines[0] + "\n{broken\n42\n" + lines[1] + "\n"
        _write(tmp_path / oc.EVENTS_FILENAME, text)
        page = oc.tail_events(tmp_path)
        assert page.status == "partial"
        assert len(page.records) == 2
        assert "2 unparseable line(s)" in page.detail
        first_bad = len(lines[0]) + 1
        assert f"first at byte {first_bad}" in page.detail

    def test_kind_filter_applies_to_window_only(self, tmp_path: Path) -> None:
        bodies = _event_bodies(4)
        bodies[1]["kind"] = "checkpoint.saved"
        del bodies[2]["kind"]  # a record without kind never matches
        _write_events(tmp_path, bodies)
        page = oc.tail_events(tmp_path, kind="checkpoint.saved")
        assert [record["payload"]["i"] for record in page.records] == [1]

    def test_q_substring_filter(self, tmp_path: Path) -> None:
        bodies = _event_bodies(3)
        bodies[2]["payload"] = {"note": "NEEDLE-42"}
        _write_events(tmp_path, bodies)
        page = oc.tail_events(tmp_path, q="NEEDLE-42")
        assert len(page.records) == 1
        assert page.records[0]["payload"]["note"] == "NEEDLE-42"

    def test_limit_is_clamped_to_bounds(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(3))
        assert len(oc.tail_events(tmp_path, limit=0).records) == 1  # clamped up to 1
        assert len(oc.tail_events(tmp_path, limit=99_999).records) == 3  # cap tolerated


# ── hash chain ──────────────────────────────────────────────────────────


class TestChainSeam:
    def test_canonical_bytes_exclude_hash_and_sort_keys(self) -> None:
        record = {"kind": "a", "hash": "deadbeef", "ts_utc": "t"}
        assert oc.canonical_record_bytes(record) == b'{"kind":"a","ts_utc":"t"}'

    def test_chain_hash_is_deterministic_sha256_hex(self) -> None:
        digest = oc.chain_hash(oc._CHAIN_GENESIS, b"{}")
        assert digest == oc.chain_hash(oc._CHAIN_GENESIS, b"{}")
        assert len(digest) == 64
        assert digest != oc.chain_hash("1" * 64, b"{}")


class TestVerifyEventsChain:
    def test_absent_file_reports_nothing_to_verify(self, tmp_path: Path) -> None:
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (False, 0, None)
        assert "absent" in detail

    def test_unreadable_file_reports_error(self, tmp_path: Path) -> None:
        (tmp_path / oc.EVENTS_FILENAME).mkdir()
        ok, _, _, detail = oc.verify_events_chain(tmp_path)
        assert ok is False
        assert "IsADirectoryError" in detail

    def test_empty_file_verifies_zero_records(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.EVENTS_FILENAME, "")
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (True, 0, None)
        assert "0 records" in detail

    def test_valid_chain_verifies_end_to_end(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(5))
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (True, 5, None)
        assert "chain verified: 5 records" in detail

    def test_blank_lines_and_truncated_tail_tolerated(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(2))
        _write(
            tmp_path / oc.EVENTS_FILENAME,
            lines[0] + "\n\n" + lines[1] + '\n{"half":',
        )
        ok, checked, _, _ = oc.verify_events_chain(tmp_path)
        assert (ok, checked) == (True, 2)

    def test_unparseable_line_breaks_with_line_number(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(2))
        _write(tmp_path / oc.EVENTS_FILENAME, lines[0] + "\n{broken\n" + lines[1] + "\n")
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (False, 1, 2)
        assert "unparseable record at line 2" in detail

    def test_non_object_record_breaks(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(1))
        _write(tmp_path / oc.EVENTS_FILENAME, "[1,2]\n" + lines[0] + "\n")
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (False, 0, 1)
        assert "carries no hash" in detail

    def test_missing_hash_breaks(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.EVENTS_FILENAME, '{"kind":"a"}\n')
        ok, _, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, break_line) == (False, 1)
        assert "carries no hash" in detail

    def test_tampered_record_breaks_at_its_line(self, tmp_path: Path) -> None:
        lines = _chained_lines(_event_bodies(3))
        tampered = json.loads(lines[1])
        tampered["kind"] = "tampered"
        lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
        _write(tmp_path / oc.EVENTS_FILENAME, "\n".join(lines) + "\n")
        ok, checked, break_line, detail = oc.verify_events_chain(tmp_path)
        assert (ok, checked, break_line) == (False, 1, 2)
        assert "chain break at line 2" in detail

    def test_max_records_bounds_the_scan(self, tmp_path: Path) -> None:
        _write_events(tmp_path, _event_bodies(5))
        ok, checked, _, detail = oc.verify_events_chain(tmp_path, max_records=2)
        assert (ok, checked) == (True, 2)
        assert "bounded at 2" in detail

    def test_every_verdict_carries_the_assumed_algorithm_note(self, tmp_path: Path) -> None:
        """T-M1: the caveat is PERMANENT — cached, fresh, pass and fail alike."""
        _write_events(tmp_path, _event_bodies(1))
        for run_dir in (tmp_path, tmp_path / "absent"):
            detail = oc.verify_events_chain(run_dir)[3]
            assert "(algorithm assumed until P2 merges)" in detail


class TestChainPeekSentinel:
    def test_raise_chain_not_verified_raises(self) -> None:
        with pytest.raises(oc.ChainNotVerifiedError):
            oc.raise_chain_not_verified()


# ── revision probes + the outage-serve doctrine (DEC-ONECLICK-PERF-CLUB-001) ──


class TestArtifactProbe:
    def test_statted_file_carries_the_revision_token(self, tmp_path: Path) -> None:
        target = tmp_path / "a.json"
        target.write_text("xy", encoding="utf-8")
        st = os.stat(target)
        probe = oc.artifact_probe(target)
        assert probe == oc.ArtifactProbe(f"{st.st_mtime_ns}:{st.st_size}", False)

    def test_absent_file_is_absent_not_outage(self, tmp_path: Path) -> None:
        assert oc.artifact_probe(tmp_path / "missing.json") == oc.ArtifactProbe(None, False)

    def test_file_component_in_path_is_absent_not_outage(self, tmp_path: Path) -> None:
        """A FILE where a run dir should be (NotADirectoryError) reads absent."""
        (tmp_path / "r-x").write_text("not a dir", encoding="utf-8")
        probe = oc.artifact_probe(tmp_path / "r-x" / "manifest.json")
        assert probe == oc.ArtifactProbe(None, False)

    def test_other_oserror_is_an_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "a.json"
        target.write_text("x", encoding="utf-8")
        real_stat = os.stat

        def _fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(target):
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(oc.os, "stat", _fake_stat)
        assert oc.artifact_probe(target) == oc.ArtifactProbe(None, True)


class TestRunsRevisionProbe:
    def test_absent_tree_is_absent_not_outage(self, tmp_path: Path) -> None:
        assert oc.runs_revision_probe(tmp_path / "sweeps") == oc.ArtifactProbe(None, False)

    def test_healthy_token_equals_runs_revision(self, tmp_path: Path) -> None:
        """The probe's token IS runs_revision's token (one digest law — §11 COMMON)."""
        runs = tmp_path / "sweep_runs"
        (runs / "r-a").mkdir(parents=True)
        (runs / "r-a" / oc.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
        probe = oc.runs_revision_probe(tmp_path / "sweeps")
        assert probe.outage is False
        assert probe.token == oc.runs_revision(tmp_path / "sweeps")
        assert probe.token is not None

    def test_root_stat_outage_reads_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = tmp_path / "sweep_runs"
        runs.mkdir()
        real_stat = os.stat

        def _fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(runs):
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(oc.os, "stat", _fake_stat)
        assert oc.runs_revision_probe(tmp_path / "sweeps") == oc.ArtifactProbe(None, True)

    def test_scandir_failure_degrades_to_the_bare_dir_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unlistable-but-stattable tree keeps the bare dir token (both paths)."""
        runs = tmp_path / "sweep_runs"
        runs.mkdir()
        st = os.stat(runs)

        def _fake_scandir(path: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr(oc.os, "scandir", _fake_scandir)
        expected = f"{st.st_mtime_ns}:{st.st_size}"
        assert oc.runs_revision(tmp_path / "sweeps") == expected
        assert oc.runs_revision_probe(tmp_path / "sweeps") == oc.ArtifactProbe(expected, False)


class TestCachedPayloadSentinel:
    def test_raise_no_cached_payload_raises(self) -> None:
        with pytest.raises(oc.CachedPayloadMissingError):
            oc.raise_no_cached_payload()


class TestOutageGate:
    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            oc.OutageGate(capacity=0)

    def test_outage_warns_once_then_stays_quiet(self) -> None:
        gate = oc.OutageGate()
        gate.healthy("coord", "key-1")
        with structlog.testing.capture_logs() as logs:
            assert gate.outage_key("coord") == "key-1"
            assert gate.outage_key("coord") == "key-1"  # second poll: no re-log
        outages = [entry for entry in logs if entry["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1
        assert outages[0]["coordinate"] == "coord"
        assert outages[0]["last_known"] is True

    def test_outage_with_no_last_known_still_warns(self) -> None:
        gate = oc.OutageGate()
        with structlog.testing.capture_logs() as logs:
            assert gate.outage_key("never-served") is None
        outages = [entry for entry in logs if entry["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1
        assert outages[0]["last_known"] is False

    def test_recovery_logs_info_and_rearms_the_warn(self) -> None:
        gate = oc.OutageGate()
        gate.healthy("coord", "key-1")
        with structlog.testing.capture_logs() as logs:
            gate.outage_key("coord")
            gate.healthy("coord", "key-2")  # probe answered again
            gate.outage_key("coord")  # a SECOND outage re-warns
        events = [entry["event"] for entry in logs]
        assert events.count("oneclick.probe_outage") == 2
        assert events.count("oneclick.probe_recovered") == 1
        recovered = next(e for e in logs if e["event"] == "oneclick.probe_recovered")
        assert recovered["resolution"] == "healthy"  # r1 NIT: the resolution is named
        # the recovery updated the last-known key
        assert gate.outage_key("coord") == "key-2"

    def test_healthy_without_prior_outage_logs_nothing(self) -> None:
        gate = oc.OutageGate()
        with structlog.testing.capture_logs() as logs:
            gate.healthy("coord", "key-1")
        assert logs == []

    def test_absent_forgets_the_last_known_key(self) -> None:
        """A deleted artifact is never resurrected by a later outage."""
        gate = oc.OutageGate()
        gate.healthy("coord", "key-1")
        gate.absent("coord")
        with structlog.testing.capture_logs() as logs:
            assert gate.outage_key("coord") is None
        assert [entry["event"] for entry in logs] == ["oneclick.probe_outage"]

    def test_absent_ends_an_outage_with_the_recovery_log(self) -> None:
        """An absence verdict is a definitive probe answer — the outage ends."""
        gate = oc.OutageGate()
        gate.healthy("coord", "key-1")
        gate.outage_key("coord")
        with structlog.testing.capture_logs() as logs:
            gate.absent("coord")
        assert [entry["event"] for entry in logs] == ["oneclick.probe_recovered"]
        # r1 NIT: the outage RESOLVED TO ABSENCE — named, never implied healthy.
        assert logs[0]["resolution"] == "absent"

    def test_absent_without_prior_state_logs_nothing(self) -> None:
        gate = oc.OutageGate()
        with structlog.testing.capture_logs() as logs:
            gate.absent("never-seen")
        assert logs == []

    def test_last_known_map_is_lru_bounded(self) -> None:
        """The oldest coordinate is evicted past capacity (hostile-poll bound)."""
        gate = oc.OutageGate(capacity=2)
        gate.healthy("a", "key-a")
        gate.healthy("b", "key-b")
        gate.healthy("c", "key-c")  # evicts "a"
        assert gate.outage_key("a") is None
        assert gate.outage_key("b") == "key-b"
        assert gate.outage_key("c") == "key-c"

    def test_in_outage_map_is_lru_bounded_so_an_evicted_outage_relogs(self) -> None:
        gate = oc.OutageGate(capacity=1)
        with structlog.testing.capture_logs() as logs:
            gate.outage_key("a")
            gate.outage_key("b")  # evicts "a" from the outage set
            gate.outage_key("a")  # re-logs (bounded, disclosed)
        outages = [entry for entry in logs if entry["event"] == "oneclick.probe_outage"]
        assert len(outages) == 3

    def test_outage_serve_refreshes_lru_recency_so_a_long_outage_survives_churn(self) -> None:
        """r1 MINOR-3: serving last-known refreshes the coordinate's LRU slot.

        Fill the last-known map PAST capacity with healthy churn WHILE one
        coordinate is continuously served from last-known — its key must
        survive (pre-fix, ``outage_key`` never moved it to the LRU end, so
        the churn evicted the in-outage coordinate and every later outage
        poll degraded to the uncached fresh recompute).
        """
        gate = oc.OutageGate(capacity=2)
        gate.healthy("out", "k-out")
        assert gate.outage_key("out") == "k-out"  # outage begins (refreshes recency)
        gate.healthy("h1", "k-1")  # map: [out, h1]
        assert gate.outage_key("out") == "k-out"  # serve refreshes → [h1, out]
        gate.healthy("h2", "k-2")  # evicts h1, NOT the in-outage coordinate
        assert gate.outage_key("out") == "k-out"  # survived the churn past capacity

    def test_sustained_outage_refreshes_in_outage_recency_never_rewarns(self) -> None:
        """r2 NIT-2: an actively-polled outage survives ``_in_outage`` churn.

        A wider-than-capacity outage must evict the STALEST outage
        coordinate, never one still actively polling (pre-fix,
        ``_in_outage`` was insertion-ordered — the fresh coordinate "c"
        evicted the still-polling "a", which then re-WARNed mid-outage).
        """
        gate = oc.OutageGate(capacity=2)
        with structlog.testing.capture_logs() as logs:
            gate.outage_key("a")  # warn — in_outage: [a]
            gate.outage_key("b")  # warn — in_outage: [a, b]
            gate.outage_key("a")  # sustained poll refreshes → [b, a]
            gate.outage_key("c")  # warn — evicts the STALE "b", not "a"
            gate.outage_key("a")  # still latched: NO re-warn
        warns_a = [
            e for e in logs if e["event"] == "oneclick.probe_outage" and e["coordinate"] == "a"
        ]
        assert len(warns_a) == 1


class _YieldingContains(OrderedDict[str, None]):
    """Fault-injection map: the membership check YIELDS the GIL mid-window.

    ``in`` on a C-level (Ordered)dict gives CPython NO eval-breaker
    checkpoint between the gate's ``coordinate not in _in_outage`` check and
    its ``_in_outage[coordinate] = None`` set, so a lock-removal mutant is
    untouchable by scheduling pressure alone (empirically 0/200 at
    switchinterval 1e-9 — the r2 MINOR-1 theater proof, and 0/10 for the
    barrier-aligned fresh-coordinate hammer too). A Python-level
    ``__contains__`` with a ``sleep`` inserts a REAL preemption point INSIDE
    the check-then-set window: under the production lock the window stays
    mutually excluded (the yield happens while the lock is held, so racing
    threads block at ``with self._lock``); with the lock removed, racing
    first-polls interleave through the yield, both read "first", and
    double-WARN — the mutant dies.
    """

    def __contains__(self, key: object) -> bool:
        present = super().__contains__(key)
        time.sleep(1e-6)  # release + reacquire the GIL — a real switch point
        return present


class TestOutageGateConcurrency:
    """REAL teeth for the class docstring's "Thread-safe" claim (r2 MINOR-1).

    The r1 hammer (8 threads free-running one SHARED already-latched
    coordinate) was proven THEATER: its only lock-protected race — the
    check-then-set on ``_in_outage`` — happened exactly ONCE per run, GIL-
    serialised, and a lock-removal mutant survived 200/200 hammer runs (the
    reviewer's reproduction). This rewrite (a) makes EVERY round a
    barrier-aligned 8-way FIRST-poll race on a FRESH seeded coordinate and
    (b) injects a genuine preemption point inside the check-then-set window
    via :class:`_YieldingContains` (whitebox on the ``_in_outage`` seam —
    the file's declared private-pin discipline) — the mutant double-WARNs
    (verified in-session: the no-op-lock mutant fails this test every run;
    the locked gate passes deterministically).
    """

    def test_first_poll_races_warn_once_per_coordinate(self) -> None:
        gate = oc.OutageGate()
        gate._in_outage = _YieldingContains()  # the injected preemption point
        n_threads = 8
        rounds = 200  # every round stays under the 512-capacity maps
        coordinates = [f"race-{i}" for i in range(rounds)]
        for i, coordinate in enumerate(coordinates):
            gate.healthy(coordinate, f"k-{i}")
        barrier = threading.Barrier(n_threads)
        served: list[tuple[str | None, str | None]] = []  # (expected, got)
        errors: list[BaseException] = []

        def _capture(args: threading.ExceptHookArgs) -> None:
            # Worker escapes of ANY tier — ``Exception`` AND ``BaseException``
            # (e.g. ``threading.BrokenBarrierError``) — land here and fail the
            # ``assert errors == []`` below: a ``threading.excepthook``
            # override instead of a literal ``except BaseException:`` handler
            # in the worker (CodeQL py/catch-base-exception; the
            # test_dashboard_rate_bridge_cache.py precedent), teeth unchanged.
            exc = args.exc_value if args.exc_value is not None else args.exc_type()
            errors.append(exc)

        def _hammer() -> None:
            for i, coordinate in enumerate(coordinates):
                # ALL threads' first outage poll of this coordinate lands
                # together — a genuine check-then-set race each round.
                barrier.wait(timeout=10)
                served.append((f"k-{i}", gate.outage_key(coordinate)))

        threads = [threading.Thread(target=_hammer) for _ in range(n_threads)]
        original_excepthook = threading.excepthook
        threading.excepthook = _capture
        try:
            with structlog.testing.capture_logs() as logs:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=60)
        finally:
            threading.excepthook = original_excepthook
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        # no lost last_key: EVERY concurrent poll served its seeded key
        assert len(served) == n_threads * rounds
        assert all(got == expected for expected, got in served)
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        # THE teeth: warns == distinct coordinates — an unlocked
        # check-then-set lets two threads both observe "first" and
        # double-WARN one coordinate (r2 MINOR-1 recipe).
        assert len(outages) == rounds, (
            f"expected one WARN per coordinate ({rounds}), got {len(outages)} — "
            "a duplicate means the first-poll check-then-set raced"
        )
        assert {e["coordinate"] for e in outages} == set(coordinates)
        assert all(e["last_known"] is True for e in outages)


class TestSiblingKeyToken:
    """The sibling arm of the outage doctrine (r1 MINOR-1) — same gate, no copy."""

    def test_healthy_returns_and_remembers_the_token(self) -> None:
        gate = oc.OutageGate()
        probe = oc.ArtifactProbe("7:9", False)
        with structlog.testing.capture_logs() as logs:
            token = oc.sibling_key_token(gate, coordinate="c|sib=events", probe=probe)
        assert token == "7:9"
        assert logs == []
        # the token was remembered for a later outage
        assert gate.outage_key("c|sib=events") == "7:9"

    def test_outage_holds_the_last_known_token_with_one_warn(self) -> None:
        gate = oc.OutageGate()
        oc.sibling_key_token(gate, coordinate="c|sib=events", probe=oc.ArtifactProbe("7:9", False))
        outage = oc.ArtifactProbe(None, True)
        with structlog.testing.capture_logs() as logs:
            first = oc.sibling_key_token(gate, coordinate="c|sib=events", probe=outage)
            second = oc.sibling_key_token(gate, coordinate="c|sib=events", probe=outage)
        assert first == second == "7:9"  # the folded key holds steady through the flap
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1  # once per sibling outage, never per poll
        assert outages[0]["coordinate"] == "c|sib=events"

    def test_cold_outage_reads_none_with_a_warn(self) -> None:
        gate = oc.OutageGate()
        with structlog.testing.capture_logs() as logs:
            token = oc.sibling_key_token(
                gate, coordinate="cold|sib=top100", probe=oc.ArtifactProbe(None, True)
            )
        assert token is None
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1
        assert outages[0]["last_known"] is False

    def test_absent_forgets_so_a_deleted_sibling_is_never_resurrected(self) -> None:
        gate = oc.OutageGate()
        oc.sibling_key_token(gate, coordinate="c|sib=events", probe=oc.ArtifactProbe("7:9", False))
        assert (
            oc.sibling_key_token(
                gate, coordinate="c|sib=events", probe=oc.ArtifactProbe(None, False)
            )
            is None
        )
        # a LATER outage cannot resurrect the pre-delete token
        assert (
            oc.sibling_key_token(
                gate, coordinate="c|sib=events", probe=oc.ArtifactProbe(None, True)
            )
            is None
        )

    def test_recovery_after_a_sibling_outage_logs_and_rekeys(self) -> None:
        gate = oc.OutageGate()
        oc.sibling_key_token(gate, coordinate="c|sib=events", probe=oc.ArtifactProbe("7:9", False))
        oc.sibling_key_token(gate, coordinate="c|sib=events", probe=oc.ArtifactProbe(None, True))
        with structlog.testing.capture_logs() as logs:
            token = oc.sibling_key_token(
                gate, coordinate="c|sib=events", probe=oc.ArtifactProbe("8:1", False)
            )
        assert token == "8:1"
        assert [e["event"] for e in logs] == ["oneclick.probe_recovered"]


class _FakeCache:
    """A minimal :class:`oc.PayloadCache` — dict store + compute counter."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.computes = 0

    def get_or_compute(self, key: str, *, generation: int, compute: Any) -> dict[str, Any]:
        if key in self.store:
            return self.store[key]
        value: dict[str, Any] = compute()  # a raising compute stores nothing
        self.computes += 1
        self.store[key] = value
        return value


class TestServeRevisionCached:
    """The ONE revision-gated serve chokepoint (C-r2-5 outage doctrine)."""

    @staticmethod
    def _payload(n: int) -> dict[str, Any]:
        return {"n": n}

    def test_healthy_serves_the_cache_and_remembers_the_key(self) -> None:
        cache = _FakeCache()
        gate = oc.OutageGate()
        probe = oc.ArtifactProbe("1:2", False)
        calls = {"n": 0}

        def _fresh() -> dict[str, Any]:
            calls["n"] += 1
            return self._payload(calls["n"])

        first = oc.serve_revision_cached(
            cache, gate, coordinate="c", probe=probe, key="k|rev=1:2", compute=_fresh
        )
        second = oc.serve_revision_cached(
            cache, gate, coordinate="c", probe=probe, key="k|rev=1:2", compute=_fresh
        )
        assert first == second == {"n": 1}
        assert cache.computes == 1  # the repeat poll is a cache hit
        assert gate.outage_key("c") == "k|rev=1:2"  # the key was remembered

    def test_outage_serves_the_last_known_payload_with_one_warn(self) -> None:
        cache = _FakeCache()
        gate = oc.OutageGate()
        healthy = oc.ArtifactProbe("1:2", False)
        oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=healthy,
            key="k|rev=1:2",
            compute=lambda: self._payload(1),
        )
        outage = oc.ArtifactProbe(None, True)

        def _must_not_compute() -> dict[str, Any]:
            raise AssertionError("an outage with a cached payload must never recompute")

        with structlog.testing.capture_logs() as logs:
            served = oc.serve_revision_cached(
                cache,
                gate,
                coordinate="c",
                probe=outage,
                key=None,
                compute=_must_not_compute,
            )
            again = oc.serve_revision_cached(
                cache,
                gate,
                coordinate="c",
                probe=outage,
                key=None,
                compute=_must_not_compute,
            )
        assert served == again == {"n": 1}
        outages = [entry for entry in logs if entry["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1  # once per outage, never per poll

    def test_outage_with_evicted_cache_entry_computes_fresh_uncached(self) -> None:
        """The last-known key can be LRU-evicted from the CACHE — honest fresh."""
        cache = _FakeCache()
        gate = oc.OutageGate()
        healthy = oc.ArtifactProbe("1:2", False)
        oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=healthy,
            key="k|rev=1:2",
            compute=lambda: self._payload(1),
        )
        cache.store.clear()  # simulate the LRU eviction
        served = oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe(None, True),
            key=None,
            compute=lambda: self._payload(7),
        )
        assert served == {"n": 7}
        assert cache.store == {}  # fresh compute cached NOTHING (no stale key)

    def test_outage_with_no_last_known_computes_fresh(self) -> None:
        cache = _FakeCache()
        gate = oc.OutageGate()
        with structlog.testing.capture_logs() as logs:
            served = oc.serve_revision_cached(
                cache,
                gate,
                coordinate="cold",
                probe=oc.ArtifactProbe(None, True),
                key=None,
                compute=lambda: self._payload(3),
            )
        assert served == {"n": 3}
        assert any(entry["event"] == "oneclick.probe_outage" for entry in logs)
        assert cache.store == {}

    def test_recovery_resumes_normal_keying(self) -> None:
        """Post-outage, a fresh healthy probe re-keys + serves the NEW revision."""
        cache = _FakeCache()
        gate = oc.OutageGate()
        oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe("1:2", False),
            key="k|rev=1:2",
            compute=lambda: self._payload(1),
        )
        oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe(None, True),
            key=None,
            compute=lambda: self._payload(99),
        )
        with structlog.testing.capture_logs() as logs:
            served = oc.serve_revision_cached(
                cache,
                gate,
                coordinate="c",
                probe=oc.ArtifactProbe("3:4", False),
                key="k|rev=3:4",
                compute=lambda: self._payload(2),
            )
        assert served == {"n": 2}  # the new revision computed fresh + cached
        assert any(entry["event"] == "oneclick.probe_recovered" for entry in logs)
        assert gate.outage_key("c") == "k|rev=3:4"

    def test_absent_computes_fresh_uncached_and_forgets(self) -> None:
        cache = _FakeCache()
        gate = oc.OutageGate()
        oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe("1:2", False),
            key="k|rev=1:2",
            compute=lambda: self._payload(1),
        )
        served = oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe(None, False),
            key=None,
            compute=lambda: self._payload(4),
        )
        assert served == {"n": 4}
        # the artifact was deleted — a LATER outage must not resurrect it
        assert gate.outage_key("c") is None

    def test_defensive_none_key_with_a_token_reads_absent(self) -> None:
        """A route bug passing key=None beside a real token degrades, never crashes."""
        cache = _FakeCache()
        gate = oc.OutageGate()
        served = oc.serve_revision_cached(
            cache,
            gate,
            coordinate="c",
            probe=oc.ArtifactProbe("1:2", False),
            key=None,
            compute=lambda: self._payload(5),
        )
        assert served == {"n": 5}
        assert cache.store == {}


class TestDiscoverRunsParseBound:
    """F4 (DEC-ONECLICK-PERF-CLUB-001): manifests parse ONLY for the top-limit runs."""

    def test_churn_case_parses_at_most_limit_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = tmp_path / "sweep_runs"
        for i in range(60):
            run_dir = runs / f"r-{i:04d}"
            run_dir.mkdir(parents=True)
            (run_dir / oc.MANIFEST_FILENAME).write_text(
                json.dumps({"suspect": False}), encoding="utf-8"
            )
            stamp = 1_700_000_000 + i
            os.utime(run_dir, (stamp, stamp))
        real_load = oc.load_manifest
        parses = {"n": 0}

        def _counting_load(run_dir: Path) -> Any:
            parses["n"] += 1
            return real_load(run_dir)

        monkeypatch.setattr(oc, "load_manifest", _counting_load)
        found = oc.discover_runs(tmp_path / "sweeps")
        assert len(found) == 50
        assert parses["n"] == 50  # NOT 60 — only the selected dirs parse
        # newest-first selection: the 10 oldest dirs never parsed
        assert [info.run_id for info in found][:2] == ["r-0059", "r-0058"]
        assert all(info.suspect is False for info in found)

    def test_limit_two_parses_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = tmp_path / "sweep_runs"
        for i in range(5):
            run_dir = runs / f"r-{i}"
            run_dir.mkdir(parents=True)
            (run_dir / oc.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
            stamp = 1_700_000_000 + i
            os.utime(run_dir, (stamp, stamp))
        parses = {"n": 0}
        real_load = oc.load_manifest

        def _counting_load(run_dir: Path) -> Any:
            parses["n"] += 1
            return real_load(run_dir)

        monkeypatch.setattr(oc, "load_manifest", _counting_load)
        found = oc.discover_runs(tmp_path / "sweeps", limit=2)
        assert [info.run_id for info in found] == ["r-4", "r-3"]
        assert parses["n"] == 2


# ── fixture root resolution ─────────────────────────────────────────────


class TestResolveFixtureRoot:
    def test_unset_env_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(oc.FIXTURE_ROOT_ENV, raising=False)
        assert oc.resolve_fixture_root() is None

    def test_blank_env_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, "   ")
        assert oc.resolve_fixture_root() is None

    def test_nonexistent_dir_fails_loud_naming_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(tmp_path / "missing"))
        with pytest.raises(oc.OneclickConfigError, match=oc.FIXTURE_ROOT_ENV):
            oc.resolve_fixture_root()

    def test_file_instead_of_dir_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "afile"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(target))
        with pytest.raises(oc.OneclickConfigError):
            oc.resolve_fixture_root()

    def test_valid_dir_resolves_and_logs_once_per_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(tmp_path))
        with structlog.testing.capture_logs() as logs:
            assert oc.resolve_fixture_root() == tmp_path
            assert oc.resolve_fixture_root() == tmp_path  # second call: no re-log
        active = [entry for entry in logs if entry["event"] == "oneclick.fixture_root_active"]
        assert len(active) == 1
        oc._reset_fixture_root_logged()  # the test seam re-arms the log
        with structlog.testing.capture_logs() as logs:
            assert oc.resolve_fixture_root() == tmp_path
        assert any(entry["event"] == "oneclick.fixture_root_active" for entry in logs)


# ── trades readers ──────────────────────────────────────────────────────


class TestAwaitingTradesReader:
    def test_always_awaiting_never_fabricates(self) -> None:
        page = oc.AwaitingTradesReader().page("r-x", 1, offset=0, limit=100)
        assert page.status == "awaiting"
        assert page.rows == ()
        assert page.total is None  # never a fabricated 0
        assert page.source == "none"
        assert "no lake root wired" in page.detail


def _trades_file(root: Path, run_id: str = "r-x", rank: int = 1, rows: int = 10) -> Path:
    """Write an interim trades page INSIDE the fixture tree rooted at *root*."""
    path = oc.trades_path(oc.fixture_results_root(root), run_id, rank)
    payload = {
        "rows": [{"entry_ts": f"t{i}", "pnl_paisa": i * 100} for i in range(rows)],
        "total": rows,
    }
    _write(path, json.dumps(payload))
    return path


class TestFixtureTradesReader:
    def test_unsafe_run_id_and_bad_rank_degrade(self, tmp_path: Path) -> None:
        reader = oc.FixtureTradesReader(tmp_path)
        assert reader.page("../evil", 1, offset=0, limit=10).status == "awaiting"
        assert reader.page("r-x", 0, offset=0, limit=10).status == "awaiting"
        assert reader.parse_count == 0

    def test_missing_page_file_is_awaiting_total_none(self, tmp_path: Path) -> None:
        reader = oc.FixtureTradesReader(tmp_path)
        page = reader.page("r-x", 3, offset=0, limit=10)
        assert page.status == "awaiting"
        assert page.total is None
        assert page.source == "none"
        assert "rank 3" in page.detail

    def test_ok_paging_slices_the_parsed_rows(self, tmp_path: Path) -> None:
        _trades_file(tmp_path, rows=10)
        reader = oc.FixtureTradesReader(tmp_path)
        page = reader.page("r-x", 1, offset=0, limit=3)
        assert page.status == "ok"
        assert [row["entry_ts"] for row in page.rows] == ["t0", "t1", "t2"]
        assert page.total == 10
        assert page.source == "fixture"
        tail = reader.page("r-x", 1, offset=8, limit=5)
        assert [row["entry_ts"] for row in tail.rows] == ["t8", "t9"]
        beyond = reader.page("r-x", 1, offset=99, limit=5)
        assert beyond.status == "ok"
        assert beyond.rows == ()

    def test_offset_and_limit_are_clamped(self, tmp_path: Path) -> None:
        _trades_file(tmp_path, rows=5)
        reader = oc.FixtureTradesReader(tmp_path)
        page = reader.page("r-x", 1, offset=-9, limit=0)
        assert [row["entry_ts"] for row in page.rows] == ["t0"]

    def test_parse_once_per_revision_memo(self, tmp_path: Path) -> None:
        """C-M3: the SECOND page click does NOT re-parse (deterministic spy)."""
        path = _trades_file(tmp_path, rows=6)
        reader = oc.FixtureTradesReader(tmp_path)
        reader.page("r-x", 1, offset=0, limit=2)
        reader.page("r-x", 1, offset=2, limit=2)
        reader.page("r-x", 1, offset=4, limit=2)
        assert reader.parse_count == 1
        # A rewrite moves the stat token → exactly one re-parse.
        payload = {"rows": [{"entry_ts": "new"}], "total": 1}
        path.write_text(json.dumps(payload), encoding="utf-8")
        fresh = reader.page("r-x", 1, offset=0, limit=2)
        assert reader.parse_count == 2
        assert fresh.rows[0]["entry_ts"] == "new"

    def test_corrupt_json_is_corrupt_and_memoised(self, tmp_path: Path) -> None:
        path = oc.trades_path(oc.fixture_results_root(tmp_path), "r-x", 1)
        _write(path, "{broken")
        reader = oc.FixtureTradesReader(tmp_path)
        assert reader.page("r-x", 1, offset=0, limit=10).status == "corrupt"
        assert reader.page("r-x", 1, offset=0, limit=10).status == "corrupt"
        assert reader.parse_count == 1  # the corrupt verdict is memoised too

    def test_unreadable_file_is_corrupt(self, tmp_path: Path) -> None:
        oc.trades_path(oc.fixture_results_root(tmp_path), "r-x", 1).mkdir(
            parents=True
        )  # dir → IsADirectoryError
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"

    @pytest.mark.parametrize("text", ["[]", '{"total": 3}', '{"rows": "junk"}'])
    def test_wrong_shape_is_corrupt(self, tmp_path: Path, text: str) -> None:
        _write(oc.trades_path(oc.fixture_results_root(tmp_path), "r-x", 1), text)
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"
        assert '"rows"' in page.detail

    @pytest.mark.parametrize("raw_total", [None, "412", True, 1.5])
    def test_non_int_total_is_honest_none(self, tmp_path: Path, raw_total: Any) -> None:
        payload: dict[str, Any] = {"rows": [{"entry_ts": "t0"}]}
        if raw_total is not None:
            payload["total"] = raw_total
        _write(oc.trades_path(oc.fixture_results_root(tmp_path), "r-x", 1), json.dumps(payload))
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "ok"
        assert page.total is None  # unknown stays None — never fabricated

    def test_bad_row_is_partial_keeping_prefix(self, tmp_path: Path) -> None:
        payload = {"rows": [{"entry_ts": "t0"}, "junk", {"entry_ts": "t2"}], "total": 3}
        _write(oc.trades_path(oc.fixture_results_root(tmp_path), "r-x", 1), json.dumps(payload))
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "partial"
        assert [row["entry_ts"] for row in page.rows] == ["t0"]
        assert "trade row 1" in page.detail

    def test_two_rank_alternation_no_longer_thrashes(self, tmp_path: Path) -> None:
        """Fix 4b (DEC-MEMORY-FOOTPRINT-HARDENING-001): the bounded 2-entry
        ``_RevisionMemo`` holds BOTH sides of a two-artifact alternation, so
        flipping back to rank 1 is a HIT — the superseded single-entry memo
        re-parsed on EVERY flip (this test previously pinned that thrash as
        ``parse_count == 3``; the thrash IS the defect fix 4b removes).
        """
        _trades_file(tmp_path, rank=1, rows=2)
        _trades_file(tmp_path, rank=2, rows=2)
        reader = oc.FixtureTradesReader(tmp_path)
        reader.page("r-x", 1, offset=0, limit=2)
        reader.page("r-x", 2, offset=0, limit=2)
        reader.page("r-x", 1, offset=0, limit=2)  # 2-entry memo: a HIT now
        assert reader.parse_count == 2

    def test_third_artifact_evicts_lru_first(self, tmp_path: Path) -> None:
        """Past the 2-entry bound the LEAST-recently-used artifact re-parses."""
        for rank in (1, 2, 3):
            _trades_file(tmp_path, rank=rank, rows=2)
        reader = oc.FixtureTradesReader(tmp_path)
        reader.page("r-x", 1, offset=0, limit=2)
        reader.page("r-x", 2, offset=0, limit=2)
        reader.page("r-x", 3, offset=0, limit=2)  # evicts rank 1 (the LRU)
        assert reader.parse_count == 3
        reader.page("r-x", 2, offset=0, limit=2)  # still memoised — a hit
        assert reader.parse_count == 3
        reader.page("r-x", 1, offset=0, limit=2)  # evicted — re-parses
        assert reader.parse_count == 4


# ── _RevisionMemo (fix 4b, DEC-MEMORY-FOOTPRINT-HARDENING-001) ──────────


class TestRevisionMemo:
    """The ONE consolidated reader memo: bounded LRU + per-key single-flight."""

    def test_capacity_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="capacity must be >= 1"):
            oc._RevisionMemo(capacity=0)

    def test_hit_refreshes_lru_recency(self) -> None:
        memo: oc._RevisionMemo[str] = oc._RevisionMemo(capacity=2)
        calls: list[str] = []

        def _compute(tag: str) -> Callable[[], str]:
            def _run() -> str:
                calls.append(tag)
                return tag

            return _run

        assert memo.get_or_compute(("a",), _compute("a")) == "a"
        assert memo.get_or_compute(("b",), _compute("b")) == "b"
        # Touch "a" so "b" becomes the LRU, then insert "c" → "b" evicts.
        assert memo.get_or_compute(("a",), _compute("a-again")) == "a"
        assert memo.get_or_compute(("c",), _compute("c")) == "c"
        assert memo.get_or_compute(("a",), _compute("a-again")) == "a"  # still a hit
        assert memo.get_or_compute(("b",), _compute("b2")) == "b2"  # evicted → recompute
        assert calls == ["a", "b", "c", "b2"]

    def test_concurrent_misses_pay_one_compute(self) -> None:
        """Per-key single-flight: 8 cold threads on ONE key pay exactly 1 parse."""
        memo: oc._RevisionMemo[int] = oc._RevisionMemo()
        gate = threading.Event()
        calls: list[int] = []
        results: list[int] = []

        def _compute() -> int:
            gate.wait(timeout=5.0)
            time.sleep(0.02)  # widen the window so followers genuinely wait
            calls.append(1)
            return 42

        def _worker() -> None:
            results.append(memo.get_or_compute(("k",), _compute))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(timeout=10.0)
        assert results == [42] * 8
        assert len(calls) == 1

    def test_raising_compute_caches_nothing_and_releases_the_flight(self) -> None:
        """A raising compute propagates raw; the NEXT caller recomputes (no wedge)."""
        memo: oc._RevisionMemo[str] = oc._RevisionMemo()
        with pytest.raises(RuntimeError, match="boom"):
            memo.get_or_compute(("k",), _raise_boom)
        assert memo.get_or_compute(("k",), lambda: "ok") == "ok"

    def test_waiter_takes_ownership_when_the_owner_raises(self) -> None:
        """A follower parked on a raising owner's flight LOOPS and recomputes."""
        memo: oc._RevisionMemo[str] = oc._RevisionMemo()
        owner_entered = threading.Event()
        release_owner = threading.Event()
        outcomes: list[str] = []

        def _owner_compute() -> str:
            owner_entered.set()
            release_owner.wait(timeout=5.0)
            raise RuntimeError("owner failed")

        def _owner() -> None:
            try:
                memo.get_or_compute(("k",), _owner_compute)
            except RuntimeError:
                outcomes.append("owner-raised")

        def _follower() -> None:
            outcomes.append(memo.get_or_compute(("k",), lambda: "recovered"))

        owner = threading.Thread(target=_owner)
        owner.start()
        assert owner_entered.wait(timeout=5.0)
        follower = threading.Thread(target=_follower)
        follower.start()
        time.sleep(0.05)  # let the follower park on the in-flight event
        release_owner.set()
        owner.join(timeout=10.0)
        follower.join(timeout=10.0)
        assert sorted(outcomes) == ["owner-raised", "recovered"]


def _raise_boom() -> str:
    raise RuntimeError("boom")


# ── production trades reader (DEC-ONECLICK-SWEEP-RUNS-REBIND-001) ───────


def _rebind_top100_row(rank: int, *, combo: str | None, tuple_ref: Any) -> dict[str, Any]:
    """One contract-complete top100 row + the SHIPPED #1275 additive keys."""
    row: dict[str, Any] = {
        "rank": rank,
        "mask_hex": "0x05",
        "conditions": ["PDH_CROSS_UP"],
        "tf": "5m",
        "direction": "long",
        "arm": {"sl_paisa": None, "tp_paisa": None},
        "metrics": {"sharpe": 1.25},
        "regime": {},
        "vix_band": None,
        "family_id": "fam-0001",
    }
    if combo is not None:
        row["run_id"] = combo
    if tuple_ref is not None:
        row["equity_ref"] = tuple_ref
    return row


_COMBO = "combo-0001"
_TUPLE_REF = "NSE-NIFTY/5m/long_k3_deadbeef"


def _captured_trade(seq: int, *, status_name: str = "OK") -> Any:
    """One REAL CapturedTrade via the engine's public dataclass (never forged)."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.engine.minute_path import PathStatus
    from brutex.engine.trade_capture import CapturedTrade
    from brutex.strategies.types import Side

    status = PathStatus[status_name]
    with_1m = status is not PathStatus.NO_1M_DATA
    return CapturedTrade(
        entry_ts=datetime(2026, 7, 1, 4, 0, seq % 60, tzinfo=UTC),
        entry_px=Decimal("100.00"),
        exit_ts=datetime(2026, 7, 1, 5, 0, seq % 60, tzinfo=UTC),
        exit_px=Decimal("101.00"),
        direction=Side.BUY,
        lots=None,  # NULL-by-design (Ruling 18)
        exit_reason="signal",
        pnl_paisa=100 + seq,
        costs_paisa=None,  # NULL-by-design (Ruling 18)
        mae_paisa=50 + seq,
        mfe_paisa=150 + seq,
        mae_1m_paisa=(40 + seq) if with_1m else None,
        mfe_1m_paisa=(160 + seq) if with_1m else None,
        path_1m_status=status,
        vix_entry=None,
        vix_exit=None,
    )


def _equity_point(day: int, *, close: int, low: int | None = None) -> Any:
    """One REAL SessionEquityPoint (the engine's own dataclass, never forged)."""
    from datetime import date

    from brutex.engine.minute_path import SessionEquityPoint

    return SessionEquityPoint(
        session_date=date(2026, 7, day),
        equity_close_paisa=close,
        equity_intraday_low_paisa=low,
        trades_closed=1,
    )


def _write_sidecars(
    run_dir: Path,
    *,
    tuple_ref: str = _TUPLE_REF,
    combo: str = _COMBO,
    n_trades: int = 3,
    statuses: tuple[str, ...] = (),
    equity: tuple[Any, ...] = (),
) -> None:
    """Write a REAL results-depth trio via trade_store's OWN public writer."""
    from brutex.engine.minute_path import PathStatus
    from brutex.engine.trade_capture import ComboCapture
    from brutex.engine.trade_store import write_tuple_sidecars

    paths = oc.sidecar_paths_from_ref(run_dir, tuple_ref)
    assert paths is not None
    kinds = statuses or tuple("OK" for _ in range(n_trades))
    capture = ComboCapture(
        run_id=combo,
        trades=tuple(_captured_trade(i, status_name=kind) for i, kind in enumerate(kinds)),
        equity=equity,
        equity_status=PathStatus.OK,
        vix_missing_trades=0,
        parity_ok=True,
        parity_detail="",
    )
    write_tuple_sidecars(paths, [capture], [], store_content_hash="0" * 64)


def _rebind_run(tmp_path: Path, run_id: str = "r-x", **row_kwargs: Any) -> Path:
    """A results root whose sweep_runs/<run_id> carries a rank-1 top100 entry."""
    results_root = tmp_path / "sweeps"
    run_dir = oc.runs_root(results_root) / run_id
    rows = [
        _rebind_top100_row(
            1,
            combo=row_kwargs.pop("combo", _COMBO),
            tuple_ref=row_kwargs.pop("tuple_ref", {"tuple": _TUPLE_REF, "sidecar": "equity"}),
        )
    ]
    _write(run_dir / oc.TOP100_FILENAME, json.dumps(_top100_doc(rows, run_id=run_id)))
    return results_root


class TestSidecarPathsFromRef:
    def test_mirror_pins_the_engine_sidecar_paths_for_law(self, tmp_path: Path) -> None:
        """The display-side twin is byte-equal to trade_store.sidecar_paths_for."""
        from brutex.engine.trade_store import sidecar_paths_for, sweep_run_dir

        sweeps = tmp_path / "sweeps"
        store_path = sweeps / "NSE-NIFTY" / "5m" / "long_k3_deadbeef.parquet"
        engine = sidecar_paths_for(store_path, sweeps, run_id="r-x")
        run_dir = sweep_run_dir(sweeps, "r-x")
        display = oc.sidecar_paths_from_ref(run_dir, _TUPLE_REF)
        assert display == engine

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "NSE-NIFTY/5m",
            "NSE-NIFTY/5m/stem/extra",
            "../evil/5m/stem",
            "NSE-NIFTY/5m/..",
            "NSE-NIFTY//stem",
        ],
    )
    def test_unsafe_or_malformed_refs_return_none(self, tmp_path: Path, ref: str) -> None:
        assert oc.sidecar_paths_from_ref(tmp_path, ref) is None


class TestTradeStoreReader:
    def test_unsafe_run_id_and_bad_rank_degrade(self, tmp_path: Path) -> None:
        reader = oc.TradeStoreReader(tmp_path / "sweeps")
        assert reader.page("../evil", 1, offset=0, limit=10).status == "awaiting"
        assert reader.page("r-x", 0, offset=0, limit=10).status == "awaiting"
        assert reader.parse_count == 0

    def test_absent_top100_is_awaiting(self, tmp_path: Path) -> None:
        page = oc.TradeStoreReader(tmp_path / "sweeps").page("r-x", 1, offset=0, limit=10)
        assert page.status == "awaiting"
        assert page.total is None
        assert page.source == "none"
        assert oc.AWAITING_DETAIL in page.detail

    def test_corrupt_top100_is_corrupt(self, tmp_path: Path) -> None:
        results_root = tmp_path / "sweeps"
        _write(oc.runs_root(results_root) / "r-x" / oc.TOP100_FILENAME, "{broken")
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"
        assert page.rows == ()

    def test_rank_not_in_top100_is_awaiting(self, tmp_path: Path) -> None:
        results_root = _rebind_run(tmp_path)
        page = oc.TradeStoreReader(results_root).page("r-x", 42, offset=0, limit=10)
        assert page.status == "awaiting"
        assert "rank 42" in page.detail

    @pytest.mark.parametrize(
        "row_kwargs",
        [
            {"combo": None},  # pre-#1275 artifact: no additive run_id
            {"tuple_ref": None},  # no equity_ref at all
            {"tuple_ref": "not-a-dict"},  # equity_ref not an object
            {"tuple_ref": {"sidecar": "equity"}},  # no tuple key
            {"combo": ""},  # empty combo run_id
        ],
    )
    def test_missing_results_depth_reference_is_awaiting(
        self, tmp_path: Path, row_kwargs: dict[str, Any]
    ) -> None:
        results_root = _rebind_run(tmp_path, **row_kwargs)
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "awaiting"
        assert "pre-#1275" in page.detail

    def test_unsafe_tuple_ref_is_corrupt_never_touching_disk(self, tmp_path: Path) -> None:
        results_root = _rebind_run(
            tmp_path, tuple_ref={"tuple": "../../evil/5m/stem", "sidecar": "equity"}
        )
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"
        assert "safe path segments" in page.detail

    def test_absent_sidecars_is_awaiting(self, tmp_path: Path) -> None:
        """Top100 present but NO results-depth trio → honest awaiting (never 0)."""
        results_root = _rebind_run(tmp_path)
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "awaiting"
        assert page.total is None
        assert page.source == "none"
        assert "results-depth sidecars absent" in page.detail

    def test_corrupt_stamp_is_named_corrupt_never_a_raise(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir)
        paths = oc.sidecar_paths_from_ref(run_dir, _TUPLE_REF)
        assert paths is not None
        paths.stamp.write_text("{broken", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"
        assert page.source == "results_store"
        assert "results-depth read failed" in page.detail
        assert any(entry["event"] == "oneclick.artifact_corrupt" for entry in logs)

    def test_real_sidecars_page_with_additive_and_null_fields(self, tmp_path: Path) -> None:
        """The happy rebind: REAL writer-built sidecars page through the reader."""
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, statuses=("OK", "PARTIAL", "NO_1M_DATA"))
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert page.status == "ok"
        assert page.source == "results_store"
        assert page.total == 3
        assert page.detail == ""  # no clamp engaged
        assert [row["trade_seq"] for row in page.rows] == [0, 1, 2]
        first, second, third = page.rows
        # Ruling 18: lots/costs pass through as honest None — never 1/0.
        assert first["lots"] is None
        assert first["costs_paisa"] is None
        # mae_granularity: 1m when the minute path fully covered, else tf.
        assert first["path_1m_status"] == "ok"
        assert first["mae_granularity"] == "1m"
        assert (first["mae"], first["mfe"]) == (40, 160)  # the 1m values
        assert second["mae_granularity"] == "tf"
        assert (second["mae"], second["mfe"]) == (51, 151)  # the sweep-TF values
        assert third["path_1m_status"] == "no_1m_data"
        assert third["mae_granularity"] == "tf"

    def test_offset_pages_and_unknown_combo_totals_zero(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, n_trades=5)
        reader = oc.TradeStoreReader(results_root)
        tail = reader.page("r-x", 1, offset=3, limit=10)
        assert [row["trade_seq"] for row in tail.rows] == [3, 4]
        assert tail.total == 5
        # A combo absent from the stamp pages honestly as total 0 (trade_store law).
        _write(
            run_dir / oc.TOP100_FILENAME,
            json.dumps(
                _top100_doc(
                    [
                        _rebind_top100_row(
                            1,
                            combo="combo-ghost",
                            tuple_ref={"tuple": _TUPLE_REF, "sidecar": "equity"},
                        )
                    ],
                    run_id="r-x",
                )
            ),
        )
        ghost = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=10)
        assert ghost.status == "ok"
        assert ghost.total == 0
        assert ghost.rows == ()

    def test_limit_beyond_page_limit_max_is_clamped_and_named(self, tmp_path: Path) -> None:
        """PAGE_LIMIT_MAX=500 (the trade_store clamp law) — named in detail."""
        pytest.importorskip("polars")
        from brutex.engine.trade_store import PAGE_LIMIT_MAX

        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, n_trades=PAGE_LIMIT_MAX + 40)
        page = oc.TradeStoreReader(results_root).page("r-x", 1, offset=0, limit=1000)
        assert page.status == "ok"
        assert len(page.rows) == PAGE_LIMIT_MAX
        assert page.total == PAGE_LIMIT_MAX + 40
        assert f"limit clamped to {PAGE_LIMIT_MAX}" in page.detail

    def test_top100_parse_once_per_revision_memo(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir)
        reader = oc.TradeStoreReader(results_root)
        reader.page("r-x", 1, offset=0, limit=5)
        reader.page("r-x", 1, offset=1, limit=5)
        assert reader.parse_count == 1  # the memo held across pages
        # A top100 rewrite moves the stat token → exactly one re-parse.
        top_path = run_dir / oc.TOP100_FILENAME
        rows = json.loads(top_path.read_text(encoding="utf-8"))
        top_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        reader.page("r-x", 1, offset=0, limit=5)
        assert reader.parse_count == 2

    def test_absent_top100_computes_fresh_uncached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ABSENT top100 (probe answered: no file) computes fresh, caches NOTHING.

        DEC-CACHE-SERVE-LAST-KNOWN-001 narrows the old blanket None-revision
        doctrine to the absent arm only — a deleted/never-written artifact
        must never be served from the memo (no stale illusion).
        """
        results_root = _rebind_run(tmp_path)
        reader = oc.TradeStoreReader(results_root)
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, False))
        page = reader.page("r-x", 1, offset=0, limit=5)
        # absent top100 → fresh parse of the (missing) artifact → awaiting.
        assert page.status == "awaiting"
        assert reader.parse_count == 1
        reader.page("r-x", 1, offset=0, limit=5)
        assert reader.parse_count == 2  # nothing was cached

    def test_top100_probe_outage_serves_last_known_memo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe OUTAGE serves the memoised top100 (labelled) — no re-parse storm.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (fails-before: the pre-change reader
        re-parsed the whole artifact on EVERY trades-page request while the
        stat probe was down).
        """
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir)
        reader = oc.TradeStoreReader(results_root)
        assert reader.page("r-x", 1, offset=0, limit=5).status == "ok"
        assert reader.parse_count == 1  # memo primed pre-outage
        real_probe = oc.artifact_probe

        def _outage_on_top100(path: Path) -> oc.ArtifactProbe:
            if path.name == oc.TOP100_FILENAME:
                return oc.ArtifactProbe(None, True)
            return real_probe(path)

        monkeypatch.setattr(oc, "artifact_probe", _outage_on_top100)
        for _ in range(3):
            page = reader.page("r-x", 1, offset=0, limit=5)
            assert page.status == "ok"
            assert "top100 revision probe outage — serving last-known" in page.detail
        assert reader.parse_count == 1  # the memo served every outage page
        monkeypatch.setattr(oc, "artifact_probe", real_probe)
        clean = reader.page("r-x", 1, offset=0, limit=5)
        assert clean.status == "ok"
        assert "outage" not in clean.detail  # the label clears on recovery
        assert reader.parse_count == 1  # same revision → the memo still holds

    def test_top100_outage_note_rides_a_served_corrupt_memo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A last-known CORRUPT top100 served under outage carries BOTH notes."""
        results_root = tmp_path / "sweeps"
        run_dir = oc.runs_root(results_root) / "r-x"
        _write(run_dir / oc.TOP100_FILENAME, "{not json")
        reader = oc.TradeStoreReader(results_root)
        assert reader.page("r-x", 1, offset=0, limit=5).status == "corrupt"
        assert reader.parse_count == 1  # corrupt verdict memoised per revision
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))
        page = reader.page("r-x", 1, offset=0, limit=5)
        assert page.status == "corrupt"
        assert "top100 revision probe outage — serving last-known" in page.detail
        assert ";" in page.detail  # the corrupt detail is kept, the note appended
        assert reader.parse_count == 1  # served from the memo — no re-parse

    def test_top100_probe_outage_without_memo_parses_fresh_uncached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage with NO primed memo falls back to the fresh uncached parse."""
        results_root = _rebind_run(tmp_path)
        reader = oc.TradeStoreReader(results_root)
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))
        page = reader.page("r-x", 1, offset=0, limit=5)
        assert page.status == "awaiting"
        assert reader.parse_count == 1
        reader.page("r-x", 1, offset=0, limit=5)
        assert reader.parse_count == 2  # nothing cached — no memo to serve

    def test_top100_outage_with_evicted_memo_parses_fresh_uncached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage whose last-known entry was LRU-EVICTED falls back honestly.

        The #1719 merge-compose: the serve-last-known doctrine now peeks the
        bounded ``_RevisionMemo`` (capacity 2) — a two-sibling-run alternation
        can evict the outage path's entry, in which case the gate still holds
        a last-known key but the peek misses and the honest fresh UNCACHED
        parse stands (never a fabricated serve, never a crash).
        """
        results_root = _rebind_run(tmp_path)
        _rebind_run(tmp_path, run_id="r-y")
        _rebind_run(tmp_path, run_id="r-z")
        reader = oc.TradeStoreReader(results_root)
        reader.page("r-x", 1, offset=0, limit=5)  # memo primed for r-x
        reader.page("r-y", 1, offset=0, limit=5)
        reader.page("r-z", 1, offset=0, limit=5)  # capacity 2 → r-x evicted
        assert reader.parse_count == 3
        real_probe = oc.artifact_probe

        def _outage_on_rx_top100(path: Path) -> oc.ArtifactProbe:
            if path.name == oc.TOP100_FILENAME and path.parent.name == "r-x":
                return oc.ArtifactProbe(None, True)
            return real_probe(path)

        monkeypatch.setattr(oc, "artifact_probe", _outage_on_rx_top100)
        reader.page("r-x", 1, offset=0, limit=5)
        assert reader.parse_count == 4  # peek missed (evicted) → fresh parse
        reader.page("r-x", 1, offset=0, limit=5)
        assert reader.parse_count == 5  # and nothing was cached under outage

    def test_revision_memo_peek_hit_and_miss(self) -> None:
        """``_RevisionMemo.peek`` returns without computing — hit and miss arms."""
        memo: oc._RevisionMemo[str] = oc._RevisionMemo()
        assert memo.peek(("k", "r1")) is None  # miss — nothing computed
        assert memo.get_or_compute(("k", "r1"), lambda: "v1") == "v1"
        assert memo.peek(("k", "r1")) == "v1"  # hit — no compute ran

    def test_top100_outage_memo_for_a_different_path_is_not_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The memo serves ONLY its own path under outage — never a foreign run's."""
        results_root = _rebind_run(tmp_path)
        _rebind_run(tmp_path, run_id="r-y")
        reader = oc.TradeStoreReader(results_root)
        reader.page("r-x", 1, offset=0, limit=5)  # memo primed for r-x
        assert reader.parse_count == 1
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))
        reader.page("r-y", 1, offset=0, limit=5)
        assert reader.parse_count == 2  # r-y parsed fresh — r-x's memo not served

    def test_top100_outage_warns_once_per_outage_and_logs_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reader's OutageGate WARNs once per outage per path + recovery INFO.

        DEC-CACHE-SERVE-LAST-KNOWN-001 reconciled onto #1406's ONE
        ``OutageGate`` mechanism: no route gate covers this reader, so it owns
        a gate keyed on the top100 path — ``oneclick.probe_outage`` fires on
        the FIRST outage poll only, ``oneclick.probe_recovered`` on the flip
        back, never per poll.
        """
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir)
        reader = oc.TradeStoreReader(results_root)
        assert reader.page("r-x", 1, offset=0, limit=5).status == "ok"
        real_probe = oc.artifact_probe

        def _outage_on_top100(path: Path) -> oc.ArtifactProbe:
            if path.name == oc.TOP100_FILENAME:
                return oc.ArtifactProbe(None, True)
            return real_probe(path)

        monkeypatch.setattr(oc, "artifact_probe", _outage_on_top100)
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                reader.page("r-x", 1, offset=0, limit=5)
        warns = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(warns) == 1  # once per outage, never per poll
        assert warns[0]["coordinate"] == str(run_dir / oc.TOP100_FILENAME)
        assert warns[0]["last_known"] is True
        monkeypatch.setattr(oc, "artifact_probe", real_probe)
        with structlog.testing.capture_logs() as logs:
            reader.page("r-x", 1, offset=0, limit=5)
        assert any(e["event"] == "oneclick.probe_recovered" for e in logs)


_EQUITY_POINTS = (
    _equity_point(1, close=1_000, low=900),
    _equity_point(2, close=1_500, low=None),  # honest None low (no-1m session)
    _equity_point(3, close=1_200, low=1_100),
)


class TestEquityPage:
    """The equity-series page (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 item 4C)."""

    def test_awaiting_reader_equity_is_awaiting(self) -> None:
        page = oc.AwaitingTradesReader().equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "awaiting"
        assert page.total is None
        assert "equity series" in page.detail

    def test_fixture_reader_equity_is_awaiting_never_synthesized(self, tmp_path: Path) -> None:
        page = oc.FixtureTradesReader(tmp_path).equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "awaiting"
        assert page.rows == ()
        assert "no results-depth" in page.detail

    def test_shared_resolution_degrades_match_the_trades_page(self, tmp_path: Path) -> None:
        """The ONE _resolve_sidecars chain: every trades degrade fires on the
        equity page with the SAME status/detail (unsafe id · absent top100 ·
        rank miss · absent sidecars)."""
        reader = oc.TradeStoreReader(tmp_path / "sweeps")
        assert reader.equity_page("../evil", 1, offset=0, limit=10).status == "awaiting"
        assert reader.equity_page("r-x", 0, offset=0, limit=10).status == "awaiting"
        results_root = _rebind_run(tmp_path)
        reader = oc.TradeStoreReader(results_root)
        miss = reader.equity_page("r-x", 42, offset=0, limit=10)
        assert miss.status == "awaiting"
        assert "rank 42" in miss.detail
        absent = reader.equity_page("r-x", 1, offset=0, limit=10)
        assert absent.status == "awaiting"
        assert "sidecars absent" in absent.detail

    def test_real_sidecars_serve_the_stored_series(self, tmp_path: Path) -> None:
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, equity=_EQUITY_POINTS)
        page = oc.TradeStoreReader(results_root).equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "ok"
        assert page.source == "results_store"
        assert page.total == 3
        assert [row["session_date"] for row in page.rows] == [
            "2026-07-01",
            "2026-07-02",
            "2026-07-03",
        ]
        assert [row["equity_close_paisa"] for row in page.rows] == [1_000, 1_500, 1_200]
        # The honest None low passes through — never a fabricated 0 (R31 law).
        assert page.rows[1]["equity_intraday_low_paisa"] is None
        assert page.rows[0]["equity_intraday_low_paisa"] == 900
        assert all(row["trades_closed"] == 1 for row in page.rows)

    def test_offset_and_negative_offset_page_the_series(self, tmp_path: Path) -> None:
        results_root = _rebind_run(tmp_path)
        _write_sidecars(oc.runs_root(results_root) / "r-x", equity=_EQUITY_POINTS)
        reader = oc.TradeStoreReader(results_root)
        page = reader.equity_page("r-x", 1, offset=1, limit=1)
        assert [row["session_date"] for row in page.rows] == ["2026-07-02"]
        assert page.total == 3
        clamped = reader.equity_page("r-x", 1, offset=-5, limit=100)
        assert len(clamped.rows) == 3  # negative offset clamps to 0

    def test_equity_page_names_a_top100_probe_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The equity page's detail carries the top100 outage note too.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (club reconcile): the note threads
        through the SHARED ``_resolve_sidecars`` 3-tuple, so BOTH pages name
        a last-known top100 serve — never a silent stale resolution.
        """
        pytest.importorskip("polars")
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, equity=_EQUITY_POINTS)
        reader = oc.TradeStoreReader(results_root)
        assert reader.equity_page("r-x", 1, offset=0, limit=100).status == "ok"
        real_probe = oc.artifact_probe

        def _outage_on_top100(path: Path) -> oc.ArtifactProbe:
            if path.name == oc.TOP100_FILENAME:
                return oc.ArtifactProbe(None, True)
            return real_probe(path)

        monkeypatch.setattr(oc, "artifact_probe", _outage_on_top100)
        page = reader.equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "ok"
        assert "top100 revision probe outage — serving last-known" in page.detail

    def test_limit_beyond_the_equity_cap_is_clamped_and_named(self, tmp_path: Path) -> None:
        results_root = _rebind_run(tmp_path)
        _write_sidecars(oc.runs_root(results_root) / "r-x", equity=_EQUITY_POINTS)
        page = oc.TradeStoreReader(results_root).equity_page("r-x", 1, offset=0, limit=999_999)
        assert page.status == "ok"
        assert page.detail == "limit clamped to 5000 (equity page law)"

    def test_unknown_combo_serves_an_honest_empty_series(self, tmp_path: Path) -> None:
        """A combo with no equity rows (present sidecars, foreign run_id
        filter) pages total=0 — never an invented curve."""
        results_root = _rebind_run(tmp_path, combo="beefbeef" + "0" * 56)
        _write_sidecars(oc.runs_root(results_root) / "r-x", combo=_COMBO, equity=_EQUITY_POINTS)
        page = oc.TradeStoreReader(results_root).equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "ok"
        assert page.rows == ()
        assert page.total == 0

    def test_unreadable_equity_sidecar_is_named_corrupt(self, tmp_path: Path) -> None:
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, equity=_EQUITY_POINTS)
        paths = oc.sidecar_paths_from_ref(run_dir, _TUPLE_REF)
        assert paths is not None
        paths.equity.write_bytes(b"{not parquet")
        page = oc.TradeStoreReader(results_root).equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "corrupt"
        assert "equity sidecar read failed" in page.detail

    def test_polars_unavailable_is_named_corrupt_never_a_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The [data]-extra-absent arm: ImportError maps to the SAME corrupt
        degrade (the route can never 500 over a missing dependency)."""
        import sys

        results_root = _rebind_run(tmp_path)
        _write_sidecars(oc.runs_root(results_root) / "r-x", equity=_EQUITY_POINTS)
        monkeypatch.setitem(sys.modules, "polars", None)
        page = oc.TradeStoreReader(results_root).equity_page("r-x", 1, offset=0, limit=100)
        assert page.status == "corrupt"
        assert "polars is required" in page.detail

    def test_series_memo_reads_once_per_revision_per_combo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results_root = _rebind_run(tmp_path)
        _write_sidecars(oc.runs_root(results_root) / "r-x", equity=_EQUITY_POINTS)
        reader = oc.TradeStoreReader(results_root)
        calls = {"n": 0}
        real = oc._load_equity_records

        def _spy(path: Path, combo: str) -> tuple[dict[str, Any], ...]:
            calls["n"] += 1
            return real(path, combo)

        monkeypatch.setattr(oc, "_load_equity_records", _spy)
        for _ in range(3):
            page = reader.equity_page("r-x", 1, offset=0, limit=2)
            assert page.status == "ok"
        assert calls["n"] == 1  # one filtered read; repeat pages slice the memo

    def test_unstattable_equity_reads_fresh_and_caches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None equity revision computes fresh and caches NOTHING."""
        results_root = _rebind_run(tmp_path)
        run_dir = oc.runs_root(results_root) / "r-x"
        _write_sidecars(run_dir, equity=_EQUITY_POINTS)
        paths = oc.sidecar_paths_from_ref(run_dir, _TUPLE_REF)
        assert paths is not None
        reader = oc.TradeStoreReader(results_root)
        real = oc.artifact_revision
        monkeypatch.setattr(
            oc,
            "artifact_revision",
            lambda path: None if path == paths.equity else real(path),
        )
        calls = {"n": 0}
        real_load = oc._load_equity_records

        def _spy(path: Path, combo: str) -> tuple[dict[str, Any], ...]:
            calls["n"] += 1
            return real_load(path, combo)

        monkeypatch.setattr(oc, "_load_equity_records", _spy)
        assert reader.equity_page("r-x", 1, offset=0, limit=2).status == "ok"
        assert reader.equity_page("r-x", 1, offset=0, limit=2).status == "ok"
        assert calls["n"] == 2  # nothing was cached (lake_revision doctrine)


from structlog.testing import capture_logs  # noqa: E402


def _cascade_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "pass",
        "rejected_at": None,
        "detail": "survived all Stage-4 filters",
        "cascade_composite": 1.5,
        "provisional": False,
        "cpcv_pass_rate": 0.75,
        "survivor_rank": 1,
    }
    base.update(overrides)
    return base


class TestBridgeCascade:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the per-row cascade bridge."""

    def test_absent_key_passes_through_untouched(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1)])
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert "cascade" not in result.rows[0]

    @pytest.mark.parametrize(
        ("status", "expected_text"),
        [
            ("pass", "PASS"),
            ("provisional_pass", "PASS (provisional)"),
            ("fail", "FAIL @ cpcv"),
            ("not_validated", "not validated"),
        ],
    )
    def test_valid_statuses_stamped_with_server_text(
        self, tmp_path: Path, status: str, expected_text: str
    ) -> None:
        obj = _cascade_obj(status=status)
        if status == "fail":
            obj["rejected_at"] = "cpcv"
        if status == "not_validated":
            obj = {"status": "not_validated", "reason": "no_report"}
        _write_top100(tmp_path, [_top100_row(1, cascade=obj)])
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        cascade = result.rows[0]["cascade"]
        assert cascade["status_text"] == expected_text
        # The raw fields ride through untouched beside the stamp.
        assert cascade["status"] == status

    def test_actionable_reasons_carry_their_suffix(self, tmp_path: Path) -> None:
        rows = [
            _top100_row(1, cascade={"status": "not_validated", "reason": "sweep_id_mismatch"}),
            _top100_row(2, cascade={"status": "not_validated", "reason": "duplicate_candidate_id"}),
        ]
        _write_top100(tmp_path, rows)
        result = oc.load_top100(tmp_path)
        assert result.rows[0]["cascade"]["status_text"] == "not validated · sweep_id_mismatch"
        assert result.rows[1]["cascade"]["status_text"] == "not validated · duplicate verdict"

    @pytest.mark.parametrize(
        "obj",
        [
            "not-a-dict",
            {"status": "GREEN"},
            {"status": 7},
            {"status": "pass", "detail": 42},
            {"status": "pass", "provisional": "yes"},
            {"status": "pass", "cpcv_pass_rate": "high"},
            {"status": "pass", "cascade_composite": True},
            {"status": "pass", "survivor_rank": 1.5},
            {"status": "not_validated", "reason": 9},
            {"status": "fail", "rejected_at": 3},
            None,
        ],
    )
    def test_invalid_object_dropped_and_named_partial(self, tmp_path: Path, obj: Any) -> None:
        _write_top100(tmp_path, [_top100_row(1, cascade=obj)])
        with capture_logs() as logs:
            result = oc.load_top100(tmp_path)
        # Row KEPT, key DROPPED, degrade NAMED — never a fabricated verdict.
        assert result.status == "partial"
        assert "invalid cascade object" in result.detail
        assert "cascade" not in result.rows[0]
        assert [e for e in logs if e["event"] == "oneclick.cascade_object_dropped"]

    def test_root_cascade_join_passthrough(self, tmp_path: Path) -> None:
        meta = {"state": "joined", "sweep_id": "s" * 64, "n_input": 4, "n_survived": 2}
        _write_top100(tmp_path, [_top100_row(1)], cascade_join=meta)
        result = oc.load_top100(tmp_path)
        assert result.cascade_join == meta

    def test_non_object_root_meta_reads_none(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1)], cascade_join="junk")
        assert oc.load_top100(tmp_path).cascade_join is None

    def test_pre_change_doc_defaults_none(self, tmp_path: Path) -> None:
        _write_top100(tmp_path, [_top100_row(1)])
        assert oc.load_top100(tmp_path).cascade_join is None

    def test_row_cap_partial_detail_wins_over_cascade_drop(self, tmp_path: Path) -> None:
        rows = [_top100_row(i + 1, cascade="junk") for i in range(oc.TOP100_ROW_CAP + 1)]
        _write_top100(tmp_path, rows)
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        # The oversized-cap detail wins the slot; the drops still happened.
        assert "beyond the" in result.detail
        assert all("cascade" not in row for row in result.rows)
