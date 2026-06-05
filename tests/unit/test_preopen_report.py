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
    _STYLE_GREEN,
    _STYLE_GREEN_BOLD,
    _STYLE_RED,
    _STYLE_RED_BOLD,
    REPORT_COLUMNS,
    PreOpenRow,
    _col_letter,
    _daily_close_by_date,
    _daily_open_by_date,
    _group_by_month,
    _month_label,
    _row_fill_styles,
    build_preopen_rows,
    render_month_workbook,
    render_report_zip,
    render_xlsx,
    resolve_report_datasets,
    select_all_symbols,
    select_rows,
    select_top_movers,
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


def test_daily_open_missing_partition_is_empty(tmp_path: Path) -> None:
    assert (
        _daily_open_by_date(_conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA")
        == {}
    )


def test_daily_close_keyed_by_ist_date(tmp_path: Path) -> None:
    _coalindia(tmp_path)
    out = _daily_close_by_date(
        _conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA"
    )
    assert out[date(2024, 3, 1)] == Decimal("458.15")
    assert out[date(2024, 3, 4)] == Decimal("431.55")


def test_daily_open_keyed_by_ist_date(tmp_path: Path) -> None:
    # The report's IEP source is the 1-DAY open (DEC-PREOPEN-REPORT-1DAY-001).
    _coalindia(tmp_path)
    out = _daily_open_by_date(_conn(), tmp_path, exchange="NSE", segment="CASH", symbol="COALINDIA")
    assert out[date(2024, 3, 1)] == Decimal("455")
    assert out[date(2024, 3, 4)] == Decimal("429")


# ── resolve_report_datasets ────────────────────────────────────────────────


def test_resolve_requires_1day_partition(tmp_path: Path) -> None:
    # The report is fully 1-day-sourced (DEC-PREOPEN-REPORT-1DAY-001): a 1day
    # partition qualifies (COALINDIA, ONLYDAY); a 1minute-only symbol is excluded.
    _coalindia(tmp_path)
    _write(
        tmp_path,
        "ONLYDAY",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="1", close="2")],
    )
    _write(
        tmp_path,
        "ONLYMIN",
        "1minute",
        [_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="1", close="2")],
    )
    assert resolve_report_datasets(tmp_path) == [
        ("NSE", "CASH", "COALINDIA"),
        ("NSE", "CASH", "ONLYDAY"),
    ]


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


def test_build_iep_is_1day_open_not_1minute_open(tmp_path: Path) -> None:
    # TMPV-style cross-basis trap: the 1-DAY series (demerger-adjusted, ~112) and
    # the 1-MINUTE series (raw intraday, ~191) live on DIFFERENT price scales. The
    # report must take BOTH PREV. CLOSE and IEP from the 1-DAY partition
    # (DEC-PREOPEN-REPORT-1DAY-001) — never the 1-minute open — else %CHNG explodes
    # to +69.88% instead of the true +2.84%.
    _write(
        tmp_path,
        "TMPV",
        "1day",
        [
            _bar(datetime(2021, 1, 1, 3, 45, tzinfo=UTC), open_="111.96", close="112.9"),
            _bar(datetime(2021, 1, 4, 3, 45, tzinfo=UTC), open_="116.11", close="115.81"),
        ],
    )
    _write(
        tmp_path,
        "TMPV",
        "1minute",
        # Raw intraday ~191.8 open — MUST be ignored by the (1-day-only) report.
        [_bar(datetime(2021, 1, 4, 3, 45, tzinfo=UTC), open_="191.8", close="191.9")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "TMPV")],
        start=date(2021, 1, 1),
        end=date(2021, 12, 31),
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.day == date(2021, 1, 4)
    assert r.prev_close == Decimal("112.9")  # prior 1-DAY close
    assert r.iep == Decimal("116.11")  # the 1-DAY open, NOT the 1-minute 191.8
    # %CHNG = (116.11 - 112.9) / 112.9 * 100 = 2.843… → +2.84 at 2dp (not +69.88).
    assert r.display_cells()[-1] == 2.84


def test_build_skips_day_without_its_own_1day_bar(tmp_path: Path) -> None:
    # Fully 1-day-sourced (DEC-PREOPEN-REPORT-1DAY-001): a day with only a 1-minute
    # bar (no 1-day open of its own) is NOT emitted — the intraday "today before
    # close" preview is intentionally dropped to keep one consistent basis.
    _write(
        tmp_path,
        "CUR",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="100", close="120")],
    )
    _write(
        tmp_path,
        "CUR",
        "1minute",
        [_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="125", close="126")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "CUR")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    # 2024-03-01 is the first 1day bar (no prior close) → skipped; 2024-03-04 has
    # no 1day bar of its own → not emitted. Net: no rows.
    assert rows == []


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


# ── DEC-REPORTS-EXCEL-CATEGORIES-001 — two categories + per-year/per-month zip ─


def _mk(day: date, symbol: str, pct: str) -> PreOpenRow:
    """A minimal :class:`PreOpenRow` carrying just the fields the selectors use."""
    pct_d = Decimal(pct)
    return PreOpenRow(
        day=day,
        symbol=symbol,
        prev_close=Decimal("100"),
        iep=Decimal("100") + pct_d,
        chng=pct_d,
        pct_chng=pct_d,
    )


