"""Tests for the flip-soak harness (DEC-FLIP-SOAK-HARNESS-001 — MIGRATION.md #1894).

Four surfaces, all runnable in shared CI (no M4-Pro, no real lake):

1. **Workflow wiring pins** on ``.github/workflows/flip-soak-m4pro.yml`` —
   dispatch-ONLY (a push/PR/schedule trigger would hang PR mergeable state
   on the offline Mac, DEC-M4PRO-PR-TRIGGER-001), the ``ref`` input (the
   #1479 dispatchability lesson — the workflow is main-resident and checks
   out the slice branch), the derived-timeout law tokens (the
   soak-selfhosted.yml plan-job fences), SHA-pinned GitHub-owned actions
   only (SEC12-1), ``persist-credentials: false`` (SEC13-1) — the
   ``TestBenchWorkflowWiring`` / ``TestMutationWorkflowWiring`` precedent.
2. **Registry pins** — flag classes valid, the §1(b) NAMING LAW (every
   row's markers derive from its ``surface_prefix``), the CLI lookups the
   bash orchestrator consumes, manifest + snapshot-token helpers.
3. **``flip_soak.sh`` behavior via the DRY/fake-lake stub-CLI seam**
   (``FLIP_SOAK_ARM_CMD`` — the #1479 round-2 ``TestTwinRunEngagementGate``
   idiom): PASS · masked-PASS refused · no-engagement refused ·
   vacuous-OFF-arm FAIL · byte-diff FAIL · env-preset refusal ·
   snapshot-moved RETRY_ENV_MOVED · the DIFFERENTIAL-class zero-divergence
   oracle (via the ``FLIP_SOAK_EXTRA_FLAGS`` test seam).
4. **PASS.json schema** (``emit_pass.py``) — round-trip, provenance
   mandatory, PASS-only-under-docs/soak refusal, twin-XOR-differential —
   plus the committed-evidence sweep (every file under ``docs/soak/`` must
   validate; vacuously green until the first operator evidence PR lands).
5. **The drift-leg driver** in ``--dry`` fake-lake mode — real
   ``SoakDriftMonitor`` + real ``process_probe`` reads, synthetic ops; a
   forced 1-KiB RSS ceiling proves the hard gate fails loudly.

HONEST envelope (Golden Rule #10): these tests prove the HARNESS's own
logic in dry mode; the real-box twin/drift legs have NOT been executed in
this PR (the real lake exists only on the operator's M4-Pro).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SOAK_DIR = _REPO / "scripts" / "soak"
_WORKFLOW = _REPO / ".github" / "workflows" / "flip-soak-m4pro.yml"
_SCRIPT = _SOAK_DIR / "flip_soak.sh"


def _load(name: str) -> ModuleType:
    """Import a scripts/soak module by name (coverage-friendly, sys.path)."""
    soak_dir = str(_SOAK_DIR)
    if soak_dir not in sys.path:
        sys.path.insert(0, soak_dir)
    return importlib.import_module(name)


def _logic_ceiling_kib(driver: ModuleType, *, margin_kib: int = 2 * 1024 * 1024) -> int:
    """RSS ceiling for the IN-PROCESS logic tests: ambient process RSS + margin.

    The driver's default ``soak.rss_ceiling`` is an ABSOLUTE whole-process hard
    gate sized for a DEDICATED standalone soak (4 GiB on the self-hosted tier).
    Run in-process inside a large pytest shard, the enclosing process can
    already sit at/near that absolute ceiling (this shard peaks ~4.1 GiB after
    importing every module + native extension), so a no-op ``--dry`` run would
    false-breach on the AMBIENT footprint — not on anything the soak did. These
    tests assert the soak LOGIC (a clean dry-run adds no breach; an env-move is
    a retry, not a fail), so they gate on a ceiling that floats ABOVE where the
    process already is (a 2 GiB self-inflicted leak in a 3-window dry-run would
    still breach — a real bug worth catching). The absolute-ceiling DEFAULT
    path and the explicit ``--rss-ceiling-kib 1`` loudly-breach test keep the
    driver's PRODUCTION absolute semantics under test unchanged.
    """
    ambient, _fd = driver._probe()
    if ambient is None:  # platform abstain: no RSS reading → the gate cannot fire
        return 1 << 40
    return int(ambient) + margin_kib


@pytest.fixture(name="registry")
def _registry() -> ModuleType:
    return _load("flag_registry")


@pytest.fixture(name="emit_pass")
def _emit_pass() -> ModuleType:
    return _load("emit_pass")


# ── 1. Workflow wiring pins ──────────────────────────────────────────


class TestFlipSoakWorkflowWiring:
    """Deterministic pins on flip-soak-m4pro.yml (the TestBenchWorkflowWiring style)."""

    _TEXT = _WORKFLOW.read_text(encoding="utf-8")

    def test_dispatch_only_no_push_pr_or_schedule(self) -> None:
        """A push/PR/schedule trigger on the offline Mac would hang PRs."""
        assert "workflow_dispatch:" in self._TEXT
        trigger_lines = re.findall(
            r"^\s{0,4}(push|pull_request|schedule):\s*$", self._TEXT, re.MULTILINE
        )
        assert trigger_lines == [], f"forbidden trigger(s) present: {trigger_lines}"

    def test_ref_input_present_and_wired_into_checkout(self) -> None:
        """The #1479 lesson: main-resident workflow, slice branch via ref."""
        assert re.search(r"^\s+ref:\s*$", self._TEXT, re.MULTILINE) is not None
        assert "ref: ${{ github.event.inputs.ref }}" in self._TEXT

    def test_flag_choice_options_equal_the_registry(self, registry: ModuleType) -> None:
        options = set(re.findall(r"^\s+- (BRUTEX_[A-Z0-9_]+)\s*$", self._TEXT, re.MULTILINE))
        assert options == {spec.flag for spec in registry.REGISTRY}

    def test_timeout_law_tokens(self) -> None:
        """The soak-selfhosted plan-job law: shell arithmetic + all fences."""
        assert "timeout_minutes=$((m + 90 + m / 6))" in self._TEXT
        assert 'if [ "$m" -gt 1100 ]; then' in self._TEXT  # SEC9-1 token-death bound
        assert "m=$((10#$m))" in self._TEXT  # SOAK-R7-3 octal fence
        assert 'if [ "$m" -lt 1 ]; then' in self._TEXT  # SOAK-R8-3 literal zero
        assert 'if [ "${#m}" -gt 5 ]; then' in self._TEXT  # SEC7-2 magnitude fence
        assert (
            self._TEXT.count("timeout-minutes: ${{ fromJSON(needs.plan.outputs.timeout_minutes) }}")
            == 2
        ), "job-level AND per-step derived timeouts (the G2 belt) must both bind"

    def test_runner_labels_and_posture(self) -> None:
        assert "runs-on: [self-hosted, macos, m4pro]" in self._TEXT
        assert "persist-credentials: false" in self._TEXT  # SEC13-1
        assert re.search(r"^permissions:\n  contents: read", self._TEXT, re.MULTILINE)
        assert "group: m4pro-flip-soak" in self._TEXT
        assert "cancel-in-progress: false" in self._TEXT

    def test_actions_are_sha_pinned_and_github_owned(self) -> None:
        """SEC12-1: GitHub-owned actions only, every one SHA-pinned."""
        uses = re.findall(r"^\s+uses:\s*(\S+)\s*(?:#.*)?$", self._TEXT, re.MULTILINE)
        assert uses, "workflow must use at least one action"
        for ref in uses:
            action, _, pin = ref.partition("@")
            assert action.startswith("actions/"), f"third-party action on the Mac: {ref}"
            assert re.fullmatch(r"[0-9a-f]{40}", pin), f"un-SHA-pinned action: {ref}"

    def test_issue_watchdog_covers_cancelled(self) -> None:
        """AL-SOAK-1: a timeout-kill reports CANCELLED, not FAILED."""
        assert "if: failure() || cancelled()" in self._TEXT

    def test_lake_guard_names_the_terminal_fallback(self) -> None:
        assert "BRUTEX_LAKE_ROOT" in self._TEXT
        assert "bash scripts/soak/flip_soak.sh" in self._TEXT

    def test_not_wired_into_required_ci(self) -> None:
        """NON-required posture: never in ci.yml's ci-ok needs."""
        ci_text = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "flip-soak" not in ci_text

    def test_plan_job_least_privilege_permissions(self) -> None:
        """Workflow NIT (a): the shell-arithmetic plan job drops issues:write.

        The top-level grant is ``contents: read`` + ``issues: write`` (only the
        flip-soak job's failure watchdog needs the latter); the plan job — which
        never checks out and never files an issue — carries its own read-only
        ``permissions`` block so it never holds a write-capable token.
        """
        # Isolate ONLY the plan job body: the text between the ``  plan:`` job
        # header (two-space indent, under ``jobs:``) and the next job header
        # ``  flip-soak:``. The top-level ``permissions:`` block sits at
        # column 0 BEFORE ``jobs:``, so it is deliberately excluded — otherwise
        # its ``issues: write`` grant would leak into this region.
        match = re.search(
            r"^  plan:\n(?P<body>.*?)^  flip-soak:", self._TEXT, re.DOTALL | re.MULTILINE
        )
        assert match is not None, "the plan job header must precede the flip-soak job header"
        plan = match.group("body")
        # A job-level permissions block (indented under the job) scoped to
        # read-only, distinct from the top-level (column-0) block.
        assert re.search(r"^    permissions:\n      contents: read\n", plan, re.MULTILINE), (
            "the plan job must declare its own least-privilege permissions: contents: read"
        )
        assert "issues: write" not in plan, "the plan job must not inherit/hold issues:write"

    def test_logs_are_redacted(self) -> None:
        """Workflow NIT (b): no host/path leaks into the world-readable log."""
        # `uname -a` leaks nodename + kernel build string; `uname -sm` is the
        # kernel + machine arch (the hardware proof) without the host identity.
        assert "uname -a" not in self._TEXT
        assert "uname -sm" in self._TEXT
        # The lake guard prints existence, never the absolute path — an
        # `echo` interpolating $BRUTEX_LAKE_ROOT would leak the box's layout.
        assert 'echo "Lake root: $BRUTEX_LAKE_ROOT' not in self._TEXT
        assert "Lake root: reachable" in self._TEXT

    def test_arm_cmd_seam_documented_as_test_only(self) -> None:
        """Workflow NIT (c): FLIP_SOAK_ARM_CMD is a test-only seam, never set here."""
        assert "FLIP_SOAK_ARM_CMD" in self._TEXT
        assert "TEST-ONLY" in self._TEXT
        # The seam must never appear as a workflow env assignment (that would
        # soak the fake-lake stub kernel in production).
        assert not re.search(r"^\s*FLIP_SOAK_ARM_CMD:\s", self._TEXT, re.MULTILINE)


