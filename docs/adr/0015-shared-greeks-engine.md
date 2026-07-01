# ADR-0015 — Shared options-Greeks engine (IV + Delta/Gamma/Theta/Vega/Rho) in `trading-core`

**Decision ID:** `DEC-GREEKS-SHARED-CORE-001` (MIGRATION.md entry #1070), refined by
`DEC-GREEKS-GENERATION-TIME-001` (MIGRATION.md entry #1071, §2b below — the
generation-time compute-and-store refinement)
**Date:** 2026-07-01
**Status:** **PROPOSED** — awaiting operator go. This ADR is the design lock;
per Golden Rule #9 a 4-agent hostile review is run on THIS ADR before any engine
code lands (the build PRs follow). NO code ships under this ADR until the
operator records go; it is docs-only and fully reversible.
**Author:** SJParthi (operator-directed research → plan)
**Reverses (scoped):** `DEC-P0-025` (ADR-adjacent, PR #47 — "prune fno-math …
remove Greeks"). The reversal is SCOPED to the Track-2 options-realism leg + the
lake read-enrichment; the brute-force ENGINE stays NIFTY / BANKNIFTY / SENSEX
(CLAUDE.md §3). This mirrors how `DEC-SHARED-CORE-001` (MIGRATION.md #441)
re-established the shared indicator core after the same prune.
**Builds on:** `DEC-SHARED-CORE-001` (MIGRATION.md #441 — the shared
`trading-core` crate + the frozen golden answer-key pattern),
`DEC-FNO-MONEYNESS-001` (#865 — the per-bar read-time-derived F&O column
pattern + the underlying-spot join), `DEC-DB-FNO-COLUMNS-001` (#881 — the
generic `source="derived"` F&O columns), `DEC-OPTIONS-LAKE-READER-001` (#882 —
the `LakeOptionBarSource` seam that hard-codes `spot=None, iv=None`).
**Full plan:** `docs/GREEKS_ENGINE_PLAN.md`.

---

## 1. Context

The operator wants a COMMON shared options-Greeks engine (IV + Delta / Gamma /
Theta / Vega / Rho) for BruteX (backtest) + TickVault (live), surfaced per-bar
in the F&O candle table and unlocking the dormant delta-target strike rules.

Two ground-truth facts (verified against repository HEAD, re-confirmed on
`origin/main`; every `file:line` in `docs/GREEKS_ENGINE_PLAN.md` §1):

- **There is NO Greeks / IV / Black-Scholes code in BruteX today.** It was
  deliberately deleted in PR #47 / `DEC-P0-025` (`brutex/options/__init__.py:19-20`
  lists `risk_free` + `greeks` as TBD). A complete kernel is recoverable from git
  at `a87bce43^:rust/fno-math/src/greeks/black_scholes.rs` (2410 lines), which
  already ships BOTH `compute_greeks` (`:272`, solve from price) and
  `compute_greeks_from_iv` (`:312`, price off a given IV) — the exact
  live-vs-solved seam. It was calibrated to the retired vendor (a fixed
  `r = 0.10`, an RBI table, OI-based PCR) and pulls an external `jaeckel` crate,
  so it is a STARTING POINT, not a drop-in.
- **The Groww historical candle endpoint stores only OHLCV + OI — no IV, no
  spot on the option bar** (`brutex/data/parquet_writer.py` schema;
  `brutex/options/lake_source.py:215-216` hard-codes `spot=None, iv=None`; the
  7-element payload at `docs/groww/api-reference/03-backtesting.md:202-236`).

The load-bearing consequence: the Track-2 spec (`docs/track2/TRACK2_OPTIONS_SPEC.md`
§7.1 / §7.7 "vendor IV authoritative + 50 bp cross-check") is STALE — it was
written pre-ADR-0009 for the retired vendor's rolling-option endpoint that
returned an `iv` array. **IV must be SOLVED from the traded option price**, and
the numerical inversion is the PRIMARY IV source, not a cross-check.

---

## 2. Decision

**D1 — Reverse DEC-P0-025's Greeks deletion, SCOPED.** Re-introduce a Greeks /
IV engine, scoped to the Track-2 options-realism leg + the lake
read-enrichment. The brute-force ENGINE is UNCHANGED — NIFTY / BANKNIFTY /
SENSEX (CLAUDE.md §3). Greeks are lake / REFERENCE_ONLY: display columns +
execution realism, NEVER a 52→60-condition vocabulary input. This is NOT a
silent scope expansion (Golden Rule #3) — it is an explicit, operator-gated,
narrowly-scoped reversal recorded here + in MIGRATION.md #1070.

**D2 — ONE shared Rust `greeks` module INSIDE `rust/trading-core/`.** Not a new
crate, not `fno-math`. `trading-core` is already the bit-identical shared core
(`cdylib` + `rlib`, dependency-free, PyO3 wheel behind the optional `python`
feature — `Cargo.toml:31,38-39,49`) that BruteX consumes via the wheel and
TickVault links natively (`DEC-SHARED-CORE-001`). Adding a `greeks` module
inherits the wheel, the CI `pip install`, the `brutex.math` facade, and the
golden harness for free.

**D3 — ONE pure function with an OPTIONAL sigma (the live-vs-solved seam).**
`greeks(price: Option<f64>, sigma: Option<f64>, forward, strike, t_years, rate,
is_call) -> Result<GreeksOut, String>`. `sigma = Some` (TickVault live / a future
vendor IV) prices directly; `sigma = None` (BruteX historical) solves IV from
price first, then prices off the SAME closed form. Both converge on ONE Greek
kernel.

**D4 — Six Greeks, Black-76 on the forward.** `iv, delta, gamma, theta, vega,
rho`. Model family = Black-76 on `F = S·e^((r−q)T)` — algebraically identical to
BSM-with-`q` for European options (Hull 11e). Per-instrument model:

| Underlying | Style | Greeks |
|---|---|---|
| NIFTY / BANKNIFTY / SENSEX index options | European (spec §7.10) | Black-76 closed form — **EXACT** |
| ~212 single-stock F&O options (REFERENCE_ONLY) | American | closed-form NOT exact — **FLAGGED APPROX** (default) or NA |
| FUT legs | n/a | **NA** (`—`) |

Style is derived from the underlying's master `instrument_type` (IDX → European,
EQ → American); an UNKNOWN underlying defaults to the conservative branch.

**D5 — Reconcile the STALE spec premise BEFORE code.** Flip TRACK2_OPTIONS_SPEC
§7.1 / §7.7 from "vendor IV authoritative + 50 bp cross-check" to "IV SOLVED from
price" (the lake stores no iv / spot). Re-home Greeks from any stale
`fno-math/src/bsm.rs` phase-doc target into `trading-core`. Ledgered in
MIGRATION.md #1070 as the first step (Golden Rule #2).

**D6 — Frozen golden as the bit-identity contract.** A NEW
`tests/golden/fixtures/greeks_golden.json`
(`(S, K, T, r, q, type, price) → (iv, delta, gamma, theta, vega, rho)`), read by
BOTH a Rust `greeks_golden.rs` (bit-for-bit `assert_eq!` on `Vec<Option<f64>>`,
like `ema_golden.rs:31-56`) AND the Python parity test. This is the BruteX-side
contract both repos satisfy — the twin of `vocab_golden_vectors.json`. Only the
BruteX side + the frozen contract are claimed; TickVault-side bit-identity cannot
be proven without the TickVault repo (Golden Rule #10).

**D7 — Read-time source of truth; HYBRID storage.** Greeks are read-time-derived
(never stored in the source-of-truth path), extending `_fno_derived_rows`
(`db_view.py:2140`) off the EXISTING per-bar spot join (zero extra I/O) — the
same reasoning as moneyness. An OPTIONAL pull-time materialized cache MAY be
added LATER as a pure accelerator using the identical kernel (so values are
bit-identical), never as the source of truth.

**D8 — Surfaces (all read-only).** (a) `/db` F&O candle table — six derived
columns; (b) `/movers` options — `delta` / `iv` cells; (c) options executor —
activate the dormant delta-target strike rules 8 / 9 / 10 via a lake-backed
`DeltaChainProvider`, stamp `iv_at_entry`; (d) TickVault contract only.

**D9 — Honest edge handling.** NA / UNKNOWN is the default output; a fabricated
number is NEVER surfaced (mirrors the never-raises `moneyness.py`). Every emitted
row is stamped with the `r`-source + version, the `q`-bias direction, the
exercise-style, and the flag set. The full 17-row edge matrix is in
`docs/GREEKS_ENGINE_PLAN.md` §9.

---

## 2b. Refinement — GENERATION-TIME compute-and-store (`DEC-GREEKS-GENERATION-TIME-001`)

> A REFINEMENT of this ADR (`DEC-GREEKS-SHARED-CORE-001`), tracked under the
> working decision id `DEC-GREEKS-GENERATION-TIME-001` (MIGRATION.md #1071). It
> SHARPENS D7 (storage) — it does NOT delete D1–D9. Both this ADR and the
> refinement stay PROPOSED, docs-only, fully reversible, OPERATOR-OVERRIDABLE.
> Sourced from the 14-agent verification panel LOCKED design; the full detail is
> `docs/GREEKS_ENGINE_PLAN.md` §15. NONE of it is shipped (the shared Greeks kernel
> does not exist; the engine consumes ZERO Greeks — all 60 vocabulary bits are
> price / volume, `iv` is display / analysis only).

**D10 — HYBRID drawn on COST-CLASS (refines D7).** Compute-and-STORE at generation
time the EXPENSIVE family (`iv`, `delta`, `gamma`, `theta`, `vega`, `rho` + the
model-input provenance stamp), and KEEP the CHEAP family (`moneyness`, `spot`,
`intrinsic`, `time_value`, `days_to_expiry`) read-time derived (UNCHANGED). The
lever is a genuine cost asymmetry: moneyness is trivial O(1) arithmetic (free to
derive, staleness-proof), whereas each Greek carries a per-bar IV root-solve whose
iteration count dominates — paying that solve on every `/db` open + every `/movers`
rank forever, for IMMUTABLE past bars, is the waste store-once eliminates. A
surgical exception for the one family whose per-read cost is unbounded, NOT a
reversal of "moneyness is never stored" (preserved for the cheap columns).

**D11 — Additive lake-schema change + a `greeks_enrich_after_pull` hook.** Ten new
nullable columns appended after `open_interest` — `iv, delta, gamma, theta, vega,
rho` (Float64, FULL precision, NOT 2dp-snapped per `DEC-MONEY-2DP-001`),
`spot_at_bar` (Float64, the ONE new column 2dp-snapped as a price), `t_years_used`,
`rate_used` (Float64), and `greeks_provenance_id` (Int32, packing formula_version +
rate_source + dividend_bias + tte_convention + style_flag — the staleness key). Bump
`FNO_CANDLE_LAKE_SCHEMA_VERSION` 2 → 3; a legacy 7-column partition reads the new
columns as NULL, NO migration. A post-pull `greeks_enrich_after_pull` (the F&O twin
of `_resample_daily_after_pull`) joins each option bar to the same-minute spot
(exact `_utc_instant` join, NO nearest-fallback), solves IV, computes Greeks, and
writes via the partition-flock read-modify-write with `merge_mode="latest-wins"`;
incremental (only new / stale windows), idempotent, best-effort (never fails the
pull); missing spot / unsolvable price → NULL, never fabricated. The RAW-OHLCV
keep-first invariant is UNTOUCHED (`_bar_values_equal` stays OHLCV+OI-only — the
Greek overlay is `latest-wins`, so a Greek written against a partial / empty window
is corrected on re-pull, never frozen).

**D12 — Backtest ↔ live parity by construction.** ONE shared Rust kernel
`greeks(price, sigma, forward, strike, t_years, rate, is_call)` (D3), two call
sites: (A) post-pull enrichment `sigma=None` → solve IV then Greeks; (B) live
tick-aggregator `sigma=Some(vendor_iv)` → skip the solve (a DESIGN SEAM — the live
feed was removed, `DEC-LIVE-REMOVE-001` #862). Bit-identity via the NEW frozen
`tests/golden/fixtures/greeks_golden.json` read by BOTH a Rust golden test AND a
Python parity test (the twin of `vocab_golden_vectors.json`, D6). HONEST caveat:
transcendentals may force a DOCUMENTED TOLERANCE instead of literal bit-for-bit.

**D13 — CONDITIONAL and gated.** The store-at-generation write cannot ship until
(a) the shared kernel lands with a green frozen `greeks_golden.json`, and (b) the
rate source / dividend `q` / time-to-expiry convention (§7 open decisions
#3 / #4 / #5) are LOCKED — storing freezes them into the bytes. The schema columns
MAY ship first ready-and-NULL (honest + additive); no Greek byte until (a) + (b). A
later convention change is a clean O(bars) bulk re-enrich (the provenance stamp
makes it incremental / resumable), NOT a correctness hazard. O(1) HONESTY: per-bar
compute O(1) (except the bounded IV solve — a pinned bench gates the cap);
enrichment O(bars)-once-per-pull; the read becomes a plain O(1) column read;
backfill honestly O(all-stored-F&O-bars) — NOT a literal O(1)-total.

---

## 3. Accepted defaults (all operator-overridable)

Recorded as the RECOMMENDED starting positions; each is a default, not a lock.

| # | Decision | Default | Rationale |
|---|---|---|---|
| 1 | American single-stock options | **FLAGGED APPROXIMATION** (not NA) | so the ~212 stocks still get usable Greeks; the flag keeps it honest |
| 2 | Risk-free `r` | **configurable FIXED rate (~6.5%) to start**; RBI / T-bill curve as a follow-up | reproducible + matches NSE's own fixed-rate chain; curve is accuracy polish |
| 3 | Dividend `q` | **`q = 0`** with the documented bias | simplest / reproducible; the bias direction stamped on output |
| 4 | Time-to-expiry `T` | **NSE trading-minutes** (business-time) | more precise near expiry than calendar Act/365.25 |
| 5 | IV solver | **Jäckel "Let's Be Rational"** (pinned / vendored) | bounded, machine-precision, O(1); Newton diverges deep OTM / ITM |
| 6 | Scope | **lake / REFERENCE_ONLY** (display + options-realism) | Golden Rule #3 — the ENGINE stays NIFTY / BANKNIFTY / SENSEX |
| 7 | When to compute | **HYBRID** (read-time source-of-truth + optional pull-time cache) — REFINED by §2b `DEC-GREEKS-GENERATION-TIME-001` to a COST-CLASS hybrid: compute-and-STORE the expensive IV+Greeks family at generation time, KEEP the cheap moneyness family read-time-derived (conditional / gated) | flexible + fast; same Rust core → bit-identical; §2b stores only the family whose per-read IV-solve cost is unbounded |

---

## 4. Honest precision envelope (Golden Rule #10)

**EXACT:** European INDEX options (NIFTY / BANKNIFTY / SENSEX) via Black-76
closed form; delta even in the intrinsic-only regime; the Jäckel IV round-trip;
BruteX ↔ TickVault bit-identity IF both link the same compiled Rust (modulo the
cross-platform ULP caveat).

**APPROXIMATE / KNOWN-BIAS (stamped on output):** `q = 0` over-prices calls /
under-prices puts (~0.004% daily at NIFTY's trailing yield) — defensible because
fills use observed prices + delta ranking is relative; the ~212 single-stock
options are AMERICAN (closed-form not exact → flagged / NA); IV is SOLVED from
price (the spec §7.7 vendor-IV premise is stale); the `T` convention is a
modelling choice; `r` is a bias not a blowup (rho ≈ 1.3% / 100 bp).

**UNVERIFIED (flagged):** Groww's own IV methodology; the European-style claim
for the 3 indices (spec-asserted §7.10, re-confirm live before coding);
cross-platform float determinism for `exp / ln / sqrt / erf` (the golden is
bit-for-bit only if a pinned `libm` / `norm_cdf` is vendored, else a documented
tolerance).

---

## 5. Phased roadmap (all gated; Phase 0 = this ADR + plan, docs only)

| Phase | Deliverable | Status |
|---|---|---|
| **0** | this ADR + `GREEKS_ENGINE_PLAN.md` + MIGRATION.md #1070 / #1071 (spec reconcile + the generation-time refinement) | **THIS PR (docs only)** |
| 1 | `brutex/options/risk_free.py` (+ fixed-`r` const or T-bill CSV) — 100% gate | awaiting go |
| 2 | `rust/trading-core/src/greeks/` kernel + PyO3 + thin wrapper + frozen golden | awaiting go |
| **2b** | pull-time Greeks materialization — the `FNO_CANDLE_LAKE_SCHEMA_VERSION` 2→3 additive columns + the `greeks_enrich_after_pull` hook (`DEC-GREEKS-GENERATION-TIME-001`, §2b); gated on Phase 2's green golden + `r`/`q`/`T` locked | awaiting go |
| 3a | `/db` F&O candle-table columns — a plain STORED-column read with a read-time NULL-fallback (was: six derived columns) | awaiting go |
| 3b | `/movers` options `delta` / `iv` cells | awaiting go |
| 3c | options-executor delta-target rules 8 / 9 / 10 + `iv_at_entry` | awaiting go |
| 4 | TickVault contract (frozen golden + shared kernel; adoption out-of-repo) | awaiting go |

---

## 6. Consequences

**Positive.** Options-realism gains real Greeks (delta-target strike selection,
`iv_at_entry`); the `/db` + `/movers` surfaces gain a standard options risk
dashboard at zero extra I/O; BruteX + TickVault share ONE compiled core with a
frozen bit-identity contract; every claim is honest (NA over fabrication, biases
stamped, UNVERIFIED items flagged).

**Negative / risks.** A new transcendental numeric surface in `trading-core`
(the cross-platform determinism risk — Open Decision #6); an external data
dependency for `r` (Phase 1); the American single-stock approximation is inexact
by construction (flagged); the spec §7.7 reconcile must land first (Golden Rule
#2). All are recorded, none hidden.

**Reversibility.** This ADR is docs-only and PROPOSED — nothing ships until the
operator records go. Each subsequent phase is an independent gated PR that can be
paused or reverted. HONEST update under §2b `DEC-GREEKS-GENERATION-TIME-001`: the
earlier "read-time derivation adds no lake-schema change to reverse" clause is now
FALSE for the generation-time refinement — Phase 2b adds the
`FNO_CANDLE_LAKE_SCHEMA_VERSION` 2 → 3 additive columns + a backfill, so it is
HARDER to reverse than a pure read-time derive. It is nevertheless bounded: the
columns are nullable + appended (a legacy 7-column partition still reads, no
forward-migration needed), the stored Greeks are a re-derivable overlay off the
same shared kernel (the read-time §4 path is the standing NULL-fallback), and the
RAW-OHLCV bar is untouched — so reverting Phase 2b means dropping the enrich hook
and reading the columns as NULL, never re-fetching or losing raw data. The
CHEAP-family read-time derivation still adds no lake-schema change to reverse.

---

## 7. Open decisions

The nine open decisions (model-per-instrument, scope confirmation, `r` source,
`q`, `T` convention, cross-platform determinism / golden tolerance, IV-solver
sourcing, storage, and whether Greeks ever feed the engine) are enumerated in
`docs/GREEKS_ENGINE_PLAN.md` §13, each with its §7 recommended default. The
operator resolves them at go / during Phase 1-2.

**Storage resolution + coupling (§2b `DEC-GREEKS-GENERATION-TIME-001`).** Open
decision #8 (Storage) is RESOLVED toward COMPUTE-AND-STORE for the expensive
IV+Greeks family (the additive `FNO_CANDLE_LAKE_SCHEMA_VERSION` 2 → 3 columns + the
`greeks_enrich_after_pull` hook), keeping the cheap moneyness family read-time
derived. This resolution is CONDITIONAL and gated (§2b D13): it cannot ship until
Phase 2's frozen `greeks_golden.json` is green AND the COUPLED open decisions
#3 (`r` source) / #4 (`q`) / #5 (`T` convention) are LOCKED FIRST — because storing
freezes them into the bytes. A later change to any of those is a clean O(bars) bulk
re-enrich (stamp-driven, incremental / resumable), not a byte-level correctness
hazard. All of these remain OPERATOR-OVERRIDABLE.
