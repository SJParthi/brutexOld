"""SURFACE TRIAL — every number the ``/reports`` pre-open export displays, independently recomputed.

DEC-SURFACE-TRIAL-001: fixtures are written with the REAL repo lake writer
(:class:`~brutex.data.parquet_writer.ParquetWriter` — the same physical layout the
production sink feeds), the REAL ``/api/v1/reports/preopen.xlsx`` + ``.zip`` routes
are driven through the established ``build_app`` + ``TestClient`` pattern, and every
displayed number is recomputed INDEPENDENTLY on the test side — via
:class:`fractions.Fraction` + a hand-rolled half-up rounder, never ``decimal`` and
never the production ``preopen_report`` helpers — then asserted equal cell-for-cell
against the actual sheet XML. A divergence is a production bug by protocol.

Number families trialed (the prioritized list of the PR-2 research):

* **Pre-open row law** — ``CHNG = IEP − PREV.CLOSE``; ``%CHNG = CHNG/PREV × 100``;
  the calendar weekend step (Mon anchors on Fri); the CONTIGUITY refusal (a day
  whose prior session's daily bar is MISSING is SKIPPED, never anchored on a stale
  far-earlier close); the first-known-session absence.
* **DEC-MONEY-2DP-001 / display snap boundaries** — ``%CHNG`` exactly ``+0.005`` →
  cell ``0.01`` and exactly ``−0.005`` → cell ``−0.01`` (ROUND_HALF_UP = away from
  zero on negatives); the sub-boundary ``0 < pct < 0.005`` row that DISPLAYS
  ``0.00`` yet fills GREEN (sign from the FULL-precision value — current behaviour
  pinned deliberately).
* **Top-mover selection** — per-day abs-max (a −6% beats a +5%); the INCLUSIVE
  ``min_pct`` boundary (``>=``); the alphabetical tie-break; a day with no
  qualifier absent; ``mode=all`` ignoring ``min_pct``.
* **Ordering / grouping** — the ``.xlsx`` ``(day, symbol)`` order vs the ``.zip``
  all-mode ``(day, −pct, symbol)`` order; one member per year, one sheet per
  non-empty month; green/red/neutral fills from the full-precision sign.
* **Minute basis** — IEP = the 09:15 1-minute open; PREV. CLOSE = the prior
  session's LAST 1-minute close (not the first); the minute-window HOLE skip.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import math
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")
pytest.importorskip("duckdb", reason="the report reads the lake via duckdb")

from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────────
# Independent recomputation helpers — NO production imports, by design.
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _full_scope_arm_1934(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEC-UNIVERSE-INDEX-ONLY-001 §6 migration law — explicit FULL arm:
        the surface trial needs 6 distinct synthetic symbols — more than the 4
    admitted names, so a Law-3 rename cannot map it (Law 2 full-arm);
        the shipped index4 default keeps its own route serve arms in the
        dedicated scope suites."""
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")


def _half_up_2dp(x: Fraction) -> Fraction:
    """Half-up to 2 decimals, AWAY from zero on negatives — on exact rationals.

    The independent twin of the production display snap (which quantizes a
    Decimal with ROUND_HALF_UP): ``sign(x) * floor(|x|·100 + 1/2) / 100`` —
    implemented on :class:`~fractions.Fraction` so ``x = ±0.005`` is EXACT and
    lands on ``±0.01`` by construction, never by float luck.
    """
    sign = -1 if x < 0 else 1
    return Fraction(sign * math.floor(abs(x) * 100 + Fraction(1, 2)), 100)


def _cell(x: Fraction) -> float:
    """A Fraction → the float the sheet's numeric cell must parse back to."""
    return float(_half_up_2dp(x))


def _pct(prev: str, iep: str) -> Fraction:
    """Full-precision %CHNG from the fixture's raw price strings."""
    p, i = Fraction(prev), Fraction(iep)
    return (i - p) / p * 100


def _chng(prev: str, iep: str) -> Fraction:
    return Fraction(iep) - Fraction(prev)


# Sheet-XML extraction (test-local, stdlib only).
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# The cellXfs style indices of the production stylesheet, transcribed here as the
# trial's EXPECTED wire values (bold=1 is the SYMBOL column; 3/4 green/red plain;
# 5/6 green/red bold; 0 neutral plain).
_BOLD, _GREEN, _RED, _GREEN_BOLD, _RED_BOLD = 1, 3, 4, 5, 6


