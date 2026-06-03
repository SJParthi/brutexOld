"""Tests for `brutex.dashboard.preopen_report` — the NSE-style pre-open .xlsx export.

100% line+branch: the lake readers (1-day close + 09:15 IEP), the dataset
resolver (both-intervals gate + symbol filter), the row builder (prev-close
anchoring, range + first-day + zero-prev skips), and the dependency-free OOXML
writer (valid zip, deterministic bytes, numeric vs inline-string cells, the
column-letter helper). Verified against the operator's COALINDIA pre-open CSV
(IEP 429.00, PREV. CLOSE 458.15, %CHNG −6.36).
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
from brutex.dashboard.preopen_report import (
    _COL_WIDTHS,
    REPORT_COLUMNS,
    PreOpenRow,
    _col_letter,
    _daily_close_by_date,
    _session_open_by_date,
    build_preopen_rows,
    render_xlsx,
    resolve_report_datasets,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def _bar(ts: datetime, *, open_: str, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(close),
        low=Decimal(open_),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _write(lake: Path, symbol: str, interval: str, bars: list[CandleBar]) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="CASH", symbol=symbol, interval=interval, bars=bars
    )


def _coalindia(lake: Path) -> None:
    """Mirror the operator's CSV: 26-May close 458.15; 27-May 09:15 open 429.00."""
    _write(
        lake,
        "COALINDIA",
        "1day",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429", close="431.55"),
        ],
    )
    _write(
        lake,
        "COALINDIA",
        "1minute",
        [
            # 2024-03-04: first bar (09:15 IST) open = the IEP, plus a later bar.
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429.00", close="431.55"),
            _bar(datetime(2024, 3, 4, 3, 46, tzinfo=UTC), open_="431.55", close="432.0"),
        ],
    )


# ── lake readers ──────────────────────────────────────────────────────────


def test_daily_close_missing_partition_is_empty(tmp_path: Path) -> None:
    assert (
        _daily_close_by_date(_conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA")
        == {}
    )


def test_session_open_missing_partition_is_empty(tmp_path: Path) -> None:
    assert (
        _session_open_by_date(_conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA")
        == {}
    )


def test_daily_close_keyed_by_ist_date(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    out = _daily_close_by_date(
        _conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA"
    )
    assert out[date(2024, 3, 1)] == Decimal("458.15")
    assert out[date(2024, 3, 4)] == Decimal("431.55")


def test_session_open_picks_first_bar_of_day(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    out = _session_open_by_date(
        _conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA"
    )
    assert out == {date(2024, 3, 4): Decimal("429.00")}  # the 09:15 open, not the 09:16 bar


# ── resolve_report_datasets ────────────────────────────────────────────────


def test_resolve_requires_both_intervals(tmp_path: Path) -> None:
    # COALINDIA has both 1day + 1minute → eligible; ONLYDAY has only 1day → excluded.
    _coalindia(tmp_path)
    _write(
        tmp_path,
        "ONLYDAY",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="1", close="2")],
    )
    assert resolve_report_datasets(tmp_path) == [("NSE", "CASH", "COALINDIA")]


def test_resolve_filters_to_requested_symbols(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    assert resolve_report_datasets(tmp_path, ["RELIANCE"]) == []
    assert resolve_report_datasets(tmp_path, ["COALINDIA"]) == [("NSE", "CASH", "COALINDIA")]


def test_resolve_empty_lake(tmp_path: Path) -> None:
    assert resolve_report_datasets(tmp_path) == []


# ── build_preopen_rows ──────────────────────────────────────────────────────


def test_build_matches_nse_numbers(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "COALINDIA")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(rows) == 1  # only 2024-03-04 has BOTH a prev close and an IEP
    r = rows[0]
    assert r.day == date(2024, 3, 4)
    assert r.prev_close == Decimal("458.15")
    assert r.iep == Decimal("429.00")
    assert r.chng == Decimal("-29.15")
    # %CHNG = -29.15 / 458.15 * 100 → -6.36 at 2dp (matches the NSE CSV).
    assert r.display_cells() == ("2024-03-04", "COALINDIA", 458.15, 429.0, -29.15, -6.36)


def test_build_skips_out_of_range(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "COALINDIA")],
        start=date(2025, 1, 1),  # after the only eligible day
        end=date(2025, 12, 31),
    )
    assert rows == []


def test_build_skips_first_day_no_prev_close(tmp_path: Path) -> None:
    # 1minute on the FIRST 1day date → no previous close → skipped.
    _write(
        tmp_path,
        "FOO",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="100", close="100")],
    )
    _write(
        tmp_path,
        "FOO",
        "1minute",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="99", close="100")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "FOO")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert rows == []


def test_build_skips_zero_prev_close(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ZED",
        "1day",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="0", close="0"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="10", close="10"),
        ],
    )
    _write(
        tmp_path,
        "ZED",
        "1minute",
        [_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="10", close="10")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "ZED")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert rows == []  # prev close 0 → divide-by-zero guard skips the row


# ── render_xlsx + helpers ──────────────────────────────────────────────────


def test_col_letter() -> None:
    assert _col_letter(0) == "A"
    assert _col_letter(25) == "Z"
    assert _col_letter(26) == "AA"


