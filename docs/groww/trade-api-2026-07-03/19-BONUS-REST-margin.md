# Groww Trade API — REST API: Margin

> **Source:** https://groww.in/trade-api/docs/curl/margin
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Margin

This guide describes how to calculate required margin for orders and get available user margin using the SDK.

## [Get Available User Margin](#get-available-user-margin)

`GET https://api.groww.in/v1/margins/detail/user`

Easily retrieve your margin details using this API.

### [Request](#request)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/margins/detail/user \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### [Response](#response)

All prices in rupees.

```
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

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| clear\_cash | decimal | Clear cash available |
| net\_margin\_used | decimal | Net margin used |
| brokerage\_and\_charges | decimal | Brokerage and charges |
| collateral\_used | decimal | Collateral used |
| collateral\_available | decimal | Collateral available |
| adhoc\_margin | decimal | Adhoc margin available |
| net\_fno\_margin\_used | decimal | Net FnO margin used |
| span\_margin\_used | decimal | Span Margin Used |
| exposure\_margin\_used | decimal | Exposure Margin Used |
| future\_balance\_available | decimal | Future Balance Available |
| option\_buy\_balance\_available | decimal | Option Buy Balance Available |
| option\_sell\_balance\_available | decimal | Option Sell Balance Available |
| net\_equity\_margin\_used | decimal | Net equity margin used |
| cnc\_margin\_used | decimal | CNC margin used |
| mis\_margin\_used | decimal | MIS margin used |
| cnc\_balance\_available | decimal | CNC balance available |
| mis\_balance\_available | decimal | MIS balance available |
| commodity\_span\_margin | decimal | Commodity Span Margin |
| commodity\_exposure\_margin | decimal | Commodity Exposure Margin |
| commodity\_tender\_margin | decimal | Commodity Tender Margin |
| commodity\_special\_margin | decimal | Commodity Special Margin |
| commodity\_additional\_margin | decimal | Commodity Additional Margin |
| commodity\_unrealised\_m2m | decimal | Commodity Unrealised Mark-to-Market |
| commodity\_realised\_m2m | decimal | Commodity Realised Mark-to-Market |

## [Required Margin For Order](#required-margin-for-order)

`POST https://api.groww.in/v1/margins/detail/orders`

Calculate the required margin for a single order or basket of orders using this API. Basket orders are supported for `FNO` and `COMMODITY` segments.

### [Request](#request-1)

```
# You can also use wget
curl -X POST https://api.groww.in/v1/margins/detail/orders?segment=CASH \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### [Body parameter](#body-parameter)

```
[
  {
    "trading_symbol": "WIPRO",
    "transaction_type": "BUY",
    "quantity": 1,
    "price": 100, // Optional: Price (include for limit orders; omit or adjust if not applicable).
    "order_type": "LIMIT",
    "product": "CNC",
    "exchange": "NSE"
  }
]
```

#### [Request schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| trading\_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | integer | Quantity of the instrument to order |
| price | decimal | Price of the instrument in rupees case of Limit order |
| exchange `*` | string | [Stock exchange](/trade-api/docs/curl/annexures#exchange) |
| segment `*` | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| product `*` | string | [Product type](/trade-api/docs/curl/annexures#product) |
| order\_type `*` | string | [Order type](/trade-api/docs/curl/annexures#order-type) |
| transaction\_type `*` | string | [Transaction type](/trade-api/docs/curl/annexures#transaction-type) of the trade |

`*`required parameters

### [Response](#response-1)

All prices in rupees.

```
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

#### [Response Schema](#response-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| exposure\_required | decimal | Required exposure for the order |
| span\_required | decimal | Required span margin for the order |
| option\_buy\_premium | decimal | Buy premium required for the order |
| brokerage\_and\_charges | decimal | Brokerage and Charges applied for the orders |
| total\_requirement | decimal | Net margin required |
| cash\_cnc\_margin\_required | decimal | Margin required applicable for CNC orders |
| cash\_mis\_margin\_required | decimal | Margin required applicable for MIS orders |
| physical\_delivery\_margin\_requirement | decimal | Physical delivery margin required if applicable |

[Previous

Portfolio](/trade-api/docs/curl/portfolio)[Next

Live Data](/trade-api/docs/curl/live-data)
