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

    def test_out_dir_override(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        out = tmp_path / "elsewhere"
        _code, report = _run(_cfg(lake, out_dir=out))
        assert (out / "report.json").is_file()
        assert report["run_id"] in (lake / "acceptance" / "LATEST").read_text(encoding="utf-8")


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
