# Track 1 — Requirements Final Review

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
| C-01 | **Total sweep trial count** | ❌ CONFLICT | TRACK1_BACKTEST_SPEC.md says **730B** (line 138). PERFORMANCE_BUDGET.md + VERIFICATION_PYRAMID.md say **33B**. MIGRATION DEC-MODE-001 adds Mode C → **1.095T**. 22×-33× discrepancy. | Every wall-time / storage / Bloom / SLA number computed against wrong denominator. |
| C-02 | **Storage after Zstd-9 compression** | ❌ CONFLICT | Spec: "~50-100 GB"; MIGRATION #203: "2.5 TB compressed for 33B"; derived: 730B × 200B × 5× = 29 TB. Three numbers; only one can be true. | 8 TB SSD blown by 7× → sweep aborts. |
| C-03 | **Throughput canary 50 M/s gate** | ❌ CONFLICT | CLAUDE.md §2 rule 12: "throughput canary ≥ 50 M/s". Actual SLA: 80-150K trials/sec (3 orders of magnitude lower). CI: ratio-only ≤ 8.0 (absolute never asserted). | Gate is permanently red OR permanently green (no one reads); headline metric unfalsifiable. |
| C-04 | **`combo_hash` reproducibility** | ❌ CONFLICT | Spec § Identity mandates 9 inputs: `strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash`. Code at `brutex/strategies/combo.py:60-106` hashes only 4: `strategy_id ‖ params ‖ data_snapshot_hash ‖ code_commit_sha`. **Missing 5 of 9.** | Two combos differing only in direction/instrument/tf/mode collide → reproducibility broken on the first cross-axis sweep. |
| C-05 | **sha256 vs blake3** | ❌ CONFLICT | Spec consistently specifies blake3 (lines 401, 537, 542, 887). Code uses `hashlib.sha256` everywhere. | Cross-component byte-equality replay fails; P-19/P-20 fail. |
| C-06 | **Stage 1B in-sample contamination** | ❌ CONFLICT (math) | `TRACK1_BACKTEST_SPEC.md:296-306` defines coarse_score as `freq × sign-correlation × coverage-penalty` where sign-correlation peeks at next-bar-sign. By construction, masks correlated with future direction are kept. | DSR at Stage 4 cannot undo Stage 1B contamination — half the funnel pruning is mathematically a snooping pre-screen. |
| C-07 | **Bloom filter sizing** | ❌ CONFLICT | MIGRATION #121: "150M-1B survivors, FPR ~0". MIGRATION #255: "may need Cuckoo at 146B survivors". MIGRATION #203: "40 GB borderline 48 GB RAM". Three sizings. | Wrong → OOM mid-sweep OR 56% FPR → silent dedup collisions → audit chain corruption. |
| C-08 | **Dashboard default bind** | ❌ CONFLICT | TRACK1_RUNTIME_ARCHITECTURE.md §6.3: default `127.0.0.1`. Code: `brutex/config/ports.py:59` `DEFAULT_HOST_BIND = "0.0.0.0"` with `# nosec — intentional LAN bind`. Zero authentication. | Audit JSONL + Parquet stats + recent events exposed to anyone on LAN/VPN. |
| C-09 | **Coverage gate "100% line + branch"** | ❌ CONFLICT (within same doc) | STANDARDS_100PCT.md row 1 says "**72.32% actual**" AND §10 line 241 says "**96.03% currently**". pyproject.toml: `fail_under = 90`. Omit list excludes 9 modules. | Headline "100%" is contradicted in the same doc; true coverage measurable only against pure non-omitted core. |
| C-10 | **Phase plan vs branch state** | ❌ CONFLICT | Spec presents 16-week locked plan with engine + GPU + DSR cascade. Live branch: Phase 1 ingest + Phase 2 atoms only. Missing: Stage 1A/1B/2/2.5/3/4 engine, BIPOP-CMA-ES, DSR, CPCV, PBO, Romano-Wolf, walk-forward, lockbox, Rust kernel, Metal GPU, Bloom. | Spec is fiction downstream of "5-stage funnel". |
| C-11 | **92-chunk first-run math** | ❌ CONFLICT (stale spec) | CLAUDE.md §2: "92-chunk first-run + 4-chunk daily" (4 instruments × 22 × 90-day + 4 daily). TRACK1_BACKTEST_SPEC.md:195 still says "3 instruments + 60 calls". CLAUDE.md correct; spec stale. | Easy fix (1 line). Worth resolving. |
| C-12 | **Golden fixture mislabel** | ❌ CONFLICT | TRACK1_VERIFICATION_PYRAMID.md:86 lists `dhan_intraday_2024-04-12_NIFTY.json` as "Ram Navami half-session". Real Ram Navami 2024 was 2024-04-17. 2024-04-12 was a regular Friday. | Half-day handling test exercises wrong date. |

