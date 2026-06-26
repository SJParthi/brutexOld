# Claude Code EAP feedback — BruteX

This directory holds **meta-feedback about Claude Code** (the Anthropic
coding agent / "Claude Code Projects" Early Access Program), gathered from a
week of building the BruteX backtesting engine almost entirely through Claude
Code. **It is NOT BruteX product code, spec, or engine documentation** — it
describes the *tool* used to build BruteX, not BruteX itself.

It is committed into the repo on its own isolated branch so a separate
**"Combined" Claude Code project** — which has both the BruteX repo and the
TickVault repo checked out — can read each repo's equivalent feedback and
consolidate the two into one EAP submission.

## What's here

| File | What it is |
|------|------------|
| `CLAUDE_CODE_EAP_FEEDBACK.html` | The self-contained feedback dossier (a shareable one-page HTML): a 5-topic at-a-glance table, one card per topic, and the itemized findings + positives + feature requests. |
| `EAP_FEEDBACK_EMAIL.md` | The ready-to-send cover email (subject + body + send checklist) that the HTML attaches to. |

## The 5-topic structure

The feedback is organized by the Claude Code team's five EAP-round topics, so
the TickVault equivalent can be merged topic-by-topic:

1. **Use-case fit** — is a long-lived, ledger-driven, multi-agent repo the
   right place to reach for a Project?
2. **Reliability** — do sessions finish autonomously, and do they refuse bad
   work?
3. **Memory** — does cross-session memory let the operator skip restating
   rules / scope / ceilings?
4. **Proactivity** — does it run work and set up routines on its own, and is
   the cost/progress observable?
5. **Session-management UX** — which session needs attention, and is per-session
   base-state surfaced?

## For the Combined consolidation

When merging with TickVault's equivalent:

- Both repos use the **same 5-topic structure** — align findings under each
  topic before de-duplicating.
- Findings carry stable IDs (`UCF-/REL-/MEM-/PRO-/UX-`, `WIN-`, `FR-`) and a
  severity (S1–S4); keep the IDs so cross-references survive the merge.
- Every finding is **real and reproducible** (Golden Rule #10 honesty) — do not
  pad or inflate counts during consolidation.

## Confidentiality

These materials are Confidential per the Trusted Tester Agreement. The feedback
channel is `claude-code-early-access@anthropic.com`.
