"""Unit tests for the INTERIM Run-Sweep launch core (DEC-SWEEP-LAUNCH-BUTTON-INTERIM-001).

Covers ``brutex/dashboard/launch_button.py``'s pure/core surface at 100%
line + branch: the LOCKED command builder (every flag verified against the
real ``cli.py`` argparse; the SLA acknowledgment strictly OPT-IN), the
``end = today + 1`` exclusive-end correction, the SEPARATE keep-awake
watcher (review round-2 M1 — the pidfile always holds the real sweep pid),
the dynamic PR #1264 futures-alias probe (all three arms), the cheap
preconditions (every LOCKED TF-5 interval partition — M4), the flock + pidfile
single-flight with the pre-spawn placeholder + compensation (M2), pid
liveness probing (waitpid + kill + best-effort cmdline disambiguation, m4),
the bounded + confined log tail (m2), and the page-state projections. The
spawner is ALWAYS injected — no test launches a real sweep (the default
spawner is exercised with harmless ``python -c`` argvs).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.cli import EXTINCTION_K_TOKEN, build_parser
from brutex.dashboard import launch_button as lb
from brutex.dashboard.sweep_runner import DEFAULT_K
from brutex.data.timeframes import LAKE_INTERVAL_LABELS

# ── locked command builder ──────────────────────────────────────────


class TestLockedSweepArgv:
    def test_futures_card_locked_flags(self) -> None:
        argv = lb.locked_sweep_argv(
            "futures",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/run1"),
            end_day=date(2026, 7, 9),
            python_executable="pyX",
        )
        assert argv[:4] == ["pyX", "-m", "brutex.cli", "sweep-all"]
        joined = " ".join(argv)
        assert "--symbols NSE-NIFTY-FUT" in joined
        assert "--exchange NSE" in joined
        assert "--segment FNO" in joined
        assert "--start 2020-01-01" in joined
        assert "--end 2026-07-09" in joined
        assert "--directions BUY,SELL" in joined
        assert "--k extinction" in joined  # DEC-DASHBOARD-DRILL-TF5-DYNK-001: dynamic-k
        assert "--k 1,2,3,4,5" not in joined  # the fixed-k audit lock is RETIRED
        assert "--sl none --tp none" in joined
        # Extinction arm: --max-combos OMITTED — an explicit cap would TRIM the
        # ladder to the C(n, k) <= cap prefix (silently re-pinning k<=6).
        assert "--max-combos" not in joined
        assert "--top 20" in joined
        assert "--results-root /lake/sweeps/run1" in joined
        assert "--lake-root /lake" in joined

    def test_numeric_k_rollback_restores_max_combos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one-constant rollback: a numeric-CSV ``LOCKED_K`` restores the cap.

        DEC-DASHBOARD-DRILL-TF5-DYNK-001's kill lever — setting ``LOCKED_K``
        back to an explicit k-list automatically re-appends ``--max-combos``
        at :data:`~brutex.dashboard.launch_button.LOCKED_MAX_COMBOS`.
        """
        monkeypatch.setattr(lb, "LOCKED_K", "1,2,3,4,5")
        argv = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
        )
        joined = " ".join(argv)
        assert "--k 1,2,3,4,5" in joined
        assert "--max-combos 50100000" in joined
        # Flag order stays argparse-valid on the legacy arm too.
        parser = build_parser()
        ns = parser.parse_args(argv[3:])
        assert ns.k == "1,2,3,4,5"
        assert ns.max_combos == 50_100_000

    def test_spot_card_locked_flags(self) -> None:
        argv = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/run2"),
            end_day=date(2026, 7, 9),
            python_executable="pyX",
        )
        joined = " ".join(argv)
        assert "--symbols NIFTY" in joined
        assert "--segment INDEX" in joined
        assert "--exchange NSE" in joined

    def test_sla_ack_is_opt_in_never_default(self) -> None:
        """The flag rides ONLY an explicit acknowledgment.

        The checkbox is the operator's deliberate override, never a
        default — posture unchanged in kind by
        DEC-DASHBOARD-DRILL-TF5-DYNK-001 (the k-aware guard is EXPECTED to
        refuse the drill run without the ack; see the SLA_ACK_FLAG note).
        """
        base = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
        )
        assert lb.SLA_ACK_FLAG not in base
        acked = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
            acknowledge_sla=True,
        )
        assert acked[-1] == lb.SLA_ACK_FLAG
        assert acked[:-1] == base

    def test_default_python_executable_is_sys_executable(self) -> None:
        argv = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/run3"),
            end_day=date(2026, 7, 9),
        )
        assert argv[0] == sys.executable

    def test_unknown_card_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown launch card"):
            lb.locked_sweep_argv(
                "options",
                lake_root=Path("/lake"),
                results_root=Path("/lake/sweeps/x"),
                end_day=date(2026, 7, 9),
            )

    def test_tf5_drill_intervals_locked(self) -> None:
        """DEC-DASHBOARD-DRILL-TF5-DYNK-001: the TF-5 drill set, a strict lake subset."""
        assert lb.LOCKED_INTERVALS == ("1minute", "2minute", "3minute", "5minute", "15minute")
        # Strict SUBSET of the 21-TF lake registry — labels resolved through
        # LAKE_INTERVAL_LABELS (never re-minted), registry itself untouched.
        assert set(lb.LOCKED_INTERVALS) < set(LAKE_INTERVAL_LABELS.values())
        assert "1day" not in lb.LOCKED_INTERVALS

    def test_locked_k_is_the_extinction_token(self) -> None:
        """LOCKED_K == sweep_runner.DEFAULT_K == cli.EXTINCTION_K_TOKEN (drift pin)."""
        assert lb.LOCKED_K == DEFAULT_K
        assert lb.LOCKED_K == EXTINCTION_K_TOKEN

    def test_every_locked_flag_exists_on_real_cli_argparse(self) -> None:
        """The locked command (with AND without ack) parses through main's REAL argparse."""
        parser = build_parser()
        argv = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
            acknowledge_sla=True,
        )
        ns = parser.parse_args(argv[3:])  # drop python -m brutex.cli
        assert ns.command == "sweep-all"
        assert ns.symbols == "NIFTY"
        assert ns.end == "2026-07-09"
        # Extinction arm: cap omitted — argparse default None (the ladder is
        # never TRIMMED; the mask-bar breaker + min-hits extinction govern).
        assert ns.max_combos is None
        assert ns.k == EXTINCTION_K_TOKEN
        assert ns.top == 20
        assert ns.acknowledge_sla_overrun is True
        assert ns.intervals.count(",") == 4  # 5 timeframes — the TF-5 drill set
        bare = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
        )
        assert parser.parse_args(bare[3:]).acknowledge_sla_overrun is False

    def test_end_date_is_exclusive_corrected(self) -> None:
        """Audit correction (a): bare-date --end excludes that session ⇒ today+1."""
        assert lb.launch_end_date(date(2026, 7, 8)) == date(2026, 7, 9)
        assert lb.launch_end_date(date(2026, 12, 31)) == date(2027, 1, 1)


