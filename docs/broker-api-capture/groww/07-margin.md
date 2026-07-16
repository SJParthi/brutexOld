# Groww Trading API — Margin

> Sources:
> - https://groww.in/trade-api/docs/curl/margin
> - https://groww.in/trade-api/docs/python-sdk/margin
> Captured: 2026-07-15

This guide describes how to calculate required margin for orders and get available user margin using the SDK.

Endpoints summary:

| Operation | Method + Path | Python SDK method |
| --- | --- | --- |
| Get Available User Margin | `GET /v1/margins/detail/user` | `groww.get_available_margin_details()` |
| Required Margin For Order(s) | `POST /v1/margins/detail/orders?segment=...` | `groww.get_order_margin_details(segment=..., orders=[...])` |

All prices in rupees.

---

## Get Available User Margin

`GET https://api.groww.in/v1/margins/detail/user`

Easily retrieve your margin details (for equity, F&O, and commodity segments) using this API.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/margins/detail/user \
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

available_margin_details_response = groww.get_available_margin_details()
print(available_margin_details_response)
```

### Response

cURL docs sample:

```json
{
  "status": "SUCCESS",
  "payload": {
    "clear_cash": 5000,
    "net_margin_used": 35000,
    "brokerage_and_charges": 200,
    "collateral_used": 3000,
    "collateral_available": 7000,
    "adhoc_margin": 1500,
    "fno_margin_details": {
      "net_fno_margin_used": 15000,
      "span_margin_used": 7000,
      "exposure_margin_used": 4000,
      "future_balance_available": 3000,
      "option_buy_balance_available": 11000,
      "option_sell_balance_available": 1000
    },
    "equity_margin_details": {
      "net_equity_margin_used": 10000,
      "cnc_margin_used": 5000,
      "mis_margin_used": 3000,
      "cnc_balance_available": 9000,
      "mis_balance_available": 1000
    },
    "commodity_margin_details": {
      "commodity_span_margin": 7000,
      "commodity_exposure_margin": 4000,
      "commodity_tender_margin": 2000,
      "commodity_special_margin": 1000,
      "commodity_additional_margin": 3000,
      "commodity_unrealised_m2m": 1500,
      "commodity_realised_m2m": 2500
    }
  }
}
```

Python SDK docs sample (payload only, real-world values):

```json
{
  "clear_cash": 96.21,
  "net_margin_used": 1.8,
  "brokerage_and_charges": 0.0,
  "collateral_used": 0.0,
  "collateral_available": 0.0,
  "adhoc_margin": 0.0,
  "fno_margin_details": {
    "net_fno_margin_used": 0.0,
    "span_margin_used": 0.0,
    "exposure_margin_used": 0.0,
    "future_balance_available": 94.41,
    "option_buy_balance_available": 94.41,
    "option_sell_balance_available": 94.41
  },
  "equity_margin_details": {
    "net_equity_margin_used": -1.8,
    "cnc_margin_used": -1.8,
    "mis_margin_used": 0.0,
    "cnc_balance_available": 94.41,
    "mis_balance_available": 94.41
  },
  "commodity_margin_details": {
    "commodity_span_margin": 7000,
    "commodity_exposure_margin": 4000,
    "commodity_tender_margin": 2000,
    "commodity_special_margin": 1000,
    "commodity_additional_margin": 3000,
    "commodity_unrealised_m2m": 1500,
    "commodity_realised_m2m": 2500
  }
}
```

> **Note:** Commodity orders require SPAN and Exposure margins. The margin requirement varies by:
> - Product type (MIS for intraday, NRML for carry forward)
> - Contract value and volatility
> - Exchange regulations

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| clear_cash | decimal/float | Clear cash available |
| net_margin_used | decimal/float | Net margin used |
| brokerage_and_charges | decimal/float | Brokerage and charges |
| collateral_used | decimal/float | Collateral used |
| collateral_available | decimal/float | Collateral available |
| adhoc_margin | decimal/float | Adhoc margin available |
| fno_margin_details.net_fno_margin_used | decimal/float | Net FnO margin used |
| fno_margin_details.span_margin_used | decimal/float | Span Margin Used (for F&O) |
| fno_margin_details.exposure_margin_used | decimal/float | Exposure Margin Used (for F&O) |
| fno_margin_details.future_balance_available | decimal/float | Future Balance Available |
| fno_margin_details.option_buy_balance_available | decimal/float | Option Buy Balance Available |
| fno_margin_details.option_sell_balance_available | decimal/float | Option Sell Balance Available |
| equity_margin_details.net_equity_margin_used | decimal/float | Net equity margin used |
| equity_margin_details.cnc_margin_used | decimal/float | CNC margin used |
| equity_margin_details.mis_margin_used | decimal/float | MIS margin used |
| equity_margin_details.cnc_balance_available | decimal/float | CNC balance available |
| equity_margin_details.mis_balance_available | decimal/float | MIS balance available |
| commodity_margin_details.commodity_span_margin | decimal/float | Commodity Span Margin (SPAN margin used for commodity trading) |
| commodity_margin_details.commodity_exposure_margin | decimal/float | Commodity Exposure Margin (exposure margin used for commodity trading) |
| commodity_margin_details.commodity_tender_margin | decimal/float | Commodity Tender Margin (tender margin for commodity contracts nearing delivery) |
| commodity_margin_details.commodity_special_margin | decimal/float | Commodity Special Margin (special margin levied during high volatility in commodities) |
| commodity_margin_details.commodity_additional_margin | decimal/float | Commodity Additional Margin (additional margin requirements for commodity positions) |
| commodity_margin_details.commodity_unrealised_m2m | decimal/float | Commodity Unrealised Mark-to-Market (unrealised M2M profit/loss for commodity positions) |
| commodity_margin_details.commodity_realised_m2m | decimal/float | Commodity Realised Mark-to-Market (realised M2M profit/loss for commodity positions) |

---

## Required Margin For Order

`POST https://api.groww.in/v1/margins/detail/orders?segment={segment}`

