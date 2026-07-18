# GlobalDataFeeds Greeks API (REST)

REST Greeks API — historical, snapshot, last-quote (single/array) and option-chain Option Greeks (IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR) with JSON/XML/CSV output.

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/gethistorygreeks-returns-historical-greeks-data/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getsnapshotgreeks/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreeks-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquotearrayoptiongreeks-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreekschain-2/

---

## GetHistoryGreeks (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/gethistorygreeks-returns-historical-greeks-data/ — last updated 2025-10-23T11:04:29 — captured 2026-07-15

GetHistoryGreeks : Returns historical greeks data

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like* NFO | Name of supported exchange. |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *MINUTE, TICK* | String value of required periodicity. |
| **Period** | *Numerical value 1,  (default = 1*) | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[TICK] periodicity types |
| **From** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **To** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **Max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. If Max = N, N records will be received in the response. |
| **userTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY28NOV2426050CE, etc. |
| **Example** | http://endpoint:port/GetHistoryGreeks?accessKey=Your_API_Key&exchange=NFO&instrumentIdentifier=BANKNIFTY30SEP2554700CE&periodicity=TICK&period=1&max=2&IsShortidentifier=true&XML=TRUE | |

**What is returned ?**

| |
| --- |
| **Token, Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR.** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data in JSON format**

**JSON**

```json
{ "USERTAG": null, "GREEKS": [ {"TOKEN": "54651","TIMESTAMP": 1758880799000,"IV": 0.0949113667011261,"DELTA": 0.343663245439529,"THETA": -24.8926239013672, "VEGA": 20.9821319580078,"GAMMA": 0.000679671124089509,"IVVWAP": 0.0931760892271996,"VANNA": 0.0145926857367158,"CHARM": -0.0231211483478546, "SPEED": 4.9191407924809e-7,"ZOMMA": -0.000055450185755034,"COLOR": 0.000083728191384579,"VOLGA": 0.318546801805496,"VETA": -3.30685520172119, "THETAGAMMARATIO": -36624.51171875,"THETAVEGARATIO": -1.18637251853943,"DTR": -0.0138058261945844 }, 
{"TOKEN": "54651","TIMESTAMP": 1758880798000,"IV": 0.0963260605931282,"DELTA": 0.337341815233231,"THETA": -25.0792255401611, "VEGA": 20.8290996551514,"GAMMA": 0.000665112747810781,"IVVWAP": 0.0931760743260384,"VANNA": 0.0149072613567114,"CHARM": -0.0238988436758518, "SPEED": 4.95147446599731e-7,"ZOMMA": -0.000052732262702193,"COLOR": 0.000080142024671658,"VOLGA": 0.339517742395401,"VETA": -3.32480382919312, "THETAGAMMARATIO": -37706.73046875,"THETAVEGARATIO": -1.20404756069183,"DTR": -0.0134510463103652 }] }
```

**XML**

```xml
<OptionGreeksHistory xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema"> <SID>SID: .220 Q:1</SID> <Value Token="64694″ IV="0.15039214491844177″ Delta="0.05992437899112701″ Theta="-5″ Vega="1.5382640361785889″ Gamma="0.0006121770711615682″ IVVwap="0.11679470539093018″ Vanna="0.012506057508289814″ Charm="-0.05992437899112701″ Speed="4.884546342509566E-06″ Zomma="5.264517676550895E-05″ Color="-0.0006121770711615682″ Volga="0.2433651238679886″ Veta="-1.5382640361785889″ ThetaGammaRatio="-8167.57177734375″ ThetaVegaRatio="-3.250417232513428″ DTR="-0.011984875425696371″ Timestamp="09-29-2025 15:29:59″/>
 <Value Token="64694″ IV="0.14931471645832062″ Delta="0.06131138652563095″ Theta="-5.099999904632568″ Vega="1.5663611888885498″ Gamma="0.0006276043714024127″ IVVwap="0.11679363995790482″ Vanna="0.012714873068034647″ Charm="-0.06131138652563095″ Speed="5.0051730795530585E-06″ Zomma="5.283048812998459E-05″ Color="-0.0006276043714024127″ Volga="0.2455870658159256″ Veta="-1.5663611888885498″ ThetaGammaRatio="-8126.138671875″ ThetaVegaRatio="-3.255954027175904″ DTR="-0.012021840550005436″ Timestamp="09-29-2025 15:29:58″/>  </OptionGreeksHistory>
```