def _sheet_data_rows(sheet_xml: str) -> list[list[tuple[str | float, int]]]:
    """Parse a worksheet's DATA rows (r >= 3) into ``[(value, style), ...]`` lists."""
    root = ET.fromstring(sheet_xml)  # noqa: S314 — trusted bytes: our own route's output
    out: list[list[tuple[str | float, int]]] = []
    for row in root.findall(".//m:sheetData/m:row", _NS):
        if int(row.get("r", "0")) < 3:  # rows 1-2 are the title + header
            continue
        cells: list[tuple[str | float, int]] = []
        for c in row.findall("m:c", _NS):
            style = int(c.get("s", "0"))
            if c.get("t") == "inlineStr":
                t = c.find("m:is/m:t", _NS)
                cells.append((t.text or "" if t is not None else "", style))
            else:
                v = c.find("m:v", _NS)
                assert v is not None and v.text is not None
                cells.append((float(v.text), style))
        out.append(cells)
    return out


def _xlsx_sheet(blob: bytes, member: str = "xl/worksheets/sheet1.xml") -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read(member).decode("utf-8")


def _zip_member(blob: bytes, member: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read(member)


# ────────────────────────────────────────────────────────────────────────
# Fixtures — March 2024 trading days: Fri 1, Mon 4, Tue 5, Wed 6, Thu 7
# (2024-03-08 is the Mahashivratri holiday; 2/3 the weekend).
# ────────────────────────────────────────────────────────────────────────


def _bar(ts: datetime, *, open_: str, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(max((open_, close), key=Fraction)),
        low=Decimal(min((open_, close), key=Fraction)),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _daily(writer: ParquetWriter, symbol: str, bars: list[tuple[date, str, str]]) -> None:
    """Write 1-day bars (each ``(day, open, close)``) at 09:15 IST (03:45 UTC)."""
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1day",
        bars=[
            _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=o, close=c)
            for d, o, c in bars
        ],
    )


#: The day-basis fixture: {symbol: [(day, open, close), ...]}. Chosen so the
#: emitted rows hit every boundary in the module docstring.
_D = {
    "mar1": date(2024, 3, 1),
    "mar4": date(2024, 3, 4),
    "mar5": date(2024, 3, 5),
    "mar7": date(2024, 3, 7),
}
_DAY_FIXTURE: dict[str, list[tuple[date, str, str]]] = {
    # ALPHA: +0.005% on Mar 4 (half-up UP boundary), −0.005% on Mar 5 (half-up
    # away-from-zero boundary), Mar 6 daily bar MISSING → Mar 7 row SKIPPED.
    "ALPHA": [
        (_D["mar1"], "100.00", "200.00"),
        (_D["mar4"], "200.01", "200.00"),
        (_D["mar5"], "199.99", "210.00"),
        (_D["mar7"], "215.00", "216.00"),
    ],
    # BETA: exact-zero %CHNG on Mar 4 (neutral fill), −6% on Mar 5 (abs top mover).
    "BETA": [
        (_D["mar1"], "149.00", "150.00"),
        (_D["mar4"], "150.00", "141.00"),
        (_D["mar5"], "132.54", "132.00"),
    ],
    # GAMMA: +5% on Mar 4 AND Mar 5 (the abs-max loser must beat it on Mar 5).
    "GAMMA": [
        (_D["mar1"], "99.00", "100.00"),
        (_D["mar4"], "105.00", "100.00"),
        (_D["mar5"], "105.00", "105.00"),
    ],
    # DELTA: +5% on Mar 4 — the exact-|pct| TIE with GAMMA → alphabetical winner.
    "DELTA": [
        (_D["mar1"], "199.00", "200.00"),
        (_D["mar4"], "210.00", "210.00"),
    ],
    # TINY: +0.004% on Mar 4 — DISPLAYS 0.00 but fills GREEN (full-precision sign).
    "TINY": [
        (_D["mar1"], "249.00", "250.00"),
        (_D["mar4"], "250.01", "250.01"),
    ],
}

