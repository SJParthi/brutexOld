# DESIGN CANDIDATE — "Folds-out, verdicts-in-Python" Rust sweep-core

Lens: CORRECTNESS-FIRST. Author: independent designer (blind to other candidates).
Date 2026-07-08. Grounding: `baseline.md`, `SPEC_RUST_SWEEP_CORE.md`,
`combo_counts.md`, `reads/*.md`, repo read-only at `/home/user/bruteX`
(branch `claude/data-edge-guards`).

---

## 0. Design thesis (the proof-obligation inversion)

Start from what must be PROVEN, not from what must be FAST.

The per-kept-mask metric body (`_compute_metrics_impl`,
`brutex/metrics/compute.py:569`) is two structurally different computations:

1. **FOLDS** — O(trades)/O(n_bars) sequential accumulations: integer trade
   aggregates, the Welford/Pébay moments, the downside Σr² sum, the
   sorted-returns p95/p5, the consecutive-runs streaks, and the per-bar
   drawdown/ulcer scan. These carry ~100% of the per-mask COST and consist
   ONLY of `+ − × /`, `sqrt`-free f64 folds, integer sums, one stable sort,
   and one already-proven Rust kernel (`drawdown_stats_checked`).
2. **VERDICTS** — O(1) scalar combination: ~25 divisions, `_safe_ratio`
   None-decisions, comparisons at pinned boundaries (`end_equity < 1`,
   `r < 0.0`, `std_dev == 0.0`), the CAGR `growth ** (1/span)` pow, the
   skewness `math.pow(m2_n, 1.5)`, `math.sqrt` calls, min/max clamps, and the
   37-field `ResultMetrics` construction. These carry ~1–3 µs of cost and
   ~100% of the SEMANTIC divergence risk (None-vs-NaN, libm pow, signed
   zeros, gate boundaries, floor-division sign).

**The core bet: move ONLY the folds to Rust; every verdict, every Optional,
every libm call, every comparison stays in today's Python code — literally
the same function bodies, parameterised.** The Rust float-proof surface then
collapses to exactly NINE f64 columns per mask (`mean, m2, m3, m4,
sum_sq_downside, p95, p5, max_dd_pct, ulcer`) plus fifteen exact-integer
columns — each a strictly sequential fold with a mechanical token-for-token
porting rule and a `struct.pack` differential oracle. Everything the #1186
review classified MUST-STAY-PYTHON (pow/CAGR/skew) stays in Python **by
construction**, not by per-platform empirical luck. Per Golden Rule #10:
per-operation O(1), total O(N_masks × N_bars); this changes the CONSTANT.

Measured basis (`baseline.md` §3, 48k-bar sandbox): the design eliminates the
FFI trade marshal (~42 µs), the ClosedTrade/Python churn (~98 µs), the Python
seam (~32 µs), and moves the O(trades) share of the metric body (~50 of
55 µs) into the fold — while the ~3–6 µs verdict pass deliberately stays
Python. It separately kills the 2.4 µs/enumerated-mask Python
enumeration+conflict cost (the dominant term for k=5/6 grids: 5.5M–50M
enumerated per tuple).

---

## (a) Architecture + exact FFI surface

### a.1 Where the Rust code lives: `rust/fno-math/src/sweep_core.rs` (NEW module, existing crate)

Justification against the alternatives (hard constraint 2):

- `rust/trading-core` is FROZEN (DEC-SHARED-CORE-001, TickVault
  bit-identity, 400-case answer-key) — excluded by constraint.
- A NEW sibling crate (`rust/sweep-core`) was rejected because the core MUST
  consume `trade_sim.rs::simulate_one_core<W>`/`MaskWord` and
  `metrics.rs::drawdown_stats_checked` as **internal Rust functions** (that
  is the whole point — trades never cross the FFI). A sibling crate would
  (i) path-depend on fno-math, whose source-digest framing does NOT cover
  path-dep drift (`reads/rust.md` §4 note) — a stale-wheel blind spot on the
  exact code we depend on; (ii) compile a SECOND copy of the trade walker
  into a second `.so` (two walkers = a drift surface the goldens can't see
  per-crate); (iii) cost the facade-golden move, ~27 ci.yml install sites, a
  new mutants matrix, a new fuzz crate, and a new EXPECTED_BUILD handshake.
- fno-math is BruteX-only (TickVault couples to trading-core only), already
  hosts the sweep kernels, already carries the exact release profile
  (`panic="abort"`, `overflow-checks=true`, lto thin, cgu 1), the
  no-unwrap/no-expect lint header, the pure-`Result<_,String>`-core /
  thin-PyErr-wrapper convention (cargo-test links no libpython;
  cargo-mutants PyResult wrappers are UNVIABLE — logic must live in `_impl`
  cores), and the 6-shard mutants matrix ("+1 shard" is the sanctioned
  overrun dial).

Symbol additions (one commit updates `#[pymodule]`,
`_EXPECTED_COMPILED_SYMBOLS`, shim import list, `__all__`, `__init__.pyi`,
`EXPECTED_BUILD.json` — the pinned sextuple): `ResidentSweepCore`,
`ResidentSweepCoreU128`, `SWEEP_CORE_ABI_VERSION`.

### a.2 Where the Python code lives (ownership zones, constraint 5)

- NEW `brutex/engine/sweep_core.py` — the seam module (flag resolution,
  burn-in, autorevert latch, fold-block decode, fallback ladder,
  observability). Ours ("new modules").
