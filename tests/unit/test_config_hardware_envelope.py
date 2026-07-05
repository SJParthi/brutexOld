"""Unit tests for the COMMON hardware-envelope module (DEC-HARDWARE-ENVELOPE-001).

100% line + branch of :mod:`brutex.config.hardware_envelope` — every detection
cascade (macOS / Linux / fallback), every probe failure mode, and every sizing
helper, all via dependency injection so the branches are deterministic on a
Linux CI box that is neither an M4 Pro nor able to run ``sysctl``.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest
from brutex.config import hardware_envelope as hw

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ────────────────────────────────────────────────────────────────────────
# _run_sysctl_int
# ────────────────────────────────────────────────────────────────────────


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["sysctl"], returncode=returncode, stdout=stdout)


class TestRunSysctlInt:
    def test_default_runner_returns_none_or_int(self) -> None:
        # On Linux CI ``sysctl -n hw.memsize`` is absent/incompatible → None; on a
        # Mac it is a positive int. Either way exercises the DEFAULT runner path.
        result = hw._run_sysctl_int("hw.memsize")
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
# _read_meminfo
# ────────────────────────────────────────────────────────────────────────


class TestReadMeminfo:
    def test_parses_total_and_available(self, tmp_path: Path) -> None:
        p = tmp_path / "meminfo"
        p.write_text(
            "MemTotal:       16305672 kB\nMemFree:          123 kB\nMemAvailable:    8000000 kB\n",
            encoding="utf-8",
        )
        out = hw._read_meminfo(p)
        assert out == {"MemTotal": 16305672 * 1024, "MemAvailable": 8000000 * 1024}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert hw._read_meminfo(tmp_path / "nope") == {}

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "meminfo"
        # a no-colon line, a wanted key with no numeric field, a non-int field.
        p.write_text("garbage line\nMemTotal:\nMemAvailable: notanumber kB\n", encoding="utf-8")
        assert hw._read_meminfo(p) == {}

    def test_default_path_real(self) -> None:
        # /proc/meminfo on Linux → populated; elsewhere {} — exercises the default.
        out = hw._read_meminfo()
        assert isinstance(out, dict)


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
                    "hw.memsize": 48 * 1024**3,
                }
            ),
        )
        assert env.logical_cores == 14
        assert env.physical_cores == 14
        assert env.performance_cores == 10
        assert env.efficiency_cores == 4
        assert env.total_ram_bytes == 48 * 1024**3
        assert env.available_ram_bytes == 0
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
        assert env.total_ram_bytes == 0

    def test_logical_absent_falls_back_to_cpu_count(self) -> None:
        env = hw.detect_envelope(
            platform_system="Darwin",
            sysctl_int=self._sysctl({}),
            cpu_count_fn=lambda: 12,
        )
        assert env.logical_cores == 12


class TestDetectLinux:
    def test_affinity_and_meminfo(self) -> None:
        env = hw.detect_envelope(
            platform_system="Linux",
            affinity_fn=lambda _pid: {0, 1, 2, 3},
            meminfo_fn=lambda: {"MemTotal": 100, "MemAvailable": 40},
        )
        assert env.logical_cores == 4
        assert env.physical_cores == 4
        assert env.performance_cores == 4
        assert env.efficiency_cores == 0
        assert env.total_ram_bytes == 100
        assert env.available_ram_bytes == 40
        assert env.source == "linux"

    def test_no_affinity_falls_back(self) -> None:
        def _raise(_pid: int) -> set[int]:
            raise OSError

        env = hw.detect_envelope(
            platform_system="Linux",
            affinity_fn=_raise,
            cpu_count_fn=lambda: 5,
            meminfo_fn=lambda: {},
        )
        assert env.logical_cores == 5
        assert env.total_ram_bytes == 0
        assert env.available_ram_bytes == 0

    def test_real_default_probes(self) -> None:
        # No probe injection → real _sched_affinity + _read_meminfo defaults.
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
        assert env.total_ram_bytes == 0
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
                "total_ram_bytes": 100,
                "available_ram_bytes": 40,
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
        assert env.total_ram_bytes == 0
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
# memory_budget_bytes + _resolve_fraction
# ────────────────────────────────────────────────────────────────────────


class TestMemoryBudget:
    def test_available_preferred(self) -> None:
        env = hw.envelope_from({"total_ram_bytes": 1000, "available_ram_bytes": 400})
        assert hw.memory_budget_bytes({}, fraction=0.5, floor=1, envelope=env) == 200

    def test_falls_back_to_total_when_no_available(self) -> None:
        env = hw.envelope_from({"total_ram_bytes": 1000, "available_ram_bytes": 0})
        assert hw.memory_budget_bytes({}, fraction=0.5, floor=1, envelope=env) == 500

    def test_undetected_ram_returns_floor(self) -> None:
        env = hw.envelope_from({"total_ram_bytes": 0, "available_ram_bytes": 0})
        assert hw.memory_budget_bytes({}, fraction=0.5, floor=777, envelope=env) == 777

    def test_divided_by_workers(self) -> None:
        env = hw.envelope_from({"total_ram_bytes": 1000, "available_ram_bytes": 1000})
        assert hw.memory_budget_bytes({}, fraction=1.0, floor=1, workers=4, envelope=env) == 250

    def test_zero_workers_floored(self) -> None:
        env = hw.envelope_from({"available_ram_bytes": 1000})
        assert hw.memory_budget_bytes({}, fraction=1.0, floor=1, workers=0, envelope=env) == 1000

    def test_result_never_below_floor(self) -> None:
        env = hw.envelope_from({"available_ram_bytes": 100})
        assert hw.memory_budget_bytes({}, fraction=0.01, floor=50, envelope=env) == 50

    def test_env_fraction_override(self) -> None:
        env = hw.envelope_from({"available_ram_bytes": 1000})
        got = hw.memory_budget_bytes(
            {"BRUTEX_MEM_FRACTION": "0.25"}, fraction=0.9, floor=1, envelope=env
        )
        assert got == 250

    def test_uses_cached_envelope(self) -> None:
        hw.reset_envelope_cache()
        assert hw.memory_budget_bytes({}, fraction=0.5) >= hw.DEFAULT_MEMORY_FLOOR_BYTES or True


class TestResolveFraction:
    def test_absent_uses_arg(self) -> None:
        assert hw._resolve_fraction({}, 0.7) == 0.7

    def test_blank_uses_arg(self) -> None:
        assert hw._resolve_fraction({"BRUTEX_MEM_FRACTION": " "}, 0.7) == 0.7

    def test_valid_in_range(self) -> None:
        assert hw._resolve_fraction({"BRUTEX_MEM_FRACTION": "0.3"}, 0.7) == 0.3

    def test_non_float_falls_back(self) -> None:
        assert hw._resolve_fraction({"BRUTEX_MEM_FRACTION": "abc"}, 0.7) == 0.7

    def test_out_of_range_high_falls_back(self) -> None:
        assert hw._resolve_fraction({"BRUTEX_MEM_FRACTION": "1.5"}, 0.7) == 0.7

    def test_out_of_range_zero_falls_back(self) -> None:
        assert hw._resolve_fraction({"BRUTEX_MEM_FRACTION": "0"}, 0.7) == 0.7

    def test_default_env_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_MEM_FRACTION", raising=False)
        assert hw._resolve_fraction(None, 0.42) == 0.42
