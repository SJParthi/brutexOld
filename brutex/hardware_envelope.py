"""Hardware-envelope RAM detection + RAM-scaled budget derivation (DEC-RAM-BUDGETS-001).

Before this module, BruteX's byte-denominated memory budgets were hand-picked
constants keyed verbatim to the CLAUDE.md §8 reference box (48 GB M4 Pro) —
e.g. the 8 GiB sweep rank-region budget
(:data:`brutex.engine.sweep_mem_budget.DEFAULT_MEM_BUDGET_BYTES`, ledger
#1352) and the 16 GiB Bloom bit-array wall
(:data:`brutex.orchestrator.bloom.MAX_BLOOM_BYTES`, DEC-P0-047). On a
smaller machine those constants over-promised (an 8 GiB budget on a 16 GB
laptop invites the raw ``MemoryError`` the budget exists to prevent); on a
larger one they under-used real headroom. This module is the ONE place that
turns "how much RAM does this box actually have" into a per-budget byte
figure:

* :func:`detect_total_ram_bytes` — total PHYSICAL RAM, detected ONCE per
  process (cached; :func:`reset_hardware_envelope_cache` is the test seam).
  Any detection failure maps to the honest sentinel ``0`` = "unknown", which
  is NEVER treated as "no free RAM".
* :func:`memory_budget_bytes` — the COMMON derivation every RAM-scaled
  budget goes through: an explicit env override wins (validated fail-LOUD on
  garbage, Golden Rule #1); undetected RAM (sentinel ``0``) returns the
  call site's ``floor_bytes`` EXACTLY (today's hand-picked constant — the
  pre-PR behavior, bit-for-bit); detected RAM returns
  ``int(total × fraction / workers)``.

SEMANTICS LAW (regression-pinned in ``tests/unit/test_hardware_envelope.py``):
every call site's ``fraction`` is chosen so that a simulated 48 GiB box
derives a budget ``>=`` its old constant (both shipped fractions give EXACT
parity: ``48 GiB × 1/6 = 8 GiB``, ``48 GiB × 1/3 = 16 GiB``). Less RAM →
a smaller budget (the portability win — a 16 GB box legitimately derives
BELOW the old constant; that is the point, not a regression); more RAM →
headroom. There is deliberately NO runtime clamp to the floor on the
detected path.

HONEST ENVELOPE (Golden Rule #10):

* This is a PORTABILITY + headroom change, NOT a measured speedup — no
  performance claim is made anywhere.
* Detection reads TOTAL physical RAM, not available/free RAM — the
  ``fraction`` at each call site is the (heuristic) share of the whole box
  that budget may claim, exactly as the old constants were heuristic shares
  of the 48 GB reference box.
* The macOS ``sysctl hw.memsize`` fallback is UNVERIFIED on real macOS in
  this Linux sandbox — it is covered by a mocked-``subprocess`` unit seam
  only. On Linux (and on macOS builds whose ``os.sysconf`` exposes
  ``SC_PHYS_PAGES``) the primary stdlib path is exercised for real.
* Budgets derived from this module are computed once per process (the
  detection cache guarantees the same answer for every call — no mid-run
  flapping); env overrides are therefore expected to be set BEFORE the
  process starts.

Import hygiene: stdlib + ``structlog`` only — this module sits below every
consumer (engine + orchestrator) and imports nothing from ``brutex``.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
from collections.abc import Mapping
from typing import Final

import structlog

_LOG = structlog.get_logger()

#: The honest "RAM detection failed" sentinel. A budget call site receiving
#: this returns its ``floor_bytes`` (the old hand-picked constant) EXACTLY —
#: the sentinel is NEVER interpreted as "zero bytes of RAM".
UNDETECTED_RAM: Final[int] = 0

#: The absolute path of the macOS ``sysctl`` binary (fixed path — never a
#: ``$PATH`` lookup, so a hijacked PATH cannot substitute the probe).
_SYSCTL_BINARY: Final[str] = "/usr/sbin/sysctl"

#: Bounded wall-clock for the ``sysctl`` fallback — detection must never
#: hang a process start (any timeout maps to the undetected sentinel).
_SYSCTL_TIMEOUT_S: Final[float] = 5.0

# Process-global detection cache + log-once registry. Module-level mutable
# state is deliberate here (the once-per-process law needs a process-global
# memo); :func:`reset_hardware_envelope_cache` is the documented test seam.
_LOCK = threading.Lock()
_DETECTED_RAM_BYTES: int | None = None
_LOGGED_BUDGETS: set[str] = set()


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
        proc = subprocess.run(  # noqa: S603 — fixed absolute binary, list argv, no shell
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
    answer per process (no mid-run budget flapping).

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
    """Reset the process-global detection cache + log-once registry (test seam).

    Production code never calls this — budgets are computed once per process
    by design. Tests use it to simulate different machines (48 GiB parity
    pin, small-RAM shrink, undetected sentinel) deterministically.
    """
    global _DETECTED_RAM_BYTES  # noqa: PLW0603 — the documented process-global memo
    with _LOCK:
        _DETECTED_RAM_BYTES = None
        _LOGGED_BUDGETS.clear()


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
    with _LOCK:
        if key in _LOGGED_BUDGETS:
            return
        _LOGGED_BUDGETS.add(key)
    _LOG.info(
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
    if detected == UNDETECTED_RAM:
        return floor_bytes
    derived = max(1, int(detected * fraction / workers))
    if derived != floor_bytes:
        _log_budget_derived_once(
            key=env_var if env_var is not None else f"fraction={fraction}:floor={floor_bytes}",
            detected_ram_bytes=detected,
            fraction=fraction,
            floor_bytes=floor_bytes,
            workers=workers,
            derived_bytes=derived,
        )
    return derived


__all__ = [
    "UNDETECTED_RAM",
    "detect_total_ram_bytes",
    "memory_budget_bytes",
    "reset_hardware_envelope_cache",
]
