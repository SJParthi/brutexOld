# TrueData historical REST API — own-words summary

> **Compiled:** 2026-07-20; evidence tiers per `../README.md`. Sources: the official
> `truedata` 7.0.1 SDK (`history/Historical_REST.py`, `TD_hist.py` — sha256-pinned
> in `../MANIFEST.md`), the 2026-07-15 official-page capture
> (`docs/broker-api-capture/truedata/04-history-rest-api.md` family), and the #1966
> pack (`docs/truedata/api-reference/04-historical-rest.md` — the deeper route
> reference). Own words; route/parameter tokens verbatim. IMPORTANT caveat carried
> from #1966: history RESPONSE shapes are reconstructed from SDK PARSING code, not
> captured wire bytes.

## Transport + auth

- Flow: `POST https://auth.truedata.in/token` (OAuth2 password grant; token TTL
  ~3600 s, SDKs re-mint with a 15–20 s early buffer) → `GET
  https://history.truedata.in/<route>` with `Authorization: Bearer <token>`.
  SDK-VERIFIED (`Historical_REST.py:32`, `TD_hist.py:12`).
- History-over-WebSocket was TERMINATED 30 Apr 2021 — REST-only since.
  PRIOR-VERIFIED-NOTE (official Telegram announcement via the 2026-07-15 capture).
- Sibling utility hosts (plain `user=<USER>&password=<PASSWORD>` params):
  `api.truedata.in` (getOptionChain/getATMStrike/getprevcloseandoi…);
  `analytics.truedata.in` + `greeks.truedata.in` (add-on REST). SDK-VERIFIED.

## Routes + request grammar

- Routes seen across the official SDKs: `/getbars` `/getticks` `/getlastnbars`
  `/getlastnticks` `/getbhavcopy` `/getbhavcopystatus` `/gettopngainers`
  `/gettopnlosers` `/gettopnvolumegainers` `/getcorpaction`
  `/getsymbolchangehistory` (Node/.NET add getLTP, 52-week H/L). SDK-VERIFIED +
  PRIOR-VERIFIED-NOTE (#1966 high).
- Time format `YYMMDDTHH:MM:SS` (2-digit year); duration alternative `1D/3W/2M/1Y`.
  SDK-VERIFIED.
- Options: `bidask=1` (tick rows carry stored L1 bid/ask — i.e. archived snapshot
  ticks), `delivery=true` (EOD delivery volume), `comp=true` (LZ4-block response,
  8-byte header). SDK-VERIFIED + PRIOR-VERIFIED-NOTE (#1966 high).
- **One symbol per request** — no multi-symbol history call exists.
  OFFICIAL-VERIFIED(capture-2026-07-15) FAQ.

## Granularities

`tick, 1min, 2min, 3min, 5min, 10min, 15min, 30min, 60min, EOD` (+ `week`/`month`
in the Python SDK, aggregated from EOD). SDK-VERIFIED (`truedata` METADATA
limitations section). A compliance note caps exchange-approved offered intervals
(Tick/1-min/2-min/5-min/15-min-delayed/EOD) — PRIOR-VERIFIED-NOTE (2026-07-16
catalog of the PR #1595 branch docs).

## Depth windows (the make-or-break)

| Class | Bundled depth | Tier |
|---|---|---|
| Tick (archived ~1-s L1 snapshots) | last **5 trading days** | OFFICIAL-SNIPPET-UNCONFIRMED (KB blocked today) + PRIOR-VERIFIED-NOTE (#1966: npm README "Max upto last 5 days of tick data" — its high tier) |
| Intraday bars (1–60 min) | last **6 months** | OFFICIAL-SNIPPET-UNCONFIRMED (same KB family; identical in the 2026-07-15 capture) |
| EOD | **10+ years** | OFFICIAL-SNIPPET-UNCONFIRMED (same) |
| "Extended History" | PAID ADD-ON; depth AND price **UNVERIFIED** (KB phrasing: 1-min "up to ~1 year" soon) | OFFICIAL-SNIPPET-UNCONFIRMED / UNVERIFIED — the #1 sales question |

Conflicting partner claim: a FYERS bundle page advertises "25 years of daily data
and 5 years of intraday data" — COMMUNITY, likely a different product line
(Velocity/IEOD); do NOT rely on 5-year intraday without a quote-stage confirmation.
Expired-contract history servability: UNVERIFIED (probe; #1966 Q3 — same shape for
both vendors). Per-request span cap / pagination: UNVERIFIED (none evidenced in
SDKs). CA-adjustment policy of served bars: UNVERIFIED.

## Rate caps

- Tick requests: **5/s · 300/min · 18,000/h**; bar requests: **10/s · 600/min ·
  18,000/h** — OFFICIAL-SNIPPET-UNCONFIRMED (KB article blocked; identical numbers
  across independent relays today AND in the 2026-07-15 capture — treat as strong
  but un-byte-read). Daily-cap existence: UNVERIFIED.
- Breach signals: response body `"API request quota exceeded!"` (the SDK sleeps 1 s
  + retries); analytics/greeks return HTTP 429. SDK-VERIFIED +
  PRIOR-VERIFIED-NOTE (#1966 high).

## The backfill verdict (BruteX-decisive)

**TrueData's bundled history CANNOT reach the BruteX 2020→today 1-minute lake floor**
— 6 months of 1-min vs a ~6-year floor, and 5 days of tick-class — so it is NOT a
Groww replacement for the lake, and no tick-class backfill exists at all (forward
capture only, exactly the GDFL verdict class). This restates
`docs/research/GDF_TRUEDATA_FEASIBILITY.md` §1 Q1: **NO for BOTH vendors as
bundled** ("order of magnitude short of the 2020→today 1-min floor"); the only
conceivable path is the unpriced, undepth'd Extended-History add-on (upload/probe
item — `../MANIFEST.md` item 5). Tier: PRIOR-VERIFIED-NOTE (in-repo feasibility
doc, 2026-07-15) + the depth rows above.
