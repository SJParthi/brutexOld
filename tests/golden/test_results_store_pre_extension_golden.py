"""PRE-EXTENSION golden for the results store (gap-register ruling 5, B2).

``tests/golden/fixtures/results_store_pre_extension_v1.parquet`` is a small
REAL store written by TODAY'S :meth:`ResultsStore.flush` — a frozen snapshot
of the schema as it exists BEFORE any future :class:`ResultRow` extension
(MAE/MFE/VIX/arm columns etc.). This suite loads it and asserts EVERY field of
EVERY row against hand-locked literals.

THE GUARD (DEC-SWEEP-RESULTS-DEPTH-001 / sweep-gap-register-jul8 ruling 5):
any future ResultRow extension MUST keep this suite green UNMODIFIED — i.e.
the extended reader must load this pre-extension file with each new field
defaulting to an honest ``None`` (presence-tolerant reads, the
WindowLedgerRecord precedent) — never a raw ``KeyError``, never a
:class:`ResultsStoreSchemaError` "corrupt" verdict that would quarantine-redo
the whole pre-extension history (R6/O-o).

Provenance: generated once on 2026-07-07 from the three literal rows mirrored
in :func:`_expected_rows` below (flush is byte-deterministic; sha256 at
generation time ``189af46640220f55879179f7bed38592bebd12d96a4922172d0d34f3a9a8c2eb``,
NOT asserted — the pin is LOGICAL field values, not version-fragile Parquet
bytes, the house golden philosophy). Do NOT regenerate after a schema
extension lands — a regenerated file would carry the new columns and the
guard would be testing nothing.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import ResultRow, ResultsStore

#: The committed pre-extension store file (a REAL flush output, frozen).
_FIXTURE: Path = Path(__file__).parent / "fixtures" / "results_store_pre_extension_v1.parquet"


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _expected_rows() -> tuple[ResultRow, ...]:
    """The exact literals the fixture was generated from (see module docstring).

    Constructing them re-derives ``condition_names`` and re-snaps the prices
    through the SAME single-source-of-truth code paths, so the comparison
    below is field-for-field against canonical values.
    """
    return (
        ResultRow(
            run_id=_rid(1),
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            mode="A",
            mask_bits=1,
            net_pnl_paisa=-123_450,
            gross_profit_paisa=250_000,
            gross_loss_paisa=373_450,
            max_drawdown_paisa=200_000,
            expectancy_paisa=-3_086,
            total_trades=40,
            winning_trades=18,
            losing_trades=22,
            sharpe_ratio=0.5217391304347826,
            sortino_ratio=0.75,
            calmar_ratio=0.6,
            profit_factor=0.6694604901594318,
            win_rate=0.45,
            max_drawdown_pct=12.5,
            avg_entry_price=Decimal("14020.7999"),  # snaps → 14020.80
            avg_exit_price=Decimal("14100.005"),  # snaps → 14100.01
            deflated_sharpe=None,
        ),
        ResultRow(
            run_id=_rid(2),
            instrument="BANKNIFTY",
            tf="15m",
            direction="bear",
            mode="B",
            mask_bits=(1 << 59) | (1 << 3),  # the TOP defined bit + bit 3
            net_pnl_paisa=987_654_321,
            gross_profit_paisa=1_000_000_000,
            gross_loss_paisa=12_345_679,
            max_drawdown_paisa=5_000_000,
            expectancy_paisa=24_691_358,
            total_trades=40,
            winning_trades=25,
            losing_trades=15,
            sharpe_ratio=3.25,
            sortino_ratio=4.5,
            calmar_ratio=2.75,
            profit_factor=81.0,
            win_rate=0.625,
            max_drawdown_pct=3.7,
            avg_entry_price=Decimal("51999.95"),
            avg_exit_price=Decimal("52001.00"),
            deflated_sharpe=1.2345678901234567,  # full precision, never rounded
        ),
        ResultRow(
            run_id=_rid(3),
            instrument="SENSEX",
            tf="1day",
            direction="bull",
            mode="A",
            mask_bits=0,  # the no-condition mask → empty condition_names
            net_pnl_paisa=0,
            gross_profit_paisa=0,
            gross_loss_paisa=0,
            max_drawdown_paisa=0,
            expectancy_paisa=0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            profit_factor=0.0,
            win_rate=0.0,
            max_drawdown_pct=0.0,
            avg_entry_price=Decimal("0"),  # snaps → 0.00
            avg_exit_price=Decimal("81000.5"),  # snaps → 81000.50
            deflated_sharpe=0.25,
        ),
    )


def test_fixture_is_committed() -> None:
    """The frozen pre-extension store ships with the repo (no polars needed)."""
    assert _FIXTURE.is_file()
    assert _FIXTURE.stat().st_size > 0


def test_pre_extension_store_loads_field_for_field() -> None:
    """Every field of every row round-trips to the hand-locked literal value."""
    pytest.importorskip("polars")  # load needs the [data] extra
    loaded = ResultsStore.load(_FIXTURE)
    expected = _expected_rows()
    assert len(loaded) == len(expected) == 3
    for exp in expected:
        got = loaded.get(exp.run_id)
        assert got is not None
        for f in fields(ResultRow):  # field-for-field, including derived fields
            assert getattr(got, f.name) == getattr(exp, f.name), f.name
        assert got == exp  # and the frozen-dataclass equality as a belt


def test_pre_extension_locked_literals() -> None:
    """Legibility pins on the load-boundary behaviours the fixture exercises."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(_FIXTURE)
    row_1, row_2, row_3 = (loaded.get(_rid(i)) for i in (1, 2, 3))
    assert row_1 is not None and row_2 is not None and row_3 is not None

    # 2dp ROUND_HALF_UP price snap survived the Parquet round-trip exactly.
    assert (row_1.avg_entry_price, row_1.avg_exit_price) == (
        Decimal("14020.80"),
        Decimal("14100.01"),
    )
    assert (row_3.avg_entry_price, row_3.avg_exit_price) == (
        Decimal("0.00"),
        Decimal("81000.50"),
    )
    # condition_names re-derived from mask_bits (single source of truth).
    assert row_1.condition_names == "close_above_ema20"
    assert row_2.condition_names == "close_below_ema200 AND choch_bearish"
    assert row_3.condition_names == ""
    # Nullable optional column: stored null → None; stored value → full precision.
    assert row_1.deflated_sharpe is None
    assert row_2.deflated_sharpe == 1.2345678901234567
    assert row_3.deflated_sharpe == 0.25


def test_pre_extension_rows_are_run_id_sorted() -> None:
    """`rows()` order (the deterministic read order) is stable for the fixture."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(_FIXTURE)
    assert [r.run_id for r in loaded.rows()] == [_rid(1), _rid(2), _rid(3)]
