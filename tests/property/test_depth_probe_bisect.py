"""Property tests for the bounded depth-probe bisection (kit stage 6, register R6).

Invariants (DEC-DOCTOR-LIVE-PROOF-KIT-001):
* the bisection LOCATES the exact 1-minute floor for any contiguous-from-floor
  vendor history with string-stamped rows, and
* it NEVER exceeds the :data:`DEPTH_PROBE_MAX_CALLS` hard bound (the SAFE
  call-budget guarantee — spec §2.3), for any floor position in the range.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from brutex.adapters.groww.probe import DEPTH_PROBE_MAX_CALLS, VERDICT_OK, probe_depth
from hypothesis import given, settings
from hypothesis import strategies as st

_BOUNDARY = date(2020, 1, 1)
_TODAY = date(2026, 7, 2)
_SPAN_DAYS = (_TODAY - _BOUNDARY).days


class _FloorClient:
    """A vendor serving 1m rows from *floor* onward (contiguous), empty before."""

    def __init__(self, floor: date) -> None:
        self.floor = floor
        self.calls = 0

    def historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        start = date.fromisoformat(str(kwargs["start"])[:10])
        end = date.fromisoformat(str(kwargs["end"])[:10])
        if end < self.floor:
            return {"candles": []}
        first_day = max(start, self.floor)
        return {
            "candles": [[f"{first_day.isoformat()}T09:15:00", 100.0, 101.0, 99.0, 100.5, 0, None]]
        }


@settings(max_examples=120, deadline=None)
@given(floor_offset=st.integers(min_value=0, max_value=_SPAN_DAYS))
def test_bisection_finds_the_exact_floor_within_the_call_bound(floor_offset: int) -> None:
    floor = _BOUNDARY + timedelta(days=floor_offset)
    client = _FloorClient(floor)
    result = probe_depth(
        client, boundary=_BOUNDARY, today=_TODAY, bisect=True, max_calls=DEPTH_PROBE_MAX_CALLS
    )
    assert result.verdict == VERDICT_OK
    assert result.probed_floor == floor
    assert result.calls_used <= DEPTH_PROBE_MAX_CALLS
    assert client.calls == result.calls_used
