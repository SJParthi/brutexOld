"""Run-orchestration pins: fencing, exit matrix, teeth withhold, streak carry.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.acceptance import report as report_mod
from brutex.acceptance import selftest
from brutex.acceptance.checks import ALL_CHECK_IDS, CHECK_REGISTRY
from brutex.acceptance.model import TeethVerdict
from brutex.acceptance.run import RunConfig, repro_command, run_acceptance

from tests.fakes.acceptance_twin import NOW, make_twin

PROVEN = selftest.SelfTestResult(
    mode="smoke",
    poisons_planted=3,
    poisons_detected=3,
    false_fails_on_clean=0,
    verdict=TeethVerdict.TEETH_PROVEN,
    duration_s=0.1,
)

FAILED = selftest.SelfTestResult(
    mode="smoke",
    poisons_planted=3,
    poisons_detected=2,
    false_fails_on_clean=0,
    verdict=TeethVerdict.TEETH_FAILED,
    duration_s=0.1,
)


def _proven_runner(**kwargs: object) -> selftest.SelfTestResult:
    return PROVEN


def _cfg(lake: Path, **kwargs: object) -> RunConfig:
    defaults: dict[str, object] = {
        "lake_root": lake,
        "checks": ("unique", "tf"),
        "seed": "2026-07-10",
        "sample_n": 50,
        "argv": ("verify-acceptance",),
    }
    defaults.update(kwargs)
    return RunConfig(**defaults)  # type: ignore[arg-type]


def _run(cfg: RunConfig, **kwargs: object):
    kwargs.setdefault("now_utc", lambda: NOW)
    kwargs.setdefault("selftest_runner", _proven_runner)
    return run_acceptance(cfg, **kwargs)  # type: ignore[arg-type]


class TestCleanRun:
    def test_exit_zero_and_report_files(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake))
        assert code == 0
        assert report["overall"]["verdict"] == "PASS_WITH_ABSTAINS"  # tf.oi structural
        assert report_mod.validate_report_dict(report) == []
        run_dir = lake / "acceptance" / report["run_id"]
        assert (run_dir / "report.json").is_file()
        assert (run_dir / "report.txt").is_file()
        latest = (lake / "acceptance" / "LATEST").read_text(encoding="utf-8").strip()
        assert latest == report["run_id"]
        # audit chain verifies with the EXISTING tool
        from brutex.audit_verify import verify_audit_file

        result = verify_audit_file(report_mod.audit_path(lake))
        assert result.entry_count == 4  # start + 2 checks + complete

    def test_seed_defaults_to_rotated_ist_date(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _code, report = _run(_cfg(lake, seed=None))
        assert report["inputs"]["seed"] == "2026-07-10"
        assert report["inputs"]["seed_mode"] == "rotated"

    def test_fixed_seed_mode(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _code, report = _run(_cfg(lake))
        assert report["inputs"]["seed_mode"] == "fixed"

    def test_real_selftest_smoke_binds_by_default(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code, report = run_acceptance(_cfg(lake, checks=("unique",)), now_utc=lambda: NOW)
        assert report["self_test"]["verdict"] == "TEETH_PROVEN"
        assert code == 0


class TestTeethGate:
    def test_teeth_failed_withholds_verdicts_exit_2(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake), selftest_runner=lambda **k: FAILED)
        assert code == 2
        assert report["checks"] == []  # real-lake verdicts WITHHELD
        assert report["overall"]["verdict"] == "SUSPECT"

    def test_selftest_crash_is_teeth_failed(self, tmp_path: Path) -> None:
        def _boom(**kwargs: object) -> selftest.SelfTestResult:
            raise RuntimeError("selftest exploded")

        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake), selftest_runner=_boom)
        assert code == 2
        assert report["self_test"]["verdict"] == "TEETH_FAILED"

    def test_skipped_selftest_recorded_not_punished(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake, self_test_mode="skipped"))
        assert code == 0
        assert report["self_test"]["verdict"] == "TEETH_SKIPPED"

    def test_full_battery_mode_binds_battery(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def _spy(**kwargs: object) -> selftest.SelfTestResult:
            calls.append("ran")
            return PROVEN

        lake = make_twin(tmp_path)
        _run(_cfg(lake, self_test_mode="full"), selftest_runner=_spy)
        assert calls == ["ran"]


class TestFencing:
    def test_raising_check_is_error_exit_2_others_still_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(ctx):
            raise RuntimeError("check exploded")

        monkeypatch.setitem(CHECK_REGISTRY, "unique", _boom)
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake))
        assert code == 2
        by_id = {c["check_id"]: c for c in report["checks"]}
        assert by_id["unique"]["verdict"] == "ERROR"
        assert "RuntimeError" in by_id["unique"]["sub_checks"][0]["evidence"]["exception"]
        assert by_id["tf"]["verdict"] in {"PASS", "FAIL"}  # the run CONTINUED

    def test_time_budget_exhausted_abstains_exit_4(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake, max_runtime_s=0.0))
        assert code == 4
        for check in report["checks"]:
            sub = check["sub_checks"][0]
            assert sub["abstain_reason"] == "time_budget_exhausted"

    def test_hung_check_hard_timeout_exit_2_run_finishes_lock_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-5 F1 defense-in-depth: a wedged check is ABANDONED on a daemon
        thread → ERROR(check_timed_out) → exit 2; the run STILL finishes + the
        later check runs + the single-flight flock is released so the next run
        acquires it (never an unbounded hang that bricks the command)."""
        import time as _t

        from brutex.acceptance.run import acceptance_lock_path

        def _wedge(ctx):
            _t.sleep(1000)  # never returns

        monkeypatch.setitem(CHECK_REGISTRY, "unique", _wedge)
        lake = make_twin(tmp_path)
        started = _t.monotonic()
        code, report = _run(_cfg(lake, check_timeout_s=1.0))
        elapsed = _t.monotonic() - started
        assert elapsed < 30, "the watchdog must abort a hung check well within budget"
        assert code == 2
        by_id = {c["check_id"]: c for c in report["checks"]}
        assert by_id["unique"]["verdict"] == "ERROR"
        sub = by_id["unique"]["sub_checks"][0]
        assert sub["id"] == "unique.timed_out"
        assert sub["evidence"]["reason"] == "check_timed_out"
        assert by_id["tf"]["verdict"] in {"PASS", "FAIL"}  # the run CONTINUED
        # the flock is released — the next run acquires it (no SUSPECT refusal).
        _code2, report2 = _run(_cfg(lake, checks=("tf",), check_timeout_s=5.0))
        assert "single-flight" not in str(report2.get("error", ""))
        assert acceptance_lock_path(lake).is_file()  # file persists; lock freed

    def test_watchdog_disabled_runs_inline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``check_timeout_s=None`` runs checks inline (no watchdog thread)."""
        import threading

        seen: dict[str, int] = {}

        def _record(ctx):
            seen["thread"] = threading.get_ident()
            from brutex.acceptance.checks import unique

            return unique.run(ctx)

        monkeypatch.setitem(CHECK_REGISTRY, "unique", _record)
        lake = make_twin(tmp_path)
        _run(_cfg(lake, checks=("unique",), check_timeout_s=None))
        assert seen["thread"] == threading.get_ident()  # ran on the caller thread


class TestExitMatrix:
    def test_fail_finding_exit_3(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import poison_duplicate_row

        from tests.fakes.acceptance_twin import DAY2

        lake = make_twin(tmp_path)
        poison_duplicate_row(lake, symbol="NIFTY", day=DAY2)
        code, report = _run(_cfg(lake, checks=("unique",)))
        assert code == 3
        assert report["overall"]["verdict"] == "FAIL"

    def test_abstain_budget_breach_exit_4(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        # pull (no ledger) + movers/latency-free selection keeps this cheap:
        # rate contributes 3 non-structural abstains on a bare twin.
        code, _report = _run(_cfg(lake, checks=("pull", "rate"), abstain_budget=0))
        assert code == 4


class TestSuspectPaths:
    def test_audit_open_failure_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def __init__(self, lake_root: Path) -> None:
                raise OSError("audit volume wedged")

        monkeypatch.setattr(report_mod, "AcceptanceAudit", _Boom)
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "audit chain unavailable" in report["error"]

    def test_audit_append_failure_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self, **kwargs: object) -> None:
            raise OSError("append wedged")

        monkeypatch.setattr(report_mod.AcceptanceAudit, "check", _boom)
        lake = make_twin(tmp_path)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "audit append failed" in report["error"]

    def test_report_write_failure_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object):
            raise report_mod.ReportWriteError("schema violated")

        monkeypatch.setattr(report_mod, "write_report_files", _boom)
        lake = make_twin(tmp_path)
        code, _report = _run(_cfg(lake))
        assert code == 2

    def test_hardware_key_failure_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.config.hardware_envelope as hw

        def _boom() -> str:
            raise RuntimeError("no fingerprint")

        monkeypatch.setattr(hw, "hardware_key", _boom)
        lake = make_twin(tmp_path)
        _code, report = _run(_cfg(lake))
        assert report["box"]["hardware_key"] is None


class TestChainAcrossRuns:
    def test_second_run_carries_deltas_and_streaks(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _code, first = _run(_cfg(lake))
        code, second = _run(_cfg(lake))
        assert code == 0
        assert second["previous"]["run_id"] == first["run_id"]
        assert isinstance(second["deltas"], dict)
        assert second["deltas"]["verdict_transitions"] == []
        # tf.oi abstains both runs → streak 2 on run 2.
        oi = next(
            sub for check in second["checks"] for sub in check["sub_checks"] if sub["id"] == "tf.oi"
        )
        assert oi["abstain_streak"] == 2

    def test_subset_run_carries_absent_subcheck_streaks_forward(self, tmp_path: Path) -> None:
        # Round-4 fix (B4-4) fails-before regression: a --checks subset run
        # wrote a report whose sub-check index lacked every other check, so
        # the NEXT full run read streak 1 where the prior full run showed a
        # higher streak — the streak-≥3 ⇒ exit-4 law was suppressible
        # indefinitely by interleaved subset runs.
        def _oi_streak(report: dict) -> int:
            return next(
                sub["abstain_streak"]
                for check in report["checks"]
                for sub in check["sub_checks"]
                if sub["id"] == "tf.oi"
            )

        lake = make_twin(tmp_path)
        _code, first = _run(_cfg(lake))
        assert _oi_streak(first) == 1
        # The interleaved SUBSET run (tf absent) must carry tf.oi forward.
        _code, subset = _run(_cfg(lake, checks=("unique",)))
        assert subset["streak_carry"]["tf.oi"]["abstain_streak"] == 1
        _code, third = _run(_cfg(lake))
        assert _oi_streak(third) == 2  # pre-fix: reset to 1

    def test_teeth_failed_run_carries_streaks_forward(self, tmp_path: Path) -> None:
        # A TEETH_FAILED run withholds checks (checks: []) — its report must
        # still carry every prior streak verbatim (B4-4).
        lake = make_twin(tmp_path)
        _code, _first = _run(_cfg(lake))
        code, withheld = _run(_cfg(lake), selftest_runner=lambda **k: FAILED)
        assert code == 2
        assert withheld["checks"] == []
        assert withheld["streak_carry"]["tf.oi"]["abstain_streak"] == 1
        _code, third = _run(_cfg(lake))
        oi = next(
            sub for check in third["checks"] for sub in check["sub_checks"] if sub["id"] == "tf.oi"
        )
        assert oi["abstain_streak"] == 2

    def test_streak_law_escalates_through_interleaved_subset_runs(self, tmp_path: Path) -> None:
        # The exit-4 law itself bites through an interleave: three pull
        # NO_LEDGER abstains (non-structural) with a unique-only subset run
        # in between still reach streak ≥3 ⇒ exit 4.
        lake = make_twin(tmp_path)
        code1, _r1 = _run(_cfg(lake, checks=("pull",)))
        code2, _r2 = _run(_cfg(lake, checks=("pull",)))
        _code, _subset = _run(_cfg(lake, checks=("unique",)))
        code4, fourth = _run(_cfg(lake, checks=("pull",)))
        # ONE non-structural abstain sits inside the default budget (3), so
        # runs 1-2 stay green — only the streak-≥3 law can trip exit 4.
        assert (code1, code2) == (0, 0)
        replan = next(
            sub
            for check in fourth["checks"]
            for sub in check["sub_checks"]
            if sub["id"] == "pull.replan_zero"
        )
        assert replan["abstain_streak"] == 3  # pre-fix: the subset run reset it
        assert code4 == 4  # streak ≥3 ⇒ exit 4 (model.py:36-38 law)

    def test_out_dir_override_is_off_chain(self, tmp_path: Path) -> None:
        # Round-3 fix (C-F3): an --out run must NOT dangle LATEST — the
        # pointer previously named a run id absent under <lake>/acceptance,
        # so the next default run's load_previous silently reset every
        # streak/delta and an operator following LATEST found nothing.
        lake = make_twin(tmp_path)
        out = tmp_path / "elsewhere"
        _code, report = _run(_cfg(lake, out_dir=out))
        assert (out / "report.json").is_file()
        assert not (lake / "acceptance" / "LATEST").exists()
        assert not (lake / "acceptance" / str(report["run_id"])).exists()

    def test_out_dir_override_keeps_existing_chain(self, tmp_path: Path) -> None:
        # An --out run after a default run leaves the chain byte-untouched.
        lake = make_twin(tmp_path)
        _code, first = _run(_cfg(lake))
        latest = lake / "acceptance" / "LATEST"
        before = latest.read_text(encoding="utf-8")
        assert str(first["run_id"]) in before
        _code2, second = _run(_cfg(lake, out_dir=tmp_path / "elsewhere"))
        assert latest.read_text(encoding="utf-8") == before
        # The off-chain run still CONSUMED the chain (deltas vs the default
        # run) — it just never advanced it.
        assert (second.get("previous") or {}).get("run_id") == first["run_id"]


class TestEvidenceTreeSymlinkRefusal:
    """Round-3 fix (B-NEW-1): ONE refusal BEFORE any write — audit included.

    The round-2 guard sat at report-write time, so the hash-chained audit
    JSONL had already been written THROUGH a symlinked ``<lake>/acceptance``
    into the attacker's target dir.
    """

    def test_symlinked_acceptance_refuses_whole_run_before_audit(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        target = tmp_path / "elsewhere"
        target.mkdir()
        (lake / "acceptance").symlink_to(target)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "symlink" in report["error"]
        # NOTHING written through the link — no audit/, no report, no ledger.
        assert list(target.iterdir()) == []

    def test_symlinked_audit_child_dir_refused(self, tmp_path: Path) -> None:
        # Round-4 fix (A-F1) fails-before regression (the refuter's
        # ``sym_inner`` repro): the round-3 guard covered only the TOP
        # <lake>/acceptance dir — a symlinked CHILD acceptance/audit dir was
        # written THROUGH (exit 0, the hash chain landed in the attacker
        # dir, no in-lake chain, no refusal).
        lake = make_twin(tmp_path)
        target = tmp_path / "attacker_audit"
        target.mkdir()
        (lake / "acceptance").mkdir()
        (lake / "acceptance" / "audit").symlink_to(target)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "symlink" in report["error"]
        assert list(target.iterdir()) == []  # nothing through the link

    def test_symlinked_audit_chain_file_refused(self, tmp_path: Path) -> None:
        # The fixed-path chain FILE itself is the third plantable link.
        lake = make_twin(tmp_path)
        target = tmp_path / "attacker_chain.jsonl"
        target.write_text("", encoding="utf-8")
        audit_dir = lake / "acceptance" / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "acceptance.jsonl").symlink_to(target)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "symlink" in report["error"]
        assert target.read_text(encoding="utf-8") == ""  # never appended through


class TestSingleFlightLock:
    """Round-4 fix (B4-1): concurrent runs corrupted the audit hash chain.

    Two overlapping ``verify-acceptance`` runs interleaved appends into the
    ONE single-writer ``acceptance.jsonl`` (``SweepAuditWriter``), corrupting
    the chain PERMANENTLY (``brutex audit verify`` failed forever after).
    The second run must REFUSE loudly, naming the holder, SUSPECT exit 2.
    """

    def test_contended_run_refused_names_holder(self, tmp_path: Path) -> None:
        import fcntl
        import os

        from brutex.acceptance.run import acceptance_lock_path

        lake = make_twin(tmp_path)
        lock_path = acceptance_lock_path(lake)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, b"acceptance:peer-run pid=999 host=peerbox")
            code, report = _run(_cfg(lake))
        finally:
            os.close(fd)
        assert code == 2
        assert "another verify-acceptance run" in report["error"]
        assert "acceptance:peer-run" in report["error"]  # the holder is NAMED
        # NOTHING was written while refused — no audit chain, no report dir.
        assert not (lake / "acceptance").exists()

    def test_contended_run_refused_without_holder_note(self, tmp_path: Path) -> None:
        import fcntl
        import os

        from brutex.acceptance.run import acceptance_lock_path

        lake = make_twin(tmp_path)
        lock_path = acceptance_lock_path(lake)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, report = _run(_cfg(lake))
        finally:
            os.close(fd)
        assert code == 2
        assert "another verify-acceptance run" in report["error"]
        assert "holder" not in report["error"]  # empty note → no holder suffix

    def test_lock_released_after_run_and_chain_verifies(self, tmp_path: Path) -> None:
        # Sequential runs re-acquire cleanly and the chain stays verifiable —
        # the lock never wedges the nightly.
        from brutex.audit_verify import verify_audit_file

        lake = make_twin(tmp_path)
        code1, _r1 = _run(_cfg(lake, checks=("unique",)))
        code2, _r2 = _run(_cfg(lake, checks=("unique",)))
        assert (code1, code2) == (0, 0)
        result = verify_audit_file(report_mod.audit_path(lake))
        assert result.entry_count == 6  # 2 x (start + 1 check + complete)

    def test_flock_oserror_closes_fd_and_is_suspect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-contention flock failure (e.g. a filesystem without flock)
        # must close the fd and surface as the SUSPECT lock-unavailable arm.
        import brutex.acceptance.run as run_mod

        def _boom(fd: int, op: int) -> None:
            raise OSError("flock unsupported on this volume")

        lake = make_twin(tmp_path)  # built BEFORE the patch (writer flocks too)
        monkeypatch.setattr(run_mod.fcntl, "flock", _boom)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "lock unavailable" in report["error"]

    def test_lock_unavailable_is_suspect(self, tmp_path: Path) -> None:
        # The lock path exists as a DIRECTORY → os.open cannot create it →
        # SUSPECT exit 2, never a raw traceback.
        from brutex.acceptance.run import acceptance_lock_path

        lake = make_twin(tmp_path)
        acceptance_lock_path(lake).mkdir(parents=True)
        code, report = _run(_cfg(lake))
        assert code == 2
        assert "lock unavailable" in report["error"]


class TestReportWriteFailureLoud:
    """Round-3 fix (C-F4): a failed report write restamps overall to SUSPECT."""

    def test_write_failure_restamps_overall_exit_2(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        code, report = _run(_cfg(lake, out_dir=blocker / "sub"))
        assert code == 2
        assert report["overall"]["exit_code"] == 2  # the table can never say exit 0
        assert report["overall"]["verdict"] == "SUSPECT"
        assert "report_write_error" in report


class TestNegativeSampleRefused:
    def test_negative_sample_is_a_core_usage_error(self, tmp_path: Path) -> None:
        # Round-3 NIT (C-F5): sample_n=-1 is a Python slice ([:-1] selects
        # population−1 datasets) — refused in the CORE, not just the CLI.
        code, report = run_acceptance(
            RunConfig(lake_root=tmp_path, checks=("unique",), sample_n=-1)
        )
        assert code == 1
        assert "sample_n" in report["error"]


class TestRegistryAndRepro:
    def test_registry_covers_all_seven(self) -> None:
        assert ALL_CHECK_IDS == ("unique", "tf", "pull", "rate", "movers", "sanity", "latency")

    def test_registry_runners_dispatch(self, tmp_path: Path) -> None:
        from tests.fakes.acceptance_twin import make_ctx

        lake = make_twin(tmp_path)
        ctx = make_ctx(lake)
        for check_id in ("unique", "tf", "pull", "rate", "sanity"):
            result = CHECK_REGISTRY[check_id](ctx)
            assert result.check_id == check_id

    def test_registry_client_checks_dispatch(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        from tests.fakes.acceptance_twin import make_ctx

        lake = make_twin(tmp_path)
        from brutex.data.instrument_store import InstrumentStore

        InstrumentStore.open(lake / "instruments.duckdb").close()
        ctx = make_ctx(lake)
        for check_id in ("movers", "latency"):
            result = CHECK_REGISTRY[check_id](ctx)
            assert result.check_id == check_id

    def test_repro_command(self) -> None:
        assert repro_command("tf") == "brutex verify-acceptance --checks tf"
        assert (
            repro_command("tf", seed="2026-07-10")
            == "brutex verify-acceptance --checks tf --seed 2026-07-10"
        )


class TestCoverageLedgerAccumulation:
    def test_ledger_written_and_summarised(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _code, report = _run(_cfg(lake))
        ledger_path = lake / "acceptance" / "coverage_ledger.json"
        assert ledger_path.is_file()
        cells = json.loads(ledger_path.read_text(encoding="utf-8"))["run"]
        assert "NSE/INDEX/NIFTY" in cells
        assert report["coverage_ledger"]["datasets_sampled_window_pct"] == 100.0
        # Round-2 fix 8: the F&O contract population joins the ledger's
        # visibility space (truncation disclosed; the twin has few contracts).
        assert report["coverage_ledger"]["fno_population_truncated"] == 0

    def test_fno_contracts_visible_to_oldest_unsampled(self, tmp_path: Path) -> None:
        # Round-2 fix 8 (refuter 2C MINOR): a never-censused contract dir must
        # be VISIBLE to the coverage ledger's oldest_unsampled headline —
        # pre-fix the population was spot-only, so ~10^5 contract dirs could
        # never surface there.
        from brutex.acceptance import sampling as sampling_mod
        from brutex.acceptance.selftest import _twin_bars
        from brutex.data.parquet_writer import ParquetWriter

        from tests.fakes.acceptance_twin import DAY2

        lake = make_twin(tmp_path)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jul26-24000-CE",
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        _code, report = _run(_cfg(lake, checks=("unique",)))
        summary = report["coverage_ledger"]
        ledger = sampling_mod.load_coverage_ledger(sampling_mod.coverage_ledger_path(lake))
        examined = {k for cells in ledger.values() for k in cells}
        # The (single) contract is examined this run OR named oldest_unsampled
        # — either way it is IN the visibility space now.
        assert (
            "NSE/FNO/NSE-NIFTY-30Jul26-24000-CE" in examined
            or summary["oldest_unsampled"] == "NSE/FNO/NSE-NIFTY-30Jul26-24000-CE"
        )


class TestPerfTimerInjection:
    def test_perf_timer_threaded_to_context(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        ticks = iter(float(i) for i in range(10_000))
        code, _report = _run(_cfg(lake, checks=("unique",)), perf_timer=lambda: next(ticks))
        assert code == 0


class TestEmptyChecksRefusedInCore:
    def test_empty_checks_is_usage_error_never_pass(self, tmp_path: Path) -> None:
        # Round 1 MINOR regression: run_acceptance(checks=()) returned a
        # zero-work exit-0 PASS — the CORE now refuses (LAW 2 at run level).
        lake = make_twin(tmp_path)
        exit_code, report = _run(_cfg(lake, checks=(), self_test_mode="skipped"))
        assert exit_code == 1
        assert "no checks requested" in report["error"]


class TestMemoryErrorPropagates:
    def test_check_oom_reraises_raw(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # N3 regression (house law): OOM is never swallowed into an ERROR verdict.
        def _oom(ctx: object) -> object:
            raise MemoryError("boom")

        monkeypatch.setitem(CHECK_REGISTRY, "unique", _oom)
        lake = make_twin(tmp_path)
        with pytest.raises(MemoryError):
            _run(_cfg(lake, checks=("unique",)))

    def test_selftest_oom_reraises_raw(self, tmp_path: Path) -> None:
        def _oom_runner(**kwargs: object) -> selftest.SelfTestResult:
            raise MemoryError("boom")

        lake = make_twin(tmp_path)
        with pytest.raises(MemoryError):
            _run(_cfg(lake), selftest_runner=_oom_runner)


class TestRunScopedEnvRestore:
    def test_env_override_never_leaks_out_of_the_run(self, tmp_path: Path) -> None:
        # MN1 regression at the RUN level: the in-process F1 nightly chain
        # must not inherit a mutated BRUTEX_LAKE_ROOT after run_acceptance.
        import os

        pytest.importorskip("fastapi")
        os.environ.pop("BRUTEX_LAKE_ROOT", None)
        lake = make_twin(tmp_path)
        # latency uses the DEFAULT client factory (no instruments.duckdb →
        # the scoped-env arm engages), then close() must restore.
        exit_code, _report = _run(_cfg(lake, checks=("latency",)))
        assert exit_code in (0, 4)
        assert "BRUTEX_LAKE_ROOT" not in os.environ
