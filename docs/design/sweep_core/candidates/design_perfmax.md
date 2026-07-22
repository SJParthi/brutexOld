# design_perfmax.md — Rust sweep-core, PERFORMANCE-MAX lens

Independent design candidate. 2026-07-08. Lens: MAXIMUM orchestration removal —
Python out of the per-mask path entirely, then work backward to bit-identity and
safety. Grounded in `baseline.md`, `SPEC_RUST_SWEEP_CORE.md`, `combo_counts.md`,
`reads/*.md`, and the repo at 46895ee5. Independence rule honored (no other
candidate read).

---

## 0. The core bet, in one paragraph

Give each tuple ONE resident Rust object — `SweepCoreRun` in a new
`rust/fno-math/src/sweep_core/` module — that owns the ENTIRE
enumerate → conflict-prune → Stage-1B-prefilter → trade-walk → per-mask-metric-fold
pipeline, and returns bounded **columnar batches** of kept-mask metric scalars.
Python never sees a trade, never sees an enumerated mask, never calls a per-mask
function. What Python does per KEPT mask is reduced to: read ~37 scalars out of
returned columns, compute the 3 libm-touched fields (cagr → calmar/mar; skew/kurt
deferred to finalists), construct one `ResultMetrics`, and run the UNTOUCHED
shared internals (`passes_gates`, `composite_term_inputs`,
`SurvivorColumns.append`) — ≈ 6–10 µs/kept, with a flag-gated Slice-3 that shrinks
it to ≈ 2–3 µs by moving the gate/term feed into Rust behind the same oracle
machinery. Everything durable (rank → finalists → store → sidecar → sentinel →
audit) stays a Python per-tuple epilogue, byte-for-byte today's code. The
**absolute minimum Python per tuple** is: sentinel check, plan, 1 audit line, one
core-session build, a bounded batch-drain loop doing O(kept) column consumption,
rank, finalist projection + belt, flush/sidecar/sentinel, 2 audit lines.

Why this is the right maximal cut and not more: the four things deliberately NOT
moved (rank fold, finalist projection, persistence, run-identity hashing) are
per-TUPLE O(kept)/O(top_k) costs measured at ~1.3 µs/mask amortised or less
(`baseline.md` §2) — moving them buys < 1 % and spends the bit-identity budget on
the store-byte and audit-chain contracts (Decimal/ROUND_HALF_UP prices, sha256
audit with Python `str(float)`, `math.fsum` sidecar) that are the most expensive
to prove in Rust. Maximum *effective* removal = remove everything that scales
per-mask/per-trade; keep everything that scales per-tuple.

---

## 1. (a) Architecture + exact FFI surface

### 1.1 Crate placement: `rust/fno-math/src/sweep_core/` (module, NOT a sibling crate)

Justification (constraint 2):

