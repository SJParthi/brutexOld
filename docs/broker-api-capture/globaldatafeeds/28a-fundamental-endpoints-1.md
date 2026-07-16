# GlobalDataFeeds — Fundamental Data API — Endpoints 1 (Guides, WebSocket API, Corporate Announcements/Actions, Results Calendar)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Each page fetched verbatim via its raw-markdown form (`<url>.md`). Endpoint pages are published by Apidog as an OpenAPI 3.0.1 spec per endpoint — parameter tables, response schemas and sample responses are embedded in the YAML. REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. Code Samples & API Trial — /code-samples-api-trial-923683m0
2. Type of Corporate Data Available — /type-of-corporate-data-available-1142925m0
3. SubscribeCorporateAnnouncements (WS) — /subscribecorporateannouncements-2174523m0
4. SubscribeFinancialResults (WS) — /subscribefinancialresults-2181121m0
5. GetCorporateAnnouncementsCategories — /getcorporateannouncementscategories-16005463e0
6. GetCorporateActionsCategories — /getcorporateactionscategories-16356140e0
7. GetCorporateActions — /getcorporateactions-15575576e0
8. GetResultsCalendar — /getresultscalendar-15575583e0

---

## Code Samples & API Trial

> Source: https://docs.globaldatafeeds.in/code-samples-api-trial-923683m0.md — captured 2026-07-15

### **API Trial**
To take hands-on trial, user can exploit this documentation website. Each of the available functionality can be found under [List of APIs](/list-of-apis-923685m0). User can visit the page of required functionality, use the default value of "accessKey" mentioned / embedded in the request and click on "Try it" button. Instant response will be shown below the request.

User can customize the available parameters while sending the request - including the format in which response is expected, alongwith other parameters - and instantly see the response as per the chosen parameter values.

:::warning[**Notes**]
Since this is trial, most recent data is not available - even when user selects most recent values for "from" / "to" parameters. An arbitrary date in the past (currently 27th February 2025) is chosen as the date for which, all responses will be sent.
:::

### **Generating the Code**

Whenever user clicks on "<Icon icon="solar-bold-alt-arrow-right"/> Try it" button, an appropriate request is formatted automatically in the **"code console"** for each of the available languages - as shown below. This is true even when user modifies the request to suit to their requirement. The response is sent as per value of "format" variable sent in the request (Json, Xml, Csv, CsvContent) and is visible right below the "code console".

User can then select the "Language" of their choice, click on "Copy" button and paste the code in their project.

