# GlobalDataFeeds — Stock Market News API

Scope: Archive of the Stock Market News API documentation site (newsdocs.globaldatafeeds.in, an Apidog-published doc) — introduction, authentication, Single Company News endpoint reference, webhook support, full diagnostic/error responses — plus the site page index and the marketing product page.

Source URLs:
- https://newsdocs.globaldatafeeds.in/ (index / Introduction)
- https://newsdocs.globaldatafeeds.in/authentication-request-response-1759050m0
- https://newsdocs.globaldatafeeds.in/single-company-news-31553454e0
- https://newsdocs.globaldatafeeds.in/webhook-2068702m0
- https://newsdocs.globaldatafeeds.in/diagnostic-api-responses-1759056m0
- https://globaldatafeeds.in/stock-market-news-apis/ (product page)

Capture notes:
- The docs site exposes an LLMs.txt index at https://newsdocs.globaldatafeeds.in/llms.txt (llms-full.txt exists but returned empty at capture time).
- Not individually captured (fetch budget) — URLs preserved in "Site structure" below: API Trial, List of APIs, Multi-Company News, General News, Glossary.

---

## News API - Introduction

> Source: https://newsdocs.globaldatafeeds.in/ (canonical: https://newsdocs.globaldatafeeds.in/news-api-introduction-2069844m0) — captured 2026-07-15

### Setting the context

Established in 2010, Global Financial Datafeeds LLP (aka Global Datafeeds or GFDL) began its operations by disseminating realtime price data of NSE for AmiBroker. Instead of building its own analysis platform, GFDL decided to make data of Indian Stock Exchanges available into world's renowned charting and stock analysis platforms.

Fast forward to 2025, today GFDL is a premier vendor of realtime data of all Indian Stock Exchanges (NSE, NFO, CDS, BSE, BFO, MCX, NCDEX) with out-of-the-box integration with **30+ major international technical analysis & charting platforms**. A robust set of matured APIs have been helping companies / traders to build their custom analysis models and platforms with ease.

### Addition of NEWS API

Though realtime market data plays a crucial role for strategy / algo / swing traders, scalpers, intraday traders, and prop desks, it does not fully cover the complete decision-making requirements of all market participants. A significant number of traders and investors rely on timely and relevant news updates to understand market movements, sentiment, and external factors affecting securities.

This data commonly referred to as **News Data** includes company-specific news, sector insights, macroeconomic developments, analyst opinions, corporate actions, and more. **Through the News API, we aim to provide structured, real-time, and historical news data to help market participants make informed trading and investment decisions.**

### News API - what is available

Real-time and historical news data related to companies listed on Indian Stock Exchanges will be available through APIs. It will include **Company-specific News, Sector & Macro Insights, Analyst & Credit Ratings, Financial Performance Updates, Strategic Actions, Corporate Actions, Index & Reshuffles, Market News, IPO Updates, Commodities, Mutual Funds, Cryptocurrency** and much more.

To see entire list of offerings: https://newsdocs.globaldatafeeds.in/list-of-apis-1759052m0

### News API - Who this is for

Our services are useful for anyone who relies on timely and relevant news data to make informed trading and investment decisions. This includes and is not limited to traders, investors, research analysts, media platforms, fintech applications, service providers, and platform vendors and many more.

### Why us

You are in good and strong company. Since 2010, we have been disseminating realtime/historical price data of companies listed on Indian Stock Exchanges with **99.995% uptime**. Today we handle **70 million data requests daily (7+ crore)** from variety of users across multiple platforms & via variety of APIs.

Robust, proven expertise in disseminating low-latency data via multiple redundant channels across **web, mobile & desktop**.

We source our data directly from the Stock Exchanges. So you can be sure about the authenticity, completeness and timeliness of the data.

Simple yet powerful APIs

**Less Time-to-Market**: we provide user-friendly APIs with extensive code samples in **15+ programming languages**. So you can customize your requirement, create & test the code on-the-fly immediately. Paste this code into your project and you are good to go live within few hours.

**No more trials, ifs-and-buts**: generate the code, test it, analyse the response - all in our publicly available platform. Talk to our support team over phone / whatsapp / discussion forum / email to resolve your queries. Only when you are satisfied, get in touch with our Sales Team for product payment & subscription.

*Modified at 2026-04-09 11:51:38*

---

## Authentication, Request & Response

