"""Pipeline stall-watchdog heartbeat regressions (DEC-PIPELINE-STALL-HEARTBEAT-001).

FORENSICALLY PROVEN BUG (operator run 2026-07-02, six logs): the dashboard pull's
stall watchdog received heartbeats ONLY on per-pair fetch advance + ledger sync, so
the post-fetch GREEKS enrichment (6.5 min small-run, hours at full-history scale),
the timeframe PREBUILD (10.2 min small-run) and the MOVERS WARM (6.4 min) were
silent. Last fetch beat 13:58:21 → watchdog fired at ~14:11:10 (769 s > the 600 s
timeout), FAILED the progress and cleared ``_running`` + released the account
single-flight — while the worker thread kept running and SUCCEEDED (prebuild done
14:15:08, 47.9M bars, movers warm + gate release clean). Two harms: (a) a false
FAILED headline on a succeeding run; (b) the released single-flight admitting a
second pull concurrent with the still-running zombie worker (double-run race).

This suite pins the fix, fail-before/pass-after:

* (a) a fake pipeline whose greeks + prebuild + movers stages outlast a tiny test
  timeout — with the new ``emit_progress_beat`` seams beating — is NEVER declared
  hung and finishes ``done``;
* (b) the same shape with the seams SILENT (a genuine hang) IS declared hung — the
  CRITICAL alert fires and the job is marked STALLED-SUSPECT — but the single-flight
  is NOT released while the worker is alive (a second pull is refused);
* (c) the progress counters advance past the metadata freeze (units + cumulative
  bars reflect the historical phase; the metadata sync no longer wipes bars);
* (d) the operator's small-run forensic shape (fetch beats stop, the greeks pass
  runs a 769 s-equivalent) reproduced through the REAL
  :func:`~brutex.data.greeks_enrich.enrich_greeks_for_targets` /
  :func:`~brutex.dashboard.gated_prebuild.run_gated_prebuild` /
  :func:`~brutex.data.timeframe_materialize.materialize_timeframes` /
  :func:`~brutex.dashboard.movers_warm.warm_movers_snapshots` loops, each proven to
  beat the process-global seam per unit of work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import polars as pl
import pytest
from brutex.dashboard.gated_prebuild import run_gated_prebuild
from brutex.dashboard.movers_warm import warm_movers_snapshots
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, STATE_RUNNING, PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.data.greeks_enrich import GreeksEnrichTarget, enrich_greeks_for_targets
from brutex.data.timeframe_materialize import MaterializeResult, materialize_timeframes
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.progress_beat import (
    register_progress_beat,
    reset_progress_beats_for_tests,
)
from brutex.observability.watchdog import WorkerEvent
from brutex.options.risk_free import FixedRateSource


class _ClockBox:
    """Mutable time holder for the manually-advanced fake clock (no sleeping)."""

    def __init__(self) -> None:
        self.t = 0.0

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_clock() -> tuple[Callable[[], float], _ClockBox]:
    """A manually-advanced monotonic clock as a PLAIN ``def`` closure.

    Not a callable class: CodeQL's cross-function points-to analysis flags the
    production ``clock()`` call sites as ``py/call-to-non-callable`` when a test
    passes a callable-class instance as the injected clock (the known
    false-positive pattern in the team playbook) — a plain ``def`` is
    unambiguously callable. Advance time through the returned :class:`_ClockBox`.
    """
    box = _ClockBox()

    def clock() -> float:
        return box.t

    return clock, box


@pytest.fixture(autouse=True)
def _clean_beat_seam() -> Iterator[None]:
    """Every test starts + ends with NO process-global beat registered."""
    reset_progress_beats_for_tests()
    yield
    reset_progress_beats_for_tests()


@pytest.fixture(autouse=True)
def _no_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the cross-process flock for the job-level tests (covered elsewhere)."""
    from brutex.dashboard import pull_jobs

    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


def _counting_beat() -> tuple[list[int], Callable[[], None]]:
    beats: list[int] = []
    return beats, lambda: beats.append(1)


# ────────────────────────────────────────────────────────────────────────
# The per-stage seams: each post-fetch loop beats the process-global seam
# per unit of work (fail-before: pre-fix these loops emitted ZERO beats).
# ────────────────────────────────────────────────────────────────────────


