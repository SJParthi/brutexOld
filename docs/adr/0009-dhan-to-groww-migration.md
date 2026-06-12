# ADR-0009 — Re-migrate Backtesting Data Source Dhan → Groww

**Decision ID:** `DEC-DHAN-GROWW-REMIGRATE-001` (MIGRATION.md entry #666)
**Date:** 2026-06-12
**Status:** ACCEPTED — operator directive, override of a 4-agent hostile-review
NO-GO (logged honestly per Golden Rule #10). Implementation is GATED on the
Phase-0 probe (`scripts/groww_probe.py`); live adapter code is deferred until
the operator supplies probe transcripts.
**Author:** Claude Code (operator-directed)
**Supersedes:** ADR-0002 (`DEC-GROWW-DHAN-REVERT-001`, Dhan revert, 2026-05-31)
**Operator directive:** "Our plan is to completely reverse this dhan to groww"
(2026-06-12), following the migration handoff of the same date.

---

## 1. Context

BruteX Track 1 is currently spec-locked to **Dhan Data API v2** as the single
source of historical OHLC, per ADR-0002 / `DEC-GROWW-DHAN-REVERT-001` and
finalized by `DEC-DHAN-FINALIZE-001` (2026-06-01). The Dhan integration is
mature and tested:

- `brutex/adapters/dhan/` — 32 modules, 9,878 LoC (verified 2026-06-12 by
  file-count + `wc -l`).
- ~110+ files couple to it: `brutex/cli.py` (`cmd_fetch`, `_resolve_dhan_fetch_intervals`),
  `brutex/dashboard/{pull_controller,option_pull_controller,app}.py`,
  `brutex/orchestrator/{quota_tracker,rate_limiter,adaptive_rate_limiter}.py`,
  `brutex/config/credentials.py`, plus CI 100%-coverage gates per
  `brutex.adapters.dhan.*` module.
- `pyproject.toml:74-77` deliberately has **no** vendor SDK extra (Dhan is
  pure REST over an injected urllib transport).

The operator has directed a **full reversal back to Groww Trade API**
(`growwapi` Python SDK) as the single backtesting data source. This reverses
ADR-0002. Live execution / WebSocket / order placement remain OUT of scope
(CLAUDE.md §1 — Track 1 is signal-only research).

This is the third vendor flip in the project's history: Dhan→Groww (ADR-0001,
2026-05-26) → Groww→Dhan (ADR-0002, 2026-05-31) → **Dhan→Groww (this ADR)**.

## 2. Decision

**Adopt a full re-migration Dhan → Groww for the Track-1 backtesting data
source.** The end state is: `brutex/adapters/groww/` is the live adapter,
Dhan is deleted, and CLAUDE.md §1/§6 + `pyproject.toml` describe Groww.

The migration is delivered as an **additive-first, delete-Dhan-last** sequence
of individually-green PRs (see §5). This sequencing is NOT optional and is NOT
waived by the operator override: the Merge-Readiness Charter (CLAUDE.md §2
Rule 13 / `MERGE_READINESS_GUARANTEE.md` §4.5) forbids a half-broken
intermediate, and the prior Groww→Dhan flip required ~23 sub-PRs. A single
110-file destructive PR cannot pass the merge gates.

### 2.1 What changes (end state)

| Layer | Dhan (current) | Groww (target) |
|---|---|---|
| Adapter package | `brutex/adapters/dhan/` (32 modules) | `brutex/adapters/groww/` (implements `MarketAdapter`) |
| Transport | plain REST (injected urllib) | `growwapi` Python SDK (injected client) |
| Spot OHLC | `POST /v2/charts/intraday` + `/historical` (columnar arrays) | `get_historical_candles` (row-arrays `[ts,o,h,l,c,v,oi]`) |
| Options | `POST /v2/charts/rollingoption` (ATM±10/±3) | `get_expiries` → `get_contracts` → `get_historical_candles` (all strikes) |
| Instrument key | `SECURITY_ID` numeric (CSV-resolved) | `groww_symbol` string (e.g. `NSE-NIFTY`, `BSE-SENSEX`) |
| Auth | `access-token` (JWT) + `dhanClientId`, auto-renew | TOTP flow (`BRUTEX_GROWW_API_KEY` / `_TOTP_SECRET` / `_TOKEN_PATH`) |
| Rate limit | 5 r/s · 100k/day (documented) | **UNDOCUMENTED for backtesting — Phase-0 probe** |
| 1m chunk window | 90 days/call | 30 days/call (Groww limit; 10-30m=90d, 1h+=180d) |
| Price unit | rupees → Decimal in lake | rupees (float) → Decimal in lake (unchanged boundary) |
| Audit `vendor` stamp | `dhan` | `groww` |

### 2.2 What stays unchanged (vendor-neutral by design, CLAUDE.md §11 COMMON)

`brutex/{engine,vocabulary,indicators,feed,data,math,metrics,ranking}`, the
data lake schema, `brutex.money`, the audit hash-chain, the adaptive token
bucket, the work-item state machine, and `brutex/adapters/protocol.py`
(`MarketAdapter`). **The brute-force engine is untouched** (CLAUDE.md §2
Rule 3): NIFTY / BANKNIFTY / SENSEX + India VIX reference-only, 21 timeframes.

## 3. Consequences

### 3.1 Accepted risks — operator override of the 2026-06-12 hostile-review NO-GO

A 4-agent hostile review on 2026-06-12 (per CLAUDE.md Golden Rule #9) returned
**NO-GO → GO-WITH-CONDITIONS**, echoing the prior 2026-05-26 4/4 NO-GO. The
operator has explicitly overridden this verdict (precedent: `DEC-P0-005`,
ADR-0001). The findings are recorded here as **accepted risks**, not swept:

| # | Finding (hostile review) | Status / mitigation |
|---|---|---|
| 1 | **The "backtest = live feed, no NSE reconciliation" argument is unsourced folklore** — zero primary-source support in `docs/groww/`; under Golden Rule #1 it cannot be asserted as fact. | **WITHDRAWN as a justification.** The migration proceeds on the operator's directive + the verifiable 2020-depth / all-strikes facts, NOT on the parity claim. Phase-0 probe #3 measures it; until measured it is `unverified`. |
| 2 | **AUTOMATED regression** — Groww needs (per `DHAN_MIGRATION_COMPARISON.md`) daily manual re-login; Dhan auto-renews (zero clicks/day). Violates Golden Rule #11 AUTOMATED. | Accepted by operator. Mitigation: TOTP-only flow + watchdog + Telegram alert on auth failure + audit-log resume. Phase-0 probe #5 confirms the unattended-token lifetime. |
| 3 | **India VIX presence on Groww UNVERIFIED** (~55-65% probability missing per prior red-team; cached Groww CSV shows no VIX row). | **Phase-0 HARD-GATE.** If VIX absent on Groww → this ADR is rescinded before the cutover PR (Dhan retained). |
| 4 | **Backtesting per-day quota / rate bucket UNDOCUMENTED.** | Adaptive token bucket auto-discovers the practical cap; Phase-0 probe #5 records the observed limit. |
| 5 | **Dhan↔Groww data drift near-certain** (1m OHLC aggregation differs). | Phase-0 probe #4 = 1-day NIFTY 1m bar-by-bar diff. Drift accepted as a one-time lineage discontinuity (Golden Rule #4). |
| 6 | **2020 range + all-strikes are silent scope expansions** (engine is 2021→today, spot-only per Rule 3). | The **engine scope is unchanged**. 2020 depth + option breadth land in the LAKE only (DEC-P0-063 surface), NOT the brute-force engine. No engine re-scope is authorized by this ADR. |
| 7 | **Supply-chain regression** — re-adding `growwapi`+`pyotp` re-arms `pip-audit`/`safety`/`cargo deny`. | The `groww` extra is re-added (PR-1) only after passing those gates; pinned + lockfile committed. |
| 8 | **Reproducibility hash reset CERTAIN** — `data_snapshot_hash` changes with vendor; this is the SECOND such break (Dhan→Groww→Dhan→Groww). | Audit chain restarts with `vendor: groww`; prior Dhan lake/audit archived; no rewrite of prior decisions. |

### 3.2 Phase-0 probe gap (Golden Rule #1 / #10 honesty)

The build environment is a sandboxed container **without Groww credentials or
network egress** — so `scripts/groww_probe.py` **cannot run here**. Per the
operator decision (2026-06-12) PR-A ships the probe HARNESS + this ADR + spec
ONLY; the operator runs the probe on their machine and supplies the transcripts
(committed to `docs/groww/probe/`) before any live adapter code (PR-B onward)
lands. No Groww fact in this ADR marked UNVERIFIED may be encoded as fact in
code until its probe transcript exists.

## 4. Phase-0 probe checklist (`scripts/groww_probe.py`)

The probe MUST resolve, with committed transcripts, all of:

1. **India VIX existence** — download `instrument.csv`, grep for an India VIX
   `groww_symbol` (`NSE-INDIAVIX`?); then `get_historical_candles` on it. (HARD-GATE; risk #3)
2. **1m granularity at the 2020 boundary** — NIFTY 1m for 2020-01-01..2020-03-31
   (COVID window); confirm rows truly start Jan 2020 at 1-minute resolution. (risk #6)
3. **Backtest-vs-live parity** — same-session diff of `get_historical_candles`
   1m vs the live feed for the same bars, plus a T+2 re-fetch to detect
   restatement. (measures risk #1; the parity claim stands only on a measured
   zero-drift + zero-restatement result.)
4. **Cross-vendor drift** — bar-by-bar NIFTY 1m diff Dhan vs Groww for ≥1
   session; quantify against the prior 5% threshold. (risk #5)
5. **Backtesting rate limit + daily quota + token lifetime** — discover the
   undocumented cap and which bucket backtesting falls in; confirm unattended
   TOTP token lifetime. (risks #2, #4)
6. **Options round-trip** — `get_expiries` → `get_contracts` →
   `get_historical_candles` for NIFTY Jan-2020. (Track-2 / lake readiness.)

## 5. Phased PR sequence (additive-first, delete-Dhan-last)

| PR | What lands | Reversible? |
|---|---|---|
| **A (this PR)** | ADR-0009 + `scripts/groww_probe.py` + MIGRATION entry #636 | Trivial (docs + standalone script) |
| **A.1 (operator)** | Probe transcripts committed to `docs/groww/probe/` | Data only |
| **B** | `pyproject.toml` `groww` extra re-added (`growwapi`+`pyotp`), passing `pip-audit`/`safety`/`cargo deny` | Pure addition |
| **C** | `brutex/adapters/groww/{__init__,constants,auth,errors,client,parser,chunks}` | Pure addition (no callers) |
| **D** | `brutex/adapters/groww/{instruments,ingest,rate_control,token_keepalive}` + `SyntheticGrowwAdapter` | Pure addition |
| **E** | `brutex/adapters/groww/{option_ingest,option_universe_ingest}` | Pure addition |
| **F** | `GrowwMarketAdapter(MarketAdapter)` + `brutex/adapters/_common/` extraction (Dhan still passes) | Pure addition / refactor |
| **G** | CLI + dashboard cutover (one surface per PR) behind a `BRUTEX_BROKER` flag; default flips to Groww | Env-flip reversible |
| **H** | Spec docs amended: CLAUDE.md §1/§6 + TRACK1_* swapped to Groww; TRACK1_GROWW_ADAPTER.md banner lifted | Revert |
| **I (last)** | Delete `brutex/adapters/dhan/` + `tests/**/test_dhan_*` in ONE commit | **HARD — git tag `v-pre-groww-remigration-2026` precedes this PR** |

Each PR carries 100% line+branch coverage on its new modules, mutation ≥85%,
mypy --strict + pyright, the O(1) bench gates (vendor-agnostic — unaffected),
and the 4-agent / hostile-review evidence cited in its body.

## 6. Honest envelope (Golden Rule #10)

- ✅ REAL: operator directive + override logged (DEC-P0-005 precedent); 4-agent
  hostile review completed and its NO-GO + conditions recorded above.
- ✅ VERIFIED (in-repo docs): Groww 2020 depth, all-strikes options flow,
  row-array candle shape, 30/90/180-day chunk tiers, TOTP auth, exception
  hierarchy — `docs/groww/api-reference/*.md`.
- 🟡 UNVERIFIED (Phase-0 gates): India VIX presence, backtesting rate
  limit/quota, 2020 1m granularity-at-boundary, Dhan↔Groww drift, token
  lifetime. None encoded as fact in code until probe transcripts land.
- ❌ FOLKLORE / WITHDRAWN: "backtest = live feed parity" — not a justification.
- ❌ CERTAIN BREAK: reproducibility lineage discontinuity (2nd vendor reset).
- 🟡 ACCEPTED REGRESSION: daily-manual-auth vs Dhan auto-renew (Golden Rule #11).

## 7. Reversal path

If Phase-0 probe shows India VIX missing OR drift > 5% in the 1-day NIFTY 1m
diff: this ADR is marked SUPERSEDED, the migration branch is abandoned (not
merged past PR-A), Dhan remains the spec-locked source, and a MIGRATION entry
records the empirical findings as the reason for non-migration.

## 8. References

- ADR-0001 `docs/adr/0001-groww-vendor-pivot.md` (prior Groww pivot, superseded)
- ADR-0002 `docs/adr/0002-groww-to-dhan-revert.md` (`DEC-GROWW-DHAN-REVERT-001`)
- `docs/TRACK1_GROWW_ADAPTER.md` §13 — the reinstated Groww adapter design
  (its SUPERSEDED banner is lifted in PR-H, not here)
- `docs/groww/api-reference/*.md` — verified Groww API facts
- `docs/groww/hostile-review-2026-05-26/` — prior 4/4 NO-GO transcripts
- `brutex/adapters/protocol.py` — the `MarketAdapter` contract the Groww
  adapter implements
- CLAUDE.md §2 Rules 1, 3, 4, 9, 10, 11, 13 — explicitly engaged by this ADR
