# SPEC_RUST_SWEEP_CORE.md — Rust sweep-core conversion (grounding-phase spec)

Status: **LOCKED FOR CHECKPOINT 1 pending verification round.** Date
2026-07-09 (external fold + lock round — Verifier B F1–F5/F15/F18/F21–F23/
F27 + baseline-truth pack + BINDING perf reconciliation, the 13-item
fix-forward register, and the coordinator rulings folded; see
DESIGN_FINAL_RUST_SWEEP_CORE.md `## External fold + lock round` for the
full resolution log). Previous: DRAFT for 4-lens hostile design review,
2026-07-08.
Companion docs: `baseline.md` (measured grounding), `DESIGN_RUST_SWEEP_CORE.md`
(architecture), `combo_counts.md` (grid math + wall-clock table),
`open_questions.md`.

## 1. Goal

Move the **per-combo orchestration** of the brute-force sweep — mask iteration,
conflict pruning, Stage-1B prefilter dispatch, trade-sim dispatch, per-mask
metrics computation, and survivor-column accumulation — from the Python loop in
`brutex/engine/sweep_run.py::_iter_mask_metrics` / `run_persisted_sweep` (and its
per-mask helpers in `sweep_driver.py` / `rust_trade_sim.py` / `metrics/compute.py`)
**inside one resident Rust core** in the `fno-math` crate, so that Python
launches a TUPLE and collects a bounded, columnar result — instead of crossing
the FFI boundary and churning Python objects **per mask and per trade**.

Measured basis (`baseline.md`): the Python + FFI residual is ≈ 230 µs of a
≈ 400 µs per-kept-mask marginal at a 48k-bar sandbox window (57%), rising toward
the audit's ~0.58 ms/combo ≈ 85% at production windows [AUDIT-RELAYED — original
audit files unrecoverable; independently re-derived within the same order].
Target: reduce the per-KEPT-MASK **orchestration residual** from ~0.58 ms to
the **~11–13 µs band** (round-7/fix-forward-item-6 reconcile + final-lock
recomposition — the ≈10 µs
measured O(1) intercept of the RETAINED Python combine + 37-field
`ResultMetrics` construction + append (`baseline.md`'s two-point fit of
`compute_metrics_from_scalars`) PLUS the ~1–3 µs relocated per-mask seam
cost (decode-row access, zip(strict), flag branch, kept-PyLong churn) —
composing exactly to 11–13 µs; the
earlier "~1–5 µs" and "~3–10 µs" bands sat below the design's own honest
floor and are withdrawn, and the interim "~10–13 µs" quote dropped the
mandatory seam term at its lower endpoint (final-lock: 10+1=11 is the
composed floor); the band is re-MEASURED on-branch before any
tighter claim ships), leaving per-combo cost kernel-bound (walk + metrics
folds; the BINDING per-mask kernel law is DESIGN §9.1's two-term pinned
form — walk ≈6.2 ns/mask·bar @`RAYON_NUM_THREADS=1` + ≈4.1 µs/trade). (Round-2 UNIT LAW, §5: 0.58 ms is a per-KEPT quantity — never a
per-enumerated one.)
Headline framing per Golden Rule #10: **per-operation O(1), total O(N_masks ×
N_bars) — Rust changes the CONSTANT; claiming O(1)-total is forbidden.**

## 2. In scope / out of scope

IN (the surgery site — anchors per the audit):
- `brutex/engine/sweep_run.py` — the enumerated-mask loop (`sweep_run.py:1438`),
  `_iter_mask_metrics` (`:1021`), the Stage-1B prefilter call, the batch/bounded
  accumulation arms.
- `brutex/engine/rust_trade_sim.py` — the per-chunk FFI dispatch + `ClosedTrade`
  materialisation (bypassed on the new path; retained verbatim as oracle/fallback).
