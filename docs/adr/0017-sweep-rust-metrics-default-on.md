# ADR-0017 — Rust sweep-metrics tail ON by default (`SWEEP_RUST_METRICS_DEFAULT = True`)

**Decision ID:** `DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001` (MIGRATION.md entry #1530)
**Date:** 2026-07-06
**Status:** **ACCEPTED** — the flip this ADR owns ships in the same PR. It was
ORIGINALLY authored as the PR's LAST commit (every earlier commit fully green
under the prior default-OFF), but after a container-restart wipe the branch was
RECONSTRUCTED from transcripts as ONE combined commit, so that commit-granular
recoverability does NOT exist in the pushed history (reconciled 2026-07-07,
Golden Rule #1). The substantive rollback paths (§6) are unaffected: the revert
is ONE constant, and `BRUTEX_SWEEP_RUST_METRICS=0` kills per process.
**Author:** engine-scale follow-up (executes a named deferral)
**Executes the deferral of:** `DEC-SWEEP-RUST-METRICS-001` (MIGRATION.md
entry #1186), whose flag section states verbatim: *"the flip to ON-by-default
is a SEPARATE later ADR, explicitly NOT here"* — this is that ADR + that flip.
**Builds on:** `DEC-SWEEP-RUST-METRICS-001` (#1186 — the fused
`fno_math.sweep_drawdown_stats` kernel + its 2⁵² f64-exactness envelope proof
+ the zero-trade O(1) shortcut), `DEC-SWEEP-EXIT-INDEX-FASTPATH-001` (#1529 —
the exit-index fast path that removes the ON arm's residual per-mask
O(N_bars) dict build, plus the burn-in self-check + autorevert latch this
flip rides on), `DEC-SWEEP-RUST-TRADESIM-WIRE-001` (#1182 — the Phase-A
fallback law the seam obeys), ADR-0016 (the sibling default-flip precedent:
one constant, goldens unmodified as the correctness net).

---

## 1. Context

`DEC-SWEEP-RUST-METRICS-001` (#1186) ported the sweep's measured-hot per-mask
trades→metrics tail (equity fold + `_drawdown_stats` drawdown scan) into the
fno-math kernel, FLAG-GATED default OFF behind `BRUTEX_SWEEP_RUST_METRICS`,
and proved bit-identity to the Python oracle inside the 2⁵² exactness
envelope (dual-arm golden byte-equality on the frozen
`_GOLDEN_CONTENT_HASH`, cross-arm full-`ResultMetrics` `struct.pack`
bit-equality under hypothesis, 18 Rust-native tests). Measured (sandbox
ratios, NOT the §8 M4 Pro): ~7.3× on the traded-mask tail, ~1683× on the
zero-trade shortcut. The flip to ON was explicitly deferred to a separate
ADR. `DEC-SWEEP-EXIT-INDEX-FASTPATH-001` (#1529, landing in the same
reconstructed commit as this flip — see Status above)
then removed the ON arm's last honest-envelope residual — the per-mask
O(N_bars) `exit_ts`→index dict build — and added the burn-in self-check +
autorevert latch, the runtime safety net a default-ON kernel needs.

## 2. Decision

`brutex/engine/rust_metrics.py::SWEEP_RUST_METRICS_DEFAULT` flips
`False → True`. Flag resolution (per-call):

1. **`_TRUTHY` value (`1/true/yes/on`) → ON** (unchanged).
2. **`_FALSY` value (`0/false/no/off`) → OFF** — the per-process operator
   kill switch, honoured regardless of the default (unchanged).
3. **Unset / whitespace-empty → `SWEEP_RUST_METRICS_DEFAULT`** = now
   **ON**: the fused Rust drawdown kernel serves the per-mask metrics tail
   by default, for EVERY consumer of the COMMON `metrics_from_trades` seam
   (sweep, walk-forward, validation, Stage 3) together.
4. **Any OTHER (unrecognised) value → OFF, with one warn-once
   `flag_unrecognised` fallback event** naming the value: the fail-safe is
   OFF — under a default-ON kernel a typo'd kill switch (`flase`, `0ff`)
   must still KILL, never be silently ignored. The `_FALSY` vocabulary is
   the RECOGNISED (silent) OFF; a garbage value's OFF is WARNED.

Revert = this ONE constant (plus the handful of default-arm test pins that
assert it — mechanical). No other code changes with the flip.

## 3. Why the flip is safe NOW (the licence)

* **Bit-identity is proven, not assumed:** the frozen `_GOLDEN_CONTENT_HASH`
  / `_GOLDEN_RUN_ID` literals are UNCHANGED by this PR, and the new
  default-arm golden (`test_golden_default_arm_engages_the_kernel_and_
  reproduces_the_hash`) asserts the NO-ENV sweep reproduces them
  byte-for-byte WITH exactly one `sweep_rust_metrics.engaged` event (the
  anti-vacuous-gate signal — a stale wheel silently serving Python cannot
  pass as parity).
* **Burn-in self-check (#1529):** the first `SELF_CHECK_SAMPLES = 16`
  kernel-served masks per process are ALSO computed by the Python oracle and
  bit-compared (`struct.pack` on the floats — signed zeros included). A
  mismatch ships the ORACLE value (a wrong kernel number is never persisted,
  not even the one exposing the bug), latches the kernel OFF for the process
  (`sweep_rust_metrics.kernel_autoreverted`, ERROR), and every later call
  short-circuits to Python — pinned end-to-end by the burn-in-poison golden
  (poisoned kernel → pinned bytes + one autorevert event).
* **Every fallback stays graceful + loud:** envelope exit, wheel
  missing/stale, kernel error, refused domain-invalid result, autorevert
  latch — each serves the mask from the bit-exact Python oracle with a
  warn-once event (the #1182 fallback law, unchanged).
* **The kill switch is explicit:** `BRUTEX_SWEEP_RUST_METRICS=0` restores the
  pure-Python tail per process, byte-identically (the OFF-cost bench now
  pins the "0" arm).

## 4. Scope — what this ADR does NOT change

* **Resolution default only.** The kernel, the envelope guard, the exit-index
  fast path, the self-check mechanics, the Python oracle, and every error
  contract are byte-untouched by the flip commit.
* ENGINE SCOPE UNCHANGED (NIFTY/BANKNIFTY/SENSEX + the ADR-0012
  NIFTY-futures track); no vocab / rate / lake / schema / audit change.
* Run identity is untouched: the drawdown values are bit-identical across
  arms, so `blake3` run hashes and store bytes cannot move (golden-pinned).

## 5. Honest envelope (Golden Rule #10)

* A CONSTANT-FACTOR win — the per-tuple tail stays O(kept · N_bars); the §8
  production-sweep SLA is STILL NOT claimed met by this flip.
* The burn-in self-check costs one extra O(N_bars) oracle scan for the first
  16 kernel-served masks per process (and per `sweep_parallel` worker) —
  bounded, amortised nil; the speedup/fast-path benches measure the steady
  state past it, explicitly.
* The self-check can only catch a wrongness that MANIFESTS within the
  burn-in window's inputs; a kernel bug conditional on later-only input
  shapes is (as before) the golden/property suites' layer. The burn-in is
  defence-in-depth ON TOP of the #1186 proof, not a substitute for it.
* An unrecognised env value fails SAFE to OFF (one `flag_unrecognised`
  warn) — the kill-switch direction: an operator reaching for
  `BRUTEX_SWEEP_RUST_METRICS=0` under pressure and typo'ing it (`flase`)
  still gets the kernel OFF, loudly, because the self-check/autorevert
  layers protect only against kernel WRONGNESS, not against an ignored
  operator intent (e.g. the `panic=abort` allocation-failure class no
  runtime net can catch). The `_FALSY` vocabulary remains the recognised
  silent OFF; garbage OFF is warned. The flip itself stays one-constant
  (only unset/empty follows the default).
* Sandbox speedup ratios only; no vendor claims; live Groww untouched.

## 6. Rollback

Set `SWEEP_RUST_METRICS_DEFAULT = False` (one constant) and mechanically
re-invert the default-arm test pins; or, operationally without a deploy,
export `BRUTEX_SWEEP_RUST_METRICS=0` (per process). The autorevert latch
additionally self-rolls-back a proven-wrong kernel within a running process
with no operator action.
