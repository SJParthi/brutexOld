"""Unit tests for the COMMON hardware-envelope module.

100% line + branch of :mod:`brutex.config.hardware_envelope` (together with
the sibling ``tests/unit/test_hardware_envelope.py``, which pins the ported
DEC-RAM-BUDGETS-001 RAM surface verbatim) — every detection cascade
(macOS / Linux / fallback), every probe failure mode, every CPU sizing
helper, and the polars/DuckDB thread bounds (DEC-THREAD-BUDGET-BOUNDS-001),
all via dependency injection so the branches are deterministic on a Linux CI
box that is neither an M4 Pro nor able to run ``sysctl``.

Since DEC-HW-ENVELOPE-CONSOLIDATE-001 this module IS the one hardware-envelope
module — the consolidation pin below supersedes the interim two-module
RAM-delegation pin of the #1167 conflict resolution.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest
from brutex.config import hardware_envelope as hw

if TYPE_CHECKING:
    from collections.abc import Callable


# ────────────────────────────────────────────────────────────────────────
# Consolidation pin — ONE hardware-envelope module, the #1360 RAM law hosted
# verbatim (DEC-HW-ENVELOPE-CONSOLIDATE-001; supersedes the interim
# two-module RAM-delegation pin of the #1167 conflict resolution)
# ────────────────────────────────────────────────────────────────────────


class TestConsolidationPin:
    def test_config_module_hosts_the_authoritative_ram_surface(self) -> None:
        """The #1360 RAM API lives HERE now (one source of truth).

        The contradictory ENVELOPE-mode ``memory_budget_bytes`` duplicate
        (available-RAM-preferring, floor-clamped, silent-ignore fraction
        override) stays DEAD — its surface must not reappear — while the
        authoritative DEC-RAM-BUDGETS-001 derivation (total-RAM,
        floor-only-on-undetected, fail-LOUD override) is hosted verbatim
        (pinned in tests/unit/test_hardware_envelope.py).
        """
        assert callable(hw.memory_budget_bytes)
        assert callable(hw.detect_total_ram_bytes)
        assert callable(hw.reset_hardware_envelope_cache)
        assert hw.UNDETECTED_RAM == 0
        for dead in (
            "_resolve_fraction",
            "MEM_FRACTION_ENV",
            "DEFAULT_MEMORY_FLOOR_BYTES",
            "_read_meminfo",
        ):
            assert not hasattr(hw, dead), f"dead envelope-mode RAM surface reappeared: {dead}"

    def test_old_sibling_module_is_gone(self) -> None:
        # Filesystem pin (not find_spec — an editable-install meta-path finder
        # can resolve against a SIBLING checkout): the package directory of
        # the imported module must not carry the old sibling file.
        from pathlib import Path

        package_dir = Path(hw.__file__).resolve().parent.parent
        assert not (package_dir / "hardware_envelope.py").exists(), (
            "brutex/hardware_envelope.py must stay deleted — "
            "brutex.config.hardware_envelope is the ONE module "
            "(DEC-HW-ENVELOPE-CONSOLIDATE-001)"
        )

    def test_envelope_dataclass_stays_cpu_only(self) -> None:
        env = hw.envelope_from({"logical_cores": 2})
        assert not hasattr(env, "total_ram_bytes")
        assert not hasattr(env, "available_ram_bytes")


# ────────────────────────────────────────────────────────────────────────
# _run_sysctl_int
# ────────────────────────────────────────────────────────────────────────


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["sysctl"], returncode=returncode, stdout=stdout)


class TestRunSysctlInt:
    def test_default_runner_returns_none_or_int(self) -> None:
        # On Linux CI ``sysctl -n hw.logicalcpu`` is absent/incompatible → None;
        # on a Mac it is a positive int. Either way exercises the DEFAULT runner path.
        result = hw._run_sysctl_int("hw.logicalcpu")
        assert result is None or (isinstance(result, int) and result > 0)

    def test_parses_positive_int(self) -> None:
        assert hw._run_sysctl_int("k", runner=lambda *a, **k: _proc("10\n")) == 10

    def test_zero_is_none(self) -> None:
        assert hw._run_sysctl_int("k", runner=lambda *a, **k: _proc("0")) is None

    def test_negative_is_none(self) -> None:
        assert hw._run_sysctl_int("k", runner=lambda *a, **k: _proc("-4")) is None

    def test_non_integer_is_none(self) -> None:
        assert hw._run_sysctl_int("k", runner=lambda *a, **k: _proc("nope")) is None

    def test_nonzero_returncode_is_none(self) -> None:
        assert hw._run_sysctl_int("k", runner=lambda *a, **k: _proc("10", returncode=1)) is None

    def test_oserror_is_none(self) -> None:
        def _raise(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise OSError("no sysctl")

        assert hw._run_sysctl_int("k", runner=_raise) is None

    def test_subprocess_error_is_none(self) -> None:
        def _raise(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="sysctl", timeout=2.0)

        assert hw._run_sysctl_int("k", runner=_raise) is None


# ────────────────────────────────────────────────────────────────────────
# _sched_affinity
# ────────────────────────────────────────────────────────────────────────


class TestSchedAffinity:
    def test_injected_returns_set(self) -> None:
        assert hw._sched_affinity(lambda _pid: {0, 1, 2}) == {0, 1, 2}

    def test_injected_oserror_returns_none(self) -> None:
        def _raise(_pid: int) -> set[int]:
            raise OSError

        assert hw._sched_affinity(_raise) is None

    def test_missing_sched_getaffinity_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
        assert hw._sched_affinity() is None

    def test_default_probe_when_available(self) -> None:
        # On Linux CI ``os.sched_getaffinity`` exists → a non-empty set; elsewhere
        # None. Either exercises the DEFAULT (no-injection) branch.
        result = hw._sched_affinity()
        assert result is None or (isinstance(result, set) and len(result) >= 1)


# ────────────────────────────────────────────────────────────────────────
# detect_envelope — macOS / Linux / fallback cascades
# ────────────────────────────────────────────────────────────────────────


class TestDetectMacos:
    def _sysctl(self, values: dict[str, int | None]) -> Callable[[str], int | None]:
        def _probe(key: str) -> int | None:
            return values.get(key)

        return _probe

    def test_full_pe_split(self) -> None:
        env = hw.detect_envelope(
            platform_system="Darwin",
            sysctl_int=self._sysctl(
                {
                    "hw.logicalcpu": 14,
                    "hw.physicalcpu": 14,
                    "hw.perflevel0.logicalcpu": 10,
                    "hw.perflevel1.logicalcpu": 4,
                }
            ),
        )
        assert env.logical_cores == 14
        assert env.physical_cores == 14
        assert env.performance_cores == 10
        assert env.efficiency_cores == 4
        assert env.source == "macos_sysctl"

    def test_perf_present_eff_missing(self) -> None:
        env = hw.detect_envelope(
            platform_system="Darwin",
            sysctl_int=self._sysctl(
                {
                    "hw.logicalcpu": 8,
                    "hw.perflevel0.logicalcpu": 8,
                    "hw.perflevel1.logicalcpu": None,
                }
            ),
        )
        assert env.performance_cores == 8
        assert env.efficiency_cores == 0

    def test_no_pe_split(self) -> None:
        # perflevel0 absent → treat all logical as performance.
        env = hw.detect_envelope(
            platform_system="Darwin",
            sysctl_int=self._sysctl({"hw.logicalcpu": 6}),
        )
        assert env.performance_cores == 6
        assert env.efficiency_cores == 0
        assert env.physical_cores == 6  # physical absent → logical

    def test_logical_absent_falls_back_to_cpu_count(self) -> None:
        env = hw.detect_envelope(
            platform_system="Darwin",
            sysctl_int=self._sysctl({}),
            cpu_count_fn=lambda: 12,
        )
        assert env.logical_cores == 12


class TestDetectLinux:
    def test_affinity_sizes_logical(self) -> None:
        env = hw.detect_envelope(
            platform_system="Linux",
            affinity_fn=lambda _pid: {0, 1, 2, 3},
        )
        assert env.logical_cores == 4
        assert env.physical_cores == 4
        assert env.performance_cores == 4
        assert env.efficiency_cores == 0
        assert env.source == "linux"

    def test_no_affinity_falls_back(self) -> None:
        def _raise(_pid: int) -> set[int]:
            raise OSError

        env = hw.detect_envelope(
            platform_system="Linux",
            affinity_fn=_raise,
            cpu_count_fn=lambda: 5,
        )
        assert env.logical_cores == 5

    def test_real_default_probes(self) -> None:
        # No probe injection → real _sched_affinity default.
        env = hw.detect_envelope(platform_system="Linux")
        assert env.logical_cores >= 1
        assert env.source == "linux"


class TestDetectFallback:
    def test_windows_like_fallback(self) -> None:
        env = hw.detect_envelope(platform_system="Windows", cpu_count_fn=lambda: 4)
        assert env.logical_cores == 4
        assert env.physical_cores == 4
        assert env.performance_cores == 4
        assert env.efficiency_cores == 0
        assert env.source == "fallback"

    def test_cpu_count_none_floors_to_one(self) -> None:
        env = hw.detect_envelope(platform_system="Windows", cpu_count_fn=lambda: None)
        assert env.logical_cores == 1

    def test_cpu_count_zero_floors_to_one(self) -> None:
        env = hw.detect_envelope(platform_system="Windows", cpu_count_fn=lambda: 0)
        assert env.logical_cores == 1

    def test_real_platform_default(self) -> None:
        # No platform override → real platform.system() (the default branch).
        env = hw.detect_envelope()
        assert env.logical_cores >= 1


# ────────────────────────────────────────────────────────────────────────
# envelope_from + cache
# ────────────────────────────────────────────────────────────────────────


class TestEnvelopeFrom:
    def test_full_overrides(self) -> None:
        env = hw.envelope_from(
            {
                "logical_cores": 14,
                "physical_cores": 14,
                "performance_cores": 10,
                "efficiency_cores": 4,
                "source": "test",
            }
        )
        assert env.performance_cores == 10
        assert env.source == "test"

    def test_defaults_fill_missing(self) -> None:
        env = hw.envelope_from({"logical_cores": 8})
        assert env.physical_cores == 8
        assert env.performance_cores == 8
        assert env.efficiency_cores == 0
        assert env.source == "override"


class TestCache:
    def test_cache_miss_then_hit(self) -> None:
        hw.reset_envelope_cache()
        first = hw.cached_envelope()
        second = hw.cached_envelope()
        assert first is second  # second call is the cached hit

    def test_reset_forces_recompute(self) -> None:
        first = hw.cached_envelope()
        hw.reset_envelope_cache()
        second = hw.cached_envelope()
        assert first is not second
        assert first == second  # same machine → equal value


# ────────────────────────────────────────────────────────────────────────
# _env_int
# ────────────────────────────────────────────────────────────────────────


class TestEnvInt:
    def test_absent(self) -> None:
        assert hw._env_int({}, "K") is None

    def test_blank(self) -> None:
        assert hw._env_int({"K": "  "}, "K") is None

    def test_valid(self) -> None:
        assert hw._env_int({"K": "6"}, "K") == 6

    def test_non_integer_logs_and_none(self) -> None:
        assert hw._env_int({"K": "x"}, "K") is None

    def test_below_one_logs_and_none(self) -> None:
        assert hw._env_int({"K": "0"}, "K") is None

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("K_UNSET_XYZ", raising=False)
        assert hw._env_int(None, "K_UNSET_XYZ") is None


# ────────────────────────────────────────────────────────────────────────
# cpu_pool_workers
# ────────────────────────────────────────────────────────────────────────


class TestCpuPoolWorkers:
    def test_m4pro_prefers_performance(self) -> None:
        env = hw.envelope_from({"logical_cores": 14, "performance_cores": 10})
        assert hw.cpu_pool_workers({}, reserve=2, envelope=env) == 8

    def test_logical_when_not_prefer_performance(self) -> None:
        env = hw.envelope_from({"logical_cores": 14, "performance_cores": 10})
        assert hw.cpu_pool_workers({}, reserve=2, prefer_performance=False, envelope=env) == 12

    def test_cap_applies(self) -> None:
        env = hw.envelope_from({"logical_cores": 32, "performance_cores": 32})
        assert hw.cpu_pool_workers({}, reserve=2, cap=8, envelope=env) == 8

    def test_floor_at_one(self) -> None:
        env = hw.envelope_from({"logical_cores": 2, "performance_cores": 2})
        assert hw.cpu_pool_workers({}, reserve=2, envelope=env) == 1

    def test_env_override_wins(self) -> None:
        env = hw.envelope_from({"logical_cores": 14, "performance_cores": 10})
        assert hw.cpu_pool_workers({"BRUTEX_CPU_WORKERS": "3"}, envelope=env) == 3

    def test_uses_cached_envelope_when_none(self) -> None:
        hw.reset_envelope_cache()
        result = hw.cpu_pool_workers({})
        assert result >= 1


# ────────────────────────────────────────────────────────────────────────
# library_thread_budget
# ────────────────────────────────────────────────────────────────────────


class TestLibraryThreadBudget:
    def test_divides_physical(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.library_thread_budget({}, 4, envelope=env) == 3

    def test_floor_at_one(self) -> None:
        env = hw.envelope_from({"physical_cores": 8})
        assert hw.library_thread_budget({}, 100, envelope=env) == 1

    def test_zero_workers_floored(self) -> None:
        env = hw.envelope_from({"physical_cores": 8})
        assert hw.library_thread_budget({}, 0, envelope=env) == 8

    def test_uses_cached_envelope(self) -> None:
        hw.reset_envelope_cache()
        assert hw.library_thread_budget({}, 2) >= 1


# ────────────────────────────────────────────────────────────────────────
# polars / DuckDB thread bounds (DEC-THREAD-BUDGET-BOUNDS-001)
# ────────────────────────────────────────────────────────────────────────


class TestPoolDivisor:
    def test_none_uses_default(self) -> None:
        assert hw._pool_divisor(None) == hw.DEFAULT_EXPECTED_LIBRARY_POOLS

    def test_below_one_uses_default(self) -> None:
        assert hw._pool_divisor(0) == hw.DEFAULT_EXPECTED_LIBRARY_POOLS

    def test_explicit_wins(self) -> None:
        assert hw._pool_divisor(4) == 4


class TestPolarsThreadBudget:
    def test_derived_splits_physical_across_pools(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        # 14 physical // 2 competing library pools (polars + DuckDB) = 7.
        assert hw.polars_thread_budget({}, envelope=env) == 7

    def test_explicit_expected_workers(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.polars_thread_budget({}, expected_workers=7, envelope=env) == 2

    def test_kill_switch_env_wins(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.polars_thread_budget({"BRUTEX_POLARS_THREADS": "5"}, envelope=env) == 5

    def test_invalid_kill_switch_ignored(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.polars_thread_budget({"BRUTEX_POLARS_THREADS": "zero"}, envelope=env) == 7

    def test_floors_at_one(self) -> None:
        env = hw.envelope_from({"physical_cores": 1})
        assert hw.polars_thread_budget({}, envelope=env) == 1


class TestApplyPolarsThreadBound:
    def test_sets_env_var_before_polars_import(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        environ: dict[str, str] = {}
        got = hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
        assert got == 7
        assert environ["POLARS_MAX_THREADS"] == "7"

    def test_operator_preset_respected_never_overwritten(self) -> None:
        environ = {"POLARS_MAX_THREADS": "3"}
        got = hw.apply_polars_thread_bound(environ, loaded_modules={})
        assert got is None
        assert environ["POLARS_MAX_THREADS"] == "3"

    def test_blank_preset_is_unset(self) -> None:
        env = hw.envelope_from({"physical_cores": 4})
        environ = {"POLARS_MAX_THREADS": "  "}
        got = hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
        assert got == 2
        assert environ["POLARS_MAX_THREADS"] == "2"

    def test_polars_already_imported_is_honest_noop(self) -> None:
        # Setting the env var after polars imported would be silently ignored
        # by polars — the honest behaviour is NOT to set it (loud warning).
        env = hw.envelope_from({"physical_cores": 14})
        environ: dict[str, str] = {}
        got = hw.apply_polars_thread_bound(
            environ, envelope=env, loaded_modules={"polars": object()}
        )
        assert got is None
        assert "POLARS_MAX_THREADS" not in environ

    def test_kill_switch_env_flows_through(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        environ = {"BRUTEX_POLARS_THREADS": "4"}
        got = hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
        assert got == 4
        assert environ["POLARS_MAX_THREADS"] == "4"

    def test_default_environ_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = hw.envelope_from({"physical_cores": 2})
        monkeypatch.delenv("POLARS_MAX_THREADS", raising=False)
        monkeypatch.delenv("BRUTEX_POLARS_THREADS", raising=False)
        got = hw.apply_polars_thread_bound(envelope=env, loaded_modules={})
        try:
            assert got == 1
            assert os.environ["POLARS_MAX_THREADS"] == "1"
        finally:
            # monkeypatch restores the pre-test absence at teardown too; this
            # keeps the assertion failure path clean.
            os.environ.pop("POLARS_MAX_THREADS", None)

    def test_default_loaded_modules_is_sys_modules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys as _sys

        env = hw.envelope_from({"physical_cores": 2})
        monkeypatch.setitem(_sys.modules, "polars", object())
        environ: dict[str, str] = {}
        assert hw.apply_polars_thread_bound(environ, envelope=env) is None
        assert "POLARS_MAX_THREADS" not in environ


class TestDuckdbThreadBudget:
    def test_derived_splits_physical_across_pools(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.duckdb_thread_budget({}, envelope=env) == 7

    def test_kill_switch_env_wins(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        assert hw.duckdb_thread_budget({"BRUTEX_DUCKDB_THREADS": "2"}, envelope=env) == 2

    def test_explicit_expected_workers(self) -> None:
        env = hw.envelope_from({"physical_cores": 12})
        assert hw.duckdb_thread_budget({}, expected_workers=3, envelope=env) == 4


class _RecordingConn:
    """A duck-typed connection recording every ``execute`` call."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> object:
        self.sql.append(sql)
        return None


