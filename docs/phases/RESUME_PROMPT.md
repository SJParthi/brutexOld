# Resume Prompt for New Claude Code Session

> **Copy-paste this as the first prompt to any new Claude Code session
> that's working on BruteX Track 1.** It gives Claude full context +
> tells it exactly how to proceed.

---

## THE PROMPT (copy from here)

```
You are working on BruteX Track 1 — an Indian spot-index brute-force
backtesting engine. Resume work from the most recently merged main.

STARTUP STEPS (do all of these before writing any code):

1. Read /home/user/bruteX/CLAUDE.md in full. Honor every Golden Rule.

2. Read /home/user/bruteX/docs/phases/README.md to understand the
   phase-checklist system.

3. Read /home/user/bruteX/docs/phases/00_MASTER_CHECKLIST.md to find
   the current work front (first phase with unchecked boxes).

4. Read the relevant phase doc (e.g. docs/phases/phase_1_*.md). For
   every checkbox in that doc, understand what files it touches and
   what tests must pass.

5. Read the source-of-truth docs that the phase references:
   - docs/TRACK1_BACKTEST_SPEC.md (master spec)
   - docs/TRACK1_DATA_INGEST_DESIGN.md (pipeline design)
   - docs/TRACK1_VERIFICATION_PYRAMID.md (test pyramid)
   - docs/TRACK1_VOCABULARY.md (52 conditions)
   - docs/TRACK1_PHASE_PLAN.md (master 8-12 week plan)
   - docs/TRACK1_PERFORMANCE_BUDGET.md (SLA + day/night)
   - docs/TRACK1_MICROSTRUCTURE.md (Indian market edges)
   - docs/TRACK1_RUNTIME_ARCHITECTURE.md (operations atlas)
   - docs/TRACK1_OPTIMIZATIONS.md (Phase 2.5 + 3.5)
   - docs/MIGRATION.md (102-decision ledger — single source of truth)

6. Run `git log --oneline -5 main` to see the latest commits.

7. Run `brutex doctor` (when implemented) for the workday-start health
   check. If C1-C11 are all GREEN or SKIP, proceed. Otherwise diagnose.

8. Create a new feature branch: `claude/<short-description>-VXXXXX`
   (replace XXXXX with 5 chars). NEVER push to main directly.

WORK PROTOCOL:

- Work through the phase doc one checkbox at a time.
- After each checkbox completes:
  - Run ruff check + ruff format --check + mypy --strict (must be clean).
  - Run relevant tests (must pass).
  - Edit the phase doc to tick the box AND add the commit SHA:
    `- [x] Build watermark table (commit: 3d47fe0)`
  - Commit with Conventional Commits message.
  - Push to the feature branch.

- At every phase end:
  - Spawn 4-agent hostile review per CLAUDE.md §9 and
    docs/runbooks/4-agent-review.md (Phase 1 deliverable).
  - Add new MIGRATION.md decision row(s) for anything locked in.
  - Open draft PR.
  - Wait for CI green.
  - Ask user "Phase N is done — ready to merge?" before flipping draft.

- NEVER skip CI hooks (--no-verify is forbidden unless user explicitly
  requests).
- NEVER force-push shared branches.
- NEVER make scope-expansion changes without user approval.
- NEVER hallucinate a number — if you don't know, say "unverified".
- NEVER use AWS — local M4 Pro only per Decision #99.

DELIVERY EXPECTATIONS:

- Code: Python 3.12 strict (mypy + pyright + ruff) + Rust (PyO3 + Arrow)
- Tests: 100% line + 100% branch coverage on brutex/ package
- Mutation score ≥ 85% on Python AND Rust
- All 26 property invariants pass (when implementations land)
- All 4 oracle tests pass (audit-replay + Python-Rust parity +
  52-condition spec table + Stage 2.5 A/B)
- 22 CI gates green on every PR (advisory drift check is non-blocking)
- 4-agent hostile review at every phase end

START NOW: find the first phase doc with unchecked boxes and begin.

The user will provide:
- Sign-off at each phase end
- Groww access token before Phase 2 starts
- Business judgment on top-3 strategies at Phase 4 ship

The user does NOT need to provide anything else. Everything else is
on you. Be honest about progress, blockers, and uncertainties.
```

---

## Variations for specific situations

### If user wants to start Phase 1 fresh

Add this line at the start:
```
Start with Phase 1 immediately. Do not wait for further instructions.
Create branch claude/phase-1-verification-scaffolding-VXP1A and begin.
```

### If user wants to resume mid-phase

Add this line:
```
Continue from where the previous session left off. Find the most recent
commit on the latest claude/phase-N-* branch and read what was last
done. Pick up the next unchecked box.
```

### If user wants a phase status report only (no code)

Add this line:
```
DO NOT write code. Read the phase doc and report:
1. % complete (checkboxes ticked / total)
2. Last commit on the active phase branch
3. Any blockers
4. Recommended next 3 actions
```

### If user wants 4-agent hostile review

Add this line:
```
Spawn a 4-agent hostile review on the current phase's deliverables.
Use the pattern in docs/runbooks/4-agent-review.md. Report findings.
DO NOT fix anything yet — present findings for user approval.
```

---

## Useful URLs / commands the new session should know

| Command | Purpose |
|---|---|
| `git log main --oneline -10` | last 10 commits on main |
| `gh pr list --state open` | open PRs (use mcp__github__list_pull_requests) |
| `brutex doctor` (when implemented) | workday-start health |
| `brutex audit verify $LAKE/audit/audit.jsonl` | verify audit chain |
| `pytest tests/property -v` | run all property invariants |
| `pytest tests/integration -v` | run oracle tests |
| `pytest tests/bench -v` | run O(1) bench gates |
| `ruff check brutex/ tests/` | lint |
| `mypy --strict brutex/` | type check |

---

## Reminder of locked-not-negotiable items

These are LOCKED. The new session must NOT change them without user approval:

- 4 instruments: NIFTY 50, BANKNIFTY, SENSEX, India VIX (last is reference-only)
- 4 hardcoded securityIds: 13, 25, 51, 21 (all `IDX_I` + `INDEX`)
- 21 timeframes (1m + 19 derived + 1d)
- 52 atomic conditions, K=2..5 ANDed
- Bull/bear direction parameter (not a vocabulary bit)
- AND only (no OR / mixed boolean)
- Single-timeframe rule (no multi-TF strategies)
- Mode A (SL/TP) + Mode B (signal-exit) parallel sweep
- Bar entry: signal at bar N → enter at bar (N+1) OPEN
- Money: int64 paisa everywhere
- NO AWS — local M4 Pro only
- NO Telegram for now (structlog + dashboard only)
- 9-layer test pyramid + 26 property invariants + 4 oracles
- 4-agent hostile review at every phase end

If you (Claude) think any of these are wrong, ASK the user first.
NEVER silently change them.
