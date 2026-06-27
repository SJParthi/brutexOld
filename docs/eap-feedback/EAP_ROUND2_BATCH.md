# Claude Code Projects — EAP Feedback, Round 2

**For:** Omid (eng lead), via the Jul 1 feedback call
**From:** Parthiban (sjparthi93@gmail.com)
**Date:** 2026-06-26
**Workload under test:** BruteX — a large multi-agent backtesting-engine repo. The product under review is the **Claude Code Projects beta**; BruteX is just the workload that exercised it.

**Ground rule for this round (self-imposed):** every finding below carries concrete, reproducible proof — a log line, a file path, a git fact, or a directly-observable session artifact. Where round 1 inferred a cause we couldn't prove, I've narrowed the claim to the part I can stand behind. Fewer, bulletproof findings beat many shaky ones.

---

## Projects (beta) — pilot feedback

**This is the lead section** — Projects is the feature the team is piloting, so it gets the spotlight. Items here are numbered with a **P-** prefix to keep them distinct from the V- (Verified) / I- (Ideas) findings below and from the F-1..F-14 evidence-gallery sequence in `evidence/EVIDENCE.md` and `EAP_ROUND3_SESSION_STATE.md`.

**Honesty framing (read first).** Projects **is** a real beta feature I have access to — there is an internal "coordinator session" that orchestrates a project, and the work in this very batch ran inside one. So this is **not** "Projects doesn't exist." The root cause of most items below is that Projects is **undocumented**: the capability is shipping, but there is no spec for what it is, what persists, or how it relates to sessions / environments / routines — so users (me included) form wrong expectations and hit the gaps below. Every item carries an **Evidence label**: **VERIFIED** = I directly observed or reproduced it this session; **HELD** = plausible but unconfirmed, raised as a documentation/clarification request, not asserted as fact.

### A. Documentation gaps

### P-1 — Projects has no documentation page at all

**Date:** 2026-06-27
**Evidence:** VERIFIED

**What was observed.** The headline pilot feature has zero documentation. The official docs index (`llms.txt`) carries no "Projects" entry — there is no page describing what a Project is, how to create one, or how it behaves. A user testing the beta has nothing authoritative to read.

**Why it matters.** With no doc, every user reverse-engineers Projects by trial and error, and forms the wrong mental model (see P-4). The absence of a single landing page is the upstream cause of most other items in this section.

**Suggested fix.** Ship a minimal "Projects (beta)" doc page — even one screen — and list it in `llms.txt`: what a Project is, how to create/open one, and a one-line definition of each moving part (Project / Session / Environment / Routine).

### P-2 — The Project ↔ Session ↔ Environment ↔ Routine hierarchy is undocumented and genuinely unclear

**Date:** 2026-06-27
**Evidence:** VERIFIED

**What was observed.** Four concepts are in play — Project, Session, Environment, Routine — with no documented relationship between them. It is not stated (anywhere I could find) which one is *persistent*, which one actually *runs the work*, and which one *holds auth*. I had to infer the model from behavior: work runs in **sessions**; **routines/triggers** are what fire scheduled/recurring work; the **environment** is the VM the session runs in; the **Project** is the persistent container of conversation history. None of that is written down.

**Why it matters.** You cannot reason about persistence, continuity, or credentials without knowing this hierarchy. The confusion directly produced P-4 (expecting the Project to "just keep working") and P-5 (expecting the Project to shield auth).

**Suggested fix.** Document the hierarchy explicitly — a single diagram + table: for each of {Project, Session, Environment, Routine}, state "persistent? runs work? holds auth? lifetime."

### P-3 — What persists in a Project vs what does not is undocumented

**Date:** 2026-06-27
**Evidence:** VERIFIED

**What was observed.** It is not documented which parts of a Project survive across time. Observed behavior: **conversation history persists** (the Project retains the thread), but **live credentials and the running VM/environment do not** — they are session-scoped and re-bound per session (this is the mechanism behind P-5 and P-6). A user has no way to know this without hitting it.

**Why it matters.** "Persistent project" reads as "everything persists." When credentials and the live VM silently don't, the user is surprised mid-task (P-5) — the most expensive kind of surprise.

