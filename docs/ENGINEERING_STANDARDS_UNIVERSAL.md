# Universal Engineering Execution, Validation, Merge, Hardening & Automation Standards

> **Operator charter (Parthi, 2026-06-26).** A standing operating contract for
> every implementation, architectural, and operational decision in this
> workspace — not a one-off checklist. It sits alongside the 12 Golden Rules in
> `CLAUDE.md` (authority order: `CLAUDE.md` first, then this charter, then
> defaults). Where this charter and a Golden Rule overlap, both apply; neither
> relaxes the other.

---

## BruteX scoping note (what applies vs N/A)

BruteX is a **single-process research engine + dashboard** (Python 3.12 + Rust
hot kernels; FastAPI UI; Parquet + DuckDB lake). Apply this charter to the areas
that actually exist; do not fabricate concerns for areas that do not.

| Area | Status in BruteX |
|------|------------------|
| Frontend (the FastAPI dashboard UI) | **APPLIES** |
| Backend (engine, adapters, pull pipeline, orchestrator) | **APPLIES** |
| DB / data lake (Parquet + DuckDB) | **APPLIES** |
| APIs (Groww adapter + dashboard routes) | **APPLIES** |
| Security (SSRF allowlist, secrets/creds, injection/XSS) | **APPLIES** |
| Performance (O(1) hot paths, p99 ratios, latency) | **APPLIES** |
| Monitoring / Observability (audit JSONL, structlog, Telegram, dashboards) | **APPLIES** |
| Kubernetes / container orchestration | **N/A — do not fabricate** |
| Message queues / event brokers | **N/A — do not fabricate** |
| Multi-service / distributed deploy | **N/A — do not fabricate** |

Per Golden Rule #10 (honest limits): never invent K8s / MQ / microservice
analysis to look thorough. State "N/A for a single-process engine" instead.

---

## 1. Execution standard — evidence before action

- Every implementation, architectural, or operational decision is **PRECEDED by
  deep, workspace-wide, evidence-based research using all available agents**
  (parallel hostile-review + workspace-health + explore + worker). The research
  precedes the code, not the reverse.
- **Never assume, never guess, never hallucinate** (Golden Rule #1). Every
  factual claim about Groww / NSE / the lake / the engine is traceable to
  `file:line`, a log, or a real run. If it is not verified, it is labelled
  Assumed / Unknown — never asserted as fact.
- A momentary in-session impression is **never** a finding or a decision basis.
  Cite the evidence (this is the `eap-verify-before-claim` rule generalised).

## 2. Validation standard — per PR

- **Pre-merge review.** Every PR / sub-PR / diff gets a parallel adversarial
  review before merge — correctness, security (XSS / injection / secrets),
  O-complexity honesty, edge cases, and coverage honesty (no dishonest
  `# pragma: no cover`).
- **Continuous monitoring.** Watch CI and runtime health through the change, not
  only at the moment of merge.
- **Post-merge validation.** After landing, confirm the merged change behaves on
  the real workspace — a green CI is necessary, not sufficient.

## 3. Merge standard

- **Never merge over a red or pending gate** (Charter §3, `CLAUDE.md` Rule 13).
- Honesty per Golden Rule #10: state what is green, what is partial, what is out
  of scope. Never claim "100% / fully / guaranteed" for a 🟡 / 📋 / 🚫 row.

## 4. Performance / complexity standard — O(1) where real

- Target **O(1) per hot path / per interaction** (bar lookup, mask eval, dedup,
  result append; dashboard interactions).
- Where O(1) is **genuinely impossible**, document the honest trade-off — the
  real complexity envelope (e.g. "O(page), not a literal O(1) total"; "O(1) on a
  repeat poll, the first poll after a pull pays one real scan"). **Never fake an
  O(1) claim.**

## 5. Hardening standard — extreme coverage

- **Extreme scenario + permutation + failure-mode coverage** for every change.
- **Resilience primitives:** retry / backoff / circuit-breaker on every external
  boundary (vendor 429/5xx/timeout ladders; disk-full / OOM graceful halt;
  per-symbol quarantine; cross-process lake flocks).
- **Full testing surface:** functional + non-functional + security + failure.
  100% line+branch on touched code; a regression test for every bug fix; the
  7 test types (unit + integration + property + golden + bench + stress +
  mutation) per `CLAUDE.md` Rule 12.

## 6. Automation standard — automation-first

- Prefer automated gates, self-healing, and alerting over manual steps (§11
  AUTOMATED — "zero clicks per day"): Release Please + auto-merge + watchdog +
  Telegram + auto-resume + auto-re-mint of the daily token.

## 7. Parallelism standard — parallel where safe

- Fan out independent work across agents/workers; serialize only true
  dependencies. Respect cross-process / cross-partition safety (advisory flocks
  on shared lake partitions; single-flight ledger flushes; atomic writes).

## 8. Evidence labels (use these explicitly, every claim)

Every claim in a report, PR body, or decision carries exactly one label:

| Label | Meaning |
|-------|---------|
| **Verified** | Confirmed from `file:line` / a real run / a log. |
| **Assumed** | A working assumption, stated as such, not proven. |
| **Risk** | A known way this could go wrong. |
| **Blocker** | Must be resolved before proceeding/merging. |
| **Limitation** | A real boundary of the change (honest scope). |
| **Recommendation** | A proposed follow-up, not done here. |
| **Unknown** | Genuinely unverified; a probe/gate is required. |

Any non-trivial deliverable carries an **Executive-Summary status table** for
stakeholders (per-area status, with the labels above).

---

## Relationship to existing rules

This charter is the cross-cutting generalisation of patterns already enforced
piecemeal in `CLAUDE.md`: Golden Rule #1 (no hallucination), #5 (O(1)
discipline), #9 (hostile review before code), #10 (honest limits), #11
(COMMON · DYNAMIC · SCALABLE · AUTOMATED), #12 (100% standards), #13 (merge
charter). It does not supersede them; it states the standing expectation that
every decision in this workspace satisfies all of them, with evidence.
