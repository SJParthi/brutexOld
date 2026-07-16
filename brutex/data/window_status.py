"""Per-window status model — the precise SUCCESS / EMPTY / FAILED tracker.

DEC-WINDOW-STATUS-001. The skip-ledger (:mod:`brutex.data.fetch_coverage`) is a
flat *set* of completed windows — it answers "skip this window?" but cannot tell
**why** a window is or isn't done. This module is the richer, browsable record:
one row per ``(market, security_id, interval, window_start, window_end)`` fetch
window, stamped with an explicit status, attempt count, bars, and last error, so
the operator can SEE exactly which windows are stored, genuinely empty, or
failed (and need a re-pull).

It was the **visibility layer** that complemented DEC-LAKE-RECONCILE-001 (#463,
which then decided the skip). **WRITE-RETIRED** (DEC-STATUS-SIDECAR-WRITE-
RETIRE-001): the unified window ledger (DEC-WINDOW-LEDGER-001) superseded this
store as the single skip + visibility authority and NO production caller
records into it any more — the module survives ONLY as (a) the read FALLBACK
for pre-ledger lakes on /coverage/windows and (b) the one-shot migration source
for :func:`~brutex.data.window_ledger_store.load_or_migrate_window_ledger`.
Full deletion is the DEC-WINDOW-LEDGER-001 Stage-6 item.

## Status semantics (aligned with the ingest mark rule, DEC-LAKE-RECONCILE-001)

* ``SUCCESS`` — the vendor returned bars (``raw_bars > 0``); the window is stored
  (or already was). This is exactly the condition under which the ingest marks a
  window complete in the skip-ledger.
* ``EMPTY`` — a clean 200 with **zero** bars (a genuinely-empty range, e.g.
  SENSEX 1-minute before 2021-08-04, or a load-induced empty). NOT a failure —
  but also NOT marked complete by the ingest, so it is re-attempted next pull.
* ``FAILED`` — an error (429 / timeout / transport / 5xx / terminal); the window
  was not fetched and must be re-pulled.

## Honesty (CLAUDE.md §1 / §10)

This RECORDS outcomes; it does NOT itself decide the skip (DEC-LAKE-RECONCILE-001
owns that). It is pure + in-memory here (``WindowStatusLog``), serialised /
restored for the durable store (:mod:`brutex.data.window_status_store`); every
mutate / read is O(1), the snapshot is O(N) over the recorded windows.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger_rust import (
    routed_classify_window_status,
    routed_window_merge_key,
)

__all__ = [
    "EMPTY",
    "FAILED",
    "SUCCESS",
    "WindowStatusLog",
    "WindowStatusRecord",
    "classify_window_status",
    "window_key_python",
]

#: The vendor returned bars — the window is stored (the ingest marks it complete).
SUCCESS: str = "SUCCESS"
#: A clean 200 with zero bars — genuinely/transiently empty, re-attempted next pull.
EMPTY: str = "EMPTY"
#: An error (429 / timeout / transport / 5xx / terminal) — must be re-pulled.
FAILED: str = "FAILED"


def classify_window_status(raw_bars: int, error_type: str | None, *, degraded: bool = False) -> str:
    """Classify a window's fetch outcome into :data:`SUCCESS` / :data:`EMPTY` / :data:`FAILED`.

    Pure. Mirrors the ingest mark rule (DEC-LAKE-RECONCILE-001): an *error*
    (``error_type`` set) is ``FAILED``; a clean fetch with bars
    (``raw_bars > 0``) is ``SUCCESS`` (the same condition under which the window
    is marked complete); a clean fetch with **zero** bars is ``EMPTY``.

    Parameters
    ----------
    raw_bars
        The number of bars the vendor returned for the window (the un-filtered
        count — a re-pull whose bars are all already stored still has
        ``raw_bars > 0`` and is ``SUCCESS``).
    error_type
        The error class name (e.g. ``"GrowwTimeoutError"``) when the fetch raised
        a retryable/terminal error, else ``None``.
    degraded
        A clean DATA-COMPLETENESS demotion signal (DEC-INTERIOR-GAP-DETECT-001 /
        DEC-PARTIAL-WINDOW-REPULL-001). When ``True`` AND the fetch is a clean
        bars-returning ``SUCCESS``, the window is demoted to ``FAILED`` so it is NOT
        skippable and is re-pulled — used when the stored bars carry a confirmed
        INTERIOR multi-session hole or a TRAILING truncation. The bars are already
        durably stored (idempotent keep-first sink); the demotion only re-opens the
        window for completion, never deletes data. ``degraded`` is IGNORED for a
        genuine error (already ``FAILED``) or an EMPTY (no bars to be incomplete) —
        only a would-be ``SUCCESS`` is affected.

    Returns
    -------
    str
        One of :data:`SUCCESS`, :data:`EMPTY`, :data:`FAILED`.

    Routing (DEC-LAKE-RUST-WINDOW-LEDGER-001, ADR-0020 Wave C-3 / G8): the
    law is served by :func:`_classify_window_status_python` — the verbatim
    Python body, the LIVE parity oracle — via the default-OFF
    ``BRUTEX_LAKE_RUST_WINDOW_LEDGER`` facade
    (:mod:`brutex.data.window_ledger_rust`). Flag OFF (the default) returns
    the oracle's result directly, byte-identical to the pre-facade law.
    """
    return routed_classify_window_status(
        raw_bars,
        error_type,
        degraded=degraded,
        python_law=_classify_window_status_python,
    )


def _classify_window_status_python(
    raw_bars: int, error_type: str | None, *, degraded: bool = False
) -> str:
    """The VERBATIM Python status law — the live parity oracle.

    The body of :func:`classify_window_status` as it stood before the G8
    routing (DEC-LAKE-RUST-WINDOW-LEDGER-001), moved token for token; the
    Rust mirror is ``rust/brutex-lake/src/window_ledger.rs``. Semantics are
    documented on the public function.
    """
    if error_type is not None:
        return FAILED
    if raw_bars > 0:
        return FAILED if degraded else SUCCESS
    return EMPTY


def window_key_python(key: WindowKey) -> tuple[str, str, str, str, str]:
    """The VERBATIM Python 5-tuple merge-key law — the live parity oracle.

    ``(market, security_id, interval, start.isoformat(), end.isoformat())``
    — the single window-identity law shared by :meth:`WindowStatusLog._key`
    and ``WindowLedger._key`` (both delegate here through the G8 routing
    facade, consolidating the two prior verbatim copies — §11 COMMON). The
    Rust mirror renders only the two date strings
    (``brutex_lake.date_isoformat``); the three identity strings never
    cross the FFI (DEC-LAKE-RUST-WINDOW-LEDGER-001).
    """
    return (
        key.market,
        key.security_id,
        key.interval,
        key.start.isoformat(),
        key.end.isoformat(),
    )


@dataclass(frozen=True, slots=True)
class WindowStatusRecord:
    """One window's recorded status — the unit of the browsable status table.

    Immutable. ``security_id`` is the engine ``symbol_name`` (the same field
    :class:`~brutex.data.fetch_coverage.WindowKey` uses), so a status row keys to
    the exact window the skip-ledger probes.
    """

    market: str
    security_id: str
    interval: str
    window_start: str  # ISO YYYY-MM-DD (serialisation-friendly)
    window_end: str
    status: str
    bars: int
    attempts: int
    last_error: str | None
    last_attempt_at: str  # ISO-8601 timestamp


@dataclass(slots=True)
class WindowStatusLog:
    """An O(1) per-window status map, recorded across one (or more) pull runs.

    Thread-safe (DEC-DHAN-PARALLEL-INGEST-001): one log is threaded into every
    parallel window worker, so :meth:`record` is called concurrently. A
    :class:`threading.Lock` guards the map so two workers stamping different
    windows never tear it. The lock is held only for the O(1) dict write — never
    across I/O. ``attempts`` accumulates across re-pulls (a window FAILED then
    re-pulled to SUCCESS shows ``attempts == 2``), giving the cross-run history
    the flat skip-set cannot.
    """

    _by_window: dict[tuple[str, str, str, str, str], WindowStatusRecord] = field(
        default_factory=dict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @staticmethod
    def _key(key: WindowKey) -> tuple[str, str, str, str, str]:
        """The 5-tuple identity of a window (market, symbol, interval, start, end)."""
        return (
            key.market,
            key.security_id,
            key.interval,
            key.start.isoformat(),
            key.end.isoformat(),
        )

    def record(
        self,
        key: WindowKey,
        *,
        raw_bars: int,
        error_type: str | None,
        bars_appended: int,
        now: datetime,
    ) -> str:
        """Record *key*'s outcome; classify + bump the attempt count — **O(1)**, atomic.

        Returns the classified status. ``attempts`` increments on every record so
        a window's full history (e.g. FAILED → re-pulled → SUCCESS) is visible.
        ``bars`` stores the rows actually persisted (``bars_appended``); ``status``
        is :func:`classify_window_status` of ``(raw_bars, error_type)``.
        """
        status = classify_window_status(raw_bars, error_type)
        with self._lock:
            wk = self._key(key)
            prior = self._by_window.get(wk)
            attempts = (prior.attempts if prior is not None else 0) + 1
            self._by_window[wk] = WindowStatusRecord(
                market=key.market,
                security_id=key.security_id,
                interval=key.interval,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                status=status,
                bars=bars_appended,
                attempts=attempts,
                last_error=error_type,
                last_attempt_at=now.isoformat(),
            )
        return status

    @property
    def count(self) -> int:
        """Number of distinct windows recorded (thread-safe)."""
        with self._lock:
            return len(self._by_window)

    def records(self) -> list[WindowStatusRecord]:
        """A deterministic, sorted snapshot of every recorded window (atomic)."""
        with self._lock:
            return sorted(
                self._by_window.values(),
                key=lambda r: (
                    r.market,
                    r.security_id,
                    r.interval,
                    r.window_start,
                    r.window_end,
                ),
            )

    def to_records(self) -> list[tuple[str, str, str, str, str, str, int, int, str | None, str]]:
        """Serialise to a deterministic list of plain tuples (for the durable store)."""
        return [
            (
                r.market,
                r.security_id,
                r.interval,
                r.window_start,
                r.window_end,
                r.status,
                r.bars,
                r.attempts,
                r.last_error,
                r.last_attempt_at,
            )
            for r in self.records()
        ]

    @classmethod
    def from_records(
        cls,
        rows: Iterable[tuple[str, str, str, str, str, str, int, int, str | None, str]],
    ) -> WindowStatusLog:
        """Restore from :meth:`to_records` output (round-trips exactly)."""
        log = cls()
        for (
            market,
            security_id,
            interval,
            window_start,
            window_end,
            status,
            bars,
            attempts,
            last_error,
            last_attempt_at,
        ) in rows:
            log._by_window[(market, security_id, interval, window_start, window_end)] = (
                WindowStatusRecord(
                    market=market,
                    security_id=security_id,
                    interval=interval,
                    window_start=window_start,
                    window_end=window_end,
                    status=status,
                    bars=bars,
                    attempts=attempts,
                    last_error=last_error,
                    last_attempt_at=last_attempt_at,
                )
            )
        return log
