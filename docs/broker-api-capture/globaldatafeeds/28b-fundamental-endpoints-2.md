# GlobalDataFeeds — Fundamental Data API — Endpoints 2 (Financial Results, Financial Ratios, Sectoral Classification)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetFinancialResultsItems — /getfinancialresultsitems-15575584e0
2. GetFinancialResults — /getfinancialresults-15575585e0
3. GetFinancialRatios — /getfinancialratios-27153098e0
4. GetSectoralClassification — /getsectoralclassification-15575605e0
5. GetSectors — /getsectors-21797542e0
6. GetMei — /getmei-21801200e0
7. GetIndustries — /getindustries-21955709e0
8. GetBasicIndustries — /getbasicindustries-21955934e0

Not retrievable (recorded in 28z): /getfinratioeod-19453261e0, /getfinratioquarterly-19498759e0, /getfinratiosnapshot-19800091e0 — both the `.md` and HTML forms return an empty body (pages appear retired; their functionality is covered by GetFinancialRatios with `type` = Realtime / EOD / Quarter).

---

## GetFinancialResultsItems

> Source: https://docs.globaldatafeeds.in/getfinancialresultsitems-15575584e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetFinancialResultsItems:
    get:
      summary: GetFinancialResultsItems
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetFinancialResultsItems** returns list of companies along with the
        respective dates for which they have published their financial results.
        This function helps investors and analysts track financial disclosures
        over time, ensuring timely access to company performance data for
        informed decision-making.


        These details are then further used in next function
        (GetFinancialResults) to get actual Financial Results




        ### What is returned?


        Year, InstrumentIdentifier, NatureOfReport, ResultsType, ResultPeriod


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
                {  
                    "Value": [  
                    {  
                    "Year": 2024,  
                    "Instrument": "INDRANIB",  
                    "NatureOfReport": "Standalone",  
                    "ResultsType": "ProfitLoss",  
                    "ResultPeriod": "QD"  
                    },  
                    {  
                    "Year": 2024,  
                    "Instrument": "INDRANIB",  
                    "NatureOfReport": "Standalone",  
                    "ResultsType": "ProfitLoss",  
                    "ResultPeriod": "M9"  
                    },  
                    {  
                    "Year": 2024,  
                    "Instrument": "INDRANIB",  
                    "NatureOfReport": "Consolidated",  
                    "ResultsType": "ProfitLoss",  
                    "ResultPeriod": "QD"  
                    },  
                    {  
                    "Year": 2024,  
                    "Instrument": "INDRANIB",  
                    "NatureOfReport": "Consolidated",  
                    "ResultsType": "ProfitLoss",  
                    "ResultPeriod": "M9"  
                    }  
                    ]  
                    }
            </Tab>
            <Tab title="XML">
                &lt;FinResultsQueryResponse&gt;
                &lt;Value&gt;
                &lt;FinResultsQueryItem Year="2024" Instrument="INDRANIB" NatureOfReport="Standalone" ResultsType="ProfitLoss" ResultPeriod="QD"/&gt;
                &lt;FinResultsQueryItem Year="2024" Instrument="INDRANIB" NatureOfReport="Standalone" ResultsType="ProfitLoss" ResultPeriod="M9"/&gt;
                &lt;FinResultsQueryItem Year="2024" Instrument="INDRANIB" NatureOfReport="Consolidated" ResultsType="ProfitLoss" ResultPeriod="QD"/&gt;
                &lt;FinResultsQueryItem Year="2024" Instrument="INDRANIB" NatureOfReport="Consolidated" ResultsType="ProfitLoss" ResultPeriod="M9"/&gt;
                &lt;/Value&gt;
                &lt;/FinResultsQueryResponse&gt;
            </Tab>
            <Tab title="CSV">
                Year,InstrumentIdentifier,NatureOfReport,ResultsType,ResultPeriod  
                2024,INDRANIB,Standalone,ProfitLoss,QD  
                2024,INDRANIB,Standalone,ProfitLoss,M9  
                2024,INDRANIB,Consolidated,ProfitLoss,QD  
                2024,INDRANIB,Consolidated,ProfitLoss,M9
            </Tab>
            <Tab title="CsvContent">
                Year,InstrumentIdentifier,NatureOfReport,ResultsType,ResultPeriod  
                2024,INDRANIB,Standalone,ProfitLoss,QD  
                2024,INDRANIB,Standalone,ProfitLoss,M9  
                2024,INDRANIB,Consolidated,ProfitLoss,QD  
                2024,INDRANIB,Consolidated,ProfitLoss,M9
                
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
        - name: Year
          in: query
          description: Should be less or equal toYear, default = currentYear, max dif = 10
          required: false
          example:
            - '2024'
          schema:
            type: integer
            format: int32
            default: 2024
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: ACC+ABB
            max. limit is 25 instruments per single request
          required: false
          example: ACC+ABB
          schema:
            type: string
            default: RELIANCE+ABB
        - name: natureOfReports
          in: query
          description: 'empty or NatureOfFinReport:'
          required: false
          example: Standalone+Consolidated
          schema:
            type: string
        - name: types
          in: query
          description: 'empty or FinResultsType:'
          required: false
          example: ProfitLoss+BalanceSheet+CashFlow
          schema:
            type: string
        - name: periods
          in: query
          description: 'empty or FinResultPeriod:'
          required: false
          example: QM+M12+QJ+QS+M6+QD+M9
          schema:
            type: string
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
                $ref: '#/components/schemas/FinResultsQueryResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Financial Results
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575584-run
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
    FinResultsQueryResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/FinResultsQueryItem'
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
    FinResultsQueryItem:
      type: object
      properties:
        year:
          type: integer
          format: int32
        instrument:
          type: string
          nullable: true
        natureOfReport:
          $ref: '#/components/schemas/NatureOfFinReport'
        resultsType:
          $ref: '#/components/schemas/FinResultsType'
        resultPeriod:
          $ref: '#/components/schemas/FinResultPeriod'
      additionalProperties: false
      x-apidog-orders:
        - year
        - instrument
        - natureOfReport
        - resultsType
        - resultPeriod
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
    FinResultPeriod:
      enum:
        - QM
        - M12
        - QJ
        - QS
        - M6
        - QD
        - M9
      type: string
      x-apidog-folder: ''
    FinResultsType:
      enum:
        - ProfitLoss
        - BalanceSheet
        - CashFlow
        - RelatedPartyTransactions
        - FR
      type: string
      x-apidog-folder: ''
    NatureOfFinReport:
      enum:
        - Standalone
        - Consolidated
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetFinancialResults

> Source: https://docs.globaldatafeeds.in/getfinancialresults-15575585e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetFinancialResults?type=ProfitLoss:
    get:
      summary: GetFinancialResults
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetFinancialResults** returns company's periodic financial performance
        reports, including revenue, profit/loss, expenses, earnings per share
        (EPS), and key financial ratios. These results, typically released
        quarterly or annually, help investors and analysts assess a company's
        profitability, growth, and overall financial health.


        To use this function, you will need year, name of the company and other
        details related to Financial Result which you can obtain from 
        [GetFinancialResultsItems](/getfinancialresultsitems-15575584e0)
        function.


        ### What is returned?


        Label, UnitId, Decimals, Value, Description, Key


        For details, please see [glossary](/glossary-923501m0)


        Financial Result Period as follows:


        <table border="1" cellpadding="8" cellspacing="0">
          <thead>
            <tr>
              <th>Financial Period</th>
              <th>Notation</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>QJ</td>
              <td>Quarter June</td>
            </tr>
            <tr>
              <td>QS</td>
              <td>Quarter September</td>
            </tr>
            <tr>
              <td>QD</td>
              <td>Quarter December</td>
            </tr>
            <tr>
              <td>QM</td>
              <td>Quarter March</td>
            </tr>
            <tr>
              <td>M6</td>
              <td>Six Months (April to September)</td>
            </tr>
            <tr>
              <td>M9</td>
              <td>Nine Months</td>
            </tr>
            <tr>
              <td>M12</td>
              <td>12 Months / Annual</td>
            </tr>
          </tbody>
        </table>




        ### **Sample Response**


        | Response Type | Sample
        Response                                                                                         
        |

        | :------------ |
        :-------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetFinancialResultsResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetFinancialResultsResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetFinancialResultsResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                                |
      tags:
        - Corporate Data APIs (RESTful)/Financial Results
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
          description: 'Example: RELIANCE'
          required: true
          example: ABB
          schema:
            type: string
            default: RELIANCE
        - name: year
          in: query
          description: |-
            please enter the financial year.
            Example : 2024 for  FY-2024-2025. 
          required: true
          example: 2024
          schema:
            type: integer
            format: int32
            default: 2024
        - name: natureOfReport
          in: query
          description: 'NatureOfReport. Available values : Standalone, Consolidated'
          required: true
          example: Standalone
          schema:
            $ref: '#/components/schemas/NatureOfFinReport'
            default: Consolidated
        - name: type
          in: query
          description: >-
            FinResultsType. Available values : ProfitLoss, BalanceSheet,
            CashFlow, RelatedPartyTransactions, FR
          required: true
          example: ProfitLoss
          schema:
            $ref: '#/components/schemas/FinResultsType'
            default: ProfitLoss
        - name: period
          in: query
          description: 'FinResultPeriod. Available values : QM, M12, QJ, QS, M6, QD, M9'
          required: true
          example: QD
          schema:
            $ref: '#/components/schemas/FinResultPeriod'
            default: QD
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
                $ref: '#/components/schemas/FinResultsResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Financial Results
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575585-run
components:
  schemas:
    NatureOfFinReport:
      enum:
        - Standalone
        - Consolidated
      type: string
      x-apidog-folder: ''
    FinResultsType:
      enum:
        - ProfitLoss
        - BalanceSheet
        - CashFlow
        - RelatedPartyTransactions
        - FR
      type: string
      x-apidog-folder: ''
    FinResultPeriod:
      enum:
        - QM
        - M12
        - QJ
        - QS
        - M6
        - QD
        - M9
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
    FinResultsResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/FinResultsFact'
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
    FinResultsFact:
      type: object
      properties:
        label:
          type: string
          nullable: true
        unitId:
          type: string
          nullable: true
        decimals:
          type: integer
          format: int32
        value:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        key:
          type: string
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - label
        - unitId
        - decimals
        - value
        - description
        - key
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetFinancialRatios

> Source: https://docs.globaldatafeeds.in/getfinancialratios-27153098e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetFinancialRatios:
    get:
      summary: GetFinancialRatios
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **GetFinancialRatios** API retrieves key financial ratios and
        performance metrics for a specified company or instrument. It enables
        users to fetch standalone or consolidated financial ratios for a
        selected reporting period, specific date, or financial year.


        Users can also specify the type of financial data to be retrieved using
        the type parameter.


        Supported Types

        EOD – End-of-Day financial ratios

        Realtime – Real-time financial ratio data

        Quarter – Quarterly financial ratios


        ### What is returned?

        Year, Stock Price, MarketCap, EODClosePrice, Week52High, Week52Low, EPS,
        PeRatio, BookValuePerShare, DividendYield, EBIT, InterestCoverageRatio,
        FaceValue, PriceSalesRatio, ProfitAfterTax, EnterpriseValue, Debt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
                {
          "EODClosePrice": 3604.45,
          "Year": 2024,
          "LastQ": "QM",
          "MarketCap": 1304121.56,
          "Week52High": 4585.9,
          "Week52Low": 3060.25,
          "EPS": 134.19,
          "PeRatio": 26.8587928464978,
          "BookValuePerShare": 261.756906077348,
          "DividendYield": 119296.980121794,
          "EBIT": 661270000000,
          "InterestCoverageRatio": 83.0741206030151,
          "FaceValue": 1,
          "PriceSalesRatio": 5.11041226050038,
          "ProfitAfterTax": 487970000000,
          "EnterpriseValue": 12957795600000,
          "Debt": 0
        }

        }
            </Tab>
            <Tab title="XML">
                 &lt;FinResultFrEod Year="2024" LastQ="QM" MarketCap="1304121.56" Week52High="4585.9" Week52Low="3060.25" EPS="134.19" PeRatio="26.858792846497767" BookValuePerShare="261.75690607734805" DividendYield="119296.9801217939" EBIT="661270000000" InterestCoverageRatio="83.07412060301507" FaceValue="1" PriceSalesRatio="5.110412260500384" ProfitAfterTax="487970000000" EnterpriseValue="12957795600000" Debt="0" EODClosePrice="3604.45" xmlns="clr-namespace:IConnectorPlugin.DataFlow.FD.EOD;assembly=IConnectorPlugin" &gt;
            </Tab>
            <Tab title="CSV">
              Year,LastQ,MarketCap,EODClosePrice,Week52High,Week52Low,EPS,PeRatio,BookValuePerShare,DividendYield,EBIT,InterestCoverageRatio,FaceValue,PriceSalesRatio,ProfitAfterTax,EnterpriseValue,Debt
        2024,QM,1304121.56,3604.45,4585.9,3060.25,134.19,26.858792846497767,261.75690607734805,119296.9801217939,661270000000,83.07412060301507,1,5.110412260500384,487970000000,12957795600000,0
            </Tab>
            <Tab title="CsvContent">
                Year,LastQ,MarketCap,EODClosePrice,Week52High,Week52Low,EPS,PeRatio,BookValuePerShare,DividendYield,EBIT,InterestCoverageRatio,FaceValue,PriceSalesRatio,ProfitAfterTax,EnterpriseValue,Debt
        2024,QM,1304121.56,3604.45,4585.9,3060.25,134.19,26.858792846497767,261.75690607734805,119296.9801217939,661270000000,83.07412060301507,1,5.110412260500384,487970000000,12957795600000,0
                
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
          description: 'Example: TCS'
          required: true
          example: TCS
          schema:
            type: string
            default: RELIANCE
        - name: type
          in: query
          description: |-
            FinancialRatiosType
            Available values : Realtime, EOD, Quarter
          required: true
          example: EOD
          schema:
            type: string
        - name: natureOfReport
          in: query
          description: 'NatureOfReport. Available values : Standalone, Consolidated'
          required: true
          example: Standalone
          schema:
            type: string
        - name: date
          in: query
          description: 'Example: 1740594600'
          required: false
          example: 1740594600
          schema:
            type: integer
        - name: period
          in: query
          description: |-
            FinResultPeriod

            Available values : QM, QJ, QS, QD, Latest
          required: false
          example: QD
          schema:
            type: string
        - name: year
          in: query
          description: 'Example : 2025'
          required: false
          example: '2025'
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
          description: Success
          content:
            text/plain:
              schema:
                $ref: '#/components/schemas/FinResultsResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Financial Results
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-27153098-run
components:
  schemas:
    FinResultsResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/FinResultsFact'
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
    FinResultsFact:
      type: object
      properties:
        label:
          type: string
          nullable: true
        unitId:
          type: string
          nullable: true
        decimals:
          type: integer
          format: int32
        value:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        key:
          type: string
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - label
        - unitId
        - decimals
        - value
        - description
        - key
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetSectoralClassification

> Source: https://docs.globaldatafeeds.in/getsectoralclassification-15575605e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetSectoralClassification:
    get:
      summary: GetSectoralClassification
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>



        The **GetSectoralClassification** retrieves the industry and sector
        categorization of listed companies, including details like sector name,
        industry group, and sub-industry classification. This function helps
        investors and analysts group companies by business domain and compare
        performance within or across sectors.


        ***Note: User must provide at least one filter like
        instrumentIdentifiers, sector, industry, basicIndustry or from-to
        parameters***


        ### **What is returned?**


        Symbol, ScripCode, CompanyName, AllotedFrom, MeiCode,
        MacroEconomicIndicator, SectCode, Sector, IndustryCode, Industry,
        BasicIndustryCode, BasicIndustry, ISIN, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {

        "Value":[

        { "Symbol": "TCS",

        "ScripCode": 532540,

        "CompanyName": "Tata Consultancy Services Ltd.",

        "AllotedFrom": "03-31-2022",

        "MeiCode": ""IN08"",

        "MacroEconomicIndicator": "Information Technology",

        "SectCode": "IN0801",

        "Sector": "Information Technology",

        "IndustryCode": "IN080101",

        "Industry": "IT - Software",

        "BasicIndustryCode": "IN080101001",

        "BasicIndustry": "Computers - Software & Consulting",

        "ISIN": "INE467B01029",

        "ReceivedAt": "05-07-2025 16:56:40",

        "SavedAt": "05-07-2025 16:56:52"}

        ]}

        </Tab>

        <Tab title="XML">

        &lt;SectoralClassification&gt;
            &lt;Value&gt;
              &lt;SectoralClassificationItem Symbol="TCS" ScripCode="532540" CompanyName="Tata Consultancy Services Ltd." AllotedFrom="03-31-2022" MeiCode="IN08" MacroEconomicIndicator="Information Technology" SectCode="IN0801" Sector="Information Technology" IndustryCode="IN080101" Industry="IT - Software" BasicIndustryCode="IN080101001" BasicIndustry="Computers - Software &amp; Consulting" ISIN="INE467B01029" ReceivedAt="05-07-2025 16:56:40" SavedAt="05-07-2025 16:56:52" /&gt;
            &lt;/Value&gt;
          &lt;/SectoralClassification&gt;
          </Tab>
          <Tab title="CSV">
        Symbol,ScripCode,CompanyName,AllotedFrom,MeiCode,MacroEconomicIndicator,SectCode,Sector,IndustryCode,Industry,BasicIndustryCode,BasicIndustry,ISIN,ReceivedAt,SavedAt


        TCS,532540,Tata Consultancy Services Ltd.,"03-31-2022",IN08,Information
        Technology,IN0801,Information Technology,IN080101,IT -
        Software,IN080101001,Computers - Software &
        Consulting,INE467B01029,"05-07-2025 16:56:40","05-07-2025 16:56:52"
          </Tab>
          <Tab title="CsvContent">
        Symbol,ScripCode,CompanyName,AllotedFrom,MeiCode,MacroEconomicIndicator,SectCode,Sector,IndustryCode,Industry,BasicIndustryCode,BasicIndustry,ISIN,ReceivedAt,SavedAt


        TCS,532540,Tata Consultancy Services Ltd.,"03-31-2022",IN08,Information
        Technology,IN0801,Information Technology,IN080101,IT -
        Software,IN080101001,Computers - Software &
        Consulting,INE467B01029,"05-07-2025 16:56:40","05-07-2025 16:56:52"


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Sectoral Classification
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
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: RELIANCE+ABB
            max. limit is 25 instruments per single request
          required: false
          example: ABB
          schema:
            type: string
        - name: sector
          in: query
          description: sector of the company (e.g., Capital Goods)
          required: false
          example: Capital Goods
          schema:
            type: string
        - name: industry
          in: query
          description: >-
            A more specific category within the sector, indicating the type of
            business (e.g., Oil & Gas, Software)
          required: false
          example: Electrical Equipment
          schema:
            type: string
        - name: basicIndustry
          in: query
          description: >-
            sub-industry the company operates in (e.g., Integrated Oil & Gas,
            Application Software).
          required: false
          example: Heavy Electrical Equipment
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: ''
          schema:
            type: string
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: ''
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
        - name: format
          in: query
          description: 'Available values : Json, Xml, Csv, CsvContent'
          required: false
          example: Json
          schema:
            $ref: '#/components/schemas/ResponseFormat'
      responses:
        '200':
          description: Success
          content:
            text/plain:
              schema:
                $ref: '#/components/schemas/SectoralClassification'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Sectoral Classification
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575605-run
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
    SectoralClassification:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/SectoralClassificationItem'
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
    SectoralClassificationItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        scripCode:
          type: integer
          format: int32
        companyName:
          type: string
          nullable: true
        allotedFrom:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        meiCode:
          type: string
          nullable: true
        macroEconomicIndicator:
          type: string
          nullable: true
        sectCode:
          type: string
          nullable: true
        sector:
          type: string
          nullable: true
        industryCode:
          type: string
          nullable: true
        industry:
          type: string
          nullable: true
        basicIndustryCode:
          type: string
          nullable: true
        basicIndustry:
          type: string
          nullable: true
        isin:
          type: string
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
        - companyName
        - allotedFrom
        - meiCode
        - macroEconomicIndicator
        - sectCode
        - sector
        - industryCode
        - industry
        - basicIndustryCode
        - basicIndustry
        - isin
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

