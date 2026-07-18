# ADR-0025: The 1-minute execution grid

> **Number provenance:** ADR-0024 is claimed by an un-landed branch (per the
> coordinator's reservation state at build time), so this ADR takes 0025 and
> the numbering gap is DELIBERATE — flagged to the coordinator; the
> renumber-at-landing law stands if the reservation state moves. ADR-0023 is
> PR-A's (worst-case-only fills).

- **Status:** ACCEPTED (the operator fill law; PR-B of the
  worst-case-fills-only track, built under the 2026-07-18 operator-approved
  W4 lane grant; design frozen 2026-07-15 at
  `docs/design/fills/PRB_1M_EXECUTION_GRID_DESIGN.md`, absorbed verbatim
  onto this branch — draft PR #1564, the do-not-merge review vehicle,
  closes without merge when this lands)
- **Decision ID:** DEC-FILL-EXEC-GRID-1M-001 (MIGRATION.md #1979;
  supersedes the INTERIM-HONESTY clause of DEC-FILL-WORST-CASE-ONLY-001 —
  MIGRATION.md #1825 / ADR-0023 — whose rider records the supersession)
- **Date:** 2026-07-18
- **Branch:** `claude/fill-1m-exec-grid`

## Context

ADR-0023 (PR-A) made worst-case the ONLY fill model but priced the adverse
extreme on the DECISION-TF bar — an explicitly INTERIM law: a 5-minute entry
took the whole 5-minute range's adverse depth, strictly MORE pessimistic
than the operator's stated target law ("the fill resolves on the NEXT
1-minute bar strictly after the signal bar's CLOSE, priced worst-case within
that 1-minute bar, for EVERY decision TF"), never optimistic. PR-B replaces
the interim with the real law. The settled interpretations (design §0,
coordinator-disclosed): SL/TP monitoring ALSO moves to the 1m grid;
same-1m-bar entry+exit ships the DEFERRAL; PR-B is its own DEC+ADR; the
PR-A interim-honesty clause is retired; a missing exact next-1m bar takes
the session-bounded SLIDE, ratified and unified with PR-A's shipped walker.

## Decision

1. **Dual-feed engine.** The sweep preloads, per swept symbol, the union
   `interval_list ∪ {"1minute"}` into the SAME slices mapping (one G10
   read-guard span, one lake generation, the same read-boundary guards; the
   `data_snapshot_hash` fold covers the 1m slice with zero change to the
   hash derivation). The decision stream contributes ONLY signals
   (Stage-1A untouched); the walker and the Rust kernels walk the
   EXECUTION stream. The signal→fill mapping is the precomputed
   `ExecGridMap` (`brutex/engine/exec_grid.py`):
   `exec_idx(N) = first ts_1m >= ts_tf[N] + close_step_minutes(tf)` —
   pure UTC arithmetic, built once per `(symbol, tf)` coordinate.
2. **Worst-case only, unchanged — now on the 1m grid.** Entry at the fill
   1m bar's adverse extreme; SL/TP detected per 1m bar and filled at the
   adverse-max-vs-level on that same bar (gap-open law included); same-bar
   tie always SL-first; the same-bar entry+exit DEFERRAL (the entry bar
   folds excursions but never exits — counted `exits_deferred`); forced
   session-close at the first 1m bar reaching the session's forced instant;
   end_of_data/session_gap at the last stored 1m bar's adverse extreme with
   the B3 terminal carve-out (when that bar IS the entry bar the fill is
   its CLOSE — the #1743 basis); missing minutes take the session-bounded
   SLIDE with every slid class counted; earliest-armed wins a collision
   (losers counted); the bounded-distance selector assertion refuses (never
   fills) a same-session violation; a printed volume-0 1m bar is REAL and
   TRADABLE on the engine path (firewalled from the executor always-reject
   ruling — two laws, two surfaces).
3. **Run identity re-keys.** `canonical_trade_params` emits
   `params["execution_grid"] = "1m_next_bar"` UNCONDITIONALLY beside the
   PR-A `fill_mode` token, with the PR-A mirror set carrying it
   (grid descriptor, tuple-cache params, tuple digest, cascade spec dict
   with refuse-on-absence, sweep-verify's generation gate, the SWEEP_LAUNCH
   audit line). The `data_snapshot_hash` additionally folds the 1m slice.
   Decision-TF-era artifacts refuse LOUDLY, never replay silently.
4. **Kernel FFI reshape.** The decision-grid OHLC columns drop out of the
   Rust FFI entirely: the kernels take the 1m exec paisa columns +
   forced/session ordinals + `arm_source` (the precomputed projection) +
   DECISION-length `bar_bits`, u64 and u128 through the ONE generic core;
   a pre-PR-B wheel demotes to the dual-feed Python oracle warn-once
   (`exec_grid_kernel_stale`), never a wrong-law kernel answer.
5. **1m quantities.** `holding_bars`, exit indices, MFE/MAE paths and
   `time_in_market_pct`'s denominator are EXEC-stream (1-minute)
   quantities; the metrics tail translates exit indices back to owning
   decision bars via `decision_of` for the per-decision-bar equity fold.

## Consequences

- **Every run_id re-keys ⇒ a deploy re-sweeps everything.** Three
  independent forces (the snapshot grows the exec slices, the params dict
  grows the key, `code_commit_sha` moves). SANCTIONED as a greenfield
  re-key (operator-approved 2026-07-18 — no production backtests were ever
  run); documented, not fought.
- **Every golden re-locked AGAIN** (the second re-lock in the fills track),
  each value hand-derived in-file; the #1825-era literals survive only as
  frozen anti-alias pins (`_PRE_GRID_RUN_ID` beside `_PRECISE_ERA_RUN_ID`).
  The vocabulary fixture and every Stage-1A bar_bits golden are
  byte-untouched (empty diff) — no `indicator_version` bump.
- **tf==1day is structurally empty** (design §6b): every 1day arm point
  crosses a session under the 1440-minute close-instant conversion — zero
  trades, pinned. tf==1minute is the fixed point whose ONE delta from PR-A
  is the same-bar deferral.
- **New aggregate counters** (`exits_deferred`,
  `signals_dropped_collision`, the `fill_slid_minutes` family) enter the
  aggregate columns (AGG_COLUMNS 50→55).
- The R45 diffcheck reference is its own independent dual-stream
  implementation (never imports `exec_grid.py` — AST-banned); coarse-TF
  results generally become LESS punitive than the PR-A interim while
  staying never-optimistic vs reality.

## Alternatives considered

- **WITHHOLD instead of SLIDE for missing 1m bars** (design §5.4 — fully
  drafted, flippable): an arming fill whose exact next 1m bar is not stored
  would be withheld (entry skipped, counted) instead of sliding to the next
  stored same-session minute. REJECTED for v1 by coordinator final ruling
  (design §5.2 ships the SLIDE): the slide is PR-A-coherent (PR-A's shipped
  walker already slides, so tf=1minute stays a fixed point), preserves
  trades on hole-bearing lakes with every slid class counted, and always
  prices a REAL stored bar worst-case (never a windfall). The flip is NOT
  symmetric — WITHHOLD would also require changing PR-A's shipped walker
  (an ENGINE change + golden re-locks), and would put TWO deltas on the
  tf=1minute case. WITHHOLD remains the documented alternative, one edit
  away, with named counters reserved
  (`entries_withheld_missing_1m` / `exits_withheld_missing_1m`).
- **Ordered-path same-bar exits** (H-then-L on the entry 1m bar) — the
  PR-A-era waiver at decision-TF granularity — REJECTED here for the
  deferral (design §3.12): at 1m granularity the deferral is the honest
  bound (an entry and an exit never share one bar).
- **Binary search per signal instead of the precomputed map** — rejected
  (design §1.2): the map is mask-independent, built once per coordinate,
  and lets the kernel receive integers only (no timestamps cross the FFI).

## Honest envelope

- Per-mask kernel cost is O(#signals + Σ in-position minutes + #trades)
  via the flat-skip jump table — NOT O(1)-total; the Python walker and the
  diffcheck reference deliberately do not flat-skip (independence oracles).
- The kernel-resident exec columns are ≈30 MB per index instrument by
  derivation; the Python CandleBar-list footprint remains UNMEASURED (the
  design's measure-at-build item, named honestly).
- The design's dedicated bench-acceptance teeth (the A/B ≤2.0×
  tuple-throughput refusal gate, the dedicated sparse-mask flat-vs-N_1m
  pin) were not minted as new gates in this pass — the existing
  throughput/O(1) benches were re-shaped dual-feed and pass; a named
  residual for the lane.
- The fill-sensitivity annotation (the R13 half, MIGRATION.md #1979) is a
  run-end O(|top100|·bars) batch on the Python oracle — disclosed, never
  the sweep hot path; annotation never re-ranks or hides.
- The B1 provenance law is STRUCTURAL in v1 (the engine reads the primary
  tree only); the dual-vendor both-write-orders bit-identity golden is a
  named fixture-dependent obligation, and a runtime provenance assertion is
  REQUIRED if dual-vendor engine reads ever become reachable.
- The born-rust placement of `exec_grid.py` (a Python stdlib leaf whose law
  is mirrored in-kernel via the precomputed `arm_source`) is recorded and
  coordinator-flagged in MIGRATION.md #1979 — the hot path IS Rust; moving
  the map BUILD into the crate stays open.