- `brutex/engine/sweep_run.py` — mask-loop arm selection only. Ours.
- `brutex/engine/rust_trade_sim.py` — export the existing
  `_derive_kernel_inputs` for reuse (paisa/forced/session columns stay
  Python-single-source, §11 COMMON). Ours.
- `brutex/metrics/compute.py` — ONE pure-parameterisation refactor:
  `_compute_metrics_impl = _combine_metrics_from_folds(_fold_trades(...))`
  (see (b)/(d)). **ZONE FLAG:** compute.py is not in our declared zone, but
  it belongs to NO parallel phase's zone either (P3 owns
  results_store/ranking-view; P2 owns orchestrator seams; the memory file
  assigns metrics/ to nobody). Duplicating the metric body into a new engine
  module instead would violate Rule 11 COMMON and the jscpd ≤1% gate. This
  design REQUIRES the compute.py decomposition and registers it as an
  explicit coordinator sign-off item (risk R1) — precedent: #1186 made the
  same-file extraction (`compute_metrics_from_scalars`) for the same reason.
  Note compute.py is in the weekly mutmut 10-pair loop: the refactor ships
  with mutation-killing exact-value tests (zero-survivor bar).
- NO edits to: sweep_supervisor, sweep_parallel, sweep_orchestrator,
  checkpoint/sentinel/audit/store writers, ranking internals,
  trading-core, or any #1295/#1291 file (constraint 4/5).

### a.3 FFI surface (exact)

Two frozen pyclasses mirroring the existing u64/u128 dual-kernel precedent,
both thin wrappers over one width-generic `SweepCoreData<W: MaskWord>`
(reusing `SweepColumns` + `build_columns` validation verbatim, so the
`MAX_PRICE_PAISA = i64::MAX/8` envelope and the 7-column length checks are
the SAME code paths already mutation-gated):

```text
#[pyclass(frozen, module="fno_math")] ResidentSweepCore        (bar_bits: Vec<u64>)
#[pyclass(frozen, module="fno_math")] ResidentSweepCoreU128    (bar_bits: Vec<u128>)

__new__(open_paisa: Vec<i64>, high_paisa: Vec<i64>, low_paisa: Vec<i64>,
        close_paisa: Vec<i64>, is_forced: Vec<bool>, session_idx: Vec<i64>,
        bar_bits: Vec<u64|u128>) -> PyResult<Self>
    # identical validation to ResidentSweepSim::__new__ (lengths, price scan)

n_bars(&self) -> usize                       # only buffer view, as today

enumerate_prefilter(
    condition_bits: Vec<u32>,                # from cfg.condition_bits (Python-sorted-unique upstream; re-sorted+deduped in core, pinned equal)
    k: usize,
    conflict_pairs: Vec<(u32, u32)>,         # marshalled from vocabulary.conflicts.CONFLICT_PAIRS per call — Rust NEVER re-derives them (single source)
    min_hits: u64,
    start_rank: u64,                         # lexicographic combination rank (combinatorial number system unranking)
    max_enumerated: u64,                     # range bound — the GIL-slice / chunk-invariance knob
) -> PyResult<(u64, u64, Vec<u64|u128>)>
    # (enumerated_in_range, conflict_pruned_in_range, kept_masks_in_range)
    # kept = conflict-free AND (min_hits == 0 OR supermask_hits >= min_hits)
    # hits via the SAME (bar & mask) == mask predicate as count_one (integer-exact)

fold_block(
    masks: Vec<u64|u128>,                    # a chunk of kept masks, input order preserved
    is_buy: bool,
    sl_paisa: Option<i64>, tp_paisa: Option<i64>,
    sl_next_open: bool, tp_next_open: bool, tp_first: bool,   # FillPolicy triple, as simulate_masks today
    start_equity_paisa: i64,
) -> PyResult<(u32, Py<PyBytes>, Py<PyBytes>, Py<PyBytes>)>
    # (SWEEP_CORE_ABI_VERSION,
    #  ints_le  : row-major i64-LE,  stride = FOLD_INT_STRIDE  = 15,
    #  floats_le: row-major f64-LE,  stride = FOLD_FLOAT_STRIDE = 9,
    #  flags    : 1 byte per mask)
```

Columnar layout (the ONE layout law, mirrored as a Python tuple
`FOLD_INT_FIELDS` / `FOLD_FLOAT_FIELDS` in `sweep_core.py` and a Rust const,
pinned equal by a golden + the ABI version int):

```text
FOLD_INT_FIELDS (i64 ×15):  n_trades, net_pnl, gross_profit, gross_loss_abs,
    winners, losers, sum_mfe, sum_mae, sum_holding, max_holding,
    largest_loss (signed), max_consecutive_losses, max_consecutive_wins,
    max_dd_paisa, max_dd_bars
FOLD_FLOAT_FIELDS (f64 ×9): mean, m2, m3, m4, sum_sq_downside, p95, p5,
    max_dd_pct, ulcer
FLAGS (u8): bit0 = zero_trade (n_trades == 0; float row = zeros, unread)
            bit1 = python_required (equity envelope > 2^52 exceeded, an i64
                   sum overflow sentinel from the i128 accumulators, or a
                   non-finite return detected — Python full spine serves
                   this mask; float row unread)
```

Python decodes with `array('q').frombytes` / `array('d').frombytes`
(memcpy-speed; float bit patterns preserved exactly — an `array('d')` read
returns the identical IEEE double). PyBytes-return precedent: `blake3_hash`.
Per-mask marshal cost ≈ 0.1–0.7 µs (measured target; plain
`Vec<i64>`→list-of-int columns is the fallback encoding if decode measures
hot — both are chunk-invariant by construction).

