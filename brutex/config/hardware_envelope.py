"""COMMON hardware-envelope CPU detection + CPU sizing (DEC-HARDWARE-ENVELOPE-001).

The ONE source of truth (CLAUDE.md §11 COMMON) for **auto-detecting the host's
CPU topology at RUNTIME** and turning that into worker / library-thread
budgets, so every CPU-bound stage uses the operator's full machine (the
CLAUDE.md §8 reference envelope is an Apple M4 Pro — 14 logical cores = 10
Performance + 4 Efficiency) WITHOUT any hardcoded core count.

A **config leaf** — it imports nothing from :mod:`brutex.dashboard` /
:mod:`brutex.engine`, so any stage can size itself off it. **Stdlib-only** —
``psutil`` is deliberately NOT a dependency; detection uses ``sysctl`` (macOS)
and ``os.sched_getaffinity`` (Linux, cgroup/CI-aware), falling back to
``os.cpu_count()``.

## RAM lives in :mod:`brutex.hardware_envelope`, NOT here (Golden Rule #2)

This module is **CPU-only**. RAM detection
(:func:`brutex.hardware_envelope.detect_total_ram_bytes`) and every RAM-scaled
byte budget (:func:`brutex.hardware_envelope.memory_budget_bytes` — env
override wins fail-LOUD, undetected RAM → the call site's floor EXACTLY,
detected → ``int(total × fraction / workers)``) are owned by the sibling
DEC-RAM-BUDGETS-001 module (MIGRATION.md #1360). An earlier draft of this
module duplicated a contradictory ``memory_budget_bytes`` here; that half was
stripped in-branch — RAM questions DELEGATE to
:mod:`brutex.hardware_envelope`, one source of truth.

## Honest envelope (Golden Rule #10)

* Detection is **best-effort + fail-soft**. An undetectable core count degrades
  to ``os.cpu_count()`` (or ``1``) — never a raise, never a guess above the
  machine.
* ``performance_cores`` / ``efficiency_cores`` are only truly split on macOS
  (``hw.perflevel0/1.logicalcpu``); on Linux every logical CPU is reported as a
  performance core (no exposed P/E split) — an honest over-report, never a
  guess of an efficiency tier that isn't measurable.
* Every helper is **pure + injectable** (via ``envelope=`` / ``env=``) so the
  detection branches are 100% unit-testable on a Linux CI box that is neither an
  M4 Pro nor able to run ``sysctl``.

## The vendor FETCH pool is NOT sized here (Golden Rule #3)

This module sizes CPU-bound COMPUTE only. The Groww fetch pool + the per-second
rate ceiling + the 500/min governor are a REAL vendor limit (a single TOTP
token; CLAUDE.md §2 Rule 6) and are deliberately NOT hardware-scaled.
"""

from __future__ import annotations

import os
import platform
import subprocess  # nosec B404 — fixed-argv `sysctl -n` probe only, no shell (see _run_sysctl_int)
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LOG = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Env overrides + constants
# ────────────────────────────────────────────────────────────────────────

#: Env var pinning the CPU-bound pool worker count (a valid ``>= 1`` integer
#: wins over auto-detection). CI / tests set this for determinism.
CPU_WORKERS_ENV: str = "BRUTEX_CPU_WORKERS"

#: Logical CPUs the default pool RESERVES for the rest of the machine — the
#: CLAUDE.md §8 "2 reserved" cores (daily apps run concurrently on the reference
#: M4 Pro). The proven sweep-parallel pattern.
DEFAULT_RESERVE_CPUS: int = 2

#: macOS ``sysctl`` keys.
_SYSCTL_PERF_KEY: str = "hw.perflevel0.logicalcpu"
_SYSCTL_EFF_KEY: str = "hw.perflevel1.logicalcpu"
_SYSCTL_LOGICAL_KEY: str = "hw.logicalcpu"
_SYSCTL_PHYSICAL_KEY: str = "hw.physicalcpu"

_DARWIN: str = "Darwin"
_LINUX: str = "Linux"


@dataclass(frozen=True, slots=True)
class HardwareEnvelope:
    """A detected (or overridden) CPU hardware envelope (DEC-HARDWARE-ENVELOPE-001).

    RAM is deliberately NOT modelled here — see
    :mod:`brutex.hardware_envelope` (DEC-RAM-BUDGETS-001) for RAM detection +
    RAM-scaled budgets.

    Attributes
    ----------
    logical_cores
        Total logical CPUs (macOS ``hw.logicalcpu`` / Linux
        ``len(os.sched_getaffinity(0))`` / ``os.cpu_count()``); always ``>= 1``.
    physical_cores
        Physical cores (macOS ``hw.physicalcpu``); ``== logical_cores`` where a
        reliable stdlib split is unavailable (Linux — hyperthreading is not
        distinguished).
    performance_cores
        Performance-core logical count (macOS ``hw.perflevel0.logicalcpu``);
        ``== logical_cores`` on a platform with no exposed P/E split.
    efficiency_cores
        Efficiency-core logical count (macOS ``hw.perflevel1.logicalcpu``);
        ``0`` where undetectable / none.
    source
        A non-secret token naming the detection path
        (``"macos_sysctl"`` / ``"linux"`` / ``"fallback"`` / ``"override"``).
    """

    logical_cores: int
    physical_cores: int
    performance_cores: int
    efficiency_cores: int
    source: str


# ────────────────────────────────────────────────────────────────────────
# Low-level probes — all injectable, 100% branch-testable
# ────────────────────────────────────────────────────────────────────────


def _run_sysctl_int(
    key: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int | None:
    """Return the positive integer ``sysctl -n <key>`` prints, or ``None``.

    macOS-only in practice. The argv is a FIXED list (never a shell string), a
    2 s timeout bounds a wedged binary, and EVERY failure — spawn error,
    non-zero exit, non-integer / non-positive output — maps to ``None`` (a clean
    "undetectable" signal, never a raise). *runner* is injected in tests.
    """
    run = subprocess.run if runner is None else runner
    try:
        proc = run(
            ["sysctl", "-n", key],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = int(proc.stdout.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _sched_affinity(affinity_fn: Callable[[int], set[int]] | None = None) -> set[int] | None:
    """The CPU-affinity set (``os.sched_getaffinity(0)``), cgroup/CI-aware, or ``None``.

    ``os.sched_getaffinity`` exists on Linux; absent on macOS / Windows. Honours
    a cgroup / taskset CPU restriction (unlike ``os.cpu_count()``), so a
    container / CI runner sizes to its real quota. *affinity_fn* is injected in
    tests. Returns ``None`` when the call is unavailable or raises.
    """
    fn = getattr(os, "sched_getaffinity", None) if affinity_fn is None else affinity_fn
    if fn is None:
        return None
    try:
        return set(fn(0))
    except OSError:
        return None


# ────────────────────────────────────────────────────────────────────────
# Detection — pure, injectable, uncached
# ────────────────────────────────────────────────────────────────────────


def _detect_macos(
    sysctl_int: Callable[[str], int | None], cpu_count_fn: Callable[[], int | None]
) -> HardwareEnvelope:
    """macOS detection via ``sysctl`` (P/E split)."""
    raw_logical = sysctl_int(_SYSCTL_LOGICAL_KEY)
    logical = raw_logical if raw_logical is not None else _fallback_cpu_count(cpu_count_fn)
    physical = sysctl_int(_SYSCTL_PHYSICAL_KEY) or logical
    perf = sysctl_int(_SYSCTL_PERF_KEY)
    if perf is None:
        performance, efficiency = logical, 0
    else:
        performance, efficiency = perf, (sysctl_int(_SYSCTL_EFF_KEY) or 0)
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=physical,
        performance_cores=performance,
        efficiency_cores=efficiency,
        source="macos_sysctl",
    )


def _detect_linux(
    affinity: set[int] | None,
    cpu_count_fn: Callable[[], int | None],
) -> HardwareEnvelope:
    """Linux detection via affinity (cgroup/CI-aware)."""
    logical = len(affinity) if affinity else _fallback_cpu_count(cpu_count_fn)
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=logical,  # no reliable stdlib HT split — honest over-report.
        performance_cores=logical,  # no exposed P/E split on Linux.
        efficiency_cores=0,
        source="linux",
    )


def _fallback_cpu_count(cpu_count_fn: Callable[[], int | None]) -> int:
    """``os.cpu_count()`` floored to ``1`` (``None`` → 1 — never guess parallelism)."""
    raw = cpu_count_fn()
    return raw if raw is not None and raw >= 1 else 1


def detect_envelope(
    *,
    platform_system: str | None = None,
    sysctl_int: Callable[[str], int | None] | None = None,
    cpu_count_fn: Callable[[], int | None] | None = None,
    affinity_fn: Callable[[int], set[int]] | None = None,
) -> HardwareEnvelope:
    """Detect the :class:`HardwareEnvelope` — macOS → Linux → fallback cascade.

    Pure — every probe is injectable, so all branches are unit-testable off the
    target hardware. O(1) (a handful of ``sysctl`` calls / one affinity call).

    Parameters
    ----------
    platform_system
        Override for :func:`platform.system` (defaults to the real value).
    sysctl_int, cpu_count_fn, affinity_fn
        Probe overrides (default to the real functions) — injected in tests.
    """
    system = platform.system() if platform_system is None else platform_system
    sysctl = _run_sysctl_int if sysctl_int is None else sysctl_int
    cpu_count = os.cpu_count if cpu_count_fn is None else cpu_count_fn

    if system == _DARWIN:
        return _detect_macos(sysctl, cpu_count)
    if system == _LINUX:
        affinity = _sched_affinity(affinity_fn)
        return _detect_linux(affinity, cpu_count)

    logical = _fallback_cpu_count(cpu_count)
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=logical,
        performance_cores=logical,
        efficiency_cores=0,
        source="fallback",
    )


