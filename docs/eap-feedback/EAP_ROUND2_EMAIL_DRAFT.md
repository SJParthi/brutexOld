# EAP Round-2 Feedback — Ready-to-Send Email Draft

> **Status:** DRAFT — not yet sent. This file is the email body the operator can
> copy/paste and send. Nothing below is presented as verified unless it is
> labelled VERIFIED in the source dossier; HELD items are flagged "pending
> reproduction."
>
> **Source of truth:** the full dossier lives in this repo at
> `docs/eap-feedback/EAP_ROUND2_BATCH.md` (items P-1..P-14, V-1..V-7, I-1..I-5)
> and `docs/eap-feedback/evidence/EVIDENCE.md` (items F-1..F-7). Cross-referenced
> F-8 (session-start auth binding) / F-9 (effort-revert) live in
> `EAP_ROUND3_SESSION_STATE.md`, which is not on this branch — see the caveat at
> the end.

---

**To:** claude-code-early-access@anthropic.com (Diana Liu)
**Cc:** (optional — Omid, if direct)
**Subject:** Claude Code Projects (beta) — Round-2 pilot feedback (evidence-gated)

---

Hi Diana,

Round-2 feedback from piloting the Claude Code **Projects (beta)** on a large
multi-agent workload (BruteX — a backtesting-engine repo). This round is
deliberately tighter than round 1: every item below carries concrete,
reproducible proof — a log line, a file path, a git fact, or a directly
observable session artifact — and anything I could not reproduce is clearly
marked **HELD (pending reproduction)** rather than asserted as fact.

Happy to also walk through any of this on the **Jul 1 call with Omid**.

---

## 1. Projects (beta) — the flagship feedback

Projects **is** a real beta feature with access (an internal coordinator session
orchestrates a project; this batch ran inside one). The root cause of most items
below is that Projects is **undocumented** — the capability ships, but there is
no spec for what it is, what persists, or how it relates to sessions /
environments / routines — so users form wrong expectations and hit gaps.

**Documentation gaps (all VERIFIED):**
- **P-1** — Projects has **no documentation page at all**; `llms.txt` carries no
  "Projects" entry.
- **P-2** — The **Project ↔ Session ↔ Environment ↔ Routine** hierarchy is
  undocumented and genuinely unclear (which is persistent, which runs work,
  which holds auth).
- **P-3** — **What persists vs what doesn't** is undocumented: history persists;
  live credentials and the running VM are session-scoped and silently re-bound
  per session.

**Behavior / expectation gaps (all VERIFIED):**
- **P-4** — Users expect a Project to be a **self-running persistent container**;
  in reality work runs as sessions and 24/7 automation still needs a manually
  wired Routine/trigger.
- **P-5** — A Project does **not** shield a running session from session-start
  auth binding: **GitHub access dropped mid-session and could only be recovered
  by starting a brand-new session — even inside a Project.**
- **P-6** — Connecting the GitHub App (`@SJParthi`, All-repos) **did not
  propagate** into the already-running Project session; a fresh session was
  required (re-tested twice).
- **P-7** — The **coordinator / ambient orchestration is invisible** — no signal
  of whether the Project is working, idle, or has anything scheduled.

**Feature requests grounded in the above (all VERIFIED as requests):**
- **P-8** — Project-level **auto-resume / "keep working."**
- **P-9** — **In-Project credential refresh** (re-bind GitHub without a new
  session).
- **P-10** — A clear **Project status surface** (persistent vs live vs stale).
- **P-11** — **Attach Routines/triggers to a Project explicitly.**

**Held — flagged pending confirmation (NOT presented as verified):**
- **P-12** — Projects/EAP **billing vs sessions** is unknown — raised as a
  documentation request, not a claim.
- **P-13** — **Dispatch ↔ Projects** relationship unclear; may be plan-gated —
  open question.
- **P-14** — **Overnight disruption appears specific to the Projects beta**
  (user-reported correlation: Fable-5 overnight runs and plain non-Projects
  sessions ran uninterrupted; the mid-session GitHub drop occurred under the
  Projects beta). Stated explicitly as an **unverified correlation, no causation
  claimed** — needs Anthropic to compare the session lifecycle + GitHub-auth
  binding between Projects-beta and plain/Fable-5 runs.

---

## 2. Other verified findings (V-bucket)

- **V-1** (medium) — Background-tasks panel has **no per-task liveness/progress**
  signal: no heartbeat, no "last active N min ago," no stalled badge; stuck and
  live agents render identically, and the "Finished N" pile grows unbounded.
- **V-2** (medium) — The run **"felt stalled" while landing PRs the whole time**;
  reproducible: 20 commits on `origin/main` over 2026-06-24..26, 13 carrying a
  `(#NNN)` PR marker. The perception gap is the product gap.
- **V-3** (medium) — **Chat-pasted screenshots aren't exposed to file tools**,
  though **@-attached uploads ARE** readable at
  `/root/.claude/uploads/<session-id>/...` (narrowed/corrected from round 1).
- **V-4** (medium) — A single **shared append-only ledger file**
  (`docs/MIGRATION.md`, 6,297 lines) forces parallel PRs to merge sequentially —
  a pattern any hot CHANGELOG/registry will hit in the multi-agent model.
- **V-5** (low) — Recurring **transient crates.io flake** in CI (environmental,
  worked around in `.github/workflows/ci.yml`; flagged as context, not a product
  bug).
