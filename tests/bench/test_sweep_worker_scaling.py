"""Sweep worker-scaling A/B bench — the ADR-0016 §5 deferred core-scaling + RSS bench.

DEC-SWEEP-CAP-BENCH-001 (MIGRATION.md #1356; ADR-0016 Amendment A1).

ADR-0016 (``docs/adr/0016-multicore-sweep-autodefault.md``,
DEC-MULTICORE-SWEEP-AUTODEFAULT-001, MIGRATION.md #1269) shipped the AUTO worker
default ``max(1, min(cpu_count − 2, 8))`` with an explicitly HONEST §5 envelope:
"8-worker THROUGHPUT is NOT benched … the #1080 '~6–8×' remains a PROJECTION
pending the same deferred core-scaling bench", and "48 GB memory safety is
ASSERTED, NOT MEASURED". This file IS that deferred bench: an A/B wall-clock +
RSS measurement of the REAL parallel driver (:func:`run_parallel_grid` — real
``ProcessPoolExecutor``, real writer-actor, real Parquet/audit/checkpoint tail)
at total-worker counts derived from ``AUTO_SWEEP_WORKERS_CAP``:
``cap−1`` (below-cap control), ``cap`` (today's auto ceiling), and the raise
candidates ``10`` and ``12``.

Terminology (kills the 7-vs-8 ambiguity permanently): ``workers`` is the TOTAL
budget = 1 writer-actor + ``workers − 1`` EVAL processes
(``sweep_parallel.py`` cap comment). Today's M4 Pro auto default is
``workers=8`` = 7 EVAL; the "speedup vs 7" axis below is vs the ``workers=7``
below-cap CONTROL arm, and every reported row prints both ``workers`` and
``eval``.

Seam: the ``workers=`` parameter of :func:`run_parallel_grid` ONLY — the env
resolver (``BRUTEX_SWEEP_WORKERS``), the small-grid sequential demotion and the
``_worker_init`` OMP/RAYON/POLARS=1 determinism pins are never touched (they are
load-bearing invariants owned by ``sweep_parallel.py`` and its unit goldens).
Every timed run uses a FRESH results root + FRESH guards — a reused
``SweepCache`` dir would short-circuit every tuple and fake a speedup.

Two tiers (the ``test_m4pro_sweep_walltime.py`` / DEC-P0-020 pattern):

* **Gated A/B body** — runs ONLY when ``BRUTEX_M4PRO_ABS=1`` (the M4 Pro
  self-hosted runner) AND ≥ ``max(counts) + 2`` CPUs are visible. On shared GHA
  or this 4-core sandbox it SKIPS: an oversubscribed or core-limited measurement
  structurally CANNOT prove a real >8-worker win (Golden Rule #10), so no
  decision number can ever be emitted from one. Warmup run first (pays spawn +
  wheel import), then median-of-N ``time.monotonic`` wall clock per arm, RSS via
  ``resource.getrusage``, and a machine-readable ``SWEEP_WORKER_SCALING …`` line
  per arm plus a ``VERDICT:`` line.
* **Ungated tests** — the pure helpers (counts derivation, speedup/verdict math,
  RSS unit normalisation, the env-gate decoder) plus a small smoke A/B that
  drives ``_run_once`` end-to-end at workers ∈ {2, 3} on the 4-tuple unit-test
  grid and asserts the STRUCTURAL invariants unconditionally: the bench
  completes and every worker count produces an IDENTICAL result-tree digest
  (full cross-count byte-identity at {3, 4, 10, 12} is owned by the golden
  ``TestSequentialEqualsParallelGolden`` in
  ``tests/unit/test_engine_sweep_parallel.py`` — extended, never weakened).

DECISION RULE (the ONLY way ``AUTO_SWEEP_WORKERS_CAP`` moves 8 → W): on the
M4 Pro runner with the core guard passed, ``median_wall(cap) / median_wall(W) ≥
RAISE_WIN_THRESHOLD`` (≥ 15% — above scheduler noise, below wishful; the #906
trap guard) for W ∈ {10, 12}, AND the projected RSS at W stays well under the
48 GB envelope, AND the byte-identity golden is green at W. Numbers from any
other machine are UNVERIFIED lab numbers and cannot satisfy the rule. This is an
HONEST MEASUREMENT, not a perf gate: the gated body asserts sanity + digest
equality only — no hard speedup assertion before first evidence exists.
Operator runbook: ``docs/runbooks/sweep-worker-scaling-bench.md``.

FIRST MEASUREMENT (2026-07-05, DEC-SWEEP-CAP-BENCH-001) — VERDICT ``KEEP
CAP=8``: run in a 4-core Linux sandbox (NOT the M4 Pro; ``nproc`` =
``sched_getaffinity`` = 4) via an out-of-band driver over these exact helpers
(the in-repo gate + core guard stayed intact and SKIPPED, as designed). Two
full repetitions, median-of-3 per arm, 24 tuples × 3,750 bars, every arm
``oversubscribed=1``, result digests identical across all arms both times:
workers 7/8/10/12 → 3.166/3.276/3.683/4.186 s (rep 1) and
3.197/3.243/3.732/4.140 s (rep 2) — ``speedup_vs_cap`` at 10 workers
0.889/0.869 and at 12 workers 0.783/0.783, i.e. the raised arms are SLOWER,
nowhere near the ≥ 1.15 threshold. Per the decision rule those numbers are
UNVERIFIED lab evidence by construction (4 usable cores time-slice every
arm) and cannot move the cap in EITHER direction; the M4 Pro run in the
runbook remains the only decision-grade input, and ``AUTO_SWEEP_WORKERS_CAP``
stays 8.
"""