class TestKeepawakeWatcher:
    """Review round-2 M1: keep-awake is a SEPARATE watcher of the sweep pid."""

    def test_watcher_argv_watches_the_sweep_pid(self) -> None:
        argv = lb.keepawake_watcher_argv(4242, which=lambda name: f"/usr/bin/{name}")
        assert argv == ["/usr/bin/caffeinate", "-dimsu", "-w", "4242"]

    def test_degrades_gracefully_without_caffeinate(self) -> None:
        assert lb.keepawake_watcher_argv(4242, which=lambda name: None) is None

    def test_spawn_watcher_records_pid(self) -> None:
        record: dict[str, Any] = {}

        def _spawn(argv: list[str]) -> int:
            record["argv"] = argv
            return 777

        pid = lb._spawn_keepawake_watcher(
            4242, which=lambda name: "/usr/bin/caffeinate", watcher_spawner=_spawn
        )
        assert pid == 777
        assert record["argv"][-2:] == ["-w", "4242"]

    def test_spawn_watcher_absent_caffeinate_is_none(self) -> None:
        assert lb._spawn_keepawake_watcher(1, which=lambda name: None) is None

    def test_spawn_watcher_failure_never_fails_the_launch(self) -> None:
        def _boom(argv: list[str]) -> int:
            raise OSError("spawn failed")

        pid = lb._spawn_keepawake_watcher(
            4242, which=lambda name: "/usr/bin/caffeinate", watcher_spawner=_boom
        )
        assert pid is None

    def test_default_watcher_spawner_detached_harmless(self) -> None:
        pid = lb._default_watcher_spawner([sys.executable, "-c", "pass"])
        assert pid > 0
        assert os.getpgid(pid) != os.getpgid(0)  # own session — detached
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited":
                break
            time.sleep(0.05)
        assert lb.probe_pid(pid) == "exited"


# ── futures-alias probe (PR #1264 dependency) ───────────────────────


class TestProbeFuturesAlias:
    def test_refused_spot_only_names_the_dec(self) -> None:
        verdict = lb.probe_futures_alias()
        # DEC-ENGINE-SPOT-ONLY-001: the alias is scope-refused — the probe
        # must say so honestly, citing the DEC (were a future DEC to re-widen
        # the scope law, this arm would flip available and the companion test
        # below pins the available projection shape).
        assert verdict.available is False
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason
        assert "spot-only" in verdict.reason

    def test_available_when_classifier_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import universe

        def _allow(*, exchange: str, segment: str, symbol: str) -> Any:
            assert (exchange, segment, symbol) == ("NSE", "FNO", "NSE-NIFTY-FUT")
            return SimpleNamespace(allowed=True, reason="alias layer merged")

        monkeypatch.setattr(universe, "classify_engine_sweep_target", _allow)
        verdict = lb.probe_futures_alias()
        assert verdict.available is True
        assert verdict.reason == "alias layer merged"

    def test_refused_when_classifier_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import universe

        monkeypatch.setattr(
            universe,
            "classify_engine_sweep_target",
            lambda **_: SimpleNamespace(allowed=False, reason="nope"),
        )
        verdict = lb.probe_futures_alias()
        assert verdict.available is False
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason
        assert "nope" in verdict.reason

    def test_guarded_import_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            lb.importlib,
            "import_module",
            lambda name: (_ for _ in ()).throw(ImportError("gone")),
        )
        verdict = lb.probe_futures_alias()
        assert verdict.available is False
        assert "scope classifier unavailable" in verdict.reason
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason

    def test_guarded_attribute_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lb.importlib, "import_module", lambda name: SimpleNamespace())
        verdict = lb.probe_futures_alias()
        assert verdict.available is False
        assert "AttributeError" in verdict.reason


# ── preconditions ────────────────────────────────────────────────────


def _fake_usage(free: int) -> Any:
    return lambda path: SimpleNamespace(total=free * 2, used=free, free=free)


def _check(checks: list[lb.PreconditionCheck], name: str) -> lb.PreconditionCheck:
    matched = [c for c in checks if c.name == name]
    assert len(matched) == 1, f"expected exactly one {name!r} check"
    return matched[0]


def _make_lake(tmp_path: Path, *, intervals: tuple[str, ...] = lb.LOCKED_INTERVALS) -> Path:
    """A lake with the given NIFTY/INDEX interval partition dirs present."""
    for interval in intervals:
        (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / interval).mkdir(parents=True)
    (tmp_path / "sweeps").mkdir(exist_ok=True)
    return tmp_path


class TestWheelsCheck:
    def test_ok_with_real_wheels(self) -> None:
        check = lb._wheels_check()
        assert check.name == "rust_wheels"
        assert check.ok is True

    def test_missing_wheel(self) -> None:
        check = lb._wheels_check(find_spec=lambda name: None)
        assert check.ok is False
        assert "fno_math" in check.detail
        assert "trading_core" in check.detail

    def test_raising_probe_counts_missing(self) -> None:
        def _boom(name: str) -> Any:
            raise ImportError(name)

        check = lb._wheels_check(find_spec=_boom)
        assert check.ok is False

    def test_value_error_probe_counts_missing(self) -> None:
        def _boom(name: str) -> Any:
            raise ValueError(name)

        check = lb._wheels_check(find_spec=_boom)
        assert check.ok is False