class TestStageSeamsBeat:
    def test_greeks_enrich_beats_per_contract(self) -> None:
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        targets = [
            GreeksEnrichTarget(
                exchange="NSE", symbol=s, start=date(2024, 1, 1), end=date(2024, 1, 20)
            )
            for s in (
                "NSE-NIFTY-25Jan24-22000-CE",
                "NSE-NIFTY-25Jan24-22000-PE",
                "NSE-NIFTY-25Jan24-FUT",
            )
        ]
        report = enrich_greeks_for_targets(
            targets=targets,
            lake_root=None,  # type: ignore[arg-type] — every I/O seam injected below
            rate_source=FixedRateSource(),
            read_bars=lambda _t: [],  # no bars → each target is a cheap counted skip
            read_spot=lambda _t, _p: (_ for _ in ()).throw(AssertionError("unreachable")),
            write_bars=lambda _t, _b: 0,
        )
        assert report.no_bars == 2 and report.futures_skipped == 1
        assert len(beats) == 3  # ONE beat per contract, before any work

    def test_timeframe_materialize_beats_per_timeframe(self) -> None:
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        frame = pl.DataFrame({"x": [1]})
        result = materialize_timeframes(
            tfs=("5m", "15m", "60m"),
            read_1m=lambda: frame,
            write_tf=lambda _tf, f: f.height,
            resampler=lambda df: {"5m": df, "15m": df},  # 60m absent → skipped
        )
        assert result.written == {"5m": 1, "15m": 1} and result.skipped == ("60m",)
        assert len(beats) == 3  # ONE beat per requested timeframe (skips beat too)

    def test_gated_prebuild_beats_per_symbol_and_contract(self) -> None:
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        inst = _spot("NIFTY")
        report = run_gated_prebuild(
            underlyings=[inst],
            materialize_spot=lambda _i: MaterializeResult(written={"5m": 1}, skipped=()),
            futures_contracts_for=lambda _i: ["NIFTY-Jan2024-FUT", "NIFTY-Feb2024-FUT"],
            materialize_future=lambda _c: MaterializeResult(written={"5m": 1}, skipped=()),
            options_contracts_for=lambda _i: ["NIFTY-Jan2024-22000-CE"],
            materialize_option=lambda _c: MaterializeResult(written={"5m": 1}, skipped=()),
        )
        assert report.ok
        # 1 spot underlying + 2 futures contracts + 1 option contract = 4 beats.
        assert len(beats) == 4

    def test_movers_warm_beats_per_coordinate(self) -> None:
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        warmed = warm_movers_snapshots([lambda: 1, lambda: 0, lambda: 2])
        assert warmed.warmed == 2
        assert len(beats) == 3  # ONE beat per warm job, raising or not


def _spot(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(name)) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


# ────────────────────────────────────────────────────────────────────────
# End-to-end job shapes (a) / (b) / (d): a real PullWatchdog on a fake
# clock, the timer poll driven deterministically from inside the work.
# ────────────────────────────────────────────────────────────────────────


def _job_with_real_watchdog(
    clock: Callable[[], float], *, timeout: float = 600.0
) -> tuple[BackgroundPullJob, list[PullWatchdog], list[BaseException]]:
    """A job wired to a REAL PullWatchdog (fake clock, no daemon timer) + alert capture."""
    built: list[PullWatchdog] = []
    alerts: list[BaseException] = []

    def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        wd = PullWatchdog(
            on_hung=on_hung, timeout_seconds=timeout, spawn=lambda _fn: None, clock=clock
        )
        built.append(wd)
        return wd

    job = BackgroundPullJob(
        progress=PullProgress(clock=clock),
        spawn=lambda fn: fn(),  # synchronous worker — the test IS the worker thread
        watchdog_factory=factory,
    )
    return job, built, alerts


