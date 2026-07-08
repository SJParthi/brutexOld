# Track 1 — Requirements Final Review

> ⚠️ **STALE SNAPSHOT (2026-05-18) — DO NOT CITE AS CURRENT STATE.**
> This review predates the Dhan revert (DEC-GROWW-DHAN-REVERT-001) and the
> engine work shipped in PRs #460–#497. It still says "Groww" and lists the
> Rust hot kernel (G-01), Bloom filter (G-02) and Stage 1A/1B/2 (G-03) as
> empty 🔴 gaps — **all three are now BUILT** (`rust/fno-math/src/python.rs`,
> `brutex/orchestrator/bloom.py`, `brutex/engine/stage1a.py`). For the
> current, evidence-backed status use **`docs/WORKSPACE_HEALTH_AND_ROADMAP.md`**
> (DEC-WORKSPACE-HEALTH-REPORT-001) and **`docs/TRACK1_GAP_ANALYSIS.md`**
> (DEC-GAP-ANALYSIS-001). This file is retained as a historical record only.

**Date:** 2026-05-18
**Trigger:** Operator directive — "I need to finalise it dude" — Renaissance-style hostile multi-perspective review before further Phase 3+ work.
**Method:** 4 agents in parallel, each covering 3 dimensions (12 lenses total).
**Baseline read:** Full Track 1 docs + CLAUDE.md (12 Golden Rules + verified facts) + MIGRATION.md (177 decisions).

## Verdict legend

| Mark | Meaning |
|---|---|
| ✅ LOCKED | Verified in code AND docs AND CI; safe to depend on |
| 🟠 PARTIAL-CLAIMED-AS-REAL | Doc says "✅ REAL" but reality is partial; honest envelope broken |
| 🟡 NEEDS VERIFICATION | Plausible, but no cite OR no test OR not measured on target hardware |
| ❌ CONFLICT | Two locked sources disagree |
| 🔴 GAP | Capability required by spec; no code implements it |
| ⚠️ SCOPE-CREEP | Track-2 / F&O / Options residue inside Track 1 package |
| 📋 OPEN-Q | Operator must decide |

---

## §1. Critical CONFLICTS (12 items — must resolve before next code phase)

| # | Requirement | Verdict | Evidence | Risk if wrong |
|---|---|---|---|---|
| C-01 | **Total sweep trial count** | ✅ RESOLVED (DEC-MODE-002, 2026-05-23) | TRACK1_BACKTEST_SPEC.md headline = **730 B** (Mode C removed; was 1.095T = 730B × 1.5 Mode C). PERFORMANCE_BUDGET.md + VERIFICATION_PYRAMID.md "33B" = Stage 2 hot-path subset. Three numbers reconciled: 730B (total) · 33B (Stage 2 hot-path) · 10²-10⁴ (final Parquet rows). DEC-MODE-001 reversed. | Resolved by removing the Mode C 1.5× multiplier per operator directive 2026-05-23. |
| C-02 | **Storage after Zstd-9 compression** | ❌ CONFLICT | Spec: "~50-100 GB"; MIGRATION #203: "2.5 TB compressed for 33B"; derived: 730B × 200B × 5× = 29 TB. Three numbers; only one can be true. | 8 TB SSD blown by 7× → sweep aborts. |
| C-03 | **Throughput canary 50 M/s gate** | ❌ CONFLICT | CLAUDE.md §2 rule 12: "throughput canary ≥ 50 M/s". Actual SLA: 80-150K trials/sec (3 orders of magnitude lower). CI: ratio-only ≤ 8.0 (absolute never asserted). | Gate is permanently red OR permanently green (no one reads); headline metric unfalsifiable. |
| C-04 | **`combo_hash` reproducibility** | ❌ CONFLICT | Spec § Identity mandates 9 inputs: `strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash`. Code at `brutex/strategies/combo.py:60-106` hashes only 4: `strategy_id ‖ params ‖ data_snapshot_hash ‖ code_commit_sha`. **Missing 5 of 9.** | Two combos differing only in direction/instrument/tf/mode collide → reproducibility broken on the first cross-axis sweep. |
| C-05 | **sha256 vs blake3** | ❌ CONFLICT | Spec consistently specifies blake3 (lines 401, 537, 542, 887). Code uses `hashlib.sha256` everywhere. | Cross-component byte-equality replay fails; P-19/P-20 fail. |
| C-06 | **Stage 1B in-sample contamination** | ❌ CONFLICT (math) | `TRACK1_BACKTEST_SPEC.md:349-354` defines coarse_score as `freq × sign-correlation × coverage-penalty` where sign-correlation peeks at next-bar-sign. By construction, masks correlated with future direction are kept. | DSR at Stage 4 cannot undo Stage 1B contamination — half the funnel pruning is mathematically a snooping pre-screen. |
| C-07 | **Bloom filter sizing** | ❌ CONFLICT | MIGRATION #121: "150M-1B survivors, FPR ~0". MIGRATION #255: "may need Cuckoo at 146B survivors". MIGRATION #203: "40 GB borderline 48 GB RAM". Three sizings. | Wrong → OOM mid-sweep OR 56% FPR → silent dedup collisions → audit chain corruption. |
| C-08 | **Dashboard default bind** | ❌ CONFLICT | TRACK1_RUNTIME_ARCHITECTURE.md §6.3: default `127.0.0.1`. Code: `brutex/config/ports.py:59` `DEFAULT_HOST_BIND = "0.0.0.0"` with `# nosec — intentional LAN bind`. Zero authentication. | Audit JSONL + Parquet stats + recent events exposed to anyone on LAN/VPN. |
| C-09 | **Coverage gate "100% line + branch"** | ❌ CONFLICT (within same doc) | STANDARDS_100PCT.md row 1 says "**72.32% actual**" AND §10 line 241 says "**96.03% currently**". pyproject.toml: `fail_under = 90`. Omit list excludes 9 modules. | Headline "100%" is contradicted in the same doc; true coverage measurable only against pure non-omitted core. |
| C-10 | **Phase plan vs branch state** | ❌ CONFLICT | Spec presents 16-week locked plan with engine + GPU + DSR cascade. Live branch: Phase 1 ingest + Phase 2 atoms only. Missing: Stage 1A/1B/2/2.5/3/4 engine, BIPOP-CMA-ES, DSR, CPCV, PBO, Romano-Wolf, walk-forward, lockbox, Rust kernel, Metal GPU, Bloom. | Spec is fiction downstream of "5-stage funnel". |
| C-11 | **92-chunk first-run math** | ❌ CONFLICT (stale spec) | CLAUDE.md §2: "92-chunk first-run + 4-chunk daily" (4 instruments × 22 × 90-day + 4 daily). TRACK1_BACKTEST_SPEC.md:195 still says "3 instruments + 60 calls". CLAUDE.md correct; spec stale. | Easy fix (1 line). Worth resolving. |
| C-12 | **Golden fixture mislabel** | ❌ CONFLICT | TRACK1_VERIFICATION_PYRAMID.md:106-108 lists `groww_intraday_2024-04-12_NIFTY.json` as "Ram Navami half-session". Real Ram Navami 2024 was 2024-04-17. 2024-04-12 was a regular Friday. | Half-day handling test exercises wrong date. |

