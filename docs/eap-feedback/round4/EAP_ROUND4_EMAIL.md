# EAP Feedback — Round 4 — Ready-to-send Email

> **To:** claude-code-early-access@anthropic.com — reply on the existing
> thread with Tim; Diana Liu included.
> **From:** Parthiban (sjparthi93@gmail.com) · **Send date:** 2026-07-10.
> **Workload:** BruteX, run as a coordinator-orchestrated fleet of more than
> 10 parallel sessions in one Project.
> Attach `EAP_ROUND4_SUBMISSION.md` + its HTML render (rename on send to
> `BruteX-ClaudeCode-EAP-Round4.md` / `.html`). EAP access extended through
> 2026-07-14. Materials are Confidential per the Trusted Tester Agreement.

---

## Subject

Claude Code Projects EAP — Round 4 feedback (BruteX), coordinator-scale fleet findings

---

*Copy the rendered body below (from "Hi Tim and Diana," through "Thanks, Parthi") straight into Gmail — the table pastes formatted.*

## Body (verbatim — paste as-is)

Hi Tim and Diana,

One more from the beta — coordinator-scale platform feedback this time,
seven items.

The ask: 30 minutes on a screen-share call to walk through all seven
live, one by one. Several of them — the desktop auto-restart, the
session-create-burst instability, the stale idle / "needs input" badges —
are timing-dependent and nearly impossible to capture after the fact; on
a call we can simply make them happen in front of you.

Thank you for extending our access through July 14 — it made this round
possible. BruteX now runs as one coordinator session dispatching,
sequencing, and monitoring a fleet of more than 10 parallel sessions in
one Project. The pattern itself works remarkably well — a fleet-wide
broadcast reached every target session without a miss. The seven items
are the remaining toil and instability at that scale:

| # | Issue | Impact |
|---|-------|--------|
| 1 | Project-level default reasoning effort is ignored — every new session spawns at the platform default (High); double-canary proven (Max, then Extra — both spawned High; the model default mapped correctly both times) | Every session corrected by hand |
| 2 | Correcting the effort across sessions in quick succession auto-restarts the desktop app | The workaround for 1 crashes the app mid-fleet |
| 3 | At more than 10 parallel sessions, rate-limit / "service busy" / API errors cluster around session-create bursts | Growing the fleet destabilizes the fleet |
| 4 | Cross-session messages dropped mid-turn — twice today, silently, no receipt or error to the sender | Coordination silently diverges |
| 5 | Sessions cannot be DELETED from the coordinator — create, list, open, message all exist; verified against the full tool registry | The fleet only ever grows; retirement is manual |
| 6 | False "needs input" badges when nothing is pending | Wasted triage round trips |
| 7 | Actively-working sessions read "idle" — status tracks only the main conversation turn | Cannot tell working from stuck |

The attached write-up (MD + HTML) has the details, suggested fixes, and
open questions. The attached report lists the coordinator and test-session
IDs with timestamps — directly traceable on your side; we can pin per-item
IDs on the call. Where we could not prove a cause, we say so plainly.

I'll keep sending findings through the extended window.

Thanks,
Parthi

---

## Send checklist

- [ ] Attach `EAP_ROUND4_SUBMISSION.md` + the HTML render (renamed for sharing).
- [ ] Confirm the To: address is `claude-code-early-access@anthropic.com` (reply on the existing thread with Tim; Diana Liu included).
- [ ] The headline ask is the 30-minute screen-share walkthrough — propose 2–3 slots when sending.
- [ ] Keep the thread Confidential per the Trusted Tester Agreement.