def envelope_from(overrides: Mapping[str, int | str]) -> HardwareEnvelope:
    """Build a :class:`HardwareEnvelope` from explicit *overrides* (the test seam).

    Missing fields default to a single-core envelope, so a test can specify
    only what it exercises (e.g. ``{"performance_cores": 10,
    "logical_cores": 14}``). ``source`` defaults to ``"override"``.
    """
    logical = int(overrides.get("logical_cores", 1))
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=int(overrides.get("physical_cores", logical)),
        performance_cores=int(overrides.get("performance_cores", logical)),
        efficiency_cores=int(overrides.get("efficiency_cores", 0)),
        source=str(overrides.get("source", "override")),
    )


# ────────────────────────────────────────────────────────────────────────
# Cache (detected once per process; reset hook for tests)
# ────────────────────────────────────────────────────────────────────────

_ENVELOPE_CACHE: HardwareEnvelope | None = None


def cached_envelope() -> HardwareEnvelope:
    """Return the process-cached :class:`HardwareEnvelope`, detecting once on first use.

    The hardware is fixed for a process, so detection (a ``sysctl`` /
    affinity probe) runs at most once; :func:`reset_envelope_cache` clears it
    for tests. O(1) after the first call.
    """
    global _ENVELOPE_CACHE  # noqa: PLW0603 — set-once process cache (reset hook below)
    if _ENVELOPE_CACHE is None:
        _ENVELOPE_CACHE = detect_envelope()
    return _ENVELOPE_CACHE


def reset_envelope_cache() -> None:
    """Clear the memoised envelope (test hook)."""
    global _ENVELOPE_CACHE  # noqa: PLW0603 — the reset hook the docstring promises
    _ENVELOPE_CACHE = None


