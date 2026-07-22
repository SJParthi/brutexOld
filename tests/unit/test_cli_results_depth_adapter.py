"""Unit suite for the cli results-depth run-end adapter (DEC-SWEEP-RESULTS-DEPTH-001, WP6).

Covers the extracted, non-pragma'd :func:`brutex.cli._run_results_depth_adapter`
in full: the WP3 provider wiring (phrases reasons + margin-true ROI), the
honest report line, the fence (loud print + log, sweep never fails), the
global-stop pass-through, and the default-runner arm driving the REAL
:func:`run_results_depth_pass` over an empty store tree (0-entry top100
written). The kill-switch PARSE law itself lives in
``test_engine_results_depth_pass.py`` (``resolve_results_depth_enabled``);
its production wiring is proven end-to-end in
``tests/integration/test_cli_results_depth_wiring.py``.
"""

# Whitebox suite: drives cli._run_results_depth_adapter /
# _print_resilience_lines directly (DEC-P0-017).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from brutex import cli
from brutex.engine.results_depth_pass import ResultsDepthReport
from brutex.ranking.phrases import reason_payload
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")

_ENTRY_DATE = date(2024, 1, 2)


def _report(**overrides: Any) -> ResultsDepthReport:
    base: dict[str, Any] = {
        "tuples_processed": 1,
        "tuples_skipped_current": 2,
        "tuples_missing_store": 0,
        "tuples_errored": 0,
        "resilience_backfilled": 0,
        "resilience_errored": 0,
        "combos_captured": 7,
        "combos_parity_failed": 0,
        "combos_zero_trades": 1,
        "vix_missing_trades": 3,
        "baselines_written": 1,
        "pool": 6,
        "gate_passers": 4,
        "excluded_no_aggs": 0,
        "zero_trade_excluded": 1,
        "parity_excluded": 0,
        "families_collapsed": 1,
        "top100_written": True,
        "top100_entries": 4,
    }
    base.update(overrides)
    return ResultsDepthReport(**base)


def _call(tmp_path: Path, runner: Any, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "out_root": tmp_path / "sweeps",
        "grid": SimpleNamespace(),
        "bars_for": lambda _instrument, _tf: [],
        "minute_bars_for": lambda _instrument: None,
        "daily_bars_for": lambda _instrument: None,
        "vix_bars": None,
        "margin_entry_date": _ENTRY_DATE,
        "pass_runner": runner,
    }
    kwargs.update(overrides)
    return cli._run_results_depth_adapter(**kwargs)


class TestProviderWiring:
    def test_pass_receives_the_wp3_providers_and_the_report_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        ok = _call(tmp_path, _runner)

        assert ok is True
        # The reasons provider is EXACTLY the phrases wire function — the ONE
        # R33 table, never a re-derived copy (CLAUDE.md §11 COMMON).
        assert captured["reason_provider"] is reason_payload
        # The margin provider closes over margin_roi_payload with lots=1 at
        # the sweep-window start date — the v1 verdict is honest "unverified".
        candidate = SimpleNamespace(row=SimpleNamespace(net_pnl_paisa=12_345, instrument="NIFTY"))
        payload = captured["margin_roi_provider"](candidate)
        assert payload["status"] == "unverified"
        assert payload["roi_pct"] is None
        assert "UNVERIFIED" in payload["detail"]
        # Every seam threads through unchanged.
        assert captured["out_root"] == tmp_path / "sweeps"
        assert captured["vix_bars"] is None
        out = capsys.readouterr().out
        assert (
            "[brutex] results-depth — tuples=1 skipped_current=2 missing_store=0 "
            "errored=0 resilience_errored=0 captured=7 parity_failed=0 pool=6 "
            "gate_passers=4 baselines=1 top100=written entries=4"
        ) in out

    def test_failed_top100_reads_failed_in_the_report_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ok = _call(tmp_path, lambda **_kwargs: _report(top100_written=False, top100_entries=0))
        assert ok is True  # the PASS completed; the artifact failure is named
        assert "top100=FAILED entries=0" in capsys.readouterr().out