---

## §2. PARTIAL-CLAIMED-AS-REAL (honest-envelope violations — 8 items)

| # | Requirement | Verdict | Evidence | Risk |
|---|---|---|---|---|
| P-01 | **"100% standards / 18 requirements"** | 🟠 | Only **9 of 21 sub-rows ✅ ENFORCED**; 4 🟡 PARTIAL; 5 📋 ROADMAP; 3 👁️ MANUAL per STANDARDS_100PCT.md's own self-audit. CLAUDE.md §2 rule 12 presents all 18 as binding. **Real coverage: ~22%.** | Decree-vs-reality gap of ~60%. |
| P-02 | **"Mutation score ≥ 85%"** | 🟠 | Covers ONE module (`brutex/orchestrator/state.py` at 98%). 4 prior PRs used `OPERATOR_OVERRIDE` to bypass entirely (DEC-P0-005). | Headline gate covers ~1% of package. |
| P-03 | **"2-4 day SLA at 80-150K trials/sec"** | 🟠 | PERFORMANCE_BUDGET §5: "OPTIMISTIC" tier. Realistic is "20-50K" (7-20 days), conservative "5-15K" (25-75 days). MIGRATION #98: probability 55-65%. **No measured M4 Pro throughput.** | Operator plans against 2-4 days; actual likely 7-20+. |
| P-04 | **`BRUTEX_SAFETY_FACTOR=0.6` configurable** | 🟠 | CI env-var set; **zero code reads it**. `DEFAULT_INITIAL_RPS = 3.0` hardcoded in `rate_limiter.py:72`. "5 × 0.6" math is comment-only. | Misleading configurability. |
| P-05 | **DhanToken redaction** | 🟠 | `__repr__`/`__str__` redact. NO `__reduce__`/`__getstate__` override → `pickle.dumps(token)` serializes plain. NO deepcopy guard. No adversarial tests. `_value` is public-ish. | structlog event or pickle of any state holding the token leaks silently. |
| P-06 | **DSR per (instrument, TF) N ≤ 10⁷** | 🟠 (math) | Spec line 344-345. Per-leaderboard math: 2.9M masks × 2 dirs × 3 modes × 2K grid × 1 inst × 1 TF ≈ **3.5 × 10¹⁰** (3 orders of magnitude higher than claimed). Threshold rises to ~6.4σ → Sharpe ~5+. | Project rejects everything OR keeps survivorship-bias garbage. |
| P-07 | **Audit JSONL on every material action** | 🟠 | Hash chain implementation + property tests are solid. **No Dhan-adapter caller appends.** "Every material action" claim unverified end-to-end. | Deferred to DEC-P0-039 (DhanWorker), which doesn't exist. |
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
| G-06 | **DhanWorker (first production caller)** | 🔴 | `brutex/orchestrator/dhan_worker.py` — drafted locally (in stash), never landed | 6 other decisions defer to it |
| G-07 | **`brutex doctor` CLI** | 🔴 | `brutex/cli.py` subcommand — doesn't exist | C1-C10 invariants; operator workday onboarding |
| G-08 | **`tests/bench/test_full_sweep_extrapolation.py`** | 🔴 | SOLE SLA validator per PERFORMANCE_BUDGET §8 — doesn't exist | The 2-4d SLA promise has no enforcement |
| G-09 | **TLS certificate pinning** | 🔴 | `brutex/adapters/dhan/client.py` — `verify=True` only, no SPKI fingerprint pinning | Compromised CA bundle = silent MITM |
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
| S-01 | `brutex/adapters/protocol.py:93` docstring says "Indian F&O (second source)" | ⚠️ | `protocol.py:91-94` | Replace with "Track 1 spot indexes via Dhan v2 historical-data" |
| S-02 | `brutex/options/` directory inside Track 1 package | ⚠️ | `brutex/options/__init__.py:1` | Move to `track2/` or document as Track 2 reference (cost calculator imports it) |
| S-03 | `models.py` declares `FUT`/`CE`/`PE` + `is_derivative` properties | ⚠️ | `models.py:47-58, 116-138` | Track 2 reference; should not be in Track 1 broker-neutral data model |
| S-04 | `Segment.FNO` enum present | ⚠️ | `models.py:37` | Same logic that retired commodity — should be removed for Track 1 |

