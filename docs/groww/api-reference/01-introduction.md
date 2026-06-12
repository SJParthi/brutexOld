# 01 — Introduction · Groww Trading API (Python SDK)

> **Source:** https://groww.in/trade-api/docs/python-sdk
> **Verified:** 12 June 2026 · **Package:** `growwapi` · **Python:** 3.9+

Welcome to the Groww Trading API. The APIs let you build and automate trading strategies with access to real-time market data, order placement, portfolio management, and more. The SDK wraps Groww's REST-like APIs into easy-to-use Python methods. With it you can execute and modify orders in real time, manage your portfolio, access live market data, and more through a clean Python interface.

## Key Features

- **Trade with Ease:** Place, modify, and cancel orders across Equity, F&O, and Commodities.
- **Real-time Market Data:** Fetch live market prices, historical data, and order book depth.
- **Secure Authentication:** Industry-standard OAuth 2.0 for seamless and secure access.
- **Comprehensive SDK:** Get started quickly with the Python SDK.
- **WebSockets for Streaming:** Subscribe to real-time market feeds and order updates.
- **Multi-Asset Trading:** Trade across NSE, BSE, and MCX exchanges.

---

## Getting Started

### Step 1: Prerequisites
- A Groww account.
- Basic knowledge of Python and REST APIs.
- A development environment with **Python 3.9+** installed.
- An **active Trading API Subscription** — purchase from `https://groww.in/user/profile/trading-apis`.

> **Note:** Groww Trading APIs support equity (**CASH**), derivatives (**FNO**), and commodities (**COMMODITY**) trading. You can trade across **NSE, BSE, and MCX** exchanges.

### Step 2: Install the Python SDK
```bash
pip install growwapi
```

### Step 3: Authentication
There are two ways to interact with GrowwAPI.

#### 1st Approach: API Key and Secret Flow
*(Uses API Key and Secret — Requires daily approval on Groww Cloud API Keys Page)*

Make sure you have the latest SDK version:
```bash
pip install --upgrade growwapi
```
Steps:
1. Go to the Groww Cloud API Keys Page (`https://groww.in/trade-api/api-keys`).
2. Log in to your Groww account.
3. Click **Generate API key**.
4. Enter a name for the key and click **Continue**.
5. Copy the **API Key** and **Secret**. (You can manage all keys from the same page.)

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
*(Uses TOTP token and TOTP QR code — No Expiry)*

Make sure you have the latest SDK version:
```bash
pip install --upgrade growwapi
```
Steps:
1. Go to the Groww Cloud API Keys Page (`https://groww.in/trade-api/api-keys`).
2. Log in to your Groww account.
3. Click **Generate TOTP token** (under the dropdown next to the `Generate API Key` button).
4. Enter a name for the key and click **Continue**.
5. Copy the **TOTP token** and **Secret**, or scan the QR via a third-party authenticator app.
6. You can manage all keys from the same page.

Install the `pyotp` library:
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
    price=250,               # Optional: Price of the stock (for Limit orders)
    trigger_price=245,       # Optional: Trigger price (if applicable)
    order_reference_id="Ab-654321234-1628190"  # Optional: User provided 8 to 20 length alphanumeric reference ID to track the order
)
print(place_order_response)
```

---

## Rate Limits

Rate limits are applied **at the type level, not on individual APIs**. All APIs grouped under a type (e.g., Orders, Live Data, Non Trading) share the same limit. If the limit for one API within a type is exhausted, all other APIs in that type are rate-limited until the window resets.

| Type        | Requests                                                          | Limit (Per second) | Limit (Per minute) |
| ----------- | ----------------------------------------------------------------- | ------------------ | ------------------ |
| Orders      | Create, Modify and Cancel Order                                   | 10                 | 250                |
| Live Data   | Market Quote, LTP, OHLC                                           | 10                 | 300                |
| Non Trading | Order Status, Order list, Trade list, Positions, Holdings, Margin | 20                 | 500                |

For the **Live feed**, up to **1000 subscriptions** are allowed at a time.
