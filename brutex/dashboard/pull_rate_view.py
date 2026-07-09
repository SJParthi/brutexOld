"""Live pull-rate view — req/sec · req/min · req/hour + a stall verdict (DEC-LIVE-RATE-MONITOR-001).

The dashboard already mirrors every live Groww-pull rate snapshot into an in-memory
:class:`~brutex.dashboard.app.RateLimitPanel` on each ``_observe_rate`` tick
(:func:`brutex.dashboard.app.make_rate_observer` ← the
:meth:`~brutex.adapters.rate_common.RateController.snapshot` ``current_rps`` /
``recent_minute`` / ``recent_hits`` + the transport
:meth:`~brutex.adapters.call_meter.CallMeter.snapshot` ``effective_rps``). That panel
powers the existing ``/api/v1/rate`` poll. What it does NOT carry is a HONEST
**idle / stall** signal: the panel's ``current_rps`` / ``effective_rps`` are the LAST
values a pull pushed, so AFTER a pull ends (or when none has run) the panel keeps
showing the final live numbers — a poll could read a fake "8 r/s flowing" on an idle
process (the illusion Golden Rule #1 forbids).

This module is the pure read-side projection that closes that gap. Given the panel +
the wall-clock age of its last update, :func:`build_pull_rate_view` derives a
:class:`PullRateView` carrying:

* **req/sec** — ``achieved`` (the transport's measured ``effective_rps``) vs ``allowed``
  (the controller's current AIMD ``current_rps``);
* **req/min** — the controller's rolling-60s ISSUE count (``recent_minute``) — a MEASURED
  rolling window, NOT a per-second figure multiplied up;
* **req/hour** — an OBSERVED / DERIVED projection ``req/min × 60`` (so labelled — there is
  no measured trailing-hour window; it is the current minute-rate extrapolated), and
* a **stall signal**: ``seconds_since_update`` (how long since the last live tick) + the
  ``stalled`` verdict the pull-health monitor's ``pull.stalled`` rule encodes
  (achieved r/s below a floor while a pull is ACTIVELY ticking).

## The five flow states (honest, never fabricated)

* ``idle`` — no live tick within ``stale_after_s`` (no pull running / the pull ended a
  while ago): the view ZEROES every live number so the widget shows a dark "idle" light,
  never the last live figure (Golden Rule #1 — never a fake "live" number when idle).
* ``done`` — the caller KNOWS no pull is running (``pull_active=False`` —
  DEC-RATE-STALL-PULL-GATE-001) while the last tick is still fresh: the pull JUST
  finished. Every live number is zeroed and the light is dark — NEVER the red
  "stalled" the pre-fix projection showed right after a successful completion (the
  operator's 80/80-complete pull read "PULL RATE stalled": honest counts, wrong word).
  Once the tick ages past ``stale_after_s`` this settles to plain ``idle``.
* ``stalled`` — a pull IS ACTIVE but its req/sec has flatlined: either a fresh tick with
  achieved r/s at/below :data:`STALL_RPS_FLOOR`, or (when the caller asserts
  ``pull_active=True``) NO tick within ``stale_after_s`` at all → a RED light. STALLED
  is NEVER shown when the caller knows no pull is running.
* ``failing`` — the meter-fed variant of ``stalled`` where the transport meter's MEASURED
  ``failed_rps`` shows calls actively burning while the completed-rate sits at/below the
  floor (an issuing-but-ALL-FAILING pull — DEC-PULL-TELEMETRY-TRIAD-001, the #1022
  follow-up): a RED light with the honest "failing" word; ``stalled`` stays ``True``.
* ``flowing`` — a recent tick with achieved r/s above the floor → a GREEN light.
* ``local`` — a pull IS ACTIVE but its CURRENT pipeline phase is LOCAL processing
  (``phase`` ∈ :data:`LOCAL_PHASES` — the timeframe prebuild + the movers warm, which
  make ZERO vendor calls by design): an AMBER light labelled
  ``"local processing — <phase> (no vendor calls)"`` (carried in
  :attr:`PullRateView.flow_detail`), never the red "stalled" the vendor-rate heuristics
  would read off the correctly-flatlined fetch rate
  (DEC-PIPELINE-PHASE-UI-HONESTY-001). Keyed STRICTLY on the phase label — a
  zero-admission FETCH phase (spot/metadata/historical) still reads STALLED
  (DEC-RATE-WIDGET-FNO-HONEST-001's pins are untouched).

``pull_active=None`` (the default) means the caller has NO pull-run signal (e.g. the
cross-process CLI bridge, which carries no active flag) — the projection then falls back
to the pre-existing tick-freshness heuristic, byte-identical to the pre-gate behaviour.

## Honest envelope (Golden Rule #10)

req/sec + req/min are MEASURED from the rate controller / call meter; req/hour is an
OBSERVED extrapolation of the current minute-rate (``× 60``), NOT a measured trailing-hour
count — the field name + the widget label both say "observed". The limits shown
(:data:`CEILING_RPS_LIMIT` 8 r/s · :data:`CEILING_PER_MIN_LIMIT` 500/min) are the
configured ceilings (``GROWW_RATE_LIMIT_RPS_CEILING`` / ``GROWW_RATE_LIMIT_PER_MIN``,
CLAUDE.md §6) — the per-second cap itself is officially UNVERIFIED, and these are the
INJECTED production ceilings, not a vendor-documented limit. The ``stalled`` verdict is an
OBSERVATION (the same heuristic the pull-health monitor alerts on), never an
auto-remediation. This is a READ-ONLY projection — it changes NO pacing decision and the
brute-force ENGINE (NIFTY/BANKNIFTY/SENSEX) is untouched.

## Decision: DEC-LIVE-RATE-MONITOR-001 (MIGRATION.md entry #995).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Final

from brutex.adapters.groww.constants import (
    GROWW_RATE_LIMIT_PER_MIN,
    GROWW_RATE_LIMIT_RPS_CEILING,
)
from brutex.observability.pull_health import STALLED_RPS_FLOOR

#: Seconds after the last live rate tick before the pull is treated as IDLE (no live
#: numbers shown). The dashboard pollers run every ~2-4 s and the pull pushes a tick per
#: processed window (~1 s cadence under load); 12 s tolerates a brief between-window /
#: between-instrument gap while still flipping to idle within a few polls once a pull ends
#: — so an ended pull never lingers as a fake "live" reading (Golden Rule #1).
STALE_AFTER_S: Final[float] = 12.0

#: Maximum CLI-bridge snapshot age (seconds) the merged projection will still TRUST
#: (DEC-RATE-METER-LIFECYCLE-001, audit C3). The CLI publishes per processed window
#: (~1 s cadence under load); its worst LEGITIMATE silent gap is bounded by one
#: fetch+write cycle — a 2-3-chunk window at the 1 r/s AIMD deep floor, plus the
#: retryable-error ladders, plus the 90 s ``GROWW_WRITE_DEADLINE_S`` hung-write bound,
#: plus a token reauth — comfortably under 900 s (= 75 × :data:`STALE_AFTER_S`). Beyond
#: it the bridge file is DEAD EVIDENCE (a SIGKILLed CLI's stuck ``pull_active=True``
#: stamp — the in-process ``finally`` that flips it False never ran) and is treated as
#: ABSENT, so a dead CLI can no longer fabricate a PERMANENT site-wide red "stalled" on
#: an idle dashboard. Within the bound (12 s < age ≤ 900 s) the red alert window is
#: PRESERVED — a CLI that died mid-run still reads honestly stalled for 15 minutes.
#: A POLICY heuristic derived from the cadence arithmetic above, NOT a vendor fact
#: (Golden Rule #10) — operator-revisable in this one Final; the literal is test-pinned.
BRIDGE_MAX_AGE_S: Final[float] = 900.0

#: How far a CLI-bridge ``wall_time`` may sit AHEAD of the reader's wall clock and still
#: be trusted as fresh (seconds). The bridge is published by a possibly SEPARATE process
#: — potentially on another host under DEC-ACCOUNT-LEASE-CROSSHOST-001 — so a few seconds
#: of unsynchronised-NTP clock skew is ordinary and must NOT flip a live pull to idle.
#: Beyond it a ``wall_time`` in the future is DEAD EVIDENCE (a ms-scaled epoch, a corrupt
#: stamp, or a badly-skewed clock), exactly like a non-finite one — see
#: :func:`bridge_age_seconds`. A POLICY heuristic (Golden Rule #10), operator-revisable in
#: this one Final; the literal is test-pinned.
BRIDGE_FUTURE_SKEW_TOLERANCE_S: Final[float] = 60.0

#: Achieved-r/s floor at/below which an ACTIVELY-ticking pull reads STALLED. Reuses the
#: pull-health monitor's :data:`~brutex.observability.pull_health.STALLED_RPS_FLOOR`
#: (0.5 r/s) so the widget's red light and the monitor's ``pull.stalled`` Telegram alert
#: agree on what "stalled" means (CLAUDE.md §11 COMMON — one source of truth).
STALL_RPS_FLOOR: Final[float] = STALLED_RPS_FLOOR

#: The configured production per-second ceiling shown as the r/s LIMIT
#: (``GROWW_RATE_LIMIT_RPS_CEILING``; CLAUDE.md §6 / DEC-GROWW-RATE-CEILING-8-002). The
#: per-second backtesting cap itself is officially UNVERIFIED — this is the injected
#: ceiling, not a vendor-documented limit.
CEILING_RPS_LIMIT: Final[float] = GROWW_RATE_LIMIT_RPS_CEILING

#: The HARD per-minute governor cap shown as the req/min LIMIT
#: (``GROWW_RATE_LIMIT_PER_MIN``; operator-confirmed 500/min, CLAUDE.md §6 /
#: DEC-GROWW-RATE-PERMIN-001 — NOT vendor-documented).
CEILING_PER_MIN_LIMIT: Final[int] = GROWW_RATE_LIMIT_PER_MIN

#: Flow-state string constants (the widget colours the stall light off these).
FLOW_IDLE: Final[str] = "idle"
FLOW_FLOWING: Final[str] = "flowing"
FLOW_STALLED: Final[str] = "stalled"
#: The pull JUST finished (``pull_active=False`` while the last tick is still fresh) —
#: dark light, honest label, never a red "stalled" on a completed pull
#: (DEC-RATE-STALL-PULL-GATE-001).
FLOW_DONE: Final[str] = "done"
#: An ACTIVE pull is in a LOCAL processing phase (zero vendor calls by design) —
#: an AMBER light, never the red "stalled" the vendor-rate heuristics would read off
#: the (correctly) flatlined fetch rate (DEC-PIPELINE-PHASE-UI-HONESTY-001).
FLOW_LOCAL: Final[str] = "local"

#: The pipeline ``set_phase`` labels that are LOCAL processing — zero vendor calls by
#: design (the timeframe prebuild writes derived partitions from the stored 1-minute;
#: the movers warm re-ranks cached/lake data): DEC-PIPELINE-AUTOPREBUILD-001's two
#: post-fetch stages. The FLOW_LOCAL verdict keys STRICTLY on ``phase in LOCAL_PHASES``
#: (never on "no admissions" / meter absence), so a zero-admission FETCH phase — spot /
#: metadata / historical — STILL reads STALLED exactly as DEC-RATE-WIDGET-FNO-HONEST-001
#: pinned (the fake-alive guard is untouched).
LOCAL_PHASES: Final[frozenset[str]] = frozenset({"prebuild", "movers"})

#: An ISSUING-but-ALL-FAILING pull (DEC-PULL-TELEMETRY-TRIAD-001, closing the #1022
#: flagged follow-up): the meter's MEASURED completed-rate sits at/below the stall floor
#: while its MEASURED ``failed_rps`` shows calls actively burning — the honest verdict is
#: this DISTINCT red "failing" (calls are being spent and every one fails), not the
#: ambiguous "stalled" (nothing happening at all). ``stalled`` stays ``True`` for it, so
#: every existing red-light consumer keeps treating it as degraded.
FLOW_FAILING: Final[str] = "failing"


@dataclass(frozen=True, slots=True)
class PullRateView:
    """Immutable live pull-rate projection for the ``/api/v1/pull/rate`` poll.

    Attributes
    ----------
    flow_state
        One of :data:`FLOW_IDLE` / :data:`FLOW_DONE` / :data:`FLOW_FLOWING` /
        :data:`FLOW_STALLED` — the widget's stall-light colour (dark / dark / green /
        red). ``done`` = the caller knows no pull is running while the last tick is
        still fresh (a pull just completed — DEC-RATE-STALL-PULL-GATE-001).
    active
        ``True`` iff the view carries LIVE numbers — a rate tick landed within
        ``stale_after_s`` and the pull is not known-finished. When ``False`` every
        live number below is zeroed.
    stalled
        ``True`` iff a pull is ACTIVE but its req/sec has flatlined (achieved r/s
        at/below :data:`STALL_RPS_FLOOR`, or — under an explicit ``pull_active=True`` —
        no tick within the staleness window at all). NEVER ``True`` when the caller
        knows no pull is running (``pull_active=False``).
    req_per_sec_achieved
        The transport's MEASURED ``effective_rps`` (calls ÷ wall-span). Zeroed when idle.
    req_per_sec_allowed
        The controller's current AIMD ``current_rps`` (the allowed pace). Zeroed when idle.
    req_per_min
        The controller's MEASURED rolling-60s issue count (``recent_minute``). Zeroed when
        idle.
    req_per_hour_observed
        An OBSERVED / DERIVED extrapolation ``req_per_min × 60`` — the current minute-rate
        projected to an hour, NOT a measured trailing-hour count (Golden Rule #10). Zeroed
        when idle. RETAINED for back-compat alongside the new MEASURED
        :attr:`req_per_hour_measured`.
    req_per_sec_count
        The MEASURED trailing-1 s issue count (``recent_1s``) — the EVIDENCE behind req/sec
        (``rps == this count over 1 s``; DEC-RATE-MONITOR-EXPAND-001). Zeroed when idle.
    req_per_10min
        The MEASURED trailing-600 s issue count (``recent_600s``) — the NEW req/10min metric,
        a REAL count of issues in the trailing ten minutes (DEC-RATE-MONITOR-EXPAND-001), not
        a per-minute figure multiplied up. Zeroed when idle.
    req_per_hour_measured
        The MEASURED trailing-3600 s issue count (``recent_3600s``) — a REAL trailing-hour
        count from actual issue timestamps (DEC-RATE-MONITOR-EXPAND-001), the TRUSTWORTHY
        companion to ``req_per_hour_observed``. During the first hour of a pull it is the true
        count SO FAR (an honest partial-hour count that grows as the window fills, never
        inflated). Zeroed when idle.
    req_per_min_rate
        The controller's ADMISSION-derived per-minute average expressed as an r/s figure
        (``recent_minute / 60``) — DEC-RATE-WIDGET-FNO-HONEST-001. This is the HONEST
        companion to :attr:`req_per_sec_achieved` for the ticks where NO transport
        :class:`~brutex.adapters.call_meter.CallMeter` is registered — the
        pre-registration window at pull start, a dry-run (which explicitly DEREGISTERS)
        or after the job-finish clear (DEC-RATE-METER-LIFECYCLE-001; every real pull's
        metadata + F&O phases are meter-FED since DEC-RATE-WIDGET-FNO-MEASURED-001's
        build-site registrations): it is a per-minute ADMISSION average — calls the
        controller ISSUED ÷ 60 — NOT a measured completed throughput, so it is surfaced in its
        OWN field + labelled "req/min rate" in the widget, never poured into the "achieved"
        (measured completed) slot (the over-report + fake-alive bug this decision fixes). Zeroed
        when idle. Always populated (it is a pure projection of ``recent_minute``), so the spot
        phase also carries it as a secondary view alongside its real ``req_per_sec_achieved``.
    req_per_sec_failed
        The transport meter's MEASURED rolling failed-calls rate (``failed_rps`` — calls
        that FAILED per second over the meter's trailing window;
        DEC-PULL-TELEMETRY-TRIAD-001). The evidence behind the :data:`FLOW_FAILING`
        verdict: an issuing-but-all-failing pull shows the burn rate here while
        ``req_per_sec_achieved`` honestly reads ~0. ``0.0`` when the meter is absent
        (the admission-only phases push no failure measurement) and zeroed when idle.
    recent_429_count
        Recent vendor 429 hits (``recent_hits``) — context for WHY the achieved rate may
        sit below the ceiling. Zeroed when idle.
    seconds_since_update
        Wall-clock seconds since the last live tick (``inf`` when no tick has EVER landed).
        Always real — the staleness gauge the widget shows even while idle.
    ceiling_rps
        The configured per-second ceiling LIMIT (:data:`CEILING_RPS_LIMIT`).
    ceiling_per_min
        The HARD per-minute governor LIMIT (:data:`CEILING_PER_MIN_LIMIT`).
    flow_detail
        A human-readable verdict detail for the widget's expanded view
        (DEC-PIPELINE-PHASE-UI-HONESTY-001). Non-empty ONLY for
        :data:`FLOW_LOCAL` — ``"local processing — <phase> (no vendor calls)"``,
        naming the pipeline phase the amber light stands for. Every other flow
        state carries ``""`` (the widget falls back to the flow-state word), so
        the pre-existing wire payloads gain one additive field and change no
        existing value.
    """

    flow_state: str
    active: bool
    stalled: bool
    req_per_sec_achieved: float
    req_per_sec_allowed: float
    req_per_min: int
    req_per_hour_observed: int
    req_per_sec_count: int
    req_per_10min: int
    req_per_hour_measured: int
    req_per_min_rate: float
    req_per_sec_failed: float
    recent_429_count: int
    seconds_since_update: float
    ceiling_rps: float
    ceiling_per_min: int
    flow_detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable mapping (``inf`` ``seconds_since_update`` → ``None``).

        ``json.dumps`` cannot encode ``inf`` as valid JSON, so a never-yet-updated panel's
        ``inf`` staleness is rendered as ``None`` (the widget reads ``null`` as "never").
        """
        data = asdict(self)
        if math.isinf(self.seconds_since_update):
            data["seconds_since_update"] = None
        return data


