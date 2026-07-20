"""FROZEN capture-ON store golden (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, G5).

``tests/golden/fixtures/results_store_temporal_v1.parquet`` is a small REAL
store written by the temporal-columns :meth:`ResultsStore.flush` — a frozen
snapshot of the EXTENDED schema (the four ``stats_by_*_json`` /
``temporal_judged_years_json`` Utf8 columns emitted, one populated row + one
all-``None`` row so the mixed-store null arm is frozen too). This suite loads
it and asserts every temporal field against hand-locked literals — the
sibling of ``test_results_store_pre_extension_golden.py`` on the OTHER side of
the extension boundary.

Provenance: generated 2026-07-09 from the literals mirrored in
:func:`_expected_rows`; REGENERATED 2026-07-15 at the club S2 merge fold —
main's REFERENCE-ONLY VIX trio (DEC-VIX-STAMP-WIRE-001, three always-emitted
nullable Float64 columns) landed on main before this branch merged, so the
pre-merge fixture's schema was stale and the branch-introduced fixture was
regenerated through the MERGED writer (the house branch-introduced-goldens-
regenerated-at-merge-reconcile precedent; origin/main goldens byte-untouched).
Flush is byte-deterministic; sha256 at regeneration time
``f9c94617799ab02913db02750b68c157c666097c80985a421e7935af0bd1b7fd``, NOT
asserted — the pin is LOGICAL field values, the house golden philosophy). Do
NOT regenerate again: a future column extension must load THIS file with its
new fields as honest ``None`` (the optional-column LAW), exactly as the
pre-extension fixture guards this one.

W2 RE-INTEGRATION (2026-07-19, the #1580 collision fold — MIGRATION.md #1976;
the sanctioned additive-only re-lock, the #1749 precedent): the W2
metrics-depth lane's 8 ALWAYS-written nullable Float64 columns (the growth
trio + ``sl_hit_rate`` + the recency four — appended strictly AFTER the
temporal quartet's landed position, the coordinator's landed-first-wins
ruling) re-appended to the writer, so a reflush of :func:`_expected_rows`
no longer lands on the v1 bytes. Per the do-NOT-regenerate law above the v1
file is BYTE-UNTOUCHED and stays the frozen LOAD oracle — it now genuinely
guards the optional-column law for W2's 8 (absent columns load as honest
``None``, pinned below) — and the byte-determinism pin moved onto the NEW
sibling ``results_store_temporal_v2.parquet``, minted 2026-07-19 as a
reviewed act through the COMBINED production writer via the documented
generation law (append :func:`_expected_rows` → ``flush()`` — the law the
reflush test enforces forever; sha256
``e3d6d02aa109dfbdd15c2b7d9a94225f08883c8117f8f362dc29ddd718c1b9eb``, 10 512
bytes, NOT asserted). The v1→v2 delta is HAND-AUDITED schema-additive-only
(and permanently machine-pinned by ``test_v2_delta_vs_v1_is_exactly_the_
appended_w2_families`` below): v2's columns == v1's 32 columns in the SAME
order + EXACTLY the 8 (all Float64, ALL-null — no W2 value exists for the
hand-built literal rows), and ``v2.drop(the 8)`` is schema- AND value-equal
to v1. The sibling ``results_store_pre_temporal_v2.parquet`` (SAME
regeneration event; sha256
``9a3122e19204602a8b35ea478b0aca69fe7baa697e245330a709c43bd69b1ad7``, 9 259
bytes; v1 frozen at
``8852565285aa69979fcd3c946cc003aa3d24a57d76d2242228e4c890ec56d312``) is the
new byte oracle for the emit-on-present all-``None`` arm in
``tests/unit/test_engine_results_store_temporal.py``, delta-audited by the
same pin below.

WAVE-2B RE-INTEGRATION (2026-07-20, the train-b rank-pareto fold —
MIGRATION.md #2059/#2060; the SAME sanctioned additive-only re-lock class):
the LANE-W1 selection-provenance pair (``pareto_member`` Boolean +
``selection_basis`` Utf8 — ALWAYS-written nullable columns, appended
strictly LAST after the W2 families) joined the writer, so a reflush of
:func:`_expected_rows` no longer lands on the v2 bytes. Per the
do-NOT-regenerate law the v2 files are BYTE-UNTOUCHED and stay frozen LOAD
oracles — they now genuinely guard the optional-column law for the
selection pair (absent columns load as honest ``None``) — and the
byte-determinism pin moved onto the NEW sibling
``results_store_temporal_v3.parquet``, minted 2026-07-20 as a reviewed act
through the COMBINED production writer via the SAME documented generation
law (sha256
``c3ad093bb35244bfddfe26c0850592bf713e99b339ea372a9acef739152b08a8``,
10 983 bytes, NOT asserted). The v2→v3 delta is machine-pinned
additive-only below: v3's columns == v2's in the SAME order + EXACTLY the
2 selection columns (``pareto_member`` Boolean + ``selection_basis`` Utf8,
ALL-null — the hand-built literal rows carry no selection provenance), and
``v3.drop(the 2)`` is schema- AND value-equal to v2. The sibling
``results_store_pre_temporal_v3.parquet`` (SAME regeneration event; sha256
``c0b2ed156cde1b074f8b85a6d69573343fac1182021f430dfba9495ab52d9382``,
9 726 bytes) is the new byte oracle for the emit-on-present all-``None``
arm in the unit suite, delta-audited by the same pin.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import ResultRow, ResultsStore

#: The committed capture-ON store file (a REAL flush output, frozen).
_FIXTURE: Path = Path(__file__).parent / "fixtures" / "results_store_temporal_v1.parquet"

#: The W2-era byte-determinism twin (module docstring): the SAME literals
#: flushed through the COMBINED writer (temporal quartet at its landed
#: position + W2's 8 appended after, all-null here).
_FIXTURE_V2: Path = Path(__file__).parent / "fixtures" / "results_store_temporal_v2.parquet"

#: The all-``None`` arm's v1/v2 pair (the unit suite's byte oracle) —
#: delta-audited here beside the temporal pair.
_PRE_TEMPORAL_V1: Path = (
    Path(__file__).parent / "fixtures" / "results_store_pre_temporal_v1.parquet"
)
_PRE_TEMPORAL_V2: Path = (
    Path(__file__).parent / "fixtures" / "results_store_pre_temporal_v2.parquet"
)

#: The wave-2b byte-determinism twins (module docstring): the SAME literals
#: flushed through the COMBINED writer with the LANE-W1 selection pair
#: appended LAST (all-null here) — MIGRATION.md #2059/#2060.
_FIXTURE_V3: Path = Path(__file__).parent / "fixtures" / "results_store_temporal_v3.parquet"
_PRE_TEMPORAL_V3: Path = (
    Path(__file__).parent / "fixtures" / "results_store_pre_temporal_v3.parquet"
)

#: The 8 W2 always-written nullable Float64 columns, in their ruled appended
#: order (GROWTH_FIELDS + EXIT_QUALITY_FIELDS + RECENCY_FIELDS — mirrored
#: literally so a constant reorder cannot silently re-derive this pin).
_W2_COLUMNS: list[str] = [
    "loss_rate",
    "kelly_fraction",
    "log_growth_at_half_kelly",
    "sl_hit_rate",
    "trailing_sharpe_ratio",
    "trailing_profit_factor",
    "trailing_net_pnl_paisa",
    "decay_weighted_net_paisa",
]

#: The LANE-W1 selection-provenance pair (DEC-RANK-PARETO-PERSIST-001 /
#: DEC-RANK-QUOTA-PERSIST-1000-001, MIGRATION.md #2059/#2060) —
#: ALWAYS-written nullable columns appended strictly LAST after the W2
#: families at the 2026-07-20 wave-2b fold (mirrored literally, as above).
_SELECTION_COLUMNS: list[str] = [
    "pareto_member",
    "selection_basis",
]

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
    assert _FIXTURE_V2.is_file()
    assert _FIXTURE_V2.stat().st_size > 0


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


def test_v1_loads_w2_fields_as_honest_none() -> None:
    """The optional-column LAW, now live for W2's 8 on the frozen v1 file: a
    store written BEFORE the growth/exit-quality/recency columns loads each of
    them as honest ``None`` — never a fabricated value, never a corrupt
    verdict (the same guard the pre-extension fixture provides one generation
    earlier)."""
    pytest.importorskip("polars")
    loaded = ResultsStore.load(_FIXTURE)
    assert len(loaded) == 2
    for row in loaded.rows():
        for name in _W2_COLUMNS:
            assert getattr(row, name) is None, name


def test_temporal_flush_reproduces_the_fixture_bytes(tmp_path: Path) -> None:
    """Byte-determinism across the extension: re-flushing the literals lands on
    the committed V2 bytes (polars/zstd-build sensitive — the same caveat class
    as the pre-extension fixture's generation note; the field pins above are
    the version-robust contract). Repointed v1 → v2 at the W2 re-integration
    (module docstring — the combined writer always appends W2's 8, so the v1
    bytes are unreproducible by construction; v1 stays the frozen LOAD
    oracle, byte-untouched) and v2 → v3 at the 2026-07-20 wave-2b
    rank-pareto fold (the writer now also always appends the selection pair;
    v2 stays a frozen LOAD oracle, byte-untouched)."""
    pytest.importorskip("polars")
    store = ResultsStore(path=tmp_path / "regen.parquet")
    for row in _expected_rows():
        store.append(row)
    store.flush()
    assert (tmp_path / "regen.parquet").read_bytes() == _FIXTURE_V3.read_bytes()


def test_v2_delta_vs_v1_is_exactly_the_appended_w2_families() -> None:
    """The PERMANENT machine form of the 2026-07-19 regeneration hand audit
    (module docstring; the #1749 additive-only re-lock law), for BOTH v1/v2
    fixture pairs — the temporal pair here and the unit suite's pre-temporal
    byte-oracle pair: v2's schema is v1's schema + EXACTLY the 8 W2 nullable
    Float64 columns appended LAST (all-null — the hand-built literal rows
    carry no W2 values), and dropping them yields a frame schema- AND
    value-equal to v1 (every old column byte/value-equal in the same order —
    ``.equals`` alone is dtype-insensitive, hence the explicit schema
    compare)."""
    pl = pytest.importorskip("polars")
    for v1_path, v2_path in ((_FIXTURE, _FIXTURE_V2), (_PRE_TEMPORAL_V1, _PRE_TEMPORAL_V2)):
        v1 = pl.read_parquet(v1_path)
        v2 = pl.read_parquet(v2_path)
        assert v2.columns == v1.columns + _W2_COLUMNS, v2_path.name
        for name in _W2_COLUMNS:
            assert v2.schema[name] == pl.Float64, name
            assert v2[name].null_count() == v2.height, name
        stripped = v2.drop(_W2_COLUMNS)
        assert stripped.schema == v1.schema, v2_path.name
        assert stripped.equals(v1), v2_path.name


def test_v3_delta_vs_v2_is_exactly_the_appended_selection_pair() -> None:
    """The PERMANENT machine form of the 2026-07-20 wave-2b regeneration
    audit (module docstring; the same #1749 additive-only re-lock law), for
    BOTH v2/v3 fixture pairs: v3's schema is v2's schema + EXACTLY the 2
    LANE-W1 selection columns appended LAST (``pareto_member`` Boolean +
    ``selection_basis`` Utf8, all-null — the hand-built literal rows carry
    no selection provenance), and dropping them yields a frame schema- AND
    value-equal to v2 (the explicit schema compare beside ``.equals``, as
    above)."""
    pl = pytest.importorskip("polars")
    for v2_path, v3_path in ((_FIXTURE_V2, _FIXTURE_V3), (_PRE_TEMPORAL_V2, _PRE_TEMPORAL_V3)):
        v2 = pl.read_parquet(v2_path)
        v3 = pl.read_parquet(v3_path)
        assert v3.columns == v2.columns + _SELECTION_COLUMNS, v3_path.name
        assert v3.schema["pareto_member"] == pl.Boolean
        assert v3.schema["selection_basis"] == pl.Utf8
        for name in _SELECTION_COLUMNS:
            assert v3[name].null_count() == v3.height, name
        stripped = v3.drop(_SELECTION_COLUMNS)
        assert stripped.schema == v2.schema, v3_path.name
        assert stripped.equals(v2), v3_path.name
