"""DEC-PULL-PROGRESS-HONESTY-001 — ETA / live-bars / open-ended honesty on PullProgress.

Fails-before framing: on the pre-change tracker (a) ``ProgressSnapshot`` carried NO
``started_at`` / rate / ETA at all, (b) ``advance(grow_total=True)`` pinned
``total == completed`` so ``percent`` read a fabricated 100 while windows were still
pending, (c) there was no way to fold a live bars delta without also advancing a unit,
and (d) nothing marked a growing-denominator phase, so the frontend could not tell an
honest partial from a pinned-complete. Every test here exercises the NEW honest
surface; the pinned invariants (ETA ``None`` under grow_total; ``open_ended``
persisting across ``set_phase("prebuild")`` / ``set_phase("movers")``) are the
mandatory refuter amendments.
"""

from __future__ import annotations

from collections.abc import Callable

from brutex.dashboard.pull_progress import (
    STATE_RUNNING,
    ProgressSnapshot,
    PullProgress,
)


def _ticking_clock(step: float = 1.0) -> Callable[[], float]:
    """Deterministic monotonically-increasing injected clock (team playbook shape)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += step
        return state["t"]

    return clock


# ── ETA fields + the linear extrapolation (item 1) ──────────────────────────────


def test_started_at_stamped_on_start() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=4)
    snap = p.snapshot()
    assert snap.started_at > 0.0
    assert snap.phase_started_at == snap.started_at


def test_elapsed_seconds_tracks_the_run_and_clamps_negative() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance()
    assert p.snapshot().elapsed_seconds == 1.0  # start@1 → advance@2
    # A never-started snapshot (updated_at == started_at == 0) clamps to 0, and a
    # hand-built out-of-order snapshot can never read negative.
    assert PullProgress(clock=_ticking_clock()).snapshot().elapsed_seconds == 0.0
    skewed = ProgressSnapshot(
        state=STATE_RUNNING,
        total=1,
        completed=0,
        failed=0,
        bars=0,
        current="",
        detail="",
        updated_at=1.0,
        started_at=5.0,
    )
    assert skewed.elapsed_seconds == 0.0


def test_eta_none_when_idle() -> None:
    # Never started → no rate, not running → an honest None (never a fake 0).
    snap = PullProgress(clock=_ticking_clock()).snapshot()
    assert snap.eta_seconds is None
    assert snap.units_per_sec == 0.0


def test_eta_linear_extrapolation_from_phase_rate() -> None:
    # clock ticks 1s per call: start@1; advance@2 stamps the baseline (base=1, t=2);
    # advance@3, advance@4 → 2 units over 2s = 1 unit/s; remaining 7 of 10 → ETA 7s.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=10)
    p.advance()  # baseline stamp — contributes NO measured rate (honest)
    p.advance()
    p.advance()
    snap = p.snapshot()
    assert snap.completed == 3
    assert snap.units_per_sec == 1.0
    assert snap.eta_seconds == 7.0


def test_eta_zero_when_nothing_remains() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance()
    p.advance()
    snap = p.snapshot()
    assert snap.completed == snap.total == 2
    assert snap.units_per_sec > 0.0
    assert snap.eta_seconds == 0.0


def test_eta_none_before_a_rate_is_measured() -> None:
    # Exactly ONE mutation after start: it only stamps the baseline → rate 0 → None.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=5)
    p.advance()
    assert p.snapshot().units_per_sec == 0.0
    assert p.snapshot().eta_seconds is None


def test_eta_none_when_total_unknown() -> None:
    # total == 0 (started with nothing planned) → no denominator → None.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=0)
    assert p.snapshot().eta_seconds is None


def test_eta_none_once_settled() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance()
    p.advance()
    p.finish()
    assert p.snapshot().eta_seconds is None  # not running — never a stale ETA


def test_sync_jump_never_fabricates_a_burst_rate() -> None:
    # The spot phase's FIRST ledger sync jumps to cross-run absolute counts
    # (e.g. 3000 previously settled windows). The baseline stamps AT that first
    # sync, so the jump contributes ZERO measured rate — the second sync's delta
    # is the only rate source. Fails-before: a naive started_at-anchored rate
    # would read 3000 units in one tick → an absurd ETA of milliseconds.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=4000)
    p.set_phase("spot")
    p.sync(completed=3000, total=4000, bars=0)  # baseline: base=3000
    snap = p.snapshot()
    assert snap.units_per_sec == 0.0 and snap.eta_seconds is None
    p.sync(completed=3010, total=4000, bars=0)  # +10 units over 1s
    snap = p.snapshot()
    assert snap.units_per_sec == 10.0
    assert snap.eta_seconds == (4000 - 3010) / 10.0


def test_set_phase_rebaselines_the_rate() -> None:
    # Phase 1 runs at 1 unit/s; the phase switch re-arms the baseline so phase 2's
    # rate starts fresh instead of inheriting phase 1's history.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=100)
    p.advance()
    p.advance()
    assert p.snapshot().units_per_sec == 1.0
    p.set_phase("metadata")
    p.sync(completed=50, total=100, bars=None)  # phase-2 baseline: base=50
    snap = p.snapshot()
    assert snap.phase_base_completed == 50
    assert snap.units_per_sec == 0.0  # no phase-2 delta measured yet


def test_negative_progress_after_rebaseline_reads_zero_rate() -> None:
    # A sync BELOW the stamped baseline (a later phase resets to smaller absolute
    # counts without a set_phase) must clamp to rate 0 / ETA None — never negative.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=10)
    p.sync(completed=8, total=10, bars=0)  # baseline base=8
    p.sync(completed=2, total=10, bars=0)  # goes BACKWARD
    snap = p.snapshot()
    assert snap.units_per_sec == 0.0
    assert snap.eta_seconds is None


def test_to_dict_carries_the_additive_keys_only() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=3)
    d = p.snapshot().to_dict()
    for key in ("started_at", "units_per_sec", "eta_seconds", "open_ended"):
        assert key in d
    # Every pre-existing flat key still ships (the _FLAT_KEYS additive contract).
    for key in (
        "state",
        "total",
        "completed",
        "failed",
        "gaps",
        "bars",
        "current",
        "detail",
        "percent",
        "running",
        "updated_at",
        "phase",
        "stalled",
        "stall_detail",
    ):
        assert key in d


# ── The MANDATORY refuter amendment: no ETA over a growing denominator ──────────


def test_eta_is_none_while_grow_total_runs() -> None:
    # Fails-before (the refuter amendment): a grow_total phase advances
    # completed == total, so a naive linear ETA would read a confident 0.0 the
    # entire time — an illusion. The snapshot must read open_ended + ETA None.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=5)
    for _ in range(4):
        p.advance(current="pair", bars=10, grow_total=True)
    snap = p.snapshot()
    assert snap.open_ended is True
    assert snap.eta_seconds is None
    # The measured rate itself still exists (units are really completing) — only
    # the ETA verdict is withheld.
    assert snap.units_per_sec > 0.0


# ── open_ended: set / sticky / cleared (item 3) ─────────────────────────────────


def test_set_phase_open_ended_marks_the_snapshot() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=45)
    assert p.snapshot().open_ended is False
    p.set_phase("historical", open_ended=True)
    snap = p.snapshot()
    assert snap.open_ended is True
    assert snap.phase == "historical"


def test_open_ended_persists_across_prebuild_and_movers_phases() -> None:
    # The PINNED invariant: once the F&O phase declared the total non-final, the
    # later pipeline phases must NOT silently restore a "known total" display.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=45)
    p.set_phase("historical", open_ended=True)
    p.set_phase("prebuild")
    assert p.snapshot().open_ended is True
    p.set_phase("movers")
    assert p.snapshot().open_ended is True
    assert p.snapshot().eta_seconds is None  # still no ETA over the unknown total


def test_open_ended_survives_finish_and_clears_only_on_start() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=45)
    p.set_phase("historical", open_ended=True)
    p.finish(detail="done")
    assert p.snapshot().open_ended is True  # the settled display stays honest
    p.start(total=3)  # a fresh run — the ONLY clear
    assert p.snapshot().open_ended is False


def test_percent_is_honest_with_ledger_driven_open_ended_counts() -> None:
    # Fails-before (the old illusion): the F&O phase's grow_total advance pinned
    # total == completed → percent 100.0 while thousands of windows were pending.
    # The NEW path syncs ABSOLUTE ledger counts + marks open_ended: percent now
    # reflects resolved/total and the flag tells the frontend the total is
    # non-final.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=45)
    p.set_phase("historical", open_ended=True)
    p.sync(completed=5, total=20, bars=None)
    snap = p.snapshot()
    assert snap.percent == 25.0  # never a fabricated 100 with 15 windows pending
    assert snap.open_ended is True


# ── add_bars (item 2) ───────────────────────────────────────────────────────────


def test_add_bars_accumulates_deltas_exactly_once_each() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=3)
    for delta in (100, 250, 7):
        p.add_bars(delta)
    assert p.snapshot().bars == 357


def test_add_bars_clamps_non_positive_deltas() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.add_bars(10)
    p.add_bars(0)
    p.add_bars(-5)
    assert p.snapshot().bars == 10


def test_add_bars_is_dropped_once_settled() -> None:
    # A late consumer delta landing after finish must never mutate a settled run.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.add_bars(10)
    p.finish()
    p.add_bars(99)
    assert p.snapshot().bars == 10


def test_add_bars_does_not_touch_units_or_eta_baseline() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=5)
    p.add_bars(1000)
    snap = p.snapshot()
    assert snap.completed == 0
    # bars are not units: the phase baseline stays PENDING (stamped by the next
    # counter mutation), so no rate was fabricated from the bars stream.
    assert snap.units_per_sec == 0.0


def test_snapshot_dataclass_defaults_are_honest() -> None:
    # Frozen-snapshot construction sanity for the new fields' defaults.
    snap = ProgressSnapshot(
        state=STATE_RUNNING,
        total=1,
        completed=0,
        failed=0,
        bars=0,
        current="",
        detail="",
        updated_at=1.0,
    )
    assert snap.started_at == 0.0
    assert snap.open_ended is False
    assert snap.phase_base_completed == 0
