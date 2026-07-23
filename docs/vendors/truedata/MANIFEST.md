# TrueData docs-capture manifest — every URL attempted, 2026-07-20

> **Environment:** BruteX build sandbox; all outbound HTTPS rides a pre-configured
> egress proxy. `BLOCKED-403` below = the proxy/CDN answered HTTP 403 at fetch time
> (for NSE hosts, verified as a policy-level CONNECT denial via the proxy status
> endpoint) — an ENVIRONMENT block, NOT a statement the site is down.
>
> **Headline:** zero bytes of any `*.truedata.in` or NSE host were retrieved this
> session (0 succeeded / 11 vendor+related fetches blocked / 1 degraded). The
> compensating channels: the two official SDK wheels read in FULL (pip, sha256-pinned
> below), official-author sample fragments via authenticated GitHub code search,
> 12 search-snippet relays, and the in-repo 2026-07-15 official-page capture
> (`docs/broker-api-capture/truedata/`).
>
> **Honesty rule (mirrors `../gdfl/MANIFEST.md`):** nothing from an unreachable page
> is summarized as fact anywhere in this archive. Blocked-page labels below derive
> only from search-result titles/snippets — they identify the page, they do not
> claim its content.
>
> **Committed-content rule:** the SDK wheels were read in the session scratchpad and
> are deliberately NOT committed (the SUMMARIZE-ONLY posture, `README.md`); the
> sha256 pins below re-fetch the identical artifacts bit-for-bit.

## WebFetch attempts — truedata.in + related (2026-07-20)

| # | URL | Outcome | Notes |
|---|---|---|---|
| 1 | https://www.truedata.in/ | BLOCKED-403 | main site |
| 2 | https://feedback.truedata.in/knowledge-base/article/getting-started-with-truedata-market-data-api | BLOCKED-403 | support KB |
| 3 | https://pypi.org/project/truedata/ | FETCHED-DEGRADED | HTTP 200 but a PyPI JS "Client Challenge" shell — no content; the wheel was downloaded via pip instead (below) |
| 4 | https://medium.com/@truedatapvtltd/which-is-the-authorized-data-vendor-for-nse-india-a39d1009fb64 | BLOCKED-403 | official TrueData Medium |
| 5 | https://www.truedata.in/blog/levels-real-time-data-nse-bse-mcx | BLOCKED-403 | the "4 Levels of Real Time Data" blog (a key TBT-honesty page) |
| 6 | https://www.marketcalls.in/softwares/data-vendors-and-quality-of-datafeeds.html | BLOCKED-403 | community vendor-quality article |
| 7 | https://www.niftyindices.com/resources/authorized-data-vendors | BLOCKED-403 | index-LICENSEE list (not the realtime feed-vendor list) |
| 8 | https://fyers.in/notice-board/revision-in-truedata-subscription-pricing/ | BLOCKED-403 | partner pricing notice |
| 9 | https://truedata.home.blog/2020/03/23/the-4-levels-of-real-time-data-from-nse-bse-mcx/ | BLOCKED-403 | official blog WordPress mirror |
| 10 | https://subscriptions.fyers.in/true-data.html | BLOCKED-403 | partner plan table |

## NSE-side attempts bearing on TrueData (same session; from the NSE-TBT research log)

| # | URL | Tool | Outcome |
|---|---|---|---|
| N1 | https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Authorized_Realtime_Data_Vendors.pdf | WebFetch | BLOCKED-403 |
| N2 | same URL | curl via proxy (browser UA) | BLOCKED — `curl: (56) CONNECT tunnel failed, response 403` (proxy policy denial, confirmed via the proxy status endpoint) |
| N3 | https://www.nseindia.com/static/market-data/real-time-data-subscription | WebFetch | BLOCKED-403 (the L1/L2/L3/TBT product page) |
| N4 | https://archives.nseindia.com/content/press/Realtime_CM_CD_TBT.pdf | WebFetch | BLOCKED-403 (MTBT vendor spec v6.2) |
| N5 | https://www.truedata.in/ | WebFetch (NSE-research pass) | BLOCKED-403 (duplicate confirmation) |

Not separately attempted (host families already proven proxy-blocked this session):
archives.nseindia.com vendor-list PDF, nsearchives "List of Authoized Vendors(_2).pdf" /
"List_of_Authorized_Realtime_Data_Vendors_0.pdf", scribd.com/document/667918775 (API
PDF v2.2.1 mirror — also GATED per the prior capture), t.me/s/truedata_ws_api,
www.truedata.in/{price, market-data-apis, products/marketdataapi, about-us, why,
information/pricing}, wstest/auth/history/api/analytics/greeks.truedata.in,
nsearchives Real_Time_Tariff_Domestic PDFs (2019→2025).

## WebSearch queries (snippet-tier evidence only; all executed OK)

TrueData pass (12): WS docs/push host · realtime tick NSE · python SDK github ·
market-depth/bidask levels · pricing plans · NSE authorized-vendor list "TrueData" ·
history depth 1-min/tick years · "tick by tick" vs snapshot per-second ·
request/call limits + connection limits · the "4 levels" blog · FYERS 1599/2599
pricing · "TrueData Financial Information" CM/F&O/CD segment list.
NSE pass (relevant subset of 24): authorized-vendor list queries (S2/S4/S7/S18) ·
TBT/colo/MTBT queries (S1/S5/S15) · tariff "tick by tick" ₹50,00,000 (S11) ·
vendor-feed 1-second cadence (S3/S12/S13) · Upstox/Zerodha calibration (S10/S17/S20).
Full query texts live in the session capture logs (ephemeral scratchpad); every fact
adopted from a snippet is tiered OFFICIAL-SNIPPET-UNCONFIRMED or COMMUNITY in-place.

