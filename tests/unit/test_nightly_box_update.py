"""Unit matrix for the nightly box auto-deploy leg (DEC-BOX-AUTODEPLOY-LANE-001).

Harness style: the injectable :class:`CommandRunner` seam (the design §10 law —
tests never exec git/maturin/pip; the scripted :class:`FakeRunner` records
every argv).  Real-git integration lives in
``tests/integration/test_box_update_git.py``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex.nightly import box_update as bu
from brutex.nightly.box_update import (
    BOX_UPDATE_STATE_ENV,
    WHEEL_SET,
    BoxUpdateEnvError,
    BoxUpdateOutcome,
    BoxUpdateStatus,
    WheelBuildResult,
    adopt_state,
    classify_delta,
    default_command_runner,
    guard_skip_reason,
    maybe_reexec,
    resolve_box_update_enabled,
    run_box_update,
)
from structlog.testing import capture_logs

FROM_SHA = "a" * 40
TO_SHA = "b" * 40

_PASS_CHECKS = [
    ["wheel_freshness", "PASS", "ok"],
    ["wheel_freshness_fno_math", "PASS", "ok"],
    ["wheel_source_digest", "PASS", "ok"],
    ["wheel_source_digest_fno_math", "PASS", "ok"],
]


def _gate_stdout(checks: Sequence[Sequence[str]] = _PASS_CHECKS) -> str:
    return f"{bu._IMPORT_OK_MARKER}\n{bu._CHECKS_MARKER}{json.dumps(list(checks))}\n"


class FakeRunner:
    """Scripted CommandRunner — records every call; rules matched newest-first."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rules: list[
            tuple[
                Callable[[list[str]], bool],
                tuple[int, str, str] | Callable[[list[str]], tuple[int, str, str]],
            ]
        ] = []
        self.state: dict[str, Any] = {}

    def on(
        self,
        pred: Callable[[list[str]], bool],
        resp: tuple[int, str, str] | Callable[[list[str]], tuple[int, str, str]],
    ) -> FakeRunner:
        self.rules.insert(0, (pred, resp))
        return self

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        call_argv = [str(a) for a in argv]
        self.calls.append({"argv": call_argv, "cwd": cwd, "timeout_s": timeout_s, "env": env})
        for pred, resp in self.rules:
            if pred(call_argv):
                return resp(call_argv) if callable(resp) else resp
        raise AssertionError(f"unscripted argv: {call_argv}")

    # ── argv census helpers ──
    def argvs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls]

    def git_calls(self, *prefix: str) -> list[list[str]]:
        want = ["git", *prefix]
        return [a for a in self.argvs() if a[: len(want)] == want]

    def mutating_git_calls(self) -> list[list[str]]:
        return [a for a in self.argvs() if a[0] == "git" and a[1] in ("merge", "checkout", "reset")]

    def maturin_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["argv"][0].endswith("maturin")]

    def pip_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["argv"][0].endswith("pip") and c["argv"][1] == "install"]

    def child_calls(self, needle: str) -> list[dict[str, Any]]:
        return [
            c
            for c in self.calls
            if c["argv"][0] == sys.executable
            and len(c["argv"]) >= 3
            and c["argv"][1] == "-c"
            and needle in c["argv"][2]
        ]

    def gate_calls(self) -> list[dict[str, Any]]:
        # The §4.5 GATE child — the smoke-INCLUDING doctor subprocess.
        return [
            c for c in self.child_calls("brutex.doctor") if bu.GATE_SMOKE_IMPORTS in c["argv"][2]
        ]

    def coherence_calls(self) -> list[dict[str, Any]]:
        # The §4.6 coherence-check child — the smoke-LESS doctor subprocess.
        return [
            c
            for c in self.child_calls("brutex.doctor")
            if bu.GATE_SMOKE_IMPORTS not in c["argv"][2]
        ]

    def smoke_calls(self) -> list[dict[str, Any]]:
        return self.child_calls("import trading_core, fno_math")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for spec in WHEEL_SET:
        lock = repo / spec.crate_dir / "Cargo.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"# lock {spec.name}\n", encoding="utf-8")
    return repo


def _std_runner(
    repo: Path,
    *,
    diff_paths: Sequence[str] = ("src/x.py",),
    gate: tuple[int, str, str] | None = None,
) -> FakeRunner:
    """The happy-path script: clean tree, origin ahead, everything green."""
    r = FakeRunner()
    r.state["head"] = FROM_SHA

    def _merge(_a: list[str]) -> tuple[int, str, str]:
        r.state["head"] = TO_SHA
        return (0, "", "")

    def _checkout_b(a: list[str]) -> tuple[int, str, str]:
        r.state["head"] = a[3]
        return (0, "", "")

    r.on(lambda a: a[:3] == ["git", "rev-parse", "--show-toplevel"], (0, f"{repo}\n", ""))
    r.on(
        lambda a: a[:3] == ["git", "rev-parse", "HEAD"],
        lambda _a: (0, str(r.state["head"]) + "\n", ""),
    )
    r.on(lambda a: a[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"], (0, "main\n", ""))
    r.on(lambda a: a[:3] == ["git", "fetch", "origin"], (0, "", ""))
    r.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, TO_SHA + "\n", ""))
    r.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (0, "", ""))
    r.on(lambda a: a[:3] == ["git", "merge-base", "--is-ancestor"], (0, "", ""))
    r.on(lambda a: a[:3] == ["git", "merge", "--ff-only"], _merge)
    r.on(
        lambda a: a[:3] == ["git", "diff", "--name-only"],
        (0, "\n".join(diff_paths) + "\n", ""),
    )
    r.on(lambda a: a[:4] == ["git", "checkout", "-B", "main"], _checkout_b)
    r.on(lambda a: a[:3] == ["git", "checkout", "-f"], (0, "", ""))
    r.on(lambda a: a[0].endswith("maturin"), (0, "", ""))
    r.on(lambda a: a[0].endswith("pip"), (0, "", ""))
    r.on(
        lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
        gate if gate is not None else (0, _gate_stdout(), ""),
    )
    r.on(
        lambda a: (
            a[0] == sys.executable
            and a[1] == "-c"
            and a[2].startswith("import ")
            and "brutex.doctor" not in a[2]
        ),
        (0, "", ""),
    )
    return r


def _run(
    repo: Path,
    runner: FakeRunner,
    *,
    env: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
) -> BoxUpdateOutcome:
    return run_box_update(
        repo_root=repo,
        runner=runner,
        env=dict(env or {}),
        state_dir=state_dir,
        sleep=lambda _s: None,
    )


