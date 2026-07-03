# Groww Trade API — Margin (User Margin + Required Margin)

> **Source:** https://groww.in/trade-api/docs/python-sdk/margin
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Margin

This guide describes how to calculate required margin for orders and get available user margin using the SDK.

## [Get Available User Margin](#get-available-user-margin)

Easily retrieve your available margin details for equity, F&O, and commodity segments using this `get_available_margin_details` method.

### [Python SDK Usage](#python-sdk-usage)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
available_margin_details_response = groww.get_available_margin_details()
print(available_margin_details_response)
```

### [Response Payload](#response-payload)

All prices in rupees.

```
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
>
> - Product type (MIS for intraday, NRML for carry forward)
> - Contract value and volatility
> - Exchange regulations

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| clear\_cash | float | Clear cash available |
| net\_margin\_used | float | Net margin used |
| brokerage\_and\_charges | float | Brokerage and charges |
| collateral\_used | float | Collateral used |
| collateral\_available | float | Collateral available |
| adhoc\_margin | float | Adhoc margin available |
| net\_fno\_margin\_used | float | Net FnO margin used |
| span\_margin\_used | float | Span Margin Used (for F&O) |
| exposure\_margin\_used | float | Exposure Margin Used (for F&O) |
| future\_balance\_available | float | Future Balance Available |
| option\_buy\_balance\_available | float | Option Buy Balance Available |
| option\_sell\_balance\_available | float | Option Sell Balance Available |
| net\_equity\_margin\_used | float | Net equity margin used |
| cnc\_margin\_used | float | CNC margin used |
| mis\_margin\_used | float | MIS margin used |
| cnc\_balance\_available | float | CNC balance available |
| mis\_balance\_available | float | MIS balance available |
| commodity\_span\_margin | float | SPAN margin used for commodity trading |
| commodity\_exposure\_margin | float | Exposure margin used for commodity trading |
| commodity\_tender\_margin | float | Tender margin for commodity contracts nearing delivery |
| commodity\_special\_margin | float | Special margin levied during high volatility in commodities |
| commodity\_additional\_margin | float | Additional margin requirements for commodity positions |
| commodity\_unrealised\_m2m | float | Unrealised mark-to-market profit/loss for commodity positions |
| commodity\_realised\_m2m | float | Realised mark-to-market profit/loss for commodity positions |

---

## [Required Margin For Order](#required-margin-for-order)

Calculate the required margin for a single order or basket of orders using this `get_order_margin_details` method. Basket orders are only supported for `FNO` Segment.

### [Python SDK Usage](#python-sdk-usage-1)

```
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

#### [Request Schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| trading\_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | integer | Quantity of instrument to order |
| price | decimal | Price of the instrument in rupees case of Limit order |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) |
| segment `*` | string | [Segment](/trade-api/docs/python-sdk/annexures#segment) of the instrument such as CASH, FNO and COMMODITY. |
| product `*` | string | [Product type](/trade-api/docs/python-sdk/annexures#product) |
| order\_type `*` | string | [Order type](/trade-api/docs/python-sdk/annexures#order-type) |
| transaction\_type `*` | string | [Transaction type](/trade-api/docs/python-sdk/annexures#transaction-type) of the trade |

`*`required parameters

### [Response Payload](#response-payload-1)

All prices in rupees.

```
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

#### [Response Schema](#response-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| exposure\_required | float | Margin required to cover the exposure for the trade. |
| span\_required | float | SPAN margin required for F&O trades (not applicable for equity cash segment). |
| option\_buy\_premium | float | Premium amount required for buying options contracts. |
| brokerage\_and\_charges | float | Total brokerage and other exchange-related charges for the order. |
| total\_requirement | float | Total margin requirement including all charges and margin components. |
| cash\_cnc\_margin\_required | float | Margin required for CNC (Cash & Carry) orders in the cash segment. |
| cash\_mis\_margin\_required | float | Margin required for MIS (Margin Intraday Square-off) orders in the cash segment. |
| physical\_delivery\_margin\_requirement | float | Additional margin required for physical settlement of derivative contracts. |

[Previous

Portfolio](/trade-api/docs/python-sdk/portfolio)[Next

Live Data](/trade-api/docs/python-sdk/live-data)
