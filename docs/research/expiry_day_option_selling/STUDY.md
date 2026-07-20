# Expiry-Day Option Selling on NIFTY/SENSEX Weeklies: A Tail-First Study

> **Status:** FINAL (research output only; no repo source modified; no
> decision ID minted — the companion harness + this document live under
> `docs/research/expiry_day_option_selling/`).
> **Evidence verified as of 2026-07-18; document finalized 2026-07-20.**
> File:line citations were verified against `/home/user/bruteX` @ branch
> `claude/rust-slice-t10-lake-keys` on 2026-07-18; the harness citations
> (§5.1, §6) were re-verified against the committed
> `bhavcopy_tail_study.py` on 2026-07-20.
> **Honest cost window: 2024-10-01 → today (right edge moves).** Option
> trades dated before 2024-10-01 are counted refusals under the repo's
> verified cost stack (§3.4).
> **Provenance discipline (Golden Rules #1/#10):** every number carries a
> tag — **(measured)** = executed against the repo/working tree or
> exchange-published, **(illustrative)** = arithmetic illustration, NOT a
> backtest result, **(sourced: URL)** = external claim as reported by the
> cited source, **UNVERIFIED** = stated plainly. §6 is the receiving
> structure for the bhavcopy measurement — BLOCKED-ON-DATA in the
> authoring environment (§6 preamble, §9); nothing in it is a result yet.

Companion harness: [`bhavcopy_tail_study.py`](./bhavcopy_tail_study.py)
(+ [`README.md`](./README.md), offline suite
[`tests_offline.py`](./tests_offline.py)).

---

## 1. Executive verdict

**"Works mostly" = yes — and that is the trap.** Short expiry-day option
premium is a negative-skew strategy: it collects many small premia and pays out
its cumulative edge (and more) on rare gamma-spike days. On most weeks the
podcast claim is *true by construction* — a high win rate is the strategy's
defining property, not evidence of edge. The entire question is the tail, and
the tail is exactly where every naive backtest is wrong: SL-at-trigger fills
are a one-sided subsidy applied to precisely the days that dominate the P&L
(illustrative gap of 6–25× per losing leg, §4.2; the documented 20-Aug-2021
freak trade filled a real SL ~650 points past trigger, §4.3). The historical
sample is short (NIFTY weeklies ~7 years, SENSEX weeklies ~3 years) and
non-stationary (expiry-weekday migrations, 3× lot-size change, new expiry-day
margins, alleged expiry-day manipulation inside the sample window, §7.3), and a
3-year window can legitimately contain **zero** true tail days — in which case
every shipped overfitting gate (DSR/CPCV/WFO/Lockbox) can pass with ruin risk
fully intact (§8). BruteX can price the honest version of this study only from
**2024-10-01** (verified cost regimes; the right edge moves with today) and
only after net-new engineering (a short-premium multi-leg trailing walker;
§3, §9).

| Question | Verdict | Where |
|---|---|---|
| Can the claim be true "most months"? | YES — definitionally, for negative skew; the average is meaningless | §2 |
| Can BruteX fetch the data? | YES (NSE); SENSEX/BSE-FNO **UNVERIFIED**, probe-gated | §3.2 |
| Can BruteX simulate short premium today? | NO — BUY-only executor, no multi-leg, no trailing, no margin | §3.3 |
| Honest priceable window | **2024-10-01 → today (right edge moves)** (ETC refusal rows before it) (measured) | §3.4 |
| Does the naive fill assumption survive scrutiny? | NO — bar-extreme vs at-trigger diverges 6–25× on tail days (illustrative), with a documented real ~650-pt slip (sourced) | §4 |
| Does even the repo's worst-case law capture the full tail? | NO — six residual understatements enumerated (halts, spreads, RMS, survivorship) | §4.4 |
| Do the shipped overfitting gates catch it? | PARTLY — they deflate luck, they cannot manufacture absent tail evidence; stress injection is the honest fix | §8 |
| External record of the strategy class | 89–93% of individuals lose (SEBI); every famous short-vol vehicle died in 1–2 days | §7 |
| Measured per-expiry-day tail on real data | **[BLOCKED-ON-DATA — harness built, run pending; §6]** | §6 |

---

## 2. The claim under test

**Claim (podcast):** selling NIFTY/SENSEX current-week options on expiry day,
across strikes, with stop-loss / trailing-stop-loss / trailing-take-profit
management, "made massive money."

**Why the average is meaningless here.** Expiry-day short premium has a
structurally high win rate: most expiry days are quiet, theta decays to zero by
close, and every quiet day books a small credit. The return distribution is
negatively skewed — the mean and the win rate say nothing about viability until
the loss tail is priced honestly, because the tail events are (a) rare (a
handful per decade of weekly history, §7.2), (b) huge relative to the per-day
credit (a single leg's honest stop-fill loss can exceed months of collected
premium — illustrative, §4.2), and (c) exactly where naive fill assumptions
break (halts have no market to fill in; gaps fill at the open, not the trigger;
liquidity vacuums fill hundreds of points away — all sourced, §7.2). Practitioner
backtests of this family report 65–80% win rates alongside average losses 2–3×
average wins (sourced, §7.5) — that shape is the strategy, not an anomaly. A
few profitable months are therefore *expected under the null*; they are not
evidence. The only honest tests are: the measured worst days under a pessimistic
fill law (§6, pending data), and stress scenarios the sample may not contain
(§8).

---

## 3. Feasibility on BruteX

All findings executed against the working tree 2026-07-18 (measured unless
marked). Full detail in the feasibility dossier; consolidated here.

### 3.1 Data present in this container: NONE (measured)

- `BRUTEX_LAKE_ROOT` unset; lake resolution `brutex/config/paths.py:62-96`
  → `$HOME/.brutex/lake`; `/root/.brutex` does not exist (executed probes).
- Filesystem sweeps found **0** bar-data parquet files (the only 4 parquet hits
  are results-store test fixtures), **0** `*.duckdb` (no instrument master, no
  expiry registry). Zero spot bars, zero option bars. Expected for a fresh
  ephemeral clone; the pull must happen on the operator box.

### 3.2 Capability table (file:line-cited)

| Capability | Status | Citation |
|---|---|---|
| Option pull chain `get_expiries` → `get_contracts` → 1-minute candles per contract | EXISTS | `brutex/dashboard/metadata_pull.py:671`; vendor docs `docs/groww/api-reference/03-backtesting.md:43,91` |
| Default option window `expiry − 30d → expiry`; expiry-level date scoping | EXISTS | `brutex/adapters/groww/contract_bounds.py:104,157,298`; `adapters/groww/expiry_scope.py` |
| ALL listed strikes fetched (no strike skipped; deep-OTM only gets a reduced empty-RETRY budget) | EXISTS | `brutex/adapters/groww/strike_chain_bound.py:1-42` (":29-33 — never SKIPS a strike, never drops a bar") |
| Lake layout `bars/<exch>/FNO/<groww_symbol>/1minute/<yyyy>/<mm>.parquet`; right-anchored symbol grammar incl. the BSE form `BSE-SENSEX-25Sep25-79500-PE` | EXISTS | `brutex/data/fno_symbol.py:3-5,14-24` |
| SENSEX/BSE-FNO ingest actually served by Groww | **UNVERIFIED** — structural-only; probe never executed | `brutex/adapters/groww/constants.py:93`; `brutex/universe.py:216-217`; `brutex/crossverify/canonical.py:60-61` (BSE_FNO "STRUCTURAL-only"; DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 closed MOOT unprobed); `docs/groww/probe/` holds no BSE transcript (executed ls) |
| Expired-option 1-minute depth to 2020 | **UNVERIFIED** — Phase-0 probe gate | CLAUDE.md §6 ("1-minute granularity at the 2020 boundary — UNVERIFIED"); `docs/adr/0009-*.md:105`; no expired-depth transcript under `docs/groww/probe/` (executed grep) |
| 1-minute option lake reader (full-contract bar map) | EXISTS, reusable | `brutex/options/lake_source.py:39,141-167` |
| Worst-case fill law (hardcoded, single member) | EXISTS | `brutex/engine/trade_sim.py:26-59`; `brutex/execution/fill_mode.py:40,72,92` |
| SHORT option positions | **ABSENT by decree** — "Track 2 only ever BUYS options … never writes / shorts" (DEC-EXEC-008) | `brutex/options/executor.py:38-41` |
| Multi-leg (straddle/spread) | **ABSENT by decree** (DEC-EXEC-009) | `brutex/options/executor.py:13-14` |
| Intraday option-leg MTM / per-minute walk | **ABSENT** — executor prices exactly TWO bars per leg (entry + forced exit) | `brutex/options/lake_source.py:141-167`; `options/executor.py:532-539` |
| Trailing SL / trailing TP | **ABSENT anywhere** — grep over engine/options/futures/strategies → 0 hits (measured) | feasibility dossier §3(a) |
| Intraday SL/TP walker | EXISTS for SPOT only (fixed levels, forced 15:20) — engine is spot-only by decree | `brutex/engine/trade_sim.py:151-152,320-339`; `brutex/universe.py:388,427,574` (DEC-ENGINE-SPOT-ONLY-001 #1790) |
| Margin / SPAN model | **REFUSAL-ONLY, zero verified rows** | `brutex/engine/margin_history.py:10-27`; `docs/track2/TRACK2_RISK_MGMT.md` §2.3 forbids hardcoded constants |
| Expiry calendar (DOW rules 2020-2026) | EXISTS but does NOT resolve holiday-shifted expiries ("downstream responsibility") | `brutex/options/expiry_calendar.py` (:28-30); `registry.duckdb` absent locally |
| Overfitting cascade + gates | EXISTS (see §8) | `brutex/metrics/cascade.py:537` et al. |

### 3.3 The gap list (build/pull/probe)

1. **PULL the lake** (operator box, outside the 08:45–15:35 IST market-hours
   guard — DEC-GROWW-MARKET-HOURS-GUARD-001 #1692). Fetch-scale ESTIMATE
   (derived, not measured): NIFTY weekly ≈ 52 expiries/yr × ~200-280 CE+PE
   contracts ≈ 10-15k contracts/yr; at the measured production 2.4-2.6
   fetches/window (MIGRATION.md #1132), the honest 2024-10 → 2026-07 window ×
   2 underlyings ≈ 90-150k fetches ≈ 3-8 wall-hours at the 480/min governor
   ceiling, closer to **8-13 h** at the historically achieved ~3.2 r/s.
2. **BUILD a short-premium multi-leg trailing walker** — net-new engine work
   under its own DEC/ADR (nothing shorts, nothing walks an option's own minute
   series, nothing trails; §3.2). Reusable: lake reader, cost calculator,
   `fno_symbol` grammar, expiry-DOW registry, fill-law conventions.
3. **BUILD a margin/SPAN model** — refusal-only today; without sourced
   SPAN/ELM history, returns can only be stated per-lot, never on capital.
4. **PROBE SENSEX/BSE-FNO availability + expired-option 1-minute depth** —
   both UNVERIFIED probe gates (§3.2).
5. **RESOLVE expiry-calendar truth** — metadata pull to materialize
   `registry.duckdb` + a holiday-shift resolution layer (an expiry-DAY strategy
   needs the actual expiry date exactly right; the static DOW rules do not
   handle shifts).

### 3.4 Costs and the honest priceable window (measured, file:line)

- Brokerage: Groww flat ₹20/order — `brutex/costs/constants.py:45,661-669`.
- Options STT (sell-side premium), `brutex/costs/constants.py:57-95`:
  0.0625% pre-2024-10-01 → 0.10% (2024-10-01→2026-03-31) → 0.15% (from
  2026-04-01); exercise STT 0.125%/0.15% on intrinsic. (The futures STT ladder
  0.02%/0.05% is a DIFFERENT table — `brutex/costs/futures_costs.py`.)
- ETC: exactly TWO verified rate rows repo-wide — NSE 0.03503% + BSE 0.03250%,
  both effective 2024-10-01 (`brutex/costs/constants.py:429-470`;
  DEC-ETC-REGIME-HISTORY-001 #1524). Every earlier window is a rate-less
  REFUSAL row; the executor maps a pre-boundary trade date to a counted
  `COST_REGIME_UNVERIFIED` reject before any lake I/O
  (`options/executor.py:530,619`).
- **HONEST PRICEABLE WINDOW: 2024-10-01 → today (right edge moves).** Option
  trades dated before 2024-10-01 are counted refusals, never priced. Any
  backtest span quoted beyond that window is, in this repo, unpriceable by
  law. (The companion harness mirrors this: `--costs post2024` prices only
  trades ≥ 2024-10-01 and marks earlier trades `cost_refused` — §5.1.)

---

## 4. Fill realism under DEC-FILL-WORST-CASE-ONLY-001 (#1825)

### 4.1 The mechanism (code-cited)

Worst-case adverse fills are THE ONLY fill model — operator directive, ADR
ACCEPTED (`docs/adr/0023-worst-case-fills-only.md:8-16,36-50`). Fill TIMING:
signal bar N → fill bar N+1; same-bar SL+TP tie is ALWAYS SL-first. Fill
PRICE: the worst the decided bar printed —

- Law statement: `brutex/engine/trade_sim.py:26-45` ("a triggered BUY exit
  (sell) fills at `min(level, bar low)`, a SELL exit (cover) at
  `max(level, bar high)` — a gap-through bar fills at the extreme").
- Implementation: `trade_sim.py:733-747` (`min(level, low)` BUY /
  `max(level, high)` SELL), comment at `:721-732` (the clamp can never
  overstate a favourable gap).
- Entry at the entry bar's ADVERSE extreme — BUY at HIGH, SELL at LOW
  (`trade_sim.py:33-36`); SL/TP levels anchor on the actual worst entry fill.
- Translation executors: option fills anchor on "the option's own minute-bar
  ADVERSE EXTREME, the ONLY fill law" (`options/executor.py:18-28,580-589`);
  futures mirrored adverse both legs (`futures/executor.py:24-31`).
- A SET `BRUTEX_FILL_MODE` — any value, even the literal `worst_case` —
  refuses at launch (`execution/fill_mode.py:40,92`).
- INTERIM honesty: the adverse extreme is the DECISION-TF bar's until the
  1-minute execution grid lands — strictly MORE pessimistic, never optimistic
  (`trade_sim.py:69-73`).

**Why this is the make-or-break for a short option stop on expiry day.** Near
expiry, gamma is at its lifetime maximum: the ITM-going leg's delta ramps
toward ~1 within a small index move, so ΔPremium ≈ Δindex. A single 1-minute
bar can span from ~the SL trigger to several multiples of it. A naive backtest
fills the stop AT the trigger; the repo law fills a short being bought back at
`max(SL_level, bar_high)` (`trade_sim.py:739`). The entire difference between
"backtest edge" and "real bleed" is concentrated in that one clamp.

### 4.2 ILLUSTRATIVE scenarios — arithmetic illustrations, NOT measured backtest results

Common assumptions (illustrative): NIFTY ≈ 24,000; lot **75** (post-20-Nov-2024
— `brutex/options/lot_size_history.py:16,62`, measured); short ATM straddle
sold expiry morning for ₹180 combined (CE ₹90 + PE ₹90); per-leg SL 30% →
trigger ₹117.

| Scenario | Naive fill (at trigger) | Honest fill (repo law) | Multiple |
|---|---|---|---|
| **1 — news/war candle:** 200-pt spike in 2 min; CE runs ₹90→~₹300, trigger bar low ₹110 / high ₹260 | (117−90)×75 = **₹2,025/lot** | `max(117, 260)` = ₹260 → (260−90)×75 = **₹12,750/lot** | **≈ 6.3×** (illustrative) |
| **2 — gap through the strikes:** index opens −3% (−720 pts); short 24,000 PE ~720 pts ITM at open, premium ≈ intrinsic; SL ₹117 decorative | **₹2,025/lot** | buy-back ~₹750 → (750−90)×75 = **₹49,500/lot** | **≈ 24×** (illustrative) |
| **3 — circuit halt / freeze:** no fill possible until reopen | fills at trigger anyway (the naive lie) | see §4.4 — the repo drops (NO_DATA) or closes at the stale pre-halt bar; even the pessimistic law is knowingly optimistic HERE | n/a |

HONEST REPO NOTE on Scenario 2: the repo engine as-built cannot hold overnight
(forced 15:20 session-close exit + the session-boundary law,
`trade_sim.py:46-67`) — the scenario applies to the real-world strategy shape
and to any naive external backtest of it, not to a BruteX sim as-built.

### 4.3 The documented real-world proof: 20 Aug 2021 (sourced)

NSE removed the Trade Execution Range on 16 Aug 2021; on 20 Aug 2021 the NIFTY
16450 CE spiked from **~₹100 to ₹803.05** in moments on a liquidity vacuum,
and sellers' **SL-M orders placed at ~₹150 were filled near ₹800** (sourced:
https://www.paytmmoney.com/blog/freak-trade-nifty/ — the primary figures for
this study[^freak]) — **~650 points of slippage, ≈ ₹35,000 loss per lot vs the
~₹2,500/lot the backtest assumed** (also
https://marketscanner.in/2021/08/the-freak-trade-16450-ce/). The Marketcalls
analysis of the 9:20-straddle community documents 100–680 points of SL-M
slippage (₹5,000–₹34,000/lot) in this event class (sourced from search
snippet — fetch 403'd, re-verify:
https://www.marketcalls.in/futures-and-options/how-the-9-20-intraday-straddlers-are-being-gamed.html).
This is direct, sourced proof that "SL fills at trigger" is an illusion in
Indian index options — and note the repo's `max(level, high)` law would have
priced this fill honestly.

[^freak]: A secondary report gives the move as ₹112 → ₹803 (~7×). The
    primary figures used throughout this study are ₹100 → ₹803.05 with SL-M
    ~₹150 orders filling near ₹800 (sourced: Paytm Money blog, URL above);
    the ~₹35,000/lot arithmetic keeps the primary shape either way.

### 4.4 Residual understatements — even the repo's worst-case law is a floor

The naive-vs-honest gap is concentrated EXACTLY on the days that dominate this
strategy's P&L; but even worst-case-per-minute-bar STILL understates real
losses. Honest residual list:

1. **Sub-minute path** — the true adverse sequence inside a minute (partial
   fills, queue position) can exceed what the printed bar extremes imply.
2. **Spread/slippage blowout** — the repo models 1-tick slippage around the
   adverse anchor (`options/executor.py:18-23`); real expiry-afternoon spreads
   on stressed strikes are many ticks wide and market SL orders sweep the book.
3. **Broker RMS square-offs** — forced liquidation at broker-chosen prices when
   margin spikes; no RMS model exists (margin is refusal-only per the BUY-only
   decree block, `options/executor.py:38-41`; `engine/margin_history.py:10-27`).
4. **Halt/reopen repricing** — the `session_gap` law closes at the PRIOR
   session's last stored bar's adverse extreme (`trade_sim.py:60-67,575-585,
   794-810`; DEC-TRADESIM-OPEN-SESSION-GUARD-001 #1743) — a stale-price
   approximation that UNDERSTATES a real halt loss (post-reopen marks are
   worse); NO_DATA drops the trade entirely rather than pricing its
   catastrophic version (`options/executor.py:540-541`) — a survivorship tilt
   the repo documents (`options/executor.py:606-617`).
5. **STALE_BAR survivorship** — volume-0 fill bars are unconditional rejects
   (`options/executor.py:543-557`; ADR-0023 §4) — the minutes that don't print
   are disproportionately the bad ones.
6. **Freeze-quantity chunking / exchange throttles** on large multi-lot exits —
   not modeled anywhere.

---

## 5. Strategy spec + grid (the concrete testable version)

Two grids, deliberately distinct: §5.1 is the **daily-bar-bracketable grid
the built harness measures today** (matches `bhavcopy_tail_study.py`
exactly); §5.2 is the **full 1-minute grid** a future engine build would
sweep. Conflating them is exactly the fill-assumption error §4 documents.

### 5.1 The harness grid (built; daily-bar-bracketable — matches the code)

Implemented in [`bhavcopy_tail_study.py`](./bhavcopy_tail_study.py)
(constants `VARIANTS`, `SL_GRID`, `NIFTY_LOT_HISTORY`; verified 2026-07-20):

- **Variants** (strike offset from the spot proxy):
  - **V1 — short ATM straddle**: ONE strike nearest the spot proxy, both legs;
  - **V2 — short strangle ±1%**: CE nearest proxy×1.01, PE nearest proxy×0.99;
  - **V3 — short strangle ±2%**: same at ±2%.
- **Spot proxy**: the OPEN of the **nearest-expiry NIFTY index FUTURE** in the
  same bhavcopy (the index itself is not in the derivatives bhavcopy) — a
  same-file 09:15-ish level proxy, documented in-code.
- **Per-leg SL grid {20%, 30%, 50%}** above entry premium (trigger =
  entry × (1+SL); pierced when the day's HIGH ≥ trigger — boundary inclusive),
  legs independent (NO combined-premium SL scope at daily-bar resolution),
  **plus a NO-SL hold-to-close baseline**.
- **Both fill brackets per pierced leg**: *optimistic* (fills AT the trigger —
  the naive assumption) vs *pessimistic* (fills at the day bar's HIGH — the
  daily-bar analogue of the repo's `max(level, high)` clamp, §4.1). The
  per-day spread between the brackets IS the measured naive-vs-honest gap.
- **Per-lot ₹ columns** via the dated NIFTY lot table, repo-sourced from
  `brutex/options/lot_size_history.py:58-64`: **75 → 50 (29-Jul-2021) →
  25 (26-Apr-2024) → 75 (20-Nov-2024) → 65 (Jan-2026)**; the pre-2021 span is
  a back-extension at 75 marked **EXTERNAL-UNVERIFIED** in-code (public
  history is consistent, not independently re-verified).
- **Costs**: gross premium points by default; `--costs post2024` overlays
  ₹20/order brokerage + sell-side STT (**0.10% from 2024-10-01, 0.15% from
  2026-04-01**; the 0.0625% rate is the PRE-2024-10 era and is never applied
  post-boundary) + NSE ETC 0.03503% for trades ≥ 2024-10-01, and marks
  earlier trades `cost_refused` — rates verified against
  `brutex/costs/constants.py:45,73,77,150` (the §3.4 stack).
- **NIFTY only**; SENSEX excluded pending the BSE bhavcopy probe (§3.2).

Out of harness scope by construction (daily bars cannot order intraday
events): entry-time variants, trailing SL/TP, combined-premium SL, re-entry,
a delta-targeted (25Δ) strangle (needs a Greeks chain), and the podcast's
"all-strikes ladder" — all §5.2 territory.

### 5.2 The full 1-minute grid (future; needs the §3.3 pull + walker) — 432 cells

Variants for the full test: **V1** short ATM straddle (ATM per
`brutex/options/strike_grid.py` `round_to_atm`/`get_strike_step`); **V2**
short ~25Δ strangle (delta-target selection needs a caller-supplied delta
chain — executor delta rules are not run by default,
`options/executor.py:30-36`; the lake's iv/delta Greeks overlay makes a 25Δ
picker buildable but unwired; the harness's ±1% strangle is its daily-bar
stand-in); **V3** the "all-strikes ladder" (the podcast claim): 1 lot of every
listed strike, both sides. Ladder capital sanity check (ILLUSTRATIVE; the repo
contains NO margin data): ~40 strikes × 2 sides = 80 short lots; at ~₹1–1.5L
margin per short index-option lot (**UNVERIFIED external figure**) ≈
₹80L–₹1.2Cr of margin to collect mostly-far-OTM premia. The structural point
survives any reasonable margin figure: per-rupee-of-margin premium yield
collapses as the ladder extends outward, while tail loss grows with every
ITM-crossing strike.

| Axis | Values | Count |
|---|---|---|
| Variant | V1, V2, V3 | 3 |
| Entry time (IST) | 09:20, 09:45, 12:00 | 3 |
| SL level × scope | {20%, 30%, 50% of received premium} × {per-leg, combined} | 6 |
| Trailing-SL step | {10%, 25% of gained premium} | 2 |
| Trailing-TP step | {10%, 25%} | 2 |
| Hard exit | 15:20 IST (fixed — the repo's forced-exit law, `trade_sim.py` + CLAUDE.md §7) | 1 |
| Re-entry | none (fixed) | 1 |
| Instrument | NIFTY weekly (NSE); SENSEX weekly (BSE) | 2 |

An earlier hard-exit buffer (e.g. 15:15 IST) is a tunable variant an
implementation may add, not the repo law — the spec pins 15:20 to match the
engine's forced session-close exit.

**3 × 3 × 6 × 2 × 2 × 2 = 432 cells** — deliberately tiny vs the engine's ~33B
sweep; a real grid, not a fantasy sweep. Lots: NIFTY 75 / SENSEX 20
post-Nov-2024 (`lot_size_history.py:16,62,74-79`; full NIFTY ladder in §5.1).
SENSEX weekly BSE data availability remains UNVERIFIED (§3.2).

### 5.3 What needs 1-minute data vs what daily bars can bracket

- **Needs 1-minute option bars** (the pull of §3.3): everything path-dependent
  — SL/TSL/TTP triggering, entry-time premium, intraday MTM, the honest
  bar-extreme fill law itself. Without the option's own minute series none of
  the 432 cells can be evaluated as specified.
- **Daily bars (NSE bhavcopy) can BRACKET, not evaluate** — this is exactly
  what the §5.1 harness does: per-expiry-day outcome bounds per short leg from
  the option series' OHLC — an OPTIMISTIC bracket (SL assumed filled at
  trigger whenever the day's high crossed it) and a PESSIMISTIC bracket (SL
  filled at the day's high — the daily-bar analogue of the repo's
  `max(level, high)` clamp). The truth lies between; the bracket width IS the
  measured naive-vs-honest gap. This is the §6 pending measurement, and it
  can cover the pre-2024-10 era descriptively (P&L points) even where the
  cost stack refuses to price it (the harness prices only the ≥ 2024-10-01
  subwindow under `--costs post2024`).

---

## 6. Measured tail — [PENDING MEASUREMENT — BLOCKED-ON-DATA]

> **Nothing in this section is a result.** The measurement harness is BUILT
> and fixture-verified (`bhavcopy_tail_study.py`, offline suite green), but
> it is **BLOCKED-ON-DATA in the authoring environment**: the org egress
> proxy denies the NSE/BSE archive hosts (CONNECT 403 on every attempt —
> evidence summarized in §9, limit 7), so no bhavcopy byte has been fetched
> here. The numbers for this section are produced by running
> `bhavcopy_tail_study.py` on an **open-egress box** — `--smoke` first, once
> per bhavcopy format era, then the full run (README "Run commands") — and
> its `summary.md` tables paste directly into the placeholders below. Each
> populated cell must carry a (measured: bhavcopy) tag + the generating
> command/commit. Do not quote this section before then.
>
> **Remediation options** (any one unblocks the measurement):
> 1. **Allowlist the 4 hosts** on the org egress proxy —
>    `archives.nseindia.com`, `nsearchives.nseindia.com`, `www.nseindia.com`
>    (NSE), `www.bseindia.com` (the SENSEX probe) — and run here.
> 2. **Run on the operator M4-Pro** (open egress; ~350 files at ~1 file/s,
>    fully cached for offline re-runs).
> 3. **Manual zip upload**: fetch the weekly bhavcopy zips elsewhere, drop
>    them into `--cache-dir`, run with `--from-cache-only` (never touches the
>    network).
>
> **Placeholder → harness-emitter map** (headings as emitted by
> `build_summary_md`):
>
> | §6 placeholder | harness output |
> |---|---|
> | §6.1 per-expiry-day P&L | `per_day_results.csv` — one row per (expiry_date, variant, sl_pct, bracket), incl. spot-proxy open/close/day-move-% |
> | §6.2 worst-10 days | `summary.md` → per-variant "Worst 10 days — SL n% (by pessimistic ₹/lot)" tables (optimistic vs pessimistic side-by-side + loss ratio) |
> | §6.3 loss distribution | `summary.md` → "P&L percentiles (₹ per lot, gross)" (p1/p5/p25/median/p75/p95/p99) + "Summary (₹ per lot, gross)" (win rate / avg win / avg loss / max DD) |
> | §6.4 per-year split | `summary.md` → "Per-year totals (₹ per lot, gross)" subtables |
> | §6.5 naive-vs-honest gap | `summary.md` → the worst-10 optimistic-vs-pessimistic columns + the per-SL "Days where the pessimistic loss ≥ 5× the optimistic loss" counter; cost sensitivity via the `--costs post2024` run's "Net-of-costs totals — priced subwindow (≥ 2024-10-01 only)" section |
>
> Honest-limits carry-over (README): daily bars cannot order intraday events;
> traded-price extremes make the pessimistic tail a LOWER BOUND; URL stems +
> column layouts are UNVERIFIED-until-smoke; `no_expiry_found` weeks are
> counted, never fabricated (`counters.json`).

### 6.1 Per-expiry-day P&L (bracketed fills)

| Expiry date | Index close move % | V1 P&L/lot (optimistic bracket) | V1 P&L/lot (pessimistic bracket) | Bracket width | Notes |
|---|---|---|---|---|---|
| [PENDING] | | | | | |

### 6.2 Worst-10 expiry days

| Rank | Date | Event | P&L/lot (pessimistic) | Multiple of median winning day | Sources |
|---|---|---|---|---|---|
| [PENDING] | | | | | |

### 6.3 Loss distribution

| Statistic | Optimistic bracket | Pessimistic bracket |
|---|---|---|
| Win rate | [PENDING] | [PENDING] |
| Median winning day (₹/lot) | | |
| Median losing day (₹/lot) | | |
| p95 / p99 / max loss day (₹/lot) | | |
| Skew | | |

### 6.4 Per-year split

| Year | Expiry days | Net P&L/lot (opt.) | Net P&L/lot (pess.) | Worst day | Regime notes (weekday/lot/margin breaks per §7.3) |
|---|---|---|---|---|---|
| [PENDING] | | | | | |

### 6.5 The naive-vs-honest gap, measured

| Metric | At-trigger fills (naive) | Bar-extreme fills (honest bracket) | Gap |
|---|---|---|---|
| Full-period net P&L/lot | [PENDING] | [PENDING] | |
| Worst single day | | | |
| Share of days where the gap > 2× | | | |

Acceptance rule for §6 (pre-registered): the study's verdict keys on the
**pessimistic** bracket and the worst-10 table, not the mean — per §2.

---

## 7. External evidence (compiled 2026-07-18; every claim carries its source URL; figures are as reported by the cited source and should be re-verified against primaries before being quoted as fact)

### 7.1 SEBI retail F&O loss studies — the base rate

| Study | Period | Headline (sourced) | Source |
|---|---|---|---|
| SEBI study, publ. 25 Jan 2023 | FY22 | **89%** of individual equity F&O traders lost money; avg loss ~₹1.1 lakh | SEBI PR 86906 (references it): https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html |
| SEBI updated study, PR 23 Sep 2024 | FY22–FY24 | **93%** of >1 crore individual F&O traders lost; aggregate **>₹1.8 lakh crore**; FY24 alone >₹61,000 crore; <1% earned >₹1 lakh net/yr; prop desks +₹33,000 cr, FPIs +₹28,000 cr gross in FY24 (largely algo) | Primary: SEBI PR No. 86906 (URL above) |
| SEBI study, Jul 2025 | FY25 | **91%** lost; net losses **₹1.06 lakh crore in FY25 alone** (+41% YoY); unique traders 61.4L → 42.7L within FY25 post-curbs | https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html |

Base-rate takeaway: across three transaction-level studies the individual loss
rate never dropped below ~89%, aggregate losses GREW after every tightening,
and the winners concentrate in proprietary/FPI algo desks. Any "made massive
money" claim is weighed against ~9-in-10 individuals losing.

### 7.2 Documented disaster/gap days for Indian index short-option books

| Date | Event | Move (sourced) | Short-book implication |
|---|---|---|---|
| **13 Mar 2020** | COVID panic | NIFTY hit the **−10% lower circuit at 09:20**; trading **HALTED 45 min** | No exit exists during a halt; SL-at-trigger backtests are structurally impossible on halt days |
| **23 Mar 2020** | Lockdown panic | Second −10% circuit halt (NIFTY −9.4% early, closed ~−13% — close figure UNVERIFIED in captured sources); VIX peak ~86 on 24 Mar UNVERIFIED | Overnight shorts had no exit until reopen |
| **24 Feb 2022** (**Thursday = NIFTY weekly expiry day**) | Russia invades Ukraine | NIFTY **−4.78% (−815 pts)**; gap-down ~3% open | A weekly-expiry-day gap: prior-evening few-rupee short puts went deep ITM at the open with zero time to adjust — the exact case the theta-income pitch ignores |
| **4 Jun 2024** | Election results below exit polls | NIFTY **−5.93% close**; **India VIX +23–27%**; intraday deeper (UNVERIFIED); 50–100× put-premium anecdotes **ANECDOTAL/UNVERIFIED** | Expiry-week 2-3 DTE short books crushed |
| **5 Aug 2024** | Yen-carry unwind | Indices ~−2.7–3% (exact UNVERIFIED); VIX spike ~+40–60% intraday UNVERIFIED | Pure external shock — unforecastable by a "sell every expiry" system |
| **7 Apr 2025** | Trump tariff shock | Sensex intraday **−5.22%**; **India VIX +56–59% intraday** | Overnight gap + vol doubling before any stop could act |
| **7–9 May 2025** | India-Pakistan escalation | VIX +~5%/day, **+10% on 9 May** | Multi-day vol regime shift repriced IV against sellers without a huge index move |
| **20 Aug 2021** | NIFTY 16450 CE freak trade (post-TER-removal) | Option **~₹100 → ₹803.05** | THE documented SL-slippage case — §4.3 |
| **Expiry-day "hero-zero" class** | Recurring last-hours gamma blasts | e.g. 16 May 2024: OTM PE **~₹2→₹200 (~100×)** (practitioner-documented, not exchange-verified) | A ₹2 short can become a ₹200 liability in under an hour |
| **Jane Street order, 3 Jul 2025** | SEBI ex-parte interim order | 18 identified expiry days (15 BANKNIFTY + 3 NIFTY, Jan-2023→May-2025) allegedly manipulated; **₹4,843.57 crore impounded** | Per the regulator, expiry-day prints inside the very sample window a podcast backtest would cite were at times engineered |

(Full source URLs for each row: §10.)

### 7.3 Regime breaks — the history is short and non-stationary

- **Product ages:** Bank Nifty weeklies 27 May 2016 → discontinued as a
  weekly product in Nov 2024 — two distinct, compatible dates: the **last
  BANKNIFTY weekly expiry traded 2024-11-13** (registry evidence), and the
  **SEBI one-weekly-per-exchange rule took effect 20-Nov-2024** (the first
  Wednesday after 13-Nov had no weekly contract left to expire; the dates do
  not conflict — one is the last listed contract, the other the rule
  boundary); **NIFTY weeklies 11 Feb 2019** (year reported-but-verify;
  primary NSE circular not captured) — ~7 years, ~2 COVID-distorted;
  **SENSEX weeklies May 2023** — ~3 years across ≥3 expiry-day regimes.
- **SEBI Oct-2024 framework** (circular 1 Oct 2024): one weekly per exchange
  from 20 Nov 2024 (NSE→NIFTY, BSE→SENSEX); contract size ₹15–20L → NIFTY lot
  25→75, SENSEX 10→20; **+2% ELM on SHORT options on expiry day** from
  20 Nov 2024; calendar-spread margin benefit removed on expiry day + upfront
  buyer premium (both from 1 Feb 2025 per broker summaries — verify against
  the circular); intraday position-limit monitoring from 1 Apr 2025.
- **Expiry-weekday migrations:** NIFTY Thursday (2019→Aug 2025) → proposed
  Monday (deferred) → **Tuesday from Sep 2025**; SENSEX Friday (May 2023) →
  Tuesday (Jan 2025) → **Thursday (Sep 2025)**. Fatal for naive day-of-week
  backtests.
- **2021 microstructure break:** NSE removed the Trade Execution Range on
  16 Aug 2021 — enabling the freak-trade class (§4.3).

Net: an "N-year backtest" of SENSEX expiry-day selling spans ≤~3 years and ≥3
expiry weekdays, a ~3× lot change, new expiry-day margins, and (per SEBI) a
period of alleged expiry-day manipulation. NIFTY weeklies span ~7 years across
the same breaks. Historical P&L is not a stationary sample.

### 7.4 International short-vol precedents

| Event | Date | Numbers (sourced) |
|---|---|---|
| **Volmageddon / XIV ETN** | 5 Feb 2018 | VIX **+116% in one day** on a −4.1% S&P day; **XIV −96%** (much of it in ~50 min after-hours); terminated at **$5.99/share** from ~$99 highs |
| **LJM "Preservation & Growth"** | 5–6 Feb 2018 | **−80% in 48 h**; AUM $812M → $14M; SEC charged managers — their marketed "worst-case daily loss" was the worst historical day, doubled and rounded (naive extrapolation as risk management) |
| **OptionSellers.com** | 15 Nov 2018 | Nat-gas +~60% in days; **~$150M client money wiped**; broker pursued clients for **$35.3M in DEBIT balances** — losses >100% of capital |

Common structure: years of steady premium income, then a 1–2 day event that
destroyed 80–100%+ of capital. "It worked for years" is what every survivor
said until the day it didn't — and LJM's risk model was literally the naive
historical extrapolation the podcast claim rests on.

### 7.5 Practitioner backtests — and their shared fill-assumption flaw

Quant-blog / practitioner sources, not peer-reviewed; nearly all assume **SL
fills at the trigger price**, the exact assumption §4.3's documented events
falsify:

- "Optimized 9:20 Straddle" (Nomad Trader, Medium): high annual return, max DD
  ≈ ₹17,000 per-lot over ~5 yr; SL-at-trigger, no slippage — promotional,
  date UNVERIFIED.
- Quintal Mind (time-based NIFTY straddles): win rate ~65–70% with 25% SL but
  **average loss 2–3× average win**; expiry-day 13:00 entry ~75–80% win on tiny
  premium; explicitly notes real results **15–30% worse than backtests**
  (slippage, ITM STT, spreads) and 2020 short-straddle drawdowns of "30–50% of
  capital" — methodology UNVERIFIED.
- OPTIONSCRACK expiry-day 9:20 variant backtest exists; figures not captured —
  UNVERIFIED.
- Marketcalls: the community's own analysts concluded SL-at-trigger backtests
  materially overstate the strategy (100–680 pt SL-M slippage documented).
- TradingQnA thread + practitioner cluster: the plain 9:20 straddle "stopped
  working after the 2020 crash" — ANECDOTAL.
- **No credible, audited, slippage-modelled public backtest of expiry-day short
  straddles reporting honest worst-day tail loss was found. The absence of an
  audited winning track record is itself a finding.**

---

## 8. Overfitting-gate mapping (what the shipped machinery catches — and what it structurally cannot)

How a "made massive money for a few months" result meets the repo's cascade
(`brutex/metrics/cascade.py`, `run_stage4_cascade` at `:537`; order: hard gates
→ DSR → CPCV → Reality-Check/SPA → PBO → Romano-Wolf → WFO → Lockbox →
Monte-Carlo, `:604-831`):

- **DSR** deflates the observed Sharpe against the trial population
  (`cascade.py:628-643`; cut-off `dsr_min = 0.95` at `:262`; `dsr_n_source`
  stamped `:389-399`; pool-N pass only PROVISIONAL `:852-854`). The 432-cell
  grid IS the trial population — a lucky few-month Sharpe gets deflated
  against all 432 siblings.
- **CPCV** pass rate over C(16,8) = 12,870 purged splits
  (`brutex/metrics/cpcv.py:94-98`; threshold 0.5 at `cascade.py:264`; recorded
  on every verdict `:314-328,858-863` and surfaced as `cpcv_pass_pct` —
  DEC-RESULTS-CASCADE-VERDICT-JOIN-001 #1749). A strategy profitable only in
  one contiguous stretch fails most splits that exclude it.
- **One-regime-wonder** flag (`combo_aggregates.py:253-254,546-611`;
  `REGIME_WONDER_SHARE = 0.8` at `:89-93`) over calendar YEARS then TREND
  regimes; the VIX complement `vix_band_lo/hi` (= [p10,p90] VIX of winning
  trades, `:185-187`) shows an expiry-seller's "works when VIX between X–Y" on
  the card. Honest note: it flags, it does not reject (`:91-92`).
- **Per-year stability** (`pnl_by_year_json` `:191-192`), WFO pass ≥ 0.75
  (`cascade.py:275`), held-out Lockbox (`:203-215,806`).
- **Hard gates** (`brutex/ranking/__init__.py:240-264`; `gate_reasons` `:267`):
  min_trades, profit_factor < 1.2, max_drawdown > 30%, win/WLR,
  max_consecutive_losses > 10, fail-closed `non_finite_input`. An expiry
  seller sails through the win-rate doors and dies on drawdown/streaks ONLY IF
  the sample contains its tail.

**The structural limit — tiny-N tails.** ~52 weekly expiry days/yr → a 3-year
window holds ~156 independent observations, of which the P&L-defining tail
events number **0–3 — and can legitimately be ZERO**. Every gate above is a
*resampling* of the observed sample; **none can manufacture tail evidence that
isn't in it.** With a tail-free window, a short-premium strategy can pass
DSR ≥ 0.95, CPCV ≥ 0.5, WFO, Lockbox and all five hard doors with its ruin risk
fully intact. The repo's own honesty apparatus points the same way (worst-5-days
is explicitly a *view*, `combo_aggregates.py:95-96`; survivorship disclosures
at `options/executor.py:606-617`).

**Recommendation — stress-scenario INJECTION.** The honest fix is synthetic
gap/halt/spike days priced under the worst-case fill law (§4.2's scenarios +
§7.2's documented days as mandatory test vectors), not more resampling of a
tail-free sample. No such injection harness exists in the repo today; it would
be a natural sibling of the R45 independent reference sim + the fault-matrix
chaos tests, but for market scenarios rather than process faults. (Follow-up
DEC territory; no ID minted.)

---

## 9. Honest limits & what a full test requires

**Limits of this study as it stands:**

1. **No measurement yet** — §6 is empty by design; every quantitative tail
   claim above is illustrative or sourced-external until the bhavcopy study
   runs (harness built; blocked on egress — limit 7).
2. **This container holds zero market data** (§3.1); nothing here was run
   against a lake.
3. **The honest priceable window is 2024-10-01 → today (right edge moves)**
   (§3.4) — barely ~1.8 years at finalization, itself nearly tail-free;
   pre-window history can only be bracketed descriptively, never priced under
   the verified cost stack.
4. **SENSEX/BSE-FNO availability + expired-option 1-minute depth are
   UNVERIFIED probe gates** (§3.2).
5. **Margin is refusal-only** — returns can only be per-lot premium P&L, never
   on capital, without importing UNVERIFIED external margin figures.
6. **Even the honest fill law is a floor** — the six residual understatements
   of §4.4 all point the same (adverse) direction.
7. **The authoring environment is egress-blocked to the exchange archives
   (measured 2026-07-20)** — every fetch attempt against the NSE bhavcopy
   hosts (`archives.nseindia.com`, `nsearchives.nseindia.com`,
   `www.nseindia.com`; the BSE host `www.bseindia.com` for the SENSEX probe)
   was denied by the org egress proxy with **CONNECT 403** (proxy-level
   refusal, before any TLS/vendor response). Consequence: the §5.1 harness is
   fixture-verified only; its URL stems + column layouts stay
   UNVERIFIED-until-smoke (README limit 3). Remediation = any of the three
   §6-preamble options (proxy allowlist / operator M4-Pro run / manual zip
   upload + `--from-cache-only`).

**The full-test plan (in dependency order):**

1. **Bhavcopy bracket study first** (no new engine needed — the harness is
   BUILT: `bhavcopy_tail_study.py`, §5.1): per-expiry-day bracketed P&L per
   §5.3/§6 — cheap, covers the full weekly-product history descriptively, and
   measures the naive-vs-honest gap directly. Run per the §6 preamble
   (`--smoke` per format era first, on an open-egress box).
2. **Probes**: SENSEX/BSE-FNO availability on Groww; expired-option 1-minute
   depth at the 2020 boundary (transcripts under `docs/groww/probe/` per the
   ADR-0009 discipline).
3. **The Groww 1-minute pull** (operator box; §3.3 item 1 — est. 8–13 wall
   hours for the honest window, outside market hours, under the 480/min
   governor).
4. **The short-premium multi-leg trailing walker** — net-new engine under its
   own DEC/ADR: short legs, per-minute premium walk, combined/per-leg SL,
   trailing SL/TP, extending `trade_sim.py`'s adverse-max conventions
   (`max(level, high)` covers as the SL arm).
5. **Margin/SPAN sourcing** — required before any on-capital return claim.
6. **Stress-injection harness** (§8) — mandatory before any "passes the gates"
   claim is treated as survivable.
7. Only then: the 432-cell grid under the full cascade, verdict keyed on the
   pessimistic tail per §2/§6.

---

## 10. Sources appendix

### SEBI / regulatory
- SEBI PR No. 86906 (23 Sep 2024, FY22–FY24 study; references the Jan-2023 FY22 study): https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html
- Business Today on the FY22–FY24 study: https://www.businesstoday.in/markets/story/93-of-individual-traders-incurred-losses-in-equity-fo-during-fy22-fy24-sebi-study-447102-2024-09-23
- Moneylife on the FY22–FY24 study: https://moneylife.in/article/93-percentage-of-individual-traders-lost-rs18-lakh-crore-in-equity-fo-in-past-3-years-sebi/75210.html
- Business Standard on the FY25 study (7 Jul 2025): https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html
- Moneylife on FY25: https://www.moneylife.in/article/106-lakh-crore-lost-by-individual-traders-in-fo-in-fy2425-govt-confirms-sebi-action-on-4-entities-for-market-abuse/79124.html
- SEBI research listing: https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=4&ssid=81&smid=109
- SEBI Jane Street interim order (3 Jul 2025): https://www.sebi.gov.in/enforcement/orders/jul-2025/interim-order-in-the-matter-of-index-manipulation-by-jane-street-group_95040.html
- CNBC on Jane Street: https://www.cnbc.com/2025/07/04/indian-regulator-bars-us-trading-firm-jane-street-from-accessing-securities-market.html
- Oxford Business Law Blog on Jane Street: https://blogs.law.ox.ac.uk/oblb/blog-post/2025/07/jane-street-and-expiry-day-trap-unpacking-sebis-crackdown-algorithmic

### Disaster days
- 13 Mar 2020 circuit halt: https://www.business-standard.com/article/markets/nifty-hits-lower-circuit-amid-global-sell-off-trading-halted-for-45-mins-120031300188_1.html ; https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-circuit-breaker-rules-252013-2020-03-13
- 23 Mar 2020 circuit halt: https://www.businesstoday.in/markets/market-perspective/story/sensex-hits-circuit-breaker-of-10-trading-halted-on-bse-nse-252823-2020-03-23 ; https://tradebrains.in/market-circuit-breakers/
- 24 Feb 2022 expiry-day war gap: https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-russia-declares-war-ukraine-top-sensex-losers-in-early-trade-323722-2022-02-24 ; https://stableinvestor.com/2021/03/biggest-nifty-1-day-falls.html
- 4 Jun 2024 election day: https://www.business-standard.com/markets/news/stock-market-live-updates-june-4-sensex-nifty-lok-sabha-election-results-2024-psu-stocks-nse-124060400081_1.html ; https://www.businesstoday.in/markets/market-commentary/story/election-results-2024-india-vix-jumps-india-alliance-exceeds-expectations-432101-2024-06-04 ; https://www.bajajbroking.in/blog/stock-market-crash-and-election-results-2024-updates
- 5 Aug 2024 yen-carry unwind: https://www.bis.org/publ/bisbull90.pdf ; https://www.cnbc.com/2024/08/05/carry-trades-a-major-unwinding-is-underway-amid-a-stock-sell-off.html ; https://www.hdfcfund.com/learn/deep-dives/tuesday-talking-point/yen-carry-trade-unwinding-story-global-event
- 7 Apr 2025 tariff shock: https://www.cnbc.com/2025/04/06/stock-market-today-live-updates.html ; https://www.opindia.com/2025/04/trump-tariff-triggers-global-stock-market-bloodbath-indian-sensex-and-nifty-nosedive-concerns-of-trade-war-and-recession-loom/ ; https://ddnews.gov.in/en/sensex-nifty-tank-as-trump-tariffs-rattle-global-markets/
- 7–9 May 2025 India-Pakistan: https://upstox.com/news/market-news/trading/sensex-nifty-50-open-in-the-red-as-india-pakistan-conflict-escalates-india-vix-up-5-5-top-things-you-need-to-know/article-166362/ ; https://www.businesstoday.in/amp/markets/stocks/story/india-pak-tensions-fear-index-india-vix-continues-to-rise-jumps-5-today-475478-2025-05-09 ; https://www.business-standard.com/markets/news/markets-turn-shaky-n-escalating-india-pak-tension-125050801511_1.html
- 20 Aug 2021 freak trade: https://www.paytmmoney.com/blog/freak-trade-nifty/ (primary figures) ; https://marketscanner.in/2021/08/the-freak-trade-16450-ce/ ; https://www.marketcalls.in/futures-and-options/how-the-9-20-intraday-straddlers-are-being-gamed.html (fetch 403'd; figures from search snippet — re-verify)
- Hero-zero class: https://pivotcall.com/2025/06/18/hero-or-zero-strategy/ ; https://www.samco.in/knowledge-center/articles/zero-to-hero-option-trading-strategy/

### International blowups
- XIV/Volmageddon: https://www.sixfigureinvesting.com/2019/02/what-caused-the-february-5th-2018-volatility-spike-xiv-termination/ ; https://www.cnbc.com/2018/02/05/xiv-exchange-traded-security-linked-to-volatility-plummets-80-percent.html ; https://www.bloomberg.com/news/articles/2019-02-06/the-day-the-vix-doubled-tales-of-volmageddon
- LJM: https://www.cnbc.com/2018/02/08/fund-crashes-after-wrong-way-volatility-trade.html ; https://www.ai-cio.com/news/risky-business-leads-to-1-billion-trading-loss-charges-for-fund-execs/ ; https://medium.com/@navnoorbawa/the-650m-volmageddon-collapse-how-ljms-leveraged-short-vol-strategy-imploded-in-48-hours-61ad04060f2d
- OptionSellers.com: https://steadyoptions.com/articles/james-cordier-another-options-selling-firm-goes-bust-r429/ ; https://www.advisorhub.com/broker-seeks-millions-from-wiped-out-optionsellers-clients/ ; https://earlyretirementnow.com/2018/12/18/the-optionsellers-debacle/ ; https://www.silverlaw.com/blog/optionsellers-com-suffers-losses-after-natural-gas-declines/

### Regime breaks / product history
- Bank Nifty weeklies launch (2016): https://www.business-standard.com/article/pti-stories/nse-to-launch-weekly-options-contracts-on-bank-nifty-index-116050501487_1.html ; https://www.nseindia.com/static/products-services/equity-derivatives-bank-nifty-option-contract-specification-weekly
- Weekly discontinuations (last BANKNIFTY weekly expiry 2024-11-13, registry evidence; SEBI one-weekly-per-exchange rule effective 20-Nov-2024): https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives
- NIFTY weeklies launch (11 Feb 2019, reported-but-verify): https://www.finideas.com/introduction-of-weekly-options-in-nifty/
- SENSEX weeklies relaunch (May 2023) + expiry-day history: https://www.businesstoday.in/markets/stocks/story/nse-bse-fo-expiry-days-what-has-changed-for-stock-investors-history-more-480781-2025-06-18 ; https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/
- SEBI Oct-2024 framework summaries: https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing ; https://www.businesstoday.in/markets/stocks/story/sebi-floats-new-guidelines-for-high-risk-derivative-trading-check-details-fo-448379-2024-10-01 ; https://www.outlookbusiness.com/markets/sebis-new-fo-rules-kick-in-how-the-risk-tail-coverage-change-will-affect-traders ; https://www.marketfeed.com/read/en/understanding-sebis-new-rules-and-their-impact-on-indian-options-trading ; https://retailinterest.com/learn/sebi-fno-regulation
- Lot-size change: https://groww.in/blog/nse-increases-lot-sizes-for-index-derivatives ; https://bigul.co/blog/bse-sensex-and-nse-nifty-new-lot-size-changes-effective-from-20-nov ; NSE circular: https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf
- Expiry-weekday migrations: https://www.bajajbroking.in/blog/nse-revises-nifty-expiry-day-to-monday ; https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html ; https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/ ; https://www.arihantplus.com/blogs/market-updates/tuesday-is-the-new-thursday-nse-and-bse-expiry-shift-from-sep-2025/ ; https://www.angelone.in/news/market-updates/nifty-weekly-expiry-today-see-what-changed-on-sept-1-2025

### Practitioner backtests
- https://medium.com/@amit179.iitk2/optimized-920-straddle-strategy-to-get-more-than-80-return-annually-e977453dca80 (promotional; date UNVERIFIED)
- https://www.quintalmind.com/blog/time-based-straddle-strategy-nifty-options (methodology UNVERIFIED)
- https://blog.optionscrack.com/options-strategies/back-testing-9-20-straddle-nifty-on-expiry/ (figures not captured; UNVERIFIED)
- https://tradingqna.com/t/reports-of-the-death-of-920-short-straddle-are-greatly-exaggerated/179061 (anecdotal)

### Repo governance anchors
- DEC-FILL-WORST-CASE-ONLY-001 — MIGRATION.md #1825; `docs/adr/0023-worst-case-fills-only.md`
- DEC-EXEC-008 (BUY-only) — `brutex/options/executor.py:38-41`; DEC-EXEC-009 (no multi-leg) — `brutex/options/executor.py:13-14`
- DEC-ENGINE-SPOT-ONLY-001 — MIGRATION.md #1790
- DEC-ETC-REGIME-HISTORY-001 — MIGRATION.md #1524 (the 2024-10-01 cost boundary)
- DEC-TRADESIM-OPEN-SESSION-GUARD-001 — MIGRATION.md #1743 (the `session_gap` stale-price law)
- DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — MIGRATION.md #1749 (cascade verdict surfacing)
- DEC-GROWW-MARKET-HOURS-GUARD-001 — MIGRATION.md #1692 (pull-window constraint)
- ADR-0009 (probe-gate discipline: nothing UNVERIFIED is encoded as fact)

### Companion harness (this directory)
- `bhavcopy_tail_study.py` — the §5.1/§6 measurement harness (stdlib-only,
  fixture-verified; run per the §6 preamble)
- `README.md` — run commands, honest limits, output → placeholder map
- `tests_offline.py` — 29 offline tests (no network; not wired into ci.yml)
