# prmeta payload — PR #1629 BODY UPDATE (not a new PR)

Apply `PR_BODY.md` (byte-exact) as the DESCRIPTION of the EXISTING draft
PR #1629 (`claude/rust-slice-t11-resample` → main). TITLE UNCHANGED.

Why: two PR-body gates are red on the current body —
1. `dec-id-validator` — the body cites DEC-FEED-RESAMPLER-DELETE-001
   (the #1947-earmarked follow-up) unbackticked; it has no MIGRATION.md
   ledger-entry header, so the EXISTENCE check exits 1. Fixed by
   backticking the token (the house workaround — see the
   dec-id-validator-header-anchored-trap memory + the backticked
   `DEC-FEED-RUST-FLIP-001` precedent).
2. `zplus-pr-guard` — the body lacked 4 of the 6 Z+ markers
   (15-row matrix / 7-row resilience matrix / 7-layer audit /
   4-agent hostile review). Fixed by adding the honest T11-grounded
   Z+ discipline block.

The corrected body passes ALL FOUR body gates locally (validate_dec_ids.py,
zplus-pr-guard.sh, definition-of-ready-guard.sh, validate_no_hallucination.py
— each exit 0, verified 2026-07-17).

RETRIGGER REQUIRED after the body edit: dec-id + zplus read the EVENT
SNAPSHOT body, so an edit alone does not re-run them — close→reopen the PR
(or push any new commit to the branch AFTER the edit). DoR reads the live
body and heals on edit alone.

Delete this prmeta branch after applying.
