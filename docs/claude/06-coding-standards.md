## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no
  other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` AND `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark`
  (SLA), `mutmut` (mutation), `cargo test`, `proptest`,
  `cargo-mutants`, `cargo fuzz`.
- **Coverage gates:** 100% line + 100% branch on `brutex/` package.
  Mutation score ≥ 85% (Python AND Rust).
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba`
  or Rust.
- **No look-ahead** — at bar N, the engine may only access bars 0..N.
  Enforced TODAY by TWO complementary runtime guards: (1) the
  feed-boundary guard (`ReplayFeed.ensure_no_lookahead`, raises
  `LookAheadError`; wired into the strategy runner — the deprecated
  execution driver, the historical second wiring site, was DELETED with
  the simulator/driver trio per DEC-EXECUTION-TRIO-DELETE-001
  (MIGRATION.md #1550) — integration-tested) — a *timestamp*-based check
  on emitted signals; and
  (2) the named **Property P-06 (instrumented bar accessor)** — now LIVE
  per **DEC-LOOKAHEAD-BARRIER-001** (`brutex/feed/lookahead.py`
  `LookAheadBarrier`): an *index*-based accessor that raises `LookAheadError`
  the moment an `access(i)` reaches index `i > N`. Guard (1) is the real
  strategy-look-ahead defence — it catches a future *signal*. Guard (2), as
  wired into the runner event loop (a default `record=False`
  O(1)-memory barrier on every sweep eval), asserts the **advance/access
  cursor invariant**: both sites `advance_to(step)` then `guard(step, …)` at
  the SAME `step`, so it fires on a feed/barrier desync or a wiring regression
  — it does NOT interpose on the arbitrary bar reads a strategy makes off its
  own state, so it is NOT a second strategy-look-ahead guard at that site
  (DEC-LOOKAHEAD-HONESTY-001). The barrier's index-`> N` teeth are genuinely
  exercised by an `access` at a future index — done by the property tests and
  the manual `brutex verify-lookahead` recheck (which re-confirms forward feed
  ordering + the cursor invariant over a lake slice, not strategy consumption).
  So the two guards are **complementary** (a Phase-2 strengthening of the
  feed-boundary guard), NOT literally redundant coverage of the same failure at
  the same site, and NOT a perf path; the accessor is O(1) per bar access (NOT
  an O(1)-total claim). Promoted from the prior `pytest.skip` ROADMAP stub (was
  corrected per DEC-WORKSPACE-VERIFY-001 entry #398 D2 /
  DEC-DOC-DRIFT-RECONCILE-001). The brute-force **SWEEP path** (which the two
  runner-wired guards above never covered) now has its own three-layer
  defence (DEC-SWEEP-LOOKAHEAD-GUARD-001, MIGRATION.md #1181): structural
  causality (audit-verified — Stage-1A updates prior-bar history AFTER
  evaluation; the trade sim enters at the next bar's open) + PERMANENT
  suffix-independence property gates
  (`tests/property/test_sweep_suffix_independence.py`: Stage-1A bar_bits, the
  resample path, and full-spine trades over bars `0..N` are bit-identical
  whether bars `N+1..` are absent, mutated, or extreme — the layer that
  catches a leak through DERIVED state) + an OPT-IN instrumented barrier
  (`brutex.engine.sweep_lookahead`, env `BRUTEX_SWEEP_LOOKAHEAD_GUARD=1`,
  default OFF) wrapping the sweep's bar window in an index-guarded sequence
  that REUSES `LookAheadBarrier` — honest envelope: it catches an indexed /
  sliced / reversed read AHEAD of the forward consumption front (strongest on
  the first sequential pass), NOT derived-state leaks; OFF returns the
  identical bars object, so the hot path is byte-identical + cost-unchanged
  (identity contract + A/B bench + the existing sweep O(1) gates).
- **Money display = 2 decimals, money-only** (DEC-MONEY-2DP-001) — every
  paisa→rupee display goes through `brutex.money` (`format_inr` /
  `round_money` / `paisa_to_rupees`). NEVER round statistical metrics (Sharpe,
  DSR, p-values, CPCV) or `iv` — they keep full precision or strategy ranking
  breaks. Money amounts (costs + display) are int64 paisa; OHLC/spot/option
  **prices** are stored as Decimal rupees (Parquet Float64), NOT paisa — and
  **snapped to the 2-decimal tick grid at the lake-write boundary**
  (`round_money`, `ROUND_HALF_UP` — never truncation; spot mirrors options) so
  the lake never holds vendor float-noise like `14020.7999`
  (DEC-SPOT-PRICE-2DP-001). See §6 (DEC-PAISA-DOC-RECONCILE-001).

---

