# GlobalDataFeeds APIs — WebSocket Code Samples (Part B: Java, NodeJS, Postman)

WebSockets API sample walkthroughs for Java, NodeJS and Postman. (Part A — downloads page, JavaScript, Python — is in 23a-code-samples-websocket-js-python.md.)

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-java/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-nodejs/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-postman/

---

## WebSockets – Java

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-java/ — last updated 2024-07-10T10:59:30 — captured 2026-07-15

### Introduction

To connect using WebSockets API, you will need following information :

– "endpoint" to connect to,
– "port number" to connect to,
– "API Key"

You will get all of the above once you purchase the API from our team

The flow of operations should be as follows :

1. Make a connection to the **ws://endpoint:port**
2. Send Authentication Request using **API Key**
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

**Ready, lets gets started then**

### Download the sample

– Download Java Sample from [Download Code Samples](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/) page.
– The sample is zipped. Once downloaded, extract it to some folder using WinZip or WinRar
– Open the Project in the editor. You can use Eclipse IDE for Java Developers (free to download) or any compatible Editor

Note the code below in file at the top :

```java
      	//GFDL TODO - please enter below the endpoint received from GFDL team. If you dont have one, please contact us on sales@globaldatafeeds.in 
        String EndPoint = "ws://endpoint:port";
        
        //GFDL TODO - please enter below the API Key received from GFDL team. If you dont have one, please contact us on sales@globaldatafeeds.in 
        String AccessKey = "Enter your API Key here";
```

– Enter the Endpoint, Port and API Access Key received from our team in above code.
– Save this file and run it. You can run it directly in the editor (if supported) or you can run it in your browser.
– Make sure you run the sample during market hours.

### What happens when sample is run for the first time ?

– When sample is run for the first time, it sends request to server for **SubscribeRealtime** function (1 symbol each from each Exchange). SubscribeRealtime pushes realtime data of requested symbol from server to client i.e. to you. The sample requests realtime data of various symbols to the server.
– If you run sample during market hours, you will notice updation of realtime data of available symbols at your end.
– Which function to run – is controlled by *main()* and *Switch-Case* statement – as shown below :

```java
public class WebSocketSample_JAVA {
    public static void main(String[] args) throws Exception {
        
    	//GFDL TODO - please enter below the endpoint received from GFDL team. If you dont have one, please contact us on sales@globaldatafeeds.in 
        String EndPoint = "ws://endpoint:port";
        
        //GFDL TODO - please enter below the API Key received from GFDL team. If you dont have one, please contact us on sales@globaldatafeeds.in 
        String AccessKey = "Enter your API Key here";

        
        /*GFDL TODO : All the functions supported by API are listed below. You can uncomment any function to see the flow of request and response*/
        
        String functionName = "SubscribeRealtime";
        //String functionName = "SubscribeSnapshot";

        //String functionName = "GetLastQuote";
        //String functionName = "GetLastQuoteShort";
        //String functionName = "GetLastQuoteShortWithClose";
        //String functionName = "GetLastQuoteArray";
        //String functionName = "GetLastQuoteArrayShort";
        //String functionName = "GetLastQuoteArrayShortWithClose";
        
        //String functionName = "GetSnapshot";
        //String functionName = "GetHistory";

        //String functionName = "GetExchanges";

        //String functionName = "GetInstrumentsOnSearch";
        //String functionName = "GetInstruments";

        //String functionName = "GetInstrumentTypes";
        //String functionName = "GetProducts";
        //String functionName = "GetExpiryDates";
        //String functionName = "GetOptionTypes";
        //String functionName = "GetStrikePrices";

        //String functionName = "GetServerInfo";
        //String functionName = "GetLimitation";

        //String functionName = "GetMarketMessages";
        //String functionName = "GetExchangeMessages";
        
        //String functionName = "GetLastQuoteOptionChain";
        //String functionName = "GetExchangeSnapshot";
    
        final WSClientEndpoint clientEndPoint;
        clientEndPoint = new WSClientEndpoint(new URI(EndPoint));
        
        //Here user is Authenticated using "getAuthenticate" function
        clientEndPoint.sendMessage(getAuthenticate("Authenticate",AccessKey));
        Thread.sleep(500);
        
        
        if(clientEndPoint.APIConnection)
        {       	
            switch (functionName) {
			///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
			///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
			/* 	GFDL : 	1. 	SubscribeRealtime : pushes realtime data every second for the subscribed instrument from server
			2. 	So you will need to send this request only once and subscribe for the data.	
			3.	Please note that if there is internet disconnection for any reason, you will need to 
			subscribe to all the instruments again - to receive the data.
			4.	To see this function in action, you should run it during market hours
			5.	If you want to subscribe to data of multiple symbols, you will need to send 1 request each - for each symbol
			6. 	Please see symbol naming conventions here : https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-convention/
			*/
			///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
			///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
	            case "SubscribeRealtime":
```

