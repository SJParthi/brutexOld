# DESIGN — Rust sweep-core, RISK-MIN candidate ("fused scalar tail")

Lens: smallest-diff incremental path. Optimize shipping certainty + reviewability
over peak speedup. Date 2026-07-08. Grounding: `baseline.md`, `SPEC_RUST_SWEEP_CORE.md`,
`combo_counts.md`, `reads/*.md`, repo read-only at 46895ee5-era working tree.

---

## 0. The core bet, in one paragraph

Do NOT move the per-mask orchestration loop into Rust. Instead, **fuse the three
dominant measured per-kept-mask costs — ClosedTrade churn (98 µs), drawdown FFI +
alloc (130 µs), and the O(trades) half of the metric body (55 µs) — into ONE new
batched pymethod on the EXISTING `ResidentSweepSim`/`ResidentSweepSimU128`
pyclasses** (`simulate_masks_metric_scalars`), which runs the proven
`simulate_one_core` walk and the proven `drawdown_stats_checked` fold entirely
inside Rust and returns **one flat ~25-scalar tuple per mask** (sums, counts,
moments, percentiles, drawdown 4-family) instead of per-trade Python objects.
Python keeps EVERYTHING derived: every division, every `_safe_ratio` None, every
gate boundary, every libm-touched field (CAGR `pow`, skew `pow`, all `sqrt`s
except the already-proven Rust ulcer), the 40-field `ResultMetrics` construction,
ranking, persistence, audit, sentinel. The Python loop shape in
`sweep_run.py::_iter_mask_metrics` is unchanged — only the per-chunk *call* and
the per-mask *tail* swap, behind a default-OFF flag with the full ADR-0017 safety
kit. Bit-identity is achieved mostly **by construction** (shared Python tail, no
new float semantics for anything divided, rooted, or powered), leaving exactly
four small, exactly-reproducible float folds as the new cross-language surface.

Measured basis (baseline.md §3, 48k-bar sandbox): this removes ~202 µs of the
~230 µs Python+FFI residual per kept mask (98 churn + 55 metric body + 17 marshal
+ ~32 seam) and cuts the drawdown constant (buffer reuse + no exits marshal),
i.e. ~2.2–2.7× per kept mask at 48k bars, rising at production windows where the
Python share is larger. A second, independent slice (enumeration+prefilter fusion)
addresses the 2.4 µs/enumerated-mask term that dominates k≥5 grids.

---

## (a) Architecture + exact FFI surface

### Where the Rust goes: `rust/fno-math/src/sweep_core.rs` (NEW module, existing crate)

Justification (risk-min, vs. a sibling crate):
- The two kernels being reused — `trade_sim.rs::simulate_one_core<W>` and
  `metrics.rs::drawdown_stats_checked` — live in fno-math. In-crate reuse is a
  plain function call; a sibling crate would need an rlib dependency on fno-math,
  a second wheel, ~27 ci.yml install-site additions, a facade-golden move, a new
  EXPECTED_BUILD/doctor/digest lane, a new mutants config + shard matrix, and a
  branch-protection job change (reads/rust.md §6). All of that is diff, none of
  it is speed.
- fno-math is BruteX-only (TickVault couples to trading-core ONLY) — hard
  constraint 2 is satisfied trivially; `rust/trading-core` is untouched, the
  400-case golden fixture byte-untouched.
- New pymethods on existing pyclasses do NOT change the module symbol set, so no
  `_EXPECTED_COMPILED_SYMBOLS` churn; wheel-staleness for the NEW METHOD is
  covered by a Python-side `getattr` probe with a warn-once
  `wheel_missing_fused` fallback (the existing envelope-guard law,
  rust_trade_sim.py idiom).
- fno-math's crate version is NOT in `_indicator_version()` → no run re-key
  (reads/rust.md §4). Bump 0.3.0→0.3.1 anyway for doctor freshness diagnostics.

Module layout:

```
rust/fno-math/src/sweep_core.rs      # pure Result<_,String> cores + folds
rust/fno-math/src/trade_sim.rs       # +2 thin pymethods (u64 + u128 pyclasses)
rust/fno-math/src/metrics.rs         # drawdown_stats_into(scratch, ...) seam
                                     # (existing fn becomes a thin allocating wrapper)
```

