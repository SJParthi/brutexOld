"""Gate 1 — the at-scale pull SATURATION gate (DEC-PULL-SCALE-HARNESS-001, MIGRATION.md #929).

Drives the REAL production pull stack (adaptive
:class:`~brutex.adapters.rate_common.RateController` at ceiling 8 r/s + the 500/min
governor, the size-4 :class:`~brutex.adapters.groww.pool.ClientPool` over ONE shared
:class:`~brutex.adapters.call_meter.CallMeter`, the real
:func:`~brutex.adapters.groww.ingest.ingest_spot` + :class:`~brutex.orchestrator.\
parallel.PoolRunParallel`, the real :class:`~brutex.data.sink.ParquetBarSink`) against a
deterministic, offline :class:`~tests.fakes.fake_groww_sdk.FakeGrowwSDK`, then asserts the
pull SATURATES the rate it is allowed:

* **slack < 15 %** — the headline RATIO assert: ``achieved_rps >= 0.85 ×
  allowed_rps_timeavg``. ``achieved_rps`` is the ISSUED-token rate (the rate controller's
  governed unit — each window legitimately consumes the bucket twice: the per-window pace
  + the client-retry pace), so a near-zero slack PROVES the limiter is the BINDING gate
  (the pull keeps the bucket full), NOT the pre-Lever-2 0.49-in-flight starve.
* **pool idle ≤ 25 %** — the K=4 connections stay busy (``in_flight_p50`` near the pool
  size). This is meaningful ONLY in the saturation regime where the vendor latency is high
  enough that the pool, not the rate, could be the constraint — the scenario tunes the
  fake's latency to that regime (see the scenario docstring).
* **rps_1s_max ≤ ceiling + tolerance** — the live PROOF the per-second cap held (the
  trailing-1 s completion peak can never run away).

Ratio-based asserts (robust to CI-host noise); the absolute write-latency ms is a LOOSE
secondary. Deterministic seed. Runs in ≤ ~3 min on the reference envelope.

**Honest envelope (CLAUDE.md §2 Rule 10):** the saturation regime needs a per-call latency
≈ ``pool_size / window_rate`` (~1 s here) so the pool can fill — at the fake's DEFAULT
fast-socket 5-20 ms latency the rate ceiling binds with near-zero concurrency (in-flight
≈ ``rate × latency`` ≈ 0.05 by Little's law), which makes ``pool_idle`` ~100 % by physics,
NOT a defect. Gate 1 therefore drives the *saturating* latency so BOTH the rate-binding
(slack) AND the pool-busy (idle) properties can hold at once; the fast-vendor-low-latency
case is exercised by the cheaper ``test_harness_smoke`` below (which asserts ONLY the
rate-binding slack, the property that holds regardless of latency).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.constants import (
    GROWW_RATE_LIMIT_RPS_CEILING,
)
from brutex.adapters.ingest_common import tile_windows
from brutex.adapters.rate_common import BURST

from tests.bench.pull_harness import PullScenario, run_pull_harness


def _expected_windows(scenario: PullScenario) -> int:
    """Total fetch windows = per-contract tile count × contracts (the production tiling)."""
    tiles = tile_windows(
        scenario.interval,
        scenario.start,
        scenario.end,
        window_days_for=window_days_for_interval,
    )
    return len(tiles) * scenario.n_contracts


#: Slack budget — achieved (issued-token) rate must be within 15 % of allowed (Gate 1).
_MAX_SLACK_RATIO = 0.15
#: Pool-idle budget — the K connections idle ≤ 25 % of wall time in the saturation regime.
_MAX_POOL_IDLE_PCT = 25.0
#: rps_1s_max tolerance over the ceiling. The trailing-1 s completion gauge can read a
#: couple over the ceiling at a window edge (a completion landing just before eviction of
#: the prior second's tail) + the BURST allowance; this bounds it loosely as a "the cap
#: did not RUN AWAY" proof, not a hard per-second equality.
_RPS_1S_MAX_TOLERANCE = 3


@pytest.mark.bench
def test_pull_at_scale_gate_saturates_allowed_rate(tmp_path: Path) -> None:
    """Gate 1: 40 contracts × 12 mo, pool 4, fast vendor, no bursts → rate is BINDING.

    The saturation regime (per-call latency ~1 s) lets BOTH the rate-binding slack AND the
    pool-busy idle properties hold. Deterministic seed → reproducible numbers.
    """
    scenario = PullScenario(
        n_contracts=40,
        start=date(2024, 1, 1),
        end=date(2025, 1, 1),  # 12 months → 12 monthly 1-minute tiles per contract
        interval="1minute",
        pool_size=4,
        ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
        candles_per_window=120,
        empty_rate=0.0,
        # Saturation regime: latency ≈ pool_size / window_rate so the pool can fill while
        # the rate ceiling still binds (see the module docstring's honest-envelope note).
        min_latency_s=0.9,
        max_latency_s=1.1,
        seed=20260626,
    )

    report = run_pull_harness(scenario, lake_root=tmp_path)

    # ── PRIMARY ratio asserts (robust to CI noise) ────────────────────────
    # 1. The rate controller is the BINDING limiter — issued-token slack < 15 %.
    assert report.allowed_rps_timeavg > 0.0
    assert report.achieved_rps >= (1.0 - _MAX_SLACK_RATIO) * report.allowed_rps_timeavg, (
        f"pull did not saturate the allowed rate: achieved(issued) "
        f"{report.achieved_rps} < {1.0 - _MAX_SLACK_RATIO:.2f} × allowed "
        f"{report.allowed_rps_timeavg} (slack_ratio={report.slack_ratio})"
    )
    assert report.slack_ratio < _MAX_SLACK_RATIO

    # 2. The pool's K connections stay busy in the saturation regime.
    assert report.pool_idle_pct <= _MAX_POOL_IDLE_PCT, (
        f"pool idle {report.pool_idle_pct}% > {_MAX_POOL_IDLE_PCT}% "
        f"(in_flight_p50={report.in_flight_p50}, pool_size={report.pool_size})"
    )
    assert report.in_flight_p50 >= (1.0 - _MAX_POOL_IDLE_PCT / 100.0) * report.pool_size

    # 3. The per-second cap held — the trailing-1 s completion peak did not run away.
    assert report.rps_1s_max <= report.allowed_rps_timeavg + BURST + _RPS_1S_MAX_TOLERANCE, (
        f"rps_1s_max {report.rps_1s_max} ran away past ceiling "
        f"{report.allowed_rps_timeavg} + burst {BURST} + tol {_RPS_1S_MAX_TOLERANCE}"
    )

    # ── SECONDARY (loose) sanity — every planned window pulled + data landed. ──
    assert report.windows == _expected_windows(scenario)  # every tile × contract pulled
    assert report.physical_calls >= report.windows  # ≥ one vendor call per window
    assert report.bars_appended > 0
    assert report.rate_limit_hits == 0  # no bursts injected in Gate 1
    assert report.timeout_hits == 0
    # Write latency is a loose secondary (absolute ms varies with the CI disk) — assert
    # only that it was measured + non-negative, never a hard threshold.
    assert report.write_p50_ms >= 0.0
    assert report.write_p99_ms >= report.write_p50_ms


@pytest.mark.bench
def test_harness_smoke_fast_vendor_rate_binds(tmp_path: Path) -> None:
    """A cheap fast-vendor (5-20 ms) run: the rate STILL binds (issued-token slack < 15 %).

    The rate-binding property holds at ANY latency (the bucket issues at ~ceiling whenever
    there is more demand than the cap allows). At the fast-socket default latency the pool
    cannot fill (in-flight ≈ rate × latency ≈ 0.05 by Little's law), so this run does NOT
    assert ``pool_idle`` — only the latency-independent slack. Keeps a quick guard that the
    whole real stack wires up + the meter/controller agree, without the ~2 min saturation
    run.
    """
    scenario = PullScenario(
        n_contracts=6,
        start=date(2024, 10, 1),
        end=date(2025, 1, 1),  # 3 monthly tiles each
        pool_size=4,
        candles_per_window=30,
        min_latency_s=0.005,
        max_latency_s=0.020,
        seed=7,
    )

    report = run_pull_harness(scenario, lake_root=tmp_path)

    assert report.allowed_rps_timeavg > 0.0
    # The bucket is the binding gate even at low latency → issued-token slack < 15 %.
    assert report.achieved_rps >= (1.0 - _MAX_SLACK_RATIO) * report.allowed_rps_timeavg
    assert report.windows == _expected_windows(scenario)
    assert report.bars_appended > 0
    # The physical-call rate is ~half the issued rate (2 paces/window) — the honest gap.
    assert report.physical_call_rps <= report.achieved_rps + 1e-6
