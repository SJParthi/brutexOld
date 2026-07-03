# Groww Trade API — Changelog (v0.0.1 → v1.5.0)

> **Source:** https://groww.in/trade-api/docs/python-sdk/changelog
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Changelog

Track all notable changes, improvements, and new features added to the Groww Python SDK.

# [Releases](#releases)

## [[1.5.0] - 3rd December, 2025](#150---3rd-december-2025)

### [Added](#added)

- Commodity trading support on MCX exchange

## [[1.4.0] - 2nd December, 2025](#140---2nd-december-2025)

### [Added](#added-1)

- Support for retrieving user profile information via the `get_user_profile` API.

## [[1.3.0] - 24th November, 2025](#130---24th-november-2025)

### [Added](#added-2)

- Option Chain retrieval API

## [[1.2.0] - 29th October, 2025](#120---29th-october-2025)

### [Added](#added-3)

- Smart Orders support (GTT, OCO) with client and examples

### [Documentation](#documentation)

- Portfolio, annexures and index updates: add MCX-not-supported note; include `realised_pnl` in positions payloads and schemas

## [[1.1.0] - 8th October, 2025](#110---8th-october-2025)

### [Fixed](#fixed)

- NATS ping handling stability improvements

## [[1.0.0] - 3rd October, 2025](#100---3rd-october-2025)

### [Added](#added-4)

- Historical data retrieval APIs

### [Documentation](#documentation-1)

- Backtesting documentation: comprehensive guide and payload updates

## [[0.0.10] - 22nd September, 2025](#0010---22nd-september-2025)

### [Fixed](#fixed-1)

- Standardized header builder; fix Authorization formatting

### [Documentation](#documentation-2)

- New authentication documentation

## [[0.0.9] - 4th September, 2025](#009---4th-september-2025)

### [Added](#added-5)

- Checksum-based token generation for authentication

## [[0.0.8] - 11th June, 2025](#008---11th-june-2025)

### [Added](#added-6)

- TOTP (Time-based One-Time Password) support

## [[0.0.7] - 21st May, 2025](#007---21st-may-2025)

### [Fixed](#fixed-2)

- Feed issue with parallel thread execution
- Feed live-data unsubscribe response handling
- Protobuf response parsing

### [Improved](#improved)

- Feed changes and historical data interval updates

### [Documentation](#documentation-3)

- Changelog and docs updates

## [[0.0.5] - 21st April, 2025](#005---21st-april-2025)

### [Changed](#changed)

- Packaging and licensing updates; project cleanup

## [[0.0.4] - 25th March, 2025](#004---25th-march-2025)

### [Added](#added-7)

- Exception handling in feed

## [[0.0.3] - 25th March, 2025](#003---25th-march-2025)

### [Added](#added-8)

- Multiple instrument support in feed with tests
- Order margin API
- Bulk LTP support
- Instruments as DataFrame helper
- Segment parameter in `get_position_for_trading_symbol`

### [Fixed](#fixed-3)

- Order detail retrieval
- Base SDK NATS testing and fixes

### [Improved](#improved-1)

- Default page value set to zero
- Renamed `get_latest_price_data` to `get_quote`
- Changed `GrowwClient` to `GrowwAPI`
- Request/response structure adjustments

### [Documentation](#documentation-4)

- NATS Order/Positions feed documentation
- Documentation structure improvements and updates

## [[0.0.1] - 27th February, 2025](#001---27th-february-2025)

### [Initial Release](#initial-release)

The foundational release of the Groww Python SDK with core trading capabilities.

#### [Core Features](#core-features)

- **Order Management**: Complete order lifecycle management

  - Place, modify, and cancel orders across Equity & F&O segments
  - Support for multiple order types: Market, Limit, Stop Loss, and Stop Loss Market
  - Order detail retrieval and tracking
- **Portfolio Management**:

  - Fetch holdings with detailed quantity breakdowns
  - Access positions for both CASH and F&O segments
  - Real-time position tracking
- **Live Market Data** via NATS WebSocket:

  - Real-time quotes and LTP (Last Traded Price) streaming
  - Order book depth (market depth) updates
  - Order and position update feeds
  - Custom callback support for event-driven applications
- **Instrument Management**:

  - Download and search instrument master data
  - Support for Equity, F&O, Currency, and Commodity segments
  - ETF instrument type support
- **Historical Data**:

  - Historical candle data retrieval
  - Support for multiple timeframes
- **Authentication & Security**:

  - API Key based authentication
  - Secure token management
- **Developer Experience**:

  - Comprehensive error handling with custom exception classes
  - Detailed documentation with code examples
  - Python 3.9+ compatibility

[Previous

Exceptions](/trade-api/docs/python-sdk/exceptions)
