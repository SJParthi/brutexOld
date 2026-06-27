# Claude Code Projects (beta) — Round-2 Feedback

**For:** Claude Code Early Access — Diana Liu (claude-code-early-access@anthropic.com); Omid (eng lead), Jul 1 call · **From:** Parthiban (sjparthi93@gmail.com) · **Date:** 2026-06-27 · **Workload:** BruteX — a large multi-agent backtesting repo, run as a real overnight batch in the Projects beta.

This is round-2 feedback on the **Claude Code Projects beta**, gathered from a real, large, multi-agent overnight batch. The strongest item — a misleading GitHub-access error — comes first; the rest follow.

For context: across these runs the model (**Opus 4.8**) and the **maximum reasoning setting** were the same, and **I never used a Routine**; the most visible difference was running under the **Projects beta**. I've run overnight multi-agent batches and day-to-day sessions before — same model, same reasoning setting, no Routine — without hitting these problems. I can't fully rule out workload scale or timing, so I flag this as the consistent pattern I saw — not a proven cause.

---

## Issues at a glance

| # | Issue |
|---|-------|
| 1 | A misleading GitHub-access error: a 403 blames an "org admin" on a personal account with no org, and doesn't look like a real GitHub error. |
| 2 | GitHub access drops mid-session with no in-session recovery — the only path back is a new session, and in my case a whole new Project. |
| 3 | Connecting the GitHub App doesn't reach an already-running session; only a fresh session picks it up. |
| 4 | Background/review agents appear idle for hours with no liveness signal and no cleanup. |
| 5 | Finished/orphaned agent worktrees aren't torn down across a long multi-agent run. |

---

## What we'd like

- **Make a Project auto-monitor and continue on its own** — Project-native continuity and scheduling so a Project keeps working toward its goal unattended, without separately wiring a Routine. Today the work runs as sessions and overnight continuity has to be arranged separately, rather than the Project sustaining it natively.
- **In-session credential refresh** — re-bind GitHub, or pick up a newly-connected App, without tearing down the session.
- **Idle-agent liveness and cleanup** — a per-agent last-activity signal plus auto-termination of stalled or orphaned agents.

---

## Open questions

- How is a Project meant to sustain overnight work without a hand-wired Routine? I have never used a Routine — not in prior overnight runs, not in normal day-to-day sessions — and they ran fine unaided. Under the Projects beta the same overnight work appeared to need one. I couldn't isolate whether Projects is the cause, but that's the consistent difference I saw.
- Does an explicitly-set maximum reasoning tier persist across a long idle? I set it to maximum and later found it on a lower tier ("Extra"). I couldn't reproduce it reliably or tell whether "ultracode" is a separate control from the effort tier, so I'm raising it as a question rather than a finding.

---

## Details

### 1 — A misleading GitHub-access error
A long-running session had been opening and merging pull requests normally for hours. Then every GitHub REST call began returning **403** at once. The body says *"an org admin must connect the Claude GitHub App for this organization"* — but this is a **personal account** with no organization and no org admin, and the App was demonstrably connected (it had been merging PRs minutes earlier). The decisive tell that this isn't GitHub's own error: its `documentation_url` points at `docs.anthropic.com` (not github.com), and the response carries none of GitHub's signature headers (`x-github-request-id`, `x-ratelimit-*`). The exact body:

```
HTTP 403
{
  "message": "GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.",
  "documentation_url": "https://docs.anthropic.com/en/docs/claude-code/github-actions"
}
```

**Why it matters:** the message sends a personal-account user chasing an "org admin" that doesn't exist, instead of telling them their session's access lapsed.

**Suggested fix:** detect the personal-account, already-connected case and say what's true (*"this session's GitHub access was not enabled; start a new session to recover"*), and distinguish an access-not-enabled state from a real credential error.

### 2 — GitHub access drops mid-session with no in-session recovery
When the 403 above hit, `git push` kept working, but opening and merging PRs through the API stopped — the session was stranded push-only mid-run. A retry in a fresh sub-agent returned the same 403, so this was a stable session-level state, not a momentary blip. The specific gap is that there was no **in-session** way to recover access: nothing inside the running session could re-establish it.

Recovery did exist, but only by leaving the session: starting a new session was the only way back, and in my case the only clean path was a **second Project pointing at the same repo**. That split my context across two Projects, duplicated the coordinating session, and left it ambiguous which Project was canonical — a heavy, confusing workaround for what should be an in-session refresh.

**Why it matters:** an unattended overnight run can't survive a credential lapse it can't recover from in-session — it just stalls until a human starts a fresh session, and recovering it shouldn't require forking a second Project.

**Suggested fix:** allow in-session re-auth, or auto-checkpoint and resume in a fresh session, so the loop survives the lapse without a new Project.

### 3 — Connecting the GitHub App doesn't reach a running session
After installing the GitHub App on my account (**@SJParthi**, All repositories, read/write), the already-running session never saw the new access. Only a brand-new session picked it up. From inside the live session, connecting the App appeared to do nothing.

**Why it matters:** the natural recovery step — connect/repair the App — has no effect on the session that actually needs it.

**Suggested fix:** propagate a credential change into the running session, or prompt *"new permissions detected — restart the session to apply."*

### 4 — Background/review agents appear idle for hours with no liveness or cleanup
The background-tasks panel showed multiple review agents marked "running" for **5+ hours** while having made as few as **1 tool action** in that whole time — for example, one at **5 h 01 m with 1 tool use**, another at **5 h 01 m with 3 tool uses**, and several more around **5 h with 2 tool uses**. They never auto-terminated and simply accumulated: a large "finished" pile (a few hundred) alongside a long column of agents sitting at ~5 hours.

**Why it matters:** there's no way to tell a stalled agent (5 hours, 1 action) from one that's slowly but genuinely working — both just read "running," with no health signal. Orphaned agents pile up with no cleanup, so a long unattended run becomes hard to trust: you can't tell progress from a hang.

**Suggested fix:** a per-agent liveness indicator (last-activity timestamp plus a "stalled" badge once idle past a threshold), and auto-termination/cleanup of idle or orphaned agents.

### 5 — Finished/orphaned agent worktrees aren't torn down
The platform-owned part: finished and orphaned agent worktrees (~34 of them, ~2.8 GB) were never reclaimed once their agent was done — they just accumulated across the run. Alongside them sat scratchpad files (~3.9 GB), some of which is genuinely the run's own output, so I'm not asking the platform to clean up my files — only the per-agent worktree (and the scratch space it created) when that agent finishes. This is a housekeeping gap tied to the idle-agents item above: stale agents leave stale worktrees behind, with nothing removing them.

**Why it matters:** across a long unattended multi-agent run, finished and orphaned agents leave platform-owned worktrees that nothing reclaims, so the workspace only grows the longer it runs.

**Suggested fix:** tear down a per-agent worktree (and the scratch space it created) once that agent finishes, rather than leaving finished/orphaned worktrees behind.

---

## Screenshots (sent separately with the email)

No images are embedded in this document. The screenshots below are attached to the email; this list maps each one to the issue it illustrates.

- **Stranded push-only state** — the overnight session showing the in-session 403 (illustrates items 1 and 2).
- **GitHub App install** — the App correctly installed on @SJParthi (All repositories, read/write), which the running session still didn't pick up (illustrates item 3).
- **Background-tasks panel** — review agents at 5 h+ runtimes with only 1–4 tool uses each, alongside the large finished pile (illustrates item 4).

*Prepared 2026-06-27. Where I couldn't reproduce something reliably, I've said so plainly rather than overstating it.*
