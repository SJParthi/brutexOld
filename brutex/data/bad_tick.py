"""Bounded bad-tick (spike / fat-finger / vendor-scaling) screen at the engine
read boundary (``DEC-SWEEP-DATA-EDGE-GUARDS-001`` + the head-seed fix
``DEC-BADTICK-HEAD-SEED-001`` + the high/low arm
``DEC-BADTICK-HILO-SCREEN-001``).

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

:data:`BAD_TICK_MAX_RATIO` = ``4.0`` (reject a close ``>= 4x`` OR ``<= 1/4x``
the in-session reference close) therefore sits ~2 orders of magnitude ABOVE the
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
  baseline SEEDS from the head MEDIAN — never blindly the LAST kept head bar.
  PRECISION: "poison" for a HEAD bar means ``>= max_ratio``x (or
  ``<= 1/max_ratio``x) the head MEDIAN, not the bar's immediate predecessor —
  so a head spike that is ``>= 4x`` its neighbor yet ``< 4x`` the median is
  KEPT, silently — but it can never SEAT the post-head baseline: with a
  last-kept-head-bar seed an escaped spike at head position 3 became the
  baseline and, over a flat/declining tail, cascade-dropped every healthy
  successor whose close was ``<= spike / max_ratio`` — ONE threshold-adjacent
  corrupt bar sufficed to collapse the session; with the median seed the
  escaped spike is kept (Stage-1A still absorbs its ~4x close into the day's
  OHLC — the residual escape-band cost) yet its healthy successors are judged
  against the MEDIAN and all survive. That escape band,
  ``[max_ratio * min(other head closes), max_ratio * median)``, is exactly as
  wide as the head-bar dispersion (~1% of price on real minute data), so only
  a threshold-adjacent ~4.00-4.04x spike can ride it (the pre-fix pairwise
  #1616 screen dropped such a bar); the ``x10`` threat-model class sits far
  outside the band and is always caught on the CLOSE arm;
* a genuine overnight / half-day / Muhurat reopen gap can still NEVER
  false-trip: consecutive real head minutes agree with each other (a real
  ``4x`` move across the first 3 minutes is impossible under the NSE circuit
  bands above), so the median is one of them and all are kept;
* **cross-session comparison stays FORBIDDEN** — the head is judged only
  against its OWN session's bars (a stock split in the ~750-name lake must
  never false-trip);
* after the head resolves, the running baseline STARTS at the head MEDIAN and
  every later bar is judged against the last KEPT close exactly as before (a
  skipped bar never advances the baseline, so a lone mid-session spike cannot
  cascade-flag its healthy successor; a KEPT post-head bar is by construction
  ``< max_ratio``x the baseline, so — unlike a last-kept-seeded escaped head
  spike — it cannot cascade-drop a successor sitting at the baseline's own
  level).

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
  corrupt bar: the healthy head bar is dropped, the corrupt MEDIAN seeds the
  post-head baseline, and — because a skipped bar never advances the
  baseline — EVERY healthy successor in the session is cascade-dropped. The
  session collapses to the corrupt bar(s) (a differently-scaled corrupt pair
  collapses it to the ONE median poison bar), which Stage-1A absorbs as
  ``_today_ohlc`` and rolls into the NEXT day's pivot / PDH-PDL / Fib
  anchors — the same session-destroying shape as the poison-head bug this
  module fixes, re-armed at a 2-corrupt-head-bars threshold (with the
  median-seeded baseline this 2-corrupt threshold is EXACT — a single kept
  escape-band head spike no longer re-arms the cascade, so a corrupt MEDIAN
  is the only remaining head-seeded collapse). For a corrupt run of length
  ≥ 2 STARTING at head position 2 this is a strict REGRESSION versus the
  #1616 screen (which baselined the healthy bar 1 and dropped the whole
  corrupt run); the recorded trade-off: the median vote moved the
  poison-baseline threshold from 1 corrupt head bar to 2, and the
  vendor-scaling threat model makes a corrupt RUN of opening minutes a
  realistic shape — majority evidence is the best a bounded in-session
  screen can do. The per-session skip WARN fires, and a session whose
  skipped count STRICTLY exceeds its kept count ADDITIONALLY fires one
  :data:`BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT` WARN — a coarse "most of
  this session was screened away — inspect the partition" signal, NOT proof
  the survivors are corrupt (on a short session with a HEALTHY median and
  two differently-corrupt head bars the screen works PERFECTLY — both
  corruptions dropped, the healthy bar kept — yet the WARN still fires;
  conversely the strict ``skipped > kept`` gate CANNOT fire on the minimal
  corrupt-majority shapes, where the survivors ARE the corruption — a 3-bar
  session with a 2-of-3 corrupt head skips 1 < keeps 2, and a 4-bar 2v2
  split is not a strict majority — so those emit only the ordinary skip
  WARN; real NSE sessions carry 60-375 bars, so a real corrupt-majority
  head cascades far past the gate and DOES fire it). SCOPE: the gate and
  its ``kept``/``skipped`` fields are computed over the session's
  FINITE-close rows only — a non-finite-close row is kept-unjudged (see the
  non-finite robustness law below) and counts in neither tally. Mask
  unchanged, disclosure only;
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
  (disclosed, never claimed).

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
  ``skip_reasons`` (per-reason count map, rendered in the pinned
  close→high→low precedence order) + ``first_reason`` fields (existing fields
  keep their exact meaning; ``first_close`` remains the first offending BAR's
  close even when the trigger was its high/low);
* omitted ``highs``/``lows`` (the default) = the close-only screen,
  byte-identical to the pre-arm behavior;
* the 1-bar / 2-bar / escape-band honest limits carry over: a 1-bar session
  is kept unscreened, a 2-bar session is kept (its highs/lows included — no
  confirmed reference exists, so a 2-bar session's x10 high is a DISCLOSED
  escape; unlike the close arm's 2-bar mutual-inconsistency
  :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN, this hi/lo escape is
  runtime-SILENT, test-pinned: the closes agree, so no session signal exists;
  an INTRA-bar witness — a high ``>= max_ratio``x or a low ``<=
  1/max_ratio``x the bar's OWN close — is a NAMED optional follow-up), and
  the head escape band applies to the high/low judgment identically.
  PRECISION (never overclaim for highs/lows): the hi/lo judgment compares the
  corrupt field against the SESSION reference (head median / last-kept
  close), never the bar's OWN level, so a x10-scaled HIGH is caught only when
  ``10·true_high >= 4·reference`` — a bar whose own close is legitimately
  KEPT in the ``(0.25, 0.4)``x-reference band carries a x10 high of only
  ~2.5-4.0x the reference and is SERVED (the ``÷10`` LOW twin escapes on a
  kept close in ``(2.5, 4)``x). Reachability is adversarial-synthetic only:
  it needs a KEPT ``>= 60%`` one-bar move, impossible on real index minute
  data under the NSE circuit bands (the CLOSE arm's x10-always-caught claim
  stands — a x10 close vs a healthy reference is ``>= 4``x by construction);
* ONE head-seed invariant does NOT carry over: the "a session can never be
  emptied by its own head screen" guarantee is CLOSE-vote-ONLY — the hi/lo
  arm additionally judges the median-close bar's OWN high/low, so an
  EXACTLY-3-bar session (the head-resolving shape — a 1-/2-bar session's
  head never resolves and is KEPT per the limits above) whose EVERY head bar
  carries an out-of-band high/low is fully EMPTIED (kept=0; loud, never
  silent: the skip WARN's ``skip_reasons`` breakdown + the majority-dropped
  WARN with ``kept=0`` both fire, and each dropped bar independently carries
  a field ``>= max_ratio``x / ``<= 1/max_ratio``x the head close-median). A
  median bar dropped for its high/low still seeds the post-head baseline
  with its CLOSE (the baseline-stays-close-derived law);
* SANCTIONED CHANGE (documented, never silent): a MIS-ORDERED stored bar
  whose corrupt field is ALSO ratio-out-of-band versus the session reference
  (e.g. a x10-scaled HIGH — the typical hi/lo-corrupt cause of mis-ordering)
  is now SKIPPED by this arm BEFORE the ``CandleBar`` constructor runs, so
  the ``DEC-STORED-DATA-VALIDATION-001`` ordering ``ValueError`` (the loud
  whole-table refusal) no longer fires for that class — a disclosed
  loud→quiet narrowing. Bounded: a mis-ordered bar whose fields all stay
  IN-band still raises the ordering ValueError, and the kill switch OFF
  restores the raise for EVERY mis-ordered bar (both arms test-pinned).

Non-finite robustness (the #1897 composition — NOT a skip screen)
-----------------------------------------------------------------

The branch-era non-finite SKIP screen (``DEC-READ-BOUNDARY-FINITE-001``) was
SUPERSEDED by the landed ``DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001`` (#1897)
before this family ported: at the ENGINE read boundary a non-finite OHLC field
RAISES the wrapped corrupt-bar ``ValueError`` (never a skip), and the frame
gates refuse via ``price_sanity``'s ``~is_finite`` arm. This module therefore
ships NO non-finite skip-and-count — but :meth:`BadTickScreen.screen_mask`
itself must stay non-finite-ROBUST, because ONE wired seam
(:func:`~brutex.data.tf_autoderive.read_lake_bars_frame`) runs NO price-sanity
raise before the screen: a row whose CLOSE is non-finite (NaN/±inf) is KEPT
UNJUDGED and EXCLUDED from every reference computation (the head buffer, the
median, the baseline, the session tallies) — NaN compares false against
everything, so letting it into the head median / baseline would silently blind
the screen for the rest of the session (the pre-port streaming screen's
documented envelope). A non-finite HIGH/LOW on a finite-close row is judged
by the ordinary :func:`is_bad_tick` law (NaN never trips — kept; ``±inf`` is
genuinely out-of-band — dropped).

Resample-input instrument-class scope (``DEC-BADTICK-RESAMPLE-INPUT-001``)
--------------------------------------------------------------------------

The :data:`BAD_TICK_MAX_RATIO` = 4.0 bound is justified by INDEX
circuit-breaker physics (above) — that justification extends to CASH single
stocks (the same 10/15/20% surveillance bands) and to FUTURES (a future
tracks its underlying tick-for-tick, so its one-minute returns obey the same
bands), but NOT to OPTION PREMIUMS: a deep-OTM / expiry-day option
legitimately moves ``> 4x`` in one minute (a clean ₹2 → ₹9 print is 4.5x —
reproduced DROPPED pre-gate, with the skipped-bar-never-advances-baseline law
then cascade-dropping the session's re-priced remainder). The RESAMPLE-INPUT
seams therefore gate the :func:`screen_frame_bad_ticks` call on the ONE
COMMON class predicate :func:`resample_screen_applies` — SPOT (``INDEX`` /
``CASH``) + FNO ``FUT`` screened, FNO ``CE`` / ``PE`` (and an UNDECODABLE
FNO symbol — cannot prove futures, never destroy real data on an unproven
class) NOT screened. Post-#1831 (options prebuild retired) CE/PE never reach
the prebuild / freshness seams in production, so the predicate there is
defense-in-depth; the DISPLAY call sites keep their existing kind-derived
contracts (:mod:`brutex.data.bar_screen`'s ``screen_rows`` warning + the
``db_view`` / ``movers_futures`` kind gates — no churn). The ENGINE READ
boundary (:func:`brutex.feed.replay.arrow_table_to_bars`) is NOT class-gated
— it serves only engine-scoped instruments (the 3 spot indices, all screened
classes). HONEST residual: a GENUINELY-corrupt option bar (a x10 vendor
scaling on a premium) in derived TFs is NOT screened — an option-aware
threshold needs premium-calibrated physics, the named follow-up
DEC-BADTICK-OPTION-THRESHOLD-001.

Decision-plane shape (the ONE law for both arms)
------------------------------------------------

:meth:`BadTickScreen.screen_mask` returns ``(keep_mask, warn_events)`` — the
SAME integers-only event coordinates the Rust kernel
(``rust/brutex-feed/src/scan.rs``, DEC-FEED-RUST-CORE-001) returns — and the
CONSUMER drains/emits them (:func:`emit_screen_warn`, anchored by
``emit_at_row`` per the replay drain law). The screen itself never logs, so
the Python oracle and the Rust kernel share ONE emission seam and can never
fork on WARN bytes (CLAUDE.md §11 COMMON; the #1948 ruling makes scan.rs the
single Rust home of this predicate — never a second Rust copy).

Contract (deliberately narrow, to never destroy real data):

* the comparison is **within the SAME IST session only**;
* a skipped bar's close does NOT advance the baseline — the next bar is judged
  against the last KEPT good close, so a lone spike cannot cascade-flag its
  healthy successor;
* exactly **one WARN per session per event class** (anchored at the session
  boundary / stream end, carrying the session's total skipped count + the
  first offender) — never a per-bar log storm;
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

from brutex.data.fno_symbol import FUTURE_TYPE, parse_fno_symbol
from brutex.data.session import ist_offset_duration

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import date

    import polars as pl

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
#: survivors are corrupt, and the minimal corrupt-majority shapes (3-bar
#: 2-of-3 corrupt; 4-bar 2v2) sit at or below the strict-majority gate and
#: emit only the ordinary skip WARN. FINITE-close-subset scoped: a
#: non-finite-close row is kept-unjudged and counts in neither tally.
#: Disclosure only — the keep mask is unchanged; remediation is a re-pull /
#: lake-heal of the named partition.
BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT: Final[str] = "feed.bad_tick_session_majority_dropped"

#: Falsy literals that DISABLE the (default-ON) screen — the mirror of the
#: ``volume_keepalive`` kill-switch vocabulary (CLAUDE.md §11 COMMON pattern).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The F&O lake segment directory name (``bars/<exch>/FNO/<groww_symbol>/…``)
#: — the coordinate :func:`resample_screen_applies` classes on.
_FNO_SEGMENT: Final[str] = "FNO"

# ── The decision-plane event vocabulary (mirrored EXACTLY by scan.rs) ────
#
# One warn event is a 9-int tuple
# ``(kind, emit_at_row, session_first_row, a, b, c, d, e, f)``:
#
# * ``kind`` — one of the three EVENT_KIND_* codes below;
# * ``emit_at_row`` — the row at whose processing the session flush fires (the
#   first row of the NEXT session — under the head vote NOT necessarily a kept
#   row), or ``== n_rows`` for the end-of-input flush;
# * ``session_first_row`` — the first row of the FLUSHED session (the consumer
#   derives the ISO session date from its OWN object at this index);
# * kind :data:`EVENT_KIND_SKIP`: ``a``=skipped count, ``b``=first_ref_row
#   (the row whose close was the first offender's reference — the head-median
#   row or the last-kept-close row; :data:`SEED_REF_ROW` when the reference
#   was a caller-supplied ``seed_prev_close``, Python-only — the kernel never
#   sees a seed), ``c``=first_skip_row, ``d``=first_reason (REASON_* code),
#   ``e``=high-reason skip count, ``f``=low-reason skip count (the close
#   count is ``a - e - f``);
# * kind :data:`EVENT_KIND_HEAD_UNCONFIRMED`: ``a``=first head row,
#   ``b``=second head row, ``c``/``d``/``e``/``f`` = 0;
# * kind :data:`EVENT_KIND_MAJORITY_DROPPED`: ``a``=skipped, ``b``=kept,
#   ``c``/``d``/``e``/``f`` = 0. Always DIRECTLY follows its session's
#   EVENT_KIND_SKIP event (same anchor + session).

#: Warn-event kind codes (field 0 of the 9-tuple).
EVENT_KIND_SKIP: Final[int] = 0
EVENT_KIND_HEAD_UNCONFIRMED: Final[int] = 1
EVENT_KIND_MAJORITY_DROPPED: Final[int] = 2

#: Per-bar skip-reason codes, in the PINNED precedence order close→high→low
#: (``DEC-BADTICK-HILO-SCREEN-001``).
REASON_CLOSE: Final[int] = 0
REASON_HIGH: Final[int] = 1
REASON_LOW: Final[int] = 2

#: Reason code → the WARN's reason token (index = REASON_* code).
SKIP_REASON_TOKENS: Final[tuple[str, str, str]] = ("close", "high", "low")

#: ``first_ref_row`` sentinel: the first offender's reference was the
#: caller-supplied ``seed_prev_close`` (the /db page-seed law — Python-only;
#: the Rust kernel has no seed and never emits it).
SEED_REF_ROW: Final[int] = -1

#: One decision-plane warn event (see the vocabulary block above).
ScreenWarnEvent = tuple[int, int, int, int, int, int, int, int, int]


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


def resample_screen_applies(segment: str, symbol: str) -> bool:
    """Whether the RESAMPLE-INPUT bad-tick screen applies to one lake coordinate.

    The ONE COMMON instrument-class gate (CLAUDE.md §11) of
    ``DEC-BADTICK-RESAMPLE-INPUT-001`` (module docstring, "instrument-class
    scope"): the 4.0x bound is INDEX-circuit-breaker calibrated, which extends
    to CASH stocks and to FUTURES (a future tracks its underlying) but NOT to
    option premiums (a deep-OTM / expiry-day option legitimately moves
    ``> 4x`` in one minute), so the two lake-read factory seams
    (:func:`~brutex.data.timeframe_materialize.make_lake_read_1m`,
    :func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window`) gate
    their :func:`screen_frame_bad_ticks` call on this predicate instead of
    screening every class. Post-#1831 the CE/PE flows are structurally absent
    from those seams in production, so this is defense-in-depth there.
    (:func:`~brutex.data.tf_autoderive.read_lake_bars_frame` needs no class
    gate — its ``screen_bad_ticks=True`` callers are the derived-freshness
    consumers, spot-only by construction: the doctor excludes CE/PE per
    DEC-OPTIONS-PREBUILD-RETIRE-001 and the sweep preflight walks the engine
    spot symbols.) The DISPLAY call sites keep their existing kind-derived
    contracts (no churn).
    Pure (no I/O — one string compare + one
    :func:`~brutex.data.fno_symbol.parse_fno_symbol` decode).

    Parameters
    ----------
    segment
        The lake partition's segment directory name (``INDEX`` / ``CASH`` /
        ``FNO`` — the ``bars/<exch>/<segment>/<symbol>`` layout).
    symbol
        The partition's symbol directory name — for ``FNO`` a contract
        ``groww_symbol`` (``NSE-NIFTY-30Sep25-24650-CE``), decoded via the
        canonical :func:`~brutex.data.fno_symbol.parse_fno_symbol` exactly as
        :func:`brutex.data.lake_heal._prebuild_kind_from_coords` classifies
        the TF-rebuild kind.

    Returns
    -------
    bool
        ``True`` for a non-``FNO`` (spot) coordinate and for an ``FNO``
        ``FUT`` contract — the screened classes. ``False`` for an ``FNO``
        ``CE`` / ``PE`` contract AND for an UNDECODABLE ``FNO`` symbol
        (cannot prove futures — the conservative identity read; never
        destroy real data on an unproven class, mirroring lake_heal's
        ``None`` = never-rebuild for the same shape).
    """
    if segment != _FNO_SEGMENT:
        return True  # SPOT (INDEX / CASH) — the core screened class.
    parts = parse_fno_symbol(symbol)
    return parts is not None and parts.instrument_type == FUTURE_TYPE


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
        post-head bar (the running baseline, SEEDED at the head-window
        median), or the head-window median for a head bar (assumed ``> 0``).
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


def emit_bad_tick_warn(
    *,
    source: str | None,
    session_iso: str | None,
    skipped: int,
    max_ratio: float,
    first_ref_close: float,
    first_close: float,
    skip_reasons: dict[str, int],
    first_reason: str,
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Emit the ONE per-session :data:`BAD_TICK_SKIP_EVENT` WARN record.

    The SINGLE emission seam shared by the frame drains and the Rust-kernel
    replay in :mod:`brutex.feed.replay` (``DEC-FEED-RUST-CORE-001``), so the
    event name + field names/values can never fork between the two arms
    (CLAUDE.md §11 COMMON). Unconditional — the callers own the
    "skipped >= 1" guard; *log* is the injectable test seam (``None`` uses
    the module logger). ``first_prev_close`` was RENAMED ``first_ref_close``
    by ``DEC-BADTICK-HEAD-SEED-001`` — a head skip's reference is the head
    MEDIAN, not a previous close — and ``DEC-BADTICK-HILO-SCREEN-001`` added
    the ADDITIVE ``skip_reasons`` (per-reason count map, rendered in the
    pinned close→high→low precedence order) + ``first_reason`` fields.
    """
    (log if log is not None else _LOG).warning(
        BAD_TICK_SKIP_EVENT,
        source=source,
        session=session_iso,
        skipped=skipped,
        max_ratio=max_ratio,
        first_ref_close=first_ref_close,
        first_close=first_close,
        skip_reasons=skip_reasons,
        first_reason=first_reason,
    )


def emit_head_unconfirmed_warn(
    *,
    source: str | None,
    session_iso: str | None,
    first_close: float,
    second_close: float,
    max_ratio: float,
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Emit the ONE per-session :data:`BAD_TICK_HEAD_UNCONFIRMED_EVENT` WARN.

    A 2-bar session whose closes are mutually inconsistent (ratio ≥ the
    bound): the symmetric ratio test identifies no offender, so BOTH bars are
    KEPT and the inconsistency is disclosed loudly
    (``DEC-BADTICK-HEAD-SEED-001`` honest limits).
    """
    (log if log is not None else _LOG).warning(
        BAD_TICK_HEAD_UNCONFIRMED_EVENT,
        source=source,
        session=session_iso,
        first_close=first_close,
        second_close=second_close,
        max_ratio=max_ratio,
    )


def emit_majority_dropped_warn(
    *,
    source: str | None,
    session_iso: str | None,
    skipped: int,
    kept: int,
    max_ratio: float,
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Emit the ONE per-session :data:`BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT` WARN.

    Observe-only (the mask is unchanged): the session's skipped count
    STRICTLY exceeded its kept count — inspect the partition
    (``DEC-BADTICK-HEAD-SEED-001`` honest limits).
    """
    (log if log is not None else _LOG).warning(
        BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
        source=source,
        session=session_iso,
        skipped=skipped,
        kept=kept,
        max_ratio=max_ratio,
    )


def emit_screen_warn(
    event: ScreenWarnEvent,
    *,
    source: str | None,
    session_iso: str | None,
    closes: Sequence[float],
    max_ratio: float,
    seed_prev_close: float | None = None,
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Emit ONE decision-plane warn event through the shared per-kind seams.

    The consumer half of the :meth:`BadTickScreen.screen_mask` /
    ``scan.rs::scan_bars`` decision plane: the event carries integer row
    coordinates only; THIS function resolves them against the caller's own
    *closes* objects (so the logged floats are the caller's — the Rust replay
    logs Python's own float objects, the structural bit-identity law) and the
    caller-derived *session_iso*.

    Parameters
    ----------
    event
        The 9-int warn event (see the module's vocabulary block).
    source, session_iso, max_ratio, log
        Woven verbatim into the emitted record.
    closes
        The SAME per-row closes the screen consumed (row-coordinate space of
        the event).
    seed_prev_close
        The caller-supplied first-session seed, REQUIRED when the event's
        ``first_ref_row`` is :data:`SEED_REF_ROW` (the /db page-seed law);
        ignored otherwise.

    Raises
    ------
    ValueError
        On a :data:`SEED_REF_ROW` reference with no *seed_prev_close* — a
        caller contract violation (a negative index must never silently wrap
        into ``closes``).
    """
    kind = event[0]
    if kind == EVENT_KIND_SKIP:
        _, _, _, skipped, ref_row, skip_row, reason, n_high, n_low = event
        if ref_row == SEED_REF_ROW:
            if seed_prev_close is None:
                raise ValueError(
                    "emit_screen_warn: first_ref_row is SEED_REF_ROW but no "
                    "seed_prev_close was supplied — the caller that seeded the "
                    "screen must pass the same seed to the emitter"
                )
            first_ref_close = seed_prev_close
        else:
            first_ref_close = closes[ref_row]
        n_close = skipped - n_high - n_low
        skip_reasons: dict[str, int] = {}
        # Rendered in the PINNED precedence order (close → high → low), only
        # reasons with a non-zero count present — deterministic on both arms
        # (a first-occurrence insertion order cannot be reconstructed from
        # the kernel's integer counts; dict EQUALITY is order-insensitive).
        if n_close:
            skip_reasons["close"] = n_close
        if n_high:
            skip_reasons["high"] = n_high
        if n_low:
            skip_reasons["low"] = n_low
        emit_bad_tick_warn(
            source=source,
            session_iso=session_iso,
            skipped=skipped,
            max_ratio=max_ratio,
            first_ref_close=first_ref_close,
            first_close=closes[skip_row],
            skip_reasons=skip_reasons,
            first_reason=SKIP_REASON_TOKENS[reason],
            log=log,
        )
    elif kind == EVENT_KIND_HEAD_UNCONFIRMED:
        emit_head_unconfirmed_warn(
            source=source,
            session_iso=session_iso,
            first_close=closes[event[3]],
            second_close=closes[event[4]],
            max_ratio=max_ratio,
            log=log,
        )
    else:
        emit_majority_dropped_warn(
            source=source,
            session_iso=session_iso,
            skipped=event[3],
            kept=event[4],
            max_ratio=max_ratio,
            log=log,
        )


class BadTickScreen:
    """Whole-table in-session bad-tick screen over one bar table (O(1)/bar).

    :meth:`screen_mask` consumes the table's per-row IST session dates + closes
    in chronological order and returns the per-row KEEP mask PLUS the owed
    warn events as integer coordinates (the decision-plane shape shared with
    the Rust kernel — the screen itself never logs; see
    :func:`emit_screen_warn` / :meth:`emit_events`). Per session it buffers
    the first up-to-:data:`HEAD_CONFIRM_BARS` bars, resolves them against
    their median close (the evidence-confirmed head seed,
    ``DEC-BADTICK-HEAD-SEED-001``), then judges every later bar against the
    last KEPT close — with the running baseline SEEDED at the head MEDIAN, so
    a kept escape-band head spike never seats it. With the optional
    keyword-only ``highs``/``lows`` supplied, each bar's high and low are
    ADDITIONALLY judged against the same close-derived reference
    (``DEC-BADTICK-HILO-SCREEN-001`` — module docstring); omitted = the
    close-only screen, byte-identical.

    The instance carries only CONFIGURATION (``source`` / ``max_ratio`` /
    ``log`` — consumed by :meth:`emit_events`); all per-session state is
    local to :meth:`screen_mask`, so one instance may screen multiple tables.

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
        seed_prev_close: float | None = None,
    ) -> tuple[list[bool], list[ScreenWarnEvent]]:
        """Return the per-row KEEP mask + warn events for one ordered table.

        Parameters
        ----------
        ist_dates
            Each row's IST session date (the session-grouping key), 1:1 with
            *closes*.
        closes
            Each row's close (assumed ``> 0`` at the engine read boundary —
            positivity is screened there before any kept row is served). A
            NON-FINITE close is tolerated robustly: the row is KEPT UNJUDGED
            and excluded from every reference computation (module docstring,
            non-finite robustness).
        highs, lows
            Optional keyword-only per-row high/low fields
            (``DEC-BADTICK-HILO-SCREEN-001``): when supplied (each
            independently), the bar's high/low are ADDITIONALLY judged
            against the SAME close-derived session reference under the SAME
            ``max_ratio`` law — an out-of-band high or low skips the WHOLE
            bar even when its close is sane (reason precedence
            ``close`` → ``high`` → ``low``, pinned). Omitted (the default) =
            the close-only screen, byte-identical to the pre-arm behavior.
        seed_prev_close
            Optional confirmed in-session close IMMEDIATELY preceding the
            FIRST session's first row (the ``/db`` page-seed law — the
            :mod:`brutex.data.bar_screen` ``screen_rows`` reader resolves it
            from a bounded prior-bar read): the first session's head is
            treated as ALREADY RESOLVED UPSTREAM — no head vote runs for it,
            the baseline starts at the seed. Later sessions head-vote
            normally. A skip whose reference is the seed carries
            :data:`SEED_REF_ROW` as its ``first_ref_row``.

        Returns
        -------
        tuple[list[bool], list[ScreenWarnEvent]]
            ``keep`` (``True`` = keep the row) + the owed warn events,
            ordered by ``emit_at_row`` (non-decreasing; a session's
            MAJORITY_DROPPED event directly follows its SKIP event at the
            same anchor).

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
        max_ratio = self._max_ratio
        keep = [True] * n
        events: list[ScreenWarnEvent] = []

        session: date | None = None
        session_first_row = 0
        head: list[int] = []  # pending (unjudged) head-bar row indices, ≤ 3
        baseline: float | None = None  # median-seeded, then last KEPT close
        baseline_row = SEED_REF_ROW  # row of the baseline value (-1 = seed)
        session_rows = 0  # finite-close rows seen in the current session
        skipped = 0
        n_high = 0
        n_low = 0
        first_offender: tuple[int, int, int] | None = None  # (ref_row, skip_row, reason)
        seed: float | None = seed_prev_close  # consumed by the FIRST session only

        def _skip_reason(reference: float, idx: int) -> int | None:
            """Name row *idx*'s out-of-band field vs *reference*, or ``None``.

            Reason precedence is PINNED ``close`` → ``high`` → ``low``
            (``DEC-BADTICK-HILO-SCREEN-001``): the first failing field names
            the skip, so a both-scaled "fat range" bar counts once as
            ``high``. The high/low judgment reuses the ONE symmetric
            :func:`is_bad_tick` law against the SAME close-derived reference
            — highs/lows are judged, they never become the baseline.
            """
            if is_bad_tick(reference, closes[idx], max_ratio=max_ratio):
                return REASON_CLOSE
            if highs is not None and is_bad_tick(reference, highs[idx], max_ratio=max_ratio):
                return REASON_HIGH
            if lows is not None and is_bad_tick(reference, lows[idx], max_ratio=max_ratio):
                return REASON_LOW
            return None

        def _judge(reference: float, ref_row: int, idx: int) -> None:
            """Judge row *idx* against *reference*; skip-and-count or advance."""
            nonlocal baseline, baseline_row, skipped, n_high, n_low, first_offender
            reason = _skip_reason(reference, idx)
            if reason is not None:
                keep[idx] = False
                skipped += 1
                if reason == REASON_HIGH:
                    n_high += 1
                elif reason == REASON_LOW:
                    n_low += 1
                if first_offender is None:
                    first_offender = (ref_row, idx, reason)
                # Deliberately do NOT advance the baseline: the next bar is
                # judged against the last KEPT good close (a bar skipped for
                # ANY reason — close, high or low — never advances it).
            else:
                baseline = closes[idx]
                baseline_row = idx

        def _resolve_head() -> None:
            """Vote the 3 buffered head bars against their median close.

            The median is picked by the ``(value, row)`` lexicographic sort of
            the three ``(close, row)`` pairs — deterministic under duplicate
            close values, and the EXACT rule ``scan.rs`` mirrors so the two
            arms name the same reference row. On the CLOSE vote the median
            bar itself always survives (``is_bad_tick(m, m)`` is ``False``
            for any positive ``m``), so the CLOSE vote alone can never empty
            a session — but with the high/low arm supplied, ``_judge`` also
            tests the median bar's OWN high/low against the median close, so
            an EXACTLY-3-bar session whose every head bar carries an
            out-of-band high/low is fully emptied (module docstring, honest
            limits; test-pinned). The post-head baseline SEEDS from the head
            MEDIAN — the evidence-confirmed center — never blindly the LAST
            kept head bar; the median CLOSE seeds the baseline even when the
            median bar itself was dropped for its high/low (the baseline is
            a close-derived quantity, never a kept-bar identity).
            """
            nonlocal baseline, baseline_row
            value, row = sorted((closes[i], i) for i in head)[1]
            for i in head:
                _judge(value, row, i)
            baseline = value
            baseline_row = row
            head.clear()

        def _flush(emit_at: int) -> None:
            """Close the current session: unconfirmable-head + WARN bookkeeping."""
            nonlocal session_rows, skipped, n_high, n_low, first_offender
            if (
                # A 2-bar pending head (a 1-bar head has no witness at all →
                # silent keep; a 3-bar head already resolved via the median).
                len(head) == HEAD_CONFIRM_BARS - 1
                and is_bad_tick(closes[head[0]], closes[head[1]], max_ratio=max_ratio)
            ):
                # 2-bar session, mutually inconsistent: the symmetric ratio
                # test identifies no offender — keep BOTH (never destroy real
                # data on 50/50 evidence) and disclose loudly. The high/low
                # arm deliberately does NOT extend into this unconfirmable
                # shape: with no confirmed reference a 2-bar session's
                # highs/lows are kept unscreened (disclosed limit).
                events.append(
                    (
                        EVENT_KIND_HEAD_UNCONFIRMED,
                        emit_at,
                        session_first_row,
                        head[0],
                        head[1],
                        0,
                        0,
                        0,
                        0,
                    )
                )
            head.clear()
            if skipped > 0 and first_offender is not None:
                ref_row, skip_row, reason = first_offender
                events.append(
                    (
                        EVENT_KIND_SKIP,
                        emit_at,
                        session_first_row,
                        skipped,
                        ref_row,
                        skip_row,
                        reason,
                        n_high,
                        n_low,
                    )
                )
                kept_in_session = session_rows - skipped
                if skipped > kept_in_session:
                    # Strict-majority disclosure (module docstring, honest
                    # limits): the screen destroyed MOST of the session —
                    # a coarse "inspect the partition" signal, NOT proof the
                    # survivors are corrupt. Mask unchanged.
                    events.append(
                        (
                            EVENT_KIND_MAJORITY_DROPPED,
                            emit_at,
                            session_first_row,
                            skipped,
                            kept_in_session,
                            0,
                            0,
                            0,
                            0,
                        )
                    )
            session_rows = 0
            skipped = 0
            n_high = 0
            n_low = 0
            first_offender = None

        for i in range(n):
            day = ist_dates[i]
            if day != session:
                # New session: flush the session just left, then reset. The
                # head buffer restarts EMPTY — cross-session comparison is
                # forbidden (a stock split must never false-trip).
                _flush(i)
                session = day
                session_first_row = i
                baseline = None
                baseline_row = SEED_REF_ROW
                if seed is not None:
                    # The /db page-seed law: the FIRST session's head was
                    # resolved UPSTREAM (a real in-session predecessor bar) —
                    # the baseline starts at the seed, no head vote runs.
                    baseline = seed
                    seed = None
            if not math.isfinite(closes[i]):
                # Non-finite robustness (module docstring): keep the row
                # UNJUDGED and exclude it from the head vote / baseline /
                # session tallies — a NaN reference compares false against
                # everything and would silently blind the screen.
                continue
            session_rows += 1
            if baseline is None:
                head.append(i)
                if len(head) == HEAD_CONFIRM_BARS:
                    _resolve_head()
                continue
            _judge(baseline, baseline_row, i)
        _flush(n)
        return keep, events

    def emit_events(
        self,
        events: Sequence[ScreenWarnEvent],
        *,
        session_dates: Sequence[date],
        closes: Sequence[float],
        seed_prev_close: float | None = None,
    ) -> None:
        """Drain ALL *events* immediately through the shared emission seams.

        The immediate-drain consumer for the frame seams (the replay boundary
        drains per-event with its own ``emit_at_row`` interleaving — see
        :func:`brutex.feed.replay.arrow_table_to_bars`). *session_dates* /
        *closes* are the SAME row-coordinate sequences :meth:`screen_mask`
        consumed; the instance's ``source``/``max_ratio``/``log`` config is
        woven in.
        """
        for event in events:
            emit_screen_warn(
                event,
                source=self._source,
                session_iso=session_dates[event[2]].isoformat(),
                closes=closes,
                max_ratio=self._max_ratio,
                seed_prev_close=seed_prev_close,
                log=self._log,
            )


def screen_frame_bad_ticks(
    frame: pl.DataFrame,
    *,
    ts_column: str = "ts_utc",
    source: str | None = None,
    max_ratio: float = BAD_TICK_MAX_RATIO,
    log: structlog.stdlib.BoundLogger | None = None,
) -> pl.DataFrame:
    """Drop in-session bad-tick rows from a sorted 1-minute frame (frame form).

    The frame twin of the engine read boundary's :class:`BadTickScreen`
    (DEC-READ-GUARD-PARITY-001 + DEC-BADTICK-RESAMPLE-INPUT-001), for the
    resample-input seams
    (:func:`brutex.data.timeframe_materialize.make_lake_read_1m`,
    :func:`~brutex.data.tf_autoderive.read_lake_bars_frame`,
    :func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window`): it
    runs the SAME screen — head-median vote + hi/lo arm, the frame's
    ``high``/``low`` columns supplied — that
    :func:`brutex.feed.replay.arrow_table_to_bars` runs on the engine read,
    so ``engine(1m)`` and ``engine(<derived TF>)`` can never disagree about
    which bars are excluded: a scaled 1-minute bar the engine read drops (its
    close OR its high/low is out-of-band vs the session's close-derived
    reference) is dropped here too, and can no longer LAUNDER into a
    self-consistent-looking derived bucket (e.g. a ``x10`` high surviving
    into a 5m/15m/1day bucket whose own close is clean).

    Two-phase (the clean path is one vectorized pass, zero-copy; a frame with
    ANY candidate runs the sequential per-row Python arm over the WHOLE frame
    — O(rows), bounded, paid by poisoned frames only):

    1. Vectorized candidate trigger — a proven SUPERSET of the sequential
       screen's skips (the pre-port close-only adjacent trigger was FALSIFIED
       by both new arms):

       * ARM 1 (adjacent): any of {close, high, low} out-of-band vs the
         previous row's close within the session (the pre-port trigger,
         widened to the hi/lo fields);
       * ARM 2 (head-median): any of {close, high, low} out-of-band vs the
         session's HEAD MEDIAN (the median of the session's first up-to-3
         closes, vectorized per session) — covers the head vote AND the
         median-seeded first post-head judgment, where the sequential
         reference is the median, not the adjacent close (the escape-band
         seating probe ``[4, 4.05, 15.9, 16.5]``: 16.5 has no adjacent
         violation yet is out-of-band vs the median 4.05);
       * ARM 3 (non-finite): ANY non-finite close/high/low routes the frame
         to the sequential arm outright — the vectorized arms are
         finite-only sound (a NaN head close shifts the sequential head
         window off the first-3-ROWS median the vectorized arm computes).

       INDUCTION (zero candidates ⇒ the sequential screen keeps every row,
       all-finite case): sessions shorter than 3 rows are never judged; head
       rows are judged vs the median — arm 2; the first post-head row is
       judged vs the median-seeded baseline — arm 2; with zero skips so far
       every later row's baseline IS the previous row's close — arm 1. So a
       no-trigger frame returns the INPUT OBJECT unchanged (identity — the
       clean-lake byte-identity law), property-pinned by the
       identity⇔no-skip hypothesis gate.

    2. Sequential arm (candidate-bearing frames only):
       :meth:`BadTickScreen.screen_mask` decides WHICH rows drop and the
       instance drains its warn events immediately — exactly one
       :data:`BAD_TICK_SKIP_EVENT` per session with ≥ 1 skip (+ the
       head-unconfirmed / majority-dropped disclosures), one WARN per
       (symbol, poisoned session), UNCAPPED, deliberately the #1616
       replay-boundary shape (each WARN is actionable), not the capped-20
       idiom.

    Preconditions (documented, not re-checked): rows sorted ascending by
    *ts_column*; whole sessions per call (the head vote is
    bounded-forward-looking over each session's first up-to-3 bars — every
    wired seam reads a whole symbol history or a whole trading day, and an
    NSE session never crosses a UTC month boundary, so each session's head is
    intact in one call); ``close`` positive where a price gate runs upstream
    (the prebuild seam runs
    :func:`~brutex.data.price_sanity.validate_frame_price_sanity` first —
    the honest seam scope: the freshness/display seams run NO raise, so
    non-finite closes are tolerated robustly, kept-unjudged). Requires
    *ts_column* + ``high``/``low``/``close``; every other column (incl.
    ``open_interest`` on the F&O prebuild path) passes through the filter
    row-aligned and untouched. The function is PURE and switch-free — the
    ``BRUTEX_BAD_TICK_SCREEN`` kill switch is the CALLER's contract
    (:func:`bad_tick_screen_enabled` at the call site, the replay wiring's
    policy), and the instrument-class gate is likewise the caller's
    (:func:`resample_screen_applies` at the resample-input seams).

    Parameters
    ----------
    frame
        Sorted 1-minute frame (*ts_column* + ``high``/``low``/``close``
        required).
    ts_column
        Timestamp column name (UTC instants; naive = UTC by lake convention).
    source
        Human-readable origin woven into the WARN (the prebuild passes
        ``"prebuild:{exchange}/{segment}/{symbol}/1minute"``, the window
        reader ``"window:{...}"`` — boundary-distinct from the engine read's
        unprefixed form), or ``None``.
    max_ratio
        The bounded multiple; defaults to :data:`BAD_TICK_MAX_RATIO`.
    log
        Injectable structlog logger (tests); ``None`` uses the module logger.

    Returns
    -------
    pl.DataFrame
        The input object itself when nothing is dropped; otherwise a filtered
        frame with the bad-tick rows removed (never value-edited, never
        fabricated — Golden Rule #1).
    """
    import polars as pl

    if frame.height == 0:
        return frame
    ist_date = (pl.col(ts_column) + ist_offset_duration()).dt.date()

    def _out_of_band(col: pl.Expr, reference: pl.Expr) -> pl.Expr:
        # The EXACT multiplicative is_bad_tick mirror (>=/<= boundary — never
        # a division ratio, which is not ulp-identical at exactly-Rx).
        return (col >= reference * max_ratio) | (col * max_ratio <= reference)

    prev = pl.col("close").shift(1).over(ist_date)
    rank = pl.int_range(pl.len()).over(ist_date)
    head_median = (
        (pl.when(rank < HEAD_CONFIRM_BARS).then(pl.col("close")).otherwise(None))
        .median()
        .over(ist_date)
    )
    candidate = (
        _out_of_band(pl.col("close"), prev)
        | _out_of_band(pl.col("high"), prev)
        | _out_of_band(pl.col("low"), prev)
        | _out_of_band(pl.col("close"), head_median)
        | _out_of_band(pl.col("high"), head_median)
        | _out_of_band(pl.col("low"), head_median)
        | ~pl.col("close").is_finite()
        | ~pl.col("high").is_finite()
        | ~pl.col("low").is_finite()
    )
    if not bool(frame.select(candidate.fill_null(value=False).any()).item()):
        return frame
    dates = frame.select(ist_date.alias("d")).get_column("d").to_list()
    closes = frame.get_column("close").to_list()
    highs = frame.get_column("high").to_list()
    lows = frame.get_column("low").to_list()
    screen = BadTickScreen(source=source, max_ratio=max_ratio, log=log)
    keep, events = screen.screen_mask(dates, closes, highs=highs, lows=lows)
    screen.emit_events(events, session_dates=dates, closes=closes)
    if all(keep):
        return frame
    return frame.filter(pl.Series("keep", keep, dtype=pl.Boolean))


__all__ = [
    "BAD_TICK_ENABLED_ENV",
    "BAD_TICK_HEAD_UNCONFIRMED_EVENT",
    "BAD_TICK_MAX_RATIO",
    "BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT",
    "BAD_TICK_SKIP_EVENT",
    "EVENT_KIND_HEAD_UNCONFIRMED",
    "EVENT_KIND_MAJORITY_DROPPED",
    "EVENT_KIND_SKIP",
    "HEAD_CONFIRM_BARS",
    "REASON_CLOSE",
    "REASON_HIGH",
    "REASON_LOW",
    "SEED_REF_ROW",
    "SKIP_REASON_TOKENS",
    "BadTickScreen",
    "ScreenWarnEvent",
    "bad_tick_screen_enabled",
    "emit_bad_tick_warn",
    "emit_head_unconfirmed_warn",
    "emit_majority_dropped_warn",
    "emit_screen_warn",
    "is_bad_tick",
    "resample_screen_applies",
    "screen_frame_bad_ticks",
]
