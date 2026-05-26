# ADR-0001 — Groww Vendor Pivot for Backtesting Data Source

**Decision ID:** `DEC-VENDOR-PIVOT-001`
**Date:** 2026-05-26
**Status:** PROPOSED (docs-only PR; implementation pending Phase 0 probe + operator approval)
**Author:** Claude Code (operator-directed)
**Operator directive:** "no need of dhan here just note one and only groww dude that too only concentrating on backtesting dude okay?" (2026-05-26)

---

## 1. Context

BruteX Track 1 is currently spec-locked (per `CLAUDE.md §1`, `§6`) to the **Dhan Historical Data API** as the single source of historical OHLC for backtesting research. The Dhan integration spans 73 files / 5,659 LoC (audited 2026-05-26 by 4-agent hostile review) including:

- `brutex/adapters/dhan/` — 10 modules, 1,424 LoC (auth, body, client, constants, options_constants, parser, ssrf_guard, timeconv, types)
- `brutex/orchestrator/dhan_worker.py` + `dhan_errors.py` — 1,167 LoC
- 14 test files matching `test_dhan_*` — 3,068 LoC
- 14 spec docs with 247+ Dhan mentions
- 20+ MIGRATION.md decision entries citing Dhan
- 3 env vars (`BRUTEX_DHAN_ACCESS_TOKEN`, `BRUTEX_DHAN_TOKEN_PATH`, `BRUTEX_DHAN_SMOKE`)
- 4 hardcoded Dhan `securityId` integers (13/25/51/21) in `brutex/adapters/dhan/constants.py`
- CI workflow strict-100% gates per `brutex.adapters.dhan.*` module

The operator has directed a full pivot to **Groww Trade API** (`growwapi` Python SDK 1.5.0, December 2025) as the SINGLE source of backtesting data. Dhan is to be removed from the codebase.

## 2. Decision

**Adopt Option C (full pivot Dhan → Groww) for backtesting data source only.** Live execution / order placement / WebSocket feed remain explicitly OUT of scope per CLAUDE.md §1 (Track 1 = signal-only research).

### 2.1 What changes

