# GDFL extract/ingest — pointer to the sibling design deliverable

> **Decision (this archive):** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> This page is deliberately a POINTER, not a design.

## The three delivery modes, one line each

1. **REST pull** — paced `GetHistory`/`GetHistoryAfterMarket` GETs against the
   customer-issued endpoint (`api/REST_API.md`); only useful within GDFL's
   days-scale 1-second-class history window (`VERDICT.md`).
2. **WS forward capture** — recording the `SubscribeRealtime` ~1/s L1 stream
   (`api/WEBSOCKET_API.md`); a LIVE-feed leg, which stays **OUT of the BruteX
   engine/dashboard per DEC-LIVE-REMOVE-001** (MIGRATION.md #862) — if built at
   all, it lives outside BruteX (TickVault-side or a fresh ADR).
3. **Vendor CSV dumps** — operator-supplied sample/export files in the measured
   `Ticker,Date,Time,LTP,BuyPrice,BuyQty,SellPrice,SellQty,LTQ,OpenInterest`
   shape (`VERDICT.md` §3), ingested as files.

## Where the data would land

GDFL's 1-second L1 snapshot records are quote/trade snapshots, not OHLCV bars —
they do not fit the engine's bar lake schema (`bars/<exch>/<segment>/...` holds
OHLCV+OI candles on the 2-decimal tick grid). Any GDFL ingest therefore lands in a
**NEW tick-class store beside `bars/`** (its own schema, partitioning and dedup
law), NEVER in the engine's bars lake, never in run identity, and never in the
52→60-condition vocabulary — the BruteX engine surface stays
NIFTY/BANKNIFTY/SENSEX (+ the ADR-0012 futures track) unchanged.

## The pointer

The full extract/ingest design — schemas, dedup/idempotence law, pacing,
completeness accounting, and the capture-leg placement decision — is owned by
`DEC-GDFL-INGEST-DESIGN-001` (a sibling deliverable; its ledger entry is
coordinator-reserved and not yet in MIGRATION.md) and lands beside this file as
`INGEST_DESIGN.md`. Nothing in this archive authorizes building it.
