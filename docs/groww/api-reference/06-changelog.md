# 06 — Changelog · Groww Trading API (Python SDK)

> **Source:** https://groww.in/trade-api/docs/python-sdk/changelog
> **Verified:** 12 June 2026 · **Latest version shown:** `1.5.0` (3 Dec 2025)

Track all notable changes, improvements, and new features added to the Groww Python SDK.

## [1.5.0] — 3rd December, 2025
**Added**
- Commodity trading support on MCX exchange

## [1.4.0] — 2nd December, 2025
**Added**
- Support for retrieving user profile information via the `get_user_profile` API

## [1.3.0] — 24th November, 2025
**Added**
- Option Chain retrieval API

## [1.2.0] — 29th October, 2025
**Added**
- Smart Orders support (GTT, OCO) with client and examples

**Documentation**
- Portfolio, annexures and index updates: add MCX-not-supported note; include `realised_pnl` in positions payloads and schemas

## [1.1.0] — 8th October, 2025
**Fixed**
- NATS ping handling stability improvements

## [1.0.0] — 3rd October, 2025
**Added**
- Historical data retrieval APIs

**Documentation**
- Backtesting documentation: comprehensive guide and payload updates

## [0.0.10] — 22nd September, 2025
**Fixed**
- Standardized header builder; fix Authorization formatting

**Documentation**
- New authentication documentation

## [0.0.9] — 4th September, 2025
**Added**
- Checksum-based token generation for authentication

## [0.0.8] — 11th June, 2025
**Added**
- TOTP (Time-based One-Time Password) support

## [0.0.7] — 21st May, 2025
**Fixed**
- Feed issue with parallel thread execution
- Feed live-data unsubscribe response handling
- Protobuf response parsing

**Improved**
- Feed changes and historical data interval updates

**Documentation**
- Changelog and docs updates

## [0.0.5] — 21st April, 2025
**Changed**
- Packaging and licensing updates; project cleanup

## [0.0.4] — 25th March, 2025
**Added**
- Exception handling in feed

## [0.0.3] — 25th March, 2025
**Added**
- Multiple instrument support in feed with tests
- Order margin API
- Bulk LTP support
- Instruments as DataFrame helper
- Segment parameter in `get_position_for_trading_symbol`

**Fixed**
- Order detail retrieval
- Base SDK NATS testing and fixes

**Improved**
- Default page value set to zero
- Renamed `get_latest_price_data` to `get_quote`
- Changed `GrowwClient` to `GrowwAPI`
- Request/response structure adjustments

**Documentation**
- NATS Order/Positions feed documentation
- Documentation structure improvements and updates

## [0.0.1] — 27th February, 2025
**Initial Release** — the foundational release of the Groww Python SDK with core trading capabilities.

**Core Features**
- **Order Management:** Complete order lifecycle management
  - Place, modify, and cancel orders across Equity & F&O segments
  - Support for multiple order types: Market, Limit, Stop Loss, and Stop Loss Market
  - Order detail retrieval and tracking
- **Portfolio Management:**
  - Fetch holdings with detailed quantity breakdowns
  - Access positions for both CASH and F&O segments
  - Real-time position tracking
- **Live Market Data** via NATS WebSocket:
  - Real-time quotes and LTP (Last Traded Price) streaming
  - Order book depth (market depth) updates
  - Order and position update feeds
  - Custom callback support for event-driven applications
- **Instrument Management:**
  - Download and search instrument master data
  - Support for Equity, F&O, Currency, and Commodity segments
  - ETF instrument type support
- **Historical Data:**
  - Historical candle data retrieval
  - Support for multiple timeframes
- **Authentication & Security:**
  - API Key based authentication
  - Secure token management
- **Developer Experience:**
  - Comprehensive error handling with custom exception classes
  - Detailed documentation with code examples
  - Python 3.9+ compatibility

---

> **Naming history to watch for in old code:** `GrowwClient` → **`GrowwAPI`** (since 0.0.3); `get_latest_price_data` → **`get_quote`** (since 0.0.3).
