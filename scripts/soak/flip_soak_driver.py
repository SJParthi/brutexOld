#!/usr/bin/env python3
"""flip_soak_driver.py — the flip-soak IN-PROCESS drift leg.

DEC-FLIP-SOAK-HARNESS-001 (MIGRATION.md #1894). Repeated
ON-arm runs for the dispatched wall-clock budget, each run (or synthetic
window) fed to the pure :class:`brutex.observability.soak_drift.SoakDriftMonitor`
— the DEC-SOAK-REAL-TIER-001 gate law inherited VERBATIM:

* ``soak.rss_ceiling`` + ``soak.fd_growth`` are DETERMINISTIC HARD GATES
  (a breach fails this process → the harness verdict is FAIL) AND fire the
  best-effort real-time :meth:`SweepAlerter.soak_drift` push (a silent no-op
  when Telegram is disabled — same honest OH7-1 disclosure as
  soak-selfhosted.yml: this driver wires no Telegram secrets).
* ``soak.p99_drift`` is OBSERVE-ONLY (the documented ~15% false-fail +
  hysteresis edge-storm rationale in ``tests/stress/test_soak_sustained.py``
  applies unchanged) — visible via structlog + the ``brutex_soak_p99_ratio``
  gauge, never a gate, never a push.

WHY in-process (design §2): ``process_probe.read_rss_kib`` /
``read_fd_count`` read the CURRENT process, and cross-process fd counting on
macOS would need ``lsof`` polling; running the sweep entry in-process also
makes cumulative leak detection STRONGER — state that should be freed
between runs accumulates visibly. The RSS rule is a CEILING backstop, not a
growth slope (macOS ``ru_maxrss`` is a process-lifetime PEAK — see
``process_probe.py``).

Modes:

* **real** (default) — per window: render the flag's registry twin argv into
  a FRESH per-window scratch results root (so a resume-skip can never fake a
  free window) and run ``brutex.cli.main`` in-process with the flag env set;
  the window's latency sample is the run's wall seconds. HONEST (Golden Rule
  #10): the real mode has NOT been executed on the M4-Pro box in this PR —
  the real lake exists only there; the dry mode below is the CI-executed
  proof of the driver's own logic.
* **--dry** — the fake-lake CI mode (the ci.yml sweep-rehearsal shape):
  synthetic hash-op windows, REAL process-probe reads, the REAL monitor —
  runs in shared CI in seconds with no lake and no M4-Pro.

Exit codes: 0 = clean (zero hard-gate breaches, zero run failures);
1 = hard-gate breach or a window run error; 2 = refusal (preset registry
flag / missing lake root / bad args).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, sleep

# Script-AND-module import resolution (the scripts/ci _textscan idiom): the
# registry sits beside this file; tests import both by name off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import TYPE_CHECKING

from flag_registry import FlagSpec, all_specs, render_argv, spec_for

if TYPE_CHECKING:  # heavy brutex imports stay lazy at runtime (CLI --help speed)
    from brutex.observability.sla import SLAEvent
    from brutex.observability.soak_drift import SoakDriftMonitor

#: The two DETERMINISTIC hard-gate rules (DEC-SOAK-REAL-TIER-001 — p99 is
#: observe-only; ONE predicate for gate + push so they can never drift).
HARD_GATE_RULE_NAMES: tuple[str, str] = ("soak.rss_ceiling", "soak.fd_growth")

#: Dry-mode window slice (seconds) — small so CI tests finish in seconds.
DRY_WINDOW_SECONDS: float = 0.05

#: Dry-mode ops per clock re-check batch.
DRY_OPS_PER_BATCH: int = 64

#: Per-window outcome classes. A transient env move (a peer pull/heal holding
#: the lake lock → ``LakeBusyError``, or the per-window ``data_snapshot_hash``
#: moving under the arm) is NOT a kernel failure — it is retried (bounded) and,
#: if it persists / is observed, folds to the RETRY_ENV_MOVED verdict, never a
#: FAIL that would falsely blame the Rust kernel (security MAJOR-1).
WINDOW_OK: str = "ok"
WINDOW_KERNEL_FAIL: str = "kernel_fail"
WINDOW_ENV_MOVED: str = "env_moved"

#: Bounded per-window retry budget for a transient env move — a single lake
#: blip must not abort the whole soak (security MAJOR-1(b)).
DEFAULT_MAX_WINDOW_ATTEMPTS: int = 3


@dataclass(frozen=True, slots=True)
class DriftResult:
    """One drift leg's fold — serialized verbatim into the fragment JSON."""

    mode: str
    minutes: float
    windows: int
    rss_peak_kib: int | None
    rss_ceiling_kib: int
    fd_baseline: int | None
    fd_final: int | None
    p99_ratio_final: float | None
    hard_gate_breaches: int
    breach_rules: tuple[str, ...]
    run_failures: int
    env_retries: int

    def to_json_obj(self) -> dict[str, object]:
        """The fragment object ``emit_pass.py`` folds into PASS.json."""
        return {
            "mode": self.mode,
            "minutes": self.minutes,
            "windows": self.windows,
            "rss_peak_kib": self.rss_peak_kib,
            "rss_ceiling_kib": self.rss_ceiling_kib,
            "fd_baseline": self.fd_baseline,
            "fd_final": self.fd_final,
            "p99_ratio_final": self.p99_ratio_final,
            "hard_gate_breaches": self.hard_gate_breaches,
            "breach_rules": list(self.breach_rules),
            "run_failures": self.run_failures,
            "env_retries": self.env_retries,
        }