Deliberately NOT in the FFI: `span_years`, `benchmark_return_pct`,
`tf_minutes`, timestamps, Decimal anything, run-identity fields, audit
anything, dedup anything. The kernel is identity-blind (identity fields stay
injected Python-side; `reads/identity.md` gotcha 1) and clock/calendar-blind
(paisa/forced/session columns computed by the existing Python
`_derive_kernel_inputs` — the kernel never re-derives rounding/IST math).

### a.4 The per-tuple call shape (digest path, flag ON)

```text
run_persisted_sweep (unchanged epilogue/prologue)
  sentinel resume | plan_rank_memory | SWEEP_START audit          [Python, unchanged]
  bar_bits (Stage-1A, kernel-cached)                              [unchanged]
  core = ResidentSweepCore(<derived columns>, bar_bits)           [once per tuple]
  loop: enumerate_prefilter(bits, k, pairs, min_hits, rank, N)    [ranged calls]
        → tallies += ; kept.extend(masks)
  for chunk in chunks(kept, CHUNK):                               [Python drives]
        blk = core.fold_block(chunk, ...)
        for mask, row in zip(chunk, blk):                         [input order]
            if row.python_required: metrics = <today's spine, lazily built sim>
            elif row.zero_trade:    metrics = _empty_metrics(FLAT_DRAWDOWN_STATS)
            else:                   metrics = combine_metrics_from_folds(row, ...)
            burn-in? also compute oracle via metrics_for_mask → bit-compare
            yield (mask, metrics)                                 [same generator contract]
  rank / persist / flush / sidecar / sentinel / audit             [Python, byte-unchanged]
```

The generator (`_iter_mask_metrics_core`) satisfies the SAME contract as
`_iter_mask_metrics` (input-order yield, `_combo_error` fingerprints,
MemoryError bare), so all three consumers — batch `pairs`, bounded
`SurvivorColumns.append` + exact `owned_nbytes()` watermark, and
`_rebuild_bounded_finalists` (which is injected the same core-backed
generator, keeping its positional mask-equality belt) — are untouched.

---

## (b) What moves to Rust vs stays Python — per item

