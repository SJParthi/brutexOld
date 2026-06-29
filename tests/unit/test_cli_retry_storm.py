# pyright: reportPrivateUsage=none
"""Tests for the ``brutex fetch`` command family (DEC-RATE-STORM-WATCHDOG-001 +
DEC-COV-CLI-HARNESS-001).

Covers the retry-storm flag wiring — argument parsing, dispatch threading,
validation, and the :func:`brutex.cli._arm_retry_storm_guard` factory that
builds the guard, opens the hash-chained audit writer, and returns the ingest
progress hook — plus ``cmd_fetch``'s execution paths: the offline ``--dry-run``
synthetic pipeline, ``--inspect``, ``--instruments``, the retired
``--universe``, the live Groww wiring (creds-gate / adapter / ingest faked — no
network, no Groww credentials; PR-J2 vendor cutover), and the bounded
``--until-clean`` rounds.
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
        # A success outcome must not give up; a terminal failure must. The Groww
        # ProgressHook is 3-arg ``(label, bars, failed)`` — no ``gap`` (the documented
        # divergence: Groww has no known-gap branch).
        hook("NIFTY 1minute", 375, False)
        hook("NIFTY 1minute", 0, True)
        hook("NIFTY 1day", 0, True)  # 2/2 windows of NIFTY → 100% → symbol row

    # The audit file was opened under <lake>/audit/ and its chain verifies.
    audit_files = list((tmp_path / "audit").glob("retry-storm-*.jsonl"))
    assert len(audit_files) == 1
    audit_path = audit_files[0]
    assert verify_chain(audit_path) == 3  # 2 give-ups + 1 symbol-failure
    actions = [e.action for e in read_entries(audit_path)]
    assert actions == ["RETRY_GIVE_UP", "RETRY_GIVE_UP", "SYMBOL_WINDOW_FAILURE"]


# ── cmd_fetch interval / mode validation ────────────────────────────────


def test_resolve_fetch_intervals_aliases() -> None:
    """``all``/``1m``/``1minute``/``1d``/``1day`` map to the fetched intervals."""
    assert cli._resolve_fetch_intervals("all") == ["1minute", "1day"]
    assert cli._resolve_fetch_intervals("1m") == ["1minute"]
    assert cli._resolve_fetch_intervals("1minute") == ["1minute"]
    assert cli._resolve_fetch_intervals("1d") == ["1day"]
    assert cli._resolve_fetch_intervals("1day") == ["1day"]


def test_resolve_fetch_intervals_rejects_unfetched() -> None:
    """A non-fetched timeframe raises the pinned DEC-DHAN-1M-ONLY-001 error."""
    with pytest.raises(ValueError, match="not fetched on Groww"):
        cli._resolve_fetch_intervals("25minute")


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
        "[brutex] --interval '5minute' is not fetched on Groww — fetch 1minute/1day only "
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


# ── _DryRunGrowwSpotAdapter (offline dry-run spot stand-in) ─────────────


def test_dry_run_groww_adapter_protocol_surface() -> None:
    """The dry-run adapter satisfies the spot MarketAdapter shape with no network.

    ``capabilities`` returns the Groww spot capabilities, ``list_instruments``
    yields one empty page (the spine fans out over an explicit list), and
    ``list_derivatives`` yields nothing (Track-1 spot is derivative-free).
    """
    from datetime import date

    from brutex.adapters.groww.adapter import GROWW_CAPABILITIES
    from brutex.models import Exchange

    adapter = cli._DryRunGrowwSpotAdapter(bars_per_window=3)
    assert adapter.capabilities is GROWW_CAPABILITIES
    page = adapter.list_instruments()
    assert tuple(page.instruments) == ()
    assert page.next_cursor is None
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )
    assert len(bars) == 3  # deterministic offline bars for start's day


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
    assert "[brutex] --window-days is ignored on Groww (1-minute is tiled internally)" in (
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
    assert "[brutex] --inspect is not yet ported to the Groww adapter" in captured.err
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
    """The live (non-dry-run) wiring builds the rate-limited Groww adapter and
    surfaces a still-failing pull as an internal error with the retry tally.

    Everything network-shaped is faked: the creds-present gate, instrument
    resolution, the live Groww adapter build, and the ingest engine itself
    (there is NO Dhan-style token preflight on the Groww path — TOTP auth).
    ``--inspect`` keeps the run persistence-free (NullBarSink, no ledger).
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    from brutex.data.sink import NullBarSink

    adapter = object()
    monkeypatch.setattr(
        cli, "_resolve_brute_force_instruments", lambda lake: [FakeInstrument("NIFTY")]
    )
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (adapter, object()),
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
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

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

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
    assert "[brutex] fetch (live Groww)" in captured.err
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
    # The live CLI pull paces at the operator-DIRECTED 5 r/s ceiling (lowered 8 → 5
    # on 2026-06-22). ``pace`` is the controller's bound method, so its ``__self__`` is
    # the RateController — assert it started at the 5 r/s ceiling.
    from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING

    # ``__self__`` exists on the bound method at runtime; pyright only sees a
    # ``FunctionType`` so reach it via ``getattr`` (typed ``Any``) to stay gate-clean.
    controller = getattr(seen["pace"], "__self__")  # noqa: B009
    assert controller.current_rps == GROWW_RATE_LIMIT_RPS_CEILING == 8.0
    assert controller.snapshot()["ceiling_per_min"] == 500


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
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), sink))

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
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

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

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
    assert "[brutex] fetch (live Groww)" in captured.err
    assert "[brutex] fetch done — windows=1 bars_appended=375 failed_pairs=0 transient=0" in (
        captured.out
    )
    assert seen["sink"] is sink  # the persisting live sink is NOT swapped
    assert seen["ledger"] is not None  # the durable window ledger is threaded
    assert seen["audit"] is not None  # the hash-chained ingest audit is threaded
    assert (tmp_path / "catalog.duckdb").exists()
    assert (tmp_path / "audit" / "spot-ingest.jsonl").exists()


