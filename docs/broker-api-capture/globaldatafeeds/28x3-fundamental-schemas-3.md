# GlobalDataFeeds — Fundamental Data API — Schemas 3/3 (QeEquityDerivativesMs … VotingFactsResponse)

Gap-fill capture (2026-07-15) of the docs.globaldatafeeds.in schema/data-model pages indexed in `28-fundamental-data-api.md`. Each page fetched verbatim via `<url>.md`. Every schema page is published as an OpenAPI 3.0.1 fragment with `paths: {}` and the schema(s) under `components.schemas` (server block: `https://test.lisuns.com:4532`). YAML blocks below are exactly as served.

Schemas in this file (24): QeEquityDerivativesMs, QeEquityDerivativesMsResult, QeIndexDetail, QeIndexDetailResult, QeIndexHighlight, QeIndexHighlightResult, QeScripGroupTradeHighlight, QeScripGroupTradeHighlightResult, QeStatisticsGroupAbCompanies, QeStatisticsGroupAbCompaniesResult, QeTop15TurnoverDetails, QeTop15TurnoverDetailsResult, QeTopGainersLosers, QeTopGainersLosersResult, QeTotalTradeHighlight, QeTotalTradeHighlightResult, ResponseFormat, ResultCalendar, ResultCalendarItem, ResultCalendarRange, SectoralClassification, SectoralClassificationItem, ShpFactsResponse, VotingFactsResponse.

---

## QeEquityDerivativesMs

> Source: https://docs.globaldatafeeds.in/qeequityderivativesms-5999247d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeEquityDerivativesMsResult

> Source: https://docs.globaldatafeeds.in/qeequityderivativesmsresult-5999248d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeIndexDetail

> Source: https://docs.globaldatafeeds.in/qeindexdetail-5999249d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeIndexDetailResult

> Source: https://docs.globaldatafeeds.in/qeindexdetailresult-5999250d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## QeIndexHighlight

> Source: https://docs.globaldatafeeds.in/qeindexhighlight-5999251d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeIndexHighlight:
      type: object
      properties:
        indexName:
          type: string
          nullable: true
        prevClose:
          type: number
          format: double
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
        prevCloseChangePts:
          type: number
          format: double
        prevCloseChangePct:
          type: number
          format: double
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
        - indexName
        - prevClose
        - opening
        - high
        - low
        - closing
        - prevCloseChangePts
        - prevCloseChangePct
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

## QeIndexHighlightResult

> Source: https://docs.globaldatafeeds.in/qeindexhighlightresult-5999252d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeIndexHighlightResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeIndexHighlight'
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
    QeIndexHighlight:
      type: object
      properties:
        indexName:
          type: string
          nullable: true
        prevClose:
          type: number
          format: double
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
        prevCloseChangePts:
          type: number
          format: double
        prevCloseChangePct:
          type: number
          format: double
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
        - indexName
        - prevClose
        - opening
        - high
        - low
        - closing
        - prevCloseChangePts
        - prevCloseChangePct
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

## QeScripGroupTradeHighlight

> Source: https://docs.globaldatafeeds.in/qescripgrouptradehighlight-5999253d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeScripGroupTradeHighlight:
      type: object
      properties:
        scripGroupCode:
          type: string
          nullable: true
        scripsNo:
          type: integer
          format: int64
        tradesNo:
          type: integer
          format: int64
        volShares:
          type: number
          format: double
        turnoverRs:
          type: number
          format: double
        totTurnoverPct:
          type: number
          format: double
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
        - scripGroupCode
        - scripsNo
        - tradesNo
        - volShares
        - turnoverRs
        - totTurnoverPct
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

## QeScripGroupTradeHighlightResult

> Source: https://docs.globaldatafeeds.in/qescripgrouptradehighlightresult-5999254d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeScripGroupTradeHighlightResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeScripGroupTradeHighlight'
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
    QeScripGroupTradeHighlight:
      type: object
      properties:
        scripGroupCode:
          type: string
          nullable: true
        scripsNo:
          type: integer
          format: int64
        tradesNo:
          type: integer
          format: int64
        volShares:
          type: number
          format: double
        turnoverRs:
          type: number
          format: double
        totTurnoverPct:
          type: number
          format: double
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
        - scripGroupCode
        - scripsNo
        - tradesNo
        - volShares
        - turnoverRs
        - totTurnoverPct
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

## QeStatisticsGroupAbCompanies

> Source: https://docs.globaldatafeeds.in/qestatisticsgroupabcompanies-5999255d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeStatisticsGroupAbCompanies:
      type: object
      properties:
        scripGroupCode:
          type: string
          nullable: true
        issuesTraded:
          type: integer
          format: int64
        advances:
          type: integer
          format: int64
        declines:
          type: integer
          format: int64
        unchanged:
          type: integer
          format: int64
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
        - scripGroupCode
        - issuesTraded
        - advances
        - declines
        - unchanged
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

## QeStatisticsGroupAbCompaniesResult

> Source: https://docs.globaldatafeeds.in/qestatisticsgroupabcompaniesresult-5999256d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeStatisticsGroupAbCompaniesResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeStatisticsGroupAbCompanies'
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
    QeStatisticsGroupAbCompanies:
      type: object
      properties:
        scripGroupCode:
          type: string
          nullable: true
        issuesTraded:
          type: integer
          format: int64
        advances:
          type: integer
          format: int64
        declines:
          type: integer
          format: int64
        unchanged:
          type: integer
          format: int64
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
        - scripGroupCode
        - issuesTraded
        - advances
        - declines
        - unchanged
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

## QeTop15TurnoverDetails

> Source: https://docs.globaldatafeeds.in/qetop15turnoverdetails-5999257d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTop15TurnoverDetails:
      type: object
      properties:
        scripName:
          type: string
          nullable: true
        sharesNo:
          type: number
          format: double
        valueRsCr:
          type: number
          format: double
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
        - scripName
        - sharesNo
        - valueRsCr
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

## QeTop15TurnoverDetailsResult

> Source: https://docs.globaldatafeeds.in/qetop15turnoverdetailsresult-5999258d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTop15TurnoverDetailsResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeTop15TurnoverDetails'
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
    QeTop15TurnoverDetails:
      type: object
      properties:
        scripName:
          type: string
          nullable: true
        sharesNo:
          type: number
          format: double
        valueRsCr:
          type: number
          format: double
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
        - scripName
        - sharesNo
        - valueRsCr
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

## QeTopGainersLosers

> Source: https://docs.globaldatafeeds.in/qetopgainerslosers-5999259d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTopGainersLosers:
      type: object
      properties:
        scripCode:
          type: integer
          format: int64
        scripName:
          type: string
          nullable: true
        scripGroup:
          type: string
          nullable: true
        currentClosing:
          type: number
          format: double
        previousClosing:
          type: number
          format: double
        percentageFluctation:
          type: number
          format: double
        quantity:
          type: integer
          format: int64
        turnover:
          type: number
          format: double
        isLoser:
          type: boolean
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
        - scripCode
        - scripName
        - scripGroup
        - currentClosing
        - previousClosing
        - percentageFluctation
        - quantity
        - turnover
        - isLoser
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

## QeTopGainersLosersResult

> Source: https://docs.globaldatafeeds.in/qetopgainerslosersresult-5999260d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTopGainersLosersResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeTopGainersLosers'
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
    QeTopGainersLosers:
      type: object
      properties:
        scripCode:
          type: integer
          format: int64
        scripName:
          type: string
          nullable: true
        scripGroup:
          type: string
          nullable: true
        currentClosing:
          type: number
          format: double
        previousClosing:
          type: number
          format: double
        percentageFluctation:
          type: number
          format: double
        quantity:
          type: integer
          format: int64
        turnover:
          type: number
          format: double
        isLoser:
          type: boolean
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
        - scripCode
        - scripName
        - scripGroup
        - currentClosing
        - previousClosing
        - percentageFluctation
        - quantity
        - turnover
        - isLoser
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

## QeTotalTradeHighlight

> Source: https://docs.globaldatafeeds.in/qetotaltradehighlight-5999261d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTotalTradeHighlight:
      type: object
      properties:
        tradeHighlights:
          type: string
          nullable: true
        scripsNo:
          type: integer
          format: int64
        tradesNo:
          type: integer
          format: int64
        sharesVol:
          type: number
          format: double
        turnoverRsCr:
          type: number
          format: double
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
        - tradeHighlights
        - scripsNo
        - tradesNo
        - sharesVol
        - turnoverRsCr
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

## QeTotalTradeHighlightResult

> Source: https://docs.globaldatafeeds.in/qetotaltradehighlightresult-5999262d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    QeTotalTradeHighlightResult:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/QeTotalTradeHighlight'
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
    QeTotalTradeHighlight:
      type: object
      properties:
        tradeHighlights:
          type: string
          nullable: true
        scripsNo:
          type: integer
          format: int64
        tradesNo:
          type: integer
          format: int64
        sharesVol:
          type: number
          format: double
        turnoverRsCr:
          type: number
          format: double
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
        - tradeHighlights
        - scripsNo
        - tradesNo
        - sharesVol
        - turnoverRsCr
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

## ResponseFormat

> Source: https://docs.globaldatafeeds.in/responseformat-5999263d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
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

## ResultCalendar

> Source: https://docs.globaldatafeeds.in/resultcalendar-5999264d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## ResultCalendarItem

> Source: https://docs.globaldatafeeds.in/resultcalendaritem-5999265d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## ResultCalendarRange

> Source: https://docs.globaldatafeeds.in/resultcalendarrange-5999266d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## SectoralClassification

> Source: https://docs.globaldatafeeds.in/sectoralclassification-5999267d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## SectoralClassificationItem

> Source: https://docs.globaldatafeeds.in/sectoralclassificationitem-5999268d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## ShpFactsResponse

> Source: https://docs.globaldatafeeds.in/shpfactsresponse-5999269d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## VotingFactsResponse

> Source: https://docs.globaldatafeeds.in/votingfactsresponse-5999270d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    VotingFactsResponse:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```
