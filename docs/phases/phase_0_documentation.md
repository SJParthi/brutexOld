# Phase 0 — Documentation + Wipe + Locked Decisions

## Status: ✅ DONE

## Effort: ~1 day (completed 2026-05-15)
## Owner: Claude
## Merged PRs: #12, #13, #14

## Goal

Lock all design decisions in markdown before any code lands. Wipe the
legacy Groww+F&O scope and pivot to Dhan + spot indexes + India VIX
(reference-only) for Track 1.

## Prerequisites

- [x] User requested Groww → Dhan pivot
- [x] User confirmed signal-only research scope
- [x] User confirmed 4 instruments + 21 TFs

## Checklist

### PR #12 — Initial pivot (merged 2026-05-15, commit `2e86bab`)

- [x] Run first 4-agent hostile review on legacy Groww+F&O design (commit 852ed1e)
- [x] Delete `brutex/adapters/groww/` (8 source files) (commit 852ed1e)
- [x] Delete `docs/groww-sdk/` (4 docs) (commit 852ed1e)
- [x] Delete 11 Groww-named test files (commit 852ed1e)
- [x] Rewrite `brutex/cli.py` (984 → 226 lines) (commit 852ed1e)
- [x] Rewrite `brutex/universe.py` (8 F&O → 3 spot indexes) (commit 852ed1e)
- [x] Rewrite `CLAUDE.md` with Dhan facts + 4-agent review pattern (commit 852ed1e)
- [x] Add DEPRECATED banner to `docs/REQUIREMENTS.md` (commit 852ed1e)
- [x] Update `pyproject.toml` — drop growwapi, bump to 0.2.0a0 (commit 852ed1e)
- [x] Write `docs/TRACK1_BACKTEST_SPEC.md` (commit 852ed1e)
- [x] Write `docs/TRACK1_VERIFICATION_PYRAMID.md` (commit 852ed1e)
- [x] Write `docs/TRACK1_VOCABULARY.md` (commit 852ed1e)
- [x] Write `docs/TRACK1_PHASE_PLAN.md` (commit 852ed1e)
- [x] Write `docs/TRACK1_PERFORMANCE_BUDGET.md` (commit 852ed1e)
- [x] Write `docs/TRACK1_MICROSTRUCTURE.md` (commit 852ed1e)
- [x] Write `docs/MIGRATION.md` (decisions #1-#70) (commit 852ed1e)
- [x] Drill-down audit fixes (1 ruff bug + 8 doc contradictions) (commit 58a26a7)
- [x] Test_universe.py rewrite for 3-index scope (commit 116cd4b)
- [x] Demote fno-math-drift to advisory-only (commit 4009213)
- [x] Apply ruff format (commit 0b6065c)
- [x] CI green + merge to main (squash commit `2e86bab`)

### PR #13 — VIX scope correction (merged 2026-05-15, commit `bfb4dc6`)

- [x] User feedback: VIX is reference-only, not brute-forced
- [x] Add `reference_only` flag to `UnderlyingSpec` (commit 7236ba8)
- [x] Export `BRUTE_FORCE_INDICES` (3) and `REFERENCE_ONLY_INDICES` (1) (commit 7236ba8)
- [x] Update `CLAUDE.md §2.3` scope (commit 7236ba8)
- [x] Update `docs/TRACK1_BACKTEST_SPEC.md` §1 + §4 + add §4.1 (commit 7236ba8)
- [x] Update `docs/TRACK1_DATA_INGEST_DESIGN.md` §1 + §2 (commit 7236ba8)
- [x] Update `docs/TRACK1_VOCABULARY.md` banner (commit 7236ba8)
- [x] Add `vix_at_entry` + `vix_at_exit` columns to result row spec (commit 7236ba8)
- [x] Add 7 new tests for partition (40 total in `test_universe.py`) (commit 7236ba8)
- [x] Lock Dhan rate limit at verified 5 r/s + 100K/day (commit 7236ba8)
- [x] Lock Dhan error code map (Data API codes 800-814 + DH-901..910) (commit 7236ba8)
- [x] Auth design — manual paste + auto-renew (commit 3dc6bfc)
- [x] User confirms NIFTY=13, BANKNIFTY=25 securityIds
- [x] Hardcode all 4 securityIds in spec (commit 852fc3d)
- [x] Drop master-CSV resolver from Phase 2 deliverables (commit 852fc3d)
- [x] Add MIGRATION decisions #71-#87 (commit 852fc3d)
- [x] CI green + merge to main (squash commit `bfb4dc6`)

### PR #14 — Runtime atlas + optimizations (merged 2026-05-15, commit `32c652b`)

- [x] Spawn 3 parallel agents (common runtime + institutional comparison + table cohesion)
- [x] Write `docs/TRACK1_RUNTIME_ARCHITECTURE.md` (700+ lines) (commit be53bbe)
- [x] Add Day/Night envelope split to spec + budget docs (commit be53bbe)
- [x] Update MIGRATION with decisions #88-#96 (commit be53bbe)
- [x] Spawn 3 throughput agents (worst-case + benchmarks + failure-mode)
- [x] Fix Bloom math bug (5 GB at 0.1% FPR was WRONG for 33B raw, correct only for ~150M-1B survivors) (commit 7c65b1d)
- [x] Add honest 4-day disclaimer + contingency SLA matrix (commit 7c65b1d)
- [x] Add C11 brutex doctor check (throughput.canary_fresh) (commit 7c65b1d)
- [x] Spawn 2 optimization-design agents (Metal GPU + Bayesian early-stop)
- [x] Write `docs/TRACK1_OPTIMIZATIONS.md` (commit ed19aa4)
- [x] Lock decisions #99-#102 (commit ed19aa4)
- [x] CI green + merge to main (squash commit `32c652b`)

## Verification gates (all passed)

- [x] ruff check brutex/ tests/ — clean across all 3 PRs
- [x] ruff format --check — clean
- [x] mypy --strict brutex/ — clean (55 files)
- [x] pytest tests/unit — 539 passed, 2 skipped (env-deps only)
- [x] 22 of 22 required CI gates green on every merge
- [x] No broken imports remaining (grepped)
- [x] No Groww refs outside MIGRATION/REQUIREMENTS allowlist (0 elsewhere)
- [x] First 4-agent hostile review (PR #12) — caught Groww+F&O blockers
- [x] Second 4-agent hostile review (PR #13) — verified India VIX availability + identifiers
- [x] Third 4-agent + extra throughput audit (PR #14) — caught Bloom math bug + 18 precision risks

## Exit criteria

- [x] All 3 PRs merged to main
- [x] MIGRATION ledger contains 102 locked decisions
- [x] No Groww code anywhere in workspace
- [x] All 4 instruments verified (NIFTY=13, BANKNIFTY=25, SENSEX=51, INDIAVIX=21 on `IDX_I` + `INDEX`)
- [x] Honest disclaimer added (4-day = TARGET, not GUARANTEE)
- [x] Contingency SLA matrix in spec §2.1
- [x] Phase 2.5 + Phase 3.5 designs locked in `docs/TRACK1_OPTIMIZATIONS.md`

## Decisions locked

MIGRATION.md decisions #1 through #102 — see that file for the full
ledger. Highlights:

| Range | Theme |
|---|---|
| #1-#70 | Groww→Dhan pivot, Track 1 scope, vocabulary, exit modes, search pipeline, verification, microstructure, phase plan |
| #71-#80 | India VIX added, post-merge corrections, Dhan rate limit verified, 4 securityIds hardcoded |
| #81-#87 | Auth design, error map, segment cleanup, single-TF invariant |
| #88-#96 | Runtime bootstrap, doctor CLI, MCP servers, Cowork isolation, cheap fixes (combo_hash etc.), institutional verdict, day/night envelope |
| #97-#102 | 3 spec consistency fixes, honest 4-day SLA, Bloom math fix, no-AWS lock, Phase 2.5 + 3.5 designs, combined optimization impact |

## Files created (final state on main)

```
docs/CLAUDE.md                                      (rewritten)
docs/TRACK1_BACKTEST_SPEC.md                        (new, master spec)
docs/TRACK1_DATA_INGEST_DESIGN.md                   (new)
docs/TRACK1_VOCABULARY.md                           (new)
docs/TRACK1_VERIFICATION_PYRAMID.md                 (new)
docs/TRACK1_PHASE_PLAN.md                           (new)
docs/TRACK1_PERFORMANCE_BUDGET.md                   (new)
docs/TRACK1_MICROSTRUCTURE.md                       (new)
docs/TRACK1_RUNTIME_ARCHITECTURE.md                 (new)
docs/TRACK1_OPTIMIZATIONS.md                        (new)
docs/MIGRATION.md                                   (new)
docs/REQUIREMENTS.md                                (DEPRECATED banner)
docs/ROADMAP.md                                     (PARTIALLY DEPRECATED)
docs/adapters.md                                    (legacy banner)
brutex/cli.py                                       (rewritten 945→226 lines)
brutex/universe.py                                  (rewritten for 4 indices)
brutex/adapters/__init__.py                         (broker-neutral docstring)
tests/unit/test_universe.py                         (40 tests)
tests/conftest.py                                   (vendor_symbol renamed)
+ many docstring/comment scrubs across the rest of brutex/
+ pyproject.toml: dropped growwapi, bumped 0.2.0a0
+ .github/workflows/ci.yml: drift check advisory
```

## What's NOT done yet

Everything code-related — actual Dhan adapter, vocabulary atoms,
brute-force engine, filter cascade, etc. **All design and locked.
Phase 1 starts implementation.**
