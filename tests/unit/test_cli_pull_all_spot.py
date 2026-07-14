# pyright: reportPrivateUsage=none
"""``brutex pull-all-spot`` (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md #1779).

The CLI arm of the scoped-ALL spot pull: eager fail-loud validation BEFORE any
lock, the ``--auto`` gate's cron-green deferrals, the lock/lease rail with the
auto-vs-manual contention exit split, the halt/deferral catches, the
crossverify tail invoked strictly AFTER the single-flight release (the
lock-ordering law, F/B-verified), the ONE shared SessionRateController, the
C §4 stdout shape, and the whole exit-code matrix incl. ``--strict-exit``.
Everything drives the REAL ``cmd_pull_all_spot`` body over injected seams —
never a network call, never a real vendor pull.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
from brutex.calendars.auto_pull_gate import PULL_ALL_SETTLE_ENV
from brutex.cli import (
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
    CliUserError,
    cmd_pull_all_spot,
    main,
)
from brutex.dashboard.pipeline_pull import PipelinePullReport
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_scope import (
    CrossverifyTailOutcome,
    spot_only_historical_report,
    spot_only_metadata_report,
)
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt

_TRADING_EVENING = datetime(2024, 1, 2, 18, 0)  # Tue, post-settle


def _spot_report(**overrides: Any) -> BulkSpotReport:
    base: dict[str, Any] = {
        "dry_run": False,
        "start_date": "2020-01-01",
        "end_date": "2026-07-12",
        "symbols": [SymbolPullResult(symbol="NIFTY", completed=7, failed=0, no_data=0)],
        "windows_skipped": 42,
    }
    base.update(overrides)
    return BulkSpotReport(**base)


def _report(**overrides: Any) -> PipelinePullReport:
    base: dict[str, Any] = {
        "spot": _spot_report(),
        "metadata": spot_only_metadata_report(),
        "historical": spot_only_historical_report(),
        "stopped_after": None,
        "note": (
            "pipeline complete — spot clean (7 pair(s)), metadata 0 expiries + 0 contracts, "
            "historical 0 bars over 0 contract(s), skipped 42 already-pulled window(s) "
            "(no vendor hit)"
        ),
        "prebuild_built": 3,
        "prebuild_total_bars": 999,
    }
    base.update(overrides)
    return PipelinePullReport(**base)


class _FakePipeline:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Harness:
    """One injected-seam bundle around ``cmd_pull_all_spot``."""

    def __init__(
        self,
        *,
        report: PipelinePullReport | None = None,
        executor_raises: BaseException | None = None,
        tail: CrossverifyTailOutcome | None = None,
    ) -> None:
        self.report = _report() if report is None else report
        self.executor_raises = executor_raises
        self.builder_kwargs: list[dict[str, Any]] = []
        self.executed: list[Any] = []
        self.tail_calls: list[str] = []
        self.hp = _FakePipeline()
        self._tail_outcome = (
            CrossverifyTailOutcome(
                status="published", human="published (published 4 · withheld 0 · failed 0)"
            )
            if tail is None
            else tail
        )

    def builder(self, **kwargs: Any) -> _FakePipeline:
        self.builder_kwargs.append(dict(kwargs))
        return self.hp

    def executor(self, hp: Any) -> PipelinePullReport:
        self.executed.append(hp)
        if self.executor_raises is not None:
            raise self.executor_raises
        return self.report

    def tail(self) -> CrossverifyTailOutcome:
        self.tail_calls.append("tail")
        return self._tail_outcome

    def run(self, lake: Path, **kwargs: Any) -> int:
        return cmd_pull_all_spot(
            lake_root=lake,
            pipeline_builder=self.builder,
            pipeline_executor=self.executor,
            crossverify_tail=self.tail,
            **kwargs,
        )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


# ── flags / help contract ────────────────────────────────────────────────


def test_help_pins_no_dates_promise(capsys: pytest.CaptureFixture[str]) -> None:
    """C 15: the --help text states the full-range promise; NO --start/--end."""
    with pytest.raises(SystemExit) as exc:
        main(["pull-all-spot", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--auto" in out
    assert "--settle-ist" in out
    assert "--strict-exit" in out
    assert "--start" not in out
    assert "--end" not in out


def test_missing_lake_root_is_user_error(tmp_path: Path) -> None:
    code = main(["pull-all-spot", "--lake-root", str(tmp_path / "nope")])
    assert code == EXIT_USER_ERROR


def test_junk_settle_flag_exits_user_error_before_lock(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[spy] Junk --settle-ist fails loud BEFORE any flock is touched."""
    locks: list[str] = []

    def lock_factory(*_a: Any, **_k: Any) -> Any:
        locks.append("lock")  # pragma: no cover — must never run
        return contextlib.nullcontext()

    with pytest.raises(CliUserError, match=PULL_ALL_SETTLE_ENV):
        cmd_pull_all_spot(lake_root=lake, settle_ist="junk", lock_factory=lock_factory)
    assert locks == []
    # And through main(): the standard one-liner + exit 1.
    code = main(["pull-all-spot", "--lake-root", str(lake), "--settle-ist", "25:99"])
    assert code == EXIT_USER_ERROR
    assert PULL_ALL_SETTLE_ENV in capsys.readouterr().err


def test_junk_market_hours_env_exits_user_error(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "junk")
    with pytest.raises(CliUserError, match="BRUTEX_MARKET_HOURS_GUARD"):
        cmd_pull_all_spot(lake_root=lake)


def test_junk_strict_gate_env_exits_user_error_before_lock(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[refuter-r1 r2 MINOR-3, spy] junk ``BRUTEX_PIPELINE_STRICT_GATE`` is a
    clean exit-1 refusal naming the knob, validated EAGERLY before any
    flock/builder work (the cmd_nightly precedent) — previously an uncaught
    raw ValueError traceback out of the held flock."""
    monkeypatch.setenv("BRUTEX_PIPELINE_STRICT_GATE", "garbage")
    locks: list[str] = []
    builds: list[str] = []

    def lock_factory(*_a: Any, **_k: Any) -> Any:
        locks.append("lock")  # pragma: no cover — must never run
        return contextlib.nullcontext()

    def builder(**_kwargs: Any) -> Any:
        builds.append("build")  # pragma: no cover — must never run
        raise AssertionError("unreachable")

    with pytest.raises(CliUserError, match="BRUTEX_PIPELINE_STRICT_GATE"):
        cmd_pull_all_spot(lake_root=lake, lock_factory=lock_factory, pipeline_builder=builder)
    assert locks == []
    assert builds == []
    # The eager law covers dry-run too (the binding resolves it either way).
    with pytest.raises(CliUserError, match="BRUTEX_PIPELINE_STRICT_GATE"):
        cmd_pull_all_spot(lake_root=lake, dry_run=True, pipeline_builder=builder)
    # And through main(): the standard one-liner + exit 1, never a traceback.
    code = main(["pull-all-spot", "--lake-root", str(lake)])
    assert code == EXIT_USER_ERROR
    assert "BRUTEX_PIPELINE_STRICT_GATE" in capsys.readouterr().err


def test_capability_refusal_is_user_error(lake: Path) -> None:
    from brutex.nightly.pipeline_binding import NightlyCapabilityError

    def builder(**_kwargs: Any) -> Any:
        raise NightlyCapabilityError("no instrument master")

    with pytest.raises(CliUserError, match="no instrument master"):
        cmd_pull_all_spot(
            lake_root=lake,
            dry_run=True,
            pipeline_builder=builder,
        )


# ── the --auto gate ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "token"),
    [
        (datetime(2026, 7, 12, 18, 0), "not an NSE trading day"),  # Sunday
        (datetime(2025, 10, 21, 18, 0), "not an NSE trading day"),  # Muhurat holiday
        (datetime(2024, 1, 2, 12, 0), "waiting for post-close settle time"),
        (datetime(2024, 1, 2, 17, 29), "waiting for post-close settle time"),
    ],
)
def test_auto_deferral_each_reason_prints_and_exits_ok(
    lake: Path, capsys: pytest.CaptureFixture[str], now: datetime, token: str
) -> None:
    """A/B H7: every --auto deferral is ONE line + exit 0; NOTHING is built."""
    h = _Harness()
    code = h.run(lake, auto=True, now=lambda: now)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "pull-all-spot deferred:" in out
    assert token in out
    assert h.builder_kwargs == []  # [spy] the binding was never built
    assert h.tail_calls == []


def test_auto_contention_exits_zero_one_line(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """B A6: --auto contention is a clean one-liner + exit 0 (no cron storm)."""

    def lock_factory(*_a: Any, **_k: Any) -> Any:
        raise PullAlreadyRunningError(Path("/x.lock"), "pull already running (holder: nightly:xyz)")

    h = _Harness()
    code = h.run(lake, auto=True, now=lambda: _TRADING_EVENING, lock_factory=lock_factory)
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "pull-all-spot refused: a pull is already running" in err
    assert "(holder: nightly:xyz)" in err
    assert h.executed == []


def test_manual_contention_exit_user_error_names_holder(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def lock_factory(*_a: Any, **_k: Any) -> Any:
        raise PullAlreadyRunningError(
            Path("/x.lock"), "pull already running (holder: pull-all-spot:42)"
        )

    h = _Harness()
    code = h.run(lake, lock_factory=lock_factory)
    assert code == EXIT_USER_ERROR
    assert "(holder: pull-all-spot:42)" in capsys.readouterr().err


# ── halts / deferrals ────────────────────────────────────────────────────


def test_manual_market_hours_midrun_deferral_exit_ok(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #1692 cmd_fetch precedent: a mid-run MarketHoursHalt is a deferral
    (ledger drained/resumable inside the spine) — exit 0, never a red cron.
    The resume hint is the BOTH-ARMS wording (refuter-r1 r2 MINOR-1: the
    composed spot-only belt also halts during an EVENING Muhurat session,
    where "the next out-of-hours run" was self-contradictory)."""
    h = _Harness(executor_raises=MarketHoursHalt())
    code = h.run(lake)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "pull-all-spot deferred:" in out
    assert (
        "resume once the market-session protection window clears "
        "(NSE hours 08:45-15:35 IST or a Muhurat session)" in out
    )
    assert "resume on the next out-of-hours run" not in out  # the false claim is gone
    assert h.hp.closed == 1  # the store is closed even on the halt path
    assert h.tail_calls == []


def test_sequencer_market_hours_deferral_exit_ok(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = PipelinePullReport(
        spot=None,
        metadata=None,
        historical=None,
        stopped_after="market_hours",
        note="deferred — inside NSE market-hours window (08:45-15:35 IST); ...",
    )
    h = _Harness(report=report)
    code = h.run(lake)
    assert code == EXIT_OK
    assert "pull-all-spot deferred:" in capsys.readouterr().out
    assert h.tail_calls == []  # the tail never fires on a deferred chain


def test_sequencer_market_hours_deferral_spot_only_wording(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[refuter-r1 r2 MINOR-1 — the CLI mirror] the REAL sequencer deferral
    note (``_market_hours_stop_note`` verbatim) prints spot-only-aware: the
    bare "inside 08:45-15:35" clock claim (FALSE during an evening Muhurat
    deferral) becomes the both-arms wording, the generic three-phase list
    reads spot-only, and the self-contradictory resume advice is gone."""
    from brutex.dashboard.pipeline_pull import _market_hours_stop_note

    report = PipelinePullReport(
        spot=None,
        metadata=None,
        historical=None,
        stopped_after="market_hours",
        note=_market_hours_stop_note(),  # the REAL wording, never re-derived
    )
    h = _Harness(report=report)
    assert h.run(lake) == EXIT_OK
    out = capsys.readouterr().out
    assert (
        "market-session protection window active "
        "(NSE hours 08:45-15:35 IST or a Muhurat session)" in out
    )
    assert "inside NSE market-hours window (08:45-15:35 IST)" not in out
    assert "F&O not applicable — spot-only scope" in out
    assert "spot / metadata / historical skipped" not in out
    assert "resume once the protection window clears" in out
    assert "resume on the next out-of-hours run" not in out


def test_cli_lakewritehalt_alerts_once_and_exits_nonzero(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B R1: a pull-wide halt fires ONE CRITICAL alert then exits 2."""
    alerts: list[BaseException] = []
    monkeypatch.setattr("brutex.cli._emit_pull_halt_alert", alerts.append)
    h = _Harness(executor_raises=LakeWriteHalt("disk_full"))
    code = h.run(lake)
    assert code == EXIT_INTERNAL_ERROR
    assert len(alerts) == 1
    assert isinstance(alerts[0], LakeWriteHalt)
    assert "pull-all-spot HALTED" in capsys.readouterr().err
    assert h.hp.closed == 1


# ── exit-code matrix + strict escalation ─────────────────────────────────


def test_exit_code_matrix(lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C 14: clean 0 · residual 0 · stopped 2."""
    assert _Harness().run(lake) == EXIT_OK  # clean
    residual = _report(quarantined_count=2, quarantined_symbols=("BADSYM",))
    assert _Harness(report=residual).run(lake) == EXIT_OK  # residual stays 0
    stopped = PipelinePullReport(
        spot=_spot_report(still_failed_windows=3),
        metadata=None,
        historical=None,
        stopped_after="spot",
        note="stopped after spot — transient_residual (3 window(s) still failing); ...",
    )
    assert _Harness(report=stopped).run(lake) == EXIT_INTERNAL_ERROR
    capsys.readouterr()


def test_strict_exit_escalates_residual_quarantine_and_tail_nonzero(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    residual = _report(quarantined_count=1, quarantined_symbols=("BADSYM",))
    assert _Harness(report=residual).run(lake, strict_exit=True) == EXIT_INTERNAL_ERROR
    bad_tail = CrossverifyTailOutcome(
        status="publish_failed", human="publish_failed (published 1 · withheld 0 · failed 2)"
    )
    assert _Harness(tail=bad_tail).run(lake, strict_exit=True) == EXIT_INTERNAL_ERROR
    # A clean run with a clean tail stays 0 under --strict-exit.
    assert _Harness().run(lake, strict_exit=True) == EXIT_OK
    capsys.readouterr()


def test_cli_tail_verdict_printed_exit_unchanged_by_default(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fail-soft decree: a tail failure is PRINTED, never the exit code."""
    bad_tail = CrossverifyTailOutcome(
        status="failed", human="FAILED (RuntimeError: boom) — pull unaffected"
    )
    assert _Harness(tail=bad_tail).run(lake) == EXIT_OK
    out = capsys.readouterr().out
    assert "cross-verify: FAILED (RuntimeError: boom) — pull unaffected" in out


# ── the lock-ordering law + the shared controller ────────────────────────


def test_cli_tail_invoked_after_singleflight_release(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[F/B lock-ordering — ruling 8b] The tail runs strictly AFTER the pull
    single-flight with-block exits: a tail that itself ACQUIRES the SAME
    ``pull_singleflight_lock`` non-blocking must SUCCEED. (Verified failing
    when the tail call is moved inside the with-block: the acquire raises
    PullAlreadyRunningError and the fence folds the outcome to 'failed'.)"""
    outcome_holder: list[CrossverifyTailOutcome] = []

    def locking_tail() -> CrossverifyTailOutcome:
        with pull_singleflight_lock(lake):
            outcome = CrossverifyTailOutcome(status="published", human="published (post-lock)")
        outcome_holder.append(outcome)
        return outcome

    h = _Harness()
    code = cmd_pull_all_spot(
        lake_root=lake,
        pipeline_builder=h.builder,
        pipeline_executor=h.executor,
        crossverify_tail=locking_tail,
    )
    assert code == EXIT_OK
    assert outcome_holder, "the tail must have re-acquired the released flock"
    assert "cross-verify: published (post-lock)" in capsys.readouterr().out


def test_cli_tail_lakewritehalt_alerts_then_reraises(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[r1 #3 — the cmd_fetch halt convention] a ``LakeWriteHalt`` raised out
    of the crossverify tail (the fence deliberately re-raises it) fires ONE
    CRITICAL alert then re-raises — never a silent, alert-less traceback."""
    alerts: list[BaseException] = []
    monkeypatch.setattr("brutex.cli._emit_pull_halt_alert", alerts.append)

    def halting_tail() -> CrossverifyTailOutcome:
        raise LakeWriteHalt("disk_full")

    h = _Harness()
    with pytest.raises(LakeWriteHalt):
        cmd_pull_all_spot(
            lake_root=lake,
            pipeline_builder=h.builder,
            pipeline_executor=h.executor,
            crossverify_tail=halting_tail,
        )
    assert len(alerts) == 1
    assert isinstance(alerts[0], LakeWriteHalt)


def test_cli_keep_awake_spans_crossverify_tail_lock_released_first(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[r1 #4] ``keep_awake`` spans the WHOLE command — the tail (a real
    vendor pull + publish leg) runs INSIDE the keep-awake span, while the
    pull single-flight/lease still exit strictly BEFORE it (the lock-ordering
    law preserved — keep_awake is lock-independent)."""
    events: list[str] = []

    @contextlib.contextmanager
    def fake_awake(_label: str) -> Any:
        events.append("awake_enter")
        try:
            yield
        finally:
            events.append("awake_exit")

    monkeypatch.setattr("brutex.observability.power_keepawake.keep_awake", fake_awake)

    def lock_factory(*_a: Any, **_k: Any) -> Any:
        @contextlib.contextmanager
        def _cm() -> Any:
            events.append("lock_enter")
            try:
                yield
            finally:
                events.append("lock_exit")

        return _cm()

    def ordered_tail() -> CrossverifyTailOutcome:
        events.append("tail")
        return CrossverifyTailOutcome(status="published", human="published (spanned)")

    h = _Harness()
    code = cmd_pull_all_spot(
        lake_root=lake,
        pipeline_builder=h.builder,
        pipeline_executor=h.executor,
        lock_factory=lock_factory,
        lease_factory=lambda _root: contextlib.nullcontext(),
        crossverify_tail=ordered_tail,
    )
    assert code == EXIT_OK
    assert events.index("awake_enter") < events.index("lock_enter")
    assert events.index("lock_exit") < events.index("tail")  # lock-ordering law intact
    assert events.index("tail") < events.index("awake_exit")  # the tail is SPANNED
    assert "cross-verify: published (spanned)" in capsys.readouterr().out


def test_cli_one_session_rate_controller_spans_pull_and_tail(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[spy — B V1 twin] The PRODUCTION tail's ``default_seams`` receives the
    SAME SessionRateController the pipeline binding received (one account
    rate budget — §2 Rule 6)."""
    seams_rates: list[Any] = []

    def fake_default_seams(config: Any, **kwargs: Any) -> Any:
        seams_rates.append(kwargs["session_rate"])
        return "SEAMS"

    def fake_run_crossverify(config: Any, seams: Any) -> Any:
        assert seams == "SEAMS"
        from tests.unit.test_dashboard_pull_pipeline_scope import _cv_report

        return _cv_report(verdict="staged_only", published=0)

    monkeypatch.setattr("brutex.crossverify.run.default_seams", fake_default_seams)
    monkeypatch.setattr("brutex.crossverify.run.run_crossverify", fake_run_crossverify)
    h = _Harness()
    code = cmd_pull_all_spot(
        lake_root=lake,
        pipeline_builder=h.builder,
        pipeline_executor=h.executor,
        crossverify_tail=None,  # the PRODUCTION tail closure
    )
    assert code == EXIT_OK
    assert len(seams_rates) == 1
    assert seams_rates[0] is h.builder_kwargs[0]["session_rate"]
    out = capsys.readouterr().out
    assert "cross-verify: staged_only (published 0 · withheld 0 · failed 0)" in out


def test_cli_production_tail_disabled_and_not_configured(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production tail's fail-soft config arms: kill switch + junk env."""
    monkeypatch.setenv("BRUTEX_CROSSVERIFY", "off")
    h = _Harness()
    assert (
        cmd_pull_all_spot(
            lake_root=lake,
            pipeline_builder=h.builder,
            pipeline_executor=h.executor,
            crossverify_tail=None,
        )
        == EXIT_OK
    )
    assert "cross-verify: disabled (BRUTEX_CROSSVERIFY=off)" in capsys.readouterr().out
    monkeypatch.setenv("BRUTEX_CROSSVERIFY", "junk-token")
    h2 = _Harness()
    assert (
        cmd_pull_all_spot(
            lake_root=lake,
            pipeline_builder=h2.builder,
            pipeline_executor=h2.executor,
            crossverify_tail=None,
        )
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "cross-verify: not configured (ValueError" in out
    assert "BRUTEX_CROSSVERIFY" in out


# ── stdout shape / movers honesty / dry-run ──────────────────────────────


def test_stdout_lines_mirror_report_fields_and_movers_honesty(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C 14 + ruling 7: the summary mirrors the REAL report fields; the
    FAILED-windows clause is ALWAYS rendered (0 included); movers honesty."""
    h = _Harness()
    assert h.run(lake) == EXIT_OK
    out = capsys.readouterr().out
    assert "[brutex] pull-all-spot: spot-only pipeline complete" in out
    assert (
        "spot:      clean — 7 pair(s) fetched, 0 window(s) FAILED (will retry), "
        "42 window(s) skipped (already present)" in out
    )
    assert "quarantined: 0" in out
    assert "prebuild:  3 symbol(s), 999 bars" in out
    assert "movers:    not warmed (headless — dashboard caches re-key on next poll)" in out
    assert "greeks:    not run — no options in this pull" in out
    assert "cross-verify: published (published 4 · withheld 0 · failed 0)" in out
    assert "verify:    dashboard /coverage/windows (FAILED tab), /completeness" in out


def test_stdout_defensive_none_fields_render_question_marks(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(prebuild_built=None, prebuild_total_bars=None)
    assert _Harness(report=report).run(lake) == EXIT_OK
    assert "prebuild:  ? symbol(s), ? bars" in capsys.readouterr().out


def test_stdout_no_source_line_emit_on_nonzero(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T22 (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001): the structured no-source
    line renders ONLY when the count is non-zero (capped names + the honest
    remainder from the TRUE count); the clean run's stdout stays
    byte-identical — no line, no token (the sibling clean-run pins above pass
    unmodified)."""
    report = _report(
        prebuild_no_source=("RELIANCE", "IDEA", "YESBANK", "SUZLON"),
        prebuild_no_source_count=6,
    )
    assert _Harness(report=report).run(lake) == EXIT_OK
    out = capsys.readouterr().out
    assert (
        "[brutex]   no-source: 6 symbol(s) skipped — no stored 1-minute data "
        "(vendor-empty or never pulled): RELIANCE, IDEA, YESBANK (+3 more)" in out
    )
    # The clean run renders NO no-source token at all (emit-on-nonzero).
    assert _Harness().run(lake) == EXIT_OK
    assert "no-source" not in capsys.readouterr().out


def test_stdout_ok_headline_carries_note_warning_clauses(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[refuter-r1 r2 MINOR-2 — the EXACT reproduced shape] an ok=True run
    whose note announces "window ledger reload FAILED" AND "prebuild CRASHED"
    must print BOTH clauses (the headline is now the surged note, never a
    bare "spot-only pipeline complete" that drops them), and the prebuild
    line reads CRASHED — never a bland "? symbol(s), ? bars"."""
    report = _report(
        note=(
            "window ledger reload FAILED — running full re-verify pull (no skip); "
            "pipeline complete — spot clean (7 pair(s)), metadata 0 expiries + "
            "0 contracts, historical 0 bars over 0 contract(s)"
            ", prebuild CRASHED (OSError)"
        ),
        prebuild_built=None,
        prebuild_total_bars=None,
        prebuild_crashed="OSError",
    )
    assert report.ok is True  # the reproduced shape: warnings do NOT flip ok
    assert _Harness(report=report).run(lake) == EXIT_OK  # exit unchanged by design
    out = capsys.readouterr().out
    assert "window ledger reload FAILED — running full re-verify pull (no skip);" in out
    assert "prebuild CRASHED (OSError)" in out
    # The headline still leads with the spot-only wording after the prefix.
    assert "spot-only pipeline complete — " in out
    assert ", F&O skipped by design (spot-only scope)" in out  # surgery applied
    assert "0 expiries" not in out  # [neg — B H1 holds on the CLI headline too]
    # The dedicated prebuild line names the crash, not a bland unknown.
    assert "[brutex]   prebuild:  CRASHED (OSError)" in out
    assert "? symbol(s), ? bars" not in out


def test_stdout_unknown_spot_renders_question_mark_failed_count(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[r2 #1] the "?"-for-unknown law covers ``still_failed`` too: a
    defensive ``spot=None`` report (non-market-hours shape) renders
    ``? window(s) FAILED`` — never a fabricated clean-looking 0."""
    report = PipelinePullReport(
        spot=None,
        metadata=None,
        historical=None,
        stopped_after="spot",
        note="stopped after spot — defensive spot-less shape",
    )
    assert _Harness(report=report).run(lake) == EXIT_INTERNAL_ERROR
    out = capsys.readouterr().out
    assert (
        "spot:      ? — ? pair(s) fetched, ? window(s) FAILED (will retry), "
        "? window(s) skipped (already present)" in out
    )
    assert "0 window(s) FAILED" not in out


def test_cli_dry_run_synthetic_no_tail_no_locks(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """B H6 CLI twin: --dry-run is lock-free AND tail-free."""
    locks: list[str] = []

    def lock_factory(*_a: Any, **_k: Any) -> Any:
        locks.append("lock")  # pragma: no cover — must never run
        return contextlib.nullcontext()

    h = _Harness(report=_report(spot=_spot_report(dry_run=True)))
    code = h.run(lake, dry_run=True, lock_factory=lock_factory, lease_factory=lock_factory)
    assert code == EXIT_OK
    assert locks == []
    assert h.tail_calls == []  # skipped_dry_run — the fence never invokes it
    assert h.builder_kwargs[0]["dry_run"] is True
    assert h.builder_kwargs[0]["scope"] == "spot-only"
    assert "cross-verify: skipped (dry-run)" in capsys.readouterr().out


def test_dispatch_wires_all_flags(lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() threads every flag into cmd_pull_all_spot verbatim."""
    seen: list[dict[str, Any]] = []

    def fake_cmd(**kwargs: Any) -> int:
        seen.append(kwargs)
        return EXIT_OK

    monkeypatch.setattr("brutex.cli.cmd_pull_all_spot", fake_cmd)
    code = main(
        [
            "pull-all-spot",
            "--lake-root",
            str(lake),
            "--auto",
            "--dry-run",
            "--settle-ist",
            "18:00",
            "--strict-exit",
        ]
    )
    assert code == EXIT_OK
    assert seen == [
        {
            "lake_root": lake,
            "auto": True,
            "dry_run": True,
            "settle_ist": "18:00",
            "strict_exit": True,
        }
    ]
