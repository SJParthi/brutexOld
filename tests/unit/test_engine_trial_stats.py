"""Unit tests for the trial-statistics sidecar core (`brutex.engine.trial_stats`).

100% line + branch for the strict per-module gate (Golden Rule #12), built to
the ADR-0008 conditions slice 1 owns (DEC-TRIAL-STATS-CORE-001):

* condition 3 — the finite-Sharpe N/V population (None/non-finite in N, out of
  the moments; the SHARED ``_MIN_FINITE_SHARPES`` threshold, never re-derived);
* condition 5 — the pairing-identity fields and their format validation;
* condition 2 (IO half) — atomic, byte-deterministic sidecar write/read with a
  golden byte pin;
* conditions 9/12 — the exact-counts/approximate-moments merge contract and
  the APPROXIMATE-only cross-check against ``statistics.pvariance``.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pytest
from brutex.engine.trial_stats import (
    TRIAL_STATS_SCHEMA_VERSION,
    TRIAL_STATS_SUFFIX,
    LeaderboardTrialStats,
    TrialStatsError,
    TupleTrialStats,
    merge_tuple_trial_stats,
    read_tuple_trial_stats,
    render_tuple_trial_stats,
    trial_stats_sidecar_path,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)

_DIGEST = "ab12cd34ef56ab12"
_STORE_HASH = "f" * 64


def _stats(**overrides: object) -> TupleTrialStats:
    base: dict[str, object] = {
        "n_backtested": 4,
        "n_finite": 3,
        "sum_sharpe": 1.5,
        "sum_sharpe_sq": 1.25,
        "n_enumerated": 10,
        "n_conflict_pruned": 2,
        "n_minhits_dropped": 1,
        "tuple_digest": _DIGEST,
        "store_content_hash": _STORE_HASH,
    }
    base.update(overrides)
    return TupleTrialStats(**base)  # type: ignore[arg-type]


# ────── tuple_trial_stats_from_sharpes (condition 3) ──────


class TestFromSharpes:
    def test_counts_every_backtest_and_only_finite_moments(self) -> None:
        # 0.5 and 0.25 are exact binary fractions — fsum equals plain arithmetic.
        stats = tuple_trial_stats_from_sharpes(
            [0.5, None, float("nan"), float("inf"), -0.25],
            n_enumerated=10,
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        assert stats.n_backtested == 5  # None + NaN + inf still count in N
        assert stats.n_finite == 2  # … but never in the V population
        assert stats.sum_sharpe == 0.25
        assert stats.sum_sharpe_sq == 0.3125

    def test_threshold_is_the_shared_overfit_constant(self) -> None:
        # Condition 3: the finite-pool minimum comes from brutex.ranking.overfit
        # — one source of truth, never a re-derived literal.
        from brutex.engine import trial_stats
        from brutex.ranking import overfit

        assert trial_stats._MIN_FINITE_SHARPES is overfit._MIN_FINITE_SHARPES

    def test_digest_length_matches_the_orchestrator_path_digest(self) -> None:
        # The 16-hex pairing digest mirrors sweep_orchestrator._PATH_DIGEST_LEN
        # (pinned here rather than imported — the module stays out of the
        # orchestrator's import graph).
        from brutex.engine import sweep_orchestrator, trial_stats

        assert trial_stats._TUPLE_DIGEST_LEN == sweep_orchestrator._PATH_DIGEST_LEN

    def test_empty_population_is_the_honest_zero(self) -> None:
        stats = tuple_trial_stats_from_sharpes(
            [],
            n_enumerated=0,
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        assert stats.n_backtested == 0
        assert stats.n_finite == 0
        assert stats.sum_sharpe == 0.0
        assert stats.sum_sharpe_sq == 0.0

    def test_signed_zero_is_canonicalised(self) -> None:
        stats = tuple_trial_stats_from_sharpes(
            [-0.0],
            n_enumerated=1,
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        assert math.copysign(1.0, stats.sum_sharpe) == 1.0  # -0.0 → +0.0


# ────── TupleTrialStats validation ──────


class TestTupleTrialStatsValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "n_backtested",
            "n_finite",
            "n_enumerated",
            "n_conflict_pruned",
            "n_minhits_dropped",
        ],
    )
    def test_negative_count_rejected(self, field: str) -> None:
        with pytest.raises(TrialStatsError, match=f"{field} must be >= 0"):
            _stats(**{field: -1})

    def test_finite_exceeding_backtested_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="cannot exceed n_backtested"):
            _stats(n_backtested=2, n_finite=3)

    def test_tallies_exceeding_enumerated_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="cannot exceed n_enumerated"):
            _stats(n_enumerated=5, n_conflict_pruned=2, n_minhits_dropped=1, n_backtested=4)

    @pytest.mark.parametrize("field", ["sum_sharpe", "sum_sharpe_sq"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_non_finite_moment_rejected(self, field: str, bad: float) -> None:
        with pytest.raises(TrialStatsError, match=f"{field} must be finite"):
            _stats(**{field: bad})

    def test_negative_sum_of_squares_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="sum_sharpe_sq must be >= 0"):
            _stats(sum_sharpe_sq=-0.5)

    def test_zero_finite_with_nonzero_moments_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="zero finite Sharpes admit no moments"):
            _stats(n_finite=0, sum_sharpe=1.0, sum_sharpe_sq=1.0)

    def test_bad_digest_length_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="tuple_digest must be 16 hex"):
            _stats(tuple_digest="abc")

    def test_uppercase_digest_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="must be lowercase hex"):
            _stats(tuple_digest=_DIGEST.upper())

    def test_non_hex_digest_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="is not valid hex"):
            _stats(tuple_digest="zz12cd34ef56ab12")

    def test_bad_store_hash_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="store_content_hash must be 64 hex"):
            _stats(store_content_hash="abcd")


# ────── LeaderboardTrialStats (variance / deflatable) ──────


class TestLeaderboardTrialStats:
    def test_negative_count_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="n_trials must be >= 0"):
            LeaderboardTrialStats(n_trials=-1, n_finite=0, sum_sharpe=0.0, sum_sharpe_sq=0.0)

    def test_finite_exceeding_trials_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="cannot exceed n_trials"):
            LeaderboardTrialStats(n_trials=1, n_finite=2, sum_sharpe=0.0, sum_sharpe_sq=0.0)

    def test_zero_finite_with_moments_rejected(self) -> None:
        with pytest.raises(TrialStatsError, match="zero finite Sharpes"):
            LeaderboardTrialStats(n_trials=3, n_finite=0, sum_sharpe=0.5, sum_sharpe_sq=0.25)

    def test_under_threshold_variance_is_zero_and_undeflatable(self) -> None:
        one = LeaderboardTrialStats(n_trials=5, n_finite=1, sum_sharpe=2.0, sum_sharpe_sq=4.0)
        assert one.variance == 0.0
        assert one.deflatable is False

    def test_variance_matches_pvariance_approximately(self) -> None:
        # Conditions 9/12: the moment identity is ULP-distinct from pvariance's
        # exact-Fraction path — the cross-check is APPROXIMATE by design.
        values = [0.31, -1.7, 2.44, 0.005, -0.92]
        merged = LeaderboardTrialStats(
            n_trials=len(values),
            n_finite=len(values),
            sum_sharpe=math.fsum(values),
            sum_sharpe_sq=math.fsum(v * v for v in values),
        )
        assert merged.variance == pytest.approx(statistics.pvariance(values), rel=1e-12)
        assert merged.deflatable is True

    def test_cancellation_clamps_to_zero(self) -> None:
        # Identical large Sharpes: true variance 0; E[x²]−E[x]² cancels to a
        # tiny signed residue — the clamp pins it at exactly 0.0.
        value = 1e8 + 0.1
        merged = LeaderboardTrialStats(
            n_trials=2,
            n_finite=2,
            sum_sharpe=math.fsum([value, value]),
            sum_sharpe_sq=math.fsum([value * value, value * value]),
        )
        assert merged.variance == 0.0
        assert merged.deflatable is False

    def test_signed_zero_moments_canonicalised(self) -> None:
        merged = LeaderboardTrialStats(n_trials=2, n_finite=2, sum_sharpe=-0.0, sum_sharpe_sq=0.0)
        assert math.copysign(1.0, merged.sum_sharpe) == 1.0


# ────── merge_tuple_trial_stats ──────


class TestMerge:
    def test_merge_matches_direct_build_over_concatenation(self) -> None:
        sharpes_a: list[float | None] = [0.5, None, 1.25]
        sharpes_b: list[float | None] = [-0.75, float("nan"), 2.0, 0.125]
        part_a = tuple_trial_stats_from_sharpes(
            sharpes_a,
            n_enumerated=5,
            n_conflict_pruned=1,
            n_minhits_dropped=1,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        part_b = tuple_trial_stats_from_sharpes(
            sharpes_b,
            n_enumerated=6,
            n_conflict_pruned=0,
            n_minhits_dropped=2,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        merged = merge_tuple_trial_stats([part_a, part_b])
        whole = tuple_trial_stats_from_sharpes(
            sharpes_a + sharpes_b,
            n_enumerated=11,
            n_conflict_pruned=1,
            n_minhits_dropped=3,
            tuple_digest=_DIGEST,
            store_content_hash=_STORE_HASH,
        )
        assert merged.n_trials == whole.n_backtested  # counts are EXACT
        assert merged.n_finite == whole.n_finite
        # Moments: approximate vs the single global fsum (the ULP disclosure).
        assert merged.sum_sharpe == pytest.approx(whole.sum_sharpe, rel=1e-12)
        assert merged.sum_sharpe_sq == pytest.approx(whole.sum_sharpe_sq, rel=1e-12)

    def test_merge_is_deterministic_for_a_fixed_order(self) -> None:
        parts = [_stats(), _stats(n_finite=2, sum_sharpe=0.75, sum_sharpe_sq=0.5)]
        first = merge_tuple_trial_stats(parts)
        second = merge_tuple_trial_stats(parts)
        assert first == second

    def test_empty_merge_is_the_honest_zero(self) -> None:
        merged = merge_tuple_trial_stats([])
        assert merged.n_trials == 0
        assert merged.deflatable is False

    def test_incomplete_flag_threads_through(self) -> None:
        assert merge_tuple_trial_stats([_stats()], complete=False).complete is False


# ────── sidecar path + IO (condition 2's IO half + condition 5) ──────


class TestSidecarIO:
    def test_path_appends_the_suffix_to_the_full_store_name(self, tmp_path: Path) -> None:
        store = tmp_path / "NIFTY" / "1minute" / "bull_k3_ab12cd34ef56ab12.parquet"
        sidecar = trial_stats_sidecar_path(store)
        assert sidecar.name == "bull_k3_ab12cd34ef56ab12.parquet" + TRIAL_STATS_SUFFIX
        assert sidecar.parent == store.parent

    def test_render_is_byte_deterministic(self) -> None:
        assert render_tuple_trial_stats(_stats()) == render_tuple_trial_stats(_stats())

    def test_golden_bytes_pin(self) -> None:
        # The exact v1 byte contract — any serialization drift fails here.
        stats = _stats(n_backtested=2, n_finite=2, sum_sharpe=0.75, sum_sharpe_sq=0.3125)
        expected = (
            '{"n_backtested":2,"n_conflict_pruned":2,"n_enumerated":10,'
            '"n_finite":2,"n_minhits_dropped":1,"schema_version":1,'
            f'"store_content_hash":"{_STORE_HASH}","sum_sharpe":0.75,'
            f'"sum_sharpe_sq":0.3125,"tuple_digest":"{_DIGEST}"}}'
        ).encode()
        assert render_tuple_trial_stats(stats) == expected

    def test_write_read_round_trip_and_rewrite_byte_identity(self, tmp_path: Path) -> None:
        path = tmp_path / "store.parquet.trials.json"
        stats = _stats()
        write_tuple_trial_stats(path, stats)
        first_bytes = path.read_bytes()
        assert read_tuple_trial_stats(path) == stats
        write_tuple_trial_stats(path, stats)  # idempotent rewrite
        assert path.read_bytes() == first_bytes

    def test_write_creates_parents_and_leaves_no_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "tree" / "store.parquet.trials.json"
        write_tuple_trial_stats(path, _stats())
        assert path.exists()
        assert [p.name for p in path.parent.iterdir()] == [path.name]

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TrialStatsError, match="not found"):
            read_tuple_trial_stats(tmp_path / "absent.trials.json")

    def test_read_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.trials.json"
        path.write_text("{not json")
        with pytest.raises(TrialStatsError, match="not valid JSON"):
            read_tuple_trial_stats(path)

    def test_read_non_object_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "list.trials.json"
        path.write_text("[1, 2]")
        with pytest.raises(TrialStatsError, match="must be a JSON object"):
            read_tuple_trial_stats(path)

    def test_read_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "v9.trials.json"
        payload = json.loads(render_tuple_trial_stats(_stats()))
        payload["schema_version"] = TRIAL_STATS_SCHEMA_VERSION + 1
        path.write_text(json.dumps(payload))
        with pytest.raises(TrialStatsError, match="unsupported trial-stats schema_version"):
            read_tuple_trial_stats(path)

    def test_read_missing_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.trials.json"
        payload = json.loads(render_tuple_trial_stats(_stats()))
        del payload["sum_sharpe"]
        path.write_text(json.dumps(payload))
        with pytest.raises(TrialStatsError, match="missing keys"):
            read_tuple_trial_stats(path)

    def test_read_malformed_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "malformed.trials.json"
        payload = json.loads(render_tuple_trial_stats(_stats()))
        payload["n_backtested"] = "many"
        path.write_text(json.dumps(payload))
        with pytest.raises(TrialStatsError, match="malformed field"):
            read_tuple_trial_stats(path)

    def test_read_propagates_invariant_violations(self, tmp_path: Path) -> None:
        path = tmp_path / "invalid.trials.json"
        payload = json.loads(render_tuple_trial_stats(_stats()))
        payload["n_finite"] = payload["n_backtested"] + 1
        path.write_text(json.dumps(payload))
        with pytest.raises(TrialStatsError, match="cannot exceed n_backtested"):
            read_tuple_trial_stats(path)
