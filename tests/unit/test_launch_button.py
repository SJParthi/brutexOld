"""Unit tests for the INTERIM Run-Sweep launch core (DEC-SWEEP-LAUNCH-BUTTON-INTERIM-001).

Covers ``brutex/dashboard/launch_button.py``'s pure/core surface at 100%
line + branch: the LOCKED command builder (every flag verified against the
real ``cli.py`` argparse), the ``end = today + 1`` exclusive-end correction,
the caffeinate wrap + Linux degrade, the dynamic PR #1264 futures-alias
probe (all three arms), the cheap preconditions, the flock + pidfile
single-flight with stale-pid self-heal, pid liveness probing (waitpid +
kill arms), the bounded log tail, and the page-state projections. The
spawner is ALWAYS injected — no test launches a real sweep (the default
spawner is exercised once with a harmless ``python -c`` argv).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.cli import build_parser
from brutex.dashboard import launch_button as lb

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
        assert "--k 1,2,3,4,5,6" in joined
        assert "--sl none --tp none" in joined
        assert "--max-combos 50100000" in joined
        assert "--top 20" in joined
        assert "--results-root /lake/sweeps/run1" in joined
        assert "--lake-root /lake" in joined
        assert "--acknowledge-sla-overrun" in joined

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

    def test_all_21_intervals_locked(self) -> None:
        assert len(lb.LOCKED_INTERVALS) == 21
        assert lb.LOCKED_INTERVALS[0] == "1minute"
        assert lb.LOCKED_INTERVALS[-1] == "1day"

    def test_every_locked_flag_exists_on_real_cli_argparse(self) -> None:
        """The locked command parses through main's REAL argparse tree."""
        argv = lb.locked_sweep_argv(
            "spot",
            lake_root=Path("/lake"),
            results_root=Path("/lake/sweeps/x"),
            end_day=date(2026, 7, 9),
        )
        parser = build_parser()
        ns = parser.parse_args(argv[3:])  # drop python -m brutex.cli
        assert ns.command == "sweep-all"
        assert ns.symbols == "NIFTY"
        assert ns.end == "2026-07-09"
        assert ns.max_combos == 50_100_000
        assert ns.top == 20
        assert ns.acknowledge_sla_overrun is True
        assert ns.intervals.count(",") == 20  # 21 timeframes

    def test_end_date_is_exclusive_corrected(self) -> None:
        """Audit correction (a): bare-date --end excludes that session ⇒ today+1."""
        assert lb.launch_end_date(date(2026, 7, 8)) == date(2026, 7, 9)
        assert lb.launch_end_date(date(2026, 12, 31)) == date(2027, 1, 1)


class TestWrapKeepawake:
    def test_wraps_when_caffeinate_found(self) -> None:
        out = lb.wrap_keepawake(["a", "b"], which=lambda name: f"/usr/bin/{name}")
        assert out == ["/usr/bin/caffeinate", "-i", "-s", "a", "b"]

    def test_degrades_gracefully_without_caffeinate(self) -> None:
        original = ["a", "b"]
        out = lb.wrap_keepawake(original, which=lambda name: None)
        assert out == ["a", "b"]
        assert out is not original  # defensive copy


# ── futures-alias probe (PR #1264 dependency) ───────────────────────