# ── 2. Registry pins ─────────────────────────────────────────────────


class TestFlagRegistry:
    def test_classes_valid_and_flags_unique(self, registry: ModuleType) -> None:
        flags = [spec.flag for spec in registry.REGISTRY]
        assert len(flags) == len(set(flags))
        for spec in registry.REGISTRY:
            assert spec.flag_class in registry.FLAG_CLASSES
            assert spec.flag.startswith("BRUTEX_")
            assert spec.flag == spec.flag.upper()

    def test_naming_law_markers_derive_from_surface_prefix(self, registry: ModuleType) -> None:
        """Design §1(b): the parameterized gate needs only the prefix."""
        for spec in registry.REGISTRY:
            prefix = spec.surface_prefix
            assert spec.engaged_marker == rf"{prefix}\.engaged"
            assert spec.bad_markers.startswith(rf"{prefix}\.(")
            assert spec.divergence_marker == rf"{prefix}\.divergence"

    def test_unknown_flag_and_unknown_class_refused(self, registry: ModuleType) -> None:
        with pytest.raises(KeyError, match="unknown flag"):
            registry.spec_for("BRUTEX_NO_SUCH_FLAG")
        with pytest.raises(ValueError, match="unknown flag_class"):
            registry.FlagSpec(
                flag="BRUTEX_X",
                flag_class="BOGUS",
                surface_prefix="x",
                engaged_marker=r"x\.engaged",
                bad_markers=r"x\.(fallback)",
                divergence_marker=r"x\.divergence",
                twin_argv=("true",),
            )
        with pytest.raises(ValueError, match="uppercase BRUTEX_"):
            registry.FlagSpec(
                flag="not_a_flag",
                flag_class=registry.SWEEP_TWIN,
                surface_prefix="x",
                engaged_marker=r"x\.engaged",
                bad_markers=r"x\.(fallback)",
                divergence_marker=r"x\.divergence",
                twin_argv=("true",),
            )

    def test_empty_engaged_or_bad_marker_refused(self, registry: ModuleType) -> None:
        """NIT-6: an empty marker would make the engagement gate pass vacuously."""
        with pytest.raises(ValueError, match="empty engaged_marker"):
            registry.FlagSpec(
                flag="BRUTEX_X",
                flag_class=registry.SWEEP_TWIN,
                surface_prefix="x",
                engaged_marker="",
                bad_markers=r"x\.(fallback)",
                divergence_marker=r"x\.divergence",
                twin_argv=("true",),
            )
        with pytest.raises(ValueError, match="empty bad_markers"):
            registry.FlagSpec(
                flag="BRUTEX_X",
                flag_class=registry.SWEEP_TWIN,
                surface_prefix="x",
                engaged_marker=r"x\.engaged",
                bad_markers="",
                divergence_marker=r"x\.divergence",
                twin_argv=("true",),
            )

    def test_render_argv_substitutes_roots_and_scale(self, registry: ModuleType) -> None:
        spec = registry.spec_for("BRUTEX_SWEEP_RUST_ENUM")
        argv = registry.render_argv(spec, lake_root="/lake", results_root="/out", scale="smoke")
        assert "/lake" in argv
        assert "/out" in argv
        assert "--top" in argv
        with pytest.raises(KeyError, match="unknown grid scale"):
            registry.render_argv(spec, lake_root="/l", results_root="/o", scale="bogus")

    def test_smoke_scale_is_documented_not_flip_qualifying(self, registry: ModuleType) -> None:
        """The #1479 vacuity lesson: minutes-scale grids qualify nothing."""
        assert "smoke" in registry.GRID_SCALES
        # The load-bearing pin: the comment beside GRID_SCALES names the law.
        source = (_SOAK_DIR / "flag_registry.py").read_text(encoding="utf-8")
        assert "NOT flip-qualifying" in source

    def test_cli_list_flags_and_field_lookups(self, registry: ModuleType) -> None:
        out = subprocess.run(
            [sys.executable, str(_SOAK_DIR / "flag_registry.py"), "--list-flags"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.split() == [spec.flag for spec in registry.REGISTRY]
        field = subprocess.run(
            [
                sys.executable,
                str(_SOAK_DIR / "flag_registry.py"),
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--field",
                "engaged_marker",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert field.stdout.strip() == r"sweep_rust_enum\.engaged"
        unknown = subprocess.run(
            [
                sys.executable,
                str(_SOAK_DIR / "flag_registry.py"),
                "--flag",
                "BRUTEX_NOPE",
                "--field",
                "flag_class",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert unknown.returncode == 2
        assert "unknown flag" in unknown.stdout

    def test_manifest_and_snapshot_helpers(self, registry: ModuleType, tmp_path: Path) -> None:
        spec = registry.spec_for("BRUTEX_SWEEP_RUST_ENUM")
        (tmp_path / "b").mkdir()
        (tmp_path / "a.parquet").write_bytes(b"x")
        (tmp_path / "b" / "c.trials.json").write_text("{}", encoding="utf-8")
        (tmp_path / "ignored.jsonl").write_text("ts\n", encoding="utf-8")  # audit: excluded
        lines = registry.manifest_lines(spec, tmp_path)
        rels = [line.split("  ", 1)[1] for line in lines]
        assert rels == ["a.parquet", "b/c.trials.json"]  # sorted, audit jsonl absent
        # snapshot: none → "", one → value, two distinct → INCONSISTENT
        assert registry.snapshot_token(tmp_path) == ""
        (tmp_path / "stamp1.json").write_text(
            json.dumps({"extra": {"data_snapshot_hash": "abc123"}}), encoding="utf-8"
        )
        assert registry.snapshot_token(tmp_path) == "abc123"
        (tmp_path / "stamp2.json").write_text(
            json.dumps({"data_snapshot_hash": "def456"}), encoding="utf-8"
        )
        assert registry.snapshot_token(tmp_path) == "INCONSISTENT"


# ── 3. flip_soak.sh behavior (DRY / fake-lake stub-CLI seam) ─────────

#: The stub arm CLI — the #1479 round-2 idiom: configurable engaged /
#: bad / divergence markers + artifacts via STUB_* env vars.
_STUB = r"""
import json, os, sys
from pathlib import Path

arm = os.environ["FLIP_SOAK_ARM"]
root = Path(os.environ["FLIP_SOAK_RESULTS_ROOT"])
root.mkdir(parents=True, exist_ok=True)

if arm == "off":
    if os.environ.get("STUB_OFF_EMPTY") != "1":
        (root / "store.parquet").write_bytes(b"identical-bytes")
    snap = os.environ.get("STUB_SNAPSHOT_OFF")
else:
    payload = b"DIFFERENT" if os.environ.get("STUB_ON_DIFFERENT") == "1" else b"identical-bytes"
    (root / "store.parquet").write_bytes(payload)
    snap = os.environ.get("STUB_SNAPSHOT_ON")
    if os.environ.get("STUB_EMIT_ENGAGED") == "1":
        print(os.environ.get("STUB_PREFIX", "sweep_rust_enum") + ".engaged mask=1")
    if os.environ.get("STUB_EMIT_BAD") == "1":
        print(os.environ.get("STUB_PREFIX", "sweep_rust_enum") + ".fallback reason=wheel")
    if os.environ.get("STUB_EMIT_DIVERGENCE") == "1":
        print(os.environ.get("STUB_PREFIX", "sweep_rust_enum") + ".divergence field=score")
if snap:
    (root / "stamp.json").write_text(json.dumps({"data_snapshot_hash": snap}))
print("arm", arm, "done")
if os.environ.get("STUB_CRASH_ARM") == arm:
    sys.exit(7)  # a crashing sweep — the MINOR-1 arm-error class
"""


def _run_flip_soak(
    tmp_path: Path,
    *,
    stub_env: dict[str, str] | None = None,
    flag: str = "BRUTEX_SWEEP_RUST_ENUM",
    extra_args: tuple[str, ...] = ("--skip-drift",),
    extra_flags_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run flip_soak.sh against the stub arm CLI in a scratch tree."""
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    out_dir = tmp_path / "out"
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("BRUTEX_SWEEP_RUST") or key.startswith("BRUTEX_TEST_DIFF"):
            env.pop(key)  # a clean ambient env — the harness refuses presets
    env["FLIP_SOAK_ARM_CMD"] = f"{sys.executable} {stub}"
    env["FLIP_SOAK_PYTHON"] = sys.executable
    if extra_flags_file is not None:
        env["FLIP_SOAK_EXTRA_FLAGS"] = str(extra_flags_file)
    env.update(stub_env or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), flag, str(lake), "--out-dir", str(out_dir), *extra_args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO),
        check=False,
    )


class TestFlipSoakScript:
    def test_happy_twin_pass(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path,
            stub_env={
                "STUB_EMIT_ENGAGED": "1",
                "STUB_SNAPSHOT_OFF": "snap777",
                "STUB_SNAPSHOT_ON": "snap777",
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FLIP-SOAK TWIN PASS" in result.stdout
        fragment = json.loads((tmp_path / "out" / "twin.json").read_text(encoding="utf-8"))
        assert fragment["verdict"] == "PASS"
        assert fragment["twin"]["byte_identical"] is True
        assert fragment["twin"]["files_compared"] == 1
        assert fragment["twin"]["snapshot_present"] is True
        assert fragment["engagement"]["engaged_count"] >= 1
        assert fragment["engagement"]["bad_markers_found"] == 0
        assert fragment["wall_clock"]["off_s"] >= 0.0

    def test_byte_identity_pass_requires_snapshots_on_both_arms(self, tmp_path: Path) -> None:
        """MAJOR-2: an empty snapshot on one arm must NOT emit a byte-identity PASS."""
        result = _run_flip_soak(
            tmp_path,
            stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_SNAPSHOT_OFF": "only-off"},
        )
        assert result.returncode == 9, result.stdout + result.stderr
        assert "snapshot_unavailable" in result.stdout
        fragment = json.loads((tmp_path / "out" / "twin.json").read_text(encoding="utf-8"))
        assert fragment["verdict"] == "FAIL"
        assert fragment["twin"]["snapshot_present"] is False

    def test_masked_pass_refused_on_bad_marker(self, tmp_path: Path) -> None:
        """The #1479 masked-PASS class: fallback/autorevert = oracle-served."""
        result = _run_flip_soak(tmp_path, stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_EMIT_BAD": "1"})
        assert result.returncode == 4, result.stdout + result.stderr
        assert "bad markers present" in result.stdout

    def test_vacuous_pass_refused_without_engagement(self, tmp_path: Path) -> None:
        result = _run_flip_soak(tmp_path)  # stub never prints .engaged
        assert result.returncode == 4, result.stdout + result.stderr
        assert "engaged marker absent" in result.stdout

    def test_zero_artifact_off_arm_is_fail(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path, stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_OFF_EMPTY": "1"}
        )
        assert result.returncode == 5, result.stdout + result.stderr
        assert "vacuous OFF arm" in result.stdout

    def test_byte_divergence_is_fail(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path, stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_ON_DIFFERENT": "1"}
        )
        assert result.returncode == 6, result.stdout + result.stderr
        assert "manifests differ" in result.stdout

    def test_preset_registry_flag_refused(self, tmp_path: Path) -> None:
        """The one-flag-at-a-time law — ANY registry flag, not just the DUT."""
        own = _run_flip_soak(tmp_path, stub_env={"BRUTEX_SWEEP_RUST_ENUM": "1"})
        assert own.returncode == 2
        assert "one-flag-at-a-time" in own.stderr
        other = _run_flip_soak(tmp_path, stub_env={"BRUTEX_SWEEP_WORKER_KERNEL_CACHE": "1"})
        assert other.returncode == 2
        assert "one-flag-at-a-time" in other.stderr

    def test_crashing_arm_reports_arm_error_exit_1(self, tmp_path: Path) -> None:
        """MINOR-1: a sweep crash surfaces as the documented arm-error exit 1.

        Not the child's raw exit code leaked through `set -e` + `pipefail`.
        """
        result = _run_flip_soak(
            tmp_path, stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_CRASH_ARM": "on"}
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "ON arm execution failed" in result.stderr

    def test_off_arm_crash_reports_arm_error_exit_1(self, tmp_path: Path) -> None:
        result = _run_flip_soak(tmp_path, stub_env={"STUB_CRASH_ARM": "off"})
        assert result.returncode == 1, result.stdout + result.stderr
        assert "OFF arm execution failed" in result.stderr

    def test_sub_minute_soak_refused_without_dry(self, tmp_path: Path) -> None:
        """MINOR-2: the terminal one-liner path refuses --minutes < 1 (the plan-job law)."""
        result = _run_flip_soak(
            tmp_path,
            stub_env={"STUB_EMIT_ENGAGED": "1"},
            extra_args=("--skip-drift", "--minutes", "0"),
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "--minutes must be >= 1" in result.stderr

    def test_missing_lake_root_refused(self, tmp_path: Path) -> None:
        stub = tmp_path / "stub.py"
        stub.write_text(_STUB, encoding="utf-8")
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("BRUTEX_SWEEP_RUST") or key.startswith("BRUTEX_TEST_DIFF"):
                env.pop(key)  # a clean ambient env — the harness refuses presets
        env["FLIP_SOAK_ARM_CMD"] = f"{sys.executable} {stub}"
        env["FLIP_SOAK_PYTHON"] = sys.executable
        result = subprocess.run(
            ["bash", str(_SCRIPT), "BRUTEX_SWEEP_RUST_ENUM", str(tmp_path / "no-such-lake")],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO),
            check=False,
        )
        assert result.returncode == 2
        assert "not a directory" in result.stderr

    def test_snapshot_moved_is_retry_env_moved_not_fail(self, tmp_path: Path) -> None:
        """A moved lake is RETRY_ENV_MOVED — never a kernel verdict."""
        result = _run_flip_soak(
            tmp_path,
            stub_env={
                "STUB_EMIT_ENGAGED": "1",
                "STUB_SNAPSHOT_OFF": "aaa111",
                "STUB_SNAPSHOT_ON": "bbb222",
            },
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "RETRY_ENV_MOVED" in result.stdout
        fragment = json.loads((tmp_path / "out" / "twin.json").read_text(encoding="utf-8"))
        assert fragment["verdict"] == "RETRY_ENV_MOVED"

    def test_equal_snapshots_pass_through(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path,
            stub_env={
                "STUB_EMIT_ENGAGED": "1",
                "STUB_SNAPSHOT_OFF": "same999",
                "STUB_SNAPSHOT_ON": "same999",
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        fragment = json.loads((tmp_path / "out" / "twin.json").read_text(encoding="utf-8"))
        assert fragment["twin"]["data_snapshot_hash_off"] == "same999"

    @staticmethod
    def _diff_row(tmp_path: Path) -> Path:
        rows = [
            {
                "flag": "BRUTEX_TEST_DIFF_FLAG",
                "flag_class": "DIFFERENTIAL",
                "surface_prefix": "test_diff",
                "twin_argv": ["true"],
                "artifact_globs": ["*.parquet"],
            }
        ]
        path = tmp_path / "extra_flags.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_differential_class_zero_divergence_passes(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path,
            flag="BRUTEX_TEST_DIFF_FLAG",
            stub_env={"STUB_EMIT_ENGAGED": "1", "STUB_PREFIX": "test_diff"},
            extra_flags_file=self._diff_row(tmp_path),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        fragment = json.loads((tmp_path / "out" / "twin.json").read_text(encoding="utf-8"))
        assert fragment["differential"] == {"divergence_count": 0}

    def test_differential_class_divergence_is_fail(self, tmp_path: Path) -> None:
        result = _run_flip_soak(
            tmp_path,
            flag="BRUTEX_TEST_DIFF_FLAG",
            stub_env={
                "STUB_EMIT_ENGAGED": "1",
                "STUB_EMIT_DIVERGENCE": "1",
                "STUB_PREFIX": "test_diff",
            },
            extra_flags_file=self._diff_row(tmp_path),
        )
        assert result.returncode == 7, result.stdout + result.stderr
        assert "divergences" in result.stdout

    def test_full_orchestration_dry_drift_emits_valid_pass_json(
        self, tmp_path: Path, emit_pass: ModuleType
    ) -> None:
        """Twin PASS + dry drift leg + emit_pass fold → schema-valid PASS."""
        result = _run_flip_soak(
            tmp_path,
            stub_env={
                "STUB_EMIT_ENGAGED": "1",
                "STUB_SNAPSHOT_OFF": "snap-x",
                "STUB_SNAPSHOT_ON": "snap-x",
            },
            extra_args=("--drift-dry", "--minutes", "0.01"),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FLIP-SOAK PASS" in result.stdout
        pass_path = tmp_path / "out" / "PASS.json"
        obj = json.loads(pass_path.read_text(encoding="utf-8"))
        assert emit_pass.validate_pass_obj(obj) == []
        assert obj["verdict"] == "PASS"
        assert obj["run_url"] == "terminal"
        assert obj["soak"]["mode"] == "dry"
        assert obj["soak"]["windows"] >= 1
        assert set(obj["ambient_flags"]) >= {"BRUTEX_SWEEP_RUST_ENUM"}
        # NIT-1: the RESOLVED grid dimensions are recorded beside the scale name.
        assert obj["grid_scale"] == "default"
        assert isinstance(obj["grid"], dict) and obj["grid"]


# ── 4. PASS.json schema (emit_pass.py) ───────────────────────────────


def _valid_pass_obj(emit_pass: ModuleType) -> dict[str, object]:
    registry = _load("flag_registry")
    # The one-flag-at-a-time proof: EXACTLY the registry flag set with the DUT
    # the only non-null value (§c of the validator).
    ambient: dict[str, object] = {spec.flag: None for spec in registry.all_specs()}
    ambient["BRUTEX_SWEEP_RUST_ENUM"] = "1"
    return {
        "schema_version": emit_pass.SCHEMA_VERSION,
        "flag": "BRUTEX_SWEEP_RUST_ENUM",
        "verdict": "PASS",
        "date_utc": "2026-07-15T00:00:00Z",
        "git_sha": "0" * 40,
        "branch": "claude/some-slice",
        "runner_name": "Parthibans-MacBook-Pro",
        "hardware": {"model": "arm64", "system": "Darwin", "ncpu": 14},
        "run_url": "terminal",
        "grid_scale": "default",
        "grid": {"symbols": "NIFTY,BANKNIFTY", "k": "2,5", "top": "1000"},
        "engagement": {"engaged_count": 3, "bad_markers_found": 0},
        "soak": {"windows": 4, "hard_gate_breaches": 0},
        "wall_clock": {"off_s": 10.0, "on_s": 9.5, "ratio": 0.95},
        "ambient_flags": ambient,
        "twin": {
            "off_manifest_sha256": "a" * 64,
            "on_manifest_sha256": "a" * 64,
            "files_compared": 3,
            "byte_identical": True,
            "data_snapshot_hash_off": "s1",
            "data_snapshot_hash_on": "s1",
            "snapshot_present": True,
        },
    }


class TestPassSchema:
    def test_valid_pass_round_trips(self, emit_pass: ModuleType) -> None:
        assert emit_pass.validate_pass_obj(_valid_pass_obj(emit_pass)) == []

    def test_missing_provenance_is_invalid(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        obj["run_url"] = ""
        errors = emit_pass.validate_pass_obj(obj)
        assert any("run_url" in e for e in errors)
        obj = _valid_pass_obj(emit_pass)
        obj["hardware"] = {}
        assert any("hardware" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_twin_byte_identical_false_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(a): a self-inconsistent byte_identical:false PASS is refused."""
        obj = _valid_pass_obj(emit_pass)
        twin = obj["twin"]
        assert isinstance(twin, dict)
        twin["byte_identical"] = False
        assert any("byte_identical == true" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_twin_empty_snapshot_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(b)/MAJOR-2: an empty snapshot on either arm invalidates a PASS."""
        obj = _valid_pass_obj(emit_pass)
        twin = obj["twin"]
        assert isinstance(twin, dict)
        twin["data_snapshot_hash_on"] = ""
        assert any("non-empty data_snapshot_hash" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_twin_unequal_snapshot_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(b): non-equal snapshots must not validate as a PASS."""
        obj = _valid_pass_obj(emit_pass)
        twin = obj["twin"]
        assert isinstance(twin, dict)
        twin["data_snapshot_hash_off"] = "s1"
        twin["data_snapshot_hash_on"] = "s2"
        assert any("equal data_snapshot_hash" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_ambient_two_non_null_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(c): the one-flag-at-a-time proof — a second non-null flag is refused."""
        obj = _valid_pass_obj(emit_pass)
        ambient = obj["ambient_flags"]
        assert isinstance(ambient, dict)
        ambient["BRUTEX_SWEEP_WORKER_KERNEL_CACHE"] = "1"
        assert any("EXACTLY ONE non-null" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_ambient_missing_dut_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(c): ambient_flags must record EXACTLY the registry flag set."""
        obj = _valid_pass_obj(emit_pass)
        ambient = obj["ambient_flags"]
        assert isinstance(ambient, dict)
        del ambient["BRUTEX_SWEEP_WORKER_KERNEL_CACHE"]  # drops a registry flag
        assert any("EXACTLY the registry flag set" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_ambient_wrong_dut_rejected(self, emit_pass: ModuleType) -> None:
        """MAJOR-1(c): the single non-null flag must be the flag under test."""
        obj = _valid_pass_obj(emit_pass)
        ambient = obj["ambient_flags"]
        assert isinstance(ambient, dict)
        ambient["BRUTEX_SWEEP_RUST_ENUM"] = None
        ambient["BRUTEX_SWEEP_WORKER_KERNEL_CACHE"] = "1"  # the wrong flag is on
        assert any("EXACTLY ONE non-null" in e for e in emit_pass.validate_pass_obj(obj))

    def test_grid_is_required(self, emit_pass: ModuleType) -> None:
        """NIT-1: the resolved grid dimensions must be recorded."""
        obj = _valid_pass_obj(emit_pass)
        del obj["grid"]
        assert any("grid" in e for e in emit_pass.validate_pass_obj(obj))
        obj = _valid_pass_obj(emit_pass)
        obj["grid"] = {}
        assert any("grid" in e for e in emit_pass.validate_pass_obj(obj))

    def test_twin_xor_differential(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        obj["differential"] = {"divergence_count": 0}
        assert any("exactly ONE" in e for e in emit_pass.validate_pass_obj(obj))
        del obj["twin"]
        assert emit_pass.validate_pass_obj(obj) == []
        del obj["differential"]
        assert any("exactly ONE" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_requires_engagement_and_clean_soak(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        obj["engagement"] = {"engaged_count": 0, "bad_markers_found": 0}
        assert any("never vacuous" in e for e in emit_pass.validate_pass_obj(obj))
        obj = _valid_pass_obj(emit_pass)
        obj["soak"] = {"windows": 4, "hard_gate_breaches": 1}
        assert any("hard_gate_breaches" in e for e in emit_pass.validate_pass_obj(obj))

    def test_pass_differential_requires_zero_divergences(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        del obj["twin"]
        obj["differential"] = {"divergence_count": 2}
        assert any("divergence_count == 0" in e for e in emit_pass.validate_pass_obj(obj))

    def test_vacuity_guard_in_schema(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        twin = obj["twin"]
        assert isinstance(twin, dict)
        twin["files_compared"] = 0
        assert any("vacuity" in e for e in emit_pass.validate_pass_obj(obj))

    def test_bad_verdict_and_schema_version(self, emit_pass: ModuleType) -> None:
        obj = _valid_pass_obj(emit_pass)
        obj["verdict"] = "MAYBE"
        assert any("verdict" in e for e in emit_pass.validate_pass_obj(obj))
        obj = _valid_pass_obj(emit_pass)
        obj["schema_version"] = 99
        assert any("schema_version" in e for e in emit_pass.validate_pass_obj(obj))
        assert emit_pass.validate_pass_obj([]) != []

    def test_fail_verdict_refused_under_docs_soak(
        self, emit_pass: ModuleType, tmp_path: Path
    ) -> None:
        """The PASS-only evidence-home law — mechanical, not advisory."""
        twin = {
            "verdict": "FAIL",
            "scale": "default",
            "engagement": {"engaged_count": 1, "bad_markers_found": 0},
            "twin": {
                "off_manifest_sha256": "a" * 64,
                "on_manifest_sha256": "b" * 64,
                "files_compared": 1,
                "byte_identical": False,
                "data_snapshot_hash_off": "",
                "data_snapshot_hash_on": "",
            },
            "wall_clock": {"off_s": 1.0, "on_s": 1.0, "ratio": 1.0},
        }
        twin_path = tmp_path / "twin.json"
        twin_path.write_text(json.dumps(twin), encoding="utf-8")
        evidence = tmp_path / "docs" / "soak" / "BRUTEX_SWEEP_RUST_ENUM" / "2026-07-15-PASS.json"
        rc = emit_pass.main(
            [
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--twin",
                str(twin_path),
                "--out",
                str(evidence),
                "--git-sha",
                "f" * 40,
                "--branch",
                "test",
            ]
        )
        assert rc == 3
        assert not evidence.exists(), "a FAIL artifact must never land in the evidence home"

    def test_committed_evidence_all_validates(self, emit_pass: ModuleType) -> None:
        """Every committed docs/soak evidence file must be a schema-valid PASS.

        Vacuously green until the first operator evidence PR lands (the
        directory ships EMPTY of evidence by design — no fabricated PASS).
        """
        evidence_files = [p for p in (_REPO / "docs" / "soak").rglob("*.json") if p.is_file()]
        for path in evidence_files:
            obj = json.loads(path.read_text(encoding="utf-8"))
            assert emit_pass.validate_pass_obj(obj) == [], f"invalid evidence: {path}"
            assert obj["verdict"] == "PASS", f"non-PASS evidence committed: {path}"

    def test_validate_cli_mode(self, emit_pass: ModuleType, tmp_path: Path) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_valid_pass_obj(emit_pass)), encoding="utf-8")
        assert emit_pass.main(["--validate", str(good)]) == 0
        bad = tmp_path / "docs" / "soak" / "X" / "bad.json"
        bad.parent.mkdir(parents=True)
        obj = _valid_pass_obj(emit_pass)
        obj["verdict"] = "FAIL"
        bad.write_text(json.dumps(obj), encoding="utf-8")
        assert emit_pass.main(["--validate", str(bad)]) == 2
        garbage = tmp_path / "garbage.json"
        garbage.write_text("{not json", encoding="utf-8")
        assert emit_pass.main(["--validate", str(garbage)]) == 2


# ── 5. The drift-leg driver (dry / fake-lake mode) ───────────────────


class TestDriftDriverDry:
    def test_dry_run_produces_clean_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The conftest wave OFF-belt pins BRUTEX_SWEEP_RUST_ENUM="0" in the
        # ambient env; the driver's one-flag-at-a-time law refuses a pre-set
        # registry flag. Present a clean env (the harness's own contract).
        monkeypatch.delenv("BRUTEX_SWEEP_RUST_ENUM", raising=False)
        driver = _load("flip_soak_driver")
        out = tmp_path / "drift.json"
        rc = driver.main(
            [
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--minutes",
                "0.01",
                "--lake-root",
                str(tmp_path),
                "--results-root",
                str(tmp_path / "drift"),
                "--out",
                str(out),
                "--dry",
                "--rss-ceiling-kib",
                # Float the absolute RSS gate above this pytest shard's ambient
                # footprint — this test gates on the soak LOGIC, not on the
                # enclosing process's absolute RSS (see _logic_ceiling_kib).
                str(_logic_ceiling_kib(driver)),
                "--max-windows",
                "3",
                "--window-seconds",
                "0.01",
            ]
        )
        assert rc == 0
        fragment = json.loads(out.read_text(encoding="utf-8"))
        assert fragment["mode"] == "dry"
        assert fragment["windows"] >= 1
        assert fragment["hard_gate_breaches"] == 0
        assert fragment["run_failures"] == 0

    def test_dry_run_rss_ceiling_breach_fails_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 1-KiB ceiling must breach on any real process — the hard gate."""
        # Clean env (the conftest wave OFF-belt pins the flag; the driver
        # refuses a pre-set registry flag — the one-flag-at-a-time law).
        monkeypatch.delenv("BRUTEX_SWEEP_RUST_ENUM", raising=False)
        driver = _load("flip_soak_driver")
        out = tmp_path / "drift.json"
        rc = driver.main(
            [
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--minutes",
                "0.01",
                "--lake-root",
                str(tmp_path),
                "--results-root",
                str(tmp_path / "drift"),
                "--out",
                str(out),
                "--dry",
                "--max-windows",
                "2",
                "--window-seconds",
                "0.01",
                "--rss-ceiling-kib",
                "1",
            ]
        )
        fragment = json.loads(out.read_text(encoding="utf-8"))
        if fragment["rss_peak_kib"] is None:
            pytest.skip("process probe abstained on this platform — no RSS oracle")
        assert rc == 1
        assert fragment["hard_gate_breaches"] >= 1
        assert "soak.rss_ceiling" in fragment["breach_rules"]

    def test_preset_flag_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _load("flip_soak_driver")
        monkeypatch.setenv("BRUTEX_SWEEP_WORKER_KERNEL_CACHE", "1")
        rc = driver.main(
            [
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--minutes",
                "0.01",
                "--lake-root",
                str(tmp_path),
                "--results-root",
                str(tmp_path / "d"),
                "--out",
                str(tmp_path / "o.json"),
                "--dry",
            ]
        )
        assert rc == 2

    def test_unknown_flag_and_missing_lake_refused(self, tmp_path: Path) -> None:
        driver = _load("flip_soak_driver")
        rc = driver.main(
            [
                "--flag",
                "BRUTEX_NO_SUCH",
                "--minutes",
                "0.01",
                "--lake-root",
                str(tmp_path),
                "--results-root",
                str(tmp_path / "d"),
                "--out",
                str(tmp_path / "o.json"),
                "--dry",
            ]
        )
        assert rc == 2
        rc = driver.main(
            [
                "--flag",
                "BRUTEX_SWEEP_RUST_ENUM",
                "--minutes",
                "0.01",
                "--lake-root",
                str(tmp_path / "missing"),
                "--results-root",
                str(tmp_path / "d"),
                "--out",
                str(tmp_path / "o.json"),
            ]
        )
        assert rc == 2

    def test_hard_gate_rules_exclude_p99(self) -> None:
        """The DEC-SOAK-REAL-TIER-001 law: p99 is observe-only end-to-end."""
        driver = _load("flip_soak_driver")
        assert "soak.p99_drift" not in driver.HARD_GATE_RULE_NAMES
        assert set(driver.HARD_GATE_RULE_NAMES) == {"soak.rss_ceiling", "soak.fd_growth"}


# ── 6. The drift-leg RETRY_ENV_MOVED path (security MAJOR-1) ──────────


class _DriftLegResult(Protocol):
    """Structural view of ``flip_soak_driver.DriftResult`` — the fields read here.

    The driver module is loaded dynamically (``_load``), so its ``DriftResult``
    dataclass is not statically importable; this Protocol gives pyright the
    exact attribute surface these tests consume.
    """

    env_retries: int
    run_failures: int
    hard_gate_breaches: int


class TestDriftRetryEnvMoved:
    """A transient env move classifies RETRY, never a kernel FAIL."""

    @staticmethod
    def _leg(driver: ModuleType, tmp_path: Path, runner: object) -> _DriftLegResult:
        return driver.run_drift_leg(
            flag="BRUTEX_SWEEP_RUST_ENUM",
            minutes=0.01,
            lake_root=str(tmp_path),
            results_root=tmp_path / "d",
            scale="default",
            dry=True,  # dry so the ambient env is never mutated; runner overrides
            max_windows=2,
            # Float the absolute RSS gate above this pytest shard's ambient
            # footprint — these legs gate on the soak LOGIC (retry/breach
            # counting), not on the enclosing process's absolute RSS
            # (see _logic_ceiling_kib).
            rss_ceiling_kib=_logic_ceiling_kib(driver),
            window_runner=runner,  # type: ignore[arg-type]
        )

    def test_is_lake_busy_matches_by_name(self) -> None:
        driver = _load("flip_soak_driver")

        class LakeBusyError(Exception):  # name-matched, not the real class
            pass

        assert driver._is_lake_busy(LakeBusyError("busy"))
        assert not driver._is_lake_busy(ValueError("nope"))

    def test_simulated_lake_busy_window_is_retry_not_fail(self, tmp_path: Path) -> None:
        """A LakeBusy-classified window → env_retries, zero run_failures."""
        driver = _load("flip_soak_driver")

        def runner(_index: int) -> tuple[list[float], str, str]:
            return [0.001], driver.WINDOW_ENV_MOVED, ""

        result = self._leg(driver, tmp_path, runner)
        assert result.env_retries >= 1
        assert result.run_failures == 0
        assert result.hard_gate_breaches == 0

    def test_per_window_snapshot_move_is_env_move(self, tmp_path: Path) -> None:
        """A data_snapshot_hash that moves between windows classifies ENV_MOVED."""
        driver = _load("flip_soak_driver")
        snaps = iter(["base111", "moved222", "moved222", "moved222"])

        def runner(_index: int) -> tuple[list[float], str, str]:
            return [0.001], driver.WINDOW_OK, next(snaps)

        result = self._leg(driver, tmp_path, runner)
        assert result.env_retries >= 1
        assert result.run_failures == 0

    def test_real_kernel_failure_still_fails(self, tmp_path: Path) -> None:
        """A genuine non-zero kernel rc counts as a run failure that FAILs the leg."""
        driver = _load("flip_soak_driver")

        def runner(_index: int) -> tuple[list[float], str, str]:
            return [0.001], driver.WINDOW_KERNEL_FAIL, ""

        result = self._leg(driver, tmp_path, runner)
        assert result.run_failures == 1
        assert result.env_retries == 0

    def test_emit_pass_folds_env_retries_to_retry(self, emit_pass: ModuleType) -> None:
        twin = {
            "verdict": "PASS",
            "scale": "default",
            "engagement": {"engaged_count": 1, "bad_markers_found": 0},
            "wall_clock": {"off_s": 1.0, "on_s": 1.0, "ratio": 1.0},
            "twin": {
                "off_manifest_sha256": "a" * 64,
                "on_manifest_sha256": "a" * 64,
                "files_compared": 1,
                "byte_identical": True,
                "data_snapshot_hash_off": "s",
                "data_snapshot_hash_on": "s",
                "snapshot_present": True,
            },
        }
        env_moved = {"hard_gate_breaches": 0, "run_failures": 0, "env_retries": 2, "windows": 3}
        obj = emit_pass.assemble(
            flag="BRUTEX_SWEEP_RUST_ENUM",
            twin=twin,
            drift=env_moved,
            run_url="terminal",
            minutes="1",
            git_sha="f" * 40,
            branch="test",
        )
        assert obj["verdict"] == "RETRY_ENV_MOVED"
        # A genuine kernel failure still FAILs; a clean drift PASSes.
        failed = dict(env_moved, run_failures=1, env_retries=0)
        assert (
            emit_pass.assemble(
                flag="BRUTEX_SWEEP_RUST_ENUM",
                twin=twin,
                drift=failed,
                run_url="terminal",
                minutes="1",
            )["verdict"]
            == "FAIL"
        )
        clean = dict(env_moved, env_retries=0)
        assert (
            emit_pass.assemble(
                flag="BRUTEX_SWEEP_RUST_ENUM",
                twin=twin,
                drift=clean,
                run_url="terminal",
                minutes="1",
            )["verdict"]
            == "PASS"
        )