`drawdown_stats_into` is the ONLY touch to a proven kernel: refactor
`drawdown_stats_checked` to take a caller-provided `&mut Vec<i64>` scratch
buffer; the existing public function delegates by allocating (its tests, goldens
and 358 mutants pass UNMODIFIED — the fold body does not move). The fused batch
call reuses one scratch buffer across the whole chunk (kills the per-mask
`vec![0; n_bars]` alloc measured inside the 130 µs). Buffer lives on the stack of
the batch call, not in the frozen pyclass — no interior mutability needed.

### Exact FFI surface (one new method, both width lanes via `MaskWord`)

```rust
// pymethod on ResidentSweepSim (u64) and ResidentSweepSimU128 (u128),
// both delegating to one width-generic core:
fn simulate_masks_metric_scalars(
    masks: Vec<u64> | Vec<u128>,       // per lane, as today
    is_buy: bool,
    sl_paisa: i64, tp_paisa: i64,      // same per-batch envelope checks as simulate_masks
    sl_next_open: bool, tp_next_open: bool, tp_first: bool,
    start_equity_paisa: i64,           // validated > 0 at the boundary
) -> PyResult<Vec<MetricScalarRow>>    // positionally aligned with masks
```

`MetricScalarRow` — one flat tuple per mask (int/float scalars only, no nested
objects, no per-trade data):

| # | field | type | producing fold |
|---|---|---|---|
| 0 | status | u8 | 0=OK, 1=EQUITY_ENVELOPE_EXCEEDED (Python per-mask fallback) |
| 1 | n_trades | u64 | walk |
| 2 | net_pnl_paisa | i64 | Σ pnl (exact i64, checked) |
| 3 | gross_profit_paisa | i64 | Σ pnl where pnl>0 |
| 4 | gross_loss_abs_paisa | i64 | Σ −pnl where pnl<0 |
| 5 | winners / 6 losers | u64 | pnl>0 / pnl<0 counts |
| 7 | sum_mfe_paisa / 8 sum_mae_paisa | i64 | Σ |
| 9 | sum_holding_bars / 10 max_holding_bars | u64 | Σ / running max |
| 11 | largest_loss_paisa | i64 | signed min over losers, init 0 |
| 12 | max_consecutive_losses / 13 wins | u64 | exact `_consecutive_runs` port |
| 14 | dd_paisa i64 / 15 dd_pct f64 / 16 dd_bars i64 / 17 ulcer f64 | | `drawdown_stats_into` — the PROVEN fold, scratch reused |
| 18 | mean / 19 m2 / 20 m3 / 21 m4 | f64 | Welford/Pébay fold, token-for-token port |
| 22 | downside_sumsq | f64 | sequential Σ r·r for r<0.0 (strict `<`) |
| 23 | p95 / 24 p5 | f64 | sort returns + `_percentile` lerp, token-for-token |

Inputs already resident (no new Py→Rust column crossing): OHLC paisa, forced
flags, session ordinals, bar_bits — the existing `SweepColumns` built once per
tuple by `_derive_kernel_inputs` (Python stays the single source of paisa
rounding / IST calendar / session ordinals — §11 COMMON preserved).

