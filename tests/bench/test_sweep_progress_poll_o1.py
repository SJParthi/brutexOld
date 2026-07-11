"""O(1) bench gate for the G5 progress-poll enrichment (DEC-SWEEP-UNKILLABLE-CORE-001).

:func:`brutex.engine.sweep_exit_status.enrich_progress_payload` runs on EVERY
dashboard ``/api/v1/sweep-progress`` poll (~seconds cadence for a multi-day
run), so it must stay O(1) in RUN SIZE: one small ``exit-status.json`` read,
never a scan of the run directory (flight-recorder lines, tuple stores,
stamps). The gate compares the per-poll latency over a TINY run dir vs a run
dir bloated ~200× (a 20k-line flight recorder + 2,000 sibling artifacts +
2,000 tuple stores) — an O(run-artifacts) regression (e.g. a directory walk
sneaking into the poll) scales with the bloat and blows the ratio.

Timing is best-of-N-MIN (min over repeated batch means — robust against
shared-runner noise spikes); the ceiling routes through the single source of
truth :func:`tests.bench._o1_tiers.ratio_ceiling` (5× shared CI; strict tier
on the M4-Pro runner). HONEST envelope: the poll's constant term is one small
file read + a dict copy — O(payload keys), not literally free; the claim
gated here is independence from RUN SIZE.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Final

from brutex.engine.sweep_exit_status import RunState, enrich_progress_payload, write_exit_status

from tests.bench._o1_tiers import ratio_ceiling

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g5_poll_serves_exit_status_smoke": ("G5",),
    "test_g5_poll_ratio_flat_vs_run_size": ("G5",),
}

_RUN_ID: Final[str] = "d" * 64

#: Sibling-artifact bloat for the LARGE base (≈200× the small base).
_SMALL_ARTIFACTS: Final[int] = 10
_LARGE_ARTIFACTS: Final[int] = 2_000
_LARGE_FLIGHT_LINES: Final[int] = 20_000

#: Best-of-N-MIN timing shape: min over _BATCHES means of _ITERATIONS polls.
_BATCHES: Final[int] = 5
_ITERATIONS: Final[int] = 200


def _build_run_dir(root: Path, *, artifacts: int, flight_lines: int) -> None:
    """Materialise a run tree: exit-status + a flight file + sibling artifacts."""
    write_exit_status(
        root,
        run_id=_RUN_ID,
        state=RunState.RUNNING,
        attempt=1,
        pid=4321,
        clock=lambda: 1000.0,
    )
    run_dir = root / "runs" / _RUN_ID
    flight = run_dir / "flight-recorder.jsonl"
    line = '{"seq": 1, "action": "TUPLE_DONE", "payload": {"index": 1}}\n'
    flight.write_text(line * flight_lines, encoding="utf-8")
    for i in range(artifacts):
        (run_dir / f"artifact-{i}.json").write_text("{}", encoding="utf-8")
        (root / f"bull_k3_{i:08d}.parquet").write_bytes(b"PARQ")


def _progress_payload() -> dict[str, Any]:
    return {
        "state": "running",
        "enumerated": 1_000_000,
        "evaluated": 500,
        "percent": 0.05,
        "run_id": _RUN_ID,
    }


def _time_poll(root: Path) -> float:
    """Best-of-N-MIN per-poll latency (ns) for enrich_progress_payload at *root*."""
    payload = _progress_payload()
    for _ in range(25):  # warm (page cache + code paths)
        enrich_progress_payload(payload, results_root=root)
    best = float("inf")
    for _ in range(_BATCHES):
        t0 = time.perf_counter_ns()
        for _ in range(_ITERATIONS):
            enrich_progress_payload(payload, results_root=root)
        t1 = time.perf_counter_ns()
        best = min(best, (t1 - t0) / _ITERATIONS)
    return best


def test_g5_poll_serves_exit_status_smoke(tmp_path: Path) -> None:
    """Correctness smoke: the enriched poll carries the artifact's exact state."""
    _build_run_dir(tmp_path, artifacts=_SMALL_ARTIFACTS, flight_lines=100)
    out = enrich_progress_payload(_progress_payload(), results_root=tmp_path)
    status = out["exit_status"]
    assert status is not None
    assert status["state"] == "running"
    assert status["pid"] == 4321
    assert out["suspect"] is False


def test_g5_poll_ratio_flat_vs_run_size(tmp_path: Path) -> None:
    """One poll costs the same over a ~200×-bloated run dir (O(1) in run size)."""
    small = tmp_path / "small"
    large = tmp_path / "large"
    small.mkdir()
    large.mkdir()
    _build_run_dir(small, artifacts=_SMALL_ARTIFACTS, flight_lines=100)
    _build_run_dir(large, artifacts=_LARGE_ARTIFACTS, flight_lines=_LARGE_FLIGHT_LINES)
    t_small = _time_poll(small)
    t_large = _time_poll(large)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"enrich_progress_payload ratio {ratio:.2f}× (bloated vs tiny run dir) exceeds "
        f"the ceiling — the poll may have regressed from one O(1) exit-status read to "
        f"an O(run-artifacts) scan. t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
    )
