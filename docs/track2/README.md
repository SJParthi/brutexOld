# BruteX Track 2 — Options Track

> **Status:** Phase 0 design in progress (2026-05-15 evening).
> **Sequencing:** Implementation BLOCKED until Track 1 spot data ingest is live + rolling window working.
> **Process:** Same Phase 0-4 methodology + 4-agent hostile review + checklist directory + RESUME_PROMPT pattern as Track 1.

## How to use this directory

| You are... | Read this |
|---|---|
| A brand-new Claude Code session working on Track 2 | `RESUME_PROMPT_TRACK2.md` |
| Operator checking Track 2 status | `00_TRACK2_MASTER_CHECKLIST.md` |
| Wondering about the API | `TRACK2_OPTIONS_SPEC.md` (master spec) |
| Wondering about storage/state machine | `TRACK2_DATA_INGEST_DESIGN.md` |
| Working on a Track 2 phase | `phases/phase_N_*.md` |

## File index

| File | Purpose | Status |
|---|---|---|
| `README.md` | This file | — |
| `00_TRACK2_MASTER_CHECKLIST.md` | Progress dashboard | living |
| `RESUME_PROMPT_TRACK2.md` | Prompt for new Claude sessions | living |
| `TRACK2_OPTIONS_SPEC.md` | Master spec (API, universe, scope) | locked |
| `TRACK2_DATA_INGEST_DESIGN.md` | Storage + state machine + pseudocode | locked |
| `phases/phase_0_documentation.md` | Phase 0 — Documentation | 🔄 in progress |
| `phases/phase_1_verification.md` | Phase 1 — Test scaffolding | ☐ |
| `phases/phase_2_groww_options_adapter.md` | Phase 2 — Adapter impl | ⏸️ blocked on Track 1 complete |
| `phases/phase_3_backtest_engine.md` | Phase 3 — Engine | ☐ |
| `phases/phase_4_first_sweep.md` | Phase 4 — First sweep | ☐ |

## Source-of-truth Track 1 docs (read these first)

| Doc | Why it matters for Track 2 |
|---|---|
| `../../CLAUDE.md` | Project rules, 4-agent review pattern, Groww facts |
| `../TRACK1_BACKTEST_SPEC.md` | Master spec — Track 2 is the post-Track-1 Options Track |
| `../TRACK1_DATA_INGEST_DESIGN.md` | Storage + state machine pattern we mirror |
| `../TRACK1_VERIFICATION_PYRAMID.md` | 9-layer pyramid extends to options |
| `../TRACK1_PHASE_PLAN.md` | Same methodology |
| `../MIGRATION.md` | Decision ledger continues with Track 2 decisions (#200+) |

## Convention

Same as Track 1:

| Symbol | Meaning |
|---|---|
| `- [ ]` | Not started |
| `- [x] (commit: abc1234)` | Done with commit SHA |
| ✅ | All sub-items done |
| 🔄 | Currently in progress |
| ⏸️ | Blocked on external dependency |

## Track 2 vs Track 1 — One-glance summary

| Aspect | Track 1 (Spot) | Track 2 (Options) |
|---|---|---|
| Universe | 4 instruments (NIFTY/BANKNIFTY/SENSEX/INDIAVIX) | 3 underlyings options (NO India VIX) |
| Endpoint | `/v2/charts/intraday` + `/v2/charts/historical` | `/v2/charts/rollingoption` |
| Address scheme | securityId per instrument | (underlying_secId, expiryFlag, strikeRelative, type) |
| Timeframes | 21 (1m + 19 derived + 1d) | **1m only** (no resampling) |
| Strike model | n/a | ATM ± 10 (Groww-enforced) |
| Greeks | n/a | IV from Groww; delta/gamma/theta computed (Black-Scholes) |
| Brokerage / STT | NONE (signal-only) | TBD (Phase 3 — for realistic execution) |
| Storage | ~80 MB / 5y | ~5.5 GB / 5.37y (6 streams × 42 series × ~25 MB) |
| Backfill wall time | ~5 min | ~88 min (15,876 calls / 3 r/s) |
| Daily API quota (backfill) | < 0.01% | 15.9% one-time |
| Daily API quota (incremental) | < 0.01% | 0.21% (210 active series × 1 call) |
| Phase 0 effort | done | in progress |
| Total build effort | 12-17 weeks | 6-8 weeks (after Track 1 ships) |
