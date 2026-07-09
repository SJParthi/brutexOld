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
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from brutex.calendars.nse import HolidayDataUnavailableError, trading_days_between
from brutex.calendars.observed_sessions import is_trading_day_checked_observed
from brutex.dashboard import db_view, preopen_report
from brutex.dashboard.db_view import list_candle_datasets
from brutex.dashboard.preopen_report import (
    _BASIS_INTERVAL,
    _COL_WIDTHS,
    _HOLE_WARN_CAP,
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
    _hole_detail,
    _hole_shape,
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
from hypothesis import assume, given, settings
from hypothesis import strategies as st
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


def _write(
    lake: Path, symbol: str, interval: str, bars: list[CandleBar], *, exchange: str = "NSE"
) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange=exchange, segment="CASH", symbol=symbol, interval=interval, bars=bars
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


def test_resolve_datasets_seam_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pre-walked ``datasets=`` seam (the warm's zero-extra-walk path) must
    # produce a result byte-identical to the FILTERED-WALK path — the in-loop
    # screens are the semantic truth, the walk filters a pure optimization.
    #
    # DEC-O1-CLAIMS-HONESTY-001 round-4 review fold: the walked side is
    # captured BEFORE any unfiltered discovery. On this ledger-less lake the
    # stat token is the non-None ``absent`` sentinel, so an unfiltered
    # ``list_candle_datasets`` FIRST would build a servable manifest and every
    # later resolve would be manifest-served — silently pinning
    # seam == manifest-projection instead of the named property. A counting
    # wrapper around the REAL ``_walk_candle_datasets`` asserts each walked
    # capture genuinely ran the preopen FILTERED walk (this assertion fails if
    # the ordering ever regresses to manifest-served captures again).
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
    real_walk = db_view._walk_candle_datasets
    walk_exclusions: list[frozenset[str]] = []

    def _counting_walk(
        lake_root: Path,
        *,
        exclude_segments: frozenset[str] = frozenset(),
        intervals: Collection[str] | None = None,
    ) -> list[dict[str, str]]:
        walk_exclusions.append(exclude_segments)
        return real_walk(lake_root, exclude_segments=exclude_segments, intervals=intervals)

    monkeypatch.setattr(db_view, "_walk_candle_datasets", _counting_walk)
    walked_by_basis = {
        basis: resolve_report_datasets(tmp_path, basis=basis) for basis in REPORT_BASES
    }
    # Every walked-side capture ran the genuine filtered walk (one per basis,
    # each carrying the preopen exclusions) — never a manifest-served read.
    assert walk_exclusions == [REPORT_EXCLUDED_SEGMENTS] * len(REPORT_BASES)
    unfiltered = list_candle_datasets(tmp_path)
    for basis in REPORT_BASES:
        assert walked_by_basis[basis] == resolve_report_datasets(
            tmp_path, basis=basis, datasets=unfiltered
        )
    assert walked_by_basis["day"] == [("NSE", "CASH", "ONLYDAY")]
    assert walked_by_basis["minute"] == [("NSE", "CASH", "ONLYMIN")]


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
    # trading day. The walk uses the STRICT is_trading_day_checked, emits ONE
    # warning, and returns None (prev-close honestly ABSENT — never a guessed
    # session, never a crash). DEC-PREOPEN-PREVDAY-COMMON-001 reconcile: the
    # delegator now rides the COMMON nse.previous_trading_session primitive, so
    # the warning is the COMMON calendar.previous_session_coverage_exhausted
    # event (the legacy module-local calendar.coverage_exhausted is RETIRED —
    # a sanctioned observability rename, grep-verified consumer-free).
    with capture_logs() as logs:
        assert _previous_trading_day(date(2020, 1, 1)) is None
    events = [log for log in logs if log["event"] == "calendar.previous_session_coverage_exhausted"]
    assert len(events) == 1
    assert events[0]["probe_date"] == "2019-12-31"
    assert events[0]["requested_date"] == "2020-01-01"


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


# ── DEC-PREOPEN-PREVDAY-COMMON-001 — minute basis checked against the REAL calendar ─
#
# The confirmed defect (independently reproduced on origin/main 63ab35f): the
# minute-basis guard anchored on trading_dates[idx-1] — the symbol's OWN 1-day
# series — with ZERO real-NSE-calendar check, so a real session missing from
# BOTH series (2024-01-03, a plain trading Wednesday) read as "a genuine
# holiday" and 2024-01-04 fabricated prev_close=100.0 → +10.0% across the hole
# (select_top_movers would have crowned it). The day basis on the IDENTICAL
# lake skipped. The fix mirrors the daily guard: the checked calendar
# (_previous_trading_day) is the prior-session authority for BOTH bases.


def _hole_lake(lake: Path, *, jan3_minute_close: str | None = None) -> None:
    """The repro lake: 2024-01-02 + 2024-01-04 in BOTH series; 01-03 daily ABSENT.

    2024-01-02 (Tue) / 01-03 (Wed) / 01-04 (Thu) are ALL real NSE sessions.
    ``jan3_minute_close`` optionally plants a 01-03 MINUTE bar (daily still
    absent) — the mutation-teeth shape: the stale nearest date (01-02, close
    100.00) carries a DIFFERENT close than the true prior session, so an
    ``!=`` → ``==`` mutant on the expected check emits a row and dies.
    """
    _write(
        lake,
        "HOLETEST",
        "1day",
        [
            _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), open_="99.00", close="100.00"),
            _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="111.00"),
        ],
    )
    minute_bars = [
        _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), open_="99.50", close="99.80"),
        _bar(datetime(2024, 1, 2, 9, 59, tzinfo=UTC), open_="99.90", close="100.00"),
        _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="110.50"),
    ]
    if jan3_minute_close is not None:
        minute_bars.insert(
            2,
            _bar(datetime(2024, 1, 3, 3, 45, tzinfo=UTC), open_="105.00", close=jan3_minute_close),
        )
    _write(lake, "HOLETEST", "1minute", minute_bars)


