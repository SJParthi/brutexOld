"""Sustained-load stress: F&O candle discovery + decode over a large lake.

Closes the ``tests/stress/`` slice for DEC-FNO-CANDLE-LAKE-001: the ``/db`` F&O candle
browser walks ``bars/<exch>/FNO/<contract>/<interval>/`` and decodes every contract
symbol once. Under a lake holding many hundreds of contracts the walk + decode must stay
correct + bounded — no exception, no lost / duplicated dataset, every option decoded to
its strike + side and every future to ``strike=None`` (Golden Rule #1: a malformed dir
is skipped, never crashes the browse).

Budget
------
* CI nightly: ``_N_CONTRACTS`` contracts (default, fast on GHA).
* Local soak: ``BRUTEX_STRESS_FNO_CONTRACTS=20000`` via ``workflow_dispatch``.

The CI default is deliberately modest (DEC-P0-005 shared-GHA flake risk for > 60s
tests); the manual soak provides the large-lake target.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.db_view import list_fno_candle_datasets
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

pytestmark = pytest.mark.stress

#: Contracts to write (each as one 1-minute partition) — env-tunable for the soak.
_N_CONTRACTS = int(os.environ.get("BRUTEX_STRESS_FNO_CONTRACTS", "600"))


def test_fno_discovery_decodes_every_contract_under_load(tmp_path: Path) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=12000,
    )
    expected_options = 0
    expected_futures = 0
    for i in range(_N_CONTRACTS):
        if i % 5 == 0:
            # A real future symbol EXCH-UNDERLYING-DDMmmYY-FUT, unique via the
            # per-i underlying so each lands in its own partition.
            symbol = f"NSE-NIFTYX{i}-30Jan25-FUT"
            expected_futures += 1
        else:
            symbol = f"NSE-NIFTYX{i}-02Jan25-{20000 + i}-CE"
            expected_options += 1
        sink.append(exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=[bar])

    rows = list_fno_candle_datasets(tmp_path)
    # No lost / duplicated dataset.
    assert len(rows) == _N_CONTRACTS
    assert len({r["symbol"] for r in rows}) == _N_CONTRACTS
    # Every contract decoded to its kind.
    options = [r for r in rows if r["instrument_type"] == "CE"]
    futures = [r for r in rows if r["instrument_type"] == "FUT"]
    assert len(options) == expected_options
    assert len(futures) == expected_futures
    assert all(r["strike"] is not None and r["option_type"] == "CE" for r in options)
    assert all(r["strike"] is None and r["option_type"] is None for r in futures)