#: The independently-derived expected rows: (day, symbol, prev, iep) with prev
#: read off the fixture by hand (Mon 4 anchors on Fri 1 across the weekend; Mar 5
#: anchors on Mar 4; ALPHA's Mar 7 is ABSENT — its prior session Mar 6 has no
#: daily bar, the contiguity refusal; every Mar 1 row is ABSENT — first session).
_EXPECTED_DAY_ROWS: list[tuple[date, str, str, str]] = [
    (_D["mar4"], "ALPHA", "200.00", "200.01"),
    (_D["mar4"], "BETA", "150.00", "150.00"),
    (_D["mar4"], "DELTA", "200.00", "210.00"),
    (_D["mar4"], "GAMMA", "100.00", "105.00"),
    (_D["mar4"], "TINY", "250.00", "250.01"),
    (_D["mar5"], "ALPHA", "200.00", "199.99"),
    (_D["mar5"], "BETA", "141.00", "132.54"),
    (_D["mar5"], "GAMMA", "100.00", "105.00"),
]


def _day_client(tmp_path: Path) -> TestClient:
    writer = ParquetWriter(lake_root=tmp_path)
    for symbol, bars in _DAY_FIXTURE.items():
        _daily(writer, symbol, bars)
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store))


def _expected_cells(day: date, symbol: str, prev: str, iep: str) -> list[str | float]:
    """One expected DATE·SYMBOL·PREV·IEP·CHNG·%CHNG display row, all independent."""
    return [
        day.isoformat(),
        symbol,
        _cell(Fraction(prev)),
        _cell(Fraction(iep)),
        _cell(_chng(prev, iep)),
        _cell(_pct(prev, iep)),
    ]


def _expected_styles(pct: Fraction) -> list[int]:
    """Expected per-column style row: fill by FULL-precision sign, SYMBOL bold."""
    if pct > 0:
        plain, sym = _GREEN, _GREEN_BOLD
    elif pct < 0:
        plain, sym = _RED, _RED_BOLD
    else:
        plain, sym = 0, _BOLD
    return [plain, sym, plain, plain, plain, plain]


# ────────────────────────────────────────────────────────────────────────
# The trial — day basis, single .xlsx (route order: (day, symbol), no fills).
# ────────────────────────────────────────────────────────────────────────


def test_xlsx_every_cell_matches_independent_recompute(tmp_path: Path) -> None:
    resp = _day_client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-03-01&to=2024-03-31")
    assert resp.status_code == 200
    rows = _sheet_data_rows(_xlsx_sheet(resp.content))
    # Row LAW: exactly the 8 expected (day, symbol) rows, sorted (day, symbol) —
    # Mar 1 first-session rows absent, ALPHA Mar 7 contiguity-skipped.
    expected = sorted(_EXPECTED_DAY_ROWS)
    assert len(rows) == len(expected)
    for got, (day, symbol, prev, iep) in zip(rows, expected, strict=True):
        assert [v for v, _s in got] == _expected_cells(day, symbol, prev, iep)


def test_halfup_boundary_cells_positive_and_negative(tmp_path: Path) -> None:
    """%CHNG exactly ±0.005 must display ±0.01 — half-up, away from zero."""
    resp = _day_client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-03-01&to=2024-03-31")
    rows = {
        (r[0][0], r[1][0]): [v for v, _s in r] for r in _sheet_data_rows(_xlsx_sheet(resp.content))
    }
    # ALPHA Mar 4: CHNG +0.01 on prev 200.00 → %CHNG = +0.005 exactly → 0.01.
    assert _pct("200.00", "200.01") == Fraction(1, 200)
    assert rows[("2024-03-04", "ALPHA")][4:] == [0.01, 0.01]
    # ALPHA Mar 5: CHNG −0.01 on prev 200.00 → %CHNG = −0.005 exactly → −0.01.
    assert _pct("200.00", "199.99") == Fraction(-1, 200)
    assert rows[("2024-03-05", "ALPHA")][4:] == [-0.01, -0.01]
    # BETA Mar 5: −8.46 on 141.00 → an EXACT −6% (the fixture's arithmetic).
    assert rows[("2024-03-05", "BETA")][4:] == [-8.46, -6.0]


