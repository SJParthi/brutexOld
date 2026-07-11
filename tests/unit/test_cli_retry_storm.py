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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli
from brutex.config.credentials import CredentialsError
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


def test_cmd_fetch_deferred_inside_market_hours(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001: a LIVE fetch inside the window defers, EXIT_OK.

    A clean (non-error) exit so the legacy auto-resume subprocess exits cleanly —
    the deferral is a legitimate no-op, not a failure; the pull runs next out-of-hours.
    """
    monkeypatch.setattr("brutex.calendars.market_hours.guard_active_default", lambda: True)
    rc = cli.cmd_fetch(
        tmp_path,
        None,
        None,
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out == (
        "[brutex] fetch deferred — inside NSE market-hours window "
        "(08:45-15:35 IST); pulls run outside market hours only\n"
    )


def test_cmd_fetch_proceeds_outside_market_hours(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard inactive (outside window / disabled) → the gate is passed; the pull proceeds.

    Proven by threading through to the downstream interval-validation refusal (no
    deferral message, EXIT_USER_ERROR from ``--interval 5minute``).
    """
    monkeypatch.setattr("brutex.calendars.market_hours.guard_active_default", lambda: False)
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "5minute",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "deferred" not in captured.out
    assert "not fetched on Groww" in captured.err


def test_cmd_fetch_dry_run_not_gated_even_when_guard_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` makes no vendor calls → the market-hours gate is EXEMPT."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    monkeypatch.setattr("brutex.calendars.market_hours.guard_active_default", lambda: True)
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "deferred" not in captured.out  # never gated
    assert "SYNTHETIC DRY-RUN" in captured.err  # the dry-run pull actually ran


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
    """``--dry-run`` runs the full synthetic fetch→sink pipeline OFF the real lake.

    DEC-DRYRUN-LAKE-ISOLATION-001 regression: the synthetic bars + catalog + the
    retry-storm audit all land on an ephemeral system-temp lake (removed at exit),
    NEVER on the resolved production lake — while the counts are still reported.
    Also arms the retry-storm guard (>=1 minutes valid) and exercises the legacy
    ``--window-days`` ignored-note path.
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
    # The explicit dry-run announces itself with the LOUD synthetic label
    # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never reads like a real pull)
    # AND names the Wave-1 isolated ephemeral lake (DEC-PULL-FORENSIC-WAVE1-001).
    assert (
        "[brutex] SYNTHETIC DRY-RUN — no real data — fetch --dry-run → synthetic candles"
        in captured.err
    )
    assert "production lake untouched" in captured.err
    assert "retry-storm guard armed — give up after 30 min" in captured.err
    assert (
        "[brutex] fetch done — windows=3 bars_appended=6 failed_pairs=0 transient=0"
    ) in captured.out
    assert f"[brutex] lake: {tmp_path} (dry-run — production lake untouched)" in captured.out
    # The PRE-FIX behaviour wrote the synthetic catalog + retry-storm audit into
    # the production lake root; the isolated dry-run leaves it EMPTY.
    assert not (tmp_path / "catalog.duckdb").exists()
    assert not (tmp_path / "audit").exists()
    assert list(tmp_path.iterdir()) == []
    # The ephemeral temp lake named on stderr is cleaned up after the pull.
    ephemeral = Path(captured.err.split("(ephemeral lake: ", 1)[1].split(" — ", 1)[0])
    assert "brutex-fetch-dryrun-lake-" in ephemeral.name
    assert not ephemeral.exists()


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


# ── --dry-run lake isolation (DEC-DRYRUN-LAKE-ISOLATION-001) ────────────


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Map every file under *root* to ``(size, mtime_ns, content)`` for byte-diffing."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_cmd_fetch_dry_run_leaves_production_lake_byte_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--dry-run`` through the real ``cmd_fetch`` path never touches the lake.

    DEC-DRYRUN-LAKE-ISOLATION-001 fails-before/passes-after regression: PRE-FIX the
    dry-run wired a REAL ``ParquetBarSink`` over the RESOLVED production lake root +
    the real ``catalog.duckdb``, so synthetic bars poisoned real partitions (the
    keep-first merge then dropped the real vendor bars pulled later). The
    'production' lake is pre-seeded and snapshotted; after the dry-run it must be
    BYTE-identical (no new, no modified files) while the run still reports its
    fetched-bar counts.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    production = tmp_path / "production-lake"
    seeded = production / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / "1minute" / "2024"
    seeded.mkdir(parents=True)
    (seeded / "01.parquet").write_bytes(b"real vendor bytes -- must survive untouched")
    before = _snapshot_tree(production)
    assert before  # the seed is real

    rc = cli.cmd_fetch(
        production,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    # Reporting behaviour preserved: the synthetic pull still counts its bars…
    assert (
        "[brutex] fetch done — windows=3 bars_appended=6 failed_pairs=0 transient=0"
    ) in captured.out
    # …but the production tree is byte-for-byte untouched (PRE-FIX this grew a
    # catalog.duckdb + synthetic bars partitions under `production`).
    assert _snapshot_tree(production) == before


class _RealVendorSpotAdapter(cli._DryRunGrowwSpotAdapter):
    """Fake LIVE adapter: the SAME timestamps as the dry-run synthetic bars, REAL prices.

    Reuses the deterministic synthetic generator at a distinct ``base_price`` so a
    dry-run bar that pre-occupied the keep-first slot is detectable by price.
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Any, interval: str, start: Any, end: Any
    ) -> Any:
        from decimal import Decimal

        from brutex.adapters.ingest_common import synthetic_spot_bars

        del symbol, exchange, interval, end
        return synthetic_spot_bars(start, count=2, base_price=Decimal("500"))


def test_cmd_fetch_real_pull_after_dry_run_stores_real_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL pull after a ``--dry-run`` stores the REAL vendor bars, nothing pre-occupied.

    DEC-DRYRUN-LAKE-ISOLATION-001 poison-proof: PRE-FIX the dry-run stored synthetic
    bars (base price 100) into the production lake at the session timestamps; the
    keep-first partition merge then PERMANENTLY dropped the real vendor bars (base
    price 500) arriving later at the same ``(series, timestamp)``. Post-fix the
    dry-run leaves the lake empty, so the subsequent real pull stores the real prices.
    """
    pl = pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    # 1) The dry-run (synthetic base price 100) over the same lake root + dates.
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    assert not list(tmp_path.rglob("*.parquet"))  # nothing pre-occupied

    # 2) A REAL (non-dry) pull through the SAME cmd_fetch path — the real ingest
    # spine + persisting sink, only the vendor adapter + creds gate faked.
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    # Post-DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 the CLI gate consumes
    # groww_credentials_error() (None = creds OK), not the old bool present-check.
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            _RealVendorSpotAdapter(bars_per_window=2),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )
    cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=2,
        auto_retry=False,
    )
    stored = sorted(tmp_path.rglob("*.parquet"))
    bars = [p for p in stored if "bars" in p.parts]
    assert bars, "the real pull stored bars into the lake"
    frame = pl.concat([pl.read_parquet(p) for p in bars])
    opens = {float(v) for v in frame["open"].to_list()}
    # The REAL vendor prices (500-base) won — PRE-FIX the dry-run's synthetic
    # 100-base bars occupied the keep-first slots and these were dropped.
    assert opens == {500.0, 501.0}


def test_make_dryrun_lake_root_is_labelled_temp_dir(tmp_path: Path) -> None:
    """The ephemeral dry-run lake is a fresh, clearly-labelled system-temp dir."""
    import tempfile

    root = cli._make_dryrun_lake_root()
    try:
        assert root.is_dir()
        assert root.name.startswith("brutex-fetch-dryrun-lake-")
        assert Path(tempfile.gettempdir()) in root.parents
        assert tmp_path not in root.parents  # never under a caller's lake
    finally:
        root.rmdir()


def _raise_boom() -> None:
    """Raise the error the cleanup test propagates (indirection keeps the
    post-``with`` assert visibly reachable to static analysis — ``pytest.raises``
    swallows the error at runtime, which a direct in-block ``raise`` hides)."""
    raise RuntimeError("boom")


def test_ephemeral_dryrun_lake_cleanup_removes_tree(tmp_path: Path) -> None:
    """The cleanup context removes the ephemeral tree on exit — even on error."""
    lake = tmp_path / "ephemeral"
    (lake / "bars").mkdir(parents=True)
    (lake / "bars" / "junk.parquet").write_bytes(b"synthetic")
    with pytest.raises(RuntimeError, match="boom"), cli._ephemeral_dryrun_lake_cleanup(lake):
        _raise_boom()
    assert not lake.exists()


def test_ephemeral_dryrun_lake_cleanup_none_is_noop(tmp_path: Path) -> None:
    """A ``None`` lake (live / --inspect pull) leaves the filesystem alone."""
    marker = tmp_path / "marker"
    marker.write_text("live lake stays")
    with cli._ephemeral_dryrun_lake_cleanup(None):
        pass
    assert marker.read_text() == "live lake stays"


# ── cmd_fetch live wiring (adapter / preflight / ingest faked) ──────────


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary printing reads.

    Carries the empty ``pairs`` mapping the async-pipeline completion-count reconcile
    folds into (DEC-CLI-FETCH-ISOLATION-001) — the fake ingest enqueues nothing, so the
    drained pipeline has no per-series totals and ``reconcile_pairs_with_pipeline`` returns
    ``pairs`` unchanged, leaving the explicit count fields above authoritative for the
    summary print + the exit-code residual.
    """

    windows: int
    bars_appended: int
    failed_pairs: int
    transient_failures: int
    still_failed_windows: int
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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
    # The live CLI pull paces at the operator-DIRECTED 8 r/s ceiling (raised 5 → 8
    # on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002). ``pace`` is the controller's bound
    # method, so its ``__self__`` is the RateController — assert it started at the
    # 8 r/s ceiling.
    from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING

    # ``__self__`` exists on the bound method at runtime; pyright only sees a
    # ``FunctionType`` so reach it via ``getattr`` (typed ``Any``) to stay gate-clean.
    controller = getattr(seen["pace"], "__self__")  # noqa: B009
    assert controller.current_rps == GROWW_RATE_LIMIT_RPS_CEILING == 8.0
    assert controller.snapshot()["ceiling_per_min"] == 500


def test_cmd_fetch_live_pull_halts_when_crossing_market_hours(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 MAJOR-1: a LIVE ``brutex fetch`` that CROSSES
    08:45 IST mid-run halts GRACEFULLY — committed work kept, ledger resumable, EXIT_OK.

    The start gate ALLOWED this pull (launched off-hours), so only the mid-run belt
    can stop it. The spine raises :class:`MarketHoursHalt` on the window that crosses;
    ``cmd_fetch`` must (a) thread a non-``None`` ``market_closed`` predicate into the
    ingest spine, (b) catch the halt, drain+persist the ledger (resumable), and (c)
    exit ``EXIT_OK`` with the halt notice — never a crash the legacy auto-resume would
    hammer-retry. The ingest engine is faked to RAISE the halt (simulating the spine's
    per-window belt firing); the real persisting sink + ledger prove the drain/persist.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.data.errors import MarketHoursHalt
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            object(),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> Any:
        """Record the wiring, then simulate the per-window belt crossing 08:45."""
        seen.update(kwargs)
        raise MarketHoursHalt()

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=2,
        auto_retry=False,
    )
    # Graceful deferral — EXIT_OK, never a crash the auto-resume would hammer-retry.
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert (
        "[brutex] fetch halted — crossed into NSE market-hours window "
        "(08:45-15:35 IST); committed work kept, resumes next out-of-hours run"
    ) in captured.out
    # (a) a REAL live pull threads a bound (non-None) market-hours predicate.
    assert seen["market_closed"] is not None
    assert callable(seen["market_closed"])
    # (b) the ledger was drained + persisted (resumable), not wiped — the durable
    # window-ledger sidecar exists for the next out-of-hours run to resume from.
    assert (tmp_path / "window_ledger" / "ledger.parquet").exists()


def test_cmd_fetch_live_wires_timeout_ladder_into_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI live pull threads the controller's OWN ``on_timeout`` into the
    ingest engine, so a 5xx/timeout storm steps the AIMD ladder DOWN.

    Regression for forensic-audit finding #16: the CLI bound only
    ``pace`` / ``on_rate_limit`` / ``on_success`` and never wired ``on_timeout``,
    so ``RateController.on_timeout`` was DEAD on the ``brutex fetch`` path (the
    dashboard's ``_live_rate_runtime`` wires the full hook quad). FAILS unfixed:
    the ``on_timeout`` kwarg reaches the ingest as ``None``. Verified here by
    driving the captured hook through a simulated timeout storm and observing
    the controller's measured step-down — never by re-reading prose.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
    from brutex.adapters.rate_common import STEP_DOWN_TIMEOUT_HITS

    monkeypatch.setattr(
        cli, "_resolve_brute_force_instruments", lambda lake: [FakeInstrument("NIFTY")]
    )
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    result = _FakeIngestResult(
        report=_FakeIngestReport(
            windows=1,
            bars_appended=1,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        ),
        retry=None,
    )
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        """Capture the hook wiring the CLI threaded in; return a clean report."""
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
    assert rc == cli.EXIT_OK
    on_timeout = seen["on_timeout"]
    assert callable(on_timeout)  # FAILS unfixed: the CLI threads None
    # The hook is the SAME controller's bound method the pace hook rides — one
    # shared AIMD ladder, exactly like the dashboard wiring (CLAUDE.md §11 COMMON).
    controller = getattr(seen["pace"], "__self__")  # noqa: B009
    assert getattr(on_timeout, "__self__") is controller  # noqa: B009
    # Cheap #17 guard: the CLI-wired ceiling IS the single source-of-truth
    # constant (DEC-GROWW-RATE-CEILING-8-002) — the controller starts there.
    assert controller.current_rps == GROWW_RATE_LIMIT_RPS_CEILING
    # Drive a sustained timeout storm through the captured hook: once
    # STEP_DOWN_TIMEOUT_HITS stalls land inside the rolling window the ladder
    # steps the live rate DOWN 1 r/s (the behaviour finding #16 said was dead).
    for _ in range(STEP_DOWN_TIMEOUT_HITS):
        on_timeout()
    assert controller.current_rps == GROWW_RATE_LIMIT_RPS_CEILING - 1.0


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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    # Count durable-ledger DELTA appends so we can prove one fired MID-pull (from
    # the on_window checkpoint) rather than only at end_pull_ledger. Since
    # DEC-LEDGER-DELTA-FLUSH-001 the throttled mid-pull flush appends a delta
    # (O(dirty)) instead of rewriting the whole base — the checkpoint contract is
    # unchanged, only the artifact moved. begin/plan/reconcile do not write (load
    # + in-memory mutate), so the first append is the mid-pull flush.
    saves = {"n": 0}
    real_append = window_ledger_store.append_ledger_delta

    def counting_append(led: Any, root: Any, *a: Any, **kw: Any) -> Any:
        saves["n"] += 1
        return real_append(led, root, *a, **kw)

    monkeypatch.setattr(window_ledger_store, "append_ledger_delta", counting_append)

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
    # end_pull_ledger save) — exactly one mid-pull delta append observed inside
    # the ingest (DEC-LEDGER-DELTA-FLUSH-001).
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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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

    Groww auth is TOTP (no Dhan JWT/RenewToken pre-flight) — the creds resolve
    is the only up-front gate (DEC-DHAN-GROWW-REMIGRATE-001 PR-J2). A failing
    resolve returns the user-error code BEFORE any adapter build or instrument
    resolution, and the stderr line RELAYS the CredentialsError's own fix text
    verbatim — never a hardcoded guess at the cause, and never a silent
    synthetic downgrade (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(
        credentials,
        "groww_credentials_error",
        lambda: "SSM parameter /tickvault/prod/groww/api-key is absent (test fix text)",
    )

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
    # The COMMON refusal message: static headline + the RELAYED fix text.
    assert "Pull REFUSED — Groww credentials unavailable" in captured.err
    assert "SSM parameter /tickvault/prod/groww/api-key is absent (test fix text)" in captured.err


def test_cmd_fetch_creds_vanishing_mid_build_refuses_with_the_relayed_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate passes, then the adapter build raises CredentialsError (the race).

    The CLI must print the SAME COMMON refusal (headline + relayed fix text)
    and exit with the user-error code — never a silent downgrade
    (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: [])

    def _vanished(*_a: Any, **_k: Any) -> Any:
        raise CredentialsError(
            "seed /tickvault/prod/groww/api-key + totp-secret in ap-south-1 (test fix text)"
        )

    monkeypatch.setattr(cli, "_build_live_groww_spot", _vanished)
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
    # The SAME loud refusal as the up-front gate, with the relayed fix text.
    assert "Pull REFUSED — Groww credentials unavailable" in captured.err
    assert "seed /tickvault/prod/groww/api-key + totp-secret in ap-south-1" in captured.err


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

    def fake_run_until_clean(
        round_runner: Any, *, max_rounds: int, market_closed: Any = None
    ) -> Any:
        """Drive ONE real round, then report the loop halted on a dead token.

        Accepts the DEC-GROWW-MARKET-HOURS-GUARD-001 between-rounds ``market_closed``
        belt kwarg the CLI now threads (default ``None`` — this fake never crosses).
        """
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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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


def _breaker_halts() -> list[Exception]:
    """The DEC-VENDOR-DOWN-BREAKER-001 global stops the CLI halt tuple must match."""
    from brutex.adapters.error_storm_breaker import VendorDownError
    from brutex.adapters.groww.errors import GrowwCredentialDeadError

    return [VendorDownError("vendor down"), GrowwCredentialDeadError("credential dead")]


@pytest.mark.parametrize("halt", _breaker_halts(), ids=lambda e: type(e).__name__)
def test_cmd_fetch_alerts_then_reraises_on_breaker_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, halt: Exception
) -> None:
    """The two DEC-VENDOR-DOWN-BREAKER-001 trips ride the SAME CLI halt path.

    The behavioral ``_pull_halt_errors`` membership pin the 4-agent hostile
    review found missing: a mutation deleting ``VendorDownError`` /
    ``GrowwCredentialDeadError`` from the CLI's hand-mirrored halt tuple would
    previously pass the whole suite — the trip would then propagate WITHOUT the
    generic CRITICAL pull-halt Telegram alert. Same fakes-only harness as the
    ``LakeWriteHalt`` twin above.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

    def fake_ingest(*_a: Any, **_k: Any) -> Any:
        """Simulate the breaker's global stop propagating out of the spine."""
        raise halt

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(type(halt)):
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
    # The alert fired exactly once with the propagating trip, then it re-raised.
    assert halts == [halt]


def test_cmd_fetch_alerts_then_reraises_on_terminal_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TERMINAL GrowwAuthError HALTS the CLI pull with the CRITICAL alert, then re-raises.

    DEC-GROWW-AUTH-HALT-001 — the CLI twin of the dashboard ``_is_pull_halt`` wiring: a
    dead credential fails every remaining symbol identically, so it fires the pull-halt
    alert exactly once and re-raises (non-zero exit preserved) instead of quarantining
    symbol-by-symbol into a silent all-failed "completion".
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.adapters.groww.errors import GrowwAuthError
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

    def fake_ingest(*_a: Any, **_k: Any) -> Any:
        """Simulate a dead-credential terminal auth failure escaping the spine."""
        raise GrowwAuthError("bad credential — re-mint budget exhausted")

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(GrowwAuthError):
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
    assert len(halts) == 1  # the CRITICAL alert fired exactly once, then it re-raised
    assert isinstance(halts[0], GrowwAuthError)


def test_cmd_fetch_retryable_auth_shape_reraises_without_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RETRYABLE GrowwAuthError subclass re-raises UNCHANGED with NO pull-halt alert.

    DEC-GROWW-AUTH-HALT-001 carve-out at the CLI except site: a stale read-only
    re-read (``GrowwTokenStillStaleError``) is transient by design — it propagates
    exactly as it did before the ``GrowwAuthError`` tuple membership caught it,
    never firing the CRITICAL alert.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.adapters.groww.errors import GrowwTokenStillStaleError
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

    def fake_ingest(*_a: Any, **_k: Any) -> Any:
        """Simulate a retryable auth-family shape escaping the spine terminally."""
        raise GrowwTokenStillStaleError("re-read returned the same dead token")

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(GrowwTokenStillStaleError):
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
    assert halts == []  # transient auth-family shape → NO CRITICAL alert, ever


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


# ── account-keyed second lock, taken FIRST (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001) ──


def test_enter_pull_singleflight_takes_account_lock_first_and_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a resolvable key the ACCOUNT lock is taken (and held) alongside the lake lock."""
    from brutex.adapters.groww.pull_singleflight import (
        PullAlreadyRunningError,
        account_pull_singleflight_lock,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # never write the real $HOME
    with contextlib.ExitStack() as stack:
        assert (
            cli._enter_pull_singleflight(
                stack, tmp_path / "lake", account_key_resolver=lambda: "key-A"
            )
            is True
        )
        # The ACCOUNT lock is held: a peer acquire for the SAME key — even with a
        # DIFFERENT lake root — is refused (the #996 residual, closed).
        with (
            pytest.raises(PullAlreadyRunningError),
            account_pull_singleflight_lock("key-A"),
        ):
            pass  # pragma: no cover — __enter__ raises first
    # Stack exited (LIFO) → both released → a fresh account acquire succeeds.
    with account_pull_singleflight_lock("key-A"):
        pass


def test_enter_pull_singleflight_refuses_on_account_contention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held ACCOUNT lock refuses the pull BEFORE the lake lock is even attempted."""
    from brutex.adapters.groww.pull_singleflight import (
        account_pull_singleflight_lock,
        pull_singleflight_lock,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    lake_root = tmp_path / "lake"
    with (
        account_pull_singleflight_lock("key-A"),  # a peer pull holds the ACCOUNT
        contextlib.ExitStack() as stack,
    ):
        assert (
            cli._enter_pull_singleflight(stack, lake_root, account_key_resolver=lambda: "key-A")
            is False
        )
        # The lake lock was never taken (account refused first) — a fresh lake acquire
        # succeeds even while the peer still holds the account lock.
        with pull_singleflight_lock(lake_root):
            pass
    captured = capsys.readouterr()
    assert "for this Groww account" in captured.err
    assert "DIFFERENT lake root" in captured.err


def test_enter_pull_singleflight_skips_account_lock_on_none_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None key (no credentials) skips the account lock; the lake lock still guards."""
    from brutex.adapters.groww.pull_singleflight import default_account_lock_dir

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with contextlib.ExitStack() as stack:
        assert (
            cli._enter_pull_singleflight(
                stack, tmp_path / "lake", account_key_resolver=lambda: None
            )
            is True
        )
        assert not default_account_lock_dir().exists()  # no account lock file created


def test_enter_pull_singleflight_default_resolver_uses_aws_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production default resolves the AWS-only api_key and locks on its digest."""
    from types import SimpleNamespace

    from brutex.adapters.groww.pull_singleflight import account_singleflight_lock_path

    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(
        credentials,
        "resolve_groww_aws_credentials",
        lambda: SimpleNamespace(api_key="aws-key", totp_secret="s"),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with contextlib.ExitStack() as stack:
        assert cli._enter_pull_singleflight(stack, tmp_path / "lake") is True
        assert account_singleflight_lock_path("aws-key").exists()


def test_enter_pull_singleflight_default_resolver_skips_on_credentials_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CredentialsError from the AWS source → graceful skip, the pull still starts."""
    from brutex.config.credentials_types import CredentialsError

    def _boom() -> Any:
        raise CredentialsError("aws unreachable")

    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "resolve_groww_aws_credentials", _boom)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with contextlib.ExitStack() as stack:
        assert cli._enter_pull_singleflight(stack, tmp_path / "lake") is True


# ── cross-host pull lease, taken LAST (DEC-ACCOUNT-LEASE-CROSSHOST-001) ──


def test_enter_pull_singleflight_refuses_on_fresh_foreign_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A FRESH lease from a DIFFERENT host on the shared lake refuses the CLI pull."""
    import json
    import time

    from brutex.adapters.groww.pull_singleflight import (
        pull_lease_path,
        pull_singleflight_lock,
        read_pull_lease,
    )

    lease_path = pull_lease_path(tmp_path)
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "token": "peer-token",
                "host": "definitely-not-this-host",
                "pid": 1,
                "started_wall": time.time(),
                "wall_time": time.time(),  # FRESH heartbeat
            }
        )
    )
    with contextlib.ExitStack() as stack:
        assert (
            cli._enter_pull_singleflight(stack, tmp_path, account_key_resolver=lambda: None)
            is False
        )
        # Refusal rolled the flocks back (ExitStack unwinding happens at stack exit,
        # but the lease enter raised BEFORE being registered) — the peer lease intact.
        lease = read_pull_lease(tmp_path)
        assert lease is not None and lease.token == "peer-token"
    # The lake flock was released by the stack — a fresh acquire succeeds.
    with pull_singleflight_lock(tmp_path):
        pass
    captured = capsys.readouterr()
    assert "another HOST" in captured.err
    assert "goes " in captured.err  # "the lease goes stale by itself..." honesty note


def test_enter_pull_singleflight_holds_lease_and_clears_on_exit(tmp_path: Path) -> None:
    """A clean CLI acquire writes the cross-host lease and clears it on stack exit."""
    from brutex.adapters.groww.pull_singleflight import read_pull_lease

    with contextlib.ExitStack() as stack:
        assert (
            cli._enter_pull_singleflight(stack, tmp_path, account_key_resolver=lambda: None) is True
        )
        lease = read_pull_lease(tmp_path)
        assert lease is not None  # our lease is on disk while the pull would run
    assert read_pull_lease(tmp_path) is None  # cleared on release (LIFO stack exit)


def test_enter_pull_singleflight_lease_factory_seam_is_injectable(tmp_path: Path) -> None:
    """The lease_factory seam replaces the live guard (tests never spawn a heartbeat)."""
    entered: list[Path] = []

    @contextlib.contextmanager
    def fake_lease(root: Path) -> Any:
        entered.append(root)
        yield

    with contextlib.ExitStack() as stack:
        assert (
            cli._enter_pull_singleflight(
                stack, tmp_path, account_key_resolver=lambda: None, lease_factory=fake_lease
            )
            is True
        )
    assert entered == [tmp_path]


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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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


def test_make_cli_rate_bridge_on_window_swallows_non_halt_publish_error(
    tmp_path: Path,
) -> None:
    """A non-LakeWriteHalt publish error must NOT propagate out of the per-window hook.

    The bridge publish is a pure VISIBILITY side-effect — an EROFS/EACCES/EXDEV from the
    atomic write, or a transient controller-snapshot error, must NEVER crash the operator's
    multi-hour pull. The wrapped hook swallows everything except LakeWriteHalt, and ``inner``
    must STILL have run (the ledger checkpoint is not skipped just because the publish failed).
    """

    class _BoomController:
        def snapshot(self) -> dict[str, object]:
            raise RuntimeError("transient snapshot failure")

    inner_calls = {"n": 0}

    def _inner() -> None:
        inner_calls["n"] += 1

    wrapped = cli._make_cli_rate_bridge_on_window(_inner, tmp_path, _BoomController(), object())
    # Must NOT raise even though controller.snapshot() blows up inside the publish.
    wrapped()
    assert inner_calls["n"] == 1  # inner STILL ran (publish error did not skip the checkpoint)
    # Nothing was published (the snapshot failed before the write).
    assert not (tmp_path / ".rate" / "pull_rate.json").exists()


def test_make_cli_rate_bridge_on_window_reraises_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LakeWriteHalt (disk-full) from the publish STILL propagates — disk-full halts the pull.

    Disk-full is a genuine pull-wide stop (it fails every remaining window), so unlike a
    best-effort visibility error it must reach the existing ``_pull_halt_errors`` handling.
    """
    from brutex.adapters.rate_common import RateController
    from brutex.data.errors import LakeWriteHalt

    def _halt(*_a: Any, **_k: Any) -> None:
        raise LakeWriteHalt("disk_full")

    # Patch the symbol the helper imported (string-target form — one consistent import style).
    monkeypatch.setattr("brutex.data.rate_bridge.publish_from_snapshots", _halt)
    controller = RateController(ceiling_rps=5.0, ceiling_per_min=500)
    wrapped = cli._make_cli_rate_bridge_on_window(lambda: None, tmp_path, controller, object())
    with pytest.raises(LakeWriteHalt) as excinfo:
        wrapped()
    assert excinfo.value.reason == "disk_full"


def test_per_window_publish_carries_pull_active_true(tmp_path: Path) -> None:
    """DEC-CLI-BRIDGE-PULL-ACTIVE-001: each per-window publish stamps pull_active=True."""
    from brutex.adapters.rate_common import RateController
    from brutex.data.rate_bridge import read_rate_snapshot

    controller = RateController(ceiling_rps=5.0, ceiling_per_min=500)
    wrapped = cli._make_cli_rate_bridge_on_window(lambda: None, tmp_path, controller, object())
    wrapped()
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is True


def test_publish_cli_pull_inactive_publishes_false(tmp_path: Path) -> None:
    """The end-of-pull finalize publishes ONE snapshot with pull_active=False."""
    from brutex.adapters.rate_common import RateController
    from brutex.data.rate_bridge import read_rate_snapshot

    controller = RateController(ceiling_rps=5.0, ceiling_per_min=500)
    cli._publish_cli_pull_inactive(tmp_path, controller, object())
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is False
    assert snap.current_rps == controller.snapshot()["current_rps"]
    # A bare object() adapter has no client.meter → the meter-None branch (achieved 0).
    assert snap.effective_rps == 0.0


def test_publish_cli_pull_inactive_none_controller_is_noop(tmp_path: Path) -> None:
    """A dry-run / inspect pull (no controller) publishes NOTHING at pull end."""
    cli._publish_cli_pull_inactive(tmp_path, None, object())
    assert not (tmp_path / ".rate" / "pull_rate.json").exists()


def test_publish_cli_pull_inactive_uses_meter_when_present(tmp_path: Path) -> None:
    """A real client.meter snapshot feeds the final publish's achieved rps."""
    from brutex.adapters.rate_common import RateController
    from brutex.data.rate_bridge import read_rate_snapshot

    class _Meter:
        def snapshot(self) -> dict[str, object]:
            return {"effective_rps": 3.25}

    class _Client:
        meter = _Meter()

    class _Adapter:
        client = _Client()

    controller = RateController(ceiling_rps=5.0, ceiling_per_min=500)
    cli._publish_cli_pull_inactive(tmp_path, controller, _Adapter())
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.effective_rps == 3.25
    assert snap.pull_active is False


def test_publish_cli_pull_inactive_swallows_every_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finalize swallows EVERYTHING — even LakeWriteHalt — the pull is already ending."""
    from brutex.adapters.rate_common import RateController
    from brutex.data.errors import LakeWriteHalt

    def _halt(*_a: Any, **_k: Any) -> None:
        raise LakeWriteHalt("disk_full")

    monkeypatch.setattr("brutex.data.rate_bridge.publish_from_snapshots", _halt)
    controller = RateController(ceiling_rps=5.0, ceiling_per_min=500)
    # Must NOT raise (contrast: the per-window hook re-raises LakeWriteHalt).
    cli._publish_cli_pull_inactive(tmp_path, controller, object())


# ── Pull-health monitor on the CLI fetch path (DEC-PULL-HEALTH-CLI-WIRING-001) ──


class _RecordingMonitor:
    """A best-effort ``PullHealthMonitor`` stand-in capturing every observe call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, object], Mapping[str, object]]] = []

    def observe(
        self,
        controller_snap: Mapping[str, object],
        meter_snap: Mapping[str, object],
    ) -> None:
        self.calls.append((controller_snap, meter_snap))


def _live_controller() -> Any:
    """A real, live ``RateController`` at the production ceiling (its snapshot is real)."""
    from brutex.adapters.groww.constants import (
        GROWW_RATE_LIMIT_PER_MIN,
        GROWW_RATE_LIMIT_RPS_CEILING,
    )
    from brutex.adapters.rate_common import RateController

    return RateController(
        ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
        ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
    )


def test_make_cli_rate_bridge_on_window_feeds_monitor(tmp_path: Path) -> None:
    """With a monitor wired, firing the wrapped hook ALSO feeds it the SAME snapshot pair.

    DEC-PULL-HEALTH-CLI-WIRING-001 — the CLI ``brutex fetch`` pull self-instruments its
    health by reusing the existing per-window rate-bridge seam: the monitor receives the
    SAME controller + call-meter snapshot the bridge publishes (no separate read).
    """
    controller = _live_controller()
    monitor = _RecordingMonitor()
    inner_calls = {"n": 0}

    def _inner() -> None:
        inner_calls["n"] += 1

    wrapped = cli._make_cli_rate_bridge_on_window(_inner, tmp_path, controller, object(), monitor)
    wrapped()

    assert inner_calls["n"] == 1  # inner ran first (byte-unchanged)
    assert len(monitor.calls) == 1  # the monitor was fed exactly once per window
    controller_snap, meter_snap = monitor.calls[0]
    # The monitor sees the REAL controller snapshot the bridge built (not a fresh re-read).
    assert controller_snap["current_rps"] == controller.snapshot()["current_rps"]
    # A bare object() adapter has no client.meter → an empty meter snapshot.
    assert meter_snap == {}


def test_make_cli_rate_bridge_on_window_none_monitor_is_noop(tmp_path: Path) -> None:
    """A ``None`` monitor (dry-run / build failure) feeds nothing — the bridge still publishes.

    The publish still happens (a live controller is present), but with no monitor wired the
    health-watch branch is a pure no-op.
    """
    from brutex.data.rate_bridge import read_rate_snapshot

    controller = _live_controller()
    wrapped = cli._make_cli_rate_bridge_on_window(
        lambda: None, tmp_path, controller, object(), None
    )
    wrapped()
    # The bridge still published (the publish path is independent of the monitor).
    assert read_rate_snapshot(tmp_path) is not None


def test_make_cli_rate_bridge_on_window_monitor_resnapshots_after_publish_failure(
    tmp_path: Path,
) -> None:
    """If the publish failed BEFORE the controller snapshot, the monitor re-snapshots it.

    The controller snapshot is taken INSIDE the publish ``try`` (just before the bridge
    write); a transient controller-snapshot failure there leaves ``controller_snap`` ``None``,
    so the monitor branch must re-snapshot it under its OWN guard rather than starve the watch.
    A controller that fails its FIRST snapshot then succeeds proves the re-snapshot path.
    """

    class _FlakyController:
        def __init__(self) -> None:
            self._n = 0

        def snapshot(self) -> dict[str, object]:
            self._n += 1
            if self._n == 1:  # the publish-side snapshot fails (controller_snap stays None)
                raise RuntimeError("transient snapshot failure")
            return {"current_rps": 5.0}  # the monitor-side re-snapshot succeeds

    monitor = _RecordingMonitor()
    wrapped = cli._make_cli_rate_bridge_on_window(
        lambda: None, tmp_path, _FlakyController(), object(), monitor
    )
    wrapped()  # must NOT raise — both the publish error and the re-snapshot are guarded
    assert len(monitor.calls) == 1
    assert monitor.calls[0][0] == {"current_rps": 5.0}  # the re-snapshot value reached observe
    # The publish itself failed before its write, so no bridge file was produced.
    assert not (tmp_path / ".rate" / "pull_rate.json").exists()


def test_make_cli_rate_bridge_on_window_swallows_monitor_error(tmp_path: Path) -> None:
    """A monitor whose ``observe`` raises must NEVER crash the operator's multi-hour pull.

    Pull-health is observe-only — a buggy monitor is swallowed + logged, and ``inner`` (the
    ledger checkpoint) must STILL have run.
    """

    class _BoomMonitor:
        def observe(self, _c: Mapping[str, object], _m: Mapping[str, object]) -> None:
            raise RuntimeError("monitor blew up")

    controller = _live_controller()
    inner_calls = {"n": 0}

    def _inner() -> None:
        inner_calls["n"] += 1

    wrapped = cli._make_cli_rate_bridge_on_window(
        _inner, tmp_path, controller, object(), _BoomMonitor()
    )
    wrapped()  # must NOT raise even though observe() blows up
    assert inner_calls["n"] == 1  # inner STILL ran


def test_make_cli_rate_bridge_on_window_breach_pushes_one_pull_degraded(
    tmp_path: Path,
) -> None:
    """END-TO-END: a degraded snapshot through the wired seam pushes ONE ``pull_degraded`` edge.

    DEC-PULL-HEALTH-CLI-WIRING-001 — wires the REAL ``PullHealthMonitor`` (the dashboard's
    builder rails: a fresh ``MetricRegistry`` + a capturing ``on_degraded``) into the CLI
    per-window seam and feeds it a snapshot pair whose achieved r/s is far below the allowed
    ceiling, so the hysteresis-gated ``pull.achieved_below_allowed`` rule fires exactly once.
    """
    from brutex.observability.metrics import MetricRegistry
    from brutex.observability.pull_health import PullHealthMonitor

    degraded: list[tuple[str, float, float, str]] = []

    def _on_degraded(rule: str, achieved: float, allowed: float, detail: str) -> None:
        degraded.append((rule, achieved, allowed, detail))

    monitor = PullHealthMonitor(
        registry=MetricRegistry(),
        on_degraded=_on_degraded,
        pool_size=4,
    )

    # A meter advertising effective_rps far below the controller's allowed current_rps trips
    # pull.achieved_below_allowed (0.5 / 5.0 = 0.1 < the 0.6 ratio threshold).
    class _DegradedMeter:
        @staticmethod
        def snapshot() -> dict[str, object]:
            return {"effective_rps": 0.5, "latency_ms_p95": 10.0, "in_flight": 4}

    class _DegradedClient:
        meter = _DegradedMeter()

    class _DegradedAdapter:
        client = _DegradedClient()

    controller = _live_controller()
    wrapped = cli._make_cli_rate_bridge_on_window(
        lambda: None, tmp_path, controller, _DegradedAdapter(), monitor
    )
    # The WARN rules are dwell-gated (DEC-PULL-HEALTH-WARN-DWELL-001: 5 consecutive
    # breaching samples before the OK->BREACH edge fires, so a ~1s-old pool never
    # cold-fires) — fire enough windows to cross the dwell and prove a SUSTAINED
    # breach still alerts exactly once through the CLI seam.
    for _ in range(8):
        wrapped()

    breaches = [d for d in degraded if d[0] == "pull.achieved_below_allowed"]
    assert len(breaches) == 1  # the breach edge fired exactly once (hysteresis dedupe)
    assert breaches[0][3].startswith("OUR pipeline is the limiter")


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
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
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


# ── Observed-sessions overlay arming (DEC-CALENDAR-OBSERVED-SESSIONS-001) ──


def _write_overlay_sidecar(lake_root: Path) -> frozenset[Any]:
    """Write a minimal valid overlay sidecar; return the expected loaded set."""
    import json
    from datetime import date

    from brutex.calendars.observed_sessions import (
        OBSERVED_LABEL,
        OBSERVED_OVERLAY_VERSION,
        overlay_path,
    )

    path = overlay_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": OBSERVED_OVERLAY_VERSION,
                "entries": [
                    {
                        "date": "2026-05-12",
                        "detected_at": "2026-07-04T00:00:00+00:00",
                        "month": "2026-05",
                        "label": OBSERVED_LABEL,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return frozenset({date(2026, 5, 12)})


def _spy_ingest_observed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Wrap the real spot ingest-with-retry, recording the observed_holidays kwarg."""
    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    captured: list[Any] = []
    real = run_ingest.run_groww_spot_ingest_with_retry

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("observed_holidays"))
        return real(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", _spy)
    return captured


def test_cmd_fetch_dry_run_threads_empty_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--dry-run`` threads the EMPTY overlay even when a sidecar exists on disk
    (the ``detect_completeness_gaps`` gating pattern — a synthetic pull must never
    consume real-lake calendar state)."""
    _write_overlay_sidecar(tmp_path)
    captured = _spy_ingest_observed(monkeypatch)
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    assert captured
    assert all(observed == frozenset() for observed in captured)


def test_cmd_fetch_live_threads_loaded_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL (non-dry) pull loads the overlay sidecar from the production lake and
    threads it into the ingest spine (the detectors' margin arithmetic)."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    expected = _write_overlay_sidecar(tmp_path)
    captured = _spy_ingest_observed(monkeypatch)
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            _RealVendorSpotAdapter(bars_per_window=2),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=2,
        auto_retry=False,
    )
    assert rc == cli.EXIT_OK
    assert captured
    assert all(observed == expected for observed in captured)


def test_cmd_fetch_real_pull_controller_receives_smooth_pacing_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI build site threads the TRI-STATE smooth-pacing mode into its controller.

    DEC-SMOOTH-PACING-AUTOLATCH-001 regression — fail-before: on origin/main the
    CLI `brutex fetch` path built its ``RateController`` WITHOUT any smooth-pacing
    argument (``BRUTEX_GROWW_SMOOTH_PACING`` was a dead flag there), so the
    captured kwargs carried no ``smooth_pacing_mode`` and this assertion failed.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from brutex.adapters import rate_common
    from brutex.adapters.groww.constants import GROWW_SMOOTH_PACING_ENV
    from brutex.adapters.rate_common import SMOOTH_PACING_ADAPTIVE, RateController
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    captured: list[str | None] = []

    class _RecordingController(RateController):
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs.get("smooth_pacing_mode"))  # type: ignore[arg-type]
            super().__init__(**kwargs)  # type: ignore[arg-type]

    monkeypatch.delenv(GROWW_SMOOTH_PACING_ENV, raising=False)
    monkeypatch.setattr(rate_common, "RateController", _RecordingController)
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            _RealVendorSpotAdapter(bars_per_window=1),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=2,
        auto_retry=False,
    )
    assert rc == cli.EXIT_OK
    # The unset default resolves ADAPTIVE and the CLI controller RECEIVED it.
    assert captured == [SMOOTH_PACING_ADAPTIVE]


def test_cmd_fetch_live_overlay_hoisted_above_the_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-SEED-OBSERVED-OVERLAY-001 ordering spy: the CLI loads the overlay ONCE,
    strictly BEFORE the cold-start reconcile, and the SAME snapshot reaches the
    seed gate (``observed=``) AND the ingest spine (``observed_holidays=``)."""
    from brutex.data import window_ledger_store as store_module
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    expected = _write_overlay_sidecar(tmp_path)
    order: list[str] = []
    seed_observed: list[Any] = []
    observed_sessions = importlib.import_module("brutex.calendars.observed_sessions")
    real_load = observed_sessions.load_observed_holidays
    real_reconcile = store_module.reconcile_ledger_with_lake

    def _load_spy(root: Path) -> Any:
        order.append("load")
        return real_load(root)

    def _reconcile_spy(*args: Any, **kwargs: Any) -> int:
        order.append("reconcile")
        seed_observed.append(kwargs.get("observed"))
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(observed_sessions, "load_observed_holidays", _load_spy)
    monkeypatch.setattr(store_module, "reconcile_ledger_with_lake", _reconcile_spy)
    ingest_observed = _spy_ingest_observed(monkeypatch)
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            _RealVendorSpotAdapter(bars_per_window=2),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=2,
        auto_retry=False,
    )
    assert rc == cli.EXIT_OK
    # ONE load per pull, strictly BEFORE the reconcile (the hoist).
    assert order.count("load") == 1
    assert "reconcile" in order
    assert order.index("load") < order.index("reconcile")
    # ONE consistent snapshot — the seed gate AND the detectors read the same set.
    assert seed_observed == [expected]
    assert ingest_observed and all(observed == expected for observed in ingest_observed)
