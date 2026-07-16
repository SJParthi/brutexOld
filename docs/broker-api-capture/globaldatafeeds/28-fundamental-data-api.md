# GlobalDataFeeds — Fundamental Data API (Corporate Data)

Scope: Archive of the Fundamental Data API documentation site (docs.globaldatafeeds.in, an Apidog-published doc) — introduction, authentication, master API list, error/diagnostic responses, WebSocket connect guide, a representative REST endpoint reference — plus the full site page index and the marketing product page.

Source URLs:
- https://docs.globaldatafeeds.in/ (index / Introduction)
- https://docs.globaldatafeeds.in/authentication-request-response-923682m0
- https://docs.globaldatafeeds.in/list-of-apis-923685m0
- https://docs.globaldatafeeds.in/diagnostic-api-responses-925705m0
- https://docs.globaldatafeeds.in/how-to-connect-using-websocket-api-2181778m0
- https://docs.globaldatafeeds.in/getcorporateannouncements-15575575e0
- https://globaldatafeeds.in/fundamental-data-apis/ (product page)

Capture notes:
- REST base URL used throughout the docs (trial/"Try It" server): `https://test.lisuns.com:4532/`
- The docs site exposes an LLMs.txt index at https://docs.globaldatafeeds.in/llms.txt (llms-full.txt exists but returned empty at capture time).
- Due to fetch budget, individual pages for every endpoint/schema were NOT captured; the complete page index with URLs is preserved in the "Site structure" section below so each page remains addressable. Pages not captured are marked.

---

## Fundamental Data API - Introduction

> Source: https://docs.globaldatafeeds.in/ (canonical: https://docs.globaldatafeeds.in/fundamental-data-api-introduction-923681m0) — captured 2026-07-15

### Setting the context

Established in 2010, Global Financial Datafeeds LLP (aka Global Datafeeds or GFDL) began its operations by disseminating realtime price data of NSE for AmiBroker. Instead of building its own analysis platform, GFDL decided to make data of Indian Stock Exchanges available into world's renowned charting and stock analysis platforms.

Fast forward to 2025, today GFDL is a premier vendor of realtime data of all Indian Stock Exchanges (NSE, NFO, CDS, BSE, BFO, MCX, NCDEX) with out-of-the-box integration with **30+ major international technical analysis & charting platforms**. A robust set of matured APIs have been helping companies / traders to build their custom analysis models and platforms with ease.

### Addition of Fundamental Data (Corporate Data)

Though realtime data serves requirement of strategy / algo / swing traders, scalpers, intraday traders, prop desk, etc., it does not truly encompass the entire gamut of stock market participants. A large set of traders/investors still rely on other type of data to take their trading / investment decisions. This data - typically known as **Corporate Data / Fundamental Data** - includes a variety of information about the listed companies, their promotors, operations, news, events, financial results and much more. **Through this new venture, we would attempt to fulfil this requirement of market participants.**

### Fundamental Data - what is available

Realtime and historical Corporate Data (Fundamental Data) of companies listed on Indian Stock Exchanges will be available through APIs. It will include **Corporate Announcements, Corporate Actions, Share Holding Patterns, Financial Results, Voting Results, Corporate Governance, Promotor's Pledge alongwith Bulk/block Deals, Company/Promotors Information, Market Cap, Annual Reports, Sectoral Classification** - and much more.

To see entire list of offerings, see List of APIs: https://docs.globaldatafeeds.in/list-of-apis-923685m0

### Fundamental Data - Who this is for

Our services are useful for anyone who needs fundamental data to take their investment decisions. This includes and is not limited to - long term investors, Mutual Funds, Research Institutes, Service Providers, Platform vendors - and many more.

### Why us

You are in good and strong company. Since 2010, we have been disseminating realtime/historical price data of companies listed on Indian Stock Exchanges with **99.995% uptime**. Today we handle **70 million data requests daily (7+ crore)** from variety of users across multiple platforms & via variety of APIs.

Robust, proven expertise in disseminating low-latency data via multiple redundant channels across **web, mobile & desktop**.

We source our data directly from the Stock Exchanges. So you can be sure about the authenticity, completeness and timeliness of the data.

Simple yet powerful APIs

**Less Time-to-Market**: we provide user-friendly APIs with extensive code samples in **15+ programming languages**. So you can customize your requirement, create & test the code on-the-fly immediately. Paste this code into your project and you are good to go live within few hours.