def test_a_beating_post_fetch_stages_never_declared_hung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) greeks+prebuild+movers outlast the timeout WITH the seams beating → no fire."""
    clock, box = _make_clock()
    job, built, alerts = _job_with_real_watchdog(clock)
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)

    def work(on_progress: ProgressHook) -> None:
        wd = built[0]
        on_progress("NIFTY 1minute", 1000, False, False)  # the last FETCH beat
        # Three post-fetch stages, each 4 × 300 s = 1200 s (2× the 600 s timeout) of
        # wall clock — but each unit of work beats the process-global seam (as the
        # real greeks/prebuild/movers loops now do), so every timer poll stays HEALTHY.
        stages_survived: list[str] = []
        for stage in ("greeks", "prebuild", "movers"):
            for _ in range(4):
                box.advance(300.0)
                # the stage loop's per-unit beat (emit_progress_beat → the job's beat)
                from brutex.observability.progress_beat import emit_progress_beat

                emit_progress_beat()
                assert wd.check_once() == ()  # the timer poll: never a HUNG edge
            stages_survived.append(stage)
        assert stages_survived == ["greeks", "prebuild", "movers"]

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_DONE
    assert snap.stalled is False
    assert alerts == []  # the watchdog NEVER fired
    assert not job.running


def test_b_silent_stages_fire_watchdog_but_hold_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) a GENUINE hang (seams silent) is still detected — and the flight stays held."""
    clock, box = _make_clock()
    job, built, alerts = _job_with_real_watchdog(clock)
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
    refused_mid_run: list[bool] = []
    stalled_mid_run: list[bool] = []
    state_mid_run: list[str] = []

    def work(on_progress: ProgressHook) -> None:
        wd = built[0]
        on_progress("NIFTY 1minute", 1000, False, False)
        box.advance(769.0)  # the forensic silence — NO seam beats (true wedge)
        events = wd.check_once()  # the timer poll declares HUNG
        assert len(events) == 1
        # While the worker is still alive: detected + reported, but NOT released.
        stalled_mid_run.append(job.snapshot().stalled)
        state_mid_run.append(job.snapshot().state)
        refused_mid_run.append(job.start(lambda _h: None, total=1))

    assert job.start(work, total=1) is True
    assert len(alerts) == 1 and isinstance(alerts[0], TimeoutError)
    assert stalled_mid_run == [True]  # honest DEGRADED marker while wedged
    assert state_mid_run == [STATE_RUNNING]  # never a false FAILED
    assert refused_mid_run == [False]  # single-flight HELD → no double-run race
    # The worker eventually exited (our synchronous work returned) → clean finish.
    snap = job.snapshot()
    assert snap.state == STATE_DONE and snap.stalled is False
    assert not job.running


