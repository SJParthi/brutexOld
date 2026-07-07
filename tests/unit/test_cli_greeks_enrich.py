# pyright: reportPrivateUsage=none
"""CLI fetch-seam tests for the post-pull Greeks enrichment (DEC-CLI-GREEKS-ENRICH-001).

Drives ``cmd_fetch`` end-to-end with the established ``test_cli_retry_storm.py``
live-wiring fakes (creds gate / instrument resolve / adapter build / ingest engine
all faked — no network, no Groww credentials) and pins the Seam-A contract:

- the enrichment stage runs EXACTLY ONCE post-drain on a persisting pull, scoped
  to the pulled underlyings × the pulled ``[start, end + 1 day)`` window, and the
  ``fno.greeks_enrich.done`` event carries the driver report verbatim;
- a pure-spot lake skips via the ``has_option_candles`` pre-gate (one DEBUG
  event, no prints, no pass construction);
- the ``BRUTEX_CLI_GREEKS_ENRICH`` kill switch skips the stage, junk values fail
  ``EXIT_USER_ERROR`` BEFORE any vendor work, and the on-tokens reach the seam;
- ``--dry-run`` / ``--inspect`` leave the stage fully inert (``_no_persist``);
- a fenced enrichment failure NEVER fails the pull (``fno.greeks_enrich.error``
  observed, exit unchanged) while the two HOST halts (``LakeWriteHalt`` /
  ``GreeksEnrichPoolBroken``) alert once via ``_emit_pull_halt_alert`` then
  re-raise into the existing CLI halt handling.

The fenced pass itself is faked HERMETICALLY by installing a stand-in
``brutex.data.greeks_enrich`` module in ``sys.modules`` (the fence's call-time
import picks it up), so every test here is wheel-free — no ``trading_core``
import, no Parquet read; option contracts are 0-byte lake-tree stubs (discovery
reads directory metadata only, the module's own zero-reads contract).
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from brutex import cli
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_enrich_pass import GREEKS_NOT_RUN, GreeksEnrichOutcome
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# ────────────────────────────────────────────────────────────────────────
# Shared fixtures / fakes (the test_cli_retry_storm.py live-wiring idiom)
# ────────────────────────────────────────────────────────────────────────

#: One NIFTY Jan-2024 CE leg — the workhorse seeded lake contract.
_NIFTY_CE = "NSE-NIFTY-25Jan24-21000-CE"
#: A BANKNIFTY leg — must be dropped when only NIFTY was pulled.
_BANK_CE = "NSE-BANKNIFTY-25Jan24-45000-CE"

#: The 13 ``fno.greeks_enrich.done`` counters, each a DISTINCT value so a
#: field-swap mutation in the fence's log call is caught verbatim.
_REPORT_FIELDS: dict[str, int] = {
    "enriched": 1,
    "already_current": 2,
    "futures_skipped": 3,
    "undecodable": 4,
    "no_bars": 5,
    "errored": 6,
    "bars_written": 7,
    "bars_solved": 8,
    "bars_null": 9,
    "bars_reused": 10,
    "spot_reads": 11,
    "spot_hits": 12,
    "bars_skipped": 13,
}


@pytest.fixture(autouse=True)
def _lake_deps() -> None:
    """``cmd_fetch``'s function-body imports need the P2 lake extras."""
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")


@pytest.fixture(autouse=True)
def _hermetic_greeks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the seam's env gates so an ambient shell can never flip a test."""
    monkeypatch.delenv("BRUTEX_CLI_GREEKS_ENRICH", raising=False)
    monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)


@dataclass(frozen=True)
class _FakeInstrument:
    """Duck-typed instrument exposing what ``underlying_match_tokens`` reads."""

    symbol_name: str
    underlying_symbol: str | None = None


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary reads.

    ``auth_aborted`` feeds the ``--until-clean`` round runner; the empty
    ``pairs`` mapping rides the async-pipeline completion-count reconcile
    unchanged (the ``test_cli_retry_storm.py`` fake, plus that one field).
    """

    windows: int = 1
    bars_appended: int = 375
    failed_pairs: int = 0
    transient_failures: int = 0
    still_failed_windows: int = 0
    auth_aborted: bool = False
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` (report + retry + final residual)."""

    report: _FakeIngestReport = field(default_factory=_FakeIngestReport)
    retry: Any = None

    @property
    def still_failed_windows(self) -> int:
        """Mirror the real wrapper: the retry residual wins when a retry ran."""
        if self.retry is not None:
            return int(self.retry.final_still_failed)
        return self.report.still_failed_windows


