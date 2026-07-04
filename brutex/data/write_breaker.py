"""Per-partition write-health breaker — a wedged VOLUME halts the pull, not one window.

DEC-WRITE-HEALTH-BREAKER-001. The two ingest spines defer a window whose durable write
times out (:class:`~brutex.data.errors.WriteDeadlineExceeded`, a hung fsync/replace —
DEC-FETCH-HUNG-WRITE-DEADLINE-001) or whose cross-process partition lock cannot be
acquired (:class:`~brutex.data.errors.PartitionLockTimeout` — DEC-LAKE-PARTITION-FLOCK-001).
Each defer is INDEPENDENT per window, so a fully wedged lake volume previously burned the
ENTIRE multi-hour pull deferring window after window at one write-deadline each — the
honest-envelope clause the :class:`~brutex.data.errors.WriteDeadlineExceeded` docstring
carried ("defers the same window every pull until the volume recovers").

This breaker adds the CROSS-WINDOW health accounting: ONE instance per pull observes every
write defer keyed by a per-partition HEALTH key. Two CONSECUTIVE defers on the same health
key open that key's breaker; once :data:`WRITE_BREAKER_DISTINCT_OPEN_LIMIT` (3) DISTINCT
keys are open the volume itself is judged wedged and the breaker raises
``LakeWriteHalt("volume_wedged")`` — which rides the EXISTING #885 global-stop machinery
(both spines' ``_GLOBAL_STOP_ERRORS``, the async pipeline's non-isolatable pool stop) and
the EXISTING #888/#889 CRITICAL Telegram pull-halt alert, with ZERO new alert wiring.

How each spine REACHES the consecutive limit at the SHIPPED defaults (review MAJOR P3-1):

* SPOT keys per series+month partition. One symbol's windows span DIFFERENT months, so a
  single key sees a second defer only via the spine's bounded IN-RUN retry passes
  (``ingest_spot`` ``max_retry_passes`` = 2): pass 0 defers it (streak 1), the retry pass
  defers it again (streak 2) → the key opens.
* F&O keys per UNDERLYING (+ interval + window-start month), NOT per contract: each F&O
  ``(contract, interval)`` pair has exactly ONE clamped ledger window per pass and NO
  in-pass tail retry (the defer goes to the NEXT pull, which builds a FRESH breaker), so a
  per-contract key could never reach streak 2 and the breaker was structurally INERT on
  that spine. Grouped by underlying, a wedged volume's defers on sibling contracts of one
  chain accumulate into a real streak; a durable write on any sibling resets it.

Honest envelope (Golden Rule #10)
---------------------------------
The thresholds are HEURISTICS, not measurements of the volume: two consecutive defers on
one partition can occur under legitimate heavy cross-process contention (a concurrent CLI
+ dashboard pull sharing a lake, #887) — the 3-DISTINCT-partitions second gate is the
mitigation (contention concentrates on shared hot partitions; a wedged volume fails
EVERYWHERE), and the env kill switch (``BRUTEX_WRITE_BREAKER=off``) disarms it entirely.
The breaker is PER-PULL and IN-PROCESS: wedge history does not persist across pulls, an
open partition never half-open re-probes mid-pull, and the window-ledger store's own lock
path is NOT observed — all named follow-ups. A pull whose deferring windows span FEWER
than 3 distinct health keys (e.g. a narrow single-underlying, single-month F&O pull)
still defers per-window without a halt — the honest floor of the distinct gate. No data
is lost either way: every deferred window's ledger row is FAILED and re-pulled
(idempotent keep-first sink).
"""

from __future__ import annotations

import os
import threading
from typing import Final

import structlog

from brutex.data.errors import LakeWriteHalt

__all__ = [
    "WRITE_BREAKER_CONSECUTIVE_LIMIT",
    "WRITE_BREAKER_DISTINCT_OPEN_LIMIT",
    "WriteHealthBreaker",
    "partition_health_key",
    "resolve_write_breaker",
]

_LOG = structlog.get_logger("brutex.data.write_breaker")

#: Consecutive write-defers on ONE partition key that OPEN that key's breaker.
WRITE_BREAKER_CONSECUTIVE_LIMIT: Final[int] = 2

#: DISTINCT open partition keys that judge the whole VOLUME wedged → LakeWriteHalt.
WRITE_BREAKER_DISTINCT_OPEN_LIMIT: Final[int] = 3

#: Kill switch: ``off`` disables the breaker for the pull (defers stay per-window only).
_ENV_MODE: Final[str] = "BRUTEX_WRITE_BREAKER"

#: Env override for :data:`WRITE_BREAKER_CONSECUTIVE_LIMIT` (clamped ``>= 1``).
_ENV_CONSECUTIVE: Final[str] = "BRUTEX_WRITE_BREAKER_CONSECUTIVE"

#: Env override for :data:`WRITE_BREAKER_DISTINCT_OPEN_LIMIT` (clamped ``>= 1``).
_ENV_DISTINCT: Final[str] = "BRUTEX_WRITE_BREAKER_DISTINCT"


def partition_health_key(
    *, exchange: str, segment: str, symbol: str, interval: str, month: str
) -> str:
    """Return the stable per-partition health key the spines observe defers under.

    A series + month string (``NSE/INDEX/NIFTY/1minute/2026-07``) derived from what the
    window tail already has. This is a HEALTH grouping key, NOT a storage key: the SPOT
    spine passes the sink series symbol (the key names a lake month partition), while the
    F&O spine passes the contract's UNDERLYING symbol (grouping the whole chain's sibling
    contracts — a per-contract key could never reach the shipped consecutive limit, see
    the module docstring). A window spanning a month boundary keys on its START month:
    window-granularity is sufficient for health grouping.

    Parameters
    ----------
    exchange, segment, interval
        The series coordinates exactly as passed to the sink append.
    symbol
        The health-group symbol: the sink series symbol (spot) or the contract's
        underlying symbol (F&O).
    month
        The window-start month as ``YYYY-MM``.

    Returns
    -------
    str
        The health key.
    """
    return f"{exchange}/{segment}/{symbol}/{interval}/{month}"


