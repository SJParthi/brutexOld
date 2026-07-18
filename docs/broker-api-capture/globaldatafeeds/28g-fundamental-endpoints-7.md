# GlobalDataFeeds — Fundamental Data API — Endpoints 7 (Total Trade Highlights, Scrip Group Trade Highlights, Top 15 Turnover)

Gap-fill capture (2026-07-15) of docs.globaldatafeeds.in pages indexed in `28-fundamental-data-api.md` but not previously archived. Fetched verbatim via `<url>.md` (Apidog raw-markdown export; OpenAPI 3.0.1 spec per endpoint). REST server: `https://test.lisuns.com:4532`.

Pages in this file:
1. GetTotalTradeHighlights — /gettotaltradehighlights-15575599e0
2. GetScripGroupTradeHighlights — /getscripgrouptradehighlights-15575600e0
3. GetTurnoverDetailsOfTop15ScripsofAgroup — /getturnoverdetailsoftop15scripsofagroup-15575601e0

---

## GetTotalTradeHighlights

> Source: https://docs.globaldatafeeds.in/gettotaltradehighlights-15575599e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetTotalTradeHighlights:
    get:
      summary: GetTotalTradeHighlights
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetTotalTradeHighlights** returns the summary of total trade activity
        across the stock exchange, providing key statistics such as total traded
        volume, turnover, number of transactions, and market participation
        trends. This data helps investors and analysts assess overall market
        liquidity and trading activity.




        ### What is returned?


        TradeHighlights, ScripsNo, TradesNo, SharesVol, TurnoverRsCr, Date,
        ReceivedAt, SavedAt, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
                            "Value": [
                              {
                                "TradeHighlights": "Total",
                                "ScripsNo": 4471,
                                "TradesNo": 2802025,
                                "SharesVol": 43.87,
                                "TurnoverRsCr": 4179,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;TotalTradeHighlightResult&gt;
                        &lt;Value&gt;
                        &lt;TotalTradeHighlight Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"&gt;
                        &lt;TradeHighlights&gt;Total&lt;/TradeHighlights&gt;
                        &lt;ScripsNo&gt;4471&lt;/ScripsNo&gt;
                        &lt;TradesNo&gt;2802025&lt;/TradesNo&gt;
                        &lt;SharesVol&gt;43.87&lt;/SharesVol&gt;
                        &lt;TurnoverRsCr&gt;4179&lt;/TurnoverRsCr&gt;
                        &lt;/TotalTradeHighlight&gt;
                        &lt;/Value&gt;
                        &lt;/TotalTradeHighlightResult&gt;
                    </Tab>
                    <Tab title="Csv">
                        TradeHighlights,ScripsNo,TradesNo,SharesVol,TurnoverRsCr,Date,ReceivedAt,SavedAt,ReceivedAt,SavedAt
                        Total,4471,2802025,43.87,4179,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                    </Tab>
                    <Tab title="CsvContent">
                        TradeHighlights,ScripsNo,TradesNo,SharesVol,TurnoverRsCr,Date,ReceivedAt,SavedAt,ReceivedAt,SavedAt
                        Total,4471,2802025,43.87,4179,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"

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
                $ref: '#/components/schemas/QeTotalTradeHighlightResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575599-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetScripGroupTradeHighlights

> Source: https://docs.globaldatafeeds.in/getscripgrouptradehighlights-15575600e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetScripGroupTradeHighlights:
    get:
      summary: GetScripGroupTradeHighlights
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetScripGroupTradeHighlights** returns the trading summary for
        different scrip groups, providing data on total traded volume, turnover,
        number of trades, and price movements within specific stock categories.
        This helps investors and analysts compare the performance and liquidity
        of various scrip groups.




        ### What is returned?


        ScripGroupCode, ScripsNo, TradesNo, VolShares, TurnoverRs,
        TotTurnoverPct, Date, ReceivedAt, SavedAt


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
                                "ScripsNo": 718,
                                "TradesNo": 1825355,
                                "VolShares": 13.99,
                                "TurnoverRs": 3017.24,
                                "TotTurnoverPct": 72.2,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "B ",
                                "ScripsNo": 1270,
                                "TradesNo": 670285,
                                "VolShares": 9.27,
                                "TurnoverRs": 640.03,
                                "TotTurnoverPct": 15.32,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "E ",
                                "ScripsNo": 24,
                                "TradesNo": 37567,
                                "VolShares": 0.39,
                                "TurnoverRs": 30.22,
                                "TotTurnoverPct": 0.723,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "F ",
                                "ScripsNo": 317,
                                "TradesNo": 3687,
                                "VolShares": 0.22,
                                "TurnoverRs": 93.53,
                                "TotTurnoverPct": 2.238,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "IF",
                                "ScripsNo": 12,
                                "TradesNo": 6563,
                                "VolShares": 0.12,
                                "TurnoverRs": 15.78,
                                "TotTurnoverPct": 0.377,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "P ",
                                "ScripsNo": 14,
                                "TradesNo": 44,
                                "VolShares": 0,
                                "TurnoverRs": 0.04,
                                "TotTurnoverPct": 0.001,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "SME",
                                "ScripsNo": 211,
                                "TradesNo": 5108,
                                "VolShares": 0.98,
                                "TurnoverRs": 103.81,
                                "TotTurnoverPct": 2.484,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "T ",
                                "ScripsNo": 245,
                                "TradesNo": 31464,
                                "VolShares": 2.04,
                                "TurnoverRs": 125.31,
                                "TotTurnoverPct": 2.999,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "TS",
                                "ScripsNo": 2,
                                "TradesNo": 20,
                                "VolShares": 0,
                                "TurnoverRs": 0.21,
                                "TotTurnoverPct": 0.005,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "X ",
                                "ScripsNo": 1053,
                                "TradesNo": 149797,
                                "VolShares": 9.18,
                                "TurnoverRs": 98.09,
                                "TotTurnoverPct": 2.347,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "XT",
                                "ScripsNo": 490,
                                "TradesNo": 62153,
                                "VolShares": 6.99,
                                "TurnoverRs": 46.64,
                                "TotTurnoverPct": 1.116,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "Z ",
                                "ScripsNo": 56,
                                "TradesNo": 9487,
                                "VolShares": 0.67,
                                "TurnoverRs": 4.9,
                                "TotTurnoverPct": 0.117,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripGroupCode": "ZP",
                                "ScripsNo": 1,
                                "TradesNo": 2,
                                "VolShares": 0,
                                "TurnoverRs": 0,
                                "TotTurnoverPct": 0,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;ScripGroupTradeHighlightResult&gt;
                        &lt;Value&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="A " ScripsNo="718" TradesNo="1825355" VolShares="13.99" TurnoverRs="3017.24" TotTurnoverPct="72.2" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="B " ScripsNo="1270" TradesNo="670285" VolShares="9.27" TurnoverRs="640.03" TotTurnoverPct="15.32" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="E " ScripsNo="24" TradesNo="37567" VolShares="0.39" TurnoverRs="30.22" TotTurnoverPct="0.723" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="F " ScripsNo="317" TradesNo="3687" VolShares="0.22" TurnoverRs="93.53" TotTurnoverPct="2.238" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="IF" ScripsNo="12" TradesNo="6563" VolShares="0.12" TurnoverRs="15.78" TotTurnoverPct="0.377" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="P " ScripsNo="14" TradesNo="44" VolShares="0" TurnoverRs="0.04" TotTurnoverPct="0.001" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="SME" ScripsNo="211" TradesNo="5108" VolShares="0.98" TurnoverRs="103.81" TotTurnoverPct="2.484" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="T " ScripsNo="245" TradesNo="31464" VolShares="2.04" TurnoverRs="125.31" TotTurnoverPct="2.999" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="TS" ScripsNo="2" TradesNo="20" VolShares="0" TurnoverRs="0.21" TotTurnoverPct="0.005" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="X " ScripsNo="1053" TradesNo="149797" VolShares="9.18" TurnoverRs="98.09" TotTurnoverPct="2.347" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="XT" ScripsNo="490" TradesNo="62153" VolShares="6.99" TurnoverRs="46.64" TotTurnoverPct="1.116" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="Z " ScripsNo="56" TradesNo="9487" VolShares="0.67" TurnoverRs="4.9" TotTurnoverPct="0.117" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;ScripGroupTradeHighlight ScripGroupCode="ZP" ScripsNo="1" TradesNo="2" VolShares="0" TurnoverRs="0" TotTurnoverPct="0" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;/Value&gt;
                        &lt;/ScripGroupTradeHighlightResult&gt;
                    </Tab>
                    <Tab title="Csv">
                        ScripGroupCode,ScripsNo,TradesNo,VolShares,TurnoverRs,TotTurnoverPct,Date,ReceivedAt,SavedAt
                        A ,718,1825355,13.99,3017.24,72.2,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        B ,1270,670285,9.27,640.03,15.32,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        E ,24,37567,0.39,30.22,0.723,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        F ,317,3687,0.22,93.53,2.238,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        IF,12,6563,0.12,15.78,0.377,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        P ,14,44,0,0.04,0.001,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        SME,211,5108,0.98,103.81,2.484,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        T ,245,31464,2.04,125.31,2.999,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        TS,2,20,0,0.21,0.005,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        X ,1053,149797,9.18,98.09,2.347,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        XT,490,62153,6.99,46.64,1.116,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        Z ,56,9487,0.67,4.9,0.117,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ZP,1,2,0,0,0,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                    </Tab>
                    <Tab title="CsvContent">
                        A ,718,1825355,13.99,3017.24,72.2,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        B ,1270,670285,9.27,640.03,15.32,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        E ,24,37567,0.39,30.22,0.723,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        F ,317,3687,0.22,93.53,2.238,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        IF,12,6563,0.12,15.78,0.377,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        P ,14,44,0,0.04,0.001,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        SME,211,5108,0.98,103.81,2.484,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        T ,245,31464,2.04,125.31,2.999,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        TS,2,20,0,0.21,0.005,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        X ,1053,149797,9.18,98.09,2.347,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        XT,490,62153,6.99,46.64,1.116,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        Z ,56,9487,0.67,4.9,0.117,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ZP,1,2,0,0,0,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"

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
                $ref: '#/components/schemas/QeScripGroupTradeHighlightResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575600-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```

---

## GetTurnoverDetailsOfTop15ScripsofAgroup

> Source: https://docs.globaldatafeeds.in/getturnoverdetailsoftop15scripsofagroup-15575601e0.md — captured 2026-07-15

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /GetTurnoverDetailsOfTop15ScripsofAgroup:
    get:
      summary: GetTurnoverDetailsOfTop15ScripsofAgroup
      deprecated: false
      description: >-
        <div style="display: flex; align-items: center; justify-content:
        space-between;">
          <div>
            >▶️ **'Try It' now ! Actual data is returned as on 27th February 2025**            
          </div>
          <img src="https://api.apidog.com/api/v1/projects/866176/resources/353145/image-preview" alt="pointer.gif" width="80" />
        </div>


        **GetQeTurnoverDetailsOfTop15ScripsOfAGroup** returns the turnover
        details of the top 15 scrips in Group A, providing data on total traded
        volume, turnover value, and price movements. This helps investors and
        analysts identify the most actively traded stocks in Group A and assess
        their market impact.




        ## What is returned?


        ScripName, SharesNo, ValueRsCr, Date, ReceivedAt, SavedAt


        For details, please see [glossary](/glossary-923501m0)


        ### **Sample Response**


        <div style="border: 1px solid #ccc; background-color: #f9f9f9; padding:
        10px; border-radius: 5px;">
            <Tabs style="background-color:lightgrey;">
                    <Tab title="JSON">
                        {
                            "Value": [
                              {
                                "ScripName": "ULTRATECH CM",
                                "SharesNo": 2.11,
                                "ValueRsCr": 219.48,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "POLYCAB     ",
                                "SharesNo": 2.8,
                                "ValueRsCr": 136.87,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "CREDITACC   ",
                                "SharesNo": 9.58,
                                "ValueRsCr": 93.16,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "HDFC BANK   ",
                                "SharesNo": 3.42,
                                "ValueRsCr": 58.11,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "INOXINDIA   ",
                                "SharesNo": 5.78,
                                "ValueRsCr": 57.47,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "TATA MOTORS ",
                                "SharesNo": 8.8,
                                "ValueRsCr": 57.39,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "BAJFINANCE  ",
                                "SharesNo": 0.65,
                                "ValueRsCr": 56.72,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "STATE BANK  ",
                                "SharesNo": 6.25,
                                "ValueRsCr": 44.22,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "ADANI POWER ",
                                "SharesNo": 8.26,
                                "ValueRsCr": 41.43,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "KEI INDUST. ",
                                "SharesNo": 1.26,
                                "ValueRsCr": 39.22,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "ZOMATO      ",
                                "SharesNo": 13.84,
                                "ValueRsCr": 31.43,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "BAJAJ FINSE ",
                                "SharesNo": 1.61,
                                "ValueRsCr": 30.9,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "ADANIGREEN  ",
                                "SharesNo": 3.62,
                                "ValueRsCr": 30,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "ICICI BANK  ",
                                "SharesNo": 2.4,
                                "ValueRsCr": 29.42,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              },
                              {
                                "ScripName": "MANAP FIN   ",
                                "SharesNo": 13.52,
                                "ValueRsCr": 28.17,
                                "Date": "02-27-2025",
                                "ReceivedAt": "02-27-2025 21:02:02",
                                "SavedAt": "02-27-2025 21:02:02"
                              }
                            ]
                          }
                    </Tab>
                    <Tab title="XML">
                        &lt;Top15TurnoverDetailsResult&gt;
                        &lt;Value&gt;
                        &lt;Top15TurnoverDetails ScripName="ULTRATECH CM" SharesNo="2.11" ValueRsCr="219.48" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="POLYCAB " SharesNo="2.8" ValueRsCr="136.87" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="CREDITACC " SharesNo="9.58" ValueRsCr="93.16" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="HDFC BANK " SharesNo="3.42" ValueRsCr="58.11" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="INOXINDIA " SharesNo="5.78" ValueRsCr="57.47" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="TATA MOTORS " SharesNo="8.8" ValueRsCr="57.39" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="BAJFINANCE " SharesNo="0.65" ValueRsCr="56.72" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="STATE BANK " SharesNo="6.25" ValueRsCr="44.22" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="ADANI POWER " SharesNo="8.26" ValueRsCr="41.43" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="KEI INDUST. " SharesNo="1.26" ValueRsCr="39.22" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="ZOMATO " SharesNo="13.84" ValueRsCr="31.43" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="BAJAJ FINSE " SharesNo="1.61" ValueRsCr="30.9" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="ADANIGREEN " SharesNo="3.62" ValueRsCr="30" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="ICICI BANK " SharesNo="2.4" ValueRsCr="29.42" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;Top15TurnoverDetails ScripName="MANAP FIN " SharesNo="13.52" ValueRsCr="28.17" Date="02-27-2025" ReceivedAt="02-27-2025 21:02:02" SavedAt="02-27-2025 21:02:02"/&gt;
                        &lt;/Value&gt;
                        &lt;/Top15TurnoverDetailsResult&gt;
                    </Tab>
                    <Tab title="Csv">
                        ScripName,SharesNo,ValueRsCr,Date,ReceivedAt,SavedAt
                        ULTRATECH CM,2.11,219.48,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        POLYCAB     ,2.8,136.87,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        CREDITACC   ,9.58,93.16,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        HDFC BANK   ,3.42,58.11,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        INOXINDIA   ,5.78,57.47,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        TATA MOTORS ,8.8,57.39,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        BAJFINANCE  ,0.65,56.72,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        STATE BANK  ,6.25,44.22,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ADANI POWER ,8.26,41.43,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        KEI INDUST. ,1.26,39.22,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ZOMATO      ,13.84,31.43,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        BAJAJ FINSE ,1.61,30.9,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ADANIGREEN  ,3.62,30,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ICICI BANK  ,2.4,29.42,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        MANAP FIN   ,13.52,28.17,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                    </Tab>
                    <Tab title="CsvContent">
                        ScripName,SharesNo,ValueRsCr,Date,ReceivedAt,SavedAt
                        ULTRATECH CM,2.11,219.48,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        POLYCAB     ,2.8,136.87,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        CREDITACC   ,9.58,93.16,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        HDFC BANK   ,3.42,58.11,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        INOXINDIA   ,5.78,57.47,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        TATA MOTORS ,8.8,57.39,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        BAJFINANCE  ,0.65,56.72,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        STATE BANK  ,6.25,44.22,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ADANI POWER ,8.26,41.43,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        KEI INDUST. ,1.26,39.22,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ZOMATO      ,13.84,31.43,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        BAJAJ FINSE ,1.61,30.9,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ADANIGREEN  ,3.62,30,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        ICICI BANK  ,2.4,29.42,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
                        MANAP FIN   ,13.52,28.17,"02-27-2025","02-27-2025 21:02:02","02-27-2025 21:02:02"
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
                $ref: '#/components/schemas/QeTop15TurnoverDetailsResult'
          headers: {}
          x-apidog-name: OK
      security: []
      x-apidog-folder: Other Data APIs
      x-apidog-status: released
      x-run-in-apidog: https://app.apidog.com/web/project/866176/apis/api-15575601-run
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
      x-apidog-ignore-properties: []
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
      x-apidog-ignore-properties: []
      x-apidog-folder: ''
  securitySchemes: {}
servers:
  - url: https://test.lisuns.com:4532
    description: https://test.lisuns.com:4532
security: []

```
