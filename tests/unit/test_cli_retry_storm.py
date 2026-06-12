# pyright: reportPrivateUsage=none
"""Tests for the ``brutex fetch`` command family (DEC-RATE-STORM-WATCHDOG-001 +
DEC-COV-CLI-HARNESS-001).

Covers the retry-storm flag wiring — argument parsing, dispatch threading,
validation, and the :func:`brutex.cli._arm_retry_storm_guard` factory that
builds the guard, opens the hash-chained audit writer, and returns the ingest
progress hook — plus ``cmd_fetch``'s execution paths: the offline ``--dry-run``
synthetic pipeline, ``--inspect``, ``--instruments``, the retired
``--universe``, the live wiring (adapter/preflight/ingest faked — no network,
no Dhan credentials), and the bounded ``--until-clean`` rounds.
"""

from __future__ import annotations

import contextlib
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli
from brutex.orchestrator.audit import read_entries, verify_chain


@dataclass(frozen=True)
class FakeInstrument:
    """Minimal stand-in exposing the ``symbol_name`` the guard registers."""

    symbol_name: str


# ── Argument parsing ────────────────────────────────────────────────────


def test_parser_defaults_max_retry_minutes_to_none() -> None:
    parser = cli.build_parser()
    without = parser.parse_args(["fetch", "--dry-run"])
    with_flag = parser.parse_args(["fetch", "--dry-run", "--max-retry-minutes", "30"])
    assert without.max_retry_minutes is None
    assert with_flag.max_retry_minutes == 30


def test_main_dispatch_threads_max_retry_minutes_into_cmd_fetch() -> None:
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run", "--max-retry-minutes", "45"])
    _, kwargs = fetch.call_args
    assert kwargs["max_retry_minutes"] == 45


def test_main_dispatch_max_retry_minutes_defaults_none() -> None:
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run"])
    _, kwargs = fetch.call_args
    assert kwargs["max_retry_minutes"] is None


# ── Validation ──────────────────────────────────────────────────────────


def test_cmd_fetch_rejects_non_positive_minutes(tmp_path: Path) -> None:
    # Validation runs before any heavy ingest import, so a bad value returns
    # the user-error code immediately.
    rc = cli.cmd_fetch(
        tmp_path,
        None,
        None,
        "all",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=5,
        max_retry_minutes=0,
    )
    assert rc == cli.EXIT_USER_ERROR


# ── _arm_retry_storm_guard factory ──────────────────────────────────────


def test_arm_guard_returns_none_when_flag_unset(tmp_path: Path) -> None:
    with contextlib.ExitStack() as stack:
        hook = cli._arm_retry_storm_guard(
            max_retry_minutes=None,
            lake_root=tmp_path,
            instruments=[FakeInstrument("NIFTY")],
            intervals=["1minute"],
            stack=stack,
        )
    assert hook is None


def test_arm_guard_builds_hook_audits_terminal_failure(tmp_path: Path) -> None:
    with contextlib.ExitStack() as stack:
        hook = cli._arm_retry_storm_guard(
            max_retry_minutes=30,
            lake_root=tmp_path,
            instruments=[FakeInstrument("NIFTY")],
            intervals=["1minute", "1day"],
            stack=stack,
        )
        assert hook is not None
        # A success outcome must not give up; a terminal failure must.
        hook("NIFTY 1minute", 375, False, False)
        hook("NIFTY 1minute", 0, True, False)
        hook("NIFTY 1day", 0, True, False)  # 2/2 windows of NIFTY → 100% → symbol row

    # The audit file was opened under <lake>/audit/ and its chain verifies.
    audit_files = list((tmp_path / "audit").glob("retry-storm-*.jsonl"))
    assert len(audit_files) == 1
    audit_path = audit_files[0]
    assert verify_chain(audit_path) == 3  # 2 give-ups + 1 symbol-failure
    actions = [e.action for e in read_entries(audit_path)]
    assert actions == ["RETRY_GIVE_UP", "RETRY_GIVE_UP", "SYMBOL_WINDOW_FAILURE"]


# ── cmd_fetch interval / mode validation ────────────────────────────────


def test_resolve_dhan_fetch_intervals_aliases() -> None:
    """``all``/``1m``/``1minute``/``1d``/``1day`` map to the fetched intervals."""
    assert cli._resolve_dhan_fetch_intervals("all") == ["1minute", "1day"]
    assert cli._resolve_dhan_fetch_intervals("1m") == ["1minute"]
    assert cli._resolve_dhan_fetch_intervals("1minute") == ["1minute"]
    assert cli._resolve_dhan_fetch_intervals("1d") == ["1day"]
    assert cli._resolve_dhan_fetch_intervals("1day") == ["1day"]


def test_resolve_dhan_fetch_intervals_rejects_unfetched() -> None:
    """A non-fetched timeframe raises the pinned DEC-DHAN-1M-ONLY-001 error."""
    with pytest.raises(ValueError, match="not fetched on Dhan"):
        cli._resolve_dhan_fetch_intervals("25minute")


