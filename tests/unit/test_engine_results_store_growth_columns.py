"""Unit tests — the nullable loss/growth trio on :class:`ResultRow`.

DEC-METRICS-LOSSRATE-FIRSTCLASS-001 / DEC-METRICS-KELLY-GROWTH-001:
``loss_rate`` / ``kelly_fraction`` / ``log_growth_at_half_kelly``
(:data:`~brutex.engine.results_store.GROWTH_FIELDS`) land on
:class:`~brutex.engine.results_store.ResultRow` as the THIRD optional-column
generation (Ruling 5) — appended LAST after the VIX trio, presence-tolerant
read (an old store loads ``None``), the DIRECTIONAL ``None`` → value
backfill lane extended from VIX (value-vs-value on a growth field is the
PLAIN run-identity conflict — the trio is a pure function of the other
stored columns + the run's code), and the NEW nulls-last rankable law
(:data:`~brutex.engine.results_store._NULLABLE_RANKABLE_FIELDS` — a ``None``
ranks AFTER every real value in BOTH directions).

Companion: ``tests/unit/test_engine_results_store_vix_columns.py`` (the
first backfill generation; its appended-last schema pin now covers both
generations' order).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import (
    _NULLABLE_RANKABLE_FIELDS,
    _RANKABLE_FIELDS,
    EXIT_QUALITY_FIELDS,
    GROWTH_FIELDS,
    RECENCY_FIELDS,
    VIX_FIELDS,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
)
from structlog.testing import capture_logs


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(**overrides: object) -> ResultRow:
    base: dict[str, object] = {
        "run_id": _rid(1),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b101,
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 1_500,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
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
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


_TRIO = {"loss_rate": 0.375, "kelly_fraction": 0.21, "log_growth_at_half_kelly": 0.0135}


# ──────────────────────────────────────────────────────────────────────────
# ResultRow — construction + validation
# ──────────────────────────────────────────────────────────────────────────


class TestGrowthFieldValidation:
    def test_trio_defaults_to_none(self) -> None:
        row = _row()
        assert (row.loss_rate, row.kelly_fraction, row.log_growth_at_half_kelly) == (
            None,
            None,
            None,
        )

    def test_full_trio_round_trips_at_full_precision(self) -> None:
        row = _row(
            loss_rate=0.3333333333333333,
            kelly_fraction=0.123456789012345,
            log_growth_at_half_kelly=-0.007654321098765,
        )
        assert row.loss_rate == 0.3333333333333333  # never rounded (CLAUDE.md §4)
        assert row.kelly_fraction == 0.123456789012345
        assert row.log_growth_at_half_kelly == -0.007654321098765

    @pytest.mark.parametrize("name", list(GROWTH_FIELDS))
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_rejected_per_field(self, name: str, bad: float) -> None:
        with pytest.raises(ResultsStoreError, match=f"{name} must be a finite float or None"):
            _row(**{name: bad})

    @pytest.mark.parametrize("name", list(GROWTH_FIELDS))
    def test_negative_zero_canonicalised(self, name: str) -> None:
        row = _row(**{name: -0.0})
        assert str(getattr(row, name)) == "0.0"  # -0.0 → +0.0 (byte-determinism)


# ──────────────────────────────────────────────────────────────────────────
# Flush / load round trip + the Ruling-5 presence-tolerant read
# ──────────────────────────────────────────────────────────────────────────


class TestGrowthFlushLoad:
    def test_round_trip_values_and_nones(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1), **_TRIO))
        store.append(_row(run_id=_rid(2), loss_rate=0.5))
        store.append(_row(run_id=_rid(3)))
        store.flush()
        loaded = ResultsStore.load(path)
        assert loaded.rows() == store.rows()

    def test_growth_columns_are_float64_on_disk(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row())
        store.flush()
        schema = pl.read_parquet_schema(path)
        assert all(schema[name] == pl.Float64 for name in GROWTH_FIELDS)

    def test_pre_extension_store_loads_none_trio(self, tmp_path: Path) -> None:
        """Ruling 5: a store written before the growth columns existed loads
        the trio as an honest ``None`` — synthesised by dropping the three
        columns from a real flush (the vix_columns unit-twin pattern; the
        committed pre-extension fixture golden covers the historical file)."""
        pl = pytest.importorskip("polars")
        modern = tmp_path / "modern.parquet"
        store = ResultsStore(path=modern)
        store.append(_row(**_TRIO))
        store.flush()
        legacy = tmp_path / "legacy.parquet"
        pl.read_parquet(modern).drop(*GROWTH_FIELDS).write_parquet(legacy)
        loaded = ResultsStore.load(legacy)
        (row,) = loaded.rows()
        assert (row.loss_rate, row.kelly_fraction, row.log_growth_at_half_kelly) == (
            None,
            None,
            None,
        )


# ──────────────────────────────────────────────────────────────────────────
# The DIRECTIONAL backfill lane (extended from VIX)
# ──────────────────────────────────────────────────────────────────────────


class TestGrowthBackfill:
    def test_identical_reappend_is_noop(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        first_appended = store.append(_row(**_TRIO))
        assert first_appended is True
        reappended = store.append(_row(**_TRIO))
        assert reappended is False

    def test_none_to_value_backfills_and_logs(self) -> None:
        # The pre-extension-store redo shape: the stored row (loaded None)
        # is UPGRADED by a redo that carries the derived values.
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row())
        with capture_logs() as logs:
            backfilled = store.append(_row(**_TRIO))
            assert backfilled is True
        events = [e for e in logs if e["event"] == "results_store.growth_backfill"]
        assert len(events) == 1
        assert set(events[0]["backfilled_fields"]) == set(GROWTH_FIELDS)
        merged = store.get(_rid(1))
        assert merged is not None
        assert merged.loss_rate == _TRIO["loss_rate"]
        assert merged.kelly_fraction == _TRIO["kelly_fraction"]
        assert merged.log_growth_at_half_kelly == _TRIO["log_growth_at_half_kelly"]

    def test_value_to_none_never_erases(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(**_TRIO))
        redo_appended = store.append(_row())
        assert redo_appended is False  # less informed — a silent no-op
        merged = store.get(_rid(1))
        assert merged is not None
        assert merged.loss_rate == _TRIO["loss_rate"]

    def test_two_known_values_raise_the_plain_conflict(self) -> None:
        # Unlike VIX (the lake-moved story → the typed VixTrioConflictError),
        # a growth value-vs-value clash IS a reproducibility breach: the trio
        # is a pure function of the other stored columns + the run's code
        # (inside the run identity via code_commit_sha).
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(**_TRIO))
        with pytest.raises(ResultsStoreError, match="growth-metric conflict"):
            store.append(
                _row(loss_rate=0.999, kelly_fraction=0.21, log_growth_at_half_kelly=0.0135)
            )

    def test_mixed_vix_and_growth_backfill_logs_both_events(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row())
        with capture_logs() as logs:
            backfilled = store.append(_row(vix_mean=13.0, **_TRIO))
            assert backfilled is True
        events = {e["event"] for e in logs}
        assert "results_store.vix_backfill" in events
        assert "results_store.growth_backfill" in events

    def test_non_backfillable_diff_still_raises(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(**_TRIO))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(_row(net_pnl_paisa=2_000, **_TRIO))


# ──────────────────────────────────────────────────────────────────────────
# The nulls-last rankable law
# ──────────────────────────────────────────────────────────────────────────


class TestNullsLastRanking:
    def _seeded(self) -> ResultsStore:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), log_growth_at_half_kelly=0.5))
        store.append(_row(run_id=_rid(2)))  # None — must rank LAST either way
        store.append(_row(run_id=_rid(3), log_growth_at_half_kelly=-0.25))
        store.append(_row(run_id=_rid(4), log_growth_at_half_kelly=1.5))
        return store

    def test_descending_none_last(self) -> None:
        got = [r.run_id for r in self._seeded().top_k(4, by="log_growth_at_half_kelly")]
        assert got == [_rid(4), _rid(1), _rid(3), _rid(2)]

    def test_ascending_none_still_last(self) -> None:
        got = [
            r.run_id
            for r in self._seeded().top_k(4, by="log_growth_at_half_kelly", descending=False)
        ]
        assert got == [_rid(3), _rid(1), _rid(4), _rid(2)]

    def test_tie_breaks_by_run_id_ascending(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(7), kelly_fraction=0.5))
        store.append(_row(run_id=_rid(2), kelly_fraction=0.5))
        store.append(_row(run_id=_rid(9)))
        store.append(_row(run_id=_rid(4)))
        got = [r.run_id for r in store.top_k(4, by="kelly_fraction")]
        # Equal values tie on run_id ASC; the two Nones tie on run_id ASC too.
        assert got == [_rid(2), _rid(7), _rid(4), _rid(9)]

    def test_loss_rate_is_rankable(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), loss_rate=0.2))
        store.append(_row(run_id=_rid(2), loss_rate=0.6))
        got = [r.run_id for r in store.top_k(2, by="loss_rate", descending=False)]
        assert got == [_rid(1), _rid(2)]

    def test_unknown_field_error_names_both_vocabularies(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        with pytest.raises(ResultsStoreError, match="unknown ranking field") as excinfo:
            store.top_k(1, by="nope")
        assert "log_growth_at_half_kelly" in str(excinfo.value)
        assert "sharpe_ratio" in str(excinfo.value)

    def test_existing_rankable_fields_unchanged(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), sharpe_ratio=1.0))
        store.append(_row(run_id=_rid(2), sharpe_ratio=2.0))
        got = [r.run_id for r in store.top_k(2, by="sharpe_ratio")]
        assert got == [_rid(2), _rid(1)]

    def test_scope_sets_are_disjoint(self) -> None:
        # The growth trio + the Stage-B exit-quality column + the Stage-C
        # recency family rank ONLY under the nulls-last law; the plain
        # rankable set is untouched, and VIX stays non-rankable everywhere.
        assert (
            frozenset(GROWTH_FIELDS + EXIT_QUALITY_FIELDS + RECENCY_FIELDS)
            == _NULLABLE_RANKABLE_FIELDS
        )
        assert _RANKABLE_FIELDS.isdisjoint(_NULLABLE_RANKABLE_FIELDS)
        assert _NULLABLE_RANKABLE_FIELDS.isdisjoint(VIX_FIELDS)
        assert "deflated_sharpe" not in _RANKABLE_FIELDS | _NULLABLE_RANKABLE_FIELDS