def test_minute_basis_refuses_real_session_hole_regression(tmp_path: Path) -> None:
    # THE regression (the finder's repro, ported): the pre-fix code emitted
    # day=2024-01-04 prev_close=100.0 pct_chng=+10.0 across the 01-03 hole.
    # Now: NO row (honest absence), exactly ONE named WARN, and the Top-mover
    # selector has nothing to crown that day.
    _hole_lake(tmp_path)
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "HOLETEST")],
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="minute",
        )
    assert rows == []  # the fabricated +10.0% row is GONE
    assert select_top_movers(rows, min_pct=Decimal("0")) == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["basis"] == "minute"
    assert events[0]["symbol"] == "HOLETEST"
    assert events[0]["exchange"] == "NSE"
    assert events[0]["segment"] == "CASH"
    assert events[0]["day"] == "2024-01-04"
    assert events[0]["expected_session"] == "2024-01-03"
    assert events[0]["nearest_lake_session"] == "2024-01-02"
    # 01-03 is absent from the minute series too → the GENUINE hole shape,
    # keeping the re-pull remediation verbatim.
    assert events[0]["hole_shape"] == "missing_session"
    assert "re-pull" in str(events[0]["detail"])
    suppressed = [
        log for log in logs if log["event"] == "preopen.prev_close_noncontiguous_suppressed"
    ]
    assert suppressed == []  # under the cap → no summary


def test_day_basis_control_on_the_hole_lake_unchanged(tmp_path: Path) -> None:
    # The day-basis CONTROL on the identical lake: it already skipped (its
    # guard was calendar-checked before this fix) — the payload is unchanged;
    # it now ALSO surfaces the named WARN (observability symmetry).
    _hole_lake(tmp_path)
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "HOLETEST")],
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["basis"] == "day"
    # The daily basis reads no minute series, so its diagnostic is two-way:
    # this genuine hole reads missing_session with the re-pull remediation.
    assert events[0]["hole_shape"] == "missing_session"
    assert "re-pull" in str(events[0]["detail"])


def test_minute_basis_hole_mutation_teeth(tmp_path: Path) -> None:
    # Mutation teeth: 01-03 has a MINUTE bar (close 106.00 ≠ the stale 01-02
    # close 100.00) but its DAILY bar is still absent — the 1-day evidence
    # disagrees with the calendar, so the honest verdict stays SKIP + WARN.
    # An `!=` → `==` mutant on the expected check anchors on 106.00 and emits
    # the row → this assertion kills it (and the distinct closes keep any
    # stale-anchor mutant distinguishable from the true session's close).
    _hole_lake(tmp_path, jan3_minute_close="106.00")
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "HOLETEST")],
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="minute",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    # This fixture IS the prebuild-lag shape (01-03 has minute bars, no 1-day
    # bar): the diagnostic names the pipeline prebuild, never a re-pull.
    assert events[0]["hole_shape"] == "prebuild_lag"
    assert "prebuild" in str(events[0]["detail"])
    assert "re-pull" not in str(events[0]["detail"])


# ── the pure guards: branch-complete over the new seams ─────────────────────


def test_minute_guard_mismatch_invokes_on_hole_with_exact_args() -> None:
    jan2, jan3, jan4 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    calls: list[tuple[date, date]] = []
    got = _minute_prev_close_contiguous(
        {jan2: Decimal("100")},
        [jan2, jan4],
        jan4,
        expected_session=jan3,
        on_hole=lambda expected, found: calls.append((expected, found)),
    )
    assert got is None
    assert calls == [(jan3, jan2)]  # (expected_session, nearest_lake_session)