class TestFence:
    def test_pass_failure_prints_loud_logs_and_never_raises(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(**_kwargs: Any) -> ResultsDepthReport:
            raise RuntimeError("sidecar write exploded")

        with capture_logs() as logs:
            ok = _call(tmp_path, _boom)

        assert ok is False
        err = capsys.readouterr().err
        assert "results-depth pass FAILED (RuntimeError: sidecar write exploded)" in err
        assert "sweep itself SUCCEEDED" in err
        assert "idempotent stamp-skip resume" in err
        events = [log for log in logs if log["event"] == "cli.results_depth.pass_failed"]
        assert len(events) == 1
        assert events[0]["error_type"] == "RuntimeError"

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_global_stops_reraise_raw(self, tmp_path: Path, exc_type: type[BaseException]) -> None:
        def _stop(**_kwargs: Any) -> ResultsDepthReport:
            raise exc_type

        with pytest.raises(exc_type):
            _call(tmp_path, _stop)


class TestDefaultRunnerArm:
    def test_none_runner_drives_the_real_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``pass_runner=None`` runs the REAL run_results_depth_pass: a grid
        whose single store never swept counts missing + still writes the
        honest 0-entry top-100 artifact (provider-wired, ``*_wired=True``)."""
        import json

        from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.strategies.types import Side

        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe(label="1minute", minutes=1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0, 1],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        out_root = tmp_path / "sweeps"
        out_root.mkdir()

        ok = _call(tmp_path, None, grid=grid)

        assert ok is True
        out = capsys.readouterr().out
        assert "results-depth — tuples=0" in out
        assert "missing_store=1" in out
        assert "top100=written entries=0" in out
        # Ruling 4: the artifact lands in the sweep_runs SIBLING root.
        artifact_path = tmp_path / "sweep_runs" / grid_identity(grid) / "top100.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["entries"] == []
        assert payload["source"]["reasons_wired"] is True
        assert payload["source"]["margin_roi_wired"] is True


class TestCascadeReportThreading:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the in-memory report → join → pass."""

    def test_report_flattened_and_threaded_to_the_pass(self, tmp_path: Path) -> None:
        from brutex.engine.cascade_wire import CascadeWireReport
        from brutex.metrics.cascade import CandidateVerdict, CascadeReport

        inner = CascadeReport(
            verdicts=(
                CandidateVerdict(
                    candidate_id="a" * 64,
                    passed=True,
                    rejected_at=None,
                    detail="survived all Stage-4 filters",
                    composite_score=1.0,
                    cpcv_pass_rate=0.75,
                ),
            ),
            survivor_ids=("a" * 64,),
            n_input=1,
            n_survived=1,
            stage_survivors={},
        )
        report = CascadeWireReport(sweep_id="s" * 64, groups=(("NIFTY", "1minute", inner),))
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        ok = _call(tmp_path, _runner, cascade_report=report)
        assert ok is True
        join = captured["cascade_join"]
        assert join is not None
        assert join.sweep_id == "s" * 64
        assert join.by_candidate["a" * 64].cpcv_pass_rate == 0.75
        assert join.by_candidate["a" * 64].survivor_rank == 1

    def test_none_report_threads_none(self, tmp_path: Path) -> None:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        assert _call(tmp_path, _runner) is True
        assert captured["cascade_join"] is None

    def test_join_error_belt_logs_and_never_fails_the_sweep(self, tmp_path: Path) -> None:
        # A structurally-unusable report (empty sweep_id) trips the
        # CascadeJoinError belt: WARN + None threaded, the pass still runs.
        from brutex.engine.cascade_wire import CascadeWireReport

        bad = CascadeWireReport(sweep_id="", groups=())
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        with capture_logs() as logs:
            ok = _call(tmp_path, _runner, cascade_report=bad)
        assert ok is True
        assert captured["cascade_join"] is None
        (warn,) = [e for e in logs if e["event"] == "cli.results_depth.cascade_join_failed"]
        assert warn["error_type"] == "CascadeJoinError"


class TestExecSimProviderThreading:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001`: the winners-hook in-memory per-run_id
    verdict maps become TOTAL provider closures — a known run_id serves its
    EXACT verdict object, an unknown one the NAMED not_priced/not_in_replay_set
    degrade; a ``None`` map threads NO provider (the entry key stays ABSENT,
    artifact bytes byte-identical)."""

    def _captured(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        assert _call(tmp_path, _runner, **overrides) is True
        return captured

    def test_options_map_builds_a_total_provider_closure(self, tmp_path: Path) -> None:
        from brutex.options.exec_verdict import options_exec_not_priced

        verdict: dict[str, object] = {"status": "priced", "net_after_cost_paisa": 64_206}
        captured = self._captured(tmp_path, options_exec={"r-known": verdict})
        provider = captured["options_exec_provider"]
        assert provider is not None
        # The known run_id serves the EXACT verdict object (never a copy —
        # dict(options_exec) shallow-copies the map, values ride through).
        known = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-known")))
        assert known is verdict
        # An unknown run_id reads the NAMED degrade, never a wrong join.
        unknown = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-unknown")))
        assert unknown == options_exec_not_priced("not_in_replay_set")
        assert unknown == {"status": "not_priced", "reason": "not_in_replay_set"}
        # Only the WIRED map grows a provider.
        assert captured["futures_exec_provider"] is None

    def test_futures_map_builds_a_total_provider_closure(self, tmp_path: Path) -> None:
        from brutex.futures.leaderboard import futures_exec_not_priced

        verdict: dict[str, object] = {"status": "executed", "net_pnl_paisa": 749_625}
        captured = self._captured(tmp_path, futures_exec={"r-known": verdict})
        provider = captured["futures_exec_provider"]
        assert provider is not None
        known = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-known")))
        assert known is verdict
        unknown = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-gone")))
        assert unknown == futures_exec_not_priced("not_in_replay_set")
        assert unknown == {"status": "not_priced", "reason": "not_in_replay_set"}
        assert captured["options_exec_provider"] is None

    def test_none_maps_thread_no_providers(self, tmp_path: Path) -> None:
        """The byte-identical passthrough: a sim that never ran threads NO
        provider — build_top100 leaves the entry key ABSENT."""
        captured = self._captured(tmp_path)
        assert captured["options_exec_provider"] is None
        assert captured["futures_exec_provider"] is None

    def test_top100_preset_threads_the_resolved_token(self, tmp_path: Path) -> None:
        from brutex.engine.top100_artifact import DEFAULT_TOP100_PRESET

        default = self._captured(tmp_path)
        assert default["preset_name"] == DEFAULT_TOP100_PRESET
        explicit = self._captured(tmp_path, top100_preset="net_after_cost")
        assert explicit["preset_name"] == "net_after_cost"

    def test_fill_sensitivity_defaults_true_and_threads_false(self, tmp_path: Path) -> None:
        """DEC-FILL-EXEC-GRID-1M-001: the annotation half is production-ON by
        default; an explicit False threads through."""
        default = self._captured(tmp_path)
        assert default["fill_sensitivity"] is True
        off = self._captured(tmp_path, fill_sensitivity=False)
        assert off["fill_sensitivity"] is False


class TestResolveTop100Preset:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001`: the eager fail-loud --top100-preset law."""

    @pytest.mark.parametrize("token", [None, "", "   "])
    def test_none_and_blank_read_the_default(self, token: str | None) -> None:
        from brutex.engine.top100_artifact import DEFAULT_TOP100_PRESET

        assert cli._resolve_top100_preset(token) == DEFAULT_TOP100_PRESET

    def test_known_token_passes_through_stripped(self) -> None:
        assert cli._resolve_top100_preset(" composite ") == "composite"

    def test_unknown_token_refuses_naming_every_valid_value(self) -> None:
        from brutex.engine.top100_artifact import TOP100_PRESETS

        with pytest.raises(cli.CliUserError) as excinfo:
            cli._resolve_top100_preset("typo")
        message = str(excinfo.value)
        assert "unknown --top100-preset 'typo'" in message
        for preset in TOP100_PRESETS:
            assert preset in message


class TestMissingStoreThreading:
    """DEC-DEPTH-MISSING-STORE-LOUD-001 (#1925, review round 1 M2): the adapter
    threads the sweep's run-scoped FAILED allowance + the quarantined-prior
    IDENTITY set (round 2 M1) into the pass +
    mirrors the pass's OWN computed ``missing_store_unexplained`` onto stderr,
    while the fence (``return True``) is byte-preserved."""

    def test_expected_missing_is_threaded_to_the_pass(self, tmp_path: Path) -> None:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        _call(tmp_path, _runner, expected_missing=3)
        assert captured["expected_missing"] == 3

    def test_quarantined_labels_are_threaded_to_the_pass(self, tmp_path: Path) -> None:
        """Review round 2 M1: the durable quarantine IDENTITY set reaches the
        pass verbatim, so its per-tuple attribution can close the count-based
        masking."""
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        labels = frozenset({"NIFTY/1minute/BUY/k=1/sl=80/tp=50"})
        _call(tmp_path, _runner, expected_missing=1, quarantined_labels=labels)
        assert captured["quarantined_labels"] == labels

    def test_unexplained_missing_warns_on_stderr_with_sample(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _report(
            tuples_missing_store=5,
            missing_store_sample=("/lake/a.parquet", "/lake/b.parquet"),
            missing_store_unexplained=3,
        )
        ok = _call(tmp_path, lambda **_kwargs: report, expected_missing=2)
        assert ok is True  # the fence's "NEVER fails the sweep" contract holds
        err = capsys.readouterr().err
        assert (
            "3 bars-present, not-quarantined finalist store(s) missing beyond the "
            "sweep's run-scoped FAILED allowance"
        ) in err
        assert "missing=5" in err
        assert "no_bars=0" in err
        assert "quarantined=0" in err  # round 2 M1: the identity-explained count
        assert "failed_expected=2" in err
        assert "/lake/a.parquet" in err
        # Review round 1 M2: the run-scoped residual heuristic is disclosed.
        assert "under-attributed" in err
        # Review round 2 M1: the identity attribution is disclosed.
        assert "stale sidecar can no longer mask a deletion" in err

    def test_explained_missing_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _report(
            tuples_missing_store=2,
            missing_store_sample=("/lake/a.parquet",),
            missing_store_unexplained=0,
        )
        ok = _call(tmp_path, lambda **_kwargs: report, expected_missing=2)
        assert ok is True
        assert "finalist store(s) missing" not in capsys.readouterr().err

    def test_adapter_mirrors_the_pass_computed_count_never_a_recompute(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Review round 1 M2: all 5 misses are no-bars-EXPECTED (the pass
        computed unexplained=0). A local ``missing - expected`` recompute would
        falsely print 5 — silence proves the adapter mirrors the report's own
        ``missing_store_unexplained`` (ONE formula, owned by the pass)."""
        report = _report(
            tuples_missing_store=5,
            missing_expected_no_bars=5,
            missing_store_unexplained=0,
        )
        ok = _call(tmp_path, lambda **_kwargs: report, expected_missing=0)
        assert ok is True
        assert "finalist store(s) missing" not in capsys.readouterr().err

    def test_unexplained_without_sample_has_no_path_suffix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _report(
            tuples_missing_store=1, missing_store_sample=(), missing_store_unexplained=1
        )
        _call(tmp_path, lambda **_kwargs: report, expected_missing=0)
        err = capsys.readouterr().err.rstrip()
        assert "1 bars-present, not-quarantined finalist store(s) missing" in err
        assert err.endswith("rebuild")  # no ": <sample>" suffix when the sample is empty


class TestReferenceUnreadableWarn:
    """DEC-DEPTH-MISSING-STORE-LOUD-001 (#1925, review round 2 M4): the
    reference-loader reconcile's ``unreadable`` list is no longer DISCARDED —
    a corrupt/unreadable reference (VIX/1day/1m) month is surfaced LOUDLY so a
    holed reference series is never silently stamped canonical."""

    def test_warn_fires_and_names_the_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        with capture_logs() as logs:
            count = cli._warn_unreadable_reference_partitions(
                ("/lake/bars/NSE/INDEX/INDIAVIX/1minute/2024/02.parquet",),
                symbol="INDIAVIX",
                interval="1minute",
            )
        assert count == 1
        warns = [
            log for log in logs if log["event"] == "results_depth.reference_partition_unreadable"
        ]
        assert len(warns) == 1
        assert warns[0]["symbol"] == "INDIAVIX"
        assert warns[0]["interval"] == "1minute"
        assert warns[0]["count"] == 1
        err = capsys.readouterr().err
        assert "unreadable (corrupt) reference partition file(s) for INDIAVIX/1minute" in err
        assert "02.parquet" in err  # the file is NAMED

    def test_no_op_when_nothing_unreadable(self, capsys: pytest.CaptureFixture[str]) -> None:
        with capture_logs() as logs:
            count = cli._warn_unreadable_reference_partitions(
                (), symbol="INDIAVIX", interval="1minute"
            )
        assert count == 0
        assert not logs
        assert capsys.readouterr().err == ""

    def test_sample_is_capped_but_count_is_true(self, capsys: pytest.CaptureFixture[str]) -> None:
        n = cli._REFERENCE_UNREADABLE_WARN_CAP + 3
        files = tuple(f"/lake/{i:02d}.parquet" for i in range(n))
        with capture_logs() as logs:
            count = cli._warn_unreadable_reference_partitions(
                files, symbol="INDIAVIX", interval="1minute"
            )
        assert count == n  # true count, not the cap
        warn = next(
            log for log in logs if log["event"] == "results_depth.reference_partition_unreadable"
        )
        assert warn["count"] == n
        assert len(warn["files"]) == cli._REFERENCE_UNREADABLE_WARN_CAP  # sample capped
        assert "(+3 more)" in capsys.readouterr().err


class TestDepthQuarantinedLabels:
    """DEC-DEPTH-MISSING-STORE-LOUD-001 (#1925, review round 2 M1): the cli
    helper reads the durable ``.sweep_quarantine.json`` sidecar (#1608) via the
    COMMON loader, sweep-scoped + fail-soft."""

    def test_reads_the_sidecar_labels_for_the_sweep_id(self, tmp_path: Path) -> None:
        import json

        from brutex.engine.sweep_parallel import quarantine_sidecar_path

        sidecar = quarantine_sidecar_path(tmp_path)
        sidecar.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "index": 1,
                            "tuple": "NIFTY/1minute/BUY/k=1/sl=80/tp=50",
                            "sweep_id": "S1",
                        },
                        {"index": 2, "tuple": "OTHER", "sweep_id": "S2"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        labels = cli._depth_quarantined_labels(tmp_path, "S1")
        assert labels == frozenset({"NIFTY/1minute/BUY/k=1/sl=80/tp=50"})
        assert isinstance(labels, frozenset)

    def test_missing_sidecar_returns_the_empty_set(self, tmp_path: Path) -> None:
        assert cli._depth_quarantined_labels(tmp_path, "S1") == frozenset()


class TestReferenceLoaderUnreadableWiring:
    """MF4 wiring proof: the REAL ``_depth_reference_loader`` surfaces a corrupt
    reference month through ``_warn_unreadable_reference_partitions`` — the
    reconcile's ``unreadable`` list is no longer discarded at this seam."""

    def test_corrupt_reference_month_warns_and_load_degrades(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        shard = lake / "bars" / "NSE" / "INDEX" / "INDIAVIX" / "1minute" / "2024" / "01.parquet"
        shard.parent.mkdir(parents=True)
        shard.write_bytes(b"not a parquet file at all")  # corrupt reference month
        loader = cli._depth_reference_loader(
            slices={},
            lake_root=lake,
            catalog_path=tmp_path / "catalog.duckdb",
            exchange="NSE",
            segment="INDEX",
            start="2024-01-01",
            end="2024-02-01",
            interval="1minute",
        )
        with capture_logs() as logs:
            bars = loader("INDIAVIX")
        assert bars is None  # honest degrade — nothing readable, never fabricated
        assert [
            log for log in logs if log["event"] == "results_depth.reference_partition_unreadable"
        ]
        err = capsys.readouterr().err
        assert "unreadable (corrupt) reference partition file(s) for INDIAVIX/1minute" in err
        assert "01.parquet" in err


@dataclass(frozen=True, slots=True)
class _ResTrade:
    """A ``fold_resilience`` ``_TradeLike`` (frozen fields — pyright-clean)."""

    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _grid() -> Any:
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe(label="5minute", minutes=5)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _write_top100_with_resilience(out_root: Path, grid: Any) -> None:
    """Write a real top100.json (stamp + hash) whose entries carry a LIVE and
    a NULL resilience block — the on-disk artifact ``_print_resilience_lines``
    reads back."""
    from brutex.engine.resilience_streaks import fold_resilience, resilience_payload
    from brutex.engine.sweep_supervisor import grid_identity
    from brutex.engine.top100_artifact import TOP100_SCHEMA_VERSION, Top100Artifact, write_top100
    from brutex.engine.trade_store import sweep_run_dir

    def _trade(pnl: int, hour: int) -> _ResTrade:
        ts = datetime(2024, 1, 2, hour, 0, tzinfo=UTC)
        return _ResTrade(pnl_paisa=pnl, entry_ts=ts, exit_ts=ts)

    run_id = grid_identity(grid)
    live = fold_resilience(
        run_id,
        [_trade(-100, 10), _trade(-100, 11), _trade(50, 12)],
        parity_ok=True,
        start_equity_paisa=1_000_000,
        fingerprint="fp0",
    )

    def _entry(rank: int, max_losses: int, record: Any) -> dict[str, Any]:
        return {
            "rank": rank,
            "run_id": run_id,
            "mask_hex": "0" * 32,
            "conditions": [],
            "instrument": "NSE-NIFTY",
            "tf": "5m",
            "direction": "long",
            "arm": {"sl_paisa": None, "tp_paisa": None},
            "metrics": {"max_consecutive_losses": max_losses},
            "regime": {},
            "vix_band": None,
            "family_id": "fam",
            "family_members": ["fam"],
            "reasons": None,
            "margin_roi": None,
            "equity_ref": {"tuple": "t", "sidecar": "equity"},
            "resilience": resilience_payload(record),
        }

    artifact = Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset="min_dd_first",
        source={},
        baselines={},
        entries=(_entry(1, 2, live), _entry(2, 0, None)),
    )
    write_top100(sweep_run_dir(out_root, run_id).parent, artifact)


class TestResilienceLines:
    """``_print_resilience_lines`` — the additive CLI resilience surface."""

    def test_not_written_prints_nothing(self, tmp_path: Path) -> None:
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=tmp_path / "sweeps",
            grid=_grid(),
            report=_report(top100_written=False),
            printer=lines.append,
        )
        assert lines == []

    def test_grid_identity_failure_is_fenced_and_logged(self, tmp_path: Path) -> None:
        lines: list[str] = []
        with capture_logs() as logs:
            cli._print_resilience_lines(
                out_root=tmp_path / "sweeps",
                grid=SimpleNamespace(),  # grid_identity chokes → fenced
                report=_report(top100_written=True),
                printer=lines.append,
            )
        assert lines == []
        events = [log for log in logs if log["event"] == "cli.resilience_line.skipped"]
        assert len(events) == 1

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_global_stops_reraise(self, tmp_path: Path, exc_type: type[BaseException]) -> None:
        from brutex.engine import sweep_supervisor as supervisor

        def _boom(_grid: Any) -> str:
            raise exc_type

        original = supervisor.grid_identity
        supervisor.grid_identity = _boom  # type: ignore[assignment]
        try:
            with pytest.raises(exc_type):
                cli._print_resilience_lines(
                    out_root=tmp_path / "sweeps",
                    grid=_grid(),
                    report=_report(top100_written=True),
                    printer=lambda _line: None,
                )
        finally:
            supervisor.grid_identity = original  # type: ignore[assignment]

    def test_missing_artifact_prints_the_unreadable_line(self, tmp_path: Path) -> None:
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=tmp_path / "sweeps",
            grid=_grid(),
            report=_report(top100_written=True),  # nothing on disk → load None
            printer=lines.append,
        )
        assert len(lines) == 1
        assert "top100 unreadable for the summary lines" in lines[0]

    def test_empty_entries_prints_nothing(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.engine.top100_artifact import (
            TOP100_SCHEMA_VERSION,
            Top100Artifact,
            write_top100,
        )
        from brutex.engine.trade_store import sweep_run_dir

        grid = _grid()
        out_root = tmp_path / "sweeps"
        run_id = grid_identity(grid)
        write_top100(
            sweep_run_dir(out_root, run_id).parent,
            Top100Artifact(
                schema_version=TOP100_SCHEMA_VERSION,
                run_id=run_id,
                preset="min_dd_first",
                source={},
                baselines={},
                entries=(),
            ),
        )
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        assert lines == []

    def test_live_and_null_entries_print_lines_and_the_null_footer(self, tmp_path: Path) -> None:
        grid = _grid()
        out_root = tmp_path / "sweeps"
        _write_top100_with_resilience(out_root, grid)
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        # header + 2 entry lines + the honest NULL footer.
        assert len(lines) == 4
        assert "losing-streak endurance" in lines[0]
        # rank-1 LIVE line names the longest streak (by count) AND the worst
        # streak (by ₹) as DISTINCT facts (here both are the same len-2 run).
        assert "longest streak 2 by count" in lines[1]
        assert "worst streak (by ₹) 2 losses" in lines[1]
        assert "signal-only gross per-unit" in lines[1]
        # rank-2 NULL line reads the honest em-dash.
        assert "— (parity-failed or no trades)" in lines[2]
        assert "⚠ 1 of 2 shown had NO resilience stats" in lines[3]

    def test_all_live_entries_print_no_null_footer(self, tmp_path: Path) -> None:
        from brutex.engine.resilience_streaks import fold_resilience, resilience_payload
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.engine.top100_artifact import (
            TOP100_SCHEMA_VERSION,
            Top100Artifact,
            write_top100,
        )
        from brutex.engine.trade_store import sweep_run_dir

        grid = _grid()
        out_root = tmp_path / "sweeps"
        run_id = grid_identity(grid)
        ts = datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
        live = fold_resilience(
            run_id,
            [_ResTrade(pnl_paisa=-100, entry_ts=ts, exit_ts=ts)],
            parity_ok=True,
            start_equity_paisa=1_000_000,
            fingerprint="fp0",
        )
        entry = {
            "rank": 1,
            "run_id": run_id,
            "mask_hex": "0" * 32,
            "conditions": [],
            "instrument": "NSE-NIFTY",
            "tf": "5m",
            "direction": "long",
            "arm": {"sl_paisa": None, "tp_paisa": None},
            "metrics": {"max_consecutive_losses": 1},
            "regime": {},
            "vix_band": None,
            "family_id": "fam",
            "family_members": ["fam"],
            "reasons": None,
            "margin_roi": None,
            "equity_ref": {"tuple": "t", "sidecar": "equity"},
            "resilience": resilience_payload(live),
        }
        write_top100(
            sweep_run_dir(out_root, run_id).parent,
            Top100Artifact(
                schema_version=TOP100_SCHEMA_VERSION,
                run_id=run_id,
                preset="min_dd_first",
                source={},
                baselines={},
                entries=(entry,),
            ),
        )
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        assert len(lines) == 2  # header + one LIVE line, NO null footer
        assert not any("had NO resilience stats" in line for line in lines)
