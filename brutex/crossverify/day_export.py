"""One symbol's day export: read → gate → per-class decision → render.

DEC-CROSSVERIFY-PRODUCER-001 §1.4 + §4.3. :func:`export_symbol` is the ONLY
constructor of publishable ``csv_bytes`` — the FIRST of the three concentric
never-publish-partial belts (``csv_bytes is not None ⇔ disposition ==
"publish"``). The gate sees the FULL deduped day sequence (pre-session-filter,
so junk trips ``count_excess`` honestly); the RENDERER refuses any
out-of-session row (§1.1 — refusal is the tripwire, not a drop).

Read path (production binding, §1.4): the month-bounded
:func:`~brutex.data.lake_day_glob.read_day_intraday_frame` (keep-FIRST dedup)
→ the **7 canonical columns selected BY NAME** (Greeks-overlay-proof — the
movers readers drop ``open_interest``; the export keeps it) → IST-day filter
→ :class:`~brutex.data.models.CandleBar` rows via the ``_row_to_bar`` idiom
(``Decimal(str(v))`` per price — zero float-noise divergence).

Honest cost: one month-partition Parquet read per (symbol, day) — O(one
month of 1-minute bars), never O(full history).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal, Protocol

import structlog

from brutex.crossverify.csv_render import ExportContractError, ExportRow, render_day_csv
from brutex.data.models import CandleBar

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from pathlib import Path

    from brutex.calendars.nse import TradingSession
    from brutex.crossverify.canonical import CanonicalIdentity
    from brutex.crossverify.gate_seam import TodayCompletenessClassifier


class DayFrameReader(Protocol):
    """Read one (symbol, IST day)'s stored 1-minute bars.

    May raise (``PolarsError`` / ``OSError`` / a malformed-row ``ValueError``)
    — the caller maps a raise to the withheld reason ``read_error``.
    """

    def __call__(self, *, exchange: str, segment: str, symbol: str, day: date) -> list[CandleBar]:
        """Return the day's deduped bars, oldest-first (empty when none)."""
        ...


_LOG = structlog.get_logger("brutex.crossverify.day_export")

#: The 7 canonical stored columns, selected BY NAME (Greeks-overlay-proof).
CANONICAL_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)

#: §4.3 — verdict reasons that admit a publish.
_REASON_COMPLETE: Final[str] = "complete"

#: §4.3 — withheld reasons eligible for the settle-retry belt (G2), per class.
SETTLE_RETRY_REASONS_INDEX: Final[frozenset[str]] = frozenset(
    {"count_short", "last_bar_incomplete"}
)
SETTLE_RETRY_REASONS_FUT: Final[frozenset[str]] = frozenset({"oi_null_settling"})

#: §4.3 — the enumerated withhold reasons (any class ⇒ withheld, exit 2).
_KNOWN_WITHHELD_REASONS: Final[frozenset[str]] = frozenset(
    {
        "count_short",
        "count_excess",
        "last_bar_incomplete",
        "oi_null_settling",
        "volume_all_zero",
        "abstain_coverage",
    }
)


@dataclass(frozen=True, slots=True)
class SymbolExport:
    """The frozen per-symbol export decision — the never-partial record."""

    identity: CanonicalIdentity
    day: date
    disposition: Literal["publish", "withheld", "blocked", "nothing"]
    reason: str  # gate verdict.reason verbatim, or "render_refused:<msg>" / "read_error"
    csv_bytes: bytes | None  # non-None IFF disposition == "publish"
    sha256: str | None
    row_count: int