---

## §2. PARTIAL-CLAIMED-AS-REAL (honest-envelope violations — 8 items)

| # | Requirement | Verdict | Evidence | Risk |
|---|---|---|---|---|
| P-01 | **"100% standards / 18 requirements"** | 🟢 LARGELY CLOSED | Was "9 of 21 ✅ / ~22%" at the original audit; the DEC-STANDARDS-AUDIT-001 → DEC-CROSSWALK-001 → DEC-PYLINT-DROP-001 → DEC-DOCTOR-001 → DEC-STRESS-001 arc ratcheted it to **15 of 21 sub-rows 🟢 ENFORCED; 2 🟡 PARTIAL; 1 📋 ROADMAP; 3 👁️ MANUAL → ~71%** (per `STANDARDS_100PCT.md:67-86`, re-verified 2026-06-08; CLAUDE.md §2 rule 12 reconciled by DEC-DOC-STANDARDS-RECONCILE-001). CLAUDE.md §2 rule 12 still presents all 18 as the binding *target*; the honest-envelope block now cites the real 71%. | Decree-vs-reality gap narrowed from ~60% → ~14% (remaining: 2 partial coverage rows + 1 throughput-runner roadmap). |
| P-02 | **"Mutation score ≥ 85%"** | 🟠 | Covers ONE module (`brutex/orchestrator/state.py` at 98%). 4 prior PRs used `OPERATOR_OVERRIDE` to bypass entirely (DEC-P0-005). | Headline gate covers ~1% of package. |
| P-03 | **"2-4 day SLA at 80-150K trials/sec"** | 🟠 | PERFORMANCE_BUDGET §5: "OPTIMISTIC" tier. Realistic is "20-50K" (7-20 days), conservative "5-15K" (25-75 days). MIGRATION #98: probability 55-65%. **No measured M4 Pro throughput.** | Operator plans against 2-4 days; actual likely 7-20+. |
| P-04 | **`BRUTEX_SAFETY_FACTOR=0.6` configurable** | 🟠 | CI env-var set; **zero code reads it**. `DEFAULT_INITIAL_RPS = 3.0` hardcoded in `rate_limiter.py:88`. "5 × 0.6" math is comment-only. | Misleading configurability. |
| P-05 | **GrowwToken redaction** | 🟠 | `__repr__`/`__str__` redact. NO `__reduce__`/`__getstate__` override → `pickle.dumps(token)` serializes plain. NO deepcopy guard. No adversarial tests. `_value` is public-ish. | structlog event or pickle of any state holding the token leaks silently. |
| P-06 | **DSR per (instrument, TF) N ≤ 10⁷** | 🟠 (math) | Spec line 344-345. Per-leaderboard math: 2.9M masks × 2 dirs × 3 modes × 2K grid × 1 inst × 1 TF ≈ **3.5 × 10¹⁰** (3 orders of magnitude higher than claimed). Threshold rises to ~6.4σ → Sharpe ~5+. | Project rejects everything OR keeps survivorship-bias garbage. |
| P-07 | **Audit JSONL on every material action** | 🟠 | Hash chain implementation + property tests are solid. **No Groww-adapter caller appends.** "Every material action" claim unverified end-to-end. | Deferred to DEC-P0-039 (GrowwWorker), which doesn't exist. |
| P-08 | **`record_rate_limit_hit()` / `record_success()` adaptive feedback** | 🟠 | Bucket has methods; **zero production callers**. Adaptive downshift untested end-to-end. | "Adaptive rate limiter" is a dataclass with hooks. |

