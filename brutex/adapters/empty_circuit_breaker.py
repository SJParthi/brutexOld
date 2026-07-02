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
  defaults, no hardcoding): a CONSECUTIVE run of confirmed-empty windows
  (:data:`EMPTY_BREAKER_CONSECUTIVE_LIMIT`), and a FRACTION of all observed
  windows once a minimum sample is seen
  (:data:`EMPTY_BREAKER_FRACTION_LIMIT` / :data:`EMPTY_BREAKER_MIN_OBSERVATIONS`).
* **Genuine-gap awareness** (honest envelope, Golden Rule #1/#10): the repo has
  NO reliable listing-date source today, so by default every empty window COUNTS
  (the register's "exclude only windows the code can PROVE are pre-listing, else
  count them"). Three mitigations keep genuine no-data from tripping it:
  an injectable :attr:`EmptySettleBreaker.expected_empty` predicate (the seam
  PROVABLE expected-empty knowledge plugs into — a pre-listing window under a
  future listing-date source, or an F&O chain's confirmed-deep far-OTM strike
  via the existing ``empty_retry_by_symbol`` budget — observed but never
  counted); **leading-empty retro-clearing** — a series' empty windows seen
  BEFORE its first data-bearing window are un-counted the moment that series
  lands data (a pre-listing prefix self-clears; under a real throttle no data
  ever arrives, so nothing clears); and a **minimum-distinct-series gate**
  (:data:`EMPTY_BREAKER_MIN_SERIES`) — a soft-throttle empties EVERY series,
  while ONE dead / reference-only / never-listed symbol is one series, so no
  trip fires until counted empties span that many distinct series (a
  single-symbol no-data pull settles honestly with the loud settled-count
  verdict + the ``reopen-skipped`` reversal instead of halting forever).
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
Every observation is **O(1)** (a fixed number of counter ops under one lock);
the per-series leading-empty map is O(series) memory, bounded by the pull's
worklist.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from brutex.models import BruteXError

__all__ = [
    "EMPTY_BREAKER_CONSECUTIVE_ENV",
    "EMPTY_BREAKER_CONSECUTIVE_LIMIT",
    "EMPTY_BREAKER_ENABLED_ENV",
    "EMPTY_BREAKER_FRACTION_ENV",
    "EMPTY_BREAKER_FRACTION_LIMIT",
    "EMPTY_BREAKER_MIN_OBSERVATIONS",
    "EMPTY_BREAKER_MIN_OBSERVATIONS_ENV",
    "EMPTY_BREAKER_MIN_SERIES",
    "EMPTY_BREAKER_MIN_SERIES_ENV",
    "EmptySettleBreaker",
    "MassEmptySettleHalt",
    "resolve_empty_breaker",
]

#: Default CONSECUTIVE confirmed-empty-window trip threshold. Heuristic, NOT a
#: vendor fact (Golden Rule #1): a full 2020→today single-symbol 1-minute
#: backfill tiles to ~27 windows (≈6.5 years / 90-day tiles), so ONE genuinely
#: dead / never-listed symbol pulled alone (~27 consecutive genuine empties)
#: must NOT trip on this rule — 40 clears that with headroom through ~2029,
#: while an hour-long soft-throttle episode on a bulk pull (where EVERY response
#: goes empty across symbols) crosses it fast. Env-overridable
#: (:data:`EMPTY_BREAKER_CONSECUTIVE_ENV`); ``<= 0`` disables the rule.
EMPTY_BREAKER_CONSECUTIVE_LIMIT: Final[int] = 40

#: Default EMPTY-FRACTION trip threshold (counted empties / observed windows).
#: Heuristic: real Track-1 symbols (the 3 indexes + F&O names) have dense 2020+
#: history, so ≥ 80% of a meaningful sample coming back empty is the
#: throttle/mis-addressing shape, not data. Env-overridable
#: (:data:`EMPTY_BREAKER_FRACTION_ENV`); must be in ``(0, 1]``.
EMPTY_BREAKER_FRACTION_LIMIT: Final[float] = 0.8

#: Minimum observed windows before the FRACTION rule arms. 50 keeps a
#: single-symbol pull (~27 windows for the full range) from EVER meeting the
#: fraction gate on its own — a lone dead symbol settles honestly (with the
#: loud settled-count verdict note) instead of halting the pull — while a bulk
#: backfill reaches 50 observations within its first minutes. Env-overridable
#: (:data:`EMPTY_BREAKER_MIN_OBSERVATIONS_ENV`); ``<= 0`` arms it immediately.
EMPTY_BREAKER_MIN_OBSERVATIONS: Final[int] = 50

#: Minimum DISTINCT series (``"<symbol>:<interval>"``) that must have contributed a
#: counted empty before EITHER rule may trip. A vendor soft-throttle [R2] empties
#: every series it touches, while ONE dead / reference-only / never-listed symbol
#: (a genuine no-data case the ``known_gap_only`` verdict already covers) is a
#: single series — it must settle honestly (loud settled-count note + the
#: ``reopen-skipped`` reversal), never halt the pull forever. 3 means a
#: single-symbol pull (1-2 series) can never trip; a bulk throttle crosses it in
#: its first seconds. Env-overridable (:data:`EMPTY_BREAKER_MIN_SERIES_ENV`);
#: ``<= 1`` arms on any series.
EMPTY_BREAKER_MIN_SERIES: Final[int] = 3

#: Env override for :data:`EMPTY_BREAKER_CONSECUTIVE_LIMIT` (int; ``<= 0`` disables).
EMPTY_BREAKER_CONSECUTIVE_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_CONSECUTIVE"
#: Env override for :data:`EMPTY_BREAKER_FRACTION_LIMIT` (float in ``(0, 1]``).
EMPTY_BREAKER_FRACTION_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_FRACTION"
#: Env override for :data:`EMPTY_BREAKER_MIN_OBSERVATIONS` (int; ``<= 0`` arms at once).
EMPTY_BREAKER_MIN_OBSERVATIONS_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_MIN_OBSERVATIONS"
#: Env override for :data:`EMPTY_BREAKER_MIN_SERIES` (int; ``<= 1`` arms on any series).
EMPTY_BREAKER_MIN_SERIES_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER_MIN_SERIES"
#: Kill switch: ``BRUTEX_EMPTY_BREAKER=off|0|false|no`` disables the breaker
#: entirely (observations no-op, never trips). Default ON — the protection is
#: the point; the switch exists for operator triage, mirroring the repo's other
#: kill-switchable pull levers.
EMPTY_BREAKER_ENABLED_ENV: Final[str] = "BRUTEX_EMPTY_BREAKER"

#: Truthy-off spellings for :data:`EMPTY_BREAKER_ENABLED_ENV` (case-insensitive).
_OFF_VALUES: Final[frozenset[str]] = frozenset({"off", "0", "false", "no"})


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
    breaker's own trips always pass the keyword counts).
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
        if args:
            # Generic construction (a plain message) — standard Exception
            # behaviour: ``str(exc)`` returns the given message.
            super().__init__(*args)
            return
        super().__init__(
            f"Mass-empty circuit breaker TRIPPED ({trip_rule} rule): "
            f"{empty_windows} of {observed_windows} fetched windows returned "
            f"clean-but-EMPTY responses ({consecutive_empties} consecutive). "
            "Likely causes: a vendor soft-throttle serving empty 200s [R2], "
            "mis-addressed requests [R3], or a genuine pre-listing gap. "
            "The pull HALTED before settling these windows as SKIPPED — they "
            "stay retryable; windows settled earlier can be re-opened with "
            "`brutex reopen-skipped`."
        )


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
        Trip after this many CONSECUTIVE counted-empty windows (``<= 0``
        disables the rule).
    fraction_limit
        Trip once ``counted empties / observed >= fraction_limit`` AND
        ``observed >= min_observations``. Clamped semantics are the resolver's
        job (:func:`resolve_empty_breaker`); the raw dataclass trusts its args.
    min_observations
        Minimum observed windows before the fraction rule arms.
    min_series
        Minimum DISTINCT series with a counted empty before EITHER rule may trip
        — the one-dead-symbol-vs-pull-wide-throttle discriminator (``<= 1`` arms
        on any series).
    enabled
        ``False`` turns every method into a no-op (the operator kill switch).
    expected_empty
        Optional ``(symbol, window_end) -> bool`` predicate: ``True`` means the
        window is PROVABLY expected to be empty (a pre-listing window under a
        listing-date source; a confirmed-deep far-OTM strike on the F&O spine),
        so it is observed but never counted toward a trip. ``None`` (the honest
        default — no reliable listing-date source exists today) counts every
        empty window.
    """

    consecutive_limit: int = EMPTY_BREAKER_CONSECUTIVE_LIMIT
    fraction_limit: float = EMPTY_BREAKER_FRACTION_LIMIT
    min_observations: int = EMPTY_BREAKER_MIN_OBSERVATIONS
    min_series: int = EMPTY_BREAKER_MIN_SERIES
    enabled: bool = True
    expected_empty: Callable[[str, date], bool] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _observed: int = field(default=0, repr=False)
    _empties: int = field(default=0, repr=False)
    _consecutive: int = field(default=0, repr=False)
    _tripped: bool = field(default=False, repr=False)
    _trip_rule: str = field(default="", repr=False)
    #: Series (``"<symbol>:<interval>"``) that have landed ≥ 1 data-bearing window
    #: this pull — the first-bar knowledge the leading-empty retro-clear keys on.
    _series_seen_data: set[str] = field(default_factory=set, repr=False)
    #: Per-series count of empties observed BEFORE that series' first data window
    #: (its candidate pre-listing prefix) — un-counted when data arrives.
    _series_leading: dict[str, int] = field(default_factory=dict, repr=False)
    #: Series that have contributed ≥ 1 COUNTED empty — the min-series trip gate's
    #: evidence set (a retro-cleared series is removed with its prefix).
    _series_counted: set[str] = field(default_factory=set, repr=False)

    @property
    def tripped(self) -> bool:
        """``True`` once either trip rule fired (latched for the pull) — **O(1)**."""
        with self._lock:
            return self._tripped

    def counts(self) -> tuple[int, int, int]:
        """``(observed, counted_empties, consecutive)`` snapshot — **O(1)**, atomic.

        A read-only telemetry/test surface (no private pokes needed to assert the
        tally): ``observed`` is every fetched window seen, ``counted_empties`` the
        empties currently held against the trip rules (post pre-listing exclusion
        and leading-empty retro-clearing), ``consecutive`` the live run length.
        """
        with self._lock:
            return (self._observed, self._empties, self._consecutive)

    def allow_settle(self) -> bool:
        """``True`` while EMPTY windows may still settle ``SKIPPED`` — **O(1)**.

        Consulted by both spines' consumer tails at ledger-record time (NOT only
        at fetch time), so a settle computed BEFORE the trip but recorded AFTER
        it (an async-pipeline queued tail, or a sibling in-flight worker) is
        still suppressed — "stop settling" holds pull-wide the instant the
        breaker trips. A disabled breaker always allows.
        """
        if not self.enabled:
            return True
        with self._lock:
            return not self._tripped

    def observe_data(self, series: str) -> None:
        """Record a data-bearing window for *series* — **O(1)**, resets the run.

        Breaks the consecutive-empty run, and — the FIRST time *series* lands
        data this pull — retro-clears its leading-empty prefix from the counted
        empties (those windows are now known to be that symbol's pre-listing /
        pre-history gap, not throttle evidence). Under a real throttle no series
        lands data, so nothing ever clears.
        """
        if not self.enabled:
            return
        with self._lock:
            self._observed += 1
            self._consecutive = 0
            if series not in self._series_seen_data:
                self._series_seen_data.add(series)
                self._empties -= self._series_leading.pop(series, 0)
                # Its counted empties were all leading (pre-first-data) — the series
                # is no longer trip evidence until a POST-data empty re-adds it.
                self._series_counted.discard(series)

    def observe_empty(self, series: str, *, symbol: str, window_end: date) -> None:
        """Record a confirmed-empty window; RAISE :class:`MassEmptySettleHalt` on/after a trip.

        The window is always counted as OBSERVED. It is counted as an EMPTY
        (and extends the consecutive run) unless the injected
        :attr:`expected_empty` predicate PROVES it expected — the register's
        rule: exclude only what the code can prove, else count it. While
        *series* has landed no data yet this pull, the empty is also tallied on
        its leading prefix so a later first-data window can retro-clear it (see
        :meth:`observe_data`). Neither rule may trip until counted empties span
        :attr:`min_series` DISTINCT series (one dead symbol is not a throttle).

        Raising here (rather than returning a flag) is deliberate: the spines
        cannot forget the halt, and an already-tripped breaker keeps raising so
        every in-flight worker stops promptly. The raise happens OUTSIDE the
        lock. **O(1)** per call.
        """
        if not self.enabled:
            return
        if self.expected_empty is not None and self.expected_empty(symbol, window_end):
            with self._lock:
                self._observed += 1
            return
        with self._lock:
            self._observed += 1
            self._empties += 1
            self._consecutive += 1
            self._series_counted.add(series)
            if series not in self._series_seen_data:
                self._series_leading[series] = self._series_leading.get(series, 0) + 1
            if not self._tripped and len(self._series_counted) >= self.min_series:
                if 0 < self.consecutive_limit <= self._consecutive:
                    self._tripped = True
                    self._trip_rule = "consecutive"
                elif (
                    self._observed >= self.min_observations
                    and self._empties / self._observed >= self.fraction_limit
                ):
                    self._tripped = True
                    self._trip_rule = "fraction"
            halt = (
                MassEmptySettleHalt(
                    empty_windows=self._empties,
                    observed_windows=self._observed,
                    consecutive_empties=self._consecutive,
                    trip_rule=self._trip_rule,
                )
                if self._tripped
                else None
            )
        if halt is not None:
            raise halt


def _resolve_int(env_name: str, default: int) -> int:
    """Read an int env override; unset / blank / non-int fall back to *default* — **O(1)**."""
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
) -> EmptySettleBreaker:
    """Build a FRESH per-pass breaker from env config (or the documented defaults).

    The single construction seam both run-ingest layers call once per pass
    (CLAUDE.md §11 COMMON) — a new instance every time, so breaker state can
    never leak across pulls or retry passes. Every knob is env-overridable
    (DYNAMIC); a bad value fails safe to its default (a mis-typed env var can
    never disable the protection by accident — only the explicit
    :data:`EMPTY_BREAKER_ENABLED_ENV` off switch can). *expected_empty* threads
    a caller's provable expected-empty knowledge (the F&O runner passes its
    confirmed-deep far-OTM strike set). **O(1)**.
    """
    raw_enabled = os.environ.get(EMPTY_BREAKER_ENABLED_ENV)
    enabled = raw_enabled is None or raw_enabled.strip().lower() not in _OFF_VALUES
    return EmptySettleBreaker(
        consecutive_limit=_resolve_int(
            EMPTY_BREAKER_CONSECUTIVE_ENV, EMPTY_BREAKER_CONSECUTIVE_LIMIT
        ),
        fraction_limit=_resolve_fraction(EMPTY_BREAKER_FRACTION_ENV, EMPTY_BREAKER_FRACTION_LIMIT),
        min_observations=_resolve_int(
            EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, EMPTY_BREAKER_MIN_OBSERVATIONS
        ),
        min_series=_resolve_int(EMPTY_BREAKER_MIN_SERIES_ENV, EMPTY_BREAKER_MIN_SERIES),
        enabled=enabled,
        expected_empty=expected_empty,
    )
