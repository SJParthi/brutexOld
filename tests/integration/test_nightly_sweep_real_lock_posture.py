"""REAL-sweep-leg proof of the nightly lock posture (DEC-NIGHTLY-SWEEP-LOCK-POSTURE-001).

The FAILS-BEFORE regression net for MIGRATION.md #1772 — the nightly twin of the
#1770 dashboard one-click-sweep self-deadlock: the nightly chain holds the lake
``pull_singleflight_lock`` EX "through the SWEEP link" (the deliberate #1612
R4/G10 sweep-vs-pull exclusion law, written PRE-G10 when "no sweep-level flock
exists"), and its sweep leg defaults to the REAL ``cmd_sweep_all`` — whose G10
``lake_read_guard`` ``LOCK_SH | LOCK_NB`` on the SAME lock file (a second fd)
fails against the nightly's OWN held EX → the SWEEP link died every night with
``LakeBusyError``. Every prior nightly test injected ``seams.run_sweep`` (the
fixture blindspot twin), so the real sweep leg under the production lock chain
was never once executed.

This suite drives the REAL chain — ``run_nightly`` with REAL locks + the REAL
``cli.cmd_sweep_all`` (``seams.run_sweep=None``) — over a tiny REAL lake, plus
the LAW pins: during the ENTIRE sweep leg a pull's EX acquire is still refused
(with the honest ``(holder: nightly:<id>)`` note), while a READER's SH guard
(what cmd_sweep_all's G10 takes) composes; a lost EX→SH reacquire race refuses
the leg LOUDLY (fail-safe), never silently.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import structlog.testing

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from collections.abc import Generator

from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
from brutex.config.credentials_types import CredentialsError
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.sweep_singleflight import LakeBusyError, lake_read_guard
from brutex.nightly.chain import LinkStatus
from brutex.nightly.run import NightlyPlan, NightlySeams, run_nightly
from brutex.nightly.sweep_binding import sweep_supervisor_audit_path

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the disk floors (the e2e-suite precedent) + the sequential sweep path."""
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


class RecordingAlerter:
    def __init__(self) -> None:
        self.summaries: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []

    def nightly_summary(self, **kwargs: Any) -> bool:
        self.summaries.append(kwargs)
        return True

    def nightly_refused(self, **kwargs: Any) -> bool:
        self.refused.append(kwargs)
        return True


class FakePipeline:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@contextmanager
def _noop_keep_awake(reason: str) -> Generator[None]:
    yield


@dataclass
class _FakeHistorical:
    bars_appended: int = 42


@dataclass
class _FakeComposite:
    historical: _FakeHistorical | None = field(default_factory=_FakeHistorical)