def read_day_bars(
    *, lake_root: Path, exchange: str, segment: str, symbol: str, day: date
) -> list[CandleBar]:
    """The §1.4 production read path — one (symbol, IST day) bar list.

    Month-bounded read via :func:`read_day_intraday_frame` (keep-FIRST dedup,
    warned), the 7 canonical columns by name, IST-day filter, ``CandleBar``
    conversion via ``Decimal(str(v))``. Raises on an unreadable partition /
    malformed stored row — the caller withholds ``read_error``. O(one month).
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame

    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    frame = read_day_intraday_frame(base, day, symbol=symbol)
    if frame is None:
        return []
    df = frame.select(list(CANONICAL_COLUMNS)).sort("timestamp")
    ist_date = pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata").dt.date()
    day_df = df.filter(ist_date == day)
    bars: list[CandleBar] = []
    for row in day_df.iter_rows(named=True):
        ts = row["timestamp"]
        if ts.tzinfo is None:  # the lake instant is UTC — re-tag, never shift
            ts = ts.replace(tzinfo=UTC)
        oi = row["open_interest"]
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=None if oi is None else int(oi),
            )
        )
    return bars


def make_day_frame_reader(lake_root: Path) -> DayFrameReader:
    """Bind :func:`read_day_bars` to *lake_root* — the production reader seam."""

    def _reader(*, exchange: str, segment: str, symbol: str, day: date) -> list[CandleBar]:
        return read_day_bars(
            lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol, day=day
        )

    return _reader


def _read_error_types() -> tuple[type[BaseException], ...]:
    """The reader-raise classes mapped to the withheld reason ``read_error``.

    The COMMON unreadable-file set (:func:`movers_read_skip_errors` —
    ``PolarsError`` + ``OSError``) plus the three malformed-STORED-ROW
    classes the ``CandleBar`` conversion can raise (round-1 hostile review —
    ONE corrupt row must withhold THAT symbol, never crash the whole
    ~750-symbol run): ``ValueError`` (a malformed field), ``InvalidOperation``
    (a NULL price cell — ``Decimal(str(None))``) and ``TypeError`` (a NULL
    volume/oi cell — ``int(None)``). A genuine code bug (any other class)
    still propagates — never silently swallowed.
    """
    from decimal import InvalidOperation

    from brutex.dashboard.movers_resilient import movers_read_skip_errors

    return (*movers_read_skip_errors(), ValueError, TypeError, InvalidOperation)


def _export_rows(bars: list[CandleBar]) -> list[ExportRow]:
    """Convert gate-admitted bars to renderer rows (values pass through)."""
    return [
        ExportRow(
            ts_utc=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            oi=bar.open_interest,
        )
        for bar in bars
    ]


def _decide(
    *, instrument_class: str, reason: str, incomplete: bool, covers_today: bool
) -> Literal["publish", "withheld", "blocked", "nothing"]:
    """The §4.3 per-class decision table — branch on ``reason``, never on
    ``incomplete`` alone (decision 5). Unknown FUTURE tokens: ``incomplete``
    ⇒ withheld verbatim; else nothing + one WARN (never publish on an unknown
    state). Defense-in-depth belt (round-2 hostile review S4): a publish
    additionally requires ``covers_today AND not incomplete`` — a
    contract-violating ``complete`` verdict (the gate promising completeness
    while flagging no-coverage / incompleteness) is WITHHELD + WARNed, never
    trusted (the caller names it ``gate_contract_violation``)."""
    if reason == _REASON_COMPLETE:
        if covers_today and not incomplete:
            return "publish"
        _LOG.warning(
            "crossverify.gate_contract_violation",
            instrument_class=instrument_class,
            covers_today=covers_today,
            incomplete=incomplete,
            action="withheld (a 'complete' verdict must cover today and not be incomplete)",
        )
        return "withheld"
    if reason == "no_today_bars":
        # A brute-force index missing its day is a pull FAILURE (BLOCKED);
        # an illiquid stock / not-yet-traded contract is honest nothing.
        return "blocked" if instrument_class == "index_spot" else "nothing"
    if reason == "non_trading":
        return "nothing"  # run-level at step B; defensive per-symbol arm
    if reason in _KNOWN_WITHHELD_REASONS:
        return "withheld"
    if incomplete:
        return "withheld"  # unknown FUTURE reason, verdict says withhold
    _LOG.warning(
        "crossverify.unknown_gate_reason",
        reason=reason,
        instrument_class=instrument_class,
        action="nothing (never publish on an unknown state)",
    )
    return "nothing"


def export_symbol(
    *,
    identity: CanonicalIdentity,
    day: date,
    session: TradingSession,
    reader: DayFrameReader,
    classifier: TodayCompletenessClassifier,
) -> SymbolExport:
    """Read → gate → decide → render one symbol's day — the never-partial chokepoint.

    ``csv_bytes`` is populated ONLY on an admitted (``complete``) verdict whose
    render passed every §1.1 contract law AND the row-count belts
    (``row_count == verdict.distinct_minutes``; for ``index_spot`` also
    ``== verdict.expected_minutes``). Every other path yields ``csv_bytes=None``
    with an honest reason. Never raises for a per-symbol data problem —
    read/render failures fold into the withheld record, and a
    signature-drifted gate module's classify ``TypeError`` withholds the
    symbol as ``gate_signature_drift`` (round-3 X3), never a mid-run crash.
    """

    def _no_bytes(
        disposition: Literal["withheld", "blocked", "nothing"], reason: str
    ) -> SymbolExport:
        return SymbolExport(
            identity=identity,
            day=day,
            disposition=disposition,
            reason=reason,
            csv_bytes=None,
            sha256=None,
            row_count=0,
        )

    try:
        bars = reader(
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            day=day,
        )
    except _read_error_types() as exc:
        _LOG.warning(
            "crossverify.read_error",
            symbol=repr(identity.canonical_symbol),  # vendor-derived — repr()'d (#907)
            day=day.isoformat(),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return _no_bytes("withheld", "read_error")
    try:
        verdict = classifier(
            bars_today=bars,
            clamp_today=day,
            window_end=day,
            instrument_class=identity.instrument_class,
        )
    except TypeError as exc:
        # Round-3 X3 (refuter R2-2): a signature-drifted sibling gate module
        # (a renamed/added classify kwarg the #1751 hostile review may land)
        # raises TypeError from the classify call — fail-closed PER SYMBOL:
        # withheld under a NAMED reason, never an escaping crash that kills
        # the whole ~750-symbol run mid-flight.
        _LOG.warning(
            "crossverify.gate_signature_drift",
            symbol=repr(identity.canonical_symbol),  # vendor-derived — repr()'d (#907)
            day=day.isoformat(),
            error=str(exc),
            remediation="re-align gate_seam.bind_production_classifier with the gate signature",
        )
        return _no_bytes("withheld", "gate_signature_drift")
    disposition = _decide(
        instrument_class=identity.instrument_class,
        reason=verdict.reason,
        incomplete=verdict.incomplete,
        covers_today=verdict.covers_today,
    )
    if disposition != "publish":
        # The S4 belt tripped on a lying "complete" verdict — name the
        # violation, never echo "complete" on a withheld record.
        reason = "gate_contract_violation" if verdict.reason == _REASON_COMPLETE else verdict.reason
        return _no_bytes(disposition, reason)
    rows = _export_rows(bars)
    try:
        if verdict.distinct_minutes is not None and len(rows) != verdict.distinct_minutes:
            raise ExportContractError(
                f"row-count belt: {len(rows)} rows != verdict.distinct_minutes="
                f"{verdict.distinct_minutes}"
            )
        if (
            identity.instrument_class == "index_spot"
            and verdict.expected_minutes is not None
            and len(rows) != verdict.expected_minutes
        ):
            raise ExportContractError(
                f"index row-count belt: {len(rows)} rows != verdict.expected_minutes="
                f"{verdict.expected_minutes}"
            )
        csv_bytes = render_day_csv(identity=identity, rows=rows, session=session)
    except ExportContractError as exc:
        _LOG.warning(
            "crossverify.render_refused",
            symbol=repr(identity.canonical_symbol),  # vendor-derived — repr()'d (#907)
            day=day.isoformat(),
            error=str(exc),
        )
        return _no_bytes("withheld", f"render_refused:{exc}")
    return SymbolExport(
        identity=identity,
        day=day,
        disposition="publish",
        reason=verdict.reason,
        csv_bytes=csv_bytes,
        sha256=hashlib.sha256(csv_bytes).hexdigest(),
        row_count=len(rows),
    )


__all__ = [
    "CANONICAL_COLUMNS",
    "SETTLE_RETRY_REASONS_FUT",
    "SETTLE_RETRY_REASONS_INDEX",
    "DayFrameReader",
    "SymbolExport",
    "export_symbol",
    "make_day_frame_reader",
    "read_day_bars",
]
