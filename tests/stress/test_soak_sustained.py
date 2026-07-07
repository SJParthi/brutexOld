"""Real long-soak DRIVER — the wall-clock runner behind the pure drift detector.

DEC-SOAK-REAL-TIER-001 (MIGRATION.md entry #1540). This is the ONE code path
that a 30-second nightly regression AND a multi-hour self-hosted soak both take
(SCALABLE — the only difference is the wall-clock budget). It drives the two hot
paths a real pull exercises — the INGEST audit-chain append
(:class:`brutex.orchestrator.audit.AuditWriter`) and the SWEEP combo-dedup key
(:func:`brutex.engine.sweep_cache.combo_digest`) — binned into time windows, and
feeds each window's RSS / fd / per-op latency into the pure
:class:`brutex.observability.soak_drift.SoakDriftMonitor`. The drift LOGIC lives
in that COMMON module (unit-tested to 100% line+branch on synthetic series); THIS
file is the honest wall-clock exerciser.

The failure signal is AUTOMATIC (CLAUDE.md §11, never eyeball-only). A per-window
RSS-ceiling or fd-growth breach (a) fires the monitor's best-effort ``on_breach``
alert IN REAL TIME — the driver WIRES it to
:meth:`brutex.notifications.sweep_alerts.SweepAlerter.soak_drift` (mirroring
DEC-PULL-HEALTH-WIRING-001, which bound the originally-dormant pull-health
monitor), a CRITICAL, trigger-independent push that fires the INSTANT a
DETERMINISTIC rule edges into breach and is a silent no-op when Telegram is
disabled (CI/dev), so it never sends spuriously nor blocks the run. The noisy
``soak.p99_drift`` rule is DELIBERATELY EXCLUDED from this real-time push (see
below), so an oscillating sub-second p99 cannot storm the operator.

The **hard CI gate** (b) is the DETERMINISTIC oracles only — the RSS ceiling, the
fd-growth count, and the audit-chain integrity are asserted at end-of-run so a
leak fails CI. ``soak.p99_drift`` is deliberately **observe-only** — neither a
hard CI gate NOR a real-time CRITICAL push: a sub-second window's p99 is
dominated by transient fsync/GC/scheduler spikes (the ingest ``audit.append``
fsyncs per record), so comparing a later window's p99 to a single warm
first-window baseline at ratio 2.0 fires on NOISE, not drift — reproduced as a
~15% false-fail rate on an idle box (ratios up to 8–9×). Because that noise
OSCILLATES (not a monotone leak), the ``SLADetector`` hysteresis fires a fresh
OK→BREACH edge on every up-crossing, so wiring it to the CRITICAL ``soak_drift``
push would emit thousands of pushes over a 6h soak and train the operator to
ignore soak alerts (§11) — the exact anti-pattern that motivated demoting it from
the gate. So the driver excludes it from BOTH the gate (:func:`_assert_healthy`)
AND the real-time push (:func:`_drive._on_breach`) via ONE shared predicate
(:func:`_fires_realtime_push`); the p99 signal stays visible via the monitor's
structlog breach warning and the ``brutex_soak_p99_ratio`` /metrics gauge, never
via a phone push (DEC-SOAK-REAL-TIER-001). The deterministic MONOTONE rules —
``soak.rss_ceiling`` and ``soak.fd_growth`` — DO both hard-gate CI and fire the
real-time push: a genuine leak crosses once, so one honest CRITICAL.

## Two entry points, ONE driver (honest tier split)

* :func:`test_soak_short` — ``@pytest.mark.stress``, a FIXED short budget, so it
  runs in the normal nightly ``pytest tests/stress -m stress`` on the ubuntu box.
  Parametrised over a couple of tiny durations so the real driver is genuinely
  exercised (no long-only path is dishonestly ``pragma``'d) and the window
  binning is proven at more than one point.
* :func:`test_soak_sustained` — ``@pytest.mark.soak``, honours the full
  ``STRESS_DURATION_S`` (hours), selected ONLY on the self-hosted
  ``soak-selfhosted.yml`` tier via ``-m 'stress or soak'``. Wires the
  previously-dead ``STRESS_DURATION_S`` knob (it existed in
  ``tests/stress/__init__.py`` but NO test read it) with a live ``windows >= 1``
  assertion.

Neither test is coverage-measured — ``[tool.coverage.run] source = ["brutex"]``
excludes ``tests/`` — so no wall-clock path rides a coverage obligation and the
drift LOGIC's 100% line+branch claim stands only on the pure module.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import pytest
from brutex.engine.sweep_cache import combo_digest
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.observability.metrics import MetricRegistry
from brutex.observability.process_probe import read_fd_count, read_rss_kib
from brutex.observability.sla import SLAEvent
from brutex.observability.soak_drift import (
    RULE_FD_GROWTH,
    RULE_P99_DRIFT,
    RULE_RSS_CEILING,
    SoakDriftMonitor,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain

# NOTE: NO module-level ``pytestmark = pytest.mark.stress`` here — that would tag
# the long ``test_soak_sustained`` with ``stress`` too, so the ubuntu nightly's
# ``-m stress`` would run the full-budget soak. The two entry points carry
# DISTINCT markers instead: ``test_soak_short`` is ``@stress`` (nightly),
# ``test_soak_sustained`` is ``@soak`` (self-hosted only). The self-hosted tier
# selects both via ``-m 'stress or soak'``.

#: Time slice per drift window (seconds). Each window collects per-op latencies,
#: then samples RSS/fd once at its boundary and evaluates the drift rules.
_WINDOW_SECONDS: float = 0.15

#: Ops per inner batch before the window-elapsed clock is re-checked (keeps the
#: perf_counter poll off the per-op hot loop).
_OPS_PER_BATCH: int = 200

#: The self-hosted sustained default when ``STRESS_DURATION_S`` is unset — a few
#: seconds, so a plain ``-m soak`` dev run is quick; the workflow overrides it.
_DEFAULT_SUSTAINED_S: float = 2.0

#: The p99-drift rule is fsync/GC/scheduler-jitter-bound on a sub-second window
#: (documented ~15% false-breach on an idle box, ratios up to 8-9x). Because that
#: noise OSCILLATES rather than climbing monotonically, the ``SLADetector``
#: hysteresis fires a FRESH OK->BREACH edge on every up-crossing — so a 6h soak
#: would emit thousands of edges. It is therefore excluded from BOTH the hard CI
#: gate (:func:`_assert_healthy`) AND the real-time CRITICAL push
#: (:func:`_drive._on_breach`) via ONE predicate below, so it can never storm the
#: operator into ignoring soak alerts (§11) — the exact anti-pattern that
#: motivated demoting it from the gate. The p99 signal stays visible via the
#: monitor's structlog breach warning + the ``brutex_soak_p99_ratio`` /metrics
#: gauge, just never as a phone push (DEC-SOAK-REAL-TIER-001). The deterministic
#: MONOTONE rules (RSS ceiling / fd growth) both hard-gate CI and fire the push:
#: a genuine leak crosses once, so ONE honest CRITICAL.
_NOISY_OBSERVE_ONLY_RULES: frozenset[str] = frozenset({RULE_P99_DRIFT})


def _fires_realtime_push(rule_name: str) -> bool:
    """A soak breach warrants a real-time CRITICAL push iff it is a deterministic rule.

    The SAME predicate gates the hard CI oracle in :func:`_assert_healthy`, so the
    push and the gate can never drift apart (COMMON, Golden Rule #11): the noisy,
    oscillating ``soak.p99_drift`` rule is observe-only on both; the monotone
    RSS-ceiling / fd-growth rules count on both.
    """
    return rule_name not in _NOISY_OBSERVE_ONLY_RULES


#: Fixed reproducibility stand-ins for the combo-dedup key (NOT real BLAKE3 run
#: identities — the sweep hot path shape is what the soak exercises, Golden
#: Rule #1). 64-hex like the memory-growth harness's run-id stand-ins.
_SNAP_HASH: str = "0" * 64
_INDICATOR_VERSION: str = "v-soak"
_CODE_COMMIT_SHA: str = "f" * 40


@dataclass(frozen=True, slots=True)
class _SoakResult:
    """The driver's outcome — window count, appended entries, drift edges, wall-clock."""

    windows: int
    appended: int
    breaches: tuple[str, ...]
    #: Measured wall-clock of the soak loop (perf_counter start→end). The tests
    #: assert ``elapsed_s >= duration_s`` — the oracle that ties the run's real
    #: length to the requested budget (SOAK-R8-3): a deadline-arithmetic
    #: regression in the loop, or an env-key drift that silently fell back to
    #: the 2 s default on a multi-hour tier, can no longer report a fake-green
    #: "multi-hour" soak that actually ran seconds.
    elapsed_s: float


def _run_soak(*, duration_s: float, monitor: SoakDriftMonitor, audit_path: Path) -> _SoakResult:
    """Drive the ingest + sweep hot loop for *duration_s*, feeding each window to *monitor*.

    Runs at least ONE window (the outer loop body always executes once), so even
    a sub-window duration yields a real observation. Returns the window count,
    the appended-entry count (for the chain-integrity check), every rule that
    edged into BREACH, and the measured wall-clock ``elapsed_s`` (which the loop
    guarantees is ``>= duration_s`` — the exit condition is
    ``perf_counter() >= deadline`` — so the callers can assert the run really
    lasted its budget, SOAK-R8-3).
    """
    breaches: list[str] = []
    appended = 0
    windows = 0
    with AuditWriter.open(audit_path) as log:
        start = perf_counter()
        deadline = start + duration_s
        while True:
            window_start = perf_counter()
            latencies: list[float] = []
            while perf_counter() - window_start < _WINDOW_SECONDS:
                for _ in range(_OPS_PER_BATCH):
                    t0 = perf_counter()
                    # INGEST hot path — one hash-chained append.
                    log.append(
                        action="SOAK_BURST",
                        subject=f"event:{appended % 1024:08d}",
                        payload={"seq": appended, "bucket": appended % 64},
                    )
                    appended += 1
                    # SWEEP hot path — one combo-dedup digest.
                    combo_digest(
                        mask=(1 << (appended % 60)),
                        instrument=("NIFTY", "BANKNIFTY", "SENSEX")[appended % 3],
                        tf=("1m", "5m", "15m")[appended % 3],
                        direction="bull" if appended % 2 == 0 else "bear",
                        data_snapshot_hash=_SNAP_HASH,
                        indicator_version=_INDICATOR_VERSION,
                        code_commit_sha=_CODE_COMMIT_SHA,
                    )
                    latencies.append(perf_counter() - t0)
            # Window boundary — sample the whole-process probes ONCE and evaluate
            # the drift rules. Kept OUT of the per-op latency region so the
            # sampling never pollutes the measured latencies.
            events = monitor.observe_window(
                rss_kib=read_rss_kib(),
                fd_count=read_fd_count(),
                latency_samples=latencies,
            )
            windows += 1
            breaches.extend(e.rule_name for e in events if e.new_state.value == "breach")
            if perf_counter() >= deadline:
                break
        elapsed_s = perf_counter() - start
    return _SoakResult(
        windows=windows, appended=appended, breaches=tuple(breaches), elapsed_s=elapsed_s
    )


def _drive(tmp_path: Path, duration_s: float) -> _SoakResult:
    """Build a monitor over a fresh registry and run the soak for *duration_s*."""
    registry = MetricRegistry()
    recorded: list[str] = []
    # Real production wiring of the advertised real-time drift alert
    # (DEC-PULL-HEALTH-WIRING-001 precedent): the default resolver returns the
    # process notifier singleton, which is disabled in CI/dev, so
    # ``soak_drift`` is a silent no-op there and only genuinely sends on a
    # Telegram-configured self-hosted soak — never a spurious push.
    alerter = SweepAlerter()

    def _on_breach(event: SLAEvent) -> None:
        recorded.append(event.rule_name)
        # Best-effort CRITICAL push the INSTANT a DETERMINISTIC rule edges into
        # breach. The noisy ``soak.p99_drift`` rule is EXCLUDED (``_fires_realtime_push``):
        # its sub-second-window p99 oscillates, so the hysteresis fires a fresh
        # OK->BREACH edge per up-crossing and pushing each would storm the operator
        # on non-regression noise (DEC-SOAK-REAL-TIER-001, §11) — the same reason
        # ``_assert_healthy`` excludes it from the hard gate. p99 stays visible via
        # the monitor's structlog warning + the ``brutex_soak_p99_ratio`` gauge.
        if not _fires_realtime_push(event.rule_name):
            return
        alerter.soak_drift(
            rule=event.rule_name,
            observed=event.observed_value,
            threshold=event.threshold,
            detail="soak drift breach — check RSS / fd over the run",
        )

    def _on_recovery(_event: SLAEvent) -> None:
        return None

    monitor = SoakDriftMonitor(
        registry=registry,
        on_breach=_on_breach,
        on_recovery=_on_recovery,
    )
    result = _run_soak(
        duration_s=duration_s,
        monitor=monitor,
        audit_path=tmp_path / "soak_audit.jsonl",
    )
    # The observed breach edges and the returned events must agree (``recorded``
    # is appended for EVERY monitor edge, before the push-vs-observe-only
    # decision, so it mirrors the full drift signal — not only the pushed subset).
    assert list(result.breaches) == recorded
    return result


def _assert_healthy(result: _SoakResult, audit_path: Path) -> None:
    """Assert the run produced windows, leaked nothing, and kept the chain intact.

    Only the DETERMINISTIC drift oracles hard-gate CI: the RSS ceiling and the
    fd-growth count (plus the audit-chain integrity below). ``soak.p99_drift`` is
    observe-only — a sub-second window's p99 is fsync/GC/scheduler-jitter-bound, so
    asserting on it false-fails the nightly cron (~15% locally) and spams CRITICAL
    issues for a non-regression (DEC-SOAK-REAL-TIER-001); it is filtered out of the
    hard gate here via the SAME ``_fires_realtime_push`` predicate that filters it
    out of the real-time push, so the gate and the push can never drift apart.
    """
    assert result.windows >= 1, "the soak must produce at least one drift window"
    # The hard gate and the real-time push share ONE predicate: p99 is
    # observe-only on both (a noise-driven up-crossing must not red-fail CI nor
    # push a CRITICAL); the monotone RSS/fd rules count on both.
    hard_breaches = tuple(rule for rule in result.breaches if _fires_realtime_push(rule))
    assert hard_breaches == (), (
        f"soak drift breach(es) detected: {hard_breaches} — a memory/fd "
        f"regression over {result.windows} windows (DEC-SOAK-REAL-TIER-001)"
    )
    verified = verify_chain(audit_path)
    assert verified == result.appended, (
        f"audit chain verified {verified} entries, expected {result.appended} — "
        f"chain drift / entry loss under sustained load"
    )


def _sustained_duration_s() -> float:
    """Resolve the sustained budget from ``STRESS_DURATION_S``, refusing non-finite values.

    A bare ``float()`` of the env knob admits ``"nan"`` / ``"inf"`` — and the
    driver's ONLY exit is ``perf_counter() >= deadline``, which is ALWAYS False
    against a NaN deadline (every comparison with NaN is False) and never True
    before an infinite one, so the loop runs FOREVER until the job timeout
    kills it (SOAK-R8-4, reproduced live). Refuse loudly instead — the same
    fail-loud contract the workflow's ``plan`` fence applies to
    ``soak_minutes`` (a non-numeric string already raises ``ValueError`` from
    ``float()`` itself).
    """
    raw = os.environ.get("STRESS_DURATION_S", str(_DEFAULT_SUSTAINED_S))
    duration_s = float(raw)
    if not math.isfinite(duration_s):
        msg = (
            f"STRESS_DURATION_S={raw!r} must be finite — a NaN/inf deadline never "
            f"expires and the soak loop would run until the job timeout kills it "
            f"(SOAK-R8-4)"
        )
        raise ValueError(msg)
    return duration_s


@pytest.mark.stress
@pytest.mark.parametrize("duration_s", [0.4, 0.8])
def test_soak_short(tmp_path: Path, duration_s: float) -> None:
    """The real soak driver, run briefly — the honest nightly regression tier.

    Fixed short budget (never reads ``STRESS_DURATION_S``), so the ubuntu
    ``-m stress`` nightly runs it in seconds while still exercising the SAME
    driver + drift detector the multi-hour tier uses.
    """
    result = _drive(tmp_path, duration_s)
    # The run must really last its budget (SOAK-R8-3) — pins the driver's
    # deadline arithmetic on the nightly tier, at more than one duration.
    assert result.elapsed_s >= duration_s
    _assert_healthy(result, tmp_path / "soak_audit.jsonl")


@pytest.mark.soak
def test_soak_sustained(tmp_path: Path) -> None:
    """The full-budget soak — honours ``STRESS_DURATION_S`` (hours on the tier).

    Selected ONLY by ``-m 'stress or soak'`` on the self-hosted
    ``soak-selfhosted.yml`` job; a plain dev run defaults to a couple of seconds.
    This is the live wiring of the previously-dead ``STRESS_DURATION_S`` knob.
    """
    duration_s = _sustained_duration_s()
    result = _drive(tmp_path, duration_s)
    # The oracle that ties the run's REAL length to the requested budget
    # (SOAK-R8-3): a deadline-arithmetic regression in ``_run_soak`` — the
    # exact fake-green "~1 s multi-hour soak" shape SEC7-2 fenced on the PLAN
    # side — now fails HERE too, instead of relying on someone eyeballing a
    # suspiciously fast job (§11). (An env-KEY drift falling back to the 2 s
    # default would still satisfy this assert against its own fallback value;
    # the workflow-side zero/garbage fences plus the plan-log line remain that
    # half's guard — honestly NOT claimed covered here.)
    assert result.elapsed_s >= duration_s
    _assert_healthy(result, tmp_path / "soak_audit.jsonl")


@pytest.mark.stress
def test_sustained_duration_rejects_non_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    """``STRESS_DURATION_S=nan``/``inf`` must fail LOUDLY, never loop forever.

    Regression pin for SOAK-R8-4: ``float("nan")`` parses cleanly, and
    ``perf_counter() >= nan`` is always False, so an unfenced NaN duration hung
    the driver until the job timeout killed it (verified live). ``inf`` is the
    same hang via a never-reached deadline; ``-inf`` is fenced for symmetry.
    """
    for bad in ("nan", "inf", "-inf", "NaN", "Infinity"):
        monkeypatch.setenv("STRESS_DURATION_S", bad)
        with pytest.raises(ValueError, match="must be finite"):
            _sustained_duration_s()
    # A garbage string stays loud too (float() itself raises).
    monkeypatch.setenv("STRESS_DURATION_S", "six hours")
    with pytest.raises(ValueError):
        _sustained_duration_s()
    # The happy paths are unchanged: explicit value and the unset default.
    monkeypatch.setenv("STRESS_DURATION_S", "21600")
    assert _sustained_duration_s() == 21600.0
    monkeypatch.delenv("STRESS_DURATION_S")
    assert _sustained_duration_s() == _DEFAULT_SUSTAINED_S


@pytest.mark.stress
def test_p99_drift_is_observe_only_not_a_realtime_push() -> None:
    """The noisy ``soak.p99_drift`` rule fires NEITHER the hard gate NOR the push.

    Regression pin for the storm-anti-pattern (DEC-SOAK-REAL-TIER-001): a
    sub-second p99 oscillates, so the hysteresis emits an OK→BREACH edge per
    up-crossing; routing each to the CRITICAL ``soak_drift`` push would flood the
    operator on the very noise that got p99 demoted from the CI gate. The ONE
    shared :func:`_fires_realtime_push` predicate keeps p99 observe-only on BOTH
    the gate and the push, while the deterministic monotone rules fire on both.
    """
    assert not _fires_realtime_push(RULE_P99_DRIFT)
    assert _fires_realtime_push(RULE_RSS_CEILING)
    assert _fires_realtime_push(RULE_FD_GROWTH)