# ── kill switch + env twins (§7; tests 9 + twins) ────────────────────────
class TestKillSwitchAndTwins:
    def test_unset_and_truthy_enable(self) -> None:
        assert resolve_box_update_enabled({}) is True
        for v in ("1", "true", "YES", " on "):
            assert resolve_box_update_enabled({"BRUTEX_BOX_UPDATE": v}) is True

    def test_falsy_disables(self) -> None:
        for v in ("0", "false", "NO", " off "):
            assert resolve_box_update_enabled({"BRUTEX_BOX_UPDATE": v}) is False

    def test_junk_raises_env_error(self) -> None:
        with pytest.raises(BoxUpdateEnvError):
            resolve_box_update_enabled({"BRUTEX_BOX_UPDATE": "banana"})

    def test_env_error_is_a_value_error(self) -> None:
        # The FillModeRemovedError precedent — the CLI's existing
        # (NightlyEnvError, ValueError) catch must always cover it.
        assert issubclass(BoxUpdateEnvError, ValueError)

    def test_truthy_falsy_twins_equality_pinned(self) -> None:
        from brutex.engine import sweep_lookahead

        assert bu._TRUTHY == sweep_lookahead._TRUTHY
        assert bu._FALSY == sweep_lookahead._FALSY

    @pytest.mark.parametrize("raw", [None, "", "  ", "junk", "-1", "0", "nan", "inf", "12.5"])
    def test_resolve_env_seconds_twin_equality_pinned(self, raw: str | None) -> None:
        from brutex.config.deadline import resolve_env_seconds

        env = {} if raw is None else {"X_T": raw}
        assert bu._resolve_env_seconds(env, "X_T", 60.0) == resolve_env_seconds(
            "X_T", 60.0, raw=raw
        )

    def test_disabled_run_is_zero_actions(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = FakeRunner()  # any call would raise "unscripted argv"
        out = _run(repo, runner, env={"BRUTEX_BOX_UPDATE": "off"}, state_dir=tmp_path / "s")
        assert out.status is BoxUpdateStatus.DISABLED
        assert runner.calls == []
        assert out.gate == "skipped"
        assert not (tmp_path / "s").exists()  # zero state-dir I/O


# ── pure decision core ───────────────────────────────────────────────────
class TestPureCore:
    def test_guard_skip_reason_truth_table(self) -> None:
        ok = {"branch": "main", "porcelain": [], "ancestor_rc": 0}
        assert guard_skip_reason(**ok) is None  # type: ignore[arg-type]
        assert guard_skip_reason(branch="HEAD", porcelain=[], ancestor_rc=0) == "detached_head"
        assert guard_skip_reason(branch="dev", porcelain=[], ancestor_rc=0) == "not_on_main"
        assert (
            guard_skip_reason(branch="main", porcelain=[" M x.py"], ancestor_rc=0) == "dirty_tree"
        )
        assert (
            guard_skip_reason(branch="main", porcelain=["?? scratch"], ancestor_rc=0) is None
        )  # untracked-only never blocks (§2 G5)
        assert guard_skip_reason(branch="main", porcelain=[], ancestor_rc=1) == "diverged"
        assert (
            guard_skip_reason(branch="main", porcelain=[], ancestor_rc=2) == "ancestor_check_failed"
        )

    def test_guard_skip_reason_negative_rc_is_ancestor_check_failed(self) -> None:
        # The delegation-alignment pin: a NEGATIVE (signal-kill) merge-base rc
        # folds ancestor_check_failed — never a fail-open proceed (the core is
        # the ONE truth table run_box_update's G6 now delegates to).
        assert (
            guard_skip_reason(branch="main", porcelain=[], ancestor_rc=-9)
            == "ancestor_check_failed"
        )

    def test_run_delegates_guard_verdicts_to_the_pure_core(self, tmp_path: Path) -> None:
        # The drift-risk NIT closure: run_box_update's G2/G5/G6 verdicts come
        # FROM guard_skip_reason (spy-pinned — one truth table, no inline twin).
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        calls: list[dict[str, Any]] = []
        real = bu.guard_skip_reason

        def _spy(**kwargs: Any) -> str | None:
            calls.append(dict(kwargs))
            return real(**kwargs)

        with mock.patch.object(bu, "guard_skip_reason", _spy):
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        # One delegation per guard phase: G2 (branch), G5 (porcelain), G6 (rc).
        assert [c["branch"] for c in calls] == ["main", "main", "main"]
        assert len(calls) == 3

    def test_classify_delta_patterns(self) -> None:
        assert classify_delta(["rust/trading-core/src/lib.rs"]) == bu.DeltaClass(True, False)
        assert classify_delta(["rust/fno-math/Cargo.lock"]) == bu.DeltaClass(True, False)
        assert classify_delta(["Cargo.lock"]) == bu.DeltaClass(True, False)  # belt
        assert classify_delta(["sub/dir/Cargo.toml"]) == bu.DeltaClass(True, False)  # belt
        assert classify_delta(["pyproject.toml"]) == bu.DeltaClass(False, True)
        assert classify_delta(["rust/fno-math/pyproject.toml"]) == bu.DeltaClass(True, False)
        # Deliberately NOT matched (§4.4).
        assert classify_delta(["Makefile", "up.sh", ".github/workflows/ci.yml"]) == (
            bu.DeltaClass(False, False)
        )
        assert classify_delta(["brutex/cli.py", "docs/x.md"]) == bu.DeltaClass(False, False)
        # A crate-pyproject path is NOT the root pyproject.
        assert classify_delta(["sub/pyproject.toml"]) == bu.DeltaClass(False, False)

    def test_wheel_set_is_the_makefile_mirror(self) -> None:
        # §10 test 16 — parse the Makefile run-raw maturin lines; set equality.
        makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
        crates: dict[str, bool] = {}
        for block in makefile.split("@$(VPY) -c")[1:]:
            if "maturin" not in block or "cd rust/" not in block:
                continue
            crate_dir = "rust/" + block.split("cd rust/", 1)[1].split(" ", 1)[0]
            warn_only = "|| echo" in block.split("maturin", 2)[-1]
            crates[crate_dir] = not warn_only
        assert crates == {s.crate_dir: s.required for s in WHEEL_SET}

    def test_outcome_to_dict_key_order_and_derivations(self) -> None:
        wheel = WheelBuildResult(
            name="fno_math",
            crate_dir="rust/fno-math",
            required=True,
            result="ok",
            digest_ok=True,
            duration_s=1.5,
            detail="",
        )
        out = BoxUpdateOutcome(
            status=BoxUpdateStatus.UPDATED,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            running_sha=FROM_SHA,
            skip_reason=None,
            delta_rust=True,
            delta_pyproject=False,
            wheels=(wheel,),
            gate="ok",
            repair="not_needed",
            pip_restore="not_needed",
            lock_restored=(),
            quarantined=(),
            alarm_signature=None,
            alarm_repeats=0,
            reexec=False,
            duration_s=2.0,
            detail="",
        )
        d = out.to_dict()
        assert list(d) == [
            "status",
            "updated",
            "from_sha",
            "to_sha",
            "running_sha",
            "reexec",
            "skip_reason",
            "delta_rust",
            "delta_pyproject",
            "wheels",
            "gate",
            "repair",
            "pip_restore",
            "lock_restored",
            "quarantined",
            "alarm_signature",
            "alarm_repeats",
            "duration_s",
            "detail",
        ]
        assert d["updated"] is True  # DERIVED: status == updated (§5 freeze)
        wheels = d["wheels"]
        assert isinstance(wheels, list)
        assert list(wheels[0]) == [
            "name",
            "crate_dir",
            "required",
            "result",
            "rebuilt",
            "digest_ok",
            "duration_s",
            "detail",
        ]
        assert wheels[0]["rebuilt"] is True  # DERIVED: result == "ok"
        failed = WheelBuildResult(
            name="x",
            crate_dir="y",
            required=True,
            result="failed",
            digest_ok=None,
            duration_s=0.0,
            detail="boom",
        )
        assert failed.to_dict()["rebuilt"] is False


# ── guard-refusal matrix (§10 test 1) ────────────────────────────────────
class TestGuardMatrix:
    def _assert_skip(self, out: BoxUpdateOutcome, runner: FakeRunner, reason: str) -> None:
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == reason
        assert runner.mutating_git_calls() == []  # no merge/checkout/reset ever
        assert runner.maturin_calls() == []
        assert out.gate == "skipped"

    def test_g1_toplevel_rc_nonzero(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "--show-toplevel"], (128, "", "fatal"))
        out = _run(repo, runner)
        self._assert_skip(out, runner, "not_a_repo_checkout")

    def test_g1_toplevel_mismatch(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        other = tmp_path / "elsewhere"
        other.mkdir()
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:3] == ["git", "rev-parse", "--show-toplevel"],
            (0, f"{other}\n", ""),
        )
        out = _run(repo, runner)
        self._assert_skip(out, runner, "not_a_repo_checkout")

    def test_g2_detached_head(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            (0, "HEAD\n", ""),
        )
        out = _run(repo, runner)
        self._assert_skip(out, runner, "detached_head")

    def test_g2_not_on_main(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            (0, "claude/feature\n", ""),
        )
        out = _run(repo, runner)
        self._assert_skip(out, runner, "not_on_main")
        assert out.running_sha == FROM_SHA  # running_sha == from_sha on skips

    @pytest.mark.parametrize("rc", [1, 124])
    def test_g3_fetch_fails_twice_with_backoff(self, tmp_path: Path, rc: int) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "fetch", "origin"], (rc, "", "network down"))
        sleeps: list[float] = []
        out = run_box_update(
            repo_root=repo, runner=runner, env={}, state_dir=None, sleep=sleeps.append
        )
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "fetch_failed"
        assert len(runner.git_calls("fetch", "origin")) == bu.FETCH_ATTEMPTS
        assert sleeps == [bu.FETCH_BACKOFF_S]  # backoff BETWEEN attempts only
        assert runner.mutating_git_calls() == []

    def test_g4_rev_parse_origin_fails(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (128, "", "x"))
        out = _run(repo, runner)
        self._assert_skip(out, runner, "rev_parse_failed")

    def test_g5_dirty_tree_blocks_with_evidence(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M brutex/cli.py\n?? scratch.txt\n", ""),
        )
        out = _run(repo, runner)
        self._assert_skip(out, runner, "dirty_tree")
        assert "brutex/cli.py" in out.detail

    def test_g5_untracked_only_proceeds(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)

        def _porcelain(_a: list[str]) -> tuple[int, str, str]:
            # Clean AFTER the merge (post-merge verify), untracked-only before.
            if runner.state.get("head") == TO_SHA:
                return (0, "?? scratch.txt\n", "")
            return (0, "?? scratch.txt\n", "")

        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], _porcelain)
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert "untracked_files=1" in out.detail

    @pytest.mark.parametrize(("rc", "reason"), [(1, "diverged"), (2, "ancestor_check_failed")])
    def test_g6_ancestor_verdicts(self, tmp_path: Path, rc: int, reason: str) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "merge-base", "--is-ancestor"], (rc, "", ""))
        out = _run(repo, runner)
        self._assert_skip(out, runner, reason)

    def test_merge_refused_is_a_skip_with_no_mutation_after(self, tmp_path: Path) -> None:
        # §10 test 25 — the §3 arm, independent of the §2 guard matrix.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:3] == ["git", "merge", "--ff-only"],
            (1, "", "error: untracked working tree files would be overwritten"),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "merge_refused"
        assert runner.git_calls("checkout") == []  # git aborted pre-mutation

    def test_postmerge_verify_failure_rolls_back(self, tmp_path: Path) -> None:
        # §10 test 1a — HEAD lands on the WRONG sha after the merge.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)

        def _merge(_a: list[str]) -> tuple[int, str, str]:
            runner.state["head"] = "c" * 40  # NOT to_sha
            return (0, "", "")

        runner.on(lambda a: a[:3] == ["git", "merge", "--ff-only"], _merge)
        out = _run(repo, runner)
        assert out.skip_reason == "postmerge_verify_failed"
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert runner.git_calls("checkout", "-B", "main") != []

    def test_delta_classify_failure_is_conservative(self, tmp_path: Path) -> None:
        # §10 test 1b — rust+pyproject conservative; ALL builds + pip issued.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "diff", "--name-only"], (128, "", "boom"))
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert out.delta_rust is True and out.delta_pyproject is True
        assert len(runner.maturin_calls()) == len(WHEEL_SET)
        assert len(runner.pip_calls()) == 1
        assert any(e["event"] == "box_update.delta_classify_failed" for e in logs)


