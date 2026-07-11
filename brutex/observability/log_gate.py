"""LRU-bounded once-per-key log-emission gate (DEC-RESULTS-TOPK-ROLLUP-001).

A COMMON stdlib-only LEAF (CLAUDE.md §11) shared by the engine rollup writers
(:mod:`brutex.engine.results_rollup`) and the dashboard `/results` serve path
(:mod:`brutex.dashboard.sweep_results_view` / ``app.py``) — homed HERE so
neither side imports the other for it (the CodeQL bidirectional-cycle fix
folded on PR #1436: the dashboard previously imported :class:`LogGate` from
the engine module whose import-time guard imports the dashboard back — the
``#1533`` leaf-homing pattern applied). Modelled on the #1711 ``OutageGate``
precedent: a poll loop against one bad state logs ONCE per key, never per
poll, and a hostile key churn is LRU-bounded.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

__all__ = ["LogGate"]


class LogGate:
    """LRU-bounded once-per-key emission gate (the #1711 ``OutageGate`` precedent).

    ``should_emit(key)`` returns ``True`` exactly once per resident *key*
    (thread-safe; the LRU bound caps a hostile key churn). ``clear_degraded``
    reports-and-resets the degraded latch so a recovery logs ONE INFO.
    """

    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._degraded = False

    def should_emit(self, key: tuple[str, ...]) -> bool:
        """``True`` exactly once per resident *key*; marks the gate degraded."""
        with self._lock:
            self._degraded = True
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True

    def clear_degraded(self) -> bool:
        """Reset the degraded latch; ``True`` iff it was set (log recovery once)."""
        with self._lock:
            was = self._degraded
            self._degraded = False
            return was