### Sample Response

```text
{"MessageType":"Authenticate","Password":"password_is_masked"}
Authentication Complete!
Authentication Complete!
Authentication Complete!
{"MessageType":"SubscribeRealtime","Exchange":"NFO","InstrumentIdentifier":"NIFTY-I"}
{"MessageType":"SubscribeRealtime","Exchange":"NFO","InstrumentIdentifier":"FUTIDX_NIFTY_30JUL2020_XX_0"}
{"MessageType":"SubscribeRealtime","Exchange":"NFO","InstrumentIdentifier":"NIFTY20JULFUT"}
{"MessageType":"SubscribeRealtime","Exchange":"NSE_IDX","InstrumentIdentifier":"NIFTY 50"}
{"MessageType":"SubscribeRealtime","Exchange":"NSE","InstrumentIdentifier":"BAJAJ-AUTO"}
{"MessageType":"SubscribeRealtime","Exchange":"CDS","InstrumentIdentifier":"USDINR-I"}
{"MessageType":"SubscribeRealtime","Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038362,"ServerTime":1594038362,"AverageTradedPrice":136.49,"BuyPrice":136.4,"BuyQty":147,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.4,"LastTradeQty":5,"Open":132.4,"OpenInterest":16857,"QuotationLot":1250.0,"SellPrice":136.5,"SellQty":144,"TotalQtyTraded":121396,"Value":20710982750.0,"PreOpen":false,"PriceChange":4.3,"PriceChangePercentage":3.26,"OpenInterestChange":4126,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038363,"ServerTime":1594038363,"AverageTradedPrice":136.49,"BuyPrice":136.4,"BuyQty":145,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.4,"LastTradeQty":31,"Open":132.4,"OpenInterest":16837,"QuotationLot":1250.0,"SellPrice":136.5,"SellQty":131,"TotalQtyTraded":121427,"Value":20716270000.0,"PreOpen":false,"PriceChange":4.3,"PriceChangePercentage":3.26,"OpenInterestChange":4106,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038364,"ServerTime":1594038364,"AverageTradedPrice":136.49,"BuyPrice":136.4,"BuyQty":142,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.4,"LastTradeQty":1,"Open":132.4,"OpenInterest":16836,"QuotationLot":1250.0,"SellPrice":136.5,"SellQty":118,"TotalQtyTraded":121428,"Value":20716440500.0,"PreOpen":false,"PriceChange":4.3,"PriceChangePercentage":3.26,"OpenInterestChange":4105,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038365,"ServerTime":1594038365,"AverageTradedPrice":136.49,"BuyPrice":136.4,"BuyQty":150,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.4,"LastTradeQty":15,"Open":132.4,"OpenInterest":16845,"QuotationLot":1250.0,"SellPrice":136.5,"SellQty":102,"TotalQtyTraded":121443,"Value":20718999375.0,"PreOpen":false,"PriceChange":4.3,"PriceChangePercentage":3.26,"OpenInterestChange":4114,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038365,"ServerTime":1594038366,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":148,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.5,"LastTradeQty":37,"Open":132.4,"OpenInterest":16842,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":153,"TotalQtyTraded":121480,"Value":20725312875.0,"PreOpen":false,"PriceChange":4.4,"PriceChangePercentage":3.33,"OpenInterestChange":4111,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038366,"ServerTime":1594038366,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":146,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.5,"LastTradeQty":7,"Open":132.4,"OpenInterest":16840,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":152,"TotalQtyTraded":121487,"Value":20726507250.0,"PreOpen":false,"PriceChange":4.4,"PriceChangePercentage":3.33,"OpenInterestChange":4109,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038367,"ServerTime":1594038367,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":141,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.5,"LastTradeQty":11,"Open":132.4,"OpenInterest":16834,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":152,"TotalQtyTraded":121498,"Value":20728384125.0,"PreOpen":false,"PriceChange":4.4,"PriceChangePercentage":3.33,"OpenInterestChange":4103,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038368,"ServerTime":1594038368,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":127,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.5,"LastTradeQty":12,"Open":132.4,"OpenInterest":16832,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":161,"TotalQtyTraded":121510,"Value":20730431750.0,"PreOpen":false,"PriceChange":4.4,"PriceChangePercentage":3.33,"OpenInterestChange":4101,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038368,"ServerTime":1594038369,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":128,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.5,"LastTradeQty":14,"Open":132.4,"OpenInterest":16824,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":161,"TotalQtyTraded":121524,"Value":20732820500.0,"PreOpen":false,"PriceChange":4.4,"PriceChangePercentage":3.33,"OpenInterestChange":4093,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038369,"ServerTime":1594038370,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":111,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.6,"LastTradeQty":30,"Open":132.4,"OpenInterest":16842,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":165,"TotalQtyTraded":121554,"Value":20737940500.0,"PreOpen":false,"PriceChange":4.5,"PriceChangePercentage":3.41,"OpenInterestChange":4111,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038370,"ServerTime":1594038370,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":109,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.6,"LastTradeQty":5,"Open":132.4,"OpenInterest":16840,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":167,"TotalQtyTraded":121559,"Value":20738794000.0,"PreOpen":false,"PriceChange":4.5,"PriceChangePercentage":3.41,"OpenInterestChange":4109,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038371,"ServerTime":1594038371,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":109,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.6,"LastTradeQty":2,"Open":132.4,"OpenInterest":16841,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":165,"TotalQtyTraded":121561,"Value":20739135500.0,"PreOpen":false,"PriceChange":4.5,"PriceChangePercentage":3.41,"OpenInterestChange":4110,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038372,"ServerTime":1594038372,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":109,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.6,"LastTradeQty":15,"Open":132.4,"OpenInterest":16839,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":158,"TotalQtyTraded":121576,"Value":20741696750.0,"PreOpen":false,"PriceChange":4.5,"PriceChangePercentage":3.41,"OpenInterestChange":4108,"MessageType":"RealtimeResult"}
{"Exchange":"MCX","InstrumentIdentifier":"NATURALGAS-I","LastTradeTime":1594038373,"ServerTime":1594038373,"AverageTradedPrice":136.49,"BuyPrice":136.5,"BuyQty":139,"Close":132.1,"High":139.6,"Low":132.0,"LastTradePrice":136.6,"LastTradeQty":3,"Open":132.4,"OpenInterest":16839,"QuotationLot":1250.0,"SellPrice":136.6,"SellQty":113,"TotalQtyTraded":121579,"Value":20742208750.0,"PreOpen":false,"PriceChange":4.5,"PriceChangePercentage":3.41,"OpenInterestChange":4108,"MessageType":"RealtimeResult"}
{"MessageType":"Echo"}
```

