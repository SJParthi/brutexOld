# Track 1 — Master Checklist Dashboard

> **Single-glance progress dashboard.** Updated as each phase ticks
> boxes. The first phase with unchecked boxes is the current work front.

Last updated: 2026-05-26 (post-DEC-VENDOR-PIVOT-001 + Dhan wipe)

## Overall progress

```
   Phase 0   ✅✅✅✅✅✅✅✅✅✅  100% (3 PRs merged: #12 #13 #14 + DEC-VENDOR-PIVOT-001 PRs #134 #135 #136 #137 #138)
   Phase 0b  ☐☐☐☐☐☐☐☐☐☐    0% (Empirical probe — HARD GATE for Phase 1+)
   Phase 1   🟡🟡🟡🟡☐☐☐☐☐☐   40% (Scaffolding largely landed — see "scaffolding done" table)
   Phase 1.5 ☐☐☐☐☐☐☐☐☐☐    0% (Groww adapter — Phase 1-4 of DEC-VENDOR-PIVOT-001)
   Phase 2.5 ☐☐☐☐☐☐☐☐☐☐    0% (Bayesian Early-Stop)
   Phase 3   ☐☐☐☐☐☐☐☐☐☐    0% (BRUTE-FORCE ENGINE — the actual product)
   Phase 3.5 ☐☐☐☐☐☐☐☐☐☐    0% (Metal GPU Offload)
   Phase 4   ☐☐☐☐☐☐☐☐☐☐    0% (Filter Cascade + first production sweep)
```

## Phase summary