# ── wheel builds (§10 tests 5/6) ─────────────────────────────────────────
class TestWheelBuilds:
    def test_python_only_delta_builds_nothing_gate_still_runs(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("brutex/cli.py",))
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert runner.maturin_calls() == []
        assert runner.pip_calls() == []
        assert len(runner.gate_calls()) == 1  # the lockstep-hole closer (§4.5)
        assert out.gate == "ok"

    def test_rust_delta_builds_wheel_set_in_bootstrap_order(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        calls = runner.maturin_calls()
        assert [c["cwd"] for c in calls] == [repo / s.crate_dir for s in WHEEL_SET]
        for call in calls:
            # BARE build — argv-exact; a --locked token anywhere FAILS the pin.
            assert call["argv"][1:] == ["develop", "--release"]
            assert "--locked" not in call["argv"]
            env = call["env"]
            assert env is not None
            assert env["VIRTUAL_ENV"] == str(Path(sys.executable).parent.parent)
            assert str(Path.home() / ".cargo" / "bin") in env["PATH"]
        assert runner.pip_calls() == []
        assert [w.result for w in out.wheels] == ["ok", "ok", "ok"]

    def test_restore_porcelain_read_after_every_maturin_call(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        _run(repo, runner)
        argvs = runner.argvs()
        maturin_idx = [i for i, a in enumerate(argvs) if a[0].endswith("maturin")]
        porcelain_idx = [
            i for i, a in enumerate(argvs) if a[:3] == ["git", "status", "--porcelain"]
        ]
        for i in maturin_idx:
            assert any(j > i for j in porcelain_idx)  # a fresh porcelain follows each build

    def test_pyproject_delta_runs_pip_only(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("pyproject.toml",))
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert runner.maturin_calls() == []
        pips = runner.pip_calls()
        assert len(pips) == 1
        assert pips[0]["argv"][1:] == ["install", "-e", ".[data,ui,groww]"]
        assert pips[0]["cwd"] == repo

    def test_optional_wheel_failure_never_rolls_back(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/brutex-feed/src/lib.rs",))
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (0, "", ""),
        )

        def _maturin(call_env: dict[str, Any]) -> tuple[int, str, str]:
            return (0, "", "")

        # brutex-feed (optional) fails; required crates build fine.
        feed_dir = repo / "rust/brutex-feed"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "feed boom") if runner.calls[-1]["cwd"] == feed_dir else (0, "", ""),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert runner.git_calls("checkout", "-B") == []
        by_name = {w.name: w for w in out.wheels}
        assert by_name["brutex_feed"].result == "failed"
        assert by_name["brutex_feed"].detail == "feed boom"

    def test_required_wheel_failure_short_circuits_into_rollback(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "build boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.running_sha == FROM_SHA
        # fno_math is FIRST in bootstrap order — the failure short-circuits
        # trading_core/brutex_feed straight into the rollback (§4.5).
        assert len(runner.maturin_calls()) == 1
        assert runner.git_calls("checkout", "-B", "main") != []
        # The §4.5 gate subprocess never ran ⇒ gate="skipped" (§1 emission law).
        assert runner.gate_calls() == []
        assert out.gate == "skipped"

    def test_required_timeout_arm_reports_timeout(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (124, "", "") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        by_name = {w.name: w for w in out.wheels}
        assert by_name["fno_math"].result == "timeout"


# ── gate arms (§10 tests 7/17) ───────────────────────────────────────────
class TestAssertGreenGate:
    def test_import_failed_rolls_back(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, gate=(1, "", "ModuleNotFoundError: x"))
        out = _run(repo, runner)
        assert out.gate == "import_failed"
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert runner.git_calls("checkout", "-B", "main") != []

    def test_doctor_warn_is_stale(self, tmp_path: Path) -> None:
        checks = [
            ["wheel_freshness", "WARN", "symbol drift"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "WARN", "digest mismatch"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, gate=(0, _gate_stdout(checks), ""))
        # The post-rollback coherence check leg reads the SAME stale checks
        # against the rolled-back tree → the mismatch repair fires; make the
        # RE-check read PASS (the repaired-crate re-verify).
        runner.state["checks_calls"] = 0

        def _gate(_a: list[str]) -> tuple[int, str, str]:
            runner.state["checks_calls"] += 1
            if runner.state["checks_calls"] == 1:
                return (0, _gate_stdout(checks), "")
            return (0, _gate_stdout(), "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _gate,
        )
        out = _run(repo, runner)
        assert out.gate == "stale"
        assert out.status is BoxUpdateStatus.ROLLED_BACK

    def test_child_crash_after_clean_import_is_error(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, gate=(1, f"{bu._IMPORT_OK_MARKER}\n", "boom"))
        out = _run(repo, runner)
        assert out.gate == "error"
        assert out.status is BoxUpdateStatus.ROLLED_BACK

    def test_gate_timeout_is_error_not_import_failed(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, gate=(124, "", ""))
        out = _run(repo, runner)
        assert out.gate == "error"

    def test_scrub_law_child_env_lacks_the_escape(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        env = {"BRUTEX_SKIP_WHEEL_FRESHNESS": "1", "KEEP": "yes"}
        out = _run(repo, runner, env=env)
        assert out.status is BoxUpdateStatus.UPDATED
        gate_env = runner.gate_calls()[0]["env"]
        assert gate_env is not None
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS" not in gate_env
        assert gate_env["KEEP"] == "yes"

    def test_skip_law_belt_forged_skip_is_stale(self, tmp_path: Path) -> None:
        checks = [
            ["wheel_freshness", "SKIP", "skipped via BRUTEX_SKIP_WHEEL_FRESHNESS=1"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "PASS", "ok"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        assert bu._fold_checks([(n, s, d) for n, s, d in checks]) == "stale"

    def test_honesty_skips_fold_green(self, tmp_path: Path) -> None:
        checks = [
            (
                "wheel_freshness",
                "SKIP",
                "no expected-fingerprint file at x — freshness not checkable",
            ),
            ("wheel_freshness_fno_math", "PASS", "ok"),
            (
                "wheel_source_digest",
                "SKIP",
                "no crate source tree at y — source digest not checkable",
            ),
            ("wheel_source_digest_fno_math", "PASS", "ok"),
        ]
        assert bu._fold_checks(checks) == "ok"

    def test_doctor_detail_literals_drift_pin(self, tmp_path: Path) -> None:
        # §10 test 7(h) — drive the REAL doctor checks; pin the literal detail
        # strings the §4.5 law-3 fold matches on.
        from brutex.doctor import check_wheel_freshness, check_wheel_source_digest

        res = check_wheel_freshness(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert res.status == "SKIP"
        assert res.detail == "skipped via BRUTEX_SKIP_WHEEL_FRESHNESS=1"
        res2 = check_wheel_source_digest(env={"BRUTEX_SKIP_WHEEL_FRESHNESS": "1"})
        assert res2.status == "SKIP"
        assert res2.detail == "skipped via BRUTEX_SKIP_WHEEL_FRESHNESS=1"
        res3 = check_wheel_freshness(
            fingerprint_file=tmp_path / "absent" / "EXPECTED_BUILD.json", env={}
        )
        assert res3.status == "SKIP"
        assert "freshness not checkable" in res3.detail
        res4 = check_wheel_source_digest(crate_root=tmp_path / "absent-crate", env={})
        assert res4.status == "SKIP"
        assert "source digest not checkable" in res4.detail

    def test_gate_skipped_emission_law(self, tmp_path: Path) -> None:
        # §10 test 17 — gate=="skipped" ⇔ the subprocess never ran.
        repo = _mk_repo(tmp_path)
        # disabled
        runner = FakeRunner()
        out = _run(repo, runner, env={"BRUTEX_BOX_UPDATE": "0"})
        assert out.gate == "skipped" and runner.gate_calls() == []
        # dirty_tree skip
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (0, " M x\n", ""))
        out = _run(repo, runner)
        assert out.gate == "skipped" and runner.gate_calls() == []
        # required-BUILD-failure rollback (row 10) — gate never ran
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out = _run(repo, runner)
        assert out.gate == "skipped" and runner.gate_calls() == []
        # negative twin: updated carries a real verdict
        runner = _std_runner(repo)
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UPDATED and out.gate == "ok"

    def test_up_to_date_still_runs_the_gate(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, FROM_SHA, ""))
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.UP_TO_DATE
        assert out.gate == "ok"
        assert len(runner.gate_calls()) == 1
        assert runner.git_calls("merge") == []

    def test_up_to_date_self_heal_rebuilds_on_stale_gate(self, tmp_path: Path) -> None:
        # §7 — a stale gate on the UP_TO_DATE path triggers the rust=True
        # rebuild + a re-run gate; green ⇒ status stays up_to_date.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, FROM_SHA, ""))
        runner.state["gate_calls"] = 0

        def _gate(_a: list[str]) -> tuple[int, str, str]:
            runner.state["gate_calls"] += 1
            if runner.state["gate_calls"] == 1:
                return (
                    0,
                    _gate_stdout(
                        [
                            ["wheel_freshness", "WARN", "stale"],
                            ["wheel_freshness_fno_math", "PASS", "ok"],
                            ["wheel_source_digest", "PASS", "ok"],
                            ["wheel_source_digest_fno_math", "PASS", "ok"],
                        ]
                    ),
                    "",
                )
            return (0, _gate_stdout(), "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _gate,
        )
        out = _run(repo, runner, state_dir=tmp_path / "state")
        assert out.status is BoxUpdateStatus.UP_TO_DATE
        assert len(runner.maturin_calls()) == len(WHEEL_SET)  # the self-heal rebuild
        assert out.gate == "ok"
        assert "self_heal" in out.detail


# ── lock-artifact restore + marker + quarantine (§10 tests 6b/18/20/21) ──
class TestRestoreMarkerQuarantine:
    def _dirty_after_merge(self, runner: FakeRunner, dirty_line: str) -> None:
        def _porcelain(_a: list[str]) -> tuple[int, str, str]:
            if runner.state.get("restored"):
                return (0, "", "")
            if runner.state.get("head") == TO_SHA and runner.state.get("built"):
                return (0, dirty_line + "\n", "")
            return (0, "", "")

        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], _porcelain)

    def test_lock_rewrite_restored_after_successful_build(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        self._dirty_after_merge(runner, " M rust/fno-math/Cargo.lock")

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            runner.state["built"] = True
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)

        def _checkout_f(a: list[str]) -> tuple[int, str, str]:
            runner.state["restored"] = True
            return (0, "", "")

        runner.on(lambda a: a[:3] == ["git", "checkout", "-f"], _checkout_f)
        with capture_logs() as logs:
            out = _run(repo, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.UPDATED  # a restore is NOT a failure
        assert out.lock_restored == ("rust/fno-math/Cargo.lock",)
        assert out.quarantined == ("rust/fno-math/Cargo.lock",)
        restores = runner.git_calls("checkout", "-f")
        assert restores == [["git", "checkout", "-f", "--", "rust/fno-math/Cargo.lock"]]
        assert any(e["event"] == "box_update.lock_artifact_restored" for e in logs)
        # The quarantine copy holds the pre-restore bytes.
        qroot = state / "quarantine"
        copies = list(qroot.rglob("rust__fno-math__Cargo.lock"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == (repo / "rust/fno-math/Cargo.lock").read_bytes()
        # The marker is CLEARED on the clean exit.
        assert not (state / "build_marker.json").exists()

    def test_marker_written_before_first_maturin(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        seen: dict[str, Any] = {}

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            if "marker" not in seen:
                seen["marker"] = json.loads(
                    (state / "build_marker.json").read_text(encoding="utf-8")
                )
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        _run(repo, runner, state_dir=state)
        assert seen["marker"]["schema"] == 1
        assert seen["marker"]["lock_paths"] == [f"{s.crate_dir}/Cargo.lock" for s in WHEEL_SET]

    def test_restore_pathset_is_marker_intersection(self, tmp_path: Path) -> None:
        # §10 test 18(b) — a dirty lock NOT in lock_paths never restores.
        repo = _mk_repo(tmp_path)
        (repo / "rust/other/Cargo.lock").parent.mkdir(parents=True)
        (repo / "rust/other/Cargo.lock").write_text("x", encoding="utf-8")
        ctx = bu._Ctx(
            runner=FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=tmp_path / "state",
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M rust/other/Cargo.lock\n M rust/fno-math/Cargo.lock\n", ""),
        )
        runner.on(lambda a: a[:3] == ["git", "checkout", "-f"], (0, "", ""))
        bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        restores = runner.git_calls("checkout", "-f")
        assert restores == [["git", "checkout", "-f", "--", "rust/fno-math/Cargo.lock"]]
        assert "rust/other/Cargo.lock" not in ctx.lock_restored

    def test_zero_intersection_issues_no_checkout(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        ctx = bu._Ctx(
            runner=FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=tmp_path / "state",
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (0, "", ""))
        bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        assert runner.git_calls("checkout") == []

    def test_failed_checkout_leaves_path_dirty_no_retry(self, tmp_path: Path) -> None:
        # §10 test 18(d) — rc≠0 restore: ONE WARN, one checkout argv total.
        repo = _mk_repo(tmp_path)
        ctx = bu._Ctx(
            runner=FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=tmp_path / "state",
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M rust/fno-math/Cargo.lock\n", ""),
        )
        runner.on(lambda a: a[:3] == ["git", "checkout", "-f"], (1, "", "refused"))
        with capture_logs() as logs:
            bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        assert len(runner.git_calls("checkout", "-f")) == 1  # never force-retried
        assert ctx.lock_restored == []
        assert ctx.quarantined == ["rust/fno-math/Cargo.lock"]  # copy preceded
        warns = [e for e in logs if e["event"] == "box_update.lock_restore_failed"]
        assert len(warns) == 1

    def test_quarantine_copy_fail_refuses_the_restore(self, tmp_path: Path) -> None:
        # §10 test 21(b) — state_dir unwritable ⇒ NO checkout for that path.
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        # A regular FILE planted at quarantine/ makes every run-dir mkdir fail
        # (mode-bit chmod is untrustworthy under a root test runner).
        (state / "quarantine").write_text("not a dir", encoding="utf-8")
        if True:
            ctx = bu._Ctx(
                runner=FakeRunner(),
                env={},
                repo_root=repo,
                state_dir=state,
                now=lambda: 0.0,
                sleep=lambda _s: None,
                started=0.0,
                fetch_timeout=1.0,
                build_timeout=1.0,
                pip_timeout=1.0,
            )
            runner = ctx.runner
            assert isinstance(runner, FakeRunner)
            runner.on(
                lambda a: a[:3] == ["git", "status", "--porcelain"],
                (0, " M rust/fno-math/Cargo.lock\n", ""),
            )
            with capture_logs() as logs:
                bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
            assert runner.git_calls("checkout") == []
            assert ctx.lock_restored == [] and ctx.quarantined == []
            assert any(e["event"] == "box_update.quarantine_copy_failed" for e in logs)

    def test_state_dir_none_vacuous_fail_no_checkout_ever(self, tmp_path: Path) -> None:
        # §10 tests 20(f)/21(c) — no state_dir ⇒ no quarantine ⇒ no restore.
        repo = _mk_repo(tmp_path)
        ctx = bu._Ctx(
            runner=FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=None,
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M rust/fno-math/Cargo.lock\n", ""),
        )
        with capture_logs():
            bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        assert runner.git_calls("checkout") == []

    def test_quarantine_retention_prunes_to_newest_ten(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        qroot = state / "quarantine"
        for i in range(12):
            (qroot / f"20260101T0000{i:02d}Z").mkdir(parents=True)
        sentinel = repo / "keepme.txt"
        sentinel.write_text("safe", encoding="utf-8")
        ctx = bu._Ctx(
            runner=FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=state,
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M rust/fno-math/Cargo.lock\n", ""),
        )
        runner.on(lambda a: a[:3] == ["git", "checkout", "-f"], (0, "", ""))
        bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        remaining = sorted(p.name for p in qroot.iterdir())
        assert len(remaining) == bu._QUARANTINE_KEEP
        assert sentinel.exists()  # pruning deletes ONLY lane quarantine dirs

    @pytest.mark.parametrize(
        "marker_text",
        [
            "{not json",
            # A TAMPERED marker scoping an arbitrary TRACKED path — the shape
            # law refuses it wholesale (defense-in-depth over the porcelain ∩).
            json.dumps({"schema": 1, "lock_paths": ["brutex/cli.py"], "started_utc": "t"}),
        ],
        ids=["corrupt", "tampered_foreign_path"],
    )
    def test_corrupt_marker_treated_as_absent(self, tmp_path: Path, marker_text: str) -> None:
        # §10 test 20(d) — corrupt/tampered marker: NO restore on unproven
        # provenance (a foreign-path entry is UNPROVEN by definition — the
        # lane only ever markers rust/<crate>/Cargo.lock).
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        (state / "build_marker.json").write_text(marker_text, encoding="utf-8")
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (0, " M x\n", ""))
        with capture_logs() as logs:
            out = _run(repo, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "dirty_tree"
        assert runner.git_calls("checkout") == []
        assert any(e["event"] == "box_update.build_marker_unreadable" for e in logs)

    @pytest.mark.parametrize(
        ("path", "ok"),
        [
            ("rust/fno-math/Cargo.lock", True),
            ("rust/trading-core/Cargo.lock", True),
            ("brutex/cli.py", False),  # not the 3-segment lane shape
            ("rust/a/b/Cargo.lock", False),  # four segments
            ("notrust/x/Cargo.lock", False),  # wrong root token
            ("rust/x/Cargo.toml", False),  # wrong leaf token
            ("rust//Cargo.lock", False),  # empty crate segment
            ("rust/./Cargo.lock", False),  # dot token
            ("rust/../Cargo.lock", False),  # parent-escape token
            ("rust/a\\b/Cargo.lock", False),  # backslash smuggling
        ],
    )
    def test_lane_lock_path_shape_law(self, path: str, ok: bool) -> None:
        # The ONE shape the lane itself ever markers — everything else is
        # refused into the corrupt-marker WARN arm (tamper defense-in-depth).
        assert bu._is_lane_lock_path(path) is ok

    def test_prologue_consumes_marker_on_quiet_upstream_night(self, tmp_path: Path) -> None:
        # §10 tests 6b/20(c) round-9 arm — planted marker + dirty lock + origin
        # UNMOVED ⇒ restore + clear at the PROLOGUE, then status=up_to_date
        # with lock_restored non-empty (the fails-before-round-9 oracle).
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        (state / "build_marker.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "lock_paths": ["rust/fno-math/Cargo.lock"],
                    "started_utc": "2026-07-17T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, FROM_SHA, ""))

        def _porcelain(_a: list[str]) -> tuple[int, str, str]:
            if runner.state.get("restored"):
                return (0, "", "")
            return (0, " M rust/fno-math/Cargo.lock\n", "")

        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], _porcelain)

        def _checkout_f(_a: list[str]) -> tuple[int, str, str]:
            runner.state["restored"] = True
            return (0, "", "")

        runner.on(lambda a: a[:3] == ["git", "checkout", "-f"], _checkout_f)
        out = _run(repo, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.UP_TO_DATE
        assert out.lock_restored == ("rust/fno-math/Cargo.lock",)
        assert not (state / "build_marker.json").exists()  # marker consumed
        # Ordering: the restore checkout PRECEDES the origin/main consumption.
        argvs = runner.argvs()
        restore_i = next(i for i, a in enumerate(argvs) if a[:3] == ["git", "checkout", "-f"])
        origin_i = next(
            i for i, a in enumerate(argvs) if a[:3] == ["git", "rev-parse", "origin/main"]
        )
        assert restore_i < origin_i

    def test_no_marker_operator_dirty_lock_takes_dirty_tree_skip(self, tmp_path: Path) -> None:
        # §10 test 20(b) — the never-clobber invariant (no marker ⇒ no restore).
        repo = _mk_repo(tmp_path)
        lock = repo / "rust/fno-math/Cargo.lock"
        lock.write_text("operator edit\n", encoding="utf-8")
        before = lock.read_bytes()
        runner = _std_runner(repo)
        runner.on(
            lambda a: a[:3] == ["git", "status", "--porcelain"],
            (0, " M rust/fno-math/Cargo.lock\n", ""),
        )
        out = _run(repo, runner, state_dir=tmp_path / "state")
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "dirty_tree"
        assert runner.git_calls("checkout") == []
        assert lock.read_bytes() == before


# ── §4.6 rollback + repair + coherence + pip restore (§10 tests 6a/23/24) ─
class TestRollbackRepairCoherence:
    def _rollback_runner(self, repo: Path) -> FakeRunner:
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        return runner

    def test_rollback_smoke_loadable_coherent_repair_not_needed(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.repair == "not_needed"
        assert out.running_sha == FROM_SHA
        # smoke ran, then the coherence check (fresh doctor child, no smoke line).
        assert len(runner.smoke_calls()) >= 1
        assert len(runner.coherence_calls()) == 1

    def test_rollback_failure_is_error_with_critical(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        runner.on(lambda a: a[:4] == ["git", "checkout", "-B", "main"], (1, "", "refused"))
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ERROR
        assert any(
            e["event"] == "box_update.rollback_failed" and e["log_level"] == "critical"
            for e in logs
        )
        assert out.pip_restore == "not_needed"  # row-12 arm: tree stayed at to_sha

    def test_unloadable_smoke_repairs_then_falls_through(self, tmp_path: Path) -> None:
        # §10 test 6a — unloadable → repair rebuild → re-smoke → coherence.
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        runner.state["smokes"] = 0

        def _smoke(a: list[str]) -> tuple[int, str, str]:
            if a[2] == "import trading_core, fno_math":
                runner.state["smokes"] += 1
                return (1, "", "broken") if runner.state["smokes"] == 1 else (0, "", "")
            if a[2] == "import fno_math":
                return (1, "", "broken")
            return (0, "", "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and a[2].startswith("import "),
            _smoke,
        )
        # After the rollback the repair rebuild must succeed (the failing rule
        # above keyed on cwd fires for the FIRST forward build; make repairs ok).
        calls_before = {"n": 0}
        fno_dir = repo / "rust/fno-math"

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            calls_before["n"] += 1
            if calls_before["n"] == 1 and runner.calls[-1]["cwd"] == fno_dir:
                return (1, "", "boom")  # the forward build failure
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.repair == "rebuilt"
        # The repair rebuilt exactly the unloadable crate (fno_math).
        repair_builds = runner.maturin_calls()[1:]
        assert [c["cwd"] for c in repair_builds] == [repo / "rust/fno-math"]

    def test_still_unloadable_after_repair_is_critical_error(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and a[2].startswith("import "),
            (1, "", "still broken"),
        )
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ERROR
        assert out.repair == "failed"
        crits = [e for e in logs if e["event"] == "box_update.required_wheel_unloadable"]
        assert len(crits) == 1 and crits[0]["log_level"] == "critical"
        # The CRITICAL terminal SKIPS the coherence check (CLI-down outranks torn).
        assert runner.coherence_calls() == []

    def test_gate_stale_rollback_mismatch_repair_rebuilt(self, tmp_path: Path) -> None:
        # §10 test 23(a) unit shape — build OK, gate stale, coherence WARN →
        # mismatch repair for exactly the WARNing crate → digest re-check PASS.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        stale = [
            ["wheel_freshness", "PASS", "ok"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "WARN", "digest mismatch"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.state["doctor_calls"] = 0

        def _gate(_a: list[str]) -> tuple[int, str, str]:
            runner.state["doctor_calls"] += 1
            # call 1 = the gate (stale); call 2 = coherence (still WARN);
            # call 3 = the post-repair re-check (PASS).
            if runner.state["doctor_calls"] <= 2:
                return (0, _gate_stdout(stale), "")
            return (0, _gate_stdout(), "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _gate,
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.gate == "stale"
        assert out.repair == "rebuilt"
        # The mismatch repair rebuilt EXACTLY trading_core (the WARNing crate).
        repair_builds = runner.maturin_calls()[len(WHEEL_SET) :]
        assert [c["cwd"] for c in repair_builds] == [repo / "rust/trading-core"]

    def test_mismatch_repair_failure_is_failed_stale_warn_class(self, tmp_path: Path) -> None:
        # §10 test 23(b) — repair rc≠0 ⇒ failed_stale, status ROLLED_BACK, no CRITICAL.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        stale = [
            ["wheel_freshness", "PASS", "ok"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "WARN", "digest mismatch"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(stale), ""),
        )
        builds = {"n": 0}

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            builds["n"] += 1
            # Forward builds (3) succeed; the mismatch repair build fails.
            return (0, "", "") if builds["n"] <= len(WHEEL_SET) else (1, "", "repair boom")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.repair == "failed_stale"
        events = {e["event"]: e for e in logs}
        assert "box_update.repair_failed_stale" in events
        assert events["box_update.repair_failed_stale"]["log_level"] == "warning"
        # The manual-fix pointer names the runbook's env-prefixed wedge-rescue
        # command (a bare maturin paste in a fresh shell hits maturin-not-found).
        assert (
            "docs/runbooks/box-autodeploy.md" in events["box_update.repair_failed_stale"]["detail"]
        )
        assert "box_update.required_wheel_unloadable" not in events
        by_name = {w.name: w for w in out.wheels}
        assert by_name["trading_core"].digest_ok is False  # visible torn residual

    def test_coherence_skip_abstain_never_rebuilds(self, tmp_path: Path) -> None:
        # §10 test 23(e) — honesty-SKIP checks ⇒ repair not_needed, zero rebuilds.
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        skips = [
            ["wheel_freshness", "SKIP", "no expected-fingerprint file — freshness not checkable"],
            [
                "wheel_freshness_fno_math",
                "SKIP",
                "no expected-fingerprint file — freshness not checkable",
            ],
            ["wheel_source_digest", "SKIP", "no crate source tree — source digest not checkable"],
            [
                "wheel_source_digest_fno_math",
                "SKIP",
                "no crate source tree — source digest not checkable",
            ],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(skips), ""),
        )
        out = _run(repo, runner)
        assert out.repair == "not_needed"
        assert len(runner.maturin_calls()) == 1  # ONLY the failed forward build

    def test_pip_restore_ordering_and_tokens(self, tmp_path: Path) -> None:
        # §10 test 24(a) — restore strictly AFTER checkout -B, BEFORE the smoke.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("pyproject.toml", "rust/fno-math/src/lib.rs"))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.pip_restore == "ok"
        argvs = runner.argvs()
        pip_idx = [i for i, a in enumerate(argvs) if a[0].endswith("pip") and "install" in a]
        checkout_i = next(
            i for i, a in enumerate(argvs) if a[:4] == ["git", "checkout", "-B", "main"]
        )
        smoke_i = next(
            i
            for i, a in enumerate(argvs)
            if a[0] == sys.executable and a[1] == "-c" and a[2] == "import trading_core, fno_math"
        )
        assert len(pip_idx) == 2  # the forward refresh + the restore
        assert checkout_i < pip_idx[1] < smoke_i

    def test_pip_restore_failure_is_warn_and_proceeds(self, tmp_path: Path) -> None:
        # §10 test 24(b) — restore rc≠0 ⇒ pip_restore=failed, WARN, smoke still runs.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("pyproject.toml", "rust/fno-math/src/lib.rs"))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        pips = {"n": 0}

        def _pip(_a: list[str]) -> tuple[int, str, str]:
            pips["n"] += 1
            return (0, "", "") if pips["n"] == 1 else (1, "", "resolver boom")

        runner.on(lambda a: a[0].endswith("pip"), _pip)
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.pip_restore == "failed"
        warns = [e for e in logs if e["event"] == "box_update.pip_restore_failed"]
        assert len(warns) == 1 and warns[0]["log_level"] == "warning"
        assert len(runner.smoke_calls()) >= 1  # the proceed law

    def test_rust_only_rollback_never_runs_pip(self, tmp_path: Path) -> None:
        # §10 test 24(c) — no forward pip ⇒ zero pip argv, not_needed.
        repo = _mk_repo(tmp_path)
        runner = self._rollback_runner(repo)
        out = _run(repo, runner)
        assert out.pip_restore == "not_needed"
        assert runner.pip_calls() == []

    def test_forward_pip_failure_rolls_back_and_still_restores(self, tmp_path: Path) -> None:
        # §10 test 24(d) — the attempted-law: forward pip rc≠0 ⇒ rollback ⇒
        # the restore STILL runs (partially-moved deps re-resolved).
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("pyproject.toml",))
        pips = {"n": 0}

        def _pip(_a: list[str]) -> tuple[int, str, str]:
            pips["n"] += 1
            return (1, "", "dep boom") if pips["n"] == 1 else (0, "", "")

        runner.on(lambda a: a[0].endswith("pip"), _pip)
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.pip_restore == "ok"
        assert pips["n"] == 2
        argvs = runner.argvs()
        checkout_i = next(
            i for i, a in enumerate(argvs) if a[:4] == ["git", "checkout", "-B", "main"]
        )
        second_pip_i = [i for i, a in enumerate(argvs) if a[0].endswith("pip")][1]
        assert checkout_i < second_pip_i


# ── alarm dedupe (§10 test 19) ───────────────────────────────────────────
class TestAlarmDedupe:
    def _fail_once(
        self, repo: Path, state: Path, *, stderr: str = "boom"
    ) -> tuple[BoxUpdateOutcome, list[dict[str, Any]]]:
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", stderr) if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        with capture_logs() as logs:
            out = _run(repo, runner, state_dir=state)
        return out, list(logs)

    def test_first_night_full_severity_state_written(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        out, logs = self._fail_once(repo, state)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.alarm_repeats == 0
        assert out.alarm_signature is not None
        payload = json.loads((state / "last_alarm.json").read_text(encoding="utf-8"))
        assert payload == {
            "schema": 1,
            "signature": out.alarm_signature,
            "first_utc": payload["first_utc"],
            "nights": 1,
        }
        rolled = [e for e in logs if e["event"] == "box_update.rolled_back"]
        assert len(rolled) == 1 and "repeat" not in rolled[0]

    def test_repeat_night_demotes_with_two_integer_law(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        self._fail_once(repo, state)
        out, logs = self._fail_once(repo, state)
        # Report integer = nights − 1; log integer = nights (§4.7 law 3).
        assert out.alarm_repeats == 1
        rolled = [e for e in logs if e["event"] == "box_update.rolled_back"]
        assert rolled[0]["repeat"] == 2
        assert rolled[0]["repeat"] == out.alarm_repeats + 1  # DISTINCT integers

    def test_different_signature_rearms_full_severity(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        self._fail_once(repo, state)
        # A DIFFERENT failure shape: timeout instead of failed.
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (124, "", "") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out = _run(repo, runner, state_dir=state)
        assert out.alarm_repeats == 0  # full severity again
        payload = json.loads((state / "last_alarm.json").read_text(encoding="utf-8"))
        assert payload["nights"] == 1

    def test_healthy_night_clears_the_state(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        self._fail_once(repo, state)
        runner = _std_runner(repo, diff_paths=("brutex/cli.py",))
        out = _run(repo, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.UPDATED
        assert not (state / "last_alarm.json").exists()

    def test_skipped_and_disabled_leave_state_untouched(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        self._fail_once(repo, state)
        before = (state / "last_alarm.json").read_bytes()
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (0, " M x\n", ""))
        out = _run(repo, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.SKIPPED
        assert (state / "last_alarm.json").read_bytes() == before
        out = _run(repo, FakeRunner(), env={"BRUTEX_BOX_UPDATE": "0"}, state_dir=state)
        assert out.status is BoxUpdateStatus.DISABLED
        assert (state / "last_alarm.json").read_bytes() == before

    def test_corrupt_state_fails_open_to_full_severity(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        (state / "last_alarm.json").write_text("{broken", encoding="utf-8")
        out, logs = self._fail_once(repo, state)
        assert out.alarm_repeats == 0
        assert any(e["event"] == "box_update.alarm_state_unreadable" for e in logs)

    def test_signature_is_machine_tokens_only(self, tmp_path: Path) -> None:
        # §10 test 19(g) — two runs differing ONLY in stderr detail text
        # produce the SAME signature (the dedupe-defeat regression).
        repo = _mk_repo(tmp_path)
        s1 = tmp_path / "s1"
        s2 = tmp_path / "s2"
        out1, _ = self._fail_once(repo, s1, stderr="boom at 12:01:07 fd=93")
        out2, _ = self._fail_once(repo, s2, stderr="boom at 03:44:55 fd=11")
        assert out1.alarm_signature == out2.alarm_signature

    def test_state_dir_none_full_severity_no_io(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        self._fail_once(repo, tmp_path / "unused_state")
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        out2 = _run(repo, runner, state_dir=None)
        assert out2.alarm_signature is not None
        assert out2.alarm_repeats == 0


# ── re-exec + adopt_state (§10 tests 11/15) ──────────────────────────────
class TestReexecAndAdopt:
    def _outcome(self, status: BoxUpdateStatus = BoxUpdateStatus.UPDATED) -> BoxUpdateOutcome:
        return BoxUpdateOutcome(
            status=status,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            running_sha=FROM_SHA,
            skip_reason=None,
            delta_rust=True,
            delta_pyproject=False,
            wheels=(
                WheelBuildResult(
                    name="fno_math",
                    crate_dir="rust/fno-math",
                    required=True,
                    result="ok",
                    digest_ok=True,
                    duration_s=1.0,
                    detail="",
                ),
            ),
            gate="ok",
            repair="not_needed",
            pip_restore="not_needed",
            lock_restored=(),
            quarantined=(),
            alarm_signature=None,
            alarm_repeats=0,
            reexec=False,
            duration_s=3.0,
            detail="",
        )

    def test_updated_and_no_sentinel_execs_plain_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(bu.os, "execv", lambda prog, argv: seen.update(prog=prog, argv=argv))
        environ: dict[str, str] = {}
        maybe_reexec(self._outcome(), argv=["nightly", "run", "--keep", "3"], environ=environ)
        assert seen["prog"] == sys.executable
        assert seen["argv"] == [sys.executable, "-m", "brutex", "nightly", "run", "--keep", "3"]
        assert BOX_UPDATE_STATE_ENV in environ

    def test_sentinel_present_never_execs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The #1712 fork-bomb regression shape — loop-proof.
        def _boom(*_a: object) -> None:
            raise AssertionError("os.execv must not be called")

        monkeypatch.setattr(bu.os, "execv", _boom)
        maybe_reexec(
            self._outcome(),
            argv=["nightly", "run"],
            environ={BOX_UPDATE_STATE_ENV: "{}"},
        )

    @pytest.mark.parametrize(
        "status",
        [
            BoxUpdateStatus.UP_TO_DATE,
            BoxUpdateStatus.SKIPPED,
            BoxUpdateStatus.ROLLED_BACK,
            BoxUpdateStatus.ERROR,
            BoxUpdateStatus.DISABLED,
        ],
    )
    def test_non_updated_never_execs(
        self, monkeypatch: pytest.MonkeyPatch, status: BoxUpdateStatus
    ) -> None:
        def _boom(*_a: object) -> None:
            raise AssertionError("os.execv must not be called")

        monkeypatch.setattr(bu.os, "execv", _boom)
        maybe_reexec(self._outcome(status), argv=[], environ={})

    def test_adopt_round_trips_and_deletes_the_sentinel(self) -> None:
        outcome = self._outcome()
        env = {BOX_UPDATE_STATE_ENV: json.dumps(outcome.to_dict()), "OTHER": "x"}
        adopted = adopt_state(env)
        assert adopted is not None
        assert BOX_UPDATE_STATE_ENV not in env  # spawn workers never inherit it
        assert adopted.reexec is True
        assert adopted.running_sha == TO_SHA  # the child EXECUTES to_sha
        assert adopted.status is BoxUpdateStatus.UPDATED
        assert adopted.wheels[0].name == "fno_math"
        assert adopted.wheels[0].digest_ok is True

    def test_adopt_absent_sentinel_is_none(self) -> None:
        assert adopt_state({}) is None

    def test_adopt_corrupt_state_is_row_15(self) -> None:
        with capture_logs() as logs:
            adopted = adopt_state({BOX_UPDATE_STATE_ENV: "{not json"})
        assert adopted is not None
        assert adopted.status is BoxUpdateStatus.ERROR
        assert adopted.detail == "state_unparseable"
        assert adopted.gate == "skipped"
        assert adopted.reexec is True
        assert any(e["event"] == "box_update.state_unparseable" for e in logs)

    def test_sentinel_never_written_to_dotenv_surfaces(self) -> None:
        # §6 step 4 process-env-only invariant — grep-style pin: the sentinel
        # name appears in NO dotenv/profile/plist writer surface.
        root = Path(__file__).resolve().parents[2]
        for rel in ("brutex/config/dotenv.py", "brutex/nightly/schedule.py"):
            assert "BRUTEX_BOX_UPDATE_STATE" not in (root / rel).read_text(encoding="utf-8")


# ── default runner (rc 124/127 law) ──────────────────────────────────────
class TestDefaultRunner:
    def test_timeout_synthesizes_rc_124(self) -> None:
        rc, _, _ = default_command_runner(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.2
        )
        assert rc == 124

    def test_oserror_synthesizes_rc_127(self, tmp_path: Path) -> None:
        rc, _, err = default_command_runner([str(tmp_path / "no-such-binary")], timeout_s=5.0)
        assert rc == 127
        assert err != ""

    def test_plain_success_passes_through(self) -> None:
        rc, out, _ = default_command_runner([sys.executable, "-c", "print('hi')"], timeout_s=10.0)
        assert (rc, out.strip()) == (0, "hi")


# ── broken-wheel import law (§10 test 19(h)) ─────────────────────────────
class TestBrokenWheelImport:
    def test_box_update_imports_with_all_wheels_poisoned(self) -> None:
        code = (
            "import sys\n"
            "for name in ('trading_core', 'fno_math', 'brutex_feed', 'brutex_lake'):\n"
            "    sys.modules[name] = None\n"
            "import brutex.nightly.box_update, brutex.nightly.env_preflight\n"
            "print('WHEEL_FREE_IMPORT_OK')\n"
        )
        rc, out, err = default_command_runner([sys.executable, "-c", code], timeout_s=60.0)
        assert rc == 0, err
        assert "WHEEL_FREE_IMPORT_OK" in out


# ── degrade arms (failure-matrix rows 17/20/21/24 + parse totality) ──────
class TestDegradeArms:
    def _ctx(self, repo: Path, state: Path | None, runner: FakeRunner | None = None) -> bu._Ctx:
        return bu._Ctx(
            runner=runner or FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=state,
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )

    def test_porcelain_path_parses_rename_arrows_and_blanks(self) -> None:
        assert bu._porcelain_path("R  old.lock -> rust/x/Cargo.lock") == "rust/x/Cargo.lock"
        assert bu._porcelain_path(" M rust/x/Cargo.lock") == "rust/x/Cargo.lock"
        assert bu._porcelain_path("??") == ""

    def test_classify_delta_skips_blank_lines(self) -> None:
        assert classify_delta(["", "  ", "rust/x/src/lib.rs"]) == bu.DeltaClass(True, False)

    def test_bounded_read_arms(self, tmp_path: Path) -> None:
        # oversized ⇒ None; missing ⇒ None; non-regular (dir) ⇒ None.
        big = tmp_path / "big.json"
        big.write_bytes(b"x" * (bu._BOUNDED_READ_CAP + 10))
        assert bu._bounded_read_json(big) is None
        assert bu._bounded_read_json(tmp_path / "absent.json") is None
        assert bu._bounded_read_json(tmp_path) is None

    def test_sidecar_write_failure_returns_false(self, tmp_path: Path) -> None:
        # state_dir path occupied by a FILE ⇒ mkdir raises ⇒ False, never a raise.
        blocked = tmp_path / "state"
        blocked.write_text("file", encoding="utf-8")
        repo = _mk_repo(tmp_path)
        ctx = self._ctx(repo, blocked)
        assert bu._sidecar_write(ctx, "x.json", {"a": 1}) is False
        with capture_logs() as logs:
            bu._write_marker(ctx, ["rust/fno-math/Cargo.lock"])
        assert any(e["event"] == "box_update.build_marker_write_failed" for e in logs)

    def test_marker_clear_failure_warns(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        marker_dir = state / "build_marker.json"
        (marker_dir / "inner").mkdir(parents=True)  # unlink on a dir raises
        ctx = self._ctx(repo, state)
        with capture_logs() as logs:
            bu._clear_marker(ctx)
        assert any(e["event"] == "box_update.build_marker_clear_failed" for e in logs)

    def test_quarantine_dir_serials_on_same_second_collision(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        ctx = self._ctx(repo, state)
        first = bu._quarantine_dir(ctx)
        second = bu._quarantine_dir(ctx)
        assert first is not None and second is not None
        assert first != second
        assert second.name.startswith(first.name.split("-")[0])

    def test_quarantine_dir_mkdir_failure_returns_none(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        (state / "quarantine").write_text("file", encoding="utf-8")
        assert bu._quarantine_dir(self._ctx(repo, state)) is None

    def test_prune_arms(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        ctx = self._ctx(repo, state)
        bu._prune_quarantine(ctx)  # missing quarantine root ⇒ benign return
        ctx_none = self._ctx(repo, None)
        bu._prune_quarantine(ctx_none)  # state_dir=None ⇒ benign

    def test_quarantine_copy_src_unreadable_warns(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        qdir = state / "quarantine" / "ts"
        qdir.mkdir(parents=True)
        ctx = self._ctx(repo, state)
        with capture_logs() as logs:
            ok = bu._quarantine_copy(ctx, qdir, "rust/absent/Cargo.lock")
        assert ok is False
        assert any(e["event"] == "box_update.quarantine_copy_failed" for e in logs)

    def test_quarantine_copy_content_mismatch_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §10 test 21(b) second arm — an equal-size wrong-byte copy is caught
        # by the CONTENT verify (the round-5 upgrade of the size-only check).
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        qdir = state / "quarantine" / "ts"
        qdir.mkdir(parents=True)
        ctx = self._ctx(repo, state)
        real_read = Path.read_bytes

        def _fake_read(self: Path) -> bytes:
            data = real_read(self)
            if self.name == "rust__fno-math__Cargo.lock":
                return b"X" * len(data)  # equal-size wrong bytes
            return data

        monkeypatch.setattr(Path, "read_bytes", _fake_read)
        with capture_logs() as logs:
            ok = bu._quarantine_copy(ctx, qdir, "rust/fno-math/Cargo.lock")
        assert ok is False
        assert any(e["event"] == "box_update.quarantine_copy_failed" for e in logs)

    def test_restore_porcelain_failure_warns_and_refuses(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        ctx = self._ctx(repo, tmp_path / "state")
        runner = ctx.runner
        assert isinstance(runner, FakeRunner)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (128, "", "boom"))
        with capture_logs() as logs:
            bu.restore_lane_lock_artifacts(ctx, ["rust/fno-math/Cargo.lock"])
        assert runner.git_calls("checkout") == []
        assert any(e["event"] == "box_update.lock_restore_failed" for e in logs)

    def test_ensure_maturin_installs_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _mk_repo(tmp_path)
        empty_bin = tmp_path / "venvbin"
        empty_bin.mkdir()
        monkeypatch.setattr(bu, "_venv_bin", lambda: empty_bin)
        runner = FakeRunner()
        runner.on(lambda a: a[0].endswith("pip"), (1, "", "no network"))
        ctx = self._ctx(repo, None, runner)
        with capture_logs() as logs:
            bu._ensure_maturin(ctx)
        pip = [a for a in runner.argvs() if a[0].endswith("pip")]
        assert pip == [[str(empty_bin / "pip"), "install", "maturin"]]
        assert any(e["event"] == "box_update.maturin_install_failed" for e in logs)
        bu._ensure_maturin(ctx)  # once-per-run law: no second attempt
        assert len(runner.argvs()) == 1

    def test_doctor_child_garbage_json_is_error(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = FakeRunner()
        runner.on(
            lambda a: True,
            (0, f"{bu._IMPORT_OK_MARKER}\n{bu._CHECKS_MARKER}{{not json", ""),
        )
        ctx = self._ctx(repo, None, runner)
        assert bu._run_doctor_child(ctx, include_smoke=True) == ("error", None)
        runner2 = FakeRunner()
        runner2.on(lambda a: True, (0, f"{bu._IMPORT_OK_MARKER}\nno checks line", ""))
        ctx2 = self._ctx(repo, None, runner2)
        assert bu._run_doctor_child(ctx2, include_smoke=True) == ("error", None)

    def test_fold_unexpected_status_is_stale(self) -> None:
        assert bu._fold_checks([("wheel_freshness", "FAIL", "x")]) == "stale"

    def test_alarm_state_clear_failure_warns(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        (state / "last_alarm.json" / "inner").mkdir(parents=True)
        ctx = self._ctx(repo, state)
        with capture_logs() as logs:
            bu._apply_alarm_dedupe(ctx, BoxUpdateStatus.UPDATED)
        assert any(e["event"] == "box_update.alarm_state_clear_failed" for e in logs)

    def test_alarm_state_write_failure_warns_both_arms(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        blocked = tmp_path / "state"
        blocked.write_text("file", encoding="utf-8")  # sidecar writes fail
        ctx = self._ctx(repo, blocked)
        ctx.gate = "import_failed"
        with capture_logs() as logs:
            sig, repeats, demoted = bu._apply_alarm_dedupe(ctx, BoxUpdateStatus.ROLLED_BACK)
        assert sig is not None and repeats == 0 and demoted is False
        assert any(e["event"] == "box_update.alarm_state_write_failed" for e in logs)

    def test_rev_parse_head_and_branch_failures_skip(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "HEAD"], (128, "", "x"))
        out = _run(repo, runner)
        assert out.skip_reason == "rev_parse_failed"
        runner2 = _std_runner(repo)
        runner2.on(lambda a: a[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"], (128, "", "x"))
        out2 = _run(repo, runner2)
        assert out2.skip_reason == "rev_parse_failed"

    def test_g5_porcelain_command_failure_skips(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "status", "--porcelain"], (128, "", "x"))
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "porcelain_failed"

    def test_toplevel_resolve_oserror_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        real_resolve = Path.resolve

        def _fake_resolve(self: Path, strict: bool = False) -> Path:
            if str(self) == str(repo) + "-loop":
                raise OSError("symlink loop")
            return real_resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", _fake_resolve)
        runner.on(
            lambda a: a[:3] == ["git", "rev-parse", "--show-toplevel"],
            (0, f"{repo}-loop\n", ""),
        )
        out = _run(repo, runner)
        assert out.skip_reason == "not_a_repo_checkout"

    def test_self_heal_rebuild_failure_enters_vacuous_rollback(self, tmp_path: Path) -> None:
        # §7 — self-heal build fails ⇒ the §4.6 arm with the tree-move VACUOUS
        # (HEAD already == origin/main ⇒ NO checkout -B argv).
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, FROM_SHA, ""))
        stale = [
            ["wheel_freshness", "WARN", "stale"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "PASS", "ok"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(stale), ""),
        )
        runner.on(lambda a: a[0].endswith("maturin"), (1, "", "build boom"))
        out = _run(repo, runner, state_dir=tmp_path / "state")
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert runner.git_calls("checkout", "-B") == []  # vacuous — no tree move
        assert "self_heal" in out.detail

    def test_already_rebuilt_crate_still_warn_folds_failed_stale(self, tmp_path: Path) -> None:
        # The at-most-once law: a crate repaired at 10b whose digest STILL
        # WARNs at the coherence check is NEVER rebuilt twice ⇒ failed_stale.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        builds = {"n": 0}

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            builds["n"] += 1
            if builds["n"] == 1 and runner.calls[-1]["cwd"] == fno_dir:
                return (1, "", "boom")
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        smokes = {"n": 0}

        def _smoke(a: list[str]) -> tuple[int, str, str]:
            if a[2] == "import trading_core, fno_math":
                smokes["n"] += 1
                return (1, "", "broken") if smokes["n"] == 1 else (0, "", "")
            if a[2] == "import fno_math":
                return (1, "", "broken")
            return (0, "", "")

        runner.on(
            lambda a: (
                a[0] == sys.executable
                and a[1] == "-c"
                and a[2].startswith("import ")
                and "brutex.doctor" not in a[2]
            ),
            _smoke,
        )
        still_warn = [
            ["wheel_freshness", "PASS", "ok"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "PASS", "ok"],
            ["wheel_source_digest_fno_math", "WARN", "still mismatched"],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(still_warn), ""),
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.repair == "failed_stale"
        # fno_math was rebuilt EXACTLY once (10b); never a second build.
        repair_builds = [c for c in runner.maturin_calls() if c["cwd"] == repo / "rust/fno-math"]
        assert len(repair_builds) == 2  # the failed forward build + ONE repair

    def test_mismatch_repair_re_smoke_unloadable_is_critical(self, tmp_path: Path) -> None:
        # 10c → re-smoke unloadable ⇒ the row-10b CRITICAL arm.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        stale = [
            ["wheel_freshness", "PASS", "ok"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "WARN", "digest mismatch"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(stale), ""),
        )
        smokes = {"n": 0}

        def _smoke(a: list[str]) -> tuple[int, str, str]:
            if a[2] == "import trading_core, fno_math":
                smokes["n"] += 1
                # First smoke loadable (10a door); the post-repair re-smoke broken.
                return (0, "", "") if smokes["n"] == 1 else (1, "", "broken")
            return (0, "", "")

        runner.on(
            lambda a: (
                a[0] == sys.executable
                and a[1] == "-c"
                and a[2].startswith("import ")
                and "brutex.doctor" not in a[2]
            ),
            _smoke,
        )
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ERROR
        assert out.repair == "failed"
        assert any(e["event"] == "box_update.required_wheel_unloadable" for e in logs)

    def test_adopt_state_shape_errors_fold_to_row_15(self) -> None:
        base = {
            "status": "updated",
            "from_sha": FROM_SHA,
            "to_sha": TO_SHA,
            "skip_reason": None,
            "delta_rust": True,
            "delta_pyproject": False,
            "wheels": ["not-a-dict"],
            "gate": "ok",
            "repair": "not_needed",
            "pip_restore": "not_needed",
            "alarm_repeats": 0,
            "duration_s": 1.0,
            "detail": "",
        }
        adopted = adopt_state({BOX_UPDATE_STATE_ENV: json.dumps(base)})
        assert adopted is not None and adopted.detail == "state_unparseable"
        bad_digest = dict(base)
        bad_digest["wheels"] = [
            {
                "name": "x",
                "crate_dir": "y",
                "required": True,
                "result": "ok",
                "digest_ok": "yes",  # wrong type
                "duration_s": 1.0,
                "detail": "",
            }
        ]
        adopted = adopt_state({BOX_UPDATE_STATE_ENV: json.dumps(bad_digest)})
        assert adopted is not None and adopted.detail == "state_unparseable"
        adopted = adopt_state({BOX_UPDATE_STATE_ENV: "[]"})
        assert adopted is not None and adopted.detail == "state_unparseable"


# ── residual coverage arms (the 100% line+branch law) ────────────────────
class TestResidualArms:
    def _ctx(self, repo: Path, state: Path | None, runner: FakeRunner | None = None) -> bu._Ctx:
        return bu._Ctx(
            runner=runner or FakeRunner(),
            env={},
            repo_root=repo,
            state_dir=state,
            now=lambda: 0.0,
            sleep=lambda _s: None,
            started=0.0,
            fetch_timeout=1.0,
            build_timeout=1.0,
            pip_timeout=1.0,
        )

    def test_command_runner_protocol_stub(self) -> None:
        # The Protocol stub body is executable (returns None unbound).
        assert bu.CommandRunner.__call__(default_command_runner, ["true"], timeout_s=1.0) is None

    def test_sidecar_write_state_dir_none_is_false(self, tmp_path: Path) -> None:
        ctx = self._ctx(_mk_repo(tmp_path), None)
        assert bu._sidecar_write(ctx, "x.json", {}) is False

    def test_read_alarm_state_state_dir_none(self, tmp_path: Path) -> None:
        assert bu._read_alarm_state(self._ctx(_mk_repo(tmp_path), None)) is None

    def test_quarantine_dir_serial_exhaustion_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        fixed = _dt(2026, 1, 1, tzinfo=_UTC)

        class _FakeDt:
            @staticmethod
            def now(tz: object = None) -> _dt:
                return fixed

            @staticmethod
            def strftime(fmt: str) -> str:  # pragma: no cover - unused
                raise AssertionError

        monkeypatch.setattr(bu, "datetime", _FakeDt)
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        qroot = state / "quarantine"
        stamp = fixed.strftime("%Y%m%dT%H%M%SZ")
        (qroot / stamp).mkdir(parents=True)
        for serial in range(1, 100):
            (qroot / f"{stamp}-{serial:02d}").mkdir()
        assert bu._quarantine_dir(self._ctx(repo, state)) is None

    def test_prune_rmtree_failure_warns_best_effort(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        qroot = state / "quarantine"
        target = tmp_path / "elsewhere"
        target.mkdir()
        qroot.mkdir(parents=True)
        # The OLDEST entry is a symlink-to-a-dir: is_dir() True, rmtree raises.
        (qroot / "00000000T000000Z").symlink_to(target)
        for i in range(1, 12):
            (qroot / f"20260101T0000{i:02d}Z").mkdir()
        with capture_logs() as logs:
            bu._prune_quarantine(self._ctx(repo, state))
        assert any(e["event"] == "box_update.quarantine_prune_failed" for e in logs)
        assert target.exists()  # never a repo/foreign deletion

    def test_ensure_maturin_install_success_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _mk_repo(tmp_path)
        empty_bin = tmp_path / "venvbin"
        empty_bin.mkdir()
        monkeypatch.setattr(bu, "_venv_bin", lambda: empty_bin)
        runner = FakeRunner()
        runner.on(lambda a: a[0].endswith("pip"), (0, "", ""))
        ctx = self._ctx(repo, None, runner)
        with capture_logs() as logs:
            bu._ensure_maturin(ctx)
        assert not any(e["event"] == "box_update.maturin_install_failed" for e in logs)

    def test_alarm_state_junk_shapes_read_absent(self, tmp_path: Path) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        for payload in ('{"signature": 1, "nights": 2}', '{"signature": "x", "nights": true}'):
            (state / "last_alarm.json").write_text(payload, encoding="utf-8")
            with capture_logs() as logs:
                assert bu._read_alarm_state(self._ctx(repo, state)) is None
            assert any(e["event"] == "box_update.alarm_state_unreadable" for e in logs)

    def test_repeat_arm_write_failure_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        ctx = self._ctx(repo, state)
        ctx.gate = "import_failed"
        sig = bu._alarm_signature(ctx, BoxUpdateStatus.ROLLED_BACK)
        (state / "last_alarm.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "signature": sig,
                    "first_utc": "2026-01-01T00:00:00+00:00",
                    "nights": 1,
                }
            ),
            encoding="utf-8",
        )

        def _boom(dest: Path, write_to: object, *, chmod: object = None) -> None:
            raise OSError("planted write failure")

        monkeypatch.setattr(bu, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            got_sig, repeats, demoted = bu._apply_alarm_dedupe(ctx, BoxUpdateStatus.ROLLED_BACK)
        assert (got_sig, repeats, demoted) == (sig, 1, True)
        assert any(e["event"] == "box_update.alarm_state_write_failed" for e in logs)

    def test_recheck_child_error_after_mismatch_repair_is_failed_stale(
        self, tmp_path: Path
    ) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        stale = [
            ["wheel_freshness", "PASS", "ok"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "WARN", "digest mismatch"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.state["doctor_calls"] = 0

        def _gate(_a: list[str]) -> tuple[int, str, str]:
            runner.state["doctor_calls"] += 1
            if runner.state["doctor_calls"] <= 2:
                return (0, _gate_stdout(stale), "")
            return (124, "", "")  # the post-repair re-check child times out

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _gate,
        )
        out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.repair == "failed_stale"  # never "rebuilt" on an unproven re-check

    def test_self_heal_rebuild_ok_but_gate_still_stale_vacuous_rollback(
        self, tmp_path: Path
    ) -> None:
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo)
        runner.on(lambda a: a[:3] == ["git", "rev-parse", "origin/main"], (0, FROM_SHA, ""))
        stale = [
            ["wheel_freshness", "WARN", "stale"],
            ["wheel_freshness_fno_math", "PASS", "ok"],
            ["wheel_source_digest", "PASS", "ok"],
            ["wheel_source_digest_fno_math", "PASS", "ok"],
        ]
        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            (0, _gate_stdout(stale), ""),
        )
        out = _run(repo, runner, state_dir=tmp_path / "state")
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert len(runner.maturin_calls()) >= len(WHEEL_SET)  # the self-heal rebuild ran
        assert runner.git_calls("checkout", "-B") == []  # vacuous


# ── partial-install sibling twins (§10 tests 23(c)/(f)) ──────────────────
def _digest_check_name(crate: str) -> str:
    """The doctor source-digest check name for *crate* (§4.6 coherence law)."""
    return "wheel_source_digest" if crate == "trading_core" else "wheel_source_digest_fno_math"


def _torn_checks(torn: str) -> list[list[str]]:
    """A coherence-check payload WARNing exactly *torn*'s source digest."""
    return [
        ["wheel_freshness", "PASS", "ok"],
        ["wheel_freshness_fno_math", "PASS", "ok"],
        [
            "wheel_source_digest",
            "WARN" if torn == "trading_core" else "PASS",
            "digest mismatch" if torn == "trading_core" else "ok",
        ],
        [
            "wheel_source_digest_fno_math",
            "WARN" if torn == "fno_math" else "PASS",
            "digest mismatch" if torn == "fno_math" else "ok",
        ],
    ]


class TestPartialInstallSiblingTwins:
    """§10 tests 23(c)/(f) — the torn-sibling fall-through, both role orders.

    Crate A ends UNLOADABLE (install truncated) or never-installed (build
    timeout); sibling B sits INSTALLED at ``to_sha`` (the torn version).  The
    rollback path must ALWAYS digest-check the loadable venv and repair the
    torn sibling — ``repair="rebuilt"`` when the sibling rebuild lands,
    ``repair="failed_stale"`` (WARN-class, never CRITICAL) when it fails.
    """

    @pytest.mark.parametrize("sibling_repair_ok", [True, False])
    def test_build_timeout_leaves_torn_sibling_digest_checked(
        self, tmp_path: Path, sibling_repair_ok: bool
    ) -> None:
        # §10 test 23(c) — fno_math builds + installs OK (to_sha), then the
        # trading_core BUILD times out ⇒ rollback ⇒ smoke LOADABLE (the old
        # trading_core wheel is intact) ⇒ the coherence check catches the
        # to_sha-built fno_math ⇒ the mismatch repair fires for exactly it
        # (the row-10 door into row 10c).  The role order is FIXED by the
        # bootstrap build order: fno_math builds FIRST, so only the
        # first-succeeds/second-fails shape can leave a torn sibling on the
        # build-failure door (the short-circuit law); the gate-door twin
        # below parametrizes BOTH role orders.
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/trading-core/src/lib.rs",))
        tc_dir = repo / "rust/trading-core"
        builds = {"n": 0}

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            builds["n"] += 1
            if runner.calls[-1]["cwd"] == tc_dir and builds["n"] <= len(WHEEL_SET):
                return (124, "", "")  # the forward trading_core build times out
            if not sibling_repair_ok and builds["n"] > len(WHEEL_SET):
                return (1, "", "repair boom")  # the sibling repair fails
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        runner.state["doctor"] = 0

        def _doctor(_a: list[str]) -> tuple[int, str, str]:
            runner.state["doctor"] += 1
            # call 1 = coherence (fno_math torn); call 2 = the post-repair
            # re-check (PASS on the ok arm, still WARN on the failed arm).
            if runner.state["doctor"] == 1 or not sibling_repair_ok:
                return (0, _gate_stdout(_torn_checks("fno_math")), "")
            return (0, _gate_stdout(), "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _doctor,
        )
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.gate == "skipped"  # the build-failure door — the gate never ran
        assert out.repair == ("rebuilt" if sibling_repair_ok else "failed_stale")
        by_name = {w.name: w for w in out.wheels}
        assert by_name["trading_core"].result == "timeout"
        assert by_name["fno_math"].result == "ok"
        assert by_name["fno_math"].digest_ok is sibling_repair_ok
        # The mismatch repair rebuilt EXACTLY the torn sibling (never the
        # timed-out crate — its old wheel matches from_sha and reads PASS).
        forward = [c["cwd"] for c in runner.maturin_calls()[:2]]
        repairs = [c["cwd"] for c in runner.maturin_calls()[2:]]
        assert forward == [repo / "rust/fno-math", tc_dir]
        assert repairs == [repo / "rust/fno-math"]
        # Order pin: the coherence check runs strictly AFTER the checkout -B.
        argvs = runner.argvs()
        checkout_i = next(
            i for i, a in enumerate(argvs) if a[:4] == ["git", "checkout", "-B", "main"]
        )
        coherence_i = runner.calls.index(runner.coherence_calls()[0])
        assert checkout_i < coherence_i
        events = {e["event"]: e for e in logs}
        if sibling_repair_ok:
            assert "box_update.repair_failed_stale" not in events
        else:
            assert events["box_update.repair_failed_stale"]["log_level"] == "warning"
        assert "box_update.required_wheel_unloadable" not in events  # never the CRITICAL arm

    @pytest.mark.parametrize(
        ("unloadable", "torn"),
        [("trading_core", "fno_math"), ("fno_math", "trading_core")],
    )
    @pytest.mark.parametrize("sibling_repair_ok", [True, False])
    def test_install_truncation_sibling_fall_through(
        self, tmp_path: Path, unloadable: str, torn: str, sibling_repair_ok: bool
    ) -> None:
        # §10 test 23(f) — every forward build reports rc 0 but crate A's
        # INSTALL is torn (unloadable .so) ⇒ the gate reads import_failed ⇒
        # rollback ⇒ smoke rc≠0 ⇒ the 10b repair rebuilds A; the re-smoke is
        # loadable ⇒ the coherence check STILL runs (the fall-through pin —
        # the fails-before-round-7 shape terminated at repair="rebuilt" with
        # ZERO check argv, leaving sibling B silently torn at to_sha) ⇒ the
        # check WARNs on B ⇒ the mismatch repair rebuilds exactly B — TWO
        # repair builds total, one per crate (the per-crate at-most-once
        # pin).  Parametrized over BOTH role orders + the sibling-repair
        # outcome (rebuilt / failed_stale).
        repo = _mk_repo(tmp_path)
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        crate_dir = {s.name: repo / s.crate_dir for s in WHEEL_SET}
        builds = {"n": 0}

        def _maturin(_a: list[str]) -> tuple[int, str, str]:
            builds["n"] += 1
            if (
                not sibling_repair_ok
                and builds["n"] > len(WHEEL_SET)
                and runner.calls[-1]["cwd"] == crate_dir[torn]
            ):
                return (1, "", "sibling repair boom")
            return (0, "", "")

        runner.on(lambda a: a[0].endswith("maturin"), _maturin)
        runner.state["combined_smokes"] = 0

        def _smoke(a: list[str]) -> tuple[int, str, str]:
            if a[2] == "import trading_core, fno_math":
                runner.state["combined_smokes"] += 1
                # Smoke #1 (pre-repair): the truncated .so fails the import;
                # every later combined smoke is loadable (A was repaired).
                if runner.state["combined_smokes"] == 1:
                    return (1, "", "truncated .so")
                return (0, "", "")
            if a[2] == f"import {unloadable}":
                return (1, "", "truncated .so")
            return (0, "", "")

        runner.on(
            lambda a: (
                a[0] == sys.executable
                and a[1] == "-c"
                and a[2].startswith("import ")
                and "brutex.doctor" not in a[2]
            ),
            _smoke,
        )
        runner.state["doctor"] = 0

        def _doctor(a: list[str]) -> tuple[int, str, str]:
            if bu.GATE_SMOKE_IMPORTS in a[2]:
                # The §4.5 gate child dies on the truncated import — rc≠0
                # BEFORE the import marker ⇒ gate="import_failed".
                return (1, "", "ImportError: truncated .so")
            runner.state["doctor"] += 1
            # Coherence #1: sibling B torn; #2 (post-repair re-check): PASS
            # on the ok arm, still WARN on the failed arm.
            if runner.state["doctor"] == 1 or not sibling_repair_ok:
                return (0, _gate_stdout(_torn_checks(torn)), "")
            return (0, _gate_stdout(), "")

        runner.on(
            lambda a: a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2],
            _doctor,
        )
        with capture_logs() as logs:
            out = _run(repo, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.gate == "import_failed"
        assert out.repair == ("rebuilt" if sibling_repair_ok else "failed_stale")
        # TWO repair builds total, one per crate, unloadable A FIRST then the
        # torn sibling B — a second build of EITHER crate FAILS this pin.
        repairs = [c["cwd"] for c in runner.maturin_calls()[len(WHEEL_SET) :]]
        assert repairs == [crate_dir[unloadable], crate_dir[torn]]
        # The fall-through pin: the FIRST coherence check runs strictly AFTER
        # the loadable re-smoke (never terminated at the 10b repair alone).
        re_smoke_i = runner.calls.index(runner.smoke_calls()[1])
        coherence_i = runner.calls.index(runner.coherence_calls()[0])
        assert re_smoke_i < coherence_i
        by_name = {w.name: w for w in out.wheels}
        assert by_name[torn].digest_ok is sibling_repair_ok
        events = [e for e in logs if e["event"] == "box_update.repair_failed_stale"]
        if sibling_repair_ok:
            assert events == []
        else:
            assert len(events) == 1 and events[0]["log_level"] == "warning"
        # Loadability was restored — never the CRITICAL CLI-down arm.
        assert not any(e["event"] == "box_update.required_wheel_unloadable" for e in logs)


# ── alarm demotion log-text arm (§10 test 19(b) — the CRITICAL-class fold) ─
class TestAlarmDemotionLogText:
    def _hard_fail(self, repo: Path, state: Path) -> tuple[BoxUpdateOutcome, list[dict[str, Any]]]:
        """One repair=failed night: required build fails + every smoke fails."""
        runner = _std_runner(repo, diff_paths=("rust/fno-math/src/lib.rs",))
        fno_dir = repo / "rust/fno-math"
        runner.on(
            lambda a: a[0].endswith("maturin"),
            lambda a: (1, "", "boom") if runner.calls[-1]["cwd"] == fno_dir else (0, "", ""),
        )
        runner.on(
            lambda a: (
                a[0] == sys.executable
                and a[1] == "-c"
                and a[2].startswith("import ")
                and "brutex.doctor" not in a[2]
            ),
            (1, "", "still broken"),
        )
        with capture_logs() as logs:
            out = _run(repo, runner, state_dir=state)
        return out, list(logs)

    def test_repeat_night_demotes_critical_class_to_warn(self, tmp_path: Path) -> None:
        # Night 1: the CRITICAL-class event at FULL severity, no repeat field.
        repo = _mk_repo(tmp_path)
        state = tmp_path / "state"
        out1, logs1 = self._hard_fail(repo, state)
        assert out1.status is BoxUpdateStatus.ERROR
        assert out1.repair == "failed"
        crit1 = [e for e in logs1 if e["event"] == "box_update.required_wheel_unloadable"]
        assert len(crit1) == 1 and crit1[0]["log_level"] == "critical"
        assert "repeat" not in crit1[0]
        # Night 2, SAME signature: the CRITICAL-class event DEMOTED to WARN,
        # the log integer is the TOTAL-nights count (repeat=2 = nights) and
        # the remediation text survives the demotion (the log-text arm).
        out2, logs2 = self._hard_fail(repo, state)
        assert out2.alarm_signature == out1.alarm_signature
        assert out2.alarm_repeats == 1
        crit2 = [e for e in logs2 if e["event"] == "box_update.required_wheel_unloadable"]
        assert len(crit2) == 1 and crit2[0]["log_level"] == "warning"  # demoted
        assert crit2[0]["repeat"] == 2
        assert crit2[0]["repeat"] == out2.alarm_repeats + 1  # the two-integer law
        assert crit2[0]["signature"] == out2.alarm_signature
        assert "maturin develop --release" in crit2[0]["detail"]
        assert "HARD CLI-DOWN" in crit2[0]["detail"]
        # The bare command alone strands a fresh shell (maturin-not-found) —
        # the detail must point at the runbook's full env-prefixed form.
        assert "docs/runbooks/box-autodeploy.md" in crit2[0]["detail"]


# ── mutation-workflow wiring pin (§10 CI gating — the #1614/#1832 precedent) ─
class TestMutationWorkflowWiring:
    """Pin ``box_update.py`` INTO ``mutation.yml``'s zero-survivor mutmut loop.

    The pair rides the historical runner (empty third field — the 3-field
    ``module:testfile:timeout`` grammar of DEC-MUTATION-RUNTIME-003); this
    deterministic pin fails if the pair is ever dropped.  HONEST: it pins
    the WIRING only — the weekly run executes on its own schedule.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/nightly/box_update.py:tests/unit/test_nightly_box_update.py:"' in text, (
            "box_update.py is not wired into mutation.yml's per-module mutmut loop"
        )
