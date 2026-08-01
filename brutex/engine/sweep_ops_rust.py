"""sweep_ops_rust — the default-ON Rust routing facade for the sweep-ops PURE laws.

DEC-RUST-SWEEP-OPS-001 (slice 0 facade + slice 1 laws; default flipped ON
by DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23). Routes
converted sweep run-core/ops PURE laws through the ``rust/brutex-sweep-ops``
crate behind the ``BRUTEX_SWEEP_OPS_RUST`` flag, with the verbatim
Python law retained as the LIVE parity oracle (the
``brutex/data/window_ledger_rust.py`` template — itself the ADR-0020 / PR-1
``lake_rust_render`` shape).

Slice-1 routed laws (each wired at its owning module, which gates on
:func:`sweep_ops_rust_enabled` FIRST so the flag-OFF default path is
byte-identical including attribute-access order):

1. **Path-join law** (:func:`routed_run_path`) —
   ``brutex/engine/run_layout.py``'s ``run_dir`` + both ``mirror_run_dir``
   joins, via the ONE crate ``join_path_segments``. Only strings cross the
   FFI; the facade re-wraps the returned string in ``Path(...)`` (whose
   constructor normalisation makes the result object-equal to the
   chained-``/`` oracle for every accepted shape — burn-in-verified).
2. **Gate-config fragment law** (:func:`routed_gate_fragment`) —
   ``brutex/engine/sweep_identity.py``'s ``_gate_config_fragment`` emission
   law. Values cross as f64/i64 (bit-exact roundtrip — the greeks_batch
   precedent); EXACT-type gates (``float``/``int``, ``bool`` excluded)
   refuse anything lossy to the oracle. This law feeds RUN IDENTITY
   (``grid_identity`` / ``tuple_cache_key`` / ``_tuple_digest``) — the
   burn-in + the exact-type gates are the anti-drift teeth. The crate
   carries ALL FOUR legs (overfit / ``max_sl_hit_rate`` / ``cvar`` /
   stage3) since DEC-RUST-SWEEP-OPS-GATE-FRAGMENT-WIDEN-001, which
   RETIRED the old ``gate_fragment_beyond_crate`` eligibility gate: the
   Door-6 ceiling (DEC-METRICS-LOSSRATE-FIRSTCLASS-001) and the CVaR
   tail gate (DEC-RANK-CVAR-TAIL-GATE-001) used to bounce the WHOLE
   computation back to the oracle; both are now computed in Rust.
3. **Scaled stall-timeout law** (:func:`routed_stall_timeout`) —
   ``brutex/engine/sweep_stall_policy.py``'s raise-only scaled tail. The
   fail-loud env parses + ``StallPolicy`` construction stay Python.

Slice-1b routed laws (the deferred leaf batch — same wiring shape; each
rides the shared :func:`_routed_law_call` engage core, a documented
slice-1b consolidation so six near-identical resolve→call→degrade→validate→
burn-in blocks are ONE copy, CLAUDE.md §11; the three slice-1 laws above
keep their original longhand bodies untouched — unifying them onto the
helper is final-slice housekeeping):

4. **Progress derived-metrics law** (:func:`routed_progress_derived`) —
   ``brutex/engine/sweep_progress.py``'s five snapshot derived properties
   as ONE ``(percent, elapsed, rate, eta, survivor_rate)`` tuple. Counts
   are gated ``|v| ≤ 2**53`` (:data:`_MAX_EXACT_COUNT`) so CPython's
   correctly-rounded ``int / int`` true division and the crate's ``as f64``
   conversions are provably identical; the ``round(x, 1)`` half-even law is
   crate-mirrored (burn-in + the real-wheel hypothesis differential are the
   teeth). The lock/state machine stays Python.
5. **SLA projection-decision law** (:func:`routed_sla_decision`) —
   ``brutex/engine/sweep_sla_guard.py``'s ``projected = tuple_count ×
   per_tuple_seconds`` → allowed/acknowledged/refused. Env parses,
   fail-loud validation, message rendering + the structlog event stay
   Python.
6. **Rank-memory plan-decision law** (:func:`routed_rank_plan`) —
   ``brutex/engine/sweep_mem_budget.py``'s ``plan_rank_memory`` decision
   table (exact-int projection + verdict token). The mode-``on`` config
   raises + refusal message rendering stay Python.
7. **Preflight zero-bar fold** (:func:`routed_preflight_missing`) +
   **derived-freshness stale fold** (:func:`routed_freshness_stale`) —
   ``brutex/engine/sweep_preflight.py``'s two pure folds. Remediation
   rendering stays Python.
8. **Liveness verdict tail** (:func:`routed_liveness_tail`) —
   ``brutex/engine/sweep_liveness.py``'s ``progress_verdict`` decision tail
   AFTER the payload typing + the ``os.kill`` pid probe (both stay Python —
   the fail-closed UNKNOWN arm IS Python-type introspection).

Slice-2 routed laws (the AUDIT-CHAIN family — same wiring shape, same
shared engage core). Byte-identity is LOAD-BEARING here (the sweep audit
JSONL hash chain is a CI gate — CLAUDE.md Golden Rule #8), so the safety
rails are explicit: Python renders EVERY hashed field string (``str(seq)``,
the float-repr ``str(ts_utc)``, the canonical payload JSON — JSON is NEVER
re-rendered in Rust, the float-repr trap) and Rust only joins + hashes /
decides; the ORCHESTRATOR SHA-256 chain (``brutex/orchestrator/audit.py``)
is the DISTINCT family and has NO seam here — never conflated:

9.  **Audit entry-hash law** (:func:`routed_audit_entry_hash`) —
    ``brutex/engine/sweep_audit.py``'s ``SweepAuditEntry.compute_hash``
    BLAKE3 over the ``|``-joined 7 pre-rendered fields (the crate uses the
    official ``blake3`` crate — the same algorithm the pure-pip ``blake3``
    package provides the oracle).
10. **Chain verify step** (:func:`routed_chain_verify_step`) —
    ``verify_chain``'s per-entry decision (seq → prev-hash linkage →
    recomputed content hash; first failure wins) as a
    ``(token, expected, actual)`` triple; Python owns the
    ``SweepAuditChainCorruption`` construction + message bytes.
11. **Stamp verdict law** (:func:`routed_stamp_verdict`) —
    ``brutex/observability/stage_stamp.py``'s ``validate_stage_stamp``
    mismatch decision (content hash / stage / run-id / the G7
    both-sides-assert snapshot law). The artifact HASHING stays Python
    (streamed ``hashlib`` — the bounded-memory law); the
    MISSING/MALFORMED verdicts are I/O-derived and decided Python-side.
12. **Chain-link state ladder** (:func:`routed_chain_link_state`) —
    ``brutex/observability/chain_map.py``'s ``_link_state`` per-link
    priority rules 1-5 (the suspect amber floor is a one-line transform
    that stays Python in ``build_chain_map``).
13. **Count-reconciliation fold** (:func:`routed_count_violations`) —
    ``brutex/observability/run_tripwires.py``'s ``_reconcile_counts`` D-9
    violations detail (EMPTY string = clean; ints only — no float repr
    crosses). The tripwire engine's fail-soft law (#1712 I-3) holds by
    construction: every seam degrades to the oracle, never a new raise.

Slice-3 routed laws (the DURABLE-ARTIFACTS family — same wiring shape,
same shared engage core). The hard rail: NO write, fsync, rename or lock
EVER crosses the FFI — the durability / crash-convergence ORDERING of
these modules is #1712-hostile-reviewed and stays Python verbatim; ONLY
pure decisions route, and RENDERING (``json.dumps`` float repr) stays
Python (the float-repr trap):

14. **Exit-status resolution law** (:func:`routed_exit_status_resolution`)
    — ``brutex/engine/sweep_exit_status.py``'s wall-clock preservation +
    suspect resolution (the #1712 round-3 ``wall_started_utc``
    preservation fix). Floats cross as f64 BIT-exactly both ways; the
    prior-artifact READ, the doc build and the atomic write stay Python.
15. **Quarantine ordinal fold** (:func:`routed_quarantine_next_ordinal`) —
    ``brutex/engine/sweep_quarantine.py``'s monotonic dirname-ordinal fold
    over ALREADY-LISTED entry names (``iterdir`` stays Python). Gated
    ASCII-only WITHOUT embedded newlines: Python's ``int()`` accepts
    Unicode digits and ``re``'s ``$`` matches before a trailing newline —
    both shapes are refused to the oracle rather than mirrored.
16. **Checkpoint record decision** (:func:`routed_checkpoint_record_decision`)
    + **adoption verdict** (:func:`routed_checkpoint_adoption_verdict`) —
    ``brutex/engine/sweep_checkpoint.py``'s monotonic high-water advance
    decision (the never-rewind law) and the load-tail adoption verdict
    (foreign-id → ignore; same-id total mismatch → the tamper refusal).
    The JSON parse + ``int()``/``float()`` coercions of ``from_dict``, the
    corruption-class → ``SweepCheckpointError`` mapping, every message
    byte and the atomic save stay Python (marshaling-heavy by design).

Slice-3 N/A disposition: ``sweep_artifact_guard`` stays Python until the
endgame — its ONLY pure decision is a one-line set membership
(``_is_legacy_schema_keyerror``) whose column set is single-sourced from
``results_store``'s field tuples by the module's OWN §11 anti-copy law; a
crate-side copy would mint a second source, and marshaling ~50 column
names per exception call is overhead, not conversion.

Slice-4 routed laws (the PROCESS/LOCK-OPS family — same wiring shape,
same shared engage core). The hard rail: NOTHING OS-shaped crosses the
FFI — flock/OFD composition, the fork/``start_new_session`` spawn + the
``BRUTEX_SWEEP_DETACHED`` env-sentinel stamp, every heal REFUSAL message
+ the destructive ``--execute`` quarantine MOVE, and Telegram MarkdownV2
rendering all stay Python verbatim; ONLY pure decision folds route:

17. **Detach probe verdict** (:func:`routed_detach_probe_verdict`) —
    ``brutex/engine/sweep_detach.py``'s ``probe_handle`` cross-check fold
    (R7). The liveness verdict is an INPUT token (``progress_verdict``'s
    output — its own tail is law 8; nothing is double-routed) and the
    ``pids_match`` equality over the raw JSON pid stays Python.
18. **Heal mismatch decision** (:func:`routed_heal_mismatch`) —
    ``brutex/engine/sweep_heal.py``'s ``assess_heal`` MISMATCH TAIL over
    the three ``Option[sha256]`` triples in ``ARTIFACT_SUFFIXES`` order.
    The happy arm (``src == plan`` → ``move_trio``) stays Python so dest
    hashing remains LAZY; ``refuse`` keys the Python-owned C2 message.
19. **Schedule next-fire arithmetic** (:func:`routed_schedule_next_fire`)
    — ``brutex/engine/sweep_scheduler.py``'s clock-injected interval +
    daily arms, gated to the exactly-representable f64 integer range; the
    OUTPUT gate reconstructs the daily arm's ``int`` return TYPE so the
    daemon's ``json.dumps`` audit bytes stay identical either arm.
    ``Schedule.parse`` stays Python (regex marshal-land — Unicode ``\\d``
    + ``$``-before-newline are Python semantics, not conversion targets).
20. **Resume decision** (:func:`routed_resume_decision`) —
    ``sweep_scheduler.decide_resume``'s FRESH / RESUME / ALREADY_COMPLETE
    classification as a 4-way token + index (the two FRESH arms are
    distinct tokens); the REASON strings render Python-side byte-exactly.

Slice-4 N/A dispositions (honest, not padding): ``sweep_singleflight``
stays Python whole — the G6/G10 laws ARE flock/OFD composition (OS-land;
the only "decision" is a trivial best-effort holder-pid parse), and
``sweep_alerts`` stays Python whole — MarkdownV2 escaping is a BYTE-LAW
(never re-rendered in Rust) and the crash-loop ``>= threshold``
comparison is a one-line trivial fold.

Slice-5 routed laws (the VERIFY/APRIORI family — the hard rail: the
RUN-IDENTITY boundary seams ``sweep_run.canonical_trade_params`` /
``mask_run_id`` are NEVER converted or re-rendered; every finding /
refusal MESSAGE byte, the audit reads, the Parquet loads and the sha256
hashing stay Python — verify verdicts feed heal quarantine plans):

21. **Recount verdict tail** (:func:`routed_recount_verdict`) —
    ``brutex/engine/sweep_verify.py``'s ``compare_recount``
    post-byte-diff classification ladder (B1/B2/B5 + the VIX-residual
    family split). The VIX attribution (``ResultsStore`` row loads +
    ``replace()``/``getattr``/``dataclasses.fields`` introspection) stays
    Python and crosses as ONE boolean; the token keys the Python-owned
    finding codes/details and the ``grid_corrupt_leaderboards`` side
    effect.
22. **Apriori join + subset-prune** (:func:`routed_apriori_join_prune`) —
    ``brutex/engine/sweep_apriori.py``'s ``F_{j-1} ⋈ F_{j-1}`` level fold
    (the module's own "deterministic, pure integer, bit-identical
    Python↔Rust" law), with the strided mask·bar budget trip returned as
    a VALUE (a trip is a semantic outcome, never degraded away; the
    ``SweepAprioriBudgetError`` message bytes stay Python). Conflict
    pairs cross pre-packed; the k==0/1 arms, the hit COUNTER (the
    resident kernel / ``count_supermasks``), the census logging and the
    env knobs stay Python.

Slice-5 N/A disposition (honest, not padding): ``sweep_diffcheck`` stays
Python WHOLE — its reference simulator IS the R45 independence artifact
(re-hosting it in the shared conversion crate would drain the never-adopt
law's value; its AST-pinned import ban passes UNMODIFIED, untouched),
``sample_masks`` is CPython-MT19937 ``random.Random.sample``
semantics-land (un-mirrorable without re-implementing CPython's RNG — the
marshaling-trap class) and ``drawdown_bit_key`` is byte RENDERING
(``struct.pack``). ``sweep_verify``'s remaining surfaces (audit-fact
reads, roll-schedule refusals, the structural/identity tiers) are
I/O + run-identity seams and stay Python.

Slice-6 routed laws (the ORCHESTRATION-CORE family — the hard rail: the
#1712 unkillable-run-core semantics are preserved EXACTLY; the
``sweep_run_core`` context manager, its flock/keep-awake/flight-recorder
/G12 crash-loop-breaker composition, every durable write, and the
run-identity-class ``grid_identity`` / ``_tuple_digest`` /
``_selection_identity_fragment`` seams NEVER cross the FFI):

23. **TUPLE_DONE decimation stride** (:func:`routed_record_stride`) —
    ``brutex/engine/sweep_run_core.py``'s D-9 ``tuple_record_stride``
    fold. The Python oracle's ``math.ceil(t / 10_000)`` is TRUE (f64)
    division, so the crate mirrors it in f64 and the input gate holds
    the operand to the exactly-representable |v| <= 2^50 band (the
    slice-4 scheduler band law) where IEEE division + ceil are
    bit-identical cross-language.
24. **Per-tuple diffcheck budget** (:func:`routed_diffcheck_budget`) —
    ``sweep_run_core``'s R45 K-distribution fold (design D-11):
    positive ``//`` positive floor division ≡ Rust integer division
    inside the gated band.
25. **Grid tuple count** (:func:`routed_grid_tuple_count`) —
    ``brutex/engine/sweep_orchestrator.py``'s ``count_sweep_tuples``
    outer-product fold over PRE-EXTRACTED axis cardinalities (Python
    owns the ``SweepGridConfig`` object marshaling); the crate
    accumulates in checked u128 and a product past u64 degrades to the
    Python bigint oracle.

Slice-6 N/A dispositions (honest, not padding — the mostly-N/A outcome
is the expected shape of this family): ``sweep_run_core``'s remainder is
CM/thread/OS/durable-I/O land (``_finalize`` / ``mirror_run_forensics``
/ ``_append_run_manifest_line`` / ``_is_halt``) and the manifest-content
``_selection_fragment``/``_conditions_hex`` builders stay Python (their
bytes feed the RUN_MANIFEST audit action — the byte-law rail);
``sweep_orchestrator``'s ``_tuple_digest`` + ``_selection_identity_
fragment`` are the #1715 gate-identity digest (byte pin
``5b020e892247deac``) — run-identity-class, NEVER converted — and
``iter_sweep_tuples``/``run_one_tuple``/``run_sweep_grid``/
``bloom_capacity_hint`` are object/I/O land (the hint composes
``combo_mask.count_combos`` — routing it would mint a SECOND binomial
source, the ``sweep_artifact_guard`` anti-copy precedent);
``sweep_driver`` stays Python WHOLE (metrics/simulation land whose Rust
story ALREADY exists — the fno_math kernels + ``rust_metrics`` + the
#1531/#1532 burn-in oracle pair; it sits on the diffcheck AST BAN list
and ``_walk_forward_run_hash`` is RUN IDENTITY). Remainder census, all
honest N/A: ``sweep_run`` (the carved-out run-identity home) ·
``sweep_cache`` (Bloom+LMDB I/O; the #1714 auto-grow fold composes the
bloom sizing helper + the RAM wall + refusal message bytes — inseparable
without splitting a hostile-reviewed law) · ``sweep_cancel`` (sidecar
I/O) · ``sweep_kernel`` (resident fno_math kernel dispatch — existing
Rust story) · ``sweep_launch`` (subprocess/env) · ``sweep_lookahead``
(the guard's OFF-path identity contract is load-bearing) ·
``sweep_parallel`` (spawn pools/threads; its stall budget ALREADY rides
the routed slice-1 law via ``resolve_stall_policy`` — never
double-routed) · ``sweep_report`` (rendering) · ``sweep_stream``
(columnar object marshaling; rank order is byte-order-pinned vs
``ResultsStore.top_k``) · ``sweep_supervisor`` (process orchestration;
its BLAKE3 chain is DISTINCT from ``sweep_audit``'s — no third chain
implementation, the ``flight_recorder`` precedent).

## The fail-closed layers (the window_ledger_rust template, adapted)

* **Flag** — ``BRUTEX_SWEEP_OPS_RUST`` default ON (per
  DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23); truthy →
  ON, falsy → OFF (the explicit kill switch), any other non-empty value →
  OFF with ONE loud ``sweep_ops_rust.fallback`` WARN
  (``flag_unrecognised``). Explicit OFF never imports the crate
  (spy-pinned) — the OFF path is byte-identical.
* **Import guard / SYMBOL handshake** — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks any
  :data:`_CRATE_API` callable is a pre-slice-1 (or foreign/broken) install
  and latches ``crate_stale``. Both serve the oracle with one loud WARN.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface), ``TypeError``/``OverflowError`` (PyO3 argument
  conversion; since :data:`_I64_MIN`/:data:`_I64_MAX` every ``i64``-typed
  argument is band-gated BEFORE the call, so this arm is now a belt, not a
  routine path), ``AttributeError`` (broken-crate-internals belt).
  ``MemoryError`` is
  DELIBERATELY absent — OOM re-raises raw, identical to the pure-Python
  path; ``KeyboardInterrupt``/``SystemExit`` propagate raw.
* **Refuse-poison gates** — exact-type INPUT gates (a ``Path``/``bool``/
  subclass input never crosses) + structural OUTPUT gates (a non-``str``
  join, a malformed fragment tuple, a malformed timeout pair are REFUSED
  to the oracle — a stale/foreign wheel can only degrade, never serve).
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the Rust value against the live oracle; ANY
  difference ships the ORACLE value and autoreverts the Rust path for the
  process (ERROR log — the #1531/#1532 pattern).

## Panic-belt honesty (``panic = "abort"``; CLAUDE.md Golden Rule #10)

The ``brutex_sweep_ops`` release wheel is built with ``panic = "abort"``
(``rust/brutex-sweep-ops/Cargo.toml`` — the sibling ``brutex-feed``
convention), so a Rust panic in the SHIPPED wheel ABORTS the host process
before any Python handler can run: the ``_panic_belt_errors()`` /
``crate_panic`` arm is therefore a DEV-BUILD (``cargo test`` / any
default-``unwind`` build, where pyo3's ``catch_unwind`` surfaces a
``pyo3_runtime.PanicException``) + defence-in-depth path, NOT a
shipped-wheel rail (this crate carries no ``catch_unwind`` FFI wrapper —
distinct from ``brutex-lake``, which builds WITHOUT ``panic = "abort"`` and
maps panics to ``RuntimeError`` at its own boundary). It is retained, not
removed, because the crate is TOTAL by construction — checked arithmetic
(``overflow-checks = true``), a ``Result`` on every fallible arm, and
exact-type / finite / band-gated inputs — so the practical panic risk is
≈ nil and the belt guards only a doubly-broken future contract (a
non-abort build, or a panic surfacing through an out-of-band pyo3 runtime).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. Every
slice-1/1b law is nanosecond-scale in Python and the FFI hop is a small
SLOWDOWN; acceptable because the seams run per launch / per run-dir
derivation / per progress-snapshot poll (never per mask); DEFAULT-ON per
DEC-RUST-DEFAULT-ON-WAVE-001. The flag is read per call
(one ``os.environ`` probe) so spawn workers resolve their own inherited
environment. ``python_law`` parameters are ZERO-ARG closures (a documented
deviation from window_ledger_rust's arg-forwarding shape — three laws with
three shapes; closures keep the facade uniform).

Import discipline: stdlib + structlog + the TWO §11-COMMON helpers —
``env_flag_tristate`` from :mod:`brutex.engine.sweep_lookahead` (the
engine's flag-vocabulary home; the sweep_diffcheck/sweep_apriori precedent)
and ``_panic_belt_errors`` from :mod:`brutex.data.lake_rust_render` (the
ONE guardedly-resolved PanicException fold; the window_ledger_rust
cross-module-import precedent). Both are import-light, so the wired leaf
modules (run_layout et al.) stay light.
"""

