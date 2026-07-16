# Groww Trading API — Introduction

> Sources:
> - https://groww.in/trade-api/docs/curl (cURL Docs — "Introduction")
> - https://groww.in/trade-api/docs/python-sdk (Python SDK Docs — "Introduction")
> - https://groww.in/trade-api/docs (Documentation landing page)
> Captured: 2026-07-15

Welcome to the Groww Trading API! Our APIs enable you to build and automate trading strategies with seamless access to real-time market data, order placement, portfolio management, and more. Whether you're an experienced algo trader or just starting with automation, Groww's API is designed to be simple, powerful, and developer-friendly.

The documentation site offers two variants of the same docs:

| Variant | URL |
| --- | --- |
| Python SDK Documentation | https://groww.in/trade-api/docs/python-sdk |
| cURL Documentation | https://groww.in/trade-api/docs/curl |

The Python SDK wraps the REST-like APIs into easy-to-use Python methods, allowing you to focus on building your trading applications without worrying about the underlying API implementation details. With the Groww SDK, you can easily execute and modify orders in real time, manage your portfolio, access live market data, and more — all through a clean and intuitive Python interface.

## Key Features (from Python SDK docs)

- Trade with Ease: Place, modify, and cancel orders across Equity, F&O, and Commodities.
- Real-time Market Data: Fetch live market prices, historical data, and order book depth.
- Secure Authentication: Use industry-standard OAuth 2.0 for seamless and secure access.
- Comprehensive SDK: Get started quickly with our Python SDK.
- WebSockets for Streaming: Subscribe to real-time market feeds and order updates.
- Multi-Asset Trading: Trade across NSE, BSE, and MCX exchanges.

## Documentation sections (sidebar)

cURL docs sidebar:
Introduction, Instruments, Orders, Smart Orders, Portfolio, Margin, Live Data, Historical Data, Backtesting, User, Annexures, Changelog.

Python SDK docs sidebar (adds two sections):
Introduction, Instruments, Orders, Smart Orders, Portfolio, Margin, Live Data, Historical Data, Backtesting, **Feed**, User, Annexures, **Exceptions**, Changelog.

## Getting Started

### Step 1: Prerequisites

Trading on Groww using Groww APIs requires:

- A Groww account.
- Basic knowledge of REST APIs (cURL variant) / Basic knowledge of Python and REST APIs, and a development environment with **Python 3.9+** installed (Python variant).
- Having an active Trading API Subscription. You can purchase a subscription from this page: https://groww.in/user/profile/trading-apis

Segment support notes (the two doc variants state this differently — both quoted verbatim):

> **Important (cURL docs):** Groww Trading APIs currently support equity (CASH) and derivatives (FNO) trading only. Commodities trading (MCX segment) is not available at this time.

> **Note (Python SDK docs):** Groww Trading APIs support equity (CASH), derivatives (FNO), and commodities (COMMODITY) trading. You can trade across NSE, BSE, and MCX exchanges.

### Step 2 (Python): Install the Python SDK

```bash
pip install growwapi
```

To upgrade (required for API-key+secret and TOTP auth flows):

```bash
pip install --upgrade growwapi
```

### Step 3: Authentication

See [02-authentication.md](./02-authentication.md) for all three auth approaches (daily Access Token, API Key + Secret with checksum approval, TOTP flow), full request/response schemas and checksum generation code in Python/Java/.NET/JavaScript.

### Step 4 (Python): Place your First Order

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"  # Access token generated using step 3.,

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

place_order_response = groww.place_order(
    trading_symbol="WIPRO",
    quantity=1,
    validity=groww.VALIDITY_DAY,
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    product=groww.PRODUCT_CNC,
    order_type=groww.ORDER_TYPE_LIMIT,
    transaction_type=groww.TRANSACTION_TYPE_BUY,
    price=250,               # Optional: Price of the stock (for Limit orders)
    trigger_price=245,       # Optional: Trigger price (if applicable)
    order_reference_id="Ab-654321234-1628190"  # Optional: User provided 8 to 20 length alphanumeric reference ID to track the order
)
print(place_order_response)
```

---

# API Request and Response structure (REST)

Base URL: `https://api.groww.in`