def test_minute_guard_mismatch_without_on_hole_is_silent() -> None:
    jan2, jan4 = date(2024, 1, 2), date(2024, 1, 4)
    # Default on_hole=None + the default Ellipsis seam (the walk computes
    # 2024-01-03 internally): the refusal VALUE is the whole contract.
    assert _minute_prev_close_contiguous({jan2: Decimal("100")}, [jan2, jan4], jan4) is None


def test_minute_guard_coverage_abstain_returns_none_without_on_hole() -> None:
    # The walk from 2020-01-01 exhausts coverage → the COMMON primitive WARNs
    # once and abstains; the guard returns None WITHOUT invoking on_hole
    # (there is no VERIFIED hole — the prior session is simply unknown).
    prior, day = date(2019, 12, 31), date(2020, 1, 1)
    calls: list[tuple[date, date]] = []
    with capture_logs() as logs:
        got = _minute_prev_close_contiguous(
            {prior: Decimal("100")},
            [prior, day],
            day,
            on_hole=lambda expected, found: calls.append((expected, found)),
        )
    assert got is None
    assert calls == []
    events = [log for log in logs if log["event"] == "calendar.previous_session_coverage_exhausted"]
    assert len(events) == 1


def test_minute_guard_explicit_none_expected_session_abstains() -> None:
    # A caller-supplied None (the memoised abstain) short-circuits identically.
    jan2, jan4 = date(2024, 1, 2), date(2024, 1, 4)
    calls: list[tuple[date, date]] = []
    got = _minute_prev_close_contiguous(
        {jan2: Decimal("100")},
        [jan2, jan4],
        jan4,
        expected_session=None,
        on_hole=lambda expected, found: calls.append((expected, found)),
    )
    assert got is None
    assert calls == []


def test_minute_guard_ellipsis_seam_parity() -> None:
    # The Ellipsis default computes the SAME walk a caller would memoise:
    # helper(...) == helper(expected_session=_previous_trading_day(day)).
    d1, d2 = date(2026, 6, 3), date(2026, 6, 4)
    minute_last = {d1: Decimal("630")}
    for day in (d1, d2):
        assert _minute_prev_close_contiguous(
            minute_last, [d1, d2], day
        ) == _minute_prev_close_contiguous(
            minute_last, [d1, d2], day, expected_session=_previous_trading_day(day)
        )


def test_minute_guard_observed_overlay_steps_over_flagged_day() -> None:
    # The dedicated SPURIOUS-SKIP pin: 2026-05-12 (a derived trading Tuesday)
    # is overlay-flagged as observably untraded. WITHOUT the overlay the
    # 05-13 anchor reads 05-12 → a hole (on_hole fires). WITH it the walk
    # steps over 05-12 to 05-11, whose bar is present → the anchor RESOLVES
    # (the overlay prevents a spurious skip; remove-only).
    may11, may12, may13 = date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)
    minute_last = {may11: Decimal("110")}
    trading = [may11, may13]
    calls: list[tuple[date, date]] = []
    without = _minute_prev_close_contiguous(
        minute_last, trading, may13, on_hole=lambda e, f: calls.append((e, f))
    )
    assert without is None
    assert calls == [(may12, may11)]
    with_overlay = _minute_prev_close_contiguous(
        minute_last, trading, may13, observed=frozenset({may12})
    )
    assert with_overlay == Decimal("110")


def test_daily_guard_mismatch_invokes_on_hole_with_exact_args() -> None:
    jan2, jan3, jan4 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    calls: list[tuple[date, date]] = []
    got = _daily_prev_close_contiguous(
        {jan2: Decimal("100"), jan4: Decimal("110")},
        [jan2, jan4],
        jan4,
        expected_session=jan3,
        on_hole=lambda expected, found: calls.append((expected, found)),
    )
    assert got is None
    assert calls == [(jan3, jan2)]


def test_daily_guard_coverage_abstain_returns_none_without_on_hole() -> None:
    prior, day = date(2019, 12, 31), date(2020, 1, 1)
    calls: list[tuple[date, date]] = []
    with capture_logs():
        got = _daily_prev_close_contiguous(
            {prior: Decimal("630"), day: Decimal("640")},
            [prior, day],
            day,
            on_hole=lambda expected, found: calls.append((expected, found)),
        )
    assert got is None
    assert calls == []


def test_daily_guard_ellipsis_seam_parity() -> None:
    d1, d2 = date(2026, 6, 3), date(2026, 6, 4)
    daily = {d1: Decimal("630"), d2: Decimal("632")}
    for day in (d1, d2):
        assert _daily_prev_close_contiguous(daily, [d1, d2], day) == _daily_prev_close_contiguous(
            daily, [d1, d2], day, expected_session=_previous_trading_day(day)
        )


def test_daily_guard_observed_kwarg_still_steps_over_flagged_day() -> None:
    # The pre-existing observed kwarg survives the seam refactor unchanged.
    may11, may12, may13 = date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)
    daily = {may11: Decimal("110"), may13: Decimal("125")}
    assert _daily_prev_close_contiguous(daily, [may11, may13], may13) is None
    assert _daily_prev_close_contiguous(
        daily, [may11, may13], may13, observed=frozenset({may12})
    ) == Decimal("110")