| Layer | Before | After |
|---|---|---|
| Historical OHLC fetch | `brutex/adapters/dhan/` | `brutex/adapters/groww/` |
| Orchestrator worker | `brutex/orchestrator/dhan_worker.py` + `dhan_errors.py` | `brutex/orchestrator/groww_worker.py` + `groww_errors.py` |
| Hardcoded instrument IDs | securityIds 13/25/51/21 (Dhan ints) | groww_symbol strings (e.g., `NSE-NIFTY`, `BSE-SENSEX`, `NSE-INDIAVIX` — pending Phase 0 probe) |
| Env vars | `BRUTEX_DHAN_*` (3 vars) | `BRUTEX_GROWW_*` (TBD — TOTP path + secret + smoke flag) |
| Rate limiter | Fixed 3 r/s (60% of Dhan's verified 5 r/s) | **Adaptive auto-tuning [4..8] r/s** — see `docs/TRACK1_GROWW_ADAPTER.md` |
| Chunk window (1m) | 90 days per call | **30 days per call** (Groww-documented limit) |
| Price unit on the wire | int64 paisa | **rupees (float)** — adapter MUST × 100 at parse boundary to preserve int64 paisa invariant |
| Auth | Manual paste + auto-renew (Dhan) | **TOTP flow only** (Groww Approach 2). Approach 1 (daily approval) REFUSED — violates CLAUDE.md §11 AUTOMATED rule "ZERO clicks per day" |
| Audit log `vendor` stamp | Dhan-implicit | `vendor: groww` explicit field |

### 2.2 What stays unchanged

| Component | Why it stays |
|---|---|
| `brutex/vocabulary/` (52 atomic conditions) | Vendor-agnostic — operates on already-parsed OHLC bars |
| `brutex/indicators/{ema, day_extremes, pivots, fib_levels, prev_day_ohlc}` | Vendor-agnostic — pure math on OHLC inputs |
| `brutex/strategies/combo.py::combo_hash` | Uses logical symbol (`"NIFTY"`), not vendor ID. Hash invariant survives broker swap |
| `brutex/orchestrator/{audit, bloom, store, replay, retry, pipeline}` | Broker-agnostic schema; only the `subject` string format may need standardization |
| `brutex/observability/{sla, watchdog, monitor, standard_monitor}` | Vendor-agnostic |
| `brutex/notifications/telegram` + `wiring` | Vendor-agnostic |
| `brutex/dashboard/` | Zero Dhan branding (audited) |
| `brutex/adapters/protocol.py::MarketAdapter` | Already designed for multi-broker; Groww adapter implements the same protocol |
| Bloom + LMDB 4-tier dedup + audit JSONL hash-chain + L3 replay oracle | Architecture-level invariants, unchanged |

### 2.3 Universe scope (per operator decree)

- 200 NSE F&O eligible single stocks (per DEC-P0-063 lake-scope expansion)
- 3 index spots: NIFTY 50, BANKNIFTY, SENSEX
- 1 volatility index: INDIA VIX (Track 1 hard requirement per CLAUDE.md §2 rule 3)
- ~46 futures contracts (near-month rolled, both indexes + stocks)
- **~250 total instruments**
- **NO options in this PR** (Track 2 / future scope)
- 4 intervals: 1-minute, 1-day, 1-week, 1-month
- Date range: 2020-01-01 → today (Groww's documented historical depth)

## 3. Consequences

### 3.1 Accepted risks (operator override of hostile-review NO-GO verdict)

A 4-agent hostile review on 2026-05-26 (Plan + Explore + Red-team + Compliance) returned **Option C: NO-GO** with 4 P0 blockers. The operator has explicitly overridden this verdict per the `DEC-P0-005` operator-override precedent. The 4 P0 blockers are recorded here as **accepted risks** to be empirically mitigated:

| # | P0 Blocker | Mitigation |
|---|---|---|
| 1 | **INDIA VIX presence on Groww UNVERIFIED** (~55-65% probability missing per red-team agent) | **Phase 0 probe MANDATORY before any adapter code lands.** If VIX absent → this ADR is rescinded and Dhan adapter remains. |
| 2 | **Per-day backtesting cap UNDOCUMENTED** (Groww docs list only per-sec / per-min; backtesting bucket assignment ambiguous) | Adaptive rate-limiter (see §4 + `docs/TRACK1_GROWW_ADAPTER.md`) auto-discovers practical cap. Empirical probe at Phase 0 records observed limit. |
| 3 | **Data quality drift Dhan ↔ Groww NEAR-CERTAIN** (broker-specific 1m OHLC aggregation: snapshot timing, gap-fill, halt-bar emission all differ) | Phase 0 includes a 1-day NIFTY 1m bar-by-bar diff. Drift > 5% triggers a follow-up ADR before adapter code lands. Drift accepted as a one-time data-lineage discontinuity. |
| 4 | **Reproducibility hash reset CERTAIN** (CLAUDE.md §2 rule 4: `data_snapshot_hash` changes with vendor → all prior strategy/audit hashes invalidated) | Audit log restarts from `seq=0` with `vendor: groww` field on every entry. Prior Dhan-sourced audit chain retired but preserved at git tag `v-pre-groww-pivot`. |

**Operator override is logged here per Golden Rule #10 (honest envelope). The 4 P0 risks are NOT silently swept — they are explicitly accepted with named mitigations.**

### 3.2 Secondary risks (P1) from hostile review

| Risk | Mitigation |
|---|---|
| Bucket misclassification (1.5-hr fetch → could be 5+ hrs at stricter throttle) | Adaptive rate-limiter handles this gracefully |
| 12-month-old SDK with thin community (2-star wrappers) | Pin SDK version in `pyproject.toml`; vendor lockfile committed |
| TOTP token fragility | Watchdog + Telegram alert on auth failure; resume via audit log on operator intervention |
| Account suspension on bulk download | Use dedicated Groww account for backtest only; never co-locate with live trading creds |
| Expired-options round-trip UNVERIFIED | Out of scope for this PR (no options). Future Track 2 ADR will address. |
| 5,659 LoC rewrite regression risk | Protocol-conformance contract test (parametrized over `GrowwAdapter` only) + golden fixture regeneration from Phase 0 probe transcripts |

### 3.3 Reproducibility lineage break

This ADR represents a hard discontinuity in the BruteX experiment lineage. Per Golden Rule #4 (blake3 `data_snapshot_hash`), every backtest run after this pivot will produce a fundamentally different `data_snapshot_hash` than its Dhan-era counterpart. **No "before vs after" comparisons are valid.**

To preserve auditability:
- Pre-pivot state tagged at `git tag v-pre-groww-pivot` (immutable snapshot of the entire repo)
- Final Dhan-sourced audit chain archived at `~/.brutex/audit/dhan-final.jsonl.tgz`
- Final Dhan-sourced lake archived at `~/.brutex/lake-snapshot-dhan-2026-05-26.tar.zst`
- All pre-pivot MIGRATION decisions remain in `docs/MIGRATION.md` (no rewriting of history)
- A new section is appended to MIGRATION.md from `DEC-VENDOR-PIVOT-001` onward

### 3.4 STANDARDS_100PCT.md §1 impact

| Row | Impact |
|---|---|
| 1, 2 (coverage line + branch) | Dhan modules deleted from the 29-module strict-100% gate ratchet. New Groww modules added in the same PRs. Net coverage % preserved by construction. |
| 7 (jscpd + vulture) | Shared chunk-window logic extracted to `brutex/adapters/_common/` to pre-empt 3% duplication threshold. |
| 8 (spec ↔ test crosswalk) | TRACK1 spec docs amended in a dedicated docs-only PR per phase; crosswalk gate transient red expected during transition; mitigated by phase-fenced PR sequence |
| 11 (7 test types) | Protocol-conformance contract test added (1 new test class). |
| 17 (no-hallucination scan) | `scripts/ci/validate_no_hallucination.py` URL allowlist updated: `dhanhq.co` → `groww.in/trade-api/docs` |

## 4. Adaptive rate-limiter design (summary)

Full spec at `docs/TRACK1_GROWW_ADAPTER.md`. Summary:

- **Range:** [4, 8] requests/second
- **Initial probe rate:** 8 r/s (start high, descend on errors)
- **Step-down trigger:** 3 `GrowwAPIRateLimitException` within a 60-second sliding window → step current_rate down by 1 r/s
- **Step-up trigger:** 5 consecutive minutes with zero rate-limit exceptions → attempt current_rate + 1 r/s (up to 8 ceiling)
- **Floor breach:** if current_rate falls below 4 r/s → CRITICAL Telegram alert + halt fetcher; operator intervention required
- **State transitions audited:** every probe-down / probe-up / lock event = audit JSONL entry + Telegram INFO

## 5. Phased PR sequence

Per Plan agent's recommendation (with Option C scope: delete Dhan, no parallel run):

| Phase | Sub-PR | What lands | Reversible? |
|---|---|---|---|
| 0a | This ADR + MIGRATION entry + rate-limiter spec doc | docs-only | Trivial |
| 0b | `scripts/groww_probe.py` (standalone, no `brutex/` touch) | probe script + transcripts to `docs/groww/probe/` | Trivial |
| 1 | `brutex/adapters/groww/{__init__, auth, constants, types}` + `BRUTEX_GROWW_*` env vars | new package; no callers yet | Pure addition |
| 2 | `brutex/adapters/groww/{body, parser, client}` + adaptive rate limiter ext | new modules | Pure addition |
| 3 | `brutex/adapters/groww/{ssrf_guard, timeconv}` + `GrowwMarketAdapter(MarketAdapter)` | satisfies protocol | Pure addition |
| 4 | `brutex/orchestrator/groww_worker.py` + `groww_errors.py` | new worker | Pure addition |
| 5 | `brutex/adapters/_common/` extracted (chunk planning + retry helpers) | refactor, Dhan still passes | Pure refactor |
| 6 | CLI flip: `brutex ingest` defaults to Groww; `BRUTEX_BROKER` env var | one-line operational flip | Env-flip reversible |
| 7 | Spec docs amended (TRACK1_* + CLAUDE.md §6 replaced) | docs-only | Revert |
| 8 | Delete `brutex/adapters/dhan/` + `dhan_worker.py` + `dhan_errors.py` + `tests/{unit,golden,smoke}/test_dhan_*` | -5,659 LoC | **HARD — git tag `v-pre-groww-pivot` precedes this PR** |

Each phase ships as 1-2 PRs under 500 LoC, with the 4-agent review evidence cited in the PR body per Z+ marker discipline.

## 6. Honest envelope (Golden Rule #10)

- ✅ REAL: 4-agent hostile review completed; verdicts archived; operator override documented per DEC-P0-005 precedent
- ✅ REAL: Groww Python SDK exists (verified 1.5.0 PyPI Dec 2025); 1-minute candle support hard-verified from SDK source `growwapi/groww/client.py:86-97`
- 🟡 PARTIAL: INDIA VIX presence on Groww — UNVERIFIED. Phase 0 probe is the gate.
- 🟡 PARTIAL: Backtesting endpoint rate-limit bucket — UNVERIFIED. Adaptive limiter mitigates.
- 🟡 PARTIAL: Data quality drift magnitude — UNQUANTIFIED. Phase 0 bar-by-bar diff is the gate.
- ❌ CERTAIN BREAK: reproducibility lineage discontinuity. Mitigated by git tag + archived lake/audit. No rewrite of prior decisions.

## 7. Reversal path

If Phase 0 probe shows INDIA VIX missing OR data drift > 5% mismatches in 1-day NIFTY 1m diff:
1. This ADR is marked SUPERSEDED.
2. The `claude/groww-pivot-VENDPV1` branch is abandoned (not merged).
3. The Dhan adapter remains the spec-locked source.
4. A short MIGRATION entry records the empirical findings as the reason for non-pivot.

## 8. References

- 4-agent hostile review transcripts archived under `docs/groww/hostile-review-2026-05-26/` (committed alongside this ADR)
- Groww Python SDK docs (operator-pasted 2026-05-26): https://groww.in/trade-api/docs/python-sdk + sub-pages
- DEC-P0-005 (MIGRATION.md entry #143): operator-override precedent
- DEC-P0-063 (MIGRATION.md entry #200): 200-stock universe expansion (preserved scope)
- CLAUDE.md §2 rules 1, 4, 9, 10, 11, 12 — explicitly engaged by this ADR
- CLAUDE.md §6 — Dhan facts table to be replaced by Groww facts table in Phase 7 of the PR sequence
- `brutex/adapters/protocol.py:79-108` — the `MarketAdapter` Protocol docstring anticipating exactly this scenario
