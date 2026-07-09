"""FROZEN capture-ON store golden (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, G5).

``tests/golden/fixtures/results_store_temporal_v1.parquet`` is a small REAL
store written by the temporal-columns :meth:`ResultsStore.flush` — a frozen
snapshot of the EXTENDED schema (the four ``stats_by_*_json`` /
``temporal_judged_years_json`` Utf8 columns emitted, one populated row + one
all-``None`` row so the mixed-store null arm is frozen too). This suite loads
it and asserts every temporal field against hand-locked literals — the
sibling of ``test_results_store_pre_extension_golden.py`` on the OTHER side of
the extension boundary.

Provenance: generated once on 2026-07-09 from the literals mirrored in
:func:`_expected_rows` (flush is byte-deterministic; sha256 at generation time
``ac6a5f3cf535cd3417afb01c6aa14eed80a89e45071d42132edc8bcfa63aa1b7``, NOT
asserted — the pin is LOGICAL field values, the house golden philosophy). Do
NOT regenerate: a future column extension must load THIS file with its new
fields as honest ``None`` (the optional-column LAW), exactly as the
pre-extension fixture guards this one.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import ResultRow, ResultsStore

#: The committed capture-ON store file (a REAL flush output, frozen).
_FIXTURE: Path = Path(__file__).parent / "fixtures" / "results_store_temporal_v1.parquet"

#: The exact canonical column strings row 1 was generated with (hand-locked).
_YEAR_JSON = '{"2021":[27,-140000,13,200000],"2022":[13,16550,5,40000]}'
_QUARTER_JSON = (
    '{"2021Q1":[15,20000,8,45000],"2021Q2":[12,-160000,5,180000],"2022Q4":[13,16550,5,40000]}'
)
_MONTH_JSON = (
    '{"2021-01":[10,50000,6,20000],"2021-02":[5,-30000,2,35000],'
    '"2021-04":[12,-160000,5,180000],"2022-11":[13,16550,5,40000]}'
)
_JUDGED_JSON = '["2020","2021","2022"]'


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _expected_rows() -> tuple[ResultRow, ...]:
    """The exact literals the fixture was generated from (module docstring)."""
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
            avg_entry_price=Decimal("14020.80"),
            avg_exit_price=Decimal("14100.01"),
            deflated_sharpe=None,
            stats_by_year_json=_YEAR_JSON,
            stats_by_quarter_json=_QUARTER_JSON,
            stats_by_month_json=_MONTH_JSON,
            temporal_judged_years_json=_JUDGED_JSON,
        ),
        ResultRow(
            run_id=_rid(2),
            instrument="BANKNIFTY",
            tf="15m",
            direction="bear",
            mode="B",
            mask_bits=(1 << 59) | (1 << 3),
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
            deflated_sharpe=1.2345678901234567,
            # deliberately NO temporal columns — the frozen mixed-store null arm
        ),
    )


def test_fixture_is_committed() -> None:
    assert _FIXTURE.is_file()
    assert _FIXTURE.stat().st_size > 0


def test_temporal_store_loads_field_for_field() -> None:
    pytest.importorskip("polars")
    loaded = ResultsStore.load(_FIXTURE)
    expected = _expected_rows()
    assert len(loaded) == len(expected) == 2
    for exp in expected:
        got = loaded.get(exp.run_id)
        assert got is not None
        for f in fields(ResultRow):
            assert getattr(got, f.name) == getattr(exp, f.name), f.name
        assert got == exp


def test_temporal_locked_literals() -> None:
    """Legibility pins on the exact canonical column strings + the null arm."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(_FIXTURE)
    populated = loaded.get(_rid(1))
    null_arm = loaded.get(_rid(2))
    assert populated is not None and null_arm is not None
    assert populated.stats_by_year_json == _YEAR_JSON
    assert populated.stats_by_quarter_json == _QUARTER_JSON
    assert populated.stats_by_month_json == _MONTH_JSON
    assert populated.temporal_judged_years_json == _JUDGED_JSON
    # The mixed-store null arm: stored Parquet nulls → honest None, frozen.
    assert null_arm.stats_by_year_json is None
    assert null_arm.stats_by_quarter_json is None
    assert null_arm.stats_by_month_json is None
    assert null_arm.temporal_judged_years_json is None


def test_temporal_flush_reproduces_the_fixture_bytes(tmp_path: Path) -> None:
    """Byte-determinism across the extension: re-flushing the literals lands on
    the committed bytes (polars/zstd-build sensitive — the same caveat class as
    the pre-extension fixture's generation note; the field pins above are the
    version-robust contract)."""
    pytest.importorskip("polars")
    store = ResultsStore(path=tmp_path / "regen.parquet")
    for row in _expected_rows():
        store.append(row)
    store.flush()
    assert (tmp_path / "regen.parquet").read_bytes() == _FIXTURE.read_bytes()