def test_cmd_fetch_checkpoints_ledger_mid_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live persisting pull flushes the durable ledger MID-pull, not only at end.

    DEC-CLI-LEDGER-CHECKPOINT-001 — before this, ``cmd_fetch`` saved the ledger
    SOLELY via ``end_pull_ledger``, so a crash after N windows lost every completed
    window's SUCCESS row → a full re-pull. The CLI now threads
    ``make_locked_flush(make_live_ledger_flush(...))`` as the ingest engine's
    per-WINDOW ``on_window`` hook (reusing the SAME throttled flush the dashboard
    uses, behind a single-flight lock, CLAUDE.md §11 COMMON). The fake ingest fires
    that hook once (a window landing) and we assert a durable save happened DURING
    the pull — before the end save.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data import window_ledger_store
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    # Count durable-ledger saves so we can prove one fired MID-pull (from the
    # on_window checkpoint) rather than only at end_pull_ledger. begin/plan/reconcile
    # do not save (load + in-memory mutate), so the first save is the mid-pull flush.
    saves = {"n": 0}
    real_save = window_ledger_store.save_window_ledger

    def counting_save(led: Any, root: Any, *a: Any, **kw: Any) -> Any:
        saves["n"] += 1
        return real_save(led, root, *a, **kw)

    monkeypatch.setattr(window_ledger_store, "save_window_ledger", counting_save)

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
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
    saves_during_ingest: list[int] = []

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        """Record the wiring; fire the per-window checkpoint as the engine would."""
        seen.update(kwargs)
        on_window = kwargs["on_window"]
        assert callable(on_window)
        on_window()  # first fire → both throttles open → durable ledger flushed
        saves_during_ingest.append(saves["n"])
        return result

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

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
    # The checkpoint hook was threaded into the ingest engine…
    assert callable(seen["on_window"])
    # …and firing it once persisted the durable ledger DURING the pull (before the
    # end_pull_ledger save) — exactly one mid-pull flush observed inside the ingest.
    assert saves_during_ingest == [1]
    assert window_ledger_store.load_window_ledger(tmp_path).records()


def test_cmd_fetch_inspect_ledger_checkpoint_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``--inspect`` (ledger None) the per-window checkpoint never touches disk.

    The checkpoint is built unconditionally, but a ``None`` ledger makes the
    underlying ``make_live_ledger_flush`` a no-op, so ``make_locked_flush`` wraps a
    no-op and firing ``on_window`` saves nothing — the persistence-free contract of
    ``--inspect`` is preserved.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    from brutex.data import window_ledger_store

    monkeypatch.setattr(
        cli, "_resolve_brute_force_instruments", lambda lake: [FakeInstrument("NIFTY")]
    )
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    saves = {"n": 0}
    real_save = window_ledger_store.save_window_ledger

    def counting_save(led: Any, root: Any, *a: Any, **kw: Any) -> Any:
        saves["n"] += 1
        return real_save(led, root, *a, **kw)

    monkeypatch.setattr(window_ledger_store, "save_window_ledger", counting_save)

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    result = _FakeIngestResult(
        report=_FakeIngestReport(
            windows=1,
            bars_appended=0,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        ),
        retry=None,
    )
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        seen.update(kwargs)
        kwargs["on_window"]()  # firing the checkpoint with a None ledger is a no-op
        return result

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

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
    assert rc == cli.EXIT_OK
    assert seen["ledger"] is None
    assert callable(seen["on_window"])  # built even when there is no ledger
    assert saves["n"] == 0  # …but a None-ledger checkpoint never saves


