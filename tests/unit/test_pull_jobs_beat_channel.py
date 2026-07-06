"""Per-run (pull-id-keyed) progress-beat channels — DEC-PULL-BEAT-PERRUN-001.

THE regression this pins: the ~600 s pull stall watchdog reads ONLY its own run's
beat channel, so an unrelated in-process thread firing ``emit_progress_beat()`` can
NEVER refresh the current run's heartbeat and mask a real pull stall. Lives in a
``test_pull_jobs*``-stemmed file (the ``test_pull_jobs_wedge.py`` precedent) so the
auto-discovery coverage gate maps it to ``brutex/dashboard/pull_jobs.py``.

Suite map:

* ``TestForeignBeatIsolation`` — the anti-masking regression (fail-before/pass-after)
  + the rebound-fan-out control arm (no new false positive) + stale-run isolation.
* ``TestBeatChannelLifecycle`` — keyed register/unregister, explicit/blank/None
  ``pull_id`` resolution, the ``_beat_for`` identity gate, the ``run_id`` watchdog
  stamp, and the durable-journal run-id pin (the ``current_pull_id()`` skip no longer
  fires during the run's own phases).
* ``TestConcurrentRunChannels`` — two live jobs' channels never crosstalk; a
  duplicate explicit pull_id is per-channel last-write-wins (loud, never masked).
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
import structlog.testing
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.observability import progress_beat as pb
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.observability.progress_beat import (
    emit_progress_beat,
    register_progress_beat,
    reset_progress_beats,
)
from brutex.observability.watchdog import WorkerEvent

from tests.unit.test_dashboard_pull_stall import _spy_factory

_FOREIGN_LOGGER = "brutex.observability.progress_beat"


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
def _no_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the cross-process flock (the guard's own suite covers it)."""
    from brutex.dashboard import pull_jobs

    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


@pytest.fixture(autouse=True)
def _reset_beats() -> Iterator[None]:
    """Every test starts + ends with an EMPTY beat registry (keyed AND legacy)."""
    reset_progress_beats()
    yield
    reset_progress_beats()


def _real_watchdog_job(
    clock: Callable[[], float],
) -> tuple[BackgroundPullJob, list[PullWatchdog]]:
    """A job wired to REAL PullWatchdogs (fake clock, no timer thread), sync worker."""
    built: list[PullWatchdog] = []

    def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        wd = PullWatchdog(
            on_hung=on_hung, timeout_seconds=600.0, clock=clock, spawn=lambda _fn: None
        )
        built.append(wd)
        return wd

    job = BackgroundPullJob(
        progress=PullProgress(clock=clock),
        spawn=lambda fn: fn(),  # synchronous worker — the test IS the worker thread
        watchdog_factory=factory,
    )
    return job, built


def _foreign_drop_warns(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _FOREIGN_LOGGER and "foreign_beat_dropped" in r.getMessage()
    ]


