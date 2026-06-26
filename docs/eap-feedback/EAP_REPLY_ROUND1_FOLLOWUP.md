Hi Diana,

Thanks so much — glad the feedback was useful, and happy to dig into both.

**1) Making project state queryable (reading it as fields, not prose)**

Yes — an MCP that lets Claude read the project "board" is the higher-value option for this workflow. Today the authoritative project facts (active branch, current decision/ledger number, open PRs, the scope lock) live inside long prose: a large project-rules file plus an append-only decision ledger. That caused two concrete pains while reading state:

- Every session had to re-parse prose to learn "where are we," and one session was actively misled by a stale line in that prose (it started work off an out-of-date branch).

- Because the decision ledger is one append-only prose file, parallel PRs all collided on it, so a batch of independent PRs was forced to merge strictly one at a time. Structured per-record state would not collide the way a single file does.

What would help most is an MCP that exposes those facts as structured fields Claude reads at the start of each session — at minimum active_branch, current_ledger_no, open_prs, scope_lock, and recent_decisions — so the agent reads state instead of parsing prose. Read-only would already solve the "where are we" problem; read-and-write would be a bonus. A plain query API would also work, but the MCP (Claude reading it mid-task) fits the autonomous loop better. This is purely about reading state — separate from the routines/scheduling point below.

**2) The "routines not reachable" finding — I need to correct this**

I have to walk this one back, and I would rather be straight with you than defend it. Routines works. In the Routines panel I can see scheduled check-ins I set during the session that actually ran, and an hourly routine running on schedule, with Schedule, GitHub-event, and API triggers all available. So my round-1 note that routines were "not reachable" was imprecise: I turned a momentary in-session impression into a finding without first checking it against the Routines panel, which plainly shows the feature working. Apologies for the noise on that one, and thank you for prompting me to look again.

There was no error to share, and on reflection there was no gap here to report.

One genuinely precise observation I can offer, and I will verify it properly before calling it more than an observation: the routine-creation form asks you to select a repository, so a routine appears to be scoped to one repo. For someone running multiple projects, that suggests recurring work spanning projects needs a routine per project rather than one shared routine. Happy to confirm this concretely if it is useful.

**3) The call**

Thank you — I would genuinely welcome the chance to chat live, and Wednesday, July 1 works well for me if it still suits your team. One small scheduling note: I am based in IST (India, UTC+5:30), so a slot in your morning generally lands in my evening and overlaps well. Whatever time is easiest for you and your engineer, I will gladly make it work — please feel free to send a calendar invite to this email at whatever time fits your schedule.

I also have more findings from continued use of the beta; I will send them as a separate, evidence-backed batch, and I would be glad to walk through them live on the call.

Thanks again,
Parthi
