# ADR 0002 — Revert Groww → Dhan (Track 1 backtesting data source)

- **Status:** Accepted (operator directive 2026-05-31)
- **Decision id:** DEC-GROWW-DHAN-REVERT-001 (MIGRATION.md #341)
- **Supersedes:** ADR 0001 (DEC-VENDOR-PIVOT-001, Dhan → Groww, 2026-05-26)
- **Scope:** Track 1 backtesting data source ONLY. Live execution /
  order placement / WebSocket feed remain OUT of scope (CLAUDE.md §1).

## Operator directive

> "remove everything entirely related to groww … meanwhile take these
> dhan docs … you can directly take the code of these also which we
> have already implemented in tickvault" (2026-05-31)

The operator supplied the verified Dhan v2 reference (now under
`docs/dhan-ref/`) and pointed at the existing tickvault Dhan
implementation as a code reference.

## Context

ADR 0001 pivoted BruteX from Dhan → Groww on 2026-05-26 and wiped the
prior Dhan integration. Critically (verified 2026-05-31 — Golden
Rule #1, not assumed):

- The tag `v-pre-groww-pivot` that ADR 0001 / CLAUDE.md §6 claim
  preserves the old Dhan code **does not exist** in this repository.
- This clone's history is **50 commits deep** and contains **no Dhan
  file in any reachable object** (`git rev-list --all --objects |
  grep -i dhan` → empty).

Therefore the revert is a **from-scratch rebuild**, NOT a `git revert`.
The Groww adapter, by contrast, is deeply wired into the core
(cli.py ingest driver; dashboard pull/instrument/universe flow;
`credentials.py`; the `groww_symbol` field in `data/watermark.py`;
`costs/types.py` `Broker` literal; `quota_tracker.py`), so the strip
and the Dhan rebuild are **one atomic migration**, not separable
phases.

## Decision

**Adopt a full revert Groww → Dhan for the backtesting data source.**

| Surface | Groww (remove) | Dhan (rebuild) |
|---|---|---|
| Adapter package | `brutex/adapters/groww/` (16 modules) | `brutex/adapters/dhan/` (MarketAdapter protocol) |
| Vendor SDK / extra | `growwapi` extra in `pyproject.toml` + CI | HTTP client (`requests`/`httpx`) — Dhan v2 REST |
| Spot fetch | `get_historical_candles` row-arrays | `POST /charts/intraday` + `/charts/historical` columnar arrays — **SPOT underlying only** |
| Options fetch | n/a | `POST /charts/rollingoption` — **ALL option OHLC (always expired in backtest), never via the spot endpoints** |
| Instrument key | `groww_symbol` string | `SECURITY_ID` numeric (CSV-resolved, never hardcoded) |
| Auth | Groww TOTP | Dhan `access-token` (JWT) + `dhanClientId`, TOTP refresh |
| Rate limit | 5 r/s · 100k/day (Groww) | **5 r/s · 100k/day (Dhan)** — adaptive `[3..5]` |
| Price unit | rupees → ×100 paisa | rupees → ×100 paisa (unchanged boundary) |
| Chunk window (1m) | 30 days/call | **90 days/call** (single Dhan cap) |
| Audit `vendor` stamp | `groww` | `dhan` |

Unchanged (vendor-neutral by design — CLAUDE.md §11 COMMON): the data
lake, feed/resampler, execution sim, `brutex.money`, audit hash-chain,
the adaptive token bucket, the work-item state machine, and
`adapters/protocol.py` (`MarketAdapter`). The **brute-force engine is
untouched** (CLAUDE.md §2 Rule 3) — same 3 indexes + INDIA VIX
reference-only.

## Consequences

- The `growwapi` dependency leaves the tree; Dhan v2 is a plain
  authenticated REST surface (no vendor SDK required).
- `1m`-only ingest (DEC-CANONICAL-1M-SOURCE-001) is **reinforced** —
  Dhan reconstructs each minute from the official exchange feed, the
  original data-fidelity motivation for that decision.
- Index `SECURITY_ID`s and INDIA VIX availability are a **Phase 0
  HARD-GATE** (resolve from the CSV + live probe) before any
  production pull — see `docs/dhan-ref/09-instrument-master.md`.
- Migration lands as a sequence of individually-green commits
  (additive Dhan adapter first, then the atomic cli/dashboard/schema
  flip + Groww deletion), per the Merge-Readiness Charter (CLAUDE.md
  §2 Rule 13) — never a half-broken intermediate.

## Honest envelope (Golden Rule #10)

- "Remove everything related to Groww" is a ~6 KLoC strip + ~6 KLoC
  rebuild to the 100%-coverage / mutation / O(1)-bench bar; it is
  delivered incrementally, not in one shot. This ADR + `docs/dhan-ref/`
  is installment 1 (spec/ADR before code — mirrors how ADR 0001
  shipped as "Phase 0a, no adapter code").
- Several Dhan facts remain UNVERIFIED pending the Phase 0 probe
  (index ids, INDIA VIX presence, Data-bucket sharing) and are flagged
  as such in each `docs/dhan-ref/*` file rather than asserted.
