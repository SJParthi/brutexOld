"""Pre-open report — the NSE-style ``SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` export.

A **standalone, lake-derived** report (DEC-PREOPEN-REPORT-001): for each requested
spot symbol and each trading day in a ``[from, to]`` range it reconstructs the
NSE pre-open columns straight from the Dhan lake — and the numbers match NSE to
the paisa (verified against the operator's pre-open CSV: COALINDIA 2026-05-27
IEP 429.00, PREV. CLOSE 458.15, %CHNG −6.36):

* **PREV. CLOSE** — the prior trading day's **1-day official close** (= the NSE
  settlement / "previous close"). This is the ONE place BruteX uses the 1-day
  official close; the brute-force engine itself uses the prior day's last
  1-minute close (DEC-SPOT-PREVCLOSE-1MIN-001) — they are deliberately distinct.
* **IEP** — the day's 09:15 IST opening price (the pre-open auction's indicative
  equilibrium price *becomes* the session open), read as the day's FIRST 1-minute
  bar open (``arg_min(open, timestamp)`` — session-filtered bars start at 09:15).
* **CHNG** = ``IEP − PREV. CLOSE``; **%CHNG** = ``CHNG / PREV. CLOSE × 100``.

The Excel is written by a tiny, dependency-free **stdlib OOXML writer**
(:func:`render_xlsx`) so there is no new third-party dependency to vet through the
licence/lockfile gates, and the bytes are **deterministic** (fixed zip metadata)
→ bit-reproducible (CLAUDE.md §2 Rule 7). Per-row work is O(1); the per-symbol
lake reads are one columnar pass each (O(N) once), mirroring the ``/db`` viewer.
"""

# pyright: reportPrivateUsage=false
# ^ This module deliberately reuses db_view's shared lake helpers (_to_decimal,
#   _bar_ist_date, _prev_trading_close, _validate_candle_coords, the interval
#   constants) — CLAUDE.md §11 COMMON (one source of truth, no copy-paste).

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

from brutex.dashboard.db_view import (
    _DAILY_INTERVAL,
    _INTRADAY_BASE_INTERVAL,
    _bar_ist_date,
    _prev_trading_close,
    _to_decimal,
    _validate_candle_coords,
    list_candle_datasets,
)

#: The exact column order of the report (mirrors the NSE pre-open file + a DATE).
REPORT_COLUMNS: Final[tuple[str, ...]] = (
    "DATE",
    "SYMBOL",
    "PREV. CLOSE",
    "IEP",
    "CHNG",
    "%CHNG",
)

#: A fixed zip member timestamp so the workbook bytes are reproducible (no wall clock).
_ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: Two-decimal display quantum (the report is a human-facing NSE-style sheet).
_2DP: Final[Decimal] = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class PreOpenRow:
    """One ``(symbol, trading-day)`` pre-open row — full precision until display."""

    day: date
    symbol: str
    prev_close: Decimal
    iep: Decimal
    chng: Decimal
    pct_chng: Decimal

    def display_cells(self) -> tuple[str, str, float, float, float, float]:
        """Return the row as display cells (money + %CHNG snapped to 2 decimals)."""
        return (
            self.day.isoformat(),
            self.symbol,
            float(_round2(self.prev_close)),
            float(_round2(self.iep)),
            float(_round2(self.chng)),
            float(_round2(self.pct_chng)),
        )


def _round2(value: Decimal) -> Decimal:
    """Snap *value* to 2 decimals, ROUND_HALF_UP (NSE-style display rounding)."""
    return value.quantize(_2DP, rounding=ROUND_HALF_UP)


