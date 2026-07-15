# 08 — Limits, per-key entitlements (GetLimitation), plans & pricing

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **FAQs** (limits) +
**Pricing & Sales** + the GetLimitation function.

## 1. The per-key entitlement model — GetLimitation (vendor-authored, HIGH)

Everything is configured PER API KEY. The `GetLimitation` response —
GFDLWS 0.0.4 README sample, values ILLUSTRATIVE (not plan quotes):

```json
{
"GeneralParams":{
  "AllowedBandwidthPerHour":-1.0,
  "AllowedCallsPerHour":-1,
  "AllowedCallsPerMonth":-1,
  "AllowedBandwidthPerMonth":-1.0,
  "ExpirationDate":1485468000,
  "Enabled":true
},
"AllowedExchanges":[
  {"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"CDS"},
  {"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"MCX"},
  {"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NFO"},
  {"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NSE"},
  {"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NSE_IDX"}
],
"AllowedFunctions":[
  {"FunctionName":"GetExchangeMessages","IsEnabled":false},
  {"FunctionName":"GetHistory","IsEnabled":true},
  {"FunctionName":"GetLastQuote","IsEnabled":false},
  {"FunctionName":"GetLastQuoteArray","IsEnabled":false},
  {"FunctionName":"GetLastQuoteArrayShort","IsEnabled":false},
  {"FunctionName":"GetLastQuoteShort","IsEnabled":false},
  {"FunctionName":"GetMarketMessages","IsEnabled":false},
  {"FunctionName":"GetSnapshot","IsEnabled":true},
  {"FunctionName":"SubscribeRealtime","IsEnabled":false}
],
"HistoryLimitation":{
  "TickEnabled":true,"DayEnabled":true,"WeekEnabled":false,"MonthEnabled":false,
  "MaxEOD":100000,"MaxIntraday":44,
  "Hour_1Enabled":false,"Hour_2Enabled":false,"Hour_3Enabled":false,"Hour_4Enabled":false,"Hour_6Enabled":false,"Hour_8Enabled":false,"Hour_12Enabled":false,
  "Minute_1Enabled":true,"Minute_2Enabled":false,"Minute_3Enabled":false,"Minute_4Enabled":false,"Minute_5Enabled":false,"Minute_6Enabled":false,"Minute_10Enabled":false,"Minute_12Enabled":false,"Minute_15Enabled":false,"Minute_20Enabled":false,"Minute_30Enabled":false,
  "MaxTicks":5
},
"MessageType":"LimitationResult"
}
```

Interpretation (field names = high; semantics partly inferred):
`-1` = unlimited; `DataDelay` per exchange (0 = realtime — delayed feeds
are a sold variant); function-level enable flags; per-interval history
enablement + record caps (`MaxTicks`, `MaxEOD`, `MaxIntraday`); key
`ExpirationDate` (epoch); bandwidth + call quotas per hour/month.

**Operational law for any adapter:** call `GetLimitation` FIRST and treat
its answer — not this document — as the capability surface of the key in
hand. Two keys can differ on every axis above.

## 2. Documented operational limits

| Limit | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Symbol limit | per API key, per requirements; exceeding ⇒ message **"Reached instrument limitation."** | vendor FAQ via search-relay | medium |
| Requests/hour | plan-based; FAQ examples **1800, 3600, 7200 per hour**; hitting it ⇒ wait for the hourly window to reset | vendor FAQ via search-relay | medium |
| Array-call cap | max **25** instruments per GetLastQuoteArray* / GetSnapshot / GetLastQuoteArrayOptionGreeks call | vendor READMEs (pypi-fetch) | high |
| Search cap | GetInstrumentsOnSearch returns max **20** instruments | vendor READMEs (pypi-fetch) | high |
| Per-second rate cap | **UNVERIFIED** — no retrieved doc states one (the model is per-hour quotas + streaming) | — | UNVERIFIED |
| Concurrent connections per key | **UNVERIFIED** | — | UNVERIFIED |
| Heartbeat / idle policy | **UNVERIFIED** | — | UNVERIFIED |

## 3. Plans & pricing

| Claim | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Pricing page | `…/pricing-sales/api-pricing/` ("API Pricing") + "Pricing & Sales Archives" exist | search-relay | medium (existence) |
| Numeric API prices | **UNVERIFIED** — the relay could not extract figures; the page appears to gate specifics behind contact/quote ("flexible pricing policy"; choose realtime-only / historical-only / realtime+historical API) | search-relay | UNVERIFIED (numbers) |
| Plan axes | exchanges/segments enabled · symbol limit · functions enabled · history intervals + record caps · calls/hour · realtime vs delayed | GetLimitation shape (high) + FAQ (medium) | high (axes) |
| Trial | "Take A Free Trial" on `/apis/`; trial/API-key via sales@globaldatafeeds.in | search-relay | medium |
| STALE third-party price point | "NimbleDataPro2 yearly NSE(200 sym)+NFO(200)+MCX(200) ≈ ₹70,000/yr" — a 2019-era reference for an OLDER DESKTOP product, NOT current API pricing. Recorded only to kill its reuse (it circulates in vendor comparisons, e.g. the alternatives row of `docs/research/ZERODHA_HISTORICAL_FEASIBILITY.md`) | tradingqna/marketcalls via search-relay | low + STALE — do not cite as current |
| Desktop-product pricing pages | marketcalls.in/premium/globaldatafeeds/{nimbledatapro, nimbledataproplus, nimbledatapropluslite, ieod-pricing}; reseller traderskart.in | search-relay | low (figures not extracted) |
| Historical bundling | API plans bundle the shallow history windows ([04](./04-historical-data.md) §6A); deep 1-min sold as IEOD file packs ([04](./04-historical-data.md) §6B) | Type-of-Data relay + marketcalls relay | medium/low |

## 4. Fair-use / ToS

No dedicated fair-use policy page was retrieved; enforcement is
mechanical via the per-key quotas above. Redistribution / end-user
data-agreement terms (GDF is an exchange-authorized vendor — see
[10](./10-company-support-products.md)): **UNVERIFIED** (pages not
retrieved).
