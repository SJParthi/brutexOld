# Claude Code Projects (beta) — Round-4 Feedback

> Observations, requests, and open questions accompanying the round-4 feedback email. Prepared 2026-07-10. Where we could not prove a cause, we say so plainly.

**For:** Claude Code Early Access team — Diana Liu (claude-code-early-access@anthropic.com)
**From:** Parthiban (sjparthi93@gmail.com) · **Date:** 2026-07-10
**Workload under test:** BruteX — run as a coordinator-orchestrated fleet of more than 10 parallel sessions in one Project.

This round is **coordinator-scale** feedback: one coordinator session dispatches, sequences, and monitors a fleet of more than 10 parallel sessions against a single repository. The pattern itself works remarkably well — the coordinator sequences the fleet reliably, a fleet-wide broadcast reached every target session without a miss, and sessions deduplicate shared resources (e.g. merge slots) between themselves; everything below is about removing the remaining toil and instability at that scale. Thank you sincerely for extending our EAP access through July 14 — it made this round possible.

---

## Issues at a glance

| # | Observation |
|---|-------------|
| 1 | The **project-level default reasoning effort is ignored** — every new session comes up at the platform default (High). Proven today by a controlled double-canary experiment (Max → High, Extra → High); the model default (Fable 5) mapped correctly both times. |
| 2 | Correcting the **effort across multiple sessions in quick succession** triggers a Claude Code **desktop app auto-restart** — the trigger is consistent for us; cause not captured. |
| 3 | With more than 10 parallel sessions, transient rate-limit / "service busy" / API errors cluster around **session-create bursts**. |
| 4 | **Cross-session messages dropped mid-turn** — twice today (2026-07-10), both silently; recovered only via fallback channels. |
| 5 | The coordinator can create, list, and message sessions but **cannot close or archive one** — verified against the full coordinator tool registry. |
| 6 | **False "needs input" badges** — sessions flagged when nothing is actually pending. |
| 7 | **Actively-working sessions show "idle"** — the status appears to track only the main conversation turn. |

---

## What we would love (raised gently)

- **Make the project-level default Effort actually apply** to new and coordinator-spawned sessions; expose the effective effort to the session (or via an API) so it can be audited; unify the "Max" (settings) vs "Ultracode" (session control) naming.
- **A look at the desktop restart** on rapid effort changes across sessions — the one crash we can trigger reliably; we will gladly capture diagnostics.
- **Guidance on the fleet envelope** — is more than 10 parallel sessions in one Project within the intended envelope, and should session creation be paced?
- **Delivery guarantees for cross-session messages** — a receipt, a queued retry, or a dead-letter surface, so a coordination message can be delayed but never silently lost.
- **Coordinator-level session lifecycle (archive/close)** — ideally gated on a verified-complete check.
- **A "needs input" badge that clears** when nothing is actually pending.
- **State indicators that reflect background/agent activity** — or at minimum a last-activity timestamp.

---

## Open questions

- Is the desktop auto-restart under rapid effort changes across sessions a known crash class, and is there a log or crash report we could capture next time?
- Is a fleet of more than 10 parallel sessions in one Project within the intended beta envelope, and is there a recommended session-create pacing?
- What are the intended delivery semantics of cross-session messaging — must a coordinator verify receipt itself?
- Does a session close/archive capability exist server-side but unexposed? We could not find it in any coordinator-accessible tool.
- Is the session list's state indicator meant to track only the main conversation turn? That would explain both items 6 and 7.
- Is there any way for a session to audit its own effective reasoning effort? Both canaries confirmed no effort field exists anywhere in a session's own context.
- What is the relationship between the settings dropdown's "Max" and the session-side "Ultracode" (asked in round 2, still open — item 1 now attaches side-by-side evidence)?

---

## Details

### 1 — The project-level default reasoning effort is ignored: every new session comes up at the platform default (High)

Proven today (2026-07-10) by a controlled double-canary experiment. With Project Settings → Effort set to **Max**, a freshly-spawned canary read **"Fable 5 · High"** (effort popover confirming "Default (High)"); after changing the default to **Extra** and saving, a second canary again read **High**. The **model** default (Fable 5) mapped correctly in both spawns — model propagation works exactly as it should; the defect is effort-only — and day-to-day observation matches: every new session, coordinator-spawned or manual, comes up at High regardless of the configured default. Two sub-findings: **(a)** a session cannot introspect its own effort — no effort field exists anywhere in its own context, so the UI footer is the only place the truth is visible; **(b)** the settings dropdown says "Max" while the session-side control says "Ultracode" — the round-2 naming question, now with evidence attached.

**Why it matters:** the human must open and correct every session by hand — across more than 10 parallel sessions, exactly the toil the project default exists to remove — and the correction sweep itself triggers item 2.

**Suggested fix:** apply the project default Effort at session create; expose the effective effort programmatically; unify the Max/Ultracode naming.

