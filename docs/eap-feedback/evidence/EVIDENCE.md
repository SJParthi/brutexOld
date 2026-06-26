# EAP Feedback — Evidence Gallery

Screenshots captured while testing the Claude Code Projects early-access beta on the BruteX workload. Each entry pairs a real screenshot with the finding it proves. Images live alongside this file in `docs/eap-feedback/evidence/`.

> Convention: `NN-short-slug.png` + an entry below (date, finding, context). These embed inline into `../CLAUDE_CODE_EAP_FEEDBACK.html`.

> **Live-batch context (2026-06-26).** The findings below were all observed during one real, large multi-agent run: an 11-PR "critical-risks resilience-hardening" batch (download disk-full halt, partition flock, mid-pull ledger checkpoint, Telegram halt alerts, atomic-write consolidation, dashboard O(1) caches, the `/db` cascade pickers) driven concurrently in a single Claude Code Projects session. Several findings are grounded not in screenshots but in directly-observable session/environment state (worktree layout, background-task panel, staged files), which is captured inline per finding so each is reproducible without an image.

| # | Image | Date | Finding | Context |
|---|-------|------|---------|---------|
| 01 | `01-background-tasks-orphaned-agents.png` | 2026-06-26 | Background-tasks panel shows multiple review subagents at **~5h runtime with only 2–4 tool uses each** — almost certainly orphaned/stuck, yet visually indistinguishable from working agents (no heartbeat / "last active" / progress %). | Running section: "Altitude review of PR #887 — 5h 01m, 79.6k tokens, 3 tool uses"; "Reuse review of PR #888 docs — 5h 00m, 89.7k tokens, 4 tool uses"; "Simplification review of PR #888 docs — 5h 00m, 72.2k tokens, 2 tool uses"; several more #888-docs review agents all ~5h. |
| 02 | `02-background-tasks-running-5h.png` | 2026-06-26 | Healthy agents (high tool-use counts) sit in the SAME list as the stuck ones with no signal to tell them apart, and finished/orphaned tasks accumulate unbounded (**"Finished 294"**). | Same panel: "Hostile review of nav-latency caches — 2h 47m, 150.7k tokens, 35 tool uses" and "Diagnose+fix #893 citation scan — 8m, 177.9k tokens, 53 tool uses" (clearly live) shown next to the ~5h / 2–4-tool-use agents; plus small Bash timer tasks and the "Finished 294" counter. |

> **Image status (2026-06-26):** The two screenshots above were shared in chat but could not be located on disk (no recent `*.png`/`*.jpg`/`*.webp` under `/tmp`, `/home/user/bruteX/.claude`, `/root/.claude`, or the session scratchpad). **Drop the two files into this directory as `01-background-tasks-orphaned-agents.png` and `02-background-tasks-running-5h.png`** — the gallery rows and the finding below are written and ready to receive them. Do not fabricate placeholder images.

---

## Findings

### F-1 — Background-tasks panel lacks per-task progress/liveness; orphaned subagents accumulate invisibly

**Date:** 2026-06-26
**Severity:** medium (operability / trust)
**Proof:** `01-background-tasks-orphaned-agents.png`, `02-background-tasks-running-5h.png`

**What the screenshots show.** In the Background-tasks "Running" section, several review subagents have been running ~5 hours each while reporting only 2–4 tool uses and ~70–90k tokens:

- Altitude review of PR #887 — 5h 01m · 79.6k tokens · 3 tool uses
- Reuse review of PR #888 docs — 5h 00m · 89.7k tokens · 4 tool uses
- Simplification review of PR #888 docs — 5h 00m · 72.2k tokens · 2 tool uses
- (plus several more #888-docs review agents, all ~5h)

For contrast, clearly-live agents in the same list show very different shapes: "Hostile review of nav-latency caches — 2h 47m · 150.7k tokens · 35 tool uses" and "Diagnose+fix #893 citation scan — 8m · 177.9k tokens · 53 tool uses". A "Finished 294" counter sits at the bottom.

**The gap.** A 5-hour agent with 2 tool uses is almost certainly orphaned/stuck, but the panel offers **no per-task progress or liveness signal** to confirm that — no heartbeat, no "last active N min ago", no progress %, no auto-reap when the PR work it was reviewing has completed. Stuck agents are visually indistinguishable from genuinely-working ones, and finished/orphaned tasks accumulate unbounded (294 finished). This directly matches the recurring operator concern: *whether tasks are really running, or some agents are stuck/hung.* Review subagents that should have been reaped once their PR work completed are still listed as "running" hours later.

**Suggested fixes (for the beta team).**
- Add a liveness signal per task: "last tool use N min ago" and/or a stalled badge when no tool activity for > some threshold.
- Surface a progress hint (current step / tool-use rate) so a 5h-2-tool-use task is obviously distinguishable from a 35-tool-use one.
- Auto-reap or flag subagents whose parent work (the PR they were reviewing) has finished.
- Let the user collapse/clear the "Finished" pile so it doesn't grow unbounded (294 and counting).

---

### F-2 — Agent file tools cannot access user-uploaded images in a Projects session

**Date:** 2026-06-26
**Severity:** medium (workflow friction; blocks automated evidence capture)

**What happened.** The user shared screenshots in the Claude Code Projects beta and asked the agent to save them and embed them into a deliverable doc. The agent can *see* the images in its context, but they are never written to any filesystem path the agent's Read/Write/Bash/Glob tools can reach (searched `/tmp`, `/home/user`, `/root/.claude`, and all project + scratchpad dirs over the last 180 minutes — no image files present). So the agent structurally cannot programmatically file, rename, or embed screenshots the user shares.

**Why it matters.** A core Projects use-case is "capture what I'm seeing into a report." Today that requires the user to manually re-save each image to a known path — the agent can't do it, breaking an otherwise-automatable loop.

**Suggested fix (for the beta team).** Expose user-uploaded attachments to the session's file tools — e.g. a readable `attachments/` path, or a tool to materialize a pasted image to disk — so agents can save, organize, and embed shared images.

---

### F-3 — Worktree-per-agent isolation leaves orphaned worktrees that share one branch name

**Date:** 2026-06-26
**Severity:** medium (workspace hygiene / confusing state)

**What was observed (no screenshot needed — reproducible from `git worktree list`).** Driving the 11-PR batch with isolated worker agents produced **16 live git worktrees** under `.claude/worktrees/`, of which **13 carry the same `…-jlzt1n` branch suffix** and several point at the *identical* branch name (e.g. three different worktree dirs all on `claude/writer-columnar-4a-jlzt1n`; two each on `claude/dashboard-latency-jlzt1n` and `claude/pull-health-monitor-jlzt1n`). Some worktrees still hold staged-but-uncommitted edits to files unrelated to their nominal task.

**Why it matters.** A new session (or a human) opening the repo cannot tell which worktree is the "live" one for a given branch, which are abandoned, or which carry stray staged changes. The CLAUDE.md worker-protocol warning — *"If you encounter confusing file state … stop and report"* — fires constantly because the on-disk state genuinely is ambiguous. Multiple worktrees on one branch name also invite accidental cross-contamination (two agents editing what they each believe is "the" branch checkout).

**Suggested fixes (for the beta team).**
- Garbage-collect (or clearly mark stale) worktrees whose agent has finished or been reaped — they currently linger indefinitely.
- Guarantee one worktree ↔ one branch name, or surface in the UI which worktree is canonical for a branch.
- Surface per-worktree dirty/staged state so a resuming session knows immediately whether a checkout is clean.

---

### F-4 — Lost work: a sub-agent's uncommitted output can silently disappear between sessions

**Date:** 2026-06-26
**Severity:** high (data loss / trust)

**What was observed.** This very follow-up was launched on the premise that prior agents had left **uncommitted F-3..F-7 findings + a refreshed scoreboard** in the working tree. An exhaustive search — every one of the 16 worktrees, `git stash`, and every `origin/*` branch — found **no trace** of that content: only the committed F-1/F-2 EVIDENCE.md and the 6-finished/3-current scoreboard (already merged to `main`). The earlier agents' additional output was never committed and is gone.

**Why it matters.** Isolated worktrees + reaped agents mean that anything a sub-agent produces but does not *commit and push* can evaporate when its worktree is cleaned or its session ends — with no warning to the orchestrating session, which then plans against output that no longer exists. The honesty rule then forces a downstream agent to either reconstruct the work or report the loss (this finding is the report).

**Suggested fixes (for the beta team).**
- Treat a sub-agent's working tree as durable until explicitly discarded, or auto-commit/auto-stash its diff to a recoverable ref on reap.
- When an orchestrator hands a follow-up "build on the prior agent's uncommitted changes," verify those changes still exist and warn loudly if they don't.
- Make "uncommitted work will be lost on session/worktree teardown" a visible, acknowledged state, not a silent one.

---

### F-5 — No cross-session view of cost/usage accrued by a multi-agent batch

**Date:** 2026-06-26
**Severity:** medium (cost observability)

**What was observed.** The background-tasks panel shows per-task token counts (e.g. 150.7k, 177.9k, 89.7k…), but there is **no rolled-up total** for the batch and no per-session/per-day usage figure. With dozens of review + worker subagents — including the ~5h orphans still "running" — the operator cannot see what an 11-PR batch actually cost, nor whether the orphaned agents are still burning budget while stuck.

**Why it matters.** Heavy multi-agent runs consume the operator's plan usage, and whether EAP usage is metered, exempt, or free is itself unconfirmed. Without an aggregate cost view, an operator can't make an informed "is this batch worth continuing" call, and a stuck-but-billed agent is invisible until the bill arrives.

**Suggested fixes (for the beta team).**
- Show a batch-level / session-level / daily token+cost rollup alongside the per-task counts.
- Flag tasks that are accruing tokens while making no tool-use progress (ties to F-1).
- Document the EAP billing policy (metered vs exempt) so operators can reason about heavy fan-out.

---

### F-6 — Resuming an agent with a stale premise wastes a full turn before the mismatch surfaces

**Date:** 2026-06-26
**Severity:** low–medium (workflow friction)

**What was observed.** A resumed/spawned agent is handed a brief instruction that assumes a specific prior state ("the working tree has uncommitted F-3..F-7"). When that premise is false (see F-4), the agent must spend a full investigative turn — listing worktrees, diffing branches, searching stashes and `origin/*` — just to *discover* the premise is wrong, before it can do the actual work. There is no cheap, authoritative "what is the current state of these files?" handoff.

**Why it matters.** Multi-agent orchestration relies on accurate state handoffs. A stale premise is not caught at dispatch time; it is caught only after the worker burns tokens reconstructing reality. This compounds with F-4 (lost work) and F-5 (cost) — the operator pays for the reconciliation.

**Suggested fixes (for the beta team).**
- Let an orchestrator attach a verified state snapshot (file hashes / `git status`) to a sub-task so the worker can confirm the premise in O(1) instead of re-deriving it.
- Surface to the orchestrator, at dispatch, whether the referenced files/branches actually exist in the target worktree.

---

### F-7 — Branch/worktree naming collisions make "which PR is this?" ambiguous in the UI

**Date:** 2026-06-26
**Severity:** low (clarity)

**What was observed.** Across the batch, the plain-English scoreboard's PR numbers (#885–#897) and the decision-ledger entries (`docs/MIGRATION.md #NNN`) use **two different numbering schemes that overlap in range**, while several background tasks are labelled only by a short title ("Reuse review of PR #888 docs") with no link back to a specific branch/worktree/commit. Combined with F-3's many-worktrees-one-branch state, it is hard to map a running review task → its branch → its eventual PR → its ledger entry.

**Why it matters.** When a reviewer task is stuck (F-1) or its work is lost (F-4), the first question is "which PR/branch/commit was it touching?" — and today that requires manual cross-referencing. Clear, linked identifiers would make triage immediate.

**Suggested fixes (for the beta team).**
- Link each background task to its branch + worktree + (eventual) PR/commit, not just a free-text title.
- Where a project uses an internal ledger numbering distinct from GitHub PR numbers, let tasks carry both identifiers so they don't get conflated.