| Item | Where | Why (proof-first justification) |
|---|---|---|
| Mask enumeration (`iter_masks_k_of_bits`) | **Rust** | pure integer, lexicographic combinations — exact order reproducible + property-comparable list-vs-list; kills 2.4 µs × C(60,k)/tuple |
| `has_conflict` (26 pairs) | **Rust** (pairs marshalled IN from `CONFLICT_PAIRS`) | `(mask & pair_mask) == pair_mask` — integer-exact; pairs stay single-sourced in Python |
| Stage-1B min-hits count | **Rust, fused** | same `count_one` predicate (integer); fusing avoids materialising 5.5M–40M survivor Python ints; kept set provably identical (exact counting) |
| Trade walk | **Rust (already)** — consumed internally via `simulate_one_core<W>` | proven bit-identical (#563/#1182, 45,863/45,863); trades stop crossing the FFI (−42 µs marshal, −98 µs churn) |
| Drawdown/ulcer scan | **Rust (already)** — `drawdown_stats_checked` called in-crate | proven inside the 2^52 envelope (#1186); fold order untouched; slice-3 buffer-reuse changes allocation only |
| Integer trade aggregates, streaks | **Rust fold** | exact i64 (i128 accumulate + overflow→`python_required` flag — Python bigint semantics preserved by fallback, never by emulation) |
| Welford/Pébay moments, downside Σr², returns conversion | **Rust fold** | sequential `+−×/` only; token-for-token port; IEEE-identical on identical order; differential-harness-provable |
| p95/p5 (sort + lerp) | **Rust fold** | Rust `sort_by(partial_cmp)` is STABLE like Python `sorted` → identical order incl. −0.0/+0.0 ties; lerp expression ported token-for-token; NaN structurally excluded (finite by envelope) + belt: non-finite → `python_required` |
| `total_return_pct`, CAGR pow, skewness pow, kurtosis, sqrt-based std/sortino, ALL `_safe_ratio` Nones, ALL gate-relevant boundaries, floor-divs (`expectancy = net_pnl // n`), clamps | **Python** (`combine_metrics_from_folds` — refactored from `_compute_metrics_impl`, same tokens) | the ENTIRE #1186 libm class + every None/boundary/sign hazard stays in today's literal code — bit-identity by construction, zero cross-language rounding argument needed |
| `ResultMetrics` construction | **Python** | downstream consumers (SurvivorColumns, rank, sidecar fsum, burn-in compare) consume today's objects unmodified |
| Ranking (`rank_combos`/`rank_survivor_columns`), gates | **Python** | bit-pinned shared internals; 1.3 µs/mask amortised — no win worth the proof |
| Finalist projection (`ranked_combo_to_result_row`) | **Python oracle** | explicit non-goal (#1182); Decimal/round_money store bytes |
| flush/sidecar/mark/sentinel/audit | **Python** | the resume/idempotence contract; ordering law untouched |
| Legacy no-digest dedup arm | **Python (today's loop, whole)** | per-mask eager run_id + probes; the core serves the DIGEST path only (`reads/dedup.md` gotcha 2) |
| `span_years`, paisa conversion, session/forced calendar, ts↔index | **Python** (computed once/tuple, passed in or kept out) | §11 COMMON single sources; kernel never re-derives |
| Zero-trade masks | **flag out, Python `_empty_metrics(FLAT_DRAWDOWN_STATS)`** | literal today's call — +0.0 signs by Python literal |

---

## (c) Batching strategy, batch boundaries vs workers / checkpoints / kill −9

**The durable unit stays the TUPLE. The FFI unit is a bounded in-memory
chunk strictly inside one tuple. The Rust core owns ZERO durable state — no
files, no statics, no caches, no cross-call cursors.**

- **Enumeration batching:** `enumerate_prefilter(start_rank, max_enumerated)`
  ranges, unranked via the combinatorial number system (exact, order-
  preserving, chunk-invariant — pinned across ≥5 range sizes). Range size is
  work-bounded: `max_enumerated ≈ clamp(2^28 / n_bars, 4096, 2^20)` so one
  FFI call holds the GIL ~0.3–1 s. This matters because the dashboard
  sweep-job runs in-process (a single monolithic call at a 555k-bar k=6
  tuple would hold the GIL for hours and freeze the event loop) and because
  chunked calls give the sequential path natural signal/beat gaps.
- **Metrics batching:** `fold_block` chunks of kept masks. Return is
  O(chunk) (24 scalars + 1 flag per mask), NOT O(trades), so the chunk can
  grow past today's 256: default `SWEEP_CORE_CHUNK = 4096` (bounded return
  ≈ 800 KB), still work-capped by `masks × n_bars ≤ 2^30` cells. Chunk size
  provably result-invariant (per-mask folds are independent; pinned by the
  chunk-invariance suite, the #1182 pattern).
- **Worker partitioning (post-#1291/#1295 shape):** unchanged — one tuple
  per worker process; the core is constructed inside `run_persisted_sweep`
  in whatever process runs the tuple. Masks are NEVER batched across tuples
  (that would cross the checkpoint/audit/sentinel unit — `reads/parallel.md`
  §8). Under `sweep_parallel`, `RAYON_NUM_THREADS=1` serialises any rayon
  dispatch; all new FP folds are scalar-sequential regardless of thread
  count, and any rayon use is restricted to order-preserving
  `IndexedParallelIterator` over INDEPENDENT masks (bit-identical to serial
  by construction — the existing `par_bridge`-forbidden law).
- **kill −9 semantics (batch fully-durable-or-fully-redone):** at ANY kill
  point — before/inside/after any Rust call, worker or parent — the tuple's
  completion sentinel is absent/unpaired ⇒ FULL deterministic tuple redo
  with byte-identical artifacts (DEC-MARK-MANY-SENTINEL-001). There is no
  partial-batch durable state to reason about: the "batch" is RAM. A killed
  worker → BrokenProcessPool → #1291 quarantine/stall semantics unchanged
  (the parent watchdog is cross-process; our chunking only shortens the
  in-tuple no-signal windows, never lengthens them). A killed parent loses
  only unapplied outcomes — contiguous-clean-prefix checkpoint + sentinels
  re-derive on resume, exactly as today.
- **Kernel panic** (`panic="abort"`): SIGABRT = worker death → pool-broken
  quarantine → repeated deaths trip #1295's crash-loop breaker; the breaker
  runbook names `BRUTEX_SWEEP_RUST_CORE=0` as lever #1 (runbook addition;
  no auto-env-flip — mirrors SPEC §7 / open-questions Q5 stance). Structural
  prevention: every FFI input validated at the boundary by pure `Result`
  cores; no unwrap/expect/index/`as`-narrowing in method bodies; i128
  accumulators + explicit envelope flags make hot loops provably
  panic-free; fuzz targets added for both entry points.
- **Gap-register interactions (rows #3/#4):** row #3 (tuple-cache identity
  gains overfit/stage3 keys — P2's `_tuple_cache_params` fix) is orthogonal:
  the core adds NO cache keys and its engagement is deliberately NOT part of
  any identity (engine-blind law, sweep_run.py:169). Row #4 (`sweep_runs/`
  sibling root, run_layout) is orthogonal: the core writes no files. Both
  verified to touch zero mask-loop files; composition re-checked at each
  slice's merge-sync round.

---

## (d) Bit-identity strategy per float operation

The governing rule: **a float operation may move to Rust ONLY if it is a
correctly-rounded IEEE-754 primitive (`+ − × / sqrt` — identical bits given
identical operands and order) applied in an order the port reproduces
token-for-token. Everything else (libm pow, semantic None, floor-div sign,
Decimal) stays in Python.** Register:

| # | Operation | Resolution |
|---|---|---|
| 1 | Integer aggregates, `//` floor-divs, `expectancy`, `avg_win/loss` | **Stay Python** (combine step) — computed from Rust-returned raw i64 sums. No Rust floor-div-vs-truncation hazard exists anywhere in this design. Rust sums use i128 accumulators; a sum that cannot narrow to i64 sets `python_required` (Python bigint path serves — behaviour, not emulation). |
| 2 | `total_return_pct = 100.0 * net_pnl / start_equity` | **Stays Python** (combine) — needs only `net_pnl` (exact int). |
| 3 | CAGR `growth ** (1.0/span_years)` + OverflowError→inf clamp; `_skewness` `math.pow(m2_n, 1.5)` | **Stay Python** (combine) — the #1186 MUST-STAY-PYTHON class, honored by construction. Rust returns `net_pnl` (→ `end_equity`) and raw `m2, m3`; Python runs the EXISTING `_skewness`/CAGR code including the `end_equity < 1` ruin boundary, the try/except OverflowError, `_CAGR_CAP_PCT` min, and the −100 clamp — all literal today's tokens. `calmar`/`mar` (consume cagr) follow automatically. |
| 4 | Welford/Pébay moments | **Rust fold**, exact update expressions in exact operand order, strictly sequential over trades in trade order; `(n*n − 3*n + 3)` computed in integer then converted (exact for all reachable n); returns raw `(mean, m2, m3, m4)`. Python combine constructs `Moments(...)` and uses the EXISTING `.variance`/`.std_dev`/`_skewness`/`_excess_kurtosis` — shared code, not a port. Differential harness: ≥10⁶ adversarial trade streams (mixed magnitudes, ±0 pnl, near-envelope), `struct.pack('<d')` compare per moment. |
| 5 | `returns[i] = pnl/start` conversion | **Rust** `(pnl as f64) / (start as f64)`. Within the 2^52 envelope both conversions are exact, so Rust's correctly-rounded f64 division equals CPython's correctly-rounded int/int true division bit-for-bit. The envelope is CHECKED IN THE FOLD (start + Σ\|pnl\| via i128) — outside ⇒ `python_required` flag ⇒ the mask runs today's spine (same demotion today's `exceeds_exact_equity_envelope` performs). Belt: any non-finite return ⇒ `python_required`. |
| 6 | `downside_dev` Σr² | **Rust fold** (sequential, trade order — matches the genexpr); the `r < 0.0` filter boundary reproduced with the identical operator on the identical bit value; final `sqrt(sum/n)` **stays Python** (combine) via `math.sqrt` — sqrt is correctly-rounded in both, but keeping it in combine means the Rust surface is a bare sum. |
| 7 | Drawdown family + ulcer | **Reuse `drawdown_stats_checked` UNCHANGED in-crate** (2^52 pre-guard now enforced by the fold's own envelope flag before the call). The per-BAR sequential `sum_sq_pct` fold stays per-bar — equal-value runs are NEVER collapsed into multiplication (k×a ≠ a+a+…+a). Slice-3 buffer reuse resets only touched indices (O(trades) zeroing) — allocation change only, fold order byte-identical, property-pinned. |
| 8 | Round-half-even / Decimal | int→f64 conversions round-half-even in both languages; `round_money` appears ONLY in the finalist projector (Python, untouched). Review assertion: the core never re-derives a money display value. |
| 9 | None vs NaN | Rust NEVER encodes a None: every Optional metric is a **Python verdict** in combine (`_safe_ratio` literal). The fold columns are total functions (always-present raw values) except behind the two flags. Burn-in compares None-bits exactly. |
| 10 | Signed zeros | Zero-trade rows come from the literal Python `_empty_metrics` (+0.0 literals). Fold sums can produce −0.0 only through f64 addition of signed zeros — identical sequences produce identical signs in both languages; the burn-in `struct.pack` compare (which distinguishes −0.0/+0.0) is the net, exactly as #1531. |
| 11 | Sort stability | Python `sorted` is stable; Rust `slice::sort_by` is stable. `partial_cmp` on finite floats ranks −0.0 == +0.0 as Equal in both ⇒ tie order = input order in both ⇒ percentile reads identical VALUES (incl. zero signs at lerp endpoints). NaN structurally excluded (finite-by-envelope + `python_required` belt); the comparator helper returns Ordering with a validated precondition — no unwrap. Adversarial −0.0-tie property test pins it. |
| 12 | Percentile lerp | ported token-for-token (`rank = pct/100*(n−1)`; floor/ceil; `lo*(1−f) + hi*f`) — `+−×/` only; f64 floor/ceil are exact; property-pinned vs `percentile()`. |
| 13 | Strict `>`/`>=`/`<` boundaries (`hits >= min_hits`, `end_equity < 1`, `r < 0.0`, `std == 0.0`, gates) | `min_hits` compare is integer in Rust (exact); every FLOAT boundary is Python (combine/gates). Each boundary gets an exact-AT-threshold dual-arm test + a mutation-killing test (#1182 (e) convention). |
| 14 | FMA / auto-vectorization | rustc stable emits no `contract`/`reassoc` fast-math flags; `mul_add` is the only FMA door and is SOURCE-PINNED ABSENT — the existing #1186 grep pin extends to `sweep_core.rs`; CI grep-pin refuses `mul_add`, `fast-math`, `ffast-math`, `reassoc`, and any new `-C target-feature`/RUSTFLAGS in the crate + workflows; the committed release profile is unchanged. FP folds are scalar sequential loops (no reassoc flag ⇒ LLVM cannot vectorise the reductions); INTEGER loop auto-vectorisation is allowed (bit-exact). |
| 15 | `math.fsum` sidecar, `span_years` expression | Python, untouched (sidecar folds the Python-side sharpe column; span_years computed once per tuple in `sweep_driver`-equivalent code and consumed only by Python combine — it never crosses the FFI). |
| 16 | Enumeration/conflict/hits | pure integers — no float surface at all. |

**Why this is mechanically provable:** the Rust float outputs are 9 columns,
each a sequential fold of correctly-rounded primitives over inputs that are
themselves exact integer conversions. "Identical operand sequence ⇒
identical bits" is a theorem for `+−×/` under IEEE-754 round-to-nearest-even
in both languages; the ONLY proof obligations are (i) the operand order is
identical (token-for-token port + code review + differential harness) and
(ii) no compiler transformation reorders it (#14 mechanism). No empirical
per-platform libm equality claim is needed anywhere.

---

## (e) Safety pattern (ADR-0017 / #1531 lineage, extended)

- **Kill switches:** `BRUTEX_SWEEP_RUST_ENUM` (slice 1) and
  `BRUTEX_SWEEP_RUST_CORE` (slice 2), both via the imported
  `env_flag_tristate` vocabulary (never copied); unrecognised value ⇒
  FAIL-SAFE OFF + warn-once (`flag_unrecognised`). Read ONCE per
  `run_persisted_sweep` invocation (the flag selects the tuple's loop
  shape — a structural decision; parallel workers read their own env). The
  documented delta vs rust_metrics' per-call read is stated in the module
  docstring (results identical either way). Both default OFF; each flips
  default-ON in its own later ADR (the #1186→#1532 two-step).
- **Engagement condition:** digest path only (`bypass_dedup` True) AND flag
  ON AND wheel symbols present AND bar_bits/masks within kernel width
  (u64 → `ResidentSweepCore`, ≤u128 → lazy `ResidentSweepCoreU128`
  promotion, beyond → today's loop). Every fallback reason lands on the
  bit-identical Python spine, warned once per (tuple, reason):
  `wheel_missing`, `wheel_stale`, `flag_unrecognised`, `kernel_error`
  (narrow `(ValueError, TypeError, OverflowError)`), `kernel_disabled`,
  `self_check_mismatch`, `abi_version_mismatch`, `legacy_dedup_path`.
  MemoryError re-raises BARE everywhere.
- **Burn-in (per process):**
  - *Metrics core:* the first `SWEEP_CORE_SELF_CHECK = 16` core-served
    masks ALSO run the fully independent pure-Python oracle
    `metrics_for_mask` (Python walker + Python equity/metrics tail — zero
    fno_math involvement, the strongest available twin) and EVERY
    `ResultMetrics` field is bit-compared (ints exact; floats via
    `struct.pack('<d')`; None-bits exact). Mismatch ⇒ the ORACLE metrics
    ship for that mask, `sweep_rust_core.kernel_autoreverted` ERROR (once),
    process latch, and the REST of the tuple continues seamlessly on
    today's spine from the next mask (lazily constructing
    `ResidentTradeSim`) — order and bytes preserved because both engines
    are bit-identical and yield order is positional.
  - *Enumeration:* the first `SWEEP_ENUM_SELF_CHECK = 2` core-enumerated
    tuples per process are fully shadowed by today's Python
    enumerate/conflict/prefilter loop; `(enumerated, conflict_pruned,
    kept_list)` compared exactly; mismatch ⇒ ship the PYTHON lists + latch.
    Cost honestly stated: ≤ 2× the Python enumeration cost of two tuples,
    once per process.
  - Latches/counters are single-cell process-globals with
    `reset_self_check_for_tests(remaining=)` /
    `reset_observability_memo_for_tests()` twins (test seams the goldens
    require). Under `sweep_parallel`, each spawned worker gets a fresh
    burn-in budget + latch — by design (mirrors #1531).
- **Anti-vacuous goldens:** the ON arms reproduce `_GOLDEN_RUN_ID` /
  `_GOLDEN_CONTENT_HASH` byte-for-byte WITH exactly one
  `sweep_rust_core.engaged` (resp. `sweep_rust_enum.engaged`) event and
  zero autorevert events; a domain-VALID poisoned fold (test seam
  monkeypatching the module-level `ResidentSweepCore` name in
  `sweep_core.py` — kept late-bound precisely for this) past burn-in MOVES
  the hash (proves the seam genuinely consumes the kernel); the same poison
  DURING burn-in autoreverted ⇒ pinned bytes + one `kernel_autoreverted`
  with `reason == "self_check_mismatch"`. A domain-INVALID block (bad ABI
  version, wrong stride, non-finite where a flag was required) ⇒ one
  `kernel_result_refused` + pinned bytes.
- **Honest limit (stated up-front, as #1531 states it):** burn-in catches
  only wrongness manifesting in its window; beyond it, the twin-run grid
  gate, the cross-arm property suite, ResultsStore's same-run_id-different-
  fields conflict tripwire, and the #1295 R45 diffcheck layer are the net.
- **Observability:** one `sweep_run.sweep_core_path` INFO per tuple
  (`core_active`, n_bars, kept, chunks) alongside the existing pinned
  events, which keep firing on their arms (`trade_sim_path` etc. unchanged
  on the OFF/legacy arms). Zero new audit lines; engine choice never enters
  audit payloads or identity (engine-blind law).

---

## (f) Phased delivery — each slice independently golden-proven

- **Slice 0 — compute.py decomposition (Python-only, no flag).**
  `_fold_trades(trades, start_equity) -> TradeFolds` +
  `combine_metrics_from_folds(folds, *, total_bars, benchmark_return_pct,
  start_equity_paisa, span_years, drawdown) -> ResultMetrics`;
  `_compute_metrics_impl` becomes `combine(fold(...))` — a pure
  parameterisation (the #1186 extraction precedent). Gates: all goldens
  UNMODIFIED, 100% coverage, mutmut zero-survivor on compute.py, exact-value
  pins for every moved expression. Coordinator sign-off for the
  out-of-zone file recorded in the PR.
- **Slice 1 — Rust enumeration+prefilter (integer-only), default-OFF
  `BRUTEX_SWEEP_RUST_ENUM`.** `enumerate_prefilter` + unranking +
  `sweep_core.py` seam + enum burn-in + chunk/range invariance pins +
  hypothesis list-equality property (random bit sets × k × synthetic
  conflict pairs × min_hits, Rust list == Python list exactly) + twin-run
  byte-identity on the golden fixture grid AND a small real-data grid.
  Ships the biggest k=5/6 win with a proof that is pure integer.
- **Slice 2 — fold_block metrics core, default-OFF
  `BRUTEX_SWEEP_RUST_CORE`.** The fold kernel (consuming
  `simulate_one_core` + `drawdown_stats_checked` internally), the columnar
  ABI, combine wiring, 16-mask burn-in + autorevert, poison twins,
  cross-arm full-column hypothesis property, twin-run gates incl.
  `sweep_parallel` workers>1 (per-worker burn-in), forced-demotion bench
  twin. Orthogonal to slice 1 (either flag alone is a valid, proven
  configuration — 4 arm combinations golden-pinned).
- **Slice 3 — measured constant work, no semantic change.** Drawdown
  scratch-buffer reuse (touched-index reset, equality-pinned), fused
  single-pass walk+drawdown (only if the per-bar fold order is provably
  byte-identical — else dropped), chunk-size tuning, PyBytes-vs-list
  encoding choice. Each lever ships only with its equality property + a
  measured ratio.
- **Slice 4 — default-ON flips.** One ADR per flag after a soak window
  (grid-scale twin runs on real data + the weekly mutants lane green),
  mirroring DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001.
- **Named non-goals recorded for the queue (not built):** hit-list
  intersection prefilter (integer-exact, kept-set-identical, the honest
  lever for the 1m-TF wall), streaming kept-mask iterator (memory), Rust
  ranking, finalist projection.

---

## (g) Test plan (incl. mutation)

1. **Goldens:** `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` byte-unchanged on
   ALL arms (OFF/enum/core/both; wired + forced-fallback; SELL twin);
   engaged-event + zero-autorevert assertions; poison-during/after-burn-in
   twins (hash pinned / hash MOVES); `tests/golden/fixtures/` diff EMPTY;
   determinism (two runs → byte-identical Parquet); layout golden pinning
   `FOLD_*_FIELDS` == Rust consts == ABI version.
2. **Twin-run gates (ship condition):** OFF vs ON store bytes +
   content_hash + trial-stats sidecar + ranked order equal on (a) the
   golden grid, (b) a real-data grid (≥2 TFs, both directions, k∈{1,2,3},
   with/without SL/TP, zero-trade-heavy tuples included), (c) a
   `sweep_parallel` workers>1 run, all within one build/commit into
   separate results roots (run-identity register per SPEC §3.1 —
   `code_commit_sha` held constant by construction).
3. **Property (hypothesis, `HYPOTHESIS_PROFILE=ci`):** cross-arm FULL
   `ResultMetrics` bit-equality (struct.pack keys) over adversarial
   bar/mask streams (random walks, ±0 pnl trades, all-win/all-loss,
   near-envelope magnitudes, −0.0 return ties, duplicate-timestamp
   windows); enumeration list-equality; chunk/range invariance ≥5 sizes;
   suffix-independence + P-06 suites pass UNMODIFIED (plus the full-spine
   suffix property extended through the core arm — statelessness);
   `python_required`/`zero_trade` flag routing properties (flagged masks
   byte-equal to today's spine).
4. **Unit (100% line+branch, `tests/unit/test_engine_sweep_core.py`
   stem — discovery gate):** every fallback reason branch; exact boundary
   duals (`hits == min_hits`, `end_equity ∈ {0,1}`, `std == 0.0`,
   `r == −0.0`); error-fingerprint format pins; re-attribution
   (oracle replay, never adopted); MemoryError-bare; watermark breach
   mid-core-loop; latch continuation mid-tuple ordering; ABI mismatch
   refusal; decode of hand-crafted byte blocks.
5. **Rust:** `cargo test` in-file exact-value pins (hand-derived Welford/
   percentile/streak vectors; `.max()`-style tie-mutant-proof idioms);
   proptest enumeration vs a naive combinations reference + unranking
   round-trip; fold vs a deliberately different-shaped reference fold
   (proptest within-crate) for structure bugs; fuzz targets for both entry
   points (rust-fuzz-smoke lane); clippy -D warnings; fmt.
6. **Mutation:** cargo-mutants zero-survivor on `sweep_core.rs` joining the
   fno-math shard matrix (+1 shard if the measured budget needs it — the
   ledgered dial); wrappers stay one-line delegates (UNVIABLE class);
   compute.py refactor keeps its weekly mutmut pair zero-survivor;
   mutation-killing tests by design on every Python boundary (exact
   messages, exact thresholds — the #1182 convention).
7. **Bench:** per-op O(1) via `_o1_tiers.ratio_ceiling`/`cache_tier_ceiling`
   (per-mask cost flat vs grid size; per-(mask·bar) flat); speedup floor
   FAR under the measured ratio with the forced-demotion anti-vacuous twin
   (#1531 pattern); OFF-arm cost gate (identity + one env probe); audit
   O(1) (exactly 3 lines/tuple both arms); marshal-decode microbench.
8. **Static:** ruff/ruff-format; `mypy --strict`; pyright baseline; jscpd
   ≤1% (the combine refactor SHARES code rather than copying — designed
   for this gate); citation/DEC gates; `.pyi` + EXPECTED_BUILD sextuple.

---

## (h) Risk register (top 8 + mitigations)

| # | Risk | Mitigation |
|---|---|---|
| R1 | **compute.py zone/coordination** — the fold/combine decomposition sits outside our declared zone; parallel-phase churn could collide | Slice-0 lands first, tiny and pure-parameterisation; coordinator sign-off recorded; file is in no other phase's zone (verified against the contracts memory); mutmut + goldens pin byte-neutrality; if refused, fallback = keep slice 1 only (still the k≥5 win) and re-scope slice 2 |
| R2 | **Float divergence in the ported folds** (Welford order, percentile lerp, −0.0 accumulation) | ONLY 9 f64 columns exist; token-for-token port + review checklist per expression; ≥10⁶-case differential harness; 16-mask burn-in per process; autorevert ships oracle values; twin-run gate before any flip |
| R3 | **Sort-stability / signed-zero tie divergence** in p95/p5 | stable `sort_by` == stable `sorted`; −0.0 ties preserve input order in both; adversarial −0.0 property; NaN structurally excluded + `python_required` belt |
| R4 | **Panic/SIGABRT** (`panic="abort"`) from unvalidated input, overflow, or alloc | boundary validation in pure Result cores; i128 accumulators + envelope flags (no unchecked hot-loop arithmetic); no unwrap/expect/index lints; fuzz; bounded allocations (chunk-capped); crash-loop-breaker runbook names the kill switch |
| R5 | **Silent vacuous parity** (stale wheel / mis-wired arm serving Python while claiming Rust) | `_EXPECTED_COMPILED_SYMBOLS` handshake + `SWEEP_CORE_ABI_VERSION` in-band per block + engaged-event assertions + the hash-MOVES poison twin (the #1186 inverted teeth) |
| R6 | **Memory at k=6** — kept-mask list ~O(10⁷) Python ints per tuple | identical to today's shape (survivors list already exists); Rust side is transient per range; streaming kept iterator named as a later measured lever; `plan_rank_memory` + bounded watermark untouched |
| R7 | **Composition drift vs #1295/#1291 + gap-register rows 3/4** | zero shared files (verified both PRs touch no mask-loop files); core adds no cache keys, no run artifacts, no audit actions; re-verify at every slice's main-sync round; tallies feed P2's manifest counters unchanged |
| R8 | **Burn-in blind spot / GIL-hold stalls** — wrongness after mask 16; long FFI calls starving the in-process dashboard job or the #1291 stall clock | honest-limit disclosure + the layered net (twin-run, property, conflict tripwire, R45 diffcheck); ALL FFI calls work-bounded (~≤1 s) so GIL gaps and liveness windows are no worse than today's 256-mask chunks; stall semantics unchanged (tuple-granular, and tuples get FASTER) |

Secondary (tracked, not top-8): warn-once flood discipline under chunked
loops (per-(tuple, reason) memo); `maybe_guard_bars` ON-mode teeth reduced to
the first sequential pass for core-native reads (same disclosure
ResidentTradeSim already carries — documented, not silent); enum burn-in
cost at k=6 (bounded to 2 tuples/process, stated).

---

## (i) Expected speedup — honest, with error bars

All sandbox rates from `baseline.md` §2 (48k-bar window unless stated);
M4-Pro absolutes NOT claimed until the bench-m4pro lane measures them
(sandbox-ratio convention). Parallel scaling quoted at **~6.5×, not 8×**.

Per-KEPT-mask marginal at 48k bars: today ≈ 400 µs →
walk 62 µs (unchanged) + drawdown scan ~100–130 µs (→ ~75–105 µs with
slice-3 buffer reuse) + Rust O(trades) folds ~1–3 µs + marshal/decode
~0.1–0.7 µs + Python combine+append ~3–6 µs ≈ **145–200 µs ⇒ 2.0–2.7×**
(error bar: ±30% on the fold/deco constants until measured; the walk+scan
floor is hard). Per-ENUMERATED non-kept mask: 2.4 µs → **~0.03–0.15 µs
(≈20–80×)**, plus the unavoidable per-survivor prefilter O(n_bars) count
(0.257 ns/bar·mask) exactly as today.

Blended per-combo: for mid/high TFs (n_bars ≤ ~120k) with prefilter-
dominated k=5/6 populations, the blended cost lands in the **10–50 µs/combo
band** — CONDITIONAL on kept-fraction staying small (unmeasured for real
data at k≥4; flagged). **The 1-minute-TF caveat is mandatory wherever that
band is quoted:** at ~555k bars even the prefilter costs ~143 µs per
survivor and the walk ~710 µs per kept mask (sandbox) — no flat 10–50 µs is
possible there; Rust cannot remove O(N_bars) (the hit-list-intersection
prefilter is the named, out-of-scope lever). Grid wall-clock (scenario B,
k≤5, 8 workers at ~6.5× effective): today ~15.2 h → **projected ~2–5 h**;
k≤6/B: ~5.9 d → **~0.7–2 d** — PROJECTIONS, weakest at the 1m rows, to be
re-stated from twin-run measurements before any SLA claim. The §8 "2–4 days
for 33B trials" SLA is NOT claimed met by this change.

---

## Appendix — invariants explicitly preserved (checklist for review)

Tallies exact (`enumerated`, `conflict_pruned`, `skipped_dedup == 0`,
`evaluated`, `minhits_dropped`, `backtested`) · yield order = enumeration
order · 3 audit lines/tuple, engine-blind payloads · lazy run_id
(finalists/errors only), `mask:%032x` · flush→sidecar→(no marks)→sentinel
STRICTLY LAST · `_combo_error` fingerprint format · oracle re-attribution
never adopted · MemoryError bare · `SweepMemoryBudgetError` watermark
per-append · bounded ≡ batch rank bit-for-bit · duplicate-mask rank raise
unreachable (enumeration emits no dup) · `DigestPathNoDedup`/`_NoDedup`
never consulted · legacy arm untouched Python · exit-index/`
metrics_from_trades` seams + #1531 burn-in intact on their arms ·
`maybe_guard_bars` OFF identity · Stage-1A untouched · trading-core
untouched · P-06 + suffix-independence suites UNMODIFIED · no
`_indicator_version` change (fno-math version not in the string; core
engagement never re-keys).
