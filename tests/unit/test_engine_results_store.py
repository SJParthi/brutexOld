"""Unit tests for the deterministic results store (`brutex.engine.results_store`).

100% line + branch coverage of the reproducible sweep→Parquet sink
(DEC-RESULT-STORE-001):

* :class:`ResultRow` construction + every validation branch (run-id shape,
  empty labels, int64 range, absolute-magnitude / count / cross-field
  invariants, non-finite metric, non-finite / negative price, 2dp price snap);
* write → read round-trip, empty store, idempotent byte-identical re-write,
  duplicate-key dedup no-op, and the run-identity **conflict** raise;
* :meth:`ResultsStore.top_k` leaderboard (ordering, tie-break, descending,
  fewer-than-k, ``k < 1`` + unknown-field guards) and ``get`` / ``__len__`` /
  ``__contains__`` / ``rows`` / ``extend``;
* the ``polars``-missing import guards and the atomic-write failure cleanup;
* the G14 load-boundary damage wrap — old-schema / wrong-dtype / null-cell /
  invalid-content / corrupt-bytes files ALL raise the typed
  :class:`ResultsStoreSchemaError` naming the store path, never a raw error
  (gap-register ruling 5, DEC-SWEEP-RESULTS-DEPTH-001);
* the presence-tolerant optional-column back-compat law (missing optional
  column → honest ``None``; unknown extra column → ignored) — the
  WindowLedgerRecord precedent, guarded for real files by the pre-extension
  golden suite;
* the ruling-1 (R21) top-1000-per-tuple regression pin: a >1000-row store
  keeps every row and serves exactly the ordered top-1000 (tie law intact,
  no silent cap), before and after a flush→load cycle.

The byte-equality oracle + determinism invariants live in the golden / property
suites; here we assert behaviour + cover every branch.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # typing-only — the tests import polars lazily at run time
    import polars as pl

import pytest
from brutex.engine.results_store import (
    CONDITION_NAME_SEP,
    EXIT_QUALITY_FIELDS,
    MASK_BITS_MAX,
    FlushResult,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    ResultsStoreGateConflictError,
    ResultsStoreSchemaError,
    VixTrioConflictError,
)
from brutex.strategies.combo import combo_hash
from brutex.vocabulary.describe import matched_condition_names


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _row(**overrides: object) -> ResultRow:
    """Build a valid :class:`ResultRow`, overriding any field by keyword."""
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


# ──────────────────────────────────────────────────────────────────────────
# ResultRow — construction + validation
# ──────────────────────────────────────────────────────────────────────────


class TestResultRowValidation:
    def test_valid_row_round_trips_fields(self) -> None:
        row = _row()
        assert row.run_id == _rid(1)
        assert row.net_pnl_paisa == 1_000
        assert row.sharpe_ratio == 1.5

    def test_price_is_snapped_to_two_decimals(self) -> None:
        # Vendor float-noise → ROUND_HALF_UP 2dp grid (DEC-SPOT-PRICE-2DP-001).
        row = _row(avg_entry_price=Decimal("14020.7999"), avg_exit_price=Decimal("14100.005"))
        assert row.avg_entry_price == Decimal("14020.80")
        assert row.avg_exit_price == Decimal("14100.01")

    def test_accepts_real_combo_hash_run_id(self) -> None:
        # The store keys on the genuine 9-field BLAKE3 run-identity (not a stub).
        rid = combo_hash(
            strategy_id="s1",
            direction="bull",
            instrument="NIFTY",
            tf="5m",
            mode="A",
            params={"len": 20},
            data_snapshot_hash="snap",
            indicator_version="v1",
            code_commit_sha="deadbeef",
        )
        assert _row(run_id=rid).run_id == rid

    @pytest.mark.parametrize("bad", ["", "abc", "0" * 63, "0" * 65])
    def test_run_id_wrong_length_rejected(self, bad: str) -> None:
        with pytest.raises(ResultsStoreError, match="64-char"):
            _row(run_id=bad)

    def test_run_id_uppercase_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="lowercase"):
            _row(run_id="A" * 64)

    def test_run_id_non_hex_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="not valid hex"):
            _row(run_id="g" * 64)

    @pytest.mark.parametrize("field", ["instrument", "tf", "direction", "mode"])
    def test_empty_context_label_rejected(self, field: str) -> None:
        with pytest.raises(ResultsStoreError, match=f"{field} must be non-empty"):
            _row(**{field: ""})

    @pytest.mark.parametrize("field", ["net_pnl_paisa", "total_trades"])
    def test_int64_overflow_rejected(self, field: str) -> None:
        with pytest.raises(ResultsStoreError, match="out of signed int64 range"):
            _row(**{field: 2**63})

    def test_int64_min_underflow_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="out of signed int64 range"):
            _row(net_pnl_paisa=-(2**63) - 1)

    @pytest.mark.parametrize(
        "field", ["gross_profit_paisa", "gross_loss_paisa", "max_drawdown_paisa"]
    )
    def test_negative_absolute_money_rejected(self, field: str) -> None:
        with pytest.raises(ResultsStoreError, match="must be >= 0"):
            _row(**{field: -1})

    def test_negative_count_rejected(self) -> None:
        # winning=-1 keeps win+loss <= total, so the count-sign branch fires first.
        with pytest.raises(ResultsStoreError, match="winning_trades must be >= 0"):
            _row(winning_trades=-1, losing_trades=0)

    def test_win_plus_loss_exceeding_total_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="exceeds total_trades"):
            _row(total_trades=40, winning_trades=25, losing_trades=20)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_metric_rejected(self, bad: float) -> None:
        with pytest.raises(ResultsStoreError, match="finite float"):
            _row(sharpe_ratio=bad)

    @pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity")])
    def test_non_finite_price_rejected(self, bad: Decimal) -> None:
        with pytest.raises(ResultsStoreError, match="finite price"):
            _row(avg_entry_price=bad)

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="avg_exit_price must be >= 0"):
            _row(avg_exit_price=Decimal("-0.01"))

    def test_zero_counts_and_signed_pnl_allowed(self) -> None:
        # Boundary: zero trades, negative net P&L / expectancy are legal.
        row = _row(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            net_pnl_paisa=-5_000,
            expectancy_paisa=-125,
            avg_entry_price=Decimal("0"),
            avg_exit_price=Decimal("0"),
        )
        assert row.total_trades == 0
        assert row.net_pnl_paisa == -5_000
        assert row.avg_entry_price == Decimal("0.00")

    def test_negative_zero_metric_is_canonicalised(self) -> None:
        # -0.0 → +0.0 so two logically-equal rows are byte-identical (NOT rounding).
        row = _row(sharpe_ratio=-0.0)
        assert row.sharpe_ratio == 0.0
        assert str(row.sharpe_ratio) == "0.0"  # sign normalised away

    def test_finite_metric_is_left_bit_unchanged(self) -> None:
        # The `+ 0.0` canonicalisation must not perturb any non-zero finite value.
        row = _row(sharpe_ratio=1.234567890123456)
        assert row.sharpe_ratio == 1.234567890123456


# ──────────────────────────────────────────────────────────────────────────
# ResultRow — readable-mask surface (DEC-SWEEP-WINNERS-BY-NAME-001)
# ──────────────────────────────────────────────────────────────────────────


class TestReadableMask:
    def test_condition_names_decode_matches_vocabulary(self) -> None:
        # bits {0, 23, 30} = close_above_ema20, near_fib_618, bar_bullish.
        mask = (1 << 0) | (1 << 23) | (1 << 30)
        row = _row(mask_bits=mask)
        expected = CONDITION_NAME_SEP.join(matched_condition_names(mask))
        assert row.condition_names == expected
        assert row.condition_names == "close_above_ema20 AND near_fib_618 AND bar_bullish"

    def test_no_condition_mask_yields_empty_names(self) -> None:
        row = _row(mask_bits=0)
        assert row.condition_names == ""

    def test_condition_names_is_not_an_init_argument(self) -> None:
        # condition_names is DERIVED — passing it must be rejected as an unknown kwarg.
        with pytest.raises(TypeError, match="condition_names"):
            _row(condition_names="forged")

    @pytest.mark.parametrize("mask", [0, 1, MASK_BITS_MAX])
    def test_mask_bits_boundary_values_accepted(self, mask: int) -> None:
        assert _row(mask_bits=mask).mask_bits == mask

    @pytest.mark.parametrize("bad", [-1, MASK_BITS_MAX + 1, 1 << 63])
    def test_mask_bits_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(ResultsStoreError, match="mask_bits"):
            _row(mask_bits=bad)

    def test_deflated_sharpe_defaults_to_none(self) -> None:
        assert _row().deflated_sharpe is None

    def test_deflated_sharpe_finite_value_kept(self) -> None:
        assert _row(deflated_sharpe=0.97).deflated_sharpe == 0.97

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_deflated_sharpe_non_finite_rejected(self, bad: float) -> None:
        with pytest.raises(ResultsStoreError, match="deflated_sharpe must be a finite"):
            _row(deflated_sharpe=bad)

    def test_deflated_sharpe_negative_zero_canonicalised(self) -> None:
        row = _row(deflated_sharpe=-0.0)
        assert row.deflated_sharpe == 0.0
        assert str(row.deflated_sharpe) == "0.0"


# ──────────────────────────────────────────────────────────────────────────
# ResultsStore — append / dedup / conflict / lookup
# ──────────────────────────────────────────────────────────────────────────


class TestAppendAndLookup:
    def test_append_new_returns_true(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        added = store.append(_row(run_id=_rid(1)))
        assert added is True
        assert len(store) == 1
        assert _rid(1) in store
        assert _rid(2) not in store

    def test_append_identical_duplicate_is_noop(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1)))
        readded = store.append(_row(run_id=_rid(1)))
        assert readded is False  # idempotent dup-key dedup
        assert len(store) == 1

    def test_append_conflicting_run_id_raises(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), sharpe_ratio=1.5))
        with pytest.raises(ResultsStoreError, match="run-identity conflict") as exc:
            store.append(_row(run_id=_rid(1), sharpe_ratio=9.9))
        assert "sharpe_ratio" in str(exc.value)

    def test_append_gate_only_conflict_none_vs_float_raises_typed(self) -> None:
        """C5 (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001): a diff in ONLY the nullable
        gate metric (gate on vs off) is a config-change conflict, not corruption."""
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), deflated_sharpe=None))
        with pytest.raises(ResultsStoreGateConflictError, match="gate-config change") as exc:
            store.append(_row(run_id=_rid(1), deflated_sharpe=0.97))
        assert "deflated_sharpe" in str(exc.value)
        assert "DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001" in str(exc.value)

    def test_append_gate_only_conflict_float_vs_float_raises_typed(self) -> None:
        """C5: two DIFFERENT overfit configs (float vs float DSR) also classify typed."""
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), deflated_sharpe=0.80))
        with pytest.raises(ResultsStoreGateConflictError):
            store.append(_row(run_id=_rid(1), deflated_sharpe=0.97))

    def test_append_mixed_conflict_stays_generic(self) -> None:
        """C5: a conflict touching ANY non-nullable field (sharpe_ratio) stays the
        generic ResultsStoreError, NOT the gate subclass (subset test excludes it)."""
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), sharpe_ratio=1.5, deflated_sharpe=0.80))
        with pytest.raises(ResultsStoreError) as exc:
            store.append(_row(run_id=_rid(1), sharpe_ratio=9.9, deflated_sharpe=0.97))
        assert not isinstance(exc.value, ResultsStoreGateConflictError)
        assert "sharpe_ratio" in str(exc.value)

    def test_append_identical_gate_metric_row_is_noop(self) -> None:
        """C5 guard is only reached on a real diff — an identical row stays a no-op."""
        store = ResultsStore(path=Path("unused.parquet"))
        row = _row(run_id=_rid(1), deflated_sharpe=0.97)
        first_append = store.append(row)
        assert first_append is True
        second_append = store.append(row)
        assert second_append is False

    def test_get_hit_and_miss(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        row = _row(run_id=_rid(7))
        store.append(row)
        assert store.get(_rid(7)) == row
        assert store.get(_rid(8)) is None

    def test_extend_counts_only_new_rows(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        added = store.extend([_row(run_id=_rid(1)), _row(run_id=_rid(2)), _row(run_id=_rid(1))])
        assert added == 2
        assert len(store) == 2

    def test_rows_returns_run_id_sorted(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend([_row(run_id=_rid(3)), _row(run_id=_rid(1)), _row(run_id=_rid(2))])
        assert [r.run_id for r in store.rows()] == [_rid(1), _rid(2), _rid(3)]

    def test_extend_accepts_a_generator(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        added = store.extend(_row(run_id=_rid(i)) for i in range(3))
        assert added == 3
        assert len(store) == 3


# ──────────────────────────────────────────────────────────────────────────
# ResultsStore — top_k leaderboard
# ──────────────────────────────────────────────────────────────────────────


class TestTopK:
    def test_ranks_descending_by_metric(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend(
            [
                _row(run_id=_rid(1), sharpe_ratio=0.5),
                _row(run_id=_rid(2), sharpe_ratio=3.0),
                _row(run_id=_rid(3), sharpe_ratio=1.5),
            ]
        )
        top = store.top_k(2, by="sharpe_ratio")
        assert [r.run_id for r in top] == [_rid(2), _rid(3)]

    def test_ascending_order(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend(
            [_row(run_id=_rid(1), sharpe_ratio=0.5), _row(run_id=_rid(2), sharpe_ratio=3.0)]
        )
        top = store.top_k(2, by="sharpe_ratio", descending=False)
        assert [r.run_id for r in top] == [_rid(1), _rid(2)]

    def test_ties_broken_by_run_id_ascending(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        # All equal metric → deterministic run_id-ascending tie-break.
        store.extend(
            [_row(run_id=_rid(3), sharpe_ratio=1.0), _row(run_id=_rid(1), sharpe_ratio=1.0)]
        )
        store.append(_row(run_id=_rid(2), sharpe_ratio=1.0))
        top = store.top_k(3, by="sharpe_ratio")
        assert [r.run_id for r in top] == [_rid(1), _rid(2), _rid(3)]

    def test_rank_by_money_column(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend(
            [_row(run_id=_rid(1), net_pnl_paisa=10), _row(run_id=_rid(2), net_pnl_paisa=99)]
        )
        assert store.top_k(1, by="net_pnl_paisa")[0].run_id == _rid(2)

    def test_returns_fewer_than_k_when_store_smaller(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1)))
        assert len(store.top_k(10, by="sharpe_ratio")) == 1

    def test_empty_store_returns_empty_tuple(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        assert store.top_k(5, by="sharpe_ratio") == ()

    def test_k_below_one_rejected(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        with pytest.raises(ResultsStoreError, match="k must be >= 1"):
            store.top_k(0, by="sharpe_ratio")

    def test_unknown_ranking_field_rejected(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        with pytest.raises(ResultsStoreError, match="unknown ranking field"):
            store.top_k(1, by="avg_entry_price")  # price columns are not rankable

    def test_ranks_money_above_2_53_exactly(self) -> None:
        # int64 paisa above 2**53 must rank EXACTLY — the kernel's rank_top_k
        # receives the raw int (the retired `_field_getter` helper's law: no
        # float() coercion that would tie 2**53 and 2**53 + 1). This guards a
        # reproducibility-critical property (and kills the float mutant).
        big = 2**53
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend(
            [
                _row(run_id=_rid(1), net_pnl_paisa=big),
                _row(run_id=_rid(2), net_pnl_paisa=big + 1),
            ]
        )
        assert store.top_k(1, by="net_pnl_paisa")[0].run_id == _rid(2)

    def test_descending_false_ties_broken_by_run_id_ascending(self) -> None:
        # The run_id tie-break stays ASCENDING in both directions (the first
        # pass is not reversed) — equal metrics must order by run_id asc.
        store = ResultsStore(path=Path("unused.parquet"))
        store.extend(
            [
                _row(run_id=_rid(3), sharpe_ratio=1.0),
                _row(run_id=_rid(1), sharpe_ratio=1.0),
                _row(run_id=_rid(2), sharpe_ratio=1.0),
            ]
        )
        top = store.top_k(3, by="sharpe_ratio", descending=False)
        assert [r.run_id for r in top] == [_rid(1), _rid(2), _rid(3)]


# ──────────────────────────────────────────────────────────────────────────
# ResultsStore — flush / load round-trip + idempotence
# ──────────────────────────────────────────────────────────────────────────


class TestFlushLoad:
    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "results.parquet"
        store = ResultsStore(path=path)
        store.extend(
            [_row(run_id=_rid(1), sharpe_ratio=1.5), _row(run_id=_rid(2), sharpe_ratio=2.5)]
        )
        result = store.flush()
        assert isinstance(result, FlushResult)
        assert result.row_count == 2
        assert result.path == path
        assert len(result.content_hash) == 64
        assert result.bytes_written == path.stat().st_size

        reloaded = ResultsStore.load(path)
        assert len(reloaded) == 2
        assert reloaded.get(_rid(1)) == store.get(_rid(1))
        assert reloaded.get(_rid(2)) == store.get(_rid(2))

    def test_readable_mask_round_trips(self, tmp_path: Path) -> None:
        # mask_bits + (derived) condition_names + nullable deflated_sharpe survive a
        # flush→load, covering BOTH the None and the float deflated branches.
        path = tmp_path / "results.parquet"
        mask = (1 << 2) | (1 << 30)
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1), mask_bits=mask, deflated_sharpe=None))
        store.append(_row(run_id=_rid(2), mask_bits=mask, deflated_sharpe=0.97))
        store.flush()
        reloaded = ResultsStore.load(path)
        got_null = reloaded.get(_rid(1))
        got_val = reloaded.get(_rid(2))
        assert got_null is not None
        assert got_val is not None
        assert got_null.mask_bits == mask
        assert got_null.condition_names == "close_above_ema200 AND bar_bullish"
        assert got_null.deflated_sharpe is None
        assert got_val.deflated_sharpe == 0.97

    def test_price_round_trips_exactly(self, tmp_path: Path) -> None:
        path = tmp_path / "results.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1), avg_entry_price=Decimal("14020.7999")))
        store.flush()
        reloaded = ResultsStore.load(path)
        got = reloaded.get(_rid(1))
        assert got is not None
        assert got.avg_entry_price == Decimal("14020.80")

    def test_reflush_is_byte_identical(self, tmp_path: Path) -> None:
        path = tmp_path / "results.parquet"
        store = ResultsStore(path=path)
        store.extend([_row(run_id=_rid(1)), _row(run_id=_rid(2))])
        first = store.flush()
        bytes_first = path.read_bytes()
        second = store.flush()
        assert path.read_bytes() == bytes_first
        assert first.content_hash == second.content_hash

    def test_load_then_flush_is_byte_identical(self, tmp_path: Path) -> None:
        path = tmp_path / "results.parquet"
        store = ResultsStore(path=path)
        store.extend([_row(run_id=_rid(1)), _row(run_id=_rid(2))])
        store.flush()
        original = path.read_bytes()
        ResultsStore.load(path).flush()
        assert path.read_bytes() == original

    def test_empty_store_flush_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.parquet"
        store = ResultsStore(path=path)
        result = store.flush()
        assert result.row_count == 0
        original = path.read_bytes()
        reloaded = ResultsStore.load(path)
        assert len(reloaded) == 0
        reloaded.flush()
        assert path.read_bytes() == original  # empty store is byte-stable too

    def test_flush_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "results.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1)))
        store.flush()
        assert path.exists()

    def test_path_property(self, tmp_path: Path) -> None:
        path = tmp_path / "results.parquet"
        assert ResultsStore(path=path).path == path

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="not found"):
            ResultsStore.load(tmp_path / "does-not-exist.parquet")

    def test_append_after_flush_then_reflush_grows(self, tmp_path: Path) -> None:
        # The in-memory dict is the source of truth; flush snapshots it. Mutating
        # the store AFTER a flush and re-flushing must serialise the new state.
        path = tmp_path / "results.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1)))
        first = store.flush()
        assert first.row_count == 1
        store.append(_row(run_id=_rid(2)))
        second = store.flush()
        assert second.row_count == 2
        reloaded = ResultsStore.load(path)
        assert len(reloaded) == 2


# ──────────────────────────────────────────────────────────────────────────
# Failure-path coverage — polars-missing guards + atomic-write cleanup
# ──────────────────────────────────────────────────────────────────────────


class TestFailurePaths:
    def test_flush_without_polars_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = ResultsStore(path=tmp_path / "r.parquet")
        store.append(_row(run_id=_rid(1)))
        monkeypatch.setitem(sys.modules, "polars", None)  # block `import polars`
        with pytest.raises(ResultsStoreError, match="requires 'polars'"):
            store.flush()

    def test_load_without_polars_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "r.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1)))
        store.flush()  # create the file while polars is available
        monkeypatch.setitem(sys.modules, "polars", None)
        with pytest.raises(ResultsStoreError, match="requires 'polars'"):
            ResultsStore.load(path)

    def test_atomic_write_cleans_up_temp_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "r.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1)))

        def _boom(_src: str, _dst: str) -> None:
            raise OSError("simulated rename failure")

        # The atomic write is now the shared atomic_io core
        # (DEC-ATOMIC-WRITE-CONSOLIDATE-001), so os.replace + the temp prefix live there.
        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _boom)
        with pytest.raises(OSError, match="simulated rename failure"):
            store.flush()
        # No half-written temp file is left behind in the destination dir.
        assert not list(tmp_path.glob(".brutex-write-*"))
        assert not path.exists()


# ──────────────────────────────────────────────────────────────────────────
# G14 — old-schema / damaged store files raise the TYPED schema error
# (gap-register ruling 5, DEC-SWEEP-RESULTS-DEPTH-001): never a raw
# polars / KeyError / TypeError / ValueError that could 500 /results.
# ──────────────────────────────────────────────────────────────────────────


def _write_current_schema_df(path: Path) -> None:
    """Flush a small REAL two-row store to *path* (the mutation base)."""
    store = ResultsStore(path=path)
    store.extend(
        [
            _row(run_id=_rid(1), sharpe_ratio=1.5, deflated_sharpe=0.5),
            _row(run_id=_rid(2), sharpe_ratio=2.5, deflated_sharpe=None),
        ]
    )
    store.flush()


class TestLoadSchemaDamageWrap:
    """B1 (G14): every decode failure is :class:`ResultsStoreSchemaError`."""

    def _mutated(self, tmp_path: Path, mutate: Callable[[pl.DataFrame], pl.DataFrame]) -> Path:
        """Write a REAL store, apply *mutate* to its DataFrame, rewrite it."""
        import polars as pl

        good = tmp_path / "good.parquet"
        _write_current_schema_df(good)
        df = mutate(pl.read_parquet(good))
        bad = tmp_path / "bad.parquet"
        df.write_parquet(bad)
        return bad

    def test_old_schema_missing_required_column_raises_typed(self, tmp_path: Path) -> None:
        # The pre-u128 shape: ONE `mask_bits` Int64 column instead of the
        # `mask_bits_lo`/`mask_bits_hi` limb pair — the way an OLD run wrote it.
        import polars as pl

        bad = self._mutated(
            tmp_path,
            lambda df: df.drop("mask_bits_lo", "mask_bits_hi").with_columns(
                pl.lit(5, dtype=pl.Int64).alias("mask_bits")
            ),
        )
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)  # the store path is named
        assert "mask_bits_lo" in str(excinfo.value)  # the missing column is named
        assert isinstance(excinfo.value.__cause__, KeyError)  # wrapped, never raw

    def test_wrong_dtype_metric_column_raises_typed(self, tmp_path: Path) -> None:
        # sharpe_ratio stored as Utf8 (a foreign/hand-rolled writer) → ValueError → typed.
        import polars as pl

        bad = self._mutated(
            tmp_path,
            lambda df: df.with_columns(pl.lit("not-a-number", dtype=pl.Utf8).alias("sharpe_ratio")),
        )
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_wrong_dtype_price_column_raises_typed(self, tmp_path: Path) -> None:
        # avg_entry_price as junk text → decimal.InvalidOperation (ArithmeticError) → typed.
        import polars as pl

        bad = self._mutated(
            tmp_path,
            lambda df: df.with_columns(pl.lit("junk", dtype=pl.Utf8).alias("avg_entry_price")),
        )
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ArithmeticError)

    def test_null_in_required_column_raises_typed(self, tmp_path: Path) -> None:
        # A null cell in a REQUIRED Int64 column (torn/foreign write) → TypeError → typed.
        import polars as pl

        bad = self._mutated(
            tmp_path,
            lambda df: df.with_columns(pl.lit(None, dtype=pl.Int64).alias("net_pnl_paisa")),
        )
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, TypeError)

    def test_invalid_row_content_raises_typed_naming_path(self, tmp_path: Path) -> None:
        # Dtype-clean but validation-violating content (negative absolute magnitude):
        # the ResultRow raise is re-wrapped so the STORE PATH is named.
        import polars as pl

        bad = self._mutated(
            tmp_path,
            lambda df: df.with_columns(pl.lit(-5, dtype=pl.Int64).alias("gross_profit_paisa")),
        )
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ResultsStoreError)

    def test_duplicate_conflicting_rows_in_file_raise_typed(self, tmp_path: Path) -> None:
        # Two rows sharing one run_id with different metrics — a file flush() can
        # never produce — is file damage, wrapped with the path named.
        import polars as pl

        def _dup(df: pl.DataFrame) -> pl.DataFrame:
            clone = df.head(1).with_columns(pl.lit(9.75, dtype=pl.Float64).alias("sharpe_ratio"))
            return pl.concat([df, clone])

        bad = self._mutated(tmp_path, _dup)
        with pytest.raises(ResultsStoreSchemaError, match="not decodable") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ResultsStoreError)

    def test_corrupt_bytes_raise_typed_schema_error_naming_path(self, tmp_path: Path) -> None:
        bad = tmp_path / "garbage.parquet"
        bad.write_bytes(b"\x00\x01 not a parquet file")
        with pytest.raises(ResultsStoreSchemaError, match="unreadable results store") as excinfo:
            ResultsStore.load(bad)
        assert str(bad) in str(excinfo.value)

    def test_schema_error_is_a_results_store_error(self) -> None:
        # Every existing resilient reader catches ResultsStoreError — the damage
        # subclass MUST stay inside that hierarchy (skip-and-count unchanged).
        assert issubclass(ResultsStoreSchemaError, ResultsStoreError)


# ──────────────────────────────────────────────────────────────────────────
# B2 — presence-tolerant reads for OPTIONAL columns (the WindowLedgerRecord
# precedent): a store written BEFORE an optional-column extension loads with
# the missing field as an honest None. The committed pre-extension golden
# (tests/golden/test_results_store_pre_extension_golden.py) is the guard;
# here we pin the MECHANISM.
# ──────────────────────────────────────────────────────────────────────────


class TestPresenceTolerantOptionalColumns:
    def test_store_missing_optional_deflated_sharpe_loads_none(self, tmp_path: Path) -> None:
        # Simulate a pre-DEC-RANKING-OVERFIT-001 store: today's file minus the
        # optional column. It must LOAD (never "old-schema damage"), with the
        # field an honest None and every other field byte-for-byte intact.
        import polars as pl

        good = tmp_path / "good.parquet"
        _write_current_schema_df(good)
        old = tmp_path / "pre-extension.parquet"
        pl.read_parquet(good).drop("deflated_sharpe").write_parquet(old)

        reloaded = ResultsStore.load(old)
        assert len(reloaded) == 2
        expected_1 = _row(run_id=_rid(1), sharpe_ratio=1.5, deflated_sharpe=None)
        expected_2 = _row(run_id=_rid(2), sharpe_ratio=2.5, deflated_sharpe=None)
        assert reloaded.get(_rid(1)) == expected_1  # None default, all else intact
        assert reloaded.get(_rid(2)) == expected_2

    def test_newer_store_with_unknown_extra_column_still_loads(self, tmp_path: Path) -> None:
        # The mirror direction: TODAY'S reader over a FUTURE store carrying an
        # unknown additive column ignores it (extensions are additive-only, so
        # an old reader never breaks on a new file).
        import polars as pl

        good = tmp_path / "good.parquet"
        _write_current_schema_df(good)
        newer = tmp_path / "future.parquet"
        pl.read_parquet(good).with_columns(
            pl.lit(7, dtype=pl.Int64).alias("mae_avg_paisa")
        ).write_parquet(newer)

        reloaded = ResultsStore.load(newer)
        assert len(reloaded) == 2
        assert reloaded.get(_rid(1)) == _row(run_id=_rid(1), sharpe_ratio=1.5, deflated_sharpe=0.5)


# ──────────────────────────────────────────────────────────────────────────
# B3 — gap-register ruling 1 (R21): the store genuinely keeps + serves
# top_k=1000 per tuple — exact count, order law intact, no silent cap.
# ──────────────────────────────────────────────────────────────────────────

#: > 1000 so the top-1000 slice is a strict subset (proves no silent cap).
_THOUSAND_POOL: Final[int] = 1_100


def _thousand_rows() -> list[ResultRow]:
    """1,100 rows with DELIBERATE ranking ties (pnl pairs) for the tie law."""
    return [
        _row(
            run_id=_rid(i),
            # Pairs tie on the ranking column → the run_id-asc tie law is
            # exercised inside the board AND across the rank-1000 boundary.
            net_pnl_paisa=(i // 2) * 10,
            sharpe_ratio=float(i % 7),
        )
        for i in range(_THOUSAND_POOL)
    ]


class TestTopThousandStorage:
    def test_top_k_1000_exact_order_and_no_silent_cap(self) -> None:
        rows = _thousand_rows()
        store = ResultsStore(path=Path("unused.parquet"))
        assert store.extend(rows) == _THOUSAND_POOL
        assert len(store) == _THOUSAND_POOL  # all 1,100 KEPT (no cap at ingest)

        board = store.top_k(1000, by="net_pnl_paisa")
        assert len(board) == 1000  # exactly 1000 served — never silently fewer

        # Independent oracle: value DESC, ties run_id ASC (ints — exact).
        expected = sorted(rows, key=lambda r: (-r.net_pnl_paisa, r.run_id))[:1000]
        assert [r.run_id for r in board] == [r.run_id for r in expected]
        # The rank-1000 boundary cut the worst 100 rows, not an arbitrary slice.
        served = {r.run_id for r in board}
        dropped = {r.run_id for r in rows} - served
        assert len(dropped) == _THOUSAND_POOL - 1000
        assert max(r.net_pnl_paisa for r in rows if r.run_id in dropped) <= min(
            r.net_pnl_paisa for r in board
        )

    def test_top_k_1000_survives_flush_load_round_trip(self, tmp_path: Path) -> None:
        # The STORAGE side of ruling 1: a >1000-row tuple store persists and
        # re-serves the identical ordered top-1000 after a flush→load cycle.
        rows = _thousand_rows()
        path = tmp_path / "tuple_store.parquet"
        store = ResultsStore(path=path)
        store.extend(rows)
        before = [r.run_id for r in store.top_k(1000, by="net_pnl_paisa")]
        flushed = store.flush()
        assert flushed.row_count == _THOUSAND_POOL

        reloaded = ResultsStore.load(path)
        assert len(reloaded) == _THOUSAND_POOL
        after = [r.run_id for r in reloaded.top_k(1000, by="net_pnl_paisa")]
        assert after == before
        assert len(after) == 1000


# ── sl_hit_rate exit-quality column (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 B) ──


class TestSlHitRateColumn:
    """The Stage-B exit-quality column: Ruling-5 optionality, backfill,
    conflict, rank law, and the public field constant."""

    def test_public_constant_and_default_none(self) -> None:
        assert EXIT_QUALITY_FIELDS == ("sl_hit_rate",)
        assert _row().sl_hit_rate is None

    def test_concrete_value_round_trips_through_parquet(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(sl_hit_rate=0.375))
        store.flush()
        loaded = ResultsStore.load(tmp_path / "s.parquet")
        assert loaded.rows()[0].sl_hit_rate == 0.375

    def test_none_round_trips_as_parquet_null(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row())
        store.flush()
        assert ResultsStore.load(tmp_path / "s.parquet").rows()[0].sl_hit_rate is None

    def test_non_finite_value_rejected(self) -> None:
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(ResultsStoreError, match="sl_hit_rate"):
                _row(sl_hit_rate=bad)

    def test_directional_backfill_none_to_value(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row())
        upgraded = store.append(_row(sl_hit_rate=0.25))
        assert upgraded is True
        assert store.get(_rid(1)) is not None
        assert store.get(_rid(1)).sl_hit_rate == 0.25  # type: ignore[union-attr]

    def test_less_informed_redo_never_erases(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(sl_hit_rate=0.25))
        redo_appended = store.append(_row())
        assert redo_appended is False  # directional no-op
        assert store.get(_rid(1)).sl_hit_rate == 0.25  # type: ignore[union-attr]

    def test_value_vs_value_conflict_raises_plain_error(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(sl_hit_rate=0.25))
        with pytest.raises(ResultsStoreError, match="exit-quality conflict") as exc_info:
            store.append(_row(sl_hit_rate=0.5))
        # The reproducibility-breach family — NOT the VIX lake-moved type.
        assert not isinstance(exc_info.value, VixTrioConflictError)

    def test_top_k_ranks_nulls_last_both_directions(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), sl_hit_rate=0.9))
        store.append(_row(run_id=_rid(2), sl_hit_rate=0.1))
        store.append(_row(run_id=_rid(3)))  # None — pre-extension unknown
        asc = store.top_k(3, by="sl_hit_rate", descending=False)
        assert [r.sl_hit_rate for r in asc] == [0.1, 0.9, None]
        desc = store.top_k(3, by="sl_hit_rate", descending=True)
        assert [r.sl_hit_rate for r in desc] == [0.9, 0.1, None]


class TestSelectionFields:
    """LANE W1 (DEC-RANK-PARETO-PERSIST-001): the selection-provenance pair."""

    def test_defaults_are_honest_none(self) -> None:
        row = _row()
        assert row.pareto_member is None
        assert row.selection_basis is None

    def test_valid_canonical_tag_accepted(self) -> None:
        row = _row(pareto_member=True, selection_basis="composite,dd,frontier")
        assert row.pareto_member is True
        assert row.selection_basis == "composite,dd,frontier"

    def test_empty_tag_refused(self) -> None:
        with pytest.raises(ResultsStoreError, match="non-empty token string"):
            _row(selection_basis="")

    def test_unknown_token_refused(self) -> None:
        with pytest.raises(ResultsStoreError, match="selection_basis invalid"):
            _row(selection_basis="composite,sharpe")

    def test_misordered_tag_refused(self) -> None:
        # Tokens valid but NOT in the canonical BASIS_ORDER join.
        with pytest.raises(ResultsStoreError, match="canonical BASIS_ORDER"):
            _row(selection_basis="dd,composite")

    def test_pareto_member_without_frontier_token_refused(self) -> None:
        # The review-round-1 coherence cross-check, direction 1: a claimed
        # frontier member MUST carry the "frontier" basis token (the persist
        # composer stamps both from the ONE frontier set).
        with pytest.raises(ResultsStoreError, match="incoherent"):
            _row(pareto_member=True, selection_basis=None)
        with pytest.raises(ResultsStoreError, match="incoherent"):
            _row(pareto_member=True, selection_basis="composite")

    def test_frontier_token_without_pareto_member_refused(self) -> None:
        # Direction 2: a "frontier"-tagged row MUST claim membership — both
        # the honest-unknown None and an explicit False are incoherent.
        with pytest.raises(ResultsStoreError, match="incoherent"):
            _row(pareto_member=None, selection_basis="frontier")
        with pytest.raises(ResultsStoreError, match="incoherent"):
            _row(pareto_member=False, selection_basis="composite,frontier")

    def test_coherent_tag_combinations_accepted(self) -> None:
        # Every coherent shape constructs: member+token, tagged non-member
        # without the token, and the legacy None/None row.
        assert _row(pareto_member=True, selection_basis="frontier").pareto_member is True
        assert _row(pareto_member=False, selection_basis="composite").selection_basis == "composite"
        legacy = _row(pareto_member=None, selection_basis=None)
        assert (legacy.pareto_member, legacy.selection_basis) == (None, None)

    def test_selection_fields_round_trip_flush_load(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        path = tmp_path / "sel.parquet"
        store = ResultsStore(path=path)
        store.append(
            _row(run_id="1" * 64, pareto_member=True, selection_basis="composite,frontier")
        )
        store.append(_row(run_id="2" * 64, mask_bits=0b11))
        store.flush()
        loaded = ResultsStore.load(path)
        rows = loaded.rows()
        assert (rows[0].pareto_member, rows[0].selection_basis) == (True, "composite,frontier")
        assert (rows[1].pareto_member, rows[1].selection_basis) == (None, None)

    def test_differing_selection_tags_are_a_strict_conflict(self) -> None:
        # The append-conflict law: tags are computed BEFORE the single append
        # per run_id; a second append with a different tag is a run-identity
        # conflict (never a silent merge — the VIX backfill is the ONE
        # sanctioned directional exception, and selection is not in it).
        store = ResultsStore(path=Path("/tmp/unused.parquet"))
        store.append(_row(selection_basis="composite"))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(_row(selection_basis="composite,dd"))

    def test_not_rankable(self) -> None:
        store = ResultsStore(path=Path("/tmp/unused.parquet"))
        store.append(_row())
        for name in ("pareto_member", "selection_basis"):
            with pytest.raises(ResultsStoreError, match="unknown ranking field"):
                store.top_k(1, by=name)


class TestSignedZeroPriceNoop:
    """The kernel's TEXT price compare must keep the retired NUMERIC law on zeros.

    ``Decimal("-0.00")`` passes the ``price >= 0`` validation and equals
    ``Decimal("0.00")`` NUMERICALLY, so the retired dataclass ``__eq__`` read an
    identical re-append as a silent no-op. ``_price_ffi_text`` normalises the
    sign wrapper-side (the hostile-review MINOR of DEC-RUST-RUNID-STORES-001) —
    without it the text compare would flip this into a false run-identity
    conflict.
    """

    def test_signed_zero_price_reappend_is_noop_not_conflict(self) -> None:
        from dataclasses import replace
        from decimal import Decimal

        store = ResultsStore(path=Path("unused.parquet"))
        stored = _row(avg_entry_price=Decimal("0.00"))
        incoming = replace(stored, avg_entry_price=Decimal("-0.00"))
        assert stored == incoming  # the retired NUMERIC law: equal rows
        appended = store.append(stored)
        assert appended is True
        reappended = store.append(incoming)
        assert reappended is False  # noop — never a conflict
        assert len(store) == 1

    def test_price_ffi_text_normalises_only_zero(self) -> None:
        from decimal import Decimal

        from brutex.engine.results_store import _price_ffi_text

        assert _price_ffi_text(Decimal("-0.00")) == "0.00"
        assert _price_ffi_text(Decimal("0.00")) == "0.00"
        assert _price_ffi_text(Decimal("123.45")) == "123.45"
        # Scale text preserved on the zero class (only the SIGN is dropped).
        assert _price_ffi_text(Decimal("-0")) == "0"

    def test_row_to_ffi_marshals_row_positions(self) -> None:
        # DEC-RUST-RUNID-STORES-002 scaffolding (zero-caller today): _row_to_ffi
        # marshals one row into the positional FFI tuple the kernel append
        # decision will consume. Pin EVERY position against its source field so
        # a field re-order or a dropped/added column fails loudly — the two
        # price positions carry the _price_ffi_text signed-zero normalisation,
        # every other position is the raw field.
        from brutex.engine.results_store import _price_ffi_text, _row_to_ffi

        row = _row(
            deflated_sharpe=0.9,
            vix_mean=14.2,
            vix_min=11.0,
            vix_max=19.5,
        )
        ffi = _row_to_ffi(row)
        assert ffi == (
            row.run_id,
            row.instrument,
            row.tf,
            row.direction,
            row.mode,
            row.mask_bits,
            row.net_pnl_paisa,
            row.gross_profit_paisa,
            row.gross_loss_paisa,
            row.max_drawdown_paisa,
            row.expectancy_paisa,
            row.total_trades,
            row.winning_trades,
            row.losing_trades,
            row.sharpe_ratio,
            row.sortino_ratio,
            row.calmar_ratio,
            row.profit_factor,
            row.win_rate,
            row.max_drawdown_pct,
            _price_ffi_text(row.avg_entry_price),
            _price_ffi_text(row.avg_exit_price),
            row.deflated_sharpe,
            row.vix_mean,
            row.vix_min,
            row.vix_max,
            row.condition_names,
        )
        # The two price positions are TEXT (the signed-zero-normalising
        # marshal), never the raw Decimal — spot-pin the transform.
        assert ffi[20] == _price_ffi_text(row.avg_entry_price)
        assert ffi[21] == _price_ffi_text(row.avg_exit_price)
        assert ffi[0] == row.run_id
        assert ffi[26] == row.condition_names