from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

# §11 COMMON — ONE flag vocabulary (the engine's home) + ONE panic belt (the
# brutex-lake facades' guardedly-resolved PanicException fold; underscore =
# module-privacy toward OUTSIDE consumers, the window_ledger_rust precedent).
from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import _panic_belt_errors
from brutex.engine.sweep_lookahead import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; falsy is the
#: explicit kill switch; junk fails loud-OFF).
SWEEP_OPS_RUST_ENV: Final[str] = "BRUTEX_SWEEP_OPS_RUST"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The path-join-law burn-in budget key.
_LAW_RUN_PATH: Final[str] = "run_path"
#: The gate-fragment-law burn-in budget key.
_LAW_GATE_FRAGMENT: Final[str] = "gate_fragment"
#: The stall-timeout-law burn-in budget key.
_LAW_STALL_TIMEOUT: Final[str] = "stall_timeout"
#: The progress-derived-metrics-law burn-in budget key (slice 1b).
_LAW_PROGRESS_DERIVED: Final[str] = "progress_derived"
#: The SLA-projection-decision-law burn-in budget key (slice 1b).
_LAW_SLA_DECISION: Final[str] = "sla_decision"
#: The rank-memory-plan-decision-law burn-in budget key (slice 1b).
_LAW_RANK_PLAN: Final[str] = "rank_plan"
#: The preflight-zero-bar-fold burn-in budget key (slice 1b).
_LAW_PREFLIGHT_MISSING: Final[str] = "preflight_missing"
#: The derived-freshness-stale-fold burn-in budget key (slice 1b).
_LAW_FRESHNESS_STALE: Final[str] = "freshness_stale"
#: The liveness-verdict-tail burn-in budget key (slice 1b).
_LAW_LIVENESS_TAIL: Final[str] = "liveness_tail"
#: The audit-entry-hash-law burn-in budget key (slice 2).
_LAW_AUDIT_ENTRY_HASH: Final[str] = "audit_entry_hash"
#: The chain-verify-step-law burn-in budget key (slice 2).
_LAW_CHAIN_VERIFY_STEP: Final[str] = "chain_verify_step"
#: The stamp-verdict-law burn-in budget key (slice 2).
_LAW_STAMP_VERDICT: Final[str] = "stamp_verdict"
#: The chain-link-state-law burn-in budget key (slice 2).
_LAW_CHAIN_LINK_STATE: Final[str] = "chain_link_state"
#: The count-reconciliation-fold burn-in budget key (slice 2).
_LAW_COUNT_VIOLATIONS: Final[str] = "count_violations"
#: The exit-status-resolution-law burn-in budget key (slice 3).
_LAW_EXIT_STATUS_RESOLUTION: Final[str] = "exit_status_resolution"
#: The quarantine-ordinal-fold burn-in budget key (slice 3).
_LAW_QUARANTINE_ORDINAL: Final[str] = "quarantine_ordinal"
#: The checkpoint-record-decision burn-in budget key (slice 3).
_LAW_CHECKPOINT_RECORD: Final[str] = "checkpoint_record"
#: The checkpoint-adoption-verdict burn-in budget key (slice 3).
_LAW_CHECKPOINT_ADOPTION: Final[str] = "checkpoint_adoption"
#: The detach-probe-verdict-law burn-in budget key (slice 4).
_LAW_DETACH_PROBE: Final[str] = "detach_probe"
#: The heal-mismatch-decision burn-in budget key (slice 4).
_LAW_HEAL_MISMATCH: Final[str] = "heal_mismatch"
#: The schedule-next-fire-law burn-in budget key (slice 4).
_LAW_SCHEDULE_NEXT_FIRE: Final[str] = "schedule_next_fire"
#: The resume-decision-law burn-in budget key (slice 4).
_LAW_RESUME_DECISION: Final[str] = "resume_decision"
#: The recount-verdict-law burn-in budget key (slice 5).
_LAW_RECOUNT_VERDICT: Final[str] = "recount_verdict"
#: The apriori-join-prune-law burn-in budget key (slice 5).
_LAW_APRIORI_JOIN: Final[str] = "apriori_join"
#: The record-stride-law burn-in budget key (slice 6).
_LAW_RECORD_STRIDE: Final[str] = "record_stride"
#: The diffcheck-budget-law burn-in budget key (slice 6).
_LAW_DIFFCHECK_BUDGET: Final[str] = "diffcheck_budget"
#: The grid-tuple-count-law burn-in budget key (slice 6).
_LAW_GRID_TUPLE_COUNT: Final[str] = "grid_tuple_count"

