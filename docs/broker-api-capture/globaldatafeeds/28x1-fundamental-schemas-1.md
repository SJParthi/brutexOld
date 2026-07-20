# GlobalDataFeeds — Fundamental Data API — Schemas 1/3 (AnnualReportItem … DeliveryVolumesRange)

Gap-fill capture (2026-07-15) of the docs.globaldatafeeds.in schema/data-model pages indexed in `28-fundamental-data-api.md`. Each page fetched verbatim via `<url>.md`. Every schema page is published as an OpenAPI 3.0.1 fragment with `paths: {}` and the schema(s) under `components.schemas` (server block: `https://test.lisuns.com:4532`). YAML blocks below are exactly as served.

Schemas in this file (20): AnnualReportItem, AnnualReports, BulkDealItem, BulkDeals, BulkDealsRange, CgFactsResponse, CompanyData, CompanyDataItem, ContactDetails, ContactDetailsItem, CorporateActions, CorporateActionsItem, CorporateAnnouncement, CorporateAnnouncementItem, DateSymbolQueryItem, DateSymbolQueryResponse, DateTimeFormat, DeliveryVolumes, DeliveryVolumesItem, DeliveryVolumesRange.

---

## AnnualReportItem

> Source: https://docs.globaldatafeeds.in/annualreportitem-5999210d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## AnnualReports

> Source: https://docs.globaldatafeeds.in/annualreports-5999211d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## BulkDealItem

> Source: https://docs.globaldatafeeds.in/bulkdealitem-5999212d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    BulkDealItem:
      type: object
      properties:
        dealDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: string
          nullable: true
        scripName:
          type: string
          nullable: true
        clientName:
          type: string
          nullable: true
        dealType:
          type: string
          nullable: true
        qty:
          type: number
          format: double
        qtyPct:
          type: number
          format: double
        price:
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
        - dealDate
        - scripCode
        - scripName
        - clientName
        - dealType
        - qty
        - qtyPct
        - price
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

## BulkDeals

> Source: https://docs.globaldatafeeds.in/bulkdeals-5999213d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    BulkDeals:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/BulkDealItem'
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
    BulkDealItem:
      type: object
      properties:
        dealDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: string
          nullable: true
        scripName:
          type: string
          nullable: true
        clientName:
          type: string
          nullable: true
        dealType:
          type: string
          nullable: true
        qty:
          type: number
          format: double
        qtyPct:
          type: number
          format: double
        price:
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
        - dealDate
        - scripCode
        - scripName
        - clientName
        - dealType
        - qty
        - qtyPct
        - price
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

## BulkDealsRange

> Source: https://docs.globaldatafeeds.in/bulkdealsrange-5999214d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    BulkDealsRange:
      enum:
        - yesterday
        - Previous7days
        - Previous15days
        - Previous30days
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## CgFactsResponse

> Source: https://docs.globaldatafeeds.in/cgfactsresponse-5999215d0.md — captured 2026-07-15

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
    CgFactsResponse:
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## CompanyData

> Source: https://docs.globaldatafeeds.in/companydata-5999216d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    CompanyDataItem:
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
        isin:
          type: string
          nullable: true
        address:
          type: string
          nullable: true
        contactDetails:
          type: string
          nullable: true
        email:
          type: string
          nullable: true
        companySecretaryName:
          type: string
          nullable: true
        designation:
          type: string
          nullable: true
        rtaName:
          type: string
          nullable: true
        rtaEmail:
          type: string
          nullable: true
        rtaContact:
          type: string
          nullable: true
        rn:
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
        - isin
        - address
        - contactDetails
        - email
        - companySecretaryName
        - designation
        - rtaName
        - rtaEmail
        - rtaContact
        - rn
        - receivedAt
        - savedAt
      x-apidog-folder: ''
    CompanyData:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/CompanyDataItem'
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

## CompanyDataItem

> Source: https://docs.globaldatafeeds.in/companydataitem-5999217d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    CompanyDataItem:
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
        isin:
          type: string
          nullable: true
        address:
          type: string
          nullable: true
        contactDetails:
          type: string
          nullable: true
        email:
          type: string
          nullable: true
        companySecretaryName:
          type: string
          nullable: true
        designation:
          type: string
          nullable: true
        rtaName:
          type: string
          nullable: true
        rtaEmail:
          type: string
          nullable: true
        rtaContact:
          type: string
          nullable: true
        rn:
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
        - isin
        - address
        - contactDetails
        - email
        - companySecretaryName
        - designation
        - rtaName
        - rtaEmail
        - rtaContact
        - rn
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

## ContactDetails

> Source: https://docs.globaldatafeeds.in/contactdetails-5999218d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    ContactDetailsItem:
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
        isin:
          type: string
          nullable: true
        address:
          type: string
          nullable: true
        contactDetails:
          type: string
          nullable: true
        email:
          type: string
          nullable: true
        companySecretaryName:
          type: string
          nullable: true
        designation:
          type: string
          nullable: true
        rtaName:
          type: string
          nullable: true
        rtaEmail:
          type: string
          nullable: true
        rtaContact:
          type: string
          nullable: true
        rn:
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
        - isin
        - address
        - contactDetails
        - email
        - companySecretaryName
        - designation
        - rtaName
        - rtaEmail
        - rtaContact
        - rn
        - receivedAt
        - savedAt
      x-apidog-folder: ''
    ContactDetails:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/ContactDetailsItem'
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

## ContactDetailsItem

