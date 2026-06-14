"""Diff live-feed 1-minute candles against backtest historical candles.

This is the payoff: given the live candles built by
:func:`~brutex.live.candle_builder.build_minute_candles` and the backtest candles
the lake stores (Groww ``get_historical_candles`` at ``1minute``), report — minute
by minute — whether they are **identical**, and where they diverge.

Bars are aligned by their UTC timestamp (both sides label the minute start in
UTC, so alignment is exact). For each aligned minute the configured price fields
are compared with :class:`~decimal.Decimal` equality (no float fuzz). Volume is
**excluded by default** — index bars carry ``volume = 0`` on both sides, so it
adds no signal; pass ``compare_volume=True`` to include it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from brutex.data.models import CandleBar

#: The price fields compared for "identical bar" by default.
DEFAULT_COMPARE_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class MinuteDiff:
    """One minute where live and backtest disagree (or one side is missing).

    Attributes
    ----------
    timestamp
        UTC minute start.
    live, backtest
        The two bars at this minute; either may be ``None`` (a gap on that side).
    reason
        ``"mismatch"`` (both present, fields differ), ``"live_only"`` (absent in
        backtest), or ``"backtest_only"`` (absent in live).
    """

    timestamp: datetime
    live: CandleBar | None
    backtest: CandleBar | None
    reason: str


@dataclass(frozen=True, slots=True)
class ParityReport:
    """The minute-by-minute parity verdict for ONE instrument.

    Attributes
    ----------
    instrument_key
        Which instrument this report covers.
    total_minutes
        Size of the union of live+backtest minute timestamps.
    matched
        Minutes present on both sides with all compared fields equal.
    mismatched
        Minutes present on both sides whose compared fields differ.
    live_only, backtest_only
        Minutes present on exactly one side.
    diffs
        Every non-matching minute (mismatch + gaps), ascending by timestamp.
    """

    instrument_key: str
    total_minutes: int
    matched: int
    mismatched: int
    live_only: int
    backtest_only: int
    diffs: tuple[MinuteDiff, ...]

    @property
    def is_identical(self) -> bool:
        """True iff every minute matched (no mismatches, no gaps either side)."""
        return self.mismatched == 0 and self.live_only == 0 and self.backtest_only == 0

    def summary(self) -> str:
        """A one-line human verdict for logs / the CLI."""
        verdict = "IDENTICAL ✅" if self.is_identical else "DIVERGES ❌"
        return (
            f"{self.instrument_key}: {verdict} — {self.matched}/{self.total_minutes} "
            f"matched, {self.mismatched} mismatched, {self.live_only} live-only, "
            f"{self.backtest_only} backtest-only"
        )


def _bars_equal(live: CandleBar, backtest: CandleBar, fields: Sequence[str]) -> bool:
    """True if *live* and *backtest* agree on every name in *fields*."""
    return all(getattr(live, name) == getattr(backtest, name) for name in fields)


def compare_candles(
    live: Sequence[CandleBar],
    backtest: Sequence[CandleBar],
    *,
    instrument_key: str,
    compare_fields: Sequence[str] = DEFAULT_COMPARE_FIELDS,
    compare_volume: bool = False,
) -> ParityReport:
    """Compare *live* vs *backtest* 1-minute candles, aligned by timestamp.

    Parameters
    ----------
    live, backtest
        1-minute candle sequences for the SAME instrument (any order; aligned
        internally by UTC timestamp).
    instrument_key
        Label carried into the report.
    compare_fields
        Price fields that must be equal for a minute to count as matched
        (default :data:`DEFAULT_COMPARE_FIELDS`).
    compare_volume
        When ``True``, ``"volume"`` is appended to the compared fields.

    Returns
    -------
    ParityReport
        The aggregated verdict + the list of diverging minutes.
    """
    fields = (*compare_fields, "volume") if compare_volume else tuple(compare_fields)
    live_by_ts = {bar.timestamp: bar for bar in live}
    backtest_by_ts = {bar.timestamp: bar for bar in backtest}
    all_ts = sorted(set(live_by_ts) | set(backtest_by_ts))

    matched = mismatched = live_only = backtest_only = 0
    diffs: list[MinuteDiff] = []
    for ts in all_ts:
        live_bar = live_by_ts.get(ts)
        backtest_bar = backtest_by_ts.get(ts)
        if live_bar is not None and backtest_bar is not None:
            if _bars_equal(live_bar, backtest_bar, fields):
                matched += 1
            else:
                mismatched += 1
                diffs.append(MinuteDiff(ts, live_bar, backtest_bar, "mismatch"))
        elif live_bar is not None:
            live_only += 1
            diffs.append(MinuteDiff(ts, live_bar, None, "live_only"))
        else:
            backtest_only += 1
            diffs.append(MinuteDiff(ts, None, backtest_bar, "backtest_only"))

    return ParityReport(
        instrument_key=instrument_key,
        total_minutes=len(all_ts),
        matched=matched,
        mismatched=mismatched,
        live_only=live_only,
        backtest_only=backtest_only,
        diffs=tuple(diffs),
    )


__all__ = ["DEFAULT_COMPARE_FIELDS", "MinuteDiff", "ParityReport", "compare_candles"]
