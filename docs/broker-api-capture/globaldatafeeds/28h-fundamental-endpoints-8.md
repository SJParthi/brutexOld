# GlobalDataFeeds — Fundamental Data API — Endpoints 8 (EOD Statistics + Helper APIs)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetSeriesChange — /getserieschange-16934245e0
2. GetBannedSecurities — /getbannedsecurities-16945669e0
3. GetDeliverable — /-getdeliverable-16953847e0
4. GetVolatality — /getvolatality-17039702e0
5. GetStatsMCap — /getstatsmcap-17991704e0
6. GetNewHL — /getnewhl-18721671e0
7. GetCircuitBreakers — /getcircuitbreakers-19111099e0
8. GetServerInfo — /getserverinfo-15575607e0
9. GetLimitation — /getlimitation-15575606e0
10. GetInstruments — /getinstruments-17992355e0

Formatting note: the source .md for GetSeriesChange and GetNewHL delivers the endpoint `description` as a single-line double-quoted YAML scalar (with \n escapes) rather than a block scalar; preserved exactly as served.

---

## GetSeriesChange

> Source: https://docs.globaldatafeeds.in/getserieschange-16934245e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetSeriesChange:
    get:
      summary: GetSeriesChange
      deprecated: false
      description: "<div style=\"display: flex; align-items: center; justify-content: space-between;\">\n  <div>\n    >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            \n  </div>\n  <img src=\"https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview\" alt=\"pointer.gif\" width=\"80\" />\n</div>\n\nThe **GetSeriesChange** retrieves information about changes in the trading series of listed securities, such as from EQ to BE.\n\n## What is returned?\n\nChangeDate, ScripId, ScripName, PrevSeries, ToSeries\n\nFor details, please see [glossary](/glossary-923501m0)\n\n### **Sample Response**\n\n<div style=\"border: 1px solid #ccc; background-color: #f9f9f9; padding: 10px; border-radius: 5px;\">\n  <Tabs style=\"background-color:lightgrey;\">\n    <Tab title=\"JSON\">\n{\n  \"Value\": [\n    {\n      \"ChangeDate\": \"02-27-2025\",\n      \"ScripId\": \"PRECISION.SM\",\n      \"ScripName\": \"PRECISION METALIKS LIMITED\",\n      \"PrevSeries\": \"SM\",\n      \"ToSeries\": \"ST\"\n    },\n    {\n      \"ChangeDate\": \"02-27-2025\",\n      \"ScripId\": \"VAKRANGEE\",\n      \"ScripName\": \"VAKRANGEE LIMITED\",\n      \"PrevSeries\": \"EQ\",\n      \"ToSeries\": \"BE\"\n    },\n    {\n      \"ChangeDate\": \"02-27-2025\",\n      \"ScripId\": \"VINEETLAB\",\n      \"ScripName\": \"VINEET LABORATORIES LIMITED\",\n      \"PrevSeries\": \"EQ\",\n      \"ToSeries\": \"BE\"\n    }\n  ]\n}\n    </Tab>\n    <Tab title=\"XML\">\n        &lt;SeriesChange&gt;\n    &lt;Value&gt;\n      &lt;SeriesChangeItem ChangeDate=\"02-27-2025\" ScripId=\"PRECISION.SM\" ScripName=\"PRECISION METALIKS LIMITED\" PrevSeries=\"SM\" ToSeries=\"ST\" /&gt;\n      &lt;SeriesChangeItem ChangeDate=\"02-27-2025\" ScripId=\"VAKRANGEE\" ScripName=\"VAKRANGEE LIMITED\" PrevSeries=\"EQ\" ToSeries=\"BE\" /&gt;\n      &lt;SeriesChangeItem ChangeDate=\"02-27-2025\" ScripId=\"VINEETLAB\" ScripName=\"VINEET LABORATORIES LIMITED\" PrevSeries=\"EQ\" ToSeries=\"BE\" /&gt;\n    &lt;/Value&gt;\n  &lt;/SeriesChange&gt;\n    </Tab>\n    <Tab title=\"CSV\">\n      ChangeDate,ScripId,ScripName,PrevSeries,ToSeries\n\t\"02-27-2025\",PRECISION.SM,PRECISION METALIKS LIMITED,SM,ST\n\t\"02-27-2025\",VAKRANGEE,VAKRANGEE LIMITED,EQ,BE\n\t\"02-27-2025\",VINEETLAB,VINEET LABORATORIES LIMITED,EQ,BE\n\n    </Tab>\n    <Tab title=\"CsvContent\">\n       ChangeDate,ScripId,ScripName,PrevSeries,ToSeries\n\t\"02-27-2025\",PRECISION.SM,PRECISION METALIKS LIMITED,SM,ST\n\t\"02-27-2025\",VAKRANGEE,VAKRANGEE LIMITED,EQ,BE\n\t\"02-27-2025\",VINEETLAB,VINEET LABORATORIES LIMITED,EQ,BE\n\n      Note: \n      1. The file containing above values in CSV format will be returned. \n      2. To see it working, copy-paste the request in browser.\n    </Tab>\n  </Tabs>\n</div>\n"
      tags:
        - EOD Statistics
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
          description: 'Example: NSE'
          required: true
          example: NSE
          schema:
            type: string
        - name: instrumentIdentifier
          in: query
          description: 'Example: DISHTV'
          required: false
          example: DISHTV
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: '1740594600'
          schema:
            type: string
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: '1740680999'
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16934245-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetBannedSecurities

> Source: https://docs.globaldatafeeds.in/getbannedsecurities-16945669e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBannedSecurities:
    get:
      summary: GetBannedSecurities
      deprecated: false
      description: >
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **GetBannedSecurities** retrieves a list of securities under trading
        restrictions for the day.


        ## What is returned?


        Date, Symbol


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
        {
          "Value": [
            {
              "Date": "02-27-2025 08:45:00",
              "Symbol": "CDSL"
            },
            {
              "Date": "02-27-2025 08:45:00",
              "Symbol": "MANAPPURAM"
            }
          ]
        }
            </Tab>
            <Tab title="XML">
                &lt;BannedSecurities&gt;
            &lt;Value&gt;
              &lt;BannedSecuritiesItem Date="02-27-2025 08:45:00" Symbol="CDSL" /&gt;
              &lt;BannedSecuritiesItem Date="02-27-2025 08:45:00" Symbol="MANAPPURAM" /&gt;
            &lt;/Value&gt;
          &lt;/BannedSecurities&gt;
            </Tab>
            <Tab title="CSV">
              Date,Symbol
        "02-27-2025 08:45:00",CDSL

        "02-27-2025 08:45:00",MANAPPURAM

            </Tab>
            <Tab title="CsvContent">
               Date,Symbol
        "02-27-2025 08:45:00",CDSL

        "02-27-2025 08:45:00",MANAPPURAM

              Note: 
              1. The file containing above values in CSV format will be returned. 
              2. To see it working, copy-paste the request in browser.
            </Tab>
          </Tabs>
        </div>
      tags:
        - EOD Statistics
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
          description: 'Example: NSE'
          required: true
          example: NSE
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: '1747074600'
          schema:
            type: string
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: '1747160999'
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16945669-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetDeliverable

