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
  bar — at ANY of positions 1/2/3 — is itself dropped, and the post-head
  baseline SEEDS from the head MEDIAN (round-3 review — never blindly the
  LAST kept head bar). PRECISION (round-2 review): "poison" for a HEAD bar
  means ``>= max_ratio``x (or ``<= 1/max_ratio``x) the head MEDIAN, not the
  bar's immediate predecessor — so a head spike that is ``>= 4x`` its
  neighbor yet ``< 4x`` the median is KEPT, silently — but (round-3 review)
  it can no longer SEAT the post-head baseline: pre-round-3 the baseline was
  the LAST kept head bar, so an escaped spike at head POSITION 3 became the
  baseline and, over a flat/declining tail, cascade-dropped every healthy
  successor whose close was ``<= spike / max_ratio`` — ONE threshold-adjacent
  corrupt bar sufficed to collapse the session (loudly: skip WARN +
  majority-dropped WARN, but destructively); with the median seed the escaped
  spike is kept (Stage-1A still absorbs its ~4x close into the day's OHLC —
  the residual escape-band cost) yet its healthy successors are judged
  against the MEDIAN and all survive. That escape band,
  ``[max_ratio * min(other head closes), max_ratio * median)``, is exactly as
  wide as the head-bar dispersion (~1% of price on real minute data), so only
  a threshold-adjacent ~4.00-4.04x spike can ride it (the pre-fix pairwise
  #1616 screen dropped such a bar); the ``x10`` threat-model class sits far
  outside the band and is always caught;
* a genuine overnight / half-day / Muhurat reopen gap can still NEVER
  false-trip: consecutive real head minutes agree with each other (a real
  ``4x`` move across the first 3 minutes is impossible under the NSE circuit
  bands above), so the median is one of them and all are kept;
* **cross-session comparison stays FORBIDDEN** — the head is judged only
  against its OWN session's bars (a stock split in the ~750-name lake must
  never false-trip);
* after the head resolves, the running baseline STARTS at the head MEDIAN
  (round-3 review) and every later bar is judged against the last KEPT close
  exactly as before (a skipped bar never advances the baseline, so a lone
  mid-session spike cannot cascade-flag its healthy successor; a KEPT
  post-head bar is by construction ``< max_ratio``x the baseline, so — unlike
  the pre-round-3 escaped head spike — it cannot cascade-drop a successor
  sitting at the baseline's own level).

Honest limits (disclosed, not papered over):

* a **1-bar session is unconfirmable** — there is no second witness, so the bar
  is KEPT unscreened (never destroyed on zero evidence);
* a **2-bar session is unconfirmable** — the ratio test is symmetric, so an
  inconsistent pair identifies no offender; BOTH bars are KEPT (never destroy
  real data on 50/50 evidence) and the inconsistency is disclosed loudly via
  one :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN (real NSE sessions carry
  60-375 one-minute bars, so this is a synthetic-data corner);
* a **corrupt-majority head (2 of the first 3 bars corrupt) collapses the
  WHOLE session onto the corruption** — whenever ≥ 2 of the first 3 bars are
  corrupt (the SAME scaling, or two DIFFERENT scalings), the median IS a
  corrupt bar: the healthy head bar is dropped, the corrupt MEDIAN seeds
  the post-head baseline, and — because a skipped bar never advances the
  baseline — EVERY healthy successor in the session is cascade-dropped. The
  session collapses to the corrupt bar(s) (a differently-scaled corrupt pair
  collapses it to the ONE median poison bar), which Stage-1A absorbs as
  ``_today_ohlc`` and rolls into the NEXT day's pivot / PDH-PDL / Fib
  anchors — the same session-destroying shape as the poison-head bug this
  module fixes, re-armed at a 2-corrupt-head-bars threshold (round-3 note:
  with the median-seeded baseline this 2-corrupt threshold is now EXACT — a
  single kept escape-band head spike no longer re-arms the cascade, so a
  corrupt MEDIAN is the only remaining head-seeded collapse). For a corrupt
  run of length ≥ 2 STARTING at head position 2 this is a strict REGRESSION
  versus the #1616 screen (which baselined the healthy bar 1 and dropped the
  whole corrupt run); the recorded trade-off: the median vote moved the
  poison-baseline threshold from 1 corrupt head bar to 2, and the
  vendor-scaling threat model makes a corrupt RUN of opening minutes a
  realistic shape — majority evidence is the best a bounded in-session
  screen can do. The per-session skip WARN fires, and a session whose
  skipped count STRICTLY exceeds its kept count ADDITIONALLY fires one
  :data:`BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT` WARN — a coarse "most of
  this session was screened away — inspect the partition" signal, NOT proof
  the survivors are corrupt (round-2 review precision, both directions: on a
  short session with a HEALTHY median and two differently-corrupt head bars
  the screen works PERFECTLY — both corruptions dropped, the healthy bar
  kept — yet the WARN still fires; conversely the strict ``skipped > kept``
  gate CANNOT fire on the minimal corrupt-majority shapes, where the
  survivors ARE the corruption — a 3-bar session with a 2-of-3 corrupt head
  skips 1 < keeps 2, and a 4-bar 2v2 split is not a strict majority — so
  those emit only the ordinary skip WARN; real NSE sessions carry 60-375
  bars, so a real corrupt-majority head cascades far past the gate and DOES
  fire it). SCOPE (fixer round 3): the gate and its ``kept``/``skipped``
  fields are computed over the session's FINITE rows only — non-finite rows
  are filtered out BEFORE the ratio screen (see the non-finite screen below)
  and counted solely by their own :data:`NON_FINITE_SKIP_EVENT` WARN, so a
  mixed session dominated by non-finite skips can read ``kept >= skipped``
  here and never fire this WARN, and a fired WARN's counts understate the
  whole-session loss by the non-finite count (probe-verified: 2 non-finite +
  3 ratio-skipped of 5 rows fires with ``skipped=3 kept=0``). Mask unchanged,
  disclosure only;
