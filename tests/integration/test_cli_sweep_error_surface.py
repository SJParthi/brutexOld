"""End-to-end proof of the sweep-all error-surface fixes (FIX PACK 3).

``cmd_sweep_all`` is ``# pragma: no cover`` (an orchestration shim over the
100%-tested engine), so — exactly like ``test_cli_results_depth_wiring.py``,
the precedent suite — this is the HONEST functional validation that:

* RES-7 — a junk ``BRUTEX_SWEEP_MEM_BUDGET_BYTES`` / ``BRUTEX_SWEEP_BOUNDED_RANK``
  is resolved EAGERLY pre-sweep to a clean ``EXIT_USER_ERROR`` (nothing ran, no
  false ``_emit_pull_halt_alert`` abort alert), while a VALID budget resolves to
  the identical behaviour (the sweep completes exit 0);
* RES-5 — a lock-conflict ``CatalogError`` at ``Catalog.open`` (a second
  concurrent sweep-all over the same output dir) is converted to a clean
  "another sweep is running" refusal (``EXIT_USER_ERROR``), while any OTHER
  ``CatalogError`` (genuine catalog corruption) still surfaces LOUDLY (re-raised,
  never masked as contention).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog, CatalogError
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


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


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path, **overrides: Any) -> int:
    """Drive ``cli.cmd_sweep_all`` over the tiny persisted lake."""
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "directions": "BUY",
        "k": "2",
        "conditions": "0,1,2,3",  # C(4,2) = 6 combos → a real multi-combo sweep
        "sl": "none",
        "tp": "none",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    kwargs.update(overrides)
    return cli.cmd_sweep_all(**kwargs)


@pytest.fixture(autouse=True)
def _sequential_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    monkeypatch.delenv("BRUTEX_RESULTS_DEPTH", raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_MEM_BUDGET_BYTES", raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_BOUNDED_RANK", raising=False)


# ─────────────────────────── RES-7 ───────────────────────────


@pytest.mark.parametrize("junk", ["lots", "-5"])
def test_junk_mem_budget_is_a_clean_pre_sweep_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], junk: str
) -> None:
    """RES-7: a junk ``BRUTEX_SWEEP_MEM_BUDGET_BYTES`` refuses BEFORE any
    compute — clean ``EXIT_USER_ERROR``, nothing ran, and NO false abort alert
    (previously it escaped as an uncaught ValueError mid-sweep and fired a
    CRITICAL "sweep halted" Telegram alert)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_SWEEP_MEM_BUDGET_BYTES", junk)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "BRUTEX_SWEEP_MEM_BUDGET_BYTES" in captured.err
    assert "sweep-all done" not in captured.out  # the sweep never launched …
    assert not (results_root / "audit").exists()  # … and wrote nothing
    assert fired == []  # a config typo is not an abort — never a false alert


def test_junk_bounded_rank_mode_is_a_clean_pre_sweep_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RES-7 (sibling knob): a junk ``BRUTEX_SWEEP_BOUNDED_RANK`` word is also a
    clean pre-sweep ``EXIT_USER_ERROR`` with no abort alert."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_SWEEP_BOUNDED_RANK", "banana")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "BRUTEX_SWEEP_BOUNDED_RANK" in captured.err
    assert "sweep-all done" not in captured.out
    assert fired == []


def test_valid_mem_budget_resolves_and_the_sweep_completes_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RES-7 no-behavior-change proof: a VALID ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``
    resolves to the identical budget the lazy runner would compute — the eager
    resolve is a validation only, so the sweep still completes (exit 0), and the
    eager resolution equals the runner's per-tuple resolution byte-for-byte."""
    from brutex.engine.sweep_mem_budget import resolve_mem_budget_bytes

    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    good = str(16 * 1024**3)  # a valid positive-int byte budget
    monkeypatch.setenv("BRUTEX_SWEEP_MEM_BUDGET_BYTES", good)
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")  # keep this test focused on the sweep

    # The eager pre-sweep resolution the CLI performs == the value the runner
    # re-resolves per tuple (same env → same budget → no behavior change).
    assert resolve_mem_budget_bytes() == 16 * 1024**3

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    out = capsys.readouterr().out
    assert "sweep-all done" in out  # a valid budget never blocks the sweep


# ─────────────────────────── RES-5 ───────────────────────────


def test_lock_conflict_is_a_clean_another_sweep_running_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RES-5: a second concurrent sweep-all over the same output dir hits the
    DuckDB catalog lock; ``Catalog.open`` raises a ``CatalogError`` carrying
    DuckDB's "Conflicting lock is held … (PID)" text. That is converted to a
    clean ``EXIT_USER_ERROR`` "another sweep is running" refusal — never a raw
    traceback, never a false abort alert."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    # The exact wrapped message shape Catalog.open produces on a cross-process
    # lock conflict (captured live from duckdb via a two-process repro).
    lock_msg = (
        f"failed to open catalog at {catalog_path}: IO Error: Could not set "
        f'lock on file "{catalog_path}": Conflicting lock is held in '
        f"/usr/bin/python3.12 (PID 4242). See also "
        f"https://duckdb.org/docs/stable/connect/concurrency"
    )

    def _locked_open(*_a: Any, **_k: Any) -> Any:
        raise CatalogError(lock_msg)

    monkeypatch.setattr(Catalog, "open", classmethod(lambda cls, *a, **k: _locked_open()))

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "another sweep is already running" in captured.err
    assert "sweep-all done" not in captured.out  # nothing swept
    assert fired == []  # contention is a user error, not a false abort alert


def test_non_lock_catalog_error_still_surfaces_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RES-5 discrimination: a NON-lock ``CatalogError`` (genuine catalog
    corruption / a real open failure) is NOT masked as "another sweep running"
    — it re-raises LOUDLY so a real fault is never silently swallowed."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    corrupt_msg = f"failed to open catalog at {catalog_path}: IO Error: database is corrupt"

    def _corrupt_open(*_a: Any, **_k: Any) -> Any:
        raise CatalogError(corrupt_msg)

    monkeypatch.setattr(Catalog, "open", classmethod(lambda cls, *a, **k: _corrupt_open()))

    with pytest.raises(CatalogError, match="database is corrupt"):
        _sweep_all(lake_root, catalog_path, results_root)
