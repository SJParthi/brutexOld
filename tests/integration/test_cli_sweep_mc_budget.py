"""End-to-end proof of the ``BRUTEX_SWEEP_MC_PERMUTATIONS`` sweep-all wiring.

DEC-REHEARSAL-MC-BUDGET-001 (MIGRATION.md #1795). ``cmd_sweep_all`` is
``# pragma: no cover`` orchestration glue, so — exactly like
``test_cli_results_depth_wiring.py``, the precedent suite — this drives the
REAL ``cli.cmd_sweep_all`` over a tiny production-writer lake and proves the
three wiring laws of the Stage-4 Monte-Carlo permutation-budget knob:

* **default identity** — with the env UNSET the manifest-stored cascade
  config keeps the production ``mc_n_permutations = 1000`` byte-identical
  (no override is threaded);
* **override threading** — a set value travels the EXISTING
  ``cascade_config_for_sweep(overrides=…)`` seam into the MANIFEST-stored
  config (the manual ``brutex cascade`` replay reads only the frozen value,
  so the replay stays exact under the override);
* **fail-loud** — a junk value is a clean PRE-sweep ``EXIT_USER_ERROR``
  naming the env var (nothing ran, nothing written, no false abort alert) —
  the eager-resolution law of the sibling diffcheck/stall knobs.

The resolver's own value matrix (blank/floor/junk/negative) is unit-tested in
``tests/unit/test_engine_cascade_wire.py::TestResolveMcPermutations``.
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

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"

#: The env knob under test (string-pinned so a rename breaks THIS suite too).
_MC_ENV = "BRUTEX_SWEEP_MC_PERMUTATIONS"


def _trading_bars() -> list[CandleBar]:
    """The proven trade-PRODUCING fixture (the results-depth wiring suite's
    12-day upward-drift bars, verbatim shape) — finalists survive the hard
    gates, so the Stage-4 cascade genuinely runs and writes its manifest."""
    out: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Persist the trade-producing bars through the PRODUCTION writer."""
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
            bars=_trading_bars(),
        )
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path) -> int:
    """Drive ``cli.cmd_sweep_all`` with args matching the trading fixture."""
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2026-01-05",
        "end": "2026-01-17",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2,3,4",
        "sl": "80",
        "tp": "50",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    return cli.cmd_sweep_all(**kwargs)


def _manifest_mc(results_root: Path) -> int:
    """The manifest-stored ``mc_n_permutations`` (the replay's frozen value)."""
    manifest = json.loads((results_root / "cascade" / "manifest.json").read_text())
    value = manifest["cascade_config"]["mc_n_permutations"]
    assert isinstance(value, int)
    return value


@pytest.fixture(autouse=True)
def _sequential_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    monkeypatch.delenv(_MC_ENV, raising=False)


def test_env_unset_keeps_the_production_default_byte_identical(tmp_path: Path) -> None:
    """No env ⇒ NO override threaded ⇒ the manifest carries the production
    ``CascadeConfig.mc_n_permutations`` default (1000) — the identity arm."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    assert _sweep_all(lake_root, catalog_path, results_root) == 0
    assert _manifest_mc(results_root) == 1000


def test_override_reaches_the_manifest_stored_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env value rides the ``overrides=`` seam into the manifest, so the
    manual replay reads the SAME budget the automatic cascade ran with."""
    monkeypatch.setenv(_MC_ENV, "25")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    assert _sweep_all(lake_root, catalog_path, results_root) == 0
    assert _manifest_mc(results_root) == 25


def test_junk_value_is_a_clean_pre_sweep_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd knob refuses BEFORE any compute — the eager-resolution law
    (never a surprise after a multi-day sweep; never a false abort alert)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv(_MC_ENV, "many")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "sweep-all config error" in captured.err
    assert _MC_ENV in captured.err
    assert "sweep-all done" not in captured.out  # the sweep never launched …
    assert not (results_root / "audit").exists()  # … and wrote nothing
    assert fired == []  # a config error is not an abort — never a false alert


def test_nonpositive_value_refuses_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``0`` would silently disable the MC filter — refused, never honoured."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv(_MC_ENV, "0")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    assert "must be an integer >= 1" in capsys.readouterr().err
    assert not (results_root / "audit").exists()