def _refuse_preset_flags() -> str | None:
    """The one-flag-at-a-time law — None when clean, else the offending flag."""
    for spec in all_specs():
        if os.environ.get(spec.flag):
            return spec.flag
    return None


def _build_monitor(rss_ceiling_kib: int, breaches: list[str]) -> SoakDriftMonitor:
    """Construct the REAL SoakDriftMonitor wired per the soak-driver law."""
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.observability.metrics import MetricRegistry
    from brutex.observability.soak_drift import SoakDriftMonitor

    alerter = SweepAlerter()

    def _on_breach(event: SLAEvent) -> None:
        rule = event.rule_name
        breaches.append(rule)
        if rule not in HARD_GATE_RULE_NAMES:
            # p99 is observe-only: recorded (visible in breach_rules) but it
            # neither pushes nor gates — the test_soak_sustained.py law.
            return
        alerter.soak_drift(
            rule=rule,
            observed=float(event.observed_value),
            threshold=float(event.threshold),
            detail="flip-soak drift breach — check RSS / fd over the run",
        )

    def _on_recovery(_event: SLAEvent) -> None:
        return None

    return SoakDriftMonitor(
        registry=MetricRegistry(),
        on_breach=_on_breach,
        on_recovery=_on_recovery,
        rss_ceiling_kib=float(rss_ceiling_kib),
    )


def _probe() -> tuple[int | None, int | None]:
    """One (rss_kib, fd_count) self-probe — ``None`` = platform abstain."""
    from brutex.observability.process_probe import read_fd_count, read_rss_kib

    return read_rss_kib(), read_fd_count()


def _dry_window(window_seconds: float) -> list[float]:
    """One synthetic hash-op window; returns its per-op latency samples."""
    latencies: list[float] = []
    deadline = perf_counter() + window_seconds
    counter = 0
    while perf_counter() < deadline:
        for _ in range(DRY_OPS_PER_BATCH):
            t0 = perf_counter()
            hashlib.sha256(counter.to_bytes(8, "little")).hexdigest()
            latencies.append(perf_counter() - t0)
            counter += 1
    if not latencies:  # pragma: no cover — a zero-length window under extreme load
        sleep(window_seconds)
    return latencies


def _is_lake_busy(exc: BaseException) -> bool:
    """True when *exc* is (or subclasses) ``LakeBusyError`` — matched BY NAME.

    ``cmd_sweep_all`` raises ``brutex.engine.sweep_singleflight.LakeBusyError``
    when a peer pull/heal holds the lake pull lock exclusively (G10 preload
    guard). Matching by class name keeps this driver importable beside the
    registry without a hard dependency on the exact module path.
    """
    return any(base.__name__ == "LakeBusyError" for base in type(exc).__mro__)