– To know how each function works, you can uncomment required function in *main()* (one function at a time), save the sample and run it.

### Trouble-shooting

#### The code outputs nothing

– Please make sure you have entered the Endpoint, Port & API Key in the sample code, saved the changes before running it.
– To see realtime data in action, you should run the sample during market hours
– If you are behind firewall, please speak to your network administrator and get the endpoint URL and port whitelisted in your firewall

#### I can see some output but I dont see all symbols requested / I get "Exchange Not Enabled." message

– SubscribeRealtime function runs at least 1 symbol from each Exchange Segment. You will need to make sure you have paid for the subscription of the Exchange you are using in your request.

– Some examples use Contractwise symbols which might have expired. Please check code and change the symbols. Please check Symbol Naming Conventions page for more information about various ways to name symbols (especially Futures and Options).

#### I have requested data but it takes lot of time to receive the response

– If you have requested entire GetHistory, GetInstruments (for NSE / NFO), GetExchangeSnapshot or GetLastQuoteOptionChain, these functions return thousands of rows. So it is possible that it takes more time than other requests to return the data

– Please make sure you are on fast internet connection.

#### I have other issues not listed here

– Please drop an email to [developer@globaldatafeeds.in](mailto:developer@globaldatafeeds.in?Subject=WebSockets%20API%20Query) with following :
1. Details of the request sent (exact syntax), expected response & received response
2. Screenshot of the request & response
3. API Key used

Once received, we will check and revert at the earliest (typically within few hours for normal queries).

---

## WebSockets – NodeJS

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-nodejs/ — last updated 2024-07-10T11:03:07 — captured 2026-07-15

### Introduction

To connect using WebSockets API, you will need following information :

– "endpoint" to connect to,
– "port number" to connect to,
– "API Key"

