# TrueData (truedata.in) — Complete API Documentation Archive

Captured: **2026-07-15**. Repo-ready Markdown mirror of TrueData's public Market Data API documentation (website, support KB/FAQ, sandbox, official SDK READMEs, official sample code, official Telegram announcements). Every file carries its source URLs.

## Quick orientation

- **Vendor**: TrueData Financial Information Pvt. Ltd. — authorised NSE/BSE/MCX data vendor.
- **Real-time**: WebSocket `wss://push.truedata.in:8082?user=...&password=...` (alt 8084; replay `replay.truedata.in:8082`; sandbox 8086; beta `pushbeta:8088`; full-feed 9084). Tick + 1min/5min bars + bid/ask (L1 NSE/MCX, L2 BSE) + greeks + touchline + heartbeats (5s) + market status.
- **History**: REST — token from `https://auth.truedata.in/token` (`grant_type=password`), then `https://history.truedata.in/...` with `Authorization: Bearer`. Methods: getTickData/getBarData/getLastNTicks/getLastNBars/getBhavCopy(+Status)/getLTP/getTopGainers/getTopLosers/getTopVolumeGainers/getCorpAction/getSymbolNameChange (+52-week H/L in .NET). Intervals 1–60min/EOD/tick; JSON or CSV.
- **Analytics add-on**: option chain (REST & streaming), IV+greeks (delta/theta/gamma/vega/rho), OI/index/industry gainers-losers, historical greeks.
- **SDKs**: Python `truedata` (current) / `truedata-ws` (legacy), Node `truedata-nodejs`, .NET `TrueData-DotNet`, Velocity External API (COM).
- **Sandbox**: https://wstest.truedata.in · **Support**: https://feedback.truedata.in · **Announcements**: https://t.me/truedata_ws_api

## Files

| File | Contents |
|---|---|
| [01-overview.md](01-overview.md) | Company, products, API catalogue, exchanges/segments, formats & latency, compliance & prohibited uses, trial/pricing/T&C, onboarding flow, product FAQs |
| [02-authentication-connection.md](02-authentication-connection.md) | Credentials model, all hosts & ports (incl. official 8082/8083/8084/8086 port map), WS query-param login, REST bearer-token flow (auth.truedata.in), force logout (api.truedata.in/logoutRequest), reconnection, maintenance, LZ4 compression, connection log strings |
| [03-realtime-websocket.md](03-realtime-websocket.md) | Subscription ops (add/remove/getMarketStatus/logout), message types (trade/bidask/bar/greeks/touchline/HeartBeat/marketstatus/welcome), full tick field table (19 fields), 1-min/5-min bar fields, L1/L2 bid-ask formats, bar-formation rules, Market Replay, Full Market Feed (9084), sandbox steps, client-side guidance |
| [04-history-rest-api.md](04-history-rest-api.md) | WS→REST migration history, doc v2.1/v2.2 + Postman collection pointers, complete endpoint surface with parameter/enum/default tables, from-to & duration formats, response shape (Timestamp/O/H/L/C/V/OI; ltp ticks; DelVolume), availability windows, rate limits, bhavcopy semantics, gainers/losers, sandbox EOD |
| [05-analytics-optionchain-greeks.md](05-analytics-optionchain-greeks.md) | Option chain streaming (args, columns incl. greeks), greeks stream fields, TD_analytics complete method surface (OI/index/industry gainers-losers, REST option chain, history greeks), corporate data API pointer |
| [06-symbols-master.md](06-symbols-master.md) | WS-API symbol formats (options YYMMDD, indices, futures, `_BSE` suffix), 17 WS-API master list URLs, 41+6 Velocity master list URLs, per-platform (Amibroker/Ninja/Metastock) sample formats, TDMaster |
| [07-python-sdk-truedata.md](07-python-sdk-truedata.md) | Current `truedata` package v7.0.1: install, TD_live/TD_hist/TD_analytics, all dataclass fields, all callbacks, history methods & caveats, release notes, official sample index |
| [08-python-sdk-truedata-ws-legacy.md](08-python-sdk-truedata-ws-legacy.md) | Legacy `truedata-ws` v5.0.11: full README — connection modes, logging, live/touchline access, req_ids, chains, history (ticker_id storage), bhavcopy, gainers/losers, 4 example strategies, complete release history |
| [09-nodejs-sdk.md](09-nodejs-sdk.md) | `truedata-nodejs`: rtConnect params, all 8 rtFeed events, control fns, historical.* full function list with defaults/enums, formatTime, examples |
| [10-dotnet-sdk.md](10-dotnet-sdk.md) | `TrueData-DotNet` v1.4.7: install, TDWebSocket events + raw JSON dispatch code (trade/bidask/HeartBeat/TrueData), TDMaster, TDHistory all methods incl. GetCorporateActions & Get52WeekHighLow, version table |
| [11-limits-errors-faq.md](11-limits-errors-faq.md) | Rate limits, connection/symbol controls, availability, error catalogue (User Already Connected → force logout; bhavcopy-not-ready; OHLC==LTP), maintenance, all 12 official FAQs with answers, support channels |
| [12-velocity-external-api.md](12-velocity-external-api.md) | Velocity External API (COM/.NET desktop): installers, install/permission steps, sample apps; sandbox page layout reference; other integration surfaces |
| [13-official-announcements-telegram.md](13-official-announcements-telegram.md) | Official Telegram announcement archive: REST migration timeline, doc/Postman releases, NSE CDS launch (+symbol examples), port announcements, library releases, incident patterns |
| [SOURCES.md](SOURCES.md) | Every URL discovered/fetched with status (OK/PARTIAL/GATED/DEAD/NOT-FETCHED) |

## Known gaps (cannot be captured without credentials/manual download)

1. **Raw-protocol PDF** ("TrueData Market Data API Documentation v2.1/v2.2") — distributed by email after signup; Dropbox copies dead, Scribd copy JS-gated. The wire-level JSON schemas herein are reconstructed strictly from official KB field lists, sandbox UI, and official SDK code (nothing fabricated); exact raw JSON key names for subscribe requests are only in that PDF.
2. **Postman collection** (`TrueData_History_REST_API_v1.1.postman_collection.zip`) — binary zip on Dropbox.
3. **Live symbol-master .txt contents** — URLs archived (06), contents change daily, not mirrored.
4. **Corporate Data API (TrueWealth) endpoint reference** — no public docs; sales-gated.
5. Account-gated behaviour (actual tokens, per-plan symbol limits) — requires live credentials.
