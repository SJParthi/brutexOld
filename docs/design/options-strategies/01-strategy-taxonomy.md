# 01 — Options-Strategy Taxonomy: the definitive merged family catalogue

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code.** The sweep
> engine remains **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001) and options remain
> **REFERENCE_ONLY**; any activation of an options-strategy engine is a
> **future operator-gated DEC/ADR**.
> **Date:** 2026-07-19.
> **Provenance:** multi-agent deep-research session, docs-only branch
> `claude/options-strategies-design`, deliberately **NO PR** (fleet-wide
> PR-creation freeze); every repo citation is `file:line` at the branch base
> SHA (see `docs/design/options-strategies/README.md`).

This document merges the three taxonomy research parts into one catalogue:

- **Part A (§3)** — directional, covered & spread basics: single-leg
  directionals, covered/secured structures, verticals, synthetics,
  conversions, boxes (A.1–A.17 + cross-cutting notes A.18).
- **Part B (§4)** — volatility, multi-leg & time structures: straddles,
  strangles, guts, condors, butterflies, calendars, diagonals, ratios,
  lizards, straps/strips, trees/ladders (B.1–B.18).
- **Part C (§5)** — dynamic families (delta-neutral short premium, gamma
  scalping, dispersion, 0DTE, event-vol), the adjustment-framework state
  machine, the consolidated Greeks matrix, and **the engine classification
  scheme** — the `family → {side, type, strike-selector, expiry-tier, ratio,
  management}` encoding table.
- **§6** — the consolidated UNVERIFIED register (all three parts, namespaced)
  + web sources + the honest-limits statement.

Scope anchors (repo-verified): the sweep engine admits exactly the 3 spot
indices (`brutex/universe.py:379` `is_engine_sweep_symbol`;
DEC-ENGINE-SPOT-ONLY-001); options are REFERENCE_ONLY, 1-minute-native lake
data (`brutex/data/fno_candle_lake.py:66-70`; DEC-OPTIONS-PREBUILD-RETIRE-001).
The shipped options *translation* executor is single-leg, **BUY-only** by
locked decision: "BULL → buy CE (`long_call`); BEAR → buy PE (`long_put`) —
option type follows the spot direction strictly, no straddles / spreads
(DEC-EXEC-009)" (`brutex/options/executor.py:12-14`) and "Track 2 only ever
BUYS options … never writes / shorts. Max loss = premium paid; no SPAN/ELM
margin modelling" (DEC-EXEC-008, `brutex/options/executor.py:38-41`). Option
**selling** is an explicitly NOT-AUTHORIZED backlog item
(`docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:1-7`,
DEC-PLAN2-OPTION-SELL-BACKLOG-001). Multi-leg structures are explicitly OUT of
the first-sweep scope: "Single-leg long premium (buy CE or buy PE) | Multi-leg
structures (straddles, condors, calendars) for first sweep"
(`docs/track2/TRACK2_OPTIONS_SPEC.md:44`). Every strategy below is therefore
described as a FUTURE, operator-gated engine citizen (its own DEC + ADR +
hostile review); nothing here proposes silent scope expansion.

Sibling docs (cross-linked where relevant): tail risk of the UNDEFINED
families → `08-seller-blowups-and-tail-events.md`; margin/capital/sizing
reality behind every "Margin (India)" cell → `09-capital-margin-and-sizing.md`;
premium-seller edge/execution evidence behind the §5.6 exit conventions →
`10-seller-edge-and-execution-evidence.md`; expiry-day empirics behind §5.4 →
`11-expiry-day-tail-study/STUDY.md`.

**Reference key used throughout.** Part A body text cross-references
strategies as `#N` (= section A.N) and UNVERIFIED tags as `U*n*`
(= register row **A-U*n*** in §6). Part B uses `§N` (= section B.N), `CN-*n*`
(= §1.6) and `U*n*` (= **B-U*n***). Part C uses `§a`…`§i` (= its own
subsections), `G*n*` (= the §5.0 ground-truth table), `ext-*n*` (= §5.8.3) and
`U*n*` (= **C-U*n***). No in-text tag was renamed — the namespacing lives in
the consolidated register so that every original tag resolves.

---

## 1. Conventions, notation & repo ground truth (merged)

### 1.1 Notation — Part A formula set

| Symbol | Meaning |
|---|---|
| `S` | Underlying settlement price at expiry (final settlement price for cash-settled index options; delivery-relevant closing price for stock options) |
| `S0` | Entry price of the underlying leg (cash shares or futures) where the strategy has one |
| `K`, `K1 < K2` | Strike price(s), ₹ per unit |
| `P` | Premium of a single option leg, ₹ per unit (per share / per index point) |
| `D` | Net **debit** per unit paid for a multi-leg structure (`D > 0`) |
| `C` | Net **credit** per unit received for a multi-leg structure (`C > 0`) |
| `N` | Signed net premium per unit (`> 0` = paid / debit, `< 0` = received / credit) — used where a structure can be either |
| `L` | Lot size, units per lot. Every per-unit figure × `L` = per-lot ₹. Lot sizes are DATE-DEPENDENT in India — the repo's verified history (`brutex/options/lot_size_history.py:58-77`): NIFTY 75→50→25→75→65, BANKNIFTY 25→15→30→35→30, SENSEX 10→20 across 2021-2026. Any options backtest MUST size per-date, never with one constant. |
| `π(S)` | Expiry payoff per unit, **gross of all transaction costs, taxes, margin funding and slippage** (costs are owned by the microstructure agents; the repo itself *refuses* to price unverified cost regimes — `brutex/options/executor.py:177-184` `COST_REGIME_UNVERIFIED`) |

### 1.2 Notation — Part B formula set

| Symbol | Meaning |
|---|---|
| `S_T` | Underlying settlement level at front expiry `T` (index options are European, cash-settled — corroborated by `docs/track2/PLAN_FINALIZATION_2026-05-16.md:333`: "stock derivs only; index F&O is cash-settled") |
| `K, K1 < K2 < K3 < K4` | Strikes on the exchange grid — NIFTY 50-pt, BANKNIFTY 100-pt, SENSEX 100-pt steps (`brutex/options/strike_grid.py:26-30`) |
| `C(K)`, `P(K)` | Call / put premium at strike `K`, rupees **per unit of underlying** |
| `w` | Wing / spread width in rupees (a multiple of the strike step) |
| `N_db` / `N_cr` | Net premium **paid** (debit) / **received** (credit) for the whole structure, per unit |
| `(x)⁺` | `max(x, 0)` |
| `L(t)` | Date-keyed lot size — per-lot P&L = per-unit P&L × `L(t)`; lot sizes changed 7+ times 2021-2026 and are looked up per trade date (`brutex/options/lot_size_history.py:1-19`; e.g. NIFTY 65 / BANKNIFTY 30 / SENSEX 20 from Jan-2026, `lot_size_history.py:19`) |
| DTE | Days to (front) expiry |