def build_pull_rate_view(
    *,
    effective_rps: float,
    current_rps: float,
    recent_minute: int,
    recent_429_count: int,
    last_update_monotonic: float,
    now_monotonic: float,
    recent_1s: int = 0,
    recent_600s: int = 0,
    recent_3600s: int = 0,
    meter_present: bool = True,
    failed_rps: float = 0.0,
    pull_active: bool | None = None,
    phase: str | None = None,
    stale_after_s: float = STALE_AFTER_S,
    stall_rps_floor: float = STALL_RPS_FLOOR,
) -> PullRateView:
    """Derive the :class:`PullRateView` from a rate panel's live fields + its update age.

    Pure + fully injectable (no clock / global state) so a test drives it with explicit
    values and asserts the idle / done / flowing / stalled projection deterministically.

    Parameters
    ----------
    effective_rps
        The transport's measured achieved r/s (panel ``effective_rps``).
    current_rps
        The controller's current allowed r/s (panel ``current_rps``).
    recent_minute
        The controller's rolling-60s issue count (panel ``recent_minute``).
    recent_429_count
        Recent vendor 429 hits (panel ``recent_429_count``).
    last_update_monotonic
        The monotonic timestamp of the panel's last live tick (``-inf`` when never
        updated — the panel's default).
    now_monotonic
        The current monotonic clock reading (the route passes ``time.monotonic()``).
    recent_1s, recent_600s, recent_3600s
        The controller's MEASURED trailing 1 s / 600 s / 3600 s real issue counts
        (DEC-RATE-MONITOR-EXPAND-001) — the evidence behind req/sec, req/10min + the MEASURED
        req/hour. Default ``0`` (an older controller / idle), so a panel that never carried
        them projects honestly zeroed.
    meter_present
        Whether the panel's ``effective_rps`` is a REAL measured completed-rate from the
        transport :class:`~brutex.adapters.call_meter.CallMeter` (DEC-RATE-WIDGET-FNO-HONEST-001).
        ``True`` (the default — any tick fed by the REGISTERED pool meter's real snapshot:
        the spot per-window observe AND, since DEC-RATE-WIDGET-FNO-MEASURED-001, the
        sampler's metadata/F&O ticks) ⇒ the ``stalled`` verdict reads the MEASURED
        ``effective_rps`` (an issued-but-all-failing pull reads STALLED because its
        completed-rate is at/below the floor — the fake-alive case). ``False`` means
        exactly "no meter registered" — the pre-registration window at pull start, a
        dry-run (which explicitly DEREGISTERS) or after the job-finish clear
        (DEC-RATE-METER-LIFECYCLE-001) ⇒ there is NO completed-rate measurement, so the
        verdict falls back to the controller's ADMISSION signal (``recent_minute == 0`` ⇒
        stalled / idle) and ``req_per_sec_achieved`` stays a HONEST ``0`` (no fabricated
        completed-rate), the per-minute admission average being carried separately in
        :attr:`~PullRateView.req_per_min_rate`.
    failed_rps
        The transport meter's MEASURED rolling failed-calls rate (the meter snapshot's
        ``failed_rps`` — DEC-PULL-TELEMETRY-TRIAD-001, closing the #1022 flagged
        follow-up). On the METER-FED path (``meter_present=True``) a completed-rate
        at/below the stall floor WITH ``failed_rps > 0`` reads the distinct
        :data:`FLOW_FAILING` verdict — the pull IS burning calls and every one fails —
        instead of the ambiguous "stalled" (``stalled`` stays ``True`` either way, so
        red-light consumers are unchanged). The default ``0.0`` (an older panel / no
        meter / no failures) is BYTE-IDENTICAL to the pre-decision projection.
    pull_active
        The caller's AUTHORITATIVE pull-run signal (DEC-RATE-STALL-PULL-GATE-001).
        ``False`` = the caller KNOWS no pull is running (the dashboard's
        ``BackgroundPullJob.running`` is ``False``) ⇒ the verdict can NEVER be
        ``stalled``: a still-fresh tick reads :data:`FLOW_DONE` (the pull just
        finished), a stale one plain :data:`FLOW_IDLE` — closing the "PULL RATE
        stalled right after a successful 80/80 completion" mislabel. ``True`` = a pull
        IS running ⇒ a fresh tick keeps the measured flowing/stalled verdict, and NO
        tick within ``stale_after_s`` reads ``stalled`` (an active pull whose rate
        ticks flatlined). ``None`` (the default) = no signal available (the CLI
        bridge) ⇒ the pre-existing tick-freshness heuristic, byte-identical.
    phase
        The pipeline's CURRENT coarse ``set_phase`` label
        (DEC-PIPELINE-PHASE-UI-HONESTY-001), or ``None`` when the caller has no
        phase signal (the CLI bridge / a non-pipeline pull) — the default,
        byte-identical to the pre-phase behaviour. When ``pull_active is True``
        AND ``phase`` ∈ :data:`LOCAL_PHASES` (``prebuild`` / ``movers`` — LOCAL
        processing, zero vendor calls by design) the verdict is
        :data:`FLOW_LOCAL` (amber, never ``stalled``) with
        :attr:`~PullRateView.flow_detail` naming the phase. Keyed STRICTLY on
        the phase label, NEVER on zero admissions / meter absence — a
        zero-admission fetch phase (``spot``/``metadata``/``historical``) still
        reads STALLED exactly as DEC-RATE-WIDGET-FNO-HONEST-001 pinned.
    stale_after_s
        Idle threshold (default :data:`STALE_AFTER_S`).
    stall_rps_floor
        Achieved-r/s stall floor (default :data:`STALL_RPS_FLOOR`).

    Returns
    -------
    PullRateView
        The honest projection: idle ⇒ every live number zeroed; active ⇒ the measured
        values + a flowing / stalled verdict.
    """
    # ``seconds_since_update`` is ALWAYS real (even while idle) — it is the staleness gauge.
    # A never-updated panel (``-inf``) yields ``inf`` (rendered ``None`` by ``to_dict``).
    if math.isinf(last_update_monotonic):
        seconds_since_update = math.inf
    else:
        # Clamp ``>= 0`` so a monotonic clock that has not advanced between the panel write
        # and this read can never read as a negative age.
        seconds_since_update = max(0.0, now_monotonic - last_update_monotonic)

    tick_fresh = seconds_since_update <= stale_after_s

    def _quiet(flow_state: str, *, stalled: bool, flow_detail: str = "") -> PullRateView:
        """A zeroed (no live numbers) view — the limits + staleness stay real."""
        return PullRateView(
            flow_state=flow_state,
            active=False,
            stalled=stalled,
            flow_detail=flow_detail,
            req_per_sec_achieved=0.0,
            req_per_sec_allowed=0.0,
            req_per_min=0,
            req_per_hour_observed=0,
            req_per_sec_count=0,
            req_per_10min=0,
            req_per_hour_measured=0,
            req_per_min_rate=0.0,
            req_per_sec_failed=0.0,
            recent_429_count=0,
            seconds_since_update=seconds_since_update,
            ceiling_rps=CEILING_RPS_LIMIT,
            ceiling_per_min=CEILING_PER_MIN_LIMIT,
        )

    if pull_active is False:
        # The caller KNOWS no pull is running (DEC-RATE-STALL-PULL-GATE-001): the verdict
        # can NEVER be "stalled". A still-fresh tick means the pull JUST finished — an
        # honest dark "done", never the red mislabel the operator saw right after an
        # 80/80-complete pull; once the tick ages out it settles to plain "idle". Every
        # live number is zeroed either way (the last live figures are history, not a
        # current rate — Golden Rule #1).
        return _quiet(FLOW_DONE if tick_fresh else FLOW_IDLE, stalled=False)

    if pull_active is True and phase in LOCAL_PHASES:
        # An ACTIVE pull in a LOCAL processing phase (DEC-PIPELINE-PHASE-UI-HONESTY-001):
        # the prebuild / movers-warm stages make ZERO vendor calls by design, so the
        # vendor-rate heuristics below (measured completed-rate at/below the floor, zero
        # admissions, or even a stale tick) would read a perfectly healthy local stage as
        # a red "stalled" — a dishonest alarm. The verdict is AMBER "local" with the
        # phase named in ``flow_detail``; the live vendor numbers are zeroed (there IS no
        # vendor traffic to show — Golden Rule #1). Checked BEFORE the tick-freshness
        # stall so a between-tick gap during a local stage can never read red either.
        # STRICTLY phase-keyed: a fetch phase (spot/metadata/historical) never lands here,
        # so every DEC-RATE-WIDGET-FNO-HONEST-001 stall pin below is untouched.
        return _quiet(
            FLOW_LOCAL,
            stalled=False,
            flow_detail=f"local processing — {phase} (no vendor calls)",
        )

    if not tick_fresh:
        if pull_active is True:
            # A pull IS running but NO rate tick landed within the staleness window —
            # its req/sec has flatlined entirely: the honest verdict is STALLED (red),
            # with the live numbers zeroed (there is no fresh measurement to show).
            return _quiet(FLOW_STALLED, stalled=True)
        # No pull-run signal (pull_active=None — the CLI bridge): the pre-gate
        # tick-freshness heuristic, byte-identical — a stale tick reads idle.
        return _quiet(FLOW_IDLE, stalled=False)

    # The ADMISSION-derived per-minute average expressed as r/s (DEC-RATE-WIDGET-FNO-HONEST-001):
    # what the controller ISSUED ÷ 60. It is NOT a measured completed throughput — surfaced in
    # its own field + labelled "req/min rate" in the widget, never the "achieved" slot.
    req_per_min_rate = recent_minute / 60.0

    if meter_present:
        # The meter-fed path: ``effective_rps`` is the transport CallMeter's MEASURED
        # completed-rate, so the verdict reads it directly — an issued-but-all-failing pull
        # (admissions happening but nothing COMPLETING) reads at/below the floor. With the
        # meter's MEASURED ``failed_rps`` now threaded in (DEC-PULL-TELEMETRY-TRIAD-001),
        # that case splits honestly: failures actively burning ⇒ the DISTINCT "failing"
        # verdict (calls spent, all failing — the #1022 flagged follow-up); no failures ⇒
        # the plain "stalled" (nothing happening). ``failed_rps=0.0`` (the default — an
        # older panel / no failures) keeps the projection byte-identical.
        stalled = effective_rps <= stall_rps_floor
        failing = stalled and failed_rps > 0.0
        achieved = effective_rps
        measured_failed_rps = failed_rps
    else:
        # NO meter is registered (DEC-RATE-METER-LIFECYCLE-001 — the pre-registration
        # window at pull start, a dry-run [which explicitly deregisters], or after the
        # job-finish clear; the metadata + F&O phases of a REAL pull are meter-FED since
        # DEC-RATE-WIDGET-FNO-MEASURED-001), so there is NO measured completed-rate.
        # Fall back to the controller's ADMISSION signal — a pull with
        # ZERO admissions in the trailing minute is idle/stalled; any admissions ⇒ flowing — and
        # keep ``achieved`` an HONEST 0 (never the fabricated ``recent_minute / 60`` the old
        # sampler poured into this slot, which both over-reported AND blinded the stall verdict).
        # NO ``failing`` verdict here: with no meter there is no measured failure rate to
        # evidence it (the honest envelope — never a fabricated "failing").
        stalled = recent_minute == 0
        failing = False
        achieved = 0.0
        measured_failed_rps = 0.0

    return PullRateView(
        flow_state=FLOW_FAILING if failing else (FLOW_STALLED if stalled else FLOW_FLOWING),
        active=True,
        stalled=stalled,
        req_per_sec_achieved=achieved,
        req_per_sec_allowed=current_rps,
        req_per_min=recent_minute,
        # OBSERVED extrapolation: the current rolling-minute rate × 60 (Golden Rule #10 —
        # NOT a measured trailing-hour count; the field name + widget label say "observed").
        req_per_hour_observed=recent_minute * 60,
        # MEASURED trailing-window real issue counts (DEC-RATE-MONITOR-EXPAND-001) — the
        # trust/transparency evidence: each is a genuine count of issues in its window.
        req_per_sec_count=recent_1s,
        req_per_10min=recent_600s,
        req_per_hour_measured=recent_3600s,
        req_per_min_rate=req_per_min_rate,
        # The MEASURED failed-calls rate rides along as evidence on the active view; the
        # meter-less phases carry an honest 0.0 (no failure measurement exists there).
        req_per_sec_failed=measured_failed_rps,
        recent_429_count=recent_429_count,
        seconds_since_update=seconds_since_update,
        ceiling_rps=CEILING_RPS_LIMIT,
        ceiling_per_min=CEILING_PER_MIN_LIMIT,
    )


