"""Pre-settle TODAY minute-completeness gate — the pure classifier + guarded wiring.

DEC-PULL-TODAY-COMPLETENESS-GATE-001 (MIGRATION.md #1755). The Groww vendor serves
the CURRENT trading day's intraday series in a FORMING shape until its end-of-day
settlement lands (MIGRATION.md #837, DEC-GROWW-FORMING-BAR-PARSE-001: 6-element
rows — no ``open_interest`` slot, ``volume`` typically ``None`` — vs the settled
7-element history). The parse destroys the literal row length
(``parser.py`` pads ``open_interest=None``; ``candle_fields`` maps absent volume
→ ``0``), and the lake's keep-first merge law
(DEC-LAKE-CONFLICT-KEEPFIRST-001 — the stored bar wins on every same-timestamp
re-pull, values compared INCLUDING volume + open_interest) makes any stored
still-settling row PERMANENT: the settled re-pull is discarded as a
``BarConflict`` forever. So a post-close (≥15:30 IST) pull whose trailing window
covers TODAY must not ``sink.append`` today's bars — nor settle the window
``SUCCESS`` — until the fetched day passes the per-class completeness criteria.

This module is the ONE pure classifier + the ONE guarded ``observe_*`` wiring
BOTH ingest tails call (the :mod:`~brutex.data.partial_window_detect` /
:func:`~brutex.data.session.observe_all_filtered` template — CLAUDE.md §11
COMMON). The tails consult it after the session filter + the future-bar drop and
BEFORE ``sink.append`` + ``ledger.record``: on a covers-today window that FAILS,
the tail (a) WITHHOLDS today's IST-dated bars from the append (the write guard —
demote-only is provably insufficient because keep-first would freeze the
still-settling rows) and (b) demotes the window to a re-pullable ``FAILED`` via
the existing ``degraded=``/``degraded_reason=`` channel with the
:data:`TODAY_INCOMPLETE` tag — a **streak-EXEMPT** demotion
(:data:`~brutex.data.window_ledger.STREAK_EXEMPT_DEGRADED_REASONS`): "the vendor
has not settled TODAY yet" is TIME-transient evidence that expires at the
session boundary by construction, so it must never walk the window toward a
``SKIPPED`` settle the way the STRUCTURAL ALL_FILTERED/INTERIOR_GAP/
SUSPECTED_PARTIAL evidence (which repeats identically forever) legitimately does.

Per-class criteria (the binding table — DEC-PULL-TODAY-COMPLETENESS-GATE-001):

* ``INDEX_SPOT`` — distinct-minute count == ``session.duration_minutes`` AND the
  close-edge law :func:`~brutex.calendars.nse.session_last_bar_complete` (volume
  and OI carry ZERO signal on an index: ``drop_volume`` forces 0, OI is always
  ``None``). Indices print every session minute, so the count demand is safe.
  HONEST: a circuit-breaker trading-HALT day (market-wide halt → genuinely
  fewer session minutes) would count-demote a genuinely-complete day —
  self-healing (streak-exempt, and the next non-covers-today pull is inert so
  the day's bars land), disclosed (round-2 review).
* ``CASH_SPOT`` — shape only: INCOMPLETE iff bars exist AND every one has
  ``volume == 0`` (the #837 still-settling ``volume=None`` → stored ``0``
  shape). Count/last-bar deliberately NOT demanded — an illiquid microcap whose
  last trade printed 15:12 is a legit day (the #1031 re-pull-storm lesson).
  HONEST: a designed extension beyond the spec's literal "(iii) for FNO"
  wording — same bug class, same mechanism — and a HEURISTIC: a genuinely
  settled all-zero-volume cash day would false-demote (near-impossible — a
  settled cash candle exists because trades happened), costing one bounded
  streak-exempt same-evening deferral, never a loss or a SKIP.
* ``FUT`` — shape mandatory: INCOMPLETE iff ANY today bar has
  ``open_interest is None`` (ANY-bar, not all-bars: an all-bars rule would pass
  a progressively-settling mixed day and freeze its OI-null minutes) OR bars
  exist AND every one has ``volume == 0`` (a settled future with literally zero
  volume across every printed minute does not exist; per-bar ``volume > 0`` is
  NOT demanded — genuine zero-volume printed FUT minutes are REAL per
  DEC-FUTURES-EXEC-TRANSLATION-001). Count vs expected is OBSERVE-ONLY (one
  capped INFO — a far-serial-month future is legitimately sparse intraday, so a
  hard count demand would false-demote it every evening; per #837 a same-day
  truncated response carries the forming shape anyway, so the shape check owns
  the demotion).
* ``OPTION`` (CE/PE) — the OI-null shape only (zero-volume option minutes are
  routine; sparsity ≈ truncation, the #1031 stance — no count/last-bar checks).
* Zero today bars: ``INDEX_SPOT`` FAILS naturally via the count (indices always
  print; post-close zero is vendor lag — streak-exempt, heals at the nightly);
  every other class ABSTAINS/passes vacuously (``no_today_bars`` — a dead strike
  / dormant far-month contract / vendor-empty cash day legitimately prints
  nothing).
* A non-trading ``clamp_today`` (weekend/holiday) passes vacuously
  (``non_trading``); an out-of-holiday-coverage date (≥2027) ABSTAINS
  (``abstain_coverage`` — the unguarded :func:`~brutex.calendars.nse.get_session`
  FABRICATES a regular session out of coverage, and demanding 375 minutes of an
  unknown holiday would demote a legitimately-empty window forever).
* ``raw_bars == 0`` is structurally the EMPTY channel
  (:func:`~brutex.data.window_status.classify_window_status` ignores
  ``degraded`` at raw 0) — the wiring short-circuits it with observe-only
  visibility (:data:`TODAY_EMPTY_EVENT`), never a demotion attempt.

The covers-today predicate is STRICT equality
``window_end == clamp_today + 1 day`` where *clamp_today* is the SAME clock
instant's date the producer passed to
:func:`~brutex.adapters.ingest_common.clamp_window_end` (stamped on the frozen
window payloads) — the exact inverse of the clamp's post-15:30 arm, so at most
ONE covers-today window per series per pass exists structurally, and a
midnight-crossing drain is judged against the date that MINTED its end (never a
fresh tail-side clock read). The gate takes NO position at the 15:30:00
boundary — it inherits ``clamp_window_end``'s ``>=`` side by construction.

Honest envelope (Golden Rule #10): the gate judges what THIS fetch returned
(pure function of bars + window + calendar + clamp date — idempotent, Golden
Rule #7; never a lake read); an already-stored partial day under an
already-settled pre-fix window is neither detected nor repaired (the probe
runbook's remediation procedure; mechanised audit = the named follow-up
DEC-LAKE-AUDIT-SETTLING-ROWS-001). The OI-null / all-zero-volume checks are
parsed-bar INFERENCE of the destroyed 6-element shape — heuristics whose false
positives cost one bounded streak-exempt deferral, measured by the companion
probe (``docs/groww/probe/postclose_settle_probe.py``,
DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final

from brutex.calendars.nse import get_session, is_holiday_data_available, session_last_bar_complete
from brutex.data.models import CandleBar
from brutex.data.session import IST

__all__ = [
    "TODAY_COMPLETENESS_GATE_ENV",
    "TODAY_COUNT_SHORT_EVENT",
    "TODAY_EMPTY_EVENT",
    "TODAY_GATE_ABSTAIN_EVENT",
    "TODAY_GATE_ERROR_EVENT",
    "TODAY_INCOMPLETE",
    "TODAY_INCOMPLETE_EVENT",
    "TODAY_INCOMPLETE_SUPPRESSED_EVENT",
    "InstrumentClass",
    "TodayBarSplit",
    "TodayCompletenessVerdict",
    "classify_today_completeness",
    "observe_today_completeness",
    "reset_today_gate_log_state",
    "resolve_today_completeness_enabled",
    "split_today_bars",
]

#: The ``degraded_reason`` / ``last_error`` tag stamped on a demoted covers-today
#: window. Lives beside its detector (this module), mirroring
#: :data:`~brutex.data.session.ALL_FILTERED` /
#: :data:`~brutex.data.interior_gap_detect.INTERIOR_GAP`; the ledger imports it
#: back into :data:`~brutex.data.window_ledger.DEMOTED_SETTLE_REASONS` +
#: :data:`~brutex.data.window_ledger.STREAK_EXEMPT_DEGRADED_REASONS`.
TODAY_INCOMPLETE: Final[str] = "TODAY_INCOMPLETE"

#: Env kill switch name (fail-loud — the ``BRUTEX_MARKET_HOURS_GUARD`` template).
#: Unset / on-token ⇒ ENABLED (the default — protecting the lake from permanent
#: keep-first-frozen still-settling rows is the safe posture); off-token ⇒
#: DISABLED. Anything else raises at pull start, never mid-run.
TODAY_COMPLETENESS_GATE_ENV: Final[str] = "BRUTEX_TODAY_COMPLETENESS_GATE"

#: ENABLE tokens (case-insensitive, whitespace-stripped). ``""`` (unset) is ENABLED.
_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true", "yes"})

#: DISABLE tokens (the operator escape hatch). Anything else raises — junk is
#: never silently coerced (the DEC-RAM-BUDGETS-001 fail-loud env convention).
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})

#: The demotion WARNING — one per demoted covers-today window, capped
#: (:data:`_EVENT_CAP_PER_PULL` per pull; an FNO pre-settle round can demote
#: thousands of contract windows — the #1649a capped-warn convention).
TODAY_INCOMPLETE_EVENT: Final[str] = "ingest.window.today_incomplete"

#: The exactly-once-per-pull suppressed-summary WARNING flushed on the next
#: observation past the cap (monotone-approximate — it carries the counts AT
#: FLUSH time, observation #cap+1, so ``suppressed_count``/``total_incomplete``
#: are a FLOOR, not the pull's final total: a 3000-demotion FNO evening logs
#: "suppressed 1 of 21"; later demotions stay counted internally but unlogged.
#: The TRUE per-pull total is the durable record — the window ledger's
#: ``FAILED`` rows carrying :data:`TODAY_INCOMPLETE` (one per demoted window).
#: A true-total pull-END flush would need a post-drain seam at all four arm
#: sites — deliberately not built (this event is a heads-up, the ledger is the
#: count; disclosed in MIGRATION.md #1755 item (8), round-1 R3-MINOR-1).
TODAY_INCOMPLETE_SUPPRESSED_EVENT: Final[str] = "ingest.window.today_incomplete_suppressed"

#: FUT observe-only count shortfall INFO (never a demotion — ``demoted=False``).
TODAY_COUNT_SHORT_EVENT: Final[str] = "ingest.window.today_count_short"

#: Out-of-calendar-coverage abstain INFO — once per (pull, reason).
TODAY_GATE_ABSTAIN_EVENT: Final[str] = "ingest.window.today_gate_abstain"

#: Observe-only visibility WARNING for a covers-today INDEX/FUT window whose
#: fetch was raw-EMPTY — it rides the EXISTING EMPTY machinery (structurally
#: unreachable by ``degraded``), this event just makes the shape visible.
TODAY_EMPTY_EVENT: Final[str] = "ingest.window.today_empty"

#: The fail-open wrapper's error WARNING (uncapped — exceptional): a crashed
#: classifier degrades to the pre-gate exposure, never a demote-loop.
TODAY_GATE_ERROR_EVENT: Final[str] = "ingest.window.today_gate_error"

#: Per-pull cap on the capped events above (the #1649a convention: 20 + one
#: suppressed summary).
_EVENT_CAP_PER_PULL: Final[int] = 20

#: Event field names :func:`observe_today_completeness` stamps EXPLICITLY —
#: filtered out of the caller-supplied ``fields`` mapping so a caller passing
#: e.g. ``symbol`` (both tails' series field dicts carry it) can never collide
#: into a duplicate-kwarg ``TypeError`` inside the fail-open wrapper.
_WRAPPER_OWNED_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "symbol",
        "interval",
        "pull_id",
        "session_date",
        "instrument_class",
        "reason",
        "expected_minutes",
        "distinct_minutes",
        "last_bar_ist",
        "oi_null_count",
        "zero_volume_count",
        "withheld_bars",
        "demoted",
        "suppressed_count",
        "total_incomplete",
        "error",
    }
)

#: The sole fetched granularity the gate judges (CLAUDE.md §2 Rule 3 — 1-minute
#: is the ONLY vendor-fetched interval; the derived ``1day`` follows it).
_ONE_MINUTE: Final[str] = "1minute"

# ---------------------------------------------------------------------------
# Verdict reason vocabulary (machine tokens on TodayCompletenessVerdict.reason).
# ---------------------------------------------------------------------------
REASON_NOT_COVERING: Final[str] = "not_covering"
REASON_ABSTAIN_COVERAGE: Final[str] = "abstain_coverage"
REASON_NON_TRADING: Final[str] = "non_trading"
REASON_NO_TODAY_BARS: Final[str] = "no_today_bars"
REASON_COUNT_SHORT: Final[str] = "count_short"
REASON_COUNT_EXCESS: Final[str] = "count_excess"
REASON_LAST_BAR_INCOMPLETE: Final[str] = "last_bar_incomplete"
REASON_OI_NULL_SETTLING: Final[str] = "oi_null_settling"
REASON_VOLUME_ALL_ZERO: Final[str] = "volume_all_zero"
REASON_COMPLETE: Final[str] = "complete"


class InstrumentClass(Enum):
    """The gate's per-class criteria selector (the binding per-segment table)."""

    #: Spot tail, ``instrument.is_index`` and not session-exempt — count +
    #: last-bar demotion teeth (indices print every session minute).
    INDEX_SPOT = "index_spot"
    #: Spot tail, everything else — the all-zero-volume shape check only.
    CASH_SPOT = "cash_spot"
    #: FNO tail, ``contract.contract_type == "FUT"`` — OI-null / all-zero-volume
    #: shape mandatory; count observe-only.
    FUT = "fut"
    #: FNO tail, CE/PE — the OI-null shape only.
    OPTION = "option"


