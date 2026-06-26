# EAP Feedback — Evidence Gallery

Screenshots captured while testing the Claude Code Projects early-access beta on the BruteX workload. Each entry pairs a real screenshot with the finding it proves. Images live alongside this file in `docs/eap-feedback/evidence/`.

> Convention: `NN-short-slug.png` + an entry below (date, finding, context). These embed inline into `../CLAUDE_CODE_EAP_FEEDBACK.html`.

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
