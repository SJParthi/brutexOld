"""The four optional ResultRow temporal columns (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

Storage-side laws pinned here:

* **Emit-on-present writer** — an all-``None`` store flushes with NO temporal
  column (proven byte-identical against the COMMITTED
  ``results_store_pre_temporal_v2.parquet`` fixture, which today's flush
  reproduces exactly — the same polars/zstd-build sensitivity caveat as the
  ``_GOLDEN_CONTENT_HASH`` pins, backed by the version-robust schema check;
  the W2 re-integration's 8 always-written nullable columns are part of the
  all-``None`` schema by the ruled combined order, MIGRATION.md #1976).
* **All-or-none per row** — a partial temporal set is a torn capture, rejected.
* **Cross-sum validation** at construction + the G14 typed wrap at load.
* **The D16 append conflict/enrichment rule** — all three cases.
* **Round-trip** — populated + ``None`` cells survive flush→load exactly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.period_stats import encode_period_stats
from brutex.engine.results_store import (
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    ResultsStoreSchemaError,
)
from structlog.testing import capture_logs

pytest.importorskip("polars")  # flush/load need the [data] extra

#: The committed PRE-extension store (see its golden suite) — the byte oracle
#: for the all-None emit-on-present arm.
_PRE_EXTENSION_FIXTURE: Path = (
    Path(__file__).parent.parent / "golden" / "fixtures" / "results_store_pre_extension_v1.parquet"
)
#: The byte oracle for the emit-on-present law: the COMBINED writer's output
#: for the SAME pre-extension literals with NO temporal values (the VIX trio
#: + W2's 8 growth/exit-quality/recency columns present as nulls). Repointed
#: v1 → v2 at the 2026-07-19 W2 re-integration (MIGRATION.md #1976; the v1→v2
#: delta is hand-audited + machine-pinned additive-only — EXACTLY the 8
#: appended W2 columns — in tests/golden/test_results_store_temporal_golden.py,
#: which also documents the generation law: append the pre-extension golden's
#: literals → flush through the production writer).
_PRE_TEMPORAL_FIXTURE: Path = (
    Path(__file__).resolve().parents[1]
    / "golden"
    / "fixtures"
    / "results_store_pre_temporal_v2.parquet"
)

#: The pre-extension column set (identity → limbs → money → counts → metrics →
#: deflated → prices → the VIX trio) — the temporal-free PREFIX of every
#: flush; an all-None flush emits exactly this + the always-written
#: ``_W2_COLUMNS`` tail (the ruled combined order — see the class below).
_PRE_EXTENSION_COLUMNS = [
    "run_id",
    "instrument",
    "tf",
    "direction",
    "mode",
    "condition_names",
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
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "max_drawdown_pct",
    "deflated_sharpe",
    "avg_entry_price",
    "avg_exit_price",
    # The REFERENCE-ONLY VIX trio (DEC-VIX-STAMP-WIRE-001) landed on main
    # before this branch merged (club S2 fold): the writer emits these three
    # nullable Float64 columns unconditionally, so they are part of the
    # PRE-TEMPORAL column set the emit-on-present law is judged against.
    "vix_mean",
    "vix_min",
    "vix_max",
]

_TEMPORAL_COLUMNS = [
    "stats_by_year_json",
    "stats_by_quarter_json",
    "stats_by_month_json",
    "temporal_judged_years_json",
]

#: The W2 metrics-depth families (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
#: DEC-METRICS-KELLY-GROWTH-001 / DEC-METRICS-TRAILING-WINDOW-001) —
#: ALWAYS-written nullable Float64 columns, re-appended strictly AFTER the
#: temporal quartet's landed position at the 2026-07-19 re-integration (the
#: coordinator's landed-first-wins ruling: the #1580 lane landed on main
#: first, so its emit-on-present quartet keeps its landed position after the
#: VIX trio and W2's 8 follow; MIGRATION.md #1976).
_W2_COLUMNS = [
    "loss_rate",
    "kelly_fraction",
    "log_growth_at_half_kelly",
    "sl_hit_rate",
    "trailing_sharpe_ratio",
    "trailing_profit_factor",
    "trailing_net_pnl_paisa",
    "decay_weighted_net_paisa",
]

# ── A consistent temporal bucket set for a 40-trade / −123450-paisa / 18-win row ──
_MONTHS = {
    "2021-01": (10, 50_000, 6, 20_000),
    "2021-02": (5, -30_000, 2, 35_000),
    "2021-04": (12, -160_000, 5, 180_000),
    "2022-11": (13, 16_550, 5, 40_000),
}
_QUARTERS = {
    "2021Q1": (15, 20_000, 8, 45_000),
    "2021Q2": (12, -160_000, 5, 180_000),
    "2022Q4": (13, 16_550, 5, 40_000),
}
_YEARS = {
    "2021": (27, -140_000, 13, 200_000),
    "2022": (13, 16_550, 5, 40_000),
}
_JUDGED = '["2020","2021","2022"]'


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(seed: int = 1, *, with_temporal: bool = False, **overrides: object) -> ResultRow:
    kwargs: dict[str, object] = {
        "run_id": _rid(seed),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 1,
        "net_pnl_paisa": -123_450,
        "gross_profit_paisa": 250_000,
        "gross_loss_paisa": 373_450,
        "max_drawdown_paisa": 200_000,
        "expectancy_paisa": -3_086,
        "total_trades": 40,
        "winning_trades": 18,
        "losing_trades": 22,
        "sharpe_ratio": 0.5,
        "sortino_ratio": 0.75,
        "calmar_ratio": 0.6,
        "profit_factor": 1.4,
        "win_rate": 0.45,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.80"),
        "avg_exit_price": Decimal("14100.01"),
    }
    if with_temporal:
        kwargs.update(
            stats_by_year_json=encode_period_stats(_YEARS),
            stats_by_quarter_json=encode_period_stats(_QUARTERS),
            stats_by_month_json=encode_period_stats(_MONTHS),
            temporal_judged_years_json=_JUDGED,
        )
    kwargs.update(overrides)
    return ResultRow(**kwargs)  # type: ignore[arg-type]


class TestEmitOnPresent:
    def test_all_none_flush_is_byte_identical_to_the_pre_temporal_fixture(
        self, tmp_path: Path
    ) -> None:
        """THE emit-on-present proof: flushing the pre-extension golden's exact
        literals through the EXTENDED writer reproduces the COMMITTED
        pre-TEMPORAL fixture bytes — the temporal extension is provably
        invisible on the all-None arm. Re-anchored at the club S2 fold: main's
        VIX trio (DEC-VIX-STAMP-WIRE-001, always-emitted nullable columns)
        landed before this branch merged, so the byte oracle became the merged
        writer's output WITHOUT temporal values (vix columns present as
        nulls), not the historical vix-less
        ``results_store_pre_extension_v1.parquet`` (which stays the frozen
        LOAD oracle in its own golden suite, byte-untouched). Re-anchored
        AGAIN at the 2026-07-19 W2 re-integration (MIGRATION.md #1976): the
        combined writer additionally always emits W2's 8 nullable columns, so
        the oracle is ``results_store_pre_temporal_v2.parquet`` (the SAME
        literals, W2's 8 as nulls; the v1→v2 delta hand-audited +
        machine-pinned additive-only in the golden suite; v1 frozen
        byte-untouched). The pin's INTENT is unchanged: temporal
        emit-on-present adds NOTHING when absent. (Byte-level, so
        polars/zstd-build sensitive like _GOLDEN_CONTENT_HASH; the schema
        test below is the version-robust twin.)"""
        from tests.golden.test_results_store_pre_extension_golden import _expected_rows

        store = ResultsStore(path=tmp_path / "a.parquet")
        for row in _expected_rows():
            store.append(row)
        store.flush()
        assert (tmp_path / "a.parquet").read_bytes() == _PRE_TEMPORAL_FIXTURE.read_bytes()

    def test_all_none_flush_emits_exactly_the_pre_extension_columns(self, tmp_path: Path) -> None:
        """The version-robust schema twin of the byte test: an all-None
        temporal flush emits EXACTLY the pre-extension set + W2's
        always-written 8 — and NO temporal column (the emit-on-present law's
        intent: the quartet adds nothing when absent). The W2 tail joined the
        expectation at the 2026-07-19 re-integration — W2's 8 are
        ALWAYS-written nullable columns (the ruled combined order), never
        emit-on-present."""
        import polars as pl

        store = ResultsStore(path=tmp_path / "a.parquet")
        store.append(_row(1))
        store.flush()
        assert pl.read_parquet(tmp_path / "a.parquet").columns == (
            _PRE_EXTENSION_COLUMNS + _W2_COLUMNS
        )

    def test_temporal_flush_emits_the_quartet_at_its_landed_position(self, tmp_path: Path) -> None:
        """The RULED combined column order (the 2026-07-19 re-integration
        ruling, MIGRATION.md #1976): the landed #1580 lane's temporal quartet
        keeps its LANDED position — strictly after the VIX trio — and W2's 8
        always-written columns follow strictly AFTER it (landed-first-wins).
        Supersedes the landed pin that the quartet is literally LAST — false
        by ruling once W2's families re-appended."""
        import polars as pl

        store = ResultsStore(path=tmp_path / "a.parquet")
        store.append(_row(1, with_temporal=True))
        store.flush()
        df = pl.read_parquet(tmp_path / "a.parquet")
        assert df.columns == _PRE_EXTENSION_COLUMNS + _TEMPORAL_COLUMNS + _W2_COLUMNS
        assert all(df.schema[name] == pl.Utf8 for name in _TEMPORAL_COLUMNS)
        assert all(df.schema[name] == pl.Float64 for name in _W2_COLUMNS)

    def test_temporal_flush_is_deterministic(self, tmp_path: Path) -> None:
        for tag in ("a", "b"):
            store = ResultsStore(path=tmp_path / f"{tag}.parquet")
            store.append(_row(1, with_temporal=True))
            store.append(_row(2))
            store.flush()
        assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


class TestRoundTrip:
    def test_populated_and_none_rows_round_trip_exactly(self, tmp_path: Path) -> None:
        """A MIXED store: nulls stay None, populated cells stay byte-exact."""
        store = ResultsStore(path=tmp_path / "a.parquet")
        with_stats = _row(1, with_temporal=True)
        without = _row(2)
        store.append(with_stats)
        store.append(without)
        store.flush()
        loaded = ResultsStore.load(tmp_path / "a.parquet")
        got_with = loaded.get(_rid(1))
        got_without = loaded.get(_rid(2))
        assert got_with == with_stats
        assert got_without == without
        assert got_with is not None and got_without is not None
        assert got_with.stats_by_year_json == encode_period_stats(_YEARS)
        assert got_without.stats_by_year_json is None
        assert got_without.temporal_judged_years_json is None

    def test_pre_extension_file_loads_none(self) -> None:
        """A store written BEFORE the extension loads honest None — never a
        fabricated value, never a 'corrupt' verdict (the optional-column LAW)."""
        loaded = ResultsStore.load(_PRE_EXTENSION_FIXTURE)
        for row in loaded.rows():
            assert row.stats_by_year_json is None
            assert row.stats_by_quarter_json is None
            assert row.stats_by_month_json is None
            assert row.temporal_judged_years_json is None


class TestConstructionValidation:
    def test_partial_temporal_set_is_a_torn_capture(self) -> None:
        with pytest.raises(ResultsStoreError, match="all-present or all-None"):
            _row(1, with_temporal=True, temporal_judged_years_json=None)

    def test_single_present_field_is_a_torn_capture(self) -> None:
        with pytest.raises(ResultsStoreError, match="all-present or all-None"):
            _row(1, stats_by_year_json="{}")

    def test_cross_sum_violation_rejected(self) -> None:
        tampered = dict(_YEARS)
        tampered["2021"] = (27, -139_999, 13, 200_000)
        with pytest.raises(ResultsStoreError, match="temporal stats invalid"):
            _row(1, with_temporal=True, stats_by_year_json=encode_period_stats(tampered))

    def test_totals_mismatch_rejected(self) -> None:
        """The buckets must sum to THIS row's totals — fabrication impossible."""
        with pytest.raises(ResultsStoreError, match="temporal stats invalid"):
            _row(
                1,
                with_temporal=True,
                total_trades=41,
                winning_trades=18,
                losing_trades=22,
            )

    def test_non_canonical_spelling_is_canonicalised_at_construction(self) -> None:
        loose = '{ "2022": [13, 16550, 5, 40000], "2021": [27, -140000, 13, 200000] }'
        row = _row(1, with_temporal=True, stats_by_year_json=loose)
        assert row.stats_by_year_json == encode_period_stats(_YEARS)

    def test_zero_trade_row_with_empty_maps_is_legal(self) -> None:
        row = _row(
            1,
            with_temporal=True,
            stats_by_year_json="{}",
            stats_by_quarter_json="{}",
            stats_by_month_json="{}",
            temporal_judged_years_json="[]",
            net_pnl_paisa=0,
            gross_profit_paisa=0,
            gross_loss_paisa=0,
            expectancy_paisa=0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
        )
        assert row.stats_by_year_json == "{}"


class TestLoadDamageWrap:
    def test_corrupt_temporal_json_raises_typed_schema_error_naming_path(
        self, tmp_path: Path
    ) -> None:
        """G14: a tampered stored cell surfaces as ResultsStoreSchemaError
        naming the store path — never a raw ValueError/KeyError."""
        import polars as pl

        good = tmp_path / "good.parquet"
        store = ResultsStore(path=good)
        store.append(_row(1, with_temporal=True))
        store.flush()
        bad = tmp_path / "bad.parquet"
        pl.read_parquet(good).with_columns(
            pl.lit('{"2021":[9,9,9,9]}', dtype=pl.Utf8).alias("stats_by_year_json")
        ).write_parquet(bad)
        with pytest.raises(ResultsStoreSchemaError, match=str(bad)):
            ResultsStore.load(bad)

    def test_nesting_bomb_temporal_cell_raises_typed_schema_error(self, tmp_path: Path) -> None:
        """Review round 1 (G14): a pathologically nested valid-JSON temporal
        cell (`'['*60000` — a nesting bomb) previously ESCAPED load as a raw
        `RecursionError` (not a `ValueError`), defeating the typed-damage law
        and every downstream skip-and-count reader. Now surfaces as
        `ResultsStoreSchemaError` naming the path like every other damage
        shape."""
        import polars as pl

        good = tmp_path / "good.parquet"
        store = ResultsStore(path=good)
        store.append(_row(1, with_temporal=True))
        store.flush()
        bad = tmp_path / "bomb.parquet"
        pl.read_parquet(good).with_columns(
            pl.lit("[" * 60_000, dtype=pl.Utf8).alias("stats_by_year_json")
        ).write_parquet(bad)
        with pytest.raises(ResultsStoreSchemaError, match=str(bad)):
            ResultsStore.load(bad)


class TestAppendEnrichmentRule:
    """The D16 conflict law — all three cases + the reverse direction."""

    def test_none_vs_populated_enriches_in_place_and_logs(self) -> None:
        store = ResultsStore(path=Path("/unused.parquet"))
        appended = store.append(_row(1))
        assert appended is True
        with capture_logs() as logs:
            enriched_in_place = store.append(_row(1, with_temporal=True))
            assert enriched_in_place is False
        events = [e for e in logs if e["event"] == "results_store.temporal_enriched"]
        assert len(events) == 1
        assert events[0]["run_id"] == _rid(1)
        assert tuple(events[0]["enriched_fields"]) == tuple(_TEMPORAL_COLUMNS)
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored.stats_by_year_json == encode_period_stats(_YEARS)
        assert len(store) == 1

    def test_populated_vs_none_keeps_the_populated_row_silently(self) -> None:
        store = ResultsStore(path=Path("/unused.parquet"))
        enriched = _row(1, with_temporal=True)
        store.append(enriched)
        with capture_logs() as logs:
            kept_silently = store.append(_row(1))
            assert kept_silently is False
        assert [e for e in logs if e["event"] == "results_store.temporal_enriched"] == []
        assert store.get(_rid(1)) == enriched

    def test_populated_vs_same_is_the_idempotent_no_op(self) -> None:
        store = ResultsStore(path=Path("/unused.parquet"))
        store.append(_row(1, with_temporal=True))
        idempotent = store.append(_row(1, with_temporal=True))
        assert idempotent is False
        assert len(store) == 1

    def test_populated_vs_different_is_a_reproducibility_breach(self) -> None:
        store = ResultsStore(path=Path("/unused.parquet"))
        other_years = dict(_YEARS)
        other_years["2021"] = (27, -140_000, 13, 111_111)  # dd differs — legal cell
        store.append(_row(1, with_temporal=True))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(
                _row(1, with_temporal=True, stats_by_year_json=encode_period_stats(other_years))
            )

    def test_non_temporal_difference_is_still_a_conflict(self) -> None:
        """The enrichment carve-out NEVER relaxes the required-field law."""
        store = ResultsStore(path=Path("/unused.parquet"))
        store.append(_row(1))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(_row(1, sharpe_ratio=9.9))

    def test_merge_helper_refuses_a_non_temporal_difference_directly(self) -> None:
        """The helper's OWN non-temporal equality precondition (its defensive
        first loop): ``append``'s pre-filter raises before ever reaching it
        with a non-sanctioned difference, so this arm is pinned DIRECTLY —
        a differing required field ⇒ ``None`` (conflict), never a merge.
        (Inherited fold-base coverage gap closed at the S2 reconcile.)"""
        from brutex.engine.results_store import _merge_temporal_enrichment

        merged = _merge_temporal_enrichment(_row(1), _row(1, sharpe_ratio=9.9))
        assert merged is None

    def test_non_temporal_difference_with_enrichment_shape_is_a_conflict(self) -> None:
        """None-vs-populated temporal + a differing required field: conflict —
        the merge is refused before any temporal comparison matters."""
        store = ResultsStore(path=Path("/unused.parquet"))
        store.append(_row(1))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(_row(1, with_temporal=True, sharpe_ratio=9.9))

    def test_enrichment_survives_a_flush_load_cycle(self, tmp_path: Path) -> None:
        """The capture-toggled-redo path end to end: old store on disk (no
        capture) → load_or_create-style load → re-append WITH capture →
        flush → the columns are present."""
        path = tmp_path / "a.parquet"
        first = ResultsStore(path=path)
        first.append(_row(1))
        first.flush()
        redo = ResultsStore.load(path)
        redo.append(_row(1, with_temporal=True))
        redo.flush()
        final = ResultsStore.load(path)
        stored = final.get(_rid(1))
        assert stored is not None
        assert stored.stats_by_month_json == encode_period_stats(_MONTHS)


class TestCombinedEnrichmentBackfillSeam:
    """R5 NIT-1 (MIGRATION.md #1976): ONE ``append`` carrying BOTH a temporal
    enrichment AND a W2 ``None`` → value backfill — the combined seam the two
    family suites each pin only half of (a pre→post-extension backfill +
    temporal enrichment in one redo). The COMBINED return law (the documented
    D16 + backfill contract on :meth:`ResultsStore.append`): ``True`` iff a
    BACKFILLABLE upgrade occurred — a temporal-ONLY enrichment reports
    ``False`` even though the stored bytes changed, so the mixed append must
    report ``True`` on its backfill half while still enriching in place."""

    def test_combined_temporal_enrichment_and_growth_backfill_in_one_append(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.parquet"
        store = ResultsStore(path=path)
        seeded = store.append(_row(1))
        assert seeded is True  # quartet + W2 fields all None
        with capture_logs() as logs:
            upgraded = store.append(_row(1, with_temporal=True, kelly_fraction=0.25))
        # (c) the combined return law: True — a backfillable upgrade occurred
        # (a temporal-only enrichment would have reported False per D16).
        assert upgraded is True
        assert len(store) == 1
        # (a) the temporal half enriched in place + logged (emit-on-present).
        enriched = [e for e in logs if e["event"] == "results_store.temporal_enriched"]
        assert len(enriched) == 1
        assert enriched[0]["run_id"] == _rid(1)
        assert tuple(enriched[0]["enriched_fields"]) == tuple(_TEMPORAL_COLUMNS)
        # (b) the W2 None → value backfill landed + logged per family.
        backfills = [e for e in logs if e["event"] == "results_store.growth_backfill"]
        assert len(backfills) == 1
        assert tuple(backfills[0]["backfilled_fields"]) == ("kelly_fraction",)
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored.stats_by_year_json == encode_period_stats(_YEARS)
        assert stored.kelly_fraction == 0.25
        # (d) a follow-up flush/load round-trips BOTH halves exactly.
        store.flush()
        loaded = ResultsStore.load(path)
        got = loaded.get(_rid(1))
        assert got is not None
        assert got == stored
        assert got.stats_by_month_json == encode_period_stats(_MONTHS)
        assert got.kelly_fraction == 0.25

    def test_growth_value_conflict_is_not_masked_by_a_temporal_enrichment(self) -> None:
        """The reviewer's traced edge — the SAME combined shape but the W2
        field differs value-vs-value: the per-family typed conflict (the
        growth-family plain :class:`ResultsStoreError`, never the VIX class)
        raises AND the co-riding temporal diff neither masks it nor lands
        partially — the stored row is byte-untouched (the merge is atomic:
        assignment happens only after every family's merge succeeds)."""
        store = ResultsStore(path=Path("/unused.parquet"))
        original = _row(1, kelly_fraction=0.25)
        store.append(original)
        with pytest.raises(ResultsStoreError, match="growth-metric conflict"):
            store.append(_row(1, with_temporal=True, kelly_fraction=0.5))
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored == original  # atomic: no partial temporal enrichment landed
        assert stored.stats_by_year_json is None
        assert stored.temporal_judged_years_json is None
        assert stored.kelly_fraction == 0.25
