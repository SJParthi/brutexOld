# Phase 1 — Verification Scaffolding

## Status: ☐ Not started

## Effort: 2-3 weeks
## Owner: Claude (entirely — user provides only sign-off)

## Goal

Build the 9-layer test pyramid as **empty shells** so every Phase 2+
code change is verified from day one. The pyramid is the safety net
that catches bugs, hallucinations, and design drift before they ship.

Plus: implement `brutex doctor` CLI, session-start hook, and the 3
cheap Phase 1 fixes flagged in the runtime audit.

## Prerequisites

- [x] Phase 0 complete (3 PRs merged on main)
- [ ] Read `docs/TRACK1_VERIFICATION_PYRAMID.md` in full
- [ ] Read `docs/TRACK1_RUNTIME_ARCHITECTURE.md` §6 (bootstrap)
- [ ] Read `docs/MIGRATION.md` decisions #88-#102 (Phase 1 cheap fixes)
- [ ] User confirms "start Phase 1"

## Checklist

### Week 1 — Test pyramid scaffolding

#### Property tests (Layer 2)

- [ ] Create `tests/property/test_invariants.py` with 26 stubs:
  - [ ] P-01 — `combo_hash(C) == combo_hash(canonical(C))`
  - [ ] P-02 — `combo_hash` changes if any single byte of 9 inputs changes
  - [ ] P-03 — Σ paper_pnl_paisa == final_balance − initial_balance (exact paise)
  - [ ] P-04 — gross_pnl − Σ costs == net_pnl (Track 1: costs=0)
  - [ ] P-05 — Bloom filter has zero false-negatives
  - [ ] P-06 — Look-ahead: forward reads must be 0
  - [ ] P-07 — Entry fill pessimistic mode (reserved for Phase 2)
  - [ ] P-08 — Resample associative: `resample(resample(x,2),3) ≡ resample(x,6)`
  - [ ] P-09 — Forced-exit invariant: no trade survives 15:20 IST
  - [ ] P-10 — STT applied only on SELL leg (Phase 2)
  - [ ] P-11 — Bitwise mask AND commutative & associative
  - [ ] P-12 — Brokerage formula monotone (Phase 2)
  - [ ] P-13 — EMA contraction property
  - [ ] P-14 — Pivot/Fib levels pure functions
  - [ ] P-15 — Continuous-futures back-adjustment (Phase 2)
  - [ ] P-16 — Audit chain count matches append count
  - [ ] P-17 — Single-byte tamper raises AuditChainCorruption
  - [ ] P-18 — Rate limiter timing
  - [ ] P-19 — Parquet writer deterministic
  - [ ] P-20 — replay(audit_log) → byte-identical Parquet
  - [ ] P-21 — Persisted price is multiple of 5 paise
  - [ ] P-22 — No bar timestamp on NSE holiday 2021-2026
  - [ ] P-23 — Every intraday bar within session window
  - [ ] P-24 — Bitmask AND independent of worker evaluation order
  - [ ] P-25 — Stage 2.5 posterior monotone (NEW, decision #100)
  - [ ] P-26 — Stage 2.5 replay reproduces decisions byte-identically (NEW, decision #100)
- [ ] Each stub is `@hypothesis.given(...)` body with `pytest.skip("Phase 2")` or similar
- [ ] CI gate: `pytest --co tests/property` collects all 26 invariants
- [ ] Coverage badge updated to show 26 invariant count

#### Golden tests (Layer 3)

- [ ] Create `tests/golden/fixtures/` directory
- [ ] Add placeholder fixtures (filled in Phase 2):
  - [ ] `dhan_intraday_2024-01-02_NIFTY.json` (placeholder)
  - [ ] `dhan_intraday_2024-04-12_NIFTY.json` (Ram Navami half-session)
  - [ ] `dhan_intraday_2024-11-01_NIFTY.json` (Diwali Muhurat)
  - [ ] `dhan_intraday_2024-05-20_NIFTY.json` (election holiday)
  - [ ] `dhan_intraday_2025-03-14_NIFTY.json` (Holi)
  - [ ] `indicator_2024-06-03_NIFTY_1m.json` (TradingView pin)
  - [ ] `condition_truth_table.csv` (skeleton with 5 example rows)
  - [ ] `paper_pnl_12fill.csv` (skeleton with exact-paisa expected)
  - [ ] `e2e_smoke_2024-09-19.parquet` (placeholder)
- [ ] `tests/golden/expected_sha256.txt` skeleton file
- [ ] `tests/golden/test_condition_vocabulary.py` skeleton

#### Integration / Oracle tests (Layer 4)

- [ ] `tests/integration/test_replay_audit_log.py` skeleton
- [ ] `tests/integration/test_python_rust_parity.py` skeleton
- [ ] `tests/integration/test_stage2_5_no_winner_loss.py` skeleton (NEW, A/B oracle per decision #100)

#### Bench tests (Layer 5)

- [ ] `tests/bench/test_hotpath_o1.py` skeleton with 6 hot-path stubs
- [ ] `tests/bench/test_full_sweep_extrapolation.py` skeleton (Phase 3 canary)
- [ ] `tests/bench/test_metal_kernel_speedup.py` skeleton (Phase 3.5 canary, decision #101)

#### Stress + Smoke tests (Layers 6 + 7)

- [ ] `tests/stress/test_24h_sustained.py` skeleton
- [ ] `tests/smoke/test_pre_push.py` skeleton

### Week 2 — Mutation + Fuzz + CI scaffolding

#### Mutation testing

- [ ] `.cargo/mutants.toml` config — wires the 85% gate
- [ ] `pyproject.toml`: add `mutmut` config — 85% gate
- [ ] `.github/workflows/weekly.yml` (new) — mutation testing weekly
- [ ] Initial mutmut run on `brutex/orchestrator/audit.py` (already exists)

#### Fuzz testing

- [ ] `rust/fno-math/fuzz/` — 7 cargo-fuzz target stubs:
  - [ ] `fuzz_target_1_indicator_inputs`
  - [ ] `fuzz_target_2_resample_boundaries`
  - [ ] `fuzz_target_3_audit_jsonl_parser`
  - [ ] `fuzz_target_4_calendar_toml`
  - [ ] `fuzz_target_5_lmdb_keys`
  - [ ] `fuzz_target_6_parquet_reader`
  - [ ] `fuzz_target_7_dsl_strategy_mask`
- [ ] `tests/fuzz_atheris/` for Python fuzz targets
- [ ] `.github/workflows/nightly.yml` (new) — stress + fuzz

#### Coverage gate

- [ ] `pyproject.toml`: raise `[tool.coverage.report]` `fail_under = 100`
- [ ] Exempt Phase 2+ code paths until they land (use `[tool.coverage.run] omit`)
- [ ] CI gate: any commit that drops coverage below 100% on covered modules fails

### Week 3 — brutex doctor + bootstrap + runbook + cheap fixes

#### brutex doctor CLI (decision #89)

- [ ] `brutex/cli.py`: add `doctor` subcommand (next to `audit verify`)
- [ ] `brutex/doctor/` package — 11 C-checks
  - [ ] `c1_watermark_freshness.py`
  - [ ] `c2_gap_last7d.py`
  - [ ] `c3_parquet_lmdb_parity.py`
  - [ ] `c4_audit_chain_intact.py`
  - [ ] `c5_dedupe_hit_rate.py`
  - [ ] `c6_rate_limit_headroom.py`
  - [ ] `c7_resample_determinism.py`
  - [ ] `c8_disk_free.py`
  - [ ] `c9_telegram_delivery.py` (SKIP per decision #78)
  - [ ] `c10_instrument_master_fresh.py` (SKIP — securityIds hardcoded per #87)
  - [ ] `c11_throughput_canary_fresh.py` (NEW per decision #98)
- [ ] `brutex/doctor/runner.py` — orchestrates checks, prints status table
- [ ] Unit tests for each check (mock fixtures)
- [ ] Smoke test: `brutex doctor` returns exit 0 when no lake yet

#### Session-start hook (decision #88)

- [ ] `scripts/ci/session_bootstrap.sh` — the 5-step hook
- [ ] `.claude/settings.json`: add `SessionStart` array
- [ ] Test: timing < 5 sec on M4 Pro
- [ ] Smoke test: hook runs without errors when no lake mounted

#### 4-agent hostile review runbook (decision #70)

- [ ] `docs/runbooks/4-agent-review.md` — the canonical pattern
  - [ ] Audit / Explore agent prompt template
  - [ ] Architecture / Plan agent prompt template
  - [ ] Hostile / general-purpose agent prompt template
  - [ ] Reality-check / general-purpose agent prompt template
- [ ] Examples from PR #12, #13, #14 audits (already done, just document them)

#### Phase 1 cheap fixes (decisions #92, #93, #94)

- [ ] **Decision #92**: Widen `combo_hash` signature to 9 inputs per spec §14
  - [ ] Update `brutex/strategies/combo.py` (if it exists)
  - [ ] Add `indicator_version` field
  - [ ] Property test: P-02 1-byte tamper detects
- [ ] **Decision #93**: Codify Result Parquet `dataclass`
  - [ ] Create `brutex/strategies/result.py` with dataclass
  - [ ] All fields from `TRACK1_RUNTIME_ARCHITECTURE.md §3.7`
  - [ ] Unit test: serialise + deserialise round-trip
- [ ] **Decision #94**: WorkItem content_hash invariant property test
  - [ ] Add to `tests/property/`: `content_hash == sha256(canonical(params))` for every stored item

#### MCP servers (decision #90)

- [ ] `~/.claude/mcp.json` per-project — duckdb + lmdb wrappers
  - [ ] `brutex/mcp/duckdb_server.py` (thin readonly wrapper)
  - [ ] `brutex/mcp/lmdb_server.py` (thin readonly wrapper)
  - [ ] filesystem + github + prometheus already wired
- [ ] Document setup in `docs/runbooks/mcp-server-setup.md`

### Phase 1 sign-off

- [ ] Run all 26 property tests — should collect (most skip with Phase 2 message)
- [ ] Run all 4 oracle test skeletons — should skip cleanly
- [ ] Run all 6+3 bench tests — should produce 0 trials (skeletons)
- [ ] Run all 7 fuzz targets — should compile (empty)
- [ ] `brutex doctor` returns clean exit
- [ ] Coverage 100% on what code exists
- [ ] Mutation 85% on existing modules
- [ ] CI 22 gates green
- [ ] **Spawn 4-agent hostile review on Phase 1 deliverables** (per runbook)
- [ ] Hostile review findings documented + fixed
- [ ] Phase 1 exit checklist complete
- [ ] **User approves Phase 1 complete** → ready to start Phase 2

## Verification gates (must all pass before sign-off)

- [ ] `pytest --co tests/property` shows 26 tests
- [ ] `pytest --co tests/integration` shows 3 oracles + 1 (Stage 2.5 A/B)
- [ ] `pytest --co tests/bench` shows 6 hot-paths + 2 canaries
- [ ] `pytest --co tests/fuzz_atheris` shows 7 targets
- [ ] `brutex doctor` exits 0 (all GREEN or SKIP)
- [ ] `ruff check brutex/ tests/` clean
- [ ] `mypy --strict brutex/` clean
- [ ] Coverage 100% on covered modules
- [ ] All 22 CI gates green
- [ ] 4-agent hostile review report committed
- [ ] MIGRATION decisions #88-#94 cross-reference real files

## Exit criteria

- [ ] All 9 test layers have ≥1 file
- [ ] All 26 property invariants stubbed (skip-with-message)
- [ ] All 4 oracle test skeletons present
- [ ] `brutex doctor` runs and produces status table
- [ ] Session-start hook configured + tested
- [ ] 4-agent review runbook written + 3 historical examples cited
- [ ] 3 cheap fixes (combo_hash widen, result schema, content_hash invariant) all in
- [ ] CI 22 gates green on Phase 1 PR
- [ ] Phase 1 hostile review report green
- [ ] User signs off

## Files to create

```
docs/phases/phase_1_verification_scaffolding.md   (this file — updated each commit)
docs/runbooks/4-agent-review.md                    (new)
docs/runbooks/mcp-server-setup.md                  (new)
tests/property/test_invariants.py                  (new, 26 stubs)
tests/golden/fixtures/*.json                       (placeholders)
tests/golden/expected_sha256.txt                   (new)
tests/golden/test_condition_vocabulary.py          (skeleton)
tests/integration/test_replay_audit_log.py         (skeleton)
tests/integration/test_python_rust_parity.py       (skeleton)
tests/integration/test_stage2_5_no_winner_loss.py  (skeleton)
tests/bench/test_hotpath_o1.py                     (skeleton)
tests/bench/test_full_sweep_extrapolation.py       (skeleton)
tests/bench/test_metal_kernel_speedup.py           (skeleton)
tests/stress/test_24h_sustained.py                 (skeleton)
tests/smoke/test_pre_push.py                       (skeleton)
tests/fuzz_atheris/*.py                            (skeletons)
rust/fno-math/fuzz/fuzz_targets/*.rs               (skeletons)
brutex/doctor/__init__.py                          (new)
brutex/doctor/runner.py                            (new)
brutex/doctor/c1_*.py through c11_*.py             (new, 11 checks)
brutex/strategies/result.py                        (new dataclass)
brutex/strategies/combo.py                         (widened signature)
brutex/mcp/duckdb_server.py                        (new)
brutex/mcp/lmdb_server.py                          (new)
scripts/ci/session_bootstrap.sh                    (new)
.claude/settings.json                              (add SessionStart hook)
.cargo/mutants.toml                                (new)
.github/workflows/nightly.yml                      (new)
.github/workflows/weekly.yml                       (new)
pyproject.toml                                     (coverage gate raised)
```

## Risk register

| Risk | Mitigation |
|---|---|
| Property test stubs not actually testing anything | Each stub MUST use `@hypothesis.given(...)` decorator + `pytest.skip("Phase X")` body; pytest collection catches if decorator is missing |
| brutex doctor false-greens | Unit tests per check with explicit failure-mode fixtures |
| Session-start hook slow (> 5 sec) | Hard time budget per step + CI test that asserts < 5 sec |
| Coverage gate gameable via empty modules | mutation testing catches "code that doesn't matter" |
| 4-agent review pattern degrades to rubber-stamp | Runbook documents BAD past examples (the 10x throughput claim caught) so future agents have anchors |
| MCP server wrappers expose write access by accident | All wrappers `readonly=True` by construction; integration test asserts |

## Decisions to lock (will be added to MIGRATION.md at end)

These will become #103-#110 (estimated):

- #103: Phase 1 verification scaffolding complete (26 invariants, 4 oracles, 11 doctor checks)
- #104: brutex doctor CLI shipped (C1-C11)
- #105: Session-start hook shipped (`scripts/ci/session_bootstrap.sh`)
- #106: 4-agent review runbook written (`docs/runbooks/4-agent-review.md`)
- #107: MCP server wrappers shipped (duckdb + lmdb readonly)
- #108: Phase 1 cheap fixes done (combo_hash + result schema + content_hash invariant)
- #109: Coverage gate raised to `fail_under = 100`
- #110: Phase 1 4-agent hostile review pass

## Phase 1 entry / exit summary

| | Before | After |
|---|---|---|
| Property invariants | 0 stubs | 26 stubs (all collect, most skip) |
| Oracle tests | 0 | 4 skeletons |
| Doctor checks | 0 | 11 implemented |
| Session-start hook | none | 5-step, < 5 sec |
| 4-agent review runbook | absent | written + 3 historical examples |
| MCP servers | github only | + filesystem + duckdb + lmdb + prometheus |
| MIGRATION decisions | 102 | ~110 |
| Coverage gate | 90% | 100% (on covered modules) |
| Phase 2 readiness | n/a | READY (just needs Dhan token) |
