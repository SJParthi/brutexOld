# TrueData — Official API Announcements Archive (Telegram @truedata_ws_api)

> Source: https://t.me/s/truedata_ws_api (public web archive; pages fetched 2026-07-15: latest, ?before=130, ?before=99)
> This channel is TrueData's primary/first channel for API information transfer (maintenance, issues, code ideas, library updates). 1.52K subscribers; 94 photos, 14 videos, 6 files, 86 links.

Selected technically-significant posts (chronological, verbatim key content):

## Historical REST migration era (2021)

- **"REST APIs for Historical Data - Coming soon"** (msg 73): "We are in the process of moving away from Websockets for Historical data to REST / HTTP based APIs. These REST APIs would help us add more features and allow for much faster downloads of historical data. i) The Real time feed would continue … over Websockets. ii) Sufficient overlap period … iii) python library would be updated…"
- **"Gradual rollout of the Historical REST APIs"** (msg 79): features coming: "a) Gainers & Losers b) Gainers & Losers by Volume c) Gainers / Losers by OI Change / OI"; "Expected overlap of the deprecated Historical Websocket and the Historical REST apis is about 1 month."
- **"Issues with the websocket-client 0.58.0 package"** (msg 83): downgrade with `pip install websocket-client==0.57.0`.
- **"Python Library Upgraded v 3.0.1"** (msg 90): REST history implemented ("super fast downloads"), WS history deprecated, Get Bhavcopy added, works with websocket-client 0.58.0. "Raw data feed documentations would also be updated shortly."
- **"TrueData Market Data API Documentation v 2.1"** (msg 92): doc + **Postman collection** links (see 04-history-rest-api.md); new features: get bhavcopy eod status, get bhavcopy, get LTP (via REST).
- **"Launching soon - nodejs library"** (msg 93) and **"nodejs Library Launched - ver 1.0.3"** (msg 94): https://www.npmjs.com/package/truedata-nodejs
- **"HISTORICAL DATA VIA WEBSOCKET - SERVICE WILL TERMINATE on 30 APR 2021"** (msg 96): "deprecated … will terminate completely on 30 Apr 2021. There will be no extension." Library users: just upgrade.
- **"NSE Currencies - Now live on Websocket"** (msg 97): "NSE Currency Derivatives Segment (NSE CDS) Real time feed is now live over Websocket. Historical Data also enabled via REST." Symbol examples (verbatim): `USDINR-I, EURINR-I, JPYINR210430FUT, JPYINR-I, JPYINR21APRFUT, USDINR21042875CE`.
- **"nodejs library updated - version 1.0.5"** (msg 99): adds symbol id to Historical REST returns — "Default > getSymbolId = 0; Need symbol Id in History REST return > getSymbolId = 1".
- **"Python Library Updated - version 3.0.2 - Automatic reconnect enabled"** (msg 102): auto WS reconnect + auto re-subscribe. "Coming soon next: 1) Top Gainers & Losers 2) Ability to get tick & 1 min data simultaneously."
- **"C# .Net Sample Project (code & Sample Release build) updated to Work with Historical (REST) APIs"** (msg 103): TestWS project; test RT flow, history, and "download the entire symbol list". C# folder: https://www.dropbox.com/sh/675xq5vbtcfsjg5/AADfdD6VGaC-Wg20xGQ1WMr6a?dl=0
- (msg 104): "Historical Data is now available over REST only. Historical Data service via Websocket is now terminated."
- **"Sandbox Page updated to accommodate Historical REST Testing"** (msg 114): https://wstest.truedata.in/ now tests REST history too.
- SSL incident postmortem (msg 119): login issue from conflicted SSLs, resolved; history REST unaffected.
- Community async adapter endorsement (msg 121): https://github.com/codesutras/truedata-adapter ("Async Python Program for truedata feed processing").
- **"Testing RT Port 8083 would not be available from Friday, May 21"** (msg 123): full official port map — production **8082 / 8084**, migration/testing **8083**, sandbox **8086**; non-default ports need backend enablement (full quote in 02-authentication-connection.md).
- **"Python Library Updated - Version 4.0.1"** (msg 124): Q&A — `historical_port` → `historical_api`; connection/disconnection log lines (see 02); RAM fix: history stored only when `ticker_id` given; tick+min dual access at `min_live_data[req_id]`; log handler added; upgrade via `pip install --upgrade truedata-ws`.
- **"Subscriptions streamlined at our backend"** (msg 129): "Please ensure that you are using the correct segments and ports assigned to you. Others may not work."

## Recent operational posts (2026 era, latest page)

- "MCX feeds are up and running." (msg 351)
- Trading-holiday notices with NSE circular links (msg 352, 353: Budget Day Sunday 1-Feb-2026 market open).
- Website network-issue outage + resolution notices (msgs 363–364, 366–367).
- **REST maintenance notice** (msg 369, verbatim): "mock data ingestion activities will be carried out today until 6:00 PM. During this period, we request you to kindly refrain from using the REST APIs to avoid any inconsistencies or disruptions."
- Assorted festival greetings (non-technical).

## Historical incident patterns worth knowing

- NSE-side outages propagate to all vendors (e.g. "NSE Spot Indices feed issue … across Brokers & Data Feed Vendors"; NSE F&O/cash closure 24-Feb-2021 with special reopen timings; OI feed freezes from the exchange — msgs 74–77, 105–107).
- "Websocket APIs working fine … no need to reconnect or restart your app unless you have hardcoded the time." (msg 78 — after exchange-extended market hours.)
- Older maintenance-timing KB URL: https://feedback.truedata.in/knowledge-base/article/service-maintenance-timing (superseded by the article captured in 11-limits-errors-faq.md §5).