# ── contiguous-arm parity: the new anchor == the old rule where the old was right ─


def test_minute_guard_contiguous_run_matches_old_rule() -> None:
    # For EVERY day of a fully-contiguous session run the calendar-verified
    # anchor equals the OLD trading_dates[idx-1] rule (the fix only ever
    # REMOVES wrong rows, never changes a right one). 2024 H1 spans weekends,
    # Republic Day, the Holi cluster and Good Friday.
    sessions = trading_days_between(date(2024, 1, 1), date(2024, 6, 30))
    minute_last = {d: Decimal(100) + Decimal(i) for i, d in enumerate(sessions)}
    for i, day in enumerate(sessions):
        old = minute_last.get(sessions[i - 1]) if i > 0 else None
        assert _minute_prev_close_contiguous(minute_last, sessions, day) == old


@settings(max_examples=25, deadline=None)
@given(
    start=st.dates(min_value=date(2020, 2, 1), max_value=date(2026, 10, 1)),
    length=st.integers(min_value=2, max_value=30),
)
def test_minute_guard_contiguous_property_new_equals_old(start: date, length: int) -> None:
    # Hypothesis twin of the deterministic run: any calendar-generated
    # contiguous window anywhere in coverage → new == old for every day.
    # A degenerate draw is REJECTED via assume (never a silent vacuous pass —
    # the hostile-review fold on the earlier bare `return`).
    sessions = trading_days_between(start, start + timedelta(days=length * 3))[:length]
    assume(len(sessions) >= 2)
    minute_last = {d: Decimal(100) + Decimal(i) for i, d in enumerate(sessions)}
    for i, day in enumerate(sessions[1:], start=1):
        assert (
            _minute_prev_close_contiguous(minute_last, sessions, day)
            == minute_last[sessions[i - 1]]
        )


# ── the delegator: equivalence with a verbatim copy of the OLD private walk ──


def _old_previous_trading_day(day: date, *, observed: frozenset[date] = frozenset()) -> date | None:
    # VERBATIM copy of the pre-DEC-PREOPEN-PREVDAY-COMMON-001 private walk
    # (origin/main 63ab35f preopen_report.py::_previous_trading_day), with the
    # abstain-path structlog warning elided — the equivalence pin is the VALUE.
    probe = day - timedelta(days=1)
    while True:
        try:
            if is_trading_day_checked_observed(probe, observed=observed):
                return probe
        except HolidayDataUnavailableError:
            return None
        probe -= timedelta(days=1)


def test_previous_trading_day_delegation_equivalence_full_span() -> None:
    # The thin delegator over nse.previous_trading_session is VALUE-identical
    # to the deleted private walk for EVERY day of the covered span
    # (2020-01-02 → 2026-12-31), with and without an overlay.
    observed_sets = (
        frozenset[date](),
        frozenset({date(2024, 1, 3), date(2025, 9, 26), date(2026, 5, 12)}),
    )
    with capture_logs():
        day = date(2020, 1, 2)
        while day <= date(2026, 12, 31):
            for observed in observed_sets:
                assert _previous_trading_day(day, observed=observed) == (
                    _old_previous_trading_day(day, observed=observed)
                ), day
            day += timedelta(days=1)


@settings(max_examples=100, deadline=None)
@given(
    day=st.dates(min_value=date(2020, 1, 2), max_value=date(2026, 12, 31)),
    observed=st.frozensets(
        st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 12, 31)), max_size=8
    ),
)
def test_previous_trading_day_delegation_equivalence_property(
    day: date, observed: frozenset[date]
) -> None:
    with capture_logs():
        assert _previous_trading_day(day, observed=observed) == _old_previous_trading_day(
            day, observed=observed
        )


# ── build-level calendar shapes (Budget Saturday / Holi / Muhurat / overlay) ─


def _both_series(lake: Path, symbol: str, days: list[tuple[date, str, str]]) -> None:
    """Write 1day + 1minute bars for each (day, open, close), split by month."""
    by_month: dict[tuple[int, int], list[tuple[date, str, str]]] = {}
    for d, o, c in days:
        by_month.setdefault((d.year, d.month), []).append((d, o, c))
    for chunk in by_month.values():
        _write(
            lake,
            symbol,
            "1day",
            [
                _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=o, close=c)
                for d, o, c in chunk
            ],
        )
        _write(
            lake,
            symbol,
            "1minute",
            [
                _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=o, close=c)
                for d, o, c in chunk
            ],
        )


