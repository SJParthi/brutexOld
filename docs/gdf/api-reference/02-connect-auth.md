# 02 — How to connect + authentication (WebSocket & REST)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **How To Connect Using
WebSockets API** · **How To Connect Using REST API** (+ auth model).

## 1. The one credential

| Claim | Value | Source (access 2026-07-15) | Confidence |
|---|---|---|---|
| Credential | ONE **API key** per account (GUID-looking string in third-party code); no username/password pair, no OAuth, no TOTP | GFDLWS 0.0.4 README + gfdl-rest 1.0.3 README (pypi-fetch) | high |
| Endpoint provisioning | Per-customer: the vendor reference states that the endpoint URL and port are issued to each subscriber by Global Datafeeds (paraphrased) — the docs never print the production host | GFDLWS README §How to Connect (pypi-fetch) | high |
| Key issuance | Purchase → key from the team; trial/API-key requests via `sales@globaldatafeeds.in`; "Take A Free Trial" on `/apis/` | search-relay of `/apis/` + how-to-connect pages | medium |
| Key expiry | Server-side: `ExpirationDate` (epoch) in the GetLimitation response — no client-visible refresh flow | GFDLWS README (pypi-fetch) | high |

## 2. WebSocket connect + authenticate

Connect to `ws://<endpoint>:<port>` then authenticate ONCE per connection,
immediately after connect; all subsequent requests ride the authenticated
connection (GFDLWS `ws.py`, pypi-fetch, high).

Request — JSON key shape (vendor identifiers kept verbatim as facts; the
key value is a placeholder):

```json
{"MessageType": "Authenticate", "Password": "<API key>"}
```

Response: one frame with `MessageType: "AuthenticateResult"` carrying
`Complete` (bool — `true` on success) and `Message` (a server greeting
string). (Field names from the vendor README sample, summarized — see the
licensing-posture section in [README.md](./README.md).)

- On `Complete: false` the vendor sample simply re-sends Authenticate
  (retry loop). (pypi-fetch, high)
- The vendor Python client connects with
  `websockets.connect(endpoint, max_size=2**50)` and a 60 s connect
  timeout — i.e. the vendor expects VERY large single WS frames
  (option-chain / exchange-snapshot responses). (pypi-fetch, high)
- Heartbeat/ping protocol: nothing in the vendor lib or any retrieved
  doc — server behavior **UNVERIFIED**.

### Observed production endpoints (NOT vendor-printed)

| Surface | Value | Evidence | Confidence |
|---|---|---|---|
| WS | `ws://nimblewebstream.lisuns.com:4575` (also with trailing `/`) | TWO independent third-party repos (AnkushSinghGandhi/DeltaStream-OptionAnalysis2 `.env.example` + `gdf_provider.py`; developerakkoo/Stonks-API `getNifty.js`), github-fetch | medium |
| Port 4575 vendor corroboration | vendor FAQ connectivity test: telnet `test.lisuns.com` port `4575` ("screen goes blank" = reachable) | `…/faqs/faqs/` via search-relay | medium |
| TLS WS (`wss://`) | zero GitHub hits for `wss://nimblewebstream`; no vendor page retrieved states one | github-fetch (negative) | UNVERIFIED |

## 3. REST connect + authenticate

NO session — every request carries the key as a query parameter
(gfdl-rest `base_api.py`, pypi-fetch, high):

```
GET {base_url}/{FunctionName}/?accessKey=<API key>&<param>=<value>…
```

- Optional output selectors appended by the vendor client: `xml=true`
  (XML body) and `format=CSV` (CSV body); default JSON. (pypi-fetch, high)
- Observed production base: `http://nimblerest.lisuns.com:4531` (≥4
  independent third-party repos, github-fetch, medium); TLS variant
  `https://nimblerest.lisuns.com:4532` observed once
  (`http.client.HTTPSConnection("nimblerest.lisuns.com", 4532)`,
  github-fetch, medium-low).
- Wire example from a working third-party integration (github-fetch):
  `http://nimblerest.lisuns.com:4531/GetLastQuote/?accessKey=<key>&exchange=NFO&instrumentIdentifier=NIFTY-I`

## 4. Security posture (honest observations)

- Third-party code widely uses **plain `ws://` (4575) and plain `http://`
  (4531)** with the accessKey in the URL query — credentials travel
  unencrypted unless the client chooses the TLS REST port 4532. A TLS
  WebSocket endpoint is UNVERIFIED. (github-fetch + negative search, 2026-07-15)
- Multiple third-party repos leak real-looking accessKeys in public code;
  no retrieved doc mentions IP binding (UNVERIFIED either way).
- Adapter guidance for BruteX/TickVault: treat the API key as a secret
  (AWS-SSM style, per the house DEC-AWS-ONLY-CREDS-001 posture), prefer
  the TLS REST port, and PROBE for a TLS WS endpoint with the vendor
  before any production use.

## 5. Session/lifecycle summary

| Property | WS | REST |
|---|---|---|
| Auth | once per connection (`Authenticate` message) | every call (`accessKey` param) |
| Token refresh | none (no token — key only) | none |
| Key expiry | server-side `ExpirationDate` (GetLimitation) | same |
| Reconnect discipline | client-side (vendor sample: retry Authenticate on failure) | stateless |
| Concurrent connections per key | UNVERIFIED | n/a |