**CSV**

```csv
Token,Timestamp,IV,Delta,Theta,Vega,Gamma,IVVwap,Vanna,Charm,Speed,Zomma,Color,Volga,Veta,ThetaGammaRatio,ThetaVegaRatio,DTR 64694,1759139999000,0.15039214491844177,0.05992437899112701,-5,1.5382640361785889,0.0006121770711615682,0.11679470539093018,0.012506057508289814,-0.05992437899112701,4.884546342509566E-06,5.264517676550895E-05,-0.0006121770711615682,0.2433651238679886,-1.5382640361785889,-8167.57177734375,-3.250417232513428,-0.011984875425696371 64694,1759139998000,0.14931471645832062,0.06131138652563095,-5.099999904632568,1.5663611888885498,0.0006276043714024127,0.11679363995790482,0.012714873068034647,-0.06131138652563095,5.0051730795530585E-06,5.283048812998459E-05,-0.0006276043714024127,0.2455870658159256,-1.5663611888885498,-8126.138671875,-3.255954027175904,-0.012021840550005436 
```

---

## GetSnapshotGreeks (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getsnapshotgreeks/ — last updated 2025-10-23T10:54:29 — captured 2026-07-15

GetSnapshotGreeks : Returns latest Snapshot Greeks Data of multiple Symbols – max 25 in single call

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like* NFO | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Periodicity** | *MINUTE* | String value of required periodicity. |
| **Period** | *Numerical value 1, default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE] periodicity types |
| **InstrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **Example** | http://endpoint:port/GetSnapshotGreeks?accessKey=Your_API_Key&exchange=NFO&periodicity=MINUTE&period=1&instrumentIdentifiers=NIFTY28OCT2525000CE%2BNIFTY28OCT2525000PE&isShortIdentifiers=true&format=Json | |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier, Periodicity, Period, LastTradeTime, Token, IV, Delta, Theta, Vega,** **Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data in JSON format**

**JSON**

```json
[ { "INSTRUMENTIDENTIFIER": "NIFTY28OCT2525000PE", "TOKEN": "58910", "LASTTRADETIME": 1761038040000, "IV": 0.137895405292511, "DELTA": -0.0357031039893627, "THETA": -2.77157807350159, "VEGA": 2.81395649909973, "GAMMA": 0.000158973212819546, "IVVWAP": 0.139961808919907, "VANNA": -0.0105502251535654, "CHARM": 0.00986738782376051, "SPEED": -5.8550182302497e-7, "ZOMMA": 0.000023905337002361, "COLOR": -0.000027629310352494, "VOLGA": 0.657894551753998, "VETA": -0.821180164813995, "THETAGAMMARATIO": -17434.24609375, "THETAVEGARATIO": -0.984939873218536, "DTR": 0.012881868518889 }, 
{ "INSTRUMENTIDENTIFIER": "NIFTY28OCT2525000CE", "TOKEN": "58909", "LASTTRADETIME": 1761038040000, "IV": 0.139153808355331, "DELTA": 0.962180197238922, "THETA": -2.9309356212616, "VEGA": 2.94937348365784, "GAMMA": 0.000165150690008886, "IVVWAP": 0.147704541683197, "VANNA": -0.0107587240636349, "CHARM": 0.0102256750687957, "SPEED": -5.9419255649118e-7, "ZOMMA": 0.000023472875909646, "COLOR": -0.000027624264475889, "VOLGA": 0.661269724369049, "VETA": -0.844122707843781, "THETAGAMMARATIO": -17747.0390625, "THETAVEGARATIO": -0.993748605251312, "DTR": -0.328284293413162 } ]
```

**XML**

