"""Unit tests for the shared CSV-safety primitives (DEC-CSV-FORMULA-INJECTION-001).

Extracted COMMON per DEC-MAPPING-INTEGRITY-EXPORT-001 — these guard EVERY downloadable
CSV (completeness scoreboard, mapping-integrity audit, any future export). 100% line +
branch over ``csv_safe`` + ``FormulaSafeWriter``.
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard._csv_utils import (
    CSV_FORMULA_FIRST_CHARS,
    CSV_INJECTION_LEADERS,
    FormulaSafeWriter,
    csv_safe,
)


def test_csv_safe_neutralises_formula_leaders() -> None:
    # A STRING cell starting with a spreadsheet formula lead-in (= + - @ TAB CR) is prefixed
    # with a single quote so a spreadsheet app renders it literally, never evaluates it.
    for leader in ("=", "+", "-", "@", "\t", "\r"):
        payload = f"{leader}cmd|'/c calc'!A1"
        assert csv_safe(payload) == "'" + payload


def test_csv_safe_neutralises_leading_whitespace_bypass() -> None:
    # The leading-WHITESPACE bypass: " =cmd" / "  @SUM" — first non-whitespace char is a
    # formula lead-in, so it is ALSO neutralised (the out-of-box edge).
    assert csv_safe(" =cmd") == "' =cmd"
    assert csv_safe("  @SUM(A1)") == "'  @SUM(A1)"


def test_csv_safe_passthrough_cases() -> None:
    # Plain string, ISO date, int, None, empty, all-whitespace, and whitespace-then-non-formula
    # (the lstrip branch's false side) all pass through UNCHANGED.
    assert csv_safe("NSE-NIFTY") == "NSE-NIFTY"
    assert csv_safe("2026-06-19") == "2026-06-19"
    assert csv_safe(42) == 42
    assert csv_safe(None) is None
    assert csv_safe("") == ""
    assert csv_safe("   ") == "   "  # all-whitespace → lstrip empty → passthrough
    assert csv_safe(" plain") == " plain"  # whitespace then non-formula → passthrough


def test_formula_safe_writer_sanitises_every_cell() -> None:
    buf = io.StringIO()
    writer = FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))
    writer.writerow(["=HYPERLINK(0)", "plain", 7])
    out = buf.getvalue()
    # The formula cell is quoted; the plain cell + int are untouched.
    assert "'=HYPERLINK(0)" in out
    assert "plain" in out
    assert "7" in out


def test_constants_expose_the_lead_in_sets() -> None:
    # The two guard sets are the documented OWASP CSV-injection lead-ins.
    assert frozenset("=+-@") == CSV_FORMULA_FIRST_CHARS
    assert frozenset("=+-@\t\r") == CSV_INJECTION_LEADERS
