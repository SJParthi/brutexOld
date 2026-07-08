"""Unit tests for brutex.dashboard.oneclick_artifacts (DEC-INSTITUTIONAL-ONECLICK-001, P4).

Covers every one of the 16 adapter functions against the checked-in fixture
tree under ``tests/fixtures/oneclick/`` PLUS ad hoc ``tmp_path`` fixtures for
scenarios the checked-in tree deliberately does not carry (corrupt
chain-map/exit-status/verify-me/trades files, mtime-controlled run ordering,
permission-style OSError). Every reader is exercised on its happy path,
its honest "awaiting_artifact" path, and its loud "artifact_error" path
(a deliberately malformed JSON fixture in every case), per the ADAPTER ROOT
RESOLUTION LAW. Path-traversal rejection is a required case on every reader
that takes a caller-supplied run_id, not an afterthought (per the
sweep-parallel-integration-contracts open-risks note).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from brutex.dashboard import oneclick_artifacts as oa

FIXTURE_ONECLICK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
FIXTURE_SWEEP_RUNS = FIXTURE_ONECLICK_ROOT / "sweep_runs"
FIXTURE_EMPTY_ROOT = FIXTURE_ONECLICK_ROOT / "empty_root"

RUN_FULL = "run_2026-07-08T090000Z_nifty_full"
RUN_SMOKE = "run_2026-07-07T210000Z_nifty_smoke_complete"
RUN_TORN = "run_torn_recorder"
RUN_MID_CORRUPT = "run_mid_file_corrupt_recorder"
RUN_CORRUPT_MANIFEST = "run_corrupt_manifest"
RUN_LEGACY = "run_legacy_top100_no_join_keys"
RUN_NO_VERIFYME = "run_no_verifyme_file"
RUN_DEMO = "demo-allpass-2026-07-08"

_TRAVERSAL_RUN_IDS = ("../../etc/passwd", "..", "a/b", "", "x" * 129, "run id with spaces")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure BRUTEX_ONECLICK_FIXTURE_ROOT never leaks between tests."""
    monkeypatch.delenv(oa.FIXTURE_ROOT_ENV, raising=False)


