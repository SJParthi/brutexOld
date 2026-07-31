# ADR-0027: O(1) enforcement — the reach problem

- **Status:** PROPOSED — DESIGN ONLY. This ADR ships no code, widens no
  ceiling, and changes no bench. It measures how far the existing O(1)
  enforcement actually reaches, and names the four gaps between the tier that
  blocks a merge and the tier the decree asks for.
- **Decision ID:** DEC-O1-ENFORCEMENT-REACH-001 (MIGRATION.md #2268)
- **Date:** 2026-07-31
- **Branch:** `claude/workspace-rust-o1-audit-3om3df`
- **Builds on:** ADR-0026 (DEC-RUST-MANDATORY-POSTURE-001, #2267 — P1/P2;
  this ADR is P3) · DEC-O1-STRICT-TIER-001 (the two-tier ceiling) ·
  DEC-O1-CACHE-TIER-001 · DEC-LAKE-STATS-MANIFEST-001 (#1614 — the
  metadata-sweep allowance) · DEC-O1-GUARANTEE-HARDEN-001 (#1791 — the audit)
  · DEC-O1-CLAIM-BACKING-001 · DEC-BENCH-ABS-BASELINE-001 (#1534) ·
  DEC-LAKE-SCAN-CHILD-DIRS-O1-001 (#2261 — the measured counterexample)

---

## Context

### The demand, restated precisely

The operator's requirement names six O(1) properties — *uniqueness,
deduplication, mapping, latency, time complexity, space complexity* — and
attaches the same phrase to all six: **with guarantee and assurance**. As with
ADR-0026, "guarantee" means a mechanism, not a claim.

O(1) differs from "is it Rust" in one way that shapes everything below:
**Rust-ness is a property of the source; O(1) is a property of a
measurement.** A source cannot drift out of being written in Rust without
someone editing it. A path can drift out of being O(1) by an edit *anywhere
its data volume is decided* — a new index, a widened glob, a lost cache key.
So the load-bearing question for P3 is not *"is there a bench?"* but **"does
the bench run when the thing it guards changes?"** That is the reach problem.

### What is already built — and it is more than the first draft credited

The repo has a genuinely three-layer chain, and it is explicitly designed as
one. `tests/unit/test_o1_claim_backing.py` states it in its own docstring:

> 1. this gate  — every O(1) claim cites a live bench   (claim → test)
> 2. the bench  — the ratio is measured, not asserted   (test → number)
> 3. the tier   — the number must hold under a ceiling  (number → gate)
>
> Break any one and the chain is a promise again.

All three layers exist and, at the **robust** tier, all three block a merge:

| Layer | Mechanism | Blocking? |
|---|---|---|
| 1 — claim → test | `test_o1_claim_backing.py` parses `docs/O1_GUARANTEE_AUDIT.md` (57 data rows across 4 tables at this commit; the test asserts ≥40 anti-vacuously); an `O(1)` row with no cited bench file, or a cited file that does not exist, fails; a `none` citation must carry a written reason in `NO_BENCH_REASONS` | **yes** (unit suite) |
| 2 — test → number | 148 bench files; `ci.yml`'s `bench` job runs the whole `tests/bench` directory on every push | **yes** (`ci-ok` needs `bench`) |
| 3 — number → gate | `tests/bench/_o1_tiers.py`, one source of truth; 147 `ratio_ceiling(...)` call sites across 102 of 148 files | **yes, at the robust ceiling** |

That is a real chain, and it is why this ADR is about *reach* rather than
about building an enforcement mechanism from nothing.

### The measured state of the tier pyramid (2026-07-31, this branch)

Every figure below was obtained by running the command, not by reading a
comment.

| Tier | Ceiling | Reach | Blocking? |
|---|---|---|---|
| **shared-CI robust** | 3–10×, modal **5.0×** (104 of 147 call sites; then 8.0× ×26, 3.0× ×10, 10.0× ×2, 1.5× ×2, default ×3) | all 148 bench files, every push | **yes** |
| **M4-Pro strict** | **1.1×** (decreed) | **13 bench files** (+1 in a later step) | **no** — post-merge only, deliberately no `pull_request` trigger |
| **cache-tier allowance** | 3.0× | cross-cache-tier ratios only | inherits its tier |
| **metadata-sweep allowance** | 3.0× | documented O(D) statx sweeps only | inherits its tier |
| **absolute floor** | per-hardware JSON | `tests/bench/baselines/` holds **zero** `.json` (README only) | **dormant by design** (bootstrap) |
| **≥50 M/s canary** | 50,000,000 ops/s | asserted at `test_throughput_absolute.py:167` and `test_trade_sim_throughput.py:187` | **yes, but M4-Pro-gated** |

The two allowances are not weakening — each was forced by a measurement and
each is *floored by the shared default* so it can never be looser than the
tier it sits inside. `cache_tier_ceiling` exists because the decreed 1.1× is
physically unsatisfiable across a cache cliff (measured: mask-AND 2.43× at
2.0 **billion** ops/s absolute); `metadata_sweep_ceiling` exists because a
72-month fixture pays 66 extra `statx` calls over a 6-month one **by design**
(measured 1.53×, Δ≈18 µs/file).

### The finding: the strict tier does not run when its own sources change

The M4-Pro job sets `BRUTEX_M4PRO_STRICT=1` and executes thirteen bench
files. Mapping each to the module it imports, and checking that module
against the job's own `paths:` trigger list:

| # | Strict bench | Source it measures | In the trigger paths? |
|---|---|---|---|
| 1 | `test_store_o1` | `brutex/orchestrator/{state,store}.py` | ❌ |
| 2 | `test_option_dedup_o1` | `brutex/data/option_dedup.py` | ❌ |
| 3 | `test_lmdb_membership_o1` | `brutex/data/lmdb_membership.py` | ❌ |
| 4 | `test_rate_limiter_o1` | `brutex/orchestrator/rate_limiter.py` | ❌ |
| 5 | `test_summary_cache_o1` | `brutex/dashboard/summary_cache.py` | ❌ |
| 6 | `test_bar_lookup_o1` | `brutex/data/models.py`, `brutex/feed/lookahead.py` | ❌ |
| 7 | `test_indicator_lookup_o1` | `brutex/indicators/{fib_levels,pivots}.py` | ❌ |
| 8 | `test_bloom_o1` | `brutex/orchestrator/bloom.py` | ❌ |
| 9 | `test_engine_sweep_cache_o1` | `brutex/engine/sweep_cache.py` | ❌ |
| 10 | `test_engine_combo_mask_o1` | `brutex/engine/combo_mask.py` | ✅ |
| 11 | `test_engine_results_store_o1` | `brutex/engine/results_store.py` | ❌ |
| 12 | `test_market_structure_o1` | `trading_core` → `rust/trading-core/**` | ❌ (only `rust/fno-math/**` is listed) |
| 13 | `test_throughput_canary` | `combo_mask` ✅, `vocabulary/**` ✅, `indicators/{fib_levels,pivots}` ❌ | partial |

**Measured result: 13 of the 15 distinct sources behind the strict tier can
be edited without the strict tier ever running.** Only
`brutex/engine/combo_mask.py` and `brutex/vocabulary/**` trigger it.

Worse, and independently: **four of the thirteen bench files the job
executes are themselves absent from the job's own `paths:` list** —
`test_store_o1.py`, `test_option_dedup_o1.py`, `test_lmdb_membership_o1.py`,
`test_rate_limiter_o1.py`. Editing one of those benches does not run it.

This is exactly the shape of #2261: a Rust hot-path function measured at
**94.46× growth** while every correctness test passed. Correctness tests do
not measure complexity, and a complexity gate that does not fire is
indistinguishable from one that does not exist.

### Why this is a reach problem and not a ceiling problem

The temptation is to read "13 files at strict, 148 at robust" as *the strict
tier is too small*. It is not, and widening it is the wrong fix. The
distinction that matters:

- The **robust** tier answers *"did this path become super-linear?"* — a 5.0×
  ceiling catches an O(n) or O(n²) regression on a 10× workload spread
  decisively, and it runs on everything, every push. It is the right gate for
  the long tail.
- The **strict** tier answers *"is the decreed 1.1× actually held?"* — it
  needs a quiet machine, it can only be honest on same-footprint paths, and
  it is correctly scoped to the Golden-Rule-#5 hot paths.

So the design is not "make strict bigger". It is **"make strict fire when its
own subject changes"**, which is a trigger-list problem with a measurable
answer.

---

## Decision

### D1 — The reach law

> **A bench that gates a path must run whenever that path's source changes.**

Stated as an obligation on the workflow rather than on the author: a bench
file's presence in a job's execution list creates a duty on that job's
trigger list. Today that duty is met for 1 source of 15.

### D2 — Close the trigger gap mechanically, not by hand

Hand-maintaining a `paths:` list against an execution list is precisely the
drift this repo already pins elsewhere (`WHEEL_SET` ↔ Makefile,
`_EXPECTED_COMPILED_SYMBOLS` ↔ crate). The same treatment applies:

1. **Derive, do not curate.** A test walks the strict job's execution list,
   imports each bench module's `brutex.*` / crate imports, and asserts every
   resulting source path is covered by the job's `paths:` filter (directly or
   by a `**` prefix).
2. **Both directions.** The same test asserts every *executed* bench file
   appears in `paths:` — the file gap above fails it today.
3. **A third direction, added when the test was built.** Directions 1 and 2
   only prove the workflow *fires*. They cannot see a bench that fires, runs,
   and then **skips its own absolute assertion** because `BRUTEX_M4PRO_ABS` is
   unset in the job that ran it. Read that precisely: the file still executes
   — `ci.yml` runs the whole `tests/bench` directory at the robust ratio tier —
   so its *ratio* assertions are live. What never executes anywhere is the
   absolute-throughput line. A skip is not a failure, so nothing goes red and
   the dormancy is invisible. That is the exact shape which let this ADR's own
   Honest Envelope credit a canary that never ran (see §3 there). The test
   therefore also asserts: every bench referencing `BRUTEX_M4PRO_ABS` is
   invoked by some job that *sets* it, with a named, shrink-only dormant set
   for the ones that are not.
4. **Fails loudly, names the miss.** The failure message lists the uncovered
   sources so the fix is one line per row, never a scavenger hunt.

The shipped pins live in `tests/unit/test_m4pro_trigger_derivation.py`; where
its docstring and this section ever disagree, **the test wins** — it is the
thing that executes.

This is layer 0 of the chain: *gate → trigger*. It sits below
`test_o1_claim_backing.py`'s layer 1 and closes the hole that layer cannot
see — a claim can cite a live bench that is never run.

### D3 — Make the strict tier's verdict visible without making it blocking

The strict job has **no `pull_request` trigger by deliberate design**
(DEC-M4PRO-PR-TRIGGER-001): the self-hosted runner queues indefinitely while
offline, and a PR trigger would hang every bench-touching PR's mergeable
state and emit a false *"MERGE BLOCKED (pending)"* — observed on PR #284.
That reasoning is sound and is **not** overturned here.

The consequence, though, is that a strict-tier failure is discovered *after*
merge, in a workflow nobody is required to look at. The design therefore
separates *blocking* from *visible*:

- Keep the strict job non-blocking and push-triggered (unchanged).
- On failure it already opens an issue; that stays.
- **Add the strict verdict to `brutex doctor`** as a named stage that reads
  the last recorded strict run, so the operator's own box reports it — the
  same treatment ADR-0026 §D7 gives the facade census.

This is the honest resolution: the runner's availability is a real
constraint, so the answer is a *reported* verdict rather than a pretended
gate. Making a queue-forever job required would re-create the exact failure
DEC-M4PRO-PR-TRIGGER-001 was written to remove.

### D4 — Leave the absolute tier's bootstrap to the operator, and say so

`tests/bench/baselines/` holds zero `.json` by design: DEC-BENCH-ABS-BASELINE-001
ships the comparator, the schema, the capture flow and the governance gate
(*a blessed-cell change needs a new ledgered DEC quoting the literal entry key
and value*) and deliberately ships **no measured numbers** — the first
baseline must be measured on the real M4-Pro and blessed through a bot PR the
operator merges.

**Ruling: that is correct and stays.** Fabricating a baseline on this
container would be exactly the illusion the honesty constraint forbids. The
absolute tier is *dormant pending one operator action*, and this ADR records
that as a named precondition rather than as a gap to be closed by code.

### D5 — What P3 does not prove

Stated so the guarantee is not oversold, in the same shape as ADR-0026 §D7:

- A green ratio bench proves **this path did not become super-linear across
  the measured spread**. It does not prove a constant-factor regression did
  not happen — that is the absolute tier's job, and the absolute tier is
  dormant (D4).
- The robust ceiling is 5.0× at the mode. A genuine regression that lands
  *under* 5.0× on the measured spread passes. Several benches therefore carry
  **deterministic** teeth alongside the ratio — file-touch counts, fs-op
  counts, statement counts — and those are the primary gate wherever the
  ratio is physics-bound (#1614's paging gate is the worked example).
- The two allowances (cache tier, metadata sweep) are **measured
  concessions**, not slack: each is floored by the shared default and each
  names the measurement that forced it. A future PR widening either without a
  measurement is a Golden-Rule-#10 violation, not a tuning change.
- **O(1) space is not gated at all.** The decree names it; no bench asserts
  it. Peak-RSS or allocation-count gating is real work and is named here as
  absent rather than implied by the latency tiers.

### D6 — Sequencing against ADR-0026

Both ADRs land wiring, and they share one file (`brutex doctor`'s stage
list). The order is:

1. **D2 (trigger derivation)** — pure test addition, no workflow behavior
   change until a row fails.
2. Then the `paths:` fix the test demands — one line per uncovered source.
3. **ADR-0026 §D7 Tier 3** (census as a CI gate + doctor stage).
4. **D3** (strict verdict as a doctor stage) — folded into the same doctor
   change so the stage list moves once.

---

## Consequences

**Gained.** The gap between "we have a bench for that" and "that bench runs
when it matters" becomes a failing test rather than a belief. The strict
tier's verdict reaches the operator's box instead of a workflow nobody
watches. And the four tiers stop being described by prose that drifts — this
ADR's own first draft mis-stated two of them, which is the argument for
deriving the numbers in a test rather than writing them down.

**Costs, stated.** The trigger-derivation test will fail on landing until the
`paths:` list is corrected — that is the point, but it means the fix PR is
two commits, not one. Widening `paths:` makes the M4-Pro job trigger more
often; on an offline runner those runs queue harmlessly (the job has no
`pull_request` trigger, so a queued run blocks nothing).

**Not changed by this ADR.** No ceiling is widened. No bench is deleted or
skipped. No workflow becomes required. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX. Run identity, the lake schema, the vocabulary and the
rate laws are untouched.

---

## Alternatives considered

**Make `bench-m4pro` a required check.** Rejected on recorded evidence: the
runner queues indefinitely while offline, and DEC-M4PRO-PR-TRIGGER-001
documents the observed failure (PR #284 hung on a perpetually-pending strict
check). A required gate that cannot complete is worse than a reported one.

**Raise the shared-CI ceiling to the decreed 1.1× and delete the two-tier
split.** Rejected by measurement: the shared 2-core runners are too noisy
(DEC-P0-020), and 1.1× is *physically* unsatisfiable across a cache cliff
even on the quiet M4-Pro — measured 2.43× at 2.0 billion ops/s absolute. The
split is a measurement result, not a compromise.

**Run every one of the 148 benches at the strict tier.** Rejected: strict is
only honest on same-footprint paths. Applying 1.1× to a cross-tier or
metadata-sweep bench produces a red gate with no defect behind it, and the
predictable response to that is a widened ceiling — which converts a real
gate into a decorative one.

**Fabricate a first absolute baseline so the tier leaves bootstrap.**
Rejected outright. The numbers would have to come from this container, which
is not the reference hardware, and the governance gate exists precisely to
stop unblessed cells. Dormant-and-honest beats active-and-invented.

---

## Honest envelope

1. **This ADR is design. It ships no code.** The trigger-derivation test, the
   `paths:` correction and the doctor stage are separate PRs with their own
   DECs.
2. **Every number here was measured on this branch at this commit**, by
   running the command: 147 `ratio_ceiling` call sites (104 at 5.0×), 102 of
   148 bench files importing `_o1_tiers`, 13 bench files in the strict job's
   execution list, 4 of them absent from its `paths:`, 13 of 15 sources
   uncovered, 57 audit data rows, zero `.json` in `baselines/`.

   **Superseded on this same branch by the §D2 fix.** Those trigger figures
   describe the state *before* `tests/unit/test_m4pro_trigger_derivation.py`
   and the `paths:` correction landed. At HEAD the derived filter is **57
   `paths:` rows** (28 before), the derivation gate runs **8 passed**, and the
   execution-⊆-paths and sources-⊆-paths gaps are **zero** — which is the
   whole point of deriving rather than curating. The dormant-assertion figure
   from direction 3 is **12 skipped tests across 14 gated files** (§3 below).
3. **ADR-0026 §D8's first draft was wrong about the canary — and this ADR's
   own first correction was itself incomplete.** The asserts do exist:
   `test_throughput_absolute.py:167` and `test_trade_sim_throughput.py:187`
   both assert the ≥50 M/s floor. What the correction omitted is the clause
   that decides whether the gate is a gate at all: **both are wrapped in
   `skipif(not BRUTEX_M4PRO_ABS)`**, an env var set only by the self-hosted
   `bench-m4pro.yml` workflow. Executed on the shared runner at this commit:

   ```
   $ .venv/bin/pytest tests/bench/test_throughput_absolute.py \
                      tests/bench/test_trade_sim_throughput.py -q
   14 passed, 2 skipped
   ```

   Both canary asserts are among the 2 skipped. Widening to every
   `BRUTEX_M4PRO_ABS`-gated bench file (14 of them, `_o1_tiers.py` excluded as
   the shared helper) measures **12 skipped, 65 passed** — so the dormant
   surface is 12 tests, not the 1 the phrase "the canary" implies.

   The consequence, stated plainly: `bench-m4pro` is a **non-required** check
   (MIGRATION.md #382, #1534, and #2254, which exempts it from both merge
   gates precisely so a red bench runner cannot block merges). Therefore
   Golden Rule #12's ≥50 M/s canary — and the other 11 absolute gates beside
   it — **is not enforced on any merge-blocking path**. It is a post-merge
   observation on hardware that must be online to observe anything. Calling it
   "asserted" without that qualifier reads as a merge guarantee it does not
   provide. `test_throughput_canary.py` mentions "50 M/s" twice, but only in
   prose explaining the figure — it carries no floor assert and is not gated.
4. **"13 of 15 sources uncovered" is a static import map**, not a taint
   analysis. A bench that reaches a module through a runtime import or a
   fixture would not appear; the derivation test in D2 has the same limit and
   should say so where it lives.
5. **O(1) space complexity is named in the decree and gated nowhere.** This
   ADR does not fix that; it records it as an open property so it stops being
   implied by the latency tiers.
6. **A green P3 gate says nothing about P1 or P2.** #2261 measured 94.46×
   growth on a fully engaged Rust path. Three properties, three gates —
   ADR-0026 §D1 carries the other two.