#: Every routed law's burn-in budget key (the reset seam iterates this).
_ALL_LAWS: Final[tuple[str, ...]] = (
    _LAW_RUN_PATH,
    _LAW_GATE_FRAGMENT,
    _LAW_STALL_TIMEOUT,
    _LAW_PROGRESS_DERIVED,
    _LAW_SLA_DECISION,
    _LAW_RANK_PLAN,
    _LAW_PREFLIGHT_MISSING,
    _LAW_FRESHNESS_STALE,
    _LAW_LIVENESS_TAIL,
    _LAW_AUDIT_ENTRY_HASH,
    _LAW_CHAIN_VERIFY_STEP,
    _LAW_STAMP_VERDICT,
    _LAW_CHAIN_LINK_STATE,
    _LAW_COUNT_VIOLATIONS,
    _LAW_EXIT_STATUS_RESOLUTION,
    _LAW_QUARANTINE_ORDINAL,
    _LAW_CHECKPOINT_RECORD,
    _LAW_CHECKPOINT_ADOPTION,
    _LAW_DETACH_PROBE,
    _LAW_HEAL_MISMATCH,
    _LAW_SCHEDULE_NEXT_FIRE,
    _LAW_RESUME_DECISION,
    _LAW_RECOUNT_VERDICT,
    _LAW_APRIORI_JOIN,
    _LAW_RECORD_STRIDE,
    _LAW_DIFFCHECK_BUDGET,
    _LAW_GRID_TUPLE_COUNT,
)

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = dict.fromkeys(_ALL_LAWS, BURN_IN_SAMPLES)

#: The crate callables every slice-1/1b law needs — the SYMBOL handshake
#: surface. A wheel missing any is a pre-slice-1b install (``crate_stale`` —
#: extending this list stales older wheels BY DESIGN; rebuild via maturin).
_CRATE_API: Final[tuple[str, ...]] = (
    "apriori_join_prune",
    "audit_entry_hash",
    "chain_link_state",
    "chain_verify_step",
    "checkpoint_adoption_verdict",
    "checkpoint_record_decision",
    "count_reconciliation",
    "detach_probe_verdict",
    "exit_status_resolution",
    "freshness_stale",
    "gate_config_fragment",
    "grid_tuple_count",
    "heal_mismatch_decision",
    "join_path_segments",
    "liveness_tail",
    "per_tuple_diffcheck_budget",
    "preflight_missing",
    "progress_derived",
    "quarantine_next_ordinal",
    "rank_plan_decision",
    "recount_verdict",
    "resume_decision",
    "schedule_next_fire",
    "sla_decision",
    "stall_scaled_timeout",
    "stamp_verdict",
    "tuple_record_stride",
)

#: Structural arities of the crate's fragment legs + stall pair (the frozen
#: FFI shapes — refuse-poison gates below).
_OVERFIT_LEG_ARITY: Final[int] = 4
_STAGE3_LEG_ARITY: Final[int] = 3
_CVAR_LEG_ARITY: Final[int] = 2
_STALL_PAIR_ARITY: Final[int] = 2

#: Structural shapes of the slice-1b crate results (refuse-poison gates).
_DERIVED_ARITY: Final[int] = 5
_DECISION_PAIR_ARITY: Final[int] = 2
_STRING_PAIR_ARITY: Final[int] = 2

#: The exact-int band ``|count| ≤ 2**53``: within it CPython's
#: correctly-rounded ``int / int`` true division equals the crate's
#: ``as f64``-then-divide (both operands convert EXACTLY), and every
#: ``int - int`` stays a lossless ``i64``. A count outside the band is
#: refused to the oracle (CPython's arbitrary-precision division could
#: legitimately differ from double division there).
_MAX_EXACT_COUNT: Final[int] = 2**53

#: The SLA decision wire tokens (``SlaDecision`` values — drift-pinned by
#: the facade test suite; the facade itself must NOT import sweep_sla_guard,
#: which imports THIS module).
_SLA_DECISION_TOKENS: Final[frozenset[str]] = frozenset({"allowed", "acknowledged", "refused"})

#: The bounded-rank mode wire tokens (``BoundedRankMode`` values —
#: drift-pinned by the facade test suite; no import, same cycle law).
_RANK_MODE_TOKENS: Final[frozenset[str]] = frozenset({"off", "auto", "on"})

#: The rank-memory plan verdict tokens (the crate ↔ wiring contract).
_RANK_PLAN_VERDICTS: Final[frozenset[str]] = frozenset(
    {"batch", "bounded", "refuse_off", "refuse_no_top_k", "refuse_no_digest"}
)

#: The liveness verdict-tail wire tokens (``RunVerdict`` values minus the
#: Python-owned fail-closed ``unknown`` — drift-pinned by the facade test
#: suite; no import, same cycle law).
_LIVENESS_TAIL_TOKENS: Final[frozenset[str]] = frozenset({"running", "stale", "not_running"})

#: A 64-hex-lowercase BLAKE3 digest's character set (output gate — slice 2).
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")
#: A BLAKE3-256 hex digest's length (mirrors ``sweep_audit.BLAKE3_HEX_LEN``
#: — drift-pinned by the facade suite; no import, same cycle law:
#: ``sweep_audit`` imports THIS module).
_HASH_HEX_LEN: Final[int] = 64
#: The chain-verify-step wire tokens (the crate ↔ wiring contract; the
#: non-``ok`` members key ``sweep_audit``'s reason map — drift-pinned).
_CHAIN_VERIFY_TOKENS: Final[frozenset[str]] = frozenset(
    {"ok", "seq_out_of_order", "prev_hash_mismatch", "entry_hash_mismatch"}
)
#: The verify-step triple's arity (refuse-poison gate — slice 2).
_VERIFY_TRIPLE_ARITY: Final[int] = 3
#: The stamp-verdict RESULT tokens (the crate ↔ wiring contract).
_STAMP_VERDICT_RESULTS: Final[frozenset[str]] = frozenset({"ok", "hash_mismatch"})
#: The stamp-verdict INPUT tokens (``StampVerdict`` values — drift-pinned by
#: the facade suite; no import, ``stage_stamp`` imports THIS module).
_STAMP_VERDICT_TOKENS: Final[frozenset[str]] = frozenset(
    {"ok", "missing_artifact", "missing_stamp", "hash_mismatch", "malformed"}
)
#: The five chain links (``chain_map.LINKS`` — drift-pinned; no import,
#: ``chain_map`` imports THIS module) — the ``last_stage`` input gate.
_CHAIN_LINKS: Final[tuple[str, ...]] = ("preflight", "sweep", "stage3", "cascade", "artifacts")
#: The chain-link state tokens (``chain_map.LinkState`` values —
#: drift-pinned, same cycle law).
_LINK_STATE_TOKENS: Final[frozenset[str]] = frozenset(
    {"pending", "running", "green", "amber", "red"}
)
#: The exit-status run-state wire tokens (``sweep_exit_status.RunState``
#: values — drift-pinned by the facade suite; no import,
#: ``sweep_exit_status`` imports THIS module) — the state input gate.
_RUN_STATE_TOKENS: Final[frozenset[str]] = frozenset(
    {"running", "complete", "failed", "refused", "halted", "suspect_complete"}
)
#: The exit-status resolution triple's arity (refuse-poison gate — slice 3).
_RESOLUTION_TRIPLE_ARITY: Final[int] = 3
#: The checkpoint record-decision tokens (the crate ↔ wiring contract; the
#: non-advance members key ``sweep_checkpoint``'s raise/noop arms).
_RECORD_DECISION_TOKENS: Final[frozenset[str]] = frozenset(
    {"reject_below_zero", "reject_beyond_total", "noop", "advance"}
)
#: The checkpoint adoption-verdict tokens (the crate ↔ wiring contract).
_ADOPTION_TOKENS: Final[frozenset[str]] = frozenset({"adopt", "foreign", "total_mismatch"})
#: The FULL ``RunVerdict`` wire tokens (``sweep_liveness.RunVerdict`` values —
#: drift-pinned by the facade suite; distinct from the 3-member
#: :data:`_LIVENESS_TAIL_TOKENS`, which the tail law can never emit
#: ``unknown`` from) — the detach probe verdict vocabulary (slice 4).
_RUN_VERDICT_TOKENS: Final[frozenset[str]] = frozenset(
    {"running", "stale", "not_running", "unknown"}
)
#: The heal mismatch-tail decision tokens (the crate ↔ wiring contract; the
#: ``refuse`` member keys ``sweep_heal``'s Python-owned C2 refusal arm).
_HEAL_MISMATCH_TOKENS: Final[frozenset[str]] = frozenset(
    {"already_quarantined", "complete_partial", "refuse"}
)
#: Artifact slots per condemned heal tuple (``sweep_heal.ARTIFACT_SUFFIXES``
#: arity — drift-pinned; no import, ``sweep_heal`` imports THIS module).
_HEAL_ARTIFACT_SLOTS: Final[int] = 3
#: The resume-decision tokens (the crate ↔ wiring contract; the two FRESH
#: arms are DISTINCT so the Python-side reason strings stay byte-exact).
_RESUME_TOKENS: Final[frozenset[str]] = frozenset(
    {"fresh_absent", "fresh_foreign", "already_complete", "resume"}
)
#: The exactly-representable f64 integer bound for scheduler operands. With
#: every input |value| ≤ 2^50, the daily arm's WIDEST intermediate — the
#: 3-term sum ``today_fire = day_start + second_of_day − tz`` — provably
#: stays an EXACTLY-representable integer: ``day_start`` is bounded by
#: ``|now| + |tz| ≤ 2·2^50`` (it is a multiple of ``SECONDS_PER_DAY``, itself
#: exact), so ``|today_fire| ≤ 2·2^50 + 2^50 + 2^50 = 2^52``, and the two
#: day-rollover ``+ SECONDS_PER_DAY`` steps keep every result under 2^53
#: (spacing 1) — hence the crate's f64 arithmetic is value-identical to the
#: Python oracle's exact INT arithmetic; beyond the bound, refuse to the
#: oracle. (2^52 was TOO LOOSE — with day_start ~2^53 the 3-term sum could
#: reach ~1.5·2^53 where f64 spacing is 2 and rounds, while the oracle's
#: arbitrary-precision int does not; DEC-RUST-SWEEP-OPS-001 review round 1.)
_SCHEDULER_EXACT_BOUND: Final[float] = float(2**50)
#: The recount post-byte-diff verdict tokens (the crate ↔ wiring contract;
#: `grid_corrupt` keys sweep_verify's heal-grade arm + the
#: grid_corrupt_leaderboards side effect — both stay Python).
_RECOUNT_VERDICT_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "vix_divergence",
        "grid_corrupt",
        "grid_inconclusive",
        "cell_dependent",
        "cell_corrupt",
        "cell_inconclusive",
    }
)
#: The Apriori frontier bit ceiling (`sweep_apriori._MASK_BITS` — drift-pinned
#: by the facade suite; the generator refuses ``>= 128`` bits before any join).
#: 128 since DEC-APRIORI-U128-WIDEN-001 — the 74-bit vocabulary does not fit
#: one u64 limb, so the old 64 ceiling refused every real production tuple.
_APRIORI_MAX_BIT: Final[int] = 128
#: The exclusive ceiling for a CONFLICT PAIR-MASK crossing the FFI. The kernel
#: types this argument ``Vec<u128>`` (DEC-APRIORI-U128-WIDEN-001), so the gate
#: is 2**128 — DELIBERATELY WIDER than :data:`_U64_CEILING` below.
_APRIORI_MASK_CEILING: Final[int] = 2**128
#: The exclusive ceiling for every argument the kernel types ``u64``: the
#: Apriori ``n_bars``/``limit``/``stride`` scalars and the grid-tuple-count
#: axes. MUST STAY 2**64 — widening it hands the crate a value that overflows
#: its own parameter type. Kept separate from
#: :data:`_APRIORI_MASK_CEILING` precisely so a mask widening can never
#: silently widen these too.
_U64_CEILING: Final[int] = 2**64

