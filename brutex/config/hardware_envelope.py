"""COMMON hardware-envelope detection + CPU/RAM sizing (DEC-HARDWARE-ENVELOPE-001).

The ONE source of truth (CLAUDE.md §11 COMMON) for **auto-detecting the host's
CPU + RAM at RUNTIME** and turning that into worker / library-thread / memory
budgets, so every CPU-bound stage uses the operator's full machine (the
CLAUDE.md §8 reference envelope is an Apple M4 Pro — 14 logical cores = 10
Performance + 4 Efficiency, 48 GB RAM) WITHOUT any hardcoded core count.

A **config leaf** — it imports nothing from :mod:`brutex.dashboard` /
:mod:`brutex.engine`, so any stage can size itself off it. **Stdlib-only** —
``psutil`` is deliberately NOT a dependency; detection uses ``sysctl`` (macOS),
``os.sched_getaffinity`` + ``/proc/meminfo`` (Linux, cgroup/CI-aware), falling
back to ``os.cpu_count()``.

## Honest envelope (Golden Rule #10)

* Detection is **best-effort + fail-soft**. An undetectable core count degrades
  to ``os.cpu_count()`` (or ``1``); an undetectable RAM total is the honest
  **sentinel ``0``** (NOT a fabricated number) — :func:`memory_budget_bytes`
  treats ``0`` as "unknown → use the conservative floor", never as "0 bytes
  free".
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
import subprocess
from dataclasses import dataclass
from pathlib import Path
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

#: Env var pinning the memory-budget fraction (a float in ``(0, 1]``).
MEM_FRACTION_ENV: str = "BRUTEX_MEM_FRACTION"

#: Logical CPUs the default pool RESERVES for the rest of the machine — the
#: CLAUDE.md §8 "2 reserved" cores (daily apps run concurrently on the reference
#: M4 Pro). The proven sweep-parallel pattern.
DEFAULT_RESERVE_CPUS: int = 2

#: Conservative memory floor (1 GiB) — the budget :func:`memory_budget_bytes`
#: returns when total RAM is the undetected sentinel ``0`` (never treats
#: "unknown" as "0 free").
DEFAULT_MEMORY_FLOOR_BYTES: int = 1 * 1024**3

#: macOS ``sysctl`` keys.
_SYSCTL_PERF_KEY: str = "hw.perflevel0.logicalcpu"
_SYSCTL_EFF_KEY: str = "hw.perflevel1.logicalcpu"
_SYSCTL_LOGICAL_KEY: str = "hw.logicalcpu"
_SYSCTL_PHYSICAL_KEY: str = "hw.physicalcpu"
_SYSCTL_MEMSIZE_KEY: str = "hw.memsize"

_DARWIN: str = "Darwin"
_LINUX: str = "Linux"

#: ``/proc/meminfo`` keys of interest (values are in kB → converted to bytes).
_MEMINFO_TOTAL: str = "MemTotal"
_MEMINFO_AVAILABLE: str = "MemAvailable"


@dataclass(frozen=True, slots=True)
class HardwareEnvelope:
    """A detected (or overridden) hardware envelope (DEC-HARDWARE-ENVELOPE-001).

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
    total_ram_bytes
        Total physical RAM in bytes, or the honest sentinel ``0`` when
        undetected.
    available_ram_bytes
        Currently-available RAM in bytes (Linux ``MemAvailable``), or ``0`` when
        undetected (macOS — no simple stdlib probe).
    source
        A non-secret token naming the detection path
        (``"macos_sysctl"`` / ``"linux"`` / ``"fallback"`` / ``"override"``).
    """

    logical_cores: int
    physical_cores: int
    performance_cores: int
    efficiency_cores: int
    total_ram_bytes: int
    available_ram_bytes: int
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


def _read_meminfo(path: str | Path = "/proc/meminfo") -> dict[str, int]:
    """Parse ``MemTotal`` / ``MemAvailable`` from ``/proc/meminfo`` into BYTES.

    Returns ``{}`` when the file is unreadable (non-Linux, or a probe error) —
    the caller then uses the ``0`` sentinel. A malformed line is skipped, never
    fatal. Values in ``/proc/meminfo`` are kB; converted to bytes here.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep or key not in (_MEMINFO_TOTAL, _MEMINFO_AVAILABLE):
            continue
        fields = rest.split()
        if not fields:
            continue
        try:
            kb = int(fields[0])
        except ValueError:
            continue
        out[key] = kb * 1024
    return out