def test_zip_all_mode_cells_order_and_fills(tmp_path: Path) -> None:
    """The all-mode workbook: (day, −pct, symbol) order + full-precision-sign fills."""
    resp = _day_client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-03-01&to=2024-03-31&mode=all&min_pct=99"
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["preopen_all_2024.xlsx"]  # one member per year
    sheet = _xlsx_sheet(_zip_member(resp.content, "preopen_all_2024.xlsx"))
    rows = _sheet_data_rows(sheet)

    # Independent selection: EVERY row (min_pct=99 ignored in all mode), sorted
    # (day, −pct, symbol) — gainers → losers within each day.
    expected = sorted(_EXPECTED_DAY_ROWS, key=lambda r: (r[0], -_pct(r[2], r[3]), r[1]))
    assert len(rows) == len(expected)
    for got, (day, symbol, prev, iep) in zip(rows, expected, strict=True):
        assert [v for v, _s in got] == _expected_cells(day, symbol, prev, iep)
        assert [s for _v, s in got] == _expected_styles(_pct(prev, iep))


def test_sub_boundary_pct_displays_zero_but_fills_green(tmp_path: Path) -> None:
    """The pinned edge: 0 < %CHNG < 0.005 displays 0.00 yet fills GREEN.

    TINY Mar 4 is +0.004% — the cell snaps to ``0.0`` while the fill sign uses
    the FULL-precision value. Current behaviour, deliberately pinned (flagged to
    the operator as a display-consistency question, not a trial-diff failure).
    """
    resp = _day_client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-03-01&to=2024-03-31&mode=all"
    )
    sheet = _xlsx_sheet(_zip_member(resp.content, "preopen_all_2024.xlsx"))
    tiny = [r for r in _sheet_data_rows(sheet) if r[1][0] == "TINY"]
    assert len(tiny) == 1
    values = [v for v, _s in tiny[0]]
    styles = [s for _v, s in tiny[0]]
    assert values[5] == 0.0  # displays 0.00
    assert _pct("250.00", "250.01") == Fraction(1, 250)  # +0.004% full precision
    assert styles == _expected_styles(Fraction(1, 250))  # ...but fills GREEN
    # And the exact-ZERO row stays NEUTRAL (BETA Mar 4) — the sign law's other arm.
    beta4 = [r for r in _sheet_data_rows(sheet) if r[1][0] == "BETA" and r[0][0] == "2024-03-04"]
    assert [s for _v, s in beta4[0]] == _expected_styles(Fraction(0))


def test_topmover_abs_max_tie_and_min_pct_boundary(tmp_path: Path) -> None:
    """Top-mover law: per-day abs-max, alphabetical tie, INCLUSIVE min_pct."""
    client = _day_client(tmp_path)

    def picks(min_pct: str) -> list[tuple[str, str]]:
        resp = client.get(
            "/api/v1/reports/preopen.zip?from=2024-03-01&to=2024-03-31"
            f"&mode=topmover&min_pct={min_pct}"
        )
        assert resp.status_code == 200
        sheet = _xlsx_sheet(_zip_member(resp.content, "preopen_topmover_2024.xlsx"))
        return [(str(r[0][0]), str(r[1][0])) for r in _sheet_data_rows(sheet)]

    # Independent brute-force pick over the test's own row list.
    def independent(min_pct: Fraction) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for day in sorted({r[0] for r in _EXPECTED_DAY_ROWS}):
            q = [
                (abs(_pct(prev, iep)), sym)
                for d, sym, prev, iep in _EXPECTED_DAY_ROWS
                if d == day and abs(_pct(prev, iep)) >= min_pct
            ]
            if q:
                best = min(q, key=lambda t: (-t[0], t[1]))  # abs-max, then A-Z
                out.append((day.isoformat(), best[1]))
        return out

    # min_pct=0: Mar 4 → the +5% TIE (GAMMA vs DELTA) breaks alphabetical →
    # DELTA; Mar 5 → BETA's −6% beats GAMMA's +5% on ABSOLUTE value.
    assert (
        picks("0")
        == independent(Fraction(0))
        == [
            ("2024-03-04", "DELTA"),
            ("2024-03-05", "BETA"),
        ]
    )
    # min_pct=5 is INCLUSIVE (>=): the +5% movers still qualify on Mar 4.
    assert (
        picks("5")
        == independent(Fraction(5))
        == [
            ("2024-03-04", "DELTA"),
            ("2024-03-05", "BETA"),
        ]
    )
    # min_pct=5.5: Mar 4 has NO qualifier → the day is ABSENT; Mar 5 keeps BETA.
    assert picks("5.5") == independent(Fraction("5.5")) == [("2024-03-05", "BETA")]