You will get all of the above once you purchase the API from our team

The flow of operations should be as follows :

1. Make a connection to the **ws://endpoint:port**
2. Send Authentication Request using **API Key**
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

**Ready, lets gets started then**

### Download the sample

– Download NodeJS Sample from [Download Code Samples](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/) page.
– The sample is zipped. Once downloaded, extract it to some folder using WinZip or WinRar
– Open the *WebsocketSample_Phython.py* in the editor. You can use Visual Studio Code, Notepad++ or any compatible Editor

Note the code below in file at the top :

```javascript
"""
/*
GFDL TODO - please enter below the endpoint received from GFDL team. 
If you dont have one, please contact us on sales@globaldatafeeds.in 
*/
var endpoint = "ws://endpoint:port/";

/*
//GFDL TODO - please enter below the API Key received from GFDL team. 
If you dont have one, please contact us on sales@globaldatafeeds.in 
*/
var accesskey = "Enter Your API Key Here";
```

– Enter the Endpoint, Port and API Access Key received from our team in above code.
– Save this file and run it.
– Make sure you run the sample during market hours.

### What happens when sample is run for the first time ?

– When sample is run for the first time, it sends request to server for **SubscribeRealtime** function (NIFTY-I Symbol from NFO Exchange). SubscribeRealtime pushes realtime data of requested symbol from server to client i.e. to you. If your account is not enabled for this Exchange, kindly make suitable changes in *async def SubscribeRealtime()* function and replace *ExchangeName* & *InstIdentifier* variables. The sample code has examples of symbols from other Exchanges for quick reference. For complete reference, please see Symbol Naming Conventions.
– If you run sample during market hours, you will notice updation of realtime data of available symbol at your end.
– Which function to call – is controlled in *function functionCall()* function. Here, you can uncomment the required function to see it in action (one function at a time).
– The code below will be run when sample is run :

```javascript
///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
	/* 	GFDL : 	1. 	SubscribeRealtime : pushes realtime data every second for the subscribed instrument from server
				2. 	So you will need to send this request only once and subscribe for the data.	
				3.	Please note that if there is internet disconnection for any reason, you will need to 
					subscribe to all the instruments again - to receive the data.
				4.	To see this function in action, you should run it during market hours
				5.	If you want to subscribe to data of multiple symbols, you will need to send 1 request each - for each symbol
				6. 	Please see symbol naming conventions here : https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-convention/
	*/
///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
/*
Subscribing to realtime data of Futures & Options :
---------------------------------------------------
//Below request will subscribe to realtime data of Current Month Futures of NIFTY	
//This example shows how to request data using Continuous Format
//Similarly, you can send NIFTY-II (Near month), NIFTY-III (Far month). 
//Below Symbol is Continuous Format of NIFTY Futures. It will never expire. So no change in code will be necessary.
//You can use same naming convention for Futures of Instruments from NFO, CDS, MCX Exchanges
//CDS Examples : USDINR-I, USDINR-II, USDINR-III
//MCX Examples : NATURALGAS-I, NATURALGAS-II, NATURALGAS-III

//Similarly, you can send NIFTY20AUGFUT (near month), NIFTY20SEPFUT (far month). 
//You can use same naming convention for Futures of Instruments from NFO, CDS, MCX Exchanges
//NFO Options Examples : NIFTY02JUL2010000CE, RELIANCE30JUL201700CE
//CDS Futures Examples : USDINR20JULFUT, USDINR20AUGFUT, USDINR20SEPFUT
//CDS Options Examples : USDINR29JUL2075.5CE, EURINR29JUL2080CE
//MCX Options Examples : CRUDEOIL20JULFUT, CRUDEOIL20AUGFUT, CRUDEOIL20SEPFUT
//MCX Options Examples : CRUDEOIL20JUL2050PE, GOLD20JUL43700PE	
//Important : Replace it with appropriate expiry date if this contract is expired

//Similarly, you can send FUTIDX_NIFTY_27AUG2020_XX_0 (near month), FUTIDX_NIFTY_24SEP2020_XX_0 (far month). 
//You can use same naming convention for Futures of Instruments from NFO, CDS, MCX Exchanges
//NFO Options Examples : OPTIDX_NIFTY_02JUL2020_CE_10000, OPTSTK_RELIANCE_30JUL2020_CE_1700
//CDS Futures Examples : FUTCUR_USDINR_26JUN2020_XX_0, FUTCUR_USDINR_29JUL2020_XX_0, FUTCUR_USDINR_27AUG2020_XX_0
//CDS Options Examples : OPTCUR_USDINR_29JUL2020_CE_75.5, OPTCUR_EURINR_29JUL2020_CE_80
//MCX Futures Examples : FUTCOM_CRUDEOIL_20JUL2020__0, FUTCOM_CRUDEOIL_19AUG2020__0, FUTCOM_CRUDEOIL_21SEP2020__0
//MCX Options Examples : OPTFUT_CRUDEOIL_16JUL2020_PE_2050, OPTFUT_GOLD_27JUL2020_PE_43700
//Important : Replace it with appropriate expiry date if this contract is expired

Subscribing to realtime data of NSE Indices
-------------------------------------------
//Use InstrumentIdenfier value "NIFTY 50", "NIFTY BANK", "NIFTY 100", etc.
//Use NSE_IDX as Exchange
//Please note that Indices Symbols have white space. For example, between NIFTY & 50, NIFTY & BANK above

Subscribing to realtime data of NSE Stocks 
------------------------------------------
//For EQ Series, use InstrumentIdentifier value BAJAJ-AUTO, RELIANCE, AXISBANK, LT, etc..
//To subscribe to realtime data of any other series, append the series name to symbol name 
//for example, to request data of RELIANCE CAPITAL from BE Series, use RELCAPITAL.BE
//EQ Series Symbols are mentioned without any suffix

// Please see symbol naming conventions here : 
// https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-convention/
*/

function SubscribeRealtime()
{
	if (connection.connected) 
	{
		var ExchangeName = "NFO";				//GFDL : Supported values : NSE (stocks), NSE_IDX (Indices), NFO (F&O), MCX & CDS (Currency)
		var InstIdentifier = "NIFTY-I";			//GFDL : NIFTY-I always represents current month Futures. 
		request = '{"MessageType":"SubscribeRealtime","Exchange":"' + ExchangeName + '","InstrumentIdentifier":"' + InstIdentifier + '"}';
		callAPI(request);
	}
}
```

