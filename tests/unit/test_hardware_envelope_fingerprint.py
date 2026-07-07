"""Hardware fingerprint + bench-baseline key tests (DEC-BENCH-ABS-BASELINE-001).

Covers the :mod:`brutex.config.hardware_envelope` additions: the CPU-model
probe (macOS ``sysctl -n hw.model`` / Linux ``/proc/cpuinfo``, every failure
→ the honest ``"unknown"``), the KEY-DEFINING :class:`HardwareFingerprint`,
the ONE key-format law shared with the bench-baseline schema, the env
override fail-loud contract, and the env-cannot-move-the-key pin.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from brutex.config import hardware_envelope as hw
from brutex.observability.bench_baseline import KEY_SLUG_RE


@pytest.fixture(autouse=True)
def _reset_caches() -> Any:
    """Isolate the process-global model/RAM caches per test."""
    hw.reset_hardware_envelope_cache()
    yield
    hw.reset_hardware_envelope_cache()


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["sysctl"], returncode=returncode, stdout=stdout, stderr=""
    )


def _sysctl_runner(stdout: str, returncode: int = 0) -> Any:
    def runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _proc(stdout, returncode)

    return runner


def _raising_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise OSError("no sysctl binary")


M4PRO_ENVELOPE = hw.envelope_from(
    {
        "logical_cores": 14,
        "physical_cores": 14,
        "performance_cores": 10,
        "efficiency_cores": 4,
    }
)

GIB = 2**30


class TestRunSysctlStr:
    def test_happy_path(self) -> None:
        assert hw._run_sysctl_str("hw.model", runner=_sysctl_runner("Mac16,8\n")) == "Mac16,8"

    def test_nonzero_exit_is_none(self) -> None:
        assert hw._run_sysctl_str("hw.model", runner=_sysctl_runner("x", returncode=1)) is None

    def test_empty_output_is_none(self) -> None:
        assert hw._run_sysctl_str("hw.model", runner=_sysctl_runner("  \n")) is None

    def test_spawn_error_is_none(self) -> None:
        assert hw._run_sysctl_str("hw.model", runner=_raising_runner) is None


class TestCpuModelString:
    def test_macos_path(self) -> None:
        model = hw.cpu_model_string(
            platform_system="Darwin", sysctl_runner=_sysctl_runner("Mac16,8\n")
        )
        assert model == "Mac16,8"

    def test_macos_probe_failure_is_unknown(self) -> None:
        assert (
            hw.cpu_model_string(platform_system="Darwin", sysctl_runner=_raising_runner)
            == "unknown"
        )

    def test_linux_path(self) -> None:
        cpuinfo = "processor: 0\nmodel name\t: Apple M4 Pro Emulated\nflags: x\n"
        model = hw.cpu_model_string(platform_system="Linux", cpuinfo_reader=lambda: cpuinfo)
        assert model == "Apple M4 Pro Emulated"

    def test_linux_no_model_line_is_unknown(self) -> None:
        assert (
            hw.cpu_model_string(platform_system="Linux", cpuinfo_reader=lambda: "processor: 0\n")
            == "unknown"
        )

    def test_linux_empty_reader_is_unknown(self) -> None:
        assert hw.cpu_model_string(platform_system="Linux", cpuinfo_reader=lambda: "") == "unknown"

    def test_unknown_platform_is_unknown(self) -> None:
        assert hw.cpu_model_string(platform_system="Windows") == "unknown"

    def test_real_detection_is_cached_until_reset(self) -> None:
        first = hw.cpu_model_string()
        assert first  # never empty
        assert hw.cpu_model_string() == first  # memoised
        hw.reset_hardware_envelope_cache()
        assert hw.cpu_model_string() == first  # re-detected, same box

    def test_cache_hit_skips_probing_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The once-per-process pin: a cache HIT returns before ANY probe —
        the ``_CPU_MODEL_CACHE`` store/read is live dataflow, not a dead
        write (CodeQL py/unused-global-variable 672 regression)."""
        first = hw.cpu_model_string()

        def _boom() -> str:
            raise AssertionError("cache miss — cpu_model_string re-probed the platform")

        monkeypatch.setattr(hw.platform, "system", _boom)
        assert hw.cpu_model_string() == first

    def test_injected_probe_bypasses_the_cache(self) -> None:
        real = hw.cpu_model_string()
        injected = hw.cpu_model_string(
            platform_system="Darwin", sysctl_runner=_sysctl_runner("Mac99,9")
        )
        assert injected == "Mac99,9"
        assert hw.cpu_model_string() == real  # cache not poisoned

    def test_default_cpuinfo_reader_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        text = hw._read_proc_cpuinfo()
        assert isinstance(text, str)
        monkeypatch.setattr(hw, "_PROC_CPUINFO", "/nonexistent/cpuinfo")
        assert hw._read_proc_cpuinfo() == ""


