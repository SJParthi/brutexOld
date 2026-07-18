"""Signal→next-1-minute execution-grid mapping (DEC-FILL-EXEC-GRID-1M-001).

The PR-B 1-minute execution grid's ONE mapping leaf (design
``docs/design/fills/PRB_1M_EXECUTION_GRID_DESIGN.md`` §1.2 D2): decision-TF
bars generate the SIGNALS (Stage-1A, untouched); every FILL resolves on the
1-minute execution stream. This module owns the signal→fill-bar relation —
precomputed ONCE per ``(symbol, tf)`` coordinate as plain integer arrays (the
derive-in-Python / consume-in-Rust discipline: the kernel receives columns,
never timestamps), DECIDED over a per-event binary search (§1.2 rationale).

## The mapping law (§1.2 — pure UTC arithmetic)

Decision bars are labelled at bucket START; decision bar ``N``'s close
INSTANT is ``ts_tf[N] + tf_minutes``. The fill bar for a signal on ``N`` is

    ``exec_idx(N) = min{ j : ts_1m[j] >= ts_tf[N] + tf_minutes }``

— the first stored 1-minute bar at-or-after the close instant. On a
contiguous aligned feed that is the bar labelled exactly at the close
instant; on a hole it is the next STORED same-session minute — the RATIFIED
session-bounded SLIDE law (design §5). The mapping itself does NO IST
conversion (§3.2): both streams carry UTC-aware timestamps and IST has a
fixed offset, so UTC interval arithmetic ≡ IST interval arithmetic.

## The gates baked into ``arm_source`` (build-time, mask-independent)

IST enters ONLY where it lawfully lives — session identity — through the
canonical calendar seams (§3.2):

* **Same-session gate (R2-OC-2 on the 1m grid):** a fill candidate on a
  LATER (or otherwise different) IST session date than its arming decision
  bar is DROPPED — the slide law is session-bounded, cross-session attempts
  DROP (§2.9). Baked here because the kernel FFI carries no decision-grid
  session column (design §1.3).
* **Bounded-distance selector assertion (B6, §3.2):** a same-date candidate
  opening AT/AFTER the arming session's close instant is REFUSED with the
  named ``fill_selector_distance_refused`` event (counted + capped WARN) —
  never a fill. A +05:30 tz-shifted candidate grid violates this arm (or the
  same-session arm) structurally; defense-in-depth even if a mislabelled
  stream survived ingest.
* **Collision WINNER LAW (B4, §1.2):** a ≥tf-minute hole crossing a decision
  close boundary can map two decision bars onto ONE 1m bar. **Earliest-armed
  wins** — ``arm_source[j] = min{N : exec_idx(N) == j}``; every LOSING
  decision index is recorded in ``collision_losers`` (its signal drops,
  counted ``signals_dropped_collision`` by the consumers; capped WARN here).

## Cycle freedom + observability

The leaf imports ONLY the calendar seams (:mod:`brutex.calendars.nse`) —
nothing from :mod:`brutex.engine` — so every engine module can import it
freely (the ``exit_index.py`` cycle-freedom pattern). The design's "stdlib
leaf" intent is honoured as ENGINE-cycle freedom: the B6 selector assertion
needs the arming session's close instant, resolved through the SAME canonical
seam the walker uses (one session notion, CLAUDE.md §11 COMMON), never a
second calendar copy. All build-time WARNs are mask-independent and capped
(the ``_TAIL_HOLE_WARN_CAP`` idiom — DEC-SWEEP-LOG-VOLUME-GUARD-001 posture).

## Honest envelope (Golden Rule #10)

``build_exec_grid_map`` is ``O(N_1m + N_tf)`` (one two-pointer merge over two
chronological streams) plus O(sessions) calendar resolutions — paid ONCE per
``(symbol, tf)`` coordinate, never per mask and never per tuple. Lookups on
the built map are O(1) array reads. NOT folded into run identity (§4.4): the
map is a pure function of the two hashed bar streams + ``tf_minutes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars.nse import IST, SESSION_CLOSE_IST, get_session_checked

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import date

    from brutex.data.models import CandleBar

__all__ = [
    "EXECUTION_GRID_1M_NEXT_BAR",
    "EXEC_INTERVAL_LABEL",
    "ExecGridMap",
    "build_exec_grid_map",
    "exec_window_slice",
]

_LOG = structlog.get_logger(__name__)

#: The canonical run-identity token for the PR-B 1-minute execution grid
#: (design §4.2, DEC-FILL-EXEC-GRID-1M-001). Emitted UNCONDITIONALLY as
#: ``params["execution_grid"]`` by ``canonical_trade_params`` and mirrored at
#: the exact PR-A ``fill_mode`` mirror set (grid descriptor, tuple cache
#: params, tuple store digest, cascade manifest spec, sweep-verify's recorded
#: generation gate). ONE source of the literal (CLAUDE.md §11 COMMON): every
#: emitter and every refusal law imports this name — a drifted spelling can
#: never silently alias a decision-TF-era identity.
EXECUTION_GRID_1M_NEXT_BAR: Final[str] = "1m_next_bar"

#: The lake interval label of the EXECUTION stream (design §1.2 D1): the
#: preload loads every swept symbol's ``interval_list ∪ {"1minute"}`` into the
#: SAME slices mapping, and every spine consumer fetches the exec stream via
#: ``bars_for(symbol, EXEC_INTERVAL_LABEL)`` — at tf=1minute the key is
#: identical, so no double load and no double snapshot fold.
EXEC_INTERVAL_LABEL: Final[str] = "1minute"

#: Cap on per-class build WARNs (`exec_grid.*`) — everything past it rolls
#: into ONE ``*_suppressed`` summary per class (the house
#: ``_TAIL_HOLE_WARN_CAP`` capped-warn precedent; mask-independent by
#: construction — the build runs once per coordinate, never per mask).
_BUILD_WARN_CAP: Final[int] = 20


@dataclass(frozen=True, slots=True)
class ExecGridMap:
    """The precomputed signal→next-1m mapping for ONE ``(symbol, tf)`` coordinate.

    Design §1.2 D2 (DEC-FILL-EXEC-GRID-1M-001). The four prescribed columns
    plus the additive build-observability fields the B4/B6/slide laws
    require (each mask-independent, derived once at build).

    Parameters
    ----------
    arm_source
        Per 1m bar ``j``: the decision index ``N`` whose next-1m fill instant
        lands at ``j`` (the collision WINNER — earliest-armed), else ``-1``.
        Strictly increasing over its non-negative entries by construction.
    decision_of
        Per 1m bar ``j``: index of the LAST decision bar whose label is
        ``<= ts_1m[j]`` (the owning bucket; ``-1`` before the first decision
        bar) — the metrics tail's ``exit_idx_1m → decision bar`` O(1)
        translation (design §3.8).
    n_decision
        Decision-stream length the map was built against.
    n_exec
        Execution (1m) stream length the map was built against.
    arm_slid
        Per 1m bar ``j``: ``True`` iff ``arm_source[j] >= 0`` AND the fill
        bar opens STRICTLY after the arming bar's exact close instant — a
        SLID fill under the ratified slide law (§5; feeds the widened
        ``fill_slid_minutes`` counter family).
    collision_losers
        Decision indices that LOST the B4 earliest-armed-wins collision law
        (their signals drop, counted ``signals_dropped_collision``).
    distance_refused
        Decision indices refused by the B6 bounded-distance selector
        assertion (``fill_selector_distance_refused`` — the fill candidate
        opened at/after the arming session's close instant).
    cross_session_dropped
        Decision indices whose fill candidate landed on a DIFFERENT IST
        session date (the session-bounded slide law's cross-session DROP).

    Raises
    ------
    ValueError
        If the columns are inconsistent (wrong lengths, out-of-range
        indices, or a non-increasing ``arm_source``) — a malformed
        hand-built instance; :func:`build_exec_grid_map` always produces
        consistent maps.
    """

    arm_source: tuple[int, ...]
    decision_of: tuple[int, ...]
    n_decision: int
    n_exec: int
    arm_slid: tuple[bool, ...]
    collision_losers: tuple[int, ...] = ()
    distance_refused: tuple[int, ...] = ()
    cross_session_dropped: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.n_decision < 0 or self.n_exec < 0:
            raise ValueError(
                f"ExecGridMap: negative stream length (n_decision={self.n_decision}, "
                f"n_exec={self.n_exec})"
            )
        for name, column in (
            ("arm_source", self.arm_source),
            ("decision_of", self.decision_of),
            ("arm_slid", self.arm_slid),
        ):
            if len(column) != self.n_exec:
                raise ValueError(
                    f"ExecGridMap: {name} length {len(column)} != n_exec {self.n_exec}"
                )
        prev_arm = -1
        for j, src in enumerate(self.arm_source):
            if not -1 <= src < self.n_decision:
                raise ValueError(
                    f"ExecGridMap: arm_source[{j}] = {src} outside [-1, {self.n_decision})"
                )
            if src >= 0:
                if src <= prev_arm:
                    # The winner law + the monotone mapping make winners
                    # strictly increasing; anything else is a corrupt map.
                    raise ValueError(
                        f"ExecGridMap: arm_source not strictly increasing over its "
                        f"non-negative entries at j={j} ({src} <= {prev_arm})"
                    )
                prev_arm = src
            if self.arm_slid[j] and src < 0:
                raise ValueError(f"ExecGridMap: arm_slid[{j}] set on an armless bar")
        for j, owner in enumerate(self.decision_of):
            if not -1 <= owner < self.n_decision:
                raise ValueError(
                    f"ExecGridMap: decision_of[{j}] = {owner} outside [-1, {self.n_decision})"
                )
        for name, indices in (
            ("collision_losers", self.collision_losers),
            ("distance_refused", self.distance_refused),
            ("cross_session_dropped", self.cross_session_dropped),
        ):
            for value in indices:
                if not 0 <= value < self.n_decision:
                    raise ValueError(
                        f"ExecGridMap: {name} entry {value} outside [0, {self.n_decision})"
                    )


def _require_chronological(name: str, bars: Sequence[CandleBar]) -> None:
    """Refuse a REORDERED stream LOUDLY (the two-pointer precondition).

    The mapping law is defined over chronological streams; a DECREASING
    timestamp would silently mis-map fills, so the build refuses it with a
    named ``ValueError`` (correct-or-absent — the DEC-SWEEP-PREFLIGHT-001
    loud-refusal posture applied at the engine seam). A DUPLICATE timestamp
    is tolerated (the ``>=`` mapping deterministically selects the FIRST
    duplicate) so a malformed-but-ordered window still simulates — the
    exit-index fast path separately withholds indices on such a window via
    :func:`~brutex.engine.exit_index.bars_strictly_increasing` (the
    historical uncertified-window contract, byte-preserved).
    """
    for i in range(1, len(bars)):
        if bars[i - 1].timestamp > bars[i].timestamp:
            raise ValueError(
                f"build_exec_grid_map: {name} timestamps decrease at "
                f"index {i} ({bars[i - 1].timestamp!r} > {bars[i].timestamp!r})"
            )


def _session_close_instant(day_ist: date, cache: dict[date, datetime]) -> datetime:
    """Resolve *day_ist*'s session-close INSTANT (aware) — the B6 distance bound.

    The bound IS the slide law's session boundary (design §3.2): a fill may
    never open at/after its arming session's close. Resolved through the
    canonical :func:`~brutex.calendars.nse.get_session_checked` seam — ONE
    session notion with the walker (CLAUDE.md §11 COMMON). An in-coverage
    date with NO session falls back to the regular 15:30 close
    (deterministic, mirroring the walker's non-session fallback stance); an
    out-of-coverage WEEKDAY raises ``HolidayDataUnavailableError`` LOUDLY —
    exactly the walker/seam construction contract.
    """
    cached = cache.get(day_ist)
    if cached is not None:
        return cached
    session = get_session_checked(day_ist)
    close_t = SESSION_CLOSE_IST if session is None else session.close_time_ist
    instant = datetime(
        day_ist.year, day_ist.month, day_ist.day, close_t.hour, close_t.minute, tzinfo=IST
    )
    cache[day_ist] = instant
    return instant


def _warn_capped(event: str, decision_indices: Sequence[int]) -> None:
    """Emit at most :data:`_BUILD_WARN_CAP` per-index WARNs + one summary."""
    for n in decision_indices[:_BUILD_WARN_CAP]:
        _LOG.warning(event, decision_idx=n)
    if len(decision_indices) > _BUILD_WARN_CAP:
        _LOG.warning(
            f"{event}_suppressed",
            total=len(decision_indices),
            shown=_BUILD_WARN_CAP,
        )


def build_exec_grid_map(
    decision_bars: Sequence[CandleBar],
    exec_bars_1m: Sequence[CandleBar],
    *,
    tf_minutes: int,
) -> ExecGridMap:
    """Build the :class:`ExecGridMap` for one decision/execution stream pair.

    One ``O(N_1m + N_tf)`` two-pointer merge over the two chronological
    streams (+ O(sessions) calendar resolutions), applying — in order, per
    decision bar — the ``>=``-close-instant mapping law, the cross-session
    DROP, the B6 bounded-distance refusal and the B4 earliest-armed-wins
    collision law (module docstring). Mask-independent: built once per
    ``(symbol, tf)`` coordinate.

    Parameters
    ----------
    decision_bars
        Chronologically ordered decision-TF bars (bucket-START labels).
    exec_bars_1m
        Chronologically ordered 1-minute execution bars for the same
        instrument. At ``tf_minutes == 1`` callers pass the SAME sequence
        for both (the §6 degenerate fixed point).
    tf_minutes
        The decision timeframe in whole minutes (``>= 1``) — sets each
        decision bar's close instant ``label + tf_minutes``.

    Returns
    -------
    ExecGridMap
        The precomputed mapping + build observability columns.

    Raises
    ------
    ValueError
        If ``tf_minutes < 1`` or either stream carries a DECREASING
        timestamp (duplicates are tolerated — see
        :func:`_require_chronological`).
    brutex.calendars.nse.HolidayDataUnavailableError
        If a decision bar is dated on a weekday OUTSIDE the published holiday
        span (the session-close bound cannot be verified — refuse LOUDLY,
        never fabricate a boundary; mirrors the walker construction
        contract).
    """
    if tf_minutes < 1:
        raise ValueError(f"build_exec_grid_map: tf_minutes must be >= 1, got {tf_minutes}")
    _require_chronological("decision_bars", decision_bars)
    _require_chronological("exec_bars_1m", exec_bars_1m)

    n_decision = len(decision_bars)
    n_exec = len(exec_bars_1m)
    arm_source = [-1] * n_exec
    arm_slid = [False] * n_exec
    decision_of = [-1] * n_exec
    collision_losers: list[int] = []
    distance_refused: list[int] = []
    cross_session_dropped: list[int] = []

    # decision_of: per 1m bar j, the LAST decision index whose label <= ts_1m[j]
    # (-1 before the first decision bar) — one forward two-pointer pass.
    k = 0
    for j, ebar in enumerate(exec_bars_1m):
        while k < n_decision and decision_bars[k].timestamp <= ebar.timestamp:
            k += 1
        decision_of[j] = k - 1

    # arm_source: the >=-close-instant mapping + the three build-time gates.
    step = timedelta(minutes=tf_minutes)
    close_cache: dict[date, datetime] = {}
    j = 0
    for n, dbar in enumerate(decision_bars):
        close_instant = dbar.timestamp + step
        while j < n_exec and exec_bars_1m[j].timestamp < close_instant:
            j += 1
        if j >= n_exec:
            # No stored candidate for N (or any later decision bar): the
            # session's stored tail ended — entries whose fill instant lands
            # past it simply never fill (design §5.2 cause 4).
            break
        fill_bar = exec_bars_1m[j]
        arming_day = dbar.timestamp.astimezone(IST).date()
        fill_day = fill_bar.timestamp.astimezone(IST).date()
        if fill_day != arming_day:
            # Session-bounded slide: cross-session attempts DROP (§2.9).
            cross_session_dropped.append(n)
            continue
        if fill_bar.timestamp >= _session_close_instant(arming_day, close_cache):
            # B6 selector assertion: same IST date but at/after the arming
            # session's close instant — a tz-shifted/wrong-time-of-day
            # candidate. Named refusal, never a fill.
            distance_refused.append(n)
            continue
        if arm_source[j] >= 0:
            # B4 collision: an earlier decision bar already claimed j —
            # earliest-armed wins, this one drops its signal.
            collision_losers.append(n)
            continue
        arm_source[j] = n
        arm_slid[j] = fill_bar.timestamp != close_instant

    _warn_capped("exec_grid.fill_selector_distance_refused", distance_refused)
    _warn_capped("exec_grid.signals_dropped_collision", collision_losers)
    if arm_slid.count(True):
        slid_bars = [j for j, s in enumerate(arm_slid) if s]
        for j in slid_bars[:_BUILD_WARN_CAP]:
            _LOG.warning("exec_grid.fill_slid_minute", exec_idx=j, decision_idx=arm_source[j])
        if len(slid_bars) > _BUILD_WARN_CAP:
            _LOG.warning(
                "exec_grid.fill_slid_minute_suppressed",
                total=len(slid_bars),
                shown=_BUILD_WARN_CAP,
            )

    return ExecGridMap(
        arm_source=tuple(arm_source),
        decision_of=tuple(decision_of),
        n_decision=n_decision,
        n_exec=n_exec,
        arm_slid=tuple(arm_slid),
        collision_losers=tuple(collision_losers),
        distance_refused=tuple(distance_refused),
        cross_session_dropped=tuple(cross_session_dropped),
    )


def exec_window_slice(
    decision_window: Sequence[CandleBar],
    exec_bars_1m: Sequence[CandleBar],
) -> tuple[int, int]:
    """Return ``[lo, hi)`` bounds of the exec-stream sub-window serving *decision_window*.

    The dual-stream WINDOW-CUT law (design §7.2/Q3 — ONE boundary applied to
    BOTH streams): a sliced decision window (a walk-forward fold, an
    out-of-sample split, a Stage-3 train slice) is served by exactly the
    execution bars from the window's FIRST decision label through the END of
    the LAST decision bar's IST session. Nothing earlier can ever fill (the
    earliest fill instant is ``first_label + tf``); nothing later can either
    (the slide law is session-bounded — a fill never crosses out of the last
    decision bar's session, §2.9), so the cut is loss-free by construction.

    Pure UTC/IST-boundary arithmetic — two ``bisect`` probes over the exec
    timestamps, ``O(N_1m)`` to materialise the key list (paid per call; the
    callers are per-fold/per-split seams, never per mask). An empty decision
    window returns ``(0, 0)``.
    """
    if not decision_window:
        return (0, 0)
    from bisect import bisect_left

    ts_keys = [bar.timestamp for bar in exec_bars_1m]
    lo = bisect_left(ts_keys, decision_window[0].timestamp)
    last_day_ist = decision_window[-1].timestamp.astimezone(IST).date()
    next_midnight_ist = datetime(
        last_day_ist.year, last_day_ist.month, last_day_ist.day, tzinfo=IST
    ) + timedelta(days=1)
    hi = bisect_left(ts_keys, next_midnight_ist, lo)
    return (lo, hi)