# ── select_all_symbols ──────────────────────────────────────────────────────


def test_select_all_keeps_every_row_gainers_then_losers_per_day() -> None:
    rows = [
        _mk(date(2024, 3, 4), "BBB", "-6"),
        _mk(date(2024, 3, 4), "AAA", "5"),
        _mk(date(2024, 3, 5), "CCC", "2"),
    ]
    out = select_all_symbols(rows)
    # Day-ordered; within 2024-03-04 the +5 gainer precedes the -6 loser.
    assert [(r.day, r.symbol) for r in out] == [
        (date(2024, 3, 4), "AAA"),
        (date(2024, 3, 4), "BBB"),
        (date(2024, 3, 5), "CCC"),
    ]


def test_select_all_breaks_pct_ties_by_symbol() -> None:
    rows = [_mk(date(2024, 3, 4), "ZED", "5"), _mk(date(2024, 3, 4), "ACE", "5")]
    out = select_all_symbols(rows)
    assert [r.symbol for r in out] == ["ACE", "ZED"]  # equal %CHNG → alphabetical


def test_select_all_does_not_mutate_input() -> None:
    rows = [_mk(date(2024, 3, 5), "B", "1"), _mk(date(2024, 3, 4), "A", "2")]
    select_all_symbols(rows)
    assert [r.symbol for r in rows] == ["B", "A"]  # original order untouched


# ── select_top_movers ───────────────────────────────────────────────────────


def test_top_mover_picks_largest_absolute_gainer_or_loser() -> None:
    # +5 vs -6 on the same day → the -6 (the bigger absolute mover).
    rows = [_mk(date(2024, 3, 4), "AAA", "5"), _mk(date(2024, 3, 4), "BBB", "-6")]
    out = select_top_movers(rows, min_pct=Decimal("0"))
    assert len(out) == 1
    assert out[0].symbol == "BBB"
    assert out[0].pct_chng == Decimal("-6")


def test_top_mover_filters_by_min_pct_and_skips_empty_days() -> None:
    rows = [
        _mk(date(2024, 3, 4), "AAA", "1.5"),  # below 2 → does not qualify
        _mk(date(2024, 3, 5), "BBB", "3"),  # qualifies
    ]
    out = select_top_movers(rows, min_pct=Decimal("2"))
    # 2024-03-04 has no qualifier → skipped; only 2024-03-05 survives.
    assert [(r.day, r.symbol) for r in out] == [(date(2024, 3, 5), "BBB")]


def test_top_mover_min_pct_is_inclusive() -> None:
    rows = [_mk(date(2024, 3, 4), "AAA", "2")]  # exactly at the threshold
    out = select_top_movers(rows, min_pct=Decimal("2"))
    assert [r.symbol for r in out] == ["AAA"]  # |%CHNG| >= min is inclusive


def test_top_mover_ties_break_by_symbol() -> None:
    # Same |%CHNG| (one +5, one -5) → deterministic alphabetical pick.
    rows = [_mk(date(2024, 3, 4), "ZED", "5"), _mk(date(2024, 3, 4), "ACE", "-5")]
    out = select_top_movers(rows, min_pct=Decimal("0"))
    assert [r.symbol for r in out] == ["ACE"]


def test_top_mover_zero_min_yields_one_per_day() -> None:
    rows = [
        _mk(date(2024, 3, 4), "AAA", "0.1"),
        _mk(date(2024, 3, 5), "BBB", "0.2"),
    ]
    out = select_top_movers(rows, min_pct=Decimal("0"))
    assert [r.day for r in out] == [date(2024, 3, 4), date(2024, 3, 5)]


def test_select_rows_dispatches_on_mode() -> None:
    rows = [_mk(date(2024, 3, 4), "AAA", "5"), _mk(date(2024, 3, 4), "BBB", "-6")]
    assert len(select_rows(rows, mode="all", min_pct=Decimal("0"))) == 2
    top = select_rows(rows, mode="topmover", min_pct=Decimal("0"))
    assert [r.symbol for r in top] == ["BBB"]


# ── fill-style mapping ───────────────────────────────────────────────────────


def test_row_fill_styles_green_red_neutral() -> None:
    sym = REPORT_COLUMNS.index("SYMBOL")
    other = 0  # the DATE column
    # Gainer → green family (bold on SYMBOL, plain green elsewhere).
    assert _row_fill_styles(Decimal("5"), sym, sym) == _STYLE_GREEN_BOLD
    assert _row_fill_styles(Decimal("5"), sym, other) == _STYLE_GREEN
    # Loser → red family.
    assert _row_fill_styles(Decimal("-5"), sym, sym) == _STYLE_RED_BOLD
    assert _row_fill_styles(Decimal("-5"), sym, other) == _STYLE_RED
    # Exact zero → neutral (no fill): bold SYMBOL (1), plain otherwise (0).
    assert _row_fill_styles(Decimal("0"), sym, sym) == 1
    assert _row_fill_styles(Decimal("0"), sym, other) == 0


