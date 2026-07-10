# EAP Feedback — Round 4 — Ready-to-send Email

> Send (or reply on the existing thread) to **claude-code-early-access@anthropic.com**
> (contact: Diana Liu, Product Operations Manager, Claude Code).
> Attach the detailed write-up: `EAP_ROUND4_SUBMISSION.md` + its HTML render
> (rename on send to e.g. `BruteX-ClaudeCode-EAP-Round4.md` / `.html`),
> plus the 9 screenshots listed at the end of the write-up.
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

Thank you sincerely for extending our EAP access through July 14 — it made
this round possible.

Round 4 is coordinator-scale feedback. BruteX now runs as one coordinator
session dispatching, sequencing, and monitoring a fleet of more than 10
parallel sessions in a single Project. The pattern works remarkably well —
a fleet-wide broadcast reached every target session without a miss, and
sessions deduplicate shared resources like merge slots between themselves.
Everything below is about removing the remaining toil and instability at
that scale, not about the pattern failing.

The seven items this round, one line each:

1) The project-level default reasoning effort is ignored — every new
   session comes up at the platform default (High), proven by a controlled
   double-canary experiment (Max then Extra, both spawned at High) while
   the model default mapped correctly both times.

2) Correcting the effort across multiple sessions in quick succession
   reproducibly auto-restarts the Claude Code desktop app mid-fleet-operation.

3) At more than 10 parallel sessions, transient rate-limit / "service
   busy" / API errors cluster around session-create bursts.

4) Cross-session messages dropped mid-turn — twice today, silently, with
   no receipt or error to the sender; recovered only via fallback channels.

5) The coordinator can create, list, and message sessions but cannot close
   or archive one — verified against the full coordinator tool registry.

6) Sessions show a false "needs input" badge when nothing is actually
   pending.

7) The inverse of 6: actively-working sessions read "idle" — the status
   appears to track only the main conversation turn.

The full details, suggested fixes, and open questions are in the attached
write-up (MD + HTML), with 9 screenshots: the four-image item-1 evidence
sequence (configured effort default vs the spawned session's actual effort,
across two configured values), the post-restart desktop app, the session
list at fleet scale, the missing close/archive affordance, and the false
"needs input" / working-but-idle status mismatches. Where we could not
prove a cause, we say so plainly.

Happy to capture whatever diagnostics would help — especially on the
desktop restart and the create-burst instability. I'll keep sending
findings through the extended window.

Thank you again — it's a pleasure to help push this.

Best,
Parthiban
(sjparthi93@gmail.com)
```

---

## Send checklist

- [ ] Attach `EAP_ROUND4_SUBMISSION.md` + the HTML render (renamed for sharing).
- [ ] Attach the 9 screenshots (the 4-image item-1 effort-default evidence sequence · post-restart app · fleet-scale session list · no close/archive affordance · false "needs input" badge · working-but-idle status).
- [ ] Confirm the To: address is `claude-code-early-access@anthropic.com`.
- [ ] Keep the thread Confidential per the Trusted Tester Agreement.
