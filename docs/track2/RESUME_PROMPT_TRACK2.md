# Resume Prompt for New Claude Code Session — Track 2

> Copy-paste this as the FIRST prompt to any new Claude Code session
> that's working on BruteX **Track 2** (Options Track). For Track 1,
> use `../phases/RESUME_PROMPT.md` instead.

---

## THE PROMPT (copy from here)

```
You are working on BruteX Track 2 — the Options Track that translates
Track 1 winning spot signals into Indian index options via Dhan's
/v2/charts/rollingoption endpoint. Resume work from the most recently
merged main.

STARTUP STEPS (do all of these before writing any code):

1. Read /home/user/bruteX/CLAUDE.md in full. Honor every Golden Rule.
   Track 2 inherits ALL of them (no AWS, no hallucination, Dhan rate
   limit, no scope expansion, 4-agent review at every phase end).

2. Read /home/user/bruteX/docs/track2/README.md to understand the
   Track 2 directory layout.

3. Read /home/user/bruteX/docs/track2/00_TRACK2_MASTER_CHECKLIST.md
   to find the current work front (first phase with unchecked boxes).

4. Read the relevant Track 2 phase doc (e.g.
   docs/track2/phases/phase_0_documentation.md). For every checkbox
   in that doc, understand what files it touches and what tests must
   pass.

5. Read the Track 2 source-of-truth docs:
   - docs/track2/TRACK2_OPTIONS_SPEC.md (master spec)
   - docs/track2/TRACK2_DATA_INGEST_DESIGN.md (storage + state machine)

6. Read the Track 1 docs that Track 2 inherits from:
   - docs/TRACK1_BACKTEST_SPEC.md (Track 1 master spec)
   - docs/TRACK1_DATA_INGEST_DESIGN.md (pipeline pattern)
   - docs/TRACK1_VERIFICATION_PYRAMID.md (9-layer pyramid)
   - docs/TRACK1_PHASE_PLAN.md (methodology)
   - docs/TRACK1_RUNTIME_ARCHITECTURE.md (atlas)
   - docs/MIGRATION.md (decision ledger — Track 2 rows are #200+)

7. Check the Track 1 prereq gate: read docs/phases/00_MASTER_CHECKLIST.md.
   If Track 1 Phase 2 is not yet ✅ DONE, Track 2 Phase 2+ are BLOCKED;
   only Phase 0 (docs) and Phase 1 (test scaffolding against fakes)
   can proceed.

8. Run `git log --oneline -5 main` to see latest commits.

9. Create a new feature branch:
   `claude/track2-<short-description>-VXXXXX`
   (replace XXXXX with 5 chars). NEVER push to main directly.

WORK PROTOCOL:

- Work through the Track 2 phase doc one checkbox at a time.
- After each checkbox completes:
  - Run ruff check + ruff format --check + mypy --strict (must be clean).
  - Run relevant tests (must pass).
  - Edit the phase doc to tick the box AND add the commit SHA:
    `- [x] Wire rollingoption adapter (commit: 3d47fe0)`
  - Commit with Conventional Commits message.
  - Push to the feature branch.

- At every phase end:
  - Spawn 4-agent hostile review per CLAUDE.md §9.
  - Add new MIGRATION.md decision row(s).
  - Open draft PR.
  - Wait for CI green.
  - Ask user "Track 2 Phase N is done — ready to merge?" before flipping draft.

- NEVER skip CI hooks (--no-verify is forbidden).
- NEVER force-push shared branches.
- NEVER expand the locked Track 2 scope without user approval.
- NEVER add Track 1 features (spot vocabulary, VIX combinations).
- NEVER use AWS — local M4 Pro only per Decision #99.

LOCKED TRACK 2 SCOPE (do NOT silently change):

- 3 underlying universes: NIFTY 50 (secId 13), BANKNIFTY (secId 25),
  SENSEX (secId 51). NO India VIX options (none exist on Dhan).
- Endpoint: POST /v2/charts/rollingoption (ATM-relative time-series).
- Strikes: ATM-10 to ATM+10 (21 strikes), CE + PE = 42 series per
  (underlying, cycle, day).
- Cycles: weekly + monthly per underlying, with per-regime date gating
  across 2021-2026 (see TRACK2_OPTIONS_SPEC.md §2 for the full timeline
  reconstructed from BSE Notice 20230327-65 + NSE FAOP circulars).
  Key facts: BANKNIFTY weekly killed 2024-11-13 (data exists for 2021→
  Nov 2024 window). SENSEX options were tradable 2021-01-01 onwards on
  Thursday (thin liquidity pre-2023-05-15). Post-Sep-2025: NSE→Tue, BSE→Thu.
- **6 active backfill streams**: NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-WEEK
  (until 2024-11-13), BANKNIFTY-MONTH, SENSEX-WEEK, SENSEX-MONTH.
- Timeframe: 1m ONLY (no resampling for options).
- Greeks: IV from Dhan; delta/gamma/theta/vega computed by BruteX
  using Black-Scholes-Merton.
- Storage: ~5.5 GB for 5.37y across all 6 streams.
- Backfill: ~15,876 API calls, ~88 min wall time, 15.9% of daily quota (one-time).

DELIVERY EXPECTATIONS:

- Code: Python 3.12 strict + Rust (Black-Scholes kernel in fno-math)
- Tests: 100% line + 100% branch on brutex/adapters/dhan/options*
  and brutex/options/
- 11 new property invariants (options-specific) on top of Track 1's 26
- 2 new oracle tests (options replay + Black-Scholes-vs-py_vollib)
- 22 CI gates green on every PR
- 4-agent hostile review at every phase end

START NOW: find the first Track 2 phase doc with unchecked boxes and
begin.

The user will provide:
- Sign-off at each phase end
- Confirmation that Track 1 Phase 2 is shipped before Phase 2 starts
- Business judgment on top-3 strategies at Phase 4 ship

The user does NOT need to provide anything else. Everything else is
on you. Be honest about progress, blockers, and uncertainties.
```