def _daily_close_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 1-day OFFICIAL close per day → ``{ist_date: close}``.

    The NSE "previous close" anchor for the pre-open report (NOT the engine's
    1-minute baseline). Reads ``<lake>/bars/<exch>/<seg>/<symbol>/1day/**`` via a
    bound ``read_parquet`` glob; keyed by the bar's IST date. ``{}`` when the
    1-day partition is absent.
    """
    _validate_candle_coords(exchange, segment, symbol, _DAILY_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _DAILY_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    cursor = conn.execute(
        'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true)',
        [glob],
    )
    return {_bar_ist_date(ts): _to_decimal(close) for ts, close in cursor.fetchall()}


def _session_open_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read each day's 09:15 IST opening price (= the IEP) → ``{ist_date: open}``.

    The day's FIRST 1-minute bar open: session-filtered intraday bars start at
    09:15 IST, so ``arg_min(open, timestamp)`` per bar-date IS the opening price
    (= the pre-open auction's indicative equilibrium price). Reads
    ``<lake>/bars/<exch>/<seg>/<symbol>/1minute/**``; ``{}`` when absent.
    """
    _validate_candle_coords(exchange, segment, symbol, _INTRADAY_BASE_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    cursor = conn.execute(
        'SELECT CAST("timestamp" AS DATE) AS d, arg_min(open, "timestamp") AS iep '
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [glob],
    )
    return {day: _to_decimal(iep) for day, iep in cursor.fetchall()}


def resolve_report_datasets(
    lake_root: Path, symbols: list[str] | None = None
) -> list[tuple[str, str, str]]:
    """Resolve the ``(exchange, segment, symbol)`` triples the report can build.

    A dataset qualifies only when it has BOTH a ``1day`` partition (the PREV.
    CLOSE source) AND a ``1minute`` partition (the IEP source) in the lake. When
    *symbols* is given, the result is filtered to that set (case-sensitive on the
    lake symbol). Sorted by symbol for a stable picker / report order. An empty
    or missing lake yields ``[]`` (the report degrades to a header-only sheet).
    """
    wanted = set(symbols) if symbols else None
    intervals_by_key: dict[tuple[str, str, str], set[str]] = {}
    for ds in list_candle_datasets(lake_root):
        key = (ds["exchange"], ds["segment"], ds["symbol"])
        intervals_by_key.setdefault(key, set()).add(ds["interval"])
    resolved = [
        key
        for key, intervals in intervals_by_key.items()
        if _DAILY_INTERVAL in intervals
        and _INTRADAY_BASE_INTERVAL in intervals
        and (wanted is None or key[2] in wanted)
    ]
    return sorted(resolved, key=lambda k: k[2])


def build_preopen_rows(
    conn: Any,
    lake_root: Path,
    *,
    datasets: list[tuple[str, str, str]],
    start: date,
    end: date,
) -> list[PreOpenRow]:
    """Build the pre-open rows for *datasets* over ``[start, end]`` (inclusive).

    Each dataset is an ``(exchange, segment, symbol)`` spot triple. For each
    symbol the prior trading day's 1-day close anchors every day's 09:15 IEP; a
    day is emitted only when BOTH its IEP and a previous close exist and the day
    falls in range. Rows are sorted by ``(day, symbol)`` for a single, scannable
    sheet. O(1) per emitted row (two dict lookups).
    """
    rows: list[PreOpenRow] = []
    for exchange, segment, symbol in datasets:
        prev_close = _prev_trading_close(
            _daily_close_by_date(conn, lake_root, exchange=exchange, segment=segment, symbol=symbol)
        )
        opens = _session_open_by_date(
            conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
        )
        for day in sorted(opens):
            if day < start or day > end:
                continue
            anchor = prev_close.get(day)
            if anchor is None or anchor == 0:
                continue
            iep = opens[day]
            chng = iep - anchor
            rows.append(
                PreOpenRow(
                    day=day,
                    symbol=symbol,
                    prev_close=anchor,
                    iep=iep,
                    chng=chng,
                    pct_chng=chng / anchor * Decimal(100),
                )
            )
    rows.sort(key=lambda r: (r.day, r.symbol))
    return rows


def _cell_xml(col: int, row: int, value: str | float) -> str:
    """Render one ``<c>`` cell — numeric for int/float, inline-string otherwise."""
    ref = f"{_col_letter(col)}{row}"
    if isinstance(value, bool):  # pragma: no cover — defensive; never a bool cell
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value!r}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>'


def _xml_escape(text: str) -> str:
    """Escape the 3 XML metacharacters for element text (``&`` first, then ``<``/``>``).

    A 3-replace stdlib escaper — deliberately NOT ``xml.sax.saxutils.escape`` (its
    ``xml.sax`` import is blacklisted by the security scanners even though it only
    escapes strings); this avoids the dependency + the false-positive finding.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col_letter(idx: int) -> str:
    """0-based column index → spreadsheet letter (0→A, 25→Z, 26→AA)."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_xml(header: tuple[str, ...], data: Sequence[tuple[str | float, ...]]) -> str:
    """Build ``sheet1.xml`` with a frozen header row + auto-filter (no styles)."""
    last_col = _col_letter(len(header) - 1)
    last_row = len(data) + 1
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>',
        "</sheetView></sheetViews>",
        "<sheetData>",
        '<row r="1">' + "".join(_cell_xml(c, 1, h) for c, h in enumerate(header)) + "</row>",
    ]
    for r, record in enumerate(data, start=2):
        out.append(
            f'<row r="{r}">' + "".join(_cell_xml(c, r, v) for c, v in enumerate(record)) + "</row>"
        )
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A1:{last_col}{last_row}"/>')
    out.append("</worksheet>")
    return "".join(out)


_CONTENT_TYPES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_ROOT_RELS: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Pre-Open" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def render_xlsx(rows: list[PreOpenRow]) -> bytes:
    """Render *rows* to a deterministic, dependency-free ``.xlsx`` byte string.

    A minimal valid OOXML workbook (one ``Pre-Open`` sheet, frozen header row,
    auto-filter) written with the stdlib :mod:`zipfile` + a FIXED member
    timestamp, so the same rows always produce byte-identical output
    (reproducible, CLAUDE.md §2 Rule 7). An empty *rows* yields a valid
    header-only workbook.
    """
    data = [r.display_cells() for r in rows]
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK,
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/worksheets/sheet1.xml": _sheet_xml(REPORT_COLUMNS, data),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


__all__ = [
    "REPORT_COLUMNS",
    "PreOpenRow",
    "build_preopen_rows",
    "render_xlsx",
    "resolve_report_datasets",
]
