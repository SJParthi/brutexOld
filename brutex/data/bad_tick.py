"""Bounded bad-tick (spike / fat-finger / vendor-scaling) screen at the engine
read boundary (``DEC-SWEEP-DATA-EDGE-GUARDS-001``).

The problem this closes (the D-MAJ-2 gap): the existing stored-data validation
(:mod:`brutex.data.price_sanity` + the :func:`arrow_table_to_bars` positivity
screen) rejects only NON-positive / mis-ORDERED bars. A *self-consistent* scaled
bar — every OHLC field multiplied (or divided) by the same factor, e.g. a NIFTY
minute printed ``2794`` instead of ``27940`` — is internally ordered and
strictly positive, so it passes every existing check and then silently flips
every price-relative condition bit, corrupts the running EMA /
market-structure swing ring (and, before its bits-52/53 retirement, the
VWAP), and (via ``TodayOHLC.absorb``'s running max/min) poisons the NEXT day's
PDH/PDL/pivots too. This module adds a conservative, bounded, O(1)/bar screen at
the SAME engine read mouth the DEC-STORED-DATA-VALIDATION-001 precedent guards,
so such a bar is SKIPPED-and-COUNTED (never value-edited, never fabricated)
before it reaches Stage-1A / run identity.

Threshold justification (Golden Rule #10 — a conservative bound derived from
real Indian-market physics, NOT a magic number):

* NSE applies **index-wide circuit breakers at 10% / 15% / 20%** daily moves
  *from the prior close*, halting the WHOLE market — so within a single
  continuous session the index physically cannot travel more than one 20% band
  before a full-day halt, and a single-MINUTE move is a small fraction of that.
* The largest single-DAY NIFTY moves on record (COVID, March 2020) were on the
  order of **−13% / +9%** for the whole day; the largest single-MINUTE move is
  far smaller (well under a couple of percent).
* A fat-finger / decimal-point-shift / vendor-scaling corruption is
  characteristically an **order of magnitude** (``x10`` or ``÷10`` → a close
  ratio of ``10.0`` or ``0.1``).

:data:`BAD_TICK_MAX_RATIO` = ``4.0`` (reject a close ``>= 4x`` OR ``<= 1/4x`` the
previous IN-SESSION close) therefore sits ~2 orders of magnitude ABOVE the
largest plausible real single-minute return, yet comfortably BELOW the ``x10``
scaling class (``10 > 4`` and ``0.1 < 0.25``): it catches the scaled-bar family
while being impossible to trip on real index / stock minute data. Regular-day
bars move ``<< 0.25x`` per minute, so every existing golden is byte-identical.

Contract (deliberately narrow, to never destroy real data):

* the comparison is **within the SAME IST session only** — the FIRST bar of
  each session is exempt (``prev_close`` is ``None``), so a legitimate overnight
  / half-day / Muhurat reopen gap can NEVER false-trip;
* a skipped bar's close does NOT advance ``prev_close`` — the next bar is judged
  against the last KEPT good close, so a lone spike cannot cascade-flag its
  healthy successor;
* exactly **one WARN per session** (emitted at the session boundary / stream
  end, carrying the session's total skipped count + the first offender) — never
  a per-bar log storm;
* an **env kill switch**, ``BRUTEX_BAD_TICK_SCREEN`` (default **ON**;
  ``0``/``false``/``no``/``off`` disables), so an operator can turn the screen
  off for a forensic replay of raw stored bytes.

O(1)/bar: one multiply-compare per bar plus O(1) session state (no history, no
allocation on the keep path).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from datetime import date

_LOG = structlog.get_logger(__name__)

#: The conservative bounded multiple: a bar whose close is ``>= 4x`` OR ``<= 1/4x``
#: the previous IN-SESSION close is a bad tick (see the module docstring for the
#: circuit-breaker-grounded justification).
BAD_TICK_MAX_RATIO: Final[float] = 4.0

#: Environment kill switch. Default ON; a falsy value disables the screen.
BAD_TICK_ENABLED_ENV: Final[str] = "BRUTEX_BAD_TICK_SCREEN"

#: One structlog event per session that had ≥ 1 skipped bad tick.
BAD_TICK_SKIP_EVENT: Final[str] = "feed.bad_tick_skipped"

#: Falsy literals that DISABLE the (default-ON) screen — the mirror of the
#: ``volume_keepalive`` kill-switch vocabulary (CLAUDE.md §11 COMMON pattern).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def bad_tick_screen_enabled(raw: str | None = None) -> bool:
    """Return whether the bad-tick screen is ENABLED (default ``True``).

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`BAD_TICK_ENABLED_ENV` from the environment.

    Returns
    -------
    bool
        ``False`` only for an explicit falsy literal (``0``/``false``/``no``/
        ``off``, case-insensitive, surrounding whitespace stripped); ``True``
        for anything else (unset, empty, ``1``, ``on`` …) — the screen is ON by
        default.
    """
    value = os.environ.get(BAD_TICK_ENABLED_ENV) if raw is None else raw
    return not (value is not None and value.strip().lower() in _FALSY)


def is_bad_tick(prev_close: float, close: float, *, max_ratio: float = BAD_TICK_MAX_RATIO) -> bool:
    """Return whether *close* is a bad tick versus the previous in-session *close*.

    A multiplicative test (no division — so a defensive ``prev_close <= 0`` can
    never divide-by-zero; at the engine read boundary both are already screened
    ``> 0`` by the positivity guard): the bar is bad iff
    ``close >= prev_close * max_ratio`` (an up-spike) OR
    ``close * max_ratio <= prev_close`` (a down-spike). The boundary
    (exactly ``max_ratio``x) is treated as bad (``>=`` / ``<=``).

    Parameters
    ----------
    prev_close
        The last KEPT good close in the same session (assumed ``> 0``).
    close
        This bar's close (assumed ``> 0``).
    max_ratio
        The bounded multiple; defaults to :data:`BAD_TICK_MAX_RATIO`.

    Returns
    -------
    bool
        ``True`` if *close* is outside ``[prev_close / max_ratio,
        prev_close * max_ratio]``.
    """
    return close >= prev_close * max_ratio or close * max_ratio <= prev_close


class BadTickScreen:
    """Stateful single-pass in-session bad-tick screen (O(1)/bar).

    Fed the bars of one Arrow table in chronological order; decides per bar
    whether to KEEP it, tracking the last kept in-session close and the
    per-session skip tally. Emits exactly one WARN per session that had a skip
    (at the session boundary and again at :meth:`finish`).

    Parameters
    ----------
    source
        Human-readable partition origin woven into the WARN (e.g.
        ``"NSE/INDEX/NSE-NIFTY/1minute"``), or ``None``.
    max_ratio
        The bounded multiple; defaults to :data:`BAD_TICK_MAX_RATIO`.
    log
        Injectable structlog logger (tests); ``None`` uses the module logger.
    """

    __slots__ = (
        "_first_offender",
        "_log",
        "_max_ratio",
        "_prev_close",
        "_session",
        "_skipped",
        "_source",
    )

    def __init__(
        self,
        *,
        source: str | None = None,
        max_ratio: float = BAD_TICK_MAX_RATIO,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._source = source
        self._max_ratio = max_ratio
        self._log = log if log is not None else _LOG
        self._session: date | None = None
        self._prev_close: float | None = None
        self._skipped: int = 0
        self._first_offender: tuple[float, float] | None = None

    def keep(self, *, ist_date: date, close: float) -> bool:
        """Return ``True`` to KEEP the bar, ``False`` to SKIP a bad tick.

        Parameters
        ----------
        ist_date
            The bar's IST session date (the session-grouping key).
        close
            The bar's close (assumed ``> 0`` — positivity screened upstream).
        """
        if ist_date != self._session:
            # New session: flush any warn owed for the session just left, then
            # reset the per-session state. The first bar of a session is always
            # kept (prev_close is None → no reopen-gap false trip).
            self._flush()
            self._session = ist_date
            self._prev_close = None
            self._skipped = 0
            self._first_offender = None

        if self._prev_close is not None and is_bad_tick(
            self._prev_close, close, max_ratio=self._max_ratio
        ):
            self._skipped += 1
            if self._first_offender is None:
                self._first_offender = (self._prev_close, close)
            # Deliberately do NOT advance _prev_close: the next bar is judged
            # against the last KEPT good close.
            return False

        self._prev_close = close
        return True

    def finish(self) -> None:
        """Flush the WARN owed for the final session (call once after the last bar)."""
        self._flush()

    def _flush(self) -> None:
        """Emit exactly one WARN if the current session skipped ≥ 1 bad tick."""
        if self._skipped > 0 and self._first_offender is not None:
            prev_close, close = self._first_offender
            self._log.warning(
                BAD_TICK_SKIP_EVENT,
                source=self._source,
                session=self._session.isoformat() if self._session is not None else None,
                skipped=self._skipped,
                max_ratio=self._max_ratio,
                first_prev_close=prev_close,
                first_close=close,
            )


__all__ = [
    "BAD_TICK_ENABLED_ENV",
    "BAD_TICK_MAX_RATIO",
    "BAD_TICK_SKIP_EVENT",
    "BadTickScreen",
    "bad_tick_screen_enabled",
    "is_bad_tick",
]