class TestProbeFuturesAlias:
    def test_refused_on_main_names_pr_1264(self) -> None:
        verdict = lb.probe_futures_alias()
        # On main the alias layer is NOT merged — the probe must say so
        # honestly. (After PR #1264 merges this arm flips to available and
        # the companion test below pins the available projection shape.)
        if not verdict.available:
            assert "PR #1264" in verdict.reason

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
        assert "PR #1264" in verdict.reason
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
        assert "PR #1264" in verdict.reason

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
        (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute").mkdir(parents=True)
        (tmp_path / "sweeps").mkdir()
        checks = lb.run_preconditions(
            tmp_path, min_free_bytes=1, disk_usage=_fake_usage(10 * 1024**3)
        )
        assert all(c.ok for c in checks), [c for c in checks if not c.ok]
        assert _check(checks, "results_root_writable").detail == str(tmp_path / "sweeps")

    def test_missing_partition_and_low_disk(self, tmp_path: Path) -> None:
        checks = lb.run_preconditions(
            tmp_path, min_free_bytes=10 * 1024**3, disk_usage=_fake_usage(1)
        )
        assert _check(checks, "nifty_1minute_partition").ok is False
        assert _check(checks, "disk_free").ok is False
        # no sweeps dir yet → the writable probe falls back to the lake root
        assert _check(checks, "results_root_writable").detail == str(tmp_path)

    def test_disk_usage_oserror_is_honest_failure(self, tmp_path: Path) -> None:
        def _boom(path: str) -> Any:
            raise OSError("volume wedged")

        checks = lb.run_preconditions(tmp_path, disk_usage=_boom)
        check = _check(checks, "disk_free")
        assert check.ok is False
        assert "disk_usage failed" in check.detail

    def test_active_launch_running_blocks(self, tmp_path: Path) -> None:
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


# ── pid probing + pidfile ────────────────────────────────────────────


class TestProbePid:
    def test_nonpositive_pid_is_exited(self) -> None:
        assert lb.probe_pid(0) == "exited"
        assert lb.probe_pid(-1) == "exited"

    def test_own_live_child_running_then_reaped_exited(self) -> None:
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert lb.probe_pid(proc.pid) == "running"
        finally:
            proc.kill()
            proc.wait()
        # After the wait() reap the pid is gone entirely.
        assert lb.probe_pid(proc.pid) == "exited"

    def test_own_exited_child_zombie_is_reaped_and_exited(self) -> None:
        import subprocess

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
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=4242),
            which=lambda name: "/usr/bin/caffeinate",
            now_utc=lambda: datetime(2026, 7, 8, 16, 0, tzinfo=UTC),
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        assert out.status == "started"
        assert out.active is not None
        assert out.active["pid"] == 4242
        # caffeinate wrap + the locked command + the end=today+1 correction
        argv = record["argv"]
        assert argv[:3] == ["/usr/bin/caffeinate", "-i", "-s"]
        assert "--end" in argv
        assert argv[argv.index("--end") + 1] == "2026-07-09"
        # fresh timestamped results root under <lake>/sweeps
        results_root = Path(out.active["results_root"])
        assert results_root.parent == tmp_path / "sweeps"
        assert results_root.name == "launch-20260708-160000-spot"
        assert results_root.is_dir()
        assert record["log_path"] == results_root / "launch.log"
        # durable pidfile
        state = lb.read_launch_state(tmp_path)
        assert state is not None
        assert state["pid"] == 4242
        assert state["card"] == "spot"

    def test_second_click_while_alive_is_already_running(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": os.getpid(), "card": "spot", "results_root": "x"})
        record: dict[str, Any] = {}
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
        assert out.started is False
        assert out.status == "already_running"
        assert out.active is not None
        assert out.active["pid"] == os.getpid()
        assert "argv" not in record  # NEVER a duplicate launch

    def test_stale_dead_pid_self_heals(self, tmp_path: Path) -> None:
        lb._write_launch_state(tmp_path, {"pid": -7, "card": "futures"})
        record: dict[str, Any] = {}
        out = lb.launch_sweep(
            "spot",
            lake_root=tmp_path,
            spawner=_spawn_recorder(record, pid=555),
            today_ist=lambda: date(2026, 7, 8),
        )
        assert out.started is True
        state = lb.read_launch_state(tmp_path)
        assert state is not None
        assert state["pid"] == 555

    def test_lock_contended_returns_unstarted(self, tmp_path: Path) -> None:
        lock_path = lb.launch_lock_path(tmp_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record: dict[str, Any] = {}
            out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
            assert out.started is False
            assert out.status == "lock_contended"
            assert "argv" not in record
        finally:
            os.close(fd)

    def test_default_clock_and_today_arms(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=_spawn_recorder(record))
        assert out.started is True
        assert "argv" in record

    def test_default_spawner_arm_via_monkeypatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawner=None resolves the module seam — tests still never launch a sweep."""
        record: dict[str, Any] = {}
        monkeypatch.setattr(lb, "_default_spawner", _spawn_recorder(record, pid=777))
        out = lb.launch_sweep("spot", lake_root=tmp_path, spawner=None)
        assert out.started is True
        assert record["argv"][-1] == "--acknowledge-sla-overrun"


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

    def test_child_env_carries_locked_workers(self, tmp_path: Path) -> None:
        log_path = tmp_path / "env.log"
        pid = lb._default_spawner(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['BRUTEX_SWEEP_WORKERS'], flush=True)",
            ],
            log_path,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if lb.probe_pid(pid) == "exited":
                break
            time.sleep(0.05)
        assert log_path.read_text(encoding="utf-8").strip() == lb.LOCKED_SWEEP_WORKERS


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


class TestLaunchStatus:
    def test_no_lake(self) -> None:
        assert lb.launch_status(None) == {"present": False, "running": False}

    def test_no_state(self, tmp_path: Path) -> None:
        assert lb.launch_status(tmp_path) == {"present": False, "running": False}

    def test_running_state_with_log_tail(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text("progress line", encoding="utf-8")
        lb._write_launch_state(
            tmp_path,
            {
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
        assert status["log_tail"] == ""


class TestBuildLaunchState:
    def test_full_payload_shape(self, tmp_path: Path) -> None:
        payload = lb.build_launch_state(
            tmp_path,
            which=lambda name: None,
            today_ist=lambda: date(2026, 7, 8),
        )
        assert payload["interim"] is True
        assert payload["lake_root"] == str(tmp_path)
        assert payload["end_day"] == "2026-07-09"
        assert payload["grid_note"] == lb.GRID_HONESTY_NOTE
        assert {c["name"] for c in payload["preconditions"]} >= {
            "lake_configured",
            "disk_free",
            "rust_wheels",
        }
        futures_card = payload["cards"]["futures"]
        spot_card = payload["cards"]["spot"]
        # On main the futures alias is refused → disabled with the PR #1264
        # note; spot stays live. (Post-#1264 the probe flips dynamically —
        # pinned by TestProbeFuturesAlias.test_available_when_classifier_allows.)
        if not payload["futures"]["available"]:
            assert futures_card["enabled"] is False
            assert "PR #1264" in futures_card["reason"]
        assert spot_card["enabled"] is True
        assert "--acknowledge-sla-overrun" in spot_card["command_display"]
        assert payload["active"] == {"present": False, "running": False}

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
        )
        payload = active.as_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["command"] == ["a", "b"]


class TestTemplateLoader:
    def test_template_loads_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lb, "_LAUNCH_TEMPLATE_CACHE", None)
        first = lb._load_launch_template()
        assert "INTERIM" in first
        assert lb._load_launch_template() is first  # cached arm
