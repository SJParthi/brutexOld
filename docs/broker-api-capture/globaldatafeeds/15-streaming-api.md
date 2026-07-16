# GlobalDataFeeds Streaming API (WebSocket)

WebSocket Streaming API — streams Realtime data (StreamAllSymbols) and Realtime Snapshot data (StreamAllSnapshots) of all symbols enabled for the API key.

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/streaming_api/stream-all-symbols/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/streaming_api/streamallsnapshots/

---

## StreamAllSymbols (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/streaming_api/stream-all-symbols/ — last updated 2026-05-04T14:39:18 — captured 2026-07-15

No additional function is required to be enabled for this functionality Only the Authentication need to be done . It will send Realtime data of all symbols of those exchanges which are enabled for that API key.

### Authenticate : Authenticates the user

**What is returned ?**

Nothing. This function authenticates the user and sends response accordingly

**Sample Request (JavaScript)**

```javascript
 function Authenticate()
  {
     writeToScreen("Authenticate");
    var message = 
    {
       MessageType: "Authenticate",
       Password: accessKey
     };
    doSend(message);
  }
```

**JSON Response**

```json
{"Complete":true,"Message":"Welcome!","MessageType":"AuthenticateResult"}"
```

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open**,**High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close) |
| LastTradeTime, ServerTradeTime : These values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data in JSON format for exchanges which are enabled**