Symbol-collision note (deliberate, to keep every source formula byte-exact):
in Part A, `C` is a *net credit*; in Part B, `C(K)` is a *call premium*. Each
part's payoff blocks are self-consistent in its own set.

Money convention: a future engine must express every payoff in **int64
paisa** (prices are 2-dp-tick Decimal rupees at the lake boundary —
CLAUDE.md §4, DEC-MONEY-2DP-001). All Part B formulas stay exact in integer
paisa because strikes are integer rupees and premiums are 2-dp ticks; the
only division anywhere is the strap/strip breakeven `N_db/2`, which is exact
in paisa when `N_db` is an even paisa count and otherwise needs a stated
rounding law.

### 1.3 Exercise, settlement, session & margin conventions

- **Exercise style.** All formulas are expiry-exercise (European). NSE index
  options are European; NSE single-stock options are also European-style
  (since the 2011 conversion — UNVERIFIED-CIRCULAR, register item A-U1). No
  early-assignment branch is modeled anywhere in Parts A/B. The repo's Greeks
  machinery treats INDEX as European exact and CASH single stock as an
  American **flagged approximation** (`STYLE_AMERICAN_APPROX`,
  `brutex/data/greeks_enrich.py:31-32`).
- **Settlement.** Index options (NIFTY/BANKNIFTY/SENSEX): **cash-settled**.
  Single-stock options: **physically settled** at expiry (in-the-money ⇒
  give/take delivery of shares — UNVERIFIED-CIRCULAR, A-U2; the settlement
  SPLIT itself is repo-corroborated at
  `docs/track2/PLAN_FINALIZATION_2026-05-16.md:333`). This asymmetry changes
  the *meaning* of "covered" and "secured" and is restated per strategy.
- **No tradable cash underlying for indices.** You cannot buy or short "one
  unit of NIFTY" in cash. Every index structure written with an "underlying"
  leg (covered call, covered put, protective put, collar, conversion,
  synthetic call/put) is in practice **futures-covered** — the underlying leg
  is an index futures position, which carries its own SPAN+exposure margin,
  its own expiry/roll calendar, and a basis (futures ≠ spot) that shifts every
  breakeven by the basis at entry. All `S0`-based formulas then read `S0` =
  the futures entry price and `S` = the futures/settlement convergence price.
- **Expiry instant.** The repo pins the option expiry instant at 15:30 IST
  (`brutex/data/greeks_enrich.py:239-240`, `EXPIRY_HOUR_IST=15`,
  `EXPIRY_MINUTE_IST=30`), equal to the NSE session close.
- **Margin vocabulary (India).** *Long option* = full premium paid upfront,
  no further margin (**PREMIUM-ONLY**). *Short option / futures* = SPAN +
  Exposure margin, collected upfront (**SPAN+EXPOSURE**; peak-margin regime —
  UNVERIFIED-CIRCULAR, A-U5). Recognized hedges (verticals,
  futures-vs-option, calendars) receive a SPAN **spread/offset benefit**
  (**NET-OF-HEDGE**) whose magnitude is deliberately NOT quantified anywhere
  in this document: the repo's own recon found **no sourceable SPAN history**
  and ships a refusal-only margin table (`brutex/engine/margin_history.py:1-18`
  — "Recon found NO sourceable SPAN history anywhere"; every margin-ROI
  verdict renders UNVERIFIED; `docs/track2/TRACK2_RISK_MGMT.md` §2.3 forbids
  hard-coded margin constants per the same docstring). The *calendar-spread
  margin benefit* and its *removal on expiry day* (a SEBI 2024-10-01
  measure-series item) are flagged per-strategy and are UNVERIFIED here
  (B-U5–B-U7). Every margin statement in this document is therefore
  *qualitative*. See `09-capital-margin-and-sizing.md`.
- **Greeks notation.** Sign first, then qualitative magnitude. Δ delta,
  Γ gamma, Θ theta (per-day decay; `+` = position earns from time passing),
  V vega. "ATM ≈ ±0.5Δ" etc. are the standard Black-Scholes facts; no repo
  Greek is cited because the engine computes Greeks only as lake enrichment
  (reference), not strategy state.
- **Risk classes.** `DEFINED` (max loss a fixed formula), `DEFINED-LARGE`
  (bounded only by S→0 — bounded in math, catastrophic in size),
  `UNDEFINED` (loss unbounded as S→∞ — **labelled loudly** every time).
  Part C's matrix (§5.7) spells `DEFINED-LARGE` as "large-defined" —
  synonyms. Tail mechanics of the UNDEFINED families:
  `08-seller-blowups-and-tail-events.md`.

### 1.4 Repo reality check (what exists today vs. what is NEW)

- The ONLY options materialization path in the repo today is the post-sweep
  translation executor, and it is **BUY-only by decree**:
  `brutex/options/executor.py:38-41` — "Track 2 only ever BUYS options
  (`long_call` / `long_put`) — never writes" (DEC-EXEC-008), with the
  direction-strict mapping BULL → buy CE, BEAR → buy PE
  (`brutex/options/executor.py:12-13`).
- Consequently, of the 44 families in the §2 master table, only **A.1 (long
  call)** and **A.2 (long put)** have any existing repo pricing path. Every
  short-premium, multi-leg, or underlying-legged structure is NEW capability
  whose activation requires the future operator-gated DEC/ADR.
- The executor's per-trade honesty gates generalize directly to multi-leg
  strategies and should be treated as the design floor: reject rather than
  fabricate on missing bars (`NO_DATA`), tick-floor-saturated premiums
  (`TICK_FLOOR`), unknown lot size (`LOT_SIZE_UNKNOWN` — lot history starts
  2021-01-01 while the data floor is 2020-01-01), zero-volume bars
  (`STALE_BAR`) and unverified cost regimes (`COST_REGIME_UNVERIFIED`)
  (`brutex/options/executor.py:155-184`).

### 1.5 Verified expiry-regime anchors (used repeatedly in "Indian-market fit")

| Underlying | Weekly today | Monthly | Repo source |
|---|---|---|---|
| NIFTY | **Tuesday** weekly (since 2025-09-02) | Last Tuesday | `docs/track2/TRACK2_OPTIONS_SPEC.md:92` |
| BANKNIFTY | **NONE** — weekly killed after 2024-11-13 (SEBI 2024-10-01 master circular via NSE FAOP/64506) | Last Wednesday → per current regime table | `docs/track2/TRACK2_OPTIONS_SPEC.md:108`; `brutex/options/expiry_calendar.py:18-20` |
| SENSEX | **Thursday** weekly (since 2025-09-04) | Last Thursday | `docs/track2/TRACK2_OPTIONS_SPEC.md:118` |
| Single stocks | No weeklies — **monthly only** (operator premise; B-U1) + **physical settlement** of stock derivatives (`PLAN_FINALIZATION_2026-05-16.md:333`; Groww margin API exposes a `physical_delivery_margin_requirement` field, `docs/groww/trade-api-2026-07-03/05-margin.md:183`) | Monthly | register B-U1 |

