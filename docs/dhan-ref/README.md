# `docs/dhan-ref/` — Dhan v2 API verified reference

Operator-supplied, source-cited Dhan HQ v2 API facts that the Track 1
**Dhan adapter** is built against. This is the SINGLE SOURCE OF TRUTH
for every Dhan vendor fact (CLAUDE.md §2 Golden Rule #2; no fact may be
re-derived or hallucinated — Golden Rule #1).

Provenance: operator directive 2026-05-31 to revert Groww → Dhan
(DEC-GROWW-DHAN-REVERT-001, MIGRATION.md #341). Facts fetched from the
operator's `tickvault/docs/dhan-ref/*` mirror + DhanHQ public docs
(https://dhanhq.co/docs/v2/) + the pasted Expired-Options page.

| File | Surface |
|---|---|
| `01-introduction-and-rate-limits.md` | base URL, rate limits, quota |
| `02-authentication.md` | access-token / TOTP / headers / §11 AUTOMATED |
| `05-historical-data.md` | `/charts/historical` + `/charts/intraday` (CORE) |
| `06-expired-options-data.md` | `/charts/rollingoption` (lake options) |
| `09-instrument-master.md` | scrip-master CSV, SecurityId rule, index gate |

> Track 1 scope (CLAUDE.md §1): **Data APIs only** — Historical /
> Intraday / Expired-Options. Orders, Funds, Live Feed, Postback, EDIS,
> Full Market Depth are explicitly OUT of scope.
