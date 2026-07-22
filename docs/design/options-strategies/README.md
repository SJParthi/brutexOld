# Option-Selling Readiness Tree — front door & operator exec summary

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code ships in this doc.**
> The sweep engine stays **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001, MIGRATION.md #1790;
> `brutex/universe.py:379` `is_engine_sweep_symbol` admits exactly the 3 spot indices)
> and options stay **REFERENCE_ONLY** lake data (CLAUDE.md §2 Rule 3;
> `brutex/data/fno_candle_lake.py:66-90`, 1-minute-native per DEC-OPTIONS-PREBUILD-RETIRE-001).
> The capability described here ships **DEFAULT-OFF / INERT**, and its **activation**, a
> **SPAN-history ingest**, and **any entry of options into the engine's brute-force
> sweep-scope or into the 60-condition vocabulary** are **SEPARATE future operator-gated
> DECs** (Golden Rule #3). Nothing in this tree authorizes activation, a scope change, or a
> vocabulary change.
>
> **Date:** 2026-07-20
> **Provenance:** multi-agent design panel, docs-only branch
> `claude/options-strategies-design`, part of **DEC-OPTION-SELLING-RESEARCH-001 (#2210)**.
> The future default-OFF code slice this tree specs is
> **DEC-OPTSELL-STRUCTURE-EXECUTOR-001 (#2211, its own PR)**.

---

## 1. Thesis — the premium *is* the crash compensation, and it can be a first-class citizen

Option **selling** harvests the **variance-risk premium**: implied variance persistently
exceeds subsequently realized variance on equity indices, so the writer is paid the spread
on average (Carr & Wu 2009 *RFS* 22(3); Bakshi & Kapadia 2003 *RFS* 16(2) — chapters `08`/`10`).
But that premium is **not free money — it is compensation for bearing jump/crash risk**: the
edge concentrates exactly where the danger concentrates, and decades of small wins fund rare
catastrophic losses, so a short-vol book can print a multi-year Sharpe > 2 and still carry
negative full-cycle expectancy if the sample missed its tail (chapter `08` §1; the
Broadie–Chernov–Johannes warning is UNVERIFIED this session). The design claim of this tree is
narrow and load-bearing: **BruteX can make selling *structures* — condors, spreads, flies,
strangles, cash-secured puts — first-class drill-down brute-force citizens integrated with the
ENTIRE existing machinery** (the 60-bit mask vocabulary reused verbatim as the entry filter,
the worst-case fill law applied per leg, the Stage-4 cascade, the additive run-identity and
results columns, the annotation-never-reranks display discipline). Across the **99
deduplicated reuse-matrix rows** of the inventory map, **~half are VERBATIM, ~a quarter
EXTEND** under laws the repo documents, and **~a quarter are NEW-NEEDED**, concentrated in four
clusters — chain infrastructure, the multi-leg structure layer, the expiry/margin lifecycle,
and the three trade-re-derivation twins (chapter `03` §1.7). Every strategy is described as a
**FUTURE, operator-gated engine citizen**, shipping DEFAULT-OFF/REFERENCE_ONLY until an
operator activation DEC — nothing here proposes silent scope expansion.

**The four honesty laws the whole tree defends** (each an *existing* repo invariant, not a new
promise — chapter `05` §0, chapter `07` §0):

1. **Correct-or-absent** — a structure that cannot be priced, sized, or settled honestly
   **refuses atomically and is counted**, never simulated approximately (the `RejectReason` +
   count-consistency discipline, `brutex/options/executor.py:155-184`, `:602-712`).
2. **Worst-case only** — every fill takes the adverse intra-bar extreme; the product-wide fill
   law admits no other mode (DEC-FILL-WORST-CASE-ONLY-001; `brutex/engine/trade_sim.py:28,197,613-798`).
3. **Additive identity** — new run-identity keys are **default-omitted** so every existing spot
   `run_id` stays byte-identical (`canonical_trade_params`, `brutex/engine/sweep_run.py:719-809`).
4. **Never a ranking gate without its own DEC** — every seller analytic lands first as a
   display-only annotation, `—` abstains, zero run-identity impact
   (`brutex/engine/resilience_streaks.py:10-16`).

---

## 2. The doc index — read in this order

| Ch | Title | One-sentence coverage | Depends on / feeds |
|----|-------|-----------------------|--------------------|
| **01** | Options-strategy taxonomy | The definitive merged family catalogue (directional/covered/spreads · vol/multi-leg/time · dynamic families) + the `family → {side, type, strike-selector, expiry-tier, ratio, management}` engine-encoding scheme. | root (definitional) → feeds `03`,`04`,`05` |
| **02** | Indian F&O microstructure | The verified-facts register 2020→2026 — expiry-regime timeline, strike grids, lot sizes, settlement/STT/exercise, margins/limits/bans/freeze/circuits — that a future engine must model. | sourcing root → feeds `04`,`05`,`09` |
| **03** | BruteX inventory & reuse map | The master reuse matrix (99 rows: lake · Greeks · executors · engine · results · quality) with VERBATIM / EXTEND / NEW-NEEDED / LEAVE verdicts — what exists, extends, or must be built. | reads the repo → feeds `04`; consumed by `05` |
| **04** | Gap analysis | The distance from ship-today to an honest options backtest — data gaps (`D-*`), simulation gaps (`S-*`), fill-realism (`F-*`) — each with cost and severity (`BLOCK`/`DEGRADE`/`ok`) + the consolidated honest-limits register. | consumes `02`+`03` → feeds `05`,`07` |
| **05** | The option-selling engine design | The integration-first engine: the `StructureLeg`/`OptionStructure` grammar, mask composition, the multi-leg `trade_sim.py`-sibling walker, per-leg worst-case fills, and the honest per-trial complexity (**no options trials/s SLA exists**). | synthesizes `01`§h+`03`+`04` → consumed by `07` |
| **06** | Edge-case register | 16 definitive edge cases (E-01…E-16) — scenario · families hit · detection signature · correct-or-absent handling law · honest limits · test types — the test-plan seed. | merges edge research → feeds `05`,`07` |
| **07** | Verification & phasing | How it is PROVEN (the module-by-module pyramid M1–M8, seven test types, 100%-line+branch + mutation gates) and how it SHIPS (the ordered operator-gated DEC ladder) + the consolidated honest-limits register (H-1…H-18). | consumes `05`+`06` → the shipping spine |
| **08** | Seller blow-ups & tail events | What the worst days did to short premium and what an honest backtest must reproduce — the *payment schedule* of the strategy class, each with backtest-reproduction requirements. | tail evidence → feeds `05` tail battery + the edge-study DEC |
| **09** | Capital, margin & sizing | How the `02` §4 margin facts become sizing math; the shipped `margin_history.py` REFUSAL channel; the SPAN-history gap that BLOCKS undefined-risk families. | consumes `02`§4 → feeds `05`§5 + the SPAN DEC |
| **10** | Seller edge & execution evidence | The variance-risk-premium literature review, the Indian counterparty, and defense-time fill realism (F-A…F-E execution-realism liquidity classing). | VRP + fill evidence → feeds `05` + the edge-study DEC |
| **11** | Expiry-day tail study | A reproducible, stdlib-only bhavcopy harness measuring per-expiry-day short-premium P&L, every SL fill **bracketed** optimistic↔pessimistic (the DEC-FILL-WORST-CASE-ONLY-001 law) — the measured naive-vs-honest gap. | the measured study → feeds `05`§7.4 + the edge-study DEC |

---

## 3. Operator exec table — what is data-ready TODAY (plain English)

Read this first. "Data-ready TODAY" = whether the **shipped repo substrate + the
REFERENCE_ONLY lake** can honestly backtest the family under the four laws — **not** that any
engine ships (nothing does; the engine stays spot-only). Every family is **first-class in the
grammar** but the capital/data axes decide v1 admissibility (chapter `05` §2.4; chapter `07` §2).

| Strategy-family group | Data-ready TODAY? | What unlocks / widens it (DEC or DATA) | Honest limit (never claim beyond this) |
|---|---|---|---|
| **Index defined-risk** — iron condor, iron butterfly, bull-put & bear-call spreads (index, ≤30 DTE, same expiry) — **the core v1-admissible defined-risk spreads** | **partial** — priceable end-to-end on the 30-day-window premium + stored Greeks; **exact max-loss capital basis** (no SPAN needed). The best-served group. | the **#2211 capability slice** activates it DEFAULT-OFF; the **feature-lake DEC** later unlocks vol-regime (IV-rank) timing (H-8) | no IV-rank series today ⇒ "sell when IV is high" entries have **no substrate** (H-8); no chain-completeness census ⇒ missing liquid strikes are **unattested absence / survivorship bias** (H-9); **no SLA** — no options trials/s figure exists (H-11); pre-2024-10-01 option ETC = counted `COST_REGIME_UNVERIFIED` refusal (H-12) |
| **Index undefined-risk** — naked short straddle / strangle / front-ratio / jade lizard (index) | **no — REFUSED in v1 on the capital axis** (premium path is measurable; the *sizing* is not) | **DEC-SPAN-HISTORY-INGEST-001** (dated NSE-Clearing SPAN files → converts refusal windows to admissible date-by-date) | **NO SPAN margin history exists anywhere** — `margin_history.py` is one all-refusal row from 2020-01-01 (`brutex/engine/margin_history.py:12-23,160-173`); the drawdown-to-margin **spiral can only be BOUNDED, never simulated** (H-1/H-2); archive retention depth 2020→today is **UNVERIFIED** — if shallow, permanent past-refusal + forward-only rows |
| **0DTE selling** — same-day expiry structures, time-anchored entries | **partial** — 1-minute-native lake exists; **iron fly = admissible, straddle/strangle = refuse** (chapter `05` §2.4 row 12) | same as its base family (defined-risk fly rides #2211; naked legs wait on SPAN); an **FSP-ingest DEC** tightens settlement (H-5) | the last ~10 min before expiry is `AT_EXPIRY`-NULL by design, so a **delta-selected 0DTE strike is unresolvable** there (chapter `05` §2.2); expiry-afternoon spreads gap violently and 1-minute OHLC mis-times fills exactly when trades matter most (chapter `04` §2.3(iv)); settlement is a **PROXY with a disclosed band**, not the exact NSE FSP (H-5) |
| **Covered call / cash-secured put** (index; the covered/collar body uses the **index FUTURES leg**, not the underlying) | **cash-secured put = YES** (admitted on `capital_basis=max_loss`); **covered call = no — REFUSED** (naked-class downside, UND) | the **SPAN-ingest DEC** moves the CSP to a `span_margin` basis and opens the covered-call downside; covered-call `B:FUT` leg is index-futures-covered by construction | cash-secured-put max-loss basis **UNDERSTATES leveraged ROI by a large multiple (magnitude UNVERIFIED, G1) vs real SPAN** (H-16 / ruling R-1); the covered-call downside is DEFINED-LARGE (downside), but the covered futures leg is SPAN-margined ⇒ not v1-capital-admissible without SPAN history ⇒ v1 refuse (chapter `05` §2.4 rows 7/8) |
| **Stock-option selling** — covered-call-like / CSP-like / verticals on single stocks | **no — REFUSED in v1** | a **stock-scope data-package DEC** (corporate-action calendar + factor + symbol re-map · MWPL ban-list archive · per-underlying earnings · delivery-STT split) — multiple independent BLOCK-class gaps | **physical settlement** (delivery risk the intraday engine cannot model) + **zero corporate-action / ban-list layer**; stock options are **monthly-only** in India (no weekly-track tokens); the executor's `LakeOptionBarSource` maps only the 3 indexes today (H-6) |

**The honest product claim, verbatim (chapter `07` §2):** *"worst-case-bounded against
demonstrated trades, liquidity-classed, spread-stressed — not size-realistic, not
quote-complete, not margin-realistic, not settle-exact, intraday-only, tail-gates
shadow-first, index-only."* Anything stronger is a Golden-Rule-#10 violation.

---

## 4. Evidence-class legend (four classes)

Every claim in this tree carries exactly one tag. The research container has **NO production
lake** (verified in-session: `BRUTEX_LAKE_ROOT` unset, `~/.brutex` absent — chapter `08` §2,
chapter `09`), so class 3 is a **PENDING-ON-BOX recipe**, never a number.

| Class | Tag | Meaning | How a later reader closes / confirms it |
|---|---|---|---|
| **1 — Repo evidence** | `[REPO path:line]` | Verified by reading the cited file at the cited line in this checkout at the base SHA below. | re-read the file at the cited line at your own checkout SHA |
| **2 — Externally sourced** | `[WEB url]` / `[EXT-SNIPPET url]` | Externally verified with a URL; `EXT-SNIPPET` = surfaced via search-result snippets ONLY (the page returned HTTP 403 through this environment's proxy — SECONDARY, never quote onward as page-verified). Most SEBI/NSE/Clearing/Zerodha hosts 403 the proxy. | fetch the primary circular/PDF with normal egress |
| **3 — Lake-measured (PENDING-ON-BOX)** | `[MEASURED]` (this container) / **PENDING-ON-BOX** | `[MEASURED]` = a command + its output shown from THIS container (environment facts only). **PENDING-ON-BOX** = a number that must come from OUR lake — **not measurable here**; the doc ships the measurement recipe instead (e.g. `Catalog.get_bars`, chapter `08` §2). | run the stated recipe on the production box (M4-Pro, CLAUDE.md §8) |
| **4 — Unverified** | `UNVERIFIED` | Cannot be claimed with current data; the tag names exactly what would verify it and resolves in each chapter's consolidated register. | perform the named verification, then re-tag |

---

## 5. Provenance & scope footer

- **Research umbrella:** DEC-OPTION-SELLING-RESEARCH-001 (**#2210**) — this docs-only tree
  (`docs/design/options-strategies/`, chapters `01`–`11` + this front door). Ships nothing
  executable; the doc-lint / citation-accuracy gate is that every `path:line` resolves and no
  `brutex/` · `rust/` · `tests/` file is touched.
- **Future capability slice:** DEC-OPTSELL-STRUCTURE-EXECUTOR-001 (**#2211**, its own PR) — the
  DEFAULT-OFF / INERT structure executor (grammar · multi-leg walker · per-leg SELL + defense
  fills · V1 max-loss capital gate · settlement proxy · additive results). It supersedes
  DEC-EXEC-008 **for the structure track only**; vocabulary stays 60 bits; engine scope stays
  spot-only. The Phase-2 DATA/DEC ladder (feature-lake · SPAN-history · vocab-extend 60→N≤92 ·
  edge-study · tail-gates · options-into-scope activation) is enumerated in chapter `07` §3.
- **Scope firewall (Golden Rule #3):** admitting options as sweep **instruments** is the ONE
  DEC that would change `is_engine_sweep_symbol` / the DEC-ENGINE-SPOT-ONLY-001 posture — a
  conscious, ledgered scope-expansion ruling, **separate** from both #2211 and the vocab
  DEC, and NEVER a silent expansion. Until it lands, the entire capability is a
  translation/annotation layer over a spot-only engine.
- **Joint vehicle:** draft **PR #1725** carries this research pair, with the reproducible
  bhavcopy expiry-day tail study as **chapter `11`**.
- **Base SHA — how to resolve every `path:line` citation.** Chapters `01`–`10` + this
  README pin every `brutex/` / `rust/` / non-design `docs/` `path:line` citation to
  `184a48a4abb0ec2fd61906499a6a799a752abd7e` (= the `origin/main` tip the branch was cut
  from). **Resolve each citation with `git show 184a48a4:<path>`, NOT the head working
  tree.** The branch head has DRIFTED from the base: the two `origin/main` main-sync merges
  (`dbf2ee6`, `ade2427`) shifted line numbers in many cited sources (e.g.
  `sweep_run.py canonical_trade_params` :719→:782; `catalog.py get_bars` :477→:501;
  `nse.py`, `ingest.py`, `parquet_writer.py`, `timeframe_materialize.py`,
  `spot_daily_resample.py`, `docs/vendors/gdfl/INGEST_DESIGN.md` likewise).
  `git diff --name-only 184a48a4…HEAD` returns **431** files (the design tree + the branch's
  own MIGRATION.md #2210 ledger commits + the two main-sync merges' content) — **not**
  design-only. Chapter `11` (STUDY.md) carries its OWN citation basis (declared in STUDY §0)
  and is NOT covered by this base-SHA pin.
