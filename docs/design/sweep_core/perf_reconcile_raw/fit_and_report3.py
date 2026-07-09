"""Final fit: PINNED (RAYON_NUM_THREADS=1, production config) two-term model
   cost(mask) = c0 + walk_rate*n_bars + trade_cost*n_trades
plus the unpinned control ratio, the disputed-point verdict, and the campaign band."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
pinned = json.loads((HERE / "grid3_pinned_raw.json").read_text())
unpinned = json.loads((HERE / "grid2_unpinned_raw.json").read_text())

p_cells = [c for c in pinned["cells"] if isinstance(c["target"], int)]
u_cells = [c for c in unpinned["cells"] if isinstance(c["target"], int)]
# Capture base: origin/main 665181f3 — sweep_parallel.py:558 at capture (the cite in the
# "config" string below is capture-time provenance and is baked into the frozen
# fit_results_pinned.json bytes — do NOT edit it); the pin lives at :842 as of 95955a71;
# semantics unchanged. (Verifier A round-2.) Comment only — emitted JSON bytes unchanged.
out: dict = {"config": "RAYON_NUM_THREADS=1 (production, sweep_parallel.py:558)"}

TIERS = ("count", "raw", "wrapper")
KEYS = [f"{arm}_{t}" for arm in ("prod", "serial") for t in TIERS]

fits: dict = {}
for key in KEYS:
    A = np.array([[1.0, c["n_bars"], c["trades_per_mask"]] for c in p_cells])
    y = np.array([c[key]["us_best"] for c in p_cells])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    c0, w, tc = (float(v) for v in coef)
    pred = A @ coef
    resid = 100.0 * (pred - y) / y
    ss_res = float(np.sum((pred - y) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    fits[key] = {
        "c0_us": round(c0, 2),
        "walk_rate_ns_per_bar": round(w * 1e3, 4),
        "trade_cost_us_per_trade": round(tc, 4),
        "r2": round(1 - ss_res / ss_tot, 5),
        "rms_resid_pct": round(float(np.sqrt(np.mean(resid**2))), 1),
        "max_abs_resid_pct": round(float(np.max(np.abs(resid))), 1),
        "resid_by_cell": [
            {"n_bars": c["n_bars"], "t": round(c["trades_per_mask"], 1),
             "meas_us": round(float(yy), 1), "resid_pct": round(float(r), 1)}
            for c, yy, r in zip(p_cells, y, resid)
        ],
    }
out["fits_pinned"] = fits

# per-n_bars effective walk rate (intercept at zero trades) — pinned prod tier
per_nb: dict = {}
for nb in sorted({c["n_bars"] for c in p_cells}):
    sub = [c for c in p_cells if c["n_bars"] == nb]
    row = {}
    for key in ("prod_count", "prod_raw", "prod_wrapper"):
        t = np.array([c["trades_per_mask"] for c in sub])
        y = np.array([c[key]["us_best"] for c in sub])
        A2 = np.vstack([np.ones_like(t), t]).T
        (b0, slope), *_ = np.linalg.lstsq(A2, y, rcond=None)
        row[key] = {"walk_ns_per_bar": round(float(b0) * 1e3 / nb, 3),
                    "trade_cost_us_per_trade": round(float(slope), 3)}
    per_nb[str(nb)] = row
out["per_n_bars_pinned_prod"] = per_nb

# unpinned control: rayon speedup on the kernel-only (count) prod tier
ratio = {}
for nb in sorted({c["n_bars"] for c in u_cells}):
    up = [c for c in u_cells if c["n_bars"] == nb and c["target"] == 5][0]
    pp = [c for c in p_cells if c["n_bars"] == nb and c["target"] == 5][0]
    ratio[str(nb)] = round(pp["prod_count"]["us_best"] / up["prod_count"]["us_best"], 2)
out["unpinned_rayon_speedup_kernel_prod_count"] = ratio


def predict(key: str, nb: int, tr: float) -> float:
    f = fits[key]
    return f["c0_us"] + f["walk_rate_ns_per_bar"] / 1e3 * nb + f["trade_cost_us_per_trade"] * tr


# disputed points under the PINNED production model (prod arm = production op)
disp = {}
for tag, nb, tr, claim_us in (
    ("mine: 48,620 bars / 43.6 trades — claimed 62us (1.28 ns/bar), UNPINNED chunk-86", 48_620, 43.6, 62.0),
    ("audit FIX-L: 604,485 bars / 1,423 trades — claimed 7,700us (12.8 ns/bar)", 604_485, 1423.0, 7700.0),
    ("audit FIX-L k=2 median: 604,485 bars / 1,532 trades", 604_485, 1532.0, None),
    ("audit FIX-M: 93,750 bars / ~250 trades — claimed 4.35 ns/bar (~408us)", 93_874, 250.0, 408.0),
):
    row = {"n_bars": nb, "trades": tr, "claimed_us": claim_us}
    for key in ("prod_count", "prod_raw", "prod_wrapper"):
        p = predict(key, nb, tr)
        row[key] = {"pred_us": round(p, 1), "eff_ns_per_bar": round(p * 1e3 / nb, 2)}
    if claim_us:
        row["claim_vs_pred_wrapper_pct"] = round(
            100.0 * (claim_us - predict("prod_wrapper", nb, tr)) / predict("prod_wrapper", nb, tr), 1
        )
    disp[tag] = row
out["disputed_points"] = disp

# campaign band — FIX-L shape (604,485 bars), pinned coefficients.
# current per-combo = pinned wrapper(T) + Python residual (0.40-0.58 ms, given).
# post-port floor: raw kernel (aggressive — per-trade marshal also ported) vs
# wrapper (conservative — ClosedTrade materialisation kept in Python).
camp_rows = []
for label, t in (("zero-trade (7.7% of masks)", 0), ("sparse", 220), ("mid", 950),
                 ("FIX-L k=2 median", 1532)):
    kw = predict("prod_wrapper", 604_485, t) / 1e3
    kr = predict("prod_raw", 604_485, t) / 1e3
    row = {"density": label, "trades": t, "wrapper_ms": round(kw, 2), "raw_ms": round(kr, 2)}
    for r in (0.40, 0.58):
        row[f"speedup_resid{r}_floor_raw"] = round((kw + r) / kr, 2)
        row[f"speedup_resid{r}_floor_wrapper"] = round((kw + r) / kw, 2)
    camp_rows.append(row)
out["campaign_604485_bars"] = camp_rows

(HERE / "fit_results_pinned.json").write_text(json.dumps(out, indent=2))
for key in KEYS:
    f = fits[key]
    print(f"{key:15s} c0={f['c0_us']:8.1f}us walk={f['walk_rate_ns_per_bar']:7.3f}ns/bar "
          f"trade={f['trade_cost_us_per_trade']:6.3f}us/tr R2={f['r2']} rms={f['rms_resid_pct']}%")
print("\nunpinned rayon kernel speedup:", json.dumps(ratio))
print("\nper-n_bars (pinned prod):", json.dumps(per_nb, indent=1))
print("\ndisputed:", json.dumps(disp, indent=1))
print("\ncampaign:", json.dumps(camp_rows, indent=1))
