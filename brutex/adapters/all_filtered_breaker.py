"""Mass ALL-FILTERED circuit breaker — the ONE per-pull guard for both ingest spines.

DEC-ALLFILTERED-MASS-BREAKER-001. A vendor-wide timestamp/session bug can make
EVERY window's bars land on a non-trading date (or outside every session), so
the calendar-aware boundary filter (DEC-INGEST-BOUNDARY-INTEGRITY-001) drops
them ALL — and the existing guards are structurally blind to the shape:

* the mass-EMPTY breaker (:mod:`brutex.adapters.empty_circuit_breaker`) is fed
  at the PRODUCER off the RAW bar count — an all-filtered window has
  ``raw_bars > 0``, so it reads as a DATA window (and even RESETS the
  consecutive-empty run);
* the all-filtered verdict exists only in the consumer TAILS, and the ledger's
  degraded settle arm had NO suppression gate (``suppress_settle`` is
  documented "IGNORED for … degraded outcomes"), so after
  :data:`~brutex.data.window_ledger.EMPTY_TERMINAL_THRESHOLD` pulls a
  vendor-day bug quietly settled EVERY affected window ``SKIPPED``.

This module is the vendor-neutral per-pull counter fed at BOTH spine tails
(the spot ``_process_window_tail`` + the F&O ``_process_option_window_tail`` —
CLAUDE.md §11 COMMON, one implementation) exactly where the verdict is
computed, gated on the demotion's OWN eligibility (a dry-run / synthetic /
daily / session-exempt window never feeds it). Two trip rules, each keyed to a
different mass-event signature:

1. **SPREAD** — all-filtered verdicts span
   :data:`ALLFILTERED_BREAKER_SPREAD_SERIES` DISTINCT ``"<symbol>:<interval>"``
   series, where ``<symbol>`` is the spot instrument (spot spine) or the
   contract's UNDERLYING (F&O spine — ``underlying_symbol or symbol``, so a
   whole CE/PE chain is ONE series; review round 2: a per-CONTRACT key crossed
   the floor at one underlying's 10th strike and fired the vendor-wide alarm
   off a single-underlying event): the
   one-bad-vendor-day-inside-long-per-symbol-backfills shape (≈ 1% of windows
   but ~100% of series). ONE genuinely-weird symbol/underlying is 1-2 series —
   structurally below the floor on BOTH spines.
2. **FRACTION** — ``all_filtered / observed >=``
   :data:`ALLFILTERED_BREAKER_FRACTION` over ≥
   :data:`ALLFILTERED_BREAKER_MIN_OBSERVATIONS` observed windows across ≥
   :data:`ALLFILTERED_BREAKER_FRACTION_MIN_SERIES` distinct series: the
   timestamps-shifted-wholesale shape. The 3-series floor is DELIBERATE — the
   ENGINE's own spot pull is exactly 3 series (3 indexes × ``1minute``,
   CLAUDE.md §3) and MUST be trippable in its BACKFILL shape (≥
   :data:`ALLFILTERED_BREAKER_MIN_OBSERVATIONS` eligible windows — the daily
   incremental shape is BELOW the observation floor, see the honest envelope),
   while a 1-2-symbol targeted pull stays structurally below.

A trip LATCHES once per pull and the tripping caller then (i) RESCINDS the
same-pull pre-trip ``ALL_FILTERED`` settles
(:meth:`~brutex.data.window_ledger.WindowLedger.rescind_all_filtered_settles`),
(ii) emits exactly ONE count-naming structlog ERROR + CRITICAL Telegram alarm
(:func:`brutex.notifications.sweep_alerts.emit_mass_all_filtered_alert`), and
(iii) suppresses every further (and the trip-crossing) ``ALL_FILTERED``
SKIPPED-settle for the pull via the ledger's record-time
``record(allow_degraded_settle=…)`` re-check (this breaker's
:meth:`~AllFilteredBreaker.allow_settle`, evaluated UNDER the ledger lock —
see its docstring for the trip/settle race closure) — windows stay ``FAILED``
+ re-pullable. Deliberately NOT a ``_GLOBAL_STOP_ERRORS`` member: the decree is
"halt further SKIPPED-settles", not "halt the pull" — a filter-side event
poisons SETTLING, never fetching/storage.

**Honest envelope (Golden Rule #10):** the thresholds are HEURISTICS over
response shapes, not vendor facts. Blind spots (stated, not hidden — review
round 2 widened this list to the REAL envelope):

* a PARTIAL-shift bug (some bars survive the filter) is not all-filtered and
  not detected here — the per-window ``all_bars_filtered`` WARNs, the
  truncation/interior-gap machinery, and the
  ``SETTLED_SKIPPED_OPERATOR_THRESHOLD`` pull-end verdict net remain the
  backstops;
* a mass event confined to ≤ 2 series is not halted (the mass-EMPTY breaker's
  identical blind-spot class; env-tunable);
* a SMALL pull — fewer than :data:`ALLFILTERED_BREAKER_MIN_OBSERVATIONS`
  eligible windows AND fewer than :data:`ALLFILTERED_BREAKER_SPREAD_SERIES`
  distinct series — can trip NEITHER rule. The ENGINE's own DAILY INCREMENTAL
  pull is in this hole (~3 eligible windows per pass: 3 indexes × ``1minute``
  × one open window), so a persisting vendor-day bug settles those windows
  ``SKIPPED`` after ``EMPTY_TERMINAL_THRESHOLD`` such pulls with NO trip — the
  "engine pull trippable" claim above holds for the BACKFILL shape only
  (test-pinned). Lowering the observation floor trades this hole for
  small-sample false trips — a conscious operator env call, not the default;
* a LATE mass event spanning 3-9 series inside a LARGE pull whose whole-pull
  all-filtered fraction stays below :data:`ALLFILTERED_BREAKER_FRACTION`
  escapes both rules (the fraction is pull-CUMULATIVE, never windowed) —
  broader than the ≤ 2-series hole above (test-pinned);
* evidence never pools ACROSS the pipeline's SPOT and F&O phases (each phase's
  passes resolve their own breaker — deliberate, no cross-phase plumbing), so a
  vendor-day event split e.g. 5 spot series + 5 F&O underlyings trips NEITHER
  phase's SPREAD rule. WITHIN the F&O phase the futures + options market
  buckets DO share ONE per-pass breaker — ``historical_pull._one_pass`` injects
  it into every ``run_groww_option_ingest`` bucket call (review round 3: the
  original per-invocation resolve fragmented even that intra-phase evidence;
  test-pinned by the cross-bucket trip test in ``test_pull_historical.py``).

In every blind-spot shape the per-window WARNs + the streak-provenance
column (DEC-STREAK-PROVENANCE-001) keep any such settle visible + reversible
via ``brutex reopen-skipped``. Every :meth:`AllFilteredBreaker.observe` /
:meth:`~AllFilteredBreaker.allow_settle` is **O(1)** (fixed counter ops under
one lock); the per-series set is O(series) memory bounded by the pull's
worklist — the :class:`~brutex.adapters.empty_circuit_breaker.EmptySettleBreaker`
envelope verbatim.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Final

from brutex.adapters.empty_circuit_breaker import (
    OFF_ENV_VALUES,
    resolve_fraction_env,
    resolve_int_env,
)

__all__ = [
    "ALLFILTERED_BREAKER_ENABLED_ENV",
    "ALLFILTERED_BREAKER_FRACTION",
    "ALLFILTERED_BREAKER_FRACTION_ENV",
    "ALLFILTERED_BREAKER_FRACTION_MIN_SERIES",
    "ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV",
    "ALLFILTERED_BREAKER_MIN_OBSERVATIONS",
    "ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV",
    "ALLFILTERED_BREAKER_SPREAD_SERIES",
    "ALLFILTERED_BREAKER_SPREAD_SERIES_ENV",
    "AllFilteredBreaker",
    "AllFilteredTrip",
    "resolve_all_filtered_breaker",
]

#: Default SPREAD trip threshold — distinct ``"<symbol>:<interval>"`` series
#: carrying ≥ 1 all-filtered verdict (the F&O spine keys the series on the
#: contract's UNDERLYING, so one weird chain is ONE series — review round 2).
#: Heuristic (Golden Rule #1): a vendor-day
#: bug touches ~every series whose window covers the bad day, while one weird
#: symbol is 1-2 series. ``<= 0`` disables the rule (env-overridable).
ALLFILTERED_BREAKER_SPREAD_SERIES: Final[int] = 10

#: Default FRACTION trip threshold (all-filtered / observed). Heuristic: real
#: Track-1 pulls keep the boundary-filter drop rate near zero, so HALF of a
#: meaningful sample being emptied at the boundary is the vendor-bug shape.
ALLFILTERED_BREAKER_FRACTION: Final[float] = 0.5

#: Minimum observed (eligible) windows before the FRACTION rule arms — the
#: sample-size floor; ``<= 0`` arms immediately. This floor is ALSO the
#: small-pull blind spot named in the module docstring: a pull with fewer
#: eligible windows (the engine's ~3-window daily incremental shape) can never
#: arm the FRACTION rule.
ALLFILTERED_BREAKER_MIN_OBSERVATIONS: Final[int] = 20

#: Minimum DISTINCT all-filtered series before the FRACTION rule may trip.
#: DELIBERATELY 3 (not the mass-EMPTY breaker's 10): the ENGINE's own spot pull
#: is exactly 3 series (NIFTY/BANKNIFTY/SENSEX × ``1minute``) and MUST be
#: trippable under a vendor-day bug in its BACKFILL shape — ≥
#: :data:`ALLFILTERED_BREAKER_MIN_OBSERVATIONS` eligible windows; the ~3-window
#: daily incremental shape stays below the observation floor (module
#: docstring). A 1-2-symbol targeted pull stays below.
#: ``<= 1`` arms on any series.
ALLFILTERED_BREAKER_FRACTION_MIN_SERIES: Final[int] = 3

#: Env override for :data:`ALLFILTERED_BREAKER_SPREAD_SERIES` (int; ``<= 0`` disables).
ALLFILTERED_BREAKER_SPREAD_SERIES_ENV: Final[str] = "BRUTEX_ALLFILTERED_BREAKER_SPREAD_SERIES"
#: Env override for :data:`ALLFILTERED_BREAKER_FRACTION` (float in ``(0, 1]``).
ALLFILTERED_BREAKER_FRACTION_ENV: Final[str] = "BRUTEX_ALLFILTERED_BREAKER_FRACTION"
#: Env override for :data:`ALLFILTERED_BREAKER_MIN_OBSERVATIONS` (int; ``<= 0`` arms at once).
ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV: Final[str] = "BRUTEX_ALLFILTERED_BREAKER_MIN_OBSERVATIONS"
#: Env override for :data:`ALLFILTERED_BREAKER_FRACTION_MIN_SERIES` (int; ``<= 1``
#: arms on any series).
ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV: Final[str] = (
    "BRUTEX_ALLFILTERED_BREAKER_FRACTION_MIN_SERIES"
)
#: Kill switch: ``BRUTEX_ALLFILTERED_BREAKER=off|0|false|no`` disables the breaker
#: entirely (observations no-op, never trips — the sanctioned deliberate-re-settle
#: path for one pull). Default ON; shares :data:`~brutex.adapters.
#: empty_circuit_breaker.OFF_ENV_VALUES` (CLAUDE.md §11 COMMON).
ALLFILTERED_BREAKER_ENABLED_ENV: Final[str] = "BRUTEX_ALLFILTERED_BREAKER"

#: Cap on the series / window samples a trip snapshot carries (alarm brevity).
_SAMPLE_CAP: Final[int] = 10


@dataclass(frozen=True, slots=True)
class AllFilteredTrip:
    """An immutable snapshot of the breaker's state at (or after) its trip.

    Carried to :func:`brutex.notifications.sweep_alerts.emit_mass_all_filtered_alert`
    so the ONE loud alarm NAMES the counts (the error-storm breaker's
    ``_emit_vendor_down_alert`` precedent — never a count-less halt message).
    """

    trip_rule: str  # "spread" | "fraction"
    all_filtered_windows: int
    observed_windows: int
    distinct_series: int
    series_sample: tuple[str, ...]  # capped at 10, sorted (deterministic)
    window_samples: tuple[str, ...]  # capped at 10, "<series> <start>→<end>"


@dataclass(slots=True)
class AllFilteredBreaker:
    """Per-pull, thread-safe mass all-filtered counter with two trip rules.

    One instance per ingest pass (:func:`resolve_all_filtered_breaker` — fresh
    every time, Golden Rule #7: state never leaks across pulls), shared by every
    window worker/consumer of that pass. All counter ops run under ONE lock,
    each **O(1)**.

    Parameters
    ----------
    spread_series_limit
        SPREAD rule: trip once all-filtered verdicts span this many DISTINCT
        series (``<= 0`` disables the rule).
    fraction_limit
        FRACTION rule: trip once ``all_filtered / observed >= fraction_limit``
        over ``min_observations`` observed windows across
        ``fraction_min_series`` distinct series. Clamped semantics are the
        resolver's job; the raw dataclass trusts its args.
    min_observations
        Minimum observed windows before the fraction rule arms (``<= 0`` arms
        immediately).
    fraction_min_series
        Minimum DISTINCT all-filtered series before the fraction rule may trip
        (``<= 1`` arms on any series — the 3-index engine-pull floor lives on
        the DEFAULT, not here).
    enabled
        ``False`` turns every method into a no-op (the operator kill switch).
    """

    spread_series_limit: int = ALLFILTERED_BREAKER_SPREAD_SERIES
    fraction_limit: float = ALLFILTERED_BREAKER_FRACTION
    min_observations: int = ALLFILTERED_BREAKER_MIN_OBSERVATIONS
    fraction_min_series: int = ALLFILTERED_BREAKER_FRACTION_MIN_SERIES
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _observed: int = field(default=0, repr=False)
    _all_filtered: int = field(default=0, repr=False)
    #: Series (``"<symbol>:<interval>"``) that contributed ≥ 1 all-filtered
    #: verdict this pull — BOTH rules' distinct-series evidence.
    _series: set[str] = field(default_factory=set, repr=False, compare=False)
    #: First ≤ :data:`_SAMPLE_CAP` all-filtered windows, ``"<series> <start>→<end>"``.
    _samples: list[str] = field(default_factory=list, repr=False, compare=False)
    _tripped: bool = field(default=False, repr=False)
    _trip_rule: str = field(default="", repr=False)

    @property
    def tripped(self) -> bool:
        """``True`` once either trip rule fired (latched for the pull) — **O(1)**."""
        with self._lock:
            return self._tripped

    def counts(self) -> tuple[int, int, int]:
        """``(observed, all_filtered, distinct_series)`` snapshot — **O(1)**, atomic."""
        with self._lock:
            return (self._observed, self._all_filtered, len(self._series))

    def allow_settle(self) -> bool:
        """``True`` while ``ALL_FILTERED`` windows may still settle ``SKIPPED`` — **O(1)**.

        Threaded by both spines' consumer tails as
        :meth:`~brutex.data.window_ledger.WindowLedger.record`'s
        ``allow_degraded_settle`` callable and consulted INSIDE ``record()``
        UNDER the ledger's own lock (review round 1 — never pre-computed in the
        tail: a tail-time boolean raced the tripping sibling's one-shot
        rescind, letting a settle in flight at the trip instant escape BOTH the
        suppression and the rescind). Because the trip latches before the
        rescind and ``record``/``rescind_all_filtered_settles`` serialize on
        the ledger lock, a racing settle is either suppressed (record runs
        after the rescind) or captured in ``_af_settled_keys`` and reversed by
        the rescind (record ran first) — "stop settling" holds pull-wide from
        the trip, with no leakable interleaving. A disabled breaker always
        allows.
        """
        if not self.enabled:
            return True
        with self._lock:
            return not self._tripped

    def observe(
        self,
        series: str,
        *,
        all_filtered: bool,
        window_start: str = "",
        window_end: str = "",
    ) -> bool:
        """Count ONE eligible window's boundary verdict; report the trip TRANSITION.

        Returns ``True`` EXACTLY ONCE — on the observation that transitions the
        latch ``False`` → ``True`` (the caller then rescinds + alarms). A
        disabled breaker counts nothing and always returns ``False``; post-trip
        observations keep counting (telemetry for :meth:`trip_snapshot`) but
        never return ``True`` again. Both rules are evaluated under the lock
        AFTER counting, so the trip-crossing window can suppress ITSELF at its
        own record. **O(1)** per call.
        """
        if not self.enabled:
            return False
        with self._lock:
            self._observed += 1
            if all_filtered:
                self._all_filtered += 1
                self._series.add(series)
                if len(self._samples) < _SAMPLE_CAP:
                    self._samples.append(f"{series} {window_start}→{window_end}")
            if self._tripped:
                return False
            self._trip_rule = self._tripping_rule_locked()
            self._tripped = bool(self._trip_rule)
            return self._tripped

    def trip_snapshot(self) -> AllFilteredTrip | None:
        """The count-carrying trip snapshot — ``None`` until tripped, atomic."""
        with self._lock:
            if not self._tripped:
                return None
            return AllFilteredTrip(
                trip_rule=self._trip_rule,
                all_filtered_windows=self._all_filtered,
                observed_windows=self._observed,
                distinct_series=len(self._series),
                series_sample=tuple(sorted(self._series)[:_SAMPLE_CAP]),
                window_samples=tuple(self._samples),
            )

    def _tripping_rule_locked(self) -> str:
        """Evaluate both trip rules; return the tripping rule's name or ``""`` — **O(1)**.

        Caller holds ``_lock`` and has already counted this observation. SPREAD
        first (the more specific vendor-day signature); each rule's gates are
        documented on its default constant.
        """
        if 0 < self.spread_series_limit <= len(self._series):
            return "spread"
        if (
            self._observed >= self.min_observations
            and len(self._series) >= max(1, self.fraction_min_series)
            and self._all_filtered / self._observed >= self.fraction_limit
        ):
            return "fraction"
        return ""


def resolve_all_filtered_breaker() -> AllFilteredBreaker:
    """Build a FRESH per-pass breaker from env config (or the documented defaults).

    The single construction seam (CLAUDE.md §11 COMMON), called once per pass:
    the spot spine inside :func:`~brutex.adapters.groww.run_ingest.
    run_groww_spot_ingest` (one invocation per pass), the production F&O pass
    loop in ``historical_pull._one_pass`` (ONE instance injected into every
    market bucket's ``run_groww_option_ingest`` call — review round 3, so the
    futures + options buckets pool evidence), and the F&O driver's ``None``
    fallback for direct callers — a new instance every time, so breaker state
    can never leak across pulls or retry passes (Golden Rule #7). Every knob is
    env-overridable (DYNAMIC); a bad value fails safe to its default via the
    SHARED :func:`~brutex.adapters.empty_circuit_breaker.resolve_int_env` /
    :func:`~brutex.adapters.empty_circuit_breaker.resolve_fraction_env`
    resolvers (a mis-typed env var can never disable the protection by accident
    — only the explicit :data:`ALLFILTERED_BREAKER_ENABLED_ENV` off switch
    can). **O(1)**.
    """
    raw_enabled = os.environ.get(ALLFILTERED_BREAKER_ENABLED_ENV)
    enabled = raw_enabled is None or raw_enabled.strip().lower() not in OFF_ENV_VALUES
    return AllFilteredBreaker(
        spread_series_limit=resolve_int_env(
            ALLFILTERED_BREAKER_SPREAD_SERIES_ENV, ALLFILTERED_BREAKER_SPREAD_SERIES
        ),
        fraction_limit=resolve_fraction_env(
            ALLFILTERED_BREAKER_FRACTION_ENV, ALLFILTERED_BREAKER_FRACTION
        ),
        min_observations=resolve_int_env(
            ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV, ALLFILTERED_BREAKER_MIN_OBSERVATIONS
        ),
        fraction_min_series=resolve_int_env(
            ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV,
            ALLFILTERED_BREAKER_FRACTION_MIN_SERIES,
        ),
        enabled=enabled,
    )
