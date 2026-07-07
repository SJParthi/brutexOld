"""The nightly counts reader against a REAL ``cmd_sweep_all`` run
(DEC-SWEEP-NIGHTLY-AUTOMATION-001, review round 1 finding 2).

The round-1 defect: ``read_sweep_tail_counts`` scanned the ingest
``audit/sweep-all.jsonl`` for an action string that is never written there —
the completion tally is appended by ``SweepGuards.audit`` (``sup_audit``) to
``audit/sweep-all-supervisor.jsonl`` with action ``"SUPERVISOR_COMPLETE"``
(cli.py / sweep_supervisor.py). Every prior test injected a fake ``run_sweep``
shaped after the buggy reader, so no test exercised the true integration.

This file drives the REAL ``cli.cmd_sweep_all`` over a tiny persisted lake
(the ``test_cli_sweep_all_dedup`` fixture pattern) and asserts the nightly
reader finds the tally at the production path/action — and that the ingest
audit genuinely never carries it. The fixture can no longer drift to match a
buggy reader, because there is no fixture: production writes the bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.nightly.sweep_binding import (
    read_sweep_tail_counts,
    sweep_supervisor_audit_path,
)

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


def test_tail_counts_read_from_a_real_cmd_sweep_all_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_sweep_tail_counts finds the REAL supervisor tally; the T5 identity
    holds on it; the ingest ``sweep-all.jsonl`` never carries the completion."""
    from brutex import cli

    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    rc = cli.cmd_sweep_all(
        lake_root=lake_root,
        catalog_path=catalog_path,
        results_root=results_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbols=_SYMBOL,
        intervals=_INTERVAL,
        start="2024-01-02",
        end="2024-01-03",
        directions="BUY",
        k="2",
        conditions="0,1,2,3",  # C(4,2) = 6 combos → a real multi-combo sweep
        sl="none",
        tp="none",
        top=10,
        mode="A",
        max_tuples=None,
        max_combos=None,
        data_snapshot_hash=None,
        options_sim=False,
    )
    assert rc == 0

    # The nightly reader finds the tally at the PRODUCTION path + action.
    sup_path = sweep_supervisor_audit_path(results_root)
    assert sup_path == results_root / "audit" / "sweep-all-supervisor.jsonl"
    assert sup_path.exists()
    counts = read_sweep_tail_counts(sup_path)
    assert counts is not None
    # The T5 identity the nightly checks, on REAL supervisor numbers.
    assert counts["tuples_total"] == (
        counts["tuples_run"]
        + counts["skipped_cache"]
        + counts["skipped_resume"]
        + counts["tuples_skipped_no_bars"]
        + counts["tuples_failed"]
    )
    assert counts["tuples_total"] >= 1
    assert counts["tuples_failed"] == 0  # exit 0 ⟺ failed == 0 (the exit law)

    # The round-1 wrong target: the ingest audit NEVER carries the completion.
    assert read_sweep_tail_counts(results_root / "audit" / "sweep-all.jsonl") is None