```xml
<SnapshotGreeksArray xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
<Value InstrumentIdentifier="NIFTY28OCT2525000CE" LastTradeTime="10-21-2025 14:44:00″ Token="58909″ IV="0.13915380835533142″ Delta="0.9621801972389221″ Theta="-2.9309356212615967″ Vega="2.949373483657837″ Gamma="0.00016515069000888616″ IVVwap="0.14770454168319702″ Vanna="-0.010758724063634872″ Charm="0.010225675068795681″ Speed="-5.941925564911799E-07″ Zomma="2.3472875909646973E-05″ Color="-2.7624264475889504E-05″ Volga="0.6612697243690491″ Veta="-0.8441227078437805″ ThetaGammaRatio="-17747.0390625″ ThetaVegaRatio="-0.9937486052513123″ DTR="-0.32828429341316223″/>
<Value InstrumentIdentifier="NIFTY28OCT2525000PE" LastTradeTime="10-21-2025 14:44:00″ Token="58910″ IV="0.137895405292511″ Delta="-0.03570310398936272″ Theta="-2.771578073501587″ Vega="2.8139564990997314″ Gamma="0.00015897321281954646″ IVVwap="0.13996180891990662″ Vanna="-0.010550225153565407″ Charm="0.00986738782376051″ Speed="-5.855018230249698E-07″ Zomma="2.3905337002361193E-05″ Color="-2.762931035249494E-05″ Volga="0.6578945517539978″ Veta="-0.8211801648139954″ ThetaGammaRatio="-17434.24609375″ ThetaVegaRatio="-0.9849398732185364″ DTR="0.01288186851888895″/>

</SnapshotGreeksArray>
```

**CSV**

```csv
InstrumentIdentifier,Token,LastTradeTime,IV,Delta,Theta,Vega,Gamma,IVVwap,Vanna,Charm,Speed,Zomma,Color,Volga,Veta,ThetaGammaRatio,ThetaVegaRatio,DTR NIFTY28OCT2525000CE,58909,1761038040000,0.13915380835533142,0.9621801972389221,-2.9309356212615967,2.949373483657837,0.00016515069000888616,0.14770454168319702,-0.010758724063634872,0.010225675068795681,-5.941925564911799E-07,2.3472875909646973E-05,-2.7624264475889504E-05,0.6612697243690491,-0.8441227078437805,-17747.0390625,-0.9937486052513123,-0.32828429341316223 NIFTY28OCT2525000PE,58910,1761038040000,0.137895405292511,-0.03570310398936272,-2.771578073501587,2.8139564990997314,0.00015897321281954646,0.13996180891990662,-0.010550225153565407,0.00986738782376051,-5.855018230249698E-07,2.3905337002361193E-05,-2.762931035249494E-05,0.6578945517539978,-0.8211801648139954,-17434.24609375,-0.9849398732185364,0.01288186851888895
```

**Important Note:**

GetSnapshotGreeks , the time when request is sent is very important. For example : If user sends request for 1minute snapshot data at 9:20:01, he will get record of trade data between 9:19:00 to 9:20:00. However, if request is sent at 9:19:58 i.e. before minute is complete, user will get trade data between 9:18:00 to 9:19:00. In both cases, timestamp of the returned data will be start timestamp of the period. So when data between 9:15:00 to 9:16:00 is returned, it will have timestamp of 9:15:00. Also if a user subscribes for snapshot data of any symbol for a period and if no trades happen during that period, no data is sent by the server.
Also, this function works only during market hours.

---

## GetLastQuoteOptionGreek (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreeks-2/ — last updated 2025-09-27T14:24:55 — captured 2026-07-15