from __future__ import annotations

import math
import os
import resource
import statistics
import sys
import time
from typing import TYPE_CHECKING, Final

import pytest
from brutex.engine.sweep_orchestrator import count_sweep_tuples
from brutex.engine.sweep_parallel import (
    AUTO_SWEEP_WORKERS_CAP,
    AUTO_SWEEP_WORKERS_RESERVED_CPUS,
    run_parallel_grid,
)
from brutex.orchestrator.audit import AuditWriter

# Intentional friend import (the sanctioned `test_m4pro_sweep_walltime.py`
# pattern): the deterministic grid/bars/guards/digest fixtures live in exactly
# ONE place — the sweep_parallel unit+golden suite — and this bench must drive
# the IDENTICAL recipe so its digests are comparable with the golden's
# (CLAUDE.md §11 COMMON: reuse, never copy).
from tests.unit.test_engine_sweep_parallel import (
    _grid,  # pyright: ignore[reportPrivateUsage]
    _guards,  # pyright: ignore[reportPrivateUsage]
    _tree,  # pyright: ignore[reportPrivateUsage]
    _trending_bars,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:  # pragma: no cover - typing only (annotations are lazy)
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.engine.sweep_orchestrator import SweepGridConfig

# Env-var gate — set to "1" on the M4 Pro self-hosted runner (bench-m4pro.yml).
# The SAME gate the sibling absolute/wall-time benches use; no new marker.
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

# The >cap raise candidates under test (ADR-0016 §5's open question is exactly
# "is there a real win ABOVE 8 total workers?"). The below-cap control + cap
# arms are DERIVED from AUTO_SWEEP_WORKERS_CAP in worker_counts(), never
# hardcoded, so a future cap change re-baselines the bench automatically.
_CANDIDATE_RAISED_WORKERS: Final[tuple[int, ...]] = (10, 12)

# Bench grid sizing: 24 tuples ≥ 2 × (12 − 1) EVAL processes, so the small-grid
# heuristic's "~2 tuples per worker" economics hold by construction at the
# largest measured arm; 10 sessions × 375 minute-bars = 3,750 bars per tuple so
# a tuple is real compute, not pure dispatch overhead.
_BENCH_DAYS: Final[int] = 10
_BENCH_BARS_PER_DAY: Final[int] = 375

# Median-of-N timed runs per arm (warmup excluded).
_TIMED_RUNS: Final[int] = 3

# Decision threshold: the cap moves only on a ≥15% median wall-clock win at a
# raised count — above scheduler noise, below wishful (the #906 trap guard).
RAISE_WIN_THRESHOLD: Final[float] = 1.15

# The CLAUDE.md §8 M4 Pro memory envelope the RSS projection is read against.
_RSS_BUDGET_GIB: Final[float] = 48.0

_GIB: Final[int] = 1024**3


def _m4pro_gate_active() -> bool:
    """Return ``True`` iff ``BRUTEX_M4PRO_ABS=1`` is set (the M4 Pro runner)."""
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


skip_unless_m4pro = pytest.mark.skipif(
    not _m4pro_gate_active(),
    reason=(
        f"{_ENV_M4PRO_ABS}=1 not set — the A/B worker-scaling wall-clock body runs "
        "only on the M4 Pro self-hosted runner (skipping on shared GHA / sandbox "
        "to avoid chronic flake per DEC-P0-020 honest-envelope; an oversubscribed "
        "measurement cannot prove a >8-worker win anyway, Golden Rule #10)"
    ),
)


def worker_counts() -> tuple[int, ...]:
    """Measured total-worker arms, sorted ascending.

    Returns
    -------
    tuple[int, ...]
        ``(cap − 1, cap, *raised candidates)`` deduplicated + sorted — the
        below-cap control arm, today's AUTO cap, and the raise candidates.
        Derived from ``AUTO_SWEEP_WORKERS_CAP`` so a future cap change
        re-baselines every arm automatically (never a hardcoded 8).
    """
    return tuple(
        sorted({AUTO_SWEEP_WORKERS_CAP - 1, AUTO_SWEEP_WORKERS_CAP, *_CANDIDATE_RAISED_WORKERS})
    )


def usable_cores() -> int:
    """CPUs genuinely usable by this process.

    Returns
    -------
    int
        ``min(os.cpu_count(), len(sched_getaffinity(0)))`` where the affinity
        mask exists (Linux; a cgroup/taskset-limited runner reports the smaller
        truth), else ``os.cpu_count()`` (macOS has no affinity call). Always
        ≥ 1.
    """
    cpu = os.cpu_count() or 1
    affinity_fn = getattr(os, "sched_getaffinity", None)  # absent on macOS
    if affinity_fn is not None:
        cpu = min(cpu, len(affinity_fn(0)))
    return max(1, cpu)


def oversubscribed(workers: int, cores: int) -> bool:
    """True when *workers* exceeds *cores* — the measurement is then lab-only.

    An oversubscribed arm time-slices EVAL processes on shared cores, so its
    wall clock says nothing about a dedicated-core win (Golden Rule #10); the
    flag is stamped into every machine-readable summary line.
    """
    return workers > cores


def speedup(baseline_s: float, candidate_s: float) -> float:
    """Wall-clock speedup of *candidate* vs *baseline* (> 1 ⇒ candidate faster).

    Raises
    ------
    ValueError
        If ``candidate_s`` is not positive.
    """
    if candidate_s <= 0.0:
        raise ValueError("candidate_s must be positive")
    return baseline_s / candidate_s


def _maxrss_bytes(raw: int, platform: str | None = None) -> int:
    """Normalise a ``ru_maxrss`` reading to BYTES.

    ``getrusage`` reports ``ru_maxrss`` in BYTES on macOS (the M4 Pro runner)
    but KIBIBYTES on Linux — an un-normalised read would be off by 1024× on one
    of the two platforms this bench runs on. *platform* defaults to
    ``sys.platform`` (injectable so both arms are unit-tested everywhere).
    """
    plat = sys.platform if platform is None else platform
    return raw if plat == "darwin" else raw * 1024


def project_total_rss_bytes(parent_peak: int, child_peak: int, total_workers: int) -> int:
    """Project peak whole-sweep RSS at *total_workers* from measured peaks.

    Parameters
    ----------
    parent_peak
        Peak RSS of the parent (writer-actor) process, bytes.
    child_peak
        Peak RSS of the largest single EVAL child, bytes
        (``RUSAGE_CHILDREN.ru_maxrss`` semantics: the max over dead children).
    total_workers
        Total worker budget (1 writer-actor + ``total_workers − 1`` EVAL).

    Returns
    -------
    int
        ``parent_peak + (total_workers − 1) × child_peak`` — a linear
        projection read against the 48 GB envelope; printed, never hard-gated
        (per-tuple RSS is data-dependent).
    """
    return parent_peak + max(0, total_workers - 1) * child_peak


def scaling_verdict(cap_median_s: float, raised_medians_s: Mapping[int, float]) -> str:
    """The throughput arm of the ADR-0016 §5 cap decision rule, as a pure function.

    Parameters
    ----------
    cap_median_s
        Median wall seconds at ``workers = AUTO_SWEEP_WORKERS_CAP``.
    raised_medians_s
        Median wall seconds per raised worker count (> cap arms only).

    Returns
    -------
    str
        ``"RAISE-ELIGIBLE(W)"`` for the LARGEST raised count whose median beats
        the cap arm by ≥ ``RAISE_WIN_THRESHOLD``, else ``"KEEP CAP=<cap>"``.
        Throughput arm ONLY — a real raise additionally requires the RSS
        projection under 48 GB and a green byte-identity golden at W.
    """
    eligible = [
        w
        for w, median_s in sorted(raised_medians_s.items())
        if median_s > 0.0 and cap_median_s / median_s >= RAISE_WIN_THRESHOLD
    ]
    if eligible:
        return f"RAISE-ELIGIBLE({max(eligible)})"
    return f"KEEP CAP={AUTO_SWEEP_WORKERS_CAP}"


def summary_line(
    *,
    workers: int,
    median_s: float,
    tuples_per_s: float,
    baseline_workers: int,
    speedup_vs_baseline: float,
    speedup_vs_cap: float | None,
    cores: int,
    grid_tuples: int,
) -> str:
    """One machine-readable ``SWEEP_WORKER_SCALING`` result line for one arm.

    Every row carries BOTH ``workers`` (total budget) and ``eval`` (= workers − 1
    EVAL processes) plus the detected core count and an ``oversubscribed`` flag,
    so a lab-only number can never be mistaken for a dedicated-core one.
    ``speedup_vs_cap`` may be ``None`` (smoke runs have no cap arm) → ``NA``.
    """
    cap_field = "NA" if speedup_vs_cap is None else f"{speedup_vs_cap:.3f}"
    return (
        f"SWEEP_WORKER_SCALING workers={workers} eval={workers - 1} "
        f"median_s={median_s:.3f} tuples={grid_tuples} tuples_per_s={tuples_per_s:.2f} "
        f"baseline_w={baseline_workers} speedup_vs_baseline={speedup_vs_baseline:.3f} "
        f"speedup_vs_cap={cap_field} cores={cores} "
        f"oversubscribed={int(oversubscribed(workers, cores))}"
    )


def _bench_grid() -> SweepGridConfig:
    """The 24-tuple bench grid — the unit-test grid widened, never redefined.

    1 instrument × 1 timeframe × 2 directions × 3 k-values × 2 SLs × 2 TPs =
    24 tuples ≥ 2 × 11 EVAL processes at the largest arm (workers=12), so the
    per-worker economics the small-grid heuristic encodes hold by construction.
    """
    return _grid(
        k_values=[1, 2, 3],
        condition_bits=[0, 1, 2, 3, 4, 5, 6, 7],
        stop_losses=[80, 100],
        take_profits=[50, 75],
    )


def _bench_bars() -> list[CandleBar]:
    """Deterministic 10-session × 375-minute trending bars (3,750 per tuple)."""
    return _trending_bars(n_days=_BENCH_DAYS, per_day=_BENCH_BARS_PER_DAY)


def _run_once(tmp: Path, grid: SweepGridConfig, bars: Sequence[CandleBar], workers: int) -> float:
    """One REAL parallel sweep in a FRESH root; returns wall seconds.

    Fresh root + fresh guards per call is MANDATORY: a reused ``SweepCache``
    dir short-circuits every tuple on the next run and fakes a speedup. The
    clock (``time.monotonic``) wraps ONLY the driver call and deliberately
    INCLUDES pool spawn + wheel import per arm — spawning more interpreters is
    a REAL cost of more workers and belongs in the measurement.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "out"

    def bars_for(_instrument: str, _tf: str) -> Sequence[CandleBar]:
        return bars

    with (
        AuditWriter.open(tmp / "ingest.jsonl") as ingest,
        _guards(tmp, grid) as guards,
    ):
        start = time.monotonic()
        run_parallel_grid(
            grid,
            bars_for=bars_for,
            root=out,
            ingest_audit=ingest,
            guards=guards,
            workers=workers,
        )
        return time.monotonic() - start


@pytest.mark.slow
@pytest.mark.bench
@skip_unless_m4pro
def test_sweep_worker_scaling_m4pro(tmp_path: Path) -> None:
    """A/B worker-scaling measurement + VERDICT (M4-Pro gated, core-guarded).

    Median-of-N wall clock per total-worker arm over the REAL parallel driver,
    RSS peaks + a 48 GB projection, one machine-readable line per arm, and the
    ADR-0016 §5 decision-rule verdict. Asserts only structural sanity + the
    cross-arm result-digest invariant — an honest MEASUREMENT, not a perf gate.
    """
    counts = worker_counts()
    cores = usable_cores()
    needed = max(counts) + AUTO_SWEEP_WORKERS_RESERVED_CPUS
    if cores < needed:
        pytest.skip(
            f"only {cores} usable CPUs visible (< {needed} = max arm + "
            f"{AUTO_SWEEP_WORKERS_RESERVED_CPUS} reserved) — a "
            f">{AUTO_SWEEP_WORKERS_CAP}-worker measurement here is oversubscribed "
            "and CANNOT prove a real win (Golden Rule #10); run on the 14-core M4 Pro"
        )

    grid = _bench_grid()
    bars = _bench_bars()
    tuples = count_sweep_tuples(grid)
    assert tuples >= 2 * (max(counts) - 1), (
        f"bench grid too small: {tuples} tuples < 2 × {max(counts) - 1} EVAL — "
        "per-worker economics would be dispatch-dominated"
    )

    # Warmup (un-timed): pays the first pool spawn + wheel import + allocator warm.
    _run_once(tmp_path / "warmup", grid, bars, workers=2)

    medians: dict[int, float] = {}
    digests: dict[int, dict[str, str]] = {}
    child_rss: dict[int, int] = {}
    for w in counts:
        runs: list[float] = []
        for r in range(_TIMED_RUNS):
            arm_tmp = tmp_path / f"w{w}_r{r}"
            runs.append(_run_once(arm_tmp, grid, bars, workers=w))
            if r == 0:
                digests[w] = _tree(arm_tmp / "out")
        medians[w] = statistics.median(runs)
        child_rss[w] = _maxrss_bytes(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)

    # Structural invariant: every arm produced the IDENTICAL result tree (the
    # golden owns the exhaustive cross-count byte-identity pin; this is the
    # bench-local cheap check over the trees it already built).
    reference = digests[counts[0]]
    assert reference, "bench produced an empty result tree"
    for w in counts[1:]:
        assert digests[w] == reference, (
            f"result digest at workers={w} diverged from workers={counts[0]} — "
            "determinism regression, do NOT trust the timing numbers"
        )

    baseline_w = counts[0]
    cap = AUTO_SWEEP_WORKERS_CAP
    parent_rss = _maxrss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    child_peak = max(child_rss.values())
    projected = project_total_rss_bytes(parent_rss, child_peak, max(counts))

    print(
        f"\nSweep worker-scaling A/B (ADR-0016 §5 deferred bench) — "
        f"{tuples} tuples × {len(bars)} bars, median of {_TIMED_RUNS}, cores={cores}:"
    )
    for w in counts:
        med = medians[w]
        print(
            "  "
            + summary_line(
                workers=w,
                median_s=med,
                tuples_per_s=tuples / med,
                baseline_workers=baseline_w,
                speedup_vs_baseline=speedup(medians[baseline_w], med),
                speedup_vs_cap=speedup(medians[cap], med),
                cores=cores,
                grid_tuples=tuples,
            )
        )
    print(
        f"  rss parent_peak={parent_rss / _GIB:.3f} GiB "
        f"child_peak={child_peak / _GIB:.3f} GiB "
        "(RUSAGE_CHILDREN cumulative max over ALL dead children incl. warmup — "
        "not per-arm)"
    )
    print(
        f"  rss projected @ workers={max(counts)}: {projected / _GIB:.3f} GiB vs "
        f"{_RSS_BUDGET_GIB:.0f} GiB envelope — printed, NOT hard-gated (data-dependent)"
    )
    raised = {w: m for w, m in medians.items() if w > cap}
    print(
        f"VERDICT: {scaling_verdict(medians[cap], raised)} "
        "(throughput arm only — a cap raise ALSO requires the RSS projection "
        "under 48 GiB above AND the byte-identity golden green at the raised "
        "count; docs/runbooks/sweep-worker-scaling-bench.md)"
    )

    # HONEST sanity only — no hard speedup gate before first evidence exists.
    for med in medians.values():
        assert math.isfinite(med)
        assert med > 0.0
    assert projected > 0


@pytest.mark.bench
def test_run_once_smoke_ab(tmp_path: Path) -> None:
    """CI-runnable smoke A/B: the harness completes + digests match (every runner).

    Drives ``_run_once`` end-to-end at workers ∈ {2, 3} on the small 4-tuple
    unit-test grid (seconds, not minutes) and asserts the STRUCTURAL invariants
    unconditionally: both runs complete with positive finite wall time and
    produce an IDENTICAL non-empty result-tree digest. No wall-clock
    comparison — that lives behind the M4-Pro gate.
    """
    grid = _grid()
    bars = _trending_bars()
    tuples = count_sweep_tuples(grid)
    cores = usable_cores()

    secs_2 = _run_once(tmp_path / "w2", grid, bars, workers=2)
    secs_3 = _run_once(tmp_path / "w3", grid, bars, workers=3)

    tree_2 = _tree(tmp_path / "w2" / "out")
    tree_3 = _tree(tmp_path / "w3" / "out")
    assert tree_2, "smoke run produced an empty result tree"
    assert tree_2 == tree_3

    for w, secs in ((2, secs_2), (3, secs_3)):
        assert math.isfinite(secs)
        assert secs > 0.0
        print(
            "  "
            + summary_line(
                workers=w,
                median_s=secs,
                tuples_per_s=tuples / secs,
                baseline_workers=2,
                speedup_vs_baseline=speedup(secs_2, secs),
                speedup_vs_cap=None,
                cores=cores,
                grid_tuples=tuples,
            )
        )


@pytest.mark.bench
def test_scaling_helpers() -> None:
    """Cover every pure helper on EVERY runner (no M4-Pro gate).

    The counts derivation, grid sizing law, speedup/verdict math, RSS unit
    normalisation + projection, oversubscription flag and the summary-line
    formatter — so the file's logic is fully verified without the M4-Pro
    runner (Golden Rule #10 worst-case hardening).
    """
    counts = worker_counts()
    assert counts == tuple(sorted(set(counts)))
    assert counts[0] == AUTO_SWEEP_WORKERS_CAP - 1  # below-cap control arm
    assert AUTO_SWEEP_WORKERS_CAP in counts
    assert set(_CANDIDATE_RAISED_WORKERS) <= set(counts)
    assert all(w > AUTO_SWEEP_WORKERS_CAP for w in _CANDIDATE_RAISED_WORKERS)
    assert all(w >= 2 for w in counts)  # parallel driver hard-refuses < 2

    # Grid sizing law: ≥ 2 tuples per EVAL process at the largest arm.
    grid = _bench_grid()
    tuples = count_sweep_tuples(grid)
    assert tuples == 24
    assert tuples >= 2 * (max(counts) - 1)
    assert len(_bench_bars()) == _BENCH_DAYS * _BENCH_BARS_PER_DAY

    # speedup: >1 ⇒ candidate faster; degenerate candidate refused.
    assert speedup(10.0, 5.0) == pytest.approx(2.0)
    assert speedup(10.0, 10.0) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="positive"):
        speedup(1.0, 0.0)

    # oversubscription flag + core detection.
    assert oversubscribed(12, 4) is True
    assert oversubscribed(14, 14) is False
    assert oversubscribed(8, 14) is False
    assert usable_cores() >= 1

    # ru_maxrss unit normalisation: bytes on darwin, KiB elsewhere.
    assert _maxrss_bytes(100, platform="darwin") == 100
    assert _maxrss_bytes(100, platform="linux") == 102_400

    # RSS projection: parent + (W−1) × child; degenerate W never negative.
    assert project_total_rss_bytes(2**30, 2**28, 12) == 2**30 + 11 * 2**28
    assert project_total_rss_bytes(2**30, 2**28, 1) == 2**30
    assert project_total_rss_bytes(2**30, 2**28, 0) == 2**30

    # Verdict truth table (threshold 1.15, boundary INCLUSIVE, largest W wins).
    cap = AUTO_SWEEP_WORKERS_CAP
    assert scaling_verdict(11.5, {10: 10.0, 12: 9.9}) == "RAISE-ELIGIBLE(12)"
    assert scaling_verdict(11.5, {10: 10.0, 12: 10.4}) == "RAISE-ELIGIBLE(10)"
    assert scaling_verdict(1.15, {10: 1.0}) == "RAISE-ELIGIBLE(10)"  # exact boundary
    assert scaling_verdict(10.0, {10: 9.5, 12: 9.4}) == f"KEEP CAP={cap}"
    assert scaling_verdict(10.0, {}) == f"KEEP CAP={cap}"
    assert scaling_verdict(10.0, {10: 0.0}) == f"KEEP CAP={cap}"  # no div-by-zero

    # Machine-readable line: both worker axes + the oversubscription stamp.
    line = summary_line(
        workers=10,
        median_s=1.234,
        tuples_per_s=19.45,
        baseline_workers=7,
        speedup_vs_baseline=1.2,
        speedup_vs_cap=1.1,
        cores=14,
        grid_tuples=24,
    )
    assert "SWEEP_WORKER_SCALING workers=10 eval=9 " in line
    assert "baseline_w=7" in line
    assert "speedup_vs_cap=1.100" in line
    assert "oversubscribed=0" in line
    lab = summary_line(
        workers=12,
        median_s=1.0,
        tuples_per_s=24.0,
        baseline_workers=7,
        speedup_vs_baseline=1.0,
        speedup_vs_cap=None,
        cores=4,
        grid_tuples=24,
    )
    assert "speedup_vs_cap=NA" in lab
    assert "oversubscribed=1" in lab


@pytest.mark.bench
def test_m4pro_gate_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env-gate decoder runs on EVERY runner — the gated branch is never dark.

    Mirrors the sibling benches' decoder truth table (Golden Rule #10): only a
    stripped ``"1"`` activates; unset/empty/``0``/``true`` stay inactive.
    """
    for value, expected in [("1", True), (" 1 ", True), ("", False), ("0", False), ("true", False)]:
        monkeypatch.setenv(_ENV_M4PRO_ABS, value)
        assert _m4pro_gate_active() is expected
    monkeypatch.delenv(_ENV_M4PRO_ABS, raising=False)
    assert _m4pro_gate_active() is False
