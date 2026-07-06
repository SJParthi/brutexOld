"""Cross-platform process-resource probe — the ONE source of truth for RSS + fd reads.

DEC-SOAK-REAL-TIER-001 (MIGRATION.md entry #1512). A real multi-hour soak must
watch two whole-process signals a single-point unit test cannot see: resident
memory (RSS) growth and open-file-descriptor (fd) growth. Both were read
ad-hoc before (``tests/stress/test_memory_growth.py`` parsed ``/proc/self/status``
inline, off-Linux → ``None``), and the real soak tier runs on the operator's
**macOS** self-hosted M4-Pro runner (CLAUDE.md §8), where ``/proc`` does NOT
exist — so a Linux-only probe would silently ABSTAIN on the very tier meant to
catch a leak (Golden Rule #10 honesty hole). This module closes that: it is the
COMMON probe every consumer (the memory-growth harness + the soak driver + the
drift monitor's window sampler) imports, Linux-primary with macOS/BSD fallbacks,
so the RSS/fd drift gates actually gate on the long tier.

## RSS — current-vs-peak honesty

* **Linux primary** — ``/proc/self/status`` ``VmRSS`` is the CURRENT resident
  set (KiB), so a window-to-window delta is a genuine growth slope.
* **macOS / non-``/proc`` fallback** — :func:`resource.getrusage` ``ru_maxrss``
  is the process-lifetime PEAK (monotonic, never decreases), normalised to KiB
  (Linux reports KiB, macOS/BSD report BYTES → ``// 1024``). Because it is a
  PEAK not a current value, a consumer must treat the fallback RSS as an
  ABSOLUTE CEILING backstop (does this soak ever cross a hard wall?), NEVER as a
  growth slope — the drift monitor's RSS rule is a ceiling for exactly this
  reason. This revives the resource-ceiling signal class the removed PR-#99
  ``ru_maxrss`` gate abandoned, without its flakiness (it is a backstop, not a
  tight per-test gate).

## fd — descriptor-leak detection

* **Linux primary** — ``len(os.listdir("/proc/self/fd"))``.
* **macOS fallback** — ``len(os.listdir("/dev/fd"))``.

Both count the process's currently-open descriptors; a monotonically rising
count across a soak is a descriptor leak (a Parquet partition / DuckDB
connection / flock sidecar / audit writer never closed).

## Honest envelope (Golden Rule #10)

Every reader is **best-effort** and returns ``None`` when the value is genuinely
unavailable (no ``/proc``, no ``/dev/fd``, ``getrusage`` unsupported) — ``None``
is the drift monitor's "abstain, do not gate" signal, never a fabricated 0. The
module is pure stdlib (``os`` / ``sys`` / ``resource``), dependency-free, and
imports nothing from ``brutex`` — so it is safe to call from a hot measured
region without pulling a heavy dependency graph.
"""

from __future__ import annotations

import os
import sys

__all__ = ["read_fd_count", "read_rss_kib"]


def _read_rss_kib_proc() -> int | None:
    """Return current ``VmRSS`` (KiB) from ``/proc/self/status``, or ``None``.

    ``None`` means the ``VmRSS`` line was absent OR ``/proc`` is unreadable —
    the caller falls back to :func:`_read_rss_kib_rusage`.
    """
    try:
        text = _read_proc_status()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def _read_proc_status() -> str:
    """Read ``/proc/self/status`` as ASCII text (isolated for a deterministic test seam)."""
    with open("/proc/self/status", encoding="ascii") as handle:
        return handle.read()


def _read_rss_kib_rusage() -> int | None:
    """Return peak RSS (KiB) via :func:`resource.getrusage`, or ``None``.

    ``ru_maxrss`` is the process-lifetime PEAK, normalised to KiB: Linux reports
    it in KiB already, macOS/BSD in BYTES (``sys.platform == "darwin"`` →
    ``// 1024``). PEAK not current — the drift monitor uses the fallback only as
    an absolute ceiling. ``None`` when ``resource`` is unavailable (e.g. a
    stripped platform) so the caller degrades honestly.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover — resource is stdlib on POSIX; guarded for portability
        return None
    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(ru_maxrss) // 1024
    return int(ru_maxrss)


def read_rss_kib() -> int | None:
    """Return the process resident-set size in KiB, or ``None`` when unavailable.

    Linux-primary (``/proc/self/status`` ``VmRSS``, a CURRENT value); on any
    non-``/proc`` platform (macOS) falls back to ``ru_maxrss`` (a PEAK value,
    KiB-normalised). ``None`` only when BOTH paths fail.
    """
    proc = _read_rss_kib_proc()
    if proc is not None:
        return proc
    return _read_rss_kib_rusage()


def read_fd_count() -> int | None:
    """Return the count of currently-open file descriptors, or ``None``.

    Linux-primary (``/proc/self/fd``); macOS fallback (``/dev/fd``). ``None``
    when neither directory can be listed, so the fd-drift gate abstains rather
    than fabricating a count.
    """
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(fd_dir))
        except OSError:
            continue
    return None
