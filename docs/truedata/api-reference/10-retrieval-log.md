# 10 — Retrieval log (every URL attempted → outcome), 2026-07-15

The honesty backbone of this pack (Golden Rule #1/#10). Environment:
BruteX build sandbox; outbound HTTPS via agent proxy. `403` below = proxy
CONNECT block (ENVIRONMENT), not vendor/site truth unless noted.

## 1. Direct fetch attempts

| URL | Outcome |
|---|---|
| https://www.truedata.in/ | 403-blocked (proxy) |
| https://feedback.truedata.in/knowledge-base/article/getting-started-with-truedata-market-data-api | 403-blocked (proxy) |
| https://wstest.truedata.in/ · https://auth.truedata.in/ · https://history.truedata.in/ | 403-blocked (proxy) — 5 truedata.in subdomains all blocked |
| truedata.in/downloads/symbol_lists/1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt | not attempted separately (host proven blocked) |
| https://pypi.org/project/truedata/ · /truedata-ws/ | FETCHED (direct) |
| pypi.org/pypi/{truedata, truedata-ws}/json | FETCHED; long-descriptions saved verbatim |
| files.pythonhosted.org …truedata-7.0.1.tar.gz | FETCHED; full source extracted |
| registry.npmjs.org/truedata-nodejs (+ 1.2.0 tarball) | FETCHED; registry JSON + verbatim README + full source extracted |
| npmjs.com package page | 403-blocked (proxy) — registry JSON used instead |
| https://www.nuget.org/packages/TrueData-DotNet | FETCHED (direct) |
| github.com/kapilmar/truedata-ws (+ raw README) | **404 Not Found** — GENUINE dead link (the PyPI "Homepage" for both packages) |
| github.com/Nahas-N/Truedata-sample-code (+ 4 of 5 raw sample files) | FETCHED verbatim (`callback_sample_code.py` not attempted — time-boxed) |
| web.archive.org fallback | BLOCKED |
| r.jina.ai relay | 403-blocked (proxy) |
| scribd.com/document/667918775/… (official PDF v2.2.1 mirror) | 403-blocked — PDF UNRETRIEVED |
| t.me/s/truedata_ws_api · medium.com vendor article | 403-blocked (proxy) |
| fyers.in pricing-revision notice · marketcalls.in vendor comparison | 403-blocked (proxy) |

## 2. WebSearch relays (19 queries, all successful — vendor-page content relayed at medium confidence)

The queries enumerated the doc surfaces (KB article list, sandbox,
PDF mirror), the auth flow wording, the depth article (tick 5 days /
bars 6 months / EOD 10+ years / Extended History add-on), the rate-limit
article (5/300/18k · 10/600/18k · 1 login instance · plan symbol limits),
the tick/bar field lists, the bar-formation rule, the replay
announcements, the symbology blog ("NIFTY 50", `-I/-II/-III`,
`NIFTY23OCTFUT`, the conflicting `NIFTY20000C1` platform variant), the
trial/onboarding flow, the pricing-page titles (no numerics), the product
suite, and a negative India-VIX-symbol search. The full query-by-query
log lives in the session research notes; every relayed fact in this pack
cites its article by URL in the file where it is used.

## 3. Honest-limits summary (carried as UNVERIFIED throughout this pack)

- Nothing on a `*.truedata.in` host was fetched directly; every
  vendor-PAGE fact is search-relayed (medium). The load-bearing
  protocol/endpoint facts come from vendor-PUBLISHED SDK source
  (pkg-source, high).
- The official API PDF (v2.2.1) and the sandbox client were not
  retrievable — history-REST response samples are reconstructed from SDK
  parsing code, not captured wire bytes.
- UNVERIFIED list: numeric prices + plan names; Extended-History add-on
  depth/price; per-plan symbol counts; the port→plan map beyond
  8082/8084/9084/9092; INDIA VIX symbol; expired-contract history;
  corporate-action adjustment policy; exact latency SLA; daily caps;
  market-status value vocabulary; replay schedule; refresh-token fields;
  a status page; a Postman collection.
- Everything above needs a credentialed live probe (the trial + sandbox
  flow in [02](./02-authentication.md) §5 is the cheap path) before being
  encoded as fact in BruteX/TickVault code.
