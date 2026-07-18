# TrueData — Official C#/.NET SDK: `TrueData-DotNet` (v1.4.7)

> Source: https://www.nuget.org/packages/TrueData-DotNet/ (fetched 2026-07-15; v1.4.7 published 7/29/2025 — "Fixes for compressed payload"; MIT license; owner `metacode`; .NET Standard 2.0)
> Dependencies: K4os.Compression.LZ4 (>= 1.2.15), Newtonsoft.Json (>= 13.0.1), System.Runtime.CompilerServices.Unsafe (>= 5.0.0)

## Install

```
dotnet add package TrueData-DotNet --version 1.4.7
NuGet\Install-Package TrueData-DotNet -Version 1.4.7
<PackageReference Include="TrueData-DotNet" Version="1.4.7" />
paket add TrueData-DotNet --version 1.4.7
#r "nuget: TrueData-DotNet, 1.4.7"
```

Minimum framework: .NET Standard 2.0 (works on .NET Framework 4.6.1+, .NET Core 2.0+, .NET 5–10).

## Coverage (verbatim)

**WebSocket APIs** — Live ticks (default), 1-min bars, 5-min bars, tick+1min, tick+5min, tick+1min+5min, 1min+5min, Option Greek. (Non-default feeds may require exchange approvals.)

**REST APIs** — Bar/OHLC Data, Ticks Data (ticks), Gainers / Losers, Bhavcopy.

**Analytics and Greeks — ADDON** — Top Gainers / Losers, Industry Gainer / Losers, Options Greeks.

## WebSocket usage

```csharp
TDWebSocket tDWebSocket;  // Truedata WebSocket
tDWebSocket = new TDWebSocket("your_id", "your_pwd", "websocket_url", port_no); // Login to Truedata WebSocket service
// e.g. new TDWebSocket("user_id", "user_pwd", "wss://replay.truedata.in", 8082) for replay

TDMaster tDMaster;        // Truedata Master
tDMaster = new TDMaster("your_id", "your_pwd");   // To download latest master

// WebSocket important event callbacks
tDWebSocket.OnConnect    += TDWebSocket_OnConnect;
tDWebSocket.OnDataArrive += TDWebSocket_OnDataArrive;
tDWebSocket.OnClose      += TDWebSocket_OnClose;
tDWebSocket.OnError      += TDWebSocket_OnError;

tDWebSocket.ConnectAsync();  // Connect to WebSocket
```

### Subscribe

```csharp
// To start WebSocket streaming one need to subscribe symbols or tokens as follows:
string[] symbols = { "NIFTY-I", "RELIANCE", "NIFTY 50", "CRUDEOIL-I", "USDINR-I", "SENSEX", "RELIANCE_BSE" };
tDWebSocket.Subscribe(symbols);
```

### OnDataArrive — raw JSON message dispatch (verbatim)

"Whenever the trade happens or bid-ask changes, event will be called Timestamp of Symbol, LTP, Volume, Open, High, Low, Close and OI for symbols which are mentioned in subscribe symbol list":

```csharp
private static void TDWebSocket_OnDataArrive(object sender, EventDataArgs e)
{
    if (e.JsonMsg.Contains("bidask"))
    {
        BidAsk b = JsonConvert.DeserializeObject<BidAsk>(e.JsonMsg);
        Console.WriteLine(b.SymbolId + "-" + b.Timestamp + "-" + b.Bid + "-" + b.Ask + "-" + b.BidQty);
    }
    else if (e.JsonMsg.Contains("trade"))
    {
        SnapData t = JsonConvert.DeserializeObject<SnapData>(e.JsonMsg);
        Scrip s = scrips.Where(v => v.symbolid == t.SymbolId).FirstOrDefault();
        if (s != null)
            Console.WriteLine(s.symbol + "-" + t.SymbolId + "-" + t.Timestamp + "-" + t.LTP + "-" + t.Volume + "-" + t.Open + "-" + t.High + "-" + t.Low + "-" + t.PrevClose);
    }
    else if (e.JsonMsg.Contains("HeartBeat"))
    {
        HeartBeat hb = JsonConvert.DeserializeObject<HeartBeat>(e.JsonMsg);
        Console.WriteLine("Heartbeat " + hb.timestamp + "-" + hb.message);
    }
    else if (e.JsonMsg.Contains("TrueData"))   // connect acknowledgement
    {
        Console.WriteLine("Connected");
        string[] symbols = { "NIFTY-I", "RELIANCE", "NIFTY 50", "CRUDEOIL-I", "USDINR-I", "SENSEX", "RELIANCE_BSE" };
        tDWebSocket.Subscribe(symbols);
    }
}
```

Model classes: `BidAsk` (SymbolId, Timestamp, Bid, Ask, BidQty…), `SnapData` (SymbolId, Timestamp, LTP, Volume, Open, High, Low, PrevClose…), `HeartBeat` (timestamp, message), `Scrip` (symbol, symbolid — from TDMaster).

## History REST usage (TDHistory)

```csharp
TDHistory tDHistory;
tDHistory = new TDHistory("your_id", "your_pwd");
tDHistory.login();
```

### Endpoint methods (verbatim examples)

```csharp
// GetBars - OHLC: Symbol, Number of Bar, Boolean for Json/csv response, time interval
string csvResponse = tDHistory.GetBarHistory("ACC", DateTime.Today.AddHours(-1), DateTime.Now, false, Constants.Interval_1Min, false);

// GetTicks
string csvTickData = tDHistory.GetTickHistory("ACC", false, DateTime.Now.AddHours(-0.5), DateTime.Now, false, true);

// GetLastNBar
string csvLastNBar = tDHistory.GetLastNBars("HDFC", 1, false, "15min");

// GetLastNTicks
string csvLastNTicks = tDHistory.GetLastNTicks("HDFC", true, 1);

// GetBhavCopyStatus
Console.WriteLine(tDHistory.GetBhavCopyStatus("BSEFO", true));

// GetBhavCopy
string csvBhavCopy = tDHistory.GetBhavCopy("EQ", DateTime.Today, true);

// GetTopGainers
string csvGainers = tDHistory.GetTopGainers("NSEEQ", true, 10);

// GetTopLosers
string csvLosers = tDHistory.GetTopLosers("NSEEQ", true, 10);

// GetCorporateActions
string csvCorpAction = tDHistory.GetCorporateActions("AARTIIND", true);

// Get52WeekHighLow
string str52wHighLow = tDHistory.Get52WeekHighLow("RELIANCE");
```

Interval constants: `Constants.Interval_1Min` etc.; string intervals like `"15min"` are accepted by GetLastNBars. Booleans select CSV vs JSON output and symbol-id inclusion (mirroring REST `response` / `getSymbolId`).

## Version history (NuGet)

1.4.7 (7/29/2025, current — compressed payload fixes) · 1.4.6 (12/9/2024) · 1.4.5 (11/19/2024) · 1.4.2, 1.4.1, 1.4.0, 1.3.x, 1.2.x (2023–2024; all ≤1.4.2 deprecated for critical bugs). Total downloads ~23.8K.