@pytest.mark.parametrize("basis", ["day", "minute"])
def test_build_budget_saturday_is_a_session_both_bases(tmp_path: Path, basis: str) -> None:
    # Sat 2025-02-01 is a SPECIAL_WEEKEND_TRADING_SESSIONS Budget session:
    # with all three sessions present, the SATURDAY row anchors on Friday and
    # Monday 02-03 anchors on the SATURDAY (never on Friday) — zero warns.
    _both_series(
        tmp_path,
        "BUDGET",
        [
            (date(2025, 1, 31), "500.00", "505.00"),
            (date(2025, 2, 1), "503.00", "504.00"),
            (date(2025, 2, 3), "503.50", "507.50"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "BUDGET")],
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            basis=basis,  # type: ignore[arg-type]
        )
    assert [(r.day, r.prev_close) for r in rows] == [
        (date(2025, 2, 1), Decimal("505.00")),  # Saturday ← Friday's close
        (date(2025, 2, 3), Decimal("504.00")),  # Monday ← the SATURDAY's close
    ]
    assert [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"] == []


@pytest.mark.parametrize("basis", ["day", "minute"])
def test_build_missing_budget_saturday_is_a_hole_both_bases(tmp_path: Path, basis: str) -> None:
    # The Saturday session MISSING from the lake: Monday 02-03 must NOT
    # baseline on Friday across the missing special session — skip + warn
    # (pre-fix the minute basis fabricated exactly this row).
    _both_series(
        tmp_path,
        "BUDGET",
        [
            (date(2025, 1, 31), "500.00", "505.00"),
            (date(2025, 2, 3), "503.50", "507.50"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "BUDGET")],
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            basis=basis,  # type: ignore[arg-type]
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["expected_session"] == "2025-02-01"
    assert events[0]["nearest_lake_session"] == "2025-01-31"
    # The Saturday session is absent from BOTH series → the genuine hole
    # shape on both bases (the minute basis holds evidence proving it).
    assert events[0]["hole_shape"] == "missing_session"


def test_build_minute_basis_holi_cluster_gap_is_not_a_hole(tmp_path: Path) -> None:
    # Holi 2024: Mon 03-25 is a listed holiday, so Fri 03-22 → Tue 03-26 is
    # CONTIGUOUS across holiday + weekend — the row is emitted, zero warns
    # (a calendar gap is never a data hole).
    _both_series(
        tmp_path,
        "HOLI",
        [
            (date(2024, 3, 22), "101.50", "102.00"),
            (date(2024, 3, 26), "103.00", "104.50"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "HOLI")],
            start=date(2024, 3, 23),
            end=date(2024, 3, 31),
            basis="minute",
        )
    assert [(r.day, r.prev_close) for r in rows] == [(date(2024, 3, 26), Decimal("102.00"))]
    assert [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"] == []


def test_build_minute_basis_muhurat_bar_day_after_is_skipped(tmp_path: Path) -> None:
    # The ANCHOR-law Muhurat stance (DEC-PREVSESSION-MUHURAT-COHERENCE-001):
    # the 2024-11-01 Diwali Muhurat day is a calendar NON-session for the
    # prior-session walk, so a lake carrying its bars has the day-after row
    # (Mon 11-04) conservatively SKIPPED — expected prior REGULAR session is
    # Thu 2024-10-31 and the pre-open report follows the ENGINE anchor law
    # (mirrors stage1a.muhurat_prev_day_anchor_skip, #1616). The WARN now
    # names the DEFINITIVE muhurat_session_bar shape — a HEALTHY lake, "no
    # action needed" — instead of the old extra_nonsession_bar guess.
    _both_series(
        tmp_path,
        "MUHURAT",
        [
            (date(2024, 11, 1), "100.00", "101.00"),
            (date(2024, 11, 4), "102.00", "103.00"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "MUHURAT")],
            start=date(2024, 11, 2),
            end=date(2024, 11, 30),
            basis="minute",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["expected_session"] == "2024-10-31"
    assert events[0]["nearest_lake_session"] == "2024-11-01"
    detail = str(events[0]["detail"])
    assert events[0]["hole_shape"] == "muhurat_session_bar"
    assert "2024-11-01" in detail and "2024-10-31" in detail  # names found + expected
    assert "Muhurat" in detail
    assert "no action needed" in detail
    assert "re-pull" not in detail
    # The two-law cross-reference: the detail points at the engine skip AND
    # the /movers display-law counterpart, so an operator sees the design.
    assert "stage1a.muhurat_prev_day_anchor_skip" in detail
    assert "prev_close_muhurat" in detail


def test_build_day_basis_muhurat_bar_shape_mirror(tmp_path: Path) -> None:
    # The DAILY twin of the Muhurat pin (day↔minute mirror parity): the
    # found > expected split needs NO minute evidence, so the day basis names
    # the SAME definitive muhurat_session_bar shape.
    _both_series(
        tmp_path,
        "MUHURAT",
        [
            (date(2024, 11, 1), "100.00", "101.00"),
            (date(2024, 11, 4), "102.00", "103.00"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "MUHURAT")],
            start=date(2024, 11, 2),
            end=date(2024, 11, 30),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["hole_shape"] == "muhurat_session_bar"
    assert "no action needed" in str(events[0]["detail"])
    assert "re-pull" not in str(events[0]["detail"])


def test_build_minute_basis_weekend_junk_bar_reads_suspicious_shape(tmp_path: Path) -> None:
    # A NON-Muhurat non-session 1-day bar (a stray Saturday 2024-01-06 write)
    # after the prior session (Fri 2024-01-05) → the reworded
    # extra_nonsession_bar shape: honestly SUSPICIOUS ("inspect"), never the
    # old falsely-reassuring "no action is needed", never a Muhurat guess —
    # and kills the has_muhurat_session-dropped classifier mutant on the
    # integration path (the mutant would read muhurat_session_bar here).
    _both_series(
        tmp_path,
        "JUNKBAR",
        [
            (date(2024, 1, 5), "100.00", "101.00"),
            (date(2024, 1, 6), "200.00", "201.00"),  # Saturday — a stray write
            (date(2024, 1, 8), "102.00", "103.00"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "JUNKBAR")],
            start=date(2024, 1, 8),
            end=date(2024, 1, 8),
            basis="minute",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["hole_shape"] == "extra_nonsession_bar"
    detail = str(events[0]["detail"])
    assert "inspect" in detail
    assert "no action" not in detail
    assert "Diwali" not in detail  # the Muhurat guess is GONE from this shape
    assert "re-pull" not in detail


def test_day_basis_prebuild_lag_lake_reads_missing_session(tmp_path: Path) -> None:
    # The daily basis's DOCUMENTED two-way limit, pinned: on the prebuild-lag
    # lake (01-03 has minute bars but no 1-day bar) the day basis holds no
    # minute evidence — the shape honestly degrades to missing_session (the
    # minute basis, which holds the evidence, names prebuild_lag instead).
    _hole_lake(tmp_path, jan3_minute_close="106.00")
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "HOLETEST")],
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["hole_shape"] == "missing_session"
    assert "re-pull" in str(events[0]["detail"])


def test_build_minute_basis_overlay_recovers_row(tmp_path: Path) -> None:
    # The minute twin of the daily overlay test: bars on 2026-05-11 + 05-13,
    # the derived-calendar prior session 05-12 observed-flagged → the walk
    # steps over it, the anchor resolves on 05-11, and the row is emitted.
    _both_series(
        tmp_path,
        "DRIFTED",
        [
            (date(2026, 5, 11), "100.00", "110.00"),
            (date(2026, 5, 13), "120.00", "125.00"),
        ],
    )
    datasets = [("NSE", "CASH", "DRIFTED")]
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    without = build_preopen_rows(
        _conn(), tmp_path, datasets=datasets, start=start, end=end, basis="minute"
    )
    assert [r.day for r in without] == []  # 05-12 reads as a lake hole
    _write_overlay(tmp_path, "2026-05-12")
    with_overlay = build_preopen_rows(
        _conn(), tmp_path, datasets=datasets, start=start, end=end, basis="minute"
    )
    assert [r.day for r in with_overlay] == [date(2026, 5, 13)]
    assert with_overlay[0].prev_close == Decimal("110.00")


def test_build_minute_basis_pure_minute_fallback_stays_calendar_blind(tmp_path: Path) -> None:
    # The DOCUMENTED honest limit (chosen, not missed): a symbol with NO 1-day
    # partition keeps the previous-AVAILABLE-minute-session fallback even
    # across a real-session hole (01-03), byte-unchanged — there is no 1-day
    # evidence to corroborate, and the ledgered fallback contract stands.
    _write(
        tmp_path,
        "PUREMIN",
        "1minute",
        [
            _bar(datetime(2024, 1, 2, 9, 59, tzinfo=UTC), open_="99.90", close="100.00"),
            _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="110.50"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "PUREMIN")],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            basis="minute",
        )
    assert [(r.day, r.prev_close) for r in rows] == [(date(2024, 1, 4), Decimal("100.00"))]
    assert [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"] == []


# ── the capped, per-(exchange, segment, symbol)-deduped WARN machinery ───────


def test_hole_warn_dedupes_per_dataset_triple(tmp_path: Path) -> None:
    # TWO holes for ONE dataset triple (01-03 and 01-05 both missing) →
    # exactly ONE warn (per-(exchange, segment, symbol) dedup), and still
    # zero rows fabricated.
    _write(
        tmp_path,
        "TWOHOLES",
        "1day",
        [
            _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), open_="99.00", close="100.00"),
            _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="111.00"),
            _bar(datetime(2024, 1, 8, 3, 45, tzinfo=UTC), open_="112.00", close="113.00"),
        ],
    )
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("NSE", "CASH", "TWOHOLES")],
            start=date(2024, 1, 3),
            end=date(2024, 1, 31),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == 1
    assert events[0]["symbol"] == "TWOHOLES"


