"""Integration — the results-depth missing-store classification on a RESUME.

DEC-DEPTH-MISSING-STORE-LOUD-001 (MIGRATION.md #1636), review round 1 M2. The
unexplained-missing WARN originally allowed ``tuples_skipped_no_bars +
tuples_failed + tuples_quarantined_prior`` — RUN-N counters. On a RESUMED run
the prefix tuples fold into ``skipped_resume`` and are never re-classified, so
a no-bars tuple in the resumed prefix made ``tuples_missing_store``
exceed the allowance → a FALSE "partial store deletion or a foreign grid
identity" WARN on a perfectly healthy resume (and, inversely, a run-N failed
count whose store survived from an earlier run could inflate the allowance and
mask a real deletion). The fix classifies each missing store DETERMINISTICALLY
in the pass — an empty bar slice ⇒ expected-missing, resume or not — and the
CLI threads only the run-scoped ``tuples_failed + tuples_quarantined_prior``.

Both tests drive the REAL ``cmd_sweep_all`` twice over one results_root (the
second run is a full checkpoint resume: every tuple folds into
``skipped_resume``):

* a no-bars tuple in the resumed prefix (BANKNIFTY with ZERO lake bars,
  ``--allow-missing-slices``) fires NO missing-store warning on the resume
  [fails-before: the run-N formula WARNed "partial store deletion"];
* a genuine store DELETION between run 1 and the resume STILL warns loud
  (the classification never masks a real deletion).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.cli import EXIT_OK, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_MISSING_MARKER = "finalist store(s) missing"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over the tiny grid."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
    """Trending NIFTY 1-minute bars over Jan 2-5 2024 (one month partition)."""
    out: list[CandleBar] = []
    for day in (2, 3, 4, 5):
        price = 100.0
        for m in range(40):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = datetime(2024, 1, day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return out


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Write a NIFTY-only 1-minute lake (BANKNIFTY deliberately has NO bars)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_bars(),
        )
    return lake_root, catalog_path


def _kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path, *, symbols: str) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": symbols,
        "intervals": "1minute",
        "start": "2024-01-02",
        "end": "2024-01-06",
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
        "allow_missing_slices": True,
    }


def test_resume_with_no_bars_prefix_tuple_fires_no_false_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE M2 resume pin: BANKNIFTY has zero bars, so its tuple is skipped
    no-bars in run 1 (checkpoint recorded) and folds into ``skipped_resume``
    on run 2 — where the run-scoped allowance is 0. The deterministic no-bars
    classification keeps BOTH runs free of the missing-store warning.

    Fails-before (hunk reverted to the run-N formula): run 2's expected
    allowance read 0 while BANKNIFTY's store was missing → a false
    "partial store deletion or a foreign grid identity" WARNING on stderr.
    """
    lake_root, catalog_path = _seed(tmp_path)
    kwargs = _kwargs(lake_root, catalog_path, tmp_path, symbols="NIFTY,BANKNIFTY")

    rc1 = cmd_sweep_all(**kwargs)
    err1 = capsys.readouterr().err
    assert rc1 == EXIT_OK
    assert _MISSING_MARKER not in err1  # run 1: no-bars classified, no warn

    rc2 = cmd_sweep_all(**kwargs)  # full checkpoint resume — the false-WARN shape
    err2 = capsys.readouterr().err
    assert rc2 == EXIT_OK
    assert _MISSING_MARKER not in err2


def test_genuine_deletion_on_resume_still_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The teeth twin: delete the swept BARS-PRESENT NIFTY store between run 1
    and the resume — the resume's depth pass WARNs loud on stderr (naming the
    bars-present class), while the sweep's exit stays EXIT_OK (the fence)."""
    lake_root, catalog_path = _seed(tmp_path)
    kwargs = _kwargs(lake_root, catalog_path, tmp_path, symbols="NIFTY")

    rc1 = cmd_sweep_all(**kwargs)
    capsys.readouterr()
    assert rc1 == EXIT_OK

    results_root = tmp_path / "sweeps"
    stores = [
        p
        for p in results_root.rglob("*.parquet")
        if "audit" not in p.parts and "checkpoint" not in p.parts
    ]
    assert len(stores) == 1, f"expected exactly one finalist store, found {stores}"
    stores[0].unlink()

    rc2 = cmd_sweep_all(**kwargs)  # the resume skips the tuple; the store stays gone
    err2 = capsys.readouterr().err
    assert rc2 == EXIT_OK  # the depth pass NEVER fails the sweep (fence pin)
    assert _MISSING_MARKER in err2
    assert "bars-present" in err2
