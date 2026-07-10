# Claude Code Projects (beta) — Round-4 Feedback

> Detailed observations, requests, and open questions accompanying the round-4 feedback email. Prepared 2026-07-10. Where we could not prove a cause, we have said so plainly.

**For:** Claude Code Early Access team — Diana Liu (claude-code-early-access@anthropic.com)
**From:** Parthiban (sjparthi93@gmail.com) · **Date:** 2026-07-10
**Workload under test:** BruteX — now run as a coordinator-orchestrated fleet of more than 10 parallel sessions in one Project.

This round is **coordinator-scale** feedback. Since round 3 our usage pattern has changed: we now run **one coordinator session that dispatches, sequences, and monitors a fleet of more than ten child sessions** against a single repository. We want to say up front that the pattern itself works remarkably well — the coordinator reliably sequences the fleet, a fleet-wide broadcast has reached every one of its target sessions without a miss, and the sessions successfully deduplicate shared resources (for example merge slots) between themselves. Everything below is about removing the remaining toil and instability at that scale, not about the pattern failing. And thank you sincerely for extending our EAP access through July 14 — it is what made this round of observations possible.

---

## Issues at a glance

| # | Observation |
|---|-------------|
| 1 | The model selected when **scheduling or spinning up a session from the coordinator does not map onto the new session**: the session comes up on the default model and must be corrected by hand. Observed repeatedly. |
| 2 | Even with the **Project Settings default model** (and the coordinator's own model) configured, each session **still had to be opened and its model re-selected manually** across the fleet, every time — pure setup toil at fleet scale. |
| 3 | Changing the **model or the reasoning effort across multiple sessions in quick succession** triggers a sudden Claude Code **desktop app auto-restart** — reproducibly, and disruptively mid-fleet-operation. |
| 4 | With the fleet at more than 10 open sessions we see recurring transient rate-limit / "service busy" / API errors and sudden restarts — **especially when spinning up new sessions** (session-create bursts). This is round 3's throttle item, now observed at larger scale with a new trigger datum. |
| 5 | **Cross-session messages drop mid-turn**: twice today (2026-07-10), in two different fleet sessions, a `send_message` delivery was lost mid-turn — in one case detected by the sender only indirectly, in the other silently — and coordination recovered only through fallback channels. |
| 6 | The coordinator can **create, list, and message sessions — but cannot close or archive one**. Verified against the full tool registry available to the coordinator: no session close/archive/delete exists anywhere. Completed sessions accumulate in the project view, and the human has to inspect each one by hand to know what is safe to close. |
| 7 | Sessions repeatedly show a **"needs input" badge when nothing actually needs input** — opening the session reveals no pending question, prompt, or permission request. In a fleet of more than 10 sessions the human triages by these badges, so a false one sends them into sessions for nothing. Repeatedly observed; mechanism not visible to us. |
| 8 | The inverse of 7: **actively-working sessions show as "idle"** in the session list — sessions demonstrably mid-work (background workers running, CI being driven) read idle, corroborated by the coordinator's own activity digests. The status appears to track only the main conversation turn, not background/agent activity. |
| 9 | The **project-level default reasoning effort is ignored** — every new session comes up at the platform default ("High"). Confirmed today by a controlled two-spawn experiment: project defaults of Max and then Extra both produced sessions reading "High", while the model default (Fable 5) mapped correctly in both spawns. |

---

## What we would love (raised gently)

- **Honor the model selected at spawn time** — when a session is created or scheduled from a coordinator with an explicitly selected model, the new session should come up on exactly that model.
- **Have the Project Settings default model actually propagate** — we have it configured, yet every new and spawned session still needed its model re-selected by hand. Either the project default taking effect or the spawn-time selection being honored (ideally both) would remove the largest single piece of manual overhead we have at fleet scale.
- **A look at the desktop restart** when the model is changed across several sessions in quick succession — it is the one crash we can trigger reliably.
- **Guidance on the fleet envelope** — whether running more than ten parallel sessions in one Project is within the intended envelope, and whether session creation should be paced (and if so, at what rate) to avoid the create-burst instability.
- **Delivery guarantees for cross-session messages** — a delivery receipt/acknowledgement visible to the sender, a queued retry on a failed mid-turn delivery, or a dead-letter surface where undelivered messages land — so a coordination message is never silently lost.
- **Coordinator-level session lifecycle (archive/close)** — a counterpart to session creation, ideally gated on a verified-complete check, so a coordinator or watchdog can retire finished sessions instead of leaving the human to inspect and close each one by hand.
- **A "needs input" badge that clears when the input is resolved** — so the badge can be trusted as the triage signal it is at fleet scale.
- **State indicators that reflect background/agent activity** — or at minimum a last-activity timestamp alongside the state — so a session deep in a long background task does not read "idle" the whole time.
- **Make the project-level default Effort actually apply** — to new and coordinator-spawned sessions alike; expose the effective effort to the session itself (or via an API) so it can be audited without opening the UI; and unify the naming between the settings dropdown ("Max") and the session-side control ("Ultracode"/tier names).

---

## Open questions

- Is the **Project Settings default model meant to apply** to new and coordinator-spawned sessions? We have it configured, and sessions still come up needing a manual model re-select — we cannot tell whether that is a bug or an unimplemented mapping.
- Are sessions spawned or scheduled from a coordinator **meant** to come up on the model selected at spawn time (or the coordinator's model)? The same uncertainty as above, at the spawn boundary.
- Is a fleet of more than ten parallel sessions in one Project within the intended envelope for the beta — and is there a recommended pacing for session creation so that create bursts do not trip the transient throttling?
- Is the desktop auto-restart under rapid model changes across sessions a known crash class, and is there a log or crash report we could capture next time that would help you pinpoint it?
- What are the intended delivery semantics of cross-session messaging — is a mid-turn delivery delivered without guarantees by design, and should a coordinator assume it must verify receipt itself?
- Is the session list's **state indicator meant to track only the main conversation turn**? That would explain both the lingering "needs input" badges and the working-sessions-reading-idle pattern — we cannot see the mechanism from our side.
- Does a **session close/archive capability exist server-side but unexposed**? The platform's docs reference a session-creation surface server-side, so a close counterpart may exist internally — we could not find it in any coordinator-accessible tool.
- Is there any way for a **session to audit its own effective reasoning effort**? Both canaries in our item-9 experiment confirmed that no effort field exists anywhere in a session's own context — the UI footer is the only place the truth is visible, so there is no programmatic way to verify what a session is actually running at.
- What is the **relationship between the settings dropdown's "Max" and the session-side "Ultracode"** (and the tier names in between)? We asked this in round 2 and it remains unanswered — item 9 now attaches evidence of the two vocabularies side by side.

---

## Details

### 1 — The model selected at spawn time is not mapped onto the new session

When we create or schedule a new session **from the coordinator** and explicitly select a model for it, the session comes up on the default model — the selection is simply not mapped onto the spawned session. We have observed this repeatedly, not once; it appears to be the consistent behavior rather than an intermittent failure. The result is that, in every case we have checked, the session the coordinator spins up begins its work on the default model — not the one selected for it — until someone notices and corrects it by hand.

**Why it matters:** the whole point of a coordinator is unattended dispatch. If the spawned session silently ignores the model it was created with, work that was meant to run on a specific model quietly runs on the default — the same silent-downgrade concern we raised as an open question in round 2, now appearing at the spawn boundary instead of after an idle.

**Suggested fix:** honor the model selection at session-create time, and reflect the applied model back in the new session's UI so a mismatch is visible immediately.

### 2 — The Project Settings default model does not take effect: every session re-selected by hand

Even with the Project Settings default model configured — and the coordinator itself on the intended model — every session in the fleet still had to be opened individually and its model re-selected by hand, every time. This is pure hand-work: it is slow, it is error-prone (a missed session runs on the default model unnoticed), and it directly feeds item 3 below, since the fastest way to do it is to click through sessions in quick succession — which is exactly what triggers the desktop restart.

**Why it matters:** at 3–5 sessions the manual step is a shrug; at more than ten it dominates fleet-setup time and undermines the unattended operation the coordinator pattern otherwise delivers.

**Suggested fix:** have the Project Settings default model propagate to every new and coordinator-spawned session, with per-session override preserved. If the spawn-time selection (item 1) were honored as well, the two together would eliminate this entirely.

### 3 — Desktop app auto-restarts when changing the model or effort across sessions in quick succession

While working through the manual per-session correction above — changing the model, and equally the reasoning effort (item 9) — the Claude Code desktop app **suddenly restarts** — reproducibly. The pattern is: go session to session changing the model or the effort, and the app restarts on its own. We have not captured a crash report, so we cannot say what dies; we can only say the trigger (rapid successive model-or-effort changes across sessions) is consistent for us.

**Why it matters:** the restart lands mid-fleet-operation — in the middle of configuring or supervising more than ten live sessions — and we lose our place in the fleet each time. Combined with items 2 and 9, the current experience is that the one workaround for the non-propagating defaults is itself the thing that crashes the app.

**Suggested fix:** we would gladly capture whatever diagnostics would help (a crash log location or a debug flag). Structurally, of course, items 1, 2, and 9 remove most of the reason to perform rapid model-or-effort changes at all.

### 4 — Scale instability with more than 10 open sessions, especially on session-create bursts

With the fleet at more than 10 open sessions we see recurring transient errors — rate-limit messages, "service busy", generic API errors, and occasional sudden restarts. The clearest new datum since round 3 is the trigger: the errors cluster **when we are spinning up new sessions**, particularly several in a short burst, more than during steady-state work. We want to be careful here, exactly as in round 3: we cannot isolate whether the cause is the create burst itself, the total open-session count, the coordinator's own traffic, or the overall scale — we share this as the pattern we observed, not a proven mechanism. In our observation the instability concentrates around fleet growth.

**Why it matters:** the coordinator's natural rhythm is to spin up batches of sessions as work becomes ready. If the create burst is the trigger, the coordinator is repeatedly steering into the one maneuver that destabilizes the fleet — and a paced-create guideline (or server-side smoothing) would let us avoid it entirely.

**Suggested fix:** guidance on a safe session-create rate and fleet ceiling; or, better, back-off-and-retry on session creation so a burst degrades to a queue rather than to errors.

### 5 — Cross-session messages drop mid-turn, silently

Twice today (2026-07-10), in two different fleet sessions, a cross-session `send_message` was lost mid-turn. The messaging layer surfaced no explicit delivery error to either sender — one sender inferred the failure indirectly and fell back, the other loss went unnoticed until later:

- A **verifier session's verdict delivery** failed mid-turn; it fell back to posting the verdict as a PR comment and leaving a dead-drop entry in team memory.
- A **decision message from another session** was lost mid-turn and only recovered later via team memory and direct re-confirmation.

Both recoveries worked, but only because the sessions had independently built fallback channels (PR comments, team memory) — the messaging layer itself gave no receipt, no retry, and no record of the failure visible to us.

**Why it matters:** in a coordinator-orchestrated fleet, cross-session messages carry verdicts, decisions, and sequencing instructions. A silently-dropped message is worse than a failed one: the sender believes coordination happened, the recipient never knew it was attempted, and the fleet quietly diverges until someone reconciles by hand.

**Suggested fix:** any one of — a delivery receipt/acknowledgement the sender can check; a queued retry when a mid-turn delivery fails; or a dead-letter surface where undelivered messages land visibly. With any of the three, a coordination message can be delayed but never silently lost.

### 6 — No way to close or archive a session from the coordinator

The coordinator session can create a child session, list the fleet, and message any session — but there is no counterpart to close, archive, or delete one. We verified this against the full tool registry available to the coordinator: session-creation, listing, messaging, transcript-reading, and Routine/trigger management all exist; no session close/archive/delete exists anywhere. The consequence at fleet scale: completed and merged sessions accumulate in the project view, and the human operator has no reliable way to know which of more-than-ten sessions are truly safe to close without inspecting each one by hand. Today we work around it by having a watchdog session compile an evidence-backed "safe to close" list that the human then executes manually in the UI. One honest hedge: the platform's docs reference a session-creation surface server-side, so a close counterpart may exist internally — but it is not exposed to any coordinator-accessible tool we could find.

**Why it matters:** the coordinator pattern's promise is a fleet that manages itself. Creation without retirement is half a lifecycle: the fleet only ever grows, the project view fills with finished sessions that look just like live ones, and the one task that most needs to be safe-by-default — deciding what can be closed — falls back on the human.

**Suggested fix:** coordinator-level session lifecycle (archive/close), ideally gated on a verified-complete check so a coordinator cannot retire a session that still has work in flight.

### 7 — "Needs input" badge shows when nothing needs input

Sessions repeatedly show a "needs input" indicator in the session list, but opening the session reveals no pending question, prompt, or permission request — nothing to act on at all. In a fleet of more than ten sessions the human triages by these badges, so a false "needs input" sends them into sessions for nothing. We report this as a repeatedly-observed pattern; we cannot see the indicator's mechanism from our side, so we cannot say whether the badge is stale, mis-set, or tracking something invisible to us.

**Why it matters:** the badge is the operator's primary triage signal at fleet scale. Every false positive costs an open-inspect-close round trip, and — worse — teaches the operator to distrust the signal, which blunts it for the sessions that genuinely do need input.

**Suggested fix:** a "needs input" badge that clears when the input is resolved (or was never actually pending).

### 8 — Actively-working sessions show as "idle"

The inverse of item 7. Sessions that are demonstrably mid-work — long background workers running, CI being driven, folds executing — show "idle" in the session list. Here we can say more than "observed": the coordinator's own activity digests and a watchdog session's sweep cycles repeatedly confirmed real work in progress during windows where the list read idle. The status appears to track only the main conversation turn, not background/agent activity — so a session deep in a half-hour background task reads idle the whole time.

**Why it matters:** together with item 7 this means the list's state column cannot be trusted in either direction — it flags sessions that need nothing and rests sessions that are hard at work — which is precisely where a fleet operator needs it most. An operator who cannot tell working from idle either interrupts live work or leaves genuinely stuck sessions unattended.

**Suggested fix:** state indicators that reflect background/agent activity — or at minimum a last-activity timestamp alongside the state, so the operator can distinguish "idle for forty minutes" from "turn ended but workers still running" — and, per item 7, a "needs input" badge that clears when resolved.

### 9 — The project-level default reasoning effort is ignored: every new session comes up at the platform default ("High")

This is our best-evidenced finding this round, because unlike the observational items above we could run a controlled, repeated experiment. Today (2026-07-10) we ran a two-spawn experiment from the coordinator. First, with Project Settings → Effort set to **Max**, we spawned a fresh canary session: its footer read **"Fable 5 · High"**, and the effort popover confirmed High — i.e. the dropdown's "Default (High)". Second, we changed the project default to **Extra**, saved, and spawned a second canary: its footer again read **High**. In both spawns the **model** default (Fable 5) mapped correctly — so the defect is specific to the effort setting: it appears to be ignored entirely in favor of the platform default, for coordinator-spawned sessions in the experiment and, per our day-to-day observation, for manually created sessions as well.

Two sub-findings from the same experiment: **(a)** a session cannot introspect its own effort tier — both canaries confirmed that no effort field exists anywhere in a session's own context, so the UI footer is the only place the truth is visible and there is no way to audit this programmatically; **(b)** the terminology is inconsistent — the settings dropdown says "Max" while the session-side control uses different naming ("Ultracode"/tier names), the same Max-vs-Ultracode relationship we asked about in round 2, still unanswered and now with evidence attached.

**Why it matters:** the human must open every session and manually correct the effort — across more than 10 parallel sessions this is the exact toil the project-level default exists to remove. And per item 3, doing that correction across multiple sessions in quick succession is precisely what triggers the desktop auto-restart — the workaround for the ignored default steers straight into the one crash we can reproduce.

**Suggested fix:** make the project default Effort actually apply to new and coordinator-spawned sessions; expose the effective effort to the session (or via an API) so it can be audited; and unify the Max/Ultracode naming.

---

## Screenshots (attached with the email)

The following screenshots are attached with the email:

1. **Project Settings showing the configured default model** (item 2) — the default is set at the Project level, yet new sessions do not come up on it.
2. **A freshly-spawned session on the default model instead of the configured one** (items 1 and 2) — the model selected at spawn time and the Project default both absent from the new session.
3. **The desktop app immediately after an auto-restart** (item 3) — captured right after changing the model across several sessions in quick succession.
4. **The session list at fleet scale** (item 4) — the Project running more than 10 parallel sessions, the scale at which the create-burst errors appear.
5. **A completed session's view/menu showing no archive/close/delete affordance** (item 6) — creation, listing, and messaging all exist; no lifecycle counterpart does.
6. **The session list showing a "needs input" badge beside the same session opened with no pending prompt visible** (item 7) — one combined side-by-side image.
7. **The session list showing "idle" while that session's transcript shows work in progress at the same minute** (item 8) — one combined image, timestamps visible in both halves.
8. **Project Settings showing Effort = Max (and model Fable 5)** (item 9) — already captured today; the configured project default before the first canary spawn.
9. **The first canary session's footer and effort popover reading "High" while the settings said Max** (item 9) — already captured today; footer "Fable 5 · High", popover confirming High.
10. **The settings Effort dropdown open, showing "Default (High)" and Extra selected** (item 9) — already captured today; the project default changed to Extra ahead of the second spawn.
11. **The second canary's footer reading "High" under the Extra setting** (item 9) — already captured today; the second spawn confirming the configured default is ignored regardless of its value.

Screenshots 8–11 together form the item-9 evidence sequence: configured default → spawned session's actual effort, repeated across two configured values (Max, then Extra).

*Prepared 2026-07-10. Where we could not prove a cause, we have said so plainly rather than overstating it.*
