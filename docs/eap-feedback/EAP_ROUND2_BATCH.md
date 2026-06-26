# Claude Code Projects — EAP Feedback, Round 2

**For:** Omid (eng lead), via the Jul 1 feedback call
**From:** Parthiban (sjparthi93@gmail.com)
**Date:** 2026-06-26
**Workload under test:** BruteX — a large multi-agent backtesting-engine repo. The product under review is the **Claude Code Projects beta**; BruteX is just the workload that exercised it.

**Ground rule for this round (self-imposed):** every finding below carries concrete, reproducible proof — a log line, a file path, a git fact, or a directly-observable session artifact. Where round 1 inferred a cause we couldn't prove, I've narrowed the claim to the part I can stand behind. Fewer, bulletproof findings beat many shaky ones.

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