#: The INCLUSIVE band every argument the crate types ``i64`` accepts
#: (DEC-SWEEP-OPS-INT-BAND-GATES-001 — the named follow-up this change
#: closes). A Python ``int`` outside it cannot reach the crate AT ALL:
#: PyO3's argument conversion raises ``OverflowError`` (the module
#: docstring's degrade-set clause — "an out-of-``i64`` stage-3 knob lands
#: here") before a single line of Rust runs. That is an INPUT-envelope
#: verdict, never a Rust fault — so the ten laws whose gates used to admit
#: an unbounded ``int`` now decide it HERE, as ``*_input_refused``, and
#: their ``*_error`` reason means exactly one thing again: the crate ran
#: and misbehaved. Routed-domain NO-OP by construction — every value the
#: crate could ever have served is inside the band.
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1

#: Once-per-process-per-reason log memo.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (module docstring). MemoryError
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


def _i64_refused(value: object) -> bool:
    """``True`` when *value* may NOT cross the FFI as a crate ``i64``.

    The exact-``int`` gate (``bool`` IS an ``int`` subclass whose canonical
    render differs — excluded) UNION the :data:`_I64_MIN`/:data:`_I64_MAX`
    band. Replaces the bare ``type(v) is not int`` gates of the ten laws
    whose crate parameter is ``i64``, so an out-of-band ``int`` is decided
    as an INPUT verdict here instead of surfacing as an ambiguous
    ``<law>_error`` from PyO3's conversion ``OverflowError``.
    """
    return type(value) is not int or not _I64_MIN <= value <= _I64_MAX


