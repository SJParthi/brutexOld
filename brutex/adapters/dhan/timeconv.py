"""IST ↔ UTC conversion helpers for Dhan API timestamps.

Dhan v2 historical endpoints return `timestamp` as **epoch seconds anchored
to IST wall-clock minute boundaries**. Naïvely treating the integer as UTC
epoch seconds shifts every bar 5h30m later than its true IST time, which
breaks:

* Parquet partitioning by IST trading-day (bars leak across boundaries).
* Microstructure invariant P-22 (NSE holiday bars must not appear).
* The audit-replay oracle (Golden Rule #7 reproducibility) because every
  re-fetch reproduces the same WRONG anchor.

This module is the **single source of truth** for the conversion (Golden
Rule #11 COMMON). The two public functions are inverses and bit-exact.

References
----------
* CLAUDE.md §6: "Time format | IST market times (convert to UTC at ingest)"
* CLAUDE.md §7: trading hours 09:15-15:30 IST
* docs/TRACK1_DATA_INGEST_DESIGN.md: pull_chunk pseudocode
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

#: India Standard Time = UTC+5:30 (no DST). Locked tzinfo singleton for
#: the entire codebase. Importing from elsewhere risks divergent
#: time-zone definitions; downstream modules MUST use this object.
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

#: UTC ZoneInfo singleton. Mirrors :data:`IST` style.
UTC: ZoneInfo = ZoneInfo("UTC")


def dhan_epoch_to_utc(epoch_seconds: int) -> datetime:
    """Convert a Dhan historical timestamp to a tz-aware UTC datetime.

    Dhan returns ``timestamp`` as integer epoch seconds whose wall-clock
    interpretation is IST (09:15 IST = naïve epoch ``ts`` such that
    ``datetime.fromtimestamp(ts)`` on a UTC-locale host gives ``09:15``).

    The conversion is:

    1. Parse the epoch as a *naïve* local time.
    2. Attach IST tzinfo (declares "this 09:15 means IST").
    3. ``.astimezone(UTC)`` (shifts back 5h30m to get true UTC).

    Parameters
    ----------
    epoch_seconds
        Dhan's raw integer timestamp (seconds since 1970-01-01 IST-wall).

    Returns
    -------
    datetime
        Tz-aware UTC datetime. ``dt.tzinfo is UTC`` always holds.

    Examples
    --------
    >>> from datetime import datetime
    >>> # 2024-03-01 09:15 IST → 2024-03-01 03:45 UTC
    >>> dt = dhan_epoch_to_utc(1709263500)
    >>> dt.isoformat()
    '2024-03-01T03:45:00+00:00'
    """
    naive = datetime.fromtimestamp(epoch_seconds, tz=UTC).replace(tzinfo=None)
    ist_aware = naive.replace(tzinfo=IST)
    return ist_aware.astimezone(UTC)


def utc_to_dhan_epoch(dt: datetime) -> int:
    """Convert a tz-aware UTC datetime to Dhan's IST-anchored epoch seconds.

    Inverse of :func:`dhan_epoch_to_utc`. Used by golden tests to round-trip
    a known UTC moment and assert the integer matches Dhan's expected wire
    format.

    Parameters
    ----------
    dt
        Tz-aware datetime. Must have ``tzinfo`` set; naïve datetimes raise
        ``ValueError`` (we never guess time-zones — Golden Rule #1).

    Returns
    -------
    int
        Dhan's epoch-seconds form (IST wall-clock interpreted as UTC).

    Raises
    ------
    ValueError
        If *dt* is naïve (``tzinfo is None``).
    """
    if dt.tzinfo is None:
        raise ValueError(
            "utc_to_dhan_epoch: refusing naïve datetime; pass a tz-aware datetime "
            "(use brutex.adapters.dhan.timeconv.UTC for UTC moments)"
        )
    ist_aware = dt.astimezone(IST)
    naive_as_if_utc = ist_aware.replace(tzinfo=UTC)
    return int(naive_as_if_utc.timestamp())