```text
RESPONSE: {"T":"Batch", "data":[{"E":"NFO","I":"OPTSTK_SBIN_28OCT2025_CE_860","LTT":1759831039,"STT":1759831040,"ATP":22.22,"BP":20.15,"BQ":15000,"C":25.95,"H":26.5,"L":19.25,"LTP":20.15,"LTQ":0,"O":26.45,"OI":1998000,"QL":750,"SP":20.25,"SQ":750,"TTQ":3099000,"VAL":68859780,"PO":false,"PC":-5.8,"PCP":-22.35,"OIC":232500,"T":"RT"},{"E":"NFO","I":"OPTSTK_OFSS_28OCT2025_CE_9200","LTT":1759831039,"STT":1759831040,"ATP":305.12,"BP":378,"BQ":75,"C":249.55,"H":383,"L":250,"LTP":377.5,"LTQ":0,"O":251.35,"OI":19950,"QL":75,"SP":380.5,"SQ":75,"TTQ":199050,"VAL":60734136,"PO":false,"PC":127.95,"PCP":51.27,"OIC":-3375,"T":"RT"},{"E":"NFO","I":"OPTSTK_SUPREMEIND_28OCT2025_CE_4300","LTT":1759831039,"STT":1759831040,"ATP":120.85,"BP":104,"BQ":175,"C":84.05,"H":144.25,"L":91.8,"LTP":105,"LTQ":175,"O":92,"OI":50575,"QL":175,"SP":105.8,"SQ":175,"TTQ":281575,"VAL":34028338.75,"PO":false,"PC":20.95,"PCP":24.93,"OIC":22050,"T":"RT"},{"E":"NFO","I":"OPTIDX_NIFTYNXT50_28OCT2025_CE_61900","LTT":1759831040,"STT":1759831040,"ATP":0,"BP":6224.4,"BQ":575,"C":6923.3,"H":0,"L":0,"LTP":0,"LTQ":0,"O":0,"OI":0,"QL":25,"SP":7893.2,"SQ":50,"TTQ":0,"VAL":0,"PO":false,"PC":-6923.3,"PCP":-100,"OIC":0,"T":"RT"},{"E":"NFO","I":"OPTIDX_MIDCPNIFTY_28OCT2025_PE_12300","LTT":1759831039,"STT":1759831040,"ATP":20.46,"BP":19.55,"BQ":140,"C":27.6,"H":26.25,"L":16.75,"LTP":20.25,"LTQ":140,"O":25.85,"OI":206500,"QL":140,"SP":20.4,"SQ":700,"TTQ":574140,"VAL":11746904.4,"PO":false,"PC":-7.35,"PCP":-26.63,"OIC":-25900,"T":"RT"},{"E":"NFO","I":"OPTIDX_NIFTY_04NOV2025_CE_24650","LTT":1759831039,"STT":1759831040,"ATP":0,"BP":302.4,"BQ":75,"C":723.8,"H":0,"L":0,"LTP":723.8,"LTQ":0,"O":0,"OI":0,"QL":75,"SP":692.35,"SQ":75,"TTQ":0,"VAL":0,"PO":false,"PC":0,"PCP":0,"OIC":-525,"T":"RT"},{"E":"NFO","I":"OPTIDX_NIFTYNXT50_28OCT2025_CE_62300","LTT":1759831040,"STT":1759831040,"ATP":0,"BP":5861.85,"BQ":50,"C":6617.85,"H":0,"L":0,"LTP":0,"LTQ":0,"O":0,"OI":0,"QL":25,"SP":7470.65,"SQ":50,"TTQ":0,"VAL":0,"PO":false,"PC":-6617.85,"PCP":-100,"OIC":0,"T":"RT"},{"E":"NFO","I":"OPTSTK_CDSL_28OCT2025_PE_1560","LTT":1759831039,"STT":1759831040,"ATP":35.16,"BP":37.15,"BQ":475,"C":59.6,"H":59.55,"L":29.8,"LTP":37.4,"LTQ":0,"O":58,"OI":141075,"QL":475,"SP":37.5,"SQ":475,"TTQ":1582700,"VAL":55647732,"PO":false,"PC":-22.2,"PCP":-37.25,"OIC":85025,"T":"RT"},{"E":"NFO","I":"OPTIDX_BANKNIFTY_28OCT2025_PE_60700","LTT":1759831040,"STT":1759831040,"ATP":0,"BP":4209.75,"BQ":35,"C":5186.7,"H":0,"L":0,"LTP":5186.7,"LTQ":0,"O":0,"OI":0,"QL":35,"SP":4468.3,"SQ":350,"TTQ":0,"VAL":0,"PO":false,"PC":0,"PCP":0,"OIC":-140,"T":"RT"},{"E":"NFO","I":"OPTSTK_LTIM_28OCT2025_PE_5300","LTT":1759831039,"STT":1759831040,"ATP":164.77,"BP":172.05,"BQ":300,"C":171.9,"H":180.85,"L":124.65,"LTP":173.95,"LTQ":0,"O":125.4,"OI":34800,"QL":150,"SP":173.7,"SQ":150,"TTQ":97350,"VAL":16040359.5,"PO":false,"PC":2.05,"PCP":1.19,"OIC":12900,"T":"RT"},{"E":"NFO","I":"OPTSTK_TORNTPHARM_28OCT2025_PE_3250","LTT":1759831039,"STT":1759831040,"ATP":4.13,"BP":3,"BQ":250,"C":5.45,"H":5.25,"L":3,"LTP":3,"LTQ":0,"O":5.1,"OI":5750,"QL":250,"SP":6.35,"SQ":250,"TTQ":2000,"VAL":8260,"PO":false,"PC":-2.45,"PCP":-44.95,"OIC":250,"T":"RT"},{"E":"NFO","I":"OPTIDX_MIDCPNIFTY_28OCT2025_PE_12675","LTT":1759831040,"STT":1759831040,"ATP":64.3,"BP":57.45,"BQ":280,"C":78.6,"H":76.3,"L":54.95,"LTP":58,"LTQ":0,"O":75.45,"OI":29540,"QL":140,"SP":59.35,"SQ":140,"TTQ":24920,"VAL":1602356,"PO":false,"PC":-20.6,"PCP":-26.21,"OIC":-560,"T":"RT"},{"E":"NFO","I":"OPTSTK_GODREJPROP_28OCT2025_PE_2200","LTT":1759831039,"STT":1759831040,"ATP":148.25,"BP":138.25,"BQ":275,"C":152.45,"H":168.4,"L":139,"LTP":139.75,"LTQ":0,"O":150,"OI":72325,"QL":275,"SP":139.4,"SQ":275,"TTQ":17325,"VAL":2568431.25,"PO":false,"PC":-12.7,"PCP":-8.33,"OIC":0,"T":"RT"},{"E":"NFO","I":"OPTIDX_BANKNIFTY_25NOV2025_PE_57100","LTT":1759831040,"STT":1759831040,"ATP":1020.99,"BP":1006.35,"BQ":105,"C":1125.45,"H":1146.55,"L":946,"LTP":1008.75,"LTQ":0,"O":1146.55,"OI":770,"QL":35,"SP":1011.45,"SQ":70,"TTQ":805,"VAL":821896.95,"PO":false,"PC":-116.7,"PCP":-10.37,"OIC":490,"T":"RT"},{"E":"NFO","I":"OPTIDX_BANKNIFTY_25NOV2025_CE_55500","LTT":1759831040,"STT":1759831040,"ATP":1707.51,"BP":1639,"BQ":140,"C":1536.15,"H":1802,"L":1500,"LTP":1637.45,"LTQ":0,"O":1536.15,"OI":27405,"QL":35,"SP":1646.4,"SQ":35,"TTQ":7350,"VAL":12550198.5,"PO":false,"PC":101.3,"PCP":6.59,"OIC":-105,"T":"RT"},{"E":"NFO","I":"OPTIDX_BANKNIFTY_28OCT2025_CE_54400","LTT":1759831039,"STT":1759831040,"ATP":2182.21,"BP":2128.5,"BQ":70,"C":1996,"H":2338.2,"L":1925.45,"LTP":2118.6,"LTQ":0,"O":1980.85,"OI":17325,"QL":35,"SP":2149.25,"SQ":35,"TTQ":2905,"VAL":6339320.05,"PO":false,"PC":122.6,"PCP":6.14,"OIC":-665,"T":"RT"},{"E":"NFO","I":"OPTSTK_TORNTPHARM_28OCT2025_CE_3350","LTT":1759831039,"STT":1759831040,"ATP":189.8,"BP":199.5,"BQ":250,"C":253.75,"H":191.3,"L":188.3,"LTP":191.3,"LTQ":0,"O":188.3,"OI":750,"QL":250,"SP":226.35,"SQ":250,"TTQ":500,"VAL":94900,"PO":false,"PC":-62.45,"PCP":-24.61,"OIC":250,"T":"RT"}]}

++ multiple line of traded symbols in batch wise format like above .

Expansion on  returns:- 

E - Exchange , I - InstrumentIdentifier ,LTT - LastTradeTime , STT - ServerTime , ATP - AverageTradedPrice ,C- Close ,O- Open ,H- High ,L -Low, LTP- LastTradePrice,LTQ- LastTradeQty ,TTQ- TotalQtyTraded, BP- BuyPrice,BQ-BuyQty,SP- SellPrice ,SQ- SellQty,OI-OpenInterest,QL-QuotationLot,VAL-Value,PO-PreOpen,PC-PriceChange,PCP-PriceChangePercentage,OIC-OpenInterestChange, T- MessageType
```