def sweep_ops_rust_enabled() -> bool:
    """Resolve ``BRUTEX_SWEEP_OPS_RUST`` (default ON, junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False`` (the explicit kill
    switch); unset/blank → ``True`` (the default — ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23); any OTHER
    non-empty value → ``False`` with one ``sweep_ops_rust.fallback`` WARN
    (``flag_unrecognised``) per process. Read per call so spawn workers
    resolve their own environment (the sibling facades' law, byte-identical
    vocabulary via the imported :func:`env_flag_tristate`).
    """
    raw = os.environ.get(SWEEP_OPS_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def sweep_ops_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_sweep_ops_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the parity suites depend
    on that). Never called from production code (the sibling facades'
    reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for law in _ALL_LAWS:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


#: Fall-back reasons DELIBERATELY exempt from the strict-Rust refusal
#: (DEC-RUST-STRICT-NO-FALLBACK-001, the module's own "input outside the
#: crate's envelope" class). The retained Python oracle was ALWAYS the
#: intended handler for these, so refusing them would turn a legitimate
#: input into a crash — which the strict-mode doctrine explicitly forbids.
#: TWO families, both evidence-grounded (never a blanket exemption):
#:
#: 1. Every ``*_input_refused`` gate. The value the caller supplied is one
#:    the crate's flat-typed FFI cannot carry without a lossy conversion —
#:    an ``int`` where the law renders a ``float``, a ``bool`` where it
#:    renders an ``int``, a ``Path`` where it joins ``str``. They stay
#:    warn-and-degrade until each law's envelope is widened.
#:
#: 2. The TWO ``*_error`` reasons whose law can still raise for an input
#:    CPython's unbounded ints handle exactly, because the overflowing
#:    quantity is DERIVED INSIDE the crate — no INPUT gate can bound it
#:    without re-implementing the law Python-side (which would defeat the
#:    conversion). A gate-passing value makes the crate raise its declared
#:    ``ValueError`` on a LEGITIMATE input, so the retained oracle is the
#:    intended handler and refusing would crash the sweep. Both, with the
#:    crate line that proves the class:
#:
#:    * ``grid_tuple_count_error`` — the axes are individually u64-banded
#:      but their PRODUCT is not bindable at the gate; the crate returns
#:      ``Err`` at ``run_core.rs:65`` (``"axis product overflows u128"``)
#:      and ``run_core.rs:66-67`` (``"axis product exceeds u64"``).
#:      ``routed_grid_tuple_count``'s own docstring calls the degrade
#:      intended ("a legitimately astronomical grid is oracle territory,
#:      never a truncation"). Widening = a ``u128``/bigint return type on
#:      the crate side, which is a ``python.rs`` signature change.
#:    * ``quarantine_ordinal_error`` — the u64 ordinal is PARSED out of the
#:      dirname strings, so no numeric gate exists to bound it; the crate
#:      returns ``Err`` at ``artifacts.rs:113`` (a ``parse::<u64>()`` that
#:      does not fit) and ``artifacts.rs:118-119``
#:      (``checked_add(1)`` past ``u64::MAX``). Widening = a bigint ordinal
#:      on the crate side, again a ``python.rs`` signature change.
#:
#:    HONEST IMPRECISION (named, not hidden): a ``*_error`` token is
#:    AMBIGUOUS — it ALSO fires for a genuine crate fault on those two
#:    laws, and holding the token out silences that fault under strict mode
#:    too. Refusing them instead would crash a legitimate astronomical grid
#:    (or a lake carrying a >u64 quarantine ordinal), which is strictly
#:    worse. Retiring the ambiguity needs the crate to surface its
#:    envelope-overflow as a DISTINCT error type from a genuine fault — a
#:    ``rust/brutex-sweep-ops/src/python.rs`` change, which this pass is
#:    coordination-blocked from making. That is the NAMED FOLLOW-UP
#:    ``DEC-SWEEP-OPS-CRATE-OVERFLOW-TYPED-001``.
#:
#:    The sibling follow-up ``DEC-SWEEP-OPS-INT-BAND-GATES-001`` is CLOSED
#:    by this pass: every law whose overflow was an ARGUMENT-extraction
#:    ``OverflowError`` (``sla_decision``, ``rank_plan``,
#:    ``preflight_missing``, ``chain_verify_step``, ``chain_link_state``,
#:    ``count_violations`` — which bands even its checked
#:    ``evaluated + in_flight`` sum — ``checkpoint_record``,
#:    ``checkpoint_adoption``, ``resume_decision`` and the
#:    ``gate_fragment`` cvar/stage3 legs) now decides the out-of-band value
#:    at its OWN gate as ``*_input_refused``, so its ``*_error`` token
#:    means exactly one thing again: the crate ran and misbehaved.
#:
#: Every OTHER reason REFUSES — Rust absent (``crate_unavailable``), stale
#: (``crate_stale``), panicked (``crate_panic``), errored on a law whose
#: gate DOES bound the domain, structurally wrong (``*_result_refused``),
#: burn-in-divergent (``burn_in_mismatch_*``) or a junk flag
#: (``flag_unrecognised``) — those all mean Rust could not or did not serve
#: correctly, which is exactly what strict mode exists to surface.
#:
#: Drift teeth: ``tests/unit/test_engine_sweep_ops_rust.py`` pins this set
#: EQUAL to the ``*_input_refused`` literals present in this module UNION
#: the two named ``*_error`` reasons, so a new input gate must be added
#: here consciously rather than inherited silently.
_STRICT_HELD_OUT_REASONS: Final[frozenset[str]] = frozenset(
    {
        # ── family 2: the crate-INTERNAL overflow laws (see the note above)
        # Reached by an axis PRODUCT past u64 (run_core.rs:65-67) and by a
        # quarantine dirname whose PARSED ordinal is past u64
        # (artifacts.rs:113 / :118-119). Both overflow on a value derived
        # INSIDE Rust, so no Python-side input gate can pre-decide them —
        # the honest fix is widening the crate's return type (a python.rs
        # signature change), never refusing a legitimate sweep.
        "grid_tuple_count_error",
        "quarantine_ordinal_error",
        # ── family 1: the input gates ──
        "apriori_join_input_refused",
        "audit_entry_hash_input_refused",
        "chain_link_state_input_refused",
        "chain_verify_step_input_refused",
        "checkpoint_adoption_input_refused",
        "checkpoint_record_input_refused",
        "count_violations_input_refused",
        "detach_probe_input_refused",
        "diffcheck_budget_input_refused",
        "exit_status_resolution_input_refused",
        "freshness_stale_input_refused",
        # The gate-fragment leg gates. The ``max_sl_hit_rate`` leg is the
        # NAMED RESIDUE of DEC-RUST-SWEEP-OPS-GATE-FRAGMENT-WIDEN-001: that
        # widening RETIRED ``gate_fragment_beyond_crate`` (which fired for
        # EVERY set ceiling — every supported Door-6 sweep), but
        # ``SweepGridConfig`` validates the ceiling as finite-and-in-(0,1]
        # WITHOUT an exact-``float`` gate, so an ``int``/``Decimal`` ceiling
        # still refuses here rather than splitting the run-identity digest.
        "gate_fragment_input_refused",
        "grid_tuple_count_input_refused",
        "heal_mismatch_input_refused",
        "liveness_tail_input_refused",
        "preflight_missing_input_refused",
        "progress_derived_input_refused",
        "quarantine_next_ordinal_input_refused",
        "rank_plan_input_refused",
        "record_stride_input_refused",
        "recount_verdict_input_refused",
        "resume_decision_input_refused",
        "run_path_input_refused",
        "schedule_next_fire_input_refused",
        "sla_decision_input_refused",
        "stall_timeout_input_refused",
        "stamp_verdict_input_refused",
    }
)


def _warn_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``sweep_ops_rust.fallback`` warn-once.

    One of this facade's TWO fall-back choke points (:func:`_disable` is the
    other), so :func:`~brutex.config.rust_strict.refuse_if_strict` here plus
    there is a complete block on the Python oracle for every reason NOT in
    :data:`_STRICT_HELD_OUT_REASONS` (DEC-RUST-STRICT-NO-FALLBACK-001).

    With strict mode OFF the historic behaviour is byte-identical.
    """
    if reason not in _STRICT_HELD_OUT_REASONS:
        refuse_if_strict("brutex.engine.sweep_ops_rust", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_ops_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Refuse under strict mode; else latch the Rust path OFF for the process.

    The facade's SECOND fall-back choke point (:func:`_warn_once` is the
    other). Every reason reaching here means Rust is absent
    (``crate_unavailable``), stale (``crate_stale``), panicked
    (``crate_panic``) or answered wrongly (``burn_in_mismatch_*``) — never
    "this input is outside the crate's envelope" — so under strict mode ALL
    of them refuse (DEC-RUST-STRICT-NO-FALLBACK-001) and the latch below is
    unreachable. Refusing BEFORE the latch keeps the refusal repeatable:
    a latched-then-silent facade would serve Python for the rest of the
    process off a single early degrade.

    With strict mode OFF the historic behaviour is byte-identical:
    ``error=True`` logs ``sweep_ops_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``sweep_ops_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    refuse_if_strict("brutex.engine.sweep_ops_rust", reason, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("sweep_ops_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("sweep_ops_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_sweep_ops``, or latch OFF.

    An absent wheel (``ImportError`` — including the shim's own stale-wheel
    handshake raise) latches ``crate_unavailable``. A wheel that imports but
    lacks any :data:`_CRATE_API` callable is a pre-slice-1 (or
    foreign/broken) install — latch ``crate_stale``. Fail-closed: never a
    new crash class.
    """
    try:
        crate = importlib.import_module("brutex_sweep_ops")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="maturin develop --release inside rust/brutex-sweep-ops "
            "(with the project venv active)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_sweep_ops.{name} missing or not callable — "
                "the installed wheel predates the slice-1 laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-sweep-ops (maturin develop --release)",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``sweep_ops_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "sweep_ops_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _burn_in_ships_oracle(law: str, rust_value: object, oracle_value: object) -> bool:
    """Run the per-*law* burn-in compare; ``True`` ⇒ ship the ORACLE value.

    Decrements the law's budget on a clean compare (INFO once at zero); a
    mismatch autoreverts the process (ERROR) and returns ``True`` so the
    caller ships the oracle value. A zero budget is the steady state — the
    caller must not have computed the oracle (callers check the budget
    BEFORE building the oracle value; this helper is only reached with a
    positive budget).
    """
    if rust_value != oracle_value:
        _disable(
            f"burn_in_mismatch_{law}",
            error=True,
            rust_value=repr(rust_value),
            python_value=repr(oracle_value),
        )
        return True
    _BURN_IN_REMAINING[law] -= 1
    if _BURN_IN_REMAINING[law] == 0:
        _LOG.info("sweep_ops_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return False


def routed_run_path(
    base: str,
    segments: tuple[str, ...],
    *,
    python_law: Callable[[], Path],
) -> Path:
    """The routed path-join law — flag-gated Rust with the live Python oracle.

    EVERY path returns a valid ``Path``: flag-OFF / latched / guarded /
    degraded calls return ``python_law()`` (the oracle), a burn-in mismatch
    returns the ORACLE value + autoreverts, and only a guard-clean,
    burn-in-clean call returns ``Path(<rust join>)``.

    Exact-type input gates: *base* and every segment must be exact ``str``
    (a ``Path``/subclass input never crosses the FFI) and every segment must
    be non-empty, relative and slash-free — the ONLY shapes the crate's join
    law mirrors ``pathlib`` for (anything else is refused to the oracle).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if type(base) is not str or any(
        type(segment) is not str or not segment or "/" in segment for segment in segments
    ):
        _warn_once("run_path_input_refused", base_type=type(base).__name__)
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        joined = crate.join_path_segments(base, list(segments))
    except _DEGRADE_ERRORS as exc:
        _warn_once("run_path_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not isinstance(joined, str):
        _warn_once("run_path_result_refused", result_type=type(joined).__name__)
        return python_law()
    rust_path = Path(joined)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_RUN_PATH] > 0:
        oracle_path = python_law()
        if _burn_in_ships_oracle(_LAW_RUN_PATH, rust_path, oracle_path):
            return oracle_path
    return rust_path


def routed_gate_fragment(
    overfit_values: tuple[float, float, float, float] | None,
    stage3_enable: bool,
    stage3_knobs: tuple[int, int, int],
    *,
    max_sl_hit_rate: float | None = None,
    cvar_values: tuple[int, int] | None = None,
    python_law: Callable[[], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """The routed gate-config fragment law (feeds RUN IDENTITY — see docstring).

    Exact-type input gates: every overfit value and a SET *max_sl_hit_rate*
    must be exact ``float``, and every stage-3 / *cvar_values* knob exact
    ``int`` (``bool`` IS an ``int`` subclass whose canonical-JSON render
    differs — excluded), so the f64/i64 FFI roundtrip is bit-exact by
    construction; anything else is refused to the oracle.

    Envelope (DEC-RUST-SWEEP-OPS-GATE-FRAGMENT-WIDEN-001 — the widening
    that RETIRED the ``gate_fragment_beyond_crate`` refusal): the crate's
    ``gate_config_fragment`` now carries FOUR legs, so BOTH previously
    beyond-crate knobs — *max_sl_hit_rate*
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) and *cvar_values*
    (DEC-RANK-CVAR-TAIL-GATE-001) — are computed IN RUST instead of
    bouncing the WHOLE computation back to the Python oracle. The previous
    seam refused them because the crate would otherwise serve a fragment
    silently DROPPING their keys (a run-identity-class divergence); the
    honest fix was to teach the crate, not to keep degrading.

    HONEST RESIDUE (named, not closed): ``SweepGridConfig.__post_init__``
    validates *max_sl_hit_rate* as finite-and-in-``(0.0, 1.0]`` but NOT as
    an exact ``float``, so a numeric-but-not-float ceiling (an ``int`` 1, a
    ``Decimal``) is accepted by the constructor. The Python oracle renders
    such a value VERBATIM (``{"max_sl_hit_rate": 1}`` → JSON ``1``) while an
    f64 roundtrip would render ``1.0`` — a digest split. Those shapes are
    therefore refused to the oracle here as ``gate_fragment_input_refused``
    (leg ``max_sl_hit_rate``), exactly like a non-float overfit knob.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if overfit_values is not None and any(type(value) is not float for value in overfit_values):
        _warn_once("gate_fragment_input_refused", leg="overfit")
        return python_law()
    if max_sl_hit_rate is not None and type(max_sl_hit_rate) is not float:
        # See the HONEST RESIDUE clause: an int/Decimal ceiling renders
        # differently through the oracle than through an f64 roundtrip.
        _warn_once("gate_fragment_input_refused", leg="max_sl_hit_rate")
        return python_law()
    if cvar_values is not None and any(_i64_refused(knob) for knob in cvar_values):
        _warn_once("gate_fragment_input_refused", leg="cvar")
        return python_law()
    if any(_i64_refused(knob) for knob in stage3_knobs):
        _warn_once("gate_fragment_input_refused", leg="stage3")
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        overfit_leg, max_sl_leg, cvar_leg, stage3_leg = crate.gate_config_fragment(
            overfit_values, max_sl_hit_rate, cvar_values, bool(stage3_enable), *stage3_knobs
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("gate_fragment_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not _valid_fragment_legs(overfit_leg, max_sl_leg, cvar_leg, stage3_leg):
        _warn_once("gate_fragment_result_refused")
        return python_law()
    fragment: dict[str, Any] = {}
    if overfit_leg is not None:
        fragment["overfit"] = list(overfit_leg)
    if max_sl_leg is not None:
        fragment["max_sl_hit_rate"] = max_sl_leg
    if cvar_leg is not None:
        fragment["cvar"] = list(cvar_leg)
    if stage3_leg is not None:
        fragment["stage3"] = list(stage3_leg)
    rust_fragment = fragment or None
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_GATE_FRAGMENT] > 0:
        oracle_fragment = python_law()
        if _burn_in_ships_oracle(_LAW_GATE_FRAGMENT, rust_fragment, oracle_fragment):
            return oracle_fragment
    return rust_fragment


def _valid_fragment_legs(
    overfit_leg: object,
    max_sl_leg: object,
    cvar_leg: object,
    stage3_leg: object,
) -> bool:
    """Structural OUTPUT gate for the crate's four fragment legs (refuse-poison)."""
    if overfit_leg is not None and not (
        isinstance(overfit_leg, tuple)
        and len(overfit_leg) == _OVERFIT_LEG_ARITY
        and all(type(value) is float for value in overfit_leg)
    ):
        return False
    if max_sl_leg is not None and type(max_sl_leg) is not float:
        return False
    if cvar_leg is not None and not (
        isinstance(cvar_leg, tuple)
        and len(cvar_leg) == _CVAR_LEG_ARITY
        and all(type(knob) is int for knob in cvar_leg)
    ):
        return False
    return stage3_leg is None or (
        isinstance(stage3_leg, tuple)
        and len(stage3_leg) == _STAGE3_LEG_ARITY
        and all(type(knob) is int for knob in stage3_leg)
    )


def routed_stall_timeout(
    factor: float,
    estimate: float,
    base: float,
    *,
    python_law: Callable[[], tuple[float, bool]],
) -> tuple[float, bool]:
    """The routed scaled stall-timeout tail — ``(timeout_seconds, scaled?)``.

    Exact-type input gates: all three operands must be exact ``float`` (the
    callers' fail-loud env parses always produce floats; anything else is
    refused to the oracle so the f64 roundtrip is bit-exact).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(type(value) is not float for value in (factor, estimate, base)):
        _warn_once("stall_timeout_input_refused")
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        result = crate.stall_scaled_timeout(factor, estimate, base)
    except _DEGRADE_ERRORS as exc:
        _warn_once("stall_timeout_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not (
        isinstance(result, tuple)
        and len(result) == _STALL_PAIR_ARITY
        and type(result[0]) is float
        and type(result[1]) is bool
    ):
        _warn_once("stall_timeout_result_refused", result_type=type(result).__name__)
        return python_law()
    rust_pair: tuple[float, bool] = (result[0], result[1])
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_STALL_TIMEOUT] > 0:
        oracle_pair = python_law()
        if _burn_in_ships_oracle(_LAW_STALL_TIMEOUT, rust_pair, oracle_pair):
            return oracle_pair
    return rust_pair


# ────────────────── slice 1b — the shared engage core ──────────────────


def _routed_law_call[T](
    law: str,
    crate_attr: str,
    args: tuple[Any, ...],
    *,
    python_law: Callable[[], T],
    validate: Callable[[object], T | None],
) -> T:
    """The ONE engage core every slice-1b routed law rides (§11 COMMON).

    Resolve the crate → call *crate_attr* → the degrade trio (narrow
    :data:`_DEGRADE_ERRORS` warn-once; the escape set re-raises raw; the
    panic belt latches) → the refuse-poison *validate* gate (``None`` =
    refused — no routed law legitimately returns bare ``None``) → the
    per-*law* burn-in. Flag/latch gating + the law-specific INPUT gates
    stay in each ``routed_*`` wrapper. The three slice-1 laws keep their
    original longhand bodies (final-slice housekeeping to unify).
    """
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        raw = getattr(crate, crate_attr)(*args)
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{law}_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    value = validate(raw)
    if value is None:
        _warn_once(f"{law}_result_refused", result_type=type(raw).__name__)
        return python_law()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[law] > 0:
        oracle_value = python_law()
        if _burn_in_ships_oracle(law, value, oracle_value):
            return oracle_value
    return value


def _validate_progress_derived(
    raw: object,
) -> tuple[float, float, float, float | None, float] | None:
    """Structural OUTPUT gate for the progress derived-metrics tuple."""
    if not (isinstance(raw, tuple) and len(raw) == _DERIVED_ARITY):
        return None
    percent, elapsed, rate, eta, survivor_rate = raw
    if any(type(value) is not float for value in (percent, elapsed, rate, survivor_rate)):
        return None
    if eta is not None and type(eta) is not float:
        return None
    return (percent, elapsed, rate, eta, survivor_rate)


def routed_progress_derived(
    enumerated: int,
    evaluated: int,
    survivors: int,
    started_at: float,
    updated_at: float,
    *,
    python_law: Callable[[], tuple[float, float, float, float | None, float]],
) -> tuple[float, float, float, float | None, float]:
    """The routed progress derived-metrics law —
    ``(percent, elapsed_seconds, combos_per_sec, eta_seconds, survivor_rate)``.

    Exact-type input gates: the three counts must be exact ``int``
    (``bool`` excluded) inside the :data:`_MAX_EXACT_COUNT` band (the
    ``int / int`` correctly-rounded-division parity bound) and both clock
    stamps exact ``float``; anything else is refused to the oracle.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(
        type(count) is not int or abs(count) > _MAX_EXACT_COUNT
        for count in (enumerated, evaluated, survivors)
    ) or any(type(stamp) is not float for stamp in (started_at, updated_at)):
        _warn_once("progress_derived_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_PROGRESS_DERIVED,
        "progress_derived",
        (enumerated, evaluated, survivors, started_at, updated_at),
        python_law=python_law,
        validate=_validate_progress_derived,
    )


def _validate_sla_decision(raw: object) -> tuple[float, str] | None:
    """Structural OUTPUT gate for the SLA ``(projected, decision)`` pair."""
    if not (isinstance(raw, tuple) and len(raw) == _DECISION_PAIR_ARITY):
        return None
    projected, token = raw
    if type(projected) is not float or type(token) is not str:
        return None
    if token not in _SLA_DECISION_TOKENS:
        return None
    return (projected, token)


def routed_sla_decision(
    tuple_count: int,
    per_tuple_seconds: float,
    envelope_seconds: float,
    acknowledge: bool,
    *,
    python_law: Callable[[], tuple[float, str]],
) -> tuple[float, str]:
    """The routed SLA projection decision — ``(projected_seconds, token)``.

    Exact-type input gates: *tuple_count* an exact ``int`` INSIDE the
    :func:`_i64_refused` band (the ``int → float`` conversion is
    round-nearest on BOTH sides for every ``i64``, so the band is the only
    gate needed), both seconds exact ``float``. *acknowledge* crosses as
    its truthiness (the Python law's own ``if acknowledge:`` semantics).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if _i64_refused(tuple_count) or any(
        type(value) is not float for value in (per_tuple_seconds, envelope_seconds)
    ):
        _warn_once("sla_decision_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_SLA_DECISION,
        "sla_decision",
        (tuple_count, per_tuple_seconds, envelope_seconds, bool(acknowledge)),
        python_law=python_law,
        validate=_validate_sla_decision,
    )


def _validate_rank_plan(raw: object, *, has_top_k: bool) -> tuple[int, str] | None:
    """Structural OUTPUT gate for the rank-plan ``(projected, verdict)`` pair.

    Beyond shape: a ``"bounded"`` verdict without a finite ``top_k`` is
    structurally impossible from the Python law (mode ``on`` validates it,
    mode ``auto`` requires it) — a wheel emitting one is refused.
    """
    if not (isinstance(raw, tuple) and len(raw) == _DECISION_PAIR_ARITY):
        return None
    projected, verdict = raw
    if type(projected) is not int or type(verdict) is not str:
        return None
    if verdict not in _RANK_PLAN_VERDICTS:
        return None
    if verdict == "bounded" and not has_top_k:
        return None
    return (projected, verdict)


def routed_rank_plan(
    n_masks: int,
    budget_bytes: int,
    mode_token: str,
    has_top_k: bool,
    digest_path: bool,
    *,
    python_law: Callable[[], tuple[int, str]],
) -> tuple[int, str]:
    """The routed rank-memory plan decision — ``(projected_bytes, verdict)``.

    Exact-type input gates: *n_masks* / *budget_bytes* exact ``int``
    INSIDE the :func:`_i64_refused` band (the crate multiplies in exact
    ``i128``, so the band is the only gate needed) and *mode_token* one of
    :data:`_RANK_MODE_TOKENS`.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        any(_i64_refused(value) for value in (n_masks, budget_bytes))
        or type(mode_token) is not str
        or mode_token not in _RANK_MODE_TOKENS
    ):
        _warn_once("rank_plan_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_RANK_PLAN,
        "rank_plan_decision",
        (n_masks, budget_bytes, mode_token, bool(has_top_k), bool(digest_path)),
        python_law=python_law,
        validate=lambda raw: _validate_rank_plan(raw, has_top_k=bool(has_top_k)),
    )