### Sample Response

```text
D:\node-server>node WebsocketSample_NodeJS.js
Client Connected!
request: *****{"MessageType":"Authenticate","Password":"API_Key_is_Masked"}*****
request: *****{"MessageType":"SubscribeRealtime","Exchange":"NFO","InstrumentIdentifier":"NIFTY-I"}*****
Received: '{"MessageType":"Echo"}'
Received: '{"Complete":true,"Message":"Welcome!","MessageType":"AuthenticateResult"}'
Received: '{"AllowVMRunning":false,"MessageType":"AllowVMRunningResult"}'
Received: '{"AllowServerOSRunning":false,"MessageType":"AllowServerOSRunningResult"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112003,"ServerTime":1594112004,"AverageTradedPrice":10717.52,"BuyPrice":10740.0,"BuyQty":1350,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10740.0,"LastTradeQty":150,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10740.85,"SellQty":75,"TotalQtyTraded":11468850,"Value":122917629252.0,"PreOpen":false,"PriceChange":-14.85,"PriceChangePercentage":-0.14,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112004,"ServerTime":1594112005,"AverageTradedPrice":10717.52,"BuyPrice":10740.05,"BuyQty":75,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10740.5,"LastTradeQty":1050,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10741.5,"SellQty":75,"TotalQtyTraded":11469900,"Value":122928882648.0,"PreOpen":false,"PriceChange":-14.35,"PriceChangePercentage":-0.13,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"MessageType":"Echo"}'
request: *****{"MessageType":"SubscribeRealtime","Exchange":"NFO","InstrumentIdentifier":"NIFTY-I"}*****
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112005,"ServerTime":1594112006,"AverageTradedPrice":10717.52,"BuyPrice":10739.0,"BuyQty":75,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10739.65,"LastTradeQty":3450,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10740.0,"SellQty":3600,"TotalQtyTraded":11473350,"Value":122965858092.0,"PreOpen":false,"PriceChange":-15.2,"PriceChangePercentage":-0.14,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112006,"ServerTime":1594112007,"AverageTradedPrice":10717.52,"BuyPrice":10738.35,"BuyQty":150,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10739.0,"LastTradeQty":150,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10739.9,"SellQty":300,"TotalQtyTraded":11473500,"Value":122967465720.0,"PreOpen":false,"PriceChange":-15.85,"PriceChangePercentage":-0.15,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"MessageType":"Echo"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112007,"ServerTime":1594112008,"AverageTradedPrice":10717.52,"BuyPrice":10738.15,"BuyQty":75,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10738.35,"LastTradeQty":225,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10739.75,"SellQty":75,"TotalQtyTraded":11473725,"Value":122969877162.0,"PreOpen":false,"PriceChange":-16.5,"PriceChangePercentage":-0.15,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112008,"ServerTime":1594112009,"AverageTradedPrice":10717.53,"BuyPrice":10738.15,"BuyQty":150,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10738.35,"LastTradeQty":75,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10739.95,"SellQty":75,"TotalQtyTraded":11473800,"Value":122970795714.0,"PreOpen":false,"PriceChange":-16.5,"PriceChangePercentage":-0.15,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"MessageType":"Echo"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112009,"ServerTime":1594112010,"AverageTradedPrice":10717.53,"BuyPrice":10738.35,"BuyQty":75,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10738.5,"LastTradeQty":975,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10739.95,"SellQty":75,"TotalQtyTraded":11474775,"Value":122981245305.75,"PreOpen":false,"PriceChange":-16.35,"PriceChangePercentage":-0.15,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1594112010,"ServerTime":1594112011,"AverageTradedPrice":10717.53,"BuyPrice":10738.65,"BuyQty":75,"Close":10754.85,"High":10767.5,"Low":10657.7,"LastTradePrice":10740.0,"LastTradeQty":375,"Open":10761.9,"OpenInterest":12112200,"QuotationLot":0.0,"SellPrice":10740.0,"SellQty":3525,"TotalQtyTraded":11475150,"Value":122985264379.5,"PreOpen":false,"PriceChange":-14.85,"PriceChangePercentage":-0.14,"OpenInterestChange":874500,"MessageType":"RealtimeResult"}'
Received: '{"MessageType":"Echo"}'
```