def _real_window(
    spec: FlagSpec, *, lake_root: str, results_root: Path, scale: str, index: int
) -> tuple[list[float], str, str]:
    """One real ON-arm run in-process; returns (latency samples, outcome, snapshot).

    ``outcome`` is one of :data:`WINDOW_OK` / :data:`WINDOW_KERNEL_FAIL` /
    :data:`WINDOW_ENV_MOVED` — a ``LakeBusyError`` (a peer pull/heal holds the
    lake lock) is classified ENV_MOVED, never a kernel failure. ``snapshot`` is
    the window's ``data_snapshot_hash`` (empty when unavailable), used by the
    caller to detect a per-window lake move.
    """
    import brutex.cli as brutex_cli
    from flag_registry import snapshot_token

    window_root = results_root / f"w{index:04d}"
    window_root.mkdir(parents=True, exist_ok=True)
    argv = list(render_argv(spec, lake_root=lake_root, results_root=str(window_root), scale=scale))[
        1:
    ]  # drop the "brutex" program token — cli.main takes the sub-argv
    t0 = perf_counter()
    try:
        rc = brutex_cli.main(argv)
    except SystemExit as exc:  # argparse exits carry the status in .code
        rc = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # LakeBusyError is a transient env move, not a bug
        if _is_lake_busy(exc):
            return [perf_counter() - t0], WINDOW_ENV_MOVED, ""
        raise
    wall = perf_counter() - t0
    outcome = WINDOW_OK if rc == 0 else WINDOW_KERNEL_FAIL
    return [wall], outcome, snapshot_token(window_root)


#: A window runner: given the 0-based window index, returns
#: ``(latency_samples, outcome, snapshot)`` (see :func:`_real_window`).
WindowRunner = Callable[[int], "tuple[list[float], str, str]"]


def _default_window_runner(
    spec: FlagSpec,
    *,
    dry: bool,
    lake_root: str,
    results_root: Path,
    scale: str,
    window_seconds: float,
) -> WindowRunner:
    """The production window runner — dry synthetic ops or a real ON-arm run."""

    def runner(index: int) -> tuple[list[float], str, str]:
        if dry:
            return _dry_window(window_seconds), WINDOW_OK, ""
        return _real_window(
            spec, lake_root=lake_root, results_root=results_root, scale=scale, index=index
        )

    return runner


def run_drift_leg(
    *,
    flag: str,
    minutes: float,
    lake_root: str,
    results_root: Path,
    scale: str,
    dry: bool,
    rss_ceiling_kib: int | None = None,
    max_windows: int | None = None,
    window_seconds: float = DRY_WINDOW_SECONDS,
    max_window_attempts: int = DEFAULT_MAX_WINDOW_ATTEMPTS,
    window_runner: WindowRunner | None = None,
) -> DriftResult:
    """Run the drift leg and fold the verdict (pure of I/O beyond the runs).

    A transient env move (``LakeBusyError`` or a per-window
    ``data_snapshot_hash`` change) is retried up to *max_window_attempts* and,
    when observed, folds to a RETRY (``env_retries > 0``) verdict — never a
    kernel FAIL (security MAJOR-1). Only a genuine non-zero kernel rc counts as
    a ``run_failure`` that fails the leg. ``window_runner`` overrides the
    default runner (the test seam for simulating both classes).
    """
    spec = spec_for(flag)
    ceiling = rss_ceiling_kib if rss_ceiling_kib is not None else spec.rss_ceiling_kib
    breaches: list[str] = []
    monitor = _build_monitor(ceiling, breaches)
    runner = window_runner or _default_window_runner(
        spec,
        dry=dry,
        lake_root=lake_root,
        results_root=results_root,
        scale=scale,
        window_seconds=window_seconds,
    )

    deadline = perf_counter() + minutes * 60.0
    windows = 0
    run_failures = 0
    env_retries = 0
    baseline_snapshot: str | None = None
    rss_peak: int | None = None
    fd_baseline: int | None = None
    fd_final: int | None = None
    # A real leg sets the flag env for the whole loop (an injected runner drives
    # its own state, so it never mutates the ambient env).
    manage_env = not dry and window_runner is None
    if manage_env:
        os.environ[flag] = "1"
        for key, value in spec.extra_env:
            os.environ[key] = value
    try:
        while True:
            # Per-window bounded retry: a transient env move is retried; the
            # last non-env outcome (or the exhausted-retry env move) is folded.
            outcome = WINDOW_ENV_MOVED
            latencies: list[float] = []
            snapshot = ""
            for attempt in range(max_window_attempts):
                latencies, outcome, snapshot = runner(windows)
                if outcome == WINDOW_OK and snapshot and baseline_snapshot not in (None, snapshot):
                    # The lake's data_snapshot_hash moved under this window —
                    # treat exactly like a LakeBusyError (env move, not a bug).
                    outcome = WINDOW_ENV_MOVED
                if outcome != WINDOW_ENV_MOVED:
                    break
                env_retries += 1
                if attempt + 1 < max_window_attempts:
                    continue  # retry the same window
            if outcome == WINDOW_OK and snapshot and baseline_snapshot is None:
                baseline_snapshot = snapshot
            if outcome == WINDOW_KERNEL_FAIL:
                run_failures += 1
            rss_kib, fd_count = _probe()
            if rss_kib is not None:
                rss_peak = rss_kib if rss_peak is None else max(rss_peak, rss_kib)
            if fd_count is not None:
                if fd_baseline is None:
                    fd_baseline = fd_count
                fd_final = fd_count
            monitor.observe_window(rss_kib=rss_kib, fd_count=fd_count, latency_samples=latencies)
            windows += 1
            if max_windows is not None and windows >= max_windows:
                break
            if perf_counter() >= deadline:
                break
            if outcome == WINDOW_KERNEL_FAIL:
                # A failing run would loop failures for the whole budget —
                # abort the leg after the first (recorded, verdict FAIL).
                break
            if outcome == WINDOW_ENV_MOVED:
                # The lake is moving under us — stop and fold to RETRY_ENV_MOVED
                # (rerun outside the nightly window), never a kernel FAIL.
                break
    finally:
        if manage_env:
            os.environ.pop(flag, None)

    p99_ratio = _p99_ratio(monitor)
    hard = tuple(rule for rule in breaches if rule in HARD_GATE_RULE_NAMES)
    return DriftResult(
        mode="dry" if dry else "real",
        minutes=minutes,
        windows=windows,
        rss_peak_kib=rss_peak,
        rss_ceiling_kib=ceiling,
        fd_baseline=fd_baseline,
        fd_final=fd_final,
        p99_ratio_final=p99_ratio,
        hard_gate_breaches=len(hard),
        breach_rules=tuple(breaches),
        run_failures=run_failures,
        env_retries=env_retries,
    )


