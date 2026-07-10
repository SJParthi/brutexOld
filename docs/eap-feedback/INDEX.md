# EAP Feedback — Living Dossier Index

> **Purpose.** A living index of the Claude Code Early Access Program (EAP)
> feedback rounds sent from this project to
> `claude-code-early-access@anthropic.com`. Each round is a batch of
> findings about **Claude Code itself** (the tool), gathered while building
> BruteX as the workload — never about BruteX product code. This index is
> maintained continuously: every new round gets a section here, with
> pointers to that round's artifacts in `docs/eap-feedback/`.
>
> **How rounds are added.** A finding enters a round only after it has been
> **verified with concrete evidence** (a log line, a file path, a git fact,
> a screenshot, or a directly observable session artifact — the round-2
> "Ground rule"). Anything unproven is labelled HELD / pending reproduction,
> never asserted as fact. When a round's files land in the repo, this index
> is updated with the file pointers; until then a round is honestly listed
> as "files: not yet committed".
>
> **Redaction note (permanent).** This index deliberately omits certain
> operational specifics of the test workload: exact parallel-session /
> fleet counts are recorded only as **"more than 10 parallel sessions"**,
> and model-configuration details are not recorded here. Keep both
> omissions when updating this file.
>
> Materials are Confidential per the Trusted Tester Agreement.

---

## Round 1 — Day-1 feedback (2026-06-26)

**Status:** sent · **response received** (reply from Product Ops recorded
via the committed follow-up; a live call for 2026-07-01 was arranged in it).

Feature-by-feature testing verdict + a 5-topic summary (use-case fit,
reliability, memory, proactivity, session-management UX) + 6 itemized
findings:

| # | Item (short title) | Severity |
|---|---|---|
| 1 | A lone agent self-certified a fake 3.57× benchmark (its own adversarial reviewer caught the real ~1.17× and blocked it) | S2 |
| 2 | The beta spawned a worktree on a disjoint git base | S2 |
| 3 | Long background jobs give no progress heartbeat | S2 |
| 4 | The autonomously-opened PR tripped the repo's own gates | S2 |
| 5 | Project memory isn't queryable (state lives in prose, not fields) | S3 |
| 6 | Scheduled routines were not reachable — **RETRACTED** in the round-1 follow-up (Routines verified working; retraction recorded honestly) | S3 |

**Files (in repo):**

- `CLAUDE_CODE_EAP_FEEDBACK.html` — the self-contained one-page dossier
  (also serves as the house-style template: inline CSS, shareable single
  file).
- `EAP_FEEDBACK_EMAIL.md` — the round-1 cover email (subject, body, send
  checklist).
- `EAP_REPLY_ROUND1_FOLLOWUP.md` — the recorded reply to Product Ops'
  response: the project-state-MCP elaboration, the routines retraction,
  and the 2026-07-01 call scheduling.

---

## Round 2 — Evidence-gated batch (2026-06-26 → 2026-06-27)

**Status:** sent (per operator record; note the committed email-draft file
still carries its pre-send "DRAFT" stamp). Responses: the 2026-07-01 call
with the eng lead was arranged in the round-1 follow-up; no per-item
written response is recorded in the repo.

Contents, per artifact:

| Artifact | Items | Notes |
|---|---|---|
| `EAP_ROUND2_SUBMISSION.md` / `.html` (prepared 2026-06-27) | 5 issues + 3 requests | (1) misleading GitHub-access 403 (blames an "org admin" on a personal account); (2) GitHub access drops mid-session with no in-session recovery; (3) connecting the GitHub App doesn't reach a running session; (4) background/review agents idle 5+ hours with no liveness signal or cleanup; (5) finished/orphaned agent worktrees aren't torn down. |
| `EAP_ROUND2_BATCH.md` (dated 2026-06-26) | P-1..P-14, V-1..V-7, I-1..I-5 | The full dossier: Projects-beta pilot feedback (documentation gaps, behavior/expectation gaps, feature requests, HELD items), 7 other verified findings, 5 ideas. Every item labelled VERIFIED or HELD. |
| `evidence/EVIDENCE.md` | F-1..F-7 | Evidence gallery: per-finding proof + a 2-row screenshot table. The image files themselves are **not stored in the repo** (the gallery rows are written and ready to receive them). |
| `EAP_ROUND2_EMAIL_DRAFT.md` | — | The round-2 email body (summarizes P/V/I/F buckets; attachments checklist; held-items caveat). |