---

## §3. GAPS (capability required, no code — 15 items)

| # | Capability | Verdict | Where it should live | Blocks |
|---|---|---|---|---|
| G-01 | **Rust hot kernel** | 🔴 | `rust/fno-math/src/` — empty stub, no pyo3/metal/fastbloom deps | Stage 1B/2 kernel; 80-150K trials/sec SLA |
| G-02 | **Bloom filter** | 🔴 | `brutex/orchestrator/` — `grep -r Bloom brutex/` returns 0 hits | 4-tier dedup (CLAUDE.md §2 rule 11) |
| G-03 | **Stage 1A/1B/2/3/4 funnel** | 🔴 | `brutex/engine/` — only `combo_mask.py` exists. `brutex/ranking/` empty | Entire backtest engine |
| G-04 | **Stage 2.5 Bayesian early-stop** | 🔴 | `rust/fno-math/src/stage2_5/` — doesn't exist | DEC-OPT-001 "61% trial savings"; overnight sweep claim |
| G-05 | **Phase 3.5 Metal GPU kernel** | 🔴 | `rust/fno-math/src/gpu/` — doesn't exist | DEC-OPT-002 "2× blended speedup" |
| G-06 | **GrowwWorker (first production caller)** | 🔴 | `brutex/orchestrator/groww_worker.py` — drafted locally (in stash), never landed | 6 other decisions defer to it |
| G-07 | **`brutex doctor` CLI** | 🔴 | `brutex/cli.py` subcommand — doesn't exist | C1-C10 invariants; operator workday onboarding |
| G-08 | **`tests/bench/test_full_sweep_extrapolation.py`** | 🔴 | SOLE SLA validator per PERFORMANCE_BUDGET §8 — doesn't exist | The 2-4d SLA promise has no enforcement |
| G-09 | **TLS certificate pinning** | 🔴 | `brutex/adapters/groww/client.py` — `verify=True` only, no SPKI fingerprint pinning | Compromised CA bundle = silent MITM |
| G-10 | **DH-907 watermark advance** | 🔴 | `brutex/data/watermark.py` — doesn't exist | NO_DATA chunks loop forever → quota burn |
| G-11 | **Stress / 24h soak test directory** | 🔴 | `tests/stress/` — doesn't exist | Phase 6 calibration canary |
| G-12 | **NIFTY 50 rebalance handling** | 🔴 | `brutex/calendars/nse.py` has no rebalance dates | Phase 1 spec deliverable |
| G-13 | **Half-day session list** | 🔴 | `SessionType.HALF_DAY` enum exists, no date list | P-22 holiday-bar exclusion incomplete |
| G-14 | **`indicator_version` threading** | 🔴 | `grep -rn indicator_version brutex/` returns zero | Plugin add/remove policy unimplementable |
| G-15 | **P-25, P-26 property tests** | 🔴 | Referenced in vocabulary + spec, never defined in verification pyramid | VIX leak guard + audit-replay oracle |

---

## §4. SCOPE-CREEP residuals (4 items — clean up)

| # | Item | Verdict | File:Line | Fix |
|---|---|---|---|---|
| S-01 | `brutex/adapters/protocol.py:93` docstring says "Indian F&O (second source)" | ⚠️ | `brutex/adapters/protocol.py:91-94` | Replace with "Track 1 spot indexes via Groww v2 historical-data" |
| S-02 | `brutex/options/` directory inside Track 1 package | ⚠️ | `brutex/options/__init__.py:1` | Move to `track2/` or document as Track 2 reference (cost calculator imports it) |
| S-03 | `models.py` declares `FUT`/`CE`/`PE` + `is_derivative` properties | ⚠️ | `brutex/models.py:84-86,163` | Track 2 reference; should not be in Track 1 broker-neutral data model |
| S-04 | `Segment.FNO` enum present | ⚠️ | `brutex/models.py:58` | Same logic that retired commodity — should be removed for Track 1 |

---

## §5. LOCKED (✅ — verified, safe to depend on — 17 items)

