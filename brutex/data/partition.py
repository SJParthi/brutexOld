"""Pure partition-path computation for the canonical data lake.

Implements ``FR-CAT-001``: canonical bars SHALL be stored as Parquet,
partitioned by::

    <lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet

This module is **intentionally pure**: no I/O, no heavy imports,
no clock. Everything is a deterministic function of the inputs so
it is trivial to unit-test and to reason about for NFR-DET-001
reproducibility.

## Interval whitelist

Only the 23 canonical candle intervals are accepted (the 21 Track 1
timeframe labels + ``1week``/``1month``, DEC-TF-VOCAB-21-001). Passing any
other string raises :class:`PartitionError`.

## Symbol sanitisation

Canonical contract symbols contain hyphens (``NSE-NIFTY-02Jan25-28500-PE``) which
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
from brutex.data.timeframes import LAKE_INTERVAL_LABELS

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: The top-level subdirectory inside the lake root where bar partitions
#: live. Future per-type subdirs (e.g. ``orderflow/``, ``greeks/``) will
#: sit alongside ``bars/``.
BAR_DIR_NAME: Final[str] = "bars"


#: Minimum acceptable partition year. BruteX supports 2000 onward
#: because historical data begins around 2000 and NSE itself
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

#: The 23 canonical candle intervals accepted in partition paths: the 21
#: Track 1 timeframe labels (DEC-TF-VOCAB-21-001 — derived from the canonical
#: ``brutex.data.timeframes.LAKE_INTERVAL_LABELS`` map, the single source of
#: truth) plus the two calendar-aware labels (``1week``/``1month``, P3
#: ADR-0014) that pre-date the 21-TF ladder.
VALID_INTERVALS: Final[frozenset[str]] = frozenset(LAKE_INTERVAL_LABELS.values()) | frozenset(
    {
        "1week",
        "1month",
    }
)


#: Exchanges BruteX stores bars for.
VALID_EXCHANGES: Final[frozenset[str]] = frozenset({"NSE", "BSE"})


#: Segments BruteX stores bars for. ``INDEX`` (vendor ``IDX_I``) was added by
#: DEC-INDEX-SEGMENT-PK-FIX-001 (#406) — index instruments now carry
#: ``segment='INDEX'``, so a spot-candle pull for NIFTY/BANKNIFTY/SENSEX routes
#: an ``INDEX`` segment into the partition path; without it the lake-write
#: rejected every index spot pull (the live ``segment must be one of
#: ['CASH','FNO'], got 'INDEX'`` crash). DEC-PARTITION-INDEX-SEGMENT-001.
VALID_SEGMENTS: Final[frozenset[str]] = frozenset({"INDEX", "CASH", "FNO"})

#: The spot segments an instrument's UNDERLYING rides, in PROBE ORDER: an index
#: underlying lives under ``INDEX``, a single-stock underlying under ``CASH`` (a tuple,
#: not the unordered :data:`VALID_SEGMENTS` frozenset, because the probe order matters).
#: The ONE source of truth for that order — both the movers underlying-spot resolver
#: (``movers_futures._SPOT_SEGMENTS``) and the ``/db`` per-bar moneyness spot join
#: (``db_view._SPOT_PROBE_SEGMENTS``) import it, so the order can never drift between the
#: two surfaces (CLAUDE.md §11 COMMON; DEC-FNO-MONEYNESS-001).
SPOT_PROBE_SEGMENTS: Final[tuple[str, ...]] = ("INDEX", "CASH")


#: Characters that would break filesystem paths on Linux / macOS /
#: Windows, PLUS the glob metachars `[`/`]` (DEC-SPOT-DERIVE-TS-RANGE-
#: HARDENING-001, completing the #1594 round-15 metachar law write-side): a
#: symbol dir named ``NSE-X[12]`` beside real ``NSE-X1``/``NSE-X2`` siblings
#: makes any glob-interpreting read pull the SIBLINGS' bars into the wrong
#: symbol (executed-repro'd on the engine feed mouth ``Catalog.get_bars``).
#: `*`/`?` were already members; no legitimate vendor symbol carries a
#: bracket (NSE trading symbols are alphanumeric plus ``&``/``-``/``.`` —
#: verified against the real instrument-master fixtures). A symbol containing
#: any of these is rejected.
_FORBIDDEN_SYMBOL_CHARS: Final[frozenset[str]] = frozenset(
    {"/", "\\", ":", "*", "?", '"', "<", ">", "|", "\0", "[", "]"}
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
    """Raise PartitionError if *interval* is not a canonical interval name."""
    if interval not in VALID_INTERVALS:
        raise PartitionError(
            f"interval must be one of the {len(VALID_INTERVALS)} canonical intervals, "
            f"got {interval!r}"
        )


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


def option_partition_stem(year: int, month: int) -> str:
    """The canonical zero-padded stem of one expired-options month partition.

    THE one source of the ``options/<series>/<YYYY>-<MM>.parquet`` stem format
    (CLAUDE.md §11 COMMON — a hostile-review NIT found the f-string duplicated
    at four load-bearing sites: ``OptionParquetWriter.write``'s destination,
    ``lake_invariants._option_file_month``'s canonical-stem check,
    ``lake_invariants._canonical_option_keys``'s sibling path and
    ``lake_heal._heal_option_foreign``'s reassignment destination — a drift in
    any one would desynchronise routing from addressing). Pure formatting; the
    inverse parse (+ its canonical-form/bounds gates) lives in
    ``lake_invariants._option_file_month``.
    """
    return f"{year:04d}-{month:02d}"