---

## §5. LOCKED (✅ — verified, safe to depend on — 17 items)

| # | Requirement | Evidence |
|---|---|---|
| L-01 | Dhan rate limit 5 r/s + 100K/day | CLAUDE.md §6 cites dhanhq.co/docs/v2 |
| L-02 | Native intraday TFs (1/5/15/25/60 only) | CLAUDE.md §6 + MIGRATION #54 (self-corrected) |
| L-03 | DEC-PROTOCOL-001 retired | CLAUDE.md header + MIGRATION #164 |
| L-04 | Spot indices not tradable → no slippage on spot | TRACK1_BACKTEST_SPEC.md:518-531 |
| L-05 | IST midnight quota reset | `brutex/orchestrator/quota_tracker.py:103-110` uses `ZoneInfo("Asia/Kolkata")` |
| L-06 | 21 timeframes enforced | `brutex/data/timeframes.py:28-69` + compile-time assertion |
| L-07 | 52 atomic conditions enforced | `brutex/vocabulary/bits.py:23-91` + assertion |
| L-08 | Muhurat session handled | `brutex/calendars/nse.py:182-188, 210-212` |
| L-09 | DH-908 length-mismatch synthesis | `brutex/adapters/dhan/parser.py:122-127` |
| L-10 | Pre-open auction filter | `brutex/calendars/nse.py:276-278` |
| L-11 | 15:20 IST forced exit | `brutex/calendars/nse.py:25, 266-273` |
| L-12 | SSRF guard | `brutex/adapters/dhan/ssrf_guard.py:59-127` (DNS-rebind gap documented) |
| L-13 | Audit JSONL hash chain integrity | `brutex/orchestrator/audit.py:470-527` + property tests |
| L-14 | `safe_message` discipline (Dhan/quota errors) | `auth.py`, `quota_tracker.py`, `ssrf_guard.py` all use `safe_message=` |
| L-15 | DEC-P0-033 through DEC-P0-038 merged | git log: `1e2f3a0`, `20d5ccc`, `ec3cc09`, `a31d623`, `1470f9d`, `0f824d7` |
| L-16 | Pyright strict project-wide | `pyproject.toml:231` `typeCheckingMode = "strict"`; CI runs `pyright brutex tests` |
| L-17 | 92-chunk first-run math (CLAUDE.md form) | TRACK1_DATA_INGEST_DESIGN.md:458 derivation correct |

---

## §6. OPEN QUESTIONS for operator (12 items — operator must decide)