@pytest.fixture
def fixture_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the adapter at the checked-in sweep_runs fixture tree."""
    monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(FIXTURE_SWEEP_RUNS))
    return Path("/does/not/matter")  # lake_root is ignored once the env override is set


# ─────────────────────────── sweep_runs_root / validate_run_id ───────────────────────────


class TestSweepRunsRoot:
    def test_production_path_is_sibling_of_lake_root(self, tmp_path: Path) -> None:
        assert oa.sweep_runs_root(tmp_path) == tmp_path / "sweep_runs"

    def test_env_override_wins_and_is_the_root_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "fixture-root"
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(override))
        assert oa.sweep_runs_root(tmp_path / "unrelated-lake") == override

    def test_never_raises_when_directory_absent(self, tmp_path: Path) -> None:
        # Pure path computation — no filesystem touch, so no exception even
        # though tmp_path/sweep_runs does not exist.
        result = oa.sweep_runs_root(tmp_path / "no-such-lake")
        assert result == tmp_path / "no-such-lake" / "sweep_runs"

    def test_empty_env_var_falls_back_to_production_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, "")
        assert oa.sweep_runs_root(tmp_path) == tmp_path / "sweep_runs"


class TestValidateRunId:
    @pytest.mark.parametrize(
        "run_id",
        [RUN_FULL, RUN_SMOKE, "demo-allpass-2026-07-08", "a", "A_B.C:D-1", "1" * 128],
    )
    def test_accepts_well_formed_ids(self, run_id: str) -> None:
        assert oa.validate_run_id(run_id) == run_id

    @pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
    def test_rejects_traversal_and_malformed_ids(self, run_id: str) -> None:
        with pytest.raises(ValueError, match="invalid run_id"):
            oa.validate_run_id(run_id)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            oa.validate_run_id(None)  # type: ignore[arg-type]


# ─────────────────────────── read_json_artifact ───────────────────────────


class TestReadJsonArtifact:
    def test_ok(self, tmp_path: Path) -> None:
        p = tmp_path / "a.json"
        p.write_text('{"x": 1}', encoding="utf-8")
        state, data, err = oa.read_json_artifact(p)
        assert (state, data, err) == ("ok", {"x": 1}, None)

    def test_absent(self, tmp_path: Path) -> None:
        state, data, err = oa.read_json_artifact(tmp_path / "does-not-exist.json")
        assert (state, data, err) == ("absent", None, None)

    def test_corrupt_malformed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"x": tru', encoding="utf-8")
        state, data, err = oa.read_json_artifact(p)
        assert state == "corrupt"
        assert data is None
        assert err is not None and "invalid JSON" in err

    def test_corrupt_non_filenotfound_oserror(self, tmp_path: Path) -> None:
        # A directory where a file is expected raises IsADirectoryError, a
        # non-FileNotFoundError OSError subclass — must be "corrupt", not
        # silently treated as absent.
        d = tmp_path / "actually-a-dir"
        d.mkdir()
        state, data, err = oa.read_json_artifact(d)
        assert state == "corrupt"
        assert data is None
        assert err is not None

    def test_never_raises_on_repeated_calls(self, tmp_path: Path) -> None:
        for _ in range(3):
            assert oa.read_json_artifact(tmp_path / "still-absent.json")[0] == "absent"


# ─────────────────────────── list_runs ───────────────────────────


class TestListRuns:
    def test_ok_lists_all_checked_in_runs(self, fixture_env: Path) -> None:
        result = oa.list_runs(fixture_env)
        assert result["state"] == "ok"
        run_ids = {r["run_id"] for r in result["runs"]}
        for expected in (
            RUN_FULL,
            RUN_SMOKE,
            RUN_TORN,
            RUN_MID_CORRUPT,
            RUN_CORRUPT_MANIFEST,
            RUN_LEGACY,
            RUN_NO_VERIFYME,
            RUN_DEMO,
        ):
            assert expected in run_ids

    def test_demo_flag(self, fixture_env: Path) -> None:
        result = oa.list_runs(fixture_env)
        by_id = {r["run_id"]: r for r in result["runs"]}
        assert by_id[RUN_DEMO]["is_demo"] is True
        assert by_id[RUN_SMOKE]["is_demo"] is False

    def test_complete_vs_running_state_surfaced(self, fixture_env: Path) -> None:
        result = oa.list_runs(fixture_env)
        by_id = {r["run_id"]: r for r in result["runs"]}
        assert by_id[RUN_SMOKE]["state"] == "complete"
        assert by_id[RUN_FULL]["state"] == "running"

    def test_awaiting_when_root_absent(self, tmp_path: Path) -> None:
        result = oa.list_runs(tmp_path / "nonexistent-lake")
        assert result["state"] == "awaiting_artifact"
        assert result["runs"] == []
        assert "reason" in result

    def test_awaiting_when_empty_root_fixture(self) -> None:
        result = oa.list_runs(FIXTURE_EMPTY_ROOT)
        assert result["state"] == "awaiting_artifact"
        assert result["runs"] == []

    def test_corrupt_exit_status_row_is_included_not_dropped(self, tmp_path: Path) -> None:
        root = tmp_path / "sweep_runs"
        run_dir = root / "run_with_corrupt_exit_status"
        run_dir.mkdir(parents=True)
        (run_dir / "exit-status.json").write_text("{not valid", encoding="utf-8")
        result = oa.list_runs(tmp_path)
        assert result["state"] == "ok"
        assert len(result["runs"]) == 1
        assert result["runs"][0]["run_id"] == "run_with_corrupt_exit_status"
        assert result["runs"][0]["state"] == "artifact_error"

    def test_absent_exit_status_row_is_unknown_not_fabricated(self, tmp_path: Path) -> None:
        root = tmp_path / "sweep_runs"
        run_dir = root / "run_with_no_exit_status_yet"
        run_dir.mkdir(parents=True)
        result = oa.list_runs(tmp_path)
        assert result["runs"][0]["state"] == "unknown"

    def test_newest_first_by_mtime(self, tmp_path: Path) -> None:
        root = tmp_path / "sweep_runs"
        root.mkdir(parents=True)
        older = root / "run_older"
        newer = root / "run_newer"
        older.mkdir()
        newer.mkdir()
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now, now))
        result = oa.list_runs(tmp_path)
        run_ids = [r["run_id"] for r in result["runs"]]
        assert run_ids == ["run_newer", "run_older"]

    def test_limit_is_honoured(self, tmp_path: Path) -> None:
        root = tmp_path / "sweep_runs"
        root.mkdir(parents=True)
        for i in range(5):
            (root / f"run_{i}").mkdir()
        result = oa.list_runs(tmp_path, limit=2)
        assert result["state"] == "ok"
        assert len(result["runs"]) == 2

    def test_root_is_a_file_not_a_directory(self, tmp_path: Path) -> None:
        bogus_lake = tmp_path / "lake"
        bogus_lake.mkdir()
        (bogus_lake / "sweep_runs").write_text("not a directory", encoding="utf-8")
        result = oa.list_runs(bogus_lake)
        assert result["state"] == "awaiting_artifact"
        assert result["runs"] == []

    def test_root_iterdir_permission_error_is_artifact_error_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sweep_runs"
        root.mkdir()
        real_iterdir = Path.iterdir

        def _fake_iterdir(self: Path) -> object:
            if self == root:
                raise PermissionError("denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _fake_iterdir)
        result = oa.list_runs(tmp_path)
        assert result["state"] == "artifact_error"
        assert result["runs"] == []

    def test_per_run_stat_error_during_sort_does_not_crash_the_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mtime-sort fallback (a bare ``except OSError: return 0.0``
        # inside list_runs' local ``_mtime`` helper) protects against a run
        # directory vanishing between ``iterdir()`` and ``stat()`` (e.g. a
        # concurrent O-o run-wipe). Simulate that precise race: let
        # ``is_dir()`` succeed normally (it tolerates ENOENT internally) but
        # make the LATER explicit ``.stat()`` call used purely for sorting
        # fail with a *different* OSError that ``is_dir()`` would not
        # swallow.
        root = tmp_path / "sweep_runs"
        (root / "run_a").mkdir(parents=True)
        (root / "run_b").mkdir()
        real_stat = Path.stat
        calls: dict[str, int] = {}

        def _fake_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "run_a":
                calls["run_a"] = calls.get("run_a", 0) + 1
                if calls["run_a"] > 1:  # first call is is_dir()'s own internal stat
                    raise OSError("stat denied")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", _fake_stat)
        result = oa.list_runs(tmp_path)
        assert result["state"] == "ok"
        assert {r["run_id"] for r in result["runs"]} == {"run_a", "run_b"}


# ─────────────────────────── door_labels ───────────────────────────


class TestDoorLabels:
    def test_covers_the_full_documented_vocabulary(self) -> None:
        labels = oa.door_labels()
        expected = {
            "min_hits",
            "no_bars",
            "cache_skip",
            "resume_skip",
            "dsr",
            "spa",
            "cpcv",
            "walk_forward",
            "k_of_n",
            "robustness",
            "min_trades",
            "profit_factor",
            "max_drawdown",
            "win_rate",
            "consecutive_losses",
        }
        assert set(labels) == expected

    def test_every_entry_is_a_two_tuple_of_strings(self) -> None:
        for key, value in oa.door_labels().items():
            assert isinstance(key, str)
            assert isinstance(value, tuple) and len(value) == 2
            assert all(isinstance(v, str) and v for v in value)

    def test_pure_and_stable_across_calls(self) -> None:
        assert oa.door_labels() == oa.door_labels()


# ─────────────────────────── read_chain_map ───────────────────────────


class TestReadChainMap:
    def test_ok(self, fixture_env: Path) -> None:
        result = oa.read_chain_map(fixture_env, RUN_SMOKE)
        assert result["state"] == "ok"
        assert result["run_id"] == RUN_SMOKE
        assert result["links"]["preflight"]["state"] == "green"
        assert result["suspect"] is False
        assert result["schema_version"] == 1

    def test_awaiting_when_run_dir_absent(self, fixture_env: Path) -> None:
        result = oa.read_chain_map(fixture_env, "run_that_never_launched")
        assert result["state"] == "awaiting_artifact"
        assert result["run_id"] == "run_that_never_launched"

    def test_corrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_x"
        run_dir.mkdir()
        (run_dir / "chain-map.json").write_text("{bad json", encoding="utf-8")
        result = oa.read_chain_map(tmp_path, "run_x")
        assert result["state"] == "artifact_error"
        assert "chain-map.json present but unreadable" in result["reason"]

    def test_non_object_json_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_y"
        run_dir.mkdir()
        (run_dir / "chain-map.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_chain_map(tmp_path, "run_y")
        assert result["state"] == "artifact_error"

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_chain_map(fixture_env, "../../etc/passwd")


# ─────────────────────────── read_exit_status ───────────────────────────


class TestReadExitStatus:
    def test_ok_running(self, fixture_env: Path) -> None:
        result = oa.read_exit_status(fixture_env, RUN_FULL)
        assert result["state"] == "ok"
        assert result["run_state"] == "running"
        assert result["exit_code"] is None
        assert result["wall_ended_utc"] is None

    def test_ok_complete(self, fixture_env: Path) -> None:
        result = oa.read_exit_status(fixture_env, RUN_SMOKE)
        assert result["run_state"] == "complete"
        assert result["exit_code"] == 0
        assert result["wall_ended_utc"] is not None

    def test_awaiting(self, fixture_env: Path) -> None:
        result = oa.read_exit_status(fixture_env, "run_never_launched")
        assert result["state"] == "awaiting_artifact"

    def test_corrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_z"
        run_dir.mkdir()
        (run_dir / "exit-status.json").write_text("not json at all {{{", encoding="utf-8")
        result = oa.read_exit_status(tmp_path, "run_z")
        assert result["state"] == "artifact_error"

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_exit_status(fixture_env, "..")

    def test_non_object_json_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_array_exit_status"
        run_dir.mkdir()
        (run_dir / "exit-status.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_exit_status(tmp_path, "run_array_exit_status")
        assert result["state"] == "artifact_error"


# ─────────────────────────── read_manifest ───────────────────────────


class TestReadManifest:
    def test_ok_full_doors_observed_on_completed_run(self, fixture_env: Path) -> None:
        result = oa.read_manifest(fixture_env, RUN_SMOKE)
        assert result["state"] == "ok"
        by_name = result["per_door_kills_by_name"]
        for door in (
            "min_hits",
            "no_bars",
            "cache_skip",
            "resume_skip",
            "dsr",
            "spa",
            "cpcv",
            "walk_forward",
            "k_of_n",
            "robustness",
        ):
            assert by_name[door]["observed"] is True
            assert isinstance(by_name[door]["killed"], int)
        for door in (
            "min_trades",
            "profit_factor",
            "max_drawdown",
            "win_rate",
            "consecutive_losses",
        ):
            assert by_name[door] == {"observed": False, "killed": None}

    def test_absent_door_law_on_in_progress_run(self, fixture_env: Path) -> None:
        # D-8: an unobservable door is ABSENT — never fabricated as 0.
        result = oa.read_manifest(fixture_env, RUN_FULL)
        by_name = result["per_door_kills_by_name"]
        for door in ("dsr", "spa", "cpcv", "walk_forward", "k_of_n", "robustness"):
            assert by_name[door]["observed"] is False
            assert by_name[door]["killed"] is None
        assert by_name["min_hits"]["observed"] is True
        assert by_name["min_hits"]["killed"] == 812004

    def test_contract_keys_pass_through_verbatim(self, fixture_env: Path) -> None:
        result = oa.read_manifest(fixture_env, RUN_SMOKE)
        for key in (
            "selection",
            "estimate",
            "actual",
            "per_stage_timings",
            "per_door_kills",
            "dedup_skips",
            "tripwires",
            "suspect",
            "schema_version",
            "dedup_skips_detail",
        ):
            assert key in result

    def test_estimate_null_is_preserved_not_fabricated(self, fixture_env: Path) -> None:
        result = oa.read_manifest(fixture_env, RUN_FULL)
        assert result["estimate"] is None

    def test_unknown_door_in_file_is_surfaced_not_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_future_door"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run_future_door",
                    "selection": {},
                    "estimate": None,
                    "actual": {},
                    "per_stage_timings": {},
                    "per_door_kills": [{"door": "future_p3_door", "killed": 7}],
                    "dedup_skips": 0,
                    "tripwires": [],
                    "suspect": False,
                }
            ),
            encoding="utf-8",
        )
        result = oa.read_manifest(tmp_path, "run_future_door")
        assert result["per_door_kills_by_name"]["future_p3_door"] == {"observed": True, "killed": 7}

    def test_awaiting_when_absent(self, fixture_env: Path) -> None:
        result = oa.read_manifest(fixture_env, "run_never_launched")
        assert result["state"] == "awaiting_artifact"

    def test_corrupt(self, fixture_env: Path) -> None:
        result = oa.read_manifest(fixture_env, RUN_CORRUPT_MANIFEST)
        assert result["state"] == "artifact_error"
        assert "manifest.json present but unreadable" in result["reason"]

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_manifest(fixture_env, "../secret")

    def test_non_object_json_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_array_manifest"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_manifest(tmp_path, "run_array_manifest")
        assert result["state"] == "artifact_error"

    def test_malformed_per_door_kills_entries_are_ignored_not_crashed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_malformed_kills"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run_malformed_kills",
                    "selection": {},
                    "estimate": None,
                    "actual": {},
                    "per_stage_timings": {},
                    # not a list at all
                    "per_door_kills": "not-a-list",
                    "dedup_skips": 0,
                    "tripwires": [],
                    "suspect": False,
                }
            ),
            encoding="utf-8",
        )
        result = oa.read_manifest(tmp_path, "run_malformed_kills")
        assert result["state"] == "ok"
        assert result["per_door_kills_by_name"]["min_hits"] == {"observed": False, "killed": None}

    def test_per_door_kills_entries_missing_door_key_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_no_door_key"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run_no_door_key",
                    "selection": {},
                    "estimate": None,
                    "actual": {},
                    "per_stage_timings": {},
                    "per_door_kills": [
                        "not-a-dict",
                        {"killed": 5},
                        {"door": "min_hits", "killed": 3},
                    ],
                    "dedup_skips": 0,
                    "tripwires": [],
                    "suspect": False,
                }
            ),
            encoding="utf-8",
        )
        result = oa.read_manifest(tmp_path, "run_no_door_key")
        assert result["per_door_kills_by_name"]["min_hits"] == {"observed": True, "killed": 3}


# ─────────────────────────── read_run_status ───────────────────────────


class TestReadRunStatus:
    def test_bundles_all_three_for_explicit_run_id(self, fixture_env: Path) -> None:
        result = oa.read_run_status(fixture_env, RUN_SMOKE)
        assert result["run_id"] == RUN_SMOKE
        assert result["chain_map"]["state"] == "ok"
        assert result["exit_status"]["state"] == "ok"
        assert result["manifest"]["state"] == "ok"

    def test_resolves_latest_run_when_run_id_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        older = tmp_path / "run_older"
        newer = tmp_path / "run_newer"
        older.mkdir()
        newer.mkdir()
        (older / "exit-status.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
        (newer / "exit-status.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
        now = time.time()
        os.utime(older, (now - 50, now - 50))
        os.utime(newer, (now, now))
        result = oa.read_run_status(tmp_path)
        assert result["run_id"] == "run_newer"

    def test_no_run_ever_launched_is_all_awaiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path / "empty"))
        result = oa.read_run_status(tmp_path)
        assert result["run_id"] is None
        assert result["chain_map"]["state"] == "awaiting_artifact"
        assert result["exit_status"]["state"] == "awaiting_artifact"
        assert result["manifest"]["state"] == "awaiting_artifact"

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_run_status(fixture_env, "../../etc/passwd")


# ─────────────────────────── preflight_estimate ───────────────────────────


class TestPreflightEstimate:
    def test_awaiting_in_production_no_env(self, tmp_path: Path) -> None:
        result = oa.preflight_estimate(tmp_path, {"symbols": ["NIFTY"]})
        assert result["state"] == "awaiting_artifact"
        assert "P1" in result["reason"]

    def test_default_fixture_matches_generic_selection(self, fixture_env: Path) -> None:
        result = oa.preflight_estimate(
            fixture_env, {"symbols": ["NIFTY"], "intervals": ["5m"], "k": 3}
        )
        assert result["ok"] is True
        assert result["estimate_basis"] == "provisional"
        assert "estimator" in result

    def test_refusal_fixture_matches_banknifty_selection(self, fixture_env: Path) -> None:
        result = oa.preflight_estimate(fixture_env, {"symbols": ["BANKNIFTY"]})
        assert result["ok"] is False
        assert result["refusals"][0]["symbol"] == "BANKNIFTY"
        assert result["refusals"][0]["missing_spans"]

    def test_awaiting_when_fixture_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "no-preflight-fixtures-here"
        empty.mkdir()
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(empty / "sweep_runs"))
        result = oa.preflight_estimate(tmp_path, {"symbols": ["NIFTY"]})
        assert result["state"] == "awaiting_artifact"

    def test_match_key_selection_does_not_supply_falls_through_to_default(
        self, fixture_env: Path
    ) -> None:
        # "symbols" match key is present on refusal_missing_spans.json, but
        # the caller's selection doesn't carry "symbols" at all — falls
        # through to the default.json fallback instead of a false match.
        result = oa.preflight_estimate(fixture_env, {"intervals": ["5m"]})
        assert result["ok"] is True  # the default fixture, not the refusal one

    def test_non_dict_fixture_file_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_root = tmp_path / "fixtures"
        (fixture_root / "sweep_runs").mkdir(parents=True)
        pf_dir = fixture_root / "preflight_fixtures"
        pf_dir.mkdir()
        (pf_dir / "default.json").write_text(
            json.dumps({"match": {}, "ok": True, "refusals": [], "clamps": [], "estimator": {}}),
            encoding="utf-8",
        )
        (pf_dir / "aaa_not_an_object.json").write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(fixture_root / "sweep_runs"))
        result = oa.preflight_estimate(tmp_path, {"symbols": ["NIFTY"]})
        assert result["ok"] is True

    def test_awaiting_when_no_fixture_readable_and_no_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_root = tmp_path / "fixtures"
        (fixture_root / "sweep_runs").mkdir(parents=True)
        pf_dir = fixture_root / "preflight_fixtures"
        pf_dir.mkdir()
        (pf_dir / "only_corrupt.json").write_text("{not valid", encoding="utf-8")
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(fixture_root / "sweep_runs"))
        result = oa.preflight_estimate(tmp_path, {"symbols": ["NIFTY"]})
        assert result["state"] == "awaiting_artifact"

    def test_non_default_fixture_with_empty_match_block_matches_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-default.json fixture with its OWN empty "match" block (not
        # the reserved default.json name) is a legitimate wildcard fixture
        # too — exercises the `if not match: return True` branch of
        # _coarse_match via a real fixture file, distinct from default.json's
        # special-cased fallback handling.
        fixture_root = tmp_path / "fixtures"
        (fixture_root / "sweep_runs").mkdir(parents=True)
        pf_dir = fixture_root / "preflight_fixtures"
        pf_dir.mkdir()
        (pf_dir / "aaa_wildcard.json").write_text(
            json.dumps(
                {
                    "match": {},
                    "ok": True,
                    "refusals": [],
                    "clamps": [],
                    "estimator": {"trials": 1, "est_seconds_provisional": 1, "est_bytes": 1},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(fixture_root / "sweep_runs"))
        result = oa.preflight_estimate(tmp_path, {"symbols": ["ANYTHING"]})
        assert result["ok"] is True
        assert result["estimator"]["trials"] == 1

    def test_glob_oserror_is_awaiting_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture_root = tmp_path / "fixtures"
        (fixture_root / "sweep_runs").mkdir(parents=True)
        (fixture_root / "preflight_fixtures").mkdir()
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(fixture_root / "sweep_runs"))

        def _fake_glob(self: Path, pattern: str) -> object:
            raise OSError("glob denied")

        monkeypatch.setattr(Path, "glob", _fake_glob)
        result = oa.preflight_estimate(tmp_path, {"symbols": ["NIFTY"]})
        assert result["state"] == "awaiting_artifact"


# ─────────────────────────── read_estimator_calibration ───────────────────────────


class TestReadEstimatorCalibration:
    def test_ok_via_fixture_sibling(self, fixture_env: Path) -> None:
        result = oa.read_estimator_calibration(fixture_env)
        assert result["state"] == "ok"
        assert result["calibrated"] is True
        assert result["trials_per_second"] == 41200000

    def test_awaiting_when_absent_production(self, tmp_path: Path) -> None:
        result = oa.read_estimator_calibration(tmp_path)
        assert result["state"] == "awaiting_artifact"

    def test_ok_production_sibling_of_lake_root(self, tmp_path: Path) -> None:
        (tmp_path / "estimator_calibration.json").write_text(
            json.dumps(
                {
                    "calibrated": False,
                    "trials_per_second": None,
                    "last_run_id": None,
                    "updated_utc": None,
                }
            ),
            encoding="utf-8",
        )
        result = oa.read_estimator_calibration(tmp_path)
        assert result["state"] == "ok"
        assert result["calibrated"] is False

    def test_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / "estimator_calibration.json").write_text("{not valid", encoding="utf-8")
        result = oa.read_estimator_calibration(tmp_path)
        assert result["state"] == "artifact_error"

    def test_non_object_json_is_artifact_error(self, tmp_path: Path) -> None:
        (tmp_path / "estimator_calibration.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_estimator_calibration(tmp_path)
        assert result["state"] == "artifact_error"


# ─────────────────────────── iter_timeline ───────────────────────────


class TestIterTimeline:
    def test_ok_cursor_pagination_covers_every_record_once(self, fixture_env: Path) -> None:
        seen = []
        after = 0
        for _ in range(20):
            page = oa.iter_timeline(fixture_env, RUN_SMOKE, after_seq=after, limit=5)
            assert page["state"] == "ok"
            assert page["bounded_scan"] is False
            if not page["records"]:
                break
            seen.extend(page["records"])
            after = page["next_after_seq"]
        assert len(seen) == 33  # matches the generated fixture's line count
        kinds = {r["kind"] for r in seen}
        assert "RUN_START" in kinds
        assert "TUPLE_DONE" in kinds

    def test_default_after_seq_zero_includes_the_first_record(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_SMOKE, after_seq=0, limit=1)
        assert len(page["records"]) == 1
        assert page["records"][0]["kind"] == "RUN_START"

    def test_kind_filter(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_SMOKE, after_seq=0, kind="MILESTONE", limit=50)
        assert page["records"]
        assert all(r["kind"] == "MILESTONE" for r in page["records"])

    def test_torn_tail_is_skipped_silently(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_TORN, after_seq=0, limit=200)
        assert page["state"] == "ok"
        assert len(page["records"]) == 9  # 1 RUN_START + 8 TUPLE_DONE; torn final line dropped
        assert all(r["kind"] != "_unreadable_line" for r in page["records"])

    def test_mid_file_corrupt_line_is_loud_not_dropped(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_MID_CORRUPT, after_seq=0, limit=200)
        assert page["state"] == "ok"
        kinds = [r["kind"] for r in page["records"]]
        assert kinds == ["RUN_START", "_unreadable_line", "TUPLE_DONE", "RUN_END"]

    def test_q_search_sets_bounded_scan_true(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_SMOKE, q="MILESTONE", limit=10)
        assert page["bounded_scan"] is True
        assert page["records"]

    def test_q_search_finds_payload_text_too(self, fixture_env: Path) -> None:
        page = oa.iter_timeline(fixture_env, RUN_SMOKE, q="survivors", limit=10)
        assert page["bounded_scan"] is True
        assert page["records"]

    def test_awaiting_when_recorder_absent(self, fixture_env: Path) -> None:
        result = oa.iter_timeline(fixture_env, "run_no_recorder_here", after_seq=0)
        assert result["state"] == "awaiting_artifact"
        assert result["records"] == []
        assert result["bounded_scan"] is False

    def test_no_run_ever_launched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path / "nothing"))
        result = oa.iter_timeline(tmp_path, None)
        assert result["state"] == "awaiting_artifact"
        assert result["run_id"] is None

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.iter_timeline(fixture_env, "../../etc/passwd")

    def test_never_crashes_on_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_empty_recorder"
        run_dir.mkdir()
        (run_dir / "flight-recorder.jsonl").write_text("", encoding="utf-8")
        result = oa.iter_timeline(tmp_path, "run_empty_recorder")
        assert result["state"] == "ok"
        assert result["records"] == []
        assert result["next_after_seq"] is None

    def test_artifact_error_when_recorder_path_is_a_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_recorder_is_dir"
        (run_dir / "flight-recorder.jsonl").mkdir(parents=True)
        result = oa.iter_timeline(tmp_path, "run_recorder_is_dir")
        assert result["state"] == "artifact_error"
        assert result["records"] == []

    def test_blank_lines_are_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_blank_lines"
        run_dir.mkdir()
        rec = json.dumps(
            {
                "seq": 1,
                "prev_hash": "0" * 64,
                "ts_utc": "2026-07-08T00:00:00Z",
                "actor": "flight",
                "action": "RUN_START",
                "subject": "run_blank_lines",
                "payload": {},
                "entry_hash": "x" * 64,
            }
        )
        (run_dir / "flight-recorder.jsonl").write_text(f"{rec}\n   \n\n{rec}\n", encoding="utf-8")
        result = oa.iter_timeline(tmp_path, "run_blank_lines", after_seq=0, limit=200)
        assert result["state"] == "ok"
        assert len(result["records"]) == 2

    def test_non_object_json_line_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_scalar_line"
        run_dir.mkdir()
        rec = json.dumps(
            {
                "seq": 2,
                "prev_hash": "0" * 64,
                "ts_utc": "2026-07-08T00:00:01Z",
                "actor": "flight",
                "action": "RUN_END",
                "subject": "run_scalar_line",
                "payload": {},
                "entry_hash": "y" * 64,
            }
        )
        (run_dir / "flight-recorder.jsonl").write_text(f"42\n{rec}\n", encoding="utf-8")
        result = oa.iter_timeline(tmp_path, "run_scalar_line", after_seq=0, limit=200)
        assert result["state"] == "ok"
        assert [r["kind"] for r in result["records"]] == ["_unreadable_line", "RUN_END"]

    def test_bounded_scan_window_is_capped_by_line_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_many_lines"
        run_dir.mkdir()

        def _rec(seq: int) -> str:
            return json.dumps(
                {
                    "seq": seq,
                    "prev_hash": "0" * 64,
                    "ts_utc": "2026-07-08T00:00:00Z",
                    "actor": "flight",
                    "action": "TUPLE_DONE",
                    "subject": "run_many_lines",
                    "payload": {"needle": "findme"} if seq == 1 else {"done_cum": seq},
                    "entry_hash": "z" * 64,
                }
            )

        # More than _TIMELINE_SCAN_MAX_LINES lines so the bounded reverse
        # scan window genuinely truncates — seq=1 (containing the search
        # needle) sits OUTSIDE the scan window and must not be found.
        n = oa._TIMELINE_SCAN_MAX_LINES + 50
        lines = [_rec(i) for i in range(1, n + 1)]
        (run_dir / "flight-recorder.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = oa.iter_timeline(tmp_path, "run_many_lines", q="findme", limit=10)
        assert result["state"] == "ok"
        assert result["bounded_scan"] is True
        assert result["records"] == []  # the needle was scanned past the bound


# ─────────────────────────── dedupe_family_representatives ───────────────────────────


class TestDedupeFamilyRepresentatives:
    def test_rows_without_family_id_pass_through(self) -> None:
        rows = [{"rank": 1, "mask_hex": "1"}, {"rank": 2, "mask_hex": "3"}]
        assert oa.dedupe_family_representatives(rows) == rows

    def test_keeps_fewest_bit_member_per_family(self) -> None:
        rows = [
            {"rank": 1, "mask_hex": "7", "family_id": "f1"},  # 3 bits
            {"rank": 2, "mask_hex": "1", "family_id": "f1"},  # 1 bit — wins
            {"rank": 3, "mask_hex": "3", "family_id": "f1"},  # 2 bits
        ]
        result = oa.dedupe_family_representatives(rows)
        assert len(result) == 1
        assert result[0]["rank"] == 2

    def test_ties_broken_by_lower_rank(self) -> None:
        rows = [
            {"rank": 5, "mask_hex": "1", "family_id": "f1"},
            {"rank": 2, "mask_hex": "2", "family_id": "f1"},  # same bit count, lower rank wins
        ]
        result = oa.dedupe_family_representatives(rows)
        assert result[0]["rank"] == 2

    def test_output_preserves_first_seen_position(self) -> None:
        rows: list[dict[str, object]] = [
            {"rank": 1, "family_id": None},
            {"rank": 2, "mask_hex": "3", "family_id": "f1"},
            {"rank": 3, "family_id": None},
            {"rank": 4, "mask_hex": "1", "family_id": "f1"},  # wins f1, but stays at slot 1
        ]
        result = oa.dedupe_family_representatives(rows)
        ranks = [r["rank"] for r in result]
        assert ranks == [1, 4, 3]

    def test_pure_does_not_mutate_input(self) -> None:
        rows = [
            {"rank": 1, "mask_hex": "3", "family_id": "f1"},
            {"rank": 2, "mask_hex": "1", "family_id": "f1"},
        ]
        original = [dict(r) for r in rows]
        oa.dedupe_family_representatives(rows)
        assert rows == original

    def test_empty_input(self) -> None:
        assert oa.dedupe_family_representatives([]) == []

    def test_falsy_and_unparsable_mask_hex_both_count_as_zero_bits(self) -> None:
        rows = [
            {"rank": 1, "mask_hex": "not-hex", "family_id": "f1"},  # unparsable -> 0 bits
            {"rank": 2, "mask_hex": "", "family_id": "f1"},  # falsy -> 0 bits
            {"rank": 3, "mask_hex": "1", "family_id": "f1"},  # 1 bit — loses to the 0-bit rows
        ]
        result = oa.dedupe_family_representatives(rows)
        assert len(result) == 1
        assert result[0]["rank"] in (1, 2)  # tie broken by lower rank between the two 0-bit rows


# ─────────────────────────── read_top100 ───────────────────────────


class TestReadTop100:
    def test_ok_deduped_and_decorated(self, fixture_env: Path) -> None:
        result = oa.read_top100(fixture_env, RUN_SMOKE)
        assert result["state"] == "ok"
        assert len(result["rows"]) == 99  # 100 rows, one family collapsed
        assert all(r["rank"] != 23 for r in result["rows"])
        row = result["rows"][0]
        assert row["simple_sentence"]
        assert row["pro_sentence"]
        assert row["regime_label"]
        assert row["join_key_inferred"] is False
        assert row["instrument"] == "NIFTY"
        assert row["sweep_run_id"] == RUN_SMOKE

    def test_legacy_rows_backfilled_with_join_key_inferred(self, fixture_env: Path) -> None:
        result = oa.read_top100(fixture_env, RUN_LEGACY)
        assert result["state"] == "ok"
        for row in result["rows"]:
            assert row["join_key_inferred"] is True
            assert row["instrument"] == "—"
            assert row["mode"] == "—"
            assert row["combo_id"].startswith("legacy-")
            assert row["sweep_run_id"] == RUN_LEGACY

    def test_awaiting_when_top100_not_yet_written(self, fixture_env: Path) -> None:
        result = oa.read_top100(fixture_env, RUN_FULL)
        assert result["state"] == "awaiting_artifact"
        assert result["rows"] == []

    def test_corrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_bad_top100"
        run_dir.mkdir()
        (run_dir / "top100.json").write_text("[not valid", encoding="utf-8")
        result = oa.read_top100(tmp_path, "run_bad_top100")
        assert result["state"] == "artifact_error"
        assert result["rows"] == []

    def test_non_array_json_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_top100_wrong_shape"
        run_dir.mkdir()
        (run_dir / "top100.json").write_text('{"not": "an array"}', encoding="utf-8")
        result = oa.read_top100(tmp_path, "run_top100_wrong_shape")
        assert result["state"] == "artifact_error"

    def test_no_run_ever_launched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path / "nothing"))
        result = oa.read_top100(tmp_path)
        assert result["state"] == "awaiting_artifact"
        assert result["run_id"] is None

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_top100(fixture_env, "../../etc/passwd")

    def test_family_size_and_members_on_the_smoke_fixtures_one_collision(
        self, fixture_env: Path
    ) -> None:
        """R34 twin-detector drill-down: rank 7 / rank 23 share ``fam-0007``.

        The simpler (fewer-bit) row (rank 7) is the representative
        ``read_top100`` returns; its decorated row carries the OTHER family
        member (rank 23) in ``family_members`` so the operator can drill into
        the twin without a second request, while every non-colliding row's
        ``family_size`` is honestly 1 with an empty ``family_members`` list.
        """
        result = oa.read_top100(fixture_env, RUN_SMOKE)
        by_rank = {r["rank"]: r for r in result["rows"]}
        rep = by_rank[7]
        assert rep["family_size"] == 2
        assert [m["rank"] for m in rep["family_members"]] == [23]
        twin = rep["family_members"][0]
        assert twin["mask_hex"] == "88000948080000"
        assert "conditions" in twin

        # rank 23 itself never appears as its own top-level row (collapsed).
        assert 23 not in by_rank

        # A non-colliding row is honestly size-1 with no members to drill into.
        solo = by_rank[1]
        assert solo["family_size"] == 1
        assert solo["family_members"] == []

    def test_family_members_empty_when_family_id_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_no_family"
        run_dir.mkdir()
        (run_dir / "top100.json").write_text(
            json.dumps(
                [
                    {
                        "rank": 1,
                        "mask_hex": "1",
                        "conditions": [],
                        "tf": "5m",
                        "direction": "long",
                        "arm": "sl_tp",
                        "metrics": {},
                        "regime": {},
                        "vix_band": {},
                        "family_id": None,
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = oa.read_top100(tmp_path, "run_no_family")
        assert result["rows"][0]["family_size"] == 1
        assert result["rows"][0]["family_members"] == []

    def test_decode_combo_reused_matches_direct_call(self, fixture_env: Path) -> None:
        from brutex.dashboard.combo_decoder import decode_combo

        result = oa.read_top100(fixture_env, RUN_SMOKE)
        row = result["rows"][0]
        expected = decode_combo(int(row["mask_hex"], 16), row["direction"], row["tf"])
        assert row["simple_sentence"] == expected.simple_sentence
        assert row["pro_sentence"] == expected.pro_sentence
        assert row["regime_label"] == expected.regime_label

    def test_unparsable_mask_hex_falls_back_to_empty_mask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_bad_mask"
        run_dir.mkdir()
        (run_dir / "top100.json").write_text(
            json.dumps(
                [
                    {
                        "rank": 1,
                        "mask_hex": "not-hex-at-all",
                        "conditions": [],
                        "tf": "5m",
                        "direction": "long",
                        "arm": "sl_tp",
                        "metrics": {},
                        "regime": {},
                        "vix_band": {},
                        "family_id": None,
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = oa.read_top100(tmp_path, "run_bad_mask")
        assert result["state"] == "ok"
        assert result["rows"][0]["conditions"] == []
        assert "no conditions" in result["rows"][0]["simple_sentence"]


# ─────────────────────────── read_trades_page ───────────────────────────


class TestReadTradesPage:
    def test_ok_with_combo_explanation(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(
            fixture_env, RUN_SMOKE, 1, mask_hex="1", direction="long", tf="5m"
        )
        assert result["state"] == "ok"
        assert result["total"] == 12
        assert len(result["rows"]) == 12
        assert result["combo_explanation"]["simple_sentence"]
        row0 = result["rows"][0]
        assert row0["lots"] is None
        assert row0["costs_paisa"] is None

    def test_ok_without_combo_explanation_when_mask_missing(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_SMOKE, 1)
        assert result["state"] == "ok"
        assert result["combo_explanation"] is None

    def test_offset_and_limit_page(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_SMOKE, 1, offset=10, limit=5)
        assert result["offset"] == 10
        assert result["limit"] == 5
        assert len(result["rows"]) == 2  # only 12 total, 10..12

    def test_limit_clamped_server_side(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_SMOKE, 1, limit=999999)
        assert result["limit"] == 500

    def test_negative_offset_clamped(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_SMOKE, 1, offset=-5)
        assert result["offset"] == 0

    def test_awaiting_when_rank_not_covered(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_SMOKE, 99)
        assert result["state"] == "awaiting_artifact"
        assert result["rows"] == []

    def test_awaiting_in_production_even_if_run_exists(self, tmp_path: Path) -> None:
        # Production root (no BRUTEX_ONECLICK_FIXTURE_ROOT) — trades layout is
        # fixture-ONLY per the contract, so this must stay honestly awaiting.
        run_dir = tmp_path / "sweep_runs" / "run_prod"
        run_dir.mkdir(parents=True)
        (run_dir / "trades").mkdir()
        (run_dir / "trades" / "rank-0001.json").write_text(
            json.dumps({"rows": [], "total": 0}), encoding="utf-8"
        )
        result = oa.read_trades_page(tmp_path, "run_prod", 1)
        assert result["state"] == "awaiting_artifact"
        assert "fixture-only" in result["reason"]

    def test_corrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_bad_trades"
        (run_dir / "trades").mkdir(parents=True)
        (run_dir / "trades" / "rank-0001.json").write_text("{not valid", encoding="utf-8")
        result = oa.read_trades_page(tmp_path, "run_bad_trades", 1)
        assert result["state"] == "artifact_error"

    def test_no_run_ever_launched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path / "nothing"))
        result = oa.read_trades_page(tmp_path, None, 1)
        assert result["state"] == "awaiting_artifact"
        assert result["run_id"] is None

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_trades_page(fixture_env, "../../etc/passwd", 1)

    def test_demo_trades_carry_real_lots_and_costs(self, fixture_env: Path) -> None:
        result = oa.read_trades_page(fixture_env, RUN_DEMO, 1)
        assert result["state"] == "ok"
        assert result["rows"][0]["lots"] == 1
        assert result["rows"][0]["costs_paisa"] == 4200

    def test_non_object_json_trades_file_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_trades_array"
        (run_dir / "trades").mkdir(parents=True)
        (run_dir / "trades" / "rank-0001.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_trades_page(tmp_path, "run_trades_array", 1)
        assert result["state"] == "artifact_error"

    def test_rows_not_a_list_defaults_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_rows_not_list"
        (run_dir / "trades").mkdir(parents=True)
        (run_dir / "trades" / "rank-0001.json").write_text(
            json.dumps({"rows": "not-a-list", "total": 3}), encoding="utf-8"
        )
        result = oa.read_trades_page(tmp_path, "run_rows_not_list", 1)
        assert result["state"] == "ok"
        assert result["rows"] == []

    def test_total_not_an_int_falls_back_to_len_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_bad_total"
        (run_dir / "trades").mkdir(parents=True)
        (run_dir / "trades" / "rank-0001.json").write_text(
            json.dumps({"rows": [{"entry_ts": "x"}], "total": "not-an-int"}), encoding="utf-8"
        )
        result = oa.read_trades_page(tmp_path, "run_bad_total", 1)
        assert result["state"] == "ok"
        assert result["total"] == 1

    def test_unparsable_mask_hex_in_combo_explanation_falls_back_to_empty_mask(
        self, fixture_env: Path
    ) -> None:
        result = oa.read_trades_page(
            fixture_env, RUN_SMOKE, 1, mask_hex="not-hex", direction="long", tf="5m"
        )
        assert result["state"] == "ok"
        assert result["combo_explanation"]["conditions"] == []


# ─────────────────────────── read_verify_me ───────────────────────────


class TestReadVerifyMe:
    def test_all_awaiting_when_file_absent(self, fixture_env: Path) -> None:
        result = oa.read_verify_me(fixture_env, RUN_NO_VERIFYME)
        assert result["state"] == "ok"
        assert result["is_demo"] is False
        assert set(result["checks"]) == set(oa.VERIFY_ME_CHECKS)
        assert all(c["status"] == "awaiting" for c in result["checks"].values())

    def test_all_awaiting_when_no_run_ever_launched_matches_absent_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path / "nothing"))
        no_run = oa.read_verify_me(tmp_path)
        assert no_run["state"] == "ok"
        assert all(c["status"] == "awaiting" for c in no_run["checks"].values())

    def test_non_object_json_is_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_verify_array"
        run_dir.mkdir()
        (run_dir / "verify-me.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = oa.read_verify_me(tmp_path, "run_verify_array")
        assert result["state"] == "artifact_error"
        assert all(c["status"] == "awaiting" for c in result["checks"].values())

    def test_demo_all_pass_is_clearly_separated(self, fixture_env: Path) -> None:
        result = oa.read_verify_me(fixture_env, RUN_DEMO)
        assert result["state"] == "ok"
        assert result["is_demo"] is True
        assert all(c["status"] == "pass" for c in result["checks"].values())

    def test_honest_run_awaiting_even_when_complete(self, fixture_env: Path) -> None:
        # RUN_SMOKE is a COMPLETED run but verify-me.json still reads all
        # "awaiting" (P2 backend not merged) — the honest default persists
        # even post-sweep.
        result = oa.read_verify_me(fixture_env, RUN_SMOKE)
        assert result["is_demo"] is False
        assert all(c["status"] == "awaiting" for c in result["checks"].values())

    def test_corrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_bad_verify"
        run_dir.mkdir()
        (run_dir / "verify-me.json").write_text("{not valid", encoding="utf-8")
        result = oa.read_verify_me(tmp_path, "run_bad_verify")
        assert result["state"] == "artifact_error"
        assert all(c["status"] == "awaiting" for c in result["checks"].values())

    def test_unknown_status_in_file_falls_back_to_awaiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_weird_status"
        run_dir.mkdir()
        (run_dir / "verify-me.json").write_text(
            json.dumps({"checks": {"determinism": {"status": "maybe", "detail": "??"}}}),
            encoding="utf-8",
        )
        result = oa.read_verify_me(tmp_path, "run_weird_status")
        assert result["checks"]["determinism"]["status"] == "awaiting"

    def test_missing_checks_key_synthesizes_full_awaiting_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(tmp_path))
        run_dir = tmp_path / "run_partial_verify"
        run_dir.mkdir()
        (run_dir / "verify-me.json").write_text(json.dumps({"is_demo": False}), encoding="utf-8")
        result = oa.read_verify_me(tmp_path, "run_partial_verify")
        assert set(result["checks"]) == set(oa.VERIFY_ME_CHECKS)

    def test_rejects_path_traversal(self, fixture_env: Path) -> None:
        with pytest.raises(ValueError):
            oa.read_verify_me(fixture_env, "../../etc/passwd")


# ─────────────────────────── fixture-tree self-consistency ───────────────────────────


class TestFixtureTreeIsHonest:
    """Guards against the fixture generator silently drifting from the
    contract shapes it claims to demonstrate — these are cheap sanity pins,
    not adapter-behavior tests."""

    def test_hash_chain_is_actually_valid_on_the_clean_runs(self) -> None:
        import hashlib

        def canon(payload: object) -> str:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))

        for run_id in (RUN_SMOKE, RUN_DEMO, RUN_FULL):
            path = FIXTURE_SWEEP_RUNS / run_id / "flight-recorder.jsonl"
            prev = "0" * 64
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn tail, if any, is not this test's concern
                assert rec["prev_hash"] == prev
                digest = hashlib.sha256(
                    "|".join(
                        [
                            str(rec["seq"]),
                            rec["prev_hash"],
                            rec["ts_utc"],
                            rec["actor"],
                            rec["action"],
                            rec["subject"],
                            canon(rec["payload"]),
                        ]
                    ).encode("utf-8")
                ).hexdigest()
                assert digest == rec["entry_hash"]
                prev = rec["entry_hash"]

    def test_smoke_top100_has_exactly_one_family_collision(self) -> None:
        data = json.loads((FIXTURE_SWEEP_RUNS / RUN_SMOKE / "top100.json").read_text())
        assert len(data) == 100
        family_ids = [row["family_id"] for row in data]
        assert len(family_ids) != len(set(family_ids))  # at least one collision by construction

    def test_demo_run_has_one_trades_file_per_rank(self) -> None:
        trades_dir = FIXTURE_SWEEP_RUNS / RUN_DEMO / "trades"
        files = sorted(trades_dir.glob("rank-*.json"))
        assert len(files) == 100

    def test_empty_root_fixture_has_no_sweep_runs_child(self) -> None:
        assert FIXTURE_EMPTY_ROOT.is_dir()
        assert not (FIXTURE_EMPTY_ROOT / "sweep_runs").exists()