class TestHardwareFingerprint:
    def test_reference_m4pro_fingerprint(self) -> None:
        fp = hw.hardware_fingerprint(
            envelope=M4PRO_ENVELOPE,
            total_ram_bytes=48 * GIB,
            machine="arm64",
            os_family="darwin",
            model="Mac16,8",
        )
        assert fp == hw.HardwareFingerprint(
            model_slug="mac16-8",
            arch="arm64",
            os_family="darwin",
            physical_cores=14,
            performance_cores=10,
            efficiency_cores=4,
            total_ram_gib=48,
        )

    def test_ram_sentinel_stays_zero(self) -> None:
        fp = hw.hardware_fingerprint(
            envelope=M4PRO_ENVELOPE,
            total_ram_bytes=hw.UNDETECTED_RAM,
            machine="arm64",
            os_family="darwin",
            model="Mac16,8",
        )
        assert fp.total_ram_gib == 0

    def test_real_detection_produces_sane_fields(self) -> None:
        fp = hw.hardware_fingerprint()
        assert fp.physical_cores >= 1
        assert fp.performance_cores >= 1
        assert KEY_SLUG_RE.fullmatch(fp.model_slug)
        assert KEY_SLUG_RE.fullmatch(fp.arch)


class TestHardwareKey:
    def test_reference_m4pro_key(self) -> None:
        fp = hw.hardware_fingerprint(
            envelope=M4PRO_ENVELOPE,
            total_ram_bytes=48 * GIB,
            machine="arm64",
            os_family="darwin",
            model="Mac16,8",
        )
        assert hw.hardware_key({}, fingerprint=fp) == "mac16-8-14c10p4e-48g-arm64"

    def test_ram_unknown_key_token(self) -> None:
        fp = hw.hardware_fingerprint(
            envelope=M4PRO_ENVELOPE,
            total_ram_bytes=hw.UNDETECTED_RAM,
            machine="arm64",
            os_family="darwin",
            model="Mac16,8",
        )
        assert hw.hardware_key({}, fingerprint=fp) == "mac16-8-14c10p4e-unkg-arm64"

    def test_env_override_valid_wins(self) -> None:
        key = hw.hardware_key({hw.BENCH_BASELINE_KEY_ENV: "testos-x86-2c1p1e-1g-x86-64"})
        assert key == "testos-x86-2c1p1e-1g-x86-64"

    def test_env_override_invalid_raises_fail_loud(self) -> None:
        with pytest.raises(ValueError, match="violates the hardware-key slug law"):
            hw.hardware_key({hw.BENCH_BASELINE_KEY_ENV: "Bad Key!"})

    def test_env_override_blank_falls_through(self) -> None:
        fp = hw.hardware_fingerprint(
            envelope=M4PRO_ENVELOPE,
            total_ram_bytes=48 * GIB,
            machine="arm64",
            os_family="darwin",
            model="Mac16,8",
        )
        assert hw.hardware_key({hw.BENCH_BASELINE_KEY_ENV: "  "}, fingerprint=fp).startswith(
            "mac16-8-"
        )

    def test_real_key_satisfies_slug_law(self) -> None:
        assert KEY_SLUG_RE.fullmatch(hw.hardware_key({}))

    def test_worker_env_overrides_cannot_move_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env-cannot-move-the-key pin: every BRUTEX_* sizing override is
        ignored by the fingerprint (RAW detection only)."""
        before = hw.hardware_key({})
        for var in (
            hw.CPU_WORKERS_ENV,
            hw.POLARS_THREADS_ENV,
            hw.DUCKDB_THREADS_ENV,
            "BRUTEX_PREBUILD_WORKERS",
            "BRUTEX_SWEEP_WORKERS",
        ):
            monkeypatch.setenv(var, "1")
        hw.reset_hardware_envelope_cache()
        assert hw.hardware_key({}) == before

    def test_key_is_deterministic(self) -> None:
        assert hw.hardware_key({}) == hw.hardware_key({})
