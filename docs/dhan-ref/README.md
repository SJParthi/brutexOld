# `docs/dhan-ref/` — Dhan v2 API verified reference

Operator-supplied, source-cited Dhan HQ v2 API facts that the Track 1
**Dhan adapter** is built against. This is the SINGLE SOURCE OF TRUTH
for every Dhan vendor fact (CLAUDE.md §2 Golden Rule #2; no fact may be
re-derived or hallucinated — Golden Rule #1).

Provenance: operator directive 2026-05-31 to revert Groww → Dhan
(DEC-GROWW-DHAN-REVERT-001, MIGRATION.md #341). Facts fetched from the
operator's `tickvault/docs/dhan-ref/*` mirror + DhanHQ public docs
(https://dhanhq.co/docs/v2/) + the pasted Expired-Options page.

**Authoritative consolidated reference** (operator-uploaded 2026-06-02,
DEC-DHAN-DOCSET-001): [`DHANHQ_V2_API_REFERENCE.md`](DHANHQ_V2_API_REFERENCE.md)
— the complete DhanHQ v2 REST + Python-SDK reference in one file. The
per-endpoint files below are the **in-scope (Track 1 data)** slices.

| File | Surface | Track 1 |
|---|---|---|
| `01-introduction-and-rate-limits.md` | base URL, rate limits, quota | ✅ |
| `02-authentication.md` | access-token / TOTP / headers / §11 AUTOMATED | ✅ |
| `08-annexure.md` | enums — `exchangeSegment` (incl. `IDX_I`), instrument types, `expiryCode`, intervals | ✅ |
| `05-historical-data.md` | `/charts/historical` + `/charts/intraday` (CORE) | ✅ |
| `13-intraday-charts.md` | `/charts/intraday` precise request/response schema | ✅ |
| `14-historical-charts.md` | `/charts/historical` precise request/response schema | ✅ |
| `06-expired-options-data.md` | `/charts/rollingoption` (lake options) | ✅ |
| `12-option-chart.md` | `/charts/rollingoption` precise request/response schema | ✅ |
| `09-instrument-master.md` | scrip-master CSV, SecurityId rule, index gate | ✅ |

> **Track 1 scope (CLAUDE.md §1): Data APIs only** — Historical / Intraday
> / Expired-Options + auth + instrument master + enums. The live/trading
> surfaces (Option Chain snapshot, Live Market Feed + Full Market Depth
> WebSockets, Live Order Update, Orders / Super / Forever / eDIS / Funds /
> Portfolio) are kept in the consolidated reference for completeness but
> are **explicitly OUT of scope** and NOT implemented — BruteX is a
> historical backtest engine: **no live data, no order placement.**
