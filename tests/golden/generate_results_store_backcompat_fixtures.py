"""Generate the frozen pre-column results-store back-compat fixtures.

DEC-VIX-STAMP-WIRE-001 (Stage C): :class:`brutex.engine.results_store.ResultRow`
gained the three nullable REFERENCE-ONLY India-VIX columns ``vix_mean`` /
``vix_min`` / ``vix_max``, appended LAST in the Parquet column order and read
back through the optional-column law (``results_store._optional_float``): a
store written BEFORE a column existed loads that field as ``None`` — the honest
unknown, never a fabricated value (Golden Rule #1). The committed fixtures here
are real on-disk files in the two HISTORICAL schemas, so the reader law is
pinned against genuine legacy bytes, not a mock:

* ``fixtures/results_store_pre_vix_columns.parquet`` — the exact pre-Stage-C
  layout (…, ``deflated_sharpe``, prices; NO VIX trio). Loads with the trio
  ``None`` while ``deflated_sharpe`` round-trips (value AND null).
* ``fixtures/results_store_pre_deflated_sharpe.parquet`` — the older
  optional-column generation (…, prices; NO ``deflated_sharpe``, NO VIX trio;
  every column the current reader REQUIRES is present). Loads with
  ``deflated_sharpe`` AND the trio ``None``.

HOW THE BYTES ARE PRODUCED (Golden Rule #1): the row VALUES are built by the
REAL :class:`~brutex.engine.results_store.ResultRow` (live validation + the 2dp
price snap), and the byte stream uses the store's REAL determinism knobs
(:data:`~brutex.engine.results_store.ZSTD_LEVEL`,
:data:`~brutex.engine.results_store.ROW_GROUP_SIZE`, ``statistics=False``,
``use_pyarrow=False`` — the exact ``_render_bytes`` settings). ONLY the column
lists are frozen LOCAL copies of the two historical layouts — deliberately NOT
imported from the live module, because the live tuples may grow again while a
committed legacy file never changes (a golden freezes history; this is not a
CLAUDE.md §11 COMMON violation, same stance as every byte-frozen corpus here).

Like the pinned store-content hash in ``test_engine_sweep_run_golden.py``, the
emitted bytes are polars/zstd-build sensitive; CI pins the toolchain, and the
consuming golden (``tests/golden/test_results_store_backcompat_golden.py``)
both re-renders via this module AND pins the committed files' SHA-256.

Regenerate (deterministic — requires the ``[data]`` extra):

    .venv/bin/python tests/golden/generate_results_store_backcompat_fixtures.py

then update the ``_EXPECTED_SHA256`` manifest in
``tests/golden/test_results_store_backcompat_golden.py`` if the bytes moved.
"""

from __future__ import annotations

import hashlib
import io
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.engine.results_store import ROW_GROUP_SIZE, ZSTD_LEVEL, ResultRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: Where the committed fixtures live (this file's sibling ``fixtures/`` dir).
FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"
#: The pre-DEC-VIX-STAMP-WIRE-001 store (has ``deflated_sharpe``, no VIX trio).
PRE_VIX_FIXTURE: Final[Path] = FIXTURES_DIR / "results_store_pre_vix_columns.parquet"
#: The pre-``deflated_sharpe`` optional-column generation (no VIX trio either).
PRE_DEFLATED_FIXTURE: Final[Path] = FIXTURES_DIR / "results_store_pre_deflated_sharpe.parquet"
#: The pre-DEC-RANK-PARETO-PERSIST-001 generation (LANE W1): has
#: ``deflated_sharpe`` AND the VIX trio, but NOT the ``pareto_member`` /
#: ``selection_basis`` selection columns.
PRE_SELECTION_FIXTURE: Final[Path] = FIXTURES_DIR / "results_store_pre_selection_columns.parquet"

# ── The FROZEN historical column layouts (deliberate local copies — see the
#    module docstring; a committed legacy file never tracks the live schema) ──
_LEGACY_STRING_COLUMNS: Final[tuple[str, ...]] = (
    "run_id",
    "instrument",
    "tf",
    "direction",
    "mode",
    "condition_names",
)
_LEGACY_INT_COLUMNS: Final[tuple[str, ...]] = (
    "mask_bits_lo",
    "mask_bits_hi",
    "net_pnl_paisa",
    "gross_profit_paisa",
    "gross_loss_paisa",
    "max_drawdown_paisa",
    "expectancy_paisa",
    "total_trades",
    "winning_trades",
    "losing_trades",
)
_LEGACY_METRIC_COLUMNS: Final[tuple[str, ...]] = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "max_drawdown_pct",
)
_LEGACY_PRICE_COLUMNS: Final[tuple[str, ...]] = ("avg_entry_price", "avg_exit_price")
#: The pre-selection generation's TRAILING trio (frozen local copy — the VIX
#: columns were LAST on disk before the W1 selection columns landed).
_LEGACY_VIX_COLUMNS: Final[tuple[str, ...]] = ("vix_mean", "vix_min", "vix_max")

#: The u64→signed-Int64 limb offset (frozen copy of the on-disk mask encoding).
_U64_TO_I64_OFFSET: Final[int] = 1 << 63
_U64_MASK: Final[int] = (1 << 64) - 1


