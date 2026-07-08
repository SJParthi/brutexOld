# Track 1 — Verification Pyramid (9 Layers, 24 Invariants, 3 Oracles)

> **Status:** Locked 2026-05-15 by Agent 2 (Plan agent, hostile-review pass).
> Companion to `TRACK1_BACKTEST_SPEC.md §18`.

This document specifies the verification architecture for Track 1.
**Every guarantee in the spec maps to at least 4 independent tests in
the pyramid.** No single point of failure.

---

## 1. The 9 Guarantees Being Proven

| ID | Guarantee |
|---|---|
| G1 | O(1) hot-path latency (bar lookup, indicator get, Bloom, LMDB, mask AND, result append) |
| G2 | Bit-for-bit reproducibility (same input → identical Parquet SHA256) |
| G3 | No look-ahead bias (at bar N, engine reads only bars 0..N) |
| G4 | Correct PnL (paper_pnl_paisa math is exact integer paisa) |
| G5 | 33B trials per sweep within 2-4 day SLA on 10-core M4 Pro |
| G6 | Dedup uniqueness (SHA256+Bloom+LMDB with TF as part of hash) |
| G7 | Hash-chained audit log integrity (replay produces byte-identical output) |
| G8 | Indian market microstructure handled (pre-open, Muhurat, halts, rebalance) |
| G9 | 52-condition vocabulary correctness (every atom returns its spec'd boolean) |

---

## 2. The 9-Layer Test Pyramid

| Layer | Tool | CI gate | Run time |
|---|---|---|---|
| 1 — Unit | pytest + cargo test | line 100% / branch 100% Python; line 100% / branch 95% Rust | <90s/PR |
| 2 — Property | hypothesis + proptest | per-test budgets (10–200 examples) × 24 invariants, derandomised via the `ci` profile | <5 min/PR |
| 3 — Golden | byte-equality SHA256 | hardcoded fixtures match | <60s/PR |
| 4 — Integration | pytest tests/integration | all pass | <5 min/PR |
| 5 — Benchmark | pytest-benchmark + criterion | O(1) ratio ≤ 1.1 at N=1K..1M | <8 min/PR |
| 6 — Stress | macOS leaks + heaptrack | ~~24h, RSS ±5%~~ RSS/fd/p99 drift flat **(CORRECTED — DEC-SOAK-REAL-TIER-001 #1566: no tier delivers a 24h single run; the nightly is a ~5-min regression gate and the self-hosted soak is ≤ 20 h per dispatch — SEC9-1 GITHUB_TOKEN bound)** | nightly ~5-min gate (`stress-nightly.yml`) + weekly self-hosted multi-hour soak (`soak-selfhosted.yml`) |
| 7 — Smoke | pytest -m smoke | 10-bar/2-combo sanity | <30s pre-push |
| 8 — Fuzz | cargo fuzz + atheris | 10 min/target, no panics | nightly |
| 9 — Mutation | mutmut + cargo-mutants | ≥85% | weekly |

---

## 3. The 24 Property Invariants

Each invariant runs with its per-test `max_examples` budget (typically
10–200; hypothesis default 100). CI loads the repo-registered `ci`
hypothesis profile (`derandomize=True, print_blob=True, deadline=None,
database=None, suppress_health_check=(too_slow,)` — fully explicit, so
environment-invariant: a local `HYPOTHESIS_PROFILE=ci` run is
field-identical to CI; registered in `tests/conftest.py` and selected
via the `HYPOTHESIS_PROFILE` env var), so example generation is a pure
function of each test and a CI failure reproduces on every re-run.
Honesty (DEC-TEST-APPARATUS-DEBTS-001(b), round-2 reconcile): the old
"500 examples / `HYPOTHESIS_SEED`" claim here was false (that env var
never existed) — but GHA property runs were nonetheless ALREADY
implicitly derandomized, because hypothesis ≥6.113 auto-loads its own
built-in `ci` profile whenever a CI env var (`CI`/`GITHUB_ACTIONS`) is
set. The pack's real deltas are therefore: the EXPLICIT env-selectable
in-repo profile (upstream-drift-independent, pinned by
`tests/unit/test_hypothesis_ci_profile.py` incl. a CI-env-stripped
subprocess selection pin) + `HYPOTHESIS_PROFILE=ci` coverage on
verify-m4pro/mutation — NOT first-time PR-path determinism. The
sanctioned behavior change is the REVERSE flip: a CI-env run that does
not set `HYPOTHESIS_PROFILE` (stress-nightly and the other scheduled
arms) now loads the random `default` profile, OVERRIDING hypothesis's
auto-load — deliberate exploration off the PR path (currently vacuous:
no `@given` tests live in those suites today).

| # | Invariant | Where | Proves |
|---|---|---|---|
| P-01 | `combo_hash(C) == combo_hash(canonical(C))` for any parameter permutation | dedup | G6 |
| P-02 | `combo_hash` changes if any single byte of (mask‖dir‖inst‖tf‖mode‖params‖data_hash‖indicator_ver‖commit) changes | dedup | G6 |
| P-03 | Σ per-trade paper_pnl == final_balance − initial_balance (exact paise) | accounting | G4 |
| P-04 | gross_pnl − Σ costs == net_pnl (Track 1: costs=0, so gross == net) | accounting | G4 |
| P-05 | Bloom filter has zero false-negatives (never says "absent" when LMDB has it) | dedup | G6 |
| P-06 | For any bar index N, `evaluate(N)` reads only indices ≤ N (instrumented bar accessor counts forward reads → must be 0) | look-ahead | G3 |
| P-07 | Entry fill price ≥ bar.high (BUY) or ≤ bar.low (SELL) — pessimistic mode (held in reserve for Phase 2) | simulator | G4 |
| P-08 | Resample (1m→Tm) is associative: `resample(resample(x,2),3) ≡ resample(x,6)` when 09:15 anchor aligns | resampler | G4 |
| P-09 | Forced-exit invariant: no trade survives 15:20 IST | simulator | G4 |
| P-10 | STT applied only on SELL leg | costs | G4 |
| P-11 | Bitwise mask AND on u64 vocabulary is commutative & associative | signal | G6 |
| P-12 | Brokerage is flat ₹20 per executed order (₹40 round-trip), invariant in notional/lots — the degenerate flat case of "monotone non-decreasing until floor, then flat" (the old `min(₹20, 0.03% notional)` wording here was the stale equity-delivery formula; the verified F&O law is flat per order — `docs/track2/CALCULATOR_SPEC.md` §4 Step 4, `docs/track2/COSTS_VERIFIED.md` §8.1-8.2) | costs | G4 |
| P-13 | EMA(α, x) is a contraction: `‖EMA(α,x)−EMA(α,y)‖∞ ≤ ‖x−y‖∞` | indicators | G4 |
| P-14 | Pivot/Fib levels are pure functions of (PDH, PDL, PDC) — no intraday state leak | indicators | G3 |
| P-15 | Continuous-futures back-adjustment (held in reserve for Phase 2) is multiplicative & invertible: round-trip ratio == 1.0 ± 1 tick | data | G4 |
| P-16 | Audit chain `verify_chain(file)` count == #append() calls | audit | G7 |
| P-17 | Any single-byte tamper on JSONL audit line → `AuditChainCorruption` | audit | G7 |
| P-18 | Rate limiter: 1000 calls at 10 r/s complete in ≥ 1000/10 s (Groww-tuned) | adapter | G8 |
| P-19 | Parquet writer is deterministic: `bytes(write(df)) == bytes(write(df))` across two processes | reproducibility | G2 |
| P-20 | `replay(audit_log) → Parquet` byte-equals the original Parquet | reproducibility | G2, G7 |
| P-21 | Tick rounding: every persisted price is a multiple of 5 paise | data | G4 |
| P-22 | Holiday calendar: no bar timestamp on any NSE holiday 2021-2026 | microstructure | G8 |
| P-23 | Session window: every persisted intraday bar has 03:45 ≤ utc_minute_of_day ≤ 10:00 UTC (= 09:15-15:30 IST) | microstructure | G8 |
| P-24 | Bitmask AND result is independent of evaluation order across worker permutations | concurrency | G6 |

---

## 4. Golden Test Corpus

Located at `tests/golden/fixtures/`. Each fixture is paired with an
expected SHA256 in `tests/golden/expected_sha256.txt`.

| Fixture | Purpose |
|---|---|
| `groww_intraday_2024-01-02_NIFTY.json` | Post-budget gap day; canonical 1m → Parquet conversion |
| `groww_intraday_2024-02-01_NIFTY.json` | NSE Budget Day 2024 half-session (09:15-12:30 IST); bar count must reject post-12:30 entries. (Prior version of this row cited "2024-04-12 Ram Navami half-session" — fixed in DEC-P0-042: Ram Navami 2024 was 2024-04-17 *full* holiday, and 2024-04-12 was a regular Friday — see `brutex/calendars/nse.py` HOLIDAYS_2024.) |
| `groww_intraday_2024-11-01_NIFTY.json` | Diwali Muhurat — anomalous bars, post-15:20 hours |
| `groww_intraday_2024-05-20_NIFTY.json` | Election holiday (BSE/NSE shut) — must reject the bar |
| `groww_intraday_2025-03-14_NIFTY.json` | Holi (Friday holiday); PDH/PDL for Monday must skip to Thursday |
| `indicator_2024-06-03_NIFTY_1m.json` | TradingView-pinned EMA/pivot/Fib values, within 0.5% |
| `condition_truth_table.csv` | 52 atomic conditions × N input snippets → expected booleans |
| `paper_pnl_12fill.csv` | 12 fill scenarios with exact paisa expected (Track 1 cost-free version) |
| `e2e_smoke_2024-09-19.parquet` | 1 day, NIFTY, 4 conditions, modes A+B → frozen output |

---

## 5. Benchmark Tests (O(1) Proofs)

Run at N ∈ {1K, 10K, 100K, 1M}. Each must satisfy:
```
p99_latency(N=1M) / p99_latency(N=1K) ≤ 1.1
```

| Hot path | Benchmark | Pass criterion |
|---|---|---|
| Bar lookup by ts | `bench_bar_lookup` | ratio ≤ 1.1; absolute p99 < 1 µs at 1M |
| Indicator value get | `bench_indicator_get` | ratio ≤ 1.1; absolute p99 < 50 ns at 1M |
| Bloom dedup pre-check | `bench_bloom_check` | p99 < 200 ns at all N |
| LMDB dedup confirm | `bench_lmdb_confirm` | ratio < 3× (existing relaxed gate); absolute p99 < 100 µs |
| Strategy mask u64 AND | `bench_mask_and` | constant ~1 ns; ratio ≤ 1.05 |
| Result Parquet append | `bench_result_append` | amortised < 5 µs/row at all N |

**Throughput SLA** (`bench_full_sweep_extrapolation`): run 100K trials
on the M4 Pro reference, multiply, assert projected 33B/(2-4 days) ≥
target. Nightly only — too slow for per-PR.

---

## 6. The 3 Oracle Tests

These are the single most important tests in the entire pyramid.
Without these, "correctness" cannot be claimed.

### 6.1 Audit-log replay (`tests/integration/test_replay_audit_log.py`)

```
input:  audit/audit.jsonl from a completed sweep
action: re-run the orchestrator driven by the audit log
output: results.parquet — must be byte-identical (SHA256 equal) to
        the original results.parquet
```

**Proves:** G2 (reproducibility) AND G7 (audit chain integrity) jointly.

### 6.2 Python-Rust parity (`tests/integration/test_python_rust_parity.py`)

```
input:  1 day of NIFTY 1m bars, 50 brute-force combos, mode A and B
action:
  - Run via naive pure-Python reference engine (slow, obviously correct,
    ~50 lines of code, no optimisations, hand-readable)
  - Run via Rust engine (production hot path)
output: numeric columns of both Parquets must be byte-identical;
        derived metrics (Sharpe etc.) within ±1 paisa
```

**Proves:** G4 (PnL correctness) AND G9 (vocabulary semantics).

### 6.3 52-condition spec table (`tests/golden/test_condition_vocabulary.py`)

```
input:  conditions_spec.csv with rows (snippet_id, condition_id,
        expected_bool)
        + a fixed test-snippet library at tests/golden/snippets/
action: For each row, load the snippet, evaluate the named condition,
        compare to expected_bool
output: 100% match required
```

**Proves:** G9 (vocabulary semantics) at the granularity of the
individual atomic condition.

---

## 7. Layer-by-Layer Verification Matrix

| Guarantee | Unit (L1) | Property (L2) | Golden (L3) | Integration (L4) | Benchmark (L5) | Stress (L6) | Fuzz (L8) | Mutation (L9) |
|---|---|---|---|---|---|---|---|---|
| **G1 O(1)** | indicator+bar accessors | P-11, P-24 | — | smoke combo | ALL 6 hotpath ratios | multi-hour sustained latency p99 flat (weekly soak, ≤ 20 h/dispatch — #1566) | — | mask + lookup mutants |
| **G2 Reproducibility** | Parquet writer unit | P-19, P-20 | corpora #1, #9 SHA256 frozen | **replay_audit_log** | — | two-machine identity (nightly gate + weekly soak tier — #1566) | Parquet reader fuzz | writer mutants |
| **G3 No look-ahead** | bar iterator unit | P-06 instrumented accessor | — | reference strategies trace test | — | — | — | iterator mutants |
| **G4 Correct PnL** | costs.py 100% branch | P-03, P-04, P-08, P-09, P-13, P-14, P-21 | corpus #8 (12-fill paisa) | **python_rust_parity** | — | — | — | costs.py mutants |
| **G5 33B/2-4 days** | per-trial cost unit (criterion micro-bench) | — | — | 100K micro-sweep correctness | bench_full_sweep_extrapolation projection | multi-hour sustained throughput (weekly soak — #1566) | — | mutation on cost-counter helpers |
| **G6 Dedup uniqueness** | hash function unit | P-01, P-02, P-05, P-11, P-24 | corpus #9 | resume_from_kill | LMDB O(1) | — | LMDB key fuzz | hash mutants |
| **G7 Audit chain** | audit.py 100% branch | P-16, P-17 | — | **replay_audit_log** | — | multi-hour log size + verify (weekly soak — #1566) | JSONL reader fuzz | audit mutants |
| **G8 Microstructure** | calendar + session unit | P-22, P-23, P-18 | corpora #1-5 | calendar integration | — | — | calendar TOML fuzz | calendar mutants |
| **G9 52-condition vocab** | each atom unit | P-11 + per-condition determinism | **condition truth table** (corpus #7) | python_rust_parity | — | — | DSL fuzz | strategy crate mutants |

**Every guarantee has at least 4 columns with X marks.** No single
point of failure.

---

## 8. CI Gate Wiring

The current `.github/workflows/ci.yml` runs Layers 1-5 + parts of 7-9
on every PR. After Track 1 lands, the gates evolve to:

| Workflow | Trigger | Layers |
|---|---|---|
| `ci.yml` | every PR | L1, L2, L3, L4, L5, L7 |
| `nightly.yml` (new) | 02:00 UTC nightly | L6, L8 |
| `weekly.yml` (new) | Sunday 02:00 UTC | L9 mutation |
| `release.yml` | tag push | full pyramid + release artifacts |

Coverage gates raised from current `fail_under = 90` to `fail_under =
100` once Track 1 codepaths land.

---

## 9. What MUST Exist Before Shipping Track 1

1. **`tests/golden/fixtures/`** populated with all 9 corpora
2. **`tests/integration/test_replay_audit_log.py`** green
3. **`tests/integration/test_python_rust_parity.py`** green
4. **`tests/bench/test_hotpath_o1.py`** with all 6 ratios < 1.1 at 1M
5. **Property test count ≥ 24**, derandomised in CI via the `ci`
   hypothesis profile (`HYPOTHESIS_PROFILE=ci` → `derandomize=True`,
   registered in `tests/conftest.py`)
6. **`cargo fuzz` corpora** for all 7 fuzz targets, seeded from real Groww responses
7. **Coverage gates raised** in `pyproject.toml` to `fail_under = 100`
8. **Mutation 85% gates** wired in `.github/workflows/mutation.yml`

This is the verification bar for the Track 1 ship gate.
