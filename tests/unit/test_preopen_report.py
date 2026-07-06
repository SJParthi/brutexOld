"""Tests for `brutex.dashboard.preopen_report` — the NSE-style pre-open .xlsx export.

100% line+branch: the lake readers (1-day close + 1-day open IEP), the dataset
resolver (1day-only gate + symbol filter), the row builder (prev-close
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
import pytest
from brutex.dashboard import preopen_report
from brutex.dashboard.db_view import list_candle_datasets
from brutex.dashboard.preopen_report import (
    _BASIS_INTERVAL,
    _COL_WIDTHS,
    _STYLE_GREEN,
    _STYLE_GREEN_BOLD,
    _STYLE_RED,
    _STYLE_RED_BOLD,
    REPORT_BASES,
    REPORT_COLUMNS,
    REPORT_EXCLUDED_SEGMENTS,
    PreOpenRow,
    _col_letter,
    _daily_close_by_date,
    _daily_open_by_date,
    _daily_prev_close_contiguous,
    _group_by_month,
    _minute_prev_close_contiguous,
    _month_label,
    _previous_trading_day,
    _row_fill_styles,
    build_preopen_rows,
    render_month_workbook,
    render_report_zip,
    render_xlsx,
    report_filename,
    resolve_report_datasets,
    select_all_symbols,
    select_rows,
    select_top_movers,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs


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


def _write_fno(lake: Path, intervals: tuple[str, ...] = ("1minute",)) -> None:
    """Seed one F&O contract partition per interval (segment='FNO')."""
    writer = ParquetWriter(lake_root=lake)
    for interval in intervals:
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-26Jun2024-FUT",
            interval=interval,
            bars=[_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="100", close="101")],
        )


def test_resolve_excludes_fno_datasets(tmp_path: Path) -> None:
    # DEC-O1-CLAIMS-HONESTY-001 SANCTIONED behavior change: an F&O contract
    # partition holding a basis interval is never a pre-open report symbol —
    # it is pruned at the walk level. Fails before PART 1b, passes after.
    _coalindia(tmp_path)  # CASH: 1day + 1minute
    _write_fno(tmp_path, intervals=("1day", "1minute"))
    for basis in REPORT_BASES:
        triples = resolve_report_datasets(tmp_path, basis=basis)
        assert ("NSE", "CASH", "COALINDIA") in triples
        assert all(seg != "FNO" for _exch, seg, _sym in triples)


def test_resolve_walk_is_filtered_at_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The discovery call itself carries the filters (walk-level pruning —
    # DEC-O1-CLAIMS-HONESTY-001), never a walk-everything-then-discard.
    captured: list[dict[str, object]] = []

    def _spy(
        lake_root: Path,
        *,
        exclude_segments: frozenset[str] = frozenset(),
        intervals: object = None,
    ) -> list[dict[str, str]]:
        captured.append({"exclude_segments": exclude_segments, "intervals": intervals})
        return []

    monkeypatch.setattr(preopen_report, "list_candle_datasets", _spy)
    assert resolve_report_datasets(tmp_path, basis="day") == []
    assert resolve_report_datasets(tmp_path, basis="minute") == []
    assert captured == [
        {"exclude_segments": REPORT_EXCLUDED_SEGMENTS, "intervals": frozenset({"1day"})},
        {"exclude_segments": REPORT_EXCLUDED_SEGMENTS, "intervals": frozenset({"1minute"})},
    ]


def test_resolve_datasets_seam_byte_identical(tmp_path: Path) -> None:
    # The pre-walked ``datasets=`` seam (the warm's zero-extra-walk path) must
    # produce a result byte-identical to the filtered-walk path — the in-loop
    # screens are the semantic truth, the walk filters a pure optimization.
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
    _write_fno(tmp_path)
    unfiltered = list_candle_datasets(tmp_path)
    for basis in REPORT_BASES:
        walked = resolve_report_datasets(tmp_path, basis=basis)
        assert walked == resolve_report_datasets(tmp_path, basis=basis, datasets=unfiltered)
    assert resolve_report_datasets(tmp_path, basis="day") == [("NSE", "CASH", "ONLYDAY")]
    assert resolve_report_datasets(tmp_path, basis="minute") == [("NSE", "CASH", "ONLYMIN")]


def test_report_bases_pins_basis_interval() -> None:
    # DYNAMIC drift pin: a new basis added to _BASIS_INTERVAL must extend
    # REPORT_BASES too (else it would never be warmed post-pull).
    assert set(REPORT_BASES) == set(_BASIS_INTERVAL)


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
            # IEP = the 1-day OPEN (116.11); close just needs >= open for a valid
            # bar (the helper sets high=close, low=open) and is irrelevant here.
            _bar(datetime(2021, 1, 4, 3, 45, tzinfo=UTC), open_="116.11", close="117"),
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


def test_build_and_render_space_symbol(tmp_path: Path) -> None:
    # DEC-PREOPEN-SPACE-SYMBOL-001: a space-containing INDEX symbol (e.g.
    # 'INDIA VIX', 'NIFTY 50', 'NIFTY TOTAL MKT') used to 500 the whole .zip
    # export (``illegal candle symbol 'INDIA VIX'``). The candle-symbol validator
    # now admits a literal space (same safety class as '&'), so the symbol is
    # queryable end-to-end and no stored data is dropped.
    _write(
        tmp_path,
        "INDIA VIX",
        "1day",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="14.20", close="14.85"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="15.10", close="15.40"),
        ],
    )
    assert resolve_report_datasets(tmp_path, ["INDIA VIX"]) == [("NSE", "CASH", "INDIA VIX")]
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "INDIA VIX")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(rows) == 1
    assert rows[0].symbol == "INDIA VIX"
    assert rows[0].prev_close == Decimal("14.85")
    assert rows[0].iep == Decimal("15.10")
    # Renders without raising; the space survives into the SYMBOL cell.
    sheet = _open_part(render_xlsx(rows), "xl/worksheets/sheet1.xml")
    assert "INDIA VIX" in sheet


def test_build_preopen_rows_resilient_to_one_bad_symbol(tmp_path: Path) -> None:
    # DEC-PREOPEN-SPACE-SYMBOL-001 resilience belt: one un-queryable symbol must
    # NEVER 500 the whole export. A datasets entry whose symbol carries a
    # glob-metachar (still rejected by the validator) is logged + skipped; the
    # good symbol still builds. (Defence-in-depth: even if a non-validatable
    # coordinate slips through dataset discovery, the build degrades, not crashes.)
    _write(
        tmp_path,
        "COALINDIA",
        "1day",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="400", close="410"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="412", close="415"),
        ],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        # 'BAD*SYM' has a glob metachar → _validate_candle_coords raises → skipped.
        datasets=[("NSE", "CASH", "BAD*SYM"), ("NSE", "CASH", "COALINDIA")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert [r.symbol for r in rows] == ["COALINDIA"]  # bad symbol skipped, good one kept


def test_build_minute_basis(tmp_path: Path) -> None:
    # DEC-PREOPEN-MINUTE-BASIS-001: the minute basis takes IEP from the 09:15:00
    # 1-minute session-open OPEN and PREV. CLOSE from the PRIOR trading day's LAST
    # 1-minute close — both on the 1-minute scale (never the 1-day bar).
    # 03:45 UTC == 09:15 IST (session open); a later same-day bar is the last close.
    _write(
        tmp_path,
        "COALINDIA",
        "1minute",
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="100", close="101"),  # d1 09:15
            _bar(datetime(2024, 3, 1, 3, 50, tzinfo=UTC), open_="104", close="105"),  # d1 last
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="106", close="107"),  # d2 09:15
            _bar(datetime(2024, 3, 4, 3, 50, tzinfo=UTC), open_="108", close="110"),  # d2 last
        ],
    )
    # Minute basis qualifies on the 1minute partition (no 1day needed).
    assert resolve_report_datasets(tmp_path, ["COALINDIA"], basis="minute") == [
        ("NSE", "CASH", "COALINDIA")
    ]
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "COALINDIA")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        basis="minute",
    )
    # Only 2024-03-04 emits (2024-03-01 has no prior trading day).
    assert len(rows) == 1
    assert rows[0].day == date(2024, 3, 4)
    assert rows[0].prev_close == Decimal("105")  # d1's LAST 1-minute close
    assert rows[0].iep == Decimal("106")  # d2's 09:15 1-minute OPEN
    assert rows[0].chng == Decimal("1")


def test_resolve_report_datasets_basis_gate(tmp_path: Path) -> None:
    # DEC-PREOPEN-MINUTE-BASIS-001: a 1day-only symbol qualifies for basis="day"
    # but NOT basis="minute", and a 1minute-only symbol qualifies for
    # basis="minute" but NOT basis="day" (each basis reads its own partition).
    _write(
        tmp_path,
        "DAYONLY",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="10", close="11")],
    )
    _write(
        tmp_path,
        "MINONLY",
        "1minute",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="20", close="21")],
    )
    day_syms = {s for _, _, s in resolve_report_datasets(tmp_path, basis="day")}
    min_syms = {s for _, _, s in resolve_report_datasets(tmp_path, basis="minute")}
    assert "DAYONLY" in day_syms and "DAYONLY" not in min_syms
    assert "MINONLY" in min_syms and "MINONLY" not in day_syms


def test_build_minute_basis_no_minute_partition_is_empty(tmp_path: Path) -> None:
    # Minute-basis build on a symbol with only a 1day partition: the 09:15-open
    # helper returns {} for the absent 1minute partition → no rows, no crash
    # (covers the empty-partition branch of _intraday_open_by_date).
    _write(
        tmp_path,
        "DAYONLY",
        "1day",
        [_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="10", close="11")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "DAYONLY")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        basis="minute",
    )
    assert rows == []


# ── DEC-PREOPEN-PREVCLOSE-CONTIGUITY-001 — no fake mover across a data hole ────


def test_minute_prev_close_contiguous_branches() -> None:
    # The pure guard: PREV. CLOSE is the IMMEDIATELY-preceding 1-day session's
    # last 1-minute close, valid ONLY when that session has minute data.
    d1, d2, d3 = date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)
    trading_dates = [d1, d2, d3]
    minute_last = {d1: Decimal("630")}
    # valid — the prior session (d1) has minute data → its last close.
    assert _minute_prev_close_contiguous(minute_last, trading_dates, d2) == Decimal("630")
    # missing — the prior session (d2) has NO minute data → None (skip, no fake anchor).
    assert _minute_prev_close_contiguous(minute_last, trading_dates, d3) is None
    # first session — no prior 1-day date (idx == 0) → None.
    assert _minute_prev_close_contiguous(minute_last, trading_dates, d1) is None
    # zero prior close → None (never a /0 %CHNG).
    assert _minute_prev_close_contiguous({d2: Decimal("0")}, [d2, d3], d3) is None


def test_build_minute_basis_skips_row_when_prior_window_missing(tmp_path: Path) -> None:
    # The JUBLINGREA bug (DEC-PREOPEN-PREVCLOSE-CONTIGUITY-001): the prior trading
    # day's 1-minute window is MISSING from the lake (it failed/was never fetched),
    # but a STALE far-earlier minute close exists. The 1-day series is the calendar
    # and proves the prior session (06-03) IS a trading day — so the report must
    # SKIP 06-04 rather than anchor PREV. CLOSE on the stale 685 (a fake −7.88%).
    # ParquetWriter writes ONE (year, month) partition per call — split by month.
    _write(
        tmp_path,
        "JUBLINGREA",
        "1day",
        [_bar(datetime(2026, 4, 1, 3, 45, tzinfo=UTC), open_="680", close="685")],
    )
    _write(
        tmp_path,
        "JUBLINGREA",
        "1day",
        [
            _bar(datetime(2026, 6, 3, 3, 45, tzinfo=UTC), open_="629", close="630"),
            _bar(datetime(2026, 6, 4, 3, 45, tzinfo=UTC), open_="631", close="632"),
        ],
    )
    # a stale 01-Apr minute close (685) — the value the OLD code anchored on
    _write(
        tmp_path,
        "JUBLINGREA",
        "1minute",
        [_bar(datetime(2026, 4, 1, 3, 45, tzinfo=UTC), open_="684", close="685")],
    )
    # 03-Jun (the real prior session) is ABSENT — the missing window. 04-Jun has
    # its 09:15 open but no valid prior-session anchor.
    _write(
        tmp_path,
        "JUBLINGREA",
        "1minute",
        [_bar(datetime(2026, 6, 4, 3, 45, tzinfo=UTC), open_="631", close="631.5")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "JUBLINGREA")],
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        basis="minute",
    )
    # 01-Apr → skipped (first session); 04-Jun → skipped (prior 03-Jun minute hole).
    # NO fake −7.88% row anchored on the stale 685.
    assert rows == []


def test_build_minute_basis_contiguous_uses_prior_session(tmp_path: Path) -> None:
    # The healthy case: the prior trading session (06-03) HAS minute data, so the
    # 04-Jun row is emitted with PREV. CLOSE = 03-Jun's last 1-minute close (630).
    _write(
        tmp_path,
        "JUBLINGREA",
        "1day",
        [
            _bar(datetime(2026, 6, 3, 3, 45, tzinfo=UTC), open_="629", close="630"),
            _bar(datetime(2026, 6, 4, 3, 45, tzinfo=UTC), open_="631", close="632"),
        ],
    )
    _write(
        tmp_path,
        "JUBLINGREA",
        "1minute",
        [
            _bar(datetime(2026, 6, 3, 3, 45, tzinfo=UTC), open_="629", close="629.5"),
            _bar(datetime(2026, 6, 3, 9, 44, tzinfo=UTC), open_="629.5", close="630"),  # 15:29 last
            _bar(datetime(2026, 6, 4, 3, 45, tzinfo=UTC), open_="631.05", close="631.5"),  # 09:15
        ],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "JUBLINGREA")],
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        basis="minute",
    )
    assert len(rows) == 1
    assert rows[0].day == date(2026, 6, 4)
    assert rows[0].prev_close == Decimal("630")  # 03-Jun's LAST 1-minute close
    assert rows[0].iep == Decimal("631.05")  # 04-Jun's 09:15 open
    assert rows[0].chng == Decimal("1.05")


# ── DEC-PREOPEN-PREVCLOSE-CONTIGUITY-DAILY-001 — daily basis: no jump over a hole ─


def test_daily_prev_close_contiguous_branches() -> None:
    # The pure guard: daily-basis PREV. CLOSE is the IMMEDIATELY-preceding NSE
    # trading session's 1-day close, valid ONLY when that exact session's daily
    # bar is present. The trading sessions below (Wed/Thu/Fri) are contiguous.
    d1, d2, d3 = date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)
    daily = {d1: Decimal("630"), d2: Decimal("632")}
    # valid — the prior session (d1) has a daily bar → its close.
    assert _daily_prev_close_contiguous(daily, [d1, d2], d2) == Decimal("630")
    # first session — no earlier daily bar (idx == 0) → None.
    assert _daily_prev_close_contiguous(daily, [d1, d2], d1) is None
    # hole — the prior session (d2, Thu) bar is MISSING; the latest available
    # earlier bar (d1) is older than the calendar's prior session → None (skip).
    assert _daily_prev_close_contiguous({d1: Decimal("630")}, [d1], d3) is None
    # zero prior close → None (never a /0 %CHNG).
    assert _daily_prev_close_contiguous({d1: Decimal("0"), d2: Decimal("5")}, [d1, d2], d2) is None
    # holiday gap is NOT a hole: 2024-01-26 (Fri, Republic Day) + the weekend sit
    # between the prior session (Thu 01-25) and 01-29 (Mon) — the calendar steps
    # over them so the contiguous series still resolves to the real prior close.
    thu, mon = date(2024, 1, 25), date(2024, 1, 29)
    assert _daily_prev_close_contiguous(
        {thu: Decimal("500"), mon: Decimal("510")}, [thu, mon], mon
    ) == Decimal("500")


def test_previous_trading_day_steps_over_weekend_and_holiday() -> None:
    # day-1 is a trading day → returned directly (loop body never runs).
    assert _previous_trading_day(date(2026, 6, 4)) == date(2026, 6, 3)  # Thu → Wed
    # day-1 is a weekend → walk back to Friday (loop body runs).
    assert _previous_trading_day(date(2026, 6, 8)) == date(2026, 6, 5)  # Mon → Fri
    # Republic Day (Fri 2024-01-26) + the weekend are stepped over → Thu 01-25.
    assert _previous_trading_day(date(2024, 1, 29)) == date(2024, 1, 25)


def test_previous_trading_day_coverage_exhausted_warns_and_returns_none() -> None:
    # Calendar-correctness pack: the OLD silent behaviour is GONE — walking back
    # from the 2020-01-01 pull floor reaches 2019-12-31 (a Tuesday OUTSIDE the
    # published holiday span), which the unguarded calendar silently read as a
    # trading day. The walk now uses the STRICT is_trading_day_checked, emits
    # ONE calendar.coverage_exhausted warning, and returns None (prev-close
    # honestly ABSENT — never a guessed session, never a crash).
    with capture_logs() as logs:
        assert _previous_trading_day(date(2020, 1, 1)) is None
    events = [log for log in logs if log["event"] == "calendar.coverage_exhausted"]
    assert len(events) == 1
    assert events[0]["probe_day"] == "2019-12-31"
    assert events[0]["report_day"] == "2020-01-01"


def test_daily_prev_close_contiguous_absent_when_coverage_exhausted() -> None:
    # The degraded-but-honest boundary: a lake that DOES hold a daily bar before
    # the 2020-01-01 floor cannot have its prior SESSION verified (the calendar
    # coverage ends going back at 2020), so the anchor reads absent and the row
    # is skipped — correct-or-absent, never a wrong number, never a 500.
    with capture_logs():
        prior, day = date(2019, 12, 31), date(2020, 1, 1)
        daily = {prior: Decimal("630"), day: Decimal("640")}
        assert _daily_prev_close_contiguous(daily, [prior, day], day) is None


def test_build_day_basis_skips_row_when_prior_session_missing(tmp_path: Path) -> None:
    # The daily mirror of the JUBLINGREA bug (DEC-PREOPEN-PREVCLOSE-CONTIGUITY-
    # DAILY-001): the immediately-preceding trading session's 1-DAY bar (Wed
    # 2026-06-03) is MISSING from the lake, but a STALE far-earlier daily bar
    # (2026-04-01, close 685) exists. The calendar proves 06-03 IS a trading day,
    # so the report must SKIP 06-04 rather than anchor PREV. CLOSE on the stale
    # 685 (a fake mover). ParquetWriter writes ONE (year, month) partition per
    # call — split April from June.
    _write(
        tmp_path,
        "JUBLINGREA",
        "1day",
        [_bar(datetime(2026, 4, 1, 3, 45, tzinfo=UTC), open_="680", close="685")],
    )
    # 06-03 (the real prior session) is ABSENT; 06-04 has its own 1-day open but
    # no valid prior-session anchor.
    _write(
        tmp_path,
        "JUBLINGREA",
        "1day",
        [_bar(datetime(2026, 6, 4, 3, 45, tzinfo=UTC), open_="631", close="632")],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "JUBLINGREA")],
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
    )
    # 01-Apr → skipped (first session); 04-Jun → skipped (prior 03-Jun daily hole).
    # NO fake mover anchored on the stale 685.
    assert rows == []


def test_build_day_basis_holiday_gap_is_not_skipped(tmp_path: Path) -> None:
    # The healthy case across a real NSE holiday: Republic Day (Fri 2024-01-26) +
    # the weekend separate Thu 01-25 from Mon 01-29. Both daily bars are present,
    # so the calendar steps over the holiday gap and 01-29 IS emitted with
    # PREV. CLOSE = 01-25's close (a holiday gap is NOT a data hole).
    _write(
        tmp_path,
        "HCAL",
        "1day",
        [
            _bar(datetime(2024, 1, 25, 3, 45, tzinfo=UTC), open_="490", close="500"),
            _bar(datetime(2024, 1, 29, 3, 45, tzinfo=UTC), open_="505", close="510"),
        ],
    )
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", "HCAL")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(rows) == 1
    assert rows[0].day == date(2024, 1, 29)
    assert rows[0].prev_close == Decimal("500")  # Thu 01-25's close, across the holiday
    assert rows[0].iep == Decimal("505")  # 01-29's 1-day open
    assert rows[0].chng == Decimal("5")


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


def _member_date(blob: bytes, member: str) -> tuple[int, int, int, int, int, int]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.getinfo(member).date_time


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


def test_zip_member_date_is_the_ist_generation_time_not_1980() -> None:
    # DEC-PREOPEN-XLSX-DATE-001 (amended): each .xlsx member's mtime = the IST
    # GENERATION time (a freshly-downloaded file reads "Date Modified = now"),
    # never the alarming 1 Jan 1980. `now` is injected for a deterministic assert.
    from datetime import datetime

    from brutex.data.session import IST

    gen = datetime(2026, 6, 9, 17, 4, 30, tzinfo=IST)
    rows = [
        _mk(date(2024, 3, 4), "A", "5"),
        _mk(date(2025, 1, 6), "C", "3"),
    ]
    blob = render_report_zip(rows, mode="all", min_pct=Decimal("0"), now=gen)
    # EVERY member (any data year) carries the SAME generation timestamp.
    assert _member_date(blob, "preopen_all_2024.xlsx") == (2026, 6, 9, 17, 4, 30)
    assert _member_date(blob, "preopen_all_2025.xlsx") == (2026, 6, 9, 17, 4, 30)


def test_zip_empty_member_also_gets_the_generation_time() -> None:
    from datetime import datetime

    from brutex.data.session import IST

    gen = datetime(2026, 6, 9, 17, 4, 30, tzinfo=IST)
    blob = render_report_zip([], mode="all", min_pct=Decimal("0"), now=gen)
    assert _member_date(blob, "preopen_all_empty.xlsx") == (2026, 6, 9, 17, 4, 30)


def test_zip_default_now_is_recent_not_1980() -> None:
    # No injected `now` → defaults to datetime.now(IST); the member year is the
    # current year, never 1980 (the bug this whole fix removes).
    from datetime import datetime

    from brutex.data.session import IST

    blob = render_report_zip([_mk(date(2024, 3, 4), "A", "5")], mode="all", min_pct=Decimal("0"))
    member_year = _member_date(blob, "preopen_all_2024.xlsx")[0]
    assert member_year == datetime.now(IST).year
    assert member_year >= 2026


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
    # ``render_report_zip`` stamps each member's mtime with the GENERATION
    # clock (down to the second, DEC-PREOPEN-XLSX-DATE-001) — only the workbook
    # BODY uses ``_ZIP_EPOCH``. So determinism is asserted by PINNING the
    # injectable ``now`` (without it, two calls straddling a 1-second boundary
    # diverge at the DOS-time byte — a flake, never a body difference).
    fixed_now = datetime(2024, 3, 4, 9, 15, 0)
    a = render_report_zip(rows, mode="all", min_pct=Decimal("0"), now=fixed_now)
    b = render_report_zip(rows, mode="all", min_pct=Decimal("0"), now=fixed_now)
    assert a == b  # pinned mtime + fixed _ZIP_EPOCH body → byte-identical


class TestReportFilename:
    """DEC-REPORT-FILENAME-001 — the self-describing download name."""

    def test_xlsx_no_mode_no_symbols(self) -> None:
        name = report_filename(
            extension=".xlsx",
            start=date(2026, 6, 1),
            end=date(2026, 6, 11),
            basis="day",
            symbols=None,
        )
        assert name == "preopen_day_all-symbols_2026-06-01_2026-06-11.xlsx"

    def test_zip_topmover_carries_the_threshold(self) -> None:
        name = report_filename(
            extension=".zip",
            start=date(2026, 6, 1),
            end=date(2026, 6, 11),
            basis="minute",
            symbols=["NIFTY", "BANKNIFTY"],
            mode="topmover",
            min_pct=Decimal("2.5"),
        )
        assert name == "preopen_topmover-min2.5pct_minute_NIFTY-BANKNIFTY_2026-06-01_2026-06-11.zip"

    def test_min_pct_display_is_normalized_not_rounded(self) -> None:
        # 2.0 reads "2" (display normalization of a threshold — NEVER money
        # rounding; DEC-MONEY-2DP-001 is money-only).
        name = report_filename(
            extension=".zip",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=None,
            mode="topmover",
            min_pct=Decimal("2.0"),
        )
        assert "topmover-min2pct" in name

    def test_mode_all_ignores_min_pct(self) -> None:
        name = report_filename(
            extension=".zip",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=None,
            mode="all",
            min_pct=Decimal("3"),
        )
        assert "_all_" in name
        assert "pct" not in name

    def test_topmover_without_threshold_falls_back_to_plain_mode(self) -> None:
        name = report_filename(
            extension=".zip",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=None,
            mode="topmover",
            min_pct=None,
        )
        assert "_topmover_" in name
        assert "pct" not in name

    def test_symbol_overflow_collapses_to_count(self) -> None:
        symbols = [f"SYM{i}" for i in range(8)]
        name = report_filename(
            extension=".zip",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=symbols,
        )
        assert "SYM0-SYM1-SYM2-SYM3-SYM4+3more" in name

    def test_unsafe_symbol_is_sanitized(self) -> None:
        # M&M must not smuggle '&' (or quotes/separators) into the header name.
        name = report_filename(
            extension=".xlsx",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=["M&M", 'BAD"/NAME'],
        )
        assert "M-M-BAD-NAME" in name

    def test_all_unsafe_token_degrades_to_x(self) -> None:
        assert "_X_" in report_filename(
            extension=".xlsx",
            start=date(2026, 6, 1),
            end=date(2026, 6, 2),
            basis="day",
            symbols=["///"],
        )


# ── observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001) ──────────


def _write_overlay(lake: Path, day: str) -> None:
    import json

    from brutex.calendars.observed_sessions import (
        OBSERVED_LABEL,
        OBSERVED_OVERLAY_VERSION,
        overlay_path,
    )

    path = overlay_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": OBSERVED_OVERLAY_VERSION,
                "entries": [
                    {
                        "date": day,
                        "detected_at": "2026-07-04T00:00:00+00:00",
                        "month": day[:7],
                        "label": OBSERVED_LABEL,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_previous_trading_day_observed_subtraction() -> None:
    # An overlaid Tuesday 2026-05-12 is NOT a session — the walk steps over it
    # to Monday 2026-05-11 (remove-only; default empty = byte-identical).
    assert _previous_trading_day(date(2026, 5, 13)) == date(2026, 5, 12)
    assert _previous_trading_day(
        date(2026, 5, 13), observed=frozenset({date(2026, 5, 12)})
    ) == date(2026, 5, 11)


def test_build_daily_basis_overlay_recovers_row(tmp_path: Path) -> None:
    """An observed holiday between two daily bars no longer reads as a data hole.

    Daily bars exist on 2026-05-11 + 2026-05-13; 2026-05-12 (the calendar prior
    session of 05-13) has none. WITHOUT the overlay the contiguity rule skips the
    row (correct-or-absent). WITH the sidecar flagging 05-12 the prior session IS
    05-11, its bar is present -> the row anchors and is emitted.
    """
    _write(
        tmp_path,
        "DRIFTED",
        "1day",
        [
            _bar(datetime(2026, 5, 11, 3, 45, tzinfo=UTC), open_="100", close="110"),
            _bar(datetime(2026, 5, 13, 3, 45, tzinfo=UTC), open_="120", close="125"),
        ],
    )
    datasets = [("NSE", "CASH", "DRIFTED")]
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    without = build_preopen_rows(_conn(), tmp_path, datasets=datasets, start=start, end=end)
    assert [r.day for r in without] == []  # skipped: 05-12 reads as a missing bar
    _write_overlay(tmp_path, "2026-05-12")
    with_overlay = build_preopen_rows(_conn(), tmp_path, datasets=datasets, start=start, end=end)
    assert [r.day for r in with_overlay] == [date(2026, 5, 13)]
    assert with_overlay[0].prev_close == Decimal("110")
