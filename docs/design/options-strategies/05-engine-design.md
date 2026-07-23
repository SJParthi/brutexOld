# 05 — Engine design: the N-leg selling-structure walker & sweep grammar

> **Status: DESIGN ONLY — nothing in this chapter is implemented.** The capability designed
> here ships **DEFAULT-OFF / INERT** under the reserved **`DEC-OPTSELL-STRUCTURE-EXECUTOR-001`**
> (MIGRATION.md #2211, its own separate PR); options remain **REFERENCE_ONLY** lake data under
> **`DEC-ENGINE-SPOT-ONLY-001`** (MIGRATION.md #1790; CLAUDE.md §2 Rule 3); the spot engine and
> the 60-condition vocabulary are **byte-untouched** by this design. **Any move of options into
> the engine's brute-force sweep-scope, and any change to the 60-condition vocabulary, is its
> OWN future operator-gated scope-expansion DEC** (Golden Rule #3). Nothing in this document
> authorizes activation, a scope change, or a vocabulary change.
>
> **Date:** 2026-07-22
> **Provenance:** multi-agent design panel, docs-only branch `claude/options-strategies-design`,
> part of **`DEC-OPTION-SELLING-RESEARCH-001`** (MIGRATION.md #2210). This chapter is the
> panel's risk-first / honesty-first engine design in full: the same grammar / mask composition /
> multi-leg walker / run identity / cardinality staging / gate-reuse / default-off activation
> substrate as the reuse-maximal frame (chapter [03](03-brutex-inventory-reuse-map.md)), with
> every seam ruled from the angle of *"what can this data honestly claim — and refuse everything
> else FIRST."*
>
> **Evidence discipline.** Every repo claim is `path:line`, verified by the design panel reading
> that file at that line in this checkout (the base-SHA integrity check is recorded in chapter
> [03](03-brutex-inventory-reuse-map.md) §0). External/market facts cite the committed sibling
> chapters [02](02-indian-fno-microstructure.md) / [08](08-seller-blowups-and-tail-events.md) /
> [09](09-capital-margin-and-sizing.md) / [10](10-seller-edge-and-execution-evidence.md) (each
> of which carries its own per-claim provenance and UNVERIFIED tags), or are tagged UNVERIFIED
> and collected in the §15 register. Cardinality is ARITHMETIC — every count shows its
> multiplication (Golden Rule #10); no cardinality is presented as a measurement. Lake
> measurements (overlay coverage, chain depth, per-trial cost) are **PENDING-ON-BOX recipes,
> never numbers** — the authoring container has no lake. No number is `guaranteed`/`fully`/`100%`;
> where a family cannot be honestly priced, sized, or settled it **REFUSES and is counted** —
> the repo's own house law, made the spine of this chapter.

---

## §0. The thesis, the inverted design order, and the four honesty invariants

**A reuse-maximal design asks "what can we reuse?" and reaches roughly half-verbatim (the full
row-by-row account is chapter [03](03-brutex-inventory-reuse-map.md) §1). The risk-first design
asks a different question first — "what can this data + this machinery *honestly claim*, and
what must it *refuse*?" — and answers it as a sequence of ADMISSIBILITY GATES a structure must
pass *before* it is ever sweep-eligible.** The substrate (grammar, walker, identity) is
identical; the *ordering* is inverted, because for a premium seller the dangerous failure mode
is not a missing feature — it is a **plausible-looking number that hides an unsampled tail, an
unpriceable fill, or an unmodellable margin call.** The engine's own worst-case-fill law already
encodes the operator directive that says this out loud — backtests are never more optimistic
than reality (`brutex/execution/fill_mode.py:3-4`; `DEC-FILL-WORST-CASE-ONLY-001`, MIGRATION.md
#1825 / ADR-0023). The seller engine is that sentence applied to a structure with N legs,
temporally-inverted cash flows, and a left tail the 2020→today window may simply not contain.

The seller's edge is real and structurally a variance-risk premium (chapter
[10](10-seller-edge-and-execution-evidence.md): Carr–Wu 2009, Bakshi–Kapadia 2003) — *gross of
tails*. The whole point of this engine is that **BruteX's cascade plus the new tail battery
(§7) is the only honest instrument for reading what the seller's edge is worth NET of the tail
the seller pretends isn't there.** The design therefore front-loads refusal, so that everything
reaching the tail battery is already capital-true, fill-honest, and settle-honest.

**The four honesty invariants — each an existing repo law, re-read risk-first:**

1. **Correct-or-absent, at structure grain.** A structure that cannot be priced, sized, or
   settled honestly **refuses ATOMICALLY and is counted** — never simulated approximately, and
   never *partially* filled (a 3-of-4-leg iron condor is a naked strangle — a different risk
   object whose capital basis becomes a lie; chapter [04](04-gap-analysis.md)). The mechanism
   is the shipped `RejectReason` enum + count-consistency discipline — `NO_DATA / TICK_FLOOR /
   LOT_SIZE_UNKNOWN / STALE_BAR / COST_REGIME_UNVERIFIED` (`brutex/options/executor.py:161-184`)
   with the fail-loud survivorship-tilt disclosure baked into the result docstring
   (`executor.py:606-622`: the accepted-trade sums carry an unstated survivorship tilt — read
   `net_pnl_paisa` alongside `n_rejected`). The seller extension **widens** the vocabulary
   (`STRUCTURE_LEG_NO_DATA`, `STRUCTURE_LEG_STALE_BAR`, `STRUCTURE_OVER_FREEZE`,
   `MARGIN_UNVERIFIED`, `SETTLE_UNMODELLED`, `STRUCTURE_CROSS_UNDERLYING` — names indicative)
   and keeps the `__post_init__` count-reconciliation law.
2. **Worst-case only.** Every fill takes the adverse intra-bar extreme; the product-wide law
   admits *no other mode, no env knob, no CLI flag, no per-call parameter*
   (`brutex/execution/fill_mode.py:1-43`, `:60-72` — `FillMode.WORST_CASE` is the SINGLE enum
   member; `:92-123` refuses a set `$BRUTEX_FILL_MODE` for *every* value including
   `worst_case`). For a *seller* this is not paranoia — it is the only law that survives the
   freak-trade evidence: chapter [10](10-seller-edge-and-execution-evidence.md) documents a
   2021-08-20 NIFTY 16450 CE spiking ₹100 → ₹803.05 in seconds with an SL-market buy filled at
   the bar HIGH — so `buy @ bar_high + tick` *reproduces the real catastrophe* instead of
   smoothing it. §4 extends the law per leg.
3. **Additive identity.** New run-identity keys are **default-omitted** so every existing spot
   `run_id` stays byte-identical, and anti-aliasing keys are **unconditional**
   (`brutex/engine/sweep_run.py:719` `canonical_trade_params`; `:734` — no floats, no objects,
   only ints / strings / None; `:798` `roll_schedule` emitted only for the futures alias with
   the presence⟺meaning invariant at `:603`; `fill_mode="worst_case"` unconditional per
   `fill_mode.py:32-35`). The seller axes ride this exactly (§9) — **including the fill-honesty
   and sizing axes that SHAPE the accepted trade set** (Golden Rule #4).
4. **Never a ranking gate without its own DEC.** Every seller analytic lands FIRST as a
   display-only annotation (own sidecar, own fence, `—` abstains, zero run-identity impact) and
   is promoted to a *rejecting* gate only by a separate operator-gated DEC. Three in-code laws
   currently pin these surfaces non-gating and must be *consciously superseded* to become
   gates: `brutex/engine/resilience_streaks.py:10-16` ("ANNOTATION / DISPLAY ONLY. Never
   re-ranks / filters / rejects / mutates a winner. ZERO run-identity impact"),
   `brutex/engine/session_lens.py:38` ("never a ranking gate"), and
   `brutex/engine/combo_aggregates.py:91-92` (the one-regime-wonder flag: "a deliberate display
   heuristic — documented, never a ranking gate").

---

## §1. The four admissibility gates — the pruning law

A structure passes the gates in this order; failing any gate makes it a **counted refusal**,
never an approximate simulation. The gates are the design's load-bearing pruning law: they
decide the v1 sweep set (§2.4) *before* cardinality is even discussed (§8).

| Gate | Question | Verdict values | Where decided |
|---|---|---|---|
| **G-CAPITAL** | Can this structure's capital-at-risk be stated *exactly* from stored data? | `max_loss` (exact) / `REFUSED` (undefined-risk, no SPAN) / `max_loss*` (finite-large, disclosed) | §5 (margin truth) |
| **G-FILL** | Can every leg's price be bounded against a *demonstrated* trade at the *simulated size*? | class **A** (bounded-fill) / **B** (bounded-price, unbounded-size) / **C** (refused) | §4 (fill realism) |
| **G-SETTLE** | Can this structure's terminal (expiry/exit) be resolved without a fabricated settle price or an unmodellable physical delivery? | proxy-band (index cash) / forced-pre-expiry (stock) / `REFUSED` | §6 (settlement) |
| **G-TAIL** | Does the *surviving* edge survive the average-of-the-worst-α, every regime stratum, and the extreme-event days? | pass / **FLAG** / (future gate: reject) | §7 (the tail battery) |

---

## §2. The structure grammar — admissibility-stamped at construction

The grammar is a strict superset of today's single-leg addressing: `brutex/options/executor.py`
is BUY-only, one leg per trade — `DEC-EXEC-008`: Track 2 only ever BUYS options, never writes /
shorts; max loss = premium paid; no SPAN/ELM margin modelling (the executor's own docstring
stance; chapter [10](10-seller-edge-and-execution-evidence.md) §0). The seller slice
**supersedes `DEC-EXEC-008` for the structure track only**; a 1-leg long structure remains the
valid degenerate case.

### §2.1 The atom and the molecule

```
StructureLeg  := { side ∈ {long, short}, type ∈ {CE, PE, FUT},
                   strike_selector (E-* family, §2.2), expiry_tier (§2.3), ratio ∈ ℕ⁺ }
OptionStructure := { family_token, legs: 1..4 (v1) ordered tuple[StructureLeg],
                     entry_instant : ONE decided bar for ALL legs,
                     exit_policy   : the §4.4 management state machine,
                     capital_basis, fill_class_floor, settlement_basis }   # the three admissibility stamps
```

**Construction invariant.** All legs share ONE decided entry bar instant and ONE decided exit
instant; the roll-up enforces `structure_net == Σ leg_nets` at construction — the exact
`MaterializedFuturesTrade` `net == gross − charges` invariant precedent
(`brutex/futures/executor.py:422-424`). Per-leg pricing composes N existing single-leg
breakdowns (the calculator is pure composition per leg, `executor.py:18-28`). **v1 cap: 1–4
legs, same underlying, index only** (§6.3); 5+-leg / cross-underlying structures refuse at
construction with a typed reason.

**The risk-first difference from a reuse-maximal grammar: a `StructureSpec` is NOT
sweep-eligible until its three admissibility stamps resolve to a runnable verdict.**
`capital_basis` is decided by G-CAPITAL (§5) — an undefined-risk family stamps `REFUSED` and
never enters the v1 sweep; `fill_class_floor` is the minimum fill-honesty class the family may
claim (§4.5); `settlement_basis` is decided by G-SETTLE (§6). The grammar is *complete* (every
seller family is expressible); the v1 *admissible set* is exactly what passes the gates.

### §2.2 Strike-selector families — strike-offset addressing over the DYNAMIC strike step

Each selector is a pure function `(entry_bar, side, type) → Decimal strike | REFUSE`, evaluated
with the shipped moneyness machinery — `classify` / `derive_strike_step` / `resolve_strike_step`
/ `atm_offset` / `moneyness_view` (`brutex/data/moneyness.py`; O(1), never raises; the strike
GRID is the **dynamic** per-chain step — the min positive strike gap derived from the chain
itself, never a hardcoded NSE rule). Seed grids are the taxonomy chapter's
([01](01-strategy-taxonomy.md)):

| Selector | Rule | Seed grid | Risk-first honesty note |
|---|---|---|---|
| `E-ATMOFF(n)` | ATM ± n grid steps | n ∈ {0,1,2,3,5,8,10,15,20} | overlay-free → the **coverage-robust default** |
| `E-DELTA(δlo,δhi)` | strike whose stored \|Δ\| ∈ [δlo,δhi] | {(.05,.10),(.10,.20),(.15,.25),(.25,.35),(.45,.55)} | **refuses when the Greeks overlay is NULL** — the 7-status ladder `STATUS_NO_RATE/AT_EXPIRY/NO_SPOT/BAD_INPUT/BELOW_INTRINSIC/NO_SOLUTION/OK` (`brutex/data/greeks_enrich.py:129-136`). The last ~600 s pre-expiry is `AT_EXPIRY`-NULL by design ⇒ a Δ-selected 0DTE strike is **unresolvable in the final ~10 minutes** (chapter [04](04-gap-analysis.md)); the production-lake coverage fraction is **UNVERIFIED (G8)** — the on-box recipe is a `brutex greeks-backfill --dry-run` census |
| `E-EM(m)` | S ± m·EM, EM = S·IV·√(DTE/365) | m ∈ {0.5,0.75,1.0,1.25,1.5,2.0} | inherits the same overlay-NULL refusal + the enrichment's q=0 / rate-6.5%-ASSUMED / T-anchor bias (chapter [04](04-gap-analysis.md)) |
| `E-IVW(w0,a)` | wing width = round(w0·(1+a·IVR)) | w0 ∈ {2,4,6}; a ∈ {0,0.5,1.0} | **a>0 is UNAVAILABLE in v1** — it needs an IV-rank series that does not exist (§3.3) and a VIX-derived variant would inherit the Rule-3 bar. **v1 ships fixed-width wings (a=0 only).** |
| `E-YIELD(y)` | net credit ≥ y of the structure WIDTH | y ∈ {0.20, 0.33, 0.40} of **width** (= the exact max-loss capital basis, §5.1 — no SPAN needed) | the SPAN-**margin** denominator stays BLOCKED (§5) and undefined-risk families refuse; defined-risk (v1-admissible) families use credit/width |
| level-anchored | strike pinned to a 60-bit spot-mask level (pivot / PDH-PDL / fib) | — | overlay-free; the entry-mask bits themselves (§3) |

### §2.3 Expiry-tier addressing (`expiry_tier`) — vendor DATA, never a formula

Reuse the movers tier law verbatim: `_EXPIRY_TIERS = {near:0, next:1, far:2}` × tracks
`{all, weekly, monthly}`, monthly = last-in-calendar-month (`brutex/dashboard/movers_options.py`).
Tokens `W0/W1/W2`, `M0/M1/M2`. The **authoritative** ladder is vendor DATA via
`RegistryStore.list_expiries` / `list_contracts` — never a computed last-Thursday. Honest limits
carried forward (chapter [02](02-indian-fno-microstructure.md)): (i) stock options are
monthly-only in India (weekly stock options do not exist), so `W*` refuses on a stock
underlying — **moot in v1 (index-only)**; (ii) since 2024-11-20 only ONE benchmark weekly
exists per exchange (NIFTY/NSE, SENSEX/BSE), so the weekly space narrowed after that date — the
vendor calendar reproduces this discontinuity **by itself** (dead contract classes go honestly
absent; chapter [08](08-seller-blowups-and-tail-events.md)).

### §2.4 The family table with the risk-first admissibility stamp — 7 admissible, 6 refused

DSL: `S`=short, `B`=long; `@sel`=selector; `/Ttoken`=tier; `×r`=ratio. `UND`=undefined risk.
The **v1 capital-admissible?** column is the load-bearing risk-first ruling — decided by
G-CAPITAL (§5), and it determines the v1 sweep set:

| # | Family | Legs | Defined risk? | **v1 capital-admissible?** | Why (risk-first) |
|---|---|---|---|---|---|
| 1 | Short straddle | `S:CE@ATMOFF(0)/W0 + S:PE@ATMOFF(0)/W0` | **UND** | **NO — REFUSE** | naked both sides; no structural max loss ⇒ no honest capital denominator without SPAN |
| 2 | Short strangle | `S:CE@Δ(.10-.20)/W0 + S:PE@Δ(.10-.20)/W0` | **UND** | **NO — REFUSE** | same; the canonical inversion-ruin structure |
| 3 | Iron condor | `S:PE@Δ + B:PE@−W + S:CE@Δ + B:CE@+W /W0` | **DEF** `max(W)−C` | **YES** | capital = structural max loss, exact arithmetic |
| 4 | Iron butterfly | `S:CE@ATMOFF(0)+S:PE@ATMOFF(0)+B:PE@−W+B:CE@+W /W0` | **DEF** `W−C` | **YES** | exact |
| 5 | Bull put spread | `S:PE@Δ/T + B:PE@−W/T` | **DEF** `W−C` | **YES** | exact; entry mask = bullish 60-bit point (§3) |
| 6 | Bear call spread | `S:CE@Δ/T + B:CE@+W/T` | **DEF** `W−C` | **YES** | exact; entry mask = bearish 60-bit point |
| 7 | Covered call (index, FUT-covered) | `B:FUT/M0 + S:CE@Δ/T` | **DEFINED-LARGE (downside)** | **NO — REFUSE** | the covered futures leg is SPAN-margined ⇒ not v1-capital-admissible without SPAN history |
| 8 | Cash-secured put | `S:PE@Δ/T + reserve K·lot` | **FIN-large** `K−C` | **YES\*** | max_loss exact but a large multiple of real SPAN (magnitude **UNVERIFIED (G1)**, chapter [09](09-capital-margin-and-sizing.md)) — admitted under `capital_basis="max_loss"` with the understatement DISCLOSED (ruling R-1, §14) |
| 9 | Front-ratio (credit) | `B:CE@ATMOFF + S:CE@Δ ×2` | UND (one wing) | **NO — REFUSE** | extra naked leg |
| 10 | Jade lizard | `S:PE@Δ + S:CE@Δ + B:CE@+W_c`, `C>W_c` | UND-down | **NO — REFUSE** | naked put side |
| 11 | Calendar short-premium | `S:{CE\|PE}@ATMOFF/W0 + B:same/{W1,M0}` | ≈DEF (debit) — **EU-put exception** | **NO — EXCLUDE** | cross-expiry max-loss is NOT a theorem: a deep-ITM **European** put can trade *below intrinsic* (`P_eur ≥ K·e^{−rT} − S < K−S`), so a crash can cost **more than the debit** — the discounted-parity floor (the enrichment's own intrinsic-floor region, `brutex/data/greeks_enrich.py:653-672`; chapter [04](04-gap-analysis.md), G11); AND the far leg has no stored bars historically (the expiry−30d pull wall, chapter [04](04-gap-analysis.md)) |
| 12 | 0DTE straddle/strangle/fly | rows 1/2/4 legs with `/W0=today` | per base | **fly YES; straddle/strangle NO** | the fly is defined-risk; the naked variants refuse exactly like their non-0DTE parents |

**v1 admissible set = {3 iron condor, 4 iron butterfly, 5 bull put spread, 6 bear call spread,
8\* cash-secured put, 0DTE iron fly, degenerate-long} — 7 families** — the defined-risk
repertoire on index underlyings inside the honestly-priceable cost window [2024-10-01 → today]
(the option-ETC verified-window floor, `brutex/costs/constants.py:126-136` refusal doctrine;
§6.2). **REFUSED in v1: 6 families** — short straddle (1), short strangle (2), index covered
call (7), front-ratio (9), jade lizard (10) on the capital axis; calendar (11) on the
cross-expiry max-loss + data-wall grounds. The refused families are **first-class in the
grammar but refuse on their named axis** and unlock *additively* when the SPAN-margin DEC
(§5.5) and the cross-expiry-bound DEC land — the exact `margin_history.py` posture ("annotate
winners" extended to "admit families").

---

## §3. Composition with the 60-condition u128 entry-mask vocabulary

### §3.1 The entry mask is reused verbatim — masks gate ENTRY only

A selling structure's entry condition is a point in the existing vocabulary, consumed with ZERO
change: `TOTAL_CONDITIONS = 60` (`brutex/vocabulary/bits.py:167`), `IMPLEMENTED_MASK =
1152921504606846975 = (1<<60)−1` (`bits.py:173`), and the mask is **already a u128**
(`bits.py:11,168`). A bull-put spread's entry is `ema20_above_ema200 & close_above_vwap &
!gap_down_day`; a bear-call's is `ema20_below_ema200 & close_below_vwap & BIT_BOS_BEARISH`; a
range structure's is `BIT_INSIDE_DAY & !BOS`. **The option layer only picks strikes / tiers /
exits on top of a mask the kernel already evaluates and counts. Structures and exit laws are
SEPARATE sweep axes (`canonical_trade_params` params, §9) — never new vocabulary bits.**

### §3.2 IV-rank / DTE / structure knobs are PARAMS, not bits

They ride `canonical_trade_params` as default-omitted cell-coordinate axes (the `fib_anchor` /
`PriceSource` precedent — `price_source_params_fragment` at `sweep_run.py:797`;
`DEC-NIFTY-PRICE-SOURCE-AXIS-001`, MIGRATION.md #1681). They are NOT vocabulary bits for two
hard reasons: (i) **India VIX is barred from the mask by CLAUDE.md §2 Rule 3** (REFERENCE_ONLY,
never a bit, never in run identity), and any IVR floor derived from VIX or an ATM-IV percentile
inherits that bar; (ii) an option-*state* bit is a vocabulary scope change needing its own DEC
(§3.4).

### §3.3 The risk-first flag: the seller's most important entry signal is UNAVAILABLE in v1

**The classic short-premium entry rule is "sell when IV-rank is high" (chapter
[10](10-seller-edge-and-execution-evidence.md)); v1 has NO IV-rank substrate.** No
per-underlying constant-maturity ATM-IV history series exists (the panel's grep census found
zero `iv_surface|iv_rank|iv_percentile|term_structure|constant_maturity` hits; chapter
[04](04-gap-analysis.md)). Building one is a NEW derived FEATURE LAKE (its own DEC, its own
O(1)/bar freshness guard), *additionally* blocked for a VIX-derived variant by the Rule-3 bar,
and *additionally* tenor-capped by the expiry−30d pull wall (a 30-day constant-maturity point
sits at the *edge* of available data). **Consequence stated plainly: a v1 seller sweep is
entry-blind on the vol-regime axis that matters most.** The design does not paper over this —
it ships the entry mask + DTE + structure grid it CAN evaluate, and every result surface
carries an explicit `iv_regime_gate = unavailable` disclosure. Families whose entire edge is
vol-regime timing are honestly under-served in v1 and marked so.

### §3.4 The 60→N≤92 vocabulary extension is kernel-ready — and is a SEPARATE, LATER DEC

The engine is kernel-ready TODAY: the u128 count/enum kernels are the **active production path
for any ≥64-bit dispatch** — `brutex/engine/combo_mask.py:29-45` routes any bit ≥ 64 to
`fno_math.count_supermasks_u128s` / `ResidentBarsU128` over the ONE width-generic `MaskWord`
core (`:36`), `U64_MASK = (1<<64)−1` is the boundary (`:161`), and a missing wide kernel raises
the typed `WideKernelUnavailableError` — the LOUD-FAIL law, never a silent numpy degrade
(`:44`; `DEC-ENUM-KERNEL-U128-001`, MIGRATION.md #1942). So the vocabulary can grow 60 → ~92
bits with **no kernel work**.

**But growing the vocabulary is its own operator-gated DEC (Golden Rule #3), NOT part of the
capability slice, and NOT the same as admitting options into engine scope.** The three gates
are DISJOINT: (1) `DEC-OPTSELL-STRUCTURE-EXECUTOR-001` (#2211) ships the
structure/executor/walker/results slice with the vocabulary **unchanged at 60 bits**; (2) a
vocabulary extension (`DEC-OPTSELL-VOCAB-EXTEND-00N`, unminted) must build the indicator in the
shared `rust/trading-core` crate, bump `indicator_version` (a verified run-identity input,
`brutex/strategies/combo.py:9-10,253`), prebuild a feature lake for O(1)/bar, and satisfy the
Rule-3 bar for any VIX/IV-derived bit; (3) admitting **options as instruments** into the
sweep-scope is a THIRD, further scope-expansion DEC. The design draws all three lines
explicitly so no one gate can be mistaken for another.

---

## §4. The N-leg structure walker — fill realism as law (G-FILL)

The walker is the `trade_sim.py` sibling — the one genuinely NEW hot-path module (the NEW-NEEDED
cluster census is chapter [03](03-brutex-inventory-reuse-map.md) §1.7). This section is the
honesty core of the design: for a seller, the fill law is where the backtest either tells the
truth or fabricates an edge.

### §4.1 Per-leg worst-case fills — the shipped #1825 law applied per leg

Each leg is priced at ITS OWN intra-bar adverse extreme, verbatim from the product-wide law
(`fill_mode.py:8-24`; per-leg option arithmetic `brutex/costs/calculator.py:123-181`):

- **long (buy-to-open, or buy-to-close a short):** fill = `leg_bar.high + tick` (most debit).
- **short (sell-to-open, or sell-to-close a long):** fill = `leg_bar.low − tick` (least
  credit).
- floored at one tick (the calculator's tick-floor law); SL/TP-triggered legs take the
  with-the-level clamp `max(level, high)` / `min(level, low)` exactly (`fill_mode.py:10-13`).

The options SELL branch already exists in the calculator (`calculator.py:164-181`; the futures
executor prices live short legs with the identical adverse-both rule) — it is only
production-*unreachable* under `DEC-EXEC-008`. **Option writing inherits a fully-formed per-leg
SELL fill law; only its activation is new.** The four bar extremes are REQUIRED at construction
(`brutex/costs/types.py:303-314`); an anchor-less input refuses fail-loud.

### §4.2 All-or-nothing atomic per structure per bar (the writer-mandatory law)

Bar data has NO book depth and NO queue observability; a 1-minute bar either printed volume or
it did not. A partial fill — or a structure entering with 3 of 4 legs — is **unobservable
fiction**. **Law: all-or-nothing per structure per bar** (chapter
[04](04-gap-analysis.md)). If ANY leg lacks its entry bar OR its exit bar (source `None` →
`NO_DATA`, `brutex/options/lake_source.py:151-156`) OR fails `volume > 0` (→ `STALE_BAR`,
unconditional, `brutex/options/executor.py:543-557`), the WHOLE structure is a counted reject
carrying the failing leg — **never a partial structure.** This is mandatory for writers: an
iron condor missing its bought wing is a *naked strangle*, and its `max_loss` capital basis
(§5) would become a lie. The single-leg survivorship tilt (`executor.py:606-622`) **COMPOUNDS
with N** — deep-OTM wings (exactly the legs defined-risk structures BUY) are the least liquid,
so structure-level rejects correlate with the market states short premium finds hardest; **all
rule comparisons remain within-accepted-set comparisons only, and the censoring rate is
REPORTED per config** (a fabricated absence would hide the blow-ups — the bias runs in the
*dangerous* direction for sellers).

### §4.3 Order-independence honesty (the mandatory disclosure sentence)

All N legs fill in the SAME decided bar, each at its own worst-case extreme. This is
**order-INDEPENDENT by construction** — 1-minute bars carry no intra-bar sequencing, so any
leg-ORDER model would be fabricated microstructure. **Mandatory
`DEC-FILL-WORST-CASE-ONLY-001`-style disclosure: charging each leg its own intra-bar worst is
strictly MORE pessimistic than a real simultaneous package execution** — a real basket/IOC
spread nets some of the adverse extremes, so per-leg independent extremes can *exceed* the
worst NET package price actually printable in that minute (G12 — logically argued,
unmeasurable at 1-minute granularity, and the direction only *strengthens* the conservative
bias; the spot engine's own interim-honesty stance, `brutex/engine/trade_sim.py:69-73`:
"strictly MORE pessimistic … never optimistic"). No tighter package bound is observable
without co-timed intrabar data; it is rejected as fabrication.

### §4.4 The management state machine — the defense/exit grammar as EXIT-LAW axis members

Per open structure, per 1-minute bar, over the joined premium path (Σ signed leg closes — an
O(legs) join, the read the movers-options screen already performs per contract via
`_read_future_session`): read leg closes → compute the structure mark → evaluate triggers in a
**pinned deterministic order** → act. **v1 management law = static structures with a managed
exit**; the leg-mutating adjustments (`D-ROLL-U` roll-the-untested-side, `D-RECENTER`
delta-band re-center, `D-WING` add-a-wing, `D-INVERT`) are **v2 EXIT-LAW axis members on
restricted spaces** (§8.3) — the defense/adjustment grammar lives on the exit-law axis, never
in the vocabulary:

| Trigger | Timing | Fill class | Seed grid |
|---|---|---|---|
| `D-TP(p)` — profit-take, buy-to-close all | **chosen** | plain adverse extreme | p ∈ {25,50,75} |
| `D-SL-k(k)` — loss ≥ k·C, close all | **FORCED** | adverse extreme **+ `def_penalty_ticks`** | k ∈ {1.5,2,3} |
| `D-DTE(d)` — DTE ≤ d, close | scheduled | plain | d ∈ {21,14,7,1} |
| `D-STOP-TOUCH` — spot touches the short strike, close tested/all | **FORCED** | adverse extreme **+ penalty** | scope ∈ {tested, all} |
| **hard 15:20** — forced exit | structural | plain adverse extreme | mandatory (reuses `trade_sim.py:46-53` — the forced exit "overrides every other exit") |

**Priority (worst-case-first, the pessimistic tie generalized).** Same-bar ties resolve to the
loss-crystallizing side: forced-session-exit (P0, `trade_sim.py:47`) > stops / stop-touch (P1)
> DTE (P2) > … > profit-take (last). A bar satisfying both a stop and a take resolves to the
STOP — extending `trade_sim.py:43-45` ("the STOP fills first, always") to the whole grammar.

**THE DEFENSE-FILL CLASS (the load-bearing risk-first law).** Every action whose timing is
**FORCED by the market** (SL, stop-touch close; in v2: roll legs, wing-add, invert) fills at
`adverse_extreme ± def_penalty_ticks`, where `def_penalty_ticks` is a **sweepable,
default-omitted axis** (default 0 ⇒ byte-identical to a penalty-free run). Entries and
profit-takes (timing *chosen*) keep the plain adverse extreme. **Rationale** (chapter
[10](10-seller-edge-and-execution-evidence.md)): the bar's traded range under-represents the
executable price EXACTLY when defense triggers fire — inventory-risk + adverse-selection +
hedge-cost repricing, no quoting obligation on Indian index options, and
crowded-defense-trigger correlation (mechanism-anchored; the Indian *magnitude* UNVERIFIED),
with the freak-trade prints as the observable footprint. The penalty axis makes a strategy's
**defense-slippage fragility a MEASURED sweep OUTPUT, never an assumption.** It is NEVER
labelled a slippage/spread model.

**THE DEFER-TO-NEXT-VOLUME>0 LAW (never reject a defense).** When a defense action on leg L
decides at minute t and L's bar at t is absent OR `volume == 0`:

1. Walk forward to the FIRST bar `t' > t` of L with `volume > 0` **within the same session**;
   fill there under the defense-fill law (buy `max(high, level) + penalty`, sell
   `min(low, level) − penalty`). Deterministic and look-ahead-clean — it consumes only bars ≥
   the decision instant (the walker's forward-only law, `trade_sim.py:75-78`).
2. Count every skipped minute: `defense_deferred_minutes += (t' − t)`; stamp `deferred=true`.
3. If NO such bar exists before the forced-exit boundary → index CE/PE: settle at the
   settlement proxy (§6.1) with reason `defense_expiry_settled`; else carry to the NEXT
   session's first `volume>0` bar under `defense_session_gap` (the seller twin of the engine's
   `session_gap` law + counter, `trade_sim.py:60-65`), with interim MTM at last-known close and
   a `stale_mark_minutes` counter.
4. **NEVER emit a `NO_DATA`/`STALE_BAR` REJECTION for a defense action** — those reasons stay
   entry-only. A rejection would silently convert *"couldn't price the close"* into *"held the
   naked short for free"* — the exact anti-conservative inversion of the law's intent. **This
   is the single most important seller-specific fill law, and it deliberately inverts the
   current executor's default** (which rejects zero-volume bars): rejecting a *forced close of
   an open short* hides the risk it was supposed to price.

### §4.5 Fill-honesty classes A/B/C — a FIRST-CLASS per-trial output

The risk-first design makes the fill-honesty class a **primary, identity-shaping output**,
because for a writer the honest question is not "did it win?" but "did it win at a fill I could
actually achieve at my size?" Per-leg predicates evaluated at BOTH the entry and exit decided
bars:

| P | Predicate | Existing machinery |
|---|---|---|
| P1 | bar present | `lake_source.load_pair → None ⇒ NO_DATA` (`lake_source.py:151-156`; `executor.py:532-541`) |
| P2 | `volume > 0` | the `STALE_BAR` gate (`executor.py:543-557`) |
| P3 | `volume ≥ λ · leg_qty_units` | NEW gate; λ swept; the floor idiom `movers_snapshot.py:436,457` |
| P4 | liquid-band: \|`atm_offset`\| ≤ B AND near-expiry AND index | `moneyness.atm_offset`; registry tiers |
| P5 | `leg_qty_units ≤ freeze_quantity` | freeze plumbed (`brutex/execution/types.py:72-81`); the gate is NEW |

| Class | Definition (over ALL legs, entry AND exit) | What may HONESTLY be claimed |
|---|---|---|
| **A — bounded-fill** | P1∧P2∧P3∧P4∧P5 on every leg | the per-leg price bound plausibly holds AT the simulated size for liquid ATM-band near-expiry index legs; **residuals remain** — the impact counterfactual (P3 passing does NOT bound market impact; λ is uncalibratable, §4.6) and the instant-spread corner (a bar can print H−L=₹3 while the book quotes ₹15 wide — undetectable from OHLCV) — **DISCLOSED, not zero** |
| **B — bounded-price, unbounded-size** | P1∧P2 on every leg, but ≥1 leg fails P3/P4 | the price bound holds for a MARGINAL (small) order only; the run's SIZE claim is NOT supported for this trade |
| **C — refused** | ≥1 leg fails P1/P2/P5 (or lot/cost-regime) | nothing — atomic reject, counted with the failing leg + reason |

These per-TRIAL fill-honesty classes A/B/C are the taxonomy of record; chapter
[10](10-seller-edge-and-execution-evidence.md) §6's F-A…F-E are a DISTINCT per-ACTION
execution-realism scheme that FEEDS this per-trial verdict — see 10 §6.

**Counters** (the `OptionsRuleResult` extension shape, `executor.py:602-712` — every counter
typed, `__post_init__`-reconciled, no catch-all): `n_class_a`, `n_class_b` with
`n_accepted == n_class_a + n_class_b`; `n_class_c` decomposed per failing predicate; per-trade
regime flags folded to counters `n_expiry_day` (a leg's decided bar date == expiry) and
`n_last_30min` (decided bar ≥ 15:00 IST); `fill_honesty_class ∈ {A,B}` as an annotation on
accepted rows (annotation-only, never re-ranks — the R13 precedent,
`brutex/futures/executor.py:14-16`).

**IDENTITY CONSEQUENCE (Golden Rule #4):** λ, B, the P4 band/tier definition, and the Class-B
acceptance policy (`size_honesty ∈ {flag, refuse}`) **SHAPE the accepted trade set**, so
whenever they gate or are swept they MUST enter `canonical_trade_params` (§9) exactly like
every other axis. The class COUNTERS are outputs and stay out of identity. **The sweep then
answers, per family: what fraction of this edge survives at Class A only? — that is the honest
headline number for any option-writing claim, and no seller result surface may omit it.**

### §4.6 The spread-penalty sweep axis — an uncalibrated robustness knob, PERMANENTLY

`spread_penalty_ticks = K` (per run, int ≥ 0, default 0): every leg's fill moves K further
ticks beyond the worst-case anchor — `buy = high + (1+K)·tick`,
`sell = max(low − (1+K)·tick, tick)`, reusing the calculator's one-tick floor verbatim. Lattice
K ∈ {0,1,2,4,8}. Default-omitted identity (K=0 emits no key). Read-out: the breakeven **K\***
(largest K with net edge > 0) as a REPORTED robustness statistic (annotation-only, never a rank
key) — a family that dies at K=1 is spread-fragile whatever its K=0 Sharpe says.

**Why "uncalibrated" is a HARD, PERMANENT property — the refusal-precedent framing.** The
repo's costing doctrine forbids fabricated market constants with NO runtime escape hatch
(`brutex/costs/constants.py:126-136`: no flag/kwarg may bypass the refusal; the only sanctioned
override paths are DATA), and `margin_history.py` ships refusal-only rather than invent a
number. **A "NIFTY ATM spread = X ticks" constant would be the same class of fabrication.**
Therefore the axis is labelled, in code and doc, a **SENSITIVITY parameter — never a "slippage
model", never a claimed cost.** Calibration would need historical QUOTE data; the only
realistic source is forward-captured 1-second L1 snapshots (`docs/vendors/gdfl/VERDICT.md`),
and **a 2020→now 1-second backfill does not exist — forward capture from subscription day is
the only path.** Consequence, stated without varnish: **the 2020→present backtest window is
PERMANENTLY uncalibratable for spreads.** A future forward-capture archive could ground a
calibrated, date-scoped spread table (its own DEC, DATA-shaped like a `RegimeRow`, and only for
dates after capture start). Until then K is the only honest instrument, and it measures
fragility, not cost.

---

## §5. Margin truth — the G-CAPITAL gate that decides the v1 family set

This section is why the v1 admissible set is what it is (§2.4). It is the design's most
consequential ruling: **capital honesty is a GATE, not an afterthought — a family that cannot
state its capital-at-risk exactly does not enter the v1 sweep at all.**

### §5.1 V1 — the shipped refusal-window posture, extended from "annotate" to "admit"

The house law is `brutex/engine/margin_history.py:1-28` — recon found NO sourceable SPAN
history anywhere; every shipped table is ONE refusal row from 2020-01-01; `margin_for_date`
raises the typed `UnverifiedMarginRegimeError` for every date; NO runtime escape hatch; the
only sanctioned override paths are DATA. The risk-first extension:

- **Defined-risk families: capital = structural MAX LOSS, computable EXACTLY** from strikes +
  premiums + the date-keyed lot (`brutex/options/lot_size_history.py`), with zero SPAN
  dependence (chapter [09](09-capital-margin-and-sizing.md)):
  - credit vertical: `(width × lot) − net_credit`
  - iron condor: `(max wing width × lot) − net_credit`
  - iron butterfly / long butterfly / all-long: `W − C` / net debit — all **EXACT today**.
  Max loss is the textbook-conservative capital base: reserving it means the position can NEVER
  demand more capital than reserved (which structurally eliminates the margin-call model for
  these families — §5.4), and **ROI on a max-loss basis UNDERSTATES the leveraged ROI a real
  trader sees** — a conservative bias, the same side the worst-case fill law already chose.
  This is the honest-ROI law: defined-risk ROI = net P&L ÷ structural max loss.
- **Undefined-risk families (short straddle / strangle / naked-call-bearing): REFUSED margin
  AND ROI in V1.** Their capital consumption is SPAN + exposure + ELM — portfolio- and
  day-specific, reproducible only from the day's SPAN file; a hardcoded percentage is exactly
  the fabrication `docs/track2/TRACK2_RISK_MGMT.md:109-121` forbids. Extend the shipped table's
  key from `instrument → per-lot SPAN` to `(instrument, structure_class)` (the hedge benefit
  makes per-lot margin structure-dependent; chapter [09](09-capital-margin-and-sizing.md));
  every naked structure reads `margin_roi: unverified` and **is not v1-admissible.**
- **Cash-secured put (ruling R-1, §14):** max loss is finite and exact (`(K × lot) −
  net_credit`), so it CAN carry `capital_basis="max_loss"` — but that basis is a large multiple
  of the real SPAN margin (magnitude **UNVERIFIED (G1)**), so its ROI reads dramatically lower
  than practitioner expectation. **Ruled: admit under `max_loss` with the understatement
  DISCLOSED on every surface.**

### §5.2 V2 — the conservative-bound ANNOTATION model (`margin_proxy`)

A computable-today proxy, declared ANNOTATION-ONLY under R13 (`brutex/futures/executor.py:14-16`
— the pure spot signal row is never mutated; nothing here writes to the results store or
influences ranking), the SPAN-shaped replica `TRACK2_RISK_MGMT.md:118-121` sanctions with a
`margin_method` audit stamp (chapter [09](09-capital-margin-and-sizing.md)):

```
margin_proxy_per_lot(t) =
      premium_mark(t) × lot(t)                              # net-premium component
    + lot(t) × max over i of loss_i(t)                      # SPAN-like scan ladder:
        where loss_i = structure loss at spot shocks
        S(t)·(1 ± f_i · m(t)),  f_i ∈ {1/3, 2/3, 1},
        m(t) = max(K · σ̂(t), m_floor)                       # σ̂ = trailing daily vol from OUR
                                                            # 2020→today 1-minute→1-day series;
                                                            # m_floor presets = the worst-print regimes
    + e_rate × S(t) × lot(t)                                # exposure/ELM floor arm
margin_proxy = max(scan_arm, e_rate·notional)               # e_rate: ADR-stamped-ASSUMED or OMITTED
   with, for defined-risk, the clamp margin_proxy ≤ max_loss_per_lot + net_credit × lot
```

**Honesty rules on the proxy (rendered beside every number):** (1) NOT the exchange number —
no real PSR/VSR, no calendar charge, no intraday recalibration, no broker add-ons; the
direction of error is UNKNOWN per-day without a validation pass, so the label is
`margin_proxy (UNVERIFIED vs exchange)`; (2) NEVER a ranking input, never in run identity,
never in P&L (R13); (3) it uses OUR σ̂ and therefore **inherits vega blindness** — a
stress-sized strangle can breach a broker margin line intraday on an IV spike without the spot
ever reaching the scan boundary (written into the proxy's docstring, not papered over); (4) the
dated regime STEPS — the expiry-day short-index ELM add-on (eff. 2024-11-20) and the
calendar-spread-benefit removal on expiry day (chapter
[09](09-capital-margin-and-sizing.md), which flags the exact-date discrepancy) — CAN be encoded
exactly (cited percentages/dates, not SPAN outputs) and are the only V2 components that carry a
VERIFIED tag from day one.

### §5.3 The drawdown-to-margin spiral — REFUSED as a simulation

The seller's real ruin path is the pro-cyclical coupling of margin to the stress that produces
the loss: vol spike → SPAN parameters widen AND short marks rise → margin required rises AND
equity falls → shortfall → broker RMS force-reduces at the WORST prices → forced buy-backs push
marks further against the book → loop (chapter [09](09-capital-margin-and-sizing.md)).

**Without dated SPAN history this spiral CANNOT be simulated — only BOUNDED and ANNOTATED. The
design REFUSES to simulate it.** Concretely: `margin_required(t)` is unknowable per day ⇒
equity-vs-margin crossing times are unknowable ⇒ forced-reduction P&L is unknowable. **Any
simulated "margin call on 2020-03-23" would be a fabricated number chain (Golden Rule #1).**
What IS honestly computable from OUR data: the equity path (option-lake marks) + the §5.2
stress-scan BOUND — so every trade/day is ANNOTATED *"stress-bound loss ≥ X% of a declared
capital base; margin pressure PLAUSIBLE beyond here"*, **never "margin call occurred".** Even
WITH end-of-day SPAN files, intraday recalibration + broker add-ons + RMS discretion keep
broker-level forced-exit TIMING approximate. The upgrade ladder is: **refusal (today) →
EOD-dated margin series (SPAN ingest) → bounded intraday (interpolation, disclosed) — never a
claimed tick-accurate margin call.** This is precisely the `margin_history.py` posture, made a
first-class design law.

### §5.4 Sizing laws + the reserved-capital ledger (what IS computable)

The seller-native sizing laws that ARE exact from our data today (chapter
[09](09-capital-margin-and-sizing.md)) — **D** (defined-risk:
`n_lots = floor(B_risk · equity / max_loss_per_lot)`, EXACT), **N** (notional cap), **S**
(stress-loss cap, price-move arm only — with the disclosed vega blindness), **H** (portfolio
heat fold), **P** (concentration caps), **X** (structural clamps: `n_lots ≤ freeze_qty` per
order leg) — are all computable with zero external dependency. **M (margin-based sizing) and
any margin-denominated ROI ranking are REFUSED** until a SPAN-history ingest lands. The
reserved-capital ledger books `+net_credit` at entry for credit structures, `−net_debit` for
debit, with **no interest on premium float** (a disclosed simplification that UNDERSTATES
credit returns — the conservative side; materiality asserted second-order, not measured), and —
the key structural benefit — **with capital = max loss reserved up front, a defined-risk
structure can never demand more capital mid-trade than reserved, so the margin-call model is
NOT a correctness hole for these families, only a capital-efficiency understatement.**

### §5.5 The upgrade DEC — `DEC-SPAN-HISTORY-INGEST-001` (proposed)

Ingest NSE Clearing daily SPAN risk-parameter files (chapter
[09](09-capital-margin-and-sizing.md); the ICCL equivalent for SENSEX) → parse per-day
parameters → populate dated `MarginRegimeRow`s per `(instrument, structure_class)` → V1's
refusal windows convert to verified windows date-by-date, and V2's proxy demotes to a
cross-check. **Gate: the public archive's retention depth for past dates is UNVERIFIED (G2)** —
the DEC's phase-0 is an archive-availability probe for 2020→today. **If history is NOT
retrievable, the honest terminal state is: V1 refusal for the past, verified rows only from
ingest-start forward, V2 annotation for the past — permanently labelled.** The design carries
both branches; it never assumes the archive exists.

---

## §6. The expiry / settlement engine — G-SETTLE

The Track-2 trade-outcome state machine is a **fail-loud STUB** today — only `NORMAL_CLOSE` is
implemented, and the code-side Step-0 refusal raises `UnsupportedTradeOutcomeError` on anything
else (`brutex/costs/calculator.py:230-251`). Implementing it is a prerequisite of the SELL DEC.

### §6.1 Index cash settlement at a SETTLE-PRICE PROXY (with the divergence disclosed)

The official Final Settlement Price is the expiry-day closing value derived from the last ~30
minutes of constituent trading (a VWAP-like closing construct — chapter
[02](02-indian-fno-microstructure.md); the **exact formula is UNVERIFIED (G7)**). BruteX stores
1-minute bars; no `settlement_price` field exists anywhere in the lake. **Proxy + honest
divergence law:** v1 settles at a proxy derived from the stored 1-minute spot bars (the last
stored close, OR a last-30-min VWAP-like fold — pinned by ruling R-3, §14), with a **DISCLOSED
error band**; a short strike pinned INSIDE the band resolves at the **ADVERSE plausible settle
within the band** (the worst-case discipline generalized). **The band IS the pin-risk model**:
a settle-vs-LTP divergence flips a pinned short between "keep full premium" and "pay intrinsic"
discontinuously. Every expiry-settled trade carries `settlement_proxy=true` + the error-band
width + one capped WARN per sim (the `options.sim.unverified_cost_window` idiom). v2 = ingest
the exchange's published daily settlement prices as a new small lake dataset (its own DEC;
availability UNVERIFIED, G3). **A design-doc sentence in the `DEC-FILL-WORST-CASE-ONLY-001`
style is mandatory: no claim of settle-exactness may ship — the proxy is a bound, the band is
the disclosure.**

### §6.2 The STT trap — STT-aware expiry economics, per-regime, never a constant rule

All rates are the repo's VERIFIED date-keyed tables (`brutex/costs/constants.py`): sale STT is
**seller-side on PREMIUM** (0.10% for 2024-10-01 → 2026-03-31; 0.15% from 2026-04-01);
exercise STT is **purchaser-side on INTRINSIC** (0.125% through 2026-03-31; 0.15% after —
already encoded as forward-compat rows, `constants.py:82-95`). The trap, stated exactly:

1. **The short is NOT the STT payer on exercise** — exercise-STT is levied on the *purchaser*
   (the long assigned against you). The writer paid premium-STT once at entry and pays NO
   second STT on assignment (index cash-settled).
2. **The short's expiry cost is the settlement DEBIT** — the full intrinsic `(settle − K)⁺`
   (CE) / `(K − settle)⁺` (PE) × lot. This is the loss itself, not a tax. Sellers who "let it
   expire" ITM eat the whole intrinsic where a buy-to-close at 15:29 would have paid premium +
   a small closing cost.
3. **The exit-policy comparison is computed PER-REGIME from the dated cost tables — never a
   constant "always sell before expiry" rule.** Worked consequence (chapters
   [02](02-indian-fno-microstructure.md) / [04](04-gap-analysis.md)): under the Oct-2024
   regime, selling a deep-ITM at 15:29 saves ₹18.75 of STT per ₹75,000 of intrinsic vs
   exercising — but adding trade-level charges (ETC/SEBI/IPFT/GST) + spread, the comparison can
   FLIP; and from 2026-04-01 (both rates 0.15%) the STT asymmetry vanishes so holding becomes
   cheaper for deep-ITM index options — **subject to ONE UNVERIFIED input (G6): whether NSE
   levies transaction charges on EXERCISED (non-traded) contracts at all.** The design pins the
   spec's two registered open questions (regime keying entry-vs-exit date for expiry events;
   the sell-open STT-era note) as DECISIONS in the DEC, not inherited drift. Pre-2024-10-01
   option trades stay counted `COST_REGIME_UNVERIFIED` refusals (`executor.py:184`) — never a
   fabricated rate.

### §6.3 Stock physical settlement — the honest v1 law: REFUSED

Stock options are physically settled for the entire data window (chapter
[02](02-indian-fno-microstructure.md)). Holding a stock-option structure to expiry drags in
per-leg give/take delivery obligations with client-level netting, the expiry-week
delivery-margin ramp (a capital step-function), auction/short-delivery exposure for uncovered
assigned calls, broker forced square-offs, and a delivery-STT split that is UNVERIFIED (G9).
This is a materially bigger build than the index engine. **Law: v1 scope = INDEX underlyings
only** (NIFTY/BANKNIFTY/SENSEX — all cash-settled, matching the engine's existing spot
surface). **Stock-option structures either (i) carry a MANDATORY pre-expiry forced-exit
policy** (the 15:20 forced-exit precedent generalized — e.g. forced close no later than E-1,
before the delivery-margin ramp bites) **OR (ii) REFUSE at structure construction with a typed
reason** (`STRUCTURE_STOCK_PHYSICAL_SETTLE`). **NEVER a silent hold-through-delivery model.** A
second, independent reason stock scope stays refused: the repo has **ZERO corporate-action /
F&O-ban-list layer** (grep-verified; `brutex/engine/sweep_scope.py:28-37`) — a 1:1 bonus reads
as a fabricated −50% overnight cliff, and a backtest opening short strangles in a banned name
trades a fiction.

### §6.4 Prerequisites + wiring

The expiry calendar's holiday shifts must be applied first — a settlement keyed to real expiry
DATES must not simulate settlement on a non-session date (the calendar authority is the shared
NSE calendar; §11). New terminal tokens `expiry_settle` (index) — and `assignment` ONLY if
stock scope ever activates — append to `EXIT_REASONS` under its append-only law and walk the
proven `session_gap` chain end-to-end (enum → `TradeDetail` → aggregate `exits_*` → AGG column
→ top100 exits → display exit-mix). The index-vs-stock nuance is RESOLVED: European + cash
settle ⇒ index structures need `expiry_settle` only; `assignment` is a stock-scope token v1
never emits. The expiry-day margin regime steps belong to §5's channel but are CROSS-REFERENCED
by any "hold to settle" policy on a short structure.

---

## §7. The tail battery — G-TAIL, the centrepiece

**A premium seller is the exact adversarial input for BruteX's existing gate battery.** The
shipped hard gates are mean/ratio-shaped — profit factor ≥ 1.2, MDD ≤ 30%, the combined
win-rate door (`brutex/ranking/__init__.py:81-86`, verified: `MIN_TRADES=30`,
`_MIN_PROFIT_FACTOR=1.2`, `_MAX_DRAWDOWN_PCT=30.0`, `_MIN_WIN_RATE=0.25`,
`_MIN_WIN_LOSS_RATIO=2.0`, `_MAX_CONSECUTIVE_LOSSES=10`). A naked-strangle seller prints 90%+
win rates and a comfortable profit factor with tiny per-trade MAE — right up to the one gap day
that returns many multiples of the accumulated premium. **Every mean-based number the gates
read is flattered by the truncated left tail the backtest happened not to sample.** The tail
battery is the risk-first answer, and its honesty law is blunt: **where a named seller gate is
ABSENT in this checkout, the design says so and treats it as a landing-elsewhere dependency —
it NEVER pretends the gate exists.** (The panel's census: `grep -ri
'cvar|expected.shortfall|tail_gate' brutex/` → 0 files.)

### §7.1 CVaR / Expected-Shortfall gate — ABSENT (the highest-priority NEW seller gate)

**Status: ABSENT** — the only CVaR mention in the repo is an aspirational citation row in the
`DEC-CALIBRATE` methodology table (`docs/MIGRATION.md`); it is computed nowhere. **Mechanism
for a seller trial:** over the persisted per-trade P&L series (`<stem>.trades.parquet`, read by
`resilience_streaks.read_captured_trades`), sort ascending; `CVaR_α = mean of the worst ⌈αN⌉
outcomes` — a pure O(n log n) fold in the exact style of `tail_ratio` (p95/p5,
`brutex/metrics/compute.py:680-683` — verified consumed by NO gate). The gate =
`CVaR_α ≥ −C_max · capital` (capital from the §5 max-loss basis), or a CVaR-to-median-win
ceiling. **Why it is the decisive seller gate:** it is the only scalar family that reads the
*average of the worst α-fraction* of outcomes and therefore **cannot be fooled by win-rate** —
the exact adversarial property the seller's 90%-win distribution attacks. **`tail_ratio`
(p95/p5) is NOT a substitute** — for a seller the p95 win is a small premium capture, so the
ratio degenerates while the sub-p5 tail IS the entire risk. **Landing:** a NEW
cascade/annotation citizen on the per-trade `returns` tuple already carried on
`CandidateInput`; annotation-first (own sidecar), shadow beside the live cascade (the
`cpcv_pass_rate` recording-only precedent, `brutex/metrics/cascade.py:312-328`), promoted to a
rejecting gate only by its own operator-gated DEC.

### §7.2 Per-year stability gate — the annotation EXISTS, the GATE is ABSENT

`win_rate_year_spread` (`brutex/engine/resilience_streaks.py:117` `YEAR_MIN_TRADES = 20`;
`:125` `_MIN_QUALIFYING_YEARS = 2`) + `pnl_by_year_json` (`combo_aggregates.py`) exist and are
explicitly **display-only** (`resilience_streaks.py:10-16`). Premium harvesting is
regime-dependent income — a short-vol book prints near-uniform positive years until the one
year containing the vol event (2020 COVID sits inside the 2020-01-01 floor). A seller whose
whole edge is calm-year income and whose stress-year cells are deeply negative shows a wide
`win_rate_year_spread` + a lopsided `pnl_by_year_json` **today — but nothing rejects it.** The
gate can be specified as a threshold over exactly these existing fields (min qualifying-year
P&L share; a spread ceiling), reusing `YEAR_MIN_TRADES = 20` as the noise floor. **Landing:
fold exists, gate new; promotion consciously supersedes the in-code "ANNOTATION ONLY" stance
via its own DEC.**

### §7.3 Regime must-survive + fragility strata — the LENS EXISTS, the GATE is ABSENT

`one_regime_wonder` at `REGIME_WONDER_SHARE = 0.8` (`brutex/engine/combo_aggregates.py:93` — "a
deliberate display heuristic … never a ranking gate") already answers "is ≥80% of the positive
P&L from one year or one trend regime?" — for sellers this is the **calm-regime wonder
detector**: short premium harvested only in SIDEWAYS regimes dies in trending/gap regimes, and
the trend lens (`TrendRegime` UP/DOWN/SIDEWAYS from the same BOS/CHoCH detector the 60-bit
vocabulary uses; `session_lens.py:38` — "never a ranking gate") labels exactly that stratum.
**The must-survive gate for sellers = requiring NON-CATASTROPHIC P&L in EVERY qualifying
stratum (year × trend × VIX band — all three attributions already exist per combo:
`pnl_by_year_json`, `pnl_by_trend_json`, `vix_band_lo/hi`), instead of merely FLAGGING
dominance.** Fragility strata map onto the existing lens axes: the VIX bands (`VIX_BANDS`
`<12/12-15/15-20/20-25/25-30/>30`, `session_lens.py`) are the canonical seller fragility axis —
**short gamma dies precisely in the `>30` band** — and the honest `unknown`/abstain buckets
(`TREND_UNKNOWN`) carry the no-fabrication law for un-labelable sessions. **Landing: lens
exists, gate absent; promotion must consciously supersede two in-code "never a gate" stances.**

### §7.4 Extreme-event sensitivity — the ATTRIBUTION EXISTS, the SENSITIVITY GATE is ABSENT

Sellers blow up on DAYS, not averages, and the repo already isolates the day classes: the
verified event flags `expiry` (pin/gamma — for a WRITER the single most P&L-concentrated flag),
`muhurat`, `budget_weekend`, `nifty_rebalance` (`brutex/engine/session_lens.py:129-139`); the
worst-5-days stress view (`worst5_days_json`, `WORST_DAYS_LIMIT = 5`,
`combo_aggregates.py:96,178` — the 5 worst sessions WITH DATES; for a seller this IS the tail
in date-addressable form); and the per-event P&L split (`event_day_pnl_json`). **The
sensitivity gate = a ceiling on `(worst-event-day loss ÷ net P&L)` or a per-flag CVaR**,
computable from these existing folds. **HONEST LIMIT the design must carry (verified in-code):
election days and weekday Budget days are UNTAGGED anywhere** — `session_lens.py:26` ("election
days are tagged NOWHERE in the repo and are honestly OMITTED") — so the two most violent
recent Indian vol days (the 2024-06-04 election crash, the 2024-07-23 Budget whip — chapter
[08](08-seller-blowups-and-tail-events.md)) are **invisible to the flag set**. A seller design
must either add a verified event calendar (its own DEC) OR state the blind spot; **this design
states it and refuses to fabricate the calendar.**

**The blow-up-day fixtures a sensitivity gate must reproduce** (the event catalogue + dates are
chapter [08](08-seller-blowups-and-tail-events.md); the reproduction study for expiry-day tails
is chapter [11](11-expiry-day-tail-study/STUDY.md)) — the honest verdict per event class:

| Event class (blow-up days) | Reproducible today? | Fixture / limit |
|---|---|---|
| Overnight gap through strikes (23-Mar-2020, 4-Jun-2024, 5-Aug-2024, 7-Apr-2025) | **PARTIALLY** — the bars carry the gap (2020+), but the engine force-closes at session end (`trade_sim.py:60-65` — `session_gap` is intraday-only), so *held-through* exposure needs a NEW position-lifecycle DEC | golden-day fixture (4-Jun-2024 first) + an engine-scope DEC before it can be end-to-end |
| Intraday IV/VIX explosion pace (5-Aug-2024) | **PARTIALLY** — bars are the pricing truth; the `iv` column exists (`fno_candle_lake.py`); gated on the expired-chain depth probe (B4) | golden fixture + the §7.5 structure-lock test |
| Circuit halt = absent bars + reopen jump (13/23-Mar-2020 45-min halts) | **REPRODUCIBLE** for spot; nothing fabricates halted minutes (`tf_resampler.py` never-fabricates) and nothing flags an intra-session hole (`interior_gap_detect.py` operates on whole SESSIONS) | golden fixture with an EXPLICIT absent-minute-count pin (nothing else guards it) |
| Vol-crush at the open (3-Jun-2024) | **REPRODUCIBLE** (2020+); worst-case fills are conservative in the RIGHT direction | golden fixture |
| Margin expansion / forced deleveraging (COVID, Jun-2024) | **NOT REPRODUCIBLE** — zero verified SPAN rows, refusal-only (`margin_history.py`) | **named ABSTAIN** — §5.3 |
| Pre-2020 catastrophes (Volmageddon 2018, IL&FS 2018, 2019 election) | **NOT** — the pre-2020 floor (`pull_controller.py:163` `GROWW_HISTORY_FLOOR = date(2020,1,1)`) | chaos fixtures asserting *mechanisms* only, never a false historical-replay claim |

### §7.5 The structure-lock test the tail work depends on: the ×4 bad-tick screen must stay OFF for premia

A load-bearing negative finding, panel-verified: `BAD_TICK_MAX_RATIO = 4.0`
(`brutex/data/bad_tick.py:76` — NSE index-circuit-breaker-derived), and at the one place
options meet a screener it is **deliberately disabled for premia with a written rationale**
(`brutex/dashboard/movers_futures.py:905-911`: option PREMIA legitimately move ≥4×/minute on
leverage; the 4× bad-tick threshold is INVALID for premia; the engine never screens options).
**A crash-day deep-OTM ₹2 put printing ₹10 is a REAL price**; routing option bars through the
×4 screen would DELETE genuine blow-up prints — the exact signal the tail battery exists to
read. Any future activation that adds a screen must **structurally exclude the ×4 arm from
option series**, pinned by a permanent structure-lock test. There is no premium-calibrated
outlier screen anywhere in the repo; the insane arm (finiteness/positivity/ordering,
`price_sanity`) IS inheritable, a ×4-class screen for premia is NEW design work, and the
executor's own per-trade rejects (`STALE_BAR` / tick-floor, `executor.py:543-564`) are the
compensating guard the raw reader is otherwise unguarded behind (`lake_source.py` — zero
price-sanity/bad-tick imports, verified).

### §7.6 What the shipped cascade already defends — and what it is BLIND to

The Stage-4 cascade already carries some seller defense (thresholds verified in
`brutex/metrics/cascade.py`): `dsr_min = 0.95` (`:262`), `cpcv_min_pass_rate = 0.5` (`:264`),
`spa_alpha = 0.05` (`:268`), `pbo_max = 0.5` (`:272`), `wfo_min_pass_rate = 0.75` (`:275`),
`mc_alpha = 0.01` / `mc_n_permutations = 1000` (`:284-285`), plus the lockbox window.

- **The DSR already deflates HARDER for sellers**: it uses the candidate's own skew/kurtosis in
  the PSR radicand, so a seller's negatively-skewed leptokurtic per-trade distribution
  mechanically LOWERS its PSR — the `dsr_min = 0.95` bar is already harder for sellers than for
  symmetric strategies.
- **The Monte-Carlo permutation filter is secretly a seller gate**: it answers "is the ENTRY
  signal informative, or could random entries with this same exit logic look just as good?"
  (`brutex/metrics/monte_carlo.py`). For premium selling this is decisive — **theta income
  makes ANY entry look profitable in calm regimes**, so a seller variant that permutes entries
  while preserving the short-premium exit logic directly tests whether the entry filter adds
  anything beyond harvesting the variance-risk premium.
- **But NONE of DSR/PBO/SPA/MC sees a tail the backtest window NEVER SAMPLED** — which is
  exactly why the CVaR gate (§7.1) and the extreme-event sensitivity gate (§7.4) remain the
  genuinely missing seller defenses. This is the sentence the tail battery is built around.

### §7.7 The tail-battery landing summary

Every seller gate lands FIRST as a resilience-style sidecar annotation (compute + display + `—`
abstains, zero run-identity impact), runs in shadow beside the live cascade (recording-only,
the `cpcv_pass_rate` precedent, `cascade.py:312-328`), and is promoted to a rejecting gate only
by its own operator-gated DEC — matching R13 / ranked-never-hidden
(`top100_artifact.py:581-583` — a cascade-FAILED rank-1 stays rank-1, flagged) / the
sweep-verify never-adopt discipline. **No named seller gate is presented as present today.**
The battery order (all future gates): capital-admissibility (§5, a GATE today) → fill-class-A
survival fraction (§4.5) → CVaR_α → per-year stability → regime-must-survive →
extreme-event-sensitivity — each shadow-first.

---

## §8. Cardinality + the staged sweep plan — honest arithmetic, refusal-pruned

### §8.1 The per-axis table and the v1 worked arithmetic

Cardinalities are ARITHMETIC, never measurements — every count below shows its multiplication.
The v1 admissible families (§2.4, G-CAPITAL): **7** (iron condor, iron butterfly, bull put
spread, bear call spread, cash-secured put\*, 0DTE iron fly, degenerate long); **6 refused**
(short straddle, short strangle, index covered call, front-ratio, jade lizard, calendar).

Per-axis, worked for the iron condor (the largest admissible grid), with the honest UNAVAILABLE
axes marked:

| Axis | v1 members | Count | Status |
|---|---|---|---|
| `E-DELTA` short band | {(.05,.10),(.10,.20),(.15,.25),(.25,.35),(.45,.55)} | 5 | swept |
| `E-IVW` wing width (a=0 only) | {w0=2,4,6} | 3 | swept — a>0 UNAVAILABLE (no IV-rank series, §3.3) |
| `E-YIELD` y/W | {0.20, 0.33, 0.40} | 3 | swept |
| DTE band | {21-30, 1-7} | 2 | swept — >30-DTE entries refuse on D-C2 (04 §2.4; expiry−30d pull wall) and are counted, not priced |
| IVR floor | — | 1 | **UNAVAILABLE** — the classic seller vol-regime gate is absent in v1 (§3.3) |
| Entry mask (60-bit) | ONE curated range-regime point per family | 1 | the mask is itself a spot-engine axis; not crossed in the v1 base sweep |
| Management doctrine block | `D-STATIC` control arm (1) + `D-SL-k{1.5,2} × D-TP{50,75} × D-DTE{7,1}` (2×2×2 = 8) + width-consumed q{50,75} (+2) + `D-STOP-TOUCH`{tested,all} (+2) + singles/dedup | ≈ 30 | curated cells; `D-STATIC` is REQUIRED — defended expectancy is meaningless without the undefended baseline on the SAME entries |
| `size_honesty` fill policy | {flag, refuse} | 2 | swept (identity-shaping, §4.5) |
| `spread_penalty_ticks` K | {1,2,4,8} | 4 | **depth-pass only** (§8.2); K=0 is the base |
| `def_penalty_ticks` | default 0 | 1 | default-omitted axis; staged, not crossed in v1 base |

```
entry cells        : 5 × 3 × 3 × 2                  =  90
management cells   : ≈ 30 (curated)
fill policy        : × 2 (size_honesty)
                     ─────────────────────────────
per (family × underlying): 90 × 30 × 2              ≈ 5.4 × 10³ cells   (at K=0)
family×underlying pairs  : 7 admissible × 3 indices = 21
v1 BASE sweep            : 21 × ~5 × 10³            ≈ 1 × 10⁵ cells     (K=0, sizing law D,
                                                                          iv-regime gate absent)
```

### §8.2 The staged robustness re-runs (survivors only — the risk-first economy)

The spread-penalty axis `K ∈ {1,2,4,8}`, the strict `size_honesty=refuse` variant, and the
fill-class-A-only re-score are **NOT crossed into the base sweep** — they are **depth passes on
survivors** (the results-depth / finalist-re-simulation shape — the repo's
cheap-full-sweep → expensive-depth-pass two-tier discipline). On the top-K survivors of the
base sweep, each K-value re-run measures the **breakeven K\*** (§4.6) and the **Class-A
survival fraction** (§4.5) — the two honest headline robustness numbers — at cost
O(K · survivors), not O(K · all cells).

### §8.3 The blind full-cross is REFUSED as a sweep shape (show the number)

A blind cross of all 12 families × the full defense grammar (defense programs = 7 triggers
(chapter [01](01-strategy-taxonomy.md) §f.1) × 9 actions (§f.2) = 63 trigger→action bindings,
compounded over the `max_adjustments` depth — even a shallow 3-adjustment doctrine is 63³ ≈
2.5 × 10⁵) × entry ~10³ × K{5} × sizing{~12} ×
fill-policy{2}:

```
12 × 2.5×10⁵ × 10³ × 5 × 12 × 2  ≈  3.6 × 10¹¹ cells   — REFUSED.
```

Most cells are redundant or incoherent (`D-INVERT` without `D-ROLL-U`), and the v2 leg-mutating
families turn the hot loop into `O(bars × open-structure state)` with per-bar chain lookups —
a shape the spot budget never modeled. **The defense axis ships CURATED + STAGED, never crossed
like the 60-bit mask space.**

### §8.4 Fitting the §8 hardware envelope — staging discipline, and the NO-SLA statement

The staged plan is designed to keep every REAL sweep invocation inside the CLAUDE.md §8
per-run wall-clock discipline (the 2-4-day envelope that governs the spot sweep) by four
levers, in order:

1. **Refusal pruning FIRST** — only the 7-family admissible set sweeps at all (§2.4); the
   G-CAPITAL/G-FILL/G-SETTLE gates prune before any cell is priced.
2. **OFAT / staged invocations** — each non-default axis cell is staged as a SEPARATE bounded
   invocation rather than crossed into one giant run — the `DEC-NIFTY-PRICE-SOURCE-AXIS-001`
   (MIGRATION.md #1681) precedent, where each non-default cell ran as its own cardinality-1
   invocation to keep every run inside the ≤2-day-per-run bar.
3. **Anti-monotone extinction on the mask side** — where the entry-mask axis is ever explored
   (it is fixed at one curated point per family in the v1 base sweep), the exploration rides
   the engine's default Apriori-to-extinction ladder (`DEC-SWEEP-APRIORI-DEFAULT-001`,
   MIGRATION.md #1850) rather than a fixed-k cross.
4. **Survivor-only depth passes** for K / strict-policy / Class-A re-scores (§8.2).

**The honest complexity + the NO-SLA statement.** Per-trial cost is
`O(bars-in-trade × legs)` overlay/premium reads (a v2 adjusted structure would be
`O(bars × open-structure state)`). The per-cell cost constant is **UNVERIFIED (G5)** — an
engineering estimate by analogy to the repo's vectorize-vs-Python precedents, unbenched for
THIS workload. **There is NO options trials/s figure: the spot engine's throughput envelope
(`TRACK1_PERFORMANCE_BUDGET.md:135`) does NOT transfer** — per-leg premium lookups add memory
traffic the spot budget never modeled. **No options-sweep SLA may be written until a prototype
bench over real option 1-minute data + a `TRACK1_PERFORMANCE_BUDGET`-style re-derivation
lands** (the PENDING-ON-BOX recipe: bench the walker over one real month of stored option
bars, then re-derive the budget doc). This is the single largest open feasibility question and
is stated as such. The seller sweep keys to the tuple-store / cell-grid idioms (per-cell
digests, resumable stores) — a "wide-cell" sweep like Stage-3/Stage-4 refinement, NOT the
u64/u128 mask-enumeration path.

---

## §9. Run identity — the additive `canonical_trade_params` keys

Verified precedent: `canonical_trade_params` (`sweep_run.py:719`) is the ONE source of the
run-identity params layout — default-omitted when defaulted (`price_source_params_fragment`,
`:797`), conditionally emitted with presence⟺meaning pinned (`roll_schedule`, `:603,798`),
unconditional where aliasing is dangerous (`fill_mode`), int/string/None only (`:734`). Run
identity is `blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖
data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)` (`combo.py:9-10`).

**Proposed additive keys (ALL default-omitted; the fully-absent state ⇒ a defenseless static
structure, byte-identical to a no-grammar run):** the structure/leg axes (`opt_*`/`sel_*`
prefix), the management axes (`def_sl_bps`, `def_tp_pct`, `def_dte_stop`,
`def_stop_touch_scope`, `def_max_actions`), the fill axes (`spread_penalty_ticks`,
`def_penalty_ticks`), the sizing axes (`sizing_law`, `B_risk_bps`, `K_stress`, `m_floor_bps`,
`H_heat_bps`), and — the risk-first insistence — **the fill-honesty-policy axes that SHAPE the
accepted trade set**:

| Key | Type | Encodes | Why it MUST be in identity |
|---|---|---|---|
| `fill_lambda_x100` | int | the P3 participation multiple λ×100 | gates which trades are Class A ⇒ changes the accepted set (Golden Rule #4) |
| `fill_band_steps` | int | the P4 ATM-band width B | same |
| `size_honesty` | str | `"flag"` \| `"refuse"` | `refuse` DROPS Class-B trades ⇒ a different accepted set |
| `spread_penalty_ticks` | int | K (§4.6) | changes every fill ⇒ different P&L |
| `def_penalty_ticks` | int | the defense-fill penalty (§4.4) | changes forced-close fills |
| `sizing_law` + params | str/int | D/N/S + B_risk/K/m_floor (§5.4) | changes lots ⇒ different sized P&L |

**The fill-honesty class COUNTERS (`n_class_a/b/c`, `defense_deferred_minutes`, the
`settlement_proxy` band, `margin_proxy`) are OUTPUTS and stay OUT of identity** — they are what
the sweep reports, not what defines it. The REFERENCE_ONLY annotations (the VIX trio,
`margin_proxy`, spiral bounds) stay out of identity by the VIX-trio discipline
(`DEC-VIX-STAMP-WIRE-001`). A `def_grammar_rev` int self-describes the
priority-ladder/re-arm semantic revision across commits (the `roll_schedule` self-description
argument), with `code_commit_hash` carrying the actual code semantics. Presence-coherence
invariants (`def_invert_steps` ⇒ `def_roll_*` present) live in the config `__post_init__`,
mirroring the instrument⟺roll_schedule invariant (`sweep_run.py:603`).

---

## §10. Pipeline reuse, gate by gate

The seller sweep rides the EXISTING pipeline end-to-end; the full row-by-row inventory is
chapter [03](03-brutex-inventory-reuse-map.md) — this section names each pipeline gate and how
the structure track consumes it:

| Pipeline stage / gate | Reuse verdict | How the structure track consumes it |
|---|---|---|
| Tuple digest / dedup / resume | **VERBATIM idiom** | the seller sweep keys to the per-cell digest + resumable tuple-store idioms (the `_tuple_digest` address law — a structure cell's config folds into its digest exactly as gate/selection knobs do today); a killed run resumes from intact stores |
| Entry-mask evaluation + count kernels | **VERBATIM** | the 60-bit mask is evaluated and counted by the shipped u64/u128 kernels with zero change (§3.1); the option layer consumes the mask verdict per bar, never re-implements it |
| The hard ranking doors | **EXTEND (recalibrate; own DEC)** | the shipped doors (`ranking/__init__.py:81-86`) run unchanged in v1; seller-shaped recalibration (a 90%-win seller trivially passes the win-rate door) is a later gate DEC — never a silent threshold edit |
| Stage-1 → Stage-4 cascade | **VERBATIM** | DSR (skew/kurtosis-aware — already harder for sellers), CPCV, SPA, PBO, WFO, the MC permutation filter and the lockbox all run unchanged over the structure track's per-trade returns (§7.6) |
| The tail gates | **NEW — shadow-first** | CVaR / per-year / must-survive / event-sensitivity land annotation-first beside the cascade (§7.7); promotion is per-gate DEC |
| Results store / additive columns | **EXTEND under the additive law** | new nullable columns + counters ride the optional-column back-compat template; old rows load `None`, never a fabricated 0 |
| top100 / /results / /screener surfaces | **VERBATIM discipline** | seller analytics render as annotations under R13 / ranked-never-hidden (`top100_artifact.py:581-583`); a tail-flagged rank-1 stays rank-1, red-flagged, never hidden |
| `sweep-verify --recount` | **VERBATIM discipline** | recount re-derives structure cells exactly like the launcher and never adopts (the never-adopt family); VIX-style divergence attribution extends to the new sidecar columns |
| Translation executors | **EXTEND** | the options executor contributes the reject vocabulary + per-leg cost composition + tick-floor/stale-bar laws (§0 invariant 1, §4.1); the futures executor contributes the `net == gross − charges` invariant (`futures/executor.py:422-424`) and the R13 annotation posture (`:14-16`); the structure walker composes N single-leg breakdowns, never a second cost copy |
| Cost engine | **VERBATIM (dated tables)** | every leg is costed from the dated VERIFIED tables; pre-2024-10-01 option windows refuse `COST_REGIME_UNVERIFIED` (`executor.py:184`, `costs/constants.py:126-136`) |

---

## §11. Scenario-apparatus carry-over

The spot engine's scenario apparatus carries over to the structure track — each row names the
apparatus, its law, and the seller-specific consequence:

- **Muhurat + calendar authority.** The shared NSE calendar is the single session authority;
  the Stage-1A Muhurat-safe prev-day anchor law (`DEC-SWEEP-DATA-EDGE-GUARDS-001`) keeps a
  Muhurat evening session from poisoning the next regular day's anchors, and the seller
  settlement engine additionally applies the expiry-holiday-shift law (§6.4) so a settlement is
  never simulated on a non-session date. A Muhurat session's option bars are real stored bars;
  the walker treats them under the same session laws as spot.
- **The bad-tick screen.** The spot engine's ×4 screen (`bad_tick.py:76`) must be
  **structurally excluded from option series** — the §7.5 structure-lock: premia legitimately
  move ≥4×/minute, and screening them would delete the tail the battery exists to read. The
  inheritable arm is price-sanity (finiteness/positivity/ordering) only.
- **The session-gap guard.** The spot walker's open-position session-boundary law
  (`session_gap`, `trade_sim.py:60-65`; `DEC-TRADESIM-OPEN-SESSION-GUARD-001`) carries over
  as the seller twin `defense_session_gap` (§4.4 step 3): an open structure never silently
  rides a session boundary — a forced close defers under the counted defer law, with interim
  MTM at last-known close and a `stale_mark_minutes` counter. Held-overnight seller exposure
  remains a NEW position-lifecycle DEC (§13 item 6).
- **Completeness gates.** The pull-side completeness machinery (the today-completeness gate,
  interior-gap/truncation demotions) guards the 1-minute substrate the walker reads; the
  seller-specific hole is CHAIN completeness — no per-expiry strike-coverage census exists, so
  a genuinely-missing liquid strike is unattested absence (§13 item 12). The
  chain-completeness census is its own DEC (the building blocks exist); until it lands,
  multi-leg statistics carry an unquantified survivorship bias and say so.
- **Suffix-independence + look-ahead.** The walker must land under the same two disciplines
  the spot engine carries: the forward-only consumption law (`trade_sim.py:75-78` — the defer
  law consumes only bars ≥ the decision instant, §4.4) and a suffix-independence property gate
  (trades over bars `0..N` bit-identical whether bars `N+1..` are absent, mutated, or extreme —
  the `DEC-SWEEP-LOOKAHEAD-GUARD-001` property-gate shape, applied to the structure walker as
  its own permanent suite; the module contracts are chapter
  [07](07-verification-and-phasing.md) §0 M3).

---

## §12. Default-OFF activation — the three-gate DEC ladder + shadow phasing

The capability ships **DEFAULT-OFF / INERT** and activates through a strictly-phased,
operator-gated ladder — three DISJOINT gates the design keeps separate on purpose:

1. **`DEC-OPTSELL-STRUCTURE-EXECUTOR-001` (MIGRATION.md #2211 — the capability slice, its own
   PR).** Supersedes `DEC-EXEC-008` for the structure track; ships the grammar, the multi-leg
   walker, the per-leg SELL fill + defense-fill + defer laws, the fill-honesty classes, the V1
   max-loss capital gate, the settlement proxy, the counters — all **DEFAULT-OFF**, vocabulary
   **unchanged at 60 bits**, engine scope **unchanged (spot-only)**. Every seller analytic
   lands as a display-only annotation (the resilience-streaks sidecar template) — compute +
   display + `—` abstains, ZERO run-identity impact.
2. **`DEC-SPAN-HISTORY-INGEST-001` (proposed — unlocks the undefined-risk families).** Ingests
   dated SPAN rows; V1 refusal windows convert date-by-date; the naked families become
   capital-admissible only where verified rows exist (§5.5). Additive; DATA-gated; carries the
   "archive-shallow ⇒ permanent refusal for the past" branch.
3. **`DEC-OPTSELL-TAIL-GATES-00N` (promotes annotations to rejecting gates) + the separate
   `DEC-OPTSELL-VOCAB-EXTEND-00N` (60→N bits) + the separate options-into-engine-scope DEC.**
   Each named seller gate (§7) is promoted individually, each consciously superseding an
   in-code "never a ranking gate" stance; the vocabulary extension and the scope expansion are
   further, independent DECs (Golden Rule #3).

**Shadow phasing (the never-adopt discipline).** Before any gate rejects, it runs SHADOW
beside the live cascade — the `sweep-verify --recount` re-execute + byte-diff, adopt-nothing
pattern + the `cpcv_pass_rate` recording-only precedent (`cascade.py:312-328`) + the R45
diffcheck stance (mark SUSPECT on mismatch, NEVER replace the production value). **An
annotation is promoted to a rejecting gate only after N recorded-only runs and its own operator
ruling.** The full DEC-by-DEC verification + phasing ladder — including the module-by-module
test pyramid this design must pass — is chapter [07](07-verification-and-phasing.md). The
whole ladder is reversible: a later DEC can flip any ruling; nothing here is load-bearing
beyond the capability slice.

---

## §13. What cannot be claimed — the aggressive honesty ledger

Every item is a claim a seller backtest is tempted to make and **must not**:

1. **NOT "margin-realistic."** No margin number, ROI, or margin-based size may rank, report as
   a return, or enter run identity for any structure until dated SPAN rows land —
   undefined-risk families are REFUSED entirely in v1 (`margin_history.py`, §5). The
   drawdown-to-margin spiral is **REFUSED as a simulation** — a "margin call on date D" is a
   fabricated number chain (§5.3).
2. **NOT "size-realistic."** The per-leg worst-case bound holds for a MARGINAL order only
   unless a trade is Class A, and even Class A does NOT bound market impact (λ is
   uncalibratable) — every result surface must render the **Class-A survival fraction** and
   never claim size realism (§4.5).
3. **NOT "quote-complete" / NOT "spread-costed."** The lake stores TRADED PRINTS only — no
   bid/ask, no depth, ever, for any instrument or date. The spread cost of every option
   strategy is structurally invisible; the spread-penalty axis measures FRAGILITY, never cost,
   and the 2020→now window is PERMANENTLY uncalibratable for spreads (§4.6). Where the quoted
   spread sat outside a bar's traded range (illiquid strikes, expiry afternoon), even the
   worst-case bound can be optimistic — the one corner where "never more optimistic than
   reality" is not guaranteed by this data class (§4.5 residual).
4. **NOT "settle-exact."** Index cash settlement uses a PROXY of the official ~30-min FSP
   construct with a DISCLOSED error band; the band IS the pin-risk model, not a precise settle
   (§6.1). The exact NSE formula is UNVERIFIED (G7).
5. **NOT "stock-option-capable" in v1.** Physical settlement + the zero
   corporate-action/ban-list layer ⇒ stock scope is REFUSED (forced pre-expiry exit or typed
   reject), never a silent hold-through-delivery (§6.3).
6. **NOT "held-through-the-gap."** The engine force-closes intraday (`session_gap`,
   `trade_sim.py:60-65`); the overnight-gap blow-up class loses its teeth in an intraday-only
   engine. Held-overnight seller exposure is a NEW position-lifecycle DEC — a v1 seller sweep
   is intraday-only and SAYS SO on every result surface (§7.4).
7. **NOT "vol-regime-timed" in v1.** The classic "sell when IV-rank is high" entry has NO
   substrate (no IV-rank series; VIX barred by Rule 3); families whose edge is vol-regime
   timing are honestly under-served (§3.3).
8. **NOT "tail-gated" today.** CVaR/ES, per-year stability, regime-must-survive, and
   extreme-event sensitivity are ABSENT as gates in this checkout — landing-elsewhere
   dependencies, shadow-first, and the design NEVER pretends they exist (§7).
9. **NOT a historical replay of pre-2020 catastrophes.** Volmageddon-2018 / IL&FS-2018 / the
   2019 election are structurally out of reach (the 2020 floor); only chaos-fixture
   mechanism-replays, with no false claim of historical fidelity (§7.4).
10. **NOT an SLA.** No options trials/s figure exists; no wall-clock claim until the prototype
    bench (§8.4).
11. **NOT "cost-complete" pre-2024-10-01.** Option ETC/STT rows are VERIFIED only from
    2024-10-01; pre-boundary trades are counted `COST_REGIME_UNVERIFIED` refusals, never a
    fabricated rate (`executor.py:184`, §6.2).
12. **NOT "chain-complete."** No per-expiry strike-coverage census exists — a genuinely-missing
    liquid strike is unattested absence, so multi-leg selection carries an unquantified
    survivorship bias; the design requires a chain-completeness census (its own DEC, the
    building blocks exist) before any multi-leg family's statistics are read as unbiased.

---

## §14. Rulings where the evidence base disagrees (ruled risk-first; each reversible)

| # | Seam | The two honest choices | Ruling + reason | Reversibility |
|---|---|---|---|---|
| R-1 | Cash-secured put capital basis | `max_loss` (exact, large understatement vs SPAN) vs hold with the naked families | **`max_loss` with the understatement DISCLOSED** — a finite exact bound is admissible; the conservative-bias understatement is the same side the engine already chooses | a later DEC can move it to the SPAN channel |
| R-2 | Class-B (bounded-price, unbounded-size) acceptance | `flag` (observe) vs `refuse` (drop) | **`flag` is the v1 default; `refuse` is a swept identity-shaping variant** — observe-first, but both are carried so the Class-A survival fraction is always measurable | an axis; always both |
| R-3 | Settle-proxy source | last stored close vs a last-30-min VWAP-like fold | **the last-30-min VWAP-like fold** (nearer the official construct) with an error band — a DEC decision to pin explicitly, never inherit | v2 = ingest real FSPs |
| R-4 | 0DTE straddle/strangle | admit (naked, refuse capital) vs only the fly | **only the fly is v1-admissible** — the naked 0DTE variants refuse on capital exactly like their non-0DTE parents | unlocks with the SPAN DEC |
| R-5 | Defense on a no-print bar | reject (the current executor default) vs defer-to-next-volume>0 | **defer, NEVER reject** — a reject silently holds the undefended short for free, the anti-conservative inversion (§4.4) | none — a correctness law |
| R-6 | v2 leg-mutating adjustments | include in v1 vs stage to v2 | **stage to v2** (finalist-depth / pruned-space only) — the cardinality + `O(bars×state)` cost forbid a full cross (§8.3) | a v2 DEC |

---

## §15. Consolidated UNVERIFIED register (compact; the deliverable-wide register is chapter [07](07-verification-and-phasing.md))

| id | Item | Closer (the recipe, never a number) |
|---|---|---|
| U1 | Groww 1m candles are OHLC-of-TRADES (the §4.1 bound depends on it); volume-0 bars are carried prints | vendor doc / credentialed probe |
| G1 | CSP max-loss multiple vs real SPAN (the §5.1 understatement magnitude) | a real SPAN calc vs strike×lot (chapter [09](09-capital-margin-and-sizing.md)) |
| G2 | NSE SPAN-file public-archive retention depth 2020→today (§5.5) | probe the archive |
| G3 | Exchange daily-FSP historical dataset availability (§6.1 v2) | NSE Clearing settlement-price archive probe |
| G5 | Options-structure trials/s + the per-leg-bar cost constant (the §8.4 no-SLA) | a prototype bench on real option 1-minute data (PENDING-ON-BOX) |
| G6 | Whether NSE levies ETC/SEBI/IPFT on EXERCISED contracts (flips the §6.2 verdict) | a real exercised-position contract note / NSE circular |
| G7 | The exact index FSP formula (sets the §6.1 error-band width) | NSE Clearing settlement page + contract spec |
| G8 | Production-lake Greeks-overlay coverage fraction (gates §2.2 E-DELTA/E-EM) | `brutex greeks-backfill --dry-run` census (PENDING-ON-BOX) |
| G9 | The stock delivery-STT split (§6.3) | verified circular; moot while stock scope refuses |
| G11 | Put-calendar max-loss > net debit (the §2.4 EU-put exclusion mechanism) | a worked bound at prevailing rates (chapter [04](04-gap-analysis.md)) |
| G12 | Per-leg-worst vs package-worst gap magnitude (§4.3) | unmeasurable at 1m granularity — permanent, direction-conservative |
| U3/U4 | Indian option quoted-spread magnitudes (all §4.4/§4.6 spread claims) | forward L1 capture only (chapter [10](10-seller-edge-and-execution-evidence.md)) |
| B4 | Groww serves 2020-era expired option chains (the §7.4 blow-up-day fixtures) | credentialed `get_expiries` depth probe (PENDING-ON-BOX) |
| U-D5 | NAKED-short SPAN+ELM magnitudes (the §5 refusal's motivation) | the SPAN-archive feed + its DEC |
| election-cal | Election / weekday-Budget days untagged (the §7.4 blind spot; `session_lens.py:26`) | a verified event calendar, its own DEC |

---

**This chapter is a design deliverable only — no `brutex/` file is modified by it, and nothing
in it activates anything. The engine stays SPOT-ONLY (`DEC-ENGINE-SPOT-ONLY-001`) until an
operator-gated SELL DEC/ADR adopts these laws — and even then, the capability ships
DEFAULT-OFF, shadow-first, under `DEC-OPTSELL-STRUCTURE-EXECUTOR-001` (MIGRATION.md #2211).
Sibling chapters: [01](01-strategy-taxonomy.md) taxonomy ·
[02](02-indian-fno-microstructure.md) microstructure · [03](03-brutex-inventory-reuse-map.md)
inventory/reuse · [04](04-gap-analysis.md) gaps · [06](06-edge-case-register.md) edge cases ·
[07](07-verification-and-phasing.md) verification & phasing ·
[08](08-seller-blowups-and-tail-events.md) blow-ups · [09](09-capital-margin-and-sizing.md)
capital/margin · [10](10-seller-edge-and-execution-evidence.md) edge/execution evidence ·
[11](11-expiry-day-tail-study/STUDY.md) expiry-day tail study.**
