"""COMMON hardware-envelope detection + CPU/RAM/library-thread sizing.

The ONE source of truth (CLAUDE.md §11 COMMON) for **auto-detecting the host's
CPU topology + total RAM at RUNTIME** and turning that into worker /
library-thread / memory budgets, so every CPU-bound stage uses the operator's
full machine (the CLAUDE.md §8 reference envelope is an Apple M4 Pro — 14
logical cores = 10 Performance + 4 Efficiency, 48 GB RAM) WITHOUT any
hardcoded core count.

A **config leaf** — it imports nothing from :mod:`brutex.dashboard` /
:mod:`brutex.engine`, so any stage can size itself off it. **Stdlib-only** —
``psutil`` is deliberately NOT a dependency; detection uses ``sysctl`` (macOS)
and ``os.sched_getaffinity`` (Linux, cgroup/CI-aware), falling back to
``os.cpu_count()``. It never imports ``polars``/``duckdb`` — the polars bound
is an env var, the DuckDB bound a duck-typed ``execute`` call.

## Consolidation (DEC-HW-ENVELOPE-CONSOLIDATE-001)

This module now ALSO hosts the RAM detection + RAM-scaled budget derivation
that shipped as the sibling ``brutex/hardware_envelope.py``
(DEC-RAM-BUDGETS-001, MIGRATION.md #1360) — :func:`detect_total_ram_bytes` /
:data:`UNDETECTED_RAM` / :func:`reset_hardware_envelope_cache` /
:func:`memory_budget_bytes` — ported VERBATIM (env override wins fail-LOUD,
undetected RAM → the call site's ``floor_bytes`` EXACTLY, detected →
``int(total × fraction / workers)`` deliberately NOT floor-clamped), so the
repo carries exactly ONE hardware-envelope module. The old module is DELETED
and every consumer repointed; the #1360 contract is regression-pinned bit-
for-bit by the repointed ``tests/unit/test_hardware_envelope.py``. This
SUPERSEDES the interim two-module split (and its RAM-delegation pin) that the
#1167 conflict resolution carried — the contradictory envelope-mode
``memory_budget_bytes`` duplicate stays DEAD (it had zero consumers); the
#1360 semantics are the one true RAM-budget law.

## Thread bounding (DEC-THREAD-BUDGET-BOUNDS-001)

DuckDB defaults EVERY connection to ALL logical cores, so a dashboard process
holding the instrument-master + registry + ad-hoc reader connections stacks
several full-width pools next to polars' one process-global pool.
:func:`duckdb_thread_budget` / :func:`apply_duckdb_thread_bound` cap each
concurrent-reader connection to ``physical_cores //``
:data:`DEFAULT_EXPECTED_LIBRARY_POOLS` — the per-CONNECTION DuckDB caps ARE
the contention fix. :func:`polars_thread_budget` /
:func:`apply_polars_thread_bound` (entrypoint, BEFORE the first ``import
polars`` — the CLI is the REQUIRED launcher for the bound to apply) pin the
polars global pool to FULL PHYSICAL WIDTH by default (divisor 1): the
operator's goal is USE EVERYTHING, the #1401 parallel prebuild deliberately
runs polars at full width (its worker THREADS share the ONE global pool — a
divided budget would waste half the box in the prebuild, the phase that
matters most, and polars reads the env exactly once so the choice is
irreversible per process), and during a prebuild the heavy movers/DuckDB
reader load is GATED away (`PrebuildGate`, DEC-PIPELINE-AUTOPREBUILD-001
#1145), so there is no real cross-library contention then. The bound still
does real work: it caps polars at PHYSICAL cores (its own default is ALL
LOGICAL cpus — an SMT over-report where they differ) and gives the operator
ONE lever. HONEST premise note (Golden Rule #1): "N workers each spawn a
full-width polars pool" is true for DuckDB (per-connection) but FALSE for
polars (process-global, shared by worker threads) — dividing polars by the
outer worker count would serialise the shared resample pool (the exact
hazard DEC-HARDWARE-ENVELOPE-001 #1401 documents). For a genuinely-concurrent
multi-pool setup pass ``expected_workers`` or set the kill switches:
``BRUTEX_POLARS_THREADS`` / ``BRUTEX_DUCKDB_THREADS``. Deliberately NOT
bounded: :mod:`brutex.data.catalog`'s connection (the ENGINE bar-stream path
— O(1) discipline, CLAUDE.md §2 Rule 5; its latency budget owns the whole
box). Tradeoff note for the two bounded EPHEMERAL readers
(``coverage_reconcile.load_lake_spans`` / ``movers_warm.latest_movers_date``):
an ISOLATED scan on an otherwise-idle box now runs at ``physical // 2`` —
accepted, because both fire alongside pull/prebuild activity in production
and both are bounded, small reads (the 1day partitions / footer aggregates).

## Honest envelope (Golden Rule #10)

* Detection is **best-effort + fail-soft**. An undetectable core count degrades
  to ``os.cpu_count()`` (or ``1``) — never a raise, never a guess above the
  machine; an undetectable RAM total is the honest sentinel
  :data:`UNDETECTED_RAM` (``0`` = "unknown", never "no free RAM").
* ``performance_cores`` / ``efficiency_cores`` are only truly split on macOS
  (``hw.perflevel0/1.logicalcpu``); on Linux every logical CPU is reported as a
  performance core (no exposed P/E split) — an honest over-report, never a
  guess of an efficiency tier that isn't measurable.
* Every helper is **pure + injectable** (via ``envelope=`` / ``env=``) so the
  detection branches are 100% unit-testable on a Linux CI box that is neither an
  M4 Pro nor able to run ``sysctl``.
* The module deliberately carries TWO ``sysctl`` probes — the CPU cascade's
  PATH-resolved ``_run_sysctl_int`` and the #1360 RAM probe's fixed-absolute-
  path ``_sysctl_total_ram_bytes`` — because the #1360 semantics (its own
  once-per-process cache + reset seam + probe order) are regression-pinned
  bit-for-bit; unifying the probes is a NAMED follow-up, not silently folded.

## The vendor FETCH pool is NOT sized here (Golden Rule #3)

This module sizes CPU-bound COMPUTE only. The Groww fetch pool + the per-second
rate ceiling + the 500/min governor are a REAL vendor limit (a single TOTP
token; CLAUDE.md §2 Rule 6) and are deliberately NOT hardware-scaled. The
engine sweep's per-worker ``OMP/RAYON/POLARS=1`` determinism pins
(:mod:`brutex.engine.sweep_parallel`) and the Greeks process-pool worker pins
(:mod:`brutex.data.greeks_enrich`) are likewise untouched.

## Rust seam (DEC-CONFIG-RUST-LEAVES-001, slice T35 milestone 2)

When — and ONLY when — ``BRUTEX_CORE_RUST_HWENV`` is set truthy, the four
PURE derivation laws (the :func:`cpu_pool_workers` derivation arm ·
:func:`library_thread_budget` · :func:`memory_budget_bytes`' post-override
sentinel/derived arm · :func:`hardware_key`'s render arm) route through
the default-OFF, burn-in-checked, fail-closed
:mod:`brutex.config.hardware_envelope_rust` facade to the
``rust/brutex-core`` crate. The flag-OFF default is the verbatim
``_*_python`` oracle beside each public function — byte-identical AND
crate-untouched (bomb-crate spy-pinned). DETECTION (the ``sysctl`` /
affinity / ``sysconf`` / ``/proc/cpuinfo`` probes), every env-override
fail-loud parse, both ``apply_*`` side-effect surfaces, the caches and the
dataclass construction ALL stay Python — the honest seam-split record
lives in the facade's module docstring. Honesty banner (ADR-0020,
mandatory verbatim): expected payoff ≈ 0 — conversion is for uniformity,
not speed.
"""

