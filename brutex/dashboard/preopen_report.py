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
#   _bar_ist_date, _validate_candle_coords, the interval constants) — CLAUDE.md
#   §11 COMMON (one source of truth, no copy-paste).

from __future__ import annotations

import bisect
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from pathlib import Path
from typing import Any, Final, Literal

from brutex.dashboard.db_view import (
    _DAILY_INTERVAL,
    _INTRADAY_BASE_INTERVAL,
    _bar_ist_date,
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

#: Cell-style indices into the ``xl/styles.xml`` ``<cellXfs>`` table
#: (see :data:`_STYLES`). 0 is the implicit default, so it is omitted from
#: the cell markup.
_STYLE_NORMAL: Final[int] = 0  # default font, General alignment
_STYLE_BOLD: Final[int] = 1  # bold font — the header row + the SYMBOL column
_STYLE_TITLE: Final[int] = 2  # bold + centred — the merged date-range title row
_STYLE_GREEN: Final[int] = 3  # solid green fill — a gainer (%CHNG > 0) plain cell
_STYLE_RED: Final[int] = 4  # solid red fill — a loser (%CHNG < 0) plain cell
_STYLE_GREEN_BOLD: Final[int] = 5  # green fill + bold — a gainer row's SYMBOL cell
_STYLE_RED_BOLD: Final[int] = 6  # red fill + bold — a loser row's SYMBOL cell

#: Per-column display widths (Excel "character" units), aligned 1:1 to
#: :data:`REPORT_COLUMNS`, so the sheet opens with precise, readable columns
#: instead of the cramped OOXML default width (operator: "make the spaces
#: precise"). DEC-PREOPEN-XLSX-STYLE-001.
_COL_WIDTHS: Final[tuple[float, ...]] = (12.0, 16.0, 13.0, 12.0, 11.0, 11.0)


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


def _prev_close_asof(
    daily: dict[date, Decimal], daily_dates: list[date], day: date
) -> Decimal | None:
    """The NSE "previous close" for *day*: the latest 1-day close STRICTLY before it.

    Unlike a same-date lookup, this anchors a day that has a 09:15 IEP but **no
    1-day bar of its own yet** — the CURRENT day during/after the session, whose
    daily bar Dhan only finalises after close (DEC-PREOPEN-CURRENTDAY-001) — to the
    prior trading day's official close. For a day that DOES have its own 1-day bar
    the result is identical to that day's predecessor close, so every historical
    row is unchanged. ``None`` when no earlier 1-day bar exists (the first day).
    O(log N) via :func:`bisect.bisect_left` over the pre-sorted *daily_dates*.

    On an INTERNAL 1-day gap (a missing prior daily bar) this returns the most
    recent AVAILABLE earlier close — a best-effort anchor rather than dropping the
    row; the 1-day series is kept complete by DEC-DHAN-429-RETRY-001 (#466) +
    the windows table (#465).
    """
    idx = bisect.bisect_left(daily_dates, day)
    if idx == 0:
        return None
    return daily[daily_dates[idx - 1]]


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
    symbol every day's 09:15 IEP is anchored to the **latest 1-day close strictly
    before that day** (:func:`_prev_close_asof`), so the CURRENT day — which has an
    IEP but whose own daily bar is not finalised yet — is emitted using the prior
    trading day's official close (DEC-PREOPEN-CURRENTDAY-001), and every historical
    row is unchanged. A day is emitted only when its IEP exists, a (non-zero)
    previous close exists, and the day falls in range. Rows are sorted by
    ``(day, symbol)`` for a single, scannable sheet. O(log N) per emitted row.
    """
    rows: list[PreOpenRow] = []
    for exchange, segment, symbol in datasets:
        daily = _daily_close_by_date(
            conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
        )
        daily_dates = sorted(daily)
        opens = _session_open_by_date(
            conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
        )
        for day in sorted(opens):
            if day < start or day > end:
                continue
            anchor = _prev_close_asof(daily, daily_dates, day)
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


#: The two report categories the operator toggles between on ``/reports``.
ReportMode = Literal["all", "topmover"]


def select_all_symbols(rows: list[PreOpenRow]) -> list[PreOpenRow]:
    """Select EVERY row, sorted by day then %CHNG **descending** within each day.

    The "All symbols" category (DEC-REPORTS-EXCEL-CATEGORIES-001): one row per
    ``(symbol, day)`` is kept verbatim — the minimum-%-change threshold is
    IGNORED in this mode. Within a trading day the rows run gainers → losers
    (``%CHNG`` high → low); ties break by SYMBOL alphabetical so the order is
    deterministic and reproducible.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).

    Returns
    -------
    list[PreOpenRow]
        A new list sorted by ``(day, -pct_chng, symbol)``. The input is not
        mutated.
    """
    return sorted(rows, key=lambda r: (r.day, -r.pct_chng, r.symbol))


def select_top_movers(rows: list[PreOpenRow], *, min_pct: Decimal) -> list[PreOpenRow]:
    """Select the single biggest absolute mover per day (≥ *min_pct*), day-sorted.

    The "Top mover" category (DEC-REPORTS-EXCEL-CATEGORIES-001): for each trading
    day, among the rows whose ``abs(pct_chng) >= min_pct`` pick the ONE with the
    largest ``abs(pct_chng)`` — a gainer OR a loser (e.g. ``+5`` vs ``-6`` → the
    ``-6``). A day with no qualifying row is skipped entirely. Ties on
    ``abs(pct_chng)`` break by SYMBOL alphabetical so the choice is deterministic.
    The result is sorted by day.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).
    min_pct
        The inclusive minimum absolute %CHNG a row must clear to qualify. ``0``
        (or any non-positive value) admits every row, so every day still yields
        its top mover.

    Returns
    -------
    list[PreOpenRow]
        One row per qualifying day, sorted by ``day``. The input is not mutated.
    """
    qualifiers = [r for r in rows if abs(r.pct_chng) >= min_pct]
    by_day = sorted(qualifiers, key=lambda r: r.day)
    picked: list[PreOpenRow] = []
    for _day, day_rows in groupby(by_day, key=lambda r: r.day):
        winner = min(day_rows, key=lambda r: (-abs(r.pct_chng), r.symbol))
        picked.append(winner)
    return picked


def select_rows(rows: list[PreOpenRow], *, mode: ReportMode, min_pct: Decimal) -> list[PreOpenRow]:
    """Dispatch to the row-selection function for *mode*.

    A thin façade over :func:`select_all_symbols` (``mode="all"``) and
    :func:`select_top_movers` (``mode="topmover"``) so callers pass a single
    *mode* string. *min_pct* is forwarded only in the top-mover branch (the
    "All symbols" category ignores the threshold by spec).

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows.
    mode
        ``"all"`` for every row, ``"topmover"`` for the per-day biggest mover.
    min_pct
        The inclusive minimum absolute %CHNG (top-mover mode only).

    Returns
    -------
    list[PreOpenRow]
        The selected, deterministically-ordered rows.
    """
    if mode == "all":
        return select_all_symbols(rows)
    return select_top_movers(rows, min_pct=min_pct)


def _cell_xml(col: int, row: int, value: str | float, style: int = _STYLE_NORMAL) -> str:
    """Render one ``<c>`` cell — numeric for int/float, inline-string otherwise.

    *style* is a 0-based index into the ``xl/styles.xml`` ``<cellXfs>`` table
    (:data:`_STYLE_NORMAL` / :data:`_STYLE_BOLD` / :data:`_STYLE_TITLE`). The
    default (0) is omitted from the markup since ``s="0"`` is the implicit
    per-cell style.
    """
    ref = f"{_col_letter(col)}{row}"
    s_attr = f' s="{style}"' if style else ""
    if isinstance(value, bool):  # pragma: no cover — defensive; never a bool cell
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s_attr}><v>{value!r}</v></c>'
    return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>'


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


def _cols_xml(ncols: int) -> str:
    """Build the ``<cols>`` block giving each column a precise display width."""
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{_COL_WIDTHS[i]:g}" customWidth="1"/>'
        for i in range(ncols)
    )
    return f"<cols>{cols}</cols>"


def _sheet_xml(
    header: tuple[str, ...], data: Sequence[tuple[str | float, ...]], *, title: str
) -> str:
    """Build ``sheet1.xml`` — the NSE-pre-open look, dependency-free.

    Row 1 is a merged, **bold, centred TITLE** carrying the report's date range
    (matching the NSE sheet's titled look — and the one place a *range* report
    states its dates beyond the per-row ``DATE`` column); row 2 is the **bold,
    frozen header**; data starts at row 3 with a **bold SYMBOL column**. Precise
    per-column widths + an auto-filter on the header + a frozen title/header keep
    the sheet readable the instant it opens. Element order follows the OOXML
    ``CT_Worksheet`` sequence (``sheetViews`` → ``cols`` → ``sheetData`` →
    ``autoFilter`` → ``mergeCells``) so Excel / Numbers open it without a repair
    prompt.
    """
    ncols = len(header)
    last_col = _col_letter(ncols - 1)
    last_row = len(data) + 2  # row 1 title + row 2 header + N data rows
    symbol_col = header.index("SYMBOL")
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>',
        "</sheetView></sheetViews>",
        _cols_xml(ncols),
        "<sheetData>",
        # Row 1 — the merged, bold, centred date-range title.
        f'<row r="1">{_cell_xml(0, 1, title, _STYLE_TITLE)}</row>',
        # Row 2 — the bold header.
        '<row r="2">'
        + "".join(_cell_xml(c, 2, h, _STYLE_BOLD) for c, h in enumerate(header))
        + "</row>",
    ]
    for r, record in enumerate(data, start=3):
        cells = "".join(
            _cell_xml(c, r, v, _STYLE_BOLD if c == symbol_col else _STYLE_NORMAL)
            for c, v in enumerate(record)
        )
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A2:{last_col}{last_row}"/>')
    out.append(f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>')
    out.append("</worksheet>")
    return "".join(out)


_CONTENT_TYPES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
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
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)

#: The workbook stylesheet: a default font (xf 0), a **bold** font (xf 1, the
#: header + SYMBOL column) and a **bold + horizontally-centred** font (xf 2, the
#: merged title row). The two mandatory default fills (``none`` + ``gray125``)
#: and the empty border keep strict OOXML consumers (Excel) happy.
#: DEC-PREOPEN-XLSX-STYLE-001.
_STYLES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="4">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    # fillId 2 — a light GREEN solid fill (gainer rows, %CHNG > 0).
    '<fill><patternFill patternType="solid"><fgColor rgb="FFC6EFCE"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    # fillId 3 — a light RED solid fill (loser rows, %CHNG < 0).
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="7">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
    '<alignment horizontal="center"/></xf>'
    # xf 3 — green fill, default font (a gainer's money/date cells).
    '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/>'
    # xf 4 — red fill, default font (a loser's money/date cells).
    '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>'
    # xf 5 — green fill + bold (a gainer row's SYMBOL cell).
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    # xf 6 — red fill + bold (a loser row's SYMBOL cell).
    '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _row_fill_styles(pct_chng: Decimal, symbol_col: int, col: int) -> int:
    """Pick a cell style for column *col* of a row with the given ``%CHNG``.

    Maps the row's ``%CHNG`` sign to the green/red fill family and keeps the
    SYMBOL column bold (DEC-REPORTS-EXCEL-CATEGORIES-001 — every gainer row is
    green, every loser row red, an exact-zero row stays neutral):

    * ``pct_chng > 0`` → :data:`_STYLE_GREEN_BOLD` for the SYMBOL column,
      :data:`_STYLE_GREEN` for every other column;
    * ``pct_chng < 0`` → :data:`_STYLE_RED_BOLD` / :data:`_STYLE_RED`;
    * ``pct_chng == 0`` → :data:`_STYLE_BOLD` for SYMBOL, :data:`_STYLE_NORMAL`
      otherwise (the unfilled neutral look, identical to the single-sheet export).

    Parameters
    ----------
    pct_chng
        The row's full-precision ``%CHNG`` (its sign drives the fill).
    symbol_col
        The 0-based index of the bold SYMBOL column.
    col
        The 0-based index of the column being styled.

    Returns
    -------
    int
        The 0-based ``cellXfs`` index for the cell (see :data:`_STYLES`).
    """
    is_symbol = col == symbol_col
    if pct_chng > 0:
        return _STYLE_GREEN_BOLD if is_symbol else _STYLE_GREEN
    if pct_chng < 0:
        return _STYLE_RED_BOLD if is_symbol else _STYLE_RED
    return _STYLE_BOLD if is_symbol else _STYLE_NORMAL


def _filled_sheet_xml(rows: list[PreOpenRow], *, title: str) -> str:
    """Build one worksheet's XML with per-row green/red fills by ``%CHNG`` sign.

    The multi-sheet sibling of :func:`_sheet_xml` (DEC-REPORTS-EXCEL-CATEGORIES-001):
    same NSE look — a merged bold-centred TITLE (row 1), a bold frozen header
    (row 2), a bold SYMBOL column — but every data row is shaded by its
    ``%CHNG`` sign (green gainer / red loser / neutral zero) via
    :func:`_row_fill_styles`. Element order follows the OOXML ``CT_Worksheet``
    sequence so Excel / Numbers open it without a repair prompt.

    Parameters
    ----------
    rows
        The :class:`PreOpenRow` rows for this sheet, already in display order.
    title
        The row-1 banner text (e.g. the month label).

    Returns
    -------
    str
        The complete ``<worksheet>`` XML document.
    """
    header = REPORT_COLUMNS
    ncols = len(header)
    last_col = _col_letter(ncols - 1)
    last_row = len(rows) + 2  # row 1 title + row 2 header + N data rows
    symbol_col = header.index("SYMBOL")
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>',
        "</sheetView></sheetViews>",
        _cols_xml(ncols),
        "<sheetData>",
        f'<row r="1">{_cell_xml(0, 1, title, _STYLE_TITLE)}</row>',
        '<row r="2">'
        + "".join(_cell_xml(c, 2, h, _STYLE_BOLD) for c, h in enumerate(header))
        + "</row>",
    ]
    for r, record in enumerate(rows, start=3):
        # Widen the fixed heterogeneous display tuple to ``tuple[str | float, ...]``
        # so iterating it yields ``str | float`` (the type ``_cell_xml`` wants) —
        # iterating the FIXED tuple directly makes mypy join the element types to
        # ``object`` (matches how the single-sheet ``_sheet_xml`` types its data).
        row_cells: tuple[str | float, ...] = record.display_cells()
        cells = "".join(
            _cell_xml(c, r, v, _row_fill_styles(record.pct_chng, symbol_col, c))
            for c, v in enumerate(row_cells)
        )
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A2:{last_col}{last_row}"/>')
    out.append(f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>')
    out.append("</worksheet>")
    return "".join(out)


def _month_label(day: date) -> str:
    """The ``YYYY-MM`` sheet label for *day* (e.g. ``date(2024, 3, 4)`` → ``2024-03``)."""
    return f"{day.year:04d}-{day.month:02d}"


def _group_by_month(rows: list[PreOpenRow]) -> list[tuple[str, list[PreOpenRow]]]:
    """Group *already day-sorted* rows into ``(YYYY-MM, rows)`` pairs, month-ordered.

    Only months that actually have rows produce a pair, so empty months yield no
    sheet (DEC-REPORTS-EXCEL-CATEGORIES-001). The within-month order of *rows* is
    preserved verbatim (the caller's selection order — gainers→losers for "all",
    day-ascending for "top mover").

    Parameters
    ----------
    rows
        Rows pre-sorted so each month's rows are contiguous and ascending (the
        output of :func:`select_all_symbols` / :func:`select_top_movers`).

    Returns
    -------
    list[tuple[str, list[PreOpenRow]]]
        One ``(label, rows)`` pair per non-empty month, in month order.
    """
    return [
        (label, list(group)) for label, group in groupby(rows, key=lambda r: _month_label(r.day))
    ]


def _dynamic_content_types(nsheets: int) -> str:
    """Build ``[Content_Types].xml`` declaring *nsheets* worksheet overrides."""
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, nsheets + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{overrides}"
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _dynamic_workbook(sheet_names: list[str]) -> str:
    """Build ``xl/workbook.xml`` listing one ``<sheet>`` per label in *sheet_names*."""
    sheets = "".join(
        f'<sheet name="{_xml_escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _dynamic_workbook_rels(nsheets: int) -> str:
    """Build ``xl/_rels/workbook.xml.rels`` wiring *nsheets* sheets + the styles part."""
    sheet_rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, nsheets + 1)
    )
    styles_id = nsheets + 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{styles_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def render_month_workbook(months: list[tuple[str, list[PreOpenRow]]]) -> bytes:
    """Render one ``.xlsx`` with **one worksheet per month** in *months*.

    The per-year workbook of the two-category report
    (DEC-REPORTS-EXCEL-CATEGORIES-001): each ``(label, rows)`` pair becomes a
    worksheet named after *label* (e.g. ``2024-01``), carrying the
    ``DATE · SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` columns with per-row
    green/red fills by ``%CHNG`` sign (:func:`_filled_sheet_xml`). The bytes are
    deterministic (fixed :data:`_ZIP_EPOCH` member timestamps) so the same input
    always produces a byte-identical workbook (CLAUDE.md §2 Rule 7).

    An empty *months* yields a valid single header-only worksheet named
    ``Pre-Open`` (a workbook must have at least one sheet), so a year with no
    rows still opens cleanly rather than producing a corrupt file.

    Parameters
    ----------
    months
        ``(sheet_label, rows)`` pairs, one per non-empty month, in sheet order.

    Returns
    -------
    bytes
        The deterministic ``.xlsx`` byte string.
    """
    pairs = months or [("Pre-Open", [])]
    names = [label for label, _ in pairs]
    parts = {
        "[Content_Types].xml": _dynamic_content_types(len(pairs)),
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _dynamic_workbook(names),
        "xl/_rels/workbook.xml.rels": _dynamic_workbook_rels(len(pairs)),
        "xl/styles.xml": _STYLES,
    }
    for i, (label, rows) in enumerate(pairs, start=1):
        parts[f"xl/worksheets/sheet{i}.xml"] = _filled_sheet_xml(rows, title=label)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


def render_report_zip(rows: list[PreOpenRow], *, mode: ReportMode, min_pct: Decimal) -> bytes:
    """Render the two-category report to a ``.zip`` of one ``.xlsx`` per year.

    The top-level builder of DEC-REPORTS-EXCEL-CATEGORIES-001:

    1. select rows for *mode* (:func:`select_rows` — "all" keeps every row
       gainers→losers per day, "topmover" keeps the single biggest absolute
       mover per day clearing *min_pct*);
    2. group the selected rows by **year**;
    3. for each year build a workbook whose worksheets are that year's
       **months** (:func:`render_month_workbook`), with per-row green/red fills;
    4. write every yearly workbook into one deterministic ``.zip``
       (stdlib :mod:`zipfile`, fixed :data:`_ZIP_EPOCH`), the member name
       carrying the mode + year (e.g. ``preopen_all_2024.xlsx`` /
       ``preopen_topmover_2024.xlsx``).

    An empty selection yields a valid ``.zip`` holding a single header-only
    workbook (``preopen_<mode>_empty.xlsx``) so the download never fails.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).
    mode
        ``"all"`` (every row) or ``"topmover"`` (per-day biggest mover).
    min_pct
        The inclusive minimum absolute ``%CHNG`` for the top-mover mode
        (ignored when *mode* is ``"all"``).

    Returns
    -------
    bytes
        The deterministic ``.zip`` byte string.
    """
    selected = select_rows(rows, mode=mode, min_pct=min_pct)
    by_year: dict[int, list[PreOpenRow]] = {}
    for row in selected:
        by_year.setdefault(row.day.year, []).append(row)
    members: dict[str, bytes] = {}
    if not by_year:
        members[f"preopen_{mode}_empty.xlsx"] = render_month_workbook([])
    else:
        for year in sorted(by_year):
            workbook = render_month_workbook(_group_by_month(by_year[year]))
            members[f"preopen_{mode}_{year:04d}.xlsx"] = workbook
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


def _default_title(rows: list[PreOpenRow]) -> str:
    """Derive the sheet title from *rows* when the caller passes none.

    Rows are sorted by ``(day, symbol)``, so the first/last row bound the date
    range. An empty report falls back to the bare ``"Pre-Open Market"`` label.
    """
    if not rows:
        return "Pre-Open Market"
    return f"Pre-Open Market: {rows[0].day.isoformat()} to {rows[-1].day.isoformat()}"


def render_xlsx(rows: list[PreOpenRow], *, title: str | None = None) -> bytes:
    """Render *rows* to a deterministic, dependency-free ``.xlsx`` byte string.

    A minimal valid OOXML workbook (one ``Pre-Open`` sheet: a bold, centred
    date-range title row, a bold + frozen header, a bold SYMBOL column and
    precise per-column widths) written with the stdlib :mod:`zipfile` + a FIXED
    member timestamp, so the same rows always produce byte-identical output
    (reproducible, CLAUDE.md §2 Rule 7). An empty *rows* yields a valid
    title + header-only workbook.

    *title* sets the row-1 banner (the report's date range — how a multi-day
    *range* report states its dates, complementing the per-row ``DATE``
    column); when omitted it is derived from the rows' date span via
    :func:`_default_title`.
    """
    data = [r.display_cells() for r in rows]
    sheet_title = title if title is not None else _default_title(rows)
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK,
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/styles.xml": _STYLES,
        "xl/worksheets/sheet1.xml": _sheet_xml(REPORT_COLUMNS, data, title=sheet_title),
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
    "ReportMode",
    "build_preopen_rows",
    "render_month_workbook",
    "render_report_zip",
    "render_xlsx",
    "resolve_report_datasets",
    "select_all_symbols",
    "select_rows",
    "select_top_movers",
]