### 2 — Desktop app auto-restarts when correcting the effort across multiple sessions in quick succession

Working around item 1 means going session to session changing the effort — and doing so in quick succession reproducibly restarts the Claude Code desktop app mid-fleet-operation. We have not captured a crash report, so we cannot say what dies; only that the trigger is consistent for us.

**Why it matters:** the workaround for the ignored default steers straight into the one crash we can reproduce, and we lose our place in the fleet each time.

**Suggested fix:** point us at a crash log or debug flag and we will capture diagnostics; fixing item 1 removes most of the trigger.

### 3 — Scale instability on session-create bursts (more than 10 parallel sessions)

At fleet scale we see recurring transient errors — rate-limit messages, "service busy", generic API errors, occasional sudden restarts — clustering when several sessions are spun up in a short burst, more than during steady-state work. As in round 3, we cannot isolate the cause (create burst vs. open-session count vs. coordinator traffic) — we share the pattern, not a proven mechanism.

**Why it matters:** batch spin-up is the coordinator's natural rhythm, so it repeatedly steers into the destabilizing maneuver.

**Suggested fix:** a documented safe create rate, or back-off-and-retry on session creation so a burst degrades to a queue rather than to errors.

### 4 — Cross-session messages drop mid-turn, silently

Twice today (2026-07-10), in two different fleet sessions, a `send_message` delivery was lost mid-turn with no delivery error surfaced to the sender: a verifier session's verdict (recovered via a PR-comment fallback plus a team-memory dead-drop) and a decision message from another session (recovered only later via team memory and re-confirmation). Both recoveries worked only because the sessions had independently built fallback channels.

**Why it matters:** a silently-dropped coordination message is worse than a failed one — the sender believes coordination happened, and the fleet quietly diverges.

**Suggested fix:** any of a delivery receipt, a queued retry, or a dead-letter surface — delayed is fine, silently lost is not.

### 5 — No way to close or archive a session from the coordinator

The coordinator can create, list, message, and read sessions, but no session close/archive/delete exists — verified against the full tool registry available to the coordinator. Completed sessions accumulate in the project view; today a watchdog session compiles an evidence-backed "safe to close" list that the human executes by hand. One honest hedge: the docs reference a server-side session-creation surface, so a close counterpart may exist internally, unexposed.

**Why it matters:** creation without retirement is half a lifecycle — the fleet only ever grows, and the decision that most needs to be safe-by-default falls back on the human.

**Suggested fix:** a coordinator-level archive/close, ideally gated on a verified-complete check.

### 6 — "Needs input" badge shows when nothing needs input

Sessions repeatedly show a "needs input" badge, yet opening them reveals no pending question, prompt, or permission request. We cannot see the badge's mechanism from our side, so we cannot say whether it is stale or mis-set.

**Why it matters:** the badge is the operator's primary triage signal at fleet scale — false positives cost round trips and teach the operator to distrust it.

**Suggested fix:** clear the badge when the input is resolved or was never actually pending.

### 7 — Actively-working sessions show as "idle"

The inverse of item 6: sessions demonstrably mid-work — background workers running, CI being driven — read "idle" in the session list, corroborated repeatedly by the coordinator's own activity digests and a watchdog's sweeps. The status appears to track only the main conversation turn, not background/agent activity.

**Why it matters:** together with item 6 the state column cannot be trusted in either direction — the operator either interrupts live work or leaves stuck sessions unattended.

**Suggested fix:** reflect background/agent activity in the state, or add a last-activity timestamp beside it.

---

## Screenshots (attached with the email)

1. **Project Settings showing Effort = Max (and model Fable 5)** (item 1) — already captured; the configured default before the first canary spawn.
2. **The first canary's footer and effort popover reading "High" while the settings said Max** (item 1) — already captured; footer "Fable 5 · High".
3. **The settings Effort dropdown open, showing "Default (High)" and Extra selected** (item 1) — already captured; the default changed to Extra ahead of the second spawn.
4. **The second canary's footer reading "High" under the Extra setting** (item 1) — already captured; the default is ignored regardless of its value.
5. **The desktop app immediately after an auto-restart** (item 2) — captured right after effort changes across several sessions in quick succession.
6. **The session list at fleet scale** (item 3) — more than 10 parallel sessions, the scale at which the create-burst errors appear.
7. **A completed session's view/menu showing no archive/close/delete affordance** (item 5).
8. **The "needs input" badge beside the same session open with nothing pending** (item 6) — one combined side-by-side image.
9. **The session list showing "idle" while that session's transcript shows work in progress at the same minute** (item 7) — one combined image, timestamps in both halves.

Screenshots 1–4 form the item-1 evidence sequence: configured default → spawned session's actual effort, across two configured values (Max, then Extra).

*Prepared 2026-07-10. Where we could not prove a cause, we have said so plainly rather than overstating it.*