class TestForeignBeatIsolation:
    """The anti-masking guarantee + its control arm + stale-run isolation."""

    def test_foreign_thread_beat_does_not_suppress_stall_alarm(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """THE regression: a foreign in-process thread's beats can NOT mask a stall.

        Forensic shape: the run's last heartbeat is the watchdog-start seed at t=0; a
        FOREIGN thread (empty contextvar context ⇒ ambient pull_id ``""``, then a
        foreign-RUN id) fires 50 beats at t=400; at t=769 (> the 600 s timeout of pure
        run-channel silence) the timer poll MUST declare HUNG.

        FAIL-BEFORE/PASS-AFTER: on the unmodified single-slot seam the bare foreign
        emit at t=400 reached the one ``_BEAT`` slot → ``Watchdog._last_beat``
        refreshed → elapsed 369 < 600 at the check → no HUNG → the stalled assert
        FAILED (verified once by reverting the ``progress_beat.py`` + ``pull_jobs.py``
        keying and watching this test go red).
        """
        caplog.set_level(logging.WARNING, logger=_FOREIGN_LOGGER)
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
        stalled_mid_run: list[bool] = []
        suspect_events: list[dict[str, object]] = []

        def work(_on_progress: ProgressHook) -> None:
            box.advance(400.0)

            def _foreign_beats() -> None:
                # A fresh thread has an EMPTY contextvar context ⇒ ambient pull_id "".
                for _ in range(25):
                    emit_progress_beat()
                with pull_id_scope("feedfacef00d"):  # a foreign-RUN id
                    for _ in range(25):
                        emit_progress_beat()

            thread = threading.Thread(target=_foreign_beats)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()  # the 50 foreign emits all completed
            box.advance(369.0)  # total run-channel silence since t=0 is now 769 s
            wd = built[0]
            with structlog.testing.capture_logs() as logs:
                events = wd.check_once()  # the manual timer poll
            assert len(events) == 1  # the HUNG edge FIRED — the stall was NOT masked
            stalled_mid_run.append(job.snapshot().stalled)
            suspect_events.extend(e for e in logs if e["event"] == "pull_stall.stalled_suspect")

        assert job.start(work, total=1) is True
        assert stalled_mid_run == [True]
        assert len(suspect_events) == 1  # the LOUD event name, byte-preserved
        assert suspect_events[0]["pull_id"] == job.pull_id  # additive correlation
        assert len(alerts) == 1  # exactly one CRITICAL halt alert
        # Exactly ONE drop-WARN across the 50 foreign emits (once per register-epoch).
        assert len(_foreign_drop_warns(caplog)) == 1

    def test_rebound_fanout_thread_beat_keeps_watchdog_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control arm: the canonical capture-then-rebind fan-out beat still counts.

        A thread that DOES rebind the captured run id (the ``run_ingest._run_pair``
        simulation) keeps the watchdog HEALTHY at the same 769 s checkpoint — no new
        false positive — and pure silence past 600 s afterwards still fires HUNG (no
        false negative introduced either).
        """
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
        checkpoints: list[tuple[int, bool]] = []

        def work(_on_progress: ProgressHook) -> None:
            rid = current_pull_id()
            assert rid == job.pull_id  # _run's scope reaches the worker body

            def _fanout_beats() -> None:
                with pull_id_scope(rid):  # capture-then-rebind (run_ingest pattern)
                    for _ in range(5):
                        emit_progress_beat()

            box.advance(400.0)
            thread = threading.Thread(target=_fanout_beats)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            box.advance(369.0)  # t=769, but the rebound beat landed at t=400
            wd = built[0]
            checkpoints.append((len(wd.check_once()), job.snapshot().stalled))
            box.advance(601.0)  # pure silence past the timeout STILL alarms
            checkpoints.append((len(wd.check_once()), job.snapshot().stalled))

        assert job.start(work, total=1) is True
        assert checkpoints[0] == (0, False)  # rebound beat kept it HEALTHY at 769 s
        assert checkpoints[1] == (1, True)  # silence still fires HUNG (no masking)
        assert len(alerts) == 1

    def test_stale_run_thread_cannot_beat_newer_run(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lingering thread still bound to a FINISHED run's id never beats a newer run.

        The abandoned-hung-write-daemon / superseded-run failure mode: run A finishes,
        a leftover thread bound to A's ``pull_id`` emits during run B → B's watchdog
        gets NO beat (B stalls honestly on silence) and exactly ONE drop-WARN fires.
        """
        caplog.set_level(logging.WARNING, logger=_FOREIGN_LOGGER)
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)

        a_ids: list[str] = []

        def work_a(_on_progress: ProgressHook) -> None:
            a_ids.append(current_pull_id())

        assert job.start(work_a, total=1) is True
        a_id = a_ids[0]
        assert a_id == job.pull_id
        # A's keyed channel was popped by the finally: an emit under A's own id after
        # A finished is a SILENT no-op (empty registry — no warn, no beat).
        with pull_id_scope(a_id):
            emit_progress_beat()
        assert pb._BEATS == {}
        assert _foreign_drop_warns(caplog) == []

        stalled_mid_b: list[bool] = []

        def work_b(_on_progress: ProgressHook) -> None:
            def _stale_beats() -> None:
                with pull_id_scope(a_id):  # the stale run's id, still bound
                    for _ in range(10):
                        emit_progress_beat()

            thread = threading.Thread(target=_stale_beats)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            box.advance(601.0)  # B's channel saw ZERO beats — honest silence
            wd_b = built[1]
            assert len(wd_b.check_once()) == 1  # B is HUNG (stale beats dropped)
            stalled_mid_b.append(job.snapshot().stalled)

        assert job.start(work_b, total=1) is True
        assert job.pull_id != a_id  # a fresh run id per start
        assert stalled_mid_b == [True]
        assert len(_foreign_drop_warns(caplog)) == 1  # exactly one drop-WARN
        assert len(alerts) == 1


