"""``cmd_sweep_verify`` fill-mode wire — DEC-FILL-WORST-CASE-001 fix round 3.

THE KILLING TESTS (finding 4, MIGRATION.md #1798): drive the REAL
``sweep-verify`` CLI (never ``verify_identity`` directly) over a WORST_CASE
tree, so the surviving-mutant class the refuters kept confirming — the
recorded/flag mode never reaching the verify planning — fails a named test:

* the RECORDED mode set reaches the plan (a WORST_CASE tree with its audit
  verifies CLEAN with its store CHECKED — a severed/hardcoded-PRECISE mode
  set reads the mode-folded store address as ``grid_mismatch`` corrupt with
  ``checked_stores=0``);
* with the audit ABSENT, the operator's ``--fill-mode`` flag is HONORED
  (finding 3 — the flag was registered but silently dropped, the verify
  fabricated PRECISE);
* with the audit absent AND no flag, BOTH modes are planned (the union
  default — a healthy WORST_CASE tree with a lost audit must verify clean,
  and so must a legacy PRECISE tree).

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
    _indicator_version,
    _resolve_code_commit_sha,
    cmd_sweep_all,
    main,
)
from brutex.data.catalog import Catalog
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


def _sweep_worst_case_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One REAL ``cmd_sweep_all --fill-mode worst_case`` launch into a fresh root."""
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
        "fill_mode": "worst_case",
    }
    assert cmd_sweep_all(**kwargs) == EXIT_OK
    stores = sorted((results_root / _SYMBOL).rglob("*.parquet"))
    assert len(stores) == 1  # the WORST_CASE-digest store exists
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


def test_recorded_worst_case_mode_reaches_the_verify_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE WIRE MUTANT KILLER: hardcoding the resolved mode set to PRECISE in
    ``cmd_sweep_verify`` (the round-1/round-2 surviving-mutant class — the
    recorded mode never reaching the planning) makes the WORST_CASE store's
    mode-folded address read ``grid_mismatch`` corrupt with 0 stores checked,
    failing exactly this test."""
    lake_root, catalog_path, results_root = _sweep_worst_case_tree(tmp_path)
    capsys.readouterr()
    rc = main(_verify_args(lake_root, catalog_path, results_root))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    identity = _identity_section(out)
    assert "stores checked: 1 · verdict: CLEAN" in identity
    assert "grid_mismatch" not in out
    assert "unattributed_store" not in out


def test_fill_mode_flag_honored_when_the_audit_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 3: with nothing recorded, the registered ``--fill-mode`` flag
    reaches the verify grid (pre-fix it was silently dropped and PRECISE was
    fabricated — the WORST_CASE tree read grid_mismatch corrupt)."""
    lake_root, catalog_path, results_root = _sweep_worst_case_tree(tmp_path)
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
            "--fill-mode",
            "worst_case",
        )
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    identity = _identity_section(out)
    assert "stores checked: 1 · verdict: CLEAN" in identity
    assert "grid_mismatch" not in out


def test_union_default_covers_a_lost_audit_worst_case_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 3 (the no-flag half): with nothing recorded AND no flag, BOTH
    modes are planned — a healthy WORST_CASE tree with a lost audit verifies
    clean (pre-fix: fabricated PRECISE → grid_mismatch corrupt)."""
    lake_root, catalog_path, results_root = _sweep_worst_case_tree(tmp_path)
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
    assert "planning under BOTH modes" in out
    identity = _identity_section(out)
    assert "stores checked: 1 · verdict: CLEAN" in identity
    assert "grid_mismatch" not in out
