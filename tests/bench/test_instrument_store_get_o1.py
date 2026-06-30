"""O(1) benchmark for ``InstrumentStore.get`` (DEC-INSTRUMENTSTORE-O1-001).

The guarantee proven here: a ``store.get(symbol)`` point read (the dominant
``as_of=None`` hot path — every dashboard ``store.get(symbol)``) has latency
**independent of how many underlyings the master holds**. After the in-memory
``symbol_name`` index is built once, a hit is a single dict lookup + a
first-element read, so serving a master of 4000 underlyings is as fast as one
of 40.

This pins the fix for the H9 defect: before DEC-INSTRUMENTSTORE-O1-001, ``get``
ran a DuckDB ``SELECT * FROM instruments WHERE symbol_name = ? ORDER BY
snapshot_date DESC LIMIT 1`` per call. No index covers ``symbol_name``, so the
planner used a full ``SEQ_SCAN`` — **O(rows) per call** — while the docstring
falsely claimed "O(1) lookup". A regression back to a per-call scan would make
the latency scale with the master size and blow past the ceiling here.

Strategy mirrors ``tests/bench/test_universe_cache_o1.py``: prime the index,
then time many hit calls at a SMALL vs a LARGE master and assert the latency
ratio stays under ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner, tightened
to the strict 1.1× M4-Pro tier via ``tests/bench/_o1_tiers.py``.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the get() O(1) bench reads a real DuckDB store")

from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

from tests.bench._o1_tiers import ratio_ceiling


def _equity(security_id: int) -> Instrument:
    """One NSE cash-equity row keyed by a unique ``symbol_name``."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=f"S{security_id}",
        display_name=f"Stock {security_id}",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=f"S{security_id}",
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@contextmanager
def _primed(tmp_path: Path, n: int, tag: str) -> Generator[InstrumentStore, None, None]:
    """An *n*-underlying store whose ``get`` index is PRIMED (one clean build).

    The store is opened with ``with`` (it is a context manager — ``__exit__``
    closes it) so there is NO hand-rolled ``finally`` (CodeQL
    ``py/should-use-with``).
    """
    with InstrumentStore.open(tmp_path / f"g_{tag}.duckdb") as store:
        store.upsert_many(
            [_equity(i) for i in range(1, n + 1)],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        store.get("S1")  # build the index once
        yield store


class TestGetIsO1:
    """``store.get(symbol)`` is O(1) in the master size after the index is built."""

    def test_get_at_40(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 40, "small") as store:
            result = benchmark(lambda: store.get("S20"))
            assert result is not None and result.symbol_name == "S20"

    def test_get_at_4000(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 4000, "big") as store:
            result = benchmark(lambda: store.get("S2000"))
            assert result is not None and result.symbol_name == "S2000"

    def test_get_ratio_below_ceiling(self, tmp_path: Path) -> None:
        """Empirical hit-latency ratio at 4000 / 40 underlyings must be < the ceiling.

        The ceiling is ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner,
        tightened to the strict 1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001). A
        regression to the prior per-call O(rows) DuckDB ``SEQ_SCAN`` would scale
        with the master size and blow past this bound — the explicit O(1)
        hot-path gate that was MISSING before DEC-INSTRUMENTSTORE-O1-001.
        """

        def _time(store: InstrumentStore, symbol: str, iters: int) -> float:
            start = time.perf_counter()
            for _ in range(iters):
                store.get(symbol)
            return time.perf_counter() - start

        with (
            _primed(tmp_path, 40, "ratio_small") as store_small,
            _primed(tmp_path, 4000, "ratio_big") as store_big,
        ):
            _time(store_small, "S20", 2_000)  # warm up
            _time(store_big, "S2000", 2_000)
            iterations = 20_000
            small = _time(store_small, "S20", iterations)
            big = _time(store_big, "S2000", iterations)
            ratio = big / small if small > 0 else 1.0
            assert ratio < ratio_ceiling(5.0), (
                f"get() latency ratio {ratio:.2f} scaled with master size "
                "(regressed to an O(n) scan?)"
            )
