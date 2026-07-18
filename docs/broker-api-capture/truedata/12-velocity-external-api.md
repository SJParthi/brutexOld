# TrueData — Velocity External API (COM / desktop) & Sandbox Client

> Sources:
> - https://feedback.truedata.in/knowledge-base/article/use-the-external-api (official install/use article)
> - https://www.truedata.in/ footer downloads (installer URLs)
> - https://wstest.truedata.in/ (sandbox JS client)

The Velocity External API is a **Windows COM/.NET external API** shipped with TrueData Velocity (the desktop real-time/historical data plugin). It exposes Velocity's feed to third-party applications (Excel, custom apps) on the same machine. It is distinct from the WebSocket/REST Market Data API.

## Downloads (official installer URLs)

| Item | URL |
|---|---|
| TrueData Velocity 2.0 | https://www.truedata.in/downloads/TrueData.Velocity.2.0.Setup.msi |
| Velocity Excel Plugins (32-bit) | https://www.truedata.in/downloads/TrueData.Excel.x86.msi |
| Velocity Excel Plugins (64-bit) | https://www.truedata.in/downloads/TrueData.Excel64.x64.msi |
| Velocity External API (32-bit) | https://www.truedata.in/downloads/TrueData.Velocity.External.Setup.x86.msi |
| Velocity External API (64-bit) | https://www.truedata.in/downloads/TrueData.Velocity.External.Setup.x64.msi |
| MS C++ 2010 Redist (32-bit) | https://www.truedata.in/downloads/vcredist_x86.exe |
| MS C++ 2010 Redist (64-bit) | https://www.truedata.in/downloads/vcredist_x64.exe |

## Install & use (KB article, condensed but step-faithful)

1. Download the External API (x86 or x64) installer from the truedata.in Downloads section.
2. Install path: `C:\Program Files (x86)\TrueData\TrueData Client API x86\Release` (x64: `C:\Program Files\TrueData\TrueData Client API\Release`).
3. In the `Release` folder: right-click `TrueData.Velocity.Sample` → Properties → **Compatibility** tab → tick **Run as administrator** → OK; then **Security** tab → Edit → for each Group/username tick all Allow permissions → OK.
4. Run `TrueData.Velocity.Sample`; type a symbol name in **CAPITAL letters only** (incorrect and expired symbols won't work) → click **Initialize** → click **Start RT** → real-time data streams.
5. For COM: go to the `Release COM` folder; right-click `TrueData.Velocity.Sample.Com` → Properties → Compatibility → **Run as administrator**; Security tab → allow all permissions for each Group/username → OK.
6. Run `TrueData.Velocity.Sample.COM`; enter symbol (CAPITALS) → **Initialize** → **Start RT**.

The sample apps (`TrueData.Velocity.Sample` / `.Sample.Com`) double as reference implementations for integrating the DLL/COM interface. Support: LiveChat (https://secure.livechatinc.com/licence/5868191/v2/open_chat.cgi?groups=0) or ticket.

## Sandbox JS client (wstest.truedata.in) — layout reference

The official sandbox is a single page titled "WebSocket/REST JS Client" (© 2023 TrueData Info) with two panels:

**TrueData Websocket RT Service** — "Real Time (Port 8082)" selector; buttons: `Connect`, `Disconnect`, `Add NSE Symbols`, `Add BSE Symbols`, `Add MCX Symbols`, `Add CDS Symbols`, `Remove Symbols`; a `Request:` text area + `Send Text`; a `Message Log` (+ `Clear log`) that prints every raw JSON frame from the server.

**TrueData REST History Service** —
- `https://auth.truedata.in` with `User Id`, `Password`, `grant_type` inputs, `Login`/`Logout` buttons and a `Login Response` display (bearer-token flow);
- `https://history.truedata.in` with `Symbol`, `Interval` (dropdown: `1min, 2min, 3min, 5min, 10min, 15min, 30min, EOD, Tick`), `DelVolume` (delivery-volume flag), `From`, `To`, `Get` button, a `Request :` display (shows the exact GET URL fired) and a results table with columns `Sr | Timestamp | LTP | O | H | L | C | V | OI`.

Use it to verify feed contents against your own client ("Please use this page often to confirm the data flow whenever you think that your code is not behaving as it should" — Getting Started KB).

## Other TrueData integration surfaces (pointers)

- **Amibroker RTD plugin / Local DB**, **NinjaTrader 7/8**, **MetaStock**, **MotiveWave**, **MultiCharts**, **Advance Get**, **MS Excel** — retail Velocity-based integrations, each with its own KB category under https://feedback.truedata.in/knowledge-base (out of API scope; symbol formats per platform captured in 06-symbols-master.md §4).
- **Velocity integrations list**: https://www.truedata.in/integrations/listing/8
- **NinjaTrader feed product**: https://www.truedata.in/products/ninjatrader
- **Mutual Fund API**: contact via ticket (https://feedback.truedata.in/ticket/add) — no public docs.