![image.png](https://api.apidog.com/api/v1/projects/866176/resources/353150/image-preview)

### **Languages Available**

While this is not an exhaustive list, currently we generate code instantly for the following 15+ languages.

| Sr.No. | Language | Libraries |
|---|---|---|
| 1 | Shell | cURL, cURL-Windows, Httpie, wget, PowerShell |
| 2 | JavaScript | Fetch, Axios, jQuery, XHR, Native, Request, Unirest |
| 3 | Java | Unirest, OkHttp |
| 4 | Swift | URLRequest |
| 5 | Go | NewRequest |
| 6 | PHP | cURL, HTTP_Request2, pecl_http, Guzzle |
| 7 | Python | http.client, Requests |
| 8 | HTTP | http |
| 9 | C | Libcurl |
| 10 | C# | HttpClient |
| 11 | Objective C | NSURLSession |
| 12 | Ruby | uri, net::http |
| 13 | OCaml | CoHTTP |
| 14 | Dart | http |
| 15 | R | httr |

(Source page renders this list as an HTML table; converted 1:1 to markdown, no content changed.)

---

## Type of Corporate Data Available

> Source: https://docs.globaldatafeeds.in/type-of-corporate-data-available-1142925m0.md — captured 2026-07-15

We offer following types of Data through our APIs

| Module | API | Update Frequency | History Availability |
|---|---|---|---|
| Corporate Announcement | GetCorporateAnnouncements | Continuous 1 min | 30 Days |
| Corporate Announcement | GetCorporateAnnouncementsCategories | Continuous 1 min | 30 Days |
| Announcement Attachment Downloader | GetCorporateAnnouncementAttachment | NA | NA |
| Corporate Action | GetCorporateActionsCategories | EOD | 30 Days |
| Corporate Action | GetCorporateActions | EOD | 30 Days |
| ShareHoldingPattern | GetShpItems | Continuous 1 min | 30 Days |
| ShareHoldingPattern | GetSHP | Continuous 1 min | 30 Days |
| Voting | GetVotingItems | EOD | 30 Days |
| Voting | GetVoting | EOD | 30 Days |
| Corporate Governance | GetCorporateGovernanceItems | EOD | 30 Days |
| Corporate Governance | GetCorporateGovernance | EOD | 30 Days |
| Financial Results | GetResultsCalendar | Continuous 1 min | 30 Days |
| Financial Results | GetFinancialResultsItems | Continuous 1 min | 30 Days |
| Financial Results | GetFinancialResults | Continuous 1 min | 30 Days |
| Company Data | GetCompanyData | EOD | 30 Days |
| Sectoral Classification | GetSectoralClassification | EOD | 30 Days |
| BlockDeals | GetBlockDeals | EOD | 30 Days |
| BulkDeals | GetBulkDeals | EOD | 30 Days |
| DeliveryVolumes | GetDeliveryVolumes | EOD | 30 Days |
| Market Capitalisation | GetScripMCap | EOD | 30 Days |
| Market Capitalisation | GetExchangeMCap | EOD | 30 Days |
| ConsolidatedPledge | GetConsolidatedPledge | EOD | 30 Days |
| Annual Reports | GetAnnualReports | EOD | 30 Days |
| IndexConstituents | IndexConstituents | Monthly | NA |
| BhavCopy | GetBhavCopyCM | EOD | 30 Days |
| BhavCopy | GetBhavCopyFO | EOD | 30 Days |
| BhavCopy | GetFuturesAndOptions | EOD | 30 Days |
| Index | GetIndexDetail | EOD | 30 Days |
| Index | GetIndexHighlights | EOD | 30 Days |
| CircuitFilter | GetCircuitFilterDetails | EOD | 30 Days |
| Statistics | GetStatisticsGroupAbCompanies | EOD | 30 Days |
| GainersLosers | GetTop5GainersLosers | EOD | 30 Days |
| Highlights | GetTotalTradeHighlights | EOD | 30 Days |
| Highlights | GetScripGroupTradeHighlights | EOD | 30 Days |
| TurnOver | GetTurnoverDetailsOfTop15ScripsofAgroup | EOD | 30 Days |
| Helper Functions | GetLimitation | NA | NA |
| Helper Functions | GetServerInfo | NA | NA |
| Helper Functions | GetInstruments | NA | NA |

(Source page renders this as an HTML table with rowspans per Module; flattened 1:1 to markdown — module name repeated per row, no content changed. Note: this table mentions APIs — GetCorporateAnnouncementAttachment, GetVotingItems, GetVoting, GetCorporateGovernanceItems, GetCorporateGovernance, GetConsolidatedPledge — that have no individual endpoint pages in the site nav.)

---

## SubscribeCorporateAnnouncements (Corporate Data WS API)

> Source: https://docs.globaldatafeeds.in/subscribecorporateannouncements-2174523m0.md — captured 2026-07-15

**SubscribeCorporateAnnouncements** returns official company disclosures on earnings, mergers, dividends, leadership changes, and other key events that impact shareholders, stock prices, and investment decisions.

The optional parameters like 'descriptor','descriptorId' and 'category' can be fetched using the [GetCorporateAnnouncementsCategories](https://docs.globaldatafeeds.in/getcorporateannouncementscategories-16005463e0).

Supported Parameters

| Parameter | ValueType | Description |
|---|---|---|
| **Exchange** | String value like *BSE* | Name of supported exchange. Use the GetExchanges function to retrieve the list of available exchanges. |
| **Descriptor** | String value | Optional Parameter. Name or description of the corporate announcement category. Example: Board Meeting, Dividend, Results, etc. |
| **DescriptorId** | Integer value | Optional Parameter. Unique identifier assigned to a specific announcement descriptor/category. |
| **Category** | String value | Optional Parameter. Classification of the corporate announcement used to group similar announcement types. |
| **Unsubscribe** | [true] / [false], default = [false] | Optional parameter. By default subscribes to the selected category updates. If set to `true`, the corresponding subscription is removed. |

### What is returned?

ScripId, FillingDate, MeetingDate, TradeDate, ScripCode, ScripName, FileStatus, HeadLine, NewsSubject, AttachmentName, NewsBody, Descriptor, CriticalNews, AnnounceType, MeetingType, DescriptorId, AttachmentUrl, ReceivedAt, SavedAt

For details, please see [glossary](/glossary-923501m0).

### **Sample Code Snippet ( JavaScript)**

```
function SubscribeCorporateAnnouncements()
{
    var request1 = 
    {
    "MessageType": "SubscribeCorporateAnnouncements",
    "Exchange": "BSE"
    //"Descriptor": "TestDescriptor",
    //"DescriptorId": "",
    //"Category": "",
    //"Unsubscribe": false
    }
    doSend(request1);
}
```

### **Sample Response** (JSON tab)

```
Time : Mon May 25 2026 15:37:49 GMT+0530 (India Standard Time)
RESPONSE: {"Exchange":"BSE",
"ScripId":"CMMHOSP",      
"FillingDate":1779703656,
"MeetingDate":1779647400,
"TradeDate":1779703656,
"ScripCode":523489,
"ScripName":"Chennai Meenakshi Multispeciality Hospital Ltd-$",
"FileStatus":"N",
"HeadLine":"Board Meeting Outcome for The Approved Audited Financial Results Along With Audit Report, Cash Flow Statement For The Year Ended 31St March, 2026 And Declaration On The Report Of Auditors With Unmodified Opinion In The 25Th May 2026 Board Meeting.",
"NewsSubject":"Board Meeting Outcome for The Approved Audited Financial Results Along With Audit Report, Cash Flow Statement For The Year Ended 31St March, 2026 And Declaration On The Report Of Auditors With Unmodified Opinion In The 25Th May 2026 Board Meeting.",
"AttachmentName":"bcb3c595-d00a-4408-ada6-524540b50379.pdf",
"NewsBody":"This is to intimate that the Board of Directors of the Company at its meeting held on Monday, May 25, 2026 at the registered office of the Company have inter alia approved the Audited Financial Results for the quarter and financial year ended 31st March, 2026. The Board of Directors have not recommended any Dividend for the year 2025-26.The approved Audited Financial Results along with audit report, Cash Flow Statement for the year ended 31st March, 2026 and declaration on the report of auditors with unmodified opinion are enclosed with this letter.Further, we would like to inform that the financial results will be published in the newspapers pursuant to Regulation 47 of the SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015. The financial results are also available on the Company's website - www.cmmh.in.The meeting commenced at 12:00 Hours and concluded at 15:00 Hours.",
"Descriptor":"Revision of  outcome",
"CriticalNews":0,
"AnnounceType":"Outcome",
"MeetingType":"",
"DescriptorId":"208",
"Category":"Outcome of Board Meeting",
"AttachmentUrl":"https://www.bseindia.com/stockinfo/annpdfopen.aspx?pname=bcb3c595-d00a-4408-ada6-524540b50379.pdf","ReceivedAt":1779703668,"SavedAt":0,"MessageType":"RealtimeCorporateAnnouncement"}
```

---

## SubscribeFinancialResults (Corporate Data WS API)

> Source: https://docs.globaldatafeeds.in/subscribefinancialresults-2181121m0.md — captured 2026-07-15

**SubscribeFinancialResults** returns company's periodic financial performance reports, including revenue, profit/loss, expenses, earnings per share (EPS), and key financial ratios. These results, typically released quarterly or annually, help investors and analysts assess a company's profitability, growth, and overall financial health.

Supported Parameters

| Parameter | ValueType | Description |
|---|---|---|
| **Exchange** | String value like *BSE* | Name of supported exchange. Use the GetExchanges function to retrieve the list of available exchanges. |
| **Detailed** | [true] / [false] | Optional parameter. When set to true, the response includes complete details and extended information for the requested records. When set to false, only the standard set of fields is returned, resulting in a smaller response payload. |
| **Unsubscribe** | [true] / [false], default = [false] | Optional parameter. By default subscribes to the selected category updates. If set to `true`, the corresponding subscription is removed. |

### What is returned?

Label, UnitId, Decimals, Value, Description, Key

For details, please see [glossary](/glossary-923501m0).

### **Sample Code Snippet ( JavaScript)**

```
function SubscribeFinancialResults()
{
    var request1 = 
    {
        "MessageType": "SubscribeFinancialResults",	
        "Exchange": "BSE",
        "Detailed": true,
	"Unsubscribe": false
    }
    doSend(request1);
}
```

### **Sample Response**

| Response Type | Sample Response |
| :------------ | :------------------------------------------------------------------------------------------------------- |
| JSON          | [Download JSON Response](https://globaldatafeeds.in/resources/Sample_file/FinancialResults_SampleResponse.zip) |

---

## GetCorporateAnnouncementsCategories

> Source: https://docs.globaldatafeeds.in/getcorporateannouncementscategories-16005463e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCorporateAnnouncementsCategories:
    get:
      summary: GetCorporateAnnouncementsCategories
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **GetCorporateAnnouncementsCategories** function is used to retrieve
        the available categories of corporate announcements for a specified
        exchange. These categories help in classifying the types of corporate
        updates (e.g., general announcements, board meetings, dividends, etc.)
        published by listed companies.




        ## What is returned?


        DescriptorId, Descriptor, Category


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        | Response Type | Sample
        Response                                                                                        
        |

        | :------------ |
        :------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetCorpAnnCateResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetCorpAnnCateResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetCorporateAnnouncementsCategories.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned.                                              |
      tags:
        - Corporate Data APIs (RESTful)/Corporate Announcements
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: exchange
          in: query
          description: 'Example: BSE'
          required: true
          example: BSE
          schema:
            type: string
        - name: format
          in: query
          description: 'Available values : Json, Xml, Csv, CsvContent'
          required: false
          example: Json
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
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Corporate Announcements
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16005463-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetCorporateActionsCategories

> Source: https://docs.globaldatafeeds.in/getcorporateactionscategories-16356140e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCorporateActionsCategories:
    get:
      summary: GetCorporateActionsCategories
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetCorporateActionsCategories** function retrieves a list of corporate
        action categories available for a specific exchange.


        ### What is returned?


        CorporateActiontype, PurposeCode


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {

        "Value":[

        { "CorporateActiontype": "A.G.M.",

        "PurposeCode": "AGM"},

        { "CorporateActiontype": "Amalgamation",

        "PurposeCode": "AM "},

        { "CorporateActiontype": "Annual Book Closure",

        "PurposeCode": "ABC"},

        { "CorporateActiontype": "Bonus issue",

        "PurposeCode": "BN "},

        { "CorporateActiontype": "Buy Back of Shares",

        "PurposeCode": "BGM"},

        { "CorporateActiontype": "Change in Terms or Status",

        "PurposeCode": "EX "},

        { "CorporateActiontype": "Consolidation of Shares",

        "PurposeCode": "CS "},

        { "CorporateActiontype": "Conversion of FCD",

        "PurposeCode": "FC "}}
          </Tab>
          <Tab title="XML">
        &lt;CorporateActionsCategories&gt;

        &lt;Value&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="A.G.M."
        PurposeCode="AGM"/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Amalgamation"
        PurposeCode="AM "/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Annual Book
        Closure" PurposeCode="ABC"/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Bonus issue"
        PurposeCode="BN "/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Buy Back of
        Shares" PurposeCode="BGM"/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Change in Terms
        or Status" PurposeCode="EX "/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Consolidation of
        Shares" PurposeCode="CS "/&gt;

        &lt;CorporateActionsCategoriesItem CorporateActiontype="Conversion of
        FCD" PurposeCode="FC "/&gt;

        &lt;/Value&gt;

        &lt;/CorporateActionsCategories&gt;
          </Tab>
          <Tab title="CSV">
        CorporateActiontype,PurposeCode

        A.G.M.,AGM

        Amalgamation,AM 

        Annual Book Closure,ABC

        Bonus issue,BN 

        Buy Back of Shares,BGM

        Change in Terms or Status,EX 

        Consolidation of Shares,CS 

        Conversion of FCD,FC 
          </Tab>
          <Tab title="CsvContent">
        CorporateActiontype,PurposeCode

        A.G.M.,AGM

        Amalgamation,AM 

        Annual Book Closure,ABC

        Bonus issue,BN 

        Buy Back of Shares,BGM

        Change in Terms or Status,EX 

        Consolidation of Shares,CS 

        Conversion of FCD,FC 


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Corporate Actions
        - Corporate Data
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: exchange
          in: query
          description: 'Example: BSE'
          required: true
          example: BSE
          schema:
            type: string
            default: BSE
        - name: format
          in: query
          description: 'Available values : Json, Xml, Csv, CsvContent'
          required: false
          example: Json
          schema:
            $ref: '#/components/schemas/ResponseFormat'
            default: Json
      responses:
        '200':
          description: Success
          content:
            text/plain:
              schema:
                $ref: '#/components/schemas/CorporateActions'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Corporate Actions
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16356140-run
components:
  schemas:
    ResponseFormat:
      enum:
        - Json
        - Xml
        - Csv
        - CsvContent
      type: string
      x-apidog-folder: ''
    CorporateActions:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/CorporateActionsItem'
          nullable: true
        count:
          type: integer
          format: int32
          readOnly: true
      additionalProperties: false
      x-apidog-orders:
        - value
        - count
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
    CorporateActionsItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        scripCode:
          type: integer
          format: int32
        actionId:
          type: integer
          format: int64
        companyName:
          type: string
          nullable: true
        industryName:
          type: string
          nullable: true
        listedStatus:
          type: string
          nullable: true
        scripFaceValue:
          type: number
          format: double
        isin:
          type: string
          nullable: true
        announceType:
          type: string
          nullable: true
        announcementDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        meetingDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        corporateActionType:
          type: string
          nullable: true
        purposeCode:
          type: string
          nullable: true
        announcementUrl:
          type: string
          nullable: true
        ratioAmount:
          type: string
          nullable: true
        bcRdFlag:
          type: string
          nullable: true
        bcRdFrom:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        bcRdTo:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        provXdate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        ndStartDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        ndEndDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        paymentDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        referenceSrno:
          type: integer
          format: int64
        modifyDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        receivedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        savedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - symbol
        - scripCode
        - actionId
        - companyName
        - industryName
        - listedStatus
        - scripFaceValue
        - isin
        - announceType
        - announcementDate
        - meetingDate
        - corporateActionType
        - purposeCode
        - announcementUrl
        - ratioAmount
        - bcRdFlag
        - bcRdFrom
        - bcRdTo
        - provXdate
        - ndStartDate
        - ndEndDate
        - paymentDate
        - referenceSrno
        - modifyDate
        - receivedAt
        - savedAt
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetCorporateActions

> Source: https://docs.globaldatafeeds.in/getcorporateactions-15575576e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCorporateActions:
    get:
      summary: GetCorporateActions
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetCorporateActions** returns company-initiated events that impact
        shareholders, such as dividends, stock splits, mergers, acquisitions,
        and buybacks. They affect stock price, market cap, and investor
        sentiment, making them key for financial analysis and investment
        decisions.


        The optional parameters like 'actionType' and 'purposeCode' can be
        fetched using the
        [GetCorporateActionsCategories](https://docs.globaldatafeeds.in/getcorporateactionscategories-16356140e0)
        functions



        ### What is returned?


        ScripId, ScripCode, ScripName, ActionId, IndustryName, ListedStatus,
        ScripFaceValue, ISIN, AnnounceType, AnnouncementDate, MeetingDate,
        CorporateActionType, PurposeCode, AnnouncementUrl, RatioAmount,
        BcRdFlag, BcRdFrom, BcRdTo, ProvXdate, NdStartDate, NdEndDate,
        PaymentDate, ReferenceSrno, ModifyDate, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {  

        "Value":[  

        { "ScripId": "SHRINIWAS",  

        "ScripCode": 538897,  

        "ScripName": "Shri Niwas Leasing and Finance Limited",  

        "ActionId": 0,  

        "IndustryName": "Financial Services",  

        "ListedStatus": "Active",  

        "ScripFaceValue": "10.0",  

        "ISIN": "INE201F01015",  

        "AnnounceType": "U",  

        "AnnouncementDate": "02-20-2025 16:36:03",  

        "MeetingDate": ""01-01-0001 00:00:00"",  

        "CorporateActionType": "Right Issue of Equity Shares",  

        "PurposeCode": "ET ",  

        "AnnouncementUrl":
        "https://www.bseindia.com/xml-data/corpfiling/AttachHis/3f6bbaa7-8faa-4e87-8aa8-824213bfa789.pdf",  

        "RatioAmount": "00100001 ",  

        "BcRdFlag": "RD",  

        "BcRdFrom": ""02-27-2025 00:00:00"",  

        "BcRdTo": ""02-27-2025 00:00:00"",  

        "ProvXdate": ""02-27-2025 00:00:00"",  

        "NdStartDate": ""01-01-0001 00:00:00"",  

        "NdEndDate": ""01-01-0001 00:00:00"",  

        "PaymentDate": ""01-01-0001 00:00:00"",  

        "ReferenceSrno": "1",  

        "ModifyDate": "02-24-2025 16:43:15",  

        "ReceivedAt": "02-24-2025 16:44:02",  

        "SavedAt": "02-24-2025 16:44:02"}  

        ]}
          </Tab>
          <Tab title="XML">
        &lt;CorporateActions&gt;

        &lt;Value&gt;

        &lt;CorporateActionsItem ScripId="SHRINIWAS" ScripCode="538897"
        ActionId="0" ScripName="Shri Niwas Leasing and Finance Limited"
        IndustryName="Financial Services" ListedStatus="Active"
        ScripFaceValue="10" ISIN="INE201F01015" AnnounceType="U"
        AnnouncementDate="02-20-2025 16:36:03" MeetingDate="01-01-0001 00:00:00"
        CorporateActionType="Right Issue of Equity Shares" PurposeCode="ET "
        AnnouncementUrl="https://www.bseindia.com/xml-data/corpfiling/AttachHis/3f6bbaa7-8faa-4e87-8aa8-824213bfa789.pdf"
        RatioAmount="00100001 " BcRdFlag="RD" BcRdFrom="02-27-2025 00:00:00"
        BcRdTo="02-27-2025 00:00:00" ProvXdate="02-27-2025 00:00:00"
        NdStartDate="01-01-0001 00:00:00" NdEndDate="01-01-0001 00:00:00"
        PaymentDate="01-01-0001 00:00:00" ReferenceSrno="1"
        ModifyDate="02-24-2025 16:43:15" ReceivedAt="02-24-2025 16:44:02"
        SavedAt="02-24-2025 16:44:02"/&gt;

        &lt;/Value&gt;

        &lt;/CorporateActions&gt;
          </Tab>
          <Tab title="CSV">
        ScripId,ScripCode,ScripName,ActionId,IndustryName,ListedStatus,ScripFaceValue,ISIN,AnnounceType,AnnouncementDate,MeetingDate,CorporateActionType,PurposeCode,AnnouncementUrl,RatioAmount,BcRdFlag,BcRdFrom,BcRdTo,ProvXdate,NdStartDate,NdEndDate,PaymentDate,ReferenceSrno,ModifyDate,ReceivedAt,SavedAt  

        SHRINIWAS,538897,Shri Niwas Leasing and Finance Limited,0,Financial
        Services,Active,10.0,INE201F01015,U,"02-20-2025 16:36:03","01-01-0001
        00:00:00",Right Issue of Equity Shares,ET
        ,https://www.bseindia.com/xml-data/corpfiling/AttachHis/3f6bbaa7-8faa-4e87-8aa8-824213bfa789.pdf,
        00100001,RD,"02-27-2025 00:00:00","02-27-2025 00:00:00","02-27-2025
        00:00:00","01-01-0001 00:00:00","01-01-0001 00:00:00","01-01-0001
        00:00:00",1,"02-24-2025 16:43:15","02-24-2025 16:44:02","02-24-2025
        16:44:02"
          </Tab>
          <Tab title="CsvContent">
        ScripId,ScripCode,ScripName,ActionId,IndustryName,ListedStatus,ScripFaceValue,ISIN,AnnounceType,AnnouncementDate,MeetingDate,CorporateActionType,PurposeCode,AnnouncementUrl,RatioAmount,BcRdFlag,BcRdFrom,BcRdTo,ProvXdate,NdStartDate,NdEndDate,PaymentDate,ReferenceSrno,ModifyDate,ReceivedAt,SavedAt  

        SHRINIWAS,538897,Shri Niwas Leasing and Finance Limited,0,Financial
        Services,Active,10.0,INE201F01015,U,"02-20-2025 16:36:03","01-01-0001
        00:00:00",Right Issue of Equity Shares,ET
        ,https://www.bseindia.com/xml-data/corpfiling/AttachHis/3f6bbaa7-8faa-4e87-8aa8-824213bfa789.pdf,00100001
        ,RD,"02-27-2025 00:00:00","02-27-2025 00:00:00","02-27-2025
        00:00:00","01-01-0001 00:00:00","01-01-0001 00:00:00","01-01-0001
        00:00:00",1,"02-24-2025 16:43:15","02-24-2025 16:44:02","02-24-2025
        16:44:02"


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Corporate Actions
        - Corporate Data
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: exchange
          in: query
          description: 'Example: BSE'
          required: true
          example: BSE
          schema:
            type: string
            default: BSE
        - name: instrumentIdentifier
          in: query
          description: 'Example: SHRINIWAS'
          required: false
          example: KSB
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
            nullable: true
        - name: actionType
          in: query
          description: |-
            Used to specify the type of corporate action, for e.g.
             "Dividend"
          required: false
          example: Dividend
          schema:
            type: string
        - name: purposeCode
          in: query
          description: >-
            A unique identifier of the actionType. For e.g. Dividend has
            purposeCode as DP
          required: false
          example: DP
          schema:
            type: string
        - name: dTFormat
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example: String
          schema:
            $ref: '#/components/schemas/DateTimeFormat'
            default: String
        - name: format
          in: query
          description: 'Available values : Json, Xml, Csv, CsvContent'
          required: false
          example: Json
          schema:
            $ref: '#/components/schemas/ResponseFormat'
            default: Json
      responses:
        '200':
          description: Success
          content:
            text/plain:
              schema:
                $ref: '#/components/schemas/CorporateActions'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Corporate Actions
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575576-run
components:
  schemas:
    DateTimeFormat:
      enum:
        - Epoch
        - String
      type: string
      x-apidog-folder: ''
    ResponseFormat:
      enum:
        - Json
        - Xml
        - Csv
        - CsvContent
      type: string
      x-apidog-folder: ''
    CorporateActions:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/CorporateActionsItem'
          nullable: true
        count:
          type: integer
          format: int32
          readOnly: true
      additionalProperties: false
      x-apidog-orders:
        - value
        - count
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
    CorporateActionsItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        scripCode:
          type: integer
          format: int32
        actionId:
          type: integer
          format: int64
        companyName:
          type: string
          nullable: true
        industryName:
          type: string
          nullable: true
        listedStatus:
          type: string
          nullable: true
        scripFaceValue:
          type: number
          format: double
        isin:
          type: string
          nullable: true
        announceType:
          type: string
          nullable: true
        announcementDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        meetingDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        corporateActionType:
          type: string
          nullable: true
        purposeCode:
          type: string
          nullable: true
        announcementUrl:
          type: string
          nullable: true
        ratioAmount:
          type: string
          nullable: true
        bcRdFlag:
          type: string
          nullable: true
        bcRdFrom:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        bcRdTo:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        provXdate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        ndStartDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        ndEndDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        paymentDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        referenceSrno:
          type: integer
          format: int64
        modifyDate:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        receivedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        savedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - symbol
        - scripCode
        - actionId
        - companyName
        - industryName
        - listedStatus
        - scripFaceValue
        - isin
        - announceType
        - announcementDate
        - meetingDate
        - corporateActionType
        - purposeCode
        - announcementUrl
        - ratioAmount
        - bcRdFlag
        - bcRdFrom
        - bcRdTo
        - provXdate
        - ndStartDate
        - ndEndDate
        - paymentDate
        - referenceSrno
        - modifyDate
        - receivedAt
        - savedAt
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetResultsCalendar

> Source: https://docs.globaldatafeeds.in/getresultscalendar-15575583e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetResultsCalendar:
    get:
      summary: GetResultsCalendar
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetResultsCalendar** returns information about upcoming or past
        financial results announcements for listed companies. It helps users
        track earnings release dates and key stock-related metrics.



        ### What is returned?


        ScripId, ScripCode, ScripName, ISIN, ResultsDate, LTP, Change,
        ChangePct, ReceivedAt, SavedAt.


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {  
                            "Value": [  
                            {  
                            "ScripId": "ABB",  
                            "ScripCode": 500002,  
                            "ScripName": "ABB India Limited",  
                            "ISIN": "INE117A01022",  
                            "ResultsDate": 1739730600000,  
                            "LTP": 5289.35,  
                            "Change": -96.3,  
                            "ChangePct": -1.79,  
                            "ReceivedAt": 1739760900000,  
                            "SavedAt": 1739760901000  
                            }  
                            ]  
                            }
                    </Tab>
                    <Tab title="XML">
                        &lt;ResultCalendar&gt;
                        &lt;Value&gt;
                        &lt;ResultCalendarItem ScripId="ABB" ScripCode="500002" ScripName="ABB India Limited" ISIN="INE117A01022" ResultsDate="02-17-2025" LTP="5324.3" Change="133.8" ChangePct="2.58" ReceivedAt="02-17-2025 08:25:00" SavedAt="02-17-2025 08:25:01"/&gt;
                        &lt;/Value&gt;
                        &lt;/ResultCalendar&gt;
                    </Tab>
                    <Tab title="CSV">
                        ScripId,ScripCode,ScripName,ISIN,ResultsDate,LTP,Change,ChangePct,ReceivedAt,SavedAt  
          
                        ABB,500002,ABB India Limited,INE117A01022,1739730600000,5289.35,-96.3,-1.79,1739760900000,1739760901000
                    </Tab>
                    <Tab title="CsvContent">
                        ScripId,ScripCode,ScripName,ISIN,ResultsDate,LTP,Change,ChangePct,ReceivedAt,SavedAt  
          
                        ABB,500002,ABB India Limited,INE117A01022,1739730600000,5289.35,-96.3,-1.79,1739760900000,1739760901000
                        
                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
                  </Tabs>
          </div>
          
      tags:
        - Corporate Data APIs (RESTful)/Financial Results
        - Corporate Data
      parameters:
        - name: accessKey
          in: query
          description: ''
          required: true
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: exchange
          in: query
          description: 'Example: BSE'
          required: true
          example: BSE
          schema:
            type: string
            default: BSE
        - name: instrumentIdentifiers
          in: query
          description: >
            Example: RELIANCE+ABB Max. limit is 25 instruments per single
            request.
          required: false
          example: MANJEERA
          schema:
            type: string
        - name: range
          in: query
          description: 'ResultCalendarRange   Example: NextAll'
          required: false
          example: Previous7Days
          schema:
            $ref: '#/components/schemas/ResultCalendarRange'
            default: Next7days
        - name: dTFormat
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example: String
          schema:
            $ref: '#/components/schemas/DateTimeFormat'
            default: String
        - name: format
          in: query
          description: 'Available values : Json, Xml, Csv, CsvContent'
          required: false
          example: Json
          schema:
            $ref: '#/components/schemas/ResponseFormat'
            default: Json
      responses:
        '200':
          description: Success
          content:
            text/plain:
              schema:
                $ref: '#/components/schemas/ResultCalendar'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Financial Results
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575583-run
components:
  schemas:
    ResultCalendarRange:
      enum:
        - Today
        - Tomorrow
        - Next7days
        - Next15days
        - Next30days
        - All
        - yesterday
        - Previous7days
        - Previous15days
        - Previous30days
        - Previous90days
      type: string
      x-apidog-folder: ''
    DateTimeFormat:
      enum:
        - Epoch
        - String
      type: string
      x-apidog-folder: ''
    ResponseFormat:
      enum:
        - Json
        - Xml
        - Csv
        - CsvContent
      type: string
      x-apidog-folder: ''
    ResultCalendar:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/ResultCalendarItem'
          nullable: true
        count:
          type: integer
          format: int32
          readOnly: true
      additionalProperties: false
      x-apidog-orders:
        - value
        - count
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
    ResultCalendarItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        scripCode:
          type: string
          nullable: true
        scripName:
          type: string
          nullable: true
        isin:
          type: string
          nullable: true
        resultsDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        ltp:
          type: number
          format: double
        change:
          type: number
          format: double
        changePct:
          type: number
          format: double
        receivedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        savedAt:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - symbol
        - scripCode
        - scripName
        - isin
        - resultsDate
        - ltp
        - change
        - changePct
        - receivedAt
        - savedAt
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
