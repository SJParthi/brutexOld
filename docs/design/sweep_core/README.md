# Rust sweep-core conversion — CHECKPOINT-1 audit artifact pack

**Status: LOCKED FOR CHECKPOINT 1.**

This directory is the CHECKPOINT-1 audit artifact pack for the Rust
sweep-core conversion (DEC ids pending the implementation PRs — the
implementation slices land on this branch next with their own
MIGRATION.md ledger entries). This commit is **documentation-only** —
no `brutex/` or `rust/` code is included.

## Contents

| File | What it is |
|---|---|
| `SPEC_RUST_SWEEP_CORE.md` | The conversion spec (scope, FFI surface, identity contract) |
| `DESIGN_FINAL_RUST_SWEEP_CORE.md` | The synthesized final design (4 hostile-review rounds folded; §9/§9.1 hold the binding perf law) |
| `baseline.md` | Measured sandbox baseline (per-mask / per-kept / per-enumerated cost decomposition) |
| `combo_counts.md` | Grid combinatorics (its §3 wall-clock table + §5 sanity-check are WITHDRAWN/SUPERSEDED for projections — banner in-file; the §1/§2 combinatorial counts stand) |
| `external_findings_verifier_b.md` | Verifier B's external findings (counterparty-relayed anchors labelled as such; the day-count claims under its "BINDING" header are WITHDRAWN — banner in-file) |
| `perf_RECONCILIATION.md` | The walk-rate discrepancy reconciliation (two-term fit; from `perf_reconcile/RECONCILIATION.md`) |
| `fit_results_pinned.json` | Pinned-config fit results backing the reconciliation |
| `perf_reconcile_raw/` | Raw measurement artifacts behind the reconciliation (grids + scripts; container caveat in its README) |
| `reads/` | Codebase read notes (dedup, identity, parallel, rust, spine, tests; `reads/rust.md` §6's sibling-crate note is SUPERSEDED — the locked design is an in-crate module, banner in-file) |
| `candidates/` | The three design candidates the final design was synthesized from (correctness / perfmax / riskmin) |

## Withdrawal / supersession register (which figures, in which files)

Binding perf numbers live in EXACTLY two places: **DESIGN_FINAL §9/§9.1**
(the two-term cost law + the ratio band) and **`fit_results_pinned.json`**
(the pinned-config fit cells). Everything below is withdrawn or superseded
(Verifier A round-1 fold, 2026-07-09):

- `combo_counts.md` §3 wall-clock table + §5 sanity-check — WITHDRAWN
  (per-KEPT × per-ENUMERATED unit conflation; the "~0.58 ms/combo" anchor
  and every day/hour cell derived from it). The §1/§2 combinatorial counts
  stand.
- `external_findings_verifier_b.md` day-count claims — "k≤6 ≈5.9d →
  ~2–3.5d" (despite sitting under that file's "BINDING" header) and
  "k≤6 ÷1.7–2.2 ≈ ~2–8 M4-days" — WITHDRAWN; ratio band only is binding.
  Its "measured ~3.0–3.97×" rayon attribution is corrected in-file: own
  committed data tops 3.77×; the ~3.97× endpoint is COUNTERPARTY-RELAYED.
- `baseline.md` ≈1.26–1.28 ns/mask·bar "walk floor" — UNPINNED (free rayon)
  AND count-only; qualified at every quote site, banned from campaign math
  (DESIGN §9.1).
- The "~14.0–14.5 ms @604k dense" S2 anchor — COUNTERPARTY-RELAYED,
  illustrative only; the S2 gate's binding form is the RATIO vs the
  same-fixture measured kernel sum (DESIGN §9.1 final-lock definition).
- `reads/rust.md` §6 sibling-crate (`rust/sweep-core`) placement note —
  SUPERSEDED by the locked in-crate design (DESIGN §1.1: a NEW `sweep_core`
  module inside `rust/fno-math`).
- Pack v4 (2026-07-09) ADDS DESIGN_FINAL's "Slice-1 substrate + ordering pins (lever-discovery fold, 2026-07-09)" section — the LEX-vs-colex emission-order pin + LEX-RANK unranking mandate, the condition-major bitplane substrate, the prefix-AND LEX-DFS pruning constraints (item (c) marked UNVETTED design input), and the four streaming residual pins; cross-referenced from §6 Slice 1 and §5's enumeration burn-in bullet.
- Pack v5 (2026-07-09) FOLDS Verifier B's checkpoint-1 audit mandatory items (resolution log at the end of DESIGN_FINAL): the Slice-0 ORDER-SENSITIVITY differential gate (C23, §6); the §12 MASK-SERIALIZER LAW re-scope + corpus-(e) golden re-spec (C3+C9 — shipping bare-int digest bytes FROZEN, hex only on NEW surfaces); the §3 kill−9 store-preload correction (C5 — the append-conflict tripwire is the designed catch, not "cannot trip"); the panic-recovery re-route off #1295's corruption counter onto the real quarantine/GAVE_UP mechanisms (C6); the §12.1 identity-protocol laws + the §B.4 verbatim Q&A with the answer-(a) premise repo-corrected (C1); the §12.2 env-flag census (C2); the §3.1 audit-chain law (C42 — SHA-256 chain vs blake3 identity, buffer-and-replay, allow_nan=False); the burn-in three-way mismatch precedence (C32); the enum-shadow ranges-cap re-bound 64→256 (C51); SPEC §4 rows 2/9/13 rewritten to the locked architecture (C31); the SPEC §5 blended-formula WITHDRAWN banner (C48); and the NEW SPEC §10 LAUNCH-SURFACE section (C4 + the launch-lane scope sync — SLA recalibration against the ONE common estimate model, the dashboard dropped-return open item routed to the coordinator, the daemon lane named, F6/F24/F25/F28 pasted verbatim with the PR #1336 F6 register update, and the per-arm throughput-calibration deliverable named DISTINCT from the stall-detection floor).
- Pack v6 (2026-07-10) FOLDS the Verifier B v5 re-audit items: SPEC §4 row 15 rewritten to the RE-SCOPED §12 MASK-SERIALIZER LAW (hex mandatory on NEW surfaces only; the SHIPPING bare-int `combo_digest`/WF-hash bytes FROZEN per corpus (e) — the row's pre-re-scope hex-everywhere wording carries a since-corrected note); the "#1546 (ResidentSweepSimU128)" cite corrected to MIGRATION.md #1549 (DESIGN §12.1 answer-(a) passage; #1546 is DEC-RUN-IDENTITY-GUARD-PARITY-001); the DEC-ID DEFERRAL statement mirrored into the SPEC §8 identity register (per Verifier B's checklist placement); and the "#1295 breaker = G12" identification annotated [independently verified TRUE by Verifier B at 871b3129 — sweep_quarantine.py:84/:113] at both claim sites (the §3 kernel-panic bullet + the C6 resolution-log row).
- Pack v7 (2026-07-10) ADDS the §6 Slice-2 INDEPENDENT-ORACLE MANDATE (Verifier B Slice-0 re-pin, 2026-07-10 — binding Definition-of-Ready item): Slice-2's Rust-arm differential must verify against a GENUINELY independent oracle (frozen-bytes golden corpus or hand-rolled reference implementation), never one importing leaf helpers from live compute.py — recorded counterexample: the Pébay-m3 delta-sign mutant passed Slice-0's C23 differential 4/4 (oracle shared `_accumulate_moments`), killed only by the exact-value pins; sits alongside the TradeFolds §1.3 pairing obligation (#1685 item (3)).

## PR ↔ MIGRATION crosswalk (stub)

| PR | Branch | Content | MIGRATION.md |
|---|---|---|---|
| #1381 | `claude/rust-sweep-core` | Checkpoint-1 design/audit artifact pack (THIS directory; documentation-only) | Ledger entries PENDING — assigned by the implementation slices (Slice 0..4 land with their own DEC ids + MIGRATION.md entries; none exist yet) |
| #1336 (external dependency — not this pack's) | `claude/dedup-c1-tuple-digest-identity` @ ec19300f | The F6 fix (gap-register #3): gate/overfit/stage3 config joined to all three identity tiers via ONE `sweep_identity._gate_config_fragment` (product-audit-1 lane) — UNMERGED; the conversion MUST build on post-#1336 identity semantics (SPEC §10.5 F6) | DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001 (its own PR's ledger entry) |

## Headline honesty (binding — see DESIGN §9.1)

- **Bit-identity ship conditions:** float-layer ownership by CONSTRUCTION
  (the entire #1186 libm / None / boundary / floor-div class stays in
  today's Python `combine_metrics_from_folds`); trades NEVER cross the
  FFI; the zero-trade §1.3 short-circuit law is byte-observable via the
  in-band `scans_run` count; per-slice golden proof + the 64-mask
  dual-oracle burn-in + the S2 kernel-ratio gate (≤ ~1.15× of the
  same-fixture kernel sum, kernel+seam scope only).
- **Honest perf band:** the BINDING two-term cost law is
  `cost(mask) = c0 + ≈6.2 ns/mask·bar (walk) + ≈4.1 µs/trade` (pinned
  production config, `RAYON_NUM_THREADS=1`). Campaign band from the FFI
  port ALONE: **≈1.3–3.1× shape-dependent** (≈2.3–2.5× at the measured
  FIX-L median density). Day-level k≤6 wall-clock is **NOT** reachable
  from the port alone — the previously-quoted absolute day figures are
  WITHDRAWN (unit conflation) and day-level k≤6 requires the NAMED
  algorithmic levers (DESIGN §10): hit-list-intersection prefilter,
  parallelism topology (in-kernel rayon, measured ≈3.0–3.77× on heavy
  tuples), entry-sparsity exploitation, kept-vs-enumerated work shaping.

## Audience

Independent cross-container audit: Verifier A / Verifier B + the
coordinator. Do not merge this branch on the design pack alone.
