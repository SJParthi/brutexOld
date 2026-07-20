# 05 — The Option-Selling Engine Design: making short premium a first-class brute-force citizen

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code ships in this doc.** The sweep engine remains **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001, MIGRATION.md #1790) and options remain **REFERENCE_ONLY** lake data (CLAUDE.md §2 Rule 3). The capability described here ships **DEFAULT-OFF / INERT**, and its **activation** — plus **any entry of options into the engine's brute-force sweep-scope or into the 60-condition vocabulary** — are **SEPARATE future operator-gated DECs** (Golden Rule #3). Nothing in this document authorizes activation, a scope change, or a vocabulary change.
>
> **Date:** 2026-07-20
> **Provenance:** multi-agent design panel, docs-only branch `claude/options-strategies-design`, part of **DEC-OPTION-SELLING-RESEARCH-001 (#2210)**. The future default-OFF code slice this design specs is **DEC-OPTSELL-STRUCTURE-EXECUTOR-001 (#2211, its own PR)**.
>
> **Evidence discipline.** Every repo claim is `path:line` verified by reading the file at that line in this checkout. Market facts cite the research section files under `scratchpad/sections/` (each of which carries its own web/UNVERIFIED tags) or the committed chapters `01`/`02`/`03`/`06`/`08`/`09`/`10`/`11`, or are tagged UNVERIFIED and collected in the consolidated register (§11). Cardinality arithmetic shows the multiplication (Golden Rule #10). No number is presented as `guaranteed`/`fully`/`100%`; where a family cannot be honestly priced or sized it **refuses** — the repo's own house law.
>
> **Judge's synthesis note.** This chapter is the definitive integration-first engine design. It takes the reuse-maximal spine of the inventory map (chapter `03`), grafts the honesty / tail-gate / margin-truth rigor of the gates + gaps evidence (`gates-evidence.md`, `gap-sim.md`, `gap-fill.md`, `sell-core.md`), and adopts the real cardinality + staged sweep plan of the taxonomy DSL (chapter `01` §h). Where the panel's evidence base documents two honest design choices for the same seam, the disagreement is stated **and ruled** at the point it arises (rulings are collected in §10.4). Every ruling names its reason and its reversibility (a later DEC can flip it — nothing here is load-bearing beyond #2211).

---

## §0. The one-sentence engine and the four laws it must never break

**The engine.** A selling structure is *(a spot-side entry mask over the existing 60-bit vocabulary, reused verbatim)* × *(an option-layer structure selector — legs, strikes, expiry tiers)* × *(a management/exit law over the joined per-minute premium path)* — simulated by a multi-leg sibling of `trade_sim.py`, priced per leg at the shipped worst-case fill law, gated by the shipped Stage-4 cascade **plus** a new seller-centric tail battery, stored under the shipped additive-column and tuple-digest laws, and shown behind the shipped annotation-never-reranks discipline. **Roughly half of that sentence is code that already exists and is consumed unchanged** (chapter `03` §1.7: across 99 deduplicated reuse-matrix rows, ~half VERBATIM, ~a quarter EXTEND under documented laws, ~a quarter NEW-NEEDED concentrated in four clusters).

**The four laws the design cannot break** (each is an existing repo invariant, not a new promise):

1. **Correct-or-absent** — a structure that cannot be priced, sized, or settled honestly **refuses atomically and is counted**, never simulated approximately. (The `RejectReason` + count-consistency discipline, `brutex/options/executor.py:155-184`, `:602-712` — verified: the enum members `NO_DATA`/`TICK_FLOOR`/`LOT_SIZE_UNKNOWN`/`STALE_BAR`/`COST_REGIME_UNVERIFIED` at `:161-184`.)
2. **Worst-case only** — every fill takes the adverse intra-bar extreme; the product-wide fill law admits no other mode. (`DEC-FILL-WORST-CASE-ONLY-001`, verified across `brutex/engine/trade_sim.py:28,197,613-798`.)
3. **Additive identity** — new run-identity keys are **default-omitted** so every existing spot `run_id` stays byte-identical, and anti-aliasing keys (fill mode) are **unconditional**. (`canonical_trade_params`, `brutex/engine/sweep_run.py:719-809` — verified: `roll_schedule` conditional at `:798-799`, `fill_mode = FillMode.WORST_CASE.value` unconditional at `:809`.)
4. **Never a ranking gate without its own DEC** — every seller analytic lands first as a display-only annotation (its own sidecar, its own fence, `—` abstains, zero run-identity impact); promotion to a *rejecting* gate is a separate operator-gated DEC. (`resilience_streaks.py:10-16` "ANNOTATION / DISPLAY ONLY. Never re-ranks / filters / rejects"; the R13 / ranked-never-hidden / sweep-verify-never-adopt family, `gates-evidence.md` §4.)

Everything below is an application of those four laws to short premium.

---

## §1. THE STRUCTURE GRAMMAR

The grammar is the whole scope surface of #2211. It is a **strict superset** of today's single-leg addressing (`brutex/options/executor.py` is `BUY`-only, one leg per trade — `executor.py:38-41` verified: *"Track 2 only ever BUYS options … never writes / shorts. Max loss = premium paid; no SPAN/ELM margin modelling"*, DEC-EXEC-008). #2211 supersedes DEC-EXEC-008 for the structure track only; the single-leg long executor stays a valid degenerate case (a 1-leg long structure).

### §1.1 The atom: `StructureLeg`

```
StructureLeg := { side, type, strike_selector, expiry_tier, ratio }

  side           ∈ { long, short }          # long = buy-to-open;  short = sell-to-open
  type           ∈ { CE, PE, FUT }          # FUT = the index futures leg (covered-call/collar bodies only)
  strike_selector ∈ the E-* families (§1.3) # resolved to a Decimal strike at the entry bar
  expiry_tier    ∈ { W0,W1,W2, M0,M1,M2 }   # near/next/far × weekly/monthly (§1.4)
  ratio          ∈ ℕ⁺                        # lots of THIS leg (ratio writes, 1×2 etc.)
```

This is the leg tuple of chapter `01` §h.3 (`D3 = (side ∈ {LONG,SHORT}, type ∈ {CE,PE,FUT}, strike_selector, expiry_tier, ratio ∈ ℕ⁺)`, `01-strategy-taxonomy.md:2541`). `side`/`type` map onto the existing `strike_rules.TradeDirection` + CE/PE; `strike_selector` onto the 10 locked strike rules + the E-* extensions; `expiry_tier` onto the movers tier machinery; `ratio` is the one genuinely NEW leg field.

### §1.2 The molecule: `OptionStructure`

```
OptionStructure := {
   family        : token  # named seller family (short_straddle, iron_condor, jade_lizard, …) — §1.6
   legs          : ordered tuple[StructureLeg]  # 1..4 legs in v1 (5+ = v2)
   entry_instant : one decided bar for ALL legs
   exit_policy   : the management/exit law (§1.5)
   settlement_basis : token (§8)              # how a held-to-expiry terminal resolves
}
```

**Construction invariant (correct-or-absent at structure grain).** All legs share **one** decided entry bar instant and **one** decided exit instant (`gap-sim.md` §b closure sketch step 1). The structure roll-up enforces `structure_net == Σ leg_nets` at construction — the exact `MaterializedFuturesTrade` `net == gross − charges` invariant precedent (`brutex/futures/executor.py:422-424` per chapter `03` §1.5 R-row). Per-leg pricing composes N existing single-leg breakdowns (the calculator is "pure composition per leg" — `brutex/options/executor.py:18-28`). **v1 cap: 1–4 legs, all same underlying** (index only — §8.3); 5+-leg and cross-underlying structures refuse at construction with a typed reason (`STRUCTURE_TOO_MANY_LEGS`, `STRUCTURE_CROSS_UNDERLYING` — names indicative).

### §1.3 Strike-selector families (`strike_selector`)

Each is a pure function `(entry_bar, side, type) → Decimal strike | REFUSE`, evaluable from stored data with the shipped moneyness machinery (`brutex/data/moneyness.py`: `classify` `:68`, `derive_strike_step` `:106`, `resolve_strike_step` `:181`, `atm_offset` `:238`, `moneyness_view` `:314` — all verified, O(1), never raise). The seed grids are from `sell-core.md` §0.6:

| Selector | Rule template | Seed grid | Data source (verified) |
|---|---|---|---|
| `E-ATMOFF(n)` | strike = ATM ± n grid steps | n ∈ {0,1,2,3,5,8,10,15,20} | `atm_offset`/`derive_strike_step` `moneyness.py:238,106` |
| `E-DELTA(δlo,δhi)` | strike whose stored \|Δ\| ∈ [δlo,δhi] at entry bar | {(.05,.10),(.10,.20),(.15,.25),(.25,.35),(.45,.55=ATM)} | stored `delta` column, `brutex/data/models.py:158` |
| `E-EM(m)` | strike = S ± m·EM, EM = S·IV·√(DTE/365), rounded to grid | m ∈ {0.5,0.75,1.0,1.25,1.5,2.0} | stored `iv` `models.py:157` + step |
| `E-IVW(w0,a)` | wing width = round(w0·(1+a·IVR)) steps | w0 ∈ {2,4,6}; a ∈ {0,0.5,1.0} | derived IVR (**UNVERIFIED U-4**: no stored IV-rank column today) |
| `E-YIELD(y)` | net credit ≥ y of denominator (notional in v1) | y ∈ {0.5%,1%,2%} of `S·lot` (margin denominator BLOCKED — §7) | entry-bar premiums |
| level-anchored | strike pinned to a spot-mask level (pivot/PDH-PDL/fib) | — | the entry-mask bits themselves (§2) |

**Honest limit (E-DELTA / E-EM coverage).** `E-DELTA` and `E-EM` read the stored Greeks overlay; on bars where the overlay is NULL (the 7-status refusal ladder `NO_RATE/AT_EXPIRY/NO_SPOT/BAD_INPUT/BELOW_INTRINSIC/NO_SOLUTION`, `brutex/data/greeks_enrich.py:979-1080` per chapter `03` §1.2 G7) the selector cannot resolve and the structure refuses. Known structural holes: the last ~600 s before expiry is `AT_EXPIRY` NULL by design (so a delta-selected 0DTE strike is unresolvable in the final ~10 minutes — `gap-sim.md` §c.1), and deep-ITM zero-time-value strikes are `BELOW_INTRINSIC` NULL. Production-lake overlay coverage fraction is **UNVERIFIED (G8)** — closable by `brutex greeks-backfill --dry-run` census. `E-ATMOFF` and level-anchored selectors need no overlay and are the coverage-robust fallback.

### §1.4 Expiry-tier tokens (`expiry_tier`)

Tiers reuse the movers tier law verbatim: `_EXPIRY_TIERS = {near:0, next:1, far:2}` × tracks `{all, weekly, monthly}`, `monthly` = last-in-calendar-month law (`brutex/dashboard/movers_options.py:204,216,956` per chapter `01` §h G5). Tokens: `W0/W1/W2` = near/next/far weekly, `M0/M1/M2` = near/next/far monthly. The **authoritative** expiry + strike ladder is vendor DATA via `RegistryStore.list_expiries` / `list_contracts` (`brutex/data/registry_store.py:644,671` per `sell-core.md` §0.2) — never a computed last-Thursday formula. **Honest limits:** (i) stock options are **monthly-only** in India (weekly stock options do not exist — `sell-core.md` §7(b), `[EXT: NSE product list]`), so `W*` tokens refuse on a stock underlying; (ii) since 2024-11-20 there is only **one benchmark weekly per exchange** (NIFTY on NSE Tue, SENSEX on BSE Thu — `sell-core.md` §12, `[EXT: SEBI 2024/132]`, exact uniform-expiry circular **UNVERIFIED U-12**), so the weekly-track selection space narrowed after that date. **v1 scope is index-only (§8.3), so the stock-monthly-only limit is documented but not exercised.**

### §1.5 The management + exit law (`exit_policy`)

The exit policy is a per-1-minute-bar state machine over the **structure premium path** = Σ signed leg closes (an O(legs) join over the per-contract 1-minute frames — the exact read the movers-options screen already performs per contract, `lake_source.load_pair` per leg — `sell-core.md` §0.7). The v1 grammar is **management-of-a-static-structure only** (no mid-trade leg mutation — §1.5.3):

| ID | Trigger (per-bar, from stored data) | Action | Seed grid |
|---|---|---|---|
| `D-TP(p)` | structure MTM profit ≥ p% of net credit `C` (`PREMIUM_DECAY(p)`, `01:2447`) | close all legs | p ∈ {25,50,75} |
| `D-SL-k(k)` | structure MTM loss ≥ k·C (debit-to-close ≥ (1+k)·C) (`PREMIUM_MULT(1+k)`, `01:2446`) | close all | k ∈ {1.5,2,3} |
| `D-DTE(d)` | DTE ≤ d, or holding ≥ H sessions | close (or v2: roll out) | d ∈ {21,14,7,1} |
| `D-STOP-TOUCH` | spot touches a short strike (moneyness flips OTM→ATM/ITM via `classify`) | close tested side / all | side ∈ {tested, all} |
| **hard 15:20** | expiry-day forced exit | close all | mandatory (reuses CLAUDE.md §7 forced-exit) |

The **hard 15:20 IST forced exit is reused verbatim** — the spot walker already forces exit at 15:20 (CLAUDE.md §7; `trade_sim.py` forced-exit path). A seller structure **never holds a leg into settlement** unless its `settlement_basis` explicitly opts in (§8), and even then only for index cash-settled underlyings.

**§1.5.3 The adjustment grammar (`D-ROLL-U`, `D-RECENTER`, `D-WING`, `D-INVERT`) is v2.** These mid-trade **leg-mutating** defenses (roll the untested side, delta-band re-center, add a wing, invert a strangle — `sell-core.md` §0.7) are mechanically feasible from stored data (`gap-sim.md` §f: re-quoting a leg needs only per-bar premiums the lake already stores + per-bar delta where the overlay solved + the moneyness machinery for re-centering) but are deliberately **excluded from v1** for two reasons the estimator makes concrete (§5): each is a new sweep axis (trigger × action × parameter) that multiplies the trial count, and each turns the hot loop into `O(bars × open-structure state)` with per-bar chain lookups — a shape the spot budget never modeled (`gap-sim.md` §f.2). **v1 = static structures with a managed exit policy; v2 = adjustments as a small enumerated axis on RESTRICTED spaces** (finalist-depth re-simulation or a stage-2-pruned space — never the full mask × structure × adjustment cross-product). This mirrors the repo's existing cheap-full-sweep → expensive-depth-pass two-tier discipline.

### §1.6 The family → encoding table (the sweep seed)

Every named seller family is one `StructureSpec` template with a constrained parameter grid. Leg-encoding DSL: `S`=short, `B`=long; `@sel` = strike selector; `/T{token}` = expiry tier; `×r` = ratio. `UND` = undefined risk. This is the `sell-core.md` §(f) table, cross-checked against the taxonomy chapter `01` §h.4 rows and stamped with the **v1 capital admissibility** (§7) and the **defined-risk?** column (`gap-sim.md` §a family table):

| # | Family | Legs `{side,type,sel,tier,ratio}` | Defined risk? | v1 capital-admissible? | Entry-law family | Management-law family |
|---|---|---|---|---|---|---|
| 1 | Short straddle | `S:CE@ATMOFF(0)/W0 + S:PE@ATMOFF(0)/W0` | UND | **NO — v1 refuse** (naked; §7.4) | E-ATMOFF(0)·IVR-floor·DTE{0,1-2,21-45}·event-avoid | D-SL-k{1.5,2,3}·D-TP{25,50}·D-DTE·(D-RECENTER v2) |
| 2 | Short strangle | `S:CE@Δ(.10-.20)/W0 + S:PE@Δ(.10-.20)/W0` | UND | **NO — v1 refuse** | E-DELTA/E-EM(m≈1)·IVR-floor·DTE{21-45 or 1-7} | D-SL-k{2,3}·D-TP{50}·D-DTE·(D-ROLL-U/D-INVERT v2) |
| 3 | Iron condor | `S:PE@Δ(.10-.20)+B:PE@−W_p+S:CE@Δ(.10-.20)+B:CE@+W_c /W0` | **DEF** `max(W)−C` | **YES** | E-DELTA+E-IVW·E-YIELD y/W{.2,.33,.4}·DTE{21-45,1-7} | D-TP{50,75}·D-SL-k{1.5,2}·width-consumed q{50,75}·D-DTE |
| 4 | Iron butterfly | `S:CE@ATMOFF(0)+S:PE@ATMOFF(0)+B:PE@−W+B:CE@+W /W0` | **DEF** `W−C` | **YES** | E-ATMOFF(0)·E-EM wings·C/W{.5,.6,.75}·DTE{0,1-2,21-45} | D-TP{25,50}·D-SL-k{1.5,2}·D-DTE·(D-RECENTER v2) |
| 5 | Bull put spread | `S:PE@Δ(.15-.35)/T + B:PE@−W/T` | **DEF** `W−C` | **YES** | E-DELTA·support bits(pivot/PDL/fib)·bull mask·y/W{.25,.33} | D-TP{50,75}·D-SL-k{1.5,2}·tested-Δ≥.40 close |
| 6 | Bear call spread | `S:CE@Δ(.15-.35)/T + B:CE@+W/T` | **DEF** `W−C` | **YES** | E-DELTA·resistance bits(pivot/PDH/fib-ext)·bear mask | mirror of #5; k{1.5,2} tighter (gap risk) |
| 7a | Covered call (index) | `B:FUT/M0 ×1 + S:CE@Δ(.20-.35)/T ×1` | UND (down) | **NO — v1 refuse** (naked-class downside) | E-DELTA/E-ATMOFF{1-5}·mild-bull mask | roll-up-out@Δ≥.60 (v2)·D-TP{50,75}(call leg)·mask-exit |
| 8 | Cash-secured put | `S:PE@Δ(.15-.30)/T ×1 + reserve K·lot` | **FIN-large** `K−C` | **YES — max_loss basis, understatement disclosed** (§7.5, RULING R-1) | E-DELTA·yield-on-reserve r_min{6,10,15}%p.a.·support bits | D-TP{50,75}·roll-down-out(v2)·accept-delivery→7b(stock v2) |
| 9 | Front-ratio (credit) | `B:CE@ATMOFF(0-2) ×1 + S:CE@Δ(.15-.30) ×2` (put mirror) | UND (one wing) | **NO — v1 refuse** (extra naked leg) | credit-only gate C≥0·ratio{1×2,1×3,2×3}·IVR≥50·DTE{8-30} | leg-out@spot>K2 (v2)·D-TP{50}·hard D-DTE 7 |
| 10 | Jade lizard | `S:PE@Δ(.20-.30)+S:CE@Δ(.20-.30)+B:CE@+W_c`, `C>W_c` | UND-down / no upside risk | **NO — v1 refuse** (naked put side) | constraint-driven W_c·slack s{.05C,.15C}·IVR≥50·DTE{21-45} | put side=#8·call side none/roll-up(v2)·D-TP 50 |
| 11 | Calendar short-premium | `S:{CE\|PE}@ATMOFF(0±3)/W0 + B:same@K/{W1,M0}` | ≈DEF (debit) — **EU-put exception** (§7.6) | **NO — v1 exclude** (cross-expiry max-loss not a theorem; §7.6, G11) | tier-pair·IV-term gate(front IV≥back IV)·DTE_front{1-7} | mandatory front D-DTE(pre-expiry-day)·front D-SL 2×·campaign re-sell(v2) |
| 12 | 0DTE straddle/strangle/fly | §1/§2/§4 legs with `/W0=today`, entry_time anchor | as base | per base: **fly YES, straddle/strangle NO** | time-anchored{09:20…14:00}·session bits·re-entry{0-2} | per-leg SL{20-40%} or k{1.5,2}·D-TP{50,75}·D-STOP-TOUCH·**hard 15:20** |

**How the engine consumes the table.** Each row is a `StructureSpec` (legs DSL → contract resolution via `RegistryStore.list_expiries/list_contracts` + the `fno_symbol.parse_fno_symbol` grammar `EXCH-UNDERLYING-DDMmmYY[-STRIKE]-TYPE`, `brutex/data/fno_symbol.py:13-15` verified); its entry-law family instantiates as default-omitted `canonical_trade_params` axes (§4); its management-law family is the §1.5 state machine over the joined premium path; every refusal reuses + extends the `RejectReason` vocabulary (`executor.py:155-184`) with seller classes (`STRUCTURE_LEG_NO_DATA`, `STRUCTURE_LEG_STALE_BAR`, `MARGIN_UNVERIFIED`, `SETTLE_UNMODELLED` — names indicative). **The v1 admissible set is the seven `YES`/`YES*` families** (iron condor, iron butterfly, bull put spread, bear call spread, cash-secured put, 0DTE iron fly, and the degenerate long structures) — i.e. the defined-risk repertoire on index underlyings inside the honestly-priceable cost window. The undefined-risk families (1, 2, 7a, 9, 10) and cross-expiry (11) are **first-class in the grammar but refuse on the capital axis in v1** and unlock additively when the SPAN-margin DEC and the cross-expiry-bound DEC land (§7, §10.4 R-2).

---

## §2. COMPOSITION WITH THE 60-BIT MASK VOCABULARY

### §2.1 The entry mask is reused verbatim — the load-bearing reuse

A selling structure's **entry condition** is a point in the existing 60-bit vocabulary, consumed with **zero change**. Verified: `TOTAL_CONDITIONS = 60` (`brutex/vocabulary/bits.py:167`); `IMPLEMENTED_MASK = 1152921504606846975 = (1<<60)−1` (`bits.py:173`); the mask is a **u128** already (`bits.py:11,168`: "The strategy mask is a u128 (ADR-0012 D4); bits 0..59"). `NAME_TO_BIT` (`bits.py:184`) carries trend (`ema20_above_ema200`, `close_above_vwap`), structure (`BIT_BOS_*`/`BIT_CHOCH_*`), day-type (`BIT_GAP_UP_DAY`, `BIT_INSIDE_DAY`), session-time (`BIT_EARLY_MORNING`, `BIT_MIDDAY`), level-proximity (fib/pivot/PDH-PDL) bits (`sell-core.md` §0.2). **The entry mask IS the seller filter**: a bull-put spread's entry is `ema20_above_ema200 & close_above_vwap & !gap_down_day` (bullish regime — `sell-core.md` §5(b)); a short strangle's is `BIT_INSIDE_DAY & !BOS` (range regime); a bear-call's is `ema20_below_ema200 & close_below_vwap & BIT_BOS_BEARISH`. **The option layer only picks strikes/tiers on top of a mask the vocabulary already evaluates and the kernel already counts.**

### §2.2 IV-rank, DTE, and structure parameters are option-layer PARAMS, not vocabulary bits

The seller-native timing gates (IVR floor {none,25,50,75}; DTE band {0,1-2,3-7,8-21,21-45,30-60}; event-avoidance) and the whole strike/tier/management grid ride `canonical_trade_params` as **default-omitted cell-coordinate sweep axes** — the exact precedent of the `fib_anchor` / `PriceSource` axes (DEC-NIFTY-PRICE-SOURCE-AXIS-001, MIGRATION.md #1681: default-omitted keys ⇒ existing run_ids byte-identical). **They are NOT vocabulary bits**, for two hard reasons: (i) **India VIX is barred from the mask by CLAUDE.md §2 Rule 3** (REFERENCE_ONLY — never a vocabulary bit, never in run identity — verified stance), and an IVR floor derived from VIX or ATM-IV percentile inherits that bar; (ii) adding an option-*state* bit is a **vocabulary scope change** requiring its own DEC (§2.3). Keeping them as params also keeps the sweep composable: a structure family × its constrained param grid, over an entry mask, is the exact analogue of the spot engine sweeping masks-within-K rather than arbitrary formulas.

### §2.3 The proposed seller condition-bit extension (60 → N ≤ 92) — SEPARATE FUTURE VOCAB DEC

**The engine is kernel-ready for a bigger vocabulary today.** The u128 count/enum kernels already exist and are the **active production path for any ≥64-bit dispatch** — verified in `brutex/engine/combo_mask.py`: the `>=64-bit kernel dispatch (DEC-ENUM-KERNEL-U128-001)` routes any bar/mask with a bit `≥ 64` to `fno_math.count_supermasks_u128s` / `fno_math.ResidentBarsU128` (`combo_mask.py:29-44`), sharing the ONE width-generic `MaskWord` core with the u64 kernel so the two widths cannot drift (`:36`); `U64_MASK = (1<<64)−1` is the dispatch boundary (`:161`); `U128_MASK = (1<<128)−1` is the ceiling (`:168`); a missing wide kernel raises the typed `WideKernelUnavailableError` — **never a silent numpy degrade** (`:44,62`); `dispatch_kernel_kind` + `ResidentBarBuffer.kernel_kind`/`wide_kernel_active` let a test verify a ≥64-bit grid is genuinely Rust-served (`:74-81`). So the vocabulary can grow **60 → up to ~92 bits** (leaving u128 headroom) with **no kernel work** — the trade-sim u128 twin (#1549) and the enum/count u128 twin (#1942) are the shipped kernels.

**But growing the vocabulary is its own operator-gated DEC (Golden Rule #3), NOT part of #2211.** Candidate seller-relevant bits (each an entry-*state* condition, not an option-layer param) and the exact per-bit prerequisite:

| Candidate bit class | Example bits | Per-bit prerequisite (all three, or the bit does not ship) |
|---|---|---|
| Realized-vol / compression regime | `BIT_RV_LOW`, `BIT_VOL_CONTRACTION`, `BIT_IV_ABOVE_RV` | (1) indicator math in the shared `rust/trading-core` crate (bit-identical for BruteX + TickVault); (2) an `indicator_version` bump (verified as a run-identity input, `combo.py:253`); (3) a feature-lake prebuild so the bit is **O(1)/bar** at sweep time, never recomputed in the hot loop |
| Term-structure / IV-term | `BIT_IV_TERM_FLAT`, `BIT_IV_TERM_BACKWARD` | as above; **blocked additionally** by the India-VIX/IV bar (Rule 3) unless the bit is derived from a non-VIX, non-REFERENCE_ONLY source — a hard open question (§11 register) |
| Event-proximity | `BIT_PRE_EXPIRY`, `BIT_POST_EVENT_IV_CRUSH` | as above; requires a verified event calendar (election/Budget days are untagged today — `session_lens.py:26-29`, §6) |

**The line the design draws.** #2211 ships the structure/executor/walker/results slice with the vocabulary **unchanged at 60 bits** — every seller entry composes with existing bits, and every IV/DTE/structure knob is a `canonical_trade_params` param. **A vocabulary extension is a strictly-later DEC** (`DEC-OPTSELL-VOCAB-EXTEND-00N`, unminted) that must: build the indicator in `trading-core`, bump `indicator_version`, prebuild the feature lake for O(1)/bar, satisfy the Rule-3 REFERENCE_ONLY bar for any VIX/IV-derived bit, and — critically — this is **still a vocabulary DEC, not an engine-scope DEC**: it does not by itself admit *options as instruments* into the sweep (that is a third, separate scope-expansion DEC, §10). The three gates (capability slice #2211 → vocabulary extension → options-into-engine-scope) are deliberately disjoint.

---

## §3. THE MULTI-LEG STRUCTURE WALKER

The walker is the `trade_sim.py` sibling — the one genuinely NEW hot-path engine module (chapter `03` §1.7 cluster (ii): "the multi-leg structure layer (fills, walker, per-leg records)"). It is a **per-structure** state machine, not a per-leg one.

### §3.1 Per-leg worst-case fills — the shipped law applied per leg

Each leg is priced at **its own** intra-bar adverse extreme, verbatim from the product-wide law (`gap-fill.md` §1, verified against `brutex/costs/calculator.py:123-181` and `trade_sim.py:197`):

- **long (buy-to-open):** entry fill = `leg_bar.high + tick`; exit (buy-to-close of a short) = `leg_bar.high + tick` (most debit).
- **short (sell-to-open):** entry fill = `leg_bar.low − tick` (least credit); exit (sell-to-close of a long) = `leg_bar.low − tick`.

The four bar extremes are REQUIRED at `TradeInput` construction (`brutex/costs/types.py:303-314` per chapter `03` §1.3), and the options **short branch already exists** in the calculator (`calculator.py:164-181`) — it is only **production-unreachable** under DEC-EXEC-008 (`sell-core.md` §0.3, `gap-sim.md` §b: "the per-leg SELL fill law; only its activation is new"). #2211 activates it for the structure track. Per-leg tick-floor semantics (`TICK_FLOOR_MULTIPLE = 5` × ₹0.05, `executor.py:129-132` verified) and the volume-0 `STALE_BAR` reject (`executor.py:543-557`) are preserved per leg.

### §3.2 The N-leg atomic fill law + the order-independence honesty

**All N legs fill in the SAME decided bar; each leg independently takes its own worst-case adverse extreme ± tick.** This law is **order-INDEPENDENT by construction** — 1-minute bars carry no intra-bar sequencing, so any leg-ORDER model would be fabricated microstructure (`gap-sim.md` §b.3). **Mandatory honest disclosure** (a design-doc sentence in the DEC-FILL-WORST-CASE-ONLY-001 style): charging each leg its own intra-bar worst is **strictly MORE pessimistic** than any real simultaneous package execution — a real basket/IOC spread order nets some of the adverse extremes, so per-leg independent extremes can exceed the worst NET package price actually printable in that minute (**G12**, logically argued, not empirically measurable at 1m granularity — but the direction only strengthens the conservative bias the engine already chose for spot: "strictly MORE pessimistic … never optimistic", `trade_sim.py:69-73` verified). No tighter package-level bound is observable from per-leg OHLC without co-timed intrabar data; it is rejected as fabrication.

### §3.3 Whole-structure correct-or-absent (all-or-nothing per bar)

**Bar data has no book depth and no queue observability; a 1-minute bar either printed volume or it did not. A partial fill — or a structure entering with 3 of 4 legs — is unobservable fiction.** **Law: all-or-nothing per structure per bar** (`gap-sim.md` §b.4). If ANY leg lacks its entry bar or its exit bar (source `None`) or fails `volume > 0`, the WHOLE structure is a counted reject (`STRUCTURE_LEG_NO_DATA` / `STRUCTURE_LEG_STALE_BAR`, carrying which leg failed) — **never a partial structure** (`gap-sim.md` §b.5). This is mandatory for writers: an iron condor missing its bought wing is a **naked strangle** — a different risk object whose max-loss capital basis (§7) becomes a lie (`gap-sim.md` §b "Why it matters"). **Honesty note the counters must carry:** the single-leg liquidity-survivorship tilt (`executor.py:606-622`) COMPOUNDS with N — deep-OTM wings (exactly the legs defined-risk structures buy) are the least liquid, so structure-level rejects correlate with the market states short-premium finds hardest; all rule comparisons remain **within-accepted-set** comparisons only (§7.2).

### §3.4 The management state machine over the joined premium path

Per open structure, per 1-minute bar, the walker: (1) reads each leg's bar close (missing minute → the leg-missing law, U-5 to be pinned in the DEC); (2) computes the structure mark = Σ signed leg closes; (3) evaluates the §1.5 triggers (`D-TP`/`D-SL-k`/`D-DTE`/`D-STOP-TOUCH`/hard-15:20) in a **pinned deterministic order** (the pessimistic-tie precedent: `trade_sim.py:43-45` "the STOP fills first, always" — so a same-bar SL+TP resolves SL-first); (4) on a trigger, closes all open legs at their worst-case extremes. The exit terminal appends to `EXIT_REASONS` under its append-only law and walks the proven `session_gap` chain end-to-end: enum → `TradeDetail` → aggregate `exits_*` field → AGG column → top100 exits → display exit-mix (`gap-sim.md` §e / chapter `03`). New tokens: `structure_tp`, `structure_sl`, `structure_dte`, `structure_touch`, `expiry_settle` (§8).

### §3.5 Deterministic laws + per-trial cost (the honest complexity)

The walker is deterministic (Golden Rules #4/#7): same inputs → same structure trades, bit-for-bit; order-independent fills; a pinned trigger order. **Per-trial cost, stated honestly (Golden Rule #10):** a static structure costs `~2N fills + terminal settlement`; the hot loop is **`O(bars-in-trade × legs)`** overlay/premium reads (a v2 adjusted structure would be `O(bars × open-structure state)` with per-bar chain lookups — §1.5.3). **There is NO options trials/s figure**: the spot engine's 80–150K trials/s envelope (`TRACK1_PERFORMANCE_BUDGET.md:135` per chapter `03`) **does NOT transfer** — per-leg premium lookups add memory traffic the spot budget never modeled, and no prototype bench exists (**G5**, `gap-sim.md` §f.2). **No options-sweep SLA may be written until a prototype bench over real option 1m data + a `TRACK1_PERFORMANCE_BUDGET`-style re-derivation lands.** This is the single largest open feasibility question and is called out as such in §5 and §11.

<!-- END-CHUNK-1 -->