### GetLastQuoteOptionGreek : Returns Last Traded Option Greek values of Single Symbol (detailed)

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **Token** | *Token number of instrument* | How to get list of available token numbers of instruments you can find [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **Example** | [http://endpoint:port/GetLastQuoteOptionGreeks/?accessKey=0a0b0c&exchange=NFO&token=52534&fromat=CSV&xml=true&detailedInfo=true](http://endpoint:port/GetLastQuoteOptionGreeks/?accessKey=0a0b0c&exchange=NFO&token=52534&detailedInfo=true&xml=true) | |

**What is returned ?**

| |
| --- |
| **Exchange, Token(TokenNumber of Symbol), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR** |
| TimeStamp : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data**

**JSON**

```json
{ "AVERAGETRADEDPRICE": 871.68, "BUYPRICE": 870.1, "BUYQTY": 1, "CLOSE": 870.9, "EXCHANGE": "MCX", "HIGH": 875, "INSTRUMENTIDENTIFIER": "XXX_MCXSCHANADLH_25Jun2015_XX_X", "LASTTRADEPRICE": 875, "LASTTRADEQTY": 2, "LASTTRADETIME": 1391863200, "LOW": 868.1, "OPEN": 871.2, "OPENINTEREST": 221, "PREOPEN": false, "QUOTATIONLOT": 100, "SELLPRICE": 874, "SELLQTY": 1, "SERVERTIME": 1391821719, "TOTALQTYTRADED": 17, "VALUE": 1481850}
```

**XML**

```xml
<?xml version="1.0″ encoding="utf-16″ ?>
<RealtimeOptionGreeks
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xmlns:xsd="http://www.w3.org/2001/XMLSchema">
Exchange="NFO" Token="52534″ IV="15.530631065368652″ Delta="0.003660476068034768″ Theta="-19976.673828125″ Vega="0.0003134779690299183″ Gamma="0.000245905015617609″ IVVwap="0.13660529255867004″ Vanna="2.514479160308838″ Charm="-1602375.125″ Speed="1.4805829778197218E-05″ Zomma="9.816951933316885E-05″ Color="62.5594367980957″ Volga="19.32025146484375″ Veta="14021988″ ThetaGammaRatio="-81237352″ ThetaVegaRatio="-63725924″ DTR="-1.8323751760362936E-07″ Timestamp="07-01-2021 15:29:59″
/>
```

**CSV**

```csv
Exchange,Token,Timestamp,IV,Delta,Theta,Vega,Gamma,IVVwap,Vanna,Charm,Speed,Zomma,Color,Volga,Veta,ThetaGammaRatio,ThetaVegaRatio,DTR NFO,35007,1713952478000,2.82,0,-0.1,0,0,0.5,0,0,0,0,0,0,0,-52609.11,-20.51,-0
```

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## GetLastQuoteArrayOptionGreeks (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquotearrayoptiongreeks-2/ — last updated 2024-07-10T15:38:21 — captured 2026-07-15

### GetLastQuoteArrayOptionGreeks: Returns Last Traded Option Greek values of multiple Symbols – max 25 in single call (detailed)

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **Tokens** | *Token number of instrument* | How to get list of available token numbers of instruments you can find [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) |
| **detailedInfo** | *[true]/[false], default = [false]* | How to get list of available token numbers of instruments you can find [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **Example** | [http://endpoint:port/GetLastQuoteArrayOptionGreeks/?accessKey=0a0b0c&exchange=NFO&tokens=39489+39487&xml=true](http://endpoint:port/GetLastQuoteArrayOptionGreeks/?accessKey=0a0b0c&exchange=NFO&tokens=39489+39487&xml=true) | |

**What is returned ?**

| |
| --- |
| **Exchange, Token(TokenNumber of Sysmbol), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR** |
| LastTradeTime, ServerTime : In JSON Response, these values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data**

**JSON**

```json
[{
    "EXCHANGE": "NFO",
    "TOKEN": "39489",
    "TIMESTAMP": 1625738399000,
    "IV": 1.46,
    "DELTA": 1,
    "THETA": -16.66,
    "VEGA": 0,
    "GAMMA": 0,
    "IVVWAP": 0.12,
    "VANNA": -2666.33,
    "CHARM": 57226592,
    "SPEED": 0,
    "ZOMMA": 0,
    "COLOR": 4.99,
    "VOLGA": 50545.55,
    "VETA": 1154302720,
    "THETAGAMMARATIO": -719606.5,
    "THETAVEGARATIO": -2146264.5,
    "DTR": -0.06
  },
  {
    "EXCHANGE": "NFO",
    "TOKEN": "39487",
    "TIMESTAMP": 1625738395000,
    "IV": 3.16,
    "DELTA": 0.98,
    "THETA": -4821.25,
    "VEGA": 0.01,
    "GAMMA": 0,
    "IVVWAP": 0.16,
    "VANNA": -1.78,
    "CHARM": 12360.09,
    "SPEED": 0,
    "ZOMMA": 0,
    "COLOR": 9.14,
    "VOLGA": 43.89,
    "VETA": 382011.25,
    "THETAGAMMARATIO": -3384442,
    "THETAVEGARATIO": -693573.19,
    "DTR": 0
 }]
```

**XML**

```xml
<?xml version="1.0″ encoding="utf-16″ ?>
<RealtimeArrayOptionGreeks
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="NFO" Token="39489″ IV="1.4572917222976685″ Delta="0.999961256980896″ Theta="-16.663326263427734″ Vega="7.763873327348847E-06″ Gamma="2.315616438863799E-05″ IVVwap="0.12388955056667328″ Vanna="-2666.33447265625″ Charm="57226592″ Speed="-1.3093988854961935E-05″ Zomma="0.00023227633209899068″ Color="4.985264301300049″ Volga="50545.5546875″ Veta="1154302720″ ThetaGammaRatio="-719606.5″ ThetaVegaRatio="-2146264.5″ DTR="-0.060009703040122986″ Timestamp="07-08-2021 15:29:59″ />
<Value Exchange="NFO" Token="39487″ IV="3.160403251647949″ Delta="0.9762697219848632″ Theta="-4821.2509765625″ Vega="0.006951322313398123″ Gamma="0.00142453343141824″ IVVwap="0.15728043019771576″ Vanna="-1.782088279724121″ Charm="12360.0869140625″ Speed="-7.199313404271379E-05″ Zomma="0.0013179931556805968″ Color="9.141247749328612″ Volga="43.89303970336914″ Veta="382011.25″ ThetaGammaRatio="-3384442″ ThetaVegaRatio="-693573.1875″ DTR="-0.00020249304361641407″ Timestamp="07-08-2021 15:29:55″ />
</RealtimeArrayOptionGreeks>
```

**CSV**

```csv
BuyPrice,BuyQty,LastTradePrice,LastTradeTime,OpenInterest,QuotationLot,SellPrice,SellQty,TradedQty Exchange,Token,Timestamp,IV,Delta,Theta,Vega,Gamma,IVVwap,Vanna,Charm,Speed,Zomma,Color,Volga,Veta,ThetaGammaRatio,ThetaVegaRatio,DTR NFO,56932,1717063196000,11.74,-0.98,-361.15,0.01,0,0.21,0,0,0,0,0,0,0,-1499912.13,-63624.34,0 NFO,56933,1717063199000,31.41,0,-0.05,0,0,0.25,0,0,0,0,0,0,0,-1574.82,-268.99,-0.03
```

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## GetLastQuoteOptionGreeksChain (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreekschain-2/ — last updated 2025-09-27T14:23:14 — captured 2026-07-15

### GetLastQuoteOptionGreeksChain : Returns Last Traded Option Greek values of entire OptionChain of requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **product** | *String value like NIFTY, BANKNIFTY, etc..* | Mandatory Parameter. How to get list of available Products, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value of expiry in DDMMYYYY format, like 23JAN2020* | Optional parameter. If absent, result is sent for all active Expiries. How to get list of available Expiries, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) |
| **optionType** | *String value like CE, PE* | Optional parameter. If absent, result is sent for all OptionTypes. How to get list of available OptionTypes, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) |
| **strikePrice** | *like 10000, 75.5, etc.* | Optional parameter. If absent, result is sent for all active Strike Prices. How to get list of available Strike Prices, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/) |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **Example** | [http://endpoint:port/GetLastQuoteOptionGreeksChain/?accessKey=0a0b0c&exchange=NFO&product=NIFTY](http://endpoint:port/GetLastQuoteOptionChain/?accessKey=0a0b0c&exchange=NFO&product=NIFTY) | |

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close, **IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR**

**Example of returned data**

| | |
| --- | --- |
| **JSON** | **XML** |
| [Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionGreeksChain_JSON.zip) | [Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionGreeksChain_XML.zip) |
| **CSV** | [Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionGreeksChain_CSV.zip) |

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed
