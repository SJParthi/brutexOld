"""R26 flat-read bench gate for :func:`brutex.engine.top100_artifact.load_top100`.

Pins the R26/R29 decree: the shipped top-100 leaderboard read stays FLAT as
the STORED-WINNER count grows 100× (200 → 20,000 pooled winners), because the
artifact is precomputed at run-end to a bounded ≤100-entry file — the read is
one bounded open+parse+sha256, never a scan of the winner tree. The artifact
byte size itself must be bounded (identical entry cap at both pool sizes).

Honest envelope (Golden Rule #10): the FIRST artifact build is one sanctioned
O(pool) run-end pass (R29's "artifact FIRST build" clause) — this gate times
the READ only, which is what every dashboard poll pays.
"""

from __future__ import annotations

import time
from pathlib import Path

from brutex.engine.top100_artifact import (
    TOP100_FILENAME,
    TOP100_LIMIT,
    PoolCounts,
    Top100Candidate,
    build_top100,
    load_top100,
    write_top100,
)
from brutex.engine.twin_detect import TwinFamilies

from tests.bench._o1_tiers import cache_tier_ceiling
from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import make_candidate

_N_SMALL = 200
_N_BIG = 20_000

_ROUNDS = 7
_WARMUP = 2

_NO_FAMILIES = TwinFamilies(family_by_run_id={}, members_by_family={})


def _run_id(n_pool: int) -> str:
    return f"{n_pool:064x}"


def _pool(n: int) -> list[Top100Candidate]:
    # Distinct fingerprints + spread drawdowns so the rank is a real total
    # order over the whole pool (no degenerate all-ties shortcut).
    return [
        make_candidate(
            i + 1,
            fingerprint=fingerprint_seed(i + 1),
            row_overrides={"max_drawdown_pct": float((i * 13) % 291) / 10.0 + 0.1},
        )
        for i in range(n)
    ]


def _build_runs_dir(root: Path, n_pool: int) -> Path:
    runs_dir = root / "runs"
    artifact = build_top100(
        _pool(n_pool),
        run_id=_run_id(n_pool),
        preset="min_dd_first",
        counts=PoolCounts(stores=1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0),
        families=_NO_FAMILIES,
        data_snapshot_hashes=("snap0001",),
        baselines={},
    )
    write_top100(runs_dir, artifact)
    return runs_dir


def _time_load_ns(runs_dir: Path, run_id: str) -> float:
    for _ in range(_WARMUP):
        assert load_top100(runs_dir, run_id) is not None
    best = float("inf")
    for _ in range(_ROUNDS):
        start = time.perf_counter_ns()
        artifact = load_top100(runs_dir, run_id)
        best = min(best, float(time.perf_counter_ns() - start))
        assert artifact is not None
    return best


def test_top100_read_flat_vs_stored_winner_count(tmp_path: Path) -> None:
    small_dir = _build_runs_dir(tmp_path / "small", _N_SMALL)
    big_dir = _build_runs_dir(tmp_path / "big", _N_BIG)

    # The artifact is BOUNDED at both pool sizes — the structural reason the
    # read is O(1) in stored-winner count (R26 "one small artifact").
    small_bytes = (small_dir / _run_id(_N_SMALL) / TOP100_FILENAME).stat().st_size
    big_bytes = (big_dir / _run_id(_N_BIG) / TOP100_FILENAME).stat().st_size
    small_artifact = load_top100(small_dir, _run_id(_N_SMALL))
    big_artifact = load_top100(big_dir, _run_id(_N_BIG))
    assert small_artifact is not None
    assert big_artifact is not None
    assert len(small_artifact.entries) == TOP100_LIMIT
    assert len(big_artifact.entries) == TOP100_LIMIT
    assert abs(big_bytes - small_bytes) < small_bytes  # same-order file size, never O(pool)

    t_small = _time_load_ns(small_dir, _run_id(_N_SMALL))
    t_big = _time_load_ns(big_dir, _run_id(_N_BIG))
    ratio = t_big / max(t_small, 1.0)
    assert ratio < cache_tier_ceiling(5.0), (
        f"top100 read latency ratio {ratio:.2f}× for a {_N_BIG:,}- vs {_N_SMALL:,}-winner pool "
        f"(t_small={t_small / 1e6:.3f} ms, t_big={t_big / 1e6:.3f} ms) — the shipped leaderboard "
        "read must stay flat in stored-winner count (R26 decree)"
    )
