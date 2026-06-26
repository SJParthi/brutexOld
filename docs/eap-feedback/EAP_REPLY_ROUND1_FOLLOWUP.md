Hi Diana,

Thanks so much — glad the feedback was useful, and happy to dig into both.

**1) Queryable project state (prose to fields)**

Yes — and an MCP that lets Claude read the "board" is the higher-value of the two for this workflow. Today the authoritative project facts (active branch, current decision/ledger number, open PRs, the scope lock) live in long prose: a large project-rules file plus an append-only decision ledger. Two concrete pains this caused:

- Every session has to re-parse prose to learn "where are we," and we had a session get actively misled by a stale line in that prose (it started work off an out-of-date branch).

- Because the decision ledger is one append-only prose file, parallel PRs all collided on it, so a batch of independent PRs was forced to merge strictly one-at-a-time (rebase, then a 25 to 30 minute CI run, then merge, repeated). Structured per-record state would not collide the way a single file does.

What would help most: an MCP exposing the project board as structured fields Claude reads first each session — at minimum active_branch, current_ledger_no, open_prs, scope_lock, and recent_decisions — and ideally read and write, so an agent updates task, PR, and decision records as fields instead of appending prose. A query API would help too, but the MCP (Claude reading the board mid-task) fits the autonomous-agent loop better.

**2) The "routines not reachable" finding**

First, to be accurate: Routines clearly exist as a feature in the Projects UI — I can see them in the sidebar, so this is not "the feature is missing." And there was no thrown error, so there is no error string to share. The finding was narrower and agent-side: I had no tool in my session's toolset to create or schedule a routine programmatically. So an autonomous agent cannot set up its own recurring check-in — only a person can, through that UI.

Concretely: I wanted to schedule a self check-in to re-poll a PR after its CI finished. With no agent-callable routine tool available, I fell back to noting the check-in in the status checklist and relying on PR webhook events to wake the session. The scheduling I did observe was event/webhook-driven (PR activity waking the session), not an agent-invokable "create a recurring routine."

If there is an agent-side way to create a routine that I missed, point me at it and I will test it and report exactly what happens.

**3) The call**

Yes — Wednesday July 1 works, and I'd be glad to chat live. What time and timezone suit your engineer? I will make it work.

Thanks again,
Parthi
