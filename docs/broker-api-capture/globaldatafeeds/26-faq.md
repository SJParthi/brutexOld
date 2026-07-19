# GlobalDataFeeds APIs — FAQ

Frequently asked questions covering cloud instance sizing, symbol/request limits, connection errors, delayed snapshot data, NSE series types, GetHistory vs GetHistoryAfterMarket, and OHLC differences across platforms.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/faqs/faqs/

---

## FAQ's

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/faqs/faqs/ — last updated 2026-03-10T16:25:22 — captured 2026-07-15

### How to choose correct instance (type/size) on the AWS cloud for optimum price / performance ?

Please go through the below information which will help you to take the decision while choosing Instance type.
Stock Market applications require consistent performance hence try to choose compute optimized, memory optimized or generic instances (for example, M4 or M5 Series on AWS).

Please DO NOT choose instances with burstable CPU types (for example T2, T3 on AWS). These types of instances are suitable for intermittent load when burst of performance is required for short period of time. Since stock market applications require consistent performance throughout, these instance types (T2 or T3 Series on AWS) are NOT suitable for stock market applications.

**Volume Size :**
Choosing the right type of instance is just the first step. The persistent storage on cloud, known as Volume on AWS, should always be adequate as per the requirements. Please note that AWS assigns 3 IOPS [1] per GB of Volume. So higher the IOPS requirement, higher should be the size of the Volume. For example, when Volume size is 250GB, AWS assigns 250*3=750 IOPS for that instance. So you should choose Volume size not only from your storage requirements but also from IOPS requirements.

**Volume Type :**
Now-a-days, most cloud vendors provide SSD as the default persistant storage type. This is most ideal as SSD offer much faster data read/write than conventional storage types.

[1] What is IOPS :
[https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-io-characteristics.html](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-io-characteristics.html)

### What is the Symbol limit / Instrument limit?

The Symbol limit is set per API key based on the user's requirements. If the user exceeds this symbol limit, they will receive the message: "Reached instrument limitation."

### What is the Request per hour limit?

The Request per hour limit is the maximum number of requests allowed per hour for an API key, based on the user's requirements. For example, this limit could be 1800, 3600, or 7200 requests per hour.
If a user has a limit of 1800 requests per hour and consumes all of them before the hour is up, they will need to wait until the next hour for the request count to reset.

### What happens if I exceed my Request per hour limit?

If the Request per hour limit is reached, you will need to wait for the remainder of the hour before the limit resets, allowing you to start with a fresh quota of requests. This prevents exceeding the hourly limit and ensures system stability.

### What is the relation between the Symbol limit and the Request per hour limit?

The Symbol limit impacts how the Request per hour limit is consumed. For example, if your Symbol limit is 100 and your Request per hour limit is 1800, subscribing to 100 symbols of an exchange will consume 100 requests.
If you then unsubscribe from these 100 symbols and subscribe to a different set of 100 symbols, total 300 requests i.e. 100 (original) + 100 (unsubscription) + 100 (new subscription) requests from your 1800-hourly limit.

### Facing "unable to connect" error

Please check if you are able to telnet the endpoint from the same machine from where you are not able to connect.

**How to check telnet ?**
1. Open command prompt on the machine from which you are facing connection issue

2. Type : telnet \<address\> \<port number\> [hit ENTER button]
For example, if you are trying to connect to endpoint ws://test.lisuns.com on port 4575, you should type : telnet test.lisuns.com 4575 [hit ENTER button]

3. If the screen goes blank, it means you are able to reach upto the endpoint and issue is elsewhere. However if you see message like "connecting to test.lisuns.com…could not open connection to the host, on port 4575: connect failed", it means you are not able to reach upto our servers.

4. If you get "connect failed" message, please double check the endpoint and port entered to ensure there are no typos. Also please confirm with our team (with screenshot of console) that the endpoint is currently active (some test endpoints are closed over weekends and market holidays).

5. Please contact with your firewall team and inform them to allow data access (both to and from) test.lisuns.com on port 4575