### 1.6 Engine-facing notes common to ALL structures (CN-1 … CN-7)

- **CN-1 (leg addressing):** every structure is addressable as a vector of
  `(offset-from-ATM, CE/PE, ±qty, expiry-slot)` legs — the repo already has
  the ATM-rounding + offset grammar (`strike_grid.round_to_atm` /
  `strike_offset_from_atm` / the 21-strike `grid_around_atm(ATM±10)`,
  `brutex/options/strike_grid.py:38-102`) and weekly/monthly expiry
  addressing (`next_weekly_expiry` / `next_monthly_expiry`,
  `brutex/options/expiry_calendar.py:25-33`; weekly may honestly be `None` —
  BANKNIFTY post-Nov-2024, `expiry_calendar.py:18-20`).
- **CN-2 (joint data gate):** a multi-leg trade is priceable only when EVERY
  leg has lake bars at entry AND exit — the per-leg reject vocabulary already
  exists (`RejectReason.NO_DATA` "band-drift hole", `TICK_FLOOR`,
  `LOT_SIZE_UNKNOWN`, `STALE_BAR`, `COST_REGIME_UNVERIFIED` —
  `brutex/options/executor.py:155-184`) and must become an ALL-legs
  conjunction with the whole structure rejected (never a partial fill) when
  any leg gates out.
- **CN-3 (fill law):** the only fill model is worst-case adverse-extreme
  (DEC-FILL-WORST-CASE-ONLY-001, `brutex/options/executor.py:20-28`).
  Extending it per-leg to a multi-leg package is CONSERVATIVE but can be
  jointly infeasible (all legs printing their adverse extremes in the same
  minute may be mutually inconsistent); a future multi-leg DEC must
  consciously choose per-leg-adverse (pessimistic-biased) vs a package-level
  law. Flagged, not decided here.
- **CN-4 (short legs blocked twice):** every short-leg structure below is
  blocked by BOTH the BUY-only decision (DEC-EXEC-008, `executor.py:38-41`)
  AND the absent margin model (`margin_history.py:10-19`) — activation needs
  a selling DEC *and* a margin-data DEC.
- **CN-5 (censoring hazard):** short-premium backtests on lake data inherit
  the winner-censoring hazard recorded for the selling backlog — a sold
  strike drifts away from ATM exactly on winning trades and can exit banded
  coverage mid-trade; "Any backtest MUST censor (never fabricate) trades
  whose exit bars left coverage"
  (`docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:32-36`).
- **CN-6 (session law):** Track-2 forces exit at 15:20 IST while options
  trade to 15:30 (`docs/track2/TRACK2_OPTIONS_SPEC.md:785`) — every intraday
  variant below inherits that law; positional (overnight / hold-to-expiry)
  variants need their own exit-law DEC.