class TestApplyDuckdbThreadBound:
    def test_derived_budget_applied(self) -> None:
        env = hw.envelope_from({"physical_cores": 14})
        conn = _RecordingConn()
        got = hw.apply_duckdb_thread_bound(conn, {}, envelope=env)
        assert got == 7
        assert conn.sql == ["SET threads TO 7"]

    def test_explicit_threads_win(self) -> None:
        conn = _RecordingConn()
        got = hw.apply_duckdb_thread_bound(conn, {}, threads=3)
        assert got == 3
        assert conn.sql == ["SET threads TO 3"]

    @pytest.mark.parametrize("threads", [0, -2])
    def test_explicit_below_one_fails_loud(self, threads: int) -> None:
        conn = _RecordingConn()
        with pytest.raises(ValueError, match="threads must be >= 1"):
            hw.apply_duckdb_thread_bound(conn, {}, threads=threads)
        assert conn.sql == []

    def test_real_duckdb_connection_setting_takes_effect(self) -> None:
        duckdb = pytest.importorskip("duckdb")
        env = hw.envelope_from({"physical_cores": 14})
        with duckdb.connect() as conn:
            got = hw.apply_duckdb_thread_bound(conn, {}, envelope=env)
            row = conn.execute("SELECT current_setting('threads')").fetchone()
            assert row is not None
            assert int(row[0]) == got == 7


