"""Gate-ON sweep golden (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, G3).

A FROZEN three-year synthetic fixture (2021 gap-up ramp · 2022 gap-down
decline · 2023 below-EMA200 grind then ramp, 4 sessions/year × 120
one-minute bars) swept with the temporal year gate ACTIVE at the DEFAULT
thresholds (``min_edge_year_sessions=1`` so all three years are judged): of
the three hard-door survivors, mask ``1<<0`` passes BOTH doors (2/3
profitable judged years; positive-mass share 3780/6670 ≈ 0.567 ≤ 0.6) while
masks ``1<<2`` and ``1<<4`` die at P2 (shares 3575/4980 ≈ 0.718 and
3470/4875 ≈ 0.712 — the profit lives in one year). The pinned literals below
are the exclusion truth + the store's content address; the DEFAULT-arm
byte-identity teeth live in ``test_engine_sweep_run_golden.py`` (its
``_GOLDEN_CONTENT_HASH`` is UNMODIFIED by this DEC — the gate-OFF arm is
byte-identical to before).

2026-07-18 merge-reconcile (lawful re-craft, in-file justification): the
original up/down close-drift fixture was tuned for the pre-merge
open-anchored fill law; under the merged DEC-FILL-WORST-CASE-ONLY-001
adverse-max fill every one of its trades lost (0 wins — a toothless golden,
the same class main's own sweep-run golden fixture was re-crafted for). The
geometry below re-creates the SAME displacement story under the worst-case
law using main's proven ranking shape — 7-bar cycles of NARROW gap-up bars
(range 0.50 < the SL 0.80; the next gap-up banks the +50 TP) with periodic
deep bearish loss carriers — plus a flat 2021 tail (EMA200 convergence
bounds the 2022 lag losses) and a below-EMA200 2023 grind day (only
``close>ema20`` fires there, keeping mask ``1<<0`` balanced while the
EMA200-lag masks stay year-concentrated). The exclusion truth is UNCHANGED:
survivor ``{1<<0}``, temporal kills ``{1<<2, 1<<4}``.

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

#: Per-year day profiles — FROZEN (worst-case-fill-viable, module docstring):
#: "ramp" = main's proven ranking cycle (gap-up narrow win bars + a shallow
#: bearish breather + a deep bearish loss carrier every 4th cycle); "flat" =
#: a tight consolidation letting EMA200 converge; "down" = narrow gap-down
#: bars (every BUY entry loses); "grind"/"grindx" = a gentle below-EMA200
#: up-grind (only ``close>ema20`` fires; "grindx" doubles the carrier density
#: to keep mask ``1<<0``'s year balance under the 0.6 share door). The price
#: path is CONTINUOUS across days AND years (base 300.00; the 2022 crash-then-decline
#: bottoms well above zero — every bar positive).
_YEAR_PROFILES: tuple[tuple[int, tuple[str, str, str, str]], ...] = (
    (2021, ("ramp", "ramp", "ramp", "flat")),
    (2022, ("crash", "down", "down", "flat")),
    (2023, ("grindx", "ramp", "ramp", "ramp")),
)


def bars_3y(
    year_profiles: tuple[tuple[int, tuple[str, str, str, str]], ...] = _YEAR_PROFILES,
) -> list[CandleBar]:
    """The frozen 3-year fixture: 4 sessions/year × 120 deterministic 1m bars."""
    out: list[CandleBar] = []
    price = 300.0
    cycle = 0
    for year, profiles in year_profiles:
        for day, profile in enumerate(profiles):
            for minute in range(120):
                k = minute % 7
                if k == 0:
                    cycle += 1
                if profile == "ramp":
                    if k == 3:  # shallow bearish breather
                        o = price
                        c, h, lo = o - 0.3, o + 0.1, o - 0.4
                    elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                        o = price
                        c, h, lo = o - 0.65, o + 0.1, o - 0.75
                    else:  # narrow gap-up win bar
                        o = price + 3.0
                        c, h, lo = o + 0.3, o + 0.4, o - 0.1
                elif profile == "flat":
                    o = price
                    if k % 2:
                        c, h, lo = o + 0.05, o + 0.1, o - 0.1
                    else:
                        c, h, lo = o - 0.05, o + 0.1, o - 0.1
                elif profile == "crash":
                    # One giant opening gap puts price INSTANTLY below EMA200,
                    # so the EMA200-lag masks (1<<2 / 1<<4) exit their losing
                    # 2022 exposure in a handful of bars (the consecutive-loss
                    # hard door must not be what kills them — P2 is); the
                    # every-cycle up-pairs break the fast masks' loss streaks.
                    if minute == 0:
                        o = price - 250.0
                        c, h, lo = o - 0.3, o + 0.1, o - 0.4
                    elif k == 3:  # shallow bullish breather
                        o = price
                        c, h, lo = o + 0.3, o + 0.4, o - 0.1
                    elif k in (5, 6):  # streak-breaking gap-up win pair
                        o = price + 3.0
                        c, h, lo = o + 0.3, o + 0.4, o - 0.1
                    else:
                        o = price - 2.0
                        c, h, lo = o - 0.3, o + 0.1, o - 0.4
                elif profile == "down":
                    if k == 3:  # shallow bullish breather
                        o = price
                        c, h, lo = o + 0.3, o + 0.4, o - 0.1
                    else:  # narrow gap-down bar — every BUY entry loses
                        o = price - 2.0
                        c, h, lo = o - 0.3, o + 0.1, o - 0.4
                elif k == 4 and cycle % (2 if profile == "grindx" else 4) == 0:
                    o = price
                    c, h, lo = o - 0.65, o + 0.1, o - 0.75
                else:
                    o = price + 0.55
                    c, h, lo = o + 0.15, o + 0.25, o - 0.1
                ts = (
                    datetime(year, 3, 2, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
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
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def run_sweep(tmp_path: Path, tag: str, cfg: SweepRunConfig) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(bars_3y(), cfg, store=store, dedup=dedup, audit=audit)


#: The hand-pinned gate-ON store content hash for the frozen fixture
#: (polars/zstd-build sensitive — module docstring). RE-LOCKED 2026-07-18 at
#: the merge-reconcile (was f5f318f0…4c5cc0, generated 2026-07-09): the
#: fixture geometry was lawfully re-crafted for the merged
#: DEC-FILL-WORST-CASE-ONLY-001 adverse-max fill law (module docstring) and
#: the merged writer now always emits the DEC-VIX-STAMP-WIRE-001 VIX trio —
#: both change the store bytes; the exclusion truth + summary pins are the
#: version-robust contract and are UNCHANGED.
#: CLUB-SIDE ADOPTION (2026-07-19, integration/club-s2-results-v2 — ledger
#: #1848): this file is ADOPTED VERBATIM from the member head 9cdb93bb
#: (claude/sweep-temporal-robustness — its post-#1825 merge-forward carried
#: the lawful re-craft above); the club fold's interim hash hop was
#: f5f318f0… (the original literal, minted at member commit 8b42422a
#: 2026-07-09 and still the pin at the fold-era member blob 6703dc11) →
#: 8341bed7…ad8647 (the 2026-07-15 VIX-trio re-lock over the
#: PRE-re-craft geometry, toothless under worst-case fills: 0 hard-door
#: survivors, the documented empty-store hash cadb06a3…) → 5d8f4384… (this
#: re-craft). The canonical run-identity golden
#: (test_engine_sweep_run_golden.py) is byte-UNTOUCHED by the adoption.
#: RE-LOCKED at the 2026-07-19 W2 re-integration (MIGRATION.md #1976; the
#: sanctioned #1749 additive-only re-lock; hop 5d8f4384… → fdc92094…): the
#: W2 metrics-depth families re-appended to the writer as 8 ALWAYS-written
#: nullable Float64 columns strictly AFTER the temporal quartet's landed
#: position, so the temporal-carrying store's bytes moved. AUDITED DELTA
#: CLASS — schema-additive ONLY, proven against a live pre-W2 main-baseline
#: worktree at d5a29747 (PR #1580's landing commit), which reproduces its
#: literal ``5d8f4384…e5cf6`` EXACTLY on this box: the SAME single survivor
#: row (mask ``1<<0``, run_id byte-stable), the SAME 32-column prefix in the
#: SAME order with EVERY old column value equal (machine-diffed: zero
#: old-column diffs), plus EXACTLY the 8 appended columns. HAND AUDIT of the
#: 8 from the survivor's own 256-trade stream (exit-count wire
#: [sl=41, tp=214, forced=0, signal=0, eod=0, session_gap=1]; W=214 wins /
#: L=42 losses / 0 scratch; gp=10_190, gl=5_810, net=+4_380):
#:   loss_rate           = L/T = 42/256 = 0.1640625
#:   sl_hit_rate         = SL/T = 41/256 = 0.16015625 (< loss_rate — ONE
#:                         loss is the session_gap exit, the honest split)
#:   kelly_fraction      = p − q/b with p = 214/256 = 0.8359375,
#:                         b = (10190/214)/(5810/42) = 47.61682…/138.33333…
#:                         = 0.3442179934692039 → 0.35931366535819426
#:   log_growth_at_half_kelly = p·ln(1 + f/2·b) + q·ln(1 − f/2) at
#:                         f = kelly → 0.017670134233029836
#:   trailing (24-month = 730-day window from the GLOBAL anchor
#:   ``bars[-1].timestamp`` = 2023-03-05 05:45 UTC; cutoff EXACTLY
#:   2021-03-05 05:45 UTC — all 100 2021 trades excluded, latest excluded
#:   exit 2021-03-05 05:11 UTC): n=156 (127 W / 29 L), gp=5_840, gl=4_350 →
#:   trailing_net_pnl_paisa = +1_490, trailing_profit_factor =
#:   5840/4350 = 1.342528735632184, trailing_sharpe_ratio =
#:   0.11342874411769895 (the combine sharpe over the windowed per-trade
#:   returns)
#:   decay_weighted_net_paisa (30-day age buckets from the anchor, half-life
#:   12): bucket 0 (2023 trades) +3_780·1 + bucket 12 (2022) −2_290·0.5 +
#:   bucket 24 (2021) +2_890·0.25 = 3780 − 1145 + 722.5 = 3_357.5
#:   (cross-check: 3780 − 2290 + 2890 = +4_380 = the row's full net ✓;
#:   trailing = buckets 0+12 = 3780 − 2290 = +1_490 ✓)
#: Every derived figure equals the persisted row value EXACTLY (float
#: bit-compare, executed 2026-07-19). The exclusion truth + summary pins
#: below are UNCHANGED — the ONE sanctioned ranking-gate delta of the W2
#: lane (Door 5, loosening-only) kills nothing here.
_GOLDEN_TEMPORAL_CONTENT_HASH = "fdc92094ad05076c4ec680f597c0de02de321086d8f206ef849d832919780a76"

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
