# 📸 EAP Round-4 Screenshot Guide — what to capture & the caption for each

Dead-simple checklist for capturing and attaching the visual evidence for the
round-4 EAP feedback email. Unlike round 1, screenshots 1–7 still need to be
CAPTURED — each entry below tells you exactly what to capture on the Mac, where
to find it, what the frame must show, and the caption to paste next to it so a
non-technical reader instantly gets what it shows. Screenshots 8–11 (the item-9
effort experiment) were already captured today (2026-07-10) — just attach them.

Capture tips (macOS): `⇧⌘4` for a region grab, `⇧⌘4` then `Space` for a clean
single-window grab, `⇧⌘5` if you want to set a timed capture. Files land on the
Desktop by default. If a capture can't be reproduced today, skip its row — every
written finding still stands on the prose in `EAP_ROUND4_SUBMISSION.md`.

**Cropping rule for every sessions-LIST shot:** crop so no more than ~10
sessions are ever visible in the frame. Screenshot 4 is the one shot that must
prove "more than 10" — even there, frame just enough rows that the count is
visibly past ten and cut the list off mid-scroll; the list halves in
screenshots 6 and 7 need only the
one session's row plus a handful of neighbours. Crop or redact any session
titles you'd rather not share.

---

## Screenshots to attach (in this order)

1. **Project Settings — the configured default model.**
   - **What to capture:** the BruteX Project's settings page with the default
     model setting clearly visible and set.
   - **Where to find it:** Claude Code desktop app → open the BruteX Project →
     Project Settings (the gear icon) → **General** → the **Default model**
     selector.
   - **What it must show:** the Project name (so it's clearly THIS project) and
     the default-model dropdown showing the model we configured. Use the
     single-window grab (`⇧⌘4` + `Space`) so the whole settings pane is in frame.
   - **Caption:** "The Project-level default model IS configured — this is the
     setting we expected every new session to inherit."
   - **Backs:** issue **2** (and sets up screenshot 2, which shows the setting
     not taking effect).

2. **A freshly-spawned session sitting on the default model instead.**
   - **What to capture:** a brand-new session — ideally one the coordinator just
     spawned with an explicit model selected — with its model selector open,
     showing the app default rather than the model selected at spawn time or the
     Project default.
   - **Where to find it:** have the coordinator spin up one session (or schedule
     one), open it immediately from the sessions list, then click the model
     selector in the composer (bottom of the chat input) so the currently-active
     model is readable in the frame.
   - **What it must show:** (a) the session is clearly fresh — empty transcript
     or first turn only; (b) the active model is the default, NOT the one
     configured in screenshot 1. Capture this one shortly after screenshot 1 so
     the pair reads as a direct contradiction.
   - **Caption:** "A session spawned moments later: it comes up on the default
     model — the model selected at spawn time and the Project default are both
     absent, and we correct it by hand, per session, across the whole fleet."
   - **Backs:** issues **1 and 2**.

3. **The desktop app immediately after an auto-restart.**
   - **What to capture:** the Claude Code desktop window in the seconds right
     after it restarts itself — the relaunched/fresh-loading state (sessions
     list re-populating, whatever the app shows on relaunch).
   - **Where to find it:** the restart triggers reproducibly while changing the
     model across several sessions in quick succession. Do this only AFTER the
     fleet's in-flight work is at a safe checkpoint; then step through 3–4
     sessions changing the model, and when the app restarts, grab the window
     immediately (`⇧⌘4` + `Space`). Include the macOS menu-bar clock in frame so
     the timing is self-evident; if you can, also grab a frame seconds BEFORE
     (mid-model-change) so the before/after pair tells the story — but combine
     the two frames into ONE side-by-side before/after image, so the attachment
     count stays at the ELEVEN screenshots the submission's Screenshots section
     and the send checklist state.
   - **What it must show:** the just-relaunched app state, timestamped. There is
     no crash dialog to capture (the app restarts silently) — the relaunch state
     plus your caption is the evidence.
   - **Caption:** "Captured right after the desktop app restarted itself —
     triggered, reproducibly, by changing the model across several sessions in
     quick succession (the exact manual workaround items 1–2 force us into)."
   - **Backs:** issue **3**.

4. **The sessions list at fleet scale (10+ parallel sessions).**
   - **What to capture:** the Project's sessions list/sidebar showing the fleet
     running in parallel.
   - **Where to find it:** Claude Code desktop app → the BruteX Project → the
     sessions list. Size the window (or use a region grab) so a screenful of
     active sessions is visible.
   - **What it must show:** more than 10 parallel sessions in ONE Project — the
     scale at which the create-burst errors appear. **Crop deliberately:** frame
     just enough rows to show the count is past ten and cut the list off
     mid-scroll, keeping any total-count badge or footer OUT of frame — the
     email says "more than 10", and the
     screenshot should show exactly that and no more. Crop or redact any session
     titles you'd rather not share; the row count is the evidence, not the names.
   - **Caption:** "One Project running more than 10 parallel sessions — the
     scale at which we see the transient rate-limit / 'service busy' errors,
     clustering around session-create bursts."
   - **Backs:** issue **4**.

5. **A completed session with no archive/close/delete affordance anywhere (menu open).**
   - **What to capture:** a clearly-completed session — its work merged/done —
     open in the desktop app, with the session's menu (the kebab/… menu, or
     whatever menu could plausibly hold a lifecycle action) OPEN, so every
     available option is readable in the frame.
   - **Where to find it:** Claude Code desktop app → the BruteX Project → open
     any session from the watchdog's "safe to close" list → open the session
     menu. If the sessions list offers a right-click/row menu too, prefer the
     one with MORE options in it — the point is that even the fullest menu has
     no close/archive/delete.
   - **What it must show:** (a) the session is clearly finished — the final
     "done/merged" message visible in the transcript behind the menu; (b) the
     menu fully open with every option legible, none of them a
     close/archive/delete. One single-window grab (`⇧⌘4` + `Space`) with the
     menu held open does it.
   - **Caption:** "A completed session with its menu open — create, list, and
     message all exist for the coordinator, but no close/archive/delete exists
     anywhere, so finished sessions accumulate until a human retires each one
     by hand."
   - **Backs:** issue **6**.