| # | Requirement | Evidence |
|---|---|---|
| L-01 | Groww rate limit 5 r/s + 100K/day | CLAUDE.md §6 cites groww.in/trade-api/docs/docs/v2 |
| L-02 | Native intraday TFs (1/5/15/25/60 only) | CLAUDE.md §6 + MIGRATION #54 (self-corrected) |
| L-03 | DEC-PROTOCOL-001 retired | CLAUDE.md header + MIGRATION #164 |
| L-04 | Spot indices not tradable → no slippage on spot | TRACK1_BACKTEST_SPEC.md:518-531 |
| L-05 | IST midnight quota reset | `brutex/orchestrator/quota_tracker.py:103-110` uses `ZoneInfo("Asia/Kolkata")` |
| L-06 | 21 timeframes enforced | `brutex/data/timeframes.py:28-69` + compile-time assertion |
| L-07 | 52 atomic conditions enforced | `brutex/vocabulary/bits.py:23-91` + assertion |
| L-08 | Muhurat session handled | `brutex/calendars/nse.py:182-188, 210-212` |
| L-09 | DH-908 length-mismatch synthesis | `brutex/adapters/groww/parser.py:122-127` |
| L-10 | Pre-open auction filter | `brutex/calendars/nse.py:276-278` |
| L-11 | 15:20 IST forced exit | `brutex/calendars/nse.py:25, 266-273` |
| L-12 | SSRF guard | `brutex/adapters/groww/instrument_loader.py:44-48` host allowlist (`GROWW_CSV_ALLOWED_HOST`; the old `ssrf_guard.py` module was removed with the prior vendor) |
| L-13 | Audit JSONL hash chain integrity | `brutex/orchestrator/audit.py:470-527` + property tests |
| L-14 | `safe_message` discipline (Groww/quota errors) | `auth.py`, `quota_tracker.py` use `safe_message=` (the old `ssrf_guard.py` was removed with the prior vendor — see L-12) |
| L-15 | DEC-P0-033 through DEC-P0-038 merged | git log: `1e2f3a0`, `20d5ccc`, `ec3cc09`, `a31d623`, `1470f9d`, `0f824d7` |
| L-16 | Pyright strict project-wide | `pyproject.toml:351` `typeCheckingMode = "strict"`; CI runs `pyright brutex tests` |
| L-17 | 92-chunk first-run math (CLAUDE.md form) | TRACK1_DATA_INGEST_DESIGN.md:458 derivation correct |

---

## §6. OPEN QUESTIONS for operator (12 items — operator must decide)

| # | Question | Why it matters | Recommendation |
|---|---|---|---|
| Q-01 | Lock canonical trial count at **730B** or **33B** or **1.095T**? | Drives every wall-time / storage / Bloom / SLA number | ✅ **ANSWERED: 730 B** (DEC-MODE-002, 2026-05-23 — Mode C removed; was 1.095T pre-removal). 33B is the Stage 2 hot-path subset, not the total. |
| Q-02 | Hash function: **sha256** (current code) or **blake3** (current spec)? | Cross-component byte-equality replay | blake3 (faster, spec-aligned), migrate code |
| Q-03 | Stage 1B coarse_score: keep next-bar-sign correlation (in-sample peeking) OR rewrite to purged-CV? | DSR contamination risk | Rewrite to purged-CV |
| Q-04 | DSR effective-N: keep magic `10⁷` or derive from cluster size? | Project rejects everything vs accepts noise | Derive cluster-effective-N (Bailey-Lopez de Prado appendix) |
| Q-05 | Throughput canary 50 M/s — keep gate or downgrade? | Gate is currently theatre | Downgrade to 50 K/s OR drop the gate; replace with `test_full_sweep_extrapolation.py` |
| Q-06 | Dashboard default bind: 127.0.0.1 or 0.0.0.0? | Security exposure | 127.0.0.1 (match docs); 0.0.0.0 only with auth-required flag |
| Q-07 | GrowwWorker scope: ship-now (drafted in stash) OR continue requirements review? | 6 deferred decisions depend on it | Ship-now after this review; resume PR work |
| Q-08 | Scope-creep cleanup: delete `brutex/options/` + `FUT/CE/PE/FNO` OR keep as Track 2 reference? | Golden Rule #3 scope-lock | Move to `track2/`; cost calculator imports updated |
| Q-09 | NIFTY 50 rebalance handling: hardcode dates or fetch runtime? | Phase 1 deliverable; audit completeness | Hardcode (~10 events 2021-2026); flag in audit |
| Q-10 | TLS pinning: SPKI fingerprint pinning or accept current `verify=True` + `trust_env=False`? | MITM defence | Add SPKI pinning (modest operational cost) |
| Q-11 | Mutation gate: keep 85% on `state.py` only, OR extend to entire `brutex/`? | "100% standards" claim | Extend to all DEC-P0-modified modules; ratchet operator's 95% wish |
| Q-12 | Phase plan rebaselining: re-publish honest "Phase 1 scaffolding complete; Phase 3 engine pending" OR keep aspirational? | Operator-vs-reality drift | Rebaselline; honest envelope per Golden Rule #10 |

---

## §7. Renaissance CFO summary (one paragraph)

BruteX today is a **well-tested ingest + orchestrator-scaffolding project** with disciplined audit/quota/rate-limiter/SSRF/parser primitives and excellent CLAUDE.md governance. It is **NOT yet a backtest engine**. The spec promises a 5-stage funnel + Bloom dedup + Rust kernel + Metal GPU + DSR cascade + lockbox; the live branch has Phase 1 ingest plus the 52-bit vocabulary atoms. The headline "2-4 days for 33B trials" SLA is unmeasured AND the trial count itself is internally inconsistent across docs (33B / 730B / 1.095T). The reproducibility hash, the backbone of bit-exact reruns, hashes 4 inputs where the spec mandates 9. **No Renaissance researcher would deploy this in its current state — but the foundation is honest, the audit chain is solid, and the scope-lock is mostly intact.** The path forward is to (a) reconcile the 12 conflicts in §1, (b) close the 15 gaps in §3 starting with GrowwWorker + the engine modules, (c) downgrade the 8 PARTIAL-CLAIMED-AS-REAL items in §2 to honest 🟡 status in their source docs.

## §8. Suggested fix order (operator-prioritisable)

