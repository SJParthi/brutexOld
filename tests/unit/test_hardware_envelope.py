"""Unit tests for RAM detection + RAM-scaled budget derivation (DEC-RAM-BUDGETS-001).

Every branch of :mod:`brutex.hardware_envelope` — the two detection probes
(``sysconf`` primary, ``sysctl`` macOS fallback — the latter UNVERIFIED on
real macOS, seam-tested here with a mocked ``subprocess``), the once-per-
process cache + reset seam, the input validation, the explicit env override
(fail-loud), the undetected-RAM floor sentinel, the ``workers`` divisor, and
the log-once observability line.

Plus THE regression pin of the DEC-RAM-BUDGETS-001 semantics law, enumerating
every derive-bucket call site: on a simulated 48 GiB CLAUDE.md §8 box each
derived budget is ``>=`` (here: exactly ``==``) its old hand-picked constant,
and on a smaller simulated box each derived budget SHRINKS (the portability
win — deliberately not clamped to the floor).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import TYPE_CHECKING

import brutex.hardware_envelope as he
import pytest
from brutex.hardware_envelope import (
    UNDETECTED_RAM,
    detect_total_ram_bytes,
    memory_budget_bytes,
    reset_hardware_envelope_cache,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable

GIB = 1024**3


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    """Isolate every test from the process-global detection cache."""
    reset_hardware_envelope_cache()
    yield
    reset_hardware_envelope_cache()


def _simulate_ram(monkeypatch: pytest.MonkeyPatch, total_bytes: int) -> None:
    """Force detection to *total_bytes* (0 = the undetected sentinel).

    Resets the process cache AFTER patching — an earlier import (e.g.
    ``brutex.orchestrator.bloom``'s import-time derivation) may already have
    detected the sandbox's real RAM.
    """
    monkeypatch.setattr(he, "_sysconf_total_ram_bytes", lambda: total_bytes)
    monkeypatch.setattr(he, "_sysctl_total_ram_bytes", lambda: UNDETECTED_RAM)
    reset_hardware_envelope_cache()


# ────── detection probes ──────


class TestSysconfProbe:
    def test_real_linux_sysconf_detects(self) -> None:
        # This sandbox is Linux — the primary stdlib path runs for REAL.
        value = detect_total_ram_bytes()
        assert value > 0

    @pytest.mark.parametrize(
        "raiser",
        [ValueError("unknown name"), OSError("boom"), AttributeError("no sysconf")],
        ids=["valueerror", "oserror", "attributeerror"],
    )
    def test_sysconf_failure_maps_to_sentinel(
        self, monkeypatch: pytest.MonkeyPatch, raiser: Exception
    ) -> None:
        def _boom(_name: str) -> int:
            raise raiser

        monkeypatch.setattr("os.sysconf", _boom)
        monkeypatch.setattr(he, "_sysctl_total_ram_bytes", lambda: UNDETECTED_RAM)
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    @pytest.mark.parametrize(("page", "pages"), [(0, 100), (4096, 0), (-1, 100), (4096, -1)])
    def test_non_positive_sysconf_values_map_to_sentinel(
        self, monkeypatch: pytest.MonkeyPatch, page: int, pages: int
    ) -> None:
        values = {"SC_PAGE_SIZE": page, "SC_PHYS_PAGES": pages}
        monkeypatch.setattr("os.sysconf", lambda name: values[name])
        monkeypatch.setattr(he, "_sysctl_total_ram_bytes", lambda: UNDETECTED_RAM)
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    def test_sysconf_product_is_page_size_times_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 1000}
        monkeypatch.setattr("os.sysconf", lambda name: values[name])
        assert detect_total_ram_bytes() == 4096 * 1000


class TestSysctlFallback:
    """The macOS ``sysctl hw.memsize`` seam — mocked ``subprocess`` only.

    HONEST (Golden Rule #10): real macOS behavior is UNVERIFIED in this
    Linux sandbox; these tests pin the wrapper's failure mapping, not the
    platform.
    """

    @staticmethod
    def _no_sysconf(monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_name: str) -> int:
            raise ValueError("no sysconf name")

        monkeypatch.setattr("os.sysconf", _boom)

    @staticmethod
    def _fake_run(
        stdout: str, returncode: int = 0
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["sysctl"], returncode=returncode, stdout=stdout, stderr=""
            )

        return _run

    def test_sysctl_success_detects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_sysconf(monkeypatch)
        monkeypatch.setattr(he.subprocess, "run", self._fake_run(f"{48 * GIB}\n"))
        assert detect_total_ram_bytes() == 48 * GIB

    def test_sysctl_binary_missing_maps_to_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_sysconf(monkeypatch)

        def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("/usr/sbin/sysctl")

        monkeypatch.setattr(he.subprocess, "run", _raise)
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    def test_sysctl_timeout_maps_to_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_sysconf(monkeypatch)

        def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="sysctl", timeout=5.0)

        monkeypatch.setattr(he.subprocess, "run", _raise)
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    def test_sysctl_nonzero_exit_maps_to_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_sysconf(monkeypatch)
        monkeypatch.setattr(he.subprocess, "run", self._fake_run("", returncode=1))
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    def test_sysctl_garbage_output_maps_to_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_sysconf(monkeypatch)
        monkeypatch.setattr(he.subprocess, "run", self._fake_run("hw.memsize: lots\n"))
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    @pytest.mark.parametrize("stdout", ["0", "-5"])
    def test_sysctl_non_positive_output_maps_to_sentinel(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        self._no_sysconf(monkeypatch)
        monkeypatch.setattr(he.subprocess, "run", self._fake_run(stdout))
        assert detect_total_ram_bytes() == UNDETECTED_RAM


class TestDetectionCache:
    def test_detected_once_then_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _probe() -> int:
            calls["n"] += 1
            return 48 * GIB

        monkeypatch.setattr(he, "_sysconf_total_ram_bytes", _probe)
        assert detect_total_ram_bytes() == 48 * GIB
        # A changed probe result is IGNORED — no mid-run flapping.
        monkeypatch.setattr(he, "_sysconf_total_ram_bytes", lambda: 8 * GIB)
        assert detect_total_ram_bytes() == 48 * GIB
        assert calls["n"] == 1

    def test_undetected_sentinel_is_cached_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert detect_total_ram_bytes() == UNDETECTED_RAM
        monkeypatch.setattr(he, "_sysconf_total_ram_bytes", lambda: 48 * GIB)
        assert detect_total_ram_bytes() == UNDETECTED_RAM

    def test_reset_seam_re_detects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * GIB)
        assert detect_total_ram_bytes() == 48 * GIB
        _simulate_ram(monkeypatch, 8 * GIB)
        reset_hardware_envelope_cache()
        assert detect_total_ram_bytes() == 8 * GIB

    def test_first_detection_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * GIB)
        with capture_logs() as logs:
            detect_total_ram_bytes()
            detect_total_ram_bytes()
        events = [log for log in logs if log["event"] == "hardware_envelope.ram_detected"]
        assert len(events) == 1
        assert events[0]["total_ram_bytes"] == 48 * GIB
        assert events[0]["detected"] is True


# ────── memory_budget_bytes ──────


class TestMemoryBudgetValidation:
    @pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5, float("nan"), float("inf")])
    def test_bad_fraction_raises(self, fraction: float) -> None:
        with pytest.raises(ValueError, match="fraction must be a finite value"):
            memory_budget_bytes(fraction=fraction, floor_bytes=1)

    @pytest.mark.parametrize("floor", [0, -8])
    def test_bad_floor_raises(self, floor: int) -> None:
        with pytest.raises(ValueError, match="floor_bytes must be > 0"):
            memory_budget_bytes(fraction=0.5, floor_bytes=floor)

    @pytest.mark.parametrize("workers", [0, -1])
    def test_bad_workers_raises(self, workers: int) -> None:
        with pytest.raises(ValueError, match="workers must be >= 1"):
            memory_budget_bytes(fraction=0.5, floor_bytes=1, workers=workers)


class TestMemoryBudgetEnvOverride:
    def test_explicit_env_override_wins_over_detection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _simulate_ram(monkeypatch, 48 * GIB)
        got = memory_budget_bytes(
            fraction=0.5, floor_bytes=GIB, env={"X_BUDGET": " 12345 "}, env_var="X_BUDGET"
        )
        assert got == 12345

    def test_env_override_wins_even_when_ram_undetected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        got = memory_budget_bytes(
            fraction=0.5, floor_bytes=GIB, env={"X_BUDGET": "777"}, env_var="X_BUDGET"
        )
        assert got == 777

    def test_garbage_env_value_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="X_BUDGET='16GiB' is not an integer byte count"):
            memory_budget_bytes(
                fraction=0.5, floor_bytes=GIB, env={"X_BUDGET": "16GiB"}, env_var="X_BUDGET"
            )

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_env_value_fails_loud(self, raw: str) -> None:
        with pytest.raises(ValueError, match="must be > 0 bytes"):
            memory_budget_bytes(
                fraction=0.5, floor_bytes=GIB, env={"X_BUDGET": raw}, env_var="X_BUDGET"
            )

    def test_blank_env_value_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        got = memory_budget_bytes(
            fraction=0.5, floor_bytes=GIB, env={"X_BUDGET": "   "}, env_var="X_BUDGET"
        )
        assert got == GIB

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * GIB)
        monkeypatch.setenv("X_BUDGET", "4096")
        assert memory_budget_bytes(fraction=0.5, floor_bytes=GIB, env_var="X_BUDGET") == 4096


class TestMemoryBudgetDerivation:
    def test_undetected_ram_returns_floor_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # THE sentinel law: unknown RAM = today's constant, bit-for-bit —
        # NEVER "zero free bytes".
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB) == 8 * GIB

    def test_detected_ram_scales_by_fraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 96 * GIB)
        assert memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB) == 16 * GIB

    def test_workers_divides_the_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * GIB)
        assert memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB, workers=4) == 2 * GIB

    def test_small_ram_derives_below_floor_no_clamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The portability win: a small machine legitimately goes BELOW the
        # old constant — deliberately NOT clamped to the floor.
        _simulate_ram(monkeypatch, 12 * GIB)
        assert memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB) == 2 * GIB

    def test_derived_budget_never_below_one_byte(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 2)
        assert memory_budget_bytes(fraction=0.1, floor_bytes=GIB) == 1

    def test_derivation_logs_once_per_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 96 * GIB)
        with capture_logs() as logs:
            memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB, env_var="X_BUDGET")
            memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB, env_var="X_BUDGET")
        events = [log for log in logs if log["event"] == "hardware_envelope.budget_derived"]
        assert len(events) == 1
        assert events[0]["budget"] == "X_BUDGET"
        assert events[0]["derived_bytes"] == 16 * GIB
        assert events[0]["floor_bytes"] == 8 * GIB

    def test_derivation_without_env_var_logs_with_synthetic_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _simulate_ram(monkeypatch, 96 * GIB)
        with capture_logs() as logs:
            memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB)
        events = [log for log in logs if log["event"] == "hardware_envelope.budget_derived"]
        assert len(events) == 1
        assert "fraction=" in str(events[0]["budget"])

    def test_exact_floor_parity_does_not_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 48 GiB × 1/6 == the 8 GiB floor exactly — nothing to announce.
        _simulate_ram(monkeypatch, 48 * GIB)
        with capture_logs() as logs:
            assert memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * GIB) == 8 * GIB
        assert not [log for log in logs if log["event"] == "hardware_envelope.budget_derived"]


# ────── THE 48 GiB regression pin (semantics law, all derive call sites) ──────


class TestFortyEightGibRegressionPin:
    """On the simulated CLAUDE.md §8 48 GiB box, every derived budget must be
    ``>=`` its old hand-picked constant (both shipped fractions give exact
    parity by construction). Enumerates EVERY derive-bucket call site of
    DEC-RAM-BUDGETS-001 via the call sites' own constants."""

    def test_all_derived_call_sites_at_48_gib(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine.sweep_mem_budget import (
            DEFAULT_MEM_BUDGET_BYTES,
            MEM_BUDGET_FRACTION,
        )
        from brutex.orchestrator.bloom import (
            MAX_BLOOM_BYTES_FLOOR,
            MAX_BLOOM_BYTES_FRACTION,
        )

        _simulate_ram(monkeypatch, 48 * GIB)
        call_sites = [
            ("sweep_mem_budget.DEFAULT_MEM_BUDGET_BYTES", MEM_BUDGET_FRACTION, 8 * GIB),
            ("bloom.MAX_BLOOM_BYTES", MAX_BLOOM_BYTES_FRACTION, 16 * GIB),
        ]
        assert DEFAULT_MEM_BUDGET_BYTES == 8 * GIB
        assert MAX_BLOOM_BYTES_FLOOR == 16 * GIB
        for name, fraction, old_constant in call_sites:
            derived = memory_budget_bytes(fraction=fraction, floor_bytes=old_constant)
            assert derived >= old_constant, name
            # Both shipped fractions are chosen for EXACT parity at 48 GiB.
            assert derived == old_constant, name

    def test_all_derived_call_sites_shrink_on_a_small_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_mem_budget import (
            DEFAULT_MEM_BUDGET_BYTES,
            MEM_BUDGET_FRACTION,
        )
        from brutex.orchestrator.bloom import (
            MAX_BLOOM_BYTES_FLOOR,
            MAX_BLOOM_BYTES_FRACTION,
        )

        _simulate_ram(monkeypatch, 8 * GIB)
        for fraction, old_constant in [
            (MEM_BUDGET_FRACTION, DEFAULT_MEM_BUDGET_BYTES),
            (MAX_BLOOM_BYTES_FRACTION, MAX_BLOOM_BYTES_FLOOR),
        ]:
            derived = memory_budget_bytes(fraction=fraction, floor_bytes=old_constant)
            assert derived < old_constant

    def test_all_derived_call_sites_floor_when_undetected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_mem_budget import (
            DEFAULT_MEM_BUDGET_BYTES,
            MEM_BUDGET_FRACTION,
        )
        from brutex.orchestrator.bloom import (
            MAX_BLOOM_BYTES_FLOOR,
            MAX_BLOOM_BYTES_FRACTION,
        )

        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        for fraction, old_constant in [
            (MEM_BUDGET_FRACTION, DEFAULT_MEM_BUDGET_BYTES),
            (MAX_BLOOM_BYTES_FRACTION, MAX_BLOOM_BYTES_FLOOR),
        ]:
            assert memory_budget_bytes(fraction=fraction, floor_bytes=old_constant) == old_constant