- **V-6** (medium) — **An agent can't re-run a failed CI check** — it must push
  an empty "re-trigger" commit (48 such commits this session). Requests a
  first-class "re-run failed jobs."
- **V-7** (positive) — The **worktree stale-base trap is correctly guarded** in
  the auto-merge workflow + regression-tested. Called out as a "keep doing this."

---

## 3. Ideas (clearly labelled IDEAS, no claim of proof)

- **I-1** — A structured **project-state MCP** (read open PRs / branch status /
  worktree dirty flags / ledger head as fields).
- **I-2** — An **agent-invocable routine/scheduling tool** so a batch can
  self-pace.
- **I-3** — A **per-session base-state header** (verified snapshot at dispatch).
- **I-4** — An **up-front cost estimate** for large fan-outs.
- **I-5** — **Correlate the same infra error across subagents** into one alert.

**Three questions for Omid:** (1) is an agent-invocable routine/scheduling tool
(I-2) on the roadmap; (2) is a structured project-state MCP (I-1) planned (we'd
prototype against it); (3) is EAP/Projects usage metered, exempt, or free
(relevant to I-4 and P-12).

---

## 4. Evidence gallery (F-bucket, in `evidence/EVIDENCE.md`)

- **F-1** (medium) — Background-tasks panel lacks per-task progress/liveness;
  orphaned subagents accumulate invisibly (the V-1 evidence row; screenshots
  `01`/`02`).
- **F-2** (medium) — Agent file tools cannot access user-uploaded images in a
  Projects session (the round-1 framing later narrowed by V-3).
- **F-3** (medium) — Worktree-per-agent isolation leaves **orphaned worktrees
  sharing one branch name** (16 live worktrees, 13 on the same suffix;
  reproducible from `git worktree list`).
- **F-4** (high) — **Lost work**: a sub-agent's uncommitted output can silently
  disappear between sessions.
- **F-5** (medium) — **No cross-session cost/usage view** for a multi-agent
  batch.
- **F-6** (low–medium) — Resuming an agent with a **stale premise wastes a full
  turn** before the mismatch surfaces.
- **F-7** (low) — **Branch/worktree naming collisions** make "which PR is this?"
  ambiguous in the UI.

---

## 5. Full dossier

Everything above is summarized; the complete, evidence-cited write-ups live in
the repo:

- `docs/eap-feedback/EAP_ROUND2_BATCH.md` — P-1..P-14, V-1..V-7, I-1..I-5
- `docs/eap-feedback/evidence/EVIDENCE.md` — F-1..F-7 + the screenshot gallery

Thanks — and looking forward to the Jul 1 call.

Best,
Parthiban
sjparthi93@gmail.com

---

## Attachments checklist (screenshots to attach)

> The image files are not stored in the repo (the gallery rows are written and
> ready to receive them — see the "Image status" note in
> `evidence/EVIDENCE.md`). Attach the operator's existing screenshots, named per
> the gallery convention, and confirm each maps to the finding it backs:

- [ ] `01-background-tasks-orphaned-agents.png` — backs **V-1 / F-1**
  (~5h-runtime review subagents with only 2–4 tool uses, indistinguishable from
  live agents).
- [ ] `02-background-tasks-running-5h.png` — backs **V-1 / F-1** (healthy
  high-tool-use agents next to the stuck ones; the unbounded "Finished 294"
  counter).
- [ ] `overnight-stuck-*.png` (operator's overnight screenshots) — strengthen
  **P-5 / P-6 / P-14** (the mid-session GitHub-access drop + "start a new
  session" stranding under the Projects beta).
- [ ] `github-app-install-*.png` (operator's GitHub-App-install screenshots) —
  strengthen **P-6** (correct `@SJParthi` / All-repos install that did not
  propagate into the running session).
- [ ] *(optional / if available)* before/after screenshot of the **effort-revert
  (F-9)** — REQUIRED to move F-9 from HELD → VERIFIED (see caveat).

**Text-sufficient findings (screenshot optional):** the verbatim-403 /
doc-cited / git-fact findings — **P-1** (`llms.txt` has no Projects entry),
**V-2** (git commit/PR counts), **V-3** (the exact readable upload path),
**V-4** (`MIGRATION.md` line count), **V-5/V-6** (`ci.yml` lines + re-trigger
commit count), **V-7** (`auto-merge.yml:411` + the regression test),
**F-3/F-4** (`git worktree list` / branch search) — are carried by text and need
no screenshot.

---

## 6. Held / pending-confirmation note (do not present as verified)

The following are flagged as needing confirmation and are **NOT** presented as
verified facts in the email:

- **F-9 (effort-revert)** — HELD; **requires a before/after screenshot** to move
  to VERIFIED. (Note: F-9 lives in `EAP_ROUND3_SESSION_STATE.md`, which is not on
  this branch; include only if the operator confirms the artifact.)
- **P-12 (billing)** — HELD; documentation request, billing policy unconfirmed.
- **P-13 (Dispatch)** — HELD; open question, relationship/plan-gating
  unconfirmed.
- **P-14 (overnight-disruption repro)** — HELD; user-reported correlation only,
  no causation claimed, root cause unconfirmed.

Per verify-before-claim: nothing above is stated as fact unless it is labelled
VERIFIED in the source dossier.