class TestRunPreconditions:
    def test_no_lake_configured(self) -> None:
        checks = lb.run_preconditions(None)
        assert _check(checks, "lake_configured").ok is False
        assert _check(checks, "rust_wheels").ok is True
        assert len(checks) == 2

    def test_full_lake_all_green(self, tmp_path: Path) -> None:
        lake = _make_lake(tmp_path)
        checks = lb.run_preconditions(lake, min_free_bytes=1, disk_usage=_fake_usage(10 * 1024**3))
        assert all(c.ok for c in checks), [c for c in checks if not c.ok]
        assert _check(checks, "results_root_writable").detail == str(lake / "sweeps")
        assert "all 5" in _check(checks, "nifty_interval_partitions").detail

    def test_missing_partitions_named_and_low_disk(self, tmp_path: Path) -> None:
        """M4: the presence check covers every LOCKED (TF-5) interval, naming the missing."""
        _make_lake(tmp_path, intervals=("1minute", "5minute"))
        checks = lb.run_preconditions(
            tmp_path, min_free_bytes=10 * 1024**3, disk_usage=_fake_usage(1)
        )
        partitions = _check(checks, "nifty_interval_partitions")
        assert partitions.ok is False
        assert "missing 3/5" in partitions.detail
        assert "2minute" in partitions.detail
        assert "15minute" in partitions.detail
        assert _check(checks, "disk_free").ok is False

    def test_empty_lake_all_partitions_missing(self, tmp_path: Path) -> None:
        checks = lb.run_preconditions(tmp_path, disk_usage=_fake_usage(10**12))
        partitions = _check(checks, "nifty_interval_partitions")
        assert partitions.ok is False
        assert "missing 5/5" in partitions.detail
        # no sweeps dir yet → the writable probe falls back to the lake root
        assert _check(checks, "results_root_writable").detail == str(tmp_path)

    def test_disk_usage_oserror_is_honest_failure(self, tmp_path: Path) -> None:
        def _boom(path: str) -> Any:
            raise OSError("volume wedged")

        checks = lb.run_preconditions(tmp_path, disk_usage=_boom)
        check = _check(checks, "disk_free")
        assert check.ok is False
        assert "disk_usage failed" in check.detail

    def test_active_launch_running_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lb, "_cmdline_contains_sweep_all", lambda pid: True)
        lb._write_launch_state(tmp_path, {"pid": os.getpid(), "card": "spot"})
        checks = lb.run_preconditions(tmp_path, disk_usage=_fake_usage(10**12))
        check = _check(checks, "no_active_launch")
        assert check.ok is False
        assert "RUNNING" in check.detail

    def test_dead_recorded_pid_is_recoverable(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": -5, "card": "spot"})
        checks = lb.run_preconditions(tmp_path, disk_usage=_fake_usage(10**12))
        check = _check(checks, "no_active_launch")
        assert check.ok is True
        assert "recoverable" in check.detail


class TestDiskUsageSeam:
    """DEC-LAUNCH-DISK-GATE-HERMETIC-001: the production disk probe is
    ``shutil.disk_usage`` resolved at CALL time through the ``disk_usage=None``
    module seam — test-injectable, byte-identical gate in production."""

    def test_default_arg_is_the_none_seam(self) -> None:
        """The def-time-bound ``shutil.disk_usage`` default is gone — pin it."""
        kwdefaults = lb.run_preconditions.__kwdefaults__
        assert kwdefaults is not None
        assert kwdefaults["disk_usage"] is None
        assert kwdefaults["min_free_bytes"] == lb.DEFAULT_MIN_DISK_FREE_BYTES

    def test_default_disk_usage_delegates_to_shutil_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production default IS ``shutil.disk_usage`` (spy-proven)."""
        calls: list[str] = []

        def _spy(path: str) -> Any:
            calls.append(path)
            return SimpleNamespace(total=4, used=2, free=2)

        # lb resolves `shutil.disk_usage` at CALL time — patching the shutil
        # module attribute (the SAME module object lb imported) proves it.
        monkeypatch.setattr(shutil, "disk_usage", _spy)
        assert lb._default_disk_usage("/probe/here").free == 2
        assert calls == ["/probe/here"]

    def test_none_arm_resolves_the_module_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_preconditions`` with NO injection rides the monkeypatchable seam."""
        lake = _make_lake(tmp_path)
        monkeypatch.setattr(lb, "_default_disk_usage", _fake_usage(10**15))
        checks = lb.run_preconditions(lake)
        assert _check(checks, "disk_free").ok is True
        monkeypatch.setattr(lb, "_default_disk_usage", _fake_usage(1))
        checks = lb.run_preconditions(lake)
        assert _check(checks, "disk_free").ok is False


# ── pid probing + liveness + pidfile ─────────────────────────────────


class TestProbePid:
    def test_nonpositive_pid_is_exited(self) -> None:
        assert lb.probe_pid(0) == "exited"
        assert lb.probe_pid(-1) == "exited"

    def test_own_live_child_running_then_reaped_exited(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert lb.probe_pid(proc.pid) == "running"
        finally:
            proc.kill()
            proc.wait()
        # After the wait() reap the pid is gone entirely.
        assert lb.probe_pid(proc.pid) == "exited"

    def test_own_exited_child_zombie_is_reaped_and_exited(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            # Let it exit WITHOUT wait() — a zombie until probe_pid reaps it.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if lb.probe_pid(proc.pid) == "exited":
                    break
                time.sleep(0.05)
            assert lb.probe_pid(proc.pid) == "exited"
        finally:
            # probe_pid already reaped the child via waitpid — tell Popen so
            # its __del__ never emits the "still running" ResourceWarning.
            proc.returncode = 0

    def test_foreign_pid_alive_via_kill_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _not_our_child(pid: int, flags: int) -> Any:
            raise ChildProcessError

        monkeypatch.setattr(lb.os, "waitpid", _not_our_child)
        assert lb.probe_pid(os.getpid()) == "running"

    def test_foreign_pid_dead_via_kill_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _not_our_child(pid: int, flags: int) -> Any:
            raise ChildProcessError

        def _gone(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(lb.os, "waitpid", _not_our_child)
        monkeypatch.setattr(lb.os, "kill", _gone)
        assert lb.probe_pid(12345) == "exited"

    def test_foreign_pid_permission_error_reads_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _not_our_child(pid: int, flags: int) -> Any:
            raise ChildProcessError

        def _other_user(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(lb.os, "waitpid", _not_our_child)
        monkeypatch.setattr(lb.os, "kill", _other_user)
        assert lb.probe_pid(12345) == "running"


class TestCmdlineProbe:
    """Review round-2 m4: best-effort /proc cmdline disambiguation."""

    def test_definitive_sweep_all_true(self, tmp_path: Path) -> None:
        (tmp_path / "42").mkdir()
        (tmp_path / "42" / "cmdline").write_bytes(b"python\x00-m\x00brutex.cli\x00sweep-all\x00")
        assert lb._cmdline_contains_sweep_all(42, proc_root=tmp_path) is True

    def test_definitive_mismatch_false(self, tmp_path: Path) -> None:
        (tmp_path / "42").mkdir()
        (tmp_path / "42" / "cmdline").write_bytes(b"someother\x00process\x00")
        assert lb._cmdline_contains_sweep_all(42, proc_root=tmp_path) is False

    def test_empty_cmdline_is_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "42").mkdir()
        (tmp_path / "42" / "cmdline").write_bytes(b"")
        assert lb._cmdline_contains_sweep_all(42, proc_root=tmp_path) is None

    def test_missing_entry_is_unknown_macos_arm(self, tmp_path: Path) -> None:
        # No /proc entry (macOS has no /proc at all) → honest unknown.
        assert lb._cmdline_contains_sweep_all(42, proc_root=tmp_path / "noproc") is None


class TestStateLiveness:
    def test_running_pid_with_unknown_cmdline_stays_running(self) -> None:
        """m4: a probe ERROR must never downgrade a live run (safe = refuse dup)."""
        state = {"pid": os.getpid()}
        assert lb.state_liveness(state, cmdline_probe=lambda pid: None) == "running"

    def test_recycled_pid_definitive_mismatch_reads_exited(self) -> None:
        state = {"pid": os.getpid()}
        assert lb.state_liveness(state, cmdline_probe=lambda pid: False) == "exited"

    def test_dead_pid_exited_without_cmdline_probe(self) -> None:
        assert lb.state_liveness({"pid": -4}) == "exited"

    def test_fresh_launching_placeholder_is_running(self) -> None:
        now = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)
        state = {"phase": "launching", "started_utc": now.isoformat()}
        assert lb.state_liveness(state, now_utc=lambda: now + timedelta(seconds=5)) == "running"

    def test_stranded_launching_placeholder_self_heals(self) -> None:
        now = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)
        state = {"phase": "launching", "started_utc": now.isoformat()}
        late = now + timedelta(seconds=lb.LAUNCHING_GRACE_S + 1)
        assert lb.state_liveness(state, now_utc=lambda: late) == "exited"

    def test_junk_placeholder_timestamp_is_stranded(self) -> None:
        state = {"phase": "launching", "started_utc": "not-a-timestamp"}
        assert lb.state_liveness(state) == "exited"


class TestReadLaunchState:
    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert lb.read_launch_state(tmp_path) is None

    def test_valid_roundtrip(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": 7, "card": "spot"})
        state = lb.read_launch_state(tmp_path)
        assert state == {"pid": 7, "card": "spot"}

    def test_corrupt_json_is_none_logged(self, tmp_path: Path) -> None:
        path = lb.launch_state_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert lb.read_launch_state(tmp_path) is None

    def test_non_object_json_is_none(self, tmp_path: Path) -> None:
        path = lb.launch_state_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2]", encoding="utf-8")
        assert lb.read_launch_state(tmp_path) is None

    def test_unreadable_path_is_none_logged(self, tmp_path: Path) -> None:
        # A DIRECTORY at the pidfile path raises IsADirectoryError (an
        # OSError that is NOT FileNotFoundError) — the guarded arm.
        lb.launch_state_path(tmp_path).mkdir(parents=True)
        assert lb.read_launch_state(tmp_path) is None

    def test_junk_pid_reads_dead(self) -> None:
        assert lb._state_pid({"pid": "junk"}) == -1
        assert lb._state_pid({"pid": None}) == -1
        assert lb._state_pid({}) == -1
        assert lb._state_pid({"pid": 42}) == 42


# ── single-flight launch ─────────────────────────────────────────────


def _spawn_recorder(record: dict[str, Any], pid: int = 991) -> Any:
    def _spawn(argv: list[str], log_path: Path) -> int:
        record["argv"] = argv
        record["log_path"] = log_path
        return pid

    return _spawn


class TestLaunchSweep:
    def test_unknown_card_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown launch card"):
            lb.launch_sweep("bogus", lake_root=tmp_path, spawner=_spawn_recorder({}))

    def test_fresh_launch_writes_pidfile_and_spawns_locked_command(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        watcher_record: dict[str, Any] = {}

        def _watcher(argv: list[str]) -> int:
            watcher_record["argv"] = argv
            return 888

        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=4242),
            which=lambda name: "/usr/bin/caffeinate",
            watcher_spawner=_watcher,
            now_utc=lambda: datetime(2026, 7, 8, 16, 0, tzinfo=UTC),
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        assert out.status == "started"
        assert out.active is not None
        assert out.active["pid"] == 4242
        assert out.active["phase"] == "running"
        # M1: the SWEEP argv is spawned directly (never caffeinate-wrapped);
        # keep-awake is the SEPARATE watcher of the real sweep pid.
        argv = record["argv"]
        assert argv[0] == sys.executable
        assert "caffeinate" not in " ".join(argv)
        assert watcher_record["argv"] == ["/usr/bin/caffeinate", "-dimsu", "-w", "4242"]
        assert out.active["keepawake_pid"] == 888
        # coordinator delta: no ack by default
        assert lb.SLA_ACK_FLAG not in argv
        assert out.active["acknowledge_sla"] is False
        # end=today+1 correction
        assert argv[argv.index("--end") + 1] == "2026-07-09"
        # fresh timestamped results root under <lake>/sweeps
        results_root = Path(out.active["results_root"])
        assert results_root.parent == tmp_path / "sweeps"
        assert results_root.name == "launch-20260708-160000-spot"
        assert results_root.is_dir()
        assert record["log_path"] == results_root / "launch.log"
        # durable pidfile — finalized with the real sweep pid
        state = lb.read_launch_state(tmp_path)
        assert state is not None
        assert state["pid"] == 4242
        assert state["card"] == "spot"
        assert state["phase"] == "running"

    def test_acknowledged_launch_appends_the_flag(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record),
            which=lambda name: None,
            acknowledge_sla=True,
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        assert record["argv"][-1] == lb.SLA_ACK_FLAG
        assert out.active is not None
        assert out.active["acknowledge_sla"] is True

    def test_second_click_while_alive_is_already_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lb, "_cmdline_contains_sweep_all", lambda pid: True)
        lb._write_launch_state(tmp_path, {"pid": os.getpid(), "card": "spot", "results_root": "x"})
        record: dict[str, Any] = {}
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
        assert out.started is False
        assert out.status == "already_running"
        assert out.active is not None
        assert out.active["pid"] == os.getpid()
        assert "argv" not in record  # NEVER a duplicate launch

    def test_fresh_launching_placeholder_blocks_duplicates(self, tmp_path: Path) -> None:
        """M2: a mid-flight launch (placeholder) refuses a second click."""
        now = datetime.now(UTC)
        lb._write_launch_state(
            tmp_path,
            {"phase": "launching", "card": "spot", "started_utc": now.isoformat()},
        )
        record: dict[str, Any] = {}
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
        assert out.started is False
        assert out.status == "already_running"
        assert "argv" not in record

    def test_stranded_placeholder_self_heals(self, tmp_path: Path) -> None:
        """M2: a crash-stranded placeholder past the grace window recovers."""
        old = datetime.now(UTC) - timedelta(seconds=lb.LAUNCHING_GRACE_S + 60)
        lb._write_launch_state(
            tmp_path,
            {"phase": "launching", "card": "futures", "started_utc": old.isoformat()},
        )
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=555),
            which=lambda name: None,
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        state = lb.read_launch_state(tmp_path)
        assert state is not None
        assert state["pid"] == 555

    def test_stale_dead_pid_self_heals(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": -7, "card": "futures"})
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=555),
            which=lambda name: None,
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        state = lb.read_launch_state(tmp_path)
        assert state is not None
        assert state["pid"] == 555

    def test_lock_contended_returns_unstarted(self, tmp_path: Path) -> None:
        lock_path = lb.launch_lock_path(tmp_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record: dict[str, Any] = {}
            out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
            assert out.started is False
            assert out.status == "lock_contended"
            assert "argv" not in record
        finally:
            os.close(fd)

    def test_prespawn_write_failure_spawns_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M2: a placeholder write failure is an honest FAILED — no process."""

        def _boom(lake_root: Path, state: dict[str, Any]) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(lb, "_write_launch_state", _boom)
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot", lake_root=tmp_path, spawner=_spawn_recorder(record), which=lambda name: None
        )
        assert out.started is False
        assert out.status == "failed"
        assert "nothing launched" in out.detail
        assert "argv" not in record

    def test_finalize_write_failure_kills_the_spawned_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M2 compensation: finalize failure → the just-spawned run is killed."""
        real_write = lb._write_launch_state
        calls: list[str] = []

        def _fail_finalize(lake_root: Path, state: dict[str, Any]) -> None:
            if state.get("phase") == "launching":
                # both the pre-spawn placeholder AND the FIX-A pid-stamp
                calls.append("placeholder" if state.get("pid") is None else "pid_stamp")
                real_write(lake_root, state)
                return
            calls.append("finalize")
            raise OSError("torn volume")

        kills: list[int] = []

        def _record_kill(pid: int) -> bool:
            kills.append(pid)
            return True

        monkeypatch.setattr(lb, "_write_launch_state", _fail_finalize)
        monkeypatch.setattr(lb, "_kill_session", _record_kill)
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=6161),
            which=lambda name: None,
        )
        assert calls == ["placeholder", "pid_stamp", "finalize"]
        assert out.started is False
        assert out.status == "failed"
        assert kills == [6161]
        assert "DELIVERED" in out.detail

    def test_finalize_failure_with_failed_kill_is_disclosed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = lb._write_launch_state

        def _fail_finalize(lake_root: Path, state: dict[str, Any]) -> None:
            if state.get("phase") == "launching":
                real_write(lake_root, state)
                return
            raise OSError("torn volume")

        monkeypatch.setattr(lb, "_write_launch_state", _fail_finalize)
        monkeypatch.setattr(lb, "_kill_session", lambda pid: False)
        out = lb.launch_sweep(
            "spot", lake_root=tmp_path, spawner=_spawn_recorder({}), which=lambda name: None
        )
        assert out.status == "failed"
        assert "verify manually" in out.detail

    def test_default_clock_and_today_arms(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot", lake_root=tmp_path, spawner=_spawn_recorder(record), which=lambda name: None
        )
        assert out.started is True
        assert "argv" in record

    def test_default_spawner_arm_via_monkeypatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawner=None resolves the module seam — tests still never launch a sweep."""
        record: dict[str, Any] = {}
        monkeypatch.setattr(lb, "_default_spawner", _spawn_recorder(record, pid=777))
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=None, which=lambda name: None)
        assert out.started is True
        assert record["argv"][-1] == str(tmp_path)  # --lake-root value; NO ack by default


class TestKillSession:
    def test_kills_and_reaps_a_detached_child(self, tmp_path: Path) -> None:
        log = tmp_path / "kill.log"
        pid = lb._default_spawner([sys.executable, "-c", "import time; time.sleep(60)"], log)
        assert lb._kill_session(pid) is True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited":
                break
            time.sleep(0.05)
        assert lb.probe_pid(pid) == "exited"

    def test_nonexistent_pid_reports_failure(self) -> None:
        assert lb._kill_session(2**22 + 12345) is False


class TestDefaultSpawner:
    def test_detached_harmless_process_appends_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "launch.log"
        pid = lb._default_spawner(
            [sys.executable, "-c", "print('spawned-ok', flush=True)"], log_path
        )
        assert pid > 0
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited" and "spawned-ok" in log_path.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.05)
        assert "spawned-ok" in log_path.read_text(encoding="utf-8")

    def test_child_runs_in_its_own_session(self, tmp_path: Path) -> None:
        """m1: kills the start_new_session mutation — the child is DETACHED."""
        log_path = tmp_path / "session.log"
        pid = lb._default_spawner([sys.executable, "-c", "import time; time.sleep(30)"], log_path)
        try:
            assert os.getpgid(pid) != os.getpgid(0)  # own process group/session
        finally:
            lb._kill_session(pid)

    def test_child_stdin_is_devnull(self, tmp_path: Path) -> None:
        """m1: stdin=DEVNULL pinned — a read returns EOF immediately."""
        log_path = tmp_path / "stdin.log"
        pid = lb._default_spawner(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()), flush=True)"],
            log_path,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited":
                break
            time.sleep(0.05)
        assert log_path.read_text(encoding="utf-8").strip() == "0"

    def test_child_env_carries_locked_trio(self, tmp_path: Path) -> None:
        """Audit delta: workers + mem budget (W07_m4_oom_table) + stall floor (W05)."""
        log_path = tmp_path / "env.log"
        pid = lb._default_spawner(
            [
                sys.executable,
                "-c",
                (
                    "import os; print(os.environ['BRUTEX_SWEEP_WORKERS'],"
                    " os.environ['BRUTEX_SWEEP_MEM_BUDGET_BYTES'],"
                    " os.environ['BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC'], flush=True)"
                ),
            ],
            log_path,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited":
                break
            time.sleep(0.05)
        assert log_path.read_text(encoding="utf-8").split() == [
            lb.LOCKED_SWEEP_WORKERS,
            lb.LOCKED_SWEEP_MEM_BUDGET_BYTES,
            lb.LOCKED_STALL_MASK_BARS_PER_SEC,
        ]

    def test_mem_budget_value_parses_through_the_real_fail_loud_parser(self) -> None:
        """The exported literal is accepted by main's sweep_mem_budget resolver."""
        from brutex.engine.sweep_mem_budget import MEM_BUDGET_ENV, resolve_mem_budget_bytes

        assert MEM_BUDGET_ENV == "BRUTEX_SWEEP_MEM_BUDGET_BYTES"
        resolved = resolve_mem_budget_bytes({MEM_BUDGET_ENV: lb.LOCKED_SWEEP_MEM_BUDGET_BYTES})
        assert resolved == 1_073_741_824


