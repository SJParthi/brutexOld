# 07 — Verification & Phasing: how the option-selling capability is proven, and the exact DEC ladder that ships it

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code ships in this doc.** The sweep engine remains **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001, MIGRATION.md #1790) and options remain **REFERENCE_ONLY** lake data (CLAUDE.md §2 Rule 3). The capability verified and phased here ships **DEFAULT-OFF / INERT**, and its **activation** — plus **any entry of options into the engine's brute-force sweep-scope or into the 60-condition vocabulary** — are **SEPARATE future operator-gated DECs** (Golden Rule #3). Nothing in this document authorizes activation, a scope change, or a vocabulary change.
>
> **Date:** 2026-07-20
> **Provenance:** multi-agent design panel, docs-only branch `claude/options-strategies-design`, part of **DEC-OPTION-SELLING-RESEARCH-001 (#2210)** — draft PR #1725 carries this research pair. The future default-OFF code slice this chapter verifies + sequences is **DEC-OPTSELL-STRUCTURE-EXECUTOR-001 (#2211, its own PR)**.
>
> **What this chapter is.** Chapter `05` (+ the fuller risk-first companion the panel distilled it from) specifies the *engine*: the structure grammar, the multi-leg walker, the fill law, the tail battery, the margin truth, the settlement proxy, the cardinality, and the additive run-identity keys. This chapter answers the two questions `05` deliberately leaves open: **(1) how do we PROVE it** — the module-by-module verification pyramid, the exact goldens/properties, and the standing 100%-line+branch + mutation gates it must pass; and **(2) how does it SHIP** — the ordered, operator-gated DEC ladder from "#2210 research lands" to "options in engine scope", each DEC stamped with what it adds, what stays INERT, and the gate it must pass. It also collects the single consolidated **honest-limits register** — every claim a seller backtest is tempted to make and must not, each with the DATA or DEC that would lift it.
>
> **Evidence discipline.** Every repo claim is `path:line`; every `path:line` resolves at the pinned base SHA `184a48a4` — see README §5; the branch head has drifted (resolve via `git show 184a48a4:<path>`). Citations trace to the named research section or the committed chapters `01`/`03`/`05`/`06`/`08`/`09`/`10`/`11`. Market facts cite the research section (which carries its own web/UNVERIFIED tags) or are tagged UNVERIFIED and collected in §4. Cardinality shows the multiplication (Golden Rule #10). No number is presented as `guaranteed`/`fully`/`100%`; a family that cannot be honestly priced, sized, or settled **refuses and is counted** — the repo's own house law.

---

## §0. What is being verified, and the four laws every test enforces

The capability slice `#2211` adds a small set of **new Python modules** (no new Rust kernel — the walker is a "wide-cell" Stage-3/Stage-4-style re-simulation, **not** the u64/u128 mask-enumeration hot path — design companion §8.4) and a set of **additive, default-omitted** result columns and run-identity keys. The eight modules under test, in the panel's own decomposition (chapter `03` §1.7 cluster (ii), the four NEW-NEEDED clusters):

| # | Module (verified NEW or EXTEND) | One-line contract it must never break |
|---|---|---|
| M1 | **Structure model** — `OptionStructure` / `StructureLeg` (`05` §2.1) | `structure_net == Σ leg_nets` at construction; one decided entry instant + one decided exit instant for ALL legs; v1 cap 1-4 legs, index-only, else typed refuse |
| M2 | **Strike-selector resolver** — the E-* families (`05` §2.2) | pure `(entry_bar, side, type) → Decimal strike \| REFUSE`; never look-ahead; refuse (not fabricate) where the overlay is NULL |
| M3 | **Multi-leg structure walker** — the `trade_sim.py` sibling (`05` §4) | per-leg worst-case fill; all-or-nothing per structure per bar; deterministic; suffix-independent (no look-ahead) |
| M4 | **Defense-law engine** — the `exit_policy` state machine (`05` §4.4) | pinned trigger order (SL-first on a same-bar tie); **defer-never-reject on a no-print bar** (ruling R-5, a correctness law); annotation-never-reranks the spot signal |
| M5 | **Expiry / settlement** — index cash-settle proxy + band; stock-physical / hold-to-expiry refusal (`05` §6 / companion §6) | settle at the ADVERSE plausible value inside a DISCLOSED band; hold-to-expiry / stock-physical **refuses** until its own DEC; session constants are per-(segment, date) DATA |
| M6 | **Cost / STT engine** — per-leg composition + exercise-vs-sell STT asymmetry + ETC refusal-window (companion §6.2) | per-leg cost composes; STT asymmetry computed per-regime from dated tables, never a constant rule; pre-2024-10-01 option ETC = counted `COST_REGIME_UNVERIFIED` refusal |
| M7 | **Feature-lake prebuild** — IV/chain feature lake + chain-completeness census (gap-data §D-B/§D-C1) — **a FUTURE additive DEC, not #2211** | O(1)/bar at sweep time; every feature row carries coverage provenance; a genuinely-missing liquid strike is an ATTESTED hole, never silent survivorship |
| M8 | **Results columns** — additive nullable `ResultRow`/`AGG`/`legs`-sidecar columns + fill-honesty counters + tail-battery annotations (gap-sim §c, companion §9) | additive-only, default-omitted; counters count-consistent; every seller analytic lands sidecar-first, REFERENCE_ONLY, never re-ranks, never in run identity |

Every test in §1 is an application of the **four honesty laws** (`05` §0, each an existing repo invariant, not a new promise):

1. **Correct-or-absent** — a structure that cannot be priced, sized, or settled honestly **refuses atomically and is counted** (the `RejectReason` + count-consistency discipline, `brutex/options/executor.py:155-184`, `:602-712`).
2. **Worst-case only** — every fill takes the adverse intra-bar extreme; the product-wide fill law admits no other mode (`DEC-FILL-WORST-CASE-ONLY-001`; `brutex/engine/trade_sim.py:28,197,613-798`; `brutex/execution/fill_mode.py:3-4,10-17`).
3. **Additive identity** — new run-identity keys are default-omitted so every existing spot `run_id` stays byte-identical, and anti-aliasing keys are unconditional (`canonical_trade_params`, `brutex/engine/sweep_run.py:719-809`; `roll_schedule` conditional `:798-799`, `fill_mode` unconditional `:809`).
4. **Never a ranking gate without its own DEC** — every seller analytic lands first as a display-only annotation (its own sidecar, `—` abstains, zero run-identity impact); promotion to a *rejecting* gate is a separate operator-gated DEC (`brutex/engine/resilience_streaks.py:10-16` "ANNOTATION / DISPLAY ONLY. Never re-ranks / filters / rejects"; the R13 / ranked-never-hidden / never-adopt family, gates-evidence §4).

The verification pyramid's job is to make each of the four laws a **mechanically-failing test** — a red gate if the code drifts, never a prose promise.

---

## §1. THE VERIFICATION PYRAMID

### §1.0 The seven test types + the standing gates every module clears

CLAUDE.md §12 requires **seven test types** on every module (unit · integration · property · golden · bench · stress · mutation) and the **100% ALWAYS STANDARDS** gate battery; CLAUDE.md §4 pins the numeric bars. The capability slice inherits them unchanged:

| Gate | Bar (CLAUDE.md §4/§12) | How it applies to #2211 |
|---|---|---|
| Coverage | **100% line + 100% branch** on every new/touched module in `brutex/` | The refusal branches are the majority of the code (gap-fill §5 "the reject machinery is the MAIN path for options"); 100% branch means every `RejectReason` arm + every atomic-reject leg-index arm is exercised |
| Mutation | **≥ 85%** (the mutmut per-module loop; the recent house discipline is **zero-survivor** — the DEC-MUTATION-RUNTIME-003 pattern, gates-evidence context) | Each fill-anchor sign, each tie-break, each count-consistency law, each threshold constant is a mutant that must die (the `!=`→`==` / arg-swap kill discipline of the edge goldens, `06` E-11) |
| Types | `mypy --strict` **AND** `pyright`, both clean | New typed reject enum + `Decimal`-strike selectors + int64-paisa cost composition |
| Static | `ruff` + `pylint` (warnings-as-errors) + `bandit` | — |
| Doctest + citation-accuracy | repo-wide (the DEC-TEST-APPARATUS-DEBTS-001 gates) | every `path:line` in this chapter's doc-comments must resolve |
| No look-ahead | Property P-06 + the suffix-independence battery (CLAUDE.md §4) | the walker's own suffix-independence property (M3) is the seller-specific extension |
| No `pandas` in hot paths | ban-list gate | the walker reads `polars`/`numpy` per-leg frames only |

**Honest O(1) note (Golden Rule #10).** The CLAUDE.md §5 O(1)-per-hot-path discipline **does not** apply to the walker per-trial: a static structure is `O(bars-in-trade × legs)` overlay/premium reads, and a v2 adjusted structure is `O(bars × open-structure state)` (`05` §4.5, companion §8.4). O(1) DOES bind two places and the bench tests must pin exactly those: (a) **M7 feature-lake reads** — a prebuilt IV/chain/coverage feature is `O(1)/bar` at sweep time, never recomputed in the loop (gap-data §D-B "structurally incompatible with the O(1) discipline" if computed inline — this is a hard design constraint, tested by a bench that a feature read does not scan the chain); (b) **M8 results append** — the additive columns/sidecar append is O(1)/trial (the results-depth sidecar precedent). **No options trials/s figure exists and none may be written** until a prototype bench over real option 1-minute data + a `TRACK1_PERFORMANCE_BUDGET`-style re-derivation lands (**G5** — the single largest open feasibility question; the spot 80-150K trials/s envelope, `TRACK1_PERFORMANCE_BUDGET.md:135`, does **not** transfer — companion §8.4). The bench tier for #2211 is therefore a **linearity + no-inline-chain-scan** bench, never an absolute-throughput SLA.

### §1.1 The load-bearing cross-module golden: run-identity additive-key byte-identity

**This is the single most important test in the whole slice** — it is the mechanical proof that the capability is INERT-by-construction. The design's honesty law 3 (`05` §0) is that the fully-absent structure state ⇒ a defenseless static structure whose params are byte-identical to a no-grammar run (companion §9). The golden:

> **GOLDEN `run_identity_inert`** — take a set of existing spot sweep `run_id`s (the frozen `_GOLDEN_RUN_ID` corpus the repo already pins). Thread every new structure/leg/defense/fill/sizing param through `canonical_trade_params` at its DEFAULT (all default-omitted). Assert every existing spot `run_id` is **byte-identical** to the pre-#2211 value. This is the exact `fill_mode` / `roll_schedule` / `PriceSource` precedent (DEC-NIFTY-PRICE-SOURCE-AXIS-001: default-omitted keys ⇒ existing run_ids byte-identical — `05` §3.2; the emission law verified `sweep_run.py:782,860,798-799,809`).

Companion §9 pins **which keys enter identity and which stay out** — the golden's coverage must include both sides:

| Enters identity (default-omitted; a non-default value ⇒ a DIFFERENT run) | Stays OUT of identity (OUTPUTS, REFERENCE_ONLY) |
|---|---|
| the structure/leg axes (`opt_*`/`sel_*`), the management axes (`def_sl_bps`, `def_tp_pct`, `def_dte_stop`, `def_stop_touch_scope`, `def_max_actions`), the fill axes (`spread_penalty_ticks`, `def_penalty_ticks`), the sizing axes (`sizing_law`, `B_risk_bps`, `K_stress`, `m_floor_bps`, `H_heat_bps`) | the fill-honesty class COUNTERS (`n_class_a/b/c`), `defense_deferred_minutes`, the `settlement_proxy` band, `margin_proxy`, the VIX trio, the spiral bounds |
| **the fill-honesty-POLICY axes that SHAPE the accepted set** (Golden Rule #4): `fill_lambda_x100` (P3 participation multiple), `fill_band_steps` (P4 band width), `size_honesty ∈ {flag, refuse}` (`refuse` DROPS Class-B trades ⇒ a different accepted set) — companion §9 | — |

**Property `identity_shapes_accepted_set`** — the risk-first insistence, mechanically pinned: two configs that differ only in `size_honesty` (`flag` vs `refuse`), or only in `fill_lambda_x100`, produce a **different accepted trade set** ⇒ must have a **different `run_id`** (never a silent re-key; the DEC-FILL-WORST-CASE-ONLY-001 anti-aliasing precedent). A `def_grammar_rev` int self-describes the priority-ladder/re-arm semantic revision across commits (the `roll_schedule` self-description argument), and presence-coherence invariants (e.g. `def_invert_steps` ⇒ `def_roll_*` present) live in the config `__post_init__`, mirroring the instrument⟺roll_schedule invariant (`sweep_run.py:654`; companion §9).

### §1.2 The per-module pyramid

Each module below carries all seven test types. The **specific** golden/property is named; the standing gates (§1.0) are assumed on top. Edge-register cross-references (`E-NN`) point at the committed chapter `06` entries whose fixture the test reuses.

---

#### M1 — Structure model (`OptionStructure` / `StructureLeg`)

| Type | Specific test |
|---|---|
| unit | leg-tuple validation: `side ∈ {long,short}`, `type ∈ {CE,PE,FUT}`, `ratio ∈ ℕ⁺`; the v1 caps `STRUCTURE_TOO_MANY_LEGS` (5+ legs) + `STRUCTURE_CROSS_UNDERLYING` refuse with a typed reason (`05` §2.1) |
| integration | a family-table row (`05` §2.4) → contract resolution via `RegistryStore.list_expiries`/`list_contracts` + the `fno_symbol.parse_fno_symbol` grammar `EXCH-UNDERLYING-DDMmmYY[-STRIKE]-TYPE` (`brutex/data/fno_symbol.py:13-15`) → a fully-populated `OptionStructure` |
| property | **construction invariant `structure_net == Σ leg_nets`** at every leg count 1-4, over hypothesis-generated leg tuples — the `MaterializedFuturesTrade` `net == gross − charges` precedent (`brutex/futures/executor.py:422-424` per chapter `03` §6 e.1 / GAP-33) |
| golden | the seven `YES`-admissible family rows (iron condor / iron butterfly / bull put / bear call / cash-secured put / 0DTE iron fly / degenerate long) each construct to a byte-pinned `StructureSpec`; the six capital-refused families (short straddle/strangle, index covered call, front-ratio, jade lizard, calendar) construct-but-refuse-on-capital with pinned reason bytes (`05` §2.4, companion §2.4) |
| bench | construction is O(legs), flat vs family complexity (no chain scan at construction) |
| stress | a 4-leg cross-underlying request + a 5-leg request each refuse (never a truncated 3-of-4 structure — the M3 atomicity law seeded here) |
| mutation | the `structure_net` fold operator, the leg-count boundary (`≤4` vs `<4`), and each v1-cap reason are mutants that must die |

---

#### M2 — Strike-selector resolver (the E-* families)

| Type | Specific test |
|---|---|
| unit | each selector is a pure `(entry_bar, side, type) → Decimal strike \| REFUSE`; the moneyness machinery it rides never raises (`brutex/data/moneyness.py`: `classify` `:68`, `derive_strike_step` `:106`, `resolve_strike_step` `:181`, `atm_offset` `:238`, `moneyness_view` `:314` — verified O(1), never raise, `05` §2.2) |
| integration | `E-ATMOFF`/level-anchored resolve from stored 1m bars alone (coverage-robust); `E-DELTA`/`E-EM` join the stored Greeks overlay (`delta` `models.py:158`, `iv` `models.py:157`) |
| property | **overlay-NULL ⇒ REFUSE, never fabricate** — on every bar where the 7-status ladder returns NULL (`NO_RATE/AT_EXPIRY/NO_SPOT/BAD_INPUT/BELOW_INTRINSIC/NO_SOLUTION`, `brutex/data/greeks_enrich.py:979-1080`), a delta/EM-selected strike is unresolvable and the structure refuses (E-05 condition-abstain law, `06` §5 Law C) |
| golden | the seed grids (`sell-core.md` §0.6) resolve to pinned strikes at a fixed entry bar: `E-ATMOFF(n)` for `n ∈ {0,1,2,3,5,8,10,15,20}`; `E-DELTA` bands; `E-EM(m)` at `m ∈ {0.5..2.0}`; the **known structural holes** are pinned as refusals — the last ~600 s before expiry (`AT_EXPIRY` NULL, so a delta-selected 0DTE strike is unresolvable in the final ~10 minutes, gap-sim §c.1) and deep-ITM zero-time-value strikes (`BELOW_INTRINSIC` NULL) |
| bench | O(1) resolution per selector (no chain enumeration in the selector) |
| stress | `E-IVW(a>0)` **refuses / is UNAVAILABLE in v1** — no stored IV-rank column exists (**U-4/D-B1**); a stock underlying refuses `W*` weekly tiers (weekly stock options do not exist in India — `sell-core.md` §7(b)) — pinned as a negative test |
| mutation | the ATM-offset sign, the delta-band inequality direction, and the EM formula rounding-to-grid are mutants that must die |

**Honest limit carried into §2:** `E-DELTA`/`E-EM` coverage is bounded by the production-lake Greeks overlay fraction, which is **UNVERIFIED (G8)** — closable by `brutex greeks-backfill --dry-run` census. `E-ATMOFF` and level-anchored selectors are the coverage-robust fallback and carry no overlay dependency (`05` §2.2).

---

#### M3 — Multi-leg structure walker (the `trade_sim.py` sibling)

The one genuinely NEW hot-path engine module (chapter `03` §1.7 cluster (ii)). Its four sub-laws each get a dedicated test:

| Type | Specific test |
|---|---|
| unit | per-leg fill anchors (`05` §4.1): long entry = `leg_bar.high + tick`, short entry = `leg_bar.low − tick`, floored at one tick (`TICK_FLOOR_MULTIPLE = 5 × ₹0.05`, `brutex/options/executor.py:129-132`); the options short branch already exists (`brutex/costs/calculator.py:164-181`) and is only production-unreachable under DEC-EXEC-008 — this test activates it per leg |
| integration | a 4-leg iron condor round trip through the walker → per-leg breakdowns compose to one `structure_net` (the calculator is "pure composition per leg", `executor.py:18-28`) |
| property | **(a) whole-structure correct-or-absent (all-or-nothing per bar)** — if ANY leg lacks its entry OR exit bar (`None`) or fails `volume > 0`, the WHOLE structure is a counted reject (`STRUCTURE_LEG_NO_DATA` / `STRUCTURE_LEG_STALE_BAR` carrying the failing leg), **never a partial structure** (an iron condor missing its bought wing is a *naked strangle* — a different risk object whose max-loss basis becomes a lie; gap-sim §b.4, gap-fill §3.2 Class-C atomicity, `06` §5 Law B). **(b) look-ahead suffix-independence** — Stage-1A bar_bits, the joined premium path, and the full structure trade over bars `0..N` are **bit-identical** whether bars `N+1..` are absent / mutated / extreme (the DEC-SWEEP-LOOKAHEAD-GUARD-001 suffix-independence battery extended to the walker; CLAUDE.md §4 Property P-06). **(c) order-independence** — all N legs fill in the same decided bar, each taking its own worst-case extreme; permuting leg order leaves the fills bit-identical (1-minute bars carry no intra-bar sequencing, so any leg-ORDER model would be fabricated microstructure — gap-sim §b.3) |
| golden | **per-leg worst-case fill golden** — a fixed 4-leg fixture with hand-derived per-leg entry/exit fills at the printed extremes ± tick, byte-pinned (the `MIGRATION.md #1825` per-leg ±1-tick discipline "flat bar ≡ retired open±tick fill, so worst-case can never flatter"); **structure-atomic-reject golden** — the `06` E-01 fixture (one wing dir deleted → structure reject counted, bytes pinned, `06:213`) |
| bench | **linearity** — walker cost scales `O(bars-in-trade × legs)`; a bench pins the linear coefficient and asserts **no inline chain scan** (the O(1)-per-bar feature-read law, §1.0). **NO absolute trials/s SLA** (G5). |
| stress | the `06` E-03 fixture (a leg of an open structure goes dark mid-trade, session scale) → the retro-close/censor fork, counted, bytes pinned (`06:390`); the survivorship-tilt honesty counter must ride (deep-OTM wings — exactly the legs defined-risk structures buy — are the least liquid, so structure-level rejects correlate with the states short premium finds hardest; `executor.py:606-622`, gap-sim §b.5) |
| mutation | each fill-anchor sign, the `+tick`/`−tick` direction per side, the atomic-reject "if ANY leg" quantifier (mutating it to "if ALL legs" must die), and the leg-iteration determinism are mutants that must die |

**The mandatory honesty sentence the walker's docstring + every result surface must carry** (a design-doc sentence in the DEC-FILL-WORST-CASE-ONLY-001 style, gap-sim §b.3): charging each leg its own intra-bar worst is **strictly MORE pessimistic** than any real simultaneous package execution — a real basket/IOC spread order nets some of the adverse extremes, so per-leg independent extremes can exceed the worst NET package price actually printable in that minute (**G12**, logically argued, not empirically measurable at 1m granularity; the direction only strengthens the conservative bias the engine already chose — `trade_sim.py:69-73` "strictly MORE pessimistic … never optimistic"). No tighter package-level bound is observable from per-leg OHLC without co-timed intrabar data; it is rejected as fabrication.

---

#### M4 — Defense-law engine (the `exit_policy` state machine)

| Type | Specific test |
|---|---|
| unit | each trigger evaluates from stored data per 1-minute bar: `D-TP(p)` (MTM profit ≥ p% of net credit), `D-SL-k(k)` (MTM loss ≥ k·C), `D-DTE(d)`, `D-STOP-TOUCH` (spot flips a short strike OTM→ATM/ITM via `classify`), hard-15:20 (`05` §4.4) |
| integration | the state machine runs over the **structure premium path** = Σ signed leg closes (an O(legs) join over the per-contract 1m frames — the exact read the movers-options screen already performs, `lake_source.load_pair` per leg, `sell-core.md` §0.7); the hard 15:20 forced exit reuses the spot walker's path verbatim (CLAUDE.md §7) |
| property | **pinned deterministic trigger order** — a same-bar SL+TP resolves **SL-first, always** (the pessimistic-tie precedent, `brutex/engine/trade_sim.py:43-45`); the both-sides-touched worst-case order is deterministic per structure (`06` E-11, mirrored against the single-leg goldens, `06:1163`) |
| golden | a two-session managed-exit fixture: entry → `D-TP(50)` fires → all legs close at their worst-case extremes; the exit terminal appends to `EXIT_REASONS` and walks the proven `session_gap` chain end-to-end (enum → `TradeDetail` → aggregate `exits_*` → AGG column → top100 exits → display exit-mix, gap-sim §e) with the new tokens `structure_tp`/`structure_sl`/`structure_dte`/`structure_touch`/`expiry_settle` (`05` §6.4) — bytes pinned |
| bench | per-bar trigger evaluation is O(legs) (a v2 leg-mutating adjustment would be O(bars × open-structure state) with per-bar chain lookups — deliberately **excluded from v1**) |
| stress | **ruling R-5, the correctness law: defense on a no-print bar DEFERS, never rejects** — a reject silently holds the undefended short for free (the anti-conservative inversion); the `06` E-02 deferred-exit-law fixture pins the defer to the next `volume>0` bar, counted as `defense_deferred_minutes`, bytes pinned. This is the one seam where the executor's default (reject a volume-0 bar) is **consciously superseded** for open-defense — a defer, priced at the deferred bar's worst-case extreme (companion §12 R-5; the reject-holds-a-naked-short hazard, gap-fill §2(ii)) |
| mutation | the SL-first tie order (mutating to TP-first must die), each trigger inequality direction, and the defer-vs-reject branch are mutants that must die |

**Annotation-never-reranks pin (M4 ∩ M8):** the defense/sizing layer lives in the TRANSLATION/executor track and **never mutates the spot signal row** — the R13 law verbatim (`brutex/futures/executor.py:14-16` "the pure spot signal row is NEVER mutated … Nothing here writes to the results store or influences ranking"). A negative-pin (rank-order byte-untouched) is a permanent structure-lock test (§1.4).

---

#### M5 — Expiry / settlement engine

| Type | Specific test |
|---|---|
| unit | the **Step-0 refusal is preserved and extended** — `calculator.py:237-251` raises `UnsupportedTradeOutcomeError` on anything but `NORMAL_CLOSE` (the §4.2 trade-outcome state machine is a fail-loud STUB, `CALCULATOR_SPEC.md:275-288`); v1 adds `expiry_settle` for index cash-settle only |
| integration | index cash settlement at a **settle-price PROXY** derived from the stored 1m spot bars (last-30-min VWAP-like fold — ruling R-3) with a DISCLOSED error band; a short strike pinned inside the band resolves at the **ADVERSE plausible settle within the band** (the worst-case discipline generalized, micro-settle §6.5) |
| property | **the band IS the pin-risk model** — a settle-vs-LTP divergence flips a pinned short between "keep full premium" and "pay intrinsic" discontinuously (micro-settle §6.3), and the resolution always takes the adverse side (never optimistic); the holiday-shifted expiry date is applied (the expiry calendar's holiday shifts are NOT applied today — `brutex/options/expiry_calendar.py:26-29` — a settlement engine keyed to real dates must close that first, gap-sim §d.4) |
| golden | the `06` E-06 0DTE-endgame fixture (entry 09:20/exit 15:20 priced; entry 15:25 with hold-to-settle → **typed refusal bytes pinned**, `06:693`; the adverse-within-band settle applies to the pinned short call at `K = proxy` under route (b), `06:694-695`); the `06` E-07 assignment/expiry fixture pins the **`UnsupportedTradeOutcomeError` refusal bytes** for hold-to-expiry until the expiry-state-machine DEC lands (`06` E-07 test types: golden · property · regression) |
| bench | settle resolution is O(session bars in the last-30-min window), memoized per (underlying, expiry-date) |
| stress | **stock physical delivery = REFUSE (ruling, `05` §6.3 / companion §6.3):** v1 scope = INDEX underlyings only (all cash-settled); a stock-option structure held to expiry either carries a mandatory pre-expiry forced-exit or **refuses at construction** with a typed reason (`SETTLE_UNMODELLED`) — never a silent hold-through-delivery (the E-14 physical-delivery, client-level netting, delivery-margin ramp, `06:795` L5; gap-data §D-F2.3). **The 2026-08-03 session-shape shift** (NSE F&O close → 15:40, 15:10-15:40 settle VWAP, `06:592-593`) means session constants (the Greeks T-anchor hardcodes 15:30 today, `greeks_enrich.py:24-26`) must become **per-(segment, date) DATA** — pinned by a negative test that a post-2026-08-03 expiry uses the shifted window (**EU-03**) |
| mutation | the adverse-within-band selection (mutating to the favorable side must die), the holiday-shift application, and the Step-0 refusal gate are mutants that must die |

---

#### M6 — Cost / STT engine

| Type | Specific test |
|---|---|
| unit | per-leg cost composition (each executed order = one `₹20/order` brokerage breakdown — `costs/types.py:440-442`; ETC/SEBI/IPFT/GST per leg) |
| integration | the structure net = Σ per-leg net-of-cost; a defended structure adds `2·A·legs-moved` more per-leg cost breakdowns (₹20/order multiplies per executed order) |
| property | **exercise-vs-sell STT asymmetry is computed per-regime from the dated tables, NEVER a constant "always sell before expiry" rule** (`05` §6 / companion §6.2, micro-settle §3): sale STT = seller-side on PREMIUM (0.10% in 2024-10-01→2026-03-31; 0.15% from 2026-04-01), exercise STT = purchaser-side on INTRINSIC (0.125% through 2026-03-31; 0.15% after) — the exercise rates already exist in-repo as unreachable forward-compat rows (`costs/constants.py:82-89`); the property is that adding trade-level charges can FLIP the sell-vs-hold verdict and from 2026-04-01 the STT asymmetry vanishes |
| golden | **the STT-asymmetry golden** — the micro-settle §3.3 worked example (₹75k intrinsic; both regimes; both exit paths, `06:805`): under the Oct-2024 regime, selling a deep-ITM at 15:29 saves ₹18.75 STT vs exercising (= (0.125% exercise − 0.10% sale) × ₹75,000, the dated rates at `costs/constants.py:82-89`) — byte-pinned both regimes |
| bench | cost composition is O(legs · orders) |
| stress | pre-2024-10-01 option trades are counted `COST_REGIME_UNVERIFIED` refusals (the ETC refusal-window; option ETC/STT VERIFIED only from `2024-10-01`, `COSTS_VERIFIED.md:731`; the refusal idiom `executor.py:184,509-530`), never a fabricated rate; the **G6 open question** — whether NSE levies ETC/SEBI/IPFT on EXERCISED (non-traded) contracts (flips the exercise-vs-sell verdict) — is carried as an UNVERIFIED, the repo's branch marked illustrative-not-normative (`CALCULATOR_SPEC.md:356-358`) |
| mutation | each STT rate constant, the seller-vs-purchaser side, the premium-vs-intrinsic base, and the refusal-window boundary date are mutants that must die |

---

#### M7 — Feature-lake prebuild (a FUTURE additive DEC — see §3, not #2211)

The IV/chain feature lake + the chain-completeness census. It is **NOT part of #2211** (the capability slice ships with the vocabulary unchanged and every IV/DTE knob as a `canonical_trade_params` param, most of which resolve without it — §3). Its pyramid is specified here so its DEC can be written against it:

| Type | Specific test |
|---|---|
| unit | per-(underlying, session-or-minute) feature rows: `{CM-ATM-IV, IV-rank/percentile over a declared lookback, skew metric, near-vs-next term slope, PCR}` — computed from the stored per-bar IVs by a prebuild pass (the `materialize_lake_timeframes` pattern, gap-data §D-B) |
| integration | a chain-completeness census join: `registry.duckdb contracts` (the vendor-listed strike ladder, `brutex/data/registry_store.py:114-127`) × the window ledger's per-contract `groww-option` windows (PLANNED/SUCCESS/EMPTY/FAILED) × the write-time stats manifest → a strike×side matrix with verdicts PASS / HOLEY(named strikes) / UNPULLED (gap-data §D-C1, modeled on the uniqueness-census / `brutex verify-acceptance` pattern) |
| property | **O(1)/bar at sweep time** — a feature read never scans the chain (the sweep-hot-path constraint, gap-data §D-B "structurally incompatible with the O(1) discipline given the chain-at-instant read complexity"); every feature row carries constituent-count/coverage provenance (never a feature without its coverage) |
| golden | a fixture lake with a known hole → the census names the missing strikes (never silent survivorship); a fixture with full coverage → PASS bytes pinned |
| bench | the census is one `O(registry rows + FNO dirs + manifest shards)` pass, incremental via the stats manifest; the feature read is `O(1)` — bench pins both |
| stress | the **30-day history wall (D-C2)** — a 30d constant-maturity point sits at the EDGE of available data (interpolation becomes extrapolation whenever the next expiry > 30 DTE has no bars, `contract_bounds.py:104` `DEFAULT_OPTION_LOOKBACK_DAYS = 30`); a CM tenor beyond ~30d is historically impossible and the feature row must ABSTAIN, not extrapolate |
| mutation | the IV-rank lookback window, the coverage-fraction threshold, and the census hole-detection quantifier are mutants that must die |

**A permanent structure-lock the feature-lake DEC must carry (companion §7.5):** the ×4 bad-tick screen (`BAD_TICK_MAX_RATIO = 4.0`, `brutex/data/bad_tick.py:76`) must **stay OFF for option premia** — a crash-day deep-OTM ₹2 put printing ₹10 is a REAL price, and routing option bars through the ×4 screen would DELETE genuine blow-up prints (the exact signal the tail battery reads); it is already deliberately disabled for premia (`brutex/dashboard/movers_futures.py:905-911` "the 4× threshold is INVALID for premia"). A premium-calibrated outlier screen does not exist and is NEW design work; until then, the insane arm (finiteness/positivity/ordering, `price_sanity`) is the only inheritable screen (gap-data §D-D.5).

---

#### M8 — Results columns (additive `ResultRow`/`AGG`/`legs`-sidecar + fill-honesty counters + tail annotations)

| Type | Specific test |
|---|---|
| unit | additive nullable `ResultRow` columns (`capital_paisa`, `capital_basis ∈ {max_loss, span_margin, notional}`) under the optional-column template (`results_store.py:103-115`); the per-leg sidecar `<stem>.legs.parquet` joined on `(run_id, trade_seq)`; premium-flow AGG columns (`premium_collected_paisa`, `premium_kept_pct`) |
| integration | the results-depth pass (post-hoc over finalists, the `results_depth_pass.py:486-505` fence) folds the fill-honesty classes + the attribution residual into the sidecars |
| property | **count-consistency** — `n_accepted == n_class_a + n_class_b`; `n_class_c` decomposed per failing predicate (`n_c_no_data`, `n_c_stale_bar`, `n_c_over_freeze`, + the carried-over `lot_size_unknown`/`cost_regime_unverified`/`tick_floor`), each stamped with the FIRST failing leg — the `OptionsRuleResult` `__post_init__` reconciliation with no catch-all bucket (`executor.py:602-712`, gap-fill §3.3); **new doors extend `GATE_REASON_ORDER` + `per_door_kills` in lockstep** (`run_manifest.py:91,188-193`, gap-sim cross-cutting) |
| golden | the additive-annotation guard: a seller annotation is **one additive top-level key** that refuses to overwrite a ranking field (`top100_artifact.py:613-629` "never rewrites a ranking field"; `resilience_streaks.py:939-959` `project_resilience_annotation`, double-annotation refused, additive-only property-pinned); the **P&L-attribution residual law** — `residual = dP_actual − dP_explained` is ALWAYS computed, stored, and displayed per trade (it absorbs discretization, no-surface smile/term moves, the ~−4.7% late-expiry T-anchor bias `greeks_enrich.py:464-481`, and coverage holes); a residual-dominated trade is FLAGGED, never silently normalised (gap-sim §c) |
| bench | the sidecar append is O(1)/trial; the attribution pass is O(bars-in-trade × legs) over finalists only (post-hoc, bounded) |
| stress | the REFERENCE_ONLY discipline holds under a redo: `margin_roi` / attribution / VIX-trio columns are **never rankable, never in run identity, typed non-corrupt conflict** (`results_store.py:214-227,229-235,249-278`); a value-vs-value clash on a redo raises the typed non-corrupt conflict, not silent corruption |
| mutation | the count-consistency law, the `capital_basis` token set, and the residual-flag threshold are mutants that must die |

### §1.3 The gates the capability must PASS — the shipped cascade + the NEW seller tail battery

A seller structure's sized net P&L is judged by the **shipped Stage-4 cascade** exactly as a spot combo is — with the honest observation (gates-evidence §3) that **a premium seller is the exact adversarial input** for a mean/ratio-shaped gate battery (a naked strangle prints 90%+ win rates and PF > 1.2 right up to the one gap day). The verification requirement is therefore two-part:

**(a) The capability must pass the shipped nine-filter cascade + five hard gates unchanged** (defaults verified in gates-evidence §5, `brutex/metrics/cascade.py:244-285` / `brutex/ranking/__init__.py:81-86`):

| Layer | Default (as coded) | Seller-relevance |
|---|---|---|
| hard gates | `total_trades ≥ 30`, `profit_factor ≥ 1.2`, `max_drawdown_pct ≤ 30`, reject iff `win_rate < 0.25 AND wlr < 2.0`, `max_consecutive_losses ≤ 10`, non-finite fail-closed | the mean-shaped battery a seller flatters — none reads a tail the window never sampled |
| #2 `deflated_sharpe` | DSR ≥ `0.95` | **already deflates HARDER for sellers** — a negatively-skewed leptokurtic per-trade distribution lowers the PSR via its own moments (`overfit.py:74-81`) |
| #3 `cpcv` | pass rate ≥ `0.5` over `C(16,8) = 12,870` purged+embargoed splits (`cpcv.py:94-98`) | — |
| #4-6 `spa`/`romano_wolf`/`pbo` | `α = 0.05` / FWER 0.05 / PBO ≤ `0.5` | pool-level multiple-testing defense |
| #7 `walk_forward` | pass rate ≥ `0.75` across rolling/anchored/expanding | — |
| #8 `lockbox` | 182-day held-out, ≥ 500 trades, Sharpe ≥ 0.70×training, MDD ≤ 1.5×training | — |
| #9 `monte_carlo` | entry-permutation p < `0.01`, 1000 perms | **secretly a seller gate** — theta income makes ANY entry look profitable in calm regimes; permuting entries while preserving the short-premium exit tests whether the entry filter adds anything beyond harvesting VRP (gates-evidence §3.5) |

The cascade tests reuse the **real per-trade P&L series** the results-depth trade capture already persists (`<stem>.trades.parquet`, read by `resilience_streaks.read_captured_trades:828-849`) — reconstructing series from win/loss aggregates "would fabricate statistical significance and is FORBIDDEN" (`overfit.py:21-35`, gates-evidence §5.3).

**(b) The five NEW seller tail gates are ABSENT today and land ANNOTATION-FIRST → SHADOW → gated** (gates-evidence §0/§3; each verified absent at base SHA: `grep -rEi 'cvar|expected.shortfall|tail_gate' brutex/` → 0 files). Their verification requirement is the **house record-before-gate discipline**, itself a pinned test:

| Seller gate | Status today (verified) | The NEW test it needs |
|---|---|---|
| **CVaR / Expected-Shortfall** (the highest-priority) | ABSENT — only an aspirational citation row (`MIGRATION.md:3083`) | `CVaR_α = mean of the worst ⌈αN⌉ per-trade outcomes`, an O(n log n) fold in the `tail_ratio` style (`compute.py:680-683`); golden pins the fold; **property: cannot be fooled by win-rate** (a 90%-win seller distribution with a fat left tail FAILS a CVaR ceiling while passing every mean gate) — `tail_ratio` (p95/p5) is NOT a substitute (gates-evidence §3.1) |
| **Per-year stability gate** | fold EXISTS (`win_rate_year_spread`, `resilience_streaks.py:117,125`), gate ABSENT (display-only, `:10-16`) | a threshold over the existing field (min qualifying-year P&L share / spread ceiling), reusing `YEAR_MIN_TRADES = 20` as the noise floor; golden pins a "2021+2023-calm, 2020/2022-negative" seller FAILING (gates-evidence §3.2) |
| **Regime must-survive + fragility strata** | lens EXISTS (`one_regime_wonder` at `REGIME_WONDER_SHARE = 0.8`, `combo_aggregates.py:93`; `TrendRegime`), gate ABSENT | require NON-catastrophic P&L in EVERY qualifying stratum (year × trend × VIX band — all three attributions exist per combo); the VIX bands (`<12/…/>30`, `session_lens.py:140-152`) are the canonical fragility axis (short gamma dies in the `>30` band); golden pins a calm-regime-wonder seller FAILING (gates-evidence §3.3) |
| **Extreme-event sensitivity** | attribution EXISTS (`worst5_days_json`, `event_day_pnl_json`, `WORST_DAYS_LIMIT = 5`), gate ABSENT | a ceiling on `(worst-event-day loss ÷ net P&L)` or a per-flag CVaR; **HONEST LIMIT the design must carry:** election days + weekday Budget days are UNTAGGED anywhere (`session_lens.py:26-29`) — the two most violent Indian vol days (2024-06-04 election −5.93%, 2024-07-23 Budget) are invisible to the flag set; the gate either adds a verified event calendar (own DEC) or states the blind spot (gates-evidence §3.4) |
| **The `06`/`08`/`11` blow-up fixtures** | mixed | each catalogued blow-up day gets a golden the sensitivity gate must reproduce: the intraday-reproducible ones (vol-crush at the open 3-Jun-2024, circuit-halt absent-bars 23-Mar-2020) as full goldens; the **overnight-gap-through-strikes** ones (23-Mar-2020, 4-Jun-2024, 5-Aug-2024, 7-Apr-2025) as **PARTIAL** — the engine force-closes intraday (`trade_sim.py:60-65`, `session_gap` intraday-only), so held-through exposure needs the position-lifecycle DEC (companion §7.4 matrix); the margin-expansion ones as **named ABSTAIN** (zero verified SPAN rows, `margin_history.py`); pre-2020 catastrophes (Volmageddon 2018) as **NOT reproducible** (2020 floor, `pull_controller.py:163`) — chaos fixtures assert mechanisms only, never a false historical-replay claim |

**The record-before-gate test itself** (the load-bearing discipline, gates-evidence §4): every new seller gate must ship first as a resilience-style **sidecar annotation** (compute + display + `—` abstains, ZERO run-identity impact — the `resilience_streaks` template with its own fence, `results_depth_pass.py:486-505`), then run **shadow** beside the live cascade (recording-only, the `cpcv_pass_rate` precedent `cascade.py:312-328` — "no ordering / RNG / threshold / statistic changes"), and be promoted to a rejecting gate **only by its own operator-gated DEC**. A test pins that in annotation mode the gate value is COMPUTED and DISPLAYED but the ranking / cascade verdict / `passes_gates` result is **byte-identical** to the no-annotation run (the `sweep-verify --recount` Tier-3 never-adopt discipline, `sweep_verify.py:1830-2024`; R45 diffcheck "mark SUSPECT on mismatch, NEVER replace").

### §1.4 The structure-lock (negative-pin) battery — the invariants a future activation must never break

These are **permanent** tests (the DEC-P0-drift-pin / `futures_series` evidence-pattern): each pins an invariant that a later refactor / activation must never silently break. They are the mechanical enforcement of the four honesty laws:

| # | Invariant | Pinned by (mechanism) |
|---|---|---|
| L-1 | **Spot `run_id` byte-identity** — no default-omitted structure param moves an existing spot run_id | GOLDEN `run_identity_inert` (§1.1) + a diff pin over the frozen `_GOLDEN_RUN_ID` corpus |
| L-2 | **Annotation-never-reranks** — no seller annotation / margin proxy / attribution / tail-gate-in-annotation-mode changes ranking, cascade order, or `passes_gates` | rank-order byte-untouched pin (R13, `futures/executor.py:14-16`; `top100_artifact.py:581-583` "a cascade-FAILED rank-1 STAYS rank-1 and is flagged") |
| L-3 | **Correct-or-absent atomicity** — a structure never enters with < all-legs; a partial fill is unrepresentable | the M3 all-or-nothing property (the `06` E-01 atomic-reject golden) |
| L-4 | **Defer-never-reject on open defense** — a no-print bar during open defense DEFERS, never rejects (a reject holds an undefended short for free) | the M4 R-5 stress test |
| L-5 | **×4 bad-tick screen stays OFF for option premia** — a real crash-day premium spike is never deleted | a permanent structure-lock test that option series carry NO ×4 screen (`bad_tick.py:76` never reached by option readers, `lake_source.py` has 0 bad-tick imports, companion §7.5) |
| L-6 | **Margin / margin-based-ROI / margin-based-size never rank, report as a return, or enter run identity** (undefined-risk families REFUSED in v1) | the `margin_history.py` refusal channel (`:10-23,152-173`) — `span_per_lot_paisa = None` IS a refusal window, the charge-an-unverified-margin state is UNREPRESENTABLE (`:5-8`) |
| L-7 | **Vocabulary stays at 60 bits under #2211** — no seller knob becomes a mask bit | a pin that `TOTAL_CONDITIONS == 60` (`bits.py:167`) and `IMPLEMENTED_MASK == (1<<60)−1` (`:173`) are unchanged by the slice; every IV/DTE/structure knob resolves through `canonical_trade_params`, never `NAME_TO_BIT` (`05` §3.2) |

---

## §2. THE HONEST-LIMITS REGISTER — what CANNOT be claimed, and the DATA/DEC that lifts each

This is the section the risk-first lens exists for. Every row is a claim a seller backtest is tempted to make and **must not**; the mandatory disclosure sentence is the design-doc/result-surface wording (the DEC-FILL-WORST-CASE-ONLY-001 honesty style); the "lifted by" column names the exact DATA drop or DEC that would remove the limit (and its own gate). Consolidated from companion §11, gap-sim (G1-G14), gap-fill (U1-U10), gap-data (D-A…D-G, U-A1…U-G2), and capital-sizing (U1-U15).

| # | The claim FORBIDDEN | Mandatory disclosure | Lifted by (DATA / DEC) + its gate | UNVERIFIED id |
|---|---|---|---|---|
| H-1 | **NOT "margin-realistic."** No margin number, ROI, or margin-based size may rank, report as a return, or enter run identity | "margin denominated on max-loss basis (defined-risk); undefined-risk families REFUSED — no SPAN history exists" — the `margin_history.py` refusal, extended from "annotate" to "admit" | `DEC-SPAN-HISTORY-INGEST-001` (ingest dated NSE-Clearing SPAN parameter files, per (instrument, structure_class)); **gate:** phase-0 probe of archive retention depth 2020→today — if shallow, permanent past-refusal + forward-only verified rows | G2 / U11 / U-D5 |
| H-2 | **The drawdown-to-margin SPIRAL cannot be SIMULATED — only BOUNDED and ANNOTATED.** A "margin call on date D" is a fabricated number chain | "stress-bound loss ≥ X% of a declared capital base; margin-pressure PLAUSIBLE beyond here" — never "margin call occurred" | same SPAN-ingest DEC unlocks EOD-dated bounds; the 6×-intraday recalibration + broker RMS discretion keep FORCED-EXIT TIMING approximate even then (capital-sizing §4.2) | U11 / U12 |
| H-3 | **NOT "size-realistic."** The per-leg worst-case bound holds for a MARGINAL order only unless Class-A; even Class-A does NOT bound market impact (λ is uncalibratable) | render the **Class-A survival fraction** on every surface; "worst-case-bounded against demonstrated trades, liquidity-classed — not size-realistic" | forward L1 depth capture (GDFL-class); the **2020→now window is PERMANENTLY uncalibratable** (retail-data ceiling) — λ, B stay swept identity params, never asserted sufficient | U6 / D-A3 |
| H-4 | **NOT "quote-complete" / NOT "spread-costed."** The lake stores TRADED PRINTS only — no bid/ask/depth, ever. Where the quoted spread sat outside a bar's traded range (illiquid strikes, expiry afternoon), even the worst-case bound can be optimistic | the spread-penalty axis measures **FRAGILITY, never cost** (report the breakeven `K*`); "the one corner where 'never more optimistic than reality' is not guaranteed by this data class" | forward L1 capture → a calibrated, date-scoped spread table (its own DEC, DATA-shaped like a `RegimeRow`, valid only for dates after capture start); the historical window stays spread-blind FOREVER | U-A1 / U3 / U4 / D-A1 |
| H-5 | **NOT "settle-exact."** Index cash settlement uses a PROXY of the official ~30-min-VWAP FSP with a DISCLOSED error band; the band IS the pin-risk model | "settled at the adverse plausible value inside a disclosed band; the exact NSE FSP formula is UNVERIFIED" | an **FSP-ingest DEC** (a per-(underlying, expiry-date) table from NSE settlement files); **gate:** NSE archive fetchability + the exact last-30-min formula | G3 / G7 / U-F2 |
| H-6 | **NOT "stock-option-capable" in v1.** Physical settlement + zero corporate-action / ban-list layer ⇒ stock scope REFUSED | forced pre-expiry exit OR typed reject — never a silent hold-through-delivery | a **stock-scope data package DEC** (corporate-action adjustment calendar + factor + symbol re-map; MWPL ban-list archive; per-underlying earnings; delivery-STT split) — multiple independent BLOCK-class gaps | D-E1 / D-E2 / D-E3 / G9 / U-E1 / U-E2 |
| H-7 | **NOT "held-through-the-gap."** The engine force-closes intraday (`session_gap`, `trade_sim.py:60-65`); 4 of 11 catalogued blow-ups lose their teeth in an intraday-only engine | "v1 seller sweep is INTRADAY-ONLY" on every result surface | a **position-lifecycle DEC** (held-overnight seller exposure) — separate from #2211; the overnight-gap-through-strikes blow-ups only become end-to-end with it | (companion §7.4) |
| H-8 | **NOT "vol-regime-timed" in v1.** The classic "sell when IV-rank is high" entry has NO substrate (no IV-rank series; VIX barred by Rule 3) | "families whose edge is vol-regime timing are honestly under-served in v1"; the IVR-floor param is UNAVAILABLE | the **feature-lake DEC** (M7 — a derived per-(underlying) constant-maturity ATM-IV history + IV-rank); **gate:** coverage-provenance per row + the 30-day CM wall | D-B1 / G8 / D-C2 |
| H-9 | **NOT "chain-complete."** No per-expiry strike-coverage census exists — a genuinely-missing liquid strike is unattested absence ⇒ multi-leg selection carries an unquantified survivorship bias | "multi-leg statistics read against the census verdict (PASS/HOLEY/UNPULLED); holes are attested, not silent" | the **chain-completeness census DEC** (M7 — registry × ledger × stats-manifest join; building blocks exist) | D-C1 |
| H-10 | **NOT vol-regime / skew / term-structure-featured at long tenor.** Monthly calendars / diagonals / any > 30-DTE-entry family have NO far-leg data historically (`expiry − 30d` window law, `contract_bounds.py:104,296-330`) | "> 30-DTE-entry families are historically unbacktestable at any cost unless the vendor serves long-expired chains" | a DEC widening `DEFAULT_OPTION_LOOKBACK_DAYS` (future pulls only, ~2-3× footprint) + a vendor **expired-chain depth probe** (historical — UNVERIFIED feasible; if the probe fails, permanently unbacktestable) | D-C2 / U-C1 / U-C2 |
| H-11 | **NOT an SLA.** No options trials/s figure exists; the spot 80-150K trials/s envelope does NOT transfer | "no wall-clock claim" until a prototype bench + budget re-derivation | a **prototype bench DEC** over real option 1m data + a `TRACK1_PERFORMANCE_BUDGET`-style re-derivation (the single largest open feasibility question) | G5 / U-D7 |
| H-12 | **NOT "cost-complete" pre-2024-10-01.** Option ETC/STT rows VERIFIED only from 2024-10-01 | pre-boundary trades are counted `COST_REGIME_UNVERIFIED` refusals, never a fabricated rate | source the pre-boundary ETC circulars (the DEC-ETC-REGIME-HISTORY-001 refusal-row pattern); the exercise-STT-on-non-traded-contract question (G6) flips the exercise-vs-sell verdict | G6 |
| H-13 | **NOT "tail-gated" today.** CVaR/ES, per-year-stability, regime-must-survive, extreme-event-sensitivity are ABSENT as gates | landing-elsewhere dependencies, shadow-first; the design NEVER pretends they exist; election/Budget days are UNTAGGED (the two most violent vol days invisible) | `DEC-OPTSELL-TAIL-GATES-00N` (each gate promoted individually, each superseding an in-code "never a ranking gate" stance) + an **event-calendar DEC** for the election/Budget blind spot | (gates-evidence §3.4) |
| H-14 | **NOT a historical-replay of pre-2020 catastrophes.** Volmageddon-2018 / IL&FS-2018 / 2019-election are out of reach (2020 floor, `pull_controller.py:163`) | chaos fixtures assert MECHANISMS only, never a false historical-fidelity claim | structurally unclosable (the 2020 pull floor is a scope decree, CLAUDE.md §2 Rule 3) — permanent | — |
| H-15 | **NOT calibrated for an option-appropriate outlier screen.** The ×4 bad-tick screen is INVALID for premia and is OFF; no premium-calibrated screen exists | option series run unscreened-for-spikes (only the universal finiteness/positivity/ordering insane-arm applies) | NEW premium-calibrated outlier-screen design (own work); must structurally exclude the ×4 arm (L-5) | D-D.5 |
| H-16 | **Cash-secured-put max-loss basis UNDERSTATES leveraged ROI ~6-8×** vs real SPAN margin (ruling R-1) | "admitted under `capital_basis=max_loss` with the understatement disclosed" — a finite exact bound, the same conservative side the engine already chooses | the SPAN-ingest DEC (H-1) moves it to the `span_margin` basis; the ~6-8× magnitude is itself UNVERIFIED | G1 |
| H-17 | **The per-leg-independent adverse extreme can EXCEED the worst printable NET package price** in a minute (strictly more pessimistic than a real basket order) | a mandatory sentence: "per-leg worst-case is strictly MORE pessimistic than any real simultaneous package execution … never optimistic" | a co-timed tick-data study — out of reach at 1m granularity; the direction only STRENGTHENS the conservative bias, so it is a disclosure item, not a correctness risk | G12 |
| H-18 | **U1 dependency:** the whole fill bound assumes Groww 1m candles are OHLC-of-TRADES (not quote/mid) and volume-0 bars are carried prints | the §1 bound is conditional on U1; the repo already reasons on this basis (`futures/executor.py:35-36`) | vendor doc confirmation or a credentialed probe comparing 1m bars vs a known trades tape | U1 |

**The honest product claim** (companion §11, the sentence the whole register defends): *"worst-case-bounded against demonstrated trades, liquidity-classed, spread-stressed — not size-realistic, not quote-complete, not margin-realistic, not settle-exact, intraday-only, tail-gates shadow-first, index-only."* Anything stronger is a Golden-Rule-#10 violation.

---

## §3. THE PHASED DEC ROADMAP — the ordered, operator-gated ladder

The capability activates through a strictly-phased ladder of **disjoint** operator-gated DECs. The whole ladder is **reversible** — a later DEC can flip any ruling; nothing here is load-bearing beyond the capability slice. The **three-gates-are-disjoint law** (Golden Rule #3, companion §10) is the spine: the *capability slice* (#2211), the *vocabulary extension*, and *options-into-engine-scope* are three separate DECs, and none by itself implies the next.

### §3.1 The dependency DAG (what must precede what)

```
        [#2210]  research pair lands (this chapter + 05/06/08-11)
            │
            ▼
        [#2211]  DEC-OPTSELL-STRUCTURE-EXECUTOR-001  ── the capability slice, DEFAULT-OFF
            │      (grammar · walker · per-leg SELL+defense-fill · fill-honesty classes ·
            │       V1 max-loss capital gate · settlement proxy · counters · additive results)
            │       vocabulary UNCHANGED @ 60 bits · engine scope UNCHANGED (spot-only)
            │
   ┌────────┼─────────────────────────────┐   (Phase-2: ADDITIVE, DATA-gated, mutually independent;
   ▼        ▼                              ▼    NEITHER is on #2211's critical path)
[FEATURE- [SPAN-HISTORY-              [FSP-INGEST / stock-scope
 LAKE      INGEST-001]                 data package / event-calendar]
 DEC]      unlocks undefined-risk       lift H-5/H-6/H-13 blind spots
 unlocks   families' capital
 vol-      admissibility
 regime          │
 families        │
   │             │
   ▼             │
[VOCAB-EXTEND-00N]  ── 60→N≤92 bits  (STRICTLY AFTER the feature-lake; still a VOCAB DEC, not scope)
   │             │
   └──────┬──────┘
          ▼
   [EDGE-STUDY-00N]  ── the measured seller-edge study (tail study 08/11 + on-box VRP measurement)
          │
          ▼
   [TAIL-GATES-00N]  ── promote each shadow annotation to a rejecting gate (individually)
          │
          ▼
   [OPERATOR ACTIVATION RULING]  ── options INTO engine sweep-scope = its OWN scope-expansion DEC
          │                          (Golden Rule #3 — separate from #2211 AND from VOCAB-EXTEND)
          ▼
   [optional: GDFL forward-capture]  ── L1 spread calibration (forward-only, never historical)
```

**Reconciliation note (honest).** The brief sketches "feature-lake + condition-vocab-extension DEC → the default-OFF structure-executor slice (#2211)". The design of record (chapter `05` + companion §10) instead ships **#2211 first**, because the READY-today family set — index same-expiry, ≤ 30 DTE, defined-risk (gap-data §D-G group G1a, the "best-served group") — is honest end-to-end on the capital + data axes **without** the feature-lake (its capital basis is exact max-loss arithmetic, its data is the 30-day-window-covered premium + stored Greeks, gap-sim §a "the defined-risk unlock inverts the dependency order"). The feature-lake is therefore drawn as an **additive Phase-2 DATA DEC**, not a #2211 prerequisite. Both orderings are defensible: **#2211-first minimizes the critical path** (ship on the exact-capital families, add the feature-lake to unlock vol-regime families later); **feature-lake-first invests in the data substrate before the executor**. The panel rules **#2211-first** (the reuse-maximal / minimal-critical-path reading), reversibly — the vocab-extension in either case is STRICTLY AFTER the feature-lake (it needs the prebuilt feature for O(1)/bar).

### §3.2 Per-DEC card — what it adds, what stays INERT, the gate it must pass

| DEC | What it ADDS | What stays INERT / UNCHANGED | The gate it must pass |
|---|---|---|---|
| **#2210** `DEC-OPTION-SELLING-RESEARCH-001` (this pair) | the research + design-readiness docs (`05`-`11` + this chapter) | ALL of `brutex/`/`rust/`/`tests/` — docs-only branch | doc-lint / citation-accuracy; every `path:line` resolves; no engine file touched |
| **#2211** `DEC-OPTSELL-STRUCTURE-EXECUTOR-001` (the capability slice, its own PR) | the 7 modules (M1-M6, M8; M7 excluded), the per-leg SELL + defense-fill + **defer-never-reject** laws, the fill-honesty classes A/B/C, the **V1 max-loss capital GATE** (defined-risk families exact; undefined-risk REFUSED), the settlement proxy + band, the additive default-omitted run-identity keys, the additive REFERENCE_ONLY result columns + counters; every seller analytic ships as a display-only annotation | **engine scope (spot-only)**, **vocabulary (60 bits)**, every existing spot `run_id` (byte-identical, L-1), ranking / cascade order (L-2); ships **DEFAULT-OFF / INERT** — supersedes DEC-EXEC-008 for the structure track ONLY | the full §1 pyramid: 100% line+branch on M1-M6/M8, mutation ≥ 85% zero-survivor, the L-1…L-7 structure-locks, the shipped nine-filter cascade unchanged, the shadow/annotation-first discipline (§1.3(b)) |
| **`DEC-OPTSELL-FEATURE-LAKE-001`** (additive DATA; Phase-2) | the derived IV/chain feature lake (M7) — CM-ATM-IV history + IV-rank/percentile + skew + term slope + PCR, per-row coverage provenance — and the **chain-completeness census** (registry × ledger × stats-manifest → PASS/HOLEY/UNPULLED); unlocks the vol-regime-gated families (H-8) + attests holes (H-9) | run identity (features feed PARAMS, not the mask, until the vocab DEC); ranking (annotation until a gate DEC) | the M7 pyramid: **O(1)/bar** read (no inline chain scan), coverage-provenance-per-row property, the ×4-screen-stays-off-for-premia structure-lock (L-5); its own freshness guard (the #1932 mold) |
| **`DEC-SPAN-HISTORY-INGEST-001`** (additive DATA; Phase-2, independent) | dated NSE-Clearing SPAN parameter rows → V1 refusal windows convert date-by-date → the naked/undefined-risk families become capital-admissible where verified rows exist; the dated regime steps (+2% expiry ELM 2024-11-20, calendar-benefit removal 2025-02) land as VERIFIED rows | the max-loss basis for defined-risk families (unchanged); the margin proxy stays annotation-only (L-6); the drawdown-margin spiral stays BOUNDED not simulated (H-2) | **phase-0 archive-depth probe** (2020→today retrievable? — G2/U11): if shallow, permanent past-refusal + forward-only verified rows; the `margin_method` audit stamp; a validation pass vs live broker-calculator snapshots before any "conservative" label |
| **`DEC-OPTSELL-VOCAB-EXTEND-00N`** (STRICTLY after the feature-lake) | 60 → N ≤ 92 seller condition-bits (RV/compression, IV-term, event-proximity) — the u128 kernels ALREADY exist and are the active ≥64-bit path (`combo_mask.py:29-44,161,168`, `WideKernelUnavailableError` never a silent degrade `:44,62`) | **engine scope** (a vocab DEC does NOT admit options as instruments — that is a THIRD, separate DEC); every VIX/IV-derived bit is BLOCKED by the Rule-3 REFERENCE_ONLY bar unless from a non-VIX source | the per-bit prerequisite (all three or the bit does not ship, `05` §3.4): (1) the indicator in the shared `rust/trading-core` crate (bit-identical for BruteX + TickVault); (2) an `indicator_version` bump (a verified run-identity input, `combo.py:253`); (3) the feature-lake prebuild for O(1)/bar |
| **`DEC-OPTSELL-EDGE-STUDY-00N`** (a measured study, not a code change) | the pair's **tail study** (chapters `08` seller-blowups / `11` expiry-day-tail-study fixtures re-run over the real lake) + an **on-box VRP measurement** (the seller edge = variance-risk-premium harvest, measured on the 2020→today lake, `vrp-evidence.md`) — the evidence base for the activation ruling | nothing in `brutex/` (a measurement + report); the tail gates stay in shadow/annotation mode | reproduces the catalogued blow-up fixtures (the §1.3(b) matrix); names the intraday-only / margin-abstain / pre-2020-out-of-reach honest limits (H-7/H-2/H-14) |
| **`DEC-OPTSELL-TAIL-GATES-00N`** (promotes annotations to gates) | each named seller gate (CVaR/ES, per-year stability, regime-must-survive, extreme-event-sensitivity) promoted **individually** from shadow annotation to a rejecting gate | the shipped nine-filter cascade defaults (each promotion is additive); a promotion consciously supersedes an in-code "never a ranking gate" stance (`resilience_streaks.py:10`, `session_lens.py:38`, `combo_aggregates.py:91-92`) | **N recorded-only shadow runs** before any gate rejects (the `cpcv_pass_rate` record-before-gate + `sweep-verify --recount` never-adopt discipline); the golden that a fooled-by-win-rate seller FAILS the CVaR gate; the event-calendar DEC for the election/Budget blind spot (H-13) |
| **OPERATOR ACTIVATION RULING — options INTO engine sweep-scope** (its OWN scope-expansion DEC) | admits options as sweep INSTRUMENTS (the third disjoint gate) — this is the ONLY DEC that changes `is_engine_sweep_symbol` / the DEC-ENGINE-SPOT-ONLY-001 posture | until this lands, the whole capability is a TRANSLATION/annotation layer over a spot-only engine (every prior DEC leaves the engine spot-only) | Golden Rule #3 — a conscious, ledgered scope decree, NEVER a silent expansion; the corporate-action gate generalized from spot to option chains (`sweep_scope.py:28-37,62-70`) |
| **optional: `DEC-GDFL-L1-CAPTURE-00N` / FSP-ingest / stock-scope package** | forward-captured L1 spreads (calibrate the spread axis from cost-fragility to a real cost table, forward-only) / dated FSP rows (settle-exact) / the stock-option data package (lift H-6) | the historical 2020→now window stays spread-blind (forward-only, per the in-repo GDFL verdict, `VERDICT.md:14-17`) | each its own DATA-availability probe + refusal-until-calibrated posture (the ETC-regime pattern) |

### §3.3 The shadow-mode / annotation-first / never-adopt convention (the house law, verbatim)

Every seller gate, margin proxy, attribution column, and tail statistic lands through the SAME three-step ladder the repo already triply-precedents (gates-evidence §4):

1. **Annotation-first** — compute + display + `—` abstains, ZERO run-identity impact, its own sidecar + fence (the `resilience_streaks` template; the additive-annotation guard `top100_artifact.py:613-629` refuses to overwrite a ranking field; a fold failure degrades to an honest all-`None` block and never drops the finalist).
2. **Record-before-gate** — run SHADOW beside the live cascade, recording-only (the `cpcv_pass_rate` precedent `cascade.py:312-328` — "no ordering / RNG / threshold / statistic changes"), for N runs, surfacing the value while the verdict is byte-identical to the no-annotation run.
3. **Never-adopt-on-divergence** — the `sweep-verify --recount` Tier-3 shadow re-execute + byte-diff (`sweep_verify.py:1830-2024`) and the R45 diffcheck "mark SUSPECT on mismatch, NEVER replace the production value" — a shadow gate that would flip a verdict is SUSPECT-flagged, never auto-adopted.

Promotion of any annotation to a **rejecting** gate is a separate operator-gated DEC that consciously supersedes the in-code "never a ranking gate" stance — matching R13 / ranked-never-hidden / the sweep-verify never-adopt family. **No named seller gate is ever presented as present today.**

---

## §4. Consolidated UNVERIFIED register (compact — full provenance in the section registers)

Every id below blocks a specific claim above; the closer is the exact probe/data/DEC. Nothing here is claimed with current data.

| id | Item (what cannot be claimed) | Closer |
|---|---|---|
| U1 | Groww 1m candles are OHLC-of-TRADES (the whole §1 fill bound depends on it); volume-0 bars are carried prints | vendor doc confirmation / credentialed probe vs a known trades tape (gap-fill U1) |
| G1 | Cash-secured-put max-loss ~6-8× real SPAN (the H-16 understatement magnitude) | a real SPAN calc vs strike×lot (gap-sim G1) |
| G2 / U11 | NSE SPAN-file public archive retention depth 2020→today (gates the SPAN-ingest DEC) | probe `nsearchives.nseindia.com` / member-FTP — phase-0 of DEC-SPAN-HISTORY-INGEST-001 (capital-sizing U11) |
| G3 / U-F2 | Exchange daily FSP historical dataset availability (H-5 lift) | NSE Clearing settlement-price archive probe |
| G5 / U-D7 | Options-structure trials/s + per-leg-bar cost (the H-11 no-SLA) | prototype bench over real option 1m data + a `TRACK1_PERFORMANCE_BUDGET`-style re-derivation |
| G6 | Whether NSE levies ETC/SEBI/IPFT on EXERCISED contracts (flips the exercise-vs-sell verdict, M6) | a real exercised-position contract note / NSE FATAX circular |
| G7 | Exact index FSP formula (sets the M5 error-band width) | NSE Clearing settlement page + contract spec |
| G8 | Production-lake Greeks overlay coverage fraction (gates E-DELTA/E-EM, M2; the feature-lake historical quality) | `brutex greeks-backfill --dry-run` census on the operator lake |
| G11 | Put-calendar max-loss > net debit (the below-intrinsic European-put case, family-table exclusion) | worked bound at prevailing rates (gap-sim G11) |
| G12 | Per-leg-independent adverse extremes can exceed the worst printable NET package price (H-17) | a co-timed tick-data study — out of reach at 1m; disclosure-only, strengthens conservatism |
| U3 / U4 / U-A1 | Indian option quoted-spread magnitudes (all H-4 spread claims) | forward L1 capture; hosts 403 the research proxy (gap-fill U3) |
| U6 / D-A3 | λ (volume-floor multiple) + B (band width) values that make Class-A "safe" (H-3) | depth/quote data; UNCALIBRATED by construction until then |
| U-C1 | Groww serves 2020-era EXPIRED option chains retroactively (the H-10 far-leg + the blow-up-day fixtures) | a credentialed `get_expiries`/`get_contracts`/candles depth probe on a 2023 expired monthly (gap-data U-C1) |
| U-C2 | Footprint delta of widening `DEFAULT_OPTION_LOOKBACK_DAYS` 30→60/90 (the ~2-3× figure is the #1145 estimate, not measured) | `brutex lake` census / `du` before/after a widened trial pull |
| U-D1 | Groww option-candle volume UNIT (contracts vs underlying units) — the repo's column doc says "Traded contracts", the vendor doc says only "volume" | vendor clarification / live cross-check vs NSE bhavcopy |
| D-E1 / D-E2 / D-E3 / G9 | NSE corporate-action F&O adjustment rules + MWPL ban-list archive + earnings/event calendar + delivery-STT split (all block stock scope, H-6) | NSE circulars / SEBI MWPL framework / a vendor event-calendar surface |
| U-E3 / election-cal | Election + weekday-Budget days UNTAGGED anywhere (the H-13 extreme-event blind spot) | a verified event calendar, its own DEC |
| EU-03 | Post-2026-08-03 F&O session shape (15:40 close, 15:10-15:40 settle VWAP) — affects the M5 last-30-min window + the Greeks T-anchor | NSE circulars on CAS + revised F&O timings; post-Aug-2026 NSE Clearing settlement page |
| U-F1 | The European-style refutation's primary bytes (deletes the American early-exercise axis — no data needed, but reconciles the repo's `STYLE_AMERICAN_APPROX` stamp) | fetch SEBI CIR/DNPD/6/2010 + NSE Clearing settlement-mechanism page |

---

**Prepared by the verification-and-phasing author for the option-selling design panel. Design deliverable only — no repo file modified outside this doc; nothing here activates anything.** Repo citations resolve into `/home/user/bruteX` as read via the named research sections on 2026-07-19/20; `[sec:…]`-style references are to `scratchpad/sections/` and the committed chapters `05`/`06`/`08`-`11` + the risk-first engine companion; market facts carry their section's provenance; UNVERIFIED items are collected in §4 and the section registers. The sweep engine stays **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001) and options stay **REFERENCE_ONLY** until an operator-gated SELL DEC/ADR adopts these laws — and even then, the capability ships **DEFAULT-OFF, shadow-first**, with any entry of options into engine sweep-scope or the 60-condition vocabulary a **SEPARATE** future operator-gated DEC (Golden Rule #3).