class TestBeatChannelLifecycle:
    """Register/unregister keying, id resolution, identity gate, journal + run_id pins."""

    def test_run_registers_keyed_and_finally_pops_only_own_channel(self) -> None:
        """The finally pops ONLY the run's key — a legacy registrant survives it."""
        legacy_beats: list[int] = []
        register_progress_beat(lambda: legacy_beats.append(1))  # sentinel legacy ""
        seen_keyed: list[bool] = []

        _built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory
        )

        def work(_on_progress: ProgressHook) -> None:
            seen_keyed.append(job.pull_id in pb._BEATS)

        assert job.start(work, total=1) is True
        assert seen_keyed == [True]  # registered KEYED on the run id during the run
        assert job.pull_id not in pb._BEATS  # the keyed channel was popped
        emit_progress_beat()  # unscoped → the legacy channel still fires
        assert legacy_beats == [1]  # keyed unregister never blanket-clears

    def test_start_pull_id_explicit_blank_and_none_resolution(self) -> None:
        """Explicit id honored; ``""`` AND ``None`` both MINT (never the legacy key)."""
        _built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1, pull_id="abc123def456") is True
        assert job.pull_id == "abc123def456"
        assert job.start(lambda _h: None, total=1, pull_id="") is True
        minted_from_blank = job.pull_id
        assert len(minted_from_blank) == 12 and minted_from_blank != ""
        assert all(c in "0123456789abcdef" for c in minted_from_blank)
        assert job.start(lambda _h: None, total=1) is True
        minted_from_none = job.pull_id
        assert len(minted_from_none) == 12
        assert minted_from_none != minted_from_blank  # fresh per run

    def test_pull_id_property_defaults_empty_before_first_start(self) -> None:
        _built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory
        )
        assert job.pull_id == ""

    def test_start_stamps_run_id_onto_the_watchdog(self) -> None:
        """``watchdog.run_id`` is stamped post-construction (factory signature intact)."""
        built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1, pull_id="abc123def456") is True
        assert built[0].run_id == "abc123def456"  # type: ignore[attr-defined]
        assert job.pull_id == "abc123def456"

    def test_beat_for_identity_gate_freezes_superseded_watchdog(self) -> None:
        """A superseded run's beat closure no-ops — it can never beat a newer run."""
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1) is True
        closure = job._beat_for(cast("PullWatchdog", built[0]))
        closure()
        assert built[0].beats == 1  # current run: the closure beats ITS watchdog
        captured[0]()  # run 1's worker completes (finally pops its channel)
        assert job.start(lambda _h: None, total=1) is True  # run 2 supersedes
        closure()  # the OLD run's closure: identity gate blocks
        assert built[0].beats == 1  # frozen — never beats past its own run
        assert built[1].beats == 0  # and NEVER the newer run's watchdog

    def test_seam_beat_journals_the_run_id_during_post_fetch_phases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable journal heartbeat now records the RUN id from a seam beat.

        The graft pin: ``_run``'s scope means ``_maybe_journal_heartbeat``'s
        no-bound-``pull_id`` skip no longer fires during the run's own (post-fetch)
        phases — a ``record_pull_heartbeat`` spy sees the run id.
        """
        recorded: list[str] = []

        def _spy(_root: Path, *, pull_id: str) -> None:
            recorded.append(pull_id)

        monkeypatch.setattr("brutex.observability.pull_journal.record_pull_heartbeat", _spy)
        built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(),
            spawn=lambda fn: fn(),
            watchdog_factory=factory,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: contextlib.nullcontext(),
            account_key_resolver=lambda: None,
        )

        def work(_on_progress: ProgressHook) -> None:
            emit_progress_beat()  # a post-fetch-phase seam beat (greeks/prebuild shape)

        assert job.start(work, total=1) is True
        assert built[0].beats == 1  # the seam beat reached the run's watchdog
        assert recorded == [job.pull_id]  # journaled under the RUN id, never skipped

    def test_watchdog_never_beaten_channel_still_alarms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run whose channel is NEVER beaten alarms on the seed-age alone."""
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        fired: list[int] = []

        def work(_on_progress: ProgressHook) -> None:
            box.advance(601.0)  # no beat ever lands on the run's channel
            fired.append(len(built[0].check_once()))

        assert job.start(work, total=1) is True
        assert fired == [1]