# ── month grouping ───────────────────────────────────────────────────────────


def test_month_label_and_grouping() -> None:
    assert _month_label(date(2024, 3, 4)) == "2024-03"
    rows = select_all_symbols(
        [
            _mk(date(2024, 1, 5), "A", "1"),
            _mk(date(2024, 1, 9), "B", "2"),
            _mk(date(2024, 3, 4), "C", "3"),
        ]
    )
    grouped = _group_by_month(rows)
    assert [label for label, _ in grouped] == ["2024-01", "2024-03"]
    assert [len(g) for _, g in grouped] == [2, 1]  # Feb has no rows → no sheet


# ── render_month_workbook ────────────────────────────────────────────────────


def _names(blob: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return set(zf.namelist())


def test_month_workbook_one_sheet_per_month_with_fills() -> None:
    jan = [_mk(date(2024, 1, 5), "GAIN", "5"), _mk(date(2024, 1, 9), "LOSE", "-3")]
    feb = [_mk(date(2024, 2, 2), "FLAT", "0")]
    blob = render_month_workbook([("2024-01", jan), ("2024-02", feb)])
    names = _names(blob)
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/worksheets/sheet2.xml" in names
    wb = _open_part(blob, "xl/workbook.xml")
    assert '<sheet name="2024-01"' in wb
    assert '<sheet name="2024-02"' in wb
    s1 = _open_part(blob, "xl/worksheets/sheet1.xml")
    # The gainer's DATE cell carries the green xf (3); the loser's the red xf (4).
    assert f'<c r="A3" s="{_STYLE_GREEN}"' in s1
    assert f'<c r="A4" s="{_STYLE_RED}"' in s1
    # The neutral February row carries no fill (default style, no s= on DATE).
    s2 = _open_part(blob, "xl/worksheets/sheet2.xml")
    assert '<c r="A3" t="inlineStr">' in s2  # neutral DATE cell, unstyled


def test_month_workbook_empty_is_header_only_single_sheet() -> None:
    blob = render_month_workbook([])
    names = _names(blob)
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/worksheets/sheet2.xml" not in names
    wb = _open_part(blob, "xl/workbook.xml")
    assert '<sheet name="Pre-Open"' in wb
    sheet = _open_part(blob, "xl/worksheets/sheet1.xml")
    for col in REPORT_COLUMNS:
        assert col in sheet


def test_month_workbook_is_deterministic() -> None:
    months = [("2024-01", [_mk(date(2024, 1, 5), "A", "5")])]
    assert render_month_workbook(months) == render_month_workbook(months)


# ── render_report_zip ────────────────────────────────────────────────────────


def _xlsx_members(blob: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return sorted(zf.namelist())


def _read_member(blob: bytes, member: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read(member)


def test_zip_all_mode_one_xlsx_per_year() -> None:
    rows = [
        _mk(date(2024, 3, 4), "A", "5"),
        _mk(date(2025, 1, 6), "B", "-2"),
    ]
    blob = render_report_zip(rows, mode="all", min_pct=Decimal("0"))
    assert _xlsx_members(blob) == ["preopen_all_2024.xlsx", "preopen_all_2025.xlsx"]
    # The 2024 workbook holds the March sheet.
    inner = _read_member(blob, "preopen_all_2024.xlsx")
    assert '<sheet name="2024-03"' in _open_part(inner, "xl/workbook.xml")


def test_zip_topmover_mode_filename_and_filter() -> None:
    rows = [
        _mk(date(2024, 3, 4), "AAA", "5"),
        _mk(date(2024, 3, 4), "BBB", "-6"),  # bigger absolute → the day's pick
        _mk(date(2024, 3, 5), "CCC", "1"),  # below min 2 → day skipped
    ]
    blob = render_report_zip(rows, mode="topmover", min_pct=Decimal("2"))
    assert _xlsx_members(blob) == ["preopen_topmover_2024.xlsx"]
    inner = _read_member(blob, "preopen_topmover_2024.xlsx")
    sheet = _open_part(inner, "xl/worksheets/sheet1.xml")
    assert "BBB" in sheet  # the -6 mover won 2024-03-04
    assert "CCC" not in sheet  # 2024-03-05 had no qualifier → no row


def test_zip_empty_selection_is_valid_single_workbook() -> None:
    blob = render_report_zip([], mode="all", min_pct=Decimal("0"))
    assert _xlsx_members(blob) == ["preopen_all_empty.xlsx"]
    inner = _read_member(blob, "preopen_all_empty.xlsx")
    sheet = _open_part(inner, "xl/worksheets/sheet1.xml")
    for col in REPORT_COLUMNS:
        assert col in sheet  # header-only, opens cleanly


def test_zip_is_deterministic() -> None:
    rows = [_mk(date(2024, 3, 4), "A", "5")]
    a = render_report_zip(rows, mode="all", min_pct=Decimal("0"))
    b = render_report_zip(rows, mode="all", min_pct=Decimal("0"))
    assert a == b  # fixed _ZIP_EPOCH → byte-identical