6. **Side-by-side: the "needs input" badge vs the same session with nothing pending.**
   - **What to capture:** TWO frames combined into ONE side-by-side image —
     LEFT: the sessions list showing a session wearing the "needs input" badge;
     RIGHT: that same session opened moments later, scrolled to the bottom of
     the transcript with the composer in frame, showing NO pending question,
     prompt, or permission request.
   - **Where to find it:** the badge appears repeatedly across the fleet — when
     you spot one, region-grab (`⇧⌘4`) the list around that row (per the
     cropping rule: the row plus a handful of neighbours, never more than ~10
     sessions), then open the session and grab the transcript bottom + composer.
     Combine the two frames into ONE image, matching the "one combined
     side-by-side image" the submission's Screenshots section states.
   - **What it must show:** the SAME session name legible in both halves (so the
     pair is verifiable), the badge readable on the left, and the right half
     unambiguously showing nothing to act on — transcript end + empty composer,
     no dialog, no permission prompt.
   - **Caption:** "The same session, moments apart: the list says 'needs input'
     (left); opening it finds no pending question, prompt, or permission request
     (right) — the false badge that sends the operator into sessions for
     nothing."
   - **Backs:** issue **7**.

7. **Side-by-side: "idle" in the list vs the same session mid-work, same minute.**
   - **What to capture:** TWO frames combined into ONE side-by-side image —
     LEFT: the sessions list showing a session as "idle"; RIGHT: that same
     session's transcript at the SAME minute showing work demonstrably in
     progress (a background worker streaming output, CI being driven) — with
     timestamps visible in BOTH halves so the same-minute claim is
     self-evident.
   - **Where to find it:** pick a session the coordinator's activity digest says
     is mid-work (a long background task is ideal — it reads idle the whole
     time). Region-grab the list around its row (cropping rule: ~10 sessions
     max), then immediately grab the session's transcript. Include the macOS
     menu-bar clock in each frame — or make sure in-transcript message
     timestamps are legible — so both halves carry the same minute.
   - **What it must show:** the SAME session name legible in both halves; "idle"
     readable on the left; visible in-flight work on the right; a timestamp in
     EACH half reading the same minute.
   - **Caption:** "The same minute, both halves timestamped: the list reads
     'idle' (left) while the session's own transcript shows work mid-flight
     (right) — the state appears to track only the main conversation turn, not
     background/agent activity."
   - **Backs:** issue **8**.

8. **Project Settings showing Effort = Max (and model Fable 5).**
   - **Status:** already captured today (2026-07-10) — no capture needed, just
     attach. Shows the Project Settings pane with the default Effort set to Max
     (and the default model Fable 5), the configured state before the first
     canary spawn.
   - **Caption:** "The Project-level default Effort IS configured (Max) — the
     setting the next screenshot shows being ignored."
   - **Backs:** issue **9**.

9. **Canary-1 footer + effort popover reading "High".**
   - **Status:** already captured today (2026-07-10) — no capture needed, just
     attach. Shows the first canary session's footer reading "Fable 5 · High"
     with the effort popover open confirming High (the dropdown's "Default
     (High)"), while the project default said Max.
   - **Caption:** "The canary spawned under the Max default comes up at High —
     the model default mapped, the effort default did not."
   - **Backs:** issue **9**.

10. **The settings Effort dropdown open — "Default (High)" visible, Extra selected.**
    - **Status:** already captured today (2026-07-10) — no capture needed, just
      attach. Shows the settings Effort dropdown open with "Default (High)"
      visible and Extra selected — the project default changed to Extra ahead
      of the second spawn (and the settings-side naming for the terminology
      point).
    - **Caption:** "The project default changed to Extra for the second spawn —
      note the dropdown's own vocabulary ('Default (High)', 'Extra') vs the
      session-side tier names."
    - **Backs:** issue **9**.

11. **Canary-2 footer reading "High" under the Extra setting.**
    - **Status:** already captured today (2026-07-10) — no capture needed, just
      attach. Shows the second canary session's footer again reading High while
      the project default was Extra — the configured default ignored regardless
      of its value.
    - **Caption:** "The second canary, spawned under the Extra default, again
      comes up at High — the project-level Effort is ignored whatever it is set
      to."
    - **Backs:** issue **9**.

*(Issue 5 — the mid-turn cross-session message drops — has no screenshot: the
losses were silent, which is precisely the point made in the write-up. The PR
comment + team-memory fallbacks are described in the submission text.)*

---

## Send checklist (2 lines)

1. **Reply to Diana's existing EAP thread** (To:
   `claude-code-early-access@anthropic.com`) → paste the round-4 email body and
   attach BOTH `EAP_ROUND4_SUBMISSION.md` AND its HTML render (rename on send to
   `BruteX-ClaudeCode-EAP-Round4.md` / `BruteX-ClaudeCode-EAP-Round4.html` —
   same names the email calls for).
2. **Attach screenshots 1–11** in the order above (the two side-by-side pairs
   in 6 and 7 each count as ONE combined image; 8–11 are already captured today
   — just attach them), each with its caption pasted beside it in the email
   body → send today (2026-07-10) so it lands well before the July-14 access
   window closes.