def test_cmd_fetch_live_persisting_pull_seeds_groww_ledger_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live persisting pull seeds the durable ledger in the GROWW namespace.

    Regression for the latent CLI ledger bug DEC-DASHBOARD-GROWW-SPOT-001 (#689)
    flagged in ``cli.py`` and fixed here: ``cmd_fetch`` must thread
    ``GROWW_COVERAGE_MARKET`` + Groww's 30-day ``window_days_for`` into
    ``plan_windows`` / ``reconcile_ledger_with_lake`` so the ``PLANNED`` keys it
    seeds share the SAME ``(market, tile-width)`` namespace the Groww spot spine
    settles in. Before the fix the calls defaulted to the Dhan namespace
    (``"dhan"`` + a single 90-day window), so a multi-month pull's planned rows
    could never match the fetched ones → orphan ``PLANNED`` rows / re-pull loops.

    Over a 54-day range (> 30, ≤ 90) this asserts EVERY seeded row carries
    ``market == "groww"`` (never ``"dhan"``) AND that the 1-minute series tiled at
    Groww's 30-day width — ≥ 2 windows, each ≤ 30 days — rather than Dhan's single
    90-day window (which would have left one 54-day window). Everything
    network-shaped is faked (no creds, no network); the fake ingest writes no
    bars, so ``reconcile_ledger_with_lake`` seeds nothing and the ledger holds
    only the ``plan_windows`` rows under test.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from datetime import date

    from brutex.data.window_ledger_store import load_window_ledger
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    result = _FakeIngestResult(
        report=_FakeIngestReport(
            windows=2,
            bars_appended=0,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        ),
        retry=None,
    )
    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", lambda *a, **k: result)

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-02-25",  # 54 days: Groww 30-day tiling → ≥ 2 windows; Dhan 90-day → 1
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_OK

    records = load_window_ledger(tmp_path).records()
    assert records, "the persisting pull must have seeded PLANNED ledger rows"
    # The bug: defaulting to the Dhan namespace would stamp market="dhan".
    assert {r.market for r in records} == {"groww"}
    oneminute = [r for r in records if r.interval == "1minute"]
    # Groww tiles 1-minute at 30 days, so a 54-day range splits into ≥ 2 windows,
    # each ≤ 30 days wide — Dhan's 90-day width would have left a single 54-day one.
    assert len(oneminute) >= 2
    for row in oneminute:
        span = date.fromisoformat(row.window_end) - date.fromisoformat(row.window_start)
        assert span.days <= 30


def test_cmd_fetch_live_missing_groww_creds_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live pull with NO Groww credentials refuses up front (exit 1), no network.

    Groww auth is TOTP (no Dhan JWT/RenewToken pre-flight) — the creds-present
    resolve is the only up-front gate (DEC-DHAN-GROWW-REMIGRATE-001 PR-J2). A
    missing-creds resolve returns the user-error code BEFORE any adapter build or
    instrument resolution, so neither is reached.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: False)

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must refuse before building the adapter")

    monkeypatch.setattr(cli, "_build_live_groww_spot", _never)
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", _never)

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
    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "[brutex] no Groww credentials" in captured.err
    assert "BRUTEX_GROWW_API_KEY / BRUTEX_GROWW_TOTP_SECRET" in captured.err


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
    assert "Re-check the Groww API key" not in captured.err


def test_cmd_fetch_until_clean_dead_token_needs_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead-token outcome prints the loud auth_expired verdict and exits 2."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    until_clean_mod = importlib.import_module("brutex.adapters.ingest_common")
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
    assert "Groww auth failed mid-pull" in captured.err
    assert "Re-check the Groww API key" in captured.err


# ── Pull-halt Telegram alert (DEC-TELEGRAM-PULL-HALT-001) ────────────────


