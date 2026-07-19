"""Fluke repro for DEC-SWEEP-OVERFIT-GATE-DEFAULT-ON-001 (MIGRATION.md #1961).

NOT pytest-collected (no ``test_`` prefix — ``tests/scratch/`` holds
executable evidence scripts, never suite members). Run it directly::

    python tests/scratch/overfit_gate_fluke_repro.py

Rebuilt FROM THE REPO (the prior scratchpad evidence dir was absent — stated
honestly; nothing reused, nothing fabricated) and committed at the #1961
hostile-review round so the measured case is reproducible from the tree.

Shape: one 30-trade single-event fluke (one +100,000-paisa trade, the rest
tiny scratches -> PF huge, DD ~0) inside a 38-candidate leaderboard pool of
legit multi-event strategies. Rank gate OFF vs ON via the REAL
``brutex.ranking.rank_combos`` + ``compute_metrics`` pipeline.

Headline numbers (re-executed 2026-07-19 on this tree): fluke DSR 0.1304 /
PF 95.1538 / gate OFF rank #1 of 38 survivors / gate ON REFUSED
(fluke_rank=None) / collateral 10 of 37 legit strategies also refused.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from brutex.metrics.compute import compute_metrics
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.ranking import OverfitGateConfig, rank_combos
from brutex.ranking.overfit import deflated_sharpe_for, pool_sharpe_dispersion
from brutex.strategies.types import Side

START = 10_000_000  # 1 lakh rupees in paisa
T0 = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)


def make_metrics(pnls: list[int]):
    trades = []
    equity = [START]
    ts = T0
    for pnl in pnls:
        ts = ts + timedelta(minutes=30)
        trades.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=max(pnl, 0) + 50,
                mae_paisa=max(-pnl, 0) + 50,
                holding_bars=5,
                entry_ts=ts,
                exit_ts=ts + timedelta(minutes=5),
                direction=Side.BUY,
            )
        )
        equity.append(equity[-1] + pnl)
    curve = EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=START, span_years=1.0)
    return compute_metrics(trades, curve, total_bars=len(pnls) * 6)


def main() -> None:
    rng = random.Random(42)

    # The FLUKE: 30 trades — one +100,000-paisa event, 27 tiny wins
    # (+10..+60 paisa), 2 small losses -> PF ~ 95, DD ~ 0.
    fluke_pnls = [100_000] + [rng.randint(10, 60) for _ in range(27)] + [-530, -530]

    # 37 legit multi-event strategies: 60 trades each, consistent edge with
    # real losses (many independent winning events; genuine but unspectacular).
    candidates = []
    for i in range(37):
        pnls = [rng.randint(-400, 700) + (60 if j % 3 == 0 else 0) for j in range(60)]
        candidates.append((i + 1, make_metrics(pnls)))

    fluke_mask = 999
    fluke_m = make_metrics(fluke_pnls)
    candidates.append((fluke_mask, fluke_m))

    print(
        f"fluke metrics: trades={fluke_m.total_trades} pf={fluke_m.profit_factor} "
        f"max_dd_paisa={fluke_m.max_drawdown_paisa} "
        f"sharpe={fluke_m.sharpe_ratio:.4f} "
        f"skew={fluke_m.skewness:.3f} kurt={fluke_m.kurtosis:.3f}"
    )

    disp = pool_sharpe_dispersion([m.sharpe_ratio for _, m in candidates])
    print(
        f"pool: n={len(candidates)} n_finite={disp.n_finite} "
        f"variance={disp.variance:.6f} deflatable={disp.deflatable}"
    )

    cfg = OverfitGateConfig()  # dsr_min=0.95 — the new CLI-default config
    dsr = deflated_sharpe_for(
        fluke_m.sharpe_ratio,
        fluke_m.total_trades,
        fluke_m.skewness,
        fluke_m.kurtosis,
        n_trials=len(candidates),
        trial_sharpe_variance=disp.variance,
        cfg=cfg,
    )
    print(f"fluke DSR vs pool: {dsr}")

    off = rank_combos(candidates, overfit=None)
    on = rank_combos(candidates, overfit=cfg)

    def describe(tag: str, ranked: list) -> list:
        masks = [r.mask for r in ranked]
        pos = masks.index(fluke_mask) + 1 if fluke_mask in masks else None
        print(f"{tag}: survivors={len(ranked)} fluke_rank={pos}")
        return masks

    masks_off = describe("gate OFF (old default)", off)
    masks_on = describe("gate ON  (new default)", on)

    legit_off = [m for m in masks_off if m != fluke_mask]
    legit_on = [m for m in masks_on if m != fluke_mask]
    removed_legit = sorted(set(legit_off) - set(legit_on))
    print(
        f"collateral: legit survivors OFF={len(legit_off)} ON={len(legit_on)} "
        f"removed={removed_legit}"
    )
    for r in on[:3]:
        print(f"  ON top: mask={r.mask} dsr={r.deflated_sharpe}")


if __name__ == "__main__":
    main()