@dataclass
class _FakePipeReport:
    """The minimal green pipeline report shape (the e2e-suite fake, verbatim)."""

    ok: bool = True
    note: str = "pipeline complete"
    stopped_after: str | None = None
    quarantined_count: int = 0
    historical: _FakeComposite | None = field(default_factory=_FakeComposite)
    prebuild_built: int | None = 2
    prebuild_crashed: str | None = None
    prebuild_disk_guarded: bool = False
    prebuild_failed: tuple[str, ...] = ()
    post_steps_strict_skipped: bool = False


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _real_lake(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny REAL lake (real writer bars + catalog) + the e2e backup sidecars."""
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    writer = ParquetWriter(lake_root=lake)
    with Catalog.open(lake / "catalog.duckdb") as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    # The backup-inventory sidecars the e2e fixture seeds (tiny stand-in bytes;
    # none of these is read by cmd_sweep_all).
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER-BYTES")
    (lake / "registry.duckdb").write_bytes(b"REGISTRY")
    (lake / "instruments.duckdb").write_bytes(b"INSTRUMENTS")
    return lake, results


def _sweep_kwargs(lake: Path, results: Path) -> dict[str, Any]:
    """The tiny REAL ``cmd_sweep_all`` kwargs (the real-sweep-binding test shape)."""
    return {
        "lake_root": lake,
        "catalog_path": lake / "catalog.duckdb",
        "results_root": results,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def _plan(lake: Path, results: Path, tmp_path: Path) -> NightlyPlan:
    return NightlyPlan(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "backups",
        keep=7,
        from_date=None,
        to_date=None,
        sweep_kwargs=_sweep_kwargs(lake, results),
    )


def _seams(
    monkeypatch: pytest.MonkeyPatch,
    alerter: RecordingAlerter,
    *,
    run_sweep: Any = None,
) -> NightlySeams:
    """The e2e fakes for the VENDOR legs; ``run_sweep=None`` keeps the REAL sweep."""
    pipeline = FakePipeline()
    monkeypatch.setattr(
        "brutex.nightly.run.execute_headless_pipeline", lambda _hp: _FakePipeReport()
    )

    def no_creds() -> Any:
        raise CredentialsError("no AWS in the fixture")

    return NightlySeams(
        build_pipeline=cast(Any, lambda **_kw: pipeline),
        run_sweep=run_sweep,
        alerter=cast(Any, alerter),
        keep_awake_cm=_noop_keep_awake,
        credentials=no_creds,
    )


def _link(report: Any, name: str) -> Any:
    (outcome,) = [o for o in report.links if o.link.value == name]
    return outcome


def test_nightly_sweep_leg_completes_with_the_real_cmd_sweep_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The REAL nightly sweep leg completes — the permanent fails-before regression.

    On unfixed main the SWEEP link dies with ``LakeBusyError`` (error_type on
    the link outcome): the nightly's own whole-run pull EX flock made
    ``cmd_sweep_all``'s G10 SH acquire fail from a second fd — the exact
    self-conflict class of the #1770 dashboard fix, on the nightly surface.
    """
    lake, results = _real_lake(tmp_path)
    alerter = RecordingAlerter()
    report = run_nightly(_plan(lake, results, tmp_path), seams=_seams(monkeypatch, alerter))

    sweep = _link(report, "sweep")
    assert sweep.error_type != "LakeBusyError", f"self-deadlock: {sweep.detail!r}"
    assert sweep.status is LinkStatus.OK, f"sweep leg not green: {sweep.detail!r}"
    assert report.exit_code == 0
    # REAL sweep artifacts landed: the supervisor completion chain + a
    # per-tuple results store — never a faked SUPERVISOR_COMPLETE line.
    assert sweep_supervisor_audit_path(results).exists()
    assert any(p for p in results.rglob("*.parquet"))
    # The counts on the morning summary came from the REAL supervisor tally.
    assert report.counts["sweep_run"] is not None


def test_pull_ex_refused_for_the_whole_sweep_leg_and_reader_sh_composes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #1612 law's INTENT holds for the WHOLE leg — and the G10 reader composes.

    The injected probe runs where the sweep body runs (no inner SH of its own,
    so the ONLY exclusion in force is the nightly-level whole-leg guard):

    * a pull's EX acquire is REFUSED with the honest ``(holder: nightly:<id>)``
      note — the law + the holder-note honesty, both preserved;
    * a READER's SH guard (exactly what cmd_sweep_all's G10 takes) is ADMITTED —
      the self-conflict is dead (this arm FAILS on unfixed main, where the
      nightly still holds EX through the leg).
    """
    lake, results = _real_lake(tmp_path)
    alerter = RecordingAlerter()
    probe: dict[str, Any] = {}

    def probing_run_sweep(**_kwargs: Any) -> int:
        try:
            with pull_singleflight_lock(lake):  # pragma: no cover — must NOT acquire
                probe["pull_ex"] = "acquired"
        except PullAlreadyRunningError as exc:
            probe["pull_ex"] = str(exc)
        try:
            with lake_read_guard(lake):
                probe["reader_sh"] = "acquired"
        except LakeBusyError as exc:  # pragma: no cover — the pre-fix self-conflict
            probe["reader_sh"] = str(exc)
        # A green leg needs a fresh completion tally — write it like the e2e fake.
        from brutex.engine.sweep_audit import SweepAuditWriter
        from brutex.engine.sweep_supervisor import SUPERVISOR_ACTION_COMPLETE

        path = results / "audit" / "sweep-all-supervisor.jsonl"
        with SweepAuditWriter.open(path, actor="sweep", resume=path.exists()) as audit:
            audit.append(
                action=SUPERVISOR_ACTION_COMPLETE,
                subject="sweep:probe",
                payload={
                    "tuples_total": 1,
                    "tuples_run": 1,
                    "skipped_cache": 0,
                    "skipped_resume": 0,
                    "tuples_skipped_no_bars": 0,
                    "tuples_failed": 0,
                },
            )
        return 0

    with structlog.testing.capture_logs() as logs:
        report = run_nightly(
            _plan(lake, results, tmp_path),
            seams=_seams(monkeypatch, alerter, run_sweep=probing_run_sweep),
        )
    sweep = _link(report, "sweep")
    assert sweep.status is LinkStatus.OK, f"sweep leg not green: {sweep.detail!r}"
    # LAW: the pull writer stays excluded for the whole leg…
    assert "a pull is already running for this lake" in probe["pull_ex"]
    # …with the operator-facing holder note preserved through the posture change.
    assert "(holder: nightly:" in probe["pull_ex"]
    # MECHANISM: the reader guard (cmd_sweep_all's G10 shape) composes freely.
    assert probe["reader_sh"] == "acquired"
    # OBSERVABILITY (review-round NIT 2): the transition emits exactly one
    # INFO with the mode token, and the lost-race WARN never fires on the
    # happy path.
    downgraded = [e for e in logs if e["event"] == "nightly.sweep_lock_downgraded"]
    assert len(downgraded) == 1
    assert downgraded[0]["mode"] == "EX->SH"
    assert downgraded[0]["log_level"] == "info"
    assert not any(e["event"] == "nightly.sweep_lock_downgrade_lost_race" for e in logs)


def test_lost_reacquire_race_refuses_the_sweep_leg_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lost EX→SH reacquire race is a LOUD REFUSED sweep link — never silent.

    The release→reacquire window is a few syscalls wide; a peer pull's EX
    landing inside it makes the nightly's whole-leg SH acquire raise
    ``LakeBusyError``. Deterministically forced via the guard seam: the sweep
    body must NEVER run (no half-covered sweep beside a live pull), the link
    reads REFUSED naming the contention, and the exit code is non-zero.
    """
    from brutex.engine import sweep_singleflight

    lake, results = _real_lake(tmp_path)
    alerter = RecordingAlerter()
    calls: list[str] = []

    @contextmanager
    def raising_guard(lake_root: Path) -> Generator[None]:
        raise LakeBusyError(lake_root / ".locks" / "pull_singleflight.lock")
        yield  # pragma: no cover — never reached

    monkeypatch.setattr(sweep_singleflight, "lake_read_guard", raising_guard)

    def never_runs(**_kwargs: Any) -> int:  # pragma: no cover — must not be called
        calls.append("ran")
        return 0

    with structlog.testing.capture_logs() as logs:
        report = run_nightly(
            _plan(lake, results, tmp_path),
            seams=_seams(monkeypatch, alerter, run_sweep=never_runs),
        )
    sweep = _link(report, "sweep")
    assert calls == []
    assert sweep.status is LinkStatus.REFUSED
    assert "pull_in_flight" in sweep.detail
    assert report.exit_code != 0
    # OBSERVABILITY (review-round NIT 2): the lost race WARNs by name — a
    # regression dropping the WARN while keeping REFUSED must fail here —
    # and the happy-path INFO never fires on a refused transition.
    lost = [e for e in logs if e["event"] == "nightly.sweep_lock_downgrade_lost_race"]
    assert len(lost) == 1
    assert lost[0]["log_level"] == "warning"
    assert "pull single-flight lock" in lost[0]["error"]
    assert not any(e["event"] == "nightly.sweep_lock_downgraded" for e in logs)
