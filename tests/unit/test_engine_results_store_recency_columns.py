"""Store-level tests for the recency columns (DEC-METRICS-TRAILING-WINDOW-001).

The four nullable ``RECENCY_FIELDS`` on :class:`ResultRow` —
``trailing_sharpe_ratio`` / ``trailing_profit_factor`` /
``trailing_net_pnl_paisa`` / ``decay_weighted_net_paisa`` — appended LAST
(the optional-column back-compat template), rankable under the NULLS-LAST
law, riding the directional backfill lane with the growth-style plain-error
conflict arm.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import (
    RECENCY_FIELDS,
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


_FAMILY = {
    "trailing_sharpe_ratio": 0.87,
    "trailing_profit_factor": 1.44,
    "trailing_net_pnl_paisa": 620.0,
    "decay_weighted_net_paisa": 512.25,
}


class TestRecencyFieldValidation:
    def test_family_vocabulary_is_frozen(self) -> None:
        assert RECENCY_FIELDS == (
            "trailing_sharpe_ratio",
            "trailing_profit_factor",
            "trailing_net_pnl_paisa",
            "decay_weighted_net_paisa",
        )

    def test_family_defaults_to_none(self) -> None:
        row = _row()
        assert all(getattr(row, name) is None for name in RECENCY_FIELDS)

    def test_full_family_round_trips_at_full_precision(self) -> None:
        row = _row(**_FAMILY)
        for name, value in _FAMILY.items():
            assert getattr(row, name) == value  # never rounded (CLAUDE.md §4)

    @pytest.mark.parametrize("name", RECENCY_FIELDS)
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_values_are_refused(self, name: str, bad: float) -> None:
        with pytest.raises(ResultsStoreError, match=name):
            _row(**{name: bad})

    @pytest.mark.parametrize("name", RECENCY_FIELDS)
    def test_negative_zero_is_canonicalised(self, name: str) -> None:
        assert math.copysign(1.0, getattr(_row(**{name: -0.0}), name)) == 1.0


class TestRecencyFlushLoad:
    def test_values_and_nulls_round_trip(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1), **_FAMILY))
        store.append(_row(run_id=_rid(2)))  # all-None family
        store.flush()
        loaded = ResultsStore.load(path)
        concrete = loaded.get(_rid(1))
        empty = loaded.get(_rid(2))
        assert concrete is not None and empty is not None
        for name, value in _FAMILY.items():
            assert getattr(concrete, name) == value
            assert getattr(empty, name) is None

    def test_pre_recency_file_loads_none_family(self, tmp_path: Path) -> None:
        # The synthetic legacy twin of the committed golden fixture: drop the
        # four recency columns from a real flush — every absent column reads
        # the honest None (the Ruling-5 presence-tolerant law).
        pl = pytest.importorskip("polars")
        modern = tmp_path / "modern.parquet"
        store = ResultsStore(path=modern)
        store.append(_row(**_FAMILY))
        store.flush()
        legacy = tmp_path / "legacy.parquet"
        pl.read_parquet(modern).drop(list(RECENCY_FIELDS)).write_parquet(legacy)
        loaded = ResultsStore.load(legacy)
        row = loaded.get(_rid(1))
        assert row is not None
        assert all(getattr(row, name) is None for name in RECENCY_FIELDS)


class TestRecencyNullsLastRanking:
    @pytest.mark.parametrize("name", RECENCY_FIELDS)
    def test_none_ranks_after_every_real_value_both_directions(self, name: str) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(run_id=_rid(1), **{name: 2.0}))
        store.append(_row(run_id=_rid(2)))  # None
        store.append(_row(run_id=_rid(3), **{name: -1.0}))
        desc = [r.run_id for r in store.top_k(3, by=name)]
        assert desc == [_rid(1), _rid(3), _rid(2)]
        asc = [r.run_id for r in store.top_k(3, by=name, descending=False)]
        assert asc == [_rid(3), _rid(1), _rid(2)]


class TestRecencyBackfill:
    def test_none_to_value_backfills_with_one_info(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row())
        with capture_logs() as logs:
            assert store.append(_row(**_FAMILY)) is True
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored.decay_weighted_net_paisa == _FAMILY["decay_weighted_net_paisa"]
        events = [e for e in logs if e["event"] == "results_store.recency_backfill"]
        assert len(events) == 1
        assert set(events[0]["backfilled_fields"]) == set(RECENCY_FIELDS)

    def test_value_to_none_is_a_directional_no_op(self) -> None:
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(**_FAMILY))
        assert store.append(_row()) is False  # less-informed redo never erases
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored.trailing_sharpe_ratio == _FAMILY["trailing_sharpe_ratio"]

    @pytest.mark.parametrize("name", RECENCY_FIELDS)
    def test_value_vs_value_conflict_is_a_plain_reproducibility_error(self, name: str) -> None:
        # The growth-style arm: two known values under one run_id are a
        # genuine reproducibility breach (the anchor is inside the identity
        # via data_snapshot_hash) — the PLAIN error, never the VIX class.
        store = ResultsStore(path=Path("unused.parquet"))
        store.append(_row(**{name: 1.25}))
        with pytest.raises(ResultsStoreError, match="recency-metric conflict"):
            store.append(_row(**{name: 1.5}))
