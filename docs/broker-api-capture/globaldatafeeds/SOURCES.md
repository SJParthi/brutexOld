# SOURCES — every URL captured (2026-07-15)

Status legend: **ok** = fully captured verbatim · **partial** = captured in part (reason noted) · **failed** = fetch returned nothing usable · **gated** = not publicly documented.

Knowledgebase pages were bulk-fetched through the site's own WordPress REST API (`https://globaldatafeeds.in/wp-json/wp/v2/knowledgebase?slug=…`), which returns each page's canonical URL, last-modified date, and full HTML content; content is identical to the canonical pages listed below.

## Discovery / structure

| URL | Status | Archived in |
|---|---|---|
| https://globaldatafeeds.in/ | ok | 01 |
| https://globaldatafeeds.in/sitemap.xml (→ sitemap_index.xml) | ok | — (discovery) |
| https://globaldatafeeds.in/knowledgebase-sitemap.xml | ok | — (discovery, 105 doc URLs) |
| https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/ | ok | 01 (structure) |
| https://globaldatafeeds.in/apis/ | ok | 01 |
| https://globaldatafeeds.in/wp-json/wp/v2/types | ok | — (discovery) |

## Introduction (file 02)

Base: `https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/`

| Page | Status |
|---|---|
| introduction/introduction-to-apis/ | ok |
| introduction/type-of-data-available/ | ok |
| introduction/type-of-apis-available/ | ok |
| introduction/supported-os-languages/ | ok |
| introduction/supported-exchanges/ | ok |
| introduction/symbol-availability/ | ok |
| introduction/symbol-list/ | ok |

## Documentation & support references (file 03)

| Page | Status |
|---|---|
| documentation-support/api-fields-description/ | ok |
| documentation-support/symbol-naming-conventions/ | ok (in-cell line breaks normalized to spaces, noted in-file) |
| documentation-support/diagnostic-api-responses/ | ok |

## WebSockets API core (files 04–09)

| Page | Status |
|---|---|
| websockets-api-documentation/how-to-connect-using-websockets-api/ | ok |
| websockets-api-documentation/authenticate/ | ok |
| websockets-api-documentation/function-subscriberealtime/ | ok |
| websockets-api-documentation/function-subscribesnapshot/ | ok |
| websockets-api-documentation/function-getlastquote-2/ | ok |
| websockets-api-documentation/getlastquoteshort/ | ok |
| websockets-api-documentation/getlastquoteshortwithclose/ | ok |
| websockets-api-documentation/function-getlastquotearray-2/ | ok |
| websockets-api-documentation/getlastquotearrayshort/ | ok |
| websockets-api-documentation/getlastquotearrayshortwithclose/ | ok |
| websockets-api-documentation/function-getsnapshot-2/ | ok |
| websockets-api-documentation/function-gethistory-2/ | ok |
| websockets-api-documentation/gethistoryaftermarket-3/ | ok |
| websockets-api-documentation/getexchangesnapshot-2/ | ok |
| websockets-api-documentation/function-getexchanges-2/ | ok |
| websockets-api-documentation/function-getinstrumentsonsearch-2/ | ok |
| websockets-api-documentation/getinstruments/ | ok |
| websockets-api-documentation/getinstrumenttypes-2/ | ok |
| websockets-api-documentation/getproducts-2/ | ok |
| websockets-api-documentation/getexpirydates-2/ | ok |
| websockets-api-documentation/getoptiontypes-2/ | ok |
| websockets-api-documentation/getstrikeprices-2/ | ok |
| websockets-api-documentation/getholidays/ | ok |
| websockets-api-documentation/function-getserverinfo-2/ | ok |
| websockets-api-documentation/function-getlimitation-2/ | ok |
| websockets-api-documentation/function-getmarketmessages-2/ | ok |
| websockets-api-documentation/function-getexchangemessages-2/ | ok |

## REST API core (files 10–14)

