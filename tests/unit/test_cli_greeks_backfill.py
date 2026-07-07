"""``brutex greeks-backfill`` — CLI surface tests (DEC-GREEKS-BULK-REENRICH-001).

Parser wiring + dispatch, the fail-fast validations (missing lake / bad
``--batch`` / ``--limit`` / ``--pause-s`` / broken risk-free CSV), the FULLY
INERT ``--dry-run`` (scan+plan print only — the runner is never called, no
alert), the wet run's exactly-once completion alert + per-batch progress
lines, the errored>0 non-zero exit, and the LakeWriteHalt /
GreeksEnrichPoolBroken halt convention (one CRITICAL pull-halt alert, then
re-raise). Every heavy collaborator is monkeypatched at its SOURCE module
(the ``cmd_*`` lazy-import convention), so no real lake scan ever runs here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trading_core", reason="the backfill module needs the trading_core wheel")

from brutex.cli import EXIT_OK, EXIT_USER_ERROR, main
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_backfill import (
    BackfillPlan,
    BackfillScan,
    GreeksBackfillReport,
    dry_run_backfill_report,
)
from brutex.data.greeks_enrich import GreeksEnrichTarget
from brutex.options.risk_free import RiskFreeError

_TARGET = GreeksEnrichTarget(
    exchange="NSE",
    symbol="NSE-NIFTY-25Jan24-21500-CE",
    start=date(2024, 1, 1),
    end=date(2024, 2, 2),
)
_SCAN = BackfillScan(targets=(_TARGET,), stale_v1=1, files_probed=1)
_PLAN = BackfillPlan(
    backfill_id="f" * 64, targets=(_TARGET,), scan=_SCAN, force=False, expired_only=False
)


def _report(**overrides: Any) -> GreeksBackfillReport:
    base = replace(
        dry_run_backfill_report(_PLAN),
        dry_run=False,
        batches=1,
        enriched=1,
        note="Greeks backfill ffffffffffff: 1 contract(s) re-stamped v2, 0 already current, "
        "0 errored (re-runnable); 3 bars written",
    )
    return replace(base, **overrides)


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(root))
    return root


def _patch_scan_plan(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    def _fake_scan(lake_root: Path, **kwargs: Any) -> BackfillScan:
        captured["scan_kwargs"] = kwargs
        captured["scan_lake_root"] = lake_root
        return _SCAN

    def _fake_plan(scan: BackfillScan, **kwargs: Any) -> BackfillPlan:
        captured["plan_scan"] = scan
        return _PLAN

    monkeypatch.setattr("brutex.data.greeks_backfill.scan_stale_greeks_targets", _fake_scan)
    monkeypatch.setattr("brutex.data.greeks_backfill.plan_greeks_backfill", _fake_plan)


def _patch_alert(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    emitted: list[Any] = []
    monkeypatch.setattr(
        "brutex.notifications.sweep_alerts.emit_greeks_backfill_complete_alert",
        emitted.append,
    )
    return emitted


class TestValidation:
    def test_missing_lake_dir_is_a_loud_user_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path / "nowhere"))
        assert main(["greeks-backfill"]) == EXIT_USER_ERROR
        assert "lake root not found" in capsys.readouterr().err

    def test_batch_below_one_refused(self, lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["greeks-backfill", "--batch", "0"]) == EXIT_USER_ERROR
        assert "--batch must be >= 1" in capsys.readouterr().err

    def test_limit_below_one_refused(self, lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["greeks-backfill", "--limit", "0"]) == EXIT_USER_ERROR
        assert "--limit must be >= 1" in capsys.readouterr().err

    def test_negative_pause_refused(self, lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["greeks-backfill", "--pause-s", "-0.5"]) == EXIT_USER_ERROR
        assert "--pause-s must be >= 0" in capsys.readouterr().err

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
    def test_nonfinite_pause_refused(
        self, lake: Path, raw: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fix round 4 (refuter-confirmed): argparse's ``type=float`` parses
        "inf"/"nan" and the bare ``< 0`` check passed both — ``inf`` then
        crashed the wet run mid-batch with a raw ``OverflowError`` from
        ``time.sleep`` AFTER the operator already paid the full O(files) scan;
        ``nan`` silently disabled the pause. Both are refused EAGERLY now.
        """
        # The "=" form: argparse's negative-number matcher would read a bare
        # "-inf" as an option flag, not a value.
        assert main(["greeks-backfill", f"--pause-s={raw}"]) == EXIT_USER_ERROR
        assert "--pause-s must be finite" in capsys.readouterr().err

    def test_force_with_limit_refused(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """force+limit never progresses (stamp-independent selection re-picks the
        SAME first N every run) — refused loudly, the chipping claim is honest."""
        scanned: list[Any] = []
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.scan_stale_greeks_targets",
            lambda *a, **k: scanned.append(a),
        )
        assert main(["greeks-backfill", "--force", "--limit", "2"]) == EXIT_USER_ERROR
        assert "--force with --limit never progresses" in capsys.readouterr().err
        assert scanned == []  # refused BEFORE any scan work

    def test_broken_risk_free_source_fails_fast_before_any_scan(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RiskFreeError("csv header mismatch")

        monkeypatch.setattr("brutex.data.greeks_enrich.resolve_risk_free_source", _boom)
        scanned: list[Any] = []
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.scan_stale_greeks_targets",
            lambda *a, **k: scanned.append(a),
        )
        assert main(["greeks-backfill"]) == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "risk-free source misconfigured" in err and "BRUTEX_RISK_FREE_CSV" in err
        assert scanned == []  # fail-fast: zero partial work


