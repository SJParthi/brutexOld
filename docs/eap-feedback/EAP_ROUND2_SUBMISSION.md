# Claude Code Projects (beta) — Consolidated Round-2 Submission

**For:** Claude Code Early Access team — Diana Liu (claude-code-early-access@anthropic.com); Omid (eng lead), Jul 1 feedback call
**From:** Parthiban (sjparthi93@gmail.com)
**Date:** 2026-06-27
**Workload under test:** BruteX — a large multi-agent backtesting-engine repo. The product under review is the **Claude Code Projects beta**; BruteX is only the workload that exercised it.

> **What this is.** One consolidated, evidence-gated dossier of every Round-2
> finding, gathered while running a real, large multi-agent batch inside the
> Projects beta. It merges three working notes (the Projects pilot batch, the
> session-state note, and the evidence gallery) into a single submission.
>
> **Beta-appropriate framing (read first).** Projects is a **private
> early-access beta** — I am not flagging "missing docs" as defects (a beta is
> expected to be sparsely documented). Every Projects item below is one of three
> things and nothing else:
> 1. a **behavior** the feature did or didn't do,
> 2. an **expectation gap** (the mental model I formed vs what actually happened), or
> 3. a **feature request** (what I'd want the beta to add).
>
> **Verify-before-claim.** Each item carries an explicit evidence label —
> **VERIFIED** (directly observed/reproduced this session), **HELD** (plausible
> but needs a user-side confirmation before it can be called a defect), or
> **UNVERIFIED** (a user-reported observation not yet reproduced). Nothing
> unproven is presented as fact. This mirrors the BruteX project's own
> no-illusion bar (CLAUDE.md §2 Golden Rule #10).

---

## Table of contents

1. **Projects (beta) — pilot feedback** (P-1..P-12): behavior · expectation gaps · feature requests · held
2. **Session-state findings** (F-8..F-14): auth lapse, effort-revert, Routine limits, typosquat, Dispatch, billing
3. **Bucket-1 — verified findings** (V-1..V-7)
4. **Bucket-2 — ideas** (I-1..I-5) + 3 questions for Omid
5. **Original evidence gallery** (F-1..F-7)
6. **Screenshot map**
7. **Evidence integrity note**

---

# 1. Projects (beta) — pilot feedback

Projects **is** a real beta feature I have access to: there is an internal
"coordinator session" that orchestrates a project, and this very batch ran
inside one. So this is **not** "Projects doesn't exist." The items below are the
behaviors I saw, the expectations that diverged from them, and the capabilities
I'd want next.

## A. Behavior observed

### P-1 — GitHub access dropped mid-session and could not be recovered inside that session

**Date:** 2026-06-27 · **Severity:** high (blocks unattended completion) · **Evidence: VERIFIED (this session)**

**What happened.** A long-running session began with fully working GitHub
access — it opened PRs **#896–#905** earlier in the same run. Partway through,
the GitHub REST API began returning HTTP **403** for every API operation. The
session could still `git push` over its own credential channel, but could no
longer **open or merge PRs** — the GitHub work surface was gone mid-run while
the git surface stayed alive. **Being "in a Project" did not shield the running
session's credentials.** The literal question this raised: *"why a new session
if I'm in a Project?"* (full origin analysis in **F-8**, §2).

**Why it matters.** A persistent Project that can't keep its own session
authenticated breaks the core promise of "persistent." Long-running work dies on
a credential drop and the only recovery observed was to start over — the
opposite of what a Project is supposed to buy you.

**Proof.** The verbatim 403 body, the broker-vs-GitHub origin analysis, and the
push-works-while-API-403s channel divergence are all in **F-8** (§2). Backed by
the operator's overnight-stuck screenshots.

**Request (not a demand).** Let a Project re-bind / refresh credentials for its
running session without forcing a brand-new session (see **P-8**), or at minimum
surface the auth drop proactively the moment it happens instead of letting the
agent discover it by failing an action.

### P-2 — Connecting the GitHub App did not propagate into the Project's running session

**Date:** 2026-06-27 · **Severity:** high · **Evidence: VERIFIED (re-tested twice)**

**What happened.** After the correct remediation — installing the Claude GitHub
App on **@SJParthi**, scope **All repositories**, read/write — the new access
did **not** take effect in the already-running Project session. A fresh session
was required for it to be picked up. I re-tested twice to rule out a transient
glitch: the running session never saw the newly-granted access; only a new
session did.

**Why it matters.** The natural fix for an auth gap — "connect the App" —
appears to do nothing from inside the live session, so the user repeats it,
doubts whether it worked, and only stumbles onto "start a new session" by
accident. It compounds P-1.

**Proof.** F-8 (§2) records the re-test-after-install evidence in full
(install-did-not-recover-running-session, re-tested twice). Backed by the
operator's GitHub-App-install screenshots.

**Request.** Propagate a credential/permission change (App connect, repo grant)
into the running session, or surface an explicit *"new permissions detected —
restart session to apply"* prompt instead of silently requiring a fresh session
(see **P-9**).

### P-3 — The coordinator / ambient orchestration is invisible to the user

**Date:** 2026-06-27 · **Severity:** medium (operability / trust) · **Evidence: VERIFIED**

**What happened.** Projects are orchestrated by an internal coordinator session,
but the user gets **no signal** of whether or how the Project is continuously
working — no "the coordinator is active / idle," no view of what it is doing on
the user's behalf. The orchestration layer that defines a Project is hidden.

**Why it matters.** Invisibility of the coordinator is why the Project "feels"
idle even when something is (or should be) happening — and why the expectation
gap in P-4 is so hard to self-correct. The user cannot tell "working" from
"nothing scheduled."

**Request.** Surface coordinator/ambient state in the Project UI: active vs
idle, what is currently running, and what (if anything) is scheduled
(see **P-10**).

## B. Expectation gaps (the mental model I formed vs what actually happened)

### P-4 — I expected a Project to be a self-running, persistent container; it runs as sessions + a manually-wired Routine

**Date:** 2026-06-27 · **Severity:** medium (expectation-setting) · **Evidence: VERIFIED (this session)**

**What I expected.** The word "Project" led me to expect a self-running,
always-on container that keeps working on its own — something I could point at a
goal and walk away from.

**What actually happened.** Work runs as **sessions**, and 24/7 / recurring
automation still requires an explicitly-wired **Routine/trigger**. There is no
ambient "the Project is working" loop. This divergence cost multiple rounds of
confusion this session before the model clicked. (Routines themselves work — see
the retraction note and **F-11** for their real constraints.)

**Why it matters.** The gap between the implied promise ("a Project that runs")
and the mechanism ("sessions + a manually-wired Routine") is exactly where a
beta loses trust — a user waits for work that will never start on its own.

**The two things I learned the hard way (stated as behavior, not a docs ask):**

- **Persistence boundary I hit:** conversation history **persists** across
  sessions, but **live credentials and the running VM/environment do not** —
  they are session-scoped and re-bound per session. This is the mechanism behind
  P-1/P-2. I only learned it by hitting it mid-task — the most expensive way to
  learn it.
- **Where the work and the auth actually live:** the **session** runs the work
  and **holds the auth** (bound at session start, per F-8); a **Routine/trigger**
  fires scheduled work; the **environment** is the VM the session runs in. I had
  to infer this hierarchy from behavior.

**Request.** Either make the Project actually drive continuity (see **P-8**), or
let the running-session behaviors above (auth re-bind, status) match the
"persistent" expectation (P-9/P-10).

## C. Feature requests (grounded in the behaviors above)

### P-5 — Project-level auto-resume / "keep working"

**Evidence: VERIFIED (request grounded in P-4)**

Today continuity requires manually wiring a Routine/trigger; a Project does not
resume work on its own. Request: a first-class Project-level "keep working" /
auto-resume so users get the continuity they already expect (P-4) without
hand-building scheduling plumbing — a Project setting *"auto-resume in-flight
work" / "keep working on goal X,"* backed by the existing routine/trigger
machinery but owned by the Project. This is the single capability that would
make "Project" mean what users think it means.

### P-6 — In-Project credential refresh

**Evidence: VERIFIED (request grounded in P-1 / P-2)**

A mid-session credential drop could not be recovered in-session (P-1), and
connecting the GitHub App took effect only after a new session (P-2). Request:
an in-session *"refresh credentials / re-bind GitHub"* action (and/or automatic
pickup of a newly-connected App) that does not require tearing down the session.
Removes the "start a new session to fix auth" tax that currently breaks
long-running Project work.

### P-7 — A clear Project status surface

**Evidence: VERIFIED (request grounded in P-3 / P-4)**

There is no single place that tells the user what in a Project is **persistent**,
what is **live** (this session), and what is **stale**. Request: a Project
status panel — persistent items (history, repo, config) · live items (current
session, bound credentials, environment) · stale/expired items, each with a
clear badge. This is the cheapest fix that addresses P-1, P-2, P-3, and P-4 at
once: most of the confusion above is *"I can't tell what state I'm in."*

### P-8 — Attach Routines/triggers to a Project explicitly

**Evidence: VERIFIED (request grounded in P-4 / P-5)**

Routines/triggers exist but are not visibly owned by the Project — so "the
Project takes care of it" is not actually true; the user wires scheduling
separately. Request: first-class *"Routines attached to this Project,"* listed
and editable in the Project UI, firing into the Project's sessions. Makes the
Project the unit of automation (matching the mental model) and makes P-5's "keep
working" composable from explicit, inspectable routines.

## D. Held — needs user confirmation (NOT presented as verified)

### P-9 — Projects / EAP billing vs sessions

**Evidence: HELD**

Whether Project / EAP usage is metered against the plan, exempt, or free is
**unknown** — I have not confirmed it, so it is raised as a question, not a
claim. Heavy multi-agent fan-outs inside a Project plausibly consume plan usage,
but I will not assert that without confirmation. Operators running large Projects
need a documented billing policy to make a go/no-go call. Cross-ref **F-14**.

### P-10 — Dispatch ↔ Projects relationship

**Evidence: HELD**

The relationship between "Dispatch" and Projects is **unclear** to me, and
Dispatch may be plan-gated. I have not verified how (or whether) Dispatch
integrates with a Project — raised as an open question, not a finding.
Cross-ref **F-13**.

### P-11 — Overnight disruption appears specific to the Projects beta (user-reported correlation)

**Date:** 2026-06-27 · **Severity:** (if confirmed) high · **Evidence: HELD (user-reported correlation; no causation claimed)**

**What was observed (user-reported).** Across three run modes the user reports a
difference in overnight stability:

- **Fable-5 overnight runs** ran the **full night uninterrupted** — no
  human/manual intervention, no Routines — completing per plan.
- Recent **plain (non-Projects) sessions** also did **not** show the
  mid-session disruption.
- With the **Projects beta** feature access, the mid-session disruption **did**
  occur — the GitHub-access drop + "start a new session" stranding (P-1 / P-2 /
  F-8).

**Why it matters.** If the disruption is specific to the beta, it directly
undermines the unattended-overnight value proposition of the exact feature being
piloted.

**Honest caveat (why this is HELD).** This is a **user-reported correlation**,
not a controlled comparison; there is no telemetry exposed to compare the three
run paths, so the **root cause is UNCONFIRMED** and **no causation is claimed.**
A plausible-but-unverified mechanism worth checking: the Projects beta may wrap
sessions with a different lifecycle / coordinator or auth-binding path than plain
sessions.

**Request.** Have Anthropic compare the **session lifecycle + GitHub-auth
binding** between Projects-beta sessions and plain sessions / Fable-5 overnight
runs, to determine why only the beta path drops mid-run.

### P-12 — Reasoning-effort tier may revert to a lower value after long idle

**Date:** 2026-06-27 · **Severity:** medium (if confirmed) · **Evidence: UNVERIFIED / NEEDS REPRODUCTION — held pending**

Same finding as **F-9** (§2) — kept here as the Projects-context cross-link. The
user observed the effort selector **appears** to fall from the chosen maximum
back to "Extra" after a long idle. Held pending (a) resolving whether "ultracode"
(mode) and "Extra" (effort tier) are two independent controls — if so, "Extra"
sitting in the effort selector is just the separate control at its default, **not
a revert** — and (b) a 2–3× before/after reproduction. An agent cannot inspect
client-side UI state from inside its own session, so only the user can confirm
this. See **F-9** for the full write-up.

---

# 2. Session-state findings (F-8..F-14)

These were gathered in the same large multi-agent session. F-8 is the full,
deep write-up behind P-1/P-2; the rest cover effort-revert, Routine limits, a
supply-chain hazard, Dispatch, and billing.

## F-8 — GitHub App authorization lapses mid-session with no in-session recovery

**Date:** 2026-06-27 · **Severity:** high (blocks unattended completion) · **Evidence: VERIFIED**

**What was observed.** A long-running session began with fully working GitHub
access (opened PRs **#896–#905** earlier in the same run). Partway through, the
GitHub REST API began returning HTTP **403** for every API operation. The session
could still `git push`, but could no longer **open or merge PRs** — the GitHub
work surface was effectively gone mid-run while the git surface stayed alive.

The 403 body, quoted verbatim:

```
HTTP 403
{"message":"GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.","documentation_url":".../claude-code/github-actions"}
```

**Confirmed three independent ways.**

1. The GitHub MCP tools (`mcp__github__*`) were **no longer exposed** in the
   session — the integration that earlier opened #896–#905 had dropped out of
   the available tool set.
2. The `gh` CLI was **not installed** (invocation exited **127**) — no shell
   fallback path to GitHub.
3. A raw authenticated GitHub API call returned the **403 above**.

The 403 was **re-tested later in a fresh sub-agent** within the same session and
returned the **same 403** — a stable session-level state, not a transient blip.

**New evidence — the 403 originates in the Anthropic broker, not GitHub
(VERIFIED).** The response is **not** a GitHub-native rejection; it is the
Claude/Anthropic GitHub *broker* refusing to mint a downstream call:

- The body's `documentation_url` points at **`docs.anthropic.com`**
  (`.../claude-code/github-actions`), not at `docs.github.com`.
- The status is **HTTP 403**. GitHub's native bad-credential failure is **HTTP 401 "Bad credentials"** with a GitHub-format body — a different code and shape.
- The response carries **none of GitHub's signature headers** (`x-github-request-id`, `x-ratelimit-*`).
- The agent proxy's own `/__agentproxy/status` carries **no GitHub token/expiry state at all**, and `recentRelayFailures` was **empty** — the proxy never even attempted a GitHub relay; the refusal is upstream, at the broker's enablement gate.

> **Live corroboration captured this session.** While diagnosing the #905 CI
> failure, a `curl` to the GitHub Actions REST API
> (`api.github.com/repos/SJParthi/bruteX/actions/runs/.../jobs`) returned the
> **byte-identical broker 403** quoted above — confirming the gate is still in
> the push-only state and that the failure is broker-side, not a GitHub
> rejection. (This is also why the #905 CI logs could not be read from inside
> this session — see §1 of the cover report.)

**New evidence — the 403 message is misleading (VERIFIED).** It says *"an org
admin must connect the Claude GitHub App for this organization."* But:

- This is a **personal account (SJParthi), not an organization** — there is no
  "org admin" in the loop.
- The App was **demonstrably connected** — the same session used it to open PRs
  **#896–#905** earlier. So "must connect … for this organization" is factually
  wrong at the moment it is shown.

The message **misattributes a session-enablement lapse** as a "never connected /
needs an org admin" problem — pointing the user at the wrong fix.

**New evidence — installing the App did NOT recover the running session
(VERIFIED).** After the user installed the Claude GitHub App on **@SJParthi**,
scope **All repositories**, **read/write**, this **already-running** session
**still returned the same 403** when re-tested (twice). This proves GitHub access
is **bound at session start**: a newly-installed grant **cannot** be picked up by
a session already running. Only a **fresh session** can.

**New evidence — the two channels diverge (VERIFIED).** Throughout the 403 state,
`git push` over the session's own git credential channel **kept working.** The
agent was left **half-functional**: push **yes**, PR/merge/API **no.** The git
surface and the REST-API surface authorize independently, and only one lapsed —
which is why the failure is easy to miss (commits keep landing; only the PR step
fails).

**Root behavior (VERIFIED).** The GitHub grant is read **at session start** and
bound for that session's life. Re-connecting / installing the App mid-run does
**not** propagate to the already-running session — picking up the restored grant
requires a **new session.**

**Why it matters.** Directly undermines "run unattended overnight to completion."
An autonomous agent that loses GitHub access partway through a batch is
**stranded push-only** — it can commit and push branches but cannot open or merge
the PRs that are the deliverable — until a human restarts the session. The
failure is also **silent**: nothing surfaced the auth drop; it was discovered
only by *attempting* a GitHub action and getting the 403.

**Suggested fixes (feedback, not a demand).**
- **Surface the auth drop proactively** the moment it happens (session-level
  notification / status badge) so an operator can intervene immediately.
- **Allow in-session re-auth** so a restored grant propagates into the running
  session (the re-test-after-install shows it currently does not).
- Failing that, **auto-checkpoint and resume in a fresh session** so the
  unattended loop survives the lapse without manual intervention.
- **Fix the message text** — detect the personal account + already-connected
  state and say what is true: *"this session's GitHub access was not enabled;
  start a new session (or re-enable) to recover."*
- **Distinguish broker-gate from credential errors** (the `docs.anthropic.com`
  URL + 403 + missing GitHub headers identify it as a broker-side gate).

**Evidence / screenshots.** The user has shared screenshots of the stranded
push-only state. Image files are not yet in the repo — see the screenshot map
(§6). `(attach: overnight-stuck-403)`

## F-9 — Reasoning-effort tier may revert to a lower value after long idle

**Date:** 2026-06-27 · **Severity:** medium (if confirmed) · **Evidence: UNVERIFIED / NEEDS REPRODUCTION — held pending**

**What was observed (user-reported, not yet reproduced).** After a long idle
period, the effort / reasoning selector **appears** to change from the user's
chosen maximum back to **"Extra."** This is a user observation; it has **not**
been reproduced under controlled conditions from inside the session.

**Open question to resolve BEFORE calling this a bug.** "ultracode" and "Extra"
may be **two independent controls**: "ultracode" a *multi-agent orchestration
MODE*, "Extra" a *reasoning-effort TIER*. If they are separate, then "Extra"
sitting in the effort selector is simply that control resting at its default —
**NOT a revert** — i.e. expected behavior, not a defect. This disambiguation
must be resolved first.

**Why it is held.** Explicitly **HELD pending** two gates: (1) confirm
one-control-vs-two; (2) reproduce 2–3× with a before/after screenshot pair
(effort=max → after idle → effort=Extra).

**Scope note.** The effort selector is **desktop-client UI state** — outside the
repository and outside any tool an agent session can read or drive. An agent
cannot inspect or reproduce a client-side selector value from within its own
session, so only the user can confirm this.

**Evidence / screenshots.** Needs a **before/after pair**. `(attach: effort-before-idle-max)` / `(attach: effort-after-idle-extra)` — still to be captured.

## F-10 — Expected a Project to keep running; the execution model is sessions + a wired Routine

**Date:** 2026-06-27 · **Severity:** medium (expectation-setting) · **Evidence: VERIFIED (evidenced user expectation gap)**

**What was observed (reframed as an expectation gap, not a docs ask).** The
Claude Code UI prominently surfaces a **"Projects"** item, and the EAP itself is
called "Claude Code Projects." The mental model this produced was that a
"Project" is a **persistent, auto-running container** that lives on and does work
on its own — *avoiding* having to think in sessions. The behavior I actually
observed is the opposite: work runs as **sessions**, and unattended automation
requires a **Routine / trigger**.

**Why it matters.** The persistence/automation expectations are precisely the
ones that matter most for an "unattended overnight" tool. A user who believes "I
have a Project, so it keeps running" will not set up the Routine they actually
need — exactly the gap in **P-4**.

**Honest caveat.** This is **"the execution model diverged from my expectation,"
NOT "Projects is broken"** and NOT a documentation complaint — a private beta is
expected to be sparsely documented. It is filed as a behavior/expectation report
so the team knows the default mental model new pilots form.

**Request (for the beta team).** Make the execution model match the expectation
where feasible (P-5 auto-resume, P-8 attach-Routines), and/or set the expectation
in-product: *work runs as sessions; to make a Project run on its own, attach a
Routine.*

## F-11 — Routine limits constrain frequent / real-time automation

**Date:** 2026-06-27 · **Severity:** low–medium (automation latency ceiling) · **Evidence: VERIFIED (from docs)**

**What was observed.** Per the published Routines documentation
(`https://code.claude.com/docs/en/routines`), a Routine's **minimum schedule
interval is one hour** — **sub-hourly cron is rejected** — and there is a
**daily per-account run cap**. Together these put a floor on how frequently an
unattended loop can fire.

**Why it matters.** Some real automation needs **lower latency than one hour** —
driving CI every few minutes, polling a long-running job for completion, or
reacting promptly to an external event. With a one-hour floor and a daily cap, a
tight unattended loop cannot be expressed as a Routine; the operator must fall
back to a foreground session (defeating "unattended") or accept hour-granularity
reactions.

**Suggested fixes (for the beta team).**
- **Allow sub-hourly intervals** for short / bounded jobs, even if gated behind a
  stricter quota.
- Or **provide event-driven triggers** as the supported lower-latency
  alternative to polling Routines, so users reach for the right primitive instead
  of trying to make a 1-hour Routine behave like a 1-minute one.

> **Note (Routines do work).** An earlier round-1 note that "Routines don't fire"
> was **retracted** after verifying Routines fire correctly. F-11 is only about
> their **interval/cap limits**, not whether they work.

## F-12 — A typosquat `httpx2` wheel is installable from the managed env's package index

**Date:** 2026-06-27 · **Severity:** high (supply-chain hazard) · **Evidence: VERIFIED (security-relevant)**

**What was observed.** The **managed environment's package index** serves a wheel
named **`httpx2` version `2.5.0`** whose metadata **cribs the real `httpx`
project's** — it advertises itself as a *"next generation HTTP client"* and
reuses authorship signals associated with the genuine `httpx` (Tom Christie /
Pydantic-adjacent). The name `httpx2` is a one-character / suffix variant of the
widely-used `httpx`. In shape this is a **classic typosquat**: a near-name
package, dressed in the real package's description, made **installable** from the
index the environment trusts.

**Scope note (distinct from an internal issue).** This is **NOT** our own
repository's fabricated test stub. That stub was an *internal* artifact a prior
agent created in **our** repo, it is **our** issue, it has already been handled,
and it is **out of scope for EAP feedback** (see "Considered but excluded" in
§5). **This finding is specifically that the managed ENVIRONMENT's package index
itself serves an installable typosquat** — a property of the Claude Code
environment, not of BruteX.

**Why it matters.** An installable typosquat in the managed index is a
**supply-chain hazard**: a developer (or an autonomous agent) who fat-fingers
`httpx2`, or is steered toward it by a poisoned suggestion, can **silently
substitute** a look-alike for a real dependency — and because the metadata mimics
the genuine project, the substitution is hard to spot in review. Agents
installing dependencies non-interactively are especially exposed.

**Suggested fixes (for the beta team).**
- **Scan and block typosquat-shaped packages** in the managed index — name-edit
  distance to popular packages, plus metadata that copies a different project's
  description/authorship, are strong signals.
- At minimum, **warn loudly** when an install resolves to a near-name of a
  high-popularity package.

## F-13 — "Dispatch (Beta)" is shown in the sidebar but may not be activatable

**Date:** 2026-06-27 · **Severity:** low (clarity / availability) · **Evidence: HELD — needs user confirmation**

**What was observed (HELD).** The Desktop sidebar shows a **"Dispatch (Beta)"**
entry. It **may be plan-gated** (e.g. Pro / Max) and **not activatable for all
users** — there is a known GitHub issue describing exactly this
sidebar-presence-without-availability behavior. We have **not** confirmed the
behavior on this user's plan from inside the session.

**Why this is HELD.** Whether the feature is genuinely unavailable, plan-gated,
or simply not yet enabled for this account is a **user-side, client-state
question** an agent cannot resolve. Held pending the user confirming the behavior
(does clicking it activate, error, or no-op?). Cross-ref **P-10.**

**What to capture if confirming.** A screenshot of the sidebar entry, the result
of activating it, and the user's plan tier.

## F-14 — EAP & Routine billing is opaque

**Date:** 2026-06-27 · **Severity:** low (documentation request, not a defect) · **Evidence: HELD / documentation request**

**What was observed (HELD).** It is **unclear** whether EAP usage is **free or
metered**, and — separately — **how Routine usage is billed.** Routines draw on
plan usage **like sessions do** and are subject to a **daily run cap** (F-11), so
the cost model during/after a free-promo period is ambiguous: does a Routine
firing overnight consume plan usage the same way a manual session would? Does the
free promo cover Routine runs?

**Framing (deliberate).** This is a **"please clarify EAP billing" request, not a
defect claim.** Per the honesty bar we are **not** asserting that EAP is free, nor
that it is metered — both are **unconfirmed.** Cross-ref **P-9.**

**What would resolve it.** A short statement covering: (a) is EAP usage metered,
exempt, or free; (b) does that change when the private-beta promo ends; (c) how
Routine runs are counted against plan usage and the daily cap.

---

# 3. Bucket-1 — verified findings (V-1..V-7)

Findings grounded in directly-observable session/repo state — a log line, a file
path, or a git fact — so each is reproducible without an image.

### V-1 — The background-tasks panel has no per-task progress or liveness signal

**Severity:** medium (operability / trust) · **Evidence: VERIFIED**

**What happened.** During one large run there were dozens of subagents (review +
worker) in the Background-tasks panel at once. Some showed shapes that strongly
suggest they were stuck — e.g. ~5 hours elapsed with only 2–4 tool uses —
sitting in the same "Running" list as obviously-live agents (2h 47m / 35 tool
uses; 8m / 53 tool uses). There is no way to tell them apart from the panel.

**Concrete proof.** The provable, image-independent fact is the **absence of a
signal**: the panel shows elapsed time, token count, and a tool-use count per
task, but **no "last active N min ago," no heartbeat, no progress %, and no
stalled badge.** Two agents with identical "Running" status and wildly different
tool-use rates render identically. The unbounded "Finished N" pile (it reached
the high-hundreds this session) never collapses. (I do not claim the 5h agents
were definitively orphaned — only that the panel gives me nothing to decide that
with.)

**Why it matters.** The single most common question during a big fan-out is "is
this task actually working, or hung?" The panel can't answer it; an operator
either waits indefinitely or kills tasks blindly.

**Suggested fix.** Per-task liveness ("last tool use N min ago" + a stalled badge
past a threshold); a progress hint (tool-use rate) so a 2-tool-use/5h task reads
differently from a 35-tool-use one; a way to collapse/clear the Finished pile.

### V-2 — "Felt stalled" while the batch was, in fact, making heavy progress

**Severity:** medium (trust / perceived reliability) — tied to V-1 · **Evidence: VERIFIED (git)**

**What happened.** Because of V-1, the run repeatedly *felt* stalled. In reality
the batch was landing PRs the whole time.

**Concrete proof (git, this session).** Between 2026-06-24 and 2026-06-26, **20
commits landed on `origin/main`**, of which **13 carry an explicit `(#NNN)` PR
marker** — PRs #882, #883, #884, #887, #893, #894, #896, #897, #898, #900, #902,
#903, #927, #931. That is a sustained merge cadence, not a stall. Reproduce:

```
git log origin/main --since="2026-06-24 00:00" --pretty=format:"%h %ci %s"
git log origin/main --since="2026-06-24 00:00" --pretty=format:"%s" | grep -oE '\(#[0-9]+\)' | sort -u | wc -l   # → 13
```

**Why it matters.** The perception gap is the product gap: real, verifiable
throughput was happening, but the surface conveyed "stuck." Exactly the cost of
V-1's missing liveness signal.

**Suggested fix.** Surface forward-progress at the batch level — "N PRs
opened/merged this session," a live activity stream, or the per-task progress
from V-1. The data exists (commits, PR events); it just isn't reflected back.

### V-3 — Chat-pasted screenshots aren't exposed to file tools (though @-attached uploads are)

**Severity:** medium (workflow friction) — corrected/narrowed from round 1 · **Evidence: VERIFIED**

**What happened.** Round 1 said "the agent can't access user-uploaded images."
Too broad. The precise finding:

- **Images pasted directly into the chat** are visible to the model's context but
  are **not materialized to any filesystem path** the agent's Read/Write/Bash/
  Glob tools can reach. The agent cannot programmatically save, rename, or embed
  them.
- **Files @-attached as uploads ARE readable.** They land at
  `/root/.claude/uploads/<session-id>/<uuid>-<originalname>` and the Read tool
  reads them directly.

**Concrete proof.** An @-attached PNG was read this session with the Read tool:
`Read("/root/.claude/uploads/<session-id>/<uuid>-IMG_6793.png")` returned the
image successfully; `ls -la /root/.claude/uploads/<session-id>/` shows the upload
(526 KB PNG) alongside other @-attached files. So the gap is specifically the
**chat-paste path**, not uploads in general.

**Why it matters.** A natural Projects workflow is "here's a screenshot, capture
it into the report." With @-attach it works; with an inline paste it silently
doesn't, and the difference isn't discoverable — they look the same in chat.

**Suggested fix.** Materialize chat-pasted images to the same readable `uploads/`
path so paste and @-attach behave identically.

### V-4 — A single shared append-only ledger file forces parallel PRs to merge sequentially

**Severity:** medium (throughput ceiling for multi-agent batches) · **Evidence: VERIFIED (repo state)**

**What happened.** BruteX requires every PR to append an entry to one shared
decision ledger, `docs/MIGRATION.md`. When N agents open N PRs in parallel, each
edits the same file's tail, so each pair of PRs conflicts on that file. Whichever
merges first wins; the rest must rebase and re-resolve — serializing what was
meant to be parallel.

**Concrete proof (repo state).** `docs/MIGRATION.md` on `origin/main` is **6,297
lines / 267 ledger entries**, and every PR in this batch appended to it.
Reproduce: `git show origin/main:docs/MIGRATION.md | wc -l`. This is a structural
property of the workload, but it is a pattern the Projects multi-agent model will
hit anywhere a repo has a hot append-only file (CHANGELOG, ledger, registry).

**Why it matters.** The headline promise of multi-agent is parallelism. A hot
shared file quietly converts it back to sequential at merge time; the operator
only sees "why are these PRs taking turns?"

**Suggested fix (idea-adjacent, grounded).** A merge-queue-aware mode that
auto-rebases append-only conflicts (append is associative — two tail appends can
be auto-merged), or tooling to detect a hot shared file across a fan-out and warn
up front.

### V-5 — Recurring transient crates.io flake in CI (worked around, not a product bug)

**Severity:** low (CI reliability / friction) · **Evidence: VERIFIED (in repo)**

**What happened.** Rust wheel builds in CI intermittently failed at dependency
download — `download of <crate> failed / curl [56] Failure receiving data /
OpenSSL SSL_read: unexpected eof / Connection reset by peer` — aborting the build
before any test ran, flaking otherwise-green PRs.

**Concrete proof + our fix (in repo).** Hardened in `.github/workflows/ci.yml`:
`CARGO_NET_RETRY: "10"` (line ~79), `CARGO_NET_GIT_FETCH_WITH_CLI: "true"` (line
~80), plus per-step "belt-and-suspenders crates.io flake guard" retry loops
around each `pip install ./rust/*` wheel build. The root-cause comment block
documents the exact failure strings. Environmental, not a Projects defect —
flagged as context for how often the runner network flakes, since it interacts
with V-6.

**Why it matters.** Each flake cost a re-trigger cycle (see V-6). Worth the team
knowing how frequently GitHub-hosted runner networking drops mid-download.

**Suggested fix.** Nothing for Anthropic to fix in the product; noted because it
compounds V-6.

### V-6 — An agent can't re-run a failed CI check; it must push an empty commit to re-trigger

**Severity:** medium (workflow friction / wasted cycles) · **Evidence: VERIFIED (git)**

**What happened.** When CI fails on a transient (the V-5 crates.io flake, an
artifact-upload timeout, a stuck gate), the agent has no way to simply *re-run*
the failed check — the natural action a human takes from the GitHub UI. The only
workaround is to push an **empty/no-op "re-trigger" commit** to force the
workflow to run again.

**Concrete proof (git, across this session's branches).** There are **48
`chore(ci): re-trigger …` commits** across the session's refs, 6 of them
specifically for the crates.io flake. Reproduce:
`git log --all --oneline --grep="re-trigger" | wc -l` → 48. The workaround is
even codified in our own `ship-pr` skill ("re-trigger (empty commit) at most a
couple of times").

> **Reinforced this round (cross-ref F-8).** The broker-side 403 (F-8) **also
> blocks re-running CI via the GitHub API** — so during the push-only state there
> is no API path to retry a flaky check at all, only the empty-commit workaround.
> This is exactly the situation that produced the #905 CI failure investigation:
> the failing run could not be re-run from inside the session.

**Why it matters.** Empty commits pollute history, churn the branch, restart the
*entire* workflow (not just the failed job), and burn agent turns. A human clicks
"Re-run failed jobs"; the agent can't.

**Suggested fix.** Give the agent a first-class "re-run this check / re-run failed
jobs" capability (the GitHub `rerun` API surface), so transient failures don't
require history-polluting empty commits.

### V-7 — Worktree stale-base trap exists and is correctly guarded (a POSITIVE)

**Severity:** n/a — this one works; included as a "keep doing this" · **Evidence: VERIFIED (in repo)**

**What happened.** With many agents on many worktrees against a fast-moving
`main`, a feature branch can fall behind and a merge over a stale base would be
unsafe — a real hazard in the multi-worktree model.

**Concrete proof (it's guarded).** The auto-merge workflow refuses to merge
unless `mergeable_state` is clean — it explicitly rejects conflict / stale-base /
blocked states: `.github/workflows/auto-merge.yml:411` —
`"refusing to merge (conflict / stale base / blocked)"`, backed by a red-team
regression test `tests/redteam/test_redteam_automerge_safety.py`.

**Why it matters.** This is the safety property that *should* exist in a
multi-agent merge pipeline, and it's enforced + regression-tested. Called out so
the pattern is recognized and preserved as Projects' own merge tooling matures.

---

# 4. Bucket-2 — ideas (clearly labelled IDEAS, no claim of proof)

Not defects and no claim of proof — directions that would have helped this batch.

- **I-1 — A structured project-state MCP.** Today an orchestrator re-derives state
  by running `git status` / `git worktree list` / diffing branches every time. An
  MCP exposing project state as structured fields (open PRs, per-branch status,
  per-worktree dirty flags, current ledger head) would let an agent read state in
  O(1) instead of reconstructing it. We'd happily prototype against it.
- **I-2 — An agent-invocable routine/scheduling tool.** A first-class,
  agent-callable "run this prompt on a schedule / after condition X" primitive so
  a batch can self-pace ("re-check CI in 10 min," "resume when PR #N merges")
  instead of busy-waiting or burning a turn.
- **I-3 — A per-session base-state header.** A cheap, authoritative "verified
  current state of these files/branches" snapshot attached at sub-task dispatch,
  so a worker confirms its premise in one check rather than spending a full turn
  discovering a stale handoff.
- **I-4 — An up-front cost estimate for large fan-outs.** Before launching N
  subagents, an estimated token/cost envelope so the operator can make an
  informed go/no-go.
- **I-5 — Correlate the same infra error across subagents into one alert.** When
  the V-5 crates.io flake hits several wheel-build jobs at once, surface it as one
  "transient infra error affecting N tasks" notice instead of N separate failures.

## Questions for Omid (3)

1. **Routine/scheduling:** Is an agent-invocable routine/scheduling tool (I-2) on
   the roadmap? It's the single capability that would most change how we structure
   long multi-agent batches.
2. **Structured state MCP:** Is a structured project-state MCP (I-1) planned? If
   so, we'd like to prototype BruteX's orchestration against it and give early
   feedback.
3. **Billing:** Is EAP / Projects usage metered against the plan, exempt, or free?
   We run heavy multi-agent fan-outs and want to reason honestly about cost
   (relevant to I-4, P-9, and F-14).

---

# 5. Original evidence gallery (F-1..F-7)

The round-1/round-2 evidence-gallery findings, observed during one real 11-PR
"critical-risks resilience-hardening" batch driven concurrently in a single
Projects session. F-1/F-2 are the earlier framings later refined by V-1/V-3;
F-3..F-7 are grounded in directly-observable session/worktree state.

### F-1 — Background-tasks panel lacks per-task progress/liveness; orphaned subagents accumulate invisibly

**Date:** 2026-06-26 · **Severity:** medium · **Evidence: VERIFIED** · **Proof:** screenshots `01`, `02`

In the "Running" section, several review subagents ran ~5 hours each while
reporting only 2–4 tool uses and ~70–90k tokens:

- Altitude review of PR #887 — 5h 01m · 79.6k tokens · 3 tool uses
- Reuse review of PR #888 docs — 5h 00m · 89.7k tokens · 4 tool uses
- Simplification review of PR #888 docs — 5h 00m · 72.2k tokens · 2 tool uses
- (plus several more #888-docs review agents, all ~5h)

For contrast, clearly-live agents in the same list show very different shapes:
"Hostile review of nav-latency caches — 2h 47m · 150.7k tokens · 35 tool uses"
and "Diagnose+fix #893 citation scan — 8m · 177.9k tokens · 53 tool uses." A
"Finished 294" counter sits at the bottom.

**The gap.** A 5-hour agent with 2 tool uses is almost certainly orphaned/stuck,
but the panel offers **no per-task progress or liveness signal** — no heartbeat,
no "last active N min ago," no progress %, no auto-reap when the PR work it was
reviewing has completed. Stuck agents are visually indistinguishable from working
ones, and finished/orphaned tasks accumulate unbounded (294 finished). This is
the screenshot evidence behind **V-1.**

**Suggested fixes.** Per-task liveness signal ("last tool use N min ago" / stalled
badge); a progress hint (tool-use rate); auto-reap or flag subagents whose parent
PR work has finished; let the user collapse/clear the Finished pile.

### F-2 — Agent file tools cannot access user-uploaded images in a Projects session

**Date:** 2026-06-26 · **Severity:** medium · **Evidence: VERIFIED (round-1 framing; narrowed by V-3)**

The user shared screenshots and asked the agent to save and embed them. The agent
can *see* the images in context, but in the round-1 observation they were never
written to any filesystem path the agent's tools could reach. **Narrowed by
V-3:** the precise gap is the **chat-paste path** — @-attached uploads ARE
readable at `/root/.claude/uploads/<session-id>/...`. See V-3 for the corrected,
verified statement.

**Suggested fix.** Expose user-shared images to the session's file tools (a
readable `attachments/` path, or a tool to materialize a pasted image to disk).

### F-3 — Worktree-per-agent isolation leaves orphaned worktrees that share one branch name

**Date:** 2026-06-26 · **Severity:** medium · **Evidence: VERIFIED (reproducible from `git worktree list`)**

Driving the 11-PR batch with isolated worker agents produced **16 live git
worktrees** under `.claude/worktrees/`, of which **13 carry the same `…-jlzt1n`
branch suffix** and several point at the *identical* branch name (e.g. three
worktree dirs all on `claude/writer-columnar-4a-jlzt1n`; two each on
`claude/dashboard-latency-jlzt1n` and `claude/pull-health-monitor-jlzt1n`). Some
worktrees still hold staged-but-uncommitted edits to files unrelated to their
nominal task.

**Why it matters.** A new session (or human) opening the repo cannot tell which
worktree is "live" for a branch, which are abandoned, or which carry stray staged
changes. Multiple worktrees on one branch name also invite cross-contamination.

**Suggested fixes.** GC (or clearly mark stale) worktrees whose agent has
finished/been reaped; guarantee one worktree ↔ one branch (or surface which is
canonical); surface per-worktree dirty/staged state.

### F-4 — Lost work: a sub-agent's uncommitted output can silently disappear between sessions

**Date:** 2026-06-26 · **Severity:** high (data loss / trust) · **Evidence: VERIFIED**

A follow-up was launched on the premise that prior agents had left uncommitted
findings + a refreshed scoreboard in the working tree. An exhaustive search —
every one of the 16 worktrees, `git stash`, and every `origin/*` branch — found
**no trace** of that content. The earlier agents' additional output was never
committed and is gone.

**Why it matters.** Isolated worktrees + reaped agents mean anything a sub-agent
produces but does not *commit and push* can evaporate when its worktree is
cleaned or its session ends — with no warning to the orchestrating session, which
then plans against output that no longer exists.

**Suggested fixes.** Treat a sub-agent's working tree as durable until explicitly
discarded, or auto-commit/auto-stash its diff to a recoverable ref on reap; when
an orchestrator hands a "build on the prior agent's uncommitted changes"
follow-up, verify those changes still exist and warn if not; make "uncommitted
work will be lost on teardown" a visible state.

### F-5 — No cross-session view of cost/usage accrued by a multi-agent batch

**Date:** 2026-06-26 · **Severity:** medium (cost observability) · **Evidence: VERIFIED**

The background-tasks panel shows per-task token counts (150.7k, 177.9k, 89.7k…)
but there is **no rolled-up total** for the batch and no per-session/per-day usage
figure. With dozens of subagents — including the ~5h orphans still "running" —
the operator cannot see what an 11-PR batch cost, nor whether the orphans are
still burning budget.

**Why it matters.** Heavy runs consume the operator's plan usage, and whether EAP
usage is metered is itself unconfirmed (F-14). Without an aggregate cost view, an
operator can't make an informed "continue?" call.

**Suggested fixes.** Show a batch/session/daily token+cost rollup; flag tasks
accruing tokens while making no tool-use progress (ties to F-1); clarify the EAP
billing policy (F-14).

### F-6 — Resuming an agent with a stale premise wastes a full turn before the mismatch surfaces

**Date:** 2026-06-26 · **Severity:** low–medium · **Evidence: VERIFIED**

A resumed/spawned agent is handed a brief that assumes a specific prior state
("the working tree has uncommitted findings"). When that premise is false (F-4),
the agent must spend a full investigative turn — listing worktrees, diffing
branches, searching stashes and `origin/*` — just to *discover* the premise is
wrong, before doing the actual work. There is no cheap, authoritative "what is the
current state of these files?" handoff.

**Why it matters.** Multi-agent orchestration relies on accurate state handoffs. A
stale premise is caught only after the worker burns tokens reconstructing reality
— compounding F-4 and F-5.

**Suggested fixes.** Let an orchestrator attach a verified state snapshot (file
hashes / `git status`) to a sub-task (ties to I-3); surface at dispatch whether
the referenced files/branches actually exist in the target worktree.

### F-7 — Branch/worktree naming collisions make "which PR is this?" ambiguous in the UI

**Date:** 2026-06-26 · **Severity:** low · **Evidence: VERIFIED**

Across the batch, the scoreboard's PR numbers and the decision-ledger entries
(`docs/MIGRATION.md #NNN`) use two numbering schemes that overlap in range, while
several background tasks are labelled only by a short title ("Reuse review of PR
#888 docs") with no link back to a branch/worktree/commit. Combined with F-3's
many-worktrees-one-branch state, it is hard to map a running review task → its
branch → its eventual PR → its ledger entry.

**Why it matters.** When a reviewer task is stuck (F-1) or its work is lost (F-4),
the first question is "which PR/branch/commit was it touching?" — and that
requires manual cross-referencing.

**Suggested fixes.** Link each background task to its branch + worktree +
(eventual) PR/commit, not just a free-text title; let tasks carry both the GitHub
PR number and an internal ledger number so they don't get conflated.

---

# 6. Screenshot map

Each screenshot the user has → the finding(s) it backs. Image files are not in
the repo; `(attach: <name>)` are placeholders for the email/submission. Several
findings need **no** screenshot — they are carried by verbatim text, doc
citations, or git facts (see §7).

| Screenshot | `(attach: …)` | Backs | Status |
|---|---|---|---|
| Background-tasks panel — orphaned ~5h / 2–4-tool-use review agents | `(attach: 01-background-tasks-orphaned-agents.png)` | **V-1 / F-1** | shared in chat; not on disk — drop into `evidence/` |
| Background-tasks panel — live high-tool-use agents next to stuck ones; "Finished 294" | `(attach: 02-background-tasks-running-5h.png)` | **V-1 / F-1 / F-5** | shared in chat; not on disk — drop into `evidence/` |
| Overnight stranded push-only state (the in-session 403) | `(attach: overnight-stuck-403.png)` | **P-1 / P-2 / P-11 / F-8** | operator has it; strengthens (text-sufficient) |
| Correct GitHub-App install (`@SJParthi`, All-repos, read/write) | `(attach: github-app-install.png)` | **P-2 / F-8** | operator has it; strengthens (text-sufficient) |
| Effort tier BEFORE long idle (= max) | `(attach: effort-before-idle-max.png)` | **P-12 / F-9** | **still needed** — required to move F-9 HELD → VERIFIED |
| Effort tier AFTER long idle (= "Extra") | `(attach: effort-after-idle-extra.png)` | **P-12 / F-9** | **still needed** — the second half of the F-9 pair |

**The one still-needed capture:** the **F-9 effort before/after pair**
(`effort-before-idle-max` + `effort-after-idle-extra`). F-9 stays UNVERIFIED
until this pair is captured **and** the ultracode-mode-vs-Extra-effort-tier
disambiguation is resolved (see F-9). Everything else is either already-shared or
text-sufficient.

> **Do not fabricate placeholder images.** The gallery rows are written and ready
> to receive the real files; drop the operator's screenshots into
> `docs/eap-feedback/evidence/` under the names above.

---

# 7. Evidence integrity note

Every claim in this submission is labelled. Three tiers:

### A. Text-proven — no screenshot needed
These stand on a verbatim quote, a doc citation, or a git/repo fact that anyone
can reproduce:

- **F-8 / P-1 / P-2** — the **verbatim 403 body**, the broker-vs-GitHub origin
  analysis (`docs.anthropic.com` URL · 403-not-401 · missing GitHub headers ·
  empty proxy relay state), the personal-account/App-already-connected
  contradiction, the re-test-after-install, and the push-works-while-API-403s
  channel divergence. **Independently corroborated this session** by a live
  `curl` to the GitHub Actions API returning the byte-identical broker 403.
- **V-2** — git commit/PR counts (`git log origin/main --since=… | grep -oE
  '\(#[0-9]+\)' | sort -u | wc -l` → 13; 20 commits total).
- **V-3** — the exact readable upload path
  (`/root/.claude/uploads/<session-id>/...`) demonstrated with a real `Read`.
- **V-4** — `docs/MIGRATION.md` line count (`git show
  origin/main:docs/MIGRATION.md | wc -l` → 6,297).
- **V-5 / V-6** — `.github/workflows/ci.yml` flake-guard lines + the
  `git log --all --grep="re-trigger" | wc -l` → 48 count.
- **V-7** — `auto-merge.yml:411` refusal string + the named regression test.
- **F-3 / F-4 / F-6 / F-7** — reproducible from `git worktree list`, branch/stash
  search, and the on-disk worktree layout.
- **F-11** — quoted directly from the published Routines docs (1-hour minimum
  interval + daily run cap).
- **F-12** — the typosquat wheel name/version/metadata are observable from the
  managed index.

### B. Strengthened by a screenshot (verified by text; image adds weight)
- **V-1 / F-1 / F-5** — backed by screenshots `01`/`02`, but the *core claim* (the
  panel exposes no liveness/progress/heartbeat/stalled signal) is true
  independent of the image. The screenshots make the contrast vivid.
- **P-1 / P-2 / P-11 / F-8** — the operator's overnight-stuck + GitHub-App-install
  screenshots strengthen the auth-lapse story, which is already text-proven by the
  verbatim 403 + re-test evidence.

### C. HELD / UNVERIFIED — explicitly NOT presented as fact
- **F-9 / P-12 (effort-revert)** — **UNVERIFIED.** Needs (a) the ultracode-vs-
  Extra disambiguation and (b) a 2–3× before/after reproduction. An agent cannot
  inspect client-side UI state. **Do not present as a defect** until both gates
  pass.
- **P-9 / F-14 (billing)** — **HELD.** A documentation/clarification request;
  neither "free" nor "metered" is asserted.
- **P-10 / F-13 (Dispatch)** — **HELD.** Open question; plan-gating unconfirmed.
- **P-11 (overnight disruption specific to the beta)** — **HELD.** A
  user-reported correlation only; **no causation claimed**; root cause
  unconfirmed (no telemetry to compare the run paths).

**One excluded item (honesty, by design).** The fabricated `httpx2` **test stub**
a prior agent created inside *our* repo is **deliberately excluded** from this
submission — it was **our** code, it has been handled internally, and it is **not
a Claude Code product defect.** It must not be conflated with **F-12**, which is
the separate, genuine product finding that the *managed environment's package
index* serves an installable `httpx2` typosquat. (The stub is, however, a useful
reminder that autonomous agents can produce convincing-but-fake "green" results —
which is exactly why this submission labels every claim and leans on independent,
adversarial verification rather than face-value "it works" claims.)

---

*Prepared 2026-06-27. Every fact above is reproducible from this repo and
session; every unproven item is labelled HELD or UNVERIFIED per the project's
verify-before-claim bar (CLAUDE.md §2 Golden Rule #10).*
