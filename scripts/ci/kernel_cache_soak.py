#!/usr/bin/env python3
"""kernel_cache_soak.py — the kernel-cache OFF↔ON A/B soak driver + report tool.

DEC-SWEEP-WORKER-KERNEL-CACHE-SOAK-001 (the §8 M4-Pro soak harness feeding the
DEC-SWEEP-WORKER-KERNEL-CACHE-001 / MIGRATION.md #1729 default-ON flip DEC).
Runs the SAME ``brutex sweep-all`` grid twice as SEPARATE subprocesses — arm
OFF (``BRUTEX_SWEEP_WORKER_KERNEL_CACHE=0``) then arm ON (``=1``) — against
FRESH per-arm results roots (``run_id = grid_identity(grid)`` has NO
timestamp, so a shared root would resume/cache-skip the second arm into a
fake instant "win"; the ``test_sweep_worker_scaling`` fresh-state law), while
a parent-side sampler polls ``ps`` for per-worker + pool-wide RSS, then
harvests the #1729 telemetry (``sweep_parallel.kernel_reuse_summary`` /
``worker_kernel_built``), byte-compares the two result trees ts-masked (the
PR-A T3 identity law), and writes ONE machine-readable ``soak-report.json``
plus a MEASURED-tagged ``summary.md``.

The acceptance block is coordinator-blessed for the HARNESS + BARS only —
the default-ON FLIP itself is NOT pre-approved: it is a separate later
one-constant DEC that quotes this report's raw figures and needs operator
sign-off (the DEC-SWEEP-RUST-METRICS-001 → -DEFAULT-ON-001 two-step
precedent).

Subcommands / exit codes (the ``bench_baseline_gate.py`` rc convention)
-----------------------------------------------------------------------
``run``      preflight → arm OFF → arm ON → harvest → compare → report.
             0 = both arms complete + every judged acceptance check green +
             report written / 1 = an acceptance-relevant failure (identity
             diff, arm crash, budget overrun — the report is STILL written) /
             2 = STRUCTURAL (preflight refusal, bad usage — nothing ran).
``verdict``  re-evaluate the acceptance block from an existing
             soak-report.json (0 judged-green / 1 judged-red / 2 structural).

HONEST LIMITS (Golden Rule #10)
-------------------------------
* Per-PID peak RSS via ``ps`` polling is a LOWER BOUND on the true peak (a
  sub-interval spike can slip between samples) — stamped into the report as
  ``rss_method``.
* ``--smoke`` proves the script MECHANICS on a tiny real-writer fixture lake;
  its numbers are NEVER decision-grade — the report stamps ``smoke: true``,
  the RSS/wall bars are NOT judged, and the verdict can never read
  FLIP-READY. "Soak-passed" cannot be claimed from a smoke run.
* The full-grid wall saving stays MODELED (extrapolated off the bounded soak
  grid); the RSS evidence keeps FULL bar depth (RSS is bar-DEPTH-driven, not
  tuple-count-driven) but is SCOPED to the MEASURED kernel-cache capacity —
  ``provenance.worker_kernel_capacity_effective`` (the grid-derived default,
  EXPLICITLY PINNED into both arm envs: ``arm_env`` SETS
  ``BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY=<default>`` because the child's own
  ``bootstrap_env()`` dotenv re-read (repo ``.env`` / cwd ``.env`` /
  ``~/.brutex/.env``) re-injects any file value into an UNSET var — popping
  only covered inheritance; ``apply_dotenv`` never overrides a SET var, so
  the explicit pin is dotenv-immune — fix-round-2 MAJOR-1). The default
  grid measures capacity=1; widen ``--fib-anchors`` / ``--price-source-cells``
  to soak a higher capacity — the flip DEC must weigh whether the measured
  capacity covers the operator's grids (R2 MAJOR-2 honesty scoping).
* The wall bar carries an ORDERING bias: the second arm (default: ON) starts
  with a warm OS page-cache/import state — systematic, inside the ≤ 1.02
  band, bounded by the preload share of a 2-4 h arm (fix-round-3 NIT-1;
  disclosed on the summary; ``--arm-order on-first`` reverses it for a
  bias check).
* The ABSOLUTE RSS/pool bars are measured at PINNED sizing: ``arm_env``
  explicitly sets ``BRUTEX_SWEEP_WORKERS`` + ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``
  (the parent-resolved DEFAULT laws — the mem budget through the engine's own
  resolver at the eval-pool divisor ``workers − 1``, the divisor the driver
  itself exports per worker; fix-round-4 MAJOR corrected the round-3 ÷N pin)
  beside the capacity pin, and the SEVEN ``BRUTEX_SWEEP_TRIPWIRE_*``
  SUSPECT-latch thresholds at the engine defaults (fix-round-4 MINOR — they
  shape the clean-exit bar itself), so a stray ambient/``.env`` value can
  never silently re-scope the bars OR the judgment (fix-round-3 MINOR-1);
  the remaining symmetric sweep envs are echoed into
  ``provenance.symmetric_env_echo`` (parent-environ view — dotenv values
  are invisible there, which is exactly why the sizing/tripwire envs are
  pinned).
* A non-smoke report whose ``hardware_key`` is not the registered §8 M4-Pro
  envelope — or whose key came from the ``BRUTEX_BENCH_BASELINE_KEY`` env
  OVERRIDE rather than real hardware probes — can never read FLIP-READY: the
  verdict reads NOT-DECISION-GRADE (numbers from any other machine are
  UNVERIFIED lab evidence, the DEC-SWEEP-CAP-BENCH-001 clause). TRUSTED-INPUT
  BOUNDARY: ``soak-report.json`` is evidence for the flip DEC, not a
  tamper-proof artifact — the operator runs the harness on their own box;
  the ``verdict`` subcommand re-derives from the report it is handed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

REPORT_SCHEMA_VERSION: Final[int] = 1
REPORT_KIND: Final[str] = "kernel-cache-soak"

#: The #1729 flag + the env knobs each arm is spawned with.
KERNEL_CACHE_ENV: Final[str] = "BRUTEX_SWEEP_WORKER_KERNEL_CACHE"
SWEEP_WORKERS_ENV: Final[str] = "BRUTEX_SWEEP_WORKERS"
LOG_LEVEL_ENV: Final[str] = "BRUTEX_LOG_LEVEL"
RESULTS_DEPTH_ENV: Final[str] = "BRUTEX_RESULTS_DEPTH"
#: The per-box capacity override lever (sweep_parallel.WORKER_KERNEL_CAPACITY_ENV).
#: EXPLICITLY PINNED to the grid-derived default in both arm envs (R2 MAJOR-1,
#: fix-round-2 MAJOR-1): the flip ships the DEFAULT sizing law, so the
#: evidence must measure it — an ambient operator override OR a ``.env``-file
#: value (the child ``brutex sweep-all`` runs ``bootstrap_env()``, which
#: re-injects a dotenv value into any UNSET var) would silently re-scope the
#: RSS bar. ``apply_dotenv`` never overrides a SET var, so the explicit pin
#: is dotenv-immune where a pop was not. Drift-pinned against the engine
#: constant in tests/unit/test_kernel_cache_soak.py (TestEngineDriftPins).
KERNEL_CAPACITY_ENV: Final[str] = "BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY"
#: Mirror of sweep_parallel.WORKER_KERNEL_CAPACITY_MAX (drift-pinned).
WORKER_KERNEL_CAPACITY_MAX: Final[int] = 64
#: Mirror of sweep_mem_budget.MEM_BUDGET_ENV (drift-pinned): the rank-region
#: byte budget. Fix-round-3 MINOR-1 (refuter-A): a ``.env``-planted value
#: reached BOTH children symmetrically (never attributable to the flag) but
#: silently RE-SCOPED the ABSOLUTE RSS/pool bars away from the DEFAULT sizing
#: law the flip ships — so ``arm_env`` now PINS the parent-resolved default
#: (:func:`resolve_pinned_mem_budget`) into both arms, mirroring the capacity
#: precedent. The engine treats a SET value as the per-worker override
#: (never divided), so pinning the default-law per-worker figure is
#: byte-identical to the engine's own default — which divides by the
#: EVAL-POOL size ``workers − 1``, NOT by ``workers`` (fix-round-4 MAJOR,
#: refuter-A: the round-3 pin used the wrong divisor; see
#: :func:`resolve_pinned_mem_budget`).
MEM_BUDGET_ENV: Final[str] = "BRUTEX_SWEEP_MEM_BUDGET_BYTES"
#: Mirror of run_layout.MIRROR_ROOT_ENV (drift-pinned): the Row-15 forensics
#: mirror root. Fix-round-3 NIT (refuter-A): both arms share ``run_id``
#: (grid identity — the cache flag is env, not grid), so the default shared
#: ``~/.brutex/runs/<run_id>/`` mirror was CLOBBERED cross-arm (write-only
#: residue, no result coupling); each arm now mirrors into its OWN
#: ``<arm>/mirror/`` dir instead — per-arm forensics kept, the operator's
#: shared mirror root untouched by a soak.
RUN_MIRROR_ROOT_ENV: Final[str] = "BRUTEX_RUN_MIRROR_ROOT"
#: The SEVEN sweep tripwire threshold knobs (observability/run_tripwires.py
#: ``TripwireConfig.from_env`` — read EAGERLY in every ``sweep-all`` child).
#: Fix-round-4 MINOR (refuter-A): these shape the SUSPECT latch — the
#: CLEAN-EXIT bar itself (a loosened ``THROUGHPUT_FLOOR`` silences a genuine
#: tripwire; a tightened ``RSS_BUDGET_FRACTION`` fires a spurious SUSPECT →
#: judged FAIL), so like the sizing envs they are PINNED to the ENGINE
#: DEFAULTS in both arm envs (:func:`resolve_pinned_tripwires` — dotenv-immune
#: on the capacity precedent, values round-trip byte-identical through the
#: engine's own ``from_env`` parser, drift-pinned), never merely echoed.
TRIPWIRE_ENV_PINS: Final[tuple[str, ...]] = (
    "BRUTEX_SWEEP_TRIPWIRE_RECONCILE_INTERVAL_S",
    "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_FLOOR",
    "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_DWELL",
    "BRUTEX_SWEEP_TRIPWIRE_RSS_BUDGET_FRACTION",
    "BRUTEX_SWEEP_TRIPWIRE_MEMORY_SLOPE_BYTES",
    "BRUTEX_SWEEP_TRIPWIRE_MEMORY_DWELL",
    "BRUTEX_SWEEP_TRIPWIRE_ABSURD_PNL_PAISA",
)
#: The AUDITED symmetric sweep-reachable env knobs (fix-round-3 MINOR-1
#: audit, refuter-A's enumeration; fix-round-4 adds the two sweep-path Rust
#: toggles ``BRUTEX_FEED_RUST`` — the feed/replay bar-load choke point — and
#: ``BRUTEX_STRATEGIES_RUST_COMBO``, the same class as the already-echoed
#: ``BRUTEX_SWEEP_RUST_METRICS``/``_RUST_STAGE1A``; fix-round-5 adds five
#: more wall-posture Rust toggles from refuter-A's round-4 re-enumeration —
#: ``BRUTEX_CORE_RUST_MONEY``/``BRUTEX_CORE_RUST_HWENV``/``BRUTEX_DATA_RUST``
#: /``BRUTEX_LAKE_RUST_LOCKS`` proven consulted in the harness's OWN smoke
#: child via their ``flag_unrecognised`` warns, plus ``BRUTEX_STATS_RUST``
#: statically sweep-reachable through the cascade CPCV scoring path):
#: each reaches BOTH arms
#: identically (same base env + same dotenv application in both children), so
#: none can skew ARM IDENTITY or attribute an effect to the flag — they are
#: deliberately NOT pinned (behavior toggles are the operator's box posture)
#: but are ECHOED into ``provenance.symmetric_env_echo`` so the report states
#: the sizing/behavior posture its absolute numbers were measured under.
#: COMPLETENESS ENVELOPE (honest, fix-round-5): this list covers the
#: ENUMERATED audit — refuter-A's grep re-enumeration of the ``BRUTEX_*``
#: namespace + the executed in-child ``flag_unrecognised`` probes
#: (rounds 3-4, 2026-07-19) — it is NOT a proof of absolute completeness.
#: Any un-enumerated env remains SYMMETRIC across both arms by the
#: ``arm_env`` construction itself (same base env + same dotenv application
#: in both children), so a missed name can dull the echo's posture
#: disclosure but can never skew arm identity or attribute an effect to the
#: flag; symmetry, not enumeration, is the load-bearing property.
#: HONEST LIMIT: the echo is the PARENT-environ view — a ``.env``-file value
#: the child's own ``bootstrap_env()`` injects is invisible here (disclosed
#: in-report via ``symmetric_env_echo_scope``); the arm-identity-relevant
#: sizing + tripwire envs are PINNED instead, exactly because the echo cannot
#: see dotenv.
SYMMETRIC_ENV_ECHO: Final[tuple[str, ...]] = (
    "BRUTEX_SWEEP_BOUNDED_RANK",
    "BRUTEX_SWEEP_BUDGET_WORKERS",
    "BRUTEX_SWEEP_RUST_METRICS",
    "BRUTEX_SWEEP_RUST_STAGE1A",
    "BRUTEX_SWEEP_APRIORI",
    "BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS",
    "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY",
    "BRUTEX_SWEEP_CACHE_MAP_MB",
    "BRUTEX_SWEEP_DIFFCHECK",
    "BRUTEX_SWEEP_DIFFCHECK_K",
    "BRUTEX_SWEEP_STALL_SECONDS",
    "BRUTEX_SWEEP_STALL_FACTOR",
    "BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC",
    "BRUTEX_SWEEP_PER_TUPLE_SECONDS",
    "BRUTEX_SWEEP_LOOKAHEAD_GUARD",
    "BRUTEX_SWEEP_RESUME_STORE_GUARD",
    "BRUTEX_SWEEP_SMALL_GRID_FACTOR",
    "BRUTEX_SWEEP_DETACHED",
    "BRUTEX_BAD_TICK_SCREEN",
    "BRUTEX_LAKE_HEAL",
    "BRUTEX_MAX_BLOOM_BYTES",
    "BRUTEX_POLARS_THREADS",
    "BRUTEX_DUCKDB_THREADS",
    "BRUTEX_FEED_RUST",
    "BRUTEX_STRATEGIES_RUST_COMBO",
    "BRUTEX_CORE_RUST_MONEY",
    "BRUTEX_CORE_RUST_HWENV",
    "BRUTEX_DATA_RUST",
    "BRUTEX_LAKE_RUST_LOCKS",
    "BRUTEX_STATS_RUST",
)
#: The hardware-key env override (hardware_envelope.BENCH_BASELINE_KEY_ENV,
#: drift-pinned): when set, ``hardware_key()`` returns it VERBATIM — a forged
#: M4-Pro key would flip the provenance bar on any box, so a non-smoke report
#: whose key came from this override reads NOT-DECISION-GRADE (fix-round-2
#: MINOR — the refuter-1 spoof).
BENCH_BASELINE_KEY_ENV: Final[str] = "BRUTEX_BENCH_BASELINE_KEY"
#: The results-depth sibling root dirname (trade_store.SWEEP_RUNS_DIRNAME,
#: drift-pinned): in NON-smoke mode the #1617 results-depth pass writes its
#: flip-facing artifacts (top100.json, per-tuple trade/equity sidecars,
#: baselines) to ``<results_root>/../sweep_runs/<run_id>/`` — a SIBLING of
#: the compared results root (the Ruling-4 sibling-root law), so the identity
#: compare walks it too (fix-round-2 MAJOR — the refuter-2 coverage hole).
SWEEP_RUNS_DIRNAME: Final[str] = "sweep_runs"

#: Telemetry tokens harvested from each arm's stderr (structlog console lines).
SUMMARY_EVENT: Final[str] = "sweep_parallel.kernel_reuse_summary"
BUILT_EVENT: Final[str] = "sweep_parallel.worker_kernel_built"
#: Abort tokens whose PRESENCE in either log fails the clean-exit check.
ABORT_TOKENS: Final[tuple[str, ...]] = ("SweepWorkerPoolBrokenError", "SweepStallError")

# ── Acceptance ceilings (coordinator-confirmed bars; the flip DEC judges the
#    RAW figures — the harness only reports + pre-evaluates) ─────────────────
#: ON−OFF per-worker peak-RSS delta ceiling: 0.8 GiB in KiB — the ledgered
#: MODELED worst case ~0.66–0.73 GB/worker (sweep_parallel.py capacity law)
#: plus ~10% headroom.
RSS_PER_WORKER_DELTA_CEILING_KIB: Final[int] = 838_861
#: ON-arm pool-wide peak ceiling: 36 GiB in KiB — 75% of the §8 48 GB
#: envelope (leaves OS + writer-actor + rank-region headroom).
POOL_PEAK_CEILING_KIB: Final[int] = 36 * 1024 * 1024
#: Wall no-regression band: ON/OFF ratio must stay ≤ 1.02. A within-noise
#: no-win flip is a YELLOW FLAG surfaced by the VALUE section — the harness
#: never gates on a WIN (measured value visibility, not wishful gating).
WALL_RATIO_CEILING: Final[float] = 1.02
#: The registered §8 M4-Pro envelope's hardware-key signature — cores/RAM/arch
#: per the ONE ``format_hardware_key`` law (``…-14c10p4e-48g-arm64``; the Mac
#: model-slug PREFIX is left free — model IDs vary across otherwise-identical
#: boxes, and no committed bench baseline exists yet to read a full key from).
#: A NON-smoke report whose key does not carry this suffix reads
#: NOT-DECISION-GRADE, never FLIP-READY (R2 MINOR-4 / runbook §5 bar 6).
M4PRO_KEY_SUFFIX: Final[str] = "-14c10p4e-48g-arm64"
#: The workflow plan-job budget fence, mirrored script-side (R2 MINOR-5): the
#: operator one-command fallback must obey the SAME 1..1195 law (SEC9-1 — the
#: derived job timeout m + 45 + m/6 must stay under the 24-h token validity;
#: a zero/negative budget would SIGTERM both arms instantly).
BUDGET_MINUTES_MIN: Final[int] = 1
BUDGET_MINUTES_MAX: Final[int] = 1195
#: The NON-smoke free-disk requirement (GiB) + its operator-override fence
#: (fix-round-2 NIT): the fixed 30 GiB default is right-sized for the M4-Pro
#: default grid and the WORKFLOW never passes the override — lowering it via
#: ``--min-free-gib`` is an operator judgment for a deliberately tiny bounded
#: non-smoke grid (or sandbox validation), fenced 1..1024 and echoed into
#: ``provenance.min_free_gib`` so the report discloses it. Smoke keeps its own
#: fixed 2 GiB need (the fixture lake is tiny).
MIN_FREE_GIB_DEFAULT: Final[int] = 30
MIN_FREE_GIB_MIN: Final[int] = 1
MIN_FREE_GIB_MAX: Final[int] = 1024
#: The ``--sample-interval-s`` fence (fix-round-6 MINOR, refuter-A): this was
#: the ONE plan-job-fenced input with NO script-side twin — the workflow plan
#: job fences it to integer 1..300, but the CLI fallback accepted anything
#: ``float()`` parses. Executed repros: 0/negative/nan/1e-9 make
#: ``Event.wait`` return immediately, so the sampler forks ``ps``
#: continuously beside the measured arms (a CPU/fork storm — 323k spawns/s
#: measured — inflating the ABSOLUTE walls/RSS the flip DEC quotes); ``inf``
#: raises ``OverflowError`` inside ``Event.wait`` and KILLS the daemon
#: sampler after exactly ONE sample (near-empty sampling presented as
#: MEASURED). Same law as the ``--budget-minutes`` R2 MINOR-5 precedent:
#: enforce script-side what the workflow plan job fences. The script floor is
#: 0.05 (below the workflow's 1) ONLY so the documented smoke sub-second
#: cadence (``min(interval, 0.5)``) stays a legal value an operator may also
#: pass explicitly.
SAMPLE_INTERVAL_S_MIN: Final[float] = 0.05
SAMPLE_INTERVAL_S_MAX: Final[float] = 300.0

# ── The ts-masked identity-compare law (the PR-A T3 / runcore-byte-identity
#    exclusion set, extended with the CLI-layout state dirs) ─────────────────
RESULT_TREE_EXCLUDE_PREFIXES: Final[tuple[str, ...]] = (
    "runs/",
    ".locks/",
    ".quarantine/",
    "progress/",
    "audit/",
    "checkpoint/",
    "dedup-cache/",
)
RESULT_TREE_EXCLUDE_SUFFIXES: Final[tuple[str, ...]] = (".stamp.json",)
RESULT_TREE_EXCLUDE_NAMES: Final[tuple[str, ...]] = ("stage2",)
#: Wall-clock-volatile keys scrubbed from the RUN_MANIFEST audit payload's
#: embedded manifest before the ts-masked chain compare (empirically the ONLY
#: cross-arm diffs on an identical grid: per-stage timings + update stamps).
VOLATILE_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"updated_utc", "started_utc", "per_stage_timings", "wall_started_utc", "wall_ended_utc"}
)
#: Sidecars compared STRUCTURALLY (never byte-wise, never silently skipped) —
#: listed verbatim in the report's ``compare.excluded_sidecars``.
STRUCTURAL_SIDECARS: Final[tuple[str, ...]] = (
    "runs/<run_id>/exit-status.json (state/exit_code/suspect compared; pid + wall stamps masked)",
    "runs/<run_id>/manifest.json (via the RUN_MANIFEST audit payload, volatile keys scrubbed)",
    "runs/<run_id>/flight-recorder.jsonl + chain-map.json + diagnostic-bundle.zip (wall-clock-bearing run-core forensics — outside the result-identity law)",
    "audit/*.jsonl (compared ts-masked: seq/action/subject/payload)",
    "*.stamp.json + stage2 (wall-stamped content-hash sidecars — masked under "
    "BOTH compared roots: results/ AND the results-depth sweep_runs/ sibling)",
    "checkpoint/ + progress/ + dedup-cache/ + .locks/ (run state, pid/mtime-bearing)",
)

_ANSI_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*m")
_KV_RE: Final[re.Pattern[str]] = re.compile(r"(\w+)=(\S+)")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: RSS sampling method disclosure (Golden Rule #10) — formatted with the
#: actual cadence at report time.
RSS_METHOD_TEMPLATE: Final[str] = "ps-poll-{interval}s (peak is a LOWER BOUND on the true peak)"


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers — unit-tested standalone (tests/unit/test_kernel_cache_soak.py)
# ─────────────────────────────────────────────────────────────────────────────


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR escapes (structlog ConsoleRenderer colours stderr)."""
    return _ANSI_RE.sub("", text)