1. The core must consume `trade_sim.rs::simulate_one_core<W>` / `MaskWord` /
   `SweepColumns` / `build_columns` and `metrics.rs::drawdown_stats_checked`
   **directly** — same-crate reuse is a plain `use crate::…`. A sibling crate
   would need a path-dep on fno-math's rlib, which (i) creates the documented
   source-digest blind spot (`reads/rust.md` §4: a path-dep's drift does NOT move
   the dependent's `__source_digest__`), (ii) forces either making those
   internals `pub` across a crate boundary or duplicating them (§11 COMMON
   violation), and (iii) adds a whole new wheel to the 27 ci.yml install sites,
   the math facade golden, EXPECTED_BUILD handshake, doctor stages, and
   branch-protection job list.
2. Mutation/CI wiring already exists: fno-math's 6-shard cargo-mutants matrix
   with the ledgered "+1 shard" dial (`reads/tests.md` §5) — `sweep_core/*.rs`
   joins `examine_globs` + one new shard.
3. `rust/trading-core` is untouched (DEC-SHARED-CORE-001); the 400-case
   answer-key and the `_indicator_version()` string are unaffected (fno-math's
   version is not in run identity — `reads/rust.md` §4).

Layout: `src/sweep_core/mod.rs` (pyclass + FFI wrappers, one-line delegates for
cargo-mutants viability), `enumerate.rs` (k-of-n lexicographic combination
cursor + conflict mask pairs), `metrics_port.rs` (the O(trades) metric folds —
the bit-identity surgery site), `batch.rs` (columnar accumulators + batch
plumbing). Pure `Result<_, String>` cores throughout; PyErr only in `mod.rs`
wrappers (links under no-libpython `cargo test`; keeps mutants viable —
`reads/rust.md` §1).

### 1.2 FFI surface (exact)

```rust
// ── Session: one per tuple with kept masks. Frozen pyclass; the only
//    interior mutability is a Mutex<Scratch> (equity i64 buffer + trade
//    Vec reused across masks — allocation-free steady state; GIL-serialised
//    in practice, Mutex satisfies Sync for #[pyclass(frozen)]).
#[pyclass(frozen, module = "fno_math")]
struct SweepCoreSession {
    cols: SweepColumns,            // open/high/low/close/is_forced/session_idx (reused type)
    bar_bits: BarBits,             // enum U64(Vec<u64>) | U128(Vec<u128>) — width picked at build
    scratch: Mutex<Scratch>,
}
#[pymethods] impl SweepCoreSession {
    #[new]
    fn new(open: Vec<i64>, high: Vec<i64>, low: Vec<i64>, close: Vec<i64>,
           is_forced: Vec<bool>, session_days: Vec<i64>,
           bar_bits_lo: Vec<u64>, bar_bits_hi: Vec<u64>) -> PyResult<Self>;
    #[getter] fn n_bars(&self) -> usize;
}

// ── Run: one per (session, tuple-config). Holds the enumeration cursor +
//    tallies. NOT frozen (cursor state); single-threaded use per tuple.
#[pyclass(module = "fno_math")]
struct SweepCoreRun { /* Py<SweepCoreSession> + cursor + config + tallies */ }
#[pymethods] impl SweepCoreRun {
    #[new]
    fn new(session: Py<SweepCoreSession>,
           condition_bits: Vec<u8>,      // ALREADY sorted-unique (Python owns set-collapse)
           k: usize,
           conflict_pairs: Vec<(u8, u8)>,// passed from vocabulary.CONFLICT_PAIRS — Python
                                         // stays the single source; Rust never re-mints
           min_hits: u64,
           is_buy: bool, sl_paisa: i64, tp_paisa: i64,
           sl_next_open: bool, tp_next_open: bool, tp_first: bool,
           start_equity_paisa: i64,
           span_years: f64,              // precomputed in Python (identity rule; never re-derived)
           minutes_per_year_fallback: f64) -> PyResult<Self>;

    /// Drain the next batch. Enumerates until EITHER `max_kept` kept masks are
    /// accumulated OR `max_work_cells` (enumerated_masks × n_bars, saturating)
    /// is consumed OR the combination space is exhausted. Returns None when done.
    fn next_batch(&mut self, max_kept: usize, max_work_cells: u64)
        -> PyResult<Option<SweepCoreBatch>>;

    #[getter] fn tallies(&self) -> (u64, u64, u64, u64); // enumerated, conflict_pruned,
                                                         // evaluated, minhits_dropped (running)
}

// ── Batch result: columnar, one entry per KEPT mask, enumeration order.
#[pyclass(frozen, module = "fno_math")]
struct SweepCoreBatch { /* Vec-backed columns */ }
#[pymethods] impl SweepCoreBatch {
    #[getter] fn n_kept(&self) -> usize;
    #[getter] fn tallies_delta(&self) -> (u64, u64, u64, u64);
    /// Masks needing the Python oracle (envelope exits), as indices into this
    /// batch's kept order. Sparse; empty on every in-envelope batch.
    #[getter] fn oracle_needed(&self) -> Vec<usize>;
    /// The metric columns. Ints as Vec<i64>, floats as Vec<f64>, Optionals as
    /// (Vec<f64>, Vec<bool> present) pairs. Exposed as ONE method returning a
    /// dict-free fixed tuple of lists (order FROZEN, documented, test-pinned):
    fn columns(&self, py: Python<'_>) -> PyResult<Py<PyTuple>>;
}
```

`columns()` tuple layout (FROZEN order; `.pyi`-typed; a `COLUMNS_LAYOUT_V1`
docstring constant pins it):

| slot | column | type | feeds |
|---|---|---|---|
| 0,1 | mask_lo, mask_hi | list[int] u64 limbs | identity/rank |
| 2 | net_pnl_paisa | int | metrics + cagr ingredient |
| 3 | total_return_pct | float | metrics |
| 4–5 | gross_profit/gross_loss_paisa | int | metrics + omega |
| 6–8 | total/winning/losing_trades | int | gates/metrics |
| 9 | win_rate | float | gate/term |
| 10 | (profit_factor, present) | float+bool | gate/term |
| 11–14 | avg_win/avg_loss/expectancy/largest_losing (paisa) | int | metrics |
| 15 | (win_loss_ratio, present) | float+bool | gate/term |
| 16–17 | avg_mfe/avg_mae_paisa | int | term 7 |
| 18–19 | max_consec_wins/losses | int | gate/metrics |
| 20–22 | max_dd_paisa / max_dd_pct / max_dd_duration_bars | int/float/int | gate/metrics |
| 23 | ulcer_index | float | metrics |
| 24 | (recovery_factor, present) | float+bool | term 5 |
| 25 | (sharpe, present) | float+bool | term 1 + SIDEcar |
| 26 | (sortino, present) | float+bool | term 2 |
| 27 | (information_ratio, present) | float+bool | metrics |
| 28 | (omega, present) | float+bool | metrics |
| 29 | (tail_ratio, present) | float+bool | metrics |
| 30–31 | avg_holding (float) / max_holding (int) | | metrics |
| 32 | time_in_market_pct | float | metrics |
| 33–36 | moments n/mean/m2/m3/m4 (n int; rest float) | | Python skew/kurt (finalists) |
| 37 | end_equity_paisa | int | Python cagr |

(Optionals are (value, present) pairs — a `None` is NEVER NaN-encoded; SPEC §4
row 9, the SurvivorColumns None-bit precedent.)

**What deliberately does NOT cross the boundary:** trades (never leave Rust),
exit indices (consumed internally by the fused drawdown fold), per-mask
timestamps, run_ids (lazy Python, finalists only — `reads/dedup.md` §5), audit
lines (zero from Rust — I12), dedup probes (digest path never consults —
constraint 6; the raising `DigestPathNoDedup` stays wired and untouched).

**Finalist rematerialisation:** none needed from Rust. Finalists get full
`ResultMetrics` from the SAME columns (Python keeps the whole batch's columns
only until they are folded into `SurvivorColumns` + a bounded
finalist-candidate cache — see §3), and the permanent finalist BELT recomputes
them via the retained Python spine anyway (§5).

### 1.3 What runs inside `next_batch` (all Rust, per enumerated mask)

1. **Enumeration**: lexicographic k-combinations over the given sorted-unique
   bits — a simple index-vector cursor, bit-OR fold to (lo,hi) u128 limbs.
   Bit-exact vs `iter_masks_k_of_bits` by construction (integer only);
   parity-property-pinned (§7). ~5–15 ns/mask.
2. **Conflict prune**: precomputed pair masks `(a_bit|b_bit)` — reject iff
   `mask & pair == pair` for any of the 26 pairs. Integer-exact port of
   `has_conflict`. ~5–10 ns.
3. **Stage-1B prefilter**: `count_one(bar_bits, mask)` reusing the existing
   supermask count core (u64 lane when everything fits, u128 otherwise —
   `MaskWord` generic, one core, anti-drift-pinned). `hits < min_hits` → drop
   (counted). O(n_bars): 0.257 ns/bar sandbox — the irreducible floor per
   surviving-enumerated mask; the hit-list-intersection upgrade is explicitly
   OUT (Q8, phase-next DEC).
4. **Trade walk**: `simulate_one_core<W>` — the EXISTING proven walker,
   unmodified — writing trades into the reused scratch Vec (no per-mask alloc).
5. **Metric folds** (`metrics_port.rs`): the §4 bit-identity ports — integer
   aggregates, Welford/Pébay moments, sorted-returns percentiles, downside dev,
   drawdown via `drawdown_stats_checked` with a REUSED `&mut [i64]` scratch
   (internal refactor: extract a `drawdown_stats_into(scratch, …)` that the
   existing `sweep_drawdown_stats` pyfunction calls with a fresh vec —
   pyfunction behavior byte-identical, alloc is not identity).
6. **Envelope check**: `start + Σ|pnl| > 2^52` (exact i64 checked math) or any
   other envelope exit → do NOT fold; push the kept-index onto `oracle_needed`
   and emit placeholder column slots (Python overwrites from the oracle spine
   before consumption). Zero-trade masks fold trivially in Rust (FLAT stats,
   `_empty_metrics` field values replicated exactly, +0.0 signs — pinned).
7. Append scalars to the batch columns.

Parallelism: masks within a batch MAY be rayon-parallel (per-mask work is
independent; each mask's FP folds are internally strictly sequential; output
order preserved via IndexedParallelIterator collect — the existing dispatch
law). Under `sweep_parallel` workers `RAYON_NUM_THREADS=1` pins it serial —
results identical by the order-preserving law either way (`reads/parallel.md`
§2). No `build_global`, no `allow_threads` (crate conventions).

---

## 2. (b) What moves to Rust vs stays Python — per item

| Item | Where | Why |
|---|---|---|
| Mask enumeration + conflict prune | **Rust** | 2.4 µs → ~20 ns; integer-exact, zero float risk; parity-pinned. CONFLICT_PAIRS passed IN from Python (single source). |
| Stage-1B prefilter | **Rust (in-core)** | already Rust; fusing removes the survivors list marshal (O(enumerated) ints/tuple today). |
| Trade walk | **Rust (existing core reused)** | already proven bit-identical (#563/#1182); zero new walk code. |
| Trade→Python materialisation (`ClosedTrade`, exit lists) | **DELETED from the hot path** | the measured 98+17+32 µs/kept; trades never cross the FFI. Retained verbatim in `rust_trade_sim.py` as oracle/fallback (constraint: file stays; goldens monkeypatch it on the OFF arm). |
| Drawdown family | **Rust (existing checked core, scratch-reused)** | removes 130 µs FFI+alloc; fold order untouched. |
| O(trades) metric body (integer aggregates, moments, percentiles, downside dev) | **Rust (new port)** | the 55 µs Python body; §4 gives the per-operation identity argument. Python `_compute_metrics_impl` retained as the oracle. |
| cagr_pct, skewness, kurtosis, calmar/mar (cagr-derived) | **Python, per kept (cagr/calmar/mar) or per finalist (skew/kurt)** | libm `pow` has no correct-rounding guarantee (#1186 precedent, SPEC §4 row 3). cagr is one `**` per kept (~0.3 µs); skew/kurt consumed by NO gate/term (verified: `passes_gates` reads pf/dd_pct/win_rate/wlr/trades/consec-losses; `composite_term_inputs` reads sharpe/sortino/calmar/pf/recovery/wr·wlr/mfe÷mae — `ranking/__init__.py:192-300`) so they are computed lazily only when a full `ResultMetrics` is built. |
| `ResultMetrics` construction | **Python, per kept (Slice 1) → per finalist-candidate only (Slice 3)** | frozen-dataclass identity + the -0.0→+0.0 normalisation at ResultRow stays Python; ~2–3 µs. |
| `passes_gates` + `composite_term_inputs` + `SurvivorColumns.append` + watermark | **Python (Slice 1); Rust feed with Python oracle (Slice 3, separate flag)** | §11 COMMON: these are THE pinned single sources shared with batch rank; Slice 1 keeps them byte-for-byte. Slice 3 duplicates them in Rust ONLY behind the burn-in/belt oracle + dense property (the #1186 pattern for porting an oracle-checked copy). |
| Rank (`rank_survivor_columns` z-fold, sort) | **Python** | O(kept)/tuple ≈ 1.3 µs/mask amortised; bit-pinned shared internals; bounded ≡ batch already proven (I11). |
| Finalist projection (`ranked_combo_to_result_row`, Decimal prices) | **Python** | explicit #1182 non-goal; hashed store bytes; Q6 names the follow-up. |
| run_id/blake3, sentinel, sidecar (`math.fsum`), store flush, audit | **Python** | per-tuple; the identity/canonical-JSON matrix (`reads/identity.md` §5) is exactly where cross-language re-serialisation risk lives — do not spend budget there. Sidecar consumes the returned sharpe column + present bits, fsum'd in Python exactly as today. |
| Sentinel resume, plan_rank_memory, tallies fold | **Python** | per-tuple, crash-protocol-load-bearing. |
| Legacy no-digest dedup path | **Python (today's loop, untouched)** | the core serves the digest path ONLY; `plan_launch_dedup` shape unchanged; the raising stand-ins stay the tripwire. |

**Rank-arm decision:** the core path always accumulates via `SurvivorColumns`
(the bounded/columnar arm) regardless of `plan_rank_memory`'s batch-vs-bounded
verdict — licensed by the pinned invariant I11 (bounded ≡ batch bit-for-bit,
same shared internals, same survivor order). The plan still runs (refusals +
watermark budget unchanged); when the plan says "batch", the core path uses the
columnar accumulator with `bounded_top_k = top_k` and the SAME
`_rebuild_bounded_finalists`-shaped finalist materialisation. Twin-run proves
byte-equality; a dedicated pin asserts plan-batch-vs-plan-bounded equality on
the core arm. (`sweep_run.bounded_rank_engaged` observability gains a
`core=true` field — structlog only, never audit.)

---

## 3. (c) Batching strategy, worker partitioning, checkpoints, kill -9

**Batch = a bounded drain of the tuple's enumeration cursor, in memory only.**

```
Python (new brutex/engine/sweep_core_run.py, called from run_persisted_sweep):
  session = SweepCoreSession(…)          # one FFI column copy per tuple
  run = SweepCoreRun(session, …)
  while (batch := run.next_batch(max_kept=65_536, max_work_cells=1<<31)):
      splice oracle_needed via metrics_for_mask (Python spine, in order)
      per kept mask (in order):
          cagr/calmar/mar from columns → ResultMetrics → burn-in/belt hooks
          → passes_gates/composite_term_inputs → SurvivorColumns.append
          → owned_nbytes watermark (unchanged SweepMemoryBudgetError contract)
      fold tallies_delta
  rank_survivor_columns → finalists → belt → project → flush → sidecar → sentinel
```

- `max_kept = 65_536` bounds returned-column memory to ~20 MB/batch
  (38 columns × 8 B × 64 Ki); `max_work_cells = 2^31` bounds the time INSIDE
  one FFI call to ~0.5–1 s even when everything is conflict/prefilter-pruned
  (0.26 ns/cell sandbox) — keeping Ctrl-C latency, the opt-in look-ahead
  guard's Python-side liveness, and any #1295 in-process observability beats
  at ~1 s granularity. Both knobs are **result-invariant by construction**
  (pure batching of a deterministic stream) and pinned by batch-invariance
  tests across ≥5 (max_kept, max_work_cells) shapes (the #1182 chunk pattern).
- **Batch boundaries vs worker partitioning:** the core sits strictly INSIDE
  one tuple INSIDE one worker process (`reads/parallel.md` §8: one worker runs
  one tuple; batching across tuples would cross the checkpoint/audit/sentinel
  unit — forbidden and not done). Worker partitioning (tuple dispatch, reorder
  heap, contiguous-clean-prefix checkpoint) is UNTOUCHED — zero
  `sweep_parallel.py` edits (ownership zones, constraint 5).
- **Checkpoints / kill -9 (fully-durable-or-fully-redone):** there is NO
  durable state below the tuple. All batch accumulation lives in process
  memory (`SurvivorColumns` + the Rust run's cursor). A `kill -9` at ANY
  point — mid-FFI-call, between batches, during rank — leaves the sentinel
  absent → next run redoes the FULL tuple and rewrites every artifact
  byte-identically (sentinel-authoritative redo, DEC-MARK-MANY-SENTINEL-001).
  A batch is therefore trivially "fully durable or fully redone": it is never
  durable at all. Per-tuple checkpoint (parallel driver writer-actor) and the
  #1295 run-core lifecycle wrap ABOVE this loop and see one opaque tuple, as
  today.
- **#1295 / #1291 composition:** both verified file-disjoint from the surgery
  site. A kernel panic under `panic=abort` = worker SIGABRT → #1291's
  pool-broken quarantine; repeated deaths → #1295's crash-loop breaker, whose
  runbook gains `BRUTEX_SWEEP_RUST_CORE=0` as lever #1 (no auto-env-flip —
  open_questions Q5 stands as a policy item, not taken here). A burn-in/belt
  mismatch is NOT a crash: the tuple restarts on the Python spine in-process
  (§5) — no breaker interaction.
- **Gap-register interactions (rows #3/#4):** row 3 (tuple-cache identity
  gains overfit/stage3 config) is P2's zone — this design changes NO cache/
  checkpoint identity input and is engine-blind in run identity, so it
  composes with whatever P2 ships. Row 4 (`sweep_runs/` sibling root) — this
  design writes NOTHING new to disk (no new artifacts), so the `.parquet`-glob
  hazard is structurally avoided.

---

## 4. (d) Bit-identity strategy per float operation

Ship condition restated: same inputs → byte-identical run_ids / metrics /
rankings / stored bytes, twin-run proven at one commit (both arms, separate
results roots); the kill-switch arm is literally today's code (untouched
modules), hence bit-identical trivially.

The trade walk and drawdown fold are already-proven surfaces (reused, not
re-written). The NEW surface is `metrics_port.rs` = the port of
`_compute_metrics_impl`'s folds. Per-operation register (extends SPEC §4 with
this design's resolutions):

| # | Operation | Resolution |
|---|---|---|
| 1 | Integer aggregates + floor-divs (`expectancy = net_pnl // n`, avg_win/loss/mfe/mae/holding int divs) | i64 `checked_*`; Python `//` = FLOOR division → `i64::div_euclid` (test: negative-numerator boundary, e.g. −7//2 = −4, exact-value pinned + mutant-killed). Overflow impossible in-envelope: prices ≤ i64::MAX/8 (walk ceiling) and equity ≤ 2^52 (pre-guard); any violation → `oracle_needed`, never a panic. |
| 2 | `total_return_pct = 100.0 * net_pnl / start_equity` | exact expression order `100.0 * (net_pnl as f64) / (start as f64)`; int→f64 conversion round-half-even in BOTH languages, exact ≤ 2^53 (guaranteed by the 2^52 envelope). |
| 3 | **libm-touched**: `growth ** (1/span_years)` (cagr), `pow(…, 1.5)` (skew), kurtosis, and their derivatives calmar = `_safe_ratio(cagr_pct, max_dd_pct)`, mar = calmar | **stay in Python by construction.** Rust returns `end_equity_paisa` + moments (n, mean, m2, m3, m4). Python computes cagr per kept mask with compute.py's EXACT expression incl. the ruin branch (`end_equity < 1 → −100.0`), `_CAGR_CAP_PCT` cap and `max(−100.0, …)` clamp — token-for-token, in the new consumption module which IMPORTS a newly-extracted pure helper from `metrics/compute.py` (no copy; `_compute_metrics_impl` refactored to call the same helper — byte-identical, covered by existing tests). skew/kurt likewise via extracted helpers, computed only when a full ResultMetrics is materialised. Zero cross-language pow anywhere. |
| 4 | Welford/Pébay moments | port the EXACT update expressions, same operand order, strictly sequential left-to-right in trade order; +,−,×,/ only (IEEE-correctly-rounded in both) ⇒ bit-equal by induction. Property-pinned on adversarial streams (mixed-sign pnl, ±0.0, huge/tiny magnitudes inside envelope). |
| 5 | `sorted(returns)` + percentile lerp (tail_ratio p95/p5) | returns finite in-envelope; NaN-free f64: CPython sample sort and Rust `sort_unstable_by(partial_cmp)` produce the same VALUE sequence (stability irrelevant for pure-f64 keys); reproduce `_percentile`'s index/lerp expression token-for-token. A NaN detected pre-sort (defensive; unreachable in-envelope) → `oracle_needed`. |
| 6 | downside dev `sqrt(Σ r²/n)` | sequential left-to-right fold in trade order (matches the genexpr); hardware sqrt correctly rounded both sides. |
| 7 | Drawdown + ulcer | REUSED `drawdown_stats_checked` — fold order untouched; scratch reuse changes allocation only. The per-bar sequential f64 fold stays per-bar (no run-collapsing: k×a ≠ a+a+…). |
| 8 | Round-half-even / Decimal | no Rust surface touches Decimal or money display; `round_money` lives only in the finalist projector (Python). int→f64 conversions round-half-even in both languages. |
| 9 | None vs NaN | `_safe_ratio` None ⇒ (value slot 0.0, present=false) column pairs; Python reconstructs `None` exactly. Burn-in/belt compare present-bits exactly and floats via `struct.pack('<d')` ↔ `f64::to_le_bytes` (signed zeros distinguished). |
| 10 | Signed zeros | zero-trade FLAT stats replicate `_empty_metrics` (+0.0 everywhere it has +0.0); running max via `>` on the non-negative dd domain (the #1186 argument); the bit-compare is the net. |
| 11 | Comparison boundaries | `hits >= min_hits`, is_win/is_loss classification, `end_equity < 1` ruin, `r < 0.0` downside filter — identical operators on bit-identical operands; each gets an AT-boundary two-arm test + a killed mutant. Gate verdicts (`passes_gates`) stay Python in Slice 1, so no gate boundary is ported at all until Slice 3. |
| 12 | FMA / reassociation | rustc stable emits no fast-math flags; `mul_add` is the only FMA door — grep-pin extended to `sweep_core/` (refuses `mul_add`, `fast-math`, `reassoc` tokens in crate + workflow files); FP folds written as scalar sequential loops (no reduction vectorisation possible without reassoc); committed release profile unchanged (`opt-level=3, lto=thin, cgu=1, panic=abort, overflow-checks=on`), NO `-C target-feature`/RUSTFLAGS additions; integer loops may auto-vectorise (bit-exact). Cross-arch honesty: identity is proven per-build by burn-in/belt/twin-run on the RUNNING host, not asserted per-platform. |
| 13 | `math.fsum` sidecar, span_years, audit `str(float)` | all stay Python; span_years passed IN precomputed; the core never re-derives time/calendar/rounding (§11 COMMON: `_price_to_paisa`, IST sessions, `session_days` all computed in the existing `_derive_kernel_inputs`-style Python pass and handed over as columns). |

**Enumeration/conflict/prefilter** are integer-exact — no float register needed;
proven by the parity property (Rust batches ≡ `itertools.islice` windows of
`iter_masks_k_of_bits` + `has_conflict` + `count_supermasks`, bit-for-bit,
including tallies) plus the twin-run.

---

## 5. (e) Safety pattern (ADR-0017 lineage, extended)

- **Flag**: `BRUTEX_SWEEP_RUST_CORE`, tristate via the imported
  `env_flag_tristate` (never copied); `SWEEP_RUST_CORE_DEFAULT = False`
  (first slice ships default-OFF; flip is a later one-constant ADR — the
  #1186→#1532 two-step). Unrecognised value → FAIL-SAFE OFF + warn-once.
  Resolved ONCE per tuple in `run_persisted_sweep` (documented, test-pinned
  divergence from rust_metrics' per-call read: engine choice never changes
  mid-tuple; parallel workers read their own env at tuple start). OFF arm =
  today's spine, module-for-module untouched (the existing goldens' monkeypatch
  seams — `sweep_run.ResidentTradeSim`, `fno_math.sweep_drawdown_stats` — keep
  working unmodified on that arm).
- **Burn-in (widened)**: first `SWEEP_CORE_SELF_CHECK_SAMPLES = 64` kept masks
  per process (widened from 16 — the certified surface is ~38 columns, Q3) run
  the FULL retained Python tail (`ResidentTradeSim.simulate…` +
  `metrics_from_trades` → `ResultMetrics`) over the same mask; every column
  bit-compared (ints `==`; floats `struct.pack('<d')`; present-bits exact).
- **Autorevert latch + tuple restart**: a mismatch latches the core OFF for the
  process (`sweep_core.kernel_autoreverted` ERROR, once, reason field) and —
  because the core owns whole columns, not one 4-tuple — the clean recovery
  unit is the TUPLE: `run_persisted_sweep` restarts the current tuple from
  enumeration on the Python spine (nothing durable was written; deterministic;
  oracle-authoritative results; bounded — at most one wasted partial tuple per
  process). Later tuples run Python (`kernel_disabled` short-circuit).
- **Permanent finalist belt (beyond burn-in — this design's addition):** for
  every ranked finalist (≤ top_k), before projection, recompute full metrics
  via the retained Python spine and bit-compare against the core's columns.
  Catches post-burn-in drift on exactly the rows that PERSIST, forever. Cost:
  O(top_k) Python re-evals per tuple (~8 ms at top_k=20; at the institutional
  top=1000 it is the same order as the already-existing O(top_k × n_bars)
  finalist projection re-simulation — honest note, and a `BRUTEX_SWEEP_RUST_CORE_BELT`
  escape hatch exists, default ON). Mismatch → same autorevert + tuple restart.
  Honest limit stated: NON-finalist wrongness past burn-in (rank-order inputs,
  sidecar sharpe sums) is caught only by the twin-run gate, the hypothesis
  cross-arm property, and — detectably — by the store/sidecar hash moving; the
  belt narrows the blind spot to "wrong but never-persisted-and-never-reranking"
  values, it does not eliminate it (Golden Rule #10).
- **Anti-vacuous engagement**: `sweep_core.engaged` INFO once per process; the
  ON-arm golden asserts the frozen `_GOLDEN_CONTENT_HASH` WITH exactly one
  engaged event and zero autorevert events; the inverted-teeth test poisons a
  domain-valid non-finalist sharpe column past burn-in (belt disabled via the
  test seam) and asserts the sidecar/store hash MOVES — proving the seam
  genuinely consumes the kernel. Test seams: `reset_core_self_check_for_tests`,
  `reset_core_observability_for_tests`, `set_core_belt_for_tests`, plus a
  forcing seam (`sweep_core_run.SweepCoreSessionCls` module attribute,
  late-bound) so 100 % branch coverage of every fallback is reachable.
- **Failure attribution preserved**: a `next_batch` ValueError → Python
  re-enumerates that batch's mask range (recorded as (start_index, count);
  re-derived bit-exactly via `itertools.islice(iter_masks_k_of_bits(…))`) and
  replays each mask through the Python `metrics_for_mask` oracle — first
  raiser gets the EXACT `_combo_error` fingerprint format; all-pass → the
  chunk-level SweepComboError; **oracle results never adopted** (the existing
  `_reattribute_chunk_failure` contract, re-pointed). `MemoryError` re-raises
  bare at every level (PyO3 extraction MemoryErrors propagate; a Rust-side
  alloc failure under panic=abort aborts — the SAME documented envelope as
  today's per-call drawdown vec, now smaller because buffers are reused).
  KeyboardInterrupt latency bounded ~1 s by `max_work_cells`.
- **Wheel freshness**: new symbols join `_EXPECTED_COMPILED_SYMBOLS` +
  `EXPECTED_BUILD.json` + `.pyi` + facade golden in ONE commit (the handshake
  tripwire); a stale wheel → ImportError at import, and the Python wrapper's
  `wheel_missing`-style fallback (warn-once, Python spine serves) keeps a bare
  checkout green.

---

## 6. (f) Phased delivery — each slice independently golden-proven

1. **Slice 0 — Rust groundwork (no Python behavior change).**
   `sweep_core/` enumeration + conflict + prefilter cursor with cargo tests +
   exact-value pins; `drawdown_stats_into` scratch refactor (existing
   pyfunction byte-identical — its goldens re-run unmodified); joins clippy/
   fmt/test CI + a new mutants shard. Not wired; wheel symbols added inert.
2. **Slice 1 — the core, default-OFF.** `metrics_port.rs` + `SweepCoreRun` +
   `SweepCoreBatch`; Python `brutex/engine/sweep_core_run.py` (consumption
   loop, burn-in, belt, autorevert, oracle splice, tallies) + the ONE gate in
   `run_persisted_sweep`; extracted pure cagr/skew/kurt helpers in
   `metrics/compute.py` (refactor-in-place, byte-identical). Ship gates: twin
   run (OFF vs ON, one commit, separate roots — store bytes + content hashes +
   sidecars + ranked order + audit-modulo-ts equal) on (a) the golden fixture,
   (b) a small real-data grid (both directions, ≥2 TFs, k∈{1,2,3}, with/without
   SL-TP, min_hits∈{0,30}), (c) a workers>1 parallel run (per-worker burn-in);
   frozen goldens byte-unmoved; ON-arm golden with engaged event; full §7 test
   plan. Default OFF = zero production risk.
3. **Slice 2 — default-ON flip.** Separate ADR + one constant, licensed by an
   M4-Pro multi-day soak twin-run (the #1532 pattern); runbook lever + the
   #1295 crash-loop-breaker playbook naming `BRUTEX_SWEEP_RUST_CORE=0`.
4. **Slice 3 — Rust gate/term feed (optional, measured).**
   `BRUTEX_SWEEP_RUST_CORE_TERMS` nested flag: Rust additionally returns
   (gate_pass bit, 6 of 7 term inputs, risk_efficiency) so Python builds
   ResultMetrics only for finalist CANDIDATES; Python `passes_gates`/
   `composite_term_inputs` become the burn-in/belt oracle for the ported copy
   (the #1186 oracle-checked-duplication pattern, explicitly ledgered as a
   COMMON exception). Only worth ~4–7 µs/kept — ships only if Slice-1
   measurement shows the Python consumption loop matters at production grids.
5. **Phase-next (separate DECs, named not promised):** hit-list-intersection
   prefilter (Q8 — the only lever against the 1m-TF O(n_bars) floor); finalist
   projection service from the core (Q6); buffer-protocol columns if
   million-kept research runs measure list-marshal pain (Q4).

---

## 7. (g) Test plan (incl. mutation)

- **Golden**: all frozen goldens byte-unmoved (`_GOLDEN_RUN_ID`,
  `_GOLDEN_CONTENT_HASH`, run-identity corpus, vocab 400-case fixture diff
  EMPTY, snapshot-hash pin); NEW ON-arm goldens: frozen hash reproduced with
  exactly one `sweep_core.engaged` + zero autorevert; SELL arm; batch-vs-plan
  arms; determinism (two ON runs byte-identical Parquet).
- **Twin-run**: the §6 Slice-1 gate (a)–(c); plus an idempotence twin (ON-arm
  re-run over its own outputs → sentinel short-circuit, files untouched).
- **Property (hypothesis, `HYPOTHESIS_PROFILE=ci`)**: (1) cross-arm FULL-column
  bit-equality (every ResultMetrics field via struct.pack, None-bits exact)
  over adversarial bars/trades: mixed-sign + zero pnl, ±0.0, dup timestamps,
  envelope-edge equities (2^52±1 → oracle-splice arm), zero-trade masks,
  min_hits boundaries; (2) enumeration parity: for random (bits, k,
  batch-shape) — Rust batches ≡ islice windows of the Python enumeration incl.
  conflict verdicts, tallies, and kept sets; (3) batch invariance: ≥5
  (max_kept, max_work_cells) shapes → identical SurvivorColumns bytes + ranked
  order; (4) statelessness: prefix-run ≡ prefix-of-full (extends the
  suffix-independence pattern to the core; no cross-run Rust state); (5) P-06 +
  suffix-independence suites pass UNMODIFIED (constraint 6).
- **Unit (100 % line+branch, `tests/unit/test_engine_sweep_core_run.py`
  stem-matched for the discovery gate)**: every fallback reason
  (wheel_missing, flag_unrecognised, envelope splice, latch short-circuit,
  belt mismatch → tuple restart, batch ValueError → re-attribution fingerprint
  format-pinned, MemoryError bare), warn-once discipline, tallies arithmetic,
  boundary two-arm tests (min_hits at/below, `//` negative operands, ruin
  boundary end_equity∈{0,1}, `_MIN_PROFIT_FACTOR` at-threshold via the Python
  gates), watermark breach mid-batch → `SweepMemoryBudgetError`.
- **Mutation**: cargo-mutants zero-survivor on `sweep_core/` (new shard;
  wrappers one-line delegates; `.max()`-over-`if` idioms; in-file exact-value
  pins per fold); Python-side mutation-killing tests by design on the new seam
  (exact messages, exact boundaries — the #1182 convention; sweep modules are
  not mutmut-gated, the goldens + anti-vacuous twins are the standing teeth).
- **Bench**: per-op O(1) via `_o1_tiers` (per-kept cost flat vs grid size;
  session build O(n_bars) stated); a conservative speedup floor on the 48k
  fixture (ON-arm per-kept marginal ≤ 1/3 of OFF-arm — far under the measured
  ~4–5×, flake-proof) + the anti-vacuous demoted twin (belt/latch forced →
  ratio collapses); OFF-arm cost gate (one env probe per tuple); existing
  exit-index/trade-sim benches untouched (OFF arm still exercises them).
- **Fuzz**: a `sweep_core` fuzz target (arbitrary columns/bits/config →
  must return Err or Ok, never panic) joins rust-fuzz-smoke.
- **CI**: coverage discovery gate satisfied (stem-matched tests); engine static
  gate (`--cov=brutex.engine` from `test_engine_*.py` alone) — forcing seams
  make both arms + all branches reachable; jscpd < 1 % (helpers imported, the
  loop not copy-pasted).

---

## 8. (h) Risk register — top 8

| # | Risk | Mitigation |
|---|---|---|
| 1 | Metric-port bit-divergence (fold order, percentile lerp, floor-div, ±0.0) | token-for-token port with per-op register (§4); 64-mask burn-in; PERMANENT finalist belt; dense cross-arm hypothesis property; twin-run ship gate; autorevert→tuple-restart makes any caught divergence results-invisible. |
| 2 | libm drift sneaking in via calmar/cagr | structurally impossible in Slice 1: cagr/calmar/mar/skew/kurt never computed in Rust; extracted Python helpers are the ONE source (refactor-in-place, existing tests). |
| 3 | Kernel panic ⇒ SIGABRT kills the worker (pytest uncatchable) | pure Result cores, boundary validation-first, checked arithmetic + the 2^52/i64::MAX-8 ceilings, envelope→oracle-splice (never a raise for domain inputs), fuzz target, clippy deny unwrap/expect/index; a real abort composes with #1291 quarantine + #1295 breaker (runbook lever). |
| 4 | COMMON drift — duplicated enumeration/conflict logic in Rust diverges from Python later (someone edits CONFLICT_PAIRS or the vocab) | pairs + bits are passed IN per call (Python single source); enumeration parity property runs on every CI; Slice-3's gate/term duplication is deferred + oracle-fenced + separately ledgered. |
| 5 | Marshal/consumption re-emerges as the bottleneck at million-kept research tuples (k≥5, min_hits=0) | measured lever ladder: bounded batches (memory-flat), Slice-3 thin feed (~2–3 µs/kept), Q4 buffer-protocol columns; the bounded-rank watermark still refuses un-plannable tuples gracefully. |
| 6 | Burn-in/belt blind spot: wrong non-finalist values past burn-in skew rank order / sidecar sums undetected in production | honestly stated (Golden Rule #10); nets: twin-run gate at ship, hypothesis property in CI forever, hash-moving detectability (teeth test), belt covers everything persisted; optional periodic re-burn-in per tuple listed as a review knob. |
| 7 | Golden/monkeypatch seam breakage (tests pin `sweep_run.ResidentTradeSim`, `fno_math.sweep_drawdown_stats`, event names, per-call env reads) | OFF arm is file-untouched for those seams; core gate resolved per-tuple ABOVE them; new ON goldens own new seams; the per-tuple (vs per-call) env read is documented + pinned as a deliberate, results-identical divergence. |
| 8 | Stale wheel / mixed-version deployment serves Python silently while claiming Rust (fake-performance illusion) | `_EXPECTED_COMPILED_SYMBOLS` handshake + `EXPECTED_BUILD.json` + doctor digest stage; `sweep_core.engaged` event asserted by the ON-arm golden (anti-vacuous); `rust_active`-style `sweep_run.core_path` per-tuple log with `core_active` field. |

(Also tracked: #1295/#1291 merge-shape drift — re-verify file-disjointness at
rebase, the integration adapter is one `if` in `run_persisted_sweep`; gap-row 3
cache-identity change lands in P2's zone and is orthogonal by construction.)

---

## 9. (i) Expected speedup — honest, with error bars

All sandbox ratios (shared x86 container); M4-Pro absolutes UNCLAIMED until the
bench-m4pro lane measures (DEC-BENCH-ABS-BASELINE-001). Per-operation O(1),
total O(N_masks × N_bars) — the constant changes, never the complexity.

- **Per KEPT mask @ 48k bars (sandbox, vs the measured 400 µs):**
  walk 62 µs (unchanged) + drawdown fold 25–60 µs (105 µs compute with the
  per-call alloc+scatter removed; buffer reuse measured-later, band stated) +
  Rust metric folds 3–8 µs + column marshal 1.5–2.5 µs + Python consumption
  6–10 µs (Slice 1) ⇒ **≈ 100–145 µs ⇒ 2.8–4.0×** per kept mask
  (Slice 3: ≈ 95–130 µs). Error bars: ±30 % on the new-Rust terms (unmeasured
  until built), ±10 % on the reused kernels (measured).
- **Per ENUMERATED (pruned/dropped) mask:** 2.4 µs → ~0.02–0.06 µs + the
  prefilter's O(n_bars) 0.26 ns/bar — at k=5/6 where pruned masks dominate,
  this is the headline win (≈ 40–100× on the pruned population).
- **Blended per-combo:** the 10–50 µs band is plausible for mid/high-TF tuples
  and k≥4 grids dominated by pruned masks. **1-minute-TF caveat (mandatory):**
  per-mask O(n_bars) ops cost ~143 µs (prefilter) / ~710 µs (walk, per kept)
  at the 555k-bar 1m window (sandbox) — the flat band CANNOT hold there; only
  the Q8 prefilter upgrade attacks that floor.
- **Grid wall-clock (PROJECTION, combo_counts.md caveats apply, parallel
  efficiency ~/6.5 not /8):** scenario C k≤6: 17.8 d → **≈ 1.5–4 d**;
  scenario B k≤6: 5.9 d → **≈ 0.5–1.5 d**; k≤5 grids → **≈ 1–5 h**. Error
  bars ±2× (grid definition unconfirmed — Q1; 1m-TF share dominates the
  spread; sandbox→M4 translation unverified).
- OFF-arm cost: one env probe + one `if` per tuple — bench-gated ≈ 0.

The §8 "2–4 days for ~33 B trials" SLA is NOT claimed met; the projection is
labelled PROJECTION until the M4-Pro soak measures it.
