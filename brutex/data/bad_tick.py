"""Bounded bad-tick (spike / fat-finger / vendor-scaling) screen at the engine
read boundary (``DEC-SWEEP-DATA-EDGE-GUARDS-001`` + the head-seed fix
``DEC-BADTICK-HEAD-SEED-001``).

The problem this closes (the D-MAJ-2 gap): the existing stored-data validation
(:mod:`brutex.data.price_sanity` + the :func:`arrow_table_to_bars` positivity
screen) rejects only NON-positive / mis-ORDERED bars. A *self-consistent* scaled
bar — every OHLC field multiplied (or divided) by the same factor, e.g. a NIFTY
minute printed ``2794`` instead of ``27940`` — is internally ordered and
strictly positive, so it passes every existing check and then silently flips
every price-relative condition bit, corrupts the running EMA / VWAP / swing
ring, and (via ``TodayOHLC.absorb``'s running max/min) poisons the NEXT day's
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
in-session reference close) therefore sits ~2 orders of magnitude ABOVE the
largest plausible real single-minute return, yet comfortably BELOW the ``x10``
scaling class (``10 > 4`` and ``0.1 < 0.25``): it catches the scaled-bar family
while being impossible to trip on real index / stock minute data. Regular-day
bars move ``<< 0.25x`` per minute, so every existing golden is byte-identical.

Session-head baseline — EVIDENCE-CONFIRMED (``DEC-BADTICK-HEAD-SEED-001``)
--------------------------------------------------------------------------

The original #1616 screen exempted the FIRST bar of each session and made it the
unconditional baseline. That was strictly WORSE than no screen for a corrupt
first bar: a ``>= 4x``-scaled poison HEAD became an unhealable baseline against
which every healthy successor was cascade-dropped — the session collapsed to the
single poison bar, which Stage-1A then absorbed as ``_today_ohlc`` and rolled
into the NEXT day's pivot / PDH-PDL / Fib anchor. The head baseline is now
seeded from EVIDENCE, not trust:

* the first up-to-:data:`HEAD_CONFIRM_BARS` (3) bars of each session are
  buffered (a fixed 3-element buffer — O(1)) and each is judged against the
  **median close of the head window** (a 2-of-3 majority vote): a poison head
  bar — at ANY of positions 1/2/3 — is itself dropped, and the surviving head
  bars seed the baseline;
* a genuine overnight / half-day / Muhurat reopen gap can still NEVER
  false-trip: consecutive real head minutes agree with each other (a real
  ``4x`` move across the first 3 minutes is impossible under the NSE circuit
  bands above), so the median is one of them and all are kept;
* **cross-session comparison stays FORBIDDEN** — the head is judged only
  against its OWN session's bars (a stock split in the ~750-name lake must
  never false-trip);
* after the head resolves, every later bar is judged against the last KEPT
  close exactly as before (a skipped bar never advances the baseline, so a
  lone mid-session spike cannot cascade-flag its healthy successor).

Honest limits (disclosed, not papered over):

* a **1-bar session is unconfirmable** — there is no second witness, so the bar
  is KEPT unscreened (never destroyed on zero evidence);
* a **2-bar session is unconfirmable** — the ratio test is symmetric, so an
  inconsistent pair identifies no offender; BOTH bars are KEPT (never destroy
  real data on 50/50 evidence) and the inconsistency is disclosed loudly via
  one :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN (real NSE sessions carry
  60-375 one-minute bars, so this is a synthetic-data corner);
* a **2-of-3 corrupt head beats the median** — if two of the first three bars
  carry the SAME scaling corruption, the median IS the corruption and the
  healthy head bar is dropped instead (majority evidence is the best a bounded
  in-session screen can do; the per-session WARN still fires).

Contract (deliberately narrow, to never destroy real data):

* the comparison is **within the SAME IST session only**;
* a skipped bar's close does NOT advance the baseline — the next bar is judged
  against the last KEPT good close, so a lone spike cannot cascade-flag its
  healthy successor;
* exactly **one WARN per session** that had ≥ 1 skipped bar (emitted at the
  session boundary / stream end, carrying the session's total skipped count +
  the first offender) — never a per-bar log storm;
* an **env kill switch**, ``BRUTEX_BAD_TICK_SCREEN`` (default **ON**;
  ``0``/``false``/``no``/``off`` disables), so an operator can turn the screen
  off for a forensic replay of raw stored bytes.

O(1)/bar: one multiply-compare per bar plus O(1) session state (a fixed
≤ 3-element head buffer, no history, no allocation on the keep path beyond the
output mask).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import date

_LOG = structlog.get_logger(__name__)

#: The conservative bounded multiple: a bar whose close is ``>= 4x`` OR ``<= 1/4x``
#: its in-session reference close is a bad tick (see the module docstring for the
#: circuit-breaker-grounded justification).
BAD_TICK_MAX_RATIO: Final[float] = 4.0

#: Session-head evidence window (``DEC-BADTICK-HEAD-SEED-001``): the first up-to-3
#: bars of each session vote via their median close, so a poison head bar is
#: itself dropped instead of becoming an unhealable cascade baseline.
HEAD_CONFIRM_BARS: Final[int] = 3

#: Environment kill switch. Default ON; a falsy value disables the screen.
BAD_TICK_ENABLED_ENV: Final[str] = "BRUTEX_BAD_TICK_SCREEN"

#: One structlog event per session that had ≥ 1 skipped bad tick.
BAD_TICK_SKIP_EVENT: Final[str] = "feed.bad_tick_skipped"

#: One structlog event per ≤ 2-bar session whose bars are MUTUALLY inconsistent
#: (ratio ≥ the bound) yet ALL KEPT — the honest unconfirmable-head disclosure
#: (no third witness → no offender identified → never destroy real data).
BAD_TICK_HEAD_UNCONFIRMED_EVENT: Final[str] = "feed.bad_tick_head_unconfirmed"

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
    """Return whether *close* is a bad tick versus the in-session reference *prev_close*.

    A multiplicative test (no division — so a defensive ``prev_close <= 0`` can
    never divide-by-zero; at the engine read boundary both are already screened
    ``> 0`` by the positivity guard): the bar is bad iff
    ``close >= prev_close * max_ratio`` (an up-spike) OR
    ``close * max_ratio <= prev_close`` (a down-spike). The boundary
    (exactly ``max_ratio``x) is treated as bad (``>=`` / ``<=``). The test is
    SYMMETRIC in the ratio sense — ``is_bad_tick(a, b) == is_bad_tick(b, a)``
    for positive inputs — which is why a lone inconsistent PAIR (a 2-bar
    session) identifies no offender (module docstring, honest limits).

    Parameters
    ----------
    prev_close
        The in-session reference close — the last KEPT good close for a
        post-head bar, or the head-window median for a head bar (assumed ``> 0``).
    close
        This bar's close (assumed ``> 0``).
    max_ratio
        The bounded multiple; defaults to :data:`BAD_TICK_MAX_RATIO`.

    Returns
    -------
    bool
        ``True`` if *close* is outside ``(prev_close / max_ratio,
        prev_close * max_ratio)``.
    """
    return close >= prev_close * max_ratio or close * max_ratio <= prev_close


class BadTickScreen:
    """Single-pass in-session bad-tick screen over one Arrow table (O(1)/bar).

    :meth:`screen_mask` consumes the table's per-row IST session dates + closes
    in chronological order and returns a per-row KEEP mask. Per session it
    buffers the first up-to-:data:`HEAD_CONFIRM_BARS` bars, resolves them
    against their median close (the evidence-confirmed head seed,
    ``DEC-BADTICK-HEAD-SEED-001``), then judges every later bar against the
    last KEPT close. Emits exactly one :data:`BAD_TICK_SKIP_EVENT` WARN per
    session that skipped ≥ 1 bar, plus one
    :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN per inconsistent-but-kept
    2-bar session (the disclosed unconfirmable-head limit).

    The instance carries only CONFIGURATION (``source`` / ``max_ratio`` /
    ``log``); all per-session state is local to :meth:`screen_mask`, so one
    instance may screen multiple tables.

    Parameters
    ----------
    source
        Human-readable partition origin woven into the WARNs (e.g.
        ``"NSE/INDEX/NSE-NIFTY/1minute"``), or ``None``.
    max_ratio
        The bounded multiple; defaults to :data:`BAD_TICK_MAX_RATIO`.
    log
        Injectable structlog logger (tests); ``None`` uses the module logger.
    """

    __slots__ = ("_log", "_max_ratio", "_source")

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

    def screen_mask(self, ist_dates: Sequence[date], closes: Sequence[float]) -> list[bool]:
        """Return the per-row KEEP mask for one chronologically ordered table.

        Parameters
        ----------
        ist_dates
            Each row's IST session date (the session-grouping key), 1:1 with
            *closes*.
        closes
            Each row's close (assumed ``> 0`` — positivity screened at the
            engine read boundary before any kept row is served).

        Returns
        -------
        list[bool]
            ``True`` = keep the row, ``False`` = skip a bad tick. Same length
            and order as the inputs.

        Raises
        ------
        ValueError
            If *ist_dates* and *closes* differ in length.
        """
        n = len(closes)
        if len(ist_dates) != n:
            raise ValueError(
                f"ist_dates/closes length mismatch: {len(ist_dates)} != {n} — "
                f"the screen needs one session date per close"
            )
        keep = [True] * n

        session: date | None = None
        head: list[int] = []  # pending (unjudged) head-bar row indices, ≤ 3
        baseline: float | None = None  # last KEPT close once the head resolved
        skipped = 0
        first_offender: tuple[float, float] | None = None

        def _judge(reference: float, idx: int) -> None:
            """Judge row *idx* against *reference*; skip-and-count or advance."""
            nonlocal baseline, skipped, first_offender
            close = closes[idx]
            if is_bad_tick(reference, close, max_ratio=self._max_ratio):
                keep[idx] = False
                skipped += 1
                if first_offender is None:
                    first_offender = (reference, close)
                # Deliberately do NOT advance the baseline: the next bar is
                # judged against the last KEPT good close.
            else:
                baseline = close

        def _resolve_head() -> None:
            """Vote the 3 buffered head bars against their median close.

            The median bar itself always survives (``is_bad_tick(m, m)`` is
            ``False`` for any positive ``m``), so a session can never be
            emptied by its own head screen; the baseline ends on the LAST kept
            head bar.
            """
            median = sorted(closes[i] for i in head)[1]
            for i in head:
                _judge(median, i)
            head.clear()

        def _flush() -> None:
            """Close the current session: unconfirmable-head + WARN bookkeeping."""
            nonlocal skipped, first_offender
            if (
                # A 2-bar pending head (a 1-bar head has no witness at all →
                # silent keep; a 3-bar head already resolved via the median).
                len(head) == HEAD_CONFIRM_BARS - 1
                and is_bad_tick(closes[head[0]], closes[head[1]], max_ratio=self._max_ratio)
            ):
                # 2-bar session, mutually inconsistent: the symmetric ratio test
                # identifies no offender — keep BOTH (never destroy real data on
                # 50/50 evidence) and disclose loudly (module docstring).
                self._log.warning(
                    BAD_TICK_HEAD_UNCONFIRMED_EVENT,
                    source=self._source,
                    session=session.isoformat() if session is not None else None,
                    first_close=closes[head[0]],
                    second_close=closes[head[1]],
                    max_ratio=self._max_ratio,
                )
            head.clear()
            if skipped > 0 and first_offender is not None:
                reference, close = first_offender
                self._log.warning(
                    BAD_TICK_SKIP_EVENT,
                    source=self._source,
                    session=session.isoformat() if session is not None else None,
                    skipped=skipped,
                    max_ratio=self._max_ratio,
                    first_ref_close=reference,
                    first_close=close,
                )
            skipped = 0
            first_offender = None

        for i in range(n):
            day = ist_dates[i]
            if day != session:
                # New session: flush the session just left, then reset. The
                # head buffer restarts EMPTY — cross-session comparison is
                # forbidden (a stock split must never false-trip).
                _flush()
                session = day
                baseline = None
            if baseline is None:
                head.append(i)
                if len(head) == HEAD_CONFIRM_BARS:
                    _resolve_head()
                continue
            _judge(baseline, i)
        _flush()
        return keep


__all__ = [
    "BAD_TICK_ENABLED_ENV",
    "BAD_TICK_HEAD_UNCONFIRMED_EVENT",
    "BAD_TICK_MAX_RATIO",
    "BAD_TICK_SKIP_EVENT",
    "HEAD_CONFIRM_BARS",
    "BadTickScreen",
    "bad_tick_screen_enabled",
    "is_bad_tick",
]
