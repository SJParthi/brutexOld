"""End-to-end integration: a persisted sweep writes a store + an audited chain.

Exercises the full :func:`brutex.engine.sweep_run.run_persisted_sweep` path
against real engine bricks (Stage-1A → combo enumeration → dedup → trade sim →
ranking → results store → audit), asserting the cross-brick contracts that the
unit tests stub:

* the Parquet store file exists and re-loads with the persisted finalists;
* the audit JSONL exists and its hash chain verifies
  (:func:`brutex.orchestrator.audit.verify_chain`);
* the audit carries EXACTLY three lines — ``SWEEP_START`` / ``SWEEP_FLUSH`` /
  ``SWEEP_COMPLETE`` — NOT one per combo (Golden Rule #8: O(1) audit cost).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    # The worst-case-rankable gap-up recipe (DEC-FILL-WORST-CASE-ONLY-001):
    # narrow gap-up bars (range 0.50 < the SL 0.80 — no same-bar stop-out;
    # the next gap-up's low clears the TP level so the win banks +50), a
    # shallow bearish bar each 7-bar cycle and a DEEP bearish bar (range
    # 0.85 > the SL) every 4th cycle whose stop-out keeps profit_factor
    # finite for every k=1 mask over bits 0-4 (per-day price RESET under
    # carried EMAs fires the below-EMA bits early-day, the above-EMA bits
    # late-day).
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
    return out


def _config(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2, 3, 4],
        "k": 1,
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "tf_minutes": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def test_end_to_end_store_and_audit(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush/load need the [data] extra; skips the .[dev]-only job
    bars = _bars()
    store_path = tmp_path / "results.parquet"
    audit_path = tmp_path / "audit.jsonl"
    store = ResultsStore(path=store_path)
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))

    with AuditWriter.open(audit_path) as audit:
        summary = run_persisted_sweep(bars, _config(), store=store, dedup=dedup, audit=audit)

    # 1. Store Parquet exists + re-loads with the persisted finalists.
    assert store_path.exists()
    reloaded = ResultsStore.load(store_path)
    assert len(reloaded) == summary.persisted >= 1

    # 2. Audit JSONL exists + its hash chain verifies (Golden Rule #8).
    assert audit_path.exists()
    assert verify_chain(audit_path) == 3

    # 3. Exactly the three sweep actions, in order — NOT proportional to combos.
    actions = [e.action for e in read_entries(audit_path)]
    assert actions == ["SWEEP_START", "SWEEP_FLUSH", "SWEEP_COMPLETE"]

    # 4. The flush line carries the same content hash the summary returned.
    flush_entry = next(e for e in read_entries(audit_path) if e.action == "SWEEP_FLUSH")
    assert flush_entry.payload["store_content_hash"] == summary.store_content_hash
    assert flush_entry.payload["persisted"] == summary.persisted


def test_audit_line_count_independent_of_combo_count(tmp_path: Path) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    # A k=2 sweep enumerates C(5,2)=10 masks vs C(5,1)=5 for k=1 — the audit line
    # count must stay 3 either way (the O(1)-in-combo-count audit invariant).
    bars = _bars()
    for tag, k in (("k1", 1), ("k2", 2)):
        store = ResultsStore(path=tmp_path / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        audit_path = tmp_path / f"{tag}.jsonl"
        with AuditWriter.open(audit_path) as audit:
            run_persisted_sweep(bars, _config(k=k), store=store, dedup=dedup, audit=audit)
        assert verify_chain(audit_path) == 3
