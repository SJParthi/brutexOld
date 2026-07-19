"""Heal-scan + ledger-save watchdog liveness beats (DEC-HEAL-SCAN-LIVENESS-BEATS-001).

FORENSICALLY PROVEN BUG (production run, refuter-confirmed): the L2 post-pull
heal scan (O(files) — 36 min over 101,682 files, ``lake.invariant.scan_complete``)
emitted ZERO watchdog liveness beats, so ``pull_stall.stalled_suspect`` falsely
fired "hung" (``last_beat_seconds_ago=655.7``) for ~27 min on a provably-healthy
process (scan log lines every ~30 ms). The beat first died at a 12.96 s
synchronous ``window_ledger.save_slow`` (68,234 rows) at drain, and the heal
scan then kept it dead. The DEC-PIPELINE-STALL-HEARTBEAT-001 claim that the
"false-positive class is closed" was therefore FALSIFIED — these two surfaces
were never covered.

This suite pins the fix, fail-before/pass-after (the #1558 greeks-backfill
per-file beat precedent):

* ``lake_heal.heal_after_pull`` beats per considered TARGET and per scanned
  FILE (through ``scan_lake_invariants``'s existing ``per_file_pause`` seam —
  zero scanner changes) and per healed finding;
* ``window_ledger_store.save_window_ledger`` beats at entry AND completion, so
  a multi-second drain save can no longer strand the last heartbeat;
* the forensic replay: a REAL :class:`~brutex.dashboard.pull_stall.PullWatchdog`
  on a fake clock stays HEALTHY (zero HUNG edges ⇒ zero ``stalled_suspect``)
  across a heal scan whose per-file wall clock far exceeds the 600 s stall
  timeout, and across a >600 s ledger save — both FAIL with the beats reverted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data import lake_heal as lake_heal_module
from brutex.data import window_ledger_store as ledger_store_module
from brutex.data.lake_heal import heal_after_pull
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.observability.progress_beat import (
    register_progress_beat,
    reset_progress_beats,
)
from brutex.observability.watchdog import WorkerEvent


@pytest.fixture(autouse=True)
def _clean_beat_seam() -> Iterator[None]:
    """Every test starts + ends with NO beat registered on ANY channel."""
    reset_progress_beats()
    yield
    reset_progress_beats()


class _ClockBox:
    """Mutable time holder for the manually-advanced fake clock (no sleeping)."""

    def __init__(self) -> None:
        self.t = 0.0

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_clock() -> tuple[Callable[[], float], _ClockBox]:
    """A manually-advanced monotonic clock as a PLAIN ``def`` closure.

    Not a callable class: CodeQL's points-to analysis flags callable-class
    clock instances at the production call sites (the known false-positive
    pattern in the team playbook) — a plain ``def`` is unambiguously callable.
    """
    box = _ClockBox()

    def clock() -> float:
        return box.t

    return clock, box


def _counting_beat() -> tuple[list[int], Callable[[], None]]:
    beats: list[int] = []
    return beats, lambda: beats.append(1)


def _write_engine_bar(lake_root: Path) -> None:
    """One clean 1-minute NIFTY INDEX bar through the production writer."""
    ParquetWriter(lake_root=lake_root).write(
        bars=[
            CandleBar(
                timestamp=datetime(2025, 9, 1, 4, 0, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=0,
            )
        ],
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
    )


def _poison_engine_partition(lake_root: Path) -> None:
    """Plant a duplicate-timestamp NIFTY month file with raw pyarrow."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "10.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
    prices = [100.0, 100.0]
    table = pa.table(
        {
            "timestamp": pa.array([ts, ts], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10, 10], type=pa.int64()),
            "open_interest": pa.array([None, None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)  # type: ignore[no-untyped-call, unused-ignore]


class TestHealScanBeats:
    """The L2 heal scan's per-target / per-file / per-finding beat wiring."""

    def test_clean_heal_scan_beats_per_target_and_per_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 target + 1 scanned file ⇒ 2 beats (fail-before: ZERO beats)."""
        # EXPLICIT flag-OFF since the W-5 default flip: the exact beat COUNT
        # pins the PYTHON scanner's per-file beat wiring; the engaged crate
        # arm's beat replay is pinned by the waved scan differential.
        monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        _write_engine_bar(lake_root)
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        report = heal_after_pull(
            lake_root=lake_root,
            targets=[("NSE", "INDEX", "NIFTY")],
            start=date(2025, 9, 1),
            end=date(2025, 9, 2),
            scope="test",
        )
        assert report.findings == 0
        assert len(beats) == 2  # one per considered target + one per scanned file

    def test_poisoned_heal_beats_per_finding_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A healed finding adds its own beat (the heal rewrite is seconds-slow)."""
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        _write_engine_bar(lake_root)
        _poison_engine_partition(lake_root)
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        report = heal_after_pull(
            lake_root=lake_root,
            targets=[("NSE", "INDEX", "NIFTY")],
            start=date(2025, 10, 1),
            end=date(2025, 10, 2),
            scope="test",
        )
        assert report.healed == 1
        # 1 target + 2 scanned files (the clean 09 + poisoned 10 month files are
        # both inside the ±1-day padded IST month scope) + 1 healed finding —
        # plus the heal RECALL's own per-timeframe beats (the pre-existing
        # DEC-PIPELINE-STALL-HEARTBEAT-001 materialize seams), hence >=.
        assert len(beats) >= 4


class TestForensicReplay:
    """The false-stall replay: a REAL watchdog stays HEALTHY across slow surfaces."""

    def test_slow_heal_scan_never_declares_stalled_suspect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 36-min-scan shape: >600 s of per-file wall clock, ZERO HUNG edges.

        Each scanned file costs 400 s of fake clock and the watchdog timer polls
        after every file — cumulative silence would be 800 s ≫ the 600 s timeout,
        so WITHOUT the per-file beat (revert ``per_file_pause=emit_progress_beat``
        in ``heal_after_pull``) the poll declares HUNG and this test FAILS —
        exactly the production ``pull_stall.stalled_suspect`` false fire.
        """
        from brutex.dashboard.pull_stall import PullWatchdog

        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        _write_engine_bar(lake_root)
        _poison_engine_partition(lake_root)  # 2 files in scope → 800 s of scan

        clock, box = _make_clock()
        hung: list[WorkerEvent] = []
        wd = PullWatchdog(
            on_hung=hung.append, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        register_progress_beat(wd.beat)
        wd.beat()  # the last fetched-window heartbeat before the drain stages

        from brutex.data.lake_invariants import scan_lake_invariants as real_scan

        def _slow_scan(lake: Path, **kwargs: Any) -> Any:
            inner = kwargs.get("per_file_pause")

            def paced() -> None:
                box.advance(400.0)  # each file "costs" 400 s of wall clock
                if callable(inner):
                    inner()  # the heal's per-file liveness beat (the fix)
                # The watchdog timer poll lands mid-scan: with the beat wired
                # this NEVER fires; with it reverted the 2nd file crosses 600 s.
                assert wd.check_once() == (), "the heal scan starved the watchdog"

            kwargs["per_file_pause"] = paced
            return real_scan(lake, **kwargs)

        monkeypatch.setattr(lake_heal_module, "scan_lake_invariants", _slow_scan)
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        report = heal_after_pull(
            lake_root=lake_root,
            targets=[("NSE", "INDEX", "NIFTY")],
            start=date(2025, 10, 1),
            end=date(2025, 10, 2),
            scope="test",
        )
        assert report.healed == 1
        assert box.t >= 800.0  # the replay genuinely outlasted the 600 s timeout
        assert wd.check_once() == ()
        assert hung == []  # zero HUNG edges ⇒ zero pull_stall.stalled_suspect

    def test_slow_ledger_save_keeps_the_watchdog_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain-save shape: a 650 s synchronous save, ZERO HUNG edges.

        The forensic chain began at a 12.96 s ``window_ledger.save_slow`` that
        stranded the last heartbeat; scaled past the timeout here. The
        completion beat (the fix) refreshes liveness the moment the save body
        finishes — reverting the ``save_window_ledger`` beats FAILS this test.
        """
        from brutex.dashboard.pull_stall import PullWatchdog

        clock, box = _make_clock()
        hung: list[WorkerEvent] = []
        wd = PullWatchdog(
            on_hung=hung.append, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        register_progress_beat(wd.beat)
        wd.beat()  # the last fetched-window heartbeat before the drain save

        from brutex.data.atomic_io import atomic_write_via as real_write

        def _slow_write(path: Path, writer: Callable[[Path], None]) -> None:
            box.advance(650.0)  # the save body outlasts the 600 s stall timeout
            real_write(path, writer)

        monkeypatch.setattr(ledger_store_module, "atomic_write_via", _slow_write)
        save_window_ledger(WindowLedger(), tmp_path, clock=clock)
        assert box.t >= 650.0
        assert wd.check_once() == ()  # the completion beat refreshed liveness
        assert hung == []

    def test_ledger_save_beats_at_entry_and_completion(self, tmp_path: Path) -> None:
        """The wiring pin: one entry beat + one completion beat per save."""
        beats, beat = _counting_beat()
        register_progress_beat(beat)
        save_window_ledger(WindowLedger(), tmp_path)
        assert len(beats) == 2


class TestKeyedChannelRouting:
    """The heal/save beats ride the run's KEYED channel (DEC-PULL-BEAT-PERRUN-001)."""

    def test_heal_and_save_beats_arrive_on_the_run_keyed_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ambient-pull-id routing pin — the production `pull_jobs` pattern.

        `BackgroundPullJob._run` registers the run's beat KEYED on its own
        pull id inside `pull_id_scope` (pull_jobs.py — DEC-PULL-BEAT-PERRUN-001
        #1537), and the L2 heal + drain save run ON that worker thread, so
        their `emit_progress_beat` calls resolve the ambient id and route to
        the RUN's keyed channel — never the LEGACY fallback. The legacy-channel
        CANARY below must stay silent: a future refactor that moves the
        heal/save off the pull-worker thread (dropping the ambient id) would
        route its beats outward to the canary (or drop them on a keyed-only
        registry), starving the keyed watchdog — and FAIL this test.
        """
        from brutex.observability.correlation import pull_id_scope

        # EXPLICIT flag-OFF since the W-5 default flip: the exact keyed beat
        # COUNT pins the PYTHON scanner's beat wiring (the crate arm's beat
        # replay rides the SAME keyed channel but with its own cadence —
        # pinned by the waved differentials).
        monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        _write_engine_bar(lake_root)
        keyed_beats, keyed_beat = _counting_beat()
        legacy_beats, legacy_beat = _counting_beat()
        register_progress_beat(keyed_beat, pull_id="abc123")
        register_progress_beat(legacy_beat)  # the LEGACY canary — must stay silent
        with pull_id_scope("abc123"):
            report = heal_after_pull(
                lake_root=lake_root,
                targets=[("NSE", "INDEX", "NIFTY")],
                start=date(2025, 9, 1),
                end=date(2025, 9, 2),
                scope="test",
            )
            save_window_ledger(WindowLedger(), tmp_path)
        assert report.findings == 0
        # Heal: 1 target + 1 scanned file; save: entry + completion — ALL on
        # the run's keyed channel via the worker's ambient pull id.
        assert len(keyed_beats) == 4
        # An exact keyed hit NEVER falls back outward to the legacy channel
        # (the anti-masking invariant): the canary saw ZERO beats.
        assert legacy_beats == []


class TestRealJobKeyedBinding:
    """Item-4 pin: the REAL `BackgroundPullJob` path binds the heal/save beats.

    The synthetic `pull_id_scope` test above proves ROUTING; this drives the
    production thread/scope binding end-to-end — `BackgroundPullJob._run`
    opens `pull_id_scope(run_pull_id)` and registers its watchdog beat KEYED
    on that run id (pull_jobs.py, DEC-PULL-BEAT-PERRUN-001 #1537), and the
    drain-stage `save_window_ledger` + `heal_after_pull` beats emitted INSIDE
    `work` must land on THAT keyed channel: with a fake clock advanced past
    the 600 s stall timeout across both stages, `check_once()` stays HEALTHY
    (beats landed) and a LEGACY-channel canary stays silent (keyed routing,
    not fallback). Reverting the heal/save beat wiring fails this test.
    """

    def test_job_run_binds_heal_and_save_beats_to_its_keyed_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_jobs as pull_jobs_module
        from brutex.dashboard.pull_jobs import BackgroundPullJob
        from brutex.dashboard.pull_progress import STATE_DONE, PullProgress
        from brutex.dashboard.pull_stall import PullWatchdog

        # No cross-process account flock for the job-level test (the
        # test_pipeline_stall_heartbeat.py discipline).
        monkeypatch.setattr(pull_jobs_module, "_default_lake_root_resolver", lambda: None)

        box = _ClockBox()

        def clock() -> float:
            return box.t

        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        _write_engine_bar(lake_root)

        legacy_beats, legacy_beat = _counting_beat()
        register_progress_beat(legacy_beat)  # the LEGACY canary — must stay silent

        built: list[PullWatchdog] = []

        def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            wd = PullWatchdog(
                on_hung=on_hung, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
            )
            built.append(wd)
            return wd

        job = BackgroundPullJob(
            progress=PullProgress(clock=clock),
            spawn=lambda fn: fn(),  # synchronous worker — the test IS the worker thread
            watchdog_factory=factory,
        )

        def work(on_progress: object) -> None:
            wd = built[0]
            # The last fetched-window heartbeat, then two silent drain stages
            # each longer than the 600 s timeout — covered ONLY by the
            # heal/save production beat seams routing onto the job's keyed
            # channel via the worker's ambient pull id.
            box.advance(650.0)
            save_window_ledger(WindowLedger(), tmp_path, clock=clock)
            assert wd.check_once() == (), "the save beats did not reach the keyed channel"
            box.advance(650.0)
            report = heal_after_pull(
                lake_root=lake_root,
                targets=[("NSE", "INDEX", "NIFTY")],
                start=date(2025, 9, 1),
                end=date(2025, 9, 2),
                scope="test",
            )
            assert report.findings == 0
            assert wd.check_once() == (), "the heal beats did not reach the keyed channel"

        assert job.start(work, total=1) is True
        assert job.snapshot().state == STATE_DONE
        # Keyed routing, never the legacy fallback: the canary saw ZERO beats.
        assert legacy_beats == []
