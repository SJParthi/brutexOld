"""Live-feed subscription SELF-TEST — prove the feed subscribes + ticks.

The operator's demand is "real guarantee, not talk": this module produces, from a
REAL run on the operator's Mac, the executable proof that the live feed (a) opened,
(b) accepted the subscription for EVERY requested instrument, and (c) is actually
delivering ticks — counted PER INSTRUMENT over a window. The orchestration reuses
:class:`~brutex.live.capture.TickCapture` (COMMON — one capture path) and is
unit-tested against a fake feed; only the real feed/instrument-master wiring in
:mod:`brutex.live.__main__` is ``# pragma: no cover``.

This is the honest answer to "how do you ASSURE it works + subscribed everything":
you run ``python -m brutex.live verify`` at 09:15 IST and read the per-instrument
table — silence on an instrument is itself a reported signal (subscribed but no
tick), never hidden.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from brutex.live.capture import SnapshotFn, SubscribeFn, TickCapture, _utc_now
from brutex.live.store import TickStore
from brutex.live.tick import extract_quotes


@dataclass(frozen=True, slots=True)
class InstrumentCheck:
    """Per-instrument verdict: did this instrument actually tick?"""

    instrument_key: str
    ticks: int

    @property
    def live(self) -> bool:
        """True if at least one tick was observed for this instrument."""
        return self.ticks > 0


@dataclass(frozen=True, slots=True)
class SubscriptionReport:
    """The subscription self-test verdict.

    Attributes
    ----------
    requested
        How many instruments we asked the feed to subscribe.
    observed
        Distinct instrument keys that appeared in at least one snapshot.
    total_ticks
        Total snapshot-instrument observations across the window.
    seconds
        The measurement window length.
    checks
        Per-observed-instrument tick counts, ascending by key.
    """

    requested: int
    observed: int
    total_ticks: int
    seconds: float
    checks: tuple[InstrumentCheck, ...]

    @property
    def all_requested_ticking(self) -> bool:
        """True iff every requested instrument was observed ticking."""
        return self.requested > 0 and self.observed >= self.requested

    @property
    def silent(self) -> int:
        """Requested instruments that produced no tick (subscribed but quiet)."""
        return max(self.requested - self.observed, 0)

    def summary(self) -> str:
        """One-line human verdict for logs / the CLI."""
        verdict = "ALL TICKING ✅" if self.all_requested_ticking else "SOME SILENT ⚠️"
        return (
            f"{verdict} — requested {self.requested}, observed {self.observed} "
            f"({self.total_ticks} ticks in {self.seconds:g}s, {self.silent} silent)"
        )


def verify_subscription(
    *,
    store: TickStore,
    subscribe: SubscribeFn,
    read_snapshot: SnapshotFn,
    requested: int,
    seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    clock: Callable[[], datetime] = _utc_now,
    ns_clock: Callable[[], int] = time.time_ns,
    poll_interval: float = 0.5,
) -> SubscriptionReport:
    """Subscribe to *requested* instruments and count ticks PER instrument.

    Reuses :class:`~brutex.live.capture.TickCapture` (so the captured ticks are
    ALSO persisted to *store* — the self-test doubles as a capture), tallying
    each snapshot's instruments via an ``on_record`` hook.

    Returns
    -------
    SubscriptionReport
        The per-instrument proof that the feed is delivering.
    """
    counts: dict[str, int] = {}

    def tally(record: Any) -> None:
        for instrument_key in extract_quotes(record.snapshot):
            counts[instrument_key] = counts.get(instrument_key, 0) + 1

    capture = TickCapture(
        store=store,
        subscribe=subscribe,
        read_snapshot=read_snapshot,
        clock=clock,
        ns_clock=ns_clock,
        on_record=tally,
    )
    capture.run_for(seconds, sleep=sleep, monotonic=monotonic, poll_interval=poll_interval)
    checks = tuple(InstrumentCheck(key, counts[key]) for key in sorted(counts))
    return SubscriptionReport(
        requested=requested,
        observed=len(counts),
        total_ticks=sum(counts.values()),
        seconds=seconds,
        checks=checks,
    )


def render_subscription_markdown(report: SubscriptionReport, *, max_rows: int = 60) -> str:
    """Render the subscription proof as a human Markdown table."""
    lines = [
        "# Live-feed subscription self-test",
        "",
        f"**{report.summary()}**",
        "",
        "| Instrument | Ticks | Live? |",
        "|---|---|---|",
    ]
    for check in report.checks[:max_rows]:
        lines.append(f"| {check.instrument_key} | {check.ticks} | {'✅' if check.live else '—'} |")
    if len(report.checks) > max_rows:
        lines.append("")
        lines.append(f"_…and {len(report.checks) - max_rows} more instruments._")
    return "\n".join(lines) + "\n"


__all__ = [
    "InstrumentCheck",
    "SubscriptionReport",
    "render_subscription_markdown",
    "verify_subscription",
]