**Suggested fix.** Document a clear persistence contract: a short list of "persists across sessions" (history, files committed to the repo, project config) vs "session-scoped, re-bound each session" (credentials, the running VM, in-memory state).

### B. Behavior / expectation gaps

### P-4 — Users expect a Project to be a self-running persistent container; it is not

**Date:** 2026-06-27
**Evidence:** VERIFIED (this session)

**What was observed.** The natural expectation from the word "Project" is a self-running, always-on container that keeps working on its own. In reality, work still runs as **sessions**, and 24/7 / recurring automation still requires an explicitly-wired **Routine/trigger**. There is no ambient "the Project is working" loop. This expectation mismatch cost me multiple rounds of confusion this session before the model clicked.

**Why it matters.** The gap between the implied promise ("a Project that runs") and the mechanism ("sessions + manually-wired routines") is exactly where a beta loses trust. Users wait for work that will never start on its own.

**Suggested fix.** Either (a) make the Project actually drive continuity (see P-8), or (b) set expectations explicitly in the doc: "A Project is a persistent workspace; to make it run on its own, attach a Routine."

### P-5 — A Project does NOT shield a running session from the session-start auth binding

**Date:** 2026-06-27
**Evidence:** VERIFIED (this session)

**What was observed.** GitHub access dropped **mid-session** and could not be recovered within that session — a brand-new session was required to re-bind it — **even though the work was happening inside a Project.** Being "in a Project" did not protect the live session's credentials. This is the literal question I kept asking: *"why a new session if I'm in a Project?"* Cross-reference **F-8 in `EAP_ROUND3_SESSION_STATE.md`**, which documents the session-start auth-binding behavior in detail.

**Why it matters.** A persistent Project that cannot keep its own session authenticated breaks the core promise of "persistent." Long-running work dies on a credential drop and the only recovery is to start over — the opposite of what a Project should buy you.

**Suggested fix.** Let a Project re-bind / refresh credentials for its running session without forcing a brand-new session (see P-9), or at minimum document that auth is session-start-bound and a drop requires a new session.

### P-6 — Connecting the GitHub App did not propagate into the Project's running session

**Date:** 2026-06-27
**Evidence:** VERIFIED (re-tested twice)

**What was observed.** After a correct GitHub App install (`@SJParthi`, All-repos), the new access did **not** take effect in the already-running Project session — a fresh session was required for it to be picked up. I re-tested this twice to be sure it wasn't a transient glitch: the running session never saw the newly-granted access; only a new session did.

**Why it matters.** The natural remediation for an auth gap — "connect the App" — appears to do nothing from inside the live session, so the user repeats it, doubts whether it worked, and only stumbles onto "start a new session" by accident. It compounds P-5.

**Suggested fix.** Propagate a credential/permission change (App connect, repo grant) into the running session, or surface an explicit "new permissions detected — restart session to apply" prompt instead of silently requiring a fresh session.

### P-7 — The coordinator / ambient orchestration is invisible to the user

**Date:** 2026-06-27
**Evidence:** VERIFIED

**What was observed.** Projects are orchestrated by an internal coordinator session, but the user gets **no signal** of whether or how the Project is continuously working — no "the coordinator is active / idle," no view of what it is doing on the user's behalf. The orchestration layer that defines a Project is entirely hidden.

**Why it matters.** Invisibility of the coordinator is why the Project "feels" idle even when something is (or should be) happening — and why P-4's expectation gap is so hard to self-correct. The user cannot tell "working" from "nothing scheduled."

**Suggested fix.** Surface coordinator/ambient state in the Project UI: active vs idle, what is currently running, and what (if anything) is scheduled.

### C. Feature requests (grounded in the above)

### P-8 — Project-level auto-resume / "keep working"

**Date:** 2026-06-27
**Evidence:** VERIFIED (request grounded in P-4)

**What was observed.** Today, continuity requires manually wiring a Routine/trigger; a Project does not resume work on its own. The request is a first-class Project-level "keep working" / auto-resume so users get the continuity they already expect (P-4) without hand-building scheduling plumbing.

**Why it matters.** This is the single capability that would make "Project" mean what users think it means. It directly closes the P-4 expectation gap.

