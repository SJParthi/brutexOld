# GlobalDataFeeds APIs — REST Code Samples (Python)

RESTful API sample walkthrough for Python: connection flow, sample download, GetLastQuote example code and sample response, plus trouble-shooting.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/rest-python/

---

## REST – Python

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/rest-python/ — last updated 2024-07-23T11:32:12 — captured 2026-07-15

### Introduction

To connect using RESTful API, you will need following information :

– "endpoint" to connect to,
– "port number" to connect to,
– "API Key"

You will get all of the above once you purchase the API from our team

The flow of operations should be as follows :

1. Send HTTP GET requests using endpoint, port, API Key & function name (with mandatory and optional parameters).
2. Typical endpoint is **http://endpoint:port/**
3. You will need to send endpoint, port, apikey **WITH EVERY REQUEST** to get desired response

**Ready, lets gets started then**

### Download the sample

– Download Python Sample for RESTful API from [Download Code Samples](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/) page.
– The sample is zipped. Once downloaded, extract it to some folder using WinZip or WinRar
– Open the *RESTful_Sample_Phython.py* in the editor. You can use Visual Studio Code, IDLE or any compatible Editor

Note the code below in file at the top :

```python
"""
GFDL TODO - please enter below the endpoint received from GFDL team. 
If you dont have one, please contact us on sales@globaldatafeeds.in 
"""  
endpoint = "http://endpoint:port/"

"""
//GFDL TODO - please enter below the API Key received from GFDL team. 
If you dont have one, please contact us on sales@globaldatafeeds.in 
"""
accesskey = "Enter Your API Key Here"
```

– Enter the Endpoint, Port and API Access Key received from our team in above code.
– Save this file and run it. You can run it directly in the editor (if supported) or you can run it in your browser.
– Make sure you run the sample during market hours.

### What happens when sample is run for the first time ?

– When sample is run for the first time, it sends request to server for **GetLastQuote** function (NIFTY-I Symbol from NFO Exchange). GetLastQuote sends latest trade data of requested symbol from server to client i.e. to you. If your account is not enabled for this Exchange, kindly make suitable changes in *async def GetLastQuote()* function and replace *ExchangeName* & *InstIdentifier* variables. The sample code has examples of symbols from other Exchanges for quick reference. For complete reference, please see [Symbol Naming Conventions](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-conventions/).
– Sample will update prices 10 times before termination. To see more updates, please assign higher value for *maxcount* variable.
– If you run sample during market hours, you will notice updation of realtime data of available symbols at your end.
– The code below will be run when sample is run :

```python
"""
///////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////////
	/* 	GFDL : 	1. 	Below 3 functions return the data of SINGLE SYMBOL - whenever requested.
				2. 	So you will need to send these requests EVERY TIME when you need latest data.
				3. 	GetLastQuote : returns single record of realtime data of single symbol. Contains many fields in response
				4. 	GetLastQuoteShort : returns single record of realtime data of single symbol. Contains limited fields in response
				5. 	GetLastQuoteShortWithClose : returns single record of realtime data of single symbol. Contains limited fields in response
				6.	If you want to get data of multiple symbols, you will need to send 1 request each - for each symbol
				
                //This example shows how to request data using Continuous Format
                //Similarly, you can send NIFTY-II (Near month), NIFTY-III (Far month). 
                //Below Symbol is Continuous Format of NIFTY Futures. It will never expire. So no change in code will be necessary.
                //You can use same naming convention for Futures of Instruments from NFO, CDS, MCX,BFO Exchanges
                //CDS Examples : USDINR-I, USDINR-II, USDINR-III
                //MCX Examples : NATURALGAS-I, NATURALGAS-II, NATURALGAS-III
                //BFO Examples : BANKEX-I,BANKEX-II,BANKEX-III
                
                //Similarly, you can send NIFTY20AUGFUT (near month), NIFTY20SEPFUT (far month). 
                //You can use same naming convention for Futures of Instruments from NFO, CDS, MCX,BFO Exchanges
                //NFO Options Examples : NIFTY02JUL2010000CE, RELIANCE30JUL201700CE
                //CDS Futures Examples : USDINR20JULFUT, USDINR20AUGFUT, USDINR20SEPFUT
                //CDS Options Examples : USDINR29JUL2075.5CE, EURINR29JUL2080CE
                //MCX Options Examples : CRUDEOIL20JULFUT, CRUDEOIL20AUGFUT, CRUDEOIL20SEPFUT
                //MCX Options Examples : CRUDEOIL20JUL2050PE, GOLD20JUL43700PE	
                //BFO  Futures Examples : BANKEX24JUN24FUT, BANKEX2JUL24FUT
                //BFO  Options Examples : BANKEX14JUN2457300PE, BANKEX14JUN2457300CE	
                //Important : Replace it with appropriate expiry date if this contract is expired

                //Similarly, you can send FUTIDX_NIFTY_27AUG2020_XX_0 (near month), FUTIDX_NIFTY_24SEP2020_XX_0 (far month). 
                //You can use same naming convention for Futures of Instruments from NFO, CDS, MCX,BFO Exchanges
                //NFO Options Examples : OPTIDX_NIFTY_02JUL2020_CE_10000, OPTSTK_RELIANCE_30JUL2020_CE_1700
                //CDS Futures Examples : FUTCUR_USDINR_26JUN2020_XX_0, FUTCUR_USDINR_29JUL2020_XX_0, FUTCUR_USDINR_27AUG2020_XX_0
                //CDS Options Examples : OPTCUR_USDINR_29JUL2020_CE_75.5, OPTCUR_EURINR_29JUL2020_CE_80
                //MCX Futures Examples : FUTCOM_CRUDEOIL_20JUL2020__0, FUTCOM_CRUDEOIL_19AUG2020__0, FUTCOM_CRUDEOIL_21SEP2020__0
                //MCX Options Examples : OPTFUT_CRUDEOIL_16JUL2020_PE_2050, OPTFUT_GOLD_27JUL2020_PE_43700
                //Important : Replace it with appropriate expiry date if this contract is expired

                Requesting realtime data of NSE Indices
                -------------------------------------------
                //Use InstrumentIdenfier value "NIFTY 50", "NIFTY BANK", "NIFTY 100", etc.
                //Use NSE_IDX as Exchange
                //Please note that Indices Symbols have white space. For example, between NIFTY & 50, NIFTY & BANK above

                Requesting realtime data of NSE Stocks 
                ------------------------------------------
                //For EQ Series, use InstrumentIdentifier value BAJAJ-AUTO, RELIANCE, AXISBANK, LT, etc..
                //To subscribe to realtime data of any other series, append the series name to symbol name 
                //for example, to request data of RELIANCE CAPITAL from BE Series, use RELCAPITAL.BE
                //EQ Series Symbols are mentioned without any suffix

                // Please see symbol naming conventions here : 
                // https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-convention/
	*/
///////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////////
"""
async def GetLastQuote():
    print("----------------------------------------------------")
    print("Work in progress... sending request for GetLastQuote")
    print("----------------------------------------------------")
    ExchangeName = "NFO"
    InstIdentifier = "NIFTY-I"
    isShortIdentifier = "false"         #GFDL : When using contractwise symbol like NIFTY20JULFUT, 
                                        #this argument must be sent with value "true" 
    xml="false"                                        
    response = ""
    count = 0
    maxcount = 10                       #Change this number to see more updates. By default, sample will print 10 updates and stop
    while(count<maxcount): strmessage="endpoint+"getlastquote/?accessKey="+accesskey+"&exchange="+ExchangeName+"&instrumentIdentifier="+InstIdentifier+"&xml="+xml" response="requests.get(strMessage)" print("message="" sent="" :="" "+strmessage)="" print("waiting="" for="" response...")="" print("response="" :\n"="" +="" response.text)="" print("----------------------------------------------------")="" time.sleep(1.5)="" count="count+1"
```

