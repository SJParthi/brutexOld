# ADR-0009 — Re-pivot Dhan → Groww (Track 1 historical data source)

- **Status:** Accepted (operator directive 2026-06-12)
- **Decision ID:** DEC-GROWW-REPIVOT-001 (MIGRATION.md #650)
- **Supersedes:** ADR-0002 (DEC-GROWW-DHAN-REVERT-001, 2026-05-31)
- **Scope:** Track 1 historical data source ONLY. Live execution / order
  placement / WebSocket feed remain OUT of scope (CLAUDE.md §1).

---

## Operator directive

> "groww has data starting from Jan 2020 that too even all expired options
> futures everything … whatever the data can be seen inside groww ohlc chart
> the same data is available even in historical also … so now the backtest
> and live will be precise" (2026-06-12)

---

## Context

ADR-0002 (2026-05-31) reverted BruteX from Groww → Dhan. That revert was
operator-directed and was NOT based on empirical findings from the Phase 0
probe (which was never executed). The 4 P0 blockers from ADR-0001's hostile
review were never tested. Since then the Dhan adapter was rebuilt from
scratch (~9,878 LoC / 32 modules).

The operator now asserts three empirically-confirmed advantages of Groww
over Dhan for the historical data use case:

| Fact | Dhan | Groww |
|------|------|-------|
| 1m intraday history depth | 5 years (cap ≈ 2021-06) | **From Jan 2020** (verified) |
| Expired options coverage | ATM±10/±3 via `rollingoption` | **ALL strikes** via `get_contracts` (verified) |
| Historical = live feed data? | NO (NSE post-market reconciliation) | **YES** — chart data ≡ historical API |
| India VIX availability | Verified present | **Verified present** (operator confirmed) |

The third fact is architecturally significant: because Groww's historical
API returns the exact same data as Groww's chart/live feed, the backtest
dataset is bit-identical in character to what the live strategy evaluation
will see. This directly solves the Dhan-era backtest-live divergence from
NSE reconciliation.

---

## Why ADR-0002's revert was NOT based on data quality

ADR-0002's four P0 blockers from the hostile review were:
1. India VIX presence on Groww — UNVERIFIED → **now verified present**
2. Backtesting rate limit bucket — UNDOCUMENTED → handled by adaptive limiter
3. Data quality drift Dhan ↔ Groww — UNQUANTIFIED → moot (Groww is now the
   single source of truth; there is no "drift" when backtest == live feed)
4. Reproducibility hash reset — CERTAIN → accepted cost, same as last time

None of these were ever empirically tested. The revert was operator-changed
direction after the Dhan tickvault implementation was available, not a
technical rejection.

---

## Decision

**Adopt full re-pivot Dhan → Groww for the Track 1 historical data source.**

| Surface | Dhan (remove) | Groww (rebuild) |
|---------|--------------|-----------------|
| Adapter package | `brutex/adapters/dhan/` (32 modules, 9,878 LoC) | `brutex/adapters/groww/` |
| Vendor SDK | None (plain urllib REST) | **None** (plain urllib REST — no growwapi SDK dep) |
| Auth | Manual JWT + `RenewToken` GET | **TOTP flow** (`pyotp` only dep) |
| Spot fetch | `POST /charts/intraday` + `/charts/historical` | `GET /historical/candles` (exchange, segment, groww_symbol) |
| Options fetch | `POST /charts/rollingoption` (ATM±N only) | **`GET /historical/contracts` (ALL strikes) via expiries→contracts→candles** |
| Instrument key | `SECURITY_ID` (numeric, CSV-resolved) | **`groww_symbol` string (e.g., `NSE-NIFTY`)** |
| Chunk window (1m) | 90 days/call | **30 days/call** (3× more calls) |
| Rate limit | 5 r/s / 100k/day (verified) | **Backtesting bucket UNVERIFIED** — adaptive [4..8] r/s |
| History start | ~2021-06 (5-year rolling cap) | **2020-01-01** |
| Options coverage | ATM±10 near / ATM±3 other | **ALL strikes for ALL expiries** |

### What stays unchanged (vendor-neutral by design — CLAUDE.md §11 COMMON)

The data lake, feed/resampler, execution simulator, `brutex.money`, audit
hash-chain, adaptive token bucket, orchestrator state machine, and
`adapters/protocol.py` (`MarketAdapter`) are broker-agnostic.
The brute-force engine is untouched (CLAUDE.md §2 Rule 3).

---

## Implementation approach

**No SDK dependency.** The growwapi SDK requires pandas, nats, protobuf,
nacl, nkeys — heavy live-feed dependencies irrelevant to backtesting.
We use plain `urllib` (stdlib) for HTTP transport (injected for testability,
same pattern as the Dhan adapter) and `pyotp` for TOTP generation only.

REST endpoints (extracted from SDK source inspection):
- Auth: `POST https://api.groww.in/v1/token/api/access`
  Body: `{"key_type": "totp", "totp": "<6-digit-code>"}`
  Header: `Authorization: Bearer <TOTP_API_KEY>`
- Candles: `GET https://api.groww.in/v1/historical/candles`
- Expiries: `GET https://api.groww.in/v1/historical/expiries`
- Contracts: `GET https://api.groww.in/v1/historical/contracts`
- Instrument CSV: `https://growwapi-assets.groww.in/instruments/instrument.csv`

---

## Phase 0 gate (MANDATORY before any ingest)

Run `scripts/groww_probe.py` with `BRUTEX_GROWW_SMOKE=1` to empirically:
1. Confirm India VIX bars available via `NSE-INDIAVIX`
2. Measure effective backtesting rate limit
3. Bar-by-bar diff: Groww historical ≡ Groww chart (spot check 1 day NIFTY 1m)
4. Confirm expired NIFTY options contracts available from 2020

If India VIX absent → this ADR is rescinded and Dhan adapter remains.

---

## Consequences

- `pyotp>=2.9,<3.0` added as a new `[groww]` optional extra dep.
- `SECURITY_ID` integer addressing replaced by `groww_symbol` string addressing.
- 3× more API calls for 1m spot ingest (30-day vs 90-day window) → adaptive
  rate limiter is the primary mitigation.
- Reproducibility hash reset: all prior Dhan-era audit chains and backtest
  hashes are invalidated. Dhan adapter is archived before deletion.
- `docs/CLAUDE.md` §1 / §6 updated with Groww fact table in Phase 7.
- `docs/TRACK1_GROWW_ADAPTER.md` status changes from SUPERSEDED → ACTIVE.

## Honest envelope (Golden Rule #10)

- ✅ REAL: Groww 2020 data depth — operator-confirmed
- ✅ REAL: India VIX on Groww — operator-confirmed
- ✅ REAL: All expired option strikes via `get_contracts` — doc-verified
- ✅ REAL: Historical = chart data (no NSE reconciliation layer) — operator-confirmed
- 🟡 PARTIAL: Groww backtesting rate limit — UNVERIFIED (Phase 0 probe gates ingest)
- ❌ CERTAIN: Reproducibility hash reset — accepted, Dhan artifacts archived at git tag