| # | Question | Why it matters | Recommendation |
|---|---|---|---|
| Q-01 | Lock canonical trial count at **730B** or **33B** or **1.095T**? | Drives every wall-time / storage / Bloom / SLA number | 1.095T (most honest — includes Mode C) |
| Q-02 | Hash function: **sha256** (current code) or **blake3** (current spec)? | Cross-component byte-equality replay | blake3 (faster, spec-aligned), migrate code |
| Q-03 | Stage 1B coarse_score: keep next-bar-sign correlation (in-sample peeking) OR rewrite to purged-CV? | DSR contamination risk | Rewrite to purged-CV |
| Q-04 | DSR effective-N: keep magic `10⁷` or derive from cluster size? | Project rejects everything vs accepts noise | Derive cluster-effective-N (Bailey-Lopez de Prado appendix) |
| Q-05 | Throughput canary 50 M/s — keep gate or downgrade? | Gate is currently theatre | Downgrade to 50 K/s OR drop the gate; replace with `test_full_sweep_extrapolation.py` |
| Q-06 | Dashboard default bind: 127.0.0.1 or 0.0.0.0? | Security exposure | 127.0.0.1 (match docs); 0.0.0.0 only with auth-required flag |
| Q-07 | DhanWorker scope: ship-now (drafted in stash) OR continue requirements review? | 6 deferred decisions depend on it | Ship-now after this review; resume PR work |
| Q-08 | Scope-creep cleanup: delete `brutex/options/` + `FUT/CE/PE/FNO` OR keep as Track 2 reference? | Golden Rule #3 scope-lock | Move to `track2/`; cost calculator imports updated |
| Q-09 | NIFTY 50 rebalance handling: hardcode dates or fetch runtime? | Phase 1 deliverable; audit completeness | Hardcode (~10 events 2021-2026); flag in audit |
| Q-10 | TLS pinning: SPKI fingerprint pinning or accept current `verify=True` + `trust_env=False`? | MITM defence | Add SPKI pinning (modest operational cost) |
| Q-11 | Mutation gate: keep 85% on `state.py` only, OR extend to entire `brutex/`? | "100% standards" claim | Extend to all DEC-P0-modified modules; ratchet operator's 95% wish |
| Q-12 | Phase plan rebaselining: re-publish honest "Phase 1 scaffolding complete; Phase 3 engine pending" OR keep aspirational? | Operator-vs-reality drift | Rebaselline; honest envelope per Golden Rule #10 |

---

## §7. Renaissance CFO summary (one paragraph)

BruteX today is a **well-tested ingest + orchestrator-scaffolding project** with disciplined audit/quota/rate-limiter/SSRF/parser primitives and excellent CLAUDE.md governance. It is **NOT yet a backtest engine**. The spec promises a 5-stage funnel + Bloom dedup + Rust kernel + Metal GPU + DSR cascade + lockbox; the live branch has Phase 1 ingest plus the 52-bit vocabulary atoms. The headline "2-4 days for 33B trials" SLA is unmeasured AND the trial count itself is internally inconsistent across docs (33B / 730B / 1.095T). The reproducibility hash, the backbone of bit-exact reruns, hashes 4 inputs where the spec mandates 9. **No Renaissance researcher would deploy this in its current state — but the foundation is honest, the audit chain is solid, and the scope-lock is mostly intact.** The path forward is to (a) reconcile the 12 conflicts in §1, (b) close the 15 gaps in §3 starting with DhanWorker + the engine modules, (c) downgrade the 8 PARTIAL-CLAIMED-AS-REAL items in §2 to honest 🟡 status in their source docs.

## §8. Suggested fix order (operator-prioritisable)

1. **§4 SCOPE-CREEP fixes** (1 PR, 4 small file edits) — fast win
2. **§1 C-11 (stale 92→60 chunk fix)** + **C-12 (Ram Navami date)** — fast doc fixes
3. **§1 C-04 + C-05 (hash composition + sha256→blake3 unification)** — foundational; everything downstream depends on it
4. **§3 G-06 (DhanWorker)** — closes 6 deferred decisions
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