To know how telnet works, please see this article :
[https://phoenixnap.com/kb/telnet-windows](https://phoenixnap.com/kb/telnet-windows)

### Facing "connection refused" error

Due to server Maintenance activity the server throw you the message as "Connection refused"

Find the below details for the Server Maintenance:

02:00:00 To 02:30:00 AM (MidNight)
08:00:00 To 08:30:00 AM (Morning )
Some times morning activity (BOD Process) till 08:45:00 AM.

if you are connecting and setting up any tasks/cron jobs, etc., please connect after 8:45AM

### What is Delayed Snapshot ?

The DelayedSnapshot is a feature that allows user to introduce a delay in the data retrieval process for specific exchanges with API key. This feature is particularly useful when you need to control the timing of data access, or for other operational requirements.

### How Delayed Snapshot Works?

When the Delayed Feature is enabled for an API key:

**Delay Configuration:** A specific delay time (e.g., in minutes) is configured for each exchange.

**API Request Handling:** System checks if a delay is set for the exchange involved.

**Execution of Delay:** If a delay is configured, the system will process the request as below.
***For example:** For 15 min delay*
*Request sent at 09:15:00*

*Response received at*

*09:30:01        Last Trade Time (IST): 06-09-2024 09:15:00*

*09:31:01        Last Trade Time (IST): 06-09-2024 09:16:00*

*09:32:01        Last Trade Time (IST): 06-09-2024 09:17:00*

**Impact on Data Retrieval:** The data retrieved will reflect the delayed time, which might be useful for backtesting scenarios.

### What is the difference between realtime snapshot and delayed snapshot data provided by GFDL ?

The difference between real-time snapshot and delayed snapshot data provided by GFDL lies primarily in the timing and accessibility of the data:

***Realtime Snapshot***

- This data provides live, up-to-the-second market information. Prices, volumes, and other market conditions are captured and delivered as they occur.
- Useful for active traders and institutions who need up-to-the-second updates to make trading decisions.
- Reflects the live prices, volumes, and other market conditions without any delay.
- Commonly used for high-frequency trading (HFT), algorithmic trading, and intraday analysis.

***Delayed Snapshot Data:***

- Market data is provided with a configured time delay (e.g., 1,2, 5,10,15,30 minutes etc.).
- Delay Configuration: A specific delay time is configured for each exchange.
- This feature is beneficial for controlling the timing of data access, or for specific operational needs, such as backtesting.

In summary, real-time snapshot data is live and critical for real-time decision-making, while delayed snapshot data serves more for research or lower-priority trading activities due to its delayed nature. Response Fields will remain the same for realtime as well as delayed snapshot.

### Can you share the list of functions which can be used for getting realtime snapshot data and delayed snapshot data ?

**WEBSOCKET, DOTNET, COM API
GFDL provides Realtime Snapshot data with following API functions:**

- GetSnapshot
- GetExchangeSnapshot
- GetHistory
- SubscribeSnapshot

**GFDL provides Delayed Snapshot data with following API functions:**

- GetSnapshot (Delayed)
- GetExchangeSnapshot (Delayed)
- GetHistory (Delayed)
- SubscribeSnapshot (Delayed)

**REST API**

**GFDL provides Snapshot data on demand request for following API functions:**

- GetSnapshot
- GetExchangeSnapshot
- GetHistory

**GFDL provides Delayed Snapshot data on demand request for following API functions:**

- GetSnapshot (Delayed)
- GetExchangeSnapshot (Delayed)
- GetHistory (Delayed)

### Is there any difference in the fields returned by functions used for getting real-time and delayed snapshot data?

No, there is no difference in the fields returned by the functions for real-time and delayed snapshot data. Both functions return the same set of fields. The only difference is the timing: real-time data is provided instantly, while delayed data is supplied after a specified delay.

### How GetHistory function is applicable for Delayed feature?

The GetHistory function with the Delayed Feature will return historical data based on the configured delay.

**For example:**

GetHistory Delayed feature for NIFTY-I.NFO

Delay set to the key for NFO : 900 sec (15 min)

Request sent at 15:15:00 with Periodicity=Minute and Period=1
Response Received at Next moment

Timestamp of the last record in response : September 9, 2024 15:00:00 PM

In this example, with a delay of 900 seconds (15 minutes), the request sent at 15:15:00 will provide data as if it was requested at 15:00:00. Therefore, the response will show the Last Trade Time as 15:00:00.

Even though the request is made at 15:15:00, the delay ensures that data is retrieved as it was available 15 minutes prior.

### What are NSE series types, and why are they important?

NSE series types classify securities based on trading rules, settlement cycles, and eligibility criteria. These classifications help the NSE manage trading risks, ensure regulatory compliance, and enhance market surveillance. For traders, understanding series types provides insights into the trading restrictions and characteristics of securities. [*Download NSE Series Details*](https://globaldatafeeds.in/resources/_resources/GlobalDatafeeds-NSE%20Series%20Details.pdf).
For more details, refer to the [*NSE Legend of Series*](https://www.nseindia.com/market-data/legend-of-series).

### What is the difference between GetHistory and GetHistoryAfterMarket?

**GetHistory** and **GetHistoryAfterMarket** are designed for different use cases and are **not enabled together for the same API key**.

- **GetHistory**
  This API allows users to fetch **historical candle data during live market hours as well as after market**. It is mainly used by applications that require **intraday data access in real-time or during the trading session**.
- **GetHistoryAfterMarket**
  This API is intended for users who **do not require intraday data during market hours** but need the **complete historical data only after the market has closed or on the next day**. It was specifically developed to support such market requirements.

**Important:**
For a given **API key**, only **one of these APIs can be enabled**:

- If **GetHistory** is enabled → **GetHistoryAfterMarket will be disabled**.
- If **GetHistoryAfterMarket** is enabled → **GetHistory will be disabled**.

This ensures that the API usage aligns with the intended data access pattern.

### Why do OHLC values from GetHistory sometimes differ from TradingView or other chart platforms?

We have verified the **GetHistory candle data** on our end, and the **total traded volume we receive matches the exchange day volume**. This confirms that the **data feed itself is correct**.

However, you may sometimes notice slight differences in **Open, High, and Low (OHLC)** values when comparing with platforms like TradingView. This happens because **each data vendor constructs intraday candles using their own tick aggregation and filtering logic**.

During live markets, **hundreds of trades can occur every second**. Different platforms may:

- Capture slightly different **tick sequences**
- Apply **different filtering rules**
- Aggregate ticks differently while building **minute or intraday candles**

Because of this, **Open, High, and Low values may vary slightly between platforms**, even though the **Close price and total traded Volume usually match**.

This is a **common industry behavior**. Nithin Kamath, founder of Zerodha, has also explained that **chart values may not perfectly match across platforms** because each platform may capture and process **different tick points while building candles**.

Since the **exchange day volume and the volume received in our data match**, there is **no issue with the data feed**. Minor differences in **intraday candle values across vendors or charting platforms are expected**.

**Reference:**
Zerodha explanation on chart differences:
[https://support.zerodha.com/category/trading-and-markets/charts-and-orders/charts/articles/difference-in-charts](https://support.zerodha.com/category/trading-and-markets/charts-and-orders/charts/articles/difference-in-charts)
