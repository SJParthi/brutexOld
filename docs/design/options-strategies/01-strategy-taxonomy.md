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
  Exposure margin, collected upfront (**SPAN+EXPOSURE** — magnitude
  UNVERIFIED here; peak-margin regime — UNVERIFIED-CIRCULAR, A-U5). Recognized hedges (verticals,
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

### A.10 Bull call spread (debit vertical)

**Legs.** Buy call `K1`, sell call `K2`, `K1 < K2`, same expiry. Net debit
`D = P(K1) − P(K2) > 0`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = max(S − K1, 0) − max(S − K2, 0) − D`
- Max profit: `(K2 − K1) − D` (for all `S ≥ K2`) → per lot
  `((K2 − K1) − D)·L`
- Max loss: `D` (for all `S ≤ K1`) → per lot `D·L`
- Breakeven: `S* = K1 + D`
- Sanity identity a reviewer can recompute: max profit + max loss
  `= (K2 − K1)` always — the width is conserved between the two outcomes.

**Typical entry logic.** Directional bullish with a TARGET (spot expected at
or above `K2` by expiry); cheaper than #1 by the short-leg credit; a
moderate-IV-agnostic way to be long direction (the two legs' vegas partly
cancel). Strike placement: `K1` at/near money, `K2` at target. Risk/reward
tuning: wider = more directional, dearer; narrower = binary-like.

**Typical exit logic.** Close at a fraction of max value (verticals rarely
reach full width before the last days — the tail of the value convergence is
slow); time stop; exit on signal invalidation below `K1`.

**Common adjustments.** Roll the spread up/out; convert to butterfly by
selling another `K2` call and buying `K3` (B.8 structure); close the
short leg into weakness (leaves #1 — a conscious risk-class-preserving but
cost-raising choice).

**Greeks at entry.** Δ **+** (≈ Δ(K1) − Δ(K2), peak sensitivity between
strikes) · Γ state-dependent (+ near `K1`, − near `K2`) · Θ **−** while the
spread is OTM, flips **+** once deep ITM (the short leg's decay dominates) ·
V small **+** when OTM, small **−** when ITM — near-vega-neutral around the
midpoint.

**Margin (India).** Long leg: premium. Short leg: SPAN+Exposure, reduced by
the recognized spread offset (defined-risk verticals carry materially less
margin than naked shorts; magnitude UNVERIFIED, U6). Practical broker rule:
the margin benefit exists only while BOTH legs are on — leg-out order and
intraday leg exits can spike the margin requirement (UNVERIFIED-DETAIL,
U8).

**Risk class.** DEFINED.

**Indian-market fit.**
- Index: excellent — dense weekly strike grids on NIFTY/BANKNIFTY/SENSEX
  make width/moneyness sweeps natural; cash settlement keeps expiry clean.
- Stock: workable near ATM in liquid names; wide markets on the short leg
  often cost a large fraction of the theoretical edge; physical settlement
  of a partially-ITM spread at expiry (only one leg ITM) leaves a NET
  delivery obligation (UNVERIFIED-DETAIL, U2).
- Repo: not materializable (short leg). The strike-grid machinery the
  executor already uses (`brutex.options.strike_grid`, cited at
  `brutex/options/executor.py:11-12`) is the natural substrate for vertical
  strike selection in a future engine.

**Future sweep axes.** K1 moneyness × width (K2−K1 in strikes) × expiry
tier × exit fraction-of-width × entry signal family. This is the most
naturally brute-force-sweepable options family: 2 legs, defined risk, small
parameter space per trade.

### A.11 Bear put spread (debit vertical)

**Legs.** Buy put `K2`, sell put `K1`, `K1 < K2`, same expiry. Net debit
`D = P(K2) − P(K1) > 0`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = max(K2 − S, 0) − max(K1 − S, 0) − D`
- Max profit: `(K2 − K1) − D` (for all `S ≤ K1`) → per lot
  `((K2 − K1) − D)·L`
- Max loss: `D` (for all `S ≥ K2`) → per lot `D·L`
- Breakeven: `S* = K2 − D`
- Width identity: max profit + max loss `= (K2 − K1)`.

**Typical entry logic.** Directional bearish with a downside target `K1`;
the put-skew makes outright long puts (#2) dear — the short lower-strike
put sells that same skew back, making bear put spreads the skew-neutral way
to be short Indian indices.

**Typical exit logic / adjustments / Greeks.** Mirror of #10: exit at
fraction-of-width or invalidation above `K2`; roll down/out; Γ
state-dependent; Θ **−** while OTM (spot > `K2`), **+** deep ITM
(spot < `K1`); V small + when OTM, small − when ITM.

**Margin (India).** As #10 (debit + spread-offset SPAN on the short leg;
UNVERIFIED magnitudes, U6/U8).

**Risk class.** DEFINED.

**Indian-market fit.** As #10 mirrored; the workhorse tactical hedge for
index event risk when full protective puts are too expensive. Stock case:
same net-delivery caveat when exactly one leg is ITM at expiry
(UNVERIFIED-DETAIL, U2).

**Future sweep axes.** as #10 mirrored.

### A.12 Bull put spread (credit vertical)

**Legs.** Sell put `K2`, buy put `K1`, `K1 < K2`, same expiry. Net credit
`C = P(K2) − P(K1) > 0`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = C − [max(K2 − S, 0) − max(K1 − S, 0)]`
- Max profit: `C` (for all `S ≥ K2`) → per lot `C·L`
- Max loss: `(K2 − K1) − C` (for all `S ≤ K1`) → per lot
  `((K2 − K1) − C)·L`
- Breakeven: `S* = K2 − C`
- Parity note: same expiry-payoff SHAPE as #10 at the same strikes (both are
  "long the K1..K2 interval"); they differ in premium flow (credit received
  now vs debit paid), carry sign convention, and which leg pins at expiry.

**Typical entry logic.** Bullish-to-neutral premium harvesting with defined
risk: sell the 0.2-0.3Δ put, buy the wing 1-4 strikes lower. Entered
systematically at fixed DTE (weekly index cycles), sized off max loss.
This — with #13 — is the dominant defined-risk income structure in Indian
index options.

**Typical exit logic.** Buy back at 50-80% of credit captured; stop at
premium multiple or short-strike breach; avoid holding through the final
hours when the short strike is near-ATM (pin/gamma).

**Common adjustments.** Roll down-and-out for additional credit; convert to
iron condor by adding a bear call spread (#13; B.6 owns the combined
structure); widen the wing (more credit, more risk) or narrow (less of
both).

**Greeks at entry.** Δ **+** small · Γ **−** (concentrated at the short
strike, worst near expiry) · Θ **+** (the engine) · V **−** (entered OTM).

**Margin (India).** SPAN+Exposure on the short leg reduced by the wing's
spread benefit; the max-loss-bounded nature is *recognized but not
equal-to-max-loss* — Indian spread margin is typically above the theoretical
max loss, unlike US Reg-T defined-risk margining (UNVERIFIED-DETAIL, U6).

**Risk class.** DEFINED.

**Indian-market fit.**
- Index: the classic — dense weeklies, cash settlement, high seller
  participation. The 2024-25 SEBI measures reshaped WHICH weeklies exist
  (one weekly index per exchange — UNVERIFIED-CIRCULAR, U10) and lot sizes
  (repo-verified table, `brutex/options/lot_size_history.py:10-19`), so any
  historical sweep must model the regime changes, not a static product.
- Stock: usable in the most liquid names only; monthly cycle; physical
  settlement net-delivery caveat as #10/#11.
- Repo: not materializable (short leg).

**Future sweep axes.** short-strike Δ × wing width × DTE × profit-take
fraction × stop rule — the canonical short-premium sweep grid.

### A.13 Bear call spread (credit vertical)

**Legs.** Sell call `K1`, buy call `K2`, `K1 < K2`, same expiry. Net credit
`C = P(K1) − P(K2) > 0`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = C − [max(S − K1, 0) − max(S − K2, 0)]`
- Max profit: `C` (for all `S ≤ K1`) → per lot `C·L`
- Max loss: `(K2 − K1) − C` (for all `S ≥ K2`) → per lot
  `((K2 − K1) − C)·L`
- Breakeven: `S* = K1 + C`
- Parity note: same expiry shape as #11 at the same strikes.

**Typical entry logic.** Bearish-to-neutral income under a resistance;
the defined-risk replacement for the naked call (#3) — same thesis, tail
amputated by the `K2` wing. Systematic 0.2-0.3Δ short call + wing at fixed
DTE.

**Typical exit / adjustments / Greeks.** Mirror of #12: Δ **−** small,
Γ **−**, Θ **+**, V **−**; buy back at captured fraction; roll up-and-out;
pair with #12 into an iron condor.

**Margin (India).** As #12 (UNVERIFIED magnitudes, U6).

**Risk class.** DEFINED.

**Indian-market fit.** As #12; upside index tails (short-covering rallies,
event gaps) are the loss scenario — the wing converts #3's account-ruin
tail into a known width, which is exactly why any future BruteX
short-premium engine should treat the naked and winged forms as different
risk CLASSES, not parameter variants of one strategy.

**Future sweep axes.** as #12 mirrored.

### A.14 Synthetic long / synthetic short underlying

**Legs (synthetic long).** Buy call `K`, sell put `K`, same expiry. Signed
net premium `N = P_call − P_put` (either sign; near-ATM `N` ≈ the carry).

**Payoff at expiry (per unit; ×L per lot) — synthetic long.**

- `π(S) = max(S − K, 0) − max(K − S, 0) − N = (S − K) − N` — exactly linear,
  a forward bought at effective price `K + N`.
- Max profit: unbounded. Max loss: `(K + N)` per unit at `S = 0` —
  DEFINED-LARGE (full notional downside, like a future).
- Breakeven: `S* = K + N`.

**Synthetic short.** Sell call `K`, buy put `K`; with net credit
`N' = P_call − P_put` received: `π(S) = N' + (K − S)` — a forward sold at
`K + N'`. Breakeven `S* = K + N'`. **Loss unbounded as `S → ∞` —
UNDEFINED RISK**, exactly like a short future.

**Typical entry logic.** (a) Delta-1 exposure when the future itself is
unavailable or mispriced: parity dislocations (`C − P ≠ F − K`
discounted), derivative ban periods, or expiry-basis plays; (b) leg-wise
construction — a trader long a call converts to synthetic long by selling
the put after a dip (locking a better effective entry).

**Typical exit logic.** Managed as a futures position: stop/target on the
underlying; unwound as a pair (legging out re-introduces optionality and
changes the risk class — must be explicit).

**Common adjustments.** Convert to risk-defined by buying a wing on the
short leg (synthetic long + long OTM put = protective structure); roll the
pair to the next expiry (the "synthetic roll" — in liquid markets priced as
one spread).

**Greeks at entry.** Δ ≈ **+1** (long) / **−1** (short), constant · Γ ≈ 0 ·
Θ ≈ 0 (residual = carry) · V ≈ 0 (the long-call vega cancels the short-put
vega at the same strike/expiry). Residual sensitivity: interest rate /
dividend expectations (rho-like), which IS the basis.

**Margin (India).** The short option leg carries SPAN+Exposure; the long leg
premium. SPAN recognizes the combined delta-1 risk, producing a margin of
the same order as the corresponding futures position (UNVERIFIED-DETAIL,
U6). Net margin usually ≥ plain futures margin, which is why futures are
the default delta-1 instrument when available.

**Risk class.** Long: DEFINED-LARGE (notional). Short: **UNDEFINED** —
label as loudly as a naked call.

**Indian-market fit.**
- Index: NIFTY/BANKNIFTY/SENSEX futures are deep and liquid, so synthetics
  are mostly an ARBITRAGE surface (see #16) rather than an exposure
  vehicle; exception: fine-grained strike-pinned entries and
  post-ban-period situations.
- Stock: during MWPL ban periods NO fresh derivative positions may be
  opened in the name (futures OR options — UNVERIFIED-DETAIL, U7), so the
  "use synthetics when futures are banned" folklore does NOT cleanly apply
  in India; verify before designing around it.
- Repo: not materializable (short leg). Note the repo's futures machinery
  (series alias, roll resolver) is RETIRED from engine scope but retained
  as lake tooling (`brutex/universe.py:379` context; §6 CLAUDE.md table) —
  a future synthetic/underlying-legged engine would rebuild on that
  substrate under its own DEC.
- Split-strike cousin: the **risk reversal** (−P(K1) + C(K2), K1 < K2) is
  classified in Part C (§5.7 row 29, §5.8.4 row 29) — same
  UNDEFINED-equivalent downside class, plus skew harvesting between the two
  strikes.

**Future sweep axes.** strike offset from spot × expiry tier × basis
threshold at entry (enter only when |C − P − (F − K)| exceeds costs) —
i.e., the sweepable quantity is the PARITY RESIDUAL, not direction.

### A.15 Synthetic call / synthetic put

**Synthetic call = long underlying + long put** (same structure as #8 —
protective/married put; listed separately because the taxonomies differ in
intent: #8 is "hedge a holding", #15 is "replicate a call").

- `π(S) = (S − S0) − P + max(K − S, 0)` — identical to #8: floor
  `(K − S0) − P`, unbounded upside, breakeven `S0 + P`.
- Greeks/margin/risk/Indian-fit: see #8. Index version futures-legged.

**Synthetic put = short underlying + long call.** Short underlying at `S0`
(India: short futures — retail cash shorting is intraday-only, #6 note),
buy call `K ≥ S0` typically, pay `P`.

**Payoff at expiry (per unit; ×L per lot).**

- `π(S) = (S0 − S) − P + max(S − K, 0)`
- For `S ≥ K`: `π = (S0 − K) − P` (constant) → Max loss
  `((K − S0) + P)·L` per lot (capped upside loss — the call caps the short's
  unbounded tail)
- Max profit: `(S0 − P)` per unit at `S = 0` → `(S0 − P)·L` per lot
- Breakeven: `S* = S0 − P`
- Expiry shape ≡ **long put at K**.

**Typical entry logic.** Bearish with a hard stop bought in premium form
(the call IS the stop — gap-proof, unlike a stop order); or replicating a
put where the listed put is illiquid/rich at the desired strike.

**Typical exit logic.** As a stopped short: cover at target; the call
expires or is sold on cover.

**Common adjustments.** Roll the call up/down to re-set the stop; convert to
plain #2 by closing the futures leg.

**Greeks at entry.** Net Δ **−** (`−1 + Δ_call` ∈ (−1, 0)) · Γ **+** ·
Θ **−** · V **+**.

**Margin (India).** Short futures SPAN+Exposure + call premium; SPAN hedge
offset for the pair (UNVERIFIED, U6).

**Risk class.** DEFINED (the call caps the short-side tail) — note this is
the risk-DEFINED way to be short, vs #6 which is UNDEFINED.

**Indian-market fit.** Index: short futures + long call — the standard
gap-protected short on NIFTY/BANKNIFTY/SENSEX; overnight gap risk (the
dominant Indian short-side hazard given sharp opening gaps) is exactly what
the premium buys off. Stock: futures-legged as always; ban-period and
delivery caveats as #6/#14.

**Future sweep axes.** call strike distance (stop width) × expiry tier ×
futures roll rule.

### A.16 Conversion / reversal

**Legs (conversion).** Long underlying at `S0` + long put `K` + short call
`K` (same strike, same expiry) — i.e., long underlying + synthetic short at
`K`. **Legs (reversal).** Short underlying + short put `K` + long call `K` —
short underlying + synthetic long.

**Payoff at expiry (per unit; ×L per lot) — LOCKED, S-independent.**

Identity: `S + max(K − S, 0) − max(S − K, 0) = K` for every `S`. Hence:

- Conversion P&L: `π = (K − S0) + (P_call − P_put)` per unit, locked at
  entry (received call premium `P_call`, paid put premium `P_put`).
  Positive after ALL costs + funding ⇒ pure arbitrage.
- Reversal P&L: `π = (S0 − K) + (P_put − P_call)` per unit, locked.
- Max profit = max loss = the locked number (no S-dependence); the "risk"
  is execution, carry, and settlement friction, not direction.

**Typical entry logic.** Pure relative-value: enter when put-call parity is
violated beyond total friction (`|C − P − (F_or_carry-adjusted forward − K)|`
> costs). In practice a market-maker/arb-desk trade executed as one
3-legged package; retail leg-in attempts usually die on execution slippage.

**Typical exit logic.** Hold to expiry (the lock realizes itself) or unwind
early if the mispricing inverts enough to pay double friction.

**Common adjustments.** None in the directional sense; the management
surface is carry (funding rate changes) and — for stocks — dividend
surprises, which shift the fair conversion value and can turn a locked
profit into a locked loss.

**Greeks at entry.** All ≈ **0** (Δ, Γ, Θ, V each net out); residual
exposures: rho (funding), dividend risk (stocks), and early-unwind
mark-to-market noise.

**Margin (India).** Full margins on the futures leg + short option leg,
minus SPAN offsets for the recognized hedge (UNVERIFIED, U6). The margin
carry cost is a first-order input to whether the arb clears — a "profitable"
conversion gross of margin funding is routinely unprofitable net.

**Risk class.** DEFINED (locked arb; residual = carry/dividend/execution).

**Indian-market fit.**
- Index: the underlying leg is FUTURES (no cash index), so the Indian index
  "conversion" is really the **futures-vs-synthetic parity trade** (long
  future + synthetic short, or the reverse) — a pure basis trade,
  cash-settled, no delivery mechanics. With three exchanges' products
  (NSE × 2 indices + BSE SENSEX) and dense weeklies, transient parity
  violations exist but are harvested by SEBI-registered prop/HFT within
  ticks; retail edge net of costs is presumptively ≤ 0
  (UNVERIFIED-MAGNITUDE — an empirical claim the lake data could actually
  measure, which is a legitimate future BruteX research use).
- Stock: physical settlement HELPS true conversions — at expiry the ITM leg
  delivers/receives shares against the held/short stock, closing the
  package physically; but delivery margins, STT on the delivery leg and
  dividend risk eat the lock (UNVERIFIED-DETAILS, U2/U4).
- Repo: not materializable (short legs + underlying leg).

**Future sweep axes.** none in the strategy-parameter sense; as an engine
citizen this family is a *diagnostic* (parity-residual scanner over the
lake) rather than a P&L strategy — worth stating in the design so nobody
sweeps "conversion entries" as if they were directional signals.

### A.17 Box spread

**Legs.** Bull call spread `(K1, K2)` + bear put spread `(K2, K1)`, same
expiry: `+C(K1) −C(K2) +P(K2) −P(K1)`, `K1 < K2`.

**Payoff at expiry (per unit; ×L per lot) — LOCKED.**

- Expiry value is `K2 − K1` for EVERY `S` (each spread's identity sums to
  the width).
- Long box: pay net debit `D` today, receive `(K2 − K1)` at expiry:
  `π = (K2 − K1) − D` locked. Buying a box below width = synthetic
  LENDING at the implied rate `((K2 − K1) − D)/D` over the tenor.
- Short box: receive `C` today, pay width at expiry: `π = C − (K2 − K1)`
  locked — synthetic BORROWING.
- Max profit = max loss = the locked number; breakeven concept degenerates
  (no S-dependence).

**Typical entry logic.** Financing/rate trade, not a market trade: deploy
cash (long box) or raise cash (short box) at the box-implied rate when it
beats the alternative; or capture a genuinely mispriced 4-leg package.

**Typical exit logic.** Hold to expiry (value converges to width); early
unwind only on rate/mispricing inversion.

**Common adjustments.** None (any adjustment un-boxes it).

**Greeks at entry.** All ≈ **0**; pure rho/carry instrument.

**Margin (India).** All four legs margined; SPAN offsets the fully-hedged
package to a residual (UNVERIFIED magnitude, U6). Two short legs still
attract margin until expiry — the margin funding cost is the main reason
retail Indian boxes rarely clear positive net.

**Risk class.** DEFINED (locked; residual = execution + fees + margin
carry). **Early-exercise blowup risk — the famous US case of a short box in
AMERICAN-style options being partially exercised — does NOT apply on NSE:
index and stock options are European-exercise (UNVERIFIED-CIRCULAR for the
stock-option style history, U1), so a short box cannot be dismembered
before expiry by assignment.**

**Indian-market fit.**
- Index: European + cash-settled ⇒ structurally safe boxes; but 4 legs ×
  (brokerage + exchange charges + STT on the sell legs + stamp) against a
  locked gross of a few rupees per unit — the friction stack is the whole
  game, and it is owned quantitatively by the microstructure agents.
- Stock: 4 legs of thin books; net-delivery mechanics if held to expiry
  with mixed ITM legs (the deliveries offset in principle —
  UNVERIFIED-DETAIL, U2); practically institutional-only.
- Repo: not materializable; as with #16, the honest future-engine framing
  is a rate/mispricing DIAGNOSTIC over the lake, not a sweep strategy.

### A.18 Cross-cutting design notes for a future BruteX options engine (informative, no scope claim)

1. **Risk class must be a first-class artifact field.** #3, #6 and the
   short-synthetic arm of #14 are UNDEFINED-risk; #4/#5/#7 and the
   long-synthetic arm are DEFINED-LARGE; everything else in Part A is
   DEFINED. A ranking surface that mixes classes without a visible flag
   would be a Golden-Rule-#1-class honesty failure.
2. **Every underlying-legged structure forks into (stock | futures-covered)
   variants** with different margins, carry, basis and expiry mechanics —
   for the 3 engine indices the cash variant does not exist at all.
3. **Per-date lot sizing is mandatory** (repo-verified 3-7× drift across the
   window — `brutex/options/lot_size_history.py:3-5`).
4. **ROI denominators must be explicit** (margin-true vs cash-secured vs
   notional); the repo's refusal-only SPAN table
   (`brutex/engine/margin_history.py:12-18`) means margin-true ROI stays
   honestly UNVERIFIED until a sourced SPAN archive is integrated — a real
   prerequisite for ranking short-premium strategies at all (see
   `09-capital-margin-and-sizing.md`).
5. **The existing executor's reject taxonomy generalizes leg-wise**
   (`brutex/options/executor.py:155-184`): a multi-leg trade should reject
   (never fabricate) if ANY leg hits NO_DATA / TICK_FLOOR /
   LOT_SIZE_UNKNOWN / STALE_BAR / COST_REGIME_UNVERIFIED, with the reject
   attributed per leg.
6. **Parity-equivalent pairs (#5≡#4-shape, #6≡#3-shape, #10≡#12-shape,
   #11≡#13-shape, #8≡#1-shape@K) should be recognized in the taxonomy**, so
   the sweep space is not silently doubled and so risk flags propagate to
   the equivalent form. (The generic structure grammar that mechanizes this
   is §5.8.)

---

## 4. Part B — Volatility, multi-leg & time structures (B.1–B.18)

The 18 volatility, multi-leg and time structures. Formulas use the §1.2
symbol set; internal `§N` = section B.N; `CN-*n*` = §1.6; `U*n*` = register
row B-U*n* (§6); the shorthand `spec :N` = `docs/track2/TRACK2_OPTIONS_SPEC.md:N`.

### B.1 Long straddle

**Structure.** Buy 1 CE and 1 PE at the same strike `K` (normally ATM via
`round_to_atm` — `strike_grid.py:38-60`), same expiry. Net debit `N_db = C(K) + P(K)`.

#### Payoff math (per unit, at expiry)

```
Π(S_T) = |S_T − K| − N_db
Max loss   = N_db                  (at S_T = K — the pin)
Max profit = unbounded upside; K − N_db as S_T → 0 on the downside
Breakevens = K − N_db  and  K + N_db
```

#### Entry / exit / adjustment logic

- Entry: ahead of scheduled variance events (Budget, RBI policy, elections, earnings for
  stocks) or on low-IV-rank regimes; strike = ATM at signal time (CN-1).
- Exit: profit target as % of debit; time stop (theta bleed dominates once the move fails
  to arrive); IV-crush stop after the event; 15:20 forced exit for intraday variants (CN-6).
- Adjustments: roll the untested side toward spot ("gamma scalping" the winner is a
  live-trading technique, not a bar-replay-friendly rule); or convert to an iron fly by
  selling wings — every adjustment is itself a leg-vector transition the engine must model
  as a discrete re-materialization (CN-7).
- Sweepable axes: DTE at entry, strike offset (ATM±N), profit-target %, stop %, time-stop
  bar count, event-window filters.

#### Greeks profile

Delta ≈ 0 at entry (call ≈ +0.5, put ≈ −0.5); gamma strongly positive; vega strongly
positive (the purest long-vol structure among single-expiry combos); theta strongly
negative and accelerating into expiry. Delta drifts toward ±1 as spot leaves `K`.

#### Margin nature (India)

PREMIUM-ONLY (two long legs). No SPAN. Repo margin machinery not needed for the long form.

#### Risk class

**DEFINED** — maximum loss is the debit paid, structurally.

#### Indian-market fit

- Weekly index: excellent fit — NIFTY (Tue) and SENSEX (Thu) weeklies give cheap, dense
  event-straddle expressions; BANKNIFTY only monthly (spec `:108`, `expiry_calendar.py:20`).
  0-2 DTE straddles are extremely theta-hostile; the sweep must expose DTE as an axis.
- Stock options: feasible (long-only, no delivery risk if exited before expiry; a long ITM
  leg carried into expiry on a stock triggers physical settlement obligations — exit-before-
  expiry law required; settlement mechanics deferred to microstructure agents, U1/U2).
- BruteX readiness: closest structure to today's executor (it is exactly the union of the
  two shipped single-leg trades); needs only joint-leg gating (CN-2) + package accounting.

### B.2 Short straddle

**Structure.** Sell 1 CE and 1 PE at the same strike `K`, same expiry. Net credit
`N_cr = C(K) + P(K)`.

#### Payoff math (per unit, at expiry)

```
Π(S_T) = N_cr − |S_T − K|
Max profit = N_cr                  (at S_T = K)
Max loss   = UNBOUNDED above; K − N_cr of loss potential down to S_T = 0
Breakevens = K − N_cr  and  K + N_cr
```

#### Entry / exit / adjustment logic

- Entry: high-IV-rank / post-event IV crush; expiry-day pin plays on weeklies; typically
  delta-neutral at entry.
- Exit: credit-decay target (e.g. buy back at x% of credit); hard SL on the tested side or
  on package mark-to-market; forced square-off before close (intraday) or before expiry
  (positional, mandatory on stocks — U1/U2).
- Adjustments: roll the untested side in (inverted strangle risk); convert to iron fly by
  buying wings; delta-hedge with futures. Each is a discrete leg-vector transition (CN-7).
- Sweepable axes: DTE, entry time-of-day, SL multiple of credit, profit-take %, re-entry
  rules, event-day exclusion filters.

#### Greeks profile

Delta ≈ 0; gamma strongly negative (worst at the pin, explosive on expiry day); vega
strongly negative; theta strongly positive. The canonical "many small wins, rare brutal
losses" negative-skew profile the selling backlog names for its cousin trade: "many small
wins + rare brutal continuation-day losses (negative skew)"
(`TRACK2_OPTION_SELLING_BACKLOG.md:18-20`).

#### Margin nature (India)

SPAN+EXPOSURE on both naked legs (no offset benefit — the two shorts do not hedge each
other's tails). Expiry-day short-option add-on ELM per the SEBI 2024-10-01 measure series —
UNVERIFIED here (U7). Repo blocker: margin table is refusal-only (`margin_history.py:10-19`).

#### Risk class

**⚠ UNDEFINED — UNLIMITED upside loss, near-total downside loss.** Must be labelled and
gated as such in any future engine (position caps + margin model are hard prerequisites).
See `08-seller-blowups-and-tail-events.md`.

#### Indian-market fit

- Weekly index: the dominant Indian retail/prop short-vol expression, especially expiry-day
  (0DTE) NIFTY/SENSEX straddles; BANKNIFTY variant is monthly-only post-Nov-2024. SEBI's
  2024-10-01 measure series (weekly rationalization verified via spec `:108` + lot resize via
  `lot_size_history.py:16`; expiry-day ELM add-on U7) exists precisely because of this flow.
- Stock options: poor fit — monthly-only, gap/news risk across weeks of holding, and short
  ITM legs at expiry create physical DELIVERY obligations with stepped-up delivery margins
  (`PLAN_FINALIZATION_2026-05-16.md:333`; `docs/groww/trade-api-2026-07-03/05-margin.md:183`) — U2.
- BruteX readiness: double-gated (CN-4); winner-censoring hazard applies in full (CN-5).

### B.3 Long strangle

**Structure.** Buy 1 OTM PE at `K_p` and 1 OTM CE at `K_c`, `K_p < S < K_c`, same expiry.
Net debit `N_db = P(K_p) + C(K_c)`. Symmetric offsets (ATM−N / ATM+N) are the natural
sweep parameterization on the repo's offset grammar (CN-1).

#### Payoff math (per unit, at expiry)

```
Π(S_T) = (K_p − S_T)⁺ + (S_T − K_c)⁺ − N_db
Max loss   = N_db                  (for K_p ≤ S_T ≤ K_c — the whole inner band)
Max profit = unbounded upside; K_p − N_db as S_T → 0
Breakevens = K_p − N_db  and  K_c + N_db
```

#### Entry / exit / adjustment logic

- Entry: same regimes as the long straddle but cheaper and lower-probability (needs a
  larger move); width `K_c − K_p` trades cost against breakeven distance.
- Exit: profit %, time stop, event-crush stop; 15:20 law intraday (CN-6).
- Adjustments: leg into a reverse iron condor (sell further wings); roll strikes with spot.
- Sweepable axes: put offset, call offset (independently — asymmetric strangles are a
  genuine axis), DTE, exits.

#### Greeks profile

Delta ≈ 0 (small net from skew); gamma +; vega + (slightly less than the straddle per
rupee of vega, much less per rupee of debit); theta −. Both wings decay to zero worthless
in the whole inner band — the theta bleed is total there.

#### Margin nature (India)

PREMIUM-ONLY. No SPAN.

#### Risk class

**DEFINED** — the debit.

#### Indian-market fit

- Weekly index: strong fit; OTM weekly wings on NIFTY/SENSEX are dense on the 50/100-pt
  grids (`strike_grid.py:26-30`). Note the repo lake's fetched chain is banded near ATM
  (ATM±10 grid law, `strike_grid.py:86-102`; `RejectReason.NO_DATA` band-drift hole,
  `executor.py:158-161`) — far-OTM strangle wings may honestly gate NO_DATA.
- Stock options: as long straddle — feasible with exit-before-expiry discipline (U1/U2).

### B.4 Short strangle

**Structure.** Sell the §3 structure: −P(K_p), −C(K_c). Net credit `N_cr`.

#### Payoff math (per unit, at expiry)

```
Π(S_T) = N_cr − (K_p − S_T)⁺ − (S_T − K_c)⁺
Max profit = N_cr                  (for K_p ≤ S_T ≤ K_c)
Max loss   = UNBOUNDED above; K_p − N_cr of loss potential to S_T = 0
Breakevens = K_p − N_cr  and  K_c + N_cr
```

#### Entry / exit / adjustment logic

- Entry: delta-targeted wings (e.g. sell the ~0.15Δ call and ~0.15Δ put) or offset-targeted
  (ATM±N); high-IV regimes. Delta-targeting requires a Greeks chain — the repo's Greeks
  enrichment exists as lake overlay (`brutex/data/greeks_enrich.py`; see §5.0 G8/G9) but the
  executor's delta-target rules are caller-supplied-chain-only today (`executor.py:33-36`).
- Exit: credit-decay target; per-side SL (e.g. 2-3× credit on the tested side); roll
  untested side; square-off laws as §2.
- Sweepable axes: per-side offsets/deltas, DTE, SL/TP multiples, entry time, event filters.

#### Greeks profile

Delta ≈ 0; gamma −; vega −; theta +. Tail behavior identical in kind to the short straddle
but with a probability cushion equal to the inner-band width.

#### Margin nature (India)

SPAN+EXPOSURE, both legs naked, no offset benefit; expiry-day ELM add-on U7.

#### Risk class

**⚠ UNDEFINED — both sides.** Same gating requirements as §2.

#### Indian-market fit

- Weekly index: alongside the short straddle, the core Indian weekly income trade;
  practical only on NIFTY/SENSEX weeklies + BANKNIFTY monthlies now (spec `:108`).
- Stock options: poor fit (monthly duration + physical settlement on breached short legs —
  U1/U2); also the lake's ATM-banded stock-option coverage makes far-OTM short wings the
  exact winner-censoring shape (CN-5, backlog `:28-36`).

### B.5 Guts (long / short)

**Structure.** Both legs IN-the-money: long guts = buy ITM CE at `K1 < S` and buy ITM PE at
`K2 > S` (`K1 < K2`). Short guts sells both. Net debit `N_db ≥ K2 − K1` always (each leg
carries ≥ its intrinsic; the pair's intrinsics sum to exactly `K2 − K1` everywhere between
the strikes).

#### Payoff math (per unit, at expiry)

```
Π_long(S_T) = (S_T − K1)⁺ + (K2 − S_T)⁺ − N_db
  For K1 ≤ S_T ≤ K2:  Π = (K2 − K1) − N_db   (constant = max loss; ≤ 0 by construction)
  Max loss   = N_db − (K2 − K1)   (the aggregate time value paid)
  Breakevens = K2 − N_db  (lower)  and  K1 + N_db  (upper)
  Max profit = unbounded upside; K2 − N_db as S_T → 0

Π_short(S_T) = N_cr − (S_T − K1)⁺ − (K2 − S_T)⁺
  Max profit = N_cr − (K2 − K1)   (between the strikes)
  Max loss   = UNBOUNDED above / to-zero below
```

Economically a guts is a strangle shifted by the intrinsic corridor: long guts ≡ long
strangle at the SAME strikes plus a box of value `K2 − K1` (put-call parity), so any
premium divergence beyond costs is arbitrage-bounded.

#### Entry / exit / adjustment logic

As the strangle (§3/§4) — the choice of guts over strangle in practice is a
liquidity/microstructure choice, not a payoff choice. In India ITM options trade wider and
thinner than OTM; the same-tick-floor and stale-bar gates (`executor.py:162-176`) will
reject many ITM prints.

#### Greeks profile

Identical in aggregate to the corresponding strangle at the same strikes (parity); per-leg
deltas near ±1 make the structure execution-sensitive (large per-leg premiums, small net
time value).

#### Margin nature (India)

Long: PREMIUM-ONLY (large outlay — both legs ITM). Short: SPAN+EXPOSURE naked both sides.

#### Risk class

Long guts **DEFINED** (time value paid). Short guts **⚠ UNDEFINED both sides**.

#### Indian-market fit

Low practical value on index weeklies (the strangle dominates it on spread costs) and very
low on stock options (ITM stock options are illiquid; ITM legs held to expiry are exactly
the physical-delivery shape — U1/U2). Included for taxonomy completeness; recommend the
future engine treat guts as a strangle alias with an ITM-addressing flag rather than a
first-class structure.

### B.6 Iron condor

**Structure.** Short put spread + short call spread, all same expiry:
+P(K1) −P(K2) −C(K3) +C(K4), `K1 < K2 < K3 < K4`, put width `w_p = K2 − K1`, call width
`w_c = K4 − K3` (usually `w_p = w_c = w`). Net credit `N_cr`.

#### Payoff math (per unit, at expiry)

```
Π(S_T) = N_cr + (K1 − S_T)⁺ − (K2 − S_T)⁺ − (S_T − K3)⁺ + (S_T − K4)⁺
Max profit = N_cr                          (for K2 ≤ S_T ≤ K3)
Max loss   = max(w_p, w_c) − N_cr          (beyond either long wing; = w − N_cr equal-width)
Breakevens = K2 − N_cr  and  K3 + N_cr
```

#### Entry / exit / adjustment logic

- Entry: delta- or offset-targeted short strikes (e.g. ~0.15Δ shorts), wings 1-4 strike
  steps beyond; high-IV-rank preference; typically 0-7 DTE on Indian weeklies.
- Exit: buy back at x% of credit; SL at multiple of credit; tested-side management (roll
  the untested spread in; roll out in time — needs the calendar machinery of §11).
- Sweepable axes: short-strike offsets/deltas per side, wing width per side, DTE, credit
  target, SL multiple, entry time-of-day, event filters, symmetric-vs-skewed placement.

#### Greeks profile

Delta ≈ 0; gamma −, vega −, theta + — all three attenuated versus the short strangle by
the wings; the Greeks flip sign locally once spot approaches a wing corridor.

#### Margin nature (India)

NET-OF-HEDGE: both shorts are spread-hedged, so SPAN recognizes the defined width;
practically the margin ≈ per-side width exposure minus credit (numbers UNVERIFIED — U5).
This hedge benefit is what makes condors capital-popular in India.

#### Risk class

**DEFINED** — `max(w_p, w_c) − N_cr`. The engine can compute worst-case loss at entry
exactly, which makes the condor the natural first SHORT-family citizen once selling is
authorized (still CN-4-gated).

#### Indian-market fit

- Weekly index: the flagship Indian weekly income structure on NIFTY (Tue) and SENSEX
  (Thu); BANKNIFTY monthly-only (spec `:108`). Dense 50/100-pt grids give fine width
  control (`strike_grid.py:26-30`); far wings can hit the lake's banded-coverage NO_DATA
  gate (CN-2).
- Stock options: workable on liquid F&O names as a monthly defined-risk trade, but both
  short legs must be closed before expiry to avoid physical-settlement obligations on a
  breach (U1/U2); wide stock spreads raise cost drag.
- BruteX readiness: 4-leg vector, all machinery per CN-1/CN-2/CN-7; margin model needed
  only for capital-normalized ranking (loss cap is structural).

### B.7 Iron butterfly

**Structure.** Short ATM straddle + protective wings: +P(K−w_p) −P(K) −C(K) +C(K+w_c),
usually `w_p = w_c = w`. Net credit `N_cr` (larger than the condor's — the shorts are ATM).

#### Payoff math (per unit, at expiry)

```
Π(S_T) = N_cr + (K−w_p − S_T)⁺ − (K − S_T)⁺ − (S_T − K)⁺ + (S_T − K−w_c … i.e. (S_T − (K+w_c))⁺)
Max profit = N_cr                       (exactly at S_T = K — a pin, not a band)
Max loss   = max(w_p, w_c) − N_cr       (beyond either wing)
Breakevens = K − N_cr  and  K + N_cr
```

#### Entry / exit / adjustment logic

- Entry: pin/mean-reversion view at K (usually ATM at entry); expiry-day pin plays; the
  defined-risk twin of the short straddle.
- Exit: credit-decay %, SL multiple, breakeven-touch exits; expiry-day gamma management.
- Sweepable axes: center offset (ATM±N — a directional fly is a legitimate cell), wing
  width per side, DTE, exits.

#### Greeks profile

Delta ≈ 0 at entry; gamma/theta/vega as the short straddle in sign but wing-bounded in
magnitude; near expiry the P&L becomes extremely gamma-sensitive around K.

#### Margin nature (India)

NET-OF-HEDGE (both shorts spread-hedged) — U5; expiry-day short-option ELM add-on U7.

#### Risk class

**DEFINED** — `max(w_p, w_c) − N_cr`.

#### Indian-market fit

As §6, with a sharper pin thesis: extremely popular as the defined-risk expiry-day NIFTY /
SENSEX weekly structure. Max profit requires exact pin — realized P&L distribution is
wing-width- and exit-rule-dominated; the sweep must rank on realized exits, not the
theoretical pin (which the worst-case fill law already biases against, CN-3).

### B.8 Call butterfly / put butterfly (long, symmetric)

**Structure.** Same-type 1/−2/+1: long fly = +C(K1) −2C(K2) +C(K3) (or the all-put
mirror), `K2 − K1 = K3 − K2 = w`. Net debit `N_db` (small). Short fly = the negated
vector (credit; payoff = the iron-fly-shaped profile minus wings symmetry — rarely traded
directly in India versus the iron fly, which is its parity twin).

#### Payoff math (per unit, at expiry — long call fly)

```
Π(S_T) = (S_T − K1)⁺ − 2(S_T − K2)⁺ + (S_T − K3)⁺ − N_db
Max profit = w − N_db          (at S_T = K2)
Max loss   = N_db              (S_T ≤ K1 or S_T ≥ K3)
Breakevens = K1 + N_db  and  K3 − N_db
```

Put fly at the same strikes has the identical expiry payoff (parity); the traded choice is
a liquidity/pricing choice. Iron fly (§7) = long fly payoff + a credit/debit parity shift.

#### Entry / exit / adjustment logic

- Entry: cheap pin bet at a forecast settlement level (K2 = target, often OTM — a
  directional fly); classic expiry-day lottery structure at low debit.
- Exit: profit % of max, time stop; flies are near-binary into expiry — most P&L arrives
  in the last hours.
- Sweepable axes: center offset from ATM (the main directional axis), width w, DTE,
  call-vs-put implementation, exits.

#### Greeks profile

Small |delta| (grows with center offset); inside the wings: theta +, gamma −, vega − as
expiry nears when spot is near K2; outside the wings all Greeks ≈ 0 (the position is a
lottery ticket). Long vega when far from the body early in its life — sign flips with
spot location and time; the fly is the canonical "Greeks are local, not global" example.

#### Margin nature (India)

Long fly: net debit; the −2 body is covered 1-each by the wings, so SPAN treats it as two
spreads (NET-OF-HEDGE; effective capital ≈ debit — U5).

#### Risk class

**DEFINED** — the debit (long form). The SHORT fly is also width-bounded
(max loss = w − N_cr) but inherits short-leg margin.

#### Indian-market fit

- Weekly index: high — 50-pt NIFTY grid allows precise pin placement; very low debits on
  0-1 DTE make it the retail expiry lottery. Worst-case fills (CN-3) hit 4-leg debit
  structures hard relative to their small edge — an honest sweep will show that.
- Stock options: workable monthly on liquid names; exit before expiry (U1/U2).

### B.9 Broken-wing butterfly (skip-strike)

**Structure.** A §8 fly with unequal wings, sacrificing one tail's protection distance to
finance the body — canonical put BWB (bullish/neutral): +P(K1) −2P(K2) +P(K3),
`K1 < K2 < K3 ≤ ATM`, near width `w_n = K3 − K2` < far width `w_f = K2 − K1`; target a net
CREDIT `N_cr` (the "no-risk-on-one-side" configuration). Call BWB mirrors above spot.

#### Payoff math (per unit, at expiry — put BWB entered for credit)

```
S_T ≥ K3:            Π = N_cr                       (no upside risk)
S_T = K2 (peak):     Π = w_n + N_cr                 (max profit)
K1 ≤ S_T ≤ K2:       Π = (S_T − K2) + w_n + N_cr    (slope +1 downward through the peak zone… precisely: Π = S_T − (2K2 − K3) + N_cr)
S_T ≤ K1:            Π = N_cr − (w_f − w_n)         (max loss, constant)
Max loss   = (w_f − w_n) − N_cr     [loud: this is the WHOLE skipped width minus credit]
Breakeven  = K2 − w_n − N_cr        (= 2K2 − K3 − N_cr; only one — the credit side has none)
```

If entered for a debit `N_db` instead: upside becomes −N_db (a small risk both sides).

#### Entry / exit / adjustment logic

- Entry: income structure with a directional lean — put BWB collects credit with no risk
  on the up-move; short body placed at support / target delta; skip width chosen so the
  structure fills for a credit.
- Exit: % of max profit; manage when spot enters the peak zone late (gamma), or breaches
  K2; the far tail is the loss maker — tail-stop below K1 region.
- Sweepable axes: body offset, near width, far (skipped) width, credit floor filter, DTE,
  call/put side, exits.

#### Greeks profile

Small directional delta (put BWB slightly long); theta +, vega −, gamma − once spot nears
the body into expiry; beyond the far wing everything flattens (loss capped).

#### Margin nature (India)

NET-OF-HEDGE — both shorts covered, but the wider far wing raises the recognized spread
width (margin ≈ far-side width exposure; numbers U5).

#### Risk class

**DEFINED** — `(w_f − w_n) − N_cr` (or `+ N_db`). Loud caveat: "defined" here can still be
many multiples of the credit; the engine's ranking must use the structural max loss, not
the credit, as the capital denominator.

#### Indian-market fit

- Weekly index: high — a staple weekly-income refinement of §6/§7 on NIFTY/SENSEX; the
  dense grids make skip-strike construction easy.
- Stock options: marginal — wide monthly spreads and the delivery-risk exit law (U1/U2).

### B.10 Call condor / put condor (long, same-type)

**Structure.** +C(K1) −C(K2) −C(K3) +C(K4) (or all-put mirror), equal widths
`K2−K1 = K4−K3 = w`, inner body `K3 − K2` free. Net debit `N_db`.

#### Payoff math (per unit, at expiry — long call condor)

```
Π(S_T) = (S_T−K1)⁺ − (S_T−K2)⁺ − (S_T−K3)⁺ + (S_T−K4)⁺ − N_db
Max profit = w − N_db              (for K2 ≤ S_T ≤ K3)
Max loss   = N_db                  (S_T ≤ K1 or S_T ≥ K4)
Breakevens = K1 + N_db  and  K4 − N_db
```

Parity: the long same-type condor at strikes (K1..K4) has the same expiry payoff shape as
the §6 iron condor shifted by a constant — iron condor = short this range bet as a credit;
long condor = own the range as a debit.

#### Entry / exit / adjustment logic

- Entry: range thesis with defined debit; wider body than the fly trades probability for
  lower max profit.
- Sweepable axes: body placement, body width, wing width, DTE, call/put implementation,
  exits — the same grid as §6 minus the credit-management axes.

#### Greeks profile

As the iron condor with signs flipped where premium flow flips: inside the body theta
works FOR the position only in the sense that the debit converges to `w`; practically
delta ≈ 0, small −vega inside the body near expiry, ± locally near wings.

#### Margin nature (India)

Long condor: NET-OF-HEDGE debit structure (two spreads); capital ≈ debit (U5).

#### Risk class

**DEFINED** — the debit.

#### Indian-market fit

Medium: on weeklies the iron condor (credit form) dominates flow; the debit condor is the
capital-efficient equivalent for accounts without short-option margin access — a relevant
detail for a BruteX future engine because the LONG form is reachable WITHOUT lifting the
BUY-only decision if (and only if) a future DEC widens "buy-only" to "long-net-structures
with no naked short leg" — the −C legs here are spread-covered but are still WRITES at the
exchange level, so DEC-EXEC-008 as written ("never writes / shorts",
`executor.py:38-41`) blocks even this; stated to prevent silent-scope creep.

### B.11 Calendar (horizontal) spread — call and put

**Structure.** Same strike, different expiries: long calendar = −front(K, T1) +back(K, T2),
`T1 < T2`, net debit `N_db` (back costs more). Call and put calendars are economically
close at the same K (parity up to carry); choose by liquidity/moneyness. Reverse (short)
calendar = +front −back for a credit.

#### Payoff math — honest statement

The expiry-payoff at `T1` is **NOT closed-form**: at the front expiry the back leg is a
LIVE option whose value depends on then-spot AND then-IV. Package P&L at T1 per unit:

```
Π(S_T1) = V_back(S_T1, K, T2−T1, σ_back(T1)) − (S_T1 − K)⁺ − N_db      [call calendar]
```

where `V_back` is the back leg's market value — model-dependent (BSM value under the
then-prevailing back-month IV; the repo's shared Greeks/BSM machinery is the natural
valuation seam — `brutex/data/greeks_enrich.py`; see §5.0 G8/G9). P&L drivers instead of a
formula:

1. **Theta differential** — the front decays faster than the back near ATM; the package
   collects the difference while spot stays near K.
2. **Pin** — value at T1 is maximal near `S_T1 = K` (front expires worthless, back retains
   maximal time value) and decays toward both tails, where the two legs converge to
   intrinsic-parallel and the package → small residual (≥ 0 lower-bounded ≈ by European
   same-strike monotonicity in expiry; basis/dividend noise caveat).
3. **Vega / term structure** — net LONG back-month vega: a rise in back IV helps; an IV
   term-structure inversion (front IV spikes, e.g. pre-event) hurts at entry marks but the
   short front leg's crush after the event is the classic event-calendar edge.
- **Long calendar max loss ≈ N_db** (defined) — exact when the package is unwound at/by
  T1; carrying the naked back leg after T1 keeps loss ≤ N_db + settlement/basis noise.
- **Reverse calendar:** credit-bounded profit; after T1 the position is a NAKED SHORT back
  option — **UNDEFINED** unless mandatorily closed at T1 (engine must encode that law).

#### Entry / exit / adjustment logic

- Entry: pin-at-K + IV-term-structure thesis; pre-event short-front (sell the elevated
  weekly, own the monthly); K at ATM or at a directional target.
- Exit: at/just before front expiry (the natural exit — the structure's definition ends at
  T1); early exit on debit-multiple profit; roll the front out (→ a new calendar, a
  discrete re-materialization, CN-7).
- Sweepable axes: K offset, front-expiry slot × back-expiry slot (weekly→next-weekly,
  weekly→monthly, monthly→monthly), DTE pair, exit rules.

#### Greeks profile

Delta ≈ 0 at ATM placement; **vega net +** (back dominates); **theta net +** while spot
near K (front bleeds faster); **gamma net −** near T1 (the front's gamma explodes). The
only structure family in this file whose Greeks are driven by the IV TERM STRUCTURE, which
the current lake does not store as a surface — per-bar `iv` exists per contract
(`executor.py:30-32`), so term structure is derivable per timestamp from two contracts'
stamped IVs; correctness of that derivation is a future-engine validation item.

#### Margin nature (India)

Long calendar: debit + the short front leg is calendar-hedged — SPAN grants a
**calendar-spread margin benefit** for offsetting positions across expiries; that benefit
is **REMOVED on the front leg's expiry day** per the SEBI 2024-10-01 measure series (both
statements UNVERIFIED here — U6; numbers + effective dates owned by the microstructure
agents). Engine consequence even before numbers: margin is TIME-VARYING and jumps on
expiry morning — a margin-aware backtest cannot treat calendar margin as a constant.

#### Risk class

Long: **DEFINED ≈ debit** (with the T1-unwind law stated above).
Reverse: **⚠ UNDEFINED after T1** — must be force-closed at T1 by engine law.

#### Indian-market fit

- Weekly index: rich — NIFTY (Tue) and SENSEX (Thu) weeklies give a weekly-over-weekly and
  weekly-over-monthly ladder; BANKNIFTY calendars are month-over-month only (spec `:108`).
  The expiry-day margin-benefit removal (U6) lands exactly on the natural exit day —
  operationally important.
- Stock options: monthly-over-monthly only (U1); the short front leg breaching ITM into
  expiry triggers physical-settlement exposure (U2) — mandatory pre-expiry close.
- BruteX readiness: FIRST structure requiring multi-EXPIRY leg addressing (CN-1 already
  has the expiry-slot grammar) and a back-leg VALUATION seam at front expiry — a genuinely
  new engine capability (mark-to-model or mark-to-lake-bar of the back leg at T1).
  Mark-to-lake is the honest choice (the back leg's actual T1 bar exists in the lake);
  it needs no pricing model, only CN-2 joint-data gating.

### B.12 Diagonal spread

**Structure.** Different strike AND expiry: canonical long call diagonal ("poor man's
covered call"): +C(K1, T2) deep-ITM back leg, −C(K2, T1) OTM front leg, `K1 < K2`,
`T1 < T2`, net debit `N_db`. Put diagonals mirror (bearish). Short diagonals invert.

#### Payoff math — honest statement

As §11, T1 value is not closed-form (back leg live at T1):

```
Π(S_T1) = V_back(S_T1, K1, T2−T1, σ_back) − (S_T1 − K2)⁺ − N_db
```

Risk bound (long call diagonal, K1 < K2): at T1 the worst outcomes are
(a) collapse: both legs → ~0, loss ≈ N_db; (b) melt-up: package → (K2 − K1) + residual
back time value − front intrinsic beyond K2 is capped by the width — since
`V_back ≥ (S_T1 − K1)⁺ − ε`, the package value at T1 is ≥ (K2 − K1)-ish when S_T1 ≥ K2.
So max loss ≈ `N_db` when `N_db ≤ K2 − K1` — the standard sizing law: **pay less than the
width**. If `N_db > K2 − K1` the excess is genuinely at risk on the upside. State both in
engine gates. (Inverted diagonals — front strike ITM-side of back — carry width risk
`|K2 − K1|` against the trader and must be modeled explicitly; conventions vary and the
engine should refuse ambiguous leg-vectors rather than guess.)

#### Entry / exit / adjustment logic

- Entry: directional carry — own slow-decay back-month delta, finance with fast-decay
  front premium; roll the front every expiry (a repeating calendar of re-materializations).
- Exit: at T1 (roll or close); delta-target breach; debit-recovery target.
- Sweepable axes: back moneyness (deep-ITM depth / delta target), front offset, expiry
  pair, roll cadence, exits.

#### Greeks profile

Net delta + (call form; the point of the trade); theta + (front bleed > back bleed);
vega + (back-month); gamma − near T1 around K2.

#### Margin nature (India)

Debit + cross-expiry offset recognition as §11 (calendar-spread benefit + expiry-day
removal, U6). The short front leg alone would be SPAN+EXPOSURE; the recognized hedge is
what makes the structure capital-viable.

#### Risk class

**DEFINED (= N_db)** when `N_db ≤ |K2 − K1|` arranged; otherwise carries a stated residual
width excess. Loud engine rule: verify the bound at entry, refuse or tag cells violating it.

#### Indian-market fit

- Weekly index: high — the weekly front leg (NIFTY Tue / SENSEX Thu) against a monthly
  back leg is the dominant Indian diagonal; BANKNIFTY diagonals month-over-month only.
- Stock options: monthly-over-monthly diagonals exist but both U1/U2 constraints apply;
  deep-ITM stock back legs are illiquid (tick-floor / stale-bar gates will fire,
  `executor.py:162-176`).

### B.13 Double calendar / double diagonal

**Structure.**
- **Double calendar:** a put calendar at `K_p < S` PLUS a call calendar at `K_c > S`
  (each = −front +back at the same strike, §11). Net debit `N_db`.
- **Double diagonal:** the diagonal version — short front OTM put `K_p2` / long back
  further-OTM put `K_p1`, plus short front OTM call `K_c1` / long back further-OTM call
  `K_c2` — an iron condor whose long wings live in the BACK expiry.

#### Payoff math — honest statement

Not closed-form at the front expiry (both back legs are live — §11's statement applies
twice). Value at T1 is the sum of two §11/§12 packages; the profile is **twin-peaked**
(local maxima near `K_p` and `K_c`, a shallow valley between, decaying tails). P&L
drivers: front-vs-back theta differential across BOTH wings, net long back-month vega,
and spot staying inside the `K_p … K_c` corridor. Long form: max loss ≈ `N_db` (both
sub-packages unwound at T1; same bound + caveats as §11). Double diagonal adds the §12
width-arrangement condition per side.

#### Entry / exit / adjustment logic

- Entry: neutral-range + rising-IV thesis (versus the iron condor's neutral-range +
  falling-IV thesis — the vega sign is the whole difference); classic pre-event corridor.
- Exit: at front expiry (roll or close); corridor-breach management (close the breached
  side, recenter).
- Sweepable axes: per-side strike offsets, expiry pair, per-side diagonal widths (double
  diagonal), DTE pair, exits.

#### Greeks profile

Delta ≈ 0; vega +; theta + inside the corridor; gamma − near T1 around either strike.
The vega-positive income structure — unique among the credit-shaped profiles here.

#### Margin nature (India)

Long form is a debit package of two cross-expiry hedged pairs — NET-OF-HEDGE with the
calendar benefit + its expiry-day removal per side (U6). Double diagonal's short front
legs are hedged by back longs; recognition specifics UNVERIFIED (U5/U6).

#### Risk class

Long form **DEFINED ≈ debit** (with the §11 T1-unwind law). Short/reverse forms inherit
**⚠ UNDEFINED after T1** as §11.

#### Indian-market fit

- Weekly index: medium-high — weekly-front/monthly-back double calendars on NIFTY/SENSEX
  are standard pre-event trades; BANKNIFTY month-over-month only (spec `:108`).
- Stock options: low — two expiry slots × two strikes on monthly, thinner chains,
  physical-settlement exit law (U1/U2), and banded lake coverage across FOUR legs makes
  CN-2 joint-data gating the binding constraint.
- BruteX readiness: the most data-hungry structure so far — 4 legs × 2 expiries; the CN-2
  all-legs conjunction over the banded chain will honestly reject many candidate cells;
  the sweep must COUNT those rejections (the executor's counted-reject idiom,
  `executor.py:155-184`) rather than silently thin the sample.

### B.14 Ratio call spread / ratio put spread (front-ratio)

**Structure.**
- **Call front-ratio:** +1 C(K1) −2 C(K2), `K1 < K2` (typically ATM long, OTM shorts);
  net premium ≈ 0 or a small credit `N_cr` (or small debit `N_db`).
- **Put front-ratio:** +1 P(K2) −2 P(K1), `K1 < K2` (ATM long put, OTM shorts below).
Ratio `1×2` is canonical; `2×3` and other fronts generalize (qty vector axis).

#### Payoff math (per unit, at expiry — call front-ratio, credit form)

```
S_T ≤ K1:        Π = +N_cr                          (or −N_db)
K1 ≤ S_T ≤ K2:   Π = (S_T − K1) + N_cr              (rises to the peak)
S_T = K2:        Π = (K2 − K1) + N_cr               (max profit)
S_T ≥ K2:        Π = (K2 − K1) + N_cr − (S_T − K2)  (falls 1:1 — one naked short)
Upper breakeven  = 2K2 − K1 + N_cr                  (beyond it: UNBOUNDED loss)
Max loss         = UNBOUNDED (upside)               [call form]
```

Put front-ratio mirror (credit form): max profit `(K2 − K1) + N_cr` at `S_T = K1`; loss
grows 1:1 below `2K1 − K2 − N_cr` down to `S_T = 0`, i.e. bounded only by the zero floor:
`Π(0) = N_cr + K2 − 2K1` (a catastrophic magnitude for index levels — treat as
UNDEFINED-in-practice).

#### Entry / exit / adjustment logic

- Entry: "move to the short strike but not through it" — a directional pin thesis financed
  by the extra short; often entered for zero cost so the wrong-way small move costs nothing.
- Exit: profit at/near the peak zone into expiry; hard SL beyond the naked breakeven;
  convert to a butterfly by buying the `2K2 − K1` wing (defines the risk — a natural
  adjustment the engine can model as a discrete transition).
- Sweepable axes: long offset, short offset, ratio (1×2, 1×3, 2×3), credit/debit filter,
  DTE, exits, defined-risk conversion rule.

#### Greeks profile

Entry delta small (call form: long delta from K1 leg, short from 2×K2); net SHORT gamma
and vega (the extra short dominates), theta +. The Greeks are regime-dependent: below K1
the position is nearly flat; between strikes it behaves like a covered directional; beyond
K2 it is a naked short call plus a deep spread.

#### Margin nature (India)

SPAN+EXPOSURE: one short is spread-hedged, the EXCESS short is naked — margin ≈ one
spread + one naked short (U5). Expiry-day ELM add-on applies to the naked short (U7).

#### Risk class

Call front-ratio: **⚠ UNDEFINED — UNLIMITED upside loss.**
Put front-ratio: **⚠ UNDEFINED-in-practice** (bounded only by S→0).
Loud rule for a future engine: the naked-excess quantity must be a first-class field so
risk-class labeling is structural (`Σ qty per side beyond hedges ≠ 0 ⇒ UNDEFINED`).

#### Indian-market fit

- Weekly index: used by pros for pin/target plays on NIFTY/SENSEX weeklies; gated hard by
  CN-4 + margin. BANKNIFTY monthly-only.
- Stock options: very poor — naked stock-option shorts + physical settlement (U1/U2) +
  monthly gap risk; also the censoring hazard (CN-5) is maximal for the naked excess leg.

### B.15 Call backspread / put backspread

**Structure.** The reverse ratio:
- **Call backspread:** −1 C(K1) +2 C(K2), `K1 < K2` (short the nearer/ITM-er, own 2× the
  farther), ideally net credit `N_cr`.
- **Put backspread:** −1 P(K2) +2 P(K1), `K1 < K2`, ideally net credit.

#### Payoff math (per unit, at expiry — call backspread, credit form)

```
S_T ≤ K1:        Π = +N_cr
K1 ≤ S_T ≤ K2:   Π = N_cr − (S_T − K1)              (falls into the valley)
S_T = K2:        Π = N_cr − (K2 − K1)               (max loss — the valley floor)
S_T ≥ K2:        Π = N_cr − (K2 − K1) + (S_T − K2)  (rises 1:1, UNBOUNDED profit)
Upper breakeven  = 2K2 − K1 − N_cr
Max loss         = (K2 − K1) − N_cr                 (DEFINED)
```

Put backspread mirror: valley at `K1`, max loss `(K2 − K1) − N_cr`, profit grows 1:1 below
`2K1 − K2 + N_cr` toward `S_T = 0` (near-unbounded for a crash), small credit kept above
`K2`.

#### Entry / exit / adjustment logic

- Entry: explosive-move thesis with direction (put backspread = crash hedge that costs
  nothing if wrong-way slow); low-IV regimes (net long vega).
- Exit: time stop well before expiry (the valley is a pure expiry artifact — mark-to-market
  loss concentrates as T→0 near K2); profit target on the runner.
- Sweepable axes: short offset, long offset, ratio (1×2, 2×3), credit filter, DTE, exits.

#### Greeks profile

Net LONG gamma and vega (2 longs dominate), theta −; delta: call form net + (grows on
rallies), put form net −. The defined-risk long-vol directional — the "anti-ratio".

#### Margin nature (India)

The short leg is fully hedged by the 2 longs — NET-OF-HEDGE (recognition specifics U5);
capital ≈ valley risk. Materially lighter than §14.

#### Risk class

**DEFINED** — `(K2 − K1) − N_cr` (or `+ N_db`) at the valley; unlimited profit on the
long side. Honest caveat: the SHAPE punishes the most common outcome (a slow drift to K2
at expiry) — the sweep will surface that; expectancy, not the risk label, is the hurdle.

#### Indian-market fit

- Weekly index: put backspreads on NIFTY weeklies are a recognized crash-convexity trade;
  weekly theta bleed is brutal — DTE axis decisive.
- Stock options: low fit — monthly-only, and the short leg near ITM at expiry drags in
  physical settlement (U1/U2).

### B.16 Jade lizard / big lizard

**Structure.**
- **Jade lizard:** −1 P(K1) −1 C(K2) +1 C(K3), `K1 < K2 < K3` (short OTM put + short call
  spread above), constructed so `N_cr > K3 − K2` ⇒ **zero upside risk**.
- **Big lizard:** the straddle version — −1 P(K) −1 C(K) +1 C(K+w) (short ATM straddle +
  long call wing), same construction rule `N_cr > w`.

#### Payoff math (per unit, at expiry — jade lizard)

```
S_T ≥ K3:        Π = N_cr − (K3 − K2)               (≥ 0 by construction — no upside risk)
K2 ≤ S_T ≤ K3:   Π = N_cr − (S_T − K2)
K1 ≤ S_T ≤ K2:   Π = N_cr                           (max profit plateau)
S_T ≤ K1:        Π = N_cr − (K1 − S_T)              (naked-put slope, to S_T = 0)
Lower breakeven  = K1 − N_cr
Max loss         = K1 − N_cr at S_T = 0             (UNDEFINED-in-practice downside)
```

Big lizard: same algebra with `K1 = K2 = K`: plateau collapses to the point `S_T = K`
region between… precisely `Π = N_cr − |S_T − K|` below K3 with the call-spread cap above;
upside risk zero iff `N_cr > w`; downside naked-straddle-put slope from K.

#### Entry / exit / adjustment logic

- Entry: high-IV, mildly-bullish income; the defining entry FILTER is the credit test
  `N_cr > call-spread width` — a structural gate the engine can enforce exactly at entry.
- Exit: % of credit; tested-put management (roll down/out); the upside needs none (that is
  the design).
- Sweepable axes: put offset, call-spread placement + width, credit-test slack, DTE, exits.

#### Greeks profile

Delta mildly + at entry (short put dominates); theta +, vega −, gamma −. Below K1 the
profile IS a naked short put — all short-put tail Greeks apply.

#### Margin nature (India)

SPAN+EXPOSURE on the naked put; the call side is spread-hedged (NET-OF-HEDGE). Net margin
≈ naked-put margin (U5); expiry-day ELM add-on (U7).

#### Risk class

Upside: **DEFINED (zero risk when the credit test holds — verify at entry, never assume).**
Downside: **⚠ UNDEFINED-in-practice** (naked put to zero). The mixed label must be carried
per-side in engine metadata, not collapsed to one flag.

#### Indian-market fit

- Weekly index: practical on NIFTY/SENSEX weeklies for the income+skew expression; the
  credit test is harder to satisfy on low-IV weeks (the sweep will show the regime
  dependence). BANKNIFTY monthly-only.
- Stock options: very poor — a naked stock put held toward expiry is the canonical
  physical-delivery trap (assignment → take delivery of the full lot value; U1/U2).

### B.17 Strap / strip

**Structure.** Ratioed straddles, same strike + expiry:
- **Strap (bullish vol):** +2 C(K) +1 P(K), debit `N_db = 2C + P`.
- **Strip (bearish vol):** +1 C(K) +2 P(K), debit `N_db = C + 2P`.

#### Payoff math (per unit, at expiry — strap)

```
Π(S_T) = 2(S_T − K)⁺ + (K − S_T)⁺ − N_db
Upside:   slope +2 → breakeven K + N_db/2 ; unbounded profit
Downside: slope −1… i.e. profit (K − S_T) − N_db → breakeven K − N_db ; max K − N_db at S_T = 0
Max loss = N_db at S_T = K
```

Strip mirror: breakevens `K + N_db` (up, slope +1) and `K − N_db/2` (down, slope 2).
Note the paisa-exactness caveat for `N_db/2` (§1.2 money convention).

#### Entry / exit / adjustment logic

As the long straddle (§1) with a directional tilt on the vol thesis (strap: "a move is
coming and it is probably up"). Sweepable axes: §1's plus the ratio itself (2:1, 3:1, 3:2).

#### Greeks profile

Strap: delta + (≈ +0.5 net at ATM), gamma +, vega +, theta − (1.5× a straddle's bleed per
unit… strictly: the bleed of three ATM options). Strip mirrors with delta −.

#### Margin nature (India)

PREMIUM-ONLY (all long).

#### Risk class

**DEFINED** — the (large) debit.

#### Indian-market fit

Medium on weeklies (three-leg debit + worst-case fills = heavy cost drag, CN-3); feasible
on stock options pre-event with exit-before-expiry (U1/U2). Engine-wise trivial once §1
exists (a qty-vector generalization of the straddle — the ratio is just a leg-qty axis).

### B.18 Christmas tree / ladder

Two distinct families that get conflated; both stated, labels loud.

#### B.18a Christmas tree butterfly (defined)

**Structure (call form, canonical skip-strike tree):** +1 C(K1), −3 C(K3), +2 C(K4) with
equal grid spacing `w` and a skipped strike at `K2` (`K3 = K1 + 2w`, `K4 = K1 + 3w`).
Net debit `N_db`. Put trees mirror below spot. Quantities sum to zero ⇒ no naked excess.

##### Payoff math (per unit, at expiry)

```
S_T ≤ K1:        Π = −N_db
K1 ≤ S_T ≤ K3:   Π = (S_T − K1) − N_db
S_T = K3:        Π = 2w − N_db                      (max profit)
K3 ≤ S_T ≤ K4:   Π = 2w − N_db − 2(S_T − K3)        (slope −2)
S_T ≥ K4:        Π = −N_db                          (flat — the +2 wing caps it; verified:
                 (S−K1) − 3(S−K3) + 2(S−K4) = −K1 + 3K3 − 2K4 = 0 at equal spacing)
Max loss = N_db (both tails) ; breakevens K1 + N_db and K3 + (2w − N_db)/2 = K4 − (N_db)/2 … exactly: S = K3 + (2w − N_db)/2
```

(The `/2` breakeven inherits the §1.2 paisa-exactness caveat.)

Risk class: **DEFINED** (= debit). Margin: NET-OF-HEDGE debit package (U5). Greeks: a
skewed butterfly — pin Greeks concentrated at K3. Use: directional pin at K3 with a wider
profit ramp than a symmetric fly; sweep axes = anchor offset, spacing, skip pattern,
call/put form, DTE, exits.

#### B.18b Ladder (a.k.a. Christmas tree ladder) — ⚠ UNDEFINED on one side

**Structure (long call ladder):** +1 C(K1) −1 C(K2) −1 C(K3), `K1 < K2 < K3`; net ≈ 0 or
small debit/credit `N`. (The "short call ladder" is the negation — economically a §15-style
backspread with three strikes: defined valley, unbounded upside profit.)

##### Payoff math (per unit, at expiry — long call ladder, net premium N signed: credit +)

```
S_T ≤ K1:        Π = N
K1 ≤ S_T ≤ K2:   Π = N + (S_T − K1)
K2 ≤ S_T ≤ K3:   Π = N + (K2 − K1)                  (plateau = max profit)
S_T ≥ K3:        Π = N + (K2 − K1) − (S_T − K3)     (slope −1 — one naked short call)
Upper breakeven  = K3 + K2 − K1 + N
Max loss         = UNBOUNDED above                   [put-ladder mirror: to S_T = 0 below]
```

Risk class: **⚠ UNDEFINED** on the naked side — the ladder is a ratio spread wearing a
third strike; the same CN-4/CN-5 gating and the §14 naked-excess structural label apply.
Margin: one spread + one naked short (SPAN+EXPOSURE, U5/U7). Greeks: plateau-zone theta +,
tail-zone short gamma. Indian fit: pro-only weekly-index structure; unfit for stock
options (naked short + physical settlement, U1/U2).

**Engine recommendation for the whole §18 family:** represent trees/ladders as generic
leg-qty vectors, not named structures — the naming is presentation; the risk classifier
(naked-excess detector) and the payoff evaluator work on the vector. That collapses
§8/§9/§10/§14/§15/§18 into one parameterized structure grammar with per-cell risk labels —
the DYNAMIC rule of CLAUDE.md §2 Rule 11 applied to structures. (Part C's classification
scheme, §5.8, is exactly that grammar.)

---

## 5. Part C — Dynamic families, adjustment machinery & the engine classification scheme

Dynamic families (delta-neutral short premium, gamma scalping, dispersion,
0DTE / expiry-day, event-vol), the adjustment-framework state machine, the
consolidated Greeks matrix, and the classification scheme. Internal `§a`…`§i`
= subsections 5.1–5.9; `G*n*` = the §5.0 table; `ext-*n*` = §5.8.3; `U*n*` =
register row C-U*n* (§6).

### 5.0 Ground truth this part builds on (repo-verified primitives)

Every design statement in this part is anchored to machinery that already exists. This table is the load-bearing evidence base; each row was verified by reading the cited file at the cited line.

| # | Primitive | State today | Evidence (path:line) |
|---|---|---|---|
| G1 | Engine scope | SPOT-ONLY; `NSE-NIFTY-FUT` retired from engine scope | `brutex/universe.py:379` (`is_engine_sweep_symbol`) |
| G2 | Options executor | BUY-only (`long_call`/`long_put`), "never writes / shorts. Max loss = premium paid; no SPAN/ELM margin modelling" (DEC-EXEC-008) | `brutex/options/executor.py:38-41` |
| G3 | Option **selling** | Explicitly BACKLOGGED, "NOT AUTHORIZED" — recorded operator thesis: sell intraday premium spikes with hard SL/TP, square-off by close; honest risk profile "many small wins + rare brutal continuation-day losses (negative skew)" | `docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:1-20` |
| G4 | Strike-selection vocabulary | 10 locked pre-trade rules: 7 offset rules (`atm`, `atm_plus_{1,2,5}_in_dir`, `atm_minus_{1,2,5}_in_dir`) + 3 delta-target rules (`delta_target_030/050/015`) | `brutex/options/strike_rules.py:10-21,112-114` |
| G5 | Expiry-tier vocabulary | `near`/`next`/`far` tiers (`_EXPIRY_TIERS = {"near":0,"next":1,"far":2}`) × `all`/`weekly`/`monthly` tracks; `monthly` = last-in-calendar-month law | `brutex/dashboard/movers_options.py:204,216,956` |
| G6 | Expiry-weekday regimes | Per-underlying dated registry 2020-2026 incl. BANKNIFTY weekly KILLED EOD 2024-11-13 (`(date(2024,11,14), NO_WEEKLY)`), NIFTY weekly → Tuesday 2025-09-02, SENSEX Friday→Tuesday (2025-01)→Thursday (2025-09) | `brutex/options/expiry_calendar.py:103,113,126-127` |
| G7 | Lot-size history | Dated table incl. 2024-11-20 SEBI/HO/MRD-PoD2/CIR/P/2024/00181 step-up (NIFTY 25→75, BANKNIFTY 15→30, SENSEX 10→20) | `brutex/options/lot_size_history.py:10-18` |
| G8 | Greeks kernel | Shared Rust Black-76; `GreeksResult` = iv, delta, gamma, theta (per calendar day, Act/365), vega (per vol-point), rho, price + admission floors (`abs(delta) >= 0.02`, vega `>= 1e-6`); hard T-floor: refuses below 600 s to expiry (honest `None`, never fabricated) | `brutex/options/greeks.py:1-38,66-68,98-128` |
| G9 | Per-bar Greeks overlay | Ten nullable columns stamped on stored option bars: `iv, delta, gamma, theta, vega, rho, spot_at_bar, t_years_used, rate_used, greeks_provenance_id` | `brutex/data/models.py:156-167` |
| G10 | Exercise-style handling | INDEX → European exact; CASH single stock → American **flagged approximation** (`STYLE_AMERICAN_APPROX`) | `brutex/data/greeks_enrich.py:31-32` |
| G11 | Moneyness / ATM offset | Read-time `classify` + `atm_offset` + `derive_strike_step` (strike step DERIVED from the real ladder — works for all ~212 stocks + indices, never hardcoded) | `brutex/data/moneyness.py:1-22,106,238` |
| G12 | Option lake | Per-CONTRACT 1-minute bars at `bars/<exchange>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet`, OHLCV + per-bar `open_interest` | `brutex/data/fno_candle_lake.py:7-13`; `brutex/data/models.py:39,102-104` |
| G13 | All-strikes pull | Contract resolution via `get_expiries` → `get_contracts` fetches **all listed strikes, not relative-to-spot** (the winner-censoring hazard of banded sources is structurally avoided for the current lake) | `CLAUDE.md:12574` (§6 row); hazard: `TRACK2_OPTION_SELLING_BACKLOG.md:32-36` |
| G14 | Options are 1-minute-NATIVE | Derived-TF prebuild retired for CE/PE (`is_option_contract_coords` gate; DEC-OPTIONS-PREBUILD-RETIRE-001) | `brutex/data/fno_candle_lake.py:66` |
| G15 | Fill law | WORST-CASE is the ONLY fill model; `FillMode.WORST_CASE = "worst_case"`, emitted unconditionally into `canonical_trade_params` | `brutex/execution/fill_mode.py:33,60-72` |
| G16 | Sweep axes today | `SweepGridConfig` — "seven sweep axes (instrument × timeframe × direction × K × stop-loss × take-profit × proximity)"; SL/TP are `Sequence[int | None]` paisa axes | `brutex/engine/sweep_orchestrator.py:184-194,279-280` |
| G17 | Run identity | `blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)` — every new strategy dimension MUST enter `canonical_params` | `CLAUDE.md:10845` (§2 Rule 4) |
| G18 | Session mechanics | Forced exit 15:20 IST regular day / close−10min half-day, timeframe-aware | `brutex/engine/trade_sim.py:50-53,260-262`; `CLAUDE.md:12609` |
| G19 | Margin truth | SPAN margin regime table has **ZERO verified rows** — every date raises `UnverifiedMarginRegimeError`; `TRACK2_RISK_MGMT.md` §2.3 forbids hard-coded margin constants | `brutex/engine/margin_history.py:1-27` |
| G20 | Cost regimes (options) | STT on sell-side premium: 0.0625% pre-2024-10-01 → 0.10% 2024-10-01 → 0.15% 2026-04-01; exercise STT 0.125%→0.15% on intrinsic; premium tick ₹0.05 | `brutex/costs/constants.py:36,57-95` |
| G21 | Cost regimes (futures hedge) | Futures STT 0.02% (sell-side) eff. 2024-10-01 → 0.05% eff. 2026-04-01; pre-2024-10-01 futures STT is a REFUSAL window | `brutex/costs/futures_costs.py:27-30,43` |
| G22 | Event-day flags | ONLY four calendar-verifiable classes exist: `expiry`, `muhurat`, `budget_weekend`, `nifty_rebalance`; weekday Budget + election days "tagged NOWHERE in the repo and are honestly OMITTED" | `brutex/engine/session_lens.py:17-27,88-131` |
| G23 | India VIX | `NSE-INDIAVIX` stored; REFERENCE ONLY, never a signal input to the spot engine | `brutex/adapters/groww/constants.py:103`; CLAUDE.md §2 Rule 3 |

**Reading of G2+G3 together (design consequence):** every SHORT-side family in this part is doubly gated — the executor's BUY-only law AND the selling backlog's not-authorized status. The taxonomy below treats short families as first-class *classification* citizens (the scheme must express them) while their *activation* remains a future DEC. That is exactly the posture the backlog doc itself demands (`TRACK2_OPTION_SELLING_BACKLOG.md:3-7`).

### 5.1 (a) Delta-neutral short-premium families

**Definition.** Sell optionality (short straddle / short strangle / short-vol composites), then keep the book's net delta inside a band by re-hedging with a linear instrument (futures) or with options. P&L engine: collect theta + short vega; pay realized gamma (the exact identity is §b — a short book earns `implied − realized` variance, dollar-gamma-weighted).

#### a.1 Family members

| Family | Legs at inception | Hedge overlay | Net Greeks at inception (Δ/Γ/Θ/V) |
|---|---|---|---|
| Delta-hedged short straddle | S·CE\@ATM + S·PE\@ATM | futures or options | ~0 / −− / ++ / −− |
| Delta-hedged short strangle | S·CE\@Δ0.15-0.30 + S·PE\@Δ0.15-0.30 | futures or options | ~0 / − / + / − |
| Delta-managed iron condor | strangle + long wings | usually options-only repair | ~0 / − / + / − (defined) |
| Short-straddle-with-futures-collar | straddle + standing futures band | futures | ~0 managed |

#### a.2 The parametric re-hedge rule set (what an engine would sweep)

All of these are *scalar axes* in the same sense as today's SL/TP axes (G16) — enumerable, dedupable, hashable into `canonical_params` (G17):

| Knob | Type | Example sweep values | Notes |
|---|---|---|---|
| `hedge_instrument` | enum | {`FUT`, `OPTION_VERTICAL`, `OPTION_ATM`, `NONE`} | `NONE` degenerates to the static short family |
| `delta_band` | float (fraction of one lot's delta, or absolute futures-equivalent delta) | 0.10, 0.25, 0.50, 1.0 lot-deltas | trigger: re-hedge when abs(net Δ) > band |
| `rehedge_cadence` | enum/int | every-bar, every N minutes, band-only | band-only vs time-based is a classic variance/cost trade-off |
| `hedge_size_rule` | enum | round-to-nearest-lot, round-toward-flat | futures hedge is quantized to whole lots (G7) — see a.4 |
| `rehedge_cutoff` | time | e.g. no new hedges after 15:00 | interacts with the 15:20/expiry exit law (G18, §d) |

#### a.3 Hedging with futures vs options (decision table)

| Criterion | Futures hedge | Options hedge |
|---|---|---|
| What it neutralizes | delta only (gamma/vega untouched) | delta + partially gamma/vega (buying back optionality) |
| Cost model in repo | encoded: futures STT 0.02%/0.05% regimes + brokerage (G21) — but pre-2024-10-01 futures STT is a REFUSAL window, so a delta-hedged backtest crossing that boundary is date-partitioned by cost verifiability | options STT sell-side regimes (G20) |
| Granularity | whole lots only — post-2024-11-20 NIFTY lot 75 (G7) ⇒ minimum hedge step ≈ 75 × spot notional; residual delta floor is structural | any listed strike; finer delta granularity via strike choice |
| Liquidity/slippage | front-month index futures deep | strike-dependent |
| Effect on the short-premium thesis | preserves it (short gamma/vega intact) | dilutes it (gives back premium) |
| Worst-case-fill interaction | each re-hedge is a leg fill at the bar's adverse extreme under G15 — frequent re-hedging compounds pessimism; this is conservative, never optimistic (same posture as DEC-FILL-WORST-CASE-ONLY-001's interim law) | same, per option leg |

#### a.4 Honest limits specific to this family class

1. **Margin-relative returns are UNCOMPUTABLE today.** Short premium is a margin-consuming business; the repo's SPAN table has zero verified rows and raises for every date (G19). An engine can compute absolute paisa P&L honestly, but ROI-on-margin must render `status="unverified"` until a SPAN-archive integration lands (its own DEC). Do not design leaderboards that rank short-premium families on ROI. (See `09-capital-margin-and-sizing.md`.)
2. **Lot quantization floors the hedge quality.** With NIFTY lot 75 (post-2024-11-20, G7), one lot of futures delta = 75 index-deltas; a 1-lot short straddle (±75 max delta per side) can only be hedged in 100%-of-position steps with futures. Multi-lot position sizes are effectively REQUIRED for banded futures hedging to be meaningful — which raises capital and margin (again G19).
3. **Gap risk is unhedgeable in-model.** Overnight variants carry gap risk that no delta band addresses; intraday-only variants (square-off before close) trade it away for theta-capture truncation. Both variants must be distinct `management`-dimension tokens (§h), not post-hoc filters.
4. **Re-hedge P&L needs 1-minute spot AND futures series.** Spot 1m exists (engine lake); futures 1m per contract exists in the F&O lake (G12, `FUT` contracts); the classification must record which series the hedge fills read.

### 5.2 (b) Gamma scalping (long-vol book + re-hedge harvest)

**Definition.** Buy optionality (long straddle/strangle/single option), delta-hedge it, and harvest the difference between the volatility the market *realizes* and the volatility you *paid* (implied).

#### b.1 The realized-vs-implied P&L identity — exact statement

For a delta-hedged option position priced and hedged on a Black-Scholes/Black-76 surface at constant implied volatility σ_imp, with the underlying following any (sufficiently regular) diffusion whose instantaneous realized volatility is σ_r(t), the hedged book's instantaneous P&L is:

```
dΠ(t) = ½ · Γ(t) · S(t)² · ( σ_r(t)² − σ_imp² ) · dt        (continuous re-hedging, frictionless)
```

integrated over the holding period:

```
Π(T) = ∫₀ᵀ ½ · Γ(t) · S(t)² · ( σ_r(t)² − σ_imp² ) · dt
```

i.e. **P&L = the dollar-gamma-weighted spread of realized variance over implied variance**. In the continuous-time setting this is the robustness result of El Karoui, Jeanblanc-Picqué & Shreve (1998), "Robustness of the Black and Scholes formula" — it holds without assuming the *true* dynamics are Black-Scholes ([Artur Sepp, "How to optimize volatility trading and delta-hedging strategies…"](https://artursepp.com/2017/05/01/how-to-optimize-volatility-trading-and-delta-hedging-strategies-under-the-discrete-hedging-with-transaction-costs/); [Gatheral, "Volatility and Hedging Errors", 1999](https://www.math.columbia.edu/~smirnov/Gatheral.ppt); [Freeport Logbook, "Hedging Errors & Options PnL"](https://freeportlogbook.substack.com/p/hedging-errors)).

**Discrete (per-re-hedge-step) form** — the form a bar-driven backtest actually computes. Over hedge interval Δt with simple return r_i = ΔS_i/S_i:

```
Π_step,i ≈ ½ · Γ_i · S_i² · ( r_i² − σ_imp² · Δt )
```

Summed over steps this converges to the integral form as Δt → 0; at finite Δt a *hedging-error noise term* remains (zero-mean under the model, variance shrinking with re-hedge frequency) plus transaction costs growing with frequency — the fundamental cadence trade-off (same sources).

**Honest caveats on the identity (must be carried into any engine doc):**
- It assumes hedging at the *implied-surface* delta with a *constant* σ_imp; a moving IV adds a vega term `∂Π = V·dσ_imp` on top (marking-to-market), so a real backtest decomposes P&L into gamma-vs-theta harvest + vega mark + hedge noise + costs — four attributions, not one number.
- Rates/carry terms are suppressed above (Black-76 forward form absorbs most of it); the repo kernel is Black-76 with an explicit stamped rate (`rate_used`, G9), so the engine's attribution can be exact w.r.t. its own model.
- Discrete hedging on 1-minute bars under the worst-case fill law (G15) makes measured hedge noise systematically pessimistic — acceptable (conservative), but it biases gamma-scalping backtests AGAINST profitability; that bias must be disclosed on any result surface.

#### b.2 Engine-relevant structure

| Knob | Values | Note |
|---|---|---|
| book | long straddle / long strangle / single long option | G4 selectors pick strikes |
| entry gate | IV-level vs realized-vol estimate (e.g. India VIX percentile, trailing realized σ) | India VIX is stored (`NSE-INDIAVIX`, G23) but REFERENCE_ONLY — using it as an ENGINE INPUT is a scope decision for the future options DEC |
| re-hedge rule | same table as §a.2 | shared machinery — one implementation (CLAUDE.md §11 COMMON) |
| harvest accounting | per-step attribution (gamma harvest / theta bleed / vega mark / costs) | per-bar Γ, Θ, V available from the stored overlay (G9) |

**Data feasibility (repo-verified):** per-bar Γ/Θ/V/iv exist as stored overlay columns on every enriched option bar (G9); spot 1m + futures 1m exist (G12). The only missing input for entry gates is a *sanctioned* IV-regime series (VIX is reference-only, G23). Below 600 s to expiry the kernel refuses (G8) — a gamma-scalping engine must fall back to price-only logic in the final minutes (see §d).

### 5.3 (c) Dispersion (qualitative)

**Definition.** Trade index volatility against constituent volatility: classically SHORT index vol (index variance is dampened by imperfect correlation) and LONG single-name vol, profiting when realized correlation comes in below implied correlation — or the reverse trade when correlation is priced too low. Index variance ≈ Σᵢ wᵢ²σᵢ² + Σᵢ≠ⱼ wᵢwⱼσᵢσⱼρᵢⱼ, so the index-vs-constituents vol spread IS a correlation position.

**Why it is only a qualitative entry in this taxonomy (single-account India feasibility limits):**

1. **Leg explosion.** A faithful NIFTY dispersion book is 1 index structure + up to 50 constituent structures, re-struck as spots move — a *portfolio of families*, not a family. In the §h scheme it is the ONE entry that requires a per-leg UNDERLYING dimension (everything else is single-underlying); it is classified as a COMPOSITE meta-family.
2. **Stock-option expiry cadence.** Single stocks have one expiry per month — the repo's own structural law observes "a single stock or a weekly-less index has one expiry per month" (`brutex/dashboard/movers_options.py:85-87`) — while the index leg (NIFTY) has weeklies; tenor-matching forces the monthly tier on both legs.
3. **Liquidity concentration.** Stock-option volume concentrates near ATM in the top names (the backlog doc's spike-scan thesis exists precisely because of this concentration — `TRACK2_OPTION_SELLING_BACKLOG.md:11-13`); deep replication across 50 names is not a retail-liquidity trade. UNVERIFIED beyond that qualitative statement (register item U6).
4. **Position-limit machinery.** Single-stock derivatives are subject to market-wide position limits (MWPL) with trading-ban regimes; index options have different limit regimes. Exact current limits: UNVERIFIED here (register item U5) — a dispersion DEC would need the NSE/SEBI limit framework verified first.
5. **Margin.** A 50-name short-vol/long-vol book is margin-intensive and the repo cannot price margin at all today (G19).

**Engine posture recommendation:** classify (so the scheme is closed under it), do NOT design for activation in the first options-engine generation. A cheap proxy sub-family that IS single-underlying — "index-vs-single-name vol pair" (1 index leg + 1 stock leg) — fits the scheme without the composite extension and could serve as the research on-ramp.

### 5.4 (d) Expiry-day / 0DTE families

See also the empirical companion `11-expiry-day-tail-study/STUDY.md`.

#### d.1 Members

| Family | Structure | Session window | Thesis |
|---|---|---|---|
| Expiry-day short straddle | S·CE@ATM + S·PE@ATM, DTE=0 | entry near open or fixed clock (e.g. 09:20) | harvest the day's final theta + the intraday IV collapse |
| Expiry-day short strangle | S·CE@Δlow + S·PE@Δlow, DTE=0 | same | same, wider break-evens, smaller credit |
| Expiry-day iron fly | short straddle + long wings, DTE=0 | same | defined-risk version; wing cost eats edge |
| Last-hour theta play | short ATM straddle/strangle entered ~14:15-14:45, DTE=0 | final hour | premium decays to intrinsic by settlement |
| Pin-risk fade | short straddle at the dominant-OI strike, last hour | final hour | bet that price "pins" near the heavy-OI strike; the pinning EDGE itself is contested — treated purely as a parameterizable hypothesis (register item U7) |
| 0DTE long gamma ("lottery") | L·CE or L·PE far-OTM, DTE=0, last hours | event/breakout driven | convex payout on a late-day trend; expected-value negative most days |

#### d.2 Why the last ~30 minutes are a special regime (this is load-bearing for engine design)

1. **Greeks blow up, then the model refuses.** ATM Black-76 gamma scales like 1/(S·σ√T) → ∞ as T→0 at the money; delta becomes a step function across the strike. The repo kernel *already* encodes the honest boundary: below the 600 s T-floor it returns `None`, never a fabricated Greek (`brutex/options/greeks.py:66-68`), and the enrichment docs measure theta-approximation error "~−4.7% relative on the LAST tradable expiry-day minutes, ~−0.8% at 1 h" with T going non-positive at/after the expiry instant (`brutex/data/greeks_enrich.py:471-479`). **Design rule: every 0DTE family's final-minutes logic must be PRICE-based (premium vs intrinsic), not Greeks-based — the Greeks columns are legitimately NULL there.**
2. **Delta-band hedging becomes ill-posed.** With gamma exploding, any finite delta band is crossed on almost every bar near the strike; re-hedge counts (and worst-case-fill costs, G15) diverge. Engines must cap re-hedges (`max_rehedges`) or switch to a terminal regime (hold/close) in the final window — an explicit `terminal_regime` token in the management dimension (§h).
3. **Settlement + STT mechanics change behavior.** ITM options held to expiry incur exercise STT on INTRINSIC value (0.125% → 0.15% from 2026-04-01; `brutex/costs/constants.py:82-88`) vs sell-side STT on PREMIUM if squared off — a real, encoded cost discontinuity that pushes rational longs to square off, thinning the final book. Index options settle European-style (INDEX → European exact in the repo's style machinery, `brutex/data/greeks_enrich.py:31-32`); cash settlement of index options at the closing settlement price is standard practice but its exact procedure is not encoded in-repo (register item U8).
4. **Pin risk splits by settlement type.** For cash-settled index options, "pin risk" is settlement-price uncertainty (P&L risk only). For single-stock (deliverable) options it is assignment/delivery risk — a different risk class entirely; the Track-2 spec's expiry state machine is an acknowledged fail-loud STUB today (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 records "§4.2 expiry state machine honestly labelled a fail-loud STUB in-spec" — CLAUDE.md §1 Earlier chain, MIGRATION.md #1552-#1556). An options engine that holds positions into expiry CANNOT ship before that state machine is real.
5. **Margin spikes on exactly this day.** SEBI added a 2% additional Extreme Loss Margin on short options positions on their expiry day, effective 2024-11-20 (web-verified — see §i sources). Backtests spanning 2024-11-20 must model a margin-regime break on expiry days (currently impossible to quantify in-repo, G19 — disclose, don't fabricate).
6. **The engine's session-exit law is a design fork.** The spot engine forces exit at 15:20 IST (G18); expiry settlement references the close (~15:30). An options-expiry engine needs its own exit-instant law per family (`t=15:20` square-off vs `hold-to-settlement`), and the two must be distinct classification tokens because their cost models differ (premium STT vs intrinsic STT, point 3).

#### d.3 Post-SEBI 0DTE venue map (which day is "expiry day")

Under the post-2024-11-20 regime only ONE weekly index per exchange survives (§i): on NSE the NIFTY weekly (Tuesday since 2025-09-02, `expiry_calendar.py:103`), on BSE the SENSEX weekly (Thursday since 2025-09-04, `expiry_calendar.py:126-127`); BANKNIFTY has had NO weekly since EOD 2024-11-13 (`expiry_calendar.py:113`) — its 0DTE day exists only monthly. **Design consequence: "0DTE" is not a fixed weekday; it must be resolved per (underlying, date) through the expiry registry — the repo already owns that resolver (G6).**

### 5.5 (e) Event-driven volatility families

| Family | Structure | Entry/exit grammar | India events |
|---|---|---|---|
| Pre-event long vol | L straddle/strangle, near expiry spanning the event | enter E−k days, exit E−0 pre-announcement or E+1 | Union Budget, RBI MPC, general-election result days, index rebalances, earnings (stocks) |
| Post-event IV-crush short | S straddle/strangle (or iron fly for defined risk) entered just before/after the announcement | exit same day / E+1 | same events; harvests the implied-vol collapse once uncertainty resolves |
| Event calendar spread | S near-expiry + L far-expiry across the event | exit E+1 | isolates the event-week IV premium |

**The repo's honest event-data boundary (this constrains what is backtestable TODAY):** exactly four event classes are calendar-verifiable in-repo — `expiry`, `muhurat`, `budget_weekend`, `nifty_rebalance` — and "Weekday Budget days and election days are tagged NOWHERE in the repo and are honestly OMITTED — no invented event lists" (`brutex/engine/session_lens.py:17-27,88-131`). So:

- Backtestable now (with lake data + existing flags): expiry-day families (§d), Muhurat sessions, budget-WEEKEND sessions, rebalance days.
- NOT backtestable without a new verified event calendar (own DEC + sourced data): weekday Budgets, RBI MPC dates, election dates, earnings dates. An event-vol engine's FIRST deliverable is that calendar, with per-row citations, following the `expiry_calendar.py`/`lot_size_history.py` dated-registry idiom (G6/G7) — the repo already has the right pattern to copy.
- IV-rank entry gates additionally need a sanctioned IV series (India VIX is stored but reference-only, G23 — same scope note as §b.2).

### 5.6 (f) Adjustment frameworks as first-class machinery

Adjustments are not folklore bolted onto a family — they are a deterministic TRIGGER → ACTION → RE-ARM state machine attached to a base structure. Expressed that way, every practitioner convention becomes a *sweepable parameter vector*, which is exactly the drill-down treatment the spot engine gives its SL/TP axes today (G16).

#### f.1 Trigger vocabulary (sweepable)

| Trigger token | Parameters | Semantics |
|---|---|---|
| `DELTA_BAND(x)` | x = net-delta band (lot-deltas or fraction) | fires when abs(net Δ) > x |
| `STRIKE_TOUCH(leg)` | which short leg | underlying trades at/through the short strike |
| `PREMIUM_MULT(m)` | m ≥ 1 | structure mark-to-market ≥ m × net credit (loss trigger) |
| `PREMIUM_DECAY(p)` | 0 < p < 1 | structure mark ≤ p × net credit (profit trigger) |
| `DTE(n)` | n days | calendar trigger (e.g. the "manage at 21 DTE" convention) |
| `CLOCK(hh:mm)` | IST instant | intraday trigger (e.g. 14:45 de-risk) |
| `IV_SHIFT(s)` | s vol-points or rank | IV regime trigger (needs the sanctioned IV series — §e note) |

Multi-trigger determinism law (required for Golden Rule #4 reproducibility): when several triggers fire on the same bar, a PINNED priority order resolves them (proposal: loss triggers > strike-touch > delta-band > profit > calendar > clock), and the chosen order is itself part of `canonical_params` (G17).

#### f.2 Action vocabulary

| Action token | Parameters | What it does | Classic use |
|---|---|---|---|
| `ROLL_STRIKE(dir, n)` | up/down, n steps | close leg, reopen n strike-steps away, SAME expiry | roll the untested side of a strangle toward the market |
| `ROLL_OUT(tier)` | next/far, weekly/monthly | close leg(s), reopen SAME strike-selector at a later expiry | duration extension when tested |
| `ROLL_UP_AND_OUT(dir, n, tier)` | combined | both of the above atomically | defending a tested short call |
| `INVERT` | — | roll the tested side THROUGH the untested side (short call strike < short put strike) | last-resort strangle defense; realized-loss bookkeeping required |
| `WING_ADD(sel)` | strike-selector | buy a further-OTM wing: naked → defined (strangle → condor, straddle → iron fly) | margin/ELM relief + tail cut |
| `WING_WIDEN/NARROW(n)` | steps | move existing wings | repair a breached condor |
| `DELTA_HEDGE(instr)` | FUT / option | add a hedge overlay (hands off to the §a.2 machinery) | convert static short into managed short |
| `SIZE_DOWN(k)` | k lots | close k lots of the structure | de-risk without restructuring |
| `CLOSE_ALL` | — | terminal stop | the plain SL |

An adjustment framework = (base family, trigger set with priorities, action bound to each trigger, `max_adjustments` bound, terminal regime). The `max_adjustments` bound is REQUIRED — it keeps the state machine finite, the sweep enumerable, and the worst-case-fill cost bounded (each roll leg fills at the bar's adverse extreme under G15 — rolls are systematically expensive in-model, which is conservative and must be disclosed).

#### f.3 Premium-seller exit conventions as sweep defaults

The widely-circulated premium-selling conventions, expressed as points on the two exit axes rather than as facts about edge (evidence review: `10-seller-edge-and-execution-evidence.md`):

| Convention | As parameters | Provenance status |
|---|---|---|
| "Take profit at 50% of credit" | `PREMIUM_DECAY(0.50)` | practitioner convention popularized by tastylive-style research; UNVERIFIED as edge — treated ONLY as a default sweep point (register item U9) |
| "Stop at 2× credit" (i.e. loss = 1× credit) | `PREMIUM_MULT(2.0)`; sweep {1.5, 2.0, 2.5, 3.0} | same status |
| "Manage at 21 DTE" | `DTE(21)` | same status |
| Intraday square-off by close | `CLOCK(15:20)` (mirrors the spot engine's forced exit, G18) or family-specific | repo law for spot (G18); options instant is a design fork (§d.2 point 6) |

**Engine mapping (the concrete deliverable of this subsection):** today's SL/TP axes are absolute-paisa sequences on the spot series (`stop_losses`/`take_profits`, `brutex/engine/sweep_orchestrator.py:279-280`). Options families need the SAME two axes re-based in *units of net credit/debit* (a new `unit` token: `PAISA` | `CREDIT_MULT`), plus the trigger/action grammar above as a distinct `management` axis. All three fold into `canonical_params` and therefore into the blake3 run identity (G17) — no new identity mechanism is needed, only new keys, exactly the pattern `fill_mode` used (`brutex/execution/fill_mode.py:33`).

### 5.7 (g) Consolidated Greeks-profile matrix — all families, Parts A–C

Conventions: signs AT INCEPTION, structure centered as normally struck (ATM unless stated); `++/--` = dominant exposure; "defined" = maximum loss known and bounded at entry; "large-defined" = bounded only by underlying→0 (or strike distance); "UNDEFINED" = unbounded on at least one side. Delta signs for the bullish orientation of directional families (bearish mirrors flip Δ). Θ is per calendar day (the repo kernel's convention, G8). Families marked ⚙ are dynamic (management-dimension ≠ STATIC). Row numbers are this matrix's OWN (self-contained), not A./B. section numbers.

| # | Family | Net Δ | Γ | Θ | Vega | Risk class | Max loss at entry |
|---|---|---|---|---|---|---|---|
| 1 | Long call | + | + | − | + | defined | debit |
| 2 | Long put | − | + | − | + | defined | debit |
| 3 | Short call (naked) | − | − | + | − | UNDEFINED (upside) | unlimited |
| 4 | Short put (naked / cash-secured) | + | − | + | − | large-defined | strike→0 − credit |
| 5 | Covered call (long underlying + short CE) | + (reduced) | − | + | − | large-defined (downside) | underlying→0 − credit |
| 6 | Protective put | + (reduced) | + | − | + | defined | to put strike + debit |
| 7 | Collar | + small | ~0 mixed | ~0 mixed | ~0 mixed | defined | band width |
| 8 | Bull call spread (debit vertical) | + | ± (strike-dependent) | ± | + small | defined | debit |
| 9 | Bear put spread (debit vertical) | − | ± | ± | + small | defined | debit |
| 10 | Bull put spread (credit vertical) | + | − | + | − | defined | width − credit |
| 11 | Bear call spread (credit vertical) | − | − | + | − | defined | width − credit |
| 12 | Long straddle | ~0 | ++ | −− | ++ | defined | debit |
| 13 | Short straddle | ~0 | −− | ++ | −− | UNDEFINED (both) | unlimited |
| 14 | Long strangle | ~0 | ++ | −− | ++ | defined | debit |
| 15 | Short strangle | ~0 | −− | ++ | −− | UNDEFINED (both) | unlimited |
| 16 | Iron condor (short) | ~0 | − | + | − | defined | wing width − credit |
| 17 | Iron butterfly (short) | ~0 | −− | ++ | −− | defined | wing width − credit |
| 18 | Long butterfly (buy wings, sell 2× body) | ~0 | − near body | + | − | defined | debit |
| 19 | Short butterfly | ~0 | + | − | + | defined | width − credit |
| 20 | Long condor (debit) | ~0 | − in range | + | − | defined | debit |
| 21 | Long calendar (short near, long far, same strike) | ~0 | − (near leg dominates) | + | + (far leg dominates) | defined | debit |
| 22 | Short calendar | ~0 | + | − | − | defined-complex (margin-heavy) | ~width of vol move (bounded, model-dependent) |
| 23 | Diagonal (e.g. poor-man's covered call) | + | mixed | + | + | defined | net debit |
| 24 | Call ratio spread, front (buy 1, sell 2 OTM) | + then − | − | + | − | UNDEFINED (upside) | unlimited above upper break-even |
| 25 | Put ratio spread, front | ± then + | − | + | − | UNDEFINED (downside) | large below lower break-even |
| 26 | Call backspread (sell 1, buy 2 OTM) | + | + | − | + | defined-valley | max at long strike at expiry |
| 27 | Put backspread | − | + | − | + | defined-valley | max at long strike at expiry |
| 28 | Jade lizard (short put + short call spread, credit > call-spread width) | + | − | + | − | large-defined (downside only; no upside risk when credit > width) | strike→0 − credit |
| 29 | Risk reversal (short put + long call) | ++ | + | ~0 | ~0 mixed | UNDEFINED-equivalent (downside) | like long underlying below put strike |
| 30 | Box spread | 0 | 0 | ~0 (rates) | 0 | defined (financing) | rate/mispricing only |
| 31 | Synthetic long/short (CE−PE same strike) | ±1 | ~0 | ~0 | ~0 | UNDEFINED-equivalent | like the underlying |
| 32⚙ | Delta-hedged short straddle (§a) | ~0 managed | −− | ++ | −− | UNDEFINED (gap) | gap through band |
| 33⚙ | Delta-hedged short strangle (§a) | ~0 managed | − | + | − | UNDEFINED (gap) | gap through band |
| 34⚙ | Gamma-scalping long book (§b) | ~0 managed | ++ | −− | ++ | defined | debit + hedge slippage |
| 35⚙ | Dispersion composite (§c) | ~0 | mixed by leg | mixed | short implied correlation | complex (leg-level UNDEFINED possible) | leg-dependent |
| 36⚙ | 0DTE short straddle/strangle (§d) | ~0 | −−− (extreme) | +++ (per hour) | − small | UNDEFINED intraday | unlimited until square-off |
| 37 | 0DTE long gamma lottery (§d) | ± | +++ | −−− | small | defined | debit |
| 38 | Pre-event long vol (§e) | ~0 | + | − | ++ | defined | debit |
| 39⚙ | Post-event IV-crush short (§e) | ~0 | − | + | −− | UNDEFINED unless winged | unlimited (naked) / width (winged) |

Notes: rows 1-31 restate the static families of Parts A–B (enumerated here independently so this matrix is self-contained); rows 32-39 are Part C's dynamic/0DTE/event families. Greek signs are the standard Black-76/BSM qualitative profiles consistent with the repo kernel's conventions (Θ per calendar day, vega per vol-point — `brutex/options/greeks.py:110-114`); they are inception-profiles, not path claims. Rows 22/24/25/28/29's risk-class labels assume European cash-settled index options for index underlyings (G10) — deliverable single-stock variants add assignment risk (§d.2 point 4).

### 5.8 (h) THE CLASSIFICATION SCHEME — the minimal dimension set for the engine designers

#### h.1 Dimensions

A strategy instance = one point in `D1 × D2 × D3 × D4 × D5 × D6 × D7`:

| Dim | Name | Domain | Existing repo primitive it extends |
|---|---|---|---|
| **D1** | Underlying scope | one underlying (default) \| COMPOSITE (per-leg underlying — dispersion only) | engine instrument axis (`sweep_orchestrator.py:187`) |
| **D2** | Leg count | 1..4 structure legs + at most 1 hedge-overlay slot (dispersion: composite of D2≤4 books) | executor is 1-leg today (`executor.py:38-41`) |
| **D3** | Per-leg tuple | `(side ∈ {LONG, SHORT}, type ∈ {CE, PE, FUT}, strike_selector, expiry_tier, ratio ∈ ℕ⁺)` | side/type: `strike_rules.TradeDirection` + CE/PE; strike_selector: the 10 locked rules (G4) + extensions in h.3; expiry_tier: `{near,next,far} × {weekly,monthly}` (G5); ratio: NEW |
| **D4** | Management (the dynamic dimension) | `STATIC` \| `DELTA_MANAGED(band, hedge_instr, cadence, terminal_regime)` \| `ADJUSTED(triggers→actions, priority order, max_adjustments, terminal_regime)` | NEW — §a.2 + §f grammar |
| **D5** | Exit grammar | SL/TP in `unit ∈ {PAISA, CREDIT_MULT}` + `CLOCK(t)` \| `DTE(n)` \| `EXPIRY_HOLD` | extends the SL/TP axes (`sweep_orchestrator.py:279-280`) + the forced-exit law (G18) |
| **D6** | Entry gate | the spot 60-bit condition mask + direction (reused as the ENTRY layer) ∪ event gates (`EVENT(class, offset)`) ∪ IV gates (`IV_RANK(op, x)` — needs a sanctioned IV series) | mask: `strategies/spec.py:38,67` (`IMPLEMENTED_MASK` guard); events: `session_lens.py:88-131` (four verifiable classes only) |
| **D7** | Sizing | lots ∈ ℕ⁺, resolved through the date-keyed lot table | `lot_size_history.get_lot_size` (G7) |

**Identity law:** every D1-D7 coordinate serializes into `canonical_params` and hence into the blake3 run identity (G17) — the options engine inherits reproducibility for free by extending keys, exactly as `fill_mode` did (`execution/fill_mode.py:33`).

**Minimality (each dimension is necessary — one witness each):** drop D3.strike_selector → cannot distinguish straddle from strangle; drop D3.expiry_tier → cannot express calendars/diagonals (rows 21-23); drop D3.ratio → cannot express butterflies/ratio spreads/backspreads (rows 18-19, 24-27); drop D3.type=FUT → cannot express covered/collar/delta-hedged families (rows 5-7, 32-34); drop D4 → cannot distinguish static short strangle (15) from delta-hedged (33) or rolled (§f) variants — the entire Part-C dynamic taxonomy collapses; drop D5's `CREDIT_MULT` unit → premium-seller conventions (§f.3) inexpressible; drop D6 event gates → event families (38-39) collapse into plain vol families; drop D1 composite → dispersion (35) inexpressible; drop D7 → hedge quantization (§a.4) and margin/contract-value realities unrepresentable.

**Spanning claim (honest form):** D2×D3 spans every STATIC single-underlying family of Parts A–C (a static structure IS a multiset of legs); D4 spans the dynamic families; D6 spans the event families; D1 spans dispersion. This is demonstrated constructively in h.4 — a claim of expressibility, NOT a claim that the engine should enumerate the raw cross product (see h.5).

#### h.2 Leg-encoding grammar (compact form used in h.4)

```
leg      := <side><type>@<strike>/<expiry> [×<ratio>]
side     := L | S
type     := C | P | F                      (F = futures/linear leg; no strike/Δ token)
strike   := ATM | ATM±n | Δ.xx | K(abs) | OI_MAX | PREM(p)
expiry   := W0|W1|W2 | M0|M1|M2            (near/next/far × weekly/monthly — G5 tokens)
mgmt     := STATIC | DH(band,instr,cad,term) | ADJ(...)
exit     := {TP:…, SL:…, t:…, hold:expiry}
```

`ATM±n` carries the repo's in-direction semantics (further-OTM is `+` for both CE and PE — `strike_rules.py:25-33`), so encodings are CE/PE mirror-symmetric. `Δ.xx` generalizes the three locked delta targets into a continuous sub-axis (h.3 ext-1).

#### h.3 Extensions required beyond today's primitives (the exact delta between repo and scheme)

| Ext | What | Why | Base |
|---|---|---|---|
| ext-1 | `Δ.xx` continuous delta-target selector (today: only 0.15/0.30/0.50 locked) | condor wings (~Δ.05), risk reversals (~Δ.25) | `strike_rules.py:112-114`; per-bar delta exists in the lake (G9) |
| ext-2 | per-leg `ratio` | butterflies, ratio spreads, backspreads | none (new) |
| ext-3 | `FUT` leg type in a structure | covered/collar/hedged families | futures 1m bars + costs exist (G12, G21) |
| ext-4 | SHORT side | every credit family | executor BUY-only (G2); selling not authorized (G3) — the LARGEST gate |
| ext-5 | `CREDIT_MULT` exit unit | premium-seller conventions | SL/TP axes (G16) |
| ext-6 | `K(abs)` paired absolute strikes | box spreads (row 30) only | low priority |
| ext-7 | multi-leg ATOMIC fill law | every ≥2-leg entry/roll: define the worst-case law per leg with a same-bar ordering pin (mirror of the trade-sim same-bar SL-first tie law) | `fill_mode.py` (G15) |
| ext-8 | margin/ELM model | any margin-relative metric for short families | BLOCKED on a verified SPAN source (G19) |
| ext-9 | sanctioned IV series | IV gates (D6), event-vol entries | India VIX stored but reference-only (G23) |
| ext-10 | verified event calendar (weekday Budget/RBI/elections/earnings) | event families beyond the four in-repo classes | `session_lens.py` idiom + `expiry_calendar.py` dated-registry pattern |

#### h.4 Family → encoding table (constructive proof of coverage; Parts A–C)

The `family → {side, type, strike-selector, expiry-tier, ratio, management}`
encoding — every family of this document as a well-formed point in the D1-D7
scheme (row numbers = the §5.7 matrix rows):

| # | Family | Encoding (D2×D3) | D4 mgmt | Notes |
|---|---|---|---|---|
| 1 | Long call | `LC@sel/E` | STATIC | sel ∈ G4 rules |
| 2 | Long put | `LP@sel/E` | STATIC | |
| 3 | Short call | `SC@sel/E` | STATIC | ext-4 |
| 4 | Short put / CSP | `SP@sel/E` | STATIC | ext-4 |
| 5 | Covered call | `LF ×1 + SC@ATM+2/M0` | STATIC | ext-3, ext-4 |
| 6 | Protective put | `LF ×1 + LP@ATM+2/M0` | STATIC | ext-3 |
| 7 | Collar | `LF + SC@ATM+2/M0 + LP@ATM+2/M0` | STATIC | in-dir tokens mirror |
| 8 | Bull call spread | `LC@ATM/E + SC@ATM+2/E` | STATIC | |
| 9 | Bear put spread | `LP@ATM/E + SP@ATM+2/E` | STATIC | |
| 10 | Bull put credit spread | `SP@Δ.30/E + LP@Δ.15/E` | STATIC | ext-1/4 |
| 11 | Bear call credit spread | `SC@Δ.30/E + LC@Δ.15/E` | STATIC | ext-1/4 |
| 12 | Long straddle | `LC@ATM/E + LP@ATM/E` | STATIC | |
| 13 | Short straddle | `SC@ATM/E + SP@ATM/E` | STATIC | ext-4 |
| 14 | Long strangle | `LC@ATM+2/E + LP@ATM+2/E` | STATIC | |
| 15 | Short strangle | `SC@Δ.15/E + SP@Δ.15/E` | STATIC | ext-1/4 |
| 16 | Iron condor | `SC@Δ.15 + LC@Δ.05 + SP@Δ.15 + LP@Δ.05` (one E) | STATIC | ext-1/4 |
| 17 | Iron butterfly | `SC@ATM + SP@ATM + LC@ATM+3 + LP@ATM+3` | STATIC | ext-4 |
| 18 | Long butterfly | `LC@ATM−2 + SC@ATM ×2 + LC@ATM+2` | STATIC | ext-2 |
| 19 | Short butterfly | `SC@ATM−2 + LC@ATM ×2 + SC@ATM+2` | STATIC | ext-2/4 |
| 20 | Long condor | `LC@ATM−4 + SC@ATM−2 + SC@ATM+2 + LC@ATM+4` | STATIC | ext-4 (short body) |
| 21 | Long calendar | `SC@ATM/W0 + LC@ATM/M0` | STATIC | expiry-tier dimension |
| 22 | Short calendar | `LC@ATM/W0 + SC@ATM/M0` | STATIC | ext-4 |
| 23 | Diagonal (PMCC) | `LC@ATM−5/M1 + SC@ATM+2/W0` | STATIC or ADJ(roll short leg) | |
| 24 | Call ratio spread (front) | `LC@ATM/E + SC@ATM+2/E ×2` | STATIC | ext-2/4 |
| 25 | Put ratio spread (front) | `LP@ATM/E + SP@ATM+2/E ×2` | STATIC | ext-2/4 |
| 26 | Call backspread | `SC@ATM/E + LC@ATM+2/E ×2` | STATIC | ext-2/4 |
| 27 | Put backspread | `SP@ATM/E + LP@ATM+2/E ×2` | STATIC | ext-2/4 |
| 28 | Jade lizard | `SP@Δ.30 + SC@Δ.30 + LC@Δ.15` + invariant `credit > call-spread width` | STATIC | family-level invariant predicate |
| 29 | Risk reversal | `SP@Δ.25 + LC@Δ.25` | STATIC | ext-1/4 |
| 30 | Box spread | `LC@K1 + SC@K2 + LP@K2 + SP@K1` | STATIC | ext-6 |
| 31 | Synthetic long | `LC@ATM/E + SP@ATM/E` | STATIC | ext-4 |
| 32 | Delta-hedged short straddle | row 13 encoding | `DH(band, FUT, cad, term)` | §a |
| 33 | Delta-hedged short strangle | row 15 encoding | `DH(band, FUT, cad, term)` | §a |
| 34 | Gamma-scalping long book | row 12 or 14 encoding | `DH(band, FUT, cad, term)` | §b |
| 35 | Dispersion | `COMPOSITE{ index: row 13/M0 ; constituents_i: row 12/M0 ×wᵢ }` | per-book DH optional | D1 extension; monthly tier forced (§c) |
| 36 | 0DTE short straddle/strangle | row 13/15 with `E=W0 ∧ DTE=0` | STATIC or DH + `term` (§d.2) | exit `{t:15:00-15:20}` or `hold:expiry` |
| 37 | 0DTE long gamma | `LC@ATM+5/W0 (DTE=0)` | STATIC | |
| 38 | Pre-event long vol | row 12/14 + D6 `EVENT(class, −k)` | STATIC | ext-10 for new classes |
| 39 | Post-event IV-crush short | row 13/15/17 + D6 `EVENT(class, 0)` | STATIC or ADJ | ext-4/10 |
| f.* | Rolled/repaired variants of ANY short family | base row | `ADJ(triggers→actions, max)` | §f grammar |

Every family enumerated in Parts A–C appears above with a well-formed
encoding ⇒ the dimension set spans the taxonomy. (Parts A–B families were
re-enumerated independently here; where Parts A–B name additional
composites — guts B.5, broken-wing flies B.9, trees/ladders B.18,
conversions/reversals A.16 — they reduce to leg multisets and land in
D2×D3 by the same construction.)

#### h.5 Sweep-cardinality honesty (Golden Rule #10)

The RAW cross product is not sweepable and must never be presented as the plan. Example: 2 structure legs × (2 sides × 2 types × ~12 strike tokens × 6 expiry tokens × 2 ratios) per leg ≈ 576²/2 ≈ 1.66×10⁵ structures BEFORE the D4 management grid, D5 exit grid, and D6 entry-mask dimension (the spot engine's mask axis alone is C(60,k)-sized — `strategies/spec.py:67`). The engine therefore sweeps **named family TEMPLATES** (the h.4 rows) with per-template constrained parameter grids — the exact analogue of the spot engine sweeping masks-within-K rather than arbitrary formulas, and of Mode-A's pinned SL/TP grid (`sweep_orchestrator.py:500-501`). Template-first also keeps every result attributable to a human-meaningful family name on the display surfaces.

### 5.9 (i) Indian-market fit notes — the post-2024 weekly squeeze and other regime walls

#### i.1 The SEBI October-2024 framework (web-verified) and its repo shadow

SEBI circular **SEBI/HO/MRD/TPD-1/P/CIR/2024/132** (2024-10-01), "Measures to Strengthen Equity Index Derivatives Framework for Increased Investor Protection and Market Stability" ([SEBI circular page](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html); [NSE FAOP64506](https://nsearchives.nseindia.com/content/circulars/FAOP64506.pdf); [Zerodha Z-Connect summary](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)):

| Measure | Effective | Repo shadow (already encoded) |
|---|---|---|
| ONE weekly-expiry benchmark index per exchange | 2024-11-20 | BANKNIFTY weekly killed EOD 2024-11-13 (`expiry_calendar.py:113`); NIFTY + SENSEX weeklies survive as each exchange's one weekly (`expiry_calendar.py:96-127`) |
| Minimum contract value ₹15 lakh (review band ₹15-20 lakh) → lot-size step-up | 2024-11-20 | NIFTY 25→75, BANKNIFTY 15→30, SENSEX 10→20 (`lot_size_history.py:16`, SEBI/HO/MRD-PoD2/CIR/P/2024/00181) |
| Additional 2% ELM on SHORT option positions on their expiry day | 2024-11-20 | NOT encoded (no margin model at all — G19); must be disclosed on any 0DTE short-family result |
| Calendar-spread margin benefit REMOVED on expiry day | Feb-2025 (secondary sources report Feb 1 / Feb 10 — exact date not re-verified against the primary PDF; register U1) | NOT encoded (G19) |
| Upfront collection of option premium from buyers; intraday position-limit monitoring | Feb-2025 / Apr-2025 per secondary summaries (register U2) | NOT encoded |

#### i.2 Design consequences ("the squeeze") for each Part-C family class

1. **0DTE venue collapse + migration.** Pre-squeeze there were index weeklies expiring on MULTIPLE weekdays (NIFTY Thu, BANKNIFTY Wed-then-killed, SENSEX Fri...); post-2024-11-20 exactly one weekly per exchange remains, and the weekday itself then churned again in Sep-2025 (NIFTY→Tue, SENSEX→Thu — `expiry_calendar.py:103,126-127`). A 0DTE family swept 2020→2026 is therefore sweeping a NON-STATIONARY venue: (a) BANKNIFTY-weekly rows must structurally refuse dates after 2024-11-13 (the registry returns `NO_WEEKLY` — never a silently-substituted monthly); (b) "expiry-day" features keyed to a fixed weekday are regime-broken twice; (c) cross-family comparisons pre/post 2024-11-20 mix different margin AND lot regimes. **Recommendation: the options engine must treat 2024-11-20 as a hard REGIME WALL token in D6 (like the STT walls), and every leaderboard must be regime-segmentable.**
2. **Premium-selling capital shock.** Lot ~3× (G7) + ₹15L contract floor + expiry-day +2% ELM ⇒ per-lot capital for short families jumped discontinuously at 2024-11-20; any position-count-based sizing (D7) must be date-aware. With margin unmodeled (G19), sizing realism claims must stay qualitative.
3. **Calendar families.** The expiry-day removal of calendar-spread margin benefit changes the economics of holding calendars/diagonals INTO expiry day — unquantifiable in-repo today; disclose.
4. **Cost walls.** STT walls at 2024-10-01 (options 0.0625%→0.10%, futures →0.02%) and 2026-04-01 (0.15% / 0.05%) are already date-keyed in the cost model (G20/G21) — the options engine inherits honest cost regimes, including the pre-2024-10-01 futures-STT REFUSAL window that date-partitions delta-hedged backtests using futures hedges (§a.3).
5. **Expiry-weekday churn generally.** 2023-2025 saw at least: BANKNIFTY Thu→Wed (2023-09), NIFTY monthly Thu→Wed→Thu (2024), SENSEX Fri→Tue (2025-01), the withdrawn NIFTY Monday regime (Mar-2025, never traded), and the Sep-2025 standardization (NIFTY/Tue, SENSEX/Thu) — all encoded with the withdrawn regime deliberately EXCLUDED (`expiry_calendar.py:55-59,96-160`). Every DTE- or weekday-keyed feature must resolve through this registry; nothing may hardcode a weekday.
6. **Data fit.** The lake already carries what Part-C families need historically: per-contract 1m OHLCV+OI (G12), all listed strikes (G13), per-bar Greeks incl. delta for selectors and band triggers (G9), spot + futures 1m for hedge legs. The known gaps are exactly ext-8/9/10 (margin, sanctioned IV series, event calendar) plus the executor-side ext-2/3/4/7 (ratio, FUT legs, SHORT side, multi-leg atomic fills).
7. **Stock-underlying families.** Monthly-only expiries (`movers_options.py:85-87`), American-style approximation flagged in the Greeks provenance (G10), and MWPL/ban-period machinery (U5) make stock-option families a SECOND-generation target; index families are the honest first generation.

---

## 6. Consolidated UNVERIFIED register (all parts)

Every India-specific or empirical claim in this document that could not be
verified from repo evidence or common-knowledge derivation is registered
here, namespaced by part (in-body tags are unchanged: a `U6` inside Part A =
**A-U6**, inside Part B = **B-U6**, inside Part C = **C-U6**). The
microstructure agents own turning these into verified rows; none of these
tags blocks the taxonomy itself (payoff math is exchange-independent).
"Verify" names the concrete act that would close each item.

### 6.1 Part A register (A-U1 … A-U12)

| ID | Claim as used above | What would verify it |
|---|---|---|
| A-U1 | NSE options (index AND single-stock) are European-exercise; stock options converted from American to European style around Jan 2011 | NSE/SEBI circular archive for the 2010-11 style conversion; current NSE contract specifications pages |
| A-U2 | Single-stock derivatives are physically settled at expiry (phased in 2018-19, fully from Oct 2019); net-delivery offsetting for multi-leg/mixed-ITM positions | SEBI physical-settlement circulars (2018-2019) + NSE Clearing settlement procedure docs |
| A-U3 | Broker practice of force-closing long ITM stock options a buyer cannot fund for delivery; buyer-side delivery funding mechanics | Broker (Zerodha/Groww/etc.) policy pages + NSE Clearing delivery-margin circulars |
| A-U4 | Expiry-week DELIVERY margin ramp on in/near-the-money stock options (escalating percentages over the last sessions) | NSE Clearing delivery-margin circular series |
| A-U5 | Peak-margin / full-upfront-margin regime for F&O (SEBI phase-in through Sep 2021) | SEBI peak-margin circulars (2020-2021) |
| A-U6 | SPAN + Exposure magnitudes; spread/hedge offset magnitudes for verticals, futures-vs-option hedges, conversions, boxes; "Indian spread margin > theoretical max loss" | Daily NSE SPAN risk-parameter files (the repo itself records that NO sourced SPAN history is integrated — `brutex/engine/margin_history.py:12-18`); NSE margin calculator captures |
| A-U7 | MWPL ban-period rules: no FRESH positions in any derivative of a banned stock (futures and options alike) | SEBI/NSE MWPL framework circulars + daily ban-list mechanics |
| A-U8 | Collateral/pledge mechanics: shares pledgeable against short-option margin with haircuts; cash-vs-collateral (50:50) funding rule; margin benefit only while both legs of a spread are on; leg-out margin spikes | SEBI margin-pledge circulars (2020) + broker margin-policy docs + NSE Clearing collateral norms |
| A-U9 | Retail cash-equity short selling is intraday-only; SLB exists but is thin/operationally niche | SEBI short-selling & SLB framework + SLB volume statistics |
| A-U10 | Weekly index option availability reshaped by SEBI's Oct-2024 measures (one weekly expiry per exchange from Nov-2024); note the repo's lot-size table independently cites the same measure set (SEBI/HO/MRD-PoD2/CIR/P/2024/00181 at 2024-11-20, `brutex/options/lot_size_history.py:16`) for LOT SIZES — the weekly-expiry claim itself still needs its circular | SEBI circular of 2024-10-01 (F&O measures) + NSE/BSE implementation circulars |
| A-U11 | Indian index put-call IV skew (puts richer than symmetric calls) and its magnitude; zero-cost-collar strike asymmetry | Empirical measurement from the options lake (a legitimate future BruteX research task) — no circular exists; this is a market fact, not a rule |
| A-U12 | Index parity/conversion residuals are harvested to ≤ retail friction | Empirical measurement from the lake (bid/ask-aware); no external doc suffices |

(Part C's §i.1 carries the web-verified circular identity for the
weekly-expiry measure — SEBI/HO/MRD/TPD-1/P/CIR/2024/132, with NSE FAOP64506
as the implementation source — narrowing A-U10; the residual per-measure
primary-PDF verification tracks C-U1/C-U2.)

### 6.2 Part B register (B-U1 … B-U11)

| # | Claim | Where used | Status | What would verify it |
|---|---|---|---|---|
| B-U1 | NSE single-stock options list MONTHLY expiries only (no weekly stock options) | Master table; every "stk" fit cell | UNVERIFIED — operator premise in the tasking + common market knowledge; no circular read in-session; repo evidence covers index expiry regimes only (`TRACK2_OPTIONS_SPEC.md:89-118`) | Fetch NSE F&O contract specifications / launch circulars for single-stock derivatives; or probe the vendor `get_expiries` for a stock underlying and observe monthly-only |
| B-U2 | Physical-settlement MECHANICS for stock derivatives (assignment flow, delivery margins ramp E-4→E-1, close-out rules) | B.2, B.4, B.6, B.11-B.14, B.16, B.18 stock-fit cells | PARTIALLY grounded: the settlement SPLIT (stock derivs physical, index cash) is repo-verified (`docs/track2/PLAN_FINALIZATION_2026-05-16.md:333`) and Groww's margin API exposes `physical_delivery_margin_requirement` (`docs/groww/trade-api-2026-07-03/05-margin.md:183`); the SEBI mandate's circular number, phase-in dates and margin ramp schedule are UNVERIFIED here | SEBI physical-settlement circular(s) (2018-2019 phase-in) + NSE delivery-margin FAQ — owned by the microstructure agents |
| B-U3 | Behavioral/flow claims: short straddle/strangle/iron-condor are "the dominant Indian weekly income trades"; retail expiry-day fly lotteries | B.2, B.4, B.6-B.8 fit cells | UNVERIFIED — market-color, no dataset cited | SEBI F&O participation studies (e.g. the SEBI loss-study reports) or NSE volume-by-structure data; else soften to "commonly described as" |
| B-U4 | IV term-structure behavior around events (front-IV elevation pre-event, post-event crush) driving calendar edge | B.11, B.13 | UNVERIFIED as a quantitative claim — qualitative option-pricing folklore | Measure from the lake's stamped per-bar `iv` across paired expiries (`executor.py:30-32` documents the per-bar iv carry) once a term-structure derivation is validated |
| B-U5 | SPAN/exposure margin MAGNITUDES and offset-recognition specifics for every margin cell (naked vs spread vs cross-expiry) | Every "Margin nature (India)" subsection | UNVERIFIED BY DESIGN — repo margin table is refusal-only with ZERO verified rows (`brutex/engine/margin_history.py:10-19`); `TRACK2_RISK_MGMT.md` §2.3 forbids hard-coded margin constants (quoted in the same docstring) | The margin-data DEC: NSE SPAN archive integration or broker margin-API capture (`docs/groww/trade-api-2026-07-03/05-margin.md` documents the endpoint shape) — owned by the microstructure agents |
| B-U6 | Calendar-spread margin benefit EXISTS and is REMOVED on the front leg's expiry day (SEBI 2024-10-01 measure series), with effective dates | B.11-B.13 margin cells; §1.3 | UNVERIFIED — the measure SERIES itself is partially repo-corroborated (weekly rationalization + lot resize cite "SEBI master circular 2024-10-01" at `TRACK2_OPTIONS_SPEC.md:108` and SEBI/HO/MRD-PoD2/CIR/P/2024/00181 at `lot_size_history.py:16`), but the calendar-benefit-removal item, its circular text and effective date were not read in-session | Fetch the SEBI 2024-10-01 F&O measures circular + NSE implementation circular; record item list + dates — microstructure agents |
| B-U7 | Additional expiry-day ELM on short options (same SEBI measure series), rate and date | B.2, B.4, B.7, B.14, B.16, B.18b margin cells | UNVERIFIED — same basis as B-U6 (Part C's §i.1 carries the web-verified 2% / 2024-11-20 figure — see §6.4 sources) | Same circulars as B-U6 |
| B-U8 | European-style exercise for NSE index options (used in B.11's "loss bounded ≈ debit" argument via same-strike longer-expiry value dominance) | §1 table; B.11-B.13 | PARTIALLY grounded: cash settlement of index F&O is repo-verified (`PLAN_FINALIZATION_2026-05-16.md:333`); the European-exercise designation and the small dividend/basis caveat are textbook facts not verified in-session | NSE index-option contract specification page |
| B-U9 | Liquidity characterizations: ITM options (guts, deep-ITM diagonals) trade wider/thinner in India; far-OTM stock wings illiquid | B.5, B.12, fit cells | UNVERIFIED — plausible microstructure color; the repo's own spread model is honestly flat-1-tick and "cannot measure or verify spreads" (`TRACK2_OPTIONS_SPEC.md:1010-1012` delta-bucketed model rejection) | Measured spread study from captured depth data (the spec names this as the only honest upgrade path, `TRACK2_OPTIONS_SPEC.md:1100-1106`) |
| B-U10 | The claim that a back-month same-strike European call is always worth ≥ the front intrinsic at T1 (used for the B.11 "≈ debit" bound) | B.11, B.12 | Holds under no-dividend/no-basis assumptions; index dividend futures-basis can dent it marginally — stated as "≈" with noise caveat, not exact | Formal bound check under NSE index dividend assumptions, or empirical scan of paired-expiry lake bars at front-expiry timestamps |
| B-U11 | Weekly-index structural dominance of Indian option volume (the premise "weekly index dominance" in the tasking) | Header; fit cells | UNVERIFIED as a number — repo verifies the weekly REGIMES exist and BANKNIFTY's was killed (`TRACK2_OPTIONS_SPEC.md:92,108,118`), not volume shares | NSE monthly derivatives statistics (volume by product/expiry type) |

#### Part B repo-citation index (all verified by reading in-session)

`CLAUDE.md:10774` · `brutex/options/executor.py:12-14, 20-28, 30-36, 38-41, 155-184` ·
`brutex/engine/margin_history.py:10-19` · `brutex/options/expiry_calendar.py:18-33, 53-59` ·
`brutex/options/lot_size_history.py:1-19` · `brutex/options/strike_grid.py:26-30, 38-102` ·
`brutex/engine/sweep_run.py:719` · `brutex/dashboard/movers_options.py:956, 1077`
(weekly/monthly tier machinery `_monthly_expiries` / `_tier_universe`) ·
`docs/track2/TRACK2_OPTIONS_SPEC.md:40, 44, 89-92, 108, 115-118, 477-479, 785, 1010-1012, 1100-1106` ·
`docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:1-7, 18-20, 28-36` ·
`docs/track2/PLAN_FINALIZATION_2026-05-16.md:333` ·
`docs/groww/trade-api-2026-07-03/05-margin.md:183`

### 6.3 Part C register (C-U1 … C-U12)

| ID | Claim touched | Status + what would verify it |
|---|---|---|
| C-U1 | Exact effective date of the expiry-day calendar-spread margin-benefit removal (Feb 1 vs Feb 10, 2025 — secondary sources disagree) | Read the primary circular PDF (SEBI/HO/MRD/TPD-1/P/CIR/2024/132) §on calendar spreads |
| C-U2 | The circular's remaining two measures (upfront buyer premium collection ~Feb-2025; intraday position-limit monitoring ~Apr-2025) — taken from secondary summaries only | Same primary PDF |
| C-U3 | Index-option cash settlement procedure / closing settlement price mechanics (stated as standard practice; not encoded in-repo) | NSE contract specification pages; would also feed the Track-2 expiry state machine (currently a fail-loud stub) |
| C-U4 | Any pinning / "max pain" EDGE claim — mechanics described, edge deliberately NOT claimed | Peer-reviewed studies on expiry-day clustering for INDIAN indices; treat as a sweepable hypothesis only |
| C-U5 | Current MWPL / position-limit values and ban-period rules for stock derivatives; current index-option position-limit regime | NSE/SEBI position-limit circulars (a dispersion or stock-selling DEC prerequisite) |
| C-U6 | "Stock-option liquidity concentrates near ATM in top names" — qualitative; consistent with the backlog doc's thesis but not quantified | A lake-measured volume-by-(underlying, atm_offset) census (runnable against existing data — good first research task for the options engine) |
| C-U7 | 0DTE volume dominance / liquidity-migration magnitudes post-squeeze | Exchange volume statistics by expiry-bucket; NSE monthly derivatives statistics |
| C-U8 | The 50%-credit TP / 2×-credit SL / 21-DTE conventions' provenance (attributed to tastylive-style research) and any edge attached to them | Original tastylive/tastytrade study citations; in this doc they are ONLY default sweep points, never claimed as edge |
| C-U9 | Dispersion single-account feasibility limits — reasoned qualitatively (leg count, monthly-only stock expiries, margin), not quantified | Margin + limit verification (C-U5, ext-8) plus a constituent-liquidity census (C-U6) |
| C-U10 | Greeks-profile matrix rows (§5.7) — standard qualitative Black-Scholes/Black-76 inception profiles; not per-row re-derived in this doc | Cross-check against a standard text (e.g. Natenberg/Hull) or compute directly from the repo kernel over synthetic chains (mechanically checkable with existing `compute_greeks`) |
| C-U11 | SENSEX pre-2023 Thursday expiry — inherited from the repo's own UNVERIFIED-continuity tag | `expiry_calendar.py:47-50` records the inference and what's missing (a 2020-dated BSE circular) |
| C-U12 | Discrete-hedging noise-term magnitude claims (§b.1) — direction sourced (Sepp/Gatheral), magnitudes not quantified here | Reproduce the Sepp derivation or measure on lake data once a gamma-scalping template exists |

Part C register-item note: in Part C body text, `register item U5/U6/U7/U8/U9`
map to C-U5/C-U6/C-U7/C-U8/C-U9 above, and §i.1's `register U1/U2` map to
C-U1/C-U2.

Cross-part overlaps (deliberately kept as separate rows, since each part's
in-text tags bind to its own register): A-U2 ≈ B-U2 (physical-settlement
mechanics); A-U6 ≈ B-U5 (SPAN magnitudes); A-U7 ≈ C-U5 (MWPL/ban rules);
A-U10 ≈ B-U6/B-U7 ≈ C-U1/C-U2 (the SEBI 2024-10-01 measure series — Part C's
§i.1 carries the web-verified circular identity and per-measure table). A
future verification pass should close each cluster with ONE primary-source
read.

### 6.4 Web sources used (Part C)

- SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132 (2024-10-01): https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html
- NSE circular FAOP64506 (implementation): https://nsearchives.nseindia.com/content/circulars/FAOP64506.pdf
- Zerodha Z-Connect, "SEBI's new rules for index derivatives": https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing
- Artur Sepp, "How to optimize volatility trading and delta-hedging strategies under discrete hedging with transaction costs": https://artursepp.com/2017/05/01/how-to-optimize-volatility-trading-and-delta-hedging-strategies-under-the-discrete-hedging-with-transaction-costs/ (delta-hedged P&L identity; El Karoui et al. 1998 attribution)
- Jim Gatheral, "Volatility and Hedging Errors" (1999): https://www.math.columbia.edu/~smirnov/Gatheral.ppt
- Freeport Logbook, "Hedging Errors & Options PnL": https://freeportlogbook.substack.com/p/hedging-errors

### 6.5 Honest-limits statement (whole document)

This document verifies (a) **payoff algebra** — self-contained, recomputable
from the formulas (Part B additionally leans on standard no-arbitrage
identities, stated inline); (b) **repo facts** — each cited `path:line` was
read in the research session at the branch base SHA; (c) the **web-sourced
items of §6.4** — to the extent of the cited pages, with primary-vs-secondary
status stated per claim; and (d) nothing else. All margin MAGNITUDES,
settlement PROCEDURE details, and market-microstructure numbers are
qualitative here and tagged in §6.1-§6.3. Greek-sign rows are qualitative
inception profiles (C-U10), not per-row derivations. No strategy in this
document is claimed to be profitable, backtestable today, or in scope for
the current engine — the engine is spot-only (`brutex/universe.py:379`), the
only existing options path is BUY-only long CE/PE translation
(`brutex/options/executor.py:38-41`), option selling is not authorized
(`docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:1-7`), and every activation
path named here is a future operator-gated DEC/ADR.

— END OF 01-strategy-taxonomy.md —






