# GlobalDataFeeds — Fundamental Data API — Endpoints 3 (Share Holding Patterns, Market Capitalization, Annual Reports)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetShpItems — /getshpitems-15575577e0
2. GetSHP — /getshp-15575578e0
3. GetSHPAdvanced — /getshpadvanced-23196898e0
4. GetScripMCap — /getscripmcap-15575589e0
5. GetExchangeMCap — /getexchangemcap-17991613e0
6. GetAnnualReports — /getannualreports-31420321e0

Not retrievable (recorded in 28z): /getannualreports-15575602e0 (older URL for the same endpoint; returns empty body — superseded by /getannualreports-31420321e0 captured below).

---

## GetShpItems

> Source: https://docs.globaldatafeeds.in/getshpitems-15575577e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetShpItems:
    get:
      summary: GetShpItems
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetSHPItems** returns a list of companies along with the respective
        dates on which their Shareholding Pattern (SHP) has been reported. These
        details are then further used in next function (GetSHP) to get actual
        Share Holding Pattern.This function helps investors track ownership
        structures, including promoter holdings, institutional investors, and
        public shareholding over time.




        ### What is returned?


        Date,Instrument


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
            {
              "Value": [
                {
                  "Date": 1740594600,
                  "Instrument": "ARL"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "ARYAVAN"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "ASHIS"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "AVROIND"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "AWL"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "BNHOLDINGS"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "DSKULKARNI"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "RETAIL"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "SUDTIND-B"
                },
                {
                  "Date": 1740594600,
                  "Instrument": "UGARSUGAR"
                }
              ]
            }
              </Tab>
              <Tab title="XML">
            &lt;?xml version="1.0" encoding="utf-8"?&gt;&lt;DateSymbolQueryResponse&gt;&lt;Value&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="ARL" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="ARYAVAN" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="ASHIS" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="AVROIND" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="AWL" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="BNHOLDINGS" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="DSKULKARNI" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="RETAIL" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="SUDTIND-B" /&gt;&lt;DateSymbolQueryItem Date="1740594600000" Instrument="UGARSUGAR" /&gt;&lt;/Value&gt;&lt;/DateSymbolQueryResponse&gt;
              </Tab>
              <Tab title="CSV">
            Date,Instrument
            1740594600,ARL
            1740594600,ARYAVAN
            1740594600,ASHIS
            1740594600,AVROIND
            1740594600,AWL
            1740594600,BNHOLDINGS
            1740594600,DSKULKARNI
            1740594600,RETAIL
            1740594600,SUDTIND-B
            1740594600,UGARSUGAR
              </Tab>
              <Tab title="CsvContent">
            Date,Instrument
            1740594600,ARL
            1740594600,ARYAVAN
            1740594600,ASHIS
            1740594600,AVROIND
            1740594600,AWL
            1740594600,BNHOLDINGS
            1740594600,DSKULKARNI
            1740594600,RETAIL
            1740594600,SUDTIND-B
            1740594600,UGARSUGAR
            
            Note: 
            1. The file containing above values in CSV format will be returned. 
            2. To see it working, copy-paste the request in browser.
              </Tab>
            </Tabs>
        </div>
      tags:
        - Corporate Data APIs (RESTful)/Share Holding Patterns
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
        - name: from
          in: query
          description: >-
            Unix Time Stamp in seconds, dif limit 5days for empty
            instrumentIdentifiers, and 30 for filled
          required: true
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
        - name: to
          in: query
          description: >-
            Unix Time Stamp in seconds, dif limit 5days for empty
            instrumentIdentifiers, and 30 for filled
          required: true
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: RELIANCE+ABB
            max. limit is 25 instruments per single request
          required: false
          example: ASHIS+ARL
          schema:
            type: string
        - name: period
          in: query
          description: ''
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
                $ref: '#/components/schemas/DateSymbolQueryResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Share Holding Patterns
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575577-run
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
    DateSymbolQueryResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/DateSymbolQueryItem'
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
    DateSymbolQueryItem:
      type: object
      properties:
        date:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        instrument:
          type: string
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - date
        - instrument
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetSHP

> Source: https://docs.globaldatafeeds.in/getshp-15575578e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetSHP:
    get:
      summary: GetSHP
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetSHP** returns Shareholding Pattern (SHP) information, which details
        the distribution of a company’s shares among its shareholders. This
        includes raw information on institutional investors, promoters, public
        shareholders, and any significant changes in ownership over a specific
        period. It helps investors assess control, voting power, and potential
        risks or opportunities related to the company’s ownership structure.

        To use this function, you will need the name of the company which you
        can obtain from [GetSHPItems](/getshpitems-15575577e0) function.




        ### What is returned?


        Label, UnitId, Decimals, Value, Description, ContextName, From, To


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        | Response Type | Sample
        Response                                                                            
        |

        | :------------ |
        :------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetSHPResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetSHPResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetSHPResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned.                                  |
      tags:
        - Corporate Data APIs (RESTful)/Share Holding Patterns
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
          example: ARL
          schema:
            type: string
            default: RELIANCE
        - name: period
          in: query
          description: ''
          required: false
          example: February 2025
          schema:
            type: string
        - name: date
          in: query
          description: ''
          required: false
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
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
                $ref: '#/components/schemas/ShpFactsResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Share Holding Patterns
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575578-run
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
    ShpFactsResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/GenericXbrlFact'
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
    GenericXbrlFact:
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
        contextName:
          type: string
          nullable: true
        from:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        to:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - label
        - unitId
        - decimals
        - value
        - description
        - contextName
        - from
        - to
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetSHPAdvanced

> Source: https://docs.globaldatafeeds.in/getshpadvanced-23196898e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetSHPAdvanced:
    get:
      summary: GetSHPAdvanced
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetSHPAdvanced** API provides detailed shareholding pattern data for a
        specified stock/instrument. By changing the type parameter, users can
        get specific breakdowns such as ListPromoters,
        SummaryPromoterPublicOthers, FII holdings, etc.

        To use this function, you will need the name of the company which you
        can obtain from [GetSHPItems](/getshpitems-15575577e0) function.


        ### What is returned?


        Entity, Value, Period


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
            {
          "Value": [
            {
              "Entity": "Promoter",
              "Value": 58.22,
              "Period": "February 2025"
            },
            {
              "Entity": "Public",
              "Value": 41.78,
              "Period": "February 2025"
            },
            {
              "Entity": "Others",
              "Value": 0,
              "Period": "February 2025"
            }
          ]
        }
              </Tab>
              <Tab title="XML">
            &lt;ShpAdvancedFactsResponse&gt;
            &lt;Value&gt;
              &lt;ShpAdvancedFact&gt;
                &lt;Entity&gt;Promoter&lt;/Entity&gt;
                &lt;Value&gt;58.22&lt;/Value&gt;
                &lt;Period&gt;February 2025&lt;/Period&gt;
              &lt;/ShpAdvancedFact&gt;
              &lt;ShpAdvancedFact&gt;
                &lt;Entity&gt;Public&lt;/Entity&gt;
                &lt;Value&gt;41.78&lt;/Value&gt;
                &lt;Period&gt;February 2025&lt;/Period&gt;
              &lt;/ShpAdvancedFact&gt;
              &lt;ShpAdvancedFact&gt;
                &lt;Entity&gt;Others&lt;/Entity&gt;
                &lt;Value&gt;0&lt;/Value&gt;
                &lt;Period&gt;February 2025&lt;/Period&gt;
              &lt;/ShpAdvancedFact&gt;
            &lt;/Value&gt;
          &lt;/ShpAdvancedFactsResponse&gt;
              </Tab>
              <Tab title="CSV">
            Entity,Value,Period
        Promoter,58.22,February 2025

        Public,41.78,February 2025

        Others,0,February 2025
              </Tab>
              <Tab title="CsvContent">
            Entity,Value,Period
        Promoter,58.22,February 2025

        Public,41.78,February 2025

        Others,0,February 2025

            Note: 
            1. The file containing above values in CSV format will be returned. 
            2. To see it working, copy-paste the request in browser.
              </Tab>
            </Tabs>
        </div>
      tags:
        - Corporate Data APIs (RESTful)/Share Holding Patterns
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
          example: ARL
          schema:
            type: string
            default: RELIANCE
        - name: period
          in: query
          description: ''
          required: false
          example: ''
          schema:
            type: string
        - name: type
          in: query
          description: >-
            Available values : SummaryPromoterNationality , ListPromoter,
            ListMF, SummaryDIIWithMF, SummaryDIIWithoutMF, SummaryFII,
            SummaryAll, ListMoreThanOnePercent, ListAllShareHolders
          required: false
          example: SummaryPromoterPublicOthers
          schema:
            type: string
        - name: date
          in: query
          description: ''
          required: false
          example: 1740594600
          schema:
            type: integer
            format: int32
            default: 1740594600
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
                $ref: '#/components/schemas/ShpFactsResponse'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Share Holding Patterns
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-23196898-run
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
    ShpFactsResponse:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/GenericXbrlFact'
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
    GenericXbrlFact:
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
        contextName:
          type: string
          nullable: true
        from:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
        to:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
      additionalProperties: false
      x-apidog-orders:
        - label
        - unitId
        - decimals
        - value
        - description
        - contextName
        - from
        - to
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetScripMCap

> Source: https://docs.globaldatafeeds.in/getscripmcap-15575589e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetScripMCap:
    get:
      summary: GetScripMCap
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetScripMCap** returns company's market capitalization over time,
        calculated as share price × total outstanding shares. It reflects stock
        price movements, investor sentiment, and corporate actions like buybacks
        or mergers. Investors use it to analyze valuation trends, compare with
        industry peers, and assess financial stability for informed
        decision-making.


        ### What is returned?


        Date, Symbol, ScripCode, ScripName, ISIN, MarketCap, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
                            "Value": [
                              {
                                "Date": "02-27-2025 00:00:00",
                                "Symbol": "5PAISA",
                                "ScripCode": 540776,
                                "ScripName": "5paisa Capital Limited",
                                "ISIN": "INE618L01018",
                                "MarketCap": 1107.98,
                                "ReceivedAt": "02-27-2025 21:00:08",
                                "SavedAt": "02-27-2025 21:00:09"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;MarketCapHistory&gt;
                        &lt;Value&gt;
                        &lt;MarketCapItem Date="02-27-2025 00:00:00" Symbol="5PAISA" ScripCode="540776" ScripName="5paisa Capital Limited" ISIN="INE618L01018" MarketCap="1107.98" ReceivedAt="02-27-2025 21:00:08" SavedAt="02-27-2025 21:00:09"/&gt;
                        &lt;/Value&gt;
                        &lt;/MarketCapHistory&gt;
                    </Tab>
                    <Tab title="CSV">
                        Date,Symbol,ScripCode,ScripName,ISIN,MarketCap,ReceivedAt,SavedAt
                        "02-27-2025 00:00:00",5PAISA,540776,5paisa Capital Limited,INE618L01018,1107.98,"02-27-2025 21:00:08","02-27-2025 21:00:09"
                    </Tab>
                    <Tab title="CsvContent">
                        Date,Symbol,ScripCode,ScripName,ISIN,MarketCap,ReceivedAt,SavedAt
                        "02-27-2025 00:00:00",5PAISA,540776,5paisa Capital Limited,INE618L01018,1107.98,"02-27-2025 21:00:08","02-27-2025 21:00:09"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
          
      tags:
        - Corporate Data APIs (RESTful)/Market Capitalization
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
          required: false
          example: RELIANCE
          schema:
            type: string
            default: RELIANCE
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
                $ref: '#/components/schemas/MarketCapHistory'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Market Capitalization
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575589-run
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
    MarketCapHistory:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/MarketCapItem'
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
    MarketCapItem:
      type: object
      properties:
        date:
          type: string
          examples:
            - 02-25-2025 18:00:00
          nullable: true
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
        marketCap:
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
        - date
        - symbol
        - scripCode
        - scripName
        - isin
        - marketCap
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

## GetExchangeMCap

> Source: https://docs.globaldatafeeds.in/getexchangemcap-17991613e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetExchangeMCap:
    get:
      summary: GetExchangeMCap
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetExchangeMCap**  is a function typically used in financial or
        trading systems to retrieve the Market Capitalization (MCap) data  on a
        specific exchange for a given trading day.


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
                                {
                    "Date": "06-12-2025",
                    "MarketCapVal": 44958383,
                    "ReceivedAt": "06-12-2025",
                    "SavedAt": "06-13-2025"
                }
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;DayMCap&gt;
            &lt;Value&gt;
                &lt;DayMCapItem Date="06-12-2025" MarketCapVal="44958383" ReceivedAt="06-12-2025" SavedAt="06-13-2025" /&gt;
            &lt;/Value&gt;
        &lt;/DayMCap&gt;
                    </Tab>
                    <Tab title="CSV">
                    Date,MarketCapVal,ReceivedAt,SavedAt
                    "06-12-2025",44958383,"06-12-2025","06-13-2025"
                    </Tab>
                    <Tab title="CsvContent">
                        Date,MarketCapVal,ReceivedAt,SavedAt
                    "06-12-2025",44958383,"06-12-2025","06-13-2025"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - Corporate Data APIs (RESTful)/Market Capitalization
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
                x-apidog-orders: []
          headers: {}
          x-apidog-name: Success
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Market Capitalization
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-17991613-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetAnnualReports

> Source: https://docs.globaldatafeeds.in/getannualreports-31420321e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetAnnualReports:
    get:
      summary: GetAnnualReports
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetAnnualReports** retrieves a company’s annual financial and
        operational reports, including audited financial statements, management
        discussions, and disclosures - **in PDF Format**. This data helps
        investors and analysts assess a company's overall performance,
        compliance, and long-term strategy.


        ### **What is returned?**


        Symbol, CompanyName, SecurityCode, Year, Url, FileName, ReceivedAt,
        SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
          "Value": [
            {
              "Symbol": "CARERATING",
              "CompanyName": "CARE Ratings Ltd",
              "SecurityCode": 534804,
              "Year": 2024,
              "Url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d27c8990-03a2-4fb7-9913-1adf770db0f2.pdf",
              "ReceivedAt": "03-06-2025 21:00:04",
              "SavedAt": "03-06-2025 21:00:04"
            },
            {
              "Symbol": "DICIND",
              "CompanyName": "DIC INDIA LTD.",
              "SecurityCode": 500089,
              "Year": 2024,
              "Url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d066aa9c-9f23-4c31-b294-6fbfe5e59ab4.pdf",
              "ReceivedAt": "03-03-2025 21:00:05",
              "SavedAt": "03-03-2025 21:00:07"
            },
            {
              "Symbol": "VERONICAPRO",
              "CompanyName": "Veronica Production Ltd",
              "SecurityCode": 531695,
              "Year": 2024,
              "Url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a475402-d09e-486b-bd24-a41b4152cd94.pdf",
              "ReceivedAt": "02-27-2025 21:00:05",
              "SavedAt": "02-27-2025 21:00:07"
            },
            {
              "Symbol": "WORL",
              "CompanyName": "White Organic Retail Ltd",
              "SecurityCode": 542667,
              "Year": 2024,
              "Url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a500ec6-35ae-4cec-9795-eb285f87f44a.pdf",
              "ReceivedAt": "03-06-2025 21:00:04",
              "SavedAt": "03-06-2025 21:00:04"
            }
          ]
        }
                    </Tab>
                    <Tab title="XML">
                        &lt;AnnualReports&gt;
        &lt;Value&gt;

        &lt;AnnualReportItem Symbol="CARERATING" CompanyName="CARE Ratings Ltd"
        SecurityCode="534804" Year="2024"
        Url="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d27c8990-03a2-4fb7-9913-1adf770db0f2.pdf"
        ReceivedAt="03-06-2025 21:00:04" SavedAt="03-06-2025 21:00:04"/&gt;

        &lt;AnnualReportItem Symbol="DICIND" CompanyName="DIC INDIA LTD."
        SecurityCode="500089" Year="2024"
        Url="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d066aa9c-9f23-4c31-b294-6fbfe5e59ab4.pdf"
        ReceivedAt="03-03-2025 21:00:05" SavedAt="03-03-2025 21:00:07"/&gt;

        &lt;AnnualReportItem Symbol="VERONICAPRO" CompanyName="Veronica
        Production Ltd" SecurityCode="531695" Year="2024"
        Url="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a475402-d09e-486b-bd24-a41b4152cd94.pdf"
        ReceivedAt="02-27-2025 21:00:05" SavedAt="02-27-2025 21:00:07"/&gt;

        &lt;AnnualReportItem Symbol="WORL" CompanyName="White Organic Retail
        Ltd" SecurityCode="542667" Year="2024"
        Url="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a500ec6-35ae-4cec-9795-eb285f87f44a.pdf"
        ReceivedAt="03-06-2025 21:00:04" SavedAt="03-06-2025 21:00:04"/&gt;

        &lt;/Value&gt;

        &lt;/AnnualReports&gt;
                    </Tab>
                    <Tab title="CSV">
                        Symbol,CompanyName,SecurityCode,Year,Url,FileName,ReceivedAt,SavedAt
        CARERATING,CARE Ratings
        Ltd,534804,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d27c8990-03a2-4fb7-9913-1adf770db0f2.pdf,,"03-06-2025
        21:00:04","03-06-2025 21:00:04"

        DICIND,DIC INDIA
        LTD.,500089,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d066aa9c-9f23-4c31-b294-6fbfe5e59ab4.pdf,,"03-03-2025
        21:00:05","03-03-2025 21:00:07"

        VERONICAPRO,Veronica Production
        Ltd,531695,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a475402-d09e-486b-bd24-a41b4152cd94.pdf,,"02-27-2025
        21:00:05","02-27-2025 21:00:07"

        WORL,White Organic Retail
        Ltd,542667,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a500ec6-35ae-4cec-9795-eb285f87f44a.pdf,,"03-06-2025
        21:00:04","03-06-2025 21:00:04"
                    </Tab>
                    <Tab title="CsvContent">
                        Symbol,CompanyName,SecurityCode,Year,Url,FileName,ReceivedAt,SavedAt
        CARERATING,CARE Ratings
        Ltd,534804,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d27c8990-03a2-4fb7-9913-1adf770db0f2.pdf,,"03-06-2025
        21:00:04","03-06-2025 21:00:04"

        DICIND,DIC INDIA
        LTD.,500089,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d066aa9c-9f23-4c31-b294-6fbfe5e59ab4.pdf,,"03-03-2025
        21:00:05","03-03-2025 21:00:07"

        VERONICAPRO,Veronica Production
        Ltd,531695,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a475402-d09e-486b-bd24-a41b4152cd94.pdf,,"02-27-2025
        21:00:05","02-27-2025 21:00:07"

        WORL,White Organic Retail
        Ltd,542667,2024,https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=4a500ec6-35ae-4cec-9795-eb285f87f44a.pdf,,"03-06-2025
        21:00:04","03-06-2025 21:00:04"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - Corporate Data APIs (RESTful)/Annual Reports (PDF)
        - Corporate Data (coming soon)
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
          example: TCS
          schema:
            type: string
        - name: from
          in: query
          description: Year value
          required: false
          example: 2024
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Year value
          required: false
          example: 2025
          schema:
            type: integer
            format: int32
            nullable: true
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
                $ref: '#/components/schemas/AnnualReports'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Annual Reports (PDF)
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-31420321-run
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
    AnnualReports:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/AnnualReportItem'
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
    AnnualReportItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        companyName:
          type: string
          nullable: true
        securityCode:
          type: integer
          format: int32
        year:
          type: integer
          format: int32
        url:
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
        - companyName
        - securityCode
        - year
        - url
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