## GitHub (authenticated MCP; worked)

| Action | Outcome |
|---|---|
| Code search `"push.truedata.in"` | OK — 53 hits; read fragments incl. the OFFICIAL sample `Nahas-N/Truedata-sample-code/truedata/full_feed_sample_code.py` (port 9084, `full_feed=True`), community ports 8082/8084/8086/8094, `replay.truedata.in`, and the prior BruteX/TickVault capture docs |
| get_file_contents on Nahas-N/Truedata-sample-code | DENIED — session token restricted to the BruteX repo; fragments via code search only |

## SDK artifacts captured (pip `--no-deps`, 2026-07-20) — the SDK-VERIFIED basis

| Artifact | Version | SHA256 | Files read |
|---|---|---|---|
| `truedata-7.0.1-py3-none-any.whl` | 7.0.1 | `c3aaee95c97541cb14758053abedee798255eba49ed87175cb26bd07d211397c` | websocket/TD_live.py, TD_ws.py, utils.py, constants.py, compression_map.py, TD_chain.py; history/Historical_REST.py, TD_hist.py; analytics/TD_analytics.py; dist-info/METADATA (the official README) |
| `truedata_ws-5.0.11-py3-none-any.whl` | 5.0.11 | `cb080842541cd4dbb830339f59cc18924c45245a7183a02467065282b7609bea` | websocket/defaults.py, TD.py (partial), dist-info/METADATA (README: L1/L2 wording, live-port note, changelog) |

Re-fetch: `pip download truedata==7.0.1 --no-deps` / `pip download truedata-ws==5.0.11
--no-deps`, then verify the wheel sha256 against the pins above. (Licensing per PyPI
metadata: GPL-3.0 family — OSS; this archive still summarizes, `README.md`.)

## Blocked-host list (this session)

`www.truedata.in` · `feedback.truedata.in` · `truedata.home.blog` ·
`medium.com/@truedatapvtltd` · `www.nseindia.com` · `nsearchives.nseindia.com` ·
`archives.nseindia.com` · `www.niftyindices.com` · `fyers.in` /
`subscriptions.fyers.in` · `www.marketcalls.in`. (PyPI project HTML pages degrade to
a JS shell; the pip channel + JSON API remain the working official-material path.)

## Prior captures this archive builds on (in-repo; cite these, don't re-derive)

| Record | Dated | What it holds |
|---|---|---|
| `docs/broker-api-capture/truedata/` (00–13 + SOURCES.md) | 2026-07-15 | Official-page capture from a working-egress session; SOURCES.md marks truedata.in/market-data-apis, /products/marketdataapi, wstest.truedata.in and ~20 feedback.truedata.in KB articles "OK = fetched & archived". The OFFICIAL-VERIFIED(capture-2026-07-15) provenance base. |
| `docs/truedata/api-reference/` (README + 01–10) | 2026-07-15 | The #1966 research pack; its own `10-retrieval-log.md` is the URL-by-URL log of THAT session (truedata.in was 403-blocked in its session too; SDK/pkg-source is its high tier). |
| Team-memory catalogs (2026-07-16) | 2026-07-16 | Field-level TrueData/GFDL catalogs distilled from the PR #1595 branch doc import (that branch's files are NOT on this tree) — cited as PRIOR-VERIFIED-NOTE where used. |

## Docs the operator should MANUALLY UPLOAD (each closes a named gap)

1. **NSE authorized-vendor list PDF(s)** — nsearchives
   `Authorized_Realtime_Data_Vendors.pdf` (+ `List%20of%20Authoized%20Vendors_2.pdf`,
   `List_of_Authorized_Realtime_Data_Vendors_0.pdf`; archives
   `List_data_Vendors.pdf`) — eyeball the TrueData AND GFDL rows first-hand
   (closes VERDICT evidence row 14 for both vendors).
2. **NSE Real_Time_Tariff_Domestic_01042025_.pdf** — confirm the TBT-tier figures
   (~₹50L/yr CM) and whether any TBT row exists for F&O (closes the row-4/row-5
   tension in `VERDICT.md`).
3. **The official TrueData API PDF v2.2.1** ("TrueData Market Data API
   Documentation" — email-distributed via apisupport@truedata.in; a Scribd mirror
   exists, gated) — the raw JSON key names (probe gate 4's document half).
4. **truedata.in/price + /information/pricing** — the pricing-structure pages (the
   2026-07-15 capture deliberately skipped /price as Velocity-retail pricing).
5. **Extended-History quote from sales@truedata.in** — depth + price (the decisive
   BruteX-lake question; VERDICT probe gate 5).
6. **feedback.truedata.in KB articles**: the request/call-limit article (byte-read
   the 5/300/18k + 10/600/18k caps) and "Is the data delayed?" (the latency half
   never relayed).
7. **truedata.in/blog/levels-real-time-data-nse-bse-mcx** — the vendor's own
   4-levels/TBT-honesty page (currently snippet-tier).
