# 09 — SDKs, vendor packages, DLLs & code samples

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **Code Samples**,
**DotNet API**, **COM API** (downloads), plus the vendor's PyPI packages.

## 1. Vendor-authored PyPI packages (pypi-fetch = direct, HIGH confidence)

The vendor's PyPI family is **SEVEN packages** (author accounts
"Global Datafeeds \<developer@globaldatafeeds.in\>" + a GDFL employee).
The AUTHORITATIVE census — full-simple-index discovery, per-package
defects, pinned sdist/wheel SHA256 digests for bit-exact re-fetch — is the
sibling archive
**[docs/vendors/gdfl/sdk/PACKAGES.md](../../vendors/gdfl/sdk/PACKAGES.md)**
(DEC-GDFL-VENDOR-DOCS-001, captured 2026-07-13). Summary, plus what THIS
pack's protocol claims were read from:

| Package | Latest | Import | Status |
|---|---|---|---|
| **wsgfdl-py** | **1.3.5** (2025-11-28) | `gfdlws` | **CURRENT WS** — the newest release in the family; adds volume-shockers, snapshot-Greeks, split-adjusted history. Its span-form history call `getbyperiod` is **BROKEN as shipped** (9 args into a 10-parameter zero-default coroutine — AST-verified, sibling census) |
| **gfdl-rest** | **1.0.3** (2024-11-11) | `gfdl_rest` | **CURRENT REST** — class-per-function over `BaseAPI` (accessKey injection + `xml=true` / `format=CSV`); deps `requests`. **BUG observed here:** `GetHistoryGreeks.py` hits endpoint `"GetHistory/"` (not `GetHistoryGreeks/`) — verify against the vendor REST docs before use |
| GFDLWS | 0.0.4 (2024-09-16) | `GFDLWS` | superseded intermediate duplicate — THE package this pack's WS wire-protocol read used (full source; its README carries the function reference). Alpha classifier; **rough code** — sync-over-async wrappers, bare excepts, a `getcaldle` typo'd method name, an inverted `Max`-handling branch in that method |
| ws-gfdl | 1.1.0 (2022-08-12) | `gfdlws` — **collides with wsgfdl-py; never co-install** | original 2022 lineage; README identical in substance (29 functions); third-party integrations import `gfdlws` |
| restgfdl | 1.1.5 (2024-08-23) | `restgfdl` | employee-account predecessor of gfdl-rest (fewer functions); superseded |
| Gfdlws-Sub | 0.1 (2024-10-16) | `Gfdlws_Sub` | subscribe-only helper; the only genuinely async/callback client in the family |
| GlobalDatafeeds-ws | 0.0.1 (2024-10-14) | — | **EMPTY placeholder** — no code in sdist OR wheel (sibling census, verified) |

This pack's wire-shape evidence was read from **GFDLWS 0.0.4 + gfdl-rest
1.0.3** full source (my 2026-07-15 capture); the sibling census
additionally read **wsgfdl-py 1.3.5**, which WIDENS the WS vocabulary to
38 request / 36 result MessageTypes (e.g. `GetVolumeShockers`,
`SubscribeSnapshotGreeks`, an `AdjustSplits` history flag) — inventory in
[docs/vendors/gdfl/api/WEBSOCKET_API.md](../../vendors/gdfl/api/WEBSOCKET_API.md).
Treat ALL of these packages as reference material for the WIRE PROTOCOL,
never production-grade clients (defect inventory: sibling census).

Name-collision warnings (pypi-fetch, high): PyPI `gfdl` (0.2.0, 2026) is
"Gradient Free Deep Learning" from LANL — UNRELATED (as are `gfdlfremake`,
`gfdlnb`, `gfdlvitals`, `gdflib` — NOAA-GFDL/other, per the sibling
census). Probed-and-absent names: `gfdlrest`, `globaldatafeeds`,
`nimbleapi`, `gfdl-ws`, `gfdlws2` (404 on PyPI, 2026-07-15); `restgfdl-py`
is a name-only registration with zero releases (sibling census).

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