@dataclass(frozen=True)
class _FakeTarget:
    """The stand-in ``GreeksEnrichTarget`` the fake wheel module exposes."""

    exchange: str
    symbol: str
    start: date
    end: date


def _nifty_instruments() -> list[Any]:
    """One real synthetic NIFTY index instrument (``symbol_name == "NIFTY"``)."""
    from brutex.universe import BRUTE_FORCE_INDICES

    return [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]


def _wire_live_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instruments: list[Any] | None = None,
    ingest: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Fake the live wiring (creds / instruments / adapter / ingest) — no network.

    Returns the list of per-call ingest kwargs so callers can assert the
    engine ran (or never ran). A ``None`` *ingest* returns a clean report.
    """
    resolved = _nifty_instruments() if instruments is None else instruments
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: resolved)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )
    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    calls: list[dict[str, Any]] = []

    def _fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> Any:
        """Record the call, then return the clean report / delegate to *ingest*."""
        calls.append(kwargs)
        if ingest is not None:
            return ingest(adapter_arg, sink_arg, **kwargs)
        return _FakeIngestResult()

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", _fake_ingest)
    return calls


def _fetch(lake: Path, **overrides: Any) -> int:
    """Run ``cmd_fetch`` over the standard 2-day window with defaults overridable."""
    kwargs: dict[str, Any] = {
        "window_days": None,
        "dry_run": False,
        "inspect": False,
        "rows_per_chunk": 5,
    }
    kwargs.update(overrides)
    return cli.cmd_fetch(lake, "2024-01-02", "2024-01-03", "1m", **kwargs)


def _seed_option_contract(
    lake: Path,
    symbol: str,
    months: Sequence[tuple[int, int]] = ((2024, 1),),
    *,
    exchange: str = "NSE",
) -> None:
    """Create ``bars/<exchange>/FNO/<symbol>/1minute/<yyyy>/<mm>.parquet`` stubs.

    0-byte ``touch`` files suffice — discovery reads directory metadata only.
    """
    for year, month in months:
        year_dir = lake / "bars" / exchange / "FNO" / symbol / "1minute" / f"{year:04d}"
        year_dir.mkdir(parents=True, exist_ok=True)
        (year_dir / f"{month:02d}.parquet").touch()


def _install_fake_greeks_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    driver_error: Exception | None = None,
) -> list[dict[str, Any]]:
    """Install a stand-in ``brutex.data.greeks_enrich`` in ``sys.modules``.

    The fence's call-time ``from brutex.data.greeks_enrich import …`` (and the
    lake target builder's ``GreeksEnrichTarget`` import) resolve against this
    module, so the REAL ``run_greeks_enrich_pass`` fence runs end-to-end with a
    recording driver — wheel-free. Returns the driver's per-call kwargs list.
    """
    calls: list[dict[str, Any]] = []
    rate_sentinel = object()
    module = types.ModuleType("brutex.data.greeks_enrich")

    def _resolve_risk_free_source() -> Any:
        """The locked PR-1 rate seam — a sentinel, resolution order pinned upstream."""
        return rate_sentinel

    def _enrich_greeks_for_targets(*, targets: Any, lake_root: Path, rate_source: Any) -> Any:
        """Record the driver call; raise *driver_error* or return the 13-field report."""
        calls.append({"targets": list(targets), "lake_root": lake_root, "rate_source": rate_source})
        if driver_error is not None:
            raise driver_error
        return SimpleNamespace(**_REPORT_FIELDS)

    setattr(module, "resolve_risk_free_source", _resolve_risk_free_source)  # noqa: B010
    setattr(module, "enrich_greeks_for_targets", _enrich_greeks_for_targets)  # noqa: B010
    setattr(module, "GreeksEnrichTarget", _FakeTarget)  # noqa: B010
    monkeypatch.setitem(sys.modules, "brutex.data.greeks_enrich", module)
    return calls


def _spy_enrich_runner(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``cli._run_cli_greeks_enrich`` with a kwargs recorder."""
    calls: list[dict[str, Any]] = []

    def _recorder(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return GREEKS_NOT_RUN

    monkeypatch.setattr(cli, "_run_cli_greeks_enrich", _recorder)
    return calls


# ────────────────────────────────────────────────────────────────────────
# (a) default live fetch → the stage runs once, done event, exit unchanged
# ────────────────────────────────────────────────────────────────────────


def test_live_fetch_runs_enrich_once_with_done_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default persisting fetch over an FNO-bearing lake enriches EXACTLY once.

    End-to-end through the REAL ``_run_cli_greeks_enrich`` + the REAL common
    fence: the discovered target carries the chain-union window clamped to the
    pulled ``[2024-01-02, 2024-01-04)`` span, ``fno.greeks_enrich.done``
    carries the driver report's 13 counters verbatim, the stderr start line
    names the kill switch, and the exit code is the pull's own ``EXIT_OK``.
    """
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    driver_calls = _install_fake_greeks_module(monkeypatch)

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    # The driver ran exactly once, over exactly the seeded contract, with the
    # stored Jan-2024 month span clamped to the pulled window (+1-day end).
    assert len(driver_calls) == 1
    assert driver_calls[0]["lake_root"] == tmp_path
    assert driver_calls[0]["targets"] == [
        _FakeTarget(exchange="NSE", symbol=_NIFTY_CE, start=date(2024, 1, 2), end=date(2024, 1, 4))
    ]
    done = [e for e in logs if e["event"] == "fno.greeks_enrich.done"]
    assert len(done) == 1
    assert done[0]["log_level"] == "info"
    for field_name, value in _REPORT_FIELDS.items():
        assert done[0][field_name] == value
    assert [e for e in logs if e["event"] == "fno.greeks_enrich.error"] == []
    captured = capsys.readouterr()
    # The start line names the kill switch (operator discoverability).
    assert "greeks enrich — post-pull pass (BRUTEX_CLI_GREEKS_ENRICH=0 skips)" in captured.err
    # errored_targets surfaces the driver's per-target quarantine tally verbatim.
    assert "[brutex] greeks enrich — done, errored_targets=6" in captured.out
    assert "[brutex] fetch done" in captured.out


def test_seam_runs_after_drains_and_ledger_persist_with_pulled_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam A ordering: drain fetch → drain write → persist ledger → enrich.

    The recorder receives the resolved lake root, the pulled instruments list
    (identity), the pulled start date, and the INCLUSIVE pull end widened +1
    day into the half-open enrichment window.
    """
    instruments = _nifty_instruments()
    _wire_live_fakes(monkeypatch, instruments=instruments)
    events: list[str] = []

    pull_pipeline = importlib.import_module("brutex.adapters.groww.pull_pipeline")
    ledger_store = importlib.import_module("brutex.data.window_ledger_store")
    real_drain_fetch = pull_pipeline.drain_fetch_pipeline
    real_drain_write = pull_pipeline.drain_write_consumer
    real_end_ledger = ledger_store.end_pull_ledger

    def _spy_drain_fetch(pipeline: Any) -> Any:
        events.append("drain_fetch")
        return real_drain_fetch(pipeline)

    def _spy_drain_write(consumer: Any) -> Any:
        events.append("drain_write")
        return real_drain_write(consumer)

    def _spy_end_ledger(*args: Any, **kwargs: Any) -> Any:
        events.append("end_ledger")
        return real_end_ledger(*args, **kwargs)

    monkeypatch.setattr(pull_pipeline, "drain_fetch_pipeline", _spy_drain_fetch)
    monkeypatch.setattr(pull_pipeline, "drain_write_consumer", _spy_drain_write)
    monkeypatch.setattr(ledger_store, "end_pull_ledger", _spy_end_ledger)

    enrich_calls: list[dict[str, Any]] = []

    def _recorder(**kwargs: Any) -> Any:
        events.append("enrich")
        enrich_calls.append(kwargs)
        return GREEKS_NOT_RUN

    monkeypatch.setattr(cli, "_run_cli_greeks_enrich", _recorder)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert events == ["drain_fetch", "drain_write", "end_ledger", "enrich"]
    assert len(enrich_calls) == 1
    assert enrich_calls[0]["lake_root"] == tmp_path
    assert enrich_calls[0]["instruments"] is instruments
    assert enrich_calls[0]["start"] == date(2024, 1, 2)
    assert enrich_calls[0]["end"] == date(2024, 1, 4)  # inclusive end → half-open +1d


def test_seam_end_date_max_saturates_instead_of_crashing_the_pull(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degenerate ``--end 9999-12-31`` pull completes; the seam saturates.

    Review round-1 regression (finding 2): the seam's ``+1 day`` widening was
    evaluated BEFORE ``_run_cli_greeks_enrich`` (outside both the fence and
    the halt except-tuple), so ``end_dt.date() == date.max`` raised an
    uncaught ``OverflowError`` AFTER the drains + ledger persist — replacing
    the completed pull's summary/exit with a traceback, violating "an enrich
    error NEVER fails the pull". The widening now saturates at ``date.max``.
    """
    _wire_live_fakes(monkeypatch)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    rc = cli.cmd_fetch(
        tmp_path,
        "9999-12-01",
        "9999-12-31",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
    )

    assert rc == cli.EXIT_OK
    assert len(enrich_calls) == 1
    assert enrich_calls[0]["start"] == date(9999, 12, 1)
    assert enrich_calls[0]["end"] == date.max  # saturated — not date.max + 1 (overflow)
    assert "[brutex] fetch done" in capsys.readouterr().out  # the pull summary survived


def test_next_day_saturating_widens_and_saturates() -> None:
    """The helper's two branches: ordinary +1 day; ``date.max`` unchanged."""
    assert cli._next_day_saturating(date(2024, 1, 2)) == date(2024, 1, 3)
    assert cli._next_day_saturating(date(9999, 12, 30)) == date.max
    assert cli._next_day_saturating(date.max) == date.max


def test_until_clean_enriches_once_after_all_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--until-clean`` runs its bounded rounds, then the stage fires ONCE."""
    ingest_calls = _wire_live_fakes(monkeypatch)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    rc = _fetch(tmp_path, until_clean=True)

    assert rc == cli.EXIT_OK
    assert len(ingest_calls) == 1  # a clean round stops the loop
    assert len(enrich_calls) == 1  # the seam runs once, AFTER all rounds


# ────────────────────────────────────────────────────────────────────────
# (i) underlyings scoping — the frozenset filter, observed
# ────────────────────────────────────────────────────────────────────────


def test_scoping_only_pulled_underlyings_reach_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the pulled underlyings' contracts are discovered as targets.

    NIFTY + BANKNIFTY chains both live in the lake; the pull resolved ONLY the
    NIFTY instrument, so the BANKNIFTY contract never reaches the driver.
    """
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _seed_option_contract(tmp_path, _BANK_CE)
    _wire_live_fakes(monkeypatch)  # resolves the NIFTY instrument only
    driver_calls = _install_fake_greeks_module(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert len(driver_calls) == 1
    assert [t.symbol for t in driver_calls[0]["targets"]] == [_NIFTY_CE]


def test_zero_target_fno_lake_logs_done_with_zeros(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An FNO lake holding ONLY out-of-scope underlyings still runs an honest pass.

    The pre-gate passes (options exist), discovery filters everything out, and
    the driver receives ZERO targets — an observable no-op ``done``, exit
    unchanged (never a fabricated skip).
    """
    _seed_option_contract(tmp_path, _BANK_CE)  # no NIFTY chain at all
    _wire_live_fakes(monkeypatch)  # pulls NIFTY only
    driver_calls = _install_fake_greeks_module(monkeypatch)

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert len(driver_calls) == 1
    assert driver_calls[0]["targets"] == []
    assert [e for e in logs if e["event"] == "fno.greeks_enrich.done"] != []
    assert "[brutex] greeks enrich — done, errored_targets=6" in capsys.readouterr().out


def test_run_cli_greeks_enrich_threads_match_token_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery filter is EXACTLY the ``underlying_match_tokens`` union.

    Recorders on the common module's ``lake_target_builder`` +
    ``run_greeks_enrich_pass`` observe the frozenset (bare AND
    exchange-prefixed AND ``underlying_symbol`` forms), the window clamp, and
    that the built factory is the one handed to the pass.
    """
    from brutex.dashboard.gated_prebuild import underlying_match_tokens

    _seed_option_contract(tmp_path, _NIFTY_CE)
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")
    builder_calls: list[dict[str, Any]] = []
    pass_calls: list[dict[str, Any]] = []
    builder_sentinel = object()

    def _fake_builder(root: Path, **kwargs: Any) -> Any:
        builder_calls.append({"root": root, **kwargs})
        return builder_sentinel

    def _fake_pass(**kwargs: Any) -> GreeksEnrichOutcome:
        pass_calls.append(kwargs)
        return GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)

    monkeypatch.setattr(gep, "lake_target_builder", _fake_builder)
    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _fake_pass)

    instruments: list[Any] = [
        _FakeInstrument("NSE-NIFTY"),
        _FakeInstrument("BANKNIFTY", underlying_symbol="NIFTYBANK"),
    ]
    outcome = cli._run_cli_greeks_enrich(
        lake_root=tmp_path,
        instruments=instruments,
        start=date(2024, 1, 2),
        end=date(2024, 1, 4),
    )

    expected = frozenset[str]().union(*(underlying_match_tokens(i) for i in instruments))
    assert expected == frozenset({"NSE-NIFTY", "NIFTY", "BANKNIFTY", "NIFTYBANK"})
    assert builder_calls == [
        {
            "root": tmp_path,
            "underlyings": expected,
            "start": date(2024, 1, 2),
            "end": date(2024, 1, 4),
        }
    ]
    assert pass_calls == [{"build_targets": builder_sentinel, "lake_root": tmp_path}]
    assert outcome == GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)


# ────────────────────────────────────────────────────────────────────────
# (b) pure-spot lake — the cheap pre-gate skip
# ────────────────────────────────────────────────────────────────────────


def test_run_cli_greeks_enrich_spot_only_lake_skips_with_debug_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No FNO tree → ``GREEKS_NOT_RUN`` (identity), one DEBUG event, zero prints."""
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")

    def _never(**kwargs: Any) -> Any:
        raise AssertionError("the pass must not be constructed on a pure-spot lake")

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _never)

    with capture_logs() as logs:
        outcome = cli._run_cli_greeks_enrich(
            lake_root=tmp_path,
            instruments=[_FakeInstrument("NIFTY")],
            start=date(2024, 1, 2),
            end=date(2024, 1, 4),
        )

    assert outcome is GREEKS_NOT_RUN
    skipped = [e for e in logs if e["event"] == "cli.greeks_enrich.skipped_no_options"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "debug"
    assert skipped[0]["lake_root"] == str(tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cmd_fetch_spot_only_lake_skips_via_pre_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisting fetch into a pure-spot lake skips the stage cheaply.

    The dominant CLI case: the pre-gate answers in O(exchange dirs), one DEBUG
    event fires, the common pass is never constructed, and no operator line is
    printed for the stage.
    """
    _wire_live_fakes(monkeypatch)
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")

    def _never(**kwargs: Any) -> Any:
        raise AssertionError("the pass must not run on a pure-spot lake")

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _never)

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    events = [e["event"] for e in logs]
    assert events.count("cli.greeks_enrich.skipped_no_options") == 1
    assert not any(e.startswith("fno.greeks_enrich") for e in events)
    assert "greeks enrich" not in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────
