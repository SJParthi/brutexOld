"""End-to-end proof of the sweep-all results-depth run-end wiring (WP6).

DEC-SWEEP-RESULTS-DEPTH-001. ``cmd_sweep_all`` is ``# pragma: no cover`` (an
orchestration shim over the 100%-tested engine), so — exactly like
``test_cli_sweep_all_dedup.py``, the precedent suite — this is the HONEST
functional validation of the P3 integration adapter's production wiring: a
tiny REAL lake is persisted with the same writers ingest uses and
``cli.cmd_sweep_all`` is driven for real, proving

* the run-end pass FIRES post-cascade (output-order pinned) and writes the
  contract artifacts (``runs/<grid_identity>/top100.json`` + stamp, the
  per-tuple sidecar trio) with the WP3 providers wired;
* ``BRUTEX_RESULTS_DEPTH=0`` skips the pass loudly (no depth artifacts,
  sweep exit unchanged);
* a JUNK switch value is a clean PRE-sweep ``EXIT_USER_ERROR`` (nothing ran,
  no false abort alert);
* a pass failure is FENCED — loud stderr WARNING, sweep still exits 0, no
  halt alert (the failure is rebuildable, never masked, never fatal).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine import results_depth_pass as results_depth_pass_mod

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
def _sequential_no_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    monkeypatch.delenv("BRUTEX_RESULTS_DEPTH", raising=False)


def test_results_depth_fires_post_cascade_and_writes_the_contract_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default ON: the pass runs AFTER the sweep+cascade output and lands
    top100.json + stamp at the binding contract path plus the sidecar trio."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    out = capsys.readouterr().out
    assert "results-depth — " in out
    # POST-cascade law: the depth report prints strictly after the sweep
    # completion line (and after the cascade line whenever a cascade ran).
    assert out.index("results-depth — ") > out.index("sweep-all done")
    if "cascade — " in out:
        assert out.index("results-depth — ") > out.index("cascade — ")

    runs_dir = results_root / "runs"
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1  # ONE grid identity for this run
    artifact = run_dirs[0] / "top100.json"
    stamp = run_dirs[0] / "top100.stamp.json"
    assert artifact.exists() and stamp.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    # The WP3 providers were wired by the adapter — honesty header proves it.
    assert payload["source"]["reasons_wired"] is True
    assert payload["source"]["margin_roi_wired"] is True
    assert payload["run_id"] == run_dirs[0].name
    # The per-tuple sidecar trio landed in the glob-collision-proof subtree.
    depth_dir = results_root / "results-depth"
    assert list(depth_dir.glob("*/*/*.trades.parquet"))
    assert list(depth_dir.glob("*/*/*.aggs.parquet"))
    assert list(depth_dir.glob("*/*/*.stamp.json"))


def test_kill_switch_skips_the_pass_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BRUTEX_RESULTS_DEPTH=0: sweep completes, pass skipped, ZERO depth artifacts."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "0")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    out = capsys.readouterr().out
    assert "sweep-all done" in out
    assert "results-depth: disabled via BRUTEX_RESULTS_DEPTH" in out
    assert "results-depth — " not in out  # the pass never ran
    assert not (results_root / "runs").exists()
    assert not (results_root / "results-depth").exists()


def test_junk_kill_switch_is_a_clean_pre_sweep_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A junk BRUTEX_RESULTS_DEPTH refuses BEFORE any compute — fail-loud,
    never a surprise after a multi-day sweep; never a false abort alert."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_RESULTS_DEPTH", "maybe")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "BRUTEX_RESULTS_DEPTH" in captured.err
    assert "sweep-all done" not in captured.out  # the sweep never launched …
    assert not (results_root / "audit").exists()  # … and wrote nothing
    assert fired == []  # a config error is not an abort — never a false alert


def test_pass_failure_is_fenced_and_never_fails_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A defect inside the pass prints LOUD + logs and leaves the sweep exit
    path byte-unchanged (exit 0, completion lines intact, no halt alert)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("depth pass wedged")

    # The adapter imports the pass at call time from its source module.
    monkeypatch.setattr(results_depth_pass_mod, "run_results_depth_pass", _boom)

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    captured = capsys.readouterr()
    assert "sweep-all done" in captured.out
    assert "results-depth pass FAILED (RuntimeError: depth pass wedged)" in captured.err
    assert "sweep itself SUCCEEDED" in captured.err
    assert fired == []  # fenced — never escalated to a sweep abort alert
