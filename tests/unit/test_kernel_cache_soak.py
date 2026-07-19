"""Unit coverage of scripts/ci/kernel_cache_soak.py's pure logic.

DEC-SWEEP-WORKER-KERNEL-CACHE-SOAK-001. The script is loaded standalone (the
``spec_from_file_location`` sibling-contract idiom of
``test_scripts_bench_baseline_gate.py``) so every pure helper — ANSI-stripped
structlog parsing, telemetry harvest, ps-snapshot folding, the ts-masked
result-tree/audit identity law, delta assembly, the acceptance truth table
(real AND smoke arms), summary rendering — is verified without an M4-Pro or
a real lake. The end-to-end ``run --smoke`` mechanics proof lives in
``tests/integration/test_kernel_cache_soak_smoke.py``; the ``verdict``
subcommand's exit-code contract is driven via subprocess here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "kernel_cache_soak.py"


def load_script() -> ModuleType:
    """Standalone-load the soak script (the sibling-contract idiom)."""
    spec = importlib.util.spec_from_file_location("kernel_cache_soak_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SOAK = load_script()

#: A REAL structlog ConsoleRenderer(colors=True) stderr line captured from a
#: sandbox `brutex sweep-all` run — the exact ANSI shape the harvester parses.
_SUMMARY_LINE = (
    "\x1b[2m2026-07-19T11:41:17.325512Z\x1b[0m [\x1b[32m\x1b[1minfo     \x1b[0m] "
    "\x1b[1msweep_parallel.kernel_reuse_summary\x1b[0m "
    "\x1b[36mbuilds_total\x1b[0m=\x1b[35m1\x1b[0m "
    "\x1b[36mhits_total\x1b[0m=\x1b[35m3\x1b[0m "
    "\x1b[36mrun_id\x1b[0m=\x1b[35m2b53752ea646\x1b[0m "
    "\x1b[36mworkers\x1b[0m=\x1b[35m1\x1b[0m"
)
#: The clean structural exit-status shape (compare_arms input in tree tests).
_CLEAN_STATUS = {"state": "complete", "exit_code": 0, "suspect": False}

_BUILT_LINE = (
    "\x1b[2m2026-07-19T11:41:12.000000Z\x1b[0m [\x1b[32m\x1b[1minfo     \x1b[0m] "
    "\x1b[1msweep_parallel.worker_kernel_built\x1b[0m "
    "\x1b[36mworker_pid\x1b[0m=\x1b[35m22901\x1b[0m"
)


def _arm_env(
    base: dict[str, str] | None = None,
    *,
    cache_on: bool,
    results_depth_off: bool,
    workers: int = 2,
    capacity: int = 1,
    mem_budget_bytes: int = 1_000_000,
    mirror_root: Path | None = None,
) -> dict[str, str]:
    """``arm_env`` with defaulted fix-round-3 sizing/mirror kwargs (brevity)."""
    env: dict[str, str] = SOAK.arm_env(
        {} if base is None else base,
        cache_on=cache_on,
        workers=workers,
        results_depth_off=results_depth_off,
        capacity=capacity,
        mem_budget_bytes=mem_budget_bytes,
        mirror_root=Path("/out/arm/mirror") if mirror_root is None else mirror_root,
    )
    return env


class TestLogParsing:
    def test_strip_ansi_removes_sgr_escapes(self) -> None:
        clean = SOAK.strip_ansi(_SUMMARY_LINE)
        assert "\x1b" not in clean
        assert "sweep_parallel.kernel_reuse_summary" in clean
        assert "builds_total=1" in clean

    def test_parse_structlog_kv_extracts_pairs(self) -> None:
        kv = SOAK.parse_structlog_kv(_SUMMARY_LINE, SOAK.SUMMARY_EVENT)
        assert kv is not None
        assert kv["builds_total"] == "1"
        assert kv["hits_total"] == "3"
        assert kv["workers"] == "1"

    def test_parse_structlog_kv_none_for_other_events(self) -> None:
        assert SOAK.parse_structlog_kv(_BUILT_LINE, SOAK.SUMMARY_EVENT) is None
        assert SOAK.parse_structlog_kv("plain text line", SOAK.SUMMARY_EVENT) is None

    def test_harvest_telemetry_counts_and_aborts(self) -> None:
        log = "\n".join(
            [
                "unrelated line",
                _BUILT_LINE,
                _SUMMARY_LINE,
                _BUILT_LINE,
                "raise SweepStallError(...)",
            ]
        )
        telemetry = SOAK.harvest_telemetry(log)
        assert len(telemetry.summaries) == 1
        assert telemetry.summaries[0] == {"builds_total": 1, "hits_total": 3, "workers": 1}
        assert telemetry.built_events == 2
        assert telemetry.abort_tokens == ("SweepStallError",)

    def test_harvest_telemetry_malformed_summary_is_visibly_wrong(self) -> None:
        line = "2026-07-19T00:00:00Z [info] sweep_parallel.kernel_reuse_summary builds_total=x"
        telemetry = SOAK.harvest_telemetry(line)
        assert telemetry.summaries[0]["builds_total"] == -1  # never silently absent
        assert telemetry.summaries[0]["hits_total"] == -1

    def test_harvest_telemetry_clean_log(self) -> None:
        telemetry = SOAK.harvest_telemetry("nothing here\n")
        assert telemetry.summaries == ()
        assert telemetry.built_events == 0
        assert telemetry.abort_tokens == ()


class TestPsFolding:
    def test_parse_ps_lines_skips_malformed(self) -> None:
        text = " 10  1 1000\n 20 10 2000\ngarbage row\n 30 10 not-a-number\n 40\n"
        assert SOAK.parse_ps_lines(text) == [(10, 1, 1000), (20, 10, 2000)]

    def test_descendant_pids_transitive(self) -> None:
        rows = [(10, 1, 0), (20, 10, 0), (30, 20, 0), (99, 1, 0)]
        assert SOAK.descendant_pids(rows, 10) == {10, 20, 30}

    def test_rss_tracker_fold_peaks_and_pool(self) -> None:
        tracker = SOAK.RssTracker(root_pid=10)
        # Sample 1: root 1000 + workers 20/30 → pool 1000+500+700.
        tracker.fold([(10, 1, 1000), (20, 10, 500), (30, 10, 700), (99, 1, 9999)])
        # Sample 2: worker 20 spikes past the sample-1 pool sum; worker 30 gone.
        tracker.fold([(10, 1, 900), (20, 10, 1500)])
        assert tracker.samples == 2
        assert tracker.per_pid_max_kib == {10: 1000, 20: 1500, 30: 700}
        assert tracker.pool_peak_kib == 900 + 1500  # max over per-sample sums
        assert tracker.worker_peaks_kib() == {20: 1500, 30: 700}
        assert tracker.worker_peak_max_kib() == 1500

    def test_rss_tracker_empty_sample_not_counted(self) -> None:
        tracker = SOAK.RssTracker(root_pid=10)
        tracker.fold([(99, 1, 100)])  # arm tree not visible (already exited)
        assert tracker.samples == 0
        assert tracker.pool_peak_kib == 0
        assert tracker.worker_peak_max_kib() is None

    def test_maxrss_to_kib_platform_units(self) -> None:
        assert SOAK.maxrss_to_kib(1024 * 500, platform="darwin") == 500  # bytes → KiB
        assert SOAK.maxrss_to_kib(500, platform="linux") == 500  # already KiB


class TestIdentityLaw:
    def test_result_tree_exclusions_and_diff(self, tmp_path: Path) -> None:
        root = tmp_path / "results"
        (root / "NIFTY" / "1minute").mkdir(parents=True)
        (root / "NIFTY" / "1minute" / "store.parquet").write_bytes(b"result-bytes")
        (root / "runs" / "abc").mkdir(parents=True)
        (root / "runs" / "abc" / "exit-status.json").write_text("{}")
        (root / "audit").mkdir()
        (root / "audit" / "sweep-all.jsonl").write_text("chain")
        (root / "checkpoint").mkdir()
        (root / "checkpoint" / "sweep-all.json").write_text("{}")
        (root / "cascade").mkdir()
        (root / "cascade" / "manifest.json").write_text("{}")
        (root / "cascade" / "manifest.json.stamp.json").write_text("{}")
        (root / "stage2").write_text("wall-stamped sentinel")
        tree = SOAK.result_tree(root)
        assert set(tree) == {"NIFTY/1minute/store.parquet", "cascade/manifest.json"}

        other = dict(tree)
        other["NIFTY/1minute/store.parquet"] = "deadbeef"
        other["extra.parquet"] = "cafe"
        assert SOAK.tree_diff(tree, other) == ["NIFTY/1minute/store.parquet", "extra.parquet"]
        assert SOAK.tree_diff(tree, dict(tree)) == []

    def _arm_dir(self, root: Path, name: str, top100: bytes) -> Path:
        """A minimal arm dir: results/ store + a results-depth sweep_runs/ tree."""
        arm = root / name
        (arm / "results" / "NIFTY" / "1minute").mkdir(parents=True)
        (arm / "results" / "NIFTY" / "1minute" / "store.parquet").write_bytes(b"result-bytes")
        run_dir = arm / "sweep_runs" / "cafe1234"
        (run_dir / "results-depth").mkdir(parents=True)
        (run_dir / "top100.json").write_bytes(top100)
        (run_dir / "results-depth" / "trades.parquet").write_bytes(b"sidecar-bytes")
        # Wall-stamped sidecars differ per arm BY DESIGN — masked by the law.
        (run_dir / "results-depth" / "trades.parquet.stamp.json").write_text(
            f"stamp-{name}", encoding="utf-8"
        )
        return arm

    def test_arm_identity_tree_covers_sweep_runs(self, tmp_path: Path) -> None:
        # Fix-round-2 MAJOR (refuter-2): the results-depth sweep_runs/ sibling
        # root is INSIDE the identity compare — identical trees (modulo
        # .stamp.json) pass, a real top100.json divergence is a named diff.
        off = self._arm_dir(tmp_path, "arm-off", b'{"entries": []}')
        on = self._arm_dir(tmp_path, "arm-on", b'{"entries": []}')
        off_tree = SOAK.arm_identity_tree(off)
        assert set(off_tree) == {
            "results/NIFTY/1minute/store.parquet",
            "sweep_runs/cafe1234/top100.json",
            "sweep_runs/cafe1234/results-depth/trades.parquet",
        }
        assert SOAK.tree_diff(off_tree, SOAK.arm_identity_tree(on)) == []

        compare = SOAK.compare_arms(
            off, on, {"exit_status": _CLEAN_STATUS}, {"exit_status": _CLEAN_STATUS}
        )
        assert compare["identity_ok"] is True
        assert compare["sweep_runs_files_compared"] == 2

        # A REAL divergence in the flip-facing artifact is caught + named.
        (on / "sweep_runs" / "cafe1234" / "top100.json").write_bytes(b'{"entries": [1]}')
        compare = SOAK.compare_arms(
            off, on, {"exit_status": _CLEAN_STATUS}, {"exit_status": _CLEAN_STATUS}
        )
        assert compare["identity_ok"] is False
        assert "sweep_runs/cafe1234/top100.json" in compare["diff_paths"]

    def test_sweep_runs_tree_absent_dir_is_empty(self, tmp_path: Path) -> None:
        # Smoke runs results-depth OFF → no sweep_runs/ dir → honest {}.
        assert SOAK.sweep_runs_tree(tmp_path) == {}

    def test_scrub_manifest_drops_volatile_keys_only(self) -> None:
        manifest = {
            "run_id": "abc",
            "actual": {"tuples_done": 4},
            "updated_utc": 1.0,
            "started_utc": 2.0,
            "per_stage_timings": {"sweep": 1.5},
        }
        assert SOAK.scrub_manifest(manifest) == {"run_id": "abc", "actual": {"tuples_done": 4}}
        assert SOAK.scrub_manifest("not-a-dict") == "not-a-dict"

    def test_masked_audit_entries_equal_across_volatile_manifests(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import AuditWriter

        def write_chain(path: Path, timing: float) -> None:
            with AuditWriter.open(path) as audit:
                audit.append(action="SWEEP_START", subject="grid", payload={"tuples": 4})
                audit.append(
                    action="RUN_MANIFEST",
                    subject="grid",
                    payload={
                        "manifest": {
                            "run_id": "grid",
                            "per_stage_timings": {"sweep": timing},
                            "updated_utc": timing * 7,
                        },
                        "manifest_content_hash": f"hash-{timing}",
                    },
                )

        write_chain(tmp_path / "off.jsonl", 1.25)
        write_chain(tmp_path / "on.jsonl", 9.75)
        off = SOAK.masked_audit_entries(tmp_path / "off.jsonl")
        on = SOAK.masked_audit_entries(tmp_path / "on.jsonl")
        assert off == on  # volatile manifest keys + hash masked
        assert off[0][1] == "SWEEP_START"
        assert off[1][3] == {
            "manifest": {"run_id": "grid"},
            "manifest_content_hash": "<masked-volatile>",
        }

    def test_masked_audit_entries_detect_real_divergence(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import AuditWriter

        for name, tuples in (("a.jsonl", 4), ("b.jsonl", 5)):
            with AuditWriter.open(tmp_path / name) as audit:
                audit.append(action="SWEEP_START", subject="grid", payload={"tuples": tuples})
        assert SOAK.masked_audit_entries(tmp_path / "a.jsonl") != SOAK.masked_audit_entries(
            tmp_path / "b.jsonl"
        )

    def test_exit_status_structural(self) -> None:
        clean = {"state": "complete", "exit_code": 0, "suspect": False}
        ok, detail = SOAK.exit_status_structural(dict(clean), dict(clean))
        assert ok
        assert "clean" in detail
        ok, detail = SOAK.exit_status_structural(clean, {**clean, "exit_code": 3})
        assert not ok
        assert "diverged" in detail
        dirty = {"state": "suspect", "exit_code": 0, "suspect": True}
        ok, detail = SOAK.exit_status_structural(dict(dirty), dict(dirty))
        assert not ok
        assert "not clean" in detail


def _arm(**overrides: Any) -> dict[str, Any]:
    """A clean measured arm result (the ON shape); override per test."""
    base: dict[str, Any] = {
        "rc": 0,
        "timed_out": False,
        "wall_s": 100.0,
        "worker_peak_rss_kib_max": 700_000,
        "pool_peak_rss_kib": 5_000_000,
        "parent_peak_rss_kib": 400_000,
        "rss_samples": 20,
        "kernel_reuse_summaries": 1,
        "builds_total": 6,
        "hits_total": 42,
        "workers": 7,
        "worker_kernel_built_events": 6,
        "abort_tokens": [],
        "exit_state": "complete",
        "suspect": False,
        "quarantine_present": False,
        "tuples_done": 48,
        "exit_status": {"state": "complete", "exit_code": 0, "suspect": False},
    }
    base.update(overrides)
    return base


def _off_arm(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kernel_reuse_summaries": 0,
        "builds_total": None,
        "hits_total": None,
        "workers": None,
        "worker_kernel_built_events": 0,
        "worker_peak_rss_kib_max": 600_000,
        "pool_peak_rss_kib": 4_500_000,
    }
    base.update(overrides)
    return _arm(**base)


def _compare(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "identity_ok": True,
        "result_files_compared": 12,
        "diff_paths": [],
        "audit_ts_masked_ok": True,
        "audit_detail": "13 entries ts-masked equal",
        "exit_status_structural_ok": True,
        "exit_status_detail": "state/exit_code/suspect equal + clean",
        "excluded_sidecars": list(SOAK.STRUCTURAL_SIDECARS),
    }
    base.update(overrides)
    return base


#: A hardware key carrying the registered §8 M4-Pro signature.
_M4PRO_KEY = "mac16-8-14c10p4e-48g-arm64"


def _evaluate(
    off: dict[str, Any] | None = None,
    on: dict[str, Any] | None = None,
    compare: dict[str, Any] | None = None,
    *,
    keys: int = 6,
    smoke: bool = False,
    hardware_key: str | None = _M4PRO_KEY,
    hardware_key_source: str | None = "probed",
) -> dict[str, str]:
    off = _off_arm() if off is None else off
    on = _arm() if on is None else on
    compare = _compare() if compare is None else compare
    result: dict[str, str] = SOAK.evaluate_acceptance(
        off=off,
        on=on,
        compare=compare,
        deltas=SOAK.build_deltas(off, on),
        keys=keys,
        smoke=smoke,
        hardware_key=hardware_key,
        hardware_key_source=hardware_key_source,
    )
    return result


class TestDeltasAndAcceptance:
    def test_build_deltas_numbers(self) -> None:
        deltas = SOAK.build_deltas(_off_arm(), _arm())
        assert deltas["rss_per_worker_kib"] == 100_000
        assert deltas["rss_pool_kib"] == 500_000
        assert deltas["wall_delta_s"] == pytest.approx(0.0)
        assert deltas["wall_ratio_on_over_off"] == pytest.approx(1.0)

    def test_build_deltas_unmeasured_propagates_none(self) -> None:
        deltas = SOAK.build_deltas(
            _off_arm(worker_peak_rss_kib_max=None, pool_peak_rss_kib=None, wall_s=0), _arm()
        )
        assert deltas["rss_per_worker_kib"] is None
        assert deltas["rss_pool_kib"] is None
        assert deltas["wall_ratio_on_over_off"] is None  # zero-wall OFF never divides

    def test_all_green_real_run_is_flip_ready(self) -> None:
        acceptance = _evaluate()
        assert acceptance == {
            "identity": "PASS",
            "clean_exit": "PASS",
            "rss_envelope": "PASS",
            "wall_neutral": "PASS",
            "builds_bound": "PASS",
            "telemetry": "PASS",
            "provenance": "PASS",
            "verdict": "FLIP-READY",
        }
        assert SOAK.judged_failures(acceptance) == []

    def test_non_m4pro_hardware_is_never_flip_ready(self) -> None:
        # R2 MINOR-4: bars all green on a laptop → NOT-DECISION-GRADE, not a
        # judged FAILURE (rc stays 0 — honest lab evidence, not a harness bug).
        acceptance = _evaluate(hardware_key="pc-8c8p0e-32g-x86-64")
        assert acceptance["provenance"].startswith("NOT-DECISION-GRADE")
        assert "14c10p4e-48g-arm64" in acceptance["provenance"]
        assert acceptance["verdict"].startswith("NOT-DECISION-GRADE")
        assert "FLIP-READY" not in acceptance["verdict"]
        assert SOAK.judged_failures(acceptance) == []

    def test_unavailable_hardware_key_is_never_flip_ready(self) -> None:
        acceptance = _evaluate(hardware_key=None)
        assert acceptance["provenance"].startswith("NOT-DECISION-GRADE")
        assert "FLIP-READY" not in acceptance["verdict"]

    def test_judged_failure_outranks_not_decision_grade(self) -> None:
        acceptance = _evaluate(
            compare=_compare(identity_ok=False), hardware_key="pc-8c8p0e-32g-x86-64"
        )
        assert acceptance["verdict"] == "NOT-READY(identity)"

    def test_identity_diff_blocks(self) -> None:
        acceptance = _evaluate(compare=_compare(identity_ok=False, diff_paths=["a", "b"]))
        assert acceptance["identity"].startswith("FAIL")
        assert "2 result-tree diffs" in acceptance["identity"]
        assert acceptance["verdict"] == "NOT-READY(identity)"

    def test_audit_and_status_divergence_block(self) -> None:
        acceptance = _evaluate(
            compare=_compare(
                audit_ts_masked_ok=False,
                exit_status_structural_ok=False,
                exit_status_detail="exit-status diverged",
            )
        )
        assert "audit chains diverge ts-masked" in acceptance["identity"]
        assert "exit-status diverged" in acceptance["identity"]

    @pytest.mark.parametrize(
        ("overrides", "token"),
        [
            ({"rc": 3}, "rc=3"),
            ({"timed_out": True}, "timed out"),
            ({"exit_state": "suspect"}, "exit_state='suspect'"),
            ({"suspect": True}, "SUSPECT"),
            ({"quarantine_present": True}, ".sweep_quarantine.json present"),
            ({"abort_tokens": ["SweepStallError"]}, "SweepStallError"),
        ],
    )
    def test_clean_exit_failure_shapes(self, overrides: dict[str, Any], token: str) -> None:
        acceptance = _evaluate(on=_arm(**overrides))
        assert acceptance["clean_exit"].startswith("FAIL")
        assert token in acceptance["clean_exit"]
        assert acceptance["verdict"] == "NOT-READY(clean_exit)"

    def test_rss_per_worker_over_ceiling_fails(self) -> None:
        on = _arm(worker_peak_rss_kib_max=600_000 + SOAK.RSS_PER_WORKER_DELTA_CEILING_KIB + 1)
        acceptance = _evaluate(on=on)
        assert acceptance["rss_envelope"].startswith("FAIL")
        assert "per-worker delta" in acceptance["rss_envelope"]

    def test_pool_peak_over_ceiling_fails(self) -> None:
        on = _arm(pool_peak_rss_kib=SOAK.POOL_PEAK_CEILING_KIB + 1)
        acceptance = _evaluate(on=on)
        assert acceptance["rss_envelope"].startswith("FAIL")
        assert "pool peak" in acceptance["rss_envelope"]

    def test_rss_unmeasurable_fails_loud(self) -> None:
        acceptance = _evaluate(on=_arm(worker_peak_rss_kib_max=None, pool_peak_rss_kib=None))
        assert "unmeasurable" in acceptance["rss_envelope"]

    def test_wall_regression_fails_but_win_is_not_required(self) -> None:
        # 1.021 > the 1.02 no-regression band → FAIL.
        assert _evaluate(on=_arm(wall_s=102.1))["wall_neutral"].startswith("FAIL")
        # Exactly at the band → PASS (no-regression band, never a win gate).
        assert _evaluate(on=_arm(wall_s=102.0))["wall_neutral"] == "PASS"
        # A big win also just PASSes — the VALUE section carries the figure.
        assert _evaluate(on=_arm(wall_s=50.0))["wall_neutral"] == "PASS"

    def test_env_override_hardware_key_is_never_flip_ready(self) -> None:
        # Fix-round-2 (refuter-1 spoof): a forged BRUTEX_BENCH_BASELINE_KEY
        # carries the M4 signature on ANY box — an override-sourced key must
        # refuse even when the key text matches.
        acceptance = _evaluate(
            hardware_key=_M4PRO_KEY,
            hardware_key_source=f"env-override:{SOAK.BENCH_BASELINE_KEY_ENV}",
        )
        assert acceptance["provenance"].startswith("NOT-DECISION-GRADE")
        assert SOAK.BENCH_BASELINE_KEY_ENV in acceptance["provenance"]
        assert acceptance["verdict"].startswith("NOT-DECISION-GRADE")
        assert SOAK.judged_failures(acceptance) == []  # honest lab evidence, rc 0
        # A probed key with the signature stays PASS (the happy path).
        assert _evaluate()["provenance"] == "PASS"

    def test_verdict_time_override_attributes_the_live_env_not_the_stored_key(self) -> None:
        # Fix-round-3 NIT (refuter-A wording): the belt's distinct source
        # token yields wording that blames the LIVE verdict-time override —
        # the stored key was genuinely probed and must not be mis-attributed.
        acceptance = _evaluate(
            hardware_key=_M4PRO_KEY,
            hardware_key_source=f"env-override-at-verdict:{SOAK.BENCH_BASELINE_KEY_ENV}",
        )
        bar = acceptance["provenance"]
        assert bar.startswith("NOT-DECISION-GRADE")
        assert "set at verdict time" in bar
        assert "probed at run time" in bar
        assert "comes from the" not in bar  # the run-time wording never fires here
        assert acceptance["verdict"].startswith("NOT-DECISION-GRADE")
        assert SOAK.judged_failures(acceptance) == []

    def test_telemetry_off_arm_must_be_silent(self) -> None:
        acceptance = _evaluate(off=_off_arm(kernel_reuse_summaries=1))
        assert acceptance["telemetry"].startswith("FAIL")
        assert "OFF arm emitted 1" in acceptance["telemetry"]

    def test_telemetry_on_arm_needs_exactly_one_summary(self) -> None:
        acceptance = _evaluate(on=_arm(kernel_reuse_summaries=0))
        assert acceptance["telemetry"].startswith("FAIL")
        assert acceptance["builds_bound"] == "ABSTAIN(no ON summary)"
        assert "builds_bound" not in acceptance["verdict"]  # ABSTAIN is not judged FAIL

    def test_telemetry_sum_invariant(self) -> None:
        acceptance = _evaluate(on=_arm(builds_total=6, hits_total=41))  # 47 != 48
        assert "builds(6) + hits(41) != tuples_done(48)" in acceptance["telemetry"]

    def test_telemetry_malformed_figures(self) -> None:
        acceptance = _evaluate(on=_arm(tuples_done=None))
        assert "malformed" in acceptance["telemetry"]
        assert acceptance["builds_bound"] == "ABSTAIN(malformed figures)"

    def test_builds_bound_exceeded(self) -> None:
        # keys=6 × workers=7 = 42 < builds=43.
        acceptance = _evaluate(on=_arm(builds_total=43, hits_total=5, tuples_done=48))
        assert acceptance["builds_bound"].startswith("FAIL")
        assert "keys(6)" in acceptance["builds_bound"]

    def test_smoke_mode_unjudges_rss_and_wall(self) -> None:
        # A smoke arm wildly over every ceiling must still be mechanics-green
        # even on non-M4-Pro hardware (the sandbox) — smoke is unaffected by
        # the R2 MINOR-4 provenance bar.
        on = _arm(wall_s=500.0, worker_peak_rss_kib_max=10**9, pool_peak_rss_kib=10**9)
        acceptance = _evaluate(on=on, smoke=True, hardware_key="sandbox-4c4p0e-8g-x86-64")
        assert acceptance["rss_envelope"] == "SMOKE-UNJUDGED"
        assert acceptance["wall_neutral"] == "SMOKE-UNJUDGED"
        assert acceptance["provenance"] == "SMOKE-UNJUDGED"
        assert acceptance["verdict"] == "SMOKE-ONLY(mechanics green — NOT decision-grade)"
        assert SOAK.judged_failures(acceptance) == []

    def test_smoke_mode_still_judges_mechanics(self) -> None:
        acceptance = _evaluate(compare=_compare(identity_ok=False), smoke=True)
        assert acceptance["verdict"] == "SMOKE-FAILED(identity)"
        assert SOAK.judged_failures(acceptance) == ["identity"]

    def test_smoke_can_never_read_flip_ready(self) -> None:
        assert "FLIP-READY" not in _evaluate(smoke=True)["verdict"]


class TestGridHelpers:
    def test_keys_fan_out(self) -> None:
        assert (
            SOAK.keys_fan_out(
                "NIFTY,BANKNIFTY", "1minute,5minute,15minute", "prev_day", "hlc3:close"
            )
            == 6
        )
        assert SOAK.keys_fan_out("NIFTY", "1minute", "prev_day,rolling_weekly", "hlc3:close") == 2
        assert SOAK.keys_fan_out("NIFTY, ", "1minute", "prev_day", "hlc3:close") == 1

    def test_build_sweep_argv(self) -> None:
        argv = SOAK.build_sweep_argv(
            "python3",
            lake_root=Path("/lake"),
            results_root=Path("/out/results"),
            grid={
                "symbols": "NIFTY",
                "intervals": "1minute",
                "start": "2026-01-05",
                "end": "2026-01-17",
                "k": "1,2",
                "conditions": None,  # None → omitted
                "max_tuples": "500",
            },
            no_options_sim=True,
        )
        assert argv[:4] == ["python3", "-m", "brutex.cli", "sweep-all"]
        assert argv[4:6] == ["--lake-root", "/lake"]
        assert "--max-tuples" in argv  # underscore → dash mapping
        assert "--conditions" not in argv
        assert argv[-1] == "--no-options-sim"

    def test_arm_env(self) -> None:
        env = SOAK.arm_env(
            {"PATH": "/bin"},
            cache_on=True,
            workers=8,
            results_depth_off=True,
            capacity=1,
            mem_budget_bytes=123_456_789,
            mirror_root=Path("/out/arm-on/mirror"),
        )
        assert env[SOAK.KERNEL_CACHE_ENV] == "1"
        assert env[SOAK.SWEEP_WORKERS_ENV] == "8"
        assert env[SOAK.LOG_LEVEL_ENV] == "INFO"  # telemetry must reach stderr
        assert env["PYTHONHASHSEED"] == "0"
        assert env[SOAK.RESULTS_DEPTH_ENV] == "0"
        # Fix-round-3 MINOR-1 + NIT: sizing + mirror explicitly pinned too.
        assert env[SOAK.MEM_BUDGET_ENV] == "123456789"
        assert env[SOAK.RUN_MIRROR_ROOT_ENV] == "/out/arm-on/mirror"
        assert env["PATH"] == "/bin"
        off = _arm_env(cache_on=False, results_depth_off=False)
        assert off[SOAK.KERNEL_CACHE_ENV] == "0"
        # Fix-round-3 MINOR-1: SWEEP_WORKERS is now ALWAYS pinned (a dotenv
        # value previously re-scoped the absolute pool bar with zero echo,
        # and a dotenv =1 could bypass the sequential refusal).
        assert off[SOAK.SWEEP_WORKERS_ENV] == "2"
        # Fix-round-2 MAJOR-1: non-smoke EXPLICITLY sets =1 — absence would
        # let a dotenv BRUTEX_RESULTS_DEPTH=0 silently skip the results-depth
        # artifacts the identity compare covers.
        assert off[SOAK.RESULTS_DEPTH_ENV] == "1"

    def test_arm_env_pins_capacity_over_ambient(self) -> None:
        # Fix-round-2 MAJOR-1: the capacity is explicitly PINNED (never
        # popped) — an ambient operator override is overridden by the pin,
        # and the pin survives the child's dotenv re-read (see the dotenv
        # regressions below). The soak measures the DEFAULT sizing law.
        base = {"PATH": "/bin", SOAK.KERNEL_CAPACITY_ENV: "4"}
        for cache_on in (False, True):
            env = _arm_env(base, cache_on=cache_on, results_depth_off=False, capacity=2)
            assert env[SOAK.KERNEL_CAPACITY_ENV] == "2"
        assert base[SOAK.KERNEL_CAPACITY_ENV] == "4"  # caller's map untouched

    def test_arm_env_pins_sizing_over_ambient(self) -> None:
        # Fix-round-3 MINOR-1 (refuter-A): ambient sizing values — a stray
        # exported budget/worker count — are overridden by the explicit pins,
        # so the absolute RSS/pool bars are measured at KNOWN sizing.
        base = {
            SOAK.MEM_BUDGET_ENV: "536870912",
            SOAK.SWEEP_WORKERS_ENV: "3",
            SOAK.RUN_MIRROR_ROOT_ENV: "/tmp/shared-mirror",
        }
        env = _arm_env(
            base,
            cache_on=True,
            results_depth_off=False,
            workers=8,
            mem_budget_bytes=42,
            mirror_root=Path("/out/arm-on/mirror"),
        )
        assert env[SOAK.MEM_BUDGET_ENV] == "42"
        assert env[SOAK.SWEEP_WORKERS_ENV] == "8"
        assert env[SOAK.RUN_MIRROR_ROOT_ENV] == "/out/arm-on/mirror"
        assert base[SOAK.MEM_BUDGET_ENV] == "536870912"  # caller's map untouched

    def test_arm_env_dotenv_cannot_override_pinned_envs(self) -> None:
        # Fix-round-2 MAJOR-1 (refuter-1): the child `brutex sweep-all` runs
        # bootstrap_env(), whose apply_dotenv sets a .env key ONLY when it is
        # not already present — so every arm-identity env must be SET, never
        # merely absent. Drive the REAL apply_dotenv over an arm env with a
        # hostile .env payload: nothing may change.
        from brutex.config.dotenv import apply_dotenv

        env = _arm_env({"PATH": "/bin"}, cache_on=False, results_depth_off=False)
        hostile = {
            SOAK.KERNEL_CAPACITY_ENV: "9",
            SOAK.KERNEL_CACHE_ENV: "1",  # would silently flip the OFF arm
            SOAK.RESULTS_DEPTH_ENV: "0",
            SOAK.LOG_LEVEL_ENV: "ERROR",  # would silence the telemetry
            # Fix-round-3 MINOR-1 (refuter-A's repro payload): sizing envs
            # would re-scope the ABSOLUTE RSS/pool bars; the mirror env would
            # redirect the per-arm forensics back onto a shared clobber path.
            SOAK.MEM_BUDGET_ENV: "536870912",
            SOAK.SWEEP_WORKERS_ENV: "3",
            SOAK.RUN_MIRROR_ROOT_ENV: "/tmp/shared-mirror",
        }
        applied = apply_dotenv(hostile, env=env)
        assert applied == ()  # every pinned var already set → dotenv inert
        assert env[SOAK.KERNEL_CAPACITY_ENV] == "1"
        assert env[SOAK.KERNEL_CACHE_ENV] == "0"
        assert env[SOAK.RESULTS_DEPTH_ENV] == "1"
        assert env[SOAK.LOG_LEVEL_ENV] == "INFO"
        assert env[SOAK.MEM_BUDGET_ENV] == "1000000"
        assert env[SOAK.SWEEP_WORKERS_ENV] == "2"
        assert env[SOAK.RUN_MIRROR_ROOT_ENV] == "/out/arm/mirror"

    def test_child_bootstrap_env_cannot_flip_arm_envs(self, tmp_path: Path) -> None:
        # The refuter-1 repro shape, end-to-end: a child process runs the
        # REAL bootstrap_env() with BOTH dotenv roots planted hostile — a
        # cwd .env AND a $HOME/.brutex/.env (capacity 9 + cache flip) — and
        # must still resolve the pinned arm values.
        import os as _os

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text(
            f"{SOAK.KERNEL_CAPACITY_ENV}=9\n{SOAK.KERNEL_CACHE_ENV}=1\n"
            f"{SOAK.MEM_BUDGET_ENV}=536870912\n",
            encoding="utf-8",
        )
        home = tmp_path / "home"
        (home / ".brutex").mkdir(parents=True)
        # The refuter-A round-3 repro payload: sizing envs planted in the
        # home dotenv previously reached both children (applied_keys carried
        # MEM_BUDGET + WORKERS) and re-scoped the absolute RSS/pool bars.
        (home / ".brutex" / ".env").write_text(
            f"{SOAK.KERNEL_CAPACITY_ENV}=9\n{SOAK.RESULTS_DEPTH_ENV}=0\n"
            f"{SOAK.SWEEP_WORKERS_ENV}=3\n{SOAK.MEM_BUDGET_ENV}=536870912\n",
            encoding="utf-8",
        )
        env = _arm_env(
            dict(_os.environ),
            cache_on=False,
            results_depth_off=False,
            workers=2,
            mem_budget_bytes=1_000_000,
        )
        env["HOME"] = str(home)  # bootstrap_env resolves ~ via Path.home()
        code = (
            "from brutex.config.dotenv import bootstrap_env\n"
            "import os\n"
            "applied = bootstrap_env()\n"
            f"print(os.environ['{SOAK.KERNEL_CAPACITY_ENV}'],"
            f" os.environ['{SOAK.KERNEL_CACHE_ENV}'],"
            f" os.environ['{SOAK.RESULTS_DEPTH_ENV}'],"
            f" os.environ['{SOAK.SWEEP_WORKERS_ENV}'],"
            f" os.environ['{SOAK.MEM_BUDGET_ENV}'])\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        # Pinned values survive dotenv — incl. the round-3 sizing pins.
        assert proc.stdout.split() == ["1", "0", "1", "2", "1000000"]

    def test_effective_worker_kernel_capacity(self) -> None:
        assert SOAK.effective_worker_kernel_capacity("prev_day", "hlc3:close") == 1
        assert SOAK.effective_worker_kernel_capacity("prev_day,rolling_weekly", "hlc3:close") == 2
        assert (
            SOAK.effective_worker_kernel_capacity(
                "prev_day,rolling_weekly,weekly_static", "hlc3:close,close:close"
            )
            == 6
        )
        # Blank axes floor at 1; an absurd fan-out clamps at the engine max.
        assert SOAK.effective_worker_kernel_capacity("", "") == 1
        wide = ",".join(f"a{i}" for i in range(100))
        assert (
            SOAK.effective_worker_kernel_capacity(wide, "hlc3:close")
            == SOAK.WORKER_KERNEL_CAPACITY_MAX
        )

    def test_redact_home(self) -> None:
        home = str(Path.home())
        assert SOAK.redact_home(f"{home}/.brutex/lake") == "~/.brutex/lake"
        assert SOAK.redact_home(home) == "~"
        assert SOAK.redact_home("/srv/lake") == "/srv/lake"
        # A sibling dir sharing the home PREFIX must not be redacted.
        assert SOAK.redact_home(f"{home}2/lake") == f"{home}2/lake"

    def test_parser_exposes_capacity_widening_knobs(self) -> None:
        # R2 MAJOR-2: the grid-widening passthroughs exist and default unset.
        args = SOAK.build_parser().parse_args(
            [
                "run",
                "--out",
                "x",
                "--fib-anchors",
                "prev_day,rolling_weekly",
                "--price-source-cells",
                "hlc3:close,close:close",
            ]
        )
        assert args.fib_anchors == "prev_day,rolling_weekly"
        assert args.price_source_cells == "hlc3:close,close:close"
        default = SOAK.build_parser().parse_args(["run", "--out", "x"])
        assert default.fib_anchors is None
        assert default.price_source_cells is None

    def test_parser_arm_order(self) -> None:
        # Fix-round-3 NIT-1: the ordering-bias check lever.
        default = SOAK.build_parser().parse_args(["run", "--out", "x"])
        assert default.arm_order == "off-first"
        reversed_ = SOAK.build_parser().parse_args(["run", "--out", "x", "--arm-order", "on-first"])
        assert reversed_.arm_order == "on-first"
        with pytest.raises(SystemExit):
            SOAK.build_parser().parse_args(["run", "--out", "x", "--arm-order", "junk"])


class TestEngineDriftPins:
    """R2 NIT-6: the script's re-derived key/capacity models are pinned to the
    engine's OWN law (``sweep_parallel.worker_kernel_capacity_default``) — a
    new swept kernel-key axis (proximity/prior_n/…) changes the engine
    formula and breaks these pins LOUDLY instead of silently loosening the
    ``builds ≤ keys × workers`` bound."""

    def test_capacity_env_name_and_max_match_engine(self) -> None:
        from brutex.engine import sweep_parallel

        assert SOAK.KERNEL_CAPACITY_ENV == sweep_parallel.WORKER_KERNEL_CAPACITY_ENV
        assert SOAK.WORKER_KERNEL_CAPACITY_MAX == sweep_parallel.WORKER_KERNEL_CAPACITY_MAX

    def test_pinned_capacity_resolves_verbatim_in_engine(self) -> None:
        # Fix-round-2 MAJOR-1: the pin is behavior-identical to unset — the
        # engine resolves a set in-range value verbatim, so pinning the
        # grid-derived default measures exactly the default sizing law.
        from types import SimpleNamespace

        from brutex.engine import sweep_parallel

        grid = SimpleNamespace(
            proximities=[None], fib_anchors=["prev_day"], price_source_cells=["hlc3:close"]
        )
        default = sweep_parallel.worker_kernel_capacity_default(grid)  # type: ignore[arg-type]
        pinned = sweep_parallel.resolve_worker_kernel_capacity(
            grid,  # type: ignore[arg-type]
            env={sweep_parallel.WORKER_KERNEL_CAPACITY_ENV: str(default)},
        )
        assert pinned == default

    def test_sweep_runs_dirname_matches_engine(self) -> None:
        from brutex.engine import trade_store

        assert SOAK.SWEEP_RUNS_DIRNAME == trade_store.SWEEP_RUNS_DIRNAME
        # The results-depth root really is the SIBLING of the results root
        # (the Ruling-4 law the compare relies on).
        run_dir = trade_store.sweep_run_dir(Path("/arm/results"), "cafe")
        assert run_dir == Path("/arm") / SOAK.SWEEP_RUNS_DIRNAME / "cafe"

    def test_bench_baseline_key_env_matches_engine(self) -> None:
        from brutex.config import hardware_envelope

        assert SOAK.BENCH_BASELINE_KEY_ENV == hardware_envelope.BENCH_BASELINE_KEY_ENV

    def test_mem_budget_env_name_matches_engine(self) -> None:
        from brutex.engine import sweep_mem_budget

        assert SOAK.MEM_BUDGET_ENV == sweep_mem_budget.MEM_BUDGET_ENV

    def test_pinned_mem_budget_resolves_verbatim_and_matches_default_law(self) -> None:
        # Fix-round-3 MINOR-1: the pin is byte-identical to the engine's own
        # DEFAULT — resolve_pinned_mem_budget(N) equals the engine's default
        # resolution at BUDGET_WORKERS=N, and a SET MEM_BUDGET_ENV resolves
        # verbatim (per-worker, never divided — the #1352 contract), so a
        # pinned child measures exactly the default sizing law.
        from brutex.engine import sweep_mem_budget

        pinned = SOAK.resolve_pinned_mem_budget(4)
        assert pinned == sweep_mem_budget.resolve_mem_budget_bytes({}, workers=4)
        assert (
            sweep_mem_budget.resolve_mem_budget_bytes(
                {sweep_mem_budget.MEM_BUDGET_ENV: str(pinned)}, workers=999
            )
            == pinned
        )

    def test_mirror_root_env_name_and_semantics_match_engine(self) -> None:
        from brutex.engine import run_layout

        assert SOAK.RUN_MIRROR_ROOT_ENV == run_layout.MIRROR_ROOT_ENV
        # A SET root redirects the mirror under <root>/<run_id> — the per-arm
        # pin genuinely lands each arm's forensics in its OWN dir instead of
        # the shared (cross-arm-clobbered) ~/.brutex/runs/<run_id> default.
        assert run_layout.mirror_run_dir(
            "cafe", env={run_layout.MIRROR_ROOT_ENV: "/out/arm-on/mirror"}
        ) == Path("/out/arm-on/mirror/cafe")

    def test_pinned_workers_match_engine_auto_law(self) -> None:
        import os as _os

        from brutex.engine import sweep_parallel

        assert SOAK.resolve_pinned_workers(None) == (
            sweep_parallel.auto_sweep_workers(_os.cpu_count()),
            "auto-resolved",
        )
        assert SOAK.resolve_pinned_workers(6) == (6, "explicit")

    def test_symmetric_env_echo_names_are_real(self) -> None:
        # Fix-round-3 MINOR-1 audit hygiene: every audited symmetric env name
        # exists somewhere under brutex/ — a typo'd name would echo nothing
        # forever while the real knob went unaudited.
        import subprocess as _sp

        for name in SOAK.SYMMETRIC_ENV_ECHO:
            proc = _sp.run(
                ["grep", "-rlq", name, str(REPO_ROOT / "brutex")],
                check=False,
            )
            assert proc.returncode == 0, f"{name} not found in brutex/"

    @pytest.mark.parametrize(
        ("fib_csv", "ps_csv"),
        [
            ("prev_day", "hlc3:close"),
            ("prev_day,rolling_weekly", "hlc3:close"),
            ("prev_day,rolling_weekly,weekly_static", "hlc3:close,close:close"),
        ],
    )
    def test_capacity_matches_engine_default_law(self, fib_csv: str, ps_csv: str) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_parallel

        # worker_kernel_capacity_default reads exactly the three axis lists;
        # the CLI grid pins proximities to the singleton [None] (cli.py).
        grid = SimpleNamespace(
            proximities=[None],
            fib_anchors=[tok for tok in fib_csv.split(",") if tok],
            price_source_cells=[tok for tok in ps_csv.split(",") if tok],
        )
        engine_capacity = sweep_parallel.worker_kernel_capacity_default(grid)  # type: ignore[arg-type]
        assert SOAK.effective_worker_kernel_capacity(fib_csv, ps_csv) == engine_capacity

    def test_keys_fan_out_matches_engine_per_instrument_law(self) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_parallel

        # keys = symbols × intervals × the engine's own per-(instrument, tf)
        # kernel-key fan-out (unclamped here: product ≤ the capacity max).
        grid = SimpleNamespace(
            proximities=[None],
            fib_anchors=["prev_day", "rolling_weekly"],
            price_source_cells=["hlc3:close"],
        )
        per_instrument_tf = sweep_parallel.worker_kernel_capacity_default(grid)  # type: ignore[arg-type]
        assert (
            SOAK.keys_fan_out(
                "NIFTY,BANKNIFTY",
                "1minute,5minute,15minute",
                "prev_day,rolling_weekly",
                "hlc3:close",
            )
            == 2 * 3 * per_instrument_tf
        )


class TestGuardedCompareAndSummary:
    """Fix-round-3 (refuter-B NIT-1): the narrow no-report window is CLOSED —
    a post-arm hashing/render crash degrades to a report-with-error instead
    of a tracebacked exit 1 with no report (and no artifact)."""

    def test_compare_arms_guarded_passthrough_equals_compare_arms(self, tmp_path: Path) -> None:
        off = tmp_path / "arm-off"
        on = tmp_path / "arm-on"
        (off / "results").mkdir(parents=True)
        (on / "results").mkdir(parents=True)
        off_arm = {"exit_status": dict(_CLEAN_STATUS)}
        on_arm = {"exit_status": dict(_CLEAN_STATUS)}
        guarded = SOAK.compare_arms_guarded(off, on, off_arm, on_arm)
        assert guarded == SOAK.compare_arms(off, on, off_arm, on_arm)
        assert "compare_error" not in guarded

    def test_compare_crash_degrades_to_report_with_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The refuter-B window: a file vanishing between rglob and read_bytes
        # (SIGKILL-timeout path, surviving sweep descendant) raises OSError.
        def boom(arm_dir: Path) -> dict[str, str]:
            raise OSError("file vanished mid-walk")

        monkeypatch.setattr(SOAK, "arm_identity_tree", boom)
        compare = SOAK.compare_arms_guarded(tmp_path, tmp_path, {}, {})
        assert compare["identity_ok"] is False
        assert compare["audit_ts_masked_ok"] is False
        assert compare["exit_status_structural_ok"] is False
        assert "compare crashed" in compare["exit_status_detail"]
        assert "OSError" in compare["compare_error"]
        # Fully shaped: the report builds + the acceptance judges it a FAIL
        # (rc 1 via judged identity, never a fabricated PASS, never a crash).
        report = SOAK.build_report(
            provenance={"grid": {"keys_fan_out": 1}, "smoke": True},
            off=_off_arm(),
            on=_arm(),
            compare=compare,
            keys=1,
            smoke=True,
        )
        assert "compare crashed" in report["acceptance"]["identity"]
        assert "identity" in SOAK.judged_failures(report["acceptance"])

    def test_render_summary_guarded_degrades_and_passes_through(self) -> None:
        broken: dict[str, Any] = {"kind": SOAK.REPORT_KIND}  # render_summary KeyErrors
        text = SOAK.render_summary_guarded(broken)
        assert "summary render FAILED" in text
        assert "soak-report.json" in text
        report = SOAK.build_report(
            provenance={"grid": {"keys_fan_out": 6}, "smoke": True},
            off=_off_arm(),
            on=_arm(),
            compare=_compare(),
            keys=6,
            smoke=True,
        )
        assert SOAK.render_summary_guarded(report) == SOAK.render_summary(report)


class TestProvenanceCollection:
    def _grid(self) -> dict[str, str | None]:
        return {
            "symbols": "NIFTY",
            "intervals": "1minute",
            "fib_anchors": "prev_day,rolling_weekly",
            "price_source_cells": None,
        }

    def test_capacity_echo_and_pin_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SOAK.KERNEL_CAPACITY_ENV, "4")
        prov = SOAK.collect_provenance(
            lake_root=Path("/srv/lake"),
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
            min_free_gib=30,
        )
        assert prov["worker_kernel_capacity_effective"] == 2  # 2 anchors × default cell
        # Fix-round-2 MAJOR-1: the child env PINS the value (mechanism flag)
        # and the parent-ambient presence is echoed informationally.
        assert prov["capacity_env_child_pinned"] is True
        assert prov["capacity_env_ambient_present"] is True
        assert prov["min_free_gib"] == 30
        monkeypatch.delenv(SOAK.KERNEL_CAPACITY_ENV)
        prov = SOAK.collect_provenance(
            lake_root=Path("/srv/lake"),
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
        )
        assert prov["capacity_env_ambient_present"] is False
        assert prov["capacity_env_child_pinned"] is True

    def test_sizing_echo_and_symmetric_env_echo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fix-round-3 MINOR-1: the report states the sizing its ABSOLUTE
        # RSS/pool bars were measured at + echoes the audited symmetric envs.
        monkeypatch.setenv("BRUTEX_SWEEP_STALL_SECONDS", "77")
        monkeypatch.setenv(SOAK.SWEEP_WORKERS_ENV, "3")
        monkeypatch.delenv(SOAK.MEM_BUDGET_ENV, raising=False)
        prov = SOAK.collect_provenance(
            lake_root=Path("/srv/lake"),
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
            sweep_workers_effective=8,
            sweep_workers_source="auto-resolved",
            mem_budget_bytes_effective=123_456,
            arm_order=("on", "off"),
        )
        assert prov["sweep_workers_effective"] == 8
        assert prov["sweep_workers_source"] == "auto-resolved"
        assert prov["sweep_workers_env_ambient_present"] is True
        assert prov["mem_budget_bytes_effective"] == 123_456
        assert prov["mem_budget_env_ambient_present"] is False
        assert prov["arm_order"] == ["on", "off"]
        assert prov["symmetric_env_echo"]["BRUTEX_SWEEP_STALL_SECONDS"] == "77"
        assert "BRUTEX_SWEEP_STALL_SECONDS" in prov["symmetric_env_audited"]
        # HONEST: the echo is disclosed as the parent-environ view (dotenv
        # values the child injects are invisible — the pins cover those).
        assert "parent-environ" in prov["symmetric_env_echo_scope"]

    def test_hardware_key_source_probed_vs_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fix-round-2 (refuter-1 spoof): the report records WHERE the key
        # came from — the documented override env taints it.
        monkeypatch.delenv(SOAK.BENCH_BASELINE_KEY_ENV, raising=False)
        prov = SOAK.collect_provenance(
            lake_root=Path("/srv/lake"),
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
        )
        assert prov["hardware_key_source"] in ("probed", "unavailable")
        monkeypatch.setenv(SOAK.BENCH_BASELINE_KEY_ENV, "mac16-8-14c10p4e-48g-arm64")
        prov = SOAK.collect_provenance(
            lake_root=Path("/srv/lake"),
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
        )
        assert prov["hardware_key_source"] == f"env-override:{SOAK.BENCH_BASELINE_KEY_ENV}"

    def test_lake_root_home_redacted_and_uname_hostname_free(self) -> None:
        # R1 MINOR-1: the uploaded report must not leak the local username
        # (home prefix) nor the hostname/kernel build string (`uname -a`).
        prov = SOAK.collect_provenance(
            lake_root=Path.home() / ".brutex" / "lake",
            grid=self._grid(),
            budget_minutes=600,
            sample_interval_s=5.0,
            smoke=False,
        )
        assert prov["lake_root"] == "~/.brutex/lake"
        # `uname -sm` yields exactly "<kernel> <machine>" — two tokens, no
        # hostname (or the honest "unavailable" where uname is absent).
        assert prov["uname"] == "unavailable" or len(prov["uname"].split()) == 2


class TestPreflight:
    def test_preflight_lake_sound_and_missing(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        good = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2026"
        good.mkdir(parents=True)
        (good / "01.parquet").write_bytes(b"x")
        # BANKNIFTY under the groww-form dir name — either form satisfies.
        alt = lake / "bars" / "NSE" / "INDEX" / "NSE-BANKNIFTY" / "1minute" / "2026"
        alt.mkdir(parents=True)
        (alt / "01.parquet").write_bytes(b"x")
        assert SOAK.preflight_lake(lake, "NIFTY,BANKNIFTY", "1minute", "NSE", "INDEX") == []

        missing = SOAK.preflight_lake(lake, "NIFTY,SENSEX", "1minute,5minute", "NSE", "INDEX")
        assert len(missing) == 2  # NIFTY/5minute empty + SENSEX absent entirely
        assert any("SENSEX" in m for m in missing)
        assert any("5minute" in m for m in missing)

    def test_preflight_lake_refuses_missing_root(self, tmp_path: Path) -> None:
        missing = SOAK.preflight_lake(tmp_path / "nope", "NIFTY", "1minute", "NSE", "INDEX")
        assert missing == [f"lake root {tmp_path / 'nope'} is not a directory"]


class TestReportRendering:
    def _report(self, **acceptance_overrides: str) -> dict[str, Any]:
        off, on = _off_arm(), _arm()
        compare = _compare()
        report: dict[str, Any] = SOAK.build_report(
            provenance={
                "commit": "deadbeef",
                "hardware_key": _M4PRO_KEY,
                "lake_root": "/lake",
                "started_utc": "2026-07-19T00:00:00+00:00",
                "rss_method": "ps-poll-5.0s (peak is a LOWER BOUND on the true peak)",
                "smoke": False,
                "grid": {"keys_fan_out": 6},
                "worker_kernel_capacity_effective": 1,
                "capacity_env_child_pinned": True,
                "capacity_env_ambient_present": False,
                "hardware_key_source": "probed",
                "sweep_workers_effective": 2,
                "sweep_workers_source": "explicit",
                "mem_budget_bytes_effective": 8_589_934_592,
                "arm_order": ["off", "on"],
            },
            off=off,
            on=on,
            compare=compare,
            keys=6,
            smoke=False,
        )
        report["acceptance"].update(acceptance_overrides)
        return report

    def test_build_report_shape(self) -> None:
        report = self._report()
        assert report["schema_version"] == SOAK.REPORT_SCHEMA_VERSION
        assert report["kind"] == SOAK.REPORT_KIND
        assert report["acceptance"]["verdict"] == "FLIP-READY"
        assert report["deltas"]["rss_per_worker_kib"] == 100_000

    def test_fmt_kib(self) -> None:
        assert SOAK.fmt_kib(None) == "?"
        assert SOAK.fmt_kib(1024 * 1024) == "1048576 KiB (1.00 GiB)"

    def test_render_summary_carries_measured_value_and_honesty(self) -> None:
        text = SOAK.render_summary(self._report())
        assert "MEASURED" in text
        assert "MODELED" in text  # the full-grid extrapolation stays modeled
        assert "## VALUE" in text  # actual win/loss visible, not just pass/fail
        assert "actual headroom" in text
        assert "YELLOW FLAG" in text
        assert "NOT pre-approved" in text  # the flip-boundary honesty language
        assert "verdict: FLIP-READY" in text
        # R2 MAJOR-2: the RSS capacity scoping is stated on the summary;
        # fix-round-2 MAJOR-1: the summary states the PIN (dotenv-immune),
        # never the defeated pop claim.
        assert "RSS bar evaluated at kernel-cache capacity=1" in text
        assert SOAK.KERNEL_CAPACITY_ENV in text
        assert "PINNED explicitly" in text
        assert "popped" not in text.lower()
        # Fix-round-3 MINOR-1 + NIT-1: the sizing-pin echo and the
        # OFF→ON warm-cache ordering-bias disclosure are on the summary.
        assert "sizing PINNED in both arm envs" in text
        assert SOAK.MEM_BUDGET_ENV in text
        assert "wall-bar ordering bias" in text
        assert "OFF→ON" in text
        assert "--arm-order" in text

    def test_render_summary_smoke_banner(self) -> None:
        report = self._report()
        report["provenance"]["smoke"] = True
        assert "NOT decision-grade" in SOAK.render_summary(report)


class TestVerdictSubcommand:
    def _write_report(self, tmp_path: Path, **mutations: Any) -> Path:
        off, on = _off_arm(), _arm()
        report = SOAK.build_report(
            provenance={"grid": {"keys_fan_out": 6}, "smoke": False, "hardware_key": _M4PRO_KEY},
            off=off,
            on=on,
            compare=_compare(**mutations.pop("compare", {})),
            keys=6,
            smoke=False,
        )
        report.update(mutations)
        path = tmp_path / "soak-report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
        )

    def test_verdict_green_report_exits_zero(self, tmp_path: Path) -> None:
        proc = self._run("verdict", "--report", str(self._write_report(tmp_path)))
        assert proc.returncode == 0, proc.stderr
        assert "verdict: FLIP-READY" in proc.stdout

    def test_verdict_red_report_exits_one(self, tmp_path: Path) -> None:
        path = self._write_report(tmp_path, compare={"identity_ok": False})
        proc = self._run("verdict", "--report", str(path))
        assert proc.returncode == 1
        assert "NOT-READY(identity)" in proc.stdout

    def test_verdict_unreadable_report_is_structural(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.json"
        junk.write_text("{not json", encoding="utf-8")
        assert self._run("verdict", "--report", str(junk)).returncode == 2
        assert self._run("verdict", "--report", str(tmp_path / "absent.json")).returncode == 2

    def test_verdict_wrong_kind_is_structural(self, tmp_path: Path) -> None:
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"kind": "other", "schema_version": 1}), encoding="utf-8")
        proc = self._run("verdict", "--report", str(path))
        assert proc.returncode == 2
        assert "STRUCTURAL" in proc.stderr

    def test_verdict_invalid_shape_is_structural(self, tmp_path: Path) -> None:
        path = tmp_path / "shape.json"
        path.write_text(
            json.dumps({"kind": SOAK.REPORT_KIND, "schema_version": 1}), encoding="utf-8"
        )
        assert self._run("verdict", "--report", str(path)).returncode == 2

    def test_usage_error(self) -> None:
        assert self._run().returncode == 2  # argparse usage → structural

    def test_run_refuses_workers_below_two(self, tmp_path: Path) -> None:
        proc = self._run("run", "--out", str(tmp_path / "out"), "--workers", "1", "--smoke")
        assert proc.returncode == 2
        assert "workers" in proc.stderr

    def test_verdict_non_m4pro_report_reads_not_decision_grade(self, tmp_path: Path) -> None:
        # R2 MINOR-4: bars green on foreign hardware → rc 0 (not a harness
        # failure) but the verdict can never read FLIP-READY.
        path = self._write_report(
            tmp_path,
            provenance={
                "grid": {"keys_fan_out": 6},
                "smoke": False,
                "hardware_key": "pc-8c8p0e-32g-x86-64",
            },
        )
        proc = self._run("verdict", "--report", str(path))
        assert proc.returncode == 0
        assert "NOT-DECISION-GRADE" in proc.stdout
        assert "FLIP-READY" not in proc.stdout

    def test_verdict_env_override_key_source_reads_not_decision_grade(self, tmp_path: Path) -> None:
        # Fix-round-2 (refuter-1 spoof): a report whose recorded key SOURCE is
        # the BRUTEX_BENCH_BASELINE_KEY override — even with a perfect M4 key
        # text — can never read FLIP-READY.
        path = self._write_report(
            tmp_path,
            provenance={
                "grid": {"keys_fan_out": 6},
                "smoke": False,
                "hardware_key": _M4PRO_KEY,
                "hardware_key_source": f"env-override:{SOAK.BENCH_BASELINE_KEY_ENV}",
            },
        )
        proc = self._run("verdict", "--report", str(path))
        assert proc.returncode == 0
        assert "NOT-DECISION-GRADE" in proc.stdout
        assert SOAK.BENCH_BASELINE_KEY_ENV in proc.stdout
        assert "FLIP-READY" not in proc.stdout

    def test_verdict_live_env_override_taints_even_a_probed_report(self, tmp_path: Path) -> None:
        # Belt-check: the override set AT VERDICT TIME taints the run — the
        # report itself claims a probed M4 key, but the live env is forged.
        import os as _os

        path = self._write_report(
            tmp_path,
            provenance={
                "grid": {"keys_fan_out": 6},
                "smoke": False,
                "hardware_key": _M4PRO_KEY,
                "hardware_key_source": "probed",
            },
        )
        env = dict(_os.environ)
        env[SOAK.BENCH_BASELINE_KEY_ENV] = _M4PRO_KEY
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "verdict", "--report", str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 0
        assert "NOT-DECISION-GRADE" in proc.stdout
        assert "set at verdict time" in proc.stdout
        # Fix-round-3 NIT (refuter-A wording): the refusal attributes the
        # LIVE verdict-time override — the STORED key stays honestly probed.
        assert "probed at run time" in proc.stdout
        assert "comes from the" not in proc.stdout
        assert "FLIP-READY" not in proc.stdout

    def test_verdict_stored_override_keeps_runtime_attribution(self, tmp_path: Path) -> None:
        # Fix-round-3: when the report's RECORDED source is already the
        # run-time override, a live verdict-time env does NOT overwrite that
        # stronger attribution with the belt wording.
        import os as _os

        path = self._write_report(
            tmp_path,
            provenance={
                "grid": {"keys_fan_out": 6},
                "smoke": False,
                "hardware_key": _M4PRO_KEY,
                "hardware_key_source": f"env-override:{SOAK.BENCH_BASELINE_KEY_ENV}",
            },
        )
        env = dict(_os.environ)
        env[SOAK.BENCH_BASELINE_KEY_ENV] = _M4PRO_KEY
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "verdict", "--report", str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 0
        assert "NOT-DECISION-GRADE" in proc.stdout
        assert "comes from the" in proc.stdout  # the run-time attribution wins
        assert "set at verdict time" not in proc.stdout

    def test_verdict_prints_trusted_input_disclosure(self, tmp_path: Path) -> None:
        proc = self._run("verdict", "--report", str(self._write_report(tmp_path)))
        assert "TRUSTED INPUT" in proc.stdout
        assert "not a" in proc.stdout and "tamper-proof" in proc.stdout


class TestRunGuards:
    """cmd_run's structural refusals (R2 MAJOR-3 fresh-out + MINOR-5 fence).

    Every guard fires BEFORE any lake build/arm spawn — cheap subprocess
    probes."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
        )

    def test_run_refuses_nonempty_out(self, tmp_path: Path) -> None:
        out = tmp_path / "used"
        (out / "arm-off").mkdir(parents=True)
        proc = self._run("run", "--smoke", "--out", str(out))
        assert proc.returncode == 2
        assert "NOT empty" in proc.stderr
        assert "FRESH" in proc.stderr
        assert "resume" in proc.stderr  # names the resume/cache-skip hazard

    def test_run_refuses_auto_resolved_sequential_workers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Fix-round-3 MINOR-1: with SWEEP_WORKERS now always PINNED, an
        # auto-resolution < 2 (a tiny box) must refuse structurally — pinning
        # workers=1 would soak into a guaranteed loud telemetry FAIL instead.
        args = SOAK.build_parser().parse_args(["run", "--out", str(tmp_path / "fresh")])
        monkeypatch.setattr(SOAK, "resolve_pinned_workers", lambda w: (1, "auto-resolved"))
        assert SOAK.cmd_run(args) == 2
        err = capsys.readouterr().err
        assert "auto-resolved sweep workers=1" in err
        assert "--workers" in err
        assert not (tmp_path / "fresh").exists()  # nothing written — out stays fresh

    def test_run_refuses_out_that_is_a_file(self, tmp_path: Path) -> None:
        target = tmp_path / "file"
        target.write_text("x", encoding="utf-8")
        proc = self._run("run", "--smoke", "--out", str(target))
        assert proc.returncode == 2
        assert "not a directory" in proc.stderr

    @pytest.mark.parametrize("budget", ["0", "-5", "1196"])
    def test_run_refuses_out_of_fence_budget(self, tmp_path: Path, budget: str) -> None:
        # R2 MINOR-5: the script enforces the SAME 1..1195 SEC9-1 law the
        # workflow plan job fences — the one-command fallback cannot bypass it.
        proc = self._run(
            "run", "--smoke", "--out", str(tmp_path / "fresh"), "--budget-minutes", budget
        )
        assert proc.returncode == 2
        assert "1..1195" in proc.stderr

    @pytest.mark.parametrize("gib", ["0", "-1", "1025"])
    def test_run_refuses_out_of_fence_min_free_gib(self, tmp_path: Path, gib: str) -> None:
        # Fix-round-2 NIT: the disk-need override is bounded — never a zero /
        # negative / absurd requirement.
        proc = self._run("run", "--smoke", "--out", str(tmp_path / "fresh"), "--min-free-gib", gib)
        assert proc.returncode == 2
        assert "1..1024" in proc.stderr

    def test_refused_lake_preflight_leaves_out_reusable(self, tmp_path: Path) -> None:
        # Fix-round-2 MINOR (refuter-2): a preflight-REFUSED non-smoke run
        # must write NOTHING into --out — the documented retry (same --out,
        # corrected --lake-root) would otherwise trip the fresh-out guard.
        out = tmp_path / "out"
        proc = self._run(
            "run",
            "--out",
            str(out),
            "--lake-root",
            str(tmp_path / "no-such-lake"),
            "--min-free-gib",
            "1",
        )
        assert proc.returncode == 2
        assert "lake preflight refused" in proc.stderr
        assert "still fresh" in proc.stderr  # the remediation names the retry
        # NOTHING written: out absent (or empty) → the retry passes the guard.
        assert not out.exists() or not any(out.iterdir())
        # The guard itself agrees: a second refusal is again the LAKE refusal,
        # never the fresh-out refusal.
        proc2 = self._run(
            "run",
            "--out",
            str(out),
            "--lake-root",
            str(tmp_path / "no-such-lake"),
            "--min-free-gib",
            "1",
        )
        assert proc2.returncode == 2
        assert "lake preflight refused" in proc2.stderr
        assert "NOT empty" not in proc2.stderr
