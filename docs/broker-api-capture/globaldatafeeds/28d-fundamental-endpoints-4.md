# GlobalDataFeeds — Fundamental Data API — Endpoints 4 (Company Information, Bulk/Block Deals, Delivery Volumes, FII DII Activity)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetCompanyData — /getcompanydata-15575603e0
2. GetBulkDeals — /getbulkdeals-15575586e0
3. GetBlockDeals — /getblockdeals-15575587e0
4. GetDeliveryVolumes — /getdeliveryvolumes-15575588e0
5. GetFIIDII — /getfiidii-37773825e0

---

## GetCompanyData

> Source: https://docs.globaldatafeeds.in/getcompanydata-15575603e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetCompanyData:
    get:
      summary: GetCompanyData
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>



        The **GetCompanyData** function provides key corporate information for a
        listed company, including its stock symbol, scrip code, registered
        address, contact details, company secretary name, RTA (Registrar and
        Transfer Agent) details, and identification numbers like ISIN. This
        function helps users quickly access basic and official data about a
        company's identity and compliance contacts.


        ### **What is returned?**


        Symbol, ScripCode, CompanyName, ISIN, Address, ContactDetails, Email,
        CompanySecretaryName, Designation, RtaName, RtaEmail, RtaContact, Rn,
        ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**

        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
          <Tabs style="background-color:lightgrey;">
          <Tab title="JSON">
        {

        "Value":[

        { "Symbol": "RELIANCE",

        "ScripCode": 500325,

        "CompanyName": "RELIANCE INDUSTRIES LTD.",

        "ISIN": "INE002A01018",

        "Address": "" Maker Chambers IV   3rd Floor 222  Nariman Point ",

        "ContactDetails": "Mumbai",

        "Email": " 400021",

        "CompanySecretaryName": " Maharashtra"",

        "Designation": ""022-3555 5000",

        "RtaName": """,

        "RtaEmail": "investor.relations@ril.com",

        "RtaContact": "Savithri  Parekh",

        "Rn": "Company Secretary & Compliance Officer",

        "ReceivedAt": "04-08-2025 16:14:30",

        "SavedAt": "04-08-2025 16:14:30"}

        ]}

        </Tab>

        <Tab title="XML">

        &lt;CompanyData&gt;
            &lt;Value&gt;
              &lt;CompanyDataItem Symbol="RELIANCE" ScripCode="500325" CompanyName="RELIANCE INDUSTRIES LTD." ISIN="INE002A01018" Address="&quot; Maker Chambers IV   3rd Floor 222  Nariman Point " ContactDetails="Mumbai" Email=" 400021" CompanySecretaryName=" Maharashtra&quot;" Designation="&quot;022-3555 5000" RtaName="&quot;" RtaEmail="investor.relations@ril.com" RtaContact="Savithri  Parekh" Rn="Company Secretary &amp; Compliance Officer" ReceivedAt="04-08-2025 16:14:30" SavedAt="04-08-2025 16:14:30" /&gt;
            &lt;/Value&gt;
          &lt;/CompanyData&gt;
          </Tab>
          <Tab title="CSV">
        Symbol,ScripCode,CompanyName,ISIN,Address,ContactDetails,Email,CompanySecretaryName,Designation,RtaName,RtaEmail,RtaContact,Rn,ReceivedAt,SavedAt


        RELIANCE,500325,RELIANCE INDUSTRIES LTD.,INE002A01018," Maker Chambers
        IV   3rd Floor 222  Nariman Point ,Mumbai, 400021,
        Maharashtra","022-3555 5000,",investor.relations@ril.com,Savithri 
        Parekh,Company Secretary & Compliance Officer,"04-08-2025
        16:14:30","04-08-2025 16:14:30"
          </Tab>
          <Tab title="CsvContent">
        Symbol,ScripCode,CompanyName,ISIN,Address,ContactDetails,Email,CompanySecretaryName,Designation,RtaName,RtaEmail,RtaContact,Rn,ReceivedAt,SavedAt


        RELIANCE,500325,RELIANCE INDUSTRIES LTD.,INE002A01018," Maker Chambers
        IV   3rd Floor 222  Nariman Point ,Mumbai, 400021,
        Maharashtra","022-3555 5000,",investor.relations@ril.com,Savithri 
        Parekh,Company Secretary & Compliance Officer,"04-08-2025
        16:14:30","04-08-2025 16:14:30"


        Note: 

        1. The file containing above values in CSV format will be returned. 

        2. To see it working, copy-paste the request in browser.
          </Tab>
        </Tabs>

        </div>
      tags:
        - Corporate Data APIs (RESTful)/Company Information
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
          required: true
          example: RELIANCE+ABB
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
                $ref: '#/components/schemas/CompanyData'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Company Information
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575603-run
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetBulkDeals

> Source: https://docs.globaldatafeeds.in/getbulkdeals-15575586e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBulkDeals:
    get:
      summary: GetBulkDeals
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetBulkDeals** returns details about bulk deals executed in the stock
        market. A bulk deal occurs when a single investor or institution buys or
        sells a large quantity of shares in a single trading session, typically
        exceeding 0.5% of the company's total equity shares.


        ### What is returned?


        DealDate, ScripCode, ScripName, ClientName, DealType, Qty, QtyPct,
        Price, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {  
                            "Value": [  
                            {  
                            "DealDate": "02-27-2025",  
                            "ScripCode": 544072,  
                            "ScripId": "AIKPIPES",  
                            "ClientName": "SATYA PRAKASH MITTAL",  
                            "DealType": "S",  
                            "Qty": 44800,  
                            "QtyPct": 0.704070407040704,  
                            "Price": 81.4,  
                            "ReceivedAt": "02-27-2025 21:00:02",  
                            "SavedAt": "02-27-2025 21:00:02"  
                            },  
                            {  
                            "DealDate": "02-27-2025",  
                            "ScripCode": 544072,  
                            "ScripId": "AIKPIPES",  
                            "ClientName": "NEELAM MITTAL",  
                            "DealType": "B",  
                            "Qty": 44800,  
                            "QtyPct": 0.704070407040704,  
                            "Price": 81.4,  
                            "ReceivedAt": "02-27-2025 21:00:02",  
                            "SavedAt": "02-27-2025 21:00:02"  
                            }  
                            ]  
                            }
                    </Tab>
                    <Tab title="XML">
                        &lt;BulkDeals&gt;
                        &lt;Value&gt;
                        &lt;BulkDealItem DealDate="02-27-2025" ScripCode="544072" ScripId="AIKPIPES" ClientName="SATYA PRAKASH MITTAL" DealType="S" Qty="44800" QtyPct="0.704070407040704" Price="81.4" ReceivedAt="02-27-2025 21:00:02" SavedAt="02-27-2025 21:00:02"/&gt;
                        &lt;BulkDealItem DealDate="02-27-2025" ScripCode="544072" ScripId="AIKPIPES" ClientName="NEELAM MITTAL" DealType="B" Qty="44800" QtyPct="0.704070407040704" Price="81.4" ReceivedAt="02-27-2025 21:00:02" SavedAt="02-27-2025 21:00:02"/&gt;
                        &lt;/Value&gt;
                        &lt;/BulkDeals&gt;
                    </Tab>
                    <Tab title="CSV">
                        DealDate,ScripCode,ScripName,ClientName,DealType,Qty,QtyPct,Price,ReceivedAt,SavedAt  
                        "02-27-2025",544072,AIKPIPES,SATYA PRAKASH MITTAL,S,44800.0,0.704070407040704,81.4,"02-27-2025 21:00:02","02-27-2025 21:00:02"  
                        "02-27-2025",544072,AIKPIPES,NEELAM MITTAL,B,44800.0,0.704070407040704,81.4,"02-27-2025 21:00:02","02-27-2025 21:00:02"
                    </Tab>
                    <Tab title="CsvContent">
                        DealDate,ScripCode,ScripName,ClientName,DealType,Qty,QtyPct,Price,ReceivedAt,SavedAt  
                        "02-27-2025",544072,AIKPIPES,SATYA PRAKASH MITTAL,S,44800.0,0.704070407040704,81.4,"02-27-2025 21:00:02","02-27-2025 21:00:02"  
                        "02-27-2025",544072,AIKPIPES,NEELAM MITTAL,B,44800.0,0.704070407040704,81.4,"02-27-2025 21:00:02","02-27-2025 21:00:02"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
      tags:
        - Corporate Data APIs (RESTful)/Bulk / Block Deals
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
          description: 'Example: CHECKPOINT (If empty - max range is Previous30days)'
          required: false
          example: CHECKPOINT
          schema:
            type: string
            default: RELIANCE
        - name: range
          in: query
          description: BulkDealsRange
          required: false
          example: Previous7days
          schema:
            $ref: '#/components/schemas/BulkDealsRange'
            default: Previous7days
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
                $ref: '#/components/schemas/BulkDeals'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Bulk / Block Deals
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575586-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetBlockDeals

> Source: https://docs.globaldatafeeds.in/getblockdeals-15575587e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetBlockDeals:
    get:
      summary: GetBlockDeals
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetBlockDeals** refers to a single trade involving shares worth ₹10
        crore or more between two parties, executed through a special trading
        window on the stock exchange. These trades are not visible in the
        regular order book but are reported separately for transparency.


        ### What is returned?


        DealDate, ScripCode, ScripName, ClientName, DealType, Qty, QtyPct,
        Price, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
                            "Value": [
                              {
                                "DealDate": "03-28-2025",
                                "ScripCode": 541154,
                                "ScripId": "Hindustan Aeronautics Limited",
                                "ClientName": "GOLDMAN SACHS (SINGAPORE) PTE.- ODI",
                                "DealType": "B",
                                "Qty": 385774,
                                "QtyPct": 0,
                                "Price": 4176.25,
                                "ReceivedAt": "04-03-2025 21:01:06",
                                "SavedAt": "04-03-2025 21:01:06"
                              },
                              {
                                "DealDate": "03-28-2025",
                                "ScripCode": 541154,
                                "ScripId": "Hindustan Aeronautics Limited",
                                "ClientName": "KADENSA MASTER FUND",
                                "DealType": "S",
                                "Qty": 385774,
                                "QtyPct": 0,
                                "Price": 4176.25,
                                "ReceivedAt": "04-03-2025 21:01:06",
                                "SavedAt": "04-03-2025 21:01:06"
                              },
                              {
                                "DealDate": "03-28-2025",
                                "ScripCode": 543320,
                                "ScripId": "Zomato Limited",
                                "ClientName": "GOLDMAN SACHS (SINGAPORE) PTE.- ODI",
                                "DealType": "B",
                                "Qty": 6007412,
                                "QtyPct": 0.0622507121217147,
                                "Price": 199.5,
                                "ReceivedAt": "04-03-2025 21:01:06",
                                "SavedAt": "04-03-2025 21:01:06"
                              },
                              {
                                "DealDate": "03-28-2025",
                                "ScripCode": 543320,
                                "ScripId": "Zomato Limited",
                                "ClientName": "KADENSA MASTER FUND",
                                "DealType": "S",
                                "Qty": 6007412,
                                "QtyPct": 0.0622507121217147,
                                "Price": 199.5,
                                "ReceivedAt": "04-03-2025 21:01:06",
                                "SavedAt": "04-03-2025 21:01:06"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;BlockDeals&gt;
                        &lt;Value&gt;
                        &lt;BlockDealItem DealDate="03-28-2025" ScripCode="541154" ScripId="Hindustan Aeronautics Limited" ClientName="GOLDMAN SACHS (SINGAPORE) PTE.- ODI" DealType="B" Qty="385774" QtyPct="0" Price="4176.25" ReceivedAt="04-03-2025 21:01:06" SavedAt="04-03-2025 21:01:06"/&gt;
                        &lt;BlockDealItem DealDate="03-28-2025" ScripCode="541154" ScripId="Hindustan Aeronautics Limited" ClientName="KADENSA MASTER FUND" DealType="S" Qty="385774" QtyPct="0" Price="4176.25" ReceivedAt="04-03-2025 21:01:06" SavedAt="04-03-2025 21:01:06"/&gt;
                        &lt;BlockDealItem DealDate="03-28-2025" ScripCode="543320" ScripId="Zomato Limited" ClientName="GOLDMAN SACHS (SINGAPORE) PTE.- ODI" DealType="B" Qty="6007412" QtyPct="0.06225071212171468" Price="199.5" ReceivedAt="04-03-2025 21:01:06" SavedAt="04-03-2025 21:01:06"/&gt;
                        &lt;BlockDealItem DealDate="03-28-2025" ScripCode="543320" ScripId="Zomato Limited" ClientName="KADENSA MASTER FUND" DealType="S" Qty="6007412" QtyPct="0.06225071212171468" Price="199.5" ReceivedAt="04-03-2025 21:01:06" SavedAt="04-03-2025 21:01:06"/&gt;
                        &lt;/Value&gt;
                        &lt;/BlockDeals&gt;
                    </Tab>
                    <Tab title="CSV">
                        DealDate,ScripCode,ScripName,ClientName,DealType,Qty,QtyPct,Price,ReceivedAt,SavedAt
                        "03-28-2025",541154,Hindustan Aeronautics Limited,GOLDMAN SACHS (SINGAPORE) PTE.- ODI,B,385774.0,0.0,4176.25,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",541154,Hindustan Aeronautics Limited,KADENSA MASTER FUND,S,385774.0,0.0,4176.25,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",543320,Zomato Limited,GOLDMAN SACHS (SINGAPORE) PTE.- ODI,B,6007412.0,0.06225071212171468,199.5,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",543320,Zomato Limited,KADENSA MASTER FUND,S,6007412.0,0.06225071212171468,199.5,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                    </Tab>
                    <Tab title="CsvContent">
                        DealDate,ScripCode,ScripName,ClientName,DealType,Qty,QtyPct,Price,ReceivedAt,SavedAt
                        "03-28-2025",541154,Hindustan Aeronautics Limited,GOLDMAN SACHS (SINGAPORE) PTE.- ODI,B,385774.0,0.0,4176.25,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",541154,Hindustan Aeronautics Limited,KADENSA MASTER FUND,S,385774.0,0.0,4176.25,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",543320,Zomato Limited,GOLDMAN SACHS (SINGAPORE) PTE.- ODI,B,6007412.0,0.06225071212171468,199.5,"04-03-2025 21:01:06","04-03-2025 21:01:06"
                        "03-28-2025",543320,Zomato Limited,KADENSA MASTER FUND,S,6007412.0,0.06225071212171468,199.5,"04-03-2025 21:01:06","04-03-2025 21:01:06"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
          
      tags:
        - Corporate Data APIs (RESTful)/Bulk / Block Deals
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
          description: 'Example: RELIANCE (If empty - max range is Previous30days)'
          required: false
          example: RAJESH
          schema:
            type: string
            default: RELIANCE
        - name: range
          in: query
          description: 'BlockDealsRange. Example: Previous7days'
          required: false
          example: Previous7days
          schema:
            $ref: '#/components/schemas/BulkDealsRange'
            default: Previous7days
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
      responses:
        '200':
          description: Success
          content:
            text/plain:
              schema:
                type: object
                properties: {}
                x-apidog-orders: []
                x-apidog-ignore-properties: []
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Bulk / Block Deals
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575587-run
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
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetDeliveryVolumes

> Source: https://docs.globaldatafeeds.in/getdeliveryvolumes-15575588e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetDeliveryVolumes:
    get:
      summary: GetDeliveryVolumes
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetDeliveryVolumes** returns information about the delivery volume of
        stocks traded on a given date. It helps traders and analysts understand
        how much of the traded volume was actually delivered, indicating
        investor confidence and long-term interest in the stock.


        ### What is returned?


        GrossDate, ScripCode, ScripId, DeliveryQty, DeliveryValue,
        DeliveryQtyPct, TradedQty, Turnover, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        | Response Type | Sample
        Response                                                                                        
        |

        | :------------ |
        :------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetDeliveryVolumesResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetDeliveryVolumesResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetDeliveryVolumesResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned.                                              |
      tags:
        - Corporate Data APIs (RESTful)/Delivery Volumes
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
          description: 'Example: RELIANCE (If empty - max range is Previous7days)'
          required: false
          example: RELIANCE
          schema:
            type: string
            default: RELIANCE
        - name: range
          in: query
          description: 'DeliveryVolumesRange. Example: Previous7days'
          required: false
          example: Previous7days
          schema:
            $ref: '#/components/schemas/DeliveryVolumesRange'
            default: Previous7days
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
                $ref: '#/components/schemas/DeliveryVolumes'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Corporate Data APIs (RESTful)/Delivery Volumes
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575588-run
components:
  schemas:
    DeliveryVolumesRange:
      enum:
        - yesterday
        - Previous7days
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetFIIDII

> Source: https://docs.globaldatafeeds.in/getfiidii-37773825e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetFIIDII:
    get:
      summary: GetFIIDII
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th March 2026**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        The **GetFIIDII API** provides Foreign Institutional Investor (FII) and
        Domestic Institutional Investor (DII) derivatives positioning data for a
        specified exchange and date range. It returns long/short positions, net
        open interest (OI), and bullish/bearish signals across futures and
        options segments.


        ### **What is returned?**


        Date, ClientType, FutureIndexLong, FutureIndexShort, FutureStockLong,
        FutureStockShort, OptionIndexCallLong, OptionIndexPutLong,
        OptionIndexCallShort, OptionIndexPutShort, OptionStockCallLong,
        OptionStockPutLong, OptionStockCallShort, OptionStockPutShort,
        TotalLongContracts, TotalShortContracts, FutureIndexNetPosition,
        FutureIndexSignal, FutureStockNetPosition, FutureStockSignal,
        OptionIndexPutOI, OptionIndexCallOI, OptionIndexNetCallOI,
        OptionIndexNetPutOI, OptionIndexNetOI, OptionIndexSignal,
        OptionStockPutOI, OptionStockCallOI, OptionStockNetPutOI,
        OptionStockNetCallOI, OptionStockNetOI, OptionStockSignal


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
          "Value": [
            {
              "Date": "05-22-2026 20:12:02",
              "ClientType": "Client",
              "FutureIndexLong": 254325,
              "FutureIndexShort": 92661,
              "FutureStockLong": 3132410,
              "FutureStockShort": 274494,
              "OptionIndexCallLong": 2644777,
              "OptionIndexPutLong": 2446220,
              "OptionIndexCallShort": 2718504,
              "OptionIndexPutShort": 3083482,
              "OptionStockCallLong": 2373278,
              "OptionStockPutLong": 839786,
              "OptionStockCallShort": 1343013,
              "OptionStockPutShort": 1089193,
              "TotalLongContracts": 11690797,
              "TotalShortContracts": 8601346,
              "FutureIndexNetPosition": 161664,
              "FutureIndexSignal": "Bullish",
              "FutureStockNetPosition": 2857916,
              "FutureStockSignal": "Bullish",
              "OptionIndexPutOI": -198557,
              "OptionIndexCallOI": 364978,
              "OptionIndexNetCallOI": -73727,
              "OptionIndexNetPutOI": -637262,
              "OptionIndexNetOI": 563535,
              "OptionIndexSignal": "Bullish",
              "OptionStockPutOI": -1533492,
              "OptionStockCallOI": -253820,
              "OptionStockNetPutOI": -249407,
              "OptionStockNetCallOI": 1030265,
              "OptionStockNetOI": 1279672,
              "OptionStockSignal": "Bullish"
            },
            {
              "Date": "05-22-2026 20:12:02",
              "ClientType": "DII",
              "FutureIndexLong": 67686,
              "FutureIndexShort": 37619,
              "FutureStockLong": 435198,
              "FutureStockShort": 4321198,
              "OptionIndexCallLong": 14281,
              "OptionIndexPutLong": 56708,
              "OptionIndexCallShort": 865,
              "OptionIndexPutShort": 300,
              "OptionStockCallLong": 1098,
              "OptionStockPutLong": 30831,
              "OptionStockCallShort": 488882,
              "OptionStockPutShort": 10701,
              "TotalLongContracts": 605802,
              "TotalShortContracts": 4859565,
              "FutureIndexNetPosition": 30067,
              "FutureIndexSignal": "Bullish",
              "FutureStockNetPosition": -3886000,
              "FutureStockSignal": "Bearish",
              "OptionIndexPutOI": 42427,
              "OptionIndexCallOI": -565,
              "OptionIndexNetCallOI": 13416,
              "OptionIndexNetPutOI": 56408,
              "OptionIndexNetOI": -42992,
              "OptionIndexSignal": "Bearish",
              "OptionStockPutOI": 29733,
              "OptionStockCallOI": -478181,
              "OptionStockNetPutOI": 20130,
              "OptionStockNetCallOI": -487784,
              "OptionStockNetOI": -507914,
              "OptionStockSignal": "Bearish"
            },
            {
              "Date": "05-22-2026 20:12:02",
              "ClientType": "FII",
              "FutureIndexLong": 43530,
              "FutureIndexShort": 268522,
              "FutureStockLong": 4182673,
              "FutureStockShort": 3546361,
              "OptionIndexCallLong": 653857,
              "OptionIndexPutLong": 926504,
              "OptionIndexCallShort": 815806,
              "OptionIndexPutShort": 532543,
              "OptionStockCallLong": 244303,
              "OptionStockPutLong": 308176,
              "OptionStockCallShort": 366368,
              "OptionStockPutShort": 236884,
              "TotalLongContracts": 6359043,
              "TotalShortContracts": 5766484,
              "FutureIndexNetPosition": -224992,
              "FutureIndexSignal": "Bearish",
              "FutureStockNetPosition": 636312,
              "FutureStockSignal": "Bullish",
              "OptionIndexPutOI": 272647,
              "OptionIndexCallOI": -283263,
              "OptionIndexNetCallOI": -161949,
              "OptionIndexNetPutOI": 393961,
              "OptionIndexNetOI": -555910,
              "OptionIndexSignal": "Bearish",
              "OptionStockPutOI": 63873,
              "OptionStockCallOI": -129484,
              "OptionStockNetPutOI": 71292,
              "OptionStockNetCallOI": -122065,
              "OptionStockNetOI": -193357,
              "OptionStockSignal": "Bearish"
            },
            {
              "Date": "05-22-2026 20:12:02",
              "ClientType": "Pro",
              "FutureIndexLong": 82231,
              "FutureIndexShort": 48970,
              "FutureStockLong": 926977,
              "FutureStockShort": 535205,
              "OptionIndexCallLong": 1239552,
              "OptionIndexPutLong": 1190072,
              "OptionIndexCallShort": 1017293,
              "OptionIndexPutShort": 1003180,
              "OptionStockCallLong": 1334772,
              "OptionStockPutLong": 1250438,
              "OptionStockCallShort": 1755188,
              "OptionStockPutShort": 1092453,
              "TotalLongContracts": 6024042,
              "TotalShortContracts": 5452288,
              "FutureIndexNetPosition": 33261,
              "FutureIndexSignal": "Bullish",
              "FutureStockNetPosition": 391772,
              "FutureStockSignal": "Bullish",
              "OptionIndexPutOI": -49480,
              "OptionIndexCallOI": -14113,
              "OptionIndexNetCallOI": 222259,
              "OptionIndexNetPutOI": 186892,
              "OptionIndexNetOI": 35367,
              "OptionIndexSignal": "Bullish",
              "OptionStockPutOI": -84334,
              "OptionStockCallOI": -662735,
              "OptionStockNetPutOI": 157985,
              "OptionStockNetCallOI": -420416,
              "OptionStockNetOI": -578401,
              "OptionStockSignal": "Bearish"
            },
            {
              "Date": "05-22-2026 20:12:02",
              "ClientType": "TOTAL",
              "FutureIndexLong": 447772,
              "FutureIndexShort": 447772,
              "FutureStockLong": 8677258,
              "FutureStockShort": 8677258,
              "OptionIndexCallLong": 4552468,
              "OptionIndexPutLong": 4619504,
              "OptionIndexCallShort": 4552468,
              "OptionIndexPutShort": 4619504,
              "OptionStockCallLong": 3953451,
              "OptionStockPutLong": 2429231,
              "OptionStockCallShort": 3953451,
              "OptionStockPutShort": 2429231,
              "TotalLongContracts": 24679684,
              "TotalShortContracts": 24679684,
              "FutureIndexNetPosition": 0,
              "FutureIndexSignal": "Neutral",
              "FutureStockNetPosition": 0,
              "FutureStockSignal": "Neutral",
              "OptionIndexPutOI": 67036,
              "OptionIndexCallOI": 67036,
              "OptionIndexNetCallOI": 0,
              "OptionIndexNetPutOI": 0,
              "OptionIndexNetOI": 0,
              "OptionIndexSignal": "Neutral",
              "OptionStockPutOI": -1524220,
              "OptionStockCallOI": -1524220,
              "OptionStockNetPutOI": 0,
              "OptionStockNetCallOI": 0,
              "OptionStockNetOI": 0,
              "OptionStockSignal": "Neutral"
            }
          ]
        }
                    </Tab>
                    <Tab title="XML">
                       &lt;FIIDIIData&gt;
        &lt;Value&gt;

        &lt;FIIDIIDataItem Date="05-22-2026 20:12:02" ClientType="Client"
        FutureIndexLong="254325" FutureIndexShort="92661"
        FutureStockLong="3132410" FutureStockShort="274494"
        OptionIndexCallLong="2644777" OptionIndexPutLong="2446220"
        OptionIndexCallShort="2718504" OptionIndexPutShort="3083482"
        OptionStockCallLong="2373278" OptionStockPutLong="839786"
        OptionStockCallShort="1343013" OptionStockPutShort="1089193"
        TotalLongContracts="11690797" TotalShortContracts="8601346"
        FutureIndexNetPosition="161664" FutureIndexSignal="Bullish"
        FutureStockNetPosition="2857916" FutureStockSignal="Bullish"
        OptionIndexPutOI="-198557" OptionIndexCallOI="364978"
        OptionIndexNetCallOI="-73727" OptionIndexNetPutOI="-637262"
        OptionIndexNetOI="563535" OptionIndexSignal="Bullish"
        OptionStockPutOI="-1533492" OptionStockCallOI="-253820"
        OptionStockNetPutOI="-249407" OptionStockNetCallOI="1030265"
        OptionStockNetOI="1279672" OptionStockSignal="Bullish"/&gt;

        &lt;FIIDIIDataItem Date="05-22-2026 20:12:02" ClientType="DII"
        FutureIndexLong="67686" FutureIndexShort="37619"
        FutureStockLong="435198" FutureStockShort="4321198"
        OptionIndexCallLong="14281" OptionIndexPutLong="56708"
        OptionIndexCallShort="865" OptionIndexPutShort="300"
        OptionStockCallLong="1098" OptionStockPutLong="30831"
        OptionStockCallShort="488882" OptionStockPutShort="10701"
        TotalLongContracts="605802" TotalShortContracts="4859565"
        FutureIndexNetPosition="30067" FutureIndexSignal="Bullish"
        FutureStockNetPosition="-3886000" FutureStockSignal="Bearish"
        OptionIndexPutOI="42427" OptionIndexCallOI="-565"
        OptionIndexNetCallOI="13416" OptionIndexNetPutOI="56408"
        OptionIndexNetOI="-42992" OptionIndexSignal="Bearish"
        OptionStockPutOI="29733" OptionStockCallOI="-478181"
        OptionStockNetPutOI="20130" OptionStockNetCallOI="-487784"
        OptionStockNetOI="-507914" OptionStockSignal="Bearish"/&gt;

        &lt;FIIDIIDataItem Date="05-22-2026 20:12:02" ClientType="FII"
        FutureIndexLong="43530" FutureIndexShort="268522"
        FutureStockLong="4182673" FutureStockShort="3546361"
        OptionIndexCallLong="653857" OptionIndexPutLong="926504"
        OptionIndexCallShort="815806" OptionIndexPutShort="532543"
        OptionStockCallLong="244303" OptionStockPutLong="308176"
        OptionStockCallShort="366368" OptionStockPutShort="236884"
        TotalLongContracts="6359043" TotalShortContracts="5766484"
        FutureIndexNetPosition="-224992" FutureIndexSignal="Bearish"
        FutureStockNetPosition="636312" FutureStockSignal="Bullish"
        OptionIndexPutOI="272647" OptionIndexCallOI="-283263"
        OptionIndexNetCallOI="-161949" OptionIndexNetPutOI="393961"
        OptionIndexNetOI="-555910" OptionIndexSignal="Bearish"
        OptionStockPutOI="63873" OptionStockCallOI="-129484"
        OptionStockNetPutOI="71292" OptionStockNetCallOI="-122065"
        OptionStockNetOI="-193357" OptionStockSignal="Bearish"/&gt;

        &lt;FIIDIIDataItem Date="05-22-2026 20:12:02" ClientType="Pro"
        FutureIndexLong="82231" FutureIndexShort="48970"
        FutureStockLong="926977" FutureStockShort="535205"
        OptionIndexCallLong="1239552" OptionIndexPutLong="1190072"
        OptionIndexCallShort="1017293" OptionIndexPutShort="1003180"
        OptionStockCallLong="1334772" OptionStockPutLong="1250438"
        OptionStockCallShort="1755188" OptionStockPutShort="1092453"
        TotalLongContracts="6024042" TotalShortContracts="5452288"
        FutureIndexNetPosition="33261" FutureIndexSignal="Bullish"
        FutureStockNetPosition="391772" FutureStockSignal="Bullish"
        OptionIndexPutOI="-49480" OptionIndexCallOI="-14113"
        OptionIndexNetCallOI="222259" OptionIndexNetPutOI="186892"
        OptionIndexNetOI="35367" OptionIndexSignal="Bullish"
        OptionStockPutOI="-84334" OptionStockCallOI="-662735"
        OptionStockNetPutOI="157985" OptionStockNetCallOI="-420416"
        OptionStockNetOI="-578401" OptionStockSignal="Bearish"/&gt;

        &lt;FIIDIIDataItem Date="05-22-2026 20:12:02" ClientType="TOTAL"
        FutureIndexLong="447772" FutureIndexShort="447772"
        FutureStockLong="8677258" FutureStockShort="8677258"
        OptionIndexCallLong="4552468" OptionIndexPutLong="4619504"
        OptionIndexCallShort="4552468" OptionIndexPutShort="4619504"
        OptionStockCallLong="3953451" OptionStockPutLong="2429231"
        OptionStockCallShort="3953451" OptionStockPutShort="2429231"
        TotalLongContracts="24679684" TotalShortContracts="24679684"
        FutureIndexNetPosition="0" FutureIndexSignal="Neutral"
        FutureStockNetPosition="0" FutureStockSignal="Neutral"
        OptionIndexPutOI="67036" OptionIndexCallOI="67036"
        OptionIndexNetCallOI="0" OptionIndexNetPutOI="0" OptionIndexNetOI="0"
        OptionIndexSignal="Neutral" OptionStockPutOI="-1524220"
        OptionStockCallOI="-1524220" OptionStockNetPutOI="0"
        OptionStockNetCallOI="0" OptionStockNetOI="0"
        OptionStockSignal="Neutral"/&gt;

        &lt;/Value&gt;

        &lt;/FIIDIIData&gt;
                    </Tab>
                    <Tab title="CSV">
                        Date,ClientType,FutureIndexLong,FutureIndexShort,FutureStockLong,FutureStockShort,OptionIndexCallLong,OptionIndexPutLong,OptionIndexCallShort,OptionIndexPutShort,OptionStockCallLong,OptionStockPutLong,OptionStockCallShort,OptionStockPutShort,TotalLongContracts,TotalShortContracts,FutureIndexNetPosition,FutureIndexSignal,FutureStockNetPosition,FutureStockSignal,OptionIndexPutOI,OptionIndexCallOI,OptionIndexNetCallOI,OptionIndexNetPutOI,OptionIndexNetOI,OptionIndexSignal,OptionStockPutOI,OptionStockCallOI,OptionStockNetPutOI,OptionStockNetCallOI,OptionStockNetOI,OptionStockSignal
        "05-22-2026
        20:12:02",Client,254325,92661,3132410,274494,2644777,2446220,2718504,3083482,2373278,839786,1343013,1089193,11690797,8601346,161664,Bullish,2857916,Bullish,-198557,364978,-73727,-637262,563535,Bullish,-1533492,-253820,-249407,1030265,1279672,Bullish

        "05-22-2026
        20:12:02",DII,67686,37619,435198,4321198,14281,56708,865,300,1098,30831,488882,10701,605802,4859565,30067,Bullish,-3886000,Bearish,42427,-565,13416,56408,-42992,Bearish,29733,-478181,20130,-487784,-507914,Bearish

        "05-22-2026
        20:12:02",FII,43530,268522,4182673,3546361,653857,926504,815806,532543,244303,308176,366368,236884,6359043,5766484,-224992,Bearish,636312,Bullish,272647,-283263,-161949,393961,-555910,Bearish,63873,-129484,71292,-122065,-193357,Bearish

        "05-22-2026
        20:12:02",Pro,82231,48970,926977,535205,1239552,1190072,1017293,1003180,1334772,1250438,1755188,1092453,6024042,5452288,33261,Bullish,391772,Bullish,-49480,-14113,222259,186892,35367,Bullish,-84334,-662735,157985,-420416,-578401,Bearish

        "05-22-2026
        20:12:02",TOTAL,447772,447772,8677258,8677258,4552468,4619504,4552468,4619504,3953451,2429231,3953451,2429231,24679684,24679684,0,Neutral,0,Neutral,67036,67036,0,0,0,Neutral,-1524220,-1524220,0,0,0,Neutral
                    </Tab>
                    <Tab title="CsvContent">
                        Date,ClientType,FutureIndexLong,FutureIndexShort,FutureStockLong,FutureStockShort,OptionIndexCallLong,OptionIndexPutLong,OptionIndexCallShort,OptionIndexPutShort,OptionStockCallLong,OptionStockPutLong,OptionStockCallShort,OptionStockPutShort,TotalLongContracts,TotalShortContracts,FutureIndexNetPosition,FutureIndexSignal,FutureStockNetPosition,FutureStockSignal,OptionIndexPutOI,OptionIndexCallOI,OptionIndexNetCallOI,OptionIndexNetPutOI,OptionIndexNetOI,OptionIndexSignal,OptionStockPutOI,OptionStockCallOI,OptionStockNetPutOI,OptionStockNetCallOI,OptionStockNetOI,OptionStockSignal
        "05-22-2026
        20:12:02",Client,254325,92661,3132410,274494,2644777,2446220,2718504,3083482,2373278,839786,1343013,1089193,11690797,8601346,161664,Bullish,2857916,Bullish,-198557,364978,-73727,-637262,563535,Bullish,-1533492,-253820,-249407,1030265,1279672,Bullish

        "05-22-2026
        20:12:02",DII,67686,37619,435198,4321198,14281,56708,865,300,1098,30831,488882,10701,605802,4859565,30067,Bullish,-3886000,Bearish,42427,-565,13416,56408,-42992,Bearish,29733,-478181,20130,-487784,-507914,Bearish

        "05-22-2026
        20:12:02",FII,43530,268522,4182673,3546361,653857,926504,815806,532543,244303,308176,366368,236884,6359043,5766484,-224992,Bearish,636312,Bullish,272647,-283263,-161949,393961,-555910,Bearish,63873,-129484,71292,-122065,-193357,Bearish

        "05-22-2026
        20:12:02",Pro,82231,48970,926977,535205,1239552,1190072,1017293,1003180,1334772,1250438,1755188,1092453,6024042,5452288,33261,Bullish,391772,Bullish,-49480,-14113,222259,186892,35367,Bullish,-84334,-662735,157985,-420416,-578401,Bearish

        "05-22-2026
        20:12:02",TOTAL,447772,447772,8677258,8677258,4552468,4619504,4552468,4619504,3953451,2429231,3953451,2429231,24679684,24679684,0,Neutral,0,Neutral,67036,67036,0,0,0,Neutral,-1524220,-1524220,0,0,0,Neutral

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
          
      tags:
        - Corporate Data APIs (RESTful)/FII DII Activity
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
          description: 'Example: BSE'
          required: false
          example: NSE
          schema:
            type: string
        - name: clientType
          in: query
          description: 'Client type: FII/ DII/ Pro/ Client'
          required: false
          example: FII
          schema:
            type: string
        - name: from
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example: '1774631161'
          schema:
            type: string
        - name: to
          in: query
          description: >-
            Available values : Epoch eg: 1740493802, String eg:02-25-2025
            21:00:02
          required: false
          example: '1774631161'
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
      x-apidog-folder: Corporate Data APIs (RESTful)/FII DII Activity
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-37773825-run
components:
  schemas: {}
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