---

## Round 3 — Session-state dossier (files: not yet committed)

**Status:** sent by email (per operator record). **Files: not yet
committed** — `EAP_ROUND3_SESSION_STATE.md` is referenced by three
committed round-2 files (`EAP_ROUND2_BATCH.md`, `EAP_ROUND2_EMAIL_DRAFT.md`,
`evidence/EVIDENCE.md`) but is not present in this repo, so its exact send
date and full item list are not recorded here.

Known contents (from the in-repo cross-references, current as of
2026-06-27):

| # | Item | Notes |
|---|---|---|
| F-8 | Session-start auth binding | Documents the session-start credential-binding behavior behind round 2's P-5/P-6 (mid-session GitHub-access drop; App connect not propagating). |
| F-9 | (title held) | Referenced by the round-2 draft as HELD pending a before/after artifact; the file carrying it is not in the repo, so no further detail is recorded here. |

When the round-3 files land in the repo, replace this section's pointers
with the real paths and complete the item list from the files themselves.

---

## Round 4 — Coordinator/session-management batch (2026-07-10)

**Status:** final — 7 items, awaiting operator send. (The interim v1
9-item, 7-item v2, and v3 copies were superseded in-place by v4 on
2026-07-10: v2 dropped the two model-mapping complaints after the
double-canary experiment showed model-default propagation working — the
defect is effort-only; v3 cut the submission's preamble — routing/thanks
moved into the email cover — and tightened line by line; v4 sharpened
item 5 to the delete framing and added the new merge-coordination item;
v6 cut the merge-coordination item; v7 removed session references + all
capture evidence; the sent package is submission + email only.)

7 items (titles as in `round4/EAP_ROUND4_SUBMISSION.md`):

| # | Item (short title) |
|---|---|
| 1 | The project-level default reasoning effort is ignored: every new session comes up at the platform default (High) — double-canary two-spawn evidence |
| 2 | Desktop app auto-restarts when correcting the effort across multiple sessions in quick succession |
| 3 | Scale instability on session-create bursts (more than 10 parallel sessions) |
| 4 | Cross-session messages drop mid-turn, silently |
| 5 | Sessions cannot be DELETED from the coordinator |
| 6 | "Needs input" badge shows when nothing needs input |
| 7 | Actively-working sessions show as "idle" |

**Files (in repo):**

- `round4/EAP_ROUND4_SUBMISSION.md` — the full round-4 dossier (7 items:
  issues at a glance, requests, open questions, per-item details).
- `round4/EAP_ROUND4_SUBMISSION.html` — the self-contained one-page HTML
  twin (inline CSS, house style).
- `round4/EAP_ROUND4_EMAIL.md` — the round-4 cover email (subject, body,
  send checklist; same 7 items).

---

## Supporting artifacts (not a round)

| File | What it is |
|---|---|
| `README.md` | Directory purpose, the 5-topic structure, and the consolidation notes for the combined BruteX + TickVault submission. |
| `SCOREBOARD_LATEST.md` | The customer-facing BruteX product scoreboard published alongside the feedback evidence (context for reviewers; refreshed 2026-06-26). |
| `CLAUDE_CODE_EAP_FEEDBACK.html`, `EAP_ROUND2_SUBMISSION.html` | Self-contained one-page HTML dossiers with inline CSS — the de-facto house style for shareable round submissions (no separate template/CSS file exists). |
