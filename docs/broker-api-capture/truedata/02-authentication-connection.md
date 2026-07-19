# TrueData — Authentication, Hosts, Ports & Connection Management

> Sources:
> - https://wstest.truedata.in/ (official Sandbox "WebSocket/REST JS Client", fetched 2026-07-15)
> - https://pypi.org/project/truedata/ and https://pypi.org/project/truedata-ws/ (official Python READMEs)
> - https://www.npmjs.com/package/truedata-nodejs (official Node.js README)
> - https://www.nuget.org/packages/TrueData-DotNet/ (official .NET README)
> - https://feedback.truedata.in/knowledge-base/article/user-already-connected-error-solution-is-force-logout
> - https://feedback.truedata.in/knowledge-base/article/full-market-feed-replay-is-now-live-market-data-api_1
> - https://feedback.truedata.in/knowledge-base/article/maintenance-timings-for-truedata-market-data-api
> - https://feedback.truedata.in/knowledge-base/article/requestscalls-limit-via-historical-and-real-time-api
> - https://github.com/Nahas-N/Truedata-sample-code (official sample code)

There is **one credential pair (user id + password)** issued by TrueData, used for both the real-time WebSocket feed and the historical REST service. Credentials are provisioned by the API team (trial or paid); entitlements (tick vs 1min vs 5min streaming, bid/ask, greeks, segments, symbol limit) are enabled server-side per account.

## 1. Hosts and ports

| Service | Host | Port(s) | Notes |
|---|---|---|---|
| Real-time WebSocket (production) | `push.truedata.in` | **8082** (default live port) | `wss://push.truedata.in:8082` — user-specific; some accounts are authorised on other ports (e.g. **8084**): "If you have been authorized on another live port please enter another parameter" (truedata-ws README) |
| Real-time WebSocket (replay feed) | `replay.truedata.in` | 8082 (same port) | Post-market full market feed replay; swap host only |
| Real-time WebSocket (beta) | `pushbeta.truedata.in` | 8088 | Appears in official sample code (callback_sample_code.py) |
| Full Market Feed (all-exchange snapshot stream) | `push.truedata.in` | 9084 | `TD_live(..., live_port=9084, full_feed=True)` in official full_feed_sample_code.py |
| Sandbox real-time | via https://wstest.truedata.in | 8082 shown as "Real Time (Port 8082)"; KB sandbox how-to also references selecting "RT (8086)" | Browser JS client for testing raw feed |
| Historical REST — auth | `https://auth.truedata.in` | 443 | Token endpoint (grant_type login) |
| Historical REST — data | `https://history.truedata.in` | 443 | All history endpoints (getbars/getticks/…) |
| General API host (misc requests) | `https://api.truedata.in` | 443 | e.g. force-logout endpoint |

Port summary seen across official docs: **8082** (production RT default), **8084** (alternate assigned RT port), **8086** (sandbox RT selection), **8088** (pushbeta), **9084** (full feed).

### Official port-assignment announcement (Telegram @truedata_ws_api, msg 123 — verbatim)

> "Please make sure you are always using the ports assigned to you. Specific ports provided by us, as of date, are:-
>
> Ports assigned for production RT feed are - **8082 or 8084**. (Default is 8082. 8084 only if specified to you)
>
> Ports during migration to new RT feed - Enabled from time to time when feed formats are upgraded / changed / new additions - **8083**.
>
> Ports for sandbox RT feed environment - Having only few symbols to check feed flow in a sandbox environment - **8086**.
>
> Note :- All ports other than the default port provided to you need to be enabled by us at the backend. Please email us on support@truedata.in in case of any doubt about the same."

### Library connection log messages (official, verbatim — useful for grepping your logs)

```
WARNING :: Connected successfully to TrueData Real Time Data Service...
WARNING :: Connected successfully to TrueData Historical Data Service...
WARNING :: Disconnected from Real Time Data WebSocket Connection !
WARNING :: Disconnected from Historical Data REST Connection !
```

Known dependency pitfall (official): `websocket-client==0.58.0` broke real-time connections; pin `websocket-client==0.57.0` (fixed in truedata-ws ≥3.0.1).

## 2. WebSocket authentication

Login is passed on the connection URL as query parameters (no separate token for WS):

