"""O(1) bench — the per-alert publish cost is flat in the alert COUNT.

:meth:`brutex.notifications.sweep_alerts.SweepAlerter.watchdog_hung` (and every
sibling) does one bounded unit of work per call — resolve the notifier (a field
read), build one :class:`TelegramEvent`, escape a bounded message, and call
``publish`` once. None of that scans prior alerts, so the **per-alert** cost is
O(1) in how many alerts were already sent. This gate asserts the amortised
per-alert time over a large burst stays within a small multiple of the per-alert
time over a small burst — a regression to super-linear behaviour (e.g. an
unbounded buffer scan) would blow the ratio.

Honest scope (Golden Rule #10): sending N alerts is inherently O(N) — this gates
the PER-ALERT cost in N, NOT an O(1)-total claim, which is physically impossible.

Wheel-free: ``brutex.notifications.sweep_alerts`` imports only the pure-Python
Telegram channel (``brutex.notifications.telegram`` → ``brutex.models``), so this
bench needs no ``[data]`` / PyO3 extra.
"""

from __future__ import annotations

import time

from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 2_000
_LARGE = 20_000


class _SinkNotifier:
    """A bounded-work notifier: drops the event after counting it (no buffer growth)."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def publish(self, event: TelegramEvent) -> None:
        """Count the event without retaining it — O(1), no per-call buffer scan."""
        self.count += 1


def _amortised_per_alert_seconds(n: int) -> float:
    """Return the mean per-alert publish time over *n* watchdog-hung alerts."""
    alerter = SweepAlerter(notifier=_SinkNotifier())
    start = time.perf_counter()
    for i in range(n):
        alerter.watchdog_hung(
            worker_name=f"sweep-worker-{i}",
            last_beat_seconds_ago=float(i),
            timeout_seconds=300.0,
        )
    return (time.perf_counter() - start) / n


def test_publish_is_o1_per_alert() -> None:
    """Per-alert time at N_LARGE stays within the shared-CI ceiling of N_SMALL."""
    # Warm up so import / first-call costs don't skew the small-tier baseline.
    _amortised_per_alert_seconds(_SMALL)
    small = _amortised_per_alert_seconds(_SMALL)
    large = _amortised_per_alert_seconds(_LARGE)
    ceiling = ratio_ceiling(8.0)
    # Guard against a divide-by-zero on a too-fast small tier (sub-ns clocks).
    assert large <= max(small, 1e-9) * ceiling
