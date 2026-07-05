"""Consecutive-failure health counter for the post-pull Greeks-enrichment pass.

DEC-GREEKS-ENRICH-OBSERVE-001 — the pass-level twin of the per-window
``empty_streak``: the Greeks pass is fenced (an ordinary enrich failure can
never fail the pull; since DEC-GREEKS-PARALLEL-001 the two pull-wide HOST
halts — ``LakeWriteHalt`` / ``GreeksEnrichPoolBroken`` — deliberately RE-RAISE
past the fence and fire their own CRITICAL alert, so THAT pass records neither
ok nor fail here), so a persistently broken enrichment (a bad risk-free CSV, a
missing wheel) used to surface only as one swallowed log line per pull. This tiny INJECTED object
(constructed once in ``build_app`` and threaded to the pull job closures — no
module-global mutable state, CLAUDE.md §4) counts CONSECUTIVE whole-pass
failures across pulls and emits ONE structlog WARNING
``pull.greeks_enrich_failing`` at the OK→BREACH threshold crossing, re-arming
after a recovery so a relapse warns again.

Honest envelope (Golden Rule #10): the counter is PROCESS-LOCAL — a dashboard
restart resets it (durable cross-restart memory would need a lake-side stamp —
explicit future work, NOT claimed). Log-only: no Telegram push is fired here
(``SweepAlerter.pull_degraded`` bakes r/s units into its message — dishonest
for a Greeks pass; a units-neutral alert method is a named follow-up).
Per-target quarantines inside the driver never reach this counter — only the
whole-pass ``ok`` verdict does (one corrupt contract is normal operation).
"""

from __future__ import annotations

import threading
from typing import Final

import structlog

_LOG = structlog.get_logger("brutex.dashboard.greeks_health")

#: Default consecutive whole-pass failures before the ONE breach WARNING fires.
DEFAULT_GREEKS_FAILURE_THRESHOLD: Final[int] = 3


class GreeksEnrichHealth:
    """Thread-safe consecutive-failure counter over the Greeks pass verdicts.

    One instance is owned by the app (``build_app``) and observed from the
    background pull-job threads (``/pull-historical`` + ``/pull-pipeline``
    ``_work`` closures), so every mutation runs under one ``threading.Lock``.

    Parameters
    ----------
    threshold : int, optional
        Consecutive ``False`` observations at which the ONE
        ``pull.greeks_enrich_failing`` WARNING fires (clamped to ``>= 1``;
        default :data:`DEFAULT_GREEKS_FAILURE_THRESHOLD`).
    """

    def __init__(self, *, threshold: int = DEFAULT_GREEKS_FAILURE_THRESHOLD) -> None:
        self._lock = threading.Lock()
        self._threshold = max(1, threshold)
        self._consecutive = 0
        self._breached = False
        self._last_error: str | None = None

    def observe(self, ok: bool | None, *, error: str | None = None) -> int:
        """Fold one pull's whole-pass Greeks verdict into the streak.

        ``False`` increments the consecutive-failure count (recording *error*
        as the last seen failure detail); ``True`` resets the count AND
        re-arms the breach latch (a later relapse warns again); ``None``
        ("pass did not run" — a dry-run) is IGNORED, so a synthetic pull never
        moves the streak in either direction. At the OK→BREACH crossing
        (count reaches the threshold while un-breached) exactly ONE structlog
        WARNING ``pull.greeks_enrich_failing`` fires, carrying the consecutive
        count, the threshold, and the last error.

        Parameters
        ----------
        ok : bool or None
            The pull report's ``greeks_enrich_ok`` whole-pass verdict.
        error : str or None, optional
            The pull report's ``greeks_enrich_error`` (``"ErrorType: message"``)
            accompanying a ``False`` verdict; ignored otherwise.

        Returns
        -------
        int
            The consecutive whole-pass failure count AFTER this observation
            (unchanged for ``None``, ``0`` after a ``True`` reset).
        """
        with self._lock:
            if ok is None:
                return self._consecutive
            if ok:
                self._consecutive = 0
                self._breached = False
                self._last_error = None
                return 0
            self._consecutive += 1
            self._last_error = error
            if not self._breached and self._consecutive >= self._threshold:
                self._breached = True
                _LOG.warning(
                    "pull.greeks_enrich_failing",
                    consecutive=self._consecutive,
                    threshold=self._threshold,
                    last_error=self._last_error,
                )
            return self._consecutive


__all__ = [
    "DEFAULT_GREEKS_FAILURE_THRESHOLD",
    "GreeksEnrichHealth",
]
