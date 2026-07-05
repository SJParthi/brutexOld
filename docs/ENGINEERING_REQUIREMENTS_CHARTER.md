# BruteX — Engineering Requirements Charter (Ultra-Deep, Scope-Tagged)

> **What this is.** The operator's full ultra-deep drill-down engineering
> requirements, captured verbatim in intent and organised so every Brutex
> session can honour them. Nothing is dropped — but every block carries an
> **applicability tag** so the requirements never become an *illusion*
> (Golden Rule #10: no claiming a thing is "handled" when the subsystem does
> not even exist here).
>
> **Operative source of truth is still `CLAUDE.md`.** This charter EXPANDS
> the intent; it never overrides `CLAUDE.md`. If the two ever conflict,
> `CLAUDE.md` wins and this line is the stale copy to fix.
>
> **How to read it.** Each requirement group is tagged:
>
> | Tag | Meaning |
> |---|---|
> | ✅ **APPLIES** | In scope for Brutex today — honour it now. |
> | ♻️ **ALREADY ENFORCED** | Already a `CLAUDE.md` Golden Rule / CI gate — do not re-implement, just keep green. |
> | ⚠️ **APPLIES — HONEST FALLBACK** | In scope, but the absolute form is impossible; use the honest engineering form (e.g. "O(1)-like", not literal "O(1) always"). |
> | 🔮 **FUTURE / NOT-IN-SCOPE** | Describes a LIVE-TRADING product Brutex is **not**. Brutex is BACKTEST-ONLY (no live feed / WebSocket / ticks / orders — DEC-LIVE-REMOVE-001). Do NOT build, validate, or "harden" these here. They belong to a *separate* future product. |

---

## 0. Scope reality (read first — this is the anti-illusion guard)

Brutex is an **extreme brute-force BACKTESTING engine** for Indian **spot
indexes** (NIFTY / BANKNIFTY / SENSEX) + India VIX (reference-only), Track 1.
Concretely:

- **No live feed, no WebSocket, no ticks, no order placement, no portfolio**
  (the entire live surface was removed — DEC-LIVE-REMOVE-001, `CLAUDE.md` §1).
- **Data source:** Groww Trade API historical candles (1-minute only, the rest
  derived), pulled into a **Parquet + DuckDB + LMDB** lake.
- **Surfaces:** the brute-force engine, the ingest/pull pipeline, a FastAPI
  dashboard. **No AWS / Kubernetes / containers / frontend framework /
  message-queue / QuestDB infra exists in this repo.**

So any requirement phrased around **ticks, WebSockets, market feeds,
backpressure queues, AWS/K8s, or a web frontend is 🔮 FUTURE / NOT-IN-SCOPE**
for the backtest engine. It is preserved below for a *separate* live-trading
product, never to be silently pulled into Brutex's scope (Golden Rule #3).

### Master applicability table