def _p99_ratio(monitor: SoakDriftMonitor) -> float | None:
    """Read the monitor's final p99 drift ratio (None = abstained).

    Reads the monitor's provider closure directly (a deliberate friend
    access — the ratio is already mirrored to the ``brutex_soak_p99_ratio``
    gauge; this avoids re-parsing the registry render).
    """
    ratio = monitor._p99_ratio_provider()  # pyright: ignore[reportPrivateUsage]
    return float(ratio) if ratio is not None else None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see the module docstring for the contract."""
    parser = argparse.ArgumentParser(description="flip-soak in-process drift leg")
    parser.add_argument("--flag", required=True)
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--lake-root", required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--scale", default="default")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--rss-ceiling-kib", type=int, default=None)
    parser.add_argument("--max-windows", type=int, default=None)
    parser.add_argument("--window-seconds", type=float, default=DRY_WINDOW_SECONDS)
    args = parser.parse_args(argv)

    offender = _refuse_preset_flags()
    if offender is not None:
        print(
            f"REFUSED: registry flag {offender} is pre-set in the ambient env — "
            "the one-flag-at-a-time law requires a clean env (the driver sets "
            "the flag under test itself)."
        )
        return 2
    try:
        spec_for(args.flag)
    except KeyError as exc:
        print(f"REFUSED: {exc}")
        return 2
    if not args.dry and not Path(args.lake_root).is_dir():
        print(f"REFUSED: lake root {args.lake_root!r} is not a directory")
        return 2
    if args.minutes < 0:
        print("REFUSED: --minutes must be >= 0")
        return 2

    result = run_drift_leg(
        flag=args.flag,
        minutes=args.minutes,
        lake_root=args.lake_root,
        results_root=Path(args.results_root),
        scale=args.scale,
        dry=args.dry,
        rss_ceiling_kib=args.rss_ceiling_kib,
        max_windows=args.max_windows,
        window_seconds=args.window_seconds,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result.to_json_obj(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Verdict precedence: a genuine kernel failure or a hard-gate breach FAILs
    # (exit 1); otherwise a transient env move folds to RETRY_ENV_MOVED (exit 3,
    # rerun outside the nightly window); otherwise clean PASS (exit 0).
    failed = result.hard_gate_breaches > 0 or result.run_failures > 0
    if failed:
        verdict, rc = "FAIL", 1
    elif result.env_retries > 0:
        verdict, rc = "RETRY_ENV_MOVED", 3
    else:
        verdict, rc = "PASS", 0
    print(
        f"FLIP-SOAK DRIFT {verdict} (windows={result.windows} "
        f"rss_peak_kib={result.rss_peak_kib} hard_breaches={result.hard_gate_breaches} "
        f"run_failures={result.run_failures} env_retries={result.env_retries})"
    )
    return rc


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
