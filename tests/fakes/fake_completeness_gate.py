"""Fakes for the today-completeness gate seam (sibling MIGRATION.md #1751).

The gate module (``brutex/data/today_completeness.py``) is NOT yet merged, so
the producer tests drive :mod:`brutex.crossverify` through these doubles:

* :class:`FakeVerdict` — a structural match of the sibling's
  ``TodayCompletenessVerdict`` subset the producer reads
  (:class:`brutex.crossverify.gate_seam.VerdictLike`);
* :class:`ScriptedClassifier` — returns a pre-configured verdict per
  ``(lake_symbol)`` (the unit-matrix driver), with a thread-safe call log;
* :func:`faithful_classify` — a CONTRACT-FAITHFUL twin of the sibling's
  per-class laws (INDEX count==expected AND last-bar; CASH all-zero-volume
  only; FUT any-OI-null or all-zero-volume; ``no_today_bars`` on empty) used
  by the golden chain until the real module merges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from brutex.data.session import IST

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Sequence
    from datetime import date

    from brutex.data.models import CandleBar


@dataclass(frozen=True, slots=True)
class FakeVerdict:
    """A structural ``VerdictLike`` with every field configurable."""

    covers_today: bool = True
    incomplete: bool = False
    reason: str = "complete"
    expected_minutes: int | None = None
    distinct_minutes: int | None = None
    last_bar_ist: str | None = None
    oi_null_count: int = 0
    zero_volume_count: int = 0
    count_short_observed: bool = False


@dataclass
class ScriptedClassifier:
    """Return a scripted verdict per call — keyed by the bars' length is too
    lossy, so the script is a queue per instrument_class OR a default."""

    default: FakeVerdict = field(default_factory=FakeVerdict)
    by_class: dict[str, FakeVerdict] = field(default_factory=dict)
    calls: list[tuple[int, str]] = field(default_factory=list)  # (len(bars), class)

    def __call__(
        self,
        *,
        bars_today: Sequence[CandleBar],
        clamp_today: date,
        window_end: date,
        instrument_class: str,
    ) -> FakeVerdict:
        """Record the call and return the scripted verdict."""
        _ = (clamp_today, window_end)
        self.calls.append((len(bars_today), instrument_class))
        return self.by_class.get(instrument_class, self.default)


def faithful_classify(
    *,
    bars_today: Sequence[CandleBar],
    clamp_today: date,
    window_end: date,
    instrument_class: str,
) -> FakeVerdict:
    """A contract-faithful twin of the sibling gate's per-class laws.

    Used by the golden/export chains until ``brutex.data.today_completeness``
    merges (its laws per the pinned contract: expected minutes from the real
    session; INDEX = count==expected AND last regular bar 15:29; CASH =
    all-zero-volume shape only; FUT = ANY OI-null OR all-zero-volume).
    """
    from brutex.calendars.nse import get_session

    _ = window_end
    session = get_session(clamp_today)
    expected = session.duration_minutes if session is not None else None
    if session is None:
        return FakeVerdict(
            covers_today=False, incomplete=False, reason="non_trading", expected_minutes=None
        )
    minutes = sorted({bar.timestamp.astimezone(IST).strftime("%H:%M") for bar in bars_today})
    distinct = len(minutes)
    last = minutes[-1] if minutes else None
    if distinct == 0:
        return FakeVerdict(
            covers_today=False,
            incomplete=False,
            reason="no_today_bars",
            expected_minutes=expected,
            distinct_minutes=0,
        )
    zero_volume = sum(1 for bar in bars_today if bar.volume == 0)
    oi_null = sum(1 for bar in bars_today if bar.open_interest is None)

    def _verdict(reason: str, *, incomplete: bool) -> FakeVerdict:
        return FakeVerdict(
            covers_today=True,
            incomplete=incomplete,
            reason=reason,
            expected_minutes=expected,
            distinct_minutes=distinct,
            last_bar_ist=last,
            oi_null_count=oi_null,
            zero_volume_count=zero_volume,
        )

    if instrument_class == "index_spot":
        if expected is not None and distinct < expected:
            return _verdict("count_short", incomplete=True)
        if expected is not None and distinct > expected:
            return _verdict("count_excess", incomplete=True)
        return _verdict("complete", incomplete=False)
    if instrument_class == "cash_spot":
        if zero_volume == len(bars_today):
            return _verdict("volume_all_zero", incomplete=True)
        return _verdict("complete", incomplete=False)
    # fut
    if oi_null > 0:
        return _verdict("oi_null_settling", incomplete=True)
    if zero_volume == len(bars_today):
        return _verdict("volume_all_zero", incomplete=True)
    return _verdict("complete", incomplete=False)


__all__ = ["FakeVerdict", "ScriptedClassifier", "faithful_classify"]
