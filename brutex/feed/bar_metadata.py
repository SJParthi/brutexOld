"""Per-bar / per-trade metadata stamping (ADR-0012 D8 + the D13 integrity policy).

Per ADR-0012 D8 (``DEC-NIFTY-ROLL-METADATA-001``) every bar and every simulated
trade is stamped with:

* the **calendar date** (IST trading day),
* the **weekday** (0=Mon … 6=Sun — Python's :meth:`datetime.date.weekday`),
* the **minute-of-session** (whole minutes since the session open, 09:15 IST for
  a regular session; the ACTUAL open for a Muhurat / half-day session),
* the active **contract expiry** (the holiday-shifted near-month futures expiry —
  :class:`brutex.data.futures_roll.FuturesContract`), and
* an **is-expiry-day** flag (the bar's date == the contract's expiry).

This is what makes the time-of-day × combo expectancy map (D6) and the
expiry-day analysis possible. Each stamp is a frozen dataclass that serialises
to a JSON-safe ``dict`` ready for the hash-chained audit log
(:class:`brutex.orchestrator.audit.AuditWriter`, CLAUDE.md §2 Rule 8) — this
module produces the payload; the existing audit writer owns the chain.

## D13 data-integrity policy (this module's predicates)

* **Missing 1-minute bars are NOT forward-filled** — a synthetic bar would
  fabricate a level cross. The stamper NEVER invents a bar; the gap detector
  :func:`missing_minute_bars` reports the absent IST minutes for the caller to
  surface honestly (D13/§4.5 #8), and a synthesized bar is rejected by
  construction (the stamp is computed from a REAL bar timestamp only).
* A **circuit-breaker halt session is FLAGGED** in the metadata
  (:attr:`BarMetadata.circuit_halt`, set by the caller from the session-level
  halt detection — the stamper carries the flag, it does not guess it; D13/§4.5
  #9).
* **Forced-exit uses the last real bar at or before 15:20 IST** on the 1-minute
  base clock (:func:`last_real_bar_at_or_before_forced_exit`), so a session with
  a missing 15:20 bar force-exits on the last real bar ≤ 15:20 instead of
  fabricating one (D13/§4.5 #10).

## Rust-vs-Python split (the EVAL-PATH derivation is RUST)

Per ``DEC-NIFTY-ROLL-METADATA-001`` the metadata that feeds the vocabulary's
time-of-day group (the bar's ``tod_seconds`` is the evaluator's Group-8 input)
and the D6 time-of-day × combo expectancy map — the **minute-of-session,
weekday and is-expiry-day DERIVATION** — lives in the shared Rust
``trading-core`` crate (``rust/trading-core/src/indicators/metadata.rs``,
exposed via PyO3 as ``trading_core.derive_bar_metadata`` /
``trading_core.minute_of_session`` / ``trading_core.weekday_from_ordinal``), so
BruteX (this backtest) and TickVault (native Rust, live) derive **bit-identical**
metadata by construction. THIS module is now a thin **delegating shim**: it does
the timezone localization + the shared-calendar session-open resolution
(BruteX-resident DATA), converts to the IST civil components the Rust core
expects, and lets Rust do the arithmetic.

What stays Python: the timezone localization (the lake stores tz-aware UTC, IST
is a fixed +05:30), the session-open lookup (the NSE calendar is BruteX data),
the D13 gap / forced-exit predicates below (calendar-driven set logic, not a
per-bar numeric kernel), and the bulk pull-time stamping loop (BruteX
orchestration that CALLS this shim per bar).

## COMMON · DYNAMIC · SCALABLE · AUTOMATED

- COMMON: ONE source of truth for the derivation — the Rust ``trading-core``
  crate, shared with TickVault; this shim adds no second copy of the
  seconds-of-day / weekday / minute-of-session arithmetic.
- DYNAMIC: a new metadata field is one Rust struct field + one shim attribute +
  one ``as_audit_payload`` key — no re-architecting.
- SCALABLE: O(1) per bar (one tz convert + one Rust FFI call + field reads).
- AUTOMATED: pure functions, no I/O; the stamp is fully deterministic from the
  bar timestamp + the resolved contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

import trading_core

from brutex.calendars.nse import IST, SESSION_OPEN_IST, get_session_checked, is_forced_exit_bar
from brutex.data.futures_roll import FuturesContract

__all__ = [
    "BarMetadata",
    "last_real_bar_at_or_before_forced_exit",
    "minute_of_session",
    "missing_minute_bars",
    "stamp_bar",
]


def _ist_datetime(ts: datetime) -> datetime:
    """Convert a tz-aware bar timestamp to IST.

    Parameters
    ----------
    ts
        A timezone-aware bar timestamp (stored UTC). A naive datetime is
        rejected — the lake stores tz-aware timestamps (``models._validate_bar``)
        and a naive value would silently mis-localize the session minute.

    Raises
    ------
    ValueError
        If ``ts`` is naive (no ``tzinfo``).
    """
    if ts.tzinfo is None:
        raise ValueError(f"bar timestamp must be timezone-aware, got naive {ts!r}")
    return ts.astimezone(IST)


def minute_of_session(ts: datetime, *, session_open: time = SESSION_OPEN_IST) -> int:
    """Whole minutes from the session open to the bar's IST time-of-day.

    The 09:15 IST open bar is minute 0; 09:16 is minute 1; … the 15:20 forced
    exit is minute 365 of a regular 375-minute session. A bar BEFORE the session
    open (e.g. a stray pre-open 09:00-09:15 bar) yields a NEGATIVE minute — the
    caller is expected to have excluded pre-open bars
    (:func:`brutex.calendars.nse.is_pre_open`); the negative value is surfaced
    honestly rather than clamped (it signals a mis-fed bar, never silently 0).

    Parameters
    ----------
    ts
        A timezone-aware bar timestamp.
    session_open
        The session's open time-of-day in IST (09:15 for a regular session; the
        actual open for a Muhurat / half-day session). Defaults to the regular
        :data:`~brutex.calendars.nse.SESSION_OPEN_IST`.

    Returns
    -------
    int
        Whole minutes since ``session_open`` (seconds truncated toward the open,
        matching the 1-minute bar grid). The DERIVATION is delegated to the
        shared Rust ``trading_core.minute_of_session`` (DEC-NIFTY-ROLL-METADATA-001);
        this function only localizes to IST + computes the seconds-of-day.
    """
    ist = _ist_datetime(ts)
    bar_seconds = _seconds_of_day(ist.hour, ist.minute, ist.second)
    open_seconds = _session_open_seconds(session_open)
    # Shared Rust derivation: floor-division toward the open (a pre-open bar
    # floors to a negative minute — honest, not clamped), bit-identical to the
    # TickVault-native path.
    return int(trading_core.minute_of_session(bar_seconds, open_seconds))


def _seconds_of_day(hour: int, minute: int, second: int) -> int:
    """Seconds-since-IST-midnight for a wall-clock time. One COMMON helper.

    Delegates to the shared Rust ``trading_core.seconds_of_day``
    (DEC-TOD-SECONDS-RUST-SEAM-001) — the SAME derivation the vocabulary
    evaluator + Stage-1A VWAP feed consume, so the eval path holds NO inline
    ``h*3600`` arithmetic and the value is bit-identical to the
    TickVault-native path by construction.
    """
    return int(trading_core.seconds_of_day(hour, minute, second))


#: The regular 09:15 IST session open as seconds-since-midnight, derived ONCE
#: at import so the default-session paths do not re-derive a constant per call.
_REGULAR_OPEN_SECONDS: int = _seconds_of_day(
    SESSION_OPEN_IST.hour, SESSION_OPEN_IST.minute, SESSION_OPEN_IST.second
)


def _session_open_seconds(session_open: time) -> int:
    """Seconds-since-midnight for a session open — hoisted for the regular open.

    The regular 09:15 open (the overwhelmingly common case) returns the
    module constant; a Muhurat / half-day open keeps the dynamic derivation.
    """
    if session_open == SESSION_OPEN_IST:
        return _REGULAR_OPEN_SECONDS
    return _seconds_of_day(session_open.hour, session_open.minute, session_open.second)


@dataclass(frozen=True, slots=True)
class BarMetadata:
    """The D8 metadata stamp for one bar (or one simulated trade's entry bar).

    Parameters
    ----------
    trade_date
        The bar's IST calendar trading date.
    weekday
        ``trade_date.weekday()`` (0=Mon … 6=Sun; 0-4 for a normal session).
    minute_of_session
        Whole minutes since the session open (see :func:`minute_of_session`).
    contract_expiry
        The active near-month futures contract's holiday-shifted expiry.
    is_expiry_day
        ``trade_date == contract_expiry`` — the bar is on the contract's final
        session (an intraday trade here force-exits at 15:20, no overnight roll).
    contract_series_key
        The per-contract series identity
        (:attr:`brutex.data.futures_roll.FuturesContract.series_key`) — the roll
        reset token; a change between two bars is a roll boundary.
    circuit_halt
        Whether the bar's SESSION contained a circuit-breaker halt
        (10/15/20%). The caller supplies this from session-level halt detection;
        the stamper carries it (D13 — flagged, never guessed).
    """

    trade_date: date
    weekday: int
    minute_of_session: int
    contract_expiry: date
    is_expiry_day: bool
    contract_series_key: str
    circuit_halt: bool

    def as_audit_payload(self) -> dict[str, object]:
        """Return a JSON-safe payload for the hash-chained audit log.

        Dates are ISO strings so the dict is :mod:`json`-serialisable as-is for
        :meth:`brutex.orchestrator.audit.AuditWriter.append`.
        """
        return {
            "trade_date": self.trade_date.isoformat(),
            "weekday": self.weekday,
            "minute_of_session": self.minute_of_session,
            "contract_expiry": self.contract_expiry.isoformat(),
            "is_expiry_day": self.is_expiry_day,
            "contract_series_key": self.contract_series_key,
            "circuit_halt": self.circuit_halt,
        }


def stamp_bar(
    ts: datetime,
    contract: FuturesContract,
    *,
    circuit_halt: bool = False,
) -> BarMetadata:
    """Stamp ONE real bar with its D8 metadata. O(1).

    The bar's IST trading date drives the weekday + the expiry-day flag; the
    session open (regular vs Muhurat / half-day) drives the minute-of-session.
    The stamp is computed ENTIRELY from the real bar timestamp + the resolved
    contract — no bar is ever synthesized (D13: no forward-fill).

    Parameters
    ----------
    ts
        A timezone-aware bar timestamp (stored UTC; converted to IST here).
    contract
        The active near-month :class:`~brutex.data.futures_roll.FuturesContract`
        for the bar's trading date (resolve via
        :func:`brutex.data.futures_roll.resolve_active_future`).
    circuit_halt
        Whether the bar's session contained a circuit-breaker halt (D13). The
        caller supplies it from session-level detection; defaults ``False``.

    Returns
    -------
    BarMetadata
        The frozen stamp.

    Raises
    ------
    ValueError
        If ``ts`` is naive.
    brutex.calendars.nse.HolidayDataUnavailableError
        If the bar is dated on a weekday OUTSIDE the published holiday span —
        the session open (and thus the minute-of-session) cannot be verified,
        so the stamp REFUSES loudly (Golden Rule #1) instead of silently
        assuming a regular 09:15 session for a possibly-unlisted holiday.
    """
    ist = _ist_datetime(ts)
    trade_date = ist.date()
    session = get_session_checked(trade_date)
    session_open = session.open_time_ist if session is not None else SESSION_OPEN_IST
    # Delegate the full eval-path derivation (tod_seconds / weekday / minute-of-
    # session / is-expiry-day) to the shared Rust core so this stamp is
    # bit-identical to the TickVault-native path (DEC-NIFTY-ROLL-METADATA-001).
    # The Python side supplies only the IST civil components + the
    # shared-calendar session open (BruteX-resident data).
    open_seconds = _session_open_seconds(session_open)
    _tod_seconds, weekday, minute, is_expiry_day = trading_core.derive_bar_metadata(
        trade_date.toordinal(),
        ist.hour,
        ist.minute,
        ist.second,
        open_seconds,
        contract.expiry.toordinal(),
    )
    return BarMetadata(
        trade_date=trade_date,
        weekday=int(weekday),
        minute_of_session=int(minute),
        contract_expiry=contract.expiry,
        is_expiry_day=bool(is_expiry_day),
        contract_series_key=contract.series_key,
        circuit_halt=circuit_halt,
    )


def missing_minute_bars(
    present: Iterable[datetime],
    expected: Iterable[datetime],
) -> tuple[datetime, ...]:
    """Report expected 1-minute instants ABSENT from *present* (D13 — no fill).

    Missing 1-minute bars are NOT forward-filled (a synthetic bar would
    fabricate a level cross). This is the honest DETECT side: given the expected
    1-minute grid for a session and the bars actually fetched, it returns the
    missing instants (sorted) so the caller can surface the gap — it NEVER
    fabricates a bar.

    Both iterables are compared at the EXACT timezone-aware instant (the lake's
    UTC bar timestamps). Comparison is by instant, so a UTC and an
    IST-expressed copy of the same moment match.

    Parameters
    ----------
    present
        The bar timestamps actually present (tz-aware).
    expected
        The full expected 1-minute grid for the session (tz-aware).

    Returns
    -------
    tuple[datetime, ...]
        The expected instants with no present bar, sorted ascending. Empty when
        the session is complete.

    Raises
    ------
    ValueError
        If any timestamp is naive.
    """
    present_instants = {_aware(ts).timestamp() for ts in present}
    missing = [exp for exp in expected if _aware(exp).timestamp() not in present_instants]
    return tuple(sorted(missing, key=lambda d: d.timestamp()))


def _aware(ts: datetime) -> datetime:
    """Reject a naive timestamp; return ``ts`` unchanged when tz-aware."""
    if ts.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive {ts!r}")
    return ts


def last_real_bar_at_or_before_forced_exit(
    bars: Sequence[datetime],
    *,
    tf_minutes: int = 1,
) -> datetime | None:
    """Return the last REAL bar at or before the 15:20 IST forced exit (D13).

    Forced-exit is evaluated on the 1-minute base clock against REAL bars only —
    if the 15:20 bar itself is missing, the trade force-exits on the last real
    bar that does NOT reach the forced-exit window (the latest bar whose
    ``[open, open+tf)`` does NOT pass 15:20). No bar is fabricated.

    A bar is "at or before the forced exit" when it is NOT itself a forced-exit
    bar per :func:`brutex.calendars.nse.is_forced_exit_bar` — i.e. its window
    ends at or before 15:20. The returned bar is the latest such real bar; the
    caller exits the trade there (the last real price ≤ 15:20).

    Parameters
    ----------
    bars
        The session's REAL bar OPEN timestamps (tz-aware), in any order.
    tf_minutes
        The bar timeframe in whole minutes (1 for the base clock; coarser for a
        resampled trade — the forced-exit test is always on the bar's window).

    Returns
    -------
    datetime | None
        The latest real bar whose window does not pass 15:20 IST, or ``None`` if
        every bar is after the forced exit / the session is empty.

    Raises
    ------
    ValueError
        If any timestamp is naive, or ``tf_minutes < 1`` (mirrors
        :func:`brutex.calendars.nse.is_forced_exit_bar`).
    """
    if tf_minutes < 1:
        raise ValueError(f"tf_minutes must be >= 1, got {tf_minutes}")
    eligible = [ts for ts in bars if not is_forced_exit_bar(_ist_datetime(ts).time(), tf_minutes)]
    if not eligible:
        return None
    return max(eligible, key=lambda d: d.timestamp())
