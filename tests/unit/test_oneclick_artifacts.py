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
    def test_runs_root_appends_runs(self, tmp_path: Path) -> None:
        assert oc.runs_root(tmp_path) == tmp_path / "runs"

    def test_trades_path_zero_pads_rank(self, tmp_path: Path) -> None:
        path = oc.trades_path(tmp_path, "r-x", 7)
        assert path == tmp_path / "runs" / "r-x" / "trades" / "rank-0007.json"

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

    def test_runs_revision_stats_the_runs_dir(self, tmp_path: Path) -> None:
        assert oc.runs_revision(tmp_path) is None
        (tmp_path / "runs").mkdir()
        assert oc.runs_revision(tmp_path) is not None


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
        assert oc.discover_runs(tmp_path) == ()

    def test_newest_first_and_limit(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        for name, age in (("r-old", 100), ("r-mid", 50), ("r-new", 0)):
            (runs / name).mkdir(parents=True)
            stamp = 1_700_000_000 - age
            os.utime(runs / name, (stamp, stamp))
        found = oc.discover_runs(tmp_path)
        assert [info.run_id for info in found] == ["r-new", "r-mid", "r-old"]
        assert [info.run_id for info in oc.discover_runs(tmp_path, limit=2)] == [
            "r-new",
            "r-mid",
        ]
        assert oc.discover_runs(tmp_path, limit=0) == ()

    def test_stray_file_under_runs_is_ignored(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        (runs / "r-good").mkdir(parents=True)
        (runs / "stray.txt").write_text("x", encoding="utf-8")
        assert [info.run_id for info in oc.discover_runs(tmp_path)] == ["r-good"]

    def test_unreadable_entries_skipped_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-MINOR-1: unreadability via monkeypatched scandir entries, never chmod."""
        runs = tmp_path / "runs"
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
            found = oc.discover_runs(tmp_path)
        assert [info.run_id for info in found] == ["r-good"]
        skipped = [entry for entry in logs if entry["event"] == "oneclick.run_skipped"]
        assert {entry["run"] for entry in skipped} == {"r-badstat", "r-badisdir"}


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


class TestLoadTop100:
    def test_missing_is_awaiting(self, tmp_path: Path) -> None:
        result = oc.load_top100(tmp_path)
        assert result.status == "awaiting"
        assert result.rows == ()
        assert result.revision is None

    def test_unreadable_is_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / oc.TOP100_FILENAME).mkdir()
        assert oc.load_top100(tmp_path).status == "corrupt"

    def test_bad_json_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, '[{"rank": 1')
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert "not valid JSON" in result.detail

    def test_non_array_is_corrupt(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, '{"rows": []}')
        result = oc.load_top100(tmp_path)
        assert result.status == "corrupt"
        assert "not a JSON array" in result.detail

    def test_valid_empty_list_is_ok(self, tmp_path: Path) -> None:
        """T-M2 status law: a VALID EMPTY artifact EXISTS — status ok, 0 rows."""
        _write(tmp_path / oc.TOP100_FILENAME, "[]")
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert result.rows == ()

    def test_short_list_is_ok_with_true_row_count(self, tmp_path: Path) -> None:
        rows = [_top100_row(rank) for rank in range(1, 18)]
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps(rows))
        result = oc.load_top100(tmp_path)
        assert result.status == "ok"
        assert len(result.rows) == 17  # never assumed 100

    def test_non_dict_row_is_partial_keeping_prefix(self, tmp_path: Path) -> None:
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps([_top100_row(1), "junk"]))
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == 1
        assert "row 1" in result.detail

    def test_row_missing_keys_is_partial_naming_them(self, tmp_path: Path) -> None:
        bad = _top100_row(2)
        del bad["metrics"], bad["vix_band"]
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps([_top100_row(1), bad]))
        result = oc.load_top100(tmp_path)
        assert result.status == "partial"
        assert len(result.rows) == 1
        assert "metrics" in result.detail
        assert "vix_band" in result.detail

    def test_full_precision_metrics_pass_through_untouched(self, tmp_path: Path) -> None:
        row = _top100_row(1)
        row["metrics"]["sharpe"] = 1.0000000000000002
        _write(tmp_path / oc.TOP100_FILENAME, json.dumps([row]))
        result = oc.load_top100(tmp_path)
        assert result.rows[0]["metrics"]["sharpe"] == 1.0000000000000002


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
        assert "merge-integration" in page.detail


def _trades_file(root: Path, run_id: str = "r-x", rank: int = 1, rows: int = 10) -> Path:
    path = oc.trades_path(root, run_id, rank)
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
        path = oc.trades_path(tmp_path, "r-x", 1)
        _write(path, "{broken")
        reader = oc.FixtureTradesReader(tmp_path)
        assert reader.page("r-x", 1, offset=0, limit=10).status == "corrupt"
        assert reader.page("r-x", 1, offset=0, limit=10).status == "corrupt"
        assert reader.parse_count == 1  # the corrupt verdict is memoised too

    def test_unreadable_file_is_corrupt(self, tmp_path: Path) -> None:
        oc.trades_path(tmp_path, "r-x", 1).mkdir(parents=True)  # dir → IsADirectoryError
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"

    @pytest.mark.parametrize("text", ["[]", '{"total": 3}', '{"rows": "junk"}'])
    def test_wrong_shape_is_corrupt(self, tmp_path: Path, text: str) -> None:
        _write(oc.trades_path(tmp_path, "r-x", 1), text)
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "corrupt"
        assert '"rows"' in page.detail

    @pytest.mark.parametrize("raw_total", [None, "412", True, 1.5])
    def test_non_int_total_is_honest_none(self, tmp_path: Path, raw_total: Any) -> None:
        payload: dict[str, Any] = {"rows": [{"entry_ts": "t0"}]}
        if raw_total is not None:
            payload["total"] = raw_total
        _write(oc.trades_path(tmp_path, "r-x", 1), json.dumps(payload))
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "ok"
        assert page.total is None  # unknown stays None — never fabricated

    def test_bad_row_is_partial_keeping_prefix(self, tmp_path: Path) -> None:
        payload = {"rows": [{"entry_ts": "t0"}, "junk", {"entry_ts": "t2"}], "total": 3}
        _write(oc.trades_path(tmp_path, "r-x", 1), json.dumps(payload))
        page = oc.FixtureTradesReader(tmp_path).page("r-x", 1, offset=0, limit=10)
        assert page.status == "partial"
        assert [row["entry_ts"] for row in page.rows] == ["t0"]
        assert "trade row 1" in page.detail

    def test_different_rank_is_a_memo_miss(self, tmp_path: Path) -> None:
        """The memo is SINGLE-ENTRY by design (one artifact hot at a time)."""
        _trades_file(tmp_path, rank=1, rows=2)
        _trades_file(tmp_path, rank=2, rows=2)
        reader = oc.FixtureTradesReader(tmp_path)
        reader.page("r-x", 1, offset=0, limit=2)
        reader.page("r-x", 2, offset=0, limit=2)
        reader.page("r-x", 1, offset=0, limit=2)  # single-entry memo: re-parse
        assert reader.parse_count == 3
