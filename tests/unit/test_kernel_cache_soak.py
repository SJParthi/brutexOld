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
_BUILT_LINE = (
    "\x1b[2m2026-07-19T11:41:12.000000Z\x1b[0m [\x1b[32m\x1b[1minfo     \x1b[0m] "
    "\x1b[1msweep_parallel.worker_kernel_built\x1b[0m "
    "\x1b[36mworker_pid\x1b[0m=\x1b[35m22901\x1b[0m"
)


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


def _evaluate(
    off: dict[str, Any] | None = None,
    on: dict[str, Any] | None = None,
    compare: dict[str, Any] | None = None,
    *,
    keys: int = 6,
    smoke: bool = False,
) -> dict[str, str]:
    off = _off_arm() if off is None else off
    on = _arm() if on is None else on
    compare = _compare() if compare is None else compare
    result: dict[str, str] = SOAK.evaluate_acceptance(
        off=off, on=on, compare=compare, deltas=SOAK.build_deltas(off, on), keys=keys, smoke=smoke
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
            "verdict": "FLIP-READY",
        }
        assert SOAK.judged_failures(acceptance) == []

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
        # A smoke arm wildly over every ceiling must still be mechanics-green.
        on = _arm(wall_s=500.0, worker_peak_rss_kib_max=10**9, pool_peak_rss_kib=10**9)
        acceptance = _evaluate(on=on, smoke=True)
        assert acceptance["rss_envelope"] == "SMOKE-UNJUDGED"
        assert acceptance["wall_neutral"] == "SMOKE-UNJUDGED"
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
        env = SOAK.arm_env({"PATH": "/bin"}, cache_on=True, workers=8, results_depth_off=True)
        assert env[SOAK.KERNEL_CACHE_ENV] == "1"
        assert env[SOAK.SWEEP_WORKERS_ENV] == "8"
        assert env[SOAK.LOG_LEVEL_ENV] == "INFO"  # telemetry must reach stderr
        assert env["PYTHONHASHSEED"] == "0"
        assert env[SOAK.RESULTS_DEPTH_ENV] == "0"
        assert env["PATH"] == "/bin"
        off = SOAK.arm_env({}, cache_on=False, workers=None, results_depth_off=False)
        assert off[SOAK.KERNEL_CACHE_ENV] == "0"
        assert SOAK.SWEEP_WORKERS_ENV not in off
        assert SOAK.RESULTS_DEPTH_ENV not in off


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
                "hardware_key": "m4pro-key",
                "lake_root": "/lake",
                "started_utc": "2026-07-19T00:00:00+00:00",
                "rss_method": "ps-poll-5.0s (peak is a LOWER BOUND on the true peak)",
                "smoke": False,
                "grid": {"keys_fan_out": 6},
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

    def test_render_summary_smoke_banner(self) -> None:
        report = self._report()
        report["provenance"]["smoke"] = True
        assert "NOT decision-grade" in SOAK.render_summary(report)


class TestVerdictSubcommand:
    def _write_report(self, tmp_path: Path, **mutations: Any) -> Path:
        off, on = _off_arm(), _arm()
        report = SOAK.build_report(
            provenance={"grid": {"keys_fan_out": 6}, "smoke": False},
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