def test_cmd_fetch_unfetched_interval_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--interval 5minute`` refuses with the pinned remediation message."""
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "5minute",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_USER_ERROR
    assert capsys.readouterr().err == (
        "[brutex] --interval '5minute' is not fetched on Dhan — fetch 1minute/1day only "
        "(the 19 intraday timeframes are resampled from the 1-minute base). "
        "Use --interval 1minute, 1day, or all.\n"
    )


def test_cmd_fetch_universe_is_retired(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--universe`` is retired (DEC-DHAN-FINALIZE-001) — pinned refusal, exit 1."""
    rc = cli.cmd_fetch(
        tmp_path,
        None,
        None,
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
        universe=True,
    )
    assert rc == cli.EXIT_USER_ERROR
    assert capsys.readouterr().err == (
        "[brutex] --universe is retired (legacy three-phase ingest removed); "
        "use `brutex fetch --instruments` to load the master, then the "
        "/universe dashboard buttons to pull spot + options\n"
    )


def test_cmd_fetch_instruments_only_loads_master(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--instruments`` loads the keyless master, prints the counts, and exits 0."""
    counts = {
        "parsed": 10,
        "filtered": 6,
        "quarantined": 1,
        "index_count": 2,
        "stock_count": 3,
        "total": 5,
    }
    with mock.patch.object(cli, "_run_instrument_master_load", return_value=counts) as loader:
        rc = cli.cmd_fetch(
            tmp_path,
            None,
            None,
            "all",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
            instruments=True,
        )
    assert rc == cli.EXIT_OK
    loader.assert_called_once_with(tmp_path)
    assert capsys.readouterr().out == (
        "[brutex] fetch --instruments → keyless public instrument master (no keys)\n"
        "[brutex] instruments loaded — parsed=10 filtered=6 quarantined=1 "
        "· indices=2 stocks=3 total=5\n"
        f"[brutex] lake: {tmp_path}\n"
    )


# ── cmd_fetch dry-run execution paths (offline synthetic pipeline) ──────


def test_cmd_fetch_dry_run_synthetic_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` runs the full synthetic fetch→sink pipeline into the lake.

    Also arms the retry-storm guard (>=1 minutes valid) and exercises the
    legacy ``--window-days`` ignored-note path.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=7,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
        max_retry_minutes=30,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "[brutex] --window-days is ignored on Dhan (1-minute is tiled internally)" in (
        captured.err
    )
    assert "[brutex] fetch --dry-run → synthetic candles" in captured.err
    assert "retry-storm guard armed — give up after 30 min" in captured.err
    assert (
        "[brutex] fetch done — windows=3 bars_appended=6 failed_pairs=0 transient=0"
    ) in captured.out
    assert f"[brutex] lake: {tmp_path}" in captured.out
    assert (tmp_path / "catalog.duckdb").exists()
    assert list((tmp_path / "audit").glob("retry-storm-*.jsonl"))


def test_cmd_fetch_dry_run_inspect_persists_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--inspect`` routes to the NullBarSink: no catalog, no lake, no audit."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=True,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "[brutex] --inspect is not yet ported to the Dhan adapter" in captured.err
    assert "[brutex] fetch done — windows=3 bars_appended=6 failed_pairs=0 transient=0" in (
        captured.out
    )
    assert list(tmp_path.iterdir()) == []


# ── cmd_fetch live wiring (adapter / preflight / ingest faked) ──────────


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``DhanIngestReport`` surface the fetch summary printing reads."""

    windows: int
    bars_appended: int
    failed_pairs: int
    transient_failures: int
    still_failed_windows: int


@dataclass(frozen=True)
class _FakeRetryOutcome:
    """Duck-typed ``RetryOutcome`` surface the auto-retry summary reads."""

    rounds_run: int
    recovered_windows: int
    final_still_failed: int


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` (report + retry + final residual)."""

    report: _FakeIngestReport
    retry: _FakeRetryOutcome | None

    @property
    def still_failed_windows(self) -> int:
        """Mirror the real wrapper: the retry residual wins when a retry ran."""
        if self.retry is not None:
            return self.retry.final_still_failed
        return self.report.still_failed_windows


def test_cmd_fetch_live_inspect_reports_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live (non-dry-run) wiring builds the rate-limited adapter and
    surfaces a still-failing pull as an internal error with the retry tally.

    Everything network-shaped is faked: instrument resolution, the live
    adapter build, the token preflight, and the ingest engine itself.
    ``--inspect`` keeps the run persistence-free (NullBarSink, no ledger).
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    from brutex.data.sink import NullBarSink

    adapter = object()
    monkeypatch.setattr(
        cli, "_resolve_brute_force_instruments", lambda lake: [FakeInstrument("NIFTY")]
    )
    monkeypatch.setattr(
        cli,
        "_build_live_dhan_spot",
        lambda lake, instruments, catalog=None: (adapter, object()),
    )
    preflight = importlib.import_module("brutex.adapters.dhan.token_preflight")
    monkeypatch.setattr(preflight, "probe_token_data_live", lambda adapter, instrument: "alive")
    monkeypatch.setattr(preflight, "preflight_pull_verdict", lambda probe: None)

    run_ingest = importlib.import_module("brutex.adapters.dhan.run_ingest")
    result = _FakeIngestResult(
        report=_FakeIngestReport(
            windows=2,
            bars_appended=0,
            failed_pairs=1,
            transient_failures=1,
            still_failed_windows=1,
        ),
        retry=_FakeRetryOutcome(rounds_run=2, recovered_windows=0, final_still_failed=1),
    )
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        """Record the wiring the CLI threaded in; return the failing report."""
        seen["adapter"] = adapter_arg
        seen["sink"] = sink_arg
        seen.update(kwargs)
        return result

    monkeypatch.setattr(run_ingest, "run_dhan_spot_ingest_with_retry", fake_ingest)

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=True,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_INTERNAL_ERROR
    captured = capsys.readouterr()
    assert "[brutex] fetch (live Dhan)" in captured.err
    assert "[brutex] fetch done — windows=2 bars_appended=0 failed_pairs=1 transient=1" in (
        captured.out
    )
    assert "[brutex] auto-retry — rounds=2 recovered_windows=0 still_failed=1" in captured.out
    assert seen["adapter"] is adapter
    assert isinstance(seen["sink"], NullBarSink)  # --inspect swaps in the null sink
    assert seen["auto_retry"] is True
    assert seen["ledger"] is None  # --inspect never touches the real ledger
    assert callable(seen["pace"])
    assert callable(seen["on_rate_limit"])
    assert callable(seen["on_success"])


def test_cmd_fetch_live_persisting_pull_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean live pull (no --inspect) keeps the persisting sink, threads the
    durable window ledger + ingest audit, and exits 0.

    Same fakes as the inspect twin (no network); the persisting branch
    additionally opens the real catalog, ledger, and hash-chained audit files.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.universe import BRUTE_FORCE_INDICES

    sink = object()
    # The durable ledger reconcile reads real Instrument fields (exch_id /
    # segment enums), so resolve a full synthetic index instrument.
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    monkeypatch.setattr(
        cli, "_build_live_dhan_spot", lambda lake, instruments, catalog=None: (object(), sink)
    )
    preflight = importlib.import_module("brutex.adapters.dhan.token_preflight")
    monkeypatch.setattr(preflight, "probe_token_data_live", lambda adapter, instrument: "alive")
    monkeypatch.setattr(preflight, "preflight_pull_verdict", lambda probe: None)

    run_ingest = importlib.import_module("brutex.adapters.dhan.run_ingest")
    result = _FakeIngestResult(
        report=_FakeIngestReport(
            windows=1,
            bars_appended=375,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        ),
        retry=None,
    )
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        """Record the persisting wiring; return the clean report."""
        seen["sink"] = sink_arg
        seen.update(kwargs)
        return result

    monkeypatch.setattr(run_ingest, "run_dhan_spot_ingest_with_retry", fake_ingest)

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "[brutex] fetch (live Dhan)" in captured.err
    assert "[brutex] fetch done — windows=1 bars_appended=375 failed_pairs=0 transient=0" in (
        captured.out
    )
    assert seen["sink"] is sink  # the persisting live sink is NOT swapped
    assert seen["ledger"] is not None  # the durable window ledger is threaded
    assert seen["audit"] is not None  # the hash-chained ingest audit is threaded
    assert (tmp_path / "catalog.duckdb").exists()
    assert (tmp_path / "audit" / "spot-ingest.jsonl").exists()


# ── cmd_fetch --until-clean (bounded rounds, DEC-TOKEN-PREFLIGHT-001) ───


def test_cmd_fetch_until_clean_reaches_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean dry-run drives the REAL until-clean engine to ``stop_clean``."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
        until_clean=True,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "[brutex] until-clean — verdict=stop_clean rounds=1 still_failed=0" in captured.out
    assert "Regenerate the 24-hour token" not in captured.err


def test_cmd_fetch_until_clean_dead_token_needs_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead-token outcome prints the loud auth_expired verdict and exits 2."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    until_clean_mod = importlib.import_module("brutex.adapters.dhan.until_clean")
    budgets: list[int] = []

    def fake_run_until_clean(round_runner: Any, *, max_rounds: int) -> Any:
        """Drive ONE real round, then report the loop halted on a dead token."""
        budgets.append(max_rounds)
        round_runner()
        return until_clean_mod.UntilCleanOutcome(
            verdict="stop_dead",
            rounds_run=1,
            rounds=(
                until_clean_mod.UntilCleanRound(
                    still_failed_windows=4, auth_aborted=True, auth_cause="HTTP 401"
                ),
            ),
        )

    monkeypatch.setattr(until_clean_mod, "run_until_clean", fake_run_until_clean)
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
        until_clean=True,
        until_clean_rounds=2,
    )
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert budgets == [2]  # --until-clean-rounds overrides DEFAULT_MAX_ROUNDS
    captured = capsys.readouterr()
    assert "[brutex] until-clean — verdict=stop_dead rounds=1 still_failed=4" in captured.out
    assert "Dhan token expired mid-pull" in captured.err
    assert "Regenerate the 24-hour token" in captured.err
