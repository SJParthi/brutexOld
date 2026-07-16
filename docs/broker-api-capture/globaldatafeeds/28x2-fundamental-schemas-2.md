# GlobalDataFeeds — Fundamental Data API — Schemas 2/3 (FinResultPeriod … QeCircuitFilterDetailsResult)

Gap-fill capture (2026-07-15) of the docs.globaldatafeeds.in schema/data-model pages indexed in `28-fundamental-data-api.md`. Each page fetched verbatim via `<url>.md`. Every schema page is published as an OpenAPI 3.0.1 fragment with `paths: {}` and the schema(s) under `components.schemas` (server block: `https://test.lisuns.com:4532`). YAML blocks below are exactly as served.

Schemas in this file (17): FinResultPeriod, FinResultsFact, FinResultsQueryItem, FinResultsQueryResponse, FinResultsResponse, FinResultsType, GainersLosersRequest, GenericXbrlFact, MarketCapHistory, MarketCapItem, NatureOfFinReport, QeBhavCopyCM, QeBhavCopyCMResult, QeBhavCopyFO, QeBhavCopyFOResult, QeCircuitFilterDetails, QeCircuitFilterDetailsResult.

---

## FinResultPeriod

> Source: https://docs.globaldatafeeds.in/finresultperiod-5999230d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## FinResultsFact

> Source: https://docs.globaldatafeeds.in/finresultsfact-5999231d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## FinResultsQueryItem

> Source: https://docs.globaldatafeeds.in/finresultsqueryitem-5999232d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## FinResultsQueryResponse

> Source: https://docs.globaldatafeeds.in/finresultsqueryresponse-5999233d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## FinResultsResponse

> Source: https://docs.globaldatafeeds.in/finresultsresponse-5999234d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## FinResultsType

> Source: https://docs.globaldatafeeds.in/finresultstype-5999235d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    FinResultsType:
      enum:
        - ProfitLoss
        - BalanceSheet
        - CashFlow
        - RelatedPartyTransactions
        - FR
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## GainersLosersRequest

> Source: https://docs.globaldatafeeds.in/gainerslosersrequest-5999236d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    GainersLosersRequest:
      enum:
        - GainersLosers
        - Gainers
        - Losers
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## GenericXbrlFact

> Source: https://docs.globaldatafeeds.in/genericxbrlfact-5999237d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## MarketCapHistory

> Source: https://docs.globaldatafeeds.in/marketcaphistory-5999238d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## MarketCapItem

> Source: https://docs.globaldatafeeds.in/marketcapitem-5999239d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## NatureOfFinReport

> Source: https://docs.globaldatafeeds.in/natureoffinreport-5999240d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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

## QeBhavCopyCM

> Source: https://docs.globaldatafeeds.in/qebhavcopycm-5999241d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeBhavCopyCMResult

> Source: https://docs.globaldatafeeds.in/qebhavcopycmresult-5999242d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeBhavCopyFO

> Source: https://docs.globaldatafeeds.in/qebhavcopyfo-5999243d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeBhavCopyFOResult

> Source: https://docs.globaldatafeeds.in/qebhavcopyforesult-5999244d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeCircuitFilterDetails

> Source: https://docs.globaldatafeeds.in/qecircuitfilterdetails-5999245d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeCircuitFilterDetailsResult

> Source: https://docs.globaldatafeeds.in/qecircuitfilterdetailsresult-5999246d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```