# ── log tail + status + page state ──────────────────────────────────


class TestReadLogTail:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert lb.read_log_tail(tmp_path / "nope.log") == ""

    def test_small_file_full_contents(self, tmp_path: Path) -> None:
        path = tmp_path / "a.log"
        path.write_text("hello", encoding="utf-8")
        assert lb.read_log_tail(path) == "hello"

    def test_large_file_bounded_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "big.log"
        path.write_text("x" * 10_000 + "TAIL-END", encoding="utf-8")
        tail = lb.read_log_tail(path, tail_bytes=64)
        assert len(tail) == 64
        assert tail.endswith("TAIL-END")


class TestLogTailConfinement:
    """Review round-2 m2: the tail is confined under <lake>/sweeps."""

    def test_inside_sweeps_is_served(self, tmp_path: Path) -> None:
        log = tmp_path / "sweeps" / "r1" / "launch.log"
        log.parent.mkdir(parents=True)
        log.write_text("ok", encoding="utf-8")
        assert lb._confined_log_tail(tmp_path, str(log), tail_bytes=64) == "ok"

    def test_outside_sweeps_is_refused(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("do not serve", encoding="utf-8")
        out = lb._confined_log_tail(tmp_path, str(secret), tail_bytes=64)
        assert "refused" in out
        assert "do not serve" not in out

    def test_traversal_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "sweeps").mkdir()
        (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
        sneaky = str(tmp_path / "sweeps" / ".." / "secret.txt")
        out = lb._confined_log_tail(tmp_path, sneaky, tail_bytes=64)
        assert "refused" in out


class TestLaunchStatus:
    def test_no_lake(self) -> None:
        assert lb.launch_status(None) == {"present": False, "running": False}

    def test_no_state(self, tmp_path: Path) -> None:
        assert lb.launch_status(tmp_path) == {"present": False, "running": False}

    def test_running_state_with_log_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lb, "_cmdline_contains_sweep_all", lambda pid: True)
        log = tmp_path / "sweeps" / "r" / "run.log"
        log.parent.mkdir(parents=True)
        log.write_text("progress line", encoding="utf-8")
        lb._write_launch_state(
            tmp_path,
            {
                "phase": "running",
                "pid": os.getpid(),
                "card": "spot",
                "results_root": str(tmp_path / "sweeps" / "r"),
                "log_path": str(log),
                "started_utc": "2026-07-08T16:00:00+00:00",
            },
        )
        status = lb.launch_status(tmp_path)
        assert status["present"] is True
        assert status["running"] is True
        assert status["log_tail"] == "progress line"

    def test_exited_state_without_log_path(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": -3, "card": "spot"})
        status = lb.launch_status(tmp_path)
        assert status["present"] is True
        assert status["running"] is False
        assert status["pid"] is None  # non-positive pid projected as null
        assert status["log_tail"] == ""

    def test_launching_placeholder_reads_running(self, tmp_path: Path) -> None:
        lb._write_launch_state(
            tmp_path,
            {
                "phase": "launching",
                "card": "spot",
                "started_utc": datetime.now(UTC).isoformat(),
            },
        )
        status = lb.launch_status(tmp_path)
        assert status["present"] is True
        assert status["running"] is True
        assert status["phase"] == "launching"


class TestBuildLaunchState:
    def test_full_payload_shape(self, tmp_path: Path) -> None:
        _make_lake(tmp_path)
        payload = lb.build_launch_state(
            tmp_path,
            which=lambda name: None,
            today_ist=lambda: date(2026, 7, 8),
        )
        assert payload["interim"] is True
        assert payload["lake_root"] == str(tmp_path)
        assert payload["end_day"] == "2026-07-09"
        assert payload["grid_note"] == lb.GRID_HONESTY_NOTE
        assert payload["keepawake_available"] is False
        assert {c["name"] for c in payload["preconditions"]} >= {
            "lake_configured",
            "nifty_interval_partitions",
            "disk_free",
            "rust_wheels",
        }
        futures_card = payload["cards"]["futures"]
        spot_card = payload["cards"]["spot"]
        # DEC-ENGINE-SPOT-ONLY-001: the futures alias is scope-refused →
        # the card is disabled with the DEC-citing note; spot stays live.
        # (The probe flips dynamically were the scope law re-widened —
        # pinned by TestProbeFuturesAlias.test_available_when_classifier_allows.)
        assert payload["futures"]["available"] is False
        assert futures_card["enabled"] is False
        assert "DEC-ENGINE-SPOT-ONLY-001" in futures_card["reason"]
        assert spot_card["enabled"] is True
        # coordinator delta: the base display carries NO ack flag; the exact
        # token ships separately for the checkbox-conditional display.
        assert lb.SLA_ACK_FLAG not in spot_card["command_display"]
        assert spot_card["sla_ack_flag"] == lb.SLA_ACK_FLAG
        assert payload["active"] == {"present": False, "running": False}

    def test_keepawake_available_when_caffeinate_found(self, tmp_path: Path) -> None:
        payload = lb.build_launch_state(tmp_path, which=lambda name: f"/usr/bin/{name}")
        assert payload["keepawake_available"] is True

    def test_no_lake_payload(self) -> None:
        payload = lb.build_launch_state(None, which=lambda name: None)
        assert payload["lake_root"] is None
        assert payload["cards"]["spot"]["enabled"] is False
        assert payload["cards"]["spot"]["reason"] == "no lake configured"
        assert payload["active"] == {"present": False, "running": False}

    def test_futures_available_enables_card(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-#1264 projection: the alias resolves ⇒ the futures card is live."""
        monkeypatch.setattr(
            lb,
            "probe_futures_alias",
            lambda: lb.FuturesAvailability(available=True, reason="alias merged"),
        )
        payload = lb.build_launch_state(
            tmp_path, which=lambda name: None, today_ist=lambda: date(2026, 7, 8)
        )
        assert payload["futures"]["available"] is True
        assert payload["cards"]["futures"]["enabled"] is True
        assert payload["cards"]["futures"]["reason"] == ""

    def test_default_today_arm(self, tmp_path: Path) -> None:
        payload = lb.build_launch_state(tmp_path, which=lambda name: None)
        assert payload["end_day"] > date.today().isoformat()  # tomorrow (IST) > today


class TestStoreLakeRootResolver:
    def test_resolves_store_path_parent(self, tmp_path: Path) -> None:
        store = SimpleNamespace(path=tmp_path / "instruments.duckdb")
        assert lb.store_lake_root_resolver(store)() == tmp_path

    def test_none_store_resolves_none(self) -> None:
        assert lb.store_lake_root_resolver(None)() is None

    def test_pathless_store_resolves_none(self) -> None:
        assert lb.store_lake_root_resolver(SimpleNamespace(path="not-a-path"))() is None


class TestActiveLaunchModel:
    def test_as_dict_roundtrips_json(self) -> None:
        active = lb.ActiveLaunch(
            pid=1,
            card="spot",
            results_root="/r",
            log_path="/r/l.log",
            command=("a", "b"),
            started_utc="2026-07-08T16:00:00+00:00",
            keepawake_pid=2,
            acknowledge_sla=True,
        )
        payload = active.as_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["command"] == ["a", "b"]
        assert payload["phase"] == "running"
        assert payload["keepawake_pid"] == 2
        assert payload["acknowledge_sla"] is True


class TestTemplateLoader:
    def test_template_loads_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lb, "_LAUNCH_TEMPLATE_CACHE", None)
        first = lb._load_launch_template()
        assert "INTERIM" in first
        assert lb._load_launch_template() is first  # cached arm


class TestLogTailUnresolvable:
    def test_null_byte_log_path_is_refused(self, tmp_path: Path) -> None:
        """A tampered state file with an embedded null byte cannot crash the page."""
        out = lb._confined_log_tail(tmp_path, "bad\x00path.log", tail_bytes=64)
        assert "refused" in out
        assert "ValueError" in out


class TestLaunchingRecordWithPid:
    """Round-2 refuter FIX-A: a pid-bearing launching record is PROBED, not aged."""

    def test_live_pid_beats_stale_age(self) -> None:
        """A dashboard death before finalize can no longer age a LIVE sweep out."""
        old = datetime.now(UTC) - timedelta(seconds=lb.LAUNCHING_GRACE_S + 600)
        state = {"phase": "launching", "pid": os.getpid(), "started_utc": old.isoformat()}
        verdict = lb.state_liveness(state, cmdline_probe=lambda pid: True)
        assert verdict == "running"

    def test_dead_pid_self_heals_even_within_grace(self) -> None:
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()  # reap — the pid is definitively gone
        state = {
            "phase": "launching",
            "pid": proc.pid,
            "started_utc": datetime.now(UTC).isoformat(),
        }
        assert lb.state_liveness(state) == "exited"

    def test_pidless_placeholder_still_uses_age_grace(self) -> None:
        now = datetime.now(UTC)
        fresh = {"phase": "launching", "started_utc": now.isoformat()}
        assert lb.state_liveness(fresh, now_utc=lambda: now) == "running"

    def test_launch_blocked_by_stale_aged_but_live_launching_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the refuter's duplicate-launch repro now refuses 409-style."""
        monkeypatch.setattr(lb, "_cmdline_contains_sweep_all", lambda pid: True)
        old = datetime.now(UTC) - timedelta(seconds=lb.LAUNCHING_GRACE_S + 600)
        lb._write_launch_state(
            tmp_path,
            {
                "phase": "launching",
                "pid": os.getpid(),
                "card": "spot",
                "started_utc": old.isoformat(),
            },
        )
        record: dict[str, Any] = {}
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
        assert out.started is False
        assert out.status == "already_running"
        assert "argv" not in record  # the live sweep is never duplicated

    def test_fresh_launch_stamps_pid_onto_launching_record_before_finalize(
        self, tmp_path: Path
    ) -> None:
        """The launching record carries the real pid the moment Popen returns."""
        seen: list[dict[str, Any]] = []
        real_write = lb._write_launch_state

        def _spawn_and_snoop(argv: list[str], log_path: Path) -> int:
            return 4243

        def _snoop_write(lake_root: Path, state: dict[str, Any]) -> None:
            seen.append(dict(state))
            real_write(lake_root, state)

        import unittest.mock

        with unittest.mock.patch.object(lb, "_write_launch_state", _snoop_write):
            out = lb.launch_sweep(
                "spot", lake_root=tmp_path, spawner=_spawn_and_snoop, which=lambda name: None
            )
        assert out.started is True
        phases = [(s.get("phase"), s.get("pid")) for s in seen]
        assert phases == [
            ("launching", None),  # pre-spawn placeholder — no pid exists yet
            ("launching", 4243),  # FIX-A: real pid stamped right after Popen
            ("running", 4243),  # finalize
        ]


class TestSpawnFailureUnwind:
    """Round-2 refuter FIX-B: a fork/exec OSError is a clean failed, no lockout."""

    def test_spawn_oserror_failed_placeholder_removed_retry_possible(self, tmp_path: Path) -> None:
        def _boom(argv: list[str], log_path: Path) -> int:
            raise OSError("EAGAIN: resource temporarily unavailable")

        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_boom, which=lambda name: None)
        assert out.started is False
        assert out.status == "failed"
        assert "nothing launched" in out.detail
        assert "EAGAIN" in out.detail
        # the placeholder is unwound — no 120 s lockout
        assert lb.read_launch_state(tmp_path) is None
        # an IMMEDIATE retry succeeds
        record: dict[str, Any] = {}
        retry = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=515),
            which=lambda name: None,
        )
        assert retry.started is True
        assert record["argv"][0] == sys.executable

    def test_pid_stamp_write_failure_kills_the_spawned_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIX-A's stamp write failing compensates exactly like the finalize arm."""
        real_write = lb._write_launch_state

        def _fail_stamp(lake_root: Path, state: dict[str, Any]) -> None:
            if state.get("phase") == "launching" and state.get("pid") is not None:
                raise OSError("torn volume")
            real_write(lake_root, state)

        kills: list[int] = []
        monkeypatch.setattr(lb, "_write_launch_state", _fail_stamp)
        monkeypatch.setattr(lb, "_kill_session", lambda pid: kills.append(pid) or True)
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder({}, pid=6262),
            which=lambda name: None,
        )
        assert out.status == "failed"
        assert kills == [6262]
        assert "pid stamp write failed" in out.detail

    def test_remove_launch_state_missing_file_is_noop(self, tmp_path: Path) -> None:
        lb._remove_launch_state(tmp_path)  # nothing recorded — silently fine
        assert lb.read_launch_state(tmp_path) is None

    def test_remove_launch_state_suppresses_oserror(self, tmp_path: Path) -> None:
        # A DIRECTORY at the pidfile path makes unlink raise IsADirectoryError
        # (an OSError) — suppressed; the placeholder then ages out instead.
        lb.launch_state_path(tmp_path).mkdir(parents=True)
        lb._remove_launch_state(tmp_path)  # must not raise


