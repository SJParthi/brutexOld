"""O(1) benchmarks for the persisted sweep runner (`brutex.engine.sweep_run`).

Two hot-path / scaling invariants (CLAUDE.md §2 r5 / §12 / Golden Rule #8):

* :meth:`ComboDedup.seen` — the pre-evaluation dedup probe must be latency-
  independent of how many identities are already marked (a Bloom→exact O(1)
  membership test, not a scan). Routed through the single-source-of-truth
  :func:`tests.bench._o1_tiers.ratio_ceiling` (5× on shared CI, the strict 1.1×
  tier on the M4-Pro runner), mirroring
  ``tests/bench/test_engine_results_store_o1.py``.
* The audit-append count is **independent of the combo count** — a k=1 sweep
  (C(5,1)=5 masks) and a k=2 sweep (C(5,2)=10 masks) both emit exactly 3 audit
  lines. This pins the O(1)-in-combo-count audit cost (Golden Rule #8), the
  scaling analogue of the per-mask O(1) hot paths the engine bricks already
  bench (``ResultsStore.append`` / ``count_supermasks``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, mask_run_id, run_persisted_sweep
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling

_IST = timezone(timedelta(hours=5, minutes=30))
_SMALL = 1_000
_LARGE = 10_000

_CTX: dict[str, str] = {
    "direction": "bull",
    "instrument": "NIFTY",
    "tf": "1m",
    "mode": "A",
    "data_snapshot_hash": "snap0001",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}


def _rid(seed: int) -> str:
    return mask_run_id(mask=seed, params={"seed": seed}, **_CTX)


def _filled(n: int) -> ComboDedup:
    dedup = ComboDedup(OptionDedupIndex(capacity=10 * _LARGE, fpr_target=1e-6))
    dedup.mark_many(_rid(i) for i in range(n))
    return dedup


class TestSeenIsO1:
    def test_seen_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        dedup = _filled(_SMALL)
        probe = _rid(_SMALL // 2)
        result = benchmark(lambda: dedup.seen(probe))
        assert result is True

    def test_seen_at_large_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        dedup = _filled(_LARGE)
        probe = _rid(_LARGE // 2)
        result = benchmark(lambda: dedup.seen(probe))
        assert result is True

    def test_seen_ratio_is_o1(self) -> None:
        """`seen` latency at 10k / 1k marked identities must stay O(1) (< ceiling)."""

        def _time(n: int, *, iterations: int = 5_000) -> float:
            dedup = _filled(n)
            probe = _rid(n // 2)
            for _ in range(50):  # warm
                dedup.seen(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                dedup.seen(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"ComboDedup.seen ratio {ratio:.2f}× (10k vs 1k marked) exceeds 5× — "
            f"dedup may have regressed from O(1) Bloom→exact membership. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )


def _bars() -> list[CandleBar]:
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


def _config(k: int) -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2, 3, 4],
        k=k,
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
    )


class TestAuditCountIsComboIndependent:
    def test_three_lines_regardless_of_k(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
        # k=1 → 5 masks; k=2 → 10 masks; both emit EXACTLY 3 audit lines.
        bars = _bars()
        counts: list[int] = []
        for tag, k in (("k1", 1), ("k2", 2)):
            store = ResultsStore(path=tmp_path / f"{tag}.parquet")
            dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
            audit_path = tmp_path / f"{tag}.jsonl"
            with AuditWriter.open(audit_path) as audit:
                run_persisted_sweep(
                    bars, _config(k), store=store, dedup=dedup, audit=audit, exec_bars_1m=bars
                )
            counts.append(verify_chain(audit_path))
        assert counts == [3, 3]
