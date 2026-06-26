# Claude Code Early Access Program — Consolidated Feedback Dossier

> Master thread → **claude-code-early-access@anthropic.com**. Projects: **BruteX** + **TickVault**.
> **Status:** TickVault half populated from real repo usage (11 findings). **BruteX round-1 (20 findings) PENDING paste** — merges on receipt; matching findings get promoted to UNIVERSAL.
> **Severity:** S1 = blocks / serious · S2 = notable friction · S3 = minor.
> **Evidence kind:** OBSERVED (reproduced this/last session) · ARTIFACT (a workaround the repo built proves the pain was real) · INFERENCE (reasoned, flagged).
> **Tag:** UNIVERSAL (confirmed in both projects) · LIKELY-UNIVERSAL (mechanism is general; confirm vs BruteX) · PROJECT-SPECIFIC.
> Last updated: 2026-06-26 · Round: 2 (TickVault folded in; BruteX pending)

---

## 0. The 30-second human view (anyone can read this)

| # | What we noticed | Like… | 👍 / ⚠️ | What it costs you | Universal? |
|---|---|---|---|---|---|
| 1 | The agent can't "ship" code until it passes a wall of safety checks | A chef who can't serve a dish until the food inspector signs off | 👍 | Mistakes get caught before they land | pattern |
| 2 | Every session it re-reads ~40 rulebooks, relevant or not | Hauling all 40 cookbooks to make one cup of tea | ⚠️ | Wastes its memory/attention on every single turn | yes |
| 3 | Big answers can cut off mid-sentence on a network blip | A phone call dropping right as you say the key word | ⚠️ | Lost work, you have to ask again | yes |
| 4 | It auto-runs a health check the moment a session opens | The shop boy who checks the till + stock before you arrive | 👍 | Always briefed, zero clicks | pattern |
| 5 | A helper "didn't have" a tool, so it passed the job down 3 helpers deep before one did it | Four people each saying "not my job, ask him" | ⚠️ | ~1.2M tokens burned going in circles | yes |
| 6 | A helper's tool list didn't match what it could actually use | A toolbox labelled "hammer inside" that's empty | ⚠️ | Found out only by failing mid-task | yes |
| 7 | Sent 7 helpers at once, server got busy, ALL failed together — and it quietly returned a half-empty result | 7 runners jam one doorway at rush hour; you're handed a blank form, unaware | ⚠️ | Silent degraded output, not flagged | yes |
| 8 | The PR robot isn't always told when tests pass or a conflict appears | Waiting for a delivery with no tracking pings | ⚠️ | You babysit / it re-checks manually | yes |
| 9 | In this web view, anything said outside the "reply" button is invisible to you | Talking into a mic that's switched off | ⚠️ | Right answer, never heard | this setup |
| 10 | Its memory needs a hand-written index that can go stale | A filing cabinet whose contents-page is updated by hand | ⚠️ | Minor drift over time | yes |
| 11 | "Never skip a safety check" is great — but a flaky check can stall the start | A smoke alarm that false-beeps and won't let you cook | ⚠️ | Minor start delays | this setup |

**The one-line takeaway:** the *brain* is strong (great fit as a gated, self-briefing autonomous builder); the *plumbing under load* is where it bleeds — context overload, dropped big replies, and unreliable sub-helper tooling.

---

## 1. Summary

Claude Code is a strong fit as the autonomous "builder" on TickVault — its hard pre-commit/pre-push gates plus an auto-firing session briefing keep long unattended runs honest and on-track. The rough edges are all plumbing-under-load: blanket rule auto-loading floods context every turn, large streamed replies time out, and sub-agent tooling is unreliable enough that one mis-judged tool became a 3-deep delegation loop and a rate-limited fan-out silently degraded its own output.

---

## 2. At a glance

| Topic | Verdict | What works | What needs work | Projects |
|---|---|---|---|---|
| use-case fit | 👍 strong | Agent-as-builder under hard, fail-closed gates is a natural fit | Nothing major surfaced this round | TickVault (BruteX pending) |
| reliability (autonomy-to-completion) | ⚠️ needs work | PR auto-merge + activity-subscribe drive PRs forward | Sub-agent delegation loop; tool-manifest mismatch; rate-limited fan-out collapses silently; PR webhook gaps | TickVault (BruteX pending) |
| memory | ⚖️ mixed | Two-tier private/team memory + index concept is sound | ~40 rule files auto-load every turn regardless of relevance; index needs hand-maintenance | TickVault (BruteX pending) |
| proactivity | 👍 strong | SessionStart hooks auto-brief health/plans/PRs, zero clicks | "Never bypass" + a flaky check can stall the start | TickVault (BruteX pending) |
| session-management UX | ⚠️ needs work | File-first discipline mitigates lost output | Large streamed replies time out; reply-tool split hides plain text; tools discovered by failing | TickVault (BruteX pending) |

---

## 3. Per-topic cards

### use-case fit — 👍 strong
**What works:** the architect/builder split + fail-closed hooks make the agent a reliable executor that can't quietly cut corners.
**What needs work:** —

