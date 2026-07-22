# 04 — Gap analysis: option strategies as brute-force citizens — what is missing, what closes it, and what it honestly costs

**Status:** RESEARCH / DESIGN-READINESS ONLY — zero engine code; the sweep engine remains **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001) and options remain **REFERENCE_ONLY**; any activation of an options-strategy engine is a **future operator-gated DEC/ADR**.

**Date:** 2026-07-19

**Provenance:** multi-agent deep-research session, docs-only branch `claude/options-strategies-design`, draft PR #1725 (DEC-OPTION-SELLING-RESEARCH-001, #2210) carries this research pair; every repo citation is `file:line` at the branch base SHA (see [README.md](README.md)).

---

This chapter is the definitive **gap analysis**: the distance between what BruteX ships today and an
HONEST brute-force backtest of option strategies (writing/selling and every structure family), assembled
from three dedicated gap-analyst sections — **data gaps**, **simulation gaps**, and **fill realism** —
which in turn consumed the repo-evidence surveys (assembled into
[03-brutex-inventory-reuse-map.md](03-brutex-inventory-reuse-map.md)) and the market-microstructure
chapters (assembled into [02-indian-fno-microstructure.md](02-indian-fno-microstructure.md); margin and
sizing detail in [09-capital-margin-and-sizing.md](09-capital-margin-and-sizing.md)). Strategy-family
definitions live in [01-strategy-taxonomy.md](01-strategy-taxonomy.md); seller-side tail evidence in
[08-seller-blowups-and-tail-events.md](08-seller-blowups-and-tail-events.md) and
[10-seller-edge-and-execution-evidence.md](10-seller-edge-and-execution-evidence.md).

The chapter ends with the **consolidated honest-limits register** (§11) — the single register the
operator should read before commissioning any options-strategy DEC/ADR.

---

## 0. Scope, method, and how to read the severity keys