class TestConcurrentRunChannels:
    """Two live channels never crosstalk; a duplicate id is last-write-wins (loud)."""

    def test_two_concurrent_jobs_route_only_to_their_own_watchdogs(self) -> None:
        built_a, factory_a = _spy_factory()
        built_b, factory_b = _spy_factory()
        job_b = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory_b
        )
        job_a = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory_a
        )

        def work_a(_on_progress: ProgressHook) -> None:
            # While A is live, run B to completion INSIDE A's window so both keyed
            # channels are registered concurrently at B's emit points.
            def work_b(_on_progress_b: ProgressHook) -> None:
                with pull_id_scope(job_a.pull_id):
                    emit_progress_beat()  # addressed to A → beats ONLY A
                emit_progress_beat()  # ambient = B's run id → beats ONLY B
                emit_progress_beat(pull_id=job_b.pull_id)  # explicit arg → B again

            assert job_b.start(work_b, total=1) is True

        assert job_a.start(work_a, total=1) is True
        assert built_a[0].beats == 1  # no crosstalk in either direction
        assert built_b[0].beats == 2

    def test_duplicate_pull_id_is_per_channel_last_write_wins(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two live runs keyed on the SAME id: the newest registrant owns the channel
        AND the overwrite is WARNed as a caller bug (``progress_beat.channel_collision``
        — fix round). HONEST residual: WHILE both are live, an emit addressed to the
        shared key routes to the NEWER registrant (the channel key is the only identity
        the seam has), so a duplicate live id is loudly flagged, not silently
        discriminated. After the newer run finishes it pops the shared key
        (identity-guarded), so the older run's beats are then DROPPED (its watchdog
        stalls honestly on silence) — never a SILENT beat into a run that did not
        register the channel."""
        caplog.set_level(logging.WARNING, logger=_FOREIGN_LOGGER)
        empty_after_pop: list[bool] = []
        built_a, factory_a = _spy_factory()
        built_b, factory_b = _spy_factory()
        job_b = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory_b
        )
        job_a = BackgroundPullJob(
            progress=PullProgress(), spawn=lambda fn: fn(), watchdog_factory=factory_a
        )

        def work_a(_on_progress: ProgressHook) -> None:
            def work_b(_on_progress_b: ProgressHook) -> None:
                emit_progress_beat(pull_id="dupdupdup001")  # B re-keyed the channel

            assert job_b.start(work_b, total=1, pull_id="dupdupdup001") is True
            # B's finally popped the SHARED key (its own channel): A's beat is now
            # DROPPED — never silently rerouted into another run — so A's watchdog
            # stalls honestly on silence.
            empty_after_pop.append(pb._BEATS == {})
            emit_progress_beat(pull_id="dupdupdup001")

        assert job_a.start(work_a, total=1, pull_id="dupdupdup001") is True
        assert built_b[0].beats == 1  # last write won while both were live
        assert built_a[0].beats == 0  # A never received a beat it did not own
        assert empty_after_pop == [True]  # the shared key is gone → honest silence
        collisions = [
            r
            for r in caplog.records
            if r.name == _FOREIGN_LOGGER and "channel_collision" in r.getMessage()
        ]
        assert len(collisions) == 1  # the duplicate LIVE id was loudly flagged


class TestDirectHookGate:
    """The FIX-ROUND direct-hook gate: `_beat` (behind `_on_progress` /
    `sync_from_ledger` / `add_bars`) drops a beat whose ambient pull_id names a
    DIFFERENT run — closing the zombie side-door the keyed seam channels never see
    (a drain-abandoned fetch-pipeline consumer un-wedging after run A ended and
    folding its backlog through app.py's ``on_bars=_pull_job.add_bars``)."""

    def test_stale_run_thread_add_bars_cannot_beat_newer_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE zombie-consumer regression: run A's abandoned thread (still bound to
        A's id by the consumer's own ``pull_id_scope``) drives ``job.add_bars``
        while run B is live → B's watchdog gets NO beat (B alarms honestly at
        769 s) and exactly ONE ``pull_job.foreign_beat_dropped`` WARN fires."""
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)

        a_ids: list[str] = []
        assert job.start(lambda _h: a_ids.append(current_pull_id()), total=1) is True
        a_id = a_ids[0]

        stalled_mid_b: list[bool] = []
        drop_events: list[dict[str, object]] = []

        def work_b(_on_progress: ProgressHook) -> None:
            box.advance(400.0)

            def _zombie_folds() -> None:
                with pull_id_scope(a_id):  # the abandoned consumer's bound run-A id
                    job.add_bars(5)
                    job.add_bars(7)  # a second fold — the WARN stays once per run

            thread = threading.Thread(target=_zombie_folds)
            with structlog.testing.capture_logs() as logs:
                thread.start()
                thread.join(timeout=10)
            assert not thread.is_alive()
            drop_events.extend(e for e in logs if e["event"] == "pull_job.foreign_beat_dropped")
            box.advance(369.0)  # 769 s of run-B channel silence since the t=0 seed
            wd_b = built[1]
            assert len(wd_b.check_once()) == 1  # HUNG — the zombie folds were dropped
            stalled_mid_b.append(job.snapshot().stalled)

        assert job.start(work_b, total=1) is True
        assert job.pull_id != a_id
        assert stalled_mid_b == [True]
        assert len(drop_events) == 1  # loud exactly once per run
        assert drop_events[0]["ambient_pull_id"] == a_id
        assert len(alerts) == 1

    def test_empty_ambient_direct_beat_keeps_the_watchdog_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control arm (back-compat): an UNSCOPED thread's direct hook still beats —
        legacy callers / tests / unrebound hooks introduce no new false stall."""
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        checkpoints: list[int] = []

        def work(_on_progress: ProgressHook) -> None:
            box.advance(400.0)

            def _legacy_fold() -> None:
                job.add_bars(3)  # fresh thread — empty contextvar context, ambient ""

            thread = threading.Thread(target=_legacy_fold)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            box.advance(369.0)  # t=769, but the empty-ambient beat landed at t=400
            checkpoints.append(len(built[0].check_once()))

        assert job.start(work, total=1) is True
        assert checkpoints == [0]  # HEALTHY — the legacy direct beat counted

    def test_own_run_direct_beat_beats_and_warn_rearms_next_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run's OWN ambient id passes the gate; the once-per-run WARN throttle
        re-arms on the next start (a stale fold in run C warns again)."""
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)

        def work_b(_on_progress: ProgressHook) -> None:
            box.advance(400.0)
            job.add_bars(1)  # worker thread — ambient IS the run id (bound by _run)
            box.advance(369.0)
            assert len(built[0].check_once()) == 0  # own-run beat counted at t=400

        assert job.start(work_b, total=1) is True
        b_id = job.pull_id

        def work_c(_on_progress: ProgressHook) -> None:
            def _stale() -> None:
                with pull_id_scope(b_id):
                    job.add_bars(1)

            thread = threading.Thread(target=_stale)
            with structlog.testing.capture_logs() as logs:
                thread.start()
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert [e["event"] for e in logs].count("pull_job.foreign_beat_dropped") == 1

        assert job.start(work_c, total=1) is True  # the WARN re-armed for run C


class TestStallEdgeRunId:
    """The three stall-edge events stamp the race-free ``watchdog.run_id`` — a stale
    edge can never carry a NEWER run's ``self._pull_id`` (fix-round MINOR)."""

    def test_stalled_suspect_event_carries_run_id_not_clobbered_pull_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        events: list[dict[str, object]] = []

        def work(_on_progress: ProgressHook) -> None:
            run_id = job.pull_id
            # Simulate the race window: a newer run publishes a different _pull_id
            # between the identity-gated lock and the log line.
            job._pull_id = "feedfacef00d"
            box.advance(601.0)
            with structlog.testing.capture_logs() as logs:
                assert len(built[0].check_once()) == 1
            events.extend(e for e in logs if e["event"] == "pull_stall.stalled_suspect")
            assert events[0]["pull_id"] == run_id  # the OWN run's id, never the newer

        assert job.start(work, total=1) is True
        assert len(events) == 1

    def test_stalled_recovered_event_carries_run_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock, box = _make_clock()
        job, built = _real_watchdog_job(clock)
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        events: list[dict[str, object]] = []

        def work(_on_progress: ProgressHook) -> None:
            run_id = job.pull_id
            box.advance(601.0)
            assert len(built[0].check_once()) == 1  # HUNG
            job._pull_id = "feedfacef00d"  # the race-window clobber
            built[0].beat()  # a heartbeat lands again
            with structlog.testing.capture_logs() as logs:
                built[0].check_once()  # the recovery edge
            events.extend(e for e in logs if e["event"] == "pull_stall.stalled_recovered")
            assert events[0]["pull_id"] == run_id

        assert job.start(work, total=1) is True
        assert len(events) == 1
        assert job.snapshot().stalled is False

    def test_wedged_event_carries_run_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.observability.watchdog import WorkerState

        clock, _box = _make_clock()
        job, built = _real_watchdog_job(clock)
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
        events: list[dict[str, object]] = []

        def work(_on_progress: ProgressHook) -> None:
            run_id = job.pull_id
            job._pull_id = "feedfacef00d"  # the race-window clobber
            evt = WorkerEvent(
                worker_name="dashboard-pull",
                new_state=WorkerState.HUNG,
                last_beat_seconds_ago=3600.0,
                timeout_seconds=600.0,
            )
            with structlog.testing.capture_logs() as logs:
                job._on_wedged(evt, built[0])
            events.extend(e for e in logs if e["event"] == "pull_stall.wedged")
            assert events[0]["pull_id"] == run_id

        assert job.start(work, total=1) is True
        assert len(events) == 1
        assert len(alerts) == 1


class TestRefusedStartRollback:
    """A singleflight-REFUSED start publishes NO phantom run identity (fix round)."""

    def test_refused_start_leaves_no_phantom_pull_id(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.pull_singleflight import PullAlreadyRunningError

        class _HeldGuard:
            def __enter__(self) -> None:
                raise PullAlreadyRunningError(tmp_path / "pull.flock")

            def __exit__(self, *_exc: object) -> None:
                return None

        _built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(),
            spawn=lambda fn: fn(),
            watchdog_factory=factory,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _HeldGuard(),
            account_key_resolver=lambda: None,
        )
        assert job.start(lambda _h: None, total=1, pull_id="abc123def456") is False
        assert job.pull_id == ""  # the refused run never existed — no phantom id
        assert job.running is False
        # A later start is unaffected by the rollback.
        job2 = BackgroundPullJob(
            progress=PullProgress(),
            spawn=lambda fn: fn(),
            watchdog_factory=_spy_factory()[1],
        )
        assert job2.start(lambda _h: None, total=1) is True
