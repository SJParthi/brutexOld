"""Rolling-NOT-fixed-contract structural HONESTY pins (the teeth).

DEC-DHAN-ROLLING-OPTIONS-001. The binding honesty law: Dhan serves NO
per-expired-contract candles — only an ATM-relative ROLLING series — so the resolved
expiry DATE and absolute STRIKE are per-ROW columns, NEVER partition keys, and no
partition/column NAME / log event frames the lake as fixed-contract.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
from brutex.adapters.dhan import rolling_option_constants as c
from brutex.adapters.dhan.rolling_option_parser import (
    RESOLVED_EXPIRY_SOURCE_DERIVED,
    RollingOptionBar,
)
from brutex.adapters.dhan.rolling_option_writer import RollingOptionWriter
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.option_parquet_writer import OptionSeriesCoordinate

#: The exact identifier tokens forbidden in any NAME / docstring / log of the
#: rolling modules (the label-audit set). "per-expired-contract" (an HONEST
#: statement of what Dhan does NOT serve) is deliberately NOT one of these — none of
#: these six tokens is a substring of it.
_FORBIDDEN = (
    "per-contract",
    "per contract",
    "per-expiry",
    "per expiry",
    "per-strike",
    "per strike",
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _rolling_sources() -> list[Path]:
    pkg = Path(c.__file__).parent
    return [*sorted(pkg.glob("rolling_option_*.py")), pkg / "run_dhan_rolling_option_ingest.py"]


def test_label_audit_no_fixed_contract_language_in_rolling_modules() -> None:
    for source_file in _rolling_sources():
        text = source_file.read_text(encoding="utf-8").casefold()
        for token in _FORBIDDEN:
            assert token not in text, f"{source_file.name} contains forbidden token {token!r}"


def test_parse_fno_symbol_never_decodes_a_rolling_label() -> None:
    # parse_fno_symbol is the fixed-contract decoder — it must never resolve a rolling
    # selector label or the rolling subtree name to a contract.
    assert parse_fno_symbol(c.DHAN_ROLLING_SUBDIR) is None
    assert parse_fno_symbol("ATM") is None
    assert parse_fno_symbol("ATMp1") is None
    assert parse_fno_symbol("ATM+1") is None
    assert parse_fno_symbol("EC0") is None


def test_rolling_subdir_absent_from_fixed_contract_reader_heal_modules() -> None:
    # The rolling subtree name must not appear in any fixed-contract reader / heal
    # module — structural proof they can never touch the rolling lake.
    import brutex.data.fno_symbol as _fno
    import brutex.data.lake_heal as _heal
    import brutex.data.lake_invariants as _inv
    import brutex.data.option_parquet_writer as _opw

    for mod in (_fno, _opw, _inv, _heal):
        module_file = mod.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        assert c.DHAN_ROLLING_SUBDIR not in source, (
            f"{Path(module_file).name} names rolling subtree"
        )


def _write_one_bar(tmp_path: Path) -> Path:
    writer = RollingOptionWriter(lake_root=tmp_path)
    coord = OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol="NIFTY",
        expiry_flag="WEEK",
        expiry_code=0,
        strike_offset="ATM+1",
    )
    writer.write(
        coordinate=coord,
        option_type="CALL",
        interval="1minute",
        bars=[
            RollingOptionBar(
                timestamp=datetime(2024, 6, 27, 3, 45, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=10,
                open_interest=5,
                iv=Decimal("0.15"),
                spot=Decimal("24010"),
                resolved_strike=Decimal("24000"),  # the absolute strike (a COLUMN)
                resolved_expiry_date=date(2024, 6, 27),  # the resolved date (a COLUMN)
                resolved_expiry_source=RESOLVED_EXPIRY_SOURCE_DERIVED,
            )
        ],
    )
    files = list(tmp_path.glob("rolling_options/**/*.parquet"))
    assert len(files) == 1
    return files[0]


def test_no_path_segment_is_a_date_or_an_absolute_strike(tmp_path: Path) -> None:
    path = _write_one_bar(tmp_path)
    # The SELECTOR portion of the path (between rolling_options/ and the standard
    # /yyyy/mm.parquet month partition) carries only requested selectors — no date,
    # no absolute strike.
    rel = path.relative_to(tmp_path)
    selector_parts = rel.parts[1:-2]  # drop leading 'rolling_options' + trailing yyyy/mm.parquet
    for part in selector_parts:
        assert not _ISO_DATE.search(part), f"selector segment {part!r} looks like a date"
    # The absolute strike (24000) and the resolved expiry date must NOT appear anywhere
    # in the path — they are per-ROW columns, never partition keys.
    assert "24000" not in rel.parts
    assert "2024-06-27" not in path.as_posix()
    # The strike segment is the OFFSET LABEL, path-hazard-safe.
    assert "ATMp1" in rel.parts


def test_resolved_date_and_strike_are_columns_not_keys(tmp_path: Path) -> None:
    path = _write_one_bar(tmp_path)
    df = pl.read_parquet(path)
    # They live as real, queryable COLUMNS on the bar.
    assert "resolved_expiry_date" in df.columns
    assert "resolved_strike" in df.columns
    assert df.row(0, named=True)["resolved_strike"] == 24000.0
    assert df.row(0, named=True)["resolved_expiry_date"] == date(2024, 6, 27)
