# Track 2 — Execution Simulator Specification

**Status:** DRAFT 2026-05-16 (operator-requested). Bridges Track 1
brute-force winners to Track 2 real-money option replay.

**Purpose:** For each Track 1 winning combo, simulate the actual
option trade with full cost stack, slippage, and liquidity filtering,
across the entire 21-strike grid per signal (ATM ± 10 strikes). The
option type follows the spot signal direction (LONG-spot → BUY CE;
SHORT-spot → BUY PE), and Track 2 ONLY BUYS options — never writes
(sells short). Produce a ranked leaderboard of `(combo_hash,
strike_offset, direction)` tuples sorted by post-cost DSR.

**Locked decision (DEC-EXEC-008, 2026-05-16):** BUY-only policy.
Track 2 NEVER writes / shorts options. Reasons:
1. Retail-intraday focus — option-writing requires SPAN+ELM margins
   (₹1-2 lakh per lot) that don't fit the strategy profile.
2. Asymmetric risk — short-option max-loss is unbounded; doesn't fit
   the "max loss = premium paid" simplicity of long options.
3. STT structure favors buyer — long-side STT applies only on
   exit-sell of premium; no STT-on-entry. Sell-side STT is paid on
   open, which adds upfront drag for short positions.
4. Cost model already includes `short_call` / `short_put` branches
   in `CALCULATOR_SPEC.md §4` as forward-compat stubs — Track 2 hard-
   codes side ∈ {long_call, long_put} only.

**Scope:** This document is the orchestrator. It does NOT duplicate
the per-trade math (already locked in `CALCULATOR_SPEC.md`), the
slippage model (already locked in `TRACK2_OPTIONS_SPEC.md §9`), the
liquidity filter (already locked in `TRACK2_RISK_MGMT.md §4.3`), or
the strike-grid construction (already locked in
`TRACK2_OPTIONS_SPEC.md §6.6`). It SPECIFIES how all of these
components compose into the execution pipeline.