1. **§4 SCOPE-CREEP fixes** (1 PR, 4 small file edits) — fast win
2. **§1 C-11 (stale 92→60 chunk fix)** + **C-12 (Ram Navami date)** — fast doc fixes
3. **§1 C-04 + C-05 (hash composition + sha256→blake3 unification)** — foundational; everything downstream depends on it
4. **§3 G-06 (GrowwWorker)** — closes 6 deferred decisions
5. **§1 C-01 reconcile trial count** + **§1 C-02 storage** + **§1 C-07 Bloom sizing** — single coordinated update across spec + perf-budget + verification-pyramid
6. **§3 G-12 + G-13 (NIFTY rebalance + half-day list)** — Phase 1 deliverables
7. **§1 C-06 (Stage 1B contamination)** — needs §6 Q-03 decision
8. **§3 G-02 (Bloom) + G-01 (Rust kernel) + G-03 (engine stages)** — biggest engineering bloc; Phase 3 proper

## §9. Per-agent raw reports

Each agent's full output is preserved in:
- `/tmp/.../tasks/aa696c0d7b69a3911.output` — Agent 1 (Math/Hot-path/Fact-checker, R-001 through R-020)
- `/tmp/.../tasks/acfeda32f4d1fbc1b.output` — Agent 2 (Eng/Sec/Skills, R-100 through R-120)
- `/tmp/.../tasks/a02fd43dd34eda789.output` — Agent 3 (Data/Correct/Scope, R-200 through R-224)
- `/tmp/.../tasks/af039581e7ec42e90.output` — Agent 4 (Ops/Cite-path/Sanity, R-300 through R-319)

Synthesised by Claude (Opus 4.7) on 2026-05-18.

---

## §10. Post-2026-05-18 resolution status (as of 2026-05-27)

**Purpose:** The §1–§4 tables above are a static snapshot from 2026-05-18. Several items have been closed by subsequent decisions, and their evidence-citations point to text that no longer exists. This section maps each resolved item to its closing decision so operators reading §1–§4 don't chase stale citations.

**Method:** This is purely additive. The original §1–§4 tables are preserved unchanged to maintain the audit trail of what the 4-agent hostile review found at the 2026-05-18 baseline.

### §10.1 Conflicts RESOLVED since 2026-05-18

