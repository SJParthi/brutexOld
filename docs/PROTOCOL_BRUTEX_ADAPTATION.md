# BruteX Protocol Adaptation — tickvault rules → BruteX equivalents

**Status:** ACTIVE companion to `.claude/rules/project/brutex-protocol.md`.
**Created:** 2026-05-17 (DEC-PROTOCOL-001).
**Purpose:** This doc is the SINGLE SOURCE OF TRUTH for how to read the
verbatim tickvault-flavored rules bundle inside BruteX context. Without
it, the bundle's references to QuestDB / WebSocket / DHAT / Loom /
`crates/<X>/` / Telegram NotificationEvent / Dhan order WS will silently
mismatch BruteX's actual infrastructure.

**Authority order:** `CLAUDE.md` Golden Rules → this adaptation doc →
`.claude/rules/project/brutex-protocol.md` (bundle body) → defaults.

**Read this first** if you are a Claude Code session, a reviewer, or the
operator validating an auto-loaded charter applies correctly to BruteX.

---

## 0. The honest envelope (BruteX-specific)

Tickvault is a live retail trading product (WebSocket → QuestDB → orders).
BruteX is an offline backtesting engine (HTTP REST → Parquet/DuckDB →
signal generation). Many of tickvault's "100% never disconnects / zero
ticks lost" guarantees DO NOT APPLY to BruteX because BruteX has no
live tick stream.

The BruteX-flavored honest 100% claim — uses **only currently-shipping
capability**, marks each line as REAL or ROADMAP:

> "100% inside the tested envelope, with ratcheted regression coverage:
> - int64 paisa arithmetic ✅ REAL (`brutex/costs/types.py`, `brutex/costs/calculator.py`)
> - deterministic combo dedup via LMDB content-hash key ✅ REAL
>   (`brutex/orchestrator/store.py`); **Bloom + LMDB 4-tier is ROADMAP**
>   per STANDARDS_100PCT.md §1 row 15 (📋 P-DEDUP-1 not yet written)
> - replayable backtest result via audit JSONL hash-chain ✅ REAL
>   (`brutex/orchestrator/audit.py` — see module docstring lines 4-34)
> - bit-exact reproducibility per `sha256(strategy_mask ‖ direction ‖
>   instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖
>   indicator_version ‖ code_commit_hash)` 🟡 PARTIAL — spec'd in CLAUDE.md
>   §2 #4 + TRACK1_BACKTEST_SPEC.md; **not yet computed end-to-end** in
>   any code path (engine foundation is Phase 1; sweep runner unbuilt).
> Beyond the envelope, the audit JSONL captures every material action;
> `brutex doctor C2` integrity check is **ROADMAP** (subcommand not yet
> added to `brutex/cli.py`)."

The phrase "WebSocket never disconnects" is N/A to BruteX (no WS in
Track 1; Track 2 sim only). Do not paste it.

**Honesty discipline:** when a PR cites a guarantee from the bundle, FIRST
verify the underlying mechanism EXISTS in BruteX today. If it's spec'd
but unbuilt, mark it 📋 ROADMAP — do NOT promise it as REAL. The 4-agent
review pass on this PR caught 5 false-REAL claims in the first draft;
expect the same discipline on every future PR.

---

## 1. The single substitution table (terminology dictionary)