> Source: https://docs.globaldatafeeds.in/contactdetailsitem-5999219d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    ContactDetailsItem:
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
        isin:
          type: string
          nullable: true
        address:
          type: string
          nullable: true
        contactDetails:
          type: string
          nullable: true
        email:
          type: string
          nullable: true
        companySecretaryName:
          type: string
          nullable: true
        designation:
          type: string
          nullable: true
        rtaName:
          type: string
          nullable: true
        rtaEmail:
          type: string
          nullable: true
        rtaContact:
          type: string
          nullable: true
        rn:
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
        - isin
        - address
        - contactDetails
        - email
        - companySecretaryName
        - designation
        - rtaName
        - rtaEmail
        - rtaContact
        - rn
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

## CorporateActions

> Source: https://docs.globaldatafeeds.in/corporateactions-5999220d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## CorporateActionsItem

> Source: https://docs.globaldatafeeds.in/corporateactionsitem-5999221d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## CorporateAnnouncement

> Source: https://docs.globaldatafeeds.in/corporateannouncement-5999222d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    CorporateAnnouncementItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        fillingDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        meetingDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        tradeDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: integer
          format: int32
        companyName:
          type: string
          nullable: true
        fileStatus:
          type: string
          nullable: true
        headLine:
          type: string
          nullable: true
        newsSubject:
          type: string
          nullable: true
        attachmentName:
          type: string
          nullable: true
        newsBody:
          type: string
          nullable: true
        descriptor:
          type: string
          nullable: true
        criticalNews:
          type: integer
          format: int64
        announceType:
          type: string
          nullable: true
        meetingType:
          type: string
          nullable: true
        descriptorId:
          type: string
          nullable: true
        attachmentUrl:
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
        - fillingDate
        - meetingDate
        - tradeDate
        - scripCode
        - companyName
        - fileStatus
        - headLine
        - newsSubject
        - attachmentName
        - newsBody
        - descriptor
        - criticalNews
        - announceType
        - meetingType
        - descriptorId
        - attachmentUrl
        - receivedAt
        - savedAt
      x-apidog-folder: ''
    CorporateAnnouncement:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/CorporateAnnouncementItem'
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

## CorporateAnnouncementItem

> Source: https://docs.globaldatafeeds.in/corporateannouncementitem-5999223d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    CorporateAnnouncementItem:
      type: object
      properties:
        symbol:
          type: string
          nullable: true
        fillingDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        meetingDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        tradeDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: integer
          format: int32
        companyName:
          type: string
          nullable: true
        fileStatus:
          type: string
          nullable: true
        headLine:
          type: string
          nullable: true
        newsSubject:
          type: string
          nullable: true
        attachmentName:
          type: string
          nullable: true
        newsBody:
          type: string
          nullable: true
        descriptor:
          type: string
          nullable: true
        criticalNews:
          type: integer
          format: int64
        announceType:
          type: string
          nullable: true
        meetingType:
          type: string
          nullable: true
        descriptorId:
          type: string
          nullable: true
        attachmentUrl:
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
        - fillingDate
        - meetingDate
        - tradeDate
        - scripCode
        - companyName
        - fileStatus
        - headLine
        - newsSubject
        - attachmentName
        - newsBody
        - descriptor
        - criticalNews
        - announceType
        - meetingType
        - descriptorId
        - attachmentUrl
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

## DateSymbolQueryItem

> Source: https://docs.globaldatafeeds.in/datesymbolqueryitem-5999224d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## DateSymbolQueryResponse

> Source: https://docs.globaldatafeeds.in/datesymbolqueryresponse-5999225d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
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
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## DateTimeFormat

> Source: https://docs.globaldatafeeds.in/datetimeformat-5999226d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    DateTimeFormat:
      enum:
        - Epoch
        - String
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```

---

## DeliveryVolumes

> Source: https://docs.globaldatafeeds.in/deliveryvolumes-5999227d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    DeliveryVolumesItem:
      type: object
      properties:
        grossDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: string
          nullable: true
        scripName:
          type: string
          nullable: true
        deliveryQty:
          type: number
          format: double
        deliveryValue:
          type: number
          format: double
        deliveryQtyPct:
          type: number
          format: double
        tradedQty:
          type: number
          format: double
        turnover:
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
        - grossDate
        - scripCode
        - scripName
        - deliveryQty
        - deliveryValue
        - deliveryQtyPct
        - tradedQty
        - turnover
        - receivedAt
        - savedAt
      x-apidog-folder: ''
    DeliveryVolumes:
      type: object
      properties:
        value:
          type: array
          items:
            $ref: '#/components/schemas/DeliveryVolumesItem'
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

## DeliveryVolumesItem

> Source: https://docs.globaldatafeeds.in/deliveryvolumesitem-5999228d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    DeliveryVolumesItem:
      type: object
      properties:
        grossDate:
          type: string
          examples:
            - 02-25-2025
          nullable: true
        scripCode:
          type: string
          nullable: true
        scripName:
          type: string
          nullable: true
        deliveryQty:
          type: number
          format: double
        deliveryValue:
          type: number
          format: double
        deliveryQtyPct:
          type: number
          format: double
        tradedQty:
          type: number
          format: double
        turnover:
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
        - grossDate
        - scripCode
        - scripName
        - deliveryQty
        - deliveryValue
        - deliveryQtyPct
        - tradedQty
        - turnover
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

## DeliveryVolumesRange

> Source: https://docs.globaldatafeeds.in/deliveryvolumesrange-5999229d0.md — captured 2026-07-15

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths: {}
components:
  schemas:
    DeliveryVolumesRange:
      enum:
        - yesterday
        - Previous7days
      type: string
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []
```
