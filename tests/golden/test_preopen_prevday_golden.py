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
the contiguous arm). The lake builder + artifact renderers live in the shared
``tests/golden/_preopen_prevday_harness.py`` (imported by BOTH this test and
the generator, so neither imports the other — the CodeQL ``py/cyclic-import``
break, alerts 887 + 888).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests.golden._preopen_prevday_harness import (
    GOLDEN_DAY_XLSX,
    GOLDEN_MINUTE_XLSX,
    GOLDEN_ZIP,
    build_golden_lake,
    golden_artifacts,
    golden_rows,
)


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


def test_generator_refuses_to_overwrite_existing_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-fix capture IS the oracle: with the fixtures present and the
    # explicit BRUTEX_REGEN_PREOPEN_GOLDEN=1 escape hatch unset, the generator
    # REFUSES (SystemExit) before writing a byte — a post-fix tree can never
    # silently regenerate the oracle (the hostile-review fold).
    from tests.golden import generate_preopen_prevday_golden as gen

    monkeypatch.delenv(gen.REGEN_ENV, raising=False)
    before = {p: p.read_bytes() for p in (GOLDEN_DAY_XLSX, GOLDEN_MINUTE_XLSX, GOLDEN_ZIP)}
    with pytest.raises(SystemExit, match="REFUSED"):
        gen.main()
    for path, blob in before.items():
        assert path.read_bytes() == blob


def test_generator_never_imports_the_test_module() -> None:
    # Structural pin for the CodeQL py/cyclic-import fix (alerts 887 + 888):
    # the generator's import closure must never re-acquire a module-level
    # edge onto THIS module (the shared harness is the only sanctioned link).
    import ast

    generator = Path(__file__).parent / "generate_preopen_prevday_golden.py"
    source = generator.read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert not {m for m in imported if "test_preopen_prevday_golden" in m}
    assert "tests.golden._preopen_prevday_harness" in imported
