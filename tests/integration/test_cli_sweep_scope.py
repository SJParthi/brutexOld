"""End-to-end proof of the sweep-entry scope guard (DEC-ENGINE-SCOPE-GUARD-001).

The FAILS-BEFORE / PASSES-AFTER regression suite for Layer 2 at the production
launch mouths: before this decision, ``brutex sweep-all --symbols INDIAVIX``
over a VIX-seeded lake happily swept India VIX into the brute-force engine —
a CLAUDE.md §2 Rule 3 scope-lock violation. Now:

* ``cmd_sweep`` / ``cmd_sweep_all`` REFUSE an out-of-scope launch with the law
  citation, at ZERO lake I/O cost (reconcile/feed spies prove no read), and
  the scope refusal BEATS the misleading preflight "ZERO bars" message;
* the CLI-only ``--allow-out-of-scope`` research escape proceeds LOUDLY —
  WARNING + ``sweep.scope_override`` structlog + a hash-chained
  ``SWEEP_SCOPE_OVERRIDE`` audit line stamping ``scope_override: True``;
* the unattended daemon REFUSES the override flag outright;
* the funnel lock: the daemon and the dashboard runner both reach
  ``cmd_sweep_all`` (so neither can bypass the guard).

``cmd_sweep_all`` is ``# pragma: no cover`` (an orchestration shim), so this
integration suite is the honest functional validation of its guard wiring —
mirrors ``test_cli_sweep_all_dedup.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.cli import EXIT_OK, EXIT_USER_ERROR, cmd_sweep, cmd_sweep_all, main
from brutex.data import catalog_reconcile
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_INTERVAL = "1minute"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential path EXPLICITLY (ADR-0016 §6).

    This suite's determinism previously rode the small-grid fallback only
    incidentally (its grids are 1-tuple); the explicit ``=1`` opt-out makes the
    sequential requirement survive any future grid widening.
    """
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
    """A short in-session minute window (stored UTC), valid OHLC."""
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