# ────────────────────────────────────────────────────────────────────────
# Detection — pure, injectable, uncached
# ────────────────────────────────────────────────────────────────────────


def _detect_macos(
    sysctl_int: Callable[[str], int | None], cpu_count_fn: Callable[[], int | None]
) -> HardwareEnvelope:
    """macOS detection via ``sysctl`` (P/E split + memsize)."""
    raw_logical = sysctl_int(_SYSCTL_LOGICAL_KEY)
    logical = raw_logical if raw_logical is not None else _fallback_cpu_count(cpu_count_fn)
    physical = sysctl_int(_SYSCTL_PHYSICAL_KEY) or logical
    perf = sysctl_int(_SYSCTL_PERF_KEY)
    if perf is None:
        performance, efficiency = logical, 0
    else:
        performance, efficiency = perf, (sysctl_int(_SYSCTL_EFF_KEY) or 0)
    total_ram = sysctl_int(_SYSCTL_MEMSIZE_KEY) or 0
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=physical,
        performance_cores=performance,
        efficiency_cores=efficiency,
        total_ram_bytes=total_ram,
        available_ram_bytes=0,  # macOS: no simple stdlib available-RAM probe.
        source="macos_sysctl",
    )


def _detect_linux(
    affinity: set[int] | None,
    meminfo: Mapping[str, int],
    cpu_count_fn: Callable[[], int | None],
) -> HardwareEnvelope:
    """Linux detection via affinity (cgroup/CI-aware) + ``/proc/meminfo``."""
    logical = len(affinity) if affinity else _fallback_cpu_count(cpu_count_fn)
    total_ram = meminfo.get(_MEMINFO_TOTAL, 0)
    available_ram = meminfo.get(_MEMINFO_AVAILABLE, 0)
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=logical,  # no reliable stdlib HT split — honest over-report.
        performance_cores=logical,  # no exposed P/E split on Linux.
        efficiency_cores=0,
        total_ram_bytes=total_ram,
        available_ram_bytes=available_ram,
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
    meminfo_fn: Callable[[], dict[str, int]] | None = None,
) -> HardwareEnvelope:
    """Detect the :class:`HardwareEnvelope` — macOS → Linux → fallback cascade.

    Pure — every probe is injectable, so all branches are unit-testable off the
    target hardware. O(1) (a handful of ``sysctl`` calls / one affinity call /
    one ``/proc/meminfo`` read).

    Parameters
    ----------
    platform_system
        Override for :func:`platform.system` (defaults to the real value).
    sysctl_int, cpu_count_fn, affinity_fn, meminfo_fn
        Probe overrides (default to the real functions) — injected in tests.
    """
    system = platform.system() if platform_system is None else platform_system
    sysctl = _run_sysctl_int if sysctl_int is None else sysctl_int
    cpu_count = os.cpu_count if cpu_count_fn is None else cpu_count_fn

    if system == _DARWIN:
        return _detect_macos(sysctl, cpu_count)
    if system == _LINUX:
        affinity = _sched_affinity(affinity_fn)
        meminfo = _read_meminfo() if meminfo_fn is None else meminfo_fn()
        return _detect_linux(affinity, meminfo, cpu_count)

    logical = _fallback_cpu_count(cpu_count)
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=logical,
        performance_cores=logical,
        efficiency_cores=0,
        total_ram_bytes=0,
        available_ram_bytes=0,
        source="fallback",
    )


