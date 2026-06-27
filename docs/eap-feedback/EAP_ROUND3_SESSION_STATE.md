# EAP Round 3 — Silent session-state loss after long idle or reconnect

> **Meta-feedback about Claude Code** (the Anthropic coding agent / "Claude
> Code Projects" Early Access Program), gathered while building the BruteX
> backtesting engine. **This is NOT BruteX product code, spec, or engine
> documentation** — it describes the *tool* used to build BruteX, not BruteX
> itself.
>
> Round-3 dossier. Builds on the round-2 findings (`evidence/EVIDENCE.md`,
> F-1..F-7) and the round-1 follow-up (`EAP_REPLY_ROUND1_FOLLOWUP.md`). Same
> conventions: each finding carries a date, a severity, and an explicit
> evidence label — **VERIFIED** / **HYPOTHESIS** / **UNVERIFIED** — per the
> project's no-illusion, verify-before-claim bar (CLAUDE.md Golden Rule #10).
> Nothing unproven is presented as fact.

**Theme.** An unattended, long-running Claude Code session can silently lose or
reset state after a long idle period or a reconnect, with no signal to the
user. Because the value proposition of these sessions is *running unattended
overnight to completion*, a silent state change is especially costly: the
operator only learns of it after the fact, when work has already been blocked
or degraded. The two findings below are both instances of this theme — one
**VERIFIED** (GitHub authorization), one **UNVERIFIED / held** (reasoning-effort
tier).

---

## F-8 — GitHub App authorization lapses mid-session with no in-session recovery

**Date:** 2026-06-27
**Severity:** high (blocks unattended completion)
**Evidence label: VERIFIED**

**What was observed.** A long-running session began with fully working GitHub
access — it opened PRs **#896–#905** earlier in the same run. Partway through,
the GitHub REST API began returning HTTP **403** for every API operation. The
session could still `git push` over its own credential channel, but could no
longer **open or merge PRs** — the GitHub work surface was effectively gone
mid-run while the git surface stayed alive.

The 403 body, quoted verbatim:

```
HTTP 403
{"message":"GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.","documentation_url":".../claude-code/github-actions"}
```

**Confirmed three independent ways.**

1. The GitHub MCP tools (`mcp__github__*`) were **no longer exposed** in the
   session — the integration that earlier opened #896–#905 had dropped out of
   the available tool set.
2. The `gh` CLI was **not installed** (invocation exited **127**), so there was
   no fallback path to GitHub through the shell.
3. A raw authenticated GitHub API call returned the **403 above**.

The 403 was **re-tested later in a fresh sub-agent** within the same session and
returned the **same 403** — i.e. it was a stable session-level state, not a
transient blip on one call.

**Root behavior (VERIFIED).** The GitHub grant is read **at session start**.
Re-connecting / re-authorizing the GitHub App mid-run does **not** propagate to
the already-running session — picking up the restored grant requires starting a
**new session**. So once the grant lapses mid-run, the running session is stuck
in a push-only state with no in-session path back to full GitHub access.

**Why it matters.** This directly undermines "run unattended overnight to
completion." An autonomous agent that loses GitHub access partway through a
batch is **stranded push-only** — it can commit and push branches, but cannot
open or merge the PRs that are the actual deliverable — until a human notices
and manually restarts the session. The failure is also **silent**: nothing
surfaced the auth drop proactively; it was discovered only by *attempting* a
GitHub action and getting the 403 back.

**Suggested fix framing (feedback, not a demand).**
- **Surface the auth drop proactively** the moment it happens, rather than
  letting the agent discover it by failing an action — a session-level
  notification or status badge would let an operator intervene immediately.
- **Allow in-session re-auth**: let a restored GitHub grant propagate into the
  running session so it does not require a full restart to recover.
- Failing that, **auto-checkpoint and resume in a fresh session** so the
  unattended loop survives the auth lapse without manual intervention.

**Evidence / screenshots.** The user has shared screenshots of the stranded
push-only state (the 403 surfaced in-session). The image files are not yet in
the repo; drop them into `evidence/` and replace the placeholder below.

![screenshot: 403 stranded state](TODO-attach)

---

## F-9 — Reasoning-effort tier may revert to a lower value after long idle

**Date:** 2026-06-27
**Severity:** medium (if confirmed)
**Evidence label: UNVERIFIED / NEEDS REPRODUCTION — held pending**

**What was observed (USER-REPORTED, not yet reproduced by us).** After a long
idle period, the effort / reasoning selector **appears** to change from the
user's chosen maximum back to **"Extra"**. This is a user observation; it has
**not** been reproduced under controlled conditions from inside the session.

**Open question to resolve BEFORE calling this a bug.** "ultracode" and "Extra"
may be **two independent controls**:

- **ultracode** is a *multi-agent orchestration MODE*.
- **Extra** is a *reasoning-effort TIER*.

If they are indeed two separate controls, then **"Extra" sitting in the
effort selector is simply the separate effort control resting at its default —
NOT a revert** of anything the user changed. In that case the observed state is
**expected behavior, not a defect**. This one-control-versus-two
disambiguation **must be resolved first**; until it is, the observation cannot
be classified as a regression.

**Why this is held, not reported as a defect.** This finding is explicitly
**HELD pending** two gates, and is **not** stated as a confirmed defect:

1. **Confirm one-control-vs-two** — determine whether "ultracode" (mode) and
   "Extra" (effort tier) are the same control or two independent ones, per the
   disambiguation above.
2. **Reproduce 2–3× with before/after screenshots** — a clean, repeatable
   sequence: set effort = max → leave idle for a long period → observe whether
   it reads "Extra" afterward (effort=max → after idle → effort=Extra), captured
   with a before/after screenshot pair.

**Scope note (why we can't root-cause it from inside an agent).** The effort
selector is **desktop-client UI state** — it lives outside the repository and
outside any tool an agent session can read or drive. An agent cannot inspect or
reproduce a client-side selector value from within its own session, so this
finding can only be confirmed by the user at the client, not by the agent.

**Evidence / screenshots.** The user should attach a **before/after pair**
demonstrating the sequence (effort set to max, then the post-idle state showing
"Extra"). The image files are not yet in the repo; replace the placeholders
below once captured.

![screenshot: effort tier before idle (max)](TODO-attach)
![screenshot: effort tier after idle (Extra)](TODO-attach)

---

## Status

- **F-8 (GitHub App authorization lapse):** **ready to send** — VERIFIED, with
  the verbatim 403 and the session-start-grant root behavior confirmed.
- **F-9 (effort-tier revert):** **held pending reproduction** — UNVERIFIED;
  must first resolve the ultracode-mode vs Extra-effort-tier disambiguation,
  then reproduce 2–3× with before/after screenshots, before it can be
  reported as a defect.