**No more trials, ifs-and-buts**: generate the code, test it, analyse the response - all in our publicly available platform. Talk to our support team over phone / whatsapp / discussion forum / email to resolve your queries. Only when you are satisfied, get in touch with our Sales Team for product payment & subscription.

*Modified at 2025-05-07 05:38:34*

---

## Authentication, Request & Response

> Source: https://docs.globaldatafeeds.in/authentication-request-response-923682m0 — captured 2026-07-15

### Request Format

RESTful APIs (HTTP GET) are used for sending the request where inputs are sent as part of URL in the form of query parameters.

### Response Format

Response is received in **Json, Xml, Csv** or **CsvContent** format. This is user-configurable; value of "format" parameter sent in request decides the format of the response.

While data is sent in Csv as part of response, a file containing Csv values is sent when format=CsvContent.

Sometimes, API can return diagnostic response in plain/text format instead of actual data. When this happens, user will need to send the request again - till they receive actual data. A list of available diagnostic responses can be found at: https://docs.globaldatafeeds.in/diagnostic-api-responses-925705m0

### Authentication

To receive the response, user will need to send valid "AccessKey" parameter with each request. User can get the accessKey for their use by contacting our Sales Team and making the payment for the data subscription.

*Modified at 2025-04-11 12:13:17*

---

## List of APIs

> Source: https://docs.globaldatafeeds.in/list-of-apis-923685m0 — captured 2026-07-15

**Corporate Data APIs**

Currently, we provide following APIs to receive fundamental data.

| Sl | API | Update Frequency | Description |
| --- | --- | --- | --- |
| 1 | GetCorporateAnnouncementsCategories | NA | **GetCorporateAnnouncementsCategories** function is used to retrieve the available categories of corporate announcements for a specified exchange. These categories help in classifying the types of corporate updates (e.g., general announcements, board meetings, dividends, etc.) published by listed companies. |
| 2 | GetCorporateAnnouncements | Continuous (1 Min) | **GetCorporateAnnouncements** are official company disclosures on earnings, mergers, dividends, leadership changes, and other key events. |
| 3 | GetCorporateActionsCategories | EOD | **GetCorporateActionsCategories** function retrieves a list of corporate action categories available for a specific exchange. |
| 4 | GetCorporateActions | EOD | **GetCorporateActions** data are company-initiated events such as dividends, stock splits, mergers, acquisitions and buybacks. |
| 5 | GetResultsCalendar | Continuous (1 Min) | **GetResultsCalendar** returns information about upcoming or past financial results announcements for listed companies. |
| 6 | GetFinancialResultsItems | Continuous (1 Min) | **GetFinancialResultsItems** retrieves a list of companies along with the respective dates on which they have published their financial results. These details are then further used in next function (GetFinancialResults) to get actual Financial Results. |
| 7 | GetFinancialResults | Continuous (1 Min) | **GetFinancialResults** returns company's periodic financial performance reports, including revenue, profit/loss, expenses, earnings per share (EPS), and key financial ratios. |
| 8 | GetFinRatioEOD | Continuous (1 Min) | **GetFinRatioEOD** The GetFinRatioEOD API provides key financial ratios and end-of-day (EOD) metrics for a given company/instrument on a specific date. |
| 9 | GetFinRatioQuarterly | Continuous (1 Min) | **GetFinRatioQuarterly** API retrieves quarterly financial ratios for a given company based on its instrument identifier and exchange. |
| 10 | GetFinRatioSnapshot | Continuous (1 Min) | **GetFinRatioSnapshot** API function provides a snapshot of key financial ratios for a specified instrument, using the most recent financial data and the current market price. |
| 11 | GetSectoralClassification | EOD | **GetSectoralClassification** retrieves the industry and sector categorization of listed companies, including details like sector name, industry group, and sub-industry classification. |
| 12 | GetSHPItems | Continuous (1 Min) | **GetSHPItems** retrieves list of companies along with the respective dates on which their Shareholding Pattern (SHP) has been reported. These details are then further used in next function (GetSHP) to get actual Share Holding Pattern. |
| 13 | GetSHP | Continuous (1 Min) | **GetSHP** returns Share Holding Pattern. |
| 14 | GetScripMCap | EOD | **GetScripMCap** returns a company's market capitalization of scrips, calculated as share price × total outstanding shares. |
| 15 | GetExchangeMCap | EOD | **GetExchangeMCap** is a function typically used in financial or trading systems to retrieve the Market Capitalization (MCap) data on a specific exchange for a given trading day. |
| 16 | GetAnnualReports | EOD | **GetAnnualReports** retrieves a company's annual financial and operational reports, including audited financial statements, management discussions, and disclosures - in PDF Format. |
| 17 | GetCompanyData | EOD | **GetCompanyData** function provides key corporate information for a listed company, including its stock symbol, scrip code, registered address, contact details, company secretary name, RTA (Registrar and Transfer Agent) details, and identification numbers like ISIN. |
| 18 | GetBulkDeals | EOD | **GetBulkDeals** returns details about bulk deals executed in the stock market. |
| 19 | GetBlockDeals | EOD | **GetBlockDeals** returns details about block deals executed in the stock market. |
| 20 | GetDeliveryVolumes | EOD | **GetDeliveryVolumes** returns information about the delivery volume of stocks traded on a given date. |
| 21 | GetBhavCopyCM | EOD | **GetBhavCopyCM** returns the BhavCopy for the BSE Capital Market (CM) segment, providing trading data such as open, high, low, close (OHLC) prices, traded volume, turnover, etc. |
| 22 | GetBhavCopyFO | EOD | **GetBhavCopyFO** returns the BhavCopy for BSE Futures & Options (FO) segment, including details such as open, high, low, close (OHLC) prices, traded volume, open interest, and turnover for derivative contracts. |
| 23 | GetFuturesAndOptions | EOD | **GetFuturesAndOptions** returns the BhavCopy for BSE Futures & Options (FO) segment, including details such as open, high, low, close (OHLC) prices, traded volume, open interest, and turnover for derivative contracts. |
| 24 | GetIndexDetail | EOD | **GetIndexDetail** returns the BhavCopy for BSE Indices segment, including details such as open, high, low, close (OHLC) prices. |
| 25 | GetCircuitFilterDetails | EOD | **GetCircuitFilterDetails** returns the list of companies who have hit the circuit filter limits (upper and lower) as set by the exchange. |
| 26 | GetStatisticsGroupAbCompanies | EOD | **GetStatisticsGroupAbCompanies** returns statistical data for Group A and B companies of BSE, providing key metrics such as traded volume, turnover, market capitalization, and price movements. |
| 27 | GetIndexHighlights | EOD | **GetIndexHighlights** returns the summary of key stock market indices, providing data on index performance, percentage change, highest and lowest levels, traded volume, and turnover. |
| 28 | GetTotalTradeHighlights | EOD | **GetTotalTradeHighlights** returns the summary of total trade activity across the stock exchange, providing key statistics such as total traded volume, turnover, number of transactions, and market participation trends. |
| 29 | GetScripGroupTradeHighlights | EOD | **GetScripGroupTradeHighlights** retrieves the trading summary for different scrip groups, providing data on total traded volume, turnover, number of trades, and price movements within specific stock categories. |
| 30 | GetTurnoverDetailsOfTop15ScripsOfAGroup | EOD | **GetTurnoverDetailsOfTop15ScripsOfAGroup** retrieves the turnover details of the top 15 scrips in Group A, providing data on total traded volume, turnover value, and price movements. |

