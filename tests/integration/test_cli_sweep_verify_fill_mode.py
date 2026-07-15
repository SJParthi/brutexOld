"""``cmd_sweep_verify`` under the ONE fill law — DEC-FILL-WORST-CASE-ONLY-001.

THE REFUSAL-LAW KILLING TESTS: drive the REAL ``sweep-verify`` CLI (never
``verify_identity`` directly) so the legacy-artifact refusal is proven on the
production wire:

* a REAL ``cmd_sweep_all`` tree (the writers record the unconditional
  ``fill_mode: worst_case`` audit token) verifies CLEAN with its store
  CHECKED — the one law needs no mode planning;
* a PRECISE-ERA audit (a valid hash chain whose ``SWEEP_START`` params lack
  the ``fill_mode`` key — the retired elide law) REFUSES loudly naming
  DEC-FILL-WORST-CASE-ONLY-001 with a clean user-error exit, never a silent
  replay and never a traceback;
* with the audit ABSENT, the explicit context flags verify the tree clean
  under the one law (no ``--fill-mode`` flag exists any more — the killed
  argparse token is pinned).

``cmd_sweep_all`` / ``cmd_sweep_verify`` are ``# pragma: no cover``
orchestration shims, so this suite is their honest functional validation —
mirrors ``test_cli_snapshot_hash.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.cli import (
    EXIT_OK,
    EXIT_USER_ERROR,
    _indicator_version,
    _resolve_code_commit_sha,
    cmd_sweep_all,
    main,
)
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.orchestrator.audit import AuditWriter

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"
_START = "2024-01-02"
_END = "2024-01-03"
#: An explicit launch snapshot override, so the audit-less arms can supply
#: the SAME context via flags (the tree's grid was minted with this hash).
_SNAPSHOT = "ab" * 32


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential path explicitly (ADR-0016 §6; the sibling suites' law)."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
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


def _sweep_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One REAL ``cmd_sweep_all`` launch into a fresh root (the one fill law)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": _START,
        "end": _END,
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": _SNAPSHOT,
        "options_sim": False,
    }
    assert cmd_sweep_all(**kwargs) == EXIT_OK
    stores = sorted((results_root / _SYMBOL).rglob("*.parquet"))
    assert len(stores) == 1  # the worst-case-digest store exists
    return lake_root, catalog_path, results_root


def _verify_args(lake_root: Path, catalog_path: Path, results_root: Path, *extra: str) -> list[str]:
    return [
        "sweep-verify",
        "--symbols",
        _SYMBOL,
        "--intervals",
        _INTERVAL,
        "--start",
        _START,
        "--end",
        _END,
        "--directions",
        "BUY",
        "--k",
        "1",
        "--conditions",
        "0,1",
        "--top",
        "5",
        "--lake-root",
        str(lake_root),
        "--catalog",
        str(catalog_path),
        "--results-root",
        str(results_root),
        *extra,
    ]


def _identity_section(out: str) -> str:
    start = out.index("sweep-verify [identity]")
    return out[start:].split("sweep-verify [", 2)[1]


def test_worst_case_tree_verifies_clean_under_the_one_law(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one-law wire: the recorded worst_case generation verifies CLEAN
    with its store CHECKED — no mode planning, no grid_mismatch, no
    unattributed store."""
    lake_root, catalog_path, results_root = _sweep_tree(tmp_path)
    capsys.readouterr()
    rc = main(_verify_args(lake_root, catalog_path, results_root))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    identity = _identity_section(out)
    assert "stores checked: 1 · verdict: CLEAN" in identity
    assert "grid_mismatch" not in out
    assert "unattributed_store" not in out


def test_precise_era_audit_refuses_loudly_naming_the_dec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FAILS-BEFORE refusal pin: a precise-era audit generation (params
    WITHOUT the fill_mode key — the retired elide law) refuses at
    fact-derivation time with a clean user-error exit naming
    DEC-FILL-WORST-CASE-ONLY-001 and the re-run remediation — never a silent
    replay of precise-priced stores."""
    lake_root, catalog_path, results_root = _sweep_tree(tmp_path)
    audit_path = results_root / "audit" / "sweep-all.jsonl"
    audit_path.unlink()
    with AuditWriter.open(audit_path) as audit:
        audit.append(
            action="SWEEP_START",
            subject="sweep:NIFTY:1minute:bull:k=1",
            payload={
                "instrument": _SYMBOL,
                "data_snapshot_hash": _SNAPSHOT,
                "indicator_version": _indicator_version(),
                "code_commit_sha": _resolve_code_commit_sha(),
                "params": {"sl_paisa": None, "tp_paisa": None},  # precise-era: NO fill_mode
            },
        )
    capsys.readouterr()
    rc = main(_verify_args(lake_root, catalog_path, results_root))
    captured = capsys.readouterr()
    assert rc == EXIT_USER_ERROR
    assert "precise-era sweep tree refused (DEC-FILL-WORST-CASE-ONLY-001)" in captured.err
    assert "Re-run the sweep" in captured.err


def test_absent_audit_with_flags_verifies_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the audit ABSENT, the explicit context flags verify the tree
    clean under the one law — the retired union/mode planning is gone and
    nothing fabricates a mode."""
    lake_root, catalog_path, results_root = _sweep_tree(tmp_path)
    (results_root / "audit" / "sweep-all.jsonl").unlink()
    capsys.readouterr()
    rc = main(
        _verify_args(
            lake_root,
            catalog_path,
            results_root,
            "--data-snapshot-hash",
            _SNAPSHOT,
            "--indicator-version",
            _indicator_version(),
            "--code-commit-sha",
            _resolve_code_commit_sha(),
        )
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "planning under BOTH modes" not in out  # the union planner is dead
    identity = _identity_section(out)
    assert "stores checked: 1 · verdict: CLEAN" in identity
    assert "grid_mismatch" not in out


def test_fill_mode_flag_is_deleted_from_the_cli(tmp_path: Path) -> None:
    """Killed-token pin: ``--fill-mode`` no longer parses on sweep-verify OR
    sweep-all (argparse exits 2), and ``cmd_sweep_all`` takes no fill_mode
    kwarg."""
    import inspect

    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    with pytest.raises(SystemExit) as excinfo:
        main(
            _verify_args(lake_root, catalog_path, tmp_path / "sweeps", "--fill-mode", "worst_case")
        )
    assert excinfo.value.code == 2
    assert "fill_mode" not in inspect.signature(cmd_sweep_all).parameters