def test_month_sheets_only_nonempty(tmp_path: Path) -> None:
    """One worksheet per NON-EMPTY month: a March-only fixture yields one sheet."""
    resp = _day_client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=all"
    )
    inner = _zip_member(resp.content, "preopen_all_2024.xlsx")
    with zipfile.ZipFile(io.BytesIO(inner)) as wb:
        sheets = [n for n in wb.namelist() if n.startswith("xl/worksheets/")]
    assert sheets == ["xl/worksheets/sheet1.xml"]
    assert "2024-03" in _xlsx_sheet(inner)  # the sheet is titled by its month


# ────────────────────────────────────────────────────────────────────────
# The trial — minute basis (09:15 open · prior LAST 1-minute close · hole skip).
# ────────────────────────────────────────────────────────────────────────


def _minute_client(tmp_path: Path) -> TestClient:
    """MINSYM: daily calendar Mar 1/4/5/6; 1-minute windows Mar 1, 5, 6 (Mar 4
    minute window MISSING while its daily bar exists — the hole). Mar 5 — the
    day that ANCHORS the one asserted row (Mar 6) — carries TWO minute bars
    with DISTINCT closes, so the 311.00 prev-close assertion genuinely proves
    the LAST-close law (last vs first is distinguishable)."""
    writer = ParquetWriter(lake_root=tmp_path)
    _daily(
        writer,
        "MINSYM",
        [
            (_D["mar1"], "298.00", "300.50"),
            (_D["mar4"], "301.00", "302.00"),
            (_D["mar5"], "310.00", "311.50"),
            (date(2024, 3, 6), "306.00", "307.00"),
        ],
    )
    minute_bars = [
        # Mar 1: two bars — but Mar 1 anchors ONLY Mar 4, whose row is
        # deliberately absent (the hole), so these bars prove nothing about
        # the last-close law; they only exercise the hole skip on a
        # multi-bar first session.
        _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="299.00", close="299.50"),
        _bar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), open_="299.50", close="300.00"),
        # Mar 4: MISSING (the minute-window hole; the daily bar exists).
        # Mar 5: TWO bars with DISTINCT closes. This day anchors the asserted
        # Mar 6 row, so asserting 311.00 below proves the prev-close law takes
        # the day's LAST close (311.00, arg_max(close, timestamp)) and NOT its
        # first (310.50) — mutating production to arg_min fails this test.
        # (Mar 5's own row is still SKIPPED: its prior session Mar 4 has no
        # minute data.)
        _bar(datetime(2024, 3, 5, 3, 45, tzinfo=UTC), open_="310.00", close="310.50"),
        _bar(datetime(2024, 3, 5, 4, 0, tzinfo=UTC), open_="310.50", close="311.00"),
        # Mar 6: present — prior session Mar 5 HAS minute data → the one real row.
        _bar(datetime(2024, 3, 6, 3, 45, tzinfo=UTC), open_="306.00", close="305.00"),
    ]
    writer.write(
        exchange="NSE", segment="CASH", symbol="MINSYM", interval="1minute", bars=minute_bars
    )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store))


def test_minute_basis_last_close_anchor_and_hole_skip(tmp_path: Path) -> None:
    resp = _minute_client(tmp_path).get(
        "/api/v1/reports/preopen.xlsx?from=2024-03-01&to=2024-03-31&basis=minute"
    )
    assert resp.status_code == 200
    rows = _sheet_data_rows(_xlsx_sheet(resp.content))
    # EXACTLY one row: Mar 6. Mar 1 is the first session; Mar 5 is SKIPPED —
    # its prior session Mar 4 has a daily bar but NO minute window (anchoring
    # on Mar 1's 300.00 would be the stale-jump bug the contiguity law kills).
    assert [(r[0][0], r[1][0]) for r in rows] == [("2024-03-06", "MINSYM")]
    # PREV. CLOSE = Mar 5's LAST 1-minute close (311.00 — NOT its first,
    # 310.50, so last-vs-first is genuinely distinguished); IEP = the 09:15 open.
    assert [v for v, _s in rows[0]] == _expected_cells(
        date(2024, 3, 6), "MINSYM", "311.00", "306.00"
    )
    # Independent %CHNG: (306 − 311)/311 × 100 = −500/311 → half-up −1.61.
    assert _cell(_pct("311.00", "306.00")) == -1.61