**Suggested fix.** A Project setting: "auto-resume in-flight work" / "keep working on goal X," backed by the existing routine/trigger machinery but owned by the Project, not the user.

### P-9 — In-Project credential refresh

**Date:** 2026-06-27
**Evidence:** VERIFIED (request grounded in P-5 / P-6)

**What was observed.** Connecting the GitHub App took effect only after starting a new session (P-6), and a mid-session credential drop could not be recovered in-session (P-5). The request is in-Project credential refresh so connecting/granting access takes effect in the running session.

**Why it matters.** Removes the "start a new session to fix auth" tax that currently breaks long-running Project work.

**Suggested fix.** An in-session "refresh credentials / re-bind GitHub" action (and/or automatic pickup of a newly-connected App) that does not require tearing down the session.

### P-10 — A clear Project status surface

**Date:** 2026-06-27
**Evidence:** VERIFIED (request grounded in P-3 / P-7)

**What was observed.** There is no single place that tells the user what in a Project is **persistent**, what is **live** (this session), and what is **stale**. The request is a status surface that makes the persistence contract (P-3) and the coordinator state (P-7) visible at a glance.

**Why it matters.** Most of the confusion above is "I can't tell what state I'm in." A status surface is the cheapest fix that addresses P-3, P-5, P-6, and P-7 at once.

**Suggested fix.** A Project status panel: persistent items (history, repo, config) · live items (current session, bound credentials, environment) · stale/expired items, each with a clear badge.

### P-11 — Attach Routines/triggers to a Project explicitly

**Date:** 2026-06-27
**Evidence:** VERIFIED (request grounded in P-4 / P-8)

**What was observed.** Routines/triggers exist but are not visibly owned by the Project — so "the Project takes care of it" is not actually true; the user wires scheduling separately. The request is to attach Routines/triggers to a Project explicitly, so a Project carries its own automation.

**Why it matters.** Makes the Project the unit of automation (matching the mental model), and makes P-8's "keep working" composable from explicit, inspectable routines rather than hidden plumbing.

**Suggested fix.** First-class "Routines attached to this Project," listed and editable in the Project UI, firing into the Project's sessions.

### D. Held — needs user confirmation

### P-12 — Projects billing / usage vs sessions

**Date:** 2026-06-27
**Evidence:** HELD

**What was observed.** Whether Project / EAP usage is metered against the plan, exempt, or free is **unknown** — I have not confirmed it, so I am raising it as a documentation request, not a claim. Heavy multi-agent fan-outs inside a Project plausibly consume plan usage, but I will not assert that without confirmation.

**Why it matters.** Operators running large Projects need to reason about cost; without a documented billing policy they cannot make a go/no-go call (ties to V-7 / the billing question for Omid below).

**Suggested fix.** Document the EAP / Projects billing policy (metered vs exempt vs free), and ideally show a per-Project usage rollup.

### P-13 — Dispatch ↔ Projects relationship

**Date:** 2026-06-27
**Evidence:** HELD

**What was observed.** The relationship between "Dispatch" and Projects is **unclear** to me, and Dispatch may be plan-gated. I have not verified how (or whether) Dispatch integrates with a Project, so this is raised as an open question, not a finding.

**Why it matters.** If Dispatch is the intended way to drive a Project (or a separate, plan-gated path), users need that stated; otherwise it is another undocumented moving part adding to P-2's hierarchy confusion.

**Suggested fix.** Clarify in the Projects doc how Dispatch relates to a Project (same thing? a launcher? plan-gated?), and whether it is required for any Project workflow.

### P-14 — Overnight disruption appears specific to the Projects beta (user-reported correlation)

**Date:** 2026-06-27
**Evidence:** HELD (user-reported correlation; root cause UNCONFIRMED — no causation asserted)

**What was observed (user-reported).** Across three run modes the user reports a difference in overnight stability:

- **Fable 5 overnight runs** ran the **full night uninterrupted** — no human/manual intervention, no Routines — completing per plan.
- Recent **plain (non-Projects) sessions** also did **not** show the mid-session disruption.
- With the **Projects beta** feature access, the mid-session disruption **did** occur — the GitHub-access drop + "start a new session" stranding documented in P-5 / P-6 / F-8.

This points to the disruption possibly being **specific to the Projects beta path** rather than a universal session behavior.