def bridge_age_seconds(
    now_wall: float,
    wall_time: float,
    *,
    future_skew_s: float = BRIDGE_FUTURE_SKEW_TOLERANCE_S,
) -> tuple[float, bool]:
    """Derive a bridge snapshot's honest age (seconds) + a DEAD-EVIDENCE verdict.

    DEC-RATE-METER-LIFECYCLE-001 audit-C3, hardened. The route computed the bridge
    age as ``max(0.0, now_wall - wall_time)``, which SILENTLY CLAMPED a non-finite or
    materially-future ``wall_time`` to a maximally-fresh ``0.0`` — permanently, since
    that clamp can never grow past :data:`BRIDGE_MAX_AGE_S`, so the
    :func:`build_pull_rate_view_merged` ``math.isfinite`` dead-evidence guard could
    never act on it and a SIGKILLed CLI's stuck ``pull_active=True`` bridge with a
    ``+inf`` / ``NaN`` / future ``wall_time`` fabricated a permanent live "flowing"
    reading. This is the ONE chokepoint that turns a ``wall_time`` into the age the
    merged projection consumes, so the finiteness + future law lives here (Golden
    Rule #1 / #11 COMMON).

    Rules, in order:

    * A non-finite ``wall_time`` (``+inf`` / ``-inf`` / ``NaN``) or a non-finite
      ``now_wall`` → age ``inf``, DEAD (the ``isfinite`` guard rejects it → the bridge
      is treated as ABSENT, panel-only projection).
    * A ``wall_time`` MORE than ``future_skew_s`` in the future → age ``inf``, DEAD
      (a ms-scaled epoch / corrupt stamp / badly-skewed clock).
    * A ``wall_time`` within ``future_skew_s`` ahead (ordinary cross-host NTP skew) →
      age ``0.0``, LIVE (tolerated — the same value the old ``max(0.0, …)`` produced).
    * A past ``wall_time`` → the real positive age, LIVE (byte-identical to the old
      ``max(0.0, now_wall - wall_time)`` on the fresh path).

    Parameters
    ----------
    now_wall
        The reader's wall clock (``time.time()``).
    wall_time
        The bridge snapshot's embedded publish wall clock.
    future_skew_s
        The tolerated future skew (default :data:`BRIDGE_FUTURE_SKEW_TOLERANCE_S`).

    Returns
    -------
    tuple[float, bool]
        ``(age_seconds, dead_evidence)``. On ``dead_evidence`` the age is ``inf`` so
        the merged guard treats the bridge as absent; the caller additionally labels
        the served ``bridge_source`` ``unverified`` and WARNs once per distinct dead
        ``wall_time`` stamp (:func:`should_warn_dead_bridge`).
    """
    if not math.isfinite(wall_time) or not math.isfinite(now_wall):
        return math.inf, True
    delta = now_wall - wall_time
    if delta < -future_skew_s:
        return math.inf, True
    return (delta if delta > 0.0 else 0.0), False


