# Claude Code Projects (beta) — Round-2 Feedback

**For:** Claude Code Early Access team — Diana Liu (claude-code-early-access@anthropic.com); Omid (eng lead), Jul 1 call
**From:** Parthiban (sjparthi93@gmail.com) · **Date:** 2026-06-27
**Workload under test:** BruteX — a large multi-agent backtesting-engine repo, run as a real overnight batch inside the Projects beta.

This is round-2 feedback on the **Claude Code Projects beta**, gathered from running a real, large, multi-agent batch inside it. The Projects/auth issues come first; a few smaller workflow and security items follow. Where I observed something but could not reproduce it reliably, I say so plainly in the issue's description.

---

## Issues at a glance

| # | Issue |
|---|-------|
| 1 | GitHub access drops mid-session, with no way to recover it from inside the session — you have to start a new one. |
| 2 | That auth drop forced me into creating a whole new Project for the same repo just to get a working session. |
| 3 | Connecting the GitHub App doesn't reach an already-running session; only a fresh session picks it up. |
| 4 | A Project isn't a self-running container — overnight work still needs a separate Routine to keep going. |
| 5 | The 403 error blames an "org admin" on a personal account that has no org, and isn't a real GitHub error. |
| 6 | Long-running review agents sit idle for hours with no liveness signal and no cleanup. |
| 7 | I observed overnight automation getting harder under Projects than it had been on plain sessions (couldn't fully isolate the cause). |
| 8 | The effort tier seemed to drop back down after a long idle (observed, not reliably reproduced). |
| 9 | Disk steadily accumulates across a long multi-agent run with no auto-cleanup. |
| 10 | A failed CI check can't be re-run — the only workaround is an empty re-trigger change. |
| 11 | Screenshots pasted into chat aren't readable by the file tools; only @-attached uploads are. |
| 12 | One shared append-only file forces parallel-branch merges to happen one at a time. |
| 13 | A typosquat `httpx2` wheel is installable from the managed environment's package index (security). |

---

## What we'd like

- **Project auto-resume** — a first-class "keep working on goal X" so a Project keeps work going without hand-wiring a schedule.
- **In-session credential refresh** — re-bind GitHub, or pick up a newly-connected App, without tearing down the session.
- **A Project status surface** — one place showing what's persistent (history, repo), what's live (this session, bound credentials, environment), and what's stale.
- **Attach Routines to a Project** — Routines listed and owned by the Project, firing into its sessions.

---

## Details

### 1 — GitHub access drops mid-session, with no in-session recovery
A long-running session was opening and merging pull requests normally for hours. Then every GitHub REST call started returning **403**, all at once. `git push` kept working, but opening and merging PRs through the API died — the session was stranded push-only mid-run. I retried in a fresh sub-agent and got the same 403, so this was a stable session-level state, not a momentary blip. There was no way to recover access from inside the running session.

**Why it matters:** an unattended overnight run can't survive a credential lapse it can't recover from — it just stalls.

**Suggested fix:** allow in-session re-auth, or auto-checkpoint and resume in a fresh session so the loop survives the lapse.

### 2 — The auth drop forced a whole new Project for the same repo
Because nothing recovered the running session (I retried several times), the only clean path back to a working session was to spin up a **second Project pointing at the same repo**. That split my context across two Projects, duplicated the coordinating session, and left it ambiguous which Project was canonical.

**Why it matters:** forking a second Project just to recover credentials is a heavy, confusing workaround for what should be a refresh.

**Suggested fix:** in-session or in-Project credential refresh, so recovering auth doesn't mean forking a second Project.

### 3 — Connecting the GitHub App doesn't reach a running session
After installing the GitHub App on my account (**@SJParthi**, All repositories, read/write), the already-running session never saw the new access. Only a brand-new session picked it up. From inside the live session, connecting the App appeared to do nothing.

**Why it matters:** the natural recovery step — connect/repair the App — has no effect on the session that actually needs it.

**Suggested fix:** propagate a credential change into the running session, or prompt *"new permissions detected — restart the session to apply."*

### 4 — A Project isn't a self-running container
"Project" suggested a self-running, always-on container. In practice the work runs as **sessions**, and keeping work going overnight still needs a separately-wired **Routine/trigger** — there's no ambient "the Project is working on it" loop.

**Why it matters:** the mental model the name sets up (a Project that just keeps working) doesn't match how continuity actually has to be arranged.

**Suggested fix:** Project auto-resume, and/or set the expectation in-product (work runs as sessions; attach a Routine to make it run on its own).

### 5 — The 403 message is misleading
The 403 body says *"an org admin must connect the Claude GitHub App for this organization"* — but this is a **personal account** with no organization and no org admin, and the App was demonstrably connected (it had been opening and merging PRs minutes earlier). The response also doesn't look like a real GitHub error: its `documentation_url` points at `docs.anthropic.com`, the status is 403 (GitHub returns 401 for bad credentials), and it carries none of GitHub's signature headers (`x-github-request-id`, `x-ratelimit-*`). The exact body:

```
HTTP 403
{"message":"GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.","documentation_url":".../claude-code/github-actions"}
```

**Why it matters:** the message sends a personal-account user chasing an "org admin" that doesn't exist, instead of telling them their session's access lapsed.

**Suggested fix:** detect the personal-account, already-connected case and say what's true (*"this session's GitHub access was not enabled; start a new session to recover"*), and distinguish an access-not-enabled state from a real credential error.

### 6 — Long-running review agents sit idle for hours with no liveness or cleanup
The background-tasks panel showed multiple review agents marked "running" for **5+ hours** while having made as few as **1 tool action** the entire time — for example, one review at **5 h 01 m with 1 tool use**, another at **5 h 01 m with 3 tool uses**, and several more around **5 h with 2 tool uses**. They never auto-terminated and just accumulated: the panel showed a large "finished" pile (a few hundred) alongside a long column of agents stuck at ~5 hours.

**Why it matters:** there's no way to tell a stuck/idle agent (5 hours, 1 action) from one actively working — both just read "running," with no health signal. Orphaned agents pile up indefinitely with no cleanup, so a long unattended run becomes hard to trust: you can't tell progress from a hang.

**Suggested fix:** a per-agent liveness indicator (last-activity timestamp plus a "stalled" badge once idle past a threshold), and auto-termination/cleanup of idle or orphaned agents.

### 7 — Overnight automation seemed to get harder under Projects
Before the Projects beta, my plain (non-Projects) sessions ran overnight plan work to completion on their own, without any Routine and without the long-idle disruption — I never set up a Routine for overnight automation. Under the Projects beta, the same overnight work appeared to need a separate Routine to keep going.

I want to be honest about the limits of this observation: I saw plain sessions run overnight fine without a Routine, and Projects seemed to need one, but I can't say for certain that Projects itself is the cause — I wasn't able to isolate it cleanly, and I'm happy to capture a side-by-side repro if that would help. Either way, the open question stands: if not a Routine, how is a Project meant to keep overnight work going? Project auto-resume or attach-Routines-to-a-Project (see "What we'd like") would resolve it.

### 8 — Effort tier seemed to drop after a long idle
After a long idle, the effort selector seemed to fall from the maximum I'd chosen back down to "Extra."

I observed this but wasn't able to reproduce it consistently, and I can't fully rule out that two separate controls are in play (e.g. a mode versus an effort tier resting at its default rather than an actual revert). A before/after capture across an idle period would confirm whether it's a real revert — happy to grab one.

### 9 — Disk accumulates with no auto-cleanup on a long multi-agent run
On this environment the writable volume sat around half-full against an effective ~38 GB ceiling (roughly 19 GB free). Most of the space went to accumulated agent worktrees (around 30 of them, ~2.8 GB total) plus the scratchpad (~3.9 GB), neither of which was auto-cleaned. Local mutation-testing runs additionally left uncleaned temporary virtualenvs (~165 MB) and a cache directory behind, also with no cleanup.

**Why it matters:** across a long unattended multi-agent run, untended worktree, scratchpad, and mutation artifacts steadily eat a finite envelope — a slow slide toward disk pressure with nothing cleaning up after it. (It ties into the orphaned-agents item above: stale agents leave stale worktrees.)

**Suggested fix:** auto-clean finished or orphaned agent worktrees, scratchpad temp, and mutation virtualenvs, and surface remaining disk headroom.

### 10 — A failed CI check can't be re-run
On a transient CI failure (a network flake or timeout), there's no way to "re-run failed jobs" from inside the workflow. The only workaround is an empty re-trigger change, which pollutes history and restarts the whole workflow rather than just the failed check. (The 403 in item 5 also blocks re-running CI through the API, leaving only this re-trigger path.)

**Why it matters:** routine transient failures force noisy history-polluting workarounds instead of a one-click re-run.

**Suggested fix:** a first-class "re-run this check / re-run failed jobs" capability.

### 11 — Chat-pasted screenshots aren't readable by the file tools
Images **pasted into chat** are visible to the model but never land at a path the file tools can open. **@-attached uploads, by contrast, are readable** at `/root/.claude/uploads/<session-id>/...` (I confirmed this by reading one). The two look identical in chat, so the difference isn't discoverable.

**Why it matters:** a user pastes a screenshot expecting the agent to work with it, and the agent silently can't reach the file — with no signal that paste and @-attach behave differently.

**Suggested fix:** materialise chat-pasted images to the same readable path so paste and @-attach behave identically.

### 12 — One shared append-only file serialises parallel merges
This repo requires every change to append to one shared ledger file. When several branches run in parallel, each edits that file's tail, so every pair conflicts at merge time — the parallelism collapses to sequential right at the merge step. Any repo with a hot append-only file (a changelog, a registry) will hit the same wall.

**Why it matters:** fan-out parallelism is undercut by a single shared file, and the conflicts are mechanical tail appends that a tool could resolve.

**Suggested fix:** a merge-queue mode that auto-rebases append-only conflicts (tail appends are associative), or detection of a hot shared file across a fan-out so it can warn up front.

### 13 — A typosquat `httpx2` wheel is installable from the managed index
The managed environment's package index serves a wheel named **`httpx2` 2.5.0** whose metadata cribs the real, very popular `httpx` project (its description and authorship signals). A near-name of a high-popularity package, dressed in that package's description, is installable — a classic typosquat / supply-chain hazard, and especially risky for agents that install dependencies non-interactively. (This is separate from a small internal test stub in our own repo, which is handled and out of scope here.)

**Why it matters:** an agent resolving a near-name could silently pull a typosquatted package from the managed index.

**Suggested fix:** scan for and block typosquat-shaped packages in the managed index, or at minimum warn loudly when a near-name of a popular package resolves.

---

## Screenshots attached

- **Stranded push-only state** — the overnight session showing the in-session 403 (relates to items 1, 2, 3, 5, 7).
- **GitHub App install** — the App correctly installed on @SJParthi (All repositories, read/write), which the running session still didn't pick up (relates to item 3).
- **Background-tasks panel** — review agents at 5 h+ runtimes with only 1–4 tool uses each, alongside the large finished pile (relates to item 6).
- **Effort tier before/after a long idle** — to confirm item 8 (this one I still need to capture cleanly).

*Prepared 2026-06-27. For the few items I couldn't reproduce reliably (7, 8, and the disk-full-during-mutation point under 9), I've said so plainly in the description rather than overstating them.*
