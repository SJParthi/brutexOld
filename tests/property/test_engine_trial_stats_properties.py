"""Hypothesis properties for the trial-statistics sidecar core (ADR-0008 slice 1).

Pins the DEC-TRIAL-STATS-CORE-001 invariants over arbitrary inputs:

* partition invariance — merging ANY partition of a Sharpe population agrees
  with the whole-population build on counts EXACTLY and on moments/variance to
  relative tolerance (the conditions-9/12 ULP disclosure, never exact);
* the variance is always ``>= 0`` and approximately ``statistics.pvariance``;
* the builder's population invariants (``n_finite <= n_backtested``);
* the sidecar JSON round-trip is lossless and byte-stable.
"""

from __future__ import annotations

import math
import statistics

import pytest
from brutex.engine.trial_stats import (
    LeaderboardTrialStats,
    merge_tuple_trial_stats,
    read_tuple_trial_stats,
    render_tuple_trial_stats,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_SETTINGS = settings(max_examples=200, deadline=None)

#: Sharpe-like draws: realistic magnitudes + None/NaN/inf contaminants.
_SHARPES = st.lists(
    st.one_of(
        st.none(),
        st.just(float("nan")),
        st.just(float("inf")),
        st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
    ),
    max_size=60,
)

_IDENTITY = {
    "tuple_digest": "ab12cd34ef56ab12",
    "store_content_hash": "f" * 64,
}


def _build(sharpes: list[float | None]) -> tuple[int, int]:
    """Build stats for *sharpes* with consistent tallies; return (n, n_finite)."""
    stats = tuple_trial_stats_from_sharpes(
        sharpes,
        n_enumerated=len(sharpes),
        n_conflict_pruned=0,
        n_minhits_dropped=0,
        **_IDENTITY,
    )
    return stats.n_backtested, stats.n_finite


@given(sharpes=_SHARPES)
@_SETTINGS
def test_builder_population_invariants(sharpes: list[float | None]) -> None:
    n, n_finite = _build(sharpes)
    assert n == len(sharpes)
    assert 0 <= n_finite <= n


@given(sharpes=_SHARPES, cut=st.integers(min_value=0, max_value=60))
@_SETTINGS
def test_partition_merge_agrees_with_whole(sharpes: list[float | None], cut: int) -> None:
    split = min(cut, len(sharpes))
    parts = [
        tuple_trial_stats_from_sharpes(
            chunk,
            n_enumerated=len(chunk),
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            **_IDENTITY,
        )
        for chunk in (sharpes[:split], sharpes[split:])
    ]
    merged = merge_tuple_trial_stats(parts)
    whole_stats = tuple_trial_stats_from_sharpes(
        sharpes,
        n_enumerated=len(sharpes),
        n_conflict_pruned=0,
        n_minhits_dropped=0,
        **_IDENTITY,
    )
    assert merged.n_trials == whole_stats.n_backtested  # counts EXACT
    assert merged.n_finite == whole_stats.n_finite
    assert merged.sum_sharpe == pytest.approx(whole_stats.sum_sharpe, rel=1e-9, abs=1e-9)
    assert merged.sum_sharpe_sq == pytest.approx(whole_stats.sum_sharpe_sq, rel=1e-9, abs=1e-9)


@given(sharpes=_SHARPES)
@_SETTINGS
def test_variance_non_negative_and_matches_pvariance(sharpes: list[float | None]) -> None:
    finite = [s for s in sharpes if s is not None and math.isfinite(s)]
    merged = LeaderboardTrialStats(
        n_trials=len(sharpes),
        n_finite=len(finite),
        sum_sharpe=math.fsum(finite),
        sum_sharpe_sq=math.fsum(s * s for s in finite),
    )
    assert merged.variance >= 0.0
    if len(finite) >= 2:
        assert merged.variance == pytest.approx(statistics.pvariance(finite), rel=1e-9, abs=1e-9)
    else:
        assert merged.variance == 0.0
        assert merged.deflatable is False


@given(sharpes=_SHARPES)
@_SETTINGS
def test_sidecar_round_trip_is_lossless_and_byte_stable(
    sharpes: list[float | None], tmp_path_factory: pytest.TempPathFactory
) -> None:
    stats = tuple_trial_stats_from_sharpes(
        sharpes,
        n_enumerated=len(sharpes),
        n_conflict_pruned=0,
        n_minhits_dropped=0,
        **_IDENTITY,
    )
    path = tmp_path_factory.mktemp("trials") / "s.parquet.trials.json"
    write_tuple_trial_stats(path, stats)
    restored = read_tuple_trial_stats(path)
    assert restored == stats
    assert render_tuple_trial_stats(restored) == path.read_bytes()