Endpoint doc URLs referenced by this table (in row order): getcorporateannouncementscategories-16005463e0, getcorporateannouncements-15575575e0, getcorporateactionscategories-16356140e0, getcorporateactions-15575576e0, getresultscalendar-15575583e0, getfinancialresultsitems-15575584e0, getfinancialresults-15575585e0, getfinratioeod-19453261e0, getfinratioquarterly-19498759e0, getfinratiosnapshot-19800091e0, getsectoralclassification-15575605e0, getshpitems-15575577e0, getshp-15575578e0, getscripmcap-15575589e0, getexchangemcap-17991613e0, getannualreports-15575602e0, getcompanydata-15575603e0, getbulkdeals-15575586e0, getblockdeals-15575587e0, getdeliveryvolumes-15575588e0, getbhavcopycm-15575591e0, getbhavcopyfo-15575592e0, getfuturesandoptions-15575593e0, getindexdetail-15575594e0, getcircuitfilterdetails-15575595e0, getstatisticsgroupabcompanies-15575596e0, getindexhighlights-15575598e0, gettotaltradehighlights-15575599e0, getscripgrouptradehighlights-15575600e0, getturnoverdetailsoftop15scripsofagroup-15575601e0 (all under `https://docs.globaldatafeeds.in/`).

*Modified at 2026-01-20 07:40:38*

---

## How To Connect Using WebSocket API (Corporate Data WS API)

> Source: https://docs.globaldatafeeds.in/how-to-connect-using-websocket-api-2181778m0 — captured 2026-07-15

**Overview**