## Headers

All requests must have the following headers, providing the generated access token in the Authorization header.

| Header Name | Header Value |
| ------------- | ----------------------- |
| Authorization | Bearer `{ACCESS_TOKEN}` |
| Accept | application/json |
| X-API-VERSION | 1.0 |

## Request structure

**GET** Requests: Send the required parameters as query parameters in the request. For example,

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/order/detail/{groww_order_id}?segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer ACCESS_TOKEN' \
  -H 'X-API-VERSION: 1.0'
```

Example response:

```json
{
  "status": "SUCCESS",
  "payload": {
    "groww_order_id": "GMK39038RDT490CCVRO",
    "trading_symbol": "RELIANCE-EQ",
    "order_status": "OPEN",
    "remark": "Order placed successfully",
    "quantity": 100,
    "price": 2500,
    "trigger_price": 2450,
    "filled_quantity": 100,
    "remaining_quantity": 10,
    "average_fill_price": 2500,
    "deliverable_quantity": 10,
    "amo_status": "PENDING",
    "validity": "DAY",
    "exchange": "NSE",
    "order_type": "MARKET",
    "transaction_type": "BUY",
    "segment": "CASH",
    "product": "CNC",
    "created_at": "2023-10-01T10:15:30",
    "exchange_time": "2023-10-01T10:15:30",
    "trade_date": "2019-08-24T14:15:22Z",
    "order_reference_id": "Ab-654321234-1628190"
  }
}
```

**POST** Requests: Parameters are sent in the request body as JSON. For example,

```bash
curl -X POST https://api.groww.in/v1/order/create \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0' \
  -d '{
    "validity": "DAY",
    "exchange": "NSE",
    "transaction_type": "BUY",
    "order_type": "MARKET",
    "price": 0,
    "product": "CNC",
    "quantity": 1,
    "segment": "CASH",
    "trading_symbol": "IDEA"
}'
```

Responses from the API are always JSON.

## Successful Request (HTTP 200 OK)

When a request is successfully processed, the API returns a JSON object with a status field set to **SUCCESS**. The payload field contains the requested data.

```json
{
    "status": "SUCCESS",
    "payload": {
        "symbolIsin": "INE002A01018",
        "productWisePositions": {}
    }
}
```

## Failed Request (HTTP 40x or 50x)

If a request fails, the API returns a JSON object with a status field set to **FAILURE**. The error field contains details about the failure.

```json
{
    "status": "FAILURE",
    "error": {
        "code": "GA001",
        "message": "Invalid trading symbol.",
        "metadata": null
    }
}
```

See [16-errors-exceptions.md](./16-errors-exceptions.md) for the full error-code table and Python SDK exception classes, and [15-rate-limits.md](./15-rate-limits.md) for rate limits.

## Error Codes (from Introduction page)

| Code | Message |
| ----- | --------------------------------------------- |
| GA000 | Internal error occurred |
| GA001 | Bad request |
| GA003 | Unable to serve request currently |
| GA004 | Requested entity does not exist |
| GA005 | User not authorised to perform this operation |
| GA006 | Cannot process this request |
| GA007 | Duplicate order reference id |

## Rate Limits (from Introduction page)

The rate limits are applied at the type level, not on individual APIs. This means that all APIs grouped under a type (e.g., Orders, Live Data, Non Trading) share the same limit. If the limit for one API within a type is exhausted, all other APIs in that type will also be rate-limited until the limit window resets.

| Type | Requests | Limit (Per second) | Limit (Per minute) |
| ----------- | ----------------------------------------------------------------- | ------------------ | ------------------ |
| Orders | Create, Modify and Cancel Order | 10 | 250 |
| Live Data | Market Quote, LTP, OHLC | 10 | 300 |
| Non Trading | Order Status, Order list, Trade list, Positions, Holdings, Margin | 20 | 500 |

For Live feed, upto 1000 subscriptions are allowed at a time.
