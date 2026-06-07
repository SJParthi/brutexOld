"""Generic throttled callback — fire a side-effect at most once per interval.

DEC-OPTIONS-LIVE-STATUS-001. The pure, dependency-free essence of the spot
window-status live-flush throttle (DEC-WINDOW-STATUS-LIVE-001,
:func:`brutex.data.window_status_store.make_live_status_flush`), lifted out so a
non-window-status surface can reuse the SAME throttled-flush principle.

The options pull's only per-underlying progress hook fires once after each
underlying's WHOLE fan-out, so a single-underlying options pull leaves its
lake-backed ``/coverage/options`` surface frozen until that fan-out finishes
(the option lake writer writes per series, but the read surface only refreshes
when something invalidates the summary cache). This wraps a cheap-but-not-free
observability side-effect (a summary-cache ``mark_dirty``) into a callable that
runs **at most once per** *min_interval_s*, so a per-series tick can refresh the
surface mid-pull without firing on every one of thousands of series.

Design (priority law — Correctness > Reliability > … > Performance):

* **Correctness.** The first call always fires (no record yet); subsequent calls
  within the window are dropped; the first call past the window fires again. The
  throttle gates ONLY how often the side-effect runs — it never changes what the
  side-effect does.
* **Not internally locked.** Like :func:`make_live_status_flush`, fire it from a
  SINGLE thread (the ingest controller thread, where the options drain loop
  runs). *clock* + *min_interval_s* are injectable for deterministic tests.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

#: Default minimum seconds between two fires of a throttled callback. Matches the
#: summary cache's 2 s TTL floor (DEC-DASHBOARD-FASTREAD-001) so a per-series tick
#: invalidates the cache at most about as often as the cache will recompute.
DEFAULT_THROTTLE_INTERVAL_S: float = 2.0


def make_throttled(
    callback: Callable[[], None],
    *,
    min_interval_s: float = DEFAULT_THROTTLE_INTERVAL_S,
    clock: Callable[[], float] = monotonic,
) -> Callable[[], None]:
    """Return a zero-arg callable that runs *callback* at most once per *min_interval_s*.

    The first invocation always runs *callback*; any invocation within
    *min_interval_s* of the last RUN is a no-op; the first invocation after the
    window runs it again and resets the window. O(1) per call.

    Parameters
    ----------
    callback
        The zero-arg side-effect to throttle (e.g. a summary-cache
        ``mark_dirty``). Run on the calling thread, synchronously.
    min_interval_s
        Minimum seconds between two runs of *callback* (default
        :data:`DEFAULT_THROTTLE_INTERVAL_S`). A non-positive value runs on every
        call (no throttle).
    clock
        Monotonic time source (defaults to :func:`time.monotonic`), injected for
        deterministic tests.

    Returns
    -------
    Callable[[], None]
        The throttled wrapper. Not internally locked — fire from one thread.
    """
    last_run: list[float | None] = [None]

    def _throttled() -> None:
        now = clock()
        if last_run[0] is not None and now - last_run[0] < min_interval_s:
            return
        last_run[0] = now
        callback()

    return _throttled


__all__ = [
    "DEFAULT_THROTTLE_INTERVAL_S",
    "make_throttled",
]