def _persist(tmp_path: Path, symbol: str) -> tuple[Path, Path]:
    """Write *symbol*'s bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=symbol,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep_all_kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    """The minimal real cmd_sweep_all kwargs over the tiny seeded lake."""
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
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


# ── cmd_sweep (single-tuple, report-only) ────────────────────────────────────


def test_sweep_out_of_scope_symbol_refused_before_any_lake_io(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No lake exists at all — the guard must fire BEFORE any lake/catalog read.
    rc = cmd_sweep(
        lake_root=tmp_path / "no-such-lake",
        catalog_path=tmp_path / "no-such-catalog.duckdb",
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="RELIANCE",
        interval=_INTERVAL,
        start="2024-01-02",
        end="2024-01-03",
        direction="BUY",
        k=1,
        conditions="0,1",
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=5,
        min_hits=0,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep REFUSED" in err
    assert "RELIANCE" in err
    assert "CLAUDE.md §2 Rule 3" in err


def test_sweep_override_proceeds_loudly_over_vix_lake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path, "INDIAVIX")
    with capture_logs() as logs:
        rc = cmd_sweep(
            lake_root=lake_root,
            catalog_path=catalog_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="INDIAVIX",
            interval=_INTERVAL,
            start="2024-01-02",
            end="2024-01-03",
            direction="BUY",
            k=1,
            conditions="0,1",
            stop_loss_paisa=None,
            take_profit_paisa=None,
            top=5,
            min_hits=0,
            sort_by="composite",
            validate_oos=False,
            oos_only=False,
            allow_out_of_scope=True,
        )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "WARNING" in out
    assert "INDIAVIX" in out
    events = [entry for entry in logs if entry.get("event") == "sweep.scope_override"]
    assert len(events) == 1
    assert events[0]["symbols"] == ["INDIAVIX"]


def test_sweep_argv_out_of_scope_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The argv path pins the main() dispatch threading of allow_out_of_scope.
    rc = main(
        [
            "sweep",
            "RELIANCE",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--lake-root",
            str(tmp_path / "no-such-lake"),
        ]
    )
    assert rc == EXIT_USER_ERROR
    assert "sweep REFUSED" in capsys.readouterr().err


# ── cmd_sweep_all (production grid launch) ──────────────────────────────────


def test_sweep_all_out_of_scope_refused_with_zero_lake_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The money regression: INDIAVIX over a VIX-SEEDED lake is REFUSED, unread."""
    lake_root, catalog_path = _persist(tmp_path, "INDIAVIX")

    def _reconcile_spy(**_kwargs: Any) -> None:
        raise AssertionError("reconcile_catalog_from_lake must NOT run on a scope refusal")

    # cmd_sweep_all imports the reconciler from its source module at call time,
    # so patching the source proves the refusal costs ZERO lake I/O.
    monkeypatch.setattr(catalog_reconcile, "reconcile_catalog_from_lake", _reconcile_spy)

    rc = cmd_sweep_all(symbols="INDIAVIX", **_sweep_all_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep REFUSED" in err
    assert "INDIAVIX" in err
    assert "REFERENCE_ONLY" in err
    # No results tree was created — the launch never got past the guard.
    assert not (tmp_path / "sweeps").exists()


def test_sweep_all_scope_refusal_beats_the_zero_bars_preflight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ordering pin: out-of-scope symbol + EMPTY lake → the SCOPE message."""
    rc = cmd_sweep_all(
        symbols="RELIANCE",
        **_sweep_all_kwargs(tmp_path / "empty-lake", tmp_path / "cat.duckdb", tmp_path),
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep REFUSED" in err
    assert "no bars" not in err
    assert "ZERO bars" not in err


def test_sweep_all_override_runs_and_is_logged_and_audit_stamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path, "INDIAVIX")
    with capture_logs() as logs:
        rc = cmd_sweep_all(
            symbols="INDIAVIX",
            allow_out_of_scope=True,
            **_sweep_all_kwargs(lake_root, catalog_path, tmp_path),
        )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "WARNING" in out
    assert "--allow-out-of-scope OVERRIDE" in out
    # structlog event names the let-through symbols (Golden Rule #8).
    events = [entry for entry in logs if entry.get("event") == "sweep.scope_override"]
    assert len(events) == 1
    assert events[0]["symbols"] == ["INDIAVIX"]
    assert events[0]["exchange"] == _EXCHANGE
    # The hash-chained launch audit carries the override stamp.
    audit_lines = [
        json.loads(line)
        for line in (tmp_path / "sweeps" / "audit" / "sweep-all.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    override_lines = [
        entry for entry in audit_lines if entry.get("action") == "SWEEP_SCOPE_OVERRIDE"
    ]
    assert len(override_lines) == 1
    assert override_lines[0]["payload"]["scope_override"] is True
    assert override_lines[0]["payload"]["symbols"] == ["INDIAVIX"]


def test_sweep_all_in_scope_default_run_is_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """DEFAULT-PRESERVATION: an in-scope NIFTY launch runs exactly as before."""
    lake_root, catalog_path = _persist(tmp_path, "NIFTY")
    with capture_logs() as logs:
        rc = cmd_sweep_all(symbols="NIFTY", **_sweep_all_kwargs(lake_root, catalog_path, tmp_path))
    assert rc == EXIT_OK
    assert "sweep-all done" in capsys.readouterr().out
    assert not any(entry.get("event") == "sweep.scope_override" for entry in logs)
    audit_text = (tmp_path / "sweeps" / "audit" / "sweep-all.jsonl").read_text(encoding="utf-8")
    assert "SWEEP_SCOPE_OVERRIDE" not in audit_text


# ── daemon + dashboard funnel ────────────────────────────────────────────────


def test_daemon_refuses_the_override_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """The research escape hatch is NEVER available on the unattended daemon."""
    rc = main(
        [
            "sweep-daemon",
            "--once",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--allow-out-of-scope",
        ]
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "sweep-daemon REFUSED" in err
    assert "--allow-out-of-scope" in err


def test_daemon_funnels_through_cmd_sweep_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Funnel lock: the daemon delegates the grid to cmd_sweep_all (guard inherited)."""
    calls: list[dict[str, Any]] = []

    def _fake_sweep_all(**kwargs: Any) -> int:
        calls.append(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_sweep_all", _fake_sweep_all)
    rc = cli.cmd_sweep_daemon(
        schedule="daily@02:30",
        once=True,
        max_cycles=None,
        tz_offset_minutes=None,
        lake_root=tmp_path,
        results_root=tmp_path / "sweeps",
        symbols="NIFTY",
    )
    assert rc == EXIT_OK
    assert len(calls) == 1
    assert calls[0]["symbols"] == "NIFTY"
    capsys.readouterr()  # drain the daemon's own progress prints


def test_dashboard_runner_funnels_through_cmd_sweep_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Funnel lock: run_sweep_from_request reaches cmd_sweep_all with the explicit
    allow_out_of_scope=False (no dashboard override exists by design)."""
    from brutex.dashboard.sweep_runner import resolve_sweep_request, run_sweep_from_request

    calls: list[dict[str, Any]] = []

    def _fake_sweep_all(**kwargs: Any) -> int:
        calls.append(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_sweep_all", _fake_sweep_all)
    request = resolve_sweep_request(symbols="NIFTY", today=date(2024, 1, 3))
    rc = run_sweep_from_request(request, lake_root=tmp_path, results_root=tmp_path / "sweeps")
    assert rc == EXIT_OK
    assert len(calls) == 1
    assert calls[0]["symbols"] == "NIFTY"
    assert calls[0]["allow_out_of_scope"] is False