class TestReapKeepawake:
    """Round-2 refuter FIX-C: exited watcher zombies are opportunistically reaped."""

    def test_exited_watcher_zombie_is_reaped_via_status(self, tmp_path: Path) -> None:
        watcher_pid = lb._default_watcher_spawner([sys.executable, "-c", "pass"])
        # wait for the child to EXIT (zombie — nothing has reaped it yet)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if Path(f"/proc/{watcher_pid}/stat").read_text().split(") ")[1][0] == "Z":
                    break
            except OSError:
                break
            time.sleep(0.05)
        lb._write_launch_state(tmp_path, {"pid": -1, "card": "spot", "keepawake_pid": watcher_pid})
        lb.launch_status(tmp_path)  # the reap rides the status path
        # after the reap the pid is fully gone — kill(0) raises
        with pytest.raises((ProcessLookupError, PermissionError)):
            os.kill(watcher_pid, 0)

    def test_guarded_arms_never_raise(self) -> None:
        lb._reap_keepawake({})  # no keepawake_pid
        lb._reap_keepawake({"keepawake_pid": None})
        lb._reap_keepawake({"keepawake_pid": -2})
        lb._reap_keepawake({"keepawake_pid": "junk"})
        lb._reap_keepawake({"keepawake_pid": 2**22 + 999})  # not our child → suppressed

    def test_reap_never_affects_liveness(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": -1, "card": "spot", "keepawake_pid": 2**22 + 998})
        status = lb.launch_status(tmp_path)
        assert status["running"] is False  # verdict from the SWEEP pid only