*(Note: the tail of the above code block — the `while` loop body — appears garbled on the source page itself; it is preserved here exactly as published.)*

### Sample Response

```text
----------------------------------------------------
Work in progress... sending request for GetLastQuote
----------------------------------------------------
Message sent : http://endpoint:port/getlastquote/?accessKey=accesskey&exchange=NFO&instrumentIdentifier=NIFTY-I&xml=false
Waiting for response...
Response :
Authentication request received. Try request data in next moment.
----------------------------------------------------
Message sent : http://endpoint:port/getlastquote/?accessKey=accesskey&exchange=NFO&instrumentIdentifier=NIFTY-I&xml=false
Waiting for response...
Response :
{ "AVERAGETRADEDPRICE": 11126.36,
"BUYPRICE": 11114.2,
"BUYQTY": 300,
"CLOSE": 11170.25,
"EXCHANGE": "NFO",
"HIGH": 11225,
"INSTRUMENTIDENTIFIER": "NIFTY-I",
"LASTTRADEPRICE": 11114.8,
"LASTTRADEQTY": 1125,
"LASTTRADETIME": 1595844000000,
"LOW": 11070.1,
"OPEN": 11201.65,
"OPENINTEREST": 10055850,
"PREOPEN": false,
"QUOTATIONLOT": 75,
"SELLPRICE": 11116,
"SELLQTY": 975,
"SERVERTIME": 1595844000000,
"TOTALQTYTRADED": 12215100,
"VALUE": 135909600036,
"PRICECHANGE": -55.45,
"PRICECHANGEPERCENTAGE": -0.5,
"OPENINTERESTCHANGE": 372375}
----------------------------------------------------
```

– To know how each function works, you can uncomment required function at top (one function at a time), save the sample and run it.

### Trouble-shooting

#### The code outputs nothing

– Please make sure you have entered the Endpoint, Port & API Key in the sample code, saved the changes before running it.
– To see realtime data in action, you should run the sample during market hours
– If you are behind firewall, please speak to your network administrator and get the endpoint URL and port whitelisted in your firewall

#### I can see some output but I dont see all symbols requested / I get "Exchange Not Enabled." message

– GetLastQuote function requests data of a symbol (NIFTY-I from NFO Exchange in the unedited sample). You will need to make sure you have paid for the subscription of the Exchange you are using in your request.

– Some examples use Contractwise symbols which might have expired. Please check code and change the symbols. Please check Symbol Naming Conventions page for more information about various ways to name symbols (especially Futures and Options).

#### I have requested data but it takes lot of time to receive the response

– If you have requested entire GetHistory, GetInstruments (for NSE / NFO / BFO), GetExchangeSnapshot or GetLastQuoteOptionChain, these functions return thousands of rows. So it is possible that it takes more time than other requests to return the data

– Please make sure you are on fast internet connection.

#### I have other issues not listed here

– Please drop an email to [developer@globaldatafeeds.in](mailto:developer@globaldatafeeds.in?Subject=WebSockets%20API%20Query) with following :
1. Details of the request sent (exact syntax), expected response & received response
2. Screenshot of the request & response
3. API Key used

Once received, we will check and revert at the earliest (typically within few hours for normal queries).