| Page | Status |
|---|---|
| rest-api-documentation/how-to-connect-using-rest-api/ | ok |
| rest-api-documentation/handling-special-characters/ | ok |
| rest-api-documentation/clientaccesspolicy-xml-for-silverlight-applications/ | ok |
| rest-api-documentation/crossdomain-policy-for-adobe-flash-applications/ | ok |
| rest-api-documentation/function-getlastquote/ | ok |
| rest-api-documentation/function-getlastquoteshort/ | ok |
| rest-api-documentation/function-getlastquoteshortwithclose/ | ok |
| rest-api-documentation/function-getlastquotearray/ | ok |
| rest-api-documentation/function-getlastquotearrayshort/ | ok |
| rest-api-documentation/function-getlastquotearrayshortwithclose/ | ok |
| rest-api-documentation/function-getsnapshot/ | ok |
| rest-api-documentation/function-gethistory/ | ok |
| rest-api-documentation/gethistoryaftermarket-2/ | ok |
| rest-api-documentation/getexchangesnapshot/ | ok |
| rest-api-documentation/function-getexchanges/ | ok |
| rest-api-documentation/function-getinstrumentsonsearch/ | ok |
| rest-api-documentation/function-getinstruments/ | ok |
| rest-api-documentation/getinstrumenttypes/ | ok |
| rest-api-documentation/getproducts/ | ok |
| rest-api-documentation/getexpirydates/ | ok |
| rest-api-documentation/getoptiontypes/ | ok |
| rest-api-documentation/getstrikeprices/ | ok |
| rest-api-documentation/getholidays-2/ | ok |
| rest-api-documentation/function-getserverinfo/ | ok |
| rest-api-documentation/function-getlimitation/ | ok |
| rest-api-documentation/function-getmarketmessages/ | ok |
| rest-api-documentation/function-getexchangemessages/ | ok |

## Streaming / OptionChain / Greeks / Delayed / Gainers / VolumeShockers (files 15–20)

| Page | Status |
|---|---|
| streaming_api/stream-all-symbols/ | ok |
| streaming_api/streamallsnapshots/ | ok |
| optionchain-api/subscribeoptionchain/ | ok |
| optionchain-api/function-getlastquoteoptionchain/ | ok |
| optionchain-api-rest-api-documentation/getlastquoteoptionchain/ | ok |
| greeks-api/subscriberealtimegreeks/ | ok |
| greeks-api/subscribesnapshotgreeks/ | ok |
| greeks-api/getsnapshotgreeks-2/ | ok |
| greeks-api/subscribeoptiongreekschain/ | ok |
| greeks-api/getlastquoteoptiongreeks/ | ok |
| greeks-api/getlastquotearrayoptiongreeks/ | ok |
| greeks-api/getlastquoteoptiongreekschain/ | ok |
| greeks-api/gethistorygreeks-2/ | ok |
| greeks-api-global-datafeeds-apis/gethistorygreeks-returns-historical-greeks-data/ | ok |
| greeks-api-global-datafeeds-apis/getsnapshotgreeks/ | ok |
| greeks-api-global-datafeeds-apis/getlastquoteoptiongreeks-2/ | ok |
| greeks-api-global-datafeeds-apis/getlastquotearrayoptiongreeks-2/ | ok |
| greeks-api-global-datafeeds-apis/getlastquoteoptiongreekschain-2/ | ok |
| delayed-api/subscribesnapshot-delayed/ | ok |
| delayed-api/getsnapshot-delayed/ | ok |
| delayed-api/gethistory-delayed/ | ok |
| delayed-api/getexchangesnapshot-delayed/ | ok |
| delayed-api-rest-api-documentation/gethistory-delayed-returns-historical-data-tick-minute-eod/ | ok |
| delayed-api-rest-api-documentation/getsnapshot-delayed-2/ | ok |
| delayed-api-rest-api-documentation/getexchangesnapshot-delayed-2/ | ok |
| gainers-losers/subscribetopgainerslosers/ | ok |
| gainers-losers/gettopgainerslosers/ | ok |
| gainers-losers-rest-api-documentation/gettopgainerslosers-2/ | ok |
| volume-shockers/getvolumeshockers-2/ | ok |
| volume-shockers-rest-api-documentation/getvolumeshockers/ | ok |

