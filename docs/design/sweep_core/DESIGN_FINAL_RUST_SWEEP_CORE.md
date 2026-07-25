# DESIGN_FINAL_RUST_SWEEP_CORE.md — the synthesized final design

Status: **LOCKED FOR CHECKPOINT 1 pending verification round.** Date
2026-07-09 (external fold + lock round: Verifier B findings F1–F5/F15/F18/
F21–F23/F27 + baseline-truth-pack + PERF RECONCILIATION folded; the 7-round
hostile-review fix-forward register (13 items) resolved; coordinator rulings
reflected — see `## External fold + lock round` at the end of this doc).
Previous: FINAL (post-3-judge synthesis), 2026-07-08.
Base: the **correctness** candidate ("folds-out, verdicts-in-Python"), winner
2-of-3 judges; every judge-endorsed graft from riskmin / perfmax / grounding
folded in; every named fatal flaw resolved inline (§14 Decision log).
Grounding: `baseline.md` (measured), `SPEC_RUST_SWEEP_CORE.md` (contract),
`combo_counts.md` (grid math), `open_questions.md`, `reads/*.md`,
repo read-only at `/home/user/bruteX`.

Golden Rule #10 framing, everywhere: **per-operation O(1), total
O(N_masks × N_bars). Rust changes the CONSTANT; claiming O(1)-total is
forbidden.** All absolute times are sandbox ratios (shared x86 container);
no M4-Pro absolute is claimed until the bench-m4pro lane measures it
(DEC-BENCH-ABS-BASELINE-001).

---

## 0. Design thesis (proof-obligation inversion + full-pipeline deletion)

The per-kept-mask metric body (`_compute_metrics_impl`,
`brutex/metrics/compute.py:569`) is two structurally different computations:

1. **FOLDS** — O(trades)/O(n_bars) sequential accumulations (integer trade
   aggregates, Welford/Pébay moments, downside Σr², sorted-returns p95/p5,
   consecutive-run streaks, the per-bar drawdown/ulcer scan). ~100% of the
   per-mask COST; consist ONLY of `+ − × /`, integer sums, one stable sort,
   ONE CPython-3.12 **Neumaier-compensated** builtin `sum()` (the downside
   Σr² — ported as CPython's exact compensation loop, NOT a naive fold;
   §4 row 6), and one already-proven Rust kernel (`drawdown_stats_checked`).
2. **VERDICTS** — O(1) scalar combination (~25 divisions, `_safe_ratio`
   None-decisions, pinned comparison boundaries, the CAGR/skewness libm
   `pow`s, `math.sqrt`s, clamps, the 37-field `ResultMetrics` construction).
   ~1–3 µs of cost and ~100% of the SEMANTIC divergence risk.

**The bet: move ONLY the folds (plus the integer-exact
enumeration/conflict/prefilter pipeline) into one resident Rust core in
`fno-math`; every verdict, every Optional, every libm call, every gate
boundary, every floor-division stays in today's literal Python code,
parameterised — bit-identity by CONSTRUCTION, not by per-expression
argument.** The Rust float-proof surface collapses to exactly NINE f64
columns per mask plus fifteen exact-integer columns, each a strictly
sequential fold with a mechanical token-for-token porting rule and a
`struct.pack` differential oracle. Trades never cross the FFI — now an
ACCEPTANCE CRITERION with measured teeth (Verifier B PERF RECONCILIATION:
a half-port leaving `ClosedTrade` materialisation in Python collapses
dense-mask gains to a MEASURED 1.04–1.16×; `fold_block`'s columnar returns
satisfy the criterion by construction — §9.1); **NON-KEPT** enumerated masks
never materialise as Python ints (fix-forward item 8 scope correction: KEPT
masks DO materialise — `enumerate_prefilter`'s Vec return is a Python list
of PyLongs, ~O(10⁷)/k=6 tuple per §8 R7, identical to today's survivor-list
shape and immaterial beside the O(n_bars) walk; on min_hits==0 grids kept ≈
80% of enumerated at k=6, so the earlier unscoped "enumerated masks never
materialise" sentence was false and is corrected — the per-enumerated win in
§9 is correctly scoped to non-kept masks); Python consumes bounded columnar
byte blocks.

Measured basis (`baseline.md` §3, 48k-bar sandbox, per kept mask ≈ 400 µs):
eliminates the FFI trade marshal (~42 µs), the ClosedTrade/Python churn
(~98 µs), the Python seam (~32 µs), moves the O(trades) share of the metric
body (~50 of 55 µs) into the fold, and separately kills the
2.4 µs/enumerated-mask Python enumeration+conflict term — the dominant
**PYTHON** cost on k=5/6 grids (5.5M–50M enumerated masks per tuple,
`combo_counts.md` §1). Honest scope (round-1 correction; round-6 min_hits
scoping): it is the dominant
TOTAL cost only on high-TF (small-n_bars) tuples — **on `min_hits > 0`
grids** every conflict-FREE mask
still pays the RETAINED O(n_bars) Rust prefilter count (0.257 ns/bar·mask),
which exceeds 2.4 µs above ~9.3k bars, so on the wall-clock-dominant
1m/2m/3m/5m tuples the untouched prefilter floor governs (§9 mandatory
caveat; the hit-list-intersection lever, §10, is the named answer there).
On a **`min_hits == 0` grid the prefilter count term is ZERO in BOTH
arms** — today's spine never runs the Stage-1B count there
(`if cfg.min_hits > 0 and survivors:`, sweep_run.py:1465) and the core is
MANDATED to skip it identically (the §1.3 MIN-HITS SHORT-CIRCUIT LAW,
round-6 — the enum-side twin of the zero-trade scan law, with its own
in-band `hits_counted` observable); the earlier unscoped "every
conflict-free mask still pays … exactly as today" wording was false for
those grids and is corrected here and in §9.
The ≈11–13 µs/kept Python verdict pass deliberately stays — `baseline.md`'s
own two-point fit of `compute_metrics_from_scalars` (28.5 µs @18 trades,
54.9 µs @43.6) yields an O(1) intercept ≈ 10 µs for the retained
construction+verdict residue, PLUS the ~1–3 µs relocated per-mask seam cost
(decode-row access, zip(strict), flag branch, kept-PyLong churn), i.e.
**~6–8% of the 48k-bar walk+scan floor**; the earlier "~3–6 µs / <5%" AND
"~3–10 µs" bands sat below this composed floor and are withdrawn (final-lock
item FL-5: this paragraph previously still led with the withdrawn ~3–10 µs
band SPEC §1 and §9 row 1 had already deleted — the residue reads ≈11–13 µs
at every corrected site now, and is re-MEASURED on-branch before any tighter
claim ships, §9).

Beyond the winning candidate, this final design adds (judge-endorsed grafts,
§14): a **permanent finalist belt** (every ranked finalist re-proven by the
Python spine before it persists — forever, not just during burn-in), a
**64-mask dual-oracle burn-in**, riskmin's **Slice-0-first sequencing** of
the compute.py decomposition, riskmin's **`drawdown_stats_into` scratch
seam**, perfmax's **`max_work_cells` GIL/liveness bound + dual-knob
invariance matrix**, and grounding's **identity register / gap-register
crosswalk / coordinator-ruling items** carried verbatim.

---

## 1. Architecture + exact FFI surface

### 1.1 Rust placement: `rust/fno-math/src/sweep_core.rs` (NEW module, existing crate)

- `rust/trading-core` is FROZEN (DEC-SHARED-CORE-001; TickVault bit-identity;
  400-case answer-key) — untouched, diff must be EMPTY.
- A sibling crate is rejected: the core MUST consume
  `trade_sim.rs::simulate_one_core<W>`/`MaskWord`/`SweepColumns`/`build_columns`
  and `metrics.rs::drawdown_stats_checked` as internal Rust functions (the
  whole point — trades never cross the FFI). A sibling crate would
  (i) path-depend on fno-math, whose source-digest handshake does NOT cover
  path-dep drift (`reads/rust.md` §4 — a stale-wheel blind spot on the exact
  code we depend on); (ii) compile a SECOND trade walker into a second `.so`;
  (iii) cost the facade-golden move, ~27 ci.yml install sites, a new mutants
  matrix, a new fuzz crate, and a new EXPECTED_BUILD handshake.
- fno-math is BruteX-only (TickVault couples to trading-core only), already
  carries the exact release profile (`opt-level=3, lto="thin",
  codegen-units=1, panic="abort", overflow-checks=true`), the
  no-unwrap/no-expect lints, the pure-`Result<_,String>`-core /
  thin-PyErr-wrapper convention (cargo test links no libpython; PyResult
  wrappers are UNVIABLE mutants — logic lives in `_impl` cores), and the
  6-shard cargo-mutants matrix (round-1 correction: the #1597 "+1 shard"
  overrun dial was ALREADY CONSUMED by the u128 merge, 5→6 — a further
  overrun needs a NEW ledgered re-shard decision; budget measured on-branch,
  §7.6 / §8 R10).
- fno-math's crate version is NOT in `_indicator_version()` → core engagement
  never re-keys any run (`reads/rust.md` §4). Version bumps 0.3.x for doctor
  freshness only.

Symbol additions land in ONE commit updating the pinned sextuple
(`#[pymodule]`, `_EXPECTED_COMPILED_SYMBOLS`, shim import list, `__all__`,
`__init__.pyi`, `EXPECTED_BUILD.json`): `ResidentSweepCore`,
`ResidentSweepCoreU128`, `SWEEP_CORE_ABI_VERSION`, **plus (round-4)
`SWEEP_CORE_FOLD_INT_FIELDS` and `SWEEP_CORE_FOLD_FLOAT_FIELDS`** —
module-level tuples of field-name strings exported FROM the Rust consts, so
the §1.3 layout golden's "Python tuples == Rust consts" compare has an
actual Python-readable mechanism (with only the original three symbols the
golden could verify strides/ABI shape but never the Rust field ORDER; the
round-3 claim was unimplementable as written). Five symbols, all in the
same one-commit sextuple update. New SYMBOLS (not new
methods on existing classes) mean the module-level handshake genuinely covers
wheel staleness — closing riskmin's method-level-staleness gap — and the
in-band ABI version int is the second belt.

Internal layout: `sweep_core.rs` holds pure `Result<_,String>` cores
(`enumerate_prefilter_impl`, `fold_block_impl`, the fold functions) with
one-line PyErr wrappers; `metrics.rs` gains the `drawdown_stats_into(&mut
Vec<i64>, …)` seam (riskmin graft): the EXISTING public
`sweep_drawdown_stats` pyfunction becomes a thin allocating delegate, so its
tests, goldens, and 358 mutants pass UNMODIFIED — the fold body never moves.

### 1.2 Python placement (ownership zones — see §11)

- NEW `brutex/engine/sweep_core.py` — the seam module: flag resolution,
  burn-in, autorevert latch, finalist belt, fold-block decode, fallback
  ladder, observability. Ours ("new modules" rule).
- `brutex/engine/sweep_run.py` — mask-loop arm selection + the belt hook.
  Ours.
- `brutex/engine/rust_trade_sim.py` — export the existing
  `_derive_kernel_inputs` for reuse (paisa/forced/session columns stay
  Python-single-source, §11 COMMON). Ours; the file itself is retained
  verbatim as oracle/fallback.
- `brutex/metrics/compute.py` — ONE pure-parameterisation refactor
  (Slice 0): `_compute_metrics_impl = combine_metrics_from_folds(
  _fold_trades(...))`. Zone handling in §11 / risk R1.
- NO edits to: sweep_supervisor, sweep_parallel, sweep_orchestrator,
  sweep_scheduler, checkpoint/sentinel/audit/store writers, `results_store.py`
  (P3-exclusive), `brutex/ranking/*` internals, trading-core, or any
  #1295/#1291 file.

### 1.3 FFI surface (exact)

Two frozen pyclasses over one width-generic `SweepCoreData<W: MaskWord>`
(reusing `SweepColumns` + `build_columns` validation verbatim — the
`MAX_PRICE_PAISA = i64::MAX/8` envelope and 7-column length checks are the
SAME already-mutation-gated code paths):

```text
#[pyclass(frozen, module="fno_math")] ResidentSweepCore        (bar_bits: Vec<u64>)
#[pyclass(frozen, module="fno_math")] ResidentSweepCoreU128    (bar_bits: Vec<u128>)

__new__(open_paisa, high_paisa, low_paisa, close_paisa: Vec<i64>,
        is_forced: Vec<bool>, session_idx: Vec<i64>,
        bar_bits: Vec<u64|u128>) -> PyResult<Self>
    # identical validation to ResidentSweepSim::__new__
    # WINDOW-LENGTH INVARIANT AT THE SEAM (Verifier B F27 / B-INV-102 —
    # the FOURTH skip authority): `run_one_tuple` returns None for an
    # EMPTY bar-window and ADVANCES the positional checkpoint with NO
    # cache/sentinel/artifact — so an FFI marshalling defect that
    # yielded an empty (or shortened) window would be a CLEAN-EXIT
    # silent false-skip. Closed structurally: (a) the empty-window skip
    # authority stays PYTHON-side PRE-FFI (the None-skip fires on the
    # Python-loaded window BEFORE any core construction;
    # `run_persisted_sweep` refuses empty bars, sweep_run.py:1287 — the
    # core can never manufacture that skip); (b) the seam asserts
    # `core.n_bars() == len(<python-side bars>)` immediately after
    # construction and before ANY call — a mismatch is the NAMED
    # refusal `bars_length_mismatch` (warn-once, spine serves), so a
    # non-empty Python window can never arrive empty/short across the
    # FFI; (c) §7.4 pins BOTH directions: an empty Python-side window
    # reproduces skip-not-error at the SAME tuple with the core never
    # constructed, and a stubbed n_bars()-lying core trips
    # `bars_length_mismatch` deterministically.

n_bars(&self) -> usize

enumerate_prefilter(
    condition_bits: Vec<u32>,        # from cfg.condition_bits (Python-sorted-unique;
                                     # re-sorted+deduped in core, pinned equal)
    k: usize,
    conflict_pairs: Vec<(u32, u32)>, # marshalled from vocabulary.conflicts.
                                     # CONFLICT_PAIRS per call — Rust NEVER
                                     # re-derives them (single source)
    min_hits: u64,
    start_rank: u64,                 # lexicographic combination rank
                                     # (combinatorial-number-system unranking)
    max_enumerated: u64,             # range bound — chunk-invariance knob
    max_work_cells: u64,             # enumerated×n_bars work bound (perfmax
                                     # graft) — the GIL/liveness knob; the call
                                     # returns early when consumed; default
                                     # 2^28 (re-scaled, §3)
                                     # CHARGING RULE, explicit (fix-forward
                                     # items 5+9): cells are charged per
                                     # enumerated mask × n_bars REGARDLESS of
                                     # the min-hits short-circuit — the charge
                                     # model is deliberately min-hits-BLIND
                                     # (liveness-conservative: over-charging
                                     # only SHORTENS GIL holds; results are
                                     # cap-invariant by the dual-knob matrix).
                                     # The §7.4/§7.5 cap-respected pins ENCODE
                                     # this: an implementer who "fixes" the
                                     # charge to actual O(k) work at
                                     # min_hits==0 would advance up to
                                     # max_enumerated masks and
                                     # deterministically FAIL the
                                     # `(next_rank − start_rank) × n_bars ≤
                                     # max_work_cells + n_bars` inequality on
                                     # the matrix's min_hits==0/large-n_bars
                                     # shapes — do not "fix" it. Honest cost
                                     # consequence stated beside the §9
                                     # per-enumerated row: at min_hits==0 /
                                     # 555k bars the cells cap terminates each
                                     # call at ~483 masks (2^28/555k) instead
                                     # of the 4096 max_enumerated floor ⇒
                                     # ~103,600 FFI calls per C(60,6) tuple vs
                                     # ~12,200 — ~µs call overhead each,
                                     # ~4–10 ns/enumerated-mask of call-
                                     # overhead tax (~3–30% of the per-
                                     # enumerated win on exactly those grids;
                                     # ~0.5–1.5 s/tuple — immaterial, safe
                                     # direction, stated not hidden).
) -> PyResult<(u64, u64, u64, bool, u64, Vec<u64|u128>)>
    # (enumerated_in_range, conflict_pruned_in_range, next_rank, exhausted,
    #  hits_counted_in_range, kept_masks)
    # kept = conflict-free AND (min_hits == 0 OR supermask_hits >= min_hits)
    # hits via the SAME (bar & mask) == mask predicate as count_one
    # MIN-HITS SHORT-CIRCUIT is a MANDATED LAW (round-6 — the enum-side
    # twin of the §1.3 zero-trade scan law, same hazard class): when
    # `min_hits == 0` the per-mask O(n_bars) supermask-hits count MUST NOT
    # run — kept = conflict-free directly, exactly today's spine
    # (`if cfg.min_hits > 0 and survivors:` guards the WHOLE Stage-1B
    # count, sweep_run.py:1465-1473 — at min_hits==0 today pays ZERO
    # per-mask O(n_bars) cost). This must be stated because a "natural"
    # implementation counting unconditionally is KEPT-SET- and
    # BYTE-IDENTICAL (the `min_hits == 0 OR …` predicate hides it) and
    # therefore invisible to burn-in / belt / twin-run / the dual-knob
    # matrix — yet it silently adds 0.257 ns × n_bars per conflict-free
    # mask where today costs O(1): ~12.3 µs/mask @48k (a ~5× REGRESSION
    # vs the 2.4 µs Python enum term this design claims a 20–80× win on)
    # and ~143 µs/mask @555k — hours added per k=6 1m min_hits=0 tuple.
    # Enforcement is BYTE-OBSERVABLE, in-band, exactly the `scans_run`
    # standard: `hits_counted_in_range` returns the number of masks whose
    # supermask-hits count actually EXECUTED in this range — a correct
    # call has `hits_counted == 0` when `min_hits == 0` and
    # `hits_counted == enumerated_in_range − conflict_pruned_in_range`
    # when `min_hits > 0` (the count runs on every conflict-free mask).
    # MERGE-BLOCKING nets are deterministic: in-crate cargo units
    # (min_hits=0 range ⇒ hits_counted == 0; min_hits>0 range ⇒ == the
    # conflict-free count) + a Python seam pin on the decoded value
    # (§7.4/§7.5); a mutant deleting the guard is data-identical but
    # flips `hits_counted` and dies deterministically (§7.6 — no
    # exclude_re entry needed). Observability/test data ONLY — never
    # persisted, never in any identity (the scans_run contract). The
    # §7.2 twin matrix's min_hits∈{0,30} shapes exercise both arms.
    # STRICT->= BIT-PARITY GATE (Verifier B F15/F18): the in-core
    # min-hits count MUST satisfy `count_hits ≡ count_supermasks`
    # bit-parity at the strict `hits >= min_hits` boundary — a property
    # gate over random (bar_bits, mask, min_hits) triples on BOTH kernel
    # widths (u64 + wide), plus the frozen Verifier B baseline-truth
    # corpus (h) keep/drop sets at the exact boundary baked as goldens
    # (a mask with hits == min_hits is KEPT in both arms; == min_hits−1
    # dropped in both). The predicate is the SAME `(bar & mask) == mask`
    # supermask test as `count_one` — the parity gate pins that the
    # fused Rust count never drifts from it.
    # EXHAUSTION CONTRACT (round-3 — previously undefined, so a driver had
    # to invent it): `exhausted` is computed IN-CORE as
    # `next_rank == total_count` (total_count = C(n_distinct, k), checked
    # u128); the §1.4 driver loops WHILE `not exhausted`, never on
    # "returned empty" or "no progress" heuristics. GUARANTEED-PROGRESS
    # LAW: every non-exhausted call enumerates AT LEAST ONE mask — the
    # work caps are checked AFTER each mask's work is done (the repo's
    # `max(1, …)` chunk-floor idiom, sweep_run.py:203), so
    # `next_rank > start_rank` on every non-exhausted return and a
    # zero-progress infinite driver loop is structurally impossible even
    # under a degenerate cap. Driver belt: a non-exhausted return with
    # `next_rank == start_rank` (unreachable by the law) ⇒ fallback reason
    # `enum_no_progress`, today's spine serves — never a busy loop.
    # TOTAL-RECONCILIATION BELT (round-4 — a self-consistent in-core
    # total_count defect was only probabilistically netted): total_count
    # is computed BY THE CORE, so a defect in the core's binomial /
    # unranking arithmetic yielding a too-small (or too-large) total is
    # fully self-consistent in-band — `exhausted` fires against the same
    # wrong total, the tallies and the kept set truncate/extend together,
    # and the wrong counts flow into SWEEP_COMPLETE, the trial-stats
    # sidecar (n_enumerated/n_conflict_pruned, sweep_run.py:1590-1600),
    # and the persisted population statistics. The driver ALREADY holds an
    # INDEPENDENT Python-side total on every path:
    # `n_masks = count_combos(cfg.k, n_unique)` (sweep_run.py:1300,
    # audit-stamped as SWEEP_START's `n_masks_enumerated`). MANDATE: on
    # loop exit the driver asserts final `next_rank == n_masks` AND
    # Σ enumerated_in_range == n_masks; a mismatch is the named fallback
    # reason `enum_total_mismatch` — today's spine serves the WHOLE
    # tuple, warn-once, exactly the `enum_no_progress` belt shape —
    # converting the entire total-count / rank-partition defect class
    # from probabilistic (CNS proptests, list-equality property,
    # twin-runs, the mask-count-floored enum shadow burn-in) to DETERMINISTIC per-tuple
    # detection at zero cost, using a computation that exists today.
    # Today's Python spine satisfies enumerated == n_masks by
    # construction (the loop and count_combos share iter_masks
    # semantics), so the assertion is a pure belt, never a behaviour
    # change; §7.4 pins it via a stubbed wrong-total return.
    # DOMAIN VALIDATION + CHECKED COMBINATORICS (round-1 fix; round-2
    # per-INDEX extension): the pure core validates UP FRONT, before any
    # shift, mask construction, or unranking:
    #   (a) k <= n_distinct_bits <= kernel width (64/128)   [COUNT bound]
    #       — k == 0 is LEGAL (round-3): `SweepRunConfig` validates
    #       `0 <= k <= n_unique` (sweep_run.py:543) and
    #       `iter_masks_k_of_bits(bits, 0)` doctests to `[0]`
    #       (combo_mask.py:677) — C(n,0) = 1, the single EMPTY mask 0,
    #       trivially conflict-free, hits == n_bars ((bar & 0) == 0 for
    #       every bar). The validation must NEVER tighten to `1 <= k`,
    #       and the CNS unranking must serve rank 0 → the empty
    #       combination; §7.5 bakes the k=0 vector (kept == [0],
    #       enumerated == 1, next_rank == 1, exhausted == true).
    #   (b) EVERY condition_bits index < W::BITS AND EVERY conflict_pairs
    #       index < W::BITS                                  [VALUE bound]
    #   (c) start_rank <= total_count                        [RANK bound]
    #       — round-3: the public FFI accepts an arbitrary u64 start_rank;
    #       a rank > C(n_distinct, k) has NO defined combination, and a
    #       "saturate at the last combination" implementation would return
    #       a silently WRONG kept set (not a panic — the round-4
    #       MODULE-scoped `#![deny(clippy::indexing_slicing)]` on
    #       sweep_core.rs (§8 R4 — the crate carries NO indexing lint
    #       today and cannot gain one crate-wide) + checked binomials
    #       make the abort class unlikely, which is
    #       exactly why the silent-result class needs its own refusal).
    #       start_rank > total_count ⇒ Err (`rank_out_of_domain`);
    #       start_rank == total_count is legal and returns
    #       (0, 0, total_count, exhausted=true, hits_counted=0, []).
    #       Unreachable via the
    #       production driver (rank 0 then core-returned next_rank) —
    #       the refusal is the same Python-reachability standard as (b);
    #       §7.5 pins it beside the C(128,64) refusal.
    # (b) is the round-2 fix: the count bound alone admits bits=[100] with
    # n_distinct=1 <= 64, and `1 << pos` mask/pair-mask construction is then
    # a shift-with-overflow panic ⇒ SIGABRT under the crate's
    # overflow-checks=true + panic="abort" profile — the SAME abort class as
    # the CNS binomials, same remedy: an Err refusal, never an abort. ALL
    # binomial-coefficient arithmetic in the CNS unranking is u128
    # checked_mul/checked_add, and a total combination count exceeding
    # u64::MAX returns Err — a clean PyErr, NEVER an overflow-check SIGABRT.
    # (Adversarial (n,k) like C(128,64) ≈ 2.4e37 IS Python-reachable through
    # this FFI and must be a refusal, not a process abort; §7.5 pins it —
    # and now also pins bits=[100]-on-u64 + a >=width conflict-pair index.
    # Python-side, cfg bits are validated only against MAX_VOCAB_BITS=128
    # (combo_mask.py:100), so a 64<=bit<128 index IS legal input reaching a
    # u64-core call through any seam width-selection bug: the refusal must
    # live in the core, and the seam's width SELECTION is keyed off the bit
    # indices too — §5 engagement condition.)

fold_block(
    masks: Vec<u64|u128>,            # a chunk of kept masks, input order preserved
    is_buy: bool,
    sl_paisa: Option<i64>, tp_paisa: Option<i64>,
    sl_next_open: bool, tp_next_open: bool, tp_first: bool,  # FillPolicy triple
    start_equity_paisa: i64,
) -> PyResult<(u32, u64, Py<PyBytes>, Py<PyBytes>, Py<PyBytes>)>
    # (SWEEP_CORE_ABI_VERSION,
    #  scans_run: the number of drawdown/ulcer scans this block actually
    #            executed (round-3 — the BYTE-OBSERVABLE oracle for the
    #            zero-trade short-circuit law below: a correct block has
    #            scans_run == count(plain rows), an all-zero-trade chunk
    #            has scans_run == 0; observability/test data ONLY — never
    #            persisted, never in any identity).
    #            SCAN-LAST ORDERING (round-4 — makes the equality oracle
    #            EXACT rather than over-strong): bit1's definition covers a
    #            non-finite value "anywhere in the fold", so a belt trip in
    #            a float fold executing AFTER the scan would yield a
    #            scanned-but-flag-served row and violate the equality on a
    #            conforming implementation (FFI-unconstructible through the
    #            validated entry points, but the pins must not encode an
    #            oracle the spec itself contradicts). MANDATE: per mask, the
    #            drawdown/ulcer scan runs ONLY after the envelope gate AND
    #            every float fold have completed finite — any bit1 trip
    #            short-circuits BEFORE the scan (the per-mask folds and the
    #            per-bar scan are order-independent computations, so the
    #            ordering is free) — hence a flag-served row NEVER counts a
    #            scan and `scans_run == count(plain rows)` holds exactly,
    #            including on the defence-in-depth bit1 paths,
    #  ints_le  : row-major i64-LE, stride = FOLD_INT_STRIDE  = 15,
    #  floats_le: row-major f64-LE, stride = FOLD_FLOAT_STRIDE = 9,
    #  flags    : 1 byte per mask)
```

Columnar layout (ONE layout law, mirrored as Python tuples
`FOLD_INT_FIELDS`/`FOLD_FLOAT_FIELDS` in `sweep_core.py` and Rust consts —
the Rust side EXPORTED to Python as the §1.1 module tuples
`SWEEP_CORE_FOLD_{INT,FLOAT}_FIELDS` (round-4: the export IS the mechanism;
without it the golden cannot read the Rust consts) — pinned equal by a
layout golden doing a literal tuple == tuple compare + the in-band ABI
version int):

```text
FOLD_INT_FIELDS (i64 ×15):  n_trades, net_pnl, gross_profit, gross_loss_abs,
    winners, losers, sum_mfe, sum_mae, sum_holding, max_holding,
    largest_loss (signed), max_consecutive_losses, max_consecutive_wins,
    max_dd_paisa, max_dd_bars
FOLD_FLOAT_FIELDS (f64 ×9): mean, m2, m3, m4, sum_sq_downside, p95, p5,
    max_dd_pct, ulcer
FLAGS (u8): bit0 = zero_trade      (n_trades == 0; float row zeros, unread)
            bit1 = python_required (2^52 equity envelope exceeded, an i128→i64
                   narrowing failure, or a non-finite value detected anywhere
                   in the fold — the Python full spine serves this mask;
                   float row unread; under the §1.3 SCAN-LAST ordering a
                   bit1 trip always precedes the drawdown/ulcer scan, so a
                   bit1 row counts NO scan — round-4)
            bits 2–7 = RESERVED, MUST BE ZERO (round-3): the Rust side
                   never sets them at this ABI version, and the decode
                   wrapper REFUSES any row whose flags byte has a reserved
                   bit set (`kernel_result_refused` reason
                   `reserved_flag_bits`) — a future-ABI or corrupted flags
                   byte must never be silently treated as a plain row and
                   fed to combine (the same silent-acceptance class the
                   rest of the refusal law closes; the ABI-version int
                   covers honest version skew, the per-row check makes the
                   flag byte self-validating).
```

**Zero-trade short-circuit is a MANDATED fold law (round-2 fix, perf):**
when the in-crate walk yields ZERO trades for a mask, `fold_block` emits the
`zero_trade` flag + zeroed rows and SKIPS the drawdown/ulcer scan AND every
float fold for that mask — the in-crate twin of today's pure-Python FLAT
shortcut (`rust_metrics.py` FLAT_DRAWDOWN_STATS, served with NO Rust
drawdown call; the repo's own comment measures ~66% of the profiled fixture
tail as zero-trade). This must be stated because a "natural" implementation
that calls `drawdown_stats_checked` unconditionally is BIT-IDENTICAL (the
flag makes the float row unread) and therefore invisible to burn-in / belt /
twin-run — yet it silently re-adds an O(n_bars) scan (~100–130 µs @48k,
~1.4 ms @555k) where today's tail costs 7.6 µs, collapsing the per-kept
headline toward ~1× on zero-trade-heavy tuples (including every min_hits=0
grid, which keeps EVERY conflict-free mask). **Enforcement is
BYTE-OBSERVABLE, not wall-clock (round-3 replacement of the round-2
"pinned by COST, not bytes" enforcement):** the round-2 remedy was a
wall-clock bench whose required sensitivity (catch a 2.4–2.7× per-mask
regression) sits BELOW the repo's shared-CI noise doctrine (the
`_o1_tiers.py` 3–10× shared ceilings exist precisely because tighter
ratios flake there; the strict 1.1× tier is M4-Pro-only), so as specified
it was either vacuous (a 2.6× regression passes a 3× ceiling) or destined
for the NON-required bench-m4pro lane — no merge-blocking net. The law is
therefore made observable in-band: `fold_block` returns `scans_run` (the
count of drawdown/ulcer scans executed, signature above), and the
MERGE-BLOCKING nets are deterministic — an in-crate cargo unit test
(all-zero-trade chunk ⇒ `scans_run == 0`; mixed chunk ⇒ `== n_plain_rows`)
plus a Python seam unit pin on the decoded value (§7.4/§7.5). This also
closes the residual output-equivalent cargo-mutants class the round-2 law
created: a mutant deleting the short-circuit guard is bit-identical in the
data buffers but flips `scans_run`, so it dies in the unit test rather
than surviving as "output-equivalent" (§7.6 — no new `exclude_re` entry
needed). The §7.7 wall-clock zero-trade pin is RETAINED but honestly
LANED: it runs under `BRUTEX_M4PRO_STRICT=1`/`BRUTEX_M4PRO_ABS=1` on the
bench-m4pro lane only (the `test_write_consumer_overlap_bench.py`
precedent — cross-arm wall-clock margins flip in shared-GHA noise), with
its ceiling sized to walk + the RETAINED Python per-mask tail (§9
zero-trade row), i.e. it catches the ~100–130 µs unconditional-scan
regression without false-failing a correct implementation. §9 carries the
dedicated zero-trade row.

The bit1 non-finite belt is DEFENCE-IN-DEPTH and stated as such (round-1
fix): through the public entry points the envelope gate — checked FIRST —
plausibly makes a non-finite fold value unreachable (bounded-integer inputs
⇒ finite returns ⇒ finite moments ≪ f64 range). To keep the cargo-mutants
zero-survivor gate honest rather than colliding with dead code, the check is
FACTORED into a tiny pure helper (`fold_guard_finite`) with direct in-crate
unit tests on synthetic non-finite inputs — a flipped `is_finite` mutant
dies in the helper's own tests, never as an unkillable survivor hiding
behind the envelope gate.

Python decodes with `array('q').frombytes` / `array('d').frombytes`
(memcpy-speed; `array('d')` reads return the identical IEEE-double bit
patterns). PyBytes-return precedent: `blake3_hash`. **Decode refusal law
(round-2 fix):** the decode wrapper validates, BEFORE yielding any row,
`n_rows := len(flags) == len(chunk)` AND `len(ints_le) == n_rows ×
FOLD_INT_STRIDE × 8` AND `len(floats_le) == n_rows × FOLD_FLOAT_STRIDE × 8`
— each failure a NAMED `kernel_result_refused` reason
(`row_count_mismatch`, `ints_length_mismatch`, `floats_length_mismatch`)
beside the existing ABI-version and stride refusals, **plus (round-3) the
per-row reserved-bits check: any flags byte with a bit in 2–7 set ⇒
`kernel_result_refused` reason `reserved_flag_bits` (the flags definition
above) — the refusal law otherwise validated shapes but silently accepted
an unknown flag as a plain row**; the consumption loop
additionally uses `zip(chunk, rows, strict=True)` as the belt (Python-3.12
baseline). Rationale: the three buffers admit INDEPENDENT inconsistency (a
stride-clean n−1-row block passes divisibility-by-itemsize, which is all
`array.frombytes` guards), and a bare `zip` stops at the shortest iterable
— silently DROPPING the chunk's trailing masks from the yield stream, the
exact silent-divergence class this design refuses everywhere else. §7.4's
decode vectors include a truncated-but-stride-clean block, a
cross-buffer length mismatch, and (round-3) a flags byte with a reserved
bit 2–7 set. Per-mask marshal
≈ 0.1–0.7 µs (measured target) — the ONLY per-value-churn-free transport of
the four candidates (judge 2's decisive point). Fallback encoding if decode
measures hot: plain `Vec<i64>`/`Vec<f64>` lists — both chunk-invariant.
**Endianness AND itemsize are EXPLICIT engagement preconditions, not
accidents** (round-1 fix; round-6 itemsize completion): the Rust side
encodes LE (`to_le_bytes`) while Python's
`array` decodes NATIVE byte order AND native C widths — CPython guarantees
`'q'` is a MINIMUM of 8 bytes, not exactly 8, so the decode law's hardcoded
`× 8` arithmetic rests on `array('q').itemsize == 8` and
`array('d').itemsize == 8`, which the round-1 fix pinned for byte ORDER but
not for width. The seam engages only when
`sys.byteorder == "little"` (warn-once reason `big_endian_host`) **AND
`array('q').itemsize == 8 == array('d').itemsize`** (checked once beside
it; a nonstandard-width host falls back to the Python spine, warn-once
reason `native_itemsize_mismatch` — on such a host the ×8 length checks and
`frombytes` would DISAGREE and rows would misparse rather than refuse, so
the precondition must refuse first), and the layout golden
pins `sys.byteorder == "little"` AND both itemsizes == 8 alongside the
field tuples — both implicit platform assumptions become tested
invariants. (Every actual target —
M4-Pro arm64, x86-64 CI — is LE with 8/8 itemsizes; this is completeness,
not exposure.) **PROBE SEAM SYMMETRY (fix-forward item 12):** byteorder is
read — like itemsize — through a PATCHABLE engagement-time probe
(`_host_byteorder()` beside the itemsize probe in `sweep_core.py`), NEVER
captured as a module-level constant: a module-level `sys.byteorder ==
"little"` capture would make the `big_endian_host` branch import-time-only
and un-branch-coverable on the LE CI host (the layout golden itself asserts
LE as an environment fact, so the fallback can never fire naturally there);
with the probe seam, §7.4's `big_endian_host` unit patches the probe and the
claimed every-fallback-reason branch coverage is mechanically true — the
round-6 itemsize mechanism applied to its previously half-covered sibling.

**Deliberately NOT in the FFI:** trades (never leave Rust), exit indices
(consumed in-crate by the drawdown fold — ONLY under the per-tuple
`bars_strictly_increasing` certificate, §4 row 7; an uncertified
duplicate-timestamp window falls back to today's spine whole-tuple),
`span_years`,
`benchmark_return_pct`, `tf_minutes`, timestamps, Decimal anything,
run-identity fields, audit anything, dedup anything. The kernel is
identity-blind (identity fields stay injected Python-side —
`reads/identity.md` gotcha 1) and clock/calendar/rounding-blind (paisa,
forced flags, session ordinals come from the existing Python
`_derive_kernel_inputs`). **`benchmark_return_pct` in particular is a
Python-combine input, defaulting exactly as today's
`compute_metrics(..., benchmark_return_pct=0.0)` on the sweep path — the
core never computes `information_ratio`** (resolves perfmax's silent
benchmark=0.0 FFI hole; the dependency is now stated AND stays in Python).

**Frozen-input LAWS (Verifier B structural findings, named as laws because
no grep gate can see a Rust violator):**

- **ONE-PRELOAD / HASH-WHAT-YOU-CONSUME LAW (F21):** `cmd_sweep_all`
  preloads each data slice ONCE, hashes THAT dict
  (`data_snapshot_hash` covers exactly the bars the sweep consumes), and
  serves the whole sweep from it. The Rust core owns ZERO lake I/O — no
  file opens, no readers, no caches; bars cross the FFI as DATA from the
  one preload, full stop. This is stated as a LAW, not a gate: the
  existing bypass gate is a Python-spelling grep that is structurally
  BLIND to a Rust-side reader, so review + the §3 statelessness contract
  (no files, no statics, no cursors) are the enforcement, and any future
  Rust I/O is a design-review refusal, never a lint miss.
- **PAISA-MINTING SEAM LAW (F22):** the float→paisa minting seam
  (`_ema_to_paisa`) routes float → `Decimal(str(v))` → ROUND_HALF_UP —
  REPR-MEDIATED, deliberately NOT `(v*100).round()`: the tie case
  `v = 1.005` mints 101 through the seam but 100 through the float
  round — Verifier B baseline-truth corpus (f) freezes those tie cases
  as the break-oracle. The seam is answer-key-INVISIBLE (the 400-case
  golden's inputs are already-snapped paisa ints), stays Python-single-
  source (`_derive_kernel_inputs`, §1.2), and is PORTED BY NAME only:
  any future Rust port must reproduce the repr-mediated Decimal HALF_UP
  semantics against corpus (f), never a float multiply-round.
- **CALENDAR-INPUT CONTRACT (F15/F18 + RT-40/E-18):** the NSE calendar
  (Muhurat sessions, the #1616 Muhurat-safe prev-day anchor) is a FROZEN
  INPUT the core never re-derives — paisa/forced/session ordinals arrive
  from the existing Python derivation. RT-40 disposition (Verifier B
  baseline-truth pack, ESCALATED to LIVE MAJOR pre-existing engine
  defect, then FIXED): `fib_anchor ∈ {rolling_weekly, weekly_static}`
  with a Muhurat/non-trading `today` raised a plain ValueError at
  `weekly_anchors.py:125` (static twin `:195`), propagating past the
  per-tuple `except SweepComboError` (sweep_parallel.py:891 at origin/main
  e8c708bd; was :595 at the authoring base) into a
  grid-wide SUPERVISOR_ABORT exit 2 / daemon GAVE_UP (captured
  2024-11-01 transcript). **The fix is landed on the engine branch
  `claude/data-edge-guard-fixes` (pending merge), and this design pins
  the POST-FIX ABSTAIN SHAPE as the parity truth the Rust twin must
  reproduce bit-identically: fib=None, Group-4 bits CLEARED, exactly one
  `stage1a.muhurat_weekly_anchor_ineligible` WARN, the tuple COMPLETES —
  never TUPLE_FAILED, never fabricated fib bits.** Until that branch
  merges, any tree still carrying the defect exhibits the pre-fix abort
  shape (grid-abort/exit-2, zero rows) — a PRE-EXISTING engine truth
  outside this conversion; the conversion never converts the abort into
  a silent per-tuple skip in either direction, and the twin-run gate
  runs against whichever shape the merged tree exhibits.

### 1.4 The per-tuple call shape (digest path, both flags ON)

```text
run_persisted_sweep (prologue/epilogue byte-unchanged)
  sentinel resume | plan_rank_memory | SWEEP_START audit        [Python, unchanged]
  bar_bits (Stage-1A, ResidentKernelCache)                      [unchanged]
  core = ResidentSweepCore(<derived columns>, bar_bits)         [once per tuple]
  rank = 0
  loop: (enum, pruned, rank, exhausted, hits_counted, masks) =
            core.enumerate_prefilter(bits, k, pairs, min_hits, rank, N, W)
        tallies += ; kept.extend(masks)
        until exhausted        # the IN-BAND flag (§1.3 exhaustion
                               # contract: next_rank == total_count),
                               # never a "returned empty"/"no progress"
                               # heuristic; the ≥1-mask progress law makes
                               # every non-exhausted call advance rank.
                               # On a mid-tuple ENUM LATCH (shadow
                               # mismatch) the remainder of the rank
                               # space rides today's Python enumeration
                               # per the §5 MID-TUPLE CONTINUATION
                               # CONTRACT (round-6) — the exit assert
                               # below is defined over the MIXED
                               # provenance.
  assert rank == n_masks and sum(enum) == n_masks
                               # the §1.3 TOTAL-RECONCILIATION belt
                               # (round-4) vs the Python-side
                               # count_combos(cfg.k, n_unique) the driver
                               # already computes (sweep_run.py:1300);
                               # mismatch ⇒ `enum_total_mismatch`
                               # fallback — spine serves, warn-once
  for chunk in chunks(kept, SWEEP_CORE_CHUNK):                  [Python drives]
        blk = core.fold_block(chunk, ...)
        rows = decode(blk)            # validates ABI ver + strides + n_rows
                                      # == len(chunk) + cross-buffer lengths
                                      # (§1.3 decode refusal law)
        for mask, row in zip(chunk, rows, strict=True):         [input order]
            if row.python_required: metrics = <today's spine, lazy ResidentTradeSim>
            elif row.zero_trade:    metrics = _empty_metrics(FLAT_DRAWDOWN_STATS)
            else:                   metrics = combine_metrics_from_folds(row, ...)
            burn-in? dual-oracle bit-compare (§5)
            yield (mask, metrics)                               [same generator contract]
  rank_combos / rank_survivor_columns                           [Python, unchanged]
  FINALIST BELT (PRE-PASS): ALL ranked finalists recomputed via
      the mechanised-pure oracle and bit-compared BEFORE the
      FIRST store append; + the BOUNDED-ARM SERVE RECONCILIATION
      (round-4 §5: retained rank columns vs rebuild, gate
      re-verdict, candidate-finalist served-object compares);
      + the 4-mask sampled population
      spot-check (§5)                                           [new, default ON]
  finalist projection / store append / flush / sidecar /
      sentinel (STRICTLY LAST) / audit                          [Python, byte-unchanged]
```

The generator (`_iter_mask_metrics_core`) satisfies the SAME contract as
`_iter_mask_metrics` (input-order yield, `_combo_error` fingerprints,
MemoryError bare), so all three consumers — batch `pairs`, bounded
`SurvivorColumns.append(mask, metrics)` + exact `owned_nbytes()` watermark,
and `_rebuild_bounded_finalists` (injected the same core-backed generator,
keeping its positional mask-equality belt) — are untouched. **A full
`ResultMetrics` is built per kept mask in Python combine**, so
`SurvivorColumns.append`'s signature, `passes_gates`,
`composite_term_inputs`, AND the 6th (overfit) gate's per-survivor
skew/kurtosis consumption (`ranking/__init__.py:555`;
`sweep_stream.py:188-191` stores skew/kurt columns when overfit is active)
are fed exactly as today — no column-based gate twins, no COMMON
duplication (resolves grounding's SurvivorColumns contradiction AND
perfmax's overfit-gate starvation, §14).

**Rank-arm shape:** `plan_rank_memory`'s batch-vs-bounded verdict is honored
UNCHANGED in Slices 0–2 (the generator feeds whichever arm the plan picked,
as today). Perfmax's "route the ON arm through the bounded accumulator
unconditionally" is recorded as an OPTIONAL Slice-3 memory lever, licensed
only by a dedicated plan-batch-vs-plan-bounded byte-equality pin on the core
arm (judge 2's condition) — not required for any slice's correctness.

---

## 2. What moves to Rust vs stays Python — per item

| Item | Where | Why (proof-first justification) |
|---|---|---|
| Mask enumeration (`iter_masks_k_of_bits`) | **Rust** | pure integer, lexicographic; exact-order list-vs-list property; kills 2.4 µs × C(60,k)/tuple — the k≥5/6 dominant term |
| `has_conflict` (26 pairs) | **Rust** (pairs marshalled IN) | `(mask & pair) == pair` integer-exact; `CONFLICT_PAIRS` stays single-sourced in Python |
| Stage-1B min-hits count | **Rust, fused; SKIPPED WHOLESALE at `min_hits == 0` (the §1.3 MIN-HITS SHORT-CIRCUIT LAW, round-6 — today's `if cfg.min_hits > 0` guard twin; in-band `hits_counted` is the deterministic net)** | same `count_one` predicate; fusing avoids materialising 5.5M–40M survivor Python ints; exact counting ⇒ kept set provably identical; an unconditional count would be byte-identical but a silent O(n_bars)/mask cost ADD on every min_hits=0 grid — the zero-trade-law hazard class on the enum entry point |
| Trade walk | **Rust (already)** — `simulate_one_core<W>` consumed in-crate | proven bit-identical (#563/#1182, 45,863/45,863); trades stop crossing the FFI (−42 µs marshal, −98 µs churn) |
| Drawdown/ulcer scan | **Rust (already)** — `drawdown_stats_checked` via the new `drawdown_stats_into` scratch seam | proven fold (#1186); fold order untouched; scratch reuse changes allocation only; existing pyfunction = thin allocating wrapper (tests/goldens/358 mutants UNMODIFIED); walker-native exit indices consumed ONLY under the `bars_strictly_increasing` certificate (§4 row 7 — uncertified ⇒ `ts_uncertified` whole-tuple fallback) |
| Integer trade aggregates, streaks, max-holding, largest-loss | **Rust fold** | exact i64 via i128 accumulate; narrowing failure ⇒ `python_required` (Python bigint semantics preserved by FALLBACK, never emulation) |
| Welford/Pébay moments, returns conversion, downside Σr² | **Rust fold** | sequential `+−×/` only; token-for-token port — of the correct ALGORITHM per construct: bare `+=` loops port as bare folds, the downside Σr² `builtins.sum()` ports as CPython 3.12's Neumaier compensation loop (§4 row 6); IEEE-identical on identical algorithm+order; differential-harness-provable |
| p95/p5 (sort + lerp) | **Rust fold** | STABLE `slice::sort_by(partial_cmp)` == stable Python `sorted` (−0.0/+0.0 ties keep input order in BOTH); lerp token-for-token; NaN structurally excluded + `python_required` belt |
| `total_return_pct`, CAGR pow + OverflowError clamp, skewness/kurtosis pow, sqrt-based std/sortino/downside_dev tails, `information_ratio` (+benchmark input), ALL `_safe_ratio` Nones, ALL gate boundaries, `//` floor-divs (expectancy, avg_*), clamps | **Python** (`combine_metrics_from_folds` — today's literal tokens) | the ENTIRE #1186 libm class + every None/boundary/sign/floor-div hazard stays in today's code — bit-identity by construction; the layer rule (§4) decides this, not per-field taste (resolves grounding's ad-hoc sharpe-vs-sortino split) |
| `ResultMetrics` construction | **Python** | downstream consumers (SurvivorColumns incl. overfit skew/kurt columns, rank, sidecar fsum, burn-in compare) consume today's objects unmodified |
| `passes_gates`/`composite_term_inputs`/rank | **Python** | bit-pinned shared internals; ~1.3 µs/mask amortised — no win worth the proof; the P3 gap-#12 reason-returning gate twin plugs into the SAME site later |
| Finalist projection (`ranked_combo_to_result_row`) | **Python oracle** | explicit #1182 non-goal; Decimal/`round_money` store bytes never re-derived in Rust; NOW additionally belt-verified (§5) |
| flush/sidecar (`math.fsum`)/mark/sentinel/audit | **Python** | resume/idempotence contract; ordering law untouched; 3 audit lines/tuple, engine-blind |
| Legacy no-digest dedup arm | **Python (today's loop, whole)** | per-mask eager run_id + Bloom/LMDB probes; the core serves the DIGEST path only (`reads/dedup.md` gotcha 2); `DigestPathNoDedup` stays the raising tripwire |
| `span_years`, paisa conversion, session/forced calendar, ts↔index | **Python** (once/tuple, passed in or kept out) | §11 COMMON single sources; kernel never re-derives |
| Zero-trade masks | **flag out; the fold SHORT-CIRCUITS after the walk (NO drawdown scan / NO float folds — the §1.3 mandated law); Python `_empty_metrics(FLAT_DRAWDOWN_STATS)`** | literal today's call — +0.0 signs Python-owned; the short-circuit is BYTE-OBSERVABLE via the in-band `scans_run` count (deterministic §7.4/§7.5 pins — the merge-blocking net) + the M4-Pro-laned §7.7 cost pin (round-3) — a bit-identical-but-unconditional drawdown call is a silent 10–20× cost regression on the ~66%-zero-trade tail, and it flips `scans_run` |

---

## 3. Batching + checkpoint / kill −9 semantics

**The durable unit stays the TUPLE. The FFI unit is a bounded in-memory
chunk strictly inside one tuple. The Rust core owns ZERO durable state and
ZERO cross-call state — no files, no statics, no caches, no cursors.**

- **Enumeration batching:** `enumerate_prefilter(start_rank, max_enumerated,
  max_work_cells)` ranges, unranked via the combinatorial number system
  (exact, order-preserving, stateless). The CNS unranking is retained over a
  resumable Rust-side cursor DELIBERATELY: a cursor is cross-call state,
  which the statelessness law (and the suffix-independence property class)
  forbids; the extra proof burden is paid with a proptest unranking
  round-trip (`rank → combo → rank`), a ranged-vs-monolithic equality
  property, and the list-equality oracle vs `itertools.islice` of the Python
  enumeration (resolves the judge-3 friction point by proof, not retreat).
  Dual work bounds: `max_enumerated ≈ clamp(2^28 // max(1, n_bars), 4096,
  2^20)` — the DIVISOR-floored repo idiom (round-5: the previous bare
  `2^28 / n_bars` transcription was exactly the expression class the
  round-4 fix (ii) below withdrew for the fold-side sibling; both sibling
  sizing expressions now carry the same `max(1, n_bars)` floor, and the
  §7.4 n_bars == 0 unit-pin mandate covers BOTH) AND
  `max_work_cells` default **2^28** (enumerated × n_bars, saturating — the
  perfmax graft, RE-SCALED after round-1 review: at the measured
  0.257 ns/bar·mask prefilter rate 2^28 cells ≈ 70 ms; the earlier 2^31 cap
  was copied from perfmax without re-scaling and would have held the GIL
  ~0.55 s). **GIL honesty:** fno-math's recorded convention holds the GIL
  for the FULL call (no `Python::detach`/`allow_threads` — the crate does
  not link libpython under the cargo-test build,
  `rust/fno-math/src/python.rs:297,439`), so the work cap IS the liveness
  mechanism, and it is sized to it: one FFI call ≈ tens of ms — the same
  order as today's `_TRADE_SIM_CHUNK_CELLS = 2^22` walk calls (~5–15 ms) —
  bounding Ctrl-C latency, in-process dashboard event-loop starvation, and
  #1291-adjacent liveness beats to **~0.1 s** granularity. Per-call FFI
  overhead is ~µs, so the smaller caps cost nothing measurable.
- **Metrics batching:** `fold_block` chunks of kept masks. Return is
  O(chunk) (24 scalars + 1 flag per mask), NOT O(trades) — chunk can grow
  past today's 256: default `SWEEP_CORE_CHUNK = 4096` masks (bounded return
  ≈ 800 KB), work-capped by **`masks × n_bars ≤ 2^24` cells** — the BINDING
  knob, re-scaled after round-1 review: a fold cell costs walk 1.28 +
  in-crate drawdown ~2.1–2.7 ns ≈ **3.4–4 ns sandbox (~10× a prefilter
  cell)**, so 2^24 ≈ 55–70 ms/call (effective chunk ~350 masks @48k bars,
  ~30 masks @555k). **PINNED-CONFIG QUALIFIER (Verifier B PERF
  RECONCILIATION, BINDING — §9.1):** the 1.28 ns/mask·bar walk figure is
  the UNPINNED, count-only rate; under the PRODUCTION config
  (`RAYON_NUM_THREADS=1`, sweep_parallel.py:842) the reconciled two-term
  law prices the walk at **≈6.2 ns/mask·bar + ≈4.1 µs/trade**, so a
  2^24-cell fold call is ~110–160 ms, not 55–70 ms. The fold cap default
  is therefore RE-DERIVED ON-BRANCH from the pinned rate before Slice 2
  merges (safe direction: LOWER the default cap — e.g. 2^23 — to keep
  nominal holds ≲100 ms; the caps are env-tunable and results are
  cap-invariant by the dual-knob matrix, so the re-size is liveness-only);
  the §7.7 GIL-hold ceiling is sized from the pinned rate, never the
  unpinned figure, and the unpinned 1.26–1.28 ns rate must NEVER enter
  campaign math. The withdrawn 2^30 figure would have held the GIL
  ~3.6–4.3 s per call — a 50–300× liveness regression vs today's 5–15 ms
  holds, contradicting this section's own claims; corrected. Both caps are
  env-tunable (`BRUTEX_SWEEP_CORE_WORK_CELLS` /
  `BRUTEX_SWEEP_CORE_ENUM_CELLS`, fail-loud parse) **with a documented
  upper CLAMP (round-2 fix): an enum-cells override is clamped to ≤ 2^30
  (~0.28 s at the 0.257 ns/cell prefilter rate) and a fold-cells override
  to ≤ 2^27 (~0.45–0.55 s at ~3.4–4 ns/cell), each clamp emitting one loud
  `work_cap_clamped` warn-once** — the fail-loud parse validates
  numeric-ness, not magnitude, so a typo'd `2**44` would otherwise hold the
  GIL for minutes on exactly the in-process dashboard arm this section
  flags as sensitive (the round-1 re-scale closed the DEFAULT hole; the
  clamp closes the OVERRIDE hole, the repo's clamp idiom). **AND a LOWER
  clamp + a guaranteed-progress law (round-3 — the round-2 clamp closed
  only the too-LARGE direction):** a numeric-but-degenerate override
  (`=0`, `=1`, or any value below one mask's work) passed the parse,
  missed the upper clamp, and produced a cap consumed before the first
  mask — under an undefined "until exhausted" driver contract that was
  either an infinite zero-progress busy loop on the watchdog-less
  sequential/in-process arm (this section's own honest-containment split)
  or a silently-EMPTY tuple (enumerated=0, kept=[]) persisted as real —
  the exact silent-divergence class the decode law refuses. Three-part
  fix: (i) both env overrides are ALSO lower-clamped to ≥ 2^16 cells at
  parse time (the same `work_cap_clamped` warn-once, with a `direction`
  field); (ii) guaranteed ≥ ONE mask of progress per non-exhausted call
  regardless of the cap, **with the ENFORCEMENT POINT named per entry
  point (round-4 — the round-3 wording attributed both to "the CORE",
  but `fold_block`'s FFI signature carries NO work-cap parameter and no
  cursor, and the §1.3 decode refusal law's `len(flags) == len(chunk)`
  structurally EXCLUDES an in-core partial-chunk fold):** the ENUM side
  is IN-CORE — `enumerate_prefilter` checks its caps AFTER each mask's
  work (the floor idiom of `sweep_run.py:203`), so `next_rank >
  start_rank` on every non-exhausted return; the FOLD side is
  DRIVER-enforced — the Python driver sizes every `fold_block` chunk as
  `max(1, cells // max(1, n_bars))`, the DIVISOR-floored repo idiom of
  `sweep_run.py:203-204` VERBATIM (the round-3 quotient-floored
  transcription `max(1, cells // n_bars)` ZeroDivisionErrors on an
  n_bars == 0 window and is withdrawn). This is a stated ASYMMETRY: R9's
  "dual-work-bounded" is an in-core backstop for enum but a driver
  convention for fold (the repo precedent — trade-sim chunks are sized
  driver-side today). **Round-6 correction of the round-4 containment
  sentence:** the previous claim that "an oversized chunk fed anyway is
  bounded by the loud `row_count_mismatch` refusal" was FACTUALLY WRONG
  for that failure mode — a CONFORMING core fed an oversized chunk
  returns `len(flags) == len(chunk)` with matching buffer lengths, so
  the decode refusal law (which validates return-vs-chunk CONSISTENCY
  only) never fires; the true exposure is LIVENESS — the GIL held for
  the full oversized fold (a wrong-n_bars sizing regression at 555k bars
  ⇒ multi-second-to-unbounded holds at the ~3.4–4 ns/cell rate) with
  only test-time nets (the §7.4 chunk-sizing unit + spy pins), while the
  SAME trust posture's env typos earned runtime clamps in rounds 2–3.
  The runtime backstop is therefore added IN-CORE at one-multiplication
  cost: **`fold_block` refuses the WHOLE chunk with
  `Err("fold_work_ceiling")` when `masks.len() × n_bars >
  FOLD_WORK_CEILING_CELLS = 2^28`** (2× the fold override's own ≤2^27
  upper clamp, so no clamp-legal configuration can ever reach it; a
  whole-chunk Err is fully compatible with the decode law — it excludes
  only PARTIAL progress). The seam maps that error string to the NAMED
  fallback reason `fold_work_ceiling` (whole-tuple fallback to today's
  spine, warn-once — production-unreachable by construction since the
  driver sizes chunks ≤ 2^24 cells; §7.5 bakes the in-crate
  oversized-chunk ⇒ Err unit, §7.4 the seam mapping). Divergence is
  still impossible either way; the ceiling converts a silent unbounded
  GIL hold into a loud bounded refusal — the design's own liveness
  doctrine applied to its last uncapped entry point. Today's
  `ResidentSweepSim` chunks are also driver-sized with no in-core cap (a
  pre-existing convention, stated); this new core does NOT inherit that
  gap. The chunk-sizing computation stays directly unit-pinned incl.
  n_bars == 0 (§7.4) — so even a hostile cap degrades to
  one-mask-per-call slowness, never a hang and never an empty result;
  (iii) exhaustion is DEFINED in-band (§1.3: the `exhausted` return flag,
  `next_rank == total_count`) with a driver-side `enum_no_progress`
  fallback belt for the structurally-unreachable no-progress return.
  §7.4/§7.7 gain degenerate-override probes (`=1` for both knobs ⇒
  clamped + warned + byte-identical completion) beside the round-2
  `2**44` upper probe. Cap-invariance
  of results is exactly what the dual-knob matrix pins — the matrix's
  ≥5 shapes now include one lower-clamped degenerate shape per knob. Per-mask folds are
  independent ⇒ chunk size provably result-invariant.
- **Dual-knob invariance matrix (perfmax graft):** batch-invariance is
  pinned across ≥5 `(max_enumerated, max_work_cells)` shapes for
  `enumerate_prefilter` AND ≥5 chunk sizes for `fold_block` — identical
  kept sets, tallies, SurvivorColumns bytes, ranked order.
  **CAP-RESPECTED PINS (round-4 — every round-3 liveness pin passed
  under a cap-IGNORING implementation: the matrix pins RESULTS, which a
  cap-ignoring core computes identically; ranged-vs-monolithic equality
  passes trivially when the first "ranged" call returns everything; the
  ≥1-progress unit is satisfied by enumerating ALL masks; and the sole
  distinguishing gate was an unlaned ~1.4× wall-clock bench sitting
  below the repo's shared-CI noise doctrine — the exact
  bit-identical-but-liveness-catastrophic class the round-3 `scans_run`
  fix closed for the sibling law, and the same in-band data already
  exists here):** every dual-knob-matrix call ADDITIONALLY asserts, from
  the return tuple's own fields, `next_rank − start_rank ≤
  max(1, max_enumerated)` AND `(next_rank − start_rank) × n_bars ≤
  max_work_cells + n_bars` (one-mask slop — caps are checked after each
  mask's work); in-crate cargo units bake EXACT expected `next_rank`
  values on CNS fixtures (a 1-cell/1-mask cap ⇒ `next_rank ==
  start_rank + 1`, a k-mask cap ⇒ exactly k advanced), so the
  deleted-bound-check and `>=`→`>` cap-compare mutants in sweep_core.rs
  die DETERMINISTICALLY (§7.5) instead of surviving the stated plan; the
  Python seam pins the fold-side twin — the chunk-sizing computation
  unit-tested directly (incl. n_bars == 0, no ZeroDivisionError) plus a
  spy pin that no chunk fed to `fold_block` ever exceeds it (§7.4). The
  §7.7 GIL-hold bench is DEMOTED to a secondary, honestly-LANED net
  (bench-m4pro strict lane; shared CI keeps only a catastrophic ≤ ~2 s
  backstop) — the deterministic in-band pins are the merge-blocking
  liveness net.
- **Worker partitioning:** unchanged — one tuple per worker process; the
  core is built inside `run_persisted_sweep` in whatever process runs the
  tuple. Masks are NEVER batched across tuples (would cross the
  checkpoint/audit/sentinel unit — `reads/parallel.md` §8). Under
  `sweep_parallel`, `RAYON_NUM_THREADS=1` governs; all new FP folds are
  scalar-sequential regardless of thread count; any future rayon use is
  restricted to order-preserving `IndexedParallelIterator` over INDEPENDENT
  masks (serial ≡ parallel by construction; `par_bridge` stays forbidden).
- **kill −9 (batch fully-durable-or-fully-redone, trivially):** at ANY kill
  point — before/inside/after any Rust call, worker or parent — the tuple's
  completion sentinel is absent/unpaired ⇒ FULL deterministic tuple redo
  with byte-identical artifacts **ON A HEALTHY BUILD (fix-forward item 3
  scoping — the condition the twin-run ship gate pins):** the core is
  stateless, `ResultsStore` starts empty per construction, flush atomically
  overwrites, and the sentinel is strictly last, so the healthy-build
  sentinel-absent full-redo proof is sound. On a genuinely-DIVERGENT build
  the redo bytes are ENGAGEMENT-HISTORY-DEPENDENT, stated not silent (the
  design's own disclosure doctrine): the burn-in window and autorevert
  latch are PROCESS-global, so a tuple first attempted in a fresh worker
  (burn-in catches the defect ⇒ oracle/spine bytes) and redone after
  kill −9 in a REUSED worker whose window already closed on other tuples'
  masks (core serves unchecked; the belt catches finalists, escaped-
  residual-class values differ) can persist DIFFERENT bytes across
  attempts — every completed redo is internally consistent and
  belt-verified. **STORE-PRELOAD CORRECTION (Verifier B checkpoint-1
  audit C5 — the previous parenthetical here claimed "`ResultsStore`
  starts empty / the same-run_id conflict tripwire cannot trip across a
  fresh in-memory store"; BOTH halves are FALSE against production):**
  `load_or_create_store` PRE-LOADS the existing Parquet into the store
  when the file exists (`sweep_orchestrator.py:737-758` — "pre-loaded if
  the file exists", the idempotence keystone: a resume re-flushes the
  SAME rows instead of overwriting good results with an empty store), so
  a redo whose killed attempt already flushed does NOT start from an
  empty store. The REAL shape, which is the DESIGNED behavior: on a
  redo-after-kill over a flushed-but-unsentineled file, a
  DIVERGENT-build recompute that appends a different result under an
  already-persisted run_id hits the LOUD same-run_id append-conflict
  abort (`results_store.conflict` ERROR + `ResultsStoreError`,
  results_store.py:487-490 — the F2 law; "one identity can only ever
  mean one result", results_store.py:57) — divergence is REFUSED loudly,
  never silently persisted, on exactly the kill-after-flush point. The
  different-bytes-across-attempts class above is therefore confined to
  kill points where NO flush landed (file absent ⇒ genuinely fresh
  store) or where the redo path never revisits the persisted run_ids;
  where the store pre-loads, the tripwire — far from "cannot trip" — is
  the mechanism that CATCHES the divergent build (and the §7.2
  kill-after-flush REDO cell is the proof a HEALTHY build never trips
  it). The class is pre-existing in kind (#1531's rust_metrics
  carries the identical process-global-latch shape)
  (DEC-MARK-MANY-SENTINEL-001). There is no
  partial-batch durable state: the batch is RAM. A killed worker →
  BrokenProcessPool → #1291 quarantine/stall semantics unchanged (the parent
  watchdog is cross-process; chunking only SHORTENS in-tuple no-signal
  windows). A killed parent loses only unapplied outcomes —
  contiguous-clean-prefix checkpoint + sentinels re-derive on resume.
  `start_rank`/`max_enumerated` are in-memory only and MUST NEVER become a
  durable resume key (design pin; gap-#3 interaction, §11).
  **KILL-AFTER-FLUSH REDO CELL (Verifier B F2, RT-09 — a MANDATORY twin-run
  ship-gate cell, not just clean twins):** because the recompute must be
  bit-identical for the redo to succeed at all — a non-bit-identical
  recompute converts crash-REDO into a PERMANENT grid-wide append-conflict
  abort (`ResultsStoreError` is not a per-tuple `SweepComboError`, so the
  conflict raise is not quarantined) — the §7.2 twin-run proof INCLUDES a
  kill-after-flush REDO cell on the CORE arm: kill −9 strictly AFTER
  `flush()` but BEFORE the sentinel, resume, and assert (a) clean redo with
  byte-identical artifacts and (b) the audit chain reads exactly
  `[SUPERVISOR_START, TUPLE_SKIPPED_RESUME, TUPLE_DONE, SUPERVISOR_COMPLETE]`
  — the Verifier B baseline corpus (b) transcript is the frozen oracle.
- **ENUMERATION-ORDER / CHECKPOINT IDENTITY PIN (Verifier B F1 — MANDATORY
  because the CNS enumerator is order-UNRANKED):** the durable checkpoint is
  POSITIONAL (a tuple index), and `grid_identity` pins the grid AXES, not
  the enumeration ALGORITHM — so a Rust-side tuple-enumeration reorder
  would silently PREFIX-SKIP never-run tuples on resume
  (sweep_supervisor.py:710). Two belts, both required: (a) the core's
  MASK-level order is pinned lexicographic ≡ `iter_masks` by the §7.3
  list-equality property + CNS round-trip (existing); (b) NEW
  INDEX→TUPLE-DIGEST GOLDEN — a frozen fixture grid golden asserting, for
  every checkpoint index i, the tuple digest at position i is IDENTICAL on
  the OFF and ON arms (the positional-checkpoint ↔ tuple-digest map is the
  pinned artifact), so any enumeration/ordering drift breaks a golden
  instead of prefix-skipping silently. The tuple-ORDER authority stays the
  untouched Python grid iteration (the core never orders tuples — stated);
  the golden exists so that invariant is TESTED, not assumed.
- **Kernel panic** (`panic="abort"`): SIGABRT = worker death → pool-broken
  quarantine. **PANIC-RECOVERY RE-ROUTE (Verifier B checkpoint-1 audit
  C6 — the previous routing to "#1295's crash-loop breaker" is
  WITHDRAWN: that breaker is #1295's G12 checkpoint-CORRUPTION counter
  [independently verified TRUE by Verifier B at 871b3129 —
  sweep_quarantine.py:84/:113],
  structurally blind to routed crash-loop classes — it counts checkpoint
  corruption events, not worker deaths, so it CANNOT fire for a panic;
  #1295 is additionally UNMERGED, §11 F7).** The mechanisms that
  ACTUALLY fire for a repeated panic, verified in-tree:
  (a) *parallel lane* — a SIGABRT'd worker breaks the pool; the parent
  catches `BrokenProcessPool` distinctly and QUARANTINES the poison
  tuple(s) durably + reversibly (`_quarantine_broken_pool`,
  sweep_parallel.py:1340-1343 + :1297-1299; sidecar
  `<root>/.sweep_quarantine.json`, sweep_parallel.py:630; finished
  siblings drained, then the run fails LOUDLY with
  `SweepWorkerPoolBrokenError` naming tuple(s) + sidecar + remediation —
  DEC-SWEEP-STALL-OOM-GUARDS-001; a resume skips the quarantined
  tuple(s) as the DISTINCT `tuples_quarantined_prior` counter, so a
  repeated panic converges to visible per-tuple loss, never a loop);
  a panic that surfaces as a caught EXCEPTION (the seam's mapped
  `kernel_panic_exception` below) instead rides the per-tuple
  `SweepComboError` catch → failed marker → `TUPLE_FAILED` audit line
  (sweep_parallel.py:891-897 catch; `SUPERVISOR_ACTION_TUPLE_FAILED`
  append at sweep_parallel.py:1585-1596 — skip + continue);
  (b) *daemon lane* — under `brutex sweep-supervisor` a sequential-arm
  SIGABRT is a child crash; the `ProcessSupervisor` restart policy does
  the crash-loop accounting (consecutive-crash streak vs
  `policy.max_restarts`, uptime-reset, backoff —
  `orchestrator/supervisor.py:530-575`) and terminally GIVES UP loudly
  (`SupervisorOutcome.GAVE_UP`, supervisor.py:295/:564, with the
  CRITICAL `SUPERVISOR_GAVE_UP` Telegram alert, telegram.py:161 —
  DEC-SUPERVISOR-CRASHLOOP-ALERT-001; consumed by the sweep-daemon CLI
  at cli.py:7180).
  **Remediation lever #1 is RE-HOMED accordingly:** the runbook line
  "`BRUTEX_SWEEP_RUST_CORE=0` / `BRUTEX_SWEEP_RUST_ENUM=0` is
  remediation lever #1 for repeated worker SIGABRTs" attaches to the
  `SweepWorkerPoolBrokenError` quarantine runbook (the #1608 sidecar
  remediation text — the operator surface a broken-pool incident
  actually lands on) AND to the supervisor GAVE_UP alert/runbook for the
  daemon lane — NOT to #1295's corruption counter. Every later "§3
  crash-loop-breaker runbook" reference in this document reads as THIS
  re-homed pair (grounding/perfmax
  graft; NO auto-env-flip — the open_questions Q5 stance: auto-downgrade
  trades engine-choice determinism for unattended survival and is an
  operator policy call, explicitly not taken here). **Honest containment
  split (round-1 fix):** the quarantine/breaker story covers the
  `sweep_parallel` WORKER arm only. On the sequential / in-process arm
  (workers=1, or the dashboard-hosted sweep job) a SIGABRT kills the WHOLE
  hosting process — uvicorn dashboard included — with no quarantine and no
  breaker; the kill switch is PREVENTIVE, not reactive, there. This exposure
  class is pre-existing (ResidentSweepSim/`sweep_drawdown_stats` live in the
  same `panic=abort` crate, default-ON), but this conversion adds new panic
  surface, so the split is stated here AND in the breaker-runbook addition
  verbatim. Structural prevention: boundary validation in pure Result cores;
  checked-u128 CNS binomial arithmetic (never unchecked — §1.3); no
  unwrap/expect/index/`as`-narrowing in method bodies; i128 accumulators +
  envelope flags make hot FOLD loops provably panic-free; fuzz targets on
  both entry points.
  **PANIC POLICY, explicit (Verifier B F4 + F23 — the error-taxonomy
  ruling the spec previously left implicit):**
  (a) *Chosen policy:* the crate KEEPS `panic="abort"` — a Rust panic is a
  SIGABRT process death, and `pyo3_runtime.PanicException` (a
  **BaseException** subclass that would bypass BOTH the seam's
  `except Exception` fallback ladder AND #1291's quarantine taxonomy) is
  therefore STRUCTURALLY UNCONSTRUCTIBLE — the abort precedes any unwind.
  The profile is grep-pinned (§4 row 14's committed-profile pin covers
  `panic="abort"`), so the unreachable-PanicException argument is a tested
  invariant, not an assumption.
  (b) *Defence-in-depth seam catch (covers a future unwind-profile change
  or a PanicException raised by ANY other pyo3 extension on the call
  path):* the seam's FFI-call sites catch `pyo3_runtime.PanicException`
  EXPLICITLY (an `import`-guarded name — absent module ⇒ no-op catch),
  map it into the per-tuple quarantine taxonomy as a `SweepComboError`
  (⇒ TUPLE_FAILED, the #1291 per-tuple skip) with the DISTINCT fallback/
  alert reason `kernel_panic_exception` (one ERROR event + process latch,
  the autorevert shape) — so a deterministic panic can never bypass
  `except Exception` into a resume livelock with the abort alert unsent.
  (c) *Supervised-daemon parity (F23):* under `brutex sweep-supervisor`
  (relaunch on ANY nonzero exit) a deterministic SIGABRT tuple on the
  SEQUENTIAL arm is a relaunch crash-loop → the supervisor's
  restart-cap accounting (orchestrator/supervisor.py:530-575, NOT
  #1295's corruption counter — audit C6) → GAVE_UP with every
  LATER tuple permanently uncomputed — stated honestly: mapping restores
  per-tuple-skip parity ONLY where an exception (not an abort) surfaces;
  an abort cannot be caught. On the PARALLEL arm the pool quarantine
  already yields per-tuple-skip parity (worker dies, tuple quarantined,
  grid continues). The sequential-arm remediation is the runbook lever #1
  (`BRUTEX_SWEEP_RUST_CORE=0`); a distinct per-tuple-skip EXIT CODE that
  the supervisor treats differently from crash exits is recorded as a
  NAMED FOLLOW-UP for the supervisor's owner (P2 — `sweep_supervisor.py`
  is a not-ours file, §11), never silently claimed here.

### 3.1 Audit-chain law on the Rust-core arm (Verifier B checkpoint-1 audit C42)

How the core arm preserves the Golden-Rule-#8 audit laws — stated as
design LAW, each leg verified against the shipping mechanism:

- **Buffer-and-replay; Python owns the JSONL chain.** The Rust core
  returns COUNTERS/EVENT DATA only (`scans_run`, engagement/fallback
  reasons, totals — §1.3); it NEVER writes an audit line, computes an
  `entry_hash`, or holds the chain file. Audit entries are buffered by
  the PYTHON driver and appended at the EXISTING per-tuple cadence
  (3 audit lines/tuple, engine-blind payloads — the §13 invariant): on
  the parallel lane this is literally today's shipping shape — workers
  collect into `BufferedAuditCollector` (`orchestrator/audit.py:683`;
  "the worker never opens the real chain", sweep_parallel.py:865-868)
  and the parent replays "real chain FIRST (in enumeration order)"
  (sweep_parallel.py:1578-1580) — the core arm changes NOTHING about
  who appends, when, or in what order.
- **NaN sanitization BEFORE payload serialization (the allow_nan=False
  law).** The chain's canonical payload serializer already REFUSES
  non-finite floats (`_canonical_json`, `orchestrator/audit.py:176-203`
  — `allow_nan=False`, a NaN/Infinity payload raises instead of writing
  invalid JSON), mirroring the engine's canonical-JSON sites
  (stage1a.py:1020-1046, sweep_cache.py:356,
  DEC-RUN-IDENTITY-GUARD-PARITY-001). The core arm therefore MUST
  sanitize before the boundary: any core-returned value that can be
  non-finite is either structurally finite by the §1.3 envelope law
  (bit1 `python_required` trips on any non-finite fold value — the
  float row is unread) or is EXCLUDED from audit payloads; audit
  payloads stay engine-blind (§13), so no raw kernel float ever reaches
  `_canonical_json` unvetted. A defect here fails LOUDLY at append time
  (the serializer raise), never a silently-poisoned chain.
- **TWO hash algorithms, TWO purposes — never conflated.** The audit
  chain's integrity hash is **SHA-256** (`entry_hash = sha256(seq ‖
  prev_hash ‖ ts_utc ‖ actor ‖ action ‖ subject ‖
  canonical_json(payload))`, `orchestrator/audit.py:19-22` +
  `compute_hash`, audit.py:270-283) — tamper-evidence for the
  hash-chained JSONL (Golden Rule #8; breaking the chain fails CI). The
  run-identity hash is **blake3** (the Golden-Rule-#4 formula;
  `fno_math.blake3_hash ≡ blake3` pinned by
  DEC-RUN-IDENTITY-GUARD-PARITY-001) — reproducibility keying of runs
  and stores. The conversion touches NEITHER algorithm, and no design
  text may cite one where the other is meant: chain integrity = SHA-256,
  run/dedup identity = blake3.

---

## 4. Bit-identity strategy per float operation (the load-bearing section)

**The governing rule, stated once and applied uniformly (review law):**
*a float operation may move to Rust ONLY if it is a correctly-rounded
IEEE-754 primitive (`+ − × /`; `sqrt` qualifies but see below) applied in an
operand order the port reproduces token-for-token, in the FOLD layer.
Everything in the VERDICT layer — libm `pow`, semantic None, floor-division
sign, Decimal, gate comparisons, and ALL sqrt-derived verdict scalars —
stays in Python.* The layer boundary (fold vs verdict), not per-field taste,
decides placement: `sqrt` is correctly rounded in both languages, but
std_dev/sortino/downside_dev sqrts live in the verdict layer and therefore
stay Python; the ONE Rust sqrt is the pre-existing, already-proven ulcer
sqrt inside `drawdown_stats_checked` (reused, not written). This single rule
replaces grounding's waffling SPEC row 3 and its ad-hoc sharpe/sortino
split (§14).

| # | Operation | Resolution |
|---|---|---|
| 1 | Integer aggregates, `//` floor-divs (`expectancy = net_pnl // n`, avg_*) | **Python combine** from Rust-returned exact i64 sums — NO Rust floor-div-vs-truncation hazard exists anywhere in this design. Rust sums use i128 accumulators; failed i64 narrowing ⇒ `python_required` (Python bigint path serves — behaviour by fallback, never emulation). **`div_euclid` NAMED in the fold contract (Verifier B F3; RT-01 downgraded MAJOR→MINOR/dormant — expectancy_paisa is NOT in blake3 run identity and is Python-single-sourced today, and trading-core already uses `div_euclid`):** Python `//` FLOORS while Rust `/` TRUNCATES (`−7 // 2 == −4` vs Rust `−7 / 2 == −3` — verified, a 1-paisa fork on ~half of losing combos), so IF any future change ever moves a floor-div into Rust it MUST be `i64::div_euclid`, never `/`; the §7.4 negative-numerator boundary dual is the F3 golden cell (a negative-`net_pnl` expectancy/avg_* fixture pinned bit-exact on both arms), kept even though the current placement makes the hazard unreachable — the cell is the break-oracle for the forbidden port. |
| 2 | `total_return_pct = 100.0 * net_pnl / start_equity` | **Python combine** — needs only `net_pnl` (exact int). |
| 3 | CAGR `growth ** (1.0/span_years)` + try/except OverflowError→inf + `_CAGR_CAP_PCT` + −100 clamp + `end_equity < 1` ruin branch; `_skewness` `math.pow(m2_n, 1.5)`; kurtosis | **Python combine** — the #1186 MUST-STAY-PYTHON class honored by construction. Rust returns `net_pnl` (→ end_equity) and raw `m2, m3, m4`; Python runs the EXISTING `_skewness`/`_excess_kurtosis`/CAGR tokens. `calmar`/`mar` follow automatically. Zero cross-language `pow` anywhere; zero per-platform libm-equality claim needed. |
| 4 | Welford/Pébay moments | **Rust fold**: exact update expressions, exact operand order, strictly sequential left-to-right over trades; `(n*n − 3*n + 3)` in integer then converted (exact for reachable n); returns raw `(mean, m2, m3, m4)`. Python constructs `Moments(...)` and uses the EXISTING accessors — shared code, not a port. Differential harness ≥10⁶ adversarial streams, `struct.pack('<d')` per moment. |
| 5 | `returns[i] = pnl/start` | **Rust** `(pnl as f64) / (start as f64)`. Within the 2^52 envelope both int→f64 conversions are exact, so Rust's correctly-rounded division == CPython's int/int true division bit-for-bit. Envelope CHECKED IN THE FOLD (start + Σ\|pnl\| via i128); outside ⇒ `python_required`. Belt: any non-finite intermediate ⇒ `python_required` (closes riskmin's missing-belt gap). |
| 6 | downside Σr² (`sum(r * r for r in downside)`, compute.py:703) | **Rust fold porting CPython 3.12's builtin-`sum()` float fast path — NEUMAIER COMPENSATED summation (CPython gh-100425), NOT a naive left fold** (round-1 MAJOR fix: the repo pins Python ≥3.12, where `builtins.sum()` over floats runs the compensated loop; empirically a naive sequential fold bit-differs on ordinary 1000-value squared-return streams in the last ulp and on `[1e16, 1.0, −1e16]` by 1.0-vs-0.0 — a naive port would mismatch the dual-oracle burn-in on essentially the first kept mask of every process and permanently autorevert). The Neumaier loop is itself deterministic IEEE `+/−`, `abs`, compare arithmetic, so the token-for-token rule still applies — applied to CPython's compensation loop (running `f_result`, compensation `c`, the `|f_result| >= |x|` branch, and the **GUARDED final add — CPython does NOT unconditionally return `f_result + c`: the final add is `if (c && isfinite(c)) f_result += c;` (Python/bltinmodule.c, gh-100425 — the guard preserves a −0.0 result's sign and keeps an inf/overflowed sum from becoming NaN). The port reproduces the GUARDED form token-for-token from CPython SOURCE, never from this table's paraphrase (round-5: the previous token list omitted the guard). Provable value-invisibility FOR THIS FOLD, stated so the omission can never silently become load-bearing: summands are squares `r·r` of strictly-negative returns — non-negative, so `f_result >= +0.0` (never −0.0), `c` starts +0.0 and its updates cannot produce −0.0, and a non-finite `c` is excluded by the fold's bit1 finite/envelope belt — so guarded vs unconditional is bit-identical HERE; a future second builtins-sum port over SIGN-MIXED data would NOT enjoy that proof, which is exactly why the guard is in the enumerated tokens and on the per-expression review checklist**); squares accumulated in trade order matching the genexpr; strict `r < 0.0` with the identical operator on the identical bit value (−0.0 excluded in both); the fold returns the FINALISED compensated sum; final `sqrt(sum/n)` **Python combine**. Differential vectors include compensation-triggering streams (§7.5). **INT-START TRANSITION enumerated (fix-forward item 2 — the row-6 standard applied to row 6 itself):** CPython's `builtins.sum()` starts at `start=0` in the INT fast path; the FIRST float item is consumed via `PyNumber_Add` OUTSIDE the compensation loop (the int→float transition, Python/bltinmodule.c gh-100425 region) BEFORE the Neumaier loop begins — i.e. `f_result` is seeded `0 + x₀` with `c = 0.0` uninitialised-equivalent. Value-invisibility proof FOR THIS FOLD: the summands are non-negative squares, and `float(0) + x₀ == x₀` bit-exactly for every finite f64, so seeding `f_result = x₀, c = 0.0` (the natural Rust port) is bit-identical to the transition path HERE; a future sign-mixed or non-zero-start port would NOT enjoy that proof — the transition is in the enumerated tokens and on the per-expression review checklist, and the porting authority remains CPython SOURCE, never this paraphrase. |
| 7 | Drawdown family + ulcer | **Reuse `drawdown_stats_checked` UNCHANGED in-crate** (2^52 pre-guard enforced by the fold's own envelope flag first). The per-BAR sequential `sum_sq_pct` f64 fold stays per-bar — equal-value runs NEVER collapsed into multiplication (k×a ≠ a+a+…+a). Scratch reuse (Slice 3) resets only touched indices — allocation change only, fold order byte-identical, property-pinned. **Exit-index provenance is gated on the SAME per-tuple `bars_strictly_increasing` certificate today's two-arm contract uses** (round-1 MAJOR fix; `rust_trade_sim.py:340` / `exit_index.py`): the core consumes the walker's NATIVE exit indices only when the window certifies strictly-increasing timestamps. On a duplicate-timestamp window today's spine WITHHOLDS native indices and the metrics tail demotes to the last-wins `build_ts_index` dict — whose resolved index can differ from the walker's true exit bar — so native-index consumption there would bit-diverge max_dd_paisa/pct/bars/ulcer from the Python spine and trip burn-in/belt/twin-run into permanent autorevert. The certificate is checked ONCE per tuple at core construction; an UNCERTIFIED window falls back to today's spine WHOLE-TUPLE (fallback reason `ts_uncertified`, warn-once) — never a divergent fold, and the §7.3 duplicate-timestamp property passes by construction (both arms serve the identical Python-spine bytes there). The third `_exit_pairs` arm (an exit_ts absent from the bar index → the Python tail's exact `build_equity_curve` ValueError feeding `_combo_error`'s fingerprint) is structurally unreachable on the certified core path (walker indices are bar indices by construction) and behaves as today on the fallback spine. The drawdown fold is NOT invoked at all for a zero-trade mask — the §1.3 short-circuit law (round-2): the scan is O(n_bars) regardless of the exits list, so an unconditional call is bit-safe but cost-catastrophic on the zero-trade tail. |
| 8 | Round-half-even / Decimal | int→f64 conversions round-half-even in both languages; `round_money`(ROUND_HALF_UP, Decimal) appears ONLY in the finalist projector (Python, untouched). Review assertion: the core never re-derives a money display value. |
| 9 | None vs NaN | Rust NEVER encodes a None: every Optional metric is a **Python verdict** (`_safe_ratio` literal). Fold columns are total functions except behind the two flags. Burn-in/belt compare None-bits exactly. |
| 10 | Signed zeros | Zero-trade rows come from the literal Python `_empty_metrics` (+0.0 literals). Fold sums can produce −0.0 only through f64 addition of signed zeros — identical sequences produce identical signs in both languages; the `struct.pack('<d')` ↔ `f64::to_le_bytes` compare (which distinguishes ±0.0) is the net, exactly as #1531. |
| 11 | Sort stability | Python `sorted` is stable; Rust `slice::sort_by` is STABLE (never `sort_unstable_by` — the −0.0-tie bit-pattern hazard both riskmin and perfmax glossed is closed structurally, then pinned by an adversarial −0.0-tie property). `partial_cmp` on finite floats ranks −0.0 == +0.0 Equal in both ⇒ tie order = input order in both ⇒ identical VALUE sequence incl. zero signs at lerp endpoints. NaN structurally excluded (finite-by-envelope + `python_required` belt); comparator returns Ordering under a validated precondition — no unwrap. |
| 12 | Percentile lerp | token-for-token FROM SOURCE (`compute.py:272-280`), never this paraphrase (fix-forward item 2 — the row-6 round-5 standard applied here): the REAL `percentile` has TWO branches the lerp paraphrase omits, both enumerated WITH their value-invisibility proofs so the omission can never silently become load-bearing — (a) `len(sorted_values) == 1` early return (single-element: `lo ≡ hi ≡ the element`, and `lo*1.0 + lo*0.0` reproduces even a −0.0 endpoint bit-exactly, so an always-lerp port is bit-identical); (b) `lower == upper` exact-index return (integral rank ⇒ `f == 0.0`, `lo ≡ hi`, same identity; returns from int-pnl/positive-start streams can never be −0.0 anyway). Then the lerp: `rank = pct/100*(n−1)`; floor/ceil; `lo*(1−f) + hi*f` — `+−×/` only; f64 floor/ceil exact; property-pinned vs the real `_percentile` (which is why no divergence is reachable — the branches are enumerated for the review checklist, not because a defect exists). |
| 13 | Strict `>`/`>=`/`<` boundaries (`hits >= min_hits`, `end_equity < 1`, `r < 0.0`, `std == 0.0`, gates) | `min_hits` compare is INTEGER in Rust (exact); every FLOAT boundary is Python. Each boundary: exact-AT-threshold dual-arm test + mutation-killing test (#1182 (e) convention). |
| 14 | FMA / auto-vectorization | rustc stable emits no `contract`/`reassoc` fast-math flags; `mul_add` is the only FMA door and is SOURCE-PINNED ABSENT — the #1186 grep pin extends to `sweep_core.rs`; CI grep-pin refuses `mul_add`, `fast-math`, `ffast-math`, `reassoc`, and any new `-C target-feature`/RUSTFLAGS in the crate + workflow files; the committed release profile is unchanged. FP folds are scalar sequential loops (no reassoc flag ⇒ LLVM cannot vectorise the reductions); INTEGER loop auto-vectorisation is allowed (bit-exact). Cross-arch honesty: identity is proven per-build on the RUNNING host by burn-in/belt/twin-run, never asserted per-platform. |
| 15 | `math.fsum` sidecar, `span_years`, `benchmark_return_pct` | Python, untouched. sidecar folds the Python-side sharpe column exactly as today; `span_years` computed once per tuple (`sweep_driver.py:511` expression) and consumed only by Python combine — never crosses the FFI; `benchmark_return_pct` likewise (see §1.3). |
| 16 | Enumeration/conflict/hits | pure integers — no float surface. |

**Summation-algorithm law (added after round-1 hostile review):** "identical
operand sequence" presupposes the identical ALGORITHM, and CPython 3.12's
`builtins.sum()` over floats is Neumaier-COMPENSATED (gh-100425), not a bare
left fold — the round-1 review proved a naive port of row 6 bit-diverges on
routine data. Therefore every Python summation construct in the moved layer
is CLASSIFIED by its exact CPython algorithm before porting: bare `+=` loops
(Welford/Pébay, streaks, integer aggregates) port as bare folds; the ONE
`builtins.sum()` call in the fold layer (downside Σr², row 6 — grep-verified
the only one) ports as CPython's Neumaier loop token-for-token — INCLUDING
the conditional final add `if (c && isfinite(c)) f_result += c;` (round-5:
the porting authority is CPython SOURCE, never a paraphrased token list;
row 6 states the guard and its non-negative-summand value-invisibility
proof); `math.fsum`
(Shewchuk) is NEVER ported — it stays Python (row 15). The classification is
a per-expression review-checklist item and a CI grep pin (any new
`builtins.sum`/`fsum` in the fold layer fails review).

**CPython-version pin on the row-6 port (Verifier A round-1 fold):** the
Neumaier port is frozen against CPython **3.12's** `builtins.sum()` source
(the repo pins Python ≥3.12); if a later CPython minor (3.13+) changes
`sum()`'s float summation, the PYTHON oracle moves while the Rust port stays
frozen — the burn-in/belt compare would then mismatch and SILENTLY AUTOREVERT
the kernel for every process (perf-only failure mode: bytes stay correct via
the oracle, the core arm just quietly dies). The shipping wrapper therefore
carries a `sys.version_info` ceiling guard (engagement refused/warned on an
unvalidated CPython minor) OR a documented loud-degrade note on the
autorevert event naming sum-algorithm drift as a first-check cause; the
Slice-4 flip ADR must present whichever shipped (§6 Slice-4 obligation (d)).

**Why mechanically provable:** the Rust float outputs are 9 columns, each a
sequential fold of correctly-rounded primitives over inputs that are
themselves exact integer conversions. "Identical operand sequence ⇒
identical bits" is a theorem for `+−×/` under IEEE-754 round-to-nearest-even
in both languages; the proof obligations are (i) identical ALGORITHM (the
summation-algorithm law above — the round-1 review showed this obligation
was silently assumed), (ii) identical operand order (token-for-token port +
per-expression review checklist + CPython-generated differential oracle
incl. compensation-triggering vectors) and (iii) no compiler reordering
(#14). No empirical per-platform libm-equality claim exists anywhere in
this design.

---

## 5. Safety pattern (ADR-0017 / #1531 lineage, extended)

- **Kill switches:** `BRUTEX_SWEEP_RUST_ENUM` (Slice 1) and
  `BRUTEX_SWEEP_RUST_CORE` (Slice 2), both via the imported
  `env_flag_tristate` vocabulary (`_TRUTHY`/`_FALSY` from `sweep_lookahead`,
  never copied); unrecognised value ⇒ FAIL-SAFE OFF + warn-once. Read ONCE
  per `run_persisted_sweep` invocation (a structural loop-shape decision;
  parallel workers read their own env; the documented, results-identical
  delta vs rust_metrics' per-call read is stated in the module docstring and
  test-pinned). Both default OFF **as designed here**; each flips default-ON
  in its own later ADR (the #1186→#1532 two-step). **RESOLVED (2026-07-24 —
  the design's prediction, settled):** the per-slice two-step never ran;
  `BRUTEX_SWEEP_RUST_ENUM` was flipped default-ON wholesale by
  DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215), the operator
  RUST-O(1)-EVERYWHERE directive, with `=0` as the explicit kill switch and
  every safety layer named below untouched. This document is the
  design-of-record and keeps its as-designed statements; the LIVE default is
  #2215's. A third escape hatch
  `BRUTEX_SWEEP_RUST_CORE_BELT` (default ON) governs the finalist belt +
  the sampled population spot-check. **Its parse policy is the INVERSE fail
  direction, stated explicitly (round-1 fix — the uniform "unrecognised ⇒
  OFF" sentence above must NOT be applied to it):** the belt is a default-ON
  SAFETY mechanism, so fail-safe means ON — `_TRUTHY` → ON, an explicit
  `_FALSY` → OFF (the ONLY disable), unset/empty → ON, and any UNRECOGNISED
  value → **ON + warn-once** (`belt_flag_unrecognised`). A typo'd
  `ture`/`enabled` must never silently disable the oracle-verification of
  persisted bytes while the core stays engaged. This mirrors rust_metrics'
  "a typo'd kill switch must still KILL" rule — both fail toward SAFETY,
  which is OFF for a kernel and ON for a belt; pinned by a dedicated unit
  dual (§7.4).
- **Engagement condition:** digest path only (`bypass_dedup` True) AND flag
  ON AND wheel symbols present AND ABI version match AND
  `sys.byteorder == "little"` AND `array('q'/'d').itemsize == 8` (§1.3 —
  round-6, warn-once reason `native_itemsize_mismatch`) AND the per-tuple
  `bars_strictly_increasing` exit-index certificate holds (§4 row 7) AND
  **`cfg.top_k` is a finite int (round-2 fix):** `top_k=None` = "persist
  ALL ranked" is a LEGAL digest-path config (`SweepRunConfig.top_k: int |
  None = None`, `sweep_run.py:518`; `finalists = ranked` when None,
  `:1541`; only the BOUNDED rank arm requires finite top_k) on which the
  default-ON belt degenerates to a full-population pure-Python re-walk per
  tuple — §5's own definition of the conversion's negation. The core
  REFUSES engagement there (fallback reason `unbounded_top_k`, warn-once;
  today's spine serves, byte-identically); lifting the refusal is a later
  measured decision with its own belt-bound design, never a silent cost —
  AND **kernel WIDTH is selected by the EMPTY-SAFE (round-5) expression
  `max(bit_length(max(bar_bits)), max(condition_bits, default=−1)+1,
  max(conflict-pair indices, default=−1)+1)` (round-2 fix; round-5
  totalisation):**
  the ported `rust_trade_sim` width guards key off MASK values
  (`mask_beyond_u128`), but at `enumerate_prefilter` time the masks are
  what the call CREATES — a u64-fitting bar_bits window plus a
  Python-legal condition bit in [64,128) (`MAX_VOCAB_BITS=128`) must
  select `ResidentSweepCoreU128`, never reach a u64 shift (u64-fitting →
  `ResidentSweepCore`, ≤128 → lazy `ResidentSweepCoreU128` promotion,
  beyond → today's loop; the §1.3 in-core per-index refusal remains the
  backstop for any seam width bug). **The round-5 `default=−1` terms make
  the expression TOTAL on the config-legal EMPTY `condition_bits`
  (`SweepRunConfig` validates only per-bit range + `0 <= k <= n_unique`,
  sweep_run.py:542-547 — `bits=[]`, `k=0` passes; §1.3 clause (a)'s own
  k=0-legality pin protects the arm, and today's spine COMPLETES such a
  tuple: `count_combos(0,0) == 1`, kept == [0]): the bare
  `max(condition_bits)` in the round-2 formula raised ValueError in the
  SEAM before any core call — either an uncaught crash on a
  today-working config or (if it evaluated inside the narrow
  `kernel_error` catch) a silently misattributed per-tuple fallback. With
  `default=−1` the empty terms contribute 0 and width is driven by
  bar_bits alone (never empty — `run_persisted_sweep` refuses empty bars,
  sweep_run.py:1287-1288), so the bits=[]/k=0 tuple ENGAGES and serves
  the §1.3 k=0 vector end-to-end. Width selection is DEFINED to evaluate
  OUTSIDE the `kernel_error` narrow catch, as part of the engagement
  condition — with the totalised expression a ValueError there is
  structurally impossible, and §7.4 pins the bits=[]/k=0 seam vector
  (engages, kept == [0], byte-identical to the spine) beside the
  existing non-empty k=0 pin** —
  AND **the STALL-BUDGET GUARD passes (round-5 — the #1291 composition
  fix; round-6 — absent-symbol arm, clamp spec, shadow repricing,
  liveness-not-net-win honesty):** at engagement time the seam computes
  a CONSERVATIVE upper bound
  on the correctness-machinery wall time this tuple can add IN-WORKER
  before completing —
  `T_overhead_bound =
  (burnin_total_remaining(width) + top_k + SWEEP_CORE_SPOT_CANDIDATES)
  × n_bars × ORACLE_NS_PER_BAR_BOUND
  + enum_shadow_masks_remaining(width)
  × (ENUM_SHADOW_NS + n_bars × PREFILTER_NS_PER_BAR)` —
  **`burnin_total_remaining(width)` DEFINED (fix-forward item 7 — the
  input was ambiguous by 8× between the 64-total floor and the 512 cap,
  and the choice decides which tuple classes ever engage):** it is the
  remaining-to-close TOTAL-compare requirement, initially
  `SWEEP_CORE_SELF_CHECK = 64` per width, decremented per engaged
  compare — NEVER the 512 `SWEEP_CORE_SELF_CHECK_CAP` (the cap is the
  pathological zero-trade-extension ceiling, not the expected compare
  count; pricing at 512 would refuse ~8× more shapes, including the very
  tuples whose engagement is needed to close the window). **ENGAGEMENT-
  STARVATION FEEDBACK LOOP, named (same item):** the burn-in counters
  decrement ONLY on engaged tuples, so a refusal blocks the very burn-in
  whose remaining count caused it — on a worker process (or grid) whose
  tuple stream contains NO shape able to absorb the fresh-process burn-in
  term, the core NEVER engages in that process, burn-in never closes, and
  the conversion silently delivers 1.0× there (each tuple warns once —
  loud per tuple, silent in aggregate). ENGAGEMENT-COVERAGE ENVELOPE,
  disclosed: with the 64-compare pricing, a fresh process at 555k bars
  carries a burn-in term ≈ 64 × ~2.8 s ≈ 180 s > the 150 s half-floor,
  so floor-deadline (low-k) 1m tuples REFUSE until the window closes on
  a higher-work tuple; at top=1000 the BELT term alone (~2,800 s @1m)
  permanently refuses floor-deadline 1m tuples even post-burn-in — the
  refused shapes are largely the walk-bound ~1.2–1.3× shapes where
  little is lost, but the §9 'projected' columns implicitly ASSUME
  engagement, so: the §6 Slice-1 mandated sub-grid measurement MUST also
  run at the operator's actual `--top` (top=1000) to expose the refusal
  pattern (a default-top measurement cannot), and the flip-ADR soak
  already runs at the operator's `--top`. Every consequence errs toward
  refusing (today's spine at today's timing — the guard's stated
  direction);
  every oracle-walk term (burn-in compares — priced at the 64-floor
  remaining count per the definition above, while the WINDOW may extend
  to the 512 cap on zero-trade-heavy streams (those extra compares are
  walk-only-cheap; the deliberate under-pricing of the pathological
  extension is disclosed, bounded, and refuses-safe on the shapes it
  matters for); belt finalists; spot candidates)
  priced per bar at `ORACLE_NS_PER_BAR_BOUND` (a PESSIMISTIC
  pure-Python walker+tail per-bar rate constant, MEASURED on-branch,
  env-overridable `BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR` — **clamp spec,
  round-6 (previously asserted, never defined — the exact poisoned-env
  hole rounds 2–3 closed for the work-cells knobs): fail-loud parse +
  a LOWER clamp AT the committed measured default (the override may
  only RAISE the priced rate — more pessimism ⇒ more refusals, the safe
  direction; a digit-dropping typo can therefore never under-price the
  guard and resurrect the livelock it exists to prevent — the
  legitimate un-refuse lever is `BRUTEX_SWEEP_STALL_SECONDS`, note (c),
  so this knob needs NO gutting-capable low range) and an UPPER clamp
  at 1000× the default (a fat-fingered huge value merely over-refuses —
  safe — but stays finite and loud); each clamp emits one
  `stall_guard_clamped` warn-once with a `direction` field, and §7.4
  carries degenerate-override probes in BOTH directions beside the
  existing work-cells `=1`/`2**44` probes**) and the enum shadow priced
  per shadowed Python enumeration **PLUS its kept-list arm (round-6 —
  the shadow compares `(enumerated, conflict_pruned, kept_list)`, and
  producing `kept_list` runs the min-hits prefilter count per
  conflict-free mask, O(n_bars) ≈ 143 µs/mask @555k vs ~2.4 µs for the
  enumeration itself; the flat per-enumeration constant under-priced
  this ~60× at 1m and erred toward ENGAGING — the one term that
  violated note (a)'s stated error direction. `PREFILTER_NS_PER_BAR`
  = the measured 0.257 ns/bar·mask constant, priced as if EVERY
  shadowed mask were conflict-free with the count running — pessimistic
  even on min_hits=0 grids, where the §1.3 skip law means the shadow
  actually pays less; every term in the bound now errs toward
  refusing)** — and
  recomputes this tuple's OWN #1291 effective stall deadline from the
  SAME public formula (`max(stall_seconds, C(n_unique, k) × n_bars /
  mask_bars_per_sec)`), reading `DEFAULT_STALL_SECONDS` /
  `STALL_MIN_MASK_BARS_PER_SEC` and their env resolvers
  (`BRUTEX_SWEEP_STALL_SECONDS` / `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC`)
  from `sweep_parallel` via a FUNCTION-SCOPE lazy import (cycle-safe;
  ZERO edits to the not-ours #1291 file; a drift pin unit-compares the
  seam's recomputation against
  `ParallelGridDriver._effective_stall_seconds` over a grid of inputs so
  the two formulas can never diverge silently). **ABSENT-SYMBOL ARM,
  round-6 (MANDATORY — the round-5 clause silently hard-depended on
  #1291-only symbols while §11 simultaneously claimed no-PR-dependence;
  round-6 verifiers returned CONTRADICTORY merge states for #1291
  against different refs, so the implementation must assume NEITHER):**
  every #1291 symbol is resolved via `getattr(sweep_parallel, name,
  None)`; if `_effective_stall_seconds` / `STALL_MIN_MASK_BARS_PER_SEC`
  / the stall env resolvers are ABSENT (a pre-#1291 tree, or a #1291
  revert), the effective deadline is the FLAT `DEFAULT_STALL_SECONDS`
  (which exists on every tree, sweep_parallel.py:358; honored via the
  `BRUTEX_SWEEP_STALL_SECONDS` resolver iff present, else the raw
  constant) — this is not a degraded approximation but the EXACT
  deadline the RUNNING pre-#1291 parent actually enforces (no work
  scaling exists there), so the guard is exact-not-modelled on that arm;
  a guard mirroring the work-scaled formula against a flat-deadline
  parent would ADMIT tuples whose belt exceeds the real 300 s deadline —
  precisely the livelock the guard exists to close (round-6 finding 1's
  failure mode (b)). If even `DEFAULT_STALL_SECONDS` is unresolvable, or
  ANY exception escapes guard evaluation (which runs OUTSIDE the narrow
  `kernel_error` catch, so it MUST be total — the width-expression
  standard), the guard REFUSES engagement with the NEW named reason
  `stall_guard_unavailable` (warn-once; fail toward the spine, never an
  uncaught per-tuple crash). The §7.4 drift pin is SCOPED
  `skipif(_effective_stall_seconds absent)` and a dedicated
  absent-symbol unit (monkeypatch-deleted symbols) pins the
  flat-deadline arm + the `stall_guard_unavailable` refusal.** If
  `T_overhead_bound >
  SWEEP_CORE_STALL_BUDGET_FRACTION (0.5) × effective_deadline`, the core
  REFUSES engagement for the tuple — fallback reason
  `stall_budget_exceeded`, warn-once — and today's spine serves, so the
  tuple's timing IS today's timing and #1291's stall semantics hold for
  it by construction. Honesty notes, stated: (a) the seam knows only its
  OWN tuple's work, while the true parent deadline is the MAX over
  in-flight work — the recomputation is therefore a LOWER bound of the
  true deadline and the guard errs toward REFUSING (safe direction);
  (b) **the guard is a LIVENESS bound, NOT a net-win bound (round-6
  correction — the round-5 "the refused shapes are exactly those where
  the belt/burn-in overhead dominates the tuple's own compute … a net
  win, not a loss" biconditional was FALSE against the guard's own
  formula and is WITHDRAWN):** the deadline's `STALL_MIN_MASK_BARS_PER_SEC
  = 2.5e8` is #1291's hang-detection FLOOR (~4× headroom vs the ~1e9
  kernel, per its own honesty note), not a throughput model, so on
  Rust-dominated mid/high-TF tuples the recomputed deadline is ~10–15×
  the tuple's ACTUAL compute and the guard ADMITS overhead up to several
  times the tuple's own work. Concretely (the design's own numbers): a
  48k-bar k≤5 tuple at the top=1000 production sizing has work
  ≈ 2.9e11 mask·bars ⇒ deadline ≈ 1164 s ⇒ 0.5× = 582 s ≫ the belt's
  ~100–200 s ⇒ ENGAGES — yet the belt is strictly ADDITIONAL cost while
  Slices 1/2 recover only ~14 s of enum plus f-dependent kept savings
  there, so at the default min_hits=30 kept fraction the core arm is a
  net ~1.3–1.6× wall-clock REGRESSION vs today's spine on that engaged
  shape. This **ENGAGED-NET-REGRESSION class is a NAMED §5/§9 residual
  (joins the Slice-4 flip-ADR list as class (vii))**: no per-shape
  net-win criterion exists in the engagement condition (deliberate — a
  work-derived compute ESTIMATE accurate enough to gate on is a
  throughput model this design refuses to invent; the guard's one job is
  that engagement can never abort a healthy tuple), and the aggregate
  mitigation is the flip ADR's MEASURED belt fraction at the operator's
  actual `--top` on the soak grid (§6 Slice 4 — an ADR presented with a
  20–50% measured belt fraction on the operator's grid shape does not
  flip, or flips with an explicit accepted-cost statement); the guard
  still runs on EVERY arm incl. sequential — as a liveness bound;
  (c) the operator's natural
  remediation — raising `BRUTEX_SWEEP_STALL_SECONDS` — raises the
  recomputed deadline IDENTICALLY (same env, same formula), so the knob
  un-refuses engagement and protects the parent watchdog in one move;
  (d) **RESTART-PATH residual (round-6, stated not hidden):**
  `T_overhead_bound` prices the HEALTHY-build machinery only — on a
  genuinely-DIVERGENT build a belt/spot-check/reconciliation mismatch
  additionally re-runs the ENTIRE tuple on the Python spine (the §5
  restart semantics), an extra ≈1× spine wall the bound cannot price
  without a kept-fraction throughput model (pricing it at f=1 would
  refuse every work-governed tuple). On such a build, core-pass +
  ≤0.5·D overhead + a full spine restart can exceed D on high-kept-
  fraction shapes, and because the stall abort writes no sentinel and
  the fresh resume processes re-run burn-in (which the escaped defect
  deterministically re-passes), the belt-mismatch → restart →
  stall-abort → resume cycle remains reachable ON THE INCIDENT PATH —
  loud (the abort + the `stream_rebuild_divergence`/belt ERROR events),
  results-safe (sentinel-absent redo), remediated by lever #1. The §3
  crash-loop-breaker RUNBOOK addition is EXTENDED (round-6): "repeated
  `SweepStallError` aborts immediately FOLLOWING belt-mismatch /
  `stream_rebuild_divergence` restarts ⇒ `BRUTEX_SWEEP_RUST_CORE=0` is
  remediation lever #1" — the stall abort itself names no core
  involvement, so without this line the operator has no pointer to the
  lever. This residual joins the Slice-4 flip-ADR list as class
  (viii).
  **FALLBACK-REASON PRECEDENCE LAW (fix-forward item 11 — the goldens pin
  an observable this rule now specifies the producer of):** engagement
  conditions are evaluated — and the fallback reason NAMED — in this
  section's LISTING ORDER; the FIRST failing condition wins and is the ONE
  warned reason. The legacy-refusal golden's fixture fails TWO conditions
  simultaneously (`bypass_dedup=False` ⇒ legacy dedup, sweep_run.py:1415;
  AND `top_k=None`, sweep_run.py:518) — its pinned single
  `legacy_dedup_path` warn is produced BY this rule (digest-path is listed
  before finite-top_k), not by accident; an implementer who checks top_k
  first emits `unbounded_top_k` and fails the golden, which is the pin
  working as intended.
  **ARM-CONDITION PARTITION (fix-forward item 13 — the single chain was
  arm-BLIND; the partition is now normative and the §7.1 enum-only goldens
  freeze THIS partition, not an implementer's guess):**

  | Condition / refusal reason | ENUM arm (`BRUTEX_SWEEP_RUST_ENUM`) | CORE/fold arm (`BRUTEX_SWEEP_RUST_CORE`) |
  |---|---|---|
  | digest path (`legacy_dedup_path`) | ✓ | ✓ |
  | flag ON / `flag_unrecognised` | own flag | own flag |
  | wheel symbols / `wheel_missing`·`wheel_stale` | ✓ | ✓ |
  | ABI version / `abi_version_mismatch` | ✓ (in-band per call) | ✓ |
  | width selection (bit-index-keyed, empty-safe) | ✓ | ✓ |
  | `bars_length_mismatch` (§1.3 F27 seam invariant) | ✓ | ✓ |
  | `sys.byteorder` / `big_endian_host` | — (Vec<u64\|u128> return, not the LE byte transport) | ✓ |
  | itemsize / `native_itemsize_mismatch` | — | ✓ |
  | `bars_strictly_increasing` cert / `ts_uncertified` | — (enumeration touches no exit indices; a dup-ts window's ENUM arm engages — defined here, previously unstated) | ✓ (whole-tuple fallback) |
  | finite top_k / `unbounded_top_k` | — (the refusal exists solely to bound the BELT, which is core-arm-only) | ✓ |
  | STALL-BUDGET GUARD / `stall_budget_exceeded`·`stall_guard_unavailable` | ✓ but priced with the ENUM-ARM TERMS ONLY (the enum-shadow term; the burn-in/belt/spot oracle-walk terms are FOLD-arm machinery that cannot execute on an enum-only pass — pricing them would permanently refuse every low-k tuple under CORE=0/ENUM=1, the runbook's own mid-incident configuration, with reasons naming machinery that cannot run) | ✓ (full formula) |
  | `fold_work_ceiling`, decode refusals, `kernel_result_refused` | — (no fold calls) | ✓ |
  | enum-specific: `enum_no_progress`, `enum_total_mismatch` | ✓ | ✓ (the core arm consumes the enum output when both are ON) |

  Consequences, pinned: (a) under the §3 runbook lever configuration
  `CORE=0/ENUM=1` the enum arm engages under its OWN column — it is NOT
  silently starved by fold-only preconditions, and no warn ever names
  fold machinery that cannot run; (b) Slice 1's condition set IS the enum
  column, so NOTHING about the enum arm's engagement changes when Slice 2
  lands (the previously-silent set change is now structurally impossible);
  (c) §7.1's enum-only and disabled-arm goldens assert against this
  table; (d) every mis-application still errs toward the byte-identical
  Python spine — the partition is an engagement-coverage and
  observability-honesty law, not a divergence path.
  Every
  fallback reason lands on the bit-identical Python spine, warned once per
  (tuple, reason): `wheel_missing`, `wheel_stale`, `flag_unrecognised`,
  `kernel_error` (narrow `(ValueError, TypeError, OverflowError)`),
  `kernel_disabled`, `self_check_mismatch`, `abi_version_mismatch`,
  `legacy_dedup_path`, `big_endian_host`, `ts_uncertified`,
  `unbounded_top_k`, `enum_no_progress` (round-3 — the §1.3/§3
  guaranteed-progress belt; structurally unreachable, covered via a
  stubbed return), `enum_total_mismatch` (round-4 — the §1.3
  total-reconciliation belt vs the Python-side `count_combos` total;
  covered via a stubbed wrong-total return), `stall_budget_exceeded`
  (round-5 — the stall-budget guard above; covered via a synthetic
  low-work/high-top_k tuple shape), `stall_guard_unavailable` (round-6 —
  the guard's absent-symbol/total-evaluation arm; covered via
  monkeypatch-deleted #1291 symbols), `native_itemsize_mismatch`
  (round-6 — the §1.3 itemsize precondition; covered via a patched
  itemsize probe), `fold_work_ceiling` (round-6 — the §3 in-core
  fold work ceiling; covered via a stubbed oversized-chunk Err),
  `bars_length_mismatch` (external fold, Verifier B F27 — the §1.3
  window-length seam invariant; covered via a stubbed n_bars()-lying
  core), `kernel_panic_exception` (external fold, Verifier B F4 — the §3
  panic-policy seam catch; covered via an injected PanicException stub;
  maps into the per-tuple quarantine taxonomy, distinct ERROR alert).
  Fallback granularity
  (riskmin graft): a WHOLE-CHUNK
  envelope/policy precondition miss is detected by the Python wrapper FIRST
  and routes the chunk to today's path BEFORE any kernel error can surface
  where the oracle would have simulated; per-MASK demotion rides the
  `python_required` flag. MemoryError re-raises BARE everywhere.
- **Burn-in (per process), widened + dual-oracle (perfmax + riskmin
  grafts):** the first `SWEEP_CORE_SELF_CHECK = 64` core-served masks —
  **"core-served" DEFINED (round-2 fix): every mask whose row came back
  from a `fold_block` call, INCLUDING flag-served rows.** A `zero_trade`
  or `python_required` row counts toward the 64 and IS compared — the
  compare is always the SERVED BYTES vs both oracle arms, regardless of
  which arm served the row. This deliberately BREAKS with the #1531
  precedent's natural reading (`sweep_driver.py` short-circuits zero-trade
  masks to FLAT_DRAWDOWN_STATS with NO FFI call, and
  `consume_self_check_sample` fires only inside the kernel-served path —
  sound THERE because the kernel computed nothing for them): in this core
  the zero_trade VERDICT is itself a kernel output (the fold's n_trades
  column drives the flag), so a fold bug that wrongly zeroes n_trades
  serves `_empty_metrics` for a mask whose oracle metrics are REAL — under
  the exclusion reading the burn-in never compares it, and the belt is
  STRUCTURALLY blind to the same mask (`_empty_metrics` rows carry
  `total_trades = 0 < MIN_TRADES` and None ratios, so `passes_gates`
  rejects them, `ranking/__init__.py:213` — the damaged mask can never be
  a ranked finalist: a true winner silently deleted). With membership
  pinned, that misflag mismatches inside the window (both oracle arms have
  trades) ⇒ oracle ships + latch, exactly like any other fold defect.
  Honest note, stated not hidden: for a `python_required` row the served
  value IS today's spine, so the served-vs-arm-(b) compare is vacuous by
  construction (same computation); the arm-(a)-vs-arm-(b) leg still runs
  as the pre-existing-bug tripwire, and a WRONG python_required flag
  (unnecessary demotion) is byte-safe — the spine serves identical bytes.
  **NON-TRIVIAL FLOOR (round-3 — the flag-inclusive membership above,
  taken alone, DILUTES the window):** a `zero_trade` compare exercises
  only the n_trades column + flag routing, none of the 9 float folds
  (Welford, Neumaier downside sum, percentile sort+lerp) — and ~66% of
  the profiled fixture tail is zero-trade (`rust_metrics.py:174`), with
  zero-trade density mask-shape-correlated and min_hits=0 grids keeping
  EVERY conflict-free mask, so the FIRST 64 core-served masks of a
  process can plausibly be all or nearly-all zero-trade: the burn-in
  would then complete having certified few or ZERO real fold columns,
  silently voiding this bullet's own calibration rationale. Therefore
  window CLOSURE requires BOTH counters: ≥ `SWEEP_CORE_SELF_CHECK = 64`
  total core-served compares AND ≥ `SWEEP_CORE_SELF_CHECK_NONTRIVIAL =
  16` PLAIN (non-flag-served) rows compared — the window stays OPEN
  (every core-served row still compared, flag-served rows still counted
  AND compared) across tuple boundaries (the counters are process-global)
  until the plain-row floor is met, hard-capped at
  `SWEEP_CORE_SELF_CHECK_CAP = 512` total compares. If the cap trips with
  the floor unmet (a pathological all-zero-trade process start), ONE
  `burnin_nontrivial_floor_unmet` warn fires, the core stays engaged
  (zero-trade rows themselves were all verified; plain rows remain
  netted by the permanent belt + the flag-inclusive spot-check), and the
  condition joins the Slice-4 ADR residual list as class (v). Cost,
  honest: worst case 512 compares vs 64 — but a zero-trade oracle
  compare costs ~the walk only (no metrics tail), so the worst-case
  burn-in cost grows by ≪ 8×; the §9 accounting carries the line.
  **WIDTH-KEYED (round-4 — the counters were width-BLIND across the
  u64→u128 kernel crossover):** the burn-in latch AND both counters
  (total / non-trivial) are keyed PER KERNEL WIDTH — two independent
  single-cell sets for `ResidentSweepCore` (u64) and
  `ResidentSweepCoreU128`, with `reset_self_check_for_tests(width=…)`
  twins. The two pyclasses are SEPARATELY MONOMORPHISED fold/enum code,
  and this section's own width-selection law makes a [64,128)-bit tuple
  Python-reachable mid-process (config validates against
  MAX_VOCAB_BITS=128, `combo_mask.py:100`): under width-blind counters
  a u64-heavy grid start would close the window entirely on u64-served
  rows, and a later ≥64-bit tuple would engage the u128 monomorphisation
  with ZERO burn-in compares ever run against it — the round-3 dilution
  class on an unaddressed sibling axis. Each width now certifies
  itself; the autorevert latch is likewise per-width (a u64 fold defect
  must never silently certify — or block — the other kernel). Honest
  cost + reachability: a second window runs only if the second width
  actually engages in the process — production-UNREACHABLE today
  (`IMPLEMENTED_MASK = (1<<60)-1`: no bar sets a bit ≥ 60, so a u128
  tuple yields essentially no plain rows and its non-trivial floor
  rides the 512-cap/warn path); §7.4 pins that a process completing the
  u64 window then engaging u128 runs a FRESH 64/16 window on the u128
  kernel. This deliberately extends the #1531 single-cell precedent
  (`rust_metrics.py:195` — one width existed there), stated not
  silent.
  The masks (widened from #1531's 16 — that width was calibrated for a
  4-field drawdown surface; this core certifies ~24 columns feeding a
  37-field object; the round-3 non-trivial floor guarantees ≥16 of them
  actually exercise the fold columns) ALSO run **TWO comparison arms with
  STATED provenance**:
  (a) a dedicated MECHANISED-PURE oracle `_oracle_metrics_pure(mask)` in
  `sweep_core.py` — the pure-Python walker (`trade_sim.simulate_trades`,
  verified fno_math-import-free) composed DIRECTLY with `build_equity_curve`
  + `compute_metrics` / the Python `_drawdown_stats` tail. It must NOT call
  `metrics_for_mask`/`metrics_from_trades`: **round-1 MAJOR fix — under the
  production default (`SWEEP_RUST_METRICS_DEFAULT=True`, ADR-0017) those
  route the drawdown 4-tuple through `fno_math.sweep_drawdown_stats`, the
  SAME `drawdown_stats_into` fold body the new core consumes in-crate**, so
  the previous text's "pure-Python oracle `metrics_for_mask` — zero fno_math
  involvement" was checkably false and would have made the dual-oracle
  compare kernel-vs-kernel on max_dd_paisa/max_dd_pct/max_dd_bars/ulcer.
  Independence is MECHANISED, not asserted — **and the pin is
  FUNCTION-granular over an ENUMERATED CLOSED SET, not module-granular
  (round-2 fix):** `build_equity_curve` lives in `sweep_driver.py`, which
  imports `fno_math` at MODULE scope for its run-id hashing
  (`sweep_driver.py:31`/`:1040`), so any module-import-granular pin FAILS
  structurally on the honest composition. The AST pin therefore walks the
  transitive call graph over an enumerated closed set of FUNCTION bodies —
  `_oracle_metrics_pure` itself, `trade_sim.simulate_trades` (+ its
  same-module private helpers), `sweep_driver.build_equity_curve`,
  `exit_index.build_ts_index`, `compute.compute_metrics` /
  `_compute_metrics_impl` / `_drawdown_stats` / `_percentile` /
  `_accumulate_moments` and their same-set transitive callees — asserting
  (i) no body references any `fno_math` attribute or name, and (ii) every
  call resolves INSIDE the closed set or to stdlib/pure-local helpers
  (an out-of-set call fails the pin LOUDLY, so a future refactor cannot
  silently widen the graph). The runtime poison pin (monkeypatch
  `fno_math.sweep_drawdown_stats` → poison; oracle output unmoved) remains
  the second belt for the ONE symbol the shipping tail actually routes
  through — it proves independence from that symbol only, which is why the
  closed-set AST pin is the load-bearing arm. And
  (b) today's shipping OFF-arm (`simulate_masks_with_exit_indices([mask])` +
  `metrics_from_trades`) — the exact object twin-run byte-identity is
  defined against; under the default env this arm DELIBERATELY shares the
  Rust drawdown provenance (that IS the shipping object; arm (a) carries the
  independence). Every `ResultMetrics` field is bit-compared across all
  three (ints exact; floats via `struct.pack('<d')`; None-bits exact).
  **THREE-WAY MISMATCH DISAMBIGUATION PRECEDENCE (Verifier B
  checkpoint-1 audit C32 — supersedes the previous two-outcome text,
  whose oracle-vs-oracle arm shipped the OFF-arm value + LATCHED the
  kernel: latching on a mismatch the kernel did not cause punishes the
  wrong component and silently picks sides in a harness defect):**
  evaluated per burn-in mask IN THIS ORDER —
  **(1) oracle-vs-oracle mismatch** (arm (a), the mechanised-pure Python
  oracle, disagrees with arm (b), the OFF-arm spine — regardless of what
  the kernel returned) = a HARNESS/ENVIRONMENT defect (a pre-existing
  spine bug, a poisoned wheel, a broken oracle) → **ABORT the burn-in
  LOUDLY, NO autorevert latch**: the tuple fails as a per-tuple
  `SweepComboError` with the distinct reason
  `burnin_oracle_disagreement` (⇒ the #1291 TUPLE_FAILED skip path +
  one ERROR event) — no value is shipped for that mask, because
  shipping either arm's value would silently choose sides in a defect
  the kernel is not evidence about, and latching would misattribute it
  to the kernel;
  **(2) kernel-vs-BOTH-oracles mismatch** (arms (a) and (b) agree with
  each other, kernel differs) = a KERNEL defect → the agreed ORACLE
  metrics ship for that mask, `sweep_rust_core.kernel_autoreverted`
  ERROR (once), process latch, and the REST of the tuple continues on
  today's spine (lazily constructing `ResidentTradeSim`) — order and
  bytes preserved because both engines are bit-identical and yield
  order is positional;
  **(3) partial/mixed** (kernel agrees with exactly one arm; or any
  comparison combination not classified above) = treated as (1),
  CONSERVATIVE — a state where the two oracles do not both indict the
  kernel is not clean evidence for an autorevert, and is never a state
  to silently ship from. §7.1 pins one golden per precedence class
  (stubbed (1)/(2)/(3) states ⇒ the exact outcome + event above).
  - *Enumeration burn-in, work-bounded, WIDTH-KEYED (round-5), MASK-COUNT-
    FLOORED (external fold, fix-forward item 4):* the shadow window is
    keyed on a **MASK-COUNT floor, not a range count** — ranges remain the
    comparison UNIT, but the window stays open until
    `SWEEP_ENUM_SELF_CHECK_MASKS = 2^16` (65,536) core-enumerated masks
    have been shadowed **per process PER KERNEL WIDTH**, hard-capped at
    `SWEEP_ENUM_SELF_CHECK_RANGES_CAP = 256` ranges. **RANGES-CAP
    RE-BOUND 64 → 256 (Verifier B checkpoint-1 audit C51 — at the old
    64-range cap the 2^16 floor was UNREACHABLE at default caps on
    exactly the production grids it was derived for):** at the default
    enum work-cells cap a call's effective mask yield on the heavy 1m
    production shapes is ⌊2^28 / n_bars⌋ ≈ **483 masks/call @555k bars
    and ≈ 444 @604k** (this bullet's own round-7 arithmetic below), so
    64 ranges × 444–483 ≈ **28.4k–30.9k masks < 65,536** — the floor
    could NEVER be met there, `enum_shadow_floor_unmet` would have fired
    on EVERY 1m grid, and the shadow (the ONLY production net for the
    in-range kept-set residual) would permanently under-arm on the
    shapes that matter most. The floor and the accumulate-ACROSS-calls
    law are UNCHANGED (they are the coverage guarantee); the CAP is
    re-derived from them: reaching 2^16 masks at the heaviest production
    yield (≈444/call @604k bars) needs ⌈65,536 / 444⌉ = **148 ranges**,
    so 256 gives ≈1.7× headroom while still bounding a
    genuinely-DEGENERATE tiny-range stream (256 ranges of pathological
    small windows cost ≤ 256 × (2.4 µs + n_bars × 0.257 ns) — trivially
    sub-second even @604k bars, and the FULL floor's cost is unchanged
    because the shadow's cost is priced in MASKS, not ranges — the
    ~1.1 s @48k / ~9.5 s @555k figures below stand). A stream that
    cannot reach the mask floor within 256 ranges warns once,
    `enum_shadow_floor_unmet`, and joins the flip-ADR residual list
    beside class (v) — that warn is now the signal of a genuinely
    degenerate stream, not the guaranteed outcome of every 1m
    production tuple. Round-7 rationale — the round-3 dilution class on the
    range-count axis: a "range" is one `enumerate_prefilter` call's
    window, and §3's work-cells cap binds FIRST on every window with
    n_bars ≥ ~256 (2^28/48k ≈ 5,592 masks/range; 2^28/555k ≈ 483), so the
    old 4-RANGE window's real production coverage was ~1.9k–22k masks —
    100–2000× below its own "4×2^20" cost framing — and a 1m-first grid
    spent all 4 ranges on ~0.004% of one tuple with every later tuple
    unshadowed; the shadow is the ONLY production net for the in-range
    kept-set residual, so its coverage is now floored in the unit that
    matters. Each shadowed range is compared by `itertools.islice` of
    today's Python
    enumerate/conflict/prefilter over the SAME rank window;
    `(enumerated, conflict_pruned, kept_list)` compared exactly; mismatch ⇒
    ship the Python lists + latch (the enum latch likewise per-width).
    Round-5 rationale — the SAME dilution class round 4 closed for the
    fold burn-in, on the sibling entry point it left width-BLIND:
    `enumerate_prefilter` is monomorphised per width too (the CNS
    unranking, shift/mask construction, and conflict checks compile
    twice), and this matters MORE than the fold twin in one respect —
    the round-4-NARROWED enum residual (strictly in-range drops with
    self-consistent counts) is production-netted ONLY by the enum
    burn-in + the sampled spot-check, and a DROPPED mask cannot be
    sampled, so under width-blind counters a u64-heavy process start
    consuming all 4 ranges would leave a later ≥64-bit tuple's u128
    enumeration with ZERO production burn-in net at ALL. Reachability is
    the identical argument that width-keyed the fold counters
    (MAX_VOCAB_BITS=128, combo_mask.py:100; config admits bits 64..127,
    sweep_run.py:537-547); honest cost identical too — the second
    shadow window runs only if the u128 kernel actually engages
    (production-unreachable today, IMPLEMENTED_MASK < 2^60).
    `reset_enum_self_check_for_tests(width=…, masks_remaining=…,
    ranges_cap_remaining=…)` duals (the seam supersedes the round-5
    ranges-only signature — the window state is now a mask-count pair);
    §7.4 pins that a process that consumed the u64 shadow window then
    engaging a [64,128)-bit tuple runs a FRESH u128 window, AND that a
    small-range stream keeps the window OPEN across calls until the 2^16
    mask floor is met.
    Cost RESTATED IN MASKS (item 4): bounded to ~2^16 shadowed Python
    enumerations per process per engaged width — worst ≈ 65,536 ×
    (2.4 µs + n_bars × 0.257 ns) ≈ ~1.1 s @48k bars, ~9.5 s @555k (the
    kept-list prefilter arm dominates at 1m; min_hits==0 grids pay less
    per the §1.3 skip law) — still seconds-scale once per process per
    width, NOT two whole k=6 tuples (the judge-3 soak-tax resolution
    stands), and the stall-guard's shadow term prices
    `enum_shadow_masks_remaining(width)` directly in masks (consistent
    with the formula above).
    **MID-TUPLE CONTINUATION CONTRACT (round-6 — "ship the Python lists
    + latch" left the REMAINDER of the rank space unspecified on exactly
    the path a mismatch incident exercises, and the §1.4 driver loop
    defines only the core-call path):** on a shadow mismatch the Python
    lists for the shadowed window ship, the enum latch flips, and the
    remainder of the tuple's rank space is served by CONTINUING today's
    Python enumeration from rank `start_rank + len(python_window)` — the
    PYTHON-side window length, never the untrusted core `next_rank` —
    via `itertools.islice` of the same `iter_masks` stream the shadow
    already holds, to exhaustion. The §1.3 TOTAL-RECONCILIATION assert
    is DEFINED over the resulting MIXED provenance: Σ (core-range
    enumerated before the latch) + (Python-range enumerated after) ==
    `count_combos` — well-defined because both enumerations share
    `iter_masks` lexicographic order, so the rank partition is exact
    across the handoff. §7.4 pins a stubbed range-2-of-4 mismatch
    completing the tuple byte-identical to the spine with the
    mixed-provenance reconciliation assert passing.
    **ENUMERATION-ORDER cross-reference (lever-discovery fold,
    2026-07-09):** the rank space this shadow — and the whole
    enumeration contract — is defined over is the POSITION-LEX
    (itertools) order, NOT ascending-numeric/colex; the LEX-RANK
    unranking MANDATE, the n=4/k=2 counterexample, and the
    full-stream blake3 emission goldens live in the "Slice-1
    substrate + ordering pins (lever-discovery fold, 2026-07-09)"
    section, item 1.
  - *Metrics burn-in cost, QUANTIFIED (round-1 fix — the enum shadow was
    bounded but this strictly larger cost was silent):* arm (a) is a
    pure-Python walk + pure-Python metrics tail per mask — ~40–70 ms/mask
    @20k bars sandbox (`baseline.md` §2), so 64 masks ≈ **3–10 s per worker
    process @20–48k bars, up to ~1–2 min at the 555k-bar 1m TF** — once per
    process (`ProcessPoolExecutor` reuses workers), ~1000× the #1531
    16-mask drawdown-fold precedent this extends; arm (b) adds the cheaper
    shipping-arm recompute. Amortised over hour-scale grids ≈ ~1%; on small
    grids whose first tuples are 1m it is a VISIBLE startup fraction —
    accepted and stated (the burn-in is the primary post-ship correctness
    net), and carried as a line in the §9 overhead accounting.
  - Latches/counters are single-cell process-globals with test-seam
    twins. **The test seam SUPERSEDES the #1531 single-scalar signature
    (round-5 — `rust_metrics.py:295`'s `reset_self_check_for_tests(*,
    remaining: int)` cannot express this core's window states):** the
    round-3 two-counter closure law + the 512 hard cap + the round-4
    width keying make a window configuration a per-width TRIPLE of
    counter state plus a latch — "closed" (total ≥ 64 AND plain ≥ 16),
    "near-cap with the plain floor unmet", and
    "closed-for-u64-open-for-u128" are DISTINCT states no single
    `remaining` scalar can reach, yet every §7.1 poison-past-burn-in
    golden, the 512-cap `burnin_nontrivial_floor_unmet` pin, and the
    width-fresh-window pin must force exactly one of them. The seam is
    therefore specified as the full state-setter
    `reset_self_check_for_tests(*, width=…, total_remaining=…,
    plain_remaining=…, cap_remaining=…, latched=False)` (defaults = a
    fresh per-width window; `total_remaining=0, plain_remaining=0` = the
    post-burn-in closed state the poison-past-burn-in goldens need;
    `cap_remaining` small + `plain_remaining` unmet = the cap-trip pin's
    state) plus `reset_enum_self_check_for_tests(width=…,
    masks_remaining=…, ranges_cap_remaining=…)` (the external-fold item-4
    mask-floor signature) and `reset_observability_memo_for_tests()`.
    Each §7.1/§7.4 window-state golden NAMES the exact counter state it
    sets. The NATURAL-closure recipe (a clean warm-up tuple with ≥64
    core-served compares incl. ≥16 plain rows per width) is ALSO
    mandated as ONE shared integration-tier fixture helper — the seam
    proves the states cheaply, the warm-up fixture proves the real
    closure path once. Each `sweep_parallel`
    worker burns in independently (mirrors #1531).
- **PERMANENT FINALIST BELT (perfmax graft — re-scoped and re-specified
  after round-1 review):** a **PRE-PASS between ranking and persistence** —
  after `rank_combos`/`rank_survivor_columns` returns and BEFORE the FIRST
  `ranked_combo_to_result_row`/`store.append` call, EVERY ranked finalist
  (≤ top_k) is recomputed via `_oracle_metrics_pure` (the SAME
  mechanised-pure arm as burn-in arm (a) — the belt must not share the
  in-crate drawdown fold with the unit under test, the same round-1 defect
  as arm (a)) and bit-compared against the core-derived `rc.metrics`
  (struct.pack keys; None-bits exact).
  **COMPARAND PROVENANCE per rank arm (round-4 MAJOR fix — on the
  bounded arm "the core-derived rc.metrics" was a RE-FOLD, the exact
  comparand class this section's own spot-check paragraph records as
  REJECTED):** on the BATCH arm `rc.metrics` IS the served object (it
  lives in `pairs`), so the belt compares the true serve there. On the
  BOUNDED arm — the arm that engages on exactly the large-population
  tuples — the streamed `ResultMetrics` are DROPPED after
  `SurvivorColumns.append` (`sweep_stream.py:86-101`;
  `sweep_run.py:1520-1541`) and `_rebuild_bounded_finalists` RE-FOLDS the
  finalists through the same core-backed generator, so the belt's oracle
  compare there proves the REBUILD's value-integrity ONLY: a
  NONDETERMINISTIC core defect (e.g. a Slice-3 scratch-buffer state leak,
  any chunk-shape-dependent corruption) that poisons a FINALIST's
  STREAMED sharpe/skew/kurt/gate columns — the values that actually
  decided gate verdicts, the population z-normalisation, rank
  membership/order, and the persisted per-finalist `deflated_sharpe`
  (`sweep_run.py:806-812`, stream-computed and re-married to the rebuilt
  metrics unverified) — while the rebuild folds clean would (a) mis-rank
  or mis-admit the finalist, (b) persist a DSR computed over the corrupt
  streamed sharpe, and (c) PASS the belt. **BOUNDED-ARM SERVE
  RECONCILIATION (round-4, mandatory, governed by the same belt flag;
  round-5 MECHANISM REPAIR) —
  three DETERMINISTIC checks in the belt pre-pass, O(top_k) total, zero
  oracle cost beyond the belt itself — O(top_k) is TRUE only via the
  round-5 POSITION CARRY below; the round-4 "all built from evidence the
  machinery already holds" claim was FALSE and is withdrawn:
  `rank_survivor_columns` builds finalists via heapq indices into its
  local `positions` list and DISCARDS them at finalist construction
  (`sweep_stream.py:385-401`) — `BoundedFinalist` carries only
  (mask, composite_score, deflated_sharpe) (`:280`), `SurvivorColumns`
  exposes forward-only `mask_at(index)` with NO mask→position reverse
  map, and kept-order mask integers are NOT numerically sorted
  (combination-lex ≠ numeric order), so no binary search exists. As
  round-4 wrote it, clause (1) required either an O(kept) reverse pass /
  mask→index dict per tuple — on exactly the bounded arm's large
  populations (k=6 kept ~10^7–4×10^7: a ~4–20 s/tuple Python scan, or a
  ~GB dict sitting OUTSIDE the `owned_nbytes` watermark on the
  memory-budgeted arm) — or an unauthorized structural edit. Both are
  REFUSED; the fix is the POSITION CARRY: **`BoundedFinalist` gains an
  additive `position: int` field — the finalist's SurvivorColumns
  kept-order index (`positions[survivor]`, in hand at the exact
  construction site that currently discards it) — as the ONE authorized
  Slice-2 edit to `sweep_stream.py` (§11 amended accordingly: additive,
  bit-pinned — the field is write-only for ranking, so rank order,
  scores, and every byte artifact are unchanged; §7.4 pins the
  byte-neutrality and that `columns.mask_at(finalist.position) ==
  finalist.mask`)**. Clause (1) then reads each finalist's stored entry
  at `finalist.position` through the EXISTING O(1)/O(top_k) accessors
  (`mask_at` / `sharpe_at` / `gate_passed` / `term_column`, plus a tiny
  additive read-only per-position accessor in the same edit iff a
  retained column lacks one), preceded by the positional MASK-EQUALITY
  belt (`mask_at(position) == mask` — a position-threading bug fails
  deterministically, mirroring `_rebuild_bounded_finalists`' positional
  belt) — genuinely O(top_k) total, ~µs/finalist, and the §9 accounting
  line is now licensed by this mechanism rather than by the withdrawn
  claim:
  (1) **rank-column reconciliation** — every rebuilt finalist's ranking
  columns (EVERY column `SurvivorColumns.append` retained from the
  STREAM, incl. skew/kurt when the overfit gate is active) are
  bit-compared (struct.pack) against its OWN stored SurvivorColumns
  entry AT THE CARRIED POSITION; sharpe is a retained column, so each
  finalist's streamed DSR
  input and every stream-vs-rebuild divergence on every rank-bearing
  column of every finalist is caught deterministically;
  (2) **gate re-verdict** — each rebuilt finalist's metrics re-run the
  literal shared `passes_gates` (~1.3 µs/mask) and must return True (the
  streamed pass admitted it); False exposes an admission only the
  corrupt streamed values licensed;
  (3) **candidate-finalist serve compare** — retained spot-check
  candidates that ARE finalists are NO LONGER EXCLUDED: each is
  bit-compared FULL-column SERVED-object vs the rebuilt `rc.metrics`
  (the direct stream-vs-rebuild serve check) AND served-object vs
  `_oracle_metrics_pure` — zero extra retention (the ≤16 objects are
  already in hand; the round-3 scheme compared only non-finalist
  candidates and deliberately DISCARDED exactly this evidence).
  Any (1)/(2)/(3) divergence is a CORE-DEFECT verdict with the distinct
  reason `stream_rebuild_divergence`, entering the SAME disambiguation +
  latch + tuple-restart semantics as a belt mismatch (below). On the
  BATCH arm, (1) and (2) are cheap identities (the ranked object IS the
  served object) and still run as belt-internal assertions; (3) runs on
  both arms. ALL finalists verify before ANY row
  is appended: the round-1 doc's two contradictory placements (§1.4
  pre-pass vs "before `ranked_combo_to_result_row` runs" read
  per-finalist-interleaved) are resolved to the PRE-PASS — an interleaved
  belt would leave finalists 0..i−1 already appended when finalist i
  mismatches, and the restart's re-append of the same run_ids would trip
  `ResultsStore`'s same-run_id-different-fields conflict raise
  (`results_store.py:478`), converting the restart into a tuple failure.
  The pre-pass makes the restart APPEND-FREE by construction; the conflict
  tripwire stays a backstop, never part of the restart flow.
  This exists because `ranked_combo_to_result_row` persists `rc.metrics`
  values directly (verified `sweep_run.py:809` region — only price anchors
  are re-simulated), so a post-burn-in fold divergence would otherwise land
  in STORED bytes. **Cost, honest:** O(top_k × n_bars) — the pure-Python
  walker+tail per finalist (~0.1–0.2 s/finalist @48k bars, up to ~1–2 s at
  the 1m TF ⇒ worst ~40 s/tuple at top_k=20), the SAME order as the
  finalist-projection Python re-simulation the epilogue already pays; the
  earlier "~8 ms at top_k=20" figure assumed a Rust-assisted recompute and
  is withdrawn with the independence upgrade. **top=1000 quantified
  (round-2 fix — it is NOT merely 'institutional': the CLI's own `--top`
  help pins 1000 as "the spec's Stage-4 filter #1 PRODUCTION sizing",
  `cli.py:385-392`, DEC-SPEC-TOPK-RECONCILE-001):** at top=1000 the belt
  is ~100–200 s/tuple @48k bars and ~17–33 min/tuple at the 1m TF — i.e.
  the belt ≈ DOUBLES the existing O(top_k × n_bars) finalist-projection
  epilogue term at any top_k (same per-finalist oracle re-sim, run twice),
  and can reach ~20–50% overhead on a k≤5 grid projection. Consequently
  (a) the §9 "low-single-digit percent" aggregate claim is SCOPED to
  top_k ≤ 20 and the §9 accounting carries a top=1000 line; (b) the
  Slice-4 default-ON flip ADR MUST present the MEASURED belt fraction at
  the operator's actual `--top` on the soak grid before the flip — a
  projected fraction is not acceptance evidence; (c) `top_k=None` never
  reaches the belt at all: core engagement REFUSES it
  (`unbounded_top_k`, engagement condition above), so the default-ON belt
  has NO unbounded arm (belt-cost bench pinned flat-vs-kept and
  exercised at both top_k=20 and top_k=1000 shapes, §7.7).
  **Belt/spot-check ARM SCOPING — CORE-ARM-ONLY, stated (round-2 fix; the
  §9 cost accounting silently assumed it):** the belt and the spot-check
  run ONLY on a pass whose masks the CORE served. They are SKIPPED — with
  one `belt_skipped_non_core` INFO per tuple — on the restarted
  Python-spine pass, on every fallback-reason tuple (`ts_uncertified`,
  `wheel_missing`, `unbounded_top_k`, …), on a latched process, and when
  the kernel flags are OFF. Rationale: the Python spine is the twin-run-
  pinned authority this whole design defines byte-identity against;
  belting IT against the pure oracle asks the PRE-EXISTING
  spine-vs-oracle-divergence question — out of this conversion's scope,
  O(top_k × n_bars) on arms that gain nothing from it, and (the round-2
  finding) the source of a non-convergent restart loop.
  **Mismatch semantics — DISAMBIGUATED, then restarted (round-2 fix: the
  round-1 text compared core-vs-pure-oracle only, but under the ADR-0017
  default the "Python spine" the tuple restarts onto routes its drawdown
  through the SAME `fno_math` fold body past rust_metrics' 16-mask
  burn-in, so a PRE-EXISTING shipping-spine-vs-pure-oracle divergence
  would reproduce on the restarted pass — an unbounded restart loop as
  round-1 literally specified it; the burn-in already had the dual-arm
  disambiguation, the belt lacked it):** on a belt or spot-check mismatch
  the mask is recomputed ONCE via the shipping OFF-arm
  (`simulate_masks_with_exit_indices` + `metrics_from_trades` — burn-in
  arm (b)), and the three-way verdict decides:
  * **OFF-arm == core ≠ pure-oracle ⇒ PRE-EXISTING divergence** (the core
    faithfully reproduced today's shipping bytes): NO restart, NO core
    latch — the core (= spine) values ship (they ARE the twin-run-pinned
    bytes), one DISTINCT `belt_preexisting_divergence` ERROR fires (once
    per process) — the belt's mirror of the burn-in's oracle-vs-oracle
    tripwire branch.
  * **OFF-arm == pure-oracle ≠ core ⇒ CORE DEFECT:** latch the core OFF
    for the process + tuple restart on the Python spine (below).
  * **Three-way disagreement ⇒ treated as a core defect** (latch +
    restart) with a distinct `belt_threeway_divergence` reason.
  **Restart re-entry SPECIFIED (round-1 fix, round-2 termination): the
  "nothing durable written yet" premise was false — the SWEEP_START audit
  line is durably appended BEFORE enumeration, `sweep_run.py:1370`.** The
  rank order was computed over core values, so a per-finalist oracle-ship
  is NOT sufficient — a core-defect verdict restarts the current TUPLE on
  the Python spine, re-entering STRICTLY BELOW the SWEEP_START emission,
  WITHIN the same `run_persisted_sweep` invocation. Structural requirement
  on the Slice-2 refactor: the enumerate→fold→rank→belt body is factored
  into ONE inner function whose **TUPLE-SCOPED state — the
  `enumerated`/`conflict_pruned`/`skipped_dedup` tallies,
  `pairs`/`SurvivorColumns` accumulators, kept-mask lists, spot-check
  samples — is FRESH per call; the burn-in/autorevert LATCHES and the
  burn-in REMAINING-counters are explicitly NOT in that list (round-2 fix
  of a round-1 self-contradiction: the round-1 restart paragraph listed
  "burn-in counters" as fresh-per-call while the same section pinned them
  single-cell process-global and §9 costed the 64-mask burn-in once per
  process). Resolution, one semantic: burn-in latch + counters are
  PROCESS-GLOBAL single cells (#1531, `reset_self_check_for_tests`
  twins) — the §9 once-per-process accounting is NORMATIVE — and §7.4
  pins that the second tuple in the same worker process (and the
  restarted pass) runs ZERO additional burn-in compares.** The mismatch
  path discards the first call's tuple-scoped state WHOLESALE and calls
  the inner function once more with the engine forced to the Python
  spine, then proceeds to the untouched persist/flush/sidecar/sentinel
  epilogue. **Termination is STRUCTURAL (round-2):** the restarted pass is
  spine-served, and the belt/spot-check are core-arm-only (above) — so
  they do not run on it, a second mismatch is impossible by construction,
  and the restart is genuinely bounded to at most one wasted partial tuple
  per process (latch), never a loop. Consequences, pinned: the tuple
  still emits EXACTLY 3 audit lines (SWEEP_START never re-emitted), and
  the SWEEP_FLUSH/SWEEP_COMPLETE payloads carry ONLY the restarted
  (Python-spine) run's tallies — byte-equal to a pure-Python run of the
  same tuple, never doubled. A kill −9 during the restart stays safe
  (sentinel still absent ⇒ full redo).
  **What the belt guarantees — restated at VALUE level (round-1 correction
  of the overbroad "everything that PERSISTS is oracle-verified forever";
  round-4 re-correction of the still-overbroad "any divergence that
  touches a ranked finalist's own metrics", which was FALSE on the
  bounded arm for stream-only divergence):** the belt + the round-4
  serve reconciliation together prove the VALUE-integrity of every
  persisted finalist's `ResultMetrics` (batch arm: the served object
  directly; bounded arm: the rebuilt object via the belt, PLUS every
  finalist's streamed rank-bearing columns and gate verdict via the
  reconciliation, PLUS full streamed columns for candidate-finalists) —
  i.e. any divergence that touches a ranked finalist's PERSISTED or
  RANK-BEARING values is detected on either arm. Persisted/rank-bearing
  artifact classes that provably ESCAPE are named, not papered over:
  (i) **finalist SET/ORDER (membership)** — gate verdicts + the 7 composite
  terms are z-normalised across the surviving POPULATION, so a
  post-burn-in divergence confined to a NON-finalist (deflating a true
  winner below a gate/top_k boundary, or shifting any survivor's term and
  thus the z-statistics) changes WHICH rows persist while every row that
  does persist verifies clean (round-4 re-scope: a divergence on a
  FINALIST's own streamed rank-bearing columns is NO LONGER in this
  class — the serve reconciliation catches it deterministically; the
  class is now genuinely confined to non-finalist divergence);
  (ii) **persisted `deflated_sharpe`** — computed by the overfit gate over
  EVERY backtested combo's sharpe (`ranking/__init__.py:555`) and persisted
  verbatim per finalist (`sweep_run.py:809`), so a non-finalist divergence
  moves every finalist's stored DSR (round-4 re-scope: each finalist's
  OWN streamed sharpe input is now reconciled; the residual is the
  NON-finalist sharpes in the DSR population);
  (iii) **the trial-stats sidecar sums** — `sum_sharpe`/`sum_sharpe_sq`
  fsum-folded over the FULL backtested population (`sweep_run.py:1544`,
  `trial_stats.py`), persisted bytes the Stage-4 leaderboard DSR gate later
  consumes.
  Full verification of (i)–(iii) would require recomputing the whole
  population on the Python spine — the conversion's negation. The design
  therefore adds a **SAMPLED POPULATION SPOT-CHECK (permanent, governed by
  the same belt flag), via RETAINED CANDIDATES (round-3 mechanism — the
  round-2 text had NO core-side comparand on the bounded rank arm):** the
  round-2 wording "recomputed via `_oracle_metrics_pure` and bit-compared"
  never said WHAT the core-derived comparand is. On the batch arm it lives
  in `pairs`; on the BOUNDED arm — honored UNCHANGED per §1.4 —
  `SurvivorColumns.append(mask, metrics)` retains ~5 ranking columns and
  the caller DROPS the 37-field `ResultMetrics` for every non-finalist
  (`sweep_stream.py:86-101` docstring; `sweep_run.py:1509-1553`), and the
  non-finalist sample is drawable only POST-rank — so on exactly the
  large-population tuples the spot-check exists for (the bounded arm
  engages where the batch projection blows the budget) the check as
  written had NOTHING core-derived to compare, and a naive "compare
  retained columns only" narrowing would silently blind it to a wrong
  avg_mfe/max_dd_bars fold. Mechanism, ONE scheme on BOTH rank arms:
  BEFORE ranking, `SWEEP_CORE_SPOT_CANDIDATES = 16` candidate masks are
  drawn DETERMINISTICALLY (seeded from the tuple digest over the kept
  list — reproducible; the kept list is arm-identical so the draw is
  too) from the FULL kept list — **INCLUDING flag-served masks (round-2
  pin preserved: a wrongly-flagged `zero_trade` mask is a candidate)**;
  during the streaming yield loop the seam RETAINS the full served
  `ResultMetrics` for exactly those ≤16 masks (O(16) extra objects,
  ≈ tens of KB — a constant additive term stated BESIDE the
  `plan_rank_memory`/`owned_nbytes` watermark, never folded into it);
  POST-rank the sample = the first `SWEEP_CORE_SPOT_CHECK = 4` candidates
  that are NON-finalists, each bit-compared **SERVED-OBJECT vs
  `_oracle_metrics_pure`** with the SAME disambiguation + latch +
  tuple-restart semantics as the belt (above), and the same
  CORE-ARM-ONLY scoping. **Round-4: candidates that ARE finalists are no
  longer discarded — each runs the serve-reconciliation clause-(3)
  compare (served vs rebuilt full-column + served vs oracle; the belt
  paragraph above), so the retained evidence for a
  candidate-turned-finalist is USED, never deliberately excluded.** This compares the row that was actually SERVED
  (not a fresh re-fold), so a nondeterministic-serve defect stays inside
  the net; the rejected alternative — re-calling `fold_block` on the
  sampled masks post-rank — verifies only a RECOMPUTE and is recorded as
  rejected for exactly that reason, so an implementer cannot "simplify"
  back to it. **Degenerate pools DEFINED (round-2 fix, round-3 restated
  for candidates):** `spot_check_sampled` = the count actually compared
  = `min(4, |non-finalist candidates|)`; an EMPTY pool (kept == 0 —
  reachable, `sweep_run.py:1461` min_hits can kill all — or every kept
  mask a finalist, i.e. kept ≤ top_k) SKIPS with `spot_check_sampled = 0`
  — a real, covered branch, never a crash and never a silent no-op; a
  tuple with ≥4 non-finalists whose 16 candidates nonetheless include
  <4 of them can yield 0 < sampled < 4 (finalists are ≤ top_k of kept,
  so the probability vanishes as kept grows; on bounded-arm populations
  candidates are essentially always non-finalists) — honest, stated:
  the per-tuple `sweep_core_path` INFO event carries `spot_check_sampled`
  (observability bullet below), §7.4 pins a >4-non-finalist fixture
  asserting `spot_check_sampled == 4`, an all-finalist fixture asserting
  `== 0`, AND a BOUNDED-rank-arm fixture asserting the served-object
  compare actually ran (the round-3 teeth), and the detection arithmetic
  is SCOPED to tuples yielding ≥ 4 non-finalist candidates.
  This converts the population blind spot from
  "never checked in production" to "probabilistically checked every tuple,
  forever" (an input-dependent fold bug affecting fraction f of kept masks
  survives a |pool|≥4 tuple with probability ≈ (1−f)^4 and a G-tuple grid
  with ≈ (1−f)^{4G}). The DETERMINISTIC membership/population net remains
  the 64-mask dual-oracle burn-in + the ship-time twin-run grids + the
  pre-flip soak twin-runs — and the Slice-4 default-ON ADR must state
  EXACTLY this residual, never the withdrawn blanket claim. **The named
  residual list for that ADR is now NINE-fold (round-2 addition; round-3
  extension; round-4 re-scope of (i)/(ii) + extension; round-6 (vii)
  engaged-net-regression + (viii) restart-path stall — both defined in
  the stall-budget-guard notes (b)/(d) above; external fold (ix)
  tallies-only enumeration divergence — fix-forward item 1):**
  (i)–(iii) above, PLUS (iv) the `zero_trade`-MISFLAG class — a wrongly
  zeroed mask is gate-rejected (`total_trades < MIN_TRADES`) and can
  never be a ranked finalist, so it is belt-blind BY CONSTRUCTION; its
  production nets are the round-2 burn-in membership pin (flag-served
  rows compared inside the 64-mask window) + the flag-INCLUSIVE
  spot-check sampling, deterministic only at ship time via §7.2's
  zero-trade-heavy twin tuples and §7.3's flag-routing property; PLUS
  (v) the round-3 burn-in DILUTION residual — on a pathological
  all-zero-trade process start the 512-compare hard cap can trip with
  the 16-plain-row non-trivial floor unmet (`burnin_nontrivial_floor_
  unmet` warned once), leaving the float folds burn-in-UNcertified for
  that process until its first plain rows land in belt/spot-check
  territory; PLUS (vi) the round-4 bounded-arm SERVE residual — a
  stream-only divergence on a NON-candidate finalist confined to columns
  that are NEITHER SurvivorColumns-retained NOR gate-verdict-flipping is
  unverified by the serve reconciliation, and is provably EFFECT-FREE on
  every persisted and rank-bearing output (those streamed values are
  dropped after append and feed nothing that persists: persisted values
  are the belt-verified rebuild, rank order + the finalist's DSR input
  ride the reconciled retained columns, admission rides the re-verdicted
  gates) — named for completeness, netted by the candidate-finalist
  full-column compares + burn-in + twin-runs; PLUS (vii) the round-6
  ENGAGED-NET-REGRESSION class (stall-guard note (b): an engaged
  mid/high-TF tuple at top=1000 can be a net ~1.3–1.6× wall-clock
  regression vs today's spine — the guard is a LIVENESS bound, not a
  net-win bound; mitigation = the flip ADR's MEASURED belt fraction at
  the operator's `--top`); PLUS (viii) the round-6 RESTART-PATH stall
  class (stall-guard note (d): on a genuinely-divergent build the
  belt-mismatch restart's spine pass is unpriced by the guard, so a
  `SweepStallError` remains reachable on the incident path — loud,
  results-safe, remediated by the extended breaker-runbook lever
  naming `BRUTEX_SWEEP_RUST_CORE=0`); PLUS (ix) the TALLIES-ONLY
  ENUMERATION-DIVERGENCE class (external fold, fix-forward item 1 —
  previously absent from the residual list): a COUNT-only divergence
  with a CORRECT kept set and CORRECT Σ enumerated (the one-line
  mis-bucketing bug: a min-hits drop incremented into `conflict_pruned`
  or vice versa) escapes every permanent production net EXCEPT the
  2^16-mask-floor (256-range-cap — audit C51) enum shadow — store bytes and
  `FlushResult.content_hash` do not move (belt, spot-check,
  content-hash poison teeth all blind), and the round-4
  total-reconciliation belt asserts ONLY `next_rank == n_masks` and
  `Σ enumerated == n_masks`, never the per-bucket split — yet the
  divergence lands in PERSISTED bytes: the trial-stats sidecar's
  `n_conflict_pruned`/`n_minhits_dropped` (sweep_run.py:1593-1598 →
  trial_stats.py:178-180) and the SWEEP_COMPLETE audit payload, both
  consumed by the sweep-verify reconciliation arithmetic
  (sweep_verify.py:411-412); the enum shadow DOES compare
  `conflict_pruned`, so the surviving sub-class is rank-region/
  input-dependent mis-bucketing past the shadow window — netted
  deterministically only by the ship-time twin-run SIDECAR compare
  (whose teeth in this direction are proven by the §7.1 COUNT-ONLY
  poison golden: sidecar hash MOVES, store hash pinned UNCHANGED).
  The layered net
  beyond: the hypothesis cross-arm property, ResultsStore's
  same-run_id-different-fields conflict tripwire, and the #1295 R45
  diffcheck — each honestly scoped: R45 re-simulates masks sampled FROM
  persisted rows and the conflict tripwire needs a colliding append, so
  NEITHER can see a missing/replaced finalist or a wrong population
  statistic (stated, round-1).
- **Anti-vacuous goldens — on a NEW DIGEST-PATH GOLDEN VARIANT (round-3
  MAJOR fix: as previously written the engaged-event assertions were
  UNIMPLEMENTABLE against the fixture their literals come from):** the
  only source of `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` is
  `tests/golden/test_engine_sweep_run_golden.py`, whose `_run` passes NO
  `trial_stats_digest` (⇒ `bypass_dedup=False`, the LEGACY dedup path —
  `sweep_run.py:1415`) and whose `_config` leaves `top_k=None`
  (`sweep_run.py:518`) — this section's own engagement condition REFUSES
  the core on BOTH grounds (`legacy_dedup_path`, `unbounded_top_k`), so
  on that fixture the core/enum arms can never engage; a naive
  implementation would either fail the engaged assertion or silently pass
  a Python==Python vacuous compare — the exact #1186 failure class this
  bullet exists to prevent (the #1531 precedent worked because
  rust-metrics is not digest-gated; this core is). Resolution: a
  DIGEST-PATH GOLDEN VARIANT is added to the same golden module — the
  same synthetic grid with a synthetic `trial_stats_digest` and a finite
  `top_k` ≥ the ranked count (finalists == ranked, so the finalist SET
  matches the legacy fixture's) — whose
  `_GOLDEN_RUN_ID_DIGEST`/`_GOLDEN_CONTENT_HASH_DIGEST` literals are
  MINTED ON THE OFF ARM FIRST (the variant lands in Slice 1's PR while
  the fold core does not yet exist / is flag-OFF, so the literals are
  **SHIPPING-SPINE-authored — NEW-CORE-free by construction** (round-5
  provenance correction: under the ADR-0017 default,
  `SWEEP_RUST_METRICS_DEFAULT=True` at `rust_metrics.py:154`, the OFF
  arm's metrics tail routes every drawdown 4-tuple through
  `fno_math.sweep_drawdown_stats` — so calling the literals
  "pure-Python-spine bytes … never Rust-authored" repeated the exact
  provenance claim round-1 item 3 ruled checkably false for the oracle
  arm; #1531-proven bit-identical, so the BYTES are unaffected in every
  direction, but the minting instruction must state the true provenance:
  the literals are the exact twin-run-pinned SHIPPING bytes, which is
  precisely what the goldens need)) and then
  frozen. EVERY engagement-dependent golden below (engaged events, poison
  twins, belt-restart, misflag-in-window) runs against the VARIANT. The
  ORIGINAL fixture + literals stay frozen byte-unchanged and gain a new
  role: the LEGACY-REFUSAL golden — both flags ON reproduces the original
  literals with ZERO engaged events + one `legacy_dedup_path`
  fallback-reason warn (which is simultaneously an engagement-condition
  teeth test and one of the negative pins below). **Negative
  engaged-event pins (round-3 MAJOR fix — bytes are identical across all
  four arms BY CONSTRUCTION, so the structlog engagement events are the
  ONLY arm discriminator, and positive-only pins cannot see
  CROSS-engagement):** every arm-combination golden asserts BOTH
  directions — enum-only: exactly one `sweep_rust_enum.engaged` AND ZERO
  `sweep_rust_core.engaged`; core-only: the inverse; OFF: zero of both;
  both-ON: exactly one of each. Without the zero-count assertions, a seam
  defect engaging `fold_block` under the enum flag alone (an or-of-flags
  resolution bug, a width-promotion path) passes every gate — bytes
  identical, the expected positive event still fires, the OFF-arm cost
  gate passes (cross-engagement makes that arm FASTER), and the weekly
  mutmut lane covers `sweep_core.py` only POST-merge on the scheduled
  cadence (round-4: the module now JOINS the weekly python loop — §7.6 —
  closing the prior no-lane gap, but a scheduled lane is not a merge
  gate, so the merge-blocking arm discriminator remains these negative
  pins). **Disabled-arm POISON goldens (round-3,
  the runbook's teeth):** a poisoned `ResidentSweepCore` with
  `BRUTEX_SWEEP_RUST_CORE=0` + `ENUM=1` reproduces the digest-variant
  literals byte-for-byte (proves the core flag ALONE prevents core
  consumption — exactly the mid-incident configuration the §3
  crash-loop-breaker runbook's remediation lever #1 puts an operator in,
  previously shipped untested), and symmetrically a poisoned
  `enumerate_prefilter` with `ENUM=0` + `CORE=1` reproduces them. §6
  Slice 2's "all 4 arm combinations golden-pinned" is DELIVERED by these
  pins, not merely claimed. Then the positive teeth as before: the ON
  arms reproduce the digest-variant literals byte-for-byte WITH exactly
  one `sweep_rust_core.engaged` (resp. `sweep_rust_enum.engaged`) event
  and zero autorevert events; a domain-VALID poisoned fold (test seam monkeypatching
  the late-bound module-level `ResidentSweepCore` name in `sweep_core.py`)
  past burn-in **with the belt disabled via its test seam** MOVES the hash
  (proves the seam genuinely consumes the kernel — the #1186 inverted
  teeth); the same poison with the belt ON ⇒ pinned bytes + one autorevert +
  tuple restart **with audit-line count == 3 and the SWEEP_COMPLETE tally
  payload byte-equal to the pure-Python arm** (the restart-shape pins,
  round-1); the same poison DURING burn-in ⇒ pinned bytes + one
  `kernel_autoreverted` with `reason == "self_check_mismatch"`. A
  domain-INVALID block (bad ABI version, wrong stride, row-count ≠
  submitted-chunk length, cross-buffer length mismatch — the §1.3 decode
  refusal law) ⇒ one `kernel_result_refused` + pinned bytes. **The ENUM arm gets the SAME
  inverted teeth (round-1 gap — engaged-event pins alone were exactly what
  the #1186 precedent ruled insufficient):** a domain-VALID poisoned
  `enumerate_prefilter` (one kept mask silently dropped) past the enum
  burn-in MOVES the tallies AND the content hash — necessary because the
  finalist belt structurally cannot catch a wrong kept SET (it re-verifies
  ranked finalists' metrics GIVEN their masks, and the tallies come from
  the same core call, self-consistently wrong). Honest residual, stated
  (round-4 NARROWED; round-6 SYMMETRISED — the round-4 "strictly …
  drops" wording was FALSE: the total-reconciliation belt reconciles
  TOTALS only, never the kept SET, so the symmetric SPURIOUS-KEEP — a
  conflicted or below-min-hits mask wrongly ADMITTED with
  self-consistently adjusted tallies — equally survives every production
  net; worse, a spurious keep walks, enters the backtested population
  (shifting z-normalisation, sidecar fsum sums, the DSR population), and
  can persist as a finalist today's spine would never emit, while the
  finalist belt verifies its metrics GIVEN the mask — clean by
  construction — and the spot-check compares served-vs-oracle for the
  SAME mask, never membership legitimacy): the §1.3 TOTAL-RECONCILIATION
  belt nets the
  whole total-count / rank-partition defect class DETERMINISTICALLY per
  tuple (a truncated/extended enumeration cannot match the driver's
  independent `count_combos` total), so the surviving indirect-only class
  past the mask-count-floored enum shadow burn-in is **IN-RANGE KEPT-SET DIVERGENCE —
  silent DROPS *or* SPURIOUS KEEPS — with SELF-CONSISTENT counts**,
  netted only INDIRECTLY in production (a dropped mask cannot be
  sampled; a spuriously-kept mask CAN be spot-check-sampled — its
  served-vs-oracle metrics compare clean, so sampling does not catch
  membership either); the
  deterministic net for the WHOLE class is the CNS proptests, the
  hypothesis list-equality property, and the ship-time twin-run grids;
  this is a named residual beside the belt paragraph's residual list and
  joins it in the Slice-4 flip ADR verbatim — with the round-6
  BOTH-DIRECTION wording, never the withdrawn drops-only narrowing.
  §7.1 gains the SYMMETRIC poison twin (round-6): a domain-VALID
  poisoned `enumerate_prefilter` that wrongly KEEPS one conflicted mask
  past the enum burn-in MOVES the tallies AND the content hash — the
  spurious-keep mirror of the dropped-mask poison, proving the twin-run
  gate has teeth on both directions of the class.
- **Observability:** one `sweep_run.sweep_core_path` INFO per tuple
  (`core_active`, n_bars, kept, chunks, belt state, `spot_check_sampled`,
  restart flag) beside the existing pinned events (which keep firing on their arms). Zero new audit lines;
  engine choice never enters audit payloads or any identity (engine-blind
  law, `sweep_run.py:169`).

---

## 6. Phased delivery — each slice independently golden-proven, revertable

- **Slice 0 — compute.py fold/combine decomposition (Python-only, no flag,
  LANDS ALONE FIRST — riskmin sequencing graft).**
  `_fold_trades(trades, start_equity) -> TradeFolds` +
  `combine_metrics_from_folds(folds, *, total_bars, benchmark_return_pct,
  start_equity_paisa, span_years, drawdown) -> ResultMetrics`;
  `_compute_metrics_impl` becomes `combine(fold(...))` — a pure
  parameterisation (the #1186 same-file extraction precedent). Its own
  revertable commit/PR BEFORE any Rust, with byte-neutrality proof: all
  goldens UNMODIFIED, 100% line+branch, weekly-mutmut zero-survivor on
  compute.py, exact-value pins for every moved expression, **and the
  ORDER-SENSITIVITY DIFFERENTIAL GATE below (Verifier B checkpoint-1
  audit C23 — MANDATORY, lands BEFORE the Slice-0 PR opens)**.
  **Slice-0 ORDER-SENSITIVITY ORACLE (Verifier B checkpoint-1 audit
  C23 — MANDATED).** The named Slice-0 gates above are all
  fixed-input gates (frozen goldens + exact-value pins on frozen
  expressions + mutmut over the frozen suite): an UNFAITHFUL
  fold/combine decomposition of the SEQUENTIAL Welford/Pébay moment
  accumulations (`brutex/metrics/compute.py:32-33` module contract;
  the third/fourth-central-moment online updates live in
  `_accumulate_moments`, `compute.py:121-122`) could pass every one of
  them on the fixture inputs yet silently change production bytes on an
  input shape the fixtures never exercise. Slice-0 acceptance therefore
  ADDITIONALLY MANDATES a hypothesis DIFFERENTIAL property gate:
  `combine_metrics_from_folds(_fold_trades(trades, …), …)` must equal
  the pre-decomposition `_compute_metrics_impl(trades, …)`
  (`brutex/metrics/compute.py:569` — the internal metrics impl this
  decomposition refactors; the oracle arm is the VERBATIM pre-Slice-0
  body, captured as a test-local copy or via the merge-base source so
  the comparison is never decomposition-vs-itself) at **struct.pack
  BYTE equality per float field** (`struct.pack("<d", x)` — signed
  zeros + NaN payloads included, the #1531 burn-in comparison idiom;
  int fields compare `==`), over hypothesis-GENERATED trade sequences
  that MUST include: (a) adversarial ORDERINGS (sorted ascending /
  descending / interleaved-sign permutations of the same multiset —
  the fold order the sequential Welford walk is sensitive to), (b)
  fold/combine SPLITS at every boundary (split index swept 0..n —
  degenerate empty-left/empty-right folds included) in whatever
  decomposition the fold layer admits, (c) single-element folds
  (n=1 chains), and (d) the negative / zero / denormal pnl classes
  (pnl_paisa < 0, == 0, and float-denormal-producing magnitude ratios
  feeding the moment accumulator). This gate lands IN the Slice-0 PR's
  test set BEFORE that PR opens (it is part of Slice-0's Definition of
  Ready, not a follow-up), and is the Slice-0 instance of the §7 O2
  cross-arm oracle discipline — the later Rust fold_block arm (Slice 2)
  re-verifies against the SAME oracle through the §5 burn-in
  [SUPERSEDED-IN-PART for Slice 2 by the Slice-2 INDEPENDENT-ORACLE
  MANDATE below (Verifier B Slice-0 re-pin, 2026-07-10): the Slice-0
  oracle imports leaf helpers from live compute.py, so Slice 2's
  differential must bind to a GENUINELY independent oracle instead —
  the §5 burn-in machinery stands, the "SAME oracle" arm does not].
  **COORDINATOR RULING RECEIVED (2026-07-09): the compute.py Slice-0 zone
  is GRANTED**, under three binding conditions carried verbatim into the
  Slice-0 PR: (1) BEHAVIOR-IDENTICAL — a pure parameterisation, zero
  observable change (the byte-neutrality proof above IS the evidence);
  (2) a STANDALONE COMMIT (its own revertable commit/PR, nothing else in
  it); (3) a PRE-PUSH DIFF-CHECK vs PR #1295's branch (verify compute.py
  is untouched there immediately before push — a mid-flight #1295 touch
  ⇒ stop + re-coordinate, never a silent conflict). SEQUENCING RULING
  (same date): the TWIN-BASELINE capture WAITS for product-audit-1's
  identity PR-1; **Slice 0 MAY PRECEDE it** (Slice 0 is byte-neutral so
  it cannot move any baseline) — Slices 1+ order behind the identity
  PR-1 baseline capture. The refusal-fallback clause is superseded by
  the grant but retained as history: had sign-off been refused, the
  fallback scope was Slice 1 alone + a re-scoped Slice 2 decision round.
  **Slice-0 work item (Verifier A round-1 fold): fix the
  `brutex/strategies/combo.py:66-68` stale signed-zero DOCSTRING** — the
  module docstring's item 3 still claims "Signed zero is split (`0.0` ->
  `"0.0"`, `-0.0` -> `"-0.0"`) — the SAFE dedup direction", but the CODE
  at `combo.py:325-331` normalizes `-0.0 → +0.0` BEFORE repr (`value =
  value + 0.0`, "the results_store / trial_stats precedent") — verified
  against origin/main 95955a71: the docstring describes the opposite of
  the shipped behavior, and Verifier B's frozen corpus (d) pins the CODE
  as authoritative ("combo.py docstring :66-68 stale, code :325-331
  wins"). Docstring-only edit, zero behavior change — riding Slice 0
  because that is the one granted compute-zone-adjacent Python-only
  commit before any Rust lands.
- **Slice 1 — Rust enumeration+prefilter (integer-only), default-OFF
  `BRUTEX_SWEEP_RUST_ENUM`** (as SHIPPED by #1738; the LIVE default is now ON
  per DEC-RUST-DEFAULT-ON-WAVE-001 #2215 — see the §-above RESOLVED note)**.**
  `enumerate_prefilter` + CNS unranking +
  `sweep_core.py` seam + work-bounded enum burn-in + the round-4
  TOTAL-RECONCILIATION belt (`enum_total_mismatch` vs the driver's own
  `count_combos` total) + dual-knob invariance
  pins WITH the round-4 cap-respected assertions + the round-6 MIN-HITS
  SHORT-CIRCUIT LAW with its in-band `hits_counted` pins (§1.3) +
  hypothesis
  list-equality property (random bit sets × k × synthetic
  conflict pairs × min_hits: Rust list == Python list exactly) + twin-run
  byte-identity on the golden fixture grid AND a small real-data grid —
  **LANED per the §7.2 round-6 venue law: the golden-grid twin is the
  CI-lane arm; the real-data twin is a REQUIRED ON-BRANCH gate with
  command + both arms' hashes recorded verbatim in the PR body.**
  **Slice 1 ALSO lands the DIGEST-PATH GOLDEN VARIANT (round-3, §5
  anti-vacuous bullet) with its literals minted on the OFF arm** — the
  variant's fixture commit precedes (or is flag-OFF during) any core
  code, so `_GOLDEN_RUN_ID_DIGEST`/`_GOLDEN_CONTENT_HASH_DIGEST` are
  SHIPPING-SPINE-authored, new-core-free by construction (round-5
  provenance wording — §5 anti-vacuous bullet); the enum engaged-event +
  negative-pin goldens run against it (the legacy fixture cannot engage
  either arm — legacy dedup path + top_k=None).
  Ships the dominant-PYTHON-cost k=5/6 win with a proof that is PURE
  INTEGER — the safest possible proof class. (Scope honesty, round-1: at
  production 1m/2m/3m/5m windows the retained O(n_bars) prefilter floor
  governs total cost — §0/§9; Slice 1's grid-level impact concentrates on
  high-TF tuples and on deleting the Python term everywhere.)
  **Slice 1 additionally binds the "Slice-1 substrate + ordering pins
  (lever-discovery fold, 2026-07-09)" section:** the LEX-RANK (NOT
  colex/standard-CNS) unranking MANDATE + its emission-order goldens
  (item 1), the condition-major transposed-bitplane internal substrate
  (item 2), and the streaming residual pins (item 4); the prefix-AND
  LEX-DFS pruning lever (item 3) is a SEPARATE post-proof commit, NOT
  part of the Slice-1 bit-identity proof.
- **Slice 2 — fold_block metrics core, default-OFF
  `BRUTEX_SWEEP_RUST_CORE`.** The fold kernel (consuming
  `simulate_one_core` + `drawdown_stats_checked` via `drawdown_stats_into`),
  the columnar ABI + layout golden (incl. the little-endian pin), combine
  wiring, the `_oracle_metrics_pure` mechanised-pure oracle + its
  AST/import pin, 64-mask dual-oracle burn-in (WIDTH-KEYED per kernel —
  round-4; enum shadow ranges ALSO width-keyed — round-5; the full
  state-setter test seam `reset_self_check_for_tests(width=…,
  total_remaining=…, plain_remaining=…, cap_remaining=…, latched=…)` +
  the shared warm-up-tuple fixture helper — round-5) + autorevert, the
  PERMANENT
  finalist belt pre-pass + the round-4 BOUNDED-ARM SERVE RECONCILIATION
  (rank-column reconciliation + gate re-verdict + candidate-finalist
  serve compares, `stream_rebuild_divergence` semantics — mechanised by
  the round-5 POSITION CARRY: the additive `BoundedFinalist.position`
  field, the ONE authorized `sweep_stream.py` edit, byte-neutrality +
  positional-mask-belt pinned) + the round-5 STALL-BUDGET GUARD
  (`stall_budget_exceeded` refusal + the `_effective_stall_seconds`
  drift pin — round-6: the ABSENT-SYMBOL arm (flat-`DEFAULT_STALL_SECONDS`
  deadline on a #1291-less tree; `stall_guard_unavailable` total-evaluation
  refusal), the ORACLE_NS clamp spec, the shadow kept-list repricing,
  and the §3 in-core `fold_work_ceiling`) + the 4-mask
  sampled population spot-check (+ the
  belt flag with its fail-ON parse dual + the inner-function tuple-restart
  refactor and its audit-shape pins), the `ts_uncertified` certificate
  gate, poison twins (fold AND enum, incl. the round-4 STREAM-ONLY
  nondeterministic poison on the bounded arm), cross-arm full-column
  hypothesis
  property, twin-run gates incl. `sweep_parallel` workers>1 (per-worker
  burn-in) — the real-data + workers>1 twins as REQUIRED ON-BRANCH gates
  with PR-body-recorded evidence (§7.2 round-6 venue law), the
  golden-grid twin in the CI lane —, forced-demotion bench twin, the round-4 cap-respected
  deterministic pins + module-scoped `#![deny(clippy::indexing_slicing)]`,
  and the `sweep_core.py` weekly-mutmut pair measured on-branch + wired
  (§7.6). Orthogonal to Slice 1 — either flag
  alone is a valid, proven configuration; all 4 arm combinations
  golden-pinned — DELIVERED (round-3) by the digest-variant goldens with
  per-arm positive AND zero-count negative engaged-event assertions plus
  the two disabled-arm poison goldens (§5 anti-vacuous bullet), not by
  byte-equality alone (bytes are arm-identical by construction and
  cannot discriminate).
  **INDEPENDENT-ORACLE MANDATE (Verifier B Slice-0 carry-forward —
  Verifier B Slice-0 re-pin, 2026-07-10; BINDING Definition-of-Ready
  item for this slice): Slice-2's Rust-arm differential must verify
  against a GENUINELY independent oracle — frozen-bytes golden corpus
  or a hand-rolled reference implementation — NEVER an oracle importing
  leaf helpers from live compute.py. Recorded counterexample: Verifier
  B's Pébay-m3 delta-sign mutant passed Slice-0's C23 differential 4/4
  (the oracle shared `_accumulate_moments`) and was killed only by the
  exact-value pins. This mandate sits ALONGSIDE the existing TradeFolds
  §1.3 pairing obligation (#1685 item (3)).**
- **Slice 3 — measured constant work, no semantic change.** Drawdown
  scratch-buffer chunk-level reuse (touched-index reset, equality-pinned);
  fused single-pass walk+drawdown ONLY if the per-bar fold order is provably
  byte-identical (else dropped); chunk-size tuning; PyBytes-vs-list encoding
  re-check; OPTIONAL bounded-arm routing for plan-batch tuples, licensed
  only by the dedicated plan-batch-vs-plan-bounded byte-equality pin
  (judge-2 condition). Each lever ships only with its equality property + a
  measured ratio.
- **Slice 4 — default-ON flips.** One ADR per flag after a soak window
  (grid-scale twin runs on real data on the real box + the weekly mutants
  lane green), mirroring DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001. Kill
  switches remain forever. Each flip ADR MUST state the §5
  residuals verbatim (belt = VALUE-level, with the round-4 comparand
  provenance stated per rank arm — batch: served object; bounded:
  rebuild + the serve-reconciliation triple; the NINE-fold list —
  membership (non-finalist-confined, round-4 re-scope) / persisted DSR
  (non-finalist population inputs) / sidecar sums / the
  zero_trade-misflag class / the round-3 burn-in-dilution cap-trip
  class / the round-4 bounded-arm serve residual (non-candidate
  finalists' non-retained non-gate-flipping streamed columns —
  effect-free on persisted/rank-bearing outputs) / the round-6
  ENGAGED-NET-REGRESSION class (the stall guard is a LIVENESS bound,
  not a net-win bound — an engaged mid/high-TF top=1000 tuple can be a
  net ~1.3–1.6× wall-clock regression, §5 note (b)) / the round-6
  RESTART-PATH stall class (a divergent build's belt-mismatch restart
  spine pass is unpriced by the guard, §5 note (d)) / the external-fold
  (ix) TALLIES-ONLY enumeration-divergence class (count-only
  mis-bucketing with correct kept set + correct Σ enumerated — sidecar/
  audit-payload bytes only; netted by the enum shadow in-window and the
  twin-run SIDECAR compare at ship time, teeth proven by the §7.1
  count-only poison golden — fix-forward item 1) — plus the
  round-6-SYMMETRISED enum residual (in-range KEPT-SET divergence —
  silent drops OR spurious keeps — with
  self-consistent counts; the total-count class is deterministically
  belted; never the withdrawn round-4 drops-only "strictly" wording),
  each netted probabilistically
  by the flag-inclusive spot-check and deterministically only by the
  flag-inclusive burn-in (with its round-3 non-trivial floor) + the soak
  twin-runs) — never the withdrawn
  "everything persisted is verified" claim — AND (round-2) present the
  MEASURED belt fraction at the operator's actual `--top` (top=1000 is
  the spec production sizing; round-6: a measured 20–50%-scale belt
  fraction on the operator's grid shape BLOCKS the flip absent an
  explicit accepted-cost statement in the ADR).
  **Slice-4 flip-ADR obligations — consolidated (Verifier A round-1
  fold, 2026-07-09; (a)/(c) restate obligations already scattered above
  as one checklist, (b)/(d) are NEW):**
  (a) the **MEASURED belt fraction at the operator sizing
  (`--top=1000`)** with the disclosed risk stated in the ADR verbatim:
  default-ON can be a NET **~1.3–1.6× wall-clock REGRESSION** on
  engaged mid/high-TF shapes (§5 residual (vii) — the stall guard is a
  liveness bound, not a net-win bound); the flip requires the
  measurement OR an explicit accepted-cost statement — never neither;
  (b) an **M4-Pro memory-bandwidth analysis for the in-kernel rayon
  lever** before that lever (or any topology reshaping licensed by its
  ratios) informs a flip decision: the ≈3.0–3.77× ratios were measured
  on shared 4-vCPU x86 sandboxes (fit_results_pinned.json), and M4-Pro
  memory-subsystem contention (unified-memory bandwidth shared with the
  8-worker pool, P/E-core asymmetry) is UNANALYZED — the ratios must
  not be assumed portable to the §8 box without either the analysis or
  a bench-m4pro measurement;
  (c) the **belt-exempt residual classes re-affirmed CARRIED VERBATIM**
  — the ADR restates the §5 NINE-fold residual list above word-for-word
  (never a summary, never "everything persisted is verified");
  (d) the **Neumaier/CPython-sum port carries a `sys.version_info`
  ceiling guard or a loud-degrade note** in the shipping wrapper: the §4
  row-6 port is token-for-token against CPython **3.12's**
  `builtins.sum()` float fast path (gh-100425), and a future CPython
  (3.13+) change to `sum()`'s summation algorithm would make the Python
  oracle bit-diverge from the frozen Rust port — the burn-in/belt would
  then SILENTLY AUTOREVERT the kernel process-wide (perf-only: results
  stay correct via the oracle, but the core arm dies quietly on every
  process). The flip ADR must show either the version ceiling guard
  (refuse/warn engagement on an unvalidated CPython minor) or the
  documented loud-degrade path (the autorevert ERROR event names the
  suspected sum-algorithm drift as a checkable cause). Mirrored in the
  §4 summation-algorithm law note.
  (e) **ONE identity-safe grid-end engagement ROLLUP (Verifier A
  round-2):** a single structlog INFO emitted at grid end carrying the
  parent-summed counters — `tuples_engaged` / `refused_by_reason{}` /
  `autoreverted` / `spot_check_sampled` — OUTSIDE the audit JSONL and
  OUTSIDE run_id (byte-identity untouched), so a 2-4 day grid answers
  "did the core help?" without log-grepping. Rationale: engagement is
  otherwise loud PER-TUPLE but silent in AGGREGATE — a silent
  starvation loop could deliver 1.0× with only scattered per-worker
  warns.
- **Named non-goals recorded for the queue (§10), not built:** hit-list
  intersection prefilter (the honest 1m-TF lever), streaming kept-mask
  iterator, Rust ranking, finalist projection from the core (Q6), Slice-3
  gate/term Rust feed (perfmax's — explicitly rejected for now: the overfit
  gate consumes per-survivor skew/kurt, so any "ResultMetrics only for
  finalists" plan must first re-derive the overfit inputs from returned
  moments and re-clear COMMON review; deferred with that precondition
  recorded).

---

## 7. Test plan (incl. mutation)

1. **Goldens:** `_GOLDEN_RUN_ID` (`f588194f…921a`) + `_GOLDEN_CONTENT_HASH`
   (`2387cc88…d147`) byte-unchanged on ALL arms (OFF / enum / core / both;
   wired + forced-fallback; SELL twin) — **round-3: on the LEGACY fixture
   the ON arms additionally assert ZERO engaged events + the
   `legacy_dedup_path` refusal (the fixture is legacy-dedup + top_k=None
   and CANNOT engage the core; §5); every engagement-dependent golden
   below runs against the DIGEST-PATH VARIANT
   (`_GOLDEN_RUN_ID_DIGEST`/`_GOLDEN_CONTENT_HASH_DIGEST`, OFF-arm-minted
   in Slice 1)**; per-arm engaged-event assertions BOTH-directional
   (exactly one own-arm event AND zero other-arm events — enum-only /
   core-only / OFF / both, the round-3 negative pins) + zero-autorevert
   assertions; the two DISABLED-ARM poison goldens (poisoned core under
   `CORE=0,ENUM=1`; poisoned enum under `ENUM=0,CORE=1` — pinned bytes,
   proving each flag alone prevents consumption — the §3 runbook lever's
   teeth); poison-during-burn-in / poison-past-burn-in-belt-on /
   poison-past-burn-in-belt-off twins (pinned / pinned+restart / hash
   MOVES) — the belt-restart twin ADDITIONALLY pins audit-line count == 3,
   SWEEP_COMPLETE tally-payload byte-equality vs the pure-Python arm, AND
   (round-2) that the restarted pass runs ZERO belt/spot-check/burn-in
   compares (core-arm-only + process-global-counter pins); a
   zero-trade-misflag poison INSIDE the burn-in window ⇒ pinned bytes +
   one autorevert (the round-2 membership teeth); a STREAM-ONLY
   NONDETERMINISTIC poison on the BOUNDED arm (round-4 — a test seam
   corrupting a finalist's `fold_block` output on the STREAMING pass
   while the `_rebuild_bounded_finalists` pass folds clean, past
   burn-in, belt ON) ⇒ pinned bytes + one `stream_rebuild_divergence` +
   tuple restart — the serve-reconciliation teeth: this exact poison
   PASSED the round-3 belt (rebuild-vs-oracle compared clean);
   the ENUM poison twin (dropped kept mask past enum burn-in ⇒ tallies +
   content hash MOVE) **plus its round-6 SPURIOUS-KEEP mirror (a
   domain-VALID poisoned `enumerate_prefilter` wrongly KEEPING one
   conflicted mask past the enum burn-in ⇒ tallies + content hash MOVE —
   teeth on BOTH directions of the kept-set-divergence residual)**; the
   **COUNT-ONLY (tallies-only) poison golden (external fold, fix-forward
   item 1 — the residual-(ix) teeth):** a domain-VALID poisoned
   `enumerate_prefilter` that mis-BUCKETS one drop (a min-hits drop
   incremented into `conflict_pruned`, kept set + Σ enumerated CORRECT)
   past the enum shadow ⇒ the trial-stats SIDECAR hash MOVES
   (n_conflict_pruned/n_minhits_dropped are persisted bytes,
   sweep_run.py:1593-1598 → trial_stats.py:178-180, consumed by
   sweep_verify.py:411-412) while the STORE content hash is pinned
   UNCHANGED — proving the twin-run sidecar compare has teeth in the
   direction every other gate is blind to (both prior enum poisons
   asserted store-hash movement; this one must not); an **INDEX→TUPLE-
   DIGEST checkpoint golden (Verifier B F1):** the fixture grid's
   positional checkpoint index → tuple-digest map byte-identical OFF vs
   ON (a tuple-enumeration reorder breaks the golden, never
   prefix-skips silently — §3); a **DUP-TS LAST-WINS golden (Verifier B
   dup-ts oracle (i), RT-37 fixture):** a duplicate-timestamp window
   fixture pinning the `build_ts_index` LAST-WINS resolved bytes (the
   LATER bar's close at the duplicated ts) on BOTH arms — the core arm
   serves it via the `ts_uncertified` whole-tuple fallback (§4 row 7),
   so the golden's teeth are against any future Rust index consumer
   that keeps the EARLIER index (which today passes every existing
   golden yet diverges — zero prior coverage); a **MASK-SERIALIZATION
   golden (Verifier B F5, RE-SPECIFIED per checkpoint-1 audit C3+C9):**
   the frozen u128-mask JSON bytes corpus (e) pins that the SHIPPING
   serialization bytes REPRODUCE EXACTLY on the conversion tree —
   `combo_digest`'s canonical payload with its bare-int `"mask": int(mask)`
   (sweep_cache.py:389-391) and the walk-forward hash's bare-int
   `"mask": required_mask` (sweep_driver.py:977+) FROZEN AS SHIPPED
   (bare-int is the frozen shipping behavior, not a pattern to copy),
   plus the run_id's 32-nibble hex `mask:%032x` (spec.rs:219) — while
   any serialization surface the conversion INTRODUCES must be the hex
   string (the golden is "the exact shipping digest bytes reproduce",
   NOT "hex everywhere" — the previous wording would have re-keyed
   SweepCache + WF identities; see the §12 re-scoped SERIALIZER LAW); a
   stubbed WRONG-total enum return ⇒
   `enum_total_mismatch` + spine serves + pinned bytes (round-4);
   `tests/golden/fixtures/` diff EMPTY incl. the
   400-case `vocab_golden_vectors.json`; determinism (two ON runs →
   byte-identical Parquet); layout golden pinning `FOLD_*_FIELDS` == the
   EXPORTED `SWEEP_CORE_FOLD_*_FIELDS` module tuples (round-4 — the
   Python-readable projection of the Rust consts, §1.1) == ABI version ==
   `sys.byteorder == "little"` == **`array('q').itemsize == 8` ==
   `array('d').itemsize == 8` (round-6 — the decode law's hardcoded ×8
   widths become tested invariants, exactly as the round-1 endianness
   pin did for LE)**.
2. **Twin-run gates (ship condition):** OFF vs ON store bytes +
   `FlushResult.content_hash` + trial-stats sidecar + ranked order equal on
   (a) the golden grid, (b) a real-data grid (≥2 TFs, both directions,
   k∈{1,2,3}, with/without SL/TP, zero-trade-heavy tuples,
   min_hits∈{0,30}), (c) a `sweep_parallel` workers>1 run — all within one
   build at one commit into separate results roots. **EXECUTION VENUE +
   EVIDENCE LAW (round-6 — per-PR CI has no lake data, so (b)/(c) cannot
   be CI gates, and the ship condition previously named no lane; the
   §7.10 differential-harness wording is the template):** arm (a) — the
   golden fixture grid, synthetic data — is the CI-LANE arm and runs
   per-PR; arms (b) and (c) are REQUIRED ON-BRANCH gates per slice
   (Slice 1: OFF vs ENUM; Slice 2: all four arm combinations), each one
   command on the lake-bearing box, with the exact commands AND both
   arms' `FlushResult.content_hash` + store-Parquet sha256 + sidecar
   hash values recorded VERBATIM in the PR body — the merge is blocked
   until the PR body carries that evidence (the §7.10 "one command,
   wall-clock measured and recorded in the PR body" standard applied to
   the design's central ship condition, so it can never degrade into an
   unverifiable claim at merge time). **Run-identity register
   carried verbatim in the PR body (grounding graft):** `code_commit_sha`
   is an injected input held CONSTANT by construction (both arms, one
   process/build/commit); the frozen goldens pin SYNTHETIC identity fields
   ("deadbeef"/"snap0001"/"ind-v1") so the literals are commit-independent;
   sentinel/store keys embed `code_commit_sha` so a new commit re-keys by
   construction and no cross-commit byte-identity is ever claimed; within
   one commit the twin arms use separate `--results-root`s (else the second
   arm legitimately sentinel-short-circuits). Plus an idempotence twin
   (ON-arm re-run over its own outputs → sentinel short-circuit, files
   untouched). **Plus the KILL-AFTER-FLUSH REDO cell (Verifier B F2,
   RT-09 — mandatory, not just clean twins):** on the CORE arm, kill −9
   strictly after `flush()` / before the sentinel, resume, assert clean
   redo with byte-identical artifacts + the corpus-(b) audit-chain shape
   `[SUPERVISOR_START, TUPLE_SKIPPED_RESUME, TUPLE_DONE,
   SUPERVISOR_COMPLETE]` (a non-bit-identical recompute here is a
   permanent grid-wide append-conflict abort — the cell is the proof it
   cannot happen; §3). **Twin-baseline sequencing (coordinator ruling
   2026-07-09):** the twin-baseline capture WAITS for product-audit-1's
   identity PR-1; Slice 0 may precede it (byte-neutral); Slices 1+ run
   their twins against the post-identity-PR-1 baseline.
3. **Property (hypothesis, `HYPOTHESIS_PROFILE=ci`):** cross-arm FULL
   `ResultMetrics` bit-equality (struct.pack keys, None-bits exact) over
   adversarial streams (random walks, ±0.0 pnl, all-win/all-loss,
   near-envelope 2^52±1 magnitudes, −0.0 return ties, ruin, >100% dd,
   Neumaier compensation-triggering squared-return streams (the
   `[1e16, 1.0, −1e16]` class + alternating huge/tiny), duplicate-timestamp
   windows — served via the `ts_uncertified` fallback on the core arm, so
   both arms provably yield the identical Python-spine bytes — exits at
   0/last); enumeration
   list-equality + CNS round-trip + ranged-vs-monolithic equality;
   dual-knob batch invariance (≥5 shapes each entry point);
   suffix-independence + P-06 suites pass UNMODIFIED (plus the full-spine
   suffix property extended through the core arm — statelessness:
   prefix-run ≡ prefix-of-full); `python_required`/`zero_trade` flag
   routing (flagged masks byte-equal to today's spine).
4. **Unit (100% line+branch; `tests/unit/test_engine_sweep_core.py` stem
   for the discovery gate + the engine static 100% gate):** every fallback
   reason branch (incl. `ts_uncertified` + `big_endian_host`); exact
   boundary duals (`hits == min_hits`, `end_equity ∈ {0,1}`, `std == 0.0`,
   `r == −0.0`, negative-numerator `//`); the belt-flag parse dual
   (unrecognised ⇒ ON + `belt_flag_unrecognised` warn-once; explicit
   `_FALSY` ⇒ OFF); the `_oracle_metrics_pure` FUNCTION-granular
   closed-set AST purity pin (incl. the out-of-set-call loud failure) + the
   poisoned-`sweep_drawdown_stats` runtime-independence pin; spot-check
   determinism (same tuple digest ⇒ same 4 masks) + spot-check mismatch ⇒
   disambiguate + latch + restart; **spot-check degenerate branches
   (round-2): >4-non-finalist fixture ⇒ `spot_check_sampled == 4`;
   all-finalist / kept==0 fixture ⇒ `spot_check_sampled == 0`, no crash;**
   **spot-check retained candidates (round-3): a BOUNDED-rank-arm tuple
   runs the SERVED-OBJECT compare (the retained candidate `ResultMetrics`,
   not a re-fold — spy-pinned zero extra `fold_block` calls post-rank);
   candidate retention bounded at 16 objects; the candidate draw is
   pre-rank over the kept list and identical across rank arms;**
   **belt disambiguation triple (round-2): OFF-arm==oracle≠core ⇒ latch +
   restart; OFF-arm==core≠oracle ⇒ NO restart + NO latch + one
   `belt_preexisting_divergence` ERROR + served bytes == today's spine;
   three-way ⇒ latch + restart + `belt_threeway_divergence`;** **belt +
   spot-check SKIPPED (with `belt_skipped_non_core`) on the restarted
   pass, on every fallback-reason tuple, and on latched processes;**
   **burn-in membership (round-2): a flag-served row inside the 64-mask
   window IS compared — a wrong `zero_trade` flag ⇒ mismatch + oracle
   ships + latch; the second tuple in the same worker process (and the
   restarted pass) runs ZERO additional burn-in compares (process-global
   counters, the §5 one-semantic rule);** **burn-in non-trivial floor
   (round-3): an all-zero-trade first tuple leaves the window OPEN — the
   next tuple's plain rows are still compared until 16 plain compares
   land; a 512-compare cap trip with the floor unmet ⇒ ONE
   `burnin_nontrivial_floor_unmet` warn, core stays engaged; a mixed
   tuple closes the window at ≥64 total AND ≥16 plain;** **burn-in
   width-keying (round-4): a process completing the u64 window that then
   engages a [64,128)-bit tuple runs a FRESH 64/16 window on the u128
   kernel (compares observed, pinned); a u64 autorevert latch does NOT
   latch — or certify — the u128 kernel and vice versa;
   `reset_self_check_for_tests(width=…)` duals;** **bounded-arm serve
   reconciliation (round-4): a stubbed stream-vs-rebuild rank-column
   divergence on a finalist ⇒ `stream_rebuild_divergence` + latch +
   tuple restart on the spine; a rebuilt finalist failing the
   `passes_gates` re-verdict ⇒ same path; a candidate-that-is-a-finalist
   runs BOTH the served-vs-rebuilt full-column compare AND the
   served-vs-oracle compare (spy-pinned — the round-3 exclusion is
   dead); batch-arm clauses (1)/(2) run as identities;** **`scans_run`
   seam pins (round-3): an all-zero-trade chunk decodes `scans_run == 0`,
   a mixed chunk `== n_plain_rows` (the zero-trade law's deterministic,
   merge-blocking net — §1.3; round-4: a bit1/envelope-tripped row
   counts NO scan — the scan-last-ordering law);** **enumeration exhaustion/progress
   (round-3): driver terminates on the in-band `exhausted` flag; a
   `k=0` config yields exactly `[mask 0]` end-to-end through the seam;
   degenerate env overrides (`BRUTEX_SWEEP_CORE_{ENUM,WORK}_CELLS=1`) ⇒
   lower-clamped + one `work_cap_clamped` warn (direction=low) +
   byte-identical completion, never a hang or an empty tuple; the
   `enum_no_progress` fallback belt branch covered via a stubbed
   no-progress return;** **cap-respected + total-reconciliation seam pins
   (round-4): every dual-knob-matrix call asserts `next_rank − start_rank
   ≤ max(1, max_enumerated)` and `(next_rank − start_rank) × n_bars ≤
   max_work_cells + n_bars` from the return tuple; the fold chunk-sizing
   function (`max(1, cells // max(1, n_bars))`) unit-tested directly
   incl. n_bars == 0 (no ZeroDivisionError) + a spy pin that no chunk
   fed to `fold_block` exceeds it; a stubbed WRONG-total return
   (Σ enumerated ≠ count_combos) ⇒ `enum_total_mismatch` fallback +
   spine serves + warn-once;** **round-5 pins: the bits=[]/k=0 seam
   vector — the empty-safe width expression selects u64 off bar_bits
   alone, the tuple ENGAGES, kept == [0], bytes == the spine's (beside
   the existing non-empty k=0 pin); the STALL-BUDGET GUARD — a synthetic
   low-work/high-top_k tuple shape (work bound at the 300 s floor,
   overhead bound above half of it) refuses with `stall_budget_exceeded`
   + warn-once + spine bytes, a high-work shape engages, and the
   `_effective_stall_seconds` DRIFT pin compares the seam's recomputed
   deadline against the real `ParallelGridDriver._effective_stall_seconds`
   over a grid of (work, env-override) inputs; `BRUTEX_SWEEP_STALL_SECONDS`
   raised ⇒ the same shape un-refuses (the knob flows through both
   formulas); enum-burn-in width duals — a process that consumed the u64
   shadow WINDOW (the fix-forward-item-4 MASK-COUNT floor: 2^16 masks,
   256-range hard cap per audit C51 — never the superseded 4-range window) then engaging
   a [64,128)-bit tuple opens a FRESH u128 window
   (`reset_enum_self_check_for_tests(width=…, masks_remaining=…,
   ranges_cap_remaining=…)` duals — the superseding signature, §5);
   burn-in STATE-SETTER duals — each window state the goldens force
   (closed / near-cap-floor-unmet / closed-u64-open-u128) is set via
   `reset_self_check_for_tests(width=…, total_remaining=…,
   plain_remaining=…, cap_remaining=…, latched=…)` AND the shared
   warm-up-tuple fixture reproduces natural closure once; POSITION-CARRY
   pins — `BoundedFinalist.position` byte-neutrality (bounded rank
   result order/scores byte-identical pre/post the additive field),
   `columns.mask_at(position) == mask` for every finalist, and a stubbed
   position-threading corruption fails the positional mask-equality belt
   deterministically;** **round-6 pins: MIN-HITS SHORT-CIRCUIT seam pins —
   a min_hits=0 call decodes `hits_counted == 0`, a min_hits>0 call
   decodes `hits_counted == enumerated − conflict_pruned` (the
   deterministic merge-blocking net for the §1.3 min-hits law — the
   `scans_run` standard on the enum entry point); STALL-GUARD
   ABSENT-SYMBOL duals — with the #1291 symbols monkeypatch-DELETED from
   `sweep_parallel` the guard computes against the flat
   `DEFAULT_STALL_SECONDS` deadline (a low-overhead shape engages, a
   belt-heavy shape refuses `stall_budget_exceeded` at the flat 300 s ×
   0.5 threshold), and with `DEFAULT_STALL_SECONDS` itself unresolvable
   (or an injected exception inside guard evaluation) the tuple refuses
   `stall_guard_unavailable` — never an uncaught crash; the
   `_effective_stall_seconds` DRIFT pin is `skipif`-SCOPED to
   symbol-present trees; **STALL-GUARD SYMBOL-PRESENT duals on a
   #1291-ABSENT tree (external fold, fix-forward item 10 — the
   symbol-PRESENT branch is source code in sweep_core.py, reachable on
   the actually-current tree ONLY by monkeypatch-ADDING the symbols, a
   test the plan previously never named, so the merge-blocking engine
   100% line+branch gate (ci.yml:388) would have failed as
   test-planned):** the work-scaled
   `max(stall_seconds, work/STALL_MIN_MASK_BARS_PER_SEC)` recomputation
   branch AND the round-5 "`BRUTEX_SWEEP_STALL_SECONDS` raised ⇒
   un-refuses" resolver-flow test run via monkeypatch-ADDED fake #1291
   symbols (`_effective_stall_seconds`, `STALL_MIN_MASK_BARS_PER_SEC`,
   the env resolvers) set onto `sweep_parallel` — 100% branch
   reachability on EVERY merge state; only the DRIFT pin (which needs
   the REAL function) stays skipif-scoped; **F27 window-length pins
   (Verifier B):** an EMPTY Python-side bar window reproduces the
   skip-not-error verdict at the SAME tuple with the core never
   constructed (spy-pinned zero core calls), and a stubbed
   n_bars()-mismatching core trips the named `bars_length_mismatch`
   refusal with spine-identical bytes;** ORACLE_NS clamp probes —
   `BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR=1` ⇒ clamped LOW to the
   committed default + one `stall_guard_clamped` warn (direction=low),
   a 10**12 override ⇒ clamped HIGH at 1000× + warn (direction=high),
   each with deterministic guard verdicts; MID-TUPLE ENUM-LATCH
   CONTINUATION pin — a stubbed shadow mismatch on range 2 of 4 ships
   the Python window, latches, completes the tuple byte-identical to
   the spine via the islice continuation, and the mixed-provenance
   total-reconciliation assert passes; `fold_work_ceiling` seam
   mapping — a stubbed in-core oversized-chunk Err maps to the named
   whole-tuple fallback + warn-once; `native_itemsize_mismatch`
   precondition — a patched itemsize probe refuses engagement with
   spine-identical bytes;** `unbounded_top_k` refusal
   (top_k=None ⇒ Python spine, warn-once, belt never runs); width
   selection keyed off max bit index (a [64,128) condition bit with
   u64-fitting bar_bits ⇒ u128 core); `_combo_error` fingerprint format
   pins; re-attribution (oracle
   replay, never adopted — a `fold_block` ValueError re-enumerates the
   chunk's masks and replays each through the Python oracle; first raiser
   gets the exact fingerprint); MemoryError-bare; watermark breach
   mid-core-loop → `SweepMemoryBudgetError`; latch continuation mid-tuple
   ordering; belt mismatch → tuple restart + byte-identical redo; ABI
   mismatch refusal; hand-crafted byte-block decode — **incl. (round-2) a
   truncated-but-stride-clean n−1-row block and a cross-buffer length
   mismatch, each ⇒ its NAMED `kernel_result_refused` reason, plus the
   `zip(strict=True)` belt, and (round-3) a flags byte with a reserved
   bit 2–7 set ⇒ `reserved_flag_bits` refusal**; warn-once flood
   discipline under chunked loops.
5. **Rust:** cargo test in-file exact-value pins with expected values
   GENERATED BY CPYTHON and baked as consts (riskmin graft — moments/
   percentile/streak vectors incl. ±0.0, denormals, alternating huge/tiny;
   Neumaier-compensation vectors for the downside Σr² fold, CPython
   `builtins.sum()`-generated; envelope AT 2^52 and 2^52+1;
   `.max()`-over-`if` tie-mutant-proof idioms); direct non-finite unit tests
   on the factored `fold_guard_finite` helper (§1.3 — the zero-survivor
   reachability requirement); CNS overflow-refusal pins (n=128 k=64 and
   kin ⇒ `Err`, never SIGABRT — checked-u128 binomials, §1.3); **per-INDEX
   refusal pins (round-2): `condition_bits=[100]` on the u64 core ⇒ `Err`,
   never a shift-overflow SIGABRT; a conflict-pair index ≥ width ⇒ `Err`;
   both baked beside the C(128,64) pin**; **round-3 pins beside them:
   `start_rank > C(n_distinct, k)` ⇒ `Err` (`rank_out_of_domain`), never a
   silently-saturated wrong kept set; `start_rank == total_count` ⇒
   `(0, 0, total_count, exhausted=true, hits_counted=0, [])`; the k=0 vector — `kept ==
   [0]`, `enumerated == 1`, `next_rank == 1`, `exhausted == true` (the
   legal `SweepRunConfig` k=0 arm, `combo_mask.py:677` doctest parity —
   validation must never tighten to `1 <= k`) — PLUS (round-5) its
   EMPTY-BITS twin `condition_bits=[]`, `k=0` (n_distinct=0, C(0,0)=1,
   same return shape — the in-core contract is already total on it; the
   crash lived in the SEAM's width formula, closed by the §5 empty-safe
   `default=−1` terms + the §7.4 seam vector); guaranteed-progress unit
   (a 1-cell work cap still enumerates ≥1 mask and advances next_rank);
   `scans_run` in-crate units (all-zero-trade chunk ⇒ 0; mixed ⇒
   n_plain_rows — kills a deleted-short-circuit mutant deterministically,
   §1.3/§7.6; round-4: an envelope-tripping/bit1 row counts NO scan —
   the §1.3 scan-last-ordering pin); **`hits_counted` in-crate units
   (round-6 — the min-hits law's deterministic net): a min_hits=0 range
   returns `hits_counted == 0` with kept == the conflict-free set; a
   min_hits>0 range returns `hits_counted == enumerated −
   conflict_pruned` — a mutant deleting the min_hits==0 guard is
   data-identical but flips `hits_counted` and dies here; the
   `fold_work_ceiling` unit — a chunk with `masks.len() × n_bars > 2^28`
   returns a whole-chunk `Err`, never a partial fold and never an
   unbounded GIL hold (§3)**; CAP-RESPECTED units (round-4):
   EXACT expected `next_rank` baked on CNS fixtures — a
   1-mask/1-cell-equivalent cap ⇒ `next_rank == start_rank + 1`, a
   k-mask cap ⇒ exactly k advanced, plus the two inequalities
   (`next_rank − start_rank ≤ max(1, max_enumerated)`;
   `(next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars`) on
   caps strictly smaller than the population — the deleted-bound-check
   and `>=`→`>` cap-compare mutant classes die deterministically, never
   as survivors of a results-only matrix; the module-scoped
   `#![deny(clippy::indexing_slicing)]` attribute PRESENT in
   sweep_core.rs (grep pin, §8 R4)**; proptest
   enumeration vs a naive combinations reference + unranking round-trip;
   fold vs a deliberately different-shaped in-crate reference fold; fuzz
   targets for both entry points (rust-fuzz-smoke lane — arbitrary
   columns/bits/config incl. adversarial (n,k) must return Err or Ok, never
   panic); clippy `-D warnings`; fmt; the FMA/reassoc grep pin extended to
   `sweep_core.rs` + workflows.
6. **Mutation:** cargo-mutants zero-survivor on `sweep_core.rs` joining the
   fno-math shard matrix. **Budget honesty (round-1 fix): the #1597 "+1
   shard" dial is ALREADY CONSUMED** — the matrix went 5→6 when the u128
   merge grew the scope to 599 mutants (`rust/fno-math/.cargo/mutants.toml`),
   and the current worst-shard +20% band sits ~86 min against the 120-min
   job cap. `sweep_core.rs` plausibly adds ~300 mutants (metrics.rs-like
   density), so the slice MEASURES its mutant count + per-shard time
   on-branch BEFORE merge; if the +20% worst-shard band exceeds the cap at
   6 shards, the SAME PR ships a NEW ledgered re-shard decision (6→7,
   projected ~95–100 min band) — never a silent reuse of the spent dial.
   Timeout-class control (round-1): enumeration/unranking loops are written
   as UPFRONT-BOUNDED `for` shapes (iteration count derived from
   `max_enumerated`/`k` before the loop, never an open
   `while next_rank < …` whose advancement a mutant can no-op into a 300 s
   `--timeout` burn), so loop-control mutants die in normal test time.
   Wrappers one-line delegates (UNVIABLE class); the Slice-0 compute.py
   refactor keeps its weekly mutmut pair zero-survivor; mutation-killing
   tests by design on every Python boundary (exact messages, exact
   thresholds — #1182 convention). **Equivalent-mutant remedy (round-2
   fix — the zero-survivor mandate is otherwise UNSATISFIABLE as written):
   two ported comparisons carry PROVABLY-equivalent cargo-mutants mutants:
   (a) the downside filter's strict `r < 0.0` → `<=` — the repo's own
   Python source documents exactly this equivalence
   (`compute.py:696-702` `# pragma: no mutate`: an admitted r == 0.0
   contributes r·r == 0.0 to the sum, unobservable — the divisor is
   n_trades, not len(downside)); (b) the Neumaier tie branch
   `|f_result| >= |x|` → `>` — both operands are non-negative magnitudes,
   equal magnitude ⇒ equal value ⇒ both branch arms compute bit-identical
   compensation on ties. **Tooling honesty (round-5 — the previous "cargo-
   mutants has no inline-pragma mechanism" premise was checkably FALSE):
   cargo-mutants (incl. the pinned 27.1.0) DOES ship an in-source skip
   mechanism — the function-granular `#[mutants::skip]` attribute. It is
   FORBIDDEN here, and that prohibition is the actual justification for
   the exclude_re remedy: the attribute is function-COARSE, so placed on
   the tiny factored helpers it would silently exempt WHOLESALE the
   killable threshold-value mutants (0.0→1.0 etc.) this plan explicitly
   requires to stay viable and killed — passing the zero-survivor gate
   while gutting its teeth. Per-MUTANT granularity requires `exclude_re`;
   the §7.5/#1186 CI grep-pin list is EXTENDED to refuse `mutants::skip`
   in `sweep_core.rs` so an implementer who discovers the attribute
   cannot "simplify" onto it.** The remedy is therefore the crate's
   documented-exclusion precedent
   (`.cargo/mutants.toml`'s "DELIBERATELY OUT — documented, not silent"
   compat/* pattern, applied surgically): both loops are factored into
   tiny named helpers (`downside_accumulate`, `neumaier_add`) with direct
   in-crate unit tests on every OBSERVABLE boundary (the threshold-value
   mutants 0.0→1.0 etc. stay viable and killed), and the two equivalent
   comparison-operator mutants get PRECISE `exclude_re` entries in
   `rust/fno-math/.cargo/mutants.toml`, each with a comment citing this
   analysis + the compute.py pragma — a ledgered per-mutant justification
   mirroring DEC-MUTATION-GATE-COMPUTE-001, never a file/blanket skip
   and never a function-level `#[mutants::skip]`.
   **The zero-trade short-circuit GUARD is deliberately NOT in this
   exclusion class (round-3): a mutant deleting the guard is
   data-buffer-equivalent but flips the in-band `scans_run` count (§1.3),
   so it dies deterministically in the in-crate unit test — the round-2
   law's own output-equivalent-mutant risk is closed by observability,
   not by exclusion.** Any
   OTHER sweep_core.rs survivor stays a hard red.** **Suite-runtime
   discipline under the mutants matrix (round-2 — the 6→7 re-shard
   projection implicitly held 29–40 s/mutant constant, but cargo-mutants
   re-runs the WHOLE fno-math suite per mutant, so the slice's new
   in-crate tests inflate ALL ~900 mutants, not just the new ~300):**
   the NEW sweep_core proptest targets read a DEDICATED
   `SWEEP_CORE_PROPTEST_CASES` env var (per-target `ProptestConfig`
   override), and the mutants workflow sets ONLY that var to a reduced
   case count (e.g. 32) for the mutants run — **NEVER the process-wide
   `PROPTEST_CASES` (round-3 fix: `PROPTEST_CASES` is crate-global, so
   the round-2 wording would have also cut the case volume of the
   PRE-EXISTING `metrics.rs` fused-vs-two-pass proptest
   (`rust/fno-math/src/metrics.rs:444-453`) under every one of the
   already-zero-survivor 599 mutants — proptest generation is freshly
   random per run, so any existing mutant whose kill depends on case
   volume becomes an INTERMITTENT survivor, i.e. a flaky scheduled-gate
   red on exactly the lane whose first green run is STANDARDS row 3's
   completion proof (#1597); the existing suite's knob stays untouched,
   grep-pinned: no `PROPTEST_CASES` in mutation.yml)**. **Round-4 — the
   round-3 reduction re-created the SAME intermittent-survivor class on
   the NEW ~300 mutants (a fresh-random 32-case run is exactly the
   volume-dependent-kill shape the round-3 fix was written against):
   TWO mandates close it. (a) FIXED SEED: the mutants lane sets a
   dedicated `SWEEP_CORE_PROPTEST_SEED` beside the case-count var,
   consumed by the same per-target config seam (a fixed-seed
   `TestRunner` RNG), so the reduced-case runs are DETERMINISTIC per
   mutant — same verdict on the merge-time measure run and every weekly
   run thereafter. (b) DETERMINISTIC-KILLER RULE: every anticipated
   sweep_core.rs mutant class must be killable by a DETERMINISTIC test
   (CPython-baked vectors, boundary pins, the round-4 cap-respected
   exact-next_rank units, `scans_run` units) — the proptests are a
   supplementary net, NEVER the sole killer of record; the
   measure-before-merge zero-survivor run executes with the SAME
   reduced+fixed-seed proptest configuration the weekly lane runs, so
   its verdict transfers instead of measuring a different suite.**
   **Python mutation lane (round-4 — `brutex/engine/sweep_core.py`, the
   module concentrating ALL safety routing (flag resolution, burn-in
   window closure, autorevert latch, belt disambiguation triple, decode
   refusal, chunk-size cap computation, the belt flag's inverted fail
   direction), was in NO mutmut lane, and §5 even used that absence as
   a load-bearing argument: the module JOINS the weekly python mutmut
   loop as its own pair (the #1597 timeout-wrapped-runner pattern),
   runtime MEASURED on-branch before merge within the weekly budget's
   split dial — if it does not fit, the SAME PR ships a ledgered
   budget/split decision, never a silent omission; zero-survivor on the
   pair is a Slice-4 flip precondition. The merge-time net stays the
   negative engaged-event pins + unit duals (mutmut is a scheduled
   lane, not a merge gate — stated).**
   The CPython-baked
   vector suites stay O(ms) each; the ≥10⁶-case
   differential harness lives PYTHON-side (§7.10) and NEVER runs under
   cargo-mutants. The measure-before-merge mandate measures the REAL
   per-mutant suite time WITH the new tests, across the full mutant set —
   the re-shard projection is re-derived from that measurement, never
   from the pre-slice per-mutant constant. STANDARDS row 3's completion
   proof is a green SCHEDULED mutation run — an overrun is a standing
   red, hence measure-before-merge.
7. **Bench:** per-op O(1) via `_o1_tiers.ratio_ceiling`/`cache_tier_ceiling`
   (per-kept cost flat vs grid size; per-(mask·bar) flat; session build
   O(n_bars) stated); speedup floor FAR under the measured ratio with the
   forced-demotion anti-vacuous twin (#1531 pattern); OFF-arm cost gate
   (identity + one env probe); audit O(1) (exactly 3 lines/tuple both
   arms, INCLUDING the belt-restart path); marshal-decode microbench;
   belt+spot-check cost bench (O(top_k × n_bars) + 4 oracle masks/tuple,
   flat vs kept-count; exercised at BOTH top_k=20 and the top=1000
   production-sizing shape — round-2; the 1m worst case stated);
   **zero-trade-mask cost pin — LANE-HONEST (round-2 pin, round-3
   re-laned): the primary MERGE-BLOCKING net for the §1.3 short-circuit
   law is now the deterministic in-band `scans_run` pins (§7.4/§7.5 —
   zero wall-clock dependence); the wall-clock pin (per zero-trade kept
   mask ≈ walk + the retained ~3–8 µs Python tail, §9 zero-trade row —
   catching the ~100–130 µs unconditional-scan regression) is env-gated
   to the `BRUTEX_M4PRO_STRICT=1`/`BRUTEX_M4PRO_ABS=1` bench-m4pro lane,
   because its required sensitivity (a 2.4–2.7× ratio) sits BELOW the
   shared-CI `_o1_tiers` 3–10× noise ceilings — a shared-tier version
   would be vacuous (a 2.6× regression passes a 3× ceiling), and the
   repo precedent (`test_write_consumer_overlap_bench.py`,
   DEC-HARDENING-LEFTOVERS-001) already env-gates cross-arm wall-clock
   medians off shared GHA for exactly this reason; the ceiling is sized
   to INCLUDE the retained per-mask Python tail so a correct
   implementation never false-fails**; FFI GIL-hold bench —
   **RE-LANED + DEMOTED (round-4): the ~100 ms ceiling is only ~1.4×
   over the ~70 ms nominal hold, BELOW the shared-CI `_o1_tiers` 3–10×
   noise doctrine, so as an unlaned shared-GHA gate it was either flaky
   or (if sized generously) vacuous; it now rides the bench-m4pro
   `BRUTEX_M4PRO_STRICT=1` lane (max observed hold ≤ ~100 ms at the
   default caps, regression-loud) with shared CI keeping only a
   catastrophic ≤ ~2 s backstop ceiling; the MERGE-BLOCKING liveness net
   is the deterministic in-band CAP-RESPECTED pins (§3/§7.4/§7.5 —
   `next_rank − start_rank ≤ max(1, max_enumerated)`, work-cells ≤ cap +
   one-mask slop, exact-next_rank cargo units, the fold chunk-sizing
   unit + spy pins — the `scans_run` standard applied to this law)** —
   plus clamped-override probes
   in BOTH directions: `BRUTEX_SWEEP_CORE_WORK_CELLS=2**44` engages the
   §3 upper clamp, and `=1` engages the round-3 LOWER clamp + the ≥1-mask
   progress law — each with one `work_cap_clamped` warning + byte-identical
   results.
8. **Chaos/integration:** kill −9 mid-tuple (worker + parent, mid-FFI and
   between chunks) → sentinel-absent full redo byte-identical; SIGABRT
   injection → BrokenProcessPool → #1291 quarantine path (against the
   post-merge shape once landed).
9. **Static:** ruff/ruff-format; `mypy --strict`; pyright baseline; jscpd
   ≤1% (combine SHARES code — designed for the gate); citation/DEC gates;
   `.pyi` + EXPECTED_BUILD sextuple; doctest gate.
10. **Differential harness lane (round-2 fix — the "≥10⁶-case
    differential harness" §4/§8-R2 lean on now has an EXECUTABLE home; no
    listed lane previously delivered it: the hypothesis `ci` profile is
    derandomized at default max_examples ≈ 100):** a dedicated SEEDED
    generator suite under `tests/rust_differential/` (marker
    `rust_differential`, excluded from the default per-PR selection),
    generating adversarial trade/return streams (the §7.5 vector classes
    at scale), computing Python-vs-Rust folds and comparing
    `struct.pack('<d')` bytes; failures dump seed + stream verbatim. Two
    tiers: a 10⁴-case seeded subset runs per-PR in ci.yml (budget
    measured, target ≤ ~60 s), and the FULL ≥10⁶-case sweep is a REQUIRED
    ON-BRANCH gate before the Slice-2 merge — one command, wall-clock
    measured and recorded in the PR body — then rides the weekly
    scheduled lane thereafter. It never runs under cargo-mutants (§7.6
    suite-runtime discipline).

---

## 8. Risk register (top 10)

| # | Risk | Mitigation |
|---|---|---|
| R1 | **compute.py zone/coordination** — the fold/combine decomposition sits outside our declared zone | Slice 0 lands ALONE first, tiny + pure-parameterisation, its own revertable commit with goldens+mutmut byte-neutrality proof; coordinator sign-off **GRANTED for the Slice-0 compute.py zone (2026-07-09, §11 ruling (b) — the originally-bundled sign-off request SPLIT: the Q7 rust/-zone ruling, gap #18, is now ALSO CLOSED — coordinator ruling 2026-07-08, re-confirmed 2026-07-09; the sweep-core rust/ edit zone = the NEW sweep_core module under rust/fno-math + the four trade_sim.rs pub(crate) visibility promotions, trading-core fenced OFF — see the §11 ruling (a) block for the verbatim ruling)**; the file is in NO other phase's zone (verified); *[HISTORY — superseded by the grant, retained per the final-lock fix round]* the pre-grant refusal fallback was Slice 1 only + a Slice-2 re-scope round |
| R2 | **Float divergence in the ported folds** (Welford order, Neumaier downside sum, percentile lerp, −0.0 accumulation) | ONLY 9 f64 columns exist; summation-algorithm law (§4 — builtin-sum ports as Neumaier, the round-1 catch) + token-for-token port + per-expression review checklist; CPython-generated exact-value consts incl. compensation vectors; ≥10⁶-case differential harness — now with an EXECUTABLE lane (§7.10: seeded generator suite, 10⁴/PR + full-sweep on-branch gate + weekly); 64-mask dual-oracle burn-in with the MECHANISED-pure arm (flag-served rows in-window, round-2); PERMANENT finalist belt (every persisted finalist VALUE oracle-checked forever — batch arm: the served object; bounded arm: the rebuild PLUS the round-4 serve reconciliation, so stream-vs-rebuild serve nondeterminism on a finalist's rank-bearing columns is caught deterministically) + flag-inclusive sampled population spot-check (candidate-finalists compared too, round-4); twin-run gate before any flip |
| R3 | **Sort-stability / signed-zero tie divergence** in p95/p5 | stable `sort_by` == stable `sorted` (never sort_unstable); −0.0 ties preserve input order in both; adversarial −0.0 property; NaN structurally excluded + `python_required` belt |
| R4 | **Panic/SIGABRT** (`panic="abort"`) from unvalidated input, overflow, alloc | boundary validation in pure Result cores; i128 accumulators + envelope flags (no unchecked hot-loop arithmetic); the crate's existing no-unwrap/no-expect deny lints PLUS (round-4) a MODULE-scoped `#![deny(clippy::indexing_slicing)]` mandated on `sweep_core.rs` — the crate has NO indexing lint today (`lib.rs:30-35` denies unwrap/expect/print/dbg/unsafe only) and a crate-wide flip is NOT retrofit-able (trade_sim.rs has 17 direct-index sites, metrics.rs 3, both consumed UNCHANGED), so out-of-bounds indexing in the new index-heavy surfaces (CNS unranking, columnar emit) is mechanized-lint-netted at module scope (`get()`/checked accessors in all new code; a CI grep pin asserts the deny attribute is PRESENT in sweep_core.rs), review-convention + fuzz remaining the net for the pre-existing kernels; fuzz; chunk-capped allocations; whole-chunk precondition pre-detect in the Python wrapper; #1291 quarantine + the C6 re-homed recovery pair (broken-pool quarantine / supervisor GAVE_UP — §3) + the runbook kill-switch lever |
| R5 | **Silent vacuous parity** (stale wheel / mis-wired arm serving Python while claiming Rust) | new-SYMBOL `_EXPECTED_COMPILED_SYMBOLS` handshake + `SWEEP_CORE_ABI_VERSION` in-band per block + engaged-event golden assertions + the hash-MOVES poison twin |
| R6 | **Belt mismatch mid-grid** — tuple restart churn on a genuinely-divergent build | restart is bounded (once per process — latch), deterministic, APPEND-FREE (belt pre-pass runs before the first store append — no ResultsStore run-id conflict), audit-shape-preserving (re-entry below SWEEP_START; 3 lines/tuple pinned), results-safe (Python spine authoritative), and **TERMINATING by construction (round-2): the OFF-arm disambiguation routes a PRE-EXISTING spine-vs-pure-oracle divergence to a no-restart distinct-ERROR branch (the core is not at fault, the served bytes ARE today's bytes), and the belt/spot-check are core-arm-only so the restarted spine pass runs no compare — a second mismatch is structurally impossible, never an unbounded loop**; one ERROR event; the soak window before any default-ON flip is the operational net |
| R7 | **Memory at k=6** — kept-mask list ~O(10⁷) Python ints per tuple | identical to today's survivor-list shape; Rust side transient per range; `plan_rank_memory` + bounded watermark untouched; streaming kept iterator + optional bounded-arm routing named as measured Slice-3+ levers |
| R8 | **Composition drift vs #1295/#1291 + gap rows 3/4** | zero shared files (verified — both PRs touch no mask-loop file); core adds no cache keys, no run artifacts, no audit actions; `start_rank` never durable; re-verify file-disjointness at every slice's main-sync round |
| R9 | **Burn-in blind spot / GIL-hold stalls** | honest-limit disclosure + the layered net (belt = VALUE-level oracle proof of persisted finalist rows; sampled population spot-check; burn-in; twin-run/soak, property, conflict tripwire, R45 diffcheck — each honestly scoped per §5's round-1 restatement, incl. the named membership + enum-kept-set residuals); ALL FFI calls dual-work-bounded at the RE-SCALED caps (2^28 prefilter / 2^24 fold cells ⇒ ~≤100 ms/call sandbox — the round-1 review proved the earlier "~≤1 s / no worse than today's 256-mask chunks" claim quantitatively false at the 2^30/2^31 caps: a work-capped fold_block would have held the GIL ~3.6–4.3 s, ~50–300× today's 5–15 ms `_TRADE_SIM_CHUNK_CELLS` holds; withdrawn + corrected in §3); the crate has NO allow_threads, so the cap is the ONLY GIL lever (stated); "dual-work-bounded" enforcement points NAMED (round-4): enum cap IN-CORE, fold cap DRIVER-side chunk sizing (`max(1, cells // max(1, n_bars))` — §3's stated asymmetry, repo precedent sweep_run.py:203-204); cap-RESPECTED deterministic in-band pins are the merge-blocking liveness net (§3/§7.4/§7.5 — the round-3 pins all passed under a cap-IGNORING core), the GIL-hold bench re-laned to bench-m4pro as the secondary net (§7.7); **#1291 stall composition (round-5 — the previous "tuples get FASTER, the 250M mask·bars/s floor becomes more conservative" one-liner is WITHDRAWN as falsified by this design's OWN §5/§9 numbers):** `_effective_stall_seconds = max(300 s, max_inflight_work / 250M)` models mask·bars work ONLY, while the default-ON belt (~17–33 min/tuple @1m at the top=1000 production sizing, cli.py:385) + the 512-cap burn-in extension (~10–16 min worst @1m) run IN-WORKER before completion and are INVISIBLE to that formula — a low-k 1m tuple's work-derived deadline sits at the 300 s floor (k=2: ~9.8e8 mask·bars → 3.9 s → floor), so an all-low-k in-flight set would hit a deterministic `SweepStallError`, and since the stall path has NO quarantine and the aborted tuple wrote no sentinel, an identical resume re-runs the same belt/burn-in and aborts again — an abort-resume LIVELOCK introduced by our own safety machinery; on the core arm such tuples are strictly SLOWER than today, not faster. CLOSED — for the HEALTHY-build overhead cause — BY the §5 STALL-BUDGET GUARD: engagement is refused (`stall_budget_exceeded`) whenever the conservative overhead bound exceeds half the tuple's own recomputed effective deadline — the refused tuple runs today's spine at today's timing, so the composition holds by construction FOR THAT CAUSE (round-6 scoping: the guard is a LIVENESS bound, not a net-win bound — engaged mid/high-TF top=1000 tuples can still be a net ~1.3–1.6× regression, the named §5 residual (vii); and on a genuinely-DIVERGENT build the belt-mismatch restart's unpriced spine pass keeps a stall abort reachable on the incident path — residual (viii), remediated by the extended breaker-runbook lever naming `BRUTEX_SWEEP_RUST_CORE=0` after repeated stall-after-restart incidents). The guard itself is TOTAL on every tree (round-6): the #1291 symbols resolve via getattr with an ABSENT-SYMBOL arm computing against the flat `DEFAULT_STALL_SECONDS` — the exact deadline a pre-#1291 parent enforces — and any guard-evaluation failure is the named refusal `stall_guard_unavailable`, never a crash; the remediation knobs `BRUTEX_SWEEP_STALL_SECONDS` / `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC` are NAMED (raising the floor flows through both formulas identically), the `ORACLE_NS_PER_BAR` override is clamped so it can only RAISE pessimism (§5 round-6 clamp spec), and the Slice-4 flip ADR must present the MEASURED max belt+burn-in phase duration vs the effective deadline on the soak grid at the operator's actual `--top` |
| R10 | **cargo-mutants budget overrun** on the sharded weekly matrix | measure mutant count + shard time on-branch BEFORE merge; the #1597 "+1 shard" dial is SPENT (5→6 at the u128 merge — round-1 correction; worst-shard +20% band already ~86/120 min), so an overrun at 6 shards ⇒ a NEW ledgered 6→7 re-shard decision in the SAME PR; upfront-bounded `for`-loop shapes kill the 300 s infinite-loop timeout-mutant class in the new enumeration surface; logic-out-of-wrappers keeps the viable-mutant count tight |

Secondary (tracked): warn-once flood discipline (per-(tuple, reason) memo);
`maybe_guard_bars` ON-mode teeth reduced to the first sequential pass for
core-native reads (same disclosure ResidentTradeSim already carries —
documented, not silent); enum burn-in cost (mask-count-floored per fix-forward item 4: ~2^16
shadowed masks per process per engaged width — ≈1.1 s @48k / ≈9.5 s @555k,
256-range hard cap (audit C51 — the 64-range cap made the floor unreachable
on 1m production shapes); stated in §5); metrics burn-in + belt + spot-check cost (quantified per §5/§9 —
the round-1 addition); the per-tuple (vs per-call) env read documented +
pinned.

---

## 9. Expected speedup — honest, with error bars

All sandbox rates from `baseline.md` §2 (48k-bar window unless stated);
M4-Pro absolutes NOT claimed until the bench-m4pro lane measures them.
**Parallel efficiency quoted at ~6.5×, never 8×** (single writer-actor
fsync + reorder + marshalling eat ~20–40% — the in-repo `sweep_parallel`
honesty envelope; multiply ideal-scaling wall-clocks by ~1.15–1.35).

| Quantity | Today (sandbox) | Post-conversion (projected) | Basis |
|---|---|---|---|
| Per KEPT **TRADE-BEARING** mask @48k bars (round-5 scoping: this row — and any relayed per-kept headline — covers trade-bearing masks ONLY, the 48k fixture @43.6 trades/mask; the ~66%-zero-trade kept tail is the ≈1.0× row below, and the honest whole-tail figure is the BLENDED row; **round-7/§9.1 qualifier: ALL µs constants in this row are UNPINNED-config sandbox rates — campaign math uses ONLY the §9.1 two-term pinned law**) | ≈ 400 µs (the baseline "Marginal total per KEPT mask", which SUMS the retained enum 2.46 + Stage-1B prefilter 12.5 + rank 1.29 µs alongside walk/marshal/churn/metrics) | **Slice 2 ships: ~193–198 µs ⇒ ~2.0–2.1×. WITH the CONDITIONAL Slice-3 scratch-reuse lever (a measured, ships-only-with-its-equality-property+ratio lever — §6): ~168–198 µs ⇒ ~2.0–2.4×. (Final-lock recomposition: endpoints re-summed from this row's own basis column at the ≈11–13 µs combine residue.) (Round-7 re-derivation, fix-forward item 6 — the round-6 endpoints rested on the combine ≈ 3 µs lower bound §0 itself WITHDRAWS: at the doc's own measured ≈10 µs intercept plus the ~1–3 µs relocated per-mask seam cost the "~2.2×"/"up to ~2.5×" endpoints are unreachable and are WITHDRAWN — neither they nor the blended "≈1.8×" derived from them may be relayed; the honest bands stand as stated until the mandated on-branch re-measure lands, and the flip ADR quotes the band of what ACTUALLY shipped. The round-6 basis repair — omitted prefilter/rank segments, eliminated exits-marshal — stands.)** | walk 62 µs (unchanged; UNPINNED-config figure — §9.1) + drawdown scan COMPUTE ~105 µs (marshal excluded — eliminated; → ~80–105 µs with Slice-3 buffer reuse) + retained Stage-1B prefilter ~12.5 µs (min_hits>0 — ZERO in both columns at min_hits==0 per the §1.3 skip law) + retained Python rank ~1.3 µs + Rust folds ~1–3 µs + marshal/decode ~0.1–0.7 µs + Python combine+append+relocated-seam ≈ 11–13 µs (fix-forward item 6 + final-lock recomposition — the measured ≈10 µs intercept of the two-point fit PLUS the ~1–3 µs relocated per-mask seam: decode-row access, zip(strict), flag branch, kept-PyLong churn from the Vec return + fold_block re-marshal; the "~3–10 µs" basis carrying the withdrawn 3–6 half is deleted, and the interim "≈10–13" quote dropped the mandatory seam term at its lower endpoint — 10+1=11 is the composed floor); ±30% on unmeasured fold/decode constants; the walk+scan floor is hard |
| Per KEPT ZERO-TRADE mask @48k bars | ≈ 70 µs (walk ~62 µs + 7.6 µs Python FLAT tail — NO drawdown FFI, `rust_metrics.py` shortcut; ~66% of the profiled fixture tail) | **≈ 65–70 µs ⇒ ratio ≈ 1.0× — NOT a gain (round-3 correction of the round-2 "walk-only ~62–65 µs" cell, which omitted the per-mask Python tail §1.4 itself RETAINS: the decode-row access, the flag branch, the literal `_empty_metrics(FLAT_DRAWDOWN_STATS)` 37-field construction, and the yield/append — most of today's 7.6 µs tail, which baseline.md marks "no FFI", so the conversion deletes only the dispatch/`_exit_pairs` slice of it, ~0–4 µs). The row's claim is REGRESSION AVOIDANCE, not speedup: WITHOUT the §1.3 short-circuit law the cost is ≈ 167 µs — a 2.4× REGRESSION vs today on the majority tail, invisible to every byte gate — hence the `scans_run` pins + the M4-Pro-laned §7.7 cost pin (sized to include the retained tail, so a correct implementation never false-fails)** | round-2 addition, round-3 corrected; baseline.md §2; min_hits=0 grids keep EVERY conflict-free mask |
| **BLENDED per KEPT mask at the profiled 66/34 zero-trade density (round-5 — the honest whole-kept-tail headline, composed from this table's OWN two rows at its own density: `0.66 × PK_zero + 0.34 × PK_trade`)** | ≈ 0.66×70 + 0.34×400 ≈ **182 µs** | **≈ 0.66×67 + 0.34×196 ≈ 111 µs ⇒ ~1.6× (Slice 2); ~1.6–1.8× with the conditional Slice-3 lever (final-lock recomposition: PK_trade_post ~168–198 µs ⇒ blend ≈ 0.66×67 + 0.34×(168–198) ≈ 101–112 µs ⇒ ≈1.6–1.8× — the previous "~1.6–1.7×" cap was computed at the SLICE-2 PK_trade basis and contradicted this table's own Slice-3 trade-bearing endpoint; the round-6 "≈1.8×" REMAINS non-relayable AS the withdrawn combine-≈3 µs derivation — the corrected basis independently reaches ≈1.8× only at the Slice-3 optimistic endpoint, quotable solely as this recomposed band with the Slice-3 conditionality stated) — MATERIALLY BELOW the trade-bearing ~2.0–2.1× headline, which must never be quoted as the whole-tail figure** | `rust_metrics.py:174` (66% of the profiled fixture tail zero-trade); §1.3 zero-trade law (min_hits=0 grids keep every conflict-free mask); zero-trade density is mask-shape-correlated, so the 66/34 split is the PROFILED-fixture anchor, not a universal constant — per-grid density is measured by the required on-branch sub-grid run |
| Per ENUMERATED non-kept mask | 2.4 µs | **~0.03–0.15 µs (≈20–80×)** | + the per-survivor prefilter O(n_bars) count (0.257 ns/bar·mask) **on `min_hits > 0` grids only — exactly as today there; at `min_hits == 0` the count term is ZERO in BOTH columns (today's `if cfg.min_hits > 0` guard / the §1.3 MIN-HITS SHORT-CIRCUIT LAW — round-6 scoping of the previously-unscoped "exactly as today")** |
| Blended per-ENUMERATED combo (mid/high TFs, k=5/6) | **bottom-up formula (round-2 REBUILD; round-5 ZERO-TRADE SPLIT — the single `f·PK` term conflated trade-bearing and zero-trade kept masks, systematically overstating the per-kept term wherever zero-trade density is high, i.e. every min_hits=0 grid): T_today ≈ 2.4 µs + s·(0.257 ns × n_bars) + f_zero·PK_zero_today(n_bars) + f_trade·PK_trade_today(n_bars)** — @48k, f→0: ≈ 14.9 µs; @3k (high TF), f→0: ≈ 3.2 µs | **T_post ≈ 0.03–0.15 µs + s·(0.257 ns × n_bars) + f_zero·PK_zero_post(n_bars) + f_trade·PK_trade_post(n_bars)** — **the `s·(0.257 ns × n_bars)` prefilter term appears in BOTH columns iff `min_hits > 0`; at `min_hits == 0` it is ZERO in BOTH (today's guard / the §1.3 skip law — round-6: the unscoped formula mispriced every min_hits=0 sub-grid in both columns, and the combo_counts rebuild inherits the SCOPED form)** — @48k, f→0, min_hits>0: ≈ 12.6 µs (~1.2×, prefilter-floor-bound); @3k, f→0: ≈ 0.8–0.9 µs (~3.5–4×, enum-dominated); at f>0 the blend interpolates toward the BLENDED per-kept row (~1.6× at the profiled 66/34 density), reaching the trade-bearing band (~2.0–2.1× Slice 2; ~2.0–2.4× only with the conditional Slice-3 lever — round-7 item-6-corrected bands) only as f_zero→0. **Per-enumerated FFI CALL-COUNT tax, stated (fix-forward items 5+9):** at min_hits==0/large-n_bars the min-hits-blind work-cells charge (§1.3 charging rule) forces ~2^28/n_bars-mask calls (~483 @555k ⇒ ~103,600 calls per C(60,6) tuple vs ~12,200 at the max_enumerated floor), each paying ~µs FFI overhead + per-call re-marshal of condition_bits/conflict_pairs + the Python loop turn ⇒ a ~4–10 ns/enumerated-mask overhead NOT in the ~0.03–0.15 µs row — a 3–30% tax on the per-enumerated win on exactly those grids (~0.5–1.5 s/tuple, safe direction: shorter GIL holds) | s = conflict-free fraction, f_zero/f_trade = zero-trade/trade-bearing kept fractions (f = f_zero + f_trade) — STATED UNKNOWNS for real data at k≥4; all four PK constants are already measured in baseline.md §2 (400 µs @43.6 trades vs 7.6 µs zero-trade tail + 62 µs walk). **This SPLIT formula is the NORMATIVE tool for the round-2-mandated combo_counts.md §3 rebuild** — the rebuild measures s, f_zero, f_trade per sub-grid shape (the on-branch wall-clock run nets any residual error directly). **The round-1 "today ~0.58 ms [AUDIT-RELAYED]" cell is WITHDRAWN as unit-conflated: SPEC §5 defines 0.58 ms as the per-KEPT-MASK residual, and pairing it with an ENUMERATED-mask population made the row arithmetically impossible under this table's own per-kept cap (today=580 µs requires f·PK_today ≥ 565 µs ⇒ post-blend ≥ ~204 µs, never 10–50 µs)** |
| Grid wall-clock, scenario B k≤5 / k≤6 | **NOT DERIVABLE from this document set's measurements (round-2):** the prior "~15.2 h / ~5.9 d" anchors were `0.58 ms(per-KEPT) × enumerated-count ÷ workers` arithmetic (combo_counts.md §3 — same unit conflation) and are WITHDRAWN with the constant | **grid-level headline DEFERRED to a REQUIRED measurement: before the Slice-1 merge a representative sub-grid (≥2 TFs incl. one 1m tuple, k∈{2,5}) is timed on-branch BOTH arms — AND (fix-forward item 7) additionally at the operator's actual `--top` (top=1000), because a default-top measurement cannot expose the stall-guard's top=1000 refusal/engagement pattern — and combo_counts.md §3 is rebuilt bottom-up from the measured s/f_zero/f_trade and the round-5 SPLIT formula row above (never the withdrawn single-f form); the previous 15.2 h→2–5 h and 5.9 d→0.7–2 d headline PAIRS are withdrawn — the surviving honest claims are the per-TERM measured ratios above and the §9.1 BINDING campaign band (≈1.3–3.1× from the port alone; ≈2.3–2.5× at median density — RATIO band ONLY: the day-count pair "k≤6 ≈5.9 d → ~2–3.5 d" is WITHDRAWN by the final-lock fix round, because its 5.9 d "today" anchor traces solely to combo_counts.md §3's 0.58 ms(per-KEPT)×enumerated-count arithmetic — the EXACT unit conflation this row's Today column withdraws — and perf_reconcile/RECONCILIATION.md derives ratios only, no day count; absolute k≤6 day figures are DEFERRED to this row's required on-branch measurement + the combo_counts.md §3 rebuild)** | round-2 finding: the two columns of the old rows described different populations; no blended/grid speedup is quotable until s and f are measured |

**MANDATORY 1-minute-TF caveat wherever any blended per-combo figure (the
formula row's evaluated examples, or the legacy 10–50 µs band — now
withdrawn as a headline and legal only as a formula output at stated
s/f/n_bars) is quoted:** every
surviving per-mask operation is O(n_bars); at the 1m TF's ~555k-bar
2020→2026 window even the cheapest prefilter count costs ~143 µs per
surviving-enumerated mask and the walk ~710 µs per KEPT mask (sandbox,
UNPINNED count-only rate — under the §9.1 PINNED production config the
walk is ≈6.2 ns/mask·bar ⇒ ~3.4 ms per kept mask @555k, plus
≈4.1 µs/trade) — no
flat 10–50 µs/combo is possible there; **Rust cannot remove O(N_bars)**. The
hit-list-intersection prefilter (integer-exact, kept-set-identical) is the
named, out-of-scope lever against that floor (§10). Non-per-combo overheads
(Stage-1A ~14.5 µs/bar amortised, per-tuple flush/fsync/sentinel/3 audit
lines, and the O(top_k × n_bars) finalist projection — minutes-to-hours per
grid at the institutional top=1000, open_questions Q6) do NOT shrink with
the per-combo constant.

**Correctness-overhead accounting (round-1 addition; round-2 scoping):**
the 64-mask dual-oracle burn-in ≈ 3–10 s per worker process @20–48k bars,
up to ~1–2 min at the 1m TF — plus (round-3) the non-trivial-floor
extension: on zero-trade-heavy starts the window stays open until 16
PLAIN rows are compared, hard-capped at 512 total compares; the extra
compares are zero-trade-cheap (~walk-only each), so the worst-case
burn-in cost grows well under 8× and stays process-bounded —
**ONCE PER PROCESS PER KERNEL WIDTH, normative** (the burn-in
counters are process-global single cells keyed per width — round-4;
the u128 window is production-unreachable today, IMPLEMENTED_MASK <
2^60, so the practical cost is once per process; the §5 restart
paragraph's tuple-scoped fresh-state list explicitly excludes the
counters — round-2 fix of
the round-1 self-contradiction; ProcessPoolExecutor reuses workers, and
§7.4 pins zero compares on the second tuple); the
finalist-belt pre-pass ≈ 2–4 s/tuple @48k bars **at top_k=20** (worst
~40 s at 1m; the round-4 serve reconciliation adds O(top_k)
struct.pack column compares + `passes_gates` re-verdicts — ~µs/finalist,
invisible beside the belt's oracle re-sims, a claim LICENSED (round-5) by
the `BoundedFinalist.position` carry: without it the reconciliation was
O(kept) or a ~GB off-watermark dict on exactly the bounded arm — §5);
**at the top=1000 PRODUCTION sizing (cli.py:385) the belt is
~100–200 s/tuple @48k / ~17–33 min/tuple at 1m — up to ~20–50% of a k≤5
grid, NOT low-single-digit (round-2 fix): the "low-single-digit percent"
aggregate claim below is SCOPED to top_k ≤ 20, and the Slice-4 flip ADR
must present the MEASURED belt fraction at the operator's actual top;
top_k=None never reaches the belt (engagement refuses it,
`unbounded_top_k`)**; the 4-mask population spot-check ≈ 0.4–8 s/tuple
(TF-dependent; 0 on empty-pool tuples — `spot_check_sampled` says so).
The belt/spot-check/burn-in all run on the CORE ARM ONLY (§5) — fallback
and restarted-spine passes pay none of this. All bounded, all TF-scoped,
none grow with grid size per tuple; on multi-hour grids at top_k ≤ 20 the
aggregate is low-single-digit percent, on 1m-heavy small grids (or at
top=1000) it is a visible, stated fraction — included in the soak twin-run
measurement before any default-ON flip. **Round-5 stall composition (round-6 scoping): this
whole overhead block is additionally BOUNDED per tuple against the #1291
stall deadline by the §5 STALL-BUDGET GUARD — a tuple whose conservative
overhead bound exceeds half its own recomputed effective deadline never
engages the core (`stall_budget_exceeded`; today's spine, today's
timing) — so the belt/burn-in can never convert a low-k 1m tuple into a
`SweepStallError` abort-resume livelock ON A HEALTHY BUILD. The guard is
a LIVENESS bound, NOT a net-win bound (round-6 — §5 note (b)): an
ENGAGED mid/high-TF tuple at top=1000 sits far under its work-derived
deadline (the 2.5e8 floor is ~10–15× below the real kernel rate there)
yet carries the belt as strictly ADDITIONAL cost, a net ~1.3–1.6×
wall-clock regression vs today's spine on such shapes — the named §5
residual (vii), mitigated only in AGGREGATE by the flip ADR's measured
belt fraction; and on a genuinely-DIVERGENT build the belt-mismatch
restart's spine pass is unpriced (residual (viii), §5 note (d) — loud,
results-safe, breaker-runbook lever). The Slice-4 flip ADR presents
the MEASURED max belt+burn-in phase vs the effective deadline at the
operator's actual `--top` on the soak grid.**

The §8 "2–4 days for ~33B trials" SLA is NOT claimed
met by this change; every table above is a PROJECTION until the twin-run
soak measures it on the real box.

### 9.1 PERF RECONCILIATION — BINDING two-term cost law + campaign band (Verifier B, RESOLVED 2026-07-09)

The guarantee-session-vs-this-track walk-rate discrepancy (≈1.28 vs
≈12.8 ns/mask·bar — 10× apart) is **RESOLVED** by side-by-side reproduction
on identical fixtures (evidence: `perf_reconcile/RECONCILIATION.md`); both
sides independently CONFIRM one two-term law. These numbers are BINDING for
every table in this document — anything derived from the earlier unpinned
rate is qualified, never campaign math:

- **Two-term cost law (pinned production config,
  `RAYON_NUM_THREADS=1` — sweep_parallel.py:842):**
  `cost(mask) = c0 + ≈6.2 ns/mask·bar (walk) + ≈4.1 µs/trade` on the
  production arm (0.35 µs/trade on the raw-kernel arm). R² per pinned
  tier (final-lock correction — the earlier blanket "R² ≥ 0.993"
  overstated the production-arm fit): production arm (prod_wrapper, the
  fit the ≈4.1 µs/trade constant comes from) **0.9927 — strictly below
  0.993** (rms resid 7.8%, max |resid| 20.8%, RECONCILIATION.md §4);
  count tier 0.99935; raw tier 0.99901
  (perf_reconcile/fit_results_pinned.json).
- **The earlier ≈1.26–1.28 ns/mask·bar "walk floor" (baseline.md) was
  UNPINNED (free rayon) AND count-only** — every quote of it now carries
  that qualifier (§3 fold-cell sizing, §9 rows, the 1m caveat) and it must
  NEVER be used in campaign math. Unpinned/pinned rayon ratio ≈3.0–3.8×
  (bar-count dependent).
- **Campaign band from the FFI port ALONE: ≈1.3× (sparse @604k bars) to
  ≈3.1× (short windows); ≈2.3–2.5× at the FIX-L k=2 MEDIAN density
  (1,532 trades/mask, 7.7% zero-trade).** The day-count pair previously
  quoted here ("k≤6 ≈5.9 d today → ~2–3.5 d from the port alone") is
  **WITHDRAWN (final-lock):** the 5.9 d "today" anchor has NO derivation
  in this document set outside combo_counts.md §3's withdrawn
  0.58 ms(per-KEPT)×enumerated-count arithmetic — the exact unit
  conflation the §9 grid row withdraws — and RECONCILIATION.md derives
  ratios only; the BINDING band is the RATIO band above, and absolute
  k≤6 day figures are DEFERRED to the §9-mandated on-branch sub-grid
  measurement + combo_counts.md §3 rebuild. A day-level-k≤6 claim is
  therefore not derivable from the port band alone either way; the
  ALGORITHMIC levers remain FIRST-CLASS design space regardless (§10):
  **parallelism topology** (8×1 workers×rayon today; in-kernel rayon is
  worth ≈3.0–3.77× on the walk for heavy tuples MEASURED in this
  document set's artifacts — 3.77 @48.6k · 3.69 @93.9k · 3.51 @300k ·
  3.00 @604k bars, fit_results_pinned.json; the ~3.97× endpoint is
  COUNTERPARTY-RELAYED, confirmed only "within shared-vCPU noise"
  (RECONCILIATION.md §0) and consistent with the ≈3.0–3.8×
  unpinned/pinned ratio above — final-lock relabel of the former
  "MEASURED ~3.0–3.97×" — reshaping topology PER-TUPLE
  is a measured lever, licensed only with the order-preserving
  `IndexedParallelIterator` constraint of §3), **entry-sparsity
  exploitation**, and **kept-vs-enumerated work shaping on coarse TFs**
  (1day k=3 keeps 46.4%).
- **ACCEPTANCE CRITERIA (ship gates):** (1) **S2 gate — DEFINED
  (final-lock; the fold had imported the gate underived, with a
  "per-tuple" the verifier source never stated, and — taken literally
  against this doc's own permanent-belt accounting — unsatisfiable):**
  on ONE FROZEN dense trade-bearing fixture at 604,485 bars (the S2
  fixture — the heavy 1m-class shape of the guarantee session's
  correction, dense masks at ~1.4k trades/mask; mask count / min_hits /
  top_k are pinned in the gate's bench file when the mandated on-branch
  Slice-1 measurement freezes it), the post-port INCLUSIVE steady-state
  mask-loop time (summed over the fixture's masks) must be ≤ ~1.15× of
  the SAME-fixture kernel-time SUM, both sides measured on-branch in
  the same process. **SCOPE (binding):** the gate times the KERNEL+SEAM
  path ONLY — burn-in windows CLOSED, the finalist belt + population
  spot-check phases EXCLUDED and timed separately — because the belt is
  a PERMANENT, deliberate correctness overhead (~2–4 s/tuple @48k
  top_k=20, ~100–200 s/tuple at top=1000 — the §9 correctness-overhead
  block; residual (vii)'s net ~1.3–1.6× regression on engaged top=1000
  shapes stands) that no 1.15× kernel ratio can contain: an
  inclusive-with-belt reading can NEVER satisfy this gate and is not
  what it gates. The gate proves the PORT strands no measurable Python
  residue on the mask loop; it is NOT a net-win gate. **ANCHOR HONESTY:**
  the "~14.0–14.5 ms @604k dense" figure is COUNTERPARTY-RELAYED —
  ≈1.15× of the guarantee session's own ~12.2–12.6 ms kernel sum at
  their ~1,423-trades/mask density (external_findings_verifier_b.md;
  perf_reconcile/RECONCILIATION.md contains NO S2 cell) — and does NOT
  recompose from the §9.1 two-term law at that density (which gives a
  ≈9.5–10.0 ms per-mask cost @604k at 1,423–1,532 trades/mask —
  ≈10.9–11.4 ms even at the gate's own 1.15× ratio — a cross-container
  offset RECONCILIATION.md §5 already flags as expected between
  containers); the BINDING form of the gate is therefore the
  RATIO against the same-fixture measured kernel sum, the ms figure
  illustrative only; (2) **trades NEVER cross the FFI** — the measured
  half-port counterexample (ClosedTrade materialisation left in Python)
  collapses dense-mask gains to 1.04–1.16×; `fold_block`'s columnar
  returns satisfy the criterion by construction, and any future "partial
  port" proposal is refused against this measurement.
- **Anomaly on record, DO NOT CITE:** the audit's FIX-M
  "4.35 ns/bar @250 trades" fits neither config (likely unpinned) —
  flagged to its authors.
- Any residual old-framing figure (the withdrawn 10–50 µs/combo band, the
  0.58 ms×enumerated rows, the unpinned walk floor) is LEGAL only as a
  historical formula output with stated s/f/n_bars + the pinned-config
  qualifier — never as a campaign or headline number.

---

## 10. ROADMAP — the wider Rust-conversion queue (context; NON-GOALS here)

Operator's standing decree (relayed via coordinator): "except the Groww
pull/fetch, EVERYTHING becomes Rust O(1)-per-operation", sequenced
EVIDENCE-FIRST:

1. **This sweep-core conversion lands PROVEN first** — it owns the measured
   ~85% of sweep wall-clock [AUDIT-RELAYED; independently re-derived at
   57–85% depending on window scale, `baseline.md` §3]. Scope stays
   surgical: the mask-loop surgery site only.
2. **Then a Rust-conversion QUEUE ordered by MEASURED Python-share per
   hotspot.** Honest note: the polars/DuckDB read surfaces are already
   native; the real candidates are the PYTHON LOOPS around them. Named
   (each needs its own DEC + 2-clean-round hostile review + bit-identity
   proof + default-OFF/burn-in/autorevert where results exist):
   - **Hit-list-intersection Stage-1B prefilter** (Q8) — the ONLY lever
     against the 1m-TF O(n_bars)/enumerated floor; integer-exact
     (identical `hits` ints ⇒ twin-run gates it; no float machinery
     needed).
   - **ALGORITHMIC LEVERS — FIRST-CLASS design space per the §9.1 BINDING
     perf reconciliation (a day-level-k≤6 claim is NOT derivable from the
     FFI port's ratio band alone — campaign band ≈1.3–3.1×, ≈2.3–2.5× at
     median density; no absolute k≤6 day baseline survives §9's
     withdrawals — final-lock):**
     (a) **parallelism topology** — today 8×1 workers×rayon
     (`RAYON_NUM_THREADS=1` pinned); in-kernel rayon measures ≈3.0–3.77×
     in this document set's artifacts (the ~3.97× endpoint is
     counterparty-relayed — §9.1)
     on the walk for heavy tuples, so PER-TUPLE topology reshaping
     (fewer workers × more rayon threads on heavy 1m tuples) is a
     measured lever, constrained to order-preserving
     `IndexedParallelIterator` over independent masks (serial ≡ parallel
     by construction; `par_bridge` forbidden — §3) with its own
     twin-run/bit-identity gate; (b) **entry-sparsity exploitation**
     (skip walk segments provably entry-free for a mask class); (c)
     **kept-vs-enumerated work shaping on coarse TFs** (1day k=3 keeps
     46.4% — the kept tail dominates there). Each is a queue item with
     its own DEC + hostile review, NOT shipped by this conversion.
   - **Finalist projection from the core** (Q6) — `ranked_combo_to_
     result_row`'s O(top_k × n_bars) Python-oracle re-sim; urgent iff the
     gap-#1 institutional top=1000 lands.
   - Stage-1A's per-bar Python push loop (~14.5 µs/bar sandbox).
   - Perfmax's gate/term Rust feed — deferred with the recorded
     precondition: the overfit gate consumes per-survivor skew/kurt
     (`ranking/__init__.py:555`), so any thin-feed design must return the
     moments AND re-clear the §11-COMMON oracle-checked-duplication bar,
     coordinated with P3's gap-#12 reason-returning gate twin.
   - Movers ranking loops; Greeks-enrich orchestration; prebuild/TF-
     materialization driver.
3. **The vendor pull/fetch stays Python by explicit operator exception**
   (rate-governed I/O — Rust buys nothing; the §2 Rule 6 rate machinery is
   untouched forever under this plan).

Nothing in 2–3 is implemented, promised, or scheduled by THIS change
(Golden Rule #3 — no silent expansion).

---

## 11. Gap-register / ownership-zone interactions (BINDING)

Sources: `sweep-parallel-integration-contracts.md` +
`sweep-gap-register-jul8.md` (project memory, read in full).

**Our zone (edit):** `brutex/engine/sweep_run.py`, `sweep_driver.py`
(oracle side), `rust_trade_sim.py` (export seam), NEW
`brutex/engine/sweep_core.py`, `rust/fno-math/src/{sweep_core.rs,
metrics.rs seam,lib.rs}` + `python/fno_math/__init__.pyi`, tests.
**PLUS (Verifier A round-1 fold): the `trade_sim.rs` VISIBILITY
promotion** — `simulate_one_core`, `MaskWord`, `SweepColumns`, and
`build_columns` are today MODULE-PRIVATE (Verifier A live-verified at
`rust/fno-math/src/trade_sim.rs:544` (`trait MaskWord`), `:566`
(`struct SweepColumns`), `:584` (`fn build_columns`), `:644`
(`fn simulate_one_core`); re-verified line-exact against origin/main
95955a71, 2026-07-09 — all four carry no `pub`), so the sibling
`sweep_core` module cannot consume them as §6 Slice 2 requires; all
four gain `pub(crate)` — a VISIBILITY-ONLY edit (zero body/signature
change, TickVault-safe: fno-math is not the shared crate and the
symbols stay crate-internal), mutation-neutral (visibility keywords
are not a cargo-mutants mutation surface; the existing trade_sim
mutant census is unchanged).
`sweep_stream.py` gets EXACTLY ONE authorized Slice-2 edit (round-5 —
this supersedes the previous "NOT needed in Slices 0–2" clause, which
contradicted the §5 serve-reconciliation mandate): the additive
`BoundedFinalist.position: int` field (+ a tiny additive read-only
per-position column accessor iff a retained column lacks one) — additive,
bit-pinned (rank order/scores/artifact bytes unchanged, §7.4
byte-neutrality pin); a bulk-extend helper remains NOT needed (full
ResultMetrics feeds the existing `append`).

**Not-ours (interface via contracts, never edit):** `sweep_orchestrator/
supervisor/scheduler/parallel` (P2 — the run-core arrives via #1295;
#1291 owns sweep_parallel.py); `results_store.py` (P3 EXCLUSIVELY —
Ruling 5; our path ends at the existing `store.append(row)`/`flush()`);
`brutex/ranking/*` (P3 — we IMPORT the shared internals, add nothing);
dashboard (P4), preflight/estimator (P1), nightly CLI (P6). Missing
integration point ⇒ a thin clearly-marked adapter in OUR zone.

**Coordinator rulings (grounding Q7 graft, originally bundled as one
sign-off request, since SPLIT):** (a) the `rust/` zone (gap #18) —
**CLOSED**. Ruling recorded verbatim:

> "Gap #18 → CLOSED (coordinator ruling 2026-07-08, re-confirmed
> 2026-07-09): the sweep-core session's rust/ edit zone = the NEW
> sweep_core module under rust/fno-math PLUS the four pub(crate)
> visibility promotions in trade_sim.rs. rust/trading-core stays
> UNTOUCHED — the shared 52/60-vocab bit-identity contract with
> TickVault is inviolable; any trading-core edit requires a separate
> coordinator ruling + golden bit-identity re-verification. Slice-0
> (pure-Python compute.py decomposition) separately granted and
> unaffected."

Scope note: the 'NEW sweep_core module under rust/fno-math' grant
encompasses the module-integration edits the §11 zone list already
enumerates — sweep_core.rs, the metrics.rs seam, lib.rs module
registration, and python/fno_math/__init__.pyi stub additions — all
within the fno-math crate; trading-core remains fenced OFF; #18's
exit_reason kernel-record rider stays sequenced strictly AFTER this
conversion.

(b) the Slice-0 `metrics/compute.py` decomposition —
**GRANTED (2026-07-09)** under three conditions carried into the Slice-0
PR: behavior-identical + standalone commit + a pre-push diff-check vs PR
#1295's branch (§6 Slice 0); sequencing rider: the twin-baseline capture
waits for product-audit-1's identity PR-1, and Slice 0 may precede it
(byte-neutral by proof).

**Pending coordinator rulings the design PINS TO (Verifier B F7/F8 —
routed, not yet ruled; the design binds to whatever is ruled and carries
the required interaction meanwhile):**
- **F7 — `BRUTEX_SWEEP_STALL_SECONDS` semantic collision:** #1295 and
  #1291 both define the env var on the same file with OPPOSITE semantics
  (fail-loud vs fail-soft) and different scaling laws. (Status at
  origin/main e8c708bd, final-lock: **#1291 (774d9d16) is MERGED** — the
  env var resolves only in sweep_parallel.py:364 and the guard's
  symbol-PRESENT arm is now the natural production arm, the
  absent-symbol arm the dormant pre-#1291/revert branch; **#1295 is
  UNMERGED**, so the collision lands iff #1295 merges — F7 stays
  genuinely pending.) This design's
  stall-budget guard reads the symbols via getattr from WHATEVER the
  merged tree resolves (the §5 absent-symbol arm + the drift pin make it
  total either way), so the guard tracks the ruling automatically; the
  RULING must land before the Slice-2 flip ADR, and the guard's §7.4
  drift pin re-verifies against the ruled formula.
- **F8 — #1295 peak-relative throughput tripwire × ADR-0017 autorevert:**
  required interaction, stated now: the tripwire's throughput baseline
  MUST be ARM-AWARE (or the autorevert latch must INFORM the tripwire) —
  a mid-grid autorevert legitimately drops throughput to today's spine
  rate, and a peak captured on the engaged arm would otherwise brand a
  byte-correct autoreverted run SUSPECT_COMPLETE forever. Until the
  ruling lands, the Slice-2 PR body carries this interaction note
  verbatim and the flip ADR is BLOCKED on the ruled mechanism (baseline
  re-arm on latch, or an autorevert→tripwire hook — whichever is ruled).

**Gap-register rows:**

| Gap | Effect | Detail |
|---|---|---|
| #3 (BLOCKER, P2) tuple-cache identity omits overfit+stage3 | Orthogonal, must-not-worsen — satisfied | we add NO resume/cache keys; `_tuple_digest`/`_grid_descriptor`/`SweepCache` untouched; the ENGINE is deliberately absent from every identity; `start_rank`/chunking is in-memory only and pinned never-durable; when P2 re-keys, both arms re-key identically |
| #4 (BLOCKER, P2/P3) `sweep_runs/` layout + foreign-parquet crash | Orthogonal | we write NO files, NO new artifacts; per-tuple trio shapes unchanged |
| #8 (MAJOR, P1/P2) dead VWAP bits 52/53 on spot | Orthogonal | dead-bit combos flow through the core as today (prefilter-dropped); either exclude-vs-flag ruling changes only the `condition_bits` INPUT — composes |
| #11 (MAJOR, P2) determinism re-run backend | **Helps** | our twin-run harness IS the scratch-root-recount shape; the OFF-arm spine stays fully alive behind the kill switch forever |
| #12 (MAJOR, P2+P3) reason-returning gate twin | Orthogonal | gates stay Python at the SAME site; the phase-next gate-feed precondition recorded (§10) |
| #16 (MINOR) snapshot re-key | Orthogonal | snapshot hashing untouched |
| #18 rust/ zone unowned | **CLOSED** (coordinator ruling 2026-07-08, re-confirmed 2026-07-09) | the §11 ruling (a) block above (verbatim ruling + scope note): zone = the NEW sweep_core module under rust/fno-math + the four trade_sim.rs pub(crate) promotions; trading-core fenced OFF; the exit_reason kernel-record rider sequenced strictly AFTER this conversion |
| #1/#2/#5/#6/#7/#13/#22 | Orthogonal | no file overlap; #1's top=1000 interacts with finalist projection + belt cost (both honest-noted, Q6 named) |

**#1295/#1291 composition (verified from fetched diffs):** neither touches
any mask-loop file. The core sits BELOW `run_persisted_sweep` (signature
unchanged) below `run_one_tuple`; #1295's sentinel/quarantine/flight-
record/tripwire/diffcheck seams and #1291's parent-side stall guard +
BrokenProcessPool quarantine compose UNCHANGED — for the stall guard
this is TRUE only via the §5 STALL-BUDGET GUARD (round-5: the belt +
512-cap burn-in add in-worker wall time the `_effective_stall_seconds`
mask·bars work model cannot see, up to ~17–33 min/tuple @1m/top=1000 —
without the guard a low-k 1m in-flight set deterministically hits
`SweepStallError` + an identical-resume livelock; §8 R9 carries the full
arithmetic; the guard reads the #1291 constants via a lazy import, edits
NOTHING in sweep_parallel.py). R45 diffcheck is
engine-agnostic (reads persisted rows) and becomes our third always-on
net. **PR-dependence, RECONCILED (round-6 — the previous blanket "must
NOT depend on either PR landing" sentence was falsified by the round-5
guard's use of #1291-only symbols, and round-6 verifiers returned
CONTRADICTORY #1291 merge states against different refs):** the design
must not — and with the §5 ABSENT-SYMBOL arm does not — depend on
**#1295** landing at all, nor on **#1291's work-scaled stall symbols
EXISTING**: the guard resolves them via getattr and degrades to the
flat `DEFAULT_STALL_SECONDS` deadline (present on every tree,
sweep_parallel.py:358 — the exact deadline a pre-#1291 parent enforces),
with `stall_guard_unavailable` as the total-evaluation refusal and the
`_effective_stall_seconds` drift pin `skipif`-scoped to symbol-present
trees; default-OFF flags + unchanged signatures mean zero rebase risk
either way, and symbol-existence — which the file-disjointness re-verify
cannot see — is now handled at runtime, not assumed. The crash-loop
breaker runbook gains the kill-switch lever (§3) PLUS the round-6
stall-after-restart line (§5 note (d)).

---

## 12. Identity & goldens register (quick reference — carried into the PR body verbatim)

| Artifact | Key composition | Rust-core effect |
|---|---|---|
| `mask_run_id` | blake3 over 9 fields incl. injected `code_commit_sha` | unchanged formula; lazy (finalists/errors only); engine NOT in the hash |
| `_tuple_digest` (store path + sentinel + sidecar) | blake3 over tuple axes + grid provenance incl. `code_commit_sha` | unchanged; a new commit re-keys fully — no cross-engine skip-complete is possible in either direction |
| `FlushResult.content_hash` | sha256 of Parquet bytes | byte-equal across arms (twin-run gate) |
| audit chain | hash-chained JSONL, 3 lines/tuple | byte-identical payload keys; NO new action (engine-blind) |
| frozen goldens | synthetic identity fields ("deadbeef"/"snap0001"/"ind-v1") | commit-independent literals stay frozen |
| 400-case vocab key | trading-core evaluator | untouched crate ⇒ untouched key |
| `_indicator_version()` | trading-core version string | fno-math not included ⇒ no re-key from this work |
| mask encoding in identity JSON | SHIPPING bytes FROZEN per surface: `combo_digest` + WF hash keep their bare-int `"mask"` (sweep_cache.py:389-391 / sweep_driver.py:977+ — frozen shipping behavior); run_id keeps hex `mask:%032x` (spec.rs:219); every NEW conversion-introduced surface = 32-nibble HEX STRING | **MASK-SERIALIZER LAW (Verifier B F5; RE-SCOPED per checkpoint-1 audit C3+C9 — the previous "every mask serializes as the hex string on BOTH arms" was SELF-CONTRADICTORY with corpus (e), which freezes the SHIPPING bare-int digest bytes; as written it would have RE-KEYED every SweepCache dedup entry + WF identity):** the hazard is serde_json's u64 cap ALIASING a bare-int u128 mask `m` with `m + 2^64` ⇒ a silent false-skip — a RUST-side hazard only (Python's `json.dumps` serializes arbitrary-precision ints exactly, so the shipping Python sites carry NO aliasing). The re-scoped law: (a) the shipping serialization bytes are FROZEN per corpus (e) — the Rust arm must reproduce the existing bytes exactly, INCLUDING the existing bare-int sites; (b) those sites STAY PYTHON (never ported — §2 table); (c) NO NEW bare-int mask serialization surface may be introduced by the conversion — every mask on any NEW surface serializes as the 32-nibble hex string; the corpus-(e) golden (§7.1, re-specified) + a grep pin refusing any new bare-int mask serialization in sweep_core.rs/`sweep_core.py` enforce it |

### 12.1 Identity-protocol laws (Verifier B checkpoint-1 audit C1)

Four register additions, each binding on the twin/baseline proving
protocol (§7.2) and the slice PRs:

1. **WHEEL-PROVENANCE law (twin proof).** Both twin arms must run the
   SAME `fno_math` wheel BUILD — not merely the same version string. The
   twin protocol RECORDS in the proof transcript: the wheel file's
   sha256, the build flags (the committed profile,
   `rust/fno-math/Cargo.toml` `[profile.release]`), and the wheel's
   `__source_digest__` (the build-time blake3 source stamp,
   `rust/fno-math/src/lib.rs:78-87` / `brutex/wheel_digest.py` —
   DEC-RUST-SOURCE-DIGEST-TWIN-001, the existing same-version-drift
   catcher, reused here). A wheel import FALLBACK during a proof run —
   pip-resolved instead of the locally-built wheel, or ANY import-path
   substitution between arms — is a REFUSAL of the proof run, never a
   silent alias.
2. **DIRTY-TREE law (baseline/twin capture).** `code_commit_hash` is
   only meaningful on a clean tree; the twin/baseline capture protocol
   REFUSES a dirty working tree — `git status --porcelain` empty is a
   recorded PRECONDITION step of every capture. Honest scope: ordinary
   production runs OUTSIDE the proving protocol keep today's behavior
   (no dirtiness check exists in the shipping run path today, and this
   pack adds no silent new gate there — stated, not smuggled).
3. **OUTPUT-ROOT WIPE law (operational, proving period).** WIPE the
   sweep output roots (stores + sentinels + checkpoints) between
   PROTOTYPE builds: a sentinel written by build A must never
   skip-complete a tuple under build B during the proving period.
   Within one COMMITTED build this is belt-and-braces (`_tuple_digest`
   embeds `code_commit_sha`, so a new commit re-keys — the table row
   above); between DIRTY/prototype builds sharing one commit SHA it is
   the ONLY protection, which is why it is an operational instruction
   here and law #2 refuses dirty capture runs outright.
4. **DEC-ID DEFERRAL (deliberate).** The conversion's DEC id(s) are
   minted in the IMPLEMENTATION PR ledger entries (Slice 0..4, each
   with its own MIGRATION.md entry), NOT by this design pack — the pack
   is documentation-only and pins no ledger number (the README states
   the same). This is deliberate: a pack-minted id would collide with
   the live ledger tail exactly as the renumbering precedents
   (#1580/#1617) document.

**The §B.4 identity question, answered (Verifier B register, pasted
2026-07-09 — question quoted VERBATIM):**

> "In the twin-run bit-identity proof, are both runs minted under ONE
> code_commit_sha and ONE indicator_version (same checkout, same
> wheels), with only the engine arm toggled — and if so, what MECHANISM
> guarantees that every FUTURE behavioural change to the Rust sweep
> core re-keys new runs? Concretely: (a) does the sweep-core crate's
> version join _indicator_version() (the #1548 precedent says version
> strings track the INDICATOR stack; a trade-sim/metrics crate is
> arguably not an indicator — the single most consequential open
> question); (b) if the answer is 'commit discipline alone', how is the
> brutex-{__version__} wheel fallback (H-2) prevented from aliasing
> wheel-swapped environments; (c) is a dirty-working-tree run refused
> or '-dirty'-stamped (H-6)?"

ANSWERS (design decisions; premise-corrected against the repo per
Golden Rule #1):

- **Yes to the preamble:** both twin runs are minted under ONE
  `code_commit_sha` + ONE `indicator_version` (same checkout, same
  wheels — law #1 above makes "same wheels" recorded evidence, not an
  assumption), only the engine flag toggled (§7.2).
- **(a) — NO, with the transmitted premise CORRECTED:** the relayed
  answer's premise ("the fno-math crate version ALREADY moves
  `_indicator_version()` by the #1548 precedent") is REFUTED by the
  repo: `_indicator_version()` composes
  `brutex-{__version__}+trading_core-{trading_core.__version__}`
  (cli.py:6159-6166) — the TRADING-CORE version only; the #1548
  precedent bumped trading-core 0.3.0→0.3.1, and fno-math (0.3.0,
  rust/fno-math/Cargo.toml:11) does NOT appear in the string (the §12
  table row above already records this). The precedent that actually
  governs an fno-math kernel is #1531/#1549 (rust_metrics /
  ResidentSweepSimU128 — cite corrected from "#1546" per the Verifier B
  v5 re-audit: the u128 kernel is DEC-TRADE-SIM-U128-KERNEL-001,
  MIGRATION.md #1549 within the renumbered #1546–#1550 pack; #1546 is
  DEC-RUN-IDENTITY-GUARD-PARITY-001): fno-math kernels ship under the BIT-IDENTITY
  contract with NO `_indicator_version` change, re-keying via
  `code_commit_hash` per commit. This conversion keeps that posture
  (§13 pins "no `_indicator_version` change") BECAUSE byte-identity is
  the ship gate — a behavioural change is a refused outcome, not a
  re-key event. **The FORWARD RULE, recorded:** any FUTURE *sanctioned*
  behaviour-affecting sweep-core change (one that is ALLOWED to move
  bytes) MUST (i) bump the fno-math crate version, AND (ii) join a
  version signal to run identity at that point — either extending
  `_indicator_version()` with `fno_math-{fno_math.__version__}`
  (exposed today: lib.rs:127) or minting a distinct identity component
  — under its own DEC; review-enforced + ledger-noted. Until such a
  change exists, commit discipline + bit-identity is the mechanism, and
  it is sufficient because no sanctioned byte movement exists.
- **(b) — therefore NOT commit-discipline-alone for the PROOF runs:**
  law #1 (wheel sha256 + `__source_digest__` recorded; import-fallback
  = refusal) closes the wheel-swapped-environment alias for every
  proving run; the `brutex-{__version__}` fallback arm of
  `_indicator_version()` (cli.py:6165-6166, taken only when the
  trading_core wheel is absent) cannot silently alias a proof run
  because the proof protocol records the wheels. Production runs
  additionally have the doctor's `wheel_source_digest_fno_math` stage
  (same-version behavioural-drift catcher) as the standing net.
- **(c) — REFUSED, for capture runs:** law #2 (dirty tree =
  precondition failure) — chosen over '-dirty'-stamping because a
  stamped-dirty baseline is still an unreproducible baseline; ordinary
  runs outside the protocol keep today's behavior (stated in law #2).

### 12.2 Env-flag census (Verifier B checkpoint-1 audit C2 — B's census adopted verbatim in substance, every cell repo-verified where the flag exists today)

**Design law (the exemplar rule):** every NEW flag/knob this conversion
introduces follows the `BRUTEX_SWEEP_PER_TUPLE_SECONDS` EXEMPLAR —
EAGER launch-time validation (resolved once at launch, never first
parsed mid-run/per-call) with a LOUD CLEAN failure (CLI: caught
`ValueError` → `EXIT_USER_ERROR` one-liner, cli.py:6564-6570; route:
clean 400 — the `BRUTEX_PIPELINE_STRICT_GATE` fail-loud-parse-at-launch
precedent), with exactly TWO sanctioned deviations: tristate ARM flags
may fail SAFE (unrecognised ⇒ OFF + warn-once — the
`BRUTEX_SWEEP_RUST_METRICS` disclosed pattern) and default-ON SAFETY
flags fail ON + warn-once (the belt). The existing silent/mid-run
defects below are NAMED as PRE-EXISTING — fixing them is NOT this
conversion's scope; they are registered as observations for the
coordinator.

NEW flags (this conversion):

| Flag | Class | Junk policy | Validation point |
|---|---|---|---|
| `BRUTEX_SWEEP_RUST_ENUM` | tristate arm (results-pinned-identical by the twin gates) | fail-SAFE OFF + warn-once (`env_flag_tristate` over `sweep_lookahead._TRUTHY`/`_FALSY`, imported never copied — §5) | read ONCE per `run_persisted_sweep` invocation (§5); never per-call |
| `BRUTEX_SWEEP_RUST_CORE` | tristate arm (results-pinned-identical) | fail-SAFE OFF + warn-once (same vocabulary) | same §5 once-per-invocation read |
| `BRUTEX_SWEEP_RUST_CORE_BELT` | tristate SAFETY (default ON) | INVERSE fail direction: unrecognised ⇒ **ON** + warn-once `belt_flag_unrecognised`; only explicit `_FALSY` disables (§5 round-1) | same §5 read |
| `BRUTEX_SWEEP_CORE_WORK_CELLS` | numeric knob (liveness-only; results cap-invariant by the dual-knob matrix) | fail-LOUD parse + DUAL CLAMP (upper ≤ 2^27, lower ≥ 2^16, `work_cap_clamped` warn-once with `direction` — §3) | EAGER at launch resolve (the exemplar rule) — never first-parsed mid-tuple |
| `BRUTEX_SWEEP_CORE_ENUM_CELLS` | numeric knob (liveness-only) | fail-LOUD parse + dual clamp (upper ≤ 2^30, lower ≥ 2^16 — §3) | same eager launch resolve |
| `BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR` | numeric knob (stall-budget pessimism input) | fail-LOUD parse + one-direction clamp (may only RAISE pessimism — §5 round-6 clamp spec) | same eager launch resolve |

EXISTING flags, with their defect status (Verifier B's census —
repo-verified; pre-existing, out of this conversion's fix scope):

| Flag | Class | Junk policy (as shipped) | Validation point (as shipped) |
|---|---|---|---|
| `BRUTEX_SWEEP_PER_TUPLE_SECONDS` | launch-gate numeric — **the EXEMPLAR** | eager-LOUD: non-finite/non-positive raises `ValueError` naming the env (sweep_sla_guard.py:105-128) → clean `EXIT_USER_ERROR` "sweep-all config error" (cli.py:6564-6570) | launch-time, before the sweep body |
| `BRUTEX_SWEEP_RUST_METRICS` | tristate arm | warn-once fail-safe OFF on unrecognised (`flag_unrecognised`, rust_metrics.py:30-31/:140-141) — ACCEPTABLE, disclosed | per-call read (documented #1531/#1532 posture) |
| `BRUTEX_BAD_TICK_SCREEN` | tristate ARM, default ON | **SILENT-JUNK defect:** junk → stays ON with ZERO signal (only an explicit falsy literal disables; bad_tick.py:291-309) | read at screen construction |
| `BRUTEX_SWEEP_LOOKAHEAD_GUARD` | tristate arm (opt-in instrumentation) | **SILENT-JUNK defect:** junk → silently OFF, no warn (sweep_lookahead.py:146-157 — deliberate fail-safe, but signal-free) | read at guard resolve |
| `BRUTEX_LAKE_HEAL` | mode string (`auto`/`observe`) | **SILENT-JUNK defect (same class):** anything ≠ `observe` — junk included — is silently `auto` (lake_heal.py:177-198) | read at heal resolve |
| `BRUTEX_SWEEP_MEM_BUDGET_BYTES` + `BRUTEX_SWEEP_BOUNDED_RANK` | capacity knobs | **MID-RUN-VALIDATED defect:** first parsed per-tuple inside `run_persisted_sweep` (`resolve_bounded_rank_mode()` / `resolve_mem_budget_bytes()` at sweep_run.py:1349-1350; sweep_mem_budget.py:106/:137) — junk wastes the WHOLE preload then grinds every tuple to `ValueError` | per-tuple, post-preload |
| `BRUTEX_MAX_BLOOM_BYTES` | capacity knob | **IMPORT-TIME-RAW defect:** junk = raw `ValueError` traceback at module import (`MAX_BLOOM_BYTES = resolve_max_bloom_bytes()` at import, orchestrator/bloom.py:243/:208-231) — loud but not clean | import time |
| `BRUTEX_SWEEP_STALL_SECONDS` / `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC` | liveness knobs | invalid → default + LOUD warn (DEC-SWEEP-STALL-OOM-GUARDS-001; constructor refuses non-positive/non-finite) | driver construction |

## 13. Invariants preserved (review checklist)

Tallies exact (`enumerated`, `conflict_pruned`, `skipped_dedup == 0`,
`evaluated`, `minhits_dropped`, `backtested`) — **honesty scope
(fix-forward item 1): the permanent production mechanism behind the
per-BUCKET split on the core arm is the enum shadow (in-window) + the
twin-run sidecar compare (ship time, teeth = the §7.1 count-only poison
golden); the total-reconciliation belt nets TOTALS only — residual (ix)** ·
core enumeration total
reconciled per tuple against the driver's `count_combos` (round-4
`enum_total_mismatch` belt) · masks in identity JSON serialize as 32-nibble
hex strings, never bare ints (the §12 MASK-SERIALIZER LAW, F5) · the core
owns ZERO lake I/O — bars are DATA from the one preload (F21) · the paisa
mint stays the repr-mediated Decimal seam (F22) · the NSE calendar is a
frozen input; the RT-40 post-fix Muhurat weekly-anchor ABSTAIN shape is
parity truth (§1.3 calendar contract) · `core.n_bars()` == the Python
window length at the seam (`bars_length_mismatch`, F27) · panic policy
explicit — abort profile pinned + PanicException seam catch mapping into
the quarantine taxonomy (F4/F23) · bounded-arm finalists serve-reconciled
(retained rank columns + gate re-verdict + candidate-finalist compares,
round-4) · finalist SurvivorColumns positions CARRIED
(`BoundedFinalist.position` additive field; `mask_at(position) == mask`
positional belt — round-5) · core engagement STALL-BUDGET-guarded per
tuple vs the recomputed #1291 effective deadline
(`stall_budget_exceeded` refusal — round-5; round-6: guard TOTAL on
every tree — absent-symbol flat-deadline arm + `stall_guard_unavailable`;
a liveness bound, not a net-win bound) · min_hits==0 Stage-1B count
SKIPPED in-core exactly as today's spine guard (the §1.3 MIN-HITS
SHORT-CIRCUIT LAW; in-band `hits_counted` — round-6) · fold_block
in-core work ceiling (`fold_work_ceiling` whole-chunk refusal at 2^28
cells — round-6) · yield order = enumeration
order · 3 audit lines/tuple, engine-blind payloads · lazy run_id
(finalists/errors only), `mask:%032x` · flush→sidecar→(no marks)→sentinel
STRICTLY LAST · `_combo_error` fingerprint format · oracle re-attribution
never adopted · MemoryError bare · `SweepMemoryBudgetError` watermark
per-append · bounded ≡ batch rank bit-for-bit · duplicate-mask rank raise
unreachable · `DigestPathNoDedup`/`_NoDedup` never consulted · legacy arm
untouched Python · exit-index/`metrics_from_trades` seams + #1531 burn-in
intact on their arms · `maybe_guard_bars` OFF identity · Stage-1A untouched
· trading-core untouched (diff EMPTY) · P-06 + suffix-independence suites
UNMODIFIED · no `_indicator_version` change · overfit gate fed per-survivor
skew/kurt exactly as today · `benchmark_return_pct` a Python-combine input,
never a Rust assumption.

---

## 14. Decision log

**Winner per judge:**
- Judge 1 (correctness skeptic, bit-identity double-weighted): **correctness**
  (50) > riskmin (49.5) > grounding (45) > perfmax (41).
- Judge 2 (performance realist): **correctness** (42.5) > riskmin (42) >
  grounding (36.5) > perfmax (32.5).
- Judge 3 (ship/integration realist): **riskmin** (42) > correctness (39) >
  grounding (36) > perfmax (31) — but its own rationale prescribed "land
  riskmin's slices with the correctness doctrine and perfmax's finalist belt
  grafted in".

**Consensus resolution:** correctness wins 2-of-3 and judge 3's runner-up
scoring of it carried no fatal flaw ("no fatal flaw; two friction points").
The disagreement I had to break: judge 3 preferred riskmin's smaller diff
(existing pyclasses, existing chunk geometry, per-mask tuple rows) over
correctness's new machinery (two pyclasses, PyBytes ABI, CNS unranking,
dual burn-ins). Broken in correctness's favour because judges 1 and 2 showed
riskmin's shape fails the mandate on the two measured dominant terms — its
`Vec<MetricScalarRow>` re-creates a per-mask FFI tuple crossing (~25
PyObject allocations/mask, the punished churn class) and it strands the
k≥5/6 enumeration term (13–120 s/tuple of pure Python) in a deferred slice —
while judge 3's actual shipping concerns are fully absorbed by grafting
riskmin's SEQUENCING (Slice 0 alone first), its scratch seam, its
CPython-generated oracle constants, and its fallback-granularity rule into
the correctness architecture. Both judge-3 friction points on correctness
are resolved below (CNS by proof, enum burn-in by work-bounding).

**Grafts folded in (source → section):**
1. perfmax → §5: the PERMANENT finalist belt (`BRUTEX_SWEEP_RUST_CORE_BELT`,
   default ON; bit-compare every ranked finalist against the mechanised-pure
   oracle as a PRE-PASS before the first append; mismatch ⇒ latch + tuple
   restart). Endorsed by all three judges. [Round 1 re-scoped it to a
   VALUE-level guarantee + added the sampled population spot-check — §5.]
2. perfmax → §5: burn-in widened 16 → 64 kept masks per process (judges 1+2).
3. perfmax → §1.3/§3: the `max_work_cells` work bound on `enumerate_prefilter`
   + the dual-knob (≥5-shape) batch-invariance matrix (judges 2+3).
4. riskmin → §6: Slice 0 (compute.py fold/combine decomposition) lands ALONE
   as its own revertable pure-parameterisation commit BEFORE any Rust, with
   goldens + weekly-mutmut zero-survivor as byte-neutrality proof (all three
   judges).
5. riskmin → §1.1: the `drawdown_stats_into(&mut Vec<i64>)` scratch seam —
   existing pyfunction becomes a thin allocating wrapper; its tests, goldens,
   and 358 mutants pass UNMODIFIED (judges 1+2+3).
6. riskmin → §5: today's shipping OFF-arm
   (`simulate_masks_with_exit_indices` + `metrics_from_trades`) added as the
   SECOND burn-in comparison arm beside the pure oracle — the OFF arm is the
   exact object twin-run byte-identity is defined against (judge 1). [Round
   1: the pure arm is now the dedicated `_oracle_metrics_pure`, NOT
   `metrics_for_mask`, whose tail is Rust-drawdown under the ADR-0017
   default — §5.]
7. riskmin → §5/§7: CPython-GENERATED exact-value constants baked into the
   Rust fold unit tests (judge 2); per-mask fallback granularity with
   whole-chunk precondition pre-detect in the Python wrapper (judge 2).
8. grounding → §12/§7.2: the run-identity/`code_commit_sha` register carried
   verbatim into the PR body (judges 1+3).
9. grounding → §11: the ownership-zone + gap-register crosswalk table and the
   explicit Q7 coordinator ruling for un-owned `rust/` (gap #18), bundled
   with the compute.py sign-off (judges 1+2+3); #18's exit_reason rider
   sequenced strictly after this conversion.
10. grounding/perfmax → §3: the crash-loop-breaker runbook addition naming
    the kill switches as remediation lever #1 for repeated SIGABRT worker
    deaths; no auto-env-flip per the Q5 stance (judges 1+3).
11. grounding/perfmax → §6 Slice 3: the plan-batch-vs-bounded routing kept
    OPTIONAL, licensed only by its dedicated equality pin (judge 2's
    condition; judge 3's hazard note honored by NOT taking it in Slice 1/2).

**Fatal flaws named by judges, and their resolutions:**
- *correctness — persisted-finalist blind spot past burn-in*
  (`ranked_combo_to_result_row` persists `rc.metrics` directly; verified):
  RESOLVED by graft #1 — the permanent finalist belt makes every persisted
  finalist VALUE oracle-verified forever; belt mismatch restarts the tuple
  on the Python spine (rank order was core-derived, so per-finalist
  oracle-ship is insufficient — perfmax's tuple-restart semantics adopted,
  restart shape specified in round 1). Round 1 proved the blanket
  "everything that persists" form overbroad (membership / DSR / sidecar
  escape the belt) — see the §5 restatement + the sampled spot-check. §5.
- *correctness — hard dependency on the out-of-zone compute.py
  decomposition*: RESOLVED by graft #4 (Slice-0-first, tiny, revertable,
  proof-carrying) + the bundled coordinator sign-off (§11) + a stated
  refusal fallback (Slice 1 only). §6/§8-R1.
- *correctness (friction) — CNS unranking is new algorithmic surface*:
  RESOLVED by keeping it (a cursor would be cross-call Rust state, violating
  the statelessness law) and paying the proof: unranking round-trip
  proptest + ranged-vs-monolithic equality + list-equality vs
  `itertools.islice`. §3/§7.
- *correctness (friction) — enum shadow burn-in duplicates full k=6 Python
  enumeration cost*: RESOLVED by work-bounding the shadow to the first 4
  RANGES per process (each ≤ `max_enumerated`), compared against islice over
  the same rank window — seconds, not two whole tuples. §5.
- *riskmin — per-mask tuple FFI crossing + retained per-mask Python loop*:
  avoided structurally — the PyBytes columnar transport (~0.1–0.7 µs/mask,
  zero per-value churn) is the adopted marshal. §1.3.
- *riskmin — k≥5/6 enumeration deferred*: avoided — `enumerate_prefilter`
  lands in Slice 1 as the FIRST Rust slice (integer-only, the cheapest
  proof class). §6.
- *riskmin — `sort_unstable_by` ±0.0 gloss + missing non-finite belt*:
  RESOLVED — stable `sort_by` structurally + adversarial −0.0-tie property;
  any non-finite fold value ⇒ `python_required`. §4 rows 5/11.
- *riskmin — method-level wheel staleness outside the module handshake*:
  avoided — new SYMBOLS (covered by `_EXPECTED_COMPILED_SYMBOLS`) + the
  in-band `SWEEP_CORE_ABI_VERSION` per block. §1.1.
- *perfmax — falsified "skew/kurt consumed by NO gate" claim / Slice 3
  starving the overfit gate*: RESOLVED by NOT adopting perfmax's Slice 3 —
  a full `ResultMetrics` is built per kept mask in Python combine, so the
  6th (overfit) gate's per-survivor skew/kurt consumption
  (`ranking/__init__.py:555`, verified) and SurvivorColumns' skew/kurt
  columns are fed exactly as today; the thin-feed idea moves to the roadmap
  queue with the overfit-inputs precondition recorded. §1.4/§10.
- *perfmax — Rust `information_ratio` silently baking benchmark=0.0*:
  RESOLVED — `information_ratio` stays in Python combine with
  `benchmark_return_pct` an explicit stated input (today's 0.0 sweep-path
  default). §1.3/§4 row 15.
- *perfmax — largest cross-language proof surface (floor-divs, _safe_ratio
  Nones, total_return_pct, sharpe/sortino/omega/tail ported)*: avoided —
  the fold/verdict layer rule keeps ALL of those in Python; the Rust float
  surface is exactly 9 fold columns. §0/§4.
- *perfmax — unshippable big-bang Slice 1 + rank-arm override + asserted
  sort stability*: RESOLVED by the four-slice sequencing (§6), the
  optional-with-pin treatment of the rank-arm routing (graft #11), and the
  stable-sort resolution above.
- *grounding — SPEC §4 row 3 shipping abandoned reasoning; ad-hoc
  sharpe-vs-sortino sqrt split*: RESOLVED — §4 states ONE clean layer rule
  (folds move; verdicts incl. ALL sqrt-derived verdict scalars stay) with no
  alternatives narrated inline.
- *grounding — SurvivorColumns/ResultMetrics signature contradiction ("never
  materialise 37-field objects" vs `append(mask, metrics)`)*: RESOLVED —
  this design deliberately BUILDS ResultMetrics per kept mask (≈11–13 µs
  with the relocated seam — the ≈10 µs measured intercept plus the
  ~1–3 µs per-mask seam, ~6–8% of the walk+scan floor per the final-lock
  correction in §0/§9) precisely so every pinned shared internal is
  consumed unmodified. §1.4.
- *grounding — compute.py expressions needed but the file absent from its
  zone list (copy-vs-extract unresolved)*: RESOLVED — Slice 0 extracts
  (never copies; jscpd-safe) with the sign-off recorded. §6/§11.

---

## Hostile-review resolution log — round 1

All 25 confirmed findings (some duplicated across lenses) resolved as REAL
design changes, grouped by root defect. Section references are post-revision.

1. **Neumaier builtin-sum mis-port (MAJOR, bitident — §4 row 6 / SPEC §4
   row 6).** RESOLVED: the downside Σr² fold now ports CPython 3.12's
   Neumaier-compensated `builtins.sum()` float fast path token-for-token
   (running result + compensation + the `|f_result| >= |x|` branch + the
   `f_result + c` return path), never a naive left fold — §0, §2, §4 row 6,
   SPEC §4 row 6. A new **summation-algorithm law** (§4, before the
   closing paragraph) classifies EVERY ported summation construct by its
   exact CPython algorithm (bare `+=` fold / builtin-sum Neumaier /
   fsum-never-ported), adds it to the review checklist + a CI grep pin, and
   the "mechanically provable" theorem statement now carries the explicit
   identical-ALGORITHM obligation (i) it previously assumed silently.
   Compensation-triggering differential vectors added to §7.3/§7.5.

2. **Belt guarantee overbroad — persisted DSR, sidecar sums, finalist
   SET/ORDER escape it (MAJOR ×3: bitident, perf, edge).** RESOLVED: the
   §5 belt paragraph withdraws "everything that PERSISTS is oracle-verified
   forever" and restates the guarantee at VALUE level, NAMING the three
   escaping persisted/rank-bearing artifact classes (membership via
   population-z-normalised gates/terms; persisted `deflated_sharpe` over the
   cross-trial population; the trial-stats sidecar sums). NEW permanent
   mechanism added: the **4-mask sampled population spot-check** per tuple
   (deterministic tuple-digest seed, pure oracle, same latch+restart
   semantics) — probabilistic production coverage of the population blind
   spot, with the honest (1−f)^4-per-tuple detection arithmetic stated. The
   Slice-4 default-ON ADR is REQUIRED to state exactly this residual; R45
   diffcheck + the conflict tripwire are re-scoped honestly (neither sees a
   missing finalist or a population statistic). §5, §8 R2/R9, §14 graft #1.

3. **Burn-in oracle arm (a) shares fno_math drawdown under the ADR-0017
   default (MAJOR ×3: bitident, coverage, edge).** RESOLVED: arm (a) is now
   a dedicated `_oracle_metrics_pure` (pure-Python walker +
   `build_equity_curve` + `compute_metrics` directly), FORBIDDEN from
   calling `metrics_for_mask`/`metrics_from_trades`; independence is
   MECHANISED (AST/import pin + a runtime poisoned-`sweep_drawdown_stats`
   pin). The PERMANENT belt and the spot-check use the SAME pure arm. Arm
   (b) deliberately keeps the shipping provenance (it defines twin-run
   byte-identity) — provenance now stated per arm. §5, §6 Slice 2, SPEC §3
   condition 4 + §4 burn-in paragraph, §14 graft #6.

4. **Belt-mismatch tuple restart under-specified; "nothing durable written
   yet" false; interleaved belt trips the ResultsStore run-id conflict
   (MAJOR edge + MINOR ×2 bitident/coverage).** RESOLVED: the belt is
   mandated as a **PRE-PASS** (ALL finalists verify before the FIRST
   append — restart is append-free by construction; the §1.4-vs-§5
   placement contradiction removed). Restart re-entry is SPECIFIED: strictly
   BELOW the already-durable SWEEP_START line, within the same invocation,
   via an inner enumerate→fold→rank→belt function whose tallies /
   accumulators / burn-in counters are fresh per call and discarded
   wholesale on mismatch [SUPERSEDED in round 2, item 2: "burn-in
   counters" was a self-contradiction — they are PROCESS-GLOBAL; only
   tuple-scoped state is fresh per call] — exactly 3 audit lines/tuple
   preserved,
   SWEEP_FLUSH/SWEEP_COMPLETE carry only the restarted run's tallies. The
   belt-restart golden now pins audit-line count == 3 + tally-payload
   byte-equality. §1.4, §5, §7.1, §7.7, §8 R6, SPEC §6 item 8.

5. **GIL-hold caps 50–300× today's envelope; R9/§3 liveness claims
   quantitatively false (MAJOR ×2: security, perf).** RESOLVED: caps
   RE-SCALED — `enumerate_prefilter` work cap 2^31 → **2^28** (~70 ms) and
   `fold_block` 2^30 → **2^24 cells** (~55–70 ms at the ~3.4–4 ns/cell fold
   cost the review derived), both env-tunable; §3 states the crate's
   no-allow_threads convention explicitly (the cap IS the only GIL lever),
   corrects Ctrl-C/dashboard granularity to ~0.1 s, and R9 withdraws the
   false "no worse than today's 256-mask chunks" comparison with the real
   arithmetic shown. NEW GIL-hold bench pin (§7.7).

6. **CNS binomial overflow ⇒ SIGABRT under overflow-checks+panic=abort
   (MINOR ×2: security, edge).** RESOLVED: §1.3 pins up-front domain
   validation (k ≤ n_distinct_bits ≤ kernel width) + checked-u128
   binomial arithmetic returning `Err` (clean PyErr) on any overflow incl.
   C(128,64)-class inputs; added to §3's structural-prevention list and
   §7.5's unit/fuzz vectors.

7. **panic=abort containment covers only the worker arm (MINOR,
   security).** RESOLVED: §3 states the honest containment split — on the
   sequential/in-process (dashboard-hosted) arm a SIGABRT kills the whole
   process, no quarantine/breaker; the kill switch is preventive there —
   and the breaker-runbook addition carries the same sentence verbatim.

8. **Belt flag poisoned-env fail direction unspecified (MINOR, security).**
   RESOLVED: `BRUTEX_SWEEP_RUST_CORE_BELT` unrecognised value ⇒ **ON** +
   warn-once (`belt_flag_unrecognised`); only an explicit `_FALSY`
   disables — the inverse of the kernel flags, both failing toward SAFETY;
   pinned by a unit dual. §5, §7.4, SPEC §3 condition 4.

9. **Endianness: LE-labelled ABI decoded native (NIT ×2).** RESOLVED:
   `sys.byteorder == "little"` is an explicit engagement precondition
   (fallback reason `big_endian_host`) and the layout golden pins it. §1.3,
   §5 engagement condition, §7.1.

10. **fold_block ignores the bars_strictly_increasing certificate —
    duplicate-timestamp windows bit-diverge (MAJOR, edge).** RESOLVED: the
    core consumes walker-native exit indices ONLY under the per-tuple
    strictly-increasing certificate (the same two-arm contract as today);
    an uncertified window falls back to today's spine WHOLE-TUPLE
    (`ts_uncertified`, warn-once), making the §7.3 duplicate-timestamp
    property pass by construction; the `_exit_pairs` third arm's ValueError
    fingerprint behaviour is stated. §1.3, §2, §4 row 7, §5 engagement
    condition, §7.3/§7.4.

11. **No inverted teeth for the ENUM arm; enum past-burn-in residual
    unstated (MAJOR, coverage).** RESOLVED: a mandatory ENUM poison twin
    (dropped kept mask past enum burn-in ⇒ tallies + content hash MOVE) in
    §5/§7.1, plus the honest statement that the enum arm's production
    kept-set correctness past burn-in is the design's second named residual
    (netted deterministically by CNS proptests + list-equality property +
    twin-run grids only).

12. **Mutation "+1 shard" dial already consumed; timeout-mutant class
    unaddressed (MAJOR, coverage).** RESOLVED: §7.6/§8 R10 record the dial
    as SPENT (5→6, u128 merge; worst-shard band ~86/120 min), require
    measured mutant-count/shard-time on-branch BEFORE merge with a NEW
    ledgered 6→7 re-shard decision in the same PR if needed, and mandate
    upfront-bounded `for`-loop shapes in the enumeration so loop-control
    mutants cannot hang to the 300 s timeout.

13. **`python_required` non-finite belt likely unreachable ⇒ standing
    mutants (MINOR, coverage).** RESOLVED: the check is factored into the
    pure `fold_guard_finite` helper with direct in-crate non-finite unit
    tests (§1.3, §7.5); the belt is explicitly labelled defence-in-depth.

14. **Retained per-kept Python cost understated (~10 µs intercept vs
    claimed 3–6 µs / <5%) (MINOR, perf).** RESOLVED: §0/§9/§14 restate the
    residue as ~3–10 µs with the measured ≈10 µs intercept as the honest
    anchor (~5–7% of the floor), headline band widened to 1.9–2.7×,
    re-measure-on-branch required before any tighter claim.

15. **"Dominant cost on k=5/6 grids" over-scoped (MINOR, perf).**
    RESOLVED: §0 and §6 Slice 1 re-scope the enumeration term as the
    dominant PYTHON cost, dominant in TOTAL only on high-TF tuples (the
    >~9.3k-bar crossover vs the retained prefilter floor stated; 1m rows
    governed by the prefilter floor, hit-list lever named).

16. **64-mask dual-oracle burn-in cost unquantified (MINOR, perf).**
    RESOLVED: quantified in §5 (3–10 s/worker @20–48k bars, up to ~1–2 min
    at 1m; once per process; ~1000× the #1531 precedent) and carried into
    §9's new correctness-overhead accounting alongside the belt
    (~2–4 s/tuple @48k top_k=20, worst ~40 s at 1m — the withdrawn "~8 ms"
    figure corrected) and the spot-check (0.4–8 s/tuple).

---

## Hostile-review resolution log — round 2

All 18 confirmed findings resolved as REAL design changes. Section
references are post-revision. Duplicated findings share one root-defect
resolution, cross-referenced.

1. **Burn-in membership for flag-served rows unspecified; a wrong
   zero_trade flag escapes burn-in AND the belt structurally (MAJOR,
   bitident).** RESOLVED: §5 now DEFINES "core-served" as every mask whose
   row came back from `fold_block`, INCLUDING flag-served rows — they count
   toward the 64 and ARE compared, served-bytes vs both oracle arms,
   regardless of serving arm; the deliberate break with #1531's zero-trade
   exclusion is stated with its rationale (the zero_trade VERDICT is a
   kernel output here). A misflag inside the window now mismatches (both
   arms have trades) ⇒ oracle ships + latch. The `python_required`
   served-vs-arm-(b) vacuousness is stated, not hidden. The spot-check
   sampling pool is made flag-INCLUSIVE (a misflagged mask is sampleable),
   and the Slice-4 ADR residual list gains the named fourth class
   `zero_trade`-MISFLAG (belt-blind by construction — `_empty_metrics`
   fails MIN_TRADES, `ranking/__init__.py:213`). §5 (burn-in bullet,
   spot-check paragraph, residual list), §7.1 (misflag-poison-in-window
   golden), §7.4, SPEC §3 condition 4 + §4 burn-in paragraph, §6 item 9.

2. **"Burn-in counters" listed as FRESH-per-call while also pinned
   process-global with once-per-process cost accounting (MINOR, bitident;
   duplicate of finding 17).** RESOLVED: one semantic wins and is stated —
   the restart paragraph's fresh-per-call list is re-scoped to TUPLE-SCOPED
   state only (tallies, accumulators, kept-mask lists, spot-check samples)
   and EXPLICITLY EXCLUDES the burn-in latch + remaining-counters, which
   stay single-cell PROCESS-GLOBALS (#1531); §9's once-per-process
   accounting is declared NORMATIVE; §7.4 pins that the second tuple in the
   same worker process (and the restarted pass) runs ZERO additional
   burn-in compares. §5 restart paragraph, §9, §7.4, SPEC §3.

3. **enumerate_prefilter validates bit COUNT, not per-position < kernel
   width — Python-reachable shift-overflow SIGABRT (MINOR, bitident;
   duplicates findings 4 and 15).** RESOLVED with finding 4 below.

4. **Shift-overflow SIGABRT on bit indices ≥ kernel width + enumerate-call
   width selection keyed on masks that do not exist yet (MAJOR,
   security).** RESOLVED: §1.3's up-front validation now carries the
   per-INDEX VALUE bound — every `condition_bits` index AND every
   `conflict_pairs` index < W::BITS, an `Err` refusal before any shift —
   beside the count bound and the checked-u128 binomials, with the
   Python-reachability note (combo_mask.py validates against
   MAX_VOCAB_BITS=128 only). §5's engagement condition now derives kernel
   WIDTH from `max(bit_length(max(bar_bits)), max(condition_bits)+1,
   max(conflict-pair indices)+1)` — never bar_bits/mask magnitude alone —
   so a u64-fitting window with a [64,128) condition bit selects the u128
   core; the in-core refusal is the stated backstop for any seam bug.
   §7.5 pins `bits=[100]`-on-u64 and a ≥width pair index beside the
   C(128,64) pin; §7.4 pins the seam width selection. SPEC §6 item 9.

5. **fold_block decode: row-count-vs-chunk equality absent from the
   refusal contract; bare zip() silently truncates (MINOR, security).**
   RESOLVED: §1.3 gains the DECODE REFUSAL LAW — the wrapper validates
   `len(flags) == len(chunk)` and both byte-buffer lengths against
   n_rows × stride × 8 BEFORE yielding, each failure a NAMED
   `kernel_result_refused` reason (`row_count_mismatch`,
   `ints_length_mismatch`, `floats_length_mismatch`); the §1.4 loop is
   `zip(chunk, rows, strict=True)` as the belt. §7.4's decode vectors gain
   a truncated-but-stride-clean block + a cross-buffer mismatch; the §5
   domain-INVALID golden clause names the new reasons. SPEC §6 item 9.

6. **Env-tunable work-cap overrides carry no upper clamp — a typo re-opens
   the round-1 GIL-hold hole (NIT, security).** RESOLVED: §3 clamps
   overrides (enum ≤ 2^30 ≈ 0.28 s; fold ≤ 2^27 ≈ 0.5 s) with one loud
   `work_cap_clamped` warn-once; §7.7 gains a clamped-override probe
   (`2**44` engages the clamp + the warning).

7. **Speedup-table 'today' columns applied the per-KEPT 0.58 ms constant
   to ENUMERATED populations — blended row arithmetically impossible
   (MAJOR, perf).** RESOLVED: §9's blended row is REBUILT bottom-up from
   the document set's own measured rates as an explicit formula
   (`T ≈ enum + s·prefilter(n_bars) + f·PK(n_bars)`, s/f stated unknowns)
   with evaluated examples (@48k f→0: 14.9 → 12.6 µs ≈ 1.2×,
   prefilter-floor-bound; @3k f→0: ≈ 3.5–4×); the 0.58 ms cell is
   WITHDRAWN as unit-conflated with the impossibility arithmetic shown
   in-table. The grid rows' "~15.2 h / ~5.9 d" anchors (0.58 ms ×
   enumerated ÷ workers) are WITHDRAWN with the constant; grid headlines
   are DEFERRED to a REQUIRED on-branch sub-grid measurement (both arms,
   ≥2 TFs incl. 1m, k∈{2,5}) before the Slice-1 merge, after which
   combo_counts.md §3 is rebuilt bottom-up. SPEC §5 gains the UNIT LAW
   (per-KEPT constants must never multiply enumerated counts).

8. **Zero-trade kept masks: the fold never pinned the in-crate
   short-circuit — an O(n_bars) scan where today costs O(1), on the ~66%
   tail (MAJOR, perf).** RESOLVED: §1.3 mandates the ZERO-TRADE
   SHORT-CIRCUIT LAW (walk yields zero trades ⇒ emit flag + zero rows,
   SKIP the drawdown scan and all float folds — the in-crate twin of
   today's FLAT shortcut), with the explicit warning that the
   unconditional-scan implementation is bit-identical and therefore
   invisible to every byte gate; §2's zero-trade row and §4 row 7 restate
   it; §7.7 gains the zero-trade COST pin (≈ walk-only, regression-loud);
   §9 gains the dedicated zero-trade row quantifying the avoided 2.4×
   regression. SPEC §6 item 9.

9. **Belt cost quantified only at top_k=20; top=1000 is the spec'd
   production sizing; top_k=None leaves the belt unbounded (MINOR, perf;
   duplicate root with finding 16).** RESOLVED: §5 + §9 quantify the belt
   at top=1000 (~100–200 s/tuple @48k, ~17–33 min/tuple @1m, up to
   ~20–50% of a k≤5 grid — cli.py:385 cited as the production-sizing
   authority, no longer "institutional"); the "low-single-digit percent"
   claim is SCOPED to top_k ≤ 20; the Slice-4 flip ADR must present the
   MEASURED belt fraction at the operator's actual `--top`. The
   top_k=None arm is closed structurally: core engagement REFUSES it
   (fallback reason `unbounded_top_k` — §5 engagement condition), so the
   default-ON belt has NO unbounded arm; §7.7's belt bench runs both
   top_k=20 and top=1000 shapes. SPEC §3 condition 4, §6 item 9.

10. **Ported downside/Neumaier folds contain provably-EQUIVALENT
    cargo-mutants mutants — zero-survivor unsatisfiable as written
    (MAJOR, coverage).** RESOLVED: §7.6 names the remedy — both loops
    factored into tiny named helpers (`downside_accumulate`,
    `neumaier_add`) with direct boundary tests, and the TWO
    proven-equivalent comparison-operator mutants (`r < 0.0`→`<=`,
    Neumaier `>=`→`>`) get PRECISE `exclude_re` entries in
    `rust/fno-math/.cargo/mutants.toml` with per-mutant comments citing
    the equivalence analysis + the compute.py:696 pragma
    (DEC-MUTATION-GATE-COMPUTE-001) — the crate's documented-exclusion
    precedent, never a file skip; every other sweep_core.rs survivor
    stays a hard red.

11. **The `_oracle_metrics_pure` AST/import pin cannot be module-level —
    `build_equity_curve` lives in sweep_driver.py which imports fno_math
    (MINOR, coverage).** RESOLVED: §5 specifies the pin as
    FUNCTION-granular over an ENUMERATED CLOSED SET of function bodies
    (listed), asserting no fno_math reference AND that every call resolves
    inside the set (out-of-set calls fail loudly); the runtime poison pin
    is re-scoped honestly as covering the one shipping-tail symbol only.
    §7.4 pins both.

12. **Sampled spot-check silently vacuous on degenerate pools; no
    executed/sample-count pin (MINOR, coverage; duplicate root with
    finding 18).** RESOLVED: §5 defines the degenerate branch — sample
    `min(4, |pool|)`, empty pool ⇒ covered SKIP with
    `spot_check_sampled = 0`; the per-tuple `sweep_core_path` INFO event
    carries `spot_check_sampled`; §7.4 pins `== 4` on a ≥4-non-finalist
    fixture and `== 0` on an all-finalist fixture; the (1−f)^4 arithmetic
    is scoped to |pool| ≥ 4 tuples. SPEC §6 item 9.

13. **The ≥10⁶-case differential harness had no executable lane; the
    re-shard projection held per-mutant suite time constant (MINOR,
    coverage).** RESOLVED: NEW §7.10 gives the harness an executable home
    (seeded generator suite under `tests/rust_differential/`; 10⁴-case
    per-PR subset ≤ ~60 s; the FULL ≥10⁶ sweep a REQUIRED on-branch gate
    before the Slice-2 merge, wall-clock recorded in the PR body; weekly
    thereafter; never under cargo-mutants). §7.6 adds the suite-runtime
    discipline: PROPTEST_CASES reduced under the mutants run, vector
    suites O(ms), heavy differential Python-side — and the 6→7 re-shard
    projection is re-derived from the MEASURED per-mutant time WITH the
    new tests, never the pre-slice 29–40 s constant. §8 R2 updated.

14. **Belt/spot-check restart non-convergent on a PRE-EXISTING
    spine-vs-pure-oracle divergence; belt arm scoping unstated (MAJOR,
    edge).** RESOLVED: §5's mismatch semantics gain the burn-in's
    dual-arm DISAMBIGUATION — on mismatch the mask is recomputed once via
    the shipping OFF-arm; OFF-arm==core≠oracle = pre-existing divergence
    ⇒ NO restart, NO latch, distinct `belt_preexisting_divergence` ERROR
    (the served bytes ARE today's bytes); OFF-arm==oracle≠core (or
    three-way) = core defect ⇒ latch + restart. Belt + spot-check are
    declared CORE-ARM-ONLY (skipped with `belt_skipped_non_core` on the
    restarted pass, fallback tuples, latched processes, flag-OFF) — so
    the restarted pass runs no compare and a second mismatch is
    structurally impossible: termination by construction, at most one
    wasted partial tuple per process. §9's accounting now states the
    core-arm-only assumption it previously made silently. §5, §7.1, §7.4,
    §8 R6, SPEC §3 condition 4 + §6 item 9.

15. **Per-index shift-overflow (MINOR, edge — duplicate of findings
    3/4).** RESOLVED with finding 4 (per-index validation + width-selection
    law + §7.5 pins).

16. **top_k=None makes the belt a full-population pure-Python re-walk
    (MINOR, edge — duplicate root with finding 9).** RESOLVED with
    finding 9 (engagement refusal `unbounded_top_k`; the belt never sees
    an unbounded finalist set; lifting the refusal is a named later
    measured decision).

17. **Internal fresh-per-call vs process-global burn-in contradiction
    (MINOR, edge — duplicate of finding 2).** RESOLVED with finding 2
    (tuple-scoped fresh state; process-global latch/counters normative;
    second-tuple-zero-compares pin).

18. **Spot-check undefined on kept=0 / kept<4 / all-finalist pools and on
    tuples the core never served (MINOR, edge — duplicate root with
    finding 12).** RESOLVED with finding 12 (degenerate-pool law +
    sampled-count observability + pins) and finding 14 (core-arm-only
    scoping answers the never-served-tuple arm: the spot-check does not
    run there, so it can neither verify nothing nor inherit the
    pre-existing-divergence restart hole).

---

## Hostile-review resolution log — round 3

All 15 confirmed findings resolved as REAL design changes. Section
references are post-revision. Duplicated findings share one root-defect
resolution, cross-referenced.

1. **Flag-inclusive burn-in membership dilutes the 64-mask window toward
   ZERO real-fold compares on zero-trade-heavy tuples (MINOR, bitident).**
   RESOLVED: §5's burn-in gains the NON-TRIVIAL FLOOR — window closure now
   requires BOTH ≥64 total core-served compares AND
   ≥`SWEEP_CORE_SELF_CHECK_NONTRIVIAL = 16` PLAIN (non-flag-served) rows
   compared; the window stays open across tuple boundaries
   (process-global counters) until the floor is met, hard-capped at 512
   total compares; a cap-trip with the floor unmet fires ONE
   `burnin_nontrivial_floor_unmet` warn and joins the Slice-4 ADR
   residual list as the new class (v) (§5 residual list, §6 Slice 4).
   Cost quantified in §9 (extra compares are zero-trade-cheap, ≪8×
   worst case); §7.4 pins the open-window-across-tuples, cap-trip, and
   mixed-tuple-closure branches. SPEC §3 condition 4 round-3 paragraph.

2. **SPEC §4 rows 1/3 + §2 contradict the design's fold/verdict layer
   rule (MINOR, bitident).** RESOLVED in SPEC: row 1's "use
   `div_euclid`-style floor division explicitly" instruction is withdrawn
   and replaced with the layer rule (floor-divs NEVER move to Rust —
   Python combine from exact i64 sums); row 3's abandoned option
   narration ("CHOSEN … NO: too fragile. FINAL CHOICE …") is deleted and
   replaced with the single final rule (raw inputs out, today's literal
   Python tokens compute every libm/sqrt verdict scalar); §2's IN bullet
   now scopes `_compute_metrics_impl` to "the FOLD layer only", verdicts
   staying Python — completing the round-1 resolution grounding's finding
   had only half-delivered.

3. **Sampled spot-check has no core-side comparand on the BOUNDED rank
   arm (MINOR bitident; duplicate root with finding 13 MAJOR edge).**
   RESOLVED with finding 13: the RETAINED-CANDIDATE scheme (§5).

4. **Decode refusal law does not constrain flag bits 2–7 (NIT,
   bitident).** RESOLVED: §1.3's flags definition declares bits 2–7
   RESERVED-MUST-BE-ZERO and the decode refusal law gains the per-row
   `reserved_flag_bits` refusal; §7.4's decode vectors add a
   reserved-bit-set flags byte. SPEC §6 item 10.

5. **Work-cap overrides have an UPPER clamp only; "until exhausted"
   undefined ⇒ zero-progress infinite loop or silently-empty tuple
   (MINOR security; duplicate root with finding 14 MINOR edge).**
   RESOLVED with finding 14: §3's three-part fix — (i) parse-time LOWER
   clamp (≥2^16 cells, `work_cap_clamped` warn-once with a `direction`
   field); (ii) the in-core GUARANTEED-PROGRESS LAW (caps checked AFTER
   each mask's work, the `max(1, n_bars)` floor idiom of
   `sweep_run.py:203` — every non-exhausted call advances `next_rank` ≥ 1;
   `fold_block` effective chunk `max(1, cells // n_bars)`) [SUPERSEDED in
   round 4, item 4: the fold-side cap is DRIVER-enforced, not in-core,
   and the idiom is the divisor-floored
   `max(1, cells // max(1, n_bars))`]; (iii) the
   EXHAUSTION CONTRACT — `enumerate_prefilter` returns an in-band
   `exhausted` flag (`next_rank == total_count`, §1.3 signature grown to
   a 5-tuple), the §1.4 driver terminates ONLY on it, and a
   structurally-unreachable no-progress return hits the new
   `enum_no_progress` fallback belt (§5 reason list). §7.4/§7.7 gain
   degenerate-override probes (`=1` both knobs ⇒ clamped + warned +
   byte-identical completion) beside the round-2 `2**44` upper probe;
   the dual-knob matrix includes one lower-clamped shape per knob.

6. **enumerate_prefilter validation omits a start_rank bound — silent
   wrong-combination result (NIT, security).** RESOLVED: §1.3 validation
   gains clause (c) `start_rank ≤ total_count` (`> total_count` ⇒ `Err`
   `rank_out_of_domain`; `== total_count` ⇒ empty exhausted return —
   never a saturated wrong kept set); §7.5 bakes the refusal pin beside
   the C(128,64) pin. SPEC §6 item 10.

7. **SPEC's per-kept residual target (~1–5 µs) never updated to the
   round-1-corrected ~3–10 µs residue (MINOR, perf).** RESOLVED: SPEC §1
   restates the target as the ~3–10 µs band with the measured ≈10 µs
   intercept named and the stale figure explicitly withdrawn —
   re-measured on-branch before any tighter claim.

8. **§9 zero-trade row's "≈ walk-only ~62–65 µs" omits the retained
   per-mask Python tail — true post ≈ today's ~70 µs, ratio ≈ 1.0× (NIT,
   perf).** RESOLVED: the §9 zero-trade row is corrected to ≈65–70 µs /
   ratio ≈ 1.0× with the retained tail itemised (decode-row, flag branch,
   `_empty_metrics` construction, yield/append — baseline's 7.6 µs tail
   is "no FFI", so only the dispatch slice deletes) and the row's claim
   restated as REGRESSION AVOIDANCE; the §7.7 wall-clock pin's ceiling is
   sized to include the retained tail so a correct implementation never
   false-fails.

9. **No negative engaged-event pins — cross-flag engagement invisible to
   every stated gate (MAJOR, coverage).** RESOLVED: §5's anti-vacuous
   bullet + §7.1 mandate BOTH-directional per-arm event assertions
   (exactly one own-arm `engaged` AND ZERO other-arm `engaged` on
   enum-only / core-only; zero of both on OFF; one each on both-ON) plus
   TWO disabled-arm poison goldens (poisoned core under `CORE=0,ENUM=1`
   and poisoned enum under `ENUM=0,CORE=1` both reproduce pinned bytes) —
   giving the §3 crash-loop-breaker runbook's remediation lever #1 real
   test backing and making §6 Slice 2's "all 4 arm combinations
   golden-pinned" claim actually delivered. SPEC §3 condition 1 + §6
   item 10.

10. **The zero-trade short-circuit law's only net was a wall-clock bench
    below shared-CI noise ceilings, lane unstated (MINOR, coverage).**
    RESOLVED: the law is made BYTE-OBSERVABLE — `fold_block` returns an
    in-band `scans_run` count (§1.3 signature; observability/test data
    only, never persisted, never in any identity); the merge-blocking
    nets are deterministic `scans_run` unit pins in-crate (§7.5) and at
    the Python seam (§7.4); the wall-clock pin is explicitly LANED to
    bench-m4pro under `BRUTEX_M4PRO_STRICT`/`BRUTEX_M4PRO_ABS` with the
    ceiling arithmetic stated (2.4–2.7× target < the 3× shared ceiling ⇒
    a shared-tier pin is vacuous; the `test_write_consumer_overlap_bench`
    precedent cited) — §1.3 enforcement paragraph + §7.7. Bonus closure:
    the deleted-guard cargo-mutants output-equivalent class dies on
    `scans_run` (§7.6 — no new `exclude_re`).

11. **Crate-wide PROPTEST_CASES reduction under cargo-mutants degrades
    kill reliability of the EXISTING fno-math proptests (MINOR,
    coverage).** RESOLVED: §7.6 scopes the reduction to the NEW targets
    only via a DEDICATED `SWEEP_CORE_PROPTEST_CASES` env var (per-target
    `ProptestConfig`); the mutants workflow never sets the process-wide
    `PROPTEST_CASES` (grep-pinned), so the pre-existing `metrics.rs`
    proptest keeps its full case volume under every mutant and the
    zero-survivor 599-mutant set cannot acquire intermittent survivors
    on the STANDARDS-row-3 completion lane.

12. **Anti-vacuous goldens unimplementable against the fixture their
    literals come from — legacy dedup path + top_k=None, both refused by
    the engagement condition (MAJOR, edge).** RESOLVED: §5/§7.1 add the
    DIGEST-PATH GOLDEN VARIANT — same synthetic grid + synthetic
    `trial_stats_digest` + finite `top_k` ≥ ranked count, its
    `_GOLDEN_RUN_ID_DIGEST`/`_GOLDEN_CONTENT_HASH_DIGEST` literals MINTED
    ON THE OFF ARM in Slice 1 (pure-Python-authored by construction) then
    frozen; EVERY engagement-dependent golden (engaged events, poison
    twins, belt-restart, misflag-in-window) targets the variant; the
    ORIGINAL fixture + literals stay frozen and become the LEGACY-REFUSAL
    negative golden (flags ON ⇒ original literals + zero engaged events +
    `legacy_dedup_path`), which doubles as a finding-9 negative pin. The
    impossibility is stated in-doc so a naive implementation cannot
    reproduce it. §6 Slice 1/2, SPEC §3 condition 1 + §6 item 10.

13. **Spot-check has no defined comparand on the bounded rank arm —
    SurvivorColumns drops non-finalist ResultMetrics by design (MAJOR,
    edge; root shared with finding 3).** RESOLVED: §5's spot-check is
    re-specified as the RETAINED-CANDIDATE scheme, ONE mechanism on BOTH
    rank arms: 16 candidate masks drawn deterministically PRE-rank from
    the full kept list (flag-inclusive, arm-identical draw); the seam
    RETAINS the full served `ResultMetrics` for exactly those ≤16 masks
    during streaming (O(16) objects, a stated constant additive term
    beside — never inside — the `plan_rank_memory` watermark); POST-rank
    the first 4 non-finalist candidates are bit-compared SERVED-OBJECT vs
    `_oracle_metrics_pure` with the belt's disambiguation/latch/restart
    semantics. This verifies the row that was actually served (a
    nondeterministic-serve defect stays in-net); the fresh-re-fold
    alternative is recorded as REJECTED for exactly that weakness, and a
    columns-only compare is named as the forbidden narrowing. Degenerate
    semantics restated for candidates (`spot_check_sampled` = count
    actually compared; the <4-non-finalist-candidates case stated
    honestly); the (1−f)^4 arithmetic re-scoped to candidate yield; §7.4
    adds the bounded-arm served-object pin (spy: zero post-rank
    `fold_block` calls). SPEC §3 condition 4 + §6 item 10.

14. **Work-cap lower-bound hole / driver spin (MINOR, edge — duplicate
    of finding 5).** RESOLVED with finding 5 (lower clamp +
    guaranteed-progress law + exhaustion contract + probes).

15. **k=0 is a legal SweepRunConfig but absent from the CNS domain,
    validation contract, and §7.5 vectors (NIT, edge).** RESOLVED: §1.3
    validation clause (a) now states k=0 LEGALITY explicitly (C(n,0)=1,
    the single empty mask 0, conflict-free, hits == n_bars; validation
    must never tighten to `1 <= k`) and §7.5 bakes the named k=0 vector
    (kept == [0], enumerated == 1, next_rank == 1, exhausted == true);
    §7.4 carries the end-to-end k=0 seam pin. SPEC §6 item 10.

---

## Hostile-review resolution log — round 4

All 11 confirmed findings resolved as REAL design changes. Section
references are post-revision.

1. **Bounded-rank-arm finalist belt compared a RE-FOLD, not the served
   stream — the comparand class the design itself records as REJECTED;
   finalist stream-vs-rebuild serve-nondeterminism escaped the belt and
   the residual list (MAJOR, bitident).** RESOLVED: §5's belt paragraph
   now states COMPARAND PROVENANCE per rank arm (batch: `rc.metrics` IS
   the served object; bounded: the belt's oracle compare proves the
   REBUILD only — the streamed metrics are dropped and
   `_rebuild_bounded_finalists` re-folds) and adds the mandatory
   **BOUNDED-ARM SERVE RECONCILIATION** (same belt flag, O(top_k), zero
   extra oracle cost): (1) every rebuilt finalist's
   SurvivorColumns-retained ranking columns (incl. skew/kurt under the
   overfit gate) bit-compared against its OWN stream-written
   SurvivorColumns entry — sharpe is retained, so each finalist's
   streamed DSR input and every rank-bearing stream-vs-rebuild
   divergence is caught deterministically; (2) each rebuilt finalist
   re-runs `passes_gates` (must be True); (3) retained candidates that
   ARE finalists are compared full-column served-vs-rebuilt AND
   served-vs-oracle (the round-3 scheme discarded exactly this
   in-hand evidence). Any divergence ⇒ the distinct CORE-DEFECT reason
   `stream_rebuild_divergence` with the belt's
   disambiguation/latch/tuple-restart semantics. The belt guarantee
   sentence is re-corrected (the round-3 "detects any divergence that
   touches a ranked finalist's own metrics" was FALSE on the bounded arm
   for stream-only divergence — now claimed only for PERSISTED and
   RANK-BEARING values, which the reconciliation makes true); residuals
   (i)/(ii) are re-scoped to genuinely-non-finalist divergence, and the
   list grows to SIX-fold with the named class (vi): a NON-candidate
   finalist's stream-only divergence confined to non-retained,
   non-gate-flipping columns — unverified AND provably effect-free on
   every persisted/rank-bearing output (those streamed values are
   dropped and feed nothing that persists). Teeth: a STREAM-ONLY
   nondeterministic poison golden (corrupt stream, clean rebuild — a
   poison that PASSED the round-3 belt) ⇒ pinned bytes + one
   `stream_rebuild_divergence` + restart (§7.1); §7.4 unit pins for all
   three clauses; §1.4/§6 Slice 2/§8 R2/Slice-4 ADR wording updated;
   SPEC §3 condition 4 + §6 item 11.

2. **No deterministic reconciliation of the core's enumeration total
   against the driver's own `count_combos` — a self-consistent
   total_count defect was netted only probabilistically (MINOR,
   bitident).** RESOLVED: §1.3 gains the TOTAL-RECONCILIATION BELT — on
   loop exit the §1.4 driver asserts final `next_rank == n_masks` AND
   Σ enumerated_in_range == n_masks against the independent Python-side
   `n_masks = count_combos(cfg.k, n_unique)` it already computes
   (sweep_run.py:1300, SWEEP_START-stamped); mismatch ⇒ the named
   fallback reason `enum_total_mismatch` (whole tuple served by today's
   spine, warn-once — the `enum_no_progress` belt shape). Zero cost, a
   pure belt (today's spine satisfies the equality by construction). The
   §5 enum honest-residual is NARROWED to strictly in-range drops with
   self-consistent counts; §7.1/§7.4 pin the stubbed wrong-total return;
   §13 invariant added; §6 Slice 1 carries it; SPEC §3/§6.

3. **"No-index lints" cited as SIGABRT prevention do not exist in the
   crate and cannot be added crate-wide (MINOR, security).** RESOLVED:
   §1.3 and §8 R4 now mandate a MODULE-scoped
   `#![deny(clippy::indexing_slicing)]` on `sweep_core.rs` (all new code
   uses `get()`/checked accessors; a CI grep pin asserts the attribute
   is present — §7.5), with the honest statement that the crate carries
   NO indexing lint today and a crate-wide flip is not retrofit-able
   (trade_sim.rs 17 / metrics.rs 3 direct-index sites, consumed
   UNCHANGED) — review convention + fuzz remain the net for the
   pre-existing kernels. The two claim sites no longer cite a
   nonexistent mechanism.

4. **fold_block work-cap/progress law attributed to "the CORE" but
   unimplementable in-core under the decode refusal law; quotient- vs
   divisor-floored idiom (NIT).** RESOLVED: §3 fix (ii) names the
   ENFORCEMENT POINT per entry point — enum cap IN-CORE; fold cap
   DRIVER-side chunk sizing, a stated asymmetry with the contained
   failure mode (the loud `row_count_mismatch` refusal) — and the
   expression is corrected to the divisor-floored repo idiom verbatim
   (`max(1, cells // max(1, n_bars))`; the round-3 transcription
   ZeroDivisionErrored on n_bars == 0 and is withdrawn). §8 R9 restates
   "dual-work-bounded" with the named split; §7.4 unit-pins the sizing
   function incl. n_bars == 0.

5. **Per-kept headline '145–210 µs ⇒ 1.9–2.7×' folded the conditional
   Slice-3 lever into its upper endpoint (NIT, perf).** RESOLVED: the §9
   per-kept row is SPLIT per slice — Slice 2 ships ~166–206 µs ⇒
   ~1.9–2.4×; up to ~2.7× ONLY with the measured Slice-3 scratch-reuse
   lever — with the explicit instruction that the flip ADR (and any
   relayed summary) quotes the band of what ACTUALLY shipped; the
   formula row's interpolation note carries the same split.

6. **Work-cap liveness law had NO deterministic test net — every stated
   pin passed under a cap-IGNORING implementation; the only
   distinguishing gate was an unlaned ~1.4× wall-clock bench below the
   shared-CI noise doctrine (MAJOR, coverage).** RESOLVED: the
   `scans_run` standard is applied to this law using the in-band data
   the return tuple already carries — §3's matrix bullet adds
   CAP-RESPECTED PINS (`next_rank − start_rank ≤ max(1, max_enumerated)`
   AND `(next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars`
   one-mask slop, asserted per dual-knob-matrix call); §7.5 bakes
   in-crate EXACT-next_rank units (a 1-mask cap advances exactly 1; a
   k-mask cap exactly k) so the deleted-bound-check and `>=`→`>`
   cap-compare mutant classes die deterministically; §7.4 pins the
   Python fold-side twin (chunk-sizing unit + a spy pin that no chunk
   fed to `fold_block` exceeds it). The §7.7 GIL-hold bench is DEMOTED
   to a secondary net and honestly LANED (bench-m4pro strict lane;
   shared CI keeps only a catastrophic ≤ ~2 s backstop). SPEC §3
   condition 4 + §6 item 11.

7. **Reduced-case proptests for the NEW targets recreated the
   intermittent-survivor class round 3 closed for the existing suite
   (MINOR, coverage).** RESOLVED: §7.6 adds (a) a FIXED SEED for the
   mutants-lane runs of the new proptest targets
   (`SWEEP_CORE_PROPTEST_SEED` through the same per-target config seam —
   deterministic verdict per mutant, identical on the measure run and
   every weekly run) and (b) the DETERMINISTIC-KILLER RULE: every
   anticipated sweep_core.rs mutant class must be killable by a
   deterministic test (CPython-baked vectors, boundary pins,
   cap-respected units, `scans_run` units) — proptests are supplementary,
   never the killer of record; the measure-before-merge run executes the
   SAME reduced+fixed-seed configuration the weekly lane runs.

8. **`brutex/engine/sweep_core.py` — the module concentrating ALL safety
   routing — was in no Python mutation lane, and §7.6 neither added it
   nor named the omission (MINOR, coverage).** RESOLVED: §7.6 adds the
   module to the weekly python mutmut loop as its own pair (#1597
   timeout-wrapped-runner pattern), runtime measured on-branch within
   the weekly budget (overrun ⇒ a ledgered budget/split decision in the
   SAME PR, never silence); zero-survivor on the pair is a Slice-4 flip
   precondition. §5's load-bearing "no lane covers it" argument is
   reworded to the accurate post-change form (the lane is scheduled,
   not a merge gate — the negative engaged-event pins remain the
   merge-blocking discriminator). §6 Slice 2 carries the wiring.

9. **Layout golden claimed 'FOLD_*_FIELDS == Rust consts' with no
   mechanism to read the Rust tuples from Python (NIT, coverage).**
   RESOLVED: the exported-symbol set grows to FIVE —
   `SWEEP_CORE_FOLD_INT_FIELDS` + `SWEEP_CORE_FOLD_FLOAT_FIELDS` join
   the three round-3 symbols in the same one-commit pinned-sextuple
   update (§1.1) — so the §1.3/§7.1 layout golden performs a literal
   tuple == tuple compare against the Rust-exported field names, not a
   degraded stride/ABI-shape check.

10. **Burn-in counters width-blind across the u64→u128 crossover — the
    u128 monomorphisation could engage with the window already closed
    (MINOR, edge).** RESOLVED: §5's burn-in latch + both counters are
    WIDTH-KEYED (independent single-cell sets per kernel;
    `reset_self_check_for_tests(width=…)` duals) — each separately
    monomorphised kernel certifies itself, and a per-width latch never
    certifies or blocks the other. Honest reachability stated (bits ≥60
    unimplemented ⇒ the u128 window is production-unreachable today;
    the deliberate break with the #1531 single-cell precedent is
    stated); §7.4 pins the fresh-u128-window-after-u64-closure branch;
    §9 accounting reads once-per-process-PER-WIDTH; SPEC §3
    condition 4.

11. **The scans_run oracle was over-strong against bit1's own
    definition — a post-scan non-finite belt trip yields a
    scanned-but-flag-served row (NIT, edge).** RESOLVED: §1.3 mandates
    SCAN-LAST ORDERING — per mask, the drawdown/ulcer scan runs only
    after the envelope gate and every float fold completed finite (the
    computations are order-independent, so the ordering is free), so a
    bit1 trip always precedes the scan, a flag-served row never counts
    one, and `scans_run == count(plain rows)` is EXACT including on the
    FFI-unconstructible defence-in-depth paths; the flags definition
    cross-references the ordering, and §7.4/§7.5 pins carry it.

---

## Hostile-review resolution log — round 5

All 12 confirmed findings resolved as REAL design changes. Section
references are post-revision. Duplicated findings share one root-defect
resolution, cross-referenced.

1. **Neumaier token list omitted CPython's conditional final add
   `if (c && isfinite(c))` (NIT, bitident).** RESOLVED: §4 row 6 now
   enumerates the GUARDED final add as a ported token — reproduced
   token-for-token from CPython SOURCE (Python/bltinmodule.c, gh-100425),
   never from the table's paraphrase — with the stated provable
   value-invisibility for THIS fold (non-negative summands ⇒ `f_result`
   never −0.0, `c` never −0.0, non-finite `c` excluded by the bit1 belt)
   so the guard can never silently become load-bearing if a second
   builtins-sum port over sign-mixed data ever lands; the
   summation-algorithm law and the per-expression review checklist carry
   the same clause. SPEC §4 row 6 mirrored.

2. **Width-selection expression crashes on the config-legal empty
   `condition_bits` (k=0, n_unique=0) arm (MINOR, security; duplicate
   root with finding 12 NIT, edge).** RESOLVED: §5's width formula is
   TOTALISED — `max(condition_bits, default=−1)+1` /
   `max(pair indices, default=−1)+1` — so empty terms contribute 0 and
   width keys off bar_bits alone (never empty: empty bars are refused at
   `run_persisted_sweep`, sweep_run.py:1287-1288); width selection is
   DEFINED to evaluate OUTSIDE the `kernel_error` narrow catch as part of
   the engagement condition (with the total expression a ValueError there
   is structurally impossible — neither an uncaught crash nor a
   misattributed fallback); §7.4 pins the bits=[]/k=0 seam vector
   (engages, kept == [0], spine-identical bytes) and §7.5 bakes the
   empty-bits in-core twin beside the k=0 vector. SPEC §3 condition 4.

3. **Enum-side default `max_enumerated` retained the bare `/ n_bars`
   division round 4 withdrew for the fold sibling (NIT, security).**
   RESOLVED: §3's enum default is corrected to the divisor-floored repo
   idiom `clamp(2^28 // max(1, n_bars), 4096, 2^20)`; both sibling sizing
   expressions now carry the same floor and the §7.4 n_bars == 0 unit-pin
   mandate covers both.

4. **Round-4 serve reconciliation claimed O(top_k) "from evidence the
   machinery already holds" — but no finalist→SurvivorColumns position
   exists; as written it was O(kept)/a ~GB dict or an out-of-plan edit
   (MAJOR, perf).** RESOLVED: the false claim is WITHDRAWN in place
   (§5 states the real structures: heapq indices discarded at
   `sweep_stream.py:385-401`, no reverse map, kept-order masks not
   numerically sorted) and the mechanism is REPAIRED with the POSITION
   CARRY — `BoundedFinalist` gains an additive `position: int` field
   (the `positions[survivor]` value in hand at the exact construction
   site that discarded it), authorized as the ONE Slice-2
   `sweep_stream.py` edit (§11 amended; additive, bit-pinned —
   byte-neutrality + `mask_at(position) == mask` positional belt pinned
   in §7.4). Clause (1) reads stored entries at the carried position via
   existing accessors (+ a tiny additive per-position accessor iff a
   retained column lacks one) — genuinely O(top_k), ~µs/finalist, and
   the §9 accounting line now cites this mechanism as its licence. The
   O(kept) scan and mask→index dict alternatives are recorded as
   REFUSED with their costs. §5, §6 Slice 2, §9, §11, §13, SPEC §2/§3
   condition 4/§6 item 12.

5. **§9 blended formula's single `f·PK` term conflated trade-bearing and
   zero-trade kept masks — whole-tail per-kept ratio ≈1.7–1.8×, below
   the 1.9–2.4× headline; the combo_counts rebuild would inherit the
   overstatement (MINOR, perf).** RESOLVED: the blended formula row is
   SPLIT — `f_zero·PK_zero + f_trade·PK_trade` (all four constants
   already measured, baseline.md §2) — and declared the NORMATIVE tool
   for the round-2-mandated combo_counts.md §3 rebuild (the withdrawn
   single-f form forbidden); a NEW BLENDED per-kept row composes the
   doc's own two rows at its own profiled 66/34 density (today ≈182 µs →
   Slice 2 ≈108 µs ⇒ ~1.7×, ≈1.8× with Slice 3) and the per-kept
   headline row is re-titled TRADE-BEARING with the explicit
   instruction that any relayed "1.9–2.4×" states trade-bearing-only
   scope. §9.

6. **R9's "#1291 needs no analysis — tuples get FASTER, the floor
   becomes more conservative" falsified by the design's own belt/burn-in
   numbers at top=1000 (MINOR, perf; duplicate root with finding 10
   MAJOR, edge).** RESOLVED with finding 10 below — the one-liner is
   withdrawn and replaced by the full arithmetic + the STALL-BUDGET
   GUARD.

7. **Burn-in test seam under-specified — a single `remaining` scalar
   cannot express the window states the doc's own goldens must force
   (MINOR, coverage).** RESOLVED: §5's seam bullet SUPERSEDES the #1531
   single-scalar signature (stated, with `rust_metrics.py:295` cited)
   with the full per-width state-setter
   `reset_self_check_for_tests(width=…, total_remaining=…,
   plain_remaining=…, cap_remaining=…, latched=…)` +
   `reset_enum_self_check_for_tests(width=…, ranges_remaining=…)`; each
   window-state golden NAMES its counter state; the natural warm-up-tuple
   closure recipe is ALSO mandated as one shared integration-tier
   fixture helper (the seam proves states cheaply, the warm-up proves
   real closure once). §5, §6 Slice 2, §7.4.

8. **"cargo-mutants has no inline-pragma mechanism" is false —
   `#[mutants::skip]` exists (NIT, coverage).** RESOLVED: §7.6 states
   the attribute EXISTS and is FORBIDDEN here — function-coarse, it
   would wholesale-exempt the killable threshold-value mutants the plan
   requires viable-and-killed; per-mutant granularity requires
   `exclude_re` (the remedy stands on its true justification), and the
   CI grep-pin list is extended to refuse `mutants::skip` in
   sweep_core.rs so the attribute cannot be "simplified" onto the
   helpers.

9. **Digest-variant literals labelled "pure-Python-spine bytes … never
   Rust-authored" — false provenance under the ADR-0017 default the
   doc's own round-1 item 3 established (NIT, coverage).** RESOLVED:
   the minting instruction now reads "SHIPPING-SPINE-authored —
   NEW-CORE-free by construction", with the true provenance stated
   (the OFF arm routes drawdown through `fno_math.sweep_drawdown_stats`
   under `SWEEP_RUST_METRICS_DEFAULT=True`; #1531-proven bit-identical,
   bytes unaffected — the literals are the exact twin-run-pinned
   shipping bytes, which is what the goldens need). §5 anti-vacuous
   bullet.

10. **#1291 stall composition falsified — deterministic
    `SweepStallError` + identical-resume livelock on reachable low-k 1m
    shapes under the default-ON belt/burn-in (MAJOR, edge).** RESOLVED:
    NEW §5 engagement clause — the STALL-BUDGET GUARD: at engagement
    the seam computes a CONSERVATIVE overhead bound (≤512 burn-in oracle
    walks + enum shadow ranges + (top_k + spot candidates) oracle
    re-sims, priced at a pessimistic measured-on-branch
    `ORACLE_NS_PER_BAR_BOUND`, env-overridable) and recomputes the
    tuple's OWN #1291 effective deadline from the SAME public formula
    (constants + env resolvers lazily imported from sweep_parallel —
    ZERO edits to the not-ours file; a §7.4 drift pin compares the
    recomputation against the real `_effective_stall_seconds`); if the
    bound exceeds `SWEEP_CORE_STALL_BUDGET_FRACTION (0.5)` × deadline
    the core REFUSES (`stall_budget_exceeded`, warn-once) and today's
    spine serves at today's timing — composition holds by construction,
    and the livelock state is never entered. Honesty stated: the
    recomputed deadline is a LOWER bound of the parent's max-in-flight
    deadline (guard errs toward refusing); the guard runs on every arm
    (refused shapes are exactly those where overhead dominates the
    tuple — a net win) *[the "exactly … a net win" biconditional was
    FALSIFIED and WITHDRAWN in round 6 — see round-6 item 7: the guard
    is a liveness bound, not a net-win bound]*; raising
    `BRUTEX_SWEEP_STALL_SECONDS` flows
    through both formulas identically (the knobs are now NAMED). §8 R9
    rewritten with the falsifying arithmetic in place of the withdrawn
    one-liner; §9 accounting + §11 composition paragraph updated; the
    Slice-4 flip ADR must present the MEASURED max belt+burn-in phase
    vs the effective deadline at the operator's actual `--top`. SPEC §3
    condition 4 + §6 item 12.

11. **Enumeration burn-in width-BLIND across the u64→u128 crossover —
    the round-4 dilution class unfixed on the sibling entry point, and
    the u128 enum path's ONLY production net (MINOR, edge).** RESOLVED:
    §5's enum burn-in bullet keys `SWEEP_ENUM_SELF_CHECK_RANGES = 4`
    (and the enum latch) PER KERNEL WIDTH, mirroring round-4 item 10,
    with the stated stronger rationale (a dropped mask cannot be
    sampled, so the enum residual's production net is the enum burn-in
    alone), the identical reachability/cost honesty (second window only
    if u128 engages; production-unreachable today), the
    `reset_enum_self_check_for_tests(width=…)` duals, and the §7.4
    fresh-u128-shadow pin. SPEC §3 condition 4.

12. **Width formula undefined on empty condition_bits (NIT, edge —
    duplicate of finding 2).** RESOLVED with finding 2 (empty-safe
    `default=−1` terms; outside-the-catch evaluation; §7.4 seam vector +
    §7.5 empty-bits core twin).

## Hostile-review resolution log — round 6

All 15 confirmed findings resolved as REAL design changes. Section
references are post-revision. Duplicated findings share one root-defect
resolution, cross-referenced.

1. **Round-5 stall-budget guard hard-depends on unmerged/revertable #1291
   symbols, contradicting §11's no-PR-dependence law; the pre-#1291 arm
   was entirely unspecified (MAJOR, bitident; duplicate roots with
   findings 10 MINOR/coverage and 13 MINOR/edge).** RESOLVED: §5 gains
   the mandatory ABSENT-SYMBOL ARM — every #1291 symbol resolves via
   `getattr(sweep_parallel, name, None)`; when `_effective_stall_seconds`
   / `STALL_MIN_MASK_BARS_PER_SEC` / the stall env resolvers are absent,
   the effective deadline is the FLAT `DEFAULT_STALL_SECONDS` (present on
   every tree, sweep_parallel.py:358) — not an approximation but the
   EXACT deadline a pre-#1291 parent enforces, closing failure mode (b)
   (a work-scaled guard against a flat-deadline parent would ADMIT
   tuples whose belt exceeds the real 300 s deadline — the livelock the
   guard exists to prevent); guard evaluation is defined TOTAL (it runs
   OUTSIDE the narrow `kernel_error` catch — the width-expression
   standard), with any unresolvable constant or escaped exception the
   NEW named refusal `stall_guard_unavailable` (warn-once, spine
   serves — never an uncaught per-tuple crash, closing failure mode
   (a)); the `_effective_stall_seconds` drift pin is `skipif`-scoped to
   symbol-present trees and a dedicated absent-symbol unit pins the flat
   arm (§7.4). §11's blanket independence sentence is RECONCILED: no
   dependence on #1295 at all, and no dependence on #1291's symbols
   EXISTING (round-6 verifiers returned contradictory #1291 merge
   states against different refs, so the implementation assumes
   neither); symbol-existence — invisible to the file-disjointness
   check — is handled at runtime. §5, §8 R9, §11, §7.4; SPEC §3
   condition 4 + §6 item 13.

2. **min_hits==0 unconditional hits-count — a silent O(n_bars)-per-mask
   cost ADD with no mandated skip, and §0/§9's "exactly as today" cost
   claim false for those grids (MINOR, bitident; duplicate root with
   finding 12 MAJOR, edge).** RESOLVED: §1.3 gains the mandated
   MIN-HITS SHORT-CIRCUIT LAW (the enum-side sibling of the zero-trade
   scan law, the design's own round-2/3 pattern): at `min_hits == 0`
   the per-mask O(n_bars) supermask count MUST NOT run — today's
   `if cfg.min_hits > 0 and survivors:` guard twin
   (sweep_run.py:1465-1473). Enforcement is BYTE-OBSERVABLE in-band:
   `enumerate_prefilter` returns a NEW `hits_counted_in_range` field
   (min_hits=0 ⇒ 0; min_hits>0 ⇒ enumerated − conflict_pruned — the
   `scans_run` mechanism), with deterministic merge-blocking nets
   (in-crate cargo units + a Python seam pin, §7.4/§7.5; a
   deleted-guard mutant is data-identical but flips the count and dies).
   §0 and §9 (the per-enumerated row + the NORMATIVE split-formula row
   the combo_counts.md §3 rebuild inherits) are SCOPED: the
   `s·(0.257 ns × n_bars)` prefilter term exists only at min_hits>0 and
   is ZERO in BOTH columns at min_hits==0. §1.4/§2/§13 updated; SPEC §3
   condition 4 + §6 item 13.

3. **The round-4 "strictly IN-RANGE drops" enum residual omitted the
   symmetric SPURIOUS-KEEP class (MINOR, bitident).** RESOLVED: §5's
   anti-vacuous residual is SYMMETRISED to "in-range KEPT-SET
   divergence — silent DROPS or SPURIOUS KEEPS — with self-consistent
   counts", with the mechanism stated (the total-reconciliation belt
   reconciles TOTALS, never the kept set; a spuriously-kept mask walks,
   shifts z-normalisation/sidecar/DSR population, can persist as a
   finalist, and the belt/spot-check verify metrics GIVEN the mask —
   never membership); the Slice-4 flip-ADR list carries the
   both-direction wording verbatim (§6), and §7.1 gains the SYMMETRIC
   poison twin (a wrongly-KEPT conflicted mask past enum burn-in MOVES
   tallies + content hash). SPEC §6 item 13.

4. **fold_block had no in-core work ceiling and §3's containment
   rationale was factually wrong (MINOR, security).** RESOLVED: the
   false "bounded by the loud row_count_mismatch refusal" sentence is
   CORRECTED in place (a conforming core fed an oversized chunk returns
   consistent lengths — no refusal fires; the exposure is an unbounded
   GIL hold, not divergence), and the cheap runtime backstop is ADDED:
   `fold_block` returns a whole-chunk `Err("fold_work_ceiling")` when
   `masks.len() × n_bars > 2^28` (2× the override's own upper clamp —
   unreachable by any clamp-legal configuration; whole-chunk Err is
   compatible with the decode law, which excludes only PARTIAL
   progress), mapped by the seam to the NAMED fallback reason
   `fold_work_ceiling`. §3, §5 reason list, §7.4/§7.5 pins, §13; SPEC
   §3 condition 4 + §6 item 13.

5. **BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR "clamps" asserted but never
   defined — a digit-dropping typo could under-price the guard and
   resurrect the livelock (MINOR, security).** RESOLVED: §5 defines the
   clamp spec — LOWER clamp AT the committed measured-on-branch default
   (the override can only RAISE the priced rate; more pessimism ⇒ more
   refusals, the safe direction — the legitimate un-refuse lever is
   `BRUTEX_SWEEP_STALL_SECONDS`, so this knob has no gutting-capable
   low range by construction), UPPER clamp at 1000× (finite + loud);
   each clamp emits `stall_guard_clamped` warn-once with a `direction`
   field; §7.4 carries degenerate probes in both directions beside the
   work-cells `=1`/`2**44` probes. SPEC §3 condition 4 + §6 item 13.

6. **Decode law hardcodes ×8 byte widths but only sys.byteorder was
   pinned — array('q'/'d') itemsize is platform-native (NIT,
   security).** RESOLVED: `array('q').itemsize == 8 ==
   array('d').itemsize` is added to BOTH the layout golden (§7.1,
   beside the LE pin) and the engagement precondition (§1.3/§5 —
   refusal `native_itemsize_mismatch`, warn-once; on a hypothetical
   non-8/8 host rows would MISPARSE rather than refuse, so the
   precondition refuses first). Completeness, not exposure (all actual
   targets are 8/8) — stated. SPEC §3 condition 4 + §6 item 13.

7. **The guard's "refused shapes are exactly those where overhead
   dominates — a net win" biconditional is false: engaged mid-TF
   top=1000 tuples are a net wall-clock REGRESSION (MAJOR, perf).**
   RESOLVED: §5 note (b) is REWRITTEN — the guard is a LIVENESS bound,
   NOT a net-win bound (the 2.5e8 floor is #1291's hang-detection
   floor with ~4× headroom vs the ~1e9 kernel, not a throughput model,
   so mid/high-TF deadlines run ~10–15× actual compute and the guard
   admits overhead up to several times the tuple's own work); the
   concrete engaged-net-loss arithmetic (48k k≤5 @top=1000: deadline
   ≈1164 s ⇒ 0.5× = 582 s ≫ belt 100–200 s ⇒ ENGAGES; net ~1.3–1.6×
   slower than today's spine at default kept fractions) is stated in
   place; the ENGAGED-NET-REGRESSION class is the NAMED residual (vii)
   in §5/§6/§9 and the flip-ADR list, with the aggregate mitigation
   strengthened (a measured 20–50%-scale belt fraction on the
   operator's grid shape BLOCKS the flip absent an explicit
   accepted-cost statement); a per-shape net-win engagement term is
   deliberately NOT added (it would require a throughput model this
   design refuses to invent — stated). The round-5 log item 10's "net
   win" line carries an in-place withdrawal note; §8 R9 + §9 stall
   paragraphs re-scoped to "closed for the healthy-build overhead
   cause". SPEC §3 condition 4 + §6 item 13.

8. **§9 per-kept headline basis omitted retained segments (Stage-1B
   prefilter + rank) while retaining the eliminated exits-marshal —
   both band endpoints mis-derived (MINOR, perf).** RESOLVED: row 1's
   basis is REBUILT — walk 62 + drawdown scan COMPUTE ~105 (marshal
   excluded as eliminated; baseline §3 splits the 130.1 µs FFI into
   ~105 compute + marshal) + retained prefilter ~12.5 (min_hits>0) +
   retained rank ~1.3 + folds/marshal/combine ⇒ Slice 2 ≈ 185–195 µs ⇒
   **~2.0–2.2×** (up to ~2.5× with Slice-3), the today column annotated
   as SUMMING the retained segments; the withdrawn 1.9–2.4×/2.7× bands
   are marked never-relay; the BLENDED row recomposes at the corrected
   PK_trade_post ⇒ **~1.6–1.7×** (Slice 2) / ~1.8× (Slice 3); every
   downstream quote (formula row, §6, SPEC items 11/12 + condition 4)
   updated. Within the stated ±30% error bars and the mandated
   on-branch re-measure — but the two headline rows now sum their own
   segments consistently, and the combo_counts §3 rebuild inherits the
   corrected constants.

9. **The stall guard priced the enum shadow at a flat per-enumeration
   constant, omitting the shadow's O(n_bars) kept-list prefilter cost —
   the one term erring toward ENGAGING (NIT, perf).** RESOLVED: the §5
   formula's shadow term becomes `enum_ranges_remaining × max_enumerated
   × (ENUM_SHADOW_NS + n_bars × PREFILTER_NS_PER_BAR)` — each shadowed
   mask priced as if conflict-free with the count running (pessimistic
   even on min_hits=0 grids, where the §1.3 skip law makes the shadow
   cheaper); every term in the bound now errs toward refusing, so a
   future `max_enumerated` retune cannot silently grow an under-counted
   term. SPEC §3 condition 4.

10. **§11's no-PR-dependence claim falsified; no degradation specified
    on import failure (MINOR, coverage).** RESOLVED with finding 1 (the
    absent-symbol arm + `stall_guard_unavailable` + the reconciled §11
    wording scoping the claim and recording the contradictory verifier
    evidence).

11. **§7.2's ship-condition twin-runs include real-data grids with no
    execution venue or evidence recording — CI cannot run them (MINOR,
    coverage).** RESOLVED: §7.2 gains the EXECUTION VENUE + EVIDENCE
    LAW mirroring §7.10 — the golden-fixture twin is the CI-lane arm;
    the real-data grid twin and the workers>1 twin are REQUIRED
    ON-BRANCH gates per slice (Slice 1: OFF vs ENUM; Slice 2: all four
    arms), one command each on the lake-bearing box, with commands +
    both arms' `FlushResult.content_hash` / store-Parquet sha256 /
    sidecar hashes recorded VERBATIM in the PR body — merge blocked
    until the evidence is present. §6 Slices 1/2 cross-referenced; SPEC
    §3 condition 1 + §6 item 13.

12. **min_hits==0 count-skip implied but never mandated or pinned —
    the design's own bit-identical-but-cost-catastrophic class left
    open on the sibling enum entry point (MAJOR, edge).** RESOLVED with
    finding 2 (the mandated law + the in-band `hits_counted`
    observable + deterministic pins + the §0/§9 scoping).

13. **The stall-budget guard's #1291-only symbols do not exist in the
    tree; no absent-symbol arm; §11 contradiction (MINOR, edge).**
    RESOLVED with finding 1.

14. **Belt-mismatch tuple-restart wall time excluded from
    T_overhead_bound — the livelock class survives on the restart path
    of a divergent build (MINOR, edge).** RESOLVED: §5 note (d) states
    the RESTART-PATH residual honestly — the bound prices HEALTHY-build
    machinery only; pricing the restart's spine pass requires a
    kept-fraction throughput model (an f=1 bound would refuse every
    work-governed tuple), so on a genuinely-divergent build the
    belt-mismatch → restart → stall-abort → resume cycle remains
    reachable ON THE INCIDENT PATH (loud, results-safe: sentinel-absent
    redo) — named residual (viii) in §5/§6/§9 and the flip-ADR list;
    the §3 crash-loop-breaker RUNBOOK is EXTENDED with the operator
    pointer the stall abort lacks: "repeated `SweepStallError` aborts
    immediately following belt-mismatch / `stream_rebuild_divergence`
    restarts ⇒ `BRUTEX_SWEEP_RUST_CORE=0` is remediation lever #1".
    §8 R9 re-scoped accordingly. SPEC §3 condition 4 + §6 item 13.

15. **Post-latch mid-tuple continuation of ENUMERATION unspecified
    after an enum burn-in mismatch (NIT, edge).** RESOLVED: §5's enum
    burn-in bullet gains the MID-TUPLE CONTINUATION CONTRACT — on a
    shadow mismatch the Python window ships, the latch flips, and the
    remainder of the rank space is served by continuing today's Python
    enumeration from `start_rank + len(python_window)` (the PYTHON-side
    count, never the untrusted core `next_rank`) via `itertools.islice`
    of the shadow's own `iter_masks` stream to exhaustion; the §1.3
    total-reconciliation assert is DEFINED over the mixed provenance
    (Σ core-range + Python-range enumerated == `count_combos` — exact
    because both share `iter_masks` lexicographic order); §1.4
    cross-references it and §7.4 pins a stubbed range-2-of-4 mismatch
    completing byte-identical with the mixed assert passing. SPEC §6
    item 13.

---

## External fold + lock round — resolution log (2026-07-09)

Status flip: FINAL → **LOCKED FOR CHECKPOINT 1 pending verification round.**
Every item below cites its source id and names its resolution location in
this doc (and SPEC_RUST_SWEEP_CORE.md where restated). Sources:
(V) `external_findings_verifier_b.md` (Verifier B findings + baseline-truth
pack + PERF RECONCILIATION); (FF) the 7-round hostile-review fix-forward
register (13 items, coordinator severity-gate ruling 2026-07-09);
(CR) coordinator rulings.

### Verifier B findings (V)

1. **F3 (div_euclid + negative-numerator golden; RT-01 downgraded
   MAJOR→MINOR/dormant)** → §4 row 1: `div_euclid` NAMED in the fold
   contract (Python `//` floors, Rust `/` truncates; −7//2 = −4 vs −3), the
   §7.4 negative-numerator dual designated the F3 golden cell; dormancy
   basis stated (expectancy not in blake3 identity; Python-single-sourced;
   trading-core already div_euclid). SPEC §4 row 1 restated.
2. **F5 (u128 mask as bare JSON int — serde_json u64 aliasing false-skip)**
   → §12 new register row: the MASK-SERIALIZER LAW (32-nibble hex string
   per spec.rs:219 / `mask:%032x`, never a bare JSON int; grep pin) + §7.1
   MASK-SERIALIZATION golden over the frozen corpus (e) + §13 invariant.
   SPEC §4 new row 15.
3. **F1 (positional checkpoint × unranked enumerator — silent prefix-skip)**
   → §3 new bullet: ENUMERATION-ORDER / CHECKPOINT IDENTITY PIN — the
   INDEX→TUPLE-DIGEST golden (OFF ≡ ON positional map) + the mask-level
   list-equality property; tuple-order authority stays the untouched Python
   grid iteration, now TESTED. §7.1 golden added. SPEC §6 item 14.
4. **F2 (crash-REDO append-conflict abort; RT-09)** → §3 kill −9 bullet +
   §7.2: the KILL-AFTER-FLUSH REDO CELL is a mandatory twin-run ship-gate
   cell (kill after flush/before sentinel → byte-identical redo + the
   corpus-(b) audit-chain shape). SPEC §6 item 14.
5. **F4 (pyo3 PanicException is BaseException — bypasses `except Exception`
   + the quarantine taxonomy)** → §3 PANIC POLICY (explicit): (a) profile
   stays `panic="abort"` — PanicException structurally unconstructible,
   grep-pinned; (b) defence-in-depth seam catch of
   `pyo3_runtime.PanicException` mapped into `SweepComboError`/TUPLE_FAILED
   with distinct reason `kernel_panic_exception` + ERROR alert + latch.
   SPEC §7 restated.
6. **F23 (supervised-daemon per-tuple-skip parity)** → §3 PANIC POLICY (c):
   parallel arm = pool quarantine per-tuple parity; sequential/daemon arm
   SIGABRT = relaunch crash-loop → breaker → GAVE_UP, stated honestly with
   runbook lever #1; a supervisor-distinct per-tuple-skip exit code is a
   NAMED FOLLOW-UP for the P2 owner (not-ours file), never silently claimed.
7. **F27 / B-INV-102 (fourth skip authority — empty-marshalled-window
   false-skip)** → §1.3 `__new__` WINDOW-LENGTH INVARIANT: empty-window
   skip stays Python-side PRE-FFI; the seam asserts `core.n_bars() ==`
   Python window length (`bars_length_mismatch` refusal); §7.4 pins both
   directions. §5 partition table row; §13 invariant.
8. **F21 (one-preload / hash-what-you-consume — LAW not gate)** → §1.3
   Frozen-input LAWS: core owns ZERO lake I/O; bars cross as DATA from the
   one preload; stated as LAW because the bypass grep is blind to a Rust
   reader. §13 invariant.
9. **F22 (paisa-minting seam — repr-mediated Decimal HALF_UP)** → §1.3
   Frozen-input LAWS: port-by-name only; corpus (f) tie cases
   (1.005 → 101 ≠ 100) are the break-oracle. §13 invariant. SPEC §4 row 15.
10. **F15/F18 (shared trading-core never forked; FIB_LEVELS_LEN=15 frozen;
    calendar frozen input; count_hits ≡ count_supermasks at strict >=)** →
    trading-core fence pre-existing (§1.1, diff EMPTY — unchanged); §1.3
    min-hits law gains the STRICT->= BIT-PARITY GATE (property over both
    widths + corpus (h) boundary goldens); calendar contract below.
11. **RT-40 / E-18 (Muhurat weekly-anchor crash — ESCALATED LIVE MAJOR,
    then FIXED on `claude/data-edge-guard-fixes`, pending merge)** → §1.3
    CALENDAR-INPUT CONTRACT: pins the POST-FIX ABSTAIN SHAPE as parity
    truth (fib=None, Group-4 bits cleared, one
    `stage1a.muhurat_weekly_anchor_ineligible` WARN, tuple COMPLETES —
    never TUPLE_FAILED, never fabricated bits); pre-merge trees exhibit the
    pre-fix abort shape (grid-abort/exit-2) as a pre-existing engine truth;
    the twin runs against whichever shape the merged tree exhibits. §13.
12. **Dup-ts last-wins oracle (i) (RT-37 — zero golden coverage)** → §7.1
    DUP-TS LAST-WINS golden (RT-37 fixture; last-wins bytes pinned on both
    arms; teeth against a future earlier-index Rust consumer). §4 row 7
    unchanged (ts_uncertified fallback already correct).
13. **Baseline-truth-pack honest framing (0 live cross-language divergences
    today — F5/F10/F14/F22 are PORT BREAK-ORACLES)** → carried in the §7.1
    golden descriptions + this log; the nine frozen corpora (a)–(i) are the
    Rust twin's byte-exact oracle set. (Final-lock scope correction of this
    entry's "goldens reference them by letter" over-claim: goldens reference
    (b)/(e)/(f)/(h) BY LETTER; (a)/(c)/(g)/(i) are folded BY CONTENT — the
    SPEC-pinned `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` twin pins (a), the
    §3 flush→sidecar→sentinel write-order law (c), the
    vocab_golden_vectors.json diff-EMPTY pin (g), the §7.1 DUP-TS golden
    (i); corpus **(d) float-text** (repr(1e-5)='1e-05'; −0.0 normalized to
    +0.0 in combo.py canonical JSON) is OUT-OF-SURFACE for this design —
    every run-identity serialization stays Python-side (§1.2
    identity-blind kernel), so no Rust surface consumes float repr in this
    conversion; the corpus RE-ARMS as a mandatory golden iff a future
    slice ever serializes floats in Rust.)
14. **PERF RECONCILIATION (BINDING)** → NEW §9.1: two-term pinned law
    (walk ≈6.2 ns/mask·bar @RAYON_NUM_THREADS=1 + ≈4.1 µs/trade,
    R²≥0.993); the unpinned 1.26–1.28 ns rate qualified at EVERY quote site
    (§3 fold-cell sizing + on-branch cap re-derivation; §9 row 1; the 1m
    caveat) and banned from campaign math; campaign band ≈1.3–3.1×
    (≈2.3–2.5× at FIX-L k=2 median density; k≤6 ≈5.9 d → ~2–3.5 d
    port-only) replacing every old-framing headline; algorithmic levers
    FIRST-CLASS in §10 (parallelism topology with the measured 3.0–3.97×
    in-kernel rayon ratios, entry-sparsity, kept-vs-enumerated shaping);
    acceptance criteria = the S2 gate (inclusive ≤ ~1.15× of kernel sum,
    ~14.0–14.5 ms @604k dense) + trades-never-cross-FFI with the measured
    1.04–1.16× half-port counterexample; FIX-M anomaly flagged DO-NOT-CITE.
    §9 grid row now points at the band; SPEC §1/§5 re-anchored.
    [Since corrected by FL-4/FL-12/§9.1 (and FL-11 for the "R²≥0.993"
    clause) — see the withdrawal register (the final-lock Register
    FL-1..FL-12 below, whose supersession note retains this entry
    VERBATIM as history while withdrawing its 5.9 d → 2–3.5 d
    day-counts, "3.0–3.97×", and "R²≥0.993" figures). Verifier A
    round-2 register hygiene — annotation only, history not rewritten.]
15. **F7 (BRUTEX_SWEEP_STALL_SECONDS semantic collision #1295 vs #1291)** →
    §11 pending-rulings block: design binds via getattr to whatever the
    merged tree resolves; ruling required before the Slice-2 flip ADR.
16. **F8 (#1295 throughput tripwire × autorevert → SUSPECT_COMPLETE
    branding)** → §11 pending-rulings block: required interaction stated
    (arm-aware baseline OR autorevert-informs-tripwire hook); flip ADR
    BLOCKED on the ruled mechanism.

### Fix-forward register (FF, 13 items)

1. **Tallies-only enumeration divergence unnamed + no count-only teeth
   (MINOR)** → §5 residual list extended to NINE-fold with class (ix)
   (full mechanism + persisted-bytes consequence stated); §7.1 COUNT-ONLY
   poison golden (sidecar hash MOVES, store hash pinned UNCHANGED); §6
   Slice-4 ADR list updated; §13 tallies row honesty-scoped. SPEC §6
   item 14.
2. **§4 row-12 percentile branches + row-6 int-start transition (NIT)** →
   §4 row 12 enumerates the len==1 and lower==upper branches WITH
   value-invisibility proofs + port-from-SOURCE note; §4 row 6 enumerates
   the builtins-sum int→float transition with its non-negative-summand
   proof.
3. **§3 kill−9 byte-identical claim divergent-build-unscoped (NIT)** → §3
   kill −9 bullet scoped to HEALTHY builds + the divergent-build
   engagement-history-dependence disclosure (process-global latch shape,
   #1531 precedent, results-safe).
4. **Enum shadow dilution on the range-count axis (MINOR)** → §5 enum
   burn-in re-keyed to a MASK-COUNT floor (`SWEEP_ENUM_SELF_CHECK_MASKS =
   2^16`, ranges the unit, 64-range hard cap + `enum_shadow_floor_unmet`
   residual); cost line restated in masks; stall-guard shadow term
   re-expressed as `enum_shadow_masks_remaining(width)`; test seam
   signature updated.
5. **max_work_cells min-hits-blind FFI-call inflation unstated (NIT)** →
   §1.3 CHARGING RULE comment (explicit, with the ~103,600-call
   arithmetic) + §9 formula-row call-count-tax note.
6. **§9 endpoints rest on the withdrawn ~3 µs combine bound (MINOR)** →
   §9 row 1 basis rebuilt at ≈10–13 µs (intercept + relocated seam);
   Slice-2 band ~2.0–2.1×, Slice-3 ~2.0–2.4× ("up to ~2.5×" + blended
   "≈1.8×" WITHDRAWN, never relay); blended row recomposed ~1.6×; SPEC §1
   target band ~10–13 µs; SPEC §5 "removes ~230 µs" corrected.
7. **Stall-guard burn-in starvation feedback + 8× input ambiguity
   (MINOR)** → §5 guard: `burnin_total_remaining` DEFINED as the 64-floor
   remaining count (never the 512 cap, with the disclosed pathological-
   extension under-pricing); the ENGAGEMENT-STARVATION FEEDBACK LOOP named
   + the engagement-coverage envelope disclosed (fresh-process 1m refusals;
   top=1000 permanent floor-deadline refusals); §9 grid-row measurement
   mandate extended to the operator's `--top`.
8. **§0 "enumerated masks never materialise" contradiction (NIT)** → §0
   thesis corrected to NON-KEPT scope with the kept-list mechanics stated.
9. **Cap-respected pins encode the min-hits-blind charge — implementer
   trap (NIT)** → §1.3 CHARGING RULE: "do not fix it" stated with the
   failing-pin consequence (shared resolution with FF-5).
10. **Stall-guard symbol-PRESENT arm uncoverable on the #1291-absent tree
    (MINOR)** → §7.4 STALL-GUARD SYMBOL-PRESENT duals via monkeypatch-ADDED
    fake symbols; only the drift pin stays skipif-scoped.
11. **Fallback-reason precedence unspecified under multi-failure fixtures
    (NIT)** → §5 FALLBACK-REASON PRECEDENCE LAW (listing order, first
    failure wins; the legacy golden's single `legacy_dedup_path` warn is
    now rule-produced).
12. **big_endian_host lacks a probe seam (NIT)** → §1.3 PROBE SEAM
    SYMMETRY: `_host_byteorder()` patchable engagement-time probe (never a
    module constant); §7.4 coverage mechanically true.
13. **Engagement chain arm-blind (MINOR)** → §5 ARM-CONDITION PARTITION
    table (per-arm conditions/reasons; enum arm engages under its own
    column incl. CORE=0/ENUM=1; dup-ts enum behavior defined; Slice-1 set
    == the enum column; goldens freeze the partition).

### Coordinator rulings (CR)

- **compute.py Slice-0 zone GRANTED** (conditions: behavior-identical +
  standalone commit + pre-push diff-check vs PR #1295's branch) → §6
  Slice 0 + §11 ruling (b) + §8 R1 fallback clause superseded-as-history.
- **Twin-baseline capture waits for product-audit-1's identity PR-1;
  Slice 0 may precede it** → §6 Slice 0 + §7.2 sequencing note.
- **Round-7 severity gate:** 13 ≤MINOR items carried fix-forward (ruling
  2026-07-09) — all 13 resolved above; clean-streak accounting stands at
  1 with this fold round substituting for the round-7 fix pass.

Verification round remains OPEN: this lock is pending one verification
pass re-checking the folded text against the sources; no implementation
begins before that pass and the pending rulings where their gates apply —
gap-#18 (a) is since **CLOSED** (coordinator ruling 2026-07-08,
re-confirmed 2026-07-09; §11 ruling (a) verbatim block), so **F7/F8 remain
the only pending rulings**.

---

## Final-lock fix round — resolution log (2026-07-09, post-fold verification pass)

The verification pass re-checking the folded text against the sources
produced 12 confirmed findings (2 MAJOR · 5 MINOR · 5 NIT), all resolved
below. Per the round discipline, the two MAJORs reset the clean-streak
accounting to 0 — one further verification pass over THIS fix round is
required before implementation. Register (FL-1..FL-12):

1. **FL-1 (NIT — §0 cross-reference mislabel):** the §0 NON-KEPT scope
   correction cited "fix-forward item 12" but is item 8 in both the source
   register and this doc's own FF register (item 12 = the big_endian_host
   probe seam, correctly cited in §1.3). → §0 in-text number corrected to
   item 8; one token.
2. **FL-2 (NIT — corpora "referenced by letter" over-claim):** external-fold
   log item 13 claimed all nine frozen corpora (a)–(i) are letter-referenced
   by goldens; only (b)/(e)/(f)/(h) are. → item 13 amended in place: (a)/
   (c)/(g)/(i) folded BY CONTENT (named pins), and corpus **(d) float-text**
   is explicitly scoped OUT-OF-SURFACE for this design (every run-identity
   serialization stays Python-side, §1.2 identity-blind kernel; the corpus
   RE-ARMS iff a future slice serializes floats in Rust).
3. **FL-3 (MAJOR — S2 gate undefined + belt-contradictory + non-recomposing
   anchor):** the imported S2 ship gate had no fixture definition, added a
   "per-tuple" the verifier source never stated, was unsatisfiable taken
   literally against this doc's own PERMANENT-belt accounting (~100–200
   s/tuple at top=1000 vs a ~14 ms kernel sum), and its ~14.0–14.5 ms
   anchor neither recomposes from the §9.1 two-term law at FIX-L density
   (~10.5–11.7 ms @604k) nor appears in RECONCILIATION.md. → §9.1
   acceptance-criteria block REWRITTEN: S2 DEFINED (frozen dense 604,485-bar
   fixture, params pinned by the on-branch Slice-1 measurement); BINDING
   form = the RATIO vs the SAME-fixture measured kernel sum; SCOPE =
   kernel+seam only, burn-in CLOSED, belt/spot-check EXCLUDED and timed
   separately (the gate proves the port strands no residue — it is NOT a
   net-win gate; residual (vii) stands); the ms anchor labelled
   COUNTERPARTY-RELAYED (1.15× of the guarantee session's ~12.2–12.6 ms
   kernel sum at ~1,423 trades/mask) and illustrative only. SPEC §5 + §6
   item 14 mirrored.
4. **FL-4 (MAJOR — withdrawn 5.9 d anchor re-imported as BINDING):** the
   §9.1 campaign clause "k≤6 ≈5.9 d today → ~2–3.5 d port-only" (echoed in
   the §9 grid row) re-imported the exact 0.58 ms×enumerated÷workers
   arithmetic the same grid row withdraws as unit-conflated; no independent
   derivation exists (RECONCILIATION.md derives ratios only). → the
   day-count pair WITHDRAWN at §9.1, the §9 grid row, and SPEC §5; the
   BINDING campaign band is the RATIO band only (≈1.3–3.1×; ≈2.3–2.5× at
   median density); absolute k≤6 day figures DEFERRED to the required
   on-branch sub-grid measurement + combo_counts.md §3 rebuild; §10's
   "day-level not reachable" claim re-scoped to "not DERIVABLE from the
   ratio band alone".
5. **FL-5 (MINOR — §0 still led with the withdrawn ~3–10 µs band):** §0's
   verdict-pass sentence contradicted SPEC §1's withdrawal and §9 row 1's
   deletion, and its own next clause (intercept ≈10 µs). → §0 rewritten at
   the composed ≈11–13 µs residue (~6–8% of the 48k walk+scan floor).
6. **FL-6 (MINOR — blended-with-Slice-3 cap contradicted the same table's
   Slice-3 endpoint):** the "~1.6–1.7×" blended cap was computed at the
   SLICE-2 PK_trade basis; at the table's own Slice-3 endpoint (~168 µs)
   the blend reaches ≈1.8× — numerically the very figure the cell forbade
   relaying. → blended row recomposed ~1.6–1.8× with the derivation shown
   (0.66×67 + 0.34×(168–198) ≈ 101–112 µs); the never-relay decree
   re-scoped to the round-6 combine-≈3 µs DERIVATION of "≈1.8×", not the
   recomposed band; SPEC condition + amendment sites updated.
7. **FL-7 (MINOR — FF-4 mask-count floor not propagated):** §7.4's enum
   width duals still mandated the superseded 4-range window + ranges-only
   seam signature; §8's secondary-risk list still said "bounded to 4
   ranges/process"; §5 residual (ix) still said "4-range/2^16-mask". → all
   three sites re-keyed to the 2^16-mask floor / 64-range cap + the
   superseding `reset_enum_self_check_for_tests(width=…, masks_remaining=…,
   ranges_cap_remaining=…)` signature and the restated masks-unit cost.
8. **FL-8 (NIT — band-recomposition slop):** SPEC §5's "removes ~205–215 µs
   of ~229.5 µs" did not recompose from its own terms, and the "~10–13 µs"
   band's lower endpoint dropped the mandatory ~1–3 µs seam term. → the
   residue band is **≈11–13 µs** (10+1=11 composed floor) at every live
   site (§0, §9 row 1, SPEC §1/§3/§5); the removal band recomposed to
   **~212–217 µs** (229.5 − (11–13) − (2–4) = 212.5–216.5); §9 row-1
   endpoints re-summed (~193–198 µs Slice 2 / ~168–198 µs Slice 3 — ratio
   bands unchanged).
9. **FL-9 (NIT — §8 R1 not reconciled with the split-bundle outcome):** R1
   still presented the pre-grant refusal fallback as live and implied the
   Q7 ruling was recorded with the Slice-0 grant. → R1 cell now carries the
   split explicitly: Slice-0 zone GRANTED 2026-07-09 (§11 ruling (b)); the
   Q7 rust/-zone ruling (gap #18) STILL PENDING [since CLOSED — coordinator
   ruling 2026-07-08, re-confirmed 2026-07-09; §11 ruling (a) verbatim
   block; the R1 cell and both lock-round closing lines updated by the
   checkpoint-1-pack-v2 fold]; the refusal-fallback
   clause marked [HISTORY — superseded by the grant].
10. **FL-10 (MINOR — 7 file:line cites drifted at origin/main e8c708bd; the
    docs were authored at 665181f3, 13 commits behind):** re-anchored —
    sweep_parallel.py :558→:842 (RAYON pin), :595→:891 (per-tuple except),
    :310→:358 (DEFAULT_STALL_SECONDS); cli.py :313/:313-318→:385/:385-392
    (--top production sizing); ranking/__init__.py :462→:555 (skew/kurt
    consumption); sweep_supervisor.py :700→:710 (positional prefix-skip);
    results_store.py :426→:478 (same-run_id conflict raise). All referenced
    code verified intact and semantically unchanged; the pinned
    perf_reconcile artifacts stay byte-frozen (their :558 cite is true at
    their own capture base). STATUS NOTE folded into §11 F7: **#1291
    (774d9d16) is MERGED on origin/main** — the symbol-PRESENT stall-guard
    arm is now the natural production arm (the absent-symbol arm is the
    dormant pre-#1291/revert branch; the design is total either way);
    **#1295 remains UNMERGED** (`BRUTEX_SWEEP_STALL_SECONDS` resolves only
    at sweep_parallel.py:364), so F7 stays genuinely pending.
11. **FL-11 (MINOR — "R² ≥ 0.993" overstated the production-arm fit):**
    fit_results_pinned.json pins prod_wrapper (the ≈4.1 µs/trade fit) at
    R² = 0.99267 — strictly below 0.993; only count (0.99935) and raw
    (0.99901) satisfy ≥. → §9.1 states per-tier R² values with the
    production arm's residuals (rms 7.8%, max 20.8%) — the blanket "≥"
    clause deleted.
12. **FL-12 (NIT — "MEASURED ~3.0–3.97×" mislabelled the counterparty
    endpoint):** own artifacts measured 3.77/3.69/3.51/3.00×; the ~3.97×
    is counterparty-relayed ("within shared-vCPU noise"). → §9.1 + §10
    restated as ≈3.0–3.77× MEASURED here with the ~3.97× endpoint
    explicitly COUNTERPARTY-RELAYED, consistent with the sibling
    ≈3.0–3.8× bullet.

Historical register entries above (external-fold items 6/14, round-7 item
8, FF-4/FF-12 wordings, CR bundling note) are RETAINED VERBATIM as history
where they carry the pre-final-lock figures ("≈10–13 µs", "~205–215 µs",
"R²≥0.993", "3.0–3.97×", the 5.9 d pair, "4 ranges", the bundled-sign-off
wording); this register supersedes them wherever they disagree.

Verification round REMAINS OPEN (clean streak reset to 0 by FL-3/FL-4):
one further pass re-checks THIS fix round against the sources; no
implementation begins before that pass and the pending rulings where
their gates apply — gap-#18 (a) is since **CLOSED** (coordinator ruling
2026-07-08, re-confirmed 2026-07-09; §11 ruling (a) verbatim block), so
**F7/F8 remain the only pending rulings**.

## Pre-commit polish — resolution log (2026-07-09, pre-audit pass)

A final polish pass re-verified all 12 final-lock resolutions (FL-1..FL-12)
against the sources and current origin/main (95955a71 — one dashboard-CSS
commit past FL-10's e8c708bd base; NO cited file touched, all seven
re-anchored cites plus a 16-cite spot-check re-verified line-exact there;
#1291 (774d9d16) confirmed merged, #1295 confirmed absent). Ten of twelve
resolutions verified clean, numbers recomposing (blend 0.66×67+0.34×(168–198)
≈ 101–112 µs ⇒ ≈1.6–1.8×; row endpoints 193–198/168; 229.5−(11–13)−(2–4) =
212.5–216.5; per-arm R² 0.99267/0.99935/0.99901 and rayon 3.77/3.69/3.51/3.00
byte-checked against fit_results_pinned.json). Corrections applied by this
pass (each the smallest sufficient edit):

1. **FL-7 stragglers (three live "4-range" mentions):** §1.3's belt comment
   ("twin-runs, the 4-range enum burn-in"), §5's width-keying rationale
   ("the second 4-range window"), and §5's kept-set-divergence residual
   ("past the 4-range enum burn-in") still named the enum shadow by its
   superseded shape → re-keyed to the mask-count-floored enum shadow
   (the FF-4 floor; historical rationale describing the OLD window left
   verbatim).
2. **FL-5 straggler (§14 decision log):** the grounding-contradiction
   resolution cell still priced the deliberate ResultMetrics build at the
   withdrawn "~3–10 µs / ~5–7% (round-1 correction)" → restated at the
   final-lock ≈11–13 µs / ~6–8% walk+scan-floor basis.
3. **FL-3 anchor arithmetic (§9.1 S2):** the non-recomposition parenthetical
   "~10.5–11.7 ms @604k" did not itself recompose from the two-term law →
   replaced with the recomputable ≈9.5–10.0 ms per-mask law cost at
   1,423–1,532 trades/mask (walk 6.15–6.20 ns × 604,485 + 4.106 µs/trade
   + c0) ⇒ ≈10.9–11.4 ms at the gate's 1.15× ratio, with the
   cross-container offset cite (RECONCILIATION.md §5). The FL-3 register
   entry's "~10.5–11.7" wording is retained as history; this note
   supersedes it.
4. **§9.1 qualifier propagation (SPEC §5, two sites):** the "post-conversion
   KERNEL-BOUND" composition (≈1.3 ns/bar·mask walk) and the 1-minute
   walk-bound claim (~0.7 ms sandbox) quoted the UNPINNED free-rayon
   count-only rate without the §9.1-mandated qualifier → both now carry
   the unpinned qualifier + the pinned-law twin figure (≈6.2 ns/mask·bar ⇒
   ~3.4 ms @555k + ≈4.1 µs/trade), matching DESIGN §9's 1m caveat.
5. **FF-4 seam propagation (SPEC §3):** the external-fold amendment clause
   now states the superseding `reset_enum_self_check_for_tests(width=…,
   masks_remaining=…, ranges_cap_remaining=…)` signature in-SPEC (the
   round-5 "4 shadow ranges"/width-only wording earlier in the same
   condition is layered history it explicitly supersedes).

Known imperfections deliberately left: the prior-round resolution logs and
the §6/§3 round-tagged amendment layers retain superseded figures verbatim
(the doc's standing layered-history convention — each later block states
what it supersedes); SPEC §6 items 12/13 likewise describe round-5/6 gate
additions in their original wording, superseded by item 14. FL-10's log
states its verification base as e8c708bd — true when written; this pass
re-verified at 95955a71. No live (non-history) site quotes a withdrawn
figure after this pass.

## Slice-1 substrate + ordering pins (lever-discovery fold, 2026-07-09)

Source: **lever-discovery session, refuter-gated report + live repo
verification @ 95955a71**. Four pins folded into the locked design.
None of them changes the locked FFI surface, the emission-order/counter
contracts, or the §9/§9.1 binding perf law — items 1/2 bind the Slice-1
implementation, item 3 is a post-proof optimization commit, item 4
restates the streaming residual hazards as pins. Cross-referenced from
the §6 Slice-1 bullet and §5's enumeration burn-in bullet.

### 1. EMISSION-ORDER PIN (CRITICAL — prevents a real Slice-1 bug)

The engine's `iter_masks_k_of_bits` emits **POSITION-LEX** order
(`combo_mask.py:648-649` — "lexicographic on the sorted positions"; impl
`:689-693` via `itertools.combinations`), **NOT ascending-numeric**.
Ascending-numeric == COLEX == the STANDARD combinatorial-number-system
order — so a naive/standard CNS unranking would enumerate in a DIFFERENT
order than the engine stream and silently break emission-order identity
PLUS the F1 positional-checkpoint pin (§3's enumeration-order/checkpoint
identity pin). **MANDATE:** `enumerate_prefilter` uses the **LEX-RANK
unranking variant** — rank within `combinations(sorted_bits, k)` lex
order; unranking O(k) with an exact binomial table (`math.comb` /
`count_combos`, `combo_mask.py:533-580`). Counterexample to memorialize:
n=4, k=2 → itertools emits masks `[3, 5, 9, 6, 10, 12]` vs
ascending-numeric `[3, 5, 6, 9, 10, 12]`. **GOLDEN mandated:** first +
last 10k masks + blake3 of the FULL emission stream vs the Python
oracle, plus a set+order survivor golden vs the flat path.

Why order is load-bearing (**corrected grounds** — NOT fsum, which is
order-independent per `trial_stats.py:41-42,:237`): survivor stream
order → chunked-sim → ResultRow append order → store bytes →
`store_content_hash`/sentinel byte-identity; the first-16-mask burn-in
SELECTION + `_reattribute_chunk_failure` are stream-order-dependent; and
`SurvivorColumns` retains kept order (`sweep_stream.py:89`). Across k:
NO interleaving — k is a grid axis, one k per `SweepTuple`
(`sweep_orchestrator.py:604-613`); k-major with per-k boundaries is the
only defensible stream shape.

### 2. SUBSTRATE CHOICE (absorbed now, ~zero cost)

The kernel's INTERNAL layout is **CONDITION-MAJOR TRANSPOSED BITPLANES**
(one per-condition bitmap over bars) — chosen at Slice-1 implementation
time so the later prefix-AND DFS pruning lever (item 3) retrofits
freely, with no substrate migration. The locked FFI surface, the
emission order (item 1), and the counters are UNCHANGED by this internal
choice — it is invisible outside the kernel.

### 3. PREFIX-AND LEX-DFS PRUNING (post-proof optimization commit)

NOT part of the initial bit-identity proof; a SEPARATE post-proof
optimization commit that slides past acceptance if time requires — zero
stranded work given item 2's substrate. Mechanism: a DFS extending each
prefix only with strictly-greater bits, candidates ascending, is **by
construction** the pre-order traversal whose depth-k leaves are
itertools-lex — prefix-AND state carried on the stack cannot alter order
(analytically argued, NOT machine-verified — item 1's golden is the
proof obligation). **HARD CONSTRAINT: NO prefix memo tables**
(refuter-measured 384–452 GB blowup) — stack-carried state only.
Range-partitioned mid-DFS starts are compatible with THREE subtleties,
each needing its OWN test:

- **(a) BOUNDARY TRUNCATION** — a subtree straddling the range's `hi`
  must stop at `hi` (compare the prefix's leaf-rank interval vs
  `[lo, hi)` before descending);
- **(b) ANCESTOR-PRUNE RECHECK AT SEEK** — re-apply conflict +
  anti-monotone prune tests to ALL ancestors of the unranked start,
  else the range emits/counts masks the unpartitioned DFS would have
  pruned shallower;
- **(c) PER-RANGE PRUNED-MASK ACCOUNTING** — per-range
  `enumerated = hi − lo`; per-range `conflict_pruned` =
  inclusion-exclusion over the CONFLICT_PAIRS (**26 pairs** —
  repo-verified count; treat any "27" in older quotes as a journal
  error) restricted to the rank interval; per-subtree `minhits_dropped`
  = subtree_leaf_count − conflicted_leaf_count. **Item (c)'s closed-form
  construction is UNVETTED DESIGN INPUT** (first-principles from the
  discovery session, not refuter-vetted) — it must pass its OWN hostile
  round before the pruning commit ships; the acceptance bar is counter
  IDENTITY vs the sidecar (`trial_stats`) + the audit payload
  (`sweep_run.py:1439/:1441/:1474/:1593-1600` counters).

### 4. STREAMING RESIDUAL PINS (the four hazards that survive into the Rust core)

From the OOM analysis: today's Python engine accumulates four
O(C(n,k)) terms per worker (~3.5–5 GB × 8 workers ≈ 28–40 GB at k=6,
CPython object overhead ~40 B/mask; `sweep_run.py:1434/:1453/
:1468-1474/:1485`, `sweep_kernel.py:145-166` one-shot marshal) — ALL
absent in this design **by construction, PROVIDED:**

- **(a)** survivors NEVER return as Python `list[int]` — a ~50M-mask
  list re-inflates ~1.9 GB/worker; compact columnar buffers only,
  per-call-capped (already the design — PINNED here);
- **(b)** Stage-1B counts are BLOCK-SCOPED — never batch all counts
  after streaming enumeration, or hits retention re-enters;
- **(c)** streaming/fused enumeration is DIGEST-PATH-ONLY
  (`bypass_dedup = trial_stats_digest is not None`,
  `sweep_run.py:1415`) — per-block `mark_many` on the legacy arm breaks
  marks-after-flush (the MIGRATION #650 resume-skip class);
- **(d)** downstream whole-tuple O(kept) accumulations
  (`SurvivorColumns` + `sidecar_sharpes`) are NOT enumeration
  structures — they stay bounded by the owned-bytes watermark
  (`plan_rank_memory` ⇒ k=6 always takes the bounded columnar rank arm,
  `sweep_mem_budget.py:84/:312`). This boundary is stated honestly: the
  design deletes the ENUMERATION-scaled terms, not the kept-scaled
  ones.

---

## Verifier B checkpoint-1 audit fold — resolution log (2026-07-09, pack v5)

The mandatory items of Verifier B's checkpoint-1 audit, folded item by
item; every repo claim written here was re-verified against the real
tree (file:line cited at each edit site). Items C1/C2/C4 additionally
fold the coordinator's two addenda of 2026-07-09 (the launch-lane scope
sync + Verifier B's verbatim transmitted texts) and the F6 register
update (PR #1336).

| Item | Severity | Resolution (where) |
|---|---|---|
| C23 | MANDATORY, Slice-0 | §6 Slice 0 gains the ORDER-SENSITIVITY DIFFERENTIAL GATE: hypothesis `combine(fold(trades)) ≡ _compute_metrics_impl(trades)` (compute.py:569; Welford/Pébay per compute.py:32-33/:121-122) at struct.pack byte equality, over adversarial orderings + splits at every boundary + single-element folds + negative/zero/denormal classes; lands BEFORE the Slice-0 PR opens; joined to the Slice-0 gates list. Mirrored to the running Slice-0 workflow's scratchpad copy immediately after the edit. |
| C3 (+C9) | MAJOR | §12 MASK-SERIALIZER LAW re-scoped (no NEW bare-int surface; shipping bytes FROZEN incl. the bare-int `combo_digest`/WF sites — sweep_cache.py:389-391, sweep_driver.py:977+; the self-contradiction with corpus (e) removed) + the §7.1 corpus-(e) golden re-specified implementably ("the exact shipping digest bytes reproduce", not hex-everywhere). |
| C5 | MAJOR | §3 kill−9 bullet corrected: `load_or_create_store` PRE-LOADS (sweep_orchestrator.py:737-758), so a divergent-build redo hits the LOUD same-run_id append-conflict abort (results_store.py:487-490 — the F2 law, the DESIGNED behavior); the false "starts empty / cannot trip" claim removed. |
| C6 | MAJOR | §3 kernel-panic bullet re-routed off "#1295's crash-loop breaker" (the G12 checkpoint-CORRUPTION counter [independently verified TRUE by Verifier B at 871b3129 — sweep_quarantine.py:84/:113] — blind to panics; #1295 UNMERGED) onto the mechanisms that ACTUALLY fire: parallel-lane `BrokenProcessPool` quarantine + `SweepComboError`→TUPLE_FAILED (sweep_parallel.py:891-897/:1340-1343/:1585-1596), daemon-lane supervisor restart-cap → GAVE_UP + CRITICAL alert (orchestrator/supervisor.py:295/:530-575, telegram.py:161); remediation lever #1 re-homed to those runbooks; the (c) supervised-daemon wording updated. |
| C1 | MAJOR | §12.1: WHEEL-PROVENANCE law (wheel sha256 + build flags + `__source_digest__` recorded; import-fallback = refusal), DIRTY-TREE law (porcelain-empty precondition; ordinary runs honestly unchanged), OUTPUT-ROOT WIPE instruction (proving period), DEC-ID DEFERRAL named deliberate; the §B.4 question pasted VERBATIM + answered — with the transmitted answer-(a) premise CORRECTED against the repo (`_indicator_version()` = brutex+trading_core ONLY, cli.py:6159-6166; fno-math is NOT in the string — the forward rule mandates joining a version signal only with a future sanctioned byte-moving change, under its own DEC). |
| C2 | MAJOR | §12.2 ENV-FLAG census: the six NEW conversion flags/knobs (two arm flags + belt + three numeric knobs) each classed with junk policy + eager validation point; Verifier B's existing-flag defect census adopted + repo-verified (BAD_TICK_SCREEN silent-ON bad_tick.py:291-309; LOOKAHEAD_GUARD silent-OFF sweep_lookahead.py:146-157; LAKE_HEAL silent-auto lake_heal.py:177-198; MEM_BUDGET/BOUNDED_RANK mid-run sweep_run.py:1349-1350; MAX_BLOOM_BYTES import-raw bloom.py:243; PER_TUPLE_SECONDS the exemplar cli.py:6564-6570; RUST_METRICS warn-once disclosed); pre-existing defects NAMED as out-of-scope observations for the coordinator. |
| C42 | MINOR→FAIL 7 | NEW §3.1 AUDIT section: buffer-and-replay (BufferedAuditCollector audit.py:683; parent replays in enumeration order, sweep_parallel.py:1578-1580), core returns counters/events only — Python owns the JSONL chain at the 3-lines/tuple cadence; allow_nan=False law (audit.py:176-203); SHA-256 chain (audit.py:19-22/:270-283) vs blake3 run-identity — both algorithms + separate purposes stated. |
| C32 | MINOR | §5 burn-in gains the THREE-WAY DISAMBIGUATION PRECEDENCE: (1) oracle-vs-oracle = harness/environment defect → burn-in abort, LOUD `burnin_oracle_disagreement` tuple failure, NO autorevert; (2) kernel-vs-both = kernel defect → autorevert latch + oracle ships; (3) partial/mixed = (1) conservative; supersedes the prior ship-OFF-arm+latch outcome; one golden per class pinned. |
| C51 | MINOR | Enum-shadow ranges cap re-bounded 64 → 256 with honest arithmetic (default caps yield ≈444–483 masks/call @555k–604k bars ⇒ 64 ranges max ≈28.4k–30.9k < the 2^16 floor — structurally unreachable on the very shapes it was derived for; 256 ≥ the needed 148 with ~1.7× headroom); floor + accumulate-across-calls law unchanged; `enum_shadow_floor_unmet` becomes a genuine degenerate-stream signal. Downstream cap mentions updated (§7.4 width dual, cost line, §5 negative pin). |
| C31 | MINOR (SPEC) | SPEC §4 rows 2/9/13 rewritten to the locked architecture (columnar `fold_block`, trades never cross, `enumerate_prefilter`) with since-corrected notes. |
| C48 | MINOR (SPEC) | SPEC §5's quoted single-`f` blended formula gets the WITHDRAWN banner treatment, pointing at DESIGN §9 (round-5 SPLIT form) + §9.1 (binding two-term law). |
| C4 | MAJOR partial → completed (SPEC) | SPEC gains the LAUNCH-SURFACE section: SLA-guard recalibration obligation (launch-lane scope-sync wording — the ONE common estimate model), the dashboard dropped-return keep-or-fix named OPEN → coordinator, the sweep-daemon lane named, F6/F24/F25/F28 pasted VERBATIM (attributed; F6 carries the PR #1336 register update + cite correction), and the NEW machine-readable per-arm throughput-calibration deliverable (named DISTINCT from the stall-detection floor). |