- **CN-7 (run identity):** structure parameters (leg offsets, widths, ratios,
  expiry slots, exit rules) must enter the canonical run-identity params
  exactly as spot params do today (`canonical_trade_params`,
  `brutex/engine/sweep_run.py:719`) so every structure-cell is a
  reproducible, dedupable sweep coordinate (Golden Rule #4).

---

## 2. MASTER SUMMARY TABLE — every family

Legend: `+C`/`−C` long/short call · `+P`/`−P` long/short put · `+U`/`−U`
long/short underlying (shares or futures) · Θ sign at initiation
(ATM-centered) · risk classes per §1.3 · margin classes per §1.3 (magnitudes
UNVERIFIED by design — `brutex/engine/margin_history.py:10-19`; see
`09-capital-margin-and-sizing.md`). "idx" = index-options fit (post-2024
weekly regime, §1.5); "stk" = monthly, physically-settled stock-options fit.
IDs: A* = Part A (§3), B* = Part B (§4), C* = Part C dynamic families (§5),
RR = named only in Part C's matrix/encoding. Premium-flow (debit/credit) and
per-family Γ/V detail live in the per-strategy sections and the consolidated
Greeks matrix (§5.7).

| ID | Strategy family | Legs | Direction | Vol view | Θ | Risk class | Margin nature (India) | Indian fit |
|---|---|---|---|---|---|---|---|---|
| A1 | Long call | +C(K) | Bullish | Long vol | − | DEFINED | Premium only | Only repo-materializable bull structure today (BUY-only executor) |
| A2 | Long put | +P(K) | Bearish | Long vol | − | DEFINED | Premium only | Only repo-materializable bear structure today |
| A3 | Short (naked) call | −C(K) | Bearish/neutral | Short vol | + | **UNDEFINED** | SPAN+Exposure | idx: cash-settled weekly seller staple · stk: assignment ⇒ share delivery |
| A4 | Short (naked) put | −P(K) | Bullish/neutral | Short vol | + | DEFINED-LARGE | SPAN+Exposure | stk ITM expiry ⇒ mandatory take-delivery |
| A5 | Covered call | +U, −C(K) | Mildly bullish | Short vol | + | DEFINED-LARGE (downside) | Shares/futures + SPAN on call | idx version is **futures-covered** (no cash index) |
| A6 | Covered put | −U, −P(K) | Mildly bearish | Short vol | + | **UNDEFINED** (upside) | Futures + SPAN on put | Retail can't short cash overnight ⇒ always futures-legged |
| A7 | Cash-secured put | −P(K) + cash | Bullish/neutral | Short vol | + | DEFINED-LARGE | SPAN+Exposure (no CSP regime) | "Secured" is self-imposed sizing; real only for stocks (delivery) |
| A8 | Protective / married put | +U, +P(K) | Bullish, hedged | Long vol | − | DEFINED | Shares/futures + premium | idx version futures-legged; ≡ synthetic long call |
| A9 | Collar | +U, +P(K1), −C(K2) | Mildly bullish | ≈ flat | ≈ mixed | DEFINED | Shares/futures + SPAN on call (hedge benefit) | Zero-cost variant common on stocks; idx = futures collar |
| A10 | Bull call spread | +C(K1), −C(K2) | Bullish | ≈ flat/long | − (OTM) / + (ITM) | DEFINED | Net debit + SPAN on short leg w/ spread benefit | Liquid on index weeklies; strike-grid sweepable |
| A11 | Bear put spread | +P(K2), −P(K1) | Bearish | ≈ flat/long | − (OTM) / + (ITM) | DEFINED | Net debit + SPAN w/ benefit | Same |
| A12 | Bull put spread | −P(K2), +P(K1) | Bullish/neutral | Short vol | + | DEFINED | SPAN w/ spread benefit | The Indian index credit-seller workhorse |
| A13 | Bear call spread | −C(K1), +C(K2) | Bearish/neutral | Short vol | + | DEFINED | SPAN w/ spread benefit | Same |
| A14 | Synthetic long / short underlying | +C(K)−P(K) / −C(K)+P(K) | Δ≈±1 | ≈ none | ≈ 0 | Long: DEFINED-LARGE · Short: **UNDEFINED** | SPAN on short leg + premium | Futures usually cheaper; used at parity dislocations / ban periods |
| RR | Risk reversal (split-strike synthetic) | −P(K1), +C(K2), K1<K2 | Strongly bullish (Δ ++) | ≈ 0 mixed (V) | ≈ 0 | UNDEFINED-equivalent (downside — like long underlying below the put strike) | SPAN+EXPOSURE on the short put + premium (§1.3 classes) | No dedicated A/B section — classified via §5.7 row 29 + §5.8.4 row 29 |
| A15 | Synthetic call / synthetic put | +U+P / −U+C | Hedged directional | Long vol | − | DEFINED | Underlying leg + premium | ≡ A8 / mirrored; idx = futures-legged |
| A16 | Conversion / reversal | ±U, ∓synthetic | None (locked) | None | ≈ 0 | DEFINED (arb) | Full margins both legs, SPAN offsets | Basis/carry arb; stock physical settlement enables true conversions |
| A17 | Box spread | Bull call spr + bear put spr | None (locked) | None | ≈ 0 | DEFINED (arb) | SPAN-offset residual | European style ⇒ no early-exercise blowup; 4-leg costs usually eat the edge |
| B1 | Long straddle | +C(K) +P(K) | Neutral | Long vol | − | DEFINED (= debit) | PREMIUM-ONLY | idx: high (event days) · stk: medium (IV illiquidity) |
| B2 | Short straddle | −C(K) −P(K) | Neutral (short move) | Short vol | + | **UNDEFINED both sides** | SPAN+EXPOSURE | idx: high (expiry-day pin) — gated · stk: low (delivery + gap risk) |
| B3 | Long strangle | +P(K_p) +C(K_c) | Neutral | Long vol | − | DEFINED (= debit) | PREMIUM-ONLY | idx: high · stk: medium |
| B4 | Short strangle | −P(K_p) −C(K_c) | Neutral | Short vol | + | **UNDEFINED both sides** | SPAN+EXPOSURE | idx: high — gated · stk: low |
| B5 | Guts (long/short) | ITM C(K1) + ITM P(K2), K1<K2 | Neutral | Long/short vol | −/+ | Long DEFINED / short **UNDEFINED** | PREMIUM-ONLY / SPAN | idx: low (ITM spreads wide) · stk: very low |
| B6 | Iron condor | +P(K1) −P(K2) −C(K3) +C(K4) | Neutral | Short vol | + | DEFINED (width − credit) | NET-OF-HEDGE | idx: very high (weekly staple) · stk: low-medium |
| B7 | Iron butterfly | +P(K−w) −P(K) −C(K) +C(K+w) | Neutral (pin K) | Short vol | + | DEFINED (w − credit) | NET-OF-HEDGE | idx: very high (expiry pin) · stk: low-medium |
| B8 | Call / put butterfly | +1/−2/+1 same-type | Pin K2 | Short vol (inside wings) | + near K2 | DEFINED (= debit) | NET-OF-HEDGE (long fly ≈ debit) | idx: high (cheap pin bets) · stk: medium |
| B9 | Broken-wing butterfly | +1/−2/+1, unequal wings | Mildly directional | Short vol | + | DEFINED (wide-wing excess − credit) | NET-OF-HEDGE | idx: high · stk: low-medium |
| B10 | Call / put condor | +1/−1/−1/+1 same-type | Range | Short vol (inside) | + inside | DEFINED (= debit) | NET-OF-HEDGE | idx: medium · stk: medium |
| B11 | Calendar (call or put) | −front K, +back K | Neutral, pin K | Long back-month vega | + (front decays faster) | DEFINED ≈ debit (long); reverse **UNDEFINED** after T1 | NET-OF-HEDGE (benefit removed expiry day — B-U6) | idx: high (weekly ladders NIFTY/SENSEX) · stk: low (monthly-over-monthly only) |
| B12 | Diagonal | −front K2, +back K1 (K1≠K2) | Directional + carry | Long vol | + | DEFINED when width ≥ 0 arranged; else residual width risk | NET-OF-HEDGE (B-U6) | idx: high · stk: low-medium |
| B13 | Double calendar / double diagonal | put-side + call-side calendars/diagonals | Neutral range | Long vol | + | DEFINED ≈ debit (long form) | NET-OF-HEDGE (B-U6) | idx: medium-high · stk: low |
| B14 | Ratio front-spread (call/put) | +1 near, −2 far | Directional into short strike | Short vol | + | Call: **UNDEFINED up**; Put: to-zero (practically UNDEFINED) | SPAN+EXPOSURE (naked excess short) | idx: medium — gated · stk: very low |
| B15 | Backspread (call/put) | −1 near, +2 far | Strong directional + long vol | Long vol | − | DEFINED (valley = width − credit); unlimited profit side | SPAN+EXPOSURE (short leg) but net-long-gamma | idx: medium · stk: low |
| B16 | Jade lizard / big lizard | −P(K1) −C(K2) +C(K3) [big: straddle+wing] | Mildly bullish | Short vol | + | No upside risk iff `N_cr > K3−K2`; **downside UNDEFINED-in-practice** | SPAN+EXPOSURE | idx: medium — gated · stk: very low |
| B17 | Strap / strip | +2C+1P / +1C+2P at K | Vol + directional tilt | Long vol | − | DEFINED (= debit) | PREMIUM-ONLY | idx: medium · stk: medium |
| B18 | Christmas tree / ladder | 1/−3/+2 tree; +1/−1/−1 ladder | Range→directional | Short vol | + | Tree DEFINED; **ladder UNDEFINED** on the naked side | NET-OF-HEDGE / SPAN | idx: medium — gated (ladder) · stk: very low (ladder) |
| C1 | Delta-hedged short straddle | B2 legs + futures/option hedge overlay | Neutral (managed) | Short vol | ++ | **UNDEFINED** (gap through band) | SPAN+EXPOSURE; margin-ROI uncomputable today (G19) | idx weekly/monthly; double-gated (ext-4/ext-8) |
| C2 | Delta-hedged short strangle | B4 legs + hedge overlay | Neutral (managed) | Short vol | + | **UNDEFINED** (gap) | SPAN+EXPOSURE | as C1 |
| C3 | Gamma-scalping long book | B1/B3 legs + re-hedge overlay | Neutral (managed) | Long vol | −− | DEFINED (debit + hedge slippage) | PREMIUM-ONLY book + futures margin on hedge leg | Data-feasible on the lake today; IV entry gate needs a sanctioned series (ext-9) |
| C4 | Dispersion composite | index vol book vs constituent vol books | Neutral | Short implied correlation | mixed | Complex (leg-level UNDEFINED possible) | Margin-intensive; unmodeled (G19) | Classify-only; NOT a first-generation activation target (§5.3) |
| C5 | 0DTE short straddle/strangle | B2/B4 at DTE=0 | Neutral | Short vol | +++ (per hour) | **UNDEFINED** intraday (until square-off) | SPAN+EXPOSURE + expiry-day +2% ELM (2024-11-20) | Venue = NIFTY Tue / SENSEX Thu only post-2024-11-20; BANKNIFTY monthly (§5.4.3) |
| C6 | 0DTE long gamma ("lottery") | far-OTM +C or +P, DTE=0 | Directional | Long vol | −−− | DEFINED (= debit) | PREMIUM-ONLY | Same venue map; expected-value negative most days |
| C7 | Pre-event long vol | B1/B3 spanning the event | Neutral | Long vol | − | DEFINED (= debit) | PREMIUM-ONLY | Only 4 event classes calendar-verifiable in-repo; the rest need ext-10 |
| C8 | Post-event IV-crush short | B2/B4/B7 at the event | Neutral | Short vol | + | **UNDEFINED** unless winged (width if winged) | SPAN+EXPOSURE | Same event-calendar constraint; gated (ext-4) |

Reading guide: parity-equivalent pairs (A5≡A4-shape, A6≡A3-shape,
A10≡A12-shape, A11≡A13-shape, A8≡A1-shape@K, B10≡B6-shape, B5≡B3+box) are
called out per section — the sweep space must not silently double, and risk
flags must propagate to the equivalent form (A.18).

---

## 3. Part A — Directional, covered & spread basics (A.1–A.17)

Single-leg directionals, covered/secured structures, verticals, synthetics,
conversions and boxes. Formulas use the §1.1 symbol set; `#N` = section A.N;
`U*n*` = register row A-U*n* (§6).

### A.1 Long call

**Legs.** Buy 1 call at strike `K`, pay premium `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = max(S − K, 0) − P`
- Max profit: **unbounded** (grows 1:1 above `K + P`)
- Max loss: `P` (for all `S ≤ K`) → per lot `P·L`
- Breakeven: `S* = K + P`

**Typical entry logic.** Directional bullish signal with a defined horizon;
or a long-vol view (buy IV believed cheap). Strike selection is the key
parameter: ITM (Δ≈0.7+) behaves stock-like with lower theta-bleed per delta;
ATM (Δ≈0.5) maximizes gamma per premium; OTM (Δ≈0.2-0.3) is convex
lottery-shaped with the worst theta/win-rate. Expiry selection trades theta
(short-dated bleeds fastest) against premium outlay.

**Typical exit logic.** (a) Premium target (e.g., 50-100% gain on premium);
(b) underlying target hit → close, don't hold to expiry; (c) time stop —
exit before the final-days theta cliff if the move hasn't come; (d) signal
invalidation (spot back below the entry trigger). The repo's Track-1
translation prices exactly an entry bar → 15:20-IST forced-exit bar
round-trip (`brutex/options/executor.py:15`), i.e., intraday option holding —
a legitimate but very narrow special case of this exit family.

**Common adjustments.** Roll up (sell the appreciated call, buy a higher
strike — banks gains, keeps exposure); roll out (later expiry — pays more
theta budget); leg into a bull call spread by selling a higher-strike call
against it (cuts residual cost basis, caps upside); convert to a synthetic
straddle by adding a put after a large run (rare, vol-view dependent).

**Greeks at entry.** Δ **+** (0→1 by moneyness; ≈+0.5 ATM) · Γ **+** (largest
ATM and near expiry) · Θ **−** (accelerates into expiry; worst held ATM) ·
V **+** (largest ATM, larger for longer-dated).

**Margin (India).** Premium only, paid upfront. No SPAN, no exposure margin,
no MTM calls. This is what makes long options the only *capital-light*
directional instrument in Indian F&O.

**Risk class.** DEFINED (premium).

**Indian-market fit.**
- Index (NIFTY/BANKNIFTY/SENSEX): the most liquid option market in the world
  by contract count; weekly expiries make short-dated long calls cheap in ₹
  but brutal in theta. Cash-settled — no delivery consequence to holding ITM
  through expiry (settlement of the intrinsic in cash).
- Single stock: liquidity is concentrated in the front month near ATM; wide
  spreads away from ATM make the *executed* breakeven materially worse than
  the formula. Physical settlement: an ITM long call carried to expiry
  becomes a **take-delivery obligation** (funded purchase of `L` shares at
  `K`) — a buyer who cannot fund delivery must exit before expiry
  (UNVERIFIED-DETAIL on broker auto-square-off practice, U3).
- Repo: this is one of the exactly two structures the current BUY-only
  executor can already materialize (`brutex/options/executor.py:12-13,38-41`).

**Future sweep axes (design-readiness).** strike offset (moneyness or Δ
target) × expiry tier (weekly/next/monthly — mirrors the repo's
`WEEKLY/MONTHLY` expiry enum, `brutex/options/executor.py:151-152`) × entry
signal family × exit rule (premium multiple / time stop / forced EOD).

### A.2 Long put

**Legs.** Buy 1 put at strike `K`, pay premium `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = max(K − S, 0) − P`
- Max profit: `K − P` per unit, attained at `S = 0` (index: notional bound;
  practically "very large") → per lot `(K − P)·L`
- Max loss: `P` (for all `S ≥ K`) → per lot `P·L`
- Breakeven: `S* = K − P`

**Typical entry logic.** Directional bearish signal; portfolio hedge sizing
(see #8 for the attached version); long-vol crash convexity (far-OTM puts).
Same strike/expiry trade-offs as #1, mirrored. Note the volatility skew:
Indian index puts typically carry higher IV than symmetric-moneyness calls
(put skew), so the bear case pays a structurally higher premium
(UNVERIFIED-MAGNITUDE — skew level is an empirical, dataset-derivable fact).

**Typical exit logic.** As #1 mirrored: premium multiple, underlying target,
time stop, signal invalidation. Vol-crush awareness: a fast down-move spikes
IV — exits into the spike capture vega profit that evaporates if held.

**Common adjustments.** Roll down-and-out; leg into a bear put spread
(sell lower-strike put); calendarize (roll the long leg out, finance with a
short near-dated put — becomes a diagonal, B.12 territory).

**Greeks at entry.** Δ **−** (0→−1; ≈−0.5 ATM) · Γ **+** · Θ **−** ·
V **+** (all magnitudes as #1).

**Margin (India).** Premium only.

**Risk class.** DEFINED (premium).

**Indian-market fit.**
- Index: the natural hedge instrument; cash settlement makes expiry
  mechanics trivial. Weekly granularity allows precise event hedging
  (budget day, RBI policy, elections).
- Stock: an ITM long put carried to expiry is a **give-delivery** obligation
  (deliver `L` shares at `K`) — a put buyer without the shares must either
  exit pre-expiry or fund/borrow the delivery (UNVERIFIED-DETAIL, U3);
  this materially changes "hold to expiry" economics vs. the cash-settled
  index case.
- Repo: the second of the two currently-materializable structures
  (BEAR → buy PE, `brutex/options/executor.py:12-13`).

**Future sweep axes.** As #1 mirrored; plus hedged-vs-naked flag when
combined with an underlying leg (then it is #8, a distinct taxon).

### A.3 Short (naked) call — **UNDEFINED RISK**

**Legs.** Sell 1 call at strike `K`, receive premium `P`. No underlying, no
hedge leg.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = P − max(S − K, 0)`
- Max profit: `P` (for all `S ≤ K`) → per lot `P·L`
- Max loss: **UNBOUNDED** as `S → ∞`. There is no formula-bounded worst case.
  **This is an undefined-risk structure and must be labelled as such in any
  future engine artifact, ranking surface, and margin display.**
- Breakeven: `S* = K + P`

**Typical entry logic.** Neutral-to-bearish view with an overhead level
(resistance, prior high, round number) beyond `K`; rich implied volatility
(sell IV believed expensive vs. realized); systematic premium harvesting at
a fixed short delta (e.g., sell the 0.15-0.25Δ call at a fixed
days-to-expiry). Entry quality is dominated by IV level + distance-to-strike
in σ units, not by premium in ₹.

**Typical exit logic.** (a) Buy back at a fixed fraction of premium captured
(e.g., 50-80% of max profit); (b) hard stop at a premium multiple (e.g.,
close when the option trades at 2-3× credit received); (c) delta breach
(short strike Δ exceeding a bound, e.g., 0.40); (d) never carry an ITM naked
short into the final session (gamma is extreme).

**Common adjustments.** Roll up-and-out (buy back, re-sell higher strike /
later expiry — collects more premium, extends risk duration); **convert to a
defined-risk bear call spread** by buying a further-OTM call (caps the
unbounded tail — the single most important adjustment, because it changes
the risk *class*); delta-hedge with index futures (turns it into a
covered-call-like profile from the other side); add a short put to make a
strangle (B.4 — increases premium, adds downside risk).

**Greeks at entry.** Δ **−** (small if OTM, e.g., −0.2) · Γ **−** (grows
violently as spot approaches `K` near expiry) · Θ **+** (the carry engine of
the trade) · V **−** (short vega — an IV spike loses even without spot
movement).

**Margin (India).** SPAN + Exposure, upfront — the heaviest per-unit margin
class in this part; order of magnitude is a double-digit percent of contract
notional, but the exact number is a per-day exchange output (the repo holds
NO sourced SPAN archive — `brutex/engine/margin_history.py:12-18` — so no
figure is quoted). Margin *expands* as the position moves against you
(SPAN re-computes on the moved spot/IV), which is the practical ruin
mechanism: forced buy-ins at the worst prices.

**Risk class.** **UNDEFINED** (upside-unbounded).

**Indian-market fit.**
- Index: cash-settled, European — the dominant Indian seller product;
  short weekly index calls are a retail-and-prop staple. No delivery risk;
  the tail risk is price gap + IV spike (e.g., election-result gaps).
- Stock: physical settlement makes an ITM short call at expiry a
  **give-delivery** obligation (deliver `L` shares); a naked short-call
  writer without shares faces auction/penalty mechanics if unable to deliver
  (UNVERIFIED-DETAIL, U2/U3). MWPL ban periods can freeze fresh positions in
  a stock's derivatives (UNVERIFIED-DETAIL, U7). Expiry-week delivery
  margins ramp on stock short options (UNVERIFIED-DETAIL, U4).
- Repo: NOT materializable today (BUY-only decree, DEC-EXEC-008,
  `brutex/options/executor.py:38-41`). Enabling ANY short-premium strategy
  is precisely the future DEC/ADR boundary.

**Future sweep axes.** short-strike Δ target × DTE at entry × profit-take
fraction × stop multiple × (index-only vs stock) — with the risk-class flag
carried as a first-class artifact field so undefined-risk results can never
silently rank beside defined-risk ones.

### A.4 Short (naked) put

**Legs.** Sell 1 put at strike `K`, receive premium `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = P − max(K − S, 0)`
- Max profit: `P` (for all `S ≥ K`) → per lot `P·L`
- Max loss: `K − P` per unit at `S = 0` → per lot `(K − P)·L`. Bounded in
  math, catastrophic in size — **DEFINED-LARGE**, not "safe".
- Breakeven: `S* = K − P`

**Typical entry logic.** Bullish-to-neutral; support below; rich IV
(especially put-skew richness); systematic short-Δ put selling at fixed DTE;
"acquire the stock cheaper" intent (the stock version — see #7 for the
secured form). Post-crash IV spikes are the classic entry (sell fear).

**Typical exit logic.** As #3 mirrored: premium-fraction take, premium
multiple stop, delta breach, never carry ITM into the last session (for
stocks additionally because of delivery).

**Common adjustments.** Roll down-and-out; convert to bull put spread by
buying a lower-strike put (defines risk); take assignment intentionally
(stock version) and switch to covered-call management (#5) — the "wheel";
delta-hedge with short futures on breach.

**Greeks at entry.** Δ **+** (e.g., +0.2 for a 0.2Δ short put) · Γ **−** ·
Θ **+** · V **−** (short puts are the most vega-negative-feeling trade in a
crash: spot down, IV up, both against you simultaneously).

**Margin (India).** SPAN + Exposure, upfront; same class as #3 (no
"cash-secured" discount exists in the Indian margin regime — see #7).
Figures deliberately unquoted (no sourced SPAN archive,
`brutex/engine/margin_history.py:12-18`).

**Risk class.** DEFINED-LARGE (bounded only by `S → 0`).

**Indian-market fit.**
- Index: cash-settled; the short put is the standard bullish-carry seller
  trade; crash gaps are the tail (the payoff is short a crash).
- Stock: an ITM short put at expiry ⇒ **mandatory take-delivery** — buy `L`
  shares at `K`, requiring full funding; expiry-week delivery margin ramp
  applies (UNVERIFIED-DETAIL, U4). This is where "naked" vs "secured" (#7)
  stops being a labeling nicety and becomes a funding fact.
- Repo: not materializable today (BUY-only decree).

**Future sweep axes.** as #3 mirrored + assignment-intent flag (stock).

### A.5 Covered call

**Legs.** Long underlying at `S0` (stock: `L` shares; index: futures — see
Indian fit) + short 1 call at `K ≥ S0` typically, receive premium `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = (S − S0) + P − max(S − K, 0)`
- Max profit: `(K − S0) + P` (for all `S ≥ K`) → per lot `((K − S0) + P)·L`
- Max loss: `S0 − P` per unit at `S = 0` → per lot `(S0 − P)·L` —
  **DEFINED-LARGE**: the full downside of the underlying, cushioned only by
  the premium.
- Breakeven: `S* = S0 − P`
- Parity note: expiry payoff is identical in shape to a **short put at K**
  (plus carry/basis differences) — anyone ranking covered calls beside naked
  puts should expect near-identical P&L distributions.

**Typical entry logic.** Own (or want to own) the underlying; neutral to
mildly bullish view; harvest premium against the holding. Strike: OTM
0.2-0.4Δ balances income vs. upside surrender; ATM maximizes income and is
really a synthetic short put. Systematic monthly/weekly overwriting is the
classic institutional program.

**Typical exit logic.** Let the call expire worthless and re-sell (the
income loop); buy back the call early at a high captured fraction; if
assignment/called-away is unwanted, roll before expiry; unwind both legs on
thesis change.

**Common adjustments.** Roll up (spot rallies through `K`: buy back, sell
higher `K`, usually out in time — pays to keep upside); roll down (spot
falls: re-strike lower for more premium — but this caps the recovery, the
classic covered-call trap); convert to collar by buying a put (#9); let
shares be called away (stock version) as a disciplined exit.

**Greeks at entry.** Net Δ **+** (`1 − Δ_call` ≈ +0.6..0.8 for an OTM call) ·
Γ **−** (short the call's gamma) · Θ **+** · V **−**.

**Margin (India).**
- Stock version: shares held in demat (fully paid, CNC) + SPAN+Exposure on
  the short call. Shares can be pledged as collateral toward that margin
  (subject to haircut and the cash-vs-collateral funding split —
  UNVERIFIED-DETAIL, U8); the exchange does NOT automatically treat held
  shares as margin-offsetting cover the way a US "covered" designation does
  (UNVERIFIED-DETAIL, U8).
- Index version: **futures-covered** — long index futures (SPAN+Exposure) +
  short call (SPAN+Exposure) with a SPAN offset for the hedged combination
  (magnitude UNVERIFIED, U6).

**Risk class.** DEFINED-LARGE (full underlying downside minus premium).

**Indian-market fit.**
- **Index: there is no tradable cash underlying — a NIFTY/BANKNIFTY/SENSEX
  "covered call" is actually long index FUTURES + short call.** That changes
  three things vs. the textbook: (a) both legs are margined; (b) the futures
  leg has its own expiry/roll (calendar basis risk if the call and future
  expire on different dates); (c) `S0` is the futures price, so the
  breakeven embeds the basis at entry.
- Stock: the genuinely "covered" case — physical settlement means an
  assigned ITM short call at expiry settles by **delivering the held
  shares**, which is exactly what the structure intends. Off-expiry the
  shares still must margin-fund the short call as above.
- Repo: not materializable (needs a short leg + an underlying leg — both
  outside today's BUY-only, options-only executor).

**Future sweep axes.** call Δ/moneyness × expiry tier × roll trigger
(days-before-expiry / delta breach) × stock-vs-futures-covered flag; the
futures-covered variant additionally sweeps the futures roll rule.

### A.6 Covered put (short underlying + short put) — **UNDEFINED RISK**

**Legs.** Short underlying at `S0` (in India, in practice: short FUTURES —
see Indian fit) + short 1 put at `K ≤ S0` typically, receive premium `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = (S0 − S) + P − max(K − S, 0)`
- For `S ≤ K`: `π = (S0 − K) + P` (constant) → Max profit
  `((S0 − K) + P)·L` per lot
- For `S > K`: `π = (S0 − S) + P`, declining 1:1 — **UNBOUNDED loss as
  `S → ∞`. UNDEFINED RISK — the mirror of the covered call is NOT
  risk-mirrored: it is strictly worse-classed.**
- Breakeven: `S* = S0 + P`
- Parity note: expiry payoff shape ≡ **naked short call at K**.

**Typical entry logic.** Bearish-to-neutral; expecting a grind down to `K`
but not a crash; harvest put premium against a short futures position.
Rare as a standalone retail strategy; more often the intermediate state of
a hedging/rolling program.

**Typical exit logic.** Cover both legs at target; buy back the put when
mostly decayed and manage the bare short future; hard stop above `S0 + P`
(the naked-call-equivalent tail must be stopped, not hoped against).

**Common adjustments.** Buy a further-OTM call to cap the upside tail
(converts the risk class to DEFINED — structurally becomes a
collar-mirror); roll the put down as spot falls; close the futures leg on
upside breach (leaves a naked short put, #4 — a *class change* the operator
must see).

**Greeks at entry.** Net Δ **−** (`−1 + Δ_put_short` where the short put
contributes +Δ; net ≈ −0.6..−0.8) · Γ **−** · Θ **+** · V **−**.

**Margin (India).** Short futures (SPAN+Exposure) + short put
(SPAN+Exposure) with hedge offset (UNVERIFIED magnitude, U6). Both legs
MTM daily.

**Risk class.** **UNDEFINED** (upside-unbounded).

**Indian-market fit.**
- **Retail cannot short cash equity overnight** (intraday-only short selling;
  the SLB mechanism exists but is thin and operationally niche —
  UNVERIFIED-DETAIL, U9). Therefore the Indian covered put is effectively
  ALWAYS futures-legged, for stocks and (necessarily) for indices.
- Index: short futures + short put, cash-settled, no delivery mechanics.
- Stock: short futures + short put; if the put finishes ITM the writer
  takes delivery of shares (physically settled) which then offsets the
  short futures delivery obligation in the same settlement — the netting
  mechanics are exchange-defined (UNVERIFIED-DETAIL, U2/U4).
- Repo: not materializable today.

**Future sweep axes.** as #5 mirrored + a mandatory undefined-risk artifact
flag.

### A.7 Cash-secured put

**Legs.** Sell 1 put at strike `K`, receive premium `P`; simultaneously
earmark cash ≈ `K·L` (or `(K − P)·L`) to absorb the worst case / fund
delivery. **The payoff is identical to #4 (naked short put)** — the
"secured" part is capital discipline, not a different contract.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = P − max(K − S, 0)` (as #4)
- Max profit `P·L`; max loss `(K − P)·L` at `S = 0`; breakeven `K − P`.
- The return-on-capital differs from #4 only because the denominator is the
  self-imposed `K·L` cash reserve rather than the (much smaller) exchange
  margin — this matters enormously for honest ROI ranking in any future
  engine: the SAME trade shows a 3-10× different "return" depending on
  which denominator is used (margin-true vs cash-secured). The repo already
  refuses to fabricate the margin denominator
  (`brutex/engine/margin_history.py:15-18` — refusal-only SPAN table).

**Typical entry logic.** Stock version: "I want to own this stock at `K`,
paid to wait" — sell the put at the acquisition price, fully funded.
Index version: conservative premium harvesting with a hard self-imposed
notional cap (see Indian fit — "delivery" never occurs on index).

**Typical exit logic.** Expire worthless and repeat; buy back at high
captured fraction; on stock assignment, take delivery and roll into
covered-call management (#5) — the "wheel" cycle.

**Common adjustments.** Roll down-and-out for credit; accept assignment
(stock); convert to bull put spread if the reserve must be freed (changes
class from DEFINED-LARGE to DEFINED).

**Greeks at entry.** Identical to #4: Δ **+**, Γ **−**, Θ **+**, V **−**.

**Margin (India).** **There is no distinct cash-secured-put margin regime in
India.** The exchange charges SPAN + Exposure on the short put exactly as
for #4, regardless of any cash set aside (UNVERIFIED-DETAIL on broker-level
treatment, U8). The "security" is entirely self-imposed sizing. In the
stock case the reserve becomes real at expiry: ITM assignment ⇒ mandatory
take-delivery of `L` shares at `K`, requiring ≈`K·L` of settlement funding,
with expiry-week delivery-margin ramps (UNVERIFIED-DETAIL, U4).

**Risk class.** DEFINED-LARGE (identical to #4; the class is a property of
the payoff, not the wallet).

**Indian-market fit.**
- Index: cash-settled — "taking delivery at K" never happens; a
  cash-secured NIFTY put is just a fully-funded naked put. The label adds
  discipline, not mechanics.
- Stock: the label is REAL — physical settlement makes the reserve the
  delivery funding. This is the honest home of the strategy in India.
- Repo: not materializable today (short leg).

**Future sweep axes.** as #4 + ROI-denominator mode as an explicit,
artifact-stamped choice (cash-secured vs margin-true vs notional) — never a
silent default, because it re-ranks everything.

### A.8 Protective put / married put

**Legs.** Long underlying at `S0` + long 1 put at `K ≤ S0` typically, pay
premium `P`. ("Married" = bought simultaneously with the underlying;
"protective" = added to an existing holding; identical payoff.)

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = (S − S0) − P + max(K − S, 0)`
- For `S ≤ K`: `π = (K − S0) − P` (constant floor) → Max loss
  `((S0 − K) + P)·L` per lot (assuming `K ≤ S0`; if `K > S0` the "loss"
  floor can even be a locked gain minus premium)
- Max profit: **unbounded** (upside 1:1 minus the premium)
- Breakeven: `S* = S0 + P`
- Parity note: expiry payoff ≡ **long call at K** (this IS the synthetic
  call of #15) plus carry/basis differences.

**Typical entry logic.** Own the underlying, want a hard floor: event hedge
(results day, budget, elections), portfolio insurance, concentrated-position
protection. Strike = the floor you can tolerate (5-10% OTM typical);
expiry = the event horizon. Cost is the drag: continuous protection at
put-skew IVs is expensive (the "insurance premium bleed").

**Typical exit logic.** Hedge expires with the event; sell the put after the
event passes (vol-crush aware); if spot crashes, either sell the
appreciated put (monetize, keep shares) or exercise/settle (exit whole
position at the floor).

**Common adjustments.** Roll the put down after a rally (re-strike higher
floor — locking gains); spread the cost by selling a lower-strike put
(→ put-spread collar variant); sell a call against it (→ collar #9);
calendarize the protection (longer-dated put, cheaper per day).

**Greeks at entry.** Net Δ **+** (`1 + Δ_put`, Δ_put<0, so ≈ +0.6..0.9) ·
Γ **+** · Θ **−** (the insurance bleed) · V **+**.

**Margin (India).** Stock: fully-paid shares + put premium; no short leg, no
SPAN. Index: **futures-legged** (long futures SPAN+Exposure + put premium;
SPAN hedge offset for the pair — UNVERIFIED magnitude, U6).

**Risk class.** DEFINED (floored at `(S0 − K) + P` per unit).

**Indian-market fit.**
- **Index: no cash underlying — the "married" leg is index futures**, so the
  floor formula runs off the futures entry price and the pair carries
  futures roll/basis risk; a common institutional variant instead holds the
  cash *portfolio* (beta ≈ index) and buys index puts as the overlay —
  cross-hedge basis (portfolio ≠ index) then replaces futures basis.
- Stock: clean fit; ITM put at expiry give-delivers the held shares
  (physical settlement) — the floor executes as an actual exit at `K`.
- Repo: not materializable (underlying leg).

**Future sweep axes.** floor distance (K/S0) × expiry tier × roll rule ×
hedged-fraction (0-100% of the underlying).

### A.9 Collar

**Legs.** Long underlying at `S0` + long put `K1` + short call `K2`, with
`K1 < S0 < K2` typically. Net premium `N = P_put − P_call` per unit
(`N > 0` debit; `N < 0` credit; the marketed "zero-cost collar" tunes
`K1, K2` so `N ≈ 0`).

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = (S − S0) − N + max(K1 − S, 0) − max(S − K2, 0)`
- For `S ≤ K1`: `π = (K1 − S0) − N` → Max loss `((S0 − K1) + N)·L`
- For `S ≥ K2`: `π = (K2 − S0) − N` → Max profit `((K2 − S0) − N)·L`
- Breakeven: `S* = S0 + N` (valid while `K1 ≤ S0 + N ≤ K2`; a large-credit
  collar with `S0 + N < K1` has a locked profit floor — no loss region)
- Parity note: collar ≡ bull call spread `(K1, K2)` in expiry shape (long
  underlying + put K1 ≡ long call K1).

**Typical entry logic.** Protect a holding at near-zero premium outlay:
accept an upside cap `K2` to finance the downside floor `K1`. Standard for
concentrated stock positions, ESOP hangovers, and pre-event index hedging
where paying full put premium (#8) is unpalatable.

**Typical exit logic.** Expiry-managed (re-collar each cycle); unwind the
option pair when the view changes; on a rally to `K2`, either deliver into
the cap (stock) or roll the call up-and-out.

**Common adjustments.** Roll the whole collar up after gains (raise both
strikes — ratchet locking); widen/narrow the band (risk appetite); shift to
put-spread collar (sell a lower put too, cheapening further at the cost of
a re-opened tail below).

**Greeks at entry.** Net Δ **+** small-to-moderate (long underlying partly
offset by put Δ and short call Δ) · Γ ≈ mixed (long gamma near `K1`, short
near `K2`, ≈flat between) · Θ ≈ small mixed (put bleed vs call earn) ·
V ≈ small mixed (long `K1` vega vs short `K2` vega roughly offsetting).

**Margin (India).** Stock: shares + SPAN+Exposure on the short call (long
put premium paid; pledge/collateral mechanics as #5 — UNVERIFIED-DETAIL,
U8). Index: futures + the option pair, SPAN-offset (UNVERIFIED, U6).

**Risk class.** DEFINED (both sides capped).

**Indian-market fit.**
- Index: futures-collared (no cash index); all three legs margined with
  offsets; cash settlement at expiry.
- Stock: the natural home — physical settlement means a capped rally
  delivers the shares at `K2` (intended exit) and a floor breach
  give-delivers at `K1` (intended stop). Liquidity constrains `K1/K2`
  choice to near-ATM strikes in most single names.
- Zero-cost calibration on Indian skew: because index put IV > call IV at
  symmetric distances, the zero-cost put is typically FURTHER from spot than
  the call it finances (floor wider than cap) — an empirical, dataset-
  derivable asymmetry (UNVERIFIED-MAGNITUDE).
- Repo: not materializable (3 legs incl. short + underlying).

**Future sweep axes.** (K1 distance, K2 distance) grid × expiry tier ×
re-collar cadence × zero-cost-solve toggle (solve K2 given K1 s.t. N≈0).

<!-- CONT-2 -->