---

## Variations for specific situations

### If user wants Phase 0 status report only

```
DO NOT write code. Read docs/track2/phases/phase_0_documentation.md
and report:
1. % complete (checkboxes ticked / total)
2. Last commit on the active track2 branch
3. Any blockers
4. Recommended next 3 actions
```

### If user wants to start Phase 1 (test scaffolding against fakes)

```
Start with Phase 1 immediately. Track 1 Phase 1 (verification
scaffolding) provides the 9-layer pyramid; Phase 1 of Track 2 EXTENDS
it with 11 options-specific invariants. Do not wait for Track 1
implementation — work against the Phase 1 fake adapter.
Create branch claude/track2-phase-1-verification-VXTV1 and begin.
```

### If user wants 4-agent hostile review on a Phase

```
Spawn a 4-agent hostile review on the current Track 2 phase deliverables.
Use the pattern in docs/runbooks/4-agent-review.md. Report findings.
DO NOT fix anything yet — present findings for user approval.
```

---

## Useful commands the new session should know

| Command | Purpose |
|---|---|
| `git log main --oneline -10` | last 10 commits on main |
| `git log --oneline claude/track2-*` | latest track2 branches |
| `mcp__github__list_pull_requests` | open PRs |
| `brutex doctor` (when implemented) | workday-start health |
| `brutex audit verify $LAKE/audit/audit.jsonl` | verify audit chain |
| `pytest tests/property/test_options_*.py -v` | options invariants (Phase 1+) |
| `pytest tests/integration/test_options_replay.py -v` | replay oracle |
| `ruff check brutex/ tests/` | lint |
| `mypy --strict brutex/` | type check |

---

## Reminder of LOCKED Track 2 decisions

These are LOCKED. The new session must NOT change them without user approval:

- 3 underlyings (NIFTY 50, BANKNIFTY, SENSEX) — NO India VIX options
- `/v2/charts/rollingoption` endpoint (ATM-relative, NOT per-contract)
- 1-minute timeframe ONLY (no resampling)
- ATM ± 10 strike radius (Dhan-enforced)
- Current expiry at sweep time (week + month per underlying)
- **6 backfill streams** (NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-WEEK [active until 2024-11-13, dormant after], BANKNIFTY-MONTH, SENSEX-WEEK, SENSEX-MONTH; SENSEX extends back to 2021-01-01 per BSE Notice 20230327-65 with thin pre-2023-05-15 liquidity)
- IV from Dhan; delta/gamma/theta/vega computed (Black-Scholes-Merton)
- Money: int64 paisa everywhere
- NO AWS — local M4 Pro only
- Apple Silicon GPU offload (Phase 3.5 of Track 1 inherits)
- 22 CI gates green + 4-agent hostile review at every phase end

If you (Claude) think any of these are wrong, ASK the user first.
NEVER silently change them.