---

## StreamAllSnapshots (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/streaming_api/streamallsnapshots/ — last updated 2026-04-20T13:24:05 — captured 2026-07-15

StreamAllSnapshots – No additional function is required to be enabled for this functionality. It will send Realtime Snapshot data of all symbols **with respective to the Exchange, Periodicity and Period**, which are enabled for the API key.

### Authenticate : Authenticates the user

**What is returned ?**

Nothing. This function authenticates the user and sends response accordingly

**Sample Request (JavaScript)**

```javascript
 function Authenticate()
  {
     writeToScreen("Authenticate");
    var message = 
    {
       MessageType: "Authenticate",
       Password: accessKey
     };
    doSend(message);
  }
```

**JSON Response**

```json
{"Complete":true,"Message":"Welcome!","MessageType":"AuthenticateResult"}"
```

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier** (Symbol)**,** **Periodicity , Period,** **LastTradeTime, TradedQuantity, OpenInterest**, **Open**, **High, Low**, **Close** (previous Day's Close). |
| LastTradeTime, ServerTradeTime : These values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data in JSON format for exchanges which are enabled**

```text
RESPONSE: {"Exchange":"MCX","Periodicity":"MINUTE","Period":1,"Result":[{"InstrumentIdentifier":"FUTCOM_CRUDEOIL_19NOV2024__0",
"LastTradeTime":1729683720,
"TradedQty":61,
"OpenInterest":14564,
"Open":5934.0,
"High":5936.0,
"Low":5934.0,
"Close":5935.0},
.
.
.
{"InstrumentIdentifier":"OPTFUT_NATURALGAS_24OCT2024_CE_190",
"LastTradeTime":1729683720,
"TradedQty":141,
"OpenInterest":10085,
"Open":4.95,
"High":5.05,
"Low":4.9,
"Close":5.05}],
"MessageType":"RealtimeSnapshotCollection"}

++ multiple line of traded symbols 
```