# (c) + on-values — the BRUTEX_CLI_GREEKS_ENRICH kill switch
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "off", "FALSE"])
def test_env_kill_switch_skips_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """An OFF token skips the stage entirely on an otherwise-eligible pull."""
    monkeypatch.setenv("BRUTEX_CLI_GREEKS_ENRICH", value)
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert enrich_calls == []
    assert "greeks enrich" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", "1", "on", "TRUE"])
def test_env_on_values_reach_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Every ON token (blank included — default-ON) reaches the enrich seam."""
    monkeypatch.setenv("BRUTEX_CLI_GREEKS_ENRICH", value)
    _wire_live_fakes(monkeypatch)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert len(enrich_calls) == 1


# ────────────────────────────────────────────────────────────────────────
# (d) junk env value — EXIT_USER_ERROR before ANY vendor work
# ────────────────────────────────────────────────────────────────────────


def test_env_junk_fails_fast_before_any_vendor_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junk switch value refuses BEFORE the adapter is built or a fetch runs.

    The gate folds into the pre-pull intervals ``try`` (the DEC-RAM-BUDGETS-001
    fail-loud env convention), so a junk value can never fail an
    already-completed paced pull.
    """
    monkeypatch.setenv("BRUTEX_CLI_GREEKS_ENRICH", "banana")
    ingest_calls = _wire_live_fakes(monkeypatch)
    builds: list[Any] = []

    def _never_build(lake: Path, catalog: Any = None) -> Any:
        builds.append(lake)
        raise AssertionError("the adapter must never be built on a junk env gate")

    # Over-pin the wired fake with a raiser: reaching the adapter build at all
    # would fail the test loudly, not just trip the empty-list asserts below.
    monkeypatch.setattr(cli, "_build_live_groww_spot", _never_build)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "BRUTEX_CLI_GREEKS_ENRICH='banana' is not a recognised switch value" in captured.err
    assert builds == []  # zero adapter builds
    assert ingest_calls == []  # zero vendor/ingest calls
    assert enrich_calls == []  # and of course no enrichment


