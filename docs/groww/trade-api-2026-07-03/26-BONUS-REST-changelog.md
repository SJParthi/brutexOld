# Groww Trade API — REST API: Changelog

> **Source:** https://groww.in/trade-api/docs/curl/changelog
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Changelog

Release history and updates for Groww Trading APIs

# [Releases](#releases)

All notable changes to the Groww Trading API will be documented in this file.

## [[1.0]](#10)

### [December 2025](#december-2025)

#### [Commodity trading support on MCX exchange - 3rd December 2025](#commodity-trading-support-on-mcx-exchange---3rd-december-2025)

- Added Commodities trading

#### [User Profile API - 2nd December 2025](#user-profile-api---2nd-december-2025)

- Added User Profile API

### [November 2025](#november-2025)

#### [Option Chain retrieval API - 24th November 2025](#option-chain-retrieval-api---24th-november-2025)

- Added option chain API

#### [Portfolio Enhancement - 8th November 2025](#portfolio-enhancement---8th-november-2025)

- Added `realised_pnl` field in positions API
- Accurate profit/loss tracking for closed positions
- Available across all position queries

### [October 2025](#october-2025)

#### [Historical Data APIs - 3rd October 2025](#historical-data-apis---3rd-october-2025)

- Comprehensive historical market data from 2020 onwards
- Candle data across multiple timeframes (1min to 1month intervals)
- Expiries API for derivatives backtesting
- Contracts API for options and futures chain

#### [Initial API Release](#initial-api-release)

- Order management (place, modify, cancel) for equity and derivatives
- Smart orders (GTT, OCO) with segment-specific strategies
- Portfolio tracking (positions, holdings)
- Live market data (quotes, OHLC, market depth)
- Margin calculations and leverage information
- Instrument search and master data
- Multiple authentication flows (Access Token, API Key, TOTP)

---

### [Version Policy](#version-policy)

Groww Trading APIs follow partial semantic versioning (`MAJOR.MINOR`). Deprecated endpoints receive 6-month sunset notice.

[Previous

Annexures](/trade-api/docs/curl/annexures)
