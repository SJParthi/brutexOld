"""Gate-ON sweep golden (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, G3).

A FROZEN three-year synthetic fixture (2021 up-drift · 2022 down-drift · 2023
up-drift, 4 sessions/year × 120 one-minute bars) swept with the temporal year
gate ACTIVE at the DEFAULT thresholds (``min_edge_year_sessions=1`` so all
three years are judged): of the three hard-door survivors, mask ``1<<0``
passes BOTH doors (2/3 profitable judged years; positive-mass share
1620/3220 ≈ 0.503 ≤ 0.6) while masks ``1<<2`` and ``1<<4`` die at P2 (shares
1600/2620 ≈ 0.611 and 1550/2570 ≈ 0.603 — the profit lives in one year). The
pinned literals below are the exclusion truth + the store's content address;
the DEFAULT-arm byte-identity teeth live in
``test_engine_sweep_run_golden.py`` (its ``_GOLDEN_CONTENT_HASH`` is
UNMODIFIED by this DEC — the gate-OFF arm is byte-identical to before).

Same version-envelope caveat as every store-hash golden: the literal content
hash is polars/zstd-build sensitive; the logical summary pins are the
version-robust contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.execution.fill_mode import FillMode
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking.temporal import TemporalRobustnessConfig
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: (year, drift) blocks — FROZEN. "up" mirrors the sweep-run golden's pattern;
#: "down" is its inversion; base price 300.00 keeps every bar positive.
_YEAR_DRIFTS: tuple[tuple[int, str], ...] = ((2021, "up"), (2022, "down"), (2023, "up"))


def bars_3y(year_drifts: tuple[tuple[int, str], ...] = _YEAR_DRIFTS) -> list[CandleBar]:
    """The frozen 3-year fixture: 4 sessions/year × 120 deterministic 1m bars."""
    out: list[CandleBar] = []
    for year, drift in year_drifts:
        price = 300.0
        for day in range(4):
            for minute in range(120):
                if drift == "up":
                    close = price + 0.6 if minute % 3 else price - 0.3
                else:
                    close = price - 0.6 if minute % 3 else price + 0.3
                ts = (
                    datetime(year, 3, 2, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
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


#: The gate config the golden runs at: DEFAULT thresholds; the 1-session edge
#: floor makes all three 4-session years judged (the production 120-session
#: default needs real multi-month years — config, not a special case).
GOLDEN_TEMPORAL = TemporalRobustnessConfig(min_edge_year_sessions=1)


def golden_config(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2, 3, 4],
        "k": 1,
        "data_snapshot_hash": "snap3y01",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "tf_minutes": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
        "temporal": GOLDEN_TEMPORAL,
        # FROZEN GOLDEN PINS THE **PRECISE** LEGACY MODE (DEC-FILL-WORST-CASE-001,
        # club S2 fold): the gate-ON hash + the displacement/survivor literals
        # below were minted under exact fills; the SweepRunConfig field default
        # is now the WORST_CASE production floor, so this config pins PRECISE
        # explicitly — the test_engine_sweep_run_golden.py precedent.
        "fill_mode": FillMode.PRECISE,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def run_sweep(tmp_path: Path, tag: str, cfg: SweepRunConfig) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(bars_3y(), cfg, store=store, dedup=dedup, audit=audit)


#: The hand-pinned gate-ON store content hash for the frozen fixture
#: (generated 2026-07-09; polars/zstd-build sensitive — module docstring).
#: RE-LOCKED 2026-07-15 at the club S2 merge fold (was
#: f5f318f0b1be3afebbedf4b2570c019c581f2c34c11ae113ea291a46314c5cc0): main's
#: REFERENCE-ONLY VIX trio (DEC-VIX-STAMP-WIRE-001) landed before this branch
#: merged, so the store schema gained three always-emitted nullable Float64
#: columns and the BYTES moved — the row VALUES, survivor set and exclusion
#: literals below are unchanged (those pins passed unmodified across the
#: re-lock), and the frozen MAIN sweep golden (`_GOLDEN_CONTENT_HASH` in
#: test_engine_sweep_run_golden.py) stayed byte-untouched.
_GOLDEN_TEMPORAL_CONTENT_HASH = "8341bed7b4281709d81b64f61bf05c438b18c26b87a7d097015bbfb937ad8647"

#: The exclusion truth at DEFAULT thresholds (the operator demand, frozen).
_SURVIVOR_MASKS = (1 << 0,)
_GATE_KILLED = 2  # masks 1<<2 + 1<<4 — P2 positive-mass share > 0.6


def test_golden_gate_on_summary(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    summary = run_sweep(tmp_path, "g", golden_config())
    assert summary.enumerated == 5
    assert summary.evaluated == 5
    assert summary.temporal_judged_years == 3
    assert summary.temporal_abstained is False
    assert summary.temporal_gated == _GATE_KILLED
    assert summary.temporal_observed_kills == 0
    assert summary.ranked == 1
    assert summary.persisted == 1


def test_golden_gate_on_survivor_set(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    run_sweep(tmp_path, "g", golden_config())
    loaded = ResultsStore.load(tmp_path / "g.parquet")
    assert tuple(sorted(row.mask_bits for row in loaded.rows())) == _SURVIVOR_MASKS
    row = loaded.rows()[0]
    # The captured columns are PRESENT and carry the judged denominator.
    assert row.temporal_judged_years_json == '["2021","2022","2023"]'
    assert row.stats_by_year_json is not None


def test_golden_gate_on_content_hash_and_determinism(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    a = run_sweep(tmp_path, "a", golden_config())
    b = run_sweep(tmp_path, "b", golden_config())
    assert a.store_content_hash == b.store_content_hash
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()
    assert a.store_content_hash == _GOLDEN_TEMPORAL_CONTENT_HASH
