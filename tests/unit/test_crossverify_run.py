"""Unit tests — :mod:`brutex.crossverify.run` (step machine + default seams)."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.calendars.nse import HolidayDataUnavailableError, SessionType, TradingSession
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key
from brutex.crossverify.config import CrossverifyConfig
from brutex.crossverify.day_export import SymbolExport
from brutex.crossverify.gate_seam import CompletenessGateUnavailable
from brutex.crossverify.journal import (
    DayJournal,
    load_journal,
    staging_dir,
    staging_path,
    staging_path_for_key,
)
from brutex.crossverify.prebuild_leg import DeriveOutcome
from brutex.crossverify.pull_leg import PullLegReport
from brutex.crossverify.run import (
    MOVERS_WARM_NOTE,
    CrossverifyReport,
    CrossverifySeams,
    _default_alerter,
    _default_emit_pull_halt,
    _settle_retry_affected,
    default_seams,
    run_crossverify,
)
from brutex.crossverify.s3_publish import S3BackendUnavailable
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt
from brutex.data.models import CandleBar

from tests.fakes.fake_completeness_gate import FakeVerdict, ScriptedClassifier
from tests.fakes.fake_s3_client import FakeS3Client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from contextlib import ExitStack

_DAY = date(2026, 7, 10)
_T0 = datetime(2026, 7, 10, 16, 5, tzinfo=UTC)
_SESSION = TradingSession(
    date=_DAY,
    session_type=SessionType.REGULAR,
    open_time_ist=time(9, 15),
    close_time_ist=time(15, 30),
)
_MUHURAT = TradingSession(
    date=_DAY,
    session_type=SessionType.MUHURAT,
    open_time_ist=time(18, 0),
    close_time_ist=time(19, 0),
)


def _identity(segment: str, symbol: str, exch: str = "NSE") -> CanonicalIdentity:
    identity = canonical_identity(exchange=exch, segment=segment, lake_symbol=symbol)
    assert identity is not None
    return identity


_NIFTY = _identity("INDEX", "NIFTY")
_RELIANCE = _identity("CASH", "RELIANCE")
_FUT = _identity("FNO", "NSE-NIFTY-30Jul26-FUT")
_TARGETS = [_NIFTY, _RELIANCE, _FUT]


def _bar(identity: CanonicalIdentity) -> CandleBar:
    price = Decimal("100.00")
    return CandleBar(
        timestamp=datetime(2026, 7, 10, 3, 45, tzinfo=UTC),  # 09:15 IST
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
        open_interest=100 if identity.lake_segment == "FNO" else None,
    )


def _reader(*, exchange: str, segment: str, symbol: str, day: date) -> list[CandleBar]:
    _ = day
    identity = _identity(segment, symbol, exchange)
    return [_bar(identity)]


class _Clock:
    """A scripted now_ist(): pops the next instant, repeats the last forever."""

    def __init__(self, times: Sequence[datetime] = (_T0,)) -> None:
        self._times = list(times)

    def __call__(self) -> datetime:
        if len(self._times) > 1:
            return self._times.pop(0)
        return self._times[0]


@dataclass
class _Alerter:
    summaries: list[CrossverifyReport] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    raise_alerts: bool = False

    def crossverify_summary(self, *, run_id: str, report: CrossverifyReport) -> bool:
        assert run_id == report.run_id
        if self.raise_alerts:
            raise RuntimeError("alert transport down")
        self.summaries.append(report)
        return True

    def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
        assert run_id
        if self.raise_alerts:
            raise RuntimeError("alert transport down")
        self.refusals.append(reason)
        return True


def _clean_pull() -> PullLegReport:
    return PullLegReport(
        spot_ok=True,
        metadata_ok=True,
        futures_ok=True,
        halted=False,
        halt_reason=None,
        fut_contracts=(),
    )


@dataclass
class _Harness:
    seams: CrossverifySeams
    config: CrossverifyConfig
    client: FakeS3Client
    alerter: _Alerter
    sleeps: list[float]
    settle_calls: list[list[CanonicalIdentity]]
    prebuild_calls: list[list[CanonicalIdentity]]
    halts: list[BaseException]

    def run(self) -> CrossverifyReport:
        return run_crossverify(self.config, self.seams)


def _harness(
    lake: Path,
    *,
    targets: list[CanonicalIdentity] | None = None,
    classifier: ScriptedClassifier | None = None,
    clock: _Clock | None = None,
    acquire: Callable[[ExitStack], bool] | None = None,
    session: TradingSession | None = _SESSION,
    session_raises: bool = False,
    gate_absent: bool = False,
    run_pull: Callable[[], PullLegReport] | None = None,
    run_prebuild: Callable[[Sequence[CanonicalIdentity]], DeriveOutcome] | None = None,
    s3_factory: Callable[[], Any] | None = None,
    market_hours_blocked: bool = False,
    on_settle_retry: Callable[[], None] | None = None,
    **config_kw: Any,
) -> _Harness:
    client = FakeS3Client()
    alerter = _Alerter()
    sleeps: list[float] = []
    settle_calls: list[list[CanonicalIdentity]] = []
    prebuild_calls: list[list[CanonicalIdentity]] = []
    halts: list[BaseException] = []
    the_classifier = classifier if classifier is not None else ScriptedClassifier()

    def _session_for(day: date) -> TradingSession | None:
        assert day == _DAY
        if session_raises:
            raise HolidayDataUnavailableError("2027 coverage absent")
        return session

    def _classifier_factory() -> ScriptedClassifier:
        if gate_absent:
            raise CompletenessGateUnavailable("gate module absent")
        return the_classifier

    def _enumerate(
        day: date, pull_report: PullLegReport | None
    ) -> tuple[list[CanonicalIdentity], int]:
        _ = (day, pull_report)
        return list(_TARGETS if targets is None else targets), 2

    def _settle(affected: Sequence[CanonicalIdentity]) -> PullLegReport:
        settle_calls.append(list(affected))
        if on_settle_retry is not None:
            on_settle_retry()
        return _clean_pull()

    def _prebuild(coords: Sequence[CanonicalIdentity]) -> DeriveOutcome:
        prebuild_calls.append(list(coords))
        return DeriveOutcome(derived=len(coords), failed=0, failed_names=())

    config_defaults: dict[str, Any] = {
        "lake_root": lake,
        "day": _DAY,
        "bucket": "tv-prod-cold",
        "key_prefix": "crossverify/groww",
        "region": "ap-south-1",
        "enabled": True,
        "publish": True,
        "pull_lookback_days": 7,
        "lock_wait_s": 100.0,
        "lock_retry_s": 1.0,
        "deadline_s": 100_000.0,
        "settle_retry_rounds": 1,
        "settle_retry_delay_s": 5.0,
        "catchup_days": 5,
        "dry_run": False,
        "skip_pull": False,
    }
    config_defaults.update(config_kw)
    config = CrossverifyConfig(**config_defaults)
    seams = CrossverifySeams(
        acquire_locks=acquire if acquire is not None else (lambda stack: True),
        session_for=_session_for,
        classifier_factory=_classifier_factory,
        reader=_reader,
        enumerate_targets=_enumerate,
        run_pull=run_pull if run_pull is not None else _clean_pull,
        run_settle_retry=_settle,
        run_prebuild=run_prebuild if run_prebuild is not None else _prebuild,
        s3_client_factory=s3_factory if s3_factory is not None else (lambda: client),
        alerter=alerter,
        emit_pull_halt=halts.append,
        market_hours_blocked=lambda: market_hours_blocked,
        now_ist=clock if clock is not None else _Clock(),
        sleeper=sleeps.append,
    )
    return _Harness(
        seams=seams,
        config=config,
        client=client,
        alerter=alerter,
        sleeps=sleeps,
        settle_calls=settle_calls,
        prebuild_calls=prebuild_calls,
        halts=halts,
    )


class TestGateSteps:
    def test_disabled(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, enabled=False)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("disabled", 0)
        assert h.alerter.summaries == [] and h.alerter.refusals == []

    def test_calendar_abstain_refused_exit_2(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, session_raises=True)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("refused", 2)
        assert "coverage exhausted" in h.alerter.refusals[0]

    def test_non_trading_day_skips(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, session=None)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("skipped_non_trading", 0)
        assert h.alerter.summaries == []

    def test_muhurat_whole_run_skip_no_files(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, session=_MUHURAT)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("skipped_muhurat", 0)
        assert not (tmp_path / ".crossverify").exists()
        assert h.client.puts == []

    def test_gate_absent_fail_closed_exit_1(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, gate_absent=True)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("refused", 1)
        assert "gate module absent" in h.alerter.refusals[0]

    def test_market_hours_start_gate_defers(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, market_hours_blocked=True)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("deferred_market_hours", 0)
        assert h.client.puts == []

    def test_market_hours_gate_skipped_for_vendor_free_runs(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, market_hours_blocked=True, skip_pull=True)
        report = h.run()
        assert report.verdict == "published"  # the gate protects VENDOR calls only

    def test_refused_alert_raising_never_kills_run(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, gate_absent=True)
        h.alerter.raise_alerts = True
        report = h.run()
        assert (report.verdict, report.exit_code) == ("refused", 1)

    def test_summary_alert_raising_never_kills_run(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        h.alerter.raise_alerts = True
        report = h.run()
        assert report.verdict == "published"


class TestLocks:
    def test_lock_acquired_first_try_no_sleep(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        assert h.run().verdict == "published"
        assert h.sleeps == []

    def test_lock_retry_then_acquire(self, tmp_path: Path) -> None:
        attempts: list[int] = []

        def _acquire(stack: ExitStack) -> bool:
            attempts.append(1)
            return len(attempts) >= 3

        h = _harness(tmp_path, acquire=_acquire, settle_retry_rounds=0)
        report = h.run()
        assert report.verdict == "published"
        assert len(attempts) == 3
        assert h.sleeps[:2] == [1.0, 1.0]  # lock_retry_s between attempts

    def test_lock_budget_exhausted_refused_exit_1(self, tmp_path: Path) -> None:
        # Clock: start, wait_started, then past the budget on the re-check.
        clock = _Clock([_T0, _T0, _T0 + timedelta(seconds=101)])
        h = _harness(tmp_path, acquire=lambda stack: False, clock=clock)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("refused", 1)
        assert "no preemption" in h.alerter.refusals[0]

    def test_locks_released_before_publish(self, tmp_path: Path) -> None:
        events: list[str] = []

        class _OrderedClient(FakeS3Client):
            def put_object(self, **kwargs: Any) -> None:
                events.append("put")
                super().put_object(**kwargs)

        client = _OrderedClient()

        @contextmanager
        def _lock_cm() -> Iterator[None]:
            yield
            events.append("locks_released")

        def _acquire(stack: ExitStack) -> bool:
            stack.enter_context(_lock_cm())
            return True

        h = _harness(tmp_path, acquire=_acquire, s3_factory=lambda: client)
        report = h.run()
        assert report.verdict == "published"
        assert events.index("locks_released") < events.index("put")


class TestPullStep:
    def test_pull_skipped_on_skip_pull(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def _pull() -> PullLegReport:
            calls.append(1)
            return _clean_pull()

        h = _harness(tmp_path, run_pull=_pull, skip_pull=True)
        assert h.run().verdict == "published"
        assert calls == []

    def test_market_hours_halt_raise_defers(self, tmp_path: Path) -> None:
        def _pull() -> PullLegReport:
            raise MarketHoursHalt("window reached")

        h = _harness(tmp_path, run_pull=_pull)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("deferred_market_hours", 0)

    def test_halted_pull_report_defers(self, tmp_path: Path) -> None:
        def _pull() -> PullLegReport:
            return replace(_clean_pull(), halted=True, halt_reason="market_hours")

        h = _harness(tmp_path, run_pull=_pull)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("deferred_market_hours", 0)
        assert "market_hours" in report.detail

    def test_pull_halt_is_suspect_exit_2_with_halt_alert(self, tmp_path: Path) -> None:
        def _pull() -> PullLegReport:
            raise LakeWriteHalt("disk_full")

        h = _harness(tmp_path, run_pull=_pull)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("suspect", 2)
        assert len(h.halts) == 1
        assert isinstance(h.halts[0], LakeWriteHalt)
        assert len(h.alerter.summaries) == 1  # summary CRITICAL still fires once

    def test_non_halt_bug_propagates(self, tmp_path: Path) -> None:
        def _pull() -> PullLegReport:
            raise RuntimeError("a genuine code bug")

        h = _harness(tmp_path, run_pull=_pull)
        with pytest.raises(RuntimeError, match="genuine code bug"):
            h.run()


class TestExportPublishFlow:
    def test_full_happy_path(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("published", 0)
        assert report.published == 3
        assert (report.withheld, report.blocked, report.nothing) == (0, 0, 0)
        assert report.excluded == 2  # threaded from enumerate_targets
        assert report.derived == 3
        assert report.movers_warm == MOVERS_WARM_NOTE
        assert report.run_id.startswith("cv-20260710-")
        # S3 objects == staged bytes, keys per the ONE key law.
        expected_keys = {
            s3_key(prefix="crossverify/groww", day=_DAY, identity=identity) for identity in _TARGETS
        }
        assert set(h.client.objects) == expected_keys
        # Staging dir deleted after full publish; journal retained as done-record.
        assert not staging_dir(tmp_path, _DAY).exists()
        journal = load_journal(tmp_path, _DAY)
        assert journal is not None
        assert journal.pending_keys() == ()
        assert len(h.alerter.summaries) == 1  # exactly ONE summary

    def test_idempotent_second_run_zero_puts(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        assert h.run().verdict == "published"
        puts_after_first = len(h.client.puts)
        h2 = _harness(tmp_path, s3_factory=lambda: h.client)
        report = h2.run()
        assert report.verdict == "published"
        assert report.published == 0
        assert report.skipped_already == 3
        assert len(h.client.puts) == puts_after_first  # ZERO new puts

    def test_blocked_index_exit_2_others_still_publish(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"index_spot": FakeVerdict(reason="no_today_bars", incomplete=False)}
        )
        h = _harness(tmp_path, classifier=classifier, settle_retry_rounds=0)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("blocked", 2)
        assert report.blocked == 1
        assert report.published == 2  # the complete files still land
        assert len(h.alerter.summaries) == 1

    def test_withheld_partial_exit_2_never_publishes_partial(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"fut": FakeVerdict(reason="volume_all_zero", incomplete=True)}
        )
        h = _harness(tmp_path, classifier=classifier, settle_retry_rounds=0)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("withheld_partial", 2)
        assert report.withheld == 1
        fut_key = s3_key(prefix="crossverify/groww", day=_DAY, identity=_FUT)
        # NEVER-PUBLISH-PARTIAL (structural): the withheld key reaches neither
        # the staging tree, nor the journal, nor a single S3 put.
        assert not staging_path(tmp_path, _DAY, _FUT).exists()
        assert all(key != fut_key for _b, key, _c in h.client.puts)
        journal = load_journal(tmp_path, _DAY)
        assert journal is not None
        assert fut_key not in journal.entries
        assert journal.withheld == {_FUT.canonical_symbol: "volume_all_zero"}

    def test_nothing_to_do(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(default=FakeVerdict(reason="no_today_bars"))
        h = _harness(
            tmp_path,
            targets=[_RELIANCE, _FUT],
            classifier=classifier,
            settle_retry_rounds=0,
        )
        report = h.run()
        assert (report.verdict, report.exit_code) == ("nothing_to_do", 0)
        assert report.nothing == 2
        assert h.client.puts == []

    def test_publish_disabled_stages_only(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, publish=False)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("staged_only", 0)
        assert h.client.puts == []
        assert staging_path(tmp_path, _DAY, _NIFTY).exists()
        journal = load_journal(tmp_path, _DAY)
        assert journal is not None
        assert len(journal.pending_keys()) == 3  # armed for the next publish/recovery

    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        locks: list[int] = []

        def _acquire(stack: ExitStack) -> bool:
            locks.append(1)
            return True

        h = _harness(tmp_path, acquire=_acquire, dry_run=True)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("staged_only", 0)
        assert "dry-run" in report.detail
        assert locks == []  # no locks
        assert h.prebuild_calls == []  # no derives
        assert h.client.puts == []  # no S3
        assert not (tmp_path / ".crossverify").exists()  # nothing staged

    def test_publish_failure_exit_2(self, tmp_path: Path) -> None:
        client = FakeS3Client(
            fail_times={s3_key(prefix="crossverify/groww", day=_DAY, identity=_NIFTY): 99}
        )
        h = _harness(tmp_path, s3_factory=lambda: client)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("publish_failed", 2)
        assert report.publish_failed == 1
        assert report.published == 2
        # Failed key stays pending for tomorrow's recovery scan.
        journal = load_journal(tmp_path, _DAY)
        assert journal is not None
        assert len(journal.pending_keys()) == 1
        assert staging_dir(tmp_path, _DAY).exists()  # staged bytes retained

    def test_nonretryable_abort_named(self, tmp_path: Path) -> None:
        client = FakeS3Client(
            nonretryable=frozenset({s3_key(prefix="crossverify/groww", day=_DAY, identity=_FUT)})
        )
        h = _harness(tmp_path, s3_factory=lambda: client)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("publish_failed", 2)
        assert "nonretryable" in report.detail

    def test_boto3_missing_at_publish_exit_2(self, tmp_path: Path) -> None:
        def _factory() -> Any:
            raise S3BackendUnavailable('pip install -e ".[crossverify]"')

        h = _harness(tmp_path, s3_factory=_factory)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("publish_failed", 2)
        assert "crossverify" in report.detail
        assert report.publish_failed == 3

    def test_prebuild_coordinates_are_publish_plus_withheld(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={
                "fut": FakeVerdict(reason="volume_all_zero", incomplete=True),
                "cash_spot": FakeVerdict(reason="no_today_bars"),
            }
        )
        h = _harness(tmp_path, classifier=classifier, settle_retry_rounds=0)
        h.run()
        assert h.prebuild_calls == [[_NIFTY, _FUT]]  # "nothing" has no bars to derive

    def test_prebuild_halt_is_suspect(self, tmp_path: Path) -> None:
        def _prebuild(coords: Sequence[CanonicalIdentity]) -> DeriveOutcome:
            raise LakeWriteHalt("disk_full")

        h = _harness(tmp_path, run_prebuild=_prebuild)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("suspect", 2)
        assert len(h.halts) == 1

    def test_staging_halt_is_suspect(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data import atomic_io

        def _halting_write(dest: Path, write_to: Any) -> None:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(atomic_io, "atomic_write_via", _halting_write)
        h = _harness(tmp_path)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("suspect", 2)
        assert "staging halt" in report.detail

    def test_corrupt_existing_journal_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / ".crossverify" / f"{_DAY.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        h = _harness(tmp_path)
        assert h.run().verdict == "published"  # re-render converges


class TestSettleRetryBelt:
    def test_affected_set_law(self) -> None:
        def _export(identity: CanonicalIdentity, reason: str) -> SymbolExport:
            return SymbolExport(
                identity=identity,
                day=_DAY,
                disposition="withheld",
                reason=reason,
                csv_bytes=None,
                sha256=None,
                row_count=0,
            )

        exports = {
            "NIFTY": _export(_NIFTY, "count_short"),
            "FUT": _export(_FUT, "oi_null_settling"),
            "R1": _export(_RELIANCE, "volume_all_zero"),  # cash — never retried
            "R2": replace(_export(_RELIANCE, "complete"), disposition="publish"),
        }
        assert _settle_retry_affected(exports) == [_NIFTY, _FUT]

    def test_retry_flips_withheld_to_published(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"fut": FakeVerdict(reason="oi_null_settling", incomplete=True)}
        )

        def _settled() -> None:
            classifier.by_class.pop("fut")  # the re-pull settled the OI

        h = _harness(tmp_path, classifier=classifier, on_settle_retry=_settled)
        report = h.run()
        assert report.verdict == "published"
        assert report.published == 3
        assert h.settle_calls == [[_FUT]]
        assert h.sleeps == [5.0]  # settle_retry_delay_s before the re-pull

    def test_still_short_after_rounds_stays_withheld(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"index_spot": FakeVerdict(reason="count_short", incomplete=True)}
        )
        h = _harness(tmp_path, classifier=classifier, settle_retry_rounds=2)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("withheld_partial", 2)
        assert len(h.settle_calls) == 2  # bounded rounds

    def test_no_retry_on_skip_pull(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"fut": FakeVerdict(reason="oi_null_settling", incomplete=True)}
        )
        h = _harness(tmp_path, classifier=classifier, skip_pull=True)
        report = h.run()
        assert report.verdict == "withheld_partial"
        assert h.settle_calls == []

    def test_deadline_breaks_retry_loop(self, tmp_path: Path) -> None:
        classifier = ScriptedClassifier(
            by_class={"fut": FakeVerdict(reason="oi_null_settling", incomplete=True)}
        )
        # started, wait_started, after-pull ok, after-export ok, G2 check OVER.
        clock = _Clock([_T0, _T0, _T0, _T0, _T0 + timedelta(seconds=200_000)])
        h = _harness(tmp_path, classifier=classifier, clock=clock)
        report = h.run()
        assert h.settle_calls == []  # the belt never fired
        assert report.verdict == "suspect"  # the NEXT boundary check trips


class TestDeadline:
    def _clock_tripping_at(self, checks_before_trip: int) -> _Clock:
        # Calls: started, wait_started, then one per deadline check.
        times = [_T0, _T0] + [_T0] * checks_before_trip + [_T0 + timedelta(seconds=200_000)]
        return _Clock(times)

    def test_deadline_after_pull(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, clock=self._clock_tripping_at(0))
        report = h.run()
        assert (report.verdict, report.exit_code) == ("suspect", 2)
        assert "after the pull leg" in report.detail

    def test_deadline_after_export(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, clock=self._clock_tripping_at(1), settle_retry_rounds=0)
        report = h.run()
        assert "after the export pass" in report.detail

    def test_deadline_after_prebuild(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, clock=self._clock_tripping_at(2), settle_retry_rounds=0)
        report = h.run()
        assert "after the prebuild leg" in report.detail

    def test_deadline_before_publish(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, clock=self._clock_tripping_at(3), settle_retry_rounds=0)
        report = h.run()
        assert "before publish" in report.detail
        assert h.client.puts == []  # nothing published past the wall


class TestRecoveryScan:
    def _stage_prior(self, lake: Path, offset_days: int, key_tail: str) -> str:
        prior_day = _DAY - timedelta(days=offset_days)
        journal = DayJournal(day=prior_day, run_id="cv-prior")
        key = f"crossverify/groww/{prior_day.isoformat()}/{key_tail}"
        dest = staging_path_for_key(lake, prior_day, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = b"symbol,timestamp_ist,open,high,low,close,volume,oi\nX,...\n"
        dest.write_bytes(body)
        journal.mark_staged(key, hashlib.sha256(body).hexdigest(), len(body))
        journal.save(lake)
        return key

    def test_prior_pending_republished_without_regate(self, tmp_path: Path) -> None:
        key = self._stage_prior(tmp_path, 2, "IDX_I/NIFTY.csv")
        classifier = ScriptedClassifier()
        h = _harness(tmp_path, classifier=classifier)
        report = h.run()
        assert report.recovered_prior == 1
        assert key in h.client.objects
        # No re-gate for the prior day: the classifier saw only TODAY's targets.
        assert len(classifier.calls) == len(_TARGETS)
        assert report.verdict == "published"

    def test_prior_still_failing_counts_publish_failed(self, tmp_path: Path) -> None:
        key = self._stage_prior(tmp_path, 1, "IDX_I/BANKNIFTY.csv")
        client = FakeS3Client(nonretryable=frozenset({key}))
        h = _harness(tmp_path, s3_factory=lambda: client)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("publish_failed", 2)
        assert report.recovered_prior == 0
        assert report.publish_failed >= 1

    def test_boto3_missing_at_recovery_exit_2(self, tmp_path: Path) -> None:
        self._stage_prior(tmp_path, 1, "IDX_I/NIFTY.csv")

        def _factory() -> Any:
            raise S3BackendUnavailable("boto3 missing")

        h = _harness(tmp_path, s3_factory=_factory)
        report = h.run()
        assert (report.verdict, report.exit_code) == ("publish_failed", 2)

    def test_recovery_skipped_when_publish_off(self, tmp_path: Path) -> None:
        self._stage_prior(tmp_path, 1, "IDX_I/NIFTY.csv")
        h = _harness(tmp_path, publish=False)
        report = h.run()
        assert report.recovered_prior == 0
        assert h.client.puts == []


class TestSummaryLaw:
    @pytest.mark.parametrize(
        ("kwargs", "expect_summary"),
        [
            ({"enabled": False}, 0),
            ({"session": None}, 0),
            ({"session": _MUHURAT}, 0),
            ({"market_hours_blocked": True}, 0),
            ({}, 1),
            ({"publish": False}, 1),
            ({"dry_run": True}, 1),
        ],
    )
    def test_summary_emission(
        self, tmp_path: Path, kwargs: dict[str, Any], expect_summary: int
    ) -> None:
        h = _harness(tmp_path, **kwargs)
        h.run()
        assert len(h.alerter.summaries) == expect_summary


class TestJournalSaveCadence:
    def test_staging_saves_every_25(self, tmp_path: Path) -> None:
        many = [_identity("CASH", f"SYM{i:03d}") for i in range(26)]
        h = _harness(tmp_path, targets=many, publish=False, settle_retry_rounds=0)
        report = h.run()
        assert report.verdict == "staged_only"
        journal = load_journal(tmp_path, _DAY)
        assert journal is not None
        assert len(journal.entries) == 26  # the mid-run save at 25 + final save


class TestDefaultAlerter:
    def test_missing_helper_returns_false(self) -> None:
        alerter = _default_alerter()
        report = _harness_report_stub()
        assert alerter.crossverify_summary(run_id="cv-x", report=report) is False
        assert alerter.crossverify_refused(run_id="cv-x", reason="r") is False

    def test_present_helper_is_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.notifications import sweep_alerts

        calls: list[dict[str, Any]] = []

        def _summary(**kwargs: Any) -> bool:
            calls.append(kwargs)
            return True

        monkeypatch.setattr(sweep_alerts, "emit_crossverify_summary_alert", _summary, raising=False)
        alerter = _default_alerter()
        report = _harness_report_stub()
        assert alerter.crossverify_summary(run_id="cv-x", report=report) is True
        assert calls == [{"run_id": "cv-x", "report": report}]

    def test_raising_helper_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.notifications import sweep_alerts

        def _boom(**kwargs: Any) -> bool:
            raise RuntimeError("transport down")

        monkeypatch.setattr(sweep_alerts, "emit_crossverify_refused_alert", _boom, raising=False)
        alerter = _default_alerter()
        assert alerter.crossverify_refused(run_id="cv-x", reason="r") is False


def _harness_report_stub() -> CrossverifyReport:
    return CrossverifyReport(
        day=_DAY,
        run_id="cv-x",
        verdict="published",
        published=1,
        skipped_already=0,
        withheld=0,
        blocked=0,
        nothing=0,
        excluded=0,
        publish_failed=0,
        recovered_prior=0,
        derived=0,
        derive_failed=0,
        movers_warm=MOVERS_WARM_NOTE,
        detail="",
        exit_code=0,
    )


class TestDefaultEmitPullHalt:
    def test_exception_routes_to_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.notifications import sweep_alerts

        seen: list[Exception] = []
        monkeypatch.setattr(sweep_alerts, "emit_pull_halt_alert", seen.append)
        exc = LakeWriteHalt("disk_full")
        assert _default_emit_pull_halt(exc) is True
        assert seen == [exc]

    def test_non_exception_base_exception_skipped(self) -> None:
        assert _default_emit_pull_halt(KeyboardInterrupt()) is False

    def test_raising_helper_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.notifications import sweep_alerts

        def _boom(exc: Exception) -> None:
            raise RuntimeError("transport down")

        monkeypatch.setattr(sweep_alerts, "emit_pull_halt_alert", _boom)
        assert _default_emit_pull_halt(LakeWriteHalt("disk_full")) is False

    def test_real_helper_is_noop_when_telegram_disabled(self) -> None:
        # The real emit_pull_halt_alert is a silent best-effort no-op with
        # Telegram disabled (the default test env) — the wrapper reports True.
        assert _default_emit_pull_halt(LakeWriteHalt("disk_full")) is True


def _make_instrument(
    symbol: str,
    *,
    itype: Any = None,
    exch: Any = None,
) -> Any:
    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    itype = itype if itype is not None else InstrumentType.EQ
    exch = exch if exch is not None else Exchange.NSE
    segment = Segment.INDEX if itype is InstrumentType.IDX else Segment.CASH
    return Instrument(
        exch_id=exch,
        security_id=abs(hash((symbol, str(exch)))) % 10_000_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=itype,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1 if itype is InstrumentType.EQ else 0,
        tick_size=Decimal("0.05") if itype is InstrumentType.EQ else Decimal(0),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _seed_store(lake: Path, instruments: list[Any]) -> None:
    from brutex.data.instrument_store import DEFAULT_DB_NAME, InstrumentStore

    store = InstrumentStore.open(lake / DEFAULT_DB_NAME)
    try:
        store.upsert_many(
            instruments,
            snapshot_date=_DAY,
            run_id="test-run",
            csv_source_sha256="0" * 64,
        )
    finally:
        store.close()


def _contract_row(symbol: str, *, expiry: date, side: str = "FUT") -> Any:
    from brutex.data.registry_store import ContractRow

    return ContractRow(
        exch="NSE",
        underlying="NIFTY",
        expiry_date=expiry,
        contract_symbol=symbol,
        strike=None,
        side=side,
        candle_status="pending",
        last_run_id="run",
        fetched_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _seams_config(lake: Path, **kw: Any) -> CrossverifyConfig:
    defaults: dict[str, Any] = {
        "lake_root": lake,
        "day": _DAY,
        "bucket": "tv-prod-cold",
        "key_prefix": "crossverify/groww",
        "region": "ap-south-1",
        "enabled": True,
        "publish": True,
        "pull_lookback_days": 7,
        "lock_wait_s": 5.0,
        "lock_retry_s": 0.1,
        "deadline_s": 100.0,
        "settle_retry_rounds": 1,
        "settle_retry_delay_s": 1.0,
        "catchup_days": 5,
        "dry_run": False,
        "skip_pull": False,
    }
    defaults.update(kw)
    return CrossverifyConfig(**defaults)


_GUARD_OFF = {"BRUTEX_MARKET_HOURS_GUARD": "0"}


class TestDefaultSeams:
    def test_default_bindings_shape(self, tmp_path: Path) -> None:
        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
        )
        # session_for IS the strict calendar resolver.
        session = seams.session_for(_DAY)
        assert session is not None and session.session_type is SessionType.REGULAR
        # The guard is disabled via env — deterministic False at any wall clock.
        assert seams.market_hours_blocked() is False
        assert seams.now_ist().tzinfo is not None
        import time as time_module

        assert seams.sleeper is time_module.sleep
        # The production reader binds the lake root (empty lake → no bars).
        assert seams.reader(exchange="NSE", segment="INDEX", symbol="NIFTY", day=_DAY) == []
        # classifier_factory is the fail-closed gate binding (module absent today).
        assert seams.classifier_factory is not None

    def test_default_s3_factory_raises_without_boto3(self, tmp_path: Path) -> None:
        import importlib.util

        seams = default_seams(
            _seams_config(tmp_path), env=_GUARD_OFF, account_key_resolver=lambda: None
        )
        if importlib.util.find_spec("boto3") is not None:  # pragma: no cover
            pytest.skip("boto3 installed — the missing-extra arm is not testable here")
        with pytest.raises(S3BackendUnavailable):
            seams.s3_client_factory()

    def test_injected_s3_factory_and_alerter_pass_through(self, tmp_path: Path) -> None:
        client = FakeS3Client()
        alerter = _Alerter()
        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            s3_client_factory=lambda: client,
            alerter=alerter,
        )
        assert seams.s3_client_factory() is client
        assert seams.alerter is alerter

    def test_dry_run_threads_no_market_closed(self, tmp_path: Path) -> None:
        # dry_run resolves NO market_closed predicate (no vendor calls to
        # gate) — a junk guard env therefore only fails the NON-dry build.
        junk = {"BRUTEX_MARKET_HOURS_GUARD": "banana"}
        default_seams(
            _seams_config(tmp_path, dry_run=True),
            env=junk,
            account_key_resolver=lambda: None,
        )
        with pytest.raises(ValueError, match="BRUTEX_MARKET_HOURS_GUARD"):
            default_seams(_seams_config(tmp_path), env=junk, account_key_resolver=lambda: None)

    def test_acquire_locks_success_and_release(self, tmp_path: Path) -> None:
        from contextlib import ExitStack as RealExitStack

        lease_entered: list[str] = []

        @contextmanager
        def _lease(lake: Path) -> Any:
            lease_entered.append(str(lake))
            yield

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            lease_factory=_lease,
        )
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        with RealExitStack() as stack:
            assert seams.acquire_locks(stack) is True
            assert lease_entered == [str(tmp_path)]
            # The lake flock is genuinely HELD — a peer acquire refuses.
            from brutex.adapters.groww.pull_singleflight import PullAlreadyRunningError

            with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
                pass  # pragma: no cover — never reached
        # Released on stack close — a fresh acquire succeeds.
        with pull_singleflight_lock(tmp_path):
            pass

    def test_acquire_locks_contention_returns_false(self, tmp_path: Path) -> None:
        from contextlib import ExitStack as RealExitStack

        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            lease_factory=lambda lake: contextmanager(lambda: iter([None]))(),
        )
        with pull_singleflight_lock(tmp_path, holder_note="peer"), RealExitStack() as stack:
            assert seams.acquire_locks(stack) is False

    def test_acquire_locks_takes_account_lock_when_key_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import ExitStack as RealExitStack

        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        @contextmanager
        def _lease(lake: Path) -> Any:
            yield

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: "test-api-key",
            lease_factory=_lease,
        )
        with RealExitStack() as stack:
            assert seams.acquire_locks(stack) is True
            lock_dir = tmp_path / "home" / ".brutex" / "locks"
            assert any(lock_dir.glob("pull_account*.flock"))

    def test_acquire_locks_non_refusal_error_propagates_and_releases(self, tmp_path: Path) -> None:
        from contextlib import ExitStack as RealExitStack

        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        def _broken_lease(lake: Path) -> Any:
            raise RuntimeError("lease backend broken")

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            lease_factory=_broken_lease,
        )
        with RealExitStack() as stack, pytest.raises(RuntimeError, match="lease backend broken"):
            seams.acquire_locks(stack)
        # The partially-taken flock was released on the error path.
        with pull_singleflight_lock(tmp_path):
            pass

    def test_default_account_key_resolver_degrades_to_none(self, tmp_path: Path) -> None:
        from contextlib import ExitStack as RealExitStack

        @contextmanager
        def _lease(lake: Path) -> Any:
            yield

        # No AWS creds in this environment → CredentialsError → the account
        # lock is skipped gracefully (one WARN) and the trio still acquires.
        seams = default_seams(_seams_config(tmp_path), env=_GUARD_OFF, lease_factory=_lease)
        with RealExitStack() as stack:
            assert seams.acquire_locks(stack) is True

    def test_enumerate_targets_store_registry_authoritative(self, tmp_path: Path) -> None:
        from brutex.models import InstrumentType

        _seed_store(
            tmp_path,
            [
                _make_instrument("NIFTY", itype=InstrumentType.IDX),
                _make_instrument("RELIANCE"),
                _make_instrument("M&M"),
                _make_instrument("A/B"),  # key-unsafe → excluded + counted
            ],
        )
        seams = default_seams(
            _seams_config(tmp_path), env=_GUARD_OFF, account_key_resolver=lambda: None
        )
        pull_report = replace(
            _clean_pull(),
            fut_contracts=(
                _contract_row("NSE-NIFTY-30Jul26-FUT", expiry=date(2026, 7, 30)),
                _contract_row("NSE-JUNK", expiry=date(2026, 7, 30)),  # unmappable
            ),
        )
        identities, excluded = seams.enumerate_targets(_DAY, pull_report)
        symbols = [identity.canonical_symbol for identity in identities]
        # Deterministic order: the 3 indices, stocks alpha, futures alpha.
        assert symbols == [
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
            "M&M",
            "RELIANCE",
            "NIFTY-2026-07-30-FUT",
        ]
        assert excluded == 2  # the junk stock + the unmappable FUT row

    def test_enumerate_registry_fallback_on_skip_pull(self, tmp_path: Path) -> None:
        from brutex.data.registry_store import RegistryStore

        _seed_store(tmp_path, [])
        with RegistryStore.open(tmp_path / "registry.duckdb"):
            pass  # create an EMPTY registry — the fallback arm reads it
        seams = default_seams(
            _seams_config(tmp_path), env=_GUARD_OFF, account_key_resolver=lambda: None
        )
        identities, excluded = seams.enumerate_targets(_DAY, None)
        assert [identity.canonical_symbol for identity in identities] == [
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
        ]
        assert excluded == 0

    def test_enumerate_no_registry_no_futures(self, tmp_path: Path) -> None:
        _seed_store(tmp_path, [])
        seams = default_seams(
            _seams_config(tmp_path), env=_GUARD_OFF, account_key_resolver=lambda: None
        )
        identities, _excluded = seams.enumerate_targets(_DAY, None)
        assert all(identity.lake_segment != "FNO" for identity in identities)

    def test_injected_store_opener_is_used(self, tmp_path: Path) -> None:
        opened: list[Path] = []
        from brutex.data.instrument_store import InstrumentStore

        def _opener(path: Path) -> Any:
            opened.append(path)
            return InstrumentStore.open(path)

        _seed_store(tmp_path, [])
        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            store_opener=_opener,
            account_key_resolver=lambda: None,
        )
        seams.enumerate_targets(_DAY, None)
        assert len(opened) == 1

    def test_run_pull_closure_binds_real_stores_and_spy_pullers(self, tmp_path: Path) -> None:
        _seed_store(tmp_path, [_make_instrument("RELIANCE")])
        calls: list[dict[str, Any]] = []

        def _spot(targets: Any, **kwargs: Any) -> object:
            calls.append({"leg": "spot", "targets": targets, **kwargs})
            return object()

        def _metadata(instruments: Any, **kwargs: Any) -> object:
            calls.append({"leg": "metadata", "instruments": instruments, **kwargs})
            return object()

        def _candles(rows: Any, **kwargs: Any) -> object:
            calls.append({"leg": "candles", "rows": rows, **kwargs})
            return object()

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            spot_puller=_spot,
            metadata_puller=_metadata,
            candle_puller=_candles,
        )
        report = seams.run_pull()
        assert report.halted is False
        assert [c["leg"] for c in calls] == ["spot", "metadata", "candles"]
        assert calls[0]["to_date"] == _DAY.isoformat()  # the same-day law rides through
        assert calls[0]["market_closed"] is not None  # resolved once at build

    def test_run_settle_retry_closure(self, tmp_path: Path) -> None:
        _seed_store(tmp_path, [])
        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            spot_puller=lambda *a, **kw: object(),
            candle_puller=lambda *a, **kw: object(),
        )
        report = seams.run_settle_retry([])
        assert report.halted is False

    def test_run_prebuild_closure(self, tmp_path: Path) -> None:
        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            materialize=lambda **kw: object(),
            derive_daily=lambda **kw: object(),
            read_window_factory=lambda **kw: lambda: None,
            write_daily_factory=lambda **kw: lambda bars: 0,
        )
        outcome = seams.run_prebuild([])
        assert outcome == DeriveOutcome(derived=0, failed=0, failed_names=())

    def test_injected_session_rate_shared(self, tmp_path: Path) -> None:
        _seed_store(tmp_path, [])
        rate = object()
        seen: list[Any] = []

        def _spot(targets: Any, **kwargs: Any) -> object:
            seen.append(kwargs["session_rate"])
            return object()

        seams = default_seams(
            _seams_config(tmp_path),
            env=_GUARD_OFF,
            account_key_resolver=lambda: None,
            session_rate=rate,
            spot_puller=_spot,
            metadata_puller=lambda *a, **kw: object(),
            candle_puller=lambda *a, **kw: object(),
        )
        seams.run_pull()
        assert seen == [rate]