def test_hole_warn_dual_exchange_symbol_warns_per_exchange(tmp_path: Path) -> None:
    # The hostile-review regression: a DUAL-LISTED symbol with the same hole
    # on BOTH exchanges must warn TWICE (once per (exchange, segment, symbol)
    # triple) — the old bare-symbol dedupe key silenced (and uncounted) the
    # second exchange entirely.
    bars = [
        _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), open_="99.00", close="100.00"),
        _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="111.00"),
    ]
    _write(tmp_path, "DUAL", "1day", bars)
    _write(tmp_path, "DUAL", "1day", bars, exchange="BSE")
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=[("BSE", "CASH", "DUAL"), ("NSE", "CASH", "DUAL")],
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert [(e["exchange"], e["segment"], e["symbol"]) for e in events] == [
        ("BSE", "CASH", "DUAL"),
        ("NSE", "CASH", "DUAL"),
    ]


# ── the pure hole-shape classifier + shape-matched detail ────────────────────


def test_hole_shape_muhurat_session_bar_ignores_minute_evidence() -> None:
    # found > expected on a calendar-confirmed Muhurat date → the DEFINITIVE
    # muhurat_session_bar shape (DEC-PREVSESSION-MUHURAT-COHERENCE-001), with
    # OR without minute evidence (the classification needs none) — identical
    # on both bases.
    expected, found = date(2024, 10, 31), date(2024, 11, 1)
    assert _hole_shape(expected, found, None) == "muhurat_session_bar"
    assert _hole_shape(expected, found, {found: Decimal("1")}) == "muhurat_session_bar"


