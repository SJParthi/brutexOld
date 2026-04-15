"""Pure partition-path computation for the canonical data lake.

Implements ``FR-CAT-001``: canonical bars SHALL be stored as Parquet,
partitioned by::

    <lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet

This module is **intentionally pure**: no I/O, no heavy imports,
no clock. Everything is a deterministic function of the inputs so
it is trivial to unit-test and to reason about for NFR-DET-001
reproducibility.

## Interval whitelist

Only the 12 Groww candle intervals documented in
``docs/groww-sdk/annexures.md §4`` are accepted. Passing any other
string raises :class:`PartitionError`.

## Symbol sanitisation

Groww symbols contain hyphens (``NSE-NIFTY-02Jan25-28500-PE``) which
are filesystem-safe. We still reject symbols containing characters
that would be problematic on any major OS: ``/`` ``\\`` ``:`` ``*``
``?`` ``"`` ``<`` ``>`` ``|`` or a null byte. This catches programming
errors early rather than silently producing a broken partition path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from brutex.data.errors import PartitionError

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: The top-level subdirectory inside the lake root where bar partitions
#: live. Future per-type subdirs (e.g. ``orderflow/``, ``greeks/``) will
#: sit alongside ``bars/``.
BAR_DIR_NAME: Final[str] = "bars"


#: Minimum acceptable partition year. BruteX supports 2000 onward
#: because Groww historical data begins around 2000 and NSE itself
#: only launched in 1994.
MIN_PARTITION_YEAR: Final[int] = 2000

#: Maximum acceptable partition year. Far enough in the future that
#: BruteX's adapters are guaranteed to be rewritten before this
#: becomes binding.
MAX_PARTITION_YEAR: Final[int] = 2100

#: Number of months in a Gregorian calendar year.
MONTHS_PER_YEAR: Final[int] = 12

#: Number of components in a canonical partition path:
#: ``bars / exchange / segment / symbol / interval / yyyy / mm.parquet``
PARTITION_PATH_COMPONENTS: Final[int] = 7

#: The 12 Groww candle intervals accepted in partition paths. Must be
#: kept in sync with ``brutex.adapters.groww.candles.MAX_DURATION_DAYS``.
VALID_INTERVALS: Final[frozenset[str]] = frozenset(
    {
        "1minute",
        "2minute",
        "3minute",
        "5minute",
        "10minute",
        "15minute",
        "30minute",
        "1hour",
        "4hour",
        "1day",
        "1week",
        "1month",
    }
)


#: Exchanges BruteX stores bars for.
VALID_EXCHANGES: Final[frozenset[str]] = frozenset({"NSE", "BSE"})


#: Segments BruteX stores bars for.
VALID_SEGMENTS: Final[frozenset[str]] = frozenset({"CASH", "FNO"})


#: Characters that would break filesystem paths on Linux / macOS /
#: Windows. A symbol containing any of these is rejected.
_FORBIDDEN_SYMBOL_CHARS: Final[frozenset[str]] = frozenset(
    {"/", "\\", ":", "*", "?", '"', "<", ">", "|", "\0"}
)


# ────────────────────────────────────────────────────────────────────────
# Validation helpers
# ────────────────────────────────────────────────────────────────────────


def _validate_exchange(exchange: str) -> None:
    """Raise PartitionError if *exchange* is not in VALID_EXCHANGES."""
    if exchange not in VALID_EXCHANGES:
        raise PartitionError(f"exchange must be one of {sorted(VALID_EXCHANGES)}, got {exchange!r}")


def _validate_segment(segment: str) -> None:
    """Raise PartitionError if *segment* is not in VALID_SEGMENTS."""
    if segment not in VALID_SEGMENTS:
        raise PartitionError(f"segment must be one of {sorted(VALID_SEGMENTS)}, got {segment!r}")


def _validate_interval(interval: str) -> None:
    """Raise PartitionError if *interval* is not a Groww canonical interval name."""
    if interval not in VALID_INTERVALS:
        raise PartitionError(f"interval must be one of the 12 Groww intervals, got {interval!r}")


def _validate_symbol(symbol: str) -> None:
    """Raise PartitionError on empty or filesystem-illegal symbols."""
    if not symbol:
        raise PartitionError("symbol must be a non-empty string")
    illegal = _FORBIDDEN_SYMBOL_CHARS.intersection(symbol)
    if illegal:
        raise PartitionError(f"symbol {symbol!r} contains forbidden characters: {sorted(illegal)}")


def _validate_year_month(year: int, month: int) -> None:
    """Raise PartitionError on out-of-range year or month values."""
    if year < MIN_PARTITION_YEAR or year > MAX_PARTITION_YEAR:
        raise PartitionError(
            f"year must be a 4-digit Gregorian year in "
            f"[{MIN_PARTITION_YEAR}, {MAX_PARTITION_YEAR}], got {year}"
        )
    if not (1 <= month <= MONTHS_PER_YEAR):
        raise PartitionError(f"month must be in [1, {MONTHS_PER_YEAR}], got {month}")


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────


def build_partition_path(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    year: int,
    month: int,
) -> Path:
    """Compute the canonical partition file path.

    Parameters
    ----------
    lake_root
        Top-level lake directory. Must be a :class:`pathlib.Path`.
    exchange, segment, symbol, interval, year, month
        The six coordinates defining the partition.

    Returns
    -------
    Path
        Absolute or relative path formed by joining *lake_root* with
        ``bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet``.
        The file may or may not exist; this function never touches
        the filesystem.

    Raises
    ------
    PartitionError
        If any coordinate fails validation.
    """
    _validate_exchange(exchange)
    _validate_segment(segment)
    _validate_symbol(symbol)
    _validate_interval(interval)
    _validate_year_month(year, month)

    filename = f"{month:02d}.parquet"
    return (
        lake_root / BAR_DIR_NAME / exchange / segment / symbol / interval / f"{year:04d}" / filename
    )


def parse_partition_path(path: Path | str, *, lake_root: Path | None = None) -> dict[str, object]:
    """Inverse of :func:`build_partition_path`.

    Parameters
    ----------
    path
        A partition path previously produced by
        :func:`build_partition_path` (or an equivalent path on disk).
    lake_root
        Optional lake root. If provided, *path* must be inside it and
        the relative suffix is used for parsing; if omitted, we parse
        from the end of *path* and ignore any leading directories.

    Returns
    -------
    dict
        ``{"exchange": str, "segment": str, "symbol": str,
        "interval": str, "year": int, "month": int}``.

    Raises
    ------
    PartitionError
        If the path cannot be parsed as a valid partition path.
    """
    p = PurePosixPath(Path(path).as_posix())

    if lake_root is not None:
        try:
            rel = Path(path).resolve().relative_to(lake_root.resolve())
        except (ValueError, OSError) as exc:
            raise PartitionError(f"path {path!r} is not inside lake_root {lake_root!r}") from exc
        parts = PurePosixPath(rel.as_posix()).parts
    else:
        parts = p.parts

    # Walk from the tail: <yyyy>/<mm>.parquet with 5 parent dirs above.
    if len(parts) < PARTITION_PATH_COMPONENTS:
        raise PartitionError(
            f"partition path must have at least {PARTITION_PATH_COMPONENTS} components "
            f"(bars / exchange / segment / symbol / interval / yyyy / mm.parquet), "
            f"got {len(parts)}: {parts}"
        )

    tail = parts[-7:]
    (
        bar_dir,
        exchange,
        segment,
        symbol,
        interval,
        year_str,
        month_filename,
    ) = tail
    if bar_dir != BAR_DIR_NAME:
        raise PartitionError(f"partition must live under {BAR_DIR_NAME}/, got {bar_dir!r}")
    if not month_filename.endswith(".parquet"):
        raise PartitionError(f"partition filename must end with .parquet, got {month_filename!r}")

    try:
        year = int(year_str)
    except ValueError as exc:
        raise PartitionError(f"year segment must be an integer, got {year_str!r}") from exc

    month_str = month_filename.removesuffix(".parquet")
    try:
        month = int(month_str)
    except ValueError as exc:
        raise PartitionError(
            f"month filename must be <mm>.parquet, got {month_filename!r}"
        ) from exc

    # Re-validate the parsed components so a hand-crafted bogus path
    # is rejected consistently.
    _validate_exchange(exchange)
    _validate_segment(segment)
    _validate_symbol(symbol)
    _validate_interval(interval)
    _validate_year_month(year, month)

    return {
        "exchange": exchange,
        "segment": segment,
        "symbol": symbol,
        "interval": interval,
        "year": year,
        "month": month,
    }


def partition_for_timestamp(ts: datetime) -> tuple[int, int]:
    """Compute the ``(year, month)`` partition coordinates of *ts*.

    The timestamp must be timezone-aware — naive datetimes are ambiguous
    (local vs UTC) and we reject them early rather than silently pick.

    Parameters
    ----------
    ts
        Timezone-aware datetime.

    Returns
    -------
    tuple[int, int]
        ``(year, month)`` in UTC coordinates.

    Raises
    ------
    PartitionError
        If *ts* is naive.
    """
    if ts.tzinfo is None:
        raise PartitionError(
            f"partition_for_timestamp requires tz-aware datetime, got naive {ts!r}"
        )
    utc = ts.astimezone(tz=UTC)
    return (utc.year, utc.month)
