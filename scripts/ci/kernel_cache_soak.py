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
  ``provenance.worker_kernel_capacity_effective`` (the grid-derived default;
  any ambient ``BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY`` is POPPED from both arm
  envs so the flip's default sizing law is what gets measured). The default
  grid measures capacity=1; widen ``--fib-anchors`` / ``--price-source-cells``
  to soak a higher capacity — the flip DEC must weigh whether the measured
  capacity covers the operator's grids (R2 MAJOR-2 honesty scoping).
* A non-smoke report whose ``hardware_key`` is not the registered §8 M4-Pro
  envelope can never read FLIP-READY — the verdict reads NOT-DECISION-GRADE
  (numbers from any other machine are UNVERIFIED lab evidence, the
  DEC-SWEEP-CAP-BENCH-001 clause).
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
#: POPPED from both arm envs (R2 MAJOR-1): the flip ships the DEFAULT sizing
#: law, so the evidence must measure it — an ambient operator override would
#: silently re-scope the RSS bar. Drift-pinned against the engine constant in
#: tests/unit/test_kernel_cache_soak.py (TestEngineDriftPins).
KERNEL_CAPACITY_ENV: Final[str] = "BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY"
#: Mirror of sweep_parallel.WORKER_KERNEL_CAPACITY_MAX (drift-pinned).
WORKER_KERNEL_CAPACITY_MAX: Final[int] = 64

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
    "*.stamp.json + stage2 (wall-stamped content-hash sidecars)",
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

    The arms run with :data:`KERNEL_CAPACITY_ENV` POPPED (R2 MAJOR-1), so the
    engine resolves its grid-derived default —
    ``min(max(1, proximities × fib_anchors × price_source_cells),
    WORKER_KERNEL_CAPACITY_MAX)`` with proximity the CLI singleton ``[None]``
    (``sweep_parallel.worker_kernel_capacity_default``; drift-pinned in
    TestEngineDriftPins). Echoed into ``provenance`` so every report states
    the capacity its RSS bar was evaluated at.
    """
    return min(
        max(1, _csv_count(fib_anchors) * _csv_count(price_source_cells)),
        WORKER_KERNEL_CAPACITY_MAX,
    )


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
    workers: int | None,
    results_depth_off: bool,
) -> dict[str, str]:
    """One arm's spawn environment — the flag + determinism/visibility pins.

    Any ambient :data:`KERNEL_CAPACITY_ENV` is POPPED (R2 MAJOR-1): the flip
    ships the DEFAULT capacity sizing law, so both arms must measure it — an
    operator daemon's per-box override would silently re-scope the RSS bar.
    The pop is echoed in ``provenance.capacity_env_ambient_popped``.
    """
    env = dict(base)
    env.pop(KERNEL_CAPACITY_ENV, None)
    env[KERNEL_CACHE_ENV] = "1" if cache_on else "0"
    env[LOG_LEVEL_ENV] = "INFO"  # the telemetry MUST reach stderr (#1630 guard)
    env["PYTHONHASHSEED"] = "0"
    if workers is not None:
        env[SWEEP_WORKERS_ENV] = str(workers)
    if results_depth_off:
        env[RESULTS_DEPTH_ENV] = "0"
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
) -> dict[str, str]:
    """The acceptance block: PASS/FAIL per bar + an ADVISORY verdict.

    In ``--smoke`` mode the RSS/wall bars are stamped ``SMOKE-UNJUDGED`` (a
    fixture grid cannot produce decision-grade numbers) and the verdict can
    never read FLIP-READY; the mechanics bars (identity / clean exits /
    telemetry) are always judged. A NON-smoke run whose *hardware_key* does
    not carry the registered §8 M4-Pro signature
    (:data:`M4PRO_KEY_SUFFIX`) reads a ``provenance`` bar of
    NOT-DECISION-GRADE and the verdict can never read FLIP-READY either
    (R2 MINOR-4 / runbook §5 bar 6 — UNVERIFIED lab evidence is not a
    harness FAILURE, so it does not drive the exit code). The flip DEC
    quotes the RAW figures — this block is advisory, never the decision.
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
        f"- per-worker peak RSS delta: {fmt_kib(deltas.get('rss_per_worker_kib'))} "
        f"(ceiling {fmt_kib(RSS_PER_WORKER_DELTA_CEILING_KIB)})",
        f"- pool peak RSS delta: {fmt_kib(deltas.get('rss_pool_kib'))}; "
        f"ON pool peak {fmt_kib(on.get('pool_peak_rss_kib'))} vs ceiling "
        f"{fmt_kib(POOL_PEAK_CEILING_KIB)} — actual headroom "
        f"{fmt_kib(POOL_PEAK_CEILING_KIB - on['pool_peak_rss_kib']) if isinstance(on.get('pool_peak_rss_kib'), int) else '?'}",
        "- full-grid wall saving: MODELED only (extrapolated off this bounded grid; the "
        "2-4-day full sweep is never run twice in one job — SEC9-1 token fence)",
        f"- RSS bar evaluated at kernel-cache capacity="
        f"{prov.get('worker_kernel_capacity_effective')} (the grid-derived default; any "
        f"ambient {KERNEL_CAPACITY_ENV} is POPPED from both arm envs"
        + (" — one WAS set and popped" if prov.get("capacity_env_ambient_popped") else "")
        + ") — the flip DEC must weigh whether this capacity covers the operator's grids "
        "(widen via --fib-anchors / --price-source-cells)",
        "",
        "## Identity (MEASURED)",
        "",
        f"- result trees ts-masked identical: {compare.get('identity_ok')} "
        f"({len(compare.get('diff_paths', []))} diffs)",
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
    ps_bin = shutil.which("df")
    if ps_bin is None:
        return "df not found — cannot verify free disk; refusing to soak blind"
    proc = subprocess.run(  # noqa: S603 — fixed argv, never a shell
        [ps_bin, "-Pk", str(out_dir)], capture_output=True, text=True, check=False
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
    # sweep-all's own defaults when the axes are unset (cli.py cmd_sweep_all).
    fib_csv = grid.get("fib_anchors") or "prev_day"
    ps_csv = grid.get("price_source_cells") or "hlc3:close"
    return {
        "commit": guarded(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "hardware_key": hw_key,
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
        # The EFFECTIVE capacity both arms measure (R2 MAJOR-1: the ambient
        # override is popped by arm_env, so the grid-derived default rules).
        "worker_kernel_capacity_effective": effective_worker_kernel_capacity(fib_csv, ps_csv),
        "capacity_env_ambient_popped": KERNEL_CAPACITY_ENV in os.environ,
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
    off_root: Path, on_root: Path, off_arm: dict[str, Any], on_arm: dict[str, Any]
) -> dict[str, Any]:
    """The ts-masked identity compare (result trees + audit chains + status)."""
    off_tree = result_tree(off_root)
    on_tree = result_tree(on_root)
    diffs = tree_diff(off_tree, on_tree)
    audit_ok = False
    audit_detail = "audit chain missing on one side"
    off_chain = off_root / "audit" / "sweep-all.jsonl"
    on_chain = on_root / "audit" / "sweep-all.jsonl"
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
    return {
        "identity_ok": not diffs and bool(off_tree),
        "result_files_compared": len(set(off_tree) | set(on_tree)),
        "diff_paths": diffs[:50],
        "audit_ts_masked_ok": audit_ok,
        "audit_detail": audit_detail,
        "exit_status_structural_ok": status_ok,
        "exit_status_detail": status_detail,
        "excluded_sidecars": list(STRUCTURAL_SIDECARS),
    }


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
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir = out_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    if smoke:
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

    missing = preflight_lake(
        lake_root,
        grid["symbols"] or "",
        grid["intervals"] or "",
        grid["exchange"] or "NSE",
        grid["segment"] or "INDEX",
    )
    if missing:
        return _fail_structural(
            "lake preflight refused (never soak blind):\n  "
            + "\n  ".join(missing)
            + "\n  remediation: pass --lake-root (or the workflow lake_root input) "
            "pointing at the real lake"
        )
    disk_err = preflight_disk(out_dir, need_kib=2 * 1024 * 1024 if smoke else 30 * 1024 * 1024)
    if disk_err is not None:
        return _fail_structural(disk_err)

    provenance = collect_provenance(
        lake_root=lake_root,
        grid=grid,
        budget_minutes=args.budget_minutes,
        sample_interval_s=sample_interval,
        smoke=smoke,
    )
    keys = int(provenance["grid"]["keys_fan_out"])
    # Full path in the LOCAL run log ONLY — the uploaded report carries the
    # home-redacted form (R1 MINOR-1).
    print(f"kernel-cache-soak: lake_root={lake_root}")
    print(
        "kernel-cache-soak: effective worker kernel capacity="
        f"{provenance['worker_kernel_capacity_effective']} (grid-derived default)"
    )
    if provenance["capacity_env_ambient_popped"]:
        print(
            f"kernel-cache-soak: ambient {KERNEL_CAPACITY_ENV} detected — POPPED from "
            "both arm envs: the soak measures the DEFAULT capacity sizing law "
            "(R2 MAJOR-1)",
            file=sys.stderr,
        )
    deadline = time.monotonic() + args.budget_minutes * 60.0

    arms: dict[str, dict[str, Any]] = {}
    for arm_name, cache_on in (("off", False), ("on", True)):
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
            workers=workers,
            results_depth_off=results_depth_off,
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

    compare = compare_arms(
        out_dir / "arm-off" / "results",
        out_dir / "arm-on" / "results",
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
    (report_dir / "summary.md").write_text(render_summary(report), encoding="utf-8")
    for arm_name in ("off", "on"):
        log = out_dir / f"arm-{arm_name}" / "sweep.log"
        if log.is_file():  # bounded artifact: the last 64 KiB tail only
            data = log.read_bytes()[-65536:]
            (report_dir / f"arm-{arm_name}-sweep.tail.log").write_bytes(data)
    print(render_summary(report))

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
    except (OSError, json.JSONDecodeError) as exc:
        return _fail_structural(f"cannot read report {path}: {exc}")
    if report.get("kind") != REPORT_KIND or report.get("schema_version") != REPORT_SCHEMA_VERSION:
        return _fail_structural(
            f"not a {REPORT_KIND} v{REPORT_SCHEMA_VERSION} report: "
            f"kind={report.get('kind')!r} schema={report.get('schema_version')!r}"
        )
    try:
        acceptance = evaluate_acceptance(
            off=report["arms"]["off"],
            on=report["arms"]["on"],
            compare=report["compare"],
            deltas=report["deltas"],
            keys=int(report["provenance"]["grid"]["keys_fan_out"]),
            smoke=bool(report["provenance"].get("smoke")),
            hardware_key=report["provenance"].get("hardware_key"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _fail_structural(f"report shape invalid: {exc!r}")
    for name, status in acceptance.items():
        print(f"{name}: {status}")
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
    run.add_argument("--sample-interval-s", type=float, default=5.0)
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