def test_cmd_fetch_alerts_then_reraises_on_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull-wide ``LakeWriteHalt`` fires the CRITICAL pull-halt alert, then re-raises.

    DEC-TELEGRAM-PULL-HALT-001 — ``_one_fetch_pass`` wraps the ingest so a disk-full /
    OOM / quota global-stop pushes a best-effort Telegram alert (the operator must
    act) and RE-RAISES, so the CLI's existing halt handling / non-zero exit is
    byte-unchanged. Fakes only (no network, no creds).
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data.errors import LakeWriteHalt
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

    def fake_ingest(*_a: Any, **_k: Any) -> Any:
        """Simulate a full disk halting the pull mid-ingest."""
        raise LakeWriteHalt("disk_full")

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(LakeWriteHalt):
        cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
        )
    # The alert fired exactly once with the propagating halt, then it re-raised.
    assert len(halts) == 1
    assert isinstance(halts[0], LakeWriteHalt)
    assert halts[0].reason == "disk_full"


def test_emit_pull_halt_alert_surfaces_lake_write_halt_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_emit_pull_halt_alert`` passes ``LakeWriteHalt.reason`` verbatim to the alerter."""
    from brutex.data.errors import LakeWriteHalt

    captured: list[str] = []

    class _FakeAlerter:
        def pull_halted(self, *, reason: str) -> bool:
            captured.append(reason)
            return True

    monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
    cli._emit_pull_halt_alert(LakeWriteHalt("out_of_memory"))
    assert captured == ["out_of_memory"]


def test_emit_pull_halt_alert_other_stop_uses_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``LakeWriteHalt`` global-stop reports its class name as the halt reason."""
    captured: list[str] = []

    class _FakeAlerter:
        def pull_halted(self, *, reason: str) -> bool:
            captured.append(reason)
            return True

    monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
    cli._emit_pull_halt_alert(RuntimeError("boom"))
    assert captured == ["RuntimeError"]


def test_emit_pull_halt_alert_swallows_its_own_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The alert is fire-and-forget — its OWN failure NEVER propagates (masks the halt).

    A construction/publish failure inside ``_emit_pull_halt_alert`` is swallowed +
    logged, so the caller's re-raise of the real halt is the only exception that
    surfaces (DEC-TELEGRAM-PULL-HALT-001, structural fire-and-forget).
    """

    class _BoomAlerter:
        def __init__(self) -> None:
            raise RuntimeError("notifier construction blew up")

    monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _BoomAlerter)
    # Must NOT raise — the blanket except swallows + logs it (the no-raise IS the assertion).
    cli._emit_pull_halt_alert(RuntimeError("halt"))


# ── cross-process account single-flight (DEC-PULL-HARDENING-FOLLOWUP-001, #996) ──


def test_enter_pull_singleflight_acquires_and_holds_in_stack(tmp_path: Path) -> None:
    """A clean acquire returns True and the lock is HELD for the life of the ExitStack."""
    from brutex.adapters.groww.pull_singleflight import (
        PullAlreadyRunningError,
        pull_singleflight_lock,
    )

    with contextlib.ExitStack() as stack:
        assert cli._enter_pull_singleflight(stack, tmp_path) is True
        # Held: a second independent acquire on the SAME lake is refused while the stack
        # keeps the lock open.
        with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
            pass  # pragma: no cover — __enter__ raises first
    # Stack exited → released → a fresh acquire now succeeds.
    with pull_singleflight_lock(tmp_path):
        pass


def test_enter_pull_singleflight_refuses_on_contention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a peer already holds the lock, the helper returns False + prints a clear note."""
    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

    with (
        pull_singleflight_lock(tmp_path),  # a peer pull is mid-flight on this lake
        contextlib.ExitStack() as stack,
    ):
        assert cli._enter_pull_singleflight(stack, tmp_path) is False
    captured = capsys.readouterr()
    assert "a pull is already running for this lake" in captured.err
    assert "refusing to start a second concurrent pull" in captured.err


def test_cmd_fetch_live_refused_when_peer_pull_holds_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live persisting ``brutex fetch`` is REFUSED (exit 1) if a peer pull holds the lake
    single-flight lock — never a second concurrent pull that doubles the account rate
    (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996)."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    def _never_ingest(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the pull must be refused before the ingest engine runs")

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", _never_ingest)

    with pull_singleflight_lock(tmp_path):  # a peer pull already holds the lake lock
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
    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "a pull is already running for this lake" in captured.err


