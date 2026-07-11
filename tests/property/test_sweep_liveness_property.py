"""Property gates for :mod:`brutex.engine.sweep_liveness` (G4 — DEC-SWEEP-UNKILLABLE-CORE-001).

The G4 law as hypothesis invariants:

* **Staleness monotonicity** — for a fixed valid payload + a fixed pid
  probe, the verdict as a function of ``now_wall`` only ever moves
  RUNNING → STALE as time advances; once STALE it can never flip back.
* **Dead-pid dominance** — a dead writer pid is NOT_RUNNING at EVERY
  staleness.
* **Fail-closed absorption** — dropping ANY of the three modern keys
  (the legacy shape) or corrupting a field type yields UNKNOWN at every
  ``now_wall``, and UNKNOWN is never the quarantinable NOT_RUNNING —
  the heal-refusal law the verdict enum encodes.
"""

from __future__ import annotations

import math
from typing import Any, Final

from brutex.engine.sweep_liveness import RunVerdict, progress_verdict
from hypothesis import given, settings
from hypothesis import strategies as st

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g4_verdict_monotone_in_staleness": ("G4",),
    "test_g4_stale_is_absorbing": ("G4",),
    "test_g4_dead_pid_dominates_every_staleness": ("G4",),
    "test_g4_legacy_key_drop_is_unknown_everywhere": ("G4",),
    "test_g4_corrupt_type_is_unknown_and_never_not_running": ("G4",),
}

#: Verdict rank on the live axis (RUNNING before STALE); only these two are
#: reachable for a valid running=True payload with an alive pid.
_LIVE_RANK: Final[dict[RunVerdict, int]] = {RunVerdict.RUNNING: 0, RunVerdict.STALE: 1}

_ALIVE = lambda pid: True  # noqa: E731
_DEAD = lambda pid: False  # noqa: E731

_WALLS = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
_OFFSETS = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
_FRESH = st.floats(min_value=0.001, max_value=1e5, allow_nan=False, allow_infinity=False)
_PIDS = st.integers(min_value=1, max_value=2**22)


def _payload(wall: float, pid: int) -> dict[str, Any]:
    return {"running": True, "wall_time_utc": wall, "pid": pid}


@settings(deadline=None)
@given(wall=_WALLS, fresh=_FRESH, pid=_PIDS, dt1=_OFFSETS, dt2=_OFFSETS)
def test_g4_verdict_monotone_in_staleness(
    wall: float, fresh: float, pid: int, dt1: float, dt2: float
) -> None:
    """For now2 >= now1 the verdict rank never DECREASES (RUNNING→STALE only)."""
    now1 = wall + min(dt1, dt2)
    now2 = wall + max(dt1, dt2)
    v1 = progress_verdict(_payload(wall, pid), now_wall=now1, fresh_seconds=fresh, pid_alive=_ALIVE)
    v2 = progress_verdict(_payload(wall, pid), now_wall=now2, fresh_seconds=fresh, pid_alive=_ALIVE)
    assert _LIVE_RANK[v2] >= _LIVE_RANK[v1]


@settings(deadline=None)
@given(wall=_WALLS, fresh=_FRESH, pid=_PIDS, extra=_OFFSETS)
def test_g4_stale_is_absorbing(wall: float, fresh: float, pid: int, extra: float) -> None:
    """Once strictly past the freshness window, every later probe is STALE too."""
    stale_now = wall + fresh * 1.5 + 1.0
    v_at = progress_verdict(
        _payload(wall, pid), now_wall=stale_now, fresh_seconds=fresh, pid_alive=_ALIVE
    )
    v_later = progress_verdict(
        _payload(wall, pid), now_wall=stale_now + extra, fresh_seconds=fresh, pid_alive=_ALIVE
    )
    assert v_at is RunVerdict.STALE
    assert v_later is RunVerdict.STALE


@settings(deadline=None)
@given(wall=_WALLS, fresh=_FRESH, pid=_PIDS, dt=_OFFSETS)
def test_g4_dead_pid_dominates_every_staleness(
    wall: float, fresh: float, pid: int, dt: float
) -> None:
    """A dead writer pid is NOT_RUNNING regardless of how fresh/stale the wall is."""
    verdict = progress_verdict(
        _payload(wall, pid), now_wall=wall + dt, fresh_seconds=fresh, pid_alive=_DEAD
    )
    assert verdict is RunVerdict.NOT_RUNNING


@settings(deadline=None)
@given(
    wall=_WALLS,
    pid=_PIDS,
    now=_WALLS,
    drop=st.sampled_from(["running", "wall_time_utc", "pid"]),
)
def test_g4_legacy_key_drop_is_unknown_everywhere(
    wall: float, pid: int, now: float, drop: str
) -> None:
    """Dropping ANY modern key (the legacy shape) ⇒ UNKNOWN at every now_wall."""
    payload = _payload(wall, pid)
    del payload[drop]
    verdict = progress_verdict(payload, now_wall=now, pid_alive=_ALIVE)
    assert verdict is RunVerdict.UNKNOWN


@settings(deadline=None)
@given(
    wall=_WALLS,
    pid=_PIDS,
    now=_WALLS,
    corruption=st.sampled_from(
        [
            ("running", "yes"),
            ("running", 1),
            ("wall_time_utc", "soon"),
            ("wall_time_utc", math.nan),
            ("wall_time_utc", True),
            ("pid", "42"),
            ("pid", 0),
            ("pid", -1),
            ("pid", True),
        ]
    ),
)
def test_g4_corrupt_type_is_unknown_and_never_not_running(
    wall: float, pid: int, now: float, corruption: tuple[str, Any]
) -> None:
    """Every corrupt field type ⇒ UNKNOWN — never the quarantinable NOT_RUNNING."""
    key, bad = corruption
    payload = _payload(wall, pid)
    payload[key] = bad
    verdict = progress_verdict(payload, now_wall=now, pid_alive=_ALIVE)
    assert verdict is not RunVerdict.NOT_RUNNING  # never the quarantinable verdict
    assert verdict is RunVerdict.UNKNOWN
