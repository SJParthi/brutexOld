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

## The three flow states (honest, never fabricated)

* ``idle`` — no live tick within ``stale_after_s`` (no pull running / the pull ended): the
  view ZEROES every live number so the widget shows a dark "idle" light, never the last
  live figure (Golden Rule #1 — never a fake "live" number when idle).
* ``stalled`` — a tick landed recently (a pull IS active) but the achieved r/s is at/below
  :data:`STALL_RPS_FLOOR`: the pull is effectively dead → a RED light.
* ``flowing`` — a recent tick with achieved r/s above the floor → a GREEN light.

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


@dataclass(frozen=True, slots=True)
class PullRateView:
    """Immutable live pull-rate projection for the ``/api/v1/pull/rate`` poll.

    Attributes
    ----------
    flow_state
        One of :data:`FLOW_IDLE` / :data:`FLOW_FLOWING` / :data:`FLOW_STALLED` — the
        widget's stall-light colour (dark / green / red).
    active
        ``True`` iff a live rate tick landed within ``stale_after_s`` (a pull is running).
        When ``False`` every live number below is zeroed (idle).
    stalled
        ``True`` iff a pull is ACTIVE but its achieved r/s is at/below
        :data:`STALL_RPS_FLOOR` — the pull is effectively dead.
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
        when idle.
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
    """

    flow_state: str
    active: bool
    stalled: bool
    req_per_sec_achieved: float
    req_per_sec_allowed: float
    req_per_min: int
    req_per_hour_observed: int
    recent_429_count: int
    seconds_since_update: float
    ceiling_rps: float
    ceiling_per_min: int

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
    stale_after_s: float = STALE_AFTER_S,
    stall_rps_floor: float = STALL_RPS_FLOOR,
) -> PullRateView:
    """Derive the :class:`PullRateView` from a rate panel's live fields + its update age.

    Pure + fully injectable (no clock / global state) so a test drives it with explicit
    values and asserts the idle / flowing / stalled projection deterministically.

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

    active = seconds_since_update <= stale_after_s

    if not active:
        # Idle: a pull ended or none ran — ZERO every live number so the widget never shows
        # a stale "live" figure (Golden Rule #1). The limits + staleness stay real.
        return PullRateView(
            flow_state=FLOW_IDLE,
            active=False,
            stalled=False,
            req_per_sec_achieved=0.0,
            req_per_sec_allowed=0.0,
            req_per_min=0,
            req_per_hour_observed=0,
            recent_429_count=0,
            seconds_since_update=seconds_since_update,
            ceiling_rps=CEILING_RPS_LIMIT,
            ceiling_per_min=CEILING_PER_MIN_LIMIT,
        )

    stalled = effective_rps <= stall_rps_floor
    return PullRateView(
        flow_state=FLOW_STALLED if stalled else FLOW_FLOWING,
        active=True,
        stalled=stalled,
        req_per_sec_achieved=effective_rps,
        req_per_sec_allowed=current_rps,
        req_per_min=recent_minute,
        # OBSERVED extrapolation: the current rolling-minute rate × 60 (Golden Rule #10 —
        # NOT a measured trailing-hour count; the field name + widget label say "observed").
        req_per_hour_observed=recent_minute * 60,
        recent_429_count=recent_429_count,
        seconds_since_update=seconds_since_update,
        ceiling_rps=CEILING_RPS_LIMIT,
        ceiling_per_min=CEILING_PER_MIN_LIMIT,
    )


def build_pull_rate_view_merged(
    *,
    panel_effective_rps: float,
    panel_current_rps: float,
    panel_recent_minute: int,
    panel_recent_429_count: int,
    panel_seconds_since_update: float,
    bridge_effective_rps: float | None = None,
    bridge_current_rps: float | None = None,
    bridge_recent_minute: int | None = None,
    bridge_recent_429_count: int | None = None,
    bridge_seconds_since_update: float | None = None,
    stale_after_s: float = STALE_AFTER_S,
    stall_rps_floor: float = STALL_RPS_FLOOR,
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
    bridge_effective_rps, bridge_current_rps, bridge_recent_minute, bridge_recent_429_count
        The bridge snapshot's measured rate fields (``None`` when no bridge file is present).
    bridge_seconds_since_update
        The bridge snapshot's age in seconds (``None`` when no bridge file is present).
    stale_after_s
        Idle threshold (default :data:`STALE_AFTER_S`).
    stall_rps_floor
        Achieved-r/s stall floor (default :data:`STALL_RPS_FLOOR`).

    Returns
    -------
    PullRateView
        The honest projection of whichever source is freshest (idle when none is fresh).
    """
    # No bridge file (or it had no usable age) → project the panel alone, byte-identical to
    # the pre-bridge behaviour (the no-CLI-file in-memory fallback). The ``is not None`` test
    # narrows ``bridge_seconds_since_update`` to ``float`` for the ``build_pull_rate_view`` call.
    if (
        bridge_seconds_since_update is not None
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
        last_update_monotonic=0.0 if math.isfinite(panel_seconds_since_update) else float("-inf"),
        now_monotonic=panel_seconds_since_update
        if math.isfinite(panel_seconds_since_update)
        else 0.0,
        stale_after_s=stale_after_s,
        stall_rps_floor=stall_rps_floor,
    )


__all__ = [
    "CEILING_PER_MIN_LIMIT",
    "CEILING_RPS_LIMIT",
    "FLOW_FLOWING",
    "FLOW_IDLE",
    "FLOW_STALLED",
    "STALE_AFTER_S",
    "STALL_RPS_FLOOR",
    "PullRateView",
    "build_pull_rate_view",
    "build_pull_rate_view_merged",
]
