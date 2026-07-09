"""Golden byte-identity net for DEC-PREOPEN-PREVDAY-COMMON-001.

The /reports preopen minute-basis calendar-contiguity fix must be a pure
GUARD: on a CONTIGUOUS lake (every real NSE session between the fixture's
first and last day carries bars in BOTH the 1-day and 1-minute series) the
exported workbook bytes must be **byte-for-byte identical** to the frozen
pre-fix capture. The fixture deliberately spans the three calendar shapes a
naive "gap = holiday" reading gets wrong:

* a plain **weekend** (Fri 2024-03-22 → Tue 2024-03-26 also crosses one),
* the **Holi 2024 cluster** (Mon 2024-03-25 is a listed holiday, so
  2024-03-22 → 2024-03-26 is contiguous ACROSS holiday + weekend), and
* the **Budget-Saturday special session** (Sat 2025-02-01 IS a trading
  session — SPECIAL_WEEKEND_TRADING_SESSIONS — so Mon 2025-02-03 anchors on
  the Saturday, and the Saturday itself anchors on Fri 2025-01-31).

The frozen bytes were captured by running
``python tests/golden/generate_preopen_prevday_golden.py`` on the PRE-change
tree (origin/main 63ab35f, 2026-07-09) — the fixtures are never regenerated
by the fix itself (Golden Rule #1: the pre-fix behaviour is the oracle for
the contiguous arm).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
from brutex.dashboard.preopen_report import (
    PreOpenRow,
    build_preopen_rows,
    render_report_zip,
    render_xlsx,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DAY_XLSX = FIXTURES / "preopen_prevday_day.xlsx"
GOLDEN_MINUTE_XLSX = FIXTURES / "preopen_prevday_minute.xlsx"
GOLDEN_ZIP = FIXTURES / "preopen_prevday_all.zip"

#: Pinned generation stamp — the .zip member mtime is the injected clock
#: (DEC-PREOPEN-XLSX-DATE-001), so determinism needs a fixed ``now``.
ZIP_NOW = datetime(2026, 7, 9, 12, 0, 0)

#: The fixture's dataset triples, in the resolver's symbol-sorted order.
DATASETS = [("NSE", "CASH", "GOLDBUDGET"), ("NSE", "CASH", "GOLDHOLI")]


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


def _day_pair(d: date, *, open_: str, close: str) -> list[CandleBar]:
    """One session's 1-minute pair: the 09:15 IST open bar + a later last bar.

    03:45 UTC == 09:15 IST (the session-open minute whose OPEN is the minute
    IEP); 09:59 UTC == 15:29 IST (the session's LAST minute, whose close is
    the next day's minute-basis PREV. CLOSE). Distinct closes keep the
    last-close law distinguishable.
    """
    mid = str(Decimal(open_) + Decimal("0.05"))
    return [
        _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=open_, close=mid),
        _bar(datetime(d.year, d.month, d.day, 9, 59, tzinfo=UTC), open_=mid, close=close),
    ]


def build_golden_lake(lake: Path) -> None:
    """Write the CONTIGUOUS two-symbol 1day+1minute golden lake.

    GOLDHOLI — the Holi 2024 cluster: Thu 2024-03-21, Fri 2024-03-22,
    Tue 2024-03-26 (Mon 2024-03-25 is Holi; Sat/Sun in between) — every real
    session in the span carries bars, so both 03-22 and 03-26 emit.

    GOLDBUDGET — the Budget-Saturday window: Fri 2025-01-31,
    Sat 2025-02-01 (a SPECIAL_WEEKEND_TRADING_SESSIONS session),
    Mon 2025-02-03 — 02-01 anchors on 01-31 and 02-03 anchors on the
    SATURDAY (never on Friday). Jan/Feb bars are written in separate calls
    (ParquetWriter writes one (year, month) partition per call).
    """
    writer = ParquetWriter(lake_root=lake)
    holi_days = [
        (date(2024, 3, 21), "100.00", "101.00"),
        (date(2024, 3, 22), "101.50", "102.00"),
        (date(2024, 3, 26), "103.00", "104.50"),
    ]
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="GOLDHOLI",
        interval="1day",
        bars=[
            _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=o, close=c)
            for d, o, c in holi_days
        ],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="GOLDHOLI",
        interval="1minute",
        bars=[b for d, o, c in holi_days for b in _day_pair(d, open_=o, close=c)],
    )
    budget_jan = [(date(2025, 1, 31), "500.00", "505.00")]
    budget_feb = [
        (date(2025, 2, 1), "503.00", "504.00"),
        (date(2025, 2, 3), "503.50", "507.50"),
    ]
    for chunk in (budget_jan, budget_feb):
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="GOLDBUDGET",
            interval="1day",
            bars=[
                _bar(datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC), open_=o, close=c)
                for d, o, c in chunk
            ],
        )
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="GOLDBUDGET",
            interval="1minute",
            bars=[b for d, o, c in chunk for b in _day_pair(d, open_=o, close=c)],
        )


def golden_rows(lake: Path) -> dict[str, list[PreOpenRow]]:
    """Build both bases' rows over the golden lake (the report's real spine)."""
    conn = duckdb.connect()
    start, end = date(2024, 1, 1), date(2025, 12, 31)
    return {
        "day": build_preopen_rows(conn, lake, datasets=DATASETS, start=start, end=end, basis="day"),
        "minute": build_preopen_rows(
            conn, lake, datasets=DATASETS, start=start, end=end, basis="minute"
        ),
    }


def golden_artifacts(lake: Path) -> dict[str, bytes]:
    """Render the three frozen artifacts from a freshly-built golden lake."""
    rows = golden_rows(lake)
    return {
        "day": render_xlsx(rows["day"], title="DEC-PREOPEN-PREVDAY-COMMON-001 golden"),
        "minute": render_xlsx(rows["minute"], title="DEC-PREOPEN-PREVDAY-COMMON-001 golden"),
        "zip": render_report_zip(rows["day"], mode="all", min_pct=Decimal("0"), now=ZIP_NOW),
    }


def test_golden_lake_rows_are_the_expected_contiguous_set(tmp_path: Path) -> None:
    # Sanity BEFORE the byte pins: 4 rows per basis — GOLDHOLI 03-22 + 03-26
    # (Holi cluster stepped over, never a hole) and GOLDBUDGET 02-01 (the
    # SATURDAY anchors on Friday) + 02-03 (Monday anchors on the SATURDAY).
    build_golden_lake(tmp_path)
    rows = golden_rows(tmp_path)
    for basis in ("day", "minute"):
        assert [(r.day, r.symbol) for r in rows[basis]] == [
            (date(2024, 3, 22), "GOLDHOLI"),
            (date(2024, 3, 26), "GOLDHOLI"),
            (date(2025, 2, 1), "GOLDBUDGET"),
            (date(2025, 2, 3), "GOLDBUDGET"),
        ]
    # Day basis anchors on the prior session's 1-DAY close…
    assert rows["day"][1].prev_close == Decimal("102.00")  # 03-26 ← Fri 03-22
    assert rows["day"][3].prev_close == Decimal("504.00")  # 02-03 ← Sat 02-01
    # …the minute basis on its LAST 1-minute close (same values by fixture).
    assert rows["minute"][2].prev_close == Decimal("505.00")  # Sat ← Fri 01-31


def test_day_basis_bytes_match_prefix_capture(tmp_path: Path) -> None:
    build_golden_lake(tmp_path)
    assert golden_artifacts(tmp_path)["day"] == GOLDEN_DAY_XLSX.read_bytes()


def test_minute_basis_bytes_match_prefix_capture(tmp_path: Path) -> None:
    build_golden_lake(tmp_path)
    assert golden_artifacts(tmp_path)["minute"] == GOLDEN_MINUTE_XLSX.read_bytes()


def test_zip_bytes_match_prefix_capture(tmp_path: Path) -> None:
    build_golden_lake(tmp_path)
    assert golden_artifacts(tmp_path)["zip"] == GOLDEN_ZIP.read_bytes()
