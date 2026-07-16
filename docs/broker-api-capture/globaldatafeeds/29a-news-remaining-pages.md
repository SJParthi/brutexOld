# GlobalDataFeeds — Stock Market News API — Remaining Pages (API Trial, List of APIs, Multi-Company News, General News, Glossary)

Gap-fill capture (2026-07-15) of the newsdocs.globaldatafeeds.in pages indexed in `29-news-api.md` but not previously archived. Each page fetched verbatim via its raw-markdown form (`<url>.md`). Endpoint pages are Apidog OpenAPI 3.0.1 specs; note both news endpoints publish an empty `servers: []` block — the API host is provided with the subscription (requests use relative paths `/api/news/multi` and `/api/general/news`).

Pages in this file:
1. API Trial — /-api-trial-1759051m0
2. List of APIs — /list-of-apis-1759052m0
3. Multi-Company News — /multi-company-news-31553453e0
4. General News — /general-news-31553455e0
5. Glossary — /glossary-1759055m0

---

## API Trial

> Source: https://newsdocs.globaldatafeeds.in/-api-trial-1759051m0.md — captured 2026-07-15
> (Source page title renders with a leading space: " API Trial".)

### **API Trial**
To take hands-on trial, user can exploit this documentation website. Each of the available functionality can be found under <a href="/list-of-apis-1759052m0">List of APIs</a>. User can visit the page of required functionality, use the default value of "accessKey" mentioned / embedded in the request and click on "Try it" button. Instant response will be shown below the request.

User can customize the available parameters while sending the request and instantly see the response as per the chosen parameter values.

The News API currently provides news coverage from the following sources:
1.⁠ ⁠Business Standard
2.⁠ ⁠⁠Economic Times
3.⁠ ⁠⁠Money Control
4.⁠ ⁠⁠Livemint 
5.⁠ ⁠⁠DailyForex

:::warning[**Notes**]
Since this is trial, most recent data is not available - even when user selects most recent values for "from" / "to" parameters. An arbitrary date in the past (currently 27th February 2025) is chosen as the date for which, all responses will be sent.
:::

---

## List of APIs

> Source: https://newsdocs.globaldatafeeds.in/list-of-apis-1759052m0.md — captured 2026-07-15
> (Source renders as an HTML table; converted 1:1 to markdown, links preserved.)

**News APIs**

Currently, we provide following APIs to receive NEWS data.

| Sl | API | Update Frequency | Description | History Availability |
|---|---|---|---|---|
| 1 | [SingleCompanyNews](/single-company-news-31553454e0) | Continuous (1 Min) | **Single Company News** API is used to fetch news related to a specific company using symbol, company name, ISIN, or scrip code. It supports filters like sentiment, article type, date range, limit, and offset. | 90 Days |
| 2 | [Multi-Company News](/multi-company-news-31553453e0) | Continuous (1 Min) | **Multi-Company News** API retrieves news for multiple companies in a single request. You can pass up to 20 identifiers (symbols, ISINs, or scrip codes) as comma-separated values along with filters like sentiment, article type, and date range. | 90 Days |
| 3 | [GeneralNews](/general-news-31553455e0) | Continuous (1 Min) | **General News** API provides market-wide news across categories such as Economy, IPO, Equity, Commodities, Mutual Funds, Cryptocurrency, and more. It supports filtering by sentiment, category type, date range, and pagination. | 90 Days |
| 4 | [WebhookSupport](/webhook-2068702m0) | Real-Time | **Webhook Support** allows real-time notifications for newly published news articles. Instead of polling APIs, users can register a webhook URL to automatically receive updates via HTTP POST requests. | NA |

---

## Multi-Company News

