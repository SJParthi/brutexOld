# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate. Per Golden Rule #10, partial /
> roadmap / out-of-scope rows are disclosed in the Honest envelope below.

**Status: DRAFT** — the mega-club it was slotted behind has LANDED on main and is merged forward into this branch (head `57978fc8d`); still awaiting the coordinator's slot/ready call — do not merge/ready until the coordinator slots it.

## Before / After (plain language)

**Before:** the three read-boundary integrity detectors — raw-integrity hashing (`raw_integrity.py`), interior-gap runs (`interior_gap_detect.py`), and partial-window truncation verdicts (`partial_window_detect.py`) — run Python-only.

**After:** each law also has a Rust arm on the `rust/brutex-lake` crate behind three default-OFF env flags (`BRUTEX_LAKE_RUST_RAW_INTEGRITY` · `BRUTEX_LAKE_RUST_INTERIOR_GAP` · `BRUTEX_LAKE_RUST_PARTIAL_WINDOW`), with byte-identical verdicts and the verbatim Python law retained as the LIVE parity oracle on every path. Flag OFF (the default) is byte-identical AND the crate is never imported (bomb-spy pinned).

**How:** the established facade pattern (`brutex/data/detectors_rust.py`) — per-law 16-call burn-in bit-compare with ship-the-ORACLE autorevert, refuse-poison shape gates including the 7-token reason gate on the partial-window verdict, the imported panic belt over the `#[pyfunction]` surface, and an in-crate RFC-7693 BLAKE2b implementation so Cargo.toml/Cargo.lock stay byte-untouched (zero new dependencies). Calendar + JSON canonicalisation stay Python; off-grid session walks ABSTAIN → oracle (the DEC-FEED-RUST-CORE-001 abstain law).

> **Honesty banner (ADR-0020, verbatim):** expected payoff ≈ 0 — conversion is for uniformity, not speed; ships INERT — a default flip needs its own DEC.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered

- Happy: each detector flag ON → Rust verdict bit-equal to the Python oracle (burn-in verified per process; 97-test new battery + existing detector suites flag-ON).
- Edge: `< 2` covered sessions → `[]` (interior gap); off-grid session walks ABSTAIN → oracle (partial window); RFC appendix-A + 11 hashlib-frozen BLAKE2b vectors; span cap 9000 days.
- Empty/zero: empty payloads route through the oracle-identical arms; flag unset/blank → OFF silently.
- Failure: crate absent/stale → named degrade to the oracle; a poisoned crate result (bad shape / unknown reason token) is REFUSED → oracle (refuse-poison gates); a crate panic is caught by the panic belt → degrade; a burn-in mismatch ships the ORACLE value + autorevert-latches the process.
- Concurrent: one shared autorevert latch per law per process; detectors are pure functions — no shared mutable state crosses the FFI.

### Failure modes

- Wheel missing / API-surface mismatch → warn-once + permanent oracle degrade (flag effectively OFF).
- Burn-in bit-mismatch → the oracle value is SHIPPED, the law autoreverts for the process, one ERROR event.
- Crate raise/panic → caught, degrade to oracle, never a new crash class.
- Poisoned output (shape / reason-token gate) → refused wholesale, oracle serves.
- MemoryError / KeyboardInterrupt / SystemExit re-raise raw (never swallowed).

### Real-run proof

```
BRUTEX_LAKE_RUST_RAW_INTEGRITY=1 BRUTEX_LAKE_RUST_INTERIOR_GAP=1 BRUTEX_LAKE_RUST_PARTIAL_WINDOW=1 \
  .venv/bin/python -m pytest tests/unit/test_raw_integrity.py tests/unit/test_interior_gap_detect.py \
  tests/unit/test_partial_window_detect.py tests/unit/test_detectors_rust.py -q
```

(flag-ON: the existing detector suites + the new battery drive the REAL built wheel; the completeness refuter additionally executed 290k+ zero-mismatch differential trials, re-confirmed with 60k trials in round 2.)

### Rollback plan

- Ships INERT: the flags are default-OFF, so the default path is byte-identical — no operator action needed.
- Instant per-process disable: leave/set the three env flags unset/0.
- Full revert = revert this PR's commits; no schema/lake/run-identity change to unwind.

### Out of scope

- `window_status` — EXCLUDED from the whole slice (write-retired; its classify law already moved under DEC-LAKE-RUST-WINDOW-LEDGER-001 #1915; residual is Stage-6 deletion territory).
- `bad_tick` — COMMON-reuse ruling recorded: a later PR must REUSE the already-shipped `rust/brutex-feed` predicate (DEC-FEED-RUST-CORE-001 #1797), never mint a second Rust copy.
- The hot modules (`bad_tick` / `price_sanity` / `today_completeness`) — deliberately DEFERRED out of this PR (the mega-club they were slotted behind has now landed; they are follow-up-slice territory, still NOT in this PR).
- Any default flip of the three flags (needs its own DEC).

---

## Summary

- SLICE T24 PR-1: the THREE cold data-integrity detector pure laws (raw-integrity BLAKE2b digest · interior bar-less run walker · partial-window truncation verdict) routed through `rust/brutex-lake` (`src/detectors.rs`) behind THREE default-OFF flags via the new facade `brutex/data/detectors_rust.py`.
- Byte-identical verdicts, oracle retained live; flag OFF = byte-identical + crate never imported.
- In-crate RFC-7693 BLAKE2b so Cargo.lock is byte-untouched (zero new deps).

## Spec / decision links

- Decision ID(s): DEC-DATA-RUST-INTEGRITY-DETECTORS-001
- MIGRATION.md row(s): #1948 (coordinator-assigned; re-verified FREE against the live main tip at merge-forward)
- Spec section(s) touched: ADR-0020 conversion program (T24 is the mandate-added cold-detector slice; scope-amendment pointer recorded in #1948 — ADR-0020 itself deliberately not edited)

## Type of change

- [x] feat — new feature
- [ ] fix / docs / refactor / test / chore / perf

## Golden Rule #12 — 100% standards self-check

### Coverage + tests

- Coverage line 100.00% + branch 100.00% on all four touched `brutex/data/` modules (facade + 3 detectors)
- Property/differential load carried by the Rust unit matrix + the refuter's 290k+ zero-mismatch differential trials
- Golden: RFC appendix-A + 11 hashlib-frozen BLAKE2b vectors
- No hot-path change — no new bench required (cold detectors; ships INERT)

### Bug fixes

- N/A — feat, not fix

### Type / lint / security

- `mypy --strict` passes (476 files, re-run after merge-forward)
- `pyright` passes (0 errors on touched files)
- `ruff check` passes on every touched file (re-run after merge-forward)
- No new copy-paste — the facade follows the ONE established lake-rust pattern; BLAKE2b in-crate is the ONE Rust copy (bad_tick reuse ruling recorded so no second copy can land later)

### Performance

- No new O(n) where O(1) was achievable (the laws keep their existing complexity; the Rust arm mirrors them)
- No pandas; no new global mutable state (one warn-once/autorevert latch per law, process-local by design per the house facade pattern)

### Observability

- Degrades/autoreverts emit structlog events (warn-once + ERROR on autorevert); no `print`
- No new metric / CRITICAL condition (inert, default-OFF)

### Documentation

- NumPy-style docstrings on public symbols
- MIGRATION.md #1948 + CLAUDE.md header/§1/§3 blurbs updated
- No silent scope expansion — slice scope + the two standing exclusion rulings recorded in #1948

### No hallucination (Golden Rule #1)

- BLAKE2b sourced from RFC 7693 (appendix-A vectors pinned); reason tokens byte-equal to the Python law; no invented constants

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | 4× 100.00% line+branch on the touched modules: `detectors_rust.py` (204/64) · `raw_integrity.py` (63/6) · `interior_gap_detect.py` (105/34) · `partial_window_detect.py` (114/38) | ci.yml coverage discovery gate |
| 100% audit coverage | N/A — pure decision laws; no material-action writer touched, no audit-JSONL schema change (the ledger record is MIGRATION.md #1948) | audit-chain CI gate unchanged |
| 100% testing coverage | unit (97-test battery) + integration (existing detector suites flag-ON, 96 passed) + property (4 hypothesis differentials, remaining=0) + golden (RFC appendix-A + 11 hashlib-frozen vectors) + cargo (90 passed `--release --locked`); bench N/A (cold path, ships INERT); mutation via the mutation.yml pair wiring | pytest + cargo + mutation.yml |
| 100% code checks | ruff + ruff-format clean; `mypy --strict` clean (476 files); pyright 0 errors; bandit rides the standing pre-push/CI gates | pre-push hooks + ci.yml |
| 100% code performance | N/A — cold detectors off the Golden-Rule-#5 hot paths; the FFI hop is a disclosed small slowdown (payoff ≈ 0 banner, ADR-0020) | no new bench required |
| 100% monitoring | structlog degrade/autorevert events (warn-once registry + one ERROR per autorevert) | grep brutex/data/detectors_rust.py |
| 100% logging | every degrade path emits a named structlog event; no `print` | grep -r structlog |
| 100% alerting | N/A — inert default-OFF conversion; no new CRITICAL condition to alert on | brutex/observability/ unchanged |
| 100% security | no secret/credential/network surface touched; env-flag parse via the imported `env_flag_tristate`; no new deps (Cargo.toml/Cargo.lock byte-untouched) | pre-push gate |
| 100% security hardening | no new `noqa`/`# pragma: no cover`; refuse-poison gates mean a hostile crate result can only degrade, never corrupt | grep noqa |
| 100% bug fixing | feat not fix; the round-1 review NITs were folded at commit `a803cbc` (ledger count re-measure, panic-belt wording, parity-lane framing, reason-token gate) | review record below |
| 100% scenarios covering | failure-mode battery covers crate absent/stale, panic, poisoned shape/reason-token, burn-in mismatch, MemoryError/KeyboardInterrupt/SystemExit re-raise (see Failure modes above) | pytest -q battery |
| 100% functionalities covering | every new public symbol driven by the 53-case facade matrix + the 3 stem-matched seam suites | pre-push gate |
| 100% code review | 4-agent hostile review evidence below (2 consecutive clean rounds) | this section |
| 100% extreme check | flag-OFF bomb-crate spy pins (crate never imported) + burn-in bit-compare + refuse-poison gates are the mechanical anti-regression rails | every commit |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) | confirmed — no feed/ingest surface touched |
| WS never disconnects | N/A — no WebSocket in Track 1 | confirmed |
| Never slow/locked/hanged | N/A behavioral change — cold detector laws, default-OFF; the engaged FFI hop is a disclosed small slowdown (payoff ≈ 0, ADR-0020), never a lock/hang (pure functions, no I/O in the crate) | flag-OFF path byte-identical (spy-pinned) |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB; neither is touched by this PR | confirmed |
| O(1) latency | N/A — cold detectors are not on a Golden-Rule-#5 hot path; the laws keep their existing complexity (no new O(n) where O(1) was achievable) | no bench gate required (stated, not hidden) |
| Uniqueness + dedup | N/A — no dedup key / composite-key change; none of the three laws is in run identity | confirmed in MIGRATION.md #1948 |
| Real-time proof | the Real-run proof command above — flag-ON suites against the REAL built wheel | this PR body (Real-run proof section) |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | flag-OFF bomb-crate spy pins detect any accidental crate import on the default path; the per-law 16-call burn-in bit-compare detects Rust↔Python drift at call time; the parity lane (`lake-rust-parity.yml`) gains the T24 paths + a flag-ON leg |
| VERIFY | the 97-test battery + 4 hypothesis differentials (remaining=0) + 23 new cargo tests (RFC appendix-A + 11 hashlib-frozen BLAKE2b vectors) + the existing detector suites re-run flag-ON (96 passed) verify end-to-end verdict equality |
| RECONCILE | additive only — the verbatim Python law is retained as the LIVE parity oracle on every path; flag OFF (default) is byte-identical, so no existing state needs reconciling |
| PREVENT | refuse-poison shape gates + the 7-token reason gate refuse a law-contradicting crate result wholesale (a poisoned crate can only degrade, never corrupt); the mutation.yml pair wiring + the OFF-path spy pins prevent silent re-introduction |
| AUDIT | MIGRATION.md #1948 is the durable decision record; CLAUDE.md header/§1/§3 blurbs updated; no runtime audit-JSONL change (no material-action writer touched) |
| RECOVER | rollback = leave the three flags unset (default-OFF, zero operator action) or revert the PR's commits; no schema/lake/run-identity change to unwind |
| COOLDOWN | ships INERT default-OFF — the production default path is byte-identical, so no post-merge soak is needed; a future default flip carries its own DEC + burn-in/soak plan |

## 4-agent hostile review

Honest framing (Golden Rule #10): the review evidence for this PR is FOUR hostile-reviewer agent runs across TWO consecutive clean rounds — a general hostile reviewer + a completeness refuter per round — each run carrying the canonical lenses (perf/O-complexity, security, correctness, cite/path — `docs/zplus/ZPLUS_PR_DISCIPLINE.md`) jointly rather than as four single-lens agents.

| Agent run | Lens | Findings | Severity |
|---|---|---|---|
| Round-1 general hostile reviewer | correctness + security + O-complexity honesty + coverage honesty | SHIP — 0 MINOR+; 4 NITs (ledger count re-measure, panic-belt docstring wording, parity-lane belt framing, reason-token gate hardening) folded at commit `a803cbc` | 0 blocking |
| Round-1 completeness refuter | differential completeness vs the Python oracle | SHIP — 290k+ zero-mismatch differential trials executed | 0 |
| Round-2 general hostile reviewer | same joint lenses on the folded head | SHIP — clean (0 findings) | 0 |
| Round-2 completeness refuter | differential re-confirmation | SHIP — clean; 60k-trial re-confirmation, zero mismatches | 0 |

Review evidence: the Review record section below (round/commit trail) + MIGRATION.md #1948 (per-finding record).

---

## Honest envelope (Golden Rule #10)

- ✅ REAL: the three detector laws routed to Rust with byte-identical verdicts (burn-in + differential evidence above); flag OFF byte-identical AND crate never imported (bomb-spy pinned); 4× 100.00% line+branch; cargo 90 passed `--release --locked`; Cargo.toml/Cargo.lock byte-untouched (in-crate RFC-7693 BLAKE2b, zero new deps).
- ✅ REAL (honesty banner, ADR-0020 verbatim): expected payoff ≈ 0 — conversion is for uniformity, not speed; the engaged FFI hop is a small slowdown on cold paths.
- 🟡 PARTIAL: ONE SHARED autorevert latch across the three laws (a burn-in trip on any law reverts all three for the process — the PR-2 precedent, disclosed in MIGRATION.md #1948); the flag-ON reruns of the existing suites are the BELT — the genuine parity gates are the seam remaining=0 differentials + the cargo frozen vectors (the round-1 reframe); NO polars version guard (the laws are polars-free — the disclosed G8 deviation).
- 📋 ROADMAP: any default flip of the three flags (needs its own DEC); the `bad_tick` COMMON-reuse follow-up (reuse the `rust/brutex-feed` predicate, DEC-FEED-RUST-CORE-001 #1797 — never a second copy); the hot modules deferred until the mega-club lands; `window_status` Stage-6 deletion territory.

Anti-pattern check: every claim above cites the real gate evidence (Test plan below) or the ledger entry (MIGRATION.md #1948); no claim is based on a hypothetical artifact.

## Test plan — gate evidence (all executed, re-run after the merge-forward where noted)

- [x] cargo: **90 passed** `--release --locked` (23 new detector tests)
- [x] New Python battery: **97 passed** (facade + 3 seam suites — re-run post-merge ✅)
- [x] Flag-ON: **238 passed** (existing detector suites + battery against the real wheel)
- [x] Flag-OFF consumers: **391 passed** (byte-identical default path)
- [x] Coverage: **4× 100.00% line+branch** (facade + 3 touched detector modules)
- [x] `ruff` / `mypy --strict` / `pyright` clean (ruff + mypy re-run post-merge ✅)
- [x] `scripts/ci/validate_ledger_numbering.py` + `scripts/ci/validate_citation_accuracy.py` green (re-run post-merge ✅)
- [x] `tests/unit/test_claude_md_branch_consistency.py` 10 passed (re-run post-merge ✅)
- [x] `scripts/ci/zplus-pr-guard.sh` + `scripts/ci/definition-of-ready-guard.sh` + `scripts/ci/validate_no_hallucination.py` + `scripts/ci/validate_dec_ids.py` — all exit 0 on this body

## Review record

- Hostile review: **2 consecutive clean rounds** — round 1: 2× SHIP, 0 MINOR+, NITs folded (commit `a803cbc`); round 2: 2× SHIP, 0 MINOR+.
- Includes the completeness refuter's **290k+ zero-mismatch differential trials** (round 1) + **60k-trial round-2 confirmation**.
- Merge-forward: origin/main moved `da0a0ae` → `9e71a5a` (club FIX #1613); merged INTO the branch (merge commit `86a59ac`, no rebase), union files (MIGRATION.md ledger weave + CLAUDE.md header/§1/§3) resolved keep-both per house convention; **#1948 re-verified FREE** on the live main tip.

### Current-head update (2026-07-17 — head `57978fc8d`)

- **Merge-forward #2 (the mega-club):** origin/main took the mega-club merge (#1621, main commit `c22087064` — tf-registry + timestamp-IST + preopen-freshness + rust CPCV stats); merged INTO the branch (merge commit `57978fc8d`, no rebase), union files re-resolved keep-both per house convention; **#1948 re-verified FREE** on the live main tip (MIGRATION.md #1948).
- **Stored-outcome gate added (`927c8f4`):** NEW `tests/integration/test_detectors_rust_stored_outcome.py` (stem-matched to `detectors_rust` for the discovery gate) — an OFF-vs-ON STORED-OUTCOME parity gate driving a REAL-writer demotion pull + the raw-integrity sidecar, widening the evidence beyond the seam suites' law-level parity per the coordinator's stored-outcome question (per-item record: MIGRATION.md #1948).
- **Parity-lane saga CLOSED (class B — a CI env delta, never a parity divergence):** the `lake-rust-parity.yml` T24 flag-ON leg was deterministically red because the lane venv never installed the `trading_core` wheel (`import brutex.cli` → `brutex/vocabulary` → `trading_core` = ModuleNotFoundError → "1 failed, 242 passed"), while local dev venvs and ci.yml's test job DO install it — which is why every CI-exact local repro was green. Fixed at root by `3e92cf2` (the lane installs `./rust/trading-core` exactly like ci.yml's test job), with the lane-observability pair `178ee43` (pytest failure tails → the public job summary) + `027f3cb` (failing test ids as `::error` annotations). **The lane is GREEN on head `57978fc8d`** — on the head's checks page exactly two jobs are red repo-page-wide (the two below), neither in the lane's push/pull_request runs — and a CI-exact local rerun of the lane's pytest legs at this head exits 0.
- **Secrets-scan fix (`9639cf1a`, this push):** the CI "Secret scan (gitleaks)" job was deterministically red at the head — the full-history scan (checkout fetches every branch) reaches a sibling branch's committed workflow probe line carrying the deterministic CLAUDE.md §8 hardware-fingerprint key slug (entropy 3.5176; audited false positive — a derived hardware identifier, never a credential). `.gitleaks.toml` gains a rule-scoped `regexTarget = "match"` allowlist anchored on the core-split/RAM/arch slug shape (it cannot blunt the rule for real keys); verified CI-exact at this head: pre-fix 1 leak → post-fix "no leaks found" over 5004 scanned commits.
- **Body-gate resnapshot (this push):** the Z+ / no-hallucination body gates read the event snapshot, so this push carries the house EMPTY resnapshot commit (`chore(ci): resnapshot PR body for the event-snapshot gates`) to synchronize this fixed body into the gates and re-run CI.

## Operator action required (post-merge)

- None — ships INERT (default-OFF flags; a default flip needs its own DEC).

---

*Generated by [Claude Code](https://claude.ai/code/session_01EbPgWhaqtCrjKrJUqTxBFa)*