- **TV-UCF-01 — Agent-as-builder under mechanical gates fits well** (WIN, ARTIFACT, PROJECT-SPECIFIC) — CLAUDE.md "Parthiban = architect, Claude Code = builder" + the `.claude/hooks/` pre-commit/pre-push/git-hook stack + `design-first-wall.md`'s `plan-gate.sh` keep the agent honest. Intended use-case working as designed.

### reliability (autonomy-to-completion) — ⚠️ needs work
**What works:** PR auto-merge + `subscribe_pr_activity` event loop genuinely drive PRs toward green without polling.
**What needs work:** sub-agent tooling reliability and load-handling.

- **TV-REL-01 — Sub-agent re-delegates instead of executing → 3-deep loop** (S2, OBSERVED, LIKELY-UNIVERSAL) — a `worker` tasked to write a file kept trying the (unavailable) Write tool and handed the job to a child worker that did the same; ~1.2M tokens across 4 agents before one fell back to Bash and succeeded.
- **TV-REL-02 — Worker tool manifest mismatch (advertised ≠ available)** (S2, OBSERVED, LIKELY-UNIVERSAL) — the worker tool list advertises `Write`/`Edit`, but at runtime only `Bash` was usable; the gap is discovered by a failed call and is the root cause of TV-REL-01.
- **TV-REL-03 — Rate-limited fan-out collapses together and degrades silently** (S2, OBSERVED, LIKELY-UNIVERSAL) — a 7-agent workflow (5 miners + synth + write) all failed at once on server-side "temporarily limiting requests"; the pipeline still returned a 3-finding skeleton with "(summary unavailable)" instead of retrying/backing off.
- **TV-REL-04 — PR webhook coverage gaps force hand-rolled polling** (S2, ARTIFACT, LIKELY-UNIVERSAL) — CI-success / new-push / merge-conflict transitions aren't delivered to a subscribed session; `pr-completion-protocol.md` codifies a manual self-check loop to compensate.

### memory — ⚖️ mixed
**What works:** the private + team memory split with a loaded index is a sound design.
**What needs work:** loading is by directory, not relevance.

- **TV-MEM-01 — Blanket `.claude/rules/` auto-load floods context every turn** (S2, OBSERVED, LIKELY-UNIVERSAL) — ~40 rule files (operator-charter, every wave-N-error-codes, all Dhan-API rules) inject tens of thousands of tokens each turn regardless of the task; CLAUDE.md itself carries a "TOKEN EFFICIENCY" section worrying about exactly this. Each rule file already declares a "Trigger" glob — honored for documentation, not for loading.
- **TV-MEM-02 — Hand-maintained memory index drifts** (S3, ARTIFACT, LIKELY-UNIVERSAL) — correctness depends on the agent remembering to add a `MEMORY.md` pointer after each memory write; index and files can silently diverge (the spec itself warns "verify it still exists").

### proactivity — 👍 strong
**What works:** the session opens already briefed — health, active plans, open PRs — with zero operator action.
**What needs work:** a noisy gate can masquerade as a hard stop.

- **TV-PRO-01 — SessionStart hooks auto-brief health + context** (WIN, ARTIFACT, PROJECT-SPECIFIC) — `.claude/settings.json` SessionStart + operator-charter §A run doctor/summary/context-brief automatically. Proactivity done well; worth a first-class product slot.
- **TV-PRO-02 — "Fix, never bypass" + a flaky check can stall the start** (S3, ARTIFACT, PROJECT-SPECIFIC) — the (correct) zero-bypass stance means a transient startup-hook failure blocks useful work until diagnosed; advisory checks and hard gates aren't distinguished.

### session-management UX — ⚠️ needs work
**What works:** the file-first discipline keeps bulk content safe when a stream dies.
**What needs work:** transport fragility + invisible-reply trap.

- **TV-UX-01 — Large streamed replies hit "stream idle timeout — partial response received"** (S2, ARTIFACT, LIKELY-UNIVERSAL) — `stream-resilience.md` exists *only* because this recurred; it mandates ≤2K-token chunks + writing bulk to files. A transport-layer fragility, worked around, not fixed.
- **TV-UX-02 — Replies must route through a specific tool; plain text is invisible** (S2, OBSERVED, PROJECT-SPECIFIC) — in this web harness, assistant text not wrapped in the reply tool never reaches the user; the repeated emphatic reminder is itself evidence it trips runs.
- **TV-UX-03 — Enabled-tool set not surfaced; discovered by a failed call** (S3, OBSERVED, LIKELY-UNIVERSAL) — the main loop learned `Write` was disabled only by calling it and failing; no up-front manifest, costing a round-trip mid-plan.

---

## 4. Full findings (TickVault, this round)

