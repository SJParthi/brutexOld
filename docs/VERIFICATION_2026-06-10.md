# BruteX Workspace Verification — 2026-06-10

> **Decision:** DEC-WORKSPACE-VERIFY-002 (MIGRATION.md entry #609)
> **Branch:** `claude/sharp-mendel-lgbckj`
> **Verified tip:** `3592775` — *"feat(dashboard): show today's persisted
> Dhan quota on /universe page-load (DEC-DASHBOARD-QUOTA-PERSISTED-001)
> (#571)"* (== `origin/main` at run start, 0 ahead / 0 behind).
> **Toolchain:** Python **3.12.3** venv (`[dev,data,ui]` extras), Rust
> `cargo 1.94.1`, both PyO3 kernels (`fno_math` + `trading_core`) built
> with `maturin develop --release`; `brutex doctor` = PASS at bootstrap.
>
> **What this is:** the third full from-scratch, machine-checked health
> baseline of the entire workspace (successors of
> `VERIFICATION_2026-06-02.md` / DEC-WORKSPACE-VERIFY-001 and
> `VERIFICATION_2026-06-06.md`, plus the 2026-06-08
> `WORKSPACE_HEALTH_AND_ROADMAP.md` / DEC-WORKSPACE-HEALTH-REPORT-001).
> Method: the CLAUDE.md §9 hostile-review pattern run as a **5-agent
> verification** — the lead executed every locally-runnable CI gate
> centrally, while 4 parallel read-only agents audited (1) plans/history,
> (2) correctness & data-integrity & Dhan fidelity, (3) doc/spec drift
> since the last verification, (4) O(1) + security + CI/automation.
>
> **Honesty contract (Golden Rule #1 / #10):** every number below is a
> **real measured result** from a command executed in this session on this
> tip — not an estimate, not inherited from an older doc. Targets and
> roadmap items are labelled 📋, never ✅.

---

## 0. TL;DR for a human in a hurry

- **Zero functional / correctness bugs found.** The workspace builds,
  imports, type-checks, and passes every blocking gate that can run in a
  generic Linux container.
- **5,690 tests passed, 0 failed** (33 documented skips) in 6m31s;
  measured coverage **99.15% line / 98.43% branch** (CI floor: 90%).
- Both Rust kernels are green including the **400-case golden
  bit-identity suite** shared with TickVault (DEC-SHARED-CORE-001).
- The audits surfaced **no code defects** — only 4 drifted line-number
  citations in CLAUDE.md §6 (fixed in this PR), one stale active-branch
  line (fixed), and two pre-existing housekeeping items recorded as
  follow-ups (§7): duplicate MIGRATION.md entry numbers, and two
  Golden-Rule-#5 hot paths still missing a *dedicated* O(1) bench.

## 1. Gate matrix — every row executed this session (2026-06-10)

| # | Gate (CI job equivalent) | Command | Measured result |
|---|---|---|---|
| 1 | Lint | `ruff check brutex tests` | ✅ all checks passed |
| 2 | Format | `ruff format --check brutex tests` | ✅ clean |
| 3 | Types (strict) | `mypy --strict brutex` | ✅ **0 issues in 214 source files** |
| 4 | Types (pyright) | `pyright brutex tests` | ✅ **0 errors** (133 advisory warnings, exit 0) |
| 5 | Main suite + coverage | `pytest --cov=brutex --cov-branch --cov-fail-under=90 tests/unit tests/integration tests/property` | ✅ **5,690 passed / 33 skipped / 0 failed** (390.6s); **99.15% line / 98.43% branch** |
| 6 | Smoke + red-team | `pytest tests/smoke tests/redteam` | ✅ **71 passed** (4.7s) |
| 7 | Golden masters | `pytest tests/golden` | ✅ **123 passed** (8.2s) |
| 8 | Stress (bounded) | `pytest tests/stress` | ✅ **9 passed** (2.9s) |
| 9 | O(1) NFR benchmarks | `pytest tests/bench` (CI flags) | ✅ **105 passed / 4 skipped** (130.7s; skips = M4-Pro-only absolute gates per DEC-P0-020 honest envelope) |
| 10 | Strict per-module 100% gates | the full ci.yml step (146 pytest invocations) | ✅ **exit 0 — every per-module 100% line+branch gate green** |
| 11 | Security (SAST) | `bandit -r brutex -l` | ✅ 0 findings (exit 0) |
| 12 | Dependency CVEs | `pip-audit --strict` on the resolved declared-dep closure | ✅ no known vulnerabilities |
| 13 | Docstrings | `interrogate brutex` (CI flags) | ✅ **100.0%** (gate: 100%) |
| 14 | Rust kernel #1 | `cargo fmt --check && cargo check && cargo clippy -- -D warnings && cargo test --release` in `rust/fno-math` | ✅ all green |
| 15 | Rust kernel #2 (shared core) | same in `rust/trading-core` | ✅ all green — **13 tests incl. the 400-case golden bit-identity vectors + 52-bit both-ways property** |
| 16 | Banned patterns | `scripts/ci/scan-banned-patterns.sh` | ✅ none found |
| 17 | Spec ↔ test crosswalk | `scripts/ci/spec_test_crosswalk.py` | ✅ all required tests present |
| 18 | DEC-ID / no-hallucination scans | `scripts/ci/validate_dec_ids.py` + `validate_no_hallucination.py` | ✅ pass (gates live; enforced per-PR on the PR body) |
| 19 | Runtime health | `brutex doctor` (bootstrap step 7) | ✅ PASS |

Not runnable in this container (run by CI on the PR instead): CodeQL,
Semgrep, Trivy, gitleaks, SBOM, jscpd duplication, vulture, actionlint,
license compliance, mutation (nightly/weekly), chaos-race job. All of
these are wired into the required `CI OK (aggregate gate)` and were
green on the verified tip's own merge (#571).

## 2. What the 4 audit agents verified (hostile, evidence-cited)

**Correctness & data-integrity spine — all VERIFIED:**

- **Price integrity:** 2-decimal tick-grid snap is `ROUND_HALF_UP`,
  never truncation (`brutex/money.py:50`); prices stored as exact
  `Decimal` rupees (`brutex/data/candle_fields.py:71-84`,
  `data/models.py`).
- **Run identity:** 9-field **BLAKE3** combo hash
  (`brutex/strategies/combo.py:91-102`, Rust `fno_math.blake3_hash`) —
  the SHA256 era is fully retired (DEC-P0-060).
- **Audit trail:** hash-chained JSONL with `verify_chain()`
  (`brutex/orchestrator/audit.py` + BLAKE3 sweep sibling
  `brutex/engine/sweep_audit.py`, unified verifier
  `brutex/audit_verify.py`) — tested at unit / integration / property /
  stress / chaos depth.
- **Look-ahead:** BOTH guards live — timestamp guard
  (`ReplayFeed.ensure_no_lookahead`, `brutex/feed/replay.py:184-208`)
  AND the P-06 index barrier (`brutex/feed/lookahead.py`
  `LookAheadBarrier`), each with unit + golden + property + O(1) bench
  coverage.
- **Engine isolation:** India VIX and every F&O single stock are
  `reference_only`; the swept universe is pinned to exactly
  NIFTY / BANKNIFTY / SENSEX by `brutex/universe.py:118-120,225-236`
  plus `tests/property/test_engine_isolation.py` and golden corpus #4.
- **Dhan fact table:** every CLAUDE.md §6 **value** matches the code
  (base URL, 5 r/s + 100,000/day, [3..5] r/s adaptive band with the
  documented storm floors, 90/30-day chunk windows, 1-minute-only
  interval, RenewToken = GET headers-only). Four rows cited
  **drifted line numbers** (constants moved by ~7 lines since
  2026-06-01) — corrected in this PR.
- **Secrets:** no credential material in the repo; token never reaches
  logs or the token-events Parquet (token-free schema, masked
  client-id, `~/.brutex/credentials.json` 0600 atomic write with
  insecure-perms refusal).
- **TODO scan:** no unfinished correctness-critical work — the only
  TODOs are tracked engine-PR deferrals (`strategies/combo.py`,
  `strategies/sweep.py`) and one documentation note (`dhan/body.py:76`).

**O(1) discipline — 11 live bench gates verified** (LMDB membership,
Bloom probe, WorkItemStore, WindowLedger skip probe, results-store
get/append, per-bar trade-sim push, supermask Rust kernel, resident bar
buffer, math-facade symbol lookup, throughput canary, option dedup) —
plus the recently-added surfaces re-checked at source level:
`WindowLedger.is_skippable` is a dict + frozenset probe
(`brutex/data/window_ledger.py`), `/api/v1/quota` is one LMDB key read
(`brutex/dashboard/app.py:844-862`), token-events is append-only
crash-safe Parquet. **Gap (follow-up §7):** no *dedicated* bench yet for
ReplayFeed bar-index lookup and indicator-value lookup (hot paths 1-2 of
Golden Rule #5).

**CI/automation:** auto-merge requires the `CI OK (aggregate gate)`
check to exist and EVERY check green, then refuses
`mergeable_state ∈ {dirty, behind, blocked, unknown}`
(DEC-AUTOMERGE-SAFETY-001, `.github/workflows/auto-merge.yml:281-317`);
`conv-commits` IS in `ci-ok.needs` (ci.yml:2812). The watchdog +
Telegram notification path exists and is coverage-gated.

## 3. Drift scorecard — the 10 findings of DEC-WORKSPACE-VERIFY-001, re-audited

| Item (from 2026-06-02) | Status today | Evidence |
|---|---|---|
| D1 instruments PK inconsistency | ✅ FIXED | DEC-DHAN-NATIVE-SCHEMA-001 + DEC-INSTRUMENT-PK-SEGMENT-001 (ADR-0003) |
| D2 P-06 accessor was a skip stub | ✅ FIXED (runtime) | `LookAheadBarrier` LIVE (DEC-LOOKAHEAD-BARRIER-001); catalog row now *delegates* to `tests/golden/test_lookahead_golden.py` + `tests/property/test_lookahead_properties.py` |
| D3 stale per-module gate count | ✅ FIXED | STANDARDS_100PCT.md reconciled (DEC-ORCH-LAZY-BLOOM-001 lifted the ABI3 blocker) |
| D4 stale [4..8] r/s Groww band | ✅ FIXED | STANDARDS_100PCT.md re-pinned to Dhan `[3..5]` (`rate_control.py`) |
| D5 `test_store_o1.py` docstring 3×-vs-5× nit | ✅ FIXED *(amended same day)* | re-grepped in the DEC-O1-BENCH-GAPS-002 follow-up: the docstring already reads `5×` matching `ratio_ceiling(5.0)` and labels the `REQUIREMENTS.md` mention as the retired stub — the audit-agent claim was stale (its third stale claim alongside G1/G3) |
| D6 Groww spec lacked SUPERSEDED banner | ✅ FIXED | `docs/TRACK1_GROWW_ADAPTER.md:1-10` carries the ⛔ banner |
| D7 "bit-for-bit reproducible" not provable | 🟡 OPEN BY DESIGN | honestly enveloped in STANDARDS §8; L3 replay oracle live; Parquet bit-equality is Phase 2/3 |
| G1 auto-merge ignored `mergeable_state` | ✅ FIXED (materially) | DEC-AUTOMERGE-SAFETY-001 refuses dirty/behind/blocked/unknown; residual nuance *(closed same day)*: the `unstable` allowance is now the charter §4.5 documented pre-G2 envelope (DEC-AUTOMERGE-CLEAN-STRICT-001) — run-log sample: `unstable` 21/30 gate executions incl. every ready_for_review-path merge, `clean` only 4/18 merges (workflow_run path); strict `clean` stays the G2-gated ratchet |
| G2 server-side branch protection unapplied | 🟡 STILL OPEN | operator one-time UI setup (`docs/runbooks/github-branch-protection-setup.md`); `branch-protection-drift.yml` detects + alerts only |
| G3 conv-commits not in `ci-ok.needs` | ✅ FIXED | `ci.yml:2812` (commented "DEC-AUTOMERGE-SAFETY-001 (G3)") |

**New findings this session (none are code defects):**

1. **CLAUDE.md §6 line-citation drift (4 rows)** — rate-limit /
   chunk-window rows cited `constants.py:86-96`; real lines are
   `93/96/100/103`. **Fixed in this PR.**
2. **CLAUDE.md §1/§3 active-branch staleness** — cited a deleted
   branch. **Fixed in this PR.**
3. **MIGRATION.md duplicate entry numbers** — 28 entry numbers are
   used more than once (160-166, 214, 434, 436, 449, 547-550, 578-592;
   566 numbered entries total). Root cause: parallel sessions appended
   independently-numbered blocks (the #537 lineage-integration commit
   era). Mostly *different decisions sharing a number*; at least one
   (#214) is the same text pasted twice. DEC-IDs remain unique, so the
   ledger is unambiguous by DEC-ID — but "entry #NNN" citations in the
   590s are ambiguous. **Tracked follow-up §7 — NOT renumbered here**
   (renumbering would silently break historical citations; needs its own
   reviewed PR).
4. **Two hot paths without a dedicated O(1) bench** (bar-index lookup,
   indicator-value lookup) — partially covered indirectly
   (resident-bar-buffer + throughput canary). **Tracked follow-up §7.**

## 4. Finished / current / future

### 4.1 Finished — the last ~40 merged PRs (#531-#571), by theme

| Theme | PRs | Decision IDs | What it delivered |
|---|---|---|---|
| Window-ledger rework — one durable per-window skip authority | #543-#548, #551-#558 | DEC-WINDOW-LEDGER-001 (stages) | Replaced the coarse span ledger + JSON band-aids with ONE queryable Parquet/DuckDB per-window ledger: every planned fetch window is SUCCESS/EMPTY/FAILED/SKIPPED, skip decisions are O(1) structural probes, FetchCoverage deleted. No window can be silently masked again. |
| Token lifecycle, durable + auditable | #538, #549, #550 | DEC-DHAN-TOKEN-SINGLE-SOURCE-001, DEC-TOKEN-EVENTS-001 | One write-through durable token holder; every save/renew/expire/refuse lands in a token-free, masked, crash-safe Parquet trail browsable on /db. |
| Dhan rate control + no-data classification | #540-#542, #568, #569 | DEC-DHAN-RATE-RECOVERY-TRICKLE-001, DEC-DHAN-NODATA-SETTLED-001, DEC-FAILED-WINDOW-FORCE-REPULL-001, DEC-DHAN-NO-DATA-GAP-COUNT-001 | The limiter probes back up after mild 429 drips instead of pinning at the floor; failed windows always re-pull; benign DH-905 "no data" boundaries count as amber *gaps*, never red *failures*, on both spot and options arms. |
| Options-only eligibility guard | #564-#566 | DEC-OPTIONS-ELIGIBLE-GUARD-001 | Data-driven (vendor-master-derived) OPT vs spot-only detection wired into ingest + OPT/SPOT badges on /universe and /reports — a mis-click can no longer waste the 100k/day quota on options that don't exist. |
| Daily-quota visibility | #567, #571 | DEC-DASHBOARD-QUOTA-GAUGE-001, DEC-DASHBOARD-QUOTA-PERSISTED-001 | Live 100,000/day gauge on /universe (80%/95% colour bands), correct from page-load via the O(1) `/api/v1/quota` LMDB read. |
| Operator-visible time correctness | #561-#563 | DEC-PREOPEN-XLSX-DATE-001 (+IST display fix) | All ledger/token/coverage timestamps render in IST; pre-open .xlsx files no longer show "1 Jan 1980". |
| Engine validation arc (earlier in the window) | #531, #529 | DEC-WALK-FORWARD-OOS-001, DEC-EXEC-GOLDEN-PIN-001 | Rolling walk-forward OOS mode; execution-driver geometry pinned to a frozen golden oracle. |
| Reliability hardening | #533, #534, #536, #537 | DEC-RATE-STORM-WATCHDOG-001 et al. | Retry-storm give-up guard + 100%-failure alert; mutation gate extended to DSR; renew-token self-destruct fixed; 7-PR lineage integrated. |

### 4.2 Current state (what runs end-to-end today)

| Component | Status |
|---|---|
| Dhan adapter (REST, token auth, SPKI pin, adaptive [3..5] r/s, quota tracking) | ✅ production-ready |
| Spot ingest → Parquet/DuckDB lake (NIFTY/BANKNIFTY/SENSEX + India VIX, 1m + 1d, resample to 21 TFs) | ✅ works end-to-end (`make run`) |
| Options-chain ingest (rollingoption, transactional, audited) | ✅ implemented, guard-protected |
| Window ledger + token events + quota gauge + OPT badges (dashboard) | ✅ live |
| Sweep engine (Stage 1A/1B/2 + leaderboard + OOS/DSR/CPCV verdicts via `brutex sweep`) | ✅ runnable on lake data |
| Stage-2.5 SPRT early-stop | 📋 DESIGN-BLOCKED on 5 recorded items (DEC-SPRT-DESIGN-BLOCK-001) |
| Stage-3 BIPOP-CMA-ES refinement, options Greeks/BSM realism | 📋 roadmap (Phase 3/4) |
| First production 33B-trial sweep on the M4 Pro envelope | 📋 awaits the first full live ingest + Phase 3 |

### 4.3 Future — tracked, decision-ID'd work

1. **Per-window lake-completeness probe** (DEC-WINDOW-LEDGER-001
   residual) — replace span-enclosure reconciliation entirely.
2. **Orchestrator standalone-coverage backfill** (STANDARDS row 1
   ratchet — 8 modules currently 100% via global cov only).
3. **Mutation zero-survivor set → ranking math**
   (DEC-MUTATION-GATE-RANKING-001 ratchet).
4. **M4 Pro strict 1.1× O(1) tier + 50 M/s absolute floor**
   (DEC-O1-STRICT-TIER-001 — wired, awaits self-hosted runner).
5. **SPRT unblock items B1-B5** (DEC-SPRT-DESIGN-BLOCK-001), then
   Stage-2.5.
6. **actionlint shellcheck enablement** (DEC-ACTIONLINT-SHELLCHECK-001).
7. **Server-side branch protection** (G2 — operator one-time setup).
8. **This session's new follow-ups** — §7 below.

## 5. Plain-English comparison view (for any reader)

**The factory inspection certificate — measured today:**

| Question a customer would ask | Answer (measured 2026-06-10) |
|---|---|
| Does it build and start? | ✅ Yes — one command (`make setup`), both Rust engines compiled, health doctor PASS |
| Do the tests pass? | ✅ **5,690 of 5,690** runnable tests passed; 0 failures |
| How much of the code is exercised by tests? | ✅ **99.15%** of lines and **98.43%** of branches |
| Is the math provably identical to the shared Rust core? | ✅ Yes — **400 of 400** frozen golden answer-key cases reproduce bit-for-bit |
| Can it look into the future by accident (the classic backtest lie)? | ✅ No — two independent runtime guards, both tested, one benchmarked O(1) |
| Is money handled exactly? | ✅ Yes — exact decimal rupees, half-up rounding to the paisa, never float drift |
| Is every action auditable? | ✅ Yes — tamper-evident hash-chained logs, verified by unit / property / stress / chaos tests |
| Will it overrun the data vendor's limits? | ✅ No — hard 5 req/s + 100,000/day caps enforced + a live dashboard gauge |
| Is it fast in the way that matters? | ✅ Lookup/dedup/append hot paths hold **O(1) ratio gates** (105 benchmark assertions green) |
| Is it finished as a product? | 🟡 Honestly: **no** — the research engine + data plane are production-grade; the smart-search (SPRT/CMA-ES), options execution realism, and the first full live-data production sweep are the remaining roadmap |
| Were any defects found in this inspection? | ✅ **Zero functional defects** — 2 doc-citation fixes shipped, 4 housekeeping follow-ups recorded |

**Then vs now (the three verification baselines):**

| Measure | 2026-06-02 (#398) | 2026-06-08 (#578) | **2026-06-10 (this)** |
|---|---|---|---|
| Tests passed / failed | 3,252 / 0 | 5,428 / 0 | **5,690 / 0** |
| Coverage (line) | 97.88% | ~98.8% | **99.15%** |
| mypy --strict files clean | 150 | 208 | **214** |
| Rust shared-core golden cases | n/a (pre-core) | 400/400 | **400/400** |
| Open doc-drift findings | 7 + 3 gaps | (4 fixed) | **2 minor + 2 housekeeping** |
| Functional bugs found | 0 | 0 | **0** |

## 6. Honest envelope (Golden Rule #10)

- ✅ Everything in §1 is green and was actually executed here.
- 🟡 This container is NOT the production envelope: the M4-Pro-only
  absolute gates (50 M/s floor, 33B-trial SLA, strict 1.1× tier) were
  honestly SKIPPED (4 bench skips) — ratio gates stand in, per
  DEC-P0-020.
- 🟡 CodeQL / Semgrep / Trivy / gitleaks / SBOM / jscpd / vulture /
  mutation / chaos-race run on GitHub CI, not locally — they gate this
  PR via the required `CI OK (aggregate gate)`.
- 🟡 STANDARDS_100PCT.md §1 remains **15 🟢 / 2 🟡 / 1 📋 / 3 👁️ of 21
  sub-rows (~71% fully enforced)** — no "100% standards compliant"
  claim is made or permitted.
- 🚫 No live Dhan calls were made (no token in this environment); the
  Phase-0 empirical probes (India VIX on the historical endpoint, index
  SECURITY_IDs, expiry-code numbering) remain UNVERIFIED as §6 already
  declares.
- 🚫 This PR changes documentation only — zero `brutex/`, `rust/`, or
  `tests/` source lines touched; the engine is bit-identical to #571.

## 7. Tracked follow-ups raised by this verification

| # | Item | Why | Suggested decision ID |
|---|---|---|---|
| F-1 | Reconcile MIGRATION.md duplicate entry numbers (28 collisions; keep DEC-ID as primary key, add disambiguators rather than renumber history) | "entry #NNN" citations in the 578-592 range are ambiguous | DEC-LEDGER-RENUMBER-001 |
| F-2 | ~~Dedicated O(1) ratio benches for bar-index lookup + indicator-value lookup~~ **CLOSED same day** by DEC-O1-BENCH-GAPS-002 (`tests/bench/test_bar_lookup_o1.py` + `test_indicator_lookup_o1.py`) | the last 2 of the 5 Golden-Rule-#5 hot paths now have isolated gates | DEC-O1-BENCH-GAPS-002 |
| F-3 | ~~Fix `tests/bench/test_store_o1.py` docstring~~ **NO CHANGE NEEDED** — re-checked in the F-2 follow-up: already correct; the D5 agent claim was stale | D5 residue dissolved | — |
| F-4 | ~~Tighten auto-merge to strict `clean` or amend the charter~~ **CLOSED same day** by DEC-AUTOMERGE-CLEAN-STRICT-001 — run-log evidence (`unstable` 21/30 gate executions; the ready_for_review path can NEVER observe `clean` — the gate's own pending check run keeps the head's rollup non-passing; `clean` only 4/18 merges, all workflow_run-path) chose AMEND: charter §4.5 now documents the pre-G2 denylist envelope; strict `clean` is the G2-gated ratchet; redteam pins workflow ↔ charter. The same review closed 3 latent gate bugs found en route (in-flight-CI existence hole observed on #571, the null field-shift refusal, the dead workflow_dispatch hatch) | G1 residual nuance dissolved | DEC-AUTOMERGE-CLEAN-STRICT-001 |

---

*Compiled by the 5-agent workspace verification, 2026-06-10. Every
figure traces to a command executed in this session or a file:line cited
inline. Predecessors: `VERIFICATION_2026-06-02.md`,
`VERIFICATION_2026-06-06.md`, `WORKSPACE_HEALTH_AND_ROADMAP.md`.*

*Amendment (2026-06-10, DEC-O1-BENCH-GAPS-002, MIGRATION.md #610): F-2
closed (two new bench gates; measured ratios 1.07×–2.61× vs the 5×
ceiling), F-3 dissolved and the D5 scorecard row corrected — the
docstring was already fixed and the audit-agent claim was stale.*

*Amendment (2026-06-10, DEC-AUTOMERGE-CLEAN-STRICT-001, MIGRATION.md
#612): F-4 closed and the G1 scorecard row's residual nuance dissolved.
Auto-merge run-log evidence (2026-06-03→06-10 sample: 1,230 runs
listed, 30 gate executions verified — `unstable` 21 incl. EVERY
`ready_for_review`-path merge, whose own pending check run makes
`clean` structurally unobservable; `clean` only 4 of 18 verified
merges, all `workflow_run`-path; `dirty`/`behind`/`blocked`/
`has_hooks` never observed) chose AMEND over TIGHTEN: strict `clean`
today would deadlock auto-merge. Charter §4.5 now documents the pre-G2
denylist envelope (refuse `dirty`/`behind`/`blocked`/`unknown`; every
other state passes, red checks refused upstream by the all-green
ci-check); CLAUDE.md §13(a) + the PR template banner aligned; the
strict-`clean` ratchet stays tied to G2; redteam pins workflow ↔
charter both ways. The hostile review additionally found and the same
PR FIXED three latent gate bugs: (1) the in-flight-CI existence hole —
PR #571 merged 19:56:26 while its pull_request CI run (whose `CI OK`
check run did not exist yet) ran until 19:57:03 (runs 27231261106 /
27231295196); ci-check now requires every push/pull_request CI run on
the head COMPLETED + a green pull_request verdict (self-healing: a
refused in-flight run re-triggers on its own completion); (2) the
null field-shift — an uncomputed `mergeable: null` rendered as an
empty TSV field, `read` shifted "unknown" into `mergeable`, the poll
loop broke on attempt 1 and the DEC-AUTOMERGE-RACE-FIX-001 deferral
was dead code (all 4 sampled refusals show `mergeable=unknown
mergeable_state=`); jq now defaults null fields to the string "null";
(3) the `workflow_dispatch` "manual escape hatch" was dead code (it
emitted only `pr_number`, so trust-check always read empty metadata →
`trusted=false`; zero dispatch runs exist in history); it now resolves
author/head_ref/is_draft and passes through the same gates.*