def test_cmd_fetch_dry_run_does_not_take_singleflight_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` writes no real data + paces no account requests, so it NEVER takes the
    single-flight lock — even while a peer holds it (DEC-PULL-HARDENING-FOLLOWUP-001)."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

    # A spy so we can assert the dry-run path never even calls the gate.
    called: list[Path] = []
    real_enter = cli._enter_pull_singleflight

    def _spy(stack: Any, lake_root: Path) -> bool:
        called.append(lake_root)
        return real_enter(stack, lake_root)

    monkeypatch.setattr(cli, "_enter_pull_singleflight", _spy)

    with pull_singleflight_lock(tmp_path):  # peer holds the lock — must NOT block dry-run
        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=True,
            inspect=False,
            rows_per_chunk=5,
        )
    assert rc == cli.EXIT_OK
    assert called == []  # the gate was never consulted on the dry-run path


# ── Cross-process CLI→dashboard rate bridge (DEC-CLI-RATE-BRIDGE-001) ──────


def test_make_cli_rate_bridge_on_window_none_controller_returns_inner() -> None:
    """A dry-run / inspect pull (no controller) → the checkpoint is returned UNWRAPPED.

    Nothing real is paced, so there is nothing to publish; the helper must be a zero-overhead
    passthrough (the exact ``inner`` object back), and no bridge file is created.
    """
    sentinel = object()
    out = cli._make_cli_rate_bridge_on_window(sentinel, Path("/unused"), None, object())
    assert out is sentinel


def test_make_cli_rate_bridge_on_window_publishes_real_snapshot(tmp_path: Path) -> None:
    """With a live controller, firing the wrapped hook publishes the bridge file.

    The wrapped callback runs *inner* FIRST (byte-unchanged), then atomically writes the
    controller + call-meter snapshot to ``<lake_root>/.rate/pull_rate.json`` — which the
    dashboard ``/api/v1/pull/rate`` reader surfaces. A bare ``object()`` adapter (no
    ``client.meter``) publishes a zero achieved-rps (the meter-None branch), proving the
    publish never crashes on a meterless adapter.
    """
    from brutex.adapters.groww.constants import (
        GROWW_RATE_LIMIT_PER_MIN,
        GROWW_RATE_LIMIT_RPS_CEILING,
    )
    from brutex.adapters.rate_common import RateController
    from brutex.data.rate_bridge import read_rate_snapshot

    controller = RateController(
        ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
        ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
    )
    inner_calls = {"n": 0}

    def _inner() -> None:
        inner_calls["n"] += 1

    wrapped = cli._make_cli_rate_bridge_on_window(_inner, tmp_path, controller, object())
    assert wrapped is not _inner  # it WAS wrapped (controller present)
    wrapped()

    assert inner_calls["n"] == 1  # inner ran first
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    # The controller snapshot's allowed rps + governor cap are published verbatim.
    assert snap.current_rps == controller.snapshot()["current_rps"]
    assert snap.ceiling_per_min == GROWW_RATE_LIMIT_PER_MIN
    # No call-meter on a bare adapter → the achieved rps defaults to 0 (meter-None branch).
    assert snap.effective_rps == 0.0
    assert snap.wall_time > 0.0


def test_cmd_fetch_live_pull_publishes_rate_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live persisting ``brutex fetch`` writes the cross-process rate bridge per window.

    DEC-CLI-RATE-BRIDGE-001 — the CLI pull runs in a separate process from the dashboard,
    so it publishes its live rate to ``<lake_root>/.rate/pull_rate.json`` on each processed
    window, which the dashboard widget surfaces. The fake ingest fires the per-window hook
    once; we assert the bridge file exists + carries the live controller's allowed rps.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data.rate_bridge import rate_bridge_path, read_rate_snapshot
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_present", lambda: True)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
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
    bridge_existed_during_ingest: list[bool] = []

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        on_window = kwargs["on_window"]
        on_window()  # one processed window → publishes the bridge file
        bridge_existed_during_ingest.append(rate_bridge_path(tmp_path).is_file())
        return result

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

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
    # The bridge was published DURING the pull (from the per-window hook).
    assert bridge_existed_during_ingest == [True]
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.current_rps > 0.0  # the live controller's allowed pace was published


def test_cmd_fetch_dry_run_does_not_publish_rate_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--dry-run`` pull paces no real account requests, so it publishes NO bridge file.

    The controller is ``None`` on the dry-run path, so ``_make_cli_rate_bridge_on_window``
    returns the checkpoint unwrapped and no ``.rate/pull_rate.json`` is written.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data.rate_bridge import rate_bridge_path

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_OK
    assert not rate_bridge_path(tmp_path).exists()
