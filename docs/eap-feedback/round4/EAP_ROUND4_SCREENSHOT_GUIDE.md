# 📸 EAP Round-4 Screenshot Guide — what to capture & the caption for each

Checklist for the NINE screenshots attached to the round-4 EAP email, in the
submission's order (each "Backs" points at the submission's 7-item issue
table). Entries 1–4 (the item-1 effort-evidence sequence) were captured
2026-07-10 — just attach them; entries 5–9 still need capturing on the Mac.

Capture tips (macOS): `⇧⌘4` region grab, `⇧⌘4` then `Space` for a clean
single-window grab, `⇧⌘5` for a timed capture; files land on the Desktop. If a
capture can't be reproduced today, skip its row — every finding stands on the
prose in `EAP_ROUND4_SUBMISSION.md`.

**Cropping rule for every sessions-LIST shot:** no more than ~10 sessions ever
visible. Screenshot 6 is the one shot that must prove "more than 10" — frame
just enough rows that the count is visibly past ten and cut the list off
mid-scroll; the list halves in 8 and 9 need only the one session's row plus a
few neighbours. Crop or redact any session titles you'd rather not share.

---

## Screenshots to attach (in this order)

1. **Project Settings showing Effort = Max (and model Fable 5).** Already
   captured (2026-07-10) — the configured default before the first canary
   spawn.
   **Caption:** "The Project-level default Effort IS configured (Max) — the
   setting the next screenshot shows being ignored." — **Backs:** item 1.

2. **Canary-1 footer + effort popover reading "High".** Already captured —
   footer "Fable 5 · High", popover "Default (High)", while the project
   default said Max.
   **Caption:** "The canary spawned under the Max default comes up at High —
   the model default mapped, the effort default did not." — **Backs:** item 1.

3. **The settings Effort dropdown open — "Default (High)" visible, Extra
   selected.** Already captured — the default changed to Extra ahead of the
   second spawn (also shows the settings-side naming for the terminology
   point).
   **Caption:** "The project default changed to Extra for the second spawn —
   note the dropdown's own vocabulary ('Default (High)', 'Extra') vs the
   session-side tier names." — **Backs:** item 1.

4. **Canary-2 footer reading "High" under the Extra setting.** Already
   captured — the second canary again at High: the configured default ignored
   regardless of its value.
   **Caption:** "The second canary, spawned under the Extra default, again
   comes up at High — the project-level Effort is ignored whatever it is set
   to." — **Backs:** item 1.

5. **The desktop app immediately after an auto-restart.** Grab the
   just-relaunched window (`⇧⌘4` + `Space`) with the menu-bar clock in frame;
   trigger by changing the effort across 3–4 sessions in quick succession,
   only AFTER the fleet is at a safe checkpoint. If you also grab a
   before-frame, combine the two into ONE side-by-side image so the attachment
   count stays at NINE.
   **Caption:** "Captured right after the desktop app restarted itself —
   triggered, reproducibly, by changing the effort across several sessions in
   quick succession (the exact manual workaround item 1 forces us into)."
   — **Backs:** item 2.

6. **The sessions list at fleet scale (more than 10 parallel sessions).**
   Region-grab the Project's sessions list; frame just enough rows that the
   count is visibly past ten, cut off mid-scroll, and keep any total-count
   badge or footer OUT of frame — the row count is the evidence, not the
   names.
   **Caption:** "One Project running more than 10 parallel sessions — the
   scale at which we see the transient rate-limit / 'service busy' errors,
   clustering around session-create bursts." — **Backs:** item 3.

7. **A completed session's menu open — no archive/close/delete affordance
   anywhere.** Open a session from the watchdog's "safe to close" list, open
   its fullest menu (kebab or right-click row menu), and single-window grab
   with the final "done/merged" message visible behind the menu.
   **Caption:** "A completed session with its menu open — create, list, and
   message all exist for the coordinator, but no close/archive/delete exists
   anywhere, so finished sessions accumulate until a human retires each one by
   hand." — **Backs:** item 5.

8. **Side-by-side: the "needs input" badge vs the same session with nothing
   pending.** ONE combined image — left: the list row wearing the badge
   (cropping rule applies); right: that session moments later, transcript
   bottom + empty composer, no dialog or permission prompt; the SAME session
   name legible in both halves.
   **Caption:** "The same session, moments apart: the list says 'needs input'
   (left); opening it finds no pending question, prompt, or permission request
   (right) — the false badge that sends the operator into sessions for
   nothing." — **Backs:** item 6.

9. **Side-by-side: "idle" in the list vs the same session mid-work, same
   minute.** ONE combined image — left: the list showing "idle" (cropping rule
   applies); right: the transcript with work demonstrably in flight; a legible
   timestamp (menu-bar clock or message timestamps) in EACH half reading the
   same minute.
   **Caption:** "The same minute, both halves timestamped: the list reads
   'idle' (left) while the session's own transcript shows work mid-flight
   (right) — the state appears to track only the main conversation turn, not
   background/agent activity." — **Backs:** item 7.

*(Item 4 — the mid-turn cross-session message drops — has no screenshot: the
losses were silent, which is precisely the point. The PR-comment +
team-memory fallbacks are described in the submission text.)*

---

## Send checklist (2 lines)

1. **Reply to Diana's existing EAP thread** (To:
   `claude-code-early-access@anthropic.com`) → paste the round-4 email body
   and attach BOTH `EAP_ROUND4_SUBMISSION.md` AND its HTML render (rename on
   send to `BruteX-ClaudeCode-EAP-Round4.md` / `BruteX-ClaudeCode-EAP-Round4.html`).
2. **Attach screenshots 1–9** in the order above (the side-by-side pairs in 8
   and 9 each count as ONE image; 1–4 are already captured — just attach),
   each with its caption pasted beside it → send today (2026-07-10), well
   before the July-14 window closes.
