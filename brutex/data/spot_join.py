"""COMMON same-minute underlying-spot join for F&O readers (plan §15.4).

The generation-time Greeks enrichment (`DEC-GREEKS-GENERATION-TIME-001`,
``docs/GREEKS_ENGINE_PLAN.md`` §15.4) needs, per option bar, the underlying
spot close at the IDENTICAL timestamp instant — the SAME exact-``utc_instant``
join contract the ``/db`` moneyness derive already satisfies
(``brutex/dashboard/db_view.py`` ``_underlying_spot_close_by_ts``). This module
is the plan's "lift to a shared ``brutex/data/`` helper" (Golden Rule #11
COMMON): the probe order (:data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS` —
``INDEX`` then ``CASH``), the instant normaliser (:func:`utc_instant`), and a
**window-bounded** lake read (:func:`spot_close_by_ts_window`) bounded via
:func:`~brutex.data.spot_daily_resample.ist_span_partition_files` — O(pulled
window) month files, never O(lake-depth).

Honest scope note (Golden Rule #10): ``db_view`` keeps its DuckDB,
page-``BETWEEN``-bounded variant for the read-time ``/db`` path (it rides the
view's shared connection + resilient-read machinery); the two variants share
the SAME probe segments, the SAME exact-instant join key, and the SAME
never-fabricate contract. Collapsing the DuckDB variant onto this reader is
Phase 3a's read-surface work, not Phase 2b's.

The join result additionally reports WHICH probe segment supplied the spot:
an ``INDEX`` underlying is a European-exercise index option, a ``CASH``
underlying an American-exercise single stock — the exercise-style signal the
enrichment stamps (plan §15.5; the lake segment is written from the master's
``instrument_type`` at spot-ingest time, so the probe is master-faithful).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from brutex.data.partition import SPOT_PROBE_SEGMENTS
from brutex.data.spot_daily_resample import ist_span_partition_files

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

__all__ = [
    "SpotJoin",
    "spot_close_by_ts_window",
    "utc_instant",
]


def utc_instant(ts: datetime) -> datetime:
    """Normalise a bar timestamp to a UTC-anchored instant for cross-series joining.

    Both the option bars and the underlying spot bars are stored tz-aware UTC,
    so they already compare equal at the same instant; normalising to UTC makes
    the dict KEY representation identical too (belt-and-braces against a
    tz-rendering difference). A naive datetime (defensive) is read as UTC.
    """
    return ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class SpotJoin:
    """One resolved same-minute spot join: the map plus its provenance.

    Attributes
    ----------
    close_by_ts
        ``{utc_instant: Decimal(close)}`` for every non-null spot close in the
        window. Empty when the underlying has no stored spot (an honest empty
        join — every consumer degrades to its no-spot NULL, never a fabricated
        value; Golden Rule #1).
    segment
        The probe segment that supplied the spot (``"INDEX"`` / ``"CASH"``),
        or ``None`` when no segment held any file — the exercise-style signal
        (plan §15.5: ``INDEX`` → European exact, ``CASH`` → American approx).
    """

    __slots__ = ("close_by_ts", "segment")

    def __init__(self, close_by_ts: dict[datetime, Decimal], segment: str | None) -> None:
        self.close_by_ts = close_by_ts
        self.segment = segment


def spot_close_by_ts_window(
    lake_root: Path,
    *,
    exchange: str,
    underlying: str,
    interval: str,
    start: date,
    end: date,
) -> SpotJoin:
    """Read the underlying's ``{utc_instant: close}`` map over an IST date window.

    Probes the underlying spot under :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS`
    (``INDEX`` then ``CASH``) at *interval*, reading ONLY the
    ``<yyyy>/<mm>.parquet`` month partitions the half-open IST span
    ``[start, end)`` covers (:func:`~brutex.data.spot_daily_resample.ist_span_partition_files`
    — O(pulled-window), never O(lake-depth)). The FIRST segment with any
    covered file wins; a null ``close`` row is dropped (that instant degrades
    to the consumer's no-spot NULL, never a crash).

    Reads are schema-tolerant (``missing_columns="insert"`` /
    ``extra_columns="ignore"``) so a mixed 7-column/enriched-17-column tree can
    never break the join. Returns an EMPTY :class:`SpotJoin` (``segment=None``)
    when no segment holds a covered file — an honest absence, never a
    fabricated spot (Golden Rule #1).
    """
    import polars as pl

    from brutex.data.lake_read import dedup_bars_frame

    for segment in SPOT_PROBE_SEGMENTS:
        base = lake_root / "bars" / exchange / segment / underlying / interval
        files = ist_span_partition_files(base, start, end)
        if not files:
            continue
        frame = (
            # glob=False: LITERAL month-file list (DEC-SPOT-DERIVE-TS-RANGE-
            # HARDENING-001, the #1594 round-15 metachar law) — a `[`-carrying
            # underlying dir must never glob-read its siblings' spot closes.
            pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore", glob=False)
            .select("timestamp", "close")
            .filter(pl.col("close").is_not_null())
            .collect()
        )
        # Keep-FIRST read dedup (DEC-LAKE-READ-UNIQUE-001): the dict build below
        # was LAST-wins on a poisoned-era duplicate instant — the first stored
        # non-null close wins now, warned, never silent.
        frame, _dropped = dedup_bars_frame(
            frame, source=f"spot_join:{exchange}/{segment}/{underlying}/{interval}"
        )
        close_by_ts = {
            utc_instant(ts): Decimal(str(close))
            for ts, close in zip(
                frame.get_column("timestamp").to_list(),
                frame.get_column("close").to_list(),
                strict=True,
            )
        }
        return SpotJoin(close_by_ts, segment)
    return SpotJoin({}, None)
