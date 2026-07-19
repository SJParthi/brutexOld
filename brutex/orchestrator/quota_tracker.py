"""Daily-quota exception types + the vendor-neutral ``QuotaGate`` seam.

DEC-QUOTA-VERDICT-MOOT-001 established the dormancy facts: the live vendor
(Groww) has **no daily quota** for backtesting
(``GROWW_DAILY_QUOTA_UNVERIFIED=None``, operator-confirmed 2026-06-13 —
CLAUDE.md §2 Rule 6 / §6), so ZERO production sites ever constructed the
``DailyQuotaTracker`` this module once carried.

DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 (MIGRATION.md #1969) then SUPERSEDED #1591's KEEP-DORMANT clause for this
module ONLY: the dormant tracker implementation (LMDB persistence, IST
rollover, the process-wide env registry, ``ClockSkewError``,
``DAILY_QUOTA_LIMIT``) is DELETED. What SURVIVES — at this byte-stable
import path, because both ingest spines' global-stop sets, the pull-halt
Telegram matcher (``pull_jobs.is_pull_halt``) and ``cli.py``'s halt handling
are load-bearing consumers — is:

* :class:`DailyQuotaExceeded` + :class:`QuotaInvariantError` — the exception
  types every halt path matches on (#1591's "exception sets are NOT
  narrowed" pin is PRESERVED).
* :class:`QuotaGate` — the structural Protocol that retypes the ONE re-wire
  point, ``RateController(quota_tracker=)`` (and the
  ``AdaptiveTokenBucket.quota_tracker`` field beneath it). Should a future
  vendor ever publish a daily quota, enforcement re-wires by IMPLEMENTING
  this Protocol — never by resurrecting the deleted class. The gate call
  order (bucket token first, then ``acquire_or_raise()`` outside the bucket
  lock) is unchanged in ``rate_limiter.py``.

The IST-midnight day-boundary policy record (DEC-P0-028) and the LMDB
single-tuple persistence design live on in the MIGRATION.md ledger entries;
a future implementation starts from the Protocol contract below plus those
records, not from resurrected code.
"""

from __future__ import annotations

from typing import Protocol

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Exceptions — the LIVE, load-bearing surface (byte-stable import path)
# ────────────────────────────────────────────────────────────────────────


class DailyQuotaExceeded(BruteXError):
    """Raised when a wired quota gate's daily limit is hit.

    Load-bearing in production TODAY (with no gate wired, as the halt
    classifier's net): member of both ingest spines' global-stop sets
    (``run_ingest`` / ``option_ingest`` / ``fetch_pipeline``), matched by
    ``pull_jobs.is_pull_halt`` (→ the CRITICAL Telegram halt alert) and by
    ``cli.py``'s halt handling. A future capped vendor's
    :class:`QuotaGate` raises this from ``acquire_or_raise()`` and the whole
    halt chain lights up with zero new code.

    Notes
    -----
    No automatic midnight resume — that would mask real-world quota
    exhaustion (the historical Agent 3 §I false-OK class).
    """

    def __init__(self, detailed_message: str = "") -> None:
        super().__init__(
            detailed_message,
            safe_message="Daily vendor API quota exceeded",
        )


class QuotaInvariantError(BruteXError):
    """Raised when persisted quota state violates a class invariant.

    Examples: negative count (corruption), malformed JSON, missing required
    keys. Kept beside :class:`DailyQuotaExceeded` because the SAME
    global-stop sets and halt matchers carry it (#1591's not-narrowed pin).
    """

    def __init__(self, detailed_message: str) -> None:
        super().__init__(
            detailed_message,
            safe_message="Persisted quota state is corrupt or malformed",
        )


# ────────────────────────────────────────────────────────────────────────
# QuotaGate — the structural re-wire seam
# ────────────────────────────────────────────────────────────────────────


class QuotaGate(Protocol):
    """Structural contract for a daily-quota gate.

    Exactly the members the two consumers call:

    * ``AdaptiveTokenBucket.acquire`` calls :meth:`acquire_or_raise` AFTER
      the bucket token is consumed (``orchestrator/rate_limiter.py``).
    * ``RateController.snapshot`` reads :meth:`current_count` /
      :meth:`remaining` / :attr:`daily_limit` for the dashboard gauge, and
      ``RateController.quota_exhausted`` reads :meth:`remaining`
      (``adapters/rate_common.py``).

    Implementations must be thread-safe (the gate is consulted from
    parallel fetch workers) and raise :class:`DailyQuotaExceeded` from
    :meth:`acquire_or_raise` when the day's budget is spent.
    """

    #: Maximum API calls allowed per vendor day.
    daily_limit: int

    def acquire_or_raise(self) -> object:
        """Atomically check + consume one quota unit, or raise.

        Raises :class:`DailyQuotaExceeded` when the daily limit is already
        reached. The return value is never consumed by any caller (the
        deleted tracker returned the post-increment count — implementations
        may return anything, hence ``object``).
        """
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def current_count(self) -> int:
        """Return the day's consumed count (read-only)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def remaining(self) -> int:
        """Return how many calls remain today (clamped to >= 0)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


__all__ = [
    "DailyQuotaExceeded",
    "QuotaGate",
    "QuotaInvariantError",
]