from __future__ import annotations

import math
import os
import platform
import subprocess  # nosec B404 — fixed-argv `sysctl -n` probes only, no shell
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import structlog

# STRICTLY one-direction (CodeQL py/cyclic-import 670/671): the hwenv-seam
# facade is a config LEAF (stdlib + structlog only at module scope —
# AST-pinned in its test file), so this import can never re-enter this
# module.
from brutex.config.hardware_envelope_rust import (
    hwenv_rust_enabled,
    route_hardware_key,
    route_library_thread_budget,
    route_memory_budget,
    route_pool_workers,
)

# STRICTLY one-direction (CodeQL py/cyclic-import 670/671): the bench-baseline
# core is a pure-stdlib LEAF with ZERO brutex imports (its runtime-key lookup
# lives in the gate CLI, not the core), so this module-scope import of the ONE
# hardware-key law can never re-enter brutex.config.
from brutex.observability.bench_baseline import (
    KEY_SLUG_RE,
    format_hardware_key,
    slugify,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping

_LOG = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Env overrides + constants
# ────────────────────────────────────────────────────────────────────────

#: Env var pinning the CPU-bound pool worker count (a valid ``>= 1`` integer
#: wins over auto-detection). CI / tests set this for determinism.
CPU_WORKERS_ENV: str = "BRUTEX_CPU_WORKERS"

#: Kill-switch env var pinning the polars global-pool thread budget (a valid
#: ``>= 1`` integer wins over the derived budget) — DEC-THREAD-BUDGET-BOUNDS-001.
POLARS_THREADS_ENV: str = "BRUTEX_POLARS_THREADS"

#: Kill-switch env var pinning the per-connection DuckDB thread budget (a
#: valid ``>= 1`` integer wins over the derived budget).
DUCKDB_THREADS_ENV: str = "BRUTEX_DUCKDB_THREADS"

#: The env var polars itself reads at FIRST import to size its global pool.
POLARS_MAX_THREADS_VAR: str = "POLARS_MAX_THREADS"

#: How many numeric-library pools compete for the CPU in one BruteX process —
#: polars' ONE process-global pool + the live DuckDB connection pools. The
#: per-connection DUCKDB budget splits the physical cores across these (the
#: contention fix); the POLARS budget deliberately does NOT (divisor 1 = full
#: physical width — prebuild throughput priority; see the module docstring).
DEFAULT_EXPECTED_LIBRARY_POOLS: int = 2

#: Logical CPUs the default pool RESERVES for the rest of the machine — the
#: CLAUDE.md §8 "2 reserved" cores (daily apps run concurrently on the reference
#: M4 Pro). The proven sweep-parallel pattern.
DEFAULT_RESERVE_CPUS: int = 2

#: The honest "RAM detection failed" sentinel (DEC-RAM-BUDGETS-001). A budget
#: call site receiving this returns its ``floor_bytes`` (the old hand-picked
#: constant) EXACTLY — the sentinel is NEVER interpreted as "zero bytes of
#: RAM".
UNDETECTED_RAM: Final[int] = 0

#: macOS ``sysctl`` keys.
_SYSCTL_PERF_KEY: str = "hw.perflevel0.logicalcpu"
_SYSCTL_EFF_KEY: str = "hw.perflevel1.logicalcpu"
_SYSCTL_LOGICAL_KEY: str = "hw.logicalcpu"
_SYSCTL_PHYSICAL_KEY: str = "hw.physicalcpu"

#: The absolute path of the macOS ``sysctl`` binary used by the RAM probe
#: (fixed path — never a ``$PATH`` lookup, so a hijacked PATH cannot
#: substitute the probe). DEC-RAM-BUDGETS-001, ported verbatim.
_SYSCTL_BINARY: Final[str] = "/usr/sbin/sysctl"

#: Bounded wall-clock for the RAM-probe ``sysctl`` fallback — detection must
#: never hang a process start (any timeout maps to the undetected sentinel).
_SYSCTL_TIMEOUT_S: Final[float] = 5.0

_DARWIN: str = "Darwin"
_LINUX: str = "Linux"

# Process-global RAM-detection cache + log-once registry (DEC-RAM-BUDGETS-001).
# Module-level mutable state is deliberate here (the once-per-process law
# needs a process-global memo); :func:`reset_hardware_envelope_cache` is the
# documented test seam.
_LOCK = threading.Lock()
_DETECTED_RAM_BYTES: int | None = None
_LOGGED_BUDGETS: set[str] = set()


class SupportsExecute(Protocol):
    """A DuckDB-connection-shaped object (duck-typed — this config leaf never
    imports ``duckdb``).

    The parameter is POSITIONAL-ONLY (``/``): the real
    ``duckdb.DuckDBPyConnection.execute`` names its first parameter
    ``query``, so a positional-or-keyword ``sql`` here would make the
    Protocol structurally incompatible with it (pyright rejects the
    keyword-name mismatch — caught on CI, where the duckdb stubs resolve).
    """

    def execute(self, sql: str, /) -> object:
        """Run one SQL statement (the ``SET threads`` pragma)."""


@dataclass(frozen=True, slots=True)
class HardwareEnvelope:
    """A detected (or overridden) CPU hardware envelope (DEC-HARDWARE-ENVELOPE-001).

    RAM is deliberately NOT modelled on this dataclass — total-RAM detection
    (:func:`detect_total_ram_bytes`) and the RAM-scaled budgets
    (:func:`memory_budget_bytes`) live in THIS module with their own
    once-per-process cache (DEC-RAM-BUDGETS-001 semantics, ported verbatim by
    DEC-HW-ENVELOPE-CONSOLIDATE-001).

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
    if not hwenv_rust_enabled():
        return _pool_workers_python(
            hw.performance_cores, hw.logical_cores, prefer_performance, reserve, cap
        )
    return route_pool_workers(
        hw.performance_cores,
        hw.logical_cores,
        prefer_performance,
        reserve,
        cap,
        _pool_workers_python,
    )


def _pool_workers_python(
    performance_cores: int,
    logical_cores: int,
    prefer_performance: bool,
    reserve: int,
    cap: int | None,
) -> int:
    """The verbatim :func:`cpu_pool_workers` derivation — the LIVE oracle.

    Token-for-token the pre-seam derivation body over the resolved
    envelope fields (DEC-CONFIG-RUST-LEAVES-001); every flag-OFF /
    ineligible / degraded / burn-in call serves this.
    """
    base = performance_cores if prefer_performance else logical_cores
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
    if not hwenv_rust_enabled():
        return _library_thread_budget_python(hw.physical_cores, workers)
    return route_library_thread_budget(hw.physical_cores, workers, _library_thread_budget_python)


def _library_thread_budget_python(physical_cores: int, workers: int) -> int:
    """The verbatim :func:`library_thread_budget` body — the LIVE oracle."""
    safe_workers = workers if workers >= 1 else 1
    return max(1, physical_cores // safe_workers)


# ────────────────────────────────────────────────────────────────────────
# RAM detection + RAM-scaled budgets (DEC-RAM-BUDGETS-001, ported verbatim —
# DEC-HW-ENVELOPE-CONSOLIDATE-001)
# ────────────────────────────────────────────────────────────────────────


def _sysconf_total_ram_bytes() -> int:
    """Probe total physical RAM via POSIX ``sysconf`` (Linux; some macOS builds).

    ``SC_PAGE_SIZE × SC_PHYS_PAGES`` — pure stdlib, no subprocess. Any
    failure (missing name, negative/zero value, OS error) returns
    :data:`UNDETECTED_RAM`.

    Returns
    -------
    int
        Total physical RAM in bytes, or ``0`` when undetectable this way.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        return UNDETECTED_RAM
    if page_size <= 0 or phys_pages <= 0:
        return UNDETECTED_RAM
    return int(page_size) * int(phys_pages)


def _sysctl_total_ram_bytes() -> int:
    """Probe total physical RAM via macOS ``sysctl -n hw.memsize``.

    The fallback for macOS builds whose ``os.sysconf`` does not expose
    ``SC_PHYS_PAGES``. UNVERIFIED on real macOS in this Linux sandbox
    (Golden Rule #10) — covered by a mocked-``subprocess`` unit seam only.
    Any failure (binary absent, non-zero exit, garbage/non-positive output,
    timeout) returns :data:`UNDETECTED_RAM`.

    Returns
    -------
    int
        Total physical RAM in bytes, or ``0`` when undetectable this way.
    """
    try:
        proc = subprocess.run(  # noqa: S603 # nosec B603 — fixed absolute binary, list argv, no shell
            [_SYSCTL_BINARY, "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SYSCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return UNDETECTED_RAM
    if proc.returncode != 0:
        return UNDETECTED_RAM
    try:
        value = int(proc.stdout.strip())
    except ValueError:
        return UNDETECTED_RAM
    if value <= 0:
        return UNDETECTED_RAM
    return value


def detect_total_ram_bytes() -> int:
    """Return total PHYSICAL RAM in bytes — detected once, cached per process.

    Probe order: POSIX ``sysconf`` (:func:`_sysconf_total_ram_bytes`) first,
    then the macOS ``sysctl hw.memsize`` fallback
    (:func:`_sysctl_total_ram_bytes`). Every failure path maps to the honest
    :data:`UNDETECTED_RAM` sentinel (``0`` = "unknown", never "no RAM") —
    detection can never raise. The first call logs one structlog event; the
    cached result makes every later call O(1) and guarantees a single stable
    answer per process (no mid-run budget flapping). DEC-RAM-BUDGETS-001.

    Returns
    -------
    int
        Total physical RAM in bytes, or :data:`UNDETECTED_RAM` (``0``) when
        undetectable.
    """
    global _DETECTED_RAM_BYTES  # noqa: PLW0603 — the documented process-global memo
    with _LOCK:
        if _DETECTED_RAM_BYTES is None:
            value = _sysconf_total_ram_bytes()
            if value == UNDETECTED_RAM:
                value = _sysctl_total_ram_bytes()
            _DETECTED_RAM_BYTES = value
            _LOG.info(
                "hardware_envelope.ram_detected",
                total_ram_bytes=value,
                detected=value != UNDETECTED_RAM,
            )
        return _DETECTED_RAM_BYTES


def reset_hardware_envelope_cache() -> None:
    """Reset the RAM-detection + CPU-model caches + log-once registry (test seam).

    Production code never calls this — budgets are computed once per process
    by design. Tests use it to simulate different machines (48 GiB parity
    pin, small-RAM shrink, undetected sentinel) deterministically.
    DEC-RAM-BUDGETS-001, ported verbatim — it deliberately does NOT clear the
    separate :func:`cached_envelope` memo (:func:`reset_envelope_cache` does).
    """
    global _DETECTED_RAM_BYTES, _CPU_MODEL_CACHE  # noqa: PLW0603 — documented process memos
    with _LOCK:
        _DETECTED_RAM_BYTES = None
        _CPU_MODEL_CACHE = None
        _LOGGED_BUDGETS.clear()


def _log_once(key: str, level: str, event: str, **kw: object) -> None:
    """Emit *event* at *level* AT MOST ONCE per process per *key*.

    Shares the :data:`_LOGGED_BUDGETS` registry (cleared by
    :func:`reset_hardware_envelope_cache`, the test seam) so a per-call
    entrypoint helper invoked repeatedly in one process — e.g. every
    ``cli.main`` call across a test session — logs its outcome once instead
    of flooding stdout.
    """
    with _LOCK:
        if key in _LOGGED_BUDGETS:
            return
        _LOGGED_BUDGETS.add(key)
    getattr(_LOG, level)(event, **kw)


def _log_budget_derived_once(
    *,
    key: str,
    detected_ram_bytes: int,
    fraction: float,
    floor_bytes: int,
    workers: int,
    derived_bytes: int,
) -> None:
    """Emit the one-per-process-per-budget derivation INFO line (observability)."""
    _log_once(
        key,
        "info",
        "hardware_envelope.budget_derived",
        budget=key,
        detected_ram_bytes=detected_ram_bytes,
        fraction=fraction,
        floor_bytes=floor_bytes,
        workers=workers,
        derived_bytes=derived_bytes,
    )


def memory_budget_bytes(
    *,
    fraction: float,
    floor_bytes: int,
    workers: int = 1,
    env: Mapping[str, str] | None = None,
    env_var: str | None = None,
) -> int:
    """Derive one RAM-scaled byte budget (the COMMON DEC-RAM-BUDGETS-001 path).

    Resolution order:

    1. ``env_var`` set AND present (non-blank) in the environment → the
       validated explicit operator override (fail-LOUD on garbage or a
       non-positive value — a broken knob must never silently fall back,
       Golden Rule #1). RAM detection is not consulted at all.
    2. RAM undetected (:func:`detect_total_ram_bytes` ==
       :data:`UNDETECTED_RAM`) → ``floor_bytes`` EXACTLY — the call site's
       old hand-picked constant, i.e. the pre-PR behavior bit-for-bit.
    3. RAM detected → ``int(total × fraction / workers)`` (never below 1
       byte). Deliberately NOT clamped to ``floor_bytes``: a smaller machine
       derives a smaller budget (the portability win); the ``>= floor`` law
       holds only at/above the 48 GiB reference box and is enforced by the
       regression-pin TESTS, not by runtime clamping.

    When the derived figure differs from its floor, ONE structlog INFO line
    is emitted per process per budget (keyed by ``env_var`` when given).

    Parameters
    ----------
    fraction
        The share of total physical RAM this budget may claim; must be a
        finite value in ``(0, 1]``. Call sites choose it for EXACT parity
        with their old constant on the CLAUDE.md §8 48 GiB reference box.
    floor_bytes
        The old hand-picked constant (> 0) — returned exactly when RAM is
        undetected.
    workers
        Divisor (>= 1) for budgets shared across concurrent workers;
        default ``1`` (the shipped call sites' fractions already account
        for concurrency, as their old constants did).
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.
    env_var
        Name of the explicit-override environment variable, or ``None`` when
        the caller handles its own override upstream.

    Returns
    -------
    int
        The resolved budget in bytes (always > 0).

    Raises
    ------
    ValueError
        On an invalid ``fraction`` / ``floor_bytes`` / ``workers``, or when
        ``env_var`` holds a non-blank value that is not a positive integer.
    """
    if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        raise ValueError(
            f"memory_budget_bytes: fraction must be a finite value in (0, 1], got {fraction!r}"
        )
    if floor_bytes <= 0:
        raise ValueError(f"memory_budget_bytes: floor_bytes must be > 0, got {floor_bytes!r}")
    if workers < 1:
        raise ValueError(f"memory_budget_bytes: workers must be >= 1, got {workers!r}")
    if env_var is not None:
        source = os.environ if env is None else env
        raw = source.get(env_var, "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(
                    f"{env_var}={raw!r} is not an integer byte count (> 0 required)"
                ) from None
            if value <= 0:
                raise ValueError(f"{env_var}={value} must be > 0 bytes")
            return value
    detected = detect_total_ram_bytes()
    if not hwenv_rust_enabled():
        derived = _memory_budget_python(detected, fraction, floor_bytes, workers)
    else:
        derived = route_memory_budget(
            detected, fraction, floor_bytes, workers, _memory_budget_python
        )
    if detected != UNDETECTED_RAM and derived != floor_bytes:
        _log_budget_derived_once(
            key=env_var if env_var is not None else f"fraction={fraction}:floor={floor_bytes}",
            detected_ram_bytes=detected,
            fraction=fraction,
            floor_bytes=floor_bytes,
            workers=workers,
            derived_bytes=derived,
        )
    return derived


def _memory_budget_python(
    detected_ram_bytes: int, fraction: float, floor_bytes: int, workers: int
) -> int:
    """The verbatim :func:`memory_budget_bytes` post-override arm — the LIVE oracle.

    The DEC-RAM-BUDGETS-001 sentinel + derived laws, token-for-token: the
    undetected sentinel returns *floor_bytes* EXACTLY; a detected total
    derives ``max(1, int(total × fraction / workers))`` — deliberately NOT
    floor-clamped. Validation + the env override + the derivation INFO log
    stay in the public function (the flag-OFF path is byte-identical: the
    sentinel arm always equals its floor, so the log condition gains only
    the equivalent ``detected != UNDETECTED_RAM`` guard).
    """
    if detected_ram_bytes == UNDETECTED_RAM:
        return floor_bytes
    return max(1, int(detected_ram_bytes * fraction / workers))


# ────────────────────────────────────────────────────────────────────────
# Hardware fingerprint + bench-baseline key (DEC-BENCH-ABS-BASELINE-001)
# ────────────────────────────────────────────────────────────────────────

#: Env override for the bench-baseline hardware key. A valid slug wins
#: verbatim (testing + renamed-runner escape); an INVALID value raises
#: fail-LOUD (Golden Rule #1 — a broken knob must never silently fall back).
BENCH_BASELINE_KEY_ENV: Final[str] = "BRUTEX_BENCH_BASELINE_KEY"

#: The macOS ``sysctl`` key naming the machine model (e.g. ``Mac16,8``).
_SYSCTL_MODEL_KEY: Final[str] = "hw.model"

#: The Linux CPU-model source read by :func:`cpu_model_string`.
_PROC_CPUINFO: Final[str] = "/proc/cpuinfo"

# Process-global CPU-model cache (the hardware is fixed for a process);
# cleared by :func:`reset_hardware_envelope_cache` (the test seam).
_CPU_MODEL_CACHE: str | None = None


def _run_sysctl_str(
    key: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    """Return the nonempty string ``sysctl -n <key>`` prints, or ``None``.

    The string twin of :func:`_run_sysctl_int` — fixed argv, bounded
    timeout, EVERY failure (spawn error, non-zero exit, empty output) maps
    to ``None``. *runner* is injected in tests.
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
    value = proc.stdout.strip()
    return value if value else None


def _read_proc_cpuinfo() -> str:
    """Best-effort ``/proc/cpuinfo`` text (Linux); any failure → ``""``."""
    try:
        with open(_PROC_CPUINFO, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _cpuinfo_model_name(text: str) -> str | None:
    """The first ``model name`` value in a ``/proc/cpuinfo`` dump, or ``None``."""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "model name" and value.strip():
            return value.strip()
    return None


def cpu_model_string(
    *,
    platform_system: str | None = None,
    sysctl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    cpuinfo_reader: Callable[[], str] | None = None,
) -> str:
    """The machine's CPU/model identity string — cached once per process.

    macOS: ``sysctl -n hw.model`` (e.g. ``Mac16,8``). Linux: the first
    ``model name`` from ``/proc/cpuinfo``. ANY failure or empty probe
    degrades to the honest ``"unknown"`` — never a guess (Golden Rule #10).
    Cached in the process-global registry
    :func:`reset_hardware_envelope_cache` clears (the test seam). Probe
    overrides are injected in tests only; an injected probe bypasses the
    cache write so tests never poison the process value.
    """
    global _CPU_MODEL_CACHE  # noqa: PLW0603 — documented process cache (reset seam)
    injected = (
        platform_system is not None or sysctl_runner is not None or cpuinfo_reader is not None
    )
    if not injected and _CPU_MODEL_CACHE is not None:
        return _CPU_MODEL_CACHE
    system = platform.system() if platform_system is None else platform_system
    model: str | None = None
    if system == _DARWIN:
        model = _run_sysctl_str(_SYSCTL_MODEL_KEY, runner=sysctl_runner)
    elif system == _LINUX:
        reader = _read_proc_cpuinfo if cpuinfo_reader is None else cpuinfo_reader
        model = _cpuinfo_model_name(reader())
    resolved = model if model else "unknown"
    if injected:
        return resolved
    # Store-then-serve THROUGH the global so the cache write is provably
    # live dataflow (the read below + the fast path above), mirroring the
    # `_DETECTED_RAM_BYTES` memo idiom — CodeQL py/unused-global-variable 672.
    _CPU_MODEL_CACHE = resolved
    return _CPU_MODEL_CACHE


@dataclass(frozen=True, slots=True)
class HardwareFingerprint:
    """The bench-baseline hardware identity (DEC-BENCH-ABS-BASELINE-001).

    Six fields are KEY-DEFINING — ``model_slug`` / ``arch`` /
    ``physical_cores`` / ``performance_cores`` / ``efficiency_cores`` /
    ``total_ram_gib``: any difference in those yields a different
    :func:`hardware_key`, hence a different committed baseline file;
    absolute numbers are never compared across fingerprints.
    ``os_family`` is fingerprint METADATA only — recorded (and governed)
    in the committed baseline document but NOT rendered into the key by
    ``format_hardware_key``. Built from RAW detection only — the
    ``BRUTEX_*`` worker/thread env overrides provably cannot move it
    (test-pinned).
    """

    model_slug: str
    arch: str
    os_family: str
    physical_cores: int
    performance_cores: int
    efficiency_cores: int
    total_ram_gib: int


def hardware_fingerprint(
    *,
    envelope: HardwareEnvelope | None = None,
    total_ram_bytes: int | None = None,
    machine: str | None = None,
    os_family: str | None = None,
    model: str | None = None,
) -> HardwareFingerprint:
    """Detect the :class:`HardwareFingerprint` from RAW probes (injectable).

    Deliberately reads :func:`detect_envelope` /
    :func:`detect_total_ram_bytes` / :func:`platform.machine` /
    :data:`sys.platform` / :func:`cpu_model_string` and NEVER any
    ``BRUTEX_*`` sizing override — the fingerprint answers "what machine
    is this", not "how is it configured". An undetected RAM total keeps
    the honest ``0`` sentinel (rendered ``unk`` in the key).
    """
    hw = detect_envelope() if envelope is None else envelope
    ram = detect_total_ram_bytes() if total_ram_bytes is None else total_ram_bytes
    ram_gib = round(ram / 2**30) if ram != UNDETECTED_RAM else 0
    raw_machine = platform.machine() if machine is None else machine
    raw_family = sys.platform if os_family is None else os_family
    raw_model = cpu_model_string() if model is None else model
    return HardwareFingerprint(
        model_slug=slugify(raw_model),
        arch=slugify(raw_machine),
        os_family=slugify(raw_family),
        physical_cores=hw.physical_cores,
        performance_cores=hw.performance_cores,
        efficiency_cores=hw.efficiency_cores,
        total_ram_gib=ram_gib,
    )


def hardware_key(
    env: Mapping[str, str] | None = None,
    *,
    fingerprint: HardwareFingerprint | None = None,
) -> str:
    """This machine's bench-baseline key, e.g. ``mac16-8-14c10p4e-48g-arm64``.

    A :data:`BENCH_BASELINE_KEY_ENV` override wins IFF it satisfies the
    slug law (else a fail-loud :class:`ValueError`); otherwise the key is
    rendered from the detected fingerprint by the ONE format law
    (:func:`brutex.observability.bench_baseline.format_hardware_key`).

    Raises
    ------
    ValueError
        When the env override is set but violates the key slug law.
    """
    source = os.environ if env is None else env
    override = source.get(BENCH_BASELINE_KEY_ENV, "").strip()
    if override:
        if KEY_SLUG_RE.fullmatch(override) is None:
            raise ValueError(
                f"{BENCH_BASELINE_KEY_ENV}={override!r} violates the hardware-key slug "
                f"law {KEY_SLUG_RE.pattern!r}"
            )
        return override
    fp = hardware_fingerprint() if fingerprint is None else fingerprint
    if not hwenv_rust_enabled():
        return _hardware_key_python(
            fp.model_slug,
            fp.physical_cores,
            fp.performance_cores,
            fp.efficiency_cores,
            fp.total_ram_gib,
            fp.arch,
        )
    return route_hardware_key(
        fp.model_slug,
        fp.physical_cores,
        fp.performance_cores,
        fp.efficiency_cores,
        fp.total_ram_gib,
        fp.arch,
        _hardware_key_python,
    )


def _hardware_key_python(
    model_slug: str,
    physical_cores: int,
    performance_cores: int,
    efficiency_cores: int,
    total_ram_gib: int,
    arch: str,
) -> str:
    """The verbatim :func:`hardware_key` render arm — the LIVE oracle.

    A thin field-splat over the ONE format law
    (:func:`brutex.observability.bench_baseline.format_hardware_key`) —
    the law itself stays in its bench_baseline home; the Rust twin's
    burn-in compares against exactly this call.
    """
    return format_hardware_key(
        model_slug=model_slug,
        physical_cores=physical_cores,
        performance_cores=performance_cores,
        efficiency_cores=efficiency_cores,
        total_ram_gib=total_ram_gib,
        arch=arch,
    )


# ────────────────────────────────────────────────────────────────────────
# Library thread bounds — polars (entrypoint env) + DuckDB (per connection)
# DEC-THREAD-BUDGET-BOUNDS-001
# ────────────────────────────────────────────────────────────────────────


def _pool_divisor(expected_workers: int | None) -> int:
    """Resolve the library-pool divisor (``None``/``< 1`` → the default)."""
    if expected_workers is not None and expected_workers >= 1:
        return expected_workers
    return DEFAULT_EXPECTED_LIBRARY_POOLS


def polars_thread_budget(
    env: Mapping[str, str] | None = None,
    *,
    expected_workers: int | None = None,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """The polars global-pool thread budget for this process.

    A valid ``>= 1`` :data:`POLARS_THREADS_ENV` kill-switch override wins
    verbatim; otherwise :func:`library_thread_budget` over *expected_workers*
    — which DEFAULTS TO 1, i.e. FULL PHYSICAL WIDTH: the #1401 parallel
    prebuild's worker threads SHARE the one process-global polars pool and
    deliberately run it at full width (dividing it would waste half the box
    in the phase the operator cares most about, and polars reads the env
    exactly once, so the choice is irreversible per process); cross-library
    contention is handled by the per-connection DuckDB caps
    (:func:`duckdb_thread_budget`) + the movers `PrebuildGate`. Pass
    ``expected_workers`` (or the kill switch) for a genuinely-concurrent
    multi-pool setup. O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`).
    expected_workers
        Explicit competing-pool divisor; ``None``/``< 1`` → ``1`` (full
        physical width).
    envelope
        Pre-detected envelope (defaults to :func:`cached_envelope`).
    """
    override = _env_int(env, POLARS_THREADS_ENV)
    if override is not None:
        return override
    divisor = expected_workers if expected_workers is not None and expected_workers >= 1 else 1
    return library_thread_budget(env, divisor, envelope=envelope)


def apply_polars_thread_bound(
    env: MutableMapping[str, str] | None = None,
    *,
    expected_workers: int | None = None,
    envelope: HardwareEnvelope | None = None,
    loaded_modules: Mapping[str, object] | None = None,
) -> int | None:
    """Set ``POLARS_MAX_THREADS`` for this process — call at the entrypoint.

    Polars sizes its ONE process-global thread pool from the
    :data:`POLARS_MAX_THREADS_VAR` env var at its FIRST import, so this must
    run BEFORE any ``import polars`` (the production choke point is
    :func:`brutex.cli.main` — every BruteX process, CLI fetch and dashboard
    alike, enters there and imports polars lazily afterwards; the call sits
    right after the polars-free ``version`` fast path, whose byte-exact
    stdout contract is test-pinned). Honest + safe by construction:

    * an operator-preset ``POLARS_MAX_THREADS`` is RESPECTED, never overwritten
      (logged ``hardware.polars_bound.preset``);
    * if polars is ALREADY imported the env var can no longer take effect, so
      nothing is written and ONE ``hardware.polars_bound.too_late`` warning is
      emitted per process (Golden Rule #10 — never pretend a bound applied;
      once-per-process because an embedding process — a test session driving
      ``main`` hundreds of times — would otherwise flood stdout);
    * otherwise the :func:`polars_thread_budget` is written and logged
      (``hardware.polars_bound.applied``).

    Parameters
    ----------
    env
        The mutable environment mapping read AND written (defaults to
        :data:`os.environ`) — injectable so tests never mutate the real env.
    expected_workers
        Forwarded to :func:`polars_thread_budget`.
    envelope
        Pre-detected envelope (defaults to :func:`cached_envelope`).
    loaded_modules
        Injectable stand-in for :data:`sys.modules` (tests).

    Returns
    -------
    int | None
        The budget written, or ``None`` when nothing was written (operator
        preset, or polars already imported).
    """
    environ: MutableMapping[str, str] = os.environ if env is None else env
    preset = environ.get(POLARS_MAX_THREADS_VAR, "")
    if preset.strip():
        _log_once("polars_bound:preset", "info", "hardware.polars_bound.preset", value=preset)
        return None
    modules = sys.modules if loaded_modules is None else loaded_modules
    if "polars" in modules:
        _log_once(
            "polars_bound:too_late",
            "warning",
            "hardware.polars_bound.too_late",
            detail="polars already imported — POLARS_MAX_THREADS would be ignored; not set",
        )
        return None
    budget = polars_thread_budget(environ, expected_workers=expected_workers, envelope=envelope)
    environ[POLARS_MAX_THREADS_VAR] = str(budget)
    _log_once(
        "polars_bound:applied",
        "info",
        "hardware.polars_bound.applied",
        budget=budget,
        expected_workers=_pool_divisor(expected_workers),
    )
    return budget


def duckdb_thread_budget(
    env: Mapping[str, str] | None = None,
    *,
    expected_workers: int | None = None,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """The per-connection DuckDB thread budget for this process.

    A valid ``>= 1`` :data:`DUCKDB_THREADS_ENV` kill-switch override wins
    verbatim; otherwise :func:`library_thread_budget` over *expected_workers*
    concurrently-competing library pools (default
    :data:`DEFAULT_EXPECTED_LIBRARY_POOLS`). Unlike polars, DuckDB's pool is
    PER CONNECTION — a dashboard holding the instrument-master + registry +
    ad-hoc reader connections would otherwise stack several all-cores pools.
    O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`).
    expected_workers
        The competing-pool divisor; ``None``/``< 1`` → the default.
    envelope
        Pre-detected envelope (defaults to :func:`cached_envelope`).
    """
    override = _env_int(env, DUCKDB_THREADS_ENV)
    if override is not None:
        return override
    return library_thread_budget(env, _pool_divisor(expected_workers), envelope=envelope)


def apply_duckdb_thread_bound(
    conn: SupportsExecute,
    env: Mapping[str, str] | None = None,
    *,
    threads: int | None = None,
    expected_workers: int | None = None,
    envelope: HardwareEnvelope | None = None,
) -> int:
    """Bound one DuckDB connection's thread pool (``SET threads TO <n>``).

    *threads* is the caller's explicit per-connection override (fail-loud on a
    ``< 1`` value — a broken knob must never silently fall back, Golden Rule
    #1); ``None`` derives :func:`duckdb_thread_budget`. The pragma changes
    query PARALLELISM only — results are byte-identical. O(1) per connection
    open (never per query).

    Parameters
    ----------
    conn
        The connection (duck-typed ``execute`` — this config leaf never
        imports ``duckdb``).
    env
        Environment mapping (defaults to :data:`os.environ`).
    threads
        Explicit per-connection thread count; ``None`` = derive.
    expected_workers, envelope
        Forwarded to :func:`duckdb_thread_budget`.

    Returns
    -------
    int
        The thread count applied.

    Raises
    ------
    ValueError
        If *threads* is given and ``< 1``.
    """
    if threads is not None and threads < 1:
        raise ValueError(f"apply_duckdb_thread_bound: threads must be >= 1, got {threads!r}")
    budget = (
        threads
        if threads is not None
        else duckdb_thread_budget(env, expected_workers=expected_workers, envelope=envelope)
    )
    conn.execute(f"SET threads TO {int(budget)}")  # nosec B608 — integer-only interpolation
    _LOG.debug("hardware.duckdb_bound.applied", budget=budget, explicit=threads is not None)
    return budget


__all__ = [
    "BENCH_BASELINE_KEY_ENV",
    "CPU_WORKERS_ENV",
    "DEFAULT_EXPECTED_LIBRARY_POOLS",
    "DEFAULT_RESERVE_CPUS",
    "DUCKDB_THREADS_ENV",
    "POLARS_MAX_THREADS_VAR",
    "POLARS_THREADS_ENV",
    "UNDETECTED_RAM",
    "HardwareEnvelope",
    "HardwareFingerprint",
    "SupportsExecute",
    "apply_duckdb_thread_bound",
    "apply_polars_thread_bound",
    "cached_envelope",
    "cpu_model_string",
    "cpu_pool_workers",
    "detect_envelope",
    "detect_total_ram_bytes",
    "duckdb_thread_budget",
    "envelope_from",
    "hardware_fingerprint",
    "hardware_key",
    "library_thread_budget",
    "memory_budget_bytes",
    "polars_thread_budget",
    "reset_envelope_cache",
    "reset_hardware_envelope_cache",
]
