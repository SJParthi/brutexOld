# Groww Trading API — Rate Limits

> Sources:
> - https://groww.in/trade-api/docs/curl (Introduction > Rate Limits)
> - https://groww.in/trade-api/docs/python-sdk (Introduction > Rate Limits)
> Captured: 2026-07-15

The rate limits are applied at the **type level, not on individual APIs**. This means that all APIs grouped under a type (e.g., Orders, Live Data, Non Trading) share the same limit. If the limit for one API within a type is exhausted, all other APIs in that type will also be rate-limited until the limit window resets.

| Type | Requests | Limit (Per second) | Limit (Per minute) |
| ----------- | ----------------------------------------------------------------- | ------------------ | ------------------ |
| Orders | Create, Modify and Cancel Order | 10 | 250 |
| Live Data | Market Quote, LTP, OHLC | 10 | 300 |
| Non Trading | Order Status, Order list, Trade list, Positions, Holdings, Margin | 20 | 500 |

Additional limits stated elsewhere in the docs:

- **Live feed (WebSocket)**: up to **1000 subscriptions** are allowed at a time. (Python SDK Introduction; Feed page: "You can subscribe for up to 1000 instruments at a time.")
- **Get LTP / Get OHLC**: up to **50 instruments** per API call.
- **Get Trades for Order**: maximum `page_size` is **50**.
- **Get Order List**: maximum `page_size` is **100** (cURL docs; Python SDK schema text says max 25 — inconsistent in the source docs).
- **Smart order list**: `page` 0–500, `page_size` 1–50 (default 10); date window must not exceed one month.
- **Historical candles (backtesting)**: per-request duration caps — 1/2/3/5 min → 30 days; 10/15/30 min → 90 days; 1 hour/4 hours/1 day/1 week/1 month → 180 days.
- **Deprecated historical candle range endpoint**: 1 min → 7 days (3 months back), 5 min → 15 days, 10 min → 30 days, 60 min → 150 days, 240 min → 365 days, 1440 min → 1080 days (full history), 10080 min → no limit (full history).
- Checksum timestamps for token generation are valid for **10 minutes**.
- Access tokens (approach 1) expire **daily at 6:00 AM**.

Exceeding the rate limit raises `GrowwAPIRateLimitException` in the Python SDK (see [16-errors-exceptions.md](./16-errors-exceptions.md)).
