# 09 — SDKs, vendor packages, DLLs & code samples

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **Code Samples**,
**DotNet API**, **COM API** (downloads), plus the vendor's PyPI packages.

## 1. Vendor-authored PyPI packages (pypi-fetch = direct, HIGH confidence)

These three packages, authored by
"Global Datafeeds \<developer@globaldatafeeds.in\>", are the strongest
evidence in this pack — the wire protocol was read from their source.

| Package | Import | Latest | Releases | Deps | Notes |
|---|---|---|---|---|---|
| **GFDLWS** ("Package to implement WebSocket API of Global Datafeeds") | `import GFDLWS as gw` | 0.0.4 (2024-09-16) | 0.0.1–0.0.3 (2024-09-13), 0.0.4 (2024-09-16) | `websockets` | Classifier "Development Status :: 3 - Alpha", Windows classifier. Modules cover all 29+5 functions. **Code quality is rough** — sync-over-async wrappers, bare excepts, a `getcaldle` typo'd method name, an inverted `Max`-handling branch in that method. Treat as reference for the WIRE PROTOCOL, not a production-grade client. |
| **ws-gfdl** (predecessor, same description) | `import gfdlws as gw` | 1.1.0 (2022-08-12) | 1.0.0–1.1.0 (2022-08-11/12) | `websockets` | Same vendor author; README identical in substance (29 functions). Third-party integrations import `gfdlws`. |
| **gfdl-rest** ("Beta Package to implement REST API of Global Datafeeds") | `import gfdl_rest as rs` | 1.0.3 (2024-11-11) | 1.0.0/1.0.1 (2024-08-23), 1.0.2 (2024-08-27), 1.0.3 (2024-11-11) | `requests` | Class-per-function over `BaseAPI` (accessKey injection + `xml=true` / `format=CSV`). **BUG observed:** `GetHistoryGreeks.py` hits endpoint `"GetHistory/"` (not `GetHistoryGreeks/`) — verify against the vendor REST docs before use. |

Name-collision warnings (pypi-fetch, high): PyPI `gfdl` (0.2.0, 2026) is
"Gradient Free Deep Learning" from LANL — UNRELATED. Probed-and-absent
names: `gfdlrest`, `globaldatafeeds`, `nimbleapi`, `gfdl-ws`, `gfdlws2`
(404 on PyPI, 2026-07-15).

## 2. Vendor code-sample & download pages (search-relay, medium)

- Code Samples archive `…/documentation-support/api-code-samples/` with
  per-language pages: WebSockets — **Python / Java / JavaScript / NodeJS /
  Postman**; REST — **Python**.
- "Code Samples, API DLLs, Documentation" download hub
  `…/api-code-samples/download-code-samples-2/` — includes **C# (DotNet)
  source samples** and **API DLLs**.
- DotNet API docs + DLL download (x86 & x64): `…/dotnet-api-documentation/download-2/`.
- COM API: `…/com-api-documentation/how-to-connect-using-com-api/`
  (same endpoint + port + API-key model).
- Vendor PDF sample: `globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.pdf`
  (July-2024) — the cleaner sample set vs the package code.
- Languages claimed: "C, C++, Python, Java, PHP, C#, Visual Basic,
  JavaScript, Node.js and much more" (homepage/apis relay, medium —
  WS/REST are language-neutral).

## 3. Legacy names — verified status

- The current docs never use the name "NimbleAPI"; the API hosts live
  under `lisuns.com` (`nimblewebstream`, `nimblerest`, `test.lisuns.com`)
  and the DESKTOP product family uses "Nimble" branding (NimbleDataPro
  etc. — see [10](./10-company-support-products.md)).
- A "Touchline" API product name: **UNVERIFIED** — no retrieved vendor
  page uses it. Do not cite.
- Vendor GitHub org: none found (repo search 2026-07-15).

## 4. Third-party integration references (github-fetch, low — working examples only)

| Repo | What it shows |
|---|---|
| AnkushSinghGandhi/DeltaStream-OptionAnalysis2 | Python feed-generator over `gfdlws`; endpoint `ws://nimblewebstream.lisuns.com:4575`; env-config pattern |
| developerakkoo/Stonks-API | Node `ws` client, same endpoint, raw MessageType JSON |
| kishanYuvmedia/apitrading | REST datasource config `http://nimblerest.lisuns.com:4531/{Function}/?accessKey=…` |
| karthiciist/Buy + PopulateTickingTables | Python `HTTPSConnection("nimblerest.lisuns.com", 4532)` — the TLS REST port |
| smol-ninja/psylab | `BASE_URL = 'http://nimblerest.lisuns.com:4531/'` |
| sisoppin/gdta | **DO NOT TRUST for endpoints** — README contains a fabricated placeholder `wss://your-endpoint.globaldatafeeds.com/websocket` (domain doesn't match vendor infrastructure) |

## 5. Adapter guidance (BruteX/TickVault)

If an adapter is ever built, write it against the WIRE PROTOCOL
([02](./02-connect-auth.md)–[06](./06-greeks-optionchain.md)) rather than
wrapping the vendor packages: the vendor Python clients are alpha-grade
(bugs documented above), the protocol is trivial JSON, and BruteX's house
rules (structlog, typed errors, injected rate control — CLAUDE.md §4)
can't be met through them. The packages remain the authoritative
REFERENCE for message shapes.