# ────────────────────────────────────────────────────────────────────────
# Sizing helpers — pure, env-overridable
# ────────────────────────────────────────────────────────────────────────


def _env_int(env: Mapping[str, str] | None, key: str) -> int | None:
    """A valid ``>= 1`` integer env override, or ``None`` (invalid logged)."""
    source = os.environ if env is None else env
    raw = source.get(key)
    if raw is None or not raw.strip():
        return None
    try:
        parsed = int(raw)
    except ValueError:
        _LOG.warning("hardware.env_int.invalid", key=key, requested=repr(raw))
        return None
    if parsed < 1:
        _LOG.warning("hardware.env_int.invalid", key=key, requested=repr(raw))
        return None
    return parsed


def cpu_pool_workers(
    env: Mapping[str, str] | None = None,
    *,
    reserve: int = DEFAULT_RESERVE_CPUS,
    cap: int | None = None,
    prefer_performance: bool = True,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """Resolve a CPU-bound pool width — ``max(1, base - reserve)``, optionally capped.

    ``base`` is the ``performance_cores`` when *prefer_performance* (heavy work
    should land on the P-cores; the E-cores of an M4 Pro are markedly slower),
    else ``logical_cores``. A valid ``>= 1`` :data:`CPU_WORKERS_ENV` override
    wins **verbatim — deliberately UNCLAMPED** (it bypasses *reserve* AND
    *cap*): an explicit operator override is trusted as intent, mirroring the
    sweep resolver's env-explicit contract
    (:func:`brutex.engine.sweep_parallel.resolve_sweep_workers`) — an absurd
    value (e.g. ``1000``) is the operator's own conscious lever, visible in the
    caller's ``*.workers.resolved`` log, never silently "corrected". Never
    returns ``< 1``. On the reference M4 Pro (``performance_cores=10``,
    ``reserve=2``) this is **8**. O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) for the override.
    reserve
        Cores to reserve for the rest of the machine (CLAUDE.md §8 "2 reserved").
    cap
        Optional hard ceiling (e.g. the sweep's ``8``); ``None`` = no cap.
    prefer_performance
        Size off ``performance_cores`` (default) vs ``logical_cores``.
    envelope
        Pre-detected envelope (defaults to :func:`cached_envelope`) — injected
        in tests.
    """
    override = _env_int(env, CPU_WORKERS_ENV)
    if override is not None:
        return override
    hw = cached_envelope() if envelope is None else envelope
    base = hw.performance_cores if prefer_performance else hw.logical_cores
    workers = max(1, base - reserve)
    if cap is not None:
        workers = min(workers, cap)
    return workers


def library_thread_budget(
    env: Mapping[str, str] | None = None,
    workers: int = 1,
    *,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """Per-worker numeric-library thread budget so ``workers × budget ≤ physical_cores``.

    ``max(1, physical_cores // workers)`` — the cap a stage that spawns
    *workers* PROCESSES (each with its OWN library thread pool) sets in every
    worker so the aggregate never oversubscribes. Honest note: a THREAD pool
    shares ONE process-wide library pool, so it does NOT multiply — the caller
    decides whether to apply this (a process pool) or leave the shared pool at
    its default (a thread pool). O(1).

    Parameters
    ----------
    env, envelope
        Unused-env placeholder for signature symmetry / a future override, and
        the injected envelope (defaults to :func:`cached_envelope`).
    workers
        The worker count to divide the physical cores across (floored to 1).
    """
    hw = cached_envelope() if envelope is None else envelope
    safe_workers = workers if workers >= 1 else 1
    return max(1, hw.physical_cores // safe_workers)


__all__ = [
    "CPU_WORKERS_ENV",
    "DEFAULT_RESERVE_CPUS",
    "HardwareEnvelope",
    "cached_envelope",
    "cpu_pool_workers",
    "detect_envelope",
    "envelope_from",
    "library_thread_budget",
    "reset_envelope_cache",
]
