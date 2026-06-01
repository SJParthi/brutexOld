"""Synthetic spot-bar generator — the dashboard/cli dry-run data source.

The Dhan analog of the legacy ``groww.ingest.SyntheticCandleFetcher``
(DEC-VENDOR-PIVOT-001): emits valid synthetic 1-minute
:class:`~brutex.data.models.CandleBar` rows so the real parse → session-filter
→ sink pipeline runs **offline** (no token, no network) — the dry-run proof
that the pipe works before any credentials are set.

Pure (no I/O): a day + a count in, a list of in-session ``CandleBar`` rows
out, so it is 100% unit-testable and the dashboard/cli can preview a pull with
zero side effects. Timestamps start at **09:15 IST** (session open) and step
by one minute, so every bar survives the
:func:`~brutex.data.session.in_session` filter (CLAUDE.md §7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar

#: India Standard Time — the synthetic bars use the NSE/BSE wall clock.
_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

#: Session open (09:15 IST) — the first synthetic bar's wall-clock time.
_SESSION_OPEN: time = time(9, 15)


def synthetic_spot_bars(
    day: date,
    *,
    count: int = 5,
    base_price: Decimal = Decimal("100"),
    interval_minutes: int = 1,
) -> list[CandleBar]:
    """Generate *count* valid in-session synthetic spot bars for *day*.

    Parameters
    ----------
    day
        The trading day the bars belong to.
    count
        Number of bars to emit (must be > 0).
    base_price
        The first bar's open; each subsequent bar steps +1 rupee.
    interval_minutes
        Spacing between bars in minutes (default 1).

    Returns
    -------
    list[CandleBar]
        *count* tz-aware (UTC) bars starting at 09:15 IST, each with a valid
        ``low <= open, close <= high`` OHLC and a small synthetic volume.

    Raises
    ------
    ValueError
        If *count* is not positive.
    """
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")
    start = datetime.combine(day, _SESSION_OPEN, tzinfo=_IST)
    bars: list[CandleBar] = []
    for i in range(count):
        open_px = base_price + i
        bars.append(
            CandleBar(
                timestamp=(start + timedelta(minutes=i * interval_minutes)).astimezone(UTC),
                open=open_px,
                high=open_px + 1,
                low=open_px - 1,
                close=open_px + Decimal("0.5"),
                volume=1000 + i,
                open_interest=None,
            )
        )
    return bars


__all__ = [
    "synthetic_spot_bars",
]