Zero-trade masks: `status=OK, n_trades=0`, all other fields zeroed; Rust SKIPS
the drawdown fold; Python substitutes `_empty_metrics(FLAT_DRAWDOWN_STATS)`
verbatim (the +0.0 sign semantics stay Python-owned, gotcha spine.md #8).

Envelope: Rust computes `start + Σ|pnl|` in checked i64; `> 2^52` ⇒ status=1
with zeroed floats — Python re-runs THAT ONE mask through today's path
(materialise trades via the existing `simulate_masks_with_exit_indices([mask])`
+ today's `metrics_from_trades`), bit-identical by construction. Same treatment
for any per-batch SL/TP/price envelope miss: the wrapper detects FIRST and
routes the whole chunk to today's path (existing warn-once reasons), never a
kernel error where the oracle would simulate (invariant I7).

### Python seam: NEW `brutex/engine/sweep_fused.py` + a ~25-line edit to `_iter_mask_metrics`

- `sweep_fused.py` owns: flag `BRUTEX_SWEEP_FUSED_CORE` (via the COMMON
  `env_flag_tristate`, unrecognised → OFF + warn-once, read per call like
  rust_metrics — parallel workers own their env); default constant
  `SWEEP_FUSED_CORE_DEFAULT = False` (the later flip is ONE constant + its own
  ADR, the #1186→#1532 two-step); per-process burn-in counter
  (`SELF_CHECK_SAMPLES = 16`), autorevert latch, `reset_self_check_for_tests` /
  `reset_observability_memo_for_tests` twins; `metrics_compare_key(ResultMetrics)`
  (ints `==`, floats `struct.pack('<d')` bit pattern, `None` distinct — the
  #1531 idiom widened to all ~40 fields); `metrics_from_scalar_row(row, ...)`.
- `metrics_from_scalar_row` maps a row to `ResultMetrics` by calling the SHARED
  `_metrics_from_aggregates` tail (see slice 0 below) — zero duplicated metric
  arithmetic (jscpd-safe), bit-identity by construction for every derived field.
- `_iter_mask_metrics` (sweep_run.py) gains one guarded arm per chunk:
  flag ON ∧ sim fused-capable → `sim.simulate_masks_metric_scalars(chunk, …)`;
  else today's code verbatim. The fused call sits INSIDE the same `try` — chunk
  failure rides the UNCHANGED `_reattribute_chunk_failure` ladder (oracle replay,
  never adopted); `MemoryError` re-raises bare; batch-contract length guard
  identical. Yield order = input order, unchanged.
- `metrics_from_trades` is NOT edited — the exit-index fast path, its bench
  gate, and the drawdown burn-in machinery stay byte-identical for the OFF arm
  and every fallback.

## (b) What moves to Rust vs stays Python — per item

| Item | Where | Why |
|---|---|---|
| Trade walk | Rust (already; `simulate_one_core` reused UNCHANGED) | proven bit-identical (#563/#1182) |
| Per-trade ClosedTrade materialisation | **DELETED on the fused path** (kept verbatim for fallback/oracle/finalists) | metrics need scalars, not objects; finalist projector re-simulates via the Python oracle anyway (spine.md #12) |
| Drawdown 4-family | Rust (existing `drawdown_stats_checked` via new `_into` seam, scratch reused, exits consumed in-Rust — no marshal) | proven fold, only alloc/marshal changes |
| Integer trade aggregates, consec runs, max_holding, largest_loss | Rust | exact i64 arithmetic, zero float risk; checked adds under the existing `MAX_PRICE_PAISA` ceiling proof |
| returns division, Welford moments, downside Σr², percentile sort+lerp | Rust | O(trades) — must move to kill the 55 µs; all four are +,−,×,/ (+ IEEE-exact int→f64 inside the 2^52 envelope) — exactly reproducible; token-for-token ports |
| ALL divisions/ratios (`_safe_ratio` Nones), `//` floor-divs (expectancy, avgs), gate boundaries, `end_equity < 1` ruin clamp, win_rate, time_in_market, tail_ratio | **Python** (shared `_metrics_from_aggregates`) | O(1)/mask; None-vs-NaN semantics + boundary operators stay literally today's code — bit-identity by construction |
| CAGR `growth ** (1/span)`, skew `pow(m2_n, 1.5)`, kurtosis, std_dev/sortino `sqrt` | **Python** | the #1186 libm precedent; O(1)/mask; span_years computed once per tuple in Python as today |
| `ResultMetrics` construction, SurvivorColumns, owned_nbytes watermark, rank, finalist projection, store/sidecar/sentinel/audit | **Python, untouched** | resume/idempotence/identity contracts (identity.md §7 "cheapest safe split") |
| Enumeration + `has_conflict` + Stage-1B kept-set | Python in slices 0–2; **Rust in slice 3** (separate flag) | independent 2.4 µs/enumerated lever; integer-only, order-pinned |
| run_id/blake3, dedup, checkpoints | Python, untouched | digest path has NO per-mask dedup (I3); lazy run_id stays lazy |

## (c) Batching strategy, batch boundaries, kill -9 resume

- FFI unit = the EXISTING chunk: `_trade_sim_chunk_masks(n_bars) = max(1,
  min(256, 4·2^20 // n_bars))` — reused as-is, so the chunk-invariance pins and
  memory envelope carry over unmodified. No new batch geometry to review.
- Batch boundaries sit strictly INSIDE one tuple, BELOW every durable boundary:
  workers partition by TUPLE (sweep_parallel), checkpoints record TUPLES,
  the completion sentinel is the intra-tuple redo authority. The Rust call holds
  ZERO durable state and ZERO cross-call state (suffix-independence statelessness
  gate) — a `kill -9` anywhere (worker, parent, mid-FFI) leaves the sentinel
  absent ⇒ the FULL tuple is deterministically redone and rewritten
  byte-identically (DEC-MARK-MANY-SENTINEL-001). "Batch fully-durable-or-fully-
  redone" is inherited, not engineered.
- Under `sweep_parallel`, workers pin `RAYON_NUM_THREADS=1`; the fused core uses
  the existing order-preserving `par_iter().map().collect()` dispatch (per-mask
  folds are internally sequential and per-mask independent, so serial ≡ parallel
  bit-for-bit; the mutation-gated `below_parallel_threshold` predicate is reused).
- #1295/#1291 composition: both verified to touch zero mask-loop files; the fused
  arm is invisible to them. A Rust panic under `panic=abort` = worker SIGABRT →
  #1291 quarantine → repeated deaths trip #1295's breaker; the runbook addition
  names `BRUTEX_SWEEP_FUSED_CORE=0` as the first lever (no auto-flip — the
  open_questions Q5 stance). Gap-register row #3 (tuple-cache identity) and #4
  (run layout) are P2-owned files we do not touch; nothing here reads or writes
  `sweep_runs/` or `_tuple_cache_params`.

## (d) Bit-identity strategy per float operation

The governing trick: **no operation with a cross-language rounding hazard crosses
the language boundary.** Register:

1. **Integer aggregates** — i64 `checked_add` under the proven
   `MAX_PRICE_PAISA = i64::MAX/8` ceiling (worst |pnl| ≤ 3·C ⇒ Σ over ≤ n_bars/1
   trades bounded; additionally the 2^52 equity screen bounds Σ|pnl|). Any
   checked failure ⇒ status=1 ⇒ Python oracle fallback for that mask. No Rust
   floor-division anywhere: `//` stays Python.
2. **returns r = pnl/start** — both operands ≤ 2^52 (envelope pre-screen IN the
   kernel, exact i64), so `as f64` conversion is exact and the single IEEE
   division is correctly rounded identically in CPython and Rust. Outside the
   envelope: status=1 fallback (CPython's exact-rational bigint division would
   otherwise diverge — never risked).
3. **Welford/Pébay fold** — port the EXACT update expressions with the exact
   operand order (`term1 * delta_n2 * ((n*n − 3n + 3) as f64)` etc.; the int
   subexpression is small ⇒ exact conversion), strictly sequential
   left-to-right over trades. Every step is +,−,×,/ — correctly rounded in both
   languages ⇒ identical bits for identical input order.
4. **downside Σ r·r** — sequential fold in trade order matching the genexpr;
   strict `r < 0.0` (−0.0 excluded in both). `sqrt` stays Python.
5. **percentile** — Rust sorts the finite returns (`sort_unstable_by(partial_cmp)`;
   no NaN possible inside the envelope; equal values are indistinguishable so
   stability is irrelevant) and evaluates `_percentile`'s exact index/lerp
   expression token-for-token (`idx = pct/100·(n−1)`; floor; lerp) — +,−,×,/ only.
6. **drawdown family** — `drawdown_stats_checked` UNCHANGED (2^52 pre-guard,
   sequential per-BAR f64 fold, no run-collapsing, ulcer sqrt already proven);
   buffer reuse changes allocation, never fold order. Exits are consumed as an
   in-order merge (exit indices are monotonic from the walk) — per-bar equity
   deltas are exact i64 either way.
7. **libm (`pow`)** — CAGR and skewness NEVER computed in Rust (the #1186
   precedent, kept). `sqrt` for std_dev/sortino stays in Python purely to keep
   the diff at "moments in, ratios out" (ulcer's Rust sqrt is the one proven
   exception, pre-existing).
8. **Signed zeros / NaN / None** — FLAT zero-trade stats stay Python (+0.0 signs
   owned there); `_safe_ratio` None semantics stay Python (no NaN encoding ever
   crosses FFI — every returned float is a finite fold result or unread);
   burn-in compares floats by `struct.pack('<d')` bit patterns so ±0.0 cannot
   hide.
9. **FMA / reassociation** — rustc stable emits no `contract`/`reassoc` flags;
   `mul_add` is the only FMA door: the existing #1186 grep pin (`.mul_add(`,
   `use rayon` reduction forms, fast-math tokens) is EXTENDED to
   `sweep_core.rs` and the workflow files; build profile stays the committed
   `opt-level=3, lto="thin", codegen-units=1, panic="abort",
   overflow-checks=true` with no `-C target-feature`/RUSTFLAGS additions. FP
   folds are scalar sequential loops (no reassoc flag ⇒ LLVM cannot vectorise
   the reduction); integer loop auto-vectorisation is allowed (bit-exact).
10. **Gate/tie boundaries** — all comparisons that produce verdicts
    (`passes_gates`, `>= min_hits`, is_win/is_loss, `end_equity < 1`,
    `r < 0.0`) run in Python on bit-identical inputs ⇒ identical verdicts;
    exact-boundary tests at each threshold on both arms.

## (e) Safety pattern (ADR-0017 lineage, retained in full)

- **Default-OFF** `BRUTEX_SWEEP_FUSED_CORE` (`env_flag_tristate` imported, never
  copied; unrecognised → OFF + warn-once `flag_unrecognised`). Later default-ON
  is a separate one-constant ADR.
- **Burn-in**: first 16 fused-served masks per PROCESS also run TODAY'S full
  path for the same mask (`simulate_masks_with_exit_indices([mask])` + today's
  `metrics_from_trades`) — the oracle is literally the shipping code, zero new
  oracle logic — and the full ~40-field `ResultMetrics` is bit-compared via
  `metrics_compare_key`. Mismatch ⇒ the ORACLE value ships, the fused arm
  latches OFF for the process, ONE `sweep_fused_core.kernel_autoreverted` ERROR
  (reason `self_check_mismatch`), later calls short-circuit at
  `kernel_disabled`. Per-worker fresh budget under sweep_parallel (process-
  global single-cell, the rust_metrics idiom). Honest limit stated as #1531
  states it: the burn-in catches only wrongness inside its window; the
  golden/property/twin-run layers are the net beyond it.
- **Engaged-event anti-vacuous goldens**: ON arm reproduces `_GOLDEN_RUN_ID` +
  `_GOLDEN_CONTENT_HASH` byte-for-byte WITH exactly one
  `sweep_fused_core.engaged` event and zero autoreverts; a domain-valid poison
  (monkeypatched pymethod) past burn-in MOVES the hash (proves consumption);
  the same poison during burn-in lands on pinned bytes with one autorevert.
- **Fallback law**: every fallback reason (`wheel_missing_fused`,
  `flag_unrecognised`, `equity_envelope_exceeded`, `kernel_error` on narrow
  `(ValueError, TypeError, OverflowError)`, `kernel_disabled`) lands on the
  bit-identical today-path, warn-once, results never depend on which arm ran.
  MemoryError re-raises bare everywhere.

## (f) Phased delivery — each slice tiny, independently golden-proven, revertable

**Slice 0 — Python-only seam refactor (no behaviour change).**
`metrics/compute.py`: extract `TradeAggregates` (frozen dataclass mirroring row
fields 1–13 + moments + downside_sumsq + p95/p5 presence) + `_aggregate_trades
(trades, start_equity)` + `_metrics_from_aggregates(agg, total_bars, benchmark,
start, span, drawdown)`; `_compute_metrics_impl` becomes compose-of-the-two.
Every golden/property/mutation suite passes UNMODIFIED (the #1186 "pure
parameterisation" precedent). Revert = one file. ~150-line diff.
*Gate: whole-package 100% cov; goldens byte-identical; jscpd unchanged.*

**Slice 1 — Rust kernel + default-OFF wiring (ONE PR with slice 0 or immediately
after).** `sweep_core.rs` pure cores + the two pymethods + `drawdown_stats_into`
seam; `sweep_fused.py`; the guarded arm in `_iter_mask_metrics`. Ships
default-OFF: the no-env arm is byte-identical today's spine (OFF-cost gate: one
env probe per call, same envelope as #1186).
*Gate: full golden arm matrix (OFF / forced-ON / poison×2 / SELL parity);
twin-run byte-identity on (i) the golden fixture grid, (ii) a small real-data
grid (2 TFs × both directions × k∈{1,2,3} × ±SL/TP), (iii) a workers>1
parallel run — OFF vs ON arms into separate roots, `content_hash` + Parquet
bytes + sidecar + rank order equal; hypothesis cross-arm full-ResultMetrics
bit-equality; chunk-invariance ≥5 sizes; P-06 + suffix-independence UNMODIFIED;
cargo test/clippy/fmt; mutants glob extended (+1 shard if budget needs);
bench floor ≥1.3× fused-vs-OFF on the 48k fixture (measured ~2.3×, flake-proof
margin) + forced-demotion arm below floor.*

**Slice 2 — soak + default-ON flip.** Separate ADR + one-constant flip
(`SWEEP_FUSED_CORE_DEFAULT = True`) after a full-grid twin run on the real box
and ≥1 week of default-OFF availability (the #1531→#1532 pattern). Kill switch
remains forever.

**Slice 3 — enumeration/prefilter fusion (independent PR, own flag
`BRUTEX_SWEEP_RUST_ENUM`).** New `ResidentBars`-adjacent method
`enumerate_kept(condition_bits, k, conflict_pairs, min_hits) →
(kept_masks, enumerated, conflict_pruned, evaluated)` reproducing
`iter_masks_k_of_bits` lexicographic order + `has_conflict` + Stage-1B in one
streamed pass (bounded internal batches; counts returned once). Tallies parity
is exact-integer; order property-pinned vs the itertools reference over random
bit-sets; tallies feed audit/sidecar unchanged. Kills the 2.4 µs/enumerated
Python term (~50× cheaper), the k≥5 grid's co-dominant cost post-slice-1.
Revert = flag.

**Slice 4 (optional, evidence-gated).** Columnar row transport (one
`Vec<i64>`+`Vec<f64>` pair per chunk instead of per-mask tuples) IF the ~1–2 µs
/mask tuple marshal shows up at production scale. Not scheduled until measured.

Each slice: independent branch, independent golden proof, revert = flag flip or
single-commit revert; no slice edits another phase's zone (only
`brutex/engine/{sweep_run.py,sweep_fused.py,rust_trade_sim.py-adjacent}`,
`brutex/metrics/compute.py` (slice 0 seam), `rust/fno-math/**` — all inside our
ownership zone; `metrics/compute.py` is engine-consumed COMMON but the refactor
is a pure parameterisation with the full suite as proof).

## (g) Test plan (incl. mutation)

Rust (`sweep_core.rs`, in-crate `#[cfg(test)]` + tests/):
- Exact-value pins for every fold, with expected values GENERATED BY CPython and
  baked as consts (moments over adversarial streams incl. ±0.0, denormals,
  alternating huge/tiny; percentile lerp at n∈{1,2,3,20,101} × pct boundaries;
  consec runs with scratch trades; envelope AT 2^52 and 2^52+1).
- proptest: fold vs a naive in-crate reference (integer aggregates), monotonic
  exit stream invariant, zero-trade row shape, status flag iff envelope.
- cargo-mutants: `examine_globs` gains `src/sweep_core.rs`; zero-survivor bar;
  budget measured, "+1 shard" the sanctioned dial. `.max()`-over-`if` idiom to
  kill tie mutants; logic kept OUT of PyResult wrappers (unviable-mutant rule).
- cargo-fuzz: one target driving the pure core with arbitrary columns/masks
  (panic/OOB/overflow = red; SIGABRT discipline).

Python:
- `tests/unit/test_engine_sweep_fused.py` (stem-matched for the discovery gate +
  the engine static 100% gate): flag tristate ×4, wheel-missing probe, all
  fallback reasons forced, burn-in match/mismatch/latch, reset seams, compare-key
  signed-zero + None-vs-0.0 discrimination, per-mask envelope fallback, zero-trade
  mapping, exact-boundary tests (min_hits, end_equity=0/1, r=−0.0, pnl=0 scratch).
- Golden: the arm matrix of §(e) added to `test_engine_sweep_run_golden.py`'s
  pattern WITHOUT touching the frozen literals; fixture diff EMPTY.
- Property: hypothesis cross-arm full-column bit-equality (the
  `test_sweep_rust_metrics_equivalence` pattern extended); chunk-invariance;
  statelessness (prefix-run-after-full-run); P-06 + suffix-independence + trade-sim
  parity suites pass UNMODIFIED.
- Bench: fused-vs-OFF floor 1.3× + demotion twin below floor; OFF-arm cost gate;
  ratios routed through `_o1_tiers`; per-mask cost grid-size-independent pin.
- Mutation-killing by convention (sweep modules are not mutmut-gated): every new
  boundary/branch has an exact-value test that a comparison/constant mutant fails.
- Twin-run script committed under tests/integration (both arms, separate roots,
  byte-compare) — runs in CI on the small grid; the grid-scale twin run is a
  documented pre-flip (slice 2) gate on the real box.

## (h) Risk register (top 8)

| # | Risk | Mitigation |
|---|---|---|
| 1 | Float bit-divergence in the 4 new Rust folds (Welford/percentile/downside/returns) | token-for-token ports of +,−,×,/ only; envelope pre-screen makes every int→f64 exact; no pow/no new sqrt crosses FFI; CPython-generated exact-value pins; burn-in full-metrics bit-compare; hypothesis cross-arm; twin-run gate; per-mask oracle fallback |
| 2 | FMA/reassoc silently introduced by toolchain or a future edit | grep pin extended (mul_add/fast-math/reassoc tokens, crate + workflows); committed build profile unchanged; scalar sequential fold shapes |
| 3 | Stale wheel lacking the new METHOD (symbol handshake is module-level only) | getattr probe → warn-once `wheel_missing_fused` → bit-identical fallback; engaged-event golden makes a silently-Python CI run FAIL; source-digest + crate version bump feed doctor |
| 4 | Wrongness past the 16-mask burn-in window | honest limit disclosed; poison-past-burn-in golden proves consumption; twin-run on real grids; hypothesis compares thousands of masks; #1295's R45 diffcheck layer composes |
| 5 | Panic/SIGABRT (overflow-checks, OOB) killing the interpreter/worker | all boundary validation reused from `build_columns`/batch checks; pure Result cores; checked arithmetic under proven ceilings; fuzz target; a SIGABRT lands in #1291 quarantine → #1295 breaker → runbook names the kill switch |
| 6 | Behavioural drift in error attribution / tallies / audit framing | fused call sits inside the SAME try (fingerprint format untouched); tallies computed at unchanged Python sites (slices 0–2); 3-audit-lines-per-tuple pin; summary-tally goldens |
| 7 | cargo-mutants budget overrun on the 6-shard matrix | measure on branch; "+1 shard" sanctioned dial; logic-out-of-wrappers keeps mutant count tight |
| 8 | Slice-0 refactor of COMMON `metrics/compute.py` regresses a non-sweep consumer (Stage-3/walk-forward/cascade all consume `metrics_for_mask`/`compute_metrics`) | pure parameterisation with the FULL suite (goldens + property + weekly mutmut on metrics/compute) as proof; landed alone as its own revertable commit before any Rust |

## (i) Expected speedup — honest, with error bars

Sandbox-measured basis only (M4-Pro absolutes claimed by nobody until
bench-m4pro runs; ratios per the #1182 convention):

- Per KEPT mask @48k bars: 400 µs → ~150–180 µs (removes ~202 µs Python residual;
  drawdown alloc/marshal −10–25 µs; adds back ~4–8 µs Python O(1) tail + ~1–2 µs
  row marshal). **≈2.2–2.7× (±30%)**. At 20k bars: 163 → ~70–90 µs.
- Per ENUMERATED mask (slice 3): 2.4 µs → ~0.05–0.2 µs.
- Blended per-combo on scenario-B k≤6 grids: the 10–50 µs/combo band is
  PLAUSIBLE for mid/high TFs after slices 1+3, **but the 1-minute-TF caveat is
  mandatory**: per-mask O(n_bars) ops cost ~143 µs (prefilter) to ~710 µs (walk,
  per kept) at 555k bars — the flat band cannot hold there; wall-clock model per
  combo_counts.md §3 with the ×1.15–1.35 parallel-efficiency correction (~/6.5
  effective, not /8).
- Wall-clock projection (PROJECTION, not a claim): k≤6 scenario B ~5.9 d → an
  estimated ~1–2.5 d after slices 1+3, bounded below by the walk-dominated 1m/2m
  tuples and the per-tuple fixed costs (Stage-1A, finalist projection, flush)
  which this design deliberately does not touch. Per-operation O(1), total
  O(N_masks × N_bars) — Rust changes the CONSTANT only.
