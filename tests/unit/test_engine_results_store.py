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
* the ``polars``-missing import guards and the atomic-write failure cleanup.

The byte-equality oracle + determinism invariants live in the golden / property
suites; here we assert behaviour + cover every branch.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import (
    CONDITION_NAME_SEP,
    MASK_BITS_MAX,
    FlushResult,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
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
        # int64 paisa above 2**53 must rank EXACTLY — `_field_getter` returns the
        # raw int (no float() coercion that would tie 2**53 and 2**53 + 1). This
        # guards a reproducibility-critical property (and kills the float mutant).
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
