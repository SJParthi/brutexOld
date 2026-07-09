"""Annotation-additive / rank-independence GATE for the resilience block.

Mirrors ``tests/property/test_futures_suffix_independence.py`` +
``tests/unit/test_futures_leaderboard.py``: annotate → strip the ONE key ==
input; the ranked top100 ORDER is bit-identical with vs without each
candidate's resilience attached; double-annotation refused; and no resilience
column is a ranking/preset input.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from brutex.engine.resilience_streaks import (
    RESILIENCE_ANNOTATION_KEY,
    RESILIENCE_COLUMNS,
    ResilienceStreaks,
    fold_resilience,
    project_resilience_annotation,
    resilience_null,
)
from brutex.engine.top100_artifact import PoolCounts, build_top100
from brutex.engine.twin_detect import TwinCandidate, group_twins
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import make_candidate

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))
_RUN = "c3" * 32


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _record(seed: int) -> ResilienceStreaks:
    ts = [
        _FakeTrade(
            pnl_paisa=-100 if (i + seed) % 2 else 200,
            entry_ts=datetime(2024, 1, 1 + i, 10, 0, tzinfo=_IST).astimezone(UTC),
            exit_ts=datetime(2024, 1, 1 + i, 10, 1, tzinfo=_IST).astimezone(UTC),
        )
        for i in range(5)
    ]
    return fold_resilience("r", ts, parity_ok=True, start_equity_paisa=1_000_000, fingerprint="fp")


@given(rank=st.integers(), sharpe=st.floats(-5, 5), net=st.integers(-(10**9), 10**9))
def test_r13_r30_rank_independence_property(rank: int, sharpe: float, net: int) -> None:
    row = {"rank": rank, "sharpe_ratio": sharpe, "net_pnl_paisa": net, "run_id": "r"}
    annotated = project_resilience_annotation(row, _record(rank))
    stripped = {k: v for k, v in annotated.items() if k != RESILIENCE_ANNOTATION_KEY}
    assert stripped == row
    assert row == {"rank": rank, "sharpe_ratio": sharpe, "net_pnl_paisa": net, "run_id": "r"}


def test_annotation_additive_only() -> None:
    row = {"a": 1, "b": 2}
    out = project_resilience_annotation(row, resilience_null("r", 0, "f"))
    assert {k: out[k] for k in row} == row
    assert row == {"a": 1, "b": 2}
    assert out[RESILIENCE_ANNOTATION_KEY]["stats_live"] is False  # type: ignore[index]


def test_double_annotation_refused() -> None:
    with pytest.raises(ValueError, match="refusing to overwrite"):
        project_resilience_annotation({RESILIENCE_ANNOTATION_KEY: {}}, None)


def _pool() -> list[object]:
    return [
        make_candidate(1, fingerprint=fingerprint_seed(1), row_overrides={"max_drawdown_pct": 4.0}),
        make_candidate(2, fingerprint=fingerprint_seed(2), row_overrides={"max_drawdown_pct": 9.0}),
        make_candidate(
            3, fingerprint=fingerprint_seed(3), row_overrides={"max_drawdown_pct": 22.0}
        ),
    ]


def _order(candidates: list[object]) -> list[object]:
    twins = group_twins(
        [
            TwinCandidate(
                run_id=c.row.run_id,  # type: ignore[attr-defined]
                mask_bits=c.row.mask_bits,  # type: ignore[attr-defined]
                fingerprint=c.agg.fingerprint,  # type: ignore[attr-defined]
                entry_ts_us=frozenset({100 * (i + 1)}),
            )
            for i, c in enumerate(candidates)
        ]
    )
    artifact = build_top100(
        candidates,  # type: ignore[arg-type]
        run_id=_RUN,
        preset="min_dd_first",
        families=twins,
        counts=PoolCounts(stores=1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0),
        data_snapshot_hashes=("s",),
        baselines={},
    )
    return [e["run_id"] for e in artifact.entries]


def test_ranking_order_is_identical_with_and_without_resilience() -> None:
    bare = _pool()
    annotated = [replace(c, resilience=_record(i)) for i, c in enumerate(bare)]  # type: ignore[arg-type]
    assert _order(bare) == _order(annotated)


def test_no_resilience_column_is_a_ranking_or_preset_input() -> None:
    from brutex.dashboard import sweep_results_view
    from brutex.engine import results_store
    from brutex.ranking import presets

    # The resilience METRIC columns (excluding the shared join/provenance keys
    # ``run_id`` / ``fingerprint``, which every sidecar carries) must appear in
    # NO ranking / preset / gate input surface — annotation-only by construction.
    metric_names = {name for name, _ in RESILIENCE_COLUMNS} - {"run_id", "fingerprint"}
    assert metric_names.isdisjoint(set(results_store._RANKABLE_FIELDS))
    rank_fields = set(presets.RankRecord.__dataclass_fields__)
    assert metric_names.isdisjoint(rank_fields)
    assert metric_names.isdisjoint(set(sweep_results_view.PRESET_INERT_AXES))
