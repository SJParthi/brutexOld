# EAP Feedback — Round 4 — Ready-to-send Email

> Send (or reply on the existing thread) to **claude-code-early-access@anthropic.com**
> (contact: Diana Liu, Product Operations Manager, Claude Code).
> Attach the detailed write-up: `EAP_ROUND4_SUBMISSION.md` + its HTML render
> (rename on send to e.g. `BruteX-ClaudeCode-EAP-Round4.md` / `.html`),
> plus the 11 screenshots listed at the end of the write-up.
> EAP access extended through 2026-07-14.
> Materials are Confidential per the Trusted Tester Agreement.

---

## Subject

```
Claude Code Projects EAP — Round 4 feedback (BruteX), coordinator-scale fleet findings
```

---

## Body (verbatim — paste as-is)

```
Hi Diana, and the Claude Code team,

Thank you sincerely for extending our EAP access through July 14 — it is
what made this round of observations possible.

Round 4 is coordinator-scale feedback. Since round 3 our usage pattern has
changed: BruteX now runs as one coordinator session dispatching, sequencing,
and monitoring a fleet of more than 10 parallel sessions in a single
Project. The pattern itself works remarkably well — a fleet-wide broadcast
reached every one of its target sessions without a miss, and sessions
deduplicate shared resources like merge slots between themselves. Everything below is about removing the
remaining toil and instability at that scale, not about the pattern failing.

The nine items this round, one line each:

1) The model selected when spawning or scheduling a session from the
   coordinator is not mapped onto the new session — it comes up on the
   default model and must be corrected by hand, repeatedly observed.

2) Even with the Project Settings default model configured, every session
   in the fleet still had to be opened and its model re-selected manually,
   every time — pure setup toil at fleet scale.

3) Changing the model or the reasoning effort across multiple sessions
   in quick succession reproducibly triggers a Claude Code desktop app
   auto-restart, mid-fleet-operation.

4) At more than 10 open sessions we see recurring transient rate-limit /
   "service busy" / API errors and sudden restarts, clustering around
   session-create bursts — round 3's throttle item at larger scale, with a
   new trigger datum.

5) Cross-session messages drop mid-turn silently — twice today, in two
   different sessions, with no receipt, retry, or error to the sender;
   coordination recovered only through fallback channels.

6) The coordinator can create, list, and message sessions but cannot
   close or archive one — no lifecycle counterpart exists in any
   coordinator-accessible tool, so completed sessions accumulate for the
   human to inspect and close by hand.

7) Sessions repeatedly show a "needs input" badge when nothing actually
   needs input — opening them reveals no pending question, prompt, or
   permission request, so badge-driven triage sends the operator in for
   nothing.

8) The inverse of 7: actively-working sessions read "idle" in the
   session list — the status appears to track only the main conversation
   turn, never background or agent activity.

9) The project-level default reasoning effort is ignored — every new
   session comes up at the platform default ("High"); confirmed by a
   controlled two-spawn experiment (Max, then Extra — both spawns read
   "High", while the model default mapped correctly both times).

The full details, suggested fixes, and open questions are in the attached
write-up (MD + HTML), along with screenshots of the configured Project
default, a freshly-spawned session on the wrong model, the post-restart
desktop app, the session list at fleet scale, the missing close/archive
affordance, the false "needs input" / working-but-idle status
mismatches, and the four-image item-9 evidence sequence (configured
effort default vs the spawned session's actual effort, across two
configured values). Where we could not prove a cause, we have said so
plainly rather than overstating it.

Happy to capture whatever diagnostics would help — especially on the
desktop restart and the create-burst instability. I'll keep sending
findings as they land through the extended window.

Thank you again — it's a pleasure to help push this.

Best,
Parthiban
(sjparthi93@gmail.com)
```

---

## Send checklist

- [ ] Attach `EAP_ROUND4_SUBMISSION.md` + the HTML render (renamed for sharing).
- [ ] Attach the 11 screenshots (Project default model · wrong-model spawn · post-restart app · fleet-scale session list · no close/archive affordance · false "needs input" badge · working-but-idle status · the 4-image item-9 effort-default evidence sequence).
- [ ] Confirm the To: address is `claude-code-early-access@anthropic.com`.
- [ ] Keep the thread Confidential per the Trusted Tester Agreement.
