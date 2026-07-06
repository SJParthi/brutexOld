"""Unit tests for the movers **prebuild gate** (DEC-PIPELINE-AUTOPREBUILD-001, chunk 2).

The pure :mod:`brutex.dashboard.prebuild_gate` state holder + the honest gated
``/api/v1/movers`` payload builder: begin → BLOCKING, release → IDLE (safe
without begin — a failed pull can never wedge the movers shut), the
monotonically-increasing run counter, the ``last_ok`` provenance, a
thread-safety smoke, the payload shape / note / logs, and the
malformed-date → ``ValueError`` (route-400) contract. No I/O anywhere.
"""

from __future__ import annotations

import threading

import pytest
from brutex.dashboard.prebuild_gate import (
    STATE_BLOCKING,
    STATE_IDLE,
    GateSnapshot,
    PrebuildGate,
    gated_movers_payload,
)
from structlog.testing import capture_logs

# ── PrebuildGate state machine ────────────────────────────────────────────


def test_gate_starts_idle() -> None:
    snap = PrebuildGate().snapshot()
    assert snap.state == STATE_IDLE
    assert snap.blocking is False
    assert snap.runs == 0
    assert snap.last_ok is None  # honest: no release has ever happened.


def test_gate_begin_blocks() -> None:
    gate = PrebuildGate()
    gate.begin()
    snap = gate.snapshot()
    assert snap.state == STATE_BLOCKING
    assert snap.blocking is True
    assert snap.runs == 1


def test_gate_release_returns_to_idle_and_records_ok() -> None:
    gate = PrebuildGate()
    gate.begin()
    gate.release(True)
    snap = gate.snapshot()
    assert snap.state == STATE_IDLE
    assert snap.blocking is False
    assert snap.last_ok is True
    # A later failed run overwrites the provenance honestly.
    gate.begin()
    gate.release(False)
    assert gate.snapshot().last_ok is False


def test_gate_double_begin_stays_blocking_and_counts_runs() -> None:
    # Defensive: the single-flight job makes a double begin unreachable in
    # production, but it must never raise — it stays blocking + counts.
    gate = PrebuildGate()
    gate.begin()
    gate.begin()
    snap = gate.snapshot()
    assert snap.blocking is True
    assert snap.runs == 2


def test_gate_release_without_begin_is_safe_no_crash() -> None:
    # The binding site's try/finally calls release unconditionally — a release
    # with no prior begin is a harmless IDLE transition, never a crash.
    gate = PrebuildGate()
    gate.release(False)
    snap = gate.snapshot()
    assert snap.state == STATE_IDLE
    assert snap.runs == 0
    assert snap.last_ok is False


def test_gate_release_logs_gate_released_with_ok() -> None:
    gate = PrebuildGate()
    gate.begin()
    with capture_logs() as logs:
        gate.release(True)
    events = [entry for entry in logs if entry["event"] == "pipeline.prebuild.gate_released"]
    assert len(events) == 1
    assert events[0]["ok"] is True
    assert events[0]["was_blocking"] is True


def test_gate_thread_safety_smoke() -> None:
    # 16 threads hammer begin/release/snapshot concurrently: no exception, the
    # run counter equals the total begin() calls (no lost update), and a final
    # release leaves the gate idle.
    gate = PrebuildGate()
    begins_per_thread = 50
    errors: list[Exception] = []

    def _hammer() -> None:
        try:
            for _ in range(begins_per_thread):
                gate.begin()
                assert gate.snapshot().runs >= 1
                gate.release(True)
        except Exception as exc:  # collect for the main-thread assert below
            errors.append(exc)

    threads = [threading.Thread(target=_hammer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    snap = gate.snapshot()
    assert snap.runs == 16 * begins_per_thread
    assert snap.blocking is False  # every begin was paired with a release.


# ── started_at / gated_for_s stamps (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) ──


class _TickClock:
    """A manually-advanced clock for deterministic duration tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_gate_idle_has_no_start_stamps() -> None:
    snap = PrebuildGate().snapshot()
    assert snap.started_at is None
    assert snap.started_at_epoch is None
    assert snap.gated_for_s is None


def test_gate_begin_stamps_epoch_and_monotonic_duration() -> None:
    # started_at_epoch = the injected WALL clock (epoch — renderable
    # client-side; the ISO `started_at` twin is derived from the SAME instant);
    # gated_for_s = the injected MONOTONIC delta (immune to wall-clock jumps).
    mono = _TickClock(start=100.0)
    wall = _TickClock(start=1_750_000_000.0)
    gate = PrebuildGate(clock=mono, wall_clock=wall)
    gate.begin()
    mono.now += 34.5
    snap = gate.snapshot()
    assert snap.blocking is True
    assert snap.started_at_epoch == 1_750_000_000.0
    assert snap.started_at == "2025-06-15T15:06:40+00:00"  # the SAME instant, ISO UTC
    assert snap.gated_for_s == 34.5


def test_gate_release_clears_start_stamps() -> None:
    mono = _TickClock()
    gate = PrebuildGate(clock=mono, wall_clock=_TickClock(start=5.0))
    gate.begin()
    gate.release(True)
    snap = gate.snapshot()
    assert snap.started_at is None
    assert snap.started_at_epoch is None
    assert snap.gated_for_s is None


def test_gate_release_without_begin_keeps_stamps_none() -> None:
    # The narrowed scope (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) makes the
    # release-without-begin path the NORMAL one for every pipeline that stops
    # before its prebuild — it must stay a safe no-op with no stamps.
    gate = PrebuildGate(clock=_TickClock(), wall_clock=_TickClock())
    gate.release(False)
    snap = gate.snapshot()
    assert snap.started_at is None
    assert snap.started_at_epoch is None
    assert snap.gated_for_s is None
    assert snap.blocking is False


def test_gate_double_begin_restamps_to_newest() -> None:
    mono = _TickClock(start=10.0)
    wall = _TickClock(start=1000.0)
    gate = PrebuildGate(clock=mono, wall_clock=wall)
    gate.begin()
    mono.now, wall.now = 50.0, 1040.0
    gate.begin()  # defensive double begin re-stamps to the newer start.
    mono.now = 51.0
    snap = gate.snapshot()
    assert snap.started_at_epoch == 1040.0
    assert snap.gated_for_s == 1.0


def test_gate_duration_clamped_nonnegative_on_clock_skew() -> None:
    # A (test-injected) backwards monotonic clock never yields a negative duration.
    mono = _TickClock(start=100.0)
    gate = PrebuildGate(clock=mono, wall_clock=_TickClock())
    gate.begin()
    mono.now = 99.0
    snap = gate.snapshot()
    assert snap.gated_for_s == 0.0


# ── gated_movers_payload ──────────────────────────────────────────────────


def _blocking_snapshot() -> GateSnapshot:
    # Deliberately PHASE-LESS (the defensive shape): the dynamic-phase echo is
    # pinned by the phase-carrying tests below (DEC-PREBUILD-GATE-HONEST-NOTE-001
    # reconciled the static `phase: "prebuild"` of the original
    # DEC-MOVERS-GATE-PREBUILD-SCOPE-001 payload into the dynamic snapshot echo).
    return GateSnapshot(
        state=STATE_BLOCKING,
        runs=3,
        last_ok=None,
        started_at_epoch=1_750_000_000.0,
        gated_for_s=12.25,
    )


def test_gated_payload_shape_is_honest_and_empty() -> None:
    payload = gated_movers_payload(
        _blocking_snapshot(),
        asset="spot",
        interval="5minute",
        date="2024-06-13",
        time="09:20",
        top_n=100,
    )
    assert payload["gated"] is True
    # The narrowed-scope honesty fields (DEC-MOVERS-GATE-PREBUILD-SCOPE-001):
    # the gate's start/duration stamps ride the payload; the phase echo is
    # DYNAMIC (snapshot-fed — None here, the phase-less defensive shape).
    assert payload["phase"] is None
    assert payload["started_at_epoch"] == 1_750_000_000.0
    assert payload["gated_for_s"] == 12.25
    assert payload["interval"] == "5minute"
    assert payload["date"] == "2024-06-13"
    assert payload["time"] == "09:20"
    assert payload["top_n"] == 100
    assert payload["instrument_count"] == 0
    assert payload["skipped_count"] == 0
    # DEC-PREBUILD-GATE-HONEST-NOTE-001 + DEC-MOVERS-WARM-UNBLOCK-001: the note
    # states the REAL release condition (the gate lifts at prebuild completion;
    # the movers warm continues un-gated in the background), phase-aware when known.
    assert "movers stay gated until the final prebuild finishes" in payload["note"]
    assert "movers warm then continues un-gated in the background" in payload["note"]
    # ZERO ranked category lists — the page renders the note, not fabricated rows.
    assert "price_gainers" not in payload
    # The raw ?asset= is LOG-only — never echoed into the payload (no reflected sink).
    assert "asset" not in payload


def test_gated_payload_blank_date_defaults_to_today_pure() -> None:
    # date=None ⇒ today's ISO date via the PURE parse — never a lake read.
    payload = gated_movers_payload(
        _blocking_snapshot(), asset="options", interval="1day", date=None, time=None, top_n=50
    )
    assert payload["gated"] is True
    assert len(payload["date"]) == len("2026-07-03")


def test_gated_payload_malformed_date_raises_valueerror() -> None:
    # The route maps ValueError → 400; the gated path keeps that contract.
    with pytest.raises(ValueError, match="malformed date"):
        gated_movers_payload(
            _blocking_snapshot(),
            asset="spot",
            interval="1day",
            date="not-a-date",
            time=None,
            top_n=10,
        )


def test_gated_payload_logs_movers_gate_blocked() -> None:
    with capture_logs() as logs:
        gated_movers_payload(
            _blocking_snapshot(),
            asset="futures",
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
        )
    events = [entry for entry in logs if entry["event"] == "movers.gate.blocked"]
    assert len(events) == 1
    assert events[0]["state"] == STATE_BLOCKING
    assert events[0]["runs"] == 3
    assert events[0]["asset"] == "futures"


def test_gate_begin_logs_movers_gate_begin() -> None:
    gate = PrebuildGate()
    with capture_logs() as logs:
        gate.begin()
    events = [entry for entry in logs if entry["event"] == "movers.gate.begin"]
    assert len(events) == 1
    assert events[0]["runs"] == 1


# ── the movers.html gated-note rendering hook (string-presence pins) ─────


def test_movers_template_renders_gated_note_and_auto_recovers() -> None:
    """The page handles `gated: true`: prominent note via textContent (never
    innerHTML for the dynamic string — the CodeQL XSS-guard convention) + an
    auto-retry timer so the screen recovers by itself when the gate lifts."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates" / "movers.html"
    ).read_text(encoding="utf-8")
    assert "j.gated" in template
    # The dynamic server note is set via textContent ONLY (XSS guard).
    assert 'textContent = j.note || "pull pipeline running' in template
    # Narrowed-scope honesty (DEC-MOVERS-GATE-PREBUILD-SCOPE-001): the shell
    # names the phase + shows the gate duration, via textContent only.
    assert 'j.phase||"prebuild"' in template
    assert "j.gated_for_s" in template
    assert 'gated (${j.phase||"prebuild"}${gatedFor})`' in template
    # Auto-recovery: a retry timer re-polls until the gate lifts…
    assert "gateTimer = setTimeout(load, GATE_RETRY_MS)" in template
    # …and every non-gated load clears it.
    assert template.count("clearTimeout(gateTimer)") >= 2
    # The gated shell must never be cached as a re-renderable payload.
    assert "lastPayload = null;  /* a pill switch must re-fetch" in template


# ── honest gated status: phase + elapsed + warm progress ──────────────────
# (DEC-PREBUILD-GATE-HONEST-NOTE-001 / DEC-MOVERS-WARM-PROGRESS-001)


def test_begin_opens_at_phase_pull_with_elapsed_clock() -> None:
    gate = PrebuildGate()
    gate.begin()
    snap = gate.snapshot()
    assert snap.blocking is True
    assert snap.phase == "pull"
    assert snap.started_at is not None
    assert snap.elapsed_s is not None and snap.elapsed_s >= 0.0
    assert snap.progress_done is None and snap.progress_total is None


def test_set_phase_annotates_and_resets_progress() -> None:
    gate = PrebuildGate()
    gate.begin()
    gate.set_phase("movers")
    gate.set_progress(5, 27)
    snap = gate.snapshot()
    assert snap.phase == "movers"
    assert (snap.progress_done, snap.progress_total) == (5, 27)
    # A new phase starts with CLEAN progress (never a stale i/N carry-over).
    gate.set_phase("prebuild")
    snap = gate.snapshot()
    assert snap.phase == "prebuild"
    assert snap.progress_done is None and snap.progress_total is None


def test_set_progress_carries_optional_segment_label() -> None:
    """DEC-MOVERS-WARM-UNBLOCK-001 fix 4: the per-segment i/N names its segment."""
    gate = PrebuildGate()
    gate.begin()
    gate.set_phase("movers")
    gate.set_progress(6, 9, segment="futures")
    snap = gate.snapshot()
    assert (snap.progress_done, snap.progress_total) == (6, 9)
    assert snap.progress_segment == "futures"
    # An unlabelled reporter (back-compat positional call) reads None — honest.
    gate.set_progress(7, 9)
    assert gate.snapshot().progress_segment is None
    # A new phase resets the labelled progress too (never a stale carry-over).
    gate.set_phase("prebuild")
    snap = gate.snapshot()
    assert snap.progress_segment is None
    assert snap.progress_done is None


def test_release_clears_phase_elapsed_and_progress() -> None:
    gate = PrebuildGate()
    gate.begin()
    gate.set_phase("historical")
    gate.set_progress(1, 3, segment="spot")
    gate.release(True)
    snap = gate.snapshot()
    assert snap.blocking is False
    assert snap.phase is None
    assert snap.started_at is None
    assert snap.elapsed_s is None
    assert snap.progress_done is None and snap.progress_total is None
    assert snap.progress_segment is None


def test_set_phase_on_idle_gate_is_invisible() -> None:
    # A dry-run pipeline never begins the gate; the phase annotation still
    # lands (harmless) but the snapshot exposes NOTHING while idle.
    gate = PrebuildGate()
    gate.set_phase("spot")
    gate.set_progress(1, 2, segment="spot")
    snap = gate.snapshot()
    assert snap.blocking is False
    assert snap.phase is None
    assert snap.elapsed_s is None
    assert snap.progress_done is None and snap.progress_total is None
    assert snap.progress_segment is None


def test_gated_payload_carries_phase_elapsed_and_warm_progress() -> None:
    snap = GateSnapshot(
        state=STATE_BLOCKING,
        runs=1,
        last_ok=None,
        phase="movers",
        started_at="2026-07-04T14:00:00+00:00",
        elapsed_s=4530.5,
        progress_done=5,
        progress_total=27,
        progress_segment="options",
    )
    payload = gated_movers_payload(
        snap, asset="spot", interval="1day", date="2024-06-13", time=None, top_n=100
    )
    assert payload["phase"] == "movers"
    assert payload["started_at"] == "2026-07-04T14:00:00+00:00"
    assert payload["elapsed_s"] == 4530.5
    assert payload["warm_done"] == 5
    assert payload["warm_total"] == 27
    # DEC-MOVERS-WARM-UNBLOCK-001: the SEGMENT name rides the payload so the
    # frontend renders "options 5/27", never a bare "5/27" misread as total.
    assert payload["warm_segment"] == "options"
    assert "(current stage: movers)" in payload["note"]
    # The ticking elapsed NEVER rides the note (the paint-once shell key would
    # otherwise repaint every 5 s poll — DEC-DASHBOARD-JS-PERF-001).
    assert "4530" not in payload["note"]


def test_gated_payload_without_phase_omits_the_stage_clause() -> None:
    payload = gated_movers_payload(
        _blocking_snapshot(),
        asset="spot",
        interval="1day",
        date="2024-06-13",
        time=None,
        top_n=100,
    )
    assert payload["phase"] is None
    assert payload["elapsed_s"] is None
    assert payload["warm_done"] is None and payload["warm_total"] is None
    assert payload["warm_segment"] is None
    assert "current stage" not in payload["note"]
    assert "movers stay gated until the final prebuild finishes" in payload["note"]


def test_live_gate_snapshot_feeds_phase_into_the_payload() -> None:
    # End-to-end through the REAL gate: begin → set_phase → the payload names it.
    gate = PrebuildGate()
    gate.begin()
    gate.set_phase("prebuild")
    payload = gated_movers_payload(
        gate.snapshot(), asset="futures", interval="1day", date=None, time=None, top_n=50
    )
    assert payload["phase"] == "prebuild"
    assert "(current stage: prebuild)" in payload["note"]
    assert payload["elapsed_s"] is not None and payload["elapsed_s"] >= 0.0