def parse_structlog_kv(line: str, event: str) -> dict[str, str] | None:
    """Return the ``key=value`` map of a console-rendered structlog *event* line.

    ``None`` when *line* does not carry *event* as a whole token. Values are
    returned as strings; the caller coerces. ANSI escapes are stripped first.
    """
    clean = strip_ansi(line)
    tokens = clean.split()
    if event not in tokens:
        return None
    return dict(_KV_RE.findall(clean.split(event, 1)[1]))


@dataclass(frozen=True)
class Telemetry:
    """Harvest of one arm's stderr log."""

    summaries: tuple[dict[str, int], ...]
    built_events: int
    abort_tokens: tuple[str, ...]


def harvest_telemetry(log_text: str) -> Telemetry:
    """Parse an arm's stderr for the #1729 telemetry + abort tokens."""
    summaries: list[dict[str, int]] = []
    built = 0
    for line in log_text.splitlines():
        kv = parse_structlog_kv(line, SUMMARY_EVENT)
        if kv is not None:
            summary: dict[str, int] = {}
            for key in ("builds_total", "hits_total", "workers"):
                try:
                    summary[key] = int(kv.get(key, ""))
                except ValueError:
                    summary[key] = -1  # malformed → visibly wrong, never silent
            summaries.append(summary)
            continue
        if parse_structlog_kv(line, BUILT_EVENT) is not None:
            built += 1
    clean = strip_ansi(log_text)
    aborts = tuple(token for token in ABORT_TOKENS if token in clean)
    return Telemetry(summaries=tuple(summaries), built_events=built, abort_tokens=aborts)