– To know how each function works, you can uncomment required function at top (one function at a time), save the sample and run it.

### Trouble-shooting

#### The code outputs nothing

– Please make sure you have entered the Endpoint, Port & API Key in the sample code, saved the changes before running it.
– To see realtime data in action, you should run the sample during market hours
– If you are behind firewall, please speak to your network administrator and get the endpoint URL and port whitelisted in your firewall

#### I can see some output but I dont see all symbols requested / I get "Exchange Not Enabled." message

– SubscribeRealtime function requests data of a symbol (NIFTY-I from NFO Exchange in the unedited sample). You will need to make sure you have paid for the subscription of the Exchange you are using in your request.

– Some examples use Contractwise symbols which might have expired. Please check code and change the symbols. Please check Symbol Naming Conventions page for more information about various ways to name symbols (especially Futures and Options).

#### I have requested data but it takes lot of time to receive the response

– If you have requested entire GetHistory, GetInstruments (for NSE / NFO/BSE/BFO), GetExchangeSnapshot or GetLastQuoteOptionChain, these functions return thousands of rows. So it is possible that it takes more time than other requests to return the data

– Please make sure you are on fast internet connection.

#### I have other issues not listed here

– Please drop an email to [developer@globaldatafeeds.in](mailto:developer@globaldatafeeds.in?Subject=WebSockets%20API%20Query) with following :
1. Details of the request sent (exact syntax), expected response & received response
2. Screenshot of the request & response
3. API Key used

Once received, we will check and revert at the earliest (typically within few hours for normal queries).

---

## WebSockets – Postman

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-postman/ — last updated 2024-10-22T13:03:37 — captured 2026-07-15

### Introduction

To connect using WebSockets API, you will need following information :

– "endpoint" to connect to,
– "port number" to connect to,
– "API Key"

You will get all of the above once you purchase the API from our team

The flow of operations should be as follows :

1. Make a connection to the **ws://endpoint:port**
2. Send Authentication Request using **API Key**
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

**Ready, lets gets started then View pdf**

**[Download Pdf (Containes of full step guided with screenshot )](https://globaldatafeeds.in/resources/_resources/How%20to%20use%20websocket%20in%20Postman.zip)** – [**View**](https://globaldatafeeds.in/resources/_resources/How%20to%20use%20websocket%20in%20Postman.pdf)

**I have other issues on regarding data or feeds oriented**

– Please drop an email to [developer@globaldatafeeds.in](mailto:developer@globaldatafeeds.in?Subject=WebSockets%20API%20Query) with following :
1. Details of the request sent (exact syntax), expected response & received response
2. Screenshot of the request & response
3. API Key used

Once received, we will check and revert at the earliest (typically within few hours for normal queries).