The WebSocket API provides a persistent, real-time connection for receiving market data and interacting with our services. To establish a successful connection, you will need the following credentials and connection details:

- **Endpoint** – The WebSocket server address provided by our team.
- **Port Number** – The port on which the WebSocket service is available.
- **API Key** – Your unique authentication key used to access the API.

**Connection Workflow**

Follow the steps below to connect and start receiving data through the WebSocket API:

**Step 1: Establish a WebSocket Connection**

Create a WebSocket connection using the endpoint and port number provided.

```
ws://endpoint:port/
```

**Step 2: Authenticate**

After the connection is established, send an authentication request containing your API key.

Authentication is mandatory before any other API requests can be processed.

**Step 3: Verify Authentication**

Wait for the authentication response from the server.
If authentication is successful, you can proceed with data requests.
If authentication fails, verify your API key and connection details before retrying.

**Step 4: Send Data Requests**

Once authenticated, you can send requests for market data and other supported WebSocket functions.

**Step 5: Handle Disconnections**

If the WebSocket connection is interrupted for any reason (network issues, server restart, timeout, etc.), your application should automatically reconnect and repeat the following process:

1. Establish a new WebSocket connection.
2. Send the authentication request.
3. Wait for successful authentication.
4. Resubscribe or resend the required data requests.

(A connection flow diagram image is embedded on the original page.)

*Modified at 2026-05-30 09:13:17*

---

## GetCorporateAnnouncements (representative REST endpoint reference)

> Source: https://docs.globaldatafeeds.in/getcorporateannouncements-15575575e0 — captured 2026-07-15

```
GET https://test.lisuns.com:4532/GetCorporateAnnouncements
```

**GetCorporateAnnouncements** returns official company disclosures on earnings, mergers, dividends, leadership changes, and other key events that impact shareholders, stock prices, and investment decisions.

