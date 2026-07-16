# GlobalDataFeeds — Fundamental Data API — Endpoints 6 (Group A/B Statistics, Top 5 Gainers/Losers, Index Highlights)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetStatisticsGroupAbCompanies — /getstatisticsgroupabcompanies-15575596e0
2. GetTop5GainersLosers — /gettop5gainerslosers-15575597e0
3. GetIndexHighlights — /getindexhighlights-15575598e0

---

## GetStatisticsGroupAbCompanies

> Source: https://docs.globaldatafeeds.in/getstatisticsgroupabcompanies-15575596e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetStatisticsGroupAbCompanies:
    get:
      summary: GetStatisticsGroupAbCompanies
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetStatisticsGroupAbCompanies** returns statistical data for Group A
        and B companies, providing key metrics such as traded volume, turnover,
        market capitalization, and price movements. This data helps investors
        and analysts assess the performance and liquidity of companies
        categorized under Group A and B on the stock exchange.




        ### What is returned?


        ScripGroupCode, IssuesTraded, Advances, Declines, Unchanged, Date,
        ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
                            "Value": [
                              {
                                "ScripGroupCode": "A ",
                                "IssuesTraded": 718,
                                "Advances": 249,
                                "Declines": 465,
                                "Unchanged": 4,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:32",
                                "SavedAt": "02-25-2025 21:02:32"
                              },
                              {
                                "ScripGroupCode": "B ",
                                "IssuesTraded": 1564,
                                "Advances": 591,
                                "Declines": 915,
                                "Unchanged": 58,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:32",
                                "SavedAt": "02-25-2025 21:02:32"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;StatisticsGroupAbCompaniesResult&gt;
                        &lt;Value&gt;
                        &lt;StatisticsGroupAbCompanies ScripGroupCode="A " IssuesTraded="718" Advances="249" Declines="465" Unchanged="4" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:32" SavedAt="02-25-2025 21:02:32"/&gt;
                        &lt;StatisticsGroupAbCompanies ScripGroupCode="B " IssuesTraded="1564" Advances="591" Declines="915" Unchanged="58" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:32" SavedAt="02-25-2025 21:02:32"/&gt;
                        &lt;/Value&gt;
                        &lt;/StatisticsGroupAbCompaniesResult&gt;
                    </Tab>
                    <Tab title="Csv">
                        ScripGroupCode,IssuesTraded,Advances,Declines,Unchanged,Date,ReceivedAt,SavedAt
                        A ,718,249,465,4,"02-25-2025","02-25-2025 21:02:32","02-25-2025 21:02:32"
                        B ,1564,591,915,58,"02-25-2025","02-25-2025 21:02:32","02-25-2025 21:02:32"
                    </Tab>
                    <Tab title="CsvContent">
                        ScripGroupCode,IssuesTraded,Advances,Declines,Unchanged,Date,ReceivedAt,SavedAt
                        A ,718,249,465,4,"02-25-2025","02-25-2025 21:02:32","02-25-2025 21:02:32"
                        B ,1564,591,915,58,"02-25-2025","02-25-2025 21:02:32","02-25-2025 21:02:32"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
          
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
        - name: groups
          in: query
          description: ''
          required: false
          example: A+B
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
                $ref: '#/components/schemas/QeStatisticsGroupAbCompaniesResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575596-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetTop5GainersLosers

> Source: https://docs.globaldatafeeds.in/gettop5gainerslosers-15575597e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetTop5GainersLosers:
    get:
      summary: GetTop5GainersLosers
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetTopGainersLosers** returns the list of top gaining and losing
        stocks, providing details such as percentage change, traded volume,
        turnover, and closing prices. This data helps investors and analysts
        identify market trends, track stock performance, and assess volatility.




        ## What is returned?


        ScripCode, ScripName, ScripGroup, CurrentClosing, PreviousClosing,
        PercentageFluctation, Quantity, Turnover, IsLoser, Date, ReceivedAt,
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
                                "ScripCode": 750958,
                                "ScripName": "THANG-RE    ",
                                "ScripGroup": "B ",
                                "CurrentClosing": 203.9,
                                "PreviousClosing": 145.65,
                                "PercentageFluctation": 39.99,
                                "Quantity": 932,
                                "Turnover": 1.79,
                                "IsLoser": false,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 533158,
                                "ScripName": "THANGAMAYIL ",
                                "ScripGroup": "B ",
                                "CurrentClosing": 1861.85,
                                "PreviousClosing": 1551.55,
                                "PercentageFluctation": 20,
                                "Quantity": 10651,
                                "Turnover": 188.13,
                                "IsLoser": false,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 540210,
                                "ScripName": "HEADSUP     ",
                                "ScripGroup": "B ",
                                "CurrentClosing": 13.36,
                                "PreviousClosing": 11.14,
                                "PercentageFluctation": 19.93,
                                "Quantity": 216028,
                                "Turnover": 28.56,
                                "IsLoser": false,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 541299,
                                "ScripName": "DLCL        ",
                                "ScripGroup": "M ",
                                "CurrentClosing": 14.52,
                                "PreviousClosing": 12.12,
                                "PercentageFluctation": 19.8,
                                "Quantity": 4000,
                                "Turnover": 0.58,
                                "IsLoser": false,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 543350,
                                "ScripName": "VIJAYA      ",
                                "ScripGroup": "A ",
                                "CurrentClosing": 1065.8,
                                "PreviousClosing": 923.35,
                                "PercentageFluctation": 15.43,
                                "Quantity": 189163,
                                "Turnover": 1933.34,
                                "IsLoser": false,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 511696,
                                "ScripName": "CHARTERED CA",
                                "ScripGroup": "X ",
                                "CurrentClosing": 212.6,
                                "PreviousClosing": 258.35,
                                "PercentageFluctation": -17.71,
                                "Quantity": 6406,
                                "Turnover": 13.77,
                                "IsLoser": true,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 537707,
                                "ScripName": "ETT LTD     ",
                                "ScripGroup": "X ",
                                "CurrentClosing": 14.89,
                                "PreviousClosing": 18.03,
                                "PercentageFluctation": -17.42,
                                "Quantity": 344067,
                                "Turnover": 53.95,
                                "IsLoser": true,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 532124,
                                "ScripName": "RELIAB VEN  ",
                                "ScripGroup": "X ",
                                "CurrentClosing": 19.49,
                                "PreviousClosing": 22.37,
                                "PercentageFluctation": -12.87,
                                "Quantity": 41732,
                                "Turnover": 8.68,
                                "IsLoser": true,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 543108,
                                "ScripName": "UTCRFS2RQP  ",
                                "ScripGroup": "F ",
                                "CurrentClosing": 420.22,
                                "PreviousClosing": 466.91,
                                "PercentageFluctation": -10,
                                "Quantity": 1,
                                "Turnover": 0,
                                "IsLoser": true,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              },
                              {
                                "ScripCode": 542817,
                                "ScripName": "NIEHSPI     ",
                                "ScripGroup": "B ",
                                "CurrentClosing": 32.18,
                                "PreviousClosing": 35.75,
                                "PercentageFluctation": -9.99,
                                "Quantity": 987,
                                "Turnover": 0.34,
                                "IsLoser": true,
                                "Date": "02-25-2025",
                                "ReceivedAt": "02-25-2025 21:02:33",
                                "SavedAt": "02-25-2025 21:02:33"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;TopGainersLosersResult&gt;
                        &lt;Value&gt;
                        &lt;TopGainersLosers ScripCode="750958" ScripName="THANG-RE " ScripGroup="B " CurrentClosing="203.9" PreviousClosing="145.65" PercentageFluctation="39.99" Quantity="932" Turnover="1.79" IsLoser="false" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="533158" ScripName="THANGAMAYIL " ScripGroup="B " CurrentClosing="1861.85" PreviousClosing="1551.55" PercentageFluctation="20" Quantity="10651" Turnover="188.13" IsLoser="false" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="540210" ScripName="HEADSUP " ScripGroup="B " CurrentClosing="13.36" PreviousClosing="11.14" PercentageFluctation="19.93" Quantity="216028" Turnover="28.56" IsLoser="false" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="541299" ScripName="DLCL " ScripGroup="M " CurrentClosing="14.52" PreviousClosing="12.12" PercentageFluctation="19.8" Quantity="4000" Turnover="0.58" IsLoser="false" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="543350" ScripName="VIJAYA " ScripGroup="A " CurrentClosing="1065.8" PreviousClosing="923.35" PercentageFluctation="15.43" Quantity="189163" Turnover="1933.34" IsLoser="false" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="511696" ScripName="CHARTERED CA" ScripGroup="X " CurrentClosing="212.6" PreviousClosing="258.35" PercentageFluctation="-17.71" Quantity="6406" Turnover="13.77" IsLoser="true" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="537707" ScripName="ETT LTD " ScripGroup="X " CurrentClosing="14.89" PreviousClosing="18.03" PercentageFluctation="-17.42" Quantity="344067" Turnover="53.95" IsLoser="true" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="532124" ScripName="RELIAB VEN " ScripGroup="X " CurrentClosing="19.49" PreviousClosing="22.37" PercentageFluctation="-12.87" Quantity="41732" Turnover="8.68" IsLoser="true" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="543108" ScripName="UTCRFS2RQP " ScripGroup="F " CurrentClosing="420.22" PreviousClosing="466.91" PercentageFluctation="-10" Quantity="1" Turnover="0" IsLoser="true" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;TopGainersLosers ScripCode="542817" ScripName="NIEHSPI " ScripGroup="B " CurrentClosing="32.18" PreviousClosing="35.75" PercentageFluctation="-9.99" Quantity="987" Turnover="0.34" IsLoser="true" Date="02-25-2025" ReceivedAt="02-25-2025 21:02:33" SavedAt="02-25-2025 21:02:33"/&gt;
                        &lt;/Value&gt;
                        &lt;/TopGainersLosersResult&gt;
                    </Tab>
                    <Tab title="Csv">
                        ScripCode,ScripName,ScripGroup,CurrentClosing,PreviousClosing,PercentageFluctation,Quantity,Turnover,IsLoser,Date,ReceivedAt,SavedAt
                        750958,THANG-RE    ,B ,203.9,145.65,39.99,932,1.79,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        533158,THANGAMAYIL ,B ,1861.85,1551.55,20,10651,188.13,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        540210,HEADSUP     ,B ,13.36,11.14,19.93,216028,28.56,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        541299,DLCL        ,M ,14.52,12.12,19.8,4000,0.58,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        543350,VIJAYA      ,A ,1065.8,923.35,15.43,189163,1933.34,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        511696,CHARTERED CA,X ,212.6,258.35,-17.71,6406,13.77,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        537707,ETT LTD     ,X ,14.89,18.03,-17.42,344067,53.95,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        532124,RELIAB VEN  ,X ,19.49,22.37,-12.87,41732,8.68,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        543108,UTCRFS2RQP  ,F ,420.22,466.91,-10,1,0,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        542817,NIEHSPI     ,B ,32.18,35.75,-9.99,987,0.34,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                    </Tab>
                    <Tab title="CsvContent">
                        ScripCode,ScripName,ScripGroup,CurrentClosing,PreviousClosing,PercentageFluctation,Quantity,Turnover,IsLoser,Date,ReceivedAt,SavedAt
                        750958,THANG-RE    ,B ,203.9,145.65,39.99,932,1.79,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        533158,THANGAMAYIL ,B ,1861.85,1551.55,20,10651,188.13,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        540210,HEADSUP     ,B ,13.36,11.14,19.93,216028,28.56,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        541299,DLCL        ,M ,14.52,12.12,19.8,4000,0.58,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        543350,VIJAYA      ,A ,1065.8,923.35,15.43,189163,1933.34,False,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        511696,CHARTERED CA,X ,212.6,258.35,-17.71,6406,13.77,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        537707,ETT LTD     ,X ,14.89,18.03,-17.42,344067,53.95,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        532124,RELIAB VEN  ,X ,19.49,22.37,-12.87,41732,8.68,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        543108,UTCRFS2RQP  ,F ,420.22,466.91,-10,1,0,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"
                        542817,NIEHSPI     ,B ,32.18,35.75,-9.99,987,0.34,True,"02-25-2025","02-25-2025 21:02:33","02-25-2025 21:02:33"

                        Note: 
                        1. The file containing above values in CSV format will be returned. 
                        2. To see it working, copy-paste the request in browser.
                    </Tab>
            </Tabs>
          </div>
          
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
        - name: type
          in: query
          description: 'Type: GainersLosers, Gainers, Losers'
          required: true
          example: GainersLosers
          schema:
            $ref: '#/components/schemas/GainersLosersRequest'
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
                $ref: '#/components/schemas/QeTopGainersLosersResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575597-run
components:
  schemas:
    GainersLosersRequest:
      enum:
        - GainersLosers
        - Gainers
        - Losers
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetIndexHighlights

> Source: https://docs.globaldatafeeds.in/getindexhighlights-15575598e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetIndexHighlights:
    get:
      summary: GetIndexHighlights
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetIndexHighlights** returns the summary of key stock market indices,
        providing data on index performance, percentage change, highest and
        lowest levels, traded volume, and turnover. This helps investors and
        analysts track overall market trends and benchmark index movements.




        ### What is returned?


        IndexName, PrevClose, Opening, High, Low, Closing, PrevCloseChangePts,
        PrevCloseChangePct, Date, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)




        ### Sample Response


        | Response Type | Sample
        Response                                                                                        
        |

        | :------------ |
        :------------------------------------------------------------------------------------------------------
        |

        | JSON          | [Download JSON
        Response](https://globaldatafeeds.in/resources/GFDL_GetIndexHighlightsResponse_JSON.zip)
        |

        | XML           | [Download XML
        Response](https://globaldatafeeds.in/resources/GFDL_GetIndexHighlightsResponse_XML.zip)  
        |

        | CSV           | [Download CSV
        Response](https://globaldatafeeds.in/resources/GFDL_GetIndexHighlightsResponse_CSV.zip)  
        |

        | CSVContent    | The file containing values in CSV format will be
        returned                                               |


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
                $ref: '#/components/schemas/QeIndexHighlightResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575598-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