def test_hole_shape_muhurat_split_covers_every_covered_boundary() -> None:
    # Every MUHURAT_SESSIONS entry classifies muhurat_session_bar against its
    # own prior regular session — the split is table-driven, never hardcoded
    # to 2024.
    from brutex.calendars.nse import MUHURAT_SESSIONS, previous_trading_session

    for muhurat_day in sorted(MUHURAT_SESSIONS):
        expected = previous_trading_session(muhurat_day, exchange="NSE")
        assert expected is not None
        assert _hole_shape(expected, muhurat_day, None) == "muhurat_session_bar", muhurat_day


def test_hole_shape_extra_nonsession_bar_is_the_non_muhurat_arm() -> None:
    # found > expected on a NON-Muhurat date (a stray Saturday bar) → the
    # honestly-suspicious extra_nonsession_bar shape, with OR without minute
    # evidence — kills the has_muhurat_session-dropped mutant (which would
    # bless every later bar as muhurat_session_bar).
    expected, found = date(2024, 1, 5), date(2024, 1, 6)
    assert _hole_shape(expected, found, None) == "extra_nonsession_bar"
    assert _hole_shape(expected, found, {found: Decimal("1")}) == "extra_nonsession_bar"


def test_hole_shape_2026_muhurat_absent_abstains_to_extra_nonsession_bar() -> None:
    # HONEST-LIMIT pin (documented abstain): Diwali 2026 (2026-11-08) has NO
    # MUHURAT_SESSIONS entry (timings not notified — never fabricated), so the
    # arm abstains to the suspicious shape. Self-limiting in production: the
    # ingest keeps Muhurat bars via the SAME MUHURAT_SESSIONS table, so a lake
    # cannot legitimately carry a 2026 Muhurat bar until the entry lands.
    from brutex.calendars.nse import has_muhurat_session

    assert has_muhurat_session(date(2026, 11, 8)) is False
    assert _hole_shape(date(2026, 11, 6), date(2026, 11, 8), None) == "extra_nonsession_bar"


def test_hole_shape_prebuild_lag_requires_minute_presence() -> None:
    # found < expected AND the expected session IS in the minute evidence →
    # the 1-day series lags the minute series.
    expected, found = date(2024, 1, 3), date(2024, 1, 2)
    assert _hole_shape(expected, found, {expected: Decimal("106")}) == "prebuild_lag"


def test_hole_shape_missing_session_arms() -> None:
    # found < expected with NO minute evidence (the daily basis) or with the
    # expected session absent from it (the minute basis) → the genuine hole.
    expected, found = date(2024, 1, 3), date(2024, 1, 2)
    assert _hole_shape(expected, found, None) == "missing_session"
    assert _hole_shape(expected, found, {found: Decimal("100")}) == "missing_session"