```
wss://push.truedata.in:8082?user=<USER_ID>&password=<PASSWORD>
```

- .NET: `tDWebSocket = new TDWebSocket("your_id", "your_pwd", "wss://push.truedata.in", 8082);`
- .NET replay: `tDWebSocket = new TDWebSocket("user_id", "user_pwd", "wss://replay.truedata.in", 8082);`
- Node.js: `rtConnect(user, pwd, symbols, port, bidask = 1, heartbeat = 1, replay = 0, url = 'push');`
- Python (current): `TD_live('<login_id>', '<password>')` — default url `push.truedata.in`, default live port 8082.
- Python (legacy): `TD('<login_id>', '<password>', live_port=8082, url='push.truedata.in')`.

On a successful connect the server responds with a handshake/welcome JSON (the .NET README shows client code branching on a message containing `"TrueData"` for the connect acknowledgement — i.e. the welcome message text contains "TrueData"; subscribe after receiving it).

**Connection control:** you can connect only from ONE place at a time (1 login instance) per user (KB: Request/Call limit article).

## 3. Historical REST authentication (bearer token)

Flow (as exposed on the official sandbox page https://wstest.truedata.in/ — "TrueData REST History Service"):

1. `POST https://auth.truedata.in/token` with form fields:

| Field | Value |
|---|---|
| `username` | your TrueData user id |
| `password` | your password |
| `grant_type` | `password` |

The sandbox login panel shows exactly these three inputs: `User Id`, `Password`, `grant_type` against base URL `https://auth.truedata.in`, with Login/Logout buttons and a "Login Response" box (OAuth2 password-grant style; response contains the access token / bearer token).

2. Use the returned token on every history call:

```
GET https://history.truedata.in/<endpoint>?...
Authorization: Bearer <access_token>
```

- Node.js wraps this as `historical.auth(user, pwd); // For authentication.`
- .NET wraps this as `tDHistory = new TDHistory("your_id", "your_pwd"); tDHistory.login();`
- Python wraps this inside `TD_hist(user, pwd)` (historical login handled internally; truedata-ws 5.0.8 release note: "Bug Fix - annoying behaviour of historical login").

## 4. Force logout ("User Already Connected" error)

KB article (verbatim): "We have a Force Logout method in case you wish to force a log out from all places or are unable to reconnect."

1. Fire the URL below to force logout from all other places you might be logged in.
2. Wait ~5 minutes, then log in again.

```
URL for Real-Time:
https://api.truedata.in/logoutRequest?user=xxx&password=yyy&port=8082
```

(Replace `xxx`/`yyy` with credentials and `port` with your assigned live port.)

## 5. Automatic reconnection

- Python & Node libraries check internet connectivity and automatically reconnect the WebSocket when the connection is steady, then **re-subscribe previously subscribed symbols automatically** and restart live data seamlessly (truedata-ws ≥3.0.2; truedata-nodejs README: "The library will check for internet connection and once steady will try to re-connect the Websocket.").
- Python `truedata` 6.0.4 change: "removing automatic reconnection after manual disconnection" (i.e. `disconnect()` is final).

## 6. Maintenance windows (WS-API)

| When | Window | Note |
|---|---|---|
| Trading weekdays | 07:30 am – 08:00 am | TrueData WebSocket API Server Maintenance |
| Every Saturday & Sunday | 07:30 am – 10:30 am | TrueData WebSocket API Server Maintenance |

"Services would return to normal post these Maintenance Timings."

Occasional ad-hoc notices are posted on Telegram (e.g. "mock data ingestion activities will be carried out today until 6:00 PM … kindly refrain from using the REST APIs").

## 7. Compression

- Python `truedata` v7.0.0: "all websocket messages are compressed for performance improvements by default" (LZ4; the lib pins `lz4==3.1.3` historically, v6.0.3 moved to latest lz4).
- v6.0.5: "added compression in analytical calls for faster performance".
- truedata-ws 4.2.1: "Decompression added for pulling smaller files and for faster downloads … for gethistoric data & getnhistoric data".
- .NET package depends on `K4os.Compression.LZ4` and its 1.4.7 release note is "Fixes for compressed payload" — i.e. the raw WS/REST payloads are LZ4-compressed and official SDKs decompress transparently.
