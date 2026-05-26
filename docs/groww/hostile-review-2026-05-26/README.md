# 4-Agent Hostile Review — DEC-VENDOR-PIVOT-001

**Date:** 2026-05-26
**Subject:** Dhan → Groww full pivot for BruteX Track 1 backtesting data source
**Reviewers:** 4 parallel agents per CLAUDE.md §9 (Plan + Explore + Red-team + Compliance)
**Operator verdict:** Override → proceed with Option C (full pivot) despite hostile-review NO-GO
**Authority:** DEC-P0-005 operator-override precedent (MIGRATION.md entry #143)

---

## Agent verdicts at a glance

| Agent | Role | Verdict on Option C (full pivot) | Verdict on Option D (multi-source) |
|---|---|---|---|
| #1 Plan | Phased migration sequencing | ❌ Irreversible at squash-delete | ✅ ~12 sub-PRs, Phase 0 first |
| #2 Explore | Hidden-coupling audit | ❌ Coupling 8/10, naive swap = production break | ✅ Abstraction at adapter boundary survives |
| #3 Red-team | Failure modes / showstoppers | ❌ **4 P0 blockers — DO NOT PROCEED** | 🟡 Same blockers, reversibly gated |
| #4 Compliance | CLAUDE.md scorer | ❌ NO-GO on Option C | 🎯 GO-IF-X (5 conditions) |

**Convergent verdict: 4/4 agents endorse Option D over Option C.**
**Operator chose Option C anyway and explicitly accepted the 4 P0 risks.**

---

## P0 blockers (explicitly accepted by operator per ADR-0001 §3.1)

1. INDIA VIX presence on Groww — UNVERIFIED (~55-65% prob missing). Mitigation: Phase 0b empirical probe is the hard gate.
2. Per-day backtesting cap — UNDOCUMENTED. Mitigation: adaptive rate-limiter [4..8] r/s.
3. Data quality drift Dhan ↔ Groww — NEAR-CERTAIN (broker-specific 1m OHLC aggregation). Mitigation: Phase 0b 1-day NIFTY 1m bar-by-bar diff; >5% drift triggers follow-up ADR.
4. Reproducibility hash reset — CERTAIN. Mitigation: git tag `v-pre-groww-pivot` precedes deletion; pre-pivot audit chain archived.

---

## Transcript files (archived separately)

Each agent returned a structured report. Full transcripts are stored in the parent agent-output filesystem (`/tmp/claude-*/tasks/*.output`) and were quoted in the relevant PR body and MIGRATION.md entry #246. This README is a permanent in-repo pointer.

| Agent ID | Role | Key finding |
|---|---|---|
| Agent #1 (Plan) | Phased migration plan | The `MarketAdapter` protocol at `brutex/adapters/protocol.py` already exists (360 LoC, runtime-checkable) — designed exactly for this scenario per its own docstring; Option D is filling a slot, Option C is replacing a conforming implementation |
| Agent #2 (Explore) | Hidden-coupling audit | **NEW P0 surfaced: paisa-vs-rupee 100× scale mismatch** (Dhan returns paisa, Groww returns rupees per their docs "All prices in rupees"). Plus 10 hidden failure modes if naive find-replace `dhan→groww` is attempted. Coupling score 8/10 (TANGLED) |
| Agent #3 (Red-team) | Showstopper hunt | 4 P0 blockers + 8 P1/P2 risks. Verdict: "DO NOT PROCEED" |
| Agent #4 (Compliance) | CLAUDE.md scorer | Option D wins 5/12 Golden Rules + 3/5 §11 pillars + STANDARDS row impact additive. Option C: NO-GO. Option D: GO-IF-X (5 conditions) |

---

## Why the operator override is legitimate per BruteX governance

Per CLAUDE.md §2 rule 9 (hostile review): "Major design changes get a parallel-agent hostile review BEFORE implementation." The rule mandates the **process**, not the **outcome**. The operator retains decision authority per the **authority hierarchy** in CLAUDE.md §0 (which lists CLAUDE.md first, then defaults — operator-issued decrees in chat are absorbed via MIGRATION.md as locked decisions).

Per DEC-P0-005 (entry #143): the operator-override mechanism (`OPERATOR_OVERRIDE=true` + `OPERATOR_OVERRIDE_REASON='<why>'`) was explicitly built for situations where the operator chooses to proceed against a gate's recommendation. DEC-P0-008 (entry #146) added a rate-limit + escalation token; this is the FIRST use of the mechanism for a strategic vendor decision (prior uses were CI-mechanical).

Compensation for accepted risk: ADR-0001 §3.1 enumerates EVERY P0 risk + its specific mitigation. Phase 0b is the hard gate. Z+ defensive layers (DETECT/VERIFY/RECONCILE/PREVENT/AUDIT/RECOVER/COOLDOWN) per `docs/TRACK1_GROWW_ADAPTER.md §7` are mandatory.