* the **head verdict is bounded-forward-looking** — a session-head bar's KEEP
  verdict reads up to 2 FOLLOWING same-session bars (the median vote), so a
  load truncated INSIDE a session's first 3 bars can keep a poison head bar
  that the full load would drop (the pre-head-seed #1616 screen was strictly
  prefix-causal; this one is prefix-causal only at whole-session-head
  granularity). Clean-lake verdicts never flip (clean head bars agree with
  their median), production sweep windows are whole days, and any ONE fixed
  window stays fully deterministic; the sweep suffix-independence property
  gates consume bar lists / resamplers directly and do NOT route through
  ``arrow_table_to_bars``, so they deliberately do not certify this screen
  (disclosed + test-pinned, never claimed).

High/low arm (``DEC-BADTICK-HILO-SCREEN-001``)
----------------------------------------------

The close-only screen above was blind to a bar whose CLOSE is sane but whose
HIGH or LOW carries the scaling corruption (the product-audit-3 DATA-3b
reproducer: a HIGH x10 with a normal open/low/close passes ``is_bad_tick`` AND
``price_sanity``, yet ``TodayOHLC.absorb``'s running max/min captures the
scaled high — the NEXT day's PDH / pivot / Fib anchors read the poison). When
the caller supplies the optional keyword-only ``highs`` / ``lows`` sequences,
:meth:`BadTickScreen.screen_mask` ADDITIONALLY judges each bar's high and low
against the SAME session-scoped close-derived reference (head median for a
head bar, last-KEPT-close baseline after) under the SAME ``max_ratio``
bounded-multiple law (boundary = bad):

* an out-of-band high OR low skips the WHOLE bar (skip-and-count, never a
  clamped/edited field — the never-value-edit law is absolute);
* the baseline stays a CLOSE-derived quantity — highs/lows are *judged*, they
  never *become* the reference (a kept bar advances the baseline to its close
  exactly as before);
* a skipped bar (for ANY reason) never advances the baseline;
* per-bar reason precedence is pinned ``close`` → ``high`` → ``low`` (the
  first failing field names the skip; a both-scaled "fat range" bar whose high
  AND low are out-of-band counts once, as ``high``);
* the one-WARN-per-session law is unchanged — high/low-triggered skips FOLD
  into the same ``skipped`` counter, and the skip WARN gains the ADDITIVE
  ``skip_reasons`` (per-reason count map) + ``first_reason`` fields (existing
  fields keep their exact name + meaning; ``first_close`` remains the first
  offending BAR's close even when the trigger was its high/low);
* omitted ``highs``/``lows`` (the default) = the close-only screen,
  byte-identical to the pre-arm behavior;
* the 1-bar / 2-bar / escape-band honest limits carry over: a 1-bar session
  is kept unscreened, a 2-bar session is kept (its highs/lows included — no
  confirmed reference exists, so a 2-bar session's x10 high is a DISCLOSED
  escape; fixer round 3 — unlike the close arm's 2-bar mutual-inconsistency
  :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN, this hi/lo escape is
  runtime-SILENT, test-pinned: the closes agree, so no session signal exists;
  an INTRA-bar witness — a high ``>= max_ratio``x or a low ``<=
  1/max_ratio``x the bar's OWN close needs no session reference and would
  close the 1-bar/2-bar hi/lo escapes with no false-positive risk under the
  same circuit-band physics — is a NAMED optional follow-up, deliberately not
  folded into a fixer round), and the head escape band applies to the
  high/low judgment identically (a threshold-adjacent ~4.00-4.04x high
  inside ``[4·min-neighbor, 4·median)`` rides it). PRECISION (fixer round 3
  — the earlier "the x10 class is always caught" wording OVERCLAIMED for
  highs/lows): the hi/lo judgment compares the corrupt field against the
  SESSION reference (head median / last-kept close), never the bar's OWN
  level, so a x10-scaled HIGH is caught only when ``10·true_high >=
  4·reference`` — a bar whose own close is legitimately KEPT in the
  ``(0.25, 0.4)``x-reference band carries a x10 high of only ~2.5-4.0x the
  reference and is SERVED (the ``÷10`` LOW twin escapes on a kept close in
  ``(2.5, 4)``x); reproduced: closes ``[100, 100, 100, 30, 30]`` with a
  ``300`` high on the first 30-close bar — all bars kept, no WARN. The
  window applies at head (median reference) AND post-head (baseline
  reference) — it is NOT bounded by the escape-band bracket above.
  Reachability is adversarial-synthetic only: it needs a KEPT ``>= 60%``
  one-bar move, impossible on real index minute data under the NSE circuit
  bands (the CLOSE arm's x10-always-caught claim stands — a x10 close vs a
  healthy reference is ``>= 4``x by construction);
* ONE head-seed invariant does NOT carry over: the ``DEC-BADTICK-HEAD-SEED-001``
  "a session can never be emptied by its own head screen" guarantee is
  CLOSE-vote-ONLY — the hi/lo arm additionally judges the median-close bar's
  OWN high/low, so a ≤3-bar session whose EVERY head bar carries an
  out-of-band high/low is fully EMPTIED (kept=0; loud, never silent: the
  skip WARN's ``skip_reasons`` breakdown + the majority-dropped WARN with
  ``kept=0`` both fire, and each dropped bar independently carries a field
  ``>= max_ratio``x / ``<= 1/max_ratio``x the head close-median). A median
  bar dropped for its high/low still seeds the post-head baseline with its
  CLOSE (the baseline-stays-close-derived law);
* SANCTIONED CHANGE (documented, never silent — the ``-inf`` raise→skip
  precedent below): a MIS-ORDERED stored bar whose corrupt field is ALSO
  ratio-out-of-band versus the session reference (e.g. a x10-scaled LOW
  above its own high — the typical hi/lo-corrupt cause of mis-ordering) is
  now SKIPPED by this arm BEFORE the ``CandleBar`` constructor runs, so the
  ``DEC-STORED-DATA-VALIDATION-001`` ordering ``ValueError`` (the loud
  whole-table refusal) no longer fires for that class — a disclosed
  loud→quiet narrowing. Bounded: a mis-ordered bar whose fields all stay
  IN-band still raises the ordering ValueError, and the kill switch OFF
  restores the raise for EVERY mis-ordered bar (both arms test-pinned).

Non-finite screen (``DEC-READ-BOUNDARY-FINITE-001``)
----------------------------------------------------

:func:`non_finite_keep_mask` screens ``inf`` / ``-inf`` / ``nan`` OHLC values
at the same read boundary: a bar with ANY non-finite OHLC field is
skipped-and-counted (never served, never edited) with one
:data:`NON_FINITE_SKIP_EVENT` WARN per affected session. A non-finite stored
price is corrupt DATA with certainty (no threshold, no false-positive risk),
but NaN COMPARES FALSE — pre-screen, a NaN close sailed past the positivity
raise (``nan <= 0`` is ``False``), poisoned this module's ratio comparisons
(every ``is_bad_tick`` against a NaN reference is ``False`` → everything
kept), and reached ``Decimal("NaN")`` downstream. The screen must therefore
run BEFORE both the positivity raise and the bad-tick ratio screen, and the
ratio screen must never see a non-finite row (the caller filters them out —
see :func:`brutex.feed.replay.arrow_table_to_bars`). SANCTIONED CHANGE
(documented, not silent): ``-inf`` previously hit the positivity RAISE
(``-inf <= 0`` is ``True`` — a loud crash); it now rides the skip class with
the other non-finite values — a non-finite bar is corrupt DATA, same class as
NaN/+inf, and a skip-and-count with a WARN is the graceful per-partition
surface (the positivity raise remains reserved for FINITE non-positive
prices). KILL SWITCH: SHARED with the bad-tick screen
(``BRUTEX_BAD_TICK_SCREEN`` — the caller gates both behind ONE
:func:`bad_tick_screen_enabled` check), deliberately NOT a second env knob:
the switch's contract is "OFF = the exact pre-#1616 raw read path for a
forensic replay of raw stored bytes", and a separate finite-only switch would
mint a mixed mode with no operator story. OFF therefore restores the
pre-#1616 behavior for non-finite values too — measured per field: ``-inf``
raises at positivity again; NaN in ANY OHLC field raises an UNWRAPPED
``decimal.InvalidOperation`` out of ``CandleBar`` validation (a crash, NOT a
raw flow — and not even the wrapped corrupt-bar ValueError naming the
partition); ``+inf`` in open/low/close raises the wrapped corrupt-bar
ordering ValueError; ONLY ``+inf``-in-HIGH genuinely flows raw. The OFF
path is byte-identical to the pre-screen tree case-for-case (the
pre-existing envelope, unchanged — disclosed precisely).

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

O(1)/bar: one multiply-compare per bar (three with the high/low arm) plus O(1)
session state (a fixed ≤ 3-element head buffer, no history, no allocation on
the keep path beyond the output mask).
"""

from __future__ import annotations

import math
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

#: One structlog event per session whose SKIPPED count STRICTLY exceeds its
#: KEPT count — a coarse "most of this session was screened away — inspect the
#: partition" disclosure (module docstring, honest limits). It usually marks
#: the corrupt-majority-head out-vote collapse, but it is NOT proof the
#: survivors are corrupt (a healthy-median short session with two
#: differently-corrupt head bars trips it after a PERFECT screen), and the
#: minimal corrupt-majority shapes (3-bar 2-of-3 corrupt; 4-bar 2v2) sit at or
#: below the strict-majority gate and emit only the ordinary skip WARN.
#: FINITE-subset scoped (fixer round 3): non-finite rows never reach the
#: ratio screen, so they count in neither ``kept`` nor ``skipped`` here —
#: their loss is reported by :data:`NON_FINITE_SKIP_EVENT` alone.
#: Disclosure only — the keep mask is unchanged; remediation is a re-pull /
#: lake-heal of the named partition.
BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT: Final[str] = "feed.bad_tick_session_majority_dropped"

#: One structlog event per session that had ≥ 1 bar skipped for a NON-FINITE
#: (``inf`` / ``-inf`` / ``nan``) OHLC field (``DEC-READ-BOUNDARY-FINITE-001``).
#: A DISTINCT event name (not additive fields on :data:`BAD_TICK_SKIP_EVENT`):
#: a non-finite price is a different corruption class (certain, not heuristic)
#: with a different remediation story (the partition holds garbage bytes, not
#: a plausible-but-scaled print).
NON_FINITE_SKIP_EVENT: Final[str] = "feed.non_finite_bar_skipped"

#: The OHLC field names :func:`non_finite_keep_mask` screens, in the pinned
#: reporting order (the first non-finite field in this order names the skip).
_OHLC_FIELDS: Final[tuple[str, str, str, str]] = ("open", "high", "low", "close")

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
        post-head bar (the running baseline, SEEDED at the head-window median
        — round-3 review), or the head-window median for a head bar (assumed
        ``> 0``).
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


def non_finite_keep_mask(
    ist_dates: Sequence[date],
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    source: str | None = None,
    log: structlog.stdlib.BoundLogger | None = None,
) -> list[bool]:
    """Return the per-row KEEP mask screening non-finite OHLC values.

    ``DEC-READ-BOUNDARY-FINITE-001``: a bar with ANY non-finite (``inf`` /
    ``-inf`` / ``nan``) OHLC field is corrupt DATA with certainty and is
    skipped-and-counted (never served, never value-edited). NaN COMPARES
    FALSE, so without this screen a NaN close sails past the positivity raise
    AND poisons the ratio screen's baseline — the caller MUST run this mask
    FIRST (before the positivity raise, before :meth:`BadTickScreen.screen_mask`)
    and must never feed a non-finite row into the ratio screen (module
    docstring). One :data:`NON_FINITE_SKIP_EVENT` WARN per affected session,
    emitted at the session boundary / stream end (never a per-bar storm),
    naming the session's skip count + the first offending field/value.

    Parameters
    ----------
    ist_dates
        Each row's IST session date (the session-grouping key for the
        one-WARN-per-session law), 1:1 with the OHLC sequences.
    opens, highs, lows, closes
        Each row's OHLC fields (any float, including non-finite).
    source
        Human-readable partition origin woven into the WARN, or ``None``.
    log
        Injectable structlog logger (tests); ``None`` uses the module logger.

    Returns
    -------
    list[bool]
        ``True`` = the row's OHLC is fully finite (keep), ``False`` = skip.
        Same length and order as the inputs.

    Raises
    ------
    ValueError
        If the five input sequences differ in length.
    """
    n = len(closes)
    if not (len(ist_dates) == len(opens) == len(highs) == len(lows) == n):
        raise ValueError(
            f"ist_dates/opens/highs/lows/closes length mismatch: "
            f"{len(ist_dates)}/{len(opens)}/{len(highs)}/{len(lows)}/{n} — "
            f"the non-finite screen needs one session date per OHLC row"
        )
    logger = log if log is not None else _LOG
    keep = [True] * n

    session: date | None = None
    skipped = 0
    first_offender: tuple[str, float] | None = None

    def _flush() -> None:
        nonlocal skipped, first_offender
        if skipped > 0 and first_offender is not None:
            field, value = first_offender
            logger.warning(
                NON_FINITE_SKIP_EVENT,
                source=source,
                session=session.isoformat() if session is not None else None,
                skipped=skipped,
                first_field=field,
                # repr string: inf/nan are not JSON-serializable numbers — the
                # WARN must never crash a structured-log renderer.
                first_value=repr(value),
            )
        skipped = 0
        first_offender = None

    for i in range(n):
        day = ist_dates[i]
        if day != session:
            _flush()
            session = day
        for field, value in zip(
            _OHLC_FIELDS, (opens[i], highs[i], lows[i], closes[i]), strict=True
        ):
            if not math.isfinite(value):
                keep[i] = False
                skipped += 1
                if first_offender is None:
                    first_offender = (field, value)
                break
    _flush()
    return keep


class BadTickScreen:
    """Single-pass in-session bad-tick screen over one Arrow table (O(1)/bar).

    :meth:`screen_mask` consumes the table's per-row IST session dates + closes
    in chronological order and returns a per-row KEEP mask. Per session it
    buffers the first up-to-:data:`HEAD_CONFIRM_BARS` bars, resolves them
    against their median close (the evidence-confirmed head seed,
    ``DEC-BADTICK-HEAD-SEED-001``), then judges every later bar against the
    last KEPT close — with the running baseline SEEDED at the head MEDIAN
    (round-3 review), so a kept escape-band head spike never seats it. With
    the optional keyword-only ``highs``/``lows`` supplied, each bar's high and
    low are ADDITIONALLY judged against the same close-derived reference
    (``DEC-BADTICK-HILO-SCREEN-001`` — module docstring); omitted = the
    close-only screen, byte-identical. Emits exactly one :data:`BAD_TICK_SKIP_EVENT` WARN per
    session that skipped ≥ 1 bar, one
    :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN per inconsistent-but-kept
    2-bar session (the disclosed unconfirmable-head limit), and one
    :data:`BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT` WARN per session whose
    skipped count STRICTLY exceeds its kept count (the coarse
    most-of-session-screened-away disclosure — module docstring, honest
    limits).

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

    def screen_mask(
        self,
        ist_dates: Sequence[date],
        closes: Sequence[float],
        *,
        highs: Sequence[float] | None = None,
        lows: Sequence[float] | None = None,
    ) -> list[bool]:
        """Return the per-row KEEP mask for one chronologically ordered table.

        Parameters
        ----------
        ist_dates
            Each row's IST session date (the session-grouping key), 1:1 with
            *closes*.
        closes
            Each row's close (assumed ``> 0`` and finite — positivity is
            screened at the engine read boundary before any kept row is
            served, and non-finite rows are filtered FIRST via
            :func:`non_finite_keep_mask` — a NaN compares false and would
            silently poison the head median / running baseline).
        highs, lows
            Optional keyword-only per-row high/low fields
            (``DEC-BADTICK-HILO-SCREEN-001``): when supplied (each
            independently), the bar's high/low are ADDITIONALLY judged
            against the SAME close-derived session reference under the SAME
            ``max_ratio`` law — an out-of-band high or low skips the WHOLE
            bar even when its close is sane (reason precedence
            ``close`` → ``high`` → ``low``, pinned). Omitted (the default) =
            the close-only screen, byte-identical to the pre-arm behavior.

        Returns
        -------
        list[bool]
            ``True`` = keep the row, ``False`` = skip a bad tick. Same length
            and order as the inputs.

        Raises
        ------
        ValueError
            If *ist_dates* / *highs* / *lows* and *closes* differ in length.
        """
        n = len(closes)
        if len(ist_dates) != n:
            raise ValueError(
                f"ist_dates/closes length mismatch: {len(ist_dates)} != {n} — "
                f"the screen needs one session date per close"
            )
        if highs is not None and len(highs) != n:
            raise ValueError(
                f"highs/closes length mismatch: {len(highs)} != {n} — "
                f"the high/low arm needs one high per close"
            )
        if lows is not None and len(lows) != n:
            raise ValueError(
                f"lows/closes length mismatch: {len(lows)} != {n} — "
                f"the high/low arm needs one low per close"
            )
        keep = [True] * n

        session: date | None = None
        head: list[int] = []  # pending (unjudged) head-bar row indices, ≤ 3
        baseline: float | None = None  # median-seeded, then last KEPT close
        session_rows = 0  # total rows seen in the current session
        skipped = 0
        skip_reasons: dict[str, int] = {}
        first_offender: tuple[float, float, str] | None = None

        def _skip_reason(reference: float, idx: int) -> str | None:
            """Name row *idx*'s out-of-band field vs *reference*, or ``None``.

            Reason precedence is PINNED ``close`` → ``high`` → ``low``
            (``DEC-BADTICK-HILO-SCREEN-001``): the first failing field names
            the skip, so a both-scaled "fat range" bar counts once as
            ``high``. The high/low judgment reuses the ONE symmetric
            :func:`is_bad_tick` law against the SAME close-derived reference
            — highs/lows are judged, they never become the baseline.
            """
            if is_bad_tick(reference, closes[idx], max_ratio=self._max_ratio):
                return "close"
            if highs is not None and is_bad_tick(reference, highs[idx], max_ratio=self._max_ratio):
                return "high"
            if lows is not None and is_bad_tick(reference, lows[idx], max_ratio=self._max_ratio):
                return "low"
            return None

        def _judge(reference: float, idx: int) -> None:
            """Judge row *idx* against *reference*; skip-and-count or advance."""
            nonlocal baseline, skipped, first_offender
            reason = _skip_reason(reference, idx)
            if reason is not None:
                keep[idx] = False
                skipped += 1
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                if first_offender is None:
                    first_offender = (reference, closes[idx], reason)
                # Deliberately do NOT advance the baseline: the next bar is
                # judged against the last KEPT good close (a bar skipped for
                # ANY reason — close, high or low — never advances it).
            else:
                baseline = closes[idx]

        def _resolve_head() -> None:
            """Vote the 3 buffered head bars against their median close.

            On the CLOSE vote the median bar itself always survives
            (``is_bad_tick(m, m)`` is ``False`` for any positive ``m``), so
            the CLOSE vote alone can never empty a session — but that
            guarantee is CLOSE-vote-ONLY: with the high/low arm supplied
            (``DEC-BADTICK-HILO-SCREEN-001`` — production always supplies
            it), ``_judge`` also tests the median bar's OWN high/low against
            the median close, so the median bar can itself be dropped and a
            ≤3-bar session whose every head bar carries an out-of-band
            high/low is fully emptied (module docstring, honest limits;
            test-pinned). The post-head baseline SEEDS from
            the head MEDIAN — the evidence-confirmed center — never blindly
            the LAST kept head bar (round-3 review): pre-round-3 a kept
            escape-band spike at head position 3 seated the baseline and
            cascade-dropped every healthy successor (module docstring). The
            median CLOSE seeds the baseline even when the median bar itself
            was dropped for its high/low (the baseline is a close-derived
            quantity, never a kept-bar identity).
            """
            nonlocal baseline
            median = sorted(closes[i] for i in head)[1]
            for i in head:
                _judge(median, i)
            baseline = median
            head.clear()

        def _flush() -> None:
            """Close the current session: unconfirmable-head + WARN bookkeeping."""
            nonlocal session_rows, skipped, first_offender
            if (
                # A 2-bar pending head (a 1-bar head has no witness at all →
                # silent keep; a 3-bar head already resolved via the median).
                len(head) == HEAD_CONFIRM_BARS - 1
                and is_bad_tick(closes[head[0]], closes[head[1]], max_ratio=self._max_ratio)
            ):
                # 2-bar session, mutually inconsistent: the symmetric ratio test
                # identifies no offender — keep BOTH (never destroy real data on
                # 50/50 evidence) and disclose loudly (module docstring). The
                # high/low arm deliberately does NOT extend into this
                # unconfirmable shape: with no confirmed reference a 2-bar
                # session's highs/lows are kept unscreened (disclosed limit).
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
                reference, close, reason = first_offender
                self._log.warning(
                    BAD_TICK_SKIP_EVENT,
                    source=self._source,
                    session=session.isoformat() if session is not None else None,
                    skipped=skipped,
                    max_ratio=self._max_ratio,
                    first_ref_close=reference,
                    first_close=close,
                    # ADDITIVE fields (DEC-BADTICK-HILO-SCREEN-001): the
                    # per-reason breakdown of `skipped` + the first offender's
                    # own trigger. Existing fields keep name + meaning —
                    # `first_close` stays the first offending BAR's close even
                    # when the trigger was its high/low.
                    skip_reasons=dict(skip_reasons),
                    first_reason=reason,
                )
                kept_in_session = session_rows - skipped
                if skipped > kept_in_session:
                    # Strict-majority disclosure (module docstring, honest
                    # limits): the screen destroyed MOST of the session. This
                    # usually marks a corrupt 2-of-3 head out-voting the median
                    # and cascade-dropping the healthy remainder — but it is a
                    # coarse "inspect the partition" signal, NOT proof the
                    # survivors are corrupt (a healthy-median short session
                    # with two differently-corrupt head bars trips it after a
                    # PERFECT screen), and the minimal corrupt-majority shapes
                    # (3-bar 2-of-3; 4-bar 2v2) sit at/below the strict gate
                    # and emit only the skip WARN above. Mask unchanged.
                    self._log.warning(
                        BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
                        source=self._source,
                        session=session.isoformat() if session is not None else None,
                        skipped=skipped,
                        kept=kept_in_session,
                        max_ratio=self._max_ratio,
                    )
            session_rows = 0
            skipped = 0
            skip_reasons.clear()
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
            session_rows += 1
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
    "BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT",
    "BAD_TICK_SKIP_EVENT",
    "HEAD_CONFIRM_BARS",
    "NON_FINITE_SKIP_EVENT",
    "BadTickScreen",
    "bad_tick_screen_enabled",
    "is_bad_tick",
    "non_finite_keep_mask",
]