| ID | Title | Topic | Sev | Tag | Evidence | Suggested fix |
|---|---|---|---|---|---|---|
| TV-UCF-01 | Agent-as-builder under mechanical gates fits well | use-case fit | WIN | PROJECT-SPECIFIC | ARTIFACT: CLAUDE.md builder split; .claude/hooks/*; design-first-wall.md plan-gate.sh | Keep; surface which gate would block *before* the commit attempt |
| TV-REL-01 | Sub-agent re-delegates instead of executing → 3-deep loop | reliability | S2 | LIKELY-UNIVERSAL | OBSERVED: task-ids aef32048…, a13fc93d…, adb3d844…, ac269837…; ~1.2M tokens; only the Bash-fallback child completed | Detect the tool is held and execute; cap self-similar delegation depth; report "no leaf completed" not per-layer "I dispatched a worker" |
| TV-REL-02 | Worker tool manifest mismatch (advertised ≠ available) | reliability | S2 | LIKELY-UNIVERSAL | OBSERVED: worker manifest lists Write/Edit; runtime only Bash usable (ac269837 wrote via Bash heredoc) | Make the advertised tool set match the runtime set per context |
| TV-REL-03 | Rate-limited fan-out collapses together, degrades silently | reliability | S2 | LIKELY-UNIVERSAL | OBSERVED: workflow wt25q1uj0 — all 5 miners + synth + write failed "temporarily limiting requests"; returned 3-finding skeleton, "(summary unavailable)" | Backoff + retry 429 inside fan-out; don't null an entire batch from one window; flag degraded output |
| TV-REL-04 | PR webhook coverage gaps force hand-rolled polling | reliability | S2 | LIKELY-UNIVERSAL | ARTIFACT: harness note "CI success, new pushes, merge-conflict transitions never delivered"; pr-completion-protocol.md self-check loop | Deliver CI-success/mergeability/push events, or a "wait for PR state X" primitive |
| TV-MEM-01 | Blanket .claude/rules/ auto-load floods context every turn | memory | S2 | LIKELY-UNIVERSAL | OBSERVED: ~40 rule files injected this turn; CLAUDE.md "TOKEN EFFICIENCY" section | Relevance/trigger-based loading (rule files already declare Trigger globs); per-session rule budget with on-demand expand |
| TV-MEM-02 | Hand-maintained memory index drifts | memory | S3 | LIKELY-UNIVERSAL | ARTIFACT: memory spec requires manual MEMORY.md pointer per write | Auto-derive the index from file frontmatter (`description:`) |
| TV-PRO-01 | SessionStart hooks auto-brief health + context | proactivity | WIN | PROJECT-SPECIFIC | ARTIFACT: .claude/settings.json SessionStart; operator-charter §A | First-class built-in "briefing" slot |
| TV-PRO-02 | "Fix, never bypass" + flaky check can stall the start | proactivity | S3 | PROJECT-SPECIFIC | ARTIFACT: charter "do NOT bypass" + proactive startup hooks | Distinguish advisory-check (warn, continue) from gate (block) |
| TV-UX-01 | Large streamed replies hit stream idle timeout | session-management UX | S2 | LIKELY-UNIVERSAL | ARTIFACT: stream-resilience.md built solely to work around recurring "stream idle timeout — partial response received" | Transport keepalive + resumable streaming |
| TV-UX-02 | Reply must route through a tool; plain text invisible | session-management UX | S2 | PROJECT-SPECIFIC | OBSERVED: web-harness reminder "IF YOU HAVE NOT called mcp__webagent__reply YOU HAVE NOT COMMUNICATED" | Auto-promote final turn text to a reply when none was sent, or loudly flag the omission |
| TV-UX-03 | Enabled-tool set discovered by a failed call | session-management UX | S3 | LIKELY-UNIVERSAL | OBSERVED: main loop got "Write … is not enabled in this context" | Expose/queryable enabled-tool manifest at session start |

---

## 5. UNIVERSAL / LIKELY-UNIVERSAL call-outs (product-level signal — most valuable)

These are the rows that are *not* TickVault quirks — they're how Claude Code behaves for anyone. Strongest candidates to fix at the product level:

1. **Sub-agent re-delegation loop + tool-manifest mismatch (TV-REL-01/02)** — advertised tools that aren't actually available make sub-agents loop instead of fall back; ~1.2M tokens wasted here.
2. **Rate-limited fan-out collapses silently (TV-REL-03)** — one busy-server window nulled a 7-agent run and still returned a degraded artifact with no "this is incomplete" signal.
3. **Context flooded by blanket rule auto-loading (TV-MEM-01)** — load by relevance/trigger, not by directory.
4. **Large streamed replies time out (TV-UX-01)** — needs transport keepalive / resumable streaming; the file-first workaround shouldn't be load-bearing.
5. **PR webhook coverage gaps (TV-REL-04)** — deliver CI-success/mergeability events or a wait-for-state primitive.

*(Confirm each against BruteX on paste; matches get promoted from LIKELY-UNIVERSAL → UNIVERSAL.)*

---

## Appendix A — BruteX round-1 (PENDING PASTE)

Awaiting the BruteX round-1 dossier (20 findings, same 5 topics). On receipt: fold its findings into sections 0–5, recompute the at-a-glance "Projects" column, and promote any TickVault finding whose mechanism also appears in BruteX from LIKELY-UNIVERSAL → UNIVERSAL.