def should_warn_dead_bridge(current_wall_time: float, last_warned: float | None) -> bool:
    """Return ``True`` iff the dead-bridge WARN should fire for this poll's ``wall_time``.

    DEDUP per distinct dead ``wall_time`` stamp. The live-rate widget polls
    ``GET /api/v1/pull/rate`` site-wide every ~3 s, and a dead bridge (a SIGKILLed
    CLI leaving a stuck ``+inf`` / ``NaN`` / materially-future stamp — see
    :func:`bridge_age_seconds`) is a PERSISTENT condition. An unthrottled
    ``pull_rate.bridge_dead_wall_time`` WARN would therefore fire ~20/min
    INDEFINITELY. This is the ONE decision function that collapses that to ONE WARN
    per distinct dead stamp, matching the repo's deduped-per-key WARN convention
    (Golden Rule #10 honesty; CLAUDE.md §11 COMMON — one source of truth).

    Rules, in order:

    * ``last_warned is None`` (the FIRST dead poll, or the bridge just recovered and
      the caller re-armed the dedup) → ``True`` (warn).
    * a ``NaN`` ``current_wall_time`` whose prior warned stamp was ALSO ``NaN`` →
      ``False`` — a persistent ``NaN`` bridge must warn only once, but ``NaN != NaN``
      would otherwise re-warn every poll, so the ``NaN``-vs-``NaN`` case is handled
      explicitly here.
    * otherwise → warn iff the stamp CHANGED (``current_wall_time != last_warned``),
      so a NEW distinct dead episode (``+inf`` → a different future stamp, or a
      changed ``NaN``-vs-finite pair) re-warns exactly once.

    The caller resets ``last_warned`` to ``None`` whenever the bridge is healthy (or
    absent), so a fresh dead episode after a recovery always re-warns.

    Parameters
    ----------
    current_wall_time
        This poll's dead-evidence ``wall_time`` (non-finite or materially future).
    last_warned
        The last dead ``wall_time`` the caller acted on, or ``None`` when re-armed.

    Returns
    -------
    bool
        ``True`` to emit the WARN this poll; ``False`` to suppress it as a duplicate.
    """
    if last_warned is None:
        return True
    if math.isnan(current_wall_time):
        return not math.isnan(last_warned)
    return current_wall_time != last_warned