| Requirement theme | Tag | Where it lives / why |
|---|---|---|
| 100% line+branch coverage, 7 test types, mutation ≥85% | ♻️ ALREADY | `CLAUDE.md` Rule #12 / STANDARDS_100PCT.md |
| Type-check (mypy strict + pyright), ruff/pylint/bandit | ♻️ ALREADY | Rule #4 / CI |
| Audit JSONL hash-chain on material actions | ♻️ ALREADY | Rule #8 |
| O(1) hot paths (bar/indicator/mask/dedup/write) | ⚠️ HONEST FALLBACK | Rule #5 — "O(1)-like", proven by p99 ratio |
| COMMON · DYNAMIC · SCALABLE · AUTOMATED | ♻️ ALREADY | Rule #11 |
| Hostile review before major change | ♻️ ALREADY | Rule #9 |
| Honest envelope / no hallucination / evidence-driven | ♻️ ALREADY | Rule #1, #10 |
| Merge-readiness charter, drive PRs to merge, resolve conflicts | ♻️ ALREADY | Rule #13 / MERGE_READINESS_GUARANTEE.md |
| Autonomy: scope→implement→test→draft PR→merge→continue | ✅ APPLIES | this charter §3 |
| Telegram CRITICAL alerts on pull halts | ✅ APPLIES | exists (`sweep_alerts.py`) |
| Structured logging (structlog), live dashboard, coverage counters | ✅ APPLIES | `brutex/observability/`, dashboard |
| Failure hardening (disk-full/OOM/quota/quarantine/idempotent rerun) | ✅ APPLIES | pull/lake/engine |
| Retry / backoff / AIMD rate control / governor | ✅ APPLIES | `rate_common.py` |
| Parallel agents/sub-agents for independent work | ✅ APPLIES | this charter §8 |
| Reporting: exec-summary table + finished/current/future PRs | ✅ APPLIES | this charter §9 |
| Secrets management (env-only creds, 0600), dependency scanning | ✅ APPLIES (scoped) | no multi-user auth in Track 1 |
| **WebSocket "never disconnect / zero tick loss / backpressure"** | 🔮 FUTURE | no live feed in Brutex |
| **Real-time market-feed pipeline (tick→dedup→queue→persist)** | 🔮 FUTURE | backtest-only |
| **AWS / Kubernetes / containers / frontend / message queues / QuestDB** | 🔮 FUTURE | not in this repo |
| **Order placement / portfolio / funds / multi-user SaaS** | 🔮 FUTURE | out of scope (Rule #3) |

---

## 1. Core mindset (✅ APPLIES)

Before any implementation, architectural, or operational decision, **actively
hunt for what is missing** — never assume a thing is "already handled":

- missing functionality, requirements, scenarios, edge cases, ideas
- missing auditing, precautions, testing, monitoring, alerting, logging
- missing security, performance issues, validations, failure handling
- missing retry handling, concurrency issues, scalability concerns
- missing observability, runtime protections, operational safeguards

**Deeply verify everything against real evidence** (source code, configs, the
lake, logs, real run data) — not memory, not assumptions.

---

## 2. Coverage & quality bars (♻️ ALREADY ENFORCED — keep green)

These are `CLAUDE.md` Rule #12 / STANDARDS_100PCT.md. Do not restate or
re-implement; enforce and keep green, with **honest** status (Rule #10):

- **Code coverage: 100% line + 100% branch** on `brutex/`.
- **Testing types (7):** unit · integration · property (hypothesis) · golden
  (byte-equality) · bench (O(1) ratio) · stress/soak · mutation (≥85%).
- **Type/lint/security:** `mypy --strict` AND `pyright`; `ruff` + `pylint`
  (warnings-as-errors) + `bandit`.
- **Audit:** JSONL hash-chain on every material action; breaking it fails CI.
- **Decision trail:** every locked decision gets a `DEC-…` ID in `MIGRATION.md`.

> Honest caveat (Rule #10): the decree target is the aspiration; the *actual*
> CI enforcement matrix is in `STANDARDS_100PCT.md` §1 — that doc is
> authoritative for what is 🟢 enforced vs 🟡 partial vs 📋 roadmap. Never
> claim "100% standards compliant" without auditing against it.

---

## 3. Autonomy + merge-to-completion (✅ APPLIES)

Operator directive: *"make it ready for merge, monitor it until merged, resolve
any conflicts/issues and re-merge, then continue with the plan — always."*

Standing behaviour for every work item:

1. **Scope → implement → test → open a DRAFT PR → drive CI to green → merge →
   sync `main` → continue with the next item.**
2. **Monitor every PR until MERGED.** Fix CI failures, resolve merge conflicts,
   re-run gates, re-push. **Never merge over a red or pending gate**
   (`CLAUDE.md` Rule #13).
3. **Escalate ONLY when** a fix is ambiguous, architecturally significant, or
   would expand scope — otherwise keep going and report.
4. **Post-merge:** confirm merge + sync, then validate (tests/run) before the
   next task.
5. **Branch discipline:** develop on the designated feature branch; **never push
   to `main`** without explicit approval; **no force-push** on shared branches.

### Automated merge-ready pipeline (the gates already in CI)

`Change → static + security scan → duplicate detection → type/lint →
tests + 100% coverage → build/import smoke → PR-body gates (Definition-of-Ready,
Z+ markers, no-hallucination citation) → CI-OK aggregate → merge → post-merge
re-validate.`

### Automatic issue handling during merge

| Problem | Action (honest version) |
|---|---|
| Merge conflict | Resolve (deterministically when the branch is a content-superset; else ask) |
| CI/build/test failure | Diagnose + fix + re-push; re-run until green |
| Coverage drop | Detect → restore to 100% before merge |
| Performance regression | Block + investigate (bench gate) |
| Security finding | Block + fix |
| Ambiguous / architectural change | **Escalate to operator** (do not auto-decide) |

> Honest line (Rule #10): "auto-resolve **all** conflicts / auto-fix **all**
> failures" blindly is unsafe. The rule is **drive to merge**, resolving the
> mechanical cases and **escalating the judgement cases** — never a blind
> auto-merge over a red gate.

---

## 4. Performance & O(1) (⚠️ APPLIES — HONEST FALLBACK)

Aim for ultra-low latency, high throughput, minimal blocking/locking/GC, stable
memory. On the hot paths (bar lookup, indicator lookup, combo dedup, mask
eval, result append) aim for:

| Goal | Honest engineering meaning |
|---|---|
| O(1) lookup / dedup / mapping / retrieval | constant-*feeling*, proven by `p99(N=1M)/p99(N=1K) ≤ 1.1` |
| O(1)-like latency | no sudden degradation as N grows |
| Predictable scaling | horizontal where possible; controlled memory growth |
| Minimal contention | efficient concurrency, bounded queues |

**Where true O(1) (time AND space) is mathematically impossible, say so,
explain the constraint, implement the best practical option, and NEVER falsely
claim O(1)** (Rule #5 + Rule #10). The operator's own requirement text endorses
this "O(1)-like" framing.

---

## 5. Reliability & failure hardening (✅ APPLIES — scoped to pull/lake/engine)

Zero **silent** failures, zero **untracked** failures, zero **duplicate
processing**, zero **data inconsistency** — applied to what Brutex actually has:
the **pull pipeline**, the **lake**, and the **engine** (not ticks/WebSockets).

Validate handling for: null / empty / invalid / corrupt / missing / duplicate /
delayed / out-of-order data; schema mismatches; (de)serialization failures;
network / timeout / vendor (Groww 4xx/5xx/429) failures; disk-full (ENOSPC);
OOM; quota exhaustion; auth-token expiry; partial/zero-price candles.

Required behaviours (most already shipped — keep them):

- **Idempotent rerun** — same inputs → same outputs bit-for-bit (Rule #7);
  keep-first union-by-timestamp merge; ledger settle **only after durable write**.
- **Graceful halt** on disk-full/OOM/quota (`LakeWriteHalt`) — no half-written
  partition, no fabricated data.
- **Retry / AIMD / governor** — exponential backoff, 429/timeout ladders,
  500/min hard governor; bounded (no infinite retry / retry storm /
  rate-limit violation); all retries observable.
- **Per-contract / per-symbol quarantine** — one bad item skips, the run
  continues, exits non-zero, names the item.
- **Cross-process safety** — partition flock; atomic tmp→fsync→rename writes.

---

## 6. Observability, monitoring & alerting (✅ APPLIES)

| Component | Brutex form |
|---|---|
| Logging | structured `structlog` (no `print`) |
| Monitoring | live dashboard + O(1) coverage counters |
| Metrics | achieved-rate meter, rate.429/stepdown/stepup events |
| Tracing | per-call latency + pull_id scoping |
| Alerting | **Telegram CRITICAL alerts** on pull halts (disk-full/OOM/quota) — already wired (`sweep_alerts.py`, DEC-TELEGRAM-PULL-HALT-001) |
| Auditing | immutable JSONL hash-chain |
| Replay | audit-log replay test is the idempotence oracle |

**Telegram alert triggers that are IN SCOPE for Brutex:** pull halts (disk-full
/ OOM / quota), retry storms, sustained 429 pressure, a whole-underlying
all-empty flag, CI/merge failures (via the PR watcher).

> 🔮 FUTURE (live-trading only): WebSocket disconnects, tick gaps/delays, queue
> buildup, packet loss, missing-tick / deduplication alerts — **no such feed in
> Brutex**.

---

## 7. Security (✅ APPLIES — scoped)

In scope today: **secrets via AWS Parameter Store only**
(DEC-AWS-ONLY-CREDS-001 — SecureStrings, fail-fast; never argv, never env,
never a local credentials file);
**no secrets in logs/commits/PRs**; **SSRF allowlist** on the instrument-master
host; **TLS verification never disabled**; **dependency + secret scanning**
(bandit / pip-audit / gitleaks / CodeQL / Semgrep / Trivy in CI); **immutable
audit trails**.

> 🔮 FUTURE: multi-user authentication / granular authorization / intrusion
> detection — Track 1 is single-operator, signal-only; no auth surface yet.

---

## 8. Execution: research, parallelism, evidence (✅ APPLIES)

- **Deep research first:** base every decision on the actual code / logs / lake
  / run data — never assumptions or memory.
- **Parallelism:** run independent work (agents/sub-agents, scans, test suites,
  multi-file reads) **in parallel**. For major changes, use a parallel hostile
  review *before* coding (Rule #9).
- **Extreme scenario coverage:** edge / corner / boundary / failure / recovery /
  rare / high-load / high-concurrency / long-duration / resource-exhaustion /
  out-of-box permutations and combinations of inputs, states, orderings,
  dependencies, and concurrency.
- **Evidence discipline (Rule #10):** distinguish **VERIFIED** (proven) vs
  **ASSUMED** vs **RISK** vs **BLOCKER** vs **LIMITATION** vs **UNKNOWN**. Never
  present an assumption as fact; never claim verification without the evidence
  (real logs / tests / benchmarks / run data).

---

## 9. Reporting format (✅ APPLIES)

Every meaningful update gives:

**A) Executive summary table** (technical AND non-technical readable):

| Area | Status | Risk | Coverage | Notes |
|---|---|---|---|---|
| Engine | ✅/⚠️/❌ | Low/Med/High | % | … |
| Pull / lake | ✅/⚠️/❌ | … | % | … |
| Dashboard | ✅/⚠️/❌ | … | % | … |
| Rate control | ✅/⚠️/❌ | … | % | … |

**B) Finished / Current / Future** as a table of PRs, each with a one-line
plain-English value (the "easy comparison even a non-engineer understands"
the operator asks for — see also `docs/PRODUCT_SCOREBOARD.md`).

**C) Priority law** (the tie-breaker for every decision):

> **Correctness > Reliability > Recoverability > Observability > Performance >
> Scalability** (`CLAUDE.md` Rule #13).

> Note: the operator's source text listed this law twice with two different
> orderings; `CLAUDE.md` Rule #13 (Correctness-first, above) is authoritative.

---

## 10. Final expectation (the master goal, honestly framed)

Brutex should always be: **ultra-fast, evidence-driven, self-healing where it
can be, fully automated after one-time bootstrap, fully observable, fully
auditable, deeply tested, recoverable, and production-grade** — with
**near-real-time** processing on the backtest/pull paths and **honest limits**
stated wherever an absolute ("zero loss", "O(1) always", "never disconnect")
is not physically guaranteed.

The 🔮 FUTURE live-trading guarantees (zero tick loss, WebSocket resilience,
AWS/K8s deployment) apply to a **separate** product and must NOT be claimed or
half-built inside the backtest engine.

> **Anti-illusion rule (Rule #10), restated:** a requirement that names a
> subsystem Brutex does not have is not "satisfied" and not "in progress" — it
> is **out of scope here**. Say that plainly rather than fabricate coverage.