@dataclass(frozen=True, slots=True)
class TodayBarSplit:
    """A window's bars partitioned by IST date against the clamp instant's date.

    The IST-date law is ``bar.timestamp.astimezone(IST).date()`` — the SAME
    conversion the session filter / the engine key on. ``prior`` is always safe
    to append (settled history strictly before *clamp_today*); ``today`` is the
    judged set; ``later`` is any boundary ``today + 1`` leak (a covers-today
    window's end date is exclusive-by-tiling but
    :func:`~brutex.adapters.ingest_common.drop_bars_after` keeps a bar ON the
    boundary date — withholding it alongside today's bars closes the same
    not-yet-settled class in one stroke).
    """

    prior: list[CandleBar]
    today: list[CandleBar]
    later: list[CandleBar]

    @property
    def unchanged(self) -> bool:
        """``True`` iff the split withholds nothing (``today`` and ``later`` empty)."""
        return not self.today and not self.later


def split_today_bars(bars: Sequence[CandleBar], clamp_today: date) -> TodayBarSplit:
    """Partition *bars* into (prior, today, later) by IST date vs *clamp_today* — pure, O(bars)."""
    prior: list[CandleBar] = []
    today: list[CandleBar] = []
    later: list[CandleBar] = []
    for bar in bars:
        bar_date = bar.timestamp.astimezone(IST).date()
        if bar_date < clamp_today:
            prior.append(bar)
        elif bar_date == clamp_today:
            today.append(bar)
        else:
            later.append(bar)
    return TodayBarSplit(prior=prior, today=today, later=later)


