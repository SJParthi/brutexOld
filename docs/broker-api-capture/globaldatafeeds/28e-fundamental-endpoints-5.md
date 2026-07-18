# GlobalDataFeeds — Fundamental Data API — Endpoints 5 (Other Data APIs: Index Constituents, EOD Stats, BhavCopy, F&O, Index Detail, Circuit Filter)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. Index Constituents (offline data product) — /index-constituents-933202m0
2. GetEODStats (marked deprecated in spec) — /geteodstats-16005369e0
3. GetBhavCopyCM — /getbhavcopycm-15575591e0
4. GetBhavCopyFO — /getbhavcopyfo-15575592e0
5. GetFuturesAndOptions — /getfuturesandoptions-15575593e0
6. GetIndexDetail — /getindexdetail-15575594e0
7. GetCircuitFilterDetails — /getcircuitfilterdetails-15575595e0

---

## Index Constituents

> Source: https://docs.globaldatafeeds.in/index-constituents-933202m0.md — captured 2026-07-15

Index Constituents Data is the list of stocks that make up a specific stock market index on the NSE or BSE, like NIFTY 50 or SENSEX. It shows which companies are included in the index.

We offer index constituents of NSE and BSE as per details given in the table below :

| Description | Details |
| --- | --- |
| Indices Covered | NSE & BSE |
| Count as on April 2025 | NSE (109), BSE (70) |
| Format | Comma Separated Values (CSV) |
| Mode Of Delivery | Offline (over email) |
| Update Frequency | Entire dump - once a month (12 deliveries in a year) |
| Fields included | Identifier, CompanyName,Industry, TokenNumber, ISIN |
| Sample Data |[Nifty 100.csv](https://www.dropbox.com/scl/fi/hv8blxg891hs78ts4ewth/Nifty-100.csv?rlkey=8kxfdet9t1dh6nnixwmxg5kjh&st=x9hwb08u&dl=0), [BSE 100.csv](https://www.dropbox.com/scl/fi/uwa38j0rsg4rfrzz6zeby/BSE-100.csv?rlkey=cw5smzhdwu4sefmcxze7ikvbq&st=0ats8xg6&dl=0)
| Subscription Period | Annual |

:::highlight green 💡
Interested ? Please contact our [Sales Team](https://globaldatafeeds.in/contact-us/#contact-sales-retail) for more details and subscription
:::

---

## GetEODStats

> Source: https://docs.globaldatafeeds.in/geteodstats-16005369e0.md — captured 2026-07-15
> Note: `deprecated: true` / `x-apidog-status: deprecated` in the published spec.

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetEODStats:
    get:
      summary: GetEODStats
      deprecated: true
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **GetEODStats** function is used to retrieve End-Of-Day (EOD)
        statistics for securities listed on a specified exchange for a specific
        date. This API provides comprehensive daily trading data for market
        analysis and reporting purposes.




        ## What is returned?


        Date, ScripId, ScripName, Open, High, Low, Close, PrevClose,
        LastTradedPrice, NoOfTrades,Turnover, DelQty, DelQtyPer, Series,
        PrevSeries, ToSeries, Band, AverageTradedPrice,TradedQty,
        UnderlyingLogReturn, PrevVolatility, Volatity, AnnualVolatality,
        NoOfShares, Mcap


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        | Response Type | Sample
        Response                                                                                        
        |

        | :------------ |
        :------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GetEODStatsResponse_Json.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GetEODStatsResponse_xml.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GetEodStatsresponse_csv.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned.                                              |
      tags:
        - Other Data APIs
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
          description: 'Example: NSE'
          required: false
          example: NSE
          schema:
            type: string
        - name: date
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: '1744309800'
          schema:
            type: string
        - name: InstrumentIdentifier
          in: query
          description: 'Example: RELIANCE'
          required: false
          example: RELIANCE
          schema:
            type: string
        - name: band
          in: query
          description: 'Example:  H (High band)'
          required: false
          example: H
          schema:
            type: string
        - name: series
          in: query
          description: 'Example: EQ '
          required: false
          example: EQ
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
      x-apidog-folder: Other Data APIs
      x-apidog-status: deprecated
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-16005369-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetBhavCopyCM

> Source: https://docs.globaldatafeeds.in/getbhavcopycm-15575591e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBhavCopyCM:
    get:
      summary: GetBhavCopyCM
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetBhavCopyCM** returns Bhavcopy for the Cash Market (CM) segment,
        providing trading data such as open, high, low, close (OHLC) prices,
        traded volume, turnover, and security-wise statistics. This data helps
        investors and analysts track stock performance and market trends.




        ### What is returned?


        InstrumentName, TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId,
        ISIN, Symbol, SctySrs, Expiry, FinInstrmActlXpryDt, StrikePrice,
        OptionType, FinInstrmNm, OpenPrice, HighPrice, LowPrice, ClosePrice,
        LastPrice, PrevClosePrice, UnderlyingPrice, SttlmPric, OI, OiChng, TTQ,
        TtlTrfVal, TotalTradesNo, SsnId, NewBrdLotQty, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ## Sample Response


        | Response Type | Sample
        Response                                                                                   
        |

        | :------------ |
        :-------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyCMResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyCMResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyCMResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                          |
      tags:
        - Other Data APIs
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
          example: RELIANCE+ABB
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1736978400
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1737064800
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
          description: ''
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
                $ref: '#/components/schemas/QeBhavCopyCMResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575591-run
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
    QeBhavCopyCMResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeBhavCopyCM'
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
    QeBhavCopyCM:
      type: object
      properties:
        instrumentName:
          type: string
          nullable: true
        tradDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        bizDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        sgmt:
          type: string
          nullable: true
        src:
          type: string
          nullable: true
        finInstrmTp:
          type: string
          nullable: true
        finInstrmId:
          type: integer
          format: int64
        isin:
          type: string
          nullable: true
        symbol:
          type: string
          nullable: true
        sctySrs:
          type: string
          nullable: true
        expiry:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        finInstrmActlXpryDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        strikePrice:
          type: number
          format: double
        optionType:
          type: string
          nullable: true
        finInstrmNm:
          type: string
          nullable: true
        openPrice:
          type: number
          format: double
        highPrice:
          type: number
          format: double
        lowPrice:
          type: number
          format: double
        closePrice:
          type: number
          format: double
        lastPrice:
          type: number
          format: double
        prevClosePrice:
          type: number
          format: double
        underlyingPrice:
          type: number
          format: double
        sttlmPric:
          type: number
          format: double
        oi:
          type: integer
          format: int64
        oiChng:
          type: integer
          format: int64
        ttq:
          type: integer
          format: int64
        ttlTrfVal:
          type: number
          format: double
        totalTradesNo:
          type: integer
          format: int64
        ssnId:
          type: string
          nullable: true
        newBrdLotQty:
          type: integer
          format: int64
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
        - instrumentName
        - tradDt
        - bizDt
        - sgmt
        - src
        - finInstrmTp
        - finInstrmId
        - isin
        - symbol
        - sctySrs
        - expiry
        - finInstrmActlXpryDt
        - strikePrice
        - optionType
        - finInstrmNm
        - openPrice
        - highPrice
        - lowPrice
        - closePrice
        - lastPrice
        - prevClosePrice
        - underlyingPrice
        - sttlmPric
        - oi
        - oiChng
        - ttq
        - ttlTrfVal
        - totalTradesNo
        - ssnId
        - newBrdLotQty
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

## GetBhavCopyFO

> Source: https://docs.globaldatafeeds.in/getbhavcopyfo-15575592e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBhavCopyFO:
    get:
      summary: GetBhavCopyFO
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetBhavCopyFO** returns Bhavcopy for the Futures & Options (FO)
        segment, including details such as open, high, low, close (OHLC) prices,
        traded volume, open interest, and turnover for derivative contracts.
        This data helps traders and analysts evaluate market trends, derivative
        pricing, and trading activity.




        ### What is returned?


        Identifier, InstrumentName, TradDt, BizDt, Sgmt, Src, FinInstrmTp,
        FinInstrmId, ISIN, Symbol, SctySrs, Expiry, FinInstrmActlXpryDt,
        StrikePrice, OptionType, FinInstrmNm, OpenPrice, HighPrice, LowPrice,
        ClosePrice, LastPrice, PrevClosePrice, UnderlyingPrice, SttlmPric, OI,
        OiChng, TTQ, TtlTrfVal, TotalTradesNo, SsnId, NewBrdLotQty, ReceivedAt,
        SavedAt


        For details, please see [glossary](/glossary-923501m0)




        ### Sample Response


        | Response Type | Sample
        Response                                                                                   
        |

        | :------------ |
        :-------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyFOResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyFOResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetBhavCopyFOResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                          |


        For details, please see
        [glossary](/reference/glossary#request-parameters)
      tags:
        - Other Data APIs
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
          description: 'Example: BFO'
          required: true
          example: BFO
          schema:
            type: string
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: IF_SENSEX_25MAR2025_FF_0+IF_BANKEX_25MAR2025_FF_0
            max. limit is 25 instruments per single request
          required: false
          example: IF_SENSEX_25MAR2025_FF_0+IF_BANKEX_25MAR2025_FF_0
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1736978400
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1737064800
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
          description: ''
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
                $ref: '#/components/schemas/QeBhavCopyFOResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575592-run
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
    QeBhavCopyFOResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeBhavCopyFO'
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
    QeBhavCopyFO:
      type: object
      properties:
        instrumentName:
          type: string
          nullable: true
        tradDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        bizDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        sgmt:
          type: string
          nullable: true
        src:
          type: string
          nullable: true
        finInstrmTp:
          type: string
          nullable: true
        finInstrmId:
          type: integer
          format: int64
        isin:
          type: string
          nullable: true
        symbol:
          type: string
          nullable: true
        sctySrs:
          type: string
          nullable: true
        expiry:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        finInstrmActlXpryDt:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        strikePrice:
          type: number
          format: double
        optionType:
          type: string
          nullable: true
        finInstrmNm:
          type: string
          nullable: true
        openPrice:
          type: number
          format: double
        highPrice:
          type: number
          format: double
        lowPrice:
          type: number
          format: double
        closePrice:
          type: number
          format: double
        lastPrice:
          type: number
          format: double
        prevClosePrice:
          type: number
          format: double
        underlyingPrice:
          type: number
          format: double
        sttlmPric:
          type: number
          format: double
        oi:
          type: integer
          format: int64
        oiChng:
          type: integer
          format: int64
        ttq:
          type: integer
          format: int64
        ttlTrfVal:
          type: number
          format: double
        totalTradesNo:
          type: integer
          format: int64
        ssnId:
          type: string
          nullable: true
        newBrdLotQty:
          type: integer
          format: int64
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
        - instrumentName
        - tradDt
        - bizDt
        - sgmt
        - src
        - finInstrmTp
        - finInstrmId
        - isin
        - symbol
        - sctySrs
        - expiry
        - finInstrmActlXpryDt
        - strikePrice
        - optionType
        - finInstrmNm
        - openPrice
        - highPrice
        - lowPrice
        - closePrice
        - lastPrice
        - prevClosePrice
        - underlyingPrice
        - sttlmPric
        - oi
        - oiChng
        - ttq
        - ttlTrfVal
        - totalTradesNo
        - ssnId
        - newBrdLotQty
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

## GetFuturesAndOptions

> Source: https://docs.globaldatafeeds.in/getfuturesandoptions-15575593e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetFuturesAndOptions:
    get:
      summary: GetFuturesAndOptions
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetFuturesAndOptions** returns Bhavcopy for the Futures & Options (FO)
        segment, including details such as open, high, low, close (OHLC) prices,
        traded volume, open interest, and turnover for derivative contracts.
        This data helps traders and analysts evaluate market trends, derivative
        pricing, and trading activity.




        ### What is returned?


        Identifier, InstrumentName, Symbol, OptionType, StrikePrice, ExpiryDate,
        Date, ProductId, ProductType, SeriesId, SeriesCode, Ltp, Ltq, High, Low,
        CumTrdVol, CumTrdVal, TotalTradesNo, Close, PrevClose, SettPrice,
        BasePrice, BaseChange, OiQty, OiChng, ContractsNo, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)




        ### Sample Response


        | Response Type | Sample
        Response                                                                                          
        |

        | :------------ |
        :--------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetFuturesAndOptionsResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetFuturesAndOptionsResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetFuturesAndOptionsResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                                 |


        For details, please see
        [glossary](/reference/glossary#request-parameters)
      tags:
        - Other Data APIs
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
          description: 'Example: BFO'
          required: true
          example: BFO
          schema:
            type: string
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: IF_SENSEX_25MAR2025_FF_0+IF_BANKEX_25MAR2025_FF_0
            max. limit is 25 instruments per single request
          required: false
          example: IF_SENSEX_25MAR2025_FF_0+IF_BANKEX_25MAR2025_FF_0
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1736978400
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1737064800
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
          description: ''
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
                $ref: '#/components/schemas/QeEquityDerivativesMsResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575593-run
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
    QeEquityDerivativesMsResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeEquityDerivativesMs'
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
    QeEquityDerivativesMs:
      type: object
      properties:
        instrumentName:
          type: string
          nullable: true
        symbol:
          type: string
          nullable: true
        marketSummaryDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        sessionID:
          type: string
          nullable: true
        seriesID:
          type: integer
          format: int32
        seriesCode:
          type: string
          nullable: true
        productType:
          type: string
          nullable: true
        productCode:
          type: string
          nullable: true
        assetCode:
          type: string
          nullable: true
        expiry:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        strikePrice:
          type: number
          format: double
        optionType:
          type: string
          nullable: true
        previousClosePrice:
          type: number
          format: double
        openPrice:
          type: number
          format: double
        highPrice:
          type: number
          format: double
        lowPrice:
          type: number
          format: double
        closePrice:
          type: number
          format: double
        totalTradedQuantity:
          type: integer
          format: int64
        totalTradedValue:
          type: number
          format: double
        averageTradedPrice:
          type: number
          format: double
        tradesNo:
          type: integer
          format: int64
        openInterest:
          type: number
          format: double
        underlyingAssetClosePrice:
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
        - instrumentName
        - symbol
        - marketSummaryDate
        - sessionID
        - seriesID
        - seriesCode
        - productType
        - productCode
        - assetCode
        - expiry
        - strikePrice
        - optionType
        - previousClosePrice
        - openPrice
        - highPrice
        - lowPrice
        - closePrice
        - totalTradedQuantity
        - totalTradedValue
        - averageTradedPrice
        - tradesNo
        - openInterest
        - underlyingAssetClosePrice
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

## GetIndexDetail

> Source: https://docs.globaldatafeeds.in/getindexdetail-15575594e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetIndexDetail:
    get:
      summary: GetIndexDetail
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetIndexDetail** returns Bhavcopy of stock market indices, providing
        data such as index value, open, high, low, close (OHLC) levels, traded
        volume, and turnover. This information helps investors and analysts
        track index performance, market trends, and overall economic sentiment.




        ## What is returned?


        Date, IndexCode, IndexName, Opening, High, Low, Closing, Change,
        PerChange, LastDay, WeekAgo, MonthAgo, YearAgo, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)




        ## Sample Response


        | Response Type | Sample
        Response                                           |

        | :------------ |
        :-------------------------------------------------------- |

        | JSON          | [Download JSON
        Response]()                                |

        | XML           | [Download XML
        Response]()                                 |

        | CSV           | [Download CSV
        Response]()                                 |

        | CSVContent    | The file containing values in CSV format will be
        returned |


        For details, please see
        [glossary](/reference/glossary#request-parameters)
      tags:
        - Other Data APIs
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
          description: 'Example: BSE_IDX'
          required: true
          example: BSE_IDX
          schema:
            type: string
        - name: instrumentIdentifiers
          in: query
          description: |-
            Example: BSE 100+BSE 200
            max. limit is 25 instruments per single request
          required: false
          example: BSE 100+BSE 200
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1736978400
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1737064800
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
          description: ''
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
                $ref: '#/components/schemas/QeIndexDetailResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575594-run
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
    QeIndexDetailResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeIndexDetail'
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
    QeIndexDetail:
      type: object
      properties:
        date:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        indexCode:
          type: string
          nullable: true
        indexName:
          type: string
          nullable: true
        opening:
          type: number
          format: double
        high:
          type: number
          format: double
        low:
          type: number
          format: double
        closing:
          type: number
          format: double
        change:
          type: number
          format: double
        perChange:
          type: number
          format: double
        lastDay:
          type: number
          format: double
        weekAgo:
          type: number
          format: double
        monthAgo:
          type: number
          format: double
        yearAgo:
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
        - indexCode
        - indexName
        - opening
        - high
        - low
        - closing
        - change
        - perChange
        - lastDay
        - weekAgo
        - monthAgo
        - yearAgo
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

## GetCircuitFilterDetails

> Source: https://docs.globaldatafeeds.in/getcircuitfilterdetails-15575595e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCircuitFilterDetails:
    get:
      summary: GetCircuitFilterDetails
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetCircuitFilterDetails** returns the list of companies who have hit
        the circuit filter limits (upper and lower) as set by the exchange. This
        information helps investors and traders understand price movement
        restrictions, manage risk, and analyze market volatility trends.




        ### What is returned?


        InstrumentName, Product, Expiry, StrikePrice, OptionType, ScripCode,
        ScripName, CktFlag, Date, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)



        ### Sample Response


        | Response Type | Sample
        Response                                                                                             
        |

        | :------------ |
        :-----------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetCircuitFilterDetailsResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetCircuitFilterDetailsResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetCircuitFilterDetailsResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                                    |


        For details, please see
        [glossary](/reference/glossary#request-parameters)
      tags:
        - Other Data APIs
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
          example: WOCKPHARMA
          schema:
            type: string
        - name: from
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1736978400
          schema:
            type: integer
            format: int32
            nullable: true
        - name: to
          in: query
          description: Unix Time Stamp in seconds
          required: false
          example: 1737064800
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
          description: ''
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
                $ref: '#/components/schemas/QeCircuitFilterDetailsResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575595-run
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
    QeCircuitFilterDetailsResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeCircuitFilterDetails'
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
    QeCircuitFilterDetails:
      type: object
      properties:
        instrumentName:
          type: string
          nullable: true
        product:
          type: string
          nullable: true
        expiry:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        strikePrice:
          type: number
          format: double
        optionType:
          type: string
          nullable: true
        scripCode:
          type: integer
          format: int32
        scripName:
          type: string
          nullable: true
        cktFlag:
          type: string
          nullable: true
        date:
          type: string
          examples:
            - 02-25-2025
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
        - instrumentName
        - product
        - expiry
        - strikePrice
        - optionType
        - scripCode
        - scripName
        - cktFlag
        - date
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