class TestDryRun:
    def test_dry_run_prints_plan_and_never_runs(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured: dict[str, Any] = {}
        _patch_scan_plan(monkeypatch, captured)
        ran: list[Any] = []
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.run_greeks_backfill", lambda *a, **k: ran.append(a)
        )
        emitted = _patch_alert(monkeypatch)
        assert main(["greeks-backfill", "--dry-run", "--limit", "2"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "greeks-backfill plan ffffffffffff" in out
        assert "scan cost: O(files)" in out
        assert "DRY RUN — would re-enrich 1 contract(s)" in out
        assert ran == [] and emitted == []  # INERT: no runner, no telegram
        assert captured["scan_kwargs"]["limit"] == 2
        assert captured["scan_lake_root"] == lake

    def test_unfixable_census_is_printed_loudly(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fix round 4: driver-unfixable contracts surface on stdout AND stderr.

        A null-stamped non-finite-OHLC row is one the enrich driver skips by
        design — silently omitting it from the CLI census would leave the run
        reading green while a stale null stamp persisted (the refuter-confirmed
        false-clean class).
        """
        unfixable_scan = BackfillScan(targets=(), stale_unfixable=2, files_probed=1)
        unfixable_plan = BackfillPlan(
            backfill_id="e" * 64,
            targets=(),
            scan=unfixable_scan,
            force=False,
            expired_only=False,
        )
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.scan_stale_greeks_targets",
            lambda *a, **k: unfixable_scan,
        )
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.plan_greeks_backfill",
            lambda *a, **k: unfixable_plan,
        )
        assert main(["greeks-backfill", "--dry-run"]) == EXIT_OK
        out, err = capsys.readouterr()
        assert "2 unfixable" in out  # the plan-line census
        assert "2 contract(s) UNFIXABLE" in err  # the loud stderr repair note
        assert "2 contract(s) UNFIXABLE" in out  # the dry-run note clause


class TestWetRun:
    def test_clean_run_alerts_once_and_exits_zero(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured: dict[str, Any] = {}
        _patch_scan_plan(monkeypatch, captured)
        emitted = _patch_alert(monkeypatch)
        report = _report()

        def _fake_run(lake_root: Path, **kwargs: Any) -> GreeksBackfillReport:
            captured["run_kwargs"] = kwargs
            kwargs["on_progress"](1, 1, 3, 0, "contracts 1/1 (batch 1)")
            return report

        monkeypatch.setattr("brutex.data.greeks_backfill.run_greeks_backfill", _fake_run)
        assert main(["greeks-backfill", "--batch", "5", "--pause-s", "0.5"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "greeks-backfill 1/1 contracts · 3 bars written · 0 errored" in out
        assert report.note in out
        assert emitted == [report]  # EXACTLY once, at the clean-finish chokepoint
        assert captured["run_kwargs"]["batch_size"] == 5
        assert captured["run_kwargs"]["pause_s"] == 0.5

    def test_errored_contracts_exit_nonzero_naming_the_count(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_scan_plan(monkeypatch, {})
        emitted = _patch_alert(monkeypatch)
        report = _report(errored=2)
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.run_greeks_backfill", lambda *a, **k: report
        )
        assert main(["greeks-backfill"]) == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "2 contract(s) errored" in err and "re-runnable" in err
        assert emitted == [report]  # the summary still fires (honest ⚠️ head)

    @pytest.mark.parametrize(
        "halt", [LakeWriteHalt("disk_full"), GreeksEnrichPoolBroken("worker died")]
    )
    def test_halt_fires_pull_halt_alert_then_reraises(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, halt: Exception
    ) -> None:
        _patch_scan_plan(monkeypatch, {})
        halted: list[Exception] = []
        monkeypatch.setattr("brutex.notifications.sweep_alerts.emit_pull_halt_alert", halted.append)
        completed = _patch_alert(monkeypatch)

        def _raise(*args: Any, **kwargs: Any) -> GreeksBackfillReport:
            raise halt

        monkeypatch.setattr("brutex.data.greeks_backfill.run_greeks_backfill", _raise)
        with pytest.raises(type(halt)):
            main(["greeks-backfill"])
        assert halted == [halt]  # ONE CRITICAL pull-halt alert, then re-raise
        assert completed == []  # no completion summary on a halted run


class TestSingleFlight:
    """The CLI wet run takes the lake-root cross-process single-flight flock.

    Refuter-confirmed defect: the CLI surface previously took NO lock, so a
    ``brutex greeks-backfill`` racing a live pull on the same lake could
    stale-read a live contract before the pull's spot minutes landed and
    latest-wins-overwrite freshly-solved OK Greeks with v2 NO_SPOT NULLs
    (the write spine's safety rests on the single-ENRICH-PASS-per-lake model).
    """

    def test_wet_run_refused_while_a_pull_holds_the_lake_lock(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _patch_scan_plan(monkeypatch, {})
        ran: list[Any] = []
        monkeypatch.setattr(
            "brutex.data.greeks_backfill.run_greeks_backfill", lambda *a, **k: ran.append(a)
        )
        emitted = _patch_alert(monkeypatch)
        with pull_singleflight_lock(lake):  # a concurrent pull holds the lake
            assert main(["greeks-backfill"]) == EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "greeks-backfill REFUSED" in err
        assert "one lake-writer job at a time" in err
        assert ran == [] and emitted == []  # never raced the pull, no fake summary

    def test_wet_run_holds_the_lake_singleflight_lock(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flock is HELD for the whole write pass, not just checked once."""
        from brutex.adapters.groww.pull_singleflight import (
            PullAlreadyRunningError,
            pull_singleflight_lock,
        )

        _patch_scan_plan(monkeypatch, {})
        _patch_alert(monkeypatch)
        held: list[bool] = []

        def _probe_run(lake_root: Path, **kwargs: Any) -> GreeksBackfillReport:
            try:
                with pull_singleflight_lock(lake_root):
                    held.append(False)
            except PullAlreadyRunningError:
                held.append(True)
            return _report()

        monkeypatch.setattr("brutex.data.greeks_backfill.run_greeks_backfill", _probe_run)
        assert main(["greeks-backfill"]) == EXIT_OK
        assert held == [True]  # a peer acquire DURING the run is refused ⇒ lock held

    def test_dry_run_stays_lock_free_while_a_pull_holds_the_lake_lock(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run is read-only — it must run fine mid-pull, lock-free."""
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _patch_scan_plan(monkeypatch, {})
        emitted = _patch_alert(monkeypatch)
        with pull_singleflight_lock(lake):
            assert main(["greeks-backfill", "--dry-run"]) == EXIT_OK
        assert "DRY RUN" in capsys.readouterr().out
        assert emitted == []


class TestParserWiring:
    def test_dispatch_threads_every_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def _fake_cmd(**kwargs: Any) -> int:
            seen.update(kwargs)
            return EXIT_OK

        monkeypatch.setattr("brutex.cli.cmd_greeks_backfill", _fake_cmd)
        assert (
            main(
                [
                    "greeks-backfill",
                    "--lake-root",
                    "/tmp/somewhere",
                    "--dry-run",
                    "--force",
                    "--expired-only",
                    "--batch",
                    "9",
                    "--limit",
                    "4",
                    "--pause-s",
                    "0.2",
                ]
            )
            == EXIT_OK
        )
        assert seen == {
            "lake_root": Path("/tmp/somewhere"),
            "dry_run": True,
            "force": True,
            "expired_only": True,
            "batch": 9,
            "limit": 4,
            "pause_s": 0.2,
        }

    def test_other_commands_fall_through_the_new_dispatch_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-backfill command passes THROUGH the greeks-backfill arm untouched."""
        monkeypatch.setattr("brutex.cli.cmd_detect_session_drift", lambda **kwargs: EXIT_OK)
        assert main(["detect-session-drift", "--month", "2026-01"]) == EXIT_OK