- `brutex/engine/sweep_driver.py::metrics_from_trades` tail +
  `brutex/metrics/compute.py::_compute_metrics_impl` — **the FOLD layer only**
  (integer trade aggregates, Welford/Pébay moments, Neumaier downside Σr²,
  percentile sort+lerp, streaks, the drawdown/ulcer scan) is re-expressed in
  Rust bit-identically for the in-core path; **the VERDICT layer — every
  `_safe_ratio` None, libm `pow`, sqrt-derived verdict scalar, floor-div,
  gate boundary, and the `ResultMetrics` construction — stays in today's
  literal Python (`combine_metrics_from_folds`)**, per the DESIGN §0/§4
  layer rule (round-3 reconcile: the earlier "re-expressed in Rust
  bit-identically" wording over the whole of `_compute_metrics_impl`
  contradicted that rule and is corrected). Python remains the oracle.
  **Coordinator ruling 2026-07-09: the Slice-0 compute.py zone is GRANTED**
  (conditions: behavior-identical + standalone commit + pre-push
  diff-check vs PR #1295's branch; twin-baseline capture waits for
  product-audit-1's identity PR-1 — Slice 0 may precede it; DESIGN §6/§11).
- `rust/fno-math` — NEW `sweep_core.rs` module (consumes the existing
  `trade_sim.rs` `MaskWord`/`simulate_one_core` and `metrics.rs`
  `drawdown_stats_checked` internals; see DESIGN §2).
- `brutex/engine/sweep_stream.py` — EXACTLY ONE additive, bit-pinned
  Slice-2 edit (round-5): `BoundedFinalist` gains `position: int` (the
  finalist's SurvivorColumns kept-order index, in hand at construction
  and previously discarded — `sweep_stream.py:385-401`), + a tiny
  additive read-only per-position column accessor iff a retained column
  lacks one; ranking order/scores/artifact bytes unchanged
  (byte-neutrality pinned). Required by the DESIGN §5 bounded-arm serve
  reconciliation to be genuinely O(top_k) — without it the
  reconciliation is O(kept) or a ~GB off-watermark mask→index dict.
  Nothing else in the file moves.
- Dedup-probe / checkpoint / audit batching AT the FFI boundary: **preserve** the
  existing per-tuple batching (the production digest path has NO per-mask dedup
  probe, NO per-mask audit line, NO per-mask checkpoint — `baseline.md` §4);
  the Rust core must not introduce any per-mask Python callback.

OUT (non-goals for this PR):
- `rust/trading-core` — **untouched** (ship condition 3; DEC-SHARED-CORE-001).
- Stage-1A `build_bar_bits` (per-tuple, kernel-cached — not the per-combo cost).
- Ranking `rank_survivor_columns` / `rank_combos` internals — stay Python
  phase-1 (bit-pinned shared internals; O(kept)/tuple ≈ 1.3 µs/mask amortised).
- Finalist projection `ranked_combo_to_result_row` (per-top-K Python oracle
  re-simulation — an explicit #1182 non-goal, unchanged).
- Store flush / sidecar / sentinel / audit writers; the #1295 run-core
  lifecycle; the #1291 stall guard (compatibility described in DESIGN §8).
- Stage-3 CMA-ES, walk-forward, validation, cascade — they consume the COMMON
  `metrics_from_trades`/`metrics_for_mask` seams, which remain byte-identical.
- The Groww pull/fetch path (operator exception — see ROADMAP §9).
- The legacy no-digest dedup path (Bloom+LMDB probes): the Rust core serves the
  DIGEST path only; a legacy caller keeps today's Python loop unchanged.

## 3. The five hard ship conditions, restated against THIS codebase

1. **Bit-identical golden proof.** Same inputs → byte-identical run_ids,
   metrics, rankings, and stored bytes. Concretely, ALL of the following stay
   frozen and must pass UNMODIFIED:
   - `tests/golden/test_engine_sweep_run_golden.py`:
     `_GOLDEN_RUN_ID = "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"`,
     `_GOLDEN_CONTENT_HASH = "2387cc884944768ee271fad117edb953f538d4d6d2d3b3c1c4b9ca3f4bd4d147"`
     — reproduced on ALL flag arms. **Round-3 correction — the engagement
     fixture is a NEW DIGEST-PATH GOLDEN VARIANT, not this fixture:** the
     existing fixture's `_run` passes NO `trial_stats_digest` (legacy dedup
     path, `bypass_dedup=False`) and `_config` leaves `top_k=None` — the
     DESIGN §5 engagement condition REFUSES the core on BOTH grounds
     (`legacy_dedup_path`, `unbounded_top_k`), so an engaged-event
     assertion against it is unimplementable. Therefore: (a) a digest-path
     golden VARIANT (synthetic `trial_stats_digest`, finite `top_k` ≥ the
     ranked count so the finalist set is the full ranked set) lands
     OFF-arm-FIRST — its `_GOLDEN_RUN_ID_DIGEST`/`_GOLDEN_CONTENT_HASH_DIGEST`
     literals are minted by the SHIPPING spine before any NEW-core code
     exists (round-5 provenance honesty: under the ADR-0017 default the
     OFF arm's drawdown routes through `fno_math.sweep_drawdown_stats` —
     #1531-proven bit-identical, so the bytes are what matters — the
     literals are NEW-CORE-free, not "pure-Python"), then frozen; every engaged-event / poison / belt-restart /
     misflag golden runs against the VARIANT with the ON arm ASSERTING the
     `engaged` event (the #1186/#1532 anti-vacuous-gate pattern); (b) the
     ORIGINAL fixture + literals stay frozen and become the LEGACY-REFUSAL
     negative golden — both flags ON must reproduce the original literals
     with ZERO engaged events + the `legacy_dedup_path` fallback reason.
     **Negative event pins (round-3):** every arm-combination golden
     asserts the ABSENT arm's engaged-event count == 0 (enum-only ⇒ zero
     `sweep_rust_core.engaged`; core-only ⇒ zero `sweep_rust_enum.engaged`;
     OFF ⇒ zero of both) plus the DISABLED-ARM POISON goldens (a poisoned
     kernel behind an OFF flag still reproduces the pinned bytes — proves
     each flag genuinely prevents consumption; DESIGN §5/§7.1).
   - The frozen 400-case `tests/golden/fixtures/vocab_golden_vectors.json`
     answer-key — byte-untouched (`git diff -- tests/golden/fixtures/` EMPTY).
   - `tests/property/test_sweep_rust_metrics_equivalence.py` (cross-arm FULL
     `ResultMetrics` bit-equality via `struct.pack`) extended to the new core.
   - **Twin-run byte-identity (the audit's W11 pattern).** HONEST NOTE: the
     audit's W11 artifact is unrecoverable in this container (see `baseline.md`
     §0); the pattern as relayed + as already practiced in-repo (the #1182
     "wired-vs-unwired store-hash golden" and #1186/#1531 dual-arm goldens) is:
     run the SAME tuple/grid TWICE **in one build at one commit** — kill switch
     OFF (today's Python spine) vs ON (Rust core) — into two separate results
     roots, and assert `FlushResult.content_hash`, the Parquet bytes, the
     trial-stats sidecar, and the ranked order are byte-equal. A grid-scale
     twin run (small real-data grid, both arms) is part of the ship gate.
     **Execution venue + evidence (round-6 — per-PR CI has no lake data):**
     the golden-fixture twin is the CI-lane arm; the real-data grid twin
     AND the `sweep_parallel` workers>1 twin are REQUIRED ON-BRANCH gates
     per slice (Slice 1: OFF vs ENUM; Slice 2: all four arm combinations),
     one command each on the lake-bearing box, with the exact commands and
     both arms' `FlushResult.content_hash` + store-Parquet sha256 + sidecar
     hashes recorded VERBATIM in the PR body — merge blocked until the PR
     body carries that evidence (the DESIGN §7.10 differential-harness
     recording standard applied to the ship condition).
   - **Run-identity / code_commit_hash register (Verifier B pre-empt).** The
     §2-Rule-4 formula includes `code_commit_hash`, so ACROSS two commits
     run_ids move BY DEFINITION — no cross-commit byte-identity is claimed,
     ever. Precisely:
     * `code_commit_sha` is an **injected input** (`SweepRunConfig.code_commit_sha`
       → `mask_run_id` → `combo_hash`), resolved once at CLI launch. The
       twin-run proof holds it CONSTANT by construction (both arms in one
       process/build at one commit) — "byte-identical run_ids" is computed over
       the identical 9-field input vector including the SAME commit sha.
     * The frozen goldens handle the same problem by pinning SYNTHETIC identity
       fields (`code_commit_sha="deadbeef"`, `data_snapshot_hash="snap0001"`,
       `indicator_version="ind-v1"` — `test_engine_sweep_run_golden.py:104`),
       so `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` are commit-INDEPENDENT
       literals; that is exactly how #1531/#1532 kept them "UNCHANGED and
       reproduced" across real commits.
     * Resume/dedup keys: the per-tuple completion sentinel + store path key on
       `_tuple_digest` (`sweep_orchestrator.py:664`), whose descriptor INCLUDES
       `code_commit_sha` — so a Rust-core run at a NEW commit resolves to NEW
       store paths + sentinels and can never skip-complete (nor be
       skip-completed by) a Python-core run's artifacts; it re-runs the full
       grid, re-keyed, by design. Within ONE commit, the twin runs use separate
       `--results-root`s (else the second arm would legitimately
       sentinel-short-circuit). The legacy LMDB dedup keys are raw
       `mask_run_id` bytes (commit-inclusive) — same re-key property — and are
       BYPASSED on the production digest path anyway.
     * The sweep-cache (`SweepCache`) tuple digests likewise embed the grid
       identity; no cross-engine key collision is possible because the ENGINE
       is deliberately NOT part of any identity (run_id hashes mask/params/
       data/code, never the engine — `sweep_run.py:169` comment; the two
       engines must therefore be bit-identical, which is the whole point).
2. **Full repo gates per CLAUDE.md.** 100% line+branch on every touched Python
   module (whole-engine static gate `pytest --cov=brutex.engine
   tests/unit/test_engine_*.py --cov-fail-under=100`); ruff + ruff-format;
   `mypy --strict brutex`; pyright at the pre-change baseline; `cargo test
   --release`, `cargo clippy --release --all-targets -- -D warnings`,
   `cargo fmt --check`; **mutation gates included** — cargo-mutants on the new
   `sweep_core.rs` rides the #1597 sharded weekly matrix (fno-math shards), and
   the touched Python seams get mutation-killing tests by design (exact
   messages, exact boundaries — the #1182 convention); doctest gate; the O(1)
   bench tiers of `tests/bench/_o1_tiers.py` with honest ceilings.
3. **DEC-SHARED-CORE-001 respected.** `rust/trading-core` is consumed
   bit-identically by TickVault; this work adds a NEW module in `fno-math`
   (BruteX-only crate) and does NOT reshape trading-core; the frozen 400-case
   golden answer-key stays satisfied byte-for-byte. (fno-math is not shared
   with TickVault — verified: TickVault coupling is trading-core only, per the
   CLAUDE.md header.)
4. **Safety pattern retained (ADR-0017 lineage).** Default-OFF flag first
   (`BRUTEX_SWEEP_RUST_CORE`; `_TRUTHY`/`_FALSY` vocabulary imported from
   `sweep_lookahead` — never copied), per-process **burn-in bit-compare** of the
   first N kernel-served masks against the Python oracle arms (the
   mechanised-pure oracle + the shipping OFF-arm `metrics_from_trades` —
   see §4's round-1-corrected burn-in paragraph), **autorevert latch** on
   mismatch (oracle value
   ships; kernel latched off for the process; one ERROR event), explicit
   kill-switch semantics (unrecognised value → FAIL-SAFE OFF with warn-once
   for the two default-OFF kernel flags; for the default-ON finalist-belt
   flag `BRUTEX_SWEEP_RUST_CORE_BELT` the fail direction is INVERTED —
   unrecognised → ON + warn-once, only an explicit `_FALSY` disables; both
   fail toward SAFETY, which is OFF for a kernel and ON for a belt — DESIGN
   §5, round-1 fix),
   and a SEPARATE later default-ON flip ADR (the #1186 → #1532 two-step).
   Round-2 amendments to this condition (DESIGN §5): **burn-in membership
   includes flag-served rows** — a `zero_trade`/`python_required` row
   counts toward the window and is bit-compared served-bytes-vs-both-arms
   (a wrongly-zeroed `zero_trade` flag is otherwise invisible to burn-in
   AND structurally belt-blind: `_empty_metrics` rows fail the MIN_TRADES
   gate and can never be a ranked finalist); **belt/spot-check mismatches
   are DISAMBIGUATED via the shipping OFF-arm before any restart** —
   OFF-arm==core≠oracle is a PRE-EXISTING spine-vs-oracle divergence
   (distinct ERROR, NO restart, NO latch — the served bytes are today's
   bytes), only OFF-arm==oracle≠core (or three-way) latches + restarts;
   **the belt and spot-check are CORE-ARM-ONLY** (skipped on the restarted
   spine pass, fallback tuples, latched processes — so the restart
   terminates structurally, at most once per process); **core engagement
   requires a finite `top_k`** (`top_k=None` ⇒ fallback reason
   `unbounded_top_k` — the default-ON belt must never have an unbounded
   O(population × n_bars) arm); burn-in latch/counters are PROCESS-GLOBAL
   single cells (once per process — the tuple-restart's fresh state is
   tuple-scoped only).
   Round-3 amendments to this condition (DESIGN §5): **burn-in carries a
   NON-TRIVIAL FLOOR** — the window does not close until ≥ 16 of the
   compared rows are PLAIN fold rows (non-flag-served: all 9 float folds
   exercised), hard-capped at 512 total compares with a loud
   `burnin_nontrivial_floor_unmet` warn + a named Slice-4 residual if the
   cap trips first (zero-trade-heavy tuples must not complete burn-in
   having certified zero real fold columns); **the spot-check compares the
   SERVED object on BOTH rank arms** via deterministically pre-drawn
   RETAINED candidates (the bounded rank arm drops non-finalist
   `ResultMetrics` by design — without retention the spot-check has no
   core-side comparand there; DESIGN §5's retained-candidate scheme, O(16)
   memory); **negative engaged-event pins + disabled-arm poison goldens**
   (condition 1) make the four arm combinations genuinely distinguishable.
   Round-4 amendments to this condition (DESIGN §1.3/§3/§5): **the
   finalist belt's COMPARAND PROVENANCE is stated per rank arm** — on the
   batch arm `rc.metrics` IS the served object; on the BOUNDED arm the
   belt's oracle compare proves the REBUILD only (the streamed metrics
   are dropped and `_rebuild_bounded_finalists` re-folds), so the
   mandatory **BOUNDED-ARM SERVE RECONCILIATION** runs in the same
   pre-pass: (1) every rebuilt finalist's SurvivorColumns-retained
   ranking columns (incl. skew/kurt under the overfit gate) bit-compared
   against its STREAM-written SurvivorColumns entry — the finalist's own
   streamed DSR input verified; (2) each rebuilt finalist re-runs
   `passes_gates` (must be True); (3) retained candidates that ARE
   finalists are compared full-column served-vs-rebuilt AND
   served-vs-oracle (no longer excluded) — any divergence is the
   CORE-DEFECT verdict `stream_rebuild_divergence` with the belt's
   disambiguation/latch/restart semantics; **burn-in latch + counters
   are WIDTH-KEYED** (u64 / u128 kernels certify independently — a
   [64,128)-bit tuple engaging the separately-monomorphised u128 kernel
   after a u64-closed window runs a FRESH window); **the enum driver
   runs the TOTAL-RECONCILIATION belt** (Σ enumerated == the Python-side
   `count_combos` total on exhaustion; mismatch ⇒ `enum_total_mismatch`
   whole-tuple fallback — a self-consistent in-core total_count defect
   is deterministically netted per tuple); **work-cap liveness has a
   DETERMINISTIC merge-blocking net** — cap-respected in-band pins
   (`next_rank − start_rank ≤ max(1, max_enumerated)`; work-cells ≤ cap
   + one-mask slop; exact-next_rank cargo units; the fold cap is
   DRIVER-side chunk sizing `max(1, cells // max(1, n_bars))`, a stated
   asymmetry) with the GIL-hold bench demoted to the bench-m4pro lane;
   **`scans_run` is defined under SCAN-LAST ordering** (a bit1 row never
   counts a scan — the equality oracle `scans_run == count(plain rows)`
   is exact).
   Round-5 amendments to this condition (DESIGN §3/§5/§8 R9/§11): **the
   bounded-arm serve reconciliation is mechanised by the POSITION
   CARRY** — the additive `BoundedFinalist.position` field (the ONE
   authorized `sweep_stream.py` edit, §2) carries each finalist's
   SurvivorColumns index, clause (1) reads the stored entry at that
   position behind a positional mask-equality belt
   (`mask_at(position) == mask`), making the reconciliation genuinely
   O(top_k) (the round-4 "evidence the machinery already holds" claim
   is withdrawn — the machinery held no position); **core engagement
   carries the STALL-BUDGET GUARD** — a conservative bound of the
   belt + burn-in + spot-check in-worker overhead is compared against
   the tuple's OWN recomputed #1291 effective stall deadline (same
   formula, constants + env resolvers lazily imported from
   `sweep_parallel`, zero edits there; drift-pinned against the real
   `_effective_stall_seconds`); overhead > 0.5 × deadline ⇒ refusal
   `stall_budget_exceeded`, today's spine at today's timing — the
   "tuples get FASTER so the 250M mask·bars/s floor becomes more
   conservative" composition claim is WITHDRAWN as falsified (a low-k
   1m tuple at top=1000 carried ~17–33 min of belt against a 300 s
   floor deadline: deterministic `SweepStallError` + identical-resume
   livelock without the guard); **the ENUMERATION burn-in is
   WIDTH-KEYED like the fold burn-in** (4 shadow ranges + enum latch
   per kernel width; `reset_enum_self_check_for_tests(width=…)` duals —
   the u128 enum path otherwise carried ZERO production burn-in, and a
   dropped mask cannot be spot-check-sampled); **the burn-in test seam
   is the full per-width STATE-SETTER**
   `reset_self_check_for_tests(width=…, total_remaining=…,
   plain_remaining=…, cap_remaining=…, latched=…)` (supersedes the
   #1531 single-`remaining` signature — the window states the goldens
   must force are counter TRIPLES) + the shared warm-up-tuple
   natural-closure fixture; **the width-selection expression is
   EMPTY-SAFE** (`max(condition_bits, default=−1)+1` — the config-legal
   `bits=[]`/`k=0` tuple engages instead of raising in the seam;
   evaluated OUTSIDE the `kernel_error` catch); **the enum-side default
   `max_enumerated` is divisor-floored** (`2^28 // max(1, n_bars)` —
   the fold-side round-4 idiom applied to its sibling); **any relayed
   per-kept headline states TRADE-BEARING scope** (the blended
   whole-kept-tail figure at the profiled 66/34 zero-trade density is
   ~1.6× Slice-2 / ~1.6–1.8× with the conditional Slice-3 lever
   (final-lock recomposition at the ≈11–13 µs combine basis; was
   "~1.6–1.7×" at the round-6 constants) — DESIGN §9's split formula
   `f_zero·PK_zero + f_trade·PK_trade` is the normative rebuild tool
   for combo_counts.md §3).
   Round-6 amendments to this condition (DESIGN §0/§1.3/§3/§5/§9/§11):
   **the MIN-HITS SHORT-CIRCUIT is a mandated fold-law sibling** — at
   `min_hits == 0` the core MUST skip the per-mask O(n_bars) Stage-1B
   count exactly as today's `if cfg.min_hits > 0` guard
   (sweep_run.py:1465), enforced by the in-band `hits_counted` return
   field (min_hits=0 ⇒ 0; min_hits>0 ⇒ the conflict-free count — the
   `scans_run` standard; an unconditional count is byte-identical but a
   silent O(n_bars)/mask cost ADD on every min_hits=0 grid), and the
   §0/§9 prefilter-floor claims are SCOPED to min_hits>0 grids (the
   term is ZERO in BOTH formula columns at min_hits==0); **the
   STALL-BUDGET GUARD is TOTAL on every tree** — the #1291 symbols
   resolve via getattr with an ABSENT-SYMBOL arm computing against the
   flat `DEFAULT_STALL_SECONDS` (the exact pre-#1291 parent deadline),
   `stall_guard_unavailable` as the total-evaluation refusal, the drift
   pin skipif-scoped to symbol-present trees, and DESIGN §11's
   no-PR-dependence claim scoped accordingly; **the guard is a LIVENESS
   bound, not a net-win bound** — the round-5 "refused shapes are
   exactly those where overhead dominates — a net win" claim is
   WITHDRAWN (an engaged mid/high-TF top=1000 tuple can be a net
   ~1.3–1.6× wall-clock regression: named flip-ADR residual (vii); a
   divergent build's belt-mismatch restart spine pass is unpriced by
   the bound: residual (viii), plus the breaker-runbook
   stall-after-restart line naming `BRUTEX_SWEEP_RUST_CORE=0`);
   **`BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR` carries DEFINED
   two-directional clamps** (lower AT the committed measured default —
   the override can only RAISE pessimism, never under-price the guard;
   upper at 1000×; `stall_guard_clamped` warn-once with direction;
   degenerate probes both directions); **the enum-shadow guard term
   prices its kept-list arm** (`ENUM_SHADOW_NS + n_bars ×
   PREFILTER_NS_PER_BAR` per shadowed mask — every guard term now errs
   toward refusing); **`fold_block` gains the in-core
   `fold_work_ceiling`** (whole-chunk Err above 2^28 cells — 2× the
   override clamp; the round-4 "an oversized chunk is bounded by the
   row_count_mismatch refusal" containment sentence was FALSE — a
   conforming core returns consistent lengths — and is corrected);
   **decode itemsize is a pinned engagement precondition**
   (`array('q').itemsize == 8 == array('d').itemsize` in the layout
   golden + the engagement condition, refusal `native_itemsize_mismatch`);
   **the enum residual is SYMMETRISED** (in-range KEPT-SET divergence —
   silent drops OR spurious keeps — with self-consistent counts; the
   spurious-keep poison twin added; never the round-4 drops-only
   "strictly" wording); **the post-latch enum MID-TUPLE CONTINUATION
   contract is defined** (Python enumeration resumed at `start_rank +
   len(python_window)` via islice; the total-reconciliation assert
   defined over the mixed provenance); **the trade-bearing per-kept
   band is RE-DERIVED** (Slice 2 ~2.0–2.2×, up to ~2.5× with the
   measured Slice-3 lever — the prior 1.9–2.4×/2.7× endpoints omitted
   the retained prefilter+rank segments and retained the eliminated
   exits-marshal; blended whole-tail ~1.6–1.7×).
   External-fold amendments to this condition (2026-07-09 — DESIGN
   §1.3/§3/§5/§9/§9.1/§12 + the `External fold + lock round` log):
   **the per-kept bands are RE-CORRECTED at the honest ≈11–13 µs combine
   basis** (fix-forward item 6 + final-lock recomposition: Slice 2
   ~2.0–2.1×, Slice-3 ~2.0–2.4×, blended ~1.6× Slice-2 / ~1.6–1.8× with
   Slice-3 — the round-6 "~2.2×"/"up to ~2.5×" endpoints rode the
   withdrawn ~3 µs combine bound and must not be relayed; the round-6
   "≈1.8×" blended figure stays non-relayable AS that derivation, though
   the corrected basis independently reaches ≈1.8× at the Slice-3
   optimistic endpoint — DESIGN §9 blended row);
   **the enum shadow is MASK-COUNT-floored** (2^16 masks per process per
   width, 64-range cap; seam `reset_enum_self_check_for_tests(width=…,
   masks_remaining=…, ranges_cap_remaining=…)` — supersedes the round-5
   "4 shadow ranges"/width-only-seam wording above — item 4);
   **`burnin_total_remaining` is the
   64-floor remaining count with the engagement-starvation feedback loop
   + coverage envelope disclosed** (item 7); **the engagement chain is
   ARM-PARTITIONED with a first-failure precedence law** (items 11+13);
   **byteorder reads through a patchable probe** (item 12); **the
   symbol-PRESENT stall-guard branch is covered via monkeypatch-ADDED
   symbols on #1291-absent trees** (item 10); **the kill−9
   byte-identical-redo claim is healthy-build-scoped** (item 3);
   **residual (ix) tallies-only enumeration divergence is named with a
   count-only poison golden** (item 1); **the work-cells charge is
   explicitly min-hits-blind** (items 5+9); **Verifier B laws land:**
   panic policy (F4/F23), window-length invariant `bars_length_mismatch`
   (F27), index→tuple-digest checkpoint golden (F1), kill-after-flush
   RT-09 redo cell (F2), mask-serializer law (F5), one-preload law (F21),
   paisa-mint seam law (F22), strict->= count-parity gate (F15/F18), the
   RT-40 calendar-input contract (post-fix abstain shape on
   `claude/data-edge-guard-fixes`, pending merge), the RT-37 dup-ts
   last-wins golden, and the §9.1 BINDING perf law + S2 /
   trades-never-cross-FFI acceptance criteria.
5. **Honesty framing (Golden Rule #10).** Every claim: per-operation O(1),
   total O(N_masks × N_bars); constant-factor win; sandbox ratios ≠ M4 Pro
   absolutes; the §8 2–4-day SLA re-projection is a PROJECTION until measured
   on the real box; the 1-minute-TF caveat of combo_counts.md §4 stated
   wherever the 10–50 µs/combo figure appears.

## 4. Bit-identity strategy (the load-bearing section)

The new core must reproduce, bit-for-bit, values currently produced by
`compute_metrics_from_scalars` / `_compute_metrics_impl`
(`brutex/metrics/compute.py:569`), `_drawdown_stats`, `build_equity_curve`'s
span expression, and the trade walk. The trade walk is already proven
(#563/#1182: pure integer state machine, 45,863/45,863 trades bit-equal). The
drawdown family is already proven inside an exactness envelope (#1186). The NEW
surface is the O(trades) metric body + gate/term projections. Register of every
operation where Python and Rust float semantics could diverge, with the
resolution for each:

| # | Operation (compute.py cite) | Divergence risk | Resolution |
|---|---|---|---|
| 1 | Integer trade aggregates (`net_pnl`, `gross_*`, counters, `//` floor-divs at `:665-673`) | Python bigint vs i64 overflow | i64 with `checked_*`; inputs pre-bounded by the #1182 price envelope (±i64::MAX/8) and the #1186 2⁵² equity envelope; any overflow → envelope-exit → Python-oracle fallback for that mask (the Phase-A fallback law), never a panic (profile keeps `panic=abort` + `overflow-checks=true`). **Floor divisions NEVER move to Rust (round-3 reconcile to DESIGN §4 row 1):** every `//` (expectancy, avg_*) is computed in the Python combine from Rust-returned exact i64 sums — no Rust floor-div-vs-truncation hazard exists anywhere in this design; the earlier "use `div_euclid`-style floor division explicitly" instruction described an abandoned in-Rust placement and is withdrawn (an implementer must not write a Rust floor-div at all). **`div_euclid` stays NAMED in the fold contract (Verifier B F3; RT-01 dormant):** Python `//` FLOORS, Rust `/` TRUNCATES (−7//2 = −4 vs −3 — a 1-paisa fork on ~half of losing combos); if a future change ever moves a floor-div into Rust it MUST be `i64::div_euclid`; the DESIGN §7.4 negative-numerator dual is the F3 break-oracle golden cell. |
| 2 | `total_return_pct = 100.0 * net_pnl / start_equity` (`:623`) | operand conversion + operation ORDER | reproduce exact expression order: `100.0 * (net_pnl as f64) / (start as f64)`; int→f64 conversion is exact only ≤ 2⁵³ — net_pnl bounded by the 2⁵² envelope pre-guard (same guard as #1186); CPython computes `int * float` by converting the int to f64 with round-half-even — identical to Rust `as f64` semantics ONLY within the envelope; outside → oracle fallback. |
| 3 | `growth ** (1.0 / span_years)` CAGR (`:643`) + `math.pow` in `_skewness` (`:416`) | **libm `pow` has NO correct-rounding guarantee** — #1186 explicitly classified these MUST-STAY-PYTHON | **ONE rule, no alternatives (round-3: the previously-shipped option narration was abandoned reasoning the DESIGN's decision log had already ruled resolved — deleted):** the kernel returns the RAW exact inputs (`net_pnl` → end_equity, raw `m2/m3/m4` moments; `span_years` never crosses the FFI — Python-computed per tuple) and Python's `combine_metrics_from_folds` runs today's LITERAL tokens for `cagr_pct` (incl. the OverflowError→inf clamp), `_skewness`, `_excess_kurtosis`, and every sqrt-derived verdict scalar (std/sortino/downside_dev tails). Zero cross-language `pow` anywhere; zero per-platform libm-equality claim needed; bit-identity by CONSTRUCTION — DESIGN §4 layer rule. |
| 4 | Welford/Pébay moments `_accumulate_moments` (`:118`) | summation ORDER + the exact update formulas | port the EXACT update expressions (same operand order, same `+=`/`*` shapes, strictly sequential left-to-right over trades, NO pairwise/Kahan/SIMD reassociation); returns (n, mean, m2, m3, m4) as f64 — every step is +,−,×,/ (IEEE-correctly-rounded both languages, identical bit results for identical input order). Property-pinned vs the Python oracle over adversarial trade streams. |
| 5 | `sorted(returns)` + percentile interpolation (`:728`) | sort stability + interpolation expression | f64 total-order: Python `sorted` on floats with no NaNs (returns are finite by envelope) == Rust `sort_by(partial_cmp)` on the same values; reproduce `_percentile`'s exact index/lerp expression token-for-token **FROM SOURCE (compute.py:272-280), never a paraphrase (fix-forward item 2): the real function's `len == 1` early return and `lower == upper` exact-index return are enumerated in DESIGN §4 row 12 with value-invisibility proofs — an always-lerp port is bit-identical, and the enumeration exists so the omission can never silently become load-bearing.** |
| 6 | `downside_dev = sqrt(sum(r*r)/n)` (`:703`) | **`builtins.sum()` on Python ≥3.12 is NEUMAIER-COMPENSATED summation (CPython gh-100425), NOT a bare left fold** — a naive sequential Rust fold bit-differs on routine data (round-1 hostile-review MAJOR; empirically confirmed on 3.12: last-ulp divergence on ordinary 1000-value streams, 1.0-vs-0.0 on `[1e16, 1.0, −1e16]`) | port CPython's compensation loop token-for-token FROM SOURCE (running `f_result`, compensation `c`, the `\|f_result\| >= \|x\|` branch, and the GUARDED final add — CPython's return path is `if (c && isfinite(c)) f_result += c;`, NOT an unconditional `f_result + c` (round-5: the guard preserves a −0.0 result's sign and keeps an inf sum from becoming NaN; provably value-invisible for THIS fold's non-negative summands — `f_result` never −0.0, `c` never −0.0, non-finite `c` excluded by the bit1 envelope belt — but the GUARDED form is what ports, so the paraphrase is never the porting authority)) over squares in trade order matching the genexpr — the Neumaier loop is itself deterministic IEEE `+/−`/abs/compare arithmetic, so identical algorithm + identical order ⇒ identical bits; compensation-triggering differential vectors CPython-generated; `f64::sqrt` is IEEE-correctly-rounded in both (hardware sqrt) and stays in the Python combine per DESIGN §4. General law (DESIGN §4): every ported summation construct is CLASSIFIED by its exact CPython algorithm (bare `+=` fold / builtin-sum Neumaier / fsum Shewchuk-never-ported) before porting. **INT-START TRANSITION enumerated (fix-forward item 2):** `sum()`'s `start=0` int fast path consumes the FIRST float via PyNumber_Add OUTSIDE the compensation loop; value-invisible here (non-negative summands, `0 + x₀ == x₀` bit-exact) — enumerated in DESIGN §4 row 6 so a future sign-mixed port cannot silently rely on the unproven case. |
| 7 | Drawdown family + ulcer (`_drawdown_stats`, `metrics.rs`) | already solved | reuse `drawdown_stats_checked` UNCHANGED inside the core (same 2⁵² envelope pre-guard, now checked in Rust with exact i64 arithmetic); the `sum_sq_pct` fold stays a strictly SEQUENTIAL per-BAR left-to-right f64 fold — do NOT collapse equal-value runs into multiplication (k×a ≠ a+a+…+a in f64); buffer reuse only changes allocation, never the fold order. |
| 8 | Round-half-even | int→f64 conversions round-half-even in both languages; `round_money`(ROUND_HALF_UP, Decimal) appears ONLY in the finalist projector (stays Python) | no Rust surface touches Decimal rounding; assert in review that the core never re-derives a money display value. |
| 9 | NaN handling | `_safe_ratio` returns None on zero denominators (`:155`) — None vs NaN is SEMANTIC downstream (spec V-review note in #1352) | represent Optional metrics as (value, present-bit) column pairs in the FFI result — NEVER NaN-encode a None (the SurvivorColumns None-BIT precedent); comparisons for burn-in use the #1531 `drawdown_compare_key` idiom: ints exact, floats by `struct.pack('<d')` bit pattern (Rust: `f64::to_le_bytes`), so +0.0 vs −0.0 divergence cannot hide behind float equality. |
| 10 | Signed zeros | `max()` running-max vs Rust `>` on +0.0/−0.0 | the #1186 precedent: running max via `>` matches Python `max()` on the +0.0-only domain (drawdown pcts are non-negative by construction); the burn-in bit-compare (which distinguishes signed zeros) is the net. |
| 11 | Strict-`>`/`>=` gate + tie boundaries | `passes_gates` (ranking `:192`), `hit >= cfg.min_hits` (sweep_run `:1473`), `win/loss` classification `is_win`/`is_loss`, `end_equity < 1` ruin boundary (`:632`), `r < 0.0` downside filter (`:702`) | reproduce every comparison with the IDENTICAL operator on the IDENTICAL bit-value operand (bit-equal inputs ⇒ identical verdicts — no epsilon anywhere in this code); each boundary gets an exact-boundary test (value AT the threshold on both arms) + a mutation-killing test per the #1182 (e) convention. Gate verdicts that consume Python-computed fields (see #3) are computed AFTER the Python pow-pass, in Python, phase-1 — so the gate boundary semantics are literally today's code. |
| 12 | FMA contraction / auto-vectorization low-bit drift (Verifier B) | LLVM could fuse `a*b+c` or reassociate reductions | rustc NEVER enables fast-math on stable: FP contraction (`fmul`+`fadd`→`fma`) and reduction reassociation require `reassoc`/`contract` fast-math flags rustc does not emit; `mul_add` is the only FMA door and is SOURCE-PINNED ABSENT (the #1186 grep pins `use rayon`/`.mul_add(` extended to `sweep_core.rs`). Build flags stay the crate's committed profile (`opt-level=3, lto="thin", codegen-units=1, panic="abort", overflow-checks=true` — `rust/fno-math/Cargo.toml:88-94`) with NO `-C target-feature`/RUSTFLAGS additions (verified none exist in-repo today); a CI grep-pin refuses `mul_add`, `fast-math`, `ffast-math`, `reassoc` tokens in the crate + workflow files. Auto-vectorization of INTEGER loops is allowed (bit-exact); FP folds are written as scalar sequential loops whose order LLVM must preserve (no reassoc flag ⇒ no reduction vectorisation). |
| 13 | `math.fsum` | trial-stats sidecar uses fsum (`trial_stats`) | stays in Python (per-tuple, O(kept)); the core returns the raw per-kept sharpe column (with None-bits) and Python folds it EXACTLY as today. |
| 14 | span_years expression | `(last_ts − first_ts).total_seconds() / (MIN_PER_YEAR·60)` | computed ONCE per tuple in Python (as today, `sweep_driver.py:511`) and passed IN as a pre-computed f64 — never re-derived in Rust. |
| 15 | Paisa minting + mask serialization (Verifier B F22 + F5, external fold) | `_ema_to_paisa` is repr-mediated (`Decimal(str(v))` → HALF_UP), NOT `(v*100).round()` — tie `v=1.005` mints 101 vs 100; u128 masks in `combo_digest`/WF-hash JSON alias `m` with `m+2^64` if serialized as bare ints (serde_json u64 cap) | BOTH stay Python / string-encoded by LAW (DESIGN §1.3 frozen-input laws + §12 MASK-SERIALIZER LAW): the paisa seam is ported BY NAME only against the frozen corpus-(f) tie cases (answer-key-invisible — its inputs are pre-snapped); every mask serializes as the 32-nibble HEX STRING (`mask:%032x`, spec.rs:219), never a bare JSON int — corpus-(e) bytes are the golden; grep pin refuses new bare-int mask serialization. |

**Burn-in precedent (#1531) extended:** for the first `SELF_CHECK_SAMPLES`
kernel-served masks per process — "kernel-served" round-2-DEFINED as every
mask whose row came back from `fold_block`, INCLUDING `zero_trade` /
`python_required` flag-served rows (the zero-trade verdict is itself a
kernel output here, unlike the #1531 precedent where the shortcut made no
FFI call; the compare is served-bytes vs both arms, DESIGN §5) — TWO
comparison arms ALSO run — (a) a
MECHANISED-PURE oracle (pure-Python walker + `build_equity_curve` +
`compute_metrics` directly; round-1 correction: it must NOT be
`metrics_for_mask`/`metrics_from_trades`, whose drawdown tail routes through
`fno_math.sweep_drawdown_stats` under the ADR-0017 default-ON flag — the
same fold the new core consumes in-crate; purity is AST/import-pinned and
runtime-poison-pinned) and (b) today's shipping OFF-arm
(`metrics_from_trades` → `ResultMetrics`) over the same
(Python-materialised) trades; every column the core returned is bit-compared
(ints exact; floats via `struct.pack('<d')`; None-bits exact). A mismatch ships
the ORACLE values for that mask, latches the core OFF for the process
(`sweep_rust_core.kernel_autoreverted` ERROR, once), and the rest of the tuple
runs on today's spine — results never depend on which engine computed them.
An honest limit stated up-front (as #1531 states it): the burn-in catches only
wrongness that manifests within its window; beyond it, the golden/property/
diffcheck(#1295 R45) layers are the net.

## 5. Honest performance claim framing (Golden Rule #10)

- The conversion removes **~212–217 µs of the measured ~229.5 µs (sandbox,
  48k bars) per-kept-mask Python+FFI residual** (fix-forward item 6
  correction of the earlier "removes the measured ~230 µs" overstatement,
  final-lock RECOMPOSED exactly from its own terms — 229.5 minus the
  retained ≈11–13 µs combine/construction residue minus the ~2–4 µs new
  fold/decode path = 212.5–216.5 µs; the interim "~205–215" band did not
  recompose from those terms at either endpoint) — the ~0.58 ms [AUDIT-RELAYED,
  production windows] figure carries the same per-KEPT scoping.
  **BINDING kernel-cost law (DESIGN §9.1, Verifier B perf reconciliation):**
  post-port per-mask cost follows the PINNED two-term law — walk
  ≈6.2 ns/mask·bar (`RAYON_NUM_THREADS=1`, the production config) +
  ≈4.1 µs/trade; the unpinned count-only ≈1.26–1.28 ns walk rate is
  NEVER campaign math; campaign band from the port alone ≈1.3–3.1×
  (≈2.3–2.5× at median density) — RATIO band ONLY (final-lock: the
  day-count pair "k≤6 ≈5.9 d → ~2–3.5 d" is WITHDRAWN, its 5.9 d anchor
  traceable solely to the withdrawn 0.58 ms×enumerated arithmetic;
  absolute day figures defer to the on-branch measurement); ship
  acceptance = the S2 gate AS DEFINED in DESIGN §9.1 (final-lock: the
  RATIO — inclusive steady-state mask-loop time ≤ ~1.15× of the
  SAME-fixture measured kernel-time sum on the frozen S2 fixture,
  belt/spot-check EXCLUDED and burn-in CLOSED; the "~14.0–14.5 ms @604k
  dense" anchor is COUNTERPARTY-RELAYED and illustrative only — it does
  not recompose from the two-term law) + trades
  never cross the FFI (the measured 1.04–1.16× half-port counterexample).
  **UNIT LAW (round-2): both constants are per-KEPT-mask quantities and must
  NEVER be multiplied by ENUMERATED-mask counts** — a prefilter-dominated
  enumerated combo costs ~15 µs today at 48k bars (2.4 µs enum + 12.5 µs
  prefilter, baseline.md §2), not 0.58 ms; the combo_counts.md §3 grid rows
  built on `0.58 ms × enumerated` are withdrawn as unit-conflated, and grid
  wall-clock headlines are deferred to the DESIGN §9 bottom-up formula + the
  required on-branch sub-grid measurement.
  Post-conversion, per-combo cost is KERNEL-BOUND:
  ≈ 0.26 ns/bar·mask prefilter (all enumerated) + ≈ 1.3 ns/bar·mask walk +
  ≈ 1–2.7 ns/bar·mask drawdown fold + O(trades) metric folds (per kept mask),
  sandbox rates (`baseline.md` §2 — UNPINNED free-rayon, count-only walk
  tier; shape context ONLY, never campaign math — under the pinned
  production config the walk is ≈6.2 ns/mask·bar + ≈4.1 µs/trade, the
  BINDING §9.1 law above).
- The **10–50 µs/combo band is DEMOTED (round-2) from a target to a
  possible OUTPUT of the DESIGN §9 bottom-up blended formula** (`T_post ≈
  enum + s·prefilter(n_bars) + f·per_kept(n_bars)`, with s = conflict-free
  fraction and f = kept fraction STATED UNKNOWNS): it holds only where
  n_bars and f are small (e.g. @3k bars, f→0: ≈ 0.8–0.9 µs; @48k, f→0:
  ≈ 12.6 µs — prefilter-floor-bound), and it must NEVER be paired against
  a "today ≈ 0.58 ms" anchor (per-KEPT unit — the UNIT LAW above). A
  full-window **1-minute-TF tuple's per-KEPT-mask cost stays walk-bound at
  ~0.7 ms sandbox (UNPINNED free-rayon count-only rate; under the DESIGN
  §9.1 PINNED production config the walk is ≈6.2 ns/mask·bar ⇒ ~3.4 ms
  per kept mask @555k bars, plus ≈4.1 µs/trade) (~est. 0.25–0.4 ms M4
  Pro, unpinned basis) — Rust cannot remove O(N_bars)**;
  see `combo_counts.md` §4 for the honest wall-clock model and the prefilter
  caveat (even the cheapest per-mask O(n_bars) op costs ~140 µs at 555k bars).
  Any quote of the band must carry this caveat + the formula's s/f inputs.
- No M4-Pro absolute is claimed until measured there (bench-m4pro /
  DEC-BENCH-ABS-BASELINE-001 lane); sandbox numbers ship as ratios with the
  #1182 "sandbox ratios ONLY" wording.
- The §8 "2–4 days for ~33B trials" SLA is NOT claimed met by this change;
  the post-conversion projection table in `combo_counts.md` is labelled
  PROJECTION with per-line caveats.

## 6. Ship-gate checklist (concrete)

1. Twin-run byte-identity: OFF-arm vs ON-arm store bytes + content hashes +
   sidecars equal on (a) the golden fixture grid, (b) a small real-data grid
   (both directions, ≥2 TFs, k∈{1,2,3}, with and without SL/TP), (c) a
   `sweep_parallel` workers>1 run (per-worker burn-in + parity).
2. `_GOLDEN_RUN_ID` + `_GOLDEN_CONTENT_HASH` literals byte-unchanged; ON arm
   reproduces them WITH exactly one engaged event and zero autorevert events.
3. `tests/golden/fixtures/` diff EMPTY (incl. `vocab_golden_vectors.json`).
4. Property suites pass UNMODIFIED: P-06 look-ahead
   (`tests/property/` LookAheadBarrier suites), suffix-independence
   (`tests/property/test_sweep_suffix_independence.py`), rust-metrics
   equivalence; NEW hypothesis cross-arm full-column bit-equality property.
5. Chunk-invariance: Rust-core internal batch size never changes results
   (pinned across ≥5 sizes, the #1182 pattern).
6. Bench gates: per-op O(1) at the honest tier ceilings; a speedup floor far
   under the measured ratio (flake-proof, regression-loud — the #1531
   anti-vacuous-twin pattern with a forced-demotion arm); an OFF-arm cost gate
   (< the #1186 off-cost envelope: one env probe).
7. Coverage 100% line+branch on touched modules; ruff/mypy --strict/pyright
   baselines; cargo test/clippy/fmt clean; cargo-mutants zero-survivor on the
   new shard (weekly lane); python mutmut on touched seams.
8. Kill switch + autorevert drill: `BRUTEX_SWEEP_RUST_CORE=0` restores today's
   spine byte-identically; a poisoned kernel inside burn-in lands on pinned
   bytes with ONE autorevert event; past burn-in, a domain-valid poison MOVES
   the hash (the #1186 inverted engagement-teeth test — proves the seam
   genuinely consumes the kernel) — required on BOTH the fold arm AND the
   enum arm (a dropped kept mask ⇒ tallies + hash MOVE; round-1 gap); the
   belt-restart golden additionally pins audit-line count == 3 and
   SWEEP_COMPLETE tally-payload byte-equality vs the pure-Python arm
   (restart re-enters strictly BELOW the already-durable SWEEP_START line —
   DESIGN §5).
9. Round-2 additions to the gate: the restarted pass runs ZERO
   belt/spot-check/burn-in compares (core-arm-only scoping + process-global
   burn-in counters — second tuple in the same worker runs zero compares,
   pinned); the belt-disambiguation triple unit-pinned (a PRE-EXISTING
   spine-vs-oracle divergence ⇒ NO restart + distinct
   `belt_preexisting_divergence` ERROR; only a core defect latches +
   restarts); a zero-trade-misflag poison inside the burn-in window ⇒
   pinned bytes + one autorevert (flag-served burn-in membership teeth);
   decode refusal vectors incl. a truncated-but-stride-clean block + a
   cross-buffer length mismatch (named `kernel_result_refused` reasons;
   `zip(strict=True)` belt); per-INDEX refusal pins (`bits=[100]` on the
   u64 core / a ≥width conflict-pair index ⇒ `Err`, never SIGABRT);
   `unbounded_top_k` refusal (top_k=None ⇒ Python spine, belt never runs);
   the zero-trade-mask COST bench pin (round-3 re-laned: the deterministic
   in-band `scans_run` pins are the merge-blocking net; the wall-clock pin
   rides the M4-Pro lane with its ceiling including the retained Python
   tail — item 10 + DESIGN §1.3/§7.7; a bit-safe unconditional drawdown
   scan flips `scans_run` and fails the laned bench);
   the ≥10⁶-case differential harness's on-branch full-sweep run executed
   + wall-clock recorded in the PR body (DESIGN §7.10); spot-check
   `spot_check_sampled` observability pins (== 4 on a ≥4-non-finalist
   fixture, == 0 skip on an all-finalist fixture).
10. Round-3 additions to the gate: the digest-path golden VARIANT
    (OFF-arm-minted literals; ALL engagement/poison goldens run against
    it) + the legacy-refusal negative golden (original fixture, flags ON,
    zero engaged events, `legacy_dedup_path` reason); per-arm NEGATIVE
    engaged-event pins (the other arm's event count == 0) + the two
    disabled-arm poison goldens (poisoned core under `CORE=0,ENUM=1` and
    poisoned enum under `ENUM=0,CORE=1` both reproduce pinned bytes);
    the in-band `scans_run` zero-trade pins (all-zero-trade chunk ⇒
    `scans_run == 0`; mixed chunk ⇒ `== n_plain_rows` — the deterministic,
    merge-blocking net for the §1.3 short-circuit law; the wall-clock
    zero-trade bench rides the M4-Pro strict lane, DESIGN §7.7 lane
    honesty); reserved-flag-bits decode refusal (`flags` bits 2–7 nonzero
    ⇒ `kernel_result_refused: reserved_flag_bits`); degenerate work-cap
    probes (`BRUTEX_SWEEP_CORE_{ENUM,WORK}_CELLS=1` ⇒ lower-clamped +
    warn-once + byte-identical completion — the ≥1-mask-per-call progress
    law + in-band `exhausted` flag, DESIGN §3); the `start_rank >
    total_count` Err refusal pin + the k=0 vector (kept == [0],
    enumerated == 1, exhausted) baked beside the C(128,64) refusal pin;
    burn-in non-trivial-floor pins (an all-zero-trade first tuple leaves
    the window OPEN — plain rows of the NEXT tuple are still compared;
    cap-trip ⇒ one `burnin_nontrivial_floor_unmet` warn); spot-check
    retained-candidate pins on the BOUNDED rank arm (served-object
    compare; candidate memory O(16)).
11. Round-4 additions to the gate: the BOUNDED-ARM SERVE RECONCILIATION
    pins — a stream-only nondeterministic poison (finalist's streamed
    fold corrupt, rebuild clean, past burn-in, belt ON) ⇒ pinned bytes +
    one `stream_rebuild_divergence` + tuple restart (this poison PASSED
    the round-3 belt — the teeth); rank-column reconciliation + gate
    re-verdict + candidate-finalist compare unit pins (spy: the
    candidate-finalist served object is compared, round-3's exclusion
    dead); the enum TOTAL-RECONCILIATION pin (stubbed wrong-total return
    ⇒ `enum_total_mismatch` + spine serves + pinned bytes); CAP-RESPECTED
    deterministic pins (dual-knob matrix calls assert
    `next_rank − start_rank ≤ max(1, max_enumerated)` and
    `(next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars`;
    in-crate exact-next_rank units — a 1-mask cap advances exactly 1;
    the fold chunk-sizing function unit-tested incl. n_bars == 0; GIL
    bench re-laned to bench-m4pro with only a catastrophic shared-CI
    backstop); WIDTH-KEYED burn-in pins (u64-closed window + u128
    engagement ⇒ fresh u128 window; per-width latches independent);
    the layout golden compares the EXPORTED
    `SWEEP_CORE_FOLD_{INT,FLOAT}_FIELDS` module tuples (the five-symbol
    sextuple update); the module-scoped
    `#![deny(clippy::indexing_slicing)]` present in sweep_core.rs
    (grep-pinned); mutants-lane proptests run reduced-case AND
    FIXED-SEED (`SWEEP_CORE_PROPTEST_SEED`) with every mutant class
    killable by a deterministic test; `brutex/engine/sweep_core.py`
    joins the weekly python mutmut loop (runtime measured on-branch;
    zero-survivor is a Slice-4 flip precondition); scans_run scan-last
    pin (a bit1/envelope row counts NO scan); the per-kept headline is
    quoted SLICE-SCOPED (round-7 item-6 correction: Slice 2 ~2.0–2.1×;
    ~2.0–2.4× only with the shipped-and-measured Slice-3 lever — the
    round-6 "up to ~2.5×" endpoint is withdrawn, never relay).
12. Round-5 additions to the gate: POSITION-CARRY pins —
    `BoundedFinalist.position` byte-neutrality (bounded rank
    order/scores byte-identical pre/post the additive field) +
    `columns.mask_at(position) == mask` for every finalist + a stubbed
    position-threading corruption fails the positional belt; the
    STALL-BUDGET GUARD pins — a synthetic low-work/high-top_k shape
    refuses with `stall_budget_exceeded` + spine bytes, a high-work
    shape engages, the seam's recomputed deadline drift-pinned against
    the real `ParallelGridDriver._effective_stall_seconds` over a grid
    of (work, env) inputs, and a raised `BRUTEX_SWEEP_STALL_SECONDS`
    un-refuses through both formulas; enum burn-in WIDTH pins (4 u64
    shadow ranges consumed + a [64,128)-bit tuple ⇒ 4 FRESH u128 shadow
    ranges; per-width enum latches independent); burn-in STATE-SETTER
    duals (each golden-forced window state — closed /
    near-cap-floor-unmet / closed-u64-open-u128 — reachable via the
    full seam; the warm-up-tuple fixture proves natural closure once);
    the bits=[]/k=0 seam vector (empty-safe width ⇒ engages, kept ==
    [0], spine-identical bytes) + the empty-bits in-core vector beside
    the k=0 pin; the `mutants::skip` grep refusal in sweep_core.rs
    (the attribute exists and is forbidden — per-mutant `exclude_re`
    only); the digest-variant literals documented as
    SHIPPING-SPINE-authored (new-core-free), never "pure-Python /
    never Rust-authored"; the Slice-4 flip ADR presents the MEASURED
    max belt+burn-in phase duration vs the #1291 effective deadline at
    the operator's actual `--top` on the soak grid, and quotes the
    blended whole-kept-tail per-kept figure (DESIGN §9 split formula)
    beside the trade-bearing headline.
13. Round-6 additions to the gate: MIN-HITS SHORT-CIRCUIT pins — in-crate
    `hits_counted` units (min_hits=0 range ⇒ 0; min_hits>0 ⇒ enumerated −
    conflict_pruned) + the Python seam pin on the decoded value (the
    deleted-guard mutant is data-identical but flips `hits_counted` and
    dies deterministically); STALL-GUARD totality pins — absent-#1291-
    symbol duals (flat-`DEFAULT_STALL_SECONDS` deadline arm engages/
    refuses deterministically; unresolvable constants or an injected
    guard-evaluation exception ⇒ `stall_guard_unavailable`, never a
    crash; the `_effective_stall_seconds` drift pin skipif-scoped to
    symbol-present trees); `BRUTEX_SWEEP_CORE_ORACLE_NS_PER_BAR`
    degenerate-override probes (=1 ⇒ clamped LOW to the committed
    default + `stall_guard_clamped` direction=low; 10**12 ⇒ clamped
    HIGH + direction=high); the `fold_work_ceiling` in-crate unit
    (masks.len() × n_bars > 2^28 ⇒ whole-chunk Err) + the seam's named
    whole-tuple fallback mapping; the `native_itemsize_mismatch`
    refusal + the layout-golden itemsize pins (`array('q').itemsize ==
    8 == array('d').itemsize`); the SPURIOUS-KEEP enum poison twin (a
    wrongly-KEPT conflicted mask past enum burn-in ⇒ tallies + content
    hash MOVE — teeth on both directions of the kept-set residual); the
    MID-TUPLE ENUM-LATCH continuation pin (stubbed range-2-of-4
    mismatch ⇒ islice continuation, spine-identical bytes, the
    mixed-provenance total-reconciliation assert passes); the twin-run
    VENUE/EVIDENCE law (real-data + workers>1 twins as on-branch gates
    with commands + both arms' hashes recorded verbatim in the PR
    body; the golden twin in the CI lane); the flip ADR carries the
    NINE-fold residual list (external fold: + (ix) tallies-only
    enumeration divergence — item 14) incl. (vii) engaged-net-regression and
    (viii) restart-path stall, the SYMMETRISED enum residual wording,
    and the round-7-corrected headline bands (~2.0–2.1× trade-bearing
    Slice 2 / ~1.6× blended — item-6 basis).
14. External-fold additions to the gate (2026-07-09 — cite ids): the
    COUNT-ONLY (tallies-only) enum poison golden — mis-bucketed drop past
    the enum shadow ⇒ trial-stats SIDECAR hash MOVES while the STORE
    content hash is pinned UNCHANGED (fix-forward item 1 / residual (ix)
    teeth); the INDEX→TUPLE-DIGEST checkpoint golden (F1 — positional
    checkpoint map OFF ≡ ON, a tuple reorder breaks a golden instead of
    prefix-skipping); the KILL-AFTER-FLUSH REDO cell (F2/RT-09 — kill −9
    after flush/before sentinel ⇒ byte-identical redo + the corpus-(b)
    audit-chain shape); the MASK-SERIALIZATION golden over corpus (e) +
    its grep pin (F5); the RT-37 DUP-TS LAST-WINS golden (dup-ts oracle
    (i)); the `bars_length_mismatch` seam pins both directions (F27); the
    strict->= `count_hits ≡ count_supermasks` parity property on both
    widths + corpus-(h) boundary goldens (F15/F18); the panic-policy pins
    (profile grep `panic="abort"`; the PanicException seam catch mapped
    to `kernel_panic_exception` unit-pinned — F4/F23); the
    `_host_byteorder()` probe-patched `big_endian_host` dual (item 12);
    the monkeypatch-ADDED symbol-present stall-guard duals (item 10); the
    enum-shadow MASK-FLOOR pins (window stays open across small ranges
    until 2^16 masks; `enum_shadow_floor_unmet` cap-trip warn — item 4);
    the arm-partition + precedence-law goldens (items 11+13); the
    Slice-1 on-branch sub-grid measurement run ALSO at the operator's
    `--top` (item 7); the S2 acceptance gate (post-port inclusive
    steady-state mask-loop time ≤ ~1.15× of the SAME-fixture measured
    kernel-time sum on the frozen S2 fixture — belt/spot-check excluded,
    burn-in closed; the ~14.0–14.5 ms @604k anchor is
    counterparty-relayed, illustrative only — DESIGN §9.1 final-lock
    definition) + the
    trades-never-cross-FFI acceptance criterion (measured 1.04–1.16×
    half-port counterexample) — DESIGN §9.1; the RT-40 calendar-contract
    parity pin (post-fix Muhurat weekly-anchor ABSTAIN shape — fib=None,
    Group-4 bits cleared, one `stage1a.muhurat_weekly_anchor_ineligible`
    WARN, tuple completes — once `claude/data-edge-guard-fixes` merges;
    the pre-merge abort shape is a pre-existing engine truth, never
    converted to a silent skip).

## 7. Batching + crash semantics (Verifier B pre-empt; detail in DESIGN §7)

- The FFI unit is a bounded chunk of masks WITHIN one tuple; ALL durable
  writes stay per-tuple and Python-side. A `kill -9` at ANY point mid-tuple —
  worker or scheduler/writer-actor, before/inside/after any Rust call — leaves
  the tuple's sentinel ABSENT, so the next run redoes the FULL tuple and
  rewrites every artifact byte-identically (the sentinel-authoritative redo
  law, DEC-MARK-MANY-SENTINEL-001). There is NO partial-batch durable state:
  the Rust core's accumulators live in process memory only.
- Scheduler-vs-worker kills (parallel driver): a killed WORKER surfaces as
  BrokenProcessPool → #1291's quarantine + stall-guard semantics (unchanged —
  the parent watchdog is cross-process and does not depend on in-tuple
  progress); a killed PARENT loses only unapplied outcomes — the
  contiguous-clean-prefix checkpoint + sentinels re-derive on resume.
- #1295 crash-loop-breaker × ADR-0017 autorevert interaction: a burn-in
  MISMATCH is NOT a crash — the oracle value ships, the process latches the
  kernel off, the run CONTINUES (no crash-loop). A kernel PANIC under
  `panic=abort` is a SIGABRT = worker death → pool-broken quarantine (#1291)
  → repeated deaths trip #1295's crash-loop breaker, whose remediation
  playbook must name `BRUTEX_SWEEP_RUST_CORE=0` as the first lever (runbook
  addition — the breaker cannot flip the env itself; an auto-flip is a design
  decision explicitly NOT taken here, see open_questions.md Q5). The
  autorevert latch is PER-PROCESS and non-durable BY DESIGN (mirrors #1531);
  a crash-looping box therefore re-enters burn-in each restart — bounded,
  loud, and results-safe (oracle values ship during every burn-in window).
- **Panic policy, explicit (Verifier B F4 + F23 — DESIGN §3):** the crate
  keeps `panic="abort"` (grep-pinned), so `pyo3_runtime.PanicException` —
  a BaseException subclass that would bypass `except Exception` AND the
  #1291 quarantine taxonomy — is structurally unconstructible; as
  defence-in-depth the seam catches PanicException explicitly and maps it
  into the `SweepComboError`/TUPLE_FAILED quarantine taxonomy with the
  distinct alert reason `kernel_panic_exception` + process latch. F23
  supervised-daemon honesty: on the sequential arm a deterministic SIGABRT
  is a relaunch crash-loop → breaker → GAVE_UP with later tuples
  uncomputed (runbook lever #1 is the remediation); a supervisor-distinct
  per-tuple-skip exit code is a NAMED FOLLOW-UP for the P2 supervisor
  owner, not claimed here.
- **Kill-after-flush REDO cell (Verifier B F2, RT-09):** the twin-run ship
  gate includes a kill −9 strictly AFTER `flush()` / before the sentinel
  on the CORE arm — resume must redo the tuple byte-identically with the
  audit chain `[SUPERVISOR_START, TUPLE_SKIPPED_RESUME, TUPLE_DONE,
  SUPERVISOR_COMPLETE]` (baseline corpus (b)); a non-bit-identical
  recompute would be a permanent grid-wide append-conflict abort
  (`ResultsStoreError` is not per-tuple-quarantined) — the cell proves it
  cannot happen. The kill −9 byte-identical-redo claim is
  HEALTHY-BUILD-scoped (fix-forward item 3; DESIGN §3 carries the
  divergent-build engagement-history disclosure).

## 8. Identity & goldens register (quick reference)

| Artifact | Key composition | Rust-core effect |
|---|---|---|
| `mask_run_id` | blake3 over 9 fields incl. `code_commit_sha` (injected) | unchanged formula; computed lazily for finalists/errors ONLY (as today); engine NOT in the hash |
| `_tuple_digest` (store path + sentinel + sidecar pairing) | blake3 over tuple axes + grid provenance incl. `code_commit_sha` | unchanged; new commit ⇒ full re-key (no cross-engine skip-complete possible) |
| `FlushResult.content_hash` | sha256 of Parquet bytes | must be byte-equal across arms (the twin-run gate) |
| audit chain | hash-chained JSONL, 3 lines/tuple | byte-identical payload keys; NO new action (the #1182 engine-blind convention) |
| frozen goldens | synthetic identity fields ("deadbeef") | commit-independent literals stay frozen |
| 400-case vocab key | trading-core evaluator | untouched crate ⇒ untouched key |

## 9. ROADMAP — operator's standing Rust plan (context; NON-GOALS for this PR)

Operator's standing decree (relayed via coordinator): **"except the Groww
pull/fetch, EVERYTHING becomes Rust O(1)-per-operation"**, sequenced
EVIDENCE-FIRST:

1. **This sweep-core conversion lands PROVEN first** — it owns the measured
   ~85% of sweep wall-clock [AUDIT-RELAYED; independently re-derived at 57–85%
   depending on window scale, `baseline.md` §3]. Scope stays surgical: the
   mask-loop surgery site only.
2. **Then a Rust-conversion QUEUE ordered by MEASURED Python-share per
   hotspot** (an independent whole-system audit is producing that table —
   not this document). Honest note: the polars/DuckDB read surfaces are
   already native; the real candidates are the PYTHON LOOPS around them —
   e.g. the movers ranking loops, the Greeks-enrich orchestration, the
   prebuild/TF-materialization driver, Stage-1A's per-bar Python push loop
   (~14.5 µs/bar sandbox, `baseline.md` §2). Each queue item repeats THIS
   protocol: multi-agent design + hostile review to 2 clean rounds,
   bit-identical proof wherever results exist, default-OFF flag + burn-in +
   autorevert where applicable, draft PR, coordinator merges.
3. **The vendor pull/fetch stays Python by explicit operator exception**
   (rate-governed I/O — Rust buys nothing there; the §2 Rule 6 rate machinery
   is untouched forever under this plan).

Nothing in items 2–3 is implemented, promised, or scheduled by THIS change;
they are recorded so the PR body can cite the standing plan without scope
creep (Golden Rule #3: no silent expansion — each queue item needs its own
DEC + review cycle).