def build_pull_rate_view_merged(
    *,
    panel_effective_rps: float,
    panel_current_rps: float,
    panel_recent_minute: int,
    panel_recent_429_count: int,
    panel_seconds_since_update: float,
    panel_recent_1s: int = 0,
    panel_recent_600s: int = 0,
    panel_recent_3600s: int = 0,
    panel_meter_present: bool = True,
    panel_failed_rps: float = 0.0,
    panel_pull_active: bool | None = None,
    panel_phase: str | None = None,
    bridge_effective_rps: float | None = None,
    bridge_current_rps: float | None = None,
    bridge_recent_minute: int | None = None,
    bridge_recent_429_count: int | None = None,
    bridge_seconds_since_update: float | None = None,
    bridge_recent_1s: int | None = None,
    bridge_recent_600s: int | None = None,
    bridge_recent_3600s: int | None = None,
    bridge_meter_present: bool = True,
    bridge_failed_rps: float | None = None,
    bridge_pull_active: bool | None = None,
    stale_after_s: float = STALE_AFTER_S,
    stall_rps_floor: float = STALL_RPS_FLOOR,
    bridge_max_age_s: float = BRIDGE_MAX_AGE_S,
) -> PullRateView:
    """Project the FRESHER of the in-memory dashboard panel and the cross-process bridge.

    DEC-CLI-RATE-BRIDGE-001. The dashboard-pull rate lives in the in-memory
    :class:`~brutex.dashboard.app.RateLimitPanel`; a CLI ``brutex fetch`` pull (a SEPARATE
    process) publishes its rate to ``<lake_root>/.rate/pull_rate.json`` (the bridge), read
    by :func:`~brutex.data.rate_bridge.read_rate_snapshot`. This picks whichever source
    ticked most recently — the SMALLER ``seconds_since_update`` — and projects it through
    :func:`build_pull_rate_view`, so the widget shows the ACTIVE pull's real measured rate
    regardless of which process is pulling. When the bridge is absent (no CLI file) the
    behaviour is BYTE-IDENTICAL to projecting the panel alone (the no-CLI-file fallback). A
    bridge fresher than the panel surfaces the CLI numbers; a panel fresher than (or equal
    to) the bridge keeps the dashboard numbers. When BOTH are stale, the fresher one's age
    is still stale ⇒ :func:`build_pull_rate_view` zeroes everything to an honest IDLE
    (never a fabricated live number — Golden Rule #1).

    The ages are passed pre-computed (the route computes ``now_monotonic -
    last_update_monotonic`` for the panel and ``now_wall - bridge.wall_time`` for the file)
    so this function stays PURE + clock-free + cross-process-clock-agnostic — the only thing
    it does with the two clocks is COMPARE their already-derived staleness in seconds, which
    is the SAME unit on both sides.

    Parameters
    ----------
    panel_effective_rps, panel_current_rps, panel_recent_minute, panel_recent_429_count
        The in-memory panel's measured rate fields.
    panel_seconds_since_update
        The panel's age in seconds (``inf`` when never updated). Always real.
    panel_recent_1s, panel_recent_600s, panel_recent_3600s
        The in-memory panel's MEASURED trailing 1 s / 600 s / 3600 s real issue counts
        (DEC-RATE-MONITOR-EXPAND-001) — req/sec·10min·hour evidence. Default ``0``.
    panel_meter_present
        Whether the in-memory panel's ``effective_rps`` is a REAL measured completed-rate
        (a tick fed by the REGISTERED pool meter's real snapshot) or honestly absent
        (``False`` — no meter registered: the pre-registration window, a dry-run [which
        deregisters] or after the job-finish clear, DEC-RATE-METER-LIFECYCLE-001) —
        DEC-RATE-WIDGET-FNO-HONEST-001. Threaded into
        :func:`build_pull_rate_view` so the ``stalled`` verdict reads the measured rate when
        present and the admission signal otherwise. Default ``True`` (the meter-fed contract).
    panel_failed_rps
        The in-memory panel's MEASURED rolling failed-calls rate (the meter snapshot's
        ``failed_rps`` — DEC-PULL-TELEMETRY-TRIAD-001), threaded into
        :func:`build_pull_rate_view` so an issuing-but-all-failing meter-fed pull reads
        the distinct :data:`FLOW_FAILING` verdict. Default ``0.0`` — byte-identical to
        the pre-decision projection.
    bridge_failed_rps
        The bridge snapshot's failed-calls rate (``None`` when the bridge file is absent
        or predates the field — projected as an honest ``0.0``, never fabricated).
    bridge_effective_rps, bridge_current_rps, bridge_recent_minute, bridge_recent_429_count
        The bridge snapshot's measured rate fields (``None`` when no bridge file is present).
    bridge_seconds_since_update
        The bridge snapshot's age in seconds (``None`` when no bridge file is present).
    bridge_recent_1s, bridge_recent_600s, bridge_recent_3600s
        The bridge snapshot's MEASURED trailing 1 s / 600 s / 3600 s real issue counts
        (``None`` when absent — an older bridge file → projected as ``0``, honestly zeroed).
    bridge_meter_present
        Whether the bridge snapshot's ``effective_rps`` is a REAL measured completed-rate
        (DEC-RATE-WIDGET-FNO-HONEST-001). The CLI ``brutex fetch`` pull publishes a real meter
        rate, so this defaults ``True`` (the bridge contract is unchanged).
    panel_pull_active, bridge_pull_active
        The per-source AUTHORITATIVE pull-run signal threaded into
        :func:`build_pull_rate_view` (DEC-RATE-STALL-PULL-GATE-001) for whichever source
        wins the freshness pick. The dashboard route passes its
        ``BackgroundPullJob.running`` as ``panel_pull_active``, so a just-completed
        dashboard pull reads ``done``/``idle``, never ``stalled``. The CLI bridge file
        NOW carries the flag too (schema v3, DEC-CLI-BRIDGE-PULL-ACTIVE-001): each
        per-window publish stamps ``pull_active=True`` and ``cmd_fetch``'s ``finally``
        publishes one final ``pull_active=False`` — so a just-finished CLI pull ALSO
        reads ``done``/``idle``. ``None`` remains the honest tick-freshness fallback for
        a pre-v3 / absent / flagless bridge file.
    panel_phase
        The dashboard pipeline's CURRENT ``set_phase`` label threaded into the
        PANEL-side projection only (DEC-PIPELINE-PHASE-UI-HONESTY-001) — the CLI
        bridge is a separate ``brutex fetch`` process with NO pipeline phase, so
        its projection NEVER receives a phase (a fresher bridge keeps its
        byte-identical verdict; ``local`` can only ever come from the
        dashboard's own pipeline).
    stale_after_s
        Idle threshold (default :data:`STALE_AFTER_S`).
    stall_rps_floor
        Achieved-r/s stall floor (default :data:`STALL_RPS_FLOOR`).
    bridge_max_age_s
        Maximum bridge age (seconds) still trusted by the freshness pick (default
        :data:`BRIDGE_MAX_AGE_S` — DEC-RATE-METER-LIFECYCLE-001, audit C3). A bridge
        OLDER than this (or with a non-finite age) is DEAD EVIDENCE and treated as
        ABSENT (panel-only projection), so a SIGKILLed CLI's stuck ``pull_active=True``
        file can no longer out-fresh a never-ticked panel FOREVER. ``<=`` keeps the
        bridge trusted AT the bound — a CLI that died mid-run keeps its full red alert
        window (12 s < age ≤ 900 s still reads stalled).

    Returns
    -------
    PullRateView
        The honest projection of whichever source is freshest (idle when none is fresh).
    """
    # No bridge file (or it had no usable age) → project the panel alone, byte-identical to
    # the pre-bridge behaviour (the no-CLI-file in-memory fallback). The ``is not None`` test
    # narrows ``bridge_seconds_since_update`` to ``float`` for the ``build_pull_rate_view``
    # call. An over-TTL or non-finite age is DEAD EVIDENCE ≡ absent (audit C3): the
    # ``math.isfinite`` conjunct makes the NaN/inf behaviour a CONTRACT (panel path),
    # not an accident of float comparison ordering.
    if (
        bridge_seconds_since_update is not None
        and math.isfinite(bridge_seconds_since_update)
        and bridge_seconds_since_update <= bridge_max_age_s
        and bridge_seconds_since_update < panel_seconds_since_update
    ):
        # The bridge fields are written together by the publisher, so a present age implies
        # present values; a defensive ``is not None`` defaults each to 0 regardless.
        return build_pull_rate_view(
            effective_rps=bridge_effective_rps if bridge_effective_rps is not None else 0.0,
            current_rps=bridge_current_rps if bridge_current_rps is not None else 0.0,
            recent_minute=bridge_recent_minute if bridge_recent_minute is not None else 0,
            recent_429_count=(
                bridge_recent_429_count if bridge_recent_429_count is not None else 0
            ),
            recent_1s=bridge_recent_1s if bridge_recent_1s is not None else 0,
            recent_600s=bridge_recent_600s if bridge_recent_600s is not None else 0,
            recent_3600s=bridge_recent_3600s if bridge_recent_3600s is not None else 0,
            meter_present=bridge_meter_present,
            failed_rps=bridge_failed_rps if bridge_failed_rps is not None else 0.0,
            pull_active=bridge_pull_active,
            last_update_monotonic=0.0,
            now_monotonic=bridge_seconds_since_update,
            stale_after_s=stale_after_s,
            stall_rps_floor=stall_rps_floor,
        )
    return build_pull_rate_view(
        effective_rps=panel_effective_rps,
        current_rps=panel_current_rps,
        recent_minute=panel_recent_minute,
        recent_429_count=panel_recent_429_count,
        recent_1s=panel_recent_1s,
        recent_600s=panel_recent_600s,
        recent_3600s=panel_recent_3600s,
        meter_present=panel_meter_present,
        failed_rps=panel_failed_rps,
        pull_active=panel_pull_active,
        phase=panel_phase,
        last_update_monotonic=0.0 if math.isfinite(panel_seconds_since_update) else float("-inf"),
        now_monotonic=panel_seconds_since_update
        if math.isfinite(panel_seconds_since_update)
        else 0.0,
        stale_after_s=stale_after_s,
        stall_rps_floor=stall_rps_floor,
    )


__all__ = [
    "BRIDGE_FUTURE_SKEW_TOLERANCE_S",
    "BRIDGE_MAX_AGE_S",
    "CEILING_PER_MIN_LIMIT",
    "CEILING_RPS_LIMIT",
    "FLOW_DONE",
    "FLOW_FAILING",
    "FLOW_FLOWING",
    "FLOW_IDLE",
    "FLOW_LOCAL",
    "FLOW_STALLED",
    "LOCAL_PHASES",
    "STALE_AFTER_S",
    "STALL_RPS_FLOOR",
    "PullRateView",
    "bridge_age_seconds",
    "build_pull_rate_view",
    "build_pull_rate_view_merged",
    "should_warn_dead_bridge",
]
