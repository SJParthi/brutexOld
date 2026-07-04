"""Mass-empty-settle circuit breaker — the ONE per-pull guard for both ingest spines.

DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 (divergence register R2/R3,
``docs/groww/pull-divergence-register.md`` §1). A vendor under load / soft-ban /
CDN degrade can answer with **clean-but-empty HTTP 200s** instead of 429s (the
repo's own observed live-throttle shape), and a **mis-addressed request** (the
proven M&M-``&`` class) gets the same clean-empty 200. Before this breaker, the
old-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001) turned every such window on a
2020→today backfill into a PERMANENT ``SKIPPED`` settle in ONE pull — under a
green "✅ Pull complete / No action needed" verdict. This module is the
vendor-neutral counter that detects the MASS-empty shape and halts the pull
LOUDLY before it cements real history as gaps:

* **One breaker per pull pass** — constructed by the run-ingest layer for every
  pass (state can NEVER leak across pulls); both the spot spine
  (:mod:`brutex.adapters.groww.ingest`) and the F&O spine
  (:mod:`brutex.adapters.groww.option_ingest`) feed the SAME implementation
  (CLAUDE.md §11 COMMON — no per-spine copy).
* **Two trip rules**, both config-injected (DYNAMIC — env-overridable, sane
  defaults, no hardcoding), each keyed to a DIFFERENT throttle signature:

  1. **CONSECUTIVE** (:data:`EMPTY_BREAKER_CONSECUTIVE_LIMIT`) — the *mid-pull*
     throttle signature. Trips only when the CURRENT consecutive counted-empty
     run (reset by any data-bearing window) (a) reaches the limit, (b) spans at
     least :data:`EMPTY_BREAKER_MIN_SERIES` DISTINCT series **within the run
     itself** (run composition, NOT a cumulative pull-lifetime tally), and (c)
     contains at least one **post-data empty** — an empty from a series that
     already landed data THIS pull. A series that was producing bars and
     suddenly goes quiet is the throttle shape; a never-yet-producing series'
     leading empties are exactly what a late-listed symbol's pre-listing
     prefix looks like, so they can NEVER satisfy (c) and can never trip this
     rule — however many of them cluster back-to-back (the worklist-tail /
     resume / recent-listing false-halt class this design exists to prevent).
  2. **FRACTION** (:data:`EMPTY_BREAKER_FRACTION_LIMIT` /
     :data:`EMPTY_BREAKER_MIN_OBSERVATIONS`) — the *throttle-from-pull-start*
     signature, where NO series ever lands data so rule 1 is structurally
     silent. Trips once counted empties reach the fraction of ALL observed
     windows over a minimum sample — but only after counted empties span
     :data:`EMPTY_BREAKER_FRACTION_MIN_SERIES` distinct series (cumulative),
     the floor that keeps a small targeted pull (2-3 recently-listed symbols,
     ≤ 8 series at both intervals) from masquerading as a throttled universe.

* **Expected-empty knowledge is excluded, everything else counts** (honest,
  Golden Rule #1): the repo has NO reliable listing-date source today, so by
  default every empty window COUNTS (the register's "exclude only windows the
  code can PROVE are pre-listing, else count them"). Three provable/structural
  exclusions carry the false-positive burden: (a) the injectable
  :attr:`EmptySettleBreaker.expected_empty` predicate (the F&O runner wires its
  confirmed-deep far-OTM strike budget; a future listing-date source plugs in
  here); (b) the per-call ``expected`` hint on :meth:`observe_empty` — the
  spines pass ``True`` for a window whose OWN ledger row already says EMPTY (a
  prior pull saw it empty — re-confirming it is expected, the empty-dominated
  RESUME shape) or is ``REOPENED``-marked (a deliberate operator re-check via
  ``brutex reopen-skipped`` — breaker-exempt by design, so a reopen + re-pull
  can never re-trip endlessly); (c) **leading-empty retro-clearing** — a
  series' empties seen BEFORE its first data-bearing window are un-counted the
  moment that series lands data (a pre-listing prefix self-clears; under a
  real throttle no data ever arrives, so nothing clears).
* **Trip = halt + un-cementable residue**: a trip raises
  :class:`MassEmptySettleHalt` (a global-stop, added to both runners'
  ``_GLOBAL_STOP_ERRORS``), naming the counts and the three candidate causes.
  The tripping window is never recorded, in-flight windows record UNTRUSTED
  empties (no settle, no streak progress — see
  :meth:`~brutex.data.window_ledger.WindowLedger.record`'s ``suppress_settle``),
  and everything un-settled stays RETRYABLE. Windows settled BEFORE the trip are
  reversible via ``brutex reopen-skipped``
  (:meth:`~brutex.data.window_ledger.WindowLedger.reopen_skipped`).

**Honest envelope (Golden Rule #10):** the two rules are HEURISTICS over
response shapes — they cannot distinguish a vendor soft-throttle from a
mis-addressed symbol from a genuine no-data universe by themselves; they only
guarantee the ambiguity is surfaced to the operator instead of silently settled.
Known blind spots (documented, not hidden): (a) a throttle active from pull
start against a SMALL fresh pull (fewer distinct series than the fraction
floor — e.g. the very first 3-index backfill, 6 series) halts nothing — the
settled-count verdict note + the ``needs_operator`` settled-skipped threshold
(:data:`~brutex.adapters.ingest_common.SETTLED_SKIPPED_OPERATOR_THRESHOLD`) and
``doctor --live`` stage 5 are the nets there; (b) a throttle that begins
exactly at a symbol boundary of a strictly SEQUENTIAL pull empties only
never-yet-producing series, so rule 1 stays silent and rule 2 must overcome
the healthy head's dilution — slow or missed; the same settled-count nets
apply; (c) empties re-confirming prior-EMPTY ledger rows are expected and
uncounted, so a throttle spanning two consecutive pulls is under-counted on
the second — the across-pull streak promotion those settles ride is the
system's designed ≥ :data:`~brutex.data.window_ledger.EMPTY_TERMINAL_THRESHOLD`
-sightings evidence standard. Every observation is **O(1)** (a fixed number of
counter ops under one lock); the per-series maps are O(series) memory, bounded
by the pull's worklist.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Final

import structlog

from brutex.models import BruteXError

#: Structured logger — the canary-probe verdicts
#: (``empty_breaker.canary_vendor_healthy`` / ``empty_breaker.canary_probe_error``,
#: DEC-EMPTY-BREAKER-CANARY-001).
_LOG = structlog.get_logger(__name__)

__all__ = [
    "EMPTY_BREAKER_CONSECUTIVE_ENV",
    "EMPTY_BREAKER_CONSECUTIVE_LIMIT",
    "EMPTY_BREAKER_ENABLED_ENV",
    "EMPTY_BREAKER_FRACTION_ENV",
    "EMPTY_BREAKER_FRACTION_LIMIT",
    "EMPTY_BREAKER_FRACTION_MIN_SERIES",
    "EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV",
    "EMPTY_BREAKER_MIN_OBSERVATIONS",
    "EMPTY_BREAKER_MIN_OBSERVATIONS_ENV",
    "EMPTY_BREAKER_MIN_SERIES",
    "EMPTY_BREAKER_MIN_SERIES_ENV",
    "OFF_ENV_VALUES",
    "EmptySettleBreaker",
    "MassEmptySettleHalt",
    "resolve_empty_breaker",
    "resolve_int_env",
]

#: Default CONSECUTIVE counted-empty-run trip threshold. Heuristic, NOT a vendor
#: fact (Golden Rule #1). TRUE tiling arithmetic (the calibration input): a full
#: ``2020-01-01 → today`` (~6.5 years ≈ ~2,370 days) single-symbol backfill tiles
#: to **~80 windows at 1-minute** (30-day tiles — `groww/chunks.py`) and ~14 at
#: 1-day (180-day tiles) — NOT the ~27/90-day figure this constant was first
#: (wrongly) derived from. Under the redesigned rule the limit no longer needs to
#: out-size any prefix: a pre-listing prefix contains no post-data empty and can
#: never trip the consecutive rule REGARDLESS of length, so 40 is purely the
#: halt-latency vs anomaly-tolerance knob — 40 confirmed-empty windows cost
#: ≥ 80 vendor calls (each confirmed empty is ≥ 2 fetches via the in-pull retry),
#: ≈ 10-15 s of continuous all-empty fetching at the 8 r/s production ceiling:
#: a real mid-pull throttle halts within seconds, while a small anomaly cluster
#: (a few genuinely-empty post-data windows) stays far below it. Env-overridable
#: (:data:`EMPTY_BREAKER_CONSECUTIVE_ENV`); ``<= 0`` disables the rule.
EMPTY_BREAKER_CONSECUTIVE_LIMIT: Final[int] = 40

#: Default EMPTY-FRACTION trip threshold (counted empties / observed windows).
#: Heuristic: real Track-1 symbols (the 3 indexes + F&O names) have dense 2020+
#: history, so ≥ 80% of a meaningful sample coming back empty is the
#: throttle/mis-addressing shape, not data. Env-overridable
#: (:data:`EMPTY_BREAKER_FRACTION_ENV`); must be in ``(0, 1]``.
EMPTY_BREAKER_FRACTION_LIMIT: Final[float] = 0.8

#: Minimum observed windows before the FRACTION rule arms — the sample-size
#: floor, NOT a single-symbol guard (the old rationale here was arithmetically
#: wrong: one symbol's 2020→today 1-minute backfill tiles ~80 windows at 30-day
#: tiles, which CROSSES 50 — the small-pull guard is
#: :data:`EMPTY_BREAKER_FRACTION_MIN_SERIES`). 50 is the smallest sample where a
#: ≥ 80% empty rate is a response *shape* rather than noise, and a bulk pull
#: reaches it within its first minute. Env-overridable
#: (:data:`EMPTY_BREAKER_MIN_OBSERVATIONS_ENV`); ``<= 0`` arms it immediately.
EMPTY_BREAKER_MIN_OBSERVATIONS: Final[int] = 50

#: Minimum DISTINCT series (``"<symbol>:<interval>"``) the CURRENT consecutive
#: run must span before the CONSECUTIVE rule may trip — evaluated over the RUN's
#: own composition (reset by any data window), NOT cumulatively over the pull
#: (the cumulative form let two long-forgotten counted series arm a much later
#: single-series run — the reviewed false-halt class). A vendor soft-throttle
#: [R2] empties every in-flight series at once, while ONE dead / delisted /
#: renamed symbol is 1-2 series (1minute + 1day), so 3 keeps a single symbol's
#: mid-range gap from halting a pull. Env-overridable
#: (:data:`EMPTY_BREAKER_MIN_SERIES_ENV`); ``<= 1`` arms on any series.
EMPTY_BREAKER_MIN_SERIES: Final[int] = 3

#: Minimum DISTINCT series (cumulative, retro-clear-pruned) that must have
#: contributed a counted empty before the FRACTION rule may trip — the floor
#: that separates "a throttled fresh UNIVERSE" (the ~750-underlying bulk pull
#: crosses 10 counted-empty series within its first handful of symbols, ~2 min
#: even fully sequentially at 8 r/s) from "a small targeted pull of
#: recently-listed names" (2-3 symbols × 2 intervals = ≤ 6 series; ≤ 4 symbols
#: = ≤ 8 — all below the floor, so their all-empty pre-listing prefixes can
#: never read as a throttled universe). Honest cost: a throttle-from-start
#: against a pull SMALLER than the floor is NOT halted — the settled-count
#: verdict + ``needs_operator`` threshold are the nets (module docstring, blind
#: spot (a)). Env-overridable (:data:`EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV`);
#: ``<= 1`` arms on any series.
EMPTY_BREAKER_FRACTION_MIN_SERIES: Final[int] = 10

#: Env override for :data:`EMPTY_BREAKER_CONSECUTIVE_LIMIT` (int; ``<= 0`` disables).
EMPTY_BREAKER_CONSECUTIVE_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_CONSECUTIVE"
#: Env override for :data:`EMPTY_BREAKER_FRACTION_LIMIT` (float in ``(0, 1]``).
EMPTY_BREAKER_FRACTION_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_FRACTION"
#: Env override for :data:`EMPTY_BREAKER_MIN_OBSERVATIONS` (int; ``<= 0`` arms at once).
EMPTY_BREAKER_MIN_OBSERVATIONS_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_MIN_OBSERVATIONS"
#: Env override for :data:`EMPTY_BREAKER_MIN_SERIES` (int; ``<= 1`` arms on any series).
EMPTY_BREAKER_MIN_SERIES_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_MIN_SERIES"
#: Env override for :data:`EMPTY_BREAKER_FRACTION_MIN_SERIES` (int; ``<= 1`` arms
#: on any series).
EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_FRACTION_MIN_SERIES"
#: Kill switch: ``BRUTEX_EMPTY_BREAKER=off|0|false|no`` disables the breaker
#: entirely (observations no-op, never trips). Default ON — the protection is
#: the point; the switch exists for operator triage, mirroring the repo's other
#: kill-switchable pull levers.
EMPTY_BREAKER_ENABLED_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER"

#: Truthy-off spellings for the breaker kill switches (case-insensitive) — PUBLIC
#: (CLAUDE.md §11 COMMON): the error-storm breaker
#: (:mod:`brutex.adapters.error_storm_breaker`, DEC-VENDOR-DOWN-BREAKER-001) shares
#: the SAME off-spellings for its own kill switch instead of copying the set.
OFF_ENV_VALUES: Final[frozenset[str]] = frozenset({"off", "0", "false", "no"})


class MassEmptySettleHalt(BruteXError):
    """The pull-wide global stop raised when the mass-empty circuit breaker trips.

    DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001. Carries the breaker's counts so the
    verdict layer can NAME them (register R2 fix: "surface settle counts in the
    verdict"), and a message enumerating the three candidate causes the code
    cannot distinguish (Golden Rule #1 — no invented certainty): a vendor
    soft-throttle serving clean-empty 200s [R2], mis-addressed requests [R3],
    or a genuine pre-listing / no-data universe. Added to both runners'
    ``_GLOBAL_STOP_ERRORS`` so it propagates past the per-symbol quarantine and
    halts the whole pull (every remaining window would poison identically).

    Also constructible like any plain exception (``MassEmptySettleHalt("msg")``)
    with the counts defaulting to 0: the pipeline's drift-pin test constructs
    every ``_GLOBAL_STOP_ERRORS`` member generically, so a global-stop class
    that cannot be built from a bare message would break that contract (the
    breaker's own trips always pass the keyword counts). The base
    ``__init__`` runs exactly ONCE on every path (the message is chosen first,
    then a single ``super().__init__`` call — the CodeQL multiple-``__init__``
    shape is structurally impossible), and a zero-argument construction gets an
    honest ``unspecified`` rule label instead of a degenerate ``( rule)`` hole.
    """

    def __init__(
        self,
        *args: object,
        empty_windows: int = 0,
        observed_windows: int = 0,
        consecutive_empties: int = 0,
        trip_rule: str = "",
    ) -> None:
        self.empty_windows = empty_windows
        self.observed_windows = observed_windows
        self.consecutive_empties = consecutive_empties
        self.trip_rule = trip_rule
        if not args:
            # Keyword/zero-arg construction — build the canonical count-naming
            # message (an empty trip_rule reads "unspecified", never "( rule)").
            args = (
                f"Mass-empty circuit breaker TRIPPED ({trip_rule or 'unspecified'} rule): "
                f"{empty_windows} of {observed_windows} fetched windows returned "
                f"clean-but-EMPTY responses ({consecutive_empties} consecutive). "
                "Likely causes: a vendor soft-throttle serving empty 200s [R2], "
                "mis-addressed requests [R3], or a genuine pre-listing gap. "
                "The pull HALTED before settling these windows as SKIPPED — they "
                "stay retryable; windows settled earlier can be re-opened with "
                "`brutex reopen-skipped`.",
            )
        # The ONE base-class init call for every construction path.
        super().__init__(*args)


@dataclass(slots=True)
class EmptySettleBreaker:
    """Per-pull, thread-safe mass-empty counter with two config-injected trip rules.

    One instance is threaded into EVERY window worker of one ingest pass (the
    spot and F&O spines share it — CLAUDE.md §11 COMMON), so all counter ops
    run under one lock, each **O(1)**. The runner constructs a fresh instance
    per pass (:func:`resolve_empty_breaker`), so state can never leak across
    pulls (Golden Rule #7 — a re-run observes only its own responses).

    Parameters
    ----------
    consecutive_limit
        Trip once the CURRENT consecutive counted-empty run reaches this length
        AND spans ``min_series`` distinct series AND contains a post-data empty
        (``<= 0`` disables the rule).
    fraction_limit
        Trip once ``counted empties / observed >= fraction_limit`` AND
        ``observed >= min_observations`` AND counted empties span
        ``fraction_min_series`` distinct series. Clamped semantics are the
        resolver's job (:func:`resolve_empty_breaker`); the raw dataclass
        trusts its args.
    min_observations
        Minimum observed windows before the fraction rule arms.
    min_series
        Minimum DISTINCT series the CURRENT consecutive run must span before
        the consecutive rule may trip — run composition, reset by any data
        window (``<= 1`` arms on any series).
    fraction_min_series
        Minimum DISTINCT series (cumulative, retro-clear-pruned) with a counted
        empty before the fraction rule may trip — the small-targeted-pull vs
        throttled-universe discriminator (``<= 1`` arms on any series).
    enabled
        ``False`` turns every method into a no-op (the operator kill switch).
    expected_empty
        Optional ``(symbol, window_end) -> bool`` predicate: ``True`` means the
        window is PROVABLY expected to be empty (a pre-listing window under a
        listing-date source; a confirmed-deep far-OTM strike on the F&O spine),
        so it is observed but never counted toward a trip. ``None`` (the honest
        default — no reliable listing-date source exists today) counts every
        empty window not covered by the per-call ``expected`` hint.
    health_probe
        Optional zero-arg VENDOR-HEALTH canary (DEC-EMPTY-BREAKER-CANARY-001):
        consulted ONCE, single-flight, at the moment a trip rule first fires.
        The production wiring fetches ONE known-liquid canary window (the
        NIFTY spot last sessions — costs 1 vendor call) via the existing
        client. ``True`` (the canary returned DATA) means the vendor answers
        with data RIGHT NOW, so the accumulated clean-empties are genuine
        no-data — the breaker does NOT halt (one
        ``empty_breaker.canary_vendor_healthy`` log; the settle law governs
        the empties) and its trip EVIDENCE resets, so a LATER real throttle
        must re-accumulate a full evidence window before the canary is probed
        again (bounded: ≤ 1 canary call per evidence cycle). ``False`` — or a
        raising probe — halts exactly as an un-probed trip does today.
        ``None`` (the default — every existing caller) is byte-identical to
        the pre-canary behaviour: a trip rule halts immediately.
    """

    consecutive_limit: int = EMPTY_BREAKER_CONSECUTIVE_LIMIT
    fraction_limit: float = EMPTY_BREAKER_FRACTION_LIMIT
    min_observations: int = EMPTY_BREAKER_MIN_OBSERVATIONS
    min_series: int = EMPTY_BREAKER_MIN_SERIES
    fraction_min_series: int = EMPTY_BREAKER_FRACTION_MIN_SERIES
    enabled: bool = True
    expected_empty: Callable[[str, date], bool] | None = None
    health_probe: Callable[[], bool] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _observed: int = field(default=0, repr=False)
    _empties: int = field(default=0, repr=False)
    _consecutive: int = field(default=0, repr=False)
    _tripped: bool = field(default=False, repr=False)
    _trip_rule: str = field(default="", repr=False)
    #: Series (``"<symbol>:<interval>"``) that have landed ≥ 1 data-bearing window
    #: this pull — the first-bar knowledge the leading-empty retro-clear AND the
    #: post-data-empty throttle signature key on.
    _series_seen_data: set[str] = field(default_factory=set, repr=False)
    #: Per-series count of empties observed BEFORE that series' first data window
    #: (its candidate pre-listing prefix) — un-counted when data arrives.
    _series_leading: dict[str, int] = field(default_factory=dict, repr=False)
    #: Series that have contributed ≥ 1 COUNTED empty over the WHOLE pull — the
    #: FRACTION rule's series floor (a retro-cleared series is removed with its
    #: prefix). NOT consulted by the consecutive rule (which reads the run).
    _series_counted: set[str] = field(default_factory=set, repr=False)
    #: Series composition of the CURRENT consecutive run (cleared by any data
    #: window) — the consecutive rule's min-series evidence.
    _run_series: set[str] = field(default_factory=set, repr=False)
    #: Post-data empties inside the CURRENT run — counted empties from a series
    #: that already landed data this pull (the throttle signature; cleared by
    #: any data window).
    _run_post_data: int = field(default=0, repr=False)
    #: ``True`` while the vendor-health canary is IN FLIGHT (single-flight —
    #: exactly ONE worker probes; siblings keep counting but neither trip nor
    #: settle until the verdict lands). DEC-EMPTY-BREAKER-CANARY-001.
    _probe_inflight: bool = field(default=False, repr=False)

    @property
    def tripped(self) -> bool:
        """``True`` once either trip rule fired (latched for the pull) — **O(1)**."""
        with self._lock:
            return self._tripped

    def counts(self) -> tuple[int, int, int]:
        """``(observed, counted_empties, consecutive)`` snapshot — **O(1)**, atomic.

        A read-only telemetry/test surface (no private pokes needed to assert the
        tally): ``observed`` is every fetched window seen, ``counted_empties`` the
        empties currently held against the trip rules (post expected-empty
        exclusion and leading-empty retro-clearing), ``consecutive`` the live run
        length.
        """
        with self._lock:
            return (self._observed, self._empties, self._consecutive)

    def allow_settle(self) -> bool:
        """``True`` while EMPTY windows may still settle ``SKIPPED`` — **O(1)**.

        Consulted by both spines' consumer tails at ledger-record time (NOT only
        at fetch time), so a settle computed BEFORE the trip but recorded AFTER
        it (an async-pipeline queued tail, or a sibling in-flight worker) is
        still suppressed — "stop settling" holds pull-wide the instant the
        breaker trips. Settling is ALSO paused while the vendor-health canary
        is in flight (DEC-EMPTY-BREAKER-CANARY-001 — a trip candidate exists;
        if the canary comes back UNHEALTHY those settles would have been
        poison; a suppressed window merely stays retryable, never lost). A
        disabled breaker always allows.
        """
        if not self.enabled:
            return True
        with self._lock:
            return not (self._tripped or self._probe_inflight)

    def observe_data(self, series: str) -> None:
        """Record a data-bearing window for *series* — **O(1)**, resets the run.

        Ends the consecutive-empty run (length, series composition, and
        post-data tally), and — the FIRST time *series* lands data this pull —
        retro-clears its leading-empty prefix from the counted empties (those
        windows are now known to be that symbol's pre-listing / pre-history
        gap, not throttle evidence). Under a real throttle no series lands
        data, so nothing ever clears.
        """
        if not self.enabled:
            return
        with self._lock:
            self._observed += 1
            self._consecutive = 0
            self._run_series.clear()
            self._run_post_data = 0
            if series not in self._series_seen_data:
                self._series_seen_data.add(series)
                self._empties -= self._series_leading.pop(series, 0)
                # Its counted empties were all leading (pre-first-data) — the series
                # is no longer fraction-floor evidence until a later empty re-adds it.
                self._series_counted.discard(series)

    def observe_empty(
        self, series: str, *, symbol: str, window_end: date, expected: bool = False
    ) -> None:
        """Record a confirmed-empty window; RAISE :class:`MassEmptySettleHalt` on/after a trip.

        The window is always counted as OBSERVED. It is COUNTED (extending the
        consecutive run) unless it is expected-empty — either the per-call
        *expected* hint (the spines pass ``True`` when the window's OWN ledger
        row is already ``EMPTY`` from a prior pull, or ``REOPENED``-marked — a
        deliberate operator re-check) or the injected :attr:`expected_empty`
        predicate proves it (the register's rule: exclude only what the code
        can prove, else count it). An expected empty is NEUTRAL to the run: it
        neither extends nor resets it. While *series* has landed no data yet
        this pull, a counted empty is also tallied on its leading prefix so a
        later first-data window can retro-clear it (see :meth:`observe_data`);
        an empty from a series that HAS landed data is a **post-data empty** —
        the throttle signature the consecutive rule requires.

        Raising here (rather than returning a flag) is deliberate: the spines
        cannot forget the halt, and an already-tripped breaker keeps raising on
        EVERY further empty observation — expected or not — so every in-flight
        worker stops promptly. The raise happens OUTSIDE the lock. **O(1)** per
        call (plus, at most once per evidence cycle, the injected canary's ONE
        vendor call — DEC-EMPTY-BREAKER-CANARY-001: when a trip rule first
        fires WITH a :attr:`health_probe` wired, the halt verdict is deferred
        to the canary — see :meth:`_resolve_trip_with_canary`; siblings
        observing during the probe keep counting but neither trip nor raise
        until the verdict lands).
        """
        if not self.enabled:
            return
        provable = expected or (
            self.expected_empty is not None and self.expected_empty(symbol, window_end)
        )
        halt: MassEmptySettleHalt | None = None
        probe: Callable[[], bool] | None = None
        with self._lock:
            self._observed += 1
            if not self._tripped and not provable:
                self._empties += 1
                self._consecutive += 1
                self._series_counted.add(series)
                self._run_series.add(series)
                if series in self._series_seen_data:
                    self._run_post_data += 1
                else:
                    self._series_leading[series] = self._series_leading.get(series, 0) + 1
                if not self._probe_inflight:
                    self._trip_rule = self._tripping_rule_locked()
                    if self._trip_rule and self.health_probe is not None:
                        # Canary seam (DEC-EMPTY-BREAKER-CANARY-001): a trip
                        # candidate with a wired probe defers the verdict to ONE
                        # vendor-health fetch — single-flight (this worker runs
                        # it OUTSIDE the lock; siblings see _probe_inflight).
                        self._probe_inflight = True
                        probe = self.health_probe
                    else:
                        self._tripped = bool(self._trip_rule)
            if self._tripped:
                halt = MassEmptySettleHalt(
                    empty_windows=self._empties,
                    observed_windows=self._observed,
                    consecutive_empties=self._consecutive,
                    trip_rule=self._trip_rule,
                )
        if halt is not None:
            raise halt
        if probe is not None:
            self._resolve_trip_with_canary(probe)

    def _resolve_trip_with_canary(self, probe: Callable[[], bool]) -> None:
        """Run the ONE canary fetch and turn its verdict into halt-or-continue.

        DEC-EMPTY-BREAKER-CANARY-001. Runs OUTSIDE the counter lock (it is a
        network call). A HEALTHY canary (data returned) proves the vendor
        answers with data right now, so the accumulated clean-empties are
        genuine no-data, not a throttle: log
        ``empty_breaker.canary_vendor_healthy`` and RESET the trip evidence —
        the pull continues (the ledger settle law governs the genuine empties),
        and a later REAL throttle must re-accumulate a full evidence window
        before the canary is probed again (≤ 1 canary call per cycle). An
        UNHEALTHY canary (empty, or a raising probe — an erroring canary is
        NOT health evidence) latches the trip and raises
        :class:`MassEmptySettleHalt` exactly as an un-probed trip does.
        """
        try:
            healthy = bool(probe())
        except Exception as exc:  # pylint: disable=broad-except — a raising canary is NOT health evidence
            _LOG.warning(
                "empty_breaker.canary_probe_error",
                error_type=type(exc).__name__,
                error=str(exc),
                note="canary probe raised — treated as vendor-unhealthy (halting)",
            )
            healthy = False
        halt: MassEmptySettleHalt | None = None
        with self._lock:
            self._probe_inflight = False
            if healthy:
                _LOG.warning(
                    "empty_breaker.canary_vendor_healthy",
                    trip_rule=self._trip_rule,
                    observed_windows=self._observed,
                    empty_windows=self._empties,
                    consecutive_empties=self._consecutive,
                    note=(
                        "the canary window returned DATA — the vendor is healthy, the "
                        "accumulated clean-empties are genuine no-data (not a throttle); "
                        "NOT halting — the settle law governs the empties; trip evidence "
                        "resets"
                    ),
                )
                self._reset_evidence_locked()
            else:
                self._tripped = True
                halt = MassEmptySettleHalt(
                    empty_windows=self._empties,
                    observed_windows=self._observed,
                    consecutive_empties=self._consecutive,
                    trip_rule=self._trip_rule,
                )
        if halt is not None:
            raise halt

    def _reset_evidence_locked(self) -> None:
        """Clear the trip evidence after a HEALTHY canary — caller holds the lock.

        The canary proved the counted empties genuine at that instant, so they
        are no longer throttle evidence: the observed/empty/consecutive
        counters, both series floors, the run composition, and the leading
        prefixes all reset (a later real throttle rebuilds them from zero —
        bounding the canary to one call per evidence cycle).
        ``_series_seen_data`` is KEPT: "this series landed data this pull" is
        still true, so the post-data throttle signature stays armed.
        """
        self._trip_rule = ""
        self._observed = 0
        self._empties = 0
        self._consecutive = 0
        self._series_counted.clear()
        self._run_series.clear()
        self._run_post_data = 0
        self._series_leading.clear()

    def _tripping_rule_locked(self) -> str:
        """Evaluate both trip rules; return the tripping rule's name or ``""`` — **O(1)**.

        Caller holds :attr:`_lock`. Consecutive first (it is the faster, more
        specific mid-pull signature); each rule's gates are documented on its
        default constant.
        """
        if (
            0 < self.consecutive_limit <= self._consecutive
            and len(self._run_series) >= max(1, self.min_series)
            and self._run_post_data >= 1
        ):
            return "consecutive"
        if (
            self._observed >= self.min_observations
            and len(self._series_counted) >= max(1, self.fraction_min_series)
            and self._empties / self._observed >= self.fraction_limit
        ):
            return "fraction"
        return ""


def resolve_int_env(env_name: str, default: int) -> int:
    """Read an int env override; unset / blank / non-int fall back to *default* — **O(1)**.

    PUBLIC (CLAUDE.md §11 COMMON): shared with the error-storm breaker's resolver
    (:func:`brutex.adapters.error_storm_breaker.resolve_error_storm_breaker`) so the
    fail-safe env parsing has ONE source of truth.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_fraction(env_name: str, default: float) -> float:
    """Read a ``(0, 1]`` float env override; anything invalid falls back — **O(1)**."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not 0.0 < value <= 1.0:
        return default
    return value


def resolve_empty_breaker(
    *,
    expected_empty: Callable[[str, date], bool] | None = None,
    health_probe: Callable[[], bool] | None = None,
) -> EmptySettleBreaker:
    """Build a FRESH per-pass breaker from env config (or the documented defaults).

    The single construction seam both run-ingest layers call once per pass
    (CLAUDE.md §11 COMMON) — a new instance every time, so breaker state can
    never leak across pulls or retry passes. Every knob is env-overridable
    (DYNAMIC); a bad value fails safe to its default (a mis-typed env var can
    never disable the protection by accident — only the explicit
    :data:`EMPTY_BREAKER_ENABLED_ENV` off switch can). *expected_empty* threads
    a caller's provable expected-empty knowledge (the F&O runner passes its
    confirmed-deep far-OTM strike set); *health_probe* threads the vendor-health
    canary (DEC-EMPTY-BREAKER-CANARY-001 — the dashboard F&O pull wires a
    one-call NIFTY-spot canary; ``None`` keeps today's trip-halts-immediately
    behaviour byte-identical). **O(1)**.
    """
    raw_enabled = os.environ.get(EMPTY_BREAKER_ENABLED_ENV)
    enabled = raw_enabled is None or raw_enabled.strip().lower() not in OFF_ENV_VALUES
    return EmptySettleBreaker(
        consecutive_limit=resolve_int_env(
            EMPTY_BREAKER_CONSECUTIVE_ENV, EMPTY_BREAKER_CONSECUTIVE_LIMIT
        ),
        fraction_limit=_resolve_fraction(EMPTY_BREAKER_FRACTION_ENV, EMPTY_BREAKER_FRACTION_LIMIT),
        min_observations=resolve_int_env(
            EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, EMPTY_BREAKER_MIN_OBSERVATIONS
        ),
        min_series=resolve_int_env(EMPTY_BREAKER_MIN_SERIES_ENV, EMPTY_BREAKER_MIN_SERIES),
        fraction_min_series=resolve_int_env(
            EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV, EMPTY_BREAKER_FRACTION_MIN_SERIES
        ),
        enabled=enabled,
        expected_empty=expected_empty,
        health_probe=health_probe,
    )