def test_d_forensic_small_run_shape_greeks_769s_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) the operator's run shape: fetch beats stop, the REAL greeks loop runs 769 s+.

    Reproduces the 13:58:21 → 14:11:10 silence through the REAL
    ``enrich_greeks_for_targets`` loop: each contract's read costs 300 s of fake
    clock and the watchdog polls after each, so the cumulative pass (900 s total,
    > the 769 s that killed the operator's run) crosses the 600 s timeout many
    times over. With the per-contract seam beat the watchdog NEVER fires
    (fail-before: reverting the greeks-loop ``emit_progress_beat`` fails this).
    """
    clock, box = _make_clock()
    job, built, alerts = _job_with_real_watchdog(clock)
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)

    def work(on_progress: ProgressHook) -> None:
        wd = built[0]
        on_progress("NSE-BANKNIFTY-25Jun26-50000-PE 1minute", 42, False, False)  # 13:58:21

        def slow_read(_t: GreeksEnrichTarget) -> list[dict[str, object]]:
            box.advance(300.0)  # a slow contract read/solve
            assert wd.check_once() == ()  # the timer poll during the greeks phase
            return []

        report = enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(
                    exchange="NSE", symbol=s, start=date(2026, 6, 1), end=date(2026, 6, 25)
                )
                for s in (
                    "NSE-NIFTY-25Jun26-22000-CE",
                    "NSE-NIFTY-25Jun26-22000-PE",
                    "NSE-BANKNIFTY-25Jun26-50000-CE",
                )
            ],
            lake_root=None,  # type: ignore[arg-type] — I/O seams injected
            rate_source=FixedRateSource(),
            read_bars=slow_read,
            read_spot=lambda _t, _p: (_ for _ in ()).throw(AssertionError("unreachable")),
            write_bars=lambda _t, _b: 0,
        )
        assert report.no_bars == 3

    assert job.start(work, total=1) is True
    assert alerts == []  # the 769 s-equivalent greeks pass is no longer a false HUNG
    assert job.snapshot().state == STATE_DONE
    assert not job.running


# ────────────────────────────────────────────────────────────────────────
# (c) the frozen progress counters: metadata no longer wipes bars, the
# historical phase advances units + cumulative bars per pair.
# ────────────────────────────────────────────────────────────────────────


class TestFrozenCountersFix:
    def test_metadata_sync_preserves_cumulative_bars(self) -> None:
        """``sync_from_ledger(bars=None)`` keeps the bars an earlier phase streamed."""
        job = BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())

        def work(_h: ProgressHook) -> None:
            # SPOT phase: the ledger sync streams real bars.
            job.sync_from_ledger(completed=10, total=20, bars=14_480_000)
            # METADATA phase: shape-only — bars=None must PRESERVE the tally
            # (the pre-fix hard-coded bars=0 wiped it to "0 BARS").
            job.sync_from_ledger(completed=45, total=45, bars=None, current="expiries")
            snap = job.snapshot()
            assert snap.bars == 14_480_000
            assert snap.completed == 45 and snap.total == 45

        assert job.start(work, total=20, ledger_synced=True) is True

    def test_historical_phase_counters_advance_past_metadata_freeze(self) -> None:
        """(c) the per-pair hook advances units + bars in the pair-progress phase.

        The forensic freeze: the pipeline job runs ``ledger_synced=True`` so the
        historical per-pair hook only refreshed the LABEL — "45/45 units, 0 BARS,
        0 failed" while 14.48M bars streamed in. With
        ``set_phase("historical", pair_progress=True)`` the hook advances the
        open-ended units + cumulative bars + failed tallies, O(1) per event.
        """
        job = BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())

        def work(on_progress: ProgressHook) -> None:
            # Metadata phase ends at the frozen shape: 45/45 units, no bars concept.
            job.set_phase("metadata")
            job.sync_from_ledger(completed=45, total=45, bars=None)
            # Historical phase: NO ledger-sync driver → opt into pair progress.
            job.set_phase("historical", pair_progress=True)
            on_progress("NIFTY-Jun2026-FUT 1minute", 7_000_000, False, False)
            on_progress("NIFTY-Jun2026-22000-CE 1minute", 7_480_000, False, False)
            on_progress("NIFTY-Jun2026-22000-PE 1minute", 0, True, False)
            snap = job.snapshot()
            assert snap.completed == 48  # 45 metadata units + 3 pairs — MOVING
            assert snap.total == 48  # open-ended: grows with completed
            assert snap.bars == 14_480_000  # cumulative bars — no longer "0 BARS"
            assert snap.failed == 1
            assert snap.phase == "historical"

        assert job.start(work, total=45, ledger_synced=True) is True
        assert job.snapshot().state == STATE_DONE

    def test_ledger_synced_phase_without_pair_progress_keeps_label_only(self) -> None:
        """The spot/metadata contract is byte-unchanged: label-only per-pair hook."""
        job = BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())

        def work(on_progress: ProgressHook) -> None:
            job.sync_from_ledger(completed=5, total=20, bars=100)
            on_progress("NIFTY 1minute", 999, False, False)  # label only — no advance
            snap = job.snapshot()
            assert snap.completed == 5 and snap.total == 20 and snap.bars == 100
            assert snap.current == "NIFTY 1minute"

        assert job.start(work, total=20, ledger_synced=True) is True

    def test_set_phase_default_resets_pair_progress(self) -> None:
        """Leaving the pair-progress phase (prebuild next) re-arms label-only mode."""
        job = BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())

        def work(on_progress: ProgressHook) -> None:
            job.set_phase("historical", pair_progress=True)
            on_progress("A 1minute", 10, False, False)
            job.set_phase("prebuild")  # default pair_progress=False
            on_progress("B 1minute", 99, False, False)  # back to label-only
            snap = job.snapshot()
            assert snap.completed == 1 and snap.bars == 10
            assert snap.current == "B 1minute"

        assert job.start(work, total=1, ledger_synced=True) is True
