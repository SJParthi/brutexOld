# Groww Trading API — Portfolio (Holdings & Positions)

> Sources:
> - https://groww.in/trade-api/docs/curl/portfolio
> - https://groww.in/trade-api/docs/python-sdk/portfolio
> Captured: 2026-07-15

Get detailed information about your holdings and positions.

Endpoints summary:

| Operation | Method + Path | Python SDK method |
| --- | --- | --- |
| Get Holdings | `GET /v1/holdings/user` | `groww.get_holdings_for_user(timeout=...)` |
| Get Positions for User | `GET /v1/positions/user` | `groww.get_positions_for_user(segment=...)` |
| Get Position for Trading Symbol | `GET /v1/positions/trading-symbol` | `groww.get_position_for_trading_symbol(trading_symbol=..., segment=...)` |

All prices in rupees.

---

## Get Holdings

`GET https://api.groww.in/v1/holdings/user`

This API can be used to retrieve the current stock holdings of the user stored in the user's DEMAT account.

Holdings represent the user's collection of long-term equity delivery stocks. An asset in a holdings portfolio stays there indefinitely unless it is sold, delisted, or modified by the exchanges. Fundamentally, the assets in the holdings are stored in the user's DEMAT account, as processed by exchanges and clearing institutions.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/holdings/user \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

# Optional: timeout parameter (in seconds) for the API call; default is typically set by the SDK.
holdings_response = groww.get_holdings_for_user(timeout=5)
print(holdings_response)
```

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "holdings": [
      {
        "isin": "INE545U01014",
        "trading_symbol": "RELIANCE",
        "quantity": 10,
        "average_price": 100,
        "pledge_quantity": 2,
        "demat_locked_quantity": 1,
        "groww_locked_quantity": 1.5,
        "repledge_quantity": 0.5,
        "t1_quantity": 3,
        "demat_free_quantity": 5,
        "corporate_action_additional_quantity": 1,
        "active_demat_transfer_quantity": 1
      }
    ]
  }
}
```

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| isin | string | The ISIN (International Securities Identification number) of the symbol |
| trading_symbol | string | The trading symbol of the holding |
| quantity | integer (SDK docs: float) | The net quantity of the holding |
| average_price | decimal (SDK docs: int) | The average price of the holding in Rupees |
| pledge_quantity | decimal/float | The pledged quantity of the holding |
| demat_locked_quantity | decimal/float | The demat locked quantity of the holding |
| groww_locked_quantity | decimal/float | The Groww locked quantity of the holding |
| repledge_quantity | decimal/float | The repledged quantity of the holding |
| t1_quantity | decimal/float | The T1 quantity of the holding |
| demat_free_quantity | decimal/float | The demat free quantity of the holding |
| corporate_action_additional_quantity | integer | The corporate action additional quantity of the holding |
| active_demat_transfer_quantity | integer | The active demat transfer quantity of the holding |

---

## Get Positions for User

`GET https://api.groww.in/v1/positions/user`

This API can be used to get the positions of the user. Positions are the assets that the user holds in his/her account. This includes positions from equity (CASH), derivatives (FNO), and commodity (COMMODITY) segments.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/positions/user \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

# Get all positions (CASH, FNO, and COMMODITY)
user_positions_response = groww.get_positions_for_user()

# Get positions for specific segment
cash_positions_response = groww.get_positions_for_user(segment=groww.SEGMENT_CASH)
fno_positions_response = groww.get_positions_for_user(segment=groww.SEGMENT_FNO)
commodity_positions_response = groww.get_positions_for_user(segment=groww.SEGMENT_COMMODITY)

print(user_positions_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. — `CASH` `FNO` `COMMODITY` (optional; omit for all segments) |

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "RELIANCE",
        "segment": "CASH",
        "credit_quantity": 10,
        "credit_price": 12500,
        "debit_quantity": 5,
        "debit_price": 12000,
        "carry_forward_credit_quantity": 8,
        "carry_forward_credit_price": 12300,
        "carry_forward_debit_quantity": 3,
        "carry_forward_debit_price": 11800,
        "exchange": "NSE",
        "symbol_isin": "INE123A01016",
        "quantity": 15,
        "product": "CNC",
        "net_carry_forward_quantity": 10,
        "net_price": 12400,
        "net_carry_forward_price": 12200,
        "realised_pnl": 500
      }
    ]
  }
}
```

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| trading_symbol | string | Trading symbol of the instrument |
| segment | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| credit_quantity | int | Quantity of credited instruments |
| credit_price | int | Average price in rupees of credited instruments |
| debit_quantity | int | Quantity of debited instruments |
| debit_price | int | Average price in rupees of debited instruments |
| carry_forward_credit_quantity | int | Quantity of carry forward credited instruments |
| carry_forward_credit_price | int | Average price in rupees of carry forward credited instruments |
| carry_forward_debit_quantity | int | Quantity of carry forward debited instruments |
| carry_forward_debit_price | int | Average price in rupees of carry forward debited instruments |
| exchange | string | [Stock exchange](./13-annexures.md#exchange) |
| symbol_isin | string | ISIN (International Securities Identification number) of the symbol |
| quantity | int | Net quantity of instruments |
| product | string | [Product type](./13-annexures.md#product) |
| net_carry_forward_quantity | int | Net carry forward quantity of instruments |
| net_price | int | Net average price in rupees of instruments |
| net_carry_forward_price | int | Net average price in rupees of carry forward instruments |
| realised_pnl | int | Realised profit and loss in rupees for the instrument |

---

## Get Position for Trading Symbol

`GET https://api.groww.in/v1/positions/trading-symbol`

This API can be used to get the positions of the user for a particular instrument.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/positions/trading-symbol?trading_symbol=string&segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

trading_symbol_position_response = groww.get_position_for_trading_symbol(trading_symbol="RELIANCE", segment=groww.SEGMENT_CASH)
print(trading_symbol_position_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| segment | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. — `CASH` `FNO` `COMMODITY` (marked required `*` in Python SDK docs) |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "RELIANCE",
        "segment": "CASH",
        "credit_quantity": 10,
        "credit_price": 12500,
        "debit_quantity": 5,
        "debit_price": 12000,
        "carry_forward_credit_quantity": 8,
        "carry_forward_credit_price": 12300,
        "carry_forward_debit_quantity": 3,
        "carry_forward_debit_price": 11800,
        "exchange": "NSE",
        "symbol_isin": "INE123A01016",
        "quantity": 15,
        "product": "CNC",
        "net_carry_forward_quantity": 10,
        "net_price": 12400,
        "net_carry_forward_price": 12200,
        "realised_pnl": 500
      }
    ]
  }
}
```

### Response Schema

Same fields as "Get Positions for User" above (trading_symbol, segment, credit_quantity, credit_price, debit_quantity, debit_price, carry_forward_credit_quantity, carry_forward_credit_price, carry_forward_debit_quantity, carry_forward_debit_price, exchange, symbol_isin, quantity, product, net_carry_forward_quantity, net_price, net_carry_forward_price, realised_pnl).