def envelope_from(overrides: Mapping[str, int | str]) -> HardwareEnvelope:
    """Build a :class:`HardwareEnvelope` from explicit *overrides* (the test seam).

    Missing fields default to a single-core, RAM-unknown envelope, so a test can
    specify only what it exercises (e.g. ``{"performance_cores": 10,
    "logical_cores": 14}``). ``source`` defaults to ``"override"``.
    """
    logical = int(overrides.get("logical_cores", 1))
    return HardwareEnvelope(
        logical_cores=logical,
        physical_cores=int(overrides.get("physical_cores", logical)),
        performance_cores=int(overrides.get("performance_cores", logical)),
        efficiency_cores=int(overrides.get("efficiency_cores", 0)),
        total_ram_bytes=int(overrides.get("total_ram_bytes", 0)),
        available_ram_bytes=int(overrides.get("available_ram_bytes", 0)),
        source=str(overrides.get("source", "override")),
    )


# ────────────────────────────────────────────────────────────────────────
# Cache (detected once per process; reset hook for tests)
# ────────────────────────────────────────────────────────────────────────

_ENVELOPE_CACHE: HardwareEnvelope | None = None


def cached_envelope() -> HardwareEnvelope:
    """Return the process-cached :class:`HardwareEnvelope`, detecting once on first use.

    The hardware is fixed for a process, so detection (a ``sysctl`` /
    ``/proc/meminfo`` probe) runs at most once; :func:`reset_envelope_cache`
    clears it for tests. O(1) after the first call.
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
    wins verbatim. Never returns ``< 1``. On the reference M4 Pro
    (``performance_cores=10``, ``reserve=2``) this is **8**. O(1).

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


def memory_budget_bytes(
    env: Mapping[str, str] | None = None,
    *,
    fraction: float,
    floor: int = DEFAULT_MEMORY_FLOOR_BYTES,
    workers: int = 1,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """A memory budget in bytes: ``total × fraction / workers``, floored.

    Prefers ``available_ram_bytes`` (falls back to ``total_ram_bytes``); when
    BOTH are the undetected sentinel ``0`` it returns the conservative *floor*
    (never treats "unknown" as "0 free"). A valid ``(0, 1]``
    :data:`MEM_FRACTION_ENV` overrides *fraction* (an out-of-range value is
    logged + ignored). The result is never below *floor*. O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`).
    fraction
        The fraction of usable RAM to budget (in ``(0, 1]``).
    floor
        The minimum budget + the undetected-RAM fallback.
    workers
        Divide the budget across this many workers (floored to 1).
    envelope
        Pre-detected envelope (defaults to :func:`cached_envelope`).
    """
    fraction = _resolve_fraction(env, fraction)
    hw = cached_envelope() if envelope is None else envelope
    usable = hw.available_ram_bytes if hw.available_ram_bytes > 0 else hw.total_ram_bytes
    if usable <= 0:
        return floor
    safe_workers = workers if workers >= 1 else 1
    budget = int(usable * fraction / safe_workers)
    return max(budget, floor)


def _resolve_fraction(env: Mapping[str, str] | None, fraction: float) -> float:
    """Resolve the memory fraction — a valid ``(0, 1]`` env override wins."""
    source = os.environ if env is None else env
    raw = source.get(MEM_FRACTION_ENV)
    if raw is None or not raw.strip():
        return fraction
    try:
        parsed = float(raw)
    except ValueError:
        _LOG.warning("hardware.mem_fraction_env.invalid", requested=repr(raw))
        return fraction
    if 0.0 < parsed <= 1.0:
        return parsed
    _LOG.warning("hardware.mem_fraction_env.invalid", requested=repr(raw))
    return fraction


__all__ = [
    "CPU_WORKERS_ENV",
    "DEFAULT_MEMORY_FLOOR_BYTES",
    "DEFAULT_RESERVE_CPUS",
    "MEM_FRACTION_ENV",
    "HardwareEnvelope",
    "cached_envelope",
    "cpu_pool_workers",
    "detect_envelope",
    "envelope_from",
    "library_thread_budget",
    "memory_budget_bytes",
    "reset_envelope_cache",
]