def fixture_rows() -> tuple[ResultRow, ...]:
    """Return the two deterministic rows both fixtures freeze.

    Row 1 carries a REAL ``deflated_sharpe`` (so the pre-VIX fixture proves the
    value round-trips beside the absent trio); row 2 carries ``None`` (the
    nullable arm). Values are fixed literals — same rows on every regeneration.
    """
    common: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "net_pnl_paisa": 125_000,
        "gross_profit_paisa": 200_000,
        "gross_loss_paisa": 75_000,
        "max_drawdown_paisa": 40_000,
        "expectancy_paisa": 3_125,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.50"),
        "avg_exit_price": Decimal("14100.25"),
    }
    row_one = ResultRow(
        run_id=f"{1:064x}",
        mask_bits=0b101,
        deflated_sharpe=0.25,
        **common,  # type: ignore[arg-type]
    )
    row_two = ResultRow(
        run_id=f"{2:064x}",
        mask_bits=0b11,
        deflated_sharpe=None,
        **common,  # type: ignore[arg-type]
    )
    return (row_one, row_two)


def fixture_rows_with_vix() -> tuple[ResultRow, ...]:
    """The :func:`fixture_rows` pair with the VIX trio populated on row 1.

    Row 1 carries a real trio (value round-trip beside the ABSENT selection
    columns); row 2 keeps the all-``None`` trio (the nullable arm).
    """
    from dataclasses import replace

    row_one, row_two = fixture_rows()
    return (replace(row_one, vix_mean=14.5, vix_min=12.25, vix_max=17.75), row_two)


def render_legacy_store_bytes(
    rows: Sequence[ResultRow], *, with_deflated_sharpe: bool, with_vix: bool = False
) -> bytes:
    """Serialise *rows* into one HISTORICAL results-store Parquet byte blob.

    Byte-for-byte the pre-VIX ``ResultsStore._render_bytes`` output
    (``with_deflated_sharpe=True``) or the pre-``deflated_sharpe`` generation
    (``False``): ``run_id``-sorted rows, the frozen legacy column order, and
    the store's exact determinism knobs. The VIX trio is NEVER emitted — that
    absence IS the fixture.
    """
    import polars as pl

    ordered = sorted(rows, key=lambda row: row.run_id)
    columns: dict[str, list[object]] = {}
    for name in _LEGACY_STRING_COLUMNS:
        columns[name] = [getattr(row, name) for row in ordered]
    columns["mask_bits_lo"] = [(row.mask_bits & _U64_MASK) - _U64_TO_I64_OFFSET for row in ordered]
    columns["mask_bits_hi"] = [(row.mask_bits >> 64) - _U64_TO_I64_OFFSET for row in ordered]
    for name in _LEGACY_INT_COLUMNS[2:] + _LEGACY_METRIC_COLUMNS:
        columns[name] = [getattr(row, name) for row in ordered]
    if with_deflated_sharpe:
        columns["deflated_sharpe"] = [row.deflated_sharpe for row in ordered]
    for name in _LEGACY_PRICE_COLUMNS:
        columns[name] = [float(getattr(row, name)) for row in ordered]
    if with_vix:
        for name in _LEGACY_VIX_COLUMNS:
            columns[name] = [getattr(row, name) for row in ordered]

    # Polars orders columns by the SCHEMA dict, so it must mirror the historical
    # insertion order exactly: strings → ints → metrics → deflated → prices.
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        **dict.fromkeys(_LEGACY_STRING_COLUMNS, pl.Utf8),
        **dict.fromkeys(_LEGACY_INT_COLUMNS, pl.Int64),
        **dict.fromkeys(_LEGACY_METRIC_COLUMNS, pl.Float64),
    }
    if with_deflated_sharpe:
        schema["deflated_sharpe"] = pl.Float64
    schema.update(dict.fromkeys(_LEGACY_PRICE_COLUMNS, pl.Float64))
    if with_vix:
        schema.update(dict.fromkeys(_LEGACY_VIX_COLUMNS, pl.Float64))
    df = pl.DataFrame(columns, schema=schema)
    bio = io.BytesIO()
    df.write_parquet(
        bio,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        row_group_size=ROW_GROUP_SIZE,
        use_pyarrow=False,
        statistics=False,
    )
    return bio.getvalue()


def main() -> None:
    """Write both fixtures and print their SHA-256 manifest."""
    rows = fixture_rows()
    for path, with_deflated in (
        (PRE_VIX_FIXTURE, True),
        (PRE_DEFLATED_FIXTURE, False),
    ):
        content = render_legacy_store_bytes(rows, with_deflated_sharpe=with_deflated)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"{hashlib.sha256(content).hexdigest()}  {path.name}")
    # The pre-W1-selection-columns generation (deflated + VIX, no selection).
    content = render_legacy_store_bytes(
        fixture_rows_with_vix(), with_deflated_sharpe=True, with_vix=True
    )
    PRE_SELECTION_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    PRE_SELECTION_FIXTURE.write_bytes(content)
    print(f"{hashlib.sha256(content).hexdigest()}  {PRE_SELECTION_FIXTURE.name}")


if __name__ == "__main__":
    main()
