# Engine Driver + Trade-Sim — Scoping Proposal (2026-06-03)

> **STATUS: PROPOSAL — not authorized for implementation.** This is a
> scoping document produced as the requested "next step" after the
> 2026-06-03 institutional-architecture audit
> (`docs/VERIFICATION_2026-06-03_INSTITUTIONAL_ARCH.md`). Per CLAUDE.md §3
> (no silent scope expansion) and §9 (4-agent hostile review before major
> design changes), **no engine code is written until (a) this plan passes a
> §9 hostile review and (b) the operator authorizes a phase.** Nothing here
> edits existing code.

---

## 1. Why this, why now

The audit's blunt finding: the 5-stage funnel is **not wired end-to-end**.
The shipped Rust kernel counts mask hits (`rust/fno-math/src/python.rs:155-223`)
but nothing turns a hit into a trade P&L, and there is no runner. The
single highest-leverage gap — the one that unblocks **both** a real sweep
**and** Stage 2.5 — is:

1. a **Stage-2 trade-simulation walker** (mask hit → round-trip
   `ClosedTrade` → Sharpe), and
2. the **end-to-end driver** that wires Stage 1A precompute → 1B coarse
   filter → 2 backtest → result store.

`brutex/engine/__init__.py:13-23` already names these as the missing
modules ("stage1a, stage1b, stage2, runner — coming"). This proposal makes
that concrete and merge-ready.

This proposal explicitly **excludes** Stages 3/4 and any
SIMD/Rayon/GPU/Metal acceleration — those are separate, later phases. It
also makes **no SLA claim**: even fully built, the single-threaded path
will not meet the 2–4 day / 33 B SLA (the audit measured ~22.8 days raw);
parallelism is a distinct future phase.

---

## 2. The contracts that already exist (reuse, do not reinvent)

| Contract | Where | Reuse for |
|---|---|---|
| u64 mask eval + supermask count | `brutex/engine/combo_mask.py:231,286`; `rust/fno-math/src/python.rs:155-299` | Stage 1A/2 "does bar N fire condition mask M?" |
| BLAKE3 9-field run identity | `brutex/strategies/combo.py:91-180` | Stamp every Stage-2 result row (replaces the `"N/A"` placeholders at `sweep.py:446-450`) |
| Order-fill + P&L accounting | `brutex/execution/simulator.py:66-348` | The fill/cost half of the trade-sim (it exists; it is just not wired to masks) |
| Closed-trade + metric types | `brutex/metrics/types.py`; `brutex/metrics/compute.py:104-530` (Welford) | Round-trip aggregation → Sharpe |
| Hash-chained audit log | `brutex/orchestrator/audit.py` | One audit line per stage transition |
| Replay / no-look-ahead feed | `brutex/feed/` + `ReplayFeed.ensure_no_lookahead` (per CLAUDE.md §4) | Bar-by-bar walk that can only see bars 0..N |
| O(1) bench tier helper | `tests/bench/_o1_tiers.py` | Per-op O(1) gates on the new hot paths |

The point: this is **integration glue**, not greenfield. The two genuinely
new pieces are the **mask→trade bridge** and the **driver FSM**.

---

## 3. Proposed PR breakdown (each independently merge-ready)

> Ordering honors the Merge-Readiness Charter (CLAUDE.md §13): every PR is
> green-gated and reversible; correctness before performance.

- **PR-0 — §9 hostile review of THIS plan (docs-only).** 4-agent
  (Plan + general-purpose + Explore + hostile) review; lock the design,
  resolve open questions (§5), record a `DEC-ENGINE-DRIVER-001` in
  `docs/MIGRATION.md`. No code.
- **PR-1 — `brutex/engine/stage1a.py` (precompute wiring).** Turn ingested
  OHLC + the 52-condition vocabulary into the per-bar u64 bitset matrix the
  kernel consumes; golden + property (no-look-ahead) tests; O(1) bench on
  the bitset build.
- **PR-2 — trade-sim walker `brutex/engine/trade_sim.py`.** The new bridge:
  given a per-bar signal stream (from a mask) + a bar feed, walk
  bar-by-bar applying entry@N+1-open, SL/TP, 15:20 forced exit
  (`brutex/calendars/nse.py:291`), emit round-trip `ClosedTrade`s into the
  existing `brutex/metrics/compute.py`. Bit-exact golden oracle + property
  invariants (P-06 instrumented bar accessor — currently a skip stub).
- **PR-3 — `brutex/engine/stage1b.py` (coarse filter).** Coarse score →
  drop bottom 50%; emits the Sharpe distribution that later becomes the
  Stage 2.5 prior.
- **PR-4 — `brutex/engine/runner.py` + `brutex sweep` CLI.** The FSM that
  wires 1A → 1B → 2 → result store, stamping the BLAKE3 run-identity on
  every row and writing one audit line per transition. This is the first
  point a user can launch an end-to-end (single-instrument, single-TF)
  sweep.
- **PR-5 — result store + reproducibility oracle.** Deterministic Parquet
  out; L3 replay test asserts byte-identical re-run (closes the 🟡 PARTIAL
  at `docs/MIGRATION.md:271`).

Stage 2.5, Stage 3, Stage 4, and parallelism are **out of scope** here and
become later phases once PR-1..PR-5 land green.

---

## 4. Test gates (CLAUDE.md §12 — non-negotiable per PR)

100% line + 100% branch on each new module; 7 test types (unit +
integration + property + golden + bench + stress + mutation); mutation
≥ 85%; `mypy --strict` + `pyright` + `ruff` + `pylint` + `bandit` clean;
O(1) bench (`p99` ratio) on every new hot path; hash-chained audit on every
material action; the no-look-ahead invariant enforced by test (promote
P-06 from skip-stub to live).

---

## 5. Open questions to resolve in PR-0 (before any code)

1. **Mode A/B exit semantics** — confirm the exact SL/TP/forced-exit
   precedence against `docs/TRACK1_BACKTEST_SPEC.md` Stage-2 spec; pin
   tie-breaks (same-bar SL and TP).
2. **Where the walker lives** — pure-Python first (correctness oracle), or
   straight to Rust? Recommendation: Python oracle in PR-2, Rust port as a
   later perf phase, keeping the Python version as the bit-exact parity
   oracle (mirrors the existing kernel pattern at
   `brutex/engine/combo_mask.py:239`).
3. **Look-ahead enforcement** — promote Property P-06 (instrumented bar
   accessor, currently a `pytest.skip` stub) to the live enforcer as part
   of PR-2.
4. **Storage envelope** — reconcile the UNVERIFIED storage-size conflict
   (`docs/TRACK1_REQUIREMENTS_FINAL.md:27`) before PR-5 commits to a result
   layout.

---

## 6. Honest envelope (Golden Rule #10)

- This unlocks a **correct, deterministic, single-instrument sweep** — it
  does **not** meet the 33 B / 2–4 day SLA. That needs Stages 2.5 + 3 + 4
  for trial pruning **and** Rayon/GPU parallelism, all later phases.
- It is a **multi-PR effort** (PR-0..PR-5), each gated; not a single drop.
- "Guaranteed brute-force on a MacBook" remains an **aspiration** until the
  pruning + parallelism phases land; this proposal is the necessary
  foundation, not the finish line.
- Implementation begins only after PR-0's §9 review and explicit operator
  authorization.
