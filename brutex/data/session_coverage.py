"""READ-ONLY session-coverage + ledger-quarantine scanners for the sweep preflight.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (Phase 1, WP3). The hard refuse-whole sweep
data preflight (``brutex.engine.sweep_gate``) needs DISK-verified answers to two
questions BEFORE any preload:

1. *Which expected NSE session dates does each requested (symbol, TF) pair
   actually hold on disk?* — :func:`scan_selection_coverage`. ``1minute`` /
   ``1day`` pairs are session-DATE-verified from the stored timestamp column
   (:class:`ScanKind.DATES`); the 19 derived intraday TFs get a span-bounded
   physical PRESENCE check (:class:`ScanKind.PRESENCE`) — never an unbounded
   ``rglob``. DISK is the presence authority (a ledger row is never consulted
   for presence — DISK-verified, never ledger-only).
2. *Which windows in the requested range does the durable window ledger flag
   as demoted / accepted-gap?* — :func:`scan_ledger_quarantine`, exactly ONE
   read-only :func:`~brutex.data.window_ledger_store.load_window_ledger` parse
   per launch. The ledger can only ADD refusals, never approve.

**Read-only law:** neither scanner ever writes — no lock file, no ledger
delta, no plan/seed/record call (the pull machinery
``plan_windows``/``record``/``seed_*`` is NEVER imported here — R3, the sweep
never pulls). A corrupt Parquet file is NAMED into
:attr:`PairScan.corrupt_files` (one WARN per file), never a traceback
(:func:`~brutex.data.errors.parquet_read_skip_errors` — the narrow COMMON
skip set; a genuine code bug still propagates).

**Honest cost envelope (Golden Rule #10):** the coverage scan is
**O(requested symbols × window-months)** file probes + timestamp-column reads
— NOT O(1); it never walks the whole lake tree (the 2.35M-file trap), so it
is independent of UNRELATED lake depth (bench-pinned,
``tests/bench/test_sweep_preflight_probe_o1.py``). The ledger scan is ONE
O(ledger-rows) parse per launch (spy-pinned).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.errors import parquet_read_skip_errors
from brutex.data.partition import BAR_DIR_NAME, build_partition_path
from brutex.data.spot_daily_resample import ist_span_partition_months
from brutex.data.window_ledger import DEMOTED_SETTLE_REASONS, FAILED, SKIPPED
from brutex.data.window_ledger_store import load_window_ledger

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "DATE_CHECKED_INTERVALS",
    "LedgerRowFlag",
    "PairScan",
    "QuarantineScan",
    "ScanKind",
    "scan_ledger_quarantine",
    "scan_selection_coverage",
]

_LOG = structlog.get_logger("brutex.data.session_coverage")

#: The two intervals whose stored bars are session-DATE verifiable: the fetched
#: ``1minute`` and the derived-but-date-checkable ``1day`` (one midnight-IST bar
#: per session — DEC-1MINUTE-ONLY-DERIVE-1DAY-001). Every OTHER requested TF is
#: a derived intraday timeframe and gets the span-bounded PRESENCE check.
DATE_CHECKED_INTERVALS: Final[frozenset[str]] = frozenset({"1minute", "1day"})

#: The fixed IST offset (+05:30, no DST) — a stored UTC bar's trading-session
#: date is ``(ts_utc + 05:30).date()`` (the lake convention,
#: :mod:`brutex.data.spot_daily_resample`).
_IST_OFFSET: Final[timedelta] = timedelta(hours=5, minutes=30)

#: The ledger statuses a demotion tag can ride: an OPEN demotion (``FAILED`` —
#: will re-pull) or an ACCEPTED gap (``SKIPPED`` — bars kept, gap accepted, the
#: stored data is KNOWN-incomplete for that window). Restated from the ledger's
#: own status vocabulary (imported, never re-spelled).
_QUARANTINE_STATUSES: Final[frozenset[str]] = frozenset({FAILED, SKIPPED})


class ScanKind(StrEnum):
    """How a (symbol, interval) pair was verified on disk."""

    #: ``1minute`` / ``1day`` — session-date coverage read from stored timestamps.
    DATES = "dates"
    #: The 19 derived intraday TFs — span-bounded physical existence only.
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class PairScan:
    """One (symbol, interval) pair's read-only disk-scan result.

    Attributes
    ----------
    symbol
        The lake symbol directory name (e.g. ``NSE-NIFTY``).
    interval
        The canonical interval label (e.g. ``1minute`` / ``5minute``).
    kind
        :class:`ScanKind` — how the pair was verified.
    covered
        DISTINCT IST session dates found on disk inside the requested span
        (:class:`ScanKind.DATES` pairs only; empty for PRESENCE pairs).
    present
        Span-bounded physical-existence verdict: ``True`` iff at least one
        month partition file of the requested span exists on disk.
    corrupt_files
        Unreadable Parquet files (absolute path strings) hit during the scan —
        each becomes a NAMED ``CORRUPT_PARTITION`` refusal, never a traceback.
    bars_found
        Deduped DISTINCT-timestamp count inside the span (observability only —
        physical rows ≠ bars; duplicate timestamps collapse by construction).
    months_probed
        Month coordinates examined — the honesty tally for logs/bench (the
        scan's cost is O(window-months), never O(lake-files)).
    """

    symbol: str
    interval: str
    kind: ScanKind
    covered: frozenset[date]
    present: bool
    corrupt_files: tuple[str, ...]
    bars_found: int
    months_probed: int


@dataclass(frozen=True, slots=True)
class LedgerRowFlag:
    """One demoted / accepted-gap window-ledger row overlapping the request.

    A projection of :class:`~brutex.data.window_ledger.WindowLedgerRecord`
    carrying exactly the fields the preflight quarantine fold renders —
    ``last_error`` is always a member of
    :data:`~brutex.data.window_ledger.DEMOTED_SETTLE_REASONS` (the filter
    guarantees non-``None``).
    """

    security_id: str
    interval: str
    window_start: str
    window_end: str
    status: str
    last_error: str


@dataclass(frozen=True, slots=True)
class QuarantineScan:
    """The ledger-quarantine scan result for one launch.

    Attributes
    ----------
    flags
        Every demoted (``FAILED``) / accepted-gap (``SKIPPED``) ledger row
        whose demotion tag is in ``DEMOTED_SETTLE_REASONS`` AND whose window
        overlaps the requested range for a requested (symbol, interval) —
        in the ledger's own deterministic record order.
    load_failed
        Surfaced VERBATIM from
        :attr:`~brutex.data.window_ledger.WindowLedger.load_failed`: the
        persisted ledger file existed but could not be read. The preflight
        treats this fail-CLOSED (``LEDGER_UNREADABLE``) — an unreadable ledger
        is the only reading under which "no quarantined partitions" is
        unverifiable.
    """

    flags: tuple[LedgerRowFlag, ...]
    load_failed: bool


def _scan_dates_pair(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    months: Sequence[tuple[int, int]],
    start: date,
    end: date,
    skip_errors: tuple[type[BaseException], ...],
) -> PairScan:
    """Session-DATE scan of one ``1minute``/``1day`` pair (read-only).

    Probes exactly the padded month partitions (*months*), reads ONLY the
    ``timestamp`` column of each existing file under the COMMON
    :func:`~brutex.data.errors.parquet_read_skip_errors` set (a corrupt file
    is NAMED into ``corrupt_files`` + one WARN, never a traceback), converts
    each stored UTC timestamp to its IST session date and intersects with
    ``[start, end]``. Distinct timestamps dedup by construction (a duplicated
    physical row never inflates ``bars_found``).
    """
    import polars as pl

    covered: set[date] = set()
    timestamps: set[object] = set()
    corrupt: list[str] = []
    probed = 0
    present = False
    for year, month in months:
        path = build_partition_path(
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=year,
            month=month,
        )
        probed += 1
        if not path.is_file():
            continue
        present = True
        try:
            column = pl.read_parquet(path, columns=["timestamp"]).get_column("timestamp")
        except skip_errors as exc:
            corrupt.append(str(path))
            _LOG.warning(
                "session_coverage.corrupt_file",
                path=str(path),
                symbol=symbol,
                interval=interval,
                error=str(exc),
            )
            continue
        for ts in column.to_list():
            ist_date = (ts + _IST_OFFSET).date()
            if start <= ist_date <= end:
                covered.add(ist_date)
                timestamps.add(ts)
    return PairScan(
        symbol=symbol,
        interval=interval,
        kind=ScanKind.DATES,
        covered=frozenset(covered),
        present=present,
        corrupt_files=tuple(corrupt),
        bars_found=len(timestamps),
        months_probed=probed,
    )


def _scan_presence_pair(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    months: Sequence[tuple[int, int]],
) -> PairScan:
    """Span-bounded PRESENCE scan of one derived-intraday-TF pair (read-only).

    ``dir.is_dir() and any(month-file .is_file() over the request's padded
    months)`` — NEVER an unbounded ``rglob`` (an out-of-range-only partition
    is caught one gate earlier; cheaper too). File CONTENTS are deliberately
    not read here — present-but-stale is the SHIPPED post-load #1492 equality
    gate's job (honest split, BUILD-SPEC §13.2).
    """
    base_dir = lake_root / BAR_DIR_NAME / exchange / segment / symbol / interval
    probed = 0
    present = False
    if base_dir.is_dir():
        for year, month in months:
            probed += 1
            if (base_dir / f"{year:04d}" / f"{month:02d}.parquet").is_file():
                present = True
                break
    return PairScan(
        symbol=symbol,
        interval=interval,
        kind=ScanKind.PRESENCE,
        covered=frozenset(),
        present=present,
        corrupt_files=(),
        bars_found=0,
        months_probed=probed,
    )


def scan_selection_coverage(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Sequence[str],
    start: date,
    end: date,
) -> dict[tuple[str, str], PairScan]:
    """Scan every requested (symbol, interval) pair's on-disk coverage — read-only.

    The DISK-presence authority feeding
    ``brutex.engine.sweep_data_preflight.preflight_disk_coverage``. Month
    coordinates come from :func:`~brutex.data.spot_daily_resample.
    ist_span_partition_months` over the half-open ``[start, end + 1 day)``
    span (its ±1-day pad makes the SAME month set serve ``1day``, whose
    midnight-IST bars straddle UTC months — a Jan-1 IST bar living in the
    December UTC file is found). ``1minute``/``1day`` pairs are session-DATE
    scanned (:func:`_scan_dates_pair`); every other interval gets the
    span-bounded PRESENCE check (:func:`_scan_presence_pair`).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange, segment
        The launch's lake coordinates (e.g. ``NSE`` / ``INDEX``).
    symbols, intervals
        The requested selection axes; every cross-product pair is scanned.
    start, end
        The CLAMPED inclusive date range (``clamp_dates`` output).

    Returns
    -------
    dict[tuple[str, str], PairScan]
        One entry per requested ``(symbol, interval)`` pair, in request order
        (symbols-major).

    Raises
    ------
    brutex.data.partition.PartitionError
        On an invalid lake coordinate (bad exchange/segment/interval/symbol) —
        a caller error, validated upstream by the selection parser.

    Notes
    -----
    **Cost (honest, Golden Rule #10):** O(symbols × intervals ×
    window-months) file probes + one timestamp-column read per existing DATES
    month — NOT O(1), but independent of UNRELATED lake depth (bench-pinned).
    Never writes anything.
    """
    months = ist_span_partition_months(start, end + timedelta(days=1))
    skip_errors = parquet_read_skip_errors()
    scans: dict[tuple[str, str], PairScan] = {}
    for symbol in symbols:
        for interval in intervals:
            if interval in DATE_CHECKED_INTERVALS:
                scans[(symbol, interval)] = _scan_dates_pair(
                    lake_root=lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    months=months,
                    start=start,
                    end=end,
                    skip_errors=skip_errors,
                )
            else:
                scans[(symbol, interval)] = _scan_presence_pair(
                    lake_root=lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    months=months,
                )
    return scans


def scan_ledger_quarantine(
    lake_root: Path,
    *,
    exchange: str,
    symbols: Sequence[str],
    intervals: Sequence[str],
    start: date,
    end: date,
) -> QuarantineScan:
    """Scan the durable window ledger for demoted / accepted-gap rows — read-only.

    Exactly ONE :func:`~brutex.data.window_ledger_store.load_window_ledger`
    parse per launch (read-only, delta-folding, never raises — spy-pinned in
    the bench suite), then a pure filter over
    :meth:`~brutex.data.window_ledger.WindowLedger.records`:

    * ``status ∈ {FAILED, SKIPPED}`` — an OPEN demotion or an ACCEPTED gap;
    * ``last_error ∈ DEMOTED_SETTLE_REASONS`` — the canonical demotion tag set
      (an expired-window "vendor confirmed no trades" settle never flags);
    * ``security_id ∈ {f"{exchange}-{symbol}", symbol}`` — both forms,
      defensive (the ledger stores the engine ``symbol_name``);
    * ``interval`` requested;
    * ISO-string window overlap with ``[start, end]`` (zero-padded ISO ⇒
      string order is date order).

    The pull machinery (``plan_windows`` / ``record`` / ``seed_*``) is NEVER
    called — R3, the sweep never pulls.

    Parameters
    ----------
    lake_root
        Top-level lake directory.
    exchange
        Exchange prefix for the prefixed security-id form (e.g. ``NSE``).
    symbols, intervals
        The requested selection axes.
    start, end
        The CLAMPED inclusive date range.

    Returns
    -------
    QuarantineScan
        The overlapping demotion flags plus the ledger's ``load_failed``
        verdict surfaced VERBATIM (fail-CLOSED upstream — an unreadable
        ledger refuses, never approves).
    """
    ledger = load_window_ledger(lake_root)
    wanted_ids = set(symbols) | {f"{exchange}-{symbol}" for symbol in symbols}
    wanted_intervals = frozenset(intervals)
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    flags: list[LedgerRowFlag] = []
    for record in ledger.records():
        if record.status not in _QUARANTINE_STATUSES:
            continue
        last_error = record.last_error
        if last_error is None or last_error not in DEMOTED_SETTLE_REASONS:
            continue
        if record.security_id not in wanted_ids:
            continue
        if record.interval not in wanted_intervals:
            continue
        if record.window_start > end_iso or record.window_end < start_iso:
            continue
        flags.append(
            LedgerRowFlag(
                security_id=record.security_id,
                interval=record.interval,
                window_start=record.window_start,
                window_end=record.window_end,
                status=record.status,
                last_error=last_error,
            )
        )
    return QuarantineScan(flags=tuple(flags), load_failed=ledger.load_failed)