**Out of scope:**
- Live order placement (Track 2 is replay-only)
- Multi-instrument cross-hedging (each combo evaluated standalone)
- Strategy mask discovery (that is Track 1's Stage 1-3 job)

---

## 1. Relationship to existing specs

```
Track 1 (signal research)                    Track 2 (options realism)
─────────────────────────                    ─────────────────────────
Stage 1A: condition precompute               
Stage 1B: post-prune mask space              
Stage 2: full brute-force on SPOT            
Stage 2.5: Bayesian early stop               
Stage 3: top-K composite filter              
Stage 4: DSR / CPCV / Walk-Forward           
Stage 4.7: Lockbox                           
                                             
                                             ─────────────────────────
                       handoff: top-10 →     Stage 5: EXECUTION SIM
                       combos sorted by         (THIS DOCUMENT)
                       Sharpe + DSR             ↓
                                             Stage 5.1: signal replay
                                             Stage 5.2: strike sweep
                                             Stage 5.3: cost + slippage
                                             Stage 5.4: liquidity filter
                                             Stage 5.5: rank + report
```

Track 1 hands off the top-10 winning strategy masks (after Lockbox
validation). Track 2 then runs the execution simulator on each one.

---

## 2. Pipeline stages

### Stage 5.1 — Signal replay

For each of the top-10 Track 1 strategy masks (output of Lockbox):

```
for combo in track1_top_10:
    for instrument in [NIFTY 50, BANKNIFTY, SENSEX]:
        for timeframe in combo.timeframe:           # single TF per combo
            for trading_day in date_range[2021-01-01 ... today]:
                bars = load_1m_bars(instrument, trading_day)
                for bar in bars[earliest_signal..latest_signal]:
                    if combo.evaluate(bar) is True:
                        emit Signal(
                            entry_bar=bar.next_bar(),
                            direction=combo.direction,  # BULL or BEAR
                            timestamp=bar.close_time,
                        )
```

Where:
- `earliest_signal` = bar "09:15" (per DEC-RISK-012)
- `latest_signal` = bar "15:00" (so entry can fire at "15:01" OPEN)
- `combo.evaluate(bar)` uses ONLY bars 0..N (no look-ahead)

Output: a list of `Signal` events per (combo × instrument × day).

### Stage 5.2 — Strike sweep enumeration

For each Signal event, the simulator enumerates ALL 21 strikes
(BUY-side only per DEC-EXEC-008):

```
for signal in stage_5_1_signals:
    # Option type follows spot signal direction
    if signal.direction == BULL:
        option_type = "CE"     # long call
        side = "long_call"
    else:  # BEAR
        option_type = "PE"     # long put
        side = "long_put"

    atm_strike = round_to_grid_step(spot_at_entry, instrument.step_size)
    for k in range(-10, +11):                       # 21 strikes: ATM-10 to ATM+10
        strike = atm_strike + (k * instrument.step_size)
        candidate = TradeCandidate(
            signal=signal,
            strike=strike,
            strike_offset=k,
            side=side,                              # long_call OR long_put — never short
            option_type=option_type,
            expiry=resolve_nearest_expiry(signal.timestamp, instrument),
        )
        yield candidate
```

Per signal, this emits **21 trade candidates** (one per strike offset
from ATM-10 to ATM+10). All are BUY-side (long-only).

The CE/PE choice is determined by signal direction:
- BULL signal → BUY CE (long call); profits if spot rises above strike + premium
- BEAR signal → BUY PE (long put); profits if spot falls below strike - premium

(Track 2 does NOT mix CE+PE on the same signal — that would be a
spread/straddle, which is out of scope. It also does NOT short any
option — that requires SPAN+ELM margin and changes the risk profile.)

### Stage 5.3 — Per-trade cost + slippage

For each candidate, look up the option's actual minute bars
(Dhan `/v2/charts/rollingoption` data), then compute the round-trip
trade and apply the full cost stack per `CALCULATOR_SPEC.md`:

```
entry_bar_open = load_option_bar(strike, expiry, entry_dt).open
exit_bar_open  = load_option_bar(strike, expiry, exit_dt).open   # forced 15:20

entry_price_with_slippage = apply_slippage(entry_bar_open, side="entry")
exit_price_with_slippage  = apply_slippage(exit_bar_open,  side="exit")

gross_pnl_paisa = compute_gross_pnl(
    entry_price=entry_price_with_slippage,
    exit_price=exit_price_with_slippage,
    side=candidate.side,
    n_lots=1,                                       # normalized at 1 lot
    lot_size=lot_size_history[instrument, entry_dt.date()],
)

cost_breakdown = compute_trade_cost(TradeInput(
    entry_dt_ist=signal.entry_bar.open_dt,
    exit_dt_ist=signal.entry_bar.open_dt.replace(hour=15, minute=20),
    instrument=instrument,
    option_type=candidate.option_type,
    strike=candidate.strike,
    expiry_date=candidate.expiry,
    entry_bar_open_inr=entry_price_with_slippage,
    exit_bar_open_inr=exit_price_with_slippage,
    side=candidate.side,   # always "long_call" or "long_put" per DEC-EXEC-008
    n_lots=1,
    broker="dhan",
    gst_split="intra_state",
    trade_outcome="NORMAL_CLOSE",                   # always for Track 2 per DEC-RISK-011
))

net_pnl_paisa = gross_pnl_paisa - cost_breakdown.total_paisa
```

Slippage model (`TRACK2_OPTIONS_SPEC.md §9`): 1-tick flat per side
(operator-locked 2026-05-16 — honest given 1-min-only data
granularity; no tick-level book data).

### Stage 5.4 — Liquidity filter

Per `TRACK2_RISK_MGMT.md §4.3`, reject any candidate whose target
strike fails:
- EOD open interest at signal-bar < 10,000 contracts, OR
- Average bid-ask spread at signal-bar > 5% of premium

```
for candidate in stage_5_3_candidates:
    oi = load_option_oi(candidate.strike, candidate.expiry, signal.entry_dt)
    spread = load_option_spread(candidate.strike, candidate.expiry, signal.entry_dt)
    premium = entry_bar_open

    if oi < 10_000:
        candidate.reject_reason = "low_oi"
        emit candidate to rejected_ledger
        continue
    if (spread / premium) > 0.05:
        candidate.reject_reason = "wide_spread"
        emit candidate to rejected_ledger
        continue

    emit candidate to accepted_ledger
```

Rejected candidates ARE recorded (for audit + later analysis of "what
would have happened if liquidity had been present"), but they do NOT
contribute to the strategy's net PnL.

### Stage 5.5 — Aggregate + rank

For each `(combo_hash, strike_offset, direction)` tuple, aggregate
across all signals (side is implied — `long_call` if BULL, `long_put` if BEAR):

```
result_row = {
    combo_hash:        combo.hash,
    strike_offset:     k,
    direction:         combo.direction,
    side:              "long_call" if BULL else "long_put",  # per DEC-EXEC-008
    n_trades:          count(accepted),
    n_rejected:        count(rejected),
    n_low_oi:          count(rejected[reason=low_oi]),
    n_wide_spread:     count(rejected[reason=wide_spread]),
    gross_pnl_inr:     sum(gross_pnl),
    total_charges_inr: sum(cost_breakdown.total),
    net_pnl_inr:       sum(net_pnl),
    sharpe:            compute_sharpe(returns),
    sortino:           compute_sortino(returns),
    max_drawdown:      compute_max_dd(equity_curve),
    deflated_sharpe:   compute_dsr(sharpe, n_candidates=42, T=n_trades),
    win_rate:          n_winners / n_trades,
    avg_winning_trade: mean(winners),
    avg_losing_trade:  mean(losers),
    profit_factor:     sum(winners) / abs(sum(losers)),
    held_overnight:    false,                       # always false per DEC-RISK-011
    ...
}
```

Output: `track2_execution_results.parquet` with one row per
`(combo_hash × strike_offset × direction × instrument)`.

Final ranking is per-instrument top-K by Deflated Sharpe Ratio (NOT
raw Sharpe). DSR adjusts for the multiple-testing burden of having
swept 21 candidates per signal × 10 combos = 210 candidate streams
per instrument.

---

## 3. Cardinality budget

For top-10 Track 1 combos:

| Quantity | Value | Notes |
|---|---|---|
| Top-10 Track 1 combos | 10 | output of Lockbox stage |
| Instruments per combo | 3 (NIFTY/BANKNIFTY/SENSEX) | combo may be flagged single-instrument |
| Signals per (combo × instrument × 5yr) | ~500 typical | sparse signal vocabularies |
| Strikes per signal | 21 (ATM ± 10) | per TRACK2_OPTIONS_SPEC §6.6 |
| Sides per strike | 1 (BUY only) | DEC-EXEC-008: never write/short |
| Trade candidates per combo | 10 × 3 × 500 × 21 ≈ 315,000 | gross |
| Rejected by liquidity (estimate 30%) | ~95,000 | varies by far-OTM concentration |
| Accepted trade simulations | ~220,000 | actual cost-stack runs |
| Per-trade cost compute | ~50 µs (Rust) | from CALCULATOR_SPEC bench target |
| Total wall time for Stage 5 | ~11 seconds | parallelizable across instruments |

Stage 5 is **fast** relative to Track 1's Stage 2 brute force (2-4 days).
This is intentional: brute-force happens once on spot, then 21-strike
sweep is a thin overlay.

---

## 4. Per-trade pipeline (composition view)

For each `TradeCandidate`, the simulator runs the following pipeline
deterministically:

```
1. Load entry-bar option OHLC      (Track 2 Parquet lake)
2. Load exit-bar option OHLC       (15:20 forced exit)
3. Apply slippage to entry price   (TRACK2_OPTIONS_SPEC §9: 1-tick flat)
4. Apply slippage to exit price    (TRACK2_OPTIONS_SPEC §9: 1-tick flat)
5. Liquidity filter check          (TRACK2_RISK_MGMT §4.3)
   - OI ≥ 10K AND spread ≤ 5% → ACCEPT
   - else → REJECT + audit-log reason
6. If ACCEPTED:
   a. Compute gross_pnl           (entry_with_slip vs exit_with_slip × lot_size)
   b. Compute cost_breakdown      (CALCULATOR_SPEC §4 — 4-outcome state machine
                                    locked to NORMAL_CLOSE per DEC-RISK-011)
   c. net_pnl = gross_pnl − cost_breakdown.total
   d. emit TradeResult to accepted_ledger
7. If REJECTED:
   a. emit TradeResult to rejected_ledger
      (with reject_reason for diagnostic analysis)
```

Each step is byte-deterministic given the same inputs. The audit
log JSONL captures every step's inputs + outputs for replay.

---

## 5. Cost stack composition (per accepted trade)

Per `CALCULATOR_SPEC.md §4` (locked):

| Cost component | Formula | Cost-of-percent of premium turnover |
|---|---|---|
| Brokerage | min(₹20 + GST, 0.03% × turnover) per leg | ~₹40 round-trip + GST |
| STT | 0.10% sell-side premium (or 0.15% post-Apr-2026 if Finance Act 2026 confirmed) | sell leg only |
| GST | 18% on (brokerage + exch + SEBI + IPFT) | both legs |
| Stamp duty | 0.003% buy-side premium | buy leg only |
| Exchange transaction charge | NSE 0.03503% / BSE 0.03250% premium both sides | both legs |
| SEBI turnover fee | 0.0001% (₹10/cr) premium both sides | both legs |
| IPFT | NSE 0.0005% (₹50/cr) both sides; BSE near-zero | both legs |
| Slippage | 1-tick × 2 legs | both legs |

Total typical cost-as-%-of-gross for NIFTY ATM options: 1-3% of
premium turnover (`COSTS_VERIFIED.md §5` worked examples).

---

## 6. Output schema (Parquet)

```python
@dataclass(frozen=True)
class ExecutionResultRow:
    # identity
    combo_hash:           bytes(32)
    instrument:           Literal["NIFTY", "BANKNIFTY", "SENSEX"]
    timeframe:            str
    strike_offset:        int               # -10 to +10
    direction:            Literal["BULL", "BEAR"]
    option_type:          Literal["CE", "PE"]    # CE if BULL, PE if BEAR
    side:                 Literal["long_call", "long_put"]  # DEC-EXEC-008: BUY only

    # trade counts
    n_signals_fired:      int
    n_trades_accepted:    int
    n_trades_rejected:    int
    n_rejected_low_oi:    int
    n_rejected_spread:    int

    # gross + costs (paisa)
    gross_pnl_paisa:      int
    total_charges_paisa:  int
    slippage_paisa:       int
    net_pnl_paisa:        int

    # risk metrics
    sharpe_ratio:         float
    sortino_ratio:        float
    calmar_ratio:         float
    deflated_sharpe:      float
    max_drawdown_pct:     float
    win_rate:             float
    profit_factor:        float
    avg_winning_trade:    float
    avg_losing_trade:     float

    # constraints
    held_overnight:       bool              # always False (DEC-RISK-011)
    used_circuit_close:   bool              # any trade in this row hit circuit
    
    # provenance
    data_snapshot_hash:   bytes(8)
    code_commit_hash:     bytes(8)
    indicator_version:    str
    cost_calc_version:    str
```

Storage: partitioned by `instrument` and `direction`. Estimated row
count: 10 combos × 3 instruments × 21 offsets × 2 sides = 1,260 rows
per top-10-batch. Trivially small (~100 KB Parquet).

---

## 7. Ranking + reporting

### 7.1 Per-instrument top-K

```
for instrument in [NIFTY, BANKNIFTY, SENSEX]:
    candidates = result_rows.where(instrument=instrument)
    candidates = candidates.where(n_trades_accepted >= MIN_TRADES_THRESHOLD)
                                                      # default 30, configurable
    top_5 = candidates.sort_by(deflated_sharpe, desc=True).head(5)
    emit_report(instrument, top_5)
```

`MIN_TRADES_THRESHOLD` prevents lucky 2-trade outliers from topping
the leaderboard. Default 30 trades per `(combo × strike × side)`
follows the same convention as TRACK1_BACKTEST_SPEC.md §"Stage 1A".

### 7.2 Cross-instrument robustness check

For each candidate, count how many instruments produce positive net
PnL. If a candidate wins on 3/3 instruments at the same (offset, side),
flag as `robust_across_indices`. This is a soft signal of generalizability.

### 7.3 Auditability

Every row in `track2_execution_results.parquet` is reproducible from
`data_snapshot_hash + code_commit_hash + indicator_version + cost_calc_version`.
The audit JSONL log records every individual trade decision, so the
operator can drill from a ranking position back to the specific
signal-bar that triggered the trade.

---

## 8. Property invariants (Phase 1 tests)

| ID | Invariant | Failure mode caught |
|---|---|---|
| P-EXEC-1 | every `n_trades_accepted` row has `held_overnight = False` | code accidentally allows multi-day position |
| P-EXEC-2 | `gross_pnl_paisa = (exit_price - entry_price) × lot_size × sign(side)` | sign error in long-vs-short |
| P-EXEC-3 | `net_pnl_paisa = gross_pnl_paisa - total_charges_paisa - slippage_paisa` | cost stack double-counted or missed |
| P-EXEC-4 | every `n_rejected_low_oi` increment has a matching `oi_at_entry < 10000` in audit log | liquidity filter inconsistent with audit |
| P-EXEC-5 | `n_trades_accepted + n_trades_rejected == n_signals_fired × 1` (per offset) | candidates dropped without record |
| P-EXEC-6 | `cost_breakdown.trade_outcome == "NORMAL_CLOSE"` for every accepted trade | Track 2 accidentally engaged EXPIRY_* branch |
| P-EXEC-7 | `entry_dt.date() == exit_dt.date()` always | intraday lock violated |
| P-EXEC-8 | `side in {"long_call", "long_put"}` for every row | DEC-EXEC-008 BUY-only violated |
| P-EXEC-9 | `(direction == BULL → option_type == CE) AND (direction == BEAR → option_type == PE)` | option type doesn't match spot direction |

---

## 9. Configuration parameters

| Parameter | Default | Range | Notes |
|---|---|---|---|
| `STRIKE_OFFSET_MIN` | -10 | -10 ≤ x ≤ 0 | minimum offset from ATM |
| `STRIKE_OFFSET_MAX` | +10 | 0 ≤ x ≤ +10 | maximum offset from ATM |
| `MIN_TRADES_THRESHOLD` | 30 | ≥ 1 | reject low-n leaderboard entries |
| `LIQUIDITY_OI_MIN` | 10,000 | ≥ 1,000 | EOD OI floor |
| `LIQUIDITY_SPREAD_MAX_PCT` | 5.0 | 1.0-20.0 | bid-ask spread cap as % of premium |
| `SLIPPAGE_TICKS_PER_LEG` | 1 | 0-3 | TRACK2_OPTIONS_SPEC §9 lock |
| `LATEST_ENTRY_TIME` | 15:00:00 IST | 14:00-15:10 | per DEC-RISK-011 |
| `FORCED_EXIT_TIME` | 15:20:00 IST | locked | per DEC-RISK-011 |

---

## 10. Decision IDs (for MIGRATION.md)

| ID | Decision | Rationale |
|---|---|---|
| DEC-EXEC-001 | Sweep all 21 strikes per signal, NOT 10 pre-trade rules | operator decision 2026-05-16: full enumeration > rule-based selection; rule-based picks become post-hoc analysis |
| DEC-EXEC-002 | BUY-only side per strike (REVISED 2026-05-16: was BUY+SELL, now BUY only) | operator decision: Track 2 never writes/shorts options; SPAN+ELM margin out of scope, asymmetric risk profile rejected |
| DEC-EXEC-003 | Liquidity filter applied per-strike, rejected candidates AUDITED | preserves "what-if liquidity" analysis without polluting net PnL |
| DEC-EXEC-004 | Deflated Sharpe Ratio for ranking, NOT raw Sharpe | DSR adjusts for 42 candidates × 10 combos multiple-testing burden |
| DEC-EXEC-005 | MIN_TRADES_THRESHOLD = 30 default | match TRACK1_BACKTEST_SPEC Stage 1A |
| DEC-EXEC-006 | Trade outcome hard-coded NORMAL_CLOSE | DEC-RISK-011 intraday-only lock |
| DEC-EXEC-007 | Strike sweep is POST-Track-1, NOT during brute force | computational discipline; 21-strike sweep happens ONLY for top-10 candidates |
| DEC-EXEC-008 | BUY-only policy — Track 2 never writes/shorts options | operator decision 2026-05-16; ties to retail intraday risk profile; sides ∈ {long_call, long_put} only; cost calculator's short_call/short_put branches are forward-compat stubs only |
| DEC-EXEC-009 | Option type follows spot signal direction strictly (BULL→CE, BEAR→PE) | NO straddles, NO strangles, NO spreads in Track 2; one option type per signal |

---

## 11. Items requiring downstream specs

These are noted here but DO NOT block Stage 5 spec lock:

1. **BSM Greeks pipeline** — needed for delta-target strike selection if
   the 10-rule approach (TRACK2_OPTIONS_SPEC.md §6b) is later promoted
   alongside or instead of the 21-strike sweep. Currently §6b's rules
   8/9/10 require Greeks; this execution simulator uses pure offset.
2. **Option-data backfill** — Track 2 Phase 2 must fill the Parquet
   lake with per-strike per-bar OHLC + OI for all 21-offset strikes
   across 5 years × 3 indices × all expiries. Blocked on Dhan token.
3. **Stage 4.5 walk-forward on Track 2 results** — apply CSCV /
   Romano-Wolf to the (combo × strike × side) leaderboard before
   declaring winners. Reuses TRACK1_BACKTEST_SPEC.md §"Stage 4" math.

---

## 12. UNVERIFIED items

1. **Per-strike historical OI from Dhan API** — confirmed via
   `expired_options_data` endpoint return fields (per CLAUDE.md §6),
   but exact OI granularity (per-bar vs end-of-bar) needs empirical
   probe. Phase 0 task.
2. **Per-strike bid-ask spread historical reconstruction** — Dhan's
   rolling option data does NOT return bid-ask directly. Must derive
   from market depth API, which has different rate limits. Operator
   action: confirm depth API quota for backfill before Stage 5 code-lock.

---

## 13. Sources

- `docs/track2/CALCULATOR_SPEC.md` — per-trade cost math (4-outcome state machine)
- `docs/track2/COSTS_VERIFIED.md` — verified rate table
- `docs/track2/TRACK2_OPTIONS_SPEC.md §6.6` — strike grid
- `docs/track2/TRACK2_OPTIONS_SPEC.md §6b` — 10 pre-trade rules (legacy, now overlay)
- `docs/track2/TRACK2_OPTIONS_SPEC.md §9` — slippage model
- `docs/track2/TRACK2_RISK_MGMT.md §2A` — intraday-only lock
- `docs/track2/TRACK2_RISK_MGMT.md §4.3` — liquidity filter
- `docs/track2/TRACK2_TAX_HANDLING.md` — post-tax PnL pipeline (consumes net_pnl)
- `docs/TRACK1_BACKTEST_SPEC.md §"Stage 4"` — DSR / CPCV math reused here
- `docs/MIGRATION.md` — decision ledger (DEC-RISK-011, DEC-RISK-012)

---

## 14. Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 | Initial draft | Operator decision: post-brute-force strike sweep across all 21 strikes with full cost stack and liquidity filter |