@dataclass(frozen=True, slots=True)
class TodayCompletenessVerdict:
    """The pure classification of a window's TODAY bars — every field honest.

    ``incomplete=True`` ⇒ the caller demotes + withholds; every abstain/vacuous
    arm reads ``incomplete=False`` with its machine ``reason`` token.
    """

    covers_today: bool
    incomplete: bool
    reason: str
    expected_minutes: int | None
    distinct_minutes: int | None
    #: ``"HH:MM"`` (IST) of the max today bar, or ``None`` when no today bars.
    last_bar_ist: str | None
    oi_null_count: int
    zero_volume_count: int
    #: FUT observe-only count-shortfall flag (never demotes — the INFO signal).
    count_short_observed: bool


def _inert_verdict(reason: str, *, covers_today: bool) -> TodayCompletenessVerdict:
    """A no-criteria verdict (not-covering / abstain / non-trading) — never demotes."""
    return TodayCompletenessVerdict(
        covers_today=covers_today,
        incomplete=False,
        reason=reason,
        expected_minutes=None,
        distinct_minutes=None,
        last_bar_ist=None,
        oi_null_count=0,
        zero_volume_count=0,
        count_short_observed=False,
    )


def classify_today_completeness(
    *,
    bars_today: Sequence[CandleBar],
    clamp_today: date,
    window_end: date,
    instrument_class: InstrumentClass,
) -> TodayCompletenessVerdict:
    """Classify a window's TODAY bars against the per-class criteria — PURE.

    Order: covers-today test (strict ``window_end == clamp_today + 1 day``) →
    :func:`~brutex.calendars.nse.is_holiday_data_available` abstain →
    :func:`~brutex.calendars.nse.get_session` ``None`` ⇒ vacuous pass
    (``non_trading``) → the per-class criteria table. The distinct-minute count
    is over IST minute-floors (dup-proof); the last-bar law is
    :func:`~brutex.calendars.nse.session_last_bar_complete` VERBATIM — never a
    hand-rolled ``>= close`` (the +1-minute half-open law: a complete regular
    session's last stored bar is labelled 15:29). *bars_today* is the caller's
    already-split TODAY set (:func:`split_today_bars`). No I/O, no clock read.
    """
    if window_end != clamp_today + timedelta(days=1):
        return _inert_verdict(REASON_NOT_COVERING, covers_today=False)
    if not is_holiday_data_available(clamp_today):
        # Out-of-coverage (≥2027): the unguarded get_session would FABRICATE a
        # regular session and the gate would demand 375 minutes of an unknown
        # holiday forever — abstain (dormant, not wrong) until the annual
        # holiday-table extension lands.
        return _inert_verdict(REASON_ABSTAIN_COVERAGE, covers_today=True)
    session = get_session(clamp_today)
    if session is None:
        # Weekend / holiday covers-today window expects ZERO session bars —
        # vacuous pass (the session filter already dropped any leak).
        return _inert_verdict(REASON_NON_TRADING, covers_today=True)
    expected = session.duration_minutes
    distinct = len(
        {b.timestamp.astimezone(IST).replace(second=0, microsecond=0) for b in bars_today}
    )
    oi_null_count = sum(1 for b in bars_today if b.open_interest is None)
    zero_volume_count = sum(1 for b in bars_today if b.volume == 0)
    last_bar_ist: str | None = None
    last_dt: datetime | None = None  # hoisted ONCE — round-1 R4-NIT-3 (was computed twice)
    if bars_today:
        last_dt = max(b.timestamp for b in bars_today).astimezone(IST)
        last_bar_ist = last_dt.strftime("%H:%M")
    reason = REASON_COMPLETE
    incomplete = False
    count_short_observed = False
    if instrument_class is InstrumentClass.INDEX_SPOT:
        # Count + close-edge demotion teeth (zero today bars FAIL here too:
        # 0 != expected — indices always print; post-close zero is vendor lag).
        # Reason precedence: count first (a short day cannot prove its close edge).
        if distinct < expected:
            reason, incomplete = REASON_COUNT_SHORT, True
        elif distinct > expected:
            # Near-impossible after the session filter caps to [open, close) —
            # but if it ever fires something is deeply wrong and settling would
            # be a lie: fail with its own reason.
            reason, incomplete = REASON_COUNT_EXCESS, True
        elif last_dt is not None and not session_last_bar_complete(session, last_dt):
            # distinct == expected >= 1 (every NSE session has >= 60 minutes),
            # so bars_today is non-empty ⇒ last_dt is not None here by
            # construction — the None guard is the type-narrower only (a
            # compound-`and` sub-condition, coverage-arc-safe).
            reason, incomplete = REASON_LAST_BAR_INCOMPLETE, True
    elif not bars_today:
        # CASH/FUT/OPTION with zero today bars: ABSTAIN / vacuous pass — a dead
        # strike, a dormant far-month contract or a vendor-empty cash day
        # legitimately prints nothing; demoting would re-pull thousands of
        # contracts every evening.
        reason = REASON_NO_TODAY_BARS
    elif instrument_class is InstrumentClass.CASH_SPOT:
        if zero_volume_count == len(bars_today):
            reason, incomplete = REASON_VOLUME_ALL_ZERO, True
    elif instrument_class is InstrumentClass.FUT:
        count_short_observed = distinct < expected
        if oi_null_count > 0:
            reason, incomplete = REASON_OI_NULL_SETTLING, True
        elif zero_volume_count == len(bars_today):
            reason, incomplete = REASON_VOLUME_ALL_ZERO, True
    elif oi_null_count > 0:  # InstrumentClass.OPTION — the OI-null shape only
        reason, incomplete = REASON_OI_NULL_SETTLING, True
    return TodayCompletenessVerdict(
        covers_today=True,
        incomplete=incomplete,
        reason=reason,
        expected_minutes=expected,
        distinct_minutes=distinct,
        last_bar_ist=last_bar_ist,
        oi_null_count=oi_null_count,
        zero_volume_count=zero_volume_count,
        count_short_observed=count_short_observed,
    )