| Original | Status | Closing decision | Notes |
|---|---|---|---|
| **C-01** Total sweep trial count | ✅ RESOLVED | DEC-MODE-002 (2026-05-23) | Mode C removed → 730B (total) / 33B (Stage 2 hot-path); already annotated inline in §1 row 26 |
| **C-04** `combo_hash` 9 inputs | ✅ RESOLVED | DEC-P0-060 (MIGRATION entry #198) + DEC-COMBO-HASH-UNIQUENESS-001 | `brutex/strategies/combo.py` now hashes the 9-field BLAKE3 composition; dedup property test added |
| **C-05** sha256 vs blake3 | ✅ RESOLVED | DEC-CALIBRATE-001 (CLAUDE.md Golden Rule #4 corrected note, 2026-05-24) | Spec + code both on `blake3` |
| **C-09** Coverage gate "100% line + branch" | 🟡 PARTIALLY RESOLVED | DEC-COV-001 (entries #210-#232) + DEC-COV-RATCHET-001 (#256) + DEC-COV-BACKFILL-001/002/003/004 (#257-#260) + DEC-COV-BACKFILL-LIMIT-001 (#261, Option B operator-accepted limit 2026-05-27) | Cited evidence "STANDARDS row 1 says '72.32% actual'" is **stale**. Current STANDARDS row 1: "strict per-module 100% gate at 51 modules" + "🟡 (51/~71 strict-gated; 100% via global cov)". Residual gap: 8 orchestrator modules blocked by `fastbloom_rs` PyO3 ABI3 single-init; covered at 100% via global cov, blocked from standalone gating. 3 ROADMAP closure paths in entry #261 |
| **C-11** 92-chunk first-run math | ✅ RESOLVED | (Inline in `TRACK1_BACKTEST_SPEC.md:217`) | Cited evidence "spec line 195 says '3 instruments + 60 calls'" is **stale**. Current spec line 217: "5 years × **4 instruments** (NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX...) × 90-day chunks ≈ **92 API calls + 4 daily calls**" |
| **C-12** Golden fixture mislabel ("2024-04-12 Ram Navami half-session") | ✅ RESOLVED | (Inline in `TRACK1_VERIFICATION_PYRAMID.md:106-108` per DEC-P0-042) | Cited evidence is **stale**. Current line 107 references `groww_intraday_2024-02-01_NIFTY.json` (NSE Budget Day half-session) with explicit fix-note: "Ram Navami 2024 was 2024-04-17 *full* holiday, and 2024-04-12 was a regular Friday — see `brutex/calendars/nse.py` HOLIDAYS_2024." |
| **C-08** Dashboard default bind | ✅ RESOLVED | (Inline in `brutex/config/ports.py:55-62` per DEC-P0-070 + Q-06 of the 2026-05-18 12-decision lock) | Cited evidence "`DEFAULT_HOST_BIND = '0.0.0.0'` with `# nosec — intentional LAN bind`" is **stale**. Current code: `DEFAULT_HOST_BIND: Final[str] = "127.0.0.1"` with comment block citing "DEC-P0-070 (2026-05-23): default locked to ``127.0.0.1`` per [Q-06 of operator's 12-decision lock]". The 0.0.0.0 LAN-exposure path is gated by explicit `BRUTEX_HOST_BIND=0.0.0.0` env-var opt-in only. |
| **(spec consistency)** 3-instrument stale text in PHASE_PLAN §Week 7 + SPEC §"Day 1 hour-by-hour" | ✅ RESOLVED (this PR) | Inline edits | `TRACK1_PHASE_PLAN.md:162` updated from "3 instruments × 15 TFs" → "4 instruments (NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX) × 21 TFs" (matches CLAUDE.md §3 + spec line 217). `TRACK1_BACKTEST_SPEC.md:718` updated from "for all 3 instruments" → "for all 4 instruments (NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX)" (same alignment). Brute-force trial-multiplication lines (152, 314, 688, 881) intentionally LEFT at "3 instruments" — those describe the brute-force ENGINE which excludes REFERENCE_ONLY VIX per CLAUDE.md §3 + DEC-P0-029. |

### §10.2 PARTIAL items moved since 2026-05-18

| Original | Status | Closing decision | Notes |
|---|---|---|---|
| **P-01** "100% standards / 18 requirements" → "Real coverage: ~22%" | 🟡 SIGNIFICANTLY IMPROVED | DEC-STANDARDS-AUDIT-001 + DEC-CROSSWALK-001 + DEC-PYLINT-DROP-001 + DEC-STRESS-001 + DEC-DOCTOR-001 (entries #205-#209) + DEC-COV-001 sequence | Cited "9 of 21 sub-rows ✅ ENFORCED" is **stale**. Current STANDARDS_100PCT.md §1 summary: **15 of 21 sub-rows 🟢 (71%)** + 2 🟡 + 1 📋 + 3 👁️. Decree-vs-reality gap closed from ~78% → ~29%. Residual 2 🟡 (rows 1+2, coverage per-module) explicitly tracked under DEC-COV-BACKFILL-LIMIT-001 ratchet |

### §10.3 GAPS closed since 2026-05-18

| Original | Status | Closing decision |
|---|---|---|
| **G-07** `brutex doctor` CLI | ✅ CLOSED | DEC-DOCTOR-001 (MIGRATION entry #209, PR #100) — `brutex/doctor.py` + `brutex doctor` subcommand + 18 unit tests at 100% line+branch |
| **G-11** Stress / 24h soak test directory | ✅ CLOSED | DEC-STRESS-001 (MIGRATION entry #208, PR #99) — `tests/stress/test_audit_chain_sustained.py` + nightly workflow `.github/workflows/stress-nightly.yml`. ~~+ 24h soak via `workflow_dispatch`~~ **CORRECTED (DEC-SOAK-REAL-TIER-001, MIGRATION entry #1580 — Golden Rule #10):** the ubuntu nightly never was and is not a soak — it is a **~5-min regression gate** whose guard step REFUSES `STRESS_EVENTS > 200000` (the retracted "24h soak" dispatch is rejected, not run). The real multi-hour soak runs on `.github/workflows/soak-selfhosted.yml` (self-hosted M4-Pro, duration-derived timeout, weekly cron + `workflow_dispatch(soak_minutes)`) |
| **G-13** Half-day session list | ✅ RESOLVED with honest envelope | Inline in `brutex/calendars/nse.py:1275-1289` (market-microstructure facts per `CLAUDE.md §7` + `TRACK1_MICROSTRUCTURE.md`) — cited "gap" is **stale**. Code explicitly documents: "NSE does NOT run pre-festival 'half-day' sessions in modern era (post-2010 trading-hours unification). Muhurat (Diwali ~1 hr evening, since 2025 ~1 hr afternoon) is the only short session and is handled by `MUHURAT_SESSIONS` above. The `SessionType.HALF_DAY` enum is kept for forward-compat." Special weekend Budget-Day sessions (e.g., 2025-02-01) are flagged as **NOT half-day** — regular 09:15-15:30 hours apply. Per Golden Rule #10 honest envelope, this is correctly documented as a zero-qualifying-event window rather than a missing list. |
| **G-12** NIFTY 50 rebalance handling | ✅ RESOLVED | Inline in `brutex/calendars/nse.py:1069-1187` per DEC-P0-046 + Q-09 of the 2026-05-18 12-decision lock — cited "gap" ("`brutex/calendars/nse.py` has no rebalance dates") is **stale**. Code now has `NIFTY50_REBALANCE_EVENTS` (semi-annual events 2021→2025 with per-event `added`/`removed`/`verified` flags) + `is_nifty_rebalance_day(d)` accessor. Honest envelope per Golden Rule #10: each event carries `verified=True` (independent financial-news source quoting the NSE Indices circular) or `verified=False` (pattern-derived from semi-annual cadence; cross-check deferred to Phase 1 Validation Gate 1). |
| **G-08** `tests/bench/test_full_sweep_extrapolation.py` (SLA validator) | ✅ RESOLVED (v1 scaffold) | File exists per DEC-P0-062 (MIGRATION entry #199) — cited "gap" ("doesn't exist") is **stale**. 4 test functions present: `test_projected_33b_within_sla` (hard SLA gate, microbench proxy → 33B projection → ≤ hardware-SLA assertion), `test_per_trial_kernel_regression_floor`, `test_per_trial_proxy_stable_across_runs`. Honest envelope per Golden Rule #10: the hard SLA gate is `xfail(strict=True)` for v1 (flips to PASS the day the real `count_supermasks_u64s` kernel + engine ship); the scaffold structure (proxy + projection + SLA comparison) runs in CI today. |
| **G-14** `indicator_version` threading | ✅ RESOLVED | Threaded into `brutex/strategies/combo.py:100` per DEC-P0-060 (combo_hash 9-field BLAKE3 migration, MIGRATION entry #197) — cited "gap" ("`grep -rn indicator_version brutex/` returns zero") is **stale**. `combo_hash(...)` now takes `indicator_version: str` as 1 of the 9 identity fields (`strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_sha`); `grep -rnE "indicator_version" brutex/` now returns 5 hits across `combo.py` + `strategies/__init__.py`. Honest envelope per Golden Rule #10: the **reproducibility-hash threading** (the cited gap) is RESOLVED; the broader plugin add/remove *policy* (hot-reload semantics) remains Phase 2/3 engine work tracked separately. |

### §10.4 SCOPE-CREEP residuals — status note

The 4 S-01..S-04 items were materially rewritten by **DEC-VENDOR-PIVOT-001** (2026-05-26, MIGRATION entry #246) which wiped the Groww adapter and reset the vendor surface. Track-2 references in `brutex/options/`, `models.py` (`FUT`/`CE`/`PE` / `Segment.FNO`) now serve the formally-authorized DEC-P0-063 expansion (200 NSE F&O stocks + spot + options across all 4 expiryCodes in the **LAKE**; brute-force engine unchanged). Per CLAUDE.md §3 (DEC-P0-063 entry), these items are no longer scope-creep — they are authorized lake-layer scope. Cross-spec reconciliation of the remaining ~15 stale-text locations is a tracked follow-up PR per CLAUDE.md §3.

### §10.5 Open items (not addressed in this addendum)

C-02 · C-03 · C-06 · C-07 · C-10 · P-02 · P-03 · P-04 · P-05 · P-06 · P-07 · P-08 · G-01 · G-02 · G-03 · G-04 · G-05 · G-06 · G-09 · G-10 · G-15 — these remain as captured in §1–§3 (no closure decision recorded since 2026-05-18 review). Many are Phase 2+ engine-build work or operator-decision items in §6. (**C-12 + C-08 + G-13 + G-12 + G-08 + G-14 removed from this list 2026-05-27/28** — all RESOLVED per §10.1 + §10.3 rows added in DEC-SPEC-CONSISTENCY-001 entry #263 + DEC-C08-RESOLUTION-001 entry #271 + DEC-G13-RESOLUTION-001 entry #273 + DEC-G12-RESOLUTION-001 entry #274 + DEC-G08-RESOLUTION-001 entry #275 + DEC-G14-RESOLUTION-001 entry #276 respectively; bookkeeping.)

### §10.6 Track 1 spec-consistency follow-up wave (2026-05-27)

Per CLAUDE.md §3's tracked follow-up note ("Full §3 / cross-spec reconciliation across the ~15 remaining stale-text locations is a tracked follow-up PR"), a single-session ratchet wave landed 7 surgical amendments bringing locked Track 1 specs into alignment with DEC-P0-063 (entry #200) engine-vs-lake distinction + Option B coverage closure. **8 of CLAUDE.md §3's ~15 stale-text locations now reconciled.** Pattern: each PR amends a single spec section with a 1-paragraph "AMENDED 2026-05-22 per DEC-P0-063" note that mirrors locked text in CLAUDE.md §3 + MIGRATION entry #200 — no new claims; engine-vs-lake distinction made explicit; "what NOT to touch" inventory protects the engine description.

| # | DEC-ID | MIGRATION entry | File:Section | Closes |
|---|---|---|---|---|
| 1 | DEC-SPEC-CONSISTENCY-001 | #263 | TRACK1_PHASE_PLAN.md:162 + TRACK1_BACKTEST_SPEC.md:718 + §10.1 C-12 row | 2 inline ingest-scope lines + C-12 citation gap |
| 2 | DEC-SPEC-CONSISTENCY-002 | #264 | TRACK1_BACKTEST_SPEC.md §"Scope Lock" | Most prominent scope-statement in the spec |
| 3 | DEC-SPEC-CONSISTENCY-003 | #265 | TRACK1_DATA_INGEST_DESIGN.md §1 "Scope" | Most prominent scope-statement in the ingest-design spec |
| 4 | DEC-SPEC-CONSISTENCY-004 | #266 | TRACK1_RUNTIME_ARCHITECTURE.md §2 | Runtime-architecture pipeline section |
| 5 | DEC-SPEC-CONSISTENCY-005 | #267 | TRACK1_PHASE_PLAN.md Phase 2 header | Phase-plan deliverable section |
| 6 | DEC-SPEC-CONSISTENCY-006 | #268 | TRACK1_BACKTEST_SPEC.md §"Decision Ledger Linkage" #301 | Decision-ledger historical entry (additive, preserves audit trail) |
| 7 | DEC-SPEC-CONSISTENCY-007 | #269 | TRACK1_PHASE_PLAN.md Week 2 coverage-gate row | Stale module count (33 → 51) + DEC-ID list update |

**Closure pattern: 1-paragraph amendment + 1 MIGRATION entry + 8-decision builds-on chain per PR + 1-agent hostile self-review per runbook trigger matrix (`docs/runbooks/4-agent-review.md §3`).**

**~7 stale-text locations still remain** per CLAUDE.md §3's "~15" estimate. Future PRs can address them with the same pattern, OR the operator can declare the wave's residual gap acceptable and move on to Option D / Phase 2 work.

**Decision ID:** DEC-REQS-FINAL-RECONCILE-001 (2026-05-27, **extended 2026-05-27 with §10.6 wave summary + §10.5 C-12 bookkeeping fix**). **Builds on:** DEC-STANDARDS-AUDIT-001 (entry #205) · DEC-CROSSWALK-001 (#206) · DEC-PYLINT-DROP-001 (#207) · DEC-STRESS-001 (#208) · DEC-DOCTOR-001 (#209) · DEC-COV-001 (#210-#232) · DEC-COV-RATCHET-001 (#256) · DEC-COV-BACKFILL-001/002/003/004 (#257-#260) · DEC-COV-BACKFILL-LIMIT-001 (#261) · DEC-MODE-002 (entry #196) · DEC-P0-060 (#198) · DEC-COMBO-HASH-UNIQUENESS-001 · DEC-CALIBRATE-001 · DEC-VENDOR-PIVOT-001 (#246) · DEC-P0-063 (#200). **Closes:** the citation-staleness gap between TRACK1_REQUIREMENTS_FINAL.md's 2026-05-18 snapshot and the current state of the repo on 2026-05-27. **Does NOT close:** the open items listed in §10.5 — those remain genuine open work.

### §10.7 Requirements-review verifiable-closure sub-wave (2026-05-27/28)

Parallel to the §10.6 spec-consistency wave, a second sub-wave verified that **5 of the 2026-05-18 review's flagged "gaps/conflicts" had already been closed by code that shipped between the review date and 2026-05-28** — their cited evidence pointed at code states that no longer exist. Each closure was verified by `grep` against the live code, NOT by re-implementing anything. Pattern: read the cited stale evidence → grep the current code → if the gap is closed, add a §10.1/§10.3 RESOLVED row with the verification chain + honest envelope, and remove from §10.5.

| # | DEC-ID | MIGRATION entry | Item | Verified-current code state |
|---|---|---|---|---|
| 1 | DEC-C08-RESOLUTION-001 | #271 | C-08 dashboard bind | `brutex/config/ports.py:62` → `DEFAULT_HOST_BIND = "127.0.0.1"` (per DEC-P0-070 #272 + Q-06) |
| 2 | DEC-G13-RESOLUTION-001 | #273 | G-13 half-day session list | `brutex/calendars/nse.py:1214-1226` honest envelope (zero qualifying events in window) |
| 3 | DEC-G12-RESOLUTION-001 | #274 | G-12 NIFTY rebalance | `brutex/calendars/nse.py:1069-1187` `NIFTY50_REBALANCE_EVENTS` + `is_nifty_rebalance_day` (per DEC-P0-046) |
| 4 | DEC-G08-RESOLUTION-001 | #275 | G-08 SLA validator | `tests/bench/test_full_sweep_extrapolation.py` exists, v1 scaffold (per DEC-P0-062) |
| 5 | DEC-G14-RESOLUTION-001 | #276 | G-14 indicator_version threading | `brutex/strategies/combo.py:100` 9-field combo_hash (per DEC-P0-060) |

Plus the retroactive **DEC-P0-070 (#272)** ledger entry that paper-trailed a 2026-05-23 code-shipped decision the validator flagged as un-ledgered.

### §10.8 Residual status + honest stopping point (2026-05-28)

After both sub-waves, the §10.5 open list is down to: **C-02 · C-03 · C-06 · C-07 · C-10 · P-02..P-08 · G-01 · G-02 · G-03 · G-04 · G-05 · G-06 · G-09 · G-10 · G-15**. Per Golden Rule #10 (honest limits), these are **NOT doc-closeable by verification** — they fall into 3 buckets:

1. **Phase 2/3 engine-build work** (G-01 Rust kernel · G-02 Bloom · G-03 Stage 1A/1B/2/3/4 funnel · G-04 Stage 2.5 · G-05 Metal GPU · G-06 GrowwWorker · C-03 throughput canary · C-06 Stage 1B contamination · C-07 Bloom sizing · P-02/P-03/P-06/P-07/P-08) — blocked on Phase 0b empirical probe + operator authorization to start Phase 2 (see `TRACK1_PHASE_PLAN.md` Phase 0b HARD-GATE).
2. **Operator-decision / analysis items** (C-02 storage budget reconciliation · C-10 phase-plan honest rebaseline [Q-12 authorized but needs a current-state audit] · P-04 safety-factor wiring · P-05 token redaction hardening) — tractable but each needs a focused decision, not a verification pass.
3. **Security / infra hardening** (G-09 TLS SPKI pinning · G-10 DH-907 watermark · G-15 P-25/P-26 property tests) — real code work, Phase 2-adjacent.

**The verifiable-resolution vein is exhausted.** Continuing the ratchet would mean either (a) starting Phase 2 engine work (operator-blocked), (b) a C-10 phase-plan honest-rebaseline PR (doc-closeable, needs a careful current-state audit first), or (c) the operator declaring the residual acceptable and pausing. This §10.8 note is the honest punctuation mark per Golden Rule #10.

**Decision ID:** DEC-REQS-RESIDUAL-STATUS-001 (2026-05-28).
