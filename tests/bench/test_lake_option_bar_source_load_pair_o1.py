"""O(1)/leg benchmark for the REAL LakeOptionBarSource.load_pair (DEC-O1-BENCH-GAP-CLOSURE-001).

Gates the load-bearing claim of ``brutex/options/lake_source.py``
(DEC-OPTIONS-LAKE-READER-001 / DEC-LAKE-READ-O1-AUDIT-001 #1025): after the first
touch of a contract reads its 1-minute partition into a cached
``{utc_instant → OptionBar}`` map, every subsequent :meth:`LakeOptionBarSource.load_pair`
is an **O(1) dict lookup of the two instants** — independent of how many bars the
contract holds. A sweep that materialises many legs on the same contract pays the
lake read ONCE.

Unlike ``tests/bench/test_options_executor_o1.py`` (which injects a FAKE
``load_pair`` stub), this bench exercises the REAL production reader against a REAL
synthetic F&O candle lake written on disk in the exact layout
``bars/<exch>/FNO/<groww_symbol>/1minute/<yyyy>/<mm>.parquet`` — the layout
:mod:`brutex.adapters.groww.option_ingest` writes and
:func:`~brutex.data.fno_symbol.parse_fno_symbol` decodes.

The gate builds two identical contracts differing ONLY in bar count (a short vs a
deep contract), warms each (one real partition read), then times repeat
``load_pair`` calls and asserts the per-leg latency is flat under
``ratio_ceiling(5.0)`` (``tests/bench/_o1_tiers.py``) — a regression that re-scanned
the partition per leg would scale with the bar count and blow past the ceiling.

HONEST envelope (Golden Rule #10): the FIRST touch of a contract is
O(bars-in-contract) (a single small partition read) — this bench primes it out and
measures only the post-cache O(1) leg lookups, exactly the guarantee the module
claims. Best-of-N-MIN timing (DEC-CANARY-MINTIME-001).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("polars")
import polars as pl
from brutex.options.lake_source import LakeOptionBarSource
from brutex.options.lot_size_history import OptionsUnderlying

from tests.bench._o1_tiers import ratio_ceiling

# A calendar-valid NIFTY option contract; the dir name decodes via parse_fno_symbol.
_CONTRACT_DIR = "NSE-NIFTY-30Jun25-24000-CE"
_EXPIRY = date(2025, 6, 30)
_STRIKE = 24000
# 2025-06-02 09:15 IST == 03:45 UTC — the session's first minute.
_SESSION_START_UTC = datetime(2025, 6, 2, 3, 45, tzinfo=UTC)


def _build_contract_lake(root: Path, n_bars: int) -> None:
    """Write a real ``n_bars``-row 1-minute partition for the single option contract."""
    part_dir = root / "bars" / "NSE" / "FNO" / _CONTRACT_DIR / "1minute" / "2025"
    part_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": _SESSION_START_UTC + timedelta(minutes=i),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10,
            "open_interest": 1000,
        }
        for i in range(n_bars)
    ]
    pl.DataFrame(rows).write_parquet(str(part_dir / "06.parquet"))


def _leg_kwargs() -> dict[str, object]:
    """The load_pair leg query — entry at minute 1, forced exit ~95 minutes later."""
    return {
        "instrument": OptionsUnderlying.NIFTY,
        "expiry_date": _EXPIRY,
        "strike": _STRIKE,
        "option_type": "CE",
        "entry_ts": _SESSION_START_UTC + timedelta(minutes=1),
        "exit_ts": _SESSION_START_UTC + timedelta(minutes=95),
    }


def _warm_source(root: Path, n_bars: int) -> LakeOptionBarSource:
    """Build the source and prime the per-contract bar-map cache with one real read."""
    _build_contract_lake(root, n_bars)
    source = LakeOptionBarSource(root)
    pair = source.load_pair(**_leg_kwargs())  # type: ignore[arg-type]
    assert pair is not None, "fixture lake did not resolve the entry/exit bars"
    return source


class TestLoadPairIsO1PerLeg:
    """After the contract is cached, each load_pair is O(1) in the bar count."""

    def test_load_pair_resolves_real_bars(self, tmp_path: Path) -> None:
        source = _warm_source(tmp_path, 300)
        pair = source.load_pair(**_leg_kwargs())  # type: ignore[arg-type]
        assert pair is not None
        assert pair.entry_bar.close == pair.exit_bar.close  # both real, both close 100.5

    def test_load_pair_flat_vs_bar_count(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Repeat load_pair latency at a deep vs shallow contract stays under the ceiling."""
        shallow = _warm_source(tmp_path, 200)
        deep = _warm_source(tmp_path_factory.mktemp("deep_contract"), 8_000)
        kwargs = _leg_kwargs()

        def _per_call(source: LakeOptionBarSource, iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    source.load_pair(**kwargs)  # type: ignore[arg-type]
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(shallow, 20_000)  # warm the timing loop
        t_shallow = _per_call(shallow, 100_000)
        t_deep = _per_call(deep, 100_000)
        ratio = t_deep / max(t_shallow, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"load_pair per-leg latency ratio {ratio:.2f}× (8000 vs 200 bars) exceeds the 5× "
            f"CI ceiling — the reader may have regressed to a per-leg partition scan. "
            f"shallow={t_shallow:.0f}ns deep={t_deep:.0f}ns."
        )
