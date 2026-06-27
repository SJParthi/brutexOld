# Claude Code Projects (beta) — Round-2 Feedback

**For:** Claude Code Early Access team — Diana Liu (claude-code-early-access@anthropic.com); Omid (eng lead), Jul 1 call
**From:** Parthiban (sjparthi93@gmail.com) · **Date:** 2026-06-27
**Workload under test:** BruteX — a large multi-agent backtesting-engine repo (the workload that exercised the Projects beta).

Round-2 feedback on the **Claude Code Projects beta**, from running a real, large multi-agent batch inside it. Findings are labelled **✅ Verified** (observed/reproduced this session) or **🟡 Needs repro** (plausible, root cause unconfirmed). Nothing unproven is stated as fact (mirrors the project's no-illusion bar, CLAUDE.md §2 Golden Rule #10).

---

## Top issues at a glance

| # | Issue | Status |
|---|-------|--------|
| 1 | GitHub access drops mid-session; no in-session recovery — must start a new session | ✅ Verified |
| 2 | That auth drop forced creating a **whole new Project** for the same repo | ✅ Verified |
| 3 | Connecting the GitHub App doesn't reach an already-running session | ✅ Verified |
| 4 | A Project isn't a self-running container; 24/7 needs a manually-wired Routine | ✅ Verified |
| 5 | The 403 is a broker message with misleading "org admin" wording on a **personal** account | ✅ Verified |
| 6 | Long-running agents sit idle/orphaned for hours with no liveness or stalled signal | ✅ Verified (screenshot-backed) |
| 7 | Can't re-run a failed CI check — must push an empty commit | ✅ Verified |
| 8 | Chat-pasted screenshots aren't file-readable (only @-attached uploads are) | ✅ Verified |
| 9 | One shared ledger file serialises parallel-branch merges | ✅ Verified |
| 10 | Typosquat `httpx2` wheel is installable from the managed env's package index (security) | ✅ Verified |
| 11 | **Projects beta seems to REQUIRE an external Routine for overnight automation that plain sessions did NOT (a regression)** | 🟡 Needs repro |
| 12 | Effort tier appears to revert after long idle | 🟡 Needs repro |

---

## What we'd like (feature requests)

- **Project auto-resume** — a first-class "keep working on goal X" so a Project drives continuity without hand-wiring scheduling.
- **In-session credential refresh** — re-bind GitHub / pick up a newly-connected App without tearing down the session.
- **A Project status surface** — one place showing what is persistent (history, repo), live (this session, bound credentials, environment), and stale.
- **Attach Routines to a Project** — Routines listed + owned by the Project, firing into its sessions.

---

## Details

### 1 — GitHub access drops mid-session; no in-session recovery
A long-running session opened PRs **#896–#905**, then every GitHub REST call began returning **403**. `git push` kept working but PR open/merge died — stranded push-only mid-run. Re-tested in a fresh sub-agent: same 403, a stable session-level state, not a blip. **Fix:** allow in-session re-auth, or auto-checkpoint and resume in a fresh session so the unattended loop survives the lapse.

### 2 — The auth drop forced a whole new Project for the same repo
Because nothing recovered the running session (re-tested 4×), the only clean way to a working session was to spin up a **second Project pointing at the same bruteX repo** — split context, duplicate coordinator, ambiguity over which is canonical. **Fix:** in-session/in-Project credential refresh removes the need to fork a second Project just to recover auth.

### 3 — Connecting the GitHub App doesn't reach a running session
After installing the App on **@SJParthi** (All repositories, read/write), the already-running session never saw the new access (re-tested twice); only a fresh session did. The natural fix appears to do nothing from inside the live session. **Fix:** propagate a credential change into the running session, or prompt *"new permissions detected — restart session to apply."*

### 4 — A Project isn't a self-running container
"Project" implied a self-running, always-on container. In practice work runs as **sessions**, and 24/7 automation still needs an explicitly-wired **Routine/trigger** — there is no ambient "the Project is working" loop. **Fix:** Project auto-resume, and/or set the expectation in-product (work runs as sessions; attach a Routine to make it run on its own).

### 5 — The 403 message is a broker gate and misleading
The body says *"an org admin must connect the Claude GitHub App for this organization,"* but this is a **personal account** (no org admin) and the App was demonstrably connected (it just opened #896–#905). The response is broker-side, not GitHub-native: its `documentation_url` points at `docs.anthropic.com`, it is 403 (GitHub's bad-credential failure is 401), and it carries none of GitHub's signature headers (`x-github-request-id`, `x-ratelimit-*`). Verbatim:

```
HTTP 403
{"message":"GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.","documentation_url":".../claude-code/github-actions"}
```

**Fix:** detect the personal-account + already-connected state and say what's true (*"this session's GitHub access was not enabled; start a new session to recover"*); distinguish broker-gate from credential errors.

### 6 — Long-running agents sit idle/orphaned for hours with no liveness or stalled signal
**Observed (✅ Verified — screenshot-backed):** the Background-tasks panel showed multiple agents marked "running" for **5+ hours** while making as few as **1 tool action** the whole time — e.g. *"Reuse review of PR #887"* at **5 h 01 m with 1 tool use**, *"Altitude review of PR #887"* at **5 h 01 m with 3 tool uses**, and several PR #888-docs reviews at **~5 h with 2 tool uses**. They never auto-terminate and accumulate: the panel shows a large **"Finished 294"** pile alongside a long column of 5 h-running reviews.

**Why it matters:** there is no way to distinguish a **stuck/idle** agent (5 hours, 1 action) from one that is actively working — same "running" state, no health signal. Orphaned agents pile up indefinitely with no cleanup. This makes long unattended runs untrustworthy: you can't tell progress from a hang.

**Fix:** a per-agent liveness indicator (last-activity timestamp + a "stalled" badge once idle past a threshold), and auto-termination/cleanup of idle/orphaned agents.

### 7 — Can't re-run a failed CI check
On a transient CI failure (network flake, timeout) the agent has no way to "re-run failed jobs" — the only workaround is an **empty re-trigger commit**, which pollutes history and restarts the whole workflow. There are **48 `chore(ci): re-trigger …` commits** across this session's refs. (The broker 403 in #5 also blocks re-running CI via the API, leaving only the empty-commit path.) **Fix:** a first-class "re-run this check / re-run failed jobs" capability.

### 8 — Chat-pasted screenshots aren't file-readable
Images **pasted into chat** are visible to the model but never materialised to a path the file tools can reach. **@-attached uploads ARE readable** at `/root/.claude/uploads/<session-id>/...` (demonstrated with a real `Read`). The difference isn't discoverable — both look the same in chat. **Fix:** materialise chat-pasted images to the same readable path so paste and @-attach behave identically.

### 9 — One shared ledger file serialises parallel merges
BruteX requires every PR to append to one shared ledger (`docs/MIGRATION.md`, 6,297 lines). N parallel PRs each edit its tail, so each pair conflicts — parallelism collapses to sequential at merge time. A pattern any repo with a hot append-only file (CHANGELOG, registry) will hit. **Fix:** a merge-queue mode that auto-rebases append-only conflicts (tail appends are associative), or detect a hot shared file across a fan-out and warn up front.

### 10 — Typosquat `httpx2` wheel installable from the managed index
The managed environment's package index serves a wheel named **`httpx2` 2.5.0** whose metadata cribs the real `httpx` project (description + authorship signals). A near-name of a high-popularity package, dressed in its description, made installable — a classic typosquat / supply-chain hazard, especially for agents installing non-interactively. (Distinct from our own repo's internal test stub, which is handled and out of scope.) **Fix:** scan/block typosquat-shaped packages in the managed index, or at minimum warn loudly on a near-name resolve.

### 11 — Overnight automation got HARDER under Projects (a regression)
**User-reported (first-hand, this past week):** plain (non-Projects) sessions ran overnight plan activities **to completion without any Routine** and without the long-idle disruption — the user has **not** used Routines for overnight automation. With the **Projects beta**, the same overnight work now appears to **need an external Routine** to keep going — unattended overnight got harder, not easier. **Open question / request:** the user does not want to wire a Routine (it worked without one before) — so if not a Routine, **how is the Project itself supposed to keep overnight work going?** Resolution would be Project auto-resume or attach-Routines-to-a-Project (see feature requests). *Honesty: the session-vs-Projects experience is the user's first-hand testimony; "Projects causes it" is a correlation with root cause **unconfirmed** — no internal causation asserted.*

### 12 — Effort tier appears to revert after long idle
After a long idle, the effort selector **appears** to fall from the chosen maximum back to "Extra." Held pending (a) confirming whether "ultracode" (a mode) and "Extra" (an effort tier) are two independent controls — if so this is the default control resting, not a revert — and (b) a 2–3× before/after screenshot pair. An agent can't inspect client-side UI state, so only the user can confirm.

---

## Screenshots to attach

| Screenshot | Backs | Status |
|---|---|---|
| Overnight stranded push-only state (the in-session 403) | items 1 / 2 / 3 / 5 / 11 | operator has it; text-sufficient |
| Correct GitHub-App install (`@SJParthi`, All-repos, read/write) | item 3 | operator has it; text-sufficient |
| Background-tasks panel — 5 h+ runtimes with 1–4 tool uses (the "Reuse/Altitude/Efficiency/Simplification review of PR #887/#888" rows + the "Finished 294" counter) | item 6 | ✅ screenshot-backed |
| Effort tier **before** long idle (= max) → **after** (= "Extra") | item 12 | **still needed** — the before/after pair to move it Verified |

*Prepared 2026-06-27. Every Verified item is reproducible from this repo and session; every 🟡 item is labelled needs-repro per the project's verify-before-claim bar (CLAUDE.md §2 Golden Rule #10).*