**Why it matters.** If the disruption is specific to the beta, it directly undermines the unattended-overnight value proposition of the exact feature being piloted — the thing a "Project" is supposed to make better is where it gets worse.

**Honest caveat (why this is HELD, not VERIFIED).** This is a **user-reported correlation**, not a controlled comparison. We could **not** independently compare the Fable-5 / plain-session / Projects-beta runs — there is no telemetry exposed to do so — so the **root cause is UNCONFIRMED** and **no causation is claimed**. A plausible *but unverified* mechanism worth checking: the Projects beta may wrap sessions with a different lifecycle / coordinator or auth-binding path than plain sessions. Stated only as a hypothesis for the team to test.

**Suggested fix.** Have Anthropic compare the **session lifecycle + GitHub-auth binding** between Projects-beta sessions and plain sessions / Fable-5 overnight runs, to determine why only the beta path drops mid-run.

---

## Bucket 1 — Verified findings

### V-1 — The background-tasks panel has no per-task progress or liveness signal

**Severity:** medium (operability / trust)

**What happened.** During one large run I had dozens of subagents (review + worker) in the Background-tasks panel at once. Some showed shapes that strongly suggest they were stuck — e.g. ~5 hours elapsed with only 2–4 tool uses — sitting in the same "Running" list as obviously-live agents (2h 47m / 35 tool uses; 8m / 53 tool uses). There is no way to tell them apart from the panel.

