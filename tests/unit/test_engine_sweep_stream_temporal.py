"""SurvivorColumns + bounded-rank temporal arm (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The columnar retention of the per-mask year-fold triples (17 B/kept, counted
inside ``owned_nbytes``), the BOTH-direction ``with_temporal`` ↔ config
mismatch refusals, and the bounded rank's hard-doors → temporal → overfit
ordering through the SAME :func:`apply_temporal_gate` core the batch rank uses.
"""

from __future__ import annotations

import pytest
from brutex.engine.sweep_stream import SurvivorColumns, rank_survivor_columns
from brutex.metrics.compute import ResultMetrics
from brutex.ranking.temporal import (
    MODE_OBSERVE,
    TemporalGateInput,
    TemporalGateStats,
    TemporalRobustnessConfig,
    TemporalTriple,
)

from tests.fakes.metrics_fixtures import make_result_metrics

_STEADY = TemporalTriple(
    profitable_judged_years=4, max_pos_year_pnl_paisa=500, sum_pos_year_pnl_paisa=1_000
)
_LUCKY = TemporalTriple(
    profitable_judged_years=1,
    max_pos_year_pnl_paisa=5_000_000,
    sum_pos_year_pnl_paisa=5_000_000,
)


def _metrics(i: int = 0, **overrides: object) -> ResultMetrics:
    return make_result_metrics(sharpe_ratio=1.0 + i * 0.25, **overrides)


def _bundle(
    *, mode: str = "gate", n_judged: int = 7, stats: TemporalGateStats | None = None
) -> TemporalGateInput:
    return TemporalGateInput(
        cfg=TemporalRobustnessConfig(mode=mode),
        n_judged_years=n_judged,
        triples=None,
        stats=stats,
    )


class TestAppendTemporalColumns:
    def test_round_trips_the_triple_exactly(self) -> None:
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(), temporal=_STEADY)
        columns.append(2, _metrics(1), temporal=_LUCKY)
        assert columns.temporal_at(0) == _STEADY
        assert columns.temporal_at(1) == _LUCKY
        assert columns.with_temporal is True

    def test_missing_triple_refused_naming_the_mask(self) -> None:
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        with pytest.raises(ValueError, match="no TemporalTriple"):
            columns.append(5, _metrics())

    def test_unexpected_triple_refused(self) -> None:
        columns = SurvivorColumns(with_overfit=False)
        with pytest.raises(ValueError, match="with_temporal=False"):
            columns.append(5, _metrics(), temporal=_STEADY)

    def test_default_with_temporal_is_false(self) -> None:
        columns = SurvivorColumns(with_overfit=False)
        assert columns.with_temporal is False

    def test_owned_nbytes_counts_exactly_17_bytes_per_kept(self) -> None:
        """B3 — the exact-byte accounting teeth: 1 (i8 prof) + 8 + 8 per kept."""
        without = SurvivorColumns(with_overfit=False)
        with_temporal = SurvivorColumns(with_overfit=False, with_temporal=True)
        for i in range(10):
            without.append(i + 1, _metrics(i))
            with_temporal.append(i + 1, _metrics(i), temporal=_STEADY)
        assert with_temporal.owned_nbytes() - without.owned_nbytes() == 17 * 10

    def test_overflowing_profitable_count_fails_loud(self) -> None:
        """The i8 column bounds judged years at 127 — a wider count raises at
        append (loud), never truncates silently."""
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        wide = TemporalTriple(
            profitable_judged_years=128,
            max_pos_year_pnl_paisa=1,
            sum_pos_year_pnl_paisa=128,
        )
        with pytest.raises(OverflowError):
            columns.append(1, _metrics(), temporal=wide)


class TestRankMismatchRefusals:
    def test_temporal_requested_without_columns_refused(self) -> None:
        columns = SurvivorColumns(with_overfit=False)
        columns.append(1, _metrics())
        with pytest.raises(ValueError, match="with_temporal=False"):
            rank_survivor_columns(columns, overfit=None, top_k=1, temporal=_bundle())

    def test_columns_without_config_refused(self) -> None:
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(), temporal=_STEADY)
        with pytest.raises(ValueError, match="no temporal gate config"):
            rank_survivor_columns(columns, overfit=None, top_k=1)

    def test_triples_payload_refused_on_the_bounded_path(self) -> None:
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(), temporal=_STEADY)
        bad = TemporalGateInput(cfg=TemporalRobustnessConfig(), n_judged_years=7, triples=[_STEADY])
        with pytest.raises(ValueError, match="triples must be None"):
            rank_survivor_columns(columns, overfit=None, top_k=1, temporal=bad)


class TestBoundedTemporalGate:
    def test_gate_mode_filters_and_counts(self) -> None:
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(0), temporal=_STEADY)
        columns.append(2, _metrics(1), temporal=_LUCKY)
        columns.append(4, _metrics(2), temporal=_STEADY)
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(stats=stats)
        )
        assert sorted(f.mask for f in result.finalists) == [1, 4]
        assert result.n_ranked == 2
        assert stats.killed == 1

    def test_observe_mode_filters_nothing(self) -> None:
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(0), temporal=_STEADY)
        columns.append(2, _metrics(1), temporal=_LUCKY)
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(mode=MODE_OBSERVE, stats=stats)
        )
        assert sorted(f.mask for f in result.finalists) == [1, 2]
        assert stats.observed_kills == 1

    def test_abstention_keeps_everything(self) -> None:
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(0), temporal=_LUCKY)
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(n_judged=1, stats=stats)
        )
        assert [f.mask for f in result.finalists] == [1]
        assert stats.abstained is True

    def test_hard_door_rejects_never_reach_the_temporal_gate(self) -> None:
        """Ordering pin: doors → temporal. A door-failing mask is not a
        temporal kill (the counters stay clean)."""
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(1, _metrics(0), temporal=_STEADY)
        columns.append(
            2,
            _metrics(1, total_trades=5, winning_trades=3, losing_trades=2),
            temporal=_LUCKY,
        )
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(stats=stats)
        )
        assert [f.mask for f in result.finalists] == [1]
        assert stats.killed == 0  # the lucky mask died at the DOORS, not here

    def test_empty_survivors_still_record_gate_stats(self) -> None:
        """All-door-failing columns: the gate still RUNS on the empty survivor
        set — exactly like the batch path's always-call `_temporal_survivors`
        — so the recorded stats never diverge batch-vs-bounded (review round 1:
        the old `and positions` short-circuit left `abstained` unset here)."""
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(
            1,
            _metrics(0, total_trades=5, winning_trades=3, losing_trades=2),
            temporal=_STEADY,
        )
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(stats=stats)
        )
        assert result.finalists == ()
        assert stats.evaluated is True  # invoked — batch-path parity
        assert stats.abstained is False
        assert stats.killed == 0

    def test_empty_survivors_sub_floor_abstains_like_batch(self) -> None:
        """Batch ≡ bounded on `temporal_abstained` for a zero-survivor
        sub-floor tuple (review round 1): the bounded path previously reported
        `abstained=False` (silent) where the batch path reported `True`."""
        stats = TemporalGateStats()
        columns = SurvivorColumns(with_overfit=False, with_temporal=True)
        columns.append(
            1,
            _metrics(0, total_trades=5, winning_trades=3, losing_trades=2),
            temporal=_STEADY,
        )
        result = rank_survivor_columns(
            columns, overfit=None, top_k=10, temporal=_bundle(n_judged=1, stats=stats)
        )
        assert result.finalists == ()
        assert stats.abstained is True  # the batch path's answer, now shared
