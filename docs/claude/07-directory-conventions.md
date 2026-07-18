## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                       ← this file
├── README.md
├── .claude/
│   ├── settings.json
│   └── agents/
├── docs/
│   ├── TRACK1_BACKTEST_SPEC.md     ← Track 1 master spec
│   ├── TRACK1_VERIFICATION_PYRAMID.md
│   ├── TRACK1_VOCABULARY.md        ← 52 atomic conditions
│   ├── TRACK1_PHASE_PLAN.md        ← 8-12 week build plan
│   ├── TRACK1_PERFORMANCE_BUDGET.md
│   ├── TRACK1_MICROSTRUCTURE.md
│   ├── TRACK1_DASHBOARD_DESIGN.md  ← v29 locked design spec + data contract
│   ├── dashboard/mockups/          ← v28.html + v29.html (canonical mockups)
│   ├── MIGRATION.md                ← decision ledger
│   ├── ENGINEERING_REQUIREMENTS_CHARTER.md ← ultra-deep requirements (scope-tagged; aspirational, CLAUDE.md wins)
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/groww/             ← Groww Trade API adapter (growwapi SDK)
│   ├── calendars/                  ← NSE/BSE trading calendar
│   ├── config/
│   ├── costs/                      ← brokerage/tax cost models
│   ├── dashboard/                  ← FastAPI UI
│   ├── data/                       ← Parquet + DuckDB lake (generic)
│   ├── engine/                     ← brute-force combo/mask sweep engine
│   ├── execution/                  ← shared execution types + typed errors
│   │                                 for the lean instrument store (the
│   │                                 deprecated costs/simulator/driver trio
│   │                                 was DELETED — DEC-EXECUTION-TRIO-DELETE-001)
│   ├── feed/                       ← replay + resampler
│   ├── indicators/                 ← evaluate-time indicators (pivots/fib/EMA)
│   ├── math/                       ← thin facade to Rust hot kernel
│   │                                 (fno_math + trading_core re-export;
│   │                                 DEC-STRUCTURE-CLEANUP-001)
│   ├── metrics/                    ← Sharpe/DSR/SPA/CPCV statistics
│   ├── notifications/
│   ├── observability/
│   ├── options/                    ← expiry/strike/lot-size rules (lake)
│   ├── orchestrator/               ← FSM + audit + rate limiter
│   ├── ranking/                    ← composite ranking + rejection gates
│   ├── strategies/                 ← 52-vocabulary engine (Phase 3)
│   └── vocabulary/                 ← 52-condition evaluator (→ trading_core)
├── rust/                           ← Rust hot kernels (shared core + compat)
│   ├── fno-math/                   ← compat types + NSE calendar; Greeks/
│   │                                 indicators removed in DEC-P0-025
│   └── trading-core/               ← SHARED 52-vocab + evaluate-time
│                                     indicators (pivots/fib); bit-identical
│                                     core for BruteX (PyO3 wheel) + TickVault
│                                     (native) — DEC-SHARED-CORE-001 #441
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/                   ← hypothesis + proptest (24 invariants)
│   ├── golden/                     ← byte-equality fixtures
│   ├── bench/                      ← O(1) ratio tests
│   ├── stress/                     ← nightly ~5-min gate + weekly soak (#1580)
│   ├── smoke/                      ← <30s pre-push
│   └── fakes/                      ← test doubles
└── .github/workflows/
```

---