**Scope guard (binding).** The sweep engine is SPOT-ONLY today (DEC-ENGINE-SPOT-ONLY-001;
`brutex/universe.py:379-399` per the repo-engine survey) and options are lake/REFERENCE_ONLY. Both
shipped executors are **post-sweep TRANSLATION layers** over already-ranked spot winners — they never
rank, never enter the engine, never touch run identity (`brutex/futures/__init__.py:27-28` — "The
brute-force ENGINE is UNCHANGED … this package is a POST-sweep translation layer only"). Everything
below is design-readiness input for a FUTURE, operator-gated options-strategy engine (its own DEC/ADR).
Nothing here proposes activation or silent scope expansion.

**Verification discipline.** Every load-bearing `path:line` citation in this chapter was verified by a
research agent reading that file at that line in the `/home/user/bruteX` working tree (2026-07-19, with
the fill-realism section's reads on 2026-07-20); during assembly, the verbatim-quoted anchors were
additionally spot-checked (`brutex/engine/margin_history.py:10-23` · `brutex/execution/fill_mode.py:1-43`
· `brutex/engine/sweep_scope.py:28-37` · `brutex/options/executor.py:543-557` ·
`brutex/costs/calculator.py:237-251` — all confirmed). Claims inherited from a sibling research section
without a fresh re-read are labelled `[per repo-X]` / `[per micro-X]`; those section files' content is
assembled into chapters [02](02-indian-fno-microstructure.md) /
[03](03-brutex-inventory-reuse-map.md) / [09](09-capital-margin-and-sizing.md), and their citations were
verified by their own authoring agents. External-world claims carry a URL or an explicit UNVERIFIED tag;
every open item lands in the §11 register. Most Indian broker/exchange hosts **403 the research proxy**
(reproduced on `equitiesindia.com`, `quintalmind.com`, `zerodha.com`, and the NSE/SEBI/nseclearing
archive hosts), so several web items are **search-captured excerpts, not directly fetched pages** —
each is flagged as such where used and mirrored into §11.

**Strategy-family severity groups** (used throughout; family definitions in
[01-strategy-taxonomy.md](01-strategy-taxonomy.md)):

| Group | Contents |
|---|---|
| **G1** | Index same-expiry structures on NIFTY/BANKNIFTY/SENSEX — single-leg, verticals, straddles/strangles, condors/flies, ≤30 DTE incl. 0DTE. Sub-tiers: **G1a** plain ≤30 DTE hold-or-intraday · **G1b** 0DTE · **G1c** vol-regime-gated (IV-rank entries) |
| **G2** | Stock-option structures (covered-call-like, cash-secured-put-like, stock verticals) |
| **G3** | Cross-expiry (calendars/diagonals) + long-DTE (>30 DTE entry) families. Sub-tiers: **G3a** weekly-vs-weekly · **G3b** monthly calendars / 45-90 DTE entries · **G3c** quarterly/half-yearly |

**Severity vocabulary:** `BLOCK` = the family is not honestly backtestable without closing the gap ·
`DEGRADE` = backtestable with a disclosed, bounded honesty caveat · `ok` = not materially binding.

---

## 1. THE GAP MATRIX

Every gap in one table. `D-*` rows are DATA gaps, `S-*` rows are SIMULATION gaps, `F-*` rows are
FILL-REALISM design artifacts still to be built. Each row is developed in the chapter named in its ID
column; honest costs are stated per Golden Rule #10 — nothing here is free.

| ID (§) | Gap | Severity per family group | Closest in-repo (file:line) | Closure path | Honest cost |
|---|---|---|---|---|---|
| **D-A1** (§2) | No bid/ask/quote data anywhere — spread cost structurally invisible (lake = traded prints only) | G1 DEGRADE (assumption axis required) · G2 DEGRADE (worse — thinner books) · G3 DEGRADE | Executor guards refuse the worst *symptoms*: volume-0 `STALE_BAR` reject `brutex/options/executor.py:543-557`, tick floor `:563-564`; the 7-field candle tuple `brutex/data/models.py:98-104`; vendor response shape `docs/groww/api-reference/03-backtesting.md:203-236`; GDFL forward-capture design `docs/vendors/gdfl/INGEST_DESIGN.md` | Forward L1 capture only (GDFL class, purchase-gated) + a declared refusal-capable spread-assumption table until calibrated; the §2.5 spread-penalty axis is the only current instrument | The 2020-2026 window stays **spread-blind FOREVER** at retail-data ceilings (backfill impossible per the in-repo GDFL verdict `docs/vendors/gdfl/VERDICT.md:14-17`); every historical result carries a "gross-of-spread / assumed-spread" label |
| **D-A2** (§2.3(ii)) | No-trade minute = NO bar (absence ≠ zero); no mark-to-market series for dormant strikes | G1 DEGRADE · G2 BLOCK-adjacent (illiquid books) · G3 DEGRADE | Honest-`None` reader `brutex/options/lake_source.py:151-156`; `NO_DATA` reject `executor.py:532-541` | A DECLARED carry-forward/mark law with staleness tags (a modelling ASSUMPTION) + forward quote capture; structure-atomic reject for short-leg exits (§4) | Intra-trade MAE/MFE, stop triggers, and margin simulation are undefined during gaps; the censoring is counted, never removable — and it is biased AGAINST observing writer blow-ups |
| **D-A3** (§2.3(i)) | No depth/size data — freeze-quantity interplay unmodelable beyond a static cap | G1 DEGRADE (1-lot honest; multi-lot unverifiable) · G2 DEGRADE · G3 DEGRADE | Static per-underlying `(lot, tick, freeze)` triple `brutex/universe.py:891-943`; freeze plumbed as data `brutex/execution/types.py:72-100` | 1-lot / freeze-capped sizing envelope + the swept λ volume floor (§2.4); forward depth capture | Multi-lot capacity claims unverifiable from this data; λ is UNCALIBRATED by construction (§11 FILL-U6) |
| **D-B1** (§6) | No per-underlying constant-maturity IV history → no IV-rank/percentile entries | **BLOCK** for vol-regime-gated families (G1c and peers); ok otherwise | Per-bar IV + six Greeks stored `brutex/data/models.py:156-167`, written by `brutex/data/greeks_enrich.py:1-40`; ZERO aggregation exists (grep-verified `[per repo-greeks §f.2-f.3]`) | NEW derived IV/chain FEATURE LAKE (prebuild-style pass, own DEC) — §6 | Coverage-bounded (enrichment NULLs), tenor-bounded ≤ ~30d by D-C2, model-inherited (q=0, rate config, T-anchor bias); no vendor cross-check exists |
| **D-B2** (§6) | No IV surface / skew / term-structure features; chain-at-instant reads are O(whole FNO tree) | **BLOCK** for skew/term families; G3 **BLOCK** (term slope IS the entry signal) | Moneyness toolkit `brutex/data/moneyness.py:106-141,238-261`; registry chains `brutex/data/registry_store.py:114-127`; the O(all FNO dirs) enumeration `brutex/options/lake_source.py:189-237` | The same feature lake + a chain-at-instant index | The read-complexity class is disqualifying for a sweep hot path until the feature lake exists (Golden Rule #5) |
| **D-C1** (§7) | No chain-completeness census — a listed strike with zero bars is unattested absence | G1 DEGRADE→BLOCK for multi-leg selection · G2 **BLOCK** · G3 **BLOCK** | Registry ladder `registry_store.py:114-127` + coarse `candle_status` `:81-89`; ledger `groww-option` windows `[per repo-lake §e]`; stats manifest `[per repo-lake §h]`; the /db "listed N with data M" chip `brutex/dashboard/fno_cascade.py:228-240` | NEW census joining registry × ledger × stats-manifest → per-(underlying, expiry) strike×side matrix + a per-session serviceability predicate for the engine preflight | One O(registry rows + FNO dirs + manifest shards) pass + its own DEC; without it every multi-leg backtest carries an unquantified selection bias |
| **D-C2** (§7) | Stored option history default-bounded to `expiry − 30d` (the calendar/diagonal killer) | G1 ok (weeklies fit) · G2 DEGRADE · G3b/G3c **BLOCK historically** | `DEFAULT_OPTION_LOOKBACK_DAYS = 30` `brutex/adapters/groww/contract_bounds.py:104`; the strict window law + not-yet-open skip `:296-330`; explicit operator dates already override `:100-103` | A DEC widening the default (future pulls only) + a vendor expired-contract depth probe for the historical side (feasibility UNVERIFIED — §11 DATA-U-C1) | Widening ~doubles monthly per-contract bar volume (unmeasured — §11 DATA-U-C2) on the dominant-footprint options lake; if the vendor probe fails, monthly calendars are **historically unbacktestable at any cost** |
| **D-D1** (§9.1) | Liquidity screens limited to per-bar volume/OI/turnover proxies — no tradability guarantee | DEGRADE everywhere (disclosed proxy) | Volume/OI substrate `brutex/data/fno_candle_lake.py:138-141`; movers min-volume idiom `brutex/dashboard/movers_snapshot.py:436-454`; PCR primitives `brutex/data/pcr.py`; executor `STALE_BAR` + tick floor `executor.py:543-564` | Screen vocabulary as declared sweep axes with as-of cutoffs; TRUE screens need D-A data | Volume-UNIT semantics UNVERIFIED (§11 DATA-U-D1); absence bias in trailing averages; OI is trusted vendor state with no plausibility check |
| **D-D2** (§9.1) | No option-calibrated outlier/sanity screen — the spot ×4 bad-tick screen is declared INVALID for premia | DEGRADE everywhere (screens + fills run over spike-unscreened premium data) | The in-repo declaration `brutex/dashboard/movers_futures.py:902-925` ("the 4× threshold is INVALID for premia"); the spot screen `brutex/data/bad_tick.py:1-15,31-36`; grep: no bad-tick module under `brutex/options/` or `brutex/futures/` | NEW premium-calibrated sanity screen (open design work; must be operating-range-aware — §2.3(iv)) | Until built, only the universal finiteness/positivity/ordering drop protects premium reads |
| **D-E1** (§8) | Zero corporate-action layer (splits/bonus/ex-date/strike adjustment) — repo self-declared | G1 ok (divisor-maintained indices) · G2 **BLOCK** · G3 G2-only | The self-declaration + hard refusal `brutex/engine/sweep_scope.py:28-37`, hazard string `:62-70` (DEC-CORP-ACTION-GATE-001) | NEW adjusted-contract detector (ex-date × factor × old→new symbol map) + a refusal law generalized from spot candles to option chains; NSE adjustment-circular data source | Exact NSE adjustment rules UNVERIFIED-in-session (§11 DATA-U-E1); G2 stays refused until its data package — exactly as `sweep_scope.py` refuses it for spot today |
| **D-E2** (§8) | No F&O ban-list (MWPL) history | G1 ok (indices exempt) · G2 **BLOCK** (short entries during bans are fictional) | None (grep-verified `[per repo-quality §f]`) | NSE ban-list archive ingestion + an entry-refusal law | Archive availability UNVERIFIED (§11 DATA-U-E2) |
| **D-E3** (§8) | Event calendar = 4 in-repo flags only (no earnings/RBI/election) | G1 DEGRADE · G2 DEGRADE→BLOCK (earnings weeks) · G3 DEGRADE | `brutex/engine/session_lens.py:123-139` (the honest "Elections/weekday-Budget: NOT tagged" note); expiry regime table `brutex/options/expiry_calendar.py` via `session_lens.py:19,75` | External event-calendar source; per-underlying earnings dates are the G2-critical piece | No such surface via Groww found (§11 DATA-U-E3); event-blind sweeps mis-attribute tail losses to strategy rather than calendar |
| **D-F1** (§5.5) | American-early-exercise axis: **REFUTED for India** (no data needed) — but the repo's `STYLE_AMERICAN_APPROX` stamp needs reconciliation | ok everywhere (a design SIMPLIFICATION) | The stamp `brutex/data/greeks_enrich.py:31-34`; `greeks_provenance.py:89-94` `[per repo-greeks §a.2]` | Primary-circular verification (SEBI CIR/DNPD/6/2010), then a cheap doc/DEC reconcile that RAISES stored stock-option Greeks quality | Primary bytes unfetched (403 wall) — carried UNVERIFIED (§11 DATA-U-F1) |
| **D-F2** (§5.3) | No final-settlement-price (FSP) series — expiry settlement approximated by the 15:29 close | DEGRADE everywhere for hold-to-expiry (P&L biased by the spot-vs-FSP gap); G2 additionally physical-delivery mechanics | 1-minute spot bars only; *no `settlement_price` producer anywhere in `brutex/`* `[per micro-settle §4.3]` | A small per-(underlying, expiry-date) FSP table from NSE settlement files; until then an explicit FSP-approximation label + adverse-within-band pin resolution (§5.3) | Fetchability UNVERIFIED (NSE archives 403 in-session); spot-vs-FSP gap magnitude UNVERIFIED (§11 DATA-U-F2/SIM-G3/SIM-G7) |
| **S-a** (§3) | Margin simulation (SPAN) — zero sourceable history | **BLOCK** for undefined-risk short families (naked calls, short straddles/strangles); **NOT blocking for defined-risk families** — the §3.3 unlock | Refusal-only regime table `brutex/engine/margin_history.py:10-23`, all-refusal tables `:149-173`, typed error `:56-63`; injectable capital denominator `brutex/metrics/types.py:163-167`; `margin_roi` already a required artifact key `engine/top100_artifact.py:204-223` `[per repo-results §c.5]` | v1 = defined-risk capital = structural max loss (exact arithmetic, no SPAN); future SPAN-archive ingest DEC for the rest | SPAN archive depth to 2020 UNVERIFIED (§11 SIM-G2) — historical margin may be permanently refusal-only; cash-secured-put ROI on a max-loss basis understates practitioner leverage ~6-8× (figure UNVERIFIED — §11 SIM-G1) |
| **S-b** (§4) | Multi-leg atomic fills — no N-leg type exists anywhere | **BLOCK** for every structure family (a non-atomic condor is silently a different risk object) | Single-leg-only proof `brutex/options/executor.py:417-422`, docstring pin `:12-14` (DEC-EXEC-009); `TradeInput` single round-trip `brutex/costs/types.py:317-334`; per-leg fill law `brutex/costs/calculator.py:108-181`; live SELL-first arm `brutex/costs/futures_costs.py:472-475,543-551` | `OptionStructure` type + order-independent per-leg adverse fills + the all-or-nothing law + structure-level reject vocabulary (§4.3) | The order-free per-leg worst-case is strictly MORE pessimistic than real package execution (disclosed — §11 SIM-G12); partial fills are unobservable fiction and stay refused |
| **S-c** (§9.2) | P&L attribution (per-leg premium flows; Greeks decomposition) | Non-blocking (analytics, not correctness) — but option-selling research is diagnosis-heavy | All SIX Greeks stamped per bar `brutex/data/models.py:156-167`; honest-NULL ladder `greeks_enrich.py:979-1080`; REFERENCE-ONLY discipline template `results_store.py:214-227` `[per repo-results §e.6]` | Post-hoc results-depth pass; additive legs-sidecar + AGG columns; the residual term ALWAYS computed and displayed | Coverage fraction UNVERIFIED (§11 SIM-G8); the residual absorbs surface moves + the T-anchor bias — the decomposition must never be presented as exact |
| **S-d** (§5) | Expiry/settlement engine — the Track-2 §4.2 state machine is a fail-loud STUB | **BLOCK** for any hold-to-expiry policy (the DEFAULT terminal state of huge families); G2 physical delivery is a materially bigger build | The stub declaration `docs/track2/CALCULATOR_SPEC.md:275-288`; the Step-0 refusal `brutex/costs/calculator.py:237-251`; STT-on-exercise rows encoded but unreachable `brutex/costs/constants.py:82-89` | The §5 build under its own DEC: ITM auto-exercise + regime-keyed exercise-vs-sell economics, FSP proxy + adverse band, stock-delivery REFUSE, holiday-shift closure | ETC-on-exercise UNVERIFIED and it FLIPS the exercise-vs-sell verdict (§11 SIM-G6); v1 without this engine can only sweep force-close-before-expiry policies — a real scope limit |
| **S-e** (§3.5) | Cash-flow / capital ledger (premium in/out, reserved capital, marks) | **BLOCK** for credit structures' equity semantics; structurally SOLVED for defined-risk via reserved-max-loss | `EquityCurve.start_equity_paisa` seam `metrics/types.py:163-167`; equity sidecar `trade_store.py:154-160` `[per repo-results §c.2]`; drawdown gate needing a denominator `ranking/__init__.py:81-86` | Three ledger laws + one refusal (§3.5) | Margin-call simulation REFUSED until SPAN lands; premium-float interest ignored (disclosed; "second-order" is asserted, not measured — §11 SIM-G4) |
| **S-f** (§9.3) | Adjustment / dynamic management (rolling, re-centering, leg mutation) | Non-blocking for v1 (static structures); a cardinality + hot-loop cost driver | Per-bar premiums for every listed strike (the F&O candle lake); per-bar delta where enriched; moneyness re-centering machinery `brutex/data/moneyness.py`; the estimator parameterization `sweep_estimator.py:160-192` `[per repo-engine §d]` | v1 static-only; v2 = a SMALL enumerated axis on RESTRICTED spaces (finalist-depth re-simulation); every rule enters `canonical_params` | No options trials/s figure exists — the spot 80-150K trials/s budget does NOT transfer (§11 SIM-G5); axis multiplication triples-to-sextuples trial counts |
| **F-1** (§2.4) | Fill-honesty classes not yet first-class counted facts (Class A/B/C + regime flags) | Cross-cutting — the honesty spine of every family | Typed-counter discipline `executor.py:602-712`; the ETC honesty footer idiom `executor.py:892-912`, `cli.py:10999-11016` `[per repo-exec §b]` | The §2.4 predicates P1-P6 + Class A/B/C counters + `expiry_day`/`last_30min` regime flags + identity keys | Class A STILL carries an unquantified size residual — the design must SAY so per run, never claim "size-realistic" |
| **F-2** (§2.5) | Spread-penalty stress axis absent | Cross-cutting — the only quantitative probe of D-A1 available | The tick-floor law it reuses `calculator.py:163-174`; the default-omitted identity template (DEC-NIFTY-PRICE-SOURCE-AXIS-001, `docs/MIGRATION.md:7108`) | `spread_penalty_ticks = K ∈ {0,1,2,4,8}` default-omitted axis + the per-config breakeven `K*` read-out | A SENSITIVITY knob, never a cost model — permanently uncalibratable for the historical window (§2.5.3); labeling discipline is mandatory |

**How to read the matrix:** no single gap blocks *everything* — the load-bearing sequencing conclusion
(developed in §3.3 and §10) is that a v1 engine restricted to **same-expiry, defined-risk structures on
index underlyings** is honest end-to-end TODAY on the capital axis, with D-A spread-blindness and D-C1
census absence carried as declared, counted caveats; undefined-risk, cross-expiry, and stock families
each hit at least one hard BLOCK row above.

---
## 2. FILL REALISM — the honesty core

This chapter is the fill-realism deep dive: exactly what the stored data class can and cannot support,
and the design artifacts (classes, counters, stress axis) that turn the residuals into first-class
counted facts instead of silent optimism.

### 2.1 The data substrate (one paragraph)

The option lake stores **1-minute OHLCV + open-interest bars per contract**
(`bars/<exch>/FNO/<symbol>/1minute/…`, OI first-class non-null — `[per repo-lake §a]`,
`fno_candle_lake.py:133-141`), 1-minute-NATIVE by decision (DEC-OPTIONS-PREBUILD-RETIRE-001,
`[per repo-lake §f]`). There are **no quotes anywhere**: no bid, no ask, no depth, no order-book state —
for any instrument, any date (bid/ask/depth exist only on Groww's LIVE API —
`docs/groww/api-reference/08-live-data.md:52-53,98-99`, depth `:64`, totals `:85-86,123-124` — and the
live surface is removed from BruteX by DEC-LIVE-REMOVE-001, CLAUDE.md §1; no historical quote endpoint
is documented in the backtesting API). Two distinct "no-trade" shapes exist side by side: a minute with
**no bar at all** (the reader returns an honest `None` — `brutex/options/lake_source.py:151-156`,
`:245-246`) and a minute with a **bar whose `volume == 0`** (routine for options —
`brutex/data/today_completeness.py:71-72` "zero-volume option minutes are routine").

**Operating assumption, stated openly (§11 FILL-U1):** the vendor's 1-minute OHLC is **OHLC of TRADES**
(not of quotes/mid). Groww's API doc does not state the construction basis
(`docs/groww/api-reference/03-backtesting.md` — grep for construction semantics finds nothing), but the
repo's own fill law already reasons on this basis — a volume-0 bar is "a stale/carried print no real
order demonstrably filled at" (`brutex/futures/executor.py:35-36`) — and NSE 1-minute candles are
conventionally trade-derived. All of §2.2 is conditional on FILL-U1.

### 2.2 What 1-minute OHLC-of-trades CAN honestly support — the bounded-fill argument

#### 2.2.1 The chain of inference

For one contract's minute bar `B` with `B.volume > 0` (and FILL-U1 holding):

1. **At least one trade printed in `[B.low, B.high]`.** By construction of a trades-OHLC bar, every
   trade price recorded in that minute lies within `[low, high]`, and some trade printed at each of
   `open`, `high`, `low`, `close`. `B.volume` units demonstrably changed hands. (The stored bar
   additionally guarantees ordering: "OptionBar guarantees low ≤ open ≤ high, so the ordering validation
   cannot fire on a real lake bar" — `brutex/options/executor.py:583-585`; prices are 2dp-snapped
   Decimal rupees at the lake write boundary — `[per repo-lake §a]`, `parquet_writer.py:269-314`.)
2. **Therefore a MARKETABLE order present in that minute plausibly transacts within `[low, high]`** —
   because the tape proves counterparties transacted at within-range prices during that minute. This is
   a *plausibility* claim about a small order arriving near a traded instant, not a certainty about
   every instant of the minute (§2.3(iii) is the corner where it fails).
3. **Therefore the per-leg worst-case fill — BUY at `B.high`, SELL at `B.low`, each pushed one further
   tick adverse — is an honest achievable-price BOUND** for a small marketable order in that minute: no
   such fill is plausibly worse than one tick beyond the worst price at which real trades actually
   cleared.

#### 2.2.2 This is exactly the shipped spot/executor law, extended verbatim per leg

The repo already states and enforces this bound as the ONE fill law (DEC-FILL-WORST-CASE-ONLY-001,
MIGRATION.md #1825 at `docs/MIGRATION.md:7217`; operator directive: "real live should be completely
reproducible; backtests never more optimistic than reality" — `brutex/execution/fill_mode.py:3-4`):

- **The adverse-max law** (`fill_mode.py:10-17`): BUY fill on bar `B` = `max(B.high, trigger_level)`
  (else `B.high`); SELL fill = `min(B.low, trigger_level)` (else `B.low`); open-anchored events at the
  pure adverse extreme; `end_of_data` / `session_gap` at the last stored bar's adverse extreme.
- **The per-leg option/futures tick discipline on top** (`brutex/costs/calculator.py:123-181`): the
  anchor is "the option's own minute-bar ADVERSE EXTREME … a buy anchors on the bar HIGH, a sell on the
  bar LOW" (`calculator.py:128-134`); `buy_fill = high + tick`, `sell_fill = low − tick`, every fill
  floored at one tick (`calculator.py:163-174`). The buy fill "deliberately prices one tick OUTSIDE the
  printed range — the honest adverse floor, not a printable price" (`calculator.py:138-140`).
  MIGRATION.md #1825 (`docs/MIGRATION.md:7217`): "F&O legs keep the ±1-tick discipline ON TOP of the
  extreme (flat bar ≡ the retired open±tick fill, so worst-case can never flatter)".
- **The SELL-first arm already has arithmetic** — the calculator's short branch (opening sell at entry
  LOW − tick, cover at exit HIGH + tick, `calculator.py:167-172`) is production-unreachable under
  BUY-only DEC-EXEC-008, but the futures executor prices live short legs with the identical
  adverse-both rule (`[per repo-exec §d]`, `futures_costs.py:503-551`). **Option writing inherits a
  fully-formed per-leg SELL fill law; only its activation is new.**
- **Anchors are REQUIRED, not optional:** `TradeInput` refuses fail-loud without all four fill-bar
  extremes (MIGRATION.md #1825 "REQUIRED ANCHORS" clause, `docs/MIGRATION.md:7217`; fed at
  `executor.py:580-589`).

**The exact per-leg extension statement for an N-leg option structure** (the design law this chapter
proposes; no new invention needed):

> Each leg of a structure fills independently on its OWN contract's decided 1-minute bar at that bar's
> adverse extreme for the leg's direction — long legs (buys) at `high + tick`, short legs (sells) at
> `low − tick` — floored at one tick, with SL/TP-triggered legs taking the with-the-level clamp exactly
> as `fill_mode.py:10-13` states. A leg whose decided bar is absent or `volume == 0` is UNPRICEABLE
> (§2.3(ii)); structure-level consequences of one unpriceable leg are a NEW structure-layer law (atomic
> reject-if-any-leg is the conservative default — §2.4, §4), flagged by the executor survey
> (`[per repo-exec §e]`) as undesigned today.

#### 2.2.3 The precise epistemic status of the bound (what "honest bound" means here)

The bound is honest in ONE direction and under TWO named conditions:

- **Direction:** it can only be pessimistic-or-equal versus a real within-range fill — "never more
  optimistic than reality" (`fill_mode.py:3-4`) — for the priced dimension (price per unit).
- **Condition 1 — size:** the order is small relative to the minute's demonstrated traded size
  (§2.3(i) — the bound says nothing about YOUR lot count).
- **Condition 2 — trade-time marketability:** the order arrives at (or its resting price is crossed
  near) an instant when trading at within-range prices was actually possible. The minute's tape proves
  such instants existed; it does not prove they spanned the whole minute (§2.3(iii)).

Within those conditions, worst-traded-price ± 1 tick is the strongest bound this data class can
support, and it is exactly what the repo already ships per leg. Everything the data cannot support is
enumerated next — and each item maps to a mitigation that COUNTS or REFUSES rather than models (the
repo's established correct-or-absent idiom).

### 2.3 What 1-minute OHLC CANNOT support — the enumerated CANNOT list

#### 2.3(i) SIZE — the print proves *a* trade, not YOUR lot count

**The gap.** `B.volume ≥ 1` proves someone traded within `[low, high]`; it does not prove an ADDITIONAL
marketable order for `N × lot_size` units at one instant would have cleared within that range. Depth is
invisible: the lake stores no book levels at any granularity (§2.1), and even the total minute volume
is an aggregate across the whole minute, not available liquidity at your arrival instant. Your
simulated order was also **not in the book** — its own impact on the path is counterfactual and
unmodellable from OHLCV.

**Compounding constraint — freeze quantity.** The exchange rejects any single order above the
contract's freeze quantity, so a position above freeze must be sliced into `⌈qty/freeze⌉` child orders,
each walking the book separately — unobservable at 1-minute granularity (`[per micro-margin §4.2]`).
The repo already carries freeze as data (`brutex/execution/types.py:72-81` — `freeze_quantity` =
"Maximum quantity per order accepted by the exchange", FR-EXEC-005; positive-when-set validated at
`execution/types.py:96-100`) and the Track-2 draft states the v1 law: "The backtest simulator must
reject any combo trade requesting more than the freeze quantity per leg"
(`docs/track2/TRACK2_RISK_MGMT.md:59-62`). Two known caveats ride along: freeze values are TIME-VARYING
(a dated freeze table is needed, not the master's "now" snapshot) and the draft's NSE freeze rows are
mislabeled lots-vs-units (`TRACK2_RISK_MGMT.md:53-56`; both `[per micro-margin §4]`).

**The honest mitigation (proposed): a swept per-bar volume floor + the residual stated.**

- **Liquidity screen:** accept a leg's decided bar only when `bar_volume ≥ λ × leg_qty_units`
  (`leg_qty_units = n_lots × lot_size`), with the participation multiple **λ swept as a parameter**
  (e.g. λ ∈ {1, 4, 10, 20}). λ = 1 is the weakest defensible screen ("the minute demonstrably absorbed
  at least my size"); larger λ approximates a participation-rate cap. A per-bar **OI floor** is
  available as a second, cheaper screen (OI is a first-class column — `[per repo-lake §a]`) for
  chronic-illiquidity exclusion at selection time.
- **Precedent shape:** the repo already has a volume-floor parameter idiom — `normalize_min_volume` /
  `value_meets_volume_floor` (`brutex/dashboard/movers_snapshot.py:436`, `:457`; display-side,
  DEC-MOVERS-MIN-VOLUME-001) — and the per-leg gate slot is exactly where `STALE_BAR` already sits
  (`executor.py:543-557`).
- **Freeze gate:** v1 = reject any leg with `leg_qty_units > freeze_quantity`
  (`TRACK2_RISK_MGMT.md:59-62`), a new typed reject reason beside the existing five
  (`executor.py:155-184`); v2 (deterministic slicing with per-child worst-case fills + per-order
  brokerage) is future work per `[micro-margin §4.2]`.
- **The residual, stated plainly (cannot be screened away):** passing the floor does NOT bound market
  impact. λ × your size trading *across a minute* at *various prices* is not evidence that your
  marginal order at one instant clears inside `[low, high]`; λ itself is **UNCALIBRATED** (calibration
  needs depth/quote data that does not exist for the backtest window — §2.5.3). Class-A trades (§2.4)
  therefore still carry an unquantified size residual; the design must SAY so per run (the
  `executor.py:606-617` honesty-block idiom), never claim "size-realistic".

#### 2.3(ii) No-trade minutes — absence means UNKNOWN quote state, not no-market

**The two shapes and the shipped law.** An **absent bar** returns `None` → the trade is rejected
`NO_DATA`, "never faked" (`lake_source.py:151-156`; `executor.py:532-541`, `:345-347`). A **volume-0
bar** is rejected `STALE_BAR` UNCONDITIONALLY — "a stale/carried price with zero trades in the minute,
so a fill at its open (even with 1-tick slippage) is unachievable" (`executor.py:171-176`, gate at
`:543-557`), uniform on BOTH executors by DEC-FILL-WORST-CASE-ONLY-001 ruling 2 (MIGRATION.md
`docs/MIGRATION.md:7217`: "the `reject_stale_bars` knob is DELETED on BOTH translation executors …
CONSCIOUSLY SUPERSEDING the R15 'a volume-0 printed bar is REAL + TRADABLE' clause"; futures-side
statement at `brutex/futures/executor.py:31-37`).

**Why this is the honest handling.** No trade printing does NOT mean no market existed — NSE option
strikes frequently carry live (sometimes tight) quotes through minutes with zero trades; a marketable
order might well have filled. But the quote state of that minute is **unknowable from this lake**, and
pricing a fill against a carried/absent print would fabricate an execution at a price nobody
demonstrably transacted at. Uniform REJECT is the only choice that keeps the §2.2 bound's
anchoring-to-demonstrated-transactions property. It is ALSO why a per-position mark-to-market path has
holes: bars where a leg is missing leave the mark NULL (never interpolated — §3.5 law 3), so
intra-trade MAE/MFE, stop triggers, and margin-path simulation are undefined during gaps unless a
DECLARED carry-forward mark law (an assumption, staleness-tagged) is adopted (matrix row D-A2).

**The recorded cost — survivorship tilt — and its writer-specific sharpening.** The repo discloses the
cost: "the uniform reject introduces a survivorship tilt — genuinely tradable-but-untraded minutes
(expiry-afternoon volume collapse) are now dropped instead of filled, so the … translation UNDERCOUNTS
trades rather than over-pricing them; the conservative side" (`futures/executor.py:37-41`), and the
options result type carries the matching selection-bias honesty block: "exit-bar availability
correlates with market conditions — an option that … stopped printing bars into the forced 15:20 exit
is disproportionately one in adverse conditions. The accepted-trade sums … carry an unstated
survivorship tilt" (`executor.py:606-617`). **For option WRITING the asymmetry sharpens:** a
missing/volume-0 bar on a SHORT leg's exit (buy-back) minute leaves open risk that cannot be priced —
the structure cannot be "held a bit longer" (hold-to-expiry needs the UNIMPLEMENTED §4.2 expiry state
machine, a fail-loud stub — §5, `CALCULATOR_SPEC.md:275-288`). So for short legs the reject must be
STRUCTURE-ATOMIC and counted (§2.4, §4): dropping only the unpriceable leg would leave a simulated
naked position the data cannot close. And because untradeable exit minutes correlate with adverse
conditions, **the writer's censored sample is biased in the DANGEROUS direction — the trades most
likely to be censored are disproportionately the blow-ups.** Reject-and-COUNT keeps the censoring
visible; the count is the statistic a strategy verdict must be read against.

#### 2.3(iii) Spread wider than the bar's range — illiquid strikes

**The gap.** `[low, high]` is the range of TRADED prices. In an illiquid strike the quoted spread at
your arrival instant can sit partly or wholly outside that range (e.g. all the minute's trades hit the
bid side; the ask you would pay as a marketable buyer was above `high` the whole time). The traded
range under-represents the cost of *immediacy at an arbitrary instant*, so for such strikes even
§2.2's bound (buy `high + tick`) can be optimistic — **the one corner where "never more optimistic than
reality" is not guaranteed by this data class.**

**What the evidence says (all secondary; hosts 403 the proxy — search-captured; §11 FILL-U3/U4):** ATM
index strikes quote tight — "sometimes just 50 paise to ₹1" (quintalmind.com option-chain guide,
search-captured) or "₹150 bid – ₹151 ask" while "far OTM might be ₹2 bid – ₹3.5 ask" (niftywise.org,
search-captured — that far-OTM example is a ~55% spread-to-mid, versus a ₹0.05-tick trade range a quiet
minute might print); a common practitioner rule is "only trade strikes with bid-ask spread less than 5%
of the option price" (same source family, search-captured). No NSE-published per-strike option spread
dataset and no peer-reviewed measurement of NSE OPTION spreads was pinned this session: the academic
literature found measures Indian STOCK spreads
([Cogent Economics & Finance 2021](https://www.tandfonline.com/doi/full/10.1080/23322039.2021.1898735),
[EconStor PDF](https://www.econstor.eu/bitstream/10419/270055/1/10.1080_23322039.2021.1898735.pdf);
[NSE research paper 128](https://nsearchives.nseindia.com/content/research/comppaper128.pdf) —
Roll-estimator, cash equities). A search-assistant paraphrase attributed "index options within ~0.2% of
mid vs ~1.2% for single-stock options" to a 2024 NSE study — that claim could NOT be traced to a
fetchable primary page and is **UNVERIFIED** (§11 FILL-U4).

**Honest handling:** the spread-wider-than-range state is UNDETECTABLE per bar from OHLCV. Mitigations
are therefore structural, not per-bar: (1) confine Class A (§2.4) to the liquid-ATM-band × near-expiry
× index families where every secondary source agrees spreads are ticks-wide, and demote everything
else; (2) probe sensitivity with the spread-penalty axis (§2.5); (3) close the evidence gap only with
real quote data — which for the historical window is impossible (§2.5.3).

#### 2.3(iv) The last-30-minutes / expiry-day (0DTE) regime

**The gap.** On expiry day — precisely where Indian index option writing concentrates — quotes gap
violently in the final stretch: gamma of near-ATM strikes explodes while premium collapses toward
intrinsic, and market-maker quoting degrades. 1-minute trade-OHLC in this regime records stair-steps
and prints that a continuous-fill simulator mis-times; the per-minute range itself becomes enormous
relative to premium, so even the honest §2.2 bound turns brutally wide exactly when trades matter most.

**Evidence tiers:**

- *India-specific, secondary (search-captured; §11 FILL-U5):* "On expiry day, bid-ask spreads widen
  significantly, liquidity disappears for out-of-the-money options post 2 PM, and execution becomes
  difficult and costly"; "Post 3:00 PM on expiry … bid-ask spreads become ridiculous" (PL Capital
  Bank-Nifty expiry-day guide family, search-captured); a broker-blog claim that on 2026-03-30
  (VIX ≈ 28.9) "Bank Nifty ATM spreads ballooned to ₹80–150" (univest.in, search-captured —
  UNVERIFIED, single low-authority source).
- *Structural analogies from US 0DTE literature (mechanism, not numbers):* terminal-hour theta/gamma
  concentration and end-of-day dealer-hedge unwinds
  ([SpotGamma 0DTE explainer](https://support.spotgamma.com/hc/en-us/articles/15298463039251-0DTE-Options-Explained-What-They-Are-and-How-to-Trade-Them),
  [Option Alpha on 0DTE time decay](https://optionalpha.com/blog/0dte-options-time-decay)). US SPX
  evidence does not transfer quantitatively to NSE.
- *Repo-adjacent regulatory microstructure (verified `[per micro-margin §5.2]`):* option contracts
  trade under delta-based daily **operating ranges** that flex in steps after cooling-off periods — "a
  deep-OTM premium can legitimately multiply many-fold intraday — the operating range mechanism means
  some of those prints occur in stair-steps after flex events, which 1-minute bars record but a naive
  continuous-fill simulator would mis-time" (sourced there to the NSE price-band pages).

**Honest handling:** (1) the engine's forced 15:20 exit law (CLAUDE.md §7 "Track 1 forced exit 15:20")
lands short-leg buy-backs INSIDE this regime on expiry days — so per-trade **regime flags** are
required: `expiry_day` (leg's decided bar date == leg contract expiry) and `last_30min` (decided bar ≥
15:00 IST), countable per structure with zero new data; (2) optionally sweep an exclusion knob
(`no_entry_after` / `exclude_expiry_day`) as a strategy parameter rather than asserting a cost; (3) the
§2.5 spread-penalty stress is the only quantitative probe available; (4) **no claim of expiry-afternoon
fill realism may ship** — a design-doc sentence in the DEC-FILL-WORST-CASE-ONLY-001 honesty style is
mandatory.

### 2.4 Fill-honesty classes A/B/C — register per strategy config, count per trade

**Purpose:** make the §2.3 residuals FIRST-CLASS COUNTED FACTS of every future options-strategy run,
the way `OptionsRuleResult` already counts rejects with a fail-loud consistency law
(`executor.py:602-712`: typed per-reason counters, `__post_init__` refusing count inconsistencies at
`:675-700`, derived `n_rejected` at `:702-712`) and the way the ETC honesty footer WARNs once per sim
(`[per repo-exec §b]`, `executor.py:892-912`).

#### 2.4.1 Per-leg predicates (each evaluated at BOTH the leg's entry and exit decided bars)

| # | Predicate | Meaning | Existing machinery |
|---|---|---|---|
| P1 | bar present | the contract printed that minute | `lake_source.load_pair` → `None` ⇒ `NO_DATA` (`lake_source.py:151-156`; `executor.py:532-541`) |
| P2 | `volume > 0` | ≥1 real trade anchors the price bound | `STALE_BAR` gate (`executor.py:543-557`) |
| P3 | `volume ≥ λ × leg_qty_units` | the minute demonstrably absorbed ≥ λ× the leg's size | NEW gate; λ swept (§2.3(i)); floor idiom precedent `movers_snapshot.py:436,457` |
| P4 | liquid-band membership | \|`atm_offset`\| ≤ B strikes AND expiry tier ∈ {near weekly/monthly} on an index underlying | moneyness machinery verbatim (`[per repo-lake §d]`: `moneyness.py` `atm_offset`/`resolve_strike_step`); registry expiry tiers (`[per repo-lake §c/§g]`) |
| P5 | `leg_qty_units ≤ freeze_quantity` | single-order exchange-legal | freeze data plumbed, gate NEW (`execution/types.py:72-81`; law `TRACK2_RISK_MGMT.md:59-62`) |
| P6 | (optional) OI floor | chronic-liquidity screen | OI first-class per bar (`[per repo-lake §a]`) |

#### 2.4.2 The classes (per STRUCTURE = the all-legs trade unit)

| Class | Definition (over ALL legs, entry AND exit bars) | What may honestly be claimed |
|---|---|---|
| **A — bounded-fill** | P1 ∧ P2 ∧ P3 ∧ P4 ∧ P5 on every leg | The per-leg price bound (§2.2) plausibly holds AT the simulated size for liquid ATM-band near-expiry index legs; residuals remain the §2.3(i) impact counterfactual + the §2.3(iii) instant-spread corner — DISCLOSED, not zero |
| **B — bounded-price, unbounded-size** | P1 ∧ P2 ∧ P5 on every leg AND no lot/cost-regime refusal (i.e. NOT Class C), but ≥1 leg fails P3 and/or P4 | The price bound holds for a MARGINAL (small) order only; the run's size claim is NOT supported for this trade |
| **C — refused** | ≥1 leg fails P1 or P2 (or P5, or lot/cost-regime/tick_floor refusals) | Nothing — the structure is rejected ATOMICALLY (one unpriceable leg refuses the whole structure; for short legs this is mandatory per §2.3(ii)), counted with the failing leg + reason |

**Class-C atomicity note:** partial structures (price the legs that can be priced) are REJECTED as a
design option for v1 — a partially-filled simulated spread is a different position with different risk,
and for writers it can be a naked short the data cannot close. This is the structure-level law the
executor survey (`[per repo-exec §e]`) flags as absent today; atomic-reject is its conservative default
(the full structure law is §4).

#### 2.4.3 Proposed counters (the `OptionsRuleResult` extension shape)

Per strategy-config result row (the `executor.py:602-712` pattern — every counter typed,
`__post_init__`-reconciled, no catch-all bucket):

- `n_class_a`, `n_class_b` with the law `n_accepted == n_class_a + n_class_b`;
- `n_class_c` decomposed per failing predicate/reason: `n_c_no_data`, `n_c_stale_bar`,
  `n_c_over_freeze`, plus the existing `lot_size_unknown` / `cost_regime_unverified` / `tick_floor`
  reasons carried over (`executor.py:155-184`), each stamped with the FIRST failing leg's identity;
- per-trade regime flags folded to counters: `n_expiry_day`, `n_last_30min` (§2.3(iv));
- per-trade annotation `fill_honesty_class ∈ {A, B}` + `censored_leg_reason` on rejects in the
  results-depth artifacts — **annotation-only, never re-ranks** (the R13 annotation-never-reranks
  precedent, `[per repo-exec §f]`), rendered with a leaderboard honesty footer like the ETC-window WARN.

**Identity consequence (Golden Rule #4, CLAUDE.md §2 Rule 4):** λ, B, the P4 band/tier definition, and
the acceptance policy for Class B (`size_honesty ∈ {flag, refuse}` — flag is the observe-first default;
refuse is the stricter sweep variant) all SHAPE the accepted trade set, so whenever they gate or are
swept they MUST enter `canonical_trade_params` exactly like every other axis; the class COUNTERS
themselves are outputs and stay out of identity. The sweep can then answer, per strategy family: *what
fraction of this edge survives at Class A only?* — **which is the honest headline number for any
option-writing claim.**

### 2.5 The optional SPREAD-PENALTY sweep axis — an uncalibrated robustness knob, never a cost model

#### 2.5.1 Definition

`spread_penalty_ticks = K` (per run; integer ≥ 0; default **0**): every leg's fill moves K further
ticks beyond the existing worst-case anchor — `buy = high + (1 + K) × tick`,
`sell = max(low − (1 + K) × tick, tick)` — reusing the calculator's one-tick floor law verbatim
(`calculator.py:163-174`). Suggested lattice K ∈ {0, 1, 2, 4, 8}. A proportional variant (bps of
premium) is deliberately NOT the v1 shape: a proportional penalty starts to look like a claimed spread
MODEL, and its parameter would be exactly the number we cannot calibrate; absolute ticks are
transparently a sensitivity knob (and the multi-leg property is honest for free — total penalty scales
with leg count, as real spread cost does).

#### 2.5.2 Identity + read-out design (both from existing templates)

- **Default-omitted identity**, the DEC-NIFTY-PRICE-SOURCE-AXIS-001 template (`docs/MIGRATION.md:7108`:
  "Params are DEFAULT-OMITTED — the source keys are emitted only when non-default … so existing run_ids
  stay BYTE-IDENTICAL"): K = 0 emits no key; K > 0 emits `{"spread_penalty_ticks": K}` into
  `canonical_trade_params` beside the unconditional `{"fill_mode": "worst_case"}` (`fill_mode.py:32-35`).
- **Read-out:** per strategy config, the breakeven `K*` (largest K with net edge > 0) as a REPORTED
  robustness statistic — annotation/report layer only (R13), never a default rank key. A family that
  dies at K = 1 is spread-fragile whatever its K = 0 Sharpe says; that single number operationalizes
  §2.3(iii)/(iv) without asserting any market fact.

#### 2.5.3 Why "uncalibrated" is a hard property, not a temporary one — the refusal-precedent framing

- The repo's costing doctrine forbids fabricated market constants with NO runtime escape hatch: "no
  flag/kwarg may bypass the refusal — a fabricated statutory rate would fork every downstream number …
  The ONLY sanctioned override paths are DATA" (`brutex/costs/constants.py:126-136`); the margin table
  ships as refusal-only rather than invent a number (`[per repo-exec §d]`, `margin_history.py:10-23`).
  A "NIFTY ATM spread = X ticks" constant would be the same class of fabrication. **Therefore the axis
  must be labeled, in code and doc, a SENSITIVITY parameter — never "slippage/spread model", never a
  claimed cost.**
- Calibration would need historical QUOTE data. The repo's own vendor verdict shows the only realistic
  quote source is forward-captured 1-second L1 snapshots — each record carries
  `BuyPrice, BuyQty, SellPrice, SellQty` (`docs/vendors/gdfl/VERDICT.md:57-59`, sample schema `:72`) —
  and that "a 2022→now 1-second-class BACKFILL is NOT available … forward capture from subscription day
  is the only 1-second-class path" (`VERDICT.md:14-17`). **Consequence: the 2020→present backtest
  window is PERMANENTLY uncalibratable for spreads.** A future forward-capture archive could ground a
  calibrated, date-scoped spread table (its own DEC, DATA-shaped like a `RegimeRow`), and even then
  only for dates after capture start. Until then K is the only honest instrument, and it measures
  fragility, not cost.

### 2.6 Slippage philosophy vs the spot engine — what transfers, what differs, the residual

**Transfers cleanly.** The worst-case philosophy is already product-wide law — one enum, one
adverse-max semantics, identity-stamped, knob-refused (`fill_mode.py:1-43`, `:60-123`; MIGRATION.md
#1825 `docs/MIGRATION.md:7217`) — and the per-leg option arithmetic (anchor at the option bar's own
adverse extreme ± tick, floor, required anchors) is shipped and tested (`calculator.py:108-181`;
`executor.py:566-590`). An option-strategy engine extends the SAME law leg-by-leg (§2.2.2); no new fill
philosophy is needed, only structure-level composition rules (§2.4.2, §4) and the counters (§2.4.3).

**Difference 1 — the spot "fill" was an abstraction; the option fill is the first real one.** Track 1
spot indexes are not tradable ("Brokerage assumption (Track 1): NONE — spot is not tradable",
CLAUDE.md §7 table), so the spot engine's worst-case bounds signal arithmetic. Option legs are where a
fill first corresponds to an executable order — every §2.3 residual that was moot for the index becomes
live.

**Difference 2 — quotes are DISCRETE and premia are small.** The ₹0.05 tick is enormous relative to
cheap OTM premia — the repo already excludes entries at ≤ 5 ticks (₹0.25) via the tick floor
(`executor.py:559-564`) and floors every fill at one tick (`calculator.py:147-151` "nothing trades
below one tick"). Two ticks of round-trip discipline on a ₹2 premium is 5%; on the index's own scale
the equivalent is noise. Any percentage-based intuition imported from the spot engine breaks on the
premium scale, and gamma amplifies the bar RANGE (the adverse depth) in premium terms precisely for the
short-dated ATM contracts a writer trades. The interim decision-TF pessimism law (MIGRATION.md #1825
`docs/MIGRATION.md:7217`: "worst-case prices come from the DECISION-TF bar … strictly MORE pessimistic
than the operator's next-1-minute-bar target law, never optimistic") is tolerable on the index; applied
to an OPTION series at a >1-minute decision TF it would be crushing in premium % — **a future options
engine should decide legs on its chosen TF but PRICE legs on the option's 1-minute bar at the decided
instant** (the executors' current shape, and the 1-minute-native lake law — `[per repo-lake §f]` —
makes this the natural design), with the PR-B-style 1-minute execution-grid concept as the eventual
refinement.

**Difference 3 — quotes are SPARSE.** Index 1-minute series are dense by construction
(`today_completeness.py:73-74`: "indices always print"; the INDEX class demands the full minute count);
option series legitimately have zero-volume minutes ("routine", `today_completeness.py:71-72`) and
wholly absent minutes for dead strikes (`:75-77`). Consequences: (a) the reject machinery is the MAIN
path for options, not the corner case — strategy statistics (MIN_TRADES floors, DSR effective-N) must
be computed on the reject-censored sample with the censoring rate REPORTED per config (§2.4.3), because
two configs' edges are only comparable at comparable censoring; (b) the engine's spot-side data screens
do not extend automatically — the 4× bad-tick screen guards the SPOT engine read boundary
(`brutex/data/bad_tick.py:1-15`, `:31-36`) and no bad-tick/price-sanity screen exists in the
option/futures leg readers today (grep `bad_tick|BadTick` over `brutex/options/` and `brutex/futures/`:
no files — verified in the fill-realism session; `[per repo-lake G7]` notes the same for CE/PE baseline
reads). A 4×-per-minute move is REAL for options (gamma + operating-range flexing, §2.3(iv)), so the
spot threshold CANNOT be transplanted as-is — an option-appropriate sanity screen is open design work
(matrix row D-D2).

**The residual, stated once, plainly.** After the per-leg worst-case bound, the liquidity classes, and
the spread-penalty stress, three things remain UNKNOWABLE from 1-minute trade-OHLCV and must be
disclosed on every future options-strategy result surface: (1) instant-level quote cost where the
spread sat outside the traded range (§2.3(iii)); (2) the market impact of your own counterfactual order
at any size, floors notwithstanding (§2.3(i)); (3) fill feasibility in untradeable-but-quoted minutes
the uniform reject drops — with censoring correlated to adverse conditions, i.e. biased AGAINST
observing writer blow-ups (§2.3(ii)). None of these is quantifiable without quote/depth data that does
not exist for the backtest window (§2.5.3). **The honest product claim is therefore:
"worst-case-bounded against demonstrated trades, liquidity-classed, spread-stressed — not
size-realistic, not quote-complete."**

---
## 3. MARGIN & CAPITAL — and THE DEFINED-RISK UNLOCK

> **THE DEFINED-RISK UNLOCK (the load-bearing sequencing insight of this whole chapter):**
> for defined-risk structures, **capital = structural maximum loss**, computable EXACTLY from
> strikes + premiums + lot size — arithmetic over the leg table, **zero SPAN dependence**. That makes
> ROI, drawdown-percent, and capital-based ranking HONEST TODAY for all-long structures, debit and
> credit verticals, iron condors/flies, and long butterflies on index underlyings — while
> **undefined-risk ROI stays REFUSED** through the repo's shipped refusal mechanism until a
> SPAN-history DATA channel lands. Margin is therefore NOT the critical path of a v1 options engine;
> it is a later, additive DATA drop that unlocks MORE families.

### 3.1 What is missing

A dated, per-underlying, per-structure SPAN+exposure margin series for any date in the data window
(2020-01-01 →). The repo's own recon is definitive: *"Recon found NO sourceable SPAN history anywhere —
nothing in code, no NSE SPAN-archive integration"* — `brutex/engine/margin_history.py:10-23`
(assembly-spot-checked). Every shipped margin table is ONE refusal row from 2020-01-01;
`margin_for_date` raises the typed `UnverifiedMarginRegimeError` for every date, and the top-100
`margin_roi` surface renders `status="unverified"` (`margin_history.py:15-20`; consumer wiring
`[per repo-results §c.5]`: `engine/top100_artifact.py:204-223` — `margin_roi` is already a REQUIRED
artifact key with only the refusal mechanism behind it).

**Why it matters.** Undefined-risk short structures (naked short call/put, short straddle/strangle,
ratio spreads with net-short wings) cannot claim margin or ROI honestly: their real-world capital
consumption is SPAN+exposure (+ the 2024-11-20 expiry-day +2% ELM, + peak-margin snapshots —
`[per micro-margin §1.1 rows 1-10]`; assembled in [09-capital-margin-and-sizing.md](09-capital-margin-and-sizing.md)),
which is portfolio- and day-specific and reproducible ONLY from the day's SPAN risk-parameter file
(`[per micro-margin §1.3]`). Any hardcoded percentage is exactly the fabrication
`docs/track2/TRACK2_RISK_MGMT.md:109-121` forbids ("must be computed live from broker SPAN file at
backtest time" — cited in-code at `margin_history.py:13-15`). Without a margin denominator, ROI,
drawdown-percent, Kelly-style sizing and any cross-family ranking of short-premium strategies are
meaningless or dishonest — a short strangle's "return" without its margin base is a number with no
denominator.

### 3.2 Closest in-repo

| Piece | Where | What it gives |
|---|---|---|
| Refusal-only margin regime table (the mechanism) | `brutex/engine/margin_history.py:1-28` (MarginRegimeRow/refusal rows; `:56-63` typed error; `:149-173` all-refusal tables for the 3 indices `[per repo-exec §d]`) | The exact citizen shape for future margin DATA: rows land with citations, refusal is representable, fabrication is unrepresentable |
| The DATA-only override doctrine | `margin_history.py:20-23` ("NO runtime escape hatch; the ONLY sanctioned override paths are DATA") + the ETC precedent `brutex/costs/constants.py:126-136` (`[per repo-exec §b]`) | The governance template: a cited row or an ADR-stamped ASSUMED row, never a flag |
| Injectable capital denominator | `brutex/metrics/types.py:163-167` (`[per repo-results §a.1]` — `EquityCurve.start_equity_paisa` "Capital base used as the denominator") | The metric layer already parameterises WHAT denominates returns — an options adapter passes margin-true or max-loss capital without touching metric math |
| Capital-basis column sketch | `[per repo-results §e.2]` — additive nullable `ResultRow` columns (`capital_paisa`, `capital_basis ∈ {max_loss, span_margin, notional}`) under the optional-column template (`results_store.py:103-115`) | The storage landing zone |

### 3.3 THE DEFINED-RISK UNLOCK — developed fully

The max-loss arithmetic per family (per structure, paisa):

| Family | Max-loss arithmetic | Exact today? |
|---|---|---|
| Long call / long put / long straddle / long strangle | net debit paid = Σ(entry premium × lot) + entry-side costs | **YES** — exact |
| Debit vertical (bull call, bear put) | net debit | **YES** |
| Credit vertical (bear call, bull put) | (strike width × lot) − net credit | **YES** |
| Iron condor / iron butterfly | (max wing width × lot) − net credit | **YES** |
| Long butterfly (debit) | net debit | **YES** |
| Cash-secured short put | (strike × lot) − net credit — finite structural max loss (underlying → 0) | **YES arithmetically** — but see the honesty caveat below |
| Naked short call / short straddle / short strangle (call side) | UNBOUNDED — no structural max loss exists | **NO** — refuses until SPAN data lands |
| Calendar / diagonal (cross-expiry) | NOT exact: at near-leg expiry the far leg's residual value is model-dependent (and a deep-ITM European put can trade below spot intrinsic — the discounted-parity floor fact, `brutex/data/greeks_enrich.py:653-672` `[per repo-greeks §a.3]` — so "max loss = net debit" is not a theorem for put calendars) | **NO** — model-dependent; v1 excludes or discloses a conservative bound (§11 SIM-G11) |

Why this is load-bearing for sequencing:

1. **It inverts the dependency order.** The naive plan ("first source SPAN history, then build the
   options engine") makes a multi-month data-acquisition project the critical path. The defined-risk
   unlock means the engine, fills, costs, results and ranking layers can all ship and be validated on
   families whose capital basis is exact TODAY — SPAN becomes a later, additive DATA drop that unlocks
   MORE families, not a prerequisite.
2. **It is maximally honest, not a compromise.** Max loss is the textbook-conservative capital base:
   reserving it means the position can NEVER demand more capital than reserved (see §3.5 — this also
   structurally eliminates the margin-call modeling gap for these families). Real exchange margin on a
   defined-risk credit vertical is typically LESS than max loss only via broker/portfolio treatment
   (`[per micro-margin §1.3]`: exchange-mandated margin approaches max-loss only under broker-side
   treatment; the exact benefit is day-specific), so ROI on a max-loss basis UNDERSTATES the leveraged
   ROI a real trader sees — a conservative bias, the same side the engine's worst-case fill law already
   chooses (`brutex/engine/trade_sim.py:26-73`).
3. **The cash-secured short put caveat (honesty):** its max loss is finite and exact, so it CAN carry a
   max_loss capital basis — but that basis is ~6-8× the real SPAN margin (figure UNVERIFIED — §11
   SIM-G1), so its ROI reads dramatically lower than practitioner expectations. Recommended: admit it
   under `capital_basis="max_loss"` with the understatement disclosed on the surface, OR hold it with
   the naked families — a design-DEC choice, both honest.
4. **The `capital_basis` token makes the split auditable per row:** `max_loss` (exact, defined-risk),
   `span_margin` (future, DATA-gated), `notional` (disclosure-only). Rankability of capital-derived
   metrics stays withheld exactly as `deflated_sharpe`/VIX are today (`results_store.py:229-235`
   `[per repo-results §c.1]`) until a DEC makes them concrete.

**Which families are fully honest TODAY on capital:** all-long structures, debit verticals, credit
verticals, iron condors, iron butterflies, long butterflies — i.e. the majority of the practitioner
defined-risk selling repertoire — on index underlyings, within the honestly-priceable cost window
**[2024-10-01 → today]** (the ETC refusal-window constraint, `docs/track2/COSTS_VERIFIED.md:731`
`[per micro-settle §4.1]`).

### 3.4 The future SPAN channel (closure sketch for the rest)

- NSE Clearing publishes SPAN risk-parameter files intraday (`[per micro-margin §1.1 row 1]`,
  <https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span>). An **archive-ingest
  DEC** could add dated margin snapshots: either (i) per-day SPAN parameter files ingested into a new
  small lake dataset + a deterministic SPAN evaluator, or (ii) coarser dated per-lot margin rows
  landing directly as cited `MarginRegimeRow`s. `TRACK2_RISK_MGMT.md:118-121` already names both shapes
  and requires a `margin_method` audit stamp (`[per micro-margin §8.1]`).
- **Historical archive DEPTH is UNVERIFIED** — whether NSE/NSE Clearing serves downloadable SPAN
  parameter files back to 2020 is not established (§11 SIM-G2). If the archive is shallow, historical
  margin for the back-window may be permanently refusal-only (or ADR-stamped ASSUMED rows), and only
  forward capture accumulates real rows — the design doc must carry both branches.
- Regime-dating discipline transfers verbatim: peak-margin phases 2020-12→2021-09, +2% expiry ELM from
  2024-11-20, premium-upfront 2025-02-01, calendar-spread expiry-day benefit removal 2025-02(-01/-10,
  date pin open), FutEq limits 2025-10-01 — each one dated row in a DEC-ETC-REGIME-HISTORY-001-style
  table (`[per micro-margin §8.2]` supplies the dates + sources; see
  [09-capital-margin-and-sizing.md](09-capital-margin-and-sizing.md)).

### 3.5 The cash-flow / capital ledger (three laws + one refusal)

**Missing:** any notion of a cash ledger — premium received/paid at entry, reserved capital,
mark-to-market path, margin-call events. The metric layer consumes only a final per-trade `pnl_paisa`
and a caller-supplied equity curve (`metrics/types.py:100-108,187-189` `[per repo-results §a.1]`); the
trade sim is signal-only paisa price-moves (`trade_sim.py:13-24` `[per repo-engine §e]`).

**Why it matters:** a credit structure's economics are temporally inverted vs spot — cash IN at entry,
obligation until exit/expiry. Without a ledger, equity-curve semantics (what IS equity mid-trade?),
capital utilisation, and concurrent-structure capacity are undefined — and drawdown-percent (a hard
ranking gate: `max_drawdown_pct ≤ 30`, `ranking/__init__.py:81-86` `[per repo-results §b.1]`) has no
denominator.

**Closest in-repo:** the injectable `EquityCurve.start_equity_paisa` capital seam
(`metrics/types.py:163-167`); the equity sidecar (per-session equity close + intraday low —
`trade_store.py:154-160` `[per repo-results §c.2]`) as the storage shape for a marked path; the
refusal-only margin mechanism (§3.1) as the honest answer for everything margin-dependent.

**Closure sketch (three laws + one refusal):**

1. **Premium flows at entry, signed; float honestly ignored.** Credit structures book `+net_credit` at
   entry, debit structures `−net_debit`; exchange premium settlement is T+1 but the ledger books at the
   entry instant. **No interest is earned on premium float or reserved capital — stated as a disclosed
   simplification**, not modeled: at days-scale holds the float on a retail-scale book is a
   second-order term, and modeling it would require a dated short-rate accrual whose only honest source
   is the same operator-configured risk-free seam (a 6.5% DEFAULT that is itself an ASSUMPTION —
   `risk_free.py:64-67` `[per repo-greeks §c]`). Ignoring it UNDERSTATES credit-structure returns —
   again the conservative side. (Tag: the materiality claim "second-order" is asserted, not measured —
   §11 SIM-G4.)
2. **Reserved-capital ledger for defined-risk structures.** At entry, reserve
   `capital_paisa = structural max loss` (§3.3 arithmetic); release at exit/settle. Equity = cash +
   Σ open-structure marks − Σ reserves is NOT needed for v1 correctness — the simpler, honest v1:
   equity curve = realized cash ledger (per-trade settlement points + per-session marks from stored
   premiums where a marked path is wanted for the equity sidecar). Key structural benefit: **with
   capital = max loss reserved up front, a defined-risk structure can never demand more capital
   mid-trade than reserved — the missing margin-call model is NOT a correctness hole for these
   families, only a capital-efficiency understatement.**
3. **Mark-to-market path from stored premiums.** A per-bar structure mark = Σ signed leg closes exists
   in the lake for every bar all legs printed; bars where a leg is missing leave the mark NULL (never
   interpolated). This feeds the equity sidecar and MAE/MFE analogues; it is a REFERENCE path, not a
   fill claim.
4. **Margin-call simulation: REFUSED until SPAN data lands.** A margin-call/RMS-liquidation event
   requires an intraday margin series (peak-margin snapshots — 4 random/day — plus broker RMS triggers
   like Groww's shortfall > ₹5,000 auto-close, which is itself repo-draft-sourced and unre-verified:
   `[per micro-margin §6.2 + its register U11]`; §11 SIM-G10). None of that is modelable without the
   §3.4 margin channel; for undefined-risk families this compounds their §3.1 refusal (they cannot
   honestly enter v1 anyway); for defined-risk families it is structurally moot (law 2). **The
   margin-call mid-trade edge therefore lands in the §11 register, cross-referenced to the
   micro-margin penalty tiers (0.5%/1%/5%/day — `[per micro-margin §6]`) — not silently omitted.**

---

## 4. MULTI-LEG ATOMICITY

### 4.1 What is missing, and why it is a BLOCK for every structure family

Any type, fill law, or reject semantics for a structure of N option legs executed together. Today's
executor is strictly single-leg: one signal → one leg, direction-locked BULL→CE/BEAR→PE
(`brutex/options/executor.py:417-422`, docstring pin `:12-14` — "no straddles / spreads
(DEC-EXEC-009)"; `[per repo-exec §a]`); `TradeInput` is a single `(strike, option_type, expiry, entry,
exit)` round trip (`brutex/costs/types.py:317-334`); *"No type in `brutex/options/` or `brutex/costs/`
represents more than one option leg per trade"* (`[per repo-exec §a]`, proven by whole-file reads). The
results side is equally leg-blind: `ClosedTrade` is one netted P&L (`metrics/types.py:100-108`
`[per repo-results §a.1]`) and `TRADE_COLUMNS` carries one `entry_px/exit_px/direction` per trade
(`trade_store.py:132-151` `[per repo-results §e.1]`).

Every option strategy family beyond single-leg long CE/PE — spreads, straddles, strangles, condors,
butterflies, ratio structures, calendars — IS a multi-leg structure. Without atomic N-leg fills there
is no options-strategy engine at all; with NON-atomic fills (legs that can individually fail) the
simulated position **silently mutates into a different risk object** — an iron condor missing its
bought wing is a naked strangle, and the §3.3 max-loss capital basis becomes a lie.

### 4.2 Closest in-repo (everything per-leg exists; the structure layer is the whole gap)

| Piece | Where | What transfers |
|---|---|---|
| Per-leg worst-case fill law (long arm) | `brutex/costs/calculator.py:108-181` (`[per repo-exec §e]`): buy anchors entry HIGH + tick, sell anchors exit LOW − tick; four bar extremes REQUIRED at `TradeInput` construction (`costs/types.py:303-314`) | The per-leg price law, verbatim per leg |
| SELL-first fill arithmetic (live) | `brutex/costs/futures_costs.py:472-475,543-551` (`[per repo-exec §d]` — "the one place the repo already prices a SELL-FIRST leg on a live path"); the options short branch exists but is production-unreachable under DEC-EXEC-008 (`calculator.py:164-181`, `costs/types.py:205-208`) | The short-leg arm an option-writing structure needs; activation of the options short branch = its own DEC |
| Volume-0 reject, unconditional | `brutex/options/executor.py:543-557` — `STALE_BAR` on a zero-volume entry OR exit bar, checked before the tick floor | The per-leg liquidity gate, verbatim per leg |
| Missing-bar reject | `executor.py:532-541` (`[per repo-exec §a]`) — source returns `None` → counted `NO_DATA` | The per-leg absence gate to lift to structure level |
| Typed reject + counter discipline | `executor.py:155-184` (RejectReason enum), `:602-700` (count-consistent `OptionsRuleResult`), pinned reject order `:479-481` (`[per repo-exec §a]`) | The counting grammar a structure-level reject vocabulary extends |
| Pessimistic-tie precedent | `trade_sim.py:43-45` — same-bar SL+TP → "the STOP fills first, always" | The philosophical anchor: ambiguity resolves deterministically to the pessimistic side |
| Freeze-quantity per-leg gate (data plumbed, engine absent) | `freeze_quantity` fully plumbed as data, consumed by NO engine (`[per micro-margin §7 row 1]`: `brutex/execution/types.py:72-100` et al.); the draft v1 law "reject any combo trade requesting more than the freeze quantity per leg" (`TRACK2_RISK_MGMT.md:60-62`) | The per-leg size gate to wire into the structure fill |

### 4.3 The closure sketch — six structure-level laws

1. **The structure type.** A new `OptionStructure` = ordered tuple of
   `StructureLeg(contract_identity, side ∈ {long, short}, lots)` + a structure-family token, all legs
   sharing one decided entry bar instant and one decided exit instant. Per-leg pricing composes N
   existing single-leg breakdowns (the calculator is "pure composition" per leg —
   `options/executor.py:18-28` via `[per repo-results §e.5]`) + a structure roll-up enforcing
   `net == Σ leg nets` at construction (the `MaterializedFuturesTrade` `net == gross − charges`
   invariant precedent, `futures/executor.py:422-424` `[per repo-results §e.1]`).
2. **The N-leg atomic fill law (proposal).** All N legs fill in the SAME decided bar; each leg
   independently takes its OWN worst-case adverse extreme ± one tick (buy legs at that leg's bar
   HIGH + tick; sell legs at that leg's bar LOW − tick), with per-leg tick-floor semantics preserved.
3. **The leg-order worst-case law (proposal + honest limit).** 1-minute bars carry NO intra-bar
   sequencing, so any leg-ORDER model would be fabricated microstructure. The proposed law is therefore
   **order-INDEPENDENT by construction**: per-leg independent adverse extremes. Honest disclosure
   required in the design doc: this is strictly MORE pessimistic than any real simultaneous package
   execution (a real basket/IOC spread order nets some of the adverse extremes; charging each leg its
   own intra-bar worst can exceed the worst NET package price actually printable in that minute). That
   over-pessimism is the same conservative side the engine already chose for spot (decision-TF adverse
   extreme, "strictly MORE pessimistic … never optimistic" — `trade_sim.py:69-73`), and it is
   order-free and deterministic — no tie-break table needed. A tighter package-level bound is
   unobservable from per-leg OHLC without co-timed intrabar data and is rejected as fabrication
   (§11 SIM-G12).
4. **The partial-fill impossibility (honest limit, state it as a law).** Bar data has no book depth and
   no order-queue observability; a 1-minute bar either printed volume or it did not. A "partial fill"
   of a leg — or a structure entering with 3 of 4 legs — is unobservable fiction. **Law: all-or-nothing
   per structure per bar.** Corollary: the freeze-quantity gate rejects (not slices) any leg above
   `freeze_quantity` in v1 (`TRACK2_RISK_MGMT.md:60-62` law; per-child slicing with per-child
   worst-case fills is a disclosed v2 per `[micro-margin §4.2]`, and needs a DATED freeze table since
   NSE revises freeze values — the master stores only "now", `[per micro-margin §8.3]`; §11
   SIM-G13/FILL-U9).
5. **One leg's bar missing → whole-structure reject, counted.** If ANY leg lacks its entry bar or its
   exit bar (source `None`) or fails volume>0, the WHOLE structure is a counted reject under new
   structure-level reasons (e.g. `STRUCTURE_LEG_NO_DATA`, `STRUCTURE_LEG_STALE_BAR`, carrying which leg
   failed) — never a partial structure. This is the correct-or-absent doctrine applied at structure
   grain. Honesty note that must ride the counters: the single-leg liquidity-survivorship tilt
   (`executor.py:606-622` `[per repo-exec §a]`) COMPOUNDS with N — deep-OTM wings (exactly the legs
   defined-risk structures buy) are the least liquid, so structure-level rejects will correlate with
   the market states short-premium strategies find hardest; rule comparisons remain
   within-accepted-set comparisons only.
6. **Reject-order law at structure grain.** Extend the pinned per-leg order
   (`lot_size → cost_regime → no_data → stale_bar → tick_floor`, `executor.py:479-481`
   `[per repo-exec §a]`) to: structure-level date gates first (lot_size, cost_regime — pure date
   preconditions, zero I/O), then per-leg data gates evaluated over ALL legs with deterministic leg
   iteration order, first-failing-leg recorded. Order pinned in the DEC exactly as today's is.

---

## 5. THE EXPIRY / SETTLEMENT ENGINE GAP

### 5.1 The stub — what exists today is a refusal, not an engine

The whole expiry lifecycle is missing. The Track-2 §4.2 trade-outcome state machine is a **fail-loud
STUB**: *"Only `NORMAL_CLOSE` is implemented … that guard is a fail-loud REFUSAL, NOT an implementation
of this state machine … The three `EXPIRY_*` charge paths below are SPEC-ONLY forward-compat stubs …
Implementing them requires a new DEC; nothing here authorizes it"* —
`docs/track2/CALCULATOR_SPEC.md:275-288`; the code-side Step-0 refusal raises
`UnsupportedTradeOutcomeError` on anything but `NORMAL_CLOSE` — `brutex/costs/calculator.py:237-251`
(assembly-spot-checked). Also missing: any settlement-price data (*no `settlement_price` producer
anywhere in `brutex/`* — `[per micro-settle §4.3]`), any physical-delivery model, any
assignment/expiry exit token in `EXIT_REASONS` (`trade_sim.py:151-171` `[per repo-results §c.3]`).

**Why it matters.** Hold-to-expiry is not an exotic corner for option strategies — it is the DEFAULT
terminal state of huge families (expiry-day premium selling, "let the OTM side die" management,
defined-risk structures held to settle). An engine without an expiry engine can only sweep
force-close-before-expiry policies — legitimate for v1 but a real scope limit that must be stated, and
even then the expiry engine is needed for the exit-policy AXIS ("close at 15:29 vs hold to settle"),
which is genuinely sweepable and regime-dependent (§5.2).

### 5.2 ITM auto-exercise with STT-aware exercise-vs-sell economics

Facts (all web-verified in the micro-settle section; assembled into
[02-indian-fno-microstructure.md](02-indian-fno-microstructure.md)): every option is European (no early
exercise anywhere in the product set — the whole American axis is REFUTED, `[per micro-settle §1]`;
§5.5); since 2023-03-30 every ITM option auto-exercises with NO opt-out (DNE dead —
`[per micro-settle §2.1]`); index exercise settles in cash T+1. The tax asymmetry the sim must encode:
sale STT = seller-side on PREMIUM (0.10% in 2024-10-01→2026-03-31; 0.15% from 2026-04-01), exercise
STT = purchaser-side on INTRINSIC (0.125% through 2026-03-31; 0.15% after) — `[per micro-settle §3.1]`;
the exercise rates are ALREADY encoded in-repo as unreachable forward-compat rows
(`costs/constants.py:82-89` `[per repo-exec §b]`). Worked consequence (`[per micro-settle §3.3]`):
under the Oct-2024 regime, selling a deep-ITM at 15:29 saves ₹18.75 STT per ₹75,000 intrinsic vs
exercising — but adding trade-level charges (ETC/SEBI/IPFT/GST) and spread the comparison can FLIP,
and from 2026-04-01 (both rates 0.15%) the STT asymmetry vanishes and holding becomes the cheaper exit
for deep-ITM index options *subject to one UNVERIFIED input*: whether NSE levies transaction charges on
exercised (non-traded) contracts at all (§11 SIM-G6 — a real contract note or an NSE FATAX circular
closes it; the repo's illustrative branch assumes yes-on-intrinsic, `CALCULATOR_SPEC.md:356-358`,
marked illustrative-not-normative). **Design law: the exit-policy comparison is computed per-regime
from the dated cost tables — never a constant "always sell before expiry" rule** — and the two
spec-registered open questions (regime keying entry-vs-exit date for expiry events; the sell-open STT
era note — `CALCULATOR_SPEC.md:347-352,366-371` `[per micro-settle §4.3]`) must be DECIDED in the DEC,
not inherited as drift.

### 5.3 Index cash settlement at the official settle price — which we do NOT have as data

The official final settlement price is the expiry-day closing value of the underlying index, derived
from the **last ~30 minutes of constituent trading (a VWAP-like closing construct)** —
`[per micro-settle §2.5]` / `[per micro-expiry §6.2]` (NSE Clearing pages; exact formula
UNVERIFIED-exact, §11 SIM-G7) — NOT any 1-minute bar close. BruteX stores 1-minute bars; no
settlement-price field exists anywhere (`[per micro-settle §4.3]`). **Proxy + honest divergence law:**
v1 settles at a proxy derived from the stored 1m spot bars (e.g. the last stored close, or a
last-30-min VWAP-like fold — the choice is a DEC decision), with a DISCLOSED error band; a short strike
pinned inside the band resolves at the ADVERSE plausible settle within the band (the worst-case
discipline generalized — `[per micro-settle §6.5]` proposal). The band IS the pin-risk model: a
settle-vs-LTP divergence flips a pinned short between "keep full premium" and "pay intrinsic"
discontinuously (`[per micro-settle §6.3]`). v2 option: ingest the exchange's published daily
settlement prices as a new small per-(underlying, expiry-date) FSP lake dataset (its own DEC — a new
data surface with its own availability question, §11 SIM-G3/DATA-U-F2; storage trivial; NSE archives
403'd in-session). Until it lands, hold-to-expiry P&L must be labelled FSP-approximated (matrix row
D-F2).

### 5.4 Stock physical delivery — the honest v1 law: REFUSE

Stock options are physically settled for the entire data window (`[per micro-settle §1]`). Holding a
stock-option structure to expiry drags in: per-leg give/take delivery obligations with client-level
NETTING across legs (`[per micro-settle §2.2-2.3]`), the expiry-week delivery-margin ramp
10/25/45/70/100% of VaR+ELM from E-4 (`[per micro-settle §2.4]` — a capital step-function),
auction/short-delivery exposure for uncovered assigned calls, broker forced square-offs, and delivery
STT whose buyer/seller split is UNVERIFIED (§11 SIM-G9). This is a materially bigger build than the
index engine (`[per micro-settle §7.2]` says exactly this). **Proposed law: v1 scope = INDEX
underlyings only (NIFTY/BANKNIFTY/SENSEX — all cash-settled, matching the engine's existing spot
surface); stock-option structures either (i) carry a mandatory pre-expiry forced-exit policy (the
15:20 forced-exit precedent generalized — e.g. forced close no later than E-1, before the ramp bites
hardest) or (ii) REFUSE at structure construction with a typed reason.** Never a silent
hold-through-delivery model.

### 5.5 The European refutation (D-F1) — an entire design axis DELETED, one repo stamp to reconcile

The micro-settle section establishes with primary-source citations (SEBI circular CIR/DNPD/6/2010
"European Style Stock Options"; NSE Clearing's current settlement-mechanism page) that **every NSE/BSE
equity derivative option — index AND single-stock — is European-style for the entire 2020→today data
window: no early exercise, no early-assignment risk, anywhere in the product set**
(`[per micro-settle §1]`, its lines 12-34). This DELETES an entire data-requirement class (per-bar
assignment-hazard inputs, early-exercise-boundary pricing data) that US-market designs carry. Hedge
(per that section's own sourcing note — exchange/SEBI pages 403'd through the research proxy, claims
rest on search-summary + mirror citations): the primary circular bytes were not fetched in-session →
carried as §11 DATA-U-F1, with the verification path named.

**Repo reconciliation flag:** the Greeks enrichment stamps CASH-segment joins `STYLE_AMERICAN_APPROX`
("CASH → American single stock → FLAGGED APPROXIMATION", `brutex/data/greeks_enrich.py:31-34`;
`greeks_provenance.py:89-94` `[per repo-greeks §a.2]`). If the European finding holds against primary
bytes, that stamp is an over-caution (European Black-76 is exercise-style-EXACT for stock options too;
the residual stock-option pricing bias is the q=0 discrete-dividend term, not exercise style) — a cheap
doc/DEC reconcile that RAISES the usable quality of stored stock-option Greeks. No code change implied
here; recorded as a design input.

### 5.6 Prerequisites and wiring

- The expiry calendar's holiday shifts are NOT applied today (`brutex/options/expiry_calendar.py:26-29`
  `[per repo-exec §a + its UNVERIFIED #10]`) — a settlement engine keyed to real expiry DATES must
  close that first (an expiry that lands on a holiday settles on the shifted prior trading day;
  simulating settlement on a non-session date is wrong by a day).
- New terminal tokens (`expiry_settle`; `assignment` only if stock scope ever activates) append to
  `EXIT_REASONS` under its append-only law and walk the proven `session_gap` end-to-end chain: enum →
  TradeDetail → aggregate `exits_*` field → AGG column → top100 exits → display exit-mix
  (`[per repo-results §e.3]`). The index-vs-stock nuance repo-results flagged (its UNVERIFIED #1) is
  RESOLVED by micro-settle: European + cash settle ⇒ index structures need `expiry_settle` only;
  `assignment` is a stock-scope token.
- The expiry-day margin regime (the +2% short-index ELM from 2024-11-20; calendar-spread benefit
  removal on expiry day from Feb-2025) belongs to the §3.4 margin channel but must be CROSS-REFERENCED
  by the expiry engine: any "hold to settle" policy on a short structure crosses those dated add-ons
  (`[per micro-margin §2 rows 3, 5]`).
- **Session-shape drift:** from 2026-08-03 NSE F&O closes 15:40 with a 15:10-15:40 settlement VWAP
  (`[per micro-expiry §6.3]`) — expiry instants and session constants must become per-(segment,
  date-range) DATA in any options design (the repo's Greeks T-anchor hardcodes 15:30 today,
  `greeks_enrich.py:24-26`).

---
## 6. THE IV-SURFACE / FEATURE-LAKE GAP

### 6.1 What exists (the substrate is real)

Per-bar per-contract IV + all six Greeks + inputs + provenance ARE stored physical columns on enriched
1-minute CE/PE partitions — `brutex/data/models.py:156-167` (`GREEKS_OVERLAY_FIELDS`), written by the
production enrichment (`brutex/data/greeks_enrich.py:1-40` — exact-minute spot join, Black-76/Jäckel
kernel, honest-NULL statuses, q=0 + Act/365-to-15:30 + stamped rate). Bulk backfill exists
(`brutex greeks-backfill`, `[per repo-greeks §e]`).

### 6.2 What is missing

1. **IV-RANK / IV-PERCENTILE substrate.** These need a per-underlying **constant-maturity (e.g. 30d)
   ATM IV history series** over months-to-years. Nothing aggregates the stored per-(contract, minute)
   IVs across strikes, expiries, or time — repo-wide greps for
   `iv_surface|iv_rank|iv_percentile|term_structure|constant_maturity|iv_history|smile|skew` found ZERO
   matches (`[per repo-greeks §f.2-f.3]`, run 2026-07-19). The only stored vol SERIES is India VIX —
   NIFTY-only, REFERENCE_ONLY, vocabulary-banned (CLAUDE.md §2 Rule 3), and its Groww historical
   availability is itself an UNVERIFIED Phase-0 row (CLAUDE.md §6).
2. **Skew + term-structure features.** Both need **same-instant cross-strike / cross-expiry reads**
   (chain-at-instant). Today that costs O(all FNO contract dirs on the exchange) cold enumeration + one
   whole-contract tree read per leg touched, with no completeness guarantee attached (`[per repo-lake
   §h]` — verified NEW-NEEDED there; see [03-brutex-inventory-reuse-map.md](03-brutex-inventory-reuse-map.md)).
   That complexity class is disqualifying for a sweep hot path.

**Why it matters per family:** short-premium families (strangles/condors/iron flies) are classically
**gated on vol regime** ("sell when IV-rank high") — without the CM-IV history the entry signal does
not exist and the family degenerates to always-in (G1c BLOCK). Skew families (risk reversals, ratio
spreads, put-skew harvesting) need the smile features. Calendars/diagonals trade the **term-structure
slope** — it is their entry signal; they are double-blocked (see §7's D-C2 for their leg data).

### 6.3 Design constraint (binding for the designers)

These features MUST become a **DERIVED FEATURE LAKE** written by a prebuild-style pass — per-
(underlying, session-or-minute) rows of {CM-ATM-IV, IV-rank/percentile over a declared lookback, skew
metric (e.g. fixed ATM±N-offset IV differential), near-vs-next term slope, PCR} — so sweep evaluation
stays **O(1)/bar** exactly like the spot engine's derived TFs (Golden Rule #5). Computing chain
aggregates inside the sweep loop is structurally incompatible with the O(1) discipline given the §7
read complexity. The existing prebuild machinery (`materialize_lake_timeframes` pattern), the moneyness
toolkit (`derive_strike_step`/`atm_offset`, `brutex/data/moneyness.py:106-141,238-261` `[per repo-lake
§d]`) and the registry chains are the reusable building blocks; the feature lake itself is NEW (own
DEC, own schema, own freshness guard in the #1932 mold).

### 6.4 Honest limits of any IV feature lake built from this repo's data

- **Coverage-bounded:** IVs exist only where enrichment succeeded (rate + same-minute spot + premium
  above the discounted floor + T > 600 s); the actual OK-vs-NULL coverage fraction on the production
  lake is UNVERIFIED (§11 DATA-U-G1/SIM-G8). Every feature row must carry constituent-count/coverage
  provenance.
- **Tenor-bounded by D-C2:** with only ≤30-DTE contracts stored by default, a 30d constant-maturity
  point sits at the EDGE of available data (interpolation becomes extrapolation whenever the next
  expiry >30 DTE has no bars); CM tenors beyond ~30d are historically impossible.
- **Model-inherited:** self-solved IVs inherit q=0, the operator's rate config (default fixed 6.5% —
  an ASSUMPTION, `[per repo-greeks §c]`), the late-session T-anchor bias (up to ~−4.7% relative on the
  last tradable expiry-day minutes, `[per repo-greeks §f.7]`, `greeks_enrich.py:464-481`), and the
  AT_EXPIRY NULL inside the last 600 s. An IV-rank series spanning a rate-config change is subtly
  non-stationary; `rate_used` stamps make it auditable.
- **No vendor cross-check:** no historical vendor IV exists (live-only chain IV; `[per repo-greeks
  §f.11]`).

---

## 7. CHAIN COMPLETENESS + THE 30-DAY HISTORY WALL

### 7.1 D-C1 — no per-(underlying, expiry, instant) strike-coverage guarantee

Verified absent: no chain-completeness census or gate exists anywhere (`[per repo-quality §d]` —
grep-verified; inheritance row 10). The completeness machinery is deliberately weak for options so
illiquid strikes never false-red: the TODAY gate's OPTION class is the OI-null shape ONLY and
zero-today-bars ABSTAINS (`brutex/data/today_completeness.py:71-77` law + the verdict arm at
`:404-414` — a dead strike "legitimately prints nothing"); interior-gap/truncation demotion never runs
on the F&O spine and the partial-window watch excludes CE/PE (`[per repo-quality §d]` rows 7-8). Net
effect (the quality survey's own words): **per-strike presence is best-effort, not attested** — a
genuinely missing liquid-strike day produces no WARN, no demotion, no census row.

**Why it matters per family:** a vertical needs K and K±n present at the same minutes; a condor needs
four legs; delta-targeted selection needs the whole ladder WITH Greeks present to find the 0.20Δ
strike. A hole either (i) silently drops the trade (survivorship distortion of the sweep statistics —
the strikes that exist select themselves), or (ii) must refuse loudly. An honest engine needs option
(ii) — **which requires knowing the hole is a hole.**

**Closest in-repo (the building blocks of the join, none joined):**

- Expected set: `registry.duckdb` `contracts` rows — the vendor-listed strike ladder per
  (exch, underlying, expiry), `brutex/data/registry_store.py:114-127` (strike stored as float `DOUBLE`;
  PK on contract symbol); coarse per-contract `candle_status ∈ {none, partial, complete}` `:81-89`.
- Attempt/settle truth: the window ledger's per-contract `groww-option` windows
  (PLANNED/SUCCESS/EMPTY/FAILED, planned==fetched key law) `[per repo-lake §e]` — including the EMPTY
  records that disambiguate dead-strike vs never-pulled (`[per repo-lake G6]`).
- Stored-presence truth: the write-time stats manifest (per-file row counts + ts bounds)
  `[per repo-lake §h]`.
- Display-only partial: the /db cascade's per-expiry "listed N with data M" chip —
  `brutex/dashboard/fno_cascade.py:228-240` — counts, not a strike×side matrix, and not a gate.

**What a chain census would assert** (modeled on the uniqueness census / `brutex verify-acceptance`
pattern): per (underlying, expiry): a strike×side matrix of `{registry-listed?,
ledger-settled(SUCCESS/EMPTY/SKIPPED/FAILED)?, bars-present?, minutes-covered, greeks-OK fraction}`
with verdicts PASS / HOLEY(named strikes) / UNPULLED, plus a per-session "chain serviceable at instant
T" predicate for the engine preflight (the sweep-preflight freshness-gate precedent). Cost: one
O(registry rows + FNO dirs + manifest shards) pass, incremental via the stats manifest; small storage;
its own DEC. **Without it, any multi-leg family's backtest carries an unquantified selection bias.**

### 7.2 D-C2 — the `expiry − 30d` stored-history wall (the calendar/diagonal killer)

The default option pull window is STRICTLY `[expiry − 30d, expiry] ∩ envelope` with the not-yet-open
skip — `brutex/adapters/groww/contract_bounds.py:104` (`DEFAULT_OPTION_LOOKBACK_DAYS = 30`), window
law + skip at `:296-330` ("a far-future contract whose window has not OPENED yet … returns None — ZERO
vendor calls"). Therefore a contract has stored bars at decision instant T **iff DTE(T) ≤ ~30**.

Concurrency consequences per tier (derived from that law):

| Leg needed at entry instant T | DTE at T | Stored bars? |
|---|---|---|
| Near weekly (≤7 DTE) | ≤7 | yes |
| Any of the ~4 concurrent NIFTY weeklies (`[per micro-expiry §3]`) | ≤ ~28 | yes |
| Near monthly | ≤30 | yes (from ~30 DTE onward) |
| **Next monthly (calendar far leg)** | 30-60 | **NO** until it crosses 30 DTE |
| Quarterly / half-yearly | ≥60 | never (default law) |
| 45-90 DTE premium-selling entries | 45-90 | **NO** |

So: weekly-vs-weekly calendars are data-covered; **monthly calendars/diagonals and all >30-DTE-entry
families have NO far-leg data historically**. The metadata registry DOES enumerate the longer expiries
(the metadata pull stores all vendor-listed expiries/contracts, `[per repo-lake §c]`) — the candles are
what's absent.

**What would close it + honest cost:** (i) a DEC widening `DEFAULT_OPTION_LOOKBACK_DAYS` (mechanism
exists — explicit operator dates already override, `contract_bounds.py:100-103` region `[per repo-lake
§e]`) — but that covers **future pulls only**, at a real footprint cost (the options lake is the
dominant footprint class — the ~2-3× figure is the DEC-PIPELINE-AUTOPREBUILD-001 operator-accepted
estimate; widening 30d→60d roughly doubles per-contract bar volume for monthlies, unmeasured — §11
DATA-U-C2); (ii) **historical** far-leg coverage depends entirely on whether Groww serves 1-minute
candles for long-EXPIRED contracts retroactively — UNVERIFIED (§11 DATA-U-C1, inheriting repo-lake
U1/U5 + repo-quality U5). If the vendor probe fails, monthly-calendar families are **historically
unbacktestable at any cost**, and the design must say so.

---

## 8. CORPORATE ACTIONS, BAN LIST, EVENT CALENDAR

### 8.1 What is in-repo: essentially nothing — and the repo says so itself

`brutex/engine/sweep_scope.py:28-37` (assembly-spot-checked): "**BruteX has ZERO corporate-action
adjustment code (no split/bonus/adjustment-factor/ex-date layer anywhere in `brutex/`)** — CORRECT for
the divisor-maintained 3-index scope, CATASTROPHIC for a single-name sweep (a 1:1 bonus reads as a
fabricated −50% overnight cliff…)". The engine already hard-refuses out-of-scope single-name sweeps
citing exactly this hazard (DEC-CORP-ACTION-GATE-001; hazard string at `:62-70`). Greps for
ban-list/earnings tokens confirm no other substrate (`[per repo-quality §f]`; the data-gap agent's own
earnings grep 2026-07-19: zero hits). Dividends enter only as the Greeks `q = 0` stamped assumption
(`brutex/data/greeks_enrich.py:21-22`).

### 8.2 Why it matters per family

- **Stock options (G2) — BLOCKING, twice over:**
  1. **Corporate-action contract adjustment.** NSE adjusts F&O contracts on splits/bonus/large special
     dividends: strike prices and market lots are revised by the adjustment factor on the ex-date,
     mid-contract (mechanics = NSE's published adjustment policy; exact rules UNVERIFIED-in-session —
     §11 DATA-U-E1; the micro-expiry section independently establishes the adjacent fact that a listed
     contract's TERMS can mutate in place, e.g. lot mutations on named EODs and expiry re-dating of
     existing contracts, `[per micro-expiry §5.1, §2.1]`). Consequences in this lake: the adjusted
     contract appears under a NEW odd-strike symbol (the `fno_symbol` grammar already admits fractional
     strikes, `[per repo-lake §b]`), the pre-adjustment symbol's series dies mid-life, premium/OI
     series break, and the rebuildable registry cache holds only the CURRENT listing — no point-in-time
     record of what was listed when (`[per repo-lake G4]`). **An options engine MUST detect adjusted
     contracts (ex-date × factor × old→new symbol mapping) or REFUSE the underlying around the event**
     — the `sweep_scope` refusal law generalized from spot candles to option chains.
  2. **F&O ban periods (MWPL).** Stocks enter ban when open interest crosses the market-wide position
     limit; new positions are prohibited during the ban (external fact, UNVERIFIED-in-session — §11
     DATA-U-E2). No ban-list history exists in-repo; a backtest opening short strangles in a banned
     name trades a fiction. Needs an NSE ban-list archive as data + an entry-refusal law.
  3. **Dividends:** discrete-dividend forwards for stock options (the q=0 bias is unquantified for
     stocks — `[per repo-greeks U2]`) + dividend-driven early assignment DOES NOT apply (exercise is
     European, §5.5) but dividend ex-dates still move forwards and hence moneyness/Greeks.
- **Index families (G1/G3) — largely exempt:** indices are divisor-maintained through constituent
  corporate actions (`sweep_scope.py:31-32`) and have no ban list. Index REBALANCE events are already
  flagged (`EVENT_NIFTY_REBALANCE`, §8.3).

### 8.3 Event calendar

The complete in-repo event vocabulary is FOUR calendar-verifiable flags — `expiry`, `muhurat`,
`budget_weekend`, `nifty_rebalance` — with the honest in-code note "Elections/weekday-Budget: NOT
tagged anywhere in the repo — honestly omitted" (`brutex/engine/session_lens.py:123-139`; the expiry
flag derives from the static `brutex/options/expiry_calendar.py` regime table, `session_lens.py:19,75`).
There is no earnings calendar, no RBI-policy-day calendar, no election calendar. Option-selling P&L is
event-concentrated (expiry-day economics already differ structurally — the 2% expiry ELM etc.,
`[per micro-expiry §6.4]`), so event-blind sweeps mis-attribute tail losses to strategy rather than
calendar. Closing it: the four flags are the reusable pattern; earnings/RBI/election dates need an
external source (none documented via Groww — §11 DATA-U-E3); per-underlying earnings dates are the
G2-critical piece.

---

## 9. SECONDARY GAPS KEPT VISIBLE

### 9.1 Liquidity screens — what the data supports, honestly (D-D1/D-D2)

**Supported today (per-bar, point-in-time-safe):**

| Screen | Substrate | Evidence |
|---|---|---|
| Per-bar volume floor | `volume` column; the movers opt-in `?min_volume=` floor machinery (`normalize_min_volume` / `value_meets_volume_floor`) | `brutex/data/fno_candle_lake.py:138`; `brutex/dashboard/movers_snapshot.py:436-454` |
| OI floor / OI-change | `open_interest` first-class non-null for F&O; prior-session OI baselines | `fno_candle_lake.py:139-141`; movers prev-OI walk `[per repo-lake §f]` |
| Turnover proxy | Σ HLC3 × volume paisa (`session_traded_value_paisa`) | `[per repo-lake §g]` (movers value law) |
| Zero-volume tradability reject | executor `STALE_BAR`, unconditional | `brutex/options/executor.py:543-557` |
| Sub-tick premium reject | tick floor `entry ≤ 5 × tick` | `executor.py:563-564` |
| PCR (OI / volume) | read-time primitives, typed zero-denominator sentinels | `brutex/data/pcr.py` `[per repo-lake §g]` |

These compose into declared sweep-axis screens (min trailing-window volume, min OI, min turnover) with
as-of cutoffs already available (`filter_to_ist_minute` family).

**Honesty limits (must be stated wherever screens are claimed):**

1. **Volume-unit semantics UNVERIFIED:** the repo labels F&O bar volume "Traded contracts in the bar"
   (`fno_candle_lake.py:138`) but the vendor doc says only "volume"
   (`docs/groww/api-reference/03-backtesting.md:230`); whether Groww reports option volume in contracts
   or underlying units is unconfirmed (§11 DATA-U-D1). A lot-normalised liquidity screen is wrong by a
   factor of lot_size if the unit assumption is wrong.
2. **A volume screen is a tradability PROXY, not a fill guarantee** — 1-minute aggregate volume says
   nothing about the book at the strategy's instant (§2.3(i)).
3. **Absence bias:** missing minutes are absent rows, not zeros — trailing-average-volume screens over
   sparse strikes overstate liquidity unless the denominator counts session minutes, not present rows.
4. **OI is trusted vendor state** — no OI plausibility/monotonicity check exists (`[per repo-quality]`
   inheritance row 4), OI updates ride the exchange's dissemination cadence (intrabar staleness
   UNVERIFIED, §11 DATA-U-D2), and the TODAY gate exists precisely because OI arrives null while
   settling (`today_completeness.py:71-77`).
5. **The ×4 bad-tick screen is declared INVALID for premia** — the one outlier screen in the repo is
   index-circuit-breaker-calibrated and deliberately forced OFF for options ("option PREMIA
   legitimately move ≥4×/minute on leverage… the 4× threshold is INVALID for premia",
   `brutex/dashboard/movers_futures.py:902-925`). A premium-calibrated outlier screen does not exist
   and would be NEW design work (`[per repo-quality §b.3]`; matrix row D-D2) — until then, liquidity
   screens run over unscreened-for-spikes premium data (only the universal insane-arm
   finiteness/positivity/ordering drop exists on display readers).

### 9.2 P&L attribution (S-c) — analytics, not correctness; the substrate largely exists

**Missing:** per-leg premium flows — `ClosedTrade` nets everything (`metrics/types.py:100-108`);
`gross_profit/loss_paisa` are win/loss magnitudes over netted trades, not premium received vs paid
(`metrics/compute.py:190-191` `[per repo-results §e.4]`); nothing distinguishes "collected 100, gave
back 60" from "bought 40, sold 80". And no Greeks-based decomposition of P&L into
delta/gamma/theta/vega(/rho) contributions between bars exists anywhere.

**Why it matters:** option-selling research is diagnosis-heavy — a short-premium family that "won" must
show WHERE the edge came from (theta harvest vs short-gamma luck vs vega regime) or the ranking is
uninterpretable and overfit-prone. Premium-flow accounting (credit received, premium-kept %) is the
practitioner-standard lens for writers; without it the R33 dual-language display promise
(`[per repo-results §b.4]`) has nothing honest to say about a credit structure.

**Closest in-repo (the good news):**

| Piece | Where | Status |
|---|---|---|
| **All SIX Greeks stamped per 1-minute CE/PE bar** — iv, delta, gamma, theta, vega, rho + spot_at_bar + t_years_used + rate_used + provenance | `brutex/data/models.py:156-167` (`[per repo-greeks §a.1]`); production-wired in every F&O pull (`[per repo-greeks §a.4]`) | EXISTS — `[per repo-greeks §f.4]` explicitly refutes the "only iv/delta stamped" assumption: **none of the six is missing**; only model `price` + the `admissible` flag are computed-and-dropped (re-derivable — `greeks_enrich.py:1113-1115`) |
| Honest NULL vocabulary | statuses NO_RATE / AT_EXPIRY / NO_SPOT / BAD_INPUT / BELOW_INTRINSIC / NO_SOLUTION (`greeks_enrich.py:979-1080` ladder `[per repo-greeks §a.3]`) | EXISTS — attribution can gate on `STATUS_OK` per bar |
| Per-leg record landing zone | the proposed 4th sidecar `<stem>.legs.parquet` joined on `(run_id, trade_seq)` (`[per repo-results §e.1]`) + premium-flow AGG columns (`premium_collected_paisa`, `premium_kept_pct` — `[per repo-results §e.4]`) | DESIGNED (additive precedents named) |
| REFERENCE-ONLY annotation discipline | VIX trio: never rankable, never in run identity, typed non-corrupt conflict (`results_store.py:214-227,249-278` `[per repo-results §e.6]`) | EXISTS — the exact template for attribution columns |

**What attribution needs that is genuinely missing:** (1) **coverage** — attribution is only computable
on bars where the overlay solved (`STATUS_OK`); the ACTUAL production-lake coverage fraction is
UNVERIFIED (§11 SIM-G8); known structural holes: the last 600 s before expiry is `AT_EXPIRY` NULL by
design (0DTE attribution impossible in the final 10 minutes — `[per repo-greeks §f.7/f.9]`), and
deep-ITM zero-time-value strikes are `BELOW_INTRINSIC` NULL (no IV). (2) **No IV surface / no smile** —
a vega term priced off the leg's OWN IV change is computable; a "skew move" term is not (§6).
(3) **The residual term (the honesty core).** Between bars t and t+1, per leg:
`dP_explained = delta·dS + ½·gamma·dS² + theta·dt + vega·dIV (+ rho·dr)`, all inputs from the stored
overlay (dS from `spot_at_bar`, dIV from stored `iv`, dt from `t_years_used`). **Law:
`residual = dP_actual − dP_explained` is ALWAYS computed, stored, and displayed per trade** — it
absorbs discretization error (1-minute steps across gaps), smile/term-structure moves (no surface), the
stored T-anchor bias (bar-label-vs-close, up to ~−4.7% relative IV on late expiry-day minutes —
`greeks_enrich.py:464-481` `[per repo-greeks §f.7]`), and coverage holes (bars where a leg's Greeks are
NULL make that interval's explained terms NULL, pushing the whole interval into the residual). The
decomposition must never be presented as summing exactly; a trade whose residual dominates is flagged,
not silently normalised.

**Closure sketch:** attribution is a **results-depth analytics pass, not an engine-hot-path feature**
— computed post-hoc over finalists only (the `results_depth_pass` shape — `[per repo-results §c.2]`),
stored as additive AGG/legs-sidecar columns (`pnl_delta_paisa`, `pnl_gamma_paisa`, `pnl_theta_paisa`,
`pnl_vega_paisa`, `pnl_residual_paisa`, per-leg and rolled up), REFERENCE-ONLY under the VIX-trio
discipline: never rankable, never in run identity. Premium flows are simpler and fully exact: signed
`premium_net_paisa` at entry (credit +, debit −) on the legs sidecar + `premium_collected_paisa` /
`premium_kept_pct` aggregates (`[per repo-results §e.4]`'s sketch adopted as-is). Cost per finalist is
O(bars-in-trade × legs) overlay reads — bounded, post-hoc, disclosed.

### 9.3 Adjustment / dynamic management (S-f) — feasible on data, expensive on cardinality

**Missing:** any mid-trade structure mutation — rolling a tested side, delta-banded re-centering,
closing one leg early, adding a hedge leg. Today's translation layer inherits entry/exit timestamps
fixed by the spot sweep and "never re-evaluates levels" (`options/executor.py:1-28` `[per repo-exec
§e]`); the spot walker's only mid-trade decisions are SL/TP/forced-exit against one price series
(`trade_sim.py:26-73`).

**Why it is FEASIBLE (data):** re-quoting legs mid-trade needs only per-bar premiums for the candidate
contracts — which the lake stores for every listed strike at 1-minute grain (the F&O candle lake,
CLAUDE.md §6 `fno_candles` row `[per repo-engine §g.2]`), plus per-bar delta for delta-triggered rules
where the overlay solved (`[per repo-greeks §a.1]`), plus the dynamic ATM/moneyness machinery for
re-centering (`brutex/data/moneyness.py` — `derive_strike_step`/`atm_offset`). The moving-ATM-band
addressing question is already flagged in the operator's option-selling backlog
(`docs/track2/TRACK2_OPTION_SELLING_BACKLOG.md:1-51` `[per repo-exec §b]` — BACKLOG ONLY, NOT
AUTHORIZED). An adjustment is mechanically: close leg(s) at their worst-case adverse extremes, open
replacement leg(s) at theirs — the §4 fill law composes with zero new pricing machinery; each
adjustment adds one more per-leg cost breakdown (₹20/order brokerage multiplies per executed order —
`costs/types.py:440-442` `[per repo-results §e.5]`).

**Why it is EXPENSIVE (the cardinality note for the estimator):**

1. **Axis multiplication.** An adjustment rule is a new sweep axis (trigger × action × parameter),
   multiplying `other_axes_product` in `count_true_trials` (`sweep_estimator.py:160-192` `[per
   repo-engine §d]` — the estimator is parameterised and flows through mechanically; the BUDGET does
   not). Even a modest `{none, roll-tested-side@delta-0.30, close-all@2× credit}` × 2 parameters
   triples-to-sextuples the trial count of every structure family it attaches to.
2. **Per-trial cost inflation.** A static structure costs ~2N fills + terminal settlement. An adjusted
   structure costs (2N + 2·A·legs-moved) fills PLUS per-bar monitoring of the trigger (delta read or
   moneyness recompute per bar per open structure) — the hot loop becomes O(bars × open-structure
   state) with chain lookups, a shape the spot budget never modeled (per-leg premium lookups already
   add memory traffic the spot budget omits — `[per repo-engine UNVERIFIED #5]`; adjustments compound
   it). The spot engine's 80-150K trials/s envelope (`TRACK1_PERFORMANCE_BUDGET.md:135` `[per
   repo-engine §e]`) does NOT transfer; no options trials/s figure exists (§11 SIM-G5 — needs a
   prototype bench + budget re-derivation before any SLA is written).
3. **Sequencing recommendation.** v1: static structures only (entry → managed exit policy, no leg
   mutation). v2: adjustments as a SMALL enumerated axis applied to RESTRICTED spaces — finalist-depth
   re-simulation (the results-depth pass shape: post-hoc over top-K only, `[per repo-results §c.2]`)
   or a stage-2-style pruned space — never the full mask × structure × adjustment cross-product. This
   mirrors the repo's existing two-tier discipline (cheap full sweep → expensive depth passes on
   survivors).
4. **Identity note.** Each adjustment rule + parameters must enter `canonical_params` (the
   fill_mode/roll_schedule additive-keys precedent, `sweep_run.py:719-810` `[per repo-engine §c]`) —
   adjustment-bearing run_ids must never alias static-structure run_ids.

### 9.4 Where every gap's counters/refusals land (so nothing fails silent)

| Event | Landing mechanism (existing precedent) |
|---|---|
| Margin-unverified structure (naked short family pre-SPAN) | Typed reject + counter, the `COST_REGIME_UNVERIFIED` shape (`executor.py:509-530` `[per repo-exec §b]`); surfaced in the leaderboard footer idiom (`cli.py:10999-11016`) |
| Structure leg missing/stale | New `STRUCTURE_LEG_*` reasons under the RejectReason + count-consistency discipline (`executor.py:155-184,602-700`) |
| Settle-price proxy divergence | Disclosed error-band field per expiry-settled trade + adverse-within-band resolution (`[per micro-settle §6.5]`); one capped WARN per sim (the `options.sim.unverified_cost_window` idiom, `executor.py:892-912` `[per repo-exec §b]`) |
| Attribution residual dominance | REFERENCE-ONLY flag column (VIX-trio discipline, `results_store.py:214-227`) |
| Expiry/assignment terminal | Appended `EXIT_REASONS` token walking the `session_gap` chain (`[per repo-results §e.3]`) |
| Gate/door visibility | New doors extend `GATE_REASON_ORDER` (`brutex/ranking/__init__.py:257`) + `per_door_kills` (`run_manifest.py:91,188-193`) in lockstep `[per repo-results §d]` |
| Fill-honesty class + regime flags | The §2.4.3 counters + leaderboard honesty footer |

---

## 10. READINESS VERDICTS PER STRATEGY-FAMILY GROUP

Data-side verdicts (from the data-gap analysis; "READY\*" always means: subject to the cross-cutting
D-A spread-blindness (assumption axis) and D-C1 census absence (unattested holes)). Execution realism,
margin modeling, and engine design overlay these per §§2-5.

| Family group | Verdict | Binding gaps | Rationale |
|---|---|---|---|
| **G1a — index same-expiry, ≤30 DTE, hold-or-intraday** (short/long single-leg, verticals, straddles/strangles, condors/flies on NIFTY/BANKNIFTY/SENSEX weeklies + near monthlies) | **READY\* — best-served group** | D-A (spread assumption), D-C1 (census), D-F2 (FSP for hold-to-expiry), D-B1 if vol-regime-gated | 1-minute premium + OI + stored Greeks + registry chains + European cash settle all exist (§2.1/§6.1 substrate); the 30d window exactly covers this family's life |
| **G1b — 0DTE index** | READY\* with named caveats | D-A worst here (expiry-day spreads); Greeks NULL inside last 600 s + late-day IV T-anchor bias (`[per repo-greeks §f.7]`); D-F2 FSP gap is the P&L close | Bars exist (expiry day is inside every contract's window); expiry-day margin/cost regimes per §3.4/§5.6 |
| **G1c — vol-regime-gated index selling** (IV-rank entries) | **BLOCKED on D-B1** (feature lake) | D-B1/D-B2 | The entry signal has no substrate; everything else as G1a |
| **G2 — stock-option structures** (covered-call-like, cash-secured-put-like, stock verticals) | **BLOCKED** | D-E1 (corporate actions — repo-self-declared zero layer), D-E2 (ban list), §5.4 physical delivery, D-E3 (earnings), plus weakest liquidity + completeness (`[per repo-quality §d]`: stock-option holes never demote) and lot-history coverage limited to 3 indices (`[per micro-expiry §5.3]`; `[per repo-exec §c]` — "no stock lot data exists at all") | Multiple independent BLOCK-class gaps; needs its own data-package DEC before any design work is honest |
| **G3a — weekly-vs-weekly calendars/diagonals (index)** | READY\* (data present) but **BLOCKED on D-B2** for the term-slope signal | D-B2, D-C1 (two chains must both be complete) | Both legs ≤ ~28 DTE → inside the 30d window (§7.2 table) |
| **G3b — monthly calendars/diagonals; any >30-DTE entry (45-90 DTE selling)** | **BLOCKED historically (D-C2)** | D-C2 (far leg has no stored bars; historical closure depends on the UNVERIFIED vendor expired-contract depth), D-B1/D-B2 | The far leg's window has not opened at entry time under the expiry−30d law (`contract_bounds.py:296-330`); a widening DEC helps only forward |
| **G3c — quarterly/half-yearly (long-dated)** | **BLOCKED** (data never pulled; liquidity also thin — `[per micro-expiry §3]`) | D-C2 hard form | The default law never opens these windows |

**Cross-cutting readiness preconditions before ANY family goes to engine design** (the data-side short
list): (1) the chain-completeness census (D-C1); (2) the IV/chain feature lake (D-B) with per-row
coverage provenance; (3) the spread-assumption axis declared refusal-capable (D-A1 / §2.5); (4) the
mark-to-market law for no-bar minutes (D-A2 / §3.5 law 3); (5) the FSP table or an explicit
FSP-approximation label (D-F2 / §5.3); (6) for G2 additionally the corporate-action/ban/earnings data
package (D-E) — without which G2 should stay refused exactly as `sweep_scope.py` refuses it for spot
today.

**The simulation-side v1 posture (the sequencing conclusion, restated):** a v1 options-strategy engine
restricted to **SAME-EXPIRY, DEFINED-RISK structures on index underlyings** — force-close-before-expiry
until §5 lands, capital = structural max loss (§3.3), atomic Class-A/B/C-counted fills (§2.4, §4) — is
honest end-to-end TODAY on the capital/margin axis within the [2024-10-01 → today] cost window.
Undefined-risk (naked-short-call-bearing) families and cross-expiry structures stay behind the
SPAN-archive / settle-model / history-widening DECs, riding refusal mechanisms the repo already ships.

---
## 11. THE CONSOLIDATED HONEST-LIMITS REGISTER

This is THE register (Golden Rules #1/#10). Part A lists what can **never** be claimed with the current
data class — hard limits no engineering closes. Part B is the consolidated UNVERIFIED register — every
open item from the three gap analyses, merged and de-duplicated (source IDs preserved: `DATA-*` = the
data-gap section, `SIM-*` = the simulation-gap section, `FILL-*` = the fill-realism section). Any
future options-strategy DEC/ADR should quote this register rather than re-derive it.

### 11.1 Hard limits — what can NEVER be claimed with current data

1. **Historical spread cost is permanently unknowable.** The lake stores traded prints only (§2.1); the
   only realistic quote source is forward-captured 1-second L1 (`docs/vendors/gdfl/VERDICT.md:57-59`)
   and "a 2022→now 1-second-class BACKFILL is NOT available … forward capture from subscription day is
   the only 1-second-class path" (`VERDICT.md:14-17`). **The 2020→present backtest window is
   PERMANENTLY uncalibratable for spreads** — every historical result is "gross-of-spread /
   assumed-spread / spread-stressed", never "spread-realistic".
2. **No size realism at any size.** Market impact of a counterfactual order is unmodellable from OHLCV;
   the λ volume floor is a screen, not a bound, and λ is UNCALIBRATED by construction (§2.3(i),
   FILL-U6). No run may claim "size-realistic"; multi-lot capacity claims are unverifiable.
3. **The censored sample is biased against observing writer blow-ups.** Uniform reject of
   absent/volume-0 minutes undercounts trades, and untradeable exit minutes correlate with adverse
   conditions (§2.3(ii); `futures/executor.py:37-41`, `executor.py:606-617`). The censoring RATE is
   reportable; the censored trades' P&L is not recoverable.
4. **The spread-wider-than-range corner breaks the pessimism guarantee.** For illiquid strikes even the
   worst-case bound (buy `high + tick`) can be optimistic — the ONE corner where "never more optimistic
   than reality" is not guaranteed by this data class (§2.3(iii)). Structural mitigation only
   (Class-A confinement + K-stress), never per-bar detection.
5. **No tighter package-level fill bound exists.** Per-leg independent adverse extremes may exceed the
   worst printable NET package price in a minute (SIM-G12) — conservative, order-free, and the best
   available; a tighter bound needs co-timed intrabar data that does not exist.
6. **No expiry-afternoon fill-realism claim may ship** (§2.3(iv)) — the regime is flagged and
   stress-probed, never modeled.
7. **Undefined-risk margin/ROI stays REFUSED until SPAN data lands** — and if the NSE Clearing archive
   is shallow (SIM-G2), historical margin for the back-window may be **permanently** refusal-only.
   `margin_history.py:10-23` is the shipped mechanism; the data is entirely NEW.
8. **Monthly calendars / >30-DTE entries may be historically unbacktestable at any cost** — closure
   depends entirely on the UNVERIFIED vendor expired-contract depth (DATA-U-C1); a widening DEC helps
   only forward (§7.2).
9. **Hold-to-expiry P&L is FSP-approximated until an FSP table lands** (§5.3) — and until the §5 expiry
   engine exists at all, only force-close-before-expiry policies are sweepable (a real scope limit).
10. **Pre-2024-10-01 index-option costing is a refusal window** — the honestly-priceable cost window is
    [2024-10-01 → today] (`docs/track2/COSTS_VERIFIED.md:731` `[per micro-settle §4.1]`; ETC refusal
    rows `constants.py:427-478` `[per repo-exec §b]`) — a systematic date truncation of any backtest
    until the pre-boundary circulars are sourced.
11. **G2 (stock options) stays refused** until its corporate-action + ban-list + earnings + delivery +
    stock-lot data package exists (§8, §5.4; `[per repo-exec §c]` — no stock lot data exists at all).
12. **The honest product claim** for any future options-strategy result surface is exactly:
    **"worst-case-bounded against demonstrated trades, liquidity-classed, spread-stressed — not
    size-realistic, not quote-complete"** (§2.6). Anything stronger is a Golden-Rule-#10 violation.

### 11.2 The consolidated UNVERIFIED register

#### A. Vendor / lake data feasibility

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 1 | DATA-U-C1 | Whether Groww `get_expiries`/`get_contracts` + `get_historical_candles` serve long-EXPIRED contracts retroactively (historical far-leg backfill feasibility; also lost-lake re-pullability). Inherits repo-lake U1/U5 + repo-quality U5; the stock-option depth flavor is the backlog's own Phase-0 hard gate (`TRACK2_OPTION_SELLING_BACKLOG.md:38-44` `[per repo-exec UNVERIFIED #11]`) | UNVERIFIED | A live credentialed probe fetching a 2023 expired monthly's chain + candles; transcript committed per the repo probe convention |
| 2 | DATA-U-C2 | Footprint delta of widening `DEFAULT_OPTION_LOOKBACK_DAYS` 30→60/90 (the ~2-3× options-lake figure is the operator-accepted #1145 estimate, not a measurement) | UNVERIFIED | `brutex lake` census / `du` on the operator box before/after a widened trial pull |
| 3 | FILL-U1 | Groww 1-minute candles are OHLC **of trades** (not quote/mid-derived), and its volume-0 bars are carried prints rather than trade prints | Operating assumption; vendor doc silent (`docs/groww/api-reference/03-backtesting.md` — no construction semantics; the repo reasons on this basis at `futures/executor.py:35-36`) | Vendor documentation/confirmation of candle construction, or a credentialed probe comparing Groww 1m bars against a known trades tape for the same session |
| 4 | DATA-U-D1 | Groww option-candle volume UNIT (contracts vs underlying units) — the repo's own column doc says "Traded contracts" (`fno_candle_lake.py:138`) but the vendor doc says only "volume" (`03-backtesting.md:230`) | UNVERIFIED | Vendor doc clarification or a live cross-check of one bar's volume vs NSE bhavcopy contract volume |
| 5 | DATA-U-D2 | Intrabar OI staleness (exchange OI dissemination cadence vs the 1-minute bar stamp) | UNVERIFIED | NSE dissemination documentation or a live capture comparison |
| 6 | DATA-U-E3 | Existence of any earnings/event calendar surface via Groww APIs | UNVERIFIED (none documented in `docs/groww/api-reference/` per in-session reads of the backtesting + live docs; an exhaustive read of all annexures was not performed) | Full read of the vendor API reference + vendor confirmation |
| 7 | DATA-U-G2 | The claim "no production CE/PE read path outside those enumerated" (inherited caveat on all reader statements) | Scoped-negative per repo-quality U1 | Import-graph walk / AST census of `bars/**/FNO` consumers |

#### B. Fill realism / microstructure evidence

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 8 | DATA-U-A1 | Magnitude of real bid-ask spreads on NSE/BSE option books per moneyness/DTE (the size of the invisible cost) | UNVERIFIED — no data in-repo, none fetchable historically | Forward L1 capture (GDFL class) once purchased; or an academic/exchange microstructure study with URLs (not fetched in-session) |
| 9 | DATA-U-A2 | Availability/price of institutional NSE historical order-book/quote data for 2020-2026 | UNVERIFIED | NSE Data & Analytics product catalogue with working egress |
| 10 | FILL-U2 | Every §2.2 "plausibly fills within [low, high]" claim for marketable small orders | Inference from trades-OHLC semantics (conditional on FILL-U1 + the §2.2.3 conditions); not directly testable from this lake | Forward-captured L1 quotes (GDFL-class, `docs/vendors/gdfl/VERDICT.md:57-59`) joined to 1m bars: measure how often best-ask(instant) exceeds bar high (buy side) per liquidity class |
| 11 | FILL-U3 | Quantitative NSE option spread figures cited in §2.3(iii)/(iv): ATM "50 paise–₹1" / "₹150–151"; far OTM "₹2 bid–₹3.5 ask"; "spread < 5% of premium" rule; expiry-day "OTM liquidity disappears post 2 PM"; "post 3 PM spreads become ridiculous"; "2026-03-30 BankNifty ATM spreads ₹80–150" | **UNVERIFIED** — broker/education blog claims, search-captured only (hosts 403 the research proxy: reproduced on quintalmind.com, equitiesindia.com, zerodha.com); no measured public dataset found | Direct quote captures (forward L1 archive); or NSE market-quality publications if any cover option spreads; treat every number as anecdote until then |
| 12 | FILL-U4 | "2024 NSE study: index options execute within ~0.2% of mid vs ~1.2% for single-stock options" | **UNVERIFIED** — search-assistant paraphrase; no primary page traced | Locate the actual NSE Market Pulse / research publication and quote it, or drop the claim |
| 13 | FILL-U5 | India-specific last-30-min 0DTE quote-gap behavior (magnitudes) | Mechanism supported by regulatory microstructure (operating-range flexing — `[per micro-margin §5.2]`) + US analogies ([SpotGamma](https://support.spotgamma.com/hc/en-us/articles/15298463039251-0DTE-Options-Explained-What-They-Are-and-How-to-Trade-Them), [Option Alpha](https://optionalpha.com/blog/0dte-options-time-decay)); NSE magnitudes unmeasured | Forward-captured expiry-day L1 quote series on NIFTY/SENSEX weeklies |
| 14 | FILL-U6 | λ (volume-floor participation multiple) and B (ATM band width) values that make Class A "safe" | **UNCALIBRATED by construction** — §2.3(i)/§2.5.3; any default is a convention, not a fact | Depth/quote data; until then λ, B are swept parameters carried in run identity, never asserted as sufficient |
| 15 | FILL-U7 | Whether NSE option quote continuity in zero-trade minutes is good enough that uniform reject materially undercounts writer trades (the size of the §2.3(ii) censoring) | Unknowable from this lake; direction of bias known (conservative for entries; risk-hiding for censored exits — disclosed §2.3(ii)) | Join forward-captured quotes to historical-style signals: measure the fraction of rejected minutes with a live two-sided quote inside X ticks |
| 16 | SIM-G12 | The §4.3 claim that per-leg-independent adverse extremes can exceed the worst printable NET package price in a minute | Logically argued (extremes need not be co-timed), not empirically measured | A co-timed tick-data study — out of reach at 1m granularity; the claim only STRENGTHENS the conservative direction, so it is a disclosure item, not a correctness risk |
| 17 | FILL-U10 | SEBI loss-study context (93% of individual F&O traders lost money FY22-FY24, ₹1.8L-cr aggregate) used as motivation for cost/fill honesty | Press-release verified to exist via search ([SEBI PR, Sep-2024](https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html)); host not fetched directly (403 wall); figures not load-bearing for any design law here | Fetch the SEBI press release / study PDF directly |

#### C. Margin / capital / sizing

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 18 | SIM-G1 | "Cash-secured short put max-loss capital is ~6-8× real SPAN margin" (the leverage-understatement magnitude in §3.3) | UNVERIFIED — practitioner-order-of-magnitude, no source pinned | A real SPAN calculation (a day's parameter file) vs strike×lot for representative NIFTY puts |
| 19 | SIM-G2 | Historical DEPTH of the NSE/NSE Clearing SPAN risk-parameter file archive (downloadable back to 2020?) | UNVERIFIED — the intraday publication is verified (`[per micro-margin §1.1]`) but archive retention is not | Probe nseclearing.in archive endpoints (host 403s the research proxy — needs operator-side fetch); determines whether historical margin rows are ever sourceable vs forward-capture-only |
| 20 | SIM-G4 | "Premium float interest is second-order at days-scale holds" (§3.5 law 1) | ASSERTED, not measured | A worked bound: max premium × max holding days × short rate vs typical structure P&L; belongs in the design doc's disclosed-simplifications table |
| 21 | SIM-G10 | Groww RMS auto-close trigger (shortfall > ₹5,000) cited in §3.5 law 4 | Repo-draft-sourced, not re-verified live (inherits micro-margin U11) | Fetch groww.co risk-management policy |
| 22 | SIM-G13 + FILL-U9 | Freeze-quantity historical values (a DATED freeze table for the §4.3 v2 slicing law); data exists as a "now" snapshot only, plus the units-vs-lots drift in the draft (`TRACK2_RISK_MGMT.md:53-56`) | UNVERIFIED beyond the 2025 snapshots (inherits micro-margin U8/§8.3) | NSE FAOP circular series harvest / BSE circular archive |

#### D. Expiry / settlement

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 23 | DATA-U-F1 | The European-style refutation's primary bytes (SEBI CIR/DNPD/6/2010; NSE Clearing settlement-mechanism page) — carried from micro-settle, whose fetches were search-summary-based due to 403s | Strongly supported, primary-unfetched | Fetch the SEBI circular PDF + NSE Clearing page with working egress; then reconcile the repo's `STYLE_AMERICAN_APPROX` stamp (§5.5 flag) |
| 24 | DATA-U-F2 + SIM-G3 | NSE FSP/settlement-price file availability + format for building the per-(underlying, expiry) FSP table; spot-vs-FSP gap magnitudes (`[per micro-expiry §8.10]` carries the magnitude question) | UNVERIFIED | NSE Clearing settlement-price page/archive probe; compute the gap empirically from constituent data; if absent, the 1m-derived proxy + error band is permanent, not interim |
| 25 | SIM-G7 | Exact index final-settlement computation formula (last-30-min construct details) — sets the §5.3 error-band width | UNVERIFIED-exact (inherits micro-settle register #8) | NSE Clearing settlement-price page body + contract specifications |
| 26 | SIM-G6 | Whether NSE levies ETC/SEBI/IPFT on EXERCISED (non-traded) contracts — flips the §5.2 exercise-vs-sell verdict | UNVERIFIED (inherits micro-settle register #3; the repo's branch is marked illustrative, `CALCULATOR_SPEC.md:356-358`) | A real contract note for an exercised position, or an NSE FATAX circular |
| 27 | SIM-G9 | Delivery-STT buyer/seller split on stock physical settlement (only matters if stock scope ever activates) | UNVERIFIED (inherits micro-settle register #4) | CBDT 2018 clarification + NSE FATAX circular |
| 28 | SIM-G11 | Put-calendar max loss exceeding net debit (the below-intrinsic European-put case in the §3.3 family table) — direction is sound (repo-documented discounted-parity floor, `greeks_enrich.py:653-672` `[per repo-greeks §a.3]`), magnitude/bound unquantified | PARTIALLY VERIFIED (mechanism) / UNVERIFIED (bound) | A worked bound at prevailing rates, or exclusion of cross-expiry structures from the max-loss basis (the recommended v1 law) |

#### E. Corporate actions / stock-option (G2) data package

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 29 | DATA-U-E1 | Exact NSE corporate-action F&O adjustment rules (which actions adjust strike/lot, factor computation, special-dividend threshold, symbol re-naming convention) | UNVERIFIED-in-session (mechanics widely published by NSE; not fetched here) | NSE's "Adjustment of Corporate Actions" policy page / circulars fetched with working egress |
| 30 | DATA-U-E2 | NSE MWPL F&O ban-list mechanics + historical ban-period archive availability | UNVERIFIED-in-session | NSE ban-list daily files (fd_sec_ban list) archive; SEBI MWPL framework circular |

#### F. Engine / quality / meta

| # | Source ID(s) | Claim / open question | Status | What would verify / close it |
|---|---|---|---|---|
| 31 | DATA-U-G1 + SIM-G8 | Production-lake Greeks overlay coverage fraction (STATUS_OK vs NULL vs unstamped) — bounds the IV feature lake's historical quality (§6.4) AND §9.2 attribution completeness | UNVERIFIED (inherits repo-greeks U1) | `brutex greeks-backfill --dry-run` scan census on the operator lake |
| 32 | SIM-G5 | Options-structure trials/sec on the §8 hardware envelope (feasibility of any sweep-scale SLA) | UNVERIFIED — no figure exists; the spot 80-150K trials/s budget does not transfer (`[per repo-engine UNVERIFIED #5]`) | Prototype bench over real option 1m data + a TRACK1_PERFORMANCE_BUDGET-style re-derivation |
| 33 | FILL-U8 | The same-bar tie SL-first clause + Rust kernel tie handling (referenced via MIGRATION.md #1825 text at `docs/MIGRATION.md:7217`) | Ledger + `fill_mode.py` docstring verified; the implementing walker/kernels not read in the gap sessions (the same residual `[per repo-exec UNVERIFIED #12]` records) | Read `brutex/engine/trade_sim*` + `rust/fno-math` tie-handling lines |
| 34 | SIM-G14 | Upstream-section citations not re-read by the gap analysts (all `[per repo-X]`/`[per micro-X]` attributions in this chapter) | Verified by the named sibling agents in their sessions, per their own re-verification disciplines; at ASSEMBLY the five verbatim-quoted anchors were additionally spot-checked and confirmed (`margin_history.py:10-23` · `fill_mode.py:1-43` · `sweep_scope.py:28-37` · `executor.py:543-557` · `calculator.py:237-251`) | Further spot-checks against the branch base SHA if any additional line is quoted verbatim downstream |

---

## Appendix — web sources cited in §2 (fill realism)

All search-mediated; direct fetches 403'd where noted (the research-proxy wall reproduced on the
broker/exchange hosts):

- SEBI press release, Sep-2024 (93% F&O loss study): <https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html>
- Cogent Economics & Finance (2021), "A comprehensive study on bid-ask spread and its determinants in India" (NSE **stocks**, not options): <https://www.tandfonline.com/doi/full/10.1080/23322039.2021.1898735> · PDF mirror <https://www.econstor.eu/bitstream/10419/270055/1/10.1080_23322039.2021.1898735.pdf>
- NSE research paper 128, "Understanding the Microstructure of Indian Markets" (Roll-estimator spreads, cash equities): <https://nsearchives.nseindia.com/content/research/comppaper128.pdf>
- Broker/education blogs on NIFTY/BankNifty option spreads (search-captured; fetches 403): <https://www.quintalmind.com/learn/option-chain-analysis> · <https://niftywise.org/blog/itm-atm-and-otm-in-nifty-options-what-they-mean-and-why-they-matter> · <https://equitiesindia.com/glossary/banknifty-vs-nifty-options-liquidity> · <https://www.plindia.com/blogs/bank-nifty-monthly-expiry-day-trading-options-strategy/> · <https://univest.in/blogs/bank-nifty-option-tips> · <https://navia.co.in/blog/what-is-bid-ask-spread/>
- Zerodha Varsity impact-cost/liquidity chapters (403'd; search-captured): <https://zerodha.com/varsity/chapter/impact-cost-and-how-it-can-ruin-a-trade/> · <https://zerodha.com/varsity/chapter/nifty-futures/>
- US 0DTE structural analogies (mechanism only, non-transferable numbers): <https://support.spotgamma.com/hc/en-us/articles/15298463039251-0DTE-Options-Explained-What-They-Are-and-How-to-Trade-Them> · <https://optionalpha.com/blog/0dte-options-time-decay> · <https://marketxls.com/blog/0dte-theta-decay-what-every-trader-should-know>
- NSE Clearing SPAN risk-parameter publication (cited in §3.4; host 403s the proxy): <https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span>

---

*Assembled 2026-07-20 from the three gap-analyst research sections (data / simulation / fill-realism)
of the options-strategies design-readiness effort. Repo citations are `path:line` into
`/home/user/bruteX` as read on 2026-07-19/20 by the section agents (assembly spot-checks per §11.2
row 34); sibling-section attributions are marked `[per …]` and their content is assembled into
chapters [02](02-indian-fno-microstructure.md), [03](03-brutex-inventory-reuse-map.md) and
[09](09-capital-margin-and-sizing.md). No existing repo file was modified; this chapter is the only
file this assembly created.*