# ────────────────────────────────────────────────────────────────────────
# (e) --dry-run / --inspect — the stage is fully inert (_no_persist)
# ────────────────────────────────────────────────────────────────────────


def test_dry_run_is_inert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` never touches the stage — even with options in the lake.

    The REAL offline synthetic pipeline runs to completion; the seam's
    ``_no_persist`` gate leaves the runner uncalled, so there is no discovery,
    no pre-gate probe, no event, no print.
    """
    _seed_option_contract(tmp_path, _NIFTY_CE)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    with capture_logs() as logs:
        rc = _fetch(tmp_path, dry_run=True, rows_per_chunk=2)

    assert rc == cli.EXIT_OK
    assert enrich_calls == []
    events = [e["event"] for e in logs]
    assert not any(e.startswith("fno.greeks_enrich") for e in events)
    assert not any(e.startswith("cli.greeks_enrich") for e in events)
    assert "greeks enrich" not in capsys.readouterr().err


def test_inspect_is_inert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--inspect`` (NullBarSink, no persistence) skips the stage too."""
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    enrich_calls = _spy_enrich_runner(monkeypatch)

    with capture_logs() as logs:
        rc = _fetch(tmp_path, inspect=True)

    assert rc == cli.EXIT_OK
    assert enrich_calls == []
    events = [e["event"] for e in logs]
    assert not any(e.startswith("fno.greeks_enrich") for e in events)
    assert not any(e.startswith("cli.greeks_enrich") for e in events)
    assert "greeks enrich" not in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────
# (f) fenced enrichment failure — the pull NEVER fails
# ────────────────────────────────────────────────────────────────────────


def test_enrich_failure_never_fails_fetch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic enrichment error is fenced: one ``.error`` event, exit unchanged."""
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    _install_fake_greeks_module(monkeypatch, driver_error=RuntimeError("boom"))

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK  # the clean pull's exit code — never flipped
    errors = [e for e in logs if e["event"] == "fno.greeks_enrich.error"]
    assert len(errors) == 1
    assert errors[0]["error"] == "boom"
    assert [e for e in logs if e["event"] == "fno.greeks_enrich.done"] == []
    captured = capsys.readouterr()
    assert "greeks enrich FAILED (RuntimeError: boom)" in captured.err
    assert "overlay re-attempted" in captured.err
    assert "[brutex] fetch done" in captured.out  # the pull itself completed


def test_run_cli_greeks_enrich_prints_ok_tally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ok-branch surfaces the per-target quarantine tally verbatim."""
    _seed_option_contract(tmp_path, _NIFTY_CE)
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")
    outcome = GreeksEnrichOutcome(ok=True, error=None, errored_targets=3)
    monkeypatch.setattr(gep, "run_greeks_enrich_pass", lambda **kwargs: outcome)

    result = cli._run_cli_greeks_enrich(
        lake_root=tmp_path,
        instruments=[_FakeInstrument("NIFTY")],
        start=date(2024, 1, 2),
        end=date(2024, 1, 4),
    )

    assert result is outcome  # the seam returns the pass outcome verbatim
    assert "[brutex] greeks enrich — done, errored_targets=3" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────────────────
# (g)+(h) HOST halts from the enrich stage — alert once, then re-raise
# ────────────────────────────────────────────────────────────────────────


def _enrich_halts() -> list[Exception]:
    """The two HOST conditions the enrich seam must alert on, then re-raise."""
    return [
        LakeWriteHalt("disk_full"),
        GreeksEnrichPoolBroken("worker terminated abruptly"),
    ]


@pytest.mark.parametrize("halt", _enrich_halts(), ids=lambda e: type(e).__name__)
def test_halt_from_enrich_alerts_once_then_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, halt: Exception
) -> None:
    """A pull-wide halt mid-enrich fires ``_emit_pull_halt_alert`` ONCE, then re-raises.

    The ``_one_fetch_pass`` halt convention at the enrich seam: the drains +
    the durable ledger persist already ran (the earlier ``finally``), the
    CRITICAL alert fires exactly once, and the halt propagates into the
    existing CLI halt handling (no summary print, non-zero via ``main``).
    """
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")

    def _raise_halt(**kwargs: Any) -> Any:
        raise halt

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _raise_halt)

    ledger_store = importlib.import_module("brutex.data.window_ledger_store")
    real_end_ledger = ledger_store.end_pull_ledger
    ledger_persists: list[str] = []

    def _spy_end_ledger(*args: Any, **kwargs: Any) -> Any:
        ledger_persists.append("end_ledger")
        return real_end_ledger(*args, **kwargs)

    monkeypatch.setattr(ledger_store, "end_pull_ledger", _spy_end_ledger)

    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(type(halt)):
        _fetch(tmp_path)

    assert halts == [halt]  # the alert fired exactly once with the propagating halt
    assert ledger_persists == ["end_ledger"]  # settle-after-durable held BEFORE the halt


def test_halted_pull_never_reaches_enrich(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seam A sits on the NON-raising path: an ingest halt skips the stage."""
    _seed_option_contract(tmp_path, _NIFTY_CE)

    def _halting_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> Any:
        raise LakeWriteHalt("disk_full")

    _wire_live_fakes(monkeypatch, ingest=_halting_ingest)
    enrich_calls = _spy_enrich_runner(monkeypatch)
    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(LakeWriteHalt):
        _fetch(tmp_path)

    assert enrich_calls == []  # the halt propagated past the seam — never enriched
    assert len(halts) == 1  # the ingest halt's own alert (the _one_fetch_pass site)


def test_keyboard_interrupt_from_enrich_propagates_without_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KeyboardInterrupt`` is a ``BaseException`` — never fenced, never alerted."""
    _seed_option_contract(tmp_path, _NIFTY_CE)
    _wire_live_fakes(monkeypatch)
    gep = importlib.import_module("brutex.data.greeks_enrich_pass")

    def _interrupt(**kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _interrupt)
    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(KeyboardInterrupt):
        _fetch(tmp_path)

    assert halts == []  # the operator's interrupt is not a pull-halt condition