# ---------------------------------------------------------------------------
# Per-pull capped-event state (the parser _warn_epoch_timestamp_once shape —
# module-level, thread-safe, keyed on pull_id, with an explicit reset seam).
# Grows O(pulls) per process lifetime — honestly bounded by the few pulls a
# process ever runs.
# ---------------------------------------------------------------------------
_GATE_LOG_LOCK: Final[threading.Lock] = threading.Lock()
_INCOMPLETE_COUNTS: dict[str, int] = {}
_SUPPRESSED_FLUSHED: set[str] = set()
_COUNT_SHORT_COUNTS: dict[str, int] = {}
_EMPTY_COUNTS: dict[str, int] = {}
_ABSTAIN_EMITTED: set[tuple[str, str]] = set()


def reset_today_gate_log_state() -> None:
    """Clear the per-pull capped-event counters — the explicit test/operator reset seam."""
    with _GATE_LOG_LOCK:
        _INCOMPLETE_COUNTS.clear()
        _SUPPRESSED_FLUSHED.clear()
        _COUNT_SHORT_COUNTS.clear()
        _EMPTY_COUNTS.clear()
        _ABSTAIN_EMITTED.clear()


def _bump_capped(counts: dict[str, int], pull_key: str) -> bool:
    """Bump *pull_key*'s count in *counts*; ``True`` iff still within the cap — atomic."""
    with _GATE_LOG_LOCK:
        count = counts.get(pull_key, 0) + 1
        counts[pull_key] = count
        return count <= _EVENT_CAP_PER_PULL


