"""On-branch sub-grid measurement — the design §9 Slice-1 merge mandate.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738). Times a representative
sub-grid through the REAL :func:`brutex.engine.sweep_run.run_persisted_sweep`
on BOTH arms (``BRUTEX_SWEEP_RUST_ENUM`` 0/1), >=2 timeframes including one
1-minute-shaped tuple, k in {2, 5}, at the CLI default ``--top`` (20) AND the
production ``--top=1000`` sizing, and extracts the measured densities the
``combo_counts.md`` §3 rebuild consumes: ``s`` (conflict-free fraction),
``f_zero`` / ``f_trade`` (zero-trade / trade-bearing fractions of the KEPT
population, measured by re-simulating the kept masks).

HONEST DISCLOSURE (Golden Rule #10): this runs on whatever box invokes it —
the 2026-07-11 numbers in ``combo_counts.md`` §3 come from a SHARED x86
sandbox over SYNTHETIC bars (the deterministic golden-fixture generator),
NOT the §8 M4 Pro and NOT the real lake. The REAL-DATA twin + the M4-Pro
run are named operator-box obligations (`scripts/ci/rust_enum_twin_run.sh` /
bench-m4pro). The sub-grid uses a 16-bit condition subset so the Python arm
completes in sandbox time — the C(60,k) full-vocabulary enum-segment ratio
is measured separately (the segment A/B below).

Usage::

    .venv/bin/python scripts/dev_measure_enum_subgrid.py
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from brutex.data.models import CandleBar  # noqa: E402
from brutex.data.option_dedup import OptionDedupIndex  # noqa: E402
from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits  # noqa: E402
from brutex.engine.results_store import ResultsStore  # noqa: E402
from brutex.engine.rust_enum import (  # noqa: E402
    enum_prefilter_via_kernel,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
)
from brutex.engine.rust_trade_sim import ResidentTradeSim  # noqa: E402
from brutex.engine.stage1a import build_bar_bits  # noqa: E402
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep  # noqa: E402
from brutex.orchestrator.audit import AuditWriter  # noqa: E402
from brutex.strategies.types import Side  # noqa: E402
from brutex.vocabulary import has_conflict  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))

#: 16-bit condition subset: C(16,2)=120, C(16,5)=4,368 — Python-arm-feasible.
_BITS = list(range(16))

#: Production Stage-1B floor (MIN_TRADES) — the min_hits>0 arm.
_MIN_HITS = 30


def _bars(n_days: int, per_day: int) -> list[CandleBar]:
    out: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2024, 1, 1, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
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


def _cfg(tf: str, tf_minutes: int, k: int, top_k: int | None) -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf=tf,
        direction=Side.BUY,
        mode="A",
        condition_bits=_BITS,
        k=k,
        data_snapshot_hash="snap-subgrid",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=tf_minutes,
        sl_paisa=80,
        tp_paisa=50,
        min_hits=_MIN_HITS,
        top_k=top_k,
    )


def _run_once(bars: list[CandleBar], cfg: SweepRunConfig, arm: str) -> tuple[float, object]:
    if arm == "on":
        os.environ["BRUTEX_SWEEP_RUST_ENUM"] = "1"
    else:
        os.environ.pop("BRUTEX_SWEEP_RUST_ENUM", None)
    reset_enum_self_check_for_tests()
    reset_observability_memo_for_tests()
    with TemporaryDirectory() as td:
        tmp = Path(td)
        store = ResultsStore(path=tmp / "s.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-6))
        t0 = time.perf_counter()
        with AuditWriter(path=tmp / "s.jsonl") as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest="5abc0de5abc0de5a"[:16],
            )
        wall = time.perf_counter() - t0
    os.environ.pop("BRUTEX_SWEEP_RUST_ENUM", None)
    return wall, summary


def _densities(bars: list[CandleBar], k: int) -> tuple[float, float, float, int, int]:
    """(s, f_zero, f_trade, kept, evaluated) for the tuple at _MIN_HITS."""
    bar_bits = build_bar_bits(bars)
    survivors = [m for m in iter_masks_k_of_bits(_BITS, k) if not has_conflict(m)]
    enumerated = sum(1 for _ in iter_masks_k_of_bits(_BITS, k))
    hits = count_supermasks(bar_bits, survivors)
    kept = [m for m, h in zip(survivors, hits, strict=True) if h >= _MIN_HITS]
    s = len(survivors) / enumerated if enumerated else 0.0
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    zero = 0
    for i in range(0, len(kept), 256):
        chunk = kept[i : i + 256]
        for trades in sim.simulate_masks(
            chunk, direction=Side.BUY, stop_loss_paisa=80, take_profit_paisa=50
        ):
            if not trades:
                zero += 1
    f_zero = zero / len(kept) if kept else 0.0
    f_trade = (len(kept) - zero) / len(kept) if kept else 0.0
    return s, f_zero, f_trade, len(kept), len(survivors)


def _segment_ab() -> None:
    """The enum-SEGMENT A/B at the production C(60,5) shape (no trade sim)."""
    bits = list(range(60))
    t0 = time.perf_counter()
    py = [m for m in iter_masks_k_of_bits(bits, 5) if not has_conflict(m)]
    t_py = time.perf_counter() - t0
    os.environ["BRUTEX_SWEEP_RUST_ENUM"] = "1"
    reset_enum_self_check_for_tests(masks_remaining=0)  # steady state (no shadow)
    reset_observability_memo_for_tests()
    t0 = time.perf_counter()
    result = enum_prefilter_via_kernel(
        bits, 5, min_hits=0, bar_bits=[], count_hits=lambda m: [0] * len(m), digest_path=True
    )
    t_rust = time.perf_counter() - t0
    os.environ.pop("BRUTEX_SWEEP_RUST_ENUM", None)
    assert result is not None
    assert result.kept == py
    print(
        f"ENUM SEGMENT C(60,5)=5,461,512 min_hits=0: python {t_py:.3f}s | "
        f"kernel(ranged, steady) {t_rust:.3f}s | ratio {t_py / t_rust:.1f}x"
    )


def main() -> None:
    print("# sub-grid measurement — shared x86 sandbox, synthetic bars (2026-07-11)")
    print(f"# bits n=16 subset, min_hits={_MIN_HITS}, both arms, tops (20, 1000)")
    shapes = [
        ("1minute", 1, 250, 375),  # one year of 1m bars = 93,750
        ("15minute", 15, 250, 25),  # the high-TF tuple = 6,250 bars
    ]
    for tf, tfm, days, per_day in shapes:
        bars = _bars(days, per_day)
        for k in (2, 5):
            s, f_zero, f_trade, kept, evaluated = _densities(bars, k)
            print(
                f"\nTF={tf} n_bars={len(bars)} k={k}: s={s:.4f} f_zero={f_zero:.4f} "
                f"f_trade={f_trade:.4f} kept={kept} evaluated={evaluated}"
            )
            for top in (20, 1000):
                row = []
                for arm in ("off", "on"):
                    wall, summary = _run_once(bars, _cfg(tf, tfm, k, top), arm)
                    row.append((arm, wall, summary.store_content_hash))
                (_, w0, h0), (_, w1, h1) = row
                match = "HASH-MATCH" if h0 == h1 else "HASH-DIVERGENCE!!"
                print(
                    f"  top={top:5d}: OFF {w0:8.3f}s | ON {w1:8.3f}s | "
                    f"ratio {w0 / w1:5.2f}x | {match}"
                )
    print()
    _segment_ab()


if __name__ == "__main__":
    main()
