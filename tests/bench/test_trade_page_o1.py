"""O(page) bench gate for :func:`brutex.engine.trade_store.read_trade_page` (R29).

Pins the R26/R29 decree for the trade-list drill: a page read at a FIXED
offset/limit stays FLAT as the tuple's TOTAL stored trade rows grow 100×
(10k → 1M rows) — the stamped contiguous-span design makes the page a pure
positional slice plus an O(1)-stat stamp probe, never a predicate scan over
foreign combos and never an O(file) re-hash. The page total itself must come
from the stamp (equal at both sizes, no COUNT scan).

Honest envelope (module doctrine): reaching a deep global row offset still
walks Parquet row-group METADATA up to it — O(row-groups), not literal O(1) —
which is exactly what this ratio gate demonstrates is flat in practice at the
pinned row-group size. Fixtures are synthesized columnar (1M `CapturedTrade`
objects would dominate the bench) through the SAME pinned render/stamp shapes
the writer uses — the writer's byte discipline is separately golden-locked.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from brutex.engine.trade_store import (
    AGG_COLUMNS,
    EQUITY_COLUMNS,
    TRADE_COLUMNS,
    TRADES_SCHEMA_VERSION,
    SidecarPaths,
    _render_frame_bytes,
    read_trade_page,
    sidecar_paths_for,
)

from tests.bench._o1_tiers import cache_tier_ceiling

_FILLER_RUN = "00" * 32
_TARGET_RUN = "aa" * 32
_STORE_HASH = "12" * 32
_TARGET_TRADES = 2_000
_PAGE_OFFSET = 1_000
_PAGE_LIMIT = 100

_N_SMALL = 10_000
_N_BIG = 1_000_000

_ROUNDS = 7
_WARMUP = 2


def _trades_frame_bytes(n_filler: int) -> bytes:
    total = n_filler + _TARGET_TRADES
    run_id = [_FILLER_RUN] * n_filler + [_TARGET_RUN] * _TARGET_TRADES
    trade_seq = list(range(n_filler)) + list(range(_TARGET_TRADES))
    base_us = 1_704_180_000_000_000  # 2024-01-02 ~09:30 IST, µs
    columns: dict[str, list[object]] = {
        "run_id": list(run_id),
        "trade_seq": list(trade_seq),
        "entry_ts_us": [base_us + 60_000_000 * i for i in range(total)],
        "exit_ts_us": [base_us + 60_000_000 * i + 60_000_000 for i in range(total)],
        "entry_px": [100.0] * total,
        "exit_px": [100.5] * total,
        "direction": ["BUY"] * total,
        "lots": [1] * total,
        "exit_reason": ["take_profit"] * total,
        "pnl_paisa": [50] * total,
        "costs_paisa": [0] * total,
        "mae_paisa": [10] * total,
        "mfe_paisa": [60] * total,
        "mae_1m_paisa": [None] * total,
        "mfe_1m_paisa": [None] * total,
        "path_1m_status": ["no_1m_data"] * total,
        "vix_entry": [None] * total,
        "vix_exit": [None] * total,
    }
    return _render_frame_bytes(TRADE_COLUMNS, columns)


def _build_sidecars(root: Path, n_filler: int) -> SidecarPaths:
    paths = sidecar_paths_for(root / "NIFTY" / "1minute" / "bull_k1_bench0000000000.parquet", root)
    paths.trades.parent.mkdir(parents=True, exist_ok=True)
    trades_bytes = _trades_frame_bytes(n_filler)
    aggs_bytes = _render_frame_bytes(AGG_COLUMNS, {name: [] for name, _ in AGG_COLUMNS})
    equity_bytes = _render_frame_bytes(EQUITY_COLUMNS, {name: [] for name, _ in EQUITY_COLUMNS})
    paths.trades.write_bytes(trades_bytes)
    paths.aggs.write_bytes(aggs_bytes)
    paths.equity.write_bytes(equity_bytes)
    stamp = {
        "schema_version": TRADES_SCHEMA_VERSION,
        "store_content_hash": _STORE_HASH,
        "run_ids": 2,
        "trade_rows": n_filler + _TARGET_TRADES,
        "agg_rows": 0,
        "equity_rows": 0,
        "content_hashes": {
            "trades": hashlib.sha256(trades_bytes).hexdigest(),
            "aggs": hashlib.sha256(aggs_bytes).hexdigest(),
            "equity": hashlib.sha256(equity_bytes).hexdigest(),
        },
        "content_sizes": {
            "trades": len(trades_bytes),
            "aggs": len(aggs_bytes),
            "equity": len(equity_bytes),
        },
        "trade_spans": {
            _FILLER_RUN: [0, n_filler],
            _TARGET_RUN: [n_filler, _TARGET_TRADES],
        },
        "produced_at_utc": "2026-07-07T00:00:00+00:00",
    }
    paths.stamp.write_text(json.dumps(stamp, sort_keys=True, separators=(",", ":")))
    return paths


def _time_page_read_ns(paths: SidecarPaths) -> float:
    for _ in range(_WARMUP):
        read_trade_page(paths, run_id=_TARGET_RUN, offset=_PAGE_OFFSET, limit=_PAGE_LIMIT)
    best = float("inf")
    for _ in range(_ROUNDS):
        start = time.perf_counter_ns()
        page = read_trade_page(paths, run_id=_TARGET_RUN, offset=_PAGE_OFFSET, limit=_PAGE_LIMIT)
        best = min(best, float(time.perf_counter_ns() - start))
        assert len(page.rows) == _PAGE_LIMIT
    return best


def test_trade_page_read_flat_vs_total_rows(tmp_path: Path) -> None:
    small = _build_sidecars(tmp_path / "small", _N_SMALL)
    big = _build_sidecars(tmp_path / "big", _N_BIG)

    # The page total is the stamp's O(1) span lookup — identical semantics at
    # both sizes, never a COUNT scan over the file.
    small_page = read_trade_page(small, run_id=_TARGET_RUN, offset=_PAGE_OFFSET, limit=_PAGE_LIMIT)
    big_page = read_trade_page(big, run_id=_TARGET_RUN, offset=_PAGE_OFFSET, limit=_PAGE_LIMIT)
    assert small_page.total == big_page.total == _TARGET_TRADES
    assert [row["trade_seq"] for row in small_page.rows] == [
        row["trade_seq"] for row in big_page.rows
    ]

    t_small = _time_page_read_ns(small)
    t_big = _time_page_read_ns(big)
    ratio = t_big / max(t_small, 1.0)
    assert ratio < cache_tier_ceiling(5.0), (
        f"trade-page read latency ratio {ratio:.2f}× for {_N_BIG:,} vs {_N_SMALL:,} stored rows "
        f"(t_small={t_small / 1e6:.2f} ms, t_big={t_big / 1e6:.2f} ms) — the fixed-offset page "
        "read must stay flat in total stored trades (R26/R29 decree)"
    )
