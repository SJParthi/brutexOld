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
    _PHASE_NOTES,
    GENERIC_GATE_NOTE,
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


# ── gated_movers_payload ──────────────────────────────────────────────────


def _blocking_snapshot() -> GateSnapshot:
    return GateSnapshot(state=STATE_BLOCKING, runs=3, last_ok=None)


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
    assert payload["interval"] == "5minute"
    assert payload["date"] == "2024-06-13"
    assert payload["time"] == "09:20"
    assert payload["top_n"] == 100
    assert payload["instrument_count"] == 0
    assert payload["skipped_count"] == 0
    assert "prebuild in progress — movers available when finished" in payload["note"]
    # No phase signal ⇒ the honest phase echo is the cleared label.
    assert payload["phase"] == ""
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


# ── phase-aware gated notes (DEC-PIPELINE-PHASE-UI-HONESTY-001) ──────────


#: The pipeline's REAL coarse ``set_phase`` labels — verified exhaustive over every
#: ``set_phase(`` call site in ``brutex/dashboard/app.py`` (spot :2963 · metadata
#: :2987 · historical :3012 · movers :3041 · prebuild :3238 on the audited baseline);
#: there is NO "greeks" label (the Greeks enrichment rides INSIDE "historical").
_REAL_PIPELINE_PHASES = ("spot", "metadata", "historical", "movers", "prebuild")


def _gated(phase: str | None) -> dict[str, object]:
    return gated_movers_payload(
        _blocking_snapshot(),
        asset="spot",
        interval="1day",
        date="2024-06-13",
        time=None,
        top_n=10,
        phase=phase,
    )


def test_phase_notes_cover_exactly_the_real_pipeline_phases() -> None:
    # The map covers EXACTLY the real set_phase labels — no fabricated "greeks"
    # phase, no missing phase (a new pipeline phase must consciously extend it).
    assert set(_PHASE_NOTES) == set(_REAL_PIPELINE_PHASES)


def test_gated_payload_phase_notes_name_the_running_phase() -> None:
    # Regression (fails before the fix): the note used to be ONE hardcoded
    # "prebuild in progress" string for the WHOLE pipeline — an over-claim during
    # the spot/metadata/historical phases. Each known phase now gets an honest
    # note that names it, echoed alongside the phase itself.
    for phase in _REAL_PIPELINE_PHASES:
        payload = _gated(phase)
        assert payload["phase"] == phase
        note = payload["note"]
        assert isinstance(note, str)
        assert note == _PHASE_NOTES[phase]
        assert phase in note  # the note NAMES what is actually running.
        assert note != GENERIC_GATE_NOTE
    # The fetch phases must no longer claim the prebuild is what's running.
    for phase in ("spot", "metadata", "historical"):
        assert "prebuild in progress" not in str(_gated(phase)["note"])


def test_gated_payload_unknown_or_absent_phase_keeps_generic_note_byte_identical() -> None:
    # The honest fallback (Golden Rule #1 — never a fabricated phase claim): no
    # signal (None), a cleared label (""), or an unknown future label all fall
    # back to the BYTE-IDENTICAL pre-phase-aware generic wording.
    expected = (
        "prebuild in progress — movers available when finished: the pull pipeline is "
        "writing the derived timeframe partitions the movers read, so this screen is "
        "gated until that final stage completes (it retries automatically; no action "
        "needed)."
    )
    assert expected == GENERIC_GATE_NOTE
    assert _gated(None)["note"] == expected
    assert _gated(None)["phase"] == ""
    assert _gated("")["note"] == expected
    assert _gated("unknown-future-phase")["note"] == expected
    assert _gated("unknown-future-phase")["phase"] == "unknown-future-phase"
    # Default (phase omitted) is byte-compatible with the pre-phase payload note.
    default_payload = gated_movers_payload(
        _blocking_snapshot(), asset="spot", interval="1day", date="2024-06-13", time=None, top_n=10
    )
    assert default_payload["note"] == expected
    assert default_payload["phase"] == ""


def test_gated_payload_logs_the_phase() -> None:
    with capture_logs() as logs:
        _gated("historical")
    events = [entry for entry in logs if entry["event"] == "movers.gate.blocked"]
    assert len(events) == 1
    assert events[0]["phase"] == "historical"


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
    # The dynamic server note is set via textContent ONLY (XSS guard). The static
    # fallback is phase-neutral (DEC-PIPELINE-PHASE-UI-HONESTY-001 — it fires only
    # when the server note is falsy, so it must not over-claim a prebuild phase).
    assert 'textContent = j.note || "pull pipeline in progress' in template
    # Auto-recovery: a retry timer re-polls until the gate lifts…
    assert "gateTimer = setTimeout(load, GATE_RETRY_MS)" in template
    # …and every non-gated load clears it.
    assert template.count("clearTimeout(gateTimer)") >= 2
    # The gated shell must never be cached as a re-renderable payload.
    assert "lastPayload = null;  /* a pill switch must re-fetch" in template