> Source: https://newsdocs.globaldatafeeds.in/multi-company-news-31553453e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/news/multi:
    get:
      summary: Multi-Company News
      deprecated: false
      description: >
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now !**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **Multi-Company News API** is used to fetch news related to multiple
        companies in a single request by passing identifiers such as symbols,
        ISINs, or scrip codes as comma-separated values. It provides details
        like title, summary, sentiment, article type, and source, and supports
        filters such as sentiment, article type, and date range to retrieve
        relevant news efficiently.




        ## What is returned?


        Title ,link, date, summary, company, article type ,sentiment ,specific
        title, long Summary, source, created at, article id,

        symbol, ISIN, scrip code 


        For details, please see [glossary](/glossary-1759055m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
            [
          {
            "title": "Ceasefire changes everything, says BofA's Amish Shah; here's what it means for your portfolio right now",
            "link": "https://economictimes.indiatimes.com/markets/expert-view/ceasefire-changes-everything-says-bofas-amish-shah-heres-what-it-means-for-your-portfolio-right-now/articleshow/130108291.cms",
            "date": "2026-04-08T09:11:20+00:00",
            "summary": "Reliance Industries is highlighted as a veteran in the market, with a focus on acquisitions, as indicated in the context of the article discussing strategic moves in response to geopolitical changes.",
            "company": "Reliance Industries Limited",
            "article_type": "Sector & Macro Insights",
            "sentiment": "neutral",
            "specific_title": "Reliance Industries Pursues Strategic Acquisitions",
            "long_summary": "Bank of America, represented by Amish Shah, has set a year-end price target for the Nifty at 26,200, suggesting a potential upside of approximately 13% from current levels. This optimistic outlook is largely attributed to the recent India-Pakistan ceasefire, which is expected to ease inflation and reduce the likelihood of interest rate hikes by the Reserve Bank of India. Shah emphasized that the ceasefire allows the market to revert to its pre-conflict dynamics, leading to improved GDP growth and lower inflation. He noted that historically, markets tend to re-rate positively following the end of conflicts, and with crude oil prices projected to average $92.5 per barrel for the year, the easing of commodity pressures is anticipated to benefit various sectors. \n\nReliance Industries is highlighted as a key player in the market, focusing on strategic acquisitions in response to geopolitical changes. Shah also pointed out that sectors such as power and energy infrastructure, pharmaceuticals, and telecom are expected to thrive regardless of geopolitical tensions, as they are insulated from conflict-related disruptions. Financials are viewed as a value sector that has become more attractive due to lower valuations during the conflict. Overall, the message from Bank of America is that the conflict premium is dissipating, and investors should adjust their portfolios accordingly.",
            "source": "Economic Times",
            "created_at": "2026-04-08T09:16:02.578128+00:00",
            "sector": null,
            "article_id": 384860,
            "symbol": "RELIANCE",
            "ISIN": "INE002A01018",
            "scrip_code": 500325
          },
          {
            "title": "Vedanta’s demerger emerged as a key lender concern in JAL race against Adani- Moneycontrol.com",
            "link": "https://www.moneycontrol.com/news/business/vedanta-s-demerger-emerged-as-a-key-lender-concern-in-jal-race-against-adani-13882910.html",
            "date": "2026-04-08T04:31:35+00:00",
            "summary": "State Bank of India is leading a consortium of lenders managing the debt resolution process for Jaiprakash Associates Ltd, evaluating bids based on upfront cash and execution certainty.",
            "company": "State Bank of India",
            "article_type": "Strategic Actions",
            "sentiment": "neutral",
            "specific_title": "State Bank of India Leads Lender Consortium for JAL Resolution",
            "long_summary": "Vedanta's ongoing demerger into five distinct entities—aluminium, zinc, oil and gas, steel, and power—has raised significant concerns among lenders during the bidding process for Jaiprakash Associates Ltd (JAL). This restructuring has influenced lenders' evaluations of Vedanta's financial profile compared to Adani Enterprises' resolution plan, which has been approved for ₹14,535 crore. Despite Vedanta's higher bid of ₹16,726 crore, the Supreme Court declined to stay the implementation of Adani's plan, further complicating Vedanta's position. The consortium of lenders, led by the State Bank of India (SBI), is focusing on both the upfront cash offered and the execution certainty of the bids. Lenders are particularly interested in how Vedanta's demerger will affect the financial stability and asset profile of the entity backing its bid. The ongoing restructuring is seen as a critical factor in assessing the viability of Vedanta's offer, as it may impact the company's ability to meet future commitments.",
            "source": "Money Control",
            "created_at": "2026-04-08T04:38:20.112503+00:00",
            "sector": null,
            "article_id": 384130,
            "symbol": "SBIN",
            "ISIN": "INE062A01020",
            "scrip_code": 500112
          },
            </Tab>
            
          </Tabs>
        </div>
      tags:
        - Multi-company news
      parameters:
        - name: api_key
          in: query
          description: 'API key provided by GFDL '
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: symbols
          in: query
          description: 'Example: TCS,SBIN,INFY'
          required: false
          example: RELIANCE,SBIN,JPPOWER,ACL
          schema:
            type: string
        - name: isins
          in: query
          description: Example:INE0RUV01018,INE930P01018,INE550C01020
          required: false
          example: INE0RUV01018,INE930P01018,INE550C01020
          schema:
            type: string
        - name: scrip_codes
          in: query
          description: |-
            Example:532921,500820,532215 
            Note: Applicable  for BSE 
          required: false
          example: 532921,500820,532215
          schema:
            type: string
        - name: sentiment
          in: query
          description: >-
            Type of sentiment you want to filter, such as positive, negative, or
            neutral.
          required: false
          example: positive
          schema:
            type: string
        - name: Articaltype
          in: query
          description: >-
            Specifies the article type for filtering news, which includes
            Financial Performance, Sector & Macro Insights, Analyst & Credit
            Ratings, Strategic Actions, Corporate Actions, and Index &
            Reshuffles.
          required: false
          example: 'corporate Action '
          schema:
            type: string
        - name: 'sector '
          in: query
          description: >-
            Specifies the sector for filtering news, such as Finance,
            Agriculture, or Automobile.

            Note: This filter is applicable only when the article type is
            “Sector & Macro Insights” or when the article type is not provided.
          required: false
          example: finance
          schema:
            type: string
        - name: from
          in: query
          description: Start date in ISO 8601 format (e.g., 2025-05-01T00:00:00+05:30).
          required: false
          example: ''
          schema:
            type: string
        - name: to
          in: query
          description: End date in ISO 8601
          required: false
          example: ''
          schema:
            type: string
        - name: limit
          in: query
          description: Return 5 articles
          required: false
          example: '5'
          schema:
            type: string
        - name: offset
          in: query
          description: Skip first 5 articles
          required: false
          example: '5'
          schema:
            type: string
        - name: 'format '
          in: query
          description: ''
          required: false
          example: ''
          schema:
            type: string
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
                x-apidog-orders: []
          headers: {}
          x-apidog-name: ''
      security: []
      x-apidog-folder: Multi-company news
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/1124707/apis/api-31553453-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

---

## General News

> Source: https://newsdocs.globaldatafeeds.in/general-news-31553455e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/general/news:
    get:
      summary: General News
      deprecated: false
      description: >
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now !**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **General News API** is used to fetch market-wide news across various
        categories such as Economy, IPO, Equity, Commodities, Mutual Funds,
        Cryptocurrency, and more. It provides detailed information including
        title, summary, sentiment, article type, and source, and supports
        filters like sentiment, category type, and date range for efficient news
        retrieval.




        ## What is returned?


        Title ,link, date, summary, company, article type ,sentiment ,specific
        title, long Summary, source, created at, article id,

        symbol, ISIN, scrip code 


        For details, please see [glossary](/glossary-1759055m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
          [
          {
            "title": "India expects US to extend waiver on Russian oil imports amid global energy volatility",
            "link": "https://economictimes.indiatimes.com/industry/energy/oil-gas/india-expects-us-to-extend-waiver-on-russian-oil-imports-amid-global-energy-volatility/articleshow/130111431.cms",
            "date": "2026-04-08T11:17:27+00:00",
            "summary": "India anticipates the U.S. will extend its waiver on Russian oil imports to stabilize energy supplies amid global market volatility. Ajay Srivastava, founder of the Global Trade Research Initiative, emphasized the need for India to use this opportunity to enhance its crude oil, LPG, and LNG reserves and strengthen long-term partnerships with stable suppliers like Russia. In March, India's Russian crude imports surged by 90% despite an overall decline in oil imports due to supply disruptions.",
            "company": "N/A",
            "article_type": "Commodities",
            "sentiment": "positive",
            "specific_title": "India Seeks to Strengthen Energy Reserves Amid Waiver on Russian Oil Imports",
            "long_summary": "India is anticipating an extension of the U.S. waiver on Russian oil imports, a move aimed at stabilizing energy supplies amid global market volatility. Ajay Srivastava, founder of the Global Trade Research Initiative, highlighted this moment as a critical opportunity for India to bolster its reserves of crude oil, liquefied petroleum gas (LPG), and liquefied natural gas (LNG). In March, India's imports of Russian crude surged by 90% compared to February, despite an overall decline in oil imports by nearly 15% due to supply disruptions in West Asia. The disruptions, particularly in the Strait of Hormuz, also led to a significant 40% drop in LPG imports and reduced LNG availability. Srivastava emphasized the importance of deepening long-term partnerships with stable suppliers like Russia to mitigate vulnerabilities to geopolitical shocks. The Kremlin has noted a rising demand for Russian energy amid ongoing global economic and energy crises, indicating a shift in market conditions. This context underscores the strategic importance of securing stable energy supplies for India's economy during these turbulent times.",
            "source": "Economic Times",
            "created_at": "2026-04-08T11:21:42.108764+00:00",
            "article_id": 385084,
            "symbol": "N/A",
            "ISIN": "N/A",
            "scrip_code": "N/A"
          },
          {
            "title": "Ceasefire may bring relief to 14% of India’s exports, but recovery hinges on Gulf stability- Moneycontrol.com",
            "link": "https://www.moneycontrol.com/news/business/economy/ceasefire-may-bring-relief-to-14-of-india-s-exports-but-recovery-hinges-on-gulf-stability-13883424.html",
            "date": "2026-04-08T10:57:55+00:00",
            "summary": "A ceasefire between Iran and the US may benefit approximately 14% of India's exports, particularly in the jewellery, agriculture, and industrial goods sectors, as trade to the Gulf region stabilizes after disruptions. However, the recovery's pace will depend on Gulf economies restoring production and demand, alongside managing elevated logistics costs.",
            "company": "N/A",
            "article_type": "Economy",
            "sentiment": "positive",
            "specific_title": "Ceasefire Benefits India's Exports to Gulf Region",
            "long_summary": "A recent ceasefire between Iran and the US is projected to benefit approximately 14% of India's exports, particularly in the jewellery, agriculture, and industrial goods sectors, as trade to the Gulf region stabilizes following disruptions caused by the closure of the Strait of Hormuz. Exporters in these sectors are expected to see a gradual resumption of trade flows, especially through this critical shipping route. However, the pace of recovery will largely depend on Gulf economies' ability to restore production and demand, alongside managing elevated logistics costs, which include higher insurance premiums and transit charges. Notably, several export categories have shown significant dependence on the Gulf region, with refined copper wire exports increasing from 91.4% in 2024 to 93.8% in 2025, and silk fabrics rising from 66.7% to 81.7%. In value terms, articles of precious metal exports surged from $5.46 billion in 2024 to $7.09 billion in 2025, while smartphone exports rose from $2.78 billion to $4.07 billion. Despite the potential for near-term relief, a sustained recovery will hinge on stabilizing logistics costs and demand conditions in the Gulf region.",
            "source": "Money Control",
            "created_at": "2026-04-08T11:00:36.941513+00:00",
            "article_id": 385046,
            "symbol": "N/A",
            "ISIN": "N/A",
            "scrip_code": "N/A"
          },
            </Tab>
            
          </Tabs>
        </div>
      tags:
        - General News
      parameters:
        - name: api_key
          in: query
          description: ''
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: Type
          in: query
          description: >-
            Specifies the general  type for filtering news, which includes
            Mutual Funds, Economy, Cryptocurrency, Fixed 

            Income, Commodities, IPO, Indices, Equity, and 

            Market News. 
          required: false
          example: IPO
          schema:
            type: string
        - name: sentiment
          in: query
          description: >-
            Type of sentiment you want to filter, such as positive, negative, or
            neutral.
          required: false
          schema:
            type: string
        - name: from
          in: query
          description: Start date in ISO 8601 format (e.g., 2025-05-01T00:00:00+05:30).
          required: false
          schema:
            type: string
        - name: to
          in: query
          description: End date in ISO 8601
          required: false
          schema:
            type: string
        - name: limit
          in: query
          description: Return 5 articles
          required: false
          example: '5'
          schema:
            type: string
        - name: offset
          in: query
          description: Skip first 5 articles
          required: false
          example: '5'
          schema:
            type: string
      responses:
        '200':
          description: ''
          content:
            application/json:
              schema:
                type: object
                properties: {}
                x-apidog-orders: []
          headers: {}
          x-apidog-name: ''
      security: []
      x-apidog-folder: General News
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/1124707/apis/api-31553455-run
components:
  schemas: {}
  securitySchemes: {}
servers: []
security: []

```

---

## Glossary

> Source: https://newsdocs.globaldatafeeds.in/glossary-1759055m0.md — captured 2026-07-15
> (Source embeds raw HTML tables in the markdown; preserved as served. Source quirks kept verbatim: parameter 7 is spelled "isns", the description of "to" reads "Example: RELIANCE", and the last five response rows repeat Sr. No. 15.)

<html>
<body>
<h2>Request Parameters</h2>
    <table>
        <thead>
            <tr>
                <th>Sr. No</th>
                <th>Parameter</th>
                <th>Description</th>
            </tr>
        </thead>
        <tbody>
            <tr><td>1</td><td>api_key</td><td>Authentication key for API access.</td></tr>
            <tr><td>2</td><td>symbol</td><td>Stock symbol of the company . Example =TCS</td></tr>
            <tr><td>3</td><td>company_name</td><td> Company name (substring search  e.g., 'Tata' matches all companies containing 'Tata')</td></tr>
            <tr><td>4</td><td>isin</td><td>International Securities Identification Number. Example= INE467B01029 </td></tr>
            <tr><td>5</td><td>scrip_code</td><td>BSE scrip code of the company. Example= 532540 </td></tr>
            <tr><td>6</td><td>symbols</td><td>Stock symbols of the company. Example = TCS,INFY</td></tr>
            <tr><td>7</td><td>isns</td><td>International Securities Identification Number. Example=INE467B01029,INE009A01021</td></tr>
            <tr><td>8</td><td>scrip_codes</td><td>BSE scrip code of the company. Example= 532540,543623 </td></tr>
            <tr><td>9</td><td>sentiment</td><td>Type of sentiment you want to filter, such as positive, negative, or neutral.</td></tr>
            <tr><td>10</td><td>articaltype</td><td>Specifies the article type for filtering news, which includes Financial Performance, Sector & Macro Insights, Analyst & Credit Ratings, Strategic Actions, Corporate Actions, and Index & Reshuffles.</td></tr>
            <tr><td>11</td><td>sector</td><td>Specifies the sector for filtering news, such as Finance, Agriculture, or Automobile.</td></tr>
            <tr><td>12</td><td>from</td><td>Start date in ISO 8601 (e.g., 2025-0501T00:00:00+05:30 </td></tr>
            <tr><td>13</td><td>to</td><td>Example: RELIANCE</td></tr>
            <tr><td>14</td><td>limit</td><td>Number of news you want to fetch  </td></tr>
            <tr><td>15</td><td>offset</td><td>Number of records to skip </td></tr>
            <tr><td>16</td><td>type</td><td>Specifies the general  type for filtering news, which includes Mutual Funds, Economy, Cryptocurrency, Fixed Income, Commodities, IPO, Indices, Equity, and Market News. </td></tr>
        </tbody>
    </table>
</body>
</html>
<!DOCTYPE html>
<html>
<head>
    <h2>Response Parameters</h2>
</head>
<body>
    <table>
        <tr>
            <th>Sr. No.</th>
            <th>Parameter</th>
            <th>Description</th>
        </tr>
        <tr><td>1</td><td>title</td><td>Original title of the news article.</td></tr>
        <tr><td>2</td><td>link</td><td>URL directing to the full article.</td></tr>
        <tr><td>3</td><td>date</td><td>Publication date and time of the article.</td></tr>
        <tr><td>4</td><td>summary</td><td>Brief overview of the article content.</td></tr>
        <tr><td>5</td><td>long_summary</td><td>Detailed summary providing in-depth information about the article.</td></tr>
        <tr><td>6</td><td>article_type</td><td>Category of the article (e.g., Financial Performance, Strategic Actions).</td></tr>
        <tr><td>7</td><td>sentiment</td><td>Sentiment of the article, which can be positive, negative, or neutral.</td></tr>
        <tr><td>8</td><td>specific_title</td><td>A more refined or specific headline describing the article.</td></tr>
        <tr><td>9</td><td>source</td><td>Name of the publisher or platform from which the article is sourced.</td></tr>
        <tr><td>10</td><td>company</td><td>Name of the company associated with the news article (if applicable).</td></tr>
        <tr><td>11</td><td>created_at</td><td> Timestamp indicating when the news record was created in the system.</td></tr>
        <tr><td>12</td><td>symbol</td><td>Trading symbol of the company associated with the news.</td></tr>
        <tr><td>13</td><td>ISIN</td><td>International Securities Identification Number used to uniquely identify the security.</td></tr>
        <tr><td>14</td><td>scrip_code</td><td>Exchange-specific code used to identify the company (applicable mainly for BSE)</td></tr>
        <tr><td>15</td><td>webhook_id</td><td>Unique identifier assigned to the registered webhook.</td></tr>
        <tr><td>15</td><td>url</td><td>The endpoint URL where webhook notifications will be sent.</td></tr>
        <tr><td>15</td><td>events</td><td> List of events subscribed for the webhook (e.g., article.published)<td></tr>
        <tr><td>15</td><td>secret</td><td>Secret key used to verify the authenticity of webhook requests.</td></tr>
<tr><td>15</td><td>article_id</td><td>Unique identifier assigned to each news article, used to distinguish and reference a specific article in the system.</td></tr>
        
    </table>
</body>
</html>