The optional parameters like 'descriptor', 'descriptorId' and 'category' can be fetched using the GetCorporateAnnouncementsCategories function (https://docs.globaldatafeeds.in/getcorporateannouncementscategories-16005463e0).

### What is returned?

ScripId, FillingDate, MeetingDate, TradeDate, ScripCode, ScripName, FileStatus, HeadLine, NewsSubject, AttachmentName, NewsBody, Descriptor, CriticalNews, AnnounceType, MeetingType, DescriptorId, AttachmentUrl, ReceivedAt, SavedAt

For details, please see glossary: https://docs.globaldatafeeds.in/glossary-923501m0

### Sample Response (JSON; also available as XML, CSV, CsvContent)

```json
{
"Value":[
{ "ScripId": "KARURVYSYA",
"FillingDate": "02-27-2025",
"MeetingDate": "01-01-0001",
"TradeDate": "02-27-2025",
"ScripCode": 590003,
"ScripName": "KARUR VYSYA BANK LTD.",
"FileStatus": "N",
"HeadLine": "Karur Vysya Bank inaugurates 6 new branches today",
"NewsSubject": "Karur Vysya Bank inaugurates 6 new branches today",
"AttachmentName": "25D02536-41FA-47C9-83E9-53DBE2225779-175624.pdf",
"NewsBody": "Karur Vysya Bank Ltd has submitted to BSE a copy of Press Release dated February 27, 2025 titled 'Karur Vysya Bank inaugurates 6 new branches today'.",
"Descriptor": "General",
"CriticalNews": 0,
"AnnounceType": "General",
"MeetingType": "",
"DescriptorId": "1",
"AttachmentUrl": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=25D02536-41FA-47C9-83E9-53DBE2225779-175624.pdf",
"ReceivedAt": "02-27-2025 17:57:03",
"SavedAt": "02-27-2025 17:57:03"}
]}
```

### Request

Query params (as shown in the request example below): `accessKey`, `exchange`, `instrumentIdentifier`, `from`, `to`, `descriptor`, `descriptorId`, `category`, `hasMeeting`, `dTFormat`, `format`.

Request example (cURL; the docs also generate samples for JavaScript, Java, Swift, Go, PHP, Python, HTTP, C, C#, Objective-C, Ruby, OCaml, Dart, R, cURL-Windows, Httpie, wget, PowerShell):

```bash
curl --location 'https://test.lisuns.com:4532/GetCorporateAnnouncements?accessKey=30bd31ff-fb7e-4d6c-a76e-06750e3eeb09&exchange=BSE&instrumentIdentifier=GODREJPROP&from=1740594600&to=1740680999&descriptor=Press%20Release%20%2F%20Media%20Release&descriptorId=35&category=Announcement%20under%20Regulation%2030%20(LODR)&hasMeeting=&dTFormat=String&format=Json'
```

(Note: the accessKey above is the public "Try It" demo key shown in the docs; the trial server returns actual data as on 27th February 2025.)

### Responses

**200 OK** — Body: text/plain — Success

Response schema example:

```json
{
    "value": [
        {
            "symbol": "string",
            "fillingDate": "02-25-2025",
            "meetingDate": "02-25-2025",
            "tradeDate": "02-25-2025",
            "scripCode": 0,
            "companyName": "string",
            "fileStatus": "string",
            "headLine": "string",
            "newsSubject": "string",
            "attachmentName": "string",
            "newsBody": "string",
            "descriptor": "string",
            "criticalNews": 0,
            "announceType": "string",
            "meetingType": "string",
            "descriptorId": "string",
            "attachmentUrl": "string",
            "receivedAt": "02-25-2025 18:00:00",
            "savedAt": "02-25-2025 18:00:00"
        }
    ],
    "count": 0
}
```

*Modified at 2026-04-11 04:32:29*

Note: other REST endpoints on this site follow the same pattern — `GET https://test.lisuns.com:4532/<FunctionName>` with `accessKey` plus function-specific query params, response format selected by `format` (Json/Xml/Csv/CsvContent), and diagnostic messages returned as text/plain.

---

## Diagnostic API Responses (error/informational messages)

> Source: https://docs.globaldatafeeds.in/diagnostic-api-responses-925705m0 — captured 2026-07-15

At times, server sends diagnostic responses instead of requested data. Please find below list of possible informational messages (returns as text/plain) by our server. Most of these responses are self-explanatory. **Your code should be designed to handle these responses which are returned as text/plain (instead of json/xml/csv).**

| API Response | Description |
| --- | --- |
| Instrument is not allowed for Demo key. | As a Trial Demo Key Only Few Instruments are allowed. Please try to check with allowed Instruments |
| Date is out of allowed range. | Please request "from" and "to" parameter values within allowed period |
| Date is out of allowed range of 1 day. | Please request "from" and "to" parameter values within allowed period |
| Date is out of allowed range of 5 days. | Please request "from" and "to" parameter values within allowed period |
| Date is out of allowed range of 30 days. | Please request "from" and "to" parameter values within allowed period |
| From - To is out of allowed range of 10 years. | Please request "from" and "to" parameter values within allowed period |
| Authentication request received. Try request data in next moment. | This response is sent when requested data is not ready at server. This is done because REST is stateless protocol without any callbacks. If this is not done and if server decides to wait, your application will feel like hang with poor user experience. When this response is received, you are supposed to send same request again in loop – till you receive requested data. |
| Access Denied. Key not found. | Please check the key used, key is not found at server |
| Access Denied. Key blocked. | Used key is blocked. Please contact our team (with screenshot of error, actual request sent & paste actual key used) on developer@globaldatafeeds.in |
| Access Denied. Key unknown or empty. | Please check the key used, key is not found at server |
| Key Expired. | Please check the key used, key is expired |
| Data for requested exchange is disabled. | The key is not authorised to request data from the Exchange used. Please make sure you are using valid Exchange value. |
| Function not enabled. | The key is using API function which is not enabled for your account. If you feel this is error, please contact our team (with screenshot of error, actual request sent & paste actual key used) on developer@globaldatafeeds.in |
| Bandwidth per hour is limited. | The key is using more bandwidth than configured value. The counter is reset every hour (e.g. 10AM, 11AM, 12PM, etc.) so if you receive this message, please try again after clock passes current hour. |
| Calls per hour are limited. | The key is using more no. of requests than configured value per hour. The counter is reset every hour (e.g. 10AM, 11AM, 12PM, etc.) so if you receive this message, please try again after clock passes current hour. |
| Calls limitation is reached. | The key is using more no. of requests than configured value for the month. |
| Bandwidth limitation is reached. | The key is using more bandwidth than configured value for the month. |
| Access Denied. Key Invalid. | Please check the key used, key is not found at server |
| Filters cant be empty. | User must provide at least one filter like sector, industry, or basicIndustry. |

*Modified at 2025-08-03 14:15:04*

---

## Site structure — full page index of docs.globaldatafeeds.in

> Source: sidebar navigation of https://docs.globaldatafeeds.in/ — captured 2026-07-15
> Pages marked [captured above] are reproduced in this file; all others were NOT individually captured (fetch budget) — URLs preserved for reference. Each page is also available as markdown by appending `.md` per the site's LLMs.txt convention (https://docs.globaldatafeeds.in/llms.txt).

Top-level guides:
- Fundamental Data API - Introduction — /fundamental-data-api-introduction-923681m0 [captured above]
- Authentication, Request & Response — /authentication-request-response-923682m0 [captured above]
- Code Samples & API Trial — /code-samples-api-trial-923683m0
- List of APIs — /list-of-apis-923685m0 [captured above]
- Type of Corporate Data Available — /type-of-corporate-data-available-1142925m0

Corporate Data (WS API):
- How To Connect Using WebSocket API — /how-to-connect-using-websocket-api-2181778m0 [captured above]
- SubscribeCorporateAnnouncements — /subscribecorporateannouncements-2174523m0
- SubscribeFinancialResults — /subscribefinancialresults-2181121m0

Corporate Data APIs (RESTful):
- Corporate Announcements: GetCorporateAnnouncementsCategories — /getcorporateannouncementscategories-16005463e0; GetCorporateAnnouncements — /getcorporateannouncements-15575575e0 [captured above]
- Corporate Actions: GetCorporateActionsCategories — /getcorporateactionscategories-16356140e0; GetCorporateActions — /getcorporateactions-15575576e0
- Financial Results: GetResultsCalendar — /getresultscalendar-15575583e0; GetFinancialResultsItems — /getfinancialresultsitems-15575584e0; GetFinancialResults — /getfinancialresults-15575585e0; GetFinancialRatios — /getfinancialratios-27153098e0 (List-of-APIs page additionally references GetFinRatioEOD — /getfinratioeod-19453261e0, GetFinRatioQuarterly — /getfinratioquarterly-19498759e0, GetFinRatioSnapshot — /getfinratiosnapshot-19800091e0)
- Sectoral Classification: GetSectoralClassification — /getsectoralclassification-15575605e0; GetSectors — /getsectors-21797542e0; GetMei — /getmei-21801200e0; GetIndustries — /getindustries-21955709e0; GetBasicIndustries — /getbasicindustries-21955934e0
- Share Holding Patterns: GetShpItems — /getshpitems-15575577e0; GetSHP — /getshp-15575578e0; GetSHPAdvanced — /getshpadvanced-23196898e0
- Market Capitalization: GetScripMCap — /getscripmcap-15575589e0; GetExchangeMCap — /getexchangemcap-17991613e0
- Annual Reports (PDF): GetAnnualReports — /getannualreports-31420321e0 (also /getannualreports-15575602e0)
- Company Information: GetCompanyData — /getcompanydata-15575603e0
- Bulk / Block Deals: GetBulkDeals — /getbulkdeals-15575586e0; GetBlockDeals — /getblockdeals-15575587e0
- Delivery Volumes: GetDeliveryVolumes — /getdeliveryvolumes-15575588e0
- FII DII Activity: GetFIIDII — /getfiidii-37773825e0

Other Data APIs:
- Index Constituents — /index-constituents-933202m0
- GetEODStats — /geteodstats-16005369e0
- GetBhavCopyCM — /getbhavcopycm-15575591e0
- GetBhavCopyFO — /getbhavcopyfo-15575592e0
- GetFuturesAndOptions — /getfuturesandoptions-15575593e0
- GetIndexDetail — /getindexdetail-15575594e0
- GetCircuitFilterDetails — /getcircuitfilterdetails-15575595e0
- GetStatisticsGroupAbCompanies — /getstatisticsgroupabcompanies-15575596e0
- GetTop5GainersLosers — /gettop5gainerslosers-15575597e0
- GetIndexHighlights — /getindexhighlights-15575598e0
- GetTotalTradeHighlights — /gettotaltradehighlights-15575599e0
- GetScripGroupTradeHighlights — /getscripgrouptradehighlights-15575600e0
- GetTurnoverDetailsOfTop15ScripsofAgroup — /getturnoverdetailsoftop15scripsofagroup-15575601e0

EOD Statistics:
- GetSeriesChange — /getserieschange-16934245e0
- GetBannedSecurities — /getbannedsecurities-16945669e0
- GetDeliverable — /-getdeliverable-16953847e0
- GetVolatality — /getvolatality-17039702e0
- GetStatsMCap — /getstatsmcap-17991704e0
- GetNewHL — /getnewhl-18721671e0
- GetCircuitBreakers — /getcircuitbreakers-19111099e0

Helper APIs:
- GetServerInfo — /getserverinfo-15575607e0
- GetLimitation — /getlimitation-15575606e0
- GetInstruments — /getinstruments-17992355e0

Reference:
- Glossary — /glossary-923501m0
- Diagnostic API Responses — /diagnostic-api-responses-925705m0 [captured above]
- Release Notes — /release-notes-926014m0
- FAQ's — /faqs-2131262m0

Schemas (data-model pages, all under `https://docs.globaldatafeeds.in/`, not individually captured):
AnnualReportItem — /annualreportitem-5999210d0; AnnualReports — /annualreports-5999211d0; BulkDealItem — /bulkdealitem-5999212d0; BulkDeals — /bulkdeals-5999213d0; BulkDealsRange — /bulkdealsrange-5999214d0; CgFactsResponse — /cgfactsresponse-5999215d0; CompanyData — /companydata-5999216d0; CompanyDataItem — /companydataitem-5999217d0; ContactDetails — /contactdetails-5999218d0; ContactDetailsItem — /contactdetailsitem-5999219d0; CorporateActions — /corporateactions-5999220d0; CorporateActionsItem — /corporateactionsitem-5999221d0; CorporateAnnouncement — /corporateannouncement-5999222d0; CorporateAnnouncementItem — /corporateannouncementitem-5999223d0; DateSymbolQueryItem — /datesymbolqueryitem-5999224d0; DateSymbolQueryResponse — /datesymbolqueryresponse-5999225d0; DateTimeFormat — /datetimeformat-5999226d0; DeliveryVolumes — /deliveryvolumes-5999227d0; DeliveryVolumesItem — /deliveryvolumesitem-5999228d0; DeliveryVolumesRange — /deliveryvolumesrange-5999229d0; FinResultPeriod — /finresultperiod-5999230d0; FinResultsFact — /finresultsfact-5999231d0; FinResultsQueryItem — /finresultsqueryitem-5999232d0; FinResultsQueryResponse — /finresultsqueryresponse-5999233d0; FinResultsResponse — /finresultsresponse-5999234d0; FinResultsType — /finresultstype-5999235d0; GainersLosersRequest — /gainerslosersrequest-5999236d0; GenericXbrlFact — /genericxbrlfact-5999237d0; MarketCapHistory — /marketcaphistory-5999238d0; MarketCapItem — /marketcapitem-5999239d0; NatureOfFinReport — /natureoffinreport-5999240d0; QeBhavCopyCM — /qebhavcopycm-5999241d0; QeBhavCopyCMResult — /qebhavcopycmresult-5999242d0; QeBhavCopyFO — /qebhavcopyfo-5999243d0; QeBhavCopyFOResult — /qebhavcopyforesult-5999244d0; QeCircuitFilterDetails — /qecircuitfilterdetails-5999245d0; QeCircuitFilterDetailsResult — /qecircuitfilterdetailsresult-5999246d0; QeEquityDerivativesMs — /qeequityderivativesms-5999247d0; QeEquityDerivativesMsResult — /qeequityderivativesmsresult-5999248d0; QeIndexDetail — /qeindexdetail-5999249d0; QeIndexDetailResult — /qeindexdetailresult-5999250d0; QeIndexHighlight — /qeindexhighlight-5999251d0; QeIndexHighlightResult — /qeindexhighlightresult-5999252d0; QeScripGroupTradeHighlight — /qescripgrouptradehighlight-5999253d0; QeScripGroupTradeHighlightResult — /qescripgrouptradehighlightresult-5999254d0; QeStatisticsGroupAbCompanies — /qestatisticsgroupabcompanies-5999255d0; QeStatisticsGroupAbCompaniesResult — /qestatisticsgroupabcompaniesresult-5999256d0; QeTop15TurnoverDetails — /qetop15turnoverdetails-5999257d0; QeTop15TurnoverDetailsResult — /qetop15turnoverdetailsresult-5999258d0; QeTopGainersLosers — /qetopgainerslosers-5999259d0; QeTopGainersLosersResult — /qetopgainerslosersresult-5999260d0; QeTotalTradeHighlight — /qetotaltradehighlight-5999261d0; QeTotalTradeHighlightResult — /qetotaltradehighlightresult-5999262d0; ResponseFormat — /responseformat-5999263d0; ResultCalendar — /resultcalendar-5999264d0; ResultCalendarItem — /resultcalendaritem-5999265d0; ResultCalendarRange — /resultcalendarrange-5999266d0; SectoralClassification — /sectoralclassification-5999267d0; SectoralClassificationItem — /sectoralclassificationitem-5999268d0; ShpFactsResponse — /shpfactsresponse-5999269d0; VotingFactsResponse — /votingfactsresponse-5999270d0

---

## Fundamental Data APIs — product page (marketing)

> Source: https://globaldatafeeds.in/fundamental-data-apis/ — captured 2026-07-15 (navigation/menus/footer omitted)

### Fundamental Data APIs

- Provides comprehensive real-time and historical corporate data for companies listed on Indian stock exchanges.
- Covers a wide range of corporate and financial information essential for investors, analysts, and fintech platforms.
- Enables access to regulatory disclosures, shareholder insights, and company performance indicators.
- Helps in understanding company structure, ownership patterns, and market presence.
- Offers reliable data for research, compliance, reporting, and investment decision-making.
- Ideal for use in financial platforms, advisory tools, portfolio tracking, and market intelligence solutions.
- Continuously updated to reflect the latest filings, announcements, and market activities.

**Solution Intended For:** Stock Broking; Banks; Portfolio Managers; Mutual Funds/AMC; Wealth Management & Office; Corporate; Financial Publication & Media; Management Consulting & Business Advisory; Private Equity/VC/FII; Institutional Stock Broking; Insurance Co; Rating Agencies

**Code Samples Supports:** Restful, Swift, Shell, Ruby, R, Python, PHP, OCaml, Objective-C, JavaScript, Java, HTTP, Go, Dart, C, C#

### Features — What is available

We offer real-time, End-of-day and Historical Fundamental (Corporate) Data of listed Indian companies through RESTful APIs.

- **Corporate Announcements APIs** — Get real-time & historical corporate announcements APIs for board meetings, dividends, mergers, and leadership changes.
- **Corporate Actions APIs** — Access real-time & historical corporate actions APIs for dividends, bonus issues, stock splits, rights issues, and more.
- **Financial Results APIs** — Get financial results APIs to access real-time and historical corporate data, including quarterly earnings, annual reports, revenue, profit, and essential financial ratios.
- **Share Holding Pattern APIs** — Access shareholding pattern APIs for real-time and historical data on promoter holdings, institutional and public investors, and ownership changes over time.
- **Sectoral Classification APIs** — Get sectoral classification APIs for accurate, up-to-date data on industry sectors, sub-sectors, and segment mapping of listed companies.
- **Annual Results APIs** — Access real-time and historical annual reports APIs of listed companies, including financial statements, management discussions, and auditor reports.
- **Market Cap APIs** — Get live & historical market cap data by API — track large-cap, mid-cap, small-cap segments and company-wise trends effortlessly.
- **Company Data APIs** — Access company data APIs for RTA details, listing info, and complete profiles.
- **Bulk Deals APIs** — Access real-time and historical bulk deals data via APIs, including trade volume, price, buyer/seller information, and transaction dates.
- **Block Deals APIs** — Get real-time and historical data on block deals via APIs, including deal size, price, buyer/seller details, and execution time.
- **Delivery Volumes APIs** — Get real-time and historical data on delivery volumes via APIs, including traded quantity, delivery quantity, delivery percentage, and price movement insights.
- **BhavCopy** — Get daily Bhav Copy data including open, high, low, close prices, traded volume, and delivery statistics.
- **Corporate Governance APIs** — Access real-time & historical data on corporate governance via APIs — covering board changes, key appointments, resignations, and compliance disclosures.
- **Voting Rights** — Get real-time and historical data on voting rights, including shareholder resolutions, voting results, meeting details, and decision outcomes.
- **Consolidated Pledge APIs** — Get detailed data on consolidated pledge holdings via APIs — including promoter pledged shares, % pledged, and changes over time.

### Access our Documentation

Get complete technical reference for integrating our Fundamental Data APIs — including endpoints, response formats, and code examples.

- Detailed API Endpoints
- Response Format Guidelines
- Code Examples for Integration

Documentation: https://docs.globaldatafeeds.in/

### Request Trial Access

Experience the full potential of our Fundamental Data APIs with limited-time trial access. Perfect for evaluation and integration testing.

- Full API Access
- Sample data
- Developer Support

The page also lists corporate clients (large set of Indian Mutual Funds/AMCs — e.g. SBI, HDFC, ICICI Prudential, Kotak, Nippon India, Axis, UTI, Tata, Mirae Asset, Motilal Oswal, Franklin Templeton, DSP, Edelweiss, Aditya Birla, Jio BlackRock and others) and standard company footer information (Global Financial Datafeeds LLP, 301, 4th Floor, Krishna Enclave, Jehan Circle, Gangapur Road, Nashik – 422005, Maharashtra, India).