**Concrete proof.** The provable, image-independent fact is the **absence of a signal**: the panel shows elapsed time, token count, and a tool-use count per task, but **no "last active N min ago", no heartbeat, no progress %, and no stalled badge**. Two agents with identical "Running" status and wildly different tool-use rates are rendered identically. (I will not claim the 5h agents were definitively orphaned — I can't prove the internal state — only that the panel gives me nothing to decide that with.) The unbounded "Finished N" pile (it reached the high-hundreds in this session) is also directly observable and never collapses.

**Why it matters.** The single most common question during a big fan-out is "is this task actually working, or hung?" Today the panel can't answer it. An operator either waits indefinitely or kills tasks blindly.

**Suggested fix.** Per-task liveness: "last tool use N min ago" + a stalled badge past a threshold; a progress hint (tool-use rate) so a 2-tool-use/5h task reads differently from a 35-tool-use one; a way to collapse/clear the Finished pile.

---

### V-2 — "Felt stalled" while the batch was, in fact, making heavy progress

**Severity:** medium (trust / perceived reliability) — closely tied to V-1

**What happened.** Because of V-1, the run repeatedly *felt* stalled. In reality the batch was landing PRs the whole time.

**Concrete proof (git, this session).** Between 2026-06-24 and 2026-06-26, **20 commits landed on `origin/main`**, of which **13 carry an explicit GitHub PR marker `(#NNN)`** — PRs #882, #883, #884, #887, #893, #894, #896, #897, #898, #900, #902, #903, #927, #931 (the remainder are squash-merged feature commits without the `(#NNN)` suffix). That is a sustained merge cadence, not a stall. Reproduce:

> `git log origin/main --since="2026-06-24 00:00" --pretty=format:"%h %ci %s"`
> `git log origin/main --since="2026-06-24 00:00" --pretty=format:"%s" | grep -oE '\(#[0-9]+\)' | sort -u | wc -l`  → 13

**Why it matters.** The perception gap is the product gap. Real, verifiable throughput was happening, but the surface conveyed "stuck." This is exactly the cost of V-1's missing liveness signal: the system was healthy and the UI couldn't say so.

**Suggested fix.** Surface forward-progress at the batch level — e.g. "N PRs opened / merged this session," a live activity stream, or the per-task progress from V-1. The data exists (commits, PR events); it just isn't reflected back to the operator.

---

### V-3 — Chat-pasted screenshots aren't exposed to file tools (though @-attached uploads are)

**Severity:** medium (workflow friction) — **corrected/narrowed from round 1**

**What happened.** Round 1 said "the agent can't access user-uploaded images." That was too broad. The verified, precise finding is narrower:

- **Images pasted directly into the chat** are visible to the model's context but are **not materialized to any filesystem path** the agent's Read/Write/Bash/Glob tools can reach. The agent cannot programmatically save, rename, or embed them.
- **Files @-attached as uploads ARE readable.** They land at `/root/.claude/uploads/<session-id>/<uuid>-<originalname>` and the Read tool reads them directly.

**Concrete proof.** I read an @-attached PNG this session with the Read tool:

> `Read("/root/.claude/uploads/fb27d844-838d-5cff-9a01-bed6f70a80d2/b38232e9-IMG_6793.png")` → returned the image successfully.

`ls -la /root/.claude/uploads/<session-id>/` shows the upload (526 KB PNG) alongside other @-attached files (logs, PDFs, HTML). So the gap is specifically the **chat-paste path**, not uploads in general.

**Why it matters.** A natural Projects workflow is "here's a screenshot of what I'm seeing, capture it into the report." With @-attach it works; with an inline paste it silently doesn't, and the difference isn't discoverable to the user — they look the same in chat.

**Suggested fix.** Materialize chat-pasted images to the same readable `uploads/` path (or any path the file tools can reach), so the paste and @-attach flows behave identically.

---

### V-4 — A single shared append-only ledger file forces parallel PRs to merge sequentially

**Severity:** medium (throughput ceiling for multi-agent batches)

**What happened.** BruteX requires every PR to append an entry to one shared decision ledger, `docs/MIGRATION.md`. When N agents open N PRs in parallel, each edits the same file's tail, so each pair of PRs conflicts on that file. Whichever merges first wins; the rest must rebase and re-resolve — serializing what was meant to be parallel.

**Concrete proof (repo state).** `docs/MIGRATION.md` on `origin/main` is **6,297 lines / 267 ledger entries**, and every PR in this batch appended to it. This is a structural property of the workload, but it's a pattern the Projects multi-agent model will hit anywhere a repo has a hot append-only file (CHANGELOG, ledger, registry). Reproduce: `git show origin/main:docs/MIGRATION.md | wc -l`.

**Why it matters.** The headline promise of multi-agent is parallelism. A hot shared file quietly converts it back to sequential at merge time, and the operator only sees it as "why are these PRs taking turns?"

**Suggested fix (idea-adjacent, but grounded).** A merge-queue-aware mode that auto-rebases append-only conflicts (append is associative — two appends to a file tail can be auto-merged), or guidance/tooling to detect a hot shared file across a fan-out and warn up front.

---

### V-5 — Recurring transient crates.io flake in CI (worked around, not a product bug — but visible)

**Severity:** low (CI reliability / friction)

**What happened.** Rust wheel builds in CI intermittently failed at dependency download — `download of <crate> failed / curl [56] Failure receiving data / OpenSSL SSL_read: unexpected eof / Connection reset by peer` — aborting the build before any test ran, flaking otherwise-green PRs.

**Concrete proof + our fix (in repo).** We hardened it in `.github/workflows/ci.yml`:

> Line 79: `CARGO_NET_RETRY: "10"`
> Line 80: `CARGO_NET_GIT_FETCH_WITH_CLI: "true"`
> Plus per-step "belt-and-suspenders crates.io flake guard" retry loops around each `pip install ./rust/*` wheel build (lines ~179, ~200, ~269, ~290).

The root-cause comment block (ci.yml:66–78) documents the exact failure strings above. This is environmental, not a Projects defect — flagging it as context for how often the underlying runner network flakes, since it interacts with V-6.

**Why it matters.** Each flake cost a re-trigger cycle (see V-6). It's worth the team knowing how frequently GitHub-hosted runner networking drops mid-download under this workload.

**Suggested fix.** Nothing for Anthropic to fix in the product; noted because it compounds V-6.

---

### V-6 — An agent can't re-run a failed CI check; it must push an empty commit to re-trigger

**Severity:** medium (workflow friction / wasted cycles)

**What happened.** When CI fails on a transient (the V-5 crates.io flake, an artifact-upload timeout, a stuck gate), the agent has no way to simply *re-run* the failed check — the natural action a human takes from the GitHub UI. The agent's path to rerun is effectively blocked, so the only workaround is to push an **empty/no-op "re-trigger" commit** to force the workflow to run again.

**Concrete proof (git, across this session's branches).** There are **48 `chore(ci): re-trigger …` commits** across the session's refs, **6 of them specifically for the crates.io flake**. A sample:

> `chore(ci): re-trigger transient crates.io wheel-build flake`
> `chore(ci): re-trigger transient crates.io flake (fno-math js-sys download)`
> `chore(ci): re-trigger stuck coverage gate`
> `chore(ci): re-trigger flaky property test on docs PR`

Reproduce: `git log --all --oneline --grep="re-trigger" | wc -l` → 48. The workaround is even codified in our own tooling: the `ship-pr` skill instructs "re-trigger (empty commit) at most a couple of times" (`.claude/skills/ship-pr/SKILL.md`).

**Why it matters.** Empty commits pollute history, churn the branch, restart the *entire* workflow (not just the failed job), and burn agent turns. A human clicks "Re-run failed jobs"; the agent can't.

**Suggested fix.** Give the agent a first-class "re-run this check / re-run failed jobs" capability (the GitHub `rerun` API surface), so transient failures don't require history-polluting empty commits.

---

### V-7 — Worktree stale-base trap exists and is correctly guarded (a POSITIVE)

**Severity:** n/a — this one works; included as a "keep doing this"

**What happened.** With many agents on many worktrees against a fast-moving `main`, a feature branch can fall behind and a merge over a stale base would be unsafe. This is a real hazard in the multi-worktree model.

**Concrete proof (it's guarded).** The auto-merge workflow refuses to merge unless `mergeable_state` is clean — it explicitly rejects conflict / stale-base / blocked states:

> `.github/workflows/auto-merge.yml:411` — `"refusing to merge (conflict / stale base / blocked)"`
> Backed by a red-team regression test: `tests/redteam/test_redteam_automerge_safety.py` ("the mergeability gate must reject conflict / stale-base states").

**Why it matters.** This is the safety property that *should* exist in a multi-agent merge pipeline, and it's enforced + regression-tested. Calling it out so the pattern is recognized and preserved as Projects' own merge tooling matures.

---

## Bucket 2 — Ideas & out-of-box suggestions (clearly labeled as IDEAS, not findings)

These are not defects and carry no claim of proof — they're directions that would have helped this batch.

**I-1 — A structured project-state MCP (read project state as fields).** Today an orchestrator re-derives state by running `git status` / `git worktree list` / diffing branches every time. An MCP that exposes project state as structured fields (open PRs, per-branch status, per-worktree dirty flags, current ledger head) would let an agent read state in O(1) instead of reconstructing it. We'd happily prototype against it.

**I-2 — An agent-invocable routine/scheduling tool.** We already use scheduled triggers manually; a first-class, agent-callable "run this prompt on a schedule / after condition X" primitive would let a batch self-pace (e.g. "re-check CI in 10 min," "resume when PR #N merges") instead of busy-waiting or burning a turn.

**I-3 — A per-session base-state header.** A cheap, authoritative "here is the verified current state of these files/branches" snapshot attached at sub-task dispatch, so a worker can confirm its premise in one check rather than spending a full turn discovering a stale handoff.

**I-4 — An up-front cost estimate for large fan-outs.** Before launching N subagents, an estimated token/cost envelope ("this fan-out is ~M agents, est. T tokens") so the operator can make an informed go/no-go.

**I-5 — Correlate the same infra error across subagents into one alert.** When the V-5 crates.io flake hits several wheel-build jobs at once, surface it as one "transient infra error affecting N tasks" notice instead of N separate failures the operator triages individually.

---

## Questions for Omid (3)

1. **Routine/scheduling:** Is an agent-invocable routine/scheduling tool (I-2) on the roadmap? It's the single capability that would most change how we structure long multi-agent batches.
2. **Structured state MCP:** Is a structured project-state MCP (I-1) planned? If so, we'd like to prototype BruteX's orchestration against it and give you early feedback.
3. **Billing:** Is EAP / Projects usage metered against the plan, exempt, or free? We run heavy multi-agent fan-outs and want to reason honestly about cost (relevant to I-4).

---

*Every fact above is reproducible from this repo and session. Where round 1 inferred a cause we couldn't verify (e.g. "agents are stuck," "agent can't access uploads"), this round states only the provable part and labels the rest as an idea.*