> Source: https://docs.globaldatafeeds.in/-getdeliverable-16953847e0.md — captured 2026-07-15
> (Page title on source renders with a leading zero-width character: " ​GetDeliverable".)

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetDeliverable:
    get:
      summary: ' ​GetDeliverable'
      deprecated: false
      description: >
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>



        The **GetDeliverable** retrieves daily trading and delivery statistics
        for a specific security on a given date from the stock exchange.



        ### What is returned?


        Date, ScripId, ScripName, Open, High, Low, Close, PrevClose,
        LastTradedPrice, NoOfTrades, Turnover, DelQty, DelQtyPer, Series,
        AverageTradedPrice, TradedQty


        For details, please see the complete [glossary](/glossary-923501m0).


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
              {
          "Value": [
            {
              "Date": "02-27-2025",
              "ScripId": "RELIANCE",
              "ScripName": "",
              "Open": 1212.8,
              "High": 1215,
              "Low": 1200.65,
              "Close": 1207.1,
              "PrevClose": 1204,
              "LastTradedPrice": 1208,
              "NoOfTrades": 236188,
              "Turnover": 138836.37,
              "DelQty": 7994875,
              "DelQtyPer": 69.46,
              "Series": "EQ",
              "AverageTradedPrice": 1206.31,
              "TradedQty": 11509215
            }
          ]
        }
            </Tab>
            <Tab title="XML">
                &lt;Deliverable&gt;
        &lt;Value&gt;

        &lt;DeliverableItem Date="02-27-2025" ScripId="RELIANCE" ScripName=""
        Open="1212.8" High="1215" Low="1200.65" Close="1207.1" PrevClose="1204"
        LastTradedPrice="1208" NoOfTrades="236188" Turnover="138836.37"
        DelQty="7994875" DelQtyPer="69.46" Series="EQ"
        AverageTradedPrice="1206.31" TradedQty="11509215"/&gt;

        &lt;/Value&gt;

        &lt;/Deliverable&gt;
            </Tab>
            <Tab title="CSV">
              Date,ScripId,ScripName,Open,High,Low,Close,PrevClose,LastTradedPrice,NoOfTrades,Turnover,DelQty,DelQtyPer,Series,AverageTradedPrice,TradedQty
        "02-27-2025",RELIANCE,,1212.8,1215,1200.65,1207.1,1204,1208,236188,138836.37,7994875,69.46,EQ,1206.31,11509215
            </Tab>
            <Tab title="CsvContent">
              Date,ScripId,ScripName,Open,High,Low,Close,PrevClose,LastTradedPrice,NoOfTrades,Turnover,DelQty,DelQtyPer,Series,AverageTradedPrice,TradedQty
        "02-27-2025",RELIANCE,,1212.8,1215,1200.65,1207.1,1204,1208,236188,138836.37,7994875,69.46,EQ,1206.31,11509215

              Note: 
              1. The file containing above values in CSV format will be returned. 
              2. To see it working, copy-paste the request in browser.
            </Tab>
          </Tabs>
        </div>
      tags:
        - EOD Statistics
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
          description: 'Example: NSE'
          required: true
          example: NSE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp in seconds
          required: true
          example: '1740594600'
          schema:
            type: string
        - name: instrumentIdentifier
          in: query
          description: 'Example: RELIANCE'
          required: false
          example:
            - RELIANCE
          schema:
            type: array
            items:
              type: string
        - name: dTformat
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example:
            - string
          schema:
            type: array
            items:
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16953847-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetVolatality

> Source: https://docs.globaldatafeeds.in/getvolatality-17039702e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetVolatality:
    get:
      summary: GetVolatality
      deprecated: false
      description: >
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>



        The **GetVolatility** provides daily and annual volatility data for a
        stock, showing how much its price fluctuates. It includes current and
        previous volatility, log returns, and closing price.



        ### What is returned?


        Date, ScripId, ScripName, Close, PrevClose, UnderlyingLogReturn,
        PrevVolatility, Volatity, AnnualVolatality


        For details, please see the complete [glossary](/glossary-923501m0).


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
            <Tab title="JSON">
              {
          "Value": [
            {
              "Date": "02-27-2025",
              "ScripId": "RELIANCE",
              "ScripName": "",
              "Close": 1207.1,
              "PrevClose": 1204,
              "UnderlyingLogReturn": 0.0026,
              "PrevVolatility": 0.0134,
              "Volatity": 0.0133,
              "AnnualVolatality": 0.2541
            }
          ]
        }
            </Tab>
            <Tab title="XML">
                &lt;Volatality&gt;
            &lt;Value&gt;
              &lt;VolatalityItem Date="02-27-2025" ScripId="RELIANCE" Close="1207.1" PrevClose="1204" UnderlyingLogReturn="0.0026" PrevVolatility="0.0134" Volatity="0.0133" AnnualVolatality="0.2541"&gt;
                &lt;ScripName&gt;
                &lt;/ScripName&gt;
              &lt;/VolatalityItem&gt;
            &lt;/Value&gt;
          &lt;/Volatality&gt;
            </Tab>
            <Tab title="CSV">
              Date,ScripId,ScripName,Close,PrevClose,UnderlyingLogReturn,PrevVolatility,Volatity,AnnualVolatality
        "02-27-2025",RELIANCE,,1207.1,1204,0.0026,0.0134,0.0133,0.2541
            </Tab>
            <Tab title="CsvContent">
              Date,ScripId,ScripName,Close,PrevClose,UnderlyingLogReturn,PrevVolatility,Volatity,AnnualVolatality
        "02-27-2025",RELIANCE,,1207.1,1204,0.0026,0.0134,0.0133,0.2541

              Note: 
              1. The file containing above values in CSV format will be returned. 
              2. To see it working, copy-paste the request in browser.
            </Tab>
          </Tabs>
        </div>
      tags:
        - EOD Statistics
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
          description: 'Example: NSE'
          required: true
          example: NSE
          schema:
            type: string
        - name: instrumentIdentifier
          in: query
          description: 'Example: RELIANCE'
          required: false
          example: RELIANCE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp in seconds
          required: true
          example: '1740594600'
          schema:
            type: string
        - name: dTformat
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example: string
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-17039702-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetStatsMCap

> Source: https://docs.globaldatafeeds.in/getstatsmcap-17991704e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetStatsMCap:
    get:
      summary: GetStatsMCap
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetStatsMCap** is a function typically used in financial or trading
        systems to retrieve the Market Capitalization (MCap) data for all
        instruments listed on a specific exchange for a given trading day.


        ### What is returned?


        Date, MarketCapVal, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
          "Value": [
            {
              "Date": "01-23-2026",
              "ScripId": "RELIANCE",
              "ScripName": "Reliance Industries Ltd",
              "Series": "A",
              "LastTradeDate": "01-23-2026",
              "FaceValue": 0,
              "NoOfShares": 135324.72634,
              "Close": 1386.1,
              "Mcap": 1875533.04
            }
          ]
        }
                    </Tab>
                    <Tab title="XML">
                        &lt;MCap&gt;
        &lt;Value&gt;

        &lt;MCapItem Date="01-23-2026" ScripId="RELIANCE" ScripName="Reliance
        Industries Ltd" Series="A" LastTradeDate="01-23-2026" FaceValue="0"
        NoOfShares="135324.72634" Close="1386.1" Mcap="1875533.04"/&gt;

        &lt;/Value&gt;

        &lt;/MCap&gt;
                    </Tab>
                    <Tab title="CSV">
                    Date,ScripId,ScripName,LastTradeDate,FaceValue,Close,Series,NoOfShares,Mcap
        "01-23-2026",RELIANCE,Reliance Industries
        Ltd,"01-23-2026",0,1386.1,A,135324.72634,1875533.04
                    </Tab>
                    <Tab title="CsvContent">
                        Date,ScripId,ScripName,LastTradeDate,FaceValue,Close,Series,NoOfShares,Mcap
        "01-23-2026",RELIANCE,Reliance Industries
        Ltd,"01-23-2026",0,1386.1,A,135324.72634,1875533.04

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - EOD Statistics
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
          description: 'Example: BSE / NSE'
          required: true
          example: NSE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp Date
          required: true
          example: '1749752999'
          schema:
            type: string
        - name: instrumentIdentifier
          in: query
          description: 'Example: RELIANCE'
          required: false
          example: RELIANCE
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-17991704-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetNewHL

> Source: https://docs.globaldatafeeds.in/getnewhl-18721671e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetNewHL:
    get:
      summary: GetNewHL
      deprecated: false
      description: "<div style=\"display: flex; align-items: center; justify-content: space-between;\">\n  <div>\n    >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            \n  </div>\n  <img src=\"https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview\" alt=\"pointer.gif\" width=\"80\" />\n</div>\n\nThe **GetNewHL** function provides information about stocks that have hit a new High (H) or new Low (L) price compared to the previous day's price band. This is often used to track significant price movements in the market\n\n### What is returned?\n\nDate, ScripName, New, Prev, NewBand\n\n\nFor details, please see [glossary](/glossary-923501m0)\n\n### **Sample Response**\n\n<div style=\"border: 1px solid #ccc; background-color: #f9f9f9; padding: 10px; border-radius: 5px;\">\n    <Tabs style=\"background-color:lightgrey;\">\n            <Tab title=\"JSON\">\n                {\n  \"Value\": [\n    {\n      \"Date\": \"02-27-2025\",\n      \"ScripName\": \"GOI TBILL 91D-17/04/25\",\n      \"New\": 99.09,\n      \"Prev\": 99.06,\n      \"NewBand\": \"H\"\n    },\n    {\n      \"Date\": \"02-27-2025\",\n      \"ScripName\": \"TAX FREE7.60% SR.IIB\",\n      \"New\": 1164.77,\n      \"Prev\": 1163,\n      \"NewBand\": \"H\"\n    },\n    {\n      \"Date\": \"02-27-2025\",\n      \"ScripName\": \"SEC RE NCD 9.9% SR IV\",\n      \"New\": 1048.1,\n      \"Prev\": 1032,\n      \"NewBand\": \"H\"\n    }\n\t}]\n            </Tab>\n            <Tab title=\"XML\">\n   &lt;NewHL&gt;\n    &lt;Value&gt;\n      &lt;NewHLItem Date=\"02-27-2025\" ScripName=\"GOI TBILL 91D-17/04/25\" New=\"99.09\" Prev=\"99.06\" NewBand=\"H\" &gt;\n      &lt;NewHLItem Date=\"02-27-2025\" ScripName=\"TAX FREE7.60% SR.IIB\" New=\"1164.77\" Prev=\"1163\" NewBand=\"H\" &gt;\n      &lt;NewHLItem Date=\"02-27-2025\" ScripName=\"SEC RE NCD 9.9% SR IV\" New=\"1048.1\" Prev=\"1032\" NewBand=\"H\" &gt;\n    &lt;Value&gt;\n&lt;NewHL&gt;\n            </Tab>\n            <Tab title=\"CSV\">\n            Date,ScripName,New,Prev,NewBand\n\"02-27-2025\",GOI TBILL 91D-17/04/25,99.09,99.06,H\n\"02-27-2025\",TAX FREE7.60% SR.IIB,1164.77,1163,H\n\"02-27-2025\",SEC RE NCD 9.9% SR IV,1048.1,1032,H\n            </Tab>\n            <Tab title=\"CsvContent\">\n                Date,ScripName,New,Prev,NewBand\n\"02-27-2025\",GOI TBILL 91D-17/04/25,99.09,99.06,H\n\"02-27-2025\",TAX FREE7.60% SR.IIB,1164.77,1163,H\n\"02-27-2025\",SEC RE NCD 9.9% SR IV,1048.1,1032,H\n\n                Note: \n                1. The file containing above values in CSV format will be returned. \n                2. To see it working, copy-paste the request in browser.\n            </Tab>\n    </Tabs>\n  </div>"
      tags:
        - EOD Statistics
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
          description: Example:NSE
          required: true
          example: NSE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp Date
          required: true
          example: '1749752999'
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-18721671-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetCircuitBreakers

> Source: https://docs.globaldatafeeds.in/getcircuitbreakers-19111099e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCircuitBreakers:
    get:
      summary: GetCircuitBreakers
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **Circuit breakers** function gives the list of symbols with its
        series that have halt or limit trading when prices move beyond
        predefined thresholds. Their purpose is to prevent excessive volatility,
        protect investors, and maintain orderly market functioning.


        ### What is returned?


        Date, ScripId, ScripName, Series, Band


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
          "Value": [
            {
              "Date": "07-03-2025",
              "ScripId": "21STCENMGM",
              "ScripName": "21ST CENTURY MGMT SERVICE",
              "Series": "EQ",
              "Band": "H"
            },
            {
              "Date": "07-03-2025",
              "ScripId": "AARTECH",
              "ScripName": "AARTECH SOLONICS LIMITED",
              "Series": "EQ",
              "Band": "L"
            },
            {
              "Date": "07-03-2025",
              "ScripId": "ABHAPOWER.ST",
              "ScripName": "ABHA POWER N STEEL LTD",
              "Series": "ST",
              "Band": "H"
            },
            {
              "Date": "07-03-2025",
              "ScripId": "ABMINTLLTD.BE",
              "ScripName": "ABM INTERNATIONAL LTD",
              "Series": "BE",
              "Band": "L"
            }]
        }
                    </Tab>
                    <Tab title="XML">
           &lt;CircuitBreakers&gt;
        &lt;Value&gt;

        &lt;CircuitBreakersItem Date="07-03-2025" ScripId="21STCENMGM"
        ScripName="21ST CENTURY MGMT SERVICE" Series="EQ" Band="H"&gt;

        &lt;CircuitBreakersItem Date="07-03-2025" ScripId="AARTECH"
        ScripName="AARTECH SOLONICS LIMITED" Series="EQ" Band="L"&gt;

        &lt;CircuitBreakersItem Date="07-03-2025" ScripId="ABHAPOWER.ST"
        ScripName="ABHA POWER N STEEL LTD" Series="ST" Band="H"&gt;

        &lt;CircuitBreakersItem Date="07-03-2025" ScripId="ABMINTLLTD.BE"
        ScripName="ABM INTERNATIONAL LTD" Series="BE" Band="L"&gt;

        &lt;Value&gt;

        &lt;CircuitBreakers&gt;
                    </Tab>
                    <Tab title="CSV">
                    Date,ScripId,ScripName,Series,Band
        "07-03-2025",21STCENMGM,21ST CENTURY MGMT SERVICE,EQ,H

        "07-03-2025",AARTECH,AARTECH SOLONICS LIMITED,EQ,L

        "07-03-2025",ABHAPOWER.ST,ABHA POWER N STEEL LTD,ST,H

        "07-03-2025",ABMINTLLTD.BE,ABM INTERNATIONAL LTD,BE,L
                    </Tab>
                    <Tab title="CsvContent">
                        Date,ScripId,ScripName,Series,Band
        "07-03-2025",21STCENMGM,21ST CENTURY MGMT SERVICE,EQ,H

        "07-03-2025",AARTECH,AARTECH SOLONICS LIMITED,EQ,L

        "07-03-2025",ABHAPOWER.ST,ABHA POWER N STEEL LTD,ST,H

        "07-03-2025",ABMINTLLTD.BE,ABM INTERNATIONAL LTD,BE,L

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - EOD Statistics
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
          description: Example:NSE
          required: true
          example: NSE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp Date
          required: true
          example: '1740594600'
          schema:
            type: string
        - name: band
          in: query
          description: ''
          required: false
          example: H
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: EOD Statistics
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-19111099-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetServerInfo

> Source: https://docs.globaldatafeeds.in/getserverinfo-15575607e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetServerInfo:
    get:
      summary: GetServerInfo
      deprecated: false
      description: >-
        **GetServerInfo** function returns information about server where
        connection is made.


        ### What is returned ?

        Server EndPoint where enduser is connected (useful while debugging
        issues)
      tags:
        - Helper APIs
        - Helper Functions
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: false
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: boolean
            default: false
        - name: format
          in: query
          description: ''
          required: false
          example: Json
          schema:
            $ref: '#/components/schemas/ResponseFormat'
      responses:
        '200':
          description: Success
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Helper APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575607-run
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetLimitation

> Source: https://docs.globaldatafeeds.in/getlimitation-15575606e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetLimitation:
    get:
      summary: GetLimitation
      deprecated: false
      description: >-
        **GetLimitation** function returns user account information (e.g. which
        functions are allowed, Exchanges allowed,etc.)


        ### What is returned ?

        Returns details about user account (what is allowed / disallowed).
      tags:
        - Helper APIs
        - Helper Functions
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: false
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: format
          in: query
          description: ''
          required: false
          example:
            - Json
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: Success
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Helper APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575606-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetInstruments

> Source: https://docs.globaldatafeeds.in/getinstruments-17992355e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetInstruments:
    get:
      summary: GetInstruments
      deprecated: false
      description: >-
        **GetInstruments** function returns array of instruments for selected
        exchange.


        ### What is returned ?

        Identifier (Symbol), Name (Instrument Type), Expiry (Expiry Date),
        StrikePrice, Product, OptionType, ProductMonth, TradeSymbol
        (ShortIdentifier for same Identifier/Symbol), QuotationLot (Lot Size),
        When detailedInfo=true, following additional information will be sent
        (if available from Exchange)TokenNumber (Token number of Symbol),
        LowPriceRange (Lower circuit limit),HighPriceRange(Upper circuit
        limit),ISIN code, Series, High52Week, Low52Week.


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
        {
          "INSTRUMENTS": [
            {
              "EXCHANGE": "BSE",
              "EXPIRY": "",
              "IDENTIFIER": "RELIANCE",
              "INDEXNAME": "",
              "NAME": "RELIANCE",
              "OPTIONTYPE": "",
              "PRICEQUOTATIONUNIT": "",
              "PRODUCT": "RELIANCE",
              "PRODUCTMONTH": "",
              "STRIKEPRICE": 0,
              "TRADESYMBOL": "RELIANCE",
              "UNDERLYINGASSET": "",
              "UNDERLYINGASSETEXPIRY": "",
              "QUOTATIONLOT": 1,
              "DESCRIPTION": "RELIANCE INDUSTRIES LTD.",
              "TOKENNUMBER": "500325",
              "LOWPRICERANGE": "1284.9",
              "HIGHPRICERANGE": "1570.4",
              "ISIN": "INE002A01018",
              "52WeekHigh": "1608.95",
              "52WeekLow": "1115.55",
              "SERIES": "A"
            }
          ]
        }
                    </Tab>
                    <Tab title="XML">
                        &lt;Value Exchange="BSE" Identifier="RELIANCE" Name="RELIANCE" Expiry="" StrikePrice="0" Product="RELIANCE" PriceQuotationUnit="" OptionType="" ProductMonth="" UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName="" TradeSymbol="RELIANCE" QuotationLot="1" Description="RELIANCE INDUSTRIES LTD." TokenNumber="500325" LowPriceRange="1427.65" HighPriceRange="1713.15" IsActive="true" ISIN="INE002A01018" Series="A" High52Week="1608.95" Low52Week="1115.55"/&gt;
                    </Tab>
                    <Tab title="CSV">
                    Exchange,Expiry,Identifier,IndexName,Name,OptionType,PriceQuotationUnit,Product,ProductMonth,StrikePrice,TradeSymbol,UnderlyingAsset,UnderlyingAssetExpiry,QuotationLot,Description,TokenNumber,LowPriceRange,HighPriceRange,ISIN,52WeekHigh,52WeekLow,Series
        BSE,,RELIANCE,,RELIANCE,,,RELIANCE,,0.0,RELIANCE,,,1.0,RELIANCE
        INDUSTRIES LTD.,500325,1427.65,1713.15,INE002A01018,1608.95,1115.55,A
                    </Tab>
                    <Tab title="CsvContent">
                        Exchange,Expiry,Identifier,IndexName,Name,OptionType,PriceQuotationUnit,Product,ProductMonth,StrikePrice,TradeSymbol,UnderlyingAsset,UnderlyingAssetExpiry,QuotationLot,Description,TokenNumber,LowPriceRange,HighPriceRange,ISIN,52WeekHigh,52WeekLow,Series
        BSE,,RELIANCE,,RELIANCE,,,RELIANCE,,0.0,RELIANCE,,,1.0,RELIANCE
        INDUSTRIES LTD.,500325,1427.65,1713.15,INE002A01018,1608.95,1115.55,A

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - Helper APIs
      parameters:
        - name: accessKey
          in: query
          description: Please use API key provided by GlobalDatafeeds.
          required: false
          example: 30bd31ff-fb7e-4d6c-a76e-06750e3eeb09
          schema:
            type: string
        - name: exchange
          in: query
          description: 'Example: BSE / NSE'
          required: false
          example: BSE
          schema:
            type: string
        - name: product
          in: query
          description: 'Example : RELIANCE , TCS'
          required: false
          example: RELIANCE
          schema:
            type: string
        - name: series
          in: query
          description: 'Example : EQ, BE'
          required: false
          example: A
          schema:
            type: string
        - name: onlyActive
          in: query
          description: >-
            By default, function will return only active instruments. Function
            will return all (active + expired) instruments if value equals
            false.
          required: false
          example: 'false'
          schema:
            type: string
        - name: detailedInfo
          in: query
          description: >-
            By default function will return limited fields in response, function
            will return additional fields in response when this parameter is set
            as true.
          required: false
          example: 'true'
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
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Helper APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-17992355-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