Calculate the required margin for a single order or basket of orders using this API.

- cURL docs: "Basket orders are supported for `FNO` and `COMMODITY` segments."
- Python SDK docs: "Basket orders are only supported for `FNO` Segment."

### cURL Request

```bash
# You can also use wget
curl -X POST https://api.groww.in/v1/margins/detail/orders?segment=CASH \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Body parameter (array of orders):

```json
[
  {
    "trading_symbol": "WIPRO",
    "transaction_type": "BUY",
    "quantity": 1,
    "price": 100,
    "order_type": "LIMIT",
    "product": "CNC",
    "exchange": "NSE"
  }
]
```

(`price` is Optional: include for limit orders; omit or adjust if not applicable.)

### Python SDK Usage

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

order_details = [

    {
        "trading_symbol": "RELIANCE",
        "transaction_type": groww.TRANSACTION_TYPE_BUY,
        "quantity": 1,
        "price": 2500, # Optional: Price (include for limit orders; omit or adjust if not applicable).
        "order_type": groww.ORDER_TYPE_LIMIT,
        "product": groww.PRODUCT_CNC,
        "exchange": groww.EXCHANGE_NSE
    }
]
order_margin_details_response = groww.get_order_margin_details(
  segment=groww.SEGMENT_CASH,
  orders=order_details,
)
print(order_margin_details_response)
```

### Request schema (per order in list)

| Name | Type | Description |
| --- | --- | --- |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | integer | Quantity of the instrument to order |
| price | decimal | Price of the instrument in rupees case of Limit order |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO and COMMODITY (query param in REST; `segment=` argument in SDK) |
| product `*` | string | [Product type](./13-annexures.md#product) |
| order_type `*` | string | [Order type](./13-annexures.md#order-type) |
| transaction_type `*` | string | [Transaction type](./13-annexures.md#transaction-type) of the trade |

`*` required parameters

### Response

cURL docs sample:

```json
{
  "status": "SUCCESS",
  "payload": {
    "exposure_required": 1000,
    "span_required": 1000,
    "option_buy_premium": 140,
    "brokerage_and_charges": 15,
    "total_requirement": 2115,
    "cash_cnc_margin_required": 310,
    "cash_mis_margin_required": 800,
    "physical_delivery_margin_requirement": 100
  }
}
```

Python SDK docs sample (payload only):

```json
{
  "exposure_required": 0.0,
  "span_required": 0.0,
  "option_buy_premium": 0.0,
  "brokerage_and_charges": 0.2,
  "total_requirement": 100.2,
  "cash_cnc_margin_required": 100.0,
  "physical_delivery_margin_requirement": 0.0
}
```

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| exposure_required | decimal/float | Required exposure (margin) for the order/trade |
| span_required | decimal/float | Required span margin for the order — SPAN margin for F&O trades (not applicable for equity cash segment) |
| option_buy_premium | decimal/float | Buy premium required for the order (premium amount for buying options contracts) |
| brokerage_and_charges | decimal/float | Brokerage and other exchange-related charges applied for the orders |
| total_requirement | decimal/float | Net/total margin requirement including all charges and margin components |
| cash_cnc_margin_required | decimal/float | Margin required applicable for CNC (Cash & Carry) orders in the cash segment |
| cash_mis_margin_required | decimal/float | Margin required applicable for MIS (Margin Intraday Square-off) orders in the cash segment |
| physical_delivery_margin_requirement | decimal/float | Physical delivery margin required if applicable (additional margin for physical settlement of derivative contracts) |