def _validate_string_pairs(raw: object) -> tuple[tuple[str, str], ...] | None:
    """Structural OUTPUT gate for a crate ``list[(str, str)]`` fold result."""
    if not isinstance(raw, list):
        return None
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not (
            isinstance(item, tuple)
            and len(item) == _STRING_PAIR_ARITY
            and type(item[0]) is str
            and type(item[1]) is str
        ):
            return None
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def routed_preflight_missing(
    slice_sizes: Mapping[tuple[str, str], int],
    symbols: Sequence[str],
    intervals: Sequence[str],
    *,
    python_law: Callable[[], tuple[tuple[str, str], ...]],
) -> tuple[tuple[str, str], ...]:
    """The routed preflight zero-bar fold — the missing-pair tuple.

    Exact-type input gates (an ``O(pairs)`` sweep — the fold itself is the
    same order, and the seam runs once per launch): every mapping key a
    2-tuple of exact ``str``, every count an exact ``int`` INSIDE the
    :func:`_i64_refused` band, every requested symbol/interval exact
    ``str``.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    triples: list[tuple[str, str, int]] = []
    for key, count in slice_sizes.items():
        if (
            not (isinstance(key, tuple) and len(key) == _STRING_PAIR_ARITY)
            or type(key[0]) is not str
            or type(key[1]) is not str
            or _i64_refused(count)
        ):
            _warn_once("preflight_missing_input_refused")
            return python_law()
        triples.append((key[0], key[1], count))
    if any(type(symbol) is not str for symbol in symbols) or any(
        type(interval) is not str for interval in intervals
    ):
        _warn_once("preflight_missing_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_PREFLIGHT_MISSING,
        "preflight_missing",
        (triples, list(symbols), list(intervals)),
        python_law=python_law,
        validate=_validate_string_pairs,
    )


def routed_freshness_stale(
    verdicts: Mapping[tuple[str, str], str],
    *,
    python_law: Callable[[], tuple[tuple[str, str], ...]],
) -> tuple[tuple[str, str], ...]:
    """The routed derived-freshness stale fold — the sorted stale-pair tuple.

    Exact-type input gates (``O(pairs)``, once per launch): every mapping
    key a 2-tuple of exact ``str`` and every verdict exact ``str``.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    triples: list[tuple[str, str, str]] = []
    for key, verdict in verdicts.items():
        if (
            not (isinstance(key, tuple) and len(key) == _STRING_PAIR_ARITY)
            or type(key[0]) is not str
            or type(key[1]) is not str
            or type(verdict) is not str
        ):
            _warn_once("freshness_stale_input_refused")
            return python_law()
        triples.append((key[0], key[1], verdict))
    return _routed_law_call(
        _LAW_FRESHNESS_STALE,
        "freshness_stale",
        (triples,),
        python_law=python_law,
        validate=_validate_string_pairs,
    )


def _validate_liveness_tail(raw: object) -> str | None:
    """Structural OUTPUT gate for the liveness verdict-tail token."""
    if type(raw) is not str or raw not in _LIVENESS_TAIL_TOKENS:
        return None
    return raw