def observe_today_completeness(
    log: object,
    *,
    bars: Sequence[CandleBar],
    clamp_today: date | None,
    window_end: date,
    instrument_class: InstrumentClass,
    symbol: str,
    interval: str,
    pull_id: str | None,
    raw_count: int,
    fields: Mapping[str, object] | None = None,
) -> tuple[TodayBarSplit, TodayCompletenessVerdict] | None:
    """The ONE guarded gate wiring both ingest tails call — fail-open, capped events.

    Returns ``None`` (⇒ the tail appends the ORIGINAL bars object and never
    demotes) when: *clamp_today* is ``None`` (a legacy/un-threaded payload —
    fail-open to pre-gate behavior, never a fresh clock read) · *interval* is
    not ``"1minute"`` · the window does not cover today (strict
    ``window_end == clamp_today + 1 day`` — the classifier is not even
    consulted, pinned) · the fetch was raw-EMPTY (*raw_count* == 0 — the EMPTY
    channel owns it; observe-only :data:`TODAY_EMPTY_EVENT` visibility on
    INDEX/FUT) · the verdict abstained (out-of-coverage / non-trading /
    no-today-bars) · ANY internal error (one :data:`TODAY_GATE_ERROR_EVENT`
    WARN — the :func:`~brutex.data.partial_window_detect.observe_partial_window`
    fail-open contract: a crashed diagnostic degrades to pre-gate exposure,
    disclosed, never a demote-loop).

    On a real covers-today verdict it emits the capped events itself (both
    spines share ONE emission discipline) and returns the
    ``(TodayBarSplit, TodayCompletenessVerdict)`` pair: ``incomplete=False`` ⇒
    append the ORIGINAL bars (byte-identical — the complete-day pin);
    ``incomplete=True`` ⇒ append ``split.prior`` only + demote with
    :data:`TODAY_INCOMPLETE`.

    Suppressed-summary honesty: the :data:`TODAY_INCOMPLETE_SUPPRESSED_EVENT`
    flush carries flush-time counts (observation #cap+1) — a FLOOR on the
    pull's final demotion total, never the total itself. The true per-pull
    count lives in the window ledger's ``FAILED`` + :data:`TODAY_INCOMPLETE`
    rows (see the event's own doc-comment; MIGRATION.md #1755 item (8)).

    *raw_count* is the window's UN-filtered vendor bar count (``len(raw_bars)``)
    — needed to route the raw-EMPTY shape to the EMPTY machinery instead of a
    structurally-ignored demotion. *log* is the caller's structlog logger
    (duck-typed ``.warning``/``.info``, the observe_all_filtered convention).
    """
    if clamp_today is None or interval != _ONE_MINUTE:
        return None
    if window_end != clamp_today + timedelta(days=1):
        return None
    # The caller's per-window field dict (window bounds + series coordinates) —
    # keys this wrapper stamps EXPLICITLY on its events are dropped so a caller
    # passing e.g. ``symbol``/``interval`` in *fields* (the tails' series field
    # dicts do) can never collide into a duplicate-kwarg TypeError.
    extra = {k: v for k, v in (fields or {}).items() if k not in _WRAPPER_OWNED_EVENT_KEYS}
    pull_key = pull_id or ""
    try:
        if raw_count == 0:
            # The EMPTY channel owns a raw-empty window (classify_window_status
            # ignores ``degraded`` at raw 0) — observe-only visibility on the
            # classes where a post-close empty TODAY is anomalous (indices
            # always print; a front-month future prints too), never a demotion.
            if instrument_class in (
                InstrumentClass.INDEX_SPOT,
                InstrumentClass.FUT,
            ) and _bump_capped(_EMPTY_COUNTS, pull_key):
                log.warning(  # type: ignore[attr-defined]  # duck-typed structlog logger
                    TODAY_EMPTY_EVENT,
                    pull_id=pull_id,
                    symbol=symbol,
                    interval=interval,
                    session_date=clamp_today.isoformat(),
                    instrument_class=instrument_class.value,
                    **extra,
                )
            return None
        split = split_today_bars(bars, clamp_today)
        verdict = classify_today_completeness(
            bars_today=split.today,
            clamp_today=clamp_today,
            window_end=window_end,
            instrument_class=instrument_class,
        )
        if verdict.reason == REASON_ABSTAIN_COVERAGE:
            abstain_key = (pull_key, "out_of_calendar_coverage")
            with _GATE_LOG_LOCK:
                emit_abstain = abstain_key not in _ABSTAIN_EMITTED
                _ABSTAIN_EMITTED.add(abstain_key)
            if emit_abstain:
                log.info(  # type: ignore[attr-defined]
                    TODAY_GATE_ABSTAIN_EVENT,
                    pull_id=pull_id,
                    reason="out_of_calendar_coverage",
                    session_date=clamp_today.isoformat(),
                )
            return None
        if verdict.reason in (REASON_NON_TRADING, REASON_NO_TODAY_BARS):
            return None
        if verdict.count_short_observed and _bump_capped(_COUNT_SHORT_COUNTS, pull_key):
            # FUT count shortfall — OBSERVE-ONLY (a far-serial-month future is
            # legitimately sparse); ``demoted=False`` states the count check
            # itself never demotes (the shape checks above own the demotion).
            log.info(  # type: ignore[attr-defined]
                TODAY_COUNT_SHORT_EVENT,
                pull_id=pull_id,
                symbol=symbol,
                interval=interval,
                distinct_minutes=verdict.distinct_minutes,
                expected_minutes=verdict.expected_minutes,
                demoted=False,
                **extra,
            )
        if verdict.incomplete:
            withheld = len(split.today) + len(split.later)
            with _GATE_LOG_LOCK:
                count = _INCOMPLETE_COUNTS.get(pull_key, 0) + 1
                _INCOMPLETE_COUNTS[pull_key] = count
                emit_warn = count <= _EVENT_CAP_PER_PULL
                emit_summary = count > _EVENT_CAP_PER_PULL and pull_key not in _SUPPRESSED_FLUSHED
                if emit_summary:
                    _SUPPRESSED_FLUSHED.add(pull_key)
            if emit_warn:
                log.warning(  # type: ignore[attr-defined]
                    TODAY_INCOMPLETE_EVENT,
                    pull_id=pull_id,
                    symbol=symbol,
                    interval=interval,
                    session_date=clamp_today.isoformat(),
                    expected_minutes=verdict.expected_minutes,
                    distinct_minutes=verdict.distinct_minutes,
                    last_bar_ist=verdict.last_bar_ist,
                    reason=verdict.reason,
                    oi_null_count=verdict.oi_null_count,
                    zero_volume_count=verdict.zero_volume_count,
                    withheld_bars=withheld,
                    instrument_class=instrument_class.value,
                    **extra,
                )
            if emit_summary:
                log.warning(  # type: ignore[attr-defined]
                    TODAY_INCOMPLETE_SUPPRESSED_EVENT,
                    pull_id=pull_id,
                    suppressed_count=count - _EVENT_CAP_PER_PULL,
                    total_incomplete=count,
                )
        return (split, verdict)
    except Exception as exc:  # a diagnostic must never demote-loop / crash a pull
        log.warning(  # type: ignore[attr-defined]
            TODAY_GATE_ERROR_EVENT,
            pull_id=pull_id,
            symbol=symbol,
            interval=interval,
            error=str(exc),
        )
        return None


def resolve_today_completeness_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_TODAY_COMPLETENESS_GATE`` kill switch — fail-loud.

    unset / ``""`` / ``"1"`` / ``"on"`` / ``"true"`` / ``"yes"`` (case-insensitive,
    whitespace-stripped) → ``True`` (default-ENABLED: keeping still-settling rows
    out of the keep-first lake is the safe posture); ``"0"`` / ``"off"`` /
    ``"false"`` / ``"no"`` → ``False`` (the operator escape hatch). Anything else
    raises — junk is never silently coerced (the DEC-RAM-BUDGETS-001 fail-loud
    env convention). Resolved ONCE per pull at the production arm sites (the
    :func:`~brutex.calendars.market_hours.resolve_market_closed` posture — junk
    fails loud at pull START, never mid-run).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(TODAY_COMPLETENESS_GATE_ENV, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{TODAY_COMPLETENESS_GATE_ENV}={raw!r} is not a recognised switch value; "
        f"expected one of '1', 'on', 'true', 'yes' (enable — the default when unset) "
        f"or '0', 'off', 'false', 'no' (disable)"
    )
