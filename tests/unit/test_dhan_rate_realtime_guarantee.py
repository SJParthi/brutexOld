"""Rate ≤5 real-time guarantee + assurance (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).

Two locks, both runnable WITHOUT the Rust wheels (``client.py`` is wheel-free):

* **Guarantee** — every physical POST goes through the shared rate bucket. The
  ``DhanClient`` fast-retry (a timeout/transport/5xx stall) now calls the injected
  ``pace`` before each retry attempt, so a parallel timeout storm can no longer
  fire un-gated retries past the 5 r/s cap (the gap the operator's ``dhan.retry``
  storm exposed). The INITIAL attempt is paced by the ingest layer, so the client
  paces retries ONLY (no double-pace).
* **Assurance** — :class:`CallMeter` tracks the trailing-1s POST-issue count
  (``rps_1s`` / ``rps_1s_max``), the DIRECT real-time measurement against the cap;
  ``rps_1s_max <= 5`` is the proof per-second was never exceeded.
"""

from __future__ import annotations

from collections.abc import Mapping

from brutex.adapters.dhan.client import CallMeter, DhanClient
from brutex.adapters.dhan.constants import DATA_RATE_LIMIT_RPS


class _Clock:
    """Deterministic monotonic clock — advances a fixed tick per read."""

    def __init__(self, tick: float = 0.001) -> None:
        self._now = 0.0
        self._tick = tick

    def __call__(self) -> float:
        self._now += self._tick
        return self._now


def _ok_transport(
    _url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    """A transport that always succeeds with an empty JSON object."""
    return 200, b"{}"


def _flaky_transport(n_timeouts: int):
    """A transport that raises ``TimeoutError`` *n_timeouts* times, then succeeds."""
    state = {"left": n_timeouts}

    def _transport(
        _url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        if state["left"] > 0:
            state["left"] -= 1
            raise TimeoutError("simulated Dhan stall")
        return 200, b"{}"

    return _transport


def _client(
    transport, *, pace_calls: list[int], sleep_calls: list[float], max_attempts: int = 3
) -> DhanClient:
    return DhanClient(
        access_token="tok",
        client_id="cid",
        transport=transport,
        max_attempts=max_attempts,
        retry_backoff_s=0.2,
        clock=_Clock(),
        sleep=sleep_calls.append,
        pace=lambda: pace_calls.append(1),
    )


def test_fast_retry_paces_every_retry_post() -> None:
    """GUARANTEE: two stalls → two retries → ``pace`` called exactly twice (retries only)."""
    pace_calls: list[int] = []
    sleep_calls: list[float] = []
    client = _client(_flaky_transport(2), pace_calls=pace_calls, sleep_calls=sleep_calls)

    result = client.post_json("charts/intraday", {})

    assert result == {}
    # 3 physical attempts (1 initial + 2 retries); the initial is outer-paced by
    # the ingest layer, so the client paces ONLY the 2 retries — never the first.
    assert sum(pace_calls) == 2
    assert len(sleep_calls) == 2  # one backoff per retry


def test_no_retry_means_no_client_pace() -> None:
    """A first-try success never calls the client ``pace`` (no double-pace with the ingest layer)."""
    pace_calls: list[int] = []
    sleep_calls: list[float] = []
    client = _client(_ok_transport, pace_calls=pace_calls, sleep_calls=sleep_calls)

    assert client.post_json("charts/intraday", {}) == {}
    assert pace_calls == []
    assert sleep_calls == []


def test_meter_rps_1s_counts_trailing_second() -> None:
    """ASSURANCE: ``rps_1s`` is the trailing-1s issue count; 5/s paced never exceeds 5."""
    meter = CallMeter()
    # Five issues at 0.2s spacing = exactly 5 r/s — trailing-1s count tops out at 5.
    for t in (0.0, 0.2, 0.4, 0.6, 0.8):
        meter.record_issue(t)
    assert meter.snapshot()["rps_1s_max"] == DATA_RATE_LIMIT_RPS  # == 5, never 6


def test_meter_rps_1s_evicts_stale_then_flags_a_burst() -> None:
    """A later isolated second resets the trailing count; a 6-in-1s burst trips the peak > 5."""
    meter = CallMeter()
    for t in (0.0, 0.2, 0.4):
        meter.record_issue(t)
    # Jump >1s ahead: the old issues age out, so the trailing count drops to 1.
    assert meter.record_issue(2.0) == 1
    # Six POSTs inside one second → trailing count reaches 6 (a genuine >5 breach).
    for t in (3.0, 3.1, 3.2, 3.3, 3.4):
        meter.record_issue(t)
    assert meter.record_issue(3.5) == 6
    assert meter.snapshot()["rps_1s_max"] == 6  # > DATA_RATE_LIMIT_RPS → breach visible
