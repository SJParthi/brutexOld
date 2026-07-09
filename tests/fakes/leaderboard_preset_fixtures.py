"""Shared ResultRow + per-tuple-store builders for the leaderboard preset tests.

DEC-SWEEP-RESULTS-DEPTH-001 (package C — gap-register rulings 10 + 12): the
view-level preset suite (``tests/unit/test_sweep_results_view_preset.py``) and
the route-level suite (``tests/unit/test_dashboard_results_route_preset.py``)
both need valid :class:`~brutex.engine.results_store.ResultRow` fixtures with
per-test metric overrides plus a writer that lands them at the REAL
``<lake>/sweeps/<instrument>/<tf>/<direction>_k1_<digest>.parquet`` layout the
production reader globs. One home (this package's anti-cross-import rule)
instead of a third copy-paste of the builders.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from brutex.engine.results_store import ResultRow, ResultsStore


def run_id_hex(seed: int) -> str:
    """A valid 64-char lowercase-hex run-identity from an int seed (deterministic)."""
    return f"{seed:064x}"


def make_leaderboard_row(seed: int, **overrides: Any) -> ResultRow:
    """A valid :class:`ResultRow` with sane defaults; override any field per test."""
    base: dict[str, Any] = {
        "run_id": run_id_hex(seed),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0,
        "net_pnl_paisa": 100_00,
        "gross_profit_paisa": 200_00,
        "gross_loss_paisa": 100_00,
        "max_drawdown_paisa": 50_00,
        "expectancy_paisa": 10_00,
        "total_trades": 10,
        "winning_trades": 6,
        "losing_trades": 4,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.2,
        "calmar_ratio": 0.9,
        "profit_factor": 2.0,
        "win_rate": 0.6,
        "max_drawdown_pct": 5.0,
        "avg_entry_price": Decimal("100.00"),
        "avg_exit_price": Decimal("101.00"),
    }
    base.update(overrides)
    return ResultRow(**base)


def write_tuple_store(
    lake_root: Path,
    rows: list[ResultRow],
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
) -> Path:
    """Write a per-tuple store at the production ``<lake>/sweeps/...`` layout."""
    dest = lake_root / "sweeps" / instrument / tf / f"{direction}_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest
