---
source_url: https://groww.in/trade-api/docs/python-sdk
fetched_at: 2026-05-26
api: Groww Trading API Python SDK
section: Introduction
---

# Introduction

Welcome to the Groww Trading API. The APIs enable you to build and automate trading strategies with seamless access to real-time market data, order placement, portfolio management, and more.

This SDK wraps Groww's REST-like APIs into Python methods.

## Key Features

- **Trade with Ease**: Place, modify, and cancel orders across Equity, F&O, and Commodities.
- **Real-time Market Data**: Fetch live market prices, historical data, and order book depth.
- **Secure Authentication**: OAuth 2.0 for seamless and secure access.
- **Comprehensive SDK**: Python SDK with full coverage.
- **WebSockets for Streaming**: Subscribe to real-time market feeds and order updates.
- **Multi-Asset Trading**: Trade across NSE, BSE, and MCX exchanges.

---

## Getting Started

### Step 1: Prerequisites

Trading on Groww using Groww APIs requires:

- A Groww account.
- Basic knowledge of Python and REST APIs.
- A development environment with Python **3.9+** installed.
- An active Trading API Subscription. Purchase from: https://groww.in/user/profile/trading-apis

> **Note:** Groww Trading APIs support equity (CASH), derivatives (FNO), and commodities (COMMODITY) trading across NSE, BSE, and MCX exchanges.

### Step 2: Install the Python SDK

```bash
pip install growwapi
```

### Step 3: Authentication

There are two ways you can interact with GrowwAPI.

#### 1st Approach: API Key and Secret Flow

> Uses API Key and Secret — **Requires daily approval on Groww Cloud Api Keys Page**

Make sure you have the latest SDK version:

```bash
pip install --upgrade growwapi
```

Steps:
1. Go to the [Groww Cloud API Keys Page](https://groww.in/trade-api/api-keys).
2. Log in to your Groww account.
3. Click on 'Generate API key'.
4. Enter the name for the key and click Continue.
5. Copy API Key and Secret.

You can manage all keys from the same page.

```python
from growwapi import GrowwAPI
import pyotp

api_key = "YOUR_API_KEY"
secret = "YOUR_API_SECRET"

access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
# Use access_token to initiate GrowwAPI
groww = GrowwAPI(access_token)
```

#### 2nd Approach: TOTP Flow

> Uses TOTP token and TOTP QR code — **No Expiry**

Make sure you have the latest SDK version:

```bash
pip install --upgrade growwapi
```

Steps:
1. Go to the [Groww Cloud API Keys Page](https://groww.in/trade-api/api-keys).
2. Log in to your Groww account.
3. Click on 'Generate TOTP token' (under the dropdown of the 'Generate API Key' button).
4. Enter the name for the key and click Continue.
5. Copy the TOTP token and Secret, or scan the QR via a third-party authenticator app.

To use the TOTP flow, install the `pyotp` library:

```bash
pip install pyotp
```

```python
from growwapi import GrowwAPI
import pyotp

api_key = "YOUR_TOTP_TOKEN"

# totp can be obtained using the authenticator app or generated like this:
totp_gen = pyotp.TOTP('YOUR_TOTP_SECRET')
totp = totp_gen.now()

access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
# Use access_token to initiate GrowwAPI
groww = GrowwAPI(access_token)
```

### Step 4: Place your First Order

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"  # Access token generated using step 3.

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
    price=250,               # Optional: Price (for Limit orders)
    trigger_price=245,       # Optional: Trigger price (if applicable)
    order_reference_id="Ab-654321234-1628190"  # Optional: 8-20 char alphanumeric reference ID
)
print(place_order_response)
```

---

## Rate Limits

Rate limits are applied at the **type level**, not on individual APIs. This means all APIs grouped under a type (e.g., Orders, Live Data, Non Trading) share the same limit. If the limit for one API within a type is exhausted, all other APIs in that type will also be rate-limited until the limit window resets.

| Type | Requests | Limit (Per second) | Limit (Per minute) |
|---|---|---|---|
| Orders | Create, Modify and Cancel Order | 10 | 250 |
| Live Data | Market Quote, LTP, OHLC | 10 | 300 |
| Non Trading | Order Status, Order list, Trade list, Positions, Holdings, Margin | 20 | 500 |

For [Live feed](https://groww.in/trade-api/docs/python-sdk/feed), **up to 1000 subscriptions are allowed at a time**.
