# ADR-0026: The mandatory-Rust posture

- **Status:** PROPOSED — DESIGN ONLY. This ADR ships no code, flips no flag,
  and deletes no oracle. It states, in one place, the law that four lanes
  (#2051, #1979, #2055, #2065) have already executed ad-hoc, and it names the
  work each remaining facade owes before it may follow them.
- **Decision ID:** DEC-RUST-MANDATORY-POSTURE-001 (MIGRATION.md #2267)
- **Date:** 2026-07-31
- **Branch:** `claude/workspace-rust-o1-audit-3om3df`
- **Builds on:** ADR-0020 (DEC-RUST-DATALAKE-IO-ROADMAP-001, #1891 — the
  CONVERSION law) · DEC-RUST-DEFAULT-ON-WAVE-001 (#2215 — the FLIP to
  default-ON) · DEC-RUST-ENGAGEMENT-CENSUS-001/-002 · DEC-RUST-DEGRADE-SENTINEL-001

---

## Context

### The operator's demand, and the three properties it decomposes into

The standing requirement is that everything except the Groww/Dhan vendor pull
runs as Rust, O(1), always — *with guarantee and assurance*, and explicitly
**not** as an assertion: *"how will you ensure the guarantee and assurance that
always it should be RUST O(1) — I need the assurance, I don't believe you."*

That demand contains three properties that are routinely conflated, and
conflating them is the entire error class this ADR exists to kill:

| # | Property | The question it answers | What actually proves it |
|---|---|---|---|
| **P1** | *Written* in Rust | Does a crate contain this law? | The crate source + `cargo test` |
| **P2** | *Rust answered* | Did the crate produce the value this run shipped? | A degrade-free run, or a posture with no fallback to take |
| **P3** | *O(1)* | Does cost stay flat as N grows? | A ratio bench at a named tier |

P1 is largely done (~40 seams, 12 crates). **P2 and P3 are independent of P1
and of each other**, and the repo has a measured example of exactly that
independence: DEC-LAKE-SCAN-CHILD-DIRS-O1-001 (#2261) found a *fully engaged
Rust function on the hot path* measuring **94.46× growth** while every
correctness test passed. Rust and O(1) are orthogonal. So are Rust-written and
Rust-answering.

### What #2215 did — and precisely what it did not do

DEC-RUST-DEFAULT-ON-WAVE-001 flipped ~28 facade seams from default-OFF to
default-ON under one uniform law:

> unset ⇒ ON · explicit-truthy ⇒ ON · explicit-falsy ⇒ the silent OFF kill
> switch · junk ⇒ OFF + one loud WARN

and it says so in its own honest envelope: *"a default flip is a BEHAVIOR
change protected by burn-in + autorevert + degrade, **NOT a byte-guarantee**."*

It deliberately **kept every degrade path**. That was correct for a flip wave —
but it means the current state is *default-ON*, not *mandatory*. A facade that
is ENGAGED can still serve Python on any of thirteen distinct triggers, and on
five of those it latches for the rest of the process.

### The measured state (2026-07-31, this branch)

Two runs, both executed rather than reasoned about:

**Facade census** (`python -m brutex.observability.rust_census`):

```
37 rows discovered — 32 ENGAGED, 5 REQUIRED, 0 DEGRADED, 0 OFF, 0 UNKNOWN
```

All 32 ENGAGED rows report `flag_on=True, crate_present=True`. The 5 REQUIRED
are `brutex.data.lake_rust_waved`, `brutex.engine.refine_rust`,
`brutex.engine.rust_trade_sim`, `brutex.feed_rust`, `brutex.pull_state_rust`.

**Three findings that only appear when you look:**

1. **The census under-counts by two.** `discover_facades()` applies a name
   pre-filter (`rust_census.py:405-406`) that skips a module unless its leaf
   name contains `rust` or `seam` — *before* paying the import that would run
   the structural signature test. `brutex/ranking/cvar_gate.py`
   (`CVAR_RUST_ENV` + `cvar_rust_enabled()` + `cvar_gate.engaged`) and
   `brutex/data/greeks_enrich.py` (`BRUTEX_GREEKS_RUST_BATCH`) both carry the
   full facade signature and are dropped silently. Both flags are named in
   #2215. True count is **39**.

2. **A sixth REQUIRED seam is invisible entirely.**
   `brutex/engine/results_store.py:136` is a bare module-scope
   `import brutex_identity` — no facade, no typed error, no remediation string,
   an untyped `ImportError` at import time. It fails all three discovery routes
   and appears in no census row.

3. **The guarantee machinery is BUILT and NOT WIRED.**
   `brutex/observability/rust_census.py` (660 lines, `main()` exits 1 on any
   DEGRADED) and `brutex/observability/rust_sentinel.py` (the run-level
   degrade watcher, DEC-RUST-DEGRADE-SENTINEL-001) carry **1,293 lines of
   tests between them** and have, measured by grep:

   - **zero** production callers,
   - **zero** CLI subcommands,
   - **zero** CI workflow steps.

   This is the dormant-feature class this repo has already been burned by
   twice — DEC-PULL-HEALTH-WIRING-001 (#935: a monitor that watched nothing)
   and DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001 (#1014: a detector gated dead
   in every live pull). **The single largest gap in the Rust guarantee is not
   a missing mechanism. It is two finished mechanisms nothing runs.**

### The honest shape of the problem

The naive design — "convert the remaining 32 to REQUIRED" — does not survive
contact with the code. A classification of every retained oracle (below, §D3)
finds that **roughly two thirds of them cannot be deleted at all**, for
reasons that are permanent rather than unfinished: libm bit-portability,
CPython accept-set semantics, arbitrary-precision integers, wheel-free boot,
and third-party version lockstep.

So the guarantee cannot be *"every facade is mandatory."* It has to be:

> **Every facade is either mandatory (no fallback exists), or its fallback is
> loud, classified, counted, and CI-failing — with nothing in between.**

That is achievable, and every piece of it already exists in the tree.

---

## Decision

### D1 — Three postures, named once

The repo has been using two postures with no shared vocabulary and a third
that was never named. All three are now named, and every facade carries
exactly one.

| Posture | Contract | Fallback | Proof that Rust answered |
|---|---|---|---|
| **A — ENGAGED** | Flag default-ON; Python oracle retained live | Silent degrade + warn-once, five triggers latch | Sentinel-clean run (§D7) |
| **B — REQUIRED** | No Python oracle exists; loud typed refusal at call time | **None** | *Completion is itself the proof* |
| **C — MUST-STAY** | Posture A, **permanently**. The oracle is load-bearing parity infrastructure or the sole implementation for a refused domain | By design | Sentinel-clean run + the named envelope |

Posture C is not a weaker B. It is a **different claim**: the oracle is not a
fallback awaiting deletion, it is the answer for inputs the crate refuses. The
ledger already states this in `DEC-OBSERVABILITY-ORACLE-DELETE-001`: *"the
retained Python oracles are **load-bearing parity infrastructure, not dead
code**."* Naming C stops the register being read as a backlog.

### D2 — The REQUIRED template (the B contract)

Extracted from the four facades that already ship it (`feed_rust.py`,
`refine_rust.py`, `lake_rust_waved.py`, `pull_state_rust.py`). Nine clauses,
each with a live precedent:

**B1 — Two-part chain, flag law first.** `require_<lane>_flags()` refuses a
SET-falsy / junk / retired flag with no wheel needed; `require_<lane>_kernel()`
calls it, *then* imports the wheel and returns the module. (`feed_rust.py:117`
and `:156`; refine fuses the two at `:146`.)

**B2 — Refusal fires at CALL time, never import time.** The crate resolve lives
inside the `require_*` body. Module scope stays stdlib-only. This is what
preserves wheel-free boot for the dashboard and `brutex doctor`
(`refine_rust.py:17-20`).

**B3 — The typed class is named by law.** `…UnavailableError` (wheel absent) or
`…FlagRemovedError` (flag retired). This is **machine-checked**:
`rust_census.py:111` greps `\b\w*(?:Unavailable|FlagRemoved)Error\b`. A new
REQUIRED facade that names its class anything else is silently mis-verdicted.

**B4 — The message carries four parts.** Flag/symbol echoed with `!r` → what it
asks for → **the DEC id that deleted the oracle** → the named remediation
(a module constant: `WHEEL_REMEDIATION`, `BUILD_HINT`, `_REMEDY`).

**B5 — Pick a retirement dialect, and say which.** Two exist and both are
legitimate:
  - *no-op dialect* (feed, refine): truthy passes as a harmless no-op.
  - *retired-axis dialect* (waved, pull_state): **every** non-empty value
    refuses, including `1` — because honoring `1` "would silently bless a
    stale config whose kill-switch intent no longer exists"
    (`lake_rust_waved.py:185-191`, the `BRUTEX_FILL_MODE` precedent).

  **Ruling:** a facade whose flag is *retired* by the promotion takes the
  retired-axis dialect. A facade whose flag survives as a documented no-op
  takes the no-op dialect. The choice is recorded in the promoting DEC, never
  left implicit.

**B6 — Refuse-poison flips from degrade to raise.** The guard *conditions*
survive verbatim; only the consequence changes. `validate_strict_scan` returns
a **named violated-law token** (`"result_shape"`, `"kept_order"`,
`"stop_range"`, …) that the caller weaves into its raise
(`feed_rust.py:179-242`). In posture B a poisoned wheel **can only refuse,
never serve**.

**B7 — The version guard becomes a precondition, not a latch.** For the
polars-linked crates the lockstep check is LOAD-BEARING post-delete
(`lake_rust_waved.py:239-249`) — see M16, which is why most of that family
cannot be promoted at all.

**B8 — The panic belt survives, with a changed meaning.** It still catches; it
now **re-raises as the surface's typed class** rather than degrading. Honest
bound: `fno_math` ships `panic = "abort"`, so its belt is a dev-build path
only; `brutex-lake` does not, and maps panics at its own boundary.

**B9 — The escape-set ordering is INVARIANT across both postures.**
`except (KeyboardInterrupt, SystemExit, MemoryError): raise` sits **above** the
belt, so the belt's `Exception` member can never swallow an OOM. Present
identically in all four REQUIRED and all four ENGAGED templates. **Do not
reorder.**

**Delete the machinery with the oracle.** `lake_rust_waved.py:20-22` is the
worked example: *"The burn-in / oracle / degrade machinery is REMOVED (there is
no oracle to compare against or fall back to)."* A promotion that leaves a dead
`_disable()` behind has not finished.

### D3 — The MUST-STAY register (posture C)

Eighteen classes, each with a code citation and a ledger DEC. This register is
**normative**: a PR proposing to delete an oracle in one of these classes is
refused on sight unless it first retires the class.

| # | Class | Why the oracle is permanent | Ledger |
|---|---|---|---|
| **M1** | libm `pow` | CAGR `**` and skewness `math.pow` — the not-correctly-rounded-portable class; this is *why* `compute_rust` splits fold-in-Rust / combine-in-Python | #1902 |
| **M2** | libm `log` | Kelly growth `ln` — no cross-libm bit guarantee | #1902 |
| **M3** | libm `erf` (the same-symbol FFI exception) | An fdlibm transcription was **refuted by measurement** (1-ULP on ~1.1% of 3.5M points); Rust binds the *process* libm symbol, so equality holds **per platform**, and the oracle + burn-in are the fence | #2053 |
| **M4** | libm `exp` | Logistic sigmoid; burn-in is the only tooth | meta lane |
| **M5** | `float.__repr__` / `Decimal.__str__` — the **lowered-tree law** | Floats lower via `repr()` and Decimals via `str()` **in Python before the seam**; the crate never reproduces those byte laws. Declared a **STANDING CRATE INVARIANT** | #1955 |
| **M6** | numpy internal reduction order | WFO Sharpe *replicates* numpy pairwise (the oracle is the recovery path if numpy changes); `lockbox`/`spa` `cumsum`/`maximum.accumulate` were **DROPPED, disclosed** | #2053 |
| **M7** | CPython `strptime %b` / LC_TIME | The oracle's accept set moves with locale; handled by a live probe that latches OFF, never by re-implementation | #1940 |
| **M8** | Unicode digits (`isdigit`/`int()`/`\d`) | CPython accepts Unicode Nd; "reproducing Unicode Nd tables in Rust would be drift bait" | #1940, sweep-ops |
| **M9** | `str.strip`'s CPython whitespace set | `isspace` law, not Unicode `White_Space`; tokens arrive PRE-STRIPPED | #2025 |
| **M10** | Arbitrary-precision int | `2**64-1` episode overflow → "the oracle's unbounded Python int is the honest value"; same for grid tuple-count and exec i64 | #2007, #2212 |
| **M11** | CPython `json.loads` dialect | serde refuses bytes CPython accepts (NaN/Infinity, lone surrogates, deep nesting, `\x1c` whitespace) → the crate **abstains** | #2218, backup lane |
| **M12** | CPython `Decimal` ambient context | NaN/±Inf, exponent windows, armed `Inexact`/`Rounded` traps, `Emax`/`Emin` — every one raise-vs-value divergent; **Rust has no arm at all** | #1943, #2054 |
| **M13** | f64-exactness envelopes | `2**52` equity, `2**53` counts, `2**50` scheduler band, u128 masks, `MAX_PRICE_PAISA` — the oracle *is* the out-of-envelope arm | #1186, sweep-ops |
| **M14** | I/O and OS | **"NO write, fsync, rename or lock EVER crosses the FFI"**; locks parity is BEHAVIORAL only, no byte burn-in exists | #1912 |
| **M15** | Wheel-free boot | Calendar/config/money laws are module-TOP imported into pull-gating, the dashboard, `brutex doctor`; "a wheel-required delete would break that boot" — the **#2215-sanctioned deviation** from #1962's deletion token | #2215 |
| **M16** | polars version lockstep | The crate links a polars REV pinned to the wheel's commit; a `!=` latches OFF and the oracle serves. Deleting it turns a benign bump into an outage | #1891 |
| **M17** | Standing operator exceptions | stats tails and the CVaR fold are marked **"retained forever (the operator exception)"** in source | #1928/#2053 |
| **M18** | Always-on-differential (`b*`) families | For ranking `(b*)` rows *"the wire value is ALWAYS the Python oracle's"* — deleting the oracle deletes the shipped value | slice-4 |

### D4 — Every facade classified

Applying D3 to the live census. This is the answer to "what is left":

**Posture B already (5 + 1 invisible):** `feed_rust` · `refine_rust` ·
`lake_rust_waved` · `pull_state_rust` · `rust_trade_sim` (conditional, wide
masks) · `results_store`'s bare `brutex_identity` import (§D9 makes it a real
facade).

**Posture C — permanent, ~22 facades:** the stats tails (M3/M6) ·
`compute_rust` (M1) · `meta_rust` (M4) · `fno_symbol_rust` (M7/M8) ·
`money_rust`, `models_rust`, the Decimal arms of `optcost_rust` (M12) ·
`calendar_rust` (M11/M15) · `backup_rust` parse arm (M11) ·
`observability_rust` (M10, gated on the H.2 rail) · `sweep_ops_rust` (M10/M13)
· `exec_rust` (M13; deletion already named `DEC-EXEC-PY-BODY-DELETE-001`) ·
`rust_metrics` (M13) · `cvar_gate` (M17) · `rust_ranking` + `rust_rank_select`
(M18) · `lake_keys_rust` (M8) · the `lake_rust_render`/`merge`/`resample`/
`resample_daily` family (M16) · `lake_rust_locks` (M14) ·
`hardware_envelope_rust`, `paths_rust`, `dotenv_rust` (M15) · `rust_front`
(AUTO fallback is the design) · `combo_rust`'s `_canonicalise_value` (M5, sole
implementation).

**Posture B candidates — promotable, ~10:** `detectors_rust` ·
`window_ledger_rust` · `notify_rust` · `nightly_rust` · `crossverify/rust_seam`
· `backup_env_rust` · `lake_rust_stats` · `rust_stage1a` · `rust_enum` (once a
u128 width refusal replaces the generator degrade) · the two routed oracles of
`combo_rust` and the int/date arms of `optcost_rust`.

**The honest headline:** promotion is available for roughly **a quarter** of
the ENGAGED set. The other three quarters stay posture C **permanently and
correctly**, which is exactly why §D7's sentinel — not §D2's template — is the
load-bearing half of this ADR.

### D5 — The promotion ladder

A facade moves A → B only by climbing all five rungs, in order. Each rung has
a precedent that already paid for it.

| Rung | Requirement | Precedent |
|---|---|---|
| **R1 — Corpus** | A differential corpus that is *complete for the production-reachable input space*, shipped as its own PR before the delete | #1916's 27-case poison corpus, shipped ahead of #1979 |
| **R2 — Differential** | Total parity over that corpus **with an engagement census per case** — "27/27 IDENTICAL **and** 27/27 ENGAGED (zero degrades)", both directions | #1979 |
| **R3 — Degrade closure** | Every corpus-reachable degrade converted to a crate-owned refusal verdict with byte-exact CPython repr twins, *before* the delete | #1979 W-5 |
| **R4 — Flip + soak** | Default-ON, then a soak window. The H.2 rail states it verbatim: **"parity lane green → flip DEC → soak window → delete"** | `DEC-OBSERVABILITY-ORACLE-DELETE-001` |
| **R5 — Delete** | Oracle **and machinery**; frozen goldens pass with fixtures BYTE-UNTOUCHED; the deleted oracle is kept as a frozen **in-test** reference for the hypothesis differential | #2051 |

**The cost of skipping R1/R2 is measured, not hypothetical.** #1979 ran the
full cycle and *still* surfaced a production-reachable residual after the
delete (W-6a: a 17-column Greeks-overlay dup the deleted Python core healed and
the crate refused), closed by a follow-up slice. That is the empirical argument
for corpus-*completeness* rather than corpus-*green*.

### D6 — What the greenfield ruling licenses, and what it does not

The canonical statement (#2055, mirrored at #1918→#1962): *no deployment and no
stored backtests exist, **NO stored ledger predates the kernel bytes**, so the
forbidding ruling's stored-history premise is VOID.*

**It licenses:** dropping the *bit-match-with-the-old-implementation*
obligation. Where no stored artifact predates the crate — manifest shards,
ledger bytes, notifications, plists, day-CSVs, `rust_stage1a`'s pure
intermediates — a **frozen self-golden plus algebraic/property invariants** is
a sufficient correctness bar, with deterministic seeding mandatory (Golden
Rule #4).

**It does not reach a single MUST-STAY class**, because none of them is a
stored-artifact argument:

- M1–M4 are *cross-platform on the new implementation*, not versus history —
  #2055 still carries "platform-fragile libm-dependent trajectory goldens" as
  a live MINOR *after* a full greenfield delete.
- M5 re-keys every **future** run; the invariant is declared standing.
- M6's drift source is a future numpy release.
- M7–M12 are accept-set differences on **live** input; the oracle is the only
  arm, so deleting it is a behavior change *today*.
- M14–M16 were explicitly overridden in #2215 by the operator's
  keep-every-safety-layer directive.

**The ruling therefore states:** *greenfield removes the bit-match obligation;
it does not remove the corpus-completeness obligation.* R1–R3 stand even when
R-nothing-is-owed applies to the comparand.

### D7 — The guarantee mechanism (the load-bearing half)

Three tiers. **Tier 2 and Tier 3 are already written and merely unwired** — the
work is wiring, not invention.

**Tier 1 — Posture (§D1–D6).** Where B is reachable, completion is the proof:
there is no fallback to take, so a run that produced an answer produced a Rust
answer. This tier covers ~15 facades once D5 is walked.

**Tier 2 — The run-level degrade sentinel (posture A and C residue).**
`brutex/observability/rust_sentinel.py` already implements the inversion:

> Proving "Rust ran 33 billion times" is not a thing a log can do. Proving
> "nothing ever fell back" is, because the house pattern **requires** a
> fall-back to announce itself exactly once.

It classifies on **stems, not exact suffixes** — a decision forced by
measurement: an exact-suffix set missed twelve of eighty-nine real degrade
events (13%, including a wheel-REQUIRED seam's fall-back) because the house
also emits `kernel_autoreverted`, `render_autoreverted`, `shadow_mismatch`,
`call_fallback`. It fires only when **both** the stem matches **and** the
namespace is a derived Rust-facade namespace — because
`auto_resume.disabled`, `bhavcopy.mismatch` and seven more ordinary feature
events end in a watched suffix and a suffix-only sentinel would have cried
wolf and been switched off within a day.

**This ADR rules that the sentinel is wired at three entrypoints:**

| Entrypoint | Mode | Consequence of a degrade |
|---|---|---|
| `cmd_sweep_all` (the brute-force sweep) | `strict=True` | `RustDegradedError` — the run refuses rather than silently finishing on Python |
| the dashboard sweep job | observe + latch | the verdict rides the run manifest; the run completes |
| `brutex doctor` | observe | a named stage, reported |

The strict-at-sweep choice is the one that answers the operator's question
directly: **a completed sweep is, by construction, a sweep in which no enrolled
facade fell back** — for its entire duration, not for a sample.

**Tier 3 — The census as a CI gate.** `rust_census.main()` already exits 1 on
any DEGRADED row and, under `--strict`, on OFF/UNKNOWN. It is wired as:

- a required CI step (`python -m brutex.observability.rust_census --strict`),
  so a facade that silently loses its flag, its crate or its wheel **fails the
  merge** rather than being discovered during a four-day sweep;
- a `brutex doctor` stage, so the same check runs on the operator's box where
  the wheels actually live;
- a **count pin** replacing today's `_MIN_FACADES = 25` floor: the census row
  count must equal the flag inventory named in #2215 plus the REQUIRED set, so
  a facade dropped by a discovery bug fails a test (§D9).

**What the three tiers together do NOT prove** — stated so the guarantee is
not oversold:

- A CLEAN sentinel proves **no fall-back happened**. It does **not** prove
  O(1) (§D8), and #2261 is the measured counterexample.
- The sentinel observes loggers reachable from the *current* structlog
  processor chain; a module logger bound before logging was configured is a
  narrow, named residual.
- Posture-C **eligibility gates** (§D3 M12, M13) route to the oracle **before
  the crate is ever called and emit nothing at all** — they are invisible to
  both census and sentinel by construction. Making them emit a countable
  `.eligibility_declined` event is named future work, not claimed here.
- The census reads *flag + importability*. A facade that already autoreverted
  still reports ENGAGED; only the un-throttled ERROR — and now the sentinel —
  catches that.

### D8 — Binding P3 (O(1)) to the merge gate

**P3 has its own architecture, and it is designed in ADR-0027**
(DEC-O1-ENFORCEMENT-REACH-001). This section states only the interface
between the two: P1/P2 are proved by posture and by the sentinel; P3 is
proved by a ratio gate at a named tier, and the two proofs never substitute
for each other. #2261 is the standing counterexample — a fully engaged Rust
hot path measuring 94.46× growth with every correctness test green.

**Correction to this section's first draft, recorded rather than quietly
edited (Golden Rule #10).** The draft asserted that the ≥50 M/s throughput
canary *"computes ops/sec at three sites and never compares — no assert at
all."* That is **wrong**, and re-measuring caught it:
`tests/bench/test_throughput_absolute.py:167` asserts
`ops_per_sec >= 50_000_000` and `tests/bench/test_trade_sim_throughput.py:187`
asserts the mask·bar rate against the same floor. The three no-assert sites
are in `test_throughput_canary.py`, which is a *ratio* test that prints the
absolute figure alongside — and says so in its own comment: *"Informational
… even though we don't gate on it."* The draft also under-stated the
path-filter gap (it named three sources; the measured figure is 13 of 15).
ADR-0027 carries the corrected numbers.

### D9 — Census fixes (the discovery holes)

1. **Replace the name pre-filter with a positive marker.** Deleting the filter
   outright is the wrong fix — it admits ~28 false positives (any module with
   some `*_ENV` and some `*_enabled`: `market_hours`, `sweep_apriori`,
   `loop_watchdog`…). The discriminator that measured **exactly right** is
   *"the `*_ENV` constant's own name contains RUST"*, which admits precisely
   `cvar_gate` and `greeks_enrich` and nothing else.
2. **Reconcile the docstring.** It says "Structural discovery", which is what
   the signature test does — but the name filter runs *before* it. The
   docstring is a divergence, not just the code.
3. **Give `brutex_identity` a facade.** `results_store.py:136`'s bare import
   becomes a `require_identity_kernel()` with a `…UnavailableError` and a
   remediation string, so REQUIRED seam #6 becomes visible to the census and
   its refusal names the DEC that deleted its oracle.
4. **Replace `_MIN_FACADES = 25`** with the exact-count pin of §D7 Tier 3.
5. **Disclose the one seam that no wheel-based signal can reach.**
   `brutex/dashboard/rust_front.py` engages Rust by **spawning the `brutexd`
   binary**, not by importing a wheel. Every discovery signal the census has —
   `importlib.util.find_spec` on a crate name, a `*_enabled()` callable, a
   degrade announcement — asks a question that has no answer for a subprocess
   seam: there is no module to locate. Covering it needs a *different*
   mechanism (a binary-presence probe on `PATH` / the configured
   `brutexd` location), which is real work and not a one-line filter change.
   Until that lands it is listed here as a **known, named exclusion** rather
   than silently absent, and the `discover_facades` docstring carries the same
   disclosure at the code. It is off the sweep path (a dashboard static/health
   sidecar, default-OFF `BRUTEX_RUST_FRONT`), so it does not weaken the
   sweep-path guarantee — but it does mean "42 rows" is not "every Rust seam
   in the repo", and this ADR must not imply that it is.

---

## Consequences

**Gained.** One vocabulary for a posture that four lanes had been improvising.
A promotion ladder whose every rung has a paid-for precedent. A MUST-STAY
register that converts "why is there still Python here?" from an argument into
a lookup — and stops a well-meaning PR deleting an oracle that is the only arm
for `Decimal` NaN or a `2**64` episode count. Most importantly: a stated,
mechanical answer to *"how will you ensure it"* that does not depend on
anyone's assertion — a strict sentinel makes a degraded sweep **refuse**, and a
census gate makes a lost facade **fail the merge**.

**Costs, stated.** Strict-at-sweep converts a silent degradation into a hard
failure: an operator whose wheel is stale now gets a refusal where they used to
get a slower answer. That is the intent, and the remediation string is why B4
exists. The census gate adds a required CI step. Neither changes any engine
result.

**Not changed by this ADR.** No flag flips, no oracle is deleted, no
`brutex/` behavior moves. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX. Run
identity, the lake schema, the vocabulary and the rate laws are untouched.

---

## Alternatives considered

**Convert all 32 ENGAGED facades to REQUIRED.** Rejected on the evidence: §D3
finds ~22 of them permanently non-deletable for reasons ranging from libm
bit-portability to wheel-free boot. Attempting it would either break the
dashboard's wheel-free boot (M15) or silently drop the only implementation for
refused input domains (M12, M13). Posture C exists because the alternative is
not available, not because the work is unfinished.

**Prove Rust ran by counting successes.** Rejected as structurally impossible:
the `.engaged` event fires once per process by design, so success-counting
cannot distinguish "answered once" from "answered always". The inversion —
watch the far smaller set of events that mean *stopped answering* — is the only
form the proof can take, and it is why the sentinel is the load-bearing tier.

**Suffix-only degrade detection.** Rejected by measurement: nine ordinary
non-Rust events end in a watched suffix, so a suffix-only sentinel would latch
on an unrelated feature flag and be disabled within a day.

**Delete oracles under the greenfield ruling without a corpus.** Rejected: the
ruling voids the bit-match premise, not the completeness premise, and #1979's
post-delete residual is the measured cost of reading it the other way.

---

## Honest envelope

1. **This ADR is design. It ships no code.** Nothing here is claimed as done.
   The census/sentinel wiring, the promotions, and the O(1) bindings are each
   separate PRs with their own DECs.
2. **The census numbers are measured, on this branch, at this commit** — 37
   discovered, 32 ENGAGED, 5 REQUIRED, plus 2 missed by discovery and 1
   invisible seam. They are not stable across future facade work; that is
   exactly what the §D9 count pin is for.

   **Re-measured 2026-07-31 after the §D9 discovery fixes landed:
   42 discovered — 38 ENGAGED, 4 REQUIRED, 0 DEGRADED, 0 OFF, 0 UNKNOWN.**
   Two deltas are worth stating plainly rather than leaving as arithmetic:
   - 37 → 42 is the §D9.1 marker fix (`cvar_gate`, `greeks_enrich`) plus
     three further seams the widened signals reached (`exec_rust`,
     `data.lake_keys_rust`, `engine.rust_parity_core`).
   - **REQUIRED went 5 → 4, and that is a correction, not a regression.**
     `engine.rust_trade_sim` was being promoted by a *text* match on the word
     "required" appearing in its docstring. The required-seam test is now an
     `ast.Raise` walk, so a module is REQUIRED only when it actually raises a
     refusal. `rust_trade_sim` still appears — it is now discovered by its
     inline `rust_trade_sim.fallback` announcement and classified honestly as
     ENGAGED, which is what it is.

   The count remains a snapshot, and it excludes the subprocess seam named in
   §D9.5.
3. **"Zero production callers" for census and sentinel is a grep result**, not
   an inference: `grep -rn 'sentinel_scope\|RustSentinel' brutex/` returns
   nothing outside the module itself, and no workflow mentions either module.
4. **A CLEAN sentinel is not an O(1) proof, and an O(1) bench is not a Rust
   proof.** #2261 measured 94.46× growth on a fully-engaged Rust hot path.
   P1, P2 and P3 keep three separate gates for that reason.
5. **The MUST-STAY register is derived from what the code and ledger say
   today.** Two of its entries (M17) are standing *operator exceptions* rather
   than technical impossibilities and could be revisited by the operator; the
   other sixteen are technical and are not negotiable by PR.
6. **Eligibility-gate degrades remain unobservable** (§D7). That residue is
   named here rather than papered over, and closing it is future work.