class TestPolarsBoundLogOnce:
    def test_too_late_warns_once_per_process(self) -> None:
        from structlog.testing import capture_logs

        hw.reset_hardware_envelope_cache()  # clears the log-once registry
        try:
            env = hw.envelope_from({"physical_cores": 4})
            with capture_logs() as logs:
                for _ in range(3):
                    hw.apply_polars_thread_bound(
                        {}, envelope=env, loaded_modules={"polars": object()}
                    )
            events = [log for log in logs if log["event"] == "hardware.polars_bound.too_late"]
            assert len(events) == 1
        finally:
            hw.reset_hardware_envelope_cache()

    def test_applied_and_preset_log_once_each(self) -> None:
        from structlog.testing import capture_logs

        hw.reset_hardware_envelope_cache()
        try:
            env = hw.envelope_from({"physical_cores": 4})
            with capture_logs() as logs:
                environ: dict[str, str] = {}
                hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
                # A second call now hits the preset branch (the var is set).
                hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
                hw.apply_polars_thread_bound(environ, envelope=env, loaded_modules={})
            applied = [log for log in logs if log["event"] == "hardware.polars_bound.applied"]
            preset = [log for log in logs if log["event"] == "hardware.polars_bound.preset"]
            assert len(applied) == 1
            assert len(preset) == 1
        finally:
            hw.reset_hardware_envelope_cache()