def parse_ps_lines(text: str) -> list[tuple[int, int, int]]:
    """``(pid, ppid, rss_kib)`` rows from ``ps -eo pid=,ppid=,rss=`` output.

    Malformed rows are skipped (a racing process exit can truncate a row).
    ``ps`` reports RSS in KiB on BOTH macOS and Linux — one code path.
    """
    rows: list[tuple[int, int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return rows


def descendant_pids(rows: list[tuple[int, int, int]], root_pid: int) -> set[int]:
    """*root_pid* plus every transitive child visible in *rows* (BFS)."""
    children: dict[int, list[int]] = {}
    for pid, ppid, _rss in rows:
        children.setdefault(ppid, []).append(pid)
    out = {root_pid}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, ()):
            if child not in out:
                out.add(child)
                frontier.append(child)
    return out


@dataclass
class RssTracker:
    """Running per-PID max + pool-peak fold over ``ps`` samples (pure fold)."""

    root_pid: int
    per_pid_max_kib: dict[int, int] = field(default_factory=dict)
    pool_peak_kib: int = 0
    samples: int = 0

    def fold(self, rows: list[tuple[int, int, int]]) -> None:
        """Fold ONE ps snapshot: update per-PID running maxima + pool peak."""
        in_tree = descendant_pids(rows, self.root_pid)
        pool_sum = 0
        seen_any = False
        for pid, _ppid, rss in rows:
            if pid not in in_tree:
                continue
            seen_any = True
            pool_sum += rss
            if rss > self.per_pid_max_kib.get(pid, 0):
                self.per_pid_max_kib[pid] = rss
        if seen_any:
            self.samples += 1
            self.pool_peak_kib = max(self.pool_peak_kib, pool_sum)

    def worker_peaks_kib(self) -> dict[int, int]:
        """Per-PID peaks EXCLUDING the root arm process (= the pool workers)."""
        return {pid: kib for pid, kib in self.per_pid_max_kib.items() if pid != self.root_pid}

    def worker_peak_max_kib(self) -> int | None:
        """Max single-worker peak, or ``None`` when no worker was ever sampled."""
        peaks = self.worker_peaks_kib()
        return max(peaks.values()) if peaks else None


def maxrss_to_kib(raw: int, platform: str | None = None) -> int:
    """Normalise a ``ru_maxrss`` reading to KiB (bytes on darwin, KiB on Linux)."""
    plat = sys.platform if platform is None else platform
    return raw // 1024 if plat == "darwin" else raw


def _tree_excluded(rel: str) -> bool:
    if any(rel.startswith(prefix) for prefix in RESULT_TREE_EXCLUDE_PREFIXES):
        return True
    if any(rel.endswith(suffix) for suffix in RESULT_TREE_EXCLUDE_SUFFIXES):
        return True
    return Path(rel).name in RESULT_TREE_EXCLUDE_NAMES


def result_tree(root: Path) -> dict[str, str]:
    """SHA-256 per result file under *root*, run-state/sidecar paths excluded.

    The PR-A T3 identity law: everything OUTSIDE the excluded prefixes must be
    byte-identical between the OFF and ON arms.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _tree_excluded(rel):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def sweep_runs_tree(arm_dir: Path) -> dict[str, str]:
    """SHA-256 per file under ``<arm>/sweep_runs/`` — the results-depth root.

    Fix-round-2 MAJOR (refuter-2): the #1617 results-depth pass writes its
    flip-facing artifacts (top100.json, per-tuple trade/equity sidecars,
    baselines) to the ``sweep_runs/`` SIBLING of the compared results root —
    previously never audited. Only the wall-stamped ``.stamp.json`` sidecars
    (+ the ``stage2`` name) are masked, exactly the suffix/name arm of the
    existing exclusion law — empirically the ONLY cross-arm diffs in that
    tree. Keys are ``sweep_runs/<run_id>/…`` rel-paths; ``{}`` when the dir
    is absent (smoke runs results-depth OFF).
    """
    root = arm_dir / SWEEP_RUNS_DIRNAME
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(arm_dir).as_posix()
        if any(rel.endswith(suffix) for suffix in RESULT_TREE_EXCLUDE_SUFFIXES):
            continue
        if Path(rel).name in RESULT_TREE_EXCLUDE_NAMES:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def arm_identity_tree(arm_dir: Path) -> dict[str, str]:
    """The FULL per-arm identity tree: ``results/`` + ``sweep_runs/``.

    ``results/<rel>`` keys ride :func:`result_tree`'s full exclusion law;
    ``sweep_runs/<rel>`` keys ride :func:`sweep_runs_tree`'s suffix/name law.
    ONE map so :func:`tree_diff` covers both roots — nothing is silently
    outside the compare.
    """
    out = {f"results/{rel}": digest for rel, digest in result_tree(arm_dir / "results").items()}
    out.update(sweep_runs_tree(arm_dir))
    return out


def tree_diff(off: dict[str, str], on: dict[str, str]) -> list[str]:
    """Sorted rel-paths that are missing on one side or hash-different."""
    return sorted(rel for rel in set(off) | set(on) if off.get(rel) != on.get(rel))


def scrub_manifest(manifest: object) -> object:
    """Drop the wall-clock-volatile top-level keys from a run manifest dict."""
    if not isinstance(manifest, dict):
        return manifest
    return {k: v for k, v in manifest.items() if k not in VOLATILE_MANIFEST_KEYS}


def masked_audit_entries(path: Path) -> list[tuple[int, str, str, object]]:
    """The CLI ingest chain reduced to ``(seq, action, subject, payload)``.

    Timestamps/hashes are structurally outside the tuple; the ONE
    wall-clock-bearing payload (``RUN_MANIFEST`` — its embedded manifest
    carries per-stage timings + update stamps, and its content hash covers
    those bytes) is scrubbed via :func:`scrub_manifest` with the hash masked.
    """
    from brutex.orchestrator.audit import iter_entries  # lazy: needs the installed wheel env

    out: list[tuple[int, str, str, object]] = []
    for entry in iter_entries(path):
        payload: object = entry.payload
        if entry.action == "RUN_MANIFEST" and isinstance(payload, dict):
            payload = {
                "manifest": scrub_manifest(payload.get("manifest")),
                "manifest_content_hash": "<masked-volatile>",
            }
        out.append((entry.seq, entry.action, entry.subject, payload))
    return out


def exit_status_structural(off: dict[str, Any], on: dict[str, Any]) -> tuple[bool, str]:
    """Structural exit-status compare: state/exit_code/suspect equal AND clean."""
    keys = ("state", "exit_code", "suspect")
    off_view = {k: off.get(k) for k in keys}
    on_view = {k: on.get(k) for k in keys}
    if off_view != on_view:
        return False, f"exit-status diverged: OFF={off_view!r} ON={on_view!r}"
    if on_view != {"state": "complete", "exit_code": 0, "suspect": False}:
        return False, f"exit-status not clean: {on_view!r}"
    return True, "state/exit_code/suspect equal + clean"


def _csv_count(csv: str) -> int:
    """Non-blank token count of a comma-separated axis value."""
    return len([tok for tok in csv.split(",") if tok.strip()])


def keys_fan_out(symbols: str, intervals: str, fib_anchors: str, price_source_cells: str) -> int:
    """The grid's kernel-key fan-out — the ``builds ≤ keys × workers`` bound.

    A resident kernel is keyed per ``(instrument, tf, proximity, prior_n,
    fib_anchor, price_source, roll_schedule)``; on the CLI grid the free axes
    are exactly symbols × intervals × fib-anchors × price-source cells
    (proximity is the singleton ``[None]``; prior_n/roll_schedule are fixed
    per instrument). Re-derived here (the script cannot cheaply build a
    ``SweepGridConfig``) but DRIFT-PINNED against the engine's own
    ``worker_kernel_capacity_default`` formula in
    tests/unit/test_kernel_cache_soak.py (TestEngineDriftPins, R2 NIT-6) —
    a new swept kernel-key axis breaks the pin loudly.
    """
    return (
        _csv_count(symbols)
        * _csv_count(intervals)
        * _csv_count(fib_anchors)
        * _csv_count(price_source_cells)
    )


def effective_worker_kernel_capacity(fib_anchors: str, price_source_cells: str) -> int:
    """The EFFECTIVE per-worker kernel-cache capacity both arms run at.

    The grid-derived default — ``min(max(1, proximities × fib_anchors ×
    price_source_cells), WORKER_KERNEL_CAPACITY_MAX)`` with proximity the CLI
    singleton ``[None]`` (``sweep_parallel.worker_kernel_capacity_default``;
    drift-pinned in TestEngineDriftPins). :func:`arm_env` PINS this exact
    value into :data:`KERNEL_CAPACITY_ENV` for BOTH arms (fix-round-2
    MAJOR-1: the child re-reads dotenv, so absence is not a guarantee — an
    explicitly SET var is, because ``apply_dotenv`` never overrides one; the
    engine resolves a set in-range value verbatim, identical to its own
    default here). Echoed into ``provenance`` so every report states the
    capacity its RSS bar was evaluated at.
    """
    return min(
        max(1, _csv_count(fib_anchors) * _csv_count(price_source_cells)),
        WORKER_KERNEL_CAPACITY_MAX,
    )


def resolve_pinned_workers(cli_workers: int | None) -> tuple[int, str]:
    """The worker count ``arm_env`` PINS into ``BRUTEX_SWEEP_WORKERS`` + its source.

    Fix-round-3 MINOR-1 (refuter-A): a dotenv ``BRUTEX_SWEEP_WORKERS`` reached
    both children symmetrically yet re-scoped the ABSOLUTE pool-RSS bar with
    zero report echo — the count is now always PINNED explicitly (dotenv-immune
    like the capacity pin) and echoed into provenance. *cli_workers* (the
    ``--workers`` flag / the smoke default) wins verbatim (``"explicit"``);
    otherwise the engine's own AUTO law ``auto_sweep_workers(os.cpu_count())``
    is resolved PARENT-side (``"auto-resolved"``) — deliberately WITHOUT the
    small-grid sequential demotion (a sequential arm can never produce soak
    evidence; the caller refuses ``< 2`` structurally instead of soaking into
    a guaranteed loud telemetry FAIL).
    """
    if cli_workers is not None:
        return cli_workers, "explicit"
    from brutex.engine.sweep_parallel import auto_sweep_workers  # lazy: wheel env

    return auto_sweep_workers(os.cpu_count()), "auto-resolved"


def resolve_pinned_mem_budget(workers: int) -> int:
    """The per-worker rank-region budget ``arm_env`` PINS into :data:`MEM_BUDGET_ENV`.

    The engine's shipped DEFAULT sizing law, reproduced by CALLING the
    engine's OWN resolver through its OWN exported seam — never a harness-side
    re-derivation of fraction/floor/divisor. Fix-round-4 MAJOR (refuter-A):
    the round-3 pin divided by *workers*, but the engine's default divides by
    the EVAL-POOL size — ``ParallelGridDriver.run`` sets ``budget_workers =
    pool_size = workers − 1`` (the writer-actor core is the parent and runs no
    tuples) and ``_worker_init`` exports it as ``BRUTEX_SWEEP_BUDGET_WORKERS``,
    so each worker's own default resolves ``total × 1/6 ÷ (workers − 1)``.
    At N=8 the round-3 pin ran every judged arm at a budget 12.5% TIGHTER than
    the law the flip ships (351,171,754 vs the true 401,339,148 on the
    refuter's box) — a non-conservative absolute-RSS/headroom skew. The ONE
    remaining harness-side mirror is the pool-size law ``max(1, workers − 1)``
    (``cmd_run`` refuses ``workers < 2``, so the ``max`` is belt-and-braces);
    it is drift-pinned in TestEngineDriftPins against the engine constants AND
    non-tautologically against a REAL child's own resolution (the integration
    ``budget_derived`` probe — a divisor drift on EITHER side fails). The
    engine resolves a SET :data:`MEM_BUDGET_ENV` value as the per-worker
    override, never divided (the #1352 contract), so pinning this exact figure
    is byte-identical to each eval worker's own default resolution. An
    ambient/.env override is deliberately excluded (the injected env carries
    ONLY the exported pool size — the flip ships the DEFAULT law, so that is
    what the soak measures; ambient presence is echoed separately).
    """
    from brutex.engine.sweep_mem_budget import (  # lazy: wheel env
        BUDGET_WORKERS_ENV,
        resolve_mem_budget_bytes,
    )

    # The ONE mirrored engine law: sweep_parallel.ParallelGridDriver.run —
    # pool_size = self._workers - 1 — exported per worker by _worker_init.
    pool_size = max(1, workers - 1)
    return resolve_mem_budget_bytes({BUDGET_WORKERS_ENV: str(pool_size)})


def resolve_pinned_tripwires() -> dict[str, str]:
    """Engine-DEFAULT tripwire thresholds, pinned into both arm envs.

    Fix-round-4 MINOR (refuter-A): the seven ``BRUTEX_SWEEP_TRIPWIRE_*``
    knobs are read eagerly in every ``sweep-all`` child and shape the SUSPECT
    latch — the CLEAN-EXIT bar — so an ambient/``.env`` value would silently
    re-scope the judged bar with zero report trace. The values come from the
    engine's OWN ``TripwireConfig()`` defaults (never re-minted constants)
    and round-trip byte-identically through the engine's own ``from_env``
    parser (drift-pinned in TestEngineDriftPins). Echoed into
    ``provenance.tripwire_env_pinned`` so the report states the SUSPECT bar
    its clean-exit verdicts were judged under.
    """
    from brutex.observability.run_tripwires import TripwireConfig  # lazy: wheel env

    cfg = TripwireConfig()
    return {
        "BRUTEX_SWEEP_TRIPWIRE_RECONCILE_INTERVAL_S": repr(cfg.reconcile_interval_s),
        "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_FLOOR": repr(cfg.throughput_floor_fraction),
        "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_DWELL": str(cfg.throughput_dwell),
        "BRUTEX_SWEEP_TRIPWIRE_RSS_BUDGET_FRACTION": repr(cfg.rss_budget_fraction),
        "BRUTEX_SWEEP_TRIPWIRE_MEMORY_SLOPE_BYTES": str(cfg.memory_slope_bytes_per_tuple),
        "BRUTEX_SWEEP_TRIPWIRE_MEMORY_DWELL": str(cfg.memory_dwell),
        "BRUTEX_SWEEP_TRIPWIRE_ABSURD_PNL_PAISA": str(cfg.absurd_pnl_paisa),
    }


def redact_home(path_str: str) -> str:
    """Home-prefix-redact a path for the UPLOADED report (R1 MINOR-1).

    The soak report is a public-repo Actions artifact (90 d) — a full
    ``/Users/<user>/…`` lake path would disclose the operator's local
    username. The full path stays in the LOCAL run log only.
    """
    home = str(Path.home())
    if home and home != "/" and (path_str == home or path_str.startswith(home + os.sep)):
        return "~" + path_str[len(home) :]
    return path_str


def build_sweep_argv(
    python: str,
    *,
    lake_root: Path,
    results_root: Path,
    grid: dict[str, str | None],
    no_options_sim: bool,
) -> list[str]:
    """The fixed ``brutex sweep-all`` argv for one arm (never a shell string)."""
    argv = [python, "-m", "brutex.cli", "sweep-all"]
    argv += ["--lake-root", str(lake_root), "--results-root", str(results_root)]
    for flag in (
        "symbols",
        "intervals",
        "start",
        "end",
        "exchange",
        "segment",
        "directions",
        "k",
        "conditions",
        "sl",
        "tp",
        "top",
        "fib_anchors",
        "price_source_cells",
        "max_tuples",
    ):
        value = grid.get(flag)
        if value is not None:
            argv += ["--" + flag.replace("_", "-"), str(value)]
    if no_options_sim:
        argv.append("--no-options-sim")
    return argv


def arm_env(
    base: dict[str, str],
    *,
    cache_on: bool,
    workers: int,
    results_depth_off: bool,
    capacity: int,
    mem_budget_bytes: int,
    mirror_root: Path,
    tripwires: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """One arm's spawn environment — every arm-identity env EXPLICITLY SET.

    Fix-round-2 MAJOR-1 (refuter-1): the child ``brutex sweep-all`` runs
    ``bootstrap_env()`` at startup, which loads ``.env`` (repo root → cwd →
    ``~/.brutex/.env``) and applies any key that is NOT already set — so
    POPPING an env var only covers inheritance, not the child's own dotenv
    re-read (proven: a popped capacity var resolved a ``.env``-planted ``9``
    in the child). ``apply_dotenv`` NEVER overrides a set var (the dotenv
    module's own law), so every env this harness depends on for ARM IDENTITY
    is explicitly SET, never left absent:

    * :data:`KERNEL_CACHE_ENV` — ``0``/``1`` per arm (a dotenv ``=1`` can no
      longer flip the OFF arm; was already set, kept),
    * :data:`KERNEL_CAPACITY_ENV` — PINNED to *capacity* (the grid-derived
      default the flip ships; a set in-range value resolves verbatim in the
      engine, byte-identical to its own default),
    * :data:`RESULTS_DEPTH_ENV` — ``0`` (smoke) / ``1`` (non-smoke; a dotenv
      ``=0`` would silently skip the results-depth artifacts the identity
      compare now covers),
    * :data:`LOG_LEVEL_ENV` ``INFO`` + ``PYTHONHASHSEED=0`` (already set),
    * :data:`SWEEP_WORKERS_ENV` — ALWAYS pinned to *workers* (fix-round-3
      MINOR-1: previously set only when ``--workers`` was passed — a dotenv
      value reached both children symmetrically yet re-scoped the ABSOLUTE
      pool-RSS bar with zero report echo; also closes the dotenv
      ``BRUTEX_SWEEP_WORKERS=1`` sequential bypass, which now cannot even
      reach the child),
    * :data:`MEM_BUDGET_ENV` — PINNED to *mem_budget_bytes*, the
      parent-resolved DEFAULT sizing law per eval worker
      (:func:`resolve_pinned_mem_budget` — the engine's own resolver at the
      exported eval-pool divisor ``workers − 1``, fix-round-4 MAJOR; the
      absolute RSS/pool bars are measured at KNOWN sizing, never a stray
      ``.env`` budget's),
    * the SEVEN :data:`TRIPWIRE_ENV_PINS` — PINNED to the engine-default
      thresholds (*tripwires*, default :func:`resolve_pinned_tripwires`;
      fix-round-4 MINOR — they shape the SUSPECT latch = the clean-exit bar,
      so an ambient/``.env`` value would re-scope the JUDGMENT itself),
    * :data:`RUN_MIRROR_ROOT_ENV` — the arm's OWN ``<arm>/mirror`` dir
      (fix-round-3 NIT: both arms share ``run_id``, so the default shared
      ``~/.brutex/runs/<run_id>/`` forensics mirror was clobbered cross-arm;
      per-arm dirs keep the forensics AND leave the operator's mirror root
      untouched — write-only, outside the compared ``results/``/
      ``sweep_runs/`` roots).

    The remaining sweep-reachable envs are SYMMETRIC by construction and
    deliberately unpinned — the ENUMERATED audit lives in
    :data:`SYMMETRIC_ENV_ECHO` (echoed into provenance); an un-enumerated
    env stays symmetric across both arms by this same construction (same
    base env + same dotenv application in both children), so it can never
    skew arm identity — see the completeness envelope on the constant.
    """
    env = dict(base)
    env[KERNEL_CACHE_ENV] = "1" if cache_on else "0"
    env[KERNEL_CAPACITY_ENV] = str(capacity)
    env[LOG_LEVEL_ENV] = "INFO"  # the telemetry MUST reach stderr (#1630 guard)
    env["PYTHONHASHSEED"] = "0"
    env[SWEEP_WORKERS_ENV] = str(workers)
    env[MEM_BUDGET_ENV] = str(mem_budget_bytes)
    env[RUN_MIRROR_ROOT_ENV] = str(mirror_root)
    env[RESULTS_DEPTH_ENV] = "0" if results_depth_off else "1"
    env.update(resolve_pinned_tripwires() if tripwires is None else tripwires)
    return env


def build_deltas(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
    """The ON−OFF deltas the acceptance bars judge (None = not measurable)."""
    off_worker = off.get("worker_peak_rss_kib_max")
    on_worker = on.get("worker_peak_rss_kib_max")
    off_pool = off.get("pool_peak_rss_kib")
    on_pool = on.get("pool_peak_rss_kib")
    off_wall = off.get("wall_s")
    on_wall = on.get("wall_s")
    ratio: float | None = None
    if isinstance(off_wall, int | float) and isinstance(on_wall, int | float) and off_wall > 0:
        ratio = on_wall / off_wall
    return {
        "rss_per_worker_kib": (
            on_worker - off_worker
            if isinstance(on_worker, int) and isinstance(off_worker, int)
            else None
        ),
        "rss_pool_kib": (
            on_pool - off_pool if isinstance(on_pool, int) and isinstance(off_pool, int) else None
        ),
        "wall_delta_s": (
            on_wall - off_wall
            if isinstance(on_wall, int | float) and isinstance(off_wall, int | float)
            else None
        ),
        "wall_ratio_on_over_off": ratio,
    }


#: Fix-round-7 NIT-2 (refuter-A): the maximum nesting depth `find_nonfinite`
#: walks before refusing the report as too deeply nested. A legitimately
#: produced soak-report nests ~5 levels; the bound exists solely so a
#: hostile/corrupt deep report reads the NAMED structural rc 2 instead of a
#: raw ``RecursionError`` traceback at rc 1 (the judged-FAIL code).
MAX_REPORT_DEPTH: Final[int] = 1000


class ReportTooDeepError(ValueError):
    """A verdict report nested beyond MAX_REPORT_DEPTH (fix-round-7 NIT-2)."""


def find_nonfinite(node: object, path: str = "report") -> str | None:
    """Return the dotted path of the FIRST non-finite float in *node*, else None.

    Fix-round-6 NIT (refuter-A): ``json.loads`` accepts the ``Infinity``/``NaN``
    constants by default (and any ``1e400``-style literal parses to ``inf``), so
    a hand-mangled report can carry non-finite numerics into the verdict's
    arithmetic — ``int(inf)`` raises ``OverflowError`` (the #1558 precedent
    class), while an ``inf`` that SURVIVES arithmetic would silently poison the
    judged bars. The verdict refuses non-finite numerics at INGESTION with a
    NAMED structural path (a legitimately-produced report never carries one:
    walls are monotonic-clock diffs, RSS figures are ints, and the wall ratio
    is division-guarded in :func:`build_deltas`). Bools are not floats; ints
    are always finite in Python — only ``float`` needs the probe.

    Fix-round-7 NIT-2 (refuter-A): the walk is ITERATIVE (explicit stack —
    never recursion-limited, the round-6 recursive descent crashed with a raw
    ``RecursionError`` at ~depth 1000) AND depth-bounded: nesting beyond
    ``MAX_REPORT_DEPTH`` raises :class:`ReportTooDeepError` for the caller's
    named structural refusal. (``json.loads``'s own C-scanner ``RecursionError``
    — which bites only at far deeper nesting — is caught in the read net.)
    The stack pushes children in reverse so pop order preserves the recursive
    version's document-order FIRST-match semantics.
    """
    stack: list[tuple[object, str, int]] = [(node, path, 0)]
    while stack:
        current, current_path, depth = stack.pop()
        if depth > MAX_REPORT_DEPTH:
            # The offending path is itself O(depth) long — quote a bounded
            # prefix so the refusal stays one readable line.
            shown = current_path if len(current_path) <= 120 else current_path[:120] + "…"
            raise ReportTooDeepError(
                f"report too deeply nested at {shown} (depth > {MAX_REPORT_DEPTH})"
            )
        if isinstance(current, float) and not math.isfinite(current):
            return current_path
        if isinstance(current, dict):
            for key, value in reversed(list(current.items())):
                stack.append((value, f"{current_path}.{key}", depth + 1))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], f"{current_path}[{index}]", depth + 1))
    return None


def _check_clean_exit(off: dict[str, Any], on: dict[str, Any]) -> str:
    reasons: list[str] = []
    for name, res in (("off", off), ("on", on)):
        if res.get("rc") != 0:
            reasons.append(f"{name} rc={res.get('rc')}")
        if res.get("timed_out"):
            reasons.append(f"{name} timed out (budget)")
        if res.get("exit_state") != "complete":
            reasons.append(f"{name} exit_state={res.get('exit_state')!r}")
        if res.get("suspect"):
            reasons.append(f"{name} SUSPECT")
        if res.get("quarantine_present"):
            reasons.append(f"{name} .sweep_quarantine.json present")
        if res.get("abort_tokens"):
            reasons.append(f"{name} abort tokens {res.get('abort_tokens')!r}")
    return "PASS" if not reasons else "FAIL(" + "; ".join(reasons) + ")"


def _check_telemetry(off: dict[str, Any], on: dict[str, Any], keys: int) -> tuple[str, str]:
    """(telemetry check, builds-bound check) — the #1729 invariants ONLY.

    Exact builds/hits splits are NEVER asserted (worker-split-dependent by
    the emission's own honesty law): the invariants are OFF-silent, ON
    exactly-one summary, ``builds + hits == tuples_done`` and
    ``builds ≤ keys × workers``.
    """
    reasons: list[str] = []
    if off.get("kernel_reuse_summaries", 0) != 0:
        reasons.append(f"OFF arm emitted {off.get('kernel_reuse_summaries')} summaries (want 0)")
    on_summaries = on.get("kernel_reuse_summaries", 0)
    if on_summaries != 1:
        reasons.append(f"ON arm emitted {on_summaries} summaries (want exactly 1)")
        return "FAIL(" + "; ".join(reasons) + ")", "ABSTAIN(no ON summary)"
    builds = on.get("builds_total")
    hits = on.get("hits_total")
    workers = on.get("workers")
    tuples_done = on.get("tuples_done")
    if not all(isinstance(v, int) for v in (builds, hits, workers, tuples_done)):
        reasons.append("ON summary/manifest figures missing or malformed")
        return "FAIL(" + "; ".join(reasons) + ")", "ABSTAIN(malformed figures)"
    assert isinstance(builds, int)  # for the type checker — guarded above
    assert isinstance(hits, int)
    assert isinstance(workers, int)
    assert isinstance(tuples_done, int)
    if builds + hits != tuples_done:
        reasons.append(f"builds({builds}) + hits({hits}) != tuples_done({tuples_done})")
    telemetry = "PASS" if not reasons else "FAIL(" + "; ".join(reasons) + ")"
    if builds <= keys * workers:
        bound = "PASS"
    else:
        bound = f"FAIL(builds={builds} > keys({keys}) × workers({workers}))"
    return telemetry, bound


def evaluate_acceptance(
    *,
    off: dict[str, Any],
    on: dict[str, Any],
    compare: dict[str, Any],
    deltas: dict[str, Any],
    keys: int,
    smoke: bool,
    hardware_key: str | None = None,
    hardware_key_source: str | None = None,
) -> dict[str, str]:
    """The acceptance block: PASS/FAIL per bar + an ADVISORY verdict.

    In ``--smoke`` mode the RSS/wall bars are stamped ``SMOKE-UNJUDGED`` (a
    fixture grid cannot produce decision-grade numbers) and the verdict can
    never read FLIP-READY; the mechanics bars (identity / clean exits /
    telemetry) are always judged. A NON-smoke run whose *hardware_key* does
    not carry the registered §8 M4-Pro signature
    (:data:`M4PRO_KEY_SUFFIX`) — or whose *hardware_key_source* says the key
    came from the :data:`BENCH_BASELINE_KEY_ENV` env OVERRIDE rather than
    real hardware probes (fix-round-2: a forged override key reads the M4
    signature on ANY box) — reads a ``provenance`` bar of NOT-DECISION-GRADE
    and the verdict can never read FLIP-READY either (R2 MINOR-4 / runbook
    §5 bar 6 — UNVERIFIED lab evidence is not a harness FAILURE, so it does
    not drive the exit code). TRUSTED-INPUT BOUNDARY: this block judges the
    report it is handed — the report is evidence for the flip DEC, not a
    tamper-proof artifact. The flip DEC quotes the RAW figures — this block
    is advisory, never the decision.
    """
    identity = (
        "PASS"
        if compare.get("identity_ok")
        and compare.get("audit_ts_masked_ok")
        and compare.get("exit_status_structural_ok")
        else "FAIL("
        + "; ".join(
            token
            for token, ok in (
                (
                    f"{len(compare.get('diff_paths', []))} result-tree diffs",
                    compare.get("identity_ok"),
                ),
                ("audit chains diverge ts-masked", compare.get("audit_ts_masked_ok")),
                (str(compare.get("exit_status_detail")), compare.get("exit_status_structural_ok")),
            )
            if not ok
        )
        + ")"
    )
    clean = _check_clean_exit(off, on)
    telemetry, builds_bound = _check_telemetry(off, on, keys)

    if smoke:
        rss_env = "SMOKE-UNJUDGED"
        wall = "SMOKE-UNJUDGED"
    else:
        rss_reasons: list[str] = []
        per_worker = deltas.get("rss_per_worker_kib")
        pool_on = on.get("pool_peak_rss_kib")
        if not isinstance(per_worker, int):
            rss_reasons.append("per-worker delta unmeasurable (no worker samples)")
        elif per_worker > RSS_PER_WORKER_DELTA_CEILING_KIB:
            rss_reasons.append(
                f"per-worker delta {per_worker} KiB > {RSS_PER_WORKER_DELTA_CEILING_KIB} KiB"
            )
        if not isinstance(pool_on, int):
            rss_reasons.append("ON pool peak unmeasurable")
        elif pool_on > POOL_PEAK_CEILING_KIB:
            rss_reasons.append(f"ON pool peak {pool_on} KiB > {POOL_PEAK_CEILING_KIB} KiB")
        rss_env = "PASS" if not rss_reasons else "FAIL(" + "; ".join(rss_reasons) + ")"
        ratio = deltas.get("wall_ratio_on_over_off")
        if not isinstance(ratio, float) or not math.isfinite(ratio):
            wall = "FAIL(wall ratio unmeasurable)"
        elif ratio <= WALL_RATIO_CEILING:
            wall = "PASS"
        else:
            wall = f"FAIL(ON/OFF wall ratio {ratio:.3f} > {WALL_RATIO_CEILING})"

    if smoke:
        provenance_bar = "SMOKE-UNJUDGED"
    elif isinstance(hardware_key_source, str) and hardware_key_source.startswith(
        "env-override-at-verdict"
    ):
        # Fix-round-3 NIT (refuter-A wording): attribute correctly — the
        # STORED key was genuinely probed at run time; what taints the run is
        # the LIVE override set at VERDICT time (the belt-check).
        provenance_bar = (
            f"NOT-DECISION-GRADE({BENCH_BASELINE_KEY_ENV} is set in the LIVE verdict "
            f"environment (set at verdict time) — the report's stored key "
            f"{hardware_key!r} was probed at run time, but an override-tainted "
            "verdict environment cannot feed the flip DEC; unset the override and "
            "re-run the verdict)"
        )
    elif isinstance(hardware_key_source, str) and hardware_key_source.startswith("env-override"):
        provenance_bar = (
            f"NOT-DECISION-GRADE(hardware_key {hardware_key!r} comes from the "
            f"{BENCH_BASELINE_KEY_ENV} env OVERRIDE ({hardware_key_source}), not real "
            "hardware probes — a spoofable key cannot feed the flip DEC; unset the "
            "override and re-run on the registered M4-Pro)"
        )
    elif isinstance(hardware_key, str) and hardware_key.endswith(M4PRO_KEY_SUFFIX):
        provenance_bar = "PASS"
    else:
        provenance_bar = (
            f"NOT-DECISION-GRADE(hardware_key {hardware_key!r} does not carry the "
            f"registered §8 M4-Pro signature {M4PRO_KEY_SUFFIX!r} — UNVERIFIED lab "
            "evidence, cannot feed the flip DEC)"
        )

    checks = {
        "identity": identity,
        "clean_exit": clean,
        "rss_envelope": rss_env,
        "wall_neutral": wall,
        "builds_bound": builds_bound,
        "telemetry": telemetry,
        "provenance": provenance_bar,
    }
    failed = sorted(name for name, status in checks.items() if status.startswith("FAIL"))
    if smoke:
        verdict = (
            "SMOKE-ONLY(mechanics green — NOT decision-grade)"
            if not failed
            else f"SMOKE-FAILED({', '.join(failed)})"
        )
    elif failed:
        verdict = f"NOT-READY({', '.join(failed)})"
    elif provenance_bar != "PASS":
        verdict = (
            "NOT-DECISION-GRADE(non-M4-Pro hardware_key — bars green but the numbers "
            "are UNVERIFIED lab evidence and cannot open the flip DEC)"
        )
    else:
        verdict = "FLIP-READY"
    checks["verdict"] = verdict
    return checks


def judged_failures(acceptance: dict[str, str]) -> list[str]:
    """The check names whose status is a judged FAIL (drives the exit code)."""
    return sorted(
        name
        for name, status in acceptance.items()
        if name != "verdict" and status.startswith(("FAIL", "SMOKE-FAILED"))
    )


def fmt_kib(value: object) -> str:
    """Human KiB → GiB rendering for the summary ('?' for unmeasured)."""
    if not isinstance(value, int):
        return "?"
    return f"{value} KiB ({value / (1024 * 1024):.2f} GiB)"


def render_summary(report: dict[str, Any]) -> str:
    """The human ``summary.md`` — every figure tagged MEASURED vs MODELED."""
    prov = report["provenance"]
    off = report["arms"]["off"]
    on = report["arms"]["on"]
    deltas = report["deltas"]
    acceptance = report["acceptance"]
    compare = report["compare"]
    smoke = bool(prov.get("smoke"))
    lines = [
        "# Kernel-cache OFF↔ON soak report",
        "",
        f"- kind: {report['kind']} (schema v{report['schema_version']})",
        f"- commit: {prov.get('commit')} · hardware_key: {prov.get('hardware_key')}",
        f"- lake_root: {prov.get('lake_root')} · started_utc: {prov.get('started_utc')}",
        f"- rss method: {prov.get('rss_method')}",
        f"- smoke: {smoke}"
        + (" — MECHANICS PROOF ONLY, numbers are NOT decision-grade" if smoke else ""),
        "",
        "## Arms (MEASURED — this run)",
        "",
        "| figure | OFF | ON |",
        "|---|---|---|",
        f"| wall_s | {off.get('wall_s'):.1f} | {on.get('wall_s'):.1f} |",
        f"| worker peak RSS (max PID) | {fmt_kib(off.get('worker_peak_rss_kib_max'))} "
        f"| {fmt_kib(on.get('worker_peak_rss_kib_max'))} |",
        f"| pool peak RSS (Σ tree) | {fmt_kib(off.get('pool_peak_rss_kib'))} "
        f"| {fmt_kib(on.get('pool_peak_rss_kib'))} |",
        f"| parent peak RSS (ru_maxrss) | {fmt_kib(off.get('parent_peak_rss_kib'))} "
        f"| {fmt_kib(on.get('parent_peak_rss_kib'))} |",
        f"| kernel_reuse summaries | {off.get('kernel_reuse_summaries')} "
        f"| {on.get('kernel_reuse_summaries')} |",
        f"| builds/hits/workers | — | {on.get('builds_total')}/{on.get('hits_total')}"
        f"/{on.get('workers')} |",
        f"| tuples_done | {off.get('tuples_done')} | {on.get('tuples_done')} |",
        f"| exit | rc={off.get('rc')} state={off.get('exit_state')} "
        f"| rc={on.get('rc')} state={on.get('exit_state')} |",
        "",
        "## VALUE — what the flip buys / costs (MEASURED on this grid)",
        "",
        f"- wall: ON−OFF = {deltas.get('wall_delta_s'):+.1f} s "
        f"(ratio {deltas.get('wall_ratio_on_over_off'):.3f}; ≤ {WALL_RATIO_CEILING} is the "
        "no-regression band — a within-noise no-win flip is a YELLOW FLAG for the DEC)",
        f"- wall-bar ordering bias (fix-round-3 NIT-1): arms ran "
        f"{'→'.join(str(a).upper() for a in prov.get('arm_order', ['off', 'on']))} — the "
        "SECOND arm inherits a warm OS page-cache/import state, a systematic bias INSIDE "
        f"the ≤ {WALL_RATIO_CEILING} band (bounded by the preload share of a 2-4 h arm; "
        "re-run with --arm-order reversed for a bias check)",
        f"- per-worker peak RSS delta: {fmt_kib(deltas.get('rss_per_worker_kib'))} "
        f"(ceiling {fmt_kib(RSS_PER_WORKER_DELTA_CEILING_KIB)})",
        f"- pool peak RSS delta: {fmt_kib(deltas.get('rss_pool_kib'))}; "
        f"ON pool peak {fmt_kib(on.get('pool_peak_rss_kib'))} vs ceiling "
        f"{fmt_kib(POOL_PEAK_CEILING_KIB)} — actual headroom "
        f"{fmt_kib(POOL_PEAK_CEILING_KIB - on['pool_peak_rss_kib']) if isinstance(on.get('pool_peak_rss_kib'), int) else '?'}",
        "- full-grid wall saving: MODELED only (extrapolated off this bounded grid; the "
        "2-4-day full sweep is never run twice in one job — SEC9-1 token fence)",
        f"- RSS bar evaluated at kernel-cache capacity="
        f"{prov.get('worker_kernel_capacity_effective')} (the grid-derived default, "
        f"PINNED explicitly as {KERNEL_CAPACITY_ENV} in both arm envs — an ambient var "
        "or a .env-file value can never re-scope it: the child's dotenv loader never "
        "overrides a set var"
        + (
            " — an ambient one WAS set and overridden by the pin"
            if prov.get("capacity_env_ambient_present")
            else ""
        )
        + ") — the flip DEC must weigh whether this capacity covers the operator's grids "
        "(widen via --fib-anchors / --price-source-cells)",
        f"- sizing PINNED in both arm envs (fix-round-3 MINOR-1 / fix-round-4 MAJOR): "
        f"workers={prov.get('sweep_workers_effective')} "
        f"({prov.get('sweep_workers_source')}) as {SWEEP_WORKERS_ENV}, "
        f"mem_budget={prov.get('mem_budget_bytes_effective')} bytes/worker (the "
        "engine's RAM-derived DEFAULT law at the eval-pool divisor workers−1, "
        f"resolved parent-side through the engine's own seam) as {MEM_BUDGET_ENV} — "
        "an ambient/.env sizing value can never re-scope the absolute RSS/pool bars; "
        "the SUSPECT-latch tripwire thresholds are pinned to the engine defaults "
        "too (provenance.tripwire_env_pinned — fix-round-4); the remaining "
        "symmetric envs are echoed in provenance.symmetric_env_echo",
        "",
        "## Identity (MEASURED)",
        "",
        f"- result + sweep_runs trees ts-masked identical: {compare.get('identity_ok')} "
        f"({len(compare.get('diff_paths', []))} diffs; "
        f"{compare.get('result_files_compared')} files incl. "
        f"{compare.get('sweep_runs_files_compared', 0)} results-depth sweep_runs/ files)",
        f"- audit chains ts-masked equal: {compare.get('audit_ts_masked_ok')}",
        f"- exit-status structural: {compare.get('exit_status_detail')}",
        "",
        "## Acceptance (advisory — the flip DEC judges the RAW figures)",
        "",
    ]
    lines += [f"- {name}: {status}" for name, status in acceptance.items()]
    lines += [
        "",
        "HONEST: the default-ON flip is NOT pre-approved by this report — it is a",
        "separate one-constant DEC (DEC-SWEEP-WORKER-KERNEL-CACHE-001 →",
        "-DEFAULT-ON) that quotes these figures and needs operator sign-off.",
        "",
    ]
    return "\n".join(lines)


def build_report(
    *,
    provenance: dict[str, Any],
    off: dict[str, Any],
    on: dict[str, Any],
    compare: dict[str, Any],
    keys: int,
    smoke: bool,
) -> dict[str, Any]:
    """Assemble the ONE machine-readable soak-report.json payload."""
    deltas = build_deltas(off, on)
    acceptance = evaluate_acceptance(
        off=off,
        on=on,
        compare=compare,
        deltas=deltas,
        keys=keys,
        smoke=smoke,
        hardware_key=provenance.get("hardware_key"),
        hardware_key_source=provenance.get("hardware_key_source"),
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "provenance": provenance,
        "arms": {"off": off, "on": on},
        "compare": compare,
        "deltas": deltas,
        "acceptance": acceptance,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Impure machinery — preflight, arm runner, smoke fixture (smoke-tested e2e)
# ─────────────────────────────────────────────────────────────────────────────


def _fail_structural(message: str) -> int:
    print(f"kernel-cache-soak: STRUCTURAL: {message}", file=sys.stderr)
    return 2


def preflight_lake(
    lake_root: Path, symbols: str, intervals: str, exchange: str, segment: str
) -> list[str]:
    """Loud lake-shape probe — the missing coordinates, empty when sound.

    A symbol's bars may live under the bare trading symbol OR the
    ``<EXCH>-<symbol>`` groww form; either satisfies the probe. NEVER soak
    blind: every requested ``(symbol, interval)`` must hold at least one
    Parquet file.
    """
    missing: list[str] = []
    if not lake_root.is_dir():
        return [f"lake root {lake_root} is not a directory"]
    base = lake_root / "bars" / exchange / segment
    for symbol in (s.strip() for s in symbols.split(",") if s.strip()):
        candidates = [base / symbol, base / f"{exchange}-{symbol}"]
        sym_dir = next((c for c in candidates if c.is_dir()), None)
        if sym_dir is None:
            missing.append(f"{base}/{{{symbol}|{exchange}-{symbol}}} (no symbol dir)")
            continue
        for interval in (i.strip() for i in intervals.split(",") if i.strip()):
            if not any((sym_dir / interval).rglob("*.parquet")):
                missing.append(f"{sym_dir / interval} (no parquet files)")
    return missing


def preflight_disk(out_dir: Path, need_kib: int) -> str | None:
    """Digits-fenced free-disk probe under *out_dir* (the SOAK-R10-1 pattern)."""
    df_bin = shutil.which("df")
    if df_bin is None:
        return "df not found — cannot verify free disk; refusing to soak blind"
    proc = subprocess.run(  # noqa: S603 — fixed argv, never a shell
        [df_bin, "-Pk", str(out_dir)], capture_output=True, text=True, check=False
    )
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) < 2:
        return f"df failed (rc={proc.returncode}) — refusing to soak blind"
    fields = lines[1].split()
    free = fields[3] if len(fields) > 3 else ""
    if not free.isdigit():
        return f"df free-space field {free!r} is not numeric — refusing to soak blind"
    if int(free) < need_kib:
        return f"insufficient disk: need ~{need_kib} KiB under {out_dir}, free {int(free)} KiB"
    return None


def collect_provenance(
    *,
    lake_root: Path,
    grid: dict[str, str | None],
    budget_minutes: int,
    sample_interval_s: float,
    smoke: bool,
    min_free_gib: int | None = None,
    sweep_workers_effective: int | None = None,
    sweep_workers_source: str | None = None,
    mem_budget_bytes_effective: int | None = None,
    arm_order: tuple[str, ...] = ("off", "on"),
) -> dict[str, Any]:
    """Environment echo for the report — every probe guarded, never fatal."""

    def guarded(argv: list[str]) -> str:
        binary = shutil.which(argv[0])
        if binary is None:
            return "unavailable"
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv
                [binary, *argv[1:]], capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return proc.stdout.strip() if proc.returncode == 0 else "unavailable"

    try:
        from brutex.config.hardware_envelope import hardware_key

        hw_key = hardware_key()
    except Exception:  # provenance echo only, never fatal
        hw_key = "unavailable"
    # Fix-round-2 (refuter-1 spoof): record WHERE the key came from — the
    # documented BENCH_BASELINE_KEY_ENV override wins verbatim inside
    # hardware_key(), so an override-sourced key is spoofable and the
    # acceptance provenance bar refuses it on a non-smoke report.
    if os.environ.get(BENCH_BASELINE_KEY_ENV, "").strip():
        hw_key_source = f"env-override:{BENCH_BASELINE_KEY_ENV}"
    elif hw_key == "unavailable":
        hw_key_source = "unavailable"
    else:
        hw_key_source = "probed"
    # sweep-all's own defaults when the axes are unset (cli.py cmd_sweep_all).
    fib_csv = grid.get("fib_anchors") or "prev_day"
    ps_csv = grid.get("price_source_cells") or "hlc3:close"
    return {
        "commit": guarded(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "hardware_key": hw_key,
        "hardware_key_source": hw_key_source,
        # uname -sm (kernel + arch ONLY — never `-a`'s hostname/build string;
        # the report is a public-repo artifact, R1 MINOR-1 / the
        # bench_baseline posture).
        "uname": guarded(["uname", "-sm"]),
        "sw_vers": guarded(["sw_vers", "-productVersion"]),
        "cpu_count": os.cpu_count(),
        # Home-prefix-redacted for the uploaded artifact (R1 MINOR-1); the
        # full path is printed to the LOCAL run log by cmd_run.
        "lake_root": redact_home(str(lake_root)),
        "grid": {
            **{k: v for k, v in grid.items() if v is not None},
            "keys_fan_out": keys_fan_out(
                grid.get("symbols") or "",
                grid.get("intervals") or "",
                fib_csv,
                ps_csv,
            ),
        },
        # The EFFECTIVE capacity both arms measure (fix-round-2 MAJOR-1: this
        # exact value is explicitly PINNED into each arm's KERNEL_CAPACITY_ENV
        # by arm_env — a dotenv/ambient override can never re-scope it because
        # the child's dotenv loader never overrides a set var).
        "worker_kernel_capacity_effective": effective_worker_kernel_capacity(fib_csv, ps_csv),
        "capacity_env_child_pinned": True,
        # Informational only: whether the PARENT's environ carried the
        # override (a .env-sourced value is invisible here — and irrelevant,
        # the child value is pinned either way; renamed from the R1
        # `capacity_env_ambient_popped`, whose parent-only view the refuter
        # proved blind to the child's dotenv re-read).
        "capacity_env_ambient_present": KERNEL_CAPACITY_ENV in os.environ,
        # Fix-round-3 MINOR-1: the sizing the ABSOLUTE RSS/pool bars were
        # measured at — both values are PINNED into the arm envs by arm_env
        # (dotenv-immune, the capacity precedent), so a stray ambient/.env
        # budget or worker count can never silently re-scope the bars.
        "sweep_workers_effective": sweep_workers_effective,
        "sweep_workers_source": sweep_workers_source,
        "sweep_workers_env_ambient_present": SWEEP_WORKERS_ENV in os.environ,
        "mem_budget_bytes_effective": mem_budget_bytes_effective,
        "mem_budget_env_ambient_present": MEM_BUDGET_ENV in os.environ,
        # Fix-round-4 MINOR (refuter-A): the engine-default tripwire
        # thresholds PINNED into both arm envs (they shape the SUSPECT latch
        # = the clean-exit bar) — the report states the judgment posture.
        "tripwire_env_pinned": resolve_pinned_tripwires(),
        # Fix-round-3 NIT-1 (refuter-A): the arm execution order — the SECOND
        # arm inherits a warm OS page-cache/import state, a systematic bias
        # inside the ≤ 1.02 wall band (see the summary's disclosure line).
        "arm_order": list(arm_order),
        # The audited SYMMETRIC (unpinned) sweep-reachable envs: parent-environ
        # view only — a .env-file value the child injects is invisible here
        # (the arm-identity-relevant ones are PINNED instead, see arm_env).
        "symmetric_env_echo": {
            name: os.environ[name] for name in SYMMETRIC_ENV_ECHO if name in os.environ
        },
        "symmetric_env_echo_scope": (
            "parent-environ view only — dotenv (.env) values the child's own "
            "bootstrap_env() injects are invisible here; arm-identity-relevant "
            "sizing envs are PINNED (see arm_env), the rest are symmetric across arms"
        ),
        "symmetric_env_audited": list(SYMMETRIC_ENV_ECHO),
        "min_free_gib": min_free_gib,
        "budget_minutes": budget_minutes,
        "sample_interval_s": sample_interval_s,
        "rss_method": RSS_METHOD_TEMPLATE.format(interval=sample_interval_s),
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "smoke": smoke,
    }


def _sampler_loop(
    tracker: RssTracker, interval_s: float, stop: threading.Event, ps_bin: str
) -> None:
    """Poll ``ps`` until *stop*; fold each snapshot into *tracker*."""
    while not stop.is_set():
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [ps_bin, "-eo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False
        )
        if proc.returncode == 0:
            tracker.fold(parse_ps_lines(proc.stdout))
        stop.wait(interval_s)


def run_arm(
    *,
    name: str,
    argv: list[str],
    env: dict[str, str],
    arm_dir: Path,
    results_root: Path,
    sample_interval_s: float,
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Spawn one ``brutex sweep-all`` arm; sample RSS; harvest artifacts."""
    arm_dir.mkdir(parents=True, exist_ok=True)
    sweep_log = arm_dir / "sweep.log"
    stdout_log = arm_dir / "stdout.log"
    started = datetime.now(UTC).isoformat(timespec="seconds")
    start = time.monotonic()
    timed_out = False
    parent_peak_kib: int | None = None
    ps_bin = shutil.which("ps")
    with stdout_log.open("wb") as out_fh, sweep_log.open("wb") as err_fh:
        proc = subprocess.Popen(argv, stdout=out_fh, stderr=err_fh, env=env)  # noqa: S603
        tracker = RssTracker(root_pid=proc.pid)
        stop = threading.Event()
        sampler: threading.Thread | None = None
        if ps_bin is not None:
            sampler = threading.Thread(
                target=_sampler_loop, args=(tracker, sample_interval_s, stop, ps_bin), daemon=True
            )
            sampler.start()
        rc: int | None = None
        try:
            while True:
                try:
                    pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
                except ChildProcessError:  # already reaped elsewhere — degrade
                    rc = proc.wait()
                    break
                if pid == proc.pid:
                    rc = os.waitstatus_to_exitcode(status)
                    proc.returncode = rc  # keep Popen coherent (no second waitpid)
                    parent_peak_kib = maxrss_to_kib(rusage.ru_maxrss)
                    break
                if time.monotonic() > deadline_monotonic:
                    timed_out = True
                    proc.send_signal(signal.SIGTERM)
                    try:
                        rc = proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        rc = proc.wait()
                    break
                time.sleep(0.25)
        finally:
            stop.set()
            if sampler is not None:
                sampler.join(timeout=10)
    wall_s = time.monotonic() - start

    telemetry = harvest_telemetry(sweep_log.read_text(encoding="utf-8", errors="replace"))
    exit_status: dict[str, Any] = {}
    status_files = sorted(results_root.glob("runs/*/exit-status.json"))
    if status_files:
        try:
            exit_status = json.loads(status_files[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            exit_status = {}
    tuples_done: int | None = None
    manifest_files = sorted(results_root.glob("runs/*/manifest.json"))
    if manifest_files:
        try:
            manifest = json.loads(manifest_files[-1].read_text(encoding="utf-8"))
            actual = manifest.get("actual")
            if isinstance(actual, dict) and isinstance(actual.get("tuples_done"), int):
                tuples_done = actual["tuples_done"]
        except (OSError, json.JSONDecodeError):
            tuples_done = None

    summary = telemetry.summaries[0] if len(telemetry.summaries) == 1 else {}
    result: dict[str, Any] = {
        "name": name,
        "rc": rc,
        "timed_out": timed_out,
        "wall_s": wall_s,
        "started_utc": started,
        "ended_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "worker_peak_rss_kib": {
            str(pid): kib for pid, kib in sorted(tracker.worker_peaks_kib().items())
        },
        "worker_peak_rss_kib_max": tracker.worker_peak_max_kib(),
        "pool_peak_rss_kib": tracker.pool_peak_kib if tracker.samples else None,
        "parent_peak_rss_kib": parent_peak_kib,
        "rss_samples": tracker.samples,
        "kernel_reuse_summaries": len(telemetry.summaries),
        "builds_total": summary.get("builds_total"),
        "hits_total": summary.get("hits_total"),
        "workers": summary.get("workers"),
        "worker_kernel_built_events": telemetry.built_events,
        "abort_tokens": list(telemetry.abort_tokens),
        "exit_state": exit_status.get("state"),
        "exit_code_artifact": exit_status.get("exit_code"),
        "suspect": exit_status.get("suspect"),
        "quarantine_present": (results_root / ".sweep_quarantine.json").exists(),
        "tuples_done": tuples_done,
        "exit_status": {k: exit_status.get(k) for k in ("state", "exit_code", "suspect")},
    }
    return result


def compare_arms(
    off_arm_dir: Path, on_arm_dir: Path, off_arm: dict[str, Any], on_arm: dict[str, Any]
) -> dict[str, Any]:
    """The ts-masked identity compare over each arm's FULL identity tree.

    Covers ``<arm>/results/`` (the per-tuple store tree, full exclusion law)
    AND ``<arm>/sweep_runs/`` (the results-depth sibling root, ``.stamp.json``
    masked — fix-round-2 MAJOR: previously silently outside the compare) plus
    the audit chains (ts-masked) and the structural exit-status.
    """
    off_tree = arm_identity_tree(off_arm_dir)
    on_tree = arm_identity_tree(on_arm_dir)
    diffs = tree_diff(off_tree, on_tree)
    audit_ok = False
    audit_detail = "audit chain missing on one side"
    off_chain = off_arm_dir / "results" / "audit" / "sweep-all.jsonl"
    on_chain = on_arm_dir / "results" / "audit" / "sweep-all.jsonl"
    if off_chain.is_file() and on_chain.is_file():
        try:
            off_entries = masked_audit_entries(off_chain)
            on_entries = masked_audit_entries(on_chain)
        except Exception as exc:  # compare must report, never crash
            audit_detail = f"audit parse failed: {exc!r}"
        else:
            audit_ok = off_entries == on_entries
            audit_detail = (
                f"{len(off_entries)} entries ts-masked equal"
                if audit_ok
                else f"chains diverge ({len(off_entries)} vs {len(on_entries)} entries)"
            )
    status_ok, status_detail = exit_status_structural(
        off_arm.get("exit_status", {}), on_arm.get("exit_status", {})
    )
    all_rels = set(off_tree) | set(on_tree)
    return {
        "identity_ok": not diffs and bool(off_tree),
        "result_files_compared": len(all_rels),
        "sweep_runs_files_compared": len(
            [rel for rel in all_rels if rel.startswith(SWEEP_RUNS_DIRNAME + "/")]
        ),
        "diff_paths": diffs[:50],
        "audit_ts_masked_ok": audit_ok,
        "audit_detail": audit_detail,
        "exit_status_structural_ok": status_ok,
        "exit_status_detail": status_detail,
        "excluded_sidecars": list(STRUCTURAL_SIDECARS),
    }


def compare_arms_guarded(
    off_arm_dir: Path, on_arm_dir: Path, off_arm: dict[str, Any], on_arm: dict[str, Any]
) -> dict[str, Any]:
    """:func:`compare_arms` with the no-report window CLOSED (fix-round-3, refuter-B).

    The tree walks read every result file (``read_bytes`` after ``rglob``) —
    a file vanishing/erroring between the two (conceivable on the
    SIGKILL-timeout path if a sweep descendant survives and mutates the tree)
    previously tracebacked out of ``cmd_run`` BEFORE the report write: exit 1
    with NO report and no artifact. Any failure here now degrades to a
    fully-shaped compare block whose identity/audit/exit-status verdicts all
    FAIL naming the crash (``compare_error``), so the report-with-error is
    STILL written and the artifact ships (rc 1 via the judged identity FAIL —
    never a fabricated PASS).
    """
    try:
        return compare_arms(off_arm_dir, on_arm_dir, off_arm, on_arm)
    except Exception as exc:  # the report must ship — never a silent no-report exit
        detail = f"compare crashed: {exc!r} — identity NOT proven"
        return {
            "identity_ok": False,
            "result_files_compared": 0,
            "sweep_runs_files_compared": 0,
            "diff_paths": [],
            "audit_ts_masked_ok": False,
            "audit_detail": detail,
            "exit_status_structural_ok": False,
            "exit_status_detail": detail,
            "excluded_sidecars": list(STRUCTURAL_SIDECARS),
            "compare_error": repr(exc),
        }


def render_summary_guarded(report: dict[str, Any]) -> str:
    """:func:`render_summary`, degrading to an error stub instead of raising.

    The second half of the fix-round-3 no-report window: a summary-render
    crash must never lose the already-assembled ``soak-report.json`` — the
    caller writes the JSON first, then this text.
    """
    try:
        return render_summary(report)
    except Exception as exc:  # the JSON report is the primary artifact
        return (
            "# Kernel-cache OFF↔ON soak report\n\n"
            f"summary render FAILED: {exc!r}\n\n"
            "The machine-readable soak-report.json beside this file is the "
            "authoritative artifact — read the acceptance block there.\n"
        )


def build_smoke_lake(root: Path) -> Path:
    """A tiny REAL-writer NIFTY 1m fixture lake (the results-depth-wiring idiom)."""
    from datetime import timezone
    from decimal import Decimal

    from brutex.data.catalog import Catalog
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    ist = timezone(timedelta(hours=5, minutes=30))
    bars: list[CandleBar] = []
    cycle = 0
    for day in range(12):
        price = 100.0
        for minute in range(120):
            k = minute % 7
            if k == 0:
                cycle += 1
            if k == 3:
                o = price
                close, hi, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:
                o = price
                close, hi, lo = o - 0.65, o + 0.1, o - 0.75
            else:
                o = price + 3.0
                close, hi, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=day, minutes=minute)
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    lake = root / "smoke-lake"
    writer = ParquetWriter(lake_root=lake)
    with Catalog.open(lake / "catalog.duckdb") as catalog:
        sink = ParquetBarSink(writer=writer, catalog=catalog)
        sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)
    return lake


SMOKE_GRID: Final[dict[str, str | None]] = {
    "symbols": "NIFTY",
    "intervals": "1minute",
    "start": "2026-01-05",
    "end": "2026-01-17",
    "exchange": "NSE",
    "segment": "INDEX",
    "directions": "BUY,SELL",
    "k": "1,2",
    "conditions": "0,1,2,3,4",
    "sl": "80",
    "tp": "50",
    "top": "10",
    "fib_anchors": None,
    "price_source_cells": None,
    "max_tuples": None,
}


def _previous_utc_day() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).date().isoformat()


def cmd_run(args: argparse.Namespace) -> int:
    """The ``run`` subcommand — the full soak pipeline."""
    smoke = bool(args.smoke)

    if not BUDGET_MINUTES_MIN <= args.budget_minutes <= BUDGET_MINUTES_MAX:
        # R2 MINOR-5: the one-command fallback bypasses the workflow plan-job
        # fence — enforce the SAME 1..1195 law (SEC9-1) script-side.
        return _fail_structural(
            f"--budget-minutes {args.budget_minutes} outside "
            f"{BUDGET_MINUTES_MIN}..{BUDGET_MINUTES_MAX} (the SEC9-1 law the workflow "
            "plan job enforces: derived timeout m + 45 + m/6 must stay under the 24-h "
            "self-hosted token validity; a zero/negative budget would SIGTERM both "
            "arms instantly)"
        )

    if not math.isfinite(args.sample_interval_s) or not (
        SAMPLE_INTERVAL_S_MIN <= args.sample_interval_s <= SAMPLE_INTERVAL_S_MAX
    ):
        # Fix-round-6 MINOR (refuter-A): mirror the workflow plan-job fence
        # script-side (the --budget-minutes R2 MINOR-5 precedent). Executed
        # repros: 0/negative/nan fork a continuous `ps` storm beside the
        # measured arms (Event.wait(<=0|nan) returns immediately); inf kills
        # the daemon sampler after ONE sample (Event.wait(inf) raises
        # OverflowError). The explicit isfinite() belt is deliberate: nan
        # also fails the range compare, but the finiteness law is the
        # load-bearing property and must not depend on comparison semantics.
        return _fail_structural(
            f"--sample-interval-s {args.sample_interval_s} outside "
            f"{SAMPLE_INTERVAL_S_MIN}..{SAMPLE_INTERVAL_S_MAX} (must be a finite "
            "float; the workflow plan job fences 1..300 — 0/negative/nan fork a "
            "ps storm beside the measured arms, inf kills the sampler after one "
            "sample)"
        )

    if not MIN_FREE_GIB_MIN <= args.min_free_gib <= MIN_FREE_GIB_MAX:
        # Fix-round-2 NIT: the override exists for deliberately tiny bounded
        # non-smoke grids (and sandbox validation) but stays fenced — the
        # M4-Pro workflow never passes it, so its default 30 GiB stands.
        return _fail_structural(
            f"--min-free-gib {args.min_free_gib} outside {MIN_FREE_GIB_MIN}..{MIN_FREE_GIB_MAX}"
        )

    if args.workers is not None and args.workers < 2:
        return _fail_structural(
            "--workers must be >= 2: the kernel-cache telemetry lives in the parallel "
            "driver; a sequential run (workers=1) can never produce soak evidence"
        )

    out_dir = Path(args.out)
    if out_dir.exists() and not out_dir.is_dir():
        return _fail_structural(f"--out {out_dir} exists and is not a directory")
    if out_dir.is_dir() and any(out_dir.iterdir()):
        # R2 MAJOR-3: the fresh-out law. run_id = grid_identity(grid) carries
        # NO timestamp, so re-running into a used --out RESUMES the prior
        # arms into fake instant walls / zero tuples — refuse, never wipe
        # (the operator's dir, never delete data silently).
        return _fail_structural(
            f"--out {out_dir} is NOT empty — a prior run's arm/report artifacts would "
            "resume/cache-skip both arms into fake instant walls (run_id carries no "
            "timestamp). Use a FRESH --out per run: delete the old dir "
            f"(`rm -rf {out_dir}`) or pass a new path"
        )
    # Fix-round-2 MINOR (refuter-2): NO artifacts before the preflights pass.
    # A refused preflight must leave --out reusable for the documented
    # retry-with---lake-root flow, so report/ is created only AFTER every
    # preflight and a refusal leaves out_dir absent-or-EMPTY (an empty dir
    # passes the fresh-out guard above).
    if smoke:
        # The smoke fixture lake is written INTO out_dir, so the disk
        # preflight runs first (out_dir still empty on refusal).
        out_dir.mkdir(parents=True, exist_ok=True)
        disk_err = preflight_disk(out_dir, need_kib=2 * 1024 * 1024)
        if disk_err is not None:
            return _fail_structural(disk_err)
        lake_root = build_smoke_lake(out_dir)
        grid = dict(SMOKE_GRID)
        workers: int | None = args.workers if args.workers is not None else 2
        sample_interval = min(args.sample_interval_s, 0.5)
        no_options_sim = True
        results_depth_off = True
    else:
        lake_root = Path(
            args.lake_root
            or os.environ.get("BRUTEX_LAKE_ROOT")
            or (Path.home() / ".brutex" / "lake")
        )
        grid = {
            "symbols": args.symbols,
            "intervals": args.intervals,
            "start": args.start or "2020-01-01",
            "end": args.end or _previous_utc_day(),
            "exchange": args.exchange,
            "segment": args.segment,
            "directions": args.directions,
            "k": args.k,
            "conditions": args.conditions,
            "sl": args.sl,
            "tp": args.tp,
            "top": str(args.top),
            # R2 MAJOR-2: capacity-widening knobs — None keeps sweep-all's
            # single-anchor/single-source default (capacity 1); the report
            # states the measured capacity either way.
            "fib_anchors": args.fib_anchors,
            "price_source_cells": args.price_source_cells,
            "max_tuples": str(args.max_tuples) if args.max_tuples is not None else None,
        }
        workers = args.workers
        sample_interval = args.sample_interval_s
        no_options_sim = False
        results_depth_off = False

    # Fix-round-3 MINOR-1: resolve the PINNED sizing BEFORE any artifact —
    # the absolute RSS/pool bars must be measured at KNOWN sizing (the
    # default law), never a stray ambient/.env budget or worker count.
    workers_effective, workers_source = resolve_pinned_workers(workers)
    if workers_effective < 2:
        return _fail_structural(
            f"auto-resolved sweep workers={workers_effective} (< 2 on this box): the "
            "kernel-cache telemetry lives in the parallel driver, so a sequential "
            "soak can never produce evidence — pass --workers >= 2 explicitly"
        )
    mem_budget_bytes = resolve_pinned_mem_budget(workers_effective)

    missing = preflight_lake(
        lake_root,
        grid["symbols"] or "",
        grid["intervals"] or "",
        grid["exchange"] or "NSE",
        grid["segment"] or "INDEX",
    )
    if missing:
        # Non-smoke: NOTHING has been written yet — the same --out retries
        # cleanly with --lake-root (runbook §4's documented remediation).
        return _fail_structural(
            "lake preflight refused (never soak blind):\n  "
            + "\n  ".join(missing)
            + "\n  remediation: pass --lake-root (or the workflow lake_root input) "
            "pointing at the real lake and RETRY — the refusal wrote nothing, so "
            "the same --out is still fresh"
        )
    if not smoke:
        out_dir.mkdir(parents=True, exist_ok=True)
        disk_err = preflight_disk(out_dir, need_kib=args.min_free_gib * 1024 * 1024)
        if disk_err is not None:
            return _fail_structural(disk_err)  # out_dir left EMPTY → retry passes the guard
    report_dir = out_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    arm_order: tuple[str, ...] = ("off", "on") if args.arm_order == "off-first" else ("on", "off")
    provenance = collect_provenance(
        lake_root=lake_root,
        grid=grid,
        budget_minutes=args.budget_minutes,
        sample_interval_s=sample_interval,
        smoke=smoke,
        min_free_gib=None if smoke else args.min_free_gib,
        sweep_workers_effective=workers_effective,
        sweep_workers_source=workers_source,
        mem_budget_bytes_effective=mem_budget_bytes,
        arm_order=arm_order,
    )
    keys = int(provenance["grid"]["keys_fan_out"])
    # Full path in the LOCAL run log ONLY — the uploaded report carries the
    # home-redacted form (R1 MINOR-1).
    print(f"kernel-cache-soak: lake_root={lake_root}")
    print(
        "kernel-cache-soak: effective worker kernel capacity="
        f"{provenance['worker_kernel_capacity_effective']} (grid-derived default, "
        f"PINNED as {KERNEL_CAPACITY_ENV} in both arm envs — dotenv-immune)"
    )
    if provenance["capacity_env_ambient_present"]:
        print(
            f"kernel-cache-soak: ambient {KERNEL_CAPACITY_ENV} detected — the arm envs "
            "PIN the grid-derived default over it (and over any .env-file value the "
            "child's dotenv re-read would inject): the soak measures the DEFAULT "
            "capacity sizing law (fix-round-2 MAJOR-1)",
            file=sys.stderr,
        )
    deadline = time.monotonic() + args.budget_minutes * 60.0

    print(
        f"kernel-cache-soak: sizing PINNED — workers={workers_effective} "
        f"({workers_source}), mem_budget={mem_budget_bytes} bytes/worker (the engine "
        f"default law at eval-pool divisor {max(1, workers_effective - 1)}), tripwires "
        f"at engine defaults, arm order {'→'.join(a.upper() for a in arm_order)}"
    )
    arms: dict[str, dict[str, Any]] = {}
    for arm_name in arm_order:
        cache_on = arm_name == "on"
        arm_dir = out_dir / f"arm-{arm_name}"
        results_root = arm_dir / "results"  # FRESH per arm — the resume/cache-skip law
        argv = build_sweep_argv(
            sys.executable,
            lake_root=lake_root,
            results_root=results_root,
            grid=grid,
            no_options_sim=no_options_sim,
        )
        env = arm_env(
            dict(os.environ),
            cache_on=cache_on,
            workers=workers_effective,
            results_depth_off=results_depth_off,
            capacity=int(provenance["worker_kernel_capacity_effective"]),
            mem_budget_bytes=mem_budget_bytes,
            # Fix-round-3 NIT: per-arm forensics mirror — the shared
            # ~/.brutex/runs/<run_id> default would be clobbered cross-arm.
            mirror_root=arm_dir / "mirror",
            # Fix-round-4 MINOR: the SUSPECT-latch thresholds, pinned to the
            # ONE provenance-echoed engine-default set for both arms.
            tripwires=provenance["tripwire_env_pinned"],
        )
        print(f"kernel-cache-soak: arm {arm_name.upper()} starting ({' '.join(argv[3:])})")
        arms[arm_name] = run_arm(
            name=arm_name,
            argv=argv,
            env=env,
            arm_dir=arm_dir,
            results_root=results_root,
            sample_interval_s=sample_interval,
            deadline_monotonic=deadline,
        )
        print(
            f"kernel-cache-soak: arm {arm_name.upper()} done rc={arms[arm_name]['rc']} "
            f"wall={arms[arm_name]['wall_s']:.1f}s"
        )

    # Fix-round-3 (refuter-B NIT-1): the guarded compare + summary close the
    # narrow no-report window — a post-arm hashing/render crash still writes
    # the report-with-error (JSON first, it is the primary artifact).
    compare = compare_arms_guarded(
        out_dir / "arm-off",
        out_dir / "arm-on",
        arms["off"],
        arms["on"],
    )
    report = build_report(
        provenance=provenance,
        off=arms["off"],
        on=arms["on"],
        compare=compare,
        keys=keys,
        smoke=smoke,
    )
    (report_dir / "soak-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary_text = render_summary_guarded(report)
    (report_dir / "summary.md").write_text(summary_text, encoding="utf-8")
    for arm_name in ("off", "on"):
        log = out_dir / f"arm-{arm_name}" / "sweep.log"
        if log.is_file():  # bounded artifact: the last 64 KiB tail only
            data = log.read_bytes()[-65536:]
            (report_dir / f"arm-{arm_name}-sweep.tail.log").write_bytes(data)
    print(summary_text)

    failures = judged_failures(report["acceptance"])
    if failures:
        print(f"kernel-cache-soak: JUDGED FAILURES: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    """Re-evaluate the acceptance block from an existing soak-report.json."""
    path = Path(args.report)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Fix-round-7 NITs (refuter-A): the report-LOAD boundary joins the
        # named structural rc 2 tier — invalid-UTF-8 report bytes raise
        # UnicodeDecodeError INSIDE read_text (a ValueError subclass the old
        # net missed), and json.loads's C scanner raises RecursionError on an
        # absurdly deep report; both previously escaped with a raw traceback
        # at rc 1 (the judged-FAIL code, conflated with a real regression).
        return _fail_structural(f"cannot read report {path}: {exc!r}")
    if not isinstance(report, dict):
        # Fix-round-5 companion to the AttributeError net below: a top-level
        # JSON array/scalar would crash `.get` before the shape net.
        return _fail_structural(f"report shape invalid: top-level {type(report).__name__}")
    if report.get("kind") != REPORT_KIND or report.get("schema_version") != REPORT_SCHEMA_VERSION:
        return _fail_structural(
            f"not a {REPORT_KIND} v{REPORT_SCHEMA_VERSION} report: "
            f"kind={report.get('kind')!r} schema={report.get('schema_version')!r}"
        )
    try:
        nonfinite = find_nonfinite(report)
    except ReportTooDeepError as exc:
        # Fix-round-7 NIT-2 (refuter-A): the round-6 recursive walk crashed
        # with a raw RecursionError at rc 1 on a valid-but-deep report; the
        # iterative walk's depth bound refuses it NAMED at rc 2 instead.
        return _fail_structural(f"report shape invalid: {exc}")
    if nonfinite is not None:
        # Fix-round-6 NIT (refuter-A): json.loads accepts Infinity/NaN, and a
        # keys_fan_out of Infinity crashed int() with a raw OverflowError
        # traceback at rc 1 (the #1558 int(inf) class) while a nested-metric
        # Infinity could survive arithmetic and silently poison a judged bar
        # — refuse non-finite numerics at ingestion, NAMED.
        return _fail_structural(f"report shape invalid: non-finite number at {nonfinite}")
    try:
        # Fix-round-2 (refuter-1 spoof): trust the report's recorded key
        # source AND belt-check the LIVE environment — a verdict run under
        # the BENCH_BASELINE_KEY_ENV override is itself override-tainted.
        # Fix-round-3 NIT (refuter-A wording): the belt uses a DISTINCT
        # source token so the refusal attributes the LIVE verdict-time
        # override, never mis-claims the stored (probed) key's origin; a
        # report whose RECORDED source is already the run-time override keeps
        # that stronger attribution.
        key_source = report["provenance"].get("hardware_key_source")
        if os.environ.get(BENCH_BASELINE_KEY_ENV, "").strip() and not (
            isinstance(key_source, str) and key_source.startswith("env-override")
        ):
            key_source = f"env-override-at-verdict:{BENCH_BASELINE_KEY_ENV}"
        acceptance = evaluate_acceptance(
            off=report["arms"]["off"],
            on=report["arms"]["on"],
            compare=report["compare"],
            deltas=report["deltas"],
            keys=int(report["provenance"]["grid"]["keys_fan_out"]),
            smoke=bool(report["provenance"].get("smoke")),
            hardware_key=report["provenance"].get("hardware_key"),
            hardware_key_source=key_source if isinstance(key_source, str) else None,
        )
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        # AttributeError joined the net in fix-round-5 (refuter-A NIT-2): a
        # hand-mangled report whose "provenance" is a JSON string or whose
        # "arms" members are lists crashed `.get(...)` with a raw traceback
        # at rc 1 (the judged-FAIL code) instead of the structural rc 2
        # every sibling malformed shape gets. OverflowError joined in
        # fix-round-6 (refuter-A NIT-2, the #1558 int(inf) precedent) as the
        # defense-in-depth belt BEHIND the find_nonfinite ingestion scan
        # above — e.g. a finite-but-astronomical INT reaching a float
        # conversion is not a non-finite float the scan can see.
        return _fail_structural(f"report shape invalid: {exc!r}")
    for name, status in acceptance.items():
        print(f"{name}: {status}")
    print(
        "NOTE: soak-report.json is TRUSTED INPUT — evidence for the flip DEC, not a "
        "tamper-proof artifact; the operator runs the harness on their own box and "
        "this verdict re-derives from the report it is handed."
    )
    return 1 if judged_failures(acceptance) else 0


def build_parser() -> argparse.ArgumentParser:
    """The soak CLI parser."""
    parser = argparse.ArgumentParser(
        prog="kernel_cache_soak.py",
        description="Kernel-cache OFF↔ON A/B soak driver (DEC-SWEEP-WORKER-KERNEL-CACHE-SOAK-001)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="preflight → arm OFF → arm ON → compare → report")
    run.add_argument(
        "--lake-root", default=None, help="lake root (default $BRUTEX_LAKE_ROOT → ~/.brutex/lake)"
    )
    run.add_argument(
        "--out",
        required=True,
        help="output dir (arms + report/ land here) — must be FRESH (nonexistent or "
        "empty): a used dir is refused, prior artifacts would resume the arms into "
        "fake instant walls (R2 MAJOR-3)",
    )
    run.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    # DELIBERATELY a fixed literal, NOT
    # ``brutex.data.timeframes.DEFAULT_SWEEP_INTERVALS`` (DEC-SWEEP-TF-SCOPE-5-001).
    # This harness measures the worker kernel cache OFF vs ON, so its workload is a
    # MEASUREMENT CONSTANT: binding it to a moving production scope would silently
    # make historical soak reports incomparable to new ones. Two reasons, both real:
    # (a) this module keeps every ``brutex`` import LAZY so it stays importable in a
    # wheel-free env, and (b) a soak's value is cross-run comparability. Operators
    # who want the production scope pass ``--intervals`` explicitly.
    run.add_argument("--intervals", default="1minute,5minute,15minute")
    run.add_argument("--start", default=None, help="grid start (default 2020-01-01)")
    run.add_argument("--end", default=None, help="grid end (default: previous UTC day)")
    run.add_argument("--exchange", default="NSE")
    run.add_argument("--segment", default="INDEX")
    run.add_argument("--directions", default="BUY,SELL")
    run.add_argument("--k", default="1,2")
    run.add_argument("--conditions", default=None)
    run.add_argument("--sl", default="none")
    run.add_argument("--tp", default="none")
    run.add_argument("--top", type=int, default=20)
    run.add_argument(
        "--fib-anchors",
        default=None,
        help="sweep-all --fib-anchors passthrough (CSV; default unset = prev_day). "
        "Widens the kernel-cache capacity axis so the soak can exercise "
        "capacity > 1 — the report states the measured capacity (R2 MAJOR-2)",
    )
    run.add_argument(
        "--price-source-cells",
        default=None,
        help="sweep-all --price-source-cells passthrough (CSV; default unset = "
        "hlc3:close). The second capacity-widening axis (R2 MAJOR-2)",
    )
    run.add_argument(
        "--max-tuples",
        type=int,
        default=None,
        help="passed through to sweep-all as the REFUSAL guard (bound the grid via "
        "--k/--conditions/--sl/--tp — sweep-all refuses, it never truncates)",
    )
    run.add_argument(
        "--workers",
        type=int,
        default=None,
        help="explicit BRUTEX_SWEEP_WORKERS for both arms (>= 2; default: auto)",
    )
    run.add_argument(
        "--budget-minutes",
        type=int,
        default=600,
        help=f"both-arms wall budget, fenced {BUDGET_MINUTES_MIN}..{BUDGET_MINUTES_MAX} "
        "(the workflow plan-job SEC9-1 law, enforced script-side too)",
    )
    run.add_argument(
        "--sample-interval-s",
        type=float,
        default=5.0,
        help=f"RSS sampler cadence in seconds, fenced finite {SAMPLE_INTERVAL_S_MIN}.."
        f"{SAMPLE_INTERVAL_S_MAX:g} script-side (fix-round-6; the workflow plan job "
        "fences 1..300 — smoke additionally caps the effective cadence at 0.5)",
    )
    run.add_argument(
        "--arm-order",
        choices=("off-first", "on-first"),
        default="off-first",
        help="arm execution order (default off-first). The SECOND arm inherits a "
        "warm OS page-cache/import state — a systematic bias inside the ≤ 1.02 "
        "wall band; re-run with the order reversed for a bias check (fix-round-3 "
        "NIT-1). Echoed as provenance.arm_order",
    )
    run.add_argument(
        "--min-free-gib",
        type=int,
        default=MIN_FREE_GIB_DEFAULT,
        help=f"NON-smoke free-disk requirement in GiB, fenced {MIN_FREE_GIB_MIN}.."
        f"{MIN_FREE_GIB_MAX} (default {MIN_FREE_GIB_DEFAULT} — right-sized for the "
        "M4-Pro default grid; the workflow never passes this, so lowering it is an "
        "operator judgment for a deliberately tiny bounded grid / sandbox "
        "validation, echoed into provenance.min_free_gib; smoke keeps its own "
        "fixed 2 GiB need)",
    )
    run.add_argument(
        "--smoke",
        action="store_true",
        help="sandbox mechanics proof: tiny real-writer fixture lake + tiny grid; "
        "numbers are NEVER decision-grade (report stamps smoke=true)",
    )
    run.set_defaults(func=cmd_run)

    verdict = sub.add_parser("verdict", help="re-evaluate acceptance from a soak-report.json")
    verdict.add_argument("--report", required=True)
    verdict.set_defaults(func=cmd_verdict)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
