# Shared Indicator + Vocabulary Core — Design Proposal (BruteX ↔ TickVault)

> **Status:** 🟢 **ACCEPTED — Option 1 (shared Rust crate), phased rollout**
> (operator decision 2026-06-03: *"build the common rust 52 vocab which can be
> used by both tickvault and brutex"*). Ledgered as **DEC-SHARED-CORE-001**
> (`docs/MIGRATION.md` #437). **Phase 0 (the golden answer-key) has shipped** in
> this PR; the `CLAUDE.md` self-containment reversal + a paired TickVault-repo
> decision land at **Phase 2** (when BruteX actually depends on the crate). A
> future ADR-0003 will formalise the cross-repo governance once the crate exists.
>
> **Author:** Claude Code session (2026-06-03). **Reviewed by:** (pending §9
> 4-agent hostile pass — see §8).
>
> **Golden Rule #1 (no hallucination):** every claim about TickVault below was
> read from its public `github.com/SJParthi/tickvault` tree + `CLAUDE.md` on
> 2026-06-03; every claim about BruteX traces to a file in this repo. Anything
> unverified is marked **UNVERIFIED**.

---

## 1. The goal (in one sentence)

> *"The indicators and the strategy/condition vocabulary must live in **one**
> place, so that adding a new indicator/condition/strategy — or changing an
> existing one — automatically and bit-identically reaches **both** the BruteX
> backtester and the TickVault live engine, with no copy-paste and no drift."*

**Why it matters (the real risk this kills):** if the backtest computes a
signal one way and the live engine computes it even slightly differently, then
**every backtest result is a lie about what live will do.** A single source of
truth for `evaluate_atoms` is the only way the Stage-1→4 "this strategy is real"
verdict transfers honestly to live execution. This is a *correctness* concern
(Merge-Readiness Charter priority #1), not a convenience.

---

## 2. Current state — verified, both repos

| Dimension | **BruteX** (this repo) | **TickVault** (`SJParthi/tickvault`) |
|---|---|---|
| Primary language | **Python 3.12** (+ Rust PyO3 hot-kernel `rust/fno-math`) | **Rust ~89%** — Cargo workspace, 6 crates |
| Crates / packages | `brutex/` (Python pkg) + `rust/fno-math` (PyO3) | `crates/{api,app,common,core,trading,storage}` |
| Job | Offline brute-force backtest (Stage 1–4), signal-only | Live **O(1)-latency** F&O execution — ticks → OMS → QuestDB |
| Indicators | **Custom Python:** EMA, pivots, Fib, day-extremes, prev-day-OHLC (`brutex/indicators/`, 1081 LoC) | **Rust `yata` crate:** SMA, EMA, RSI, MACD, BB (`crates/trading/indicator/`) |
| Condition vocabulary | **52-atom u64 vocabulary** — `evaluate_atoms(snapshot, params) → u64` (`brutex/vocabulary/`, 690 LoC, `TOTAL_CONDITIONS=52`) | **None** — no 52-condition vocabulary exists |
| Strategy model | u64 mask over the 52 atoms (bitwise AND), direction separate | **FSM evaluator + TOML config + hot-reload** (`notify` crate), `dry_run=true` default |
| Run identity | `combo_hash` = BLAKE3 over 9 fields (`brutex/strategies/combo.py`) | (UNVERIFIED — not inspected) |
| Cross-repo deps | **"self-contained… no co-development relationship to any other repo"** (DEC-P0-026, PR #48) | **"No cross-project dependencies. All logic internal to tickvault."** |
| Rust indicators history | **Removed** from `fno-math` in **DEC-P0-025** (Greeks/indicators deleted) | n/a (always Rust-native) |
| Instruments | NIFTY · BANKNIFTY · SENSEX · India VIX | NIFTY · BANKNIFTY · SENSEX · India VIX ✅ **identical** |

### 2.1 The four hard obstacles (why this is non-trivial)

1. **Language split.** BruteX's vocabulary is Python; TickVault's hot path is
   Rust. **A shared Python `pip` package cannot reach TickVault's live path.**
2. **Different indicator implementations.** BruteX hand-rolls 5 anchor-aware
   indicators; TickVault leans on the generic `yata` crate (SMA/EMA/RSI/MACD/BB).
   They are *not* the same code and not guaranteed numerically identical.
3. **Different strategy models.** BruteX = a 52-atom u64 mask. TickVault = an
   FSM + TOML. TickVault has **no notion of the 52-condition vocabulary today**,
   so adopting it is a real change to TickVault's strategy engine, not a drop-in.
4. **Both repos forbid cross-repo deps today.** Sharing reverses a *locked*
   decision in **both** `CLAUDE.md`s (DEC-P0-026 here; "no cross-project
   dependencies" there). This needs formal decisions + a §9 design pass — it is
   a deliberate scope expansion (Golden Rule #3), not a silent one.

---

## 3. The three options

### Option 1 — Shared **Rust crate** (PyO3 for BruteX, native for TickVault) ⭐

Write the indicators + the 52-atom `evaluate_atoms` **once, in a Rust crate**
(working name `trading-core` / `bx-vocab`). Both repos consume that one crate:

```
                    ┌─────────────────────────────┐
                    │   trading-core  (Rust crate) │   ← ONE source of truth
                    │   • 5 indicators             │
                    │   • evaluate_atoms → u64     │
                    │   • 52-condition bit layout  │
                    └───────────┬─────────┬────────┘
            PyO3 bindings ◄──────┘         └──────► native Cargo dep
                    │                               │
        ┌───────────▼──────────┐         ┌──────────▼───────────┐
        │  BruteX (Python)     │         │  TickVault (Rust)    │
        │  imports the crate   │         │  links the crate     │
        │  via fno-math-style  │         │  in crates/trading   │
        │  PyO3 (batched)      │         │  per-tick eval       │
        └──────────────────────┘         └──────────────────────┘
```

- **Bit-identical?** ✅ **By construction** — both run *the same compiled
  function*. No parity to maintain; it is literally one implementation.
- **"Always latest"?** Both pin the crate version (or a git ref). Bump once →
  both rebuild against it. True "change once, both get it."
- **O(1)?** ✅ The Rust `evaluate_atoms` is O(1)/bar (same as today's Python),
  and faster; BruteX **batches** the PyO3 call (pass an array of bar snapshots,
  return an array of u64) so the boundary is crossed once per chunk, not per
  bar — a net throughput *gain* for the Stage-1A sweep.
- **Cost / risk:** **High.** Port BruteX's Python vocab → Rust; add PyO3
  bindings; rewire TickVault's strategy engine onto the 52-vocab. Reverses
  **DEC-P0-025** (which deliberately *removed* Rust indicators) and both repos'
  self-containment. Couples the two release cycles through the crate's semver.
- **Fits existing strengths:** BruteX already ships a PyO3 crate (`fno-math`);
  TickVault is already Cargo/Rust. Neither learns a new toolchain.

### Option 2 — Shared **spec + golden conformance vectors**

Keep **two** implementations (Python in BruteX, Rust in TickVault) but bind them
to **one spec** (`docs/TRACK1_VOCABULARY.md`) + **one shared set of conformance
vectors** — frozen `input-snapshot → expected-u64` cases that **both** CIs must
pass, or the build goes red.

- **Bit-identical?** ✅ **Proven by tests**, not by a shared binary. The vectors
  are the contract; any divergence fails CI in both repos.
- **"Always latest"?** ⚠️ **Partial — you still implement each indicator
  twice.** Add a new condition → write it in Python *and* Rust, add a vector;
  CI enforces they agree. This is "verified in sync," **not** "write once."
- **O(1)?** ✅ Each side keeps its own O(1) impl.
- **Cost / risk:** **Medium.** No PyO3, no rewrite of either hot path; but you
  build TickVault's 52-vocab impl from scratch *and* maintain double
  implementations forever. Drift is caught, not prevented.
- **Honest gap vs the stated goal:** the operator asked for "**write once,
  reflect to both.**" Option 2 does **not** deliver that — it delivers "write
  twice, guaranteed identical." Lower risk, weaker on the actual ask.

### Option 3 — **Codegen** from a language-neutral DSL

Define the 52 conditions + indicator wiring **declaratively** (a TOML/JSON/YAML
DSL or a small Rust proc-macro source) and **generate** the Python and Rust
implementations from it.

- **Bit-identical?** ✅ Both generated from one source.
- **"Always latest"?** ✅ Edit the DSL, regenerate both → "write once."
- **O(1)?** ✅ Generated code is straight-line per bar.
- **Cost / risk:** **Medium-High.** You build *and forever maintain* a codegen
  toolchain + its own tests; the DSL must be expressive enough for Fib-proximity
  / pivot / candle-shape logic (non-trivial). Highest novelty risk.
- **When it wins:** if the condition set will grow large/fast and you want a
  declarative catalog; overkill for a fixed-ish 52.

### 3.1 Side-by-side

| Criterion | Opt 1 — Rust crate | Opt 2 — spec + vectors | Opt 3 — codegen |
|---|---|---|---|
| Truly "write once" | ✅ yes | ❌ no (write twice) | ✅ yes |
| Bit-identical guarantee | ✅ same binary | ✅ via CI vectors | ✅ generated |
| Effort to first value | High | Medium | Med-High |
| Ongoing maintenance | Low (one impl) | High (two impls) | Medium (DSL+gen) |
| Couples release cycles | Yes (semver) | Loosely (vectors) | Loosely (DSL) |
| New toolchain to learn | No (PyO3+Cargo exist) | No | Yes (the generator) |
| Reverses DEC-P0-025 | Yes | No | Partial |
| O(1) preserved | ✅ (batched PyO3) | ✅ | ✅ |
| Meets the *stated* ask | ✅ fully | ⚠️ partially | ✅ fully |

---

## 4. Recommendation — **Option 1, migrated under Option 2's safety net**

The operator's requirement is explicit: *write once, reflect to both,
bit-identical, O(1)*. Only **Option 1** (and Option 3) actually deliver
"write once"; Option 2 alone does not. Between 1 and 3, **Option 1 wins** —
it reuses both repos' existing toolchains (no novel codegen to maintain) and
is the lowest-risk way to *guarantee* bit-identity (one binary, not generated
parity). Option 3's DSL is a tool we'd own forever for a benefit Option 1 gives
for free.

**But we do not big-bang it.** We use **Option 2's conformance vectors as the
migration oracle**: freeze the *current* BruteX Python `evaluate_atoms` output
as golden vectors first, then require the new Rust crate to reproduce them
**bit-for-bit before any switchover.** This converts the scary "rewrite the
vocab in another language" risk into a mechanical, test-gated port.

> **Net recommendation:** **Destination = Option 1** (shared Rust crate).
> **Migration tool = Option 2's golden vectors.** **Reject Option 3** (codegen
> maintenance cost without an offsetting benefit). Phased + mergeable per phase.

### 4.1 Where does the shared crate live? (governance — needs an operator call)

| Choice | Pro | Con |
|---|---|---|
| **A. Own repo** `SJParthi/trading-core` ⭐ | neutral; both depend equally; clean semver | a 3rd repo to operate (CI, releases) |
| B. Inside BruteX `rust/` (BruteX owns) | reuses BruteX CI; TickVault `git` deps it | couples TickVault to BruteX's repo/cadence |
| C. Inside TickVault | reuses TickVault CI | BruteX (Python) deps a Rust-repo subdir |

**Recommend A** (own repo) for neutrality + independent semver; both repos pin a
released version, CI in each runs the shared golden vectors. *Open for operator.*

---

## 5. Phased migration plan (the recommended path)

Each phase is independently mergeable and leaves both repos green (Merge-Readiness
Charter). No phase is started before the prior phase's gate passes.

| Phase | What ships | Repo(s) | Honest envelope |
|---|---|---|---|
| **0 — Freeze the oracle** | Extract golden conformance vectors (`snapshot → u64`, all 52 bits, edge cases) from the **current** BruteX Python `evaluate_atoms`. Lock `TRACK1_VOCABULARY.md` as the cross-repo spec. | BruteX | Pure test artifact; zero behaviour change. 100%-doable now. |
| **1 — Build the Rust crate** | New `trading-core` crate: port the 5 indicators + `evaluate_atoms` to Rust. **Gate:** must reproduce every Phase-0 vector bit-for-bit. | trading-core | Rust unit + proptest + the golden vectors. No repo wired to it yet. |
| **2 — BruteX adopts it** | PyO3 bindings (batched array API); `brutex/vocabulary` + `brutex/indicators` delegate to the crate behind their **unchanged Python API**. **Gate:** BruteX's *entire existing test suite stays green* (bit-identical) + a throughput benchmark (no Stage-1A regression). | BruteX | Internals swap, public API frozen. Reverses DEC-P0-025 (new DEC-ID). |
| **3 — TickVault adopts it** | TickVault links `trading-core`; the live per-tick path evaluates the 52-vocab via the shared crate. **Decision needed:** does the 52-vocab **replace** TickVault's FSM/TOML strategy engine or run **alongside** it? (§7 open question) | TickVault | Real change to TickVault's strategy engine; its own §9 review + DEC-ID. |
| **4 — Governance + automation** | Crate semver + both repos pin it; the shared golden-vector CI gate in **both**; `CLAUDE.md` updated in **both** to record the shared-core relationship + the new authority. | all 3 | The "always latest, auto-reflect" guarantee becomes a CI-enforced invariant. |

### 5.1 What is genuinely buildable now vs deferred (Golden Rule #10)

- ✅ **Now (BruteX-only, pure, 100%-testable):** Phase 0 (golden vectors) and the
  Phase-1 Rust crate scaffold + port — *this repo can do both without touching
  TickVault.*
- 🚫 **Needs TickVault access + its operator:** Phases 3–4 require writing in the
  TickVault repo (currently **out of my session scope** — the `list_repos`/
  `add_repo` tooling is unavailable here and GitHub MCP is `brutex`-scoped). The
  operator must grant TickVault access or drive those phases there.
- 🚫 **UNVERIFIED until inspected:** TickVault's exact indicator math (does `yata`'s
  EMA match BruteX's `alpha=2/(N+1)` SMA-seed EMA?), its tick-bar snapshot shape,
  and whether its FSM can host a u64-mask evaluator. These gate Phase 3 and must
  be read from the TickVault source before that phase is designed.

---

## 6. §9 hostile-review — objections a reviewer WILL raise (+ rebuttals)

Authored pre-emptively (the doc *is* the design pass). A full 4-agent pass runs
on the chosen path before implementation.

| # | Objection (hostile) | Rebuttal / mitigation |
|---|---|---|
| 1 | "This reverses **DEC-P0-025**, which deliberately *removed* Rust indicators — you're undoing a locked decision." | Correct, and acknowledged. DEC-P0-025 removed them *because BruteX was self-contained Python*. The **requirement changed** (cross-repo single-source), which fundamentally needs a shared compiled core. The reversal is justified by the new requirement and formalized via **DEC-SHARED-CORE-001** — not a silent flip. |
| 2 | "A shared crate **couples two release cycles** — a breaking crate change breaks both repos at once." | Semver + version pinning: both pin a *released* version and bump deliberately. The crate's public API is tiny + stable (`snapshot → u64`). The shared golden vectors catch any behavioural regression before either repo bumps. |
| 3 | "Porting Python → Rust risks **subtle float/rounding divergence**." | The **Phase-0 golden vectors are the oracle**: the Rust port cannot land until it reproduces them bit-for-bit. Money is int64 paisa (exact); prices are `Decimal`→tick-snapped; the vocab is integer/boolean compares — determinism is achievable and *gated*, not hoped-for. |
| 4 | "TickVault's strategy model is **FSM+TOML, not 52-atom masks** — you're forcing a foreign model on it." | True and called out as the **biggest Phase-3 risk** (§7 Q1). The doc does **not** assume replacement; whether the vocab *replaces* or *augments* TickVault's FSM is an explicit open operator decision, with its own §9 review **in the TickVault repo**. |
| 5 | "**PyO3 per-bar boundary** crossing will wreck BruteX's billions-of-bars Stage-1A throughput." | Mitigated by **batching**: pass an array of N bar-snapshots, return an array of N u64 in one crossing (amortizes the boundary to ~0/bar). BruteX already crosses into `fno-math`; a benchmark gate (CLAUDE.md §5/§12, `p99 ratio ≤ 1.1`) blocks any regression. Expected to be a *net speedup*. |
| 6 | "**`yata` vs custom indicators** — TickVault uses `yata`'s EMA/RSI/etc.; you can't just swap." | The shared crate implements the *exact* indicators the 52-vocab needs (custom anchor-aware EMA/pivots/Fib/day-extremes/prev-day) — **not** `yata`'s generic set. TickVault keeps `yata` for any non-vocab needs and uses `trading-core` only for the shared vocab. Reconciliation is additive, not a rip-out. |
| 7 | "Who **owns** the crate? Whose CI? Who releases?" | §4.1 — recommend an **own repo** (`trading-core`) with its own CI (Rust tests + golden vectors); both consumers pin releases. Operator confirms the location. |
| 8 | "This is **scope creep** — BruteX is a self-contained backtester (DEC-P0-026)." | Yes — it is a *deliberate, operator-requested* scope expansion, formalized in **both** `CLAUDE.md`s. The §1 correctness argument (backtest signal must equal live signal) justifies it; Golden Rule #3 is satisfied by doing it *explicitly* with decision IDs, not silently. |
| 9 | "Two `CLAUDE.md`s will **disagree** about whether the repos are self-contained." | Phase 4 updates *both* `CLAUDE.md`s in lockstep to name the shared-core relationship + the authority order. The cross-repo decision is mirrored (same DEC-ID referenced in both ledgers). |

---

## 7. Open questions for the operator (block specific phases)

1. **Q1 (gates Phase 3):** In TickVault, should the 52-atom vocabulary
   **replace** the FSM+TOML strategy engine, or run **alongside** it (vocab feeds
   signals into the existing FSM)? — *Architectural; needs your call + a
   TickVault-side §9 review.*
2. **Q2 (gates Phase 1):** Crate **location** — own repo `trading-core` (rec.),
   inside BruteX `rust/`, or inside TickVault? (§4.1)
3. **Q3 (gates Phase 3):** Are TickVault's live **indicator values** numerically
   equal to BruteX's (e.g. `yata` EMA vs BruteX's `alpha=2/(N+1)`, SMA-seeded)?
   If not, the shared crate's indicators become the single definition and
   TickVault migrates onto them — confirm that's acceptable.
4. **Q4:** Do you want **strategies** shared too, or **only** the indicators +
   the 52-condition vocabulary? (BruteX strategies are u64 masks + 4 library
   templates; TickVault strategies are TOML FSMs — these are *further* apart than
   the vocabulary. Recommend: **share the vocab + indicators first**; treat
   strategy-sharing as a later, separate decision.)
5. **Q5:** Grant this session **TickVault repo access** (so Phases 3–4 can be
   designed/implemented from here), or will those be driven in the TickVault
   repo separately?

---

## 8. Decisions + governance to be created (once a path is locked)

- **BruteX:** `DEC-SHARED-CORE-001` in `docs/MIGRATION.md` + **ADR-0003**
  (`docs/adr/0003-shared-indicator-vocabulary-core.md`); amend `CLAUDE.md` §1
  (re-introduce a *scoped* shared-core relationship, superseding the DEC-P0-026
  "no co-development" line) + §5 (the `trading-core` dependency).
- **TickVault:** a mirrored decision in its ledger + a `CLAUDE.md` amendment
  naming `trading-core` as the source of truth for indicators + the 52-vocab.
- **trading-core (if Option A):** new repo, its own `CLAUDE.md`, CI = Rust tests
  + the shared golden vectors + semver release automation.
- **CI invariant (the "auto-reflect" guarantee, enforced):** the shared golden
  vectors run in **all three** CIs; a vocab change that isn't reflected
  everywhere turns a build red. *That* is what makes "always latest" real rather
  than aspirational.

---

## 9. TL;DR for the operator

- **The naive answer ("one shared Python package") is impossible** — TickVault's
  live path is Rust, not Python. (This is the whole reason to design first.)
- **To truly "write once → both get it, bit-identical": a shared Rust crate**
  (Option 1), consumed by BruteX via PyO3 and by TickVault natively.
- **De-risk it** by freezing the current Python vocab as **golden vectors** and
  gating the Rust port against them — so the port is mechanical, not a leap.
- **It reverses two locked "self-contained" decisions** — done *explicitly* with
  new decision IDs + `CLAUDE.md` updates in both repos, never silently.
- **I can build Phases 0–2 here in BruteX now;** Phases 3–4 need TickVault access
  (currently outside this session's scope).
- **Five operator decisions** (§7) unblock the rest — Q1 (replace vs augment the
  TickVault strategy engine) and Q2 (crate location) are the two that gate code.

> **Decision made (Option 1) + Phase 0 shipped.** This doc was the design pass;
> the operator accepted Option 1, and **Phase 0 (the golden conformance vectors)**
> — pure, BruteX-only, zero behaviour change — is delivered in this same PR
> (`scripts/gen_vocab_golden_vectors.py` + `tests/golden/test_vocab_golden.py` +
> the 400-case fixture). **Next:** Phase 1 (build the Rust crate, gated to
> reproduce the answer-key bit-for-bit). The §7 questions (Q1 replace-vs-augment
> TickVault's engine, Q2 crate location, Q5 TickVault access) still gate Phases
> 2–3 and are the operator's to answer.
