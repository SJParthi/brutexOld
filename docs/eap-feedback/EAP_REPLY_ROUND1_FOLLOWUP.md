Hi Diana,

Thanks so much — glad the feedback was useful, and happy to dig into both.

**1) Queryable project state (prose to fields)**

Yes — and an MCP that lets Claude read the "board" is the higher-value of the two for this workflow. Today the authoritative project facts (active branch, current decision/ledger number, open PRs, the scope lock) live in long prose: a large project-rules file plus an append-only decision ledger. Two concrete pains this caused:

- Every session has to re-parse prose to learn "where are we," and we had a session get actively misled by a stale line in that prose (it started work off an out-of-date branch).

- Because the decision ledger is one append-only prose file, parallel PRs all collided on it, so a batch of independent PRs was forced to merge strictly one-at-a-time (rebase, then a 25 to 30 minute CI run, then merge, repeated). Structured per-record state would not collide the way a single file does.

What would help most: an MCP exposing the project board as structured fields Claude reads first each session — at minimum active_branch, current_ledger_no, open_prs, scope_lock, and recent_decisions — and ideally read and write, so an agent updates task, PR, and decision records as fields instead of appending prose. A query API would help too, but the MCP (Claude reading the board mid-task) fits the autonomous-agent loop better.

**2) The "routines not reachable" finding — I need to correct this**

I have to walk this one back, and I would rather be straight with you than defend it. Routines works. In the Routines panel I can see scheduled check-ins I set during the session that actually ran, and an hourly routine running on schedule, with Schedule, GitHub-event, and API triggers all available. So my round-1 note that routines were "not reachable" was imprecise: I turned a momentary in-session impression into a finding without first checking it against the Routines panel, which plainly shows the feature working. Apologies for the noise on that one, and thank you for prompting me to look again.

There was no error to share, and on reflection there was no gap here to report.

One genuinely precise observation I can offer, and I will verify it properly before calling it more than an observation: the routine-creation form asks you to select a repository, so a routine appears to be scoped to one repo. For someone running multiple projects, that suggests recurring work spanning projects needs a routine per project rather than one shared routine. Happy to confirm this concretely if it is useful.

**3) The call**

Yes — Wednesday July 1 works, and I'd be glad to chat live. What time and timezone suit your engineer? I will make it work.

I also have more findings from continued use of the beta; I will send them as a separate, evidence-backed batch, and I would be glad to walk through them live on the call.

Thanks again,
Parthi