class WriteHealthBreaker:
    """Thread-safe per-pull write-health accounting over partition defer streaks.

    One instance PER PULL (constructed at the spine entry, threaded to both window
    tails). ``observe_defer`` fires from parallel worker / consumer threads, so all
    state lives under one :class:`threading.Lock`; both observers are O(1).

    Semantics (the scoped first step — DEC-WRITE-HEALTH-BREAKER-001):

    * ``observe_defer(key)`` increments *key*'s CONSECUTIVE defer streak; at
      *consecutive_limit* the key OPENS (logged once). When the count of DISTINCT open
      keys reaches *distinct_open_limit* it raises ``LakeWriteHalt("volume_wedged")``.
    * ``observe_success(key)`` resets *key*'s streak. An already-OPEN key stays open for
      the rest of the pull — no half-open re-probing in this step (named follow-up).
    * The trip may raise from MORE than one observing thread (each sees the tripped
      state); both spines treat ``LakeWriteHalt`` idempotently as a global stop, so
      exactly-once raising is deliberately NOT claimed.

    Parameters
    ----------
    consecutive_limit
        Consecutive defers on one key that open it (clamped ``>= 1``).
    distinct_open_limit
        Distinct open keys that trip the volume-wedged halt (clamped ``>= 1``).
    """

    def __init__(
        self,
        *,
        consecutive_limit: int = WRITE_BREAKER_CONSECUTIVE_LIMIT,
        distinct_open_limit: int = WRITE_BREAKER_DISTINCT_OPEN_LIMIT,
    ) -> None:
        self._consecutive_limit = max(consecutive_limit, 1)
        self._distinct_open_limit = max(distinct_open_limit, 1)
        self._lock = threading.Lock()
        self._streaks: dict[str, int] = {}
        self._open: set[str] = set()

    @property
    def open_partitions(self) -> frozenset[str]:
        """The partition keys whose breaker is currently OPEN (a snapshot copy)."""
        with self._lock:
            return frozenset(self._open)

    def observe_defer(self, partition_key: str) -> None:
        """Record ONE write defer on *partition_key*; raise on the volume-wedged trip.

        Parameters
        ----------
        partition_key
            The :func:`partition_health_key` of the deferred window's partition.

        Raises
        ------
        LakeWriteHalt
            ``reason="volume_wedged"`` once *distinct_open_limit* DISTINCT partitions
            have opened — the pull-wide graceful stop (#885) + Telegram alert (#888/#889).
        """
        with self._lock:
            streak = self._streaks.get(partition_key, 0) + 1
            self._streaks[partition_key] = streak
            if streak >= self._consecutive_limit and partition_key not in self._open:
                self._open.add(partition_key)
                _LOG.warning(
                    "lake.write_breaker.partition_open",
                    partition_key=partition_key,
                    consecutive_defers=streak,
                    open_partitions=len(self._open),
                    distinct_open_limit=self._distinct_open_limit,
                )
            tripped = len(self._open) >= self._distinct_open_limit
            open_keys = sorted(self._open) if tripped else []
        if tripped:
            # Raised OUTSIDE the lock (never hold a lock across a raise into foreign
            # frames). The halt rides the existing global-stop + Telegram paths.
            _LOG.error(
                "lake.write_breaker.volume_wedged",
                open_partitions=open_keys,
                distinct_open_limit=self._distinct_open_limit,
            )
            raise LakeWriteHalt("volume_wedged")

    def observe_success(self, partition_key: str) -> None:
        """Reset *partition_key*'s consecutive-defer streak after a durable write.

        An already-OPEN key deliberately stays open for the pull (no half-open
        re-probe in this step — the named follow-up); only the streak resets.

        Parameters
        ----------
        partition_key
            The :func:`partition_health_key` of the successfully written partition.
        """
        with self._lock:
            self._streaks.pop(partition_key, None)


def _env_limit(name: str, default: int) -> int:
    """Parse an integer env override for a breaker limit; garbage/absent → *default*.

    Clamped ``>= 1`` ("if uncertain, keep the breaker armed with a sane bound") so a
    zero/negative override can never make the breaker trip on the first defer of a
    healthy pull nor divide-by-zero the semantics.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 1)
    except ValueError:
        _LOG.warning("lake.write_breaker.bad_env", env=name, value=raw, using=default)
        return default


def resolve_write_breaker() -> WriteHealthBreaker | None:
    """Build the per-pull breaker honouring the env kill switch + limit overrides.

    Returns
    -------
    WriteHealthBreaker | None
        A fresh breaker (the default — armed), or ``None`` when
        ``BRUTEX_WRITE_BREAKER=off`` disables it for the pull (the escape hatch: the
        spines then keep today's independent per-window defer behaviour, byte-identical).
    """
    mode = os.environ.get(_ENV_MODE, "").strip().lower()
    if mode == "off":
        _LOG.info("lake.write_breaker.disabled", env=_ENV_MODE)
        return None
    return WriteHealthBreaker(
        consecutive_limit=_env_limit(_ENV_CONSECUTIVE, WRITE_BREAKER_CONSECUTIVE_LIMIT),
        distinct_open_limit=_env_limit(_ENV_DISTINCT, WRITE_BREAKER_DISTINCT_OPEN_LIMIT),
    )