| Tickvault term | BruteX equivalent | Notes |
|---|---|---|
| `crates/common/src/` | `brutex/` (Python package root) | |
| `crates/core/src/` | `brutex/engine/` + `brutex/strategies/` | |
| `crates/storage/src/` | `brutex/data/` + `brutex/orchestrator/store.py` (LMDB) | |
| `crates/trading/src/` | `brutex/execution/` + `brutex/costs/` | |
| `crates/api/src/` | `brutex/adapters/dhan/` (HTTP REST) | |
| `crates/app/src/` | `brutex/orchestrator/pipeline.py` | |
| Rust hot kernel | `rust/fno-math/src/` (Greeks, indicators) | Only Rust in BruteX |
| `cargo test --workspace` | `pytest tests/unit tests/integration tests/property` | |
| `cargo bench` | `pytest tests/bench --benchmark-min-rounds=3` | |
| `cargo clippy -- -D warnings` | `ruff check brutex` + `mypy --strict brutex` + `pyright brutex` | |
| `cargo fmt --check` | `ruff format --check brutex` | |
| `cargo-mutants` | `mutmut` (Python) + `cargo-mutants` (Rust crate, `rust/fno-math/`) | |
| `cargo fuzz` | `hypothesis` (Python) + `cargo fuzz` (Rust crate) | |
| `cargo audit` | `pip-audit --strict --requirement brutex-reqs.txt` | |
| DHAT (allocation profiler) | `pytest-benchmark` p99 ratio assertion (Python); Rust DHAT in `rust/fno-math/` only | |
| Loom (concurrency model checker) | N/A — BruteX is single-process pytest-parallel | Skip Loom test category entirely |
| Sanitizers (ASan / TSan) | N/A — Python doesn't expose them; Rust kernel gets them in `rust/fno-math/` weekly CI | |
| QuestDB (live TSDB) | DuckDB (offline catalog) + Parquet (bars) + LMDB (dedup) | Different access patterns |
| `ALTER ADD COLUMN IF NOT EXISTS` | Parquet schema evolution = re-write a partition; DuckDB DDL | |
| DEDUP UPSERT KEYS | LMDB content-hash key ✅ REAL (`brutex/orchestrator/store.py`); **Bloom + 4-tier defence is ROADMAP** — only LMDB single-tier today | Bloom layer = P-DEDUP-1 property test in `tests/property/` per STANDARDS_100PCT.md §1 row 15 |
| WebSocket main-feed (`wss://api-feed.dhan.co`) | N/A in Track 1 — BruteX uses `POST /v2/charts/intraday` (REST) | Track 2 may add WS for live |
| WebSocket order-update (`wss://api-order-update.dhan.co`) | N/A — Track 1 signal-only; Track 2 execution sim only | Never live orders in BruteX |
| `SubscribeRxGuard` | N/A — no WS subscription state in BruteX | |
| `compute_reconnect_base_delay_ms` | `brutex/orchestrator/retry.py` (HTTP retry with jitter) | |
| Tick rescue ring (5M tick buffer) | N/A — REST adapter has per-call chunk windowing instead | |
| NDJSON spill / DLQ | N/A — failed Dhan REST calls retry-then-quarantine via orchestrator FSM | |
| Loki structured log | `structlog` + audit JSONL hash-chain | |
| Prometheus counter (`tv_*_total`) | structlog event with `event=<name>` field + audit row | No Prometheus in MVP |
| Prometheus gauge | structlog event + dashboard (📋 unbuilt) | |
| Grafana panel | `brutex/dashboard/app.py` ✅ BUILT (38 KB FastAPI app); **🟡 dashboard module excluded from coverage in pyproject.toml `[tool.coverage.run].omit`** — coverage gap | Lift exclusion when a uvicorn-driven integration test lands |
| Alertmanager + Prom rule | `brutex/notifications/telegram.py` ✅ BUILT (539 lines); **🟡 alert sources not yet wired** — module exists but no spec-defined subsystem (watchdog / audit-chain / stall detector) fires events yet | Wire alert producers as those subsystems land |
| `mcp__tickvault-logs__run_doctor` | `brutex doctor` CLI (📋 unbuilt — listed in STANDARDS_100PCT.md P1) | |
| `mcp__tickvault-logs__questdb_sql` | `duckdb` CLI against `data/catalog.duckdb` | |
| `mcp__tickvault-logs__prometheus_query` | N/A — no Prometheus | |
| `mcp__tickvault-logs__grafana_query` | N/A — no Grafana | |
| `mcp__tickvault-logs__tail_errors` | `tail data/audit/*.jsonl` filtered by `event=error` | |
| `mcp__tickvault-logs__list_active_alerts` | Telegram bot status query (📋 unbuilt) | |
| `mcp__tickvault-logs__app_log_tail` | `tail data/logs/brutex.log` (structlog) | |
| `mcp__tickvault-logs__docker_status` | N/A — BruteX is not containerized in dev | |
| `.claude/hooks/session-auto-health.sh` | None yet — opportunity to add (low priority for offline backtester) | |
| `.claude/hooks/banned-pattern-scanner.sh` | `scripts/ci/scan-banned-patterns.sh` (already exists) | |
| `.claude/hooks/pub-fn-test-guard.sh` | Coverage gates in `pytest --cov-fail-under=90` + 100% on `brutex/costs/`, `brutex/engine/` | |
| `.claude/hooks/pub-fn-wiring-guard.sh` | None — Python's import system is more permissive | Consider for Rust crate only |
| `.claude/hooks/plan-verify.sh` | `TodoWrite` in-session + manual review for multi-session plans | |
| `.claude/hooks/per-item-guarantee-check.sh` | Manual review against `docs/STANDARDS_100PCT.md` per PR | |
| `cargo test --features dhat` | N/A for Python; Rust kernel only | |
| `cargo test --features loom` | N/A — no Loom | |
| `quality/crate-coverage-thresholds.toml` | Per-module `pytest --cov=brutex.<module> --cov-fail-under=100` in `ci.yml` | |
| `scripts/coverage-gate.sh` | Same; CI native | |
| `scripts/bench-gate.sh` | `pytest tests/bench` + (📋 ratio gate roadmap) | |
| `Secret<T>` wrapper | `pydantic.SecretStr` (Python) | |
| `error_code_tag_guard.rs` | structlog event field discipline + reviewer pass | |
| `dedup_segment_meta_guard.rs` | `tests/property/test_dedup_*.py` (📋 P-DEDUP-1 to be written) | |
| `audit-findings-2026-04-17.md` Rules 3/4/11/12/13 | Mostly N/A — they target tickvault live tasks. BruteX equivalent: `docs/TRACK1_PHASE_PLAN.md §0` + `docs/STANDARDS_100PCT.md` | |
| `gap-enforcement.md` I-P1-11 (composite security_id + segment) | Hardcoded 4-tuple in `brutex/adapters/dhan/options_constants.py` ✅ REAL (only existing constants module today; **CLAUDE.md §6 line 251 cites `constants.py` but that path doesn't exist** — pre-existing stale ref, out-of-scope for this PR, tracked for follow-up) | |

---

## 2. Per-bundle-file applicability matrix

| Bundle file | BruteX applicability | Action required |
|---|---|---|
| `pr-completion-protocol.md` | ✅ Apply 1:1 | Already aligned via PR template + conventional-commit checker |
| `operator-charter-forever.md` §A (SessionStart hooks) | 🟡 N/A — no hooks exist yet | Defer; consider adding `session-context-brief.sh` as separate PR |
| `operator-charter-forever.md` §B (Cowork MCP tools) | 🟡 Replace tickvault-logs MCP with direct `Read`/`Bash` | Use BruteX-native tools |
| `operator-charter-forever.md` §C (15-row + 7-row matrices) | ✅ Apply 1:1 | Already mirrored in `docs/STANDARDS_100PCT.md` |
| `operator-charter-forever.md` §D (Telegram 10 commandments) | 🟡 Apply NOW for message-text in `brutex/notifications/telegram.py` (already built); defer the bot-wiring half until subsystems fire events | Audit existing telegram.py message templates against the 10 commandments (plain English, no library jargon, IST 12hr, severity emoji at start) |
| `operator-charter-forever.md` §E (3-agent review) | ✅ Apply 1:1 | BruteX uses 4-agent variant per `docs/TRACK1_PHASE_PLAN.md §0` |
| `operator-charter-forever.md` §F (honest 100% claim wording) | ✅ Apply with BruteX envelope (see §0 above) | |
| `operator-charter-forever.md` §G (kid-friendly + table format) | ✅ Apply 1:1 | Already operator preference (per session opening) |
| `operator-charter-forever.md` §H (PR completion 10-step loop) | ✅ Apply 1:1 | This adoption PR is the proving ground |
| `operator-charter-forever.md` §I (WebSocket scope lock — 2 WS only) | ❌ N/A | BruteX has NO WS in Track 1; defer to Track 2 if WS ever added |
| `z-plus-defense-doctrine.md` 7-layer (L1-L7) | ✅ Apply 1:1 with BruteX example substitutions | New code paths in BruteX should fill the 7-cell table |
| `z-plus-defense-doctrine.md` 105-cell coverage matrix | ✅ Apply 1:1; many cells map to BruteX-native tools | Replace tickvault tools with BruteX equivalents per §1 above |
| `plan-enforcement.md` DRAFT→APPROVED→IN_PROGRESS→VERIFIED | 🟡 Apply for multi-session plans; in-session uses TodoWrite | Adopt when Track 2 Phase 3+ requires multi-session plans |
| `stream-resilience.md` 12 B-rules | ✅ Apply 1:1 (file-first, ≤2K chunks, parallel agents, etc.) | |
| `per-wave-guarantee-matrix.md` | ✅ Apply 1:1 | `docs/STANDARDS_100PCT.md` is the BruteX-native version |
| `wave-4-shared-preamble.md` Sections 1-8 | 🟡 See per-section table in `.claude/rules/project/brutex-protocol.md` header | |
| `testing.md` 22-test categories | 🟡 Apply 7 of 22 in BruteX: unit, integration, property, golden, bench, mutation, stress (per Golden Rule #12). Skip DHAT/Loom/sanitizer/fuzz (Rust-only) | |
| `testing-scope.md` block-scoped default | ✅ Apply 1:1 via `pytest -m <marker>` or per-module test files | |

---

## 3. What does NOT apply to BruteX

Sections of the bundle that DO NOT translate. Do not invent BruteX
equivalents; do not paste their wording into BruteX PRs:

| Tickvault section | Why N/A in BruteX |
|---|---|
| WebSocket reconnect parity / `compute_reconnect_base_delay_ms` | Track 1 has no WS at all |
| Tick rescue ring (`TICK_BUFFER_CAPACITY` = 5,000,000) | No tick stream in offline backtester |
| NDJSON spill + DLQ | REST adapter retries via `brutex/orchestrator/retry.py`; no spill needed |
| `SubscribeRxGuard` | No WS subscription state |
| Daily-reset collisions / market-hours-aware tokio tasks | BruteX runs at sweep time, not live |
| Edge-triggered alerts on tick gap | No live tick gap concept |
| SEBI 24h JWT forced reconnect | N/A — REST API uses different auth model |
| `Source=P` filter on order updates | No live order updates in BruteX |
| Depth-20 / Depth-200 WS endpoints lock | N/A — no WS endpoints used |
| `core_affinity Core 0` for tick parser | N/A — no tick parser |
| QuestDB `ALTER ADD COLUMN IF NOT EXISTS` self-heal | Parquet schemas managed via rewrite; not relevant |
| Loom interleaving tests | Python pytest is single-process per worker; Loom needs Rust |
| DHAT allocation profile on hot path | Python hot path uses `pytest-benchmark` ratio; Rust kernel has its own DHAT |
| Telegram coalescing window 60s | Defer until Telegram bot lands |

---

## 4. New BruteX-specific gates this bundle implies we SHOULD add (roadmap)

| Gate | Source bundle clause | BruteX status |
|---|---|---|
| `.claude/hooks/session-context-brief.sh` | `operator-charter-forever.md §A` step 5 | 📋 Not built. Low priority — opt-in PR later. |
| `.claude/hooks/plan-verify.sh` | `plan-enforcement.md` step 7 | 📋 Manual TodoWrite review covers; build if multi-session plans grow |
| `brutex doctor` CLI with C2 (audit chain) check | `wave-4-shared-preamble.md §5` + STANDARDS_100PCT.md §11 P1 | 📋 Already in P1 roadmap |
| `P-DEDUP-1` property test for LMDB+Bloom dedup | `dedup_segment_meta_guard.rs` analog | 📋 Already in P1 roadmap |
| Per-counter dashboard panel enforcement | `operator_health_dashboard_guard.rs` analog | 📋 Defer until FastAPI dashboard lands |
| Per-alert ratchet | `resilience_sla_alert_guard.rs` analog | 📋 Defer until Telegram bot + dashboard land |
| Mutation score gate ≥ 85% | `cargo-mutants` + `mutmut` | 🟡 mutmut runs as `\|\| true` advisory; P0 closing path removes the `\|\| true` |
| pyright strict mode | `cargo clippy -- -D warnings` analog | 🟡 Currently `standard` mode; P0 closing path bumps to strict |
| Bench p99 ratio assertion | `scripts/bench-gate.sh` | 📋 Roadmap — see STANDARDS_100PCT.md row 13 |

---

## 5. The auto-driver explanation (BruteX flavor)

> **Sir, imagine you have two shops:** tickvault is a live juice shop —
> customers walk in non-stop, juice must pour every second, if the
> blender stops for 5 seconds you lose customers. BruteX is the recipe
> R&D kitchen — you cook 10 million test batches overnight, write down
> which recipe wins, and HAND that recipe to the juice shop in the
> morning. The R&D kitchen doesn't need live phone lines or alarm bells
> at 3 AM — it just needs to be EXACTLY right when it writes the recipe,
> because tomorrow the live juice shop will pour 1 lakh juices using
> that recipe. ONE wrong digit in the recipe = 1 lakh wrong juices.
> So tickvault's rules about "phone line must never drop" don't apply
> to us — but tickvault's rules about "every measurement must be
> EXACT, ratcheted by test, no hallucination" — those apply EXTRA
> strict to us. We follow the spirit, not the letter, of the live-shop
> rules; we follow the letter, exactly, of the EXACTNESS rules.

---

## 6. Cross-references

- `CLAUDE.md` — 12 Golden Rules; supersedes any contradicting bundle clause
- `.claude/rules/project/brutex-protocol.md` — the verbatim bundle (auto-loaded)
- `docs/STANDARDS_100PCT.md` — BruteX-native 18-requirement honest status
- `docs/TRACK1_BACKTEST_SPEC.md` — algorithmic source-of-truth
- `docs/TRACK1_PHASE_PLAN.md §0` — 4-agent hostile review pattern
- `docs/MIGRATION.md` — decision-ID ledger (DEC-PROTOCOL-001 records this adoption)

---

## 7. Amendment log

| Date | Change | Trigger |
|---|---|---|
| 2026-05-17 | Initial draft | Operator paste of tickvault protocol bundle on 2026-05-17 |
