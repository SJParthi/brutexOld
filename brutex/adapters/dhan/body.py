"""Dhan v2 historical-data request body builder.

Constructs the JSON body for the two Track 1 endpoints:

* ``POST /v2/charts/intraday`` — 1/5/15/25/60-minute OHLC, 90-day window.
* ``POST /v2/charts/historical`` — daily OHLC.

The body fields are pinned per the Dhan v2 historical-data SDK signature
(see CLAUDE.md §6 verified facts table for the field-name convention).
This module is pure: it produces dicts ready for ``json.dumps``; no HTTP,
no token, no auth. The transport adapter (Phase 2 PR #2b) consumes these.

Why a dedicated module:

* **Type safety:** ``interval`` is a Dhan-enforced string ("1", "5",
  "15", "25", "60"); int values raise Dhan code 814 ("Invalid Request").
* **Endpoint divergence:** intraday body has ``interval``; historical
  body does NOT (must be OMITTED — sending ``"interval": null`` triggers
  814).
* **Invariants:** ``oi`` must be ``False`` for all Track 1 INDEX
  instruments (OI is only meaningful on derivatives — sending ``True``
  returns empty OI arrays or 814).
* **Chunk discipline:** intraday window must be ≤ 90 days per call (CLAUDE.md
  §6 verified fact "Chunk window (intraday) | 90 days per call").

References
----------
* CLAUDE.md §6 Verified Dhan Facts
* docs/TRACK1_DATA_INGEST_DESIGN.md (pull_chunk pseudocode)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from brutex.adapters.dhan.constants import DhanSpotInstrument

#: Native intraday timeframes supported by Dhan v2 (CLAUDE.md §6).
#: Other timeframes (e.g., 3m, 30m) are resampled deterministically from
#: 1m base on our side — never requested directly.
SUPPORTED_INTRADAY_INTERVALS: Final[tuple[str, ...]] = ("1", "5", "15", "25", "60")

#: Maximum window length (inclusive) for a single intraday request.
#: Dhan docs publish "90 days per call". We treat this as inclusive both
#: ends (i.e., from_date and to_date both count toward the 90). Until
#: empirically verified by the smoke test in PR #2c, callers SHOULD use
#: this constant rather than literal 90.
INTRADAY_MAX_WINDOW_DAYS: Final[int] = 90


@dataclass(frozen=True, slots=True)
class IntradayChunkRequest:
    """One request for ``POST /v2/charts/intraday``.

    Frozen + slots — every chunk request is an immutable record. The
    chunk planner (future module) emits a tuple of these; the transport
    adapter (PR #2b) consumes one at a time.

    Attributes
    ----------
    instrument
        The :class:`DhanSpotInstrument` to fetch. The body builder reads
        ``security_id``, ``segment``, and ``instrument_type`` from it.
    interval
        Dhan-encoded timeframe string — must be one of
        :data:`SUPPORTED_INTRADAY_INTERVALS`.
    from_date
        Inclusive start date (IST trading-day semantics).
    to_date
        Inclusive end date (IST). ``to_date >= from_date`` and
        ``(to_date - from_date).days < INTRADAY_MAX_WINDOW_DAYS``.

    Raises
    ------
    ValueError
        On invalid interval, reversed window, or oversize window.
    """

    instrument: DhanSpotInstrument
    interval: str
    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if self.interval not in SUPPORTED_INTRADAY_INTERVALS:
            raise ValueError(
                f"IntradayChunkRequest: interval must be one of "
                f"{SUPPORTED_INTRADAY_INTERVALS}, got {self.interval!r}"
            )
        if self.to_date < self.from_date:
            raise ValueError(
                f"IntradayChunkRequest: to_date ({self.to_date}) must be >= "
                f"from_date ({self.from_date})"
            )
        span = (self.to_date - self.from_date).days
        if span >= INTRADAY_MAX_WINDOW_DAYS:
            raise ValueError(
                f"IntradayChunkRequest: window {span} days exceeds Dhan's "
                f"{INTRADAY_MAX_WINDOW_DAYS}-day cap (split via chunk planner)"
            )


@dataclass(frozen=True, slots=True)
class HistoricalDailyRequest:
    """One request for ``POST /v2/charts/historical`` (daily OHLC).

    Used for PDH/PDL anchor backfill — no chunking ceiling documented
    by Dhan for the daily endpoint, so the caller may request the full
    rolling range in a single call.
    """

    instrument: DhanSpotInstrument
    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if self.to_date < self.from_date:
            raise ValueError(
                f"HistoricalDailyRequest: to_date ({self.to_date}) must be "
                f"!>= from_date ({self.from_date})"
            )


def build_intraday_body(req: IntradayChunkRequest) -> dict[str, Any]:
    """Build the JSON body for ``POST /v2/charts/intraday``.

    The output dict is ready for ``json.dumps``. Field names use Dhan's
    camelCase convention. The ``oi`` flag is locked to ``False`` for
    Track 1 (all 4 instruments are INDEX — no open interest).
    """
    return {
        "securityId": str(req.instrument.security_id),
        "exchangeSegment": req.instrument.segment,
        "instrument": req.instrument.instrument_type,
        "interval": req.interval,
        "oi": False,
        "fromDate": req.from_date.isoformat(),
        "toDate": req.to_date.isoformat(),
    }


def build_historical_body(req: HistoricalDailyRequest) -> dict[str, Any]:
    """Build the JSON body for ``POST /v2/charts/historical`` (daily).

    The ``interval`` field is OMITTED (not None) — sending an explicit
    null triggers Dhan code 814. ``oi`` stays ``False`` for Track 1.
    """
    return {
        "securityId": str(req.instrument.security_id),
        "exchangeSegment": req.instrument.segment,
        "instrument": req.instrument.instrument_type,
        "oi": False,
        "fromDate": req.from_date.isoformat(),
        "toDate": req.to_date.isoformat(),
    }