## GetSectors

> Source: https://docs.globaldatafeeds.in/getsectors-21797542e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetSectors:
    get:
      summary: GetSectors
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetSectors** function provides a list of sectors available for a given
        exchange. Each sector is returned with a unique code and its
        corresponding name.


        ### **What is returned?**


        Sector Code and Sector Name


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {
          "Value": [
            {
              "Code": "IN0101",
              "Name": "Chemicals"
            },
            {
              "Code": "IN0102",
              "Name": "Construction Materials"
            },
            {
              "Code": "IN0103",
              "Name": "Metals & Mining"
            }
        }

        </Tab>

        <Tab title="XML">

        &lt;CodeNames&gt;

        &lt;Value&gt;

        &lt;CodeNameItem Code="IN0101" Name="Chemicals"/&gt;

        &lt;CodeNameItem Code="IN0102" Name="Construction Materials"/&gt;

        &lt;CodeNameItem Code="IN0103" Name="Metals & Mining"/&gt;

        &lt;/Value&gt;

        &lt;/CodeNames&gt;
          </Tab>
          <Tab title="CSV">
        Code,Name

        IN0101,Chemicals

        IN0102,Construction Materials

        IN0103,Metals & Mining
          </Tab>
          <Tab title="CsvContent">
        Code,Name

        IN0101,Chemicals

        IN0102,Construction Materials

        IN0103,Metals & Mining


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Sectoral Classification
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
          description: ''
          required: true
          example: BSE
          schema:
            type: string
        - name: format
          in: query
          description: ''
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Sectoral Classification
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-21797542-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetMei

> Source: https://docs.globaldatafeeds.in/getmei-21801200e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetMei:
    get:
      summary: GetMei
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetMei** function provides a list of Macro Economic Indicators (MEI)
        associated with a given exchange. Each indicator is returned with a
        unique code and its name.


        ### **What is returned?**


        MEI Code and MEI Name


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {
          "Value": [
            {
              "Code": "IN01",
              "Name": "Commodities"
            },
            {
              "Code": "IN02",
              "Name": "Consumer Discretionary"
            },
            {
              "Code": "IN03",
              "Name": "Energy"
            }
        }

        </Tab>

        <Tab title="XML">

        &lt;CodeNames&gt;

        &lt;Value&gt;

        &lt;CodeNameItem Code="IN01" Name="Commodities"/&gt;

        &lt;CodeNameItem Code="IN02" Name="Consumer Discretionary"/&gt;

        &lt;CodeNameItem Code="IN03" Name="Energy"/&gt;

        &lt;/Value&gt;

        &lt;/CodeNames&gt;
          </Tab>
          <Tab title="CSV">
        Code,Name

        IN01,Commodities

        IN02,Consumer Discretionary

        IN03,Energy
          </Tab>
          <Tab title="CsvContent">
        Code,Name

        IN01,Commodities

        IN02,Consumer Discretionary

        IN03,Energy


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Sectoral Classification
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
          description: ''
          required: true
          example: BSE
          schema:
            type: string
        - name: format
          in: query
          description: ''
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Sectoral Classification
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-21801200-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetIndustries

> Source: https://docs.globaldatafeeds.in/getindustries-21955709e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetIndustries:
    get:
      summary: GetIndustries
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetIndustries** function provides a list of industries under a given
        exchange. Each industry is returned with a unique code and its
        descriptive name.


        ### **What is returned?**


        Industry Code and Industry Name


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {
          "Value": [
            {
              "Code": "IN010101",
              "Name": "Chemicals & Petrochemicals"
            },
            {
              "Code": "IN010102",
              "Name": "Fertilizers & Agrochemicals"
            },
            {
              "Code": "IN010203",
              "Name": "Cement & Cement Products"
            }
        }

        </Tab>

        <Tab title="XML">

        &lt;CodeNames&gt;

        &lt;Value&gt;

        &lt;CodeNameItem Code="IN010101" Name="Chemicals & Petrochemicals"/&gt;

        &lt;CodeNameItem Code="IN010102" Name="Fertilizers & Agrochemicals"/&gt;

        &lt;CodeNameItem Code="IN010203" Name="Cement & Cement Products"/&gt;

        &lt;CodeNameItem Code="IN010204" Name="Other Construction
        Materials"/&gt;

        &lt;/Value&gt;

        &lt;/CodeNames&gt;
          </Tab>
          <Tab title="CSV">
        Code,Name

        IN010101,Chemicals & Petrochemicals

        IN010102,Fertilizers & Agrochemicals

        IN010203,Cement & Cement Products
          </Tab>
          <Tab title="CsvContent">
        Code,Name

        IN010101,Chemicals & Petrochemicals

        IN010102,Fertilizers & Agrochemicals

        IN010203,Cement & Cement Products


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Sectoral Classification
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
          description: ''
          required: true
          example: BSE
          schema:
            type: string
        - name: format
          in: query
          description: ''
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Sectoral Classification
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-21955709-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetBasicIndustries

> Source: https://docs.globaldatafeeds.in/getbasicindustries-21955934e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBasicIndustries:
    get:
      summary: GetBasicIndustries
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetBasicIndustries** function provides a list of basic industries
        (sub-industries) for a given exchange. Each basic industry is returned
        with a unique code and its descriptive name.


        ### **What is returned?**


        Basic Industry Code and Basic Industry Name


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {
          "Value": [
            {
              "Code": "IN010101001",
              "Name": "Commodity Chemicals"
            },
            {
              "Code": "IN010101002",
              "Name": "Specialty Chemicals"
            },
            {
              "Code": "IN010101003",
              "Name": "Carbon Black"
            }
        </Tab>

        <Tab title="XML">

        &lt;CodeNames&gt;

        &lt;Value&gt;

        &lt;CodeNameItem Code="IN010101001" Name="Commodity Chemicals"/&gt;

        &lt;CodeNameItem Code="IN010101002" Name="Specialty Chemicals"/&gt;

        &lt;CodeNameItem Code="IN010101003" Name="Carbon Black"/&gt;

        &lt;/Value&gt;

        &lt;/CodeNames&gt;
          </Tab>
          <Tab title="CSV">
        Code,Name

        IN010101001,Commodity Chemicals

        IN010101002,Specialty Chemicals

        IN010101003,Carbon Black
          </Tab>
          <Tab title="CsvContent">
        Code,Name

        IN010101001,Commodity Chemicals

        IN010101002,Specialty Chemicals

        IN010101003,Carbon Black


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Sectoral Classification
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
          description: ''
          required: true
          example: BSE
          schema:
            type: string
        - name: format
          in: query
          description: ''
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Sectoral Classification
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-21955934-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