| Phase | Status | Effort | Owner | What it produces |
|---|---|---|---|---|
| 0 — Documentation | ✅ DONE | done | Claude | 11 TRACK1 docs + 247+ decisions + legacy-broker wipe + DEC-VENDOR-PIVOT-001 |
| **0b — Empirical Groww probe** | ☐ Not started | 30 min ops + 30 min dev | Claude + operator (Groww API key required) | Probe transcripts in `docs/groww/probe/` — VIX presence, 1m candle, rate-limit bucket, data-drift Dhan↔Groww |
| 1 — Verification Scaffolding | 🟡 Largely done (40%+) | done in pieces (PRs #45-#137) | Claude | Audit chain + Bloom + LMDB + SLA detector + Watchdog + Telegram wiring + MonitorLoop + L3 replay oracle + doctor (see "scaffolding done" below) |
| 1.5 — Groww Adapter Phases 1-4 (post-DEC-VENDOR-PIVOT-001) | ⏸️ Blocked on Phase 0b | 40-50 hours dev | Claude (needs Groww token + Phase 0b transcripts) | `brutex/adapters/groww/` (10 modules, ~2,250 LoC) + Groww worker + errors taxonomy |
| 2.5 — Bayesian Early-Stop | 🚫 Abandoned (ADR-0006 / DEC-SPRT-INFEASIBLE-001) | — | — | retired: no leaderboard-safe kill exists; see DEC-SPRT-SLA-RETIRE-001 |
| 3 — Brute-Force Engine | ☐ Not started | 6-10 weeks | Claude (requires 4-agent hostile review per CLAUDE.md §9) | Stage 1A precompute + 1B sweep (~33B trials) + Stage 2 DSR + Stage 3 CPCV + Stage 4 WFO + Lockbox |
| 3.5 — Metal GPU Offload | ☐ Not started | 2-3 weeks | Claude | 2-5× speedup, 33B sweep in 1 overnight |
| 4 — Filter Cascade + Ship | ☐ Not started | 2-3 weeks | Claude | First production sweep + top-3 candidates surfaced on dashboard |

**Total remaining: ~11-19 weeks** of Claude dev + ~30-60 min of operator unblocks.

## Phase 1 — scaffolding already DONE (today's audit revealed this)

The original master checklist said "Phase 1 — Not started." That was 11 days stale. As of 2026-05-26, the following Phase-1 scaffolding HAS landed:

| Component | File / Module | Status |
|---|---|---|
| Audit JSONL hash-chain | `brutex/orchestrator/audit.py` + 11.4 KB property tests | ✅ DONE |
| Bloom + LMDB 4-tier dedup | `brutex/orchestrator/{bloom,store}.py` | ✅ DONE |
| L3 replay oracle | `brutex/orchestrator/replay.py` | ✅ DONE |
| Rate limiter (+ the dormant `QuotaGate` quota seam) | `brutex/orchestrator/{rate_limiter,quota_tracker}.py` | ✅ DONE |
| Retry with bounded backoff (live ladder) | `brutex/adapters/groww/errors.py::is_retryable` + client `max_attempts` | ✅ DONE |
| WorkItem FSM / PhaseRunner scaffolding | DELETED as never-production-wired (MIGRATION.md #1727); live event shapes kept in `brutex/orchestrator/events.py` | 🗑 DELETED |
| SLA breach detector | `brutex/observability/sla.py` | ✅ DONE |
| Hung-worker watchdog | `brutex/observability/watchdog.py` | ✅ DONE |
| MonitorLoop daemon | `brutex/observability/monitor.py` | ✅ DONE |
| Production composer | `brutex/observability/standard_monitor.py` | ✅ DONE |
| Telegram notifier + wiring | `brutex/notifications/{telegram,wiring}.py` | ✅ DONE |
| `brutex doctor` health checks | `brutex/doctor.py` | ✅ DONE |
| `brutex monitor` CLI | `brutex/cli.py` | ✅ DONE |
| Vocabulary (52 atomic conditions) | `brutex/vocabulary/` | ✅ DONE |
| Indicators (EMA, day_extremes, pivots, fib_levels, prev_day_ohlc) | `brutex/indicators/` | ✅ DONE (strict-100% gates per DEC-INDICATOR-COVERAGE-001) |
| `MarketAdapter` Protocol | `brutex/adapters/protocol.py` | ✅ DONE |
| Dashboard skeleton | `brutex/dashboard/app.py` | ✅ DONE |
| Bootstrap script + env-var registry | `scripts/setup/bootstrap.sh` + `docs/runbooks/ENV_VARS.md` | ✅ DONE |
| SessionStart hook | `.claude/hooks/session-start.sh` | ✅ DONE |
| M4 Pro absolute throughput stub | `tests/bench/test_throughput_absolute.py` + `bench-m4pro.yml` | ✅ DONE (runner LIVE 2026-07-07 — the gate executes on that workflow's push triggers; `bench-m4pro.yml` header) |

**Total Phase-1 progress: ~40% verified done (scaffolding), the remaining ~60% is verification-pyramid tests + invariants P-01..P-26 (some implemented, full set not enumerated).**

## What user needs to provide

| Item | When | Effort | Status |
|---|---|---|---|
| Push `v-pre-groww-pivot` git tag to origin (sandbox got 403) | Anytime | 5 sec | ☐ TBD |
| Open Groww instruments CSV → grep VIX → confirm presence | **Before Phase 0b probe** | 2 min | ☐ **HARD GATE** |
| Subscribe Groww Trade API ₹499 + paste API key + TOTP secret | Before Phase 0b probe execution | 5 min | ☐ TBD |
| Telegram bot token + chat ID env vars | Anytime (Telegram works without; alerts are optional) | 5 min | ☐ TBD |
| M4 Pro self-hosted GHA runner registration | Before throughput-gate row 14 closes | 10 min | ☐ TBD |
| Sign-off per phase end | At each phase boundary | 5-30 min | recurring |
| Business judgment on top-3 strategies | At Phase 4 ship | 30-60 min | ☐ TBD |

## Decisions locked (247+ total, post-DEC-VENDOR-PIVOT-001)

See `../MIGRATION.md` for the full ledger. Phase-grouped:

| Phase / decade | Decision range | Count |
|---|---|---|
| 0 (data-source pivot + Phase 0-4 plan) | #1-#102 | 102 |
| 0 (post-#14 runtime + Phase 2.5/3.5 designs) | #103-#150 | 48 |
| P0 ratcheting + bypass-closures | #139-#214 | ~75 |
| Late-May ratchets (combo hash, Telegram, SLA, watchdog, L3 replay, MonitorLoop, standard_monitor, ops infra) | #235-#244 | 10 |
| **DEC-VENDOR-PIVOT-001 epoch** (indicator coverage, ADR, Dhan wipe) | #245-#247 | 3 |
| **TOTAL** | **#1-#247** | **247** |

## Verification stack (must remain green throughout)

| Layer | Status target | Owner |
|---|---|---|
| ruff check + ruff format --check | clean | every PR |
| mypy --strict + pyright strict | clean | every PR |
| 22-25 CI gates (Lint / Type / Tests / Property / Bench / Golden / Security / Docs / Licenses / Rust / Mutation / Z+ marker / DEC-ID / No-hallucination / Bug-fix regression / Banned pattern / Spec ↔ test crosswalk / CI OK aggregate) | all green | every PR |
| 60 property invariants (currently implemented; full P-01..P-26 set not all enumerated) | all pass with 500 examples | per phase end |
| 4-agent hostile review | required at each phase end + major design changes | per phase end |
| `brutex doctor` health checks | all GREEN (or SKIP) | every workday |

## Risk register (cross-phase)

| Risk | Phase impact | Mitigation |
|---|---|---|
| Throughput < 80K t/s on real M4 Pro | 3+ | Contingency SLA matrix in spec §2.1 self-amends |
| GPU kernel produces wrong result | 3.5 | Bit-exact parity test + audit-replay oracle blocks merge |
| Stage 2.5 kills a true winner | 2.5 | A/B oracle test; rolling 30d miss-rate budget ≤ 5% |
| **INDIA VIX absent from Groww instruments CSV** | **0b+** | **DEC-VENDOR-PIVOT-001 rescinded; halt Groww work** |
| Groww API rate-limit bucket stricter than documented | 1.5+ | Adaptive rate-limiter [4..8] r/s auto-tunes per `docs/TRACK1_GROWW_ADAPTER.md §5` |
| Data drift Dhan↔Groww > 5% on Phase 0b 1-day NIFTY diff | 0b | Follow-up ADR before any Phase 1+ code lands |
| Groww API changes / breaks | 1.5+ | Adaptive rate-limiter + quarterly smoke test |
| User's M4 Pro unavailable | 3+ | No fallback (NO AWS per decision #99) |
| Lake mount goes missing | 1.5+ | brutex doctor C1-C8 catches; halt + alert |
| Reproducibility lineage break (vendor pivot) | once at Phase 1.5 | Accepted; pre-pivot state at `git tag v-pre-groww-pivot` |

## Current work front

**☐ Phase 0b — Empirical Groww probe.** Hard gate before any Phase 1.5 (Groww adapter) code lands. Next Claude session must:

1. Wait for operator to push `v-pre-groww-pivot` tag + grep VIX in Groww CSV + subscribe to Groww (₹499 + paste API key).
2. Write `scripts/groww_probe.py` — standalone probe (no `brutex/` imports). 4 sub-tests: VIX presence, 1m candle support, rate-limit bucket empirical probe, data-drift Dhan↔Groww (1-day NIFTY).
3. Run probe, commit transcripts to `docs/groww/probe/`.
4. If VIX absent → write `docs/adr/0002-groww-vendor-pivot-RESCIND.md`; halt.
5. If all 4 sub-tests pass → Phase 1.5 (Groww adapter) is unblocked.

## DEC-VENDOR-PIVOT-001 phased rollout status

| Phase | Status | PRs |
|---|---|---|
| 0a — ADR + adapter spec + MIGRATION entry | ✅ DONE | #135 |
| 0b — Empirical probe | ☐ Not started | next |
| 1 — `brutex/adapters/groww/{auth, constants, types}` | ☐ Blocked on 0b | future |
| 2 — `brutex/adapters/groww/{body, parser, client}` + adaptive rate-limiter | ☐ Blocked | future |
| 3 — SSRF guard + timeconv + `GrowwMarketAdapter(MarketAdapter)` | ☐ Blocked | future |
| 4 — `brutex/orchestrator/groww_worker.py` + `groww_errors.py` | ☐ Blocked | future |
| 5 — `brutex/adapters/_common/` chunk-helper extract | ☐ Blocked | future |
| 6 — CLI flip + `BRUTEX_BROKER` dispatcher | ☐ Blocked | future |
| 7 — Spec doc amendments + CLAUDE.md §6 replacement | 🟡 Partial (mostly done via PRs #137/#138) | future |
| **8 — Delete Dhan (-5,659 LoC)** | ✅ DONE (executed AHEAD of Phases 1-7 per operator decree) | #136 |

## After Phase 0 (already done) recap

| PR | Merged | Highlights |
|---|---|---|
| #12 | ✅ | Legacy-broker wipe + 7 TRACK1 docs + decisions #1-#70 |
| #13 | ✅ | VIX reference-only + rate-limit + 4 securityIds + auth design + decisions #71-#87 |
| #14 | ✅ | Runtime atlas + day/night envelope + Phase 2.5/3.5 designs + decisions #88-#102 |
| #134 | ✅ | DEC-INDICATOR-COVERAGE-001 — ema/day_extremes/prev_day_ohlc to strict-100% |
| #135 | ✅ | DEC-VENDOR-PIVOT-001 ADR + TRACK1_GROWW_ADAPTER spec + 4-agent hostile-review evidence |
| #136 | ✅ | Dhan adapter wipe (Phase 8 executed early): -5,803 LoC |
| #137 | ✅ | Zero-Dhan sweep across 110 files (active code + Rust + docs + CI) |
| #138 | ✅ | Final zero-Dhan sweep on TRACK1_GROWW_ADAPTER.md + rustfmt fix |

Main currently at: `git log main --oneline -1`.