def test_hole_detail_texts_are_shape_honest() -> None:
    # Muhurat (DEC-PREVSESSION-MUHURAT-COHERENCE-001): definitive — names
    # found + expected, "no action needed", cross-references the engine skip
    # and the /movers display-law flag; no re-pull advice.
    muhurat = _hole_detail("muhurat_session_bar", date(2024, 10, 31), date(2024, 11, 1))
    assert "2024-11-01" in muhurat
    assert "2024-10-31" in muhurat
    assert "Muhurat" in muhurat
    assert "no action needed" in muhurat
    assert "stage1a.muhurat_prev_day_anchor_skip" in muhurat
    assert "prev_close_muhurat" in muhurat
    assert "re-pull" not in muhurat
    # Non-Muhurat extra bar: honestly SUSPICIOUS ("inspect") — the old Muhurat
    # guess + the falsely-reassuring "no action is needed" are GONE.
    stray = _hole_detail("extra_nonsession_bar", date(2024, 1, 5), date(2024, 1, 6))
    assert "2024-01-06" in stray
    assert "2024-01-05" in stray
    assert "inspect" in stray
    # The old "e.g. a Diwali Muhurat session" GUESS is gone — the shape now
    # affirmatively EXCLUDES Muhurat ("not a known Muhurat session").
    assert "Diwali" not in stray
    assert "not a known Muhurat session" in stray
    assert "no action" not in stray
    assert "re-pull" not in stray
    # Prebuild lag: names the pipeline prebuild, no re-pull advice (verbatim).
    lag = _hole_detail("prebuild_lag", date(2024, 1, 3), date(2024, 1, 2))
    assert "2024-01-03" in lag
    assert "prebuild" in lag
    assert "re-pull" not in lag
    # Genuine hole: the original detail text, verbatim.
    assert _hole_detail("missing_session", date(2024, 1, 3), date(2024, 1, 2)) == (
        "row skipped — prior NSE session missing from the lake; re-pull the missing session"
    )


def test_hole_warn_cap_emits_one_suppressed_summary(tmp_path: Path) -> None:
    # _HOLE_WARN_CAP + 2 hole symbols in one build → exactly the cap emitted +
    # ONE suppressed summary carrying the overflow count.
    n = _HOLE_WARN_CAP + 2
    for i in range(n):
        _write(
            tmp_path,
            f"HOLE{i:02d}",
            "1day",
            [
                _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), open_="99.00", close="100.00"),
                _bar(datetime(2024, 1, 4, 3, 45, tzinfo=UTC), open_="110.00", close="111.00"),
            ],
        )
    datasets = [("NSE", "CASH", f"HOLE{i:02d}") for i in range(n)]
    with capture_logs() as logs:
        rows = build_preopen_rows(
            _conn(),
            tmp_path,
            datasets=datasets,
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
            basis="day",
        )
    assert rows == []
    events = [log for log in logs if log["event"] == "preopen.prev_close_noncontiguous"]
    assert len(events) == _HOLE_WARN_CAP
    summaries = [
        log for log in logs if log["event"] == "preopen.prev_close_noncontiguous_suppressed"
    ]
    assert len(summaries) == 1
    assert summaries[0]["suppressed_symbols"] == 2
    assert summaries[0]["cap"] == _HOLE_WARN_CAP


def test_contiguous_build_emits_zero_hole_warns(tmp_path: Path) -> None:
    # A fully-contiguous lake never warns on either basis.
    _both_series(
        tmp_path,
        "CLEAN",
        [
            (date(2024, 3, 1), "100.00", "101.00"),
            (date(2024, 3, 4), "101.50", "102.00"),
        ],
    )
    for basis in REPORT_BASES:
        with capture_logs() as logs:
            rows = build_preopen_rows(
                _conn(),
                tmp_path,
                datasets=[("NSE", "CASH", "CLEAN")],
                start=date(2024, 1, 1),
                end=date(2024, 12, 31),
                basis=basis,
            )
        assert [r.day for r in rows] == [date(2024, 3, 4)]
        assert [
            log
            for log in logs
            if log["event"]
            in ("preopen.prev_close_noncontiguous", "preopen.prev_close_noncontiguous_suppressed")
        ] == []


def test_prior_session_walk_is_memoised_per_unique_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The perf memo: THREE symbols sharing the SAME two days → the calendar
    # back-walk runs ONCE per unique day (2), not once per (symbol, day) row
    # (6). O(unique days) per build — deliberately NOT an O(1)-total claim.
    for symbol in ("MEMOA", "MEMOB", "MEMOC"):
        _both_series(
            tmp_path,
            symbol,
            [
                (date(2024, 3, 1), "100.00", "101.00"),
                (date(2024, 3, 4), "101.50", "102.00"),
            ],
        )
    real = preopen_report._previous_trading_day
    calls: list[date] = []

    def _counting(day: date, *, observed: frozenset[date] = frozenset()) -> date | None:
        calls.append(day)
        return real(day, observed=observed)

    monkeypatch.setattr(preopen_report, "_previous_trading_day", _counting)
    rows = build_preopen_rows(
        _conn(),
        tmp_path,
        datasets=[("NSE", "CASH", s) for s in ("MEMOA", "MEMOB", "MEMOC")],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        basis="minute",
    )
    assert len(rows) == 3  # one 03-04 row per symbol
    assert sorted(calls) == [date(2024, 3, 1), date(2024, 3, 4)]  # one walk per unique day