## DotNet / COM / Code samples (files 21–24)

| Page | Status |
|---|---|
| dotnet-api-documentation/how-to-connect-using-dotnet-api/ | ok |
| dotnet-api-documentation/download-2/ | ok |
| com-api-documentation/how-to-connect-using-com-api/ | ok |
| com-api-documentation/download/ | ok |
| api-code-samples/download-code-samples-2/ | ok |
| api-code-samples/websockets-javascript-sample/ | ok |
| api-code-samples/websockets-python/ | ok |
| api-code-samples/websockets-java/ | ok |
| api-code-samples/websockets-nodejs/ | ok |
| api-code-samples/websockets-postman/ | ok |
| api-code-samples/rest-python/ | ok (one code block garbled on the source site itself; preserved as published, noted in-file) |

## Pricing / FAQ / Contact (files 25–27)

| Page | Status |
|---|---|
| pricing-sales/api-pricing/ | ok |
| pricing-sales/who-can-purchase/ | ok |
| faqs/faqs/ | ok |
| contact/contact-sales/ | ok |
| contact/contact-technical-support/ | ok |

## Fundamental Data API — docs.globaldatafeeds.in (file 28)

| URL | Status |
|---|---|
| https://docs.globaldatafeeds.in/ | ok (index + full page/nav inventory) |
| https://docs.globaldatafeeds.in/llms-full.txt | failed (empty body) |
| https://docs.globaldatafeeds.in/authentication-request-response-923682m0 | ok |
| https://docs.globaldatafeeds.in/list-of-apis-923685m0 | ok (30-row API table) |
| https://docs.globaldatafeeds.in/diagnostic-api-responses-925705m0 | ok |
| https://docs.globaldatafeeds.in/how-to-connect-using-websocket-api-2181778m0 | ok |
| https://docs.globaldatafeeds.in/getcorporateannouncements-15575575e0 | ok (representative endpoint) |
| ~60 further endpoint pages + 61 schema pages on docs.globaldatafeeds.in | **partial** — not fetched (budget); complete URL index preserved in file 28; each page is also retrievable as `<slug>.md` |
| https://globaldatafeeds.in/fundamental-data-apis/ | ok |

## Stock Market News API — newsdocs.globaldatafeeds.in (file 29)

| URL | Status |
|---|---|
| https://newsdocs.globaldatafeeds.in/ | ok (index + nav inventory) |
| https://newsdocs.globaldatafeeds.in/llms-full.txt | failed (empty body) |
| https://newsdocs.globaldatafeeds.in/authentication-request-response-1759050m0 | ok |
| https://newsdocs.globaldatafeeds.in/single-company-news-31553454e0 | ok |
| https://newsdocs.globaldatafeeds.in/webhook-2068702m0 | ok |
| https://newsdocs.globaldatafeeds.in/diagnostic-api-responses-1759056m0 | ok |
| https://globaldatafeeds.in/stock-market-news-apis/ | ok |

## Other product/company pages (files 30–32, 01)

| URL | Status |
|---|---|
| https://globaldatafeeds.in/market-data-widgets/ | ok (no inline embed code on page; widgets served via webwidgets.globaldatafeeds.in) |
| https://globaldatafeeds.in/release-notes/ | ok (single page, no pagination) |
| https://pypi.org/project/ws-gfdl/ | ok (full README, v1.1.0) |

## Not publicly documented (gated)

| Item | Status |
|---|---|
| FIX API | **gated** — advertised on https://globaldatafeeds.in/apis/ with no public reference docs; contact sales |
| Trial signup / account dashboard mechanics | gated behind login (https://globaldatafeeds.in/my-account/) |
| webwidgets.globaldatafeeds.in widget embed internals | gated — provisioned per customer |