def _open_sheet(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert set(zf.namelist()) >= {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        }
        return zf.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_render_empty_is_header_only_valid_xlsx() -> None:
    sheet = _open_sheet(render_xlsx([]))
    for col in REPORT_COLUMNS:
        assert col in sheet
    assert "COALINDIA" not in sheet


def test_render_contains_rows_and_is_deterministic() -> None:
    row = PreOpenRow(
        day=date(2024, 3, 4),
        symbol="COALINDIA",
        prev_close=Decimal("458.15"),
        iep=Decimal("429.00"),
        chng=Decimal("-29.15"),
        pct_chng=Decimal("-6.363"),
    )
    blob = render_xlsx([row])
    sheet = _open_sheet(blob)
    assert "COALINDIA" in sheet
    assert "458.15" in sheet  # numeric cell
    assert "-29.15" in sheet
    assert "-6.36" in sheet  # %CHNG snapped to 2dp
    # Frozen header + auto-filter present.
    assert "frozen" in sheet
    assert "autoFilter" in sheet
    # Deterministic: same rows → byte-identical workbook.
    assert render_xlsx([row]) == blob


def test_render_escapes_inline_strings() -> None:
    row = PreOpenRow(
        day=date(2024, 3, 4),
        symbol="A&B<C",
        prev_close=Decimal("1"),
        iep=Decimal("1"),
        chng=Decimal("0"),
        pct_chng=Decimal("0"),
    )
    sheet = _open_sheet(render_xlsx([row]))
    assert "A&amp;B&lt;C" in sheet  # XML-escaped, never raw


# ── DEC-PREOPEN-XLSX-STYLE-001 — bold header/symbol, date-range title, widths ─


def _open_part(blob: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read(name).decode("utf-8")


def test_col_widths_aligned_to_columns() -> None:
    # Exactly one precise width per report column — no cramped default, no stray.
    assert len(_COL_WIDTHS) == len(REPORT_COLUMNS)


def test_render_wires_a_bold_styles_part() -> None:
    blob = render_xlsx([], title="Pre-Open Market: 2024-01-01 to 2024-12-31")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert "xl/styles.xml" in zf.namelist()
    styles = _open_part(blob, "xl/styles.xml")
    assert "<b/>" in styles  # a bold font is defined
    assert '<alignment horizontal="center"/>' in styles  # the centred title style


def test_render_title_row_is_merged_and_carries_the_range() -> None:
    sheet = _open_part(
        render_xlsx([], title="Pre-Open Market: 2024-01-01 to 2024-12-31"),
        "xl/worksheets/sheet1.xml",
    )
    assert "Pre-Open Market: 2024-01-01 to 2024-12-31" in sheet
    assert '<mergeCell ref="A1:F1"/>' in sheet  # merged across all 6 columns
    assert 's="2"' in sheet  # the bold-centred title style
    assert "<cols>" in sheet  # precise per-column widths
    assert 'ySplit="2"' in sheet  # title + header both frozen
    assert "autoFilter" in sheet


def test_render_header_and_symbol_are_bold_but_numbers_are_not() -> None:
    row = PreOpenRow(
        day=date(2024, 3, 4),
        symbol="COALINDIA",
        prev_close=Decimal("458.15"),
        iep=Decimal("429.00"),
        chng=Decimal("-29.15"),
        pct_chng=Decimal("-6.36"),
    )
    sheet = _open_part(render_xlsx([row]), "xl/worksheets/sheet1.xml")
    # Header is row 2, every header cell bold (style 1).
    assert '<c r="B2" s="1" t="inlineStr"><is><t>SYMBOL</t></is></c>' in sheet
    # Data is row 3: the SYMBOL cell is bold; the money cells carry no style.
    assert '<c r="B3" s="1" t="inlineStr"><is><t>COALINDIA</t></is></c>' in sheet
    assert '<c r="C3"><v>458.15</v></c>' in sheet  # PREV. CLOSE — plain numeric


def test_render_default_title_is_date_range() -> None:
    rows = [
        PreOpenRow(
            day=date(2024, 3, 4),
            symbol="AAA",
            prev_close=Decimal("1"),
            iep=Decimal("1"),
            chng=Decimal("0"),
            pct_chng=Decimal("0"),
        ),
        PreOpenRow(
            day=date(2024, 3, 6),
            symbol="ZZZ",
            prev_close=Decimal("1"),
            iep=Decimal("1"),
            chng=Decimal("0"),
            pct_chng=Decimal("0"),
        ),
    ]
    sheet = _open_part(render_xlsx(rows), "xl/worksheets/sheet1.xml")
    assert "Pre-Open Market: 2024-03-04 to 2024-03-06" in sheet


def test_render_empty_default_title_is_bare_label() -> None:
    sheet = _open_part(render_xlsx([]), "xl/worksheets/sheet1.xml")
    assert "Pre-Open Market" in sheet
    assert " to " not in sheet  # no date range when there are no rows


def test_build_and_render_ampersand_symbol(tmp_path: Path) -> None:
    # M&M (Mahindra & Mahindra) — a real ``&`` symbol that used to 500 the
    # report (``illegal candle symbol 'M&M'``). Full path: write → resolve →
    # build → render, no raise; SYMBOL escaped in the bold cell.
    _write(
        tmp_path,
        "M&M",
        "1day",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="2900", close="2950.50"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960", close="2975"),
        ],
    )
    _write(
        tmp_path,
        "M&M",
        "1minute",
        [_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960.00", close="2965")],
    )
    assert resolve_report_datasets(tmp_path, ["M&M"]) == [("NSE", "CASH", "M&M")]
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "M&M")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(rows) == 1
    assert rows[0].symbol == "M&M"
    assert rows[0].prev_close == Decimal("2950.50")
    assert rows[0].iep == Decimal("2960.00")
    sheet = _open_part(render_xlsx(rows), "xl/worksheets/sheet1.xml")
    assert "M&amp;M" in sheet  # XML-escaped in the bold SYMBOL cell
