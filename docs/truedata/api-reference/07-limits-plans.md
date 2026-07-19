# 07 — Rate limits, connection limits, plans & pricing

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Rate limits (vendor-documented)

Source: KB article "Request/Call Limit for Historical Data (REST API) and
Real Time Data (WebSocket API)" (403-blocked direct; content via
search-relay — the numbers were IDENTICAL across two independent relays
on 2026-07-15; medium confidence).

| Surface | Limit |
|---|---|
| REST historical — tick data | **5/second · 300/minute · 18,000/hour** |
| REST historical — minute-bar data | **10/second · 600/minute · 18,000/hour** |
| WebSocket — connections | "You can connect only from 1 place at a time (1 login instance)" |
| WebSocket — symbols | addSymbol / removeSymbol / getMarketStatus / logout operations are "combined and limited according to your Plan's symbols limit" — symbol count is a PLAN attribute; exact counts per plan UNVERIFIED |

Enforcement evidence (pkg-source, high): a history rate-limit breach
returns body `API request quota exceeded!` (the Node SDK sleeps 1 s and
retries); analytics/greeks return HTTP 429 with a base64-encoded error
body (the Python SDK raises `TooManyRequestsError`). No daily cap
statement was retrieved (UNVERIFIED whether one exists beyond the
18,000/hour caps).

## 2. Plans & pricing

| Claim | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Pricing page | truedata.in/price ("TrueData Price Plans for NSE, BSE, MCX Data & APIs"; also /information/price) exists | search-relay titles | medium |
| Numeric prices | **NOT RETRIEVED** — page 403-blocked; relays report pricing rendered interactively; third-party mirrors also blocked in this environment | — | **UNVERIFIED** |
| API pricing model | Custom-quoted: "Once you are satisfied with the API services, the API Team will share pricing with you after going through your exact requirements" (requirements = exchanges, symbols, data type) | KB "Getting Started" + product page via search-relay | medium |
| Subscription discount | "10% discount on 1 year subscriptions" | search-relay of truedata.in/price snippet | medium |
| Trial | Up to **10 days free trial** after the signup form on /products/marketdataapi; sandbox (wstest.truedata.in) + real-time data during development | KB via search-relay + Scribd summary via search-relay | medium |
| Plan names | No named API tiers retrieved. "Velocity" is a separate desktop-feed PRODUCT (Velocity 2.0), not an API plan name, per the KB category structure | search-relay | medium; plan-name vocabulary UNVERIFIED |
| What's bundled | Real-time plans include tick + 1-min + 5-min streams (tick+1min or tick+5min combos); bid-ask L1 default; historical REST included with defaults tick=5 days / intraday bars=6 months / EOD=10+ yrs; **Extended History is an ADD-ON**; replay feed included free for active API subscribers; 1-min/5-min bar streams "need backend enablement" | KB + PyPI descriptions (direct-fetch for the enablement quote) | medium/high |
| Exchange-approval dimension | "Kindly note that data that is not enabled by default may require exchange approvals, which vary depending on the specific exchange and segment in question." (verbatim, vendor PyPI description); segment access (e.g. BSE, MCX) subject to exchange approvals/compliance review; trial "subject to exchange approval and compliance review" | PyPI truedata description (direct-fetch) + product page via search-relay | high/medium |
| Payment/refund terms | UNVERIFIED (not retrieved) | — | UNVERIFIED |

## 3. Fair-use / scale signals

- The Node SDK batches `addsymbol` at 1500 symbols per WS frame ("max
  65000 characters") — large plan symbol counts exist (pkg-source, high;
  plan sizes still UNVERIFIED).
- The full-market feed (all-symbols push) is a DISTINCT entitlement
  (`rtConnectFullFeed` / `full_feed=True`, port 9084 in the vendor
  sample) (pkg-source/github-fetch, high; commercial terms UNVERIFIED).

## 4. Historical context

An older external reference recorded in this repo
(`docs/research/ZERODHA_HISTORICAL_FEASIBILITY.md` §2 alternatives row,
researched 2026-07-10): TrueData's 1-min IEOD history was bundled with
"Velocity Standard or higher" plans only, with Velocity Lite requiring a
separate IEOD purchase — that reference concerned the VELOCITY desktop
product line, was itself search-relayed with the pricing page 403-blocked,
and should NOT be conflated with current Market Data API plan terms
(which are custom-quoted per §2 above).

## 5. Probe checklist before any commercial conclusion

1. Numeric plan prices + the Extended-History add-on price AND depth
   (1-min back to which year? tick back to which year?).
2. Per-plan symbol counts (WS) + whether multiple WS logins can be bought.
3. Whether the 18,000/hour caps flex by plan.
4. Full-feed entitlement terms (port 9084 access).
5. Exchange-approval lead times for BSE/MCX segments.