> Source: https://newsdocs.globaldatafeeds.in/authentication-request-response-1759050m0 — captured 2026-07-15

### Request Format

RESTful APIs (HTTP GET) are used for sending the request where inputs are sent as part of URL in the form of query parameters.

### Response Format

Response is received in **Json** format.

Sometimes, API can return diagnostic response in plain/text format instead of actual data. When this happens, user will need to send the request again - till they receive actual data. A list of available diagnostic responses can be found at: https://newsdocs.globaldatafeeds.in/diagnostic-api-responses-1759056m0

### Authentication

To receive the response, user will need to send valid "AccessKey" parameter with each request. User can get the accessKey for their use by contacting our Sales Team (https://globaldatafeeds.in/contact-us/#contact-sales-api) and making the payment for the data subscription.

*Modified at 2026-04-10 04:26:46*

---

## Single Company News (endpoint reference)

> Source: https://newsdocs.globaldatafeeds.in/single-company-news-31553454e0 — captured 2026-07-15

```
GET /api/news
```

**Single Company News API** is used to fetch news related to a specific company using identifiers such as symbol, company name, ISIN, or scrip code. It provides detailed information including title, summary, sentiment, article type, and source. The API also supports filters like sentiment, article type, and date range, allowing users to retrieve relevant and targeted news efficiently.

### What is returned?

Title, link, date, summary, company, article type, sentiment, specific title, long Summary, source, created at, article id, symbol, ISIN, scrip code

For details, please see glossary: https://newsdocs.globaldatafeeds.in/glossary-1759055m0

### Sample Response (JSON)

```json
{
"title": "Realty index climbs for 5th consecutive session, becomes top Nifty gainer; Prestige, DLF rise up to 9%- Moneycontrol.com",
"link": "https://www.moneycontrol.com/news/business/markets/realty-index-climbs-for-5th-consecutive-session-becomes-top-nifty-gainer-prestige-dlf-rise-up-to-9-13883305.html",
"date": "2026-04-08T07:57:43+00:00",
"summary": "Prestige Estates Projects saw a rise of approximately 9% in its stock price, following a report of a 10% increase in pre-sales to ₹7,697 crore for the fourth quarter of the previous fiscal year, and a total pre-sales of ₹30,024 crore for 2025-26, marking a 76% increase year-over-year.",
"company": "Prestige Estates Projects Limited",
"article_type": "Index & Reshuffles",
"sentiment": "positive",
"specific_title": "Prestige Estates Projects Reports Significant Pre-Sales Growth",
"long_summary": "The Realty index has achieved a significant milestone by rising for the fifth consecutive session, making it the top gainer on the Nifty with a 7% increase on the day and over 14% in the last five sessions. Key contributors to this rally include Prestige Estates Projects, which saw its stock surge approximately 9% following a 10% rise in pre-sales to ₹7,697 crore for Q4 FY2025-26, and DLF, which also experienced gains of up to 9%. The upward trend in the real estate sector has been bolstered by the Reserve Bank of India's decision to maintain the benchmark repo rate at 5.25%, providing stability that supports housing affordability and long-term planning. Other notable stocks in the sector, such as Godrej Properties and Oberoi Realty, also advanced between 5% and 7%. However, despite the current rally, there are indications of potential weakness in residential real estate sales, particularly in the National Capital Region and Pune, as noted by Knight Frank India. The market is experiencing a cyclical nature, and a correction may be expected following an extended upcycle.",
"source": "Money Control",
"created_at": "2026-04-08T08:00:42.751909+00:00",
"sector": null,
"article_id": 384632,
"symbol": "PRESTIGE",
"ISIN": "INE811K01011",
"scrip_code": 533274
},
```

### Request

Query params: `api_key` plus exactly one company identifier (`symbol`, `isin`, `company`, or `scrip_code`); optional filters include sentiment, article type, and date range (`from`/`to`), and pagination (`limit`/`offset`) — see the Diagnostic API Responses section for the parameter validation rules that confirm these.

Request example (cURL; the docs also generate samples for Shell, JavaScript, Java, Swift, Go, PHP, Python, HTTP, C, C#, Objective-C, Ruby, OCaml, Dart, R, cURL-Windows, Httpie, wget, PowerShell):

```bash
curl --location '/api/news?api_key=30bd31ff-fb7e-4d6c-a76e-06750e3eeb09&symbol=TCS'
```

(Note: the docs show a relative path `/api/news`; the host is provided with your subscription. The api_key above is the public "Try It" demo key shown in the docs.)

### Responses

**200** — application/json

Example:

```json
{}
```

*Modified at 2026-04-11 08:02:41*

Related endpoints (same site, not individually captured):
- **Multi-Company News** — https://newsdocs.globaldatafeeds.in/multi-company-news-31553453e0 — fetch news for multiple companies in a single request using `symbols`, `isins`, or `scrip_codes` (only one identifier type per request; maximum 20 identifiers; company-name search not supported — per the diagnostic rules below).
- **General News** — https://newsdocs.globaldatafeeds.in/general-news-31553455e0 — broad market news (economy, IPOs, commodities, mutual funds, indices) with its own set of valid `article_type` values.

---

## Webhook Support

> Source: https://newsdocs.globaldatafeeds.in/webhook-2068702m0 — captured 2026-07-15

Webhooks allow you to receive real-time notifications when new financial news articles are published. Instead of repeatedly polling the API for updates, your application can register a webhook endpoint to receive automatic notifications.

### Step 1: News API Server

The News API server acts as the source of financial news data. Whenever a new article is published or updated, it automatically triggers a webhook event.

**Includes:**
- Title
- Summary
- Sentiment
- Other metadata

### Step 2: Client Webhook URL (Receiver)

The client must provide a secure (**HTTPS**) webhook endpoint to receive real-time updates.

The server sends an **HTTP POST** request whenever a new article is published.

**Requirements:**
- Accept JSON payload
- Respond within a few seconds
- Return HTTP 200 status

```
POST /webhook/news
Content-Type: application/json
```

### Step 3: Client Application

Once the webhook is received, the client application processes the data.

- Store in database
- Display on dashboard
- Trigger alerts
- Integrate with other systems

This flow ensures secure and real-time delivery of financial news updates from the News API server to the client application.

Complete webhook documentation download: https://globaldatafeeds.in/resources/Sample_file/NewsAPI/Webhook%20support%20.zip

*Modified at 2026-04-10 04:51:32*

---

## Diagnostic API Responses (error codes)

> Source: https://newsdocs.globaldatafeeds.in/diagnostic-api-responses-1759056m0 — captured 2026-07-15

At times, server sends diagnostic responses instead of requested data. Please find below list of possible informational messages (returns as text/plain) by our server. Most of these responses are self-explanatory. **Your code should be designed to handle these responses which are returned as text/plain.**

### 400 – Bad Request

These errors occur when the request contains invalid parameters or parameter combinations.

| Error Message | Description |
| --- | --- |
| Invalid API key | Returned when the provided API key is invalid. |
| Use exactly one of: symbol, isin, company, scrip_code | Please provide only one company identifier in the request. |
| Use only one of: symbols, isins, scrip_codes | Please provide only one type of identifier in a multi-company request. |
| Please provide either 'symbols', 'scrip_codes', or 'isins' parameter | At least one identifier must be provided in a multi-company request. |
| Company name search is not supported in multi-company requests | Please use the single company endpoint for company name search. |
| No identifiers provided | Please provide valid symbols, ISINs, or scrip codes. |
| Too many identifiers provided. Maximum allowed: 20 | Please limit the number of identifiers to 20 per request. |
| 'from' date must be before 'to' date | Please ensure the 'from' date is earlier than the 'to' date. |
| Invalid date format | Please provide dates in ISO 8601 format (e.g., 2025-01-01T00:00:00+05:30). |
| Invalid date range | Please request 'from' and 'to' parameter values within the allowed period. |
| limit/offset must be non-negative integers | Please provide valid non-negative numeric values for pagination. |
| Invalid sentiment. Allowed: positive, negative, neutral | Please provide a valid sentiment value. |
| Invalid article_type for company-specific news | Please use valid article types applicable for company-specific news. |
| Invalid article_type for general news | Please use valid article types applicable for general news. |

### 404 – Not Found

These errors occur when the requested resource does not exist.

| Error Message | Description |
| --- | --- |
| Company with symbol '{symbol}' not found | No company found for the provided symbol. |
| Company with ISIN '{isin}' not found | No company found for the provided ISIN. |
| No companies found with name matching '{company}' | No matching company found for the provided name. |
| No valid companies found for the provided symbols | None of the provided symbols are valid. |
| No valid companies found for the provided ISINs | None of the provided ISINs are valid. |
| Invalid symbols: {symbol1, symbol2} | Some of the provided symbols are invalid. |

### 500 – Internal Server Error

These errors indicate server-side issues.

| Error Message | Description |
| --- | --- |
| Internal server error | An unexpected error occurred. Please try again later or contact support if the issue persists. |

### Webhook Error Responses

#### 400 – Bad Request

```json
{
"error": "Invalid webhook URL. Must be a valid HTTPS URL"
}
```

Please provide a valid HTTPS webhook URL.

#### 409 – Conflict

```json
{
"error": "Invalid webhook URL. Must be a valid HTTPS URL"
}
```

The webhook URL is invalid or already exists. Please verify and try again.

*Modified at 2026-04-07 12:01:25*

---

## Site structure — full page index of newsdocs.globaldatafeeds.in

> Source: sidebar navigation of https://newsdocs.globaldatafeeds.in/ — captured 2026-07-15
> Pages marked [captured above] are reproduced in this file; others were not individually captured — URLs preserved. Each page is also available as markdown per the site's LLMs.txt (https://newsdocs.globaldatafeeds.in/llms.txt).

- News API - Introduction — /news-api-introduction-2069844m0 [captured above]
- Authentication, Request & Response — /authentication-request-response-1759050m0 [captured above]
- API Trial — /-api-trial-1759051m0
- List of APIs — /list-of-apis-1759052m0
- Single company News: Single Company News — /single-company-news-31553454e0 [captured above]
- Multi-company news: Multi-Company News — /multi-company-news-31553453e0
- General News: General News — /general-news-31553455e0
- Webhook Support: Webhook — /webhook-2068702m0 [captured above]
- Glossary — /glossary-1759055m0
- Diagnostic API Responses — /diagnostic-api-responses-1759056m0 [captured above]

---

## Stock Market News APIs — product page (marketing)

> Source: https://globaldatafeeds.in/stock-market-news-apis/ — captured 2026-07-15 (navigation/menus/footer omitted)

### Stock Market News API India — Structured News from Trusted Financial Sources

Curated company, sector, and market news with sentiment, summary and realtime webhook delivery for Indian markets.

"Data That Drives Decisions"

### What We Deliver

Live market news that moves smarter investments.

- **Company News** — Provides real-time and historical news for individual companies, including earnings, corporate actions, analyst updates, and regulatory developments impacting stock performance.
- **Multi Company News** — Fetch news for multiple companies in a single request, enabling portfolio monitoring, watchlist tracking, and comparative stock analysis.
- **General Market News** — Access broad market coverage including economy updates, IPOs, commodities, mutual funds, indices, and overall financial market trends.
- **Sector & Industry News** — Track sector performance, policy changes, and industry-wide developments influencing groups of companies and thematic investment strategies.
- **Forex & Currency News** — Stay updated on global currency movements and major cryptocurrency developments impacting international markets.
- **Commodities & Bullion News** — Stay informed on commodities and bullion markets including gold, silver, crude oil, base metals, and agricultural commodities, along with global supply-demand trends influencing prices.
- **IPO & Corporate Results News** — Access timely updates on IPO announcements, listings, subscription status, and corporate earnings results. Track financial performance, quarterly results, guidance, and major corporate disclosures impacting investor decisions.
- **Real-Time News via Webhooks** — Receive instant notifications when new articles are published, enabling real-time alerts, automation, and faster decision-making without repeated API polling.
- **Market Session Reports** — Get structured pre-market, mid-session, and post-market summary highlighting key developments, market direction, and trading session insights.

"Ready to get started? Grab your API key and access real-time market news today" — Get API Key (inquiry form: https://forms.zohopublic.in/globaldatafeeds1/form/CuriousaboutNewsAPI/formperma/a0aDKWYs20oQJOt4MOZ2xZVdeQRC3ckmj-5cI9BU3Bk)

Documentation: https://newsdocs.globaldatafeeds.in/

The page also lists corporate clients (large set of Indian Mutual Funds/AMCs and fintechs) and the standard company footer (Global Financial Datafeeds LLP, 301, 4th Floor, Krishna Enclave, Jehan Circle, Gangapur Road, Nashik – 422005, Maharashtra, India).
