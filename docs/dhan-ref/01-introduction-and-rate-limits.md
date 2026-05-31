# Dhan v2 — Introduction & Rate Limits

> **Verified source:** DhanHQ v2 API docs (https://dhanhq.co/docs/v2/) +
> the operator-maintained `tickvault/docs/dhan-ref/01-introduction-and-rate-limits.md`
> (fetched 2026-05-31). Track 1 is **signal-only research** — only the
> Data APIs (Historical / Expired-Options) below are in scope. Orders,
> Super Order, Forever Order, EDIS, Funds, Postback, Live Feed, and
> Full Market Depth are **OUT of scope** (CLAUDE.md §1).

## Base URL

```
https://api.dhan.co/v2/
```

Auth-token issuance uses a separate host: `https://auth.dhan.co/` (see
`02-authentication.md`). Instrument-master CSVs are served from
`https://images.dhan.co/` (see `09-instrument-master.md`).

## Rate limits by category

| API category | Per second | Per minute | Per hour | Per day |
|---|---|---|---|---|
| Order APIs | 10 | 250 | 1,000 | 7,000 |
| **Data APIs (Historical, Option Chain)** | **5** | — | — | **100,000** |
| Quote APIs | 1 | unlimited | unlimited | unlimited |
| Non-Trading APIs | 20 | unlimited | unlimited | unlimited |

Notes (verified):

- The **Data API bucket is the only one Track 1 touches**: `5 req/s`,
  `100,000/day`, with **no documented per-minute or per-hour cap**.
- Order APIs additionally cap "order modifications at 25 per order"
  (irrelevant to Track 1 — no order placement).

## BruteX adapter policy (CLAUDE.md §2 Rule 6)

- The Dhan adapter caps at an effective **`5 × 0.6 = 3 r/s`** safety
  baseline, with the existing **adaptive token bucket** allowed to
  auto-tune within `[3..5] r/s` (never above the verified `5 r/s`
  without explicit operator approval). This mirrors the Groww
  adapter's adaptive limiter, re-pinned to Dhan's verified ceiling.
- The `100,000/day` quota is enforced by
  `brutex/orchestrator/quota_tracker.py` (re-pinned from Groww's
  identical 100k/day figure — see DEC-GROWW-DHAN-REVERT-001).
- Track 1's typical first-run + daily-incremental pull is well under
  0.1 % of the daily quota.

## Honest-envelope flags (Golden Rule #1 / #10)

- **Data-API bucket sharing** with Quote/Order buckets is NOT
  re-confirmed for Dhan; treat the `5 r/s` Data ceiling as the binding
  constraint and do not assume independent buckets.
- INDIA VIX / index `securityId` availability on the historical
  endpoint is **UNVERIFIED** until the Phase 0 probe resolves it from
  the instrument master (see `09-instrument-master.md`).