def routed_liveness_tail(
    pid_ok: bool,
    now_wall: float,
    wall: float,
    fresh_seconds: float,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed liveness verdict tail — a :data:`_LIVENESS_TAIL_TOKENS` token.

    Exact-type input gates: *pid_ok* exact ``bool`` and the three clock
    operands exact ``float`` (the wiring passes ``float(wall)``; a
    non-float ``now_wall``/``fresh_seconds`` from a hostile caller is
    refused to the oracle).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if type(pid_ok) is not bool or any(
        type(value) is not float for value in (now_wall, wall, fresh_seconds)
    ):
        _warn_once("liveness_tail_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_LIVENESS_TAIL,
        "liveness_tail",
        (pid_ok, now_wall, wall, fresh_seconds),
        python_law=python_law,
        validate=_validate_liveness_tail,
    )


# ─────────────────── slice 2 — the audit-chain family ───────────────────


def _validate_hash_hex(raw: object) -> str | None:
    """Structural OUTPUT gate for a 64-lowercase-hex BLAKE3 digest string."""
    if type(raw) is not str or len(raw) != _HASH_HEX_LEN:
        return None
    if not all(char in _HEX_DIGITS for char in raw):
        return None
    return raw


def routed_audit_entry_hash(
    seq_str: str,
    prev_hash: str,
    ts_str: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed sweep-audit BLAKE3 entry-hash law (Golden Rule #8 chain).

    Exact-type input gates: all seven fields must be exact ``str`` — they
    arrive ALREADY rendered by Python (``str(seq)`` / the float-repr
    ``str(ts_utc)`` / the canonical payload JSON), so the crate only joins
    the UTF-8 bytes with ``|`` and hashes. A lone-surrogate field (which the
    oracle's own ``.encode("utf-8")`` would raise on) fails the PyO3 string
    conversion into the degrade set and the oracle raises identically.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    fields = (seq_str, prev_hash, ts_str, actor, action, subject, payload_json)
    if any(type(field) is not str for field in fields):
        _warn_once("audit_entry_hash_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_AUDIT_ENTRY_HASH,
        "audit_entry_hash",
        fields,
        python_law=python_law,
        validate=_validate_hash_hex,
    )


def _validate_verify_step(raw: object) -> tuple[str, str, str] | None:
    """Structural OUTPUT gate for the ``(token, expected, actual)`` triple."""
    if not (isinstance(raw, tuple) and len(raw) == _VERIFY_TRIPLE_ARITY):
        return None
    token, expected, actual = raw
    if any(type(part) is not str for part in (token, expected, actual)):
        return None
    if token not in _CHAIN_VERIFY_TOKENS:
        return None
    return (token, expected, actual)


def routed_chain_verify_step(
    expected_seq: int,
    prev_expected: str,
    seq: int,
    prev_hash: str,
    ts_str: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
    entry_hash: str,
    *,
    python_law: Callable[[], tuple[str, str, str]],
) -> tuple[str, str, str]:
    """The routed per-entry ``verify_chain`` step — ``(token, expected, actual)``.

    Exact-type input gates: both seq operands exact ``int`` INSIDE the
    :func:`_i64_refused` band (``bool`` excluded — ``str(True)`` ≠ Rust
    ``"true"``) and the eight field strings exact
    ``str``. Python owns the corruption-exception construction + message
    bytes; only the DECISION (which check failed first, with the
    recomputed hash) routes.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(_i64_refused(value) for value in (expected_seq, seq)) or any(
        type(field) is not str
        for field in (
            prev_expected,
            prev_hash,
            ts_str,
            actor,
            action,
            subject,
            payload_json,
            entry_hash,
        )
    ):
        _warn_once("chain_verify_step_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_CHAIN_VERIFY_STEP,
        "chain_verify_step",
        (
            expected_seq,
            prev_expected,
            seq,
            prev_hash,
            ts_str,
            actor,
            action,
            subject,
            payload_json,
            entry_hash,
        ),
        python_law=python_law,
        validate=_validate_verify_step,
    )


def _validate_stamp_verdict(raw: object) -> str | None:
    """Structural OUTPUT gate for the stamp-verdict result token."""
    if type(raw) is not str or raw not in _STAMP_VERDICT_RESULTS:
        return None
    return raw


def routed_stamp_verdict(
    recomputed: str,
    content_hash: str,
    stage: str,
    expected_stage: str,
    run_id: str,
    expected_run_id: str | None,
    stamped_snapshot: str | None,
    expected_snapshot: str | None,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed stage-stamp mismatch decision — ``"ok"`` / ``"hash_mismatch"``.

    Exact-type input gates: the five mandatory operands exact ``str``; the
    three optional operands ``None`` or exact ``str``. The artifact hashing
    + the MISSING/MALFORMED I/O verdicts stay Python.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(
        type(field) is not str
        for field in (recomputed, content_hash, stage, expected_stage, run_id)
    ) or any(
        optional is not None and type(optional) is not str
        for optional in (expected_run_id, stamped_snapshot, expected_snapshot)
    ):
        _warn_once("stamp_verdict_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_STAMP_VERDICT,
        "stamp_verdict",
        (
            recomputed,
            content_hash,
            stage,
            expected_stage,
            run_id,
            expected_run_id,
            stamped_snapshot,
            expected_snapshot,
        ),
        python_law=python_law,
        validate=_validate_stamp_verdict,
    )


def _validate_link_state(raw: object) -> tuple[str, str] | None:
    """Structural OUTPUT gate for the ``(state_token, detail)`` pair."""
    if not (isinstance(raw, tuple) and len(raw) == _STRING_PAIR_ARITY):
        return None
    state, detail = raw
    if type(state) is not str or type(detail) is not str:
        return None
    if state not in _LINK_STATE_TOKENS:
        return None
    return (state, detail)


def routed_chain_link_state(
    link: str,
    idx: int,
    verdict_token: str | None,
    running: bool,
    last_stage: str | None,
    terminal: str | None,
    refusal_text: str | None,
    error_type: str | None,
    *,
    python_law: Callable[[], tuple[str, str]],
) -> tuple[str, str]:
    """The routed chain-map per-link state ladder — ``(state_token, detail)``.

    Exact-type input gates: *link* exact ``str``, *idx* exact ``int``
    (``bool`` excluded), *running* exact ``bool``, *verdict_token* ``None``
    or one of the mirrored ``StampVerdict`` values, *last_stage* ``None``
    or one of the mirrored :data:`_CHAIN_LINKS` (the ladder indexes it),
    and the terminal/refusal/error operands ``None`` or exact ``str``.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        type(link) is not str
        or _i64_refused(idx)
        or type(running) is not bool
        or (verdict_token is not None and verdict_token not in _STAMP_VERDICT_TOKENS)
        or (last_stage is not None and last_stage not in _CHAIN_LINKS)
        or any(
            optional is not None and type(optional) is not str
            for optional in (terminal, refusal_text, error_type)
        )
    ):
        _warn_once("chain_link_state_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_CHAIN_LINK_STATE,
        "chain_link_state",
        (link, idx, verdict_token, running, last_stage, terminal, refusal_text, error_type),
        python_law=python_law,
        validate=_validate_link_state,
    )


def _validate_violations_detail(raw: object) -> str | None:
    """Structural OUTPUT gate for the violations detail (EMPTY = clean).

    The engage core's ``None``-means-refused contract forbids a bare
    ``None`` law result, so the crate renders "no violations" as the EMPTY
    string — any exact ``str`` passes.
    """
    if type(raw) is not str:
        return None
    return raw


def routed_count_violations(
    total: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    in_flight: int,
    prev_evaluated: int | None,
    is_done: bool,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed D-9 count-reconciliation violations fold (EMPTY = clean).

    Exact-type input gates: the six counters exact ``int`` INSIDE the
    :func:`_i64_refused` band, *prev_evaluated* ``None`` or a banded exact
    ``int``, *is_done* exact ``bool`` — PLUS the crate's ONE checked
    arithmetic pre-decided here (``evaluated + in_flight`` must itself land
    inside ``i64``; ``audit_chain.rs:273-275`` returns ``Err`` otherwise,
    which would surface indistinguishably from a real crate fault). Runs
    inside the tripwire monitor's fail-soft envelope — every degrade ships
    the oracle, never a new raise into the sweep.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    counters = (total, enumerated, evaluated, skipped, survivors, in_flight)
    if (
        any(_i64_refused(count) for count in counters)
        or (prev_evaluated is not None and _i64_refused(prev_evaluated))
        or type(is_done) is not bool
    ):
        _warn_once("count_violations_input_refused")
        return python_law()
    # The crate's only fallible arithmetic (audit_chain.rs:273-275). Deciding
    # it HERE keeps ``count_violations_error`` a pure crate-fault token.
    if _i64_refused(evaluated + in_flight):
        _warn_once("count_violations_input_refused", leg="issued_sum")
        return python_law()
    return _routed_law_call(
        _LAW_COUNT_VIOLATIONS,
        "count_reconciliation",
        (*counters, prev_evaluated, is_done),
        python_law=python_law,
        validate=_validate_violations_detail,
    )


def _validate_resolution(raw: object) -> tuple[float, float | None, bool] | None:
    """Structural OUTPUT gate for the ``(started, ended, suspect)`` triple."""
    if not (isinstance(raw, tuple) and len(raw) == _RESOLUTION_TRIPLE_ARITY):
        return None
    started, ended, suspect = raw
    if type(started) is not float or not math.isfinite(started):
        return None
    if ended is not None and (type(ended) is not float or not math.isfinite(ended)):
        return None
    if type(suspect) is not bool:
        return None
    return (started, ended, suspect)


def routed_exit_status_resolution(
    state_value: str,
    prior_started: float | None,
    now: float,
    suspect: bool | None,
    prior_suspect: bool | None,
    *,
    python_law: Callable[[], tuple[float, float | None, bool]],
) -> tuple[float, float | None, bool]:
    """The routed exit-status wall-clock + suspect resolution law (slice 3).

    Exact-type input gates: *state_value* an exact ``str`` in the mirrored
    :data:`_RUN_STATE_TOKENS`; *prior_started* ``None`` or an exact FINITE
    ``float`` (the caller's ``_finite_float`` already guarantees this — the
    gate is the belt); *now* an exact finite ``float``; the two suspect
    flags ``None`` or exact ``bool``. Floats cross the FFI as f64
    BIT-exactly, so the returned ``started``/``ended`` render to the SAME
    ``json.dumps`` bytes either arm — rendering itself stays Python.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        type(state_value) is not str
        or state_value not in _RUN_STATE_TOKENS
        or (
            prior_started is not None
            and (type(prior_started) is not float or not math.isfinite(prior_started))
        )
        or type(now) is not float
        or not math.isfinite(now)
        or (suspect is not None and type(suspect) is not bool)
        or (prior_suspect is not None and type(prior_suspect) is not bool)
    ):
        _warn_once("exit_status_resolution_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_EXIT_STATUS_RESOLUTION,
        "exit_status_resolution",
        (state_value, prior_started, now, suspect, prior_suspect),
        python_law=python_law,
        validate=_validate_resolution,
    )


def _validate_ordinal(raw: object) -> int | None:
    """Structural OUTPUT gate for the next quarantine ordinal (``>= 1``)."""
    if type(raw) is not int or raw < 1:
        return None
    return raw


def routed_quarantine_next_ordinal(
    names: Sequence[str],
    prefix: str,
    suffix: str,
    *,
    python_law: Callable[[], int],
) -> int:
    """The routed monotonic quarantine-ordinal fold (slice 3).

    Exact-type input gates: *names* a ``list`` of exact ``str`` entries,
    *prefix*/*suffix* exact ``str`` — and EVERY string ASCII-only with NO
    embedded newline. Python's ``int()`` accepts non-ASCII Unicode digits
    and ``re``'s ``$`` matches before a trailing ``"\\n"`` (so a planted
    ``"run-12\\n"`` dirname IS an ordinal to the oracle) — both shapes are
    refused to the oracle rather than mirrored in Rust. The ``iterdir``
    that produced *names* — and the quarantine MOVE itself — stay Python.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        type(names) is not list
        or any(type(name) is not str or not name.isascii() or "\n" in name for name in names)
        or any(
            type(token) is not str or not token.isascii() or "\n" in token
            for token in (prefix, suffix)
        )
    ):
        _warn_once("quarantine_next_ordinal_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_QUARANTINE_ORDINAL,
        "quarantine_next_ordinal",
        (names, prefix, suffix),
        python_law=python_law,
        validate=_validate_ordinal,
    )


def _validate_record_decision(raw: object) -> str | None:
    """Structural OUTPUT gate for the checkpoint record-decision token."""
    if type(raw) is not str or raw not in _RECORD_DECISION_TOKENS:
        return None
    return raw


def routed_checkpoint_record_decision(
    completed_index: int,
    last_index: int,
    total_combos: int,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed checkpoint monotonic-advance decision (slice 3).

    Exact-type input gates: all three indices exact ``int`` INSIDE the
    :func:`_i64_refused` band. The refusal MESSAGE bytes, the in-memory
    high-water mutation and the atomic save stay with
    :meth:`SweepCheckpointStore.record` — only the decision routes, so the
    never-rewind ordering law is untouched.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(_i64_refused(value) for value in (completed_index, last_index, total_combos)):
        _warn_once("checkpoint_record_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_CHECKPOINT_RECORD,
        "checkpoint_record_decision",
        (completed_index, last_index, total_combos),
        python_law=python_law,
        validate=_validate_record_decision,
    )


def _validate_adoption_verdict(raw: object) -> str | None:
    """Structural OUTPUT gate for the checkpoint adoption-verdict token."""
    if type(raw) is not str or raw not in _ADOPTION_TOKENS:
        return None
    return raw


def routed_checkpoint_adoption_verdict(
    store_sweep_id: str,
    store_total: int,
    cp_sweep_id: str,
    cp_total: int,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed checkpoint load-tail adoption verdict (slice 3).

    Exact-type input gates: the two ids exact ``str``, the two totals exact
    ``int`` INSIDE the :func:`_i64_refused` band. The tamper-refusal
    MESSAGE bytes and the adoption side effect (the in-memory high-water
    set) stay with :meth:`SweepCheckpointStore.load` — only the verdict
    routes.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(type(value) is not str for value in (store_sweep_id, cp_sweep_id)) or any(
        _i64_refused(value) for value in (store_total, cp_total)
    ):
        _warn_once("checkpoint_adoption_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_CHECKPOINT_ADOPTION,
        "checkpoint_adoption_verdict",
        (store_sweep_id, store_total, cp_sweep_id, cp_total),
        python_law=python_law,
        validate=_validate_adoption_verdict,
    )


# ─────────────────── slice 4 — the process/lock-ops family ───────────────────


def _validate_run_verdict(raw: object) -> str | None:
    """Structural OUTPUT gate for the detach probe-verdict token."""
    if type(raw) is not str or raw not in _RUN_VERDICT_TOKENS:
        return None
    return raw


def routed_detach_probe_verdict(
    has_progress: bool,
    handle_pid_alive: bool,
    verdict_token: str | None,
    pids_match: bool,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed detach probe-handle cross-check fold (slice 4, R7).

    Exact-type input gates: the three flags exact ``bool``; *verdict_token*
    ``None`` (only when *has_progress* is ``False``) or an exact ``str`` in
    the mirrored :data:`_RUN_VERDICT_TOKENS`. The liveness verdict itself is
    an INPUT here — computed Python-side by ``progress_verdict`` (whose own
    decision tail is the slice-1b routed law; nothing is double-routed) —
    and the ``pids_match`` equality over the raw JSON ``pid`` value stays
    Python. The pid probe, the pidfile read and the fork/env-sentinel spawn
    machinery never cross the FFI.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        any(type(flag) is not bool for flag in (has_progress, handle_pid_alive, pids_match))
        or (
            verdict_token is not None
            and (type(verdict_token) is not str or verdict_token not in _RUN_VERDICT_TOKENS)
        )
        or (has_progress and verdict_token is None)
    ):
        _warn_once("detach_probe_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_DETACH_PROBE,
        "detach_probe_verdict",
        (has_progress, handle_pid_alive, verdict_token, pids_match),
        python_law=python_law,
        validate=_validate_run_verdict,
    )


def _validate_heal_mismatch(raw: object) -> str | None:
    """Structural OUTPUT gate for the heal mismatch-tail decision token."""
    if type(raw) is not str or raw not in _HEAL_MISMATCH_TOKENS:
        return None
    return raw


def _heal_triple_refused(values: Sequence[str | None]) -> bool:
    """``True`` when a heal hash triple may NOT cross the FFI (gate helper)."""
    if type(values) is not list or len(values) != _HEAL_ARTIFACT_SLOTS:
        return True
    return any(
        value is not None and (type(value) is not str or not value.isascii()) for value in values
    )


def routed_heal_mismatch(
    src_values: Sequence[str | None],
    plan_values: Sequence[str | None],
    dest_values: Sequence[str | None],
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed heal assess mismatch-tail decision (slice 4, C2).

    Exact-type input gates: each triple an exact ``list`` of exactly
    :data:`_HEAL_ARTIFACT_SLOTS` entries, every entry ``None`` or an exact
    ASCII ``str`` (sha256 hex by construction — a non-ASCII "hash" from a
    hand-built plan refuses to the oracle rather than risking a
    lone-surrogate FFI degrade). Consulted ONLY after the Python happy arm
    (``src == plan`` → ``move_trio``) failed — dest hashing stays LAZY —
    and the ``refuse`` token maps back to the Python-owned C2 refusal
    MESSAGE; the destructive quarantine MOVE itself never routes.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(_heal_triple_refused(values) for values in (src_values, plan_values, dest_values)):
        _warn_once("heal_mismatch_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_HEAL_MISMATCH,
        "heal_mismatch_decision",
        (src_values, plan_values, dest_values),
        python_law=python_law,
        validate=_validate_heal_mismatch,
    )


def _scheduler_int_refused(value: int) -> bool:
    """``True`` when a scheduler int operand may NOT cross the FFI."""
    return type(value) is not int or abs(value) > _SCHEDULER_EXACT_BOUND


def _scheduler_clock_refused(value: float) -> bool:
    """``True`` when a scheduler clock operand may NOT cross the FFI."""
    return (
        type(value) is not float or not math.isfinite(value) or abs(value) > _SCHEDULER_EXACT_BOUND
    )


def routed_schedule_next_fire(
    is_interval: bool,
    interval_seconds: int,
    second_of_day: int,
    tz_offset_seconds: int,
    now: float,
    last_fire: float | None,
    *,
    python_law: Callable[[], float],
) -> float:
    """The routed scheduler next-fire clock arithmetic (slice 4).

    Exact-type input gates: *is_interval* exact ``bool``; the three schedule
    fields exact ``int`` and the clock operands exact FINITE ``float``, each
    within :data:`_SCHEDULER_EXACT_BOUND` (|value| ≤ 2^50) — inside that
    range every daily-arm intermediate (the 3-term ``day_start +
    second_of_day − tz`` sum ≤ 2^52, then two ``+ SECONDS_PER_DAY`` rollovers
    < 2^53) is an exactly-representable integer, so the crate's f64 daily
    arithmetic is value-identical to the Python oracle's exact INT
    arithmetic. The OUTPUT gate reconstructs the daily
    arm's ``int`` return TYPE (the fire epoch is ``json.dumps``-rendered
    into the daemon's audit payload — int vs float bytes differ), so the
    audit bytes stay identical either arm.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        type(is_interval) is not bool
        or any(
            _scheduler_int_refused(value)
            for value in (interval_seconds, second_of_day, tz_offset_seconds)
        )
        or _scheduler_clock_refused(now)
        or (last_fire is not None and _scheduler_clock_refused(last_fire))
    ):
        _warn_once("schedule_next_fire_input_refused")
        return python_law()

    def _validate_fire(raw: object) -> float | None:
        if type(raw) is not float or not math.isfinite(raw):
            return None
        if is_interval:
            return raw
        if not raw.is_integer():
            return None
        return int(raw)  # int-in-float slot — the PEP 484 numeric tower

    return _routed_law_call(
        _LAW_SCHEDULE_NEXT_FIRE,
        "schedule_next_fire",
        (is_interval, interval_seconds, second_of_day, tz_offset_seconds, now, last_fire),
        python_law=python_law,
        validate=_validate_fire,
    )


def _validate_resume_decision(raw: object) -> tuple[str, int] | None:
    """Structural OUTPUT gate for the resume ``(token, index)`` pair."""
    if not (isinstance(raw, tuple) and len(raw) == _STRING_PAIR_ARITY):
        return None
    token, index = raw
    if type(token) is not str or token not in _RESUME_TOKENS or type(index) is not int:
        return None
    return (token, index)


def routed_resume_decision(
    exists: bool,
    sweep_id_matches: bool,
    next_index: int,
    total_combos: int,
    *,
    python_law: Callable[[], tuple[str, int]],
) -> tuple[str, int]:
    """The routed scheduler resume classification (slice 4).

    Exact-type input gates: the two flags exact ``bool``, the two indices
    exact ``int`` INSIDE the :func:`_i64_refused` band. Returns the 4-way
    token + resume index; the human-readable REASON strings — which differ
    between the two FRESH arms — are rendered Python-side from the token,
    byte-identical either arm.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(type(flag) is not bool for flag in (exists, sweep_id_matches)) or any(
        _i64_refused(value) for value in (next_index, total_combos)
    ):
        _warn_once("resume_decision_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_RESUME_DECISION,
        "resume_decision",
        (exists, sweep_id_matches, next_index, total_combos),
        python_law=python_law,
        validate=_validate_resume_decision,
    )


# ─────────────────── slice 5 — the verify/apriori family ───────────────────


def _validate_recount_verdict(raw: object) -> str | None:
    """Structural OUTPUT gate for the recount classification token."""
    if type(raw) is not str or raw not in _RECOUNT_VERDICT_TOKENS:
        return None
    return raw


def routed_recount_verdict(
    vix_attributable: bool,
    is_grid_tuple: bool,
    context_match: bool,
    cell_evidence: bool,
    grid_corrupt_leaderboard: bool,
    *,
    python_law: Callable[[], str],
) -> str:
    """The routed recount post-byte-diff classification tail (slice 5).

    Exact-type input gates: all five flags exact ``bool``. The VIX
    attribution itself (`ResultsStore` row loads + ``replace()``/``getattr``
    introspection) is computed Python-side and crosses as ONE boolean; the
    ``grid_corrupt_leaderboards`` side effect and every finding
    code/severity/detail BYTE stay with ``compare_recount``, keyed off the
    token — and the run-identity boundary seams
    (``canonical_trade_params`` / ``mask_run_id``) never cross the FFI.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(
        type(flag) is not bool
        for flag in (
            vix_attributable,
            is_grid_tuple,
            context_match,
            cell_evidence,
            grid_corrupt_leaderboard,
        )
    ):
        _warn_once("recount_verdict_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_RECOUNT_VERDICT,
        "recount_verdict",
        (vix_attributable, is_grid_tuple, context_match, cell_evidence, grid_corrupt_leaderboard),
        python_law=python_law,
        validate=_validate_recount_verdict,
    )


def _apriori_frontier_refused(frontier: Sequence[tuple[int, ...]]) -> bool:
    """``True`` when an Apriori frontier may NOT cross the FFI (gate helper).

    The crate's contiguous-prefix bucketing and bit-drop subset walk are
    equivalent to the oracle's insertion-ordered dict bucketing and
    index-drop walk ONLY over the constructed frontier shape — uniform-length
    tuples of strictly-ascending bits in ``[0, 128)``, strictly lex-sorted
    (so equal prefixes are contiguous and no duplicates exist). Anything
    else refuses to the oracle.
    """
    if type(frontier) is not list or not frontier:
        return type(frontier) is not list  # an empty frontier is fine (crate no-op)
    length = len(frontier[0]) if type(frontier[0]) is tuple else -1
    if length < 1:
        return True
    for tuple_ in frontier:
        if type(tuple_) is not tuple or len(tuple_) != length:
            return True
        previous = -1
        for bit in tuple_:
            if type(bit) is not int or not previous < bit < _APRIORI_MAX_BIT:
                return True
            previous = bit
    return any(not frontier[i] < frontier[i + 1] for i in range(len(frontier) - 1))


def _validate_apriori_join(raw: object) -> tuple[list[tuple[int, ...]], int | None] | None:
    """Structural OUTPUT gate for the join ``(candidates, tripped)`` pair."""
    if not (isinstance(raw, tuple) and len(raw) == _STRING_PAIR_ARITY):
        return None
    candidates_raw, tripped = raw
    if type(candidates_raw) is not list:
        return None
    if tripped is not None and (type(tripped) is not int or tripped < 1 or candidates_raw != []):
        return None
    candidates: list[tuple[int, ...]] = []
    for candidate in candidates_raw:
        if type(candidate) is not list or any(type(bit) is not int for bit in candidate):
            return None
        candidates.append(tuple(candidate))
    return (candidates, tripped)


def routed_apriori_join_prune(
    frontier: Sequence[tuple[int, ...]],
    drop_conflicts: bool,
    conflict_masks: Sequence[int],
    n_bars: int,
    limit: int,
    stride: int,
    *,
    python_law: Callable[[], tuple[list[tuple[int, ...]], int | None]],
) -> tuple[list[tuple[int, ...]], int | None]:
    """The routed Apriori join + subset-prune level fold (slice 5).

    Exact-type input gates: *frontier* per :func:`_apriori_frontier_refused`
    (the constructed shape the crate's contiguous bucketing mirrors);
    *drop_conflicts* exact ``bool``; *conflict_masks* an exact ``tuple`` of
    exact ``int`` pair-masks in ``[1, 2^64)``; *n_bars*/*limit*/*stride*
    exact ``int`` in u64 range (``limit``/``stride`` ``>= 1``). The strided
    mask·bar budget trip comes back as a VALUE (never an exception the
    facade would degrade away) — the caller raises its own
    ``SweepAprioriBudgetError`` bytes from the count. HONEST cost: the gate
    + marshaling are O(frontier·k) per level — the same order as the join
    itself, and never the ~33B-trial mask/bar hot loop.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if (
        _apriori_frontier_refused(frontier)
        or type(drop_conflicts) is not bool
        or type(conflict_masks) is not tuple
        or any(
            type(mask) is not int or not 1 <= mask < _APRIORI_MASK_CEILING
            for mask in conflict_masks
        )
        or any(type(value) is not int for value in (n_bars, limit, stride))
        or not 0 <= n_bars < _U64_CEILING
        or not 1 <= limit < _U64_CEILING
        or not 1 <= stride < _U64_CEILING
    ):
        _warn_once("apriori_join_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_APRIORI_JOIN,
        "apriori_join_prune",
        (frontier, drop_conflicts, conflict_masks, n_bars, limit, stride),
        python_law=python_law,
        validate=_validate_apriori_join,
    )


def _validate_stride(raw: object) -> int | None:
    """Structural OUTPUT gate for the record-stride fold (exact int >= 1)."""
    return raw if type(raw) is int and raw >= 1 else None


def _validate_nonneg_int(raw: object) -> int | None:
    """Structural OUTPUT gate for a non-negative exact-int fold result."""
    return raw if type(raw) is int and raw >= 0 else None


def routed_record_stride(total_tuples: int, *, python_law: Callable[[], int]) -> int:
    """The routed D-9 TUPLE_DONE decimation-stride fold (slice 6).

    Exact-type input gate: *total_tuples* an exact ``int`` within the
    exactly-representable f64 band (|v| <= 2^50 — the shared
    :func:`_scheduler_int_refused` band law): the Python oracle's
    ``math.ceil(t / 10_000)`` is TRUE (f64) division, so the crate mirrors
    it in f64 and parity is IEEE-exact only inside the band; a count
    beyond it refuses to the bigint oracle. Everything else in the #1712
    run core — the context manager, flocks, keep-awake, flight recorder,
    G12 breaker, durable writes — stays Python.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if _scheduler_int_refused(total_tuples):
        _warn_once("record_stride_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_RECORD_STRIDE,
        "tuple_record_stride",
        (total_tuples,),
        python_law=python_law,
        validate=_validate_stride,
    )


def routed_diffcheck_budget(
    k_per_run: int, total_tuples: int, *, python_law: Callable[[], int]
) -> int:
    """The routed R45 per-tuple diffcheck-budget fold (slice 6, design D-11).

    Exact-type input gates: both operands exact ``int`` within the shared
    |v| <= 2^50 band. Post-clamp both operands are positive, where the
    Python ``//`` floor division ≡ Rust integer division exactly. The R45
    never-adopt law is untouched — this distributes the K budget, the
    diffcheck itself stays the independent Python reference simulator.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if _scheduler_int_refused(k_per_run) or _scheduler_int_refused(total_tuples):
        _warn_once("diffcheck_budget_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_DIFFCHECK_BUDGET,
        "per_tuple_diffcheck_budget",
        (k_per_run, total_tuples),
        python_law=python_law,
        validate=_validate_nonneg_int,
    )


def routed_grid_tuple_count(axes: Sequence[int], *, python_law: Callable[[], int]) -> int:
    """The routed grid outer-product tuple-count fold (slice 6).

    Exact-type input gate: *axes* an exact ``tuple`` of exact ``int``
    cardinalities in u64 range (Python owns the ``SweepGridConfig``
    object marshaling — only the pre-extracted lengths cross). The crate
    accumulates in checked u128; a product past u64 raises the crate's
    declared ``ValueError``, which the degrade set folds back to the
    Python bigint oracle (a legitimately astronomical grid is oracle
    territory, never a truncation).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if type(axes) is not tuple or any(
        type(value) is not int or not 0 <= value < _U64_CEILING for value in axes
    ):
        _warn_once("grid_tuple_count_input_refused")
        return python_law()
    return _routed_law_call(
        _LAW_GRID_TUPLE_COUNT,
        "grid_tuple_count",
        (axes,),
        python_law=python_law,
        validate=_validate_nonneg_int,
    )


__all__ = [
    "BURN_IN_SAMPLES",
    "SWEEP_OPS_RUST_ENV",
    "reset_sweep_ops_rust_for_tests",
    "routed_apriori_join_prune",
    "routed_audit_entry_hash",
    "routed_chain_link_state",
    "routed_chain_verify_step",
    "routed_checkpoint_adoption_verdict",
    "routed_checkpoint_record_decision",
    "routed_count_violations",
    "routed_detach_probe_verdict",
    "routed_diffcheck_budget",
    "routed_exit_status_resolution",
    "routed_freshness_stale",
    "routed_gate_fragment",
    "routed_grid_tuple_count",
    "routed_heal_mismatch",
    "routed_liveness_tail",
    "routed_preflight_missing",
    "routed_progress_derived",
    "routed_quarantine_next_ordinal",
    "routed_rank_plan",
    "routed_record_stride",
    "routed_recount_verdict",
    "routed_resume_decision",
    "routed_run_path",
    "routed_schedule_next_fire",
    "routed_sla_decision",
    "routed_stall_timeout",
    "routed_stamp_verdict",
    "sweep_ops_rust_disabled",
    "sweep_ops_rust_enabled",
]
