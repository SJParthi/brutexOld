# pyright: reportPrivateUsage=none
"""L2 post-pull lake heal at the CLI ``brutex fetch`` seam (DEC-CLI-LAKE-HEAL-L2-001).

Pins the CLI twin of the dashboard's post-drain L2 heal wiring
(DEC-LAKE-HEAL-001, MIGRATION.md #1175 — this closes its named "the CLI
``brutex fetch`` path has no L2 seam" follow-up): ``cmd_fetch``'s ``finally``
calls the SAME dashboard glue
(:func:`brutex.dashboard.pull_controller._heal_after_pull` — CLAUDE.md §11
COMMON, zero copied healer logic) strictly AFTER both drains and BEFORE
``end_pull_ledger``, on the SUCCESS path only (``fetch_ok`` latch), with
``--dry-run`` / ``--inspect`` fully INERT (``_no_persist`` gate — zero scan,
zero writes). A heal-raised :class:`~brutex.data.errors.LakeWriteHalt` fires
the CRITICAL pull-halt Telegram alert exactly once
(DEC-TELEGRAM-PULL-HALT-001) then RE-RAISES (non-zero exit via ``main``'s raw
propagation); an ingest-raised halt leaves the latch unarmed so the heal
never double-alerts; every other heal error is swallowed + logged
``lake_heal.post_pull_error`` and the ledger persist still runs.

Harness: the proven ``test_cli_retry_storm.py`` live-pull fakes (creds gate /
adapter build / ingest all faked — no network, no Groww credentials) over a
REAL temp lake; poison planted with the shared raw-pyarrow fixture
(``tests.fakes.lake_poison``, the #1067 convention — the fixed writer refuses
duplicates). The end-to-end dup test runs the REAL glue + REAL heal core,
including the real (Telegram-disabled) alert seam — the best-effort
alert-disabled no-crash pin.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import brutex.data.lake_heal as lake_heal_module
from brutex.dashboard import pull_controller as pc
from brutex.data.errors import LakeWriteHalt
from brutex.data.partition import build_partition_path
from brutex.universe import BRUTE_FORCE_INDICES

from tests.fakes.lake_poison import poison_partition, stored_timestamps

# ── the proven cmd_fetch live-pull fakes (test_cli_retry_storm shapes) ───────


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary printing reads.

    Extends the ``test_cli_retry_storm`` shape with ``auth_aborted`` (default
    ``False``) — the live ``--until-clean`` round runner reads it when folding
    a pass into an ``UntilCleanRound``.
    """

    windows: int
    bars_appended: int
    failed_pairs: int
    transient_failures: int
    still_failed_windows: int
    auth_aborted: bool = False
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` (report + no retry outcome)."""

    report: _FakeIngestReport
    retry: None = None

    @property
    def still_failed_windows(self) -> int:
        """Mirror the real wrapper: no retry ran, the report residual wins."""
        return self.report.still_failed_windows


def _clean_result() -> _FakeIngestResult:
    """A clean pass — 1 window, bars stored, nothing failed."""
    return _FakeIngestResult(
        report=_FakeIngestReport(
            windows=1,
            bars_appended=375,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        )
    )


def _failing_result(still_failed: int) -> _FakeIngestResult:
    """A pass with *still_failed* re-pullable windows (no terminal failures)."""
    return _FakeIngestResult(
        report=_FakeIngestReport(
            windows=still_failed,
            bars_appended=0,
            failed_pairs=0,
            transient_failures=still_failed,
            still_failed_windows=still_failed,
        )
    )


def _install_live_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ingest: Any = None,
    instruments: list[Any] | None = None,
) -> list[Any]:
    """Install the proven live-pull fakes; return the resolved instruments.

    Mirrors the ``test_cli_retry_storm.py`` harness exactly: real synthetic
    index ``Instrument``s (the glue reads ``exch_id`` / ``segment`` enums), a
    ``None`` creds-gate error, an inert adapter/sink build, and a faked ingest
    spine (default: one clean pass).
    """
    resolved = (
        [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
        if instruments is None
        else instruments
    )
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: resolved)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(
        cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
    )
    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    fake = ingest if ingest is not None else (lambda *_a, **_k: _clean_result())
    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake)
    return resolved


def _fetch(
    lake_root: Path,
    *,
    start: str = "2024-01-02",
    end: str = "2024-01-05",
    dry_run: bool = False,
    inspect: bool = False,
    until_clean: bool = False,
) -> int:
    """One ``cmd_fetch`` invocation with the suite's standard arguments."""
    return cli.cmd_fetch(
        lake_root,
        start,
        end,
        "1m",
        window_days=None,
        dry_run=dry_run,
        inspect=inspect,
        rows_per_chunk=5,
        until_clean=until_clean,
    )


def _jan_ts(minute: int) -> datetime:
    """A UTC instant inside the January-2024 partition (Wed 3 Jan, IST session)."""
    return datetime(2024, 1, 3, 4, minute, tzinfo=UTC)


def _poison_spot_jan(lake_root: Path, symbol: str) -> Path:
    """Poison *symbol*'s Jan-2024 1-minute month with a verbatim duplicate."""
    dest = build_partition_path(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1minute",
        year=2024,
        month=1,
    )
    poison_partition(
        dest,
        [
            (_jan_ts(16), 100.0, 10),  # keep-first winner
            (_jan_ts(16), 999.0, 99),  # the poisoned duplicate
            (_jan_ts(17), 101.0, 11),
        ],
    )
    return dest


# ── wiring: kwargs, ordering, halt handling, inert modes ────────────────────


def test_live_fetch_heals_post_drain_with_exact_kwargs_and_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean live pull calls the dashboard glue ONCE, before the ledger persist.

    Pins the exact call shape: the resolved instruments list, the PRODUCTION
    lake root, the parsed CLI date span, and a fresh non-empty 12-hex
    ``pull_id`` — plus the drain → heal → ``end_pull_ledger`` ordering the
    dashboard seam uses (pull_controller.py's post-drain law).
    """
    instruments = _install_live_fakes(monkeypatch)
    order: list[str] = []
    calls: list[dict[str, Any]] = []

    def heal_spy(**kwargs: Any) -> None:
        order.append("heal")
        calls.append(kwargs)

    monkeypatch.setattr(pc, "_heal_after_pull", heal_spy)

    ledger_store = importlib.import_module("brutex.data.window_ledger_store")
    real_end = ledger_store.end_pull_ledger

    def end_spy(*args: Any, **kwargs: Any) -> Any:
        order.append("ledger")
        return real_end(*args, **kwargs)

    monkeypatch.setattr(ledger_store, "end_pull_ledger", end_spy)

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert order == ["heal", "ledger"]  # heal strictly BEFORE the ledger persist
    (kwargs,) = calls
    assert kwargs["instruments"] is instruments
    assert kwargs["lake_root"] == tmp_path
    assert kwargs["start"] == date(2024, 1, 2)
    assert kwargs["end"] == date(2024, 1, 5)
    pull_id = kwargs["pull_id"]
    assert isinstance(pull_id, str)
    assert len(pull_id) == 12
    int(pull_id, 16)  # 12-hex — the correlation-id mint contract


def test_heal_lake_write_halt_alerts_once_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heal-raised ``LakeWriteHalt`` fires the pull-halt alert ONCE, then re-raises.

    The CLI has no ``BackgroundPullJob`` halt net above the seam, so the seam
    itself owns the DEC-TELEGRAM-PULL-HALT-001 alert-then-re-raise contract —
    the raw propagation out of ``cmd_fetch`` is the existing non-zero exit path.
    """
    _install_live_fakes(monkeypatch)

    def heal_halt(**_kwargs: Any) -> None:
        raise LakeWriteHalt("disk_full")

    monkeypatch.setattr(pc, "_heal_after_pull", heal_halt)
    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(LakeWriteHalt):
        _fetch(tmp_path)

    assert len(halts) == 1
    assert isinstance(halts[0], LakeWriteHalt)
    assert halts[0].reason == "disk_full"


def test_ingest_halt_skips_heal_single_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An INGEST halt leaves the latch unarmed — the heal never runs, ONE alert total.

    Pins the ``fetch_ok`` success-path latch: the ingest-path alert already
    fired (``_one_fetch_pass``'s except), so a second heal-side alert would
    break alert-once. The heal is skipped entirely on the halt path.
    """

    def failing_ingest(*_args: Any, **_kwargs: Any) -> Any:
        raise LakeWriteHalt("disk_full")

    _install_live_fakes(monkeypatch, ingest=failing_ingest)
    heal_calls: list[str] = []
    monkeypatch.setattr(pc, "_heal_after_pull", lambda **_kw: heal_calls.append("heal"))
    halts: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

    with pytest.raises(LakeWriteHalt):
        _fetch(tmp_path)

    assert heal_calls == []  # the latch never armed → no heal after a halt
    assert len(halts) == 1  # the ingest-path alert only — alert-once holds


@pytest.mark.parametrize(
    ("dry_run", "inspect"),
    [(True, False), (False, True), (True, True)],
    ids=["dry-run", "inspect", "dry-run+inspect"],
)
def test_dry_run_and_inspect_never_heal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, inspect: bool
) -> None:
    """``--dry-run`` / ``--inspect`` stay persistence-free — the heal is INERT.

    The ``_no_persist`` gate closes the seam: zero scan, zero heal-audit
    write, zero quarantine dir on the PRODUCTION lake, and the exit code is
    byte-unchanged from the pre-seam behaviour.
    """
    _install_live_fakes(monkeypatch)

    def never_heal(**_kwargs: Any) -> None:
        raise AssertionError("the heal must never run for --dry-run / --inspect")

    monkeypatch.setattr(pc, "_heal_after_pull", never_heal)

    rc = _fetch(tmp_path, dry_run=dry_run, inspect=inspect)

    assert rc == cli.EXIT_OK
    assert not (tmp_path / "audit" / "lake-heal.jsonl").exists()
    assert not (tmp_path / ".quarantine").exists()


# ── end-to-end: real glue + real heal core over a real temp lake ─────────────


def test_real_dup_healed_keep_first_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poisoned pulled month is HEALED keep-FIRST by the real glue + core.

    No heal patches: the pull runs the REAL dashboard glue and the REAL
    COMMON heal core against the CLI's production lake — the duplicate row is
    dropped (first occurrence wins), the original bytes are preserved under
    ``.quarantine/<ts>/<relpath>``, and the hash-chained heal audit exists.
    The real (Telegram-disabled) alert seam runs too — the best-effort
    alert-disabled no-crash pin.
    """
    instruments = _install_live_fakes(monkeypatch)
    dest = _poison_spot_jan(tmp_path, instruments[0].symbol_name)
    original_bytes = dest.read_bytes()

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert stored_timestamps(dest) == [_jan_ts(16), _jan_ts(17)]  # keep-FIRST
    quarantined = list((tmp_path / ".quarantine").rglob("01.parquet"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original_bytes
    assert (tmp_path / "audit" / "lake-heal.jsonl").exists()


def test_observe_mode_scans_never_mutates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRUTEX_LAKE_HEAL=observe`` reports the poison and mutates NOTHING."""
    instruments = _install_live_fakes(monkeypatch)
    monkeypatch.setenv("BRUTEX_LAKE_HEAL", "observe")
    dest = _poison_spot_jan(tmp_path, instruments[0].symbol_name)
    original_bytes = dest.read_bytes()

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    assert dest.read_bytes() == original_bytes  # byte-untouched
    assert not (tmp_path / ".quarantine").exists()
    assert any(entry["event"] == "lake_heal.observe_only" for entry in logs)


def test_heal_generic_error_swallowed_exit_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-halt heal error is swallowed + logged; the ledger persist still runs.

    The REAL glue's blanket except owns the swallow (a heal bug never fails a
    successful pull); only the heal CORE is stubbed to raise.
    """
    _install_live_fakes(monkeypatch)

    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("heal bug")

    monkeypatch.setattr(lake_heal_module, "heal_after_pull", boom)

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    events = [entry for entry in logs if entry["event"] == "lake_heal.post_pull_error"]
    assert len(events) == 1
    assert "heal bug" in str(events[0]["error"])
    ledger_store = importlib.import_module("brutex.data.window_ledger_store")
    assert ledger_store.load_window_ledger(tmp_path).records()  # end_pull_ledger ran


def test_empty_pulled_set_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero resolved instruments → the real glue scans nothing and heals nothing.

    Mirrors the dashboard spot glue exactly (no early-return): the core runs
    with ``targets=[]``, logs an honest ``targets=0`` scan, and the pull's
    exit path is unchanged.
    """
    _install_live_fakes(monkeypatch, instruments=[])

    with capture_logs() as logs:
        rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    scans = [entry for entry in logs if entry["event"] == "lake_heal.post_pull_scan"]
    assert len(scans) == 1
    assert scans[0]["targets"] == 0
    assert not (tmp_path / ".quarantine").exists()


def test_until_clean_heals_once_after_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live ``--until-clean`` pull heals EXACTLY once, after the FINAL round.

    The latch arms after the whole until-clean loop returns, so a 2-round
    pull (failing round → clean round) runs both rounds first and the heal
    exactly once in the ``finally`` — never once per round.
    """
    events: list[str] = []
    results = iter([_failing_result(2), _clean_result()])

    def two_round_ingest(*_args: Any, **_kwargs: Any) -> Any:
        events.append("round")
        return next(results)

    _install_live_fakes(monkeypatch, ingest=two_round_ingest)
    monkeypatch.setattr(pc, "_heal_after_pull", lambda **_kw: events.append("heal"))

    rc = _fetch(tmp_path, until_clean=True)

    assert rc == cli.EXIT_OK
    assert events == ["round", "round", "heal"]


def test_heal_scope_catalog_audit_alert_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL glue threads scope / catalog / audit / alert into the heal core.

    The core is spied while the real glue runs: the scope is the dashboard's
    ``pull:<id>`` token (never a CLI-specific prefix), the catalog is the
    OPEN second connection to the live pull's ``catalog.duckdb`` (the
    same-process double-open the seam relies on), the hash-chained audit sink
    is real, and the captured ``on_report`` fires ``emit_lake_heal_alert``
    with the SAME scope (one alert per batch rides the emitter).
    """
    _install_live_fakes(monkeypatch)
    captured: dict[str, Any] = {}

    def core_spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(lake_heal_module, "heal_after_pull", core_spy)
    alerts: list[tuple[Any, str]] = []
    alerts_module = importlib.import_module("brutex.notifications.sweep_alerts")
    monkeypatch.setattr(
        alerts_module,
        "emit_lake_heal_alert",
        lambda report, *, scope: alerts.append((report, scope)),
    )

    rc = _fetch(tmp_path)

    assert rc == cli.EXIT_OK
    scope = captured["scope"]
    assert scope.startswith("pull:")
    assert len(scope) > len("pull:")  # a non-empty pull id after the prefix
    assert captured["catalog"] is not None  # catalog.duckdb existed → opened
    assert captured["audit"] is not None  # the hash-chained heal audit sink
    marker = object()
    captured["on_report"](marker)
    assert alerts == [(marker, scope)]  # the alert seam carries the SAME scope
