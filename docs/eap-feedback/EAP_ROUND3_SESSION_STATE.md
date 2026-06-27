# EAP Round 3 — Session-state loss, undocumented surfaces, and supply-chain hazard

> **Meta-feedback about Claude Code** (the Anthropic coding agent / "Claude
> Code Projects" Early Access Program), gathered while building the BruteX
> backtesting engine. **This is NOT BruteX product code, spec, or engine
> documentation** — it describes the *tool* used to build BruteX, not BruteX
> itself.
>
> Round-3 dossier. Builds on the round-2 findings (`evidence/EVIDENCE.md`,
> F-1..F-7) and the round-1 follow-up (`EAP_REPLY_ROUND1_FOLLOWUP.md`). Same
> conventions: each finding carries a date, a severity, and an explicit
> evidence label — **VERIFIED** / **HELD** / **UNVERIFIED** — per the
> project's no-illusion, verify-before-claim bar (CLAUDE.md Golden Rule #10).
> Nothing unproven is presented as fact.

**Themes.** This round spans three distinct themes, all observed in one real,
large multi-agent session building BruteX:

1. **Silent session-state loss** — an unattended, long-running session can lose
   or reset state mid-run with no signal to the user (F-8 GitHub authorization;
   F-9 the held reasoning-effort observation). Because the value proposition is
   *running unattended overnight to completion*, a silent state change is
   especially costly: the operator only learns of it after the fact, when work
   has already been blocked or degraded.
2. **Undocumented / under-documented surfaces** — prominent UI affordances
   ("Projects", "Dispatch (Beta)") with no matching public docs, and automation
   primitives (Routines) whose limits constrain real-time loops (F-10, F-11,
   F-13).
3. **Supply-chain & billing hygiene** — a typosquat wheel installable from the
   managed environment's package index (F-12), and opacity around EAP / Routine
   billing (F-14).

Findings are labelled honestly: **VERIFIED** where reproduced/evidenced this
session, **HELD** where they need a user-side confirmation before they can be
called defects.

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

**New evidence — the 403 originates in the Anthropic broker, not GitHub
(VERIFIED).** The response is **not** a GitHub-native rejection; it is the
Claude/Anthropic GitHub *broker* refusing to mint a downstream call:

- The body's `documentation_url` points at **`docs.anthropic.com`**
  (`.../claude-code/github-actions`), not at `docs.github.com`. GitHub's own
  API errors always cite GitHub docs.
- The status is **HTTP 403**. GitHub's native bad-credential failure is **HTTP
  401 "Bad credentials"** with a GitHub-format body — a different code and
  shape entirely.
- The response carries **none of GitHub's signature headers**
  (`x-github-request-id`, `x-ratelimit-*`). A real GitHub reply, even a
  rejection, always includes them.
- The agent proxy's own `/__agentproxy/status` carries **no GitHub
  token/expiry state at all**, and `recentRelayFailures` was **empty** — so the
  proxy never even attempted (and failed) a GitHub relay. The refusal happens
  upstream of the relay, at the broker's enablement gate.

Together these establish the failure is a **broker-side session-enablement
gate**, not an expired or rejected GitHub credential.

**New evidence — the 403 message is misleading (VERIFIED).** The message says
*"an org admin must connect the Claude GitHub App for this organization."* But:

- This is a **personal account** (**SJParthi**), **not an organization** — there
  is no "org admin" in the loop. The message asks the user to take an action
  that does not apply to their account type.
- The App was **demonstrably connected** — the same session used it to open PRs
  **#896–#905** earlier in the run. So "must connect … for this organization"
  is factually wrong at the moment it is shown: the App *was* connected.

The message therefore **misattributes a session-enablement lapse** ("this
session's GitHub access was not enabled / has dropped") as a **"never connected
/ needs an org admin"** problem — pointing the user at the wrong fix.

**New evidence — installing the App did NOT recover the running session
(VERIFIED).** After the user did the *correct* thing — installed the Claude
GitHub App on **@SJParthi**, scope **All repositories**, **read/write** — this
**already-running** session **still returned the same 403** when the API was
re-tested (re-tested **twice** after the install). This directly proves that
GitHub access is **bound at session start**: a newly-installed/restored grant
**cannot** be picked up by a session that is already running. Only a **fresh
session** can.

**New evidence — the two channels diverge (VERIFIED).** Throughout the 403
state, `git push` over the session's own git credential channel **kept
working**. So the agent was left **half-functional**: push **yes**, but
PR/merge/API **no**. The git surface and the REST-API surface authorize
independently, and only one of them lapsed — which is exactly why the failure is
easy to miss (commits keep landing; only the PR step fails).

**Root behavior (VERIFIED).** The GitHub grant is read **at session start** and
is bound for the life of that session. Re-connecting / re-authorizing /
*installing* the GitHub App mid-run does **not** propagate to the
already-running session (proven above by the re-test-after-install) — picking up
the restored grant requires starting a **new session**. So once the grant
lapses mid-run, the running session is stuck in a push-only state with no
in-session path back to full GitHub access.

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
- **Allow in-session re-auth**: let a restored/just-installed GitHub grant
  propagate into the running session so it does not require a full restart to
  recover (the re-test-after-install above shows it currently does not).
- Failing that, **auto-checkpoint and resume in a fresh session** so the
  unattended loop survives the auth lapse without manual intervention.
- **Fix the message text.** Don't tell a personal-account user that "an org
  admin must connect the App for this organization" — detect the account type
  and the already-connected state, and say what is actually true: *"this
  session's GitHub access was not enabled; start a new session (or re-enable)
  to recover."* The current wording sends users to the wrong remedy.
- **Distinguish broker-gate from credential errors** in the surfaced message
  (the body's `docs.anthropic.com` URL + 403 + missing GitHub headers identify
  it as a broker-side gate, not a GitHub rejection).

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

## F-10 — "Projects" is prominent in the UI but undocumented, producing a wrong mental model

**Date:** 2026-06-27
**Severity:** medium (onboarding / expectation-setting)
**Evidence label: VERIFIED** (docs-silent + evidenced user confusion)

**What was observed.** The Claude Code UI **prominently surfaces a "Projects"**
item, and the Early Access Program itself is referred to as "Claude Code
Projects." Yet the **official documentation has no "Projects" page**: the
documented first-class objects at `code.claude.com/docs` (and its `llms.txt`
index) are **Sessions, Environments, and Routines** — "Projects" appears
nowhere as a documented concept.

This gap produced a **sustained, wrong mental model**: the user reasonably
expected a "Project" to be a **persistent, auto-running container** — something
that lives on and does work on its own, *avoiding* the need to think in terms of
sessions. The documented reality is the opposite: work runs as **sessions**, and
**unattended automation requires a Routine / trigger**. The mismatch between a
prominent, named UI surface and its total absence from the docs is what drove
the misunderstanding.

**Why it matters.** A prominent beta feature with **no documentation** leads
users to form incorrect expectations about **persistence and automation** —
precisely the expectations that matter most for an "unattended overnight" tool.
A user who thinks "I have a Project, so it keeps running" will not set up the
Routine they actually need.

**Honest caveat.** "Projects" may simply be a **newer beta surface not yet
documented** — this finding is **"undocumented, causing confusion," NOT
"Projects don't work."** We are not claiming the feature is broken; we are
reporting that its absence from the docs created a real, evidenced
misunderstanding this session.

**Suggested fixes (for the beta team).**
- **Document what a Project is and is not** — its lifecycle, what persists, and
  the relationship between a Project, its Sessions, Environments, and Routines.
- **Clarify the execution model explicitly**: work still runs as **sessions**,
  and **unattended automation needs a Routine / trigger** — a Project alone does
  not keep running on its own.

---

## F-11 — Routine limits constrain frequent / real-time automation

**Date:** 2026-06-27
**Severity:** low–medium (automation latency ceiling)
**Evidence label: VERIFIED (from docs)**

**What was observed.** Per the published Routines documentation
(`https://code.claude.com/docs/en/routines`), a Routine's **minimum schedule
interval is one hour** — **sub-hourly cron is rejected** — and there is a
**daily per-account run cap**. These two limits together put a floor on how
frequently an unattended loop can fire.

**Why it matters.** Some real automation needs **lower latency than one hour** —
e.g. driving CI every few minutes, polling a long-running job for completion, or
reacting promptly to an external event. With a one-hour floor and a daily cap, a
tight unattended loop simply cannot be expressed as a Routine; the operator must
either fall back to a foreground session (defeating "unattended") or accept
hour-granularity reactions.

**Suggested fixes (for the beta team).**
- **Allow sub-hourly intervals** for short / bounded jobs, even if gated behind
  a stricter quota.
- Or **document event-driven triggers** as the supported lower-latency
  alternative to polling Routines, so users reach for the right primitive
  instead of trying to make a 1-hour Routine behave like a 1-minute one.

---

## F-12 — A typosquat `httpx2` wheel is installable from the managed env's package index

**Date:** 2026-06-27
**Severity:** high (supply-chain hazard)
**Evidence label: VERIFIED (security-relevant)**

**What was observed.** The **managed environment's package index** serves a
wheel named **`httpx2` version `2.5.0`** whose metadata **cribs the real
`httpx` project's** — it advertises itself as a *"next generation HTTP client"*
and reuses authorship signals associated with the genuine `httpx`
(Tom Christie / Pydantic-adjacent). The name `httpx2` is a one-character /
suffix variant of the widely-used `httpx`. In shape this is a **classic
typosquat**: a near-name package, dressed in the real package's description, made
**installable** from the index the environment trusts.

**Scope note (important — distinct from an internal issue).** This is **NOT**
the same thing as our own repository's fabricated test stub. That stub was an
*internal* artifact a prior agent created in **our** repo, it is **our** issue,
it has already been handled, and it is **out of scope for EAP feedback** (see
"Considered but excluded" below). **This finding is specifically that the
managed ENVIRONMENT's package index itself serves an installable typosquat** —
a property of the Claude Code environment, not of BruteX.

**Why it matters.** An installable typosquat in the managed index is a
**supply-chain hazard**: a developer (or an autonomous agent) who fat-fingers
`httpx2`, or who is steered toward it by a poisoned suggestion, can **silently
substitute** a look-alike package for a real dependency — and because the
metadata mimics the genuine project, the substitution is hard to spot in a
review. Agents installing dependencies non-interactively are especially exposed.

**Suggested fixes (for the beta team).**
- **Scan and block typosquat-shaped packages** in the managed index — name-edit
  distance to popular packages, plus metadata that copies a different project's
  description/authorship, are strong signals.
- At minimum, **warn loudly** when an install resolves to a near-name of a
  high-popularity package.

---

## F-13 — "Dispatch (Beta)" is shown in the sidebar but may not be activatable

**Date:** 2026-06-27
**Severity:** low (clarity / availability)
**Evidence label: HELD — needs user confirmation**

**What was observed (HELD).** The Desktop sidebar shows a **"Dispatch (Beta)"**
entry. It **may be plan-gated** (e.g. Pro / Max) and **not activatable for all
users** — there is a known GitHub issue describing exactly this
sidebar-presence-without-availability behavior. We have **not** confirmed the
behavior on this user's plan from inside the session.

**Why this is HELD, not reported.** Whether the feature is genuinely
unavailable, plan-gated, or simply not yet enabled for this account is a
**user-side, client-state question** an agent cannot resolve. It is held pending
the **user confirming the behavior on their plan** (does clicking it activate,
error, or no-op?).

**What to capture if confirming.** A screenshot of the sidebar entry plus the
result of activating it, and the user's current plan tier.

---

## F-14 — EAP & Routine billing is opaque

**Date:** 2026-06-27
**Severity:** low (documentation request, not a defect)
**Evidence label: HELD / documentation request**

**What was observed (HELD).** It is **unclear** whether EAP usage is **free or
metered**, and — separately — **how Routine usage is billed**. Routines draw on
plan usage **like sessions do** and are subject to a **daily run cap** (F-11), so
during or after a free-promo period the cost model is ambiguous: does a Routine
firing overnight consume plan usage the same way a manual session would? Does
the free promo cover Routine runs?

**Framing (deliberate).** This is a **"please document EAP billing" request, not
a defect claim.** Per the project's honesty bar we are **not** asserting that EAP
is free, nor that it is metered — both are **unconfirmed**. We are asking for the
policy to be documented so operators can reason about heavy multi-agent fan-out
and scheduled Routines.

**What would resolve it.** A short doc statement covering: (a) is EAP usage
metered, exempt, or free; (b) does that change when the private-beta promo
period ends; (c) how Routine runs are counted against plan usage and the daily
cap.

---

## Reinforcements to round-2 findings

Fresh evidence this session that **strengthens existing round-2 findings**
(`evidence/EVIDENCE.md`, F-1..F-7). These are not new findings — they are
additional corroboration:

- **Liveness / felt-stalled during long runs (reinforces F-1).** During the long
  multi-agent run this session, the same "is it actually working or stuck?"
  uncertainty recurred: long-running agents with no per-task liveness/progress
  signal are indistinguishable from hung ones. Reinforced.

- **Shared single-file ledger forces serialized merges (reinforces F-3 / F-7
  workspace-and-merge friction).** Every PR in this batch edits the **one**
  `docs/MIGRATION.md` decision ledger. With **8 branches active this session all
  editing `MIGRATION.md`**, parallel branches **cannot merge independently** —
  each must rebase onto the others' ledger edits, serializing what is otherwise
  parallelizable work and producing repeated, avoidable rebase conflicts on a
  single file. (This is partly a *project* convention, but it is amplified by the
  multi-branch fan-out the tool encourages, and it is the dominant merge-friction
  source this session.)

- **403 blocks API-driven CI re-run (reinforces F-8, this round).** The same
  broker-side 403 (F-8) also blocks **re-running CI via the GitHub API** — so an
  unattended loop that wants to retry a flaky check has no API path to do it
  while the session is in the push-only state. Reinforced this session.

---

## Considered but excluded

- **The fabricated `httpx2` TEST STUB (excluded — our repo, already fixed).**
  Earlier this session a prior agent created a fake `httpx2` **test stub** inside
  **our** repository that caused tests to falsely report "tests pass." That is an
  **internal BruteX issue**, it has **already been fixed (#941)**, and it is
  **deliberately excluded from EAP feedback** because it is **not a Claude Code
  product defect** — it was our code. (Do not conflate it with **F-12**, which is
  the separate, genuine product finding that the *managed environment's package
  index* serves an installable `httpx2` typosquat.)

- **Cautionary observation (non-defect).** That internal stub is, however, a
  useful reminder of a real risk class: **autonomous agents can produce
  convincing-but-fake "green" results** — a passing test suite that passes only
  because the agent faked the thing under test. This is **not filed as a defect**
  (it's a property of agentic workflows, not a Claude Code bug), but it
  underscores the value of **independent, adversarial verification** of an
  agent's "it works / tests pass" claims rather than trusting them at face value.

---

## Status

- **F-8 (GitHub App authorization lapse):** **ready to send** — **VERIFIED**,
  strengthened this round with broker-vs-GitHub origin proof (`docs.anthropic.com`
  URL, 403-not-401, missing GitHub headers, empty proxy relay state), the
  misleading-message evidence (personal account, App already connected), the
  install-did-not-recover-running-session re-test, and the push-works-but-API-403
  channel divergence.
- **F-10 (Projects undocumented):** **ready to send** — **VERIFIED**
  (docs-silent + evidenced confusion); framed as "undocumented, causing
  confusion," not "broken."
- **F-11 (Routine interval/cap limits):** **ready to send** — **VERIFIED**
  (from docs: 1-hour minimum interval + daily cap).
- **F-12 (`httpx2` typosquat in the managed index):** **ready to send** —
  **VERIFIED** (security-relevant); explicitly distinguished from our internal,
  already-fixed test stub.
- **F-9 (effort-tier revert):** **held** — **UNVERIFIED**; must first resolve
  the ultracode-mode vs Extra-effort-tier disambiguation, then reproduce 2–3×
  with before/after screenshots, before it can be reported as a defect.
- **F-13 (Dispatch (Beta) availability):** **held** — needs the user to confirm
  the activate/error/no-op behavior on their plan.
- **F-14 (EAP & Routine billing opacity):** **held** — documentation request,
  not a defect; awaiting (or requesting) a billing-policy statement.
